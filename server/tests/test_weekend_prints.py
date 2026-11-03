"""Saturday bars of one price on the daily charts: NinjaTrader replays its last trade when it (re)connects, and the
add-on stamps it with the PC's clock. A print stamped from a Friday's 17:00 CT to the Sunday's 16:00 CT, when no
future trades, is left out by the feeds, and those stored before are taken out once."""
import asyncio
from datetime import datetime

import pytest

from chartsrv import sessions
from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.feeds.sdk import FeedPlugin
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT
from chartsrv.store import Store, drop_weekend_prints

from test_review_fixes import build_market

pytestmark = pytest.mark.weekend   # the rule on, whatever day the tests run


def ct(*a) -> int:
    return int(datetime(*a, tzinfo=CT).timestamp() * 1000)


def test_the_weekend_runs_from_fridays_close_hour_to_sundays():
    # Oct 30 2026 is a Friday; DST ends Sunday Nov 1 at 02:00
    assert not sessions.weekend(ct(2026, 10, 30, 15, 59))    # Friday's last minute
    assert not sessions.weekend(ct(2026, 10, 30, 16, 30))    # the 16:00 hour: snap()'s (a late print)
    assert sessions.weekend(ct(2026, 10, 30, 17, 0))
    assert sessions.weekend(ct(2026, 10, 31, 12, 0))         # Saturday
    assert sessions.weekend(ct(2026, 11, 1, 15, 59))         # Sunday, after the DST change
    assert not sessions.weekend(ct(2026, 11, 1, 16, 0))      # Sunday's pre-open hour: snap()'s
    assert not sessions.weekend(ct(2026, 11, 1, 17, 0)) and not sessions.weekend(ct(2026, 11, 2, 9, 0))
    spans = sessions.weekends(ct(2026, 10, 21, 0, 0), ct(2026, 11, 3, 0, 0))
    assert spans == [(ct(2026, 10, 23, 17, 0), ct(2026, 10, 25, 16, 0)), (ct(2026, 10, 30, 17, 0), ct(2026, 11, 1, 16, 0))]


def test_ninjatraders_replay_of_its_last_trade_over_the_weekend_is_not_stored(tmp_path, monkeypatch):
    """The add-on's T line for the last trade, sent when NinjaTrader connects on a Saturday, makes no bar (before: a
    Saturday daily bar at Friday's close); a trade in the week is kept as ever."""
    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        feed = NinjaTraderFeed("127.0.0.1", 0, "")
        sat, mon = ct(2026, 10, 31, 10, 15), ct(2026, 11, 2, 9, 30)
        feed._line(m, f"T|ES|{sat}|6905.25|3|0\n".encode())
        feed._line(m, f"T|ES|{mon}|6910.50|2|1\n".encode())
        m.flush()
        rows = m.store.db.execute("SELECT t FROM bars_1m WHERE symbol='ES' AND t>=?", (ct(2026, 10, 30, 0, 0),)).fetchall()
        ticks = m.store.db.execute("SELECT ts FROM ticks WHERE symbol='ES' AND ts>=?", (ct(2026, 10, 30, 0, 0),)).fetchall()
        return [r[0] for r in rows], [r[0] for r in ticks], feed.stats.get("dropped", 0), mon
    bars, ticks, dropped, mon = asyncio.run(go())
    assert bars == [mon // 60_000 * 60_000] and ticks == [mon] and dropped == 1


def test_a_plugins_trade_stamped_over_the_weekend_is_dropped(tmp_path, monkeypatch):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        p = FeedPlugin.__new__(FeedPlugin)
        p.name, p.stats = "lines", {"ticks": 0, "dropped": 0, "last_msg_ms": None}
        m.trade_owner = {"ES": "lines"}
        return p.trade(m, "ES", ct(2026, 10, 31, 10, 15), 6905.25, 1), p.stats["dropped"]
    monkeypatch.setattr("time.time", lambda: ct(2026, 10, 31, 10, 16) / 1000)
    ok, dropped = asyncio.run(go())
    assert ok is False and dropped == 1


def test_weekend_prints_stored_before_are_taken_out_once(tmp_path):
    """Two weekends' replays (Oct 24 and Oct 31 2026) in the stored data: removed with their 1-minute bars, the
    Friday and Monday ones around them kept; the second start removes nothing more (a marker is kept)."""
    st = Store(tmp_path / "chart.db")
    keep = [ct(2026, 10, 23, 15, 59), ct(2026, 10, 26, 8, 30), ct(2026, 10, 30, 16, 10), ct(2026, 11, 2, 9, 0)]
    drop = [ct(2026, 10, 24, 11, 0), ct(2026, 10, 31, 9, 45), ct(2026, 10, 30, 17, 5)]
    rows = sorted(keep + drop)
    st.write([Tick("ES", t, 6900.0, 1, 0) for t in rows], [("ES", Bar(t // 60_000 * 60_000, 6900, 6900, 6900, 6900, 1, 0, 0, 1)) for t in rows])
    assert list(drop_weekend_prints(st.path))[-1] == {"ES": 2 * len(drop)}
    assert [r[0] for r in st.db.execute("SELECT ts FROM ticks ORDER BY ts")] == sorted(keep)
    assert [r[0] for r in st.db.execute("SELECT t FROM bars_1m ORDER BY t")] == sorted(t // 60_000 * 60_000 for t in keep)
    assert list(drop_weekend_prints(st.path))[-1] == {}
    st.close()
