"""A watchlist stock's change when its quotes come without a previous close, and a fast symbol's flash.

IBKR sends no close tick (9) for some tickers, so their quotes had a price and no Chg or Chg% (AAPL, MSFT, NVDA, QQQ
from TWS one fast morning, beside AVGO and TSLA with theirs). A quote without one now takes the previous close known
for its trading day (an earlier quote's, from any source, or the fill's), and one still without is asked of Yahoo's
daily bars, once per symbol per trading day, in batches, in the background (feeds/yahoo.py's CloseFill). A future's
change stays its settlement's. The watchlist's price flash restarts at most about once a second, so on a symbol that
ticks several times a second it fades between flashes (it stayed lit, as if the row were stuck). Market's quotes on a
real Market, IBKR's tickers ib_async's own (test_hunt13_feeds' tws_ib), web/watchlist.js in node (web_harness.js)."""
import asyncio
import shutil
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from chartsrv import market as market_mod
from chartsrv.feeds import yahoo
from test_hunt13_feeds import TWS, tws_ib
from test_hunt_web_ui import node
from test_review_fixes import build_market

NY = ZoneInfo("America/New_York")
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def ny_ms(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=NY).timestamp() * 1000)


def daily(closes: dict, price: float, at: int) -> dict:
    """A spark or chart response of daily bars: {NY date (y, m, d): close}, each stamped at its 09:30 ET open, as Yahoo
    stamps a US stock's; the latest price `price` at `at` (ms)."""
    days = sorted(closes)
    return {"meta": {"regularMarketPrice": price, "regularMarketTime": at // 1000, "gmtoffset": -18000,
                     "exchangeTimezoneName": "America/New_York"},
            "timestamp": [ny_ms(*d, 9, 30) // 1000 for d in days],
            "indicators": {"quote": [{"close": [closes[d] for d in days]}]}}


def spark_doc(responses: dict) -> dict:
    return {"spark": {"result": [{"symbol": s, "response": [r]} for s, r in responses.items()], "error": None}}


# Yahoo's daily closes the fill reads: Wednesday 4 November 2026 in progress (its last price), Tuesday's the one before
BARS = {"AAPL": {(2026, 10, 30): 266.0, (2026, 11, 2): 267.1, (2026, 11, 3): 268.5, (2026, 11, 4): 270.0},
        "MSFT": {(2026, 11, 2): 505.0, (2026, 11, 3): 507.25, (2026, 11, 4): 511.0},
        "NVDA": {(2026, 11, 3): 190.4, (2026, 11, 4): 195.0}}
WED = ny_ms(2026, 11, 4, 9, 56, 20)   # 08:56:20 CT, the morning in the report


def yahoo_http(bars=BARS, asked=None, fail=None):
    """An http client whose Yahoo spark answers come from `bars` (a ticker it has none for is left out, as Yahoo's
    spark does); each request's symbols go to `asked`; fail(request) may answer instead."""
    def answer(req):
        part = req.url.params["symbols"].split(",")
        if asked is not None:
            asked.append(part)
        if fail and (r := fail(req)) is not None:
            return r
        return httpx.Response(200, json=spark_doc({s: daily(bars[s], list(bars[s].values())[-1], WED) for s in part
                                                    if s in bars}))
    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


def at_clock(monkeypatch, start):
    clock = [start]
    monkeypatch.setattr(market_mod, "now_ms", lambda: clock[0])
    return clock


# ---- the previous close of a quote that has none ---------------------------------------------------------------------

def test_an_ibkr_quote_without_a_close_tick_gets_its_change_from_the_filled_previous_close(tmp_path, monkeypatch):
    """AAPL from TWS with no close tick (its ticker's close NaN), AVGO with one: AAPL had a price and no change. The
    fill asks Yahoo for AAPL alone and its close before today (Tuesday's 268.50) gives the change at once; the price,
    its source and time stay IBKR's. Later quotes keep it, with no new request; IBKR's own close, when it comes, counts
    from then on."""
    import ib_async
    from chartsrv.feeds.ibkr import IBKRQuotes
    at_clock(monkeypatch, WED)
    asked = []
    m, _ = build_market(tmp_path, monkeypatch, http=yahoo_http(asked=asked))
    m.watch_keys = {"AAPL", "AVGO"}
    made, make = [], tws_ib(lambda c: {"AAPL": 265598, "AVGO": 349576}[c.symbol])
    monkeypatch.setattr(ib_async, "IB", lambda: made.append(make()) or made[-1])

    def ticks(ib, *its):
        """TWS's ticks (key, tick type, price) as ib_async reads them: one pending-tickers event."""
        req = {r[2]: r[1] for r in ib.sent if r[0] == "req"}
        ib.wrapper.tcpDataArrived()
        for key, kind, price in its:
            ib.wrapper.priceSizeTick(req[key], kind, price, 100)
        ib.wrapper.tcpDataProcessed()

    async def go():
        feed = IBKRQuotes(SimpleNamespace(**TWS))
        task = asyncio.create_task(feed.run(m))
        for _ in range(20):
            await asyncio.sleep(0)
        ib = made[0]
        ticks(ib, ("AVGO", 9, 350.0), ("AVGO", 4, 352.1), ("AAPL", 4, 270.12))   # AAPL: no tick 9, ever
        out = {"before": (m.quotes["AAPL"]["prev"], m.quotes["AAPL"]["chg"]), "avgo": m.quotes["AVGO"]["prev"]}
        m.quotes_dirty.clear()
        out["filled"] = await m.close_fill.ask()
        q = m.quotes["AAPL"]
        out["after"] = (q["last"], q["prev"], round(q["chg"], 2), round(q["pct"], 2), q["src"], q["ts"], q["rt"])
        out["sent"] = "AAPL" in m.quotes_dirty
        ticks(ib, ("AAPL", 4, 270.5))
        out["next"] = (m.quotes["AAPL"]["prev"], round(m.quotes["AAPL"]["chg"], 2))
        out["again"] = await m.close_fill.ask()
        ticks(ib, ("AAPL", 9, 268.25), ("AAPL", 4, 270.75))   # IBKR's own close at last
        out["own"] = (m.quotes["AAPL"]["prev"], round(m.quotes["AAPL"]["chg"], 2))
        ticks(ib, ("AAPL", 4, 271.0))
        out["kept"] = m.quotes["AAPL"]["prev"]
        task.cancel()
        return out
    out = asyncio.run(go())
    assert out["before"] == (None, None) and out["avgo"] == 350.0
    assert out["filled"] == 1 and asked == [["AAPL"]]
    assert out["after"] == (270.12, 268.5, 1.62, 0.6, "ibkr", WED, "live") and out["sent"]
    assert out["next"] == (268.5, 2.0) and out["again"] == 0 and asked == [["AAPL"]]
    assert out["own"] == (268.25, 2.5) and out["kept"] == 268.25


def test_a_quote_without_a_previous_close_takes_the_one_known_for_its_trading_day(tmp_path, monkeypatch):
    """MSFT quoted by Yahoo first (its close before today, 507.25), then by IBKR without one: IBKR's price, Yahoo's
    previous close, nothing asked. QQQ from Alpaca the same, IWM from IBKR alone: none known, so it is asked for."""
    clock = at_clock(monkeypatch, WED)
    m, _ = build_market(tmp_path, monkeypatch, http=yahoo_http())
    m.on_quote("MSFT", 510.0, 507.25, None, source="yahoo", at=WED - 60_000)
    m.on_quote("QQQ", 640.0, 636.0, None, source="alpaca", at=WED - 2_000)
    clock[0] += 121_000   # Alpaca silent long enough for IBKR (the same rank) to take QQQ over
    m.on_quote("MSFT", 511.5, None, 1e6, source="ibkr")
    m.on_quote("QQQ", 641.0, float("nan"), None, source="ibkr")
    m.on_quote("IWM", 250.0, None, None, source="ibkr")
    q = m.quotes
    assert (q["MSFT"]["src"], q["MSFT"]["last"], q["MSFT"]["prev"], round(q["MSFT"]["chg"], 2)) == ("ibkr", 511.5, 507.25, 4.25)
    assert (q["QQQ"]["src"], q["QQQ"]["prev"], round(q["QQQ"]["chg"], 2)) == ("ibkr", 636.0, 5.0)
    assert q["IWM"]["chg"] is None and list(m.close_fill.queue) == ["IWM"]


def test_a_new_trading_day_does_not_reuse_yesterdays_previous_close(tmp_path, monkeypatch):
    """A stock's trading day runs from midnight New York (its pre-market, session and after-hours one day): MSFT's
    Wednesday reference still counts at 23:59 ET, not at 00:00:30 Thursday, when it is asked for again (Thursday's).
    Yahoo's quote before Thursday's open is Wednesday's close against Tuesday's: that is not taken for IBKR's Thursday
    pre-market price either."""
    clock = at_clock(monkeypatch, WED)
    m, _ = build_market(tmp_path, monkeypatch, http=yahoo_http())
    m.on_quote("MSFT", 510.0, 507.25, None, source="yahoo", at=WED - 60_000)
    clock[0] = ny_ms(2026, 11, 4, 23, 59)
    m.on_quote("MSFT", 512.0, None, None, source="ibkr")
    late = (m.quotes["MSFT"]["prev"], round(m.quotes["MSFT"]["chg"], 2))
    clock[0] = ny_ms(2026, 11, 5, 0, 0, 30)
    m.on_quote("MSFT", 512.1, None, None, source="ibkr")
    thu = m.quotes["MSFT"]
    assert late == (507.25, 4.75) and (thu["prev"], thu["chg"], thu["pct"]) == (None, None, None)
    assert m.close_fill.queue["MSFT"][0] == date(2026, 11, 5)

    # Thursday 08:00 ET: Yahoo's latest price is still Wednesday's 16:00 close (511.0), its previous close Tuesday's
    clock[0] = ny_ms(2026, 11, 5, 8, 0)
    m.quote_from.clear()
    m.on_quote("NVDA", 195.0, 190.4, None, source="yahoo", at=ny_ms(2026, 11, 4, 16, 0))
    m.on_quote("NVDA", 196.2, None, None, source="ibkr")
    assert m.quotes["NVDA"]["prev"] is None and "NVDA" in m.close_fill.queue


def test_the_fill_is_asked_once_per_symbol_per_trading_day_in_batches(tmp_path, monkeypatch):
    """Seven IBKR stocks without a close, quoted again and again: one request for all seven. NVDA's answer gives it;
    QQQ, which Yahoo answered nothing for, is not asked again that day. A failed request (Yahoo 503 on both hosts)
    puts its symbols back for the next. The next trading day each is asked once more."""
    clock = at_clock(monkeypatch, WED)
    asked, fail = [], [True]
    m, _ = build_market(tmp_path, monkeypatch, http=yahoo_http(asked=asked, fail=lambda r: httpx.Response(503) if fail[0] else None))
    keys = ["AAPL", "AMZN", "META", "MSFT", "NVDA", "QQQ", "IWM"]

    async def go():
        for n in range(5):
            for k in keys:
                m.on_quote(k, 100.0 + n, None, None, source="ibkr")
        with pytest.raises(httpx.HTTPStatusError):
            await m.close_fill.ask()
        out = {"failed": len(asked), "back": sorted(m.close_fill.queue)}
        fail[0] = False
        out["filled"] = await m.close_fill.ask()
        out["after"] = len(asked)
        for k in keys:
            m.on_quote(k, 110.0, None, None, source="ibkr")
        out["again"] = (await m.close_fill.ask(), len(asked))
        clock[0] = ny_ms(2026, 11, 5, 9, 31)
        for k in keys:
            m.on_quote(k, 111.0, None, None, source="ibkr")
            m.on_quote(k, 111.5, None, None, source="ibkr")
        await m.close_fill.ask()
        out["thu"] = sorted(asked[-1])
        return out
    out = asyncio.run(go())
    assert out["failed"] == 2 and asked[0] == asked[1] == sorted(keys) and out["back"] == sorted(keys)   # query1, query2
    assert out["filled"] == 3 and out["after"] == 3 and asked[2] == sorted(keys)   # AAPL, MSFT, NVDA in Yahoo's bars
    assert out["again"] == (0, 3)
    assert {k: m.quotes[k]["prev"] for k in ("AAPL", "MSFT", "NVDA")} == {"AAPL": 270.0, "MSFT": 511.0, "NVDA": 195.0}
    assert out["thu"] == sorted(keys)


def test_the_fill_runs_in_the_background_twenty_symbols_a_request(tmp_path, monkeypatch):
    """25 stocks without a close: the background task asks Yahoo in two requests, 20 and 5, a pause between; on_quote
    itself sends nothing."""
    at_clock(monkeypatch, WED)
    asked = []
    m, _ = build_market(tmp_path, monkeypatch, http=yahoo_http(asked=asked))
    monkeypatch.setattr(yahoo, "FILL_GATHER_S", 0)
    monkeypatch.setattr(yahoo, "FILL_EVERY_S", 0)
    keys = [f"S{chr(65 + i)}" for i in range(25)]

    async def go():
        task = asyncio.create_task(m.close_fill.run())
        for k in keys:
            m.on_quote(k, 50.0, None, None, source="ibkr")
        quoted = len(asked)
        for _ in range(50):
            await asyncio.sleep(0)
        task.cancel()
        return quoted
    assert asyncio.run(go()) == 0
    assert [len(a) for a in asked] == [20, 5] and sorted(k for a in asked for k in a) == keys


def test_futures_keep_their_settlement_and_are_never_filled(tmp_path, monkeypatch):
    """A future's change is from its settlement, else the source's previous close, as before: an IBKR quote of ZS1!
    without one takes no other source's, nor is it asked of Yahoo; ES1! is against its settlement whatever the quote
    says. Currency pairs and crypto take a close known for their day (17:00 New York, 00:00 UTC) and are not asked."""
    at_clock(monkeypatch, WED)
    m, _ = build_market(tmp_path, monkeypatch, http=yahoo_http())
    monkeypatch.setattr(m.settlements, "reference",
                        lambda root, now=None: {"price": 6800.0, "t": WED - 3_600_000, "src": "trades"} if root == "ES" else None)
    m.on_quote("ZS1!", 1050.0, 1040.0, None, source="yahoo", at=WED)
    m.on_quote("ZS1!", 1051.0, None, None, source="ibkr")
    m.on_quote("ES1!", 6850.0, None, None, source="ibkr")
    m.on_quote("ES1!", 6851.0, 6790.0, None, source="ibkr")
    m.on_quote("BTCUSD", 101_000.0, 100_000.0, None, source="coinbase", at=WED)
    m.quote_from.clear()
    m.on_quote("BTCUSD", 101_500.0, None, None, source="ibkr")
    m.on_quote("EURUSD", 1.16, None, None, source="ibkr")
    q = m.quotes
    assert (q["ZS1!"]["prev"], q["ZS1!"]["chg"]) == (None, None)
    assert (q["ES1!"]["prev"], q["ES1!"]["chg"], q["ES1!"]["settle_t"]) == (6800.0, 51.0, WED - 3_600_000)
    assert q["BTCUSD"]["prev"] == 100_000.0 and q["EURUSD"]["prev"] is None
    assert not m.close_fill.queue


def test_the_close_before_a_quotes_trading_day_from_yahoos_daily_bars():
    """The last daily close before the quote's own day, in the exchange's days: Tuesday's during Wednesday's
    pre-market, session and after-hours; Friday's over the weekend; none when every bar is the day's own."""
    resp = daily({(2026, 10, 30): 266.0, (2026, 11, 2): 267.1, (2026, 11, 3): 268.5, (2026, 11, 4): 270.0}, 270.0, WED)
    assert yahoo.close_before(resp, ny_ms(2026, 11, 4, 4, 0)) == 268.5
    assert yahoo.close_before(resp, ny_ms(2026, 11, 4, 19, 59)) == 268.5
    assert yahoo.close_before(resp, ny_ms(2026, 11, 5, 0, 1)) == 270.0
    assert yahoo.close_before(resp, ny_ms(2026, 11, 1, 12, 0)) == 266.0    # Sunday: Friday's
    assert yahoo.close_before(resp, ny_ms(2026, 10, 30, 12, 0)) is None
    resp["indicators"]["quote"][0]["close"][2] = None                     # a row Yahoo left empty: the one before
    assert yahoo.close_before(resp, ny_ms(2026, 11, 4, 10, 0)) == 267.1
    assert yahoo.market_day("AAPL", ny_ms(2026, 11, 4, 23, 59)) == date(2026, 11, 4)
    assert yahoo.market_day("EURUSD", ny_ms(2026, 11, 4, 17, 1)) == date(2026, 11, 5)   # the 17:00 roll
    utc = int(datetime(2026, 11, 4, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)
    assert yahoo.market_day("BTCUSD", utc) == date(2026, 11, 4)
    assert yahoo.market_day("BTCUSD", utc + 120_000) == date(2026, 11, 4) + timedelta(days=1)


# ---- the flash, at most once a second --------------------------------------------------------------------------------

@needs_node
def test_a_fast_symbols_price_flash_restarts_at_most_about_once_a_second():
    """AAPL ticking every 200 ms: the first change flashes; the next four (within 900 ms) only change the number and the
    change's color; the one a second after flashes again (the other animation name, so it restarts), the way that
    tick went. NVDA, ticking slowly, flashes on each tick."""
    out = node("""
let clock = 1000;
const q = (s, last, prev) => ({ s, last, prev, chg: last - prev, pct: (last - prev) / prev * 100, ts: 1, rt: 'live', src: 'ibkr' });
const quotes = { AAPL: q('AAPL', 270, 268.5), NVDA: q('NVDA', 195, 190.4) };
const w = makeWatchlist(WEB, { quotes, now: () => clock });
w.list.setDoc({ lists: [{ id: 'm', name: 'M', items: ['AAPL', 'NVDA'] }], flags: {}, names: {}, version: 1 });
const body = w.root.querySelector('.wl-body');
const row = (s) => body.children.find((e) => e.querySelector('.tk').textContent === s);
const fl = (s) => ['fl-up', 'fl-dn', 'fl-up2', 'fl-dn2'].filter((c) => row(s).querySelector('.last').classList.contains(c));
const tick = (s, last, dt) => { clock += dt; quotes[s] = q(s, last, quotes[s].prev); w.list.onQuotes([quotes[s]]);
  return [row(s).querySelector('.last').textContent, fl(s), row(s).querySelector('.chg').className]; };
const aapl = [tick('AAPL', 270.1, 200), tick('AAPL', 270.2, 200), tick('AAPL', 270.05, 200), tick('AAPL', 268.4, 200),
  tick('AAPL', 268.3, 200), tick('AAPL', 268.2, 200)];
const nvda = [tick('NVDA', 195.5, 1500), tick('NVDA', 195.2, 1500)];
out({ aapl, nvda });""")
    a = out["aapl"]
    assert a[0] == ["270.10", ["fl-up2"], "c chg up"]
    assert [x[1] for x in a[1:5]] == [["fl-up2"]] * 4                       # within 900 ms of it: no restart
    assert [x[0] for x in a[1:5]] == ["270.20", "270.05", "268.40", "268.30"]  # ...but the number moves
    assert a[3][2] == "c chg down"                                           # ...and so does the change's color
    assert a[5] == ["268.20", ["fl-dn"], "c chg down"]                       # a second after the first: flashes again
    assert [x[1] for x in out["nvda"]] == [["fl-up2"], ["fl-dn"]]
