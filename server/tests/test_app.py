"""End to end: sim feed -> store -> websocket history/updates, plus alert CRUD over REST."""
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

LINE = "2026-01-15, 6900.00, 6920.00, 6940.00, 6880.00, 6860.00"   # invented daily levels, near the simulator's ES


@pytest.fixture
def client(tmp_path, monkeypatch, in_session):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_DOCUMENTS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "20")
    monkeypatch.delenv("CHART_DISCORD_WEBHOOK", raising=False)
    monkeypatch.delenv("CHART_NTFY_URL", raising=False)
    from chartsrv.app import app
    with TestClient(app) as c:
        # the Plan tab's line, from the PC itself (whatever other names the app is told to answer to)
        assert c.put("/api/amt/levels/ES", json={"text": LINE}, headers={"host": "127.0.0.1:8800"}).status_code == 200
        yield c


def recv_until(ws, kind, limit=200):
    for _ in range(limit):
        m = ws.receive_json()
        if m["type"] == kind:
            return m
    raise AssertionError(f"no {kind} message")


def test_health_and_levels(client):
    h = client.get("/api/health").json()
    assert h["feeds"] == ["sim", "sim-quotes", "fred"]   # FRED values: idle until a list holds a FRED symbol
    assert h["levels"] == {"ES": "2026-01-15"}
    assert client.get("/api/levels/ES").json()["lines"][0] == ["P", 6900.0]
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("spec", ["1m", "5m", "200t", "8r", "500v"])
def test_ws_history_then_live_bars(client, spec):
    with client.websocket_connect("/ws") as ws:
        hello = recv_until(ws, "hello")
        assert "ES" in hello["instruments"] and hello["levels"]["ES"]["lines"]
        ws.send_json({"op": "sub", "id": 1, "symbol": "ES", "spec": spec})
        hist = recv_until(ws, "history")
        assert hist["spec"] == spec and len(hist["bars"]) > 1
        seqs = [b["seq"] for b in hist["bars"]]
        assert seqs == sorted(seqs)
        live = recv_until(ws, "bars")
        assert live["id"] == 1 and live["bars"][0]["seq"] >= seqs[-1]


def test_history_matches_store_volume(client):
    """1m history volume must equal the raw ticks' volume: nothing lost or double counted at the cutoff."""
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"op": "sub", "id": 1, "symbol": "NQ", "spec": "1m"})
        hist = recv_until(ws, "history")
    m = client.app.state.market
    time.sleep(1.5)  # let the store loop flush
    first_t = hist["bars"][0]["t"]
    last = hist["bars"][-1]
    db = sqlite3.connect(m.store.path)
    db_v = db.execute("SELECT SUM(size) FROM ticks WHERE symbol='NQ' AND ts>=? AND ts<?",
                              (first_t, last["t"])).fetchone()[0]
    assert sum(b["v"] for b in hist["bars"][:-1]) == db_v


def test_alert_crud_and_bad_input(client):
    r = client.post("/api/alerts", json={"symbol": "es", "kind": "price", "price": 7850.1})
    assert r.status_code == 201
    a = r.json()
    assert a["symbol"] == "ES" and a["params"]["price"] == 7850.0 and a["repeat"] is False
    lv = client.post("/api/alerts", json={"symbol": "NQ", "kind": "level"}).json()
    assert lv["repeat"] is True
    assert client.patch(f"/api/alerts/{a['id']}", json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/api/alerts/{a['id']}").status_code == 204
    assert [x["id"] for x in client.get("/api/alerts").json()] == [lv["id"]]
    assert client.post("/api/alerts", json={"symbol": "XX", "kind": "level"}).status_code == 400
    assert client.post("/api/alerts", json={"symbol": "ES", "kind": "price"}).status_code == 400
    assert client.post("/api/notify-test").status_code == 400   # nothing configured


def test_live_price_alert_fires_over_ws(client):
    m = client.app.state.market
    px = m.last_tick["ES"].price
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "hello")
        # an alert one tick away fires within seconds on the random walk; try both sides
        for p in (px + 0.25, px - 0.25):
            client.post("/api/alerts", json={"symbol": "ES", "kind": "price", "price": p})
        deadline = time.time() + 30
        while time.time() < deadline:
            msg = ws.receive_json()
            if msg["type"] == "alert":
                assert msg["event"]["symbol"] == "ES"
                break
        else:
            raise AssertionError("alert never fired")
    events = client.get("/api/alert-events").json()
    assert events and events[0]["delivered"] == ""


def test_bad_subscription_reports_error(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"op": "sub", "id": 9, "symbol": "ES", "spec": "7x"})
        err = recv_until(ws, "error")
        assert err["id"] == 9 and "bad bar spec" in err["message"]


def test_connections_and_push_api(client):
    from test_webpush import _device
    cs = {c["id"]: c for c in client.get("/api/connections").json()["connections"]}
    assert cs["ninjatrader"]["locked"] and cs["push"]["push"]["devices"] == [] and len(cs["push"]["push"]["key"]) == 87
    assert client.put("/api/connections/nothing", json={}).status_code == 404
    assert client.put("/api/connections/ibkr", json={"ibkr_port": 99999}).status_code == 400
    r = client.put("/api/connections/ntfy", json={"ntfy_url": "https://ntfy.sh/secret-topic-123"})
    assert r.status_code == 200 and "secret-topic" not in r.text and r.json()["fields"][0]["source"] == "app"
    assert client.get("/api/health").json()["notify"] == ["ntfy"]
    assert client.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://example.com/x"}}).status_code == 400
    did = client.post("/api/push/subscribe", json={"subscription": _device()[2], "label": "iPhone · app"}).json()["id"]
    assert client.get("/api/health").json()["notify"] == ["ntfy", "app"]
    assert client.delete(f"/api/push/devices/{did}").status_code == 204
    assert client.delete(f"/api/push/devices/{did}").status_code == 404


def test_templates_api(client):
    assert client.get("/api/templates").json() == {"templates": {}}
    r = client.put("/api/templates/draw:hline/Key level", json={"data": {"color": "#ff9800", "width": 2}})
    assert r.status_code == 200 and r.json()["data"]["width"] == 2
    client.put("/api/templates/chart/My NT", json={"data": {"cs": {"bg": "#000000"}}})
    client.put("/api/templates/indicators/Mine", json={"data": {"studies": {"amt": True}}})
    t = client.get("/api/templates").json()["templates"]
    assert set(t) == {"draw:hline", "chart", "indicators"} and t["draw:hline"]["Key level"]["data"]["color"] == "#ff9800"
    # a chart template saved before the two were separate: its studies become an indicator template of that name
    client.put("/api/templates/chart/Old", json={"data": {"studies": {"tpo": True}, "inds": [], "layers": ["bars"], "cs": {"bg": "#111111"}}})
    client.put("/api/templates/chart/Old studies", json={"data": {"studies": {"gap": True}}})
    t = client.get("/api/templates").json()["templates"]
    assert t["chart"]["Old"]["data"] == {"cs": {"bg": "#111111"}} and "Old studies" not in t["chart"]
    assert t["indicators"]["Old"]["data"] == {"studies": {"tpo": True}, "inds": [], "layers": ["bars"]}
    assert t["indicators"]["Old studies"]["data"] == {"studies": {"gap": True}} and t["indicators"]["Mine"]
    assert client.get("/api/templates").json()["templates"] == t   # once
    assert client.delete("/api/templates/chart/Old").status_code == 204
    for kind, name, data in [("nope", "x", {"a": 1}), ("chart", "a/b", {"a": 1}), ("chart", " ", {"a": 1}),
                             ("chart", "x", {}), ("chart", "x", {"a": "y" * 30000})]:
        assert client.put(f"/api/templates/{kind}/{name}", json={"data": data}).status_code in (400, 404, 405), (kind, name)
    assert client.delete("/api/templates/chart/My NT").status_code == 204
    assert client.delete("/api/templates/chart/My NT").status_code == 404
    assert "chart" not in client.get("/api/templates").json()["templates"]


def test_other_sites_refused(client):
    """A page of another site, open in the same browser, can't use the app: no live connection, no changes."""
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "hello"
    # behind tailscale serve: the Host it passes on, X-Forwarded-Host (the same name) and X-Forwarded-Proto
    serve = {"host": "pc.tail1.ts.net", "x-forwarded-host": "pc.tail1.ts.net", "x-forwarded-for": "100.64.0.7",
             "x-forwarded-proto": "https"}
    with client.websocket_connect("/ws", headers={**serve, "origin": "https://pc.tail1.ts.net"}) as ws:
        assert ws.receive_json()["type"] == "hello"
    body = {"data": {"studies": {}}}
    assert client.put("/api/templates/chart/x", json=body, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.put("/api/templates/chart/x", json=body, headers={"origin": "http://testserver"}).status_code == 200
    assert client.get("/api/templates", headers={"origin": "https://evil.example"}).status_code == 200   # reads: CORS guards them


def test_template_nan(client):
    r = client.put("/api/templates/chart/t1", content=b'{"data": {"x": NaN}}', headers={"content-type": "application/json"})
    assert r.status_code == 400 and client.get("/api/templates").status_code == 200
