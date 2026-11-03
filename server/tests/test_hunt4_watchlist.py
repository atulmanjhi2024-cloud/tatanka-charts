"""Fixes from round 4 of the bug hunt, in the watchlist and its quotes: web/watchlist.js runs in node (web_harness.js's
makeWatchlist, with test_hunt3_watchlist's server that keeps the document), app.js's chartSym beside watchlist.js's
helpers, and Market's quotes on a real Market."""
import json
import shutil

import pytest
from chartsrv import watchlists as wl
from test_hunt3_watchlist import SERVER
from test_hunt_web_ui import WEB, app_js, node, run_app_js
from test_review_fixes import build_market

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

ENERGY = {"lists": [{"id": "main", "name": "Main", "items": ["###ENERGY", "CL1!", "NG1!", "###METALS", "GC1!", "SI1!"]}],
          "flags": {}, "names": {}}


def test_a_lowercase_symbol_with_a_colon_is_kept_in_capitals_and_quoted():
    """'spy/fred:dgs10', 'nasdaq:aapl', 'cme_mini:es1!': every quote is keyed in capitals (Yahoo, IBKR, Alpaca, the
    NinjaTrader add-on's futures), so the whole item is, not only its FRED:, EIA: or DATA: series. A flag set on the
    lowercase one before stays on it."""
    doc = wl.clean({"lists": [{"id": "main", "name": "Main", "items": ["spy/fred:dgs10", "nasdaq:aapl", "cme_mini:es1!"]}],
                    "flags": {"aapl": "red"}})
    assert doc["lists"][0]["items"] == ["SPY/FRED:DGS10", "NASDAQ:AAPL", "CME_MINI:ES1!"]
    assert wl.quote_keys(doc) == {"SPY", "FRED:DGS10", "AAPL", "ES1!"} and wl.futures_roots(doc) == ["ES"]
    assert doc["flags"] == {"AAPL": "red"}


@needs_node
def test_a_lowercase_symbol_with_a_colon_typed_in_the_add_box_shows_its_quote():
    out = node("""
const q = (s, last, prev, src) => ({ s, last, prev, chg: last - prev, pct: (last - prev) / prev * 100, ts: 1, rt: 'delayed', src });
const quotes = { SPY: q('SPY', 410, 400, 'yahoo'), 'FRED:DGS10': q('FRED:DGS10', 4.1, 4, 'fred'), AAPL: q('AAPL', 250, 245, 'yahoo') };
const w = makeWatchlist(WEB, { quotes, env: { api: async (m, p, b) => JSON.parse(JSON.stringify(b)) } });
w.list.setDoc({ lists: [{ id: 'main', name: 'Main', items: [] }], flags: {}, names: {} });
w.root.querySelector('#wl-add-in').value = 'spy/fred:dgs10, nasdaq:aapl';
w.root.querySelector('#wl-addbox').onsubmit({ preventDefault() {} });
await settle();
out(w.root.querySelector('.wl-body').children.map((r) => [r.querySelector('.tk').textContent, r.querySelector('.last').textContent]));""")
    assert out == [["SPY/FRED:DGS10", "100.00"], ["AAPL", "250.00"]]


@needs_node
def test_quick_switch_charts_a_lowercase_fred_eia_or_data_series_from_its_source():
    """Typed in the quick switch (or Compare) with no search result picked: 'fred:t10y2ym' is FRED's series, not the
    Yahoo ticker T10Y2YM ('fred:' taken for an exchange prefix)."""
    typed = ["fred:t10y2ym", "Fred:DGS10", "eia:pet.wcestus1.w", "data:goldoil", "FRED:T10Y2YM", "nasdaq:aapl", "aapl",
             "cme_mini:es1!", "rty1!", "spy/qqq"]
    out = run_app_js(app_js("function chartSym("), f"""
const wl = require('fs').readFileSync({json.dumps(str(WEB / "watchlist.js"))}, 'utf8').split('function createWatchlist')[0];
vm.runInContext(wl + '\\nvar S = {{ instruments: {{ ES: {{}} }}, catalog: {{ RTY: {{}} }} }};\\n' + /*APP*/ + '\\nthis.chartSym = chartSym;', ctx);
out({json.dumps(typed)}.map((v) => ctx.chartSym(v)));""")
    assert out == ["FRED:T10Y2YM", "FRED:DGS10", "EIA:PET.WCESTUS1.W", "DATA:GOLDOIL", "FRED:T10Y2YM", "AAPL", "AAPL",
                   "ES", "RTY1!", None]


@needs_node
def test_add_symbol_here_goes_into_its_section_when_the_list_reloads_while_the_box_is_open():
    """Section menu > Add symbol here… on ENERGY, then a save from another device before the symbol is picked: it
    goes at the end of ENERGY as the list is now. ENERGY gone meanwhile, or another list shown: where an add goes
    without it (below the selected row, else at the end)."""
    out = node(SERVER + f"""
const doc = () => copy({json.dumps(ENERGY)});
const addHere = async (now, sym, other) => {{
  load(doc());
  body.listeners.click.forEach((f) => f({{ target: {{ closest: (s) => (s === '.sec-more' ? {{ getBoundingClientRect: () => ({{ left: 0, bottom: 0 }}) }}
    : s === '.wl-sec' ? {{ dataset: {{ i: '0' }} }} : null) }} }}));   // ENERGY's ⋯
  await menu.find((x) => x?.label === 'Add symbol here…').fn();
  load(now(doc()));   // another device's save while the box is open
  if (other) pick(other);
  w.root.querySelector('#wl-add-in').value = sym;
  w.root.querySelector('#wl-addbox').onsubmit({{ preventDefault() {{}} }});
  await answerAll();
  return server.lists.map((l) => l.items);
}};
const two = (d) => {{ d.lists.push({{ id: 'b', name: 'Stocks', items: ['###ENERGY', 'XOM', '###TECH', 'AAPL'] }}); return d; }};
out({{
  removed: await addHere((d) => {{ d.lists[0].items.splice(1, 1); return d; }}, 'HO1!'),
  added: await addHere((d) => {{ d.lists[0].items.splice(1, 0, 'XLE', 'XOM'); return d; }}, 'HO1!'),
  same: await addHere((d) => d, 'HO1!'),
  gone: await addHere((d) => {{ d.lists[0].items.splice(0, 3); return d; }}, 'HO1!'),
  switched: await addHere(two, 'CVX', 'Stocks'),
}});""")
    assert out == {"removed": [["###ENERGY", "NG1!", "HO1!", "###METALS", "GC1!", "SI1!"]],
                   "added": [["###ENERGY", "XLE", "XOM", "CL1!", "NG1!", "HO1!", "###METALS", "GC1!", "SI1!"]],
                   "same": [["###ENERGY", "CL1!", "NG1!", "HO1!", "###METALS", "GC1!", "SI1!"]],
                   "gone": [["###METALS", "GC1!", "SI1!", "HO1!"]],
                   "switched": [ENERGY["lists"][0]["items"], ["###ENERGY", "XOM", "###TECH", "AAPL", "CVX"]]}


@needs_node
def test_a_drop_after_another_devices_save_during_the_drag_keeps_that_save():
    """A save from another device arrives while a row is dragged (it waits for the drop): the drop makes the move in
    that list, its rows found again by name, instead of saving the old list with the move over it. The dragged row
    removed there meanwhile: that list as it is, nothing saved."""
    out = node(SERVER + f"""
const doc = () => copy({json.dumps(ENERGY)});
const dragDrop = async (i, y, now) => {{
  load(doc());
  sent.length = 0;
  const row = {{ dataset: {{ i: String(i) }} }}, target = {{ closest: (s) => (s === '.wl-row, .wl-sec' ? row : null) }};
  body.listeners.pointerdown.forEach((f) => f({{ target, button: 0, pointerType: 'mouse', clientY: 100, pointerId: 1, preventDefault() {{}} }}));
  w.win.pointermove.forEach((f) => f({{ pointerId: 1, clientY: y, preventDefault() {{}} }}));   // y 200: to the end; -1: to the top
  load(now(doc()));   // the other device's save, mid-drag
  w.win.pointerup.forEach((f) => f({{ pointerId: 1 }}));
  await answerAll();
  return {{ server: server.lists[0].items, shown: shown(), saves: sent.length }};
}};
out({{
  plain: await dragDrop(1, 200, (d) => d),
  added: await dragDrop(1, 200, (d) => {{ d.lists[0].items.splice(1, 0, 'XLE'); return d; }}),
  top: await dragDrop(4, -1, (d) => {{ d.lists[0].items.unshift('SPY'); return d; }}),
  removed: await dragDrop(1, 200, (d) => {{ d.lists[0].items.splice(1, 1); return d; }}),
}});""")
    assert out["plain"]["server"] == ["###ENERGY", "NG1!", "###METALS", "GC1!", "SI1!", "CL1!"]
    assert out["added"]["server"] == ["###ENERGY", "XLE", "NG1!", "###METALS", "GC1!", "SI1!", "CL1!"]
    assert out["top"]["server"] == ["SPY", "GC1!", "###ENERGY", "CL1!", "NG1!", "###METALS", "SI1!"]
    assert out["removed"] == {"server": ["###ENERGY", "NG1!", "###METALS", "GC1!", "SI1!"], "shown": ["NG1!", "GC1!", "SI1!"],
                              "saves": 0}


def test_a_series_whose_value_before_is_0_shows_its_change(tmp_path, monkeypatch):
    """FRED:USREC 0 after 0, FRED:T10Y2Y from 0.00 to 0.05: a change (no %, from 0), and 'Before 0'. A price's previous
    close of 0 is still none: no feed's price is 0."""
    m, _ = build_market(tmp_path, monkeypatch)
    m.on_quote("FRED:USREC", 0.0, 0.0, None, source="fred", at=1)
    m.on_quote("FRED:T10Y2Y", 0.05, 0.0, None, source="fred", at=1)
    m.on_quote("DATA:SPREAD", -0.25, 0.0, None, source="data", at=1)
    m.on_quote("AAPL", 250.0, 0.0, None, source="ibkr")
    got = {k: (q["prev"], q["chg"], q["pct"]) for k, q in m.quotes.items()}
    assert got["FRED:USREC"] == (0.0, 0.0, None) and got["FRED:T10Y2Y"] == (0.0, 0.05, None)
    assert got["DATA:SPREAD"] == (0.0, -0.25, None) and got["AAPL"] == (None, None, None)


def test_quotes_no_list_or_chart_needs_are_dropped(tmp_path, monkeypatch):
    """A symbol taken off the lists, or a free-data chart closed (its CHART_QUOTE_S over): its quote leaves with it,
    so hello does not send it and it does not come back stale when the symbol is listed again."""
    m, _ = build_market(tmp_path, monkeypatch)
    m.set_watchlists({"lists": [{"name": "a", "items": ["AAPL", "MSFT"]}]})
    m.watch_chart("ORCL")
    for k in ("AAPL", "MSFT", "ORCL", "ES"):
        m.on_quote(k, 100.0, 99.0, None, source="yahoo")
    m.chart_keys["ORCL"] = 0   # the chart closed long ago
    m.set_watchlists({"lists": [{"name": "a", "items": ["MSFT"]}]})
    assert set(m.quotes) == {"MSFT", "ES1!"} and set(m.quote_from) == {"MSFT", "ES1!"} and m.quotes_dirty <= {"MSFT", "ES1!"}
    assert {q["s"] for q in m.hello()["quotes"]} == {"MSFT", "ES1!"}
    m.set_watchlists({"lists": [{"name": "a", "items": ["MSFT", "AAPL"]}]})
    assert "AAPL" not in m.quotes and "AAPL" in m.watch_keys
