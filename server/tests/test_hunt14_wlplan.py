"""Round 14 of the bug hunt, the watchlist and the Plan tab: the selected row kept (and highlighted) across a reload of
the lists, a menu's action on the row pressed when a symbol is listed twice, the change in percent of a negative
value, the details panel and a sorted list left in place under a click, the Plan tab's unsaved levels kept on a
reconnect, and Level Lines in 32nds. web/watchlist.js runs in node (web_harness.js's makeWatchlist), the parts of
app.js a test needs and web/studies.js in a vm, Market's quotes on a real Market."""
import shutil
from datetime import date, timedelta

import pytest
from chartsrv import details as det
from chartsrv.sessions import day_open
from test_hunt_web_chart import run_app
from test_hunt_web_ui import node
from test_review_fixes import build_market

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

# A watchlist whose saves are answered at once (each with the next version), what it saved, its menus and keys.
WL = """
const copy = (d) => JSON.parse(JSON.stringify(d));
let menu = null;
const saved = [];
const w = makeWatchlist(WEB, { env: { menu: (x, y, its) => { menu = its; }, ask: async () => 'majors',
  api: async (m, p, b) => { if (m !== 'PUT') return {}; saved.push(b.lists[0].items.slice()); return { ...copy(b), version: (b.base || 0) + 1 }; } } });
const body = w.root.querySelector('.wl-body'), scroll = w.root.querySelector('.wl-scroll');
const doc = (items, version = 1) => ({ lists: [{ id: 'main', name: 'Main', items: items.slice() }], flags: {}, names: {}, version });
const press = (key) => scroll.listeners.keydown.forEach((f) => f({ key, target: { closest: () => null }, preventDefault() {}, stopPropagation() {} }));
const lit = () => body.children.filter((e) => e.classList.contains('sel')).map((e) => e.querySelector('.tk').textContent);
const shows = () => w.detail.children[0].children.find((e) => e.className === 'wd-sym')?.textContent ?? null;
const pick = async (label) => { await menu.find((x) => x?.label === label).fn(); await settle(); };
const secMenu = (i) => body.listeners.click.forEach((f) => f({ target: { closest: (s) => (s === '.sec-more'
  ? { getBoundingClientRect: () => ({ left: 0, bottom: 0 }) } : s === '.wl-sec' ? { dataset: { i: String(i) } } : null) } }));
"""


# ---- r14-wlplan-1: the selection is the symbol selected, found again when the lists reload ---------------------------

@needs_node
def test_the_selected_row_stays_selected_and_highlighted_when_the_lists_reload_or_shift():
    """TSLA selected: the highlight stays on it after this page's own edit and its save's answer; another device's save
    that adds AMD above it, a section header removed above it ('Remove section, keep its symbols') or one added above
    it ('Add section above…') leave TSLA selected, so the details show TSLA and Del removes TSLA (it took MSFT, NVDA,
    or nothing)."""
    out = node(WL + """
const r = {};
const STOCKS = ['###STOCKS', 'AAPL', 'MSFT', 'TSLA', 'NVDA'];
w.list.setDoc(doc(STOCKS));
w.click(3);
r.clicked = lit();
w.detail.children.find((c) => c.className === 'wd-act').children[1].children.find((b) => b.className === 'f-red').onclick({ stopPropagation() {} });
await settle();
r.flagged = [lit(), shows()];
w.list.setDoc(doc(['###STOCKS', 'AMD', 'AAPL', 'MSFT', 'TSLA', 'NVDA'], 5));   // another device's save
r.reloaded = [lit(), shows()];
press('Delete');
await settle();
r.deleted = saved.at(-1);
w.list.setDoc(doc(['SPY', ...STOCKS], 10));
w.click(4);
secMenu(1);
await pick('Remove section, keep its symbols');
r.sectionGone = [lit(), shows()];
press('Delete');
await settle();
r.deleted2 = saved.at(-1);
w.list.setDoc(doc(STOCKS, 20));
w.rightClick(3);
await pick('Add section above…');
r.sectionAdded = [saved.at(-1), lit(), shows()];
press('Delete');
await settle();
r.deleted3 = saved.at(-1);
out(r);""")
    assert out["clicked"] == ["TSLA"]
    assert out["flagged"] == [["TSLA"], "TSLA"]
    assert out["reloaded"] == [["TSLA"], "TSLA"]
    assert out["deleted"] == ["###STOCKS", "AMD", "AAPL", "MSFT", "NVDA"]
    assert out["sectionGone"] == [["TSLA"], "TSLA"]
    assert out["deleted2"] == ["SPY", "AAPL", "MSFT", "NVDA"]
    assert out["sectionAdded"] == [["###STOCKS", "AAPL", "MSFT", "###MAJORS", "TSLA", "NVDA"], ["TSLA"], "TSLA"]
    assert out["deleted3"] == ["###STOCKS", "AAPL", "MSFT", "###MAJORS", "NVDA"]


@needs_node
def test_a_selected_symbol_listed_twice_is_found_again_as_its_own_copy():
    """USDJPY in FUTURES and in FOREX, the FOREX one selected: a reload that adds a symbol above both keeps the FOREX
    copy selected; one that removes the selected symbol leaves nothing selected."""
    out = node(WL + """
const r = {};
w.list.setDoc(doc(['###FUTURES', 'ES1!', 'USDJPY', '###FOREX', 'EURUSD', 'USDJPY']));
w.click(5);
w.list.setDoc(doc(['###FUTURES', 'NQ1!', 'ES1!', 'USDJPY', '###FOREX', 'EURUSD', 'USDJPY'], 5));
press('Delete');
await settle();
r.deleted = saved.at(-1);
w.list.setDoc(doc(['###FUTURES', 'ES1!', 'USDJPY', '###FOREX', 'EURUSD', 'USDJPY'], 10));
w.click(5);
w.list.setDoc(doc(['###FUTURES', 'ES1!', 'USDJPY', '###FOREX', 'EURUSD'], 15));
r.gone = [lit(), shows()];
out(r);""")
    assert out["deleted"] == ["###FUTURES", "NQ1!", "ES1!", "USDJPY", "###FOREX", "EURUSD"]
    assert out["gone"] == [[], "ES1!"]   # nothing selected: the panel shows the charted symbol's


# ---- r14-wlplan-2: a row's or a section's menu acts on the one pressed, also when its text is listed twice ------------

@needs_node
def test_the_row_and_section_menus_act_on_the_copy_pressed_of_a_symbol_or_section_listed_twice():
    """USDJPY in FUTURES and in FOREX (as the first-run list has it): Remove and 'Add section above…' on the FOREX
    row act on that row, not on the FUTURES one; also after a reload of the lists while the menu is open. Two sections
    named ENERGY: the second's menu removes, renames or adds to the second."""
    out = node(WL + """
const r = {};
const FX = ['###FUTURES', 'ES1!', 'USDJPY', 'NQ1!', '###FOREX', 'EURUSD', 'USDJPY', 'GBPUSD'];
let v = 1;
const fresh = (items) => w.list.setDoc(doc(items, v += 10));
fresh(FX);
w.rightClick(6);
await pick('Remove USDJPY');
r.removed = saved.at(-1);
fresh(FX);
w.rightClick(6);
await pick('Add section above…');
r.sectionAbove = saved.at(-1);
fresh(FX);
w.rightClick(6);
fresh(['###FUTURES', 'ES1!', 'USDJPY', 'NQ1!', 'RTY1!', '###FOREX', 'EURUSD', 'USDJPY', 'GBPUSD']);   // another device's save meanwhile
await pick('Remove USDJPY');
r.removedAfterReload = saved.at(-1);
const EN = ['###ENERGY', 'CL1!', '###ENERGY', 'XOM', 'XLE'];
fresh(EN);
secMenu(2);
await pick('Remove section, keep its symbols');
r.secRemoved = saved.at(-1);
fresh(EN);
secMenu(2);
await pick('Rename section…');
r.secRenamed = saved.at(-1);
fresh(EN);
secMenu(2);
await pick('Add symbol here…');
w.root.querySelector('#wl-add-in').value = 'CVX';
w.root.querySelector('#wl-addbox').onsubmit({ preventDefault() {} });
await settle();
r.addedHere = saved.at(-1);
out(r);""")
    assert out["removed"] == ["###FUTURES", "ES1!", "USDJPY", "NQ1!", "###FOREX", "EURUSD", "GBPUSD"]
    assert out["sectionAbove"] == ["###FUTURES", "ES1!", "USDJPY", "NQ1!", "###FOREX", "EURUSD", "###MAJORS", "USDJPY", "GBPUSD"]
    assert out["removedAfterReload"] == ["###FUTURES", "ES1!", "USDJPY", "NQ1!", "RTY1!", "###FOREX", "EURUSD", "GBPUSD"]
    assert out["secRemoved"] == ["###ENERGY", "CL1!", "XOM", "XLE"]
    assert out["secRenamed"] == ["###ENERGY", "CL1!", "###MAJORS", "XOM", "XLE"]
    assert out["addedHere"] == ["###ENERGY", "CL1!", "###ENERGY", "XOM", "XLE", "CVX"]


# ---- r14-wlplan-3: the change in percent of a value below 0 has the change's sign -------------------------------------

def test_a_quotes_and_the_details_change_in_percent_of_a_negative_value_has_the_changes_sign(tmp_path, monkeypatch):
    """FRED:NFCI from -0.52 to -0.50 is a rise of 0.02, +3.85% (it said -3.85%, so Sort by Chg% filed it with the
    losers); T10Y2Y from -0.05 to -0.10 a fall of 100%. The details panel's 1W, 1M... the same."""
    m, _ = build_market(tmp_path, monkeypatch)
    m.on_quote("FRED:NFCI", -0.50, -0.52, source="fred")
    m.on_quote("FRED:T10Y2Y", -0.10, -0.05, source="fred")
    m.on_quote("FRED:DGS10", 4.30, 4.20, source="fred")
    q = m.quotes
    assert round(q["FRED:NFCI"]["chg"], 4) == 0.02 and round(q["FRED:NFCI"]["pct"], 2) == 3.85
    assert round(q["FRED:T10Y2Y"]["pct"], 2) == -100.0
    assert round(q["FRED:DGS10"]["pct"], 2) == 2.38
    today = date(2026, 10, 30)
    rows = [{"t": day_open(today - timedelta(days=k)), "o": -0.52, "h": -0.5, "l": -0.53, "c": -0.52} for k in range(10, 0, -1)]
    assert det._from_daily(rows, -0.50, today)["perf"]["1W"] == 3.85


@needs_node
def test_a_formula_row_the_chart_header_and_the_data_window_give_a_negative_values_change_its_sign():
    """The spread ES1!-NQ1! (-18300 to -18150) up 150 is +0.82% in its row (it said -0.82%); a chart of FRED:NFCI
    (-0.52 to -0.50) heads '+0.02 +3.85%' (it said '+-3.85%'), and its data window '+0.02 (+3.85%)'."""
    out = node("""
const q = (s, last, prev) => ({ s, last, prev, chg: last - prev, pct: (last - prev) / prev * 100, ts: 1, rt: 'live', src: 'ninjatrader' });
const w = makeWatchlist(WEB, { quotes: { 'ES1!': q('ES1!', 6850, 6800), 'NQ1!': q('NQ1!', 25000, 25100) } });
w.list.setDoc({ lists: [{ id: 'm', name: 'M', items: ['ES1!-NQ1!'] }], flags: {}, names: {}, version: 1 });
const row = w.root.querySelector('.wl-body').children[0];
out([row.querySelector('.chg').textContent, row.querySelector('.pct').textContent]);""")
    assert out == ["+150.00", "+0.82%"]
    out = run_app(["symQuote", "header", "dataWindow", "shortNum"], """
const els = {};
const mk = () => { const e = { textContent: '', className: '', hidden: false, sel: {}, children: [], append(...k) { this.children.push(...k); },
  replaceChildren(...k) { this.children = k; } }; e.querySelector = (s) => (e.sel[s] ||= mk()); return e; };
const document = { createElement: () => mk() }, $ = (s) => (els[s] ||= mk()), px = (v) => v.toFixed(2), wlKey = (t) => t, wlIsFormula = () => false;
const bars = [{ t: 1, o: -0.52, h: -0.52, l: -0.52, c: -0.52 }, { t: 2, o: -0.50, h: -0.50, l: -0.50, c: -0.50 }];
const S = { symbol: 'FRED:NFCI', instruments: {}, lastBar: bars[1], replay: false, ext: { source: 'fred' }, cs: { dataWin: true }, bars, hoverIdx: 1,
  quotes: { 'FRED:NFCI': { s: 'FRED:NFCI', last: -0.50, prev: -0.52, src: 'fred' } } };
/*APP*/
header();
dataWindow(bars[1]);
const hd = els['#q-chg'];
console.log(JSON.stringify([`${hd.sel['.abs'].textContent} ${hd.sel['.pct'].textContent}`, els['#dwin'].children[4].children[0].textContent]));""")
    assert out == ["+0.02 +3.85%", "+0.02 (+3.85%)"]


# ---- r14-wlplan-4: the Plan tab keeps an unsaved levels paste or Level Lines edit on a reconnect ------------------------

PLAN_JS = """
const els = {}, noop = () => {};
const box = (id) => (els[id] ||= { id, value: '', dataset: {}, textContent: '', classList: { toggle() {} } });
const document = { activeElement: null }, $ = box;
const SAVED = { ES: '2026-10-30, 6850, 6870, 6830', NQ: '2026-10-30, 25100, 25200, 25000' };
const LINES = { ES: { RangeTop: 6900, RangeBottom: 6800, CustomLevels: '6850|POC' }, NQ: { RangeTop: 25300, RangeBottom: 24800, CustomLevels: '25000|POC' } };
const S = { symbol: 'ES', spec: '5m', st: {}, instruments: { ES: {}, NQ: {} }, levels: {}, quotes: {}, ind: { schema: { lines: {} } } };
Object.defineProperty(S, 'llSet', { get: () => LINES[S.symbol] });
const api = async (m, p) => ({ origin: 'paste', count: 3, date: '2026-10-30', paste: SAVED[p.split('/').pop()], error: '' });
const indDlg = { refresh: noop }, levelLinesOf = () => [], watch = { setDoc: noop, markActive: noop }, chartSym = (s) => s, TK_MULTI = false;
const drawings = { state: {} }, workspaces = null, calOpen = () => false, isExtSym = () => false, indReady = () => true;
const [renderRth, applyPriceFormat, indsBuild, indSets, applyStudyVisibility, drawLevels, renderAmtMenu, versionCheck, updateNote, tabSync,
  buildHeader, renderAlerts, renderEvents, moveSharedOut, loadInd, calLoad, tplLoad, shareLoad, setupCheck, cellPost, notifyStatus, subscribe] = Array(22).fill(noop);
const loadAmtServer = async () => {}, loadGapAlerts = async () => {}, llPrim = { update: noop }, rangePrim = llPrim, alertPrim = llPrim;
const journalOpen = () => false, journalLoad = noop;   // the Journal's reconnect reload (another part of hello)
let indHeard = null, tapeWant = '';
/*APP*/
const settle = async () => { for (let k = 0; k < 10; k++) await new Promise((r) => setImmediate(r)); };
const [lv, top, ll] = ['#pl-lv', '#pl-ll-top', '#pl-ll'].map(box);
const type = (el, v) => { el.value = v; markDirty({ target: el }); };
const draft = () => { type(lv, '2026-11-02, 6900, 6925, 6880'); type(top, '6950'); type(ll, '6850|POC, 6875|VAH'); document.activeElement = null; };
const shown = () => [lv, top, ll].map((e) => [String(e.value), !!e.dataset.dirty]);
const HELLO = { type: 'hello', instruments: S.instruments, catalog: {}, levels: {}, holidays: {}, only_futures: [], alerts: [], events: [], quotes: [], watchlists: {} };
(async () => {
  const r = {};
  renderPlanLines(true);
  await loadPlanLevels();   // the page as loaded: ES's saved levels and lines
  draft();
  onMessage({ type: 'levels', symbol: 'ES', levels: { date: '2026-11-02' } });   // ML levels published for ES
  await settle();
  r.published = shown();
  onMessage(HELLO);   // a reconnect
  await settle();
  r.reconnected = shown();
  S.symbol = 'NQ';   // a switch: the drafts are ES's
  onSymbolChanged();
  await settle();
  r.switched = shown();
  SAVED.NQ = '2026-11-02, 25150, 25250, 25050';
  onMessage({ type: 'levels', symbol: 'NQ', levels: { date: '2026-11-02' } });   // no draft: the new levels show
  await settle();
  r.updated = shown()[0];
  console.log(JSON.stringify(r));
})();
"""


@needs_node
def test_the_plan_tab_keeps_unsaved_levels_and_level_lines_when_the_page_reconnects_or_ml_levels_come():
    """A pasted levels line and a Level Lines range and levels typed, not saved, the cursor out of the box: ES's ML
    levels published and a reconnect (hello) leave them (both put ES's saved ones back); a switch to NQ shows NQ's,
    and NQ's newly published levels show in a box with no edits."""
    out = run_app(["markDirty", "fillBox", "LV_ORIGIN", "loadPlanLevels", "renderPlanLevels", "LL_BOXES", "renderPlanLines",
                   "onSymbolChanged", "onMessage", "SPEC_RE"], PLAN_JS)
    drafts = [["2026-11-02, 6900, 6925, 6880", True], ["6950", True], ["6850|POC, 6875|VAH", True]]
    assert out["published"] == drafts
    assert out["reconnected"] == drafts
    assert out["switched"] == [["2026-10-30, 25100, 25200, 25000", False], ["25300", False], ["25000|POC", False]]
    assert out["updated"] == ["2026-11-02, 25150, 25250, 25050", False]


# ---- r14-wlplan-5: a quote leaves the details panel's buttons, and a press its sorted rows, in place --------------------

@needs_node
def test_a_quote_or_the_servers_numbers_leave_the_details_panels_header_and_flag_buttons_in_place():
    """ES selected: a quote of ES (about each second while it trades) and the details' numbers arriving change the
    price and stats in place; the fold header and the flag buttons are the same elements (rebuilt, a click whose
    mouse went down on one before was lost)."""
    out = node("""
const q = (s, last, prev) => ({ s, last, prev, chg: last - prev, pct: (last - prev) / prev * 100, ts: 1, rt: 'live', src: 'ninjatrader' });
const quotes = { 'ES1!': q('ES1!', 6850, 6800) };
let answer = null;
const w = makeWatchlist(WEB, { quotes, env: { api: (m, p) => new Promise((r) => { answer = r; }) } });
w.list.setDoc({ lists: [{ id: 'm', name: 'M', items: ['ES1!', 'NQ1!'] }], flags: {}, names: {}, version: 1 });
w.click(0);
const [hd, , , , act] = w.detail.children;
quotes['ES1!'] = q('ES1!', 6855.25, 6800);
w.list.onQuotes([quotes['ES1!']]);
const quoted = w.detail.children.slice();
answer({ day: { o: 6840, h: 6860, l: 6830 } });
await settle();
const stats = w.detail.children[3].children.length;
out({ quoted: [quoted[0] === hd, quoted[4] === act, quoted[1].children[0].textContent], numbers: [w.detail.children[0] === hd, w.detail.children[4] === act, stats] });""")
    assert out == {"quoted": [True, True, "6,855.25"], "numbers": [True, True, 2]}


@needs_node
def test_a_sorted_lists_live_re_sort_waits_for_a_press_to_end_and_leaves_rows_in_order_alone():
    """Sorted by Chg%: the mouse down on NQ's row, a quote moves NQ up: the re-sort waits for the button to come up
    (rows taken out under it lost the click that opens its chart), then runs. A quote that moves no row leaves the
    rows where they are."""
    out = node("""
const timers = makeTimers();
const q = (s, last, prev) => ({ s, last, prev, chg: last - prev, pct: (last - prev) / prev * 100, ts: 1, rt: 'live', src: 'ninjatrader' });
const quotes = { 'ES1!': q('ES1!', 6850, 6800), 'NQ1!': q('NQ1!', 25000, 25100), AAPL: q('AAPL', 250, 245) };
const w = makeWatchlist(WEB, { quotes, timers });
w.list.setDoc({ lists: [{ id: 'm', name: 'M', items: ['ES1!', 'NQ1!', 'AAPL'] }], flags: {}, names: {}, version: 1 });
const body = w.root.querySelector('.wl-body'), head = w.root.querySelector('.wl-head');
const order = () => body.children.map((e) => e.querySelector('.tk').textContent);
const tick = async (s, last) => { quotes[s] = q(s, last, quotes[s].prev); w.list.onQuotes([quotes[s]]); await timers.runAll(); };
head.listeners.click.forEach((f) => f({ target: { closest: (s) => (s === '[data-col]' ? { dataset: { col: 'pct' } } : null) } }));
const r = { sorted: order() };
let kids = body.children;
await tick('AAPL', 251);
r.unmoved = body.children === kids;
const nq = body.children.find((e) => e.querySelector('.tk').textContent === 'NQ1!');
body.listeners.pointerdown.forEach((f) => f({ target: { closest: (s) => (s === '.wl-row, .wl-sec' ? nq : null) }, button: 0, pointerType: 'mouse', clientY: 10, pointerId: 1 }));
kids = body.children;
await tick('NQ1!', 25300);
r.pressed = [body.children === kids, order()];
w.win.pointerup.forEach((f) => f({ pointerId: 1 }));
await timers.runAll();
r.released = order();
out(r);""")
    assert out["sorted"] == ["AAPL", "ES1!", "NQ1!"]
    assert out["unmoved"] is True
    assert out["pressed"] == [True, ["AAPL", "ES1!", "NQ1!"]]
    assert out["released"] == ["AAPL", "NQ1!", "ES1!"]


# ---- r14-wlplan-6: Level Lines' levels in 32nds, as a Treasury chart shows its prices ---------------------------------

@needs_node
def test_level_lines_take_a_treasury_price_in_32nds_and_skip_what_is_not_a_price():
    """On ZN, 110'16|POC is drawn at 110.5, as the If/Then block reads it (it was drawn at 110'00: parseFloat stopped at
    the apostrophe), 110'16.5 at 110 + 16.5/32. A level that is not wholly a number (110-16, 6850abc) is skipped, as
    NinjaTrader's double.TryParse skips it, not drawn at its leading digits."""
    out = node("""
const vm = require('vm'), fs = require('fs');
const ctx = { console, CSS: { supports: () => true }, document: { createElement: () => ({ getContext: () => ({}) }) }, window: {}, navigator: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(`${WEB}/studies.js`, 'utf8') + '\\nthis.parseLevelLines = parseLevelLines;', ctx);
const lines = ctx.parseLevelLines("110'16|POC, 110'16.5|VAH, 110-16|MID, 6850abc|X, 110.5|DEC, 1e2|E,  6850.25 |ES", { color: '#fff', dash: 'Solid', width: 1 });
out(lines.map((l) => [l.price, l.label]));""")
    assert out == [[110.5, "POC"], [110.515625, "VAH"], [110.5, "DEC"], [100, "E"], [6850.25, "ES"]]
