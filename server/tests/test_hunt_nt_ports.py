"""Regressions from the NinjaTrader-port bug hunt: AMT alerts sent once, AMT chart requests that race a settings change,
a newer request or amt_off, the HTF EMA on 2D, 3D, weekly and monthly bars."""
import asyncio
import shutil
from datetime import datetime

import pytest

from amt_parity import synth
from chartsrv import amt, amt_hub, htfema
from chartsrv.amt_runner import AmtRunner
from chartsrv.bars import make_series, parse_spec
from chartsrv.market import Client
from chartsrv.models import Bar
from chartsrv.sessions import SessionClock
from chartsrv.tpo_service import session_end
from htfema_parity import js_values
from test_bug_hunt import _warm
from test_review_fixes import build_market


def test_amt_notice_from_the_preview_is_not_sent_again_when_the_bar_commits():
    """C# Commit sends the committed frame's notices through the same `sent` set the previews filled, then clears it:
    a notice the forming bar already raised goes out once."""
    bars = synth(15, 7, datetime(2026, 10, 2, 12, 0))
    r = AmtRunner("ES", 0.25, 5, amt.Settings(), calendar="equity")
    for b in bars[:-240]:
        r.add_minute(b)
    r.commit_ready()
    r.preview()
    sent = []
    for b in bars[-240:]:   # live: each minute seen half formed, then whole, as AmtHub.step() does once a second
        for part in (Bar(t=b.t, o=b.o, h=(b.o + b.h) / 2, l=(b.o + b.l) / 2, c=b.o, v=b.v // 2), b):
            r.add_minute(part)
            notices = r.commit_ready()
            _, pn = r.preview()
            sent += [n.Key for n in r.fresh(notices + pn)]
    assert sent == ["Inventory"]    # Inventory Trap Active, from the 08:30 bar's preview; not again at 08:35


def _sent(c):
    out = []
    while not c.outbox.empty():
        out.append(c.outbox.get_nowait()["type"])
    return out


def _hold_loads(monkeypatch) -> asyncio.Event:
    """AMT loads wait for the event that comes back. A load that is quick (a warm worker, a thread) can be done before
    the awaiting coroutine looks: it then goes on without yielding, so one sleep(0) would see the chart already on the
    runner, before the change it is meant to load through."""
    gate, real = asyncio.Event(), amt_hub.workers.run

    async def held(kind, fn, *args):
        await gate.wait()
        return await real(kind, fn, *args)
    monkeypatch.setattr(amt_hub.workers, "run", held)
    return gate


@pytest.mark.parametrize("change", ["settings", "gap fill"])
def test_amt_chart_loading_through_a_runner_rebuild_asks_again(tmp_path, monkeypatch, change):
    """New shared settings (or a gap fill) drop the runners while one still loads for a chart: the chart is told to ask
    again, as the charts already on them are (its client reloads the shared settings first), not put on the dropped
    one that nothing feeds any more. Asked again, it gets the new runner."""
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        c = Client()
        c.subs[1] = object()
        gate = _hold_loads(monkeypatch)
        sub = asyncio.create_task(m.amt.subscribe(c, 1, "ES", 5))
        await asyncio.sleep(0)                                # its runner is loading
        if change == "settings":
            m.amt.set_overrides({"mvp_tf_mode": "Session"})   # Apply to all charts, or another device
        else:
            m.amt.reload("ES")
        gate.set()
        await sub
        assert _sent(c) == ["amt_reset"] and 1 not in c.amt_entries
        await m.amt.subscribe(c, 1, "ES", 5)
        e = c.amt_entries[1]
        assert m.amt.entries[("ES", 5)] is e and (c, 1) in e.subs
        if change == "settings":
            assert e.runner.S.mvp_tf_mode == "Session"
    asyncio.run(go())


def test_amt_own_settings_chart_loading_through_apply_to_all_asks_again(tmp_path, monkeypatch):
    """A chart on its own settings sends them on top of the shared ones its client had (app.js amtEff). Apply to all
    charts while one still loads: it is told to ask again, as the one already showing them, rather than run its own
    settings on the old shared ones (Weekly) while the hub's say Session."""
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        a, b = Client(), Client()
        a.subs[1] = b.subs[1] = object()
        await m.amt.subscribe(a, 1, "ES", 5, {"tbl_size": "Large"})
        _sent(a)
        gate = _hold_loads(monkeypatch)
        sub = asyncio.create_task(m.amt.subscribe(b, 1, "ES", 5, {"tbl_size": "Small"}))
        await asyncio.sleep(0)
        m.amt.set_overrides({"mvp_tf_mode": "Session"})
        gate.set()
        await sub
        assert _sent(a) == ["amt_reset"] and _sent(b) == ["amt_reset"] and 1 not in b.amt_entries
        await m.amt.subscribe(b, 1, "ES", 5, {"mvp_tf_mode": "Session", "tbl_size": "Small"})   # asked again
        assert b.amt_entries[1].runner.S.mvp_tf_mode == "Session" and b.amt_entries[1].runner.S.tbl_size == "Small"
    asyncio.run(go())


def test_amt_failed_load_leaves_the_runner_that_replaced_it(tmp_path, monkeypatch):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        gate, real = asyncio.Event(), amt_hub.workers.run

        async def first_fails(kind, fn, *args):
            monkeypatch.setattr(amt_hub.workers, "run", real)   # the next load works
            await gate.wait()
            raise OSError("disk went away")
        monkeypatch.setattr(amt_hub.workers, "run", first_fails)
        old = asyncio.create_task(m.amt.entry("ES", 5))
        await asyncio.sleep(0)
        m.amt.reload("ES")                                    # a gap fill while it loads
        new = asyncio.create_task(m.amt.entry("ES", 5))
        await asyncio.sleep(0)
        gate.set()
        with pytest.raises(RuntimeError):
            await old
        e = await new
        assert m.amt.entries.get(("ES", 5)) is e and not e.error
    asyncio.run(go())


@pytest.mark.parametrize("change", ["gap fill", "settings"])
def test_amt_chart_waiting_on_a_dropped_runner_that_fails_asks_again(tmp_path, monkeypatch, change):
    """A gap fill (or new shared settings) drops the runner a chart waits for, then that one fails to load: the chart is
    told to ask again and gets a healthy one, rather than the dropped one's error. A failed load nothing dropped still
    shows its error (asking again would only fail again)."""
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        gate, real = asyncio.Event(), amt_hub.workers.run

        async def first_fails(kind, fn, *args):
            monkeypatch.setattr(amt_hub.workers, "run", real)   # the next load works
            await gate.wait()
            raise OSError("disk went away")
        monkeypatch.setattr(amt_hub.workers, "run", first_fails)
        own = {"tbl_size": "Small"} if change == "settings" else None
        c = Client()
        c.subs[1] = object()
        sub = asyncio.create_task(m.amt.subscribe(c, 1, "ES", 5, own))
        await asyncio.sleep(0)
        if change == "settings":
            m.amt.set_overrides({"mvp_tf_mode": "Session"})   # nothing takes the old own settings' place
        else:
            m.amt.reload("ES")
            await m.amt.entry("ES", 5)                        # the alert runner, or another chart, in its place
        gate.set()
        await sub
        assert _sent(c) == ["amt_reset"]
        await m.amt.subscribe(c, 1, "ES", 5, own and {**m.amt.overrides, **own})
        assert not c.amt_entries[1].error and c.amt_entries[1].runner.S.mvp_tf_mode == ("Session" if own else "Weekly")
        assert _sent(c) == ["amt"]

        async def fails(kind, fn, *args):
            raise OSError("disk went away")
        monkeypatch.setattr(amt_hub.workers, "run", fails)
        c.subs[2] = object()
        with pytest.raises(RuntimeError):
            await m.amt.subscribe(c, 2, "ES", 3)
        assert _sent(c) == []
    asyncio.run(go())


def test_amt_newest_request_for_a_chart_wins(tmp_path, monkeypatch):
    """Monthly picked, then Weekly again before Monthly has loaded: the chart runs Weekly (the shared runner, loaded)."""
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        shared = await m.amt.entry("ES", 5)
        c = Client()
        c.subs[1] = object()
        monthly = asyncio.create_task(m.amt.subscribe(c, 1, "ES", 5, {"mvp_tf_mode": "Monthly"}))
        await asyncio.sleep(0)
        await m.amt.subscribe(c, 1, "ES", 5, None)
        await monthly
        assert c.amt_entries[1] is shared and (c, 1) in shared.subs
        assert not any((c, 1) in e.subs for k, e in m.amt.entries.items() if len(k) == 3)
    asyncio.run(go())


def test_amt_off_while_the_runner_loads_stays_off(tmp_path, monkeypatch):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        c = Client()
        c.subs[1] = object()
        sub = asyncio.create_task(m.amt.subscribe(c, 1, "ES", 5))
        await asyncio.sleep(0)
        m.amt.unsubscribe(c, 1)                               # op amt_off: the Toolkit turned off
        await sub
        assert 1 not in c.amt_entries and not m.amt.entries[("ES", 5)].subs
    asyncio.run(go())


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
@pytest.mark.parametrize("spec", ["2D", "3D", "1W", "1M"])
def test_htf_ema_on_calendar_bars_closes_with_their_last_session(spec):
    """A 2D bar closes at its second session's 16:00 close, a month at its last trading day's: Exact shows the EMA of
    the last higher-timeframe bar that closed before then (one closing with it runs after the bar, as on a tie)."""
    s = htfema.settings({"Mode": "ExactHTF"})
    m1 = synth(75, 3, datetime(2026, 10, 16, 12, 7))
    now = m1[-1].t + 60_000
    sp = parse_spec(spec)
    series = make_series(sp, 0.25, 2, 10_000)
    for m in m1:
        series.on_bar(m)
    bars = [{"t": b.t, "c": b.c} for b in series.bars]
    chart_minutes = sp.size // 60 if sp.cal not in ("W", "M") else None     # as /api/htfema
    d = htfema.compute(s, m1, chart_minutes, htfema.GetScaledLength(s, *htfema.chart_period_spec(sp)), bars[0]["t"], now)
    got = js_values(bars, d, sp.size * 1000, cal=True)
    clock, starts = SessionClock(), [b["t"] for b in bars] + [now]
    want = []
    for i in range(len(bars)):
        last = max(m.t for m in m1 if starts[i] <= m.t < starts[i + 1])
        close = session_end(clock.locate(last)) if i + 1 < len(bars) else now
        want.append([v for c, v in d["htf"] if c < close][-1])
    assert got == want
