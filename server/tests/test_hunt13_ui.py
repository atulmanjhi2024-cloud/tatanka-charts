"""Round 13 of the bug hunt, the chart page: the fired-alert panel kept clear of the toast, the menus, the dialogs and
the chart's bars; '+ Alert' let go by Esc and a right-click; Reset chart view keeping the room the AMT Toolkit's
labels need; and a drawing made while all are hidden. The app.js parts and web/drawings.js run in node with the rest
stubbed; the panel's place and stacking are read from web/style.css."""
import json
import re
import shutil
import subprocess

import pytest

from test_hunt3_drawings import run as run_drawings
from test_hunt_web_chart import WEB, app_lines, app_part, run_app

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
CSS = (WEB / "style.css").read_text()


def node(src: str):
    """Runs src in node; what it logs last is the result (JSON)."""
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().split("\n")[-1])


def css_rules(text: str) -> list[tuple[str, str, dict]]:
    """Every rule of a style sheet as (its @media, its selector list, its declarations): '' outside any @media."""
    text, out, i = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL), [], 0
    stack = []
    while True:
        j = text.find("{", i)
        if j < 0:
            return out
        k = text.find("}", i)
        if 0 <= k < j:   # a block closes first: the @media's
            stack.pop()
            i = k + 1
            continue
        head = text[i:j].strip()
        if head.startswith("@"):
            stack.append(head)
            i = j + 1
            continue
        end = text.find("}", j)
        decls = {a.strip(): b.strip() for a, _, b in (d.partition(":") for d in text[j + 1:end].split(";")) if b.strip()}
        out.append((stack[-1] if stack else "", head, decls))
        i = end + 1


RULES = css_rules(CSS)
PHONE = "@media (max-width: 760px)"


def decl(selector: str, prop: str, media: str = "") -> str | None:
    """A property of a selector as the style sheet sets it, the last rule naming it winning (only the one @media)."""
    got = None
    for m, sel, d in RULES:
        if m == media and selector in (s.strip() for s in sel.split(",")) and prop in d:
            got = d[prop]
    return got


def bottom_px(selector: str, media: str = "") -> int:
    """A fixed element's bottom: the px of calc(Npx + env(safe-area-inset-bottom))."""
    v = decl(selector, "bottom", media) or decl(selector, "bottom")
    return int(re.match(r"calc\((\d+)px \+ env\(safe-area-inset-bottom\)\)$", v).group(1))


# ---- r13-alerts-4, r13-ui-2: the fired-alert panel covers no toast, menu, dialog or bar ------------------------------

# Above the panel: the toast, every menu and dialog, and the chart's own bars and buttons (the draw bar, the replay
# bar, a drawing's bar and its sheet on a phone, the object tree, Paste levels, the quick symbol box, the go-to-live
# and nav buttons and the + by the price axis).
ABOVE = [("#toast", ""), ("#ctx", ""), (".modal", ""), ("#ask-dlg", ""), ("#login", ""), ("#tz-menu", ""), ("#tf-menu", ""),
         ("#st-menu", ""), ("#cs-menu", ""), ("#ws-menu", ""), ("#draw-edit.full", PHONE), ("#draw-tree", ""), ("#draw-paste", ""),
         ("#quick", ""), ("#draw-bar", ""), ("#replay-bar", ""), ("#draw-edit", ""), ("#ax-plus", ""), ("#go-live", ""), ("#nav", "")]


def test_the_fired_alert_panel_is_under_the_toast_the_menus_the_dialogs_and_the_bars():
    """The panel stays until OK: anything drawn under it could not be read or clicked until then (the phone's More
    menu, the chart's right-click menu, the Indicators dialog's buttons, a drawing's settings sheet, the replay bar).
    So it is under every one of them."""
    z = int(decl("#fired", "z-index"))
    over = {sel: int(decl(sel, "z-index", media)) for sel, media in ABOVE}
    assert {sel: v for sel, v in over.items() if v <= z} == {}, f"#fired z-index {z}"


def test_the_fired_alert_panel_sits_above_the_toast():
    """At every width the panel is above the toast's place (a toast of a line: 10px padding, a 1px border and 14px
    text at 1.35, about 41px), so an AMT, gap or news alert, or any message, is seen while the panel waits for OK; on a
    phone, both above the page bar."""
    line = 2 * 10 + 2 * 1 + round(14 * 1.35)
    for media in ("", PHONE):
        assert bottom_px("#fired", media) >= bottom_px("#toast", media) + line, media or "desktop"
    assert bottom_px("#toast", PHONE) == 52   # above the phone's page bar, and the panel above that


# ---- r13-ui-1: Esc and a right-click let go of '+ Alert' ---------------------------------------------------------------

PLACE_JS = """
const listeners = {}, calls = [];
function el() {
  const ls = {};
  return { ls, classList: { on: new Set(), toggle(c, v) { v ? this.on.add(c) : this.on.delete(c); }, contains(c) { return this.on.has(c); } },
    addEventListener(t, f) { (ls[t] ||= []).push(f); }, getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 400 }) };
}
const ids = {}, $ = (q) => (ids[q] ||= el());
const document = { addEventListener(t, f) { (listeners[t] ||= []).push(f); } };
const S = { placing: false, replayPick: false, replay: null };
const noop = () => {}, openTf = noop, openSt = noop, setAnchoring = noop, replayPicking = noop, replayExit = noop;
const chart = { paneSize: () => ({ height: 380 }) }, PS = () => ({ width: () => 60 }), candles = { coordinateToPrice: (y) => 7000 - y / 4 };
const drawings = { letGo: () => false }, openScaleCtx = () => calls.push('scale menu'), openCtx = () => calls.push('menu');
/*APP*/
const rightClick = () => { const e = { clientX: 300, clientY: 200, prevented: false, preventDefault() { this.prevented = true; } };
  $('#chart').ls.contextmenu.forEach((f) => f(e)); return e.prevented; };
const state = () => [S.placing, $('#place').classList.contains('on'), $('#chart-wrap').classList.contains('placing')];
setPlacing(true);
const on = state();
listeners.keydown.forEach((f) => f({ key: 'Escape' }));
const esc = state();
setPlacing(true);
const prevented = rightClick();
const right = { state: state(), calls: calls.slice(), prevented };
rightClick();   // the next right-click opens the menu
console.log(JSON.stringify({ on, esc, right, next: calls }));
"""


@needs_node
def test_esc_or_a_right_click_lets_go_of_the_alert_placing_mode():
    """'+ Alert' then Esc, or a right-click on the chart, to back out: the mode is let go as a drawing tool is (the
    keys dialog: Esc stops drawing; a right-click lets go as Esc does), so a later plain click on the chart, on any
    timeframe, makes no live price alert. The right-click opens no menu; the next one does."""
    src = PLACE_JS.replace("/*APP*/", "\n".join([
        app_part("setPlacing"),
        app_lines("document.addEventListener('keydown', (e) => {", "});"),
        app_lines("$('#chart').addEventListener('contextmenu', (e) => {", "});")]))
    out = node(src)
    assert out["on"] == [True, True, True]
    assert out["esc"] == [False, False, False]
    assert out["right"] == {"state": [False, False, False], "calls": [], "prevented": True}
    assert out["next"] == ["menu"]


# ---- r13-ui-3: Reset chart view keeps the right margin the Toolkit's labels need ---------------------------------------

RESET_JS = """
let range = null, auto = null;
const S = { bars: Array(568).fill({}), cs: { rightBars: 6 } };
const chart = { timeScale: () => ({ options: () => ({ rightOffset: 55 }), setVisibleLogicalRange: (r) => { range = r; } }) };
const setAuto = (on) => { auto = on; };
/*APP*/
resetView();
console.log(JSON.stringify({ range, auto }));
"""


@needs_node
def test_reset_chart_view_keeps_the_room_the_toolkit_labels_need():
    """The AMT Toolkit on: amtLayout made the right margin 55 bars for its labels and developing profile (more than
    the 'right margin' setting's 6). Alt+R, the reset button or the menu's Reset chart view shows the latest bars with
    that margin, as a fresh load does, not 6 bars with the labels drawn over the latest candles."""
    out = run_app(["resetView"], RESET_JS)
    assert out == {"range": {"from": 568 - 150 - 55, "to": 568 + 55}, "auto": True}


# ---- r13-ui-4: a drawing made while all are hidden ----------------------------------------------------------------------

@needs_node
def test_hide_all_lets_go_of_the_selected_drawing():
    """Hide all (the eye in Draw, or the chart's menu) with a drawing selected: it is let go, so no settings bar is
    left up for a line that isn't drawn, and the Delete key does not remove it unseen."""
    out = run_drawings(r"""
table('ES').set('a', hline('a', 7000));
await open('ES');
D.select('a');
await press('hide');
key({ key: 'Delete' }); await ticks();
console.log(JSON.stringify({ hidden: D.state.hidden, selected: D.state.selected, bar: ui.edit.hidden, kept: table('ES').has('a') }));""")
    assert out == {"hidden": True, "selected": None, "bar": True, "kept": True}


@needs_node
def test_a_drawing_made_while_all_are_hidden_shows_them_again():
    """With all drawings hidden, a new one (the chart menu's 'Horizontal line at', Alt+H, a drawing tool picked, a
    paste, Paste levels) shows them all again: it is drawn, with its handles and settings bar, not made unseen and
    left selected. The ruler, which is not a drawing, leaves them hidden."""
    out = run_drawings(r"""
table('ES').set('a', hline('a', 7000));
await open('ES');
const st = () => ({ hidden: D.state.hidden, n: D.state.list.size, selected: D.state.selected !== null, bar: !ui.edit.hidden });
await press('hide');
D.addHline(7010); await ticks();
const line = { ...st(), handles: arcs() };
await press('hide');
D.setTool('trend');
const tool = D.state.hidden;
pointer('pointerdown', 120, 200); pointer('pointerup', 120, 200); await ticks();
const drawing = { hidden: D.state.hidden, creating: !!D.state.creating, handles: arcs() };
key({ key: 'Escape' });
D.select('a');
key({ ctrlKey: true, code: 'KeyC' });
await press('hide');
key({ ctrlKey: true, code: 'KeyV' }); await ticks();
const pasted = st();
await press('hide');
await pasteLevels('7030|VAH');
const levels = { hidden: D.state.hidden, n: D.state.list.size };
await press('hide');
D.setTool('measure');
const ruler = D.state.hidden;
console.log(JSON.stringify({ line, tool, drawing, pasted, levels, ruler }));""")
    assert out["line"] == {"hidden": False, "n": 2, "selected": True, "bar": True, "handles": 1}
    assert out["tool"] is False and out["drawing"] == {"hidden": False, "creating": True, "handles": 2}
    assert out["pasted"] == {"hidden": False, "n": 3, "selected": True, "bar": True}
    assert out["levels"] == {"hidden": False, "n": 4}
    assert out["ruler"] is True


LAYOUT_JS = r"""
const vm = require('vm'), fs = require('fs');
const observed = [];
function el(tag = 'div') {
  const cls = new Set();
  const e = { tagName: tag.toUpperCase(), children: [], style: {}, dataset: {}, hidden: false, listeners: {},
    classList: { toggle(c, v = !cls.has(c)) { if (v) cls.add(c); else cls.delete(c); }, add(c) { cls.add(c); }, remove(c) { cls.delete(c); }, contains: (c) => cls.has(c) },
    append(...k) { this.children.push(...k); }, replaceChildren(...k) { this.children = k; },
    addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  if (tag === 'iframe') {   // a chart: what is open in it, as its page's querySelector finds it
    e.open = null;
    e.contentWindow = { postMessage() {} };
    e.contentDocument = { body: {}, querySelector: (s) => { e.asked = s; return e.open; } };
  }
  return e;
}
const store = { layout: '4', device: 'pc' };
const els = { 'ws-btn': el('button'), 'ws-menu': el(), grid: el() };
const ctx = { console, localStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem() {}, key: () => null, length: 0 },
  window: { innerWidth: 1400, addEventListener() {} }, location: { search: '', origin: 'http://x', reload() {} },
  document: { getElementById: (id) => els[id], createElement: el, addEventListener() {}, body: el('body') },
  MutationObserver: function (f) { this.observe = (node, opts) => observed.push({ node, opts, f }); },
  setTimeout: () => 0, clearTimeout() {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(WEB + '/layout.js', 'utf8') + ';this.cw = createWorkspaces;', ctx);
ctx.cw({ api: async () => ({ workspaces: [] }), toast() {}, fail() {}, ask: async () => null, local: { get: (k, d) => d, set() {} }, onActive() {}, reload() {} });
const frames = els.grid.children, body = ctx.document.body, over = () => body.classList.contains('cell-over');
frames.forEach((f) => (f.listeners.load || []).forEach((g) => g()));   // the charts load
const out = { frames: frames.length, start: over(),
  watched: observed.map((o) => [frames.findIndex((f) => f.contentDocument.body === o.node), o.opts.subtree, o.opts.attributeFilter]) };
const changed = (i) => observed.filter((o) => o.node === frames[i].contentDocument.body).forEach((o) => o.f());
frames[3].open = {};   // the bottom-right chart's dialog, menu, toast or drawing bar opens
changed(3);
out.open = over();
frames[3].open = null;
changed(3);
out.closed = over();
out.asked = frames[3].asked;
console.log(JSON.stringify(out));
"""


@needs_node
def test_in_a_layout_the_panel_steps_aside_for_a_charts_dialog_menu_or_note():
    """In a layout each chart is a frame, and its dialogs, menus, notes and drawing bar are drawn in it: under the
    page's fired-alert panel, whatever its z-index (the bottom-right chart's Indicators dialog footer or a right-click
    menu opened low). The page watches each chart and, while one of those is open, the panel steps aside. It also sits
    above the charts' bars, which a chart under 760 px wide has at its bottom."""
    out = node(f"const WEB = {json.dumps(str(WEB))};\n{LAYOUT_JS}")
    assert out["frames"] == 4 and out["start"] is False
    assert out["watched"] == [[i, True, ["hidden", "class"]] for i in range(4)]
    assert out["open"] is True and out["closed"] is False
    asked = {s.strip() for s in out["asked"].split(",")}
    assert {".modal:not([hidden])", "#ctx:not([hidden])", ".menu:not([hidden])", "#tf-menu:not([hidden])",
            "#draw-edit:not([hidden])", "#toast.show"} <= asked
    assert decl("body.cell-over #fired", "visibility") == "hidden"
    assert bottom_px("body.multi #fired") >= bottom_px("#fired", PHONE)
