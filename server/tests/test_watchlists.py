"""TradingView-style watchlists: validation, the quotes they need, first-run default, the API, and the
NinjaTrader add-on hearing which futures to quote."""
import asyncio
import json

import pytest

from chartsrv import watchlists as wl
from chartsrv.config import load_settings
from chartsrv.feeds.ibkr import contract_for
from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from test_review_fixes import build_market


def test_clean_sections_symbols_and_ids():
    doc = wl.clean({"lists": [{"name": " Main ", "items": ["###Futures", " es1! ", "CME_MINI:NQ1!", "aapl+msft", "SKEW/VIX"]},
                              {"id": "dup", "name": "B", "items": []}, {"id": "dup", "name": "C", "items": []}],
                    "flags": {"ES1!": "red", "NQ1!": "pink", "<x>": "red"}})
    assert doc["lists"][0]["name"] == "Main"
    assert doc["lists"][0]["items"] == ["###Futures", "ES1!", "CME_MINI:NQ1!", "AAPL+MSFT", "SKEW/VIX"]
    ids = [lst["id"] for lst in doc["lists"]]
    assert ids[1] == "dup" and ids[2] != "dup" and len(set(ids)) == 3
    assert doc["flags"] == {"ES1!": "red"}
    for bad in [{"lists": []}, {"lists": [{"name": "", "items": []}]}, {"lists": [{"name": "x", "items": ["<script>"]}]},
                {"lists": [{"name": "x", "items": ["###"]}]}, {"lists": [{"name": "x", "items": [5]}]}, "nope"]:
        with pytest.raises(ValueError):
            wl.clean(bad)


def test_quote_keys_and_futures_roots():
    doc = wl.clean({"lists": [{"name": "a", "items": ["###S", "ES1!", "CME_MINI:NQ1!", "AAPL+GOOGL*2", "SKEW/VIX", "SR31!"]}]})
    assert wl.quote_keys(doc) == {"ES1!", "NQ1!", "AAPL", "GOOGL", "SKEW", "VIX", "SR31!"}
    assert wl.futures_roots(doc) == ["ES", "NQ", "SR3"]


def test_the_default_is_a_starter_list(tmp_path):
    """config/watchlists.json (whatever starter list it holds): one list of symbols in ### sections, each section once,
    and names for its symbols."""
    doc = wl.load_default(load_settings().config_dir)
    items = doc["lists"][0]["items"]
    sections = [i[3:] for i in items if i.startswith("###")]
    assert sections == ["FUTURES", "COMMODITIES", "BONDS", "MAG 7", "SECTORS", "INDEXES", "ETF", "FOREX", "CRYPTO"]
    assert len(items) - len(sections) == 86 and doc["names"]["ES1!"] == "S&P 500 E-mini Futures"
    # the old config format still loads: a section per old list, futures as TradingView writes them
    (tmp_path / "watchlists.json").write_text(json.dumps({"lists": {"Rates": [["ZN", {"secType": "CONTFUT"}], ["TLT", {"secType": "STK"}]]}}))
    assert wl.load_default(tmp_path)["lists"][0]["items"] == ["###RATES", "ZN1!", "TLT"]


def test_market_saves_broadcasts_and_moves_quotes(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    assert {"ES1!", "AAPL", "SR31!"} <= m.watch_keys and m.hello()["watchlists"] == m.wl

    class Feed:
        heard = 0

        def watch_changed(self, market):
            self.heard += 1

    class Client:
        def __init__(self):
            self.msgs = []

        def put(self, msg):
            self.msgs.append(msg)

    f, c = Feed(), Client()
    m.feed_objs.append(f)
    m.clients.add(c)
    doc = m.set_watchlists({"lists": [{"name": "Tiny", "items": ["ZN1!", "MSFT"]}]})
    assert f.heard == 1 and c.msgs[-1] == {"type": "watchlists", "watchlists": doc}
    assert m.watch_keys == {"ZN1!", "MSFT"} | {f"{s}1!" for s in m.instruments}
    assert m.store.get_setting("watchlists") == doc
    with pytest.raises(ValueError):
        m.set_watchlists({"lists": []})


def test_api_get_put(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as client:
        doc = client.get("/api/watchlists").json()
        assert doc["lists"][0]["name"] == "Main"
        doc["lists"].append({"name": "Second", "items": ["###A", "spy"]})
        saved = client.put("/api/watchlists", json=doc).json()
        assert saved["lists"][-1]["items"] == ["###A", "SPY"] and saved["lists"][-1]["id"]
        assert client.put("/api/watchlists", json={"lists": [{"name": "x", "items": ["a<b"]}]}).status_code == 400


def test_ninjatrader_add_on_hears_the_futures_and_sends_volume(tmp_path, monkeypatch):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        feed = NinjaTraderFeed("127.0.0.1", 0, "")
        m.feed_objs.append(feed)
        server = await asyncio.start_server(lambda r, w: feed._handle(m, r, w), "127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        w.write(b"HELLO||1\nQ|ZN|112.5|112.25|1234567\n")
        await w.drain()
        first = (await asyncio.wait_for(r.readline(), 3)).decode().strip()
        while not first.startswith("W|"):   # I, R and GAP lines come first
            first = (await asyncio.wait_for(r.readline(), 3)).decode().strip()
        m.set_watchlists({"lists": [{"name": "x", "items": ["ZN1!", "6E1!", "AAPL"]}]})
        second = (await asyncio.wait_for(r.readline(), 3)).decode().strip()
        w.close()
        server.close()
        return first, second, m
    first, second, m = asyncio.run(go())
    assert first.startswith("W|") and {"ES", "NQ", "ZN", "SR3", "CL"} <= set(first[2:].split(","))
    assert second == "W|6E,ZN"
    assert m.quotes["ZN1!"]["vol"] == 1234567 and m.quotes["ZN1!"]["chg"] == 0.25


def test_ibkr_contracts():
    assert contract_for("ES1!") == {"secType": "CONTFUT", "symbol": "ES", "exchange": "CME"}
    assert contract_for("ZN1!")["exchange"] == "CBOT" and contract_for("GC1!")["exchange"] == "COMEX"
    assert contract_for("6E1!") == {"secType": "CONTFUT", "symbol": "EUR", "tradingClass": "6E", "exchange": "CME"}
    assert contract_for("EURUSD") == {"secType": "CASH", "symbol": "EUR", "currency": "USD", "exchange": "IDEALPRO"}
    assert contract_for("BTCUSD")["secType"] == "CRYPTO" and contract_for("SPX")["secType"] == "IND"
    assert contract_for("AAPL")["secType"] == "STK" and contract_for("BRK.B")["symbol"] == "BRK B"
    assert contract_for("DXY") is None and contract_for("US02Y") is None and contract_for("BTCUSDT") is None


def test_ibkr_treasury_yields_come_from_cboes_yield_indexes_in_percent():
    from chartsrv.feeds.ibkr import per_cent
    assert contract_for("US10Y") == contract_for("TNX") == {"secType": "IND", "symbol": "TNX", "exchange": "CBOE"}
    assert contract_for("US05Y")["symbol"] == "FVX" and contract_for("US30Y")["symbol"] == "TYX"
    assert per_cent("US10Y", 42.5) == 4.25 and per_cent("TNX", 4.25) == 0.425 and per_cent("US30Y", None) is None
    assert per_cent("SPX", 6000.0) == 6000.0 and per_cent("VIX", 25.0) == 25.0   # not a yield: as it comes


def test_workspaces_saved_for_every_device(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    with TestClient(app) as c:
        _workspaces(c)


def _workspaces(c):
    data = {"layout": "4", "sync": {"symbol": False}, "keys": {"sym": "NQ", "c1.spec": "15m"}}
    r = c.put("/api/workspaces/main", json={"name": "Main", "data": data, "by": "pc"})
    assert r.status_code == 200 and r.json()["name"] == "Main"
    assert c.put("/api/workspaces/main", json={"data": {**data, "layout": "2"}, "by": "phone"}).json()["by"] == "phone"
    w = c.get("/api/workspaces/main").json()
    assert w["name"] == "Main" and w["data"]["layout"] == "2"
    assert [x["id"] for x in c.get("/api/workspaces").json()["workspaces"]] == ["main"]
    assert c.put("/api/workspaces/x", json={"name": "", "data": data}).status_code == 400
    assert c.put("/api/workspaces/x", json={"name": "X", "data": {"keys": {"a": 1}}}).status_code == 400
    assert c.put("/api/workspaces/bad id", json={"name": "X", "data": data}).status_code == 400
    assert c.delete("/api/workspaces/main").status_code == 200 and c.get("/api/workspaces/main").status_code == 404
    assert c.get("/api/workspaces").json()["workspaces"] == []


def test_spot_quotes_become_their_futures_once(tmp_path):
    from chartsrv.store import Store
    store = Store(tmp_path / "c.db")
    store.set_setting("watchlists", {"lists": [{"name": "Main", "items": ["###COMMODITIES", "TVC:BRENT", "CL1!", "PALLADIUM",
                                                                           "XCUUSD", "HG1!", "NK2251!", "USDJPY", "USDJPY"]}]})
    doc = wl.load(store, tmp_path)
    assert doc["lists"][0]["items"] == ["###COMMODITIES", "BZ1!", "CL1!", "PA1!", "HG1!", "NIY1!", "USDJPY", "USDJPY"]
    assert doc["names"]["PA1!"] == "Palladium Futures"
    doc["lists"][0]["items"].append("BRENT")   # added back by hand afterwards: it stays
    store.set_setting("watchlists", doc)
    assert wl.load(store, tmp_path)["lists"][0]["items"][-1] == "BRENT"
