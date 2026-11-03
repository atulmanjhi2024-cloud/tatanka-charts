"""Round 13 of the bug hunt, the feeds: NinjaTrader's quotes stop when its market data does (the next source takes
over); IBKR quotes two watchlist keys of one contract (US10Y and TNX) both, and drops one without the other; IBKR's
Cboe yield indexes, at ten times the yield, come in percent at every level; one Yahoo batch that fails keeps the
others' quotes; a fresh install's lists are not swapped at its second start; a quote TWS refuses is asked again."""
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).resolve().parent / "addon_csharp"
mono = pytest.mark.skipif(not (shutil.which("mcs") and shutil.which("mono")), reason="needs mono")


def _ms():
    return int(time.time() * 1000)


# ---- r13-feeds-1: the add-on's Q lines stop when NinjaTrader's market data does --------------------------------------

QUOTE_SCENARIOS = {"silent": "ZN", "playback": "ZB", "dropped": "ZF"}


@pytest.fixture(scope="module")
def quote_runs(tmp_path_factory):
    """Per scenario of QuoteHarness.cs: the Q lines the add-on's send loop wrote to the server, as (ms got, line), and
    its marks {what: ms}. The add-on is compiled with this test's port, and a symbol's Q lines stop after 2.5 s
    without its market data (60 s in NinjaTrader), so the scenarios take seconds. They run side by side, one root
    each."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    got = []

    def read(conn):
        with conn, conn.makefile("r", encoding="utf-8") as f:
            for line in f:
                got.append((_ms(), line.strip()))

    def accept():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=read, args=(conn,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()

    work = tmp_path_factory.mktemp("quotes")
    src = (ROOT / "ninjatrader" / "TatankaChartFeed.cs").read_text(encoding="utf-8")
    src = src.replace("Port     = 8801;", f"Port     = {port};").replace("QuoteLiveMs = 60000;", "QuoteLiveMs = 2500;")
    (work / "TatankaChartFeed.cs").write_text(src, encoding="utf-8")
    exe = work / "quotes.exe"
    r = subprocess.run(["mcs", "-langversion:Latest", "-r:System.Core.dll", f"-out:{exe}", str(HARNESS / "Stubs.cs"),
                        str(work / "TatankaChartFeed.cs"), str(HARNESS / "QuoteHarness.cs")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr

    def run(name):
        r = subprocess.run(["mono", str(exe), name, QUOTE_SCENARIOS[name]], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return {row[2:].split()[0]: int(row.split()[2]) for row in r.stdout.splitlines() if row.startswith("# ")}
    try:
        with ThreadPoolExecutor(len(QUOTE_SCENARIOS)) as pool:
            marks = dict(zip(QUOTE_SCENARIOS, pool.map(run, QUOTE_SCENARIOS)))
        time.sleep(0.3)
    finally:
        srv.close()
    return {name: ([(t, ln) for t, ln in got if ln.startswith(f"Q|{root}|")], marks[name])
            for name, root in QUOTE_SCENARIOS.items()}


def _between(lines, start, end):
    return [ln for t, ln in lines if start <= t < end]


@mono
@pytest.mark.parametrize("name", list(QUOTE_SCENARIOS))
def test_addon_quotes_go_once_a_second_while_its_market_data_comes(quote_runs, name):
    lines, marks = quote_runs[name]
    live = _between(lines, marks["live"], marks[name])
    assert len(live) >= 2, lines                                           # once a second
    last, prev = live[-1].split("|")[2:4]
    assert float(last) > 110.5 and prev == "110.25"   # its last trade, the prior close


@mono
def test_addon_quotes_stop_once_its_market_data_is_silent_and_come_back_with_it(quote_runs):
    """NinjaTrader's data connection lost, or the market shut for the weekend: no market data comes, and the last price
    is no longer sent as a fresh quote once the limit passes (here 2.5 s; a Q line a second after it at most), so the
    server's next source takes over. When the data comes again, so do the Q lines."""
    lines, m = quote_runs["silent"]
    assert _between(lines, m["silent"] + 4000, m["again"]) == []
    assert len(_between(lines, m["again"], m["end"] + 300)) >= 2


@mono
@pytest.mark.parametrize("name", ["playback", "dropped"])
def test_addon_quotes_stop_at_once_with_market_replay_or_without_a_live_data_connection(quote_runs, name):
    """Market Replay connected (its data keeps coming, none of it live), or NinjaTrader's live data connection dropped:
    no Q line from the next second on, well within the silence limit."""
    lines, m = quote_runs[name]
    assert _between(lines, m[name] + 300, m["end"] + 300) == []


# ---- r13-feeds-2: two keys of one IBKR contract (US10Y and TNX) both quoted, one dropped without the other -----------

TWS = {"ibkr_host": "127.0.0.1", "ibkr_port": 7497, "ibkr_client_id": 1, "ibkr_market_data_type": 1}


def tws_ib(con_id=lambda c: 13455763):
    """ib_async's own IB (its tickers and request ids as they are), with TWS's side stood in: it is connected, every
    contract qualifies with con_id(contract) (by default as Cboe's TNX index: US10Y and TNX are one contract, one
    conId), and the requests it would send are kept in `sent`."""
    from ib_async import IB

    class TwsIB(IB):
        async def connectAsync(self, *a, **k):
            return self

        def reqMarketDataType(self, t):
            pass

        def isConnected(self):
            return True

        def disconnect(self):
            pass

        async def qualifyContractsAsync(self, *cs):
            for c in cs:
                c.conId = con_id(c)
            return list(cs)

    def make():
        ib = TwsIB()
        ids = iter(range(1, 1000))
        ib.sent = []
        ib.client.getReqId = lambda: next(ids)
        ib.client.reqMktData = lambda req_id, c, *a: ib.sent.append(("req", req_id, c.symbol))
        ib.client.cancelMktData = lambda req_id: ib.sent.append(("cancel", req_id))
        return ib
    return make


def test_ibkr_quotes_both_keys_of_one_contract_and_drops_one_without_the_other(monkeypatch):
    """The default Main list holds TNX and US10Y, both Cboe's TNX at IBKR: one market data line for both, and each
    tick quotes both. The TNX row removed: US10Y still quoted, its line kept; the last of them removed: the line is
    cancelled."""
    import asyncio
    from types import SimpleNamespace

    import ib_async

    from chartsrv.feeds.ibkr import IBKRQuotes
    made, make = [], tws_ib()
    monkeypatch.setattr(ib_async, "IB", lambda: made.append(make()) or made[-1])

    class Market:
        watch_keys = {"TNX", "US10Y"}
        quoted = []

        def on_quote(self, sym, last, prev, vol=None, source=None):
            self.quoted.append((sym, round(last, 6)))

    def tick(ib, price):
        """TWS's last price for each of the requests it was sent and not cancelled, as ib_async reads it."""
        live = {r[1] for r in ib.sent if r[0] == "req"} - {r[1] for r in ib.sent if r[0] == "cancel"}
        m.quoted.clear()
        ib.wrapper.tcpDataArrived()
        for req_id in live:
            ib.wrapper.priceSizeTick(req_id, 4, price, 1)
        ib.wrapper.tcpDataProcessed()
        return sorted(m.quoted)

    async def go():
        feed = IBKRQuotes(SimpleNamespace(**TWS))
        task = asyncio.create_task(feed.run(m))
        for _ in range(20):
            await asyncio.sleep(0)
        ib = made[0]
        assert [r[0] for r in ib.sent] == ["req"]                           # one line for the one contract
        assert tick(ib, 42.5) == [("TNX", 4.25), ("US10Y", 4.25)]
        assert feed.stats["symbols"] == 2
        m.watch_keys = {"US10Y"}                                             # the TNX row removed
        feed.watch_changed(m)
        for _ in range(20):
            await asyncio.sleep(0)
        assert [r[0] for r in ib.sent] == ["req"]                           # US10Y's line kept
        assert tick(ib, 42.6) == [("US10Y", 4.26)]
        m.watch_keys = {"TNX", "US10Y"}                                      # back: no second line
        feed.watch_changed(m)
        for _ in range(20):
            await asyncio.sleep(0)
        assert [r[0] for r in ib.sent] == ["req"] and tick(ib, 42.7) == [("TNX", 4.27), ("US10Y", 4.27)]
        m.watch_keys = set()                                                 # both gone: the line is freed
        feed.watch_changed(m)
        for _ in range(20):
            await asyncio.sleep(0)
        assert [r[0] for r in ib.sent] == ["req", "cancel"] and ib.sent[1][1] == ib.sent[0][1]
        assert tick(ib, 42.8) == []
        task.cancel()
    m = Market()
    asyncio.run(go())


# ---- r13-feeds-3: IBKR's Cboe yield indexes, at ten times the yield, in percent at every level -----------------------

def test_ibkr_yield_history_is_in_percent_under_two_percent_and_across_it():
    """US10Y's weekly bars as IBKR sends Cboe's TNX (ten times the yield): 2020's 0.55% (5.5), a week crossing 2%
    (19.6 to 20.6) and today's 4.25% (42.5) all come in percent, each bar in one scale; IRX's 0.04% of 2021 too.
    The minimum tick is a tenth as well (its decimals)."""
    import asyncio
    from datetime import date
    from types import SimpleNamespace as NS

    from chartsrv.ext import ExtBars
    from chartsrv.feeds.ibkr import IBKRQuotes
    rows = {"US10Y": [(date(2020, 8, 3), 5.5, 5.8, 5.0, 5.6), (date(2022, 2, 14), 19.6, 20.6, 19.3, 20.4),
                      (date(2026, 10, 26), 42.1, 42.6, 41.8, 42.5)],
            "IRX": [(date(2021, 6, 7), 0.4, 0.5, 0.3, 0.4), (date(2026, 10, 26), 41.0, 41.5, 40.5, 41.2)],
            "SPX": [(date(2026, 10, 26), 6800.0, 6850.5, 6790.0, 6840.25)]}

    class IB:
        def isConnected(self):
            return True

        async def reqHistoricalDataAsync(self, c, *a, **k):
            return [NS(date=d, open=o, high=h, low=lo, close=cl, volume=0) for d, o, h, lo, cl in rows[c.key]]
    feed = IBKRQuotes(NS())
    feed.ib = IB()

    async def details(ib, key):
        return NS(contract=NS(secType="IND", key=key), longName="", minTick=0.01)
    feed._details = details

    def bars(key):
        res = asyncio.run(feed.history(key, "1 week", "20 Y"))
        return res["tick"], [tuple(round(b[k], 6) for k in "ohlc") for b in ExtBars._ib_bars(res, "W")]
    assert bars("US10Y") == (0.001, [(0.55, 0.58, 0.5, 0.56), (1.96, 2.06, 1.93, 2.04), (4.21, 4.26, 4.18, 4.25)])
    assert bars("IRX") == (0.001, [(0.04, 0.05, 0.03, 0.04), (4.1, 4.15, 4.05, 4.12)])
    assert bars("SPX") == (0.01, [(6800.0, 6850.5, 6790.0, 6840.25)])   # not a yield: as it comes


def test_ibkr_yield_quote_and_its_previous_close_in_one_scale(monkeypatch):
    """A 10-year yield of 2.01% after a close of 1.99% (IBKR: 20.1 and 19.9): +0.02, not 19.9 for the close; a 0.45%
    13-week bill yield (4.5) is 0.45, not 4.5."""
    import asyncio
    from types import SimpleNamespace

    import ib_async

    from chartsrv.feeds.ibkr import IBKRQuotes, per_cent
    assert [round(per_cent(k, v), 6) for k, v in [("US10Y", 20.1), ("US10Y", 19.9), ("IRX", 4.5)]] == [2.01, 1.99, 0.45]
    made, make = [], tws_ib()
    monkeypatch.setattr(ib_async, "IB", lambda: made.append(make()) or made[-1])

    class Market:
        watch_keys = {"US10Y"}
        quoted = []

        def on_quote(self, sym, last, prev, vol=None, source=None):
            self.quoted.append((sym, round(last, 6), round(prev, 6)))

    async def go():
        feed = IBKRQuotes(SimpleNamespace(**TWS))
        task = asyncio.create_task(feed.run(m))
        for _ in range(20):
            await asyncio.sleep(0)
        ib = made[0]
        ib.wrapper.tcpDataArrived()
        ib.wrapper.priceSizeTick(ib.sent[0][1], 9, 19.9, 0)   # the previous close
        ib.wrapper.priceSizeTick(ib.sent[0][1], 4, 20.1, 1)
        ib.wrapper.tcpDataProcessed()
        task.cancel()
    m = Market()
    asyncio.run(go())
    assert m.quoted[-1] == ("US10Y", 2.01, 1.99)


# ---- r13-feeds-4: one Yahoo spark batch that fails keeps the quotes of the batches Yahoo answered --------------------

def yahoo_round(tmp_path, monkeypatch, fail):
    """45 Yahoo tickers (three spark batches of 20, 20 and 5), each answered at 10.0 unless `fail(batch number,
    request)` raises or answers otherwise. Returns (market, feed, one round's coroutine maker, the batches asked)."""
    from types import SimpleNamespace

    import httpx

    from chartsrv.feeds.yahoo import BATCH, YahooQuotes
    from test_review_fixes import build_market
    from test_yahoo import spark
    m, _ = build_market(tmp_path, monkeypatch)
    keys = [f"T{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(45)]
    m.wl = {"lists": [{"id": "a", "name": "A", "items": keys}]}
    m.watch_keys = set(keys)
    order = sorted(keys)
    asked = []

    def answer(req):
        part = req.url.params["symbols"].split(",")
        batch = order.index(part[0]) // BATCH + 1
        asked.append(batch)
        return fail(batch, req) or httpx.Response(200, json=spark({s: (10.0, 9.0, 100) for s in part}))
    y = YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=tmp_path), transport=httpx.MockTransport(answer))

    async def poll():
        async with httpx.AsyncClient(transport=y.transport) as http:
            return await y.poll(http, m)
    return m, y, poll, asked, order


def test_yahoo_batch_that_times_out_keeps_the_others_quotes(tmp_path, monkeypatch):
    """The second batch times out: the first and the third are delivered, the round counts, and the second's tickers
    are not taken for ones Yahoo has no price for (asked again next round). The Connections card says what failed."""
    import asyncio

    import httpx

    def fail(batch, req):
        if batch == 2:
            raise httpx.ReadTimeout("slow", request=req)
    m, y, poll, asked, order = yahoo_round(tmp_path, monkeypatch, fail)
    assert asyncio.run(poll()) == 25 and asked == [1, 2, 3]
    assert sorted(k for k, q in m.quotes.items() if q["src"] == "yahoo") == order[:20] + order[40:]
    assert not set(order[20:40]) & set(y.unknown)
    assert "ReadTimeout" in y.missed and "20 of 45" in y.missed


def test_yahoo_throttled_mid_round_delivers_what_came_and_asks_no_more(tmp_path, monkeypatch):
    """Yahoo answers 429 from the second batch on (both hosts): the first batch is delivered, the third is not asked
    this round, and neither is marked unknown."""
    import asyncio

    import httpx

    def fail(batch, req):
        return httpx.Response(429) if batch > 1 else None
    m, y, poll, asked, order = yahoo_round(tmp_path, monkeypatch, fail)
    assert asyncio.run(poll()) == 20 and asked == [1, 2, 2]
    assert sorted(k for k, q in m.quotes.items() if q["src"] == "yahoo") == order[:20]
    assert not set(order[20:]) & set(y.unknown) and "429" in y.missed


def test_yahoo_round_with_no_batch_answered_still_fails(tmp_path, monkeypatch):
    """Nothing came: the round fails as before (the feed backs off), and no ticker is marked unknown."""
    import asyncio

    import httpx

    m, y, poll, asked, order = yahoo_round(tmp_path, monkeypatch, lambda batch, req: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(poll())
    assert not y.unknown and not any(q["src"] == "yahoo" for q in m.quotes.values())


# ---- r13-feeds-5: a fresh install's lists are not swapped at its second start ---------------------------------------

def test_fresh_install_keeps_its_first_sessions_spot_names_at_the_next_start(tmp_path):
    """The once-only swap of spot names to futures (BRENT, PALLADIUM, XCUUSD, NK2251!) is for lists saved before it:
    a fresh install has nothing to swap, so what its user adds in the first session stays at the next start."""
    from chartsrv import watchlists as wl
    from chartsrv.store import Store
    store = Store(tmp_path / "chart.db")
    doc = wl.load(store, ROOT / "config")
    doc["lists"][0]["items"] += ["TVC:BRENT", "OANDA:XCUUSD"]
    doc["lists"].append({"id": "tv", "name": "TV import", "items": ["OSE:NK2251!", "TVC:PALLADIUM", "AAPL"]})
    store.set_setting("watchlists", {**wl.clean(doc), "version": wl.next_version(doc["version"])})
    again = wl.load(store, ROOT / "config")
    assert again["lists"][0]["items"][-2:] == ["TVC:BRENT", "OANDA:XCUUSD"]
    assert again["lists"][1]["items"] == ["OSE:NK2251!", "TVC:PALLADIUM", "AAPL"]


def test_lists_saved_before_the_swap_are_still_swapped_once(tmp_path):
    from chartsrv import watchlists as wl
    from chartsrv.store import Store
    store = Store(tmp_path / "chart.db")
    store.set_setting("watchlists", {"lists": [{"id": "a", "name": "A", "items": ["TVC:BRENT", "AAPL"]}], "version": 5})
    assert wl.load(store, ROOT / "config")["lists"][0]["items"] == ["BZ1!", "AAPL"]


# ---- r13-feeds-6: a quote TWS refuses (error 101, past the account's market data lines) is asked again ---------------

def test_ibkr_quote_refused_by_tws_is_not_counted_and_asked_again_when_lines_free_up(monkeypatch):
    """30 stocks at IBKR, TWS refusing the last 5 (101: max number of tickers). They are not counted as quoted, and
    when the lists lose 5 others (their lines freed) the refused ones are asked again on the same connection."""
    import asyncio
    from types import SimpleNamespace

    import ib_async

    from chartsrv.feeds.ibkr import IBKRQuotes
    keys = [f"S{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(30)]
    made, make = [], tws_ib(lambda c: 1000 + keys.index(c.symbol))
    monkeypatch.setattr(ib_async, "IB", lambda: made.append(make()) or made[-1])

    class Market:
        watch_keys = set(keys)

        def on_quote(self, *a, **k):
            pass

    async def settle():
        for _ in range(40):
            await asyncio.sleep(0)

    async def go():
        feed = IBKRQuotes(SimpleNamespace(**TWS))
        task = asyncio.create_task(feed.run(m))
        await settle()
        ib = made[0]
        assert [r[2] for r in ib.sent] == keys and feed.stats["symbols"] == 30
        for _, req_id, _ in ib.sent[25:]:
            ib.wrapper.error(req_id, 101, "Max number of tickers has been reached", "")
        assert feed.stats["symbols"] == 25
        m.watch_keys = set(keys[5:])                 # 5 dropped from the lists: their lines are free
        feed.watch_changed(m)
        await settle()
        assert [r for r in ib.sent[30:] if r[0] == "cancel"] == [("cancel", i) for i in range(1, 6)]
        assert [r[2] for r in ib.sent[30:] if r[0] == "req"] == keys[25:]
        assert feed.stats["symbols"] == 25
        ib.wrapper.error(ib.sent[-1][1], 2104, "Market data farm connection is OK:usfarm", "")   # a notice: no change
        assert feed.stats["symbols"] == 25
        task.cancel()
    m = Market()
    asyncio.run(go())
