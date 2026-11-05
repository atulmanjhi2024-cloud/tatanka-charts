"""Round 13 of the bug hunt, the AMT Toolkit's and Gap Analysis' alert settings: the alerts follow their alert switches
only (the alert runner has every group an alert switch on needs, whatever a chart shows), turning a symbol's AMT alerts
back on starts them fresh, saves send only what changed and every page hears the shared settings and the per-symbol
alert switches, presets leave the shared settings alone, Gap Analysis' alert switches are shared, not a chart's own (each
symbol's since round 14: test_gap_alerts_per_symbol.py), and an alert switch a chart had turned off before the switches
were shared is moved to the shared settings once."""
import asyncio
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from amt_parity import synth
from chartsrv import amt_hub
from chartsrv.amt_runner import ct
from chartsrv.models import Tick
from chartsrv.store import Store

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


class FakeClient:
    def __init__(self):
        self.amt_state, self.amt_entries, self.amt_asked, self.subs, self.loading, self.closed = {}, {}, {}, {}, set(), False

    def put(self, m):
        pass


def session(tmp_path, monkeypatch, shared: dict, *, toggles=(), chart=False, syn=15, start="08:00"):
    """A synthetic ES session (amt_parity.synth) through the real AmtHub: 1-minute history to `start` CT, then each
    minute live with its ticks and the once-a-second step. shared: the shared settings (PUT /api/amt/settings); toggles:
    (HH:MM, on) for the symbol's AMT alerts switch; chart: a 5m chart on the alert runner, so it keeps committing while
    alerts are off. Returns the alerts sent as (HH:MM, text), and the hub (with each live minute's ticks: h.ticks)."""
    monkeypatch.setenv("CHART_WORKERS", "0")
    bars = synth(15, 7, datetime(2026, 10, 2, syn, 0))
    h0, m0 = map(int, start.split(":"))
    i0 = next(k for k, b in enumerate(bars) if ct(b.t) == datetime(2026, 10, 2, h0, m0))
    store = Store(tmp_path / f"c{len(list(tmp_path.iterdir()))}.db")
    store.write([], [("ES", b) for b in bars[:i0]])
    store.set_setting("amt_alerts", {"ES": {"on": True}})
    now = [bars[i0 - 1].t + 30_000]
    monkeypatch.setattr(amt_hub.time, "time", lambda: now[0] / 1000)
    inst = SimpleNamespace(tick_size=0.25, rth=("08:30", "15:00"), calendar="equity")
    m = SimpleNamespace(store=store, instruments={"ES": inst}, m1={"ES": SimpleNamespace(bars=[bars[i0 - 1]])},
                        last_tick={"ES": Tick("ES", now[0], bars[i0 - 1].c, 1)}, flush_for_read=lambda *a: [],
                        daily=SimpleNamespace(current=lambda sym: ([], None, "")))
    sent = []

    async def go():
        h = amt_hub.AmtHub(m)
        h.ticks = {}
        h.set_overrides({"alertcap_enable": False, **shared})   # no storm cap: every alert counts; the alert runner loads
        h._emit = lambda sym, t, price, text: sent.append((ct(now[0]).strftime("%H:%M"), text))
        while not h.entries or not all(x.ready.is_set() for x in h.entries.values()):
            await asyncio.sleep(0.01)
        (e,) = h.entries.values()
        if chart:
            c = FakeClient()
            e.subs.add((c, 1))
            c.amt_entries[1] = e
        h.step()
        at = dict(toggles)
        for b in bars[i0:]:
            hm = ct(b.t).strftime("%H:%M")
            if hm in at:
                h.set_alerts("ES", at[hm])
            now[0] = b.t + 30_000
            m.m1["ES"].bars.append(b)
            m.last_tick["ES"] = Tick("ES", now[0], b.c, 1)
            h.on_minute("ES", b)
            h.ticks[hm] = (b.o, b.h, b.l, b.c)
            for px in h.ticks[hm]:
                h.on_live_tick(Tick("ES", now[0], px, 1))
            h.step()
        return h
    return sent, asyncio.run(go())


# ---- r13-alerts-1: Alert VWAP Cross fires with Enable RTH VWAP only a chart's own ------------------------------------

def test_an_rth_vwap_alert_fires_with_enable_rth_vwap_ticked_on_one_chart_only(tmp_path, monkeypatch):
    """The dialog's OK on one chart: 'Alert VWAP Cross' goes to the shared settings, 'Enable RTH VWAP' (off by
    default) stays that chart's. The alert runner computes the RTH VWAP all the same, so its crosses are sent (before:
    none, as the runner had no VWAP)."""
    sent, h = session(tmp_path, monkeypatch, {"vwap_alert": True})
    vwap = [x for x in sent if "RTH VWAP Cross" in x[1]]
    assert vwap and vwap[0][0] >= "08:30", sent
    assert h.settings().vwap_enable is False                 # the starting settings: no chart draws it unless it says so
    (e,) = h.entries.values()
    assert e.runner.S.vwap_enable is True and h.alert_key("ES") != ("ES", amt_hub.ALERT_MINUTES)   # not the 5m charts' runner


# ---- r13-scope-2: the alerts follow their alert switches, not a group's Enable --------------------------------------

def test_the_alert_runner_has_every_group_an_alert_switch_on_needs(tmp_path, monkeypatch):
    """Every group's Enable off in the starting settings (Apply to all charts), their Alert switches on: the alerts
    follow the switches, so IB and OVN crosses are sent; a 5m chart on the starting settings still draws none of them.
    A group whose Alert switches are off stays off in the alert runner. The level zones' width is shared too, as Zone
    Entry alerts use it."""
    off = {"ib_enable": False, "amt_enable": False, "cc_enable": False, "inv_enable": False, "showSpike": False,
           "npoc_enable": False, "npoc_w_show": False}
    sent, h = session(tmp_path, monkeypatch, off)
    names = {t.split("] ")[1].split(" Cross")[0] for _, t in sent if " Cross " in t}
    assert {"IBH", "IBL"} & names and {"OVN High", "OVN Low"} & names, sent
    (e,) = h.entries.values()
    alert = e.runner.S
    assert all(getattr(alert, k) is True for k in off)
    assert all(getattr(h.settings(), k) is False for k in off)   # what a chart on the starting settings shows
    # the IB's alert switches off: the alert runner has no IB either (nothing to alert on)
    ov = amt_hub.alert_overrides({**off, "ib_alert_ibh": False, "ib_alert_ibl": False})
    assert ov.get("ib_enable") is False and "amt_enable" not in ov
    assert amt_hub.ZONE_WIDTH <= amt_hub.SHARED
    # a chart's own Enable and zone width: the Enable is its own, the width the shared one
    _, mine = h.chart_settings({"ib_enable": False, "lvl_zoneWidthTicks": 20})
    assert mine.get("ib_enable") is False and "lvl_zoneWidthTicks" not in mine


# ---- r13-alerts-2: AMT alerts turned back on start afresh ------------------------------------------------------------

@pytest.mark.parametrize("chart", [False, True])
def test_amt_alerts_turned_back_on_send_nothing_from_while_they_were_off(tmp_path, monkeypatch, chart):
    """ES's AMT alerts off 08:20-08:50 and 09:50-10:30 (with no chart on the alert runner, or a 5m chart on it that
    keeps it committing): nothing is sent while off, and turning them back on sends nothing that alerts on all along
    would not send then (before: the 08:30 bar's 'Inventory Trap Active' at 08:50, and at 10:30 a 'Developing VAL
    Cross' from a price seen before they went off)."""
    toggles = (("08:20", False), ("08:50", True), ("09:50", False), ("10:30", True))
    base, _ = session(tmp_path, monkeypatch, {}, chart=chart, syn=12, start="07:50")
    got, h = session(tmp_path, monkeypatch, {}, toggles=toggles, chart=chart, syn=12, start="07:50")
    assert not [x for x in got if "08:20" <= x[0] < "08:50" or "09:50" <= x[0] < "10:30"]
    for on in ("08:50", "10:30"):
        ticks = h.ticks[on]
        for _, text in (x for x in got if x[0] == on):
            cross = re.search(r" Cross (Up|Down) @ ([\d.]+) \| close ([\d.]+)", text)
            if not cross:   # a notice of a bar: only one alerts on all along would send then (the forming bar's)
                assert (on, text) in base, (on, text)
                continue
            # a cross: between two of this minute's prices, the first one seen since they were turned on
            up, level, close = cross[1] == "Up", float(cross[2]), float(cross[3])
            assert any(b == close and (a <= level < b if up else a >= level > b) for a, b in zip(ticks, ticks[1:])), (on, text)
    assert [x for x in got if x[0] >= "10:35"] == [x for x in base if x[0] >= "10:35"]   # and as usual from the next bar


# ---- the pages: the real AMT and Gap Analysis adapters, the Indicators dialog and onMessage in node --------------------

PAGE_JS = r"""
function el(tag = 'div') {
  return { tagName: tag.toUpperCase(), children: [], get options() { return this.children; }, style: {}, dataset: {}, hidden: false,
    textContent: '', className: '', value: '', id: '', sel: {}, classList: { toggle() {}, add() {}, remove() {} },
    append(...k) { this.children.push(...k); }, replaceChildren(...k) { this.children = k; }, addEventListener() {}, setAttribute() {},
    focus() {}, remove() {}, querySelectorAll: () => [],
    querySelector(s) { return s.startsWith('.set') || s.startsWith('.ind-item') || s === '.chg' ? null : (this.sel[s] ||= el()); } };
}
var find = (e, f) => (f(e) ? e : (e.children || []).reduce((r, k) => r || (typeof k === 'object' && find(k, f)), null));
function Option(text, value) { this.text = text; this.value = value; }
var document = { createElement: (t) => el(t), activeElement: null, querySelectorAll: () => [] }, window = { innerWidth: 1400 };
/*IND*/
var TK_MULTI = false, ids = {}, $ = (q) => (ids[q] ||= el()), local = { get: (k, d) => d, set() {} };
var S = { symbol: 'ES', spec: '5m', st: { amt: true, gap: true }, instruments: { ES: { rth: ['08:30', '15:00'] }, NQ: { rth: ['08:30', '15:00'] } },
  ind: { schema: { gap: GAP_SCHEMA }, overrides: {}, symbols: {}, per_symbol: {} }, amtServer: {}, lvStatus: { paste: '' },
  gaps: { gaps: [] }, gapError: '', gapAlerts: {}, tabs: [], sharedSet: {},
  gapSet: { RthStart: 83000, RthEnd: 150000, MinimumGapPoints: 0, AlertOnNewGap: true, AlertOnGapFill: true, EnabledAnalysis: true, days: 90 } };
var own = {}, gapDays = 90, noop = () => {}, toasts = [], toast = (t) => toasts.push(t), fail = (e) => toasts.push('FAIL ' + e.message);
var setWhere = () => 'this chart', tabSet = () => own, setStudy = noop, amtFollow = noop, savePlanLevels = async () => {};
var loadPlanLevels = noop, setTimeout = () => 0, gapSpecOk = () => true, indReady = () => true, saveTabs = noop;
var indPut = async (key, o) => { S.ind.overrides[key] = o; };
var tabSetSave = (key, d) => {   // the open tab's own, and (indSets) the chart's Gap settings from them
  own = { ...own };
  if (d && Object.keys(d).length) own[key] = d; else delete own[key];
  if (key === 'gap') S.gapSet = { ...S.gapSet, ...d };
};
var T = { kinds: {}, names: (k) => Object.keys(T.kinds[k] || {}), data: (k, n) => T.kinds[k]?.[n], del: noop,
  save: async (k, n, d) => { (T.kinds[k] ||= {})[n] = JSON.parse(JSON.stringify(d)); } };
var indDlg;
/*APP*/
var root = el(); root.hidden = true;
indDlg = createIndicatorsDialog({ root, local, toast, fail, symbol: () => S.symbol, ask: async () => 'ES daily', templates: () => T,
  adapters: () => ['amt', 'gap'].map(indAdapter), allCharts: async () => 0 });
var ctl = (key, name) => find(root.sel['.ind-body'], (e) => e.id === `ind-${key}-${name}`);
var edit = (key, name, v) => { const c = ctl(key, name); if (typeof v === 'boolean') c.checked = v; else c.value = v; c.onchange(); };
var shows = (key, name) => { const c = ctl(key, name); return c.type === 'checkbox' ? c.checked : c.value; };
var ok = async () => { await root.sel['.ind-ok'].onclick(); };
var applyAll = async () => { await root.sel['.ind-all'].onclick(); };
var preset = async (v) => { await root.sel['.ind-tpl'].onchange({ target: { value: v } }); };
var hello = async () => { await loadAmtServer(); await loadGapAlerts(); };
"""
PAGE_PARTS = ["LVL_TEXT", "IND_NOTES", "same", "amtShared", "amtBySymbol", "pickSet", "amtOwn", "amtStart", "amtEff", "amtValue",
              "amtMinutes", "renderAmtMenu", "amtPatch", "indDefaults", "indAdapter", "amtHeard", "loadAmtServer", "GAP_SWITCH",
              "GAP_SHARED", "gapShown", "hhmm", "gapRth",
              "gapRthOk", "GAP_RTH_BAD", "gapAlertsPut", "renderGapInfo", "renderGapMenu", "loadGapAlerts", "gapAlertsFollow",
              "sharedMoved", "moveSharedOut", "onMessage"]
# The server, as app.py has it: PUT /api/amt/settings replaces the settings, PATCH merges what it is given (a value at
# its default goes), the alert settings (PER_SYMBOL) those of ?symbol= (with none, every symbol's) in SRV.syms, the
# rest every chart's in SRV.amt; PUT /api/gaps/alerts gives what it is given to the alerts and alert switches of
# ?symbol= (with none, every symbol's; the switches in SRV.switches, symbol -> {new, fill}); DELETE
# /api/gaps/alerts/notice hands the switches the first start turned off to the first page that asks.
DRIVER_JS = r"""
const vm = require('vm');
const SRV = { amt: {}, syms: {}, alerts: {}, gap: {}, switches: { ES: { new: true, fill: true }, NQ: { new: true, fill: true } }, notice: [] };
const calls = [], copy = (x) => JSON.parse(JSON.stringify(x));
const clean = (o) => Object.fromEntries(Object.entries(o).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(DEFAULTS[k])));
const api = async (method, path, body) => {
  if (method !== 'GET') calls.push([method, path, body && copy(body)]);
  const [at, query] = path.split('?'), sym = new URLSearchParams(query || '').get('symbol');
  if (at === '/api/amt') {
    return copy({ schema: SCHEMA, defaults: DEFAULTS, overrides: SRV.amt, symbols: SRV.syms, shared: SHARED, per_symbol: PER_SYMBOL,
      version: '1.1.0', alerts: SRV.alerts });
  }
  if (at === '/api/amt/settings') {
    const part = (alerts) => Object.fromEntries(Object.entries(body).filter(([k]) => PER_SYMBOL.includes(k) === alerts));
    SRV.amt = clean(method === 'PATCH' ? { ...SRV.amt, ...part(false) } : part(false));
    for (const s of sym ? [sym] : ['ES', 'NQ']) SRV.syms[s] = clean(method === 'PATCH' ? { ...(SRV.syms[s] || {}), ...part(true) } : part(true));
    return copy({ overrides: SRV.amt, symbols: SRV.syms });
  }
  if (at === '/api/amt/settings/check') return { overrides: body };
  if (path === '/api/gaps/alerts' && method === 'GET') return copy({ alerts: SRV.gap, switches: SRV.switches, watches: {}, notice: SRV.notice });
  if (path === '/api/gaps/alerts/notice') { const notice = SRV.notice; SRV.notice = []; return { notice }; }
  if (at === '/api/gaps/alerts') {
    for (const s of sym ? [sym] : ['ES', 'NQ']) {
      for (const k of ['new', 'fill']) if (k in body) SRV.switches[s] = { ...SRV.switches[s], [k]: body[k] };
      if (SRV.gap[s]) SRV.gap[s] = { ...SRV.gap[s], ...body };
    }
    return copy({ alerts: SRV.gap, switches: SRV.switches });
  }
  throw new Error(`no ${method} ${path}`);
};
const heard = (p) => {   // the broadcasts after a change
  p.onMessage({ type: 'amt_settings', overrides: copy(SRV.amt), symbols: copy(SRV.syms), alerts: copy(SRV.alerts) });
  p.onMessage({ type: 'gap_alerts', alerts: copy(SRV.gap), switches: copy(SRV.switches) });
};
const page = () => { const c = { console, api }; vm.createContext(c); vm.runInContext(PAGE, c); return c; };
const sent = (path) => calls.filter((c) => c[1].split('?')[0] === path).map((c) => [c[0], c[2]]);
const start = (s = 'ES') => copy({ ...SRV.amt, ...(SRV.syms[s] || {}) });   // what a chart of the symbol starts from
(async () => {
  const out = {};
  /*SCENARIO*/
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def pages(scenario: str) -> dict:
    """Runs the scenario (JS, with page(), heard(), SRV, calls, sent() and out) against the fake server; returns out."""
    from chartsrv.amt_hub import DEFAULTS, PER_SYMBOL, SCHEMA, SHARED
    from test_hunt_web_chart import WEB, app_part
    schema = json.loads(Path(amt_hub.__file__).with_name("nt_schema.json").read_text())["gap"]
    page = PAGE_JS.replace("/*IND*/", (WEB / "indicators.js").read_text()).replace(
        "/*APP*/", "\n".join(app_part(p) for p in PAGE_PARTS)).replace("GAP_SCHEMA", json.dumps(schema))
    js = (f"const PAGE = {json.dumps(page)};\nconst SCHEMA = {json.dumps(SCHEMA)}, DEFAULTS = {json.dumps(DEFAULTS)}, "
          f"SHARED = {json.dumps(sorted(SHARED))}, PER_SYMBOL = {json.dumps(sorted(PER_SYMBOL))};\n"
          + DRIVER_JS.replace("/*SCENARIO*/", scenario))
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().split("\n")[-1])


# ---- r13-scope-1, r13-alerts-3: a page with an old copy saves only what it changed -------------------------------------

@needs_node
def test_a_page_with_an_old_copy_of_the_shared_settings_saves_only_what_it_changed():
    """Chart 1 unticks 'Alert 80% Rule Confirmed' and pastes the levels link; the layout's page (or another device),
    which heard nothing of it, then changes Level Alert Mode, and later applies Profile Row Width to all charts: the
    80% alert stays off and the link stays (before: the old copy was put back, the alert on again and the link gone)."""
    out = pages("""
  const A = page(), B = page();
  await A.hello(); await B.hello();
  A.indDlg.open(true, 'amt');
  A.edit('amt', 'cc_alert_80', false);
  A.edit('amt', 'LevelsFile', 'https://levels.example/{root}');
  await A.ok();
  out.afterA = start();
  B.indDlg.open(true, 'amt');
  B.edit('amt', 'LevelAlertMode', 'Both');
  await B.ok();
  out.afterB = start();
  B.indDlg.open(true, 'amt');
  B.edit('amt', 'vwap_width', 3);
  await B.applyAll();
  out.afterAll = start();
  out.sentB = sent('/api/amt/settings').slice(1);
  out.toasts = [...A.toasts, ...B.toasts].filter((t) => t.startsWith('FAIL'));
""")
    link = {"cc_alert_80": False, "LevelsFile": "https://levels.example/{root}"}
    assert out["afterA"] == link and out["toasts"] == []
    assert out["afterB"] == {**link, "LevelAlertMode": "Both"}
    assert out["afterAll"] == {**link, "LevelAlertMode": "Both", "vwap_width": 3}
    assert out["sentB"] == [["PATCH", {"LevelAlertMode": "Both"}], ["PATCH", {"vwap_width": 3}]]   # what changed there only


# ---- r13-alerts-3, r13-scope-5: every page hears the shared settings and the per-symbol alert switches ------------------

@needs_node
def test_every_page_shows_the_shared_settings_and_alert_switches_as_they_are_changed_elsewhere():
    """Another chart turns ES's AMT and gap alerts on, unticks 'Alert 80% Rule Confirmed' and Gap Analysis' 'Alert on
    full gap fill': this page's Studies menu ticks both alert boxes and its open dialog shows both switches off (before:
    as at its hello until it reconnected)."""
    out = pages("""
  const A = page();
  await A.hello();
  A.indDlg.open(true, 'amt');
  out.before = [A.shows('amt', 'cc_alert_80'), A.$('#amt-alerts').checked, A.$('#gap-alerts').checked];
  SRV.syms = { ES: { cc_alert_80: false } }; SRV.alerts = { ES: { on: true } };
  SRV.switches.ES.fill = false; SRV.gap = { ES: { on: true, new: true, fill: false, rth: false } };
  heard(A);
  out.amt = [A.shows('amt', 'cc_alert_80'), A.$('#amt-alerts').checked, A.$('#amt-info').textContent];
  A.indDlg.open(true, 'gap');
  out.gap = [A.shows('gap', 'AlertOnGapFill'), A.$('#gap-alerts').checked];
  A.S.symbol = 'NQ'; A.indDlg.refresh();   // its NQ tab: NQ's, as they were
  out.nq = A.shows('gap', 'AlertOnGapFill');
""")
    assert out["before"] == [True, False, False]
    assert out["amt"] == [False, True, "Running on 5-minute bars. Alerts: Cross."]
    assert out["gap"] == [False, True] and out["nq"] is True


# ---- r13-scope-3: a preset is the chart's own settings only -----------------------------------------------------------

@needs_node
def test_loading_a_preset_leaves_the_shared_alert_switches_and_levels_file_alone():
    """'ES daily' saved with Profile Period Daily (and Alert 80% Rule Confirmed unticked in the dialog then): the preset
    holds Daily only. Later the levels link is pasted and the 80% alert unticked; loading 'ES daily' (or one saved
    before, with the alert switches in it) and OK sets this chart's Profile Period and leaves both as they are (before:
    both put back for every chart)."""
    out = pages("""
  const A = page();
  await A.hello();
  A.indDlg.open(true, 'amt');
  A.edit('amt', 'mvp_tf_mode', 'Daily');
  A.edit('amt', 'cc_alert_80', false);
  await A.preset('s');
  out.saved = A.T.kinds['ind:amt']['ES daily'].settings;
  A.indDlg.open(false);
  SRV.amt = { LevelsFile: 'https://levels.example/{root}' }; SRV.syms = { ES: { cc_alert_80: false } };
  heard(A);
  A.T.kinds['ind:amt'].old = { settings: { mvp_tf_mode: 'Monthly', cc_alert_80: true, lvl_alerts: true, LevelsFile: '' } };
  for (const name of ['ES daily', 'old']) {
    A.indDlg.open(true, 'amt');
    await A.preset(`a:${name}`);
    out[name] = { shows: [A.shows('amt', 'mvp_tf_mode'), A.shows('amt', 'cc_alert_80'), A.shows('amt', 'LevelsFile')] };
    await A.ok();
    out[name].server = start();
    out[name].own = copy(A.own.amt || {});
  }
""")
    link = {"cc_alert_80": False, "LevelsFile": "https://levels.example/{root}"}
    assert out["saved"] == {"mvp_tf_mode": "Daily"}
    assert out["ES daily"] == {"shows": ["Daily", False, link["LevelsFile"]], "server": link, "own": {"mvp_tf_mode": "Daily"}}
    assert out["old"] == {"shows": ["Monthly", False, link["LevelsFile"]], "server": link, "own": {"mvp_tf_mode": "Monthly"}}


# ---- r13-scope-4: Gap Analysis' alert switches are shared, not a chart's own (each symbol's since round 14) ---------------

@needs_node
def test_gap_alert_switches_show_and_save_as_the_symbols():
    """'Alert on full gap fill' unticked on another ES chart (the server's gap alerts for ES have it off). This ES
    chart, whose own Gap settings still have it ticked from before, shows it off, and changing only its Minimum Gap
    Points sends no switch, so fill alerts stay off; unticking 'Alert on new gap' here sends that one alone, for ES
    (before: the box showed this tab's, and every save gave every symbol this tab's switches, fill alerts on again).
    NQ's switches and alerts are left as they were."""
    out = pages("""
  SRV.switches.ES.fill = false; SRV.gap = { ES: { on: true, new: true, fill: false, rth: false, MinimumGapPoints: 0 },
    NQ: { on: true, new: true, fill: true, rth: false, MinimumGapPoints: 0 } };
  const A = page();
  A.own = { gap: { AlertOnGapFill: true, RthStart: 90000 } };
  A.S.gapSet = { ...A.S.gapSet, ...A.own.gap };
  await A.hello();
  A.indDlg.open(true, 'gap');
  out.shows = A.shows('gap', 'AlertOnGapFill');
  A.edit('gap', 'MinimumGapPoints', 2);
  await A.ok();
  A.indDlg.open(true, 'gap');
  A.edit('gap', 'AlertOnNewGap', false);
  await A.ok();
  out.sent = sent('/api/gaps/alerts').map((c) => c[1]);
  out.paths = calls.filter((c) => c[1].startsWith('/api/gaps/alerts')).map((c) => c[1]);
  out.server = [SRV.switches, SRV.gap.ES.fill, SRV.gap.ES.MinimumGapPoints, SRV.gap.NQ];
  out.own = A.own.gap;
""")
    assert out["shows"] is False
    assert out["sent"] == [{"RthStart": 90000, "RthEnd": 150000, "MinimumGapPoints": 2},
                           {"new": False, "RthStart": 90000, "RthEnd": 150000, "MinimumGapPoints": 2}]
    assert out["paths"] == ["/api/gaps/alerts?symbol=ES"] * 2
    assert out["server"] == [{"ES": {"new": False, "fill": False}, "NQ": {"new": True, "fill": True}}, False, 2,
                             {"on": True, "new": True, "fill": True, "rth": False, "MinimumGapPoints": 0}]
    assert out["own"] == {"RthStart": 90000, "MinimumGapPoints": 2}   # the chart's own, no alert switch in it


# ---- r13-scope-6: an alert switch a chart turned off before they were shared is moved to the shared settings once ------

@needs_node
def test_an_alert_switch_a_chart_had_turned_off_is_moved_to_the_shared_settings_once():
    """A tab's own set from before (Alert 80% Rule Confirmed off, its own zone width, the storm cap off, IB High
    alert on) and the chart's linked set (Alert High/Low Break off, Gap Analysis' 'Alert on full gap fill' off), both
    ES's: the alerts it had off go off on every ES chart (not the storm cap: off, it sends more; and nothing turned
    on; NQ's as they were), every shared setting leaves the chart's own, the rest stays, a toast says so, and a second
    hello does nothing (before: the alerts it had off came back on, their boxes ticked again)."""
    out = pages("""
  SRV.syms = { ES: { ib_alert_ibh: false } };
  const A = page();
  A.S.tabs = [{ sym: 'ES', own: { set: { amt: { cc_alert_80: false, vwap_width: 3, lvl_zoneWidthTicks: 12, alertcap_enable: false, ib_alert_ibh: true } } } },
    { sym: 'ES', link: true }];
  A.S.sharedSet = { amt: { pd_alert_hl: false }, gap: { AlertOnGapFill: false, ShowGapZones: false } };
  await A.hello();
  await A.moveSharedOut();
  out.server = [start(), copy(SRV.switches)];
  out.sets = [A.S.tabs[0].own.set, A.S.sharedSet];
  out.toasts = A.toasts;
  const n = calls.length;
  await A.moveSharedOut();
  const B = page();   // a chart with nothing of its own from before
  await B.hello();
  await B.moveSharedOut();
  out.again = calls.length - n;
""")
    assert out["server"] == [{"ib_alert_ibh": False, "cc_alert_80": False, "pd_alert_hl": False},
                             {"ES": {"new": True, "fill": False}, "NQ": {"new": True, "fill": True}}]   # the tabs' symbol's
    assert out["sets"] == [{"amt": {"vwap_width": 3}}, {"gap": {"ShowGapZones": False}}]
    assert len(out["toasts"]) == 1 and "Alert 80% Rule Confirmed" in out["toasts"][0] and "full gap fill" in out["toasts"][0]
    assert "Off on every ES chart, as this chart had them: " in out["toasts"][0]
    assert out["again"] == 0


# ---- the server: changes merged, and every page told -----------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "60")
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as c:
        m = app.state.market
        heard, orig = [], m.broadcast
        m.broadcast = lambda msg: (heard.append(msg), orig(msg))[1]
        c.heard = heard
        yield c


def test_amt_settings_changes_merge_and_every_page_hears_them(client):
    """r13-alerts-3, r13-scope-1, r13-scope-5: PATCH /api/amt/settings takes only what changed, over the server's as
    they are now (one back at its default goes); every change of the settings (every chart's, a symbol's alert
    settings) or of a symbol's AMT alert switch is broadcast to every page with all of them (before: none, so a page
    kept its copy from its hello). The alert settings are the symbol's given (round 14)."""
    start = client.get("/api/amt").json()
    every, es = start["overrides"], start["symbols"]["ES"]
    link = {"cc_alert_80": False, "LevelsFile": "https://levels.example/{root}"}
    r = client.patch("/api/amt/settings?symbol=ES", json=link).json()
    assert r["overrides"] == {**every, "LevelsFile": link["LevelsFile"]} and r["symbols"]["ES"] == {**es, "cc_alert_80": False}
    r = client.patch("/api/amt/settings?symbol=ES", json={"LevelAlertMode": "Both"})   # another page's change
    assert r.json()["symbols"]["ES"] == {**es, "cc_alert_80": False, "LevelAlertMode": "Both"}
    assert "cc_alert_80" not in client.patch("/api/amt/settings?symbol=ES", json={"cc_alert_80": True}).json()["symbols"]["ES"]
    assert client.patch("/api/amt/settings?symbol=ES", json={"cc_alert_80": "off"}).status_code == 400
    assert client.put("/api/amt/alerts/ES", json={"on": True}).status_code == 200
    told = [x for x in client.heard if x["type"] == "amt_settings"]
    now = client.get("/api/amt").json()
    assert len(told) == 4 and told[-1] == {"type": "amt_settings", "overrides": now["overrides"], "symbols": now["symbols"],
                                           "alerts": now["alerts"]}
    assert told[-1]["alerts"]["ES"] == {"on": True}
    r = client.put("/api/amt/settings", json={}).json()           # PUT: the whole set, as before (no symbol: every one's)
    assert r["overrides"] == {} and r["symbols"]["ES"] == {} and all(v == {} for v in r["symbols"].values())
    assert client.heard[-1]["type"] == "amt_settings" and client.heard[-1]["overrides"] == {}


def test_gap_alert_switches_are_the_symbols_and_every_page_hears_them(client):
    """r13-scope-4, r13-scope-5 (each symbol's since round 14): 'Alert on full gap fill' unticked on an ES chart is
    every ES chart's; a save on another ES chart that changed only its minimum size leaves it off (before: that
    chart's own switches went to every symbol), and NQ's alerts keep their switches and minimum; a symbol switched on
    later takes its own switches, as set on a chart of it before; a switch alone leaves the RTH and minimum as they
    are; with no symbol, every symbol's (as before); and every change is broadcast."""
    body = {"RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 0.0}
    for sym in ("ES", "NQ"):
        assert client.put(f"/api/gaps/alerts/{sym}", json={**body, "on": True}).status_code == 200
    r = client.put("/api/gaps/alerts?symbol=ES", json={**body, "fill": False})            # ES chart 1: fill unticked
    assert r.json()["switches"]["ES"] == {"new": True, "fill": False} and r.json()["switches"]["NQ"] == {"new": True, "fill": True}
    r = client.put("/api/gaps/alerts?symbol=ES", json={**body, "MinimumGapPoints": 2.0})  # ES chart 2: its minimum only
    assert {s: (c["fill"], c["MinimumGapPoints"]) for s, c in r.json()["alerts"].items()} == {"ES": (False, 2.0), "NQ": (True, 0.0)}
    assert client.put("/api/gaps/alerts?symbol=GC", json={"fill": False}).status_code == 200   # GC's, its alerts off
    on = {"on": True, "RthStart": 80000, "RthEnd": 133000, "new": True, "fill": True}
    assert client.put("/api/gaps/alerts/CL", json=on).json()["fill"] is True      # CL's own: not ES's
    assert client.put("/api/gaps/alerts/GC", json=on).json()["fill"] is False     # GC's, as set before
    r = client.put("/api/gaps/alerts", json={"new": False})                        # a switch alone, every symbol's
    a = r.json()["alerts"]
    assert all(c["new"] is False for c in a.values()) and all(sw["new"] is False for sw in r.json()["switches"].values())
    assert {s: c["fill"] for s, c in a.items()} == {"ES": False, "NQ": True, "CL": True, "GC": False}
    assert (a["ES"]["RthStart"], a["ES"]["MinimumGapPoints"], a["CL"]["RthStart"], a["NQ"]["MinimumGapPoints"]) == (83000, 2.0, 80000, 0.0)
    assert client.put("/api/gaps/alerts", json={"RthStart": 90000}).status_code == 400   # the RTH times go together
    assert client.put("/api/gaps/alerts?symbol=ES", json={"RthStart": 90000}).status_code == 400
    got = client.get("/api/gaps/alerts").json()
    assert got["switches"]["ES"] == {"new": False, "fill": False} and got["switches"]["NQ"] == {"new": False, "fill": True}
    told = [x for x in client.heard if x["type"] == "gap_alerts"]
    assert len(told) == 8 and told[-1] == {"type": "gap_alerts", "alerts": got["alerts"], "switches": got["switches"]}


def test_gap_alert_switches_from_before_they_were_shared_are_off_where_any_symbols_is():
    """Saved before the switches were every chart's: each symbol's config had its own (the chart that last saved
    gave them all its own, a symbol switched on from another chart since kept that one's). The shared switch is off
    where any is (it silences, never turns one on), and every config takes it: every symbol's (round 14: each
    symbol's from then on, starting from those)."""
    from chartsrv.gaps_service import GapService
    inst = SimpleNamespace(rth=("08:30", "15:00"))

    def service(saved: dict) -> GapService:
        store = SimpleNamespace(get_setting=lambda k, d=None: saved.get(k, d), set_setting=saved.__setitem__)
        return GapService(SimpleNamespace(store=store, instruments={"ES": inst, "NQ": inst, "CL": inst}))
    g = service({"gap_alerts": {"ES": {"on": True, "new": True, "fill": False}, "NQ": {"on": True, "new": True, "fill": True}}})
    assert g.switches() == {s: {"new": True, "fill": False} for s in ("ES", "NQ", "CL")}
    assert all(c["fill"] is False and c["new"] is True for c in g.alert_cfg.values())
    # no symbol's alerts on yet, 'Alert on new gap' off in the starting settings (Apply to all charts)
    assert service({"ind_settings": {"gap": {"AlertOnNewGap": False}}}).switches()["NQ"] == {"new": False, "fill": True}
    assert service({"gap_alert_switches": {"new": True, "fill": True}, "ind_settings": {"gap": {"AlertOnNewGap": False}}}
                   ).switches()["NQ"] == {"new": True, "fill": True}   # once stored, those
