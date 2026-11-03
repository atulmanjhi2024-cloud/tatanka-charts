"""Workspaces, second look (web/layout.js in node with web_harness.js; the parts of app.js a test needs alone in a vm):
an auto-save armed before a newer save is heard, windows of one browser following each other, a reconnect with no
workspace open, and the app.js halves of the reconnect and linked-timeframe fixes."""
import json
import shutil

import pytest

from test_hunt_web_ui import MAIN, UP_TO_DATE, WEB, app_js, node, run_app_js

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def test_an_auto_save_armed_before_a_newer_save_is_heard_does_not_save_over_it():
    """A change on the phone arms its auto-save; then it hears that the desktop saved Main (a reconnect's recheck, or
    the live broadcast). The 2 s timer does not fire: the desktop's save stays, and the phone keeps its dot."""
    out = node(f"""
const run = async (live) => {{
  const srv = makeServer([{json.dumps(MAIN)}]), keys = {json.dumps(UP_TO_DATE)};
  const tD = makeTimers(), tP = makeTimers();
  const D = srv.connect(makeBrowser(WEB, {{ device: 'desktop', keys }}).open({{ api: srv.api, timers: tD }}));
  const P = srv.connect(makeBrowser(WEB, {{ device: 'phone', keys }}).open({{ api: srv.api, timers: tP, width: 400 }}));
  await D.ws.start(); await P.ws.start();
  if (!live) P.connected = false;   // the phone app in the background
  if (live) P.local.set('sym', 'NQ');   // a watchlist symbol tapped: saved 2 s later
  D.local.set('chartTabs', '[ES 5m, CL 15m]');
  await tD.runAll();
  if (!live) {{
    P.connected = true;
    P.local.set('sym', 'NQ');   // tapped as the app comes back, before the hello's recheck answers
    await P.ws.recheck();
  }}
  await settle();
  const armed = tP.size;
  await tP.runAll();
  const w = srv.get('main');
  return {{ armed, tabs: w.data.keys.chartTabs, by: w.by, btn: P.els['ws-btn'].textContent, desktop: D.toasts }};
}};
out({{ recheck: await run(false), live: await run(true) }});""")
    for case in ("recheck", "live"):
        assert out[case] == {"armed": 0, "tabs": "[ES 5m, CL 15m]", "by": "desktop", "btn": "▦ Main •", "desktop": []}, case


def test_a_window_follows_another_window_that_loads_the_newer_save_of_its_workspace():
    """Two windows of one browser on Main; the laptop saves Main, both show the dot. Window 1 loads the newer one (▦ ->
    Main), or a new tab opens it: window 2 follows, so its old copy is never written into the settings both share and
    saved over the laptop's."""
    out = node("""
const run = async (newTab) => {
  const srv = makeServer([{ id: 'main', name: 'Main', updated: 1000, by: 'pc', data: { layout: '1', keys: { chartTabs: '[old tabs]' } } }]);
  const t = makeTimers(), B = makeBrowser(WEB, { keys: { chartTabs: '[old tabs]', wsId: 'main', wsStamp: '1000' } });
  const one = srv.connect(B.open({ api: srv.api, timers: t })), two = srv.connect(B.open({ api: srv.api, timers: t }));
  await one.ws.start(); await two.ws.start();
  await srv.api('PUT', '/api/workspaces/main', { data: { layout: '1', keys: { chartTabs: '[laptop tabs]' } }, by: 'laptop' });
  const dots = [one.els['ws-btn'].textContent, two.els['ws-btn'].textContent];
  if (newTab) await srv.connect(B.open({ api: srv.api, timers: t })).ws.start();   // its start() opens the newer one
  else { one.button('Main').onclick(); await settle(); }   // Load the newer one
  const again = srv.connect(B.open({ api: srv.api, timers: t }));   // the window that opened it, after its reload
  await again.ws.start();
  if (!two.reloads) two.local.set('chartTabs', '[old tabs, RTY 5m]');   // window 2's saveTabs(): the old Main's tabs
  await t.runAll();
  const w = srv.get('main');
  return { dots, two: two.reloads, tabs: w.data.keys.chartTabs, by: w.by, storage: B.shared.get('chartTabs') };
};
out({ load: await run(false), newTab: await run(true) });""")
    for case in ("load", "newTab"):
        assert out[case] == {"dots": ["▦ Main •", "▦ Main •"], "two": 1, "tabs": "[laptop tabs]", "by": "laptop",
                             "storage": "[laptop tabs]"}, case


def test_a_reconnect_with_no_workspace_open_here_says_nothing_changed():
    """No workspace open here: the last one deleted, a new device whose first list failed, or a browser that keeps no
    site data. A reconnect's recheck shows no 'changed on another device' and no dot."""
    out = node(f"""
const settleAll = async (ws) => {{ await ws.recheck(); await settle(); await ws.recheck(); await settle(); }};
// the last workspace deleted here
const srv = makeServer([{json.dumps(MAIN)}]);
const api = async (m, p, b) => {{
  if (m === 'DELETE') {{ srv.items = srv.items.filter((x) => `/api/workspaces/${{x.id}}` !== p); return {{ ok: true }}; }}
  return srv.api(m, p, b);
}};
const A = srv.connect(makeBrowser(WEB, {{ device: 'a', keys: {json.dumps(UP_TO_DATE)} }}).open({{ api, timers: makeTimers(), ask: async () => '' }}));
await A.ws.start();
await A.button('Delete').onclick();
await settle();
await settleAll(A.ws);
// a new device whose first list failed
const srv2 = makeServer([{json.dumps(MAIN)}]);
let first = true;
const api2 = async (m, p, b) => {{ if (first) {{ first = false; throw new Error('502'); }} return srv2.api(m, p, b); }};
const N = srv2.connect(makeBrowser(WEB, {{ device: 'n' }}).open({{ api: api2, timers: makeTimers() }}));
await N.ws.start();
await settleAll(N.ws);
// a browser that keeps no site data: every localStorage call throws
const vm = require('vm'), fs = require('fs'), els = {{}}, toasts = [];
const mk = () => ({{ children: [], style: {{}}, hidden: true, textContent: '', append() {{}}, replaceChildren() {{}}, addEventListener() {{}} }});
const ctx = {{ console, localStorage: new Proxy({{}}, {{ get() {{ throw new Error('SecurityError'); }} }}),
  window: {{ innerWidth: 400, addEventListener() {{}} }}, location: {{ search: '', reload() {{ toasts.push('reload'); }} }},
  document: {{ getElementById: (id) => (els[id] ||= mk()), createElement: mk, addEventListener() {{}} }},
  setTimeout: () => 0, clearTimeout() {{}}, ax: {{ date: {{ format: String }} }} }};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(WEB + '/layout.js', 'utf8') + '\\n;this.cw = createWorkspaces;', ctx);
const ws = ctx.cw({{ api: makeServer([{json.dumps(MAIN)}]).api, toast: (m) => toasts.push(m), fail: (e) => toasts.push(e.message),
  ask: async () => null, local: {{ get: (k, d) => d, set() {{}} }}, onActive() {{}} }});
await ws.start(); await settle();
await settleAll(ws);
out({{ deleted: [A.els['ws-btn'].textContent, A.toasts], newDevice: [N.els['ws-btn'].textContent, N.toasts],
  noSiteData: [els['ws-btn'].textContent, toasts] }});""")
    assert out == {"deleted": ["▦ Workspace", []], "newDevice": ["▦ Workspace", []],
                   "noSiteData": ["▦ Workspace", ["This browser is not keeping site data, so workspaces cannot open here"]]}


def test_every_hello_after_the_first_rechecks_the_workspace():
    """app.js's half of the reconnect fix: the first hello starts the workspaces, each one after it (a reconnect)
    rechecks them, so a save made elsewhere while the socket was down is heard."""
    out = run_app_js(app_js("function onMessage("), """
const calls = [], noop = () => {};
for (const f of ['versionCheck', 'updateNote', 'tabSync', 'buildHeader', 'renderAlerts', 'renderEvents', 'onSymbolChanged',
  'loadAmtServer', 'loadGapAlerts', 'moveSharedOut', 'loadInd', 'calLoad', 'tplLoad', 'shareLoad', 'setupCheck', 'cellPost', 'notifyStatus',
  'subscribe']) ctx[f] = noop;
Object.assign(ctx, { S: { st: {}, quotes: {}, spec: '5m' }, watch: { setDoc: noop }, chartSym: (s) => s, SPEC_RE: /^\\d+[mh]$/,
  drawings: { state: {} }, calOpen: () => false, journalOpen: () => false, TK_MULTI: false, tapeWant: '',
  workspaces: { start: () => calls.push('start'), recheck: () => calls.push('recheck') } });
vm.runInContext(/*APP*/ + ';this.onMessage = onMessage;', ctx);
const hello = { type: 'hello', instruments: { ES: {} }, quotes: [], watchlists: {}, levels: {}, alerts: [], events: [] };
for (let i = 0; i < 3; i++) ctx.onMessage(hello);
out(calls);""")
    assert out == ["start", "recheck", "recheck"]


def test_a_chart_says_when_its_data_source_forced_its_timeframe():
    """app.js's half of the linked-timeframe fix: a chart of a layout opening AAPL from 2000t is put on 5m by its data
    source (subscribe(), or the bars' answer in extLoad()); the next state it posts says forced, the one after does not."""
    out = run_app_js(app_js("function subscribe(", "async function extLoad(", "function cellPost("), """
const posted = [], noop = () => {}, el = { textContent: '', disabled: false };
for (const f of ['send', 'renderTf', 'renderStudiesMenu', 'barsLayout', 'watermarkLayout', 'replayLoad', 'tapeSymbol', 'amtRequest',
  'tpoRequest', 'vpAll', 'gapRequest', 'amtLayout', 'ifRequest', 'renderPlanIf', 'renderRth', 'tabSync', 'applyPriceFormat',
  'indsBuild', 'renderSymBtn', 'applyHistory', 'extMerge']) ctx[f] = noop;
let answer = new Promise(noop);   // subscribe()'s bars request: not answered in that part
Object.assign(ctx, {
  S: { symbol: 'AAPL', spec: '2000t', subId: 0, catalog: {}, cs: {}, vp: {}, st: {}, bars: [], layers: [], stHidden: {}, indCfg: [] },
  TK_MULTI: false, TK_CELL: 1, WL_FUT: /^([A-Z0-9]+)1!$/, VP_KINDS: [], extTimer: 0, specForced: '', linkedChange: false,
  isExtSym: () => true, extSource: () => 'yahoo', extSpecFor: (spec) => (/t$/.test(spec) ? '5m' : spec), futureAdd: noop,
  $: () => el, vol: { applyOptions: noop }, specLabel: (s) => s, drawings: { state: { loadedFor: 'AAPL' }, load: noop },
  local: { set: noop }, api: () => answer, indFmt: () => '', extSeries: () => false, extEvery: () => 1e9,
  chartSym: (s) => s, tabSet: () => ({}), setWhere: () => '', clearTimeout: noop, setTimeout: () => 0,
  window: { parent: { postMessage: (m) => posted.push([m.spec, m.forced]) } }, location: { origin: 'http://x' },
});
vm.runInContext(/*APP*/ + ';this.subscribe = subscribe; this.extLoad = extLoad; this.cellPost = cellPost;', ctx);
ctx.subscribe();   // AAPL has no tick bars: 5m
ctx.cellPost('state'); ctx.cellPost('state');
ctx.S.spec = '2000t'; ctx.S.ext = { symbol: 'AAPL' };
answer = Promise.resolve({ spec: '5m', bars: [] });   // the source answers with its nearest
await ctx.extLoad();
ctx.cellPost('state'); ctx.cellPost('state');
ctx.S.spec = '15m'; ctx.cellPost('state');   // picked on the chart
out(posted);""")
    assert out == [["5m", True], ["5m", False], ["5m", True], ["5m", False], ["15m", False]]


def test_a_window_following_another_waits_for_what_is_being_typed_and_writes_nothing_meanwhile():
    """Window 1 opens Swing while window 2 is in the middle of a dialog: window 2 opens it too through the app's idle
    reload (it waits while something is being typed), and until then saves and writes nothing of the workspace."""
    out = node("""
const srv = makeServer([
  { id: 'X', name: 'Day trading', updated: 1000, by: 'pc', data: { layout: '1', keys: { chartTabs: '[X tabs]' } } },
  { id: 'Y', name: 'Swing', updated: 1001, by: 'pc', data: { layout: '1', keys: { chartTabs: '[Y tabs]' } } }]);
const t = makeTimers(), B = makeBrowser(WEB, { keys: { chartTabs: '[X tabs]', wsId: 'X', wsStamp: '1000' } });
const asked = [];
const one = srv.connect(B.open({ api: srv.api, timers: t }));
const two = srv.connect(B.open({ api: srv.api, timers: t, reload: (why) => asked.push(why) }));   // busy: not yet
await one.ws.start(); await two.ws.start();
one.button('Swing').onclick();
await settle();
one.connected = false;
const again = srv.connect(B.open({ api: srv.api, timers: t }));
await again.ws.start();
two.local.set('chartTabs', '[X tabs, RTY 5m]');   // window 2, still showing Day trading, changes a tab
two.local.set('updateSeen', 'v9');                // not a workspace setting: kept
await t.runAll();
await two.button('Save').onclick();
await settle();
out({ asked, two: two.reloads, swing: srv.get('Y').data.keys.chartTabs, day: srv.get('X').data.keys.chartTabs,
  storage: B.shared.get('chartTabs'), seen: B.shared.get('updateSeen') });""")
    assert out == {"asked": ["Swing was opened in another window"], "two": 0, "swing": "[Y tabs]", "day": "[X tabs]",
                   "storage": "[Y tabs]", "seen": "v9"}


def test_while_a_window_waits_to_follow_another_its_page_and_charts_write_no_workspace_setting():
    """app.js: the page hands layout.js its idle reload (reloadWhenIdle), and local.set(), on the page and in each chart
    of it, writes no workspace setting while the page waits to follow another window; anything else is still kept."""
    wiring = app_js("workspaces = TK_CELL === null ? createWorkspaces({")
    out = run_app_js(app_js("const local = {"), f"""
const fs = require('fs'), store = new Map(), top = {{}};
Object.assign(ctx, {{   // a chart of a layout (?cell=1) in the page top
  location: {{ search: '?cell=1' }}, window: {{ innerWidth: 1400, top, addEventListener() {{}} }},
  localStorage: {{ getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) }},
}});
vm.runInContext(fs.readFileSync({json.dumps(str(WEB / "layout.js"))}, 'utf8'), ctx);
vm.runInContext('let workspaces = null;\\n' + /*APP*/ + ';this.local = local;', ctx);
ctx.local.set('sym', 'NQ');
top.tkWsWaiting = true;   // the page around this chart waits to follow another window
ctx.local.set('sym', 'CL'); ctx.local.set('spec', '1h'); ctx.local.set('updateSeen', 'v9');
// the page itself: what it hands createWorkspaces()
const page = {{ TK_CELL: null, why: [], api() {{}}, toast() {{}}, fail() {{}}, ask() {{}}, local: {{}}, hostActive() {{}},
  createWorkspaces: (env) => {{ page.env = env; return {{}}; }} }};
page.reloadWhenIdle = (why) => page.why.push(why);
vm.createContext(page);
vm.runInContext('let workspaces = null;\\n' + {json.dumps(wiring)}, page);
page.env.reload?.('Swing was opened in another window');
out({{ sym: store.get('c1.sym'), spec: store.get('c1.spec') ?? null, seen: store.get('updateSeen') ?? null, why: page.why }});""")
    assert out == {"sym": "NQ", "spec": None, "seen": "v9", "why": ["Swing was opened in another window"]}


def test_a_layout_change_or_auto_save_ticked_on_while_newer_elsewhere_does_not_save_over_it():
    """The dot shows (Main was saved on another device). Picking a layout reloads without saving this copy over that
    save (the reload opens the newer one); ticking "Save changes automatically" back on saves nothing either."""
    out = node(f"""
const find = (w, f) => {{ let b = null; const go = (e) => {{ if (!b && e && typeof e === 'object') {{ if (f(e)) b = e; else (e.children || []).forEach(go); }} }};
  w.els['ws-menu'].hidden = false; w.ws.render(); go(w.els['ws-menu']); return b; }};
const run = async (what) => {{
  const srv = makeServer([{json.dumps(MAIN)}]), keys = {{ ...{json.dumps(UP_TO_DATE)}, wsAuto: what === 'auto' ? '0' : '1' }};
  const tA = makeTimers(), tB = makeTimers();
  const A = srv.connect(makeBrowser(WEB, {{ device: 'A', keys }}).open({{ api: srv.api, timers: tA }}));
  const B = srv.connect(makeBrowser(WEB, {{ device: 'B', keys: {json.dumps(UP_TO_DATE)} }}).open({{ api: srv.api, timers: tB }}));
  await A.ws.start(); await B.ws.start();
  if (what === 'auto') A.local.set('chartTabs', '[an experiment]');   // kept here: auto-save is off
  B.local.set('chartTabs', '[ES 5m, CL 15m]');
  await tB.runAll();
  if (what === 'layout') find(A, (e) => e.title === '2 side by side').onclick();
  else {{ const c = find(A, (e) => e.title === 'Saves the open workspace as you change it').children[0]; c.checked = true; c.onchange(); }}
  await settle(); await tA.runAll();
  const w = srv.get('main');
  return {{ tabs: w.data.keys.chartTabs, by: w.by, btn: A.els['ws-btn'].textContent, reloads: A.reloads, B: B.toasts }};
}};
out({{ layout: await run('layout'), auto: await run('auto') }});""")
    assert out["layout"] == {"tabs": "[ES 5m, CL 15m]", "by": "B", "btn": "▦ Main •", "reloads": 1, "B": []}
    assert out["auto"] == {"tabs": "[ES 5m, CL 15m]", "by": "B", "btn": "▦ Main •", "reloads": 0, "B": []}
