from __future__ import print_function, division, absolute_import

import gc
import logging
import os
import random
import sys
import multiprocessing as mp

import numpy as np

import pytest
from toolz import valmap, first
from tornado import gen
from tornado.ioloop import IOLoop

import dask
from distributed import Nanny, rpc, Scheduler, Worker
from distributed.core import CommClosedError
from distributed.metrics import time
from distributed.protocol.pickle import dumps
from distributed.utils import ignoring, tmpfile
from distributed.utils_test import (  # noqa: F401
    gen_cluster,
    gen_test,
    inc,
    captured_logger,
    cleanup,
)


@gen_cluster(nthreads=[])
def test_nanny(s):
    n = yield Nanny(s.address, nthreads=2, loop=s.loop)

    with rpc(n.address) as nn:
        assert n.is_alive()
        assert s.nthreads[n.worker_address] == 2
        assert s.workers[n.worker_address].nanny == n.address

        yield nn.kill()
        assert not n.is_alive()
        assert n.worker_address not in s.nthreads
        assert n.worker_address not in s.workers

        yield nn.kill()
        assert not n.is_alive()
        assert n.worker_address not in s.nthreads
        assert n.worker_address not in s.workers

        yield nn.instantiate()
        assert n.is_alive()
        assert s.nthreads[n.worker_address] == 2
        assert s.workers[n.worker_address].nanny == n.address

        yield nn.terminate()
        assert not n.is_alive()

    yield n.close()


@gen_cluster(nthreads=[])
def test_many_kills(s):
    n = yield Nanny(s.address, nthreads=2, loop=s.loop)
    assert n.is_alive()
    yield [n.kill() for i in range(5)]
    yield [n.kill() for i in range(5)]
    yield n.close()


@gen_cluster(Worker=Nanny)
def test_str(s, a, b):
    assert a.worker_address in str(a)
    assert a.worker_address in repr(a)
    assert str(a.nthreads) in str(a)
    assert str(a.nthreads) in repr(a)


@gen_cluster(nthreads=[], timeout=20, client=True)
def test_nanny_process_failure(c, s):
    n = yield Nanny(s.address, nthreads=2, loop=s.loop)
    first_dir = n.worker_dir

    assert os.path.exists(first_dir)

    original_address = n.worker_address
    ww = rpc(n.worker_address)
    yield ww.update_data(data=valmap(dumps, {"x": 1, "y": 2}))
    pid = n.pid
    assert pid is not None
    with ignoring(CommClosedError):
        yield c.run(os._exit, 0, workers=[n.worker_address])

    start = time()
    while n.pid == pid:  # wait while process dies and comes back
        yield gen.sleep(0.01)
        assert time() - start < 5

    start = time()
    yield gen.sleep(1)
    while not n.is_alive():  # wait while process comes back
        yield gen.sleep(0.01)
        assert time() - start < 5

    # assert n.worker_address != original_address  # most likely

    start = time()
    while n.worker_address not in s.nthreads or n.worker_dir is None:
        yield gen.sleep(0.01)
        assert time() - start < 5

    second_dir = n.worker_dir

    yield n.close()
    assert not os.path.exists(second_dir)
    assert not os.path.exists(first_dir)
    assert first_dir != n.worker_dir
    ww.close_rpc()
    s.stop()


@gen_cluster(nthreads=[])
def test_run(s):
    pytest.importorskip("psutil")
    n = yield Nanny(s.address, nthreads=2, loop=s.loop)

    with rpc(n.address) as nn:
        response = yield nn.run(function=dumps(lambda: 1))
        assert response["status"] == "OK"
        assert response["result"] == 1

    yield n.close()


@pytest.mark.slow
@gen_cluster(
    Worker=Nanny, nthreads=[("127.0.0.1", 1)], worker_kwargs={"reconnect": False}
)
def test_close_on_disconnect(s, w):
    yield s.close()

    start = time()
    while w.status != "closed":
        yield gen.sleep(0.05)
        assert time() < start + 9


class Something(Worker):
    # a subclass of Worker which is not Worker
    pass


@gen_cluster(client=True, Worker=Nanny)
def test_nanny_worker_class(c, s, w1, w2):
    out = yield c._run(lambda dask_worker=None: str(dask_worker.__class__))
    assert "Worker" in list(out.values())[0]
    assert w1.Worker is Worker


@gen_cluster(client=True, Worker=Nanny, worker_kwargs={"worker_class": Something})
def test_nanny_alt_worker_class(c, s, w1, w2):
    out = yield c._run(lambda dask_worker=None: str(dask_worker.__class__))
    assert "Something" in list(out.values())[0]
    assert w1.Worker is Something


@pytest.mark.slow
@gen_cluster(client=False, nthreads=[])
def test_nanny_death_timeout(s):
    yield s.close()
    w = Nanny(s.address, death_timeout=1)
    with pytest.raises(gen.TimeoutError):
        yield w

    assert w.status == "closed"


@gen_cluster(client=True, Worker=Nanny)
def test_random_seed(c, s, a, b):
    @gen.coroutine
    def check_func(func):
        x = c.submit(func, 0, 2 ** 31, pure=False, workers=a.worker_address)
        y = c.submit(func, 0, 2 ** 31, pure=False, workers=b.worker_address)
        assert x.key != y.key
        x = yield x
        y = yield y
        assert x != y

    yield check_func(lambda a, b: random.randint(a, b))
    yield check_func(lambda a, b: np.random.randint(a, b))


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="num_fds not supported on windows"
)
@gen_cluster(client=False, nthreads=[])
def test_num_fds(s):
    psutil = pytest.importorskip("psutil")
    proc = psutil.Process()

    # Warm up
    w = yield Nanny(s.address)
    yield w.close()
    del w
    gc.collect()

    before = proc.num_fds()

    for i in range(3):
        w = yield Nanny(s.address)
        yield gen.sleep(0.1)
        yield w.close()

    start = time()
    while proc.num_fds() > before:
        print("fds:", before, proc.num_fds())
        yield gen.sleep(0.1)
        assert time() < start + 10


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Need 127.0.0.2 to mean localhost"
)
@gen_cluster(client=True, nthreads=[])
def test_worker_uses_same_host_as_nanny(c, s):
    for host in ["tcp://0.0.0.0", "tcp://127.0.0.2"]:
        n = yield Nanny(s.address, host=host)

        def func(dask_worker):
            return dask_worker.listener.listen_address

        result = yield c.run(func)
        assert host in first(result.values())
        yield n.close()


@gen_test()
def test_scheduler_file():
    with tmpfile() as fn:
        s = yield Scheduler(scheduler_file=fn, port=8008)
        w = yield Nanny(scheduler_file=fn)
        assert set(s.workers) == {w.worker_address}
        yield w.close()
        s.stop()


@gen_cluster(client=True, Worker=Nanny, nthreads=[("127.0.0.1", 2)])
def test_nanny_timeout(c, s, a):
    x = yield c.scatter(123)
    with captured_logger(
        logging.getLogger("distributed.nanny"), level=logging.ERROR
    ) as logger:
        response = yield a.restart(timeout=0.1)

    out = logger.getvalue()
    assert "timed out" in out.lower()

    start = time()
    while x.status != "cancelled":
        yield gen.sleep(0.1)
        assert time() < start + 7


@gen_cluster(
    nthreads=[("127.0.0.1", 1)],
    client=True,
    Worker=Nanny,
    worker_kwargs={"memory_limit": 1e8},
    timeout=20,
    clean_kwargs={"threads": False},
)
def test_nanny_terminate(c, s, a):
    from time import sleep

    def leak():
        L = []
        while True:
            L.append(b"0" * 5000000)
            sleep(0.01)

    proc = a.process.pid
    with captured_logger(logging.getLogger("distributed.nanny")) as logger:
        future = c.submit(leak)
        start = time()
        while a.process.pid == proc:
            yield gen.sleep(0.1)
            assert time() < start + 10
        out = logger.getvalue()
        assert "restart" in out.lower()
        assert "memory" in out.lower()


@gen_cluster(nthreads=[], client=True)
def test_avoid_memory_monitor_if_zero_limit(c, s):
    nanny = yield Nanny(s.address, loop=s.loop, memory_limit=0)
    typ = yield c.run(lambda dask_worker: type(dask_worker.data))
    assert typ == {nanny.worker_address: dict}
    pcs = yield c.run(lambda dask_worker: list(dask_worker.periodic_callbacks))
    assert "memory" not in pcs
    assert "memory" not in nanny.periodic_callbacks

    future = c.submit(inc, 1)
    assert (yield future) == 2
    yield gen.sleep(0.02)

    yield c.submit(inc, 2)  # worker doesn't pause

    yield nanny.close()


@gen_cluster(nthreads=[], client=True)
def test_scheduler_address_config(c, s):
    with dask.config.set({"scheduler-address": s.address}):
        nanny = yield Nanny(loop=s.loop)
        assert nanny.scheduler.address == s.address

        start = time()
        while not s.workers:
            yield gen.sleep(0.1)
            assert time() < start + 10

    yield nanny.close()


@pytest.mark.slow
@gen_test(timeout=20)
def test_wait_for_scheduler():
    with captured_logger("distributed") as log:
        w = Nanny("127.0.0.1:44737")
        IOLoop.current().add_callback(w.start)
        yield gen.sleep(6)
        yield w.close()

    log = log.getvalue()
    assert "error" not in log.lower(), log
    assert "restart" not in log.lower(), log


@gen_cluster(nthreads=[], client=True)
def test_environment_variable(c, s):
    a = Nanny(s.address, loop=s.loop, memory_limit=0, env={"FOO": "123"})
    b = Nanny(s.address, loop=s.loop, memory_limit=0, env={"FOO": "456"})
    yield [a, b]
    results = yield c.run(lambda: os.environ["FOO"])
    assert results == {a.worker_address: "123", b.worker_address: "456"}
    yield [a.close(), b.close()]


@gen_cluster(nthreads=[], client=True)
def test_data_types(c, s):
    w = yield Nanny(s.address, data=dict)
    r = yield c.run(lambda dask_worker: type(dask_worker.data))
    assert r[w.worker_address] == dict
    yield w.close()


def _noop(x):
    """Define here because closures aren't pickleable."""
    pass


@gen_cluster(
    nthreads=[("127.0.0.1", 1)],
    client=True,
    Worker=Nanny,
    config={"distributed.worker.daemon": False},
)
def test_mp_process_worker_no_daemon(c, s, a):
    def multiprocessing_worker():
        p = mp.Process(target=_noop, args=(None,))
        p.start()
        p.join()

    yield c.submit(multiprocessing_worker)


@gen_cluster(
    nthreads=[("127.0.0.1", 1)],
    client=True,
    Worker=Nanny,
    config={"distributed.worker.daemon": False},
)
def test_mp_pool_worker_no_daemon(c, s, a):
    def pool_worker(world_size):
        with mp.Pool(processes=world_size) as p:
            p.map(_noop, range(world_size))

    yield c.submit(pool_worker, 4)


@pytest.mark.asyncio
async def test_nanny_closes_cleanly(cleanup):
    async with Scheduler() as s:
        n = await Nanny(s.address)
        assert n.process.pid
        proc = n.process.process
        await n.close()
        assert not n.process
        assert not proc.is_alive()
        assert proc.exitcode == 0
