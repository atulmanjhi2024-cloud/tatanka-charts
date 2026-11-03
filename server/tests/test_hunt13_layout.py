"""Bug hunt round 13, multi-chart layouts (web/app.js): a chart of a layout narrower than a phone is never the phone's
panel page (which hid its chart and header), a Journal trade clicked on a layout's page scrolls the active chart to its
entry, the page's + Create alert starts at the active chart's last price, and a linked crosshair is not taken for the
pointer by the + and Alt+H / Alt+A. The app.js parts run in node with what they call stubbed."""
from test_hunt_web_chart import WEB, app_lines, app_part, needs_node, run_app

STUDIES = (WEB / "studies.js").read_text()


def studies_part(name: str) -> str:
    """One function of web/studies.js as written there (to its closing bracket at the start of a line)."""
    start = STUDIES.index(f"function {name}(")
    return STUDIES[start:STUDIES.index("\n}\n", start) + 3]


# Elements whose classes are kept, as a browser's are.
CLASSES_JS = """
const classes = () => {
  const on = new Set();
  return { on, toggle: (c, v = !on.has(c)) => { if (v) on.add(c); else on.delete(c); return v; }, add: (c) => on.add(c),
    remove: (c) => on.delete(c), contains: (c) => on.has(c) };
};
const elOf = () => ({ classList: classes(), style: {}, dataset: {}, hidden: false, textContent: '', title: '', setAttribute() {} });
"""

# setSide as written, in each kind of window: what the phone's page rules (style.css, max-width 760px) key on
SIDE_JS = CLASSES_JS + """
const els = {}, $ = (s) => (els[s] ||= elOf()), document = { body: elOf() };
const store = {}, local = { get: (k, d) => store[k] ?? d, set: (k, v) => { store[k] = v; } }, pageUi = () => {};
const window = { innerWidth: 0 };
let TK_CELL = null, TK_MULTI = false;
/*APP*/
const out = [];
for (const [cell, multi, width] of [[1, false, 635], [0, false, 528], [2, false, 952], [null, false, 390], [null, true, 1920], [null, false, 1920]]) {
  TK_CELL = cell; TK_MULTI = multi; window.innerWidth = width;
  for (const open of [true, false]) {
    for (const k of Object.keys(els)) delete els[k];
    document.body = elOf();
    setSide(open);
    out.push({ cell, width, open, sideOff: $('main').classList.contains('side-off'), sidePage: document.body.classList.contains('side-page'),
      stored: store[sideKey()], toggle: $('#side-toggle').textContent });
  }
}
console.log(JSON.stringify(out));
"""


@needs_node
def test_a_chart_of_a_layout_is_never_the_phones_panel_page():
    """Each chart of a layout is the page in a frame, and a frame's media queries use its own width: a chart frame
    narrower than 760 px (the small charts of '1 large, 2 small', every chart of 3 x 2 on 1920 px, a phone in landscape)
    that took the panel's 'open' (sideOpen, '1' by default) became a panel page, and the phone's rules hid its chart
    and header while its panel is hidden in a frame: an empty cell. A chart frame stays on its chart whatever the
    shared setting says (and keeps it as it is), while the page itself still opens a phone's panel page."""
    out = run_app(["phoneLayout", "sideKey", "setSide"], SIDE_JS)
    for r in out:
        cell = r["cell"] is not None
        page = r["open"] and not cell
        assert (r["sideOff"], r["sidePage"]) == (not page, page), r
        assert r["stored"] == ("1" if r["open"] else "0"), r   # the shared setting is kept as the page has it
        assert r["toggle"] == ("⇥" if r["open"] else "⇤"), r
    phone = [r for r in out if r["cell"] is None and r["width"] == 390]
    assert [(r["sideOff"], r["sidePage"]) for r in phone] == [(False, True), (True, False)]


# journalShow on a layout's page, and the active chart's message handler (the TK_CELL block) taking what it posts
JOURNAL_JS = """
const posted = [], ranges = [], switched = [], toasts = [], listeners = {};
const T0 = 1_700_000_000_000, MIN = 60_000;
const S = { symbol: 'ES', instruments: { ES: {}, NQ: {} }, bars: Array.from({ length: 200 }, (_, i) => ({ t: T0 + i * MIN, c: 6900 + i })) };
const workspaces = { toActive: (m) => posted.push(JSON.parse(JSON.stringify(m))) };
const chart = { timeScale: () => ({ setVisibleLogicalRange: (r) => ranges.push(r) }) };
const toast = (m) => toasts.push(m), setSymbol = (s) => { switched.push(s); }, cellPost = () => {}, cellXhair = () => {};
const location = { origin: 'http://x' }, $ = () => ({ addEventListener() {} });
const document = { addEventListener() {} }, window = { addEventListener: (t, f) => { listeners[t] = f; }, parent: { postMessage() {} } };
const TK_CELL = 1;
let TK_MULTI = true, linkedChange = false;
/*APP*/
const deliver = () => { TK_MULTI = false; for (const m of posted.splice(0)) listeners.message({ origin: location.origin, data: { tk: true, ...m } }); TK_MULTI = true; };
// the page's Journal: a trade on the symbol the active chart shows (bar 50), then one on another symbol
journalShow({ symbol: 'ES', entry_ts: T0 + 50 * MIN + 5000 });
deliver();
const same = { ranges: ranges.splice(0), switched: switched.splice(0) };
journalShow({ symbol: 'NQ', entry_ts: T0 + 20 * MIN });
deliver();
console.log(JSON.stringify({ same, other: { ranges: ranges.splice(0), switched: switched.splice(0), goTo: S.goTo ?? null }, toasts }));
"""


@needs_node
def test_a_journal_trade_clicked_on_a_layouts_page_scrolls_the_active_chart_to_its_entry():
    """On a layout's page the Journal only sent the trade's symbol to the active chart: on that chart's own symbol
    nothing happened, and on another symbol it switched but stayed at the latest bars. The active chart is now given
    the trade and does what a single chart does: scrolls to the entry's bar, or switches and scrolls once the bars
    are in (S.goTo, applyHistory)."""
    js = JOURNAL_JS.replace("/*APP*/", studies_part("barIndexTo") + "\n/*APP*/\n" + app_lines("if (TK_CELL !== null) {", "}"))
    out = run_app(["journalShow", "goToTime"], js)
    assert out["same"] == {"ranges": [{"from": 10, "to": 110}], "switched": []}
    assert out["other"] == {"ranges": [], "switched": ["NQ"], "goTo": 1_700_000_000_000 + 20 * 60_000}
    assert out["toasts"] == []


# openAlertDialog as written: the price a new alert starts at
ALERT_JS = CLASSES_JS + """
const els = {}, $ = (s) => (els[s] ||= { ...elOf(), value: '', focus() {}, select() {} });
const noop = () => {};
const [closeCtx, toast, indDlgFill, setExp, syncDlg] = Array(5).fill(noop);
const CHANNEL = new Set(['inside', 'outside']), dlg = {}, ax = { input: { toInput: () => '' }, short: 'CT' };
const wlKey = (sym) => `${sym}1!`, indBase = (x) => x, snap = (p) => p, fmtSym = (sym, p) => p.toFixed(2);
const S = { symbol: 'NQ', ext: null, lastBar: null, quotes: { 'NQ1!': { last: 25005.25 }, 'ES1!': { last: 6901.5 } }, instruments: { ES: {}, NQ: {} } };
/*APP*/
const out = [];
openAlertDialog({ price: S.lastBar?.c });   // a layout's page (no bars): its Alerts tab's + Create alert
out.push([$('#al-title').textContent, $('#al-v1').value]);
S.lastBar = { c: 25010.5 };   // a single chart with its bars
openAlertDialog({ price: S.lastBar?.c });
out.push([$('#al-title').textContent, $('#al-v1').value]);
openAlertDialog({});   // Alt+A off the chart, the bars in
out.push([$('#al-title').textContent, $('#al-v1').value]);
console.log(JSON.stringify(out));
"""


@needs_node
def test_create_alert_on_a_layouts_page_starts_at_the_active_charts_last_price():
    """The page around a layout's charts loads no bars, so its + Create alert (on the active chart's symbol) opened
    with an empty price; with no bars it now starts at the symbol's quote, and a chart with bars at its last bar."""
    out = run_app(["openAlertDialog"], ALERT_JS)
    assert out == [["Create alert on NQ", "25005.25"], ["Create alert on NQ", "25010.50"], ["Create alert on NQ", "25010.50"]]


# The chart's crosshairMove handler and alertPlus as written, and a chart of a layout's message handler moving its
# crosshair to the time another chart's pointer is at. The library tells a crosshair it was asked to move to later, with
# no pointer event, as it does in a browser.
XHAIR_JS = """
const listeners = {}, later = [], noop = () => {};
const T0 = 1_700_000_000_000, MIN = 60_000;
const S = { bars: Array.from({ length: 100 }, (_, i) => ({ t: T0 + i * MIN, c: 6800 + i })), inds: [], hoverIdx: -1 };
const plus = { hidden: true, dataset: {}, style: {}, offsetWidth: 20, offsetHeight: 16, matches: () => false };
const $ = (s) => (s === '#ax-plus' ? plus : { addEventListener() {}, clientWidth: 800 });
const priceAt = (y) => 7000 - y, yAt = (price) => 7000 - price;   // the price scale: 1 point a pixel
const candles = { coordinateToPrice: priceAt }, vol = {};
let move = null;
const chart = {
  subscribeCrosshairMove: (f) => { move = f; },
  priceScale: () => ({ width: () => 60 }),
  setCrosshairPosition: (price, time) => later.push({ time, point: { x: 1, y: yAt(price) }, seriesData: new Map([[candles, { close: price }]]) }),
  clearCrosshairPosition: () => {},
};
const pointer = (y) => move({ time: T0, point: { x: 1, y }, seriesData: new Map([[candles, { close: 6800 }]]), sourceEvent: { clientX: 1, clientY: y } });
const frame = () => { for (const p of later.splice(0)) move(p); };   // the library's next frame
const timeOf = (b) => b.t, barIndexAt = () => 0, barAt = () => null, legend = noop, cellXhair = noop;
const location = { origin: 'http://x' }, document = { addEventListener() {} };
const window = { addEventListener: (t, f) => { listeners[t] = f; }, parent: { postMessage() {} } };
const TK_CELL = 0, TK_MULTI = false;
let linkedChange = false, plusHide = 0, plusTouch = false;
const setTimeout = (f) => { f(); return 0; }, clearTimeout = noop;   // the + hides at once when the pointer is not on it
/*APP*/
const linked = (ms) => listeners.message({ origin: location.origin, data: { tk: true, type: 'xhair', ms } });
const out = {};
pointer(200);   // the pointer on this chart (the one focused): its own price
out.own = [S.hoverPrice, plus.hidden];
move({ time: undefined, point: undefined, seriesData: new Map(), sourceEvent: { clientX: 0, clientY: 0 } });   // it leaves
out.left = [S.hoverPrice, plus.hidden];
linked(T0 + 40 * MIN); frame();   // the pointer on another chart of the layout
out.linked = [S.hoverPrice, plus.hidden];
frame(); later.push({ time: T0, point: { x: 1, y: yAt(6840) }, seriesData: new Map([[candles, { close: 6840 }]]) }); frame();   // a tick redraws it
out.tick = [S.hoverPrice];
linked(null);
out.cleared = [S.hoverPrice];
pointer(150);   // back on this chart
out.back = [S.hoverPrice, plus.hidden];
console.log(JSON.stringify(out));
"""


@needs_node
def test_a_linked_crosshair_is_not_the_pointers_price():
    """With the crosshair link on, another chart's pointer moved this chart's crosshair to the close of the bar at that
    time, and the crosshair handler took it for the pointer: the + showed at that close and Alt+H / Alt+A in this chart
    (the one focused) put a line or an alert there, at a price never pointed at (also after the linked crosshair went
    away). A linked crosshair now leaves the pointer's price alone until the pointer is on this chart again."""
    js = XHAIR_JS.replace("/*APP*/", "\n".join([app_part("alertPlus"), app_lines("chart.subscribeCrosshairMove(", "});"),
                                                 app_lines("if (TK_CELL !== null) {", "}")]))
    out = run_app([], js)
    assert out["own"] == [6800, False]
    assert out["left"] == [None, True]
    assert out["linked"] == [None, True]
    assert out["tick"] == [None]
    assert out["cleared"] == [None]
    assert out["back"] == [6850, False]
