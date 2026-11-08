"""Formula charts ("VIXM/VIXY", "VIX9D/VIX", "ES1!/NQ1!"): the watchlist's formulas, charted from their parts' bars
(formula.py). A regime filter a trader posted (VIXM/VIXY with an 8/21 EMA cross) is the case: one series that takes
every indicator and drawing, as TradingView charts a spread."""
import json
import math
import subprocess
import time

import pytest

from chartsrv.bars import parse_spec
from chartsrv.formula import calendar_key, canonical, combine, compile_formula, decimals_for, is_formula, symbols
from test_amt import client  # noqa: F401  (the app on the simulator, as a fixture)
from test_hunt_web_chart import WEB, app_part, needs_node


def test_formulas_read_as_the_watchlist_reads_them():
    v = {"VIXM": 15.0, "VIXY": 20.0, "GC1!": 2000.0, "SI1!": 25.0, "VIX": 20.0, "A": 1.0, "B": 0.0}
    assert compile_formula("VIXM/VIXY")(v) == 0.75
    assert compile_formula("(GC1!/SI1!)*100")(v) == 8000
    assert compile_formula("-VIX+30")(v) == 10
    assert compile_formula("2*VIXM-VIXY/4")(v) == 25
    assert math.isnan(compile_formula("A/B")(v)) and math.isnan(compile_formula("A/C")(v))   # by 0; a part with none
    for bad in ("VIXM/", "(A+B", "A B", "A*/B", "A+B)", "A%B", ""):
        with pytest.raises(ValueError):
            compile_formula(bad)
    assert symbols("ES1!/NQ1!+ES1!") == ["ES1!", "NQ1!"] and is_formula("VIXM/VIXY") and not is_formula("CME_MINI:ES1!")
    # six significant digits, two at least: a ratio's small moves show
    assert [decimals_for(x) for x in (0.7512, 712.4, 0.0123, 6800.0, 4e-5, 0.0, -0.27)] == [6, 3, 7, 2, 10, 2, 6]
    assert canonical("ES/NQ1!+es", {"ES": 1, "NQ": 1}) == "ES1!/NQ1!+es" and canonical("FRED:ES-SPY", {"ES": 1}) == "FRED:ES-SPY"


def test_the_first_symbol_sets_the_times_and_the_others_count_as_of_them():
    """A stock's 09:30 hour against a future's 09:00 one, a holiday one side has: the other side's bar opening then,
    else its last close; none before both have one. High and low are the open and the close."""
    a = [{"t": 1, "o": 10, "c": 11}, {"t": 2, "o": 11, "c": 12}, {"t": 3, "o": 12, "c": 13}]
    b = [{"t": 2, "o": 4, "c": 5}, {"t": 3.5, "o": 6, "c": 7}]
    got = combine(compile_formula("A/B"), ["A", "B"], {"A": a, "B": b})
    assert got == [{"t": 2, "o": 2.75, "h": 2.75, "l": 2.4, "c": 2.4, "v": 0},     # B's own bar at 2
                   {"t": 3, "o": 2.4, "h": 2.6, "l": 2.4, "c": 2.6, "v": 0}]      # B as of 3: its close at 2


def test_an_open_never_takes_a_close_from_after_it():
    """GC's RTH hours start at :20, SI's (free data) at :00: at GC's 08:20 open, SI's 08:00 hour is still running, so
    its value then is its 07:00 hour's close, not the 08:00 hour's (known at 09:00). At GC's close (09:20) the 08:00
    hour is over: its close."""
    H, M20 = 3_600_000, 20 * 60_000
    gc = [{"t": 8 * H + M20, "o": 3500.0, "c": 3510.0}, {"t": 9 * H + M20, "o": 3510.0, "c": 3520.0}]
    si = [{"t": 7 * H, "o": 40.0, "c": 41.0}, {"t": 8 * H, "o": 41.0, "c": 42.5}, {"t": 9 * H, "o": 42.5, "c": 44.0}]
    got = combine(compile_formula("GC/SI"), ["GC", "SI"], {"GC": gc, "SI": si}, H)
    assert [(b["o"], b["c"]) for b in got] == [(3500 / 41.0, 3510 / 42.5), (3510 / 42.5, 3520 / 44.0)]
    first = combine(compile_formula("GC/SI"), ["GC", "SI"], {"GC": gc, "SI": si[1:]}, H)   # nothing of SI over yet
    assert first[0]["o"] == 3500 / 41.0                      # the open of the hour running (known at 08:00)


def test_daily_bars_meet_by_their_trade_date():
    """A live RTH day is stamped at its RTH open (GC 07:20 CT), a free-data or all-hours day at 17:00 CT the evening
    before: by time GC's Friday would meet ES's Thursday. By trade date they meet; weeks and months too."""
    from datetime import datetime
    from chartsrv.sessions import CT
    at = lambda d, h, m=0: int(datetime(2026, 10, d, h, m, tzinfo=CT).timestamp() * 1000)   # noqa: E731
    gc = [{"t": at(15, 7, 20), "o": 3500.0, "c": 3510.0}, {"t": at(16, 7, 20), "o": 3510.0, "c": 3520.0}]
    es = [{"t": at(14, 17), "o": 6900.0, "c": 6910.0}, {"t": at(15, 17), "o": 6910.0, "c": 6920.0}]
    got = combine(compile_formula("GC1!/ES1!"), ["GC1!", "ES1!"], {"GC1!": gc, "ES1!": es}, 0, calendar_key(parse_spec("1D")))
    assert [(b["o"], b["c"]) for b in got] == [(3500 / 6900, 3510 / 6910), (3510 / 6910, 3520 / 6920)]
    wk = calendar_key(parse_spec("1W"))
    assert wk(at(11, 17)) == wk(at(16, 7, 20)) != wk(at(18, 17))   # Sunday evening and Friday: one week; Sunday next the next
    assert calendar_key(parse_spec("5m")) is None


def test_a_formula_of_two_live_futures_charts_from_their_own_series(in_session, client):  # noqa: F811
    for _ in range(100):   # the simulator's first hour written
        r = client.get("/api/ext/bars", params={"symbol": "es1!/nq1!", "spec": "1m"})
        if r.status_code == 200 and len(r.json()["bars"]) > 20:
            break
        time.sleep(0.2)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["source"] == "formula" and d["symbol"] == "ES1!/NQ1!" and d["parts"] == ["ES1!", "NQ1!"]
    assert d["decimals"] == 6 and len(d["bars"]) > 20 and d["bars"][-1]["seq"] == len(d["bars"])
    assert all(0.2 < b["c"] < 0.4 and b["l"] <= min(b["o"], b["c"]) and b["h"] >= max(b["o"], b["c"]) for b in d["bars"])
    bad = {"ES1!/": "ends too soon", "ES1!/(NQ1!-NQ1!)": "division by 0", "ZZZZ9!/ES1!": "ZZZZ9!"}
    for sym, says in bad.items():
        r = client.get("/api/ext/bars", params={"symbol": sym, "spec": "1m"})
        assert r.status_code == 400 and says in r.json()["detail"], (sym, r.text)
    r = client.get("/api/ext/bars", params={"symbol": "ES1!/NQ1!", "spec": "2000t"})
    assert r.status_code == 400 and "time bars" in r.json()["detail"]


def test_drawings_on_a_formula_chart(client):  # noqa: F811
    """A formula's / comes as ~ in the drawings URL: an encoded / (%2F) would split the path all the same."""
    d = {"type": "hline", "pts": [{"t": 1_800_000_000_000, "p": 0.27}]}
    assert client.put("/api/drawings/ES1!~NQ1!/f1", json=d).status_code == 200
    assert [x["id"] for x in client.get("/api/drawings/ES1!~NQ1!").json()["drawings"]] == ["f1"]
    assert client.get("/api/drawings/ES").json()["drawings"] == []   # not ES's
    assert client.delete("/api/drawings/ES1!~NQ1!/f1").status_code == 204
    assert client.get("/api/drawings/ES1!~NQ1!").json()["drawings"] == []


@needs_node
def test_the_chart_takes_a_typed_formula_and_moves_its_last_bar_from_its_parts_quotes():
    wl = (WEB / "watchlist.js").read_text()
    wl = wl[:wl.index("function createWatchlist")]
    js = wl + "\n" + "\n".join(app_part(p) for p in ("FORMULA_SYM", "FORMULA_MAX", "formulaOk", "quickFormula", "chartSym", "symQuote")) + """
const S = { instruments: { ES: {}, NQ: {} }, onlyFutures: new Set(['6E', '6B']), catalog: {},
  quotes: { 'ES1!': { s: 'ES1!', last: 6900, prev: 6800, ts: 5 }, 'NQ1!': { s: 'NQ1!', last: 25000, prev: 25600, ts: 9 },
            VIXM: { last: 15, prev: 15, ts: 1 }, VIXY: { last: 20, prev: 20, ts: 2, src: 'sim' } } };
console.log(JSON.stringify({
  typed: ['es1!/nq1!', 'VIXM / VIXY', '6E/6B', 'VIXM/', '(A+B', 'ES', 'AAPL', 'es/nq', '(VIXM+VIXY(', 'VIXM+)', 'VIXM+*',
          'VIXM*)', 'VIXM-/', 'A+B+C+D+E+F+G+H+I+J', 'A+B+C+D+E+F+G+H+I+J+K', '-(A)*2'].map(chartSym),
  quick: ['nasdaq-100', 'e-mini', 'es1!-nq1!', 'fred:dgs10-fred:dgs2', 'vixm-vixy', 'spy/qqq', 'aapl+msft'].map(quickFormula),
  es_nq: symQuote('ES1!/NQ1!'), vix: symQuote('VIXM/VIXY'), missing: symQuote('VIXM/SPY') }));
"""
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert out["typed"] == ["ES1!/NQ1!", "VIXM/VIXY", "6E1!/6B1!", None, None, "ES", "AAPL", "ES1!/NQ1!", None, None, None,
                            None, None, "A+B+C+D+E+F+G+H+I+J", None, "-(A)*2"]   # the server's grammar; up to 10 symbols
    # hyphenated words with a search result to take instead are not a formula; known parts, or other operators, are
    assert out["quick"] == [False, False, True, True, True, True, True]
    q = out["es_nq"]
    assert q["last"] == 6900 / 25000 and q["prev"] == 6800 / 25600 and q["ts"] == 9 and q["src"] == "formula"
    assert out["vix"]["src"] == "sim" and out["missing"] is None   # a simulated part; a part with no quote
