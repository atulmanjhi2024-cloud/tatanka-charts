"""Fourth bug-hunt fixes in the login's checks: the path they look at (an odd Host on Starlette before 1.1.0, a path
prefix), and the port an address with no port is on (the app on port 80, a proxy of yours on another port)."""
import re
from pathlib import Path

import pytest
from starlette.datastructures import URL
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from test_app import client  # noqa: F401  the app on the sim feed

ROOT = Path(__file__).resolve().parents[2]
BODY = {"data": {"studies": {}}}
HOOK = {"discord_webhook": "https://discord.com/api/webhooks/1/abc"}


def _signed_out(client):  # noqa: F811
    assert client.put("/api/auth/password", json={"new": "a good password", "label": "PC"}).status_code == 200
    client.cookies.clear()   # another device, not signed in
    assert client.get("/api/connections").status_code == 401


def _old_starlette_url(self):
    """request.url as Starlette before 1.1.0 built it: the Host header as sent, unchecked, in front of the path."""
    host = dict(self.scope["headers"]).get(b"host", b"").decode("latin-1")
    return URL(f"{self.scope.get('scheme', 'http')}://{host}{self.scope['path']}")


def test_an_odd_host_still_needs_the_login(client, monkeypatch):  # noqa: F811
    """The login and the address and page checks looked at request.url.path, which Starlette before 1.1.0 (what pip
    keeps when it is already installed) built from the Host as sent: 'x#' or 'x?' gave '', 'x/y' '/y/api/...'. That
    is no /api path, so every /api route ran with no password, for anyone who reaches the port."""
    _signed_out(client)
    monkeypatch.setattr(HTTPConnection, "url", property(_old_starlette_url))
    for host in ("testserver#", "testserver?", "x/y", "localhost:8800#"):
        h = {"host": host}
        assert client.get("/api/connections", headers=h).status_code in (401, 403), host
        assert client.get("/api/backups", headers=h).status_code in (401, 403), host
        assert client.put("/api/connections/discord", json=HOOK, headers={**h, "origin": "https://evil.example"}).status_code == 403, host
    pin = re.search(r"^starlette(?:==|>=)(\d+)\.(\d+)\.", (ROOT / "server" / "requirements.txt").read_text(), re.M)
    assert pin and (int(pin[1]), int(pin[2])) >= (1, 1)   # 1.1.0 or newer: it checks the Host


def test_a_path_prefix_still_needs_the_login(client):  # noqa: F811
    """Under a path prefix (uvicorn --root-path /charts, a proxy's /charts/ in front) every path starts with it, and
    the routes are found without it: the checks must look at the path the route is found by."""
    _signed_out(client)
    client._transport.root_path = "/charts"   # what --root-path does to every request (the client's own setting)
    assert client.get("/charts/api/auth").json()["enabled"] is True   # the routes answer under the prefix
    assert client.get("/charts/api/connections").status_code == 401
    assert client.get("/charts/api/backups").status_code == 401
    assert client.put("/charts/api/connections/discord", json=HOOK, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.put("/charts/api/connections/discord", json=HOOK, headers={"host": "evil.example"}).status_code == 403


def _check(client, mine, others):  # noqa: F811
    for h in others:
        assert client.put("/api/templates/chart/x", json=BODY, headers=h).status_code == 403, h
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers=h) as ws:
                ws.receive_json()
    for h in mine:
        assert client.put("/api/templates/chart/x", json=BODY, headers=h).status_code == 200, h
        with client.websocket_connect("/ws", headers=h) as ws:
            assert ws.receive_json()["type"] == "hello", h


@pytest.fixture
def old_serve_name(monkeypatch):
    monkeypatch.setenv("CHART_ALLOWED_HOSTS", "testserver, pc.tail1.ts.net")   # a tailscale serve before 1.44's name


def test_a_page_on_the_other_default_port_is_another_site(old_serve_name, client):  # noqa: F811
    """A Host with no port is on the default port of the scheme the request came in on, not of the page's. With the
    app on port 80 (CHART_PORT=80, which docker-compose.yml publishes on 127.0.0.1 only), a page another program on
    the PC shows on https://localhost (port 443) is another site's. A Host that came through a proxy naming no scheme
    (tailscale serve before 1.44 passed the Host and X-Forwarded-For only, over https too; its name goes in
    CHART_ALLOWED_HOSTS) still takes the page's."""
    _check(client,
           mine=[{"host": "localhost", "origin": "http://localhost"},
                 {"host": "127.0.0.1", "origin": "http://127.0.0.1"},
                 {"host": "pc.tail1.ts.net", "x-forwarded-for": "100.64.0.7", "origin": "https://pc.tail1.ts.net"}],
           others=[{"host": "localhost", "origin": "https://localhost"},
                   {"host": "127.0.0.1", "origin": "https://127.0.0.1"},
                   {"host": "[::1]", "origin": "https://[::1]"}])


@pytest.fixture
def proxy_name(monkeypatch):
    monkeypatch.setenv("CHART_ALLOWED_HOSTS", "testserver, charts.home")


NGINX = {"host": "charts.home", "x-forwarded-proto": "https", "x-forwarded-for": "192.168.1.20"}   # Host $host: no port


def test_a_proxy_on_another_port(proxy_name, client):  # noqa: F811
    """nginx on port 8443 with 'proxy_set_header Host $host' passes the Host without its port, which then read as
    443 and refused every change from the app's own page. X-Forwarded-Port ($server_port) says which port it is;
    Host $http_host keeps it (docs/PHONE.md). A page on another port of that name is still another site's."""
    _check(client,
           mine=[{**NGINX, "x-forwarded-port": "8443", "origin": "https://charts.home:8443"},
                 {**NGINX, "host": "charts.home:8443", "origin": "https://charts.home:8443"},
                 {**NGINX, "origin": "https://charts.home"}],   # nginx on 443
           others=[{**NGINX, "x-forwarded-port": "8443", "origin": "https://charts.home"},
                   {**NGINX, "x-forwarded-port": "8443", "origin": "https://charts.home:9443"},
                   {**NGINX, "host": "charts.home:8443", "x-forwarded-port": "9443", "origin": "https://charts.home:9443"},
                   {**NGINX, "x-forwarded-port": "port", "origin": "https://charts.home"},   # refused, not a 500
                   {**NGINX, "origin": "https://charts.home:8443"}])   # neither says the port: 443
