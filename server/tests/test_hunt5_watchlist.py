"""Bug hunt round 5, watchlist: a symbol written longer than 120 characters as a future, lists saved that the rules
refuse now, a save with no answer and the edits made behind it, the merge of two devices' edits within sections, and
the version a start gives out."""
import json
import logging
import shutil

import pytest
from chartsrv import watchlists as wl
from test_hunt_web_ui import app_js, node, run_app_js
from test_redesign_watchlist_version import FIVE, SERVER, THREE
from test_review_fixes import build_market

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
# The lists loaded again, with a newer version than the ones shown (an older one is not taken)
RELOAD = "const reload = (doc) => { server = { ...copy(doc), version: (server?.version ?? 0) + 10 }; w.list.setDoc(copy(server)); };\n"


def test_a_symbol_written_longer_than_120_characters_as_its_future_is_refused_when_added(tmp_path, monkeypatch):
    """A bare 6E is written 6E1! (watchlists._as_future): a formula of 119 characters with 6E in it is longer than 120
    written so. It is refused at once (400), not saved longer, which refused every later save of the lists; one that
    fits is saved as it is read again, so the page's next save, with it in, is taken."""
    m, _ = build_market(tmp_path, monkeypatch)
    long = "+".join(["6E"] * 40)   # 119 characters typed, 199 as 6E1!
    with pytest.raises(ValueError, match="not a symbol"):
        m.set_watchlists({"lists": [{"name": "Mine", "items": ["AAPL", long]}]}, m.wl["version"])
    fits = "+".join(["6E"] * 24)   # 71 typed, 119 as 6E1!
    doc = m.set_watchlists({"lists": [{"name": "Mine", "items": ["AAPL", fits]}]}, m.wl["version"])
    assert doc["lists"][0]["items"] == ["AAPL", "+".join(["6E1!"] * 24)]
    nxt = {**doc, "lists": [{**doc["lists"][0], "items": doc["lists"][0]["items"] + ["MSFT"]}]}
    assert m.set_watchlists(nxt, doc["version"])["lists"][0]["items"] == ["AAPL", "+".join(["6E1!"] * 24), "MSFT"]


def test_saved_lists_with_a_symbol_refused_now_are_kept_not_replaced_by_the_default(tmp_path, monkeypatch, caplog):
    """Lists saved with a symbol the rules refuse now (a formula saved at 199 characters before the check above): the
    next start shows them without it and says so in the log, rather than store config/watchlists.json's list over
    every one of them; the saved setting stays as it is until the lists are next edited. Lists that cannot be read
    at all show the default, and stay saved too."""
    m, _ = build_market(tmp_path, monkeypatch)
    bad = "+".join(["6E1!"] * 40)
    mine = {"lists": [{"id": "main", "name": "Mine", "items": ["###MY", "AAPL", bad, "MSFT"]},
                      {"id": "b", "name": "Second", "items": ["TSLA"]}],
            "flags": {"AAPL": "red"}, "names": {}, "version": 1_800_000_000_000}
    m.store.set_setting("watchlists", mine)
    with caplog.at_level(logging.WARNING, logger="chartsrv.watchlists"):
        doc = wl.load(m.store, m.s.config_dir)
    assert [(lst["id"], lst["name"], lst["items"]) for lst in doc["lists"]] == [
        ("main", "Mine", ["###MY", "AAPL", "MSFT"]), ("b", "Second", ["TSLA"])]
    assert doc["flags"] == {"AAPL": "red"} and doc["version"] > mine["version"]
    assert m.store.get_setting("watchlists")["lists"] == mine["lists"]
    assert "6E1!+6E1!" in caplog.text
    m.wl = doc   # the next edit saves the lists as shown
    saved = m.set_watchlists({**doc, "lists": [{**doc["lists"][0], "items": ["AAPL"]}, doc["lists"][1]]}, doc["version"])
    assert m.store.get_setting("watchlists") == saved and wl.load(m.store, m.s.config_dir)["lists"] == saved["lists"]

    odd = {"lists": [{"id": "x", "name": "N" * 61, "items": ["AAPL"]}], "flags": {}, "names": {}, "version": 5}
    m.store.set_setting("watchlists", odd)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="chartsrv.watchlists"):
        doc = wl.load(m.store, m.s.config_dir)
    shown = lambda d: [(lst["name"], lst["items"]) for lst in d["lists"]]   # noqa: E731
    assert shown(doc) == shown(wl.load_default(m.s.config_dir))
    assert m.store.get_setting("watchlists")["lists"] == odd["lists"] and "a list needs a name" in caplog.text


@needs_node
def test_edits_made_while_a_save_has_no_answer_are_saved_once_it_fails():
    """One save on its way at a time: a save with no answer (a dead connection) is given up after 15 s (app.js api()'s
    timeout); the edits made meanwhile, and its own, are merged into the server's lists (another device's symbol
    added meanwhile) and saved, not thrown away with it. An edit made while the lists are read after a save that
    failed alone is kept too; a save that fails with no edit after it shows the error and the server's lists."""
    out = node(SERVER + RELOAD + f"""
load({json.dumps(FIVE)});
del('MSFT');
const stalled = reqs.shift();                                   // no answer
del('TSLA'); del('NVDA');
await settle();
const waiting = reqs.length;
other((d) => {{ d.lists[0].items.push('META'); return d; }});   // another device's save
await hear();
stalled.reject(new Error('No answer from the server in 15 s'));
await settle(); await drain();
const a = {{ waiting, opts: stalled.opts, shown: shown(), server: server.lists[0].items, fails: fails.splice(0) }};
reload({json.dumps(THREE)});
del('MSFT');
reqs.shift().reject(new Error('Failed to fetch'));
await settle();
const read = reqs.find((r) => r.method === 'GET');
del('TSLA');                                                    // while the lists are read
await drain();
const b = {{ opts: read.opts, shown: shown(), server: server.lists[0].items, fails: fails.splice(0) }};
reload({json.dumps(THREE)});
del('MSFT');
reqs.shift().reject(new Error('Failed to fetch'));
await settle(); await drain();
out({{ a, b, c: {{ shown: shown(), server: server.lists[0].items, fails }} }});""")
    assert out["a"] == {"waiting": 0, "opts": {"timeout": 15000}, "shown": ["AAPL", "AMD", "META"],
                        "server": ["AAPL", "AMD", "META"], "fails": []}
    assert out["b"] == {"opts": {"timeout": 15000}, "shown": ["AAPL"], "server": ["AAPL"], "fails": ["Failed to fetch"]}
    assert out["c"] == {"shown": ["AAPL", "MSFT", "TSLA"], "server": ["AAPL", "MSFT", "TSLA"], "fails": ["Failed to fetch"]}


@needs_node
def test_a_request_given_a_timeout_and_no_answer_is_given_up_with_an_error_that_says_so():
    """app.js api(): with a timeout, a request still unanswered then is aborted (its body too) and fails saying so,
    and one answered in time clears its timer; without one, nothing changes."""
    out = run_app_js(app_js("async function api("), """
const timers = new Map(), signals = [];
let id = 0;
Object.assign(ctx, {
  AbortController, authCheck() {},
  setTimeout: (f, ms) => { timers.set(++id, { f, ms }); return id; }, clearTimeout: (i) => timers.delete(i),
  fetch: (path, opts) => new Promise((resolve, reject) => {
    signals.push(opts.signal ?? null);
    opts.signal?.addEventListener('abort', () => reject(opts.signal.reason));
    if (path === '/ok') resolve({ ok: true, status: 200, json: async () => ({ a: 1 }) });
  }),
});
vm.runInContext(/*APP*/ + ';this.api = api;', ctx);
const hung = ctx.api('PUT', '/hang', { x: 1 }, { timeout: 15000 }).catch((e) => e.message);
const waits = [...timers.values()].map((t) => t.ms);
[...timers.values()][0].f();
const ok = await ctx.api('GET', '/ok', null, { timeout: 15000 });
const plain = await ctx.api('GET', '/ok');
out({ waits, hung: await hung, ok, plain, timers: timers.size, signals: signals.map((s) => s && s.aborted) });""")
    assert out == {"waits": [15000], "hung": "No answer from the server in 15 s", "ok": {"a": 1}, "plain": {"a": 1},
                   "timers": 0, "signals": [True, False, None]}


@needs_node
def test_the_merge_adds_and_removes_the_other_devices_symbols_in_their_own_sections():
    """This page's edit, made on the lists before another device's save, is merged into them (409): each symbol the
    other added or removed is placed in its own section, even one listed in two (the default list's USDJPY, in
    FUTURES and FOREX), one listed here and there, or one moved there to another section; a section renamed here
    still takes the symbols added there at its top."""
    out = node(SERVER + RELOAD + """
const merge = async (items, here, there) => {
  reload({ lists: [{ id: 'main', name: 'Main', items }], flags: {}, names: {} });
  await here();                                                         // sent, made on the version before
  other((d) => { d.lists[0].items = there(d.lists[0].items); return d; });   // saved first
  await drain();
  return server.lists[0].items;
};
const ins = (its, before, x) => { its.splice(its.indexOf(before), 0, x); return its; };
const seed = ['###FUTURES', 'ES1!', 'VIX', 'USDJPY', '###FOREX', 'EURUSD', 'USDJPY', '###CRYPTO', 'BTCUSD'];
const sectionMenu = async (i, label) => {
  body.listeners.click.forEach((f) => f({ target: { closest: (s) => (s === '.sec-more' ? { getBoundingClientRect: () => ({ left: 0, bottom: 0 }) }
    : s === '.wl-sec' ? { dataset: { i: String(i) } } : null) } }));
  menu.find((x) => x?.label === label).fn();
};
const add = async (i, sym) => {   // below row i
  w.click(i);
  w.root.querySelector('#wl-add-in').value = sym;
  w.root.querySelector('#wl-addbox').onsubmit({ preventDefault() {} });
};
out({
  addedAfterACopy: await merge(seed, () => del('VIX'), (its) => ins(its, '###CRYPTO', 'USDCAD')),
  removedACopy: await merge(seed, () => del('VIX'), (its) => { its.splice(its.indexOf('USDJPY'), 1); return its; }),
  renamedHere: await merge(['###INDEX', 'ES1!', 'NQ1!', '###ENERGY', 'CL1!'],
    async () => { await sectionMenu(3, 'Rename section…'); answerAsk('Oil'); await settle(); }, (its) => ins(its, 'CL1!', 'BZ1!')),
  addedOnBoth: await merge(['###A', 'X', '###B', 'Y'], () => add(1, 'Z'), (its) => [...its, 'Z']),
  movedThere: await merge(['###A', 'X', 'Y', '###B', 'Q'], () => del('Q'), () => ['###A', 'X', '###B', 'Q', 'Y']),
  fails,
});""")
    assert out == {
        "addedAfterACopy": ["###FUTURES", "ES1!", "USDJPY", "###FOREX", "EURUSD", "USDJPY", "USDCAD", "###CRYPTO", "BTCUSD"],
        "removedACopy": ["###FUTURES", "ES1!", "###FOREX", "EURUSD", "USDJPY", "###CRYPTO", "BTCUSD"],
        "renamedHere": ["###INDEX", "ES1!", "NQ1!", "###OIL", "BZ1!", "CL1!"],
        "addedOnBoth": ["###A", "X", "Z", "###B", "Y", "Z"],
        "movedThere": ["###A", "X", "###B", "Y"],
        "fails": [],
    }


def test_the_version_a_start_gives_out_is_stored_so_the_next_start_is_above_it_with_the_clock_set_back(tmp_path,
                                                                                                    monkeypatch):
    """A start gives the saved lists a version above the saved one (its time, an hour on): the open pages hold it. The
    clock set back 10 minutes (a fast clock corrected) and the server started again a minute later, with no save
    between: that start's version, and another device's save after it, are still above it, so the pages take the
    reconnect's hello and that save. Lists not read whole (a symbol left out) stay saved as they are, with it."""
    from chartsrv.store import Store
    clock = [1_800_000_000.0]
    monkeypatch.setattr(wl.time, "time", lambda: clock[0])
    store = Store(tmp_path / "c.db")
    plain = [{"id": "a", "name": "Mine", "items": ["AAPL"]}]
    odd = [{"id": "a", "name": "Mine", "items": ["AAPL", "+".join(["6E1!"] * 40)]}]   # a symbol refused now
    for lists in (plain, odd):
        store.set_setting("watchlists", {"lists": lists, "flags": {}, "names": {}, "version": int(clock[0] * 1000)})
        clock[0] += 3600
        held = wl.load(store, tmp_path)["version"]
        assert store.get_setting("watchlists")["version"] == held and store.get_setting("watchlists")["lists"] == lists
        clock[0] -= 600 - 60
        again = wl.load(store, tmp_path)
        assert held < again["version"] < wl.next_version(again["version"]) and again["lists"] == plain
