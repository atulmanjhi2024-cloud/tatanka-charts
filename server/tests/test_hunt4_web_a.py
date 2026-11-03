"""Fixes from the fourth bug hunt of the web app: the open Indicators dialog redrawn for nothing (a value being typed
was lost), the chart's fills and journal asked again in full for every new fill, the HTF EMA asked again at every
closed bar, and the bar-close countdown on 2D and 3D bars. The parts of app.js a test needs (and web/indicators.js)
run in node with the rest stubbed; the fills' asks go to the real server."""
import json
import shutil
import subprocess

import pytest

from test_hunt_web_chart import WEB, run_app
from test_hunt3_web_ui import IND_JS, IND_PARTS, SCHEMA

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


# ---- the Indicators dialog redraws only when what it shows changed ----------------------------------------------------

IND_REFRESH_JS = r"""
// A box being typed in: still the same box, and what it holds
const typing = (key, name, v) => { const c = ctl(key, name); document.activeElement = c; c.value = v; return c; };
const kept = (c, key, name) => ({ same: ctl(key, name) === c, shows: ctl(key, name).value });
const out = {};
// an AMT Apply with a levels file set (the dialog stays open), then the next change typed in Paste Levels Here: the
// levels are read again 6 s later
S.amtServer.shared = ['LevelsFile']; S.amtServer.overrides = { LevelsFile: 'C:/levels.csv' };
await on('ES');
indDlg.open(true, 'amt');
{ const b = ctl('amt', 'ShowLevels'); b.checked = false; b.onchange(); }
await root.sel['.ind-apply'].onclick();
out.timers = timers.map((x) => x.ms);
let c = typing('amt', 'lvl_text', 'ES 2026-10-26, 6855, 6871.25, 68');
for (const x of timers.splice(0)) await x.f();
out.afterApply = kept(c, 'amt', 'lvl_text');
// a 'levels' broadcast for the symbol (its file read again) with the pasted line as it was
await on('ES');
indDlg.open(true, 'amt');
c = typing('amt', 'lvl_text', 'typing...');
await loadPlanLevels();
out.levels = kept(c, 'amt', 'lvl_text');
// ... and with another pasted line (saved on another device): the dialog shows it
server.paste.ES = 'ES 2026-10-27, 6900';
await loadPlanLevels();
out.levelsChanged = ctl('amt', 'lvl_text').value;
// NinjaTrader's gap fill ('reload') or a timeframe change: subscribe() empties S.ifData and asks for the same block
await on('ES');
indDlg.open(true, 'ifthen');
c = typing('ifthen', 'RowsBlock', 'BULL|6850|6870|ES above\nBEAR|6800|6820|typ');
S.ifData = null; S.ifRows = []; await ifRequest();
out.reload = kept(c, 'ifthen', 'RowsBlock');
// a reconnect: the dialog redrawn (the settings came) while the block was on its way shows it once it comes
await on('ES');
indDlg.open(true, 'ifthen');
S.ifData = null; S.ifRows = []; indDlg.refresh();
const asking = ctl('ifthen', 'RowsBlock').value;
await ifRequest();
out.reconnect = [asking, ctl('ifthen', 'RowsBlock').value];
// an edit already made (kept by 'change'), then another box typed in: a redraw for nothing drops neither
await on('ES');
indDlg.open(true, 'ifthen');
edit('ifthen', 'InstrumentLabel', 'ES mini');
c = typing('ifthen', 'RowsBlock', 'typing after an edit');
await loadPlanLevels();
out.afterEdit = { ...kept(c, 'ifthen', 'RowsBlock'), label: ctl('ifthen', 'InstrumentLabel').value };
// the chart switched with nothing else changed: the dialog follows
await on('ES');
indDlg.open(true, 'lines');
S.symbol = 'NQ'; indDlg.refresh();
out.switched = shown();
console.log(JSON.stringify(out));
"""


def test_the_open_indicators_dialog_keeps_a_value_being_typed_when_nothing_it_shows_changed():
    """The levels read again after an AMT Apply (6 s later, a levels file set), a 'levels' broadcast and a gap fill's or
    timeframe change's If/Then block asked again redrew the open dialog with nothing changed, so a value being typed
    (kept only on 'change') was lost and the box replaced. It redraws when what it shows changed: another pasted line,
    the block that came after a redraw without it, another symbol."""
    js = IND_JS.replace("/*IND*/", (WEB / "indicators.js").read_text()).replace("SCHEMA", json.dumps(SCHEMA))
    head = js.split("const out = {};")[0].replace(
        "const api = async (method, path, body) => {",
        "const api = async (method, path, body) => {\n  if (path.startsWith('/api/amt/settings')) return {};")
    js = "const timers = [], setTimeout = (f, ms) => timers.push({ f, ms });\n" + head + IND_REFRESH_JS
    out = run_app(IND_PARTS, f"(async () => {{ {js} }})().catch((e) => {{ console.error(e); process.exit(1); }});")
    assert out["timers"] == [6000]
    assert out["afterApply"] == {"same": True, "shows": "ES 2026-10-26, 6855, 6871.25, 68"}
    assert out["levels"] == {"same": True, "shows": "typing..."}
    assert out["levelsChanged"] == "ES 2026-10-27, 6900"
    assert out["reload"] == {"same": True, "shows": "BULL|6850|6870|ES above\nBEAR|6800|6820|typ"}
    assert out["reconnect"] == ["", "BULL|6850|6870|ES above"]
    assert out["afterEdit"] == {"same": True, "shows": "typing after an edit", "label": "ES mini"}
    assert out["switched"] == " (NQ)"


# ---- the chart's fills and trades: a new fill asks only for what it changes, one ask at a time --------------------------

T = 1_760_000_000_000
H = 3_600_000


def fill(ts, side, qty, price, i, acct="Live", sym="ES"):
    return {"account": acct, "symbol": sym, "contract": f"{sym} 12-26", "ts": ts, "price": price, "qty": qty, "side": side,
            "point_value": 50.0, "commission": 2.04, "exec_id": f"x{i}", "order_id": f"o{i}", "name": "Entry"}


FILLS_PARTS = ["fillTimer", "fillsSchedule", "fillsFrom", "fillsFetch", "jrTimer", "onFill"]
FILLS_JS = r"""
const timers = new Map();
let tid = 0;
const setTimeout = (f) => { timers.set(++tid, f); return tid; }, clearTimeout = (id) => { timers.delete(id); };
const wait = (ms) => new Promise((r) => globalThis.setTimeout(r, ms));
const S = { symbol: 'ES', st: { fills: true }, bars: [{ t: FIRST }], fills: [], fillTrades: [], fillsFor: '' };
const TK_MULTI = false, local = { get: (k, d) => d }, fillAcct = () => '', fillAccounts = () => {}, fillPrim = { update() {} };
const journalOpen = () => false, journalLoad = () => {};
/*APP*/
"""
# The server (this test, over stdin and stdout) stores the fills and answers each ask; the page's settled state after
# each new fill is checked against everything asked again from the chart's first bar.
FILLS_REAL_JS = FILLS_JS + r"""
const waiting = [], asks = [];
require('readline').createInterface({ input: process.stdin }).on('line', (l) => waiting.shift()(JSON.parse(l)));
const py = (msg) => new Promise((r) => { waiting.push(r); process.stdout.write(`${JSON.stringify(msg)}\n`); });
const api = (method, url) => { asks.push(url); return py({ ask: url }); };
async function settle() {
  for (let n = 0; n < 2000; n++) {
    if (timers.size) { const fs = [...timers.values()]; timers.clear(); fs.forEach((f) => f()); }
    await wait(1);
    if (!timers.size && !fillBusy && !waiting.length) return;
  }
  throw new Error('never settled');
}
(async () => {
  const out = [];
  fillsSchedule(0);   // the chart's history came
  await settle();
  for (const group of STEPS) {   // fills heard together
    for (const f of group) await py({ add: f });
    asks.length = 0;
    for (const f of group) onFill({ type: 'fill', fill: f });
    await settle();
    const q = `start=${FIRST}&end=${Date.now() + 60_000}&symbol=ES`;
    const f = await py({ ask: `/api/fills?${q}` }), j = await py({ ask: `/api/journal?${q}&excursions=false` });
    out.push({ asks: asks.map((u) => u.replace(/&end=\d+/, '')), fills: JSON.stringify(S.fills) === JSON.stringify(f.fills),
      trades: JSON.stringify(S.fillTrades) === JSON.stringify(j.trades.filter((t) => !t.open)), n: S.fillTrades.length });
  }
  process.stdout.write(`${JSON.stringify({ out })}\n`);
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_a_new_fill_asks_again_only_from_where_every_position_was_flat_and_the_chart_shows_what_a_full_ask_would(
        tmp_path, monkeypatch):
    """Every new fill made each open chart ask for its fills and its journal (MAE and MFE too) since its first bar: on a
    daily chart, a year of trades worked out again for each fill, overlapping when fills came more than 300 ms apart. A
    fill for another symbol now asks nothing; one for the chart's symbol asks from where every position was flat (no
    trade still open, or closed since, began before), or from the first bar when that is further back (a position open
    since before it, a fill older than it). The page's fills and trades then match a full ask: across accounts, an
    orphan sell (a position held when the add-on was installed), a reversal, an exit and re-entry in one millisecond,
    a fill that comes late."""
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    from test_hunt_web_chart import app_part
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    old = [fill(T - 20 * 24 * H, -1, 1, 6800.0, 1, "Sim101"), fill(T - 2 * H, 1, 2, 6900.0, 2, "Sim102"),
           fill(T + H, 1, 1, 6905.0, 3, "Sim101"), fill(T + 2 * H, -1, 1, 6908.0, 4, "Sim101"),
           fill(T + 3 * H, 1, 1, 6910.0, 5), fill(T + 3 * H + 60_000, 1, 1, 6912.0, 6), fill(T + 4 * H, -1, 1, 6915.0, 7),
           fill(T + 5 * H, -1, 2, 6914.0, 8)]   # Live: long 2 out at 6915 and 6914, then short 1
    steps = [[fill(T + H // 2, -1, 2, 6904.0, 10, "Sim102")],    # closes a position open since before the first bar
             [fill(T + 6 * H, 1, 1, 6909.0, 11)],                  # covers the short: back to the long it came from
             [fill(T + 7 * H, 1, 1, 6911.0, 12)],                  # a new long, nothing else open
             [fill(T + 8 * H, -1, 1, 6913.0, 13), fill(T + 8 * H, 1, 1, 6913.0, 14)],   # out and in again, one ms
             [fill(T + 2 * H + H // 4, 1, 1, 6907.0, 15, "Sim101")],   # heard late: older than the Live trades
             [fill(T + 9 * H, -1, 1, 6912.0, 16, "Sim101")],
             [fill(T + 9 * H, 1, 1, 25100.0, 17, sym="NQ")],        # another symbol's: nothing to ask
             [fill(T - H, -1, 1, 6899.0, 18, "Sim103")]]            # older than the first bar
    src = "\n".join(app_part(p) for p in FILLS_PARTS)
    js = FILLS_REAL_JS.replace("/*APP*/", src).replace("FIRST", str(T)).replace("STEPS", json.dumps(steps))
    with TestClient(app) as c:
        m = app.state.market
        for f in old:
            c.portal.call(m.store.add_fill, f)
        p = subprocess.Popen(["node", "-e", js], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        out = None
        for line in p.stdout:
            msg = json.loads(line)
            if "out" in msg:
                out = msg["out"]
                break
            if "add" in msg:
                c.portal.call(m.store.add_fill, msg["add"])
                reply = {}
            else:
                reply = c.get(msg["ask"]).json()
            p.stdin.write(json.dumps(reply) + "\n")
            p.stdin.flush()
        p.stdin.close()
        assert p.wait() == 0 and out is not None, p.stderr.read()
    for i, s in enumerate(out):
        assert s["fills"] and s["trades"], (i, s)
    full, part = f"start={T}&symbol=ES", lambda ts: f"start={ts}&symbol=ES"
    assert [s["asks"] for s in out] == [
        [f"/api/fills?{full}", f"/api/journal?{full}&excursions=false"],   # Sim102 was open since before the first bar
        [f"/api/fills?{part(T + 3 * H)}", f"/api/journal?{part(T + 3 * H)}&excursions=false&flat=true"],
        [f"/api/fills?{part(T + 7 * H)}", f"/api/journal?{part(T + 7 * H)}&excursions=false&flat=true"],
        [f"/api/fills?{part(T + 7 * H)}", f"/api/journal?{part(T + 7 * H)}&excursions=false&flat=true"],   # asked once
        [f"/api/fills?{part(T + 2 * H + H // 4)}", f"/api/journal?{part(T + 2 * H + H // 4)}&excursions=false&flat=true"],
        [f"/api/fills?{part(T + 2 * H + H // 4)}", f"/api/journal?{part(T + 2 * H + H // 4)}&excursions=false&flat=true"],
        [],
        [f"/api/fills?{full}", f"/api/journal?{full}&excursions=false"]]
    assert [s["n"] for s in out] == [3, 4, 4, 5, 5, 6, 6, 6]


FILLS_BUSY_JS = FILLS_JS.replace("fillAcct = () => ''", "fillAcct = () => acct") + r"""
const pending = [], run = () => { const fs = [...timers.values()]; timers.clear(); fs.forEach((f) => f()); };
const api = (method, url) => new Promise((r) => pending.push({ url: url.replace(/&end=\d+/, ''), r }));
const none = { fills: [], trades: [], open_since: null, accounts: [] };
const answer = async (n) => { for (const p of pending.slice(n, n + 2)) p.r(none); await new Promise((r) => setImmediate(r)); };
const fl = (ts, account = 'Live', symbol = 'ES') => ({ type: 'fill', fill: { account, symbol, ts } });
(async () => {
  const out = {};
  fillsSchedule(0); run(); await answer(0);   // the chart's history came
  onFill(fl(FIRST + 10 * H)); run();   // under way...
  onFill(fl(FIRST + 11 * H)); run();   // ...while two more come, 400 ms apart
  onFill(fl(FIRST + 10.5 * H)); run();
  out.whileAsking = pending.length;
  await answer(2);   // in: asked once more, for both
  out.after = pending.map((p) => p.url);
  await answer(4);
  acct = 'Sim101';   // the chart shows one account
  onFill(fl(FIRST + 12 * H, 'Live')); onFill(fl(FIRST + 12 * H, 'Sim101', 'NQ')); run();
  out.otherAccountOrSymbol = pending.length;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_the_charts_fills_ask_one_at_a_time_and_not_for_another_accounts_or_symbols_fill():
    """Fills more than 300 ms apart each started an ask of their own while the one before was still being worked out.
    Now what comes meanwhile is asked once that one is in, from the earliest of them; a fill for an account the chart
    does not show, or for another symbol, asks nothing."""
    from test_hunt_web_chart import app_part
    js = FILLS_BUSY_JS.replace("/*APP*/", "\n".join(app_part(p) for p in FILLS_PARTS))
    js = f"let acct = '';\nconst H = {H};\n" + js.replace("FIRST", str(T))
    out = run_app([], js)
    assert out["whileAsking"] == 4
    q = lambda ts, flat="&excursions=false&flat=true": [f"/api/fills?start={ts}&symbol=ES", f"/api/journal?start={ts}&symbol=ES{flat}"]
    assert out["after"] == q(T, "&excursions=false") + q(T + 10 * H) + q(T + 10 * H + H // 2)
    assert out["otherAccountOrSymbol"] == 6


def test_the_journal_is_worked_out_in_one_thread_on_one_connection_and_without_mae_for_the_chart(tmp_path, monkeypatch):
    """The journal read its fills and worked out its trades and totals on the event loop, then sent each trade's MAE
    and MFE to a thread of its own, each on a new connection: for a year of trades, seconds of work per new fill, though
    the chart's lines use no MAE or MFE. It is now one thread and one connection; excursions=false leaves MAE and MFE
    out. open_since: the oldest open trade's entry, also one from before the range."""
    import asyncio
    from types import SimpleNamespace
    import chartsrv.app as A
    from chartsrv import store as st
    from chartsrv.models import Bar
    s = st.Store(tmp_path / "j.db")
    t0 = T - T % 60_000
    s.insert_bars_if_missing("ES", [Bar(t=t0 + i * 60_000, o=6900, h=6904, l=6897, c=6901, v=10) for i in range(10)])
    for f in [fill(t0 - 60_000, 1, 1, 6899.0, 0, "Sim102")] + [
            fill(t0 + i * 120_000 + j * 60_000, 1 - 2 * j, 1, 6900.0 + j, 2 * i + j + 1) for i in range(4) for j in (0, 1)]:
        s.add_fill(f)
    monkeypatch.setattr(A.app.state, "market", SimpleNamespace(store=s, instruments={"ES": SimpleNamespace(tick_size=0.25)}),
                        raising=False)
    threads, connects, real_thread, real_connect = [], [], asyncio.to_thread, st.connect
    monkeypatch.setattr(asyncio, "to_thread", lambda f, *a, **k: threads.append(f) or real_thread(f, *a, **k))
    monkeypatch.setattr(st, "connect", lambda *a, **k: connects.append(a) or real_connect(*a, **k))
    full = asyncio.run(A.journal_get(t0, t0 + 3_600_000, None, "ES"))
    assert len(threads) == 1 and len(connects) == 1
    assert [(t["mae"], t["mfe"]) for t in full["trades"] if not t["open"]] == [(3.0, 4.0)] * 4
    assert full["open_since"] == t0 - 60_000 and full["stats"]["trades"] == 4   # Sim102's long, from before the range
    lean = asyncio.run(A.journal_get(t0, t0 + 3_600_000, None, "ES", excursions=False))
    assert not any("mae" in t or "mfe" in t for t in lean["trades"])
    assert [{k: v for k, v in t.items() if k not in ("mae", "mfe")} for t in full["trades"]] == lean["trades"]
    s.close()


# ---- HTF EMA: a closed chart bar asks the server again only when the answer may have changed ---------------------------

HEMA_JS = """
let clock = 1.8e12;
Date.now = () => clock;
const asked = [];
const S = { st: { hema: true }, symbol: 'ES', spec: '1s', bars: [{ t: clock - 3600e3, c: 1 }], instruments: { ES: {} }, replay: null,
  hemaSet: { HtfPeriodType: 'Minute', HtfPeriod: 60, EmaLength: 21, Mode: 'SmoothScaled' }, hema: null };
const indReady = () => true, hemaDraw = () => {}, hemaLive = () => {};
let due = clock + 1800e3, next = null;
const api = async (method, path) => { asked.push(path); return { mode: S.hemaSet.Mode, htf: [[clock - 1800e3, 5]], due, next }; };
/*APP*/
const bar = async (t) => { clock = t; S.bars.push({ t, c: 1 }); hemaTick(true); await new Promise((r) => setTimeout(r, 5)); return asked.length; };
(async () => {
  await hemaRequest();   // the history
  const t0 = clock, out = {};
  for (let i = 1; i <= 30; i++) out.seconds = await bar(t0 + i * 1000);   // a 1s chart: a bar closes every second
  due = t0 + 5400e3;                        // what the server says once it has closed
  out.htfClosed = await bar(t0 + 1800e3);   // the forming HTF bar closed: asked again
  out.after = await bar(t0 + 1801e3);       // till the next one closes, not again
  out.afterAgain = await bar(t0 + 1802e3);
  S.bars.splice(0, 1);                      // the oldest bars trimmed: the chart EMA starts elsewhere
  out.trimmed = await bar(t0 + 1803e3);
  out.trimmedAfter = await bar(t0 + 1804e3);
  S.hema.next = t0 + 1810e3;                // a 2D bar was over: the server's next bar
  out.calendar = await bar(t0 + 1810e3);
  S.hema = null;                            // the last ask failed
  out.failed = await bar(t0 + 1811e3);
  console.log(JSON.stringify(out));
})();"""


def test_htf_ema_is_asked_again_at_a_closed_bar_only_when_its_answer_may_have_changed():
    """Every closed chart bar asked the server for the HTF EMA again: on a seconds or tick chart, 14 to 400 days of
    1-minute bars worked out every second for the same answer. It is asked again when the forming HTF bar has closed,
    the chart's first bar is another, a bar of several sessions was over, or the last ask failed."""
    out = run_app(["hemaBusy", "hemaRequest", "hemaTick"], HEMA_JS)
    assert out == {"seconds": 1, "htfClosed": 2, "after": 2, "afterAgain": 2, "trimmed": 3, "trimmedAfter": 3,
                   "calendar": 4, "failed": 5}


# ---- the bar-close countdown on 2D, 3D, week and month charts: the server's last session in the bar -------------------

COUNTDOWN_JS = r"""
const fs = require('fs'), vm = require('vm'), DATA = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const ctx = { console, Intl, Date, Math, Number, String, JSON, Map };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(WEB + '/ta.js', 'utf8'), ctx);
vm.runInContext(`${APP}
const S = { instruments: { ES: { calendar: 'equity' }, CL: { calendar: 'energy' } }, symbol: 'ES', rth: false, spec: '2D', holidays: ${JSON.stringify(DATA.holidays)} };
const inst = () => S.instruments[S.symbol];
this.end = (sym, spec, t) => { S.symbol = sym; S.spec = spec; return msToCtInput(barCloseMs({ t })); };`, ctx);
const bad = DATA.bars.filter((b) => ctx.end(b.sym, b.spec, b.t) !== b.end).map((b) => `${b.sym} ${b.spec} ${b.open}: ${ctx.end(b.sym, b.spec, b.t)}, not ${b.end}`);
console.log(JSON.stringify({ n: DATA.bars.length, bad }));
"""


def test_the_countdown_on_2d_3d_week_and_month_bars_runs_to_the_close_of_the_last_session_the_server_puts_in_them(tmp_path):
    """The countdown under the price walked a 2D or 3D bar's days over trading days only, from its first; the server
    groups N Monday-Friday days from fixed starts, a closed day counted, and folds a holiday halt's session into the
    next trade date. So around Christmas, New Year's, Good Friday and the halts (Thanksgiving, Presidents Day) it ran on
    to a day of the next bar, as on a week whose Friday was a halt (Juneteenth, July 3 2026) or a month whose last day
    was (Memorial Day 2027). Now: every 2D, 3D, 1W, 2W, 1M and 3M bar of ES and CL from 2025 to 2028, as the server's
    TimeSeries groups their sessions."""
    from datetime import date, timedelta
    from chartsrv import sessions
    from chartsrv.bars import make_series, parse_spec
    from chartsrv.config import load_holidays
    from test_hunt_web_chart import app_part
    sessions.set_calendar(load_holidays(WEB.parent / "config", today=date(2026, 10, 26)))
    hol = {d.isoformat(): {k: v for k, v in h.items() if k != "name"} for d, h in sessions.holidays().items()}
    ct = lambda ms: sessions.to_ct(ms).strftime("%Y-%m-%dT%H:%M")   # noqa: E731
    bars = []
    for sym, group in (("ES", "equity"), ("CL", "energy")):
        days = []   # each session: its open and close
        d = date(2025, 1, 6)
        while d < date(2028, 12, 1):
            s = sessions.day_open(d)
            if d.weekday() < 5 and sessions.is_open(s + 60_000, group) and not sessions._no_session(s + 60_000):
                t = sessions._ct_ms(d, "11:00")
                while sessions.is_open(t, group):
                    t += 60_000
                days.append((s, t))
            d += timedelta(days=1)
        for spec in ("2D", "3D", "1W", "2W", "1M", "3M"):
            series, ends = make_series(parse_spec(spec), 0.25, 2, group=group, studies=False), {}
            for s, close in days:
                b = series.bucket(s + 60_000)
                ends[b] = max(ends.get(b, 0), close)
            bars += [{"sym": sym, "spec": spec, "t": b, "open": ct(b), "end": ct(e)} for b, e in sorted(ends.items())][1:-1]
    app = "\n".join(app_part(p) for p in ("TZ_EXCHANGE", "inputClock", "specSeconds", "dayCloseCt", "dayHalts", "barCloseMs"))
    app += "\nconst { toInput: msToCtInput, toMs: ctInputToMs } = inputClock(TZ_EXCHANGE);"
    (tmp_path / "bars.json").write_text(json.dumps({"holidays": hol, "bars": bars}))
    (tmp_path / "countdown.js").write_text(f"const WEB = {json.dumps(str(WEB))}, APP = {json.dumps(app)};\n{COUNTDOWN_JS}")
    r = subprocess.run(["node", str(tmp_path / "countdown.js"), str(tmp_path / "bars.json")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["n"] > 1500 and out["bad"] == []
