"""The watchlist document's version: the server numbers each save of the lists (it only grows, across restarts and
restores too), a save made on an older version than the server's is refused (409) and the page merges its edits into
the newer lists, and the page keeps the newest lists it has heard of, so a broadcast that lags behind an answer does
not roll the list back, and another device's save made during this page's is shown and kept. Pages from before the
version (no base in their PUT) still save, last write wins. web/watchlist.js runs in node (web_harness.js's
makeWatchlist) against a server that numbers its saves as Market.set_watchlists does."""
import json
import shutil

import pytest
from chartsrv import watchlists as wl
from test_hunt_web_ui import node
from test_review_fixes import build_market

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

# The server: requests for the lists wait in `reqs` (with the options they were sent with) until handle(); then each
# answer waits in `http` (answer()) and each save's broadcast in `sock` (hear()), so a test lets them arrive in any
# order, as an HTTP answer and the socket can. A PUT with a base other than the server's version is refused (409);
# without one, last write wins. other(): another device's save, made on the lists as the server has them.
SERVER = """
let server = null, menu = null, answerAsk = null;
const reqs = [], http = [], sock = [], fails = [], copy = (d) => JSON.parse(JSON.stringify(d));
const env = {
  api: (method, path, body, opts) => (path !== '/api/watchlists' ? Promise.resolve({})   // the details panel's
    : new Promise((resolve, reject) => reqs.push({ method, body: body && copy(body), opts, resolve, reject }))),
  ask: () => new Promise((r) => { answerAsk = r; }),
  menu: (x, y, items) => { menu = items; },
  toast() {}, fail: (e) => fails.push(e.message),
};
const w = makeWatchlist(WEB, { env });
const body = w.root.querySelector('.wl-body'), scroll = w.root.querySelector('.wl-scroll');
const shown = () => body.children.filter((e) => e.className === 'wl-row').map((e) => e.querySelector('.tk').textContent);
const press = (key) => scroll.listeners.keydown.forEach((f) => f({ key, target: { closest: () => null }, preventDefault() {}, stopPropagation() {} }));
const del = (sym) => {   // the row's item index: the list's sections are items too
  w.click(Number(body.children.find((e) => e.className === 'wl-row' && e.querySelector('.tk').textContent === sym).dataset.i));
  press('Delete');
};
const pick = (label) => { w.root.querySelector('#wl-name').onclick(); menu.find((x) => x?.label === label).fn(); };
const saved = (doc) => {
  server = { lists: doc.lists, flags: doc.flags || {}, names: doc.names || {}, version: server.version + 1 };
  sock.push(copy(server));
};
const handle = () => {
  while (reqs.length) {
    const r = reqs.shift();
    if (r.method === 'GET') { const d = copy(server); http.push(() => r.resolve(d)); }
    else if (r.body.base != null && r.body.base !== server.version) {
      http.push(() => r.reject(Object.assign(new Error('The lists changed meanwhile'), { status: 409 })));
    } else { saved(r.body); const d = copy(server); http.push(() => r.resolve(d)); }
  }
};
const answer = async () => { http.shift()(); await settle(); };
const hear = async () => { w.list.setDoc(sock.shift()); await settle(); };
const other = (fn) => saved(fn(copy(server)));
const drain = async () => {
  while (reqs.length || http.length || sock.length) { handle(); if (http.length) await answer(); else if (sock.length) await hear(); }
};
const load = (doc) => { server = { ...copy(doc), version: 1 }; w.list.setDoc(copy(server)); };
"""

FIVE = {"lists": [{"id": "main", "name": "Main", "items": ["AAPL", "MSFT", "TSLA", "NVDA", "AMD"]}], "flags": {}, "names": {}}
THREE = {"lists": [{"id": "main", "name": "Main", "items": ["AAPL", "MSFT", "TSLA"]}], "flags": {}, "names": {}}


@needs_node
def test_a_broadcast_that_arrives_after_a_newer_answer_does_not_roll_the_list_back():
    """Two rows deleted, both saves answered before their broadcasts arrive (the socket behind, busy with quotes): the
    first save's broadcast is older than the list here, so the deleted row does not come back, and a delete made then
    does not save it back either."""
    out = node(SERVER + f"""
load({json.dumps(FIVE)});
del('MSFT');
del('TSLA');                   // while the first save is on its way
const seen = [];
while (reqs.length || http.length) {{ handle(); if (http.length) await answer(); seen.push(shown()); }}
await hear();                  // the first save's broadcast, late
seen.push(shown());
del('NVDA');
await drain();
out({{ seen, shown: shown(), server: server.lists[0].items }});""")
    assert all(s == ["AAPL", "NVDA", "AMD"] for s in out["seen"])
    assert out["shown"] == ["AAPL", "AMD"] and out["server"] == ["AAPL", "AMD"]


@needs_node
def test_another_devices_save_made_while_this_pages_is_on_its_way_is_shown_and_kept():
    """This page's save reaches the server, another device saves after it (a symbol added), and both broadcasts arrive
    before this page's answer: once answered, the page shows the other device's lists (the newest), and its next edit
    keeps that symbol."""
    out = node(SERVER + f"""
load({json.dumps(THREE)});
del('MSFT');
handle();                                                       // the server saves it
other((d) => {{ d.lists[0].items.push('META'); return d; }});   // then another device's save
await hear(); await hear();                                     // both broadcasts, before this page's answer
await answer();
const after = shown();
del('TSLA');
await drain();
out({{ after, shown: shown(), server: server.lists[0].items }});""")
    assert out["after"] == ["AAPL", "TSLA", "META"]
    assert out["shown"] == ["AAPL", "META"] and out["server"] == ["AAPL", "META"]


@needs_node
def test_a_save_refused_because_another_device_saved_first_is_merged_into_its_lists():
    """Another device saves first (a symbol added); this page's save, made on the version before, is refused (409):
    the page merges its delete into the newer lists and saves that, with no error shown. Both edits are kept."""
    out = node(SERVER + f"""
load({json.dumps(THREE)});
del('MSFT');                                                    // sent, made on version 1
other((d) => {{ d.lists[0].items.push('META'); return d; }});   // saved first: version 2
await drain();
out({{ shown: shown(), server: server.lists[0].items, version: server.version, fails }});""")
    assert out == {"shown": ["AAPL", "TSLA", "META"], "server": ["AAPL", "TSLA", "META"], "version": 3, "fails": []}


@needs_node
def test_the_merge_keeps_both_devices_edits():
    """This page deletes a symbol, renames a list and deletes another list (three edits, one save on its way at a
    time); another device saved first: a symbol added where the deleted one was and one removed, a list renamed, a
    flag changed, the list deleted here edited there, a list added and an untouched one deleted. The merge keeps
    every edit: a list edited on one device and deleted on the other stays."""
    base = {"lists": [{"id": "a", "name": "Futures", "items": ["###INDEX", "ES1!", "NQ1!", "###ENERGY", "CL1!"]},
                      {"id": "b", "name": "Stocks", "items": ["AAPL", "MSFT"]},
                      {"id": "c", "name": "Crypto", "items": ["BTCUSD"]},
                      {"id": "e", "name": "Old", "items": ["SPY"]}],
            "flags": {"AAPL": "red", "MSFT": "blue"}, "names": {}}
    out = node(SERVER + f"""
load({json.dumps(base)});
del('NQ1!');
pick('Rename…'); answerAsk('Index futures'); await settle();
pick('Old'); pick('Delete list…'); answerAsk(''); await settle();
other((d) => {{
  const [a, b, c, e] = d.lists;
  a.items = ['###INDEX', 'ES1!', 'NQ1!', 'RTY1!', '###ENERGY'];
  b.name = 'US stocks';
  e.items.push('QQQ');
  d.lists = [a, b, e, {{ id: 'd', name: 'Rates', items: ['ZN1!'] }}];
  d.flags.MSFT = 'green';
  return d;
}});
await drain();
out({{ lists: server.lists, flags: server.flags, fails }});""")
    assert out["lists"] == [{"id": "a", "name": "Index futures", "items": ["###INDEX", "ES1!", "RTY1!", "###ENERGY"]},
                            {"id": "b", "name": "US stocks", "items": ["AAPL", "MSFT"]},
                            {"id": "e", "name": "Old", "items": ["SPY", "QQQ"]},
                            {"id": "d", "name": "Rates", "items": ["ZN1!"]}]
    assert out["flags"] == {"AAPL": "red", "MSFT": "green"} and out["fails"] == []


@needs_node
def test_a_merge_while_a_row_is_dragged_ends_the_drag_rather_than_move_another_row():
    """A row dragged while this page's save is refused (another device saved first, a symbol added at the top): the
    merged lists are shown and the drag ends there, so its drop moves nothing, rather than the row now where the
    dragged one was."""
    out = node(SERVER + """
load({ lists: [{ id: 'main', name: 'Main', items: ['AAPL', 'MSFT', 'TSLA', 'NVDA'] }], flags: {}, names: {} });
del('MSFT');                                                       // sent, made on version 1
other((d) => { d.lists[0].items.unshift('META'); return d; });     // saved first
const row = { dataset: { i: '1' } }, target = { closest: (s) => (s === '.wl-row, .wl-sec' ? row : null) };   // TSLA
body.listeners.pointerdown.forEach((f) => f({ target, button: 0, pointerType: 'mouse', clientY: 100, pointerId: 1, preventDefault() {} }));
w.win.pointermove.forEach((f) => f({ pointerId: 1, clientY: 200, preventDefault() {} }));   // to the end
await drain();
w.win.pointerup.forEach((f) => f({ pointerId: 1 }));
await drain();
out({ shown: shown(), server: server.lists[0].items });""")
    assert out == {"shown": ["META", "AAPL", "TSLA", "NVDA"], "server": ["META", "AAPL", "TSLA", "NVDA"]}


class Socket:
    def __init__(self):
        self.msgs = []

    def put(self, msg):
        self.msgs.append(msg)


def test_each_save_has_a_newer_version_in_its_answer_broadcast_and_hello(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    c = Socket()
    m.clients.add(c)
    v0 = m.wl["version"]
    assert isinstance(v0, int) and m.hello()["watchlists"]["version"] == v0
    doc = m.set_watchlists({"lists": [{"name": "a", "items": ["AAPL"]}]}, base=v0)
    assert doc["version"] > v0 and c.msgs[-1] == {"type": "watchlists", "watchlists": doc}
    again = m.set_watchlists({"lists": [{"name": "a", "items": ["AAPL", "MSFT"]}]})   # no base: a page from before
    assert again["version"] > doc["version"] and m.hello()["watchlists"] == again
    assert m.store.get_setting("watchlists")["version"] == again["version"]


def test_a_save_made_on_an_older_version_is_refused_and_one_without_a_base_still_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as client:
        doc = client.get("/api/watchlists").json()
        v1 = doc["version"]
        first = client.put("/api/watchlists", json={**doc, "lists": [{"name": "A", "items": ["AAPL"]}], "base": v1})
        assert first.status_code == 200 and first.json()["version"] > v1
        stale = client.put("/api/watchlists", json={**doc, "lists": [{"name": "B", "items": ["MSFT"]}], "base": v1})
        assert stale.status_code == 409 and client.get("/api/watchlists").json() == first.json()
        for bad in ("1", True, 1.5):
            assert client.put("/api/watchlists", json={**doc, "base": bad}).status_code == 400
        # A page from before the version sends back the document it was given (an old version in it), no base.
        old = client.put("/api/watchlists", json={**doc, "lists": [{"name": "C", "items": ["TSLA"]}]})
        assert old.status_code == 200 and old.json()["lists"][0]["items"] == ["TSLA"]
        assert old.json()["version"] > first.json()["version"]
        assert client.put("/api/watchlists", json={**old.json(), "base": old.json()["version"]}).status_code == 200


def test_the_version_only_grows_across_restarts_restores_and_a_clock_set_back(tmp_path, monkeypatch):
    """Lists saved by an earlier version (none) get one; a restart, or a backup restored (its lists saved with an older
    version), loads a version above any the open pages were given, so they take the lists their reconnect brings. The
    clock set back, each save's is still above the one before."""
    m, _ = build_market(tmp_path, monkeypatch)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(wl.time, "time", lambda: clock[0])
    m.store.set_setting("watchlists", {"lists": [{"id": "x", "name": "Old", "items": ["SPY"]}], "flags": {}, "names": {}})
    m.wl = wl.load(m.store, m.s.config_dir)
    assert m.wl["lists"][0]["items"] == ["SPY"] and isinstance(m.wl["version"], int)
    clock[0] += 60
    saved = m.set_watchlists({"lists": [{"name": "a", "items": ["AAPL"]}]})
    assert saved["version"] > m.wl["version"] - 1 and m.store.get_setting("watchlists") == saved
    m.store.set_setting("watchlists", {**saved, "version": saved["version"] - 30_000})   # a backup from before, restored
    clock[0] += 60                                                                       # and the server started again
    restored = wl.load(m.store, m.s.config_dir)
    assert restored["version"] > saved["version"]
    m.wl = restored
    clock[0] -= 3600
    a = m.set_watchlists({"lists": [{"name": "a", "items": ["MSFT"]}]})
    b = m.set_watchlists({"lists": [{"name": "a", "items": ["TSLA"]}]})
    assert restored["version"] < a["version"] < b["version"]
