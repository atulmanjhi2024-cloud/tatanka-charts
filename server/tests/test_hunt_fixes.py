"""Fixes from the bug hunt of Oct 21: plugin gap fill, contract lines, data plugin outages, plugin constructors, the
symbol-list refresh, the Instruments version, CSV links and Finnhub's resubscribes."""
import asyncio
import time
import types
from dataclasses import replace

import httpx
import pytest

from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.feeds.sdk import FeedPlugin
from chartsrv.models import Tick
from chartsrv.plugins import BUILTIN_DIR, Plugins, PluginSeries

from test_feed_plugins import _card, _client, _wait
from test_review_fixes import build_market


def test_a_plugins_gap_fill_opens_before_its_live_trades(tmp_path, monkeypatch):
    """Trades flow the moment it connects; the hour it was down still fills, for every one of its futures."""
    m, _ = build_market(tmp_path, monkeypatch)
    now = int(time.time() * 1000)
    hour_ago = now - 3_600_000
    m.store.write([Tick("ES", hour_ago, 7000.0, 1, 1), Tick("NQ", hour_ago, 25000.0, 1, 1)], [])
    asked = []

    class Hist(FeedPlugin):
        async def run(self, market):
            self.connected()
            for s in self.symbols:   # live at once, before the fill has started
                self.trade(market, s, int(time.time() * 1000), 7010.0 if s == "ES" else 25010.0, 1, 1)
            await asyncio.sleep(1.5)

        async def history(self, symbol, start, end):
            asked.append((symbol, start, end))
            await asyncio.sleep(0.2)   # a broker takes its time
            yield ("trade", start + 1_800_000, 7005.0 if symbol == "ES" else 25005.0, 2)

    feed = Hist("hist", {}, ["ES", "NQ"])
    m.trade_owner = {"ES": "hist", "NQ": "hist"}
    with pytest.raises(ConnectionError):   # run() ends after 1.5 s: its connection ended
        asyncio.run(feed.serve(m))
    gaps = asked[:2]   # then the holes its answer left (the minutes with no trade), each asked once
    assert [a[0] for a in gaps] == ["ES", "NQ"] and all(a[1] == hour_ago for a in gaps)
    m.flush()
    for sym in ("ES", "NQ"):
        assert m.store.db.execute("SELECT COUNT(*) FROM ticks WHERE symbol=? AND ts=?", (sym, hour_ago + 1_800_000)).fetchone()[0] == 1
        assert m.gapfill.health()[sym]["state"] == "done" and m.gapfill.health()[sym]["stored"] == 1
    assert not m.gapfill.windows


def test_a_plugin_stopped_before_its_history_closes_its_windows(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    m.store.write([Tick("ES", int(time.time() * 1000) - 3_600_000, 7000.0, 1, 1)], [])

    class Down(FeedPlugin):
        async def run(self, market):
            raise ConnectionError("gateway not running")

    m.trade_owner = {"ES": "down"}
    with pytest.raises(ConnectionError):
        asyncio.run(Down("down", {}, ["ES"]).serve(m))
    assert "ES" not in m.gapfill.windows and m.gapfill.health()["ES"]["note"] == "Feed stopped before its history"


def test_ninjatrader_contract_lines_skip_a_plugins_futures(tmp_path, monkeypatch):
    m, notifier = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    m.contracts.update({"ES": "ES 12-26", "NQ": "NQ 12-26"})
    m.trade_owner = {"ES": "databento"}   # its contract is the one charted
    feed = NinjaTraderFeed("127.0.0.1", 0, "")

    async def go():
        feed._line(m, b"C|ES|ES 03-27\n")
        feed._line(m, b"C|NQ|NQ 03-27\n")
    asyncio.run(go())
    assert m.contracts["ES"] == "ES 12-26" and m.contracts["NQ"] == "NQ 03-27"


def test_a_data_plugins_outage_serves_the_stored_copy(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    answer = {"do": lambda: ([("2026-10-01", 1.5), ("2026-10-02", 1.75)], 2)}

    class Mine(FeedPlugin):
        TITLE, KIND = "Mine", "data"

        async def series(self, http, code):
            return answer["do"]()

    ds = PluginSeries(m.store, object(), Mine("mine", {}, []))
    assert asyncio.run(ds.series("MINE"))[0][-1] == ("2026-10-02", 1.75)
    for err in (httpx.ConnectError("down"), RuntimeError("plugin bug")):   # the source down, or the plugin's own error
        def fail(err=err):
            raise err
        answer["do"] = fail
        assert asyncio.run(ds.series("MINE", max_age_s=0))[0][-1] == ("2026-10-02", 1.75)
        assert "MINE" not in ds.failed
    answer["do"] = lambda: ([("2026-10-05", 2.0)], 1)   # back: read again at once
    assert asyncio.run(ds.series("MINE", max_age_s=0)) == ([("2026-10-05", 2.0)], 1)


def test_a_csv_link_down_for_now_is_not_missing_and_a_big_one_stops_early():
    csv = Plugins([BUILTIN_DIR], set()).types["csv_data"]
    read = []

    async def endless():
        for _ in range(100):
            read.append(1)
            yield b"2026-10-01,1\n" * 10_000   # 130 KB a chunk, no length said

    def answer(req):
        return {"/down.csv": httpx.Response(503), "/gone.csv": httpx.Response(410),
                "/big.csv": httpx.Response(200, content=endless())}[req.url.path]

    async def go(path):
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            return await csv.read(http, f"https://sheets.example{path}", BUILTIN_DIR)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(go("/down.csv"))   # the stored copy meanwhile, asked again next time
    with pytest.raises(ValueError, match="HTTP 410"):
        asyncio.run(go("/gone.csv"))
    with pytest.raises(ValueError, match="over 5 MB"):
        asyncio.run(go("/big.csv"))
    assert len(read) < 45   # stopped at the limit, not at the end of the file


def test_a_plugin_that_fails_as_it_is_made_doesnt_stop_the_server(tmp_path, monkeypatch):
    d = tmp_path / "feeds"
    d.mkdir()
    (d / "picky.py").write_text(
        "import asyncio\nfrom chartsrv.feeds.sdk import FeedPlugin, text\n"
        "class Picky(FeedPlugin):\n TITLE = 'Picky'\n KIND = 'quotes'\n FIELDS = [text('port', 'Port')]\n"
        " def __init__(self, *a):\n  super().__init__(*a)\n  self.port = int(self.values['port'])\n"
        " async def run(self, market):\n  self.connected()\n  await asyncio.Event().wait()\n"
        " async def listing(self, http):\n  return []\n")
    app, client = _client(tmp_path, monkeypatch, feeds_dir=d)
    with client as c:
        assert c.put("/api/connections/picky", json={"enabled": True, "port": ""}).status_code == 200
        assert _wait(lambda: "Did not start: ValueError" in _card(c, "picky")["status"])
        assert _card(c, "picky")["state"] == "error" and "plugin:picky" not in app.state.market.symbol_sources()
    _, client = _client(tmp_path, monkeypatch, feeds_dir=d)   # and it starts again with it saved
    with client as c:
        assert c.get("/api/health").status_code == 200 and "Did not start" in _card(c, "picky")["status"]
        assert c.put("/api/connections/picky", json={"port": "4001"}).status_code == 200
        assert _wait(lambda: _card(c, "picky")["state"] == "ok")


def test_a_symbol_list_that_raises_keeps_the_refresh_going(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)

    async def nulls():
        return [("ZZZT", None, "stock", "NASDAQ", "", 0)]   # an API's null description

    async def broken():
        raise RuntimeError("plugin bug")
    assert asyncio.run(m.symbols.refresh("plugin:x", nulls)) == 0 and m.symbols.errors["plugin:x"] == "TypeError"
    assert asyncio.run(m.symbols.refresh("plugin:y", broken)) == 0 and m.symbols.errors["plugin:y"] == "RuntimeError"


def test_the_instruments_version_covers_how_each_rolls(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    v = m.config_hash(saved=True)
    m.instruments["CL"] = replace(m.instruments["CL"], contract="01-27")
    assert m.config_hash(saved=True) != v   # a save from an older view is refused


def test_finnhub_resubscribes_only_when_its_symbols_change(monkeypatch):
    cls = Plugins([BUILTIN_DIR], set()).types["finnhub"]
    feed = cls("finnhub", {"api_key": "k"}, [])
    market = types.SimpleNamespace(watch_keys={"AAPL", "MSFT"})
    feed.changed, feed.keys = asyncio.Event(), {"AAPL", "MSFT"}
    feed.watch_changed(market)   # a flag, a new order: the same symbols
    assert not feed.changed.is_set()
    market.watch_keys = {"AAPL", "MSFT", "NVDA"}
    feed.watch_changed(market)
    assert feed.changed.is_set()
