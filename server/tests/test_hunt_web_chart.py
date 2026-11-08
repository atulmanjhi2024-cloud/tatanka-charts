"""Fixes from the bug hunt of the chart page (web/app.js and what it asks the server): Compare lines on a tab switch,
the footprint on RTH charts and on bars sharing an open time, per-chart tab views and tab ids in a layout, the AMT
Toolkit's style after a start with no bars, Daily SMA x4 on a chart that starts empty, and the indicators' price
format on free-data charts. The app.js parts run in node with what they call stubbed."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"
APP = (WEB / "app.js").read_text().split("\n")
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def _span(name: str) -> tuple[int, int]:
    """Where a top-level declaration of web/app.js starts and ends (line indexes): to where its brackets close."""
    head = re.compile(rf"^(?:async )?function {re.escape(name)}\(|^(?:const|let) (?:.*?, )?{re.escape(name)} = ")
    start = next(i for i, line in enumerate(APP) if head.match(line))
    depth = 0
    for i in range(start, len(APP)):
        depth += sum(APP[i].count(c) for c in "({[") - sum(APP[i].count(c) for c in ")}]")
        if depth <= 0:
            return start, i
    raise AssertionError(f"{name}: no end")


def app_part(name: str) -> str:
    """One top-level declaration of web/app.js as written there: a function, or the const or let line naming it."""
    start, end = _span(name)
    return "\n".join(APP[start:end + 1])


def app_block(first: str, last: str) -> str:
    """The lines of web/app.js from the one starting with `first` to the end of the declaration `last`."""
    start = next(i for i, line in enumerate(APP) if line.startswith(first))
    return "\n".join(APP[start:_span(last)[1] + 1])


def app_lines(first: str, last: str) -> str:
    """The lines of web/app.js from the one starting with `first` to the next one starting with `last`."""
    start = next(i for i, line in enumerate(APP) if line.startswith(first))
    return "\n".join(APP[start:next(i for i in range(start, len(APP)) if APP[i].startswith(last)) + 1])


def run_app(parts: list[str], js: str):
    """Runs js in node with the named parts of app.js where it says /*APP*/; what it logs last is the result (JSON)."""
    src = js.replace("/*APP*/", "\n".join(app_part(p) for p in parts))
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().split("\n")[-1])


# ---- Compare lines follow the open tab, also between tabs with the same symbol, timeframe and session -------------

TABS_JS = """
const sent = [], removed = [];
let mode = 0, nextSeries = 0;
const PSM = { Normal: 0, Logarithmic: 1, Percentage: 2 }, L = { LineSeries: 'line' }, TK_MULTI = false;
const chart = { addSeries: (type, o) => ({ n: ++nextSeries, title: o.title }), removeSeries: (s) => removed.push(s.title) };
const PS = () => ({ options: () => ({ mode }), applyOptions: (o) => { if ('mode' in o) mode = o.mode; }, setAutoScale() {} });
const store = {}, local = { get: (k, d) => store[k] ?? d, set: (k, v) => { store[k] = v; } };
const S = { symbol: 'ES', spec: '5m', rth: false, replay: null, bars: [{ t: 1, seq: 1 }], views: {}, viewWant: null, barsOff: false, lock: null,
  tabs: [{ id: 'a', sym: 'ES', spec: '5m', look: { scale: 'pct' }, cmp: ['NQ'], cmpMode: '' }, { id: 'b', sym: 'ES', spec: '5m', look: { scale: '' } }], tab: 0 };
const send = (m) => sent.push(m), noop = () => {};
const [viewSave, saveTabs, renderTabs, applyStudyState, applyCs, renderFpMenu, fpSettingsChanged, renderAmtDash, renderRth, psUi, cmpLegend, cmpExt,
  amtFollow, tabSync, toast, setSymbol, setSpec, subscribe] = Array(18).fill(noop);
const ownTab = () => null, sharedStudies = () => ({}), viewApply = () => true, isTime = () => true, isExtSym = () => false, chartSym = (s) => s;
const drawings = { select: noop, update: noop };
/*APP*/
S.fpSet = fpFrom({});
const lines = () => ({ lines: cmpLines.map((c) => c.sym), mode, subs: sent.filter((m) => m.op === 'sub').map((m) => m.symbol) });
cmpLoad();   // tab A's history: its NQ line
const out = { a: lines() };
openTab(1);
out.b = lines();
out.removed = removed.slice();
openTab(0);
out.back = lines();
S.tabs[1].cmp = ['CL'];   // a tab of its own compare: its line, not the other tab's
openTab(1);
out.own = lines();
console.log(JSON.stringify(out));
"""


@needs_node
def test_switching_tabs_on_the_same_bars_swaps_the_compare_lines():
    out = run_app(["cmpLines", "CMP_COLORS", "CMP_MAX", "cmpList", "cmpClear", "cmpLoad", "fpFrom", "setScaleMode", "lookStore",
                   "applyLook", "openTab"], TABS_JS)
    assert out["a"] == {"lines": ["NQ"], "mode": 2, "subs": ["NQ"]}
    assert out["b"]["lines"] == [] and out["b"]["mode"] == 0 and out["removed"] == ["NQ"]   # B has none: Normal, no NQ
    assert out["back"]["lines"] == ["NQ"] and out["back"]["mode"] == 2 and out["back"]["subs"] == ["NQ", "NQ"]
    assert out["own"]["lines"] == ["CL"] and out["own"]["mode"] == 2


# ---- the footprint and the visible-range profile on an RTH chart: its regular hours' trades only --------------------

def ct_ms(*a):
    from datetime import datetime
    from chartsrv.sessions import CT
    return int(datetime(*a, tzinfo=CT).timestamp() * 1000)


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """The app with no feed, on a database of its own (a new one each time) holding these trades."""
    from fastapi.testclient import TestClient

    from chartsrv.app import app
    from chartsrv.store import Store
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    made = []

    def start(ticks):
        made.append(tmp_path / f"chart{len(made)}.db")
        monkeypatch.setenv("CHART_DB", str(made[-1]))
        Store(made[-1]).write(ticks, [])
        return TestClient(app)
    return start


def test_rth_footprint_and_profile_leave_out_the_overnight(app_client):
    from chartsrv.models import Tick
    last_rth, first_rth = ct_ms(2026, 10, 20, 14, 56), ct_ms(2026, 10, 21, 8, 31)   # a Tuesday's last bar, Wednesday's first
    night = [Tick("ES", ct_ms(2026, 10, 20, 15, 30) + h * 3_600_000, 5980.0, 100, -1) for h in range(15)]   # to 05:30
    ticks = [Tick("ES", last_rth, 6000.0, 1, 1), *night, Tick("ES", first_rth, 5990.0, 2, 1)]
    starts, end = [ct_ms(2026, 10, 20, 14, 55), ct_ms(2026, 10, 21, 8, 30)], ct_ms(2026, 10, 21, 8, 35)   # RTH 5m bars, as sent
    with app_client(ticks) as c:
        rth = c.post("/api/footprint", json={"symbol": "ES", "starts": starts, "end": end, "rth": True}).json()["bars"]
        assert rth == [{"t": starts[0], "rows": [[24000, 0, 1]]}, {"t": starts[1], "rows": [[23960, 0, 2]]}]
        eth = c.post("/api/footprint", json={"symbol": "ES", "starts": starts, "end": end}).json()["bars"]
        assert [24000, 0, 1] in eth[0]["rows"] and [23920, 1500, 0] in eth[0]["rows"]   # all hours: the night is the bar's
        p = c.get("/api/profile", params={"symbol": "ES", "start": starts[0], "end": end, "rth": True}).json()
        assert p["total"] == 3 and p["p0"] == 5990.0
        assert c.get("/api/profile", params={"symbol": "ES", "start": starts[0], "end": end}).json()["total"] == 1503


FP_JS = """
const bodies = [], gets = [];
const S = { st: { fp: 'bidask', right: 'visible' }, ext: null, replay: null, symbol: 'ES', spec: '5m', rth: true, fp: new Map(), bars: [] };
let range = { from: 0, to: 3 };
const chart = { timeScale: () => ({ getVisibleLogicalRange: () => range, options: () => ({ barSpacing: 50 }) }) };
const FP_MIN_SPACING = 40, fpSchedule = () => {}, fpPrim = { update() {} }, rangePrim = { update() {} }, isTime = () => !/[tvrk]$/.test(S.spec);
const api = async (method, path, body) => {
  if (method === 'GET') { gets.push(path); return {}; }
  bodies.push(body);
  return { bars: body.starts.map((t, k) => ({ t, k, rows: [[t, 1, 1]] })) };   // k: its place in the request
};
/*APP*/
"""


@needs_node
def test_the_chart_asks_for_its_session_footprint_and_profile():
    out = run_app(["fpBusy", "fpFetch", "rangeTimer", "studyView", "fetchRange"], FP_JS + """
S.bars = [1, 2, 3, 4].map((k) => ({ t: Date.now() - (5 - k) * 300000, seq: k, v: 1 }));
(async () => {
  await fpFetch();
  await fetchRange();
  console.log(JSON.stringify({ rth: bodies[0].rth, profile: gets[0] }));
})();""")
    assert out["rth"] is True and out["profile"].endswith("&rth=true")


# ---- the footprint of bars that open at the same time (one print or one millisecond finishing several) -------------

def test_footprint_of_bars_sharing_an_open_time(app_client):
    from chartsrv.bars import make_series, parse_spec
    from chartsrv.models import Tick
    t0, t1, t2 = (ct_ms(2026, 10, 20, 9, 0, s) for s in (0, 1, 2))
    cases = {
        "500v": [Tick("ES", t0, 6000.0, 300, 1), Tick("ES", t1, 6000.25, 900, 1), Tick("ES", t1, 6000.5, 50, -1),
                 Tick("ES", t2, 6000.0, 700, -1), Tick("ES", t2 + 1, 6000.25, 10, 1)],   # 500, 500, 500 (two at t1), 460
        "4r": [Tick("ES", t0, 6000.0, 1, 1), Tick("ES", t1, 6003.0, 2, 1), Tick("ES", t2, 6003.25, 1, -1)],   # a 0-volume bar between
        "100t": [Tick("ES", t0, 6000.0, 1, 1)] + [Tick("ES", t1, 6000.25 + 0.25 * (k % 3), 1, 1 - 2 * (k % 2)) for k in range(250)],
    }
    for spec, ticks in cases.items():
        s = make_series(parse_spec(spec), 0.25, 2, 5000, ("08:30", "15:00"))
        for tk in ticks:
            s.on_tick(tk)
        bars = list(s.bars)
        assert len({b.t for b in bars}) < len(bars), spec   # the case: two bars open at one time
        with app_client(ticks) as c:
            r = c.post("/api/footprint", json={"symbol": "ES", "starts": [b.t for b in bars], "vols": [b.v for b in bars],
                                               "end": ticks[-1].ts + 1})
            assert r.status_code == 200, (spec, r.text)
            got = r.json()["bars"]
        assert [sum(bid + ask for _, bid, ask in f["rows"]) for f in got] == [b.v for b in bars], spec
        assert [sum(ask - bid for _, bid, ask in f["rows"]) for f in got] == [b.bv - b.sv for b in bars], spec


@needs_node
def test_the_chart_keeps_a_footprint_per_bar_also_when_bars_share_a_time():
    out = run_app(["fpBusy", "fpFetch", "rangeTimer", "studyView", "fetchRange"], FP_JS + """
const t = Date.now() - 60000;
(async () => {
  S.spec = '500v';
  S.bars = [{ t, seq: 1, v: 500 }, { t: t + 1000, seq: 2, v: 500 }, { t: t + 1000, seq: 3, v: 500 }, { t: t + 2000, seq: 4, v: 460 }];
  await fpFetch();
  const tick = { vols: bodies[0].vols, keys: [...S.fp.keys()], third: S.fp.get(3).t - t };
  S.spec = '5m'; S.fp.clear();
  S.bars = [{ t, seq: 7, v: 3 }, { t: t + 300000, seq: 8, v: 4 }];
  await fpFetch();
  console.log(JSON.stringify({ tick, time: { vols: bodies[1].vols ?? null, keys: [...S.fp.keys()] } }));
})();""")
    assert out["tick"] == {"vols": [500, 500, 500, 460], "keys": [1, 2, 3, 4], "third": 1000}   # bar 3: its own, at bar 2's time
    assert out["time"] == {"vols": None, "keys": [7, 8]}   # time bars: each trade's time says its bar


@needs_node
def test_a_footprint_window_ending_where_bars_share_a_millisecond(app_client):
    """The last bar the chart asks for can end in the millisecond the next one opens in (one print finishing it), or
    open in it too. The request ended at the next bar's open: refused (not after the last start), or the bar came up
    short of its trades in that millisecond."""
    from chartsrv.bars import make_series, parse_spec
    from chartsrv.models import Tick
    t = [ct_ms(2026, 10, 20, 9, 0, s) for s in range(7)]
    ticks = [Tick("ES", t[0], 6000.0, 300, 1), Tick("ES", t[1], 6000.25, 900, 1), Tick("ES", t[1], 6000.5, 50, -1),
             Tick("ES", t[2], 6000.0, 700, -1), Tick("ES", t[2] + 1, 6000.25, 10, 1), Tick("ES", t[3], 6000.5, 540, 1),
             Tick("ES", t[4], 6000.25, 100, -1), Tick("ES", t[5], 6000.0, 400, 1), Tick("ES", t[6], 6000.25, 1, 1)]
    s = make_series(parse_spec("500v"), 0.25, 2, 5000, ("08:30", "15:00"))
    for tk in ticks:
        s.on_tick(tk)
    bars = list(s.bars)   # two open at t1; the ones at t2 and t3 in the millisecond the bar before ends in
    assert [(b.t - t[0], b.v) for b in bars] == [(0, 500), (1000, 500), (1000, 500), (2000, 500), (3000, 500), (4000, 500), (6000, 1)]
    out = run_app(["fpBusy", "fpFetch"], FP_JS + f"""
const B = {json.dumps([{"t": b.t, "seq": b.seq, "v": b.v} for b in bars])}, out = [];
(async () => {{
  S.spec = '500v'; S.rth = false;
  for (const [to, cached] of [[0, []], [1, []], [2, []], [3, []], [2, [3, 4]]]) {{   // in view: bars 0 to to + 1
    S.bars = B.slice(); S.fp = new Map(cached.map((seq) => [seq, {{}}])); range = {{ from: 1, to }}; bodies.length = 0;
    await fpFetch();
    out.push({{ body: bodies[0], kept: [...S.fp].filter(([seq]) => !cached.includes(seq)).map(([seq, f]) => [seq, f.k]) }});
  }}
  console.log(JSON.stringify(out));
}})();""")
    assert [[seq for seq, _ in o["kept"]] for o in out] == [[1, 2], [1, 2, 3], [1, 2, 3, 4], [1, 2, 3, 4, 5], [1, 2]]
    with app_client(ticks) as c:
        for o in out:
            r = c.post("/api/footprint", json=o["body"])
            assert r.status_code == 200, (o["body"], r.text)
            got = r.json()["bars"]
            for seq, k in o["kept"]:
                b, rows = bars[seq - 1], got[k]["rows"]
                assert (sum(bid + ask for _, bid, ask in rows), sum(ask - bid for _, bid, ask in rows)) == (b.v, b.bv - b.sv), (o, seq)


@needs_node
def test_a_message_closing_several_bars_drops_each_ones_footprint():
    """One print can close the forming bar and the next in one message: the first kept the part it had while forming."""
    out = run_app(["applyBars"], """
let asked = 0;
const S = { st: { fp: 'bidask' }, cs: {}, bars: [{ t: 1, seq: 1 }, { t: 2, seq: 2 }], fp: new Map([[1, 'whole'], [2, 'part']]) };
const toPoints = () => [{}, {}], candles = { update() {} }, vol = { update() {} }, altSeries = null, BAR_CAP = 20000, anchorMs = () => null;
const fpSchedule = () => { asked += 1; };
const [updateStudyBar, indsUpdate, dsmaTick, hemaTick, cmpTail, legend, header] = Array(7).fill(() => {});
/*APP*/
applyBars({ bars: [{ t: 2, seq: 2 }, { t: 3, seq: 3 }, { t: 3, seq: 4 }] });   // bar 2 finished, 3 too, 4 forming
console.log(JSON.stringify({ kept: [...S.fp.keys()], asked }));""")
    assert out == {"kept": [1], "asked": 1}


# The frames of a layout share the page's storage; each runs layout.js as index.html loads it.
STORAGE_JS = """
const vm = require('vm'), fs = require('fs'), store = new Map();
const localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k), key: (i) => [...store.keys()][i] ?? null, get length() { return store.size; } };
const layout = fs.readFileSync(LAYOUT, 'utf8');"""


# ---- each chart of a layout keeps its own tab views (one device-wide key held them all) ---------------------------

FRAMES_JS = STORAGE_JS + """
store.set('layout', '2');
function frame(cell, tab) {   // a chart of the layout as its frame (index.html?cell=N) loads: the tab views from storage
  const ctx = vm.createContext({ localStorage, location: { search: '?cell=' + cell }, window: { innerWidth: 1600 }, clearTimeout, setTimeout });
  vm.runInContext(layout, ctx);
  vm.runInContext("let workspaces = null; const S = { tabs: [{ id: '" + tab + "' }], tab: 0, replay: null }; " + VIEWS, ctx);
  return {
    save: (sym) => vm.runInContext("viewSnap = () => ({ key: '" + sym + " 5m false', w: 120, ro: 5, auto: true }); viewSave(true);", ctx),
    view: () => vm.runInContext("S.views['" + tab + "']?.key ?? null", ctx),
    inWorkspace: (k) => vm.runInContext('tkInWorkspace(' + JSON.stringify(k) + ')', ctx),
  };
}
const out = {};
let a = frame(0, 'tA'), b = frame(1, 'tA');   // chart 2 started as a copy of chart 1: the same tab id
a.save('ES'); b.save('NQ');
out.same = [frame(0, 'tA').view(), frame(1, 'tA').view()];   // the app opens again
a = frame(0, 'tB'); b = frame(1, 'tC');   // tabs of their own: each frame writes back the map it read at start
a.save('ES'); b.save('CL');
out.own = [frame(0, 'tB').view(), frame(1, 'tC').view()];
out.workspace = [...store.keys()].filter((k) => k.includes('tabViews') && a.inWorkspace(k));
console.log(JSON.stringify(out));
"""


@needs_node
def test_each_chart_of_a_layout_keeps_its_own_tab_views():
    views = app_part("local") + "\n" + app_block("const VIEWS_MAX", "viewSave")
    js = f"const LAYOUT = {json.dumps(str(WEB / 'layout.js'))}, VIEWS = {json.dumps(views)};" + FRAMES_JS
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["same"] == ["ES 5m false", "NQ 5m false"]
    assert out["own"] == ["ES 5m false", "CL 5m false"]
    assert out["workspace"] == []   # still this device's only


# ---- a chart of a layout seeded from the first one gets tab ids of its own ------------------------------------------

TAB_IDS_JS = STORAGE_JS + """
store.set('layout', '4');
function frame(cell) {   // the tabs a chart of the layout opens with (index.html?cell=N)
  const ctx = vm.createContext({ localStorage, location: { search: '?cell=' + cell }, window: { innerWidth: 1600 } });
  vm.runInContext(layout, ctx);
  vm.runInContext("let workspaces = null; const S = { symbol: 'ES', spec: '5m' }; " + TABS, ctx);
  return vm.runInContext('({ ids: S.tabs.map((t) => t.id), fresh: tabsNew })', ctx);
}
const tabs = (...ids) => JSON.stringify(ids.map((id) => ({ id, sym: 'ES', spec: '5m', link: true })));
store.set('chartTabs', tabs('tA', 'tB'));
store.set('c1.chartTabs', tabs('tA', 'tB'));   // chart 2 seeded from chart 1, as a layout of several charts starts
store.set('c2.chartTabs', tabs('tX'));
store.set('c3.chartTabs', tabs('tX', 'tY'));   // charts 3 and 4: one id alike, not the first chart's
console.log(JSON.stringify([0, 1, 2, 3].map(frame)));
"""


@needs_node
def test_a_chart_of_a_layout_gets_tab_ids_of_its_own():
    tabs = "\n".join([app_part("local"), app_part("SPEC_RE"), app_lines("const TABS_MAX", "S.tab = ")])
    js = f"const LAYOUT = {json.dumps(str(WEB / 'layout.js'))}, TABS = {json.dumps(tabs)};" + TAB_IDS_JS
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    first, second, third, fourth = json.loads(r.stdout)
    assert first == {"ids": ["tA", "tB"], "fresh": False}   # the first chart keeps its own (and the drawings made for them)
    assert second["fresh"] and len(set(second["ids"])) == 2 and not set(second["ids"]) & {"tA", "tB"}
    assert third == {"ids": ["tX"], "fresh": False}
    assert fourth["fresh"] and fourth["ids"][1] == "tY" and fourth["ids"][0] not in ("tX", "tA", "tB")


# ---- the AMT Toolkit started on no bars: its first ready message brings its style and past profiles ------------------

def test_amt_first_ready_message_after_no_bars_has_the_style():
    from datetime import datetime
    from types import SimpleNamespace

    from chartsrv import amt
    from chartsrv.amt_hub import AmtEntry, AmtHub
    from chartsrv.amt_runner import AmtRunner
    from chartsrv.market import Client
    from amt_parity import synth
    hub = AmtHub(SimpleNamespace(store=SimpleNamespace(get_setting=lambda k, d=None: d, set_setting=lambda k, v: None), instruments={}))
    e, client = AmtEntry(AmtRunner("ES", 0.25, 5, amt.Settings())), Client()
    first = hub._message(e, client, 1, full=True)   # a symbol with no 1-minute bars yet
    assert first["data"] == {"ready": False}
    for m in list(synth(1, 5, datetime(2026, 10, 20, 10, 0)))[:120]:   # the first minutes come in live
        e.runner.add_minute(m)
    e.runner.commit_ready()
    e.runner.preview()
    d = hub._message(e, client, 1, full=False)["data"]
    assert d["ready"] and d["style"]["price_tags"] is True and d["past"] == [] and "history" in d and "samples" in d
    assert "style" not in hub._message(e, client, 1, full=False)["data"]   # once sent, not again


# ---- Daily SMA x4 on a chart whose history came empty: asked for once its first bars come ----------------------------

@needs_node
def test_daily_sma_is_asked_for_when_the_first_bars_come():
    out = run_app(["dsmaBusy", "dsmaKey", "dsmaRequest", "dsmaTick"], """
const asked = [];
const S = { st: { dsma: true }, bars: [], symbol: 'ES', spec: '5m', rth: false, replay: null, instruments: { ES: {} }, dsma: null,
  dsmaSet: { SMA1Length: 8 }, dsmaLive: new Map(), dsmaVals: [] };
const indReady = () => true, dsmaDraw = () => {}, dsmaAppend = () => {}, dsmaLive = () => {}, dsmaPeriods = () => [8, 20, 50, 200];
const api = async (method, path) => { asked.push(path); return { days: [] }; };
/*APP*/
(async () => {
  await dsmaRequest();   // the history: no bars yet
  const before = asked.length;
  S.bars.push({ t: Date.now(), c: 1 });
  dsmaTick(false);   // the first live bar
  await new Promise((r) => setTimeout(r, 10));
  dsmaTick(false);
  await new Promise((r) => setTimeout(r, 10));
  console.log(JSON.stringify({ before, after: asked.length, drawn: !!S.dsma }));
})();""")
    assert out == {"before": 0, "after": 1, "drawn": True}


# ---- a free-data chart's indicators take its decimals once its bars say them --------------------------------------

@needs_node
def test_free_data_indicators_take_the_sources_decimals():
    js = """
const made = [], toasts = [];
const S = { symbol: 'EURUSD=X', spec: '5m', subId: 1, rth: false, instruments: {}, ext: { symbol: 'EURUSD=X' }, bars: [], inds: [],
  indCfg: [{ type: 'atr' }], layersReady: false, cs: { volume: true } };
const chart = { removeSeries() {} }, IND_COLORS = { atr: ['#fff'] }, vol = { applyOptions() {} }, local = { set() {} };
const makeInd = (c) => { made.push(inst().decimals); return { plots: [] }; };
const [cotRequest, indsUpdate, priceOnlyLayout, layerSeries, renderRth, tabSync, renderTf, extMerge, applyPriceFormat, barsLayout,
  renderSymBtn, applyHistory, loadNote, barsClear] = Array(14).fill(() => {});
const EXT_WAIT_MS = 60e3, EXT_RETRY_MS = 15e3, viewKey = () => '', viewSnap = () => null;
const toast = (t) => toasts.push(t);
const extSeries = () => false, extEvery = () => 1e9, $ = () => ({});
const api = async () => ({ source: 'yahoo', name: 'EUR/USD', decimals: 5, tick: 0.00001, spec: '5m', bars: [{ t: 1, o: 1.08, h: 1.08, l: 1.08, c: 1.08 }] });
/*INST*/
/*APP*/
(async () => {
  indsBuild();   // a new symbol: before its bars (and decimals) have come
  await extLoad();
  const first = made.slice();
  await extLoad();   // the same source again (another timeframe): nothing to make again
  console.log(JSON.stringify({ first, again: made.length - first.length, toasts }));
  process.exit(0);
})();"""
    out = run_app(["extTimer", "indsBuild", "extLoad"], js.replace("/*INST*/", app_block("const inst = ", "px")))
    assert out == {"first": [2, 5], "again": 0, "toasts": []}
