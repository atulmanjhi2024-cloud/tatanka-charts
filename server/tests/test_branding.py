"""Branding: the app's name and logo, and "Powered by Tatanka Charts" on chart pictures, set in Connections >
Branding; Tatanka's by default."""
import base64
import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient

from chartsrv import branding

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
FLIP = not branding.DEFAULTS["powered"]   # "Powered by" the other way from the default
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def _client(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", "none")
    monkeypatch.setenv("CHART_SYMBOLS_ONLINE", "0")
    from chartsrv.app import app
    return app, TestClient(app)


def test_branding(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        b = c.get("/api/branding").json()
        assert b == {**branding.DEFAULTS, "logo": False, "logo_v": 0}   # Tatanka's, as before
        assert c.get("/api/branding/logo").status_code == 404
        card = next(x for x in c.get("/api/connections").json()["connections"] if x["id"] == "branding")
        assert card["status"] == "Tatanka · built in" and card["branding"]["name"] == "Tatanka"

        for bad in ({"name": ""}, {"name": "x" * 25}, {"name": "<b>"}, {"powered": "yes"},
                    {"colour": "red"}, {"logo": base64.b64encode(b"GIF89a").decode()},
                    {"logo": "not base64!"}, {"logo": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200_001).decode()}):
            assert c.put("/api/branding", json=bad).status_code == 400, bad
        assert c.get("/api/branding").json()["name"] == "Tatanka"   # nothing half-saved

        with c.websocket_connect("/ws") as ws:
            r = c.put("/api/branding", json={"name": " Acme ", "powered": FLIP,
                                             "logo": "data:image/png;base64," + base64.b64encode(PNG).decode()})
            assert r.status_code == 200, r.text
            while (msg := ws.receive_json())["type"] != "branding":
                pass
        b = r.json()
        assert b == {**branding.DEFAULTS, "name": "Acme", "powered": FLIP, "logo": True,
                     "logo_v": hashlib.sha1(PNG).hexdigest()[:12]}   # its version from its content: never an old logo's
        assert msg["branding"] == b
        logo = c.get(f"/api/branding/logo?v={b['logo_v']}")
        assert logo.content == PNG and logo.headers["content-type"] == "image/png" and logo.headers["x-content-type-options"] == "nosniff"
        card = next(x for x in c.get("/api/connections").json()["connections"] if x["id"] == "branding")
        assert card["status"] == "Acme · own name, powered, logo"

        assert c.put("/api/branding", json={"logo": base64.b64encode(WEBP).decode()}).json()["logo_v"] == hashlib.sha1(WEBP).hexdigest()[:12]
        assert c.get("/api/branding/logo").headers["content-type"] == "image/webp"
        assert c.get("/api/branding").json()["name"] == "Acme"   # a logo alone keeps the rest

        # chart posts go to Discord under the name
        sent = []
        m.notifier.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(200)))
        c.put("/api/connections/discord_charts", json={"discord_charts_webhook": "https://discord.com/api/webhooks/1/abc"})
        assert c.post("/api/share/discord", json={"caption": "x", "image": base64.b64encode(PNG).decode()}).status_code == 200
        assert b'"username": "Acme Charts"' in sent[-1].content
        assert all(r["id"] != "branding" for r in c.post("/api/connections/check").json()["results"])   # nothing to check

        # open before signing in: the sign-in shows the name; changing it needs a signed-in device
        assert c.put("/api/auth/password", json={"new": "correct horse"}).status_code == 200
        c.cookies.clear()
        assert c.get("/api/branding").json()["name"] == "Acme" and c.get("/api/branding/logo").status_code == 200
        assert c.put("/api/branding", json={"name": "Hacked"}).status_code == 401

    _, client = _client(tmp_path, monkeypatch)   # kept over a restart; back to the built-in ones
    with client as c:
        c.post("/api/auth/login", json={"password": "correct horse"})
        assert c.get("/api/branding").json()["name"] == "Acme"
        b = c.put("/api/branding", json={**branding.DEFAULTS, "logo": None}).json()
        assert b == {**branding.DEFAULTS, "logo": False, "logo_v": 0} and c.get("/api/branding/logo").status_code == 404


def test_name_and_logo_only():
    """No link behind the logo and no signature on chart pictures: the name, the logo, and "Powered by Tatanka Charts"
    on the pictures until the card turns it off."""
    want = {"name": "Tatanka", "powered": True}
    assert branding.DEFAULTS == want
    for k in {"link", "signature"} - want.keys():
        with pytest.raises(ValueError, match="no branding setting"):
            branding.clean({k: "x"}, dict(want))


@pytest.mark.parametrize("v", ["", "   "])
def test_the_name_may_not_be_empty(v):
    with pytest.raises(ValueError):
        branding.clean({"name": v}, {**branding.DEFAULTS})


def test_only_what_differs_is_stored():
    class Store(dict):
        def get_setting(self, k, default=None):
            return self.get(k, default)

        def set_setting(self, k, v):
            self[k] = v
    st = Store()
    b = branding.Branding(st)
    b.put({"name": "Acme", "powered": branding.DEFAULTS["powered"]})
    assert st[branding.SETTING] == {"name": "Acme"}
    b.put({"name": "Tatanka"})
    assert st[branding.SETTING] == {} and b.get()["name"] == "Tatanka"
