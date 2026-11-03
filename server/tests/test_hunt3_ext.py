"""Bug-hunt fixes in the free-data charts (ext.py and the chart page around them): index charts with ETH counted from
the regular open, Yahoo's hourly bars kept for what has no pre-market, the bar cache dropping old answers, crypto's N
days as calendar days, the live quote kept off the bar before on monthly and N-day charts (and on crypto's to 00:00
UTC), and D/W/M pivots read on the trade date the bars are stamped with."""
import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from chartsrv import ext
from chartsrv.sessions import day_open, to_ct
from test_ext_sources import FakeIB
from test_hunt_web_chart import WEB, needs_node, run_app
from test_review_fixes import build_market

NY = ZoneInfo("America/New_York")


def ms(*a, tz=NY) -> int:
    return int(datetime(*a, tzinfo=tz).timestamp() * 1000)


def hm(d: dict) -> list[str]:
    return [datetime.fromtimestamp(b["t"] / 1000, NY).strftime("%H:%M") for b in d["bars"]]


def chart_doc(ts: list[int], meta: dict) -> dict:
    c = [100.0 + i for i in range(len(ts))]
    return {"chart": {"result": [{"meta": meta, "timestamp": ts, "indicators": {"quote": [
        {"open": c, "high": c, "low": c, "close": c, "volume": [1] * len(ts)}]}}]}}


# ---- an index has no pre- or post-market: its bars count from 9:30 with ETH too, from Yahoo's hourly bars ----------

def test_index_with_eth_counts_from_its_open_and_keeps_hourly_bars(tmp_path, monkeypatch):
    open_s = ms(2026, 10, 20, 9, 30) // 1000
    reg = {"regular": {"start": open_s, "end": open_s + 23_400}}
    metas = {"^GSPC": {"instrumentType": "INDEX", "exchangeTimezoneName": "America/New_York", "currentTradingPeriod": reg},
             "AAPL": {"instrumentType": "EQUITY", "exchangeTimezoneName": "America/New_York", "currentTradingPeriod": reg},
             "BTC-USD": {"instrumentType": "CRYPTOCURRENCY", "exchangeTimezoneName": "UTC"},
             "7203.T": {"instrumentType": "EQUITY", "exchangeTimezoneName": "Asia/Tokyo"}}
    asked = []

    def answer(req):
        y, iv = req.url.path.rsplit("/", 1)[-1], req.url.params["interval"]
        asked.append((y, iv, req.url.params["range"]))
        step = 1800 if iv == "30m" else 3600   # 9:30 to 16:00, as Yahoo has an index (60m: the last one 15:30)
        return httpx.Response(200, json=chart_doc([open_s + i * step for i in range(-(-23_400 // step))], metas[y]))
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    m.symbols._put([("7203.T", "Toyota Motor Corp", "stock", "Tokyo", "7203.T", 1)], "yahoo")

    async def go():
        for rth in (True, False):   # the same bars either way: 9:30, 11:30, ... from Yahoo's hourly bars (2 years)
            assert hm(await m.ext.bars("SPX", "2h", rth)) == ["09:30", "11:30", "13:30", "15:30"]
            assert asked[-1] == ("^GSPC", "60m", "730d")
            assert hm(await m.ext.bars("SPX", "4h", rth)) == ["09:30", "13:30"]
        assert hm(await m.ext.bars("SPX", "90m", rth=False))[:3] == ["09:30", "11:00", "12:30"]
        for sym, y in (("BTCUSD", "BTC-USD"), ("7203.T", "7203.T")):   # no pre-market to line up with: hourly bars
            await m.ext.bars(sym, "4h", rth=False)
            assert asked[-1] == (y, "60m", "730d")
        await m.ext.bars("AAPL", "2h", rth=False)   # a US stock's ETH hours still come from 30 minutes
        assert asked[-1] == ("AAPL", "30m", "60d")
    asyncio.run(go())


def test_ibkr_index_with_eth_counts_from_its_open(tmp_path, monkeypatch):
    utc = lambda t: datetime.fromtimestamp(t / 1000, timezone.utc)   # noqa: E731

    def ib(key, size, span):   # IBKR has regular hours only for an index: 30-minute bars from 9:30
        rows = [(utc(ms(2026, 10, 20, 9, 30) + i * 1_800_000), 10, 11, 9, 10, 0) for i in range(13)] if size == "30 mins" else []
        return {"kind": "IND", "name": "S&P 500", "tick": 0.01, "rows": rows}
    fake = FakeIB(ib)
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    m.feed_objs.append(fake)

    async def go():
        for rth in (True, False):
            d = await m.ext.bars("SPX", "1h", rth)
            assert d["source"] == "ibkr" and fake.asked[-1][1:] == ("30 mins", "1 Y", True)
            assert hm(d) == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
            assert hm(await m.ext.bars("SPX", "2h", rth)) == ["09:30", "11:30", "13:30", "15:30"]
    asyncio.run(go())


# ---- the bar cache: an expired answer stays a short while as the stale fallback, then goes -----------------------

def test_bar_cache_drops_answers_long_expired(tmp_path, monkeypatch):
    down = []

    def answer(req):
        if down:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=chart_doc([ms(2026, 10, d, 9, 30) // 1000 for d in (19, 20, 21)], {}))
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    c = m.ext.cache

    async def go():
        for i in range(4):   # the watchlist's details panel, symbol after symbol: a 20-year daily answer each
            await m.ext._yahoo(f"T{i}", "1D")
        now = time.monotonic()
        for i in range(4):   # T0-T2 expired over half an hour ago, T3 a minute ago
            c[(f"T{i}", "1D")] = (now - (60 if i == 3 else ext.STALE_S + 60), c[(f"T{i}", "1D")][1])
        await m.ext._yahoo("T4", "1D")
        assert sorted(k[0] for k in c) == ["T3", "T4"]   # not kept for the life of the server
        down.append(1)
        assert len((await m.ext._yahoo("T3", "1D"))[0]) == 3   # Yahoo down: the stale answer still beats none
        with pytest.raises(ValueError):
            await m.ext._yahoo("T0", "1D")
        down.clear()
        monkeypatch.setattr(ext, "CACHE_MAX", 2)   # full: the one expiring soonest goes
        await m.ext._yahoo("T5", "1D")
        assert sorted(k[0] for k in c) == ["T4", "T5"]
    asyncio.run(go())


# ---- crypto trades every day: its N-day bars are N calendar days, not N weekdays with the weekend in Friday's -------

def test_crypto_n_days_are_calendar_days(tmp_path, monkeypatch):
    days = [ms(2026, 10, 5, tz=timezone.utc) // 1000 + 86_400 * i for i in range(14)]   # Mon Oct 5 - Sun Oct 18, 00:00 UTC
    meta = {"instrumentType": "CRYPTOCURRENCY", "exchangeTimezoneName": "UTC"}
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=chart_doc(days, meta)))))
    got = lambda d: [(f"{(to_ct(b['t']) + timedelta(hours=7)).date():%a %d}", b["v"]) for b in d["bars"]]   # noqa: E731
    two = asyncio.run(m.ext.bars("BTCUSD", "2D"))
    assert two["exchange"] == ext.IB_CLOCK["CRYPTO"]
    assert got(two) == [("Sun 04", 1), ("Tue 06", 2), ("Thu 08", 2), ("Sat 10", 2), ("Mon 12", 2), ("Wed 14", 2),
                        ("Fri 16", 2), ("Sun 18", 1)]   # from fixed starts, as every chart's N days
    assert got(asyncio.run(m.ext.bars("BTCUSD", "3D"))) == [("Mon 05", 3), ("Thu 08", 3), ("Sun 11", 3), ("Wed 14", 3), ("Sat 17", 2)]


# ---- the live quote moves a free-data chart's last bar only while that bar's period lasts -----------------------

QUOTE_JS = """
const NY = { tz: 'America/New_York', rth: ['09:30', '16:00'], day: '00:00' }, UTC = { tz: 'UTC', rth: null, day: '00:00' };
const TZ_EXCHANGE = 'America/Chicago', S = { rth: false, replay: null, symbol: 'X', bars: [] };
const wlKey = (s) => s, exch = () => S.ext.exchange, applyBars = (m) => { S.bars[S.bars.length - 1] = m.bars[0]; };
/*APP*/
const { toInput: msToCtInput, toMs: ctInputToMs } = inputClock(TZ_EXCHANGE);
const took = (spec, exchange, t, ts) => {   // does a quote at ts (UTC) move the bar opening at t?
  Object.assign(S, { spec, ext: { source: 'yahoo', exchange }, bars: [{ seq: 1, t, o: 10, h: 11, l: 9, c: 10 }] });
  extQuote([{ s: 'X', last: 20, ts }]);
  return S.bars[0].c === 20;
};
const at = (...a) => Date.UTC(...a);
console.log(JSON.stringify([
  // AAPL 1M: September's bar opens Aug 31 17:00 CT; Oct 1 is October's, not a 31st day of September
  took('1M', NY, at(2026, 7, 31, 22), at(2026, 8, 30, 19)), took('1M', NY, at(2026, 7, 31, 22), at(2026, 9, 1, 13, 35)),
  took('3M', NY, at(2026, 5, 30, 22), at(2026, 8, 30, 19)), took('3M', NY, at(2026, 5, 30, 22), at(2026, 9, 1, 15)),
  // 2D: [Tue Oct 6, Wed Oct 7] opens Mon 17:00 CT; Thursday 9:31 ET is the next bar's
  took('2D', NY, at(2026, 9, 5, 22), at(2026, 9, 7, 14)), took('2D', NY, at(2026, 9, 5, 22), at(2026, 9, 8, 13, 31)),
  // crypto 2D: two calendar days, [Oct 6, Oct 7]; Oct 9 is two bars on
  took('2D', UTC, at(2026, 9, 5, 22), at(2026, 9, 7, 12)), took('2D', UTC, at(2026, 9, 5, 22), at(2026, 9, 9, 12)),
  // 1D and 1W as they were: the day and the week
  took('1D', NY, at(2026, 9, 5, 22), at(2026, 9, 6, 14)), took('1D', NY, at(2026, 9, 5, 22), at(2026, 9, 7, 14)),
  took('1W', NY, at(2026, 9, 4, 22), at(2026, 9, 9, 14)), took('1W', NY, at(2026, 9, 4, 22), at(2026, 9, 12, 14)),
]));
"""
QUOTE_PARTS = ["calSpec", "specSeconds", "inputClock", "wallClock", "extWall", "tradeDate", "extBarEnd", "extQuote"]


@needs_node
def test_quote_stays_off_the_bar_before_on_month_and_n_day_charts():
    assert run_app(QUOTE_PARTS, QUOTE_JS) == [True, False, True, False, True, False, True, False, True, False, True, False]


CRYPTO_QUOTE_JS = QUOTE_JS.split("console.log(")[0] + """
console.log(JSON.stringify([
  // crypto 3D [Oct 5-7] opens Oct 4 17:00 CT; its last day (UTC) runs to 00:00 UTC Oct 8, 19:00 CDT
  took('3D', UTC, at(2026, 9, 4, 22), at(2026, 9, 7, 22, 30)), took('3D', UTC, at(2026, 9, 4, 22), at(2026, 9, 7, 23, 59)),
  took('3D', UTC, at(2026, 9, 4, 22), at(2026, 9, 8, 0, 1)),
  // 1M: October to 00:00 UTC Nov 1
  took('1M', UTC, at(2026, 8, 30, 22), at(2026, 9, 31, 23)), took('1M', UTC, at(2026, 8, 30, 22), at(2026, 10, 1, 0, 1)),
  // 1D as daylight time ends (Sun Nov 1): Nov 1 opens Oct 31 17:00 CDT and ends at 00:00 UTC Nov 2, 18:00 CST
  took('1D', UTC, at(2026, 9, 31, 22), at(2026, 10, 1, 23)), took('1D', UTC, at(2026, 9, 31, 22), at(2026, 10, 2, 0, 1)),
  // 1W: Monday Oct 5 to Sunday Oct 11
  took('1W', UTC, at(2026, 9, 4, 22), at(2026, 9, 11, 23, 30)), took('1W', UTC, at(2026, 9, 4, 22), at(2026, 9, 12, 0, 1)),
]));
"""


@needs_node
def test_quote_moves_a_crypto_day_week_or_month_bar_to_midnight_utc():
    """Crypto's day bars are UTC days (Yahoo's, keyed by the UTC date): a quote after the 17:00 CT open stamp of the
    next day but before 00:00 UTC still moves the last day, week, month or N-day bar; one after midnight does not."""
    assert run_app(QUOTE_PARTS, CRYPTO_QUOTE_JS) == [True, True, False, True, False, True, False, True, False]


# ---- a free-data D/W/M chart's bars are stamped the app's way (17:00 CT the evening before): their trade dates too ----

PIVOTS_JS = """
/*TA*/
const NY = { tz: 'America/New_York', rth: ['09:30', '16:00'], day: '00:00' };
const S = { ext: { exchange: NY }, spec: '1D', seqTime: new Map(), fp: { clear() {} }, dsmaLive: { clear() {} }, viewWant: null };
const noop = () => {}, series = { setData: noop }, ax = { pick: '' };
const [cmpClear, altSetData, fpSchedule, setStudyData, indsUpdate, hemaDraw, hemaRequest, dsmaDraw, dsmaRequest, renderIfInfo,
  scheduleRange, priceLineLayout, bigSchedule, fillsSchedule, goToTime, setChartZone, cmpLoad, legend, header] = Array(19).fill(noop);
const $ = () => ({ classList: { add: noop } }), toPoints = () => [{}, {}], vol = series, candles = series, inst = () => ({});
const bigOn = () => false, viewKey = () => '', viewApply = () => true;
/*APP*/
const bars = BARS, out = {};
for (const spec of ['5m', '1D']) { S.spec = spec; applyHistory({ kind: 'time', bars }); out[spec] = sessionClockKey; }
const at = (day) => bars.findIndex((b) => b.day === day);
for (const range of ['Weekly', 'Monthly']) {
  const r = new TaPivotsRun(range, 'floor'), pp = bars.map((b) => r.step(b, true)[0]);
  out[range] = { mon: sessionDay(bars[at('2026-10-05')].t), oct5: pp[at('2026-10-05')], oct1: pp[at('2026-10-01')], oct2: pp[at('2026-10-02')] };
}
console.log(JSON.stringify(out));
"""


@needs_node
def test_free_data_day_bars_take_their_trade_dates_for_pivots():
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(40)]
    days = [d for d in days if d.weekday() < 5 and d <= date(2026, 10, 9)]
    bars = [{"t": day_open(d), "day": f"{d}", "o": 95 + i, "h": 100 + i, "l": 90 + i, "c": 95 + i, "v": 1} for i, d in enumerate(days)]
    pp = lambda sel: (max(b["h"] for b in sel) + min(b["l"] for b in sel) + sel[-1]["c"]) / 3   # noqa: E731
    week = [b for b in bars if "2026-09-28" <= b["day"] <= "2026-10-02"]
    sept = [b for b in bars if b["day"] < "2026-10-01"]
    js = PIVOTS_JS.replace("/*TA*/", (WEB / "ta.js").read_text()).replace("BARS", json.dumps(bars))
    got = run_app(["TZ_EXCHANGE", "calSpec", "exch", "applyHistory"], js)
    assert got["5m"] == "America/New_York 00:00" and got["1D"] == "America/Chicago 17:00"   # intraday: the exchange's clock
    assert got["Weekly"]["mon"] == "2026-10-05" and got["Weekly"]["oct5"] == pp(week)   # Monday: the week before's levels
    assert got["Monthly"]["oct1"] == got["Monthly"]["oct2"] == pp(sept)   # October's from September, from its 1st
