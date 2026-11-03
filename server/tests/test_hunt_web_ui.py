"""Fixes from the bug hunt of the web app. layout.js and watchlist.js run in node (web_harness.js); the parts of app.js
a test needs run alone in a vm."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"
HARNESS = Path(__file__).with_name("web_harness.js")
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def node(js):
    """Runs `js` (an async body) with the harness's helpers and WEB; returns what it passes to out()."""
    code = f"""
const {{ makeBrowser, makeServer, makeTimers, makeWatchlist, settle }} = require({json.dumps(str(HARNESS))});
const WEB = {json.dumps(str(WEB))};
const out = (v) => console.log(JSON.stringify(v));
(async () => {{ {js} }})().catch((e) => {{ console.error(e); process.exit(1); }});"""
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)


def app_js(*starts):
    """Pieces of web/app.js, each from the line starting with one of `starts` to the line closing its block (app.js
    closes a top-level block at the start of a line). A piece that is not there is left out."""
    src, out = (WEB / "app.js").read_text(), []
    for s in starts:
        i = src.find("\n" + s)
        if i < 0:
            continue
        j = src.index("\n}", i + 1)
        out.append(src[i + 1:src.index("\n", j + 1)])
    return "\n".join(out)


def run_app_js(pieces, js):
    """Runs `js` (an async body) beside an empty vm context `ctx`: it gives `ctx` the globals the pieces of app.js
    use, then runs them there, their source being the string /*APP*/ stands for; returns what it passes to out()."""
    code = f"""
const vm = require('vm'), ctx = {{ console }};
vm.createContext(ctx);
const out = (v) => console.log(JSON.stringify(v));
(async () => {{ {js} }})().catch((e) => {{ console.error(e); process.exit(1); }});"""
    code = code.replace("/*APP*/", json.dumps(pieces))
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)


MAIN = {"id": "main", "name": "Main", "updated": 1000, "by": "seed", "data": {"layout": "1", "keys": {"chartTabs": "[ES 5m]", "sym": "ES"}}}
UP_TO_DATE = {"chartTabs": "[ES 5m]", "sym": "ES", "wsId": "main", "wsStamp": "1000"}


def test_a_reconnect_hears_a_save_made_while_the_socket_was_down():
    """The phone's socket drops, the desktop saves Main, the phone comes back: it shows Main as newer elsewhere and
    does not save its old copy over it (app.js calls recheck() on each hello after the first)."""
    out = node(f"""
const srv = makeServer([{json.dumps(MAIN)}]), keys = {json.dumps(UP_TO_DATE)};
const tD = makeTimers(), tP = makeTimers();
const D = srv.connect(makeBrowser(WEB, {{ device: 'desktop', keys }}).open({{ api: srv.api, timers: tD }}));
const P = srv.connect(makeBrowser(WEB, {{ device: 'phone', keys }}).open({{ api: srv.api, timers: tP, width: 400 }}));
await D.ws.start(); await P.ws.start();
P.connected = false;   // the phone app in the background
D.local.set('chartTabs', '[ES 5m, CL 15m]');
await tD.runAll();
P.connected = true;    // back: a new socket, a new hello
if (P.ws.recheck) await P.ws.recheck();
await settle();
P.local.set('sym', 'NQ');   // a watchlist symbol tapped
await tP.runAll();
const w = srv.get('main');
out({{ tabs: w.data.keys.chartTabs, by: w.by, btn: P.els['ws-btn'].textContent, toasts: P.toasts, desktop: D.toasts }});""")
    assert out["tabs"] == "[ES 5m, CL 15m]" and out["by"] == "desktop"
    assert out["btn"] == "▦ Main •" and out["toasts"] == ["Main was changed on another device: ▦ → Load the newer one"]
    assert out["desktop"] == []


def test_a_window_follows_another_window_of_the_browser_that_opens_a_workspace():
    """Two windows on Day trading; window 1 opens Swing. Window 2 opens it too (a reload): its own changes would
    otherwise land in the settings both share, now Swing's, and window 1 would save them into Swing."""
    out = node("""
const srv = makeServer([
  { id: 'X', name: 'Day trading', updated: 1000, by: 'pc', data: { layout: '1', keys: { chartTabs: '[X tabs]' } } },
  { id: 'Y', name: 'Swing', updated: 1001, by: 'pc', data: { layout: '1', keys: { chartTabs: '[Y tabs]' } } }]);
const t = makeTimers(), B = makeBrowser(WEB, { keys: { chartTabs: '[X tabs]', wsId: 'X', wsStamp: '1000' } });
const one = srv.connect(B.open({ api: srv.api, timers: t })), two = srv.connect(B.open({ api: srv.api, timers: t }));
await one.ws.start(); await two.ws.start();
one.button('Swing').onclick();   // ▦ -> Swing: its settings written, then the window reloads
await settle();
one.connected = false;
const again = srv.connect(B.open({ api: srv.api, timers: t }));   // window 1 after its reload
await again.ws.start();
if (!two.reloads) two.local.set('chartTabs', '[X tabs, RTY 5m]');   // window 2, still on Day trading, changes a tab
await t.runAll();
out({ one: one.reloads, two: two.reloads, swing: srv.get('Y').data.keys.chartTabs, day: srv.get('X').data.keys.chartTabs });""")
    assert out == {"one": 1, "two": 1, "swing": "[Y tabs]", "day": "[X tabs]"}


def test_rename_sends_the_name_only():
    """Rename keeps what the workspace holds on the server: a newer save from another device (which a reload here
    still opens), and changes kept here while auto-save is off."""
    out = node(f"""
const srv = makeServer([{json.dumps(MAIN)}]), keys = {json.dumps(UP_TO_DATE)};
const tA = makeTimers(), tB = makeTimers(), bB = makeBrowser(WEB, {{ device: 'B', keys }});
const A = srv.connect(makeBrowser(WEB, {{ device: 'A', keys }}).open({{ api: srv.api, timers: tA }}));
const B = srv.connect(bB.open({{ api: srv.api, timers: tB, ask: async () => ' Main (ES) ' }}));
await A.ws.start(); await B.ws.start();
A.local.set('chartTabs', '[ES 5m, NQ 1m]');
await tA.runAll();   // B: newer elsewhere
await B.button('Rename…').onclick();
await settle();
const w = srv.get('main'), r = {{ name: w.name, tabs: w.data.keys.chartTabs, btn: B.els['ws-btn'].textContent, toastsA: [...A.toasts] }};
B.connected = false;
const B2 = srv.connect(bB.open({{ api: srv.api, timers: tB }}));   // B reloads: the newer one opens
await B2.ws.start();
await settle();
r.reopened = B2.reloads;
r.storageTabs = bB.shared.get('chartTabs');

// auto-save off: a change here stays here, through a rename too
const tC = makeTimers(), C = srv.connect(makeBrowser(WEB, {{ device: 'C', keys: {{ ...keys, chartTabs: '[ES 5m, NQ 1m]',
  wsStamp: String(srv.get('main').updated), wsAuto: '0' }} }}).open({{ api: srv.api, timers: tC, ask: async () => 'Main 2' }}));
await C.ws.start();
C.local.set('chartTabs', '[an experiment]');
await tC.runAll();
await C.button('Rename…').onclick();
await settle();
r.auto = {{ name: srv.get('main').name, tabs: srv.get('main').data.keys.chartTabs, btn: C.els['ws-btn'].textContent }};
out(r);""")
    assert out["name"] == "Main (ES)" and out["tabs"] == "[ES 5m, NQ 1m]"
    assert out["btn"] == "▦ Main (ES) •"   # still newer elsewhere
    assert out["toastsA"] == []            # what A has is what the server holds
    assert out["reopened"] == 1 and out["storageTabs"] == "[ES 5m, NQ 1m]"
    assert out["auto"] == {"name": "Main 2", "tabs": "[ES 5m, NQ 1m]", "btn": "▦ Main 2"}


def test_the_timeframe_link_passes_on_a_picked_timeframe_not_a_forced_one():
    """Link the charts: Timeframe. A chart that opens AAPL from 2000t is put on 5m by its data source (forced): the
    other chart keeps 2000t. A timeframe that chart picks, or a tab with another symbol and timeframe, still goes."""
    out = node("""
const B = makeBrowser(WEB, { keys: { layout: '2', sync: JSON.stringify({ interval: true }) } });
const host = B.open({ api: async () => ({ workspaces: [] }), timers: makeTimers() });
const [one, two] = host.frames();
host.fromFrame(0, { type: 'state', sym: 'ES', spec: '2000t' });
host.fromFrame(1, { type: 'state', sym: 'NQ', spec: '2000t' });
host.fromFrame(1, { type: 'state', sym: 'AAPL', spec: '5m', forced: true });
const forced = one.posted.splice(0);
host.fromFrame(1, { type: 'state', sym: 'AAPL', spec: '15m' });
host.fromFrame(1, { type: 'state', sym: 'CL', spec: '1h' });
out({ frames: host.frames().length, forced, picked: one.posted, two: two.posted });""")
    assert out["frames"] == 2 and out["forced"] == []
    assert out["picked"] == [{"tk": True, "type": "spec", "spec": "15m", "link": True}, {"tk": True, "type": "spec", "spec": "1h", "link": True}]
    assert out["two"] == []


def test_a_watchlist_formula_whose_previous_value_is_0():
    """VIX-20 when VIX closed at 20: a change but no percent. Its details show, and quotes keep coming."""
    out = node("""
const w = makeWatchlist(WEB, { quotes: { VIX: { s: 'VIX', last: 21.35, prev: 20, ts: 1, rt: 'delayed' } } });
w.list.setDoc({ lists: [{ id: 'a', name: 'A', items: ['VIX-20', 'ES1!'] }], flags: {}, names: {} });
let error = null;
try {
  w.click(0);   // the formula's row: its details
  w.quotes.VIX = { s: 'VIX', last: 21.5, prev: 20, ts: 2, rt: 'delayed' };
  w.list.onQuotes([w.quotes.VIX]);
} catch (e) { error = e.message; }
const [, px] = w.detail.children;
out({ error, last: px?.children[0]?.textContent, chg: px?.children[1]?.textContent });""")
    assert out == {"error": None, "last": "1.50", "chg": "+1.50"}


def test_a_fresh_install_says_set_a_password_when_the_first_ask_failed():
    """The page's first /api/auth failed (the PC was starting), so it connected; the server closes the socket (4401)
    because the install is locked. The page says why and stops, instead of reconnecting every second."""
    out = run_app_js(app_js("function needPassword(", "async function authCheck("), """
const els = { '#login': { hidden: true }, '#login-pw': { hidden: false }, '#login-msg': { textContent: '' } };
Object.assign(ctx, {
  $: (s) => (els[s] ||= { hidden: false, textContent: '' }),
  fetch: async () => ({ json: async () => ({ enabled: false, signed_in: false, need_password: true }) }),
  timers: [], setTimeout: (f, ms) => ctx.timers.push(ms), connect() {}, showLogin() { ctx.login = true; },
});
vm.runInContext('let reconnectTimer = 0;\\n' + /*APP*/ + ';this.authCheck = authCheck;', ctx);
await ctx.authCheck(true);
out({ retries: ctx.timers, sheet: !els['#login'].hidden, pw: els['#login-pw'].hidden, msg: els['#login-msg'].textContent });""")
    assert out == {"retries": [], "sheet": True, "pw": True, "msg": "Set a password on the PC first: open the app there (http://localhost:8800)."}


SIGN_IN = """
const els = { '#login': { hidden: true }, '#login-pw': { hidden: false }, '#login-ok': { hidden: false }, '#login-msg': { textContent: '' } };
Object.assign(ctx, {
  $: (s) => (els[s] ||= { hidden: false, textContent: '', focus() {} }), TK_CELL: null, location: { hostname: 'mypc.lan' },
  answers: [], fetch: async () => { const a = ctx.answers.shift(); return { json: async () => a }; },
  timers: [], setTimeout: (f, ms) => ctx.timers.push(ms), clearTimeout() {}, connects: 0, connect() { ctx.connects++; },
  brandLoad() {},
});
vm.runInContext('let reconnectTimer = 0, loginShown = false;\\n' + /*APP*/ + ';this.authCheck = authCheck; this.start = start; this.shown = () => loginShown;', ctx);
const sheet = () => ({ sheet: !els['#login'].hidden, pw: !els['#login-pw'].hidden, ok: !els['#login-ok'].hidden, msg: els['#login-msg'].textContent });
"""
SIGN_IN_JS = ("async function authCheck(", "function showLogin(", "function needPassword(", "function wrongHost(",
              "async function start(")


def test_the_sign_in_comes_back_after_set_a_password_first():
    """A phone on a fresh install was told to set a password on the PC first. Once that is done it comes back to the
    app and gets the sign-in: the password field and Sign in, not the old message with nothing to type in."""
    out = run_app_js(app_js(*SIGN_IN_JS), SIGN_IN + """
ctx.answers.push({ enabled: false, signed_in: false, need_password: true }, { enabled: true, signed_in: false });
await ctx.authCheck(true);
const before = sheet();
await ctx.authCheck(true);   // back on the phone: the socket closed 4401
out({ before, after: sheet() });""")
    assert out["before"] == {"sheet": True, "pw": False, "ok": False,
                             "msg": "Set a password on the PC first: open the app there (http://localhost:8800)."}
    assert out["after"] == {"sheet": True, "pw": True, "ok": True, "msg": ""}


def test_an_address_the_app_does_not_answer_to_says_so():
    """Opened by a name the server refuses (not in CHART_ALLOWED_HOSTS): the page says which name and where to add
    it, and stops, instead of trying to connect for good behind 'Can't reach the server'."""
    refused = {"detail": "not this app's address (CHART_ALLOWED_HOSTS)", "wrong_host": True}
    for how in ("await ctx.start();", "await ctx.authCheck(true);"):
        out = run_app_js(app_js(*SIGN_IN_JS), SIGN_IN + f"""
ctx.answers.push({json.dumps(refused)});
{how}
out({{ ...sheet(), connects: ctx.connects, retries: ctx.timers, stays: ctx.shown() }});""")
        assert out.pop("connects") == 0 and out.pop("retries") == [] and out.pop("stays") is True, how
        assert out.pop("msg").startswith("This app doesn't answer to mypc.lan: add it to CHART_ALLOWED_HOSTS"), how
        assert out == {"sheet": True, "pw": False, "ok": False}, how


def test_a_charts_reload_waits_for_a_dialog_on_the_page_around_it():
    """A chart of a layout hears of an update and reloads the whole page: not while Connections is open there."""
    out = run_app_js(app_js("function reloadWhenIdle("), """
const doc = (modal) => ({ visibilityState: 'visible', activeElement: null, querySelectorAll: () => (modal ? [modal] : []) });
const conn = { hidden: false }, page = { document: doc(conn), location: { reload() { ctx.reloads += 1; } } };
const chart = { document: doc(null) };
page.frames = [chart, { document: doc(null) }];
Object.assign(ctx, { window: { top: page }, document: chart.document, reloads: 0, timers: [], toast() {}, swOn: async () => {},
  setTimeout: (f) => ctx.timers.push(f) });
vm.runInContext(/*APP*/ + ';this.reloadWhenIdle = reloadWhenIdle;', ctx);
ctx.reloadWhenIdle('Updated', 'v2');
const tick = async () => { ctx.timers.shift()?.(); await new Promise((r) => setImmediate(r)); };
await tick();
const open = ctx.reloads;
conn.hidden = true;   // Connections closed
await tick();
out({ open, closed: ctx.reloads });""")
    assert out == {"open": 0, "closed": 1}


def test_the_alert_dialog_saves_once_while_its_request_is_on_the_way():
    """A double tap on Create (or Save of an indicator alert, a new one and the old one deleted) sends it once."""
    out = run_app_js(app_js("$('#al-form').onsubmit = "), """
const els = { '#al-ok': { disabled: false } }, calls = [], waits = [];
Object.assign(ctx, {
  $: (s) => (els[s] ||= { disabled: false }),
  dlg: { edit: null }, dlgBody: () => ({ kind: 'ind', symbol: 'ES' }), sameAlert: () => false, describe: () => ['ES'],
  api: (m, p) => { calls.push(`${m} ${p}`); return new Promise((r) => waits.push(() => r({ id: 'n1' }))); },
  toast() {}, fail: (e) => calls.push(`fail ${e.message}`), closeAlertDialog() { ctx.dlg.edit = null; },
});
vm.runInContext(/*APP*/, ctx);
const submit = () => els['#al-form'].onsubmit({ preventDefault() {} });
const drain = async () => { while (waits.length) { waits.shift()(); await new Promise((r) => setImmediate(r)); } };
const a = submit(), b = submit();
await drain(); await a; await b;
const created = calls.splice(0);
ctx.dlg.edit = { id: 'old', kind: 'ind' };
const c = submit(), d = submit();
await drain(); await c; await d;
out({ created, edited: calls, ok: els['#al-ok'].disabled });""")
    assert out == {"created": ["POST /api/alerts"], "edited": ["POST /api/alerts", "DELETE /api/alerts/old"], "ok": False}
