"""Free-data charts: any timeframe built from a source's shorter bars, IBKR first with Yahoo behind it, and the
charted symbol quoted while it is charted."""
import asyncio
import dataclasses
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from chartsrv import ext
from chartsrv.bars import calendar_bucket
from chartsrv.feeds import ibkr
from chartsrv.sessions import day_open
from test_ext import yahoo_doc
from test_review_fixes import build_market

NY = ZoneInfo("America/New_York")


def ms(*a, tz=NY) -> int:
    return int(datetime(*a, tzinfo=tz).timestamp() * 1000)


def bar(t, c, v=10):
    return {"t": t, "o": c - 0.5, "h": c + 1, "l": c - 1, "c": c, "v": v}


def test_specs_and_bases():
    assert [ext.yahoo_base(s) for s in ("1m", "3m", "4m", "10m", "45m", "2h", "4h", "2D", "2W", "3M")] == \
        ["1m", "1m", "2m", "5m", "15m", "1h", "1h", "1D", "1W", "1M"]
    assert ext.yahoo_base("2h", eth=True) == "30m"   # Yahoo's hourly bars with the pre-market don't meet 9:30
    assert ext.split_spec("90m") == (90, "") and ext.split_spec("24h") == (1440, "") and ext.split_spec("3M") == (3, "M")
    for bad in ("2000t", "30s", "48h", "0m", "13M", "x"):
        with pytest.raises(ValueError):
            ext.split_spec(bad)
    assert ext.anchor_of(ext.US_STOCK) == "09:30" and ext.anchor_of(ext.US_STOCK, rth=False) == "04:00"
    assert ext.anchor_of(ext.IB_CLOCK["CASH"]) == "17:00" and ext.anchor_of(ext.IB_CLOCK["CRYPTO"]) == "00:00"
    # IBKR: 30-minute bars under a stock's hours (its hourly ones start on the hour, the open at 9:30)
    assert ibkr.base_minutes(60, 570) == 30 and ibkr.base_minutes(240, 1020) == 60 and ibkr.base_minutes(7, 570) == 1
    assert ibkr.base_minutes(45, 570) == 15 and ibkr.base_minutes(20, 570) == 10
    # about 3000 of the chart's bars, at most HISTORY_BARS (2000) of IBKR's: what it answers in seconds
    assert ibkr.duration(1, 1, 390) == "8 D" and ibkr.duration(5, 5, 390) == "6 W" and ibkr.duration(60, 30, 390) == "31 W"
    assert ibkr.duration(240, 30, 390) == "31 W" and ibkr.duration(1, 1, 1440, weekdays=False) == "2 D"
    assert ibkr.duration(240, 60, 960) == "25 W"   # a stock's 4h with ETH: hourly bars from 4:00, not over 1 Y
    assert ext._decimals(0.01) == 2 and ext._decimals(0.00005) == 5 and ext._decimals(0.25) == 2 and ext._decimals(0) is None


def test_regroup_intraday_counts_from_the_open():
    ones = [bar(ms(2026, 10, 20, 9, 30 + i), 100 + i) for i in range(7)]
    three = ext.regroup(ones, "3m", ext.US_STOCK)
    assert [(b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in three] == [
        (ms(2026, 10, 20, 9, 30), 99.5, 103, 99, 102, 30), (ms(2026, 10, 20, 9, 33), 102.5, 106, 102, 105, 30),
        (ms(2026, 10, 20, 9, 36), 105.5, 107, 105, 106, 10)]
    hours = [bar(ms(2026, 10, 20, 9, 30) + i * 3_600_000, 50 + i) for i in range(7)]   # 9:30 ... 15:30
    assert [b["t"] for b in ext.regroup(hours, "2h", ext.US_STOCK)] == \
        [ms(2026, 10, 20, h, 30) for h in (9, 11, 13, 15)]   # the last one short, as TradingView's
    fx = [bar(ms(2026, 10, 20, h), 1.1) for h in (15, 16, 17, 18)]   # the 17:00 New York roll starts a day
    assert [b["t"] for b in ext.regroup(fx, "4h", ext.IB_CLOCK["CASH"])] == [ms(2026, 10, 20, 13), ms(2026, 10, 20, 17)]
    # across a daylight-saving change the anchor stays 9:30 New York
    nov = [bar(ms(2026, 11, d, 9, 30), 1) for d in (2, 3)]
    assert [b["t"] for b in ext.regroup(nov, "2h", ext.US_STOCK)] == [ms(2026, 11, 2, 9, 30), ms(2026, 11, 3, 9, 30)]


def test_regroup_calendar_from_fixed_starts():
    days = [bar(day_open(date(2026, 10, d)), 10 + d) for d in (19, 20, 21, 22, 23, 26)]
    two = ext.regroup(days, "2D", ext.US_STOCK)
    want = sorted({calendar_bucket(b["t"], "D", 2) for b in days})
    assert [b["t"] for b in two] == want and sum(b["v"] for b in two) == 60
    assert ext.regroup(days, "1D", ext.US_STOCK) is days
    months = [bar(day_open(date(2026, m, 1)), m) for m in range(1, 7)]
    q = ext.regroup(months, "3M", ext.US_STOCK)
    assert [(b["t"], b["o"], b["c"]) for b in q] == [(day_open(date(2026, 1, 1)), 0.5, 3), (day_open(date(2026, 4, 1)), 3.5, 6)]


def _market(tmp_path, monkeypatch, answer):
    calls = []

    def handler(req):
        calls.append(req)
        return answer(req)
    return build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))[0], calls


def test_yahoo_builds_any_timeframe(tmp_path, monkeypatch):
    ts = [ms(2026, 10, 20, 9, 30) // 1000 + i * 3600 for i in range(7)]

    doc = yahoo_doc(ts, [100.0 + i for i in range(7)])
    doc["chart"]["result"][0]["meta"]["currentTradingPeriod"] = {"regular": {"start": ts[0], "end": ts[0] + 23_400}}

    def answer(req):
        return httpx.Response(200, json=doc)
    m, calls = _market(tmp_path, monkeypatch, answer)

    async def go():
        d = await m.ext.bars("AAPL", "2h")
        assert calls[-1].url.params["interval"] == "60m" and (d["spec"], d["source"], d["custom"]) == ("2h", "yahoo", True)
        assert [b["t"] for b in d["bars"]] == [ms(2026, 10, 20, h, 30) for h in (9, 11, 13, 15)]
        assert [b["seq"] for b in d["bars"]] == [1, 2, 3, 4]
        w = await m.ext.bars("AAPL", "2W")
        assert calls[-1].url.params["interval"] == "1wk" and w["spec"] == "2W"
        with pytest.raises(ValueError):
            await m.ext.bars("AAPL", "48h")
    asyncio.run(go())


class FakeIB:
    """IBKRQuotes as ext.py sees it: connected, and its history answers."""
    name = "ibkr"

    def __init__(self, answer):
        self.answer, self.asked, self.up = answer, [], True

    def connected(self):
        return self.up

    async def history(self, key, size, span, rth=True, timeout=ibkr.HISTORY_TIMEOUT_S):
        self.asked.append((key, size, span, rth))
        return self.answer(key, size, span)


def utc(t_ms):
    return datetime.fromtimestamp(t_ms / 1000, timezone.utc)


def test_ibkr_first_then_yahoo(tmp_path, monkeypatch):
    start = ms(2026, 10, 20, 9, 30)
    rows = [(utc(start + i * 1_800_000), 10 + i, 11 + i, 9 + i, 10.5 + i, 100) for i in range(4)]   # 30-minute bars

    def ib_answer(key, size, span):
        if key == "AAPL":
            return {"kind": "STK", "name": "APPLE INC", "tick": 0.01, "rows": rows if span.endswith("W") else rows[-1:]}
        if key == "ZN1!" and size == "1 day":
            return {"kind": "CONTFUT", "name": "10-Year T-Note", "tick": 0.015625, "rows": [(date(2026, 10, 20), 112, 113, 111, 112.5, 9)]}
        return {"kind": "STK", "name": "", "tick": 0.01, "rows": []}   # no market data subscription
    fake = FakeIB(ib_answer)

    def yahoo(req):
        return httpx.Response(200, json=yahoo_doc([start // 1000], [55.0]))
    m, calls = _market(tmp_path, monkeypatch, yahoo)
    m.feed_objs.append(fake)

    async def go():
        d = await m.ext.bars("AAPL", "1h")
        assert (d["source"], d["feed"], d["spec"], d["decimals"], d["name"]) == ("ibkr", "live", "1h", 2, "APPLE INC")
        assert fake.asked[-1] == ("AAPL", "30 mins", "31 W", True)   # 30-minute bars: the hour counts from 9:30
        assert [(b["t"], b["o"], b["c"], b["v"]) for b in d["bars"]] == [(start, 10, 11.5, 200), (start + 3_600_000, 12, 13.5, 200)]
        assert d["exchange"] == ext.US_STOCK and not calls
        await m.ext.bars("AAPL", "1h")
        assert len(fake.asked) == 1   # kept a little while
        for k in list(m.ext.cache):
            m.ext.cache[k] = (0, m.ext.cache[k][1])
        again = await m.ext.bars("AAPL", "1h")
        assert fake.asked[-1][2].endswith(" S") and len(again["bars"]) == 2   # a poll asks for the newest bars only
        # nothing from IBKR (no subscription): Yahoo, and IBKR isn't asked again for a while
        y = await m.ext.bars("MSFT", "5m")
        n = len(fake.asked)
        await m.ext.bars("MSFT", "5m")
        assert y["source"] == "yahoo" and len(fake.asked) == n
        # a future only as XX1!, never a stock's ticker as one
        z = await m.ext.bars("ZN1!", "1D")
        assert (z["source"], z["decimals"], z["exchange"]) == ("ibkr", 6, ext.CME) and z["bars"][0]["t"] == day_open(date(2026, 10, 20))
        await m.ext.bars("BZ1!", "1D")
        assert not any(a[0] in ("BZ", "BZ1!") for a in fake.asked)
        fake.up = False
        assert (await m.ext.bars("AAPL", "15m"))["source"] == "yahoo"   # IBKR down: Yahoo
        m.s = dataclasses.replace(m.s, ibkr_market_data_type=3)
        fake.up = True
        assert (await m.ext.bars("AAPL", "1h"))["feed"] == "delayed"
    asyncio.run(go())


def test_charted_symbol_is_quoted(tmp_path, monkeypatch):
    m, _ = _market(tmp_path, monkeypatch, lambda req: httpx.Response(200, json=yahoo_doc([1_792_000_000], [10.0])))
    heard = []

    class Feed:
        name = "probe"

        def watch_changed(self, market):
            heard.append(set(market.watch_keys))
    m.feed_objs.append(Feed())
    asyncio.run(m.ext.bars("ORCL", "5m"))
    assert "ORCL" in m.watch_keys and len(heard) == 1
    asyncio.run(m.ext.bars("ORCL", "5m"))
    assert len(heard) == 1   # already quoted
    m.chart_keys["ORCL"] = time.monotonic() - 1   # the chart closed 10 minutes ago
    asyncio.run(m.ext.bars("AMD", "5m"))
    assert "AMD" in m.watch_keys and "ORCL" not in m.watch_keys
    m.set_watchlists(m.wl)
    assert "AMD" in m.watch_keys   # a list change keeps the charted ones


def test_ibkr_subscriptions_move_without_reconnecting():
    class Ticker:
        def __init__(self, c):
            self.contract = c

    class IB:
        def __init__(self):
            self.live, self.cancelled = [], []

        def isConnected(self):
            return True

        async def qualifyContractsAsync(self, *cs):
            return [None if c.symbol == "NOPE" else c for c in cs]

        def reqMktData(self, c, *a):
            self.live.append(c.symbol)
            return Ticker(c)

        def cancelMktData(self, c):
            self.cancelled.append(c.symbol)

    class Market:
        wl = {"lists": [{"name": "a", "items": ["BZ1!", "AAPL"]}]}
        watch_keys = {"AAPL", "MSFT", "BZ1!", "NOPE", "DXY"}

    feed, ib, subs, by = ibkr.IBKRQuotes(None), IB(), {}, {}
    asyncio.run(feed._sync(ib, Market, subs, by))
    assert sorted(subs) == ["AAPL", "MSFT"] and sorted(ib.live) == ["AAPL", "MSFT"]   # BZ1!: no IBKR future here, nor the stock BZ
    Market.watch_keys = {"AAPL", "ORCL"}
    asyncio.run(feed._sync(ib, Market, subs, by))
    assert sorted(subs) == ["AAPL", "ORCL"] and ib.cancelled == ["MSFT"] and ib.live.count("AAPL") == 1
    assert sorted(k for keys in by.values() for k in keys) == ["AAPL", "ORCL"]
