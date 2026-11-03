"""Redesign: the app answers to this PC's own tailscale serve name, not to any *.ts.net name. It learns the name the
first time a browser opens the app by it over https (tailscale serve holds a certificate for that name only); a page
whose name was pointed at this PC comes over plain http, and its own address says so."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from chartsrv import auth, backup
from chartsrv.store import Store
from test_app import client  # noqa: F401  the app on the sim feed

NAMES = "ts_names"   # the stored setting: this PC's own names, newest last
PC = "pc.tail1.ts.net"
SERVE = {"host": PC, "x-forwarded-host": PC, "x-forwarded-proto": "https", "x-forwarded-for": "100.64.0.7"}   # tailscale serve
PAGE = {**SERVE, "referer": f"https://{PC}/"}    # the app's own page asking: a GET sends its address as the Referer
LIVE = {**SERVE, "origin": f"https://{PC}"}      # ...and the live connection (and every change) as the Origin
REBOUND = "evil.tail9.ts.net:8800"               # another site's name under ts.net, pointed at this PC's port
BODY = {"data": {"studies": {}}}


def _refused(c, h):
    """No way in: reading, changing, taking the login over, the live connection."""
    assert c.get("/api/journal?start=0&end=1", headers=h).status_code == 403, h
    r = c.get("/api/auth", headers=h)
    assert r.status_code == 403 and r.json()["wrong_host"] is True, h   # the page says so (app.js wrongHost)
    assert c.put("/api/auth/password", json={"new": "taken over"}, headers=h).status_code == 403, h
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers=h) as ws:
            ws.receive_json()


def _names(c):
    m = c.app.state.market
    return c.portal.call(m.store.get_setting, NAMES, None)   # on the app's own thread, where its database lives


def test_another_ts_net_name_is_refused(client):  # noqa: F811
    """A name under ts.net that isn't this PC's own is another site's, like any other name; nothing it sends can make
    it this PC's."""
    for h in ({"host": REBOUND, "origin": f"http://{REBOUND}"},   # the rebound page itself
              # its own requests may add tailscale serve's headers, but they carry its http:// address
              {"host": REBOUND, "x-forwarded-for": "100.64.0.7", "x-forwarded-proto": "https", "x-forwarded-host": PC,
               "referer": f"http://{REBOUND}/"},
              {"host": REBOUND, "x-forwarded-for": "100.64.0.7", "origin": f"http://{REBOUND}"},
              # a link or a form on that name's https page sends an https address, but no header can be added to it
              {"host": REBOUND, "referer": "https://evil.tail9.ts.net/"},
              {"host": REBOUND, "origin": "https://evil.tail9.ts.net"},
              {"host": "box.tail1.ts.net", "origin": "http://box.tail1.ts.net"},   # another machine's name on the tailnet
              {"host": "pc.tail1.ts.net.evil.example", "origin": "https://pc.tail1.ts.net"}):
        _refused(client, h)
    # this PC's name asked by another machine's https page (a real certificate, for its own name) proves nothing
    assert client.get("/api/health", headers={**SERVE, "origin": "https://box.tail1.ts.net"}).status_code == 403
    assert client.get("/api/health", headers=SERVE).status_code == 403   # a request naming no page: not yet
    assert _names(client) is None
    assert client.get("/api/auth").json()["enabled"] is False   # nothing changed
    assert client.get("/api/health", headers={"host": "127.0.0.1:8800"}).status_code == 200   # the PC's own addresses
    assert client.get("/api/health", headers={"host": "localhost:8800"}).status_code == 200


def test_the_pcs_own_name_is_learned_over_https(client):  # noqa: F811
    """The app's page opened by https://<pc>.<tailnet>.ts.net proves the name: from then on it is this PC's, for requests
    that name no page too (a program, /api/health typed into Safari). Other names stay refused."""
    assert client.get("/api/health", headers=SERVE).status_code == 403
    r = client.get("/api/auth", headers=PAGE)
    assert r.status_code == 200 and r.json()["signed_in"] is True
    assert _names(client) == [PC]
    for h in (SERVE, {"host": PC}, {"host": "PC.Tail1.ts.net."}, {**SERVE, "host": f"{PC}:8443"}):
        assert client.get("/api/health", headers=h).status_code == 200, h
    assert client.put("/api/templates/chart/x", json=BODY, headers=LIVE).status_code == 200
    with client.websocket_connect("/ws", headers=LIVE) as ws:
        assert ws.receive_json()["type"] == "hello"
    _refused(client, {"host": REBOUND, "origin": f"http://{REBOUND}"})
    _refused(client, {**SERVE, "host": "box.tail1.ts.net", "x-forwarded-host": "box.tail1.ts.net"})
    # a renamed PC or tailnet: its new name, by the live connection's Origin; the old one stays
    new = "pc.tail-new.ts.net"
    with client.websocket_connect("/ws", headers={**SERVE, "host": new, "x-forwarded-host": new,
                                                  "origin": f"https://{new}"}) as ws:
        assert ws.receive_json()["type"] == "hello"
    assert _names(client) == [PC, new]
    assert client.get("/api/health", headers={"host": PC}).status_code == 200


def test_the_names_kept_are_the_newest(client):  # noqa: F811
    names = [f"pc{i}.tail1.ts.net" for i in range(10)]
    for n in names:
        h = {**SERVE, "host": n, "x-forwarded-host": n, "referer": f"https://{n}/x"}   # tailscale serve: the name in both
        assert client.get("/api/health", headers=h).status_code == 200, n
    assert _names(client) == names[-8:]
    assert client.get("/api/health", headers={"host": names[-1]}).status_code == 200
    assert client.get("/api/health", headers={"host": names[0]}).status_code == 403   # gone: proved again if still used


def _start(tmp_path, monkeypatch):
    """The app on a database an older version made (the update: same database, new server)."""
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", "none")
    monkeypatch.setenv("CHART_QUOTES_FEED", "none")
    monkeypatch.setenv("CHART_REMOTE_NEEDS_PASSWORD", "1")   # the default; an install from before isn't locked by it
    from chartsrv.app import app
    return TestClient(app)


@pytest.mark.parametrize("password", [False, True])
def test_an_existing_phone_keeps_working_after_the_update(tmp_path, monkeypatch, password):
    """An install from before, its phone on https://pc.tail1.ts.net (signed in, or no password set): after the update
    the phone's app works with nothing done, the page it kept (older app.js) as well as an open one reconnecting.
    Its settings and sign-ins stay; the name is kept for the next start. A rebound name is refused."""
    st = Store(tmp_path / "chart.db")
    st.set_setting("connections", {"discord": {"discord_webhook": "https://discord.com/api/webhooks/1/x"}})
    cookie = {}
    if password:
        a = auth.Auth(st)
        a.set_password("correct horse")
        cookie = {"cookie": f"{auth.COOKIE}={a.start('iPhone')}"}
    st.close()
    with _start(tmp_path, monkeypatch) as c:
        if password:   # the page it kept opens: its first question, with the phone's cookie
            r = c.get("/api/auth", headers={**PAGE, **cookie})
            assert r.status_code == 200 and r.json()["signed_in"] is True and r.json()["label"] == "iPhone"
        with c.websocket_connect("/ws", headers={**LIVE, **cookie}) as ws:   # an open page reconnecting
            assert ws.receive_json()["type"] == "hello"
        assert c.get("/api/auth", headers={**PAGE, **cookie}).json()["signed_in"] is True
        assert c.put("/api/templates/chart/x", json=BODY, headers={**LIVE, **cookie}).status_code == 200
        assert c.get("/api/health", headers={**SERVE, **cookie}).status_code == 200
        _refused(c, {"host": REBOUND, "origin": f"http://{REBOUND}"})
        assert _names(c) == [PC]
    with _start(tmp_path, monkeypatch) as c:   # the next start: the name is still this PC's
        assert c.get("/api/health", headers={**SERVE, **cookie}).status_code == 200
    st = Store(tmp_path / "chart.db")
    assert st.get_setting("connections")["discord"]["discord_webhook"].endswith("/1/x")
    assert (st.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1) is password
    st.close()


def test_a_restore_keeps_this_pcs_names(tmp_path):
    """The names describe this PC: a backup from another PC (or from before, which has none) leaves them as they are."""
    s = Store(tmp_path / "chart.db")
    s.set_setting(NAMES, ["oldpc.tail1.ts.net"])
    s.set_setting("watchlists_probe", {"a": 1})
    other = backup.make(s.path, tmp_path / "bk")
    s.db.execute("DELETE FROM settings WHERE key=?", (NAMES,))
    s.db.commit()
    before = backup.make(s.path, tmp_path / "bk", "-before")   # an older version's: no names in it
    s.set_setting(NAMES, [PC])
    s.set_setting("watchlists_probe", {"a": 2})
    s.close()
    for bk in (other, before):
        backup.restore(s.path, bk)
        s = Store(s.path)
        assert s.get_setting("watchlists_probe") == {"a": 1}   # what you made comes back
        assert s.get_setting(NAMES) == [PC], bk.name
        s.close()
