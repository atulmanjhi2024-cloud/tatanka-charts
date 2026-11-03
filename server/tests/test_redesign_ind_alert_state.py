"""An indicator alert's first look (after a restart, a gap fill, turning it on) steps its series through the last 3000
bars. With long settings that is seconds of work: it no longer holds the event loop, the rest runs in a worker."""
import asyncio
import logging
import math
import pickle
import time

from chartsrv import ind_alerts as ia
from chartsrv import ta, workers
from chartsrv.alerts import Alert
from chartsrv.bars import make_series, parse_spec
from chartsrv.models import Tick
from test_ind_alerts import _ct
from test_review_fixes import build_market

KEY = ("ES", "1m", False)
START = _ct(13, 0)   # Monday 00:00 CT: 3000 minutes on is Wednesday 02:00, the market open
LINREG = {"ind": "linreg", "line": "middle", "len": 300, "width": 2}   # about half a second over 3000 bars
ICHIMOKU = {"ind": "ichimoku", "line": "spanA", "conv": 500, "base": 500, "span": 500, "disp": 500}


def _falling(k):   # falls faster and faster: price under its regression line at the end
    return 7000 - (k / 100) ** 2


def _watch(price=_falling, version=1):
    """A 1m watch with its 3000 bars loaded, the last one forming."""
    w = ia._Watch(make_series(parse_spec("1m"), 0.25, 2, 3000, studies=False))
    for k in range(3001):
        w.series.on_tick(Tick("ES", START + k * 60_000, price(k), 1 + k % 7, 1))
    w.ready.set()
    w.version = version
    return w


def _engine(tmp_path, monkeypatch, w):
    m, _ = build_market(tmp_path, monkeypatch)
    ind = ia.IndicatorAlerts(m)
    ind.watches[KEY] = w
    return ind


def _alert(params, i=1):
    return Alert(id=i, symbol="ES", kind="ind", params=params, enabled=True, repeat=True, note="", created=0, last_fired=None)


def _trade(ind, w, dt, price):
    """A trade `dt` ms after the forming bar opened, as the feed sends it."""
    tk = Tick("ES", w.series.bars[-1].t + dt, price, 1, 1)
    ind.on_tick(tk)
    return tk


class _Pool:
    """workers.run stood in: a replay waits until the test lets it go, then runs on a pickled copy, as in a worker."""

    def __init__(self, fail=False):
        self.jobs, self.done, self.fail, self.go = [], 0, fail, asyncio.Event()

    async def run(self, kind, fn, *args):
        self.jobs.append(kind)
        await self.go.wait()
        self.done += 1
        if self.fail:
            raise RuntimeError("the worker stopped")
        return fn(*pickle.loads(pickle.dumps(args)))


async def _until(cond, timeout=120):
    t = time.monotonic()
    while not cond():
        assert time.monotonic() - t < timeout
        await asyncio.sleep(0.01)


def _same(st, ref):
    return repr((st.pa, st.pb, st.done_t, st.fired_t)) == repr((ref.pa, ref.pb, ref.done_t, ref.fired_t))


def test_first_look_of_a_long_alert_replays_its_history_off_the_event_loop(tmp_path, monkeypatch):
    """Regression(1000) against Ichimoku(500): two seconds of replay over 3000 bars. It runs in a worker process; the
    event loop goes on with ticks meanwhile, and the state that comes back is the one a replay in place gives."""
    monkeypatch.setenv("CHART_WORKERS", "1")
    workers.shutdown()   # workers of this test's own (an earlier test's may hold another holiday calendar)
    workers.start()
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    p = ia.clean_params({**LINREG, "len": 1000}, ICHIMOKU, "1m", "close", "cross")
    a = _alert(p)
    gaps = []

    async def beat():
        while True:
            t = time.perf_counter()
            await asyncio.sleep(0.005)
            gaps.append(time.perf_counter() - t)

    async def go():
        hb = asyncio.create_task(beat())
        await asyncio.sleep(0.05)
        tk = _trade(ind, w, 10_000, 6100.0)
        t = time.perf_counter()
        assert ind.check(a, tk) is None   # the first look never fires
        held = time.perf_counter() - t
        await _until(lambda: a.id in ind.states)
        hb.cancel()
        return held
    try:
        held = asyncio.run(go())
    finally:
        workers.shutdown()
    assert held < 0.3 and max(gaps) < 0.5, (held, max(gaps))
    assert _same(ind.states[a.id], ia._State(p, w))


def test_several_long_alerts_first_looks_on_one_trade_hold_the_loop_once(tmp_path, monkeypatch):
    """AlertEngine checks each alert in turn on the same trade: their first looks don't add up on it. The trade makes
    the first; the others are made right after it on the event loop's next passes, one a pass, each as short."""
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    alerts = [_alert(ia.clean_params({"ind": "price"}, {**LINREG, "len": 300 + 50 * i}, "1m", "close", "above"), i=i + 1)
              for i in range(5)]
    ind.m.alerts.alerts = {a.id: a for a in alerts}

    async def go():
        tk = _trade(ind, w, 10_000, 6100.0)
        t = time.thread_time()
        assert [ind.check(a, tk) for a in alerts] == [None] * 5
        held, made = [time.thread_time() - t], [len(ind.replaying)]
        for _ in range(6):   # a pass of the loop each
            t = time.thread_time()
            await asyncio.sleep(0)
            held.append(time.thread_time() - t)
            made.append(len(ind.replaying))
        return max(held), made
    held, made = asyncio.run(go())
    assert held < 0.2 and made == [1, 2, 3, 4, 5, 5, 5] and len(pool.jobs) == 5 and not ind.states, (held, made)


def test_a_short_alert_after_long_ones_is_not_queued_behind_their_replays(tmp_path, monkeypatch):
    """Long alerts checked before it on the same trade used up its time on the loop: a short alert has its first look
    right after, in place, not sent whole to the worker to wait for every long replay queued there. A crossing
    soon after fires while they still replay."""
    pool = _Pool()   # the long ones' replays never come back here
    monkeypatch.setattr(workers, "run", pool.run)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    longs = [_alert(ia.clean_params({"ind": "price"}, {**LINREG, "len": 300 + 50 * i}, "1m", "close", "above"), i=i + 1)
             for i in range(3)]
    ema = _alert(ia.clean_params({"ind": "price"}, {"ind": "ema", "line": "v", "len": 21}, "1m", "bar", "cross"), i=99)
    ind.m.alerts.alerts = {a.id: a for a in longs + [ema]}

    async def go():
        out = []
        for k, px in enumerate([6100.0] * 4 + [6120.0]):   # under the EMA, then a trade far above it
            tk = _trade(ind, w, 10_000 + k * 1000, px)
            out.append([ind.check(a, tk) for a in longs + [ema]])
            await asyncio.sleep(0)
        return out
    out = asyncio.run(go())
    assert pool.jobs == ["study"] * 3 and ema.id in ind.states, pool.jobs
    assert out[:-1] == [[None] * 4] * 4 and out[-1][:3] == [None] * 3
    assert out[-1][3] and "price 6120.00 crossed above EMA(21)" in out[-1][3], out[-1]


def test_a_bar_that_closes_while_the_history_replays_is_checked_once_it_is_ready(tmp_path, monkeypatch):
    """No crossing is lost to the replay: a bar that closed meanwhile is checked on the first trade after it, as it
    would have been with the state built at once."""
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    a = _alert(ia.clean_params({"ind": "price"}, LINREG, "1m", "close", "above"))

    async def go():
        out = [ind.check(a, _trade(ind, w, 10_000, 6100.0))]   # the first look: the history goes to a worker
        out.append(ind.check(a, _trade(ind, w, 50_000, 7000.0)))   # the forming minute far above the line...
        out.append(ind.check(a, _trade(ind, w, 60_000, 7000.0)))   # ...and closed there, while it replays
        await asyncio.sleep(0)
        assert out == [None] * 3 and pool.jobs == ["study"] and a.id not in ind.states
        pool.go.set()
        await _until(lambda: a.id in ind.states)
        return ind.check(a, _trade(ind, w, 10_000, 7000.0))
    msg = asyncio.run(go())
    assert msg and "price 7000.00 crossed above Regression(300,2) Middle" in msg


def test_an_intrabar_crossing_whose_bar_closes_while_the_history_replays_fires_once_it_is_ready(tmp_path, monkeypatch):
    """An intrabar alert too: a crossing no trade checked, as the history replayed, and whose bar closed meanwhile is
    found on the closed bar on the first trade after it is ready, as with the state built at once. Once a bar."""
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    a = _alert(ia.clean_params({"ind": "price"}, LINREG, "1m", "bar", "above"))

    async def go():
        out = [ind.check(a, _trade(ind, w, 10_000, 6100.0))]   # the first look: the history goes to a worker
        out.append(ind.check(a, _trade(ind, w, 50_000, 7000.0)))   # the forming minute far above the line...
        out.append(ind.check(a, _trade(ind, w, 60_000, 7000.0)))   # ...and closed there, while it replays
        await asyncio.sleep(0)
        assert out == [None] * 3 and pool.jobs == ["study"] and a.id not in ind.states
        pool.go.set()
        await _until(lambda: a.id in ind.states)
        return [ind.check(a, _trade(ind, w, dt, 7000.0)) for dt in (10_000, 20_000)]
    msg, again = asyncio.run(go())
    assert msg and "price 7000.00 crossed above Regression(300,2) Middle" in msg and again is None, (msg, again)


def test_an_alert_turned_off_or_deleted_while_its_history_replays_is_dropped(tmp_path, monkeypatch):
    """Paused or deleted meanwhile, the replay that comes back is not kept: turned on again, it starts afresh. An
    alert on short settings still has its first look at once, in place."""
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    heavy = _alert(ia.clean_params({"ind": "price"}, LINREG, "1m", "close", "above"), i=1)
    ema = _alert(ia.clean_params({"ind": "price"}, {"ind": "ema", "line": "v", "len": 21}, "1m", "close", "above"), i=2)
    ind.m.alerts.alerts = {1: heavy, 2: ema}   # the EMA one keeps the watch wanted

    async def go():
        tk = _trade(ind, w, 10_000, 6100.0)
        assert ind.check(ema, tk) is None and ind.check(heavy, tk) is None   # the heavy one right after
        await _until(lambda: pool.jobs)
        assert 2 in ind.states and 1 not in ind.states and pool.jobs == ["study"]
        heavy.enabled = False   # paused
        ind.ensure()
        pool.go.set()
        await _until(lambda: pool.done == 1)
        assert 1 not in ind.states and 1 not in ind.replaying
        heavy.enabled = True    # restarted: a replay of its own
        ind.ensure()
        pool.go.clear()
        assert ind.check(heavy, _trade(ind, w, 20_000, 6100.0)) is None
        await asyncio.sleep(0)
        assert len(pool.jobs) == 2
        del ind.m.alerts.alerts[1]   # deleted
        ind.ensure()
        pool.go.set()
        await _until(lambda: pool.done == 2)
        assert 1 not in ind.states and 1 not in ind.replaying and 2 in ind.states
    asyncio.run(go())


def test_a_gap_fill_while_the_history_replays_replays_the_new_bars(tmp_path, monkeypatch):
    """The watch loaded again meanwhile (a gap fill changed its history): the replay of the old bars is dropped, the
    alert's state is the new bars'."""
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    p = ia.clean_params({"ind": "price"}, LINREG, "1m", "close", "above")
    a = _alert(p)
    w2 = _watch(lambda k: _falling(k) + (5 if k > 2500 else 0), version=2)

    async def go():
        assert ind.check(a, _trade(ind, w, 10_000, 6100.0)) is None
        ind.watches[KEY] = w2
        assert ind.check(a, _trade(ind, w2, 20_000, 6105.0)) is None
        await asyncio.sleep(0)
        assert len(pool.jobs) == 2
        pool.go.set()
        await _until(lambda: pool.done == 2)
    asyncio.run(go())
    st, ref = ind.states[a.id], ia._State(p, w2)
    assert st.version == 2 and _same(st, ref) and not ind.replaying


def test_a_replay_that_fails_is_logged_once_not_on_every_trade(tmp_path, monkeypatch, caplog):
    pool = _Pool(fail=True)
    monkeypatch.setattr(workers, "run", pool.run)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    a = _alert(ia.clean_params({"ind": "price"}, LINREG, "1m", "close", "above"))

    async def go():
        assert ind.check(a, _trade(ind, w, 10_000, 6100.0)) is None
        await asyncio.sleep(0)
        assert pool.jobs == ["study"]
        pool.go.set()
        await _until(lambda: pool.done == 1)
        return [ind.check(a, _trade(ind, w, 20_000 + k * 1000, 6100.0)) for k in range(5)]
    with caplog.at_level(logging.ERROR, logger="chartsrv.ind_alerts"):
        assert asyncio.run(go()) == [None] * 5
    assert pool.jobs == ["study"] and len([r for r in caplog.records if "history failed" in r.getMessage()]) == 1


def test_a_state_replayed_in_a_worker_is_the_one_replayed_in_place():
    """Every indicator, and the series saved before there were more: part of the bars stepped in place, the rest on a
    pickled copy (as a worker gets it), gives the same values, bit for bit, as all of them in place."""
    w = _watch(lambda k: 7000 + 40 * math.sin(k / 60) + 3 * math.sin(k / 7))
    lines = [{"ind": k, "line": spec["lines"][-1]} for k, spec in ta.IND.items()]
    lines += [{"ind": "ema", "len": 9}, {"ind": "rsi_avg", "len": 14}, {"ind": "macd_diff"}]
    split = 0
    for d in lines:
        for on in ("close", "bar"):
            p = ia.clean_params(d, {"ind": "price"}, "1m", on, "cross")
            ref = ia._State(p, w, w.series.bars[-1].t)   # (an intrabar one fired in the forming bar before a restart)
            part = ia._State(p, w, w.series.bars[-1].t, budget=0.002)
            split += bool(part.todo)
            copy = pickle.loads(pickle.dumps(part))
            assert copy.replay() and not copy.todo
            assert _same(copy, ref), d
            cur = ia._Now(w.series.bars[-1], 7010.0)
            assert repr(copy.a.step(cur, False)) == repr(ref.a.step(cur, False)), d
    assert split   # the long ones did go on in the copy
    nothing = ia._State(ia.clean_params(LINREG, {"value": 0}, "1m", "close", "gt"), w, budget=0)
    assert len(nothing.todo) == 2999 and nothing.done_t is None   # no time left: all of it for the worker
