"""Feed plugins: several futures and quote feeds at once, set up in Connections; each instrument's trades from one."""
import asyncio
import json
import socket
import sys
import time
import types
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from chartsrv import connections as conns
from chartsrv.config import Instrument
from chartsrv.feeds.sdk import FeedPlugin
from chartsrv.plugins import BUILTIN_DIR, Plugins


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _client(tmp_path, monkeypatch, feed="none", feeds_dir=None):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", feed)
    monkeypatch.setenv("CHART_QUOTES_FEED", "none")
    monkeypatch.setenv("CHART_SYMBOLS_ONLINE", "0")
    monkeypatch.setenv("CHART_NT_PORT", str(_port()))
    monkeypatch.setenv("CHART_FEEDS_DIR", str(feeds_dir or tmp_path / "nofeeds"))
    from chartsrv.app import app
    return app, TestClient(app)


def _card(c, cid):
    return next(x for x in c.get("/api/connections").json()["connections"] if x["id"] == cid)


def _wait(cond, timeout=5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_discovery_and_its_mistakes(tmp_path):
    d = tmp_path / "feeds"
    d.mkdir()
    (d / "good.py").write_text("from chartsrv.feeds.sdk import FeedPlugin, secret\n"
                               "class Good(FeedPlugin):\n TITLE = 'My broker'\n FIELDS = [secret('key', 'Key')]\n"
                               " async def run(self, market):\n  pass\n")
    (d / "broken.py").write_text("def (:\n")
    (d / "norun.py").write_text("from chartsrv.feeds.sdk import FeedPlugin\nclass X(FeedPlugin):\n TITLE = 'x'\n")
    (d / "taken.py").write_text("from chartsrv.feeds.sdk import FeedPlugin\nclass X(FeedPlugin):\n ID = 'discord'\n"
                               " async def run(self, m):\n  pass\n")
    (d / "_skipped.py").write_text("raise SystemExit\n")
    p = Plugins([BUILTIN_DIR, d], conns.builtin_ids())
    assert sorted(p.types) == ["csv_data", "databento_live", "finnhub", "good", "ibkr_trades", "lines", "lines_quotes"]
    errs = {e["file"]: e["error"] for e in p.errors}
    assert set(errs) == {"broken.py", "norun.py", "taken.py"} and "SyntaxError" in errs["broken.py"]
    assert "run()" in errs["norun.py"] and "taken" in errs["taken.py"]
    conns.register_plugins(p)
    try:
        good = conns.BY_ID["good"]
        assert good["group"] == "Futures feed" and good["multi"] == "Another My broker"
        assert [f["key"] for f in good["fields"]] == ["enabled", "key", "symbols"]
        assert conns.BY_ID["plugin_errors"]["about"].startswith("broken.py: SyntaxError")
        assert conns.type_of("good-2") == "good" and conns.type_of("ibkr-2") == "ibkr-2"   # IBKR quotes: only one
    finally:
        conns.register_plugins(Plugins([BUILTIN_DIR], conns.builtin_ids()))


def test_your_own_program_feeds_es_beside_ninjatrader(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch, feed="ninjatrader")
    port = _port()
    with client as c:
        m = app.state.market
        card = _card(c, "lines")
        assert not card["listed"] and card["state"] == "off" and card["group"] == "Futures feed"
        for bad, why in [({"symbols": "ZZ"}, "not in Connections > App > Instruments"), ({"symbols": "E S"}, "not a symbol"),
                         ({"port": 80}, "1024"), ({"nope": 1}, "no setting")]:
            r = c.put("/api/connections/lines", json=bad)
            assert r.status_code == 400 and why in r.json()["detail"], (bad, r.json())
        r = c.put("/api/connections/lines", json={"enabled": True, "port": port, "token": "s3cret-token", "symbols": "es"})
        assert r.status_code == 200, r.text
        assert "s3cret-token" not in r.text and r.json()["fields"][2]["set"] and r.json()["listed"]
        assert m.trade_owner == {"ES": "lines"}
        assert _wait(lambda: _card(c, "lines")["status"].startswith("Listening on port"))
        assert "ES from other feeds" in _card(c, "ninjatrader")["info"]

        # a second account of the same plugin may not take ES too
        r = c.post("/api/connections", json={"type": "lines", "enabled": True, "port": _port(), "symbols": "ES, CL"})
        assert r.status_code == 400 and "ES already comes from" in r.json()["detail"]

        now = int(time.time() * 1000)
        with socket.create_connection(("127.0.0.1", port)) as bad:   # a wrong token is refused
            bad.sendall(b"HELLO|nope|1\nT|ES|%d|6000.25|1|1\n" % now)
            time.sleep(0.3)
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.sendall(b"HELLO|s3cret-token|1\n" + b"".join(
                (f"T|ES|{now + i}|6850.{25 * (i % 4):02d}|2|1\n").encode() for i in range(3)) +
                f"T|CL|{now}|65.1|1|1\nT|ES|{now}|-1|1|1\nC|ES|ES 12-26\n".encode())
            assert _wait(lambda: _card(c, "lines")["status"].startswith("Connected · 3 trades"))
        assert m.last_tick["ES"].price == 6850.5 and "CL" not in m.last_tick   # CL: NinjaTrader's, not this program's
        assert m.contracts.get("ES") == "ES 12-26"

        # NinjaTrader's ES trades are not used while the program supplies ES
        feed = next(f for f in m.feed_objs if f.name == "ninjatrader")
        feed._line(m, f"T|ES|{now + 10}|7000|1|1\n".encode())
        assert m.last_tick["ES"].price == 6850.5

        # off: ES goes back to NinjaTrader
        assert c.put("/api/connections/lines", json={"enabled": False}).json()["state"] == "off"
        assert m.trade_owner == {} and not any(f.name == "lines" for f in m.feed_objs)
        assert c.delete("/api/connections/lines").status_code == 204 and not _card(c, "lines")["listed"]


def test_plugins_never_beside_the_simulator(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch, feed="sim")
    with client as c:
        c.put("/api/connections/lines", json={"enabled": True, "port": _port(), "symbols": "ES"})
        card = _card(c, "lines")
        assert card["status"] == "Simulator running" and card["locked"] and app.state.market.trade_owner == {}
        assert not any(f.name == "lines" for f in app.state.market.feed_objs)


def test_errors_shown_without_keys(tmp_path, monkeypatch):
    d = tmp_path / "feeds"
    d.mkdir()
    (d / "leaky.py").write_text(
        "from chartsrv.feeds.sdk import FeedPlugin, secret\n"
        "class Leaky(FeedPlugin):\n TITLE = 'Leaky'\n FIELDS = [secret('key', 'Key')]\n"
        " async def run(self, market):\n  raise ValueError('bad key ' + self.values['key'] + ' at https://x/?k=' + self.values['key'])\n"
        " @classmethod\n async def test(cls, values):\n  return False, 'refused ' + values['key']\n")
    app, client = _client(tmp_path, monkeypatch, feed="ninjatrader", feeds_dir=d)
    with client as c:
        r = c.put("/api/connections/leaky", json={"enabled": True, "key": "TOPSECRETKEY", "symbols": "NQ"})
        assert r.status_code == 200
        assert _wait(lambda: _card(c, "leaky")["state"] == "error")
        text = json.dumps(c.get("/api/connections").json())
        assert "TOPSECRETKEY" not in text and "ValueError: bad key •••" in _card(c, "leaky")["status"]
        t = c.post("/api/connections/leaky/test", json={}).json()
        assert t == {"ok": False, "message": "refused •••"}


def test_gap_fill_from_a_plugin(tmp_path):
    class Hist(FeedPlugin):
        async def run(self, market):
            pass

        async def history(self, symbol, start, end):
            for t in range(start // 60_000 * 60_000 + 60_000, end - 60_000, 60_000):
                yield ("bar", t, 10, 11, 9, 10.5, 3)

    got = {}

    class GF:
        windows: dict = {}   # GapFill's open windows: this stand-in keeps none

        def open(self, sym, now):
            return (now - 600_000, now) if sym == "ES" else None

        def minute(self, sym, t, o, h, lo, c, v):
            got.setdefault(sym, []).append((t, o, h, lo, c, v))

        def tick(self, *a):
            raise AssertionError

        def done(self, sym, kind, n, note):
            got["done"] = (sym, kind, n, note)

    market = types.SimpleNamespace(gapfill=GF(), instruments={"ES": None, "NQ": None}, trade_owner={"ES": "hist", "NQ": "hist"})
    asyncio.run(Hist("hist", {}, ["ES", "NQ"]).fill_gaps(market))
    assert len(got["ES"]) == 9 and got["ES"][0][1:] == (10.0, 11.0, 9.0, 10.5, 3) and got["done"] == ("ES", "minutes", 9, "from Feed")


def test_a_feed_leaves_the_gap_fill_of_futures_it_does_not_supply(tmp_path, monkeypatch):
    """NinjaTrader's ES window is open (its data ends an hour ago, the add-on waits for its data connection). A quotes
    plugin quoting ES, and a futures plugin listing ES that does not supply it, connect: the window stays NinjaTrader's,
    so the history it then sends for that hour is kept."""
    from chartsrv.models import Bar, Tick
    from test_review_fixes import build_market

    class Quotes(FeedPlugin):
        TITLE, KIND = "My quotes", "quotes"

        async def run(self, market):
            self.connected()
            await asyncio.sleep(3600)

    class Other(Quotes):
        TITLE, KIND = "Other futures", "futures"

    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    now = int(time.time() * 1000)
    last = now - 3_600_000
    m.store.write([Tick("ES", last, 7000.5, 2, 1)], [("ES", Bar(t=last // 60_000 * 60_000, o=7000, h=7001, l=6999, c=7000.5, v=2, n=1))])
    m.gapfill.requests(now, live=True, minutes=True, contracts=True)
    assert "ES" in m.gapfill.windows and m.trade_owner.get("ES") is None   # NinjaTrader's

    async def go():
        tasks = [asyncio.create_task(f.serve(m)) for f in (Quotes("myquotes", {}, ["ES", "AAPL"]), Other("other", {}, ["ES"]))]
        await asyncio.sleep(0.8)   # past the gap fill they would run once connected
        assert "ES" in m.gapfill.windows and "AAPL" not in m.gapfill.status
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(go())
    assert "ES" in m.gapfill.windows
    m.gapfill.tick("ES", last + 60_000, 7002.0, 3)   # NinjaTrader's history for the hour
    m.gapfill.done("ES", "trades", 1, "ok")
    assert m.store.db.execute("SELECT COUNT(*) FROM ticks WHERE symbol='ES'").fetchone()[0] == 2


def test_a_run_that_returns_is_run_again(tmp_path, monkeypatch):
    """The guide's run() ends when the broker closes its stream cleanly (async for stops): that is a lost connection,
    said on the card and run again 10 s later, not a feed stopped for good that says Connecting... One that returns
    because it was stopped stays stopped."""
    from test_review_fixes import build_market
    m, _ = build_market(tmp_path, monkeypatch)
    runs = []

    class Ends(FeedPlugin):
        TITLE = "Ends"

        async def run(self, market):
            runs.append(1)
            self.connected()
            await asyncio.sleep(0.03)   # the stream ends

    class Quiet(FeedPlugin):
        async def run(self, market):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return   # a plugin that tidies up and returns when stopped

    feed = Ends("ends", {}, ["ES"])
    with pytest.raises(ConnectionError):
        asyncio.run(feed.serve(m))
    assert not feed.stats["connected"] and "ConnectionError" in feed.stats["last_error"]

    real_sleep = asyncio.sleep

    async def go():
        monkeypatch.setattr(asyncio, "sleep", lambda s, *a: real_sleep(min(s, 0.05), *a))   # the 10 s wait, shorter
        task = asyncio.create_task(m._run_feed(Ends("ends", {}, ["ES"])))
        await real_sleep(0.5)
        again = len(runs)
        quiet = asyncio.create_task(m._run_feed(Quiet("quiet", {}, [])))
        await real_sleep(0.05)
        quiet.cancel()
        await asyncio.wait({quiet}, timeout=1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return again, quiet.done()
    again, stopped = asyncio.run(go())
    assert again >= 3 and stopped


# ---- the two broker examples against stand-ins for their client libraries ------------------------------------------
class _Event:
    def __init__(self):
        self.fns = []

    def __iadd__(self, fn):
        self.fns.append(fn)
        return self

    def emit(self, *a):
        for f in self.fns:
            f(*a)


class _Market:
    def __init__(self, owner):
        self.instruments = {"ES": Instrument("ES", "E-mini", 0.25, 50, "ES"), "NQ": Instrument("NQ", "Nasdaq", 0.25, 20, "NQ")}
        self.trade_owner = {s: owner for s in self.instruments}
        self.ticks, self.contracts = [], {}

    def on_tick(self, tk, live=True):
        self.ticks.append(tk)

    def on_contract(self, sym, name):
        self.contracts[sym] = name


def test_ibkr_trades_plugin(monkeypatch):
    fake = types.ModuleType("ib_async")
    state = {}

    class Contract:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Future(Contract):
        pass

    class IB:
        def __init__(self):
            self.disconnectedEvent, self.pendingTickersEvent = _Event(), _Event()
            state["ib"] = self

        async def connectAsync(self, host, port, clientId, timeout, readonly, fetchFields):
            assert readonly and (host, port, clientId) == ("gw", 4002, 77)

        def reqMarketDataType(self, t):
            state["mdt"] = t

        async def qualifyContractsAsync(self, c):
            assert c.secType == "CONTFUT"
            return [Contract(conId=100 + len(c.symbol), exchange=c.exchange)] if c.symbol == "ES" else [None]

        async def reqContractDetailsAsync(self, c):   # the contract, with its last trading day, and its month
            c.lastTradeDateOrContractMonth = "20261218"
            return [types.SimpleNamespace(contract=c, contractMonth="202612")]

        def reqTickByTickData(self, c, kind):
            assert kind == "Last"
            t = types.SimpleNamespace(tickByTicks=[], contract=c)
            state.setdefault("tickers", []).append(t)
            return t

        def disconnect(self):
            state["closed"] = True

    fake.IB, fake.Contract, fake.Future, fake.StartupFetch = IB, Contract, Future, lambda v: v
    monkeypatch.setitem(sys.modules, "ib_async", fake)
    from chartsrv.plugins import Plugins as P
    cls = P([BUILTIN_DIR], set()).types["ibkr_trades"]
    feed = cls("ibkr_trades", {"host": "gw", "port": 4002, "client_id": 77}, ["ES", "NQ"])
    m = _Market("ibkr_trades")

    async def go():
        task = asyncio.create_task(feed.run(m))
        await asyncio.sleep(0.05)
        tk = state["tickers"][0]
        at = datetime.now(timezone.utc)
        tk.tickByTicks = [types.SimpleNamespace(time=at, price=6850.26, size=3), None]
        state["ib"].pendingTickersEvent.emit([tk])
        state["ib"].disconnectedEvent.emit()
        with pytest.raises(ConnectionError, match="disconnected"):
            await asyncio.wait_for(task, 3)
    asyncio.run(go())
    assert [(t.symbol, t.price, t.size) for t in m.ticks] == [("ES", 6850.25, 3)] and m.contracts == {"ES": "ES 12-26"}
    assert state["mdt"] == 1 and state["closed"] and len(state["tickers"]) == 1   # NQ: IBKR didn't know it here


def test_databento_live_plugin(monkeypatch):
    fake = types.ModuleType("databento")
    seen = {}

    class TradeMsg(types.SimpleNamespace):
        pass

    class SymbolMappingMsg(types.SimpleNamespace):
        pass

    class ErrorMsg(types.SimpleNamespace):
        pass

    class Live:
        def __init__(self, key):
            seen["key"] = key

        def subscribe(self, **kw):
            seen["sub"] = kw

        def __aiter__(self):
            async def gen():
                now = time.time_ns()
                yield SymbolMappingMsg(instrument_id=7, stype_in_symbol="NQ.c.0", stype_out_symbol="NQZ6")
                yield TradeMsg(instrument_id=7, ts_event=now, price=25_000_250_000_000, size=2, side="A")
                yield TradeMsg(instrument_id=9, ts_event=now, price=1, size=1, side="B")   # not mapped: dropped
                yield ErrorMsg(err="slow reader")
            return gen()

        def terminate(self):
            seen["closed"] = True

    fake.Live, fake.TradeMsg, fake.SymbolMappingMsg, fake.ErrorMsg = Live, TradeMsg, SymbolMappingMsg, ErrorMsg
    monkeypatch.setitem(sys.modules, "databento", fake)
    cls = Plugins([BUILTIN_DIR], set()).types["databento_live"]
    feed = cls("databento_live", {"api_key": "db-KEY123", "roll": "c", "fill": "no"}, ["NQ"])
    m = _Market("databento_live")
    with pytest.raises(RuntimeError, match="slow reader"):
        asyncio.run(feed.run(m))
    assert seen["sub"] == {"dataset": "GLBX.MDP3", "schema": "trades", "stype_in": "continuous", "symbols": ["NQ.c.0"]}
    assert [(t.symbol, t.price, t.size, t.side) for t in m.ticks] == [("NQ", 25000.25, 2, -1)]
    assert m.contracts == {"NQ": "NQ 12-26"} and seen["closed"]


def test_a_quotes_plugin(tmp_path, monkeypatch):
    d = tmp_path / "feeds"
    d.mkdir()
    (d / "myquotes.py").write_text(
        "import asyncio\nfrom chartsrv.feeds.sdk import FeedPlugin\n"
        "class Q(FeedPlugin):\n TITLE = 'My quotes'\n KIND = 'quotes'\n QUOTE_RANK = 3\n"
        " async def run(self, market):\n  self.connected()\n"
        "  while True:\n   for k in self.wanted(market):\n    self.quote(market, k, 101.5, 100.0, 5000)\n   await asyncio.sleep(0.05)\n")
    app, client = _client(tmp_path, monkeypatch, feed="none", feeds_dir=d)
    with client as c:
        m = app.state.market
        m.watch_keys = {"AAPL", "MSFT"}
        assert _card(c, "myquotes")["group"] == "Quotes"
        assert c.put("/api/connections/myquotes", json={"enabled": True, "symbols": "aapl"}).status_code == 200
        assert _wait(lambda: "AAPL" in m.quotes)
        q = m.quotes["AAPL"]
        assert (q["last"], q["prev"], q["src"]) == (101.5, 100.0, "myquotes") and "MSFT" not in m.quotes
        assert m.quote_wanted("AAPL", "ibkr") is False   # rank 3: ahead of IBKR's quotes
        assert _wait(lambda: _card(c, "myquotes")["status"].startswith("Connected ·"))


def test_your_own_program_sends_quotes(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch, feed="none")
    port = _port()
    with client as c:
        m = app.state.market
        card = _card(c, "lines_quotes")
        assert card["group"] == "Quotes" and not card["listed"]
        assert c.put("/api/connections/lines_quotes", json={"enabled": True, "port": port, "symbols": "AAPL, VIX"}).status_code == 200
        assert _wait(lambda: _card(c, "lines_quotes")["status"].startswith("Listening on port"))
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.sendall(b"HELLO||1\nQ|AAPL|231.5|229.0|51000000\nQ|MSFT|500|490\nT|ES|1|6000|1|1\n")
            assert _wait(lambda: "AAPL" in m.quotes)
            time.sleep(0.2)
        q = m.quotes["AAPL"]
        assert (q["last"], q["prev"], q["vol"], q["src"]) == (231.5, 229.0, 51000000.0, "lines_quotes")
        assert "MSFT" not in m.quotes and "ES" not in m.last_tick   # not in its Symbols; a quotes feed sends no trades


def test_finnhub_plugin(monkeypatch):
    import httpx
    import websockets.asyncio.client as wsc

    sent = []

    class WS:
        def __init__(self, url):
            assert url == "wss://ws.finnhub.io?token=fh-KEY"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, msg):
            sent.append(json.loads(msg))

        def __aiter__(self):
            async def gen():
                yield json.dumps({"type": "ping"})
                yield json.dumps({"type": "trade", "data": [{"s": "AAPL", "p": 232.25, "t": int(time.time() * 1000), "v": 100}]})
                yield json.dumps({"type": "error", "msg": "Subscribing to too many symbols"})
            return gen()

    monkeypatch.setattr(wsc, "connect", lambda url, **kw: WS(url))

    def rest(req):
        assert req.url.params["token"] == "fh-KEY"
        return httpx.Response(200, json={"c": 230.0, "pc": 228.0, "t": int(time.time())})
    quotes = []
    market = types.SimpleNamespace(watch_keys={"AAPL", "BRK.B", "BTCUSD", "ES1!"}, http=httpx.AsyncClient(transport=httpx.MockTransport(rest)),
                                   on_quote=lambda *a, **kw: quotes.append((a[0], a[1], a[2], kw["source"])))
    monkeypatch.setattr(asyncio, "sleep", (lambda real: (lambda s: real(0)))(asyncio.sleep))
    cls = Plugins([BUILTIN_DIR], set()).types["finnhub"]
    feed = cls("finnhub", {"api_key": "fh-KEY"}, [])
    assert feed.wanted(market) == ["AAPL", "BRK.B"]   # stocks and ETFs only
    with pytest.raises(RuntimeError, match="too many symbols"):
        asyncio.run(feed.run(market))
    assert [s["symbol"] for s in sent] == ["AAPL", "BRK.B"]
    assert ("AAPL", 230.0, 228.0, "finnhub") in quotes and ("AAPL", 232.25, 228.0, "finnhub") in quotes


# ---- data plugins: DATA:<symbol> series that chart and go in the watchlists ----------------------------------------
def test_csv_parse():
    from importlib import util
    spec = util.spec_from_file_location("csvp", BUILTIN_DIR / "csv_data.py")
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    obs, dec = mod.parse('Date,Ratio,Other\n2026-10-01,"1,234.5",9\n10/02/2026,$1236.75,9\nbad,row\n2026-10-03,4.5%,9\n')
    assert obs == [("2026-10-01", 1234.5), ("2026-10-02", 1236.75), ("2026-10-03", 4.5)] and dec == 2
    assert mod.parse("d;v\n20261005;7\n20261004;6\n")[0] == [("2026-10-04", 6.0), ("2026-10-05", 7.0)]
    assert mod.parse("2026-10,1,2\n2026-11,3,4\n", column=3)[0] == [("2026-10-01", 2.0), ("2026-11-01", 4.0)]
    with pytest.raises(ValueError, match="no rows"):
        mod.parse("a,b\nc,d\n")


def test_your_own_data_from_a_file_and_a_link(tmp_path, monkeypatch):
    import httpx
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    (feeds / "gold_oil.csv").write_text("date,ratio\n2026-10-01,52.1\n2026-10-02,52.6\n2026-10-05,53.0\n")
    app, client = _client(tmp_path, monkeypatch, feed="none", feeds_dir=feeds)
    with client as c:
        m = app.state.market
        m.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text="2026-09-30,4.10\n2026-10-31,4.25\n") if req.url.path == "/sheet.csv" else httpx.Response(404)))
        card = _card(c, "csv_data")
        assert card["group"] == "Data" and not card["listed"] and [f["key"] for f in card["fields"]] == ["enabled", "code", "source", "name", "column"]
        t = c.post("/api/connections/csv_data/test", json={"code": "goldoil", "source": "gold_oil.csv"}).json()
        assert t["ok"] and t["message"] == "3 rows, 2026-10-01 to 2026-10-05 (last 53): DATA:GOLDOIL"
        assert not c.post("/api/connections/csv_data/test", json={"code": "x", "source": "../etc/passwd"}).json()["ok"]
        assert c.put("/api/connections/csv_data", json={"enabled": True, "code": "goldoil", "source": "gold_oil.csv", "name": "Gold/oil"}).status_code == 200
        r = c.get("/api/ext/bars", params={"symbol": "data:goldoil", "spec": "1D"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert (d["symbol"], d["name"], d["source"], d["specs"], d["decimals"]) == ("DATA:GOLDOIL", "Gold/oil", "data", ["1D", "1W", "1M"], 1)
        assert [b["c"] for b in d["bars"]] == [52.1, 52.6, 53.0]
        assert _card(c, "csv_data")["status"].startswith("On · DATA:GOLDOIL · read")

        # the symbol search finds it, by symbol or by name
        found = lambda q: [r for r in c.get("/api/symbols/search", params={"q": q}).json()["results"] if r["symbol"] == "DATA:GOLDOIL"]   # noqa: E731
        assert _wait(lambda: found("goldoil"))
        assert found("gold / oil")[0]["type"] == "data" and found("DATA:GOLD")
        src = {x["source"]: x for x in c.get("/api/symbols/status").json()["sources"]}
        assert src["plugin:csv_data"]["rows"] == 1 and _card(c, "symbols")["group"] == "Data"
        assert c.get("/api/ext/bars", params={"symbol": "DATA:NOPE", "spec": "1D"}).status_code == 400

        # a second series, from a link
        r = c.post("/api/connections", json={"type": "csv_data", "enabled": True, "code": "RATE", "source": "https://sheets.example/sheet.csv"})
        assert r.status_code == 200, r.text
        assert [b["c"] for b in c.get("/api/ext/bars", params={"symbol": "DATA:RATE", "spec": "1M"}).json()["bars"]] == [4.1, 4.25]

        # in a watchlist: the latest value and its change, as of its date
        m.watch_keys = set(m.watch_keys) | {"DATA:GOLDOIL"}
        next(f for f in m.feed_objs if f.name == "fred").watch_changed(m)
        assert _wait(lambda: "DATA:GOLDOIL" in m.quotes)
        q = m.quotes["DATA:GOLDOIL"]
        assert (q["last"], q["prev"], q["src"], q["rt"]) == (53.0, 52.6, "data", "eod")

        # saved again: read again at once (not the stored copy)
        (feeds / "gold_oil.csv").write_text("2026-10-06,54.4\n")
        c.put("/api/connections/csv_data", json={"name": "Gold / oil"})
        d = c.get("/api/ext/bars", params={"symbol": "DATA:GOLDOIL", "spec": "1D"}).json()
        assert [b["c"] for b in d["bars"]] == [54.4] and d["name"] == "Gold / oil"

        # a link that fails: said on its card
        c.put("/api/connections/csv_data-2", json={"source": "https://sheets.example/gone.csv"})
        assert c.get("/api/ext/bars", params={"symbol": "DATA:RATE", "spec": "1D"}).status_code == 400
        assert _card(c, "csv_data-2")["state"] == "error" and "HTTP 404" in _card(c, "csv_data-2")["status"]



def test_finnhub_lists_us_symbols():
    import httpx

    def answer(req):
        assert req.url.path == "/api/v1/stock/symbol" and req.url.params["exchange"] == "US"
        return httpx.Response(200, json=[{"symbol": "AAPL", "description": "APPLE INC", "type": "Common Stock", "mic": "XNAS"},
                                         {"symbol": "SPY", "description": "SPDR S&P 500 ETF TRUST", "type": "ETP", "mic": "ARCX"},
                                         {"symbol": "NSRGY", "description": "NESTLE SA-SPONS ADR", "type": "ADR", "mic": "OOTC"},
                                         {"symbol": "BRK-A.X", "description": "?", "type": "x", "mic": "?"}])
    cls = Plugins([BUILTIN_DIR], set()).types["finnhub"]

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            return await cls("finnhub", {"api_key": "fh"}, []).listing(http)
    assert asyncio.run(go()) == [("AAPL", "Apple Inc", "stock", "NASDAQ"), ("SPY", "Spdr S&P 500 Etf Trust", "etf", "NYSE Arca"),
                                 ("NSRGY", "Nestle Sa-Spons Adr", "stock", "OTC")]


def test_plugins_load_while_running(tmp_path, monkeypatch):
    d = tmp_path / "feeds"
    d.mkdir()
    app, client = _client(tmp_path, monkeypatch, feed="none", feeds_dir=d)
    src = ("import asyncio\nfrom chartsrv.feeds.sdk import FeedPlugin\n"
           "class Q(FeedPlugin):\n TITLE = 'Hot quotes'\n KIND = 'quotes'\n"
           " async def run(self, market):\n  self.connected()\n"
           "  while True:\n   for k in self.wanted(market):\n    self.quote(market, k, PRICE, 100.0, 5000)\n   await asyncio.sleep(0.05)\n")
    ids = lambda: {x["id"] for x in c.get("/api/connections").json()["connections"]}
    with client as c:
        m = app.state.market
        m.watch_keys = {"AAPL"}
        assert "hot" not in ids()
        (d / "hot.py").write_text(src.replace("PRICE", "101.5"))   # dropped in: listed without a restart
        assert _wait(lambda: "hot" in ids(), 10)
        assert c.put("/api/connections/hot", json={"enabled": True, "symbols": "AAPL"}).status_code == 200
        assert _wait(lambda: m.quotes.get("AAPL", {}).get("last") == 101.5)
        (d / "hot.py").write_text(src.replace("PRICE", "102.25"))  # changed: the running connection restarts on it
        assert _wait(lambda: m.quotes.get("AAPL", {}).get("last") == 102.25, 10)
        (d / "hot.py").write_text("def (:\n")                     # broken: stopped, the error shown
        assert _wait(lambda: "hot" not in m.feed_names, 10)
        assert any(e["file"] == "hot.py" for e in m.plugins.errors)
        (d / "hot.py").write_text(src.replace("PRICE", "103"))     # fixed: on again by itself
        assert _wait(lambda: m.quotes.get("AAPL", {}).get("last") == 103, 10)
        (d / "hot.py").unlink()                                     # removed: stopped and gone from the list
        assert _wait(lambda: "hot" not in m.feed_names and "hot" not in ids(), 10)


def test_packages_parse():
    from chartsrv import packages
    assert packages.parse("databento") == ("databento", "databento")
    assert packages.parse("python-dateutil>=2.8:dateutil") == ("python-dateutil>=2.8", "dateutil")
    assert packages.parse("ib-async[x]==2.1") == ("ib-async[x]==2.1", "ib_async")
    for bad in ["", "x; rm -rf /", "--index-url=http://evil", "a b", "https://x/y.whl", "../x", 3]:
        assert packages.parse(bad) is None, bad
    assert packages.missing(["json", "no_such_pkg_xyz", "--evil"]) == ["no_such_pkg_xyz", "--evil"]


def test_install_a_plugins_packages(tmp_path, monkeypatch):
    from chartsrv import packages
    d, lib = tmp_path / "feeds", tmp_path / "lib"
    d.mkdir()
    lib.mkdir()
    monkeypatch.syspath_prepend(str(lib))
    (d / "needy.py").write_text(
        "import asyncio\nfrom chartsrv.feeds.sdk import FeedPlugin\n"
        "class Q(FeedPlugin):\n TITLE = 'Needy'\n KIND = 'quotes'\n REQUIRES = ['tatanka-fake-a>=1']\n"
        " async def run(self, market):\n  import tatanka_fake_a\n  self.connected()\n"
        "  while True:\n   for k in self.wanted(market):\n    self.quote(market, k, tatanka_fake_a.PRICE, 100.0, 5000)\n   await asyncio.sleep(0.05)\n")
    (d / "toplevel.py").write_text(   # imports it at the top: fails to load until it's there
        "import tatanka_fake_b\nfrom chartsrv.feeds.sdk import FeedPlugin\n"
        "class T(FeedPlugin):\n TITLE = 'Top'\n KIND = 'quotes'\n REQUIRES = ['tatanka-fake-b']\n"
        " async def run(self, market):\n  self.connected()\n")
    calls = []

    async def fake_pip(specs):
        calls.append(specs)
        await asyncio.sleep(0.3)
        if specs == ["tatanka-fake-b"] and len(calls) == 2:
            return False, "ERROR: No matching distribution found for tatanka-fake-b"
        for sp in specs:
            (lib / (packages.parse(sp)[1] + ".py")).write_text("PRICE = 77.5\n")
        return True, ""
    monkeypatch.setattr(packages.Installer, "_pip", staticmethod(fake_pip))
    app, client = _client(tmp_path, monkeypatch, feed="none", feeds_dir=d)
    with client as c:
        m = app.state.market
        m.watch_keys = {"AAPL"}
        assert c.put("/api/connections/needy", json={"enabled": True, "symbols": "AAPL"}).status_code == 200
        card = _card(c, "needy")
        assert card["install"] == ["tatanka-fake-a>=1"] and card["status"].startswith("Needs the Python package tatanka-fake-a")
        assert "needy" not in m.feed_names   # not started without it
        assert c.post("/api/connections/discord/install").status_code == 400
        assert c.post("/api/connections/nope/install").status_code == 404
        assert c.post("/api/connections/needy/install").json() == {"installing": ["tatanka-fake-a>=1"]}
        assert _card(c, "needy")["status"].startswith("Installing")
        assert c.post("/api/connections/needy/install").status_code == 400   # one at a time
        assert _wait(lambda: m.quotes.get("AAPL", {}).get("last") == 77.5)    # installed: started by itself
        assert "install" not in _card(c, "needy") and calls == [["tatanka-fake-a>=1"]]

        errs = _card(c, "plugin_errors")
        assert errs["install"] == ["tatanka-fake-b"]
        c.post("/api/connections/plugin_errors/install")
        assert _wait(lambda: "failed" in _card(c, "plugin_errors")["status"])
        assert "No matching distribution" in _card(c, "plugin_errors")["status"]
        c.post("/api/connections/plugin_errors/install")                     # again: this time it works
        assert _wait(lambda: "toplevel" in m.plugins.types)
        assert not any(x["id"] == "plugin_errors" for x in c.get("/api/connections").json()["connections"])


def test_an_older_ninjatrader_add_on_is_told_to_update(tmp_path, monkeypatch):
    from chartsrv.feeds.ninjatrader import ADDON_VERSION
    app, client = _client(tmp_path, monkeypatch, feed="ninjatrader")
    with client as c:
        port = app.state.market.s.nt_port
        assert _card(c, "ninjatrader")["addon"] == {"version": ADDON_VERSION, "file": "/api/addon/TatankaChartFeed.cs", "old": False}
        r = c.get("/api/addon/TatankaChartFeed.cs")
        assert r.status_code == 200 and f"Version  = {ADDON_VERSION};" in r.text and "attachment" in r.headers["content-disposition"]
        with socket.create_connection(("127.0.0.1", port)) as s:   # the add-on before versions: HELLO|token|1
            s.sendall(b"HELLO||1\n")
            assert _wait(lambda: _card(c, "ninjatrader")["status"].startswith("Add-on connected, an older version"))
            assert _card(c, "ninjatrader")["addon"]["old"] is True
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.sendall(f"HELLO||{ADDON_VERSION}\n".encode())
            assert _wait(lambda: _card(c, "ninjatrader")["status"].startswith("Add-on connected ·"))
            assert _card(c, "ninjatrader")["addon"]["old"] is False


def test_reload_keeps_owners_and_data_plugins_right(tmp_path, monkeypatch):
    """After a plugin file changes: a data plugin is never started as a feed, and which feed supplies each future is
    worked out again (NinjaTrader told), so a future a removed plugin supplied comes back from NinjaTrader."""
    d = tmp_path / "feeds"
    d.mkdir()
    (d / "fut.py").write_text("import asyncio\nfrom chartsrv.feeds.sdk import FeedPlugin\n"
                              "class F(FeedPlugin):\n TITLE = 'My futures'\n async def run(self, market):\n"
                              "  self.connected()\n  await asyncio.Event().wait()\n")
    (d / "gold.csv").write_text("2026-10-01,1\n2026-10-02,2\n")
    app, client = _client(tmp_path, monkeypatch, feed="ninjatrader", feeds_dir=d)
    with client as c:
        m = app.state.market
        assert c.put("/api/connections/csv_data", json={"enabled": True, "code": "G", "source": "gold.csv"}).status_code == 200
        assert c.put("/api/connections/fut", json={"enabled": True, "symbols": "ES"}).status_code == 200
        assert m.trade_owner == {"ES": "fut"}
        nt = socket.create_connection(("127.0.0.1", m.s.nt_port))
        nt.sendall(b"HELLO||2\n")
        nt.settimeout(5)
        got = b""
        while b"W|" not in got:
            got += nt.recv(65536)
        assert b"ES" not in got.split(b"I|", 1)[1].split(b"\n", 1)[0].split(b",")   # ES: the plugin's
        (d / "other.py").write_text("x = 1\n")   # any change: a reload
        assert _wait(lambda: any(e["file"] == "other.py" for e in m.plugins.errors), 10)
        assert "csv_data" not in m.feed_names   # read when asked, never run
        (d / "fut.py").unlink()                  # the plugin goes: ES back to NinjaTrader, and it is told
        assert _wait(lambda: m.trade_owner == {}, 10)
        got = b""
        while b"ES" not in got.split(b"I|")[-1].split(b"\n", 1)[0].split(b","):
            got += nt.recv(65536)
        nt.close()


def test_install_refuses_names_pip_cant_take(tmp_path, monkeypatch):
    from chartsrv import packages
    d = tmp_path / "feeds"
    d.mkdir()
    (d / "odd.py").write_text("from chartsrv.feeds.sdk import FeedPlugin\nclass Q(FeedPlugin):\n TITLE = 'Odd'\n KIND = 'quotes'\n"
                              " REQUIRES = ['databento >= 0.40']\n async def run(self, market):\n  pass\n")
    (d / "str.py").write_text("from chartsrv.feeds.sdk import FeedPlugin\nclass Q(FeedPlugin):\n TITLE = 'Str'\n KIND = 'quotes'\n"
                              " REQUIRES = 'databento'\n async def run(self, market):\n  pass\n")
    app, client = _client(tmp_path, monkeypatch, feed="none", feeds_dir=d)
    with client as c:
        m = app.state.market
        r = c.post("/api/connections/odd/install")
        assert r.status_code == 400 and "Nothing pip can install" in r.json()["detail"] and m.packages.busy == []
        assert any(e["file"] == "str.py" and "REQUIRES" in e["error"] for e in m.plugins.errors)   # a string, not a list
    assert packages.parse("MetaTrader5") == ("MetaTrader5", "MetaTrader5")   # imported as it is spelled


def test_a_reused_class_cant_take_a_taken_id(tmp_path):
    d = tmp_path / "feeds"
    d.mkdir()
    src = "from chartsrv.feeds.sdk import FeedPlugin\nclass X(FeedPlugin):\n ID = 'mybroker'\n TITLE = '{t}'\n async def run(self, m):\n  pass\n"
    (d / "b_old.py").write_text(src.format(t="Old"))
    first = Plugins([d], set())
    assert first.types["mybroker"].TITLE == "Old"
    (d / "a_new.py").write_text(src.format(t="New"))   # sorts first: takes the ID, as it would at a fresh start
    again = Plugins([d], set(), previous=first)
    assert again.types["mybroker"].TITLE == "New"
    assert any(e["file"] == "b_old.py" and "taken" in e["error"] for e in again.errors)
