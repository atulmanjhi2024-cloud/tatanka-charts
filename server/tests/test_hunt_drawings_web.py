"""Drawings in the browser (web/drawings.js, run in node with a stand-in page and server): undo after Delete all and
across symbols, a typed edit when the symbol changes, the point price boxes on tiny ticks, same-ms bars, and prices
at or below zero."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"
T0 = 1_792_000_000_000

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

# drawings.js with a page of stand-in elements and a server kept in maps, one per symbol. `sym` is the open symbol,
# `calls` every request; press(act) is a button in the Draw bar, typed(k, value, type) an edit in the settings panel.
HARNESS = r"""
const vm = require('vm'), fs = require('fs');
function el() {
  const ls = {}, kids = new Map();
  const e = { style: {}, dataset: {}, hidden: false, textContent: '', value: '', children: [],
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    addEventListener(t, f) { (ls[t] ||= []).push(f); },
    fire(t, ev) { return Promise.all((ls[t] || []).map((f) => f(ev))); },
    querySelector(s) { if (!kids.has(s)) kids.set(s, el()); return kids.get(s); },
    querySelectorAll: () => [], replaceChildren(...c) { e.children = c; }, append(...c) { e.children.push(...c); },
    contains: () => false, closest() { return e; }, select() {} };
  return e;
}
const ctx = { localStorage: { getItem: () => null, setItem() {} }, window: { addEventListener() {}, innerWidth: 1200 },
  document: { addEventListener() {}, createElement: el, activeElement: null },
  Option: function (text, value) { this.text = text; this.value = value; },
  setTimeout, clearTimeout, structuredClone, console, requestAnimationFrame: (f) => setTimeout(f, 0) };
vm.runInNewContext(['press.js', 'studies.js', 'drawings.js'].map((f) => fs.readFileSync(WEB + '/' + f, 'utf8')).join(';') +
  ';this.create=createDrawings;', ctx);
const server = {}, calls = [];
const table = (s) => (server[s] ||= new Map());
let sym = 'ES', tick = 0.25, bars = [{ t: T0 }];
async function api(method, path, body) {
  const [, s, id] = /^\/api\/drawings\/([^/]+)(?:\/(.+))?$/.exec(path), m = table(decodeURIComponent(s));
  calls.push(`${method} ${path}`);
  if (method === 'GET') return { drawings: [...m.values()].map((d) => JSON.parse(JSON.stringify(d))) };
  if (method === 'PUT') { const d = JSON.parse(JSON.stringify({ ...body, id })); m.set(id, d); return JSON.parse(JSON.stringify(d)); }
  if (method === 'DELETE') { if (id) m.delete(id); else m.clear(); return {}; }
}
const ui = { bar: el(), edit: el(), paste: el() };
const D = ctx.create({ chart: { timeScale: () => ({}), priceScale: () => ({ width: () => 0 }), options: () => ({}), applyOptions() {} },
  series: { attachPrimitive() {} }, el: el(), bars: () => bars, periodMs: () => 0, tick: () => tick, fmt: String,
  symbol: () => sym, api, toast() {}, ask: async () => '', alerts: () => [], ui });
const ticks = async (n = 5) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };
const press = async (act) => { const b = el(); b.dataset.act = act; await ui.bar.fire('click', { target: b }); await ticks(); };
const typed = (k, value, type = 'text') => { const inp = el(); inp.dataset.k = k; Object.assign(inp, { value, type }); ui.edit.fire('input', { target: inp }); };
const hline = (id, p, text = '') => ({ id, type: 'hline', pts: [{ t: T0, p }], style: { color: '#2962ff', text }, alert: { on: false } });
const open = async (s) => { sym = s; await D.load(); await ticks(); };
"""


def run(scenario: str) -> dict:
    js = f"const WEB = {json.dumps(str(WEB))}, T0 = {T0};\n" + HARNESS + "\n(async () => {\n" + scenario + "\n})();"
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def test_delete_all_comes_back_whole_with_one_undo():
    """More drawings than the 50 undo steps: Delete all, then Ctrl+Z brings every one back (one step), Ctrl+Y takes
    them again."""
    out = run(r"""
for (let i = 0; i < 60; i++) table('ES').set(`h${i}`, hline(`h${i}`, 7000 + i));
await open('ES');
await press('clear');
const cleared = table('ES').size;
await press('undo');
const back = table('ES').size, shown = D.state.list.size;
await press('redo');
const redone = table('ES').size;
await press('undo');
console.log(JSON.stringify({ cleared, back, shown, redone, again: table('ES').size }));""")
    assert out == {"cleared": 0, "back": 60, "shown": 60, "redone": 0, "again": 60}


def test_undo_on_another_symbol_keeps_each_symbols_steps():
    """Steps are per symbol: Ctrl+Z on NQ leaves ES's alone, and back on ES they all still undo."""
    out = run(r"""
for (const id of ['a', 'b', 'c']) table('ES').set(id, hline(id, 7000));
table('NQ').set('n', hline('n', 20000));
await open('ES');
for (const id of ['a', 'b', 'c']) { D.select(id); ui.edit.querySelector('[data-act=delete]').onclick(); await ticks(); }
await open('NQ');
await press('undo'); await press('undo');             // nothing of NQ's to undo yet
const nqAfterUndo = [...table('NQ').keys()];
D.select('n'); ui.edit.querySelector('[data-act=delete]').onclick(); await ticks();
await open('ES');
await press('undo'); await press('undo'); await press('undo');
const es = [...table('ES').keys()].sort();
await open('NQ');
await press('undo');
console.log(JSON.stringify({ nqAfterUndo, es, nq: [...table('NQ').keys()] }));""")
    assert out == {"nqAfterUndo": ["n"], "es": ["a", "b", "c"], "nq": ["n"]}


def test_typed_edit_is_saved_when_the_drawings_reload():
    """A label typed into the settings panel, then within the save delay another symbol opens (or the connection
    comes back): the label is saved, on the symbol it was typed on."""
    out = run(r"""
table('ES').set('h1', hline('h1', 7000));
await open('ES');
D.select('h1');
typed('text', 'VAH');
await open('NQ');                                       // a watchlist click, before the 400 ms save
const switched = { es: table('ES').get('h1').style.text, nq: table('NQ').has('h1'), puts: calls.filter((c) => c.startsWith('PUT')) };
await open('ES');
D.select('h1');
typed('text', 'POC');
await open('ES');                                       // reconnected: the same symbol loads again
const reloaded = table('ES').get('h1').style.text;
await press('undo');
console.log(JSON.stringify({ switched, reloaded, undone: table('ES').get('h1').style.text }));""")
    assert out["switched"] == {"es": "VAH", "nq": False, "puts": ["PUT /api/drawings/ES/h1"]}
    assert out["reloaded"] == "POC" and out["undone"] == "VAH"   # its undo step is ES's


def test_point_price_box_shows_the_instruments_decimals():
    """The exact-price boxes use the tick's decimals, also for ticks JavaScript writes as 5e-7 (6J)."""
    out = run(r"""
const shown = {};
for (const [s, tk, p] of [['6J', 0.0000005, 0.0067005], ['ES', 0.25, 6905.25], ['YM', 1, 44010], ['X', 1e-8, 0.00001234]]) {
  sym = s; tick = tk;
  table(s).set('h1', hline('h1', p));
  await D.load(); await ticks();
  D.select('h1');
  D.state.full = true;
  D.update();
  const box = ui.edit.querySelector('[data-f=pts]'), inp = box.children[0].children[1];
  shown[s] = [inp.value, inp.step];
  D.select(null);
}
console.log(JSON.stringify(shown));""")
    assert out["6J"][0] == "0.0067005" and float(out["6J"][1]) == 0.0000005
    assert out["ES"][0] == "6905.25" and out["YM"][0] == "44010" and out["X"][0] == "0.00001234"


def test_same_ms_bars_round_trip_at_the_next_bars_ms():
    """Range bars 1 to 3 open at T and 4 to 6 at T + 1 ms: a point late in bar 3 (as a body drag leaves it) stays by
    bar 3 rather than reading as the third bar of T + 1 ms."""
    out = run(r"""
const T = T0;
bars = [T - 60000, T, T, T, T + 1, T + 1, T + 1, T + 60000].map((t) => ({ t }));
const at = (l) => D.timeToLogical(D.logicalToTime(l));
const worst = [];
for (let i = 0; i < 7; i++) for (let f = 0; f < 1; f += 0.05) { const l = i + f; worst.push(Math.abs(at(l) - l)); }
console.log(JSON.stringify({ late: at(3.6), early: at(3.3), dup: at(4.4), worst: Math.max(...worst) }));""")
    assert out["late"] == 4 and out["early"] == 3 and abs(out["dup"] - 4.4) < 0.02   # hundredths of a ms near 1.8e12
    assert out["worst"] <= 0.5 + 1e-9


def test_prices_at_or_below_zero_can_be_typed_and_pasted():
    """A spread such as FRED:T10Y2Y goes below zero: its lines take a typed -0.50, and pasted levels and the range box
    take negative prices too."""
    out = run(r"""
sym = 'FRED:T10Y2Y'; tick = 0.01;
table(sym).set('h1', hline('h1', 0.25));
await D.load(); await ticks();
D.select('h1');
typed('pt0', '-0.50', 'number');
const typedP = D.state.list.get('h1').pts[0].p;
typed('pt0', '0', 'number');
const zero = D.state.list.get('h1').pts[0].p;
D.select(null); await ticks();
const levels = D.parseLevels('-0.5|inversion low, 0|flat, -1.25');
const box = ui.paste;
box.querySelector('textarea').value = '';
box.querySelector('[data-k=top]').value = '-0.25';
box.querySelector('[data-k=bottom]').value = '-0.75';
box.querySelector('[data-k=replace]').checked = false;
await box.querySelector('[data-act=add]').onclick(); await ticks();
const rect = [...table(sym).values()].find((d) => d.type === 'rect');
box.querySelector('[data-k=top]').value = '';
box.querySelector('[data-k=bottom]').value = '';
box.querySelector('textarea').value = '1.5';
await box.querySelector('[data-act=add]').onclick(); await ticks();
console.log(JSON.stringify({ typedP, zero, saved: table(sym).get('h1').pts[0].p, levels, rect: rect && rect.pts.map((q) => q.p),
  rects: [...table(sym).values()].filter((d) => d.type === 'rect').length }));""")
    assert out["typedP"] == -0.5 and out["zero"] == 0 and out["saved"] == 0
    assert out["levels"] == {"out": [{"p": -0.5, "text": "inversion low"}, {"p": 0, "text": "flat"}, {"p": -1.25, "text": ""}], "bad": []}
    assert out["rect"] == [-0.25, -0.75] and out["rects"] == 1   # empty top and bottom: no range box


def test_undo_does_not_bring_back_a_line_alert_deleted_in_the_alerts_tab():
    """A line's alert deleted in the Alerts tab (the server switches it off and says so): Ctrl+Z and Ctrl+Y of an
    earlier move put the line back with its alert off, also for steps of a symbol that was not open then. Unticking
    the alert on the line stays its own undo step."""
    out = run(r"""
const on = { on: true, direction: 'cross' };
table('ES').set('h1', { ...hline('h1', 7400), alert: on });
table('NQ').set('n1', { ...hline('n1', 20000), alert: on });
const deleted = (s, id) => {                          // as DELETE /api/alerts/{id} broadcasts it
  const d = { ...table(s).get(id), alert: { ...table(s).get(id).alert, on: false } };
  table(s).set(id, d);
  D.onMessage({ type: 'drawing', symbol: s, drawing: JSON.parse(JSON.stringify(d)), alert_deleted: true });
};
await open('NQ');
D.select('n1'); typed('pt0', '20010', 'number'); await new Promise((r) => setTimeout(r, 600)); await ticks();
await open('ES');
D.select('h1'); typed('pt0', '7410', 'number'); await new Promise((r) => setTimeout(r, 600)); await ticks();
deleted('ES', 'h1');
deleted('NQ', 'n1');                                  // deleted while ES is open
await press('undo');
const undone = table('ES').get('h1');
await press('redo');
const redone = table('ES').get('h1');
await open('NQ');
await press('undo');
const nq = table('NQ').get('n1');
await open('ES');
const tick = async (checked) => { const inp = el(); inp.dataset.k = 'alertOn'; Object.assign(inp, { type: 'checkbox', checked });
  await ui.edit.fire('input', { target: inp }); await ticks(); };
D.select('h1'); await tick(true); await tick(false);  // on again on the line, then off there
D.onMessage({ type: 'drawing', symbol: 'ES', drawing: JSON.parse(JSON.stringify(table('ES').get('h1'))) });   // our own save
await press('undo');
console.log(JSON.stringify({ undone: [undone.pts[0].p, undone.alert.on], redone: [redone.pts[0].p, redone.alert.on],
  nq: [nq.pts[0].p, nq.alert.on], unticked: table('ES').get('h1').alert.on }));""")
    assert out["undone"] == [7400, False] and out["redone"] == [7410, False] and out["nq"] == [20000, False]
    assert out["unticked"] is True   # the line's own untick: Ctrl+Z switches it back on


def test_a_dash_bulleted_paste_is_not_read_as_prices_below_zero():
    """'- 7845.75|VAH' is a list's bullet, not -7845.75: refused as before, and Replace keeps the last pasted set. A
    minus on the digits and spaces inside a number (7 845.75) still read."""
    out = run(r"""
const bulleted = D.parseLevels('- 7845.75|VAH\n- 7830|POC\n- 7810|VAL');
const ok = D.parseLevels('-0.5|low, 7 845.75|VAH, -1.25');
table('ES').set('p1', { ...hline('p1', 7800, 'POC 7800'), style: { color: '#2962ff', text: 'POC 7800', group: 'paste' } });
await open('ES');
const box = ui.paste;
box.querySelector('textarea').value = '- 7845.75|VAH\n- 7830|POC';
box.querySelector('[data-k=top]').value = '';
box.querySelector('[data-k=bottom]').value = '';
box.querySelector('[data-k=replace]').checked = true;
await box.querySelector('[data-act=add]').onclick(); await ticks();
console.log(JSON.stringify({ bulleted, ok, prices: [...table('ES').values()].map((d) => d.pts[0].p) }));""")
    assert out["bulleted"] == {"out": [], "bad": ["- 7845.75|VAH", "- 7830|POC", "- 7810|VAL"]}
    assert out["ok"] == {"out": [{"p": -0.5, "text": "low"}, {"p": 7845.75, "text": "VAH"}, {"p": -1.25, "text": ""}], "bad": []}
    assert out["prices"] == [7800]   # nothing added, nothing replaced
