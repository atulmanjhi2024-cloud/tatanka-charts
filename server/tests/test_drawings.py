"""Drawings: validation, line geometry, alerts on drawn lines (including a sloped trend line) and the API that
keeps a drawing's alert in step with it."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from chartsrv import drawings
from chartsrv.alerts import AlertEngine
from chartsrv.config import Instrument
from chartsrv.models import Tick
from chartsrv.store import Store

ES = Instrument("ES", "E-mini", 0.25, 50, "ES")
WEB = Path(__file__).resolve().parents[2] / "web"
T0 = 1_792_000_000_000


def trend(**kw):
    d = {"type": "trend", "pts": [{"t": T0, "p": 7400.0}, {"t": T0 + 3_600_000, "p": 7410.0}],
         "style": {"color": "#2962ff", "width": 2}, "alert": {"on": True, "direction": "cross"}}
    d.update(kw)
    return d


def test_clean_accepts_drawings_and_rejects_junk():
    d = drawings.clean(trend(), "abc_1")
    assert d["id"] == "abc_1" and d["pts"][1] == {"t": T0 + 3_600_000, "p": 7410.0} and d["alert"]["on"]
    fib = drawings.clean({"type": "fib", "pts": trend()["pts"], "style": {"levels": [0, 0.618, 1]}}, "f")
    v = drawings.clean({"type": "vline", "pts": [{"t": 1_760_000_000_000.02, "p": 1}]}, "v")   # the 3rd bar of one ms
    assert v["pts"][0]["t"] == 1_760_000_000_000.02 and isinstance(d["pts"][0]["t"], int)
    assert fib["alert"] == {"on": False, "direction": "cross", "repeat": False}
    assert drawings.clean(trend(style={"locked": True, "hidden": True}), "l")["style"] == {"locked": True, "hidden": True}   # lock, eye
    assert drawings.clean(trend(style={"tab": "tlx3k9a"}), "t")["style"]["tab"] == "tlx3k9a"   # one chart tab only
    with pytest.raises(ValueError, match="bad style tab"):
        drawings.clean(trend(style={"tab": "../x"}), "t")
    for bad, msg in [({"type": "circle"}, "type"), (trend(pts=[{"t": T0, "p": 1}]), "2 point"), (trend(style={"locked": "yes"}), "bad style locked"),
                     (trend(style={"color": "red"}), "bad style color"), (trend(style={"onclick": "x"}), "bad style"),
                     (trend(pts=[{"t": T0, "p": float("nan")}, {"t": T0, "p": 1}]), "points"),
                     ({**trend(), "type": "rect"}, "alerts go on"), (trend(alert={"on": True, "direction": "sideways"}), "direction"),
                     (trend(style={"levels": [0, 99]}), "bad style levels"), (trend(style={"text": "x" * 501}), "bad style text")]:
        with pytest.raises(ValueError, match=msg):
            drawings.clean(bad, "x")
    with pytest.raises(ValueError, match="id"):
        drawings.clean(trend(), "../etc")


def test_points_before_1970_are_kept():
    """FRED and Yahoo's long monthly and weekly histories reach back past 1970: negative epoch ms."""
    t62 = -252_378_000_000                                 # 2 Jan 1962, DGS10's first day
    assert drawings.clean({"type": "hline", "pts": [{"t": t62, "p": 4.06}]}, "h")["pts"] == [{"t": t62, "p": 4.06}]
    for bad in (-5e12, 5e12):
        with pytest.raises(ValueError, match="points"):
            drawings.clean({"type": "hline", "pts": [{"t": bad, "p": 1}]}, "h")


def test_line_geometry_follows_points_and_extensions():
    d = drawings.clean(trend(), "t")
    a = drawings.alert_params(d)
    assert drawings.line_price(a, T0 + 1_800_000) == 7405.0
    assert drawings.line_price(a, T0 - 1) is None and drawings.line_price(a, T0 + 3_600_001) is None
    # drawn right to left with "extend right": still extends past the later point
    d = drawings.clean(trend(pts=list(reversed(trend()["pts"])), style={"extendRight": True}), "t")
    a = drawings.alert_params(d)
    assert drawings.line_price(a, T0 + 7_200_000) == 7420.0 and drawings.line_price(a, T0 - 1) is None
    h = drawings.alert_params(drawings.clean({"type": "hline", "pts": [{"t": T0, "p": 7390.5}], "alert": {"on": True}}, "h"))
    assert drawings.line_price(h, 0) == drawings.line_price(h, 4e12) == 7390.5
    r = drawings.alert_params(drawings.clean({"type": "hray", "pts": [{"t": T0, "p": 7390.5}], "alert": {"on": True}}, "r"))
    assert drawings.line_price(r, T0 - 1) is None and drawings.line_price(r, T0 + 10**9) == 7390.5


def test_sloped_trend_line_alert_fires_at_the_level_of_the_moment(tmp_path):
    store = Store(tmp_path / "t.db")
    fired = []
    eng = AlertEngine(store, {"ES": ES}, None, fired.append)
    store.add_alert("ES", "line", drawings.alert_params(drawings.clean(trend(), "t")), False, "trend support")
    eng.reload()
    half = T0 + 1_800_000                                  # the line is at 7405 here
    eng.on_tick(Tick("ES", half, 7406.0, 1, 0))
    eng.on_tick(Tick("ES", half + 1000, 7404.0, 1, 0))     # would not cross a flat 7400 line
    assert len(fired) == 1 and "crossed below trend line 7405.00 · trend support" in fired[0]["message"]
    eng.on_tick(Tick("ES", half + 2000, 7407.0, 1, 0))
    assert len(fired) == 1                                 # one-shot


def test_trend_line_that_rises_through_a_still_price_fires(tmp_path):
    """A cross is price and line changing sides, also when the line moves: each print against the line's level at
    its own time."""
    store = Store(tmp_path / "t.db")
    fired = []
    eng = AlertEngine(store, {"ES": ES}, None, fired.append)
    store.add_alert("ES", "line", drawings.alert_params(drawings.clean(trend(), "t")), False, "")
    eng.reload()
    t = T0 + 1_700_000                                     # the line is at 7404.72 here, under the price
    for k in range(120):                                   # two minutes at 7405.00: the line climbs through it
        eng.on_tick(Tick("ES", t + k * 1000, 7405.0, 1, 0))
    assert len(fired) == 1 and "crossed below trend line 7405.00 (last 7405.00)" in fired[0]["message"]
    eng.on_tick(Tick("ES", t + 121_000, 7404.0, 1, 0))
    assert len(fired) == 1                                 # one-shot


def test_line_set_on_the_last_price_fires_on_the_first_move_off_it(tmp_path):
    """As a price alert does: a line drawn on the last trade (the magnet on the last bar's close) and price moves."""
    store = Store(tmp_path / "t.db")
    fired = []
    eng = AlertEngine(store, {"ES": ES}, None, fired.append)
    eng.on_tick(Tick("ES", T0, 7400.0, 1, 0))
    for i, direction in enumerate(("cross", "below")):
        d = {"type": "hline", "pts": [{"t": T0, "p": 7400.0}], "alert": {"on": True, "direction": direction}}
        store.add_alert("ES", "line", drawings.alert_params(drawings.clean(d, f"h{i}")), False, "")
    eng.reload()
    eng.on_tick(Tick("ES", T0 + 1000, 7400.0, 1, 0))      # still on it
    assert fired == []
    for k, px in enumerate((7400.25, 7401.0, 7405.0)):
        eng.on_tick(Tick("ES", T0 + 2000 + k, px, 1, 0))
    assert [(e["alert_id"], e["message"]) for e in fired] == [(1, "ES crossed above line 7400.00 (last 7400.25)")]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as c:
        yield c


def test_drawings_api_and_linked_alert(client):
    def line_alerts():
        return [a for a in client.get("/api/alerts").json() if a["kind"] == "line"]

    with client.websocket_connect("/ws") as ws:
        r = client.put("/api/drawings/es/t1", json=trend())
        assert r.status_code == 200 and r.json()["id"] == "t1"
        for _ in range(100):
            m = ws.receive_json()
            if m["type"] == "drawing":
                break
        assert m["symbol"] == "ES" and m["drawing"]["id"] == "t1"
    assert [d["id"] for d in client.get("/api/drawings/ES").json()["drawings"]] == ["t1"]
    (a,) = line_alerts()
    assert a["enabled"] and a["params"]["drawing"] == "t1" and a["params"]["p1"] == 7400.0
    client.patch(f"/api/alerts/{a['id']}", json={"enabled": False})        # as if it had fired
    moved = trend(pts=[{"t": T0, "p": 7401.0}, {"t": T0 + 3_600_000, "p": 7411.0}], style={"text": "support"})
    client.put("/api/drawings/ES/t1", json=moved)
    (a2,) = line_alerts()
    assert a2["id"] == a["id"] and a2["enabled"] and a2["params"]["p1"] == 7401.0 and a2["note"] == "support"
    client.put("/api/drawings/ES/t1", json=trend(alert={"on": False}))
    assert line_alerts() == []
    client.put("/api/drawings/ES/t1", json=trend())
    assert len(line_alerts()) == 1
    assert client.delete("/api/drawings/ES/t1").status_code == 204 and line_alerts() == []
    assert client.delete("/api/drawings/ES/t1").status_code == 404
    for i, group in enumerate(["paste", "paste", None]):
        style = {"group": group} if group else {}
        client.put(f"/api/drawings/ES/h{i}", json={"type": "hline", "pts": [{"t": T0, "p": 7400 + i}], "style": style})
    assert client.delete("/api/drawings/ES", params={"group": "paste"}).json() == {"deleted": 2}
    assert [d["id"] for d in client.get("/api/drawings/ES").json()["drawings"]] == ["h2"]
    assert client.put("/api/drawings/ES/x", json={"type": "nope"}).status_code == 400
    assert client.get("/api/drawings/AAPL+MSFT").status_code == 404   # a formula: no chart, no drawings
    assert client.get("/api/drawings/FRED:DGS10").status_code == 200   # charts from free data keep drawings too


def test_a_line_alert_deleted_in_the_alerts_tab_stays_deleted(client):
    """Deleting a drawn line's alert from the Alerts tab switches it off on the line, on every chart, so the line's
    next edit (a colour, a label, a drag, an undo) does not bring it back armed."""
    def line_alerts():
        return [a for a in client.get("/api/alerts").json() if a["kind"] == "line"]

    hline = {"type": "hline", "pts": [{"t": T0, "p": 7400.0}], "alert": {"on": True, "direction": "cross"}}
    client.put("/api/drawings/ES/h1", json=hline)
    (a,) = line_alerts()
    with client.websocket_connect("/ws") as ws:
        assert client.delete(f"/api/alerts/{a['id']}").status_code == 204
        (d,) = client.get("/api/drawings/ES").json()["drawings"]
        assert d["alert"]["on"] is False and line_alerts() == []
        for _ in range(100):
            m = ws.receive_json()
            if m["type"] == "drawing":
                break
        assert m["symbol"] == "ES" and m["drawing"] == d and m["alert_deleted"] is True   # charts clear it from undo too
    client.put("/api/drawings/ES/h1", json={**d, "style": {"color": "#ff0000"}})   # the line as charts now have it
    assert line_alerts() == []


def test_a_drawn_lines_alert_is_on_the_tick_grid_trades_come_in_on(client, tmp_path):
    """The browser places a line at round(p / tick) * tick, which on a tick such as GC's 0.1 or CL's 0.01 is often a
    float beside the level (3000.1000000000004): its alert is at the level itself, so a trade exactly at the line
    crosses it from below as a price alert there does, and a touch from above that bounces is not a cross up. A fired
    alert saved before this is not re-armed by the line's next restyle, as it has not moved."""
    def line_alert(sym):
        return next(a for a in client.get("/api/alerts").json() if a["kind"] == "line" and a["symbol"] == sym)

    def fired(inst, params, direction, prices):
        store, got = Store(tmp_path / f"{inst.symbol}{direction}{prices[0]}.db"), []
        eng = AlertEngine(store, {inst.symbol: inst}, None, got.append)
        store.add_alert(inst.symbol, "line", {**params, "direction": direction}, False, "")
        eng.reload()
        for k, px in enumerate(prices):
            eng.on_tick(Tick(inst.symbol, T0 + 1000 * k, px, 1, 0))
        return [e["message"].split(" (")[0] for e in got]

    for sym, tick, near, below, above in (("GC", 0.1, 3000.1000000000004, 3000.0, 3000.2),
                                          ("CL", 0.01, 40.050000000000004, 40.04, 40.06)):
        inst = Instrument(sym, sym, tick, 1, sym)
        line = {"type": "hline", "pts": [{"t": T0, "p": near}], "alert": {"on": True, "direction": "cross"}}
        client.put(f"/api/drawings/{sym}/h1", json=line)
        params, level = line_alert(sym)["params"], inst.round(near)
        assert fired(inst, params, "cross", (below, below, level, below)) == [f"{sym} crossed above line {inst.fmt(level)}"]
        assert fired(inst, params, "above", (above, above, level, above)) == []   # down to it and back: no cross up
        assert params["p1"] == params["p2"] == level
    a, db = line_alert("GC"), Store(tmp_path / "chart.db")
    db.update_alert(a["id"], enabled=False, params={**a["params"], "p1": 3000.1000000000004, "p2": 3000.1000000000004})
    db.close()                                                          # fired, and saved off the grid before
    client.patch(f"/api/alerts/{a['id']}", json={"note": ""})           # the server reads it back
    assert line_alert("GC")["params"]["p1"] == 3000.1000000000004
    client.put("/api/drawings/GC/h1", json={**line, "pts": [{"t": T0, "p": 3000.1000000000004}], "style": {"text": "R1"}})
    a = line_alert("GC")
    assert a["params"]["p1"] == 3000.1 and not a["enabled"] and a["note"] == "R1"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_browser_geometry_and_level_parsing():
    """drawings.js: time <-> bar position round trips on timed and untimed bars, the alert list's line price
    matches the server's, and pasted levels parse."""
    js = f"""
const vm = require('vm'), fs = require('fs'), ctx = {{ localStorage: {{ getItem: () => null, setItem: () => {{}} }}, window: {{ addEventListener() {{}} }}, document: {{ addEventListener() {{}} }} }};
vm.runInNewContext(['press.js', 'studies.js', 'drawings.js'].map((f) => fs.readFileSync({json.dumps(str(WEB))} + '/' + f, 'utf8')).join(';') +   // as index.html loads them
  ';this.create=createDrawings;this.linePrice=drawingLinePrice;', ctx);
const el = {{ addEventListener() {{}}, style: {{}} }}, q = () => ({{ addEventListener() {{}}, querySelector: q, querySelectorAll: () => [], classList: {{ toggle() {{}} }}, style: {{}} }});
let bars = [], per = 0;
const d = ctx.create({{ chart: {{ timeScale: () => ({{}}), priceScale: () => ({{ width: () => 0 }}), options: () => ({{}}), applyOptions() {{}} }},
  series: {{ attachPrimitive() {{}} }}, el, bars: () => bars, periodMs: () => per, tick: () => 0.25, fmt: String, symbol: () => 'ES',
  api: async () => ({{ drawings: [] }}), toast() {{}}, alerts: () => [], ui: {{ bar: q(), edit: q(), paste: q() }} }});
const out = {{}};
bars = [0, 1, 2, 3, 5, 9].map((k) => ({{ t: 1e12 + k * 60000 }})); per = 0;
out.untimed = [1e12 + 4 * 60000, 1e12 + 9 * 60000 + 120000, 1e12 - 60000].map((t) => [d.timeToLogical(t), d.logicalToTime(d.timeToLogical(t))]);
bars = [0, 1, 2, 10, 11].map((k) => ({{ t: 1e12 + k * 300000 }})); per = 300000;
out.timed = [1e12 + 450000, 1e12 + 11 * 300000 + 600000].map((t) => [d.timeToLogical(t), d.logicalToTime(d.timeToLogical(t))]);
out.line = ctx.linePrice({{ t1: 10, p1: 100, t2: 20, p2: 120, left: false, right: true }}, 30);
out.levels = d.parseLevels('7845.75|VAH, 7 830 |POC ;7810\\nbad|x, 7799.5|a|b');
console.log(JSON.stringify(out));"""
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    # untimed bars: between bar opens by time, past the ends at the average bar length (1.8 minutes here)
    assert out["untimed"][0] == [3.5, 1e12 + 4 * 60000] and abs(out["untimed"][1][0] - (5 + 2 / 1.8)) < 1e-9
    assert all(abs(back - t) <= 1 for (_, back), t in zip(out["untimed"], [1e12 + 4 * 60000, 1e12 + 11 * 60000, 1e12 - 60000]))
    assert out["timed"][0] == [1.5, 1e12 + 450000] and out["timed"][1][1] == 1e12 + 11 * 300000 + 600000
    assert out["line"] == drawings.line_price({"t1": 10, "p1": 100, "t2": 20, "p2": 120, "right": True}, 30) == 140
    assert out["levels"]["out"] == [{"p": 7845.75, "text": "VAH"}, {"p": 7830, "text": "POC"}, {"p": 7810, "text": ""},
                                    {"p": 7799.5, "text": "a|b"}]
    assert out["levels"]["bad"] == ["bad|x"]


def test_new_drawing_types():
    pt = lambda t, p: {"t": t, "p": p}
    ok = {"vline": [pt(1, 1)], "arrow": [pt(1, 1), pt(2, 2)], "channel": [pt(1, 1), pt(2, 2), pt(1, 3)],
          "fibext": [pt(1, 1), pt(2, 2), pt(3, 1.5)], "long": [pt(1, 100), pt(9, 110), pt(9, 95)],
          "short": [pt(1, 100), pt(9, 90), pt(9, 105)], "prange": [pt(1, 1), pt(2, 2)], "drange": [pt(1, 1), pt(2, 2)]}
    for kind, pts in ok.items():
        assert drawings.clean({"type": kind, "pts": pts, "style": {"qty": 2, "color": "#ffffff"}}, "x")["type"] == kind
    with pytest.raises(ValueError):
        drawings.clean({"type": "long", "pts": ok["arrow"]}, "x")
    with pytest.raises(ValueError):
        drawings.clean({"type": "long", "pts": ok["long"], "style": {"qty": 0}}, "x")
