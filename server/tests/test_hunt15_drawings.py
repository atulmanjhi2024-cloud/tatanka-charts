"""Round 15 of the bug hunt, drawings: a contract roll's jump is no cross for the alerts, the magnet on bars that share
a millisecond, a drag the system cancels, Ctrl+C / Ctrl+V across the charts of a layout, and Paste levels in 32nds."""
import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from chartsrv import drawings
from chartsrv.feeds.ninjatrader import NinjaTraderFeed

from test_alerts_levels import LINE
from test_review_fixes import build_market

WEB = Path(__file__).resolve().parents[2] / "web"
T0 = 1_792_000_000_000

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

# drawings.js in node: frame(sym, opts) is one chart, this same page in its own frame of the page around a layout's
# charts (layout.js): its own drawings.js, chart, Draw bar and keys, its window.top the one page every frame shares.
# Bar i is at x = 10 i, price p at y = 1000 - p. One server keeps the drawings in maps, one per symbol; `calls` is
# every request. down/move/up/cancel(x, y, extra) are the pointer (a mouse unless extra says otherwise), key(code,
# extra) a key press, tool(t) a tool picked in the Draw bar.
HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const SRC = ['press.js', 'studies.js', 'drawings.js'].map((f) => fs.readFileSync(WEB + '/' + f, 'utf8')).join(';\n') +
  ';this.create=createDrawings;this.parse32=parse32;';
function el() {
  const ls = {}, kids = new Map();
  const e = { style: {}, dataset: {}, hidden: false, textContent: '', value: '', checked: false, children: [], clientWidth: 1000,
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    addEventListener(t, f) { (ls[t] ||= []).push(f); }, ls,
    fire(t, ev) { return Promise.all((ls[t] || []).map((f) => f(ev))); },
    querySelector(s) { if (!kids.has(s)) kids.set(s, el()); return kids.get(s); },
    querySelectorAll: () => [], replaceChildren(...c) { e.children = c; }, append(...c) { e.children.push(...c); },
    contains: () => false, closest() { return e; }, select() {}, getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 1000 }) };
  return e;
}
const J = (x) => JSON.parse(JSON.stringify(x));
const server = {}, calls = [];
const table = (s) => (server[s] ||= new Map());
async function api(method, path, body) {
  const [, s, id] = /^\/api\/drawings\/([^/]+)(?:\/(.+))?$/.exec(path), m = table(decodeURIComponent(s));
  calls.push(`${method} ${path}`);
  if (method === 'GET') return { drawings: [...m.values()].map(J) };
  if (method === 'PUT') { const d = J({ ...body, id }); m.set(id, d); return J(d); }
  if (method === 'DELETE') { if (id) m.delete(id); else m.clear(); return {}; }
}
const ticks = async (n = 5) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };
const page = {};   // the page around the charts
function frame(sym, { bars = [], per = 60000, tick = 0.25, top = page } = {}) {
  const wl = {}, dl = {}, toasts = [], ui = { bar: el(), edit: el(), paste: el(), tree: Object.assign(el(), { hidden: true }) };
  const ctx = { localStorage: { getItem: () => null, setItem() {} }, getSelection: () => '',
    window: { addEventListener(t, f) { (wl[t] ||= []).push(f); }, innerWidth: 1200, top },
    document: { addEventListener(t, f) { (dl[t] ||= []).push(f); }, createElement: el, activeElement: null },
    Option: function (text, value) { this.text = text; this.value = value; },
    setTimeout, clearTimeout, structuredClone, console, requestAnimationFrame: (f) => setTimeout(f, 0) };
  vm.runInNewContext(SRC, ctx);
  const chartEl = el();
  const timeScale = { coordinateToLogical: (x) => x / 10, logicalToCoordinate: (l) => l * 10, options: () => ({ barSpacing: 10 }),
    getVisibleLogicalRange: () => ({ from: 0, to: 100 }), setVisibleLogicalRange() {} };
  const D = ctx.create({ chart: { timeScale: () => timeScale, priceScale: () => ({ width: () => 0 }), options: () => ({ handleScroll: true, handleScale: true }),
    applyOptions() {}, paneSize: () => ({ height: 1000 }) },
    series: { attachPrimitive() {}, priceToCoordinate: (p) => 1000 - p, coordinateToPrice: (y) => 1000 - y, priceScale: () => ({ getVisibleRange: () => ({ from: 0, to: 1000 }) }) },
    el: chartEl, bars: () => bars, periodMs: () => per, tick: () => tick, fmt: String, symbol: () => sym, api,
    toast: (t) => toasts.push(t), ask: async () => '', alerts: () => [], ui, menu() {} });
  const ptr = (x, y, extra) => ({ clientX: x, clientY: y, button: 0, pointerType: 'mouse', preventDefault() {}, ...extra });
  return { D, ctx, ui, toasts,
    down: (x, y, extra = {}) => chartEl.ls.pointerdown.forEach((f) => f(ptr(x, y, extra))),
    move: (x, y, extra = {}) => wl.pointermove.forEach((f) => f(ptr(x, y, extra))),
    up: (x, y, extra = {}) => wl.pointerup.forEach((f) => f(ptr(x, y, extra))),
    cancel: () => wl.pointercancel.forEach((f) => f({})),
    key: (code, extra = {}) => dl.keydown.forEach((f) => f({ key: '', code, target: { closest: () => null }, preventDefault() {}, stopImmediatePropagation() {}, ...extra })),
    tool: async (t) => { const b = el(); b.dataset.tool = t; await ui.bar.fire('click', { target: b }); await ticks(); },
    open: async () => { await D.load(); await ticks(); } };
}
const hline = (id, p, extra = {}) => ({ id, type: 'hline', pts: [{ t: T0, p }], style: { color: '#2962ff', text: '' }, alert: { on: false, direction: 'cross', repeat: false }, ...extra });
const minutes = (n, p = 500) => Array.from({ length: n }, (_, i) => ({ t: T0 + i * 60000, o: p, h: p + 2, l: p - 2, c: p + 1, v: 1 }));
"""


def run(scenario: str):
    js = f"const WEB = {json.dumps(str(WEB))}, T0 = {T0};\n" + HARNESS + "\n(async () => {\n" + scenario + "\n})();"
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


# ---- r15-drawings-1: a roll's jump between contracts --------------------------------------------------------------------

def test_a_contract_roll_is_no_cross_for_line_price_or_level_alerts(tmp_path, monkeypatch):
    """ES rolls in the break: the old contract's last trade 7840, the new one's first 7888 (the spread, stored bars are
    not adjusted). A drawn line at 7860, a price alert at 7870 and the daily levels R1 to R3 between were never
    traded through: nothing fires, and an Only-once line alert is still on. Trades of the new contract then cross as
    ever. Before: the first trade was measured from the old contract's last, and every alert in the spread fired."""
    m, _ = build_market(tmp_path, monkeypatch)
    m.daily.set_paste("ES", LINE)   # the daily levels: R1 7850, R2 7862.50, R3 7875
    m.levels_changed()
    fired = []
    m.alerts.on_fire = lambda e: fired.append(e["message"])
    now = int(time.time() * 1000)
    line = drawings.clean({"type": "hline", "pts": [{"t": now - 86_400_000, "p": 7860.0}], "style": {},
                           "alert": {"on": True, "direction": "cross"}}, "lineA")
    m.store.add_alert("ES", "line", drawings.alert_params(line), False, "")
    m.store.add_alert("ES", "price", {"price": 7870.0, "direction": "cross"}, False, "")
    m.store.add_alert("ES", "level", {"levels": ["R1", "R2", "R3"], "tolerance_ticks": 0, "rearm_ticks": 8}, True, "")
    m.alerts.reload()
    m.contracts["ES"] = "ES 12-26"
    feed = NinjaTraderFeed("127.0.0.1", 0, None)

    async def go():
        feed._line(m, f"T|ES|{now - 5000}|7840.00|1|1\n".encode())    # the old contract (Dec)
        feed._line(m, f"T|ES|{now - 4000}|7840.25|1|1\n".encode())
        feed._line(m, b"C|ES|ES 03-27\n")                                # the roll (the add-on's C line)
        feed._line(m, f"T|ES|{now - 3000}|7888.00|1|1\n".encode())    # the new contract (Mar), 48 points up
        feed._line(m, f"T|ES|{now - 2000}|7888.25|1|1\n".encode())
        at_roll = list(fired), [a["enabled"] for a in m.store.list_alerts()]
        feed._line(m, f"T|ES|{now - 1000}|7858.00|1|1\n".encode())    # a real move down through all of them
        await asyncio.sleep(0)
        return at_roll
    at_roll = asyncio.run(go())
    assert m.contracts["ES"] == "ES 03-27"
    assert at_roll == ([], [True, True, True])
    assert sorted(fired) == sorted(["ES crossed below line 7860.00 (last 7858.00)", "ES crossed below 7870.00 (last 7858.00)",
                                    "ES touched R3 7875.00 (last 7858.00)", "ES touched R2 7862.50 (last 7858.00)"])


def test_an_alert_set_on_the_old_contracts_last_price_does_not_fire_on_the_new_ones_second_trade(tmp_path, monkeypatch):
    """A price alert at 7840.25 looked at first with the old contract's last trade exactly on it (its first move off it
    is the cross). After the roll that first look is the new contract's: its second trade, 7888.25, crosses nothing."""
    m, _ = build_market(tmp_path, monkeypatch)
    fired = []
    m.alerts.on_fire = lambda e: fired.append(e["message"])
    now = int(time.time() * 1000)
    m.contracts["ES"] = "ES 12-26"
    feed = NinjaTraderFeed("127.0.0.1", 0, None)

    async def go():
        feed._line(m, f"T|ES|{now - 5000}|7840.25|1|1\n".encode())
        m.store.add_alert("ES", "price", {"price": 7840.25, "direction": "cross"}, False, "")   # set on the last price
        m.alerts.reload()
        feed._line(m, f"T|ES|{now - 4000}|7840.25|1|1\n".encode())   # its first look: on it
        feed._line(m, b"C|ES|ES 03-27\n")
        feed._line(m, f"T|ES|{now - 3000}|7888.00|1|1\n".encode())
        feed._line(m, f"T|ES|{now - 2000}|7888.25|1|1\n".encode())
        await asyncio.sleep(0)
    asyncio.run(go())
    assert fired == []


# ---- r15-drawings-2: the magnet on bars that share a millisecond ----------------------------------------------------------

@needs_node
def test_the_magnet_keeps_a_point_on_the_bar_it_snapped_to_among_bars_that_share_a_millisecond():
    """A 4-tick Renko chart: one fast move opened bars 3 to 7 in the same ms. A trend line started with the magnet
    on bar 6's close is stored on bar 6 (its time plus 3/100 ms, as without the magnet), at that close; bar 2 (a ms of
    its own) on itself. Before: on bar 3, the first of the group, three bricks to the left."""
    out = run(r"""
const bars = [];
for (let i = 0, p = 500; i < 12; i++, p += 2) bars.push({ t: T0 + (i < 3 ? i : i < 8 ? 3 : i - 4) * 1000, o: p, h: p + 2, l: p, c: p + 2, v: 1 });
const F = frame('ES', { bars, per: 0 });
await F.open();
const got = [];
for (const [magnet, bar] of [[false, 6], [true, 6], [true, 7], [true, 3], [true, 2]]) {
  F.D.state.magnet = magnet;
  await F.tool('trend');
  const y = 1000 - bars[bar].c - 1;                       // a px off the close (its high): the magnet takes it
  F.down(bar * 10, y); F.up(bar * 10, y); F.down(100, 400); F.up(100, 400);
  await ticks();
  const p0 = [...F.D.state.list.values()].at(-1).pts[0];
  got.push([magnet, bar, Math.round(F.D.timeToLogical(p0.t) * 100) / 100, p0.p === bars[bar].c]);   // to 1/100 of a bar: a ms holds only so many decimals
}
console.log(JSON.stringify(got));""")
    assert out[0][:3] == [False, 6, 6]                          # without the magnet: already right
    assert [(b, l, on_close) for _, b, l, on_close in out[1:]] == [(6, 6, True), (7, 7, True), (3, 3, True), (2, 2, True)]


# ---- r15-drawings-3: a drag the system cancels -----------------------------------------------------------------------------

@needs_node
def test_a_drag_cut_off_by_pointercancel_puts_the_drawing_back_where_it_is_saved():
    """On a phone a line with its alert is dragged from 600 to 650 and the touch is cancelled before it lifts (a call
    banner, the app switcher): the line goes back to 600, where the server and its alert still have it. The next drag
    to 700 saves, and Ctrl+Z takes it back to 600. Before: it stayed at 650 on screen only, unsaved and with no undo
    step, and the next drag's undo step went back to 650."""
    out = run(r"""
table('ES').set('a', hline('a', 600, { alert: { on: true, direction: 'cross', repeat: false } }));
const F = frame('ES', { bars: minutes(50) });
await F.open();
const touch = { pointerType: 'touch' };
F.down(200, 400, touch); F.move(200, 380, touch); F.move(200, 350, touch); F.cancel();
await ticks();
const cancelled = { shown: F.D.state.list.get('a').pts[0].p, saved: table('ES').get('a').pts[0].p, puts: calls.filter((c) => c.startsWith('PUT')).length,
  steps: F.D.state.undo.length, dragging: F.D.state.drag };
F.down(200, 400, touch); F.move(200, 350, touch); F.move(200, 300, touch); F.up(200, 300, touch);
await ticks();
const dragged = table('ES').get('a').pts[0].p;
F.key('KeyZ', { ctrlKey: true });
await ticks();
console.log(JSON.stringify({ cancelled, dragged, undone: table('ES').get('a').pts[0].p, shown: F.D.state.list.get('a').pts[0].p }));""")
    assert out["cancelled"] == {"shown": 600, "saved": 600, "puts": 0, "steps": 0, "dragging": None}
    assert (out["dragged"], out["undone"], out["shown"]) == (700, 600, 600)


# ---- r15-drawings-4: Ctrl+C in one chart of a layout, Ctrl+V in another ---------------------------------------------------

@needs_node
def test_a_drawing_copied_in_one_chart_of_a_layout_pastes_in_another():
    """A 2-chart layout of ES and NQ: a line copied on ES (Ctrl+C) pastes on NQ (Ctrl+V), as a new NQ drawing; a copy
    made in the NQ chart then is the one the ES chart pastes. A chart on its own still pastes its copy, also when the
    page is framed by another site (whose window it cannot reach). Before: each chart kept its copy, and Ctrl+V in
    the other one did nothing."""
    out = run(r"""
table('ES').set('a', hline('a', 600));
table('NQ').set('n', hline('n', 900));
const A = frame('ES', { bars: minutes(50) }), B = frame('NQ', { bars: minutes(50) });
await A.open(); await B.open();
A.D.select('a'); A.key('KeyC', { ctrlKey: true });
B.key('KeyV', { ctrlKey: true }); await ticks();
const nq = [...table('NQ').values()].map((d) => d.pts[0].p).sort();
B.D.select('n'); B.key('KeyC', { ctrlKey: true });
A.key('KeyV', { ctrlKey: true }); await ticks();
const es = [...table('ES').values()].map((d) => d.pts[0].p).sort();
const alone = {};
for (const [name, top] of [['single', null], ['foreign', new Proxy({}, { get() { throw new Error('SecurityError'); }, set() { throw new Error('SecurityError'); } })]]) {
  table(name).set('x', hline('x', 300));
  const F = frame(name, { bars: minutes(50), top });
  await F.open();
  F.D.select('x'); F.key('KeyC', { ctrlKey: true });
  F.key('KeyV', { ctrlKey: true }); await ticks();
  alone[name] = [table(name).size, F.toasts[0]];
}
console.log(JSON.stringify({ toast: A.toasts[0], nq, es, alone }));""")
    assert out["toast"] == "Drawing copied: Ctrl+V pastes it (on any symbol, in any chart)"
    assert out["nq"] == [600, 900] and out["es"] == [600, 900]
    assert out["alone"] == {"single": [2, out["toast"]], "foreign": [2, out["toast"]]}


# ---- r15-drawings-5: Paste levels in 32nds -----------------------------------------------------------------------------------

@needs_node
def test_paste_levels_takes_treasury_prices_in_32nds_as_the_chart_shows_and_copies_them():
    """On a ZN chart (a 64th tick) the levels pasted as the axis shows them and 'Copy price' gives them (110'160),
    typed as 110'08.5 or in CME's dash form (110-16), are lines at their prices; decimals still work. A bad 32nd
    (110'40), a 32nd with words stuck on, or a dash form on ES stay 'not prices'. Before: every 32nd was refused."""
    out = run(r"""
const F = frame('ZN', { bars: minutes(50, 110.5), tick: 1 / 64 });
await F.open();
const zn = F.D.parseLevels("110'160|POC, 110'08.5|VAL; 110-16|VAH\n110.515625|X, 110'40|Y, 110'16POC|Z");
const box = F.ui.paste;
box.querySelector('textarea').value = "110'160|POC, 110'08.5|VAL";
for (const k of ['top', 'bottom']) box.querySelector(`[data-k=${k}]`).value = '';
await box.querySelector('[data-act=add]').onclick(); await ticks();
const added = [...table('ZN').values()].map((d) => [d.pts[0].p, d.style.text]);
const es = frame('ES', { bars: minutes(50) }).D.parseLevels("7845.75|VAH, 7845-16|X, 110'16|Y");
console.log(JSON.stringify({ zn, added, toast: F.toasts.at(-1), es }));""")
    assert out["zn"] == {"out": [{"p": 110.5, "text": "POC"}, {"p": 110.265625, "text": "VAL"}, {"p": 110.5, "text": "VAH"},
                                 {"p": 110.515625, "text": "X"}],
                         "bad": ["110'40|Y", "110'16POC|Z"]}
    assert out["added"] == [[110.5, "POC 110.5"], [110.265625, "VAL 110.265625"]] and out["toast"] == "Added 2 levels"
    assert out["es"] == {"out": [{"p": 7845.75, "text": "VAH"}], "bad": ["7845-16|X", "110'16|Y"]}
