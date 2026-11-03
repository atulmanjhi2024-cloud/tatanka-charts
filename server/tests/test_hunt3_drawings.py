"""Drawings in the browser (web/drawings.js, run in node with a stand-in page, chart and server): a line alert deleted
elsewhere while a typed change waits or the line is dragged, a typed change still waiting when the lock, the object
tree's eye or share, or undo is pressed, a copy of a pasted level, hiding the selected drawing, and changes broadcast
while the list is loading."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"
T0 = 1_792_000_000_000

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

# drawings.js with a page of stand-in elements, a chart whose bar i is at x = 100 + 10 i and price p at
# y = 200 - 4 (p - 7000), and a server kept in maps, one per symbol, that keeps each line's alert in step (as
# app.py's _sync_drawing_alert) and broadcasts every change back to the page. Its GET reads the list when asked and
# answers getDelay ms later (the network). press(act) is a button in the Draw bar,
# typed(k, value, type) an edit in the settings panel, pointer(type, x, y) the mouse, key(props) a key press,
# row(id) the drawing's row in the object tree, arcs() the selection handles drawn.
HARNESS = r"""
const vm = require('vm'), fs = require('fs');
function el() {
  const ls = {}, kids = new Map();
  const e = { style: {}, dataset: {}, hidden: false, textContent: '', value: '', checked: false, children: [],
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    addEventListener(t, f) { (ls[t] ||= []).push(f); },
    fire(t, ev) { return Promise.all((ls[t] || []).map((f) => f(ev))); },
    querySelector(s) { if (!kids.has(s)) kids.set(s, el()); return kids.get(s); },
    querySelectorAll: () => [], replaceChildren(...c) { e.children = c; }, append(...c) { e.children.push(...c); },
    contains: () => false, closest() { return e; }, select() {}, click() { e.onclick?.(); }, clientWidth: 800,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 400 }) };
  return e;
}
const docL = {}, winL = {};
const ctx = { localStorage: { getItem: () => null, setItem() {} }, getSelection: () => '',
  window: { addEventListener(t, f) { (winL[t] ||= []).push(f); }, innerWidth: 1200 },
  document: { addEventListener(t, f) { (docL[t] ||= []).push(f); }, createElement: el, activeElement: null },
  Option: function (text, value) { this.text = text; this.value = value; },
  setTimeout, clearTimeout, structuredClone, console, requestAnimationFrame: (f) => setTimeout(f, 0) };
vm.runInNewContext(['press.js', 'studies.js', 'drawings.js'].map((f) => fs.readFileSync(WEB + '/' + f, 'utf8')).join(';\n') +
  ';this.create=createDrawings;', ctx);
const J = (x) => JSON.parse(JSON.stringify(x));
const server = {}, alerts = [];
const table = (s) => (server[s] ||= new Map());
let sym = 'ES', alertSeq = 0, getDelay = 1, D, prim;
const tell = (m) => setTimeout(() => D.onMessage(J(m)), 0);
async function api(method, path, body) {
  const [, s, id] = /^\/api\/drawings\/([^/]+)(?:\/(.+))?$/.exec(path), sy = decodeURIComponent(s), m = table(sy);
  if (method === 'GET') { const list = [...m.values()].map(J); await new Promise((r) => setTimeout(r, getDelay)); return { drawings: list }; }
  await new Promise((r) => setTimeout(r, 1));
  if (method === 'PUT') {
    const d = J({ ...body, id, alert: { on: !!body.alert?.on, direction: body.alert?.direction || 'cross', repeat: !!body.alert?.repeat } });
    m.set(id, d);
    const linked = alerts.filter((a) => a.symbol === sy && a.drawing === id);
    if (d.alert.on && !linked.length) alerts.push({ id: ++alertSeq, symbol: sy, drawing: id });
    if (!d.alert.on) for (const a of linked) alerts.splice(alerts.indexOf(a), 1);
    tell({ type: 'drawing', symbol: sy, drawing: d });
    return J(d);
  }
  if (method === 'DELETE') {
    if (!m.delete(id)) throw new Error('no such drawing');
    for (const a of alerts.filter((x) => x.symbol === sy && x.drawing === id)) alerts.splice(alerts.indexOf(a), 1);
    tell({ type: 'drawing_deleted', symbol: sy, ids: [id] });
    return {};
  }
}
function alertDeletedElsewhere(drawingId) {   // app.py delete_alert, from the Alerts tab of another device
  const a = alerts.find((x) => x.drawing === drawingId), d = table(a.symbol).get(drawingId);
  alerts.splice(alerts.indexOf(a), 1);
  const off = { ...d, alert: { ...d.alert, on: false } };
  table(a.symbol).set(drawingId, off);
  tell({ type: 'drawing', symbol: a.symbol, drawing: off, alert_deleted: true });
}
const drawn = [];
const canvas = new Proxy({ arc: (...a) => drawn.push(a), measureText: () => ({ width: 10 }) }, { get: (o, k) => (k in o ? o[k] : () => {}), set: () => true });
const ui = { bar: el(), edit: el(), paste: el(), tree: Object.assign(el(), { hidden: true }) };
const chartEl = el();
const timeScale = { logicalToCoordinate: (l) => 100 + l * 10, coordinateToLogical: (x) => (x - 100) / 10, options: () => ({ barSpacing: 10 }),
  getVisibleLogicalRange: () => ({ from: -10, to: 10 }), setVisibleLogicalRange() {} };
const bars = [{ t: T0 - 60000, o: 7000, h: 7000, l: 7000, c: 7000 }, { t: T0, o: 7000, h: 7000, l: 7000, c: 7000 }];
D = ctx.create({ chart: { timeScale: () => timeScale, priceScale: () => ({ width: () => 0 }), options: () => ({}), applyOptions() {}, paneSize: () => ({ height: 400 }) },
  series: { attachPrimitive(p) { prim = p; p.attached({ requestUpdate() {} }); }, priceToCoordinate: (p) => 200 - (p - 7000) * 4,
    coordinateToPrice: (y) => 7000 + (200 - y) / 4, priceScale: () => ({}) },
  el: chartEl, bars: () => bars, periodMs: () => 60000, tick: () => 0.25, fmt: String, symbol: () => sym, menu() {}, api, toast() {},
  ask: async () => '', alerts: () => alerts.map((a) => ({ kind: 'line', enabled: true, params: { drawing: a.drawing } })), ui });
const ticks = async (n = 8) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 2)); };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const press = async (act) => { const b = el(); b.dataset.act = act; await ui.bar.fire('click', { target: b }); await ticks(); };
const typed = (k, value, type = 'text') => { const inp = el(); inp.dataset.k = k; Object.assign(inp, { value, type }); ui.edit.fire('input', { target: inp }); };
const hline = (id, p, extra = {}) => ({ id, type: 'hline', pts: [{ t: T0, p }], style: { color: '#2962ff', text: '' }, alert: { on: false, direction: 'cross', repeat: false }, ...extra });
const open = async (s) => { sym = s; await D.load(); await ticks(); };
const key = (props) => (docL.keydown || []).forEach((f) => f({ target: { closest: () => null }, preventDefault() {}, stopImmediatePropagation() {}, ...props }));
const pointer = (t, x, y) => { const e = { button: 0, pointerType: 'mouse', clientX: x, clientY: y, preventDefault() {}, shiftKey: false };
  if (t === 'pointerdown') return chartEl.fire(t, e); (winL[t] || []).forEach((f) => f(e)); };
const row = (id) => ui.tree.children.find((r) => r.dataset?.id === id);
const arcs = () => { drawn.length = 0; prim.paneViews()[0].renderer().draw({ useMediaCoordinateSpace: (f) => f({ context: canvas, mediaSize: { width: 800, height: 400 } }) }); return drawn.length; };
const pasteLevels = async (text) => {
  const box = ui.paste;
  box.querySelector('textarea').value = text;
  for (const k of ['top', 'bottom']) box.querySelector(`[data-k=${k}]`).value = '';
  box.querySelector('[data-k=replace]').checked = true;
  await box.querySelector('[data-act=add]').onclick(); await ticks();
};
"""


def run(scenario: str) -> dict:
    js = f"const WEB = {json.dumps(str(WEB))}, T0 = {T0};\n" + HARNESS + "\n(async () => {\n" + scenario + "\n})();"
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def test_a_line_alert_deleted_elsewhere_stays_deleted_after_a_typed_label_or_a_drag():
    """A line's alert deleted in another device's Alerts tab while a label typed here waits for its save, or while
    the line is being dragged here: the label's save and the drop keep the alert off (no new armed alert), and so
    do their undo steps."""
    out = run(r"""
const on = { on: true, direction: 'cross', repeat: false };
table('ES').set('a', hline('a', 7000, { alert: on }));
table('ES').set('b', hline('b', 7010, { alert: on }));
alerts.push({ id: ++alertSeq, symbol: 'ES', drawing: 'a' }, { id: ++alertSeq, symbol: 'ES', drawing: 'b' });
await open('ES');
D.select('a');
typed('text', 'VAH');
await wait(100);
alertDeletedElsewhere('a'); await ticks();
await wait(500); await ticks();                        // the label's save
const a = table('ES').get('a');
const label = { alerts: alerts.map((x) => x.drawing), on: a.alert.on, text: a.style.text, here: D.state.list.get('a').alert.on,
  undoBefore: D.state.undo.at(-1).before.alert.on };
D.select(null);
const y = 200 - (7010 - 7000) * 4;                     // the line b
pointer('pointerdown', 300, y); await ticks();
pointer('pointermove', 300, y - 20); await ticks();
alertDeletedElsewhere('b'); await ticks();
pointer('pointerup', 300, y - 20); await ticks();
const b = table('ES').get('b');
console.log(JSON.stringify({ label, drop: { alerts: alerts.map((x) => x.drawing), on: b.alert.on, p: b.pts[0].p,
  undoBefore: D.state.undo.at(-1).before.alert.on } }));""")
    assert out["label"] == {"alerts": ["b"], "on": False, "text": "VAH", "here": False, "undoBefore": False}
    assert out["drop"] == {"alerts": [], "on": False, "p": 7015, "undoBefore": False}


def test_a_typed_change_still_waiting_does_not_undo_a_lock_a_hide_or_an_undo():
    """A label typed, and within its 400 ms save delay the lock, the object tree's eye or the undo button: the label
    is saved first, as its own undo step, and does not then put the drawing back unlocked, shown or as it was. The
    tree's share still shows another tab's drawing on every tab."""
    out = run(r"""
for (const [id, p] of [['a', 7000], ['b', 7100], ['u', 7400]]) table('ES').set(id, hline(id, p));
table('ES').set('c', hline('c', 7200, { style: { color: '#2962ff', text: '', tab: 'other' } }));   // another tab's
await open('ES');
D.showTree(true); await ticks();
D.select('a'); typed('text', 'VAH'); await wait(100);
ui.edit.querySelector('[data-act=lock]').onclick(); await ticks();
await wait(500); await ticks();
const locked = { server: table('ES').get('a').style, here: D.state.list.get('a').style };
await press('undo');
const unlocked = table('ES').get('a').style;
D.select('b'); typed('text', 'POC'); await wait(100);
row('b').children[3].onclick({ stopPropagation() {} }); await ticks();
await wait(500); await ticks();
const hidden = table('ES').get('b').style;
row('c').children[3].onclick({ stopPropagation() {} }); await ticks();   // share it here
const shared = table('ES').get('c').style;
D.select('u'); typed('pt0', '7410', 'number'); await wait(600); await ticks();
typed('text', 'VAL'); await wait(100);
await press('undo');                                   // the label: the latest change
await wait(500); await ticks();
const u = () => [table('ES').get('u').pts[0].p, table('ES').get('u').style.text];
const undone = [u()];
await press('undo'); undone.push(u());
await press('redo'); undone.push(u());
await press('redo'); undone.push(u());
console.log(JSON.stringify({ locked, unlocked, hidden, shared, undone }));""")
    assert out["locked"]["server"] == out["locked"]["here"] == {"color": "#2962ff", "text": "VAH", "locked": True}
    assert out["unlocked"] == {"color": "#2962ff", "text": "VAH"}   # undo takes the lock, then the label
    assert out["hidden"] == {"color": "#2962ff", "text": "POC", "hidden": True}
    assert out["shared"] == {"color": "#2962ff", "text": ""}
    assert out["undone"] == [[7410, ""], [7400, ""], [7410, ""], [7410, "VAL"]]


def test_a_copy_of_a_pasted_level_is_not_replaced_by_the_next_paste():
    """A pasted level cloned, or copied to another symbol with Ctrl+C and Ctrl+V, is the user's own line: the next
    Paste levels (replace) takes only the pasted set."""
    out = run(r"""
await open('ES');
await pasteLevels('7845.75|VAH');
const orig = [...table('ES').keys()][0];
D.select(orig);
ui.edit.querySelector('[data-act=clone]').onclick(); await ticks();
const copy = D.state.selected;
D.select(orig); key({ ctrlKey: true, code: 'KeyC' });
await open('MES'); key({ ctrlKey: true, code: 'KeyV' }); await ticks();
await open('ES');
await pasteLevels('7900|VAH');
const es = [...table('ES').values()].map((d) => [d.id === copy ? 'clone' : d.id === orig ? 'orig' : 'new', d.style.group ?? null]);
await open('MES');
await pasteLevels('7900|VAH');
console.log(JSON.stringify({ es, mes: [...table('MES').values()].map((d) => [d.pts[0].p, d.style.group ?? null]) }));""")
    assert sorted(out["es"]) == [["clone", None], ["new", "paste"]]
    assert sorted(out["mes"]) == [[7845.75, None], [7900, "paste"]]


def test_hiding_the_selected_drawing_deselects_it():
    """The object tree's eye on the selected drawing, or a hide from another device: it is no longer selected, so
    its handles go, its settings bar closes, and a press on where a handle was or the Delete key leaves it alone."""
    out = run(r"""
table('ES').set('t', { id: 't', type: 'trend', pts: [{ t: T0 - 60000, p: 7000 }, { t: T0, p: 7020 }], style: { color: '#2962ff', text: '' }, alert: { on: false } });
table('ES').set('a', hline('a', 7100));
await open('ES');
D.showTree(true); await ticks();
row('t').onclick(); await ticks();
const before = { selected: D.state.selected, handles: arcs() };
row('t').children[3].onclick({ stopPropagation() {} });
const rightAway = D.state.selected;                    // not only once the server's copy is back
await ticks();
const after = { hidden: table('ES').get('t').style.hidden, selected: D.state.selected, bar: ui.edit.hidden, handles: arcs() };
pointer('pointerdown', 110, 120); await ticks();       // where its second point's handle was
const drag = D.state.drag;
pointer('pointerup', 110, 120); await ticks();
key({ key: 'Delete' }); await ticks();
D.select('a');
const off = { ...table('ES').get('a'), style: { ...table('ES').get('a').style, hidden: true } };
table('ES').set('a', off);
D.onMessage({ type: 'drawing', symbol: 'ES', drawing: J(off) });   // hidden on another device
console.log(JSON.stringify({ before, rightAway, after, drag, kept: table('ES').has('t'), remote: D.state.selected }));""")
    assert out["before"] == {"selected": "t", "handles": 2} and out["rightAway"] is None
    assert out["after"] == {"hidden": True, "selected": None, "bar": True, "handles": 0}
    assert out["drag"] is None and out["kept"] is True and out["remote"] is None


def test_changes_broadcast_while_the_list_loads_are_not_undone_by_its_answer():
    """The socket comes back (or the symbol is opened) and the drawings are asked for; before the answer arrives,
    another device deletes a line with an alert and moves another. The answer, read on the server before those
    changes, does not bring the line back or put the other where it was, so a drag here cannot recreate the line
    on the server with its alert armed again."""
    out = run(r"""
table('ES').set('a', hline('a', 7000, { alert: { on: true, direction: 'cross', repeat: false } }));
table('ES').set('b', hline('b', 7100));
alerts.push({ id: ++alertSeq, symbol: 'ES', drawing: 'a' });
await open('ES');
getDelay = 60;
const loading = D.load();                              // the list is read now, answered 60 ms later
await wait(10);
table('ES').delete('a'); alerts.splice(0, alerts.length);
D.onMessage({ type: 'drawing_deleted', symbol: 'ES', ids: ['a'] });
const b = { ...table('ES').get('b'), pts: [{ t: T0, p: 7110 }] };
table('ES').set('b', b);
D.onMessage({ type: 'drawing', symbol: 'ES', drawing: J(b) });
await loading; await ticks();
getDelay = 1;
const here = { a: D.state.list.has('a'), b: D.state.list.get('b').pts[0].p };
pointer('pointerdown', 300, 200); await ticks();       // where line a was drawn
pointer('pointermove', 300, 180); await ticks();
pointer('pointerup', 300, 180); await ticks(20);
console.log(JSON.stringify({ here, server: [...table('ES').keys()], alerts: alerts.map((x) => x.drawing) }));""")
    assert out == {"here": {"a": False, "b": 7110}, "server": ["b"], "alerts": []}
