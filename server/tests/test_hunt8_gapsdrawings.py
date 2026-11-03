"""Round 8 of the bug hunt, the round-7 gap alert and free-data drawing fixes: Gap Analysis' "Apply to all charts"
gives the server's gap alerts the new settings as OK does; a line deleted and put back with Ctrl+Z on the free-data
chart of a future taken off Instruments (NQ1!) gets its alert back; the alerts kept for an instrument taken off
Instruments run again once it is put back, without a restart; and a Gap settings save while it is off reaches its
gap alerts too."""
import json
import shutil

import pytest

from chartsrv.models import Tick
from test_hunt6_watchlist import LISTED
from test_hunt7_drawings import T0, _client, ids, line, line_alerts, set_instruments, stored
from test_instruments_edit import file_rows

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
GAP = {"new": True, "fill": True, "RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 0.0}
GAP_SET = {"EnabledAnalysis": True, "RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 0, "AlertOnNewGap": True,
           "AlertOnGapFill": True, "days": 90}   # a chart's S.gapSet


# ---- k-gd8-1: Gap Analysis' "Apply to all charts" makes the server's gap alerts follow ---------------------------------

APPLY_ALL_TAIL = """
const tick = (key, name, on) => { const c = ctl(key, name); c.checked = on; c.onchange(); };
S.symbol = 'ES';
S.ind = { schema: SCHEMA, overrides: {}, symbols: { gap: {} }, per_symbol: {} };
indDlg.open(true, 'gap');
edit('gap', 'MinimumGapPoints', '5');
tick('gap', 'AlertOnGapFill', false);
await root.sel['.ind-all'].onclick();
console.log(JSON.stringify({ calls, gapSet: S.gapSet }));
"""


@needs_node
def test_apply_to_all_charts_gives_the_gap_alerts_the_new_settings():
    """This tab has Gap settings of its own (RTH from 09:00, minimum size 1), which the chart keeps in S.gapSet until
    Apply to all charts clears them. Minimum size 5 and no fill alerts, applied to every chart: the server's gap
    alerts take the settings every chart now shows, the tab's RTH start included, not the tab's old minimum size."""
    from test_hunt3_web_ui import IND_JS, IND_PARTS, SCHEMA, WEB
    from test_hunt_web_chart import run_app
    own = {"RthStart": 90000, "MinimumGapPoints": 1}
    js = IND_JS.replace("/*IND*/", (WEB / "indicators.js").read_text())
    js = js.replace(", gapAlertsFollow = async () => {}", ", gapSpecOk = () => true")   # app.js' own, below
    js = js.replace("tabSet = () => ({})", f"tabSet = () => ({{ gap: {json.dumps(own)} }})")
    js = js.replace("const api = async (method, path, body) => {", """const api = async (method, path, body) => {
  if (path === '/api/gaps/alerts') { calls.push(`${method} ${path} ${JSON.stringify(body)}`); return { alerts: {} }; }""")
    js = js.replace("ask: async () => null", "ask: async () => true").replace("['amt', 'ifthen', 'lines'].map(indAdapter)", "['gap'].map(indAdapter)")
    js = js.replace("const S = { st: { amt: true, ifthen: true, lines: true }",
                    "const S = { st: { gap: true }, gapAlerts: {}, gaps: null, gapError: '', gapSet: "
                    f"{json.dumps({**GAP_SET, **own})}")
    js = js[:js.index("const ok = async () =>")] + APPLY_ALL_TAIL
    js = js.replace("SCHEMA", json.dumps(SCHEMA))
    parts = IND_PARTS + ["indPut", "hhmm", "gapRth", "gapRthOk", "GAP_RTH_BAD", "gapAlertsPut", "renderGapInfo", "gapAlertsFollow",
                         "GAP_SWITCH", "GAP_SHARED", "gapShown"]
    out = run_app(parts, f"(async () => {{ {js} }})().catch((e) => {{ console.error(e); process.exit(1); }});")
    assert out["gapSet"]["MinimumGapPoints"] == 1   # still the tab's own here (indApply is stubbed): not what is sent
    sent = [c for c in out["calls"] if c.startswith("PUT /api/gaps/alerts ")]
    assert len(sent) == 1, out["calls"]
    # the alert switches are every chart's: only the one changed here is sent (over the server's)
    assert json.loads(sent[0].split(" ", 2)[2]) == {"fill": False, "RthStart": 90000, "RthEnd": 150000, "MinimumGapPoints": 5}


# ---- k-gd8-2: Ctrl+Z after a delete on a once-live future's free-data chart brings its alert back -----------------------

def test_an_undone_delete_on_a_futures_free_data_chart_gets_its_alert_back(tmp_path, monkeypatch):
    """NQ's lines alert; NQ taken off Instruments. On the NQ1! chart a line deleted and put back with Ctrl+Z (the page
    PUTs it as it was, its alert switched on), and Delete all undone the same way: each gets its alert back, NQ's, as
    on a live chart. NQ put back live: a trade through the line fires it. A line drawn there with no alert gets none."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        c.portal.call(app.state.market.symbols._put, LISTED, "nasdaq")   # the listings: no stock is NQ
        for i, p in (("a1", 21000.0), ("b1", 21500.0), ("b2", 22000.0)):
            assert c.put(f"/api/drawings/NQ/{i}", json=line(p)).status_code == 200
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] not in ("NQ", "MNQ")])   # a restart applies it
    _, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert "NQ" not in c.get("/api/instruments").json()
        before = {d["id"]: {k: d[k] for k in ("type", "pts", "style", "alert")} for d in c.get("/api/drawings/NQ1!").json()["drawings"]}
        assert c.delete("/api/drawings/NQ1!/a1").status_code == 204
        assert c.put("/api/drawings/NQ1!/a1", json=before["a1"]).status_code == 200   # Ctrl+Z
        assert [a[:2] for a in line_alerts(c)] == [("NQ", "a1"), ("NQ", "b1"), ("NQ", "b2")]
        assert c.delete("/api/drawings/NQ1!").json() == {"deleted": 3} and line_alerts(c) == []
        for i, d in before.items():   # Ctrl+Z: every drawing Delete all took, saved again
            assert c.put(f"/api/drawings/NQ1!/{i}", json=d).status_code == 200
        assert line_alerts(c) == [("NQ", "a1", 21000.0, True), ("NQ", "b1", 21500.0, True), ("NQ", "b2", 22000.0, True)]
        assert c.put("/api/drawings/NQ1!/c1", json=line(23000.0, on=False)).status_code == 200   # drawn there: no alert
        assert len(line_alerts(c)) == 3
        set_instruments(c, file_rows(tmp_path))   # NQ put back: live at once, no restart
        assert ids(c, "NQ") == ["a1", "b1", "b2", "c1"]
        m = app.state.market
        for k, p in enumerate((20990.0, 21010.0)):
            c.portal.call(m.alerts.on_tick, Tick("NQ", T0 + 1000 * k, p, 1, 1))
        assert [e["message"].split(" (")[0] for e in c.get("/api/alert-events").json()] == ["NQ crossed above line 21000.00"]
    assert stored(tmp_path) == [("NQ1!", "a1"), ("NQ1!", "b1"), ("NQ1!", "b2"), ("NQ1!", "c1")]


# ---- k-gd8-3: the alerts kept for an instrument taken off Instruments run once it is back, no restart -------------------

def test_the_alerts_of_an_instrument_put_back_run_without_a_restart(tmp_path, monkeypatch):
    """NQ's gap, AMT, study and indicator alerts on; NQ taken off Instruments (a restart). Put back (live at once): its
    gap watch, AMT alert runner and study and indicator alert watches start then, not at the next restart."""
    from chartsrv.amt_hub import ALERT_MINUTES
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert c.put("/api/gaps/alerts/NQ", json={**GAP, "on": True}).status_code == 200
        assert c.put("/api/amt/alerts/NQ", json={"on": True}).status_code == 200
        assert c.post("/api/alerts", json={"symbol": "NQ", "kind": "study", "study": "vwap"}).status_code == 201
        assert c.post("/api/alerts", json={"symbol": "NQ", "kind": "ind", "a": {"ind": "ema", "len": 9}, "b": {"value": 21000},
                                           "spec": "5m", "direction": "above"}).status_code == 201
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] not in ("NQ", "MNQ")])
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market

        def running():   # its gap watch, AMT alert runner, study alert watch, indicator alert watch
            return ("NQ" in m.gaps.watches, ("NQ", ALERT_MINUTES) in m.amt.entries, "NQ" in m.study_sources.watches,
                    any(k[0] == "NQ" for k in m.ind_alerts.watches))
        assert running() == (False, False, False, False)
        set_instruments(c, file_rows(tmp_path))   # NQ back, live at once
        assert c.get("/api/gaps/alerts").json()["alerts"]["NQ"]["on"] is True
        assert running() == (True, True, True, True)


# ---- k-gd8-4: a Gap settings save while an instrument is off Instruments reaches its gap alerts too --------------------

def test_a_gap_settings_save_while_an_instrument_is_off_reaches_its_alerts(tmp_path, monkeypatch):
    """ES's and NQ's gap alerts on, minimum size 0 and fills; NQ taken off Instruments. Gap settings saved with minimum
    size 5 and no fill alerts: NQ's kept config takes them too (still not shown, no watch), so once NQ is back its
    alerts run with them, as ES's do."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        for s in ("ES", "NQ"):
            assert c.put(f"/api/gaps/alerts/{s}", json={**GAP, "on": True}).status_code == 200
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] not in ("NQ", "MNQ")])
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        r = c.put("/api/gaps/alerts", json={**GAP, "MinimumGapPoints": 5.0, "fill": False})   # a Gap settings save
        assert r.status_code == 200 and set(r.json()["alerts"]) == {"ES"}
        g = app.state.market.gaps
        assert (g.alert_cfg["NQ"]["MinimumGapPoints"], g.alert_cfg["NQ"]["fill"], g.alert_cfg["NQ"]["on"]) == (5.0, False, True)
        assert "NQ" not in g.watches
        set_instruments(c, file_rows(tmp_path))   # NQ back
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        a = c.get("/api/gaps/alerts").json()["alerts"]
        assert {s: (v["MinimumGapPoints"], v["fill"]) for s, v in a.items()} == {"ES": (5.0, False), "NQ": (5.0, False)}
        w = app.state.market.gaps.watches["NQ"].cfg
        assert (w["MinimumGapPoints"], w["fill"]) == (5.0, False)
