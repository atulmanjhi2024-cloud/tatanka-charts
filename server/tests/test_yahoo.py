"""Yahoo Finance as the backup watchlist quote source: symbol mapping, the spark and chart answers, and the source
ranking (NinjaTrader > IBKR > Yahoo) in Market.on_quote."""
import asyncio
import json
from types import SimpleNamespace

import httpx

from chartsrv import market as mk
from chartsrv.feeds.yahoo import YahooQuotes, parse_chart, parse_spark, yahoo_symbol
from test_review_fixes import build_market


def meta(price, prev, vol=None, t=1_760_000_000):
    m = {"regularMarketPrice": price, "chartPreviousClose": prev, "regularMarketTime": t}
    if vol is not None:
        m["regularMarketVolume"] = vol
    return m


def spark(symbols: dict) -> dict:
    return {"spark": {"result": [{"symbol": s, "response": [{"meta": meta(*v)}]} for s, v in symbols.items()], "error": None}}


def test_tradingview_symbols_to_yahoo():
    want = {"ES1!": "ES=F", "SR31!": "SR3=F", "SPX": "^GSPC", "VIX": "^VIX", "DXY": "DX-Y.NYB", "US10Y": "^TNX",
            "EURUSD": "EURUSD=X", "USDJPY": "JPY=X", "USDCNH": "CNH=X", "BTCUSD": "BTC-USD", "BTCUSDT": "BTC-USD",
            "ETHUSD": "ETH-USD", "AAPL": "AAPL", "BRK.B": "BRK-B", "XLRE": "XLRE", "BRENT": "BZ=F", "KOSPI": "^KS11",
            "SKEW": "^SKEW"}
    assert {k: yahoo_symbol(k) for k in want} == want
    assert yahoo_symbol("FDAX", future=True) is None and yahoo_symbol("NCF", future=True) is None   # FDAX1!, NCF1!
    assert yahoo_symbol("FTUK", future=True) == "^FTSE"   # FTUK1!: the FTSE 100 index, no free futures quote
    assert yahoo_symbol("HSI", future=True) == "^HSI" and yahoo_symbol("NK225", future=True) == "NIY=F"
    assert yahoo_symbol("FDAX", {"FDAX": "^GDAXI"}) == "^GDAXI" and yahoo_symbol("AAPL", {"AAPL": None}) is None


def test_parse_answers():
    assert parse_spark(spark({"AAPL": (230.5, 228.0, 1000)})) == {"AAPL": (230.5, 228.0, 1000.0, 1_760_000_000_000)}
    doc = {"chart": {"result": [{"meta": {**meta(4.25, 4.2), "previousClose": 4.21}}], "error": None}}
    assert parse_chart(doc) == (4.25, 4.21, None, 1_760_000_000_000)
    assert parse_chart({"chart": {"result": None, "error": {"code": "Not Found"}}}) is None


def test_the_previous_close_is_the_day_before_the_latest_price():
    """Yahoo's own previous close can be two trading days old: the close of the daily bar before today's is taken."""
    day = 86_400
    mon, tue, wed = 1_760_918_400, 1_760_918_400 + day, 1_760_918_400 + 2 * day   # Oct 20-22 2025, 00:00 UTC
    m = {"regularMarketPrice": 7768.28, "regularMarketTime": wed + 18 * 3600, "gmtoffset": -14400,
         "chartPreviousClose": 7712.57}   # two days old
    resp = {"meta": m, "timestamp": [mon + 13.5 * 3600, tue + 13.5 * 3600, wed + 13.5 * 3600],
            "indicators": {"quote": [{"close": [7712.57, 7742.15, 7768.28]}]}}
    doc = {"spark": {"result": [{"symbol": "^GSPC", "response": [resp]}]}}
    assert parse_spark(doc)["^GSPC"][1] == 7742.15
    assert parse_chart({"chart": {"result": [resp]}})[1] == 7742.15
    resp["timestamp"], resp["indicators"]["quote"][0]["close"] = resp["timestamp"][:2], [7712.57, 7742.15]   # no bar today yet
    assert parse_spark(doc)["^GSPC"][1] == 7742.15


def test_sources_are_ranked(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    clock = [1_000_000]
    monkeypatch.setattr(mk, "now_ms", lambda: clock[0])
    m.on_quote("ES1!", 6900.0, 6850.0, 10, source="ninjatrader")
    m.on_quote("ES1!", 6890.0, 6850.0, 10, source="yahoo")            # lags: NinjaTrader keeps it
    assert m.quotes["ES1!"]["last"] == 6900.0 and m.quotes["ES1!"]["src"] == "ninjatrader"
    clock[0] += mk.QUOTE_HOLD_MS                                       # NinjaTrader went quiet
    m.on_quote("ES1!", 6890.0, 6850.0, 10, source="yahoo", at=clock[0] - 600_000)
    assert m.quotes["ES1!"]["src"] == "yahoo" and m.quotes["ES1!"]["ts"] == clock[0] - 600_000
    m.on_quote("ES1!", 6901.0, 6850.0, 10, source="ibkr")                # a better source takes it back at once
    assert m.quotes["ES1!"]["src"] == "ibkr" and not m.quote_wanted("ES1!", "yahoo") and m.quote_wanted("ES1!", "ninjatrader")


def test_spark_then_chart_fallback(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    m.wl = {"lists": [{"id": "a", "name": "A", "items": ["AAPL", "SPX", "EURUSD", "FDAX1!", "ZZZZ"]}]}
    m.watch_keys = {"AAPL", "SPX", "EURUSD", "FDAX", "ZZZZ"}
    calls = []

    def ok(req):
        calls.append(req.url.path)
        syms = req.url.params["symbols"].split(",")
        known = {"AAPL": (230.5, 228.0, 5e6), "^GSPC": (6500.0, 6480.0, None), "EURUSD=X": (1.085, 1.08, None)}
        return httpx.Response(200, json=spark({s: known[s] for s in syms if s in known}))

    y = YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=tmp_path), transport=httpx.MockTransport(ok))

    async def go(feed):
        async with httpx.AsyncClient(transport=feed.transport) as http:
            return await feed.poll(http, m)
    assert asyncio.run(go(y)) == 3
    assert m.quotes["SPX"]["last"] == 6500.0 and m.quotes["AAPL"]["vol"] == 5e6 and m.quotes["EURUSD"]["src"] == "yahoo"
    assert "FDAX" not in m.quotes and "ZZZZ" in y.unknown and calls == ["/v7/finance/spark"]
    calls.clear()
    asyncio.run(go(y))   # ZZZZ is not asked again for a while
    assert calls == ["/v7/finance/spark"]

    def refuse(req):   # spark wants a cookie and crumb now: one symbol at a time
        if req.url.path.startswith("/v7"):
            return httpx.Response(401, json={"finance": {"error": {"code": "Unauthorized"}}})
        sym = req.url.path.rsplit("/", 1)[1]
        if sym == "AAPL":
            return httpx.Response(200, json={"chart": {"result": [{"meta": meta(231.0, 228.0)}], "error": None}})
        return httpx.Response(404, json={"chart": {"result": None, "error": {"code": "Not Found"}}})
    y2 = YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=tmp_path), transport=httpx.MockTransport(refuse))
    m.watch_keys = {"AAPL", "SPX"}
    asyncio.run(go(y2))
    assert y2.stats["mode"] == "chart" and m.quotes["AAPL"]["last"] == 231.0 and "^GSPC" in y2.unknown

    def slow(req):   # one symbol times out: the rest of the round still arrives
        if req.url.path.startswith("/v7"):
            return httpx.Response(401)
        if req.url.path.endswith("EURUSD=X"):
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"chart": {"result": [{"meta": meta(232.0, 228.0)}], "error": None}})
    y3 = YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=tmp_path), transport=httpx.MockTransport(slow))
    m.watch_keys = {"AAPL", "EURUSD"}
    assert asyncio.run(go(y3)) == 1 and m.quotes["AAPL"]["last"] == 232.0


def test_config_adds_symbols(tmp_path):
    (tmp_path / "yahoo.json").write_text(json.dumps({"symbols": {"fdax": "^GDAXI", "SPCX": None}}))
    y = YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=tmp_path))
    assert y.extra == {"FDAX": "^GDAXI", "SPCX": None}
