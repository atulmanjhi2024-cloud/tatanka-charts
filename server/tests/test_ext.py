"""Free data: FRED series (watchlist values, chart bars) and Yahoo history for symbols with no live chart."""
import asyncio
from datetime import date, datetime

import httpx
import pytest

from chartsrv import ext
from chartsrv.feeds import fred
from chartsrv.sessions import CT, day_open
from test_review_fixes import build_market

DGS10 = "observation_date,DGS10\n2026-10-12,\n2026-10-13,4.05\n2026-10-14,4.11\n2026-10-15,4.087\n2026-10-16,4.10\n"
OLD = "DATE,UNRATE\n2026-07-01,4.2\n2026-08-01,.\n2026-09-01,4.3\n"


def test_fred_csv():
    obs, dec = fred.parse_csv(DGS10)
    assert obs == [("2026-10-13", 4.05), ("2026-10-14", 4.11), ("2026-10-15", 4.087), ("2026-10-16", 4.1)] and dec == 3
    assert fred.parse_csv(OLD) == ([("2026-07-01", 4.2), ("2026-09-01", 4.3)], 1)
    with pytest.raises(ValueError):
        fred.parse_csv("<html>Series not found</html>")
    assert fred.series_id("FRED:dgs10") == "DGS10" and fred.series_id("DGS10") is None and fred.series_id("FRED:a b") is None
    # a day is stamped as the session that trades on it opens: 17:00 CT the evening before
    assert day_open(date(2026, 10, 16)) == int(datetime(2026, 10, 15, 17, tzinfo=CT).timestamp() * 1000)


def _market(tmp_path, monkeypatch, answer):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return answer(req)
    return build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))[0], calls


def test_fred_series_is_kept_and_quoted(tmp_path, monkeypatch):
    def answer(req):
        sid = req.url.params["id"]
        return httpx.Response(200, text=DGS10) if sid == "DGS10" else httpx.Response(404, text="not found")
    m, calls = _market(tmp_path, monkeypatch, answer)

    async def go():
        obs, dec = await m.fred.series("DGS10")
        await m.fred.series("DGS10")
        assert len(calls) == 1 and obs[-1] == ("2026-10-16", 4.1)   # the second from the database
        with pytest.raises(ValueError):
            await m.fred.series("NOPE")
        with pytest.raises(ValueError):
            await m.fred.series("NOPE")
        assert len(calls) == 2   # FRED said no: not asked again for a while
        m.watch_keys = {"FRED:DGS10", "ES"}
        feed = fred.FredQuotes()
        task = asyncio.create_task(feed.run(m))
        for _ in range(50):
            if "FRED:DGS10" in m.quotes:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        q = m.quotes["FRED:DGS10"]
        assert (q["last"], q["prev"], q["src"]) == (4.1, 4.087, "fred") and abs(q["chg"] - 0.013) < 1e-9
        assert q["ts"] == day_open(date(2026, 10, 16)) + 7 * 3_600_000   # the observation's day, midnight CT
        got = await m.ext.bars("FRED:DGS10", "1D")
        assert got["source"] == "fred" and got["decimals"] == 3 and len(got["bars"]) == 4
        assert got["bars"][0]["t"] == day_open(date(2026, 10, 13)) and got["name"].startswith("10-Year Treasury")
        wk = await m.ext.bars("FRED:DGS10", "1W")
        assert [(b["o"], b["h"], b["l"], b["c"]) for b in wk["bars"]] == [(4.05, 4.11, 4.05, 4.1)]
        with pytest.raises(ValueError):
            await m.ext.bars("FRED:DGS10", "5m")
    asyncio.run(go())


def yahoo_doc(ts, closes, tz="America/New_York", hint=2):
    return {"chart": {"result": [{"meta": {"exchangeTimezoneName": tz, "priceHint": hint, "longName": "Apple Inc."},
                                  "timestamp": ts, "indicators": {"quote": [{
                                      "open": [c - 1 if c else None for c in closes], "high": [c + 2 if c else None for c in closes],
                                      "low": [c - 2 if c else None for c in closes], "close": closes,
                                      "volume": [100] * len(closes)}]}}], "error": None}}


def test_yahoo_history(tmp_path, monkeypatch):
    open_et = lambda d: int(datetime(2026, 10, d, 9, 30, tzinfo=CT).timestamp()) + 3600   # noqa: E731  (9:30 ET)
    daily = yahoo_doc([open_et(13), open_et(14), open_et(15), open_et(15) + 20_000], [230.0, None, 232.0, 233.5])
    bars, meta = ext.parse_yahoo_chart(daily, "1D")
    assert [(b["t"], b["c"]) for b in bars] == [(day_open(date(2026, 10, 13)), 230.0), (day_open(date(2026, 10, 15)), 233.5)]
    assert bars[-1]["h"] == 235.5 and bars[-1]["v"] == 200   # the live row joins its day
    intraday, _ = ext.parse_yahoo_chart(yahoo_doc([1_792_000_000, 1_792_000_300], [10.0, 11.0]), "5m")
    assert [b["t"] for b in intraday] == [1_792_000_000_000, 1_792_000_300_000]
    with pytest.raises(ValueError):
        ext.parse_yahoo_chart({"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}}, "1D")

    def answer(req):
        assert req.url.params["interval"] == "1d" and req.url.params["range"] == "20y"
        return httpx.Response(200, json=daily) if req.url.path.endswith("/AAPL") else httpx.Response(404)
    m, calls = _market(tmp_path, monkeypatch, answer)

    async def go():
        got = await m.ext.bars("AAPL", "1D")
        await m.ext.bars("AAPL", "1D")
        assert len(calls) == 1 and got["source"] == "yahoo" and got["decimals"] == 2 and got["name"] == "Apple Inc."
        assert [b["seq"] for b in got["bars"]] == [1, 2] and "1h" in got["specs"]
        with pytest.raises(ValueError):
            await m.ext.bars("ZZZZ", "1D")
        with pytest.raises(ValueError):
            await m.ext.bars("AAPL", "2000t")
    asyncio.run(go())


def test_exchange_clock():
    reg = lambda a, b: {"currentTradingPeriod": {"regular": {"start": a, "end": b}}}   # noqa: E731
    nyse = int(datetime(2026, 10, 16, 8, 30, tzinfo=CT).timestamp())   # 9:30 ET
    assert ext.exchange_of({"exchangeTimezoneName": "America/New_York", **reg(nyse, nyse + 23_400)}) == \
        {"tz": "America/New_York", "rth": ["09:30", "16:00"], "day": "00:00"}
    assert ext.exchange_of({"exchangeTimezoneName": "UTC", "instrumentType": "CRYPTOCURRENCY", **reg(nyse, nyse + 86_340)}) == \
        {"tz": "UTC", "rth": None, "day": "00:00"}   # around the clock: no regular hours
    assert ext.exchange_of({"exchangeTimezoneName": "Europe/London", "instrumentType": "CURRENCY"})["day"] == "17:00"
    assert ext.exchange_of({"exchangeTimezoneName": "America/New_York", "instrumentType": "FUTURE"}) == ext.CME
    assert ext.exchange_of({"exchangeTimezoneName": "Mars/Base"})["tz"] == "UTC"


def test_yahoo_eth_asks_for_the_pre_and_post_market(tmp_path, monkeypatch):
    asked = []

    def answer(req):
        asked.append((req.url.params["interval"], req.url.params["includePrePost"]))
        return httpx.Response(200, json=yahoo_doc([1_792_000_000, 1_792_000_300], [10.0, 11.0]))
    m, _ = _market(tmp_path, monkeypatch, answer)

    async def go():
        a = await m.ext.bars("AAPL", "5m")
        await m.ext.bars("AAPL", "5m", rth=False)
        await m.ext.bars("AAPL", "1D", rth=False)   # daily bars: no such thing
        assert asked == [("5m", "false"), ("5m", "true"), ("1d", "false")]
        assert a["exchange"]["tz"] == "America/New_York"
    asyncio.run(go())


def test_what_charts_from_free_data():
    inst = {"ES": object(), "NQ": object()}
    assert ext.chart_symbol("aapl", inst) == "AAPL" and ext.chart_symbol("fred:dgs10", inst) == "FRED:DGS10"
    assert ext.chart_symbol("ZN1!", inst) == "ZN1!" and ext.chart_symbol("CBOT:ZN1!", inst) == "ZN1!"
    for bad in ("ES1!", "AAPL+MSFT", "###SECTION", "a b c ?"):
        with pytest.raises(ValueError):
            ext.chart_symbol(bad, inst)
