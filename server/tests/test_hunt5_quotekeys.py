"""Bug hunt round 5, quote keys: a bare root no stock has is its future by the symbol database's listings, a stock-only
plugin's stock is never a future's quote, a free-data chart's key leaves the watched set once its root runs live,
6-character roots, the page names a bare future-only root as the server does, and COT on a stock's chart."""
import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from chartsrv import market as market_mod
from chartsrv import watchlists as wl
from chartsrv.config import _instruments, clean_instruments
from chartsrv.details import details
from chartsrv.ext import chart_symbol
from chartsrv.feeds import alpaca
from chartsrv.feeds.plugins.finnhub import FinnhubQuotes
from chartsrv.feeds.plugins.lines import LinesQuotes
from chartsrv.feeds.yahoo import YahooQuotes
from chartsrv.models import Tick
from chartsrv.store import Store
from test_hunt_web_ui import WEB, app_js, run_app_js
from test_review_fixes import build_market

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

# Nasdaq Trader's rows for stocks whose tickers are futures roots too; ZB, ZF, ZT, UB, YM, RTY, ZC, ZW are no stock's
LISTED = [("ZS", "Zscaler", "stock", "NASDAQ", "", 0), ("NG", "NovaGold Resources", "stock", "NYSE American", "", 0),
          ("HE", "Hawaiian Electric Industries", "stock", "NYSE", "", 0), ("AAPL", "Apple", "stock", "NASDAQ", "", 0)]


def node(js: str):
    """Runs `js` after web/watchlist.js's helpers (the part before createWatchlist); returns what it passes to out()."""
    src = (WEB / "watchlist.js").read_text().split("function createWatchlist")[0]
    code = f"{src}\nconst out = (v) => console.log(JSON.stringify(v));\n{js}"
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)


def test_a_bare_root_no_listed_stock_has_is_written_as_its_future(tmp_path, monkeypatch):
    """A list's bare ZB (typed, or in an older list) is the future once the listings show no stock has that ticker:
    ZB1!'s quote and chart. ZS, NG and HE are stocks there, so they stay; without the listings only the roots that
    cannot be a ticker (6E) are."""
    m, _ = build_market(tmp_path, monkeypatch)
    assert m.set_watchlists({"lists": [{"name": "a", "items": ["ZB", "6E"]}]})["lists"][0]["items"] == ["ZB", "6E1!"]   # no listings yet
    m.symbols._put(LISTED, "nasdaq")
    items = ["ZB", "ZF", "ZT", "UB", "YM", "RTY", "ZC", "ZW", "ZS", "NG", "HE", "6E", "AAPL", "CBOT:ZB", "ZB-ZN1!"]
    doc = m.set_watchlists({"lists": [{"name": "a", "items": items}], "flags": {"zb": "red", "zs": "blue"}, "names": {"YM": "Dow"}})
    assert doc["lists"][0]["items"] == ["ZB1!", "ZF1!", "ZT1!", "UB1!", "YM1!", "RTY1!", "ZC1!", "ZW1!", "ZS", "NG", "HE",
                                        "6E1!", "AAPL", "CBOT:ZB1!", "ZB1!-ZN1!"]
    assert doc["flags"] == {"ZB1!": "red", "ZS": "blue"} and doc["names"] == {"YM1!": "Dow"}
    assert {"ZB1!", "YM1!", "ZS", "NG"} <= m.watch_keys and not {"ZB", "YM", "NG1!"} & m.watch_keys
    assert {"ZB", "YM", "RTY", "6E"} <= m.symbols.only_futures and not {"ZS", "NG", "HE"} & m.symbols.only_futures
    # typed on the chart: the same future, from Yahoo's ZB=F, not a stock "ZB" with no data
    assert chart_symbol("zb", m.instruments, m.symbols.only_futures) == "ZB1!" and m.ext.yahoo_ticker("ZB1!") == "ZB=F"
    assert chart_symbol("ZS", m.instruments, m.symbols.only_futures) == "ZS"


def test_an_older_lists_bare_roots_are_written_as_their_futures_at_start(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    m.symbols._put(LISTED, "nasdaq")   # kept in the database, as the daily refresh leaves them
    m.store.set_setting("watchlists", {"lists": [{"id": "l1", "name": "a", "items": ["ZB", "YM", "ZS", "AAPL"]}],
                                       "flags": {"YM": "red"}, "names": {}, "version": 5})
    m.store.close()
    m, _ = build_market(tmp_path, monkeypatch)   # the next start
    assert m.wl["lists"][0]["items"] == ["ZB1!", "YM1!", "ZS", "AAPL"] and m.wl["flags"] == {"YM1!": "red"}
    assert {"ZB1!", "YM1!", "ZS"} <= m.watch_keys and not {"ZB", "YM"} & m.watch_keys
    saved = Store(tmp_path / "chart.db").get_setting("watchlists")
    assert saved["lists"][0]["items"] == ["ZB1!", "YM1!", "ZS", "AAPL"] and saved["version"] == m.wl["version"]


def test_a_stock_only_plugins_stock_is_never_the_futures_quote(tmp_path, monkeypatch):
    """Finnhub asks each symbol's REST quote 1.1 s apart from the list it started with: a ZS (Zscaler) row deleted
    meanwhile, ZS1! (soybeans) still listed, gets Zscaler's price as ZS's, never as the future's. Your own quotes
    program keeps the documented rule: its ZS is the future while the lists hold ZS1! and not ZS."""
    m, _ = build_market(tmp_path, monkeypatch)
    m.symbols._put(LISTED, "nasdaq")
    m.set_watchlists({"lists": [{"name": "a", "items": ["AAPL", "ZS", "ZS1!"]}]})
    f = FinnhubQuotes("finnhub", {"api_key": "k"}, [])
    keys = f.wanted(m)
    assert keys == ["AAPL", "ZS"]

    async def rest(http, key, sym):
        if sym == "AAPL":   # the ZS row deleted, on this device or another
            m.set_watchlists({"lists": [{"name": "a", "items": ["AAPL", "ZS1!"]}]})
        return {"c": {"AAPL": 230.0, "ZS": 201.5}[sym], "pc": {"AAPL": 228.0, "ZS": 199.0}[sym], "t": 1}

    async def nosleep(_):
        return None
    monkeypatch.setattr(f, "rest", rest)
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    asyncio.run(f.closes(m, "k", keys, {}, {}))
    assert m.quotes["AAPL"]["last"] == 230.0 and "ZS1!" not in m.quotes
    LinesQuotes("lines_quotes", {}, []).line(m, ["Q", "ZS", "1050.25", "1040"])   # an older program's root: the future
    assert m.quotes["ZS1!"]["last"] == 1050.25


def test_a_free_data_charts_key_leaves_the_watched_set_once_its_root_runs_live(tmp_path, monkeypatch):
    """A MetLife (MET) or NovaGold (NG) chart polled in the last 10 minutes, then MET1! opened from the catalog or NG
    added on Connections > Instruments: the bare key is that future's now (on_quote files it as MET1!), so the stock
    feeds must stop quoting the stock under it."""
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    m.symbols._put(LISTED + [("MET", "MetLife", "stock", "NYSE", "", 0)], "nasdaq")
    m.set_watchlists({"lists": [{"name": "a", "items": ["AAPL", "MET1!", "NG1!"]}]})
    m.watch_chart("MET")   # what ext.bars does for the free-data chart
    m.watch_chart("NG")
    assert {"MET", "NG"} <= m.watch_keys
    asyncio.run(m.add_future("MET"))   # opened from the catalog: live at once
    new = clean_instruments([{"symbol": "NG", "name": "Natural Gas", "tick_size": 0.001, "point_value": 10000}])
    asyncio.run(m.add_instruments(_instruments(new)))   # Connections > Instruments > + NG > Save
    assert {"MET1!", "NG1!"} <= m.watch_keys and not {"MET", "NG"} & m.watch_keys
    y = YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=tmp_path))
    assert not {"MET", "NG"} & set(alpaca.stock_keys(m)) and not {"MET", "NG"} & set(y.wanted(m))


@needs_node
def test_an_instrument_with_a_six_character_root_keeps_its_settlement_and_details(tmp_path, monkeypatch):
    """Connections > Instruments takes roots of 1-6 characters; FBTPXX's quote is FBTPXX1!'s, still its future's: the
    change from its settlement, its contract and roll in the details. The page reads the root the same way."""
    m, _ = build_market(tmp_path, monkeypatch)
    new = clean_instruments([{"symbol": "FBTPXX", "name": "Six", "tick_size": 0.01, "point_value": 1000}])
    asyncio.run(m.add_instruments(_instruments(new)))
    asked = []
    monkeypatch.setattr(m.settlements, "reference", lambda root, now=None: asked.append(root) or {"price": 100.0, "t": 1, "src": "feed"})
    m.on_quote("FBTPXX", 101.0, 99.0)   # the TCP lines' or a plugin's root
    assert wl.future_root("FBTPXX1!") == "FBTPXX" and asked == ["FBTPXX"]
    assert (m.quotes["FBTPXX1!"]["prev"], m.quotes["FBTPXX1!"]["settle_t"]) == (100.0, 1)
    now = market_mod.now_ms()
    m.on_tick(Tick("FBTPXX", now - 1000, 101.0, 1, 1))
    d = asyncio.run(details(m, "FBTPXX", now))
    assert d["roll"] is not None and d["day"] is not None
    assert node("out([wlRoot('FBTPXX1!'), wlRoot('ES1!'), wlRoot('FBTPXXX1!')]);") == ["FBTPXX", "ES", "FBTPXXX1!"]


@needs_node
def test_the_page_names_a_bare_future_only_root_as_the_server_does(tmp_path, monkeypatch):
    """Enter in the quick switch before the search answers, or a chart symbol stored before the update: a bare M2K,
    SR3 or ZB is charted and quoted by the server as M2K1!, so the page names it so (the hello's only_futures) and
    its header and last bar look up the quote the server sends."""
    m, _ = build_market(tmp_path, monkeypatch)
    m.symbols._put(LISTED, "nasdaq")
    only = m.hello()["only_futures"]
    assert {"M2K", "SR3", "6E", "ZB", "YM"} <= set(only) and not {"ZS", "NG"} & set(only)
    assert chart_symbol("sr3", m.instruments, m.symbols.only_futures) == "SR31!"
    wlsrc = (WEB / "watchlist.js").read_text().split("function createWatchlist")[0]
    out = run_app_js(app_js("function chartSym("), f"""
vm.runInContext({json.dumps(wlsrc)} + '\\n' + /*APP*/ + '\\nthis.chartSym = chartSym;', ctx);
ctx.S = {{ instruments: {{ ES: {{}}, ZB: {{}} }}, catalog: {{ M2K: 'Micro Russell' }}, onlyFutures: new Set({json.dumps(only)}) }};
out(['m2k', 'CME:SR3', '6E', 'ZS', 'NG', 'ES', 'ES1!', 'ZB', 'M2K1!', 'AAPL'].map((s) => ctx.chartSym(s)));""")
    assert out == ["M2K1!", "SR31!", "6E1!", "ZS", "NG", "ES", "ES", "ZB", "M2K1!", "AAPL"]   # ZB: live here, its root


def test_cot_on_a_stocks_chart_is_no_futures_report(tmp_path, monkeypatch):
    """A free-data chart of ZS is Zscaler's now, HE Hawaiian Electric's: no CFTC report, not soybeans' or lean hogs'.
    ZS1! is soybeans', and a running chart future's bare root (ES) its own; one added later (ZS live) too."""
    m, _ = build_market(tmp_path, monkeypatch)
    codes = {s: m.cot.code_for(s) for s in ("ZS", "HE", "NG", "BTC", "ZS1!", "CBOT:ZN1!", "ES", "ES1!", "AAPL")}
    assert codes == {"ZS": None, "HE": None, "NG": None, "BTC": None, "ZS1!": "005602", "CBOT:ZN1!": "043602",
                     "ES": "13874A", "ES1!": "13874A", "AAPL": None}
    with pytest.raises(ValueError, match="no COT report for ZS"):
        asyncio.run(m.cot.weeks("ZS"))
    new = clean_instruments([{"symbol": "ZS", "name": "Soybeans", "tick_size": 0.25, "point_value": 50}])
    asyncio.run(m.add_instruments(_instruments(new)))
    assert m.cot.code_for("ZS") == "005602"
