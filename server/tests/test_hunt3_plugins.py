"""Third bug-hunt fixes in the feed plugins: the values the SDK helpers take, the plugin classes the app loads, data
plugin series, keys in the log and on the cards, Finnhub's previous close and the IBKR futures contract's name."""
import asyncio
import json
import logging
import sys
import time
import types
from datetime import date
from decimal import Decimal

import httpx
import pytest

from chartsrv import connections as conns
from chartsrv.feeds.sdk import FeedPlugin, secret
from chartsrv.plugins import BUILTIN_DIR, Plugins
from test_review_fixes import build_market


class _Quotes(FeedPlugin):
    KIND = "quotes"

    async def run(self, market):
        pass


class _Futures(FeedPlugin):
    KIND = "futures"

    async def run(self, market):
        pass


def _numbers():
    """What a plugin's numbers can be besides Python's own: a Decimal, and numpy's (a pandas row's) when it is here."""
    out = [Decimal]
    try:
        import numpy
        out.append(numpy.int64)
    except ImportError:
        pass
    return out


# ---- r3-feeds-live-0, r2-crashes-1: the quote and trade helpers hand on plain, finite numbers --------------------
@pytest.mark.parametrize("num", _numbers())
def test_plugin_quote_volume_and_time_are_plain_numbers(tmp_path, monkeypatch, num):
    m, _ = build_market(tmp_path, monkeypatch)
    feed = _Quotes("my_quotes", {}, [])
    at = int(time.time() * 1000) - 1000
    assert feed.quote(m, "AAPL", 230.5, 229.0, num(41_000_000), at=num(at))
    assert feed.quote(m, "MSFT", 410.0, 405.0, "1234", at=str(at))   # a JSON API's numbers as text
    q, q2 = m.quotes["AAPL"], m.quotes["MSFT"]
    assert (q["vol"], q["ts"], q2["vol"], q2["ts"]) == (41_000_000, at, 1234, at) and type(q["vol"]) is float
    assert not feed.quote(m, "NVDA", 140.0, 139.0, "lots") and not feed.quote(m, "NVDA", 140.0, 139.0, at=float("nan"))
    assert "NVDA" not in m.quotes and feed.stats["dropped"] == 2
    json.dumps(m.hello(), allow_nan=False)   # what every page gets first: it parses


def test_your_own_programs_infinite_volume_is_left_out(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    feed = Plugins([BUILTIN_DIR], set()).types["lines_quotes"]("lines_quotes", {"port": 8803, "token": ""}, [])
    feed.line(m, "Q|AAPL|231.5|230.1|inf".split("|"))   # a volume worked out by division, printed as inf
    feed.line(m, "Q|MSFT|410|405|-1".split("|"))
    assert (m.quotes["AAPL"]["last"], m.quotes["AAPL"]["vol"], m.quotes["MSFT"]["vol"]) == (231.5, None, None)
    json.dumps(m.hello(), allow_nan=False)


@pytest.mark.parametrize("num", _numbers())
def test_plugin_trade_side_is_stored_as_a_number(tmp_path, monkeypatch, in_session, num):
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    m.trade_owner = {"ES": "p"}
    feed = _Futures("p", {}, ["ES"])
    now = int(time.time() * 1000)
    assert feed.trade(m, "ES", now - 5000, 7000.25, 2, num(1)) and feed.trade(m, "ES", now - 4000, 7000.5, 1, num(-1))
    assert not feed.trade(m, "ES", now - 3000, 7000.5, 1, num(2))
    m.flush()
    assert [tuple(r) for r in m.store.db.execute("SELECT side, typeof(side) FROM ticks ORDER BY ts")] == [(1, "integer"), (-1, "integer")]


# ---- r3-feeds-live-1: a plugin whose FIELDS or TITLE is wrong is said in Connections, and the server starts -----------
def test_a_plugin_with_wrong_fields_or_title_does_not_load(tmp_path):
    d = tmp_path / "feeds"
    d.mkdir()
    head, run = "from chartsrv.feeds.sdk import FeedPlugin, secret\nclass X(FeedPlugin):\n", " async def run(self, market):\n  pass\n"
    (d / "nobrackets.py").write_text(head + " FIELDS = secret('api_key', 'API key')\n" + run)   # the brackets forgotten
    (d / "names.py").write_text(head + " FIELDS = ['api_key']\n" + run)
    (d / "notitle.py").write_text(head + " TITLE = None\n" + run)
    (d / "zz_good.py").write_text(head + " TITLE = 'Good'\n FIELDS = [secret('api_key', 'API key')]\n" + run)
    p = Plugins([BUILTIN_DIR, d], conns.builtin_ids())
    errs = {e["file"]: e["error"] for e in p.errors}
    assert set(errs) == {"nobrackets.py", "names.py", "notitle.py"} and "FIELDS" in errs["names.py"] and "TITLE" in errs["notitle.py"]
    conns.register_plugins(p)   # what the app does at start: it raised, and the app never came up
    try:
        assert "zz_good" in conns.BY_ID and "nobrackets.py: FIELDS" in conns.BY_ID["plugin_errors"]["about"]
    finally:
        conns.register_plugins(Plugins([BUILTIN_DIR], conns.builtin_ids()))


# ---- r2-crashes-2: a data plugin's missing values and odd dates never stop the FRED and EIA watchlist values ---------
class _Data(FeedPlugin):
    KIND = "data"
    rows: list = []

    async def series(self, http, code):
        return list(self.rows), 2


def test_a_data_plugins_missing_values_and_odd_dates_are_left_out(tmp_path):
    from chartsrv.plugins import PluginSeries
    from chartsrv.store import Store
    feed = _Data("mydata", {}, [])
    ds = PluginSeries(Store(tmp_path / "c.db"), object(), feed)
    feed.rows = [("2026-10-21", 100.0), ("2026-10-22", "101.5"), ("2026-10-23", None), ("2026-10-24T00:00:00Z", 5.0),
                 (date(2026, 10, 25), 6.0), ("2026-10-26", float("inf"))]   # an API's null, a datetime, a date object
    assert asyncio.run(ds.series("GOLDOIL")) == ([("2026-10-21", 100.0), ("2026-10-22", 101.5)], 2)
    feed.rows = [("2026-10-24T00:00:00Z", 5.0)]
    with pytest.raises(ValueError):
        asyncio.run(ds.series("OTHER"))
    assert "OTHER: no rows of a YYYY-MM-DD date" in feed.stats["last_error"]   # said on its card


def test_fred_values_go_on_past_a_data_plugins_bad_series(tmp_path):
    from chartsrv.feeds.fred import FredData, FredQuotes
    from chartsrv.plugins import PluginSeries
    from chartsrv.store import Store
    store = Store(tmp_path / "c.db")
    feed = _Data("mydata", {}, [])
    feed.rows = [("2026-10-22", 101.5), ("2026-10-23", None)]
    ds = PluginSeries(store, object(), feed)
    with store.db:   # a copy kept before its dates were checked
        store.db.execute(f"INSERT INTO {ds.table} VALUES ('OLD', ?, 2, '[[\"2026-10-23T00:00:00Z\", 5.0]]')", (int(time.time() * 1000),))
    csv = "observation_date,DGS10\n2026-10-22,4.10\n2026-10-23,4.12\n"
    quotes = {}
    m = types.SimpleNamespace(
        fred=FredData(store, httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=csv)))),
        watch_keys={"DATA:GOLDOIL", "DATA:OLD", "FRED:DGS10"}, data_source=lambda code: (ds, code, "data"),
        on_quote=lambda k, last, prev, vol, source, at: quotes.update({k: (last, prev)}))

    async def go():
        fq = FredQuotes()
        task = asyncio.create_task(fq.run(m))
        for _ in range(100):
            if "FRED:DGS10" in quotes or task.done():
                break
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return fq.stats
    stats = asyncio.run(go())
    assert quotes == {"DATA:GOLDOIL": (101.5, None), "FRED:DGS10": (4.12, 4.1)} and stats["errors"] == 1


# ---- r2-security-0: a plugin's key never reaches the log through its crash's traceback --------------------------------
class _Broker(FeedPlugin):
    """A plugin that sends its key as a query parameter, the way token APIs are asked, and gets a 401."""
    KIND = "quotes"
    FIELDS = [secret("api_key", "API key")]

    async def run(self, market):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(401))) as http:
            r = await http.get("https://api.broker.example/v1/quotes", params={"token": self.values["api_key"]})
            r.raise_for_status()


def test_a_plugins_crash_is_logged_without_its_key(tmp_path, monkeypatch, caplog):
    from chartsrv import market as market_mod
    m, _ = build_market(tmp_path, monkeypatch)
    feed = _Broker("mybroker", {"api_key": "AbCdEf123456"}, [])
    real_sleep = asyncio.sleep

    async def go():
        monkeypatch.setattr(market_mod.asyncio, "sleep", lambda s: real_sleep(min(s, 0.01)))   # the 10 s wait, shorter
        task = asyncio.create_task(m._run_feed(feed))
        await real_sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    monkeypatch.setattr(logging.getLogger("httpx"), "level", logging.WARNING)   # as the app sets it: its INFO lines print URLs
    with caplog.at_level("INFO"):
        asyncio.run(go())
    assert "feed mybroker crashed" in caplog.text and "quotes?token=•••" in caplog.text and "Traceback" in caplog.text
    assert "AbCdEf123456" not in caplog.text and "•••" in feed.stats["last_error"]


# ---- r2-security-1: a key with + / = or a space is taken out as a URL carries it too ----------------------------------
@pytest.mark.parametrize("key", ["Ab+c/d=EFGH1234", "my key+é/1="])
def test_a_url_encoded_key_is_taken_out_of_the_card_and_the_log(tmp_path, monkeypatch, key):
    from urllib.parse import quote_plus
    m, _ = build_market(tmp_path, monkeypatch)
    feed = _Broker("mybroker", {"api_key": key}, [])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(feed.serve(m))
    assert "token=•••" in feed.stats["last_error"] and quote_plus(key, safe="") not in feed.stats["last_error"]
    typed = str(httpx.URL(f"https://api.broker.example/v1/{key.replace('/', '')}/q?token={key}"))   # written into the URL
    assert feed.scrub(f"failed: {typed}").endswith("/q?token=•••")

    class Picky(_Broker):
        def __init__(self, name, values, symbols):
            raise ValueError(f"no account for https://api.broker.example/v1?token={quote_plus(values['api_key'], safe='')}")
    m.plugins.types["picky"] = Picky
    assert m.make_plugin("picky", "picky", {"api_key": key}) is None
    assert m.plugin_failed["picky"] == "Did not start: ValueError: no account for https://api.broker.example/v1?token=•••"


# ---- r3-feeds-quotes-2: Finnhub's previous close is asked again while its socket stays up --------------------------
def test_finnhub_asks_the_previous_close_again_while_connected(monkeypatch):
    import websockets.asyncio.client as wsc
    finnhub = Plugins([BUILTIN_DIR], set()).types["finnhub"]
    monkeypatch.setattr(sys.modules[finnhub.__module__], "PREV_EVERY_S", 0.05, raising=False)   # an hour, shorter
    day, conns, quotes = {"n": 1}, [], []
    real_sleep = asyncio.sleep

    def trade(p):
        return json.dumps({"type": "trade", "data": [{"s": "AAPL", "p": p, "t": int(time.time() * 1000)}]})

    class WS:
        def __init__(self, url):
            conns.append(url)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, msg):
            pass

        def __aiter__(self):
            async def gen():
                yield trade(230.0)   # Monday evening: against Friday's close
                day["n"] = 2         # the night passes, the socket stays up (pings); Tuesday's session opens
                await real_sleep(0.3)
                yield trade(232.0)
                yield json.dumps({"type": "error", "msg": "stop"})
            return gen()
    monkeypatch.setattr(wsc, "connect", lambda url, **kw: WS(url))
    closes = {1: {"c": 230.0, "pc": 228.0}, 2: {"c": 231.5, "pc": 230.0}}   # Friday's close 228, Monday's 230
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={**closes[day["n"]], "t": int(time.time())})))
    market = types.SimpleNamespace(watch_keys={"AAPL"}, http=http, on_quote=lambda *a, **kw: quotes.append(a[:3]))
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))   # the free plan's pause between calls
    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(finnhub("finnhub", {"api_key": "k"}, []).run(market))
    assert quotes == [("AAPL", 230.0, 228.0), ("AAPL", 230.0, 228.0), ("AAPL", 232.0, 230.0)] and len(conns) == 1


# ---- r3-feeds-live-4: an IBKR future is named by its contract month, not its last trading day -------------------------
def test_ibkr_futures_are_named_by_their_contract_month(monkeypatch):
    from chartsrv.config import Instrument
    from test_feed_plugins import _Event
    fake, fronts = types.ModuleType("ib_async"), iter([1, 1, 2])
    # IBKR's contract details for crude: CLZ6 (December) stops trading on 2026-11-20, CLF7 (January) on 2026-12-18
    details = {1: ("20261120", "202612"), 2: ("20261218", "202701")}

    class Contract:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class IB:
        def __init__(self):
            self.disconnectedEvent, self.pendingTickersEvent = _Event(), _Event()

        async def connectAsync(self, *a, **kw):
            pass

        def reqMarketDataType(self, t):
            pass

        async def qualifyContractsAsync(self, c):   # CONTFUT: the front month; a future: as its details give it
            if getattr(c, "secType", "") == "CONTFUT":
                return [Contract(conId=next(fronts), exchange=c.exchange)]
            return [(await self.reqContractDetailsAsync(c))[0].contract]

        async def reqContractDetailsAsync(self, c):
            last, month = details[c.conId]
            c.lastTradeDateOrContractMonth = last
            return [types.SimpleNamespace(contract=c, contractMonth=month)]

        def reqTickByTickData(self, c, kind):
            return types.SimpleNamespace(tickByTicks=[], contract=c)

        def disconnect(self):
            pass

    fake.IB, fake.Contract, fake.Future, fake.StartupFetch = IB, Contract, Contract, lambda v: v
    monkeypatch.setitem(sys.modules, "ib_async", fake)
    cls = Plugins([BUILTIN_DIR], set()).types["ibkr_trades"]
    monkeypatch.setattr(sys.modules[cls.__module__], "ROLL_CHECK_S", 0)   # the roll check at once
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(min(s, 0.01)))
    contracts = []
    market = types.SimpleNamespace(instruments={"CL": Instrument("CL", "Crude", 0.01, 1000, "CL")}, trade_owner={"CL": "ibkr_trades"},
                                   on_contract=lambda sym, name, was=None: contracts.append((sym, name)))
    feed = cls("ibkr_trades", {"host": "gw", "port": 4002, "client_id": 77}, ["CL"])
    with pytest.raises(RuntimeError, match="CL rolled to CL 01-27"):
        asyncio.run(feed.run(market))
    assert contracts == [("CL", "CL 12-26")]   # as NinjaTrader and Databento name it: no false roll when it takes CL over
