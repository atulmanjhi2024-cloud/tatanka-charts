"""A stock whose ticker is also a futures root (ZM Zoom and ZM1! soybean meal, ZS Zscaler and ZS1! soybeans, HE, PL,
MET...) and the future have their own quote keys: the future's is its "XX1!", the stock's its ticker. Each feed, the
symbol database, the settlements, the details panel, the free-data charts, the plugins and the web client keep the
two apart; a chart future running here still owns its bare root ("ES"), as on the chart."""
import asyncio
import json
import shutil
import time
from types import SimpleNamespace

import httpx
import pytest

from chartsrv import watchlists as wl
from chartsrv.details import details
from chartsrv.feeds import alpaca, ibkr
from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.feeds.sdk import FeedPlugin
from chartsrv.feeds.yahoo import YahooQuotes, yahoo_symbol
from chartsrv.plugins import BUILTIN_DIR, Plugins
from test_ext import yahoo_doc
from test_hunt_quotes import IB
from test_hunt_web_ui import WEB, app_js, node, run_app_js
from test_review_fixes import build_market
from test_yahoo import spark

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

# Nasdaq Trader's rows for the stocks whose tickers are futures roots too
STOCKS = [("ZM", "Zoom Communications", "stock", "NASDAQ", "", 0), ("ZS", "Zscaler", "stock", "NASDAQ", "", 0),
          ("HE", "Hawaiian Electric Industries", "stock", "NYSE", "", 0), ("MET", "MetLife", "stock", "NYSE", "", 0)]
BOTH = ["ZM", "ZM1!", "ZS", "ZS1!", "HE", "PL1!", "MET"]


def market(tmp_path, monkeypatch, items=BOTH, **kw):
    m, _ = build_market(tmp_path, monkeypatch, **kw)
    m.symbols._put(STOCKS, "nasdaq")
    m.set_watchlists({"lists": [{"name": "a", "items": list(items)}]})
    return m


def test_a_future_and_the_stock_with_its_ticker_have_their_own_quote_keys():
    doc = wl.clean({"lists": [{"name": "a", "items": ["ZM", "CBOT:ZM1!", "AAPL+ZS", "ZS1!/ZS", "ES", "FRED:DGS10"]}]})
    assert wl.quote_keys(doc) == {"ZM", "ZM1!", "AAPL", "ZS", "ZS1!", "ES", "FRED:DGS10"}
    assert wl.quote_keys(doc, {"ES": None}) == {"ZM", "ZM1!", "AAPL", "ZS", "ZS1!", "ES1!", "FRED:DGS10"}   # ES live here
    assert wl.futures_roots(doc) == ["ZM", "ZS"]   # the add-on's W line: unchanged
    assert (wl.future_root("ZS1!"), wl.future_root("ZS"), wl.future_root("FRED:DGS10")) == ("ZS", None, None)


def test_the_symbol_database_keeps_the_stock_apart_from_the_future(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    db = m.symbols
    assert (db.type_of("ZM"), db.type_of("ZM1!"), db.type_of("MET"), db.type_of("MET1!")) == ("stock", "future", "stock", "future")
    assert db.yahoo_for("ZM") is None and db.yahoo_for("ZM1!") == "ZM=F"   # the stock: Yahoo's own ZM
    assert yahoo_symbol("ZM") == "ZM" and yahoo_symbol("ZM1!") == "ZM=F" and yahoo_symbol("ZM", future=True) == "ZM=F"
    assert yahoo_symbol("FDAX1!") is None and yahoo_symbol("HSI1!") == "^HSI"   # no Yahoo twin; the index, as before


def test_yahoo_quotes_the_stock_as_the_stock_and_the_future_as_the_future(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    prices = {"ZM": (80.0, 79.0, 1e6), "ZM=F": (300.0, 299.0, 5e4), "ZS": (200.0, 195.0, 1e6), "ZS=F": (1050.0, 1040.0, 1e5),
              "HE": (11.0, 10.5, 1e6), "PL=F": (980.0, 975.0, 1e4), "MET": (78.0, 77.0, 1e6)}
    y = YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=tmp_path),
                    transport=httpx.MockTransport(lambda req: httpx.Response(200, json=spark(
                        {s: prices[s] for s in req.url.params["symbols"].split(",") if s in prices}))))
    want = y.wanted(m)
    assert {t: want[t] for t in prices} == {"ZM": ["ZM"], "ZM=F": ["ZM1!"], "ZS": ["ZS"], "ZS=F": ["ZS1!"], "HE": ["HE"],
                                            "PL=F": ["PL1!"], "MET": ["MET"]}

    async def go():
        async with httpx.AsyncClient(transport=y.transport) as http:
            return await y.poll(http, m)
    asyncio.run(go())
    assert {k: m.quotes[k]["last"] for k in BOTH} == {"ZM": 80.0, "ZM1!": 300.0, "ZS": 200.0, "ZS1!": 1050.0, "HE": 11.0,
                                                     "PL1!": 980.0, "MET": 78.0}


def test_ibkr_quotes_the_stock_and_the_future_each(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    assert ibkr.contract_for("ZM")["secType"] == "STK" and ibkr.contract_for("ZM1!") == {"secType": "CONTFUT", "symbol": "ZM", "exchange": "CBOT"}
    assert ibkr.contract_for("6E1!")["tradingClass"] == "6E" and ibkr.contract_for("KC1!") is None   # IBKR's coffee: not placed
    feed, subs = ibkr.IBKRQuotes(None), {}
    asyncio.run(feed._sync(IB(), m, subs, {}))
    kinds = {k: feed.contracts[k].secType for k in subs}
    assert {k: kinds[k] for k in BOTH} == {"ZM": "STK", "ZM1!": "FUT", "ZS": "STK", "ZS1!": "FUT", "HE": "STK", "PL1!": "FUT", "MET": "STK"}


def test_alpaca_and_finnhub_quote_the_stocks_with_a_futures_root_for_a_ticker(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    assert {"ZM", "ZS", "HE", "MET"} <= set(alpaca.stock_keys(m)) and not {"ZM1!", "ZS1!", "PL1!"} & set(alpaca.stock_keys(m))
    assert alpaca.is_stock(m, "MET") and not alpaca.is_stock(m, "MET1!")
    finnhub = Plugins([BUILTIN_DIR], set()).types["finnhub"]("finnhub", {"api_key": "k"}, [])
    assert {"ZM", "ZS", "HE", "MET"} <= set(finnhub.wanted(m)) and not {"ZM1!", "ZS1!"} & set(finnhub.wanted(m))


def test_the_add_ons_quote_is_the_futures_never_the_stocks(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    feed = NinjaTraderFeed("127.0.0.1", 0, "")
    feed._line(m, b"Q|ZM|300.5|299.0|1200\n")   # the add-on quotes futures only, by root
    feed._line(m, b"Q|ES|6900.25|6880.0\n")      # a chart future's too
    assert m.quotes["ZM1!"]["last"] == 300.5 and "ZM" not in m.quotes
    assert m.quotes["ES1!"]["last"] == 6900.25 and "ES" not in m.quotes and "ES1!" in m.watch_keys


def test_a_stocks_change_is_never_from_the_futures_settlement(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    asked = []

    def reference(root, now=None):
        asked.append(root)
        return {"price": 1040.0, "t": 1, "src": "feed"} if root == "ZS" else None
    monkeypatch.setattr(m.settlements, "reference", reference)
    m.on_quote("ZS", 200.0, 195.0, None, source="alpaca")   # Zscaler
    m.on_quote("ZS1!", 1050.0, 1045.0, None, source="ninjatrader")   # soybeans
    m.on_quote("ES", 6900.0, 6880.0)   # a chart future by its chart symbol: its future's
    assert (m.quotes["ZS"]["prev"], "settle_t" in m.quotes["ZS"]) == (195.0, False)
    assert (m.quotes["ZS1!"]["prev"], m.quotes["ZS1!"]["settle_t"]) == (1040.0, 1)
    assert asked == ["ZS", "ES"] and "ES1!" in m.quotes and "ES" not in m.quotes


def test_details_and_charts_ask_yahoo_for_the_stock_or_the_future(tmp_path, monkeypatch):
    asked = []

    def answer(req):
        asked.append(req.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=yahoo_doc([1_792_000_000], [80.0]))
    m = market(tmp_path, monkeypatch, items=["AAPL"], http=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    assert m.ext.yahoo_ticker("ZM") == "ZM" and m.ext.yahoo_ticker("ZM1!") == "ZM=F" and m.ext.yahoo_ticker("MET") == "MET"
    now = int(time.time() * 1000)
    asyncio.run(details(m, "ZM", now))
    asyncio.run(details(m, "ZS1!", now))
    asyncio.run(m.ext.bars("HE", "1D"))   # Hawaiian Electric, charted from the search
    asyncio.run(m.ext.bars("PL1!", "1D"))
    assert asked == ["ZM", "ZS=F", "HE", "PL=F"]
    assert {"HE", "PL1!"} <= m.watch_keys and not {"HE1!", "PL"} & m.watch_keys   # quoted while charted, each its own
    asyncio.run(m.ext.bars("HE1!", "1D"))   # the hogs too, in another pane: both quoted
    assert {"HE", "HE1!"} <= m.watch_keys


def test_plugins_quote_the_future_by_its_root_or_its_front_month(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch, items=["NG1!", "ZS", "ZS1!"])

    class Futures(FeedPlugin):
        KIND = "futures"

    class Quotes(FeedPlugin):
        KIND = "quotes"
    fut, q = Futures("lines", {}, []), Quotes("myquotes", {}, ["ZS", "NG"])
    assert q.wanted(m) == ["NG1!", "ZS", "ZS1!"]   # a root in its Symbols field covers its future, as before
    assert fut.quote(m, "NG", 3.10, 3.00)          # a futures feed's quote: the future's
    assert m.quotes["NG1!"]["last"] == 3.10 and "NG" not in m.quotes
    assert q.quote(m, "ZS1!", 1050.0, 1040.0) and q.quote(m, "ZS", 200.0, 195.0)
    assert (m.quotes["ZS1!"]["last"], m.quotes["ZS"]["last"]) == (1050.0, 200.0)
    m.quote_from.pop("NG1!")
    assert q.quote(m, "NG", 3.20, 3.00)            # an older plugin's root, with only the future listed: the future's
    assert m.quotes["NG1!"]["last"] == 3.20 and "NG" not in m.quotes


def test_a_bare_root_that_cannot_be_a_ticker_is_written_as_its_future():
    """An older list's 6E or SR3 (the future: no stock has that ticker) keeps its quote and chart; ZN stays ZN (a stock
    ticker too: TradingView's Zion Oil)."""
    doc = wl.clean({"lists": [{"name": "a", "items": ["6E", "CME:SR3", "6E/6B", "M2K1!", "ZN", "FRED:DGS10"]}],
                    "flags": {"6E": "red", "zn": "blue"}, "names": {"6E": "Euro FX", "SR3": "SOFR"}})
    assert doc["lists"][0]["items"] == ["6E1!", "CME:SR31!", "6E1!/6B1!", "M2K1!", "ZN", "FRED:DGS10"]
    assert doc["flags"] == {"6E1!": "red", "ZN": "blue"} and doc["names"] == {"6E1!": "Euro FX", "SR31!": "SOFR"}
    assert wl.clean(doc) == doc


@needs_node
def test_the_watchlist_shows_each_its_own_quote():
    out = node("""
const q = (s, last, prev) => ({ s, last, prev, chg: last - prev, pct: (last - prev) / prev * 100, ts: 1, rt: 'delayed', src: 'yahoo' });
const quotes = { ZM: q('ZM', 80, 79), 'ZM1!': q('ZM1!', 300, 299), ZS: q('ZS', 200, 195), 'ES1!': q('ES1!', 6900, 6880) };
const w = makeWatchlist(WEB, { quotes });
w.list.setDoc({ lists: [{ id: 'main', name: 'Main', items: ['ZM', 'ZM1!', 'ZS+ZM', 'ES1!', 'ES'] }], flags: {}, names: {} });
out(w.root.querySelector('.wl-body').children.map((r) => [r.querySelector('.tk').textContent, r.querySelector('.last').textContent]));""")
    assert out == [["ZM", "80.00"], ["ZM1!", "300.00"], ["ZS+ZM", "280.00"], ["ES1!", "6,900.00"], ["ES", "6,900.00"]]


@needs_node
def test_the_chart_header_shows_its_own_symbols_change():
    """A free-data chart of ZM1! (soybean meal) takes its change from ZM1!'s quote, not Zoom's; the live ES chart from
    ES1!'s, the key its quote has now."""
    wlsrc = (WEB / "watchlist.js").read_text().split("function createWatchlist")[0]
    out = run_app_js(app_js("function symQuote(", "function header("), f"""
const stubs = 'var els = {{}}, mk = () => ({{ textContent: "", className: "", querySelector(s) {{ return (this[s] ||= mk()); }} }});'
  + 'var $ = (s) => (els[s] ||= mk()), px = (v) => (v == null ? "" : v.toFixed(2)), S = {{}};';
vm.runInContext({json.dumps(wlsrc)} + '\\n' + stubs + '\\n' + /*APP*/
  + '\\nthis.header = header; this.S = S; this.$ = $;', ctx);
const shown = (S) => {{ Object.assign(ctx.S, S); ctx.header(); return ctx.$('#q-chg').querySelector('.abs').textContent; }};
const quotes = {{ ZM: {{ prev: 79, src: 'yahoo' }}, 'ZM1!': {{ prev: 290, src: 'yahoo' }}, 'ES1!': {{ prev: 6880, src: 'ninjatrader' }} }};
out([shown({{ symbol: 'ZM1!', ext: {{ source: 'yahoo' }}, instruments: {{ ES: {{}} }}, quotes, lastBar: {{ c: 300 }} }}),
     shown({{ symbol: 'ZM', lastBar: {{ c: 80 }} }}),
     shown({{ symbol: 'ES', ext: null, lastBar: {{ c: 6900 }} }})]);""")
    assert out == ["+10.00", "+1.00", "+20.00"]
