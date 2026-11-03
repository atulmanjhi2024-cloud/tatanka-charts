"""Round 12 of the bug hunt, the plugins' hole pass and rolls: a plugin that asks no history (Databento live with Fill
gaps off) leaves its instruments' holes for a feed that asks, instead of remembering them as answered; and a plugin's
gap window opened after a recorded roll, with nothing stored since, starts at the next session like one open at the
roll, so the new contract's history is not merged into the old contract's last session."""
import asyncio
import sys
import time
import types
from datetime import datetime, timedelta

from chartsrv import sessions
from chartsrv.feeds.plugins.databento_live import DatabentoLive
from chartsrv.feeds.sdk import FeedPlugin
from chartsrv.gapfill import ROLLED
from chartsrv.models import Bar, Tick
from test_review_fixes import build_market


# ---- k-fr11-1: Databento live with Fill gaps off: its holes are not taken for asked ----------------------------------------
def stored_with_a_hole(tmp_path, monkeypatch, now: int):
    """ES's minutes of the last 3 hours stored up to now, but for 20 minutes 2 hours ago: a hole."""
    m, _ = build_market(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    top = now // 60_000 * 60_000
    hole = range(top - 120 * 60_000, top - 100 * 60_000, 60_000)
    mins = [t for t in range(top - 180 * 60_000, top, 60_000) if t not in hole]
    m.store.write([Tick("ES", t + 10_000, 7000.0, 1, 1) for t in mins] + [Tick("ES", now - 1000, 7000.0, 1, 1)],
                  [("ES", Bar(t=t, o=7000, h=7000, l=7000, c=7000, v=1, n=1)) for t in mins])
    return m, hole


def test_databento_with_fill_gaps_off_leaves_its_holes_for_a_feed_that_asks(tmp_path, monkeypatch, in_session):
    """ES moved from NinjaTrader to Databento live with Fill gaps off (its default), a 20-minute hole in ES's stored
    data. Its connect 'asked' the hole (history() returns at once with Fill gaps off), and the empty answer was
    remembered as Databento's: the hole was never asked again, of Databento with Fill gaps turned on or of NinjaTrader
    with ES moved back, and /api/health showed it 'done, none, from Databento (live)'."""
    now = in_session
    m, _ = stored_with_a_hole(tmp_path, monkeypatch, now)
    holes = m.gapfill.holes("ES", now)
    assert len(holes) == 1
    m.trade_owner = {"ES": "databento_live"}
    asyncio.run(DatabentoLive("databento_live", {"api_key": "db-KEY123", "roll": "v", "fill": "no"}, ["ES"]).fill_gaps(m))
    assert "ES" not in m.gapfill._asked() and "ES" not in m.gapfill.status and not m.gapfill.windows
    assert m.gapfill.holes("ES", now) == holes   # Fill gaps turned on: asked at its next connect
    m.trade_owner = {}   # ES back to NinjaTrader: the add-on's connect asks it
    m.gapfill.requests(now, live=True)
    assert m.gapfill.queue["ES"] == holes


def test_databento_with_fill_gaps_on_fills_the_holes(tmp_path, monkeypatch, in_session):
    """With Fill gaps on, the same hole is asked of Databento's 1-minute bars, and filled."""
    now = in_session
    m, hole = stored_with_a_hole(tmp_path, monkeypatch, now)
    fake, asked = types.ModuleType("databento"), []

    class OHLCVMsg(types.SimpleNamespace):
        pass

    class Historical:
        def __init__(self, key):
            self.timeseries = self

        def get_range(self, **kw):
            asked.append(kw["symbols"])
            return [OHLCVMsg(ts_event=t * 1_000_000, open=7001 * 10**9, high=7001 * 10**9, low=7001 * 10**9,
                             close=7001 * 10**9, volume=4) for t in range(hole[0] - 5 * 60_000, hole[-1] + 60_000, 60_000)]
    fake.Historical, fake.OHLCVMsg = Historical, OHLCVMsg
    monkeypatch.setitem(sys.modules, "databento", fake)
    m.trade_owner = {"ES": "databento_live"}
    asyncio.run(DatabentoLive("databento_live", {"api_key": "db-KEY123", "roll": "v", "fill": "yes"}, ["ES"]).fill_gaps(m))
    assert asked == [["ES.v.0"]] and m.gapfill.status["ES"]["note"] == "from Databento (live)"
    got = m.store.db.execute("SELECT COUNT(*) FROM bars_1m WHERE symbol='ES' AND t>=? AND t<=? AND c=7001",
                             (hole[0], hole[-1])).fetchone()[0]
    assert got == len(hole) and m.gapfill.holes("ES", now) == []


# ---- k-carry-1: a plugin's window opened after a recorded roll, nothing stored since: the new contract's sessions only -----
def on_a_saturday(monkeypatch) -> tuple[int, int]:
    """A clock on the latest Saturday at 10:00 CT after a Friday whose crude oil session ran to its 16:00 CT close,
    running on from there. Returns (that Friday at 14:00 CT, Saturday 10:00 CT)."""
    real = time.time
    day = datetime.fromtimestamp(real(), sessions.CT).date()
    while True:
        sat = datetime(day.year, day.month, day.day, 10, 0, tzinfo=sessions.CT)
        fri = int((sat - timedelta(hours=20)).timestamp() * 1000)
        if day.weekday() == 5 and sat.timestamp() <= real() and all(sessions.is_open(fri + k * 60_000, "energy") for k in range(120)):
            break
        day -= timedelta(days=1)
    off = sat.timestamp() - real()
    monkeypatch.setattr(time, "time", lambda: real() + off)
    return fri, int(sat.timestamp() * 1000)


def test_a_plugins_gap_after_a_roll_recorded_at_an_earlier_connect_fills_the_new_contracts_sessions_only(tmp_path, monkeypatch):
    """CL comes from a futures plugin (IBKR's): IB Gateway went down Friday at 14:00 CT with CL 12-26's last trade
    stored. It reconnected Friday evening on CL 01-27: the roll was recorded, and that window kept the new contract's
    sessions only (none yet). Saturday the server restarts (or IB Gateway's nightly restart): the window opened from
    Friday 14:00 again, the plugin named the same contract (no roll now), and its history, CL 01-27's bars, filled
    the old contract's Friday session from 14:00 to the close. Now that stretch starts at the next session open, as
    it did when the roll came."""
    fri, _ = on_a_saturday(monkeypatch)
    m, _ = build_market(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    m.store.write([Tick("CL", fri, 65.0, 1, 1)], [("CL", Bar(t=fri, o=65.0, h=65.0, l=65.0, c=65.0, v=1, n=1))])
    m.contracts = {"CL": "CL 01-27"}   # recorded Friday evening
    m.store.set_setting(ROLLED, {"CL": fri + 4 * 3_600_000})
    m.trade_owner = {"CL": "ibkr"}
    asked, sent = [], []

    async def send(title, text, *a, **k):
        sent.append(title)
    m.notifier.send = send

    class Ibkr(FeedPlugin):
        TITLE = "IBKR"
        KIND = "futures"

        async def run(self, market):
            self.contract(market, "CL", "CL 01-27")
            self.connected()
            await asyncio.sleep(3600)

        async def history(self, symbol, start, end):   # the contract it is on now: CL 01-27's minutes, 1.00 below
            asked.append((symbol, start, end))
            for t in range(start // 60_000 * 60_000 + 60_000, end - 60_000, 60_000):
                yield ("bar", t, 64.0, 64.0, 64.0, 64.0, 2)

    async def go():
        task = asyncio.create_task(Ibkr("ibkr", {}, ["CL"]).serve(m))
        for _ in range(100):
            if asked and "CL" not in m.gapfill.windows:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.wait({task}, timeout=2)
    asyncio.run(go())
    old_session_ends = sessions.next_session_start(sessions.session_start(fri)) - 1
    new_in_old = m.store.db.execute("SELECT COUNT(*) FROM bars_1m WHERE symbol='CL' AND t>? AND t<? AND c=64.0",
                                    (fri, old_session_ends)).fetchone()[0]
    assert not sent and [a[:2] for a in asked] == [("CL", old_session_ends)] and new_in_old == 0
    assert m.gapfill.status["CL"]["state"] == "done"
