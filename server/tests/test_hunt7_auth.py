"""A password change made by the request that also brings the day's renewal: the device that changed it stays signed
in. The login check re-sent the old, just-ended cookie after the new one, and the browser kept the last."""
from chartsrv import auth
from test_app import client  # noqa: F401  the app on the sim feed
from test_hunt6_secfoot import LOCAL, PW

NEW = PW + "-new"


def test_a_password_change_on_the_days_renewal_keeps_this_device_signed_in(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(auth, "RENEW_S", -1)   # every request renews: this one too
    assert client.put("/api/auth/password", json={"new": PW, "label": "PC"}, headers=LOCAL).status_code == 200
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"password": PW, "label": "phone"})
    old = r.headers["set-cookie"].split(";")[0]
    r = client.put("/api/auth/password", json={"current": PW, "new": NEW, "label": "phone"},
                   headers={"cookie": old, "origin": "http://testserver"})   # from the app's page: it renews
    assert r.status_code == 200
    sent = [c.split(";")[0] for c in r.headers.get_list("set-cookie") if c.startswith(f"{auth.COOKIE}=")]
    assert len(sent) == 1 and sent[0] != old   # only the new session's cookie
    client.cookies.clear()
    assert client.get("/api/auth", headers={"cookie": sent[-1]}).json()["signed_in"] is True
    off = client.put("/api/auth/password", json={"current": NEW, "new": None}, headers={"cookie": sent[-1]})
    assert off.status_code == 200   # turned off: the cookie is deleted, not sent again
    assert all("max-age=0" in c.lower() or "expires=thu, 01 jan 1970" in c.lower()
               for c in off.headers.get_list("set-cookie") if c.startswith(f"{auth.COOKIE}="))
