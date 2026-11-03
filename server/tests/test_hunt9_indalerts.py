"""Round 9 of the bug hunt, the round-7 indicator alert fix: where the rest of a first look goes is decided by how
long that rest takes, not by the share of bars left. Common settings whose first look costs two to four times
INLINE_S (Ichimoku at its defaults, Regression 50, HMA 100, Donchian 100, WMA 100) had more than half their bars left
after it and waited seconds in the study worker behind a long replay; a few more passes of the loop finish them. Long
settings (Regression 1000, Ichimoku 500), whose first bars cost little while their windows fill, still go there."""
import asyncio
import os
import time
from types import SimpleNamespace

from chartsrv import ind_alerts as ia
from chartsrv import workers
from test_hunt6_indalerts import _recording
from test_redesign_ind_alert_state import ICHIMOKU, LINREG, _alert, _engine, _Pool, _same, _trade, _watch

PRICE = {"ind": "price"}
LONG = ({**LINREG, "len": 1000}, ICHIMOKU)   # the docs' long example: seconds over 3000 bars
MEDIUM = [{"ind": "ichimoku", "line": "spanA"}, {"ind": "linreg", "line": "middle", "len": 50},
          {"ind": "hma", "line": "v", "len": 100}, {"ind": "donch", "line": "upper", "len": 100},
          {"ind": "wma", "line": "v", "len": 100}]
# CPU a bar of each series once its window (n bars) has filled, about this PC's: whole first looks over 3000 bars of
# 40-75 ms for the medium ones, a second or two for the long ones. Before that a bar costs its share of the window
# (Regression's sums, the averages' weights, Donchian's highs and lows), and Ichimoku works nothing out until its
# longest window has filled. Others (the price) 1 us
STEADY = {("ichimoku", 52): 20e-6, ("linreg", 50): 16e-6, ("hma", 100): 25e-6, ("donch", 100): 18e-6,
          ("wma", 100): 14e-6, ("linreg", 1000): 450e-6, ("ichimoku", 500): 330e-6}


def _window(d):
    return d.get("len") or max(d.get("conv", 0), d.get("base", 0), d.get("span", 0)) or 1


def _cpu(monkeypatch):
    """ind_alerts' thread CPU clock made one that moves only as bars are stepped, STEADY's cost a bar: first looks
    stop where they would on this PC, the same on every run. Its reading, [seconds]."""
    clock = [0.0]
    step = ia.Line.step

    def costly(self, bar, commit=True):
        if commit:
            self.k = k = getattr(self, "k", 0) + 1
            kind, n = self.d.get("ind"), _window(self.d)
            full = STEADY.get((kind, n), 0)
            clock[0] += max(1e-6, full * (k >= n if kind == "ichimoku" else min(k, n) / n))
        return step(self, bar, commit)
    monkeypatch.setattr(ia.Line, "step", costly)
    monkeypatch.setattr(ia, "time", SimpleNamespace(thread_time=lambda: clock[0], perf_counter=time.perf_counter))
    return clock


def _alerts():
    """1: Regression(1000) crossing Ichimoku(500), 2: the price crossing Ichimoku(500) Span A, 10-14: the price
    crossing the medium ones, all intrabar."""
    long_ = [ia.clean_params(*LONG, "1m", "bar", "cross"), ia.clean_params(PRICE, ICHIMOKU, "1m", "bar", "cross")]
    return ([_alert(p, i=1 + j) for j, p in enumerate(long_)]
            + [_alert(ia.clean_params(PRICE, d, "1m", "bar", "cross"), i=10 + j) for j, d in enumerate(MEDIUM)])


# ---- k-ia9-1: a first look's rest stays on the loop when it is a few passes long, else it goes to a worker ---------

def test_a_first_look_whose_rest_takes_a_few_passes_is_finished_on_the_loop_not_behind_a_long_replay(tmp_path, monkeypatch):
    """ia9-1's case: the long alerts first, then intrabar ones at common settings costing two to four times INLINE_S.
    Each of those stops at INLINE_S with more than half its bars left, but their rests take a few passes of the
    loop: they are finished there, ready a few hundred ms of loop time after the trade, not sent to the study
    worker behind the long replays (seconds without being checked). Both long ones go there, Ichimoku(500) too,
    though its first 500 bars cost next to nothing. No pass holds the loop much over INLINE_S, and each state is
    the one made in one go."""
    pool = _Pool()   # the long replays never come back here
    monkeypatch.setattr(workers, "run", pool.run)
    made = _recording(monkeypatch)
    clock = _cpu(monkeypatch)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    alerts = _alerts()
    ind.m.alerts.alerts = {a.id: a for a in alerts}
    held, ready, look = [], {}, ind._look_later

    def timed():   # each pass's CPU, on that clock, and when each alert's state was ready
        c = clock[0]
        look()
        held.append(clock[0] - c)
        ready.update({i: clock[0] for i in ind.states if i not in ready})
    ind._look_later = timed

    async def go():
        tk = _trade(ind, w, 10_000, 6100.0)
        c = clock[0]
        assert [ind.check(a, tk) for a in alerts] == [None] * len(alerts)   # the first long one's; the others put off
        held.append(clock[0] - c)
        for _ in range(40):
            await asyncio.sleep(0)
    asyncio.run(go())
    mediums = [a.id for a in alerts[2:]]
    assert pool.jobs == ["study", "study"] and sorted(ind.replaying) == [1, 2], (pool.jobs, list(ind.replaying))
    assert sorted(ind.states) == mediums and not ind._later, (sorted(ind.states), ind._later)
    assert [b for _, b in made] == [ia.INLINE_S] * len(alerts), made   # one first look each, the whole time
    assert max(ready.values()) < 0.5, ready   # (CPU on the loop since the trade)
    assert max(held) < ia.INLINE_S * 1.1, held
    for a in alerts[2:]:
        assert _same(ind.states[a.id], ia._State(a.params, w)) and ind.states[a.id].catch_up, a.params


def test_with_real_workers_medium_first_looks_are_ready_well_under_a_second_while_long_replays_wait(tmp_path, monkeypatch):
    """The same with real worker processes and this PC's own CPU (CHART_WORKERS=2: a study pool of one, as on a 4-core
    PC by default), a trade every 50 ms. The long ones go to the worker, and only what takes longer than LOOP_REST_S
    to finish: the medium alerts are finished on the loop, ready well under a second after the first trade, before
    the long replays are back. (On a PC busy just then, the long replay on the same core, one of them can really take
    longer: it goes to the worker, by the rule, and the test allows two.) No pass of the loop, nor the trade that made
    the first look, held it much over INLINE_S (it stops on a 16-bar boundary: Ichimoku(500)'s 16 bars take 5-8 ms)."""
    monkeypatch.setenv("CHART_WORKERS", "2")
    workers.shutdown(wait=True)   # workers of this test's own
    workers.start()
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    alerts = _alerts()
    ind.m.alerts.alerts = {a.id: a for a in alerts}
    sent, held, replay, look = {}, [], ind._replay, ind._look_later

    async def to_worker(aid, st):   # with how long its rest looked
        sent[aid] = getattr(st, "rate", 0.0) * len(st.todo)
        return await replay(aid, st)

    def timed():
        c = time.thread_time()
        look()
        held.append(time.thread_time() - c)
    ind._replay, ind._look_later = to_worker, timed

    async def go():
        await workers.run("study", os.getpid)   # the worker up, as on a server running a while
        t0, ready, k = time.perf_counter(), {}, 0
        while len(ready) < len(alerts):
            assert time.perf_counter() - t0 < 60, ready
            tk = _trade(ind, w, 10_000 + k, w.series.bars[-1].c)
            c = time.thread_time()
            for a in alerts:
                ind.check(a, tk)
            held.append(time.thread_time() - c)
            for _ in range(25):   # 50 ms to the next trade
                ready.update({i: time.perf_counter() - t0 for i in ind.states if i not in ready})
                await asyncio.sleep(0.002)
            k += 1
        return ready
    try:
        ready = asyncio.run(go())
    finally:
        workers.shutdown(wait=True)
    kept = {i: round(s, 2) for i, s in ready.items() if i >= 10 and i not in sent}
    assert {1, 2} <= set(sent) and min(sent.values()) > ia.LOOP_REST_S, (sent, ready)
    assert len(kept) >= 3 and max(kept.values()) < min(1.0, ready[1], ready[2]), (kept, ready[1], ready[2])
    assert max(held) < ia.INLINE_S * 2, max(held)
