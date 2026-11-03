"""Fifth bug-hunt fixes in the address checks: a proxy that passes the browser's address in X-Forwarded-Host (Apache's
defaults), and a ts.net name "proved" through a proxy of yours on plain http."""
import pytest

from test_app import client  # noqa: F401  the app on the sim feed
from test_redesign_tsnet_host import PC, SERVE, _names, _refused

BODY = {"data": {"studies": {}}}
EVIL_TS = "evil.tail9.ts.net"   # another site's name under ts.net, pointed at this PC


def APACHE(name):
    """Apache mod_proxy's defaults (ProxyPreserveHost Off, ProxyAddHeaders On): the Host is the ProxyPass upstream, the
    browser's address goes in X-Forwarded-Host."""
    return {"host": "127.0.0.1:8800", "x-forwarded-host": name, "x-forwarded-for": "192.168.1.20",
            "x-forwarded-server": "pc.lan"}


@pytest.fixture
def lan_name(monkeypatch):
    monkeypatch.setenv("CHART_ALLOWED_HOSTS", "testserver, lan.pc")   # your proxy's name for the PC


def _works(c, h, origin):
    assert c.get("/api/health", headers=h).status_code == 200, h
    assert c.put("/api/templates/chart/x", json=BODY, headers={**h, "origin": origin}).status_code == 200, h
    with c.websocket_connect("/ws", headers={**h, "origin": origin}) as ws:
        assert ws.receive_json()["type"] == "hello", h


def test_a_proxy_that_passes_the_address_in_x_forwarded_host(lan_name, client):  # noqa: F811
    """Behind Apache with its defaults the Host is the app's own (127.0.0.1:8800, an IP address), and the name the
    browser asked for comes in X-Forwarded-Host, which the page check takes as this app's. A rebound page's name passed
    there whatever CHART_ALLOWED_HOSTS said: it read, changed, set a password, opened the live connection. Every name
    in it must be one of this app's, like the Host's; yours, in CHART_ALLOWED_HOSTS, still works."""
    for name in ("evil.example", "other.lan", EVIL_TS, "lan.pc, evil.example", "evil.example, lan.pc"):
        page = name.split(",")[0].strip()
        _refused(client, {**APACHE(name), "origin": f"http://{page}"})
        assert client.put("/api/templates/chart/pwned", json=BODY,
                          headers={**APACHE(name), "origin": f"http://{page}"}).status_code == 403, name
    # nginx with no Host line, but X-Forwarded-Host $host
    _refused(client, {"host": "127.0.0.1:8800", "x-forwarded-host": "evil.example", "x-forwarded-proto": "http",
                      "x-forwarded-for": "192.168.1.20", "origin": "http://evil.example"})
    assert client.get("/api/auth").json()["enabled"] is False   # nothing changed
    assert "pwned" not in client.get("/api/templates").json()["templates"].get("chart", {})
    _works(client, APACHE("lan.pc"), "http://lan.pc")
    _works(client, APACHE("lan.pc:8080"), "http://lan.pc:8080")
    _works(client, {"host": "lan.pc:8080", "x-forwarded-for": "192.168.1.20", "x-forwarded-proto": "http"},
           "http://lan.pc:8080")   # nginx as docs/PHONE.md has it: Host $http_host
    _works(client, {"host": "127.0.0.1:8800"}, "http://127.0.0.1:8800")   # the PC's own browser
    assert client.get("/api/auth", headers={**SERVE, "referer": f"https://{PC}/"}).status_code == 200   # tailscale serve
    _works(client, SERVE, f"https://{PC}")


def test_a_proxy_on_plain_http_proves_no_ts_net_name(lan_name, client):  # noqa: F811
    """A link or a form on another site's page, https://<its name>.ts.net, to the same name over plain http sends the
    https address (the Origin, or the Referer with Referrer-Policy unsafe-url); through a proxy of yours on plain http
    that passes any Host on (nginx as the default server) it gets an X-Forwarded-For too. That made the name this PC's,
    kept for good, and the rebound page got in by it. The proof needs tailscale serve's own X-Forwarded-Proto https and
    an X-Forwarded-Host that is the Host (1.44 and later): a proxy that names no scheme proves nothing either
    (test_hunt6_secfoot), and neither does a tailscale serve before 1.44, whose name goes in CHART_ALLOWED_HOSTS."""
    nginx = {"host": EVIL_TS, "x-forwarded-for": "192.168.1.20", "x-forwarded-proto": "http"}   # $scheme
    for h in ({**nginx, "referer": f"https://{EVIL_TS}/"},          # a link: location.href, window.open
              {**nginx, "origin": f"https://{EVIL_TS}"},            # a form posted to it
              {**nginx, "x-forwarded-proto": "http, https", "referer": f"https://{EVIL_TS}/"},
              {**nginx, "host": f"{EVIL_TS}:8080", "referer": f"https://{EVIL_TS}/"},
              {**APACHE(EVIL_TS), "referer": f"https://{EVIL_TS}/"},   # Apache: the Host is the app's own
              {**APACHE(EVIL_TS), "host": EVIL_TS, "x-forwarded-host": "lan.pc", "referer": f"https://{EVIL_TS}/"}):
        assert client.get("/api/health", headers=h).status_code == 403, h
        assert client.post("/api/auth/login", json={"password": "x"}, headers=h).status_code == 403, h
    assert _names(client) is None
    _refused(client, {"host": EVIL_TS, "origin": f"http://{EVIL_TS}"})   # the rebound page itself: still not in
    # 1.40 to 1.42 pass the Host and X-Forwarded-For only, as such a proxy may: no proof
    old = {"host": "pc.tail3.ts.net", "x-forwarded-for": "100.64.0.7"}
    r = client.get("/api/auth", headers={**old, "referer": "https://pc.tail3.ts.net/"})
    assert r.status_code == 403 and r.json()["wrong_host"] is True
    assert _names(client) is None
    # tailscale serve still proves this PC's name: 1.44 and later (X-Forwarded-Proto https, X-Forwarded-Host the Host,
    # --https=8443 too)
    for name, h in ((PC, SERVE),
                    ("pc.tail2.ts.net:8443", {**SERVE, "host": "pc.tail2.ts.net:8443", "x-forwarded-host": "pc.tail2.ts.net:8443"})):
        r = client.get("/api/auth", headers={**h, "referer": f"https://{name}/"})
        assert r.status_code == 200 and r.json()["signed_in"] is True, h
        _works(client, h, f"https://{name}")
    assert _names(client) == [PC, "pc.tail2.ts.net"]
