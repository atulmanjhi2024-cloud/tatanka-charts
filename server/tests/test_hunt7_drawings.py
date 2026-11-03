"""Bug hunt round 7, drawings: a root only a future has (NQ, ZB: no stock's ticker) and its future (NQ1!) are one
chart's drawings, read under both, and nothing moves them at a start: a once-live future's lines keep their alerts on
its free-data chart, a root a stock shares (NG) keeps its free-data chart's lines apart, and a future made live without
a restart shows its drawings at once."""
from chartsrv.models import Tick
from chartsrv.store import Store
from test_hunt6_watchlist import LISTED
from test_instruments_edit import _client as app_client, file_rows

T0 = 1_792_000_000_000


def _client(tmp_path, monkeypatch):
    """The app on the test's database, started (and stopped) as the server is; no Yahoo quotes, whose polls of the
    network (on the listings' quote keys) a stop can wait out."""
    monkeypatch.setenv("CHART_QUOTES_FEED", "none")
    return app_client(tmp_path, monkeypatch)


def line(p, on=True, text=""):
    return {"type": "hline", "pts": [{"t": T0, "p": p}], "style": {"text": text},
            "alert": {"on": on, "direction": "cross", "repeat": False}}


def ids(c, sym):
    r = c.get(f"/api/drawings/{sym}")
    assert r.status_code == 200, r.text
    return sorted(d["id"] for d in r.json()["drawings"])


def line_alerts(c):
    return sorted((a["symbol"], a["params"]["drawing"], a["params"]["p1"], a["enabled"])
                  for a in c.get("/api/alerts").json() if a["kind"] == "line")


def stored(tmp_path):
    db = Store(tmp_path / "chart.db")
    try:
        return sorted(tuple(r) for r in db.db.execute("SELECT symbol, id FROM drawings"))
    finally:
        db.close()


def set_instruments(c, rows):
    st = c.put("/api/instruments/config", json={"instruments": rows})
    assert st.status_code == 200, st.text
    return st.json()


def catalog_row(c, sym):
    row = next(r for r in c.get("/api/instruments/config").json()["catalog"] if r["symbol"] == sym)
    return {k: v for k, v in row.items() if k != "group"}


def broadcast(ws, kind="drawing"):
    for _ in range(200):
        m = ws.receive_json()
        if m["type"] == kind:
            return m
    raise AssertionError(f"no {kind} message")


def test_a_futures_lines_keep_their_alerts_on_its_free_data_chart_once_it_is_not_live(tmp_path, monkeypatch):
    """NQ live with three lines that alert, then taken off Connections > Instruments: its free-data chart NQ1! shows
    them where they are stored (NQ). A line deleted there takes its alert along, one dragged there takes its alert to
    the new level (re-armed, still NQ's), and the third's alert deleted in the Alerts tab is switched off on the line
    (the NQ1! chart hears it). NQ put back, live at once: its chart shows the two left, its trades fire only the moved
    line's alert, at its new level, and a label typed on the third brings no alert back."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        c.portal.call(app.state.market.symbols._put, LISTED, "nasdaq")   # the listings: no stock is NQ
        for i, p in (("a1", 21000.0), ("a2", 21500.0), ("a3", 22000.0)):
            assert c.put(f"/api/drawings/NQ/{i}", json=line(p)).status_code == 200
        assert [a[:3] for a in line_alerts(c)] == [("NQ", "a1", 21000.0), ("NQ", "a2", 21500.0), ("NQ", "a3", 22000.0)]
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] not in ("NQ", "MNQ")])   # a restart applies it
    _, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert "NQ" not in c.get("/api/instruments").json()
        assert ids(c, "NQ1!") == ids(c, "NQ") == ["a1", "a2", "a3"]
        a2 = next(a for a in c.get("/api/alerts").json() if a["kind"] == "line" and a["params"]["drawing"] == "a2")
        c.patch(f"/api/alerts/{a2['id']}", json={"enabled": False})   # as if it had fired
        assert c.delete("/api/drawings/NQ1!/a1").status_code == 204
        assert c.put("/api/drawings/NQ1!/a2", json=line(21800.0)).status_code == 200   # dragged on the NQ1! chart
        assert line_alerts(c) == [("NQ", "a2", 21800.0, True), ("NQ", "a3", 22000.0, True)]
        a3 = next(a for a in c.get("/api/alerts").json() if a["kind"] == "line" and a["params"]["drawing"] == "a3")
        with c.websocket_connect("/ws") as ws:
            assert c.delete(f"/api/alerts/{a3['id']}").status_code == 204   # in the Alerts tab
            m = broadcast(ws)
        assert m["symbol"] == "NQ1!" and m["drawing"]["id"] == "a3" and m["alert_deleted"] is True
        d3 = next(d for d in c.get("/api/drawings/NQ1!").json()["drawings"] if d["id"] == "a3")
        assert d3["alert"]["on"] is False and line_alerts(c) == [("NQ", "a2", 21800.0, True)]
        set_instruments(c, file_rows(tmp_path))   # NQ put back: live at once, no restart
        assert "NQ" in c.get("/api/instruments").json()
        assert ids(c, "NQ") == ["a2", "a3"] and c.get("/api/drawings/NQ1!").status_code == 404   # NQ's chart now
        m = app.state.market
        for k, p in enumerate((20990.0, 21010.0, 21490.0, 21510.0, 21790.0, 21810.0)):
            c.portal.call(m.alerts.on_tick, Tick("NQ", T0 + 1000 * k, p, 1, 1))
        assert [e["message"].split(" (")[0] for e in c.get("/api/alert-events").json()] == ["NQ crossed above line 21800.00"]
        d3 = next(d for d in c.get("/api/drawings/NQ").json()["drawings"] if d["id"] == "a3")
        typed = {**d3, "style": {**d3["style"], "text": "weekly high"}}   # an unrelated edit, as the chart has it
        assert c.put("/api/drawings/NQ/a3", json={k: typed[k] for k in ("type", "pts", "style", "alert")}).status_code == 200
        assert [a[:3] for a in line_alerts(c)] == [("NQ", "a2", 21800.0)]
    assert stored(tmp_path) == [("NQ", "a2"), ("NQ", "a3")]   # where they were drawn: nothing moved


def test_a_root_a_stock_shares_keeps_its_free_data_charts_drawings_apart(tmp_path, monkeypatch):
    """NG is natural gas's root and NovaGold's ticker. A line drawn on the free-data NG1! chart and one on NovaGold's
    NG chart stay where they were drawn through starts with NG live (a catalog future added) and after it is taken off
    again: NG1! shows the gas line, NG NovaGold's, never the two together."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        c.portal.call(app.state.market.symbols._put, LISTED, "nasdaq")
        assert c.put("/api/drawings/NG1!/gas1", json=line(3.10, on=False)).status_code == 200
        assert c.put("/api/drawings/NG/novagold1", json=line(4.20, on=False)).status_code == 200
        assert (ids(c, "NG1!"), ids(c, "NG")) == (["gas1"], ["novagold1"])
        set_instruments(c, [*file_rows(tmp_path), {"symbol": "NG", **catalog_row(c, "NG")}])   # live at once
        assert "NG" in c.get("/api/instruments").json() and ids(c, "NG") == ["novagold1"]
    _, client = _client(tmp_path, monkeypatch)   # a start with NG live
    with client as c:
        assert "NG" in c.get("/api/instruments").json()
        set_instruments(c, file_rows(tmp_path))   # NG off again: a restart applies it
    assert stored(tmp_path) == [("NG", "novagold1"), ("NG1!", "gas1")]
    _, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert "NG" not in c.get("/api/instruments").json()
        assert (ids(c, "NG1!"), ids(c, "NG")) == (["gas1"], ["novagold1"])
    assert stored(tmp_path) == [("NG", "novagold1"), ("NG1!", "gas1")]


def test_a_future_made_live_without_a_restart_shows_its_drawings_at_once(tmp_path, monkeypatch):
    """A line made on an older list's bare ZB chart (stored as ZB) and one on the free-data ZB1! chart both show on
    ZB1!. ZB added on Instruments (live at once, as a future opened from the NinjaTrader catalog is) shows both on its
    live chart, no restart. The free-data line's alert switched on there is ZB's, deleted in the Alerts tab it is off on
    the line, and the line deleted there goes, alert and all; a new line is stored as ZB, and Delete all takes the
    chart's lines wherever they are stored. A start moves none."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        c.portal.call(app.state.market.symbols._put, LISTED, "nasdaq")
    db = Store(tmp_path / "chart.db")   # drawn before quote keys, on an older list's bare ZB
    db.put_drawing("ZB", {"id": "old1", "type": "hline", "pts": [{"t": T0, "p": 118.5}], "style": {}, "alert": {"on": False}})
    db.close()
    _, client = _client(tmp_path, monkeypatch)   # the first start after the update
    with client as c:
        assert c.put("/api/drawings/ZB1!/free1", json=line(119.0, on=False)).status_code == 200
        assert ids(c, "ZB1!") == ids(c, "ZB") == ["free1", "old1"]
        set_instruments(c, [*file_rows(tmp_path), {"symbol": "ZB", **catalog_row(c, "ZB")}])   # live at once
        assert "ZB" in c.get("/api/instruments").json()
        assert ids(c, "ZB") == ["free1", "old1"] and c.get("/api/drawings/ZB1!").status_code == 404
        assert c.put("/api/drawings/ZB/free1", json=line(119.0)).status_code == 200   # its alert switched on, live
        assert line_alerts(c) == [("ZB", "free1", 119.0, True)]
        (a,) = [a for a in c.get("/api/alerts").json() if a["kind"] == "line"]
        with c.websocket_connect("/ws") as ws:
            assert c.delete(f"/api/alerts/{a['id']}").status_code == 204   # in the Alerts tab
            m = broadcast(ws)
        assert m["symbol"] == "ZB" and m["drawing"]["id"] == "free1" and m["alert_deleted"] is True
        free1 = next(d for d in c.get("/api/drawings/ZB").json()["drawings"] if d["id"] == "free1")
        assert free1["alert"]["on"] is False and line_alerts(c) == []
        assert stored(tmp_path) == [("ZB", "old1"), ("ZB1!", "free1")]   # each where it was drawn
        assert c.put("/api/drawings/ZB/free1", json=line(119.5)).status_code == 200
        assert line_alerts(c) == [("ZB", "free1", 119.5, True)]
        assert c.delete("/api/drawings/ZB/free1").status_code == 204 and line_alerts(c) == []
        assert c.delete("/api/drawings/ZB/free1").status_code == 404
        assert c.put("/api/drawings/ZB/new1", json=line(120.0, on=False)).status_code == 200
        assert stored(tmp_path) == [("ZB", "new1"), ("ZB", "old1")]
    _, client = _client(tmp_path, monkeypatch)   # ZB live from the start
    with client as c:
        assert ids(c, "ZB") == ["new1", "old1"]
        assert c.put("/api/drawings/ZB1!/x", json=line(1.0, on=False)).status_code == 404   # the live chart's
    db = Store(tmp_path / "chart.db")
    db.put_drawing("ZB1!", {"id": "free2", "type": "hline", "pts": [{"t": T0, "p": 117.0}], "style": {}, "alert": {"on": False}})
    db.close()
    _, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert ids(c, "ZB") == ["free2", "new1", "old1"]
        assert c.delete("/api/drawings/ZB").json() == {"deleted": 3} and ids(c, "ZB") == []
    assert stored(tmp_path) == []


def test_a_line_restyled_on_the_free_data_chart_keeps_its_fired_alert_on_the_tick_grid(tmp_path, monkeypatch):
    """GC's line at 3000.1000000000004 (the browser's 0.1 tick) alerts at 3000.1. With GC taken off Instruments and
    its alert fired, a label typed on the line on the GC1! chart leaves the alert as it was (on the tick the futures
    list gives GC: the line has not moved), and a drag there moves it, on that tick, and re-arms it."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        c.portal.call(app.state.market.symbols._put, LISTED, "nasdaq")
        assert c.put("/api/drawings/GC/g1", json=line(3000.1000000000004)).status_code == 200
        (a,) = [a for a in c.get("/api/alerts").json() if a["kind"] == "line"]
        c.patch(f"/api/alerts/{a['id']}", json={"enabled": False})   # as if it had fired
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] != "GC"])
    _, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert "GC" not in c.get("/api/instruments").json() and ids(c, "GC1!") == ["g1"]
        assert c.put("/api/drawings/GC1!/g1", json=line(3000.1000000000004, text="R1")).status_code == 200
        assert line_alerts(c) == [("GC", "g1", 3000.1, False)]
        assert c.put("/api/drawings/GC1!/g1", json=line(3010.2000000000003, text="R1")).status_code == 200
        assert line_alerts(c) == [("GC", "g1", 3010.2, True)]
