"""Fixes from round 3 of the bug hunt, in the watchlist: web/watchlist.js runs in node (web_harness.js's makeWatchlist)
with a server that keeps the document, answers each PUT and sends each save to the page, as Market.set_watchlists."""
import shutil

import pytest
from chartsrv import watchlists as wl
from test_hunt_web_ui import node

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

# The server: PUT /api/watchlists saves the whole document (last write wins), sends it to the page as a 'watchlists'
# broadcast (app.js: watch.setDoc) and answers with it. Each PUT waits in `puts` until the test lets it through; `sent`
# keeps what each one sent.
SERVER = """
let server = null, menu = null, answer = null;
const puts = [], sent = [], toasts = [], copy = (d) => JSON.parse(JSON.stringify(d));
const env = {
  api: (method, path, body) => method === 'GET' ? Promise.resolve(copy(server)) : new Promise((resolve) => {
    const doc = copy(body);
    sent.push(doc);
    puts.push(() => { server = doc; w.list.setDoc(copy(doc)); resolve(copy(doc)); });
  }),
  ask: () => new Promise((r) => { answer = r; }),
  menu: (x, y, items) => { menu = items; },
  toast: (m) => toasts.push(m),
};
const w = makeWatchlist(WEB, { env });
const body = w.root.querySelector('.wl-body'), scroll = w.root.querySelector('.wl-scroll');
const shown = () => body.children.filter((e) => e.className === 'wl-row').map((e) => e.querySelector('.tk').textContent);
const press = (key) => scroll.listeners.keydown.forEach((f) => f({ key, target: { closest: () => null }, preventDefault() {}, stopPropagation() {} }));
const answerAll = async () => { while (puts.length) { puts.shift()(); await settle(); } };
const load = (doc) => { server = copy(doc); w.list.setDoc(copy(doc)); };
const pick = (label) => { w.root.querySelector('#wl-name').onclick(); menu.find((x) => x?.label === label).fn(); };
"""


def test_rename_and_delete_list_work_when_the_lists_reload_while_their_dialog_is_open():
    """A save from another device (or a reconnect's hello) while Rename… or Delete list… waits for its answer: the
    rename and the delete still happen. Deleting the last list left, the other one deleted meanwhile elsewhere, does
    nothing (the server keeps at least one)."""
    out = node(SERVER + """
const two = { lists: [{ id: 'a', name: 'Futures', items: ['ES1!', 'NQ1!'] }, { id: 'b', name: 'Stocks', items: ['AAPL'] }], flags: {}, names: {} };
const names = () => server.lists.map((l) => l.name);
load(two);
pick('Rename…');
w.list.setDoc(copy(server));   // a 'watchlists' broadcast while the box is open
answer('Index futures');
await settle(); await answerAll();
const renamed = names();
pick('Delete list…');
w.list.setDoc(copy(server));
answer('');
await settle(); await answerAll();
const deleted = names(), shownName = w.root.querySelector('#wl-name span').textContent;
load(two);
pick('Futures');
pick('Delete list…');
load({ ...two, lists: [two.lists[0]] });   // another device deleted Stocks meanwhile
answer('');
await settle(); await answerAll();
out({ renamed, deleted, shownName, last: names() });""")
    assert out["renamed"] == ["Index futures", "Stocks"]
    assert out["deleted"] == ["Stocks"] and out["shownName"] == "Stocks"
    assert out["last"] == ["Futures"]


def test_an_earlier_saves_answer_or_broadcast_does_not_undo_newer_edits():
    """Three rows deleted on a slow link, the first save's broadcast and answer arriving after the second delete: the
    second deleted row stays gone, and the third delete saves the list on screen less that row. Once nothing is on its way,
    a save from another device shows again."""
    out = node(SERVER + """
load({ lists: [{ id: 'main', name: 'Main', items: ['AAPL', 'MSFT', 'TSLA', 'NVDA', 'AMD'] }], flags: {}, names: {} });
w.click(1); press('Delete');   // MSFT
w.click(1); press('Delete');   // TSLA, while the first save is on its way
puts.shift()();                // the first save's broadcast and answer
await settle();
const afterFirst = shown();
w.click(1); press('Delete');   // NVDA, the second row on screen now
await answerAll();
const after = { shown: shown(), server: server.lists[0].items };
w.list.setDoc({ lists: [{ id: 'main', name: 'Main', items: ['AAPL', 'AMD', 'META'] }], flags: {}, names: {} });   // another device
out({ afterFirst, after, other: shown() });""")
    assert out["afterFirst"] == ["AAPL", "NVDA", "AMD"]
    assert out["after"] == {"shown": ["AAPL", "AMD"], "server": ["AAPL", "AMD"]}
    assert out["other"] == ["AAPL", "AMD", "META"]


def test_a_formula_with_a_fred_eia_or_data_series_shows_its_value():
    """FRED:DGS10-FRED:DGS2 (docs/WATCHLIST.md's example) and formulas over EIA and DATA series are computed from
    their parts' quotes, which the server sends under those names (watchlists.quote_key)."""
    out = node("""
const w = makeWatchlist(WEB, { quotes: {
  'FRED:DGS10': { s: 'FRED:DGS10', last: 4.1, prev: 4, ts: 1, rt: 'eod', src: 'fred' },
  'FRED:DGS2': { s: 'FRED:DGS2', last: 3.5, prev: 3.6, ts: 1, rt: 'eod', src: 'fred' },
  'EIA:PET.WCESTUS1.W': { s: 'EIA:PET.WCESTUS1.W', last: 420000, prev: 410000, ts: 1, rt: 'eod', src: 'eia' },
  'DATA:GOLDOIL': { s: 'DATA:GOLDOIL', last: 30, prev: 29, ts: 1, rt: 'eod', src: 'data' },
  SPY: { s: 'SPY', last: 410, prev: 400, ts: 1, rt: 'delayed', src: 'yahoo' },
} });
const items = ['FRED:DGS10-FRED:DGS2', 'EIA:PET.WCESTUS1.W/1000', 'DATA:GOLDOIL*2', 'SPY/(FRED:DGS10+1)'];
w.list.setDoc({ lists: [{ id: 'a', name: 'A', items }], flags: {}, names: {} });
const rows = w.root.querySelector('.wl-body').children;
out(rows.map((r) => [r.querySelector('.tk').textContent, r.querySelector('.last').textContent, r.querySelector('.chg').textContent]));""")
    assert out == [["FRED:DGS10-FRED:DGS2", "0.6000", "+0.2000"], ["EIA:PET.WCESTUS1.W/1000", "420.00", "+10.00"],
                   ["DATA:GOLDOIL*2", "60.00", "+2.00"], ["SPY/(FRED:DGS10+1)", "80.39", "+0.39"]]


def test_add_section_above_goes_above_its_row_when_the_list_reloads_while_it_is_named():
    """Add section above… on TSLA while another device moves TSLA to the top: the section goes above TSLA where it is
    now. TSLA removed meanwhile instead: no section, as the row menu's other actions do nothing once their row is gone."""
    out = node(SERVER + """
const doc = (items) => ({ lists: [{ id: 'main', name: 'Main', items }], flags: {}, names: {} });
const addAbove = async (i, now, name) => {
  load(doc(['AAPL', 'MSFT', 'TSLA']));
  w.rightClick(i);
  menu.find((x) => x?.label === 'Add section above…').fn();
  load(doc(now));   // a save from another device while the name is typed
  answer(name);
  await settle(); await answerAll();
  return server.lists[0].items;
};
out({ moved: await addAbove(2, ['TSLA', 'AAPL', 'MSFT'], 'tech'), removed: await addAbove(2, ['AAPL', 'MSFT'], 'tech'),
  same: await addAbove(1, ['AAPL', 'MSFT', 'TSLA'], 'soft') });""")
    assert out == {"moved": ["###TECH", "TSLA", "AAPL", "MSFT"], "removed": ["AAPL", "MSFT"],
                   "same": ["AAPL", "###SOFT", "MSFT", "TSLA"]}


def test_lowercase_fred_eia_and_data_parts_are_written_as_the_server_keeps_them():
    """Typed in lowercase, a series alone (data:goldoil) or in a formula (spy/fred:dgs10) is written as the server
    cleans it (watchlists._clean_item): in capitals, its source and name and the formula's other parts too (quotes are
    keyed so). Its row shows it, not "spy/dgs10" (the source dropped as if an exchange), and a series already in the
    section is not added a second time."""
    out = node(SERVER + """
load({ lists: [{ id: 'main', name: 'Main', items: ['DATA:GOLDOIL'] }], flags: {}, names: {} });
w.root.querySelector('#wl-add-in').value = 'data:goldoil, spy/fred:dgs10, eia:pet.wcestus1.w/1000';
w.root.querySelector('#wl-addbox').onsubmit({ preventDefault() {} });
out({ shown: shown(), sent: sent.at(-1).lists[0].items, toasts });""")
    want = ["DATA:GOLDOIL", "SPY/FRED:DGS10", "EIA:PET.WCESTUS1.W/1000"]
    assert out == {"shown": want, "sent": want, "toasts": ["Added SPY/FRED:DGS10, EIA:PET.WCESTUS1.W/1000"]}
    assert wl.clean({"lists": [{"id": "main", "name": "Main", "items": want}]})["lists"][0]["items"] == want
