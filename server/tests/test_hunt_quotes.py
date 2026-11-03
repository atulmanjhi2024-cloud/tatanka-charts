"""Bug-hunt fixes in the quote feeds and the free data around them: which feed quotes a symbol, the economic and
earnings calendars, stored FRED series, Yahoo's ETH hours, the simulator's quotes."""
import asyncio
import dataclasses
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from chartsrv import earnings, ext, market as mk
from chartsrv.feeds import fred, ibkr
from chartsrv.sessions import CT
from test_ext import yahoo_doc
from test_review_fixes import build_market


class Ticker:
    def __init__(self, c):
        self.contract = c


class IB:
    """Enough of ib_async's IB for IBKRQuotes._sync: what it subscribed, as (secType, symbol)."""

    def __init__(self):
        self.live = []

    def isConnected(self):
        return True

    async def qualifyContractsAsync(self, *cs):
        return list(cs)

    def reqMktData(self, c, *a):
        self.live.append((c.secType, c.symbol))
        return Ticker(c)

    def cancelMktData(self, c):
        pass


def test_charted_future_is_not_quoted_as_the_same_named_stock(tmp_path, monkeypatch):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=yahoo_doc([1_792_000_000], [380.0]))))
    m, _ = build_market(tmp_path, monkeypatch, http=http)
    m.set_watchlists({"lists": [{"name": "a", "items": ["AAPL"]}]})
    asyncio.run(m.ext.bars("KC1!", "1D"))   # coffee, charted from the search, in no list
    asyncio.run(m.ext.bars("ORCL", "1D"))   # a stock, charted
    assert {"KC1!", "ORCL"} <= m.watch_keys and "KC" not in m.watch_keys
    feed, ib = ibkr.IBKRQuotes(None), IB()
    asyncio.run(feed._sync(ib, m, {}, {}))
    assert ("STK", "KC") not in ib.live   # not Kingsoft Cloud's price on the coffee chart
    assert ("STK", "ORCL") in ib.live and ("STK", "AAPL") in ib.live


def test_sources_of_the_same_rank_do_not_take_turns(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    clock = [1_000_000]
    monkeypatch.setattr(mk, "now_ms", lambda: clock[0])
    seen = []
    for _ in range(3):   # IBKR ticks, Alpaca polls: the first one keeps the symbol while it quotes
        m.on_quote("AAPL", 250.0, 248.0, 1_000_000, source="ibkr")
        seen.append(m.quotes["AAPL"]["src"])
        clock[0] += 1000
        m.on_quote("AAPL", 250.1, 248.0, None, source="alpaca")
        seen.append(m.quotes["AAPL"]["src"])
        clock[0] += 1000
    assert seen == ["ibkr"] * 6 and m.quotes["AAPL"]["vol"] == 1_000_000
    clock[0] += mk.QUOTE_HOLD_MS   # IBKR went quiet: Alpaca fills in
    m.on_quote("AAPL", 250.2, 248.0, None, source="alpaca")
    assert m.quotes["AAPL"]["src"] == "alpaca" and not m.quote_wanted("AAPL", "ibkr")
    # IBKR's delayed data: behind Alpaca's real-time quotes, still ahead of Yahoo's
    m.s = dataclasses.replace(m.s, ibkr_market_data_type=3)
    m.on_quote("MSFT", 500.0, 490.0, 10, source="ibkr")
    m.on_quote("MSFT", 503.0, 490.0, None, source="alpaca")
    m.on_quote("MSFT", 500.0, 490.0, 10, source="ibkr")
    assert m.quotes["MSFT"]["src"] == "alpaca" and m.quotes["MSFT"]["last"] == 503.0
    m.on_quote("NVDA", 180.0, 178.0, 10, source="ibkr")
    m.on_quote("NVDA", 179.0, 178.0, 10, source="yahoo")
    assert m.quotes["NVDA"]["src"] == "ibkr"


def test_calendar_drops_events_the_feed_moved_or_removed(tmp_path, monkeypatch):
    now = int(time.time() * 1000)
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, ZoneInfo("America/New_York")).isoformat()   # noqa: E731
    ev = lambda title, t: {"title": title, "country": "USD", "date": iso(t), "impact": "High"}   # noqa: E731
    feed = {"thisweek": [ev("ISM", now - 6 * 86_400_000), ev("NFP", now + 3 * 60_000), ev("CPI", now + 86_400_000)],
            "nextweek": [ev("FOMC", now + 8 * 86_400_000)]}

    def answer(req):
        got = feed[req.url.path.split("_")[-1].removesuffix(".json")]
        return httpx.Response(503) if got is None else httpx.Response(200, json=got)
    http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    m, _ = build_market(tmp_path, monkeypatch, http=http)
    fired = []
    m._on_alert = lambda e: fired.append(e["message"])
    titles = lambda: [(e["title"], e["t"]) for e in m.calendar.events]   # noqa: E731

    async def go():
        await m.calendar.refresh()
        # a new week: last week's ISM gone from the feed, NFP postponed two days, next week's not read this time
        feed.update(thisweek=[ev("NFP", now + 2 * 86_400_000), ev("CPI", now + 86_400_000)], nextweek=None)
        await m.calendar.refresh()
        assert titles() == [("ISM", now - 6 * 86_400_000), ("CPI", now + 86_400_000), ("NFP", now + 2 * 86_400_000),
                            ("FOMC", now + 8 * 86_400_000)]   # last week's for the markers; next week's kept
        m.calendar._warn()
        assert fired == []   # no warning at NFP's old time
        feed.update(nextweek=[])   # read again: FOMC is gone from it
        await m.calendar.refresh()
        assert [x for x, _ in titles()] == ["ISM", "CPI", "NFP"]
    asyncio.run(go())


def test_earnings_day_not_read_keeps_its_rows(tmp_path, monkeypatch):
    today = datetime.now(CT).date()
    weekdays = [today + timedelta(days=i) for i in range(earnings.AHEAD_DAYS + 1) if (today + timedelta(days=i)).weekday() < 5]
    big = lambda sym: {"time": "time-after-hours", "symbol": sym, "name": sym, "marketCap": "$3,000,000,000,000"}   # noqa: E731
    reports = {weekdays[0].isoformat(): [big("AAPL")], weekdays[5].isoformat(): [big("NVDA")]}
    down = [False]

    def answer(req):
        day = req.url.params["date"]
        if down[0] and day != weekdays[0].isoformat():
            raise httpx.ConnectTimeout("Nasdaq throttles the rest")
        return httpx.Response(200, json={"data": {"rows": reports.get(day)}})
    http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    m, _ = build_market(tmp_path, monkeypatch, http=http)
    m.s = dataclasses.replace(m.s, symbols_online=True)

    async def no_sleep(s):
        if s == 60:   # run()'s wait for the next round: stop there
            raise asyncio.CancelledError
    monkeypatch.setattr(earnings.asyncio, "sleep", no_sleep)
    ids = lambda: sorted(r["id"] for r in m.earnings.rows)   # noqa: E731
    asyncio.run(m.earnings.refresh())
    assert ids() == [f"AAPL|{weekdays[0]}", f"NVDA|{weekdays[5]}"]
    down[0] = True
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(m.earnings.run())
    assert ids() == [f"AAPL|{weekdays[0]}", f"NVDA|{weekdays[5]}"]   # NVDA's day was not read: its report stays
    assert m.earnings.error == f"{len(weekdays) - 1} days"   # the tab says so
    assert time.time() - m.earnings.fetched > earnings.REFRESH_S - 1900   # and asks again in 30 minutes, not 6 hours


def test_calendar_and_earnings_loops_go_on_when_the_database_cannot_be_written(tmp_path, monkeypatch):
    """A full disk, or another program holding the database's lock: the calendar's and the earnings' refreshes can't
    save what they read. Each loop says so and tries again later (it used to end until a restart), and a release's
    warning still goes out."""
    import sqlite3
    from chartsrv import econ_calendar
    now = int(time.time() * 1000)
    cpi = {"title": "CPI m/m", "country": "USD", "impact": "High",
           "date": datetime.fromtimestamp(now / 1000 + 180, ZoneInfo("America/New_York")).isoformat()}

    def answer(req):
        if req.url.host == "api.nasdaq.com":
            return httpx.Response(200, json={"data": {"rows": [
                {"time": "time-after-hours", "symbol": "AAPL", "name": "Apple", "marketCap": "$3,000,000,000,000"}]}})
        return httpx.Response(200, json=[cpi]) if "thisweek" in req.url.path else httpx.Response(404)
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    m.s = dataclasses.replace(m.s, symbols_online=True)
    fired = []
    m._on_alert = lambda ev: fired.append((ev["message"], ev["id"]))

    async def one_round(s):
        if s in (20, 60):   # each run()'s wait for its next round: stop there
            raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", one_round)
    m.store.db.execute("PRAGMA busy_timeout=50")   # the real wait is 5 s
    other = sqlite3.connect(m.store.path)
    other.execute("BEGIN IMMEDIATE")               # the database can't be written for now
    try:
        for run in (m.calendar.run, m.earnings.run):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(run())
    finally:
        other.rollback()
        other.close()
    assert m.calendar.error == m.earnings.error == "OperationalError"
    assert abs(time.time() - m.calendar.fetched - (econ_calendar.REFRESH_S - 600)) < 60   # again in 10 minutes
    assert abs(time.time() - m.earnings.fetched - (earnings.REFRESH_S - 1800)) < 60       # again in 30
    assert fired == [("USD CPI m/m in 3 min (High impact)", None)]   # warned, with no event row

def test_stored_fred_series_survives_a_bad_answer(tmp_path, monkeypatch):
    csv = "observation_date,DGS10\n2026-10-22,4.10\n2026-10-23,4.12\n"
    answer = [httpx.Response(200, text=csv)]
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: answer[0]))
    m, _ = build_market(tmp_path, monkeypatch, http=http)
    clock = [time.time()]
    monkeypatch.setattr(fred.time, "time", lambda: clock[0])

    async def go():
        assert (await m.fred.series("DGS10"))[0][-1] == ("2026-10-23", 4.12)
        clock[0] += 4 * 3600   # the stored copy is due again, and FRED answers once with a page that isn't CSV
        answer[0] = httpx.Response(200, text="<html><body>FRED is temporarily unavailable</body></html>")
        assert (await m.fred.series("DGS10"))[0][-1] == ("2026-10-23", 4.12)   # the stored copy, not "not a FRED series"
        answer[0] = httpx.Response(200, text=csv + "2026-10-26,4.15\n")   # FRED back a minute later
        clock[0] += 60
        assert (await m.fred.series("DGS10"))[0][-1] == ("2026-10-26", 4.15)
        answer[0] = httpx.Response(404, text="not found")   # a series it never had is still not asked again for a while
        for _ in range(2):
            with pytest.raises(ValueError):
                await m.fred.series("NOPE")
    asyncio.run(go())


def test_stock_1h_with_eth_counts_from_4am_like_2h(tmp_path, monkeypatch):
    ny = ZoneInfo("America/New_York")
    at = lambda h, mi: int(datetime(2026, 10, 20, h, mi, tzinfo=ny).timestamp())   # noqa: E731
    asked = []

    def answer(req):
        asked.append((req.url.path.rsplit("/", 1)[-1], req.url.params["interval"], req.url.params["includePrePost"]))
        ts = [at(9, 0), at(9, 30), at(10, 0)] if req.url.params["interval"] == "30m" else [at(9, 0), at(9, 30), at(10, 30)]
        doc = yahoo_doc(ts, [100.0, 101.0, 102.0])
        doc["chart"]["result"][0]["meta"]["currentTradingPeriod"] = {"regular": {"start": at(9, 30), "end": at(16, 0)}}
        return httpx.Response(200, json=doc)
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    assert ext.yahoo_base("1h", eth=True) == "30m" and ext.yahoo_base("1h") == "1h" and ext.yahoo_base("1h", True, stock=False) == "1h"
    d = asyncio.run(m.ext.bars("AAPL", "1h", rth=False))
    assert asked == [("AAPL", "30m", "true")]   # Yahoo's 60-minute bars with the pre-market have a half-hour 9:00 one
    assert [b["t"] for b in d["bars"]] == [at(9, 0) * 1000, at(10, 0) * 1000] and d["bars"][0]["c"] == 101.0   # 9:00-10:00
    asyncio.run(m.ext.bars("BTCUSD", "1h", rth=False))
    assert asked[-1] == ("BTC-USD", "60m", "true")   # crypto keeps its hourly bars and their two years


def test_simulator_leaves_real_series_alone(monkeypatch):
    from chartsrv.feeds import sim

    class Market:
        watch_keys = {"DATA:GOLDOIL", "FRED:DGS10", "EIA:PET.WCESTUS1.W", "AAPL"}
        instruments, last_tick, quoted = {}, {}, []

        def on_quote(self, sym, *a, **kw):
            self.quoted.append(sym)

    async def one_round(s):
        raise asyncio.CancelledError
    monkeypatch.setattr(sim.asyncio, "sleep", one_round)
    m = Market()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sim.SimQuotes(seed=1).run(m))
    assert m.quoted == ["AAPL"]   # your CSV's value, not a random walk over it
