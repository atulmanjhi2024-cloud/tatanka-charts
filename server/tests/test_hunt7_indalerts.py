"""Fixes from the seventh bug hunt of indicator alerts: a first look that runs a little over its time on the event loop
(Stochastics, MFI, Donchian, Williams %R at their defaults on a busy PC) is finished there on the loop's next pass,
not sent to wait seconds behind long replays in the study worker; the loop's chain of put-off first looks stops with
the market, and no job after the workers' shutdown starts new ones (a restart in place left them behind)."""
import asyncio
import gc
import multiprocessing as mp
import os
import time
from multiprocessing import resource_tracker
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from chartsrv import ind_alerts as ia
from chartsrv import workers
from test_hunt6_indalerts import EMA, _recording
from test_redesign_ind_alert_state import KEY, LINREG, _alert, _engine, _Pool, _same, _trade, _watch
from test_review_fixes import build_market

# CPU a bar of each series (the price's and others' 1 us): Regression(1000) 1.8 s over 3000 bars, Donchian 45 ms,
# Stochastics 27 ms, a little over INLINE_S as on a slower or busy PC
COST = {"linreg": 600e-6, "donch": 14e-6, "stoch": 8e-6}


def _cpu(monkeypatch):
    """ind_alerts' thread CPU clock made one that moves only as bars are stepped, COST a bar: first looks stop where
    they would on such a PC, the same on every run. Its reading, [seconds]."""
    clock = [0.0]
    step = ia.Line.step

    def costly(self, bar, commit=True):
        if commit:
            clock[0] += COST.get(self.d.get("ind"), 1e-6)
        return step(self, bar, commit)
    monkeypatch.setattr(ia.Line, "step", costly)
    monkeypatch.setattr(ia, "time", SimpleNamespace(thread_time=lambda: clock[0], perf_counter=time.perf_counter))
    return clock


def _shorts():
    stoch = ia.clean_params({"ind": "price"}, {"ind": "stoch", "line": "k"}, "1m", "bar", "cross")
    donch = ia.clean_params({"ind": "price"}, {"ind": "donch", "line": "upper"}, "1m", "bar", "cross")
    return stoch, donch


# ---- a first look a little over its time: the rest on the loop's next pass -------------------------------------------

def test_a_first_look_a_little_over_its_time_is_finished_on_the_loop_not_behind_a_long_replay(tmp_path, monkeypatch):
    """ia6-3's case on a PC where Stochastics' first look takes 27 ms: Regression(1000) first, then short ones. The
    Stochastics one stops at INLINE_S with a quarter of its bars left: it is finished on the loop's next pass, not
    sent to the study worker behind the long replay (seconds without being checked). Donchian's, with more than half
    left but its rest a pass or two long, too (ia9-1); only the long one goes there. Each pass holds the loop about
    INLINE_S, and the states are the ones made in one go."""
    pool = _Pool()   # the replays never come back here
    monkeypatch.setattr(workers, "run", pool.run)
    made = _recording(monkeypatch)
    clock = _cpu(monkeypatch)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    stoch, donch = _shorts()
    alerts = [_alert(ia.clean_params({**LINREG, "len": 1000}, {"ind": "price"}, "1m", "bar", "above"), i=1),
              _alert(stoch, i=10), _alert(donch, i=11),
              _alert(ia.clean_params({"ind": "price"}, EMA, "1m", "bar", "cross"), i=12)]
    ind.m.alerts.alerts = {a.id: a for a in alerts}
    held, look = [], ind._look_later

    def timed():   # each pass's CPU, on that clock
        c = clock[0]
        look()
        held.append(clock[0] - c)
    ind._look_later = timed

    async def go():
        tk = _trade(ind, w, 10_000, 6100.0)
        assert [ind.check(a, tk) for a in alerts] == [None] * 4   # the long one's; the others put off
        await asyncio.sleep(0)   # Stochastics' first look: stopped a quarter short
        mid = (list(ind.replaying), list(ind._later), list(pool.jobs))
        tk = _trade(ind, w, 10_001, 6100.0)   # a trade before its rest: not checked, nor looked at again
        assert [ind.check(a, tk) for a in alerts] == [None] * 4
        for _ in range(10):
            await asyncio.sleep(0)
        return mid
    mid = asyncio.run(go())
    assert mid[0] == [1, 10] and mid[1][0] == 10 and mid[2] == ["study"], mid
    assert pool.jobs == ["study"] and sorted(ind.replaying) == [1], (pool.jobs, ind.replaying)
    assert sorted(ind.states) == [10, 11, 12] and not ind._later
    assert [p for p, _ in made].count(stoch) == 1 and [b for _, b in made] == [ia.INLINE_S] * 4, made
    assert max(held) < ia.INLINE_S * 1.1, held
    assert _same(ind.states[10], ia._State(stoch, w)) and ind.states[10].catch_up
    assert _same(ind.states[11], ia._State(donch, w)) and ind.states[11].catch_up


def test_a_first_look_left_to_finish_on_the_loop_is_dropped_when_paused_or_its_bars_load_again(tmp_path, monkeypatch):
    """Paused before its rest came round, it is not finished nor kept: turned on again, it starts afresh. Its bars
    loaded again meanwhile (a gap fill): the next trade makes a first look on the new ones."""
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    made = _recording(monkeypatch)
    _cpu(monkeypatch)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    stoch, _ = _shorts()
    a = _alert(stoch, i=10)
    ind.m.alerts.alerts = {a.id: a}

    async def go():
        ind.check(a, _trade(ind, w, 10_000, 6100.0))
        queued = list(ind._later)
        a.enabled = False   # paused before the loop came round
        ind.ensure()
        ind.watches[KEY] = w
        await asyncio.sleep(0)
        paused = (a.id in ind.states, a.id in ind.replaying, list(ind._later))
        a.enabled = True
        ind.check(a, _trade(ind, w, 10_001, 6100.0))   # starts afresh
        w2 = ind.watches[KEY] = _watch(version=2)   # its bars loaded again
        await asyncio.sleep(0)
        reloaded = (a.id in ind.states, list(ind._later))
        ind.check(a, _trade(ind, w2, 10_002, 6100.0))
        for _ in range(3):
            await asyncio.sleep(0)
        return queued, paused, reloaded
    queued, paused, reloaded = asyncio.run(go())
    assert queued == [10] and paused == (False, False, []) and reloaded == (False, []), (queued, paused, reloaded)
    assert len(made) == 3 and not pool.jobs and ind.states[a.id].version == 2, (made, pool.jobs)


# ---- the chain of put-off first looks ends with the market; no job after the workers' shutdown starts new ones --------

def _longs(n):
    return [_alert(ia.clean_params({**LINREG, "len": 300 + 10 * i}, {"ind": "price"}, "1m", "close", "above"), i=i + 1)
            for i in range(n)]


def test_the_market_stopping_ends_the_loops_chain_of_put_off_first_looks(tmp_path, monkeypatch):
    """Six long alerts' first looks, five put off to the loop's passes, when the market stops: none is made after
    that, while its tasks are cancelled and gathered or later (each sent a replay to a worker being shut down)."""
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    made = _recording(monkeypatch)
    w = _watch()
    m, _ = build_market(tmp_path, monkeypatch)
    ind = m.ind_alerts
    ind.watches[KEY] = w
    alerts = _longs(6)
    m.alerts.alerts = {a.id: a for a in alerts}

    async def go():
        m.tasks = [asyncio.create_task(asyncio.sleep(3600)) for _ in range(3)]   # its loops and feeds
        tk = _trade(ind, w, 10_000, 6100.0)
        for a in alerts:
            ind.check(a, tk)
        queued = len(ind._later)
        await m.stop()
        for _ in range(5):
            await asyncio.sleep(0)
        return queued
    queued = asyncio.run(go())
    assert queued == 5 and len(made) == 1 and not ind._later, (queued, made)


def test_no_job_after_the_workers_shutdown_starts_new_ones(monkeypatch):
    """After workers.shutdown() (the server stopping) a job is cancelled, as one queued at the shutdown is: no new
    pool, no new worker process. A server started again in this process (a test's) opens them again."""
    monkeypatch.setenv("CHART_WORKERS", "1")
    workers.shutdown(wait=True)
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(workers.run("study", os.getpid))
        assert not workers._pools and not mp.active_children()
        workers.start()
        assert asyncio.run(workers.run("study", os.getpid)) != os.getpid()
    finally:
        workers.shutdown(wait=True)


@pytest.mark.parametrize("reexec", [True, False], ids=["restart-in-place", "plain-stop"])
def test_a_server_stopping_with_first_looks_put_off_leaves_no_worker_process_nor_pending_task(
        tmp_path, monkeypatch, caplog, reexec):
    """The real server with real workers (CHART_WORKERS=2) stops while twenty long alerts' first looks are queued on
    the loop, and its shared http client closes three keep-alive connections, a loop pass each (anyio's), after the
    workers' shutdown. No first look is made once the market stops, and nothing starts workers again: at a restart's
    execv no child process is left (they stayed alive beside the new server for good), nor after a plain stop, and no
    task is destroyed pending."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", "none")
    monkeypatch.setenv("CHART_QUOTES_FEED", "none")   # (Yahoo's, asking the network, can let a stop's cancel go by)
    monkeypatch.setenv("CHART_WORKERS", "2")
    workers.shutdown(wait=True)   # workers of this test's own
    from chartsrv.app import app
    close = httpx.AsyncClient.aclose

    async def aclose(self):
        for _ in range(3):
            await asyncio.sleep(0)
        await close(self)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", aclose)
    at_exec, events = [], []
    if reexec:
        monkeypatch.setattr(app.state, "reexec", True, raising=False)
        monkeypatch.setattr(os, "execv", lambda *a: at_exec.append((mp.active_children(), resource_tracker._resource_tracker._pid)))
    init = ia._State.__init__

    def look(self, *a, **k):
        events.append("look")
        init(self, *a, **k)
    monkeypatch.setattr(ia._State, "__init__", look)
    w = _watch()
    alerts = _longs(20)
    caplog.set_level("ERROR", logger="asyncio")
    with TestClient(app) as c:
        m = app.state.market
        stop = m.stop

        async def stopping():
            events.append("stop")
            await stop()
        m.stop = stopping

        def trade():   # on the server's own thread
            m.ind_alerts.watches[KEY] = w
            m.alerts.alerts = {a.id: a for a in alerts}
            tk = _trade(m.ind_alerts, w, 10_000, 6100.0)
            for a in alerts:
                m.ind_alerts.check(a, tk)
            return len(m.ind_alerts._later)
        queued = c.portal.call(trade)
    t = time.monotonic()
    while mp.active_children() and time.monotonic() - t < 20:
        time.sleep(0.1)
    gc.collect()
    assert queued == 19 and "stop" in events and "look" not in events[events.index("stop"):], (queued, events)
    assert not mp.active_children() and not workers._pools
    assert at_exec == ([([], None)] if reexec else []), at_exec
    assert "Task was destroyed but it is pending" not in caplog.text, caplog.text
