"""Bug hunt, API and install: a page whose name was pointed at this PC, the session renewal, the journal's earlier
fills, a sign-in's body, expired sessions, the update script, the container's log."""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from chartsrv import auth
from test_app import client  # noqa: F401  the app on the sim feed

ROOT = Path(__file__).resolve().parents[2]

# another site's page whose name now points at 127.0.0.1 (DNS rebinding): its own Host and a matching Origin
EVIL = {"host": "evil.test:8800", "origin": "http://evil.test:8800"}


def test_a_rebound_name_is_refused(client):  # noqa: F811
    """No password set: the page still can't read, change or take the app over; the app's own addresses still work."""
    assert client.get("/api/journal?start=0&end=1", headers=EVIL).status_code == 403
    r = client.get("/api/auth", headers=EVIL)
    assert r.status_code == 403 and r.json()["wrong_host"] is True   # the page says so, and stops
    assert client.put("/api/auth/password", json={"new": "taken over"}, headers=EVIL).status_code == 403
    assert client.put("/api/connections/heartbeat", json={}, headers={**EVIL, "x-forwarded-host": "localhost"}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers=EVIL) as ws:
            ws.receive_json()
    assert client.get("/api/auth").json()["enabled"] is False   # nothing changed
    for host in ("localhost:8800", "127.0.0.1:8800", "[::1]:8800", "192.168.1.5:8800", "100.64.0.7", "localhost.",
                 "pc.tail1.ts.net", "PC.tail1.ts.net."):
        ts = ".ts.net" in host.lower()   # this PC's tailscale serve name: its page comes over https, through it
        h = {"host": host, "origin": ("https://" if ts else "http://") + host,
             **({"x-forwarded-for": "100.64.0.7", "x-forwarded-proto": "https"} if ts else {})}
        assert client.get("/api/health", headers=h).status_code == 200, host
        assert client.put("/api/templates/chart/x", json={"data": {"studies": {}}}, headers=h).status_code == 200, host
    with client.websocket_connect("/ws", headers={"host": "pc.tail1.ts.net", "origin": "https://pc.tail1.ts.net"}) as ws:
        assert ws.receive_json()["type"] == "hello"
    assert client.get("/", headers=EVIL).status_code == 200   # the app's own files: nothing of yours in them


def test_a_rebound_name_with_no_dots_is_refused(client):  # noqa: F811
    """A name with no dots is looked up through the network's own suffix (or LLMNR, NetBIOS on Windows), which
    whoever runs that network answers: a public Wi-Fi can point it at this PC like any other name."""
    for host in ("evil:8800", "wpad", "evil.", "mypc"):
        h = {"host": host, "origin": "http://" + host}
        assert client.get("/api/journal?start=0&end=1", headers=h).status_code == 403, host
        assert client.put("/api/connections/heartbeat", json={}, headers=h).status_code == 403, host
        assert client.put("/api/auth/password", json={"new": "taken over"}, headers=h).status_code == 403, host
    assert client.get("/api/auth").json()["enabled"] is False   # nothing changed


@pytest.fixture
def own_domain(monkeypatch):
    monkeypatch.setenv("CHART_ALLOWED_HOSTS", "Charts.Example.com, other.example, mypc")


def test_allowed_hosts(own_domain, client):  # noqa: F811
    """Your own name for it (the PC's, a proxy of yours in front): CHART_ALLOWED_HOSTS."""
    h = {"host": "charts.example.com", "origin": "https://charts.example.com"}
    assert client.get("/api/health", headers=h).status_code == 200
    assert client.get("/api/health", headers={"host": "other.example:8443"}).status_code == 200
    assert client.get("/api/health", headers={"host": "mypc:8800", "origin": "http://mypc:8800"}).status_code == 200
    assert client.get("/api/health", headers=EVIL).status_code == 403


def test_a_renewal_sends_the_cookie_again(client, monkeypatch):  # noqa: F811
    """A device in use stays signed in: the day's renewal must reach the browser's cookie. A page load asks /api/auth
    first and then opens /ws; neither may use the renewal up without sending the cookie."""
    client.put("/api/auth/password", json={"new": "correct horse"})
    real, page = time.time, {"referer": "http://testserver/"}   # the app's page, which its requests name

    def days_later(n):   # the login's clock only
        monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: real() + n * 86400))
    days_later(2)
    r = client.get("/api/auth", headers=page)
    assert r.json()["signed_in"] and auth.COOKIE in r.headers.get("set-cookie", "")
    days_later(4)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
    assert auth.COOKIE in client.get("/api/health", headers=page).headers.get("set-cookie", "")
    assert "set-cookie" not in client.get("/api/health", headers=page).headers   # once a day


def test_a_sign_in_body_is_small(client):  # noqa: F811
    """The sign-in is open to devices not signed in: a body far bigger than a password is refused before it is read
    whole, with a length given or sent in chunks."""
    client.put("/api/auth/password", json={"new": "correct horse"})
    client.cookies.clear()
    js = {"content-type": "application/json"}
    big = b'{"password": "' + b"x" * 1_000_000 + b'"}'
    assert client.post("/api/auth/login", content=big, headers=js).status_code == 413

    def chunks():   # no Content-Length
        yield b'{"password": "'
        for _ in range(100):
            yield b"x" * 10_000
        yield b'"}'
    assert client.post("/api/auth/login", content=chunks(), headers=js).status_code == 413
    assert client.post("/api/auth/login", json={"password": "\U0001F402" * 200, "label": "é" * 60}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "correct horse", "label": "é" * 60}).status_code == 200


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


UPDATERS = {"update.sh": ["sh"], "tools/update.ps1": ["pwsh", "-NoProfile", "-NonInteractive", "-File"]}
updaters = pytest.mark.parametrize("script", [
    pytest.param("update.sh", marks=pytest.mark.skipif(not shutil.which("sh"), reason="sh")),
    pytest.param("tools/update.ps1", marks=pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh")),
])


def _install(tmp_path, monkeypatch, files: dict, script: str):
    """Your copy (charting), cloned from a stand-in upstream (up) holding `files` and the updaters, and a stand-in
    docker that notes what it is asked. Returns up, your copy, and a function that runs `script` in your copy."""
    if os.name == "nt" or not shutil.which("git"):
        pytest.skip("git, and sh for the stand-in docker")
    for k, v in {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                 "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                 "HOME": str(tmp_path)}.items():   # HOME: update.ps1 looks for NinjaTrader in Documents
        monkeypatch.setenv(k, v)
    up, me, bin_ = tmp_path / "up", tmp_path / "charting", tmp_path / "bin"
    for name, text in {**files, **{u: (ROOT / u).read_text() for u in UPDATERS}}.items():
        (up / name).parent.mkdir(parents=True, exist_ok=True)
        (up / name).write_text(text)
    _git(up, "init", "-q", "-b", "main")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "one")
    _git(tmp_path, "clone", "-q", str(up), str(me))
    bin_.mkdir()
    (bin_ / "docker").write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/docker.txt"\n')
    (bin_ / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")

    def update():
        return subprocess.run([*UPDATERS[script], str(me / script)], capture_output=True, text=True, timeout=120)
    return up, me, update


@updaters
def test_update_keeps_your_config_edits(tmp_path, monkeypatch, script):
    """docs/DATA.md has you edit config/holidays.json; the update then still goes through. Your change stays on the
    new version; where the update changed the same lines, the update's is used and yours is kept in git's stash."""
    five = "".join(f'"{d}": "{d}",\n' for d in "abcde")
    up, me, update = _install(tmp_path, monkeypatch, {"config/holidays.json": five}, script)
    days, mine = up / "config" / "holidays.json", me / "config" / "holidays.json"

    days.write_text(days.read_text().replace('"e": "e"', '"e": "new year"'))   # the update
    _git(up, "commit", "-qam", "two")
    mine.write_text(mine.read_text().replace('"a": "a"', '"a": "my hours"'))   # yours, as DATA.md says
    r = update()
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"a": "my hours"' in mine.read_text() and '"e": "new year"' in mine.read_text()
    assert (tmp_path / "docker.txt").read_text().startswith("compose up -d --build")

    days.write_text(days.read_text().replace('"c": "c"', '"c": "fixed"'))
    _git(up, "commit", "-qam", "three")
    mine.write_text(mine.read_text().replace('"c": "c"', '"c": "mine"'))
    r = update()
    assert r.returncode == 0, r.stdout + r.stderr
    assert mine.read_text() == days.read_text() and "config/holidays.json" in r.stdout   # no conflict marks built in
    assert '"c": "mine"' in _git(me, "stash", "show", "-p")
    assert len((tmp_path / "docker.txt").read_text().splitlines()) == 2


@updaters
def test_update_removes_a_file_you_changed(tmp_path, monkeypatch, script):
    """INDICATORS.md has you edit config/nt_settings.json; an update that removes it still goes through, yours kept
    in git's stash, and so does the next update (nothing is left half merged for its git pull to stop at)."""
    up, me, update = _install(tmp_path, monkeypatch, {"config/nt_settings.json": '{"a": 1}\n',
                                                      "config/holidays.json": "{}\n"}, script)
    _git(up, "rm", "-q", "config/nt_settings.json")   # the update
    _git(up, "commit", "-qm", "two")
    (me / "config" / "nt_settings.json").write_text('{"a": 2}\n')   # yours
    r = update()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "config/nt_settings.json" in r.stdout and '"a": 2' in _git(me, "stash", "show", "-p")
    assert _git(me, "status", "--porcelain") == "" and not (me / "config" / "nt_settings.json").exists()
    (up / "config" / "holidays.json").write_text('{"days": []}\n')
    _git(up, "commit", "-qam", "three")
    r = update()
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(me, "rev-parse", "HEAD") == _git(up, "rev-parse", "HEAD")
    assert len((tmp_path / "docker.txt").read_text().splitlines()) == 2


def test_the_container_log_is_capped():
    """Docker keeps a container's output for good unless told otherwise, and every request is a line of it (a
    footprint chart asks once a second): the chart service rotates its log."""
    compose = (ROOT / "docker-compose.yml").read_text()
    chart = compose.split("\n  chart:\n", 1)[1].split("\nvolumes:", 1)[0]
    m = re.search(r'\n    logging:[^\n]*\n      driver: json-file\n      options:\n        max-size: "(\d+)m"\n'
                  r'        max-file: "(\d+)"\n', chart)
    assert m and int(m[1]) * int(m[2]) <= 100
