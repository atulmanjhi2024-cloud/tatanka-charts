"""Bug hunt round 6, watchlist: a saved formula that writing its roots as futures makes too long, the items load()
leaves out kept saved through the start, drawings made on an older bare root's chart, the page told when the symbol
database's future-only roots change, and the Add box's check for a symbol already in its section."""
import json
import shutil

import pytest
from chartsrv.market import Client
from chartsrv.store import Store
from test_hunt_web_chart import run_app
from test_hunt_web_ui import WEB
from test_instruments_edit import _client
from test_review_fixes import build_market

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

# Nasdaq Trader's rows for stocks whose tickers are futures roots too; ZB, ZF, ZT, UB, YM, RTY, ZC, ZW are no stock's
LISTED = [("ZS", "Zscaler", "stock", "NASDAQ", "", 0), ("NG", "NovaGold Resources", "stock", "NYSE American", "", 0),
          ("HE", "Hawaiian Electric Industries", "stock", "NYSE", "", 0), ("AAPL", "Apple", "stock", "NASDAQ", "", 0)]
# a treasury-curve formula typed with bare roots, as the app before quote keys quoted them: 88 characters, 128 with
# each root written as its future
CURVE = "(ZT+ZF+ZN+ZB+UB)/5-(2*ZT+3*ZF+4*ZN+5*ZB+6*UB)/20+(ZT-ZF)*(ZN-ZB)/(UB-ZB)+(ZN-ZF)/(ZB-ZT)"


def edit(doc, items, i=0):
    """The page's save of the lists with list i's items changed."""
    return {**doc, "lists": [{**lst, "items": items} if k == i else lst for k, lst in enumerate(doc["lists"])]}


def test_a_saved_formula_too_long_with_its_roots_as_futures_keeps_them_and_refuses_no_save(tmp_path, monkeypatch):
    """An older list's formula of bare roots that fits 120 characters as saved, not with each written as its future
    (ZB1!): the start writes the list's bare ZB, ZN and YM rows as their futures, and every later save is taken, with
    the formula (and its flag) as saved. The same formula typed now is still refused, as it would be saved longer."""
    m, _ = build_market(tmp_path, monkeypatch)
    m.symbols._put(LISTED, "nasdaq")   # the listings, kept in the database by the daily refresh
    six = "+".join(["6E"] * 40)        # saved before quote keys: 119 characters, 199 as 6E1!
    m.store.set_setting("watchlists", {"lists": [{"id": "r", "name": "Rates", "items": ["###RATES", CURVE, "ZB", "ZN", "YM"]},
                                                 {"id": "f", "name": "FX", "items": [six, "EURUSD"]}],
                                       "flags": {CURVE: "red", "YM": "blue"}, "names": {}, "version": 1_800_000_000_000})
    m.store.close()
    m, _ = build_market(tmp_path, monkeypatch)   # the first start after the update
    rates = ["###RATES", CURVE, "ZB1!", "ZN1!", "YM1!"]
    assert [lst["items"] for lst in m.wl["lists"]] == [rates, [six, "EURUSD"]]
    assert m.wl["flags"] == {CURVE: "red", "YM1!": "blue"} and "YM1!" in m.watch_keys and "YM" not in m.watch_keys
    assert Store(tmp_path / "chart.db").get_setting("watchlists")["lists"] == m.wl["lists"]
    doc = m.set_watchlists(edit(m.wl, [six], 1), m.wl["version"])   # EURUSD deleted
    assert [lst["items"] for lst in doc["lists"]] == [rates, [six]] and doc["flags"][CURVE] == "red"
    for typed in (CURVE.replace("/5-", "/4-"), "+".join(["ZB"] * 40)):   # 88 and 119 characters typed, 128 and 199 saved
        with pytest.raises(ValueError, match="not a symbol"):
            m.set_watchlists(edit(doc, [*rates, typed]), doc["version"])
    m.store.close()
    m, _ = build_market(tmp_path, monkeypatch)   # and the next start
    assert [lst["items"] for lst in m.wl["lists"]] == [rates, [six]]
    assert m.set_watchlists(edit(m.wl, rates[:-1]), m.wl["version"])["lists"][0]["items"] == rates[:-1]


def test_a_formula_saved_before_the_listings_are_in_is_not_refused_once_they_are(tmp_path, monkeypatch):
    """A fresh install: a formula with bare ZB saved before the first listings refresh is taken as typed (ZB was not
    known as a future yet); once the listings are in, ZB1! would make it too long, and the lists' saves are still
    taken, with it as saved, also after a restart."""
    m, _ = build_market(tmp_path, monkeypatch)
    f = "+".join(["ZB"] * 40)   # 119 characters
    doc = m.set_watchlists({"lists": [{"id": "main", "name": "Main", "items": ["AAPL", f]}]}, m.wl["version"])
    assert doc["lists"][0]["items"] == ["AAPL", f]
    m.symbols._put(LISTED, "nasdaq")   # refresh('nasdaq') a few seconds later
    doc = m.set_watchlists(edit(doc, ["AAPL", f, "ZN"]), doc["version"])
    assert doc["lists"][0]["items"] == ["AAPL", f, "ZN1!"]
    m.store.close()
    m, _ = build_market(tmp_path, monkeypatch)
    assert m.wl["lists"][0]["items"] == ["AAPL", f, "ZN1!"]
    assert m.set_watchlists(edit(m.wl, [f, "ZN1!"]), m.wl["version"])["lists"][0]["items"] == [f, "ZN1!"]


def test_drawings_made_on_an_older_bare_roots_chart_show_on_its_futures_chart(tmp_path, monkeypatch):
    """Before quote keys, an older list's bare ZB (or 6E) charted the future, and its drawings were stored as ZB; the
    chart is ZB1! now, and shows them with its own (one whose id it has already is its own), as a future live here
    (NQ) shows those made on its free-data chart (NQ1!) before it ran here. A root the listings show a stock has (ZS)
    keeps its own apart from its future's, as does a live future's (ES, Eversource's too). ZB's before the listings
    are in are ZB's chart's (its chart then), and ZB1!'s once they are, no restart. A start moves none of them."""
    db = Store(tmp_path / "chart.db")   # a fresh install: no listings yet
    rows = [("ZB", "d0"), ("6E", "d1"), ("ZS", "d2"), ("ES", "d3"), ("NQ1!", "d4"), ("AAPL", "d5"), ("M2K", "d6"),
            ("M2K1!", "d6")]
    for i, (sym, d) in enumerate(rows):
        db.put_drawing(sym, {"id": d, "type": "hline", "pts": [{"t": 1, "p": 100.0 + i}], "style": {}})
    db.close()

    def shown(c, sym):
        r = c.get(f"/api/drawings/{sym}")
        return [(d["id"], d["pts"][0]["p"]) for d in r.json()["drawings"]] if r.status_code == 200 else r.status_code
    monkeypatch.setenv("CHART_QUOTES_FEED", "none")   # no Yahoo polls of the network for a stop to wait out
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert {s: shown(c, s) for s in ["ZB", "ZB1!", "6E", "6E1!", "ZS", "ZS1!", "ES", "ES1!", "AAPL", "M2K1!"]} == {
            "ZB": [("d0", 100.0)], "ZB1!": [], "6E": [("d1", 101.0)], "6E1!": [("d1", 101.0)], "ZS": [("d2", 102.0)],
            "ZS1!": [], "ES": [("d3", 103.0)], "ES1!": 404, "AAPL": [("d5", 105.0)], "M2K1!": [("d6", 107.0)]}
        c.portal.call(app.state.market.symbols._put, LISTED, "nasdaq")   # the first listings refresh
        assert {s: shown(c, s) for s in ["ZB", "ZB1!", "ZS", "ZS1!", "NQ"]} == {
            "ZB": [("d0", 100.0)], "ZB1!": [("d0", 100.0)], "ZS": [("d2", 102.0)], "ZS1!": [], "NQ": [("d4", 104.0)]}
    _, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert (shown(c, "ZB1!"), shown(c, "NQ")) == ([("d0", 100.0)], [("d4", 104.0)])
    db = Store(tmp_path / "chart.db")
    assert sorted(tuple(r) for r in db.db.execute("SELECT symbol, id FROM drawings")) == sorted(rows)
    db.close()


def test_a_symbol_load_leaves_out_stays_saved_through_the_start_until_a_list_is_edited(tmp_path, monkeypatch):
    """A formula saved at 199 characters before the length check is left out of the lists shown, and stays saved
    until a list is edited (load()); the start's rewrite of the lists' bare ZB to ZB1! does not save over it, and
    ZB1! is quoted at once. The first edit saves the lists as shown."""
    m, _ = build_market(tmp_path, monkeypatch)
    m.symbols._put(LISTED, "nasdaq")
    bad = "+".join(["6E1!"] * 40)
    mine = {"lists": [{"id": "main", "name": "Mine", "items": ["AAPL", bad, "ZB"]}], "flags": {}, "names": {},
            "version": 1_800_000_000_000}
    m.store.set_setting("watchlists", mine)
    m.store.close()
    m, _ = build_market(tmp_path, monkeypatch)
    assert m.wl["lists"][0]["items"] == ["AAPL", "ZB1!"] and "ZB1!" in m.watch_keys and "ZB" not in m.watch_keys
    assert Store(tmp_path / "chart.db").get_setting("watchlists")["lists"] == mine["lists"]
    doc = m.set_watchlists(edit(m.wl, ["ZB1!", "AAPL"]), m.wl["version"])
    assert Store(tmp_path / "chart.db").get_setting("watchlists") == doc and doc["lists"][0]["items"] == ["ZB1!", "AAPL"]


@needs_node
def test_open_pages_hear_when_the_future_only_roots_change(tmp_path, monkeypatch):
    """A fresh install: a page opened before the first listings refresh has only the built-in set (6E, SR3) from its
    hello. Once the listings are in, the server charts and quotes a bare ZB as ZB1!; every open page is told, so its
    quick switch names it so too (chartSym), not as a stock "ZB" whose quote never comes. A refresh that changes
    nothing sends nothing."""
    m, _ = build_market(tmp_path, monkeypatch)
    c = Client()
    m.clients.add(c)
    hello = m.hello()["only_futures"]
    m.symbols._put(LISTED, "nasdaq")
    m.symbols._put(LISTED, "nasdaq")   # the next day's refresh
    sent = []
    while not c.outbox.empty():
        sent.append(c.outbox.get_nowait())
    assert sent == [{"type": "only_futures", "only_futures": m.hello()["only_futures"]}]
    assert "ZB" in sent[0]["only_futures"] and "ZB" not in hello
    wlsrc = (WEB / "watchlist.js").read_text().split("function createWatchlist")[0]
    out = run_app(["chartSym", "onMessage"], wlsrc + f"""
const S = {{ instruments: {{ ES: {{}} }}, catalog: {{}}, onlyFutures: new Set({json.dumps(hello)}) }};
/*APP*/
const before = chartSym('ZB');
onMessage({json.dumps(sent[0])});
console.log(JSON.stringify([before, ...['ZB', 'zb', 'ZS', '6E', 'ES'].map(chartSym)]));""")
    assert out == ["ZB", "ZB1!", "ZB1!", "ZS", "6E1!", "ES"]


@needs_node
def test_a_bare_root_typed_beside_its_future_is_already_in_the_section(tmp_path, monkeypatch):
    """The Add box with comma-typed symbols (or Enter before the search answers): a bare ZN beside ZN1! is the same
    future, as the server saves it, so it is not added again; YM is added as YM1!, and ZS (Zscaler) as it is. The
    page sends what the server saves."""
    from test_hunt_web_ui import node
    from test_redesign_watchlist_version import SERVER
    m, _ = build_market(tmp_path, monkeypatch)
    m.symbols._put(LISTED, "nasdaq")
    only = json.dumps(m.hello()["only_futures"])
    page = f"const ONLY = new Set({only});\n" + SERVER.replace("const env = {", "const env = { onlyFutures: () => ONLY,", 1)
    out = node(page + """
load({ lists: [{ id: 'main', name: 'Rates', items: ['###RATES', 'ZN1!', 'ZB1!', '###FX', '6E1!'] }], flags: {}, names: {} });
const add = async (text) => {
  w.root.querySelector('#wl-add-in').value = text;
  w.root.querySelector('#wl-addbox').onsubmit({ preventDefault() {} });
  await settle();
};
w.click(1);
await add('zn, ZB');            // both there already: nothing to save
const none = reqs.length;
await add('ZN, zb, YM, ZS, ###ZB');    // a section named ZB stays ZB
out({ none, sent: reqs.find((r) => r.method === 'PUT').body });""")
    sent = out["sent"]["lists"][0]["items"]
    assert out["none"] == 0 and sent == ["###RATES", "ZN1!", "YM1!", "ZS", "###ZB", "ZB1!", "###FX", "6E1!"]
    assert m.set_watchlists({k: v for k, v in out["sent"].items() if k != "base"})["lists"][0]["items"] == sent
