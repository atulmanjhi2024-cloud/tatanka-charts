"""The symbol database: built-in symbols, Nasdaq Trader's listings, Coinbase, Yahoo's and FRED's searches, ranking."""
import asyncio

import httpx
import pytest

from chartsrv import symbols as sy
from chartsrv import watchlists as wl
from chartsrv.config import load_instruments
from chartsrv.store import Store
from test_databento import CONFIG
from test_review_fixes import build_market

NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N
ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N
File Creation Time: 1017202517:02|||||||
"""
OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. New Common Stock|N|BRK.B|N|100|N|BRK.B
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
ABR$D|Arbor Realty Trust 6.375% Series D Preferred|N|ABRpD|N|100|N|ABR-D
File Creation Time: 1017202517:02|||||||
"""


def db(tmp_path, http=None, fred_key=None, online=False):
    return sy.SymbolDB(Store(tmp_path / "c.db"), load_instruments(CONFIG), http, fred_key, online)


def test_builtin_symbols_rank_and_match(tmp_path):
    d = db(tmp_path)
    first = lambda q, k="": d.query(q, k)[0]["symbol"]   # noqa: E731
    assert first("ES") == "ES1!" and d.query("ES")[0]["live"] and not d.query("ZN")[0]["live"]
    assert first("eurusd") == "EURUSD" and first("10-year treasury", "economic").startswith("FRED:DGS10")
    assert first("FRED:DGS1") == "FRED:DGS1" and first("dgs10") == "FRED:DGS10"
    assert "CL1!" in [r["symbol"] for r in d.query("crude oil")]
    assert {r["type"] for r in d.query("", "forex")} == {"forex"} and d.query("", "economic", 5)[0]["type"] == "economic"
    assert first("bitcoin", "crypto") == "BTCUSD" and first("spx") == "SPX"
    with pytest.raises(ValueError):
        d.query("x", "bonds")
    assert d.yahoo_for("SPX") == "^GSPC" and d.yahoo_for("SOLUSD") == "SOL-USD" and d.yahoo_for("KE1!") == "KE=F"
    assert d.yahoo_for("FDAX1!") is None   # no Yahoo twin: never another instrument's price


def test_nasdaq_trader_listings(tmp_path):
    rows = sy.parse_listing(NASDAQ) + sy.parse_listing(OTHER)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("AAPL", "Apple Inc.", "stock", "NASDAQ"), ("QQQ", "Invesco QQQ Trust, Series 1", "etf", "NASDAQ"),
        ("BRK.B", "Berkshire Hathaway Inc. New", "stock", "NYSE"), ("SPY", "SPDR S&P 500 ETF Trust", "etf", "NYSE Arca")]
    calls = []

    def answer(req):
        calls.append(req.url.path)
        return httpx.Response(200, text=NASDAQ if "nasdaqlisted" in req.url.path else OTHER)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            d = db(tmp_path, http, online=True)
            with pytest.raises(ValueError):   # a few rows only: a broken file never wipes the stored ones
                await d.refresh_listings()
            sy_rows = [(f"S{chr(65 + i % 26)}{chr(65 + i // 26 % 26)}{chr(65 + i // 676)}", f"Co {i}", "stock", "NASDAQ", "", 0)
                       for i in range(3100)]
            d._put(sy_rows, "nasdaq", replace=True)
            assert d.query("SAAA")[0]["symbol"] == "SAAA" and d.count()["stock"] >= 3100
            d._put(sy_rows[:5], "nasdaq", replace=True)   # delisted ones leave with the next day's file
            assert d.count()["stock"] == 5 and d.query("ES")[0]["symbol"] == "ES1!"   # built-ins untouched
    asyncio.run(go())
    assert calls[:2] == ["/dynamic/SymDir/nasdaqlisted.txt", "/dynamic/SymDir/otherlisted.txt"]


def test_coinbase_and_yahoo_and_fred_answers():
    cb = sy.parse_coinbase([{"base_currency": "PEPE", "quote_currency": "USD", "status": "online"},
                            {"base_currency": "USDT", "quote_currency": "USD", "status": "online"},
                            {"base_currency": "XYZ", "quote_currency": "USD", "status": "delisted"},
                            {"base_currency": "ETH", "quote_currency": "EUR", "status": "online"}],
                           [{"id": "PEPE", "name": "Pepe"}])
    assert cb == [("PEPEUSD", "Pepe / U.S. Dollar", "crypto", "Coinbase", "PEPE-USD", 5)]
    q = lambda s, t, n="X": {"symbol": s, "quoteType": t, "shortname": n, "exchDisp": "EX"}   # noqa: E731
    got = sy.parse_yahoo_search({"quotes": [q("AAPL", "EQUITY", "Apple"), q("BRK-B", "EQUITY"), q("SIE.DE", "EQUITY", "Siemens"),
                                            q("^GSPC", "INDEX"), q("^FTMC", "INDEX"), q("EURUSD=X", "CURRENCY"), q("JPY=X", "CURRENCY"),
                                            q("BTC-USD", "CRYPTOCURRENCY"), q("ES=F", "FUTURE"), q("VFIAX", "MUTUALFUND"),
                                            q("AAPL250117C00150000", "OPTION"), {"news": 1}]})
    assert [(r[0], r[2], r[4]) for r in got] == [
        ("AAPL", "stock", "AAPL"), ("BRK.B", "stock", "BRK-B"), ("SIE.DE", "stock", "SIE.DE"), ("SPX", "index", "^GSPC"),
        ("^FTMC", "index", "^FTMC"), ("EURUSD", "forex", "EURUSD=X"), ("USDJPY", "forex", "JPY=X"),
        ("BTCUSD", "crypto", "BTC-USD"), ("ES1!", "future", "ES=F"), ("VFIAX", "fund", "VFIAX")]
    fr = sy.parse_fred_search({"seriess": [{"id": "CPILFESL", "title": "Core CPI", "frequency_short": "M", "popularity": 80},
                                           {"id": "bad id!", "title": "x"}]})
    assert fr == [("FRED:CPILFESL", "Core CPI", "economic", "FRED · Monthly", "", 16)]


def test_search_asks_yahoo_and_fred_once_and_keeps_the_answer(tmp_path):
    asked = []

    def answer(req):
        asked.append(req.url.host)
        if req.url.host == "api.stlouisfed.org":
            assert req.url.params["api_key"] == "k"
            return httpx.Response(200, json={"seriess": [{"id": "SIEMENSX", "title": "Siemens orders index", "frequency_short": "M"}]})
        return httpx.Response(200, json={"quotes": [{"symbol": "SIE.DE", "quoteType": "EQUITY", "longname": "Siemens AG",
                                                     "exchDisp": "XETRA"}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            d = db(tmp_path, http, fred_key="k", online=True)
            got = await d.search("siemens")
            assert {r["symbol"] for r in got} == {"SIE.DE", "FRED:SIEMENSX"}
            assert d.yahoo_for("SIE.DE") == "SIE.DE"
            n = len(asked)
            await d.search("siemens")
            assert len(asked) == n   # asked once a day
            assert [r["symbol"] for r in await d.search("siemens", "stock")] == ["SIE.DE"]
    asyncio.run(go())
    assert sorted(asked) == ["api.stlouisfed.org", "query2.finance.yahoo.com"]


def test_watchlists_keep_fred_symbols():
    doc = wl.clean({"lists": [{"id": "a", "name": "A", "items": ["fred:dgs10", "FRED:DGS10-FRED:DGS2", "CME_MINI:ES1!"]}]})
    assert doc["lists"][0]["items"][:2] == ["FRED:DGS10", "FRED:DGS10-FRED:DGS2"]
    assert wl.quote_keys(doc) == {"FRED:DGS10", "FRED:DGS2", "ES1!"} and wl.bare("CME_MINI:ES1!") == "ES1!"


def test_yahoo_quotes_use_the_database_tickers(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from chartsrv.feeds.yahoo import YahooQuotes
    m, _ = build_market(tmp_path, monkeypatch)
    m.symbols._put([("SIE.DE", "Siemens AG", "stock", "XETRA", "SIE.DE", 1)], "yahoo")
    m.watch_keys = {"SIE.DE", "BRK.B", "FRED:DGS10", "SOLUSD"}
    m.wl = {"lists": []}
    y = YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=tmp_path))
    assert y.wanted(m) == {"SIE.DE": ["SIE.DE"], "BRK-B": ["BRK.B"], "SOL-USD": ["SOLUSD"]}   # FRED: not Yahoo's


# ---- the connected feeds' symbols ------------------------------------------------------------------------------------
def test_alpaca_assets_paper_then_live():
    from chartsrv.feeds import alpaca
    items = [{"class": "us_equity", "status": "active", "symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ"},
             {"class": "us_equity", "status": "active", "symbol": "NSRGY", "name": "Nestle SA ADR", "exchange": "OTC"},
             {"class": "us_equity", "status": "inactive", "symbol": "OLD", "name": "Gone", "exchange": "NYSE"},
             {"class": "crypto", "status": "active", "symbol": "BTC/USD", "name": "Bitcoin", "exchange": "CRYPTO"},
             {"class": "us_equity", "status": "active", "symbol": "ABR$D", "name": "Preferred", "exchange": "NYSE"}]
    hosts = []

    def answer(req):
        hosts.append(req.url.host)
        assert req.headers["APCA-API-KEY-ID"] == "PKID" and req.url.params["status"] == "active"
        return httpx.Response(403) if req.url.host.startswith("paper") else httpx.Response(200, json=items)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            return await alpaca.assets(http, "PKID", "SECRET")
    rows = asyncio.run(go())
    assert hosts == ["paper-api.alpaca.markets", "api.alpaca.markets"]   # live keys: the paper endpoint refuses them
    assert rows == [("AAPL", "Apple Inc.", "stock", "NASDAQ", "", 0), ("NSRGY", "Nestle SA ADR", "stock", "OTC", "", -1)]


def test_feeds_add_their_symbols_and_take_them_away(tmp_path):
    d = db(tmp_path)
    d._put([("AAPL", "Apple Inc.", "stock", "NASDAQ", "", 0)], "nasdaq", replace=True)
    calls = []

    async def otc():
        calls.append(1)
        return [("NSRGY", "Nestle SA ADR", "stock", "OTC", "", -1), ("AAPL", "Apple (Alpaca's name)", "stock", "NASDAQ", "", 0)]

    async def broken():
        raise ValueError("Finnhub refused the key")
    d.feed_sources = lambda: {"alpaca": otc, "plugin:finnhub": broken}

    async def once():
        task = asyncio.create_task(d.run())
        await asyncio.sleep(0.05)
        task.cancel()
    asyncio.run(once())
    assert d.query("nestle")[0]["symbol"] == "NSRGY" and d.info("AAPL")["name"] == "Apple Inc."   # Nasdaq's row stays
    st = {s["source"]: s for s in d.status()}
    assert st["alpaca"]["rows"] == 1 and st["alpaca"]["refreshed"] and st["plugin:finnhub"]["error"] == "Finnhub refused the key"
    asyncio.run(once())
    assert calls == [1]   # refreshed daily, not on every pass
    d.refresh_soon({"alpaca"})
    asyncio.run(once())
    assert calls == [1, 1]
    d.feed_sources = lambda: {}   # Alpaca's keys removed: its symbols go, the others stay
    asyncio.run(once())
    assert d.info("NSRGY") is None and d.info("AAPL") and "alpaca" not in {s["source"] for s in d.status()}
    assert d.query("ES")[0]["symbol"] == "ES1!"
