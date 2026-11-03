"""Futures' daily settlements as their exchanges set them, and the watchlist's change from the latest one until the
next one."""
import asyncio
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace

from chartsrv import sessions, settlement
from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.models import Tick
from chartsrv.settlement import PUBLISH_GRACE_MS, Settlements
from test_review_fixes import build_market

CT = sessions.CT


def ms(d, h, mi, s=0):
    return int(datetime(2026, 10, d, h, mi, s, tzinfo=CT).timestamp() * 1000)


def market(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settlements.json").write_text(json.dumps({"markets": {
        "ES": {"tz": "America/Chicago", "window": ["14:59:30", "15:00:00"], "calendar": "equity"},
        "MES": {"tz": "America/Chicago", "window": ["14:59:30", "15:00:00"], "calendar": "equity", "same_as": "ES"},
        "ZN": {"tz": "America/Chicago", "window": ["13:59:30", "14:00:00"]},
        "FDAX": {"tz": "Europe/Berlin", "window": ["17:29:00", "17:30:00"]}}}))
    m.settlements = Settlements(m, cfg)
    return m


def test_a_charted_futures_settlement_from_its_own_trades(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    m.store.write([Tick("ES", ms(20, 14, 59, 10), 7750.00, 5, 1),     # before the window
                   Tick("ES", ms(20, 14, 59, 40), 7755.00, 10, 1),    # in it: VWAP of these, to the tick
                   Tick("ES", ms(20, 14, 59, 50), 7756.00, 30, -1),
                   Tick("ES", ms(20, 15, 0, 0), 7790.00, 99, 1),      # at its end: not in it
                   Tick("ES", ms(21, 14, 59, 45), 7810.25, 4, 1)], [])
    ref = m.settlements.reference("ES", ms(21, 10, 0))
    assert ref == {"t": ms(20, 15, 0), "price": 7755.75, "src": "trades"}   # (7755*10 + 7756*30) / 40 = 7755.75
    asyncio.run(_quote(m, "ES", 7809.75, 7704.25, ms(21, 10, 0)))
    q = m.quotes["ES1!"]
    assert (q["prev"], q["chg"], q["settle_t"]) == (7755.75, 7809.75 - 7755.75, ms(20, 15, 0))   # not NinjaTrader's 7704.25
    # the next settlement is set at 15:00 and counts from the 17:00 open, as TradingView: until then the day's change
    # stays against the one before
    assert m.settlements.reference("ES", ms(21, 14, 59, 50))["price"] == 7755.75
    assert m.settlements.reference("ES", ms(21, 15, 1, 30))["price"] == 7755.75
    assert m.settlements.saved["ES"]["t"] == ms(21, 15, 0)   # set already
    assert m.settlements.reference("ES", ms(21, 17, 0, 30)) == {"t": ms(21, 15, 0), "price": 7810.25, "src": "trades"}
    assert m.settlements.reference("MES", ms(21, 17, 0, 30))["price"] == 7810.25   # the micro settles to the E-mini
    fresh = Settlements(m, tmp_path / "cfg")   # a restart between the settlement and the open: the one before is known
    assert fresh.reference("ES", ms(21, 16, 0))["price"] == 7755.75


async def _quote(m, sym, last, prev, now):
    import chartsrv.market as mk
    real = mk.now_ms
    mk.now_ms = lambda: now
    try:
        m.on_quote(sym, last, prev)
    finally:
        mk.now_ms = real


def test_holidays(tmp_path, monkeypatch):
    sessions.set_calendar({"days": [{"date": "2026-11-26", "name": "Thanksgiving", "halt": "12:00"},
                                    {"date": "2026-11-27", "name": "Day after", "close": "12:15"}]})
    m = market(tmp_path, monkeypatch)
    nov = lambda d, h, mi, s=0: int(datetime(2026, 11, d, h, mi, s, tzinfo=CT).timestamp() * 1000)   # noqa: E731
    wins = m.settlements.windows("ES", nov(30, 10, 0))
    assert wins[0] == (nov(27, 12, 14, 30), nov(27, 12, 15))   # the early close: settles there
    assert wins[1] == (nov(25, 14, 59, 30), nov(25, 15, 0))    # Thanksgiving's halt: none that day
    assert [w for w in m.settlements.windows("FDAX", nov(30, 10, 0))][0][1] == \
        int(datetime(2026, 11, 27, 17, 30, tzinfo=sessions.ZoneInfo("Europe/Berlin")).timestamp() * 1000)


def test_a_quoted_futures_settlement_from_ninjatrader(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    st = m.settlements
    st.on_feed("ZN", 112.5, ms(21, 9, 0))   # on connect, in the morning: yesterday's
    assert st.reference("ZN", ms(21, 9, 0)) == {"t": ms(20, 14, 0), "price": 112.5, "src": "feed"}
    st.on_feed("ZN", 112.5, ms(21, 14, 5))   # the same again soon after today's window: not today's
    assert st.reference("ZN", ms(21, 14, 5) + 61_000)["t"] == ms(20, 14, 0)
    st.on_feed("ZN", 112.625, ms(21, 14, 6))   # published: today's, counting from the 17:00 open
    assert st.reference("ZN", ms(21, 14, 7))["price"] == 112.5
    assert st.reference("ZN", ms(21, 17, 1)) == {"t": ms(21, 14, 0), "price": 112.625, "src": "feed"}
    (tmp_path / "b").mkdir()
    st2 = market(tmp_path / "b", monkeypatch).settlements
    st2.on_feed("ZN", 112.5, ms(21, 14, 0) + PUBLISH_GRACE_MS - 1000)   # nothing known, right after the window: yesterday's
    assert st2.reference("ZN", ms(21, 14, 40))["t"] == ms(20, 14, 0)
    st2.on_feed("CL", 61.25, ms(21, 9, 0))   # no window known for it: the feed's latest, as it comes
    assert st2.reference("CL", ms(21, 9, 0))["price"] == 61.25


def test_ninjatraders_s_lines(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    feed = NinjaTraderFeed("127.0.0.1", 0, "")
    feed._line(m, b"S|ZN|112.5\n")
    feed._line(m, b"S|ZN|nan\n")
    assert m.settlements.saved["ZN"]["price"] == 112.5 and feed.stats["bad_lines"] == 1


def test_the_exchanges_times_on_early_close_days(tmp_path, monkeypatch):
    """CME's own settlement times on its early-close days: 12:00 CT for the equity index futures, energy and metals as
    usual the day after Thanksgiving and earlier on Christmas Eve (config/settlements.json's groups)."""
    sessions.set_calendar({"days": [
        {"date": "2026-11-27", "name": "Day after Thanksgiving", "close": {"default": "12:15", "energy": "13:45", "metals": "13:45"}},
        {"date": "2026-12-24", "name": "Christmas Eve", "close": {"default": "12:15", "energy": "12:45", "metals": "12:45"}},
        {"date": "2026-12-25", "name": "Christmas", "closed": True},
        {"date": "2028-07-03", "name": "Day before Independence Day", "close": {"default": "12:15", "energy": "13:45"}},
        {"date": "2028-07-04", "name": "Independence Day", "halt": {"default": "12:00", "energy": "13:30"}}]})
    m, _ = build_market(tmp_path, monkeypatch)
    m.settlements = Settlements(m, Path(__file__).resolve().parents[2] / "config")
    et = sessions.ZoneInfo("America/New_York")
    at = lambda mo, d, h, mi, s=0, tz=CT: int(datetime(2026, mo, d, h, mi, s, tzinfo=tz).timestamp() * 1000)   # noqa: E731
    st = m.settlements
    assert st.windows("ES", at(11, 30, 10, 0))[0] == (at(11, 27, 11, 59, 30), at(11, 27, 12, 0))
    assert st.windows("MES", at(11, 30, 10, 0))[0] == (at(11, 27, 11, 59, 30), at(11, 27, 12, 0))
    assert st.windows("CL", at(11, 30, 10, 0))[0] == (at(11, 27, 14, 28, tz=et), at(11, 27, 14, 30, tz=et))
    assert st.windows("CL", at(12, 28, 10, 0))[0] == (at(12, 24, 13, 28, tz=et), at(12, 24, 13, 30, tz=et))
    assert st.windows("GC", at(12, 28, 10, 0))[0] == (at(12, 24, 12, 29, tz=et), at(12, 24, 12, 30, tz=et))
    assert st.windows("ZN", at(12, 28, 10, 0))[0] == (at(12, 24, 11, 59, 30), at(12, 24, 12, 0))
    assert st.windows("FDAX", at(12, 28, 10, 0))[0][1] == at(12, 25, 17, 30, tz=sessions.ZoneInfo("Europe/Berlin"))   # not on CME's calendar
    jul5 = int(datetime(2028, 7, 5, 10, tzinfo=CT).timestamp() * 1000)   # the day before Independence Day: its default
    assert st.windows("ES", jul5)[0][1] == int(datetime(2028, 7, 3, 12, 0, tzinfo=CT).timestamp() * 1000)
    assert st.windows("CL", jul5)[0][1] == int(datetime(2028, 7, 3, 14, 30, tzinfo=et).timestamp() * 1000)
    sessions.set_calendar({"days": [{"date": "2026-12-24", "name": "Christmas Eve", "close": "12:15"},
                                    {"date": "2026-12-25", "name": "Christmas", "closed": True}]})
    st.cache.clear()
    assert st.windows("CL", at(12, 28, 10, 0))[0][1] == at(12, 24, 12, 15)   # a close before its time: settles there
    for root, w in st.markets.items():   # every market a micro settles to is listed
        assert not w.same_as or w.same_as in st.markets, root


def test_every_catalog_future_has_its_settlement_window(tmp_path, monkeypatch):
    """The futures Connections > Instruments offers (config/futures.json) all have their exchange's window, so the
    settlement NinjaTrader sends counts from the next open, not the moment it comes: BTC's, set at 15:00 CT while it
    trades on to 16:00, leaves the day's change until 17:00 as ES's does; a micro takes its parent's."""
    from chartsrv.config import ROOT, futures_catalog
    m, _ = build_market(tmp_path, monkeypatch)
    st = m.settlements = Settlements(m, ROOT / "config")
    assert [r["symbol"] for r in futures_catalog(ROOT / "config") if r["symbol"] not in st.markets] == []
    st.on_feed("BTC", 100000.0, ms(21, 8, 0))     # on connect, in the morning: yesterday's
    st.on_feed("BTC", 101000.0, ms(21, 15, 40))   # today's, published after its 14:59-15:00 CT window
    assert st.reference("BTC", ms(21, 15, 45))["price"] == 100000.0   # still trading: the day's change
    assert st.reference("BTC", ms(21, 17, 1)) == {"t": ms(21, 15, 0), "price": 101000.0, "src": "feed"}
    assert st.reference("MBT", ms(21, 17, 1))["price"] == 101000.0   # the micro settles to it

def test_an_out_of_date_settlement_gives_way_to_the_feeds_close(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    m.store.write([Tick("ES", ms(19, 14, 59, 40), 7700.00, 10, 1)], [])
    assert m.settlements.reference("ES", ms(20, 10, 0))["price"] == 7700.00
    # no trades in the next two windows (the feed was down): from Wednesday's open Monday's no longer counts
    assert m.settlements.reference("ES", ms(21, 15, 1, 30))["price"] == 7700.00
    assert m.settlements.reference("ES", ms(21, 17, 1)) is None
    asyncio.run(_quote(m, "ES", 7809.75, 7704.25, ms(21, 17, 1)))
    assert m.quotes["ES1!"]["prev"] == 7704.25 and "settle_t" not in m.quotes["ES1!"]


def test_fridays_settlement_counts_from_sundays_open(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    m.store.write([Tick("ES", ms(22, 14, 59, 40), 7700.00, 1, 1), Tick("ES", ms(23, 14, 59, 40), 7720.00, 1, 1)], [])
    assert m.settlements.reference("ES", ms(24, 12, 0))["price"] == 7700.00    # Saturday: Friday's change still shows
    assert m.settlements.reference("ES", ms(25, 16, 59))["price"] == 7700.00
    assert m.settlements.reference("ES", ms(25, 17, 1))["price"] == 7720.00    # Sunday's open
    fdax = Settlements(m, tmp_path / "cfg")   # its own open: 01:10 Frankfurt
    fdax.markets = {"FDAX": __import__("dataclasses").replace(fdax.markets["FDAX"], opens=datetime(2026, 1, 1, 1, 10).time())}
    berlin = sessions.ZoneInfo("Europe/Berlin")
    assert fdax.opens_after("FDAX", int(datetime(2026, 10, 23, 17, 30, tzinfo=berlin).timestamp() * 1000)) == \
        int(datetime(2026, 10, 26, 1, 10, tzinfo=berlin).timestamp() * 1000)



def test_livestocks_settlement_counts_from_its_own_open(tmp_path, monkeypatch):
    """r4-wrong-data-1: CME's livestock (LE, HE, GF) trades 08:30-13:05 CT only: its settlement counts from its next
    08:30 open (Friday's from Monday's), not from the 17:00 CT Globex open (config/settlements.json's opens)."""
    m, _ = build_market(tmp_path, monkeypatch)
    st = m.settlements = Settlements(m, Path(__file__).resolve().parents[2] / "config")
    st.on_feed("LE", 230.0, ms(22, 14, 0))   # Thursday's, published after its 12:59:30-13:00 CT window
    st.on_feed("LE", 231.5, ms(23, 14, 0))   # Friday's
    assert [st.reference("LE", t)["price"] for t in (ms(23, 13, 30), ms(25, 17, 30), ms(26, 8, 29))] == [230.0] * 3
    assert st.reference("LE", ms(26, 8, 31))["price"] == 231.5   # Monday's open: Friday's counts
    st.on_feed("LE", 232.0, ms(26, 14, 0))   # Monday's: the day's change until Tuesday 08:30
    assert st.reference("LE", ms(26, 17, 30))["price"] == 231.5 and st.reference("LE", ms(27, 8, 31))["price"] == 232.0

async def _contract(m, sym, contract, now):
    import chartsrv.market as mk
    real = mk.now_ms
    mk.now_ms = lambda: now
    try:
        m.on_contract(sym, contract)
    finally:
        mk.now_ms = real


def test_a_roll_drops_the_old_contracts_settlement(tmp_path, monkeypatch):
    """The add-on rolls in the 16:00-17:00 CT break, after the day's settlement was worked out from the old contract's
    trades: from the roll the change is from the new contract's own (NinjaTrader's S line), never the old one's."""
    m = market(tmp_path, monkeypatch)
    st = m.settlements
    m.store.write([Tick("ES", ms(19, 14, 59, 40), 6990.00, 10, 1), Tick("ES", ms(20, 14, 59, 40), 7000.00, 10, 1)], [])
    assert st.reference("ES", ms(20, 15, 1, 30))["price"] == 6990.00   # Tuesday's set from the 12-26 trades
    m.contracts["ES"] = "ES 12-26"
    asyncio.run(_contract(m, "ES", "ES 03-27", ms(20, 16, 30)))
    assert st.reference("ES", ms(20, 16, 30, 30)) is None   # none known for 03-27 yet: the feed's previous close
    st.on_feed("ES", 7060.00, ms(20, 16, 31))   # 03-27's own Tuesday settlement
    asyncio.run(_quote(m, "ES", 7061.00, 7060.00, ms(20, 17, 30)))
    assert (m.quotes["ES1!"]["prev"], m.quotes["ES1!"]["chg"]) == (7060.00, 1.00)
    assert Settlements(m, tmp_path / "cfg").reference("ES", ms(20, 17, 30))["price"] == 7060.00   # after a restart too
    # Wednesday's from the 03-27 trades; until the open the day's change is still from 03-27's Tuesday one
    m.store.write([Tick("ES", ms(21, 14, 59, 40), 7070.00, 10, 1)], [])
    assert st.reference("ES", ms(21, 15, 1, 30))["price"] == 7060.00
    assert st.reference("ES", ms(21, 17, 0, 30))["price"] == 7070.00
    # no S line after a roll: the windows before it are not worked out from the stored (12-26) trades
    (tmp_path / "b").mkdir()
    m2 = market(tmp_path / "b", monkeypatch)
    m2.store.write([Tick("ES", ms(20, 14, 59, 40), 7000.00, 10, 1)], [])
    m2.contracts["ES"] = "ES 12-26"
    asyncio.run(_contract(m2, "ES", "ES 03-27", ms(20, 16, 30)))
    assert m2.settlements.reference("ES", ms(20, 17, 30)) is None
    m2.store.write([Tick("ES", ms(21, 14, 59, 40), 7070.00, 10, 1)], [])
    assert m2.settlements.reference("ES", ms(21, 15, 1, 30)) is None   # Wednesday's set; Tuesday's (12-26) not known
    assert m2.settlements.reference("ES", ms(21, 17, 0, 30))["price"] == 7070.00


def test_a_watchlist_futures_roll_drops_the_old_contracts_settlement(tmp_path, monkeypatch):
    """A future only quoted (ZN in a watchlist, not charted) rolls by NinjaTrader's dates when the trade date turns at
    16:00 CT; the add-on says so (C|...|quote): until the 17:00 open the change is from the feed's previous close, not
    from the old contract's settlement the day before, off by the spread between the two contracts."""
    import chartsrv.market as mk
    import time
    m = market(tmp_path, monkeypatch)
    assert "ZN" not in m.instruments
    feed = NinjaTraderFeed("127.0.0.1", 0, "")
    clock = {}
    monkeypatch.setattr(mk, "now_ms", lambda: clock["t"])
    monkeypatch.setattr(time, "time", lambda: clock["t"] / 1000)

    def at(t, line):
        clock["t"] = t
        feed._line(m, line.encode())
    at(ms(21, 14, 45), "S|ZN|112.0")    # Wednesday: ZN 12-26 settles 112.0
    at(ms(22, 14, 45), "S|ZN|112.5")    # Thursday: 112.5
    at(ms(22, 15, 30), "Q|ZN|112.5|112.25")
    assert m.quotes["ZN1!"]["prev"] == 112.0   # the day's change until 17:00
    at(ms(22, 16, 1), "C|ZN|ZN 03-27|quote")   # rolled: the new contract's Thursday settlement and quotes follow
    at(ms(22, 16, 1), "S|ZN|111.875")
    at(ms(22, 16, 5), "Q|ZN|111.875|111.25")
    assert (m.quotes["ZN1!"]["prev"], m.quotes["ZN1!"]["chg"]) == (111.25, 0.625)   # not 112.0, 12-26's Wednesday one
    at(ms(22, 17, 5), "Q|ZN|112.0|111.25")
    assert (m.quotes["ZN1!"]["prev"], m.quotes["ZN1!"]["chg"]) == (111.875, 0.125)
    m.store.write([Tick("ES", ms(22, 14, 59, 40), 7000.00, 10, 1)], [])
    at(ms(22, 16, 2), "C|ES|ES 03-27|quote")   # charted here: its own C line rolls it
    assert m.settlements.reference("ES", ms(22, 17, 5))["price"] == 7000.00 and feed.stats["bad_lines"] == 0


def test_a_micro_between_its_and_its_e_minis_rolls(tmp_path, monkeypatch):
    """Each rolls by its own volume, so MES and ES can roll in different breaks: a micro settles to its E-mini of the
    same month, so until both are on it the micro's change is from its own (its trades, or NinjaTrader's S line)."""
    m = market(tmp_path, monkeypatch)
    st = m.settlements
    m.store.write([Tick(r, ms(d, 14, 59, 40), p, 10, 1) for r in ("ES", "MES") for d, p in ((19, 6990.00), (20, 7000.00))], [])
    assert st.reference("MES", ms(20, 15, 1, 30))["price"] == 6990.00   # Tuesday's set; Monday's until the open
    m.contracts.update({"ES": "ES 12-26", "MES": "MES 12-26"})
    asyncio.run(_contract(m, "ES", "ES 03-27", ms(20, 16, 30)))   # ES's volume crossed first
    st.on_feed("ES", 7060.00, ms(20, 16, 31))   # 03-27's Tuesday settlement
    asyncio.run(_quote(m, "MES", 7001.00, 7000.00, ms(20, 17, 30)))   # MES still on 12-26
    assert (m.quotes["MES1!"]["prev"], m.quotes["MES1!"]["chg"]) == (7000.00, 1.00)   # not ES 03-27's 7060
    asyncio.run(_contract(m, "MES", "MES 03-27", ms(21, 16, 30)))   # MES the day after: ES 03-27's again
    assert st.reference("MES", ms(21, 16, 31))["price"] == 7060.00
    (tmp_path / "b").mkdir()   # the other way round: MES first, with NinjaTrader's S line for MES 03-27
    m = market(tmp_path / "b", monkeypatch)
    st = m.settlements
    m.store.write([Tick(r, ms(d, 14, 59, 40), p, 10, 1) for r in ("ES", "MES") for d, p in ((19, 6990.00), (20, 7000.00))], [])
    assert st.reference("MES", ms(20, 15, 1, 30))["price"] == 6990.00   # Tuesday's set; Monday's until the open
    m.contracts.update({"ES": "ES 12-26", "MES": "MES 12-26"})
    asyncio.run(_contract(m, "MES", "MES 03-27", ms(20, 16, 30)))
    st.on_feed("MES", 7060.00, ms(20, 16, 31))
    asyncio.run(_quote(m, "MES", 7061.00, 7060.00, ms(20, 17, 30)))
    assert (m.quotes["MES1!"]["prev"], m.quotes["MES1!"]["chg"]) == (7060.00, 1.00)   # not ES 12-26's 7000
    assert st.reference("ES", ms(20, 17, 30))["price"] == 7000.00


def test_yesterdays_settlement_on_a_connect_soon_after_the_window(tmp_path, monkeypatch):
    """NinjaTrader connects soon after the window it missed and sends the settlement it holds, yesterday's (a tick off
    the app's own from the trades): taken for today's for now, it gives way to today's trades and to the next one."""
    m = market(tmp_path, monkeypatch)
    st = m.settlements
    m.store.write([Tick("ES", ms(20, 14, 59, 40), 6910.00, 10, 1)], [])   # CME's own for Tuesday: 6910.25
    assert st.reference("ES", ms(21, 10, 0))["price"] == 6910.00
    st.on_feed("ES", 6910.25, ms(21, 15, 8))
    m.store.write([Tick("ES", ms(21, 14, 59, 40), 6950.00, 10, 1)], [])   # the gap fill: Wednesday's window
    st.on_feed("ES", 6950.25, ms(21, 15, 14))   # CME publishes Wednesday's
    assert st.reference("ES", ms(21, 15, 15)) == {"t": ms(20, 15, 0), "price": 6910.00, "src": "trades"}
    assert st.reference("ES", ms(21, 17, 5)) == {"t": ms(21, 15, 0), "price": 6950.00, "src": "trades"}
    (tmp_path / "b").mkdir()   # no trades for Wednesday's window: the feed's next value is Wednesday's
    st = market(tmp_path / "b", monkeypatch).settlements
    st.m.store.write([Tick("ES", ms(20, 14, 59, 40), 6910.00, 10, 1)], [])
    assert st.reference("ES", ms(21, 10, 0))["price"] == 6910.00
    st.on_feed("ES", 6910.25, ms(21, 15, 8))
    st.on_feed("ES", 6950.25, ms(21, 15, 14))
    assert st.reference("ES", ms(21, 15, 15))["price"] == 6910.00
    assert st.reference("ES", ms(21, 17, 5)) == {"t": ms(21, 15, 0), "price": 6950.25, "src": "feed"}


def test_a_settlement_from_all_the_windows_trades(tmp_path, monkeypatch):
    """Worked out a minute after the window, not at the first look once it ends; and again when a gap fill stores
    more of the window's trades (NinjaTrader was down for part of it, another source looked meanwhile)."""
    m = market(tmp_path, monkeypatch)
    st = m.settlements
    m.store.write([Tick("ES", ms(20, 14, 59, 40), 7700.00, 10, 1), Tick("ES", ms(21, 14, 59, 35), 7800.00, 10, 1)], [])
    assert st.reference("ES", ms(21, 15, 0) + 200)["price"] == 7700.00
    assert st.saved["ES"]["t"] == ms(20, 15, 0)   # not yet: the window's last trades may still be on their way
    m.store.write([Tick("ES", ms(21, 14, 59, 55), 7820.00, 1000, 1)], [])
    assert st.reference("ES", ms(21, 17, 1))["price"] == 7819.75   # (7800 * 10 + 7820 * 1000) / 1010, to the tick
    (tmp_path / "b").mkdir()
    m = market(tmp_path / "b", monkeypatch)
    st = m.settlements
    m.store.write([Tick("ES", ms(20, 14, 59, 40), 7700.00, 10, 1), Tick("ES", ms(21, 14, 59, 35), 7800.00, 10, 1)], [])
    assert st.reference("ES", ms(21, 15, 5))["price"] == 7700.00
    assert st.saved["ES"] == {"t": ms(21, 15, 0), "price": 7800.00, "src": "trades",
                              "prev": {"t": ms(20, 15, 0), "price": 7700.00, "src": "trades"}}
    m.store.write([Tick("ES", ms(21, 14, 59, 55), 7820.00, 1000, 1), Tick("ES", ms(20, 14, 59, 50), 7710.00, 10, 1)], [])
    st.history_changed("ES", ms(21, 15, 20))   # the gap fill, done
    assert st.reference("ES", ms(21, 15, 21))["price"] == 7705.00
    assert st.reference("ES", ms(21, 17, 1))["price"] == 7819.75


def _gap_filled(tmp_path, monkeypatch):
    """A settlement worked out from part of its window's trades, then the gap fill stores the rest: its Z line done,
    gapfill.settle runs Market.history_changed (at 15:20 CT, as settlement.py tells the time)."""
    m = market(tmp_path, monkeypatch)
    m.store.write([Tick("ES", ms(20, 14, 59, 40), 7700.00, 10, 1), Tick("ES", ms(21, 14, 59, 35), 7800.00, 10, 1)], [])
    assert m.settlements.reference("ES", ms(21, 15, 5))["price"] == 7700.00
    m.store.write([Tick("ES", ms(21, 14, 59, 55), 7820.00, 1000, 1), Tick("ES", ms(20, 14, 59, 50), 7710.00, 10, 1)], [])
    m.gapfill.changed.add("ES")
    monkeypatch.setattr(settlement, "time", SimpleNamespace(time=lambda: ms(21, 15, 20) / 1000))
    return m


def test_a_gap_fill_works_the_settlement_out_again(tmp_path, monkeypatch):
    m = _gap_filled(tmp_path, monkeypatch)
    m.gapfill.settle("ES")
    assert m.settlements.reference("ES", ms(21, 15, 21))["price"] == 7705.00   # (7700 * 10 + 7710 * 10) / 20
    assert m.settlements.reference("ES", ms(21, 17, 1))["price"] == 7819.75


def test_a_failed_settlement_write_in_a_gap_fill_skips_no_reload(tmp_path, monkeypatch):
    """The database locked by an import past the busy timeout: the settlement isn't saved, the rest still reloads
    (TPO's daily bars, the profiles POC/VAH/VAL alerts read) and the feed carries on."""
    m = _gap_filled(tmp_path, monkeypatch)
    m.on_tick(Tick("ES", ms(21, 15, 19), 7830.00, 1, 1))   # a live trade waiting to be written
    m.tpo.daily["ES"], m.books["ES"] = "stale daily bars", "stale profiles"

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(m.store, "write", locked)
    monkeypatch.setattr(m.store, "set_setting", locked)
    m.gapfill.settle("ES")
    assert "ES" not in m.tpo.daily and "ES" not in m.books
