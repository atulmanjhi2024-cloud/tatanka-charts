"""Fixes from the third bug hunt of the web app: the Indicators dialog's and the Plan tab's per-symbol settings across
a symbol switch, a layout's indicator alert and its chart's RTH setting, a refused address after the server was down,
Time & Sales and the bubbles while their history loads, and the Journal's newest answer. The parts of app.js a test
needs (and web/indicators.js) run in node with the rest stubbed."""
import json
import shutil
import subprocess

import pytest

from test_hunt_web_chart import WEB, run_app

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
SCHEMA = json.loads((WEB.parent / "server" / "chartsrv" / "nt_schema.json").read_text())


def run_pages(pieces: str, js: str):
    """Runs js in node with vm, WEB, and APP: the source of the pieces of app.js, for js to run in pages of its own (vm
    contexts); what it logs last is the result (JSON)."""
    r = subprocess.run(["node", "-e", f"const vm = require('vm'), WEB = {json.dumps(str(WEB))}, APP = {json.dumps(pieces)};\n{js}"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().split("\n")[-1])


# ---- the Indicators dialog: a symbol's own settings go to that symbol, and only when edited ---------------------------

IND_JS = """
function el(tag = 'div') {
  return { tagName: tag.toUpperCase(), children: [], get options() { return this.children; }, style: {}, dataset: {}, hidden: false,
    textContent: '', className: '', value: '', id: '', sel: {}, classList: { toggle() {}, add() {}, remove() {} },
    append(...k) { this.children.push(...k); }, replaceChildren(...k) { this.children = k; }, addEventListener() {}, setAttribute() {},
    focus() {}, remove() {}, querySelectorAll: () => [],
    querySelector(s) { return s.startsWith('.set') || s.startsWith('.ind-item') || s === '.chg' ? null : (this.sel[s] ||= el()); } };
}
const find = (e, f) => (f(e) ? e : (e.children || []).reduce((r, k) => r || (typeof k === 'object' && find(k, f)), null));
function Option(text, value) { this.text = text; this.value = value; }
const document = { createElement: (t) => el(t), activeElement: null }, window = { innerWidth: 1400 };
/*IND*/
const TK_MULTI = false, ids = {}, $ = (q) => (ids[q] ||= el()), calls = [], local = { get: (k, d) => d, set() {} };
const START = { paste: { ES: 'ES 2026-10-26, 6850, 6870.25', NQ: 'NQ 2026-10-26, 25100, 25210.5' },
  ifthen: { ES: 'BULL|6850|6870|ES above', NQ: 'BEAR|25000|25100|NQ below' } };
let server;
const S = { st: { amt: true, ifthen: true, lines: true }, instruments: { ES: {}, NQ: {} }, ifSet: { AtrLength: 14 }, ifRows: [],
  amtServer: { schema: [{ name: 'ShowLevels', group: '2. Daily Levels', order: 1, label: 'Show levels', type: 'bool', default: true }],
    defaults: { ShowLevels: true }, overrides: {}, shared: [], version: '1' } };
const api = async (method, path, body) => {   // the server: a symbol's pasted levels, If/Then block and own settings
  const sym = path.split('/').pop().split('?')[0];
  if (method !== 'GET') calls.push(`${method} ${path} ${JSON.stringify(body)}`);
  if (path.startsWith('/api/amt/levels/')) { if (method === 'PUT') server.paste[sym] = body.text; return { paste: server.paste[sym], count: 3, origin: 'paste' }; }
  if (path.startsWith('/api/ifthen/')) return { rows: server.ifthen[sym] };
  const { RowsBlock, ...rest } = body;
  if (RowsBlock !== undefined) server.ifthen[sym] = RowsBlock;
  return { ...S.ind.symbols[path.split('/')[3]][sym], ...rest };
};
const noop = () => {}, toast = noop, fail = (e) => calls.push(`FAIL ${e.message}`), setWhere = () => 'the chart', tabSet = () => ({});
const [tabSetSave, replayBlocks, studiesChanged, renderPlanLevels, renderPlanLines, renderPlanIf, indCache, indApply, amtLayout, renderIfInfo] = Array(10).fill(noop);
const isExtSym = () => false, loadAmtServer = async () => {}, gapAlertsFollow = async () => {}, biasParse = (r) => (r ? r.split('\\n') : []);
const ifPrim = { update() {} };
let indDlg;
/*APP*/
const root = el();
root.hidden = true;   // as the page starts
indDlg = createIndicatorsDialog({ root, local, toast, fail, symbol: () => S.symbol, ask: async () => null, templates: () => null,
  adapters: () => ['amt', 'ifthen', 'lines'].map(indAdapter), allCharts: async () => 0 });
const ctl = (key, name) => find(root.sel['.ind-body'], (e) => e.id === `ind-${key}-${name}`);
const shown = () => find(root.sel['.ind-body'], (e) => e.className === 'sym')?.textContent;
const edit = (key, name, v) => { const c = ctl(key, name); c.value = v; c.onchange(); };
const load = () => Promise.all([loadPlanLevels(), ifRequest()]);   // the symbol's levels and block, as asked on a switch
// the chart on `sym`, its levels and block loaded (or still on their way: loaded false)
async function on(sym, loaded = true) {
  server = JSON.parse(JSON.stringify(START));
  S.symbol = sym; S.lvStatus = null; S.ifData = null;
  S.ind = { schema: SCHEMA, overrides: {},
    symbols: { lines: { ES: { RangeTop: 6900, RangeBottom: 6800, CustomLevels: '6850|ES mid' }, NQ: { RangeTop: 25300, RangeBottom: 24800 } },
      ifthen: { ES: { InstrumentLabel: 'ES' }, NQ: { InstrumentLabel: 'NQ' } } },
    per_symbol: { ifthen: ['InstrumentLabel', 'RowsBlock'], lines: ['CustomLevels', 'RangeBottom', 'RangeTop'] } };
  if (loaded) await load();
  calls.length = 0;
}
async function toNQ() { S.symbol = 'NQ'; S.lvStatus = null; S.ifData = null; await load(); }   // setSymbol's onSymbolChanged and subscribe
const ok = async () => { await root.sel['.ind-ok'].onclick(); return calls.slice(); };
const out = {};
for (const key of ['amt', 'ifthen', 'lines']) {
  await on('ES');
  indDlg.open(true, key);
  const before = shown();
  await toNQ();   // a layout's other chart, or the watchlist on the page around the charts, switches this chart
  out[`switch:${key}`] = { before, after: shown(), sent: await ok(), nq: [server.paste.NQ, server.ifthen.NQ] };
}
await on('ES');
indDlg.open(true, 'amt');
edit('amt', 'lvl_text', 'ES 2026-10-26, 6855');   // edited for ES, then the chart switched before OK
await toNQ();
out.editedThenSwitched = { sent: await ok(), shows: ctl('amt', 'lvl_text').value };
for (const key of ['amt', 'ifthen']) {
  await on('ES', false);
  indDlg.open(true, key);   // before the levels and the block have come
  await load();
  out[`loading:${key}`] = { sent: await ok(), es: [server.paste.ES, server.ifthen.ES] };
}
await on('ES', false);
indDlg.open(true, 'ifthen');
edit('ifthen', 'InstrumentLabel', 'ES mini');   // edited while the block is on its way
await load();
out.labelWhileLoading = await ok();
await on('ES');
indDlg.open(true, 'amt');
edit('amt', 'lvl_text', 'ES 2026-10-26, 6860');
out.edited = await ok();
console.log(JSON.stringify(out));
"""
IND_PARTS = ["setStudy", "LVL_TEXT", "IND_NOTES", "same", "amtShared", "pickSet", "amtOwn", "indReady", "indDefaults",
             "indSymbolSaved", "indPutSymbol", "loadPlanLevels", "savePlanLevels", "ifFetched", "ifRequest", "indAdapter"]


def test_the_indicators_dialog_saves_a_symbols_own_settings_only_to_it_and_only_when_edited():
    """OK with nothing edited saves none of a symbol's own settings: not after the chart switched to another symbol
    while the dialog was open (it writes ES's levels, If/Then block and Level Lines onto NQ), and not when the dialog
    opened before the symbol's levels or block had loaded (it clears them). The open dialog follows the switch; an
    edit made for ES is not written onto NQ, and one made while the block loads sends only itself."""
    js = IND_JS.replace("/*IND*/", (WEB / "indicators.js").read_text()).replace("SCHEMA", json.dumps(SCHEMA))
    out = run_app(IND_PARTS, f"(async () => {{ {js} }})().catch((e) => {{ console.error(e); process.exit(1); }});")
    for key in ("amt", "ifthen", "lines"):
        sw = out[f"switch:{key}"]
        assert sw["sent"] == [], key
        assert sw["nq"] == ["NQ 2026-10-26, 25100, 25210.5", "BEAR|25000|25100|NQ below"]
        assert sw["before"] == " (ES)" and sw["after"] == " (NQ)", key   # the dialog now shows NQ's own
    assert out["editedThenSwitched"] == {"sent": [], "shows": "NQ 2026-10-26, 25100, 25210.5"}
    for key in ("amt", "ifthen"):
        assert out[f"loading:{key}"] == {"sent": [], "es": ["ES 2026-10-26, 6850, 6870.25", "BULL|6850|6870|ES above"]}, key
    assert out["labelWhileLoading"] == ['PUT /api/indicators/ifthen/ES {"InstrumentLabel":"ES mini"}']
    assert out["edited"] == ['PUT /api/amt/levels/ES {"text":"ES 2026-10-26, 6860"}']


# ---- the Plan tab's If/Then box: another symbol's edited block does not stay after a switch ---------------------------

PLAN_IF_JS = """
const ids = {}, document = { activeElement: null }, $ = (q) => (ids[q] ||= { dataset: {}, value: '', textContent: '' });
const server = { ES: 'BULL|6850|6870|ES above', NQ: 'BEAR|25000|25100|NQ below' }, puts = [];
const api = async (method, path, body) => {
  const sym = path.split('/').pop().split('?')[0];
  if (method === 'PUT') { puts.push(`${path} ${body.rows}`); server[sym] = body.rows; return { rows: body.rows, updated: 2 }; }
  return { rows: server[sym], updated: 1 };
};
const S = { symbol: 'ES', instruments: { ES: {}, NQ: {} }, ifSet: { AtrLength: 14 }, ifData: null, ifRows: [], st: { ifthen: true } };
const TK_MULTI = true;   // the page around a layout's charts: hostActive() calls subscribe() for the chart last clicked
const noop = () => {}, ax = { full: { format: () => 'then' } }, zs = () => '', biasParse = (r) => (r ? r.split('\\n') : []);
const [renderIfInfo, amtLayout, toast, fail, setStudy, tapeSymbol] = Array(6).fill(noop);
const indReady = () => true, ifPrim = { update() {} }, indDlg = { refresh() {} };
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
/*APP*/
/*PLAN*/
const box = $('#pl-if'), type = (v) => { box.value = v; box.oninput({ target: box }); };
const state = () => ({ box: box.value, dirty: !!box.dataset.dirty, msg: $('#pl-if-msg').textContent });
(async () => {
  subscribe();
  await settle();
  type('BULL|6850|6870|ES edited, not saved');
  S.symbol = 'NQ';   // NQ clicked in the watchlist before saving
  subscribe();
  const asking = state();
  await settle();
  const after = state();
  await $('#pl-if-save').onclick();
  const saved = { puts: puts.splice(0), nq: server.NQ };
  type('BEAR|25000|25100|NQ edited');
  subscribe();   // another timeframe: the same symbol, the edit stays
  await settle();
  console.log(JSON.stringify({ asking, after, saved, spec: state() }));
})();
"""


def test_the_plan_tabs_if_then_box_shows_the_new_symbols_block_after_a_switch():
    """An unsaved edit of ES's If/Then block on the Plan tab did not go when the chart switched to NQ (subscribe()'s
    fill came before NQ's block, which then left the edited box alone), so Save wrote ES's block onto NQ. The box now
    empties on the switch and takes NQ's block when it comes; another timeframe on the same symbol keeps the edit."""
    from test_hunt_web_chart import app_lines
    js = PLAN_IF_JS.replace("/*PLAN*/", app_lines("$('#pl-if').oninput = markDirty;", "};"))
    out = run_app(["markDirty", "fillBox", "renderPlanIf", "ifFetched", "ifRequest", "subscribe"], js)
    assert out["asking"] == {"box": "", "dirty": False, "msg": ""}   # Save meanwhile: nothing of ES's to write
    assert out["after"] == {"box": "BEAR|25000|25100|NQ below", "dirty": False, "msg": "1 bands, saved then "}
    assert out["saved"] == {"puts": ["/api/ifthen/NQ BEAR|25000|25100|NQ below"], "nq": "BEAR|25000|25100|NQ below"}
    assert out["spec"] == {"box": "BEAR|25000|25100|NQ edited", "dirty": True, "msg": "1 bands, saved then "}


# ---- a layout's Alerts tab: a new indicator alert takes the active chart's RTH setting -------------------------------

RTH_JS = """
const TA = require('fs').readFileSync(`${WEB}/ta.js`, 'utf8');
// one page of a layout: the page around the charts (cell null) or a chart in it, on regular hours (rth) or not
function page(cell, rth) {
  const c = { console, posted: [], window: { parent: { postMessage: (m) => c.posted.push(m) } }, location: { origin: 'x' } };
  vm.createContext(c);
  vm.runInContext(TA, c);
  vm.runInContext(`
var TK_CELL = ${cell}, TK_MULTI = TK_CELL === null, linkedChange = false, specForced = '';
var S = { symbol: 'ES', spec: '5m', rth: ${rth}, st: {}, indCfg: [], layers: [], stHidden: {}, instruments: { ES: { rth: ['08:30', '15:00'] } } };
var ids = { '#al-iline': 'v', '#al-ip-len': '21', '#al-vs': 'price', '#al-ispec': '5m', '#al-op': 'cross' }, $ = (q) => ({ value: ids[q] ?? '' });
var noop = () => {}, toast = noop, num = Number, chartSym = (s) => (S.instruments[s] ? s : null), tabSet = () => ({}), setWhere = () => '';
var indClean = (x) => x, layersClean = (x) => x, renderStudiesMenu = noop, indSets = noop, renderAmtMenu = noop, renderPlanLines = noop;
var loadPlanLevels = noop, subscribe = noop, renderAlerts = noop, lookStore = noop, renderRth = noop, local = { set: noop };
var indDlg = { refresh() {} }, watch = { markActive() {} }, SPEC_RE = /^\\\\d+[smhtvrDWM]$/, dlg = { edit: null, on: 'close' };
${APP}
var emaAlert = () => indDlgBody({ symbol: 'ES' }, 'ema').rth;   // + on the Alerts tab: EMA crossing the price`, c);
  return c;
}
const host = page(null, false), chart2 = page(1, true);   // the page read chart 1's RTH (off) when it loaded
const heard = () => { while (chart2.posted.length) host.hostActive(chart2.posted.shift()); };
chart2.cellPost('active');   // chart 2 (RTH) clicked
heard();
const out = { clicked: host.emaAlert(), own: chart2.emaAlert() };
chart2.setRth(false);   // its RTH button: all hours now
heard();
out.toggled = host.emaAlert();
out.ownToggled = chart2.emaAlert();
console.log(JSON.stringify(out));
"""


def test_a_layouts_new_indicator_alert_computes_on_the_active_charts_hours():
    """On a layout the Alerts tab is the page around the charts, whose RTH setting was chart 1's when the page loaded:
    a new indicator alert computed on all hours for an RTH chart (or the reverse). The active chart now says its RTH
    setting with the rest of its state, also right after its RTH button changes it."""
    from test_hunt_web_ui import app_js
    out = run_pages(app_js("function cellPost(", "function hostActive(", "function indDlgBody(", "function setRth("), RTH_JS)
    assert out == {"clicked": True, "own": True, "toggled": False, "ownToggled": False}


# ---- a refused address says so, also when the server was down as the page opened -------------------------------------

WRONG_HOST_JS = """
// the server: down, up and refusing this address (/api/auth says wrong_host, /ws's handshake gets 403, which the
// browser reports as close code 1006 with no open), or up and answering
let server = 'down', now = 0;
const timers = [], els = {}, c = { console, TK_CELL: null, S: { ws: null, retry: 0 }, fetches: 0, sockets: [],
  $: (s) => (els[s] ||= { hidden: true, textContent: '', classList: { contains: () => false }, querySelector() { return (this.span ||= {}); }, focus() {} }),
  location: { hostname: 'mypc.lan', host: 'mypc.lan:8800', protocol: 'http:' },
  fetch: async () => {
    c.fetches++;
    if (server === 'down') throw new TypeError('Failed to fetch');
    return { json: async () => (server === 'refuse' ? { wrong_host: true } : { enabled: false, signed_in: false }) };
  },
  setTimeout: (f, ms) => { timers.push({ at: now + ms, f }); return timers.length; }, clearTimeout: (i) => { if (timers[i - 1]) timers[i - 1].f = null; },
  brandLoad() {}, setConn() {}, onMessage() {}, splashSay() {}, splashWatch() {}, needPassword() {}, document: { addEventListener() {} } };
c.WebSocket = class {
  constructor() {
    c.sockets.push(this);
    this.readyState = 0;
    setImmediate(() => {
      if (server === 'up') { this.readyState = 1; this.onopen(); } else { this.readyState = 3; this.onclose({ code: 1006 }); }
    });
  }
};
Object.assign(c.WebSocket, { OPEN: 1, CLOSED: 3 });
vm.createContext(c);
vm.runInContext(`let reconnectTimer = 0, loginShown = false;\\n${APP}\\nthis.start = start; this.shown = () => loginShown;`, c);
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
async function run(ms) {   // the clock runs on by ms, the timers due firing in order
  const until = now + ms;
  for (;;) {
    const t = timers.filter((x) => x.f).sort((a, b) => a.at - b.at)[0];
    if (!t || t.at > until) break;
    const f = t.f;
    t.f = null;
    now = t.at;
    f();
    await settle();
  }
  now = until;
}
const out = {};
await c.start();   // opened while the PC is asleep: the first ask fails, and so does every socket
await settle();
await run(20_000);
server = 'refuse';   // the PC is up: it refuses mypc.lan
await run(60_000);
const n = c.sockets.length;
await run(600_000);
out.refused = { sheet: els['#login']?.hidden === false, msg: els['#login-msg']?.textContent ?? '', stays: c.shown(), socketsAfter: c.sockets.length - n };
server = 'up';   // a page that connected, then the server restarted: no ask, a reconnect as before
vm.runInContext('loginShown = false; S.retry = 0; S.ws = null;', c);
c.fetches = 0;
c.start();
await settle();
const ws = c.S.ws;
ws.readyState = 3;
ws.onclose({ code: 1006 });
await settle();
out.dropped = { fetches: c.fetches, retry: timers.filter((x) => x.f).map((x) => x.at - now) };
console.log(JSON.stringify(out));
"""


def test_a_refused_address_says_so_when_the_server_was_down_as_the_page_opened():
    """Opened by a name the server refuses while the PC was asleep: the first /api/auth failed, so the page went
    straight to connecting, and the refused sockets (close 1006, never open) only ever brought more retries behind
    'Can't reach the server'. A socket that never opened now asks /api/auth why, so the refusal is said and the
    retries stop. A socket that was open and dropped reconnects as before, without asking."""
    from test_hunt_web_ui import app_js
    out = run_pages(app_js("function connect(", "async function authCheck(", "function showLogin(", "function wrongHost(",
                           "async function start("), f"(async () => {{ {WRONG_HOST_JS} }})().catch((e) => {{ console.error(e); process.exit(1); }});")
    refused = out["refused"]
    assert refused["msg"].startswith("This app doesn't answer to mypc.lan: add it to CHART_ALLOWED_HOSTS"), refused
    assert refused["sheet"] is True and refused["stays"] is True and refused["socketsAfter"] == 0
    assert out["dropped"] == {"fetches": 1, "retry": [500]}   # start()'s ask only


# ---- Time & Sales and the big-trade bubbles keep the prints that come while their history is asked for ---------------

TAPE_JS = """
function el() {
  return { children: [], textContent: '', className: '', title: '', value: '', scrollTop: 0, scrollHeight: 0,
    get childElementCount() { return this.children.length; },
    get lastElementChild() { const p = this; return { remove() { p.children.pop(); } }; },
    prepend(...k) { this.children.unshift(...k); }, append(...k) { this.children.push(...k); }, replaceChildren(...k) { this.children = k; } };
}
const els = {}, now = 1_800_000_000_000;
let answer;   // the GET's answer, given by hand: the server reads the stored trades once, then answers
const c = { console, Date, Math, S: { symbol: 'ES', instruments: { ES: {} }, big: [], bigKey: '',
    bars: Array.from({ length: 50 }, (_, i) => ({ t: now - (50 - i) * 60_000 })) },
  $: (s) => (els[s] ||= el()), document: { createElement: () => el() }, tapeMin: 1, tapeOpen: () => true, bigOn: () => true,
  bigMin: () => 25, ax: { tape: { format: (t) => String(t) } }, px: String, toast: (m) => { throw new Error(m); },
  bigPrim: { update() {} }, chart: { timeScale: () => ({ getVisibleLogicalRange: () => ({ from: 0, to: 49 }) }) },
  api: () => new Promise((r) => { answer = r; }) };
vm.createContext(c);
vm.runInContext(`${APP}\\nthis.tapeLoad = tapeLoad; this.onTrades = onTrades; this.bigFetch = bigFetch;`, c);
const tr = (t, sz) => [t, 5000, sz, 1, 1];
const out = {};
let p = c.tapeLoad();   // the Tape tab opened (or the symbol switched, or a reconnect) while trades print
c.onTrades({ symbol: 'ES', trades: [tr(now - 1000, 2), tr(now - 900, 3)] });   // the first was stored before the read
c.onTrades({ symbol: 'ES', trades: [tr(now - 800, 7)] });
answer({ trades: [tr(now - 1000, 2), tr(now - 2000, 4)] });   // newest first, as the server sends
await p;
c.onTrades({ symbol: 'ES', trades: [tr(now - 700, 1)] });
out.tape = els['#tp-list'].children.map((r) => Number(r.children[0].textContent) - now);
p = c.bigFetch();   // the bubbles, at the live edge
c.onTrades({ symbol: 'ES', trades: [tr(now - 2000, 30), tr(now - 500, 40)] });
c.onTrades({ symbol: 'ES', trades: [tr(now - 400, 60)] });
answer({ trades: [tr(now - 2000, 30)] });
await p;
c.onTrades({ symbol: 'ES', trades: [tr(now - 300, 26)] });
out.big = c.S.big.map((t) => t[2]);
console.log(JSON.stringify(out));
"""


def test_the_tape_and_the_bubbles_keep_the_prints_heard_while_their_history_loads():
    """Time & Sales showed the live prints that came while GET /api/trades was answered (a few seconds for 3 days
    of ticks), then the answer, read once before them, replaced the list; the bubbles did the same and then took
    themselves for up to date. Both now keep what came after the answer's newest, once each."""
    from test_hunt_web_ui import app_js
    pieces = app_js("function tapeRow(", "function tapeAdd(", "let tapeLoaded", "function onTrades(", "async function bigFetch(")
    out = run_pages(pieces, f"(async () => {{ {TAPE_JS} }})().catch((e) => {{ console.error(e); process.exit(1); }});")
    assert out["tape"] == [-700, -800, -900, -1000, -2000]   # newest at the top
    assert out["big"] == [30, 40, 60, 26]


# ---- the Journal tab and the fills on the chart show the newest request's answer -------------------------------------

JOURNAL_JS = """
function el() {
  return { children: [], value: '', checked: false, textContent: '', className: '', title: '', style: {},
    classList: { contains: (k) => k === 'on' },   // the Journal tab open
    set innerHTML(v) { this.children = [el(), el()]; }, get firstChild() { return this.children[0]; }, get lastChild() { return this.children.at(-1); },
    append(...k) { this.children.push(...k); }, replaceChildren(...k) { this.children = k; }, querySelectorAll: () => [] };
}
const els = {}, pending = [];
const c = { console, Date, Math, Number, Object, $: (s) => (els[s] ||= el()), document: { createElement: () => el() },
  S: { symbol: 'ES', instruments: { ES: { decimals: 2 } }, st: { fills: true }, bars: [{ t: 1 }], fills: [], fillTrades: [] },
  TK_MULTI: false, accts: '', local: { get: (k, d) => d }, fillAcct: () => c.accts,
  fillAccounts() {}, drawJournalDays() {}, journalShow() {}, fillPrim: { update() {} },
  money: (v) => `$${v}`, css: () => '', ax: { md: { format: String } },
  api: (m, url) => new Promise((r) => pending.push({ url, r })) };
vm.createContext(c);
vm.runInContext(`${APP}\\nthis.journalLoad = journalLoad; this.fillsFetch = fillsFetch;`, c);
const trade = (n, net, account = 'Sim101') => ({ symbol: 'ES', side: 'long', qty: 1, contract: 'ES 12-26', net, entry_ts: n, exit_ts: n + 1,
  entry: 1, exit: 2, points: 1, mae: 0, mfe: 1, account, fills: 2, commission: 1 });
const answer = (trades) => ({ accounts: ['Sim101', 'Live'], fills: trades.map((t) => ({ account: t.account })), trades,
  stats: { net: trades.reduce((a, t) => a + t.net, 0), trades: trades.length, win_rate: null, profit_factor: null, days: [] } });
const out = {};
c.$('#jr-range').value = '90';
const a = c.journalLoad();   // 90 days: slow on the server (MAE and MFE for every trade)
c.$('#jr-range').value = '1';   // Today picked meanwhile
const b = c.journalLoad();
pending[1].r(answer([trade(5, 50)]));   // Today's answer first
await b;
pending[0].r(answer([trade(1, -300), trade(2, -200), trade(3, 100), trade(5, 50)]));   // then the 90 days'
await a;
out.journal = { net: els['#jr-stats'].children[0].lastChild.textContent, rows: els['#jr-list'].children.length };
pending.length = 0;
const f1 = c.fillsFetch();   // the chart's fills, all accounts
c.accts = 'Live';   // the account picker: Live only
c.fillsFetch();   // asked once the one under way is in
out.asked = pending.length;
pending[0].r(answer([trade(1, -300), trade(5, 50, 'Live')])); pending[1].r(answer([trade(1, -300), trade(5, 50, 'Live')]));
await f1;
pending[2].r(answer([trade(5, 50, 'Live')])); pending[3].r(answer([trade(5, 50, 'Live')]));   // then Live's
await new Promise((r) => setImmediate(r));
out.fills = { fills: c.S.fills.map((x) => x.account), trades: c.S.fillTrades.map((t) => t.account) };
console.log(JSON.stringify(out));
"""


def test_the_journal_and_the_fills_show_the_newest_requests_answer():
    """Today picked while the 90 days' journal was still being worked out: Today's answer came first, then the 90
    days' landed over it and showed under 'Today'. The same for the account picker of the fills on the chart. An
    older request's answer is now left alone (the chart's fills ask one at a time: the newest pick once it is in)."""
    from test_hunt_web_ui import app_js
    out = run_pages(app_js("let fillTimer", "function fillsFrom", "async function fillsFetch", "const journalOpen"),
                    f"(async () => {{ {JOURNAL_JS} }})().catch((e) => {{ console.error(e); process.exit(1); }});")
    assert out["journal"] == {"net": "$50", "rows": 1}
    assert out["fills"] == {"fills": ["Live"], "trades": ["Live"]} and out["asked"] == 2
