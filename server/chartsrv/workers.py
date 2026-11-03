"""CPU-heavy history work in worker processes: AMT engine loads (90 days of minutes through the Toolkit), charts
built from raw trades (tick, volume, range and seconds bars), session volume profiles, indicator alerts' first looks
on long settings (their series stepped through 3000 bars), and seconds charts' HTF EMA seed (up to 4 million stored
trades). In a thread that work holds this process's GIL for seconds, and everything else waits behind it: other
charts, the API, live ticks. In worker processes it runs on other cores and the server stays quick.

A job takes the object to fill (a Series, an AmtRunner, a ProfileBook, an alert's state), fills it in the worker
(from the database, or the bars it brings) and sends it back; the caller swaps it in. With CHART_WORKERS=0, or when
a worker dies, the job runs in a thread instead, as before."""
import asyncio
import concurrent.futures as cf
import logging
import multiprocessing as mp
import os
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import resource_tracker

log = logging.getLogger(__name__)
# Two pools, so a chart never waits behind the AMT loads of other charts: "chart" builds bars and profiles,
# "study" runs the AMT engine and indicator alerts' history replays (an AMT load can wait behind those).
_pools: dict[str, cf.ProcessPoolExecutor] = {}
_closed = False   # shut down (the server stopping): no job starts workers again until start()


def size() -> int:
    """Worker processes in all: CHART_WORKERS, else one fewer than the cores (up to 4). 0: threads only."""
    v = os.environ.get("CHART_WORKERS", "").strip()
    if v.isdigit():
        return min(int(v), 8)
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def _sizes() -> dict[str, int]:
    n = size()
    chart = 1 if n <= 2 else 2
    return {"chart": chart, "study": max(1, n - chart)}


def _pool(kind: str) -> cf.ProcessPoolExecutor | None:
    if size() == 0:
        return None
    if kind not in _pools:   # spawn: no fork of a server that runs threads; each worker gets the holiday calendar
        from . import sessions
        _pools[kind] = cf.ProcessPoolExecutor(max_workers=_sizes()[kind], mp_context=mp.get_context("spawn"),
                                              initializer=sessions.set_calendar, initargs=(sessions.calendar(),))
    return _pools[kind]


def _ready() -> int:
    from . import amt_hub, market, studies   # noqa: F401  (imported once per worker, before the first real job)
    return os.getpid()


def start():
    """Starts the workers now, so the first chart doesn't wait for them to import (again after a shutdown: a server
    started again in this process, as the tests' are)."""
    global _closed
    _closed = False
    for kind, n in _sizes().items():
        p = _pool(kind)
        if p is not None:
            for _ in range(n):
                p.submit(_ready)


async def run(kind: str, fn, *args):
    """fn(*args) in a worker process of the `kind` pool, or in a thread when there are none. fn and its arguments
    must pickle; the worker gets copies, so fn returns what it filled. After shutdown() it is cancelled, as a job the
    shutdown found queued is (its caller ends quietly), and starts no new workers."""
    if _closed:
        raise asyncio.CancelledError
    p = _pool(kind)
    if p is not None:
        try:
            return await asyncio.get_running_loop().run_in_executor(p, fn, *args)
        except BrokenProcessPool:   # a worker died (out of memory?): this job in a thread, a new pool next time
            log.warning("a %s worker process stopped; starting new ones", kind)
            if _pools.get(kind) is p:
                del _pools[kind]
                p.shutdown(wait=False, cancel_futures=True)
    return await asyncio.to_thread(fn, *args)


def shutdown(wait: bool = False):
    """wait: until the worker processes have ended (each finishes the job it is on first), and multiprocessing's
    resource tracker too. A restart in place (os.execv) keeps this process, and the new program never waits for
    children of the old one: each would stay a zombie for as long as the container runs. And no job after it starts
    new ones (until start()): the server's own shutdown goes on for a few passes of the loop."""
    global _closed
    _closed = True
    for p in _pools.values():
        p.shutdown(wait=wait, cancel_futures=True)
    _pools.clear()
    if wait:
        resource_tracker._resource_tracker._stop()   # the helper process the pools shared; started again when needed


# --- the jobs (top level, so a worker can import them) ----------------------------------------------------
def build_series(db_path, series, symbol, spec, cutoff_rowid, split_t, now):
    from .market import build_history
    build_history(db_path, series, symbol, spec, cutoff_rowid, split_t, now)
    return series


def load_amt(db_path, runner, symbol, split):
    from .amt_hub import _load_history
    _load_history(db_path, runner, symbol, split)
    return runner


def load_book(db_path, book, symbol, now, cutoff_rowid):
    book.load(db_path, symbol, now, cutoff_rowid)
    return book


def replay_alert(state):
    """An indicator alert's state (ind_alerts._State) stepped on through the closed bars it has left."""
    state.replay()
    return state
