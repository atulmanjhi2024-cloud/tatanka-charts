"""TradingView's alert conditions (greater/less than, entering/exiting a channel), expiration, and moving or
changing an alert in place."""
import pytest

from chartsrv.alerts import AlertEngine
from chartsrv.config import Instrument
from chartsrv.models import Tick
from chartsrv.store import Store

ES = Instrument("ES", "E-mini", 0.25, 50)


def engine(tmp_path, **params):
    store = Store(tmp_path / "t.db")
    fired = []
    eng = AlertEngine(store, {"ES": ES}, None, fired.append)
    store.add_alert("ES", "price", params, params.pop("_repeat", False), "")
    eng.reload()
    feed = lambda *px: [eng.on_tick(Tick("ES", 1_000_000 + i * 1000, p, 1, 0)) for i, p in enumerate(px)]   # noqa: E731
    return eng, fired, feed


def test_greater_than_fires_while_past_the_value(tmp_path):
    eng, fired, feed = engine(tmp_path, price=7000.0, direction="gt")
    feed(7001.0, 7002.0)                       # already above on the first move: fires (no crossing needed)
    assert len(fired) == 1 and "above 7000.00" in fired[0]["message"]
    eng2, fired2, feed2 = engine(tmp_path / "b", price=7000.0, direction="lt")
    feed2(7001.0, 7000.25, 6999.75)
    assert len(fired2) == 1 and "below 7000.00" in fired2[0]["message"]


def test_channel_enter_and_exit(tmp_path):
    eng, fired, feed = engine(tmp_path, price=7000.0, price2=7010.0, direction="enter")
    feed(6995.0, 6999.75, 7000.0)
    assert len(fired) == 1 and "entered 7000.00–7010.00" in fired[0]["message"]
    eng2, fired2, feed2 = engine(tmp_path / "b", price=7000.0, price2=7010.0, direction="exit")
    feed2(7005.0, 7009.0, 7010.25)
    assert len(fired2) == 1 and "left 7000.00–7010.00" in fired2[0]["message"]


def test_expired_alert_switches_off_without_firing(tmp_path):
    changed = []
    eng, fired, feed = engine(tmp_path, price=7000.0, direction="cross", expires=1_001_500)
    eng.on_change = lambda: changed.append(1)
    feed(6999.0, 6999.5, 7001.0)               # the cross comes at 1,002,000: after it expired
    assert fired == [] and changed == [1] and eng.alerts[1].enabled is False


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_FEED", "none")
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as c:
        yield c


def test_api_create_move_and_change(client):
    a = client.post("/api/alerts", json={"symbol": "ES", "kind": "price", "price": 7010.1, "price2": 7000, "direction": "enter"}).json()
    assert a["params"] == {"price": 7000.0, "price2": 7010.0, "direction": "enter"}
    client.patch(f"/api/alerts/{a['id']}", json={"enabled": False})
    b = client.patch(f"/api/alerts/{a['id']}", json={"price": 6995.3, "direction": "cross"}).json()   # dragged: re-armed
    assert b["params"] == {"price": 6995.25, "direction": "cross"} and b["enabled"] is True
    c = client.patch(f"/api/alerts/{a['id']}", json={"expires": 4_000_000_000_000, "note": "x", "repeat": True}).json()
    assert c["params"]["expires"] == 4_000_000_000_000 and c["note"] == "x" and c["repeat"] is True
    assert "expires" not in client.patch(f"/api/alerts/{a['id']}", json={"expires": 0}).json()["params"]
    for body, code in [({"symbol": "ES", "kind": "price", "price": 1, "direction": "enter"}, 400),
                       ({"symbol": "ES", "kind": "price", "price": 1, "expires": 5}, 400),
                       ({"symbol": "ES", "kind": "price", "price": 1, "direction": "sideways"}, 422)]:
        assert client.post("/api/alerts", json=body).status_code == code, body
    lv = client.post("/api/alerts", json={"symbol": "ES", "kind": "level"}).json()
    assert client.patch(f"/api/alerts/{lv['id']}", json={"price": 1}).status_code == 400
    assert client.patch("/api/alerts/999", json={"enabled": True}).status_code == 404


def test_restarting_an_expired_alert_clears_its_expiry(client):
    import time
    exp = int(time.time() * 1000) + 300
    a = client.post("/api/alerts", json={"symbol": "ES", "kind": "price", "price": 7000, "expires": exp}).json()
    assert a["params"]["expires"] == exp
    time.sleep(0.4)                            # it ran out
    b = client.patch(f"/api/alerts/{a['id']}", json={"enabled": True}).json()
    assert b["enabled"] is True and "expires" not in b["params"]


def test_alert_that_fires_while_the_database_cannot_be_written_is_still_sent(tmp_path):
    """A full disk, or another program holding the database's lock: the alert goes out all the same (with no event
    id), and one that expires then switches off without the error leaving on_tick (it would drop the NinjaTrader
    connection, whose trades it runs on)."""
    import sqlite3
    store = Store(tmp_path / "t.db")
    fired, changed = [], []
    eng = AlertEngine(store, {"ES": ES}, None, fired.append)
    eng.on_change = lambda: changed.append(1)
    store.add_alert("ES", "price", {"price": 7000.0, "direction": "cross"}, False, "")
    store.add_alert("ES", "price", {"price": 7100.0, "direction": "cross", "expires": 1_002_500}, False, "")
    eng.reload()
    store.db.execute("PRAGMA busy_timeout=50")   # the real wait is 5 s
    eng.on_tick(Tick("ES", 1_000_000, 6999.0, 1, 0))
    other = sqlite3.connect(tmp_path / "t.db")
    other.execute("BEGIN IMMEDIATE")             # the database can't be written for now
    try:
        eng.on_tick(Tick("ES", 1_001_000, 7001.0, 1, 0))   # crosses 7000
        eng.on_tick(Tick("ES", 1_003_000, 7001.0, 1, 0))   # the other one expires
    finally:
        other.rollback()
        other.close()
    assert [(e["message"], e["id"]) for e in fired] == [("ES crossed above 7000.00 (last 7001.00)", None)]
    assert changed == [1] and not eng.alerts[2].enabled


def test_alert_sent_with_no_event_id_is_not_marked_delivered(tmp_path, monkeypatch):
    """With no event row (it couldn't be written), delivery doesn't wait on the database to mark one: the event
    loop would stall for the 5 s lock timeout."""
    import asyncio
    from test_review_fixes import build_market
    m, notifier = build_market(tmp_path, monkeypatch)
    marked = []
    m.store.set_event_delivered = lambda *a: marked.append(a)
    asyncio.run(m._deliver({"id": None, "alert_id": 1, "symbol": "ES", "ts": 1, "price": 1.0, "message": "ES crossed"}))
    assert notifier.sent == [("ES alert", "ES crossed")] and marked == []
