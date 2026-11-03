"""NinjaTrader's 1-minute history has a bar after the close: the session's whole volume (a million contracts on NQ)
at the settlement price, stamped in the 16:00 break. A gap fill stored it as a minute, and every chart and volume
profile holding it got a day's volume in one minute (the profiles squeezed to a sliver, their POC moved to it). A gap
fill now leaves out what history gives for a minute the market is shut, and the ones stored are taken out once."""
import asyncio
from datetime import datetime

import pytest

from chartsrv.models import Bar, Tick
from chartsrv.store import drop_settlement_bars
from chartsrv.sessions import CT

from test_review_fixes import build_market

pytestmark = pytest.mark.weekend   # the closed-market rule and the clean-up on (conftest's any_day turns them off)


def ms(h, mi, s=0, day=23):
    return int(datetime(2026, 10, day, h, mi, s, tzinfo=CT).timestamp() * 1000)   # Fri Oct 23 (25: the Sunday)


def gap(m, monkeypatch):
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    m.store.write([Tick("ES", ms(15, 30, 10), 6800.0, 2, 1)], [("ES", Bar(t=ms(15, 30), o=6800, h=6801, l=6799, c=6800, v=10, n=4))])
    m.gapfill.requests(ms(17, 10, day=25))   # Sunday 17:10: the gap runs over Friday's close
    m.gapfill.live("ES", ms(17, 10, day=25))


def test_a_gap_fill_leaves_out_ninjatraders_settlement_bar(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    gap(m, monkeypatch)
    for t, o, h, lo, c, v in [(ms(15, 58), 6801, 6802, 6800, 6801.5, 900), (ms(15, 59), 6801.5, 6803, 6801, 6802, 1200),
                              (ms(16, 0), 6802.25, 6802.25, 6802.25, 6802.25, 2_377_879),   # the settlement bar
                              (ms(17, 0, day=25), 6803, 6804, 6802, 6803.5, 400)]:
        m.gapfill.minute("ES", t, o, h, lo, c, v)
    m.gapfill.done("ES", "minutes", 4, "no tick history")
    bars = {r[0]: r[1] for r in m.store.db.execute("SELECT t, v FROM bars_1m WHERE symbol='ES'")}
    assert bars == {ms(15, 30): 10, ms(15, 58): 900, ms(15, 59): 1200, ms(17, 0, day=25): 400}
    assert m.gapfill.health()["ES"]["stored"] == 3


def test_a_gap_fills_trades_while_the_market_is_shut_are_left_out(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    gap(m, monkeypatch)
    for t, px, size in [(ms(15, 59, 30), 6802.0, 3), (ms(16, 0, 5), 6802.25, 2_377_879), (ms(17, 0, 30, day=25), 6803.0, 1)]:
        m.gapfill.tick("ES", t, px, size)
    m.gapfill.done("ES", "ticks", 3, "ok")
    ticks = [tuple(r) for r in m.store.db.execute("SELECT ts, size FROM ticks WHERE symbol='ES' ORDER BY ts")]
    assert ticks == [(ms(15, 30, 10), 2), (ms(15, 59, 30), 3), (ms(17, 0, 30, day=25), 1)]
    assert not m.store.db.execute("SELECT 1 FROM bars_1m WHERE symbol='ES' AND t=?", (ms(16, 0),)).fetchone()


def test_the_settlement_bars_stored_are_taken_out_once(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    changed = []
    monkeypatch.setattr(m, "history_changed", changed.append)
    one = lambda t, px, v: Bar(t=t, o=px, h=px, l=px, c=px, v=v)   # noqa: E731
    m.store.write([Tick("NQ", ms(16, 0, 5), 28534.0, 1_023_028, 0), Tick("NQ", ms(15, 59, 59), 28530.0, 4, 1)], [
        ("NQ", one(ms(16, 0), 28534.0, 1_023_028)),                                   # the settlement bar: out
        ("GC", one(ms(16, 0, day=14), 4437.3, 124_670)),                               # GC's, another day: out
        ("NQ", one(ms(15, 59), 28530.0, 60_000)),                                      # while it trades: kept
        ("NQ", Bar(t=ms(16, 1), o=28530, h=28531, l=28529, c=28530, v=80_000)),       # not one price: kept
        ("NQ", one(ms(16, 2), 28530.0, 5)),                                           # a late print, small: kept
    ])
    asyncio.run(m._drop_settlement_bars())
    left = {(r[0], r[1]) for r in m.store.db.execute("SELECT symbol, t FROM bars_1m")}
    assert left == {("NQ", ms(15, 59)), ("NQ", ms(16, 1)), ("NQ", ms(16, 2))}
    assert [tuple(r) for r in m.store.db.execute("SELECT ts, size FROM ticks WHERE symbol='NQ'")] == [(ms(15, 59, 59), 4)]
    assert sorted(changed) == ["GC", "NQ"]
    m.store.write([], [("NQ", one(ms(16, 0), 28534.0, 1_023_028))])   # once only: not looked for again
    asyncio.run(m._drop_settlement_bars())
    assert m.store.db.execute("SELECT 1 FROM bars_1m WHERE symbol='NQ' AND t=?", (ms(16, 0),)).fetchone()
    assert drop_settlement_bars(m.store.path) == {"NQ": 1}
