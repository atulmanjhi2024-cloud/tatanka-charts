"""Post to Discord: a chart picture and caption to the chart-posts webhook, which is its own Connections entry."""
import base64
import json

import httpx
from fastapi.testclient import TestClient

from chartsrv import connections as conns

HOOK = "https://discord.com/api/webhooks/9876/chartPOSTS_xyz-1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _client(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", "none")
    monkeypatch.setenv("CHART_SYMBOLS_ONLINE", "0")
    monkeypatch.delenv("CHART_DISCORD_CHARTS_WEBHOOK", raising=False)
    from chartsrv.app import app
    return app, TestClient(app)


def test_post_chart_to_discord(tmp_path, monkeypatch):
    sent = []

    def discord(req):
        sent.append(req)
        return httpx.Response(200, json={"id": "1"})
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        m.notifier.client = httpx.AsyncClient(transport=httpx.MockTransport(discord))
        img = base64.b64encode(PNG).decode()
        r = c.post("/api/share/discord", json={"caption": "NQ 5m", "image": img})
        assert r.status_code == 400 and "Connections" in r.json()["detail"]   # no webhook yet
        assert c.put("/api/connections/discord_charts", json={"discord_charts_webhook": "https://example.com/x"}).status_code == 400
        row = c.put("/api/connections/discord_charts", json={"discord_charts_webhook": HOOK}).json()
        assert row["fields"][0]["set"] and HOOK not in json.dumps(row)   # the URL never comes back
        assert m.s.discord_charts_webhook == HOOK and m.notifier.discord != HOOK   # not the alert channel
        r = c.post("/api/share/discord", json={"caption": "NQ 5m @everyone", "image": f"data:image/png;base64,{img}"})
        assert r.status_code == 200, r.text
        req = sent[-1]
        assert str(req.url) == HOOK and req.headers["content-type"].startswith("multipart/form-data")
        body = req.content
        assert b'"allowed_mentions": {"parse": []}' in body and b"NQ 5m @everyone" in body and PNG in body
        assert c.post("/api/share/discord", json={"caption": "x", "image": base64.b64encode(b"GIF89a").decode()}).status_code == 400
        assert c.post("/api/share/discord", json={"caption": "x", "image": "not base64!"}).status_code == 400
        status = next(x for x in c.get("/api/connections").json()["connections"] if x["id"] == "discord_charts")
        assert status["state"] == "ok" and status["status"].startswith("Last post")
        test = c.post("/api/connections/discord_charts/test", json={}).json()
        assert test["ok"] and json.loads(sent[-1].content)["content"] == "Chart posts are wired up."
        n = len(sent)
        check = next(x for x in c.post("/api/connections/check").json()["results"] if x["id"] == "discord_charts")
        assert check["ok"] and len(sent) == n   # Check all never posts in that channel

        m.notifier.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(404)))
        assert c.post("/api/share/discord", json={"caption": "", "image": img}).status_code == 502
    assert conns.clean("discord_charts", {"discord_charts_webhook": f" {HOOK} "}) == {"discord_charts_webhook": HOOK}
