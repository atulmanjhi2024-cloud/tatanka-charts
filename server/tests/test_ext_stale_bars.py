"""A chart tab switched from NQ to QQQ 4h (ETH) kept NQ's bars and last price under QQQ's name ("QQQ 26907.00
+26215.32 +3790.09%"): IBKR's history for QQQ (1-hour bars over 1 Y with the pre- and post-market) timed out after
25 s on every request, the chart page showed the old bars until an answer came, and nothing remembered the timeout.

Now IBKR is asked about 2000 of its bars (it answers those in seconds), waited for IB_WAIT_S at most, and a timeout
sends the chart to the next source for a while, so the answer comes within the page's own limit; the page clears the
old bars at once (tab, symbol, a layout cell's symbol, a free-data chart's timeframe or session) and says what it
waits for, or why there are no bars."""
import asyncio
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import httpx
import pytest

from chartsrv import ext
from chartsrv.feeds import ibkr
from test_alpaca_bars import _alpaca_server
from test_ext_sources import FakeIB
from test_hunt_web_chart import WEB, needs_node, run_app
from test_review_fixes import build_market

KEYS = {"ALPACA_KEY_ID": "PKABCDEFGH", "ALPACA_SECRET": "s3cretsecret"}
_LIMIT = re.search(r"const EXT_WAIT_MS = (\d+)e3", (WEB / "app.js").read_text())
EXT_WAIT_S = int(_LIMIT[1]) if _LIMIT else 0   # the page's limit for a free-data chart's answer


class HangingIB(FakeIB):
    """IBKR connected (its quotes work) and its history never answering, whatever timeout it is given."""

    def __init__(self):
        super().__init__(None)
        self.hang = True

    async def history(self, key, size, span, rth=True, timeout=ibkr.HISTORY_TIMEOUT_S):
        self.asked.append((key, size, span, rth))
        if self.hang:
            await asyncio.sleep(3600)
        start = int(time.time() // 3600 * 3600 - 3 * 3600) * 1000
        rows = [(datetime.fromtimestamp(start / 1000 + i * 3600, timezone.utc), 690, 693, 689, 692, 1000) for i in range(3)]
        return {"kind": "STK", "name": "INVESCO QQQ TRUST", "tick": 0.01, "rows": rows}


def _market(tmp_path, monkeypatch, handler, wait=0.2):
    monkeypatch.setattr(ext, "IB_WAIT_S", wait, raising=False)
    monkeypatch.setattr(ext, "IB_GRACE_S", wait, raising=False)
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **KEYS)
    ib = HangingIB()
    m.feed_objs.append(ib)
    return m, ib


def test_every_source_together_answers_within_the_pages_limit():
    """The server's longest answer, every source slow (IBKR, then Alpaca, then Yahoo), is under the page's limit; IBKR
    is waited for well under its own 25 s."""
    assert ext.ANSWER_S == ext.IB_WAIT_S + ext.IB_GRACE_S + ext.ALPACA_WAIT_S + ext.YAHOO_WAIT_S
    assert ext.ANSWER_S + 5 <= EXT_WAIT_S and ext.IB_WAIT_S < ibkr.HISTORY_TIMEOUT_S


def test_qqq_4h_eth_asks_ibkr_a_span_it_answers_quickly():
    """QQQ 4h with ETH counts from 4:00, so from IBKR's hourly bars: about 2000 of them (25 W), not 1 Y (about 4000 of
    16 hours a day, more than IBKR answered in 25 s)."""
    n, _ = ext.split_spec("4h")
    b = ibkr.base_minutes(n, 4 * 60)
    assert (b, ibkr.BAR_SIZES[b], ibkr.duration(n, b, 960)) == (60, "1 hour", "25 W")
    weeks = int(ibkr.duration(n, b, 960).split()[0])
    assert weeks * 5 * 16 <= ibkr.HISTORY_BARS * 1.05   # trading days of 16 hours: about HISTORY_BARS bars


def test_ibkr_that_hangs_gives_way_to_alpaca_and_is_remembered(tmp_path, monkeypatch):
    """IBKR's history hangs: the chart's answer comes from Alpaca after IB_WAIT_S (well within the page's limit), says
    its source, and the next requests (the chart's polls, the same chart again) go straight to Alpaca until IB_SLOW_S
    has passed; then IBKR is asked again."""
    calls = []
    m, ib = _market(tmp_path, monkeypatch, _alpaca_server(calls))

    async def go():
        t0 = time.monotonic()
        d = await asyncio.wait_for(m.ext.bars("QQQ", "4h", rth=False), 5)
        first = time.monotonic() - t0
        assert (d["source"], d["feed"], d["spec"]) == ("alpaca", "sip+iex", "4h") and d["bars"]
        assert first < 1.5 and ib.asked == [("QQQ", "1 hour", "25 W", False)]
        m.ext.cache.pop(("alpaca:QQQ", "4h+eth"))   # the chart's poll a while later: Alpaca asked again, IBKR not
        t0 = time.monotonic()
        again = await asyncio.wait_for(m.ext.bars("QQQ", "4h", rth=False), 5)
        assert again["source"] == "alpaca" and len(ib.asked) == 1 and time.monotonic() - t0 < 0.15
        ck = ("ibkr:QQQ", "60m+eth")
        until, note = m.ext.cache[ck]
        assert note == {"none": True, "why": "no answer in 0.2 s"} and until - time.monotonic() > ext.IB_SLOW_S - 5
        m.ext.cache[ck] = (time.monotonic() - 1, note)   # IB_SLOW_S later, IBKR answering again
        ib.hang = False
        back = await asyncio.wait_for(m.ext.bars("QQQ", "4h", rth=False), 5)
        assert back["source"] == "ibkr" and len(ib.asked) == 2
    asyncio.run(go())


def test_a_poll_that_ibkr_times_out_goes_to_the_next_source(tmp_path, monkeypatch):
    """IBKR charted QQQ, then hangs on a poll's newest bars: that poll waits IB_WAIT_S, not 25 s, and is answered by
    Alpaca; the polls after it don't wait on IBKR at all."""
    m, ib = _market(tmp_path, monkeypatch, _alpaca_server([]))
    ib.hang = False

    async def go():
        assert (await m.ext.bars("QQQ", "4h", rth=False))["source"] == "ibkr"
        ck = ("ibkr:QQQ", "60m+eth")
        m.ext.cache[ck] = (time.monotonic() - 1, m.ext.cache[ck][1])   # its answer expired: the poll asks the newest
        ib.hang = True
        t0 = time.monotonic()
        d = await asyncio.wait_for(m.ext.bars("QQQ", "4h", rth=False), 5)
        assert d["source"] == "alpaca" and time.monotonic() - t0 < 1.5 and ib.asked[-1][2].endswith(" S")
        n = len(ib.asked)
        m.ext.cache.pop(("alpaca:QQQ", "4h+eth"))
        assert (await asyncio.wait_for(m.ext.bars("QQQ", "4h", rth=False), 5))["source"] == "alpaca" and len(ib.asked) == n
    asyncio.run(go())


def test_no_source_says_what_each_one_said(tmp_path, monkeypatch):
    """IBKR hangs, Alpaca refuses the keys, Yahoo fails: the error (the chart's message) names each."""
    def handler(req):
        return httpx.Response(401 if "alpaca" in req.url.host else 500, json={})
    m, _ = _market(tmp_path, monkeypatch, handler)
    with pytest.raises(ValueError) as e:
        asyncio.run(asyncio.wait_for(m.ext.bars("QQQ", "4h", rth=False), 5))
    assert str(e.value) == ("No QQQ bars from IBKR (no answer in 0.2 s), Alpaca (keys refused) or Yahoo: Yahoo did not "
                            "answer (HTTPStatusError)")


def test_ibkr_history_says_when_it_timed_out():
    """IBKRQuotes.history() gives ib_async the timeout it is given; ib_async's timeout (it cancels the request and
    answers with no bars) comes back as "timeout", an answer with no bars (no subscription) does not."""
    asked = []

    class IB:
        slow = True

        def isConnected(self):
            return True

        async def reqHistoricalDataAsync(self, c, end, span, size, show, rth, formatDate=1, timeout=60):
            asked.append((span, size, rth, timeout))
            if self.slow:   # as ib_async: wait_for(timeout), then cancelHistoricalData and no bars
                await asyncio.sleep(timeout)
            return []
    feed = ibkr.IBKRQuotes(NS())
    feed.ib = IB()

    async def details(ib, key):
        return NS(contract=NS(secType="STK"), longName="INVESCO QQQ TRUST", minTick=0.01)
    feed._details = details
    slow = asyncio.run(feed.history("QQQ", "1 hour", "25 W", False, timeout=0.1))
    assert slow["timeout"] is True and slow["rows"] == [] and asked == [("25 W", "1 hour", False, 0.1)]
    feed.ib.slow = False
    assert "timeout" not in asyncio.run(feed.history("QQQ", "1 hour", "25 W", False, timeout=0.1))


# ---- the chart page: never another symbol's bars or price while a free-data chart loads, or when it fails ---------

PAGE_JS = """
(async () => {
let tid = 0, specForced = '', S;
const timers = new Map(), reqs = [], toasts = [], sent = [], merged = [];
const setTimeout = (f, ms) => { timers.set(++tid, { f, ms }); return tid; }, clearTimeout = (id) => timers.delete(id);
const fire = (ms) => { for (const [id, t] of timers) if (t.ms === ms) { timers.delete(id); t.f(); return true; } return false; };
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
const fetch = (path, o) => new Promise((resolve, reject) => {
  const r = { path, aborted: false, answer: (doc, status = 200) => resolve({ ok: status < 400, status, statusText: '', json: async () => doc }) };
  o.signal?.addEventListener('abort', () => { r.aborted = true; reject(o.signal.reason); });
  reqs.push(r);
});
const el = () => ({ textContent: '', hidden: false, disabled: false, className: '', kids: {}, classList: { add() {}, toggle() {} },
  querySelector(q) { return (this.kids[q] ||= el()); } });
const els = {}, $ = (q) => (els[q] ||= el());
const series = () => ({ data: [], setData(d) { this.data = d; }, applyOptions() {}, update() {} });
const candles = series(), vol = series(), noop = () => {};
const chart = { timeScale: () => ({ options: () => ({ rightOffset: 0 }), setVisibleLogicalRange: noop }) };
const PS = () => ({ options: () => ({ autoScale: true }) });
const [cmpClear, altSetData, fpSchedule, setStudyData, indsUpdate, hemaDraw, hemaRequest, dsmaDraw, dsmaRequest, renderIfInfo,
  scheduleRange, priceLineLayout, bigSchedule, fillsSchedule, goToTime, setChartZone, cmpLoad, setSessionClock, setSessionHolidays,
  legendStudies, dataWindow, cmpLegend, renderTf, renderStudiesMenu, barsLayout, watermarkLayout, replayLoad, tapeSymbol, amtRequest,
  tpoRequest, vpAll, gapRequest, amtLayout, ifRequest, renderPlanIf, renderRth, tabSync, applyPriceFormat, indsBuild, renderSymBtn,
  onSymbolChanged, viewSave, saveTabs, renderTabs, applyStudyState, amtFollow, lookStore, cellPost, authCheck, futureAdd, replayExit,
  applyLock, setAuto] = Array(99).fill(noop);
const TK_MULTI = false, TZ_EXCHANGE = 'America/Chicago', WL_FUT = /^([A-Z0-9]+)1!$/, VP_KINDS = [], SPEC_RE = /^\\d+[mhDWM]$/;
const NY = { tz: 'America/New_York', rth: ['09:30', '16:00'], day: '00:00' }, CME = { tz: 'America/Chicago', rth: null, day: '17:00' };
const local = { set: noop }, ax = { pick: '' }, drawings = { state: {}, load: noop, select: noop, update: noop };
const toast = (t) => toasts.push(t), send = (m) => sent.push(m), applyBars = (m) => merged.push(...m.bars);
const isExtSym = (s) => !S.instruments[s], chartSym = (s) => s, wlKey = (s) => s, wlIsFormula = () => false, specLabel = (s) => s, specOk = () => true;
const extSource = () => 'yahoo', extSpecFor = (s) => s, extSeries = () => false, calSpec = () => false, bigOn = () => false;
const exch = () => (S.ext ? S.ext.exchange || NY : CME), inst = () => ({}), indFmt = () => '', toPoints = (b) => [{ time: b.t, close: b.c }, {}];
const px = (p) => (p == null ? '–' : Number(p).toFixed(2)), viewApply = () => false, viewSnap = () => null, ownTab = () => null;
const sharedStudies = () => ({}), applyLook = () => false;
/*APP*/
const bars = (c0, c1, n = 6) => Array.from({ length: n }, (_, i) => {
  const c = +(c0 + (c1 - c0) * i / (n - 1)).toFixed(2);
  return { t: 1793664000000 + i * 14400000, o: c, h: c, l: c, c, v: 1, seq: i + 1 };
});
const answer = (sym, source, feed, c0, c1) => ({ symbol: sym, spec: '4h', source, feed, name: sym, type: 'etf', decimals: 2, tick: 0.01,
  specs: ['1h', '1D'], custom: true, exchange: NY, bars: bars(c0, c1) });
const look = () => ({ bars: S.bars.length, candles: candles.data.length, last: $('#q-last').textContent,
  chg: $('#q-chg').querySelector('.abs').textContent, legend: $('#legend').textContent,
  msg: $('#chart-msg').hidden ? '' : $('#chart-msg').textContent });
S = { symbol: 'NQ', spec: '4h', rth: false, subId: 1, instruments: { NQ: {} }, ext: null, bars: [], lastBar: null, seqTime: new Map(),
  fp: { clear: noop }, dsmaLive: { clear: noop }, viewWant: null, cs: { volume: true }, vp: {}, st: {}, views: {},
  loadedKey: '', loadedSym: '', loadNote: '', tabs: [{ id: 'a', sym: 'NQ', spec: '4h' }, { id: 'b', sym: 'QQQ', spec: '4h' }], tab: 0,
  quotes: { NQ: { s: 'NQ', last: 26907, prev: 26800 }, QQQ: { s: 'QQQ', last: 692.1, prev: 691.68 }, SPY: { s: 'SPY', last: 600, prev: 599 } } };
const out = {};
applyHistory({ kind: 'time', bars: bars(26600, 26907) });   // NQ 4h, live
out.nq = look();
openTab(1);   // the QQQ 4h tab (ETH): its bars on their way, IBKR slow at the server
out.loading = look();
legend(S.lastBar);   // a profile's answer, the crosshair: the legend still says it is loading
out.loadingAgain = look();
out.asked = reqs.map((r) => r.path);
fire(EXT_WAIT_MS);   // no answer within the page's limit
await settle();
out.failed = { ...look(), retry: [...timers.values()].some((t) => t.ms === EXT_RETRY_MS), toasts: toasts.slice() };
fire(EXT_RETRY_MS);   // asked again: the server remembers IBKR's timeout, Alpaca answers
reqs.at(-1).answer(answer('QQQ', 'alpaca', 'sip+iex', 690.5, 692.1));
await settle();
out.qqq = look();
fire(10e3);   // the poll: IBKR back, its bars in full rather than joined onto Alpaca's
reqs.at(-1).answer(answer('QQQ', 'ibkr', 'live', 690.6, 692.3));
await settle();
out.moved = { ...look(), merged: merged.length };
fire(15e3);   // the next poll, the same source: joined on
reqs.at(-1).answer({ ...answer('QQQ', 'ibkr', 'live', 690.6, 692.3), bars: bars(690.6, 692.4) });
await settle();
out.merged = merged.length;
setRth(true);   // regular hours: QQQ's ETH bars go too while the RTH ones load
out.rth = look();
setSymbol('SPY');   // a layout cell's symbol (or a watchlist pick) before QQQ's came: QQQ's answer is dropped
const old = reqs.at(-2);
old.answer(answer('QQQ', 'yahoo', '', 690, 691));
reqs.at(-1).answer(answer('SPY', 'yahoo', '', 598, 600));
await settle();
out.spy = { ...look(), oldAborted: old.aborted };
setSymbol('NQ');   // back to the live NQ: SPY's bars go until NQ's history comes
out.toLive = look();
applyHistory({ kind: 'time', bars: bars(26600, 26907) });
setSpec('1h');   // the live chart's own bars stay while its other timeframe comes (at once), with its own price
out.liveSpec = look();
console.log(JSON.stringify(out));
process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


@needs_node
def test_switching_nq_to_qqq_never_shows_nqs_bars_or_price():
    out = run_app(["api", "viewKey", "symQuote", "header", "legend", "BAR_CAP", "olderReset", "applyHistory", "barsClear", "loadNote", "extTimer", "EXT_WAIT_MS", "extEvery",
                   "extLoad", "extMerge", "subscribe", "setSymbol", "setSpec", "setRth", "openTab"], PAGE_JS)
    assert out["nq"] == {"bars": 6, "candles": 6, "last": "26907.00", "chg": "+107.00", "legend": "NQ 4h", "msg": ""}
    nothing = {"bars": 0, "candles": 0, "last": "–", "chg": ""}
    assert out["loading"] == {**nothing, "legend": "QQQ 4h · loading…", "msg": "Loading QQQ 4h…"}   # not 26907 +26215.32
    assert out["loadingAgain"] == out["loading"]
    assert out["asked"] == ["/api/ext/bars?symbol=QQQ&spec=4h&rth=0"]
    no_answer = f"No answer from the server in {EXT_WAIT_S} s; trying again…"
    assert out["failed"] == {**nothing, "legend": f"QQQ 4h · {no_answer}", "msg": no_answer, "retry": True,
                             "toasts": [f"QQQ: No answer from the server in {EXT_WAIT_S} s"]}
    assert out["qqq"] == {"bars": 6, "candles": 6, "last": "692.10", "chg": "+0.42", "legend": "QQQ 4h · real time", "msg": ""}
    assert out["moved"] == {"bars": 6, "candles": 6, "last": "692.30", "chg": "+0.62", "legend": "QQQ 4h · IBKR real time", "msg": "",
                            "merged": 0}
    assert out["merged"] == 1
    assert out["rth"] == {**nothing, "legend": "QQQ 4h RTH · loading…", "msg": "Loading QQQ 4h…"}
    assert out["spy"] == {"bars": 6, "candles": 6, "last": "600.00", "chg": "+1.00", "legend": "SPY 4h RTH · delayed", "msg": "",
                          "oldAborted": True}
    assert out["toLive"] == {**nothing, "legend": "NQ 4h RTH · loading…", "msg": "Loading NQ 4h…"}
    assert out["liveSpec"] == {"bars": 6, "candles": 6, "last": "26907.00", "chg": "+107.00", "legend": "NQ 1h RTH · loading…", "msg": ""}

