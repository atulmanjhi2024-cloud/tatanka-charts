"""Connections as lists per kind: the main ones always listed, others once in use or added with +, and more than one
Discord channel, chart-post channel or ntfy topic. What was saved before carries over as it is."""
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from chartsrv import connections as conns
from chartsrv.store import Store

HOOK = "https://discord.com/api/webhooks/111/first-ALERTS"
HOOK2 = "https://discord.com/api/webhooks/222/second-ALERTS"
POSTS2 = "https://discord.com/api/webhooks/333/second-POSTS"
NTFY = "https://ntfy.sh/my-secret-topic-91x"   # not the note's example
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _client(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", "none")
    monkeypatch.setenv("CHART_SYMBOLS_ONLINE", "0")
    for k in ("CHART_DISCORD_WEBHOOK", "CHART_DISCORD_CHARTS_WEBHOOK", "CHART_NTFY_URL", "CHART_HEARTBEAT_URL"):
        monkeypatch.delenv(k, raising=False)
    from chartsrv.app import app
    return app, TestClient(app)


def _by(c):
    return {x["id"]: x for x in c.get("/api/connections").json()["connections"]}


def test_lists_and_more_than_one_channel(tmp_path, monkeypatch):
    sent = []

    def hooks(req):
        sent.append(req)
        return httpx.Response(200, json={"id": "1"})
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        m.notifier.client = httpx.AsyncClient(transport=httpx.MockTransport(hooks))
        by = _by(c)
        groups = list(dict.fromkeys(x["group"] for x in by.values()))
        assert groups == ["Futures feed", "Quotes", "Data", "Alerts and Discord", "App"]
        listed = [k for k, x in by.items() if x["listed"]]
        assert listed == ["ninjatrader", "ibkr", "alpaca", "coinbase", "yahoo", "fred", "eia", "symbols", "push", "discord",
                          "discord_charts", "login", "backup", "instruments", "branding", "updates"]   # the main ones; ntfy, heartbeat on the +
        assert by["discord"]["multi"] and not by["ibkr"]["multi"]

        # + Another Discord channel, with its own name
        assert c.put("/api/connections/discord", json={"discord_webhook": HOOK, "name": "Private"}).status_code == 200
        r = c.post("/api/connections", json={"type": "discord", "name": "Scalps", "discord_webhook": HOOK2})
        assert r.status_code == 200, r.text
        two = r.json()
        assert (two["id"], two["title"], two["kind"], two["extra"], two["listed"], two["state"]) == \
            ("discord-2", "Scalps", "Discord", True, True, "ok")
        assert HOOK2 not in r.text and two["fields"][0]["set"] and two["fields"][0]["source"] == "app"
        assert _by(c)["discord"]["title"] == "Private"
        for bad in ({"type": "ibkr"}, {"type": "discord"}, {"type": "discord", "discord_webhook": "https://example.com/x"},
                    {"type": "discord", "discord_webhook": HOOK2, "name": "<b>"}, {"type": "nothing"}):
            assert c.post("/api/connections", json=bad).status_code == 400, bad
        assert c.put("/api/connections/discord-9", json={"name": "x"}).status_code == 404
        assert c.put("/api/connections/discord-2", json={"name": "Scalps 2"}).json()["title"] == "Scalps 2"

        # alerts go to every channel; the delivered list names the kind
        assert c.post("/api/notify-test").json() == {"configured": ["discord"], "delivered": ["discord"]}
        assert sorted(str(q.url) for q in sent[-2:]) == [HOOK, HOOK2]
        assert c.post("/api/connections/discord-2/test", json={}).json()["ok"] and str(sent[-1].url) == HOOK2
        assert _by(c)["discord-2"]["status"].startswith("Last alert delivered")
        check = {x["id"]: x for x in c.post("/api/connections/check").json()["results"]}
        assert check["discord-2"]["ok"] and check["discord-2"]["title"] == "Discord: Scalps 2"

        # ntfy: on the + until set; a second topic
        assert c.put("/api/connections/ntfy", json={"ntfy_url": NTFY}).json()["listed"]
        assert c.post("/api/connections", json={"type": "ntfy", "ntfy_url": NTFY + "-2"}).json()["title"] == "ntfy 2"
        assert c.post("/api/notify-test").json()["configured"] == ["discord", "ntfy"]

        # chart posts: pick the channel
        c.put("/api/connections/discord_charts", json={"discord_charts_webhook": "https://discord.com/api/webhooks/9/posts"})
        c.post("/api/connections", json={"type": "discord_charts", "name": "Charts room", "discord_charts_webhook": POSTS2})
        assert c.get("/api/share/channels").json() == {"channels": [{"id": "discord_charts", "name": "Discord"},
                                                                    {"id": "discord_charts-2", "name": "Charts room"}]}
        img = base64.b64encode(PNG).decode()
        assert c.post("/api/share/discord", json={"caption": "x", "image": img, "channel": "discord_charts-2"}).status_code == 200
        assert str(sent[-1].url) == POSTS2
        assert _by(c)["discord_charts-2"]["status"].startswith("Last post")
        for ch in ("discord-2", "discord_charts-7", "nope"):
            assert c.post("/api/share/discord", json={"caption": "x", "image": img, "channel": ch}).status_code == 400

        text = c.get("/api/connections").text + c.get("/api/share/channels").text
        assert not any(u in text for u in (HOOK, HOOK2, POSTS2, NTFY))   # URLs never come back

        # remove: another channel goes; a main one stays; one that isn't main is cleared and leaves the list
        assert c.delete("/api/connections/discord-2").status_code == 204
        assert "discord-2" not in _by(c) and [u for _, _, u in m.notifier.extra if u == HOOK2] == []
        assert c.delete("/api/connections/discord").status_code == 400
        assert c.delete("/api/connections/ninjatrader").status_code == 400
        assert c.delete("/api/connections/discord-2").status_code == 404
        assert c.delete("/api/connections/ntfy").status_code == 204
        by = _by(c)
        assert not by["ntfy"]["listed"] and by["ntfy-2"]["listed"] and by["ntfy"]["fields"][0]["set"] is False

    _, client = _client(tmp_path, monkeypatch)   # kept over a restart
    with client as c:
        by = _by(c)
        assert by["discord"]["title"] == "Private" and by["discord_charts-2"]["title"] == "Charts room" and "ntfy-2" in by


def test_saved_before_carries_over(tmp_path, monkeypatch):
    """A database from before the lists: the same ids, the same values, shown and applied as they were."""
    st = Store(tmp_path / "chart.db")
    st.set_setting(conns.SETTING, {"discord": {"discord_webhook": HOOK}, "yahoo": {"enabled": False},
                                   "heartbeat": {"heartbeat_url": "https://hc.example/ping"}})
    st.close()
    _, client = _client(tmp_path, monkeypatch)
    with client as c:
        by = _by(c)
        assert by["discord"]["state"] == "ok" and by["yahoo"]["fields"][0]["value"] is False
        assert by["heartbeat"]["listed"] and by["heartbeat"]["state"] == "ok"


@pytest.mark.parametrize("cid,kind", [("discord-2", "discord"), ("discord_charts-12", "discord_charts"), ("ntfy-3", "ntfy"),
                                      ("discord-1", "discord-1"), ("discord-13", "discord-13"), ("ibkr-2", "ibkr-2")])
def test_type_of(cid, kind):
    assert conns.type_of(cid) == kind


def test_twelve_at_most(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        for n in range(2, 13):
            r = c.post("/api/connections", json={"type": "ntfy", "ntfy_url": f"{NTFY}-{n}"})
            assert r.json()["id"] == f"ntfy-{n}"
        r = c.post("/api/connections", json={"type": "ntfy", "ntfy_url": f"{NTFY}-13"})
        assert r.status_code == 400 and "12" in r.json()["detail"]
        assert json.dumps(c.get("/api/connections").json()).count('"type": "ntfy"') == 12
