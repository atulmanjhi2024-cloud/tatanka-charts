"""Check all: every connection in use tried at once; the report; the PC's own command."""
import httpx

from chartsrv import check
from chartsrv import connections as conns
from test_app import client  # noqa: F401  the app on the sim feed


def test_check_all_and_report(client, monkeypatch):  # noqa: F811
    tried = []

    async def fake_test(market, cid, body):
        tried.append(cid)
        return {"ok": cid != "yahoo", "message": "Yahoo did not answer" if cid == "yahoo" else "fine"}
    monkeypatch.setattr(conns, "test", fake_test)
    d = client.post("/api/connections/check").json()
    by = {r["id"]: r for r in d["results"]}
    assert sorted(tried) == ["fred", "yahoo"]   # the sim server uses only the free data; the rest is off
    assert by["yahoo"]["ok"] is False and by["ibkr"]["ok"] is None and by["login"]["ok"] is None
    assert by["backup"]["ok"] is True   # the first backup is written at start-up
    text = check.report(d)
    assert "FAIL  Yahoo Finance" in text and "1 failing" in text and "off   Interactive Brokers" in text

    # with the login on: the browser needs to sign in; the PC's own command has the token
    client.put("/api/auth/password", json={"new": "correct horse"})
    client.cookies.clear()
    assert client.post("/api/connections/check").status_code == 401
    assert client.post("/api/connections/check", headers={"X-Local-Token": "guess"}).status_code == 401
    token = check.token_path(client.app.state.market.s).read_text()
    assert client.post("/api/connections/check", headers={"X-Local-Token": token}).status_code == 200

    def post(url, headers, timeout):
        r = client.post(url.replace("http://127.0.0.1:8800", ""), headers=headers)
        return httpx.Response(r.status_code, json=r.json(), request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    assert check.main([]) == 1   # Yahoo fails here
