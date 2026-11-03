"""Fixes from round 4 of the bug hunt, alerts and the database: gap and AMT alerts while the database can't be written
(a full disk, another program's lock), and the watchdog's bar-close check on a bar the feed stopped filling before
the close."""
import asyncio
import logging
import sqlite3
from datetime import datetime
from types import SimpleNamespace

from chartsrv import gaps, sessions
from chartsrv import gaps_service as gs
from chartsrv import ind_alerts as ia
from chartsrv import market as market_mod
from chartsrv.amt_runner import epoch_ms
from chartsrv.bars import make_series, parse_spec
from chartsrv.feeds import ninjatrader as nt
from chartsrv.market import Client
from chartsrv.models import Tick

from test_ind_alerts import _ct
from test_review_fixes import build_market, history


def _drain(c):
    out = []
    while not c.outbox.empty():
        out.append(c.outbox.get_nowait())
    return out


def _lock(path):
    """Another connection holding the database's write lock: it can't be written for now."""
    other = sqlite3.connect(path)
    other.execute("BEGIN IMMEDIATE")
    return other


# --- gap and AMT alerts while the database can't be written -----------------------------------------------------
def test_gap_alert_while_the_database_cannot_be_written_is_sent_and_its_trade_charted(tmp_path, monkeypatch, caplog):
    """The RTH open makes a gap while the database can't be written: the alert goes out all the same (with no event
    id), and the trade that made the gap reaches the open chart and the last price. The error left Market.on_tick
    before the charts had the trade, and NinjaTrader's line was logged as not stored though it was buffered."""
    monkeypatch.setattr(gs, "LIVE_BAR_AGE_MS", 10**15)   # the test's bars are not from today
    monkeypatch.setattr(nt, "LIVE_WINDOW_MS", 10**15)    # the add-on's trade counts as live
    caplog.set_level(logging.WARNING)

    async def go():
        m, notifier = build_market(tmp_path, monkeypatch)
        w = m.gaps.watches["ES"] = gs.GapWatch({"on": True, "new": True, "fill": True, **gaps.DEFAULTS},
                                               m.instruments["ES"].rth)
        w.ready.set()
        m.on_tick(Tick("ES", epoch_ms(datetime(2026, 10, 15, 14, 59, 30)), 7000.0, 1, 1), live=False)   # Thursday's RTH close
        c = Client()
        m.clients.add(c)
        await history(m, c, 1, "ES", "1m")
        _drain(c)
        m.store.db.execute("PRAGMA busy_timeout=50")   # the real wait is 5 s
        other = _lock(tmp_path / "chart.db")
        try:
            opening = epoch_ms(datetime(2026, 10, 16, 8, 30, 5))   # Friday's RTH open, 10.25 higher
            nt.NinjaTraderFeed("127.0.0.1", 0, "")._line(m, f"T|ES|{opening}|7010.25|3|1".encode())
        finally:
            other.rollback()
            other.close()
        await asyncio.sleep(0.05)
        return m, notifier, _drain(c)

    m, notifier, msgs = asyncio.run(go())
    gap = "[ES] GAP UP 10.25 pts | Open: 7010.25 | RTH close: 7000.00"
    assert [(x["event"]["message"], x["event"]["id"]) for x in msgs if x["type"] == "alert"] == [(gap, None)]
    assert notifier.sent == [("ES alert", gap)]
    assert m.last_tick["ES"].price == 7010.25 and m.by_symbol["ES"][0].series.bars[-1].c == 7010.25
    assert not [r for r in caplog.records if "not stored" in r.getMessage()]


def test_amt_alert_while_the_database_cannot_be_written_is_sent(tmp_path, monkeypatch):
    """The same for an AMT notice, which the run has already marked sent: it goes out, and the error never leaves the
    live trade or the once-a-second step."""
    async def go():
        m, notifier = build_market(tmp_path, monkeypatch)
        c = Client()
        m.clients.add(c)
        m.store.db.execute("PRAGMA busy_timeout=50")
        other = _lock(tmp_path / "chart.db")
        try:
            m.amt._notify("ES", [SimpleNamespace(Text="Price crossed RES1 7010.25")], 7010.25)
        finally:
            other.rollback()
            other.close()
        await asyncio.sleep(0.05)
        return notifier, [x["event"] for x in _drain(c) if x["type"] == "alert"]

    notifier, alerts = asyncio.run(go())
    msg = "[ES] Price crossed RES1 7010.25 | close 7010.25"
    assert [(e["message"], e["id"]) for e in alerts] == [(msg, None)] and notifier.sent == [("ES alert", msg)]


# --- the watchdog's bar-close check on a bar the feed stopped filling --------------------------------------------
def test_last_close_is_when_the_market_last_shut():
    sessions.set_calendar({"days": [{"date": "2026-11-27", "close": "12:15"}, {"date": "2026-12-25", "closed": True},
                                    {"date": "2026-01-19", "halt": {"default": "12:00", "energy": "13:30"}}]})
    t = lambda mo, d, h, mi=0: int(datetime(2026, mo, d, h, mi, tzinfo=sessions.CT).timestamp() * 1000)   # noqa: E731
    assert sessions.last_close(t(10, 27, 16, 30), "equity") == t(10, 27, 16)        # the daily break
    assert sessions.last_close(t(10, 31, 12), "equity") == t(10, 30, 16)            # a weekend: Friday's close
    assert sessions.last_close(t(11, 1, 16, 59), "equity") == t(10, 30, 16)
    assert sessions.last_close(t(11, 27, 13), "equity") == t(11, 27, 12, 15)        # an early close
    assert sessions.last_close(t(11, 28, 9), "equity") == t(11, 27, 12, 15)         # and the weekend after it
    assert sessions.last_close(t(12, 26, 9), "equity") == t(12, 24, 16)             # a closed Friday
    assert sessions.last_close(t(1, 19, 14), "equity") == t(1, 19, 12)              # a halt, per group
    assert sessions.last_close(t(1, 19, 14), "energy") == t(1, 19, 13, 30)


def test_watchdog_leaves_a_bar_the_feed_stopped_filling_before_the_close(tmp_path, monkeypatch):
    """NinjaTrader closed (or its connection lost) at 15:05 CT on Friday with the server left running: once the market
    had shut, the watchdog checked ES's daily bar as the feed left it, at 7500, and sent a crossing that the day's real
    close (brought later by the gap fill) need not make, switching the Only once alert off. NQ, which ticked to the
    close, is checked then; ES's bar waits for the next trade, as before the watchdog checked."""
    m, _ = build_market(tmp_path, monkeypatch)
    sma3 = {"ind": "sma", "line": "v", "len": 3}
    ids = {}
    for sym in ("ES", "NQ"):
        inst = m.instruments[sym]
        w = m.ind_alerts.watches[(sym, "1D", False)] = ia._Watch(make_series(
            parse_spec("1D"), inst.tick_size, inst.decimals, 3000, inst.rth, studies=False, group=inst.calendar))
        w.ready.set()
        w.version = 1
        ids[sym] = m.store.add_alert(sym, "ind", ia.clean_params({"ind": "price"}, sma3, "1D", "close", "above"), False, "")
    fired = lambda: [e["message"] for e in reversed(m.store.list_events(1000))]   # noqa: E731

    async def go():
        m.alerts_changed()
        for d, px in [(6, 7500.0), (7, 7480.0), (8, 7460.0), (9, 7440.0), (10, 7500.0)]:   # Friday the 10th closes above
            for sym in ("ES", "NQ"):
                stop = 133 if sym == "ES" and d == 10 else 137   # ES's last trade 15:05, NQ's 15:45 and 15:59
                for k in range(stop):
                    m.on_tick(Tick(sym, _ct(d - 1, 17, 5) + k * 600_000, px, 1, 1))
                if stop == 137:
                    m.on_tick(Tick(sym, _ct(d, 15, 59), px, 1, 1))
        assert fired() == []
        for now in (_ct(10, 16, 0) + 20_000, _ct(10, 16, 1) + 30_000, _ct(11, 12), _ct(12, 16, 59)):
            monkeypatch.setattr(market_mod, "now_ms", lambda now=now: now)
            await m._watchdog_pass()
        assert fired() == ["NQ 1D: price 7500.00 crossed above SMA(3) 7466.67 (last 7500.00)"]
        assert m.alerts.alerts[ids["ES"]].enabled
        m.on_tick(Tick("ES", _ct(12, 17) + 1000, 7500.0, 1, 1))   # Sunday's open, no gap fill came: the bar as stored
        assert fired()[1:] == ["ES 1D: price 7500.00 crossed above SMA(3) 7466.67 (last 7500.00)"]
    asyncio.run(go())
