"""A proxy whose Host has no port and that names the port in X-Forwarded-Port (nginx's `Host $host` with
`X-Forwarded-Port $server_port`, as docs/PHONE.md offers) on a port that is not the scheme's own: the app's page is
the page the browser is on, so the session renews and an https page's cookie is Secure (the origin check reads the
port the same way)."""
from test_app import client  # noqa: F401  the app on the sim feed
from test_hunt6_secfoot import LOCAL, PW, _secure
from test_hunt7_cookiereplay import hosts  # noqa: F401  CHART_ALLOWED_HOSTS, and every request renews


def test_a_proxy_naming_the_port_in_x_forwarded_port_renews_the_session(hosts, client):  # noqa: F811
    assert client.put("/api/auth/password", json={"new": PW, "label": "PC"}, headers=LOCAL).status_code == 200
    for page, scheme in (("https://pc.example.com:8443", "https"), ("http://pc.example.com:8080", "http")):
        port = page.rsplit(":", 1)[1]
        h = {"host": "pc.example.com", "x-forwarded-port": port, "x-forwarded-for": "100.64.0.7"}
        client.cookies.clear()
        r = client.post("/api/auth/login", json={"password": PW, "label": page}, headers={**h, "origin": page})
        assert r.status_code == 200 and _secure(r) is (scheme == "https"), (page, r.text)
        cookie = {"cookie": r.headers["set-cookie"].split(";")[0]}
        r = client.get("/api/auth", headers={**h, **cookie, "referer": f"{page}/"})   # the page's own load
        assert r.json()["signed_in"] is True and "set-cookie" in r.headers, page   # renewed
        assert _secure(r) is (scheme == "https"), page
