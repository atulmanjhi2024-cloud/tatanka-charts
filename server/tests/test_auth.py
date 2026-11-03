"""Login: off until a password is set; then /api and /ws need a signed-in device."""
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from chartsrv import auth
from test_app import client  # noqa: F401  the app on the sim feed


def test_login(client):  # noqa: F811
    assert client.get("/api/health").status_code == 200
    first = client.get("/api/backups").json()
    for _ in range(100):   # the start's first backup is written in the background: a slow machine is still at it
        if first["files"]:
            break
        time.sleep(0.05)
        first = client.get("/api/backups").json()
    assert first["download"] is False and client.get(f"/api/backups/{first['files'][0]['name']}").status_code == 403
    assert client.get("/api/auth").json() == {"enabled": False, "signed_in": True, "label": "", "session": ""}
    assert client.put("/api/auth/password", json={"new": "short"}).status_code == 400
    r = client.put("/api/auth/password", json={"new": "correct horse", "label": "PC · app"})
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/api/health").status_code == 200   # this device stays signed in
    name = client.get("/api/backups").json()["files"][0]["name"]
    assert client.get(f"/api/backups/{name}").status_code == 200 and client.get("/api/backups/x.db").status_code == 404
    mine = dict(client.cookies)

    client.cookies.clear()   # another device
    assert client.get("/api/health").status_code == 401 and client.get("/").status_code == 200
    assert client.get("/api/auth").json()["signed_in"] is False
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
    assert e.value.code == 4401
    assert client.post("/api/auth/login", json={"password": "wrong one!"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "correct horse", "label": "iPhone · app"}).status_code == 200
    assert client.get("/api/health").status_code == 200
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
    ss = client.get("/api/auth/sessions").json()["sessions"]
    assert [(s["label"], s["current"]) for s in ss if s["current"]] == [("iPhone · app", True)] and len(ss) == 2
    assert "correct horse" not in client.get("/api/connections").text

    assert client.put("/api/auth/password", json={"current": "nope", "new": "another one"}).status_code == 403
    assert client.post("/api/auth/sessions/end-others").json() == {"ended": 1}
    client.cookies.clear()
    client.cookies.update(mine)   # the PC was signed out
    assert client.get("/api/health").status_code == 401

    for _ in range(auth.FAIL_MAX - 1):   # one wrong try was made above
        client.post("/api/auth/login", json={"password": "guessing"})
    r = client.post("/api/auth/login", json={"password": "correct horse"})
    assert r.status_code == 429 and "try again" in r.json()["detail"]


def test_turn_off_and_cli(client, monkeypatch):  # noqa: F811
    client.put("/api/auth/password", json={"new": "correct horse"})
    assert client.put("/api/auth/password", json={"current": "correct horse", "new": None}).json() == {"enabled": False}
    client.cookies.clear()
    assert client.get("/api/health").status_code == 200
    client.put("/api/auth/password", json={"new": "correct horse"})
    client.cookies.clear()
    assert client.get("/api/health").status_code == 401
    assert auth.main(["off"]) == 0   # forgot it: on the PC
    assert client.get("/api/health").status_code == 200
    answers = iter(["new password1", "new password1"])
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt="": next(answers))
    assert auth.main(["set"]) == 0 and client.get("/api/health").status_code == 401
    assert client.post("/api/auth/login", json={"password": "new password1"}).status_code == 200


def test_lockout_holds(client):  # noqa: F811
    """Wrong passwords sent all at once still lock; the address is the proxy's (last) X-Forwarded-For entry; the
    password change asks the same limit; many addresses together lock too."""
    import asyncio

    import httpx
    client.put("/api/auth/password", json={"new": "correct horse"})
    client.cookies.clear()

    async def burst():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app, client=("100.64.0.9", 1234)),
                                     base_url="http://testserver") as c:
            rs = await asyncio.gather(*(c.post("/api/auth/login", json={"password": f"guess{i}xx"}) for i in range(40)))
            return [r.status_code for r in rs]
    codes = client.portal.call(burst)
    assert codes.count(401) == auth.FAIL_MAX and codes.count(429) == 40 - auth.FAIL_MAX
    # signed in on this device (another address), to try the password change form below
    assert client.post("/api/auth/login", json={"password": "correct horse"}, headers={"x-forwarded-for": "100.64.0.50"}).status_code == 200
    # a made-up first entry doesn't make a new address: the proxy's entry (last) is the one counted
    spoof = {"x-forwarded-for": "10.0.0.1, 100.64.0.7"}
    for i in range(auth.FAIL_MAX):
        client.post("/api/auth/login", json={"password": f"g{i}xxxxx"}, headers={**spoof, "x-forwarded-for": f"10.0.0.{i}, 100.64.0.7"})
    assert client.post("/api/auth/login", json={"password": "correct horse"}, headers=spoof).status_code == 429
    r = client.put("/api/auth/password", json={"current": "nope nope", "new": "another one"}, headers=spoof)
    assert r.status_code == 429   # guessing through the change form waits too
    # addresses rotating: all logins wait once FAIL_ALL wrong ones are in the window
    m = client.app.state.market
    m.auth.fails.clear()
    for i in range(auth.FAIL_ALL):
        client.post("/api/auth/login", json={"password": f"g{i}xxxxx"}, headers={"x-forwarded-for": f"100.64.1.{i}"})
    assert client.post("/api/auth/login", json={"password": "correct horse"}, headers={"x-forwarded-for": "100.64.2.1"}).status_code == 429
    assert len(m.auth.fails) <= auth.FAIL_ALL
    m.auth.fails.clear()   # a right password clears its address
    assert client.post("/api/auth/login", json={"password": "correct horse"}, headers=spoof).status_code == 200
    assert "100.64.0.7" not in m.auth.fails


def test_odd_local_token(client):  # noqa: F811
    client.put("/api/auth/password", json={"new": "correct horse"})
    client.cookies.clear()
    assert client.get("/api/health", headers={"x-local-token": "caf\xe9".encode("latin-1")}).status_code == 401   # not a 500


def test_expired_sessions_are_not_listed(tmp_path):
    """A device not used for SESSION_DAYS is signed out; it no longer shows as signed in, nor counts on the card."""
    from chartsrv.store import Store
    st = Store(tmp_path / "c.db")
    a = auth.Auth(st)
    a.start("PC")
    a.start("old phone")
    with st.db:
        st.db.execute("UPDATE sessions SET seen=seen-? WHERE label='old phone'", ((auth.SESSION_DAYS + 1) * 86_400_000,))
    assert [s["label"] for s in a.sessions()] == ["PC"]
    assert st.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    st.close()
