"""Third bug-hunt fixes in the login and the page: a sign-in or password change whose slow check crosses another
password change, the same-origin check and its ports, framing, the Login card with Login off on a fresh install,
and the files read as UTF-8 whatever the PC's own encoding."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

from chartsrv import auth
from test_app import client  # noqa: F401  the app on the sim feed
from test_hunt_web_ui import app_js, run_app_js
from test_setup import LOCAL, PHONE, _client


def _cli_sets(monkeypatch, password):
    """`python -m chartsrv.auth set` on the PC, run from inside a slow check (its own process: its own database link)."""
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt="": password)
    return lambda: auth.main(["set"])


def test_a_sign_in_in_flight_across_a_password_change(client, monkeypatch):  # noqa: F811
    """Change password signs every other device out: a sign-in with the old password still being checked when the
    password changes is refused, not signed in under the new one."""
    client.put("/api/auth/password", json={"new": "old password", "label": "PC"})
    owner = dict(client.cookies)
    real, checking, changed, calls = auth.verify, threading.Event(), threading.Event(), []

    def verify(password, cfg):   # the first check (the intruder's sign-in) waits for the change to land
        calls.append(password)
        ok = real(password, cfg)
        if len(calls) == 1:
            checking.set()
            changed.wait(10)
        return ok
    monkeypatch.setattr(auth, "verify", verify)

    async def race():
        app = client.app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("100.64.0.9", 1)), base_url="http://testserver") as intruder, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", cookies=owner) as pc:
            login = asyncio.create_task(intruder.post("/api/auth/login", json={"password": "old password", "label": "intruder"}))
            await asyncio.to_thread(checking.wait, 10)
            change = await pc.put("/api/auth/password", json={"current": "old password", "new": "new password", "label": "PC"})
            changed.set()
            r = await login
            later = await intruder.get("/api/health")   # with whatever cookie the sign-in gave
            return change.status_code, r.status_code, later.status_code
    assert client.portal.call(race) == (200, 401, 401)
    a = client.app.state.market.auth
    on_loop = client.portal.call   # the app's database link is its own thread's
    assert [s["label"] for s in on_loop(a.sessions)] == ["PC"]

    # the same with the password set on the PC from the command line meanwhile
    cli = _cli_sets(monkeypatch, "cli password")
    monkeypatch.setattr(auth, "verify", lambda password, cfg: (real(password, cfg), cli())[0])
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"password": "new password", "label": "intruder"})
    assert r.status_code == 401 and on_loop(a.sessions) == []
    monkeypatch.setattr(auth, "verify", real)
    assert client.post("/api/auth/login", json={"password": "cli password"}).status_code == 200


def test_a_password_change_in_flight_keeps_one_made_meanwhile(client, monkeypatch):  # noqa: F811
    """A change or turn-off checked against the old password doesn't overwrite one made while it was checked (from
    another device, or on the PC's command line), nor sign that device out."""
    client.put("/api/auth/password", json={"new": "old password", "label": "PC"})
    real_hash, real_verify = auth.make_hash, auth.verify
    cli = _cli_sets(monkeypatch, "cli password")
    monkeypatch.setattr(auth, "make_hash", lambda password: (cli() if password == "mine password" else 0, real_hash(password))[1])
    r = client.put("/api/auth/password", json={"current": "old password", "new": "mine password"})
    assert r.status_code == 409
    a, on_loop = client.app.state.market.auth, client.portal.call   # the app's database link is its own thread's
    assert on_loop(a.check, "cli password") and not on_loop(a.check, "mine password")

    cli = _cli_sets(monkeypatch, "cli password2")
    monkeypatch.setattr(auth, "verify", lambda password, cfg: (real_verify(password, cfg), cli())[0])
    client.cookies.clear()
    client.headers["x-local-token"] = client.app.state.local_token   # the PC's own checks, past the login
    r = client.put("/api/auth/password", json={"current": "cli password", "new": None})
    monkeypatch.setattr(auth, "verify", real_verify)
    assert r.status_code == 409 and on_loop(a.check, "cli password2")


TS = {"host": "pc.tail1.ts.net", "x-forwarded-host": "pc.tail1.ts.net", "x-forwarded-proto": "https",
      "x-forwarded-for": "100.64.0.7"}   # what tailscale serve passes on


def test_a_page_on_another_port_is_another_site(client):  # noqa: F811
    """A page another program on the PC serves (localhost:3000, a notebook, a dev server) or another tailscale serve
    port of the same name is another site's, though the browser sends it the same cookie: it can't change anything or
    open the live connection. The app's own addresses, on the PC and through tailscale serve, still can."""
    from starlette.websockets import WebSocketDisconnect
    body = {"data": {"studies": {}}}
    others = [{"host": "localhost:8800", "origin": "http://localhost:3000"},
              {"host": "localhost:8800", "origin": "http://localhost"},
              {"host": "127.0.0.1:8800", "origin": "http://127.0.0.1:8888"},
              {"host": "localhost:8800", "origin": "http://localhost:99999"},   # no such port: refused, not a 500
              {**TS, "origin": "https://pc.tail1.ts.net:8443"},   # tailscale serve --https=8443, another app
              {**TS, "origin": "http://pc.tail1.ts.net"}]         # tailscale serve --http=80, another app
    for h in others:
        assert client.put("/api/templates/chart/x", json=body, headers=h).status_code == 403, h
        assert client.post("/api/auth/sessions/end-others", headers=h).status_code == 403, h
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers=h) as ws:
                ws.receive_json()
    mine = [{"host": "localhost:8800", "origin": "http://localhost:8800"},
            {"host": "127.0.0.1:8800", "origin": "http://127.0.0.1:8800"},
            {"host": "[::1]:8800", "origin": "http://[::1]:8800"},
            {"host": "Localhost:8800", "origin": "http://localhost:8800"},
            {**TS, "origin": "https://pc.tail1.ts.net"},
            {**TS, "host": "pc.tail1.ts.net:8443", "x-forwarded-host": "pc.tail1.ts.net:8443", "origin": "https://pc.tail1.ts.net:8443"},
            {"host": "127.0.0.1:8800", "x-forwarded-host": "pc.tail1.ts.net", "origin": "https://pc.tail1.ts.net"}]
    for h in mine:
        assert client.put("/api/templates/chart/x", json=body, headers=h).status_code == 200, h
        with client.websocket_connect("/ws", headers=h) as ws:
            assert ws.receive_json()["type"] == "hello", h


def test_no_other_site_can_frame_the_app(client):  # noqa: F811
    """Another site's page can't show the app in a frame under its own buttons (clickjacking: on an install with no
    password the framed app works, its requests being the app's own). SAMEORIGIN: the layout's own frames still can."""
    for path in ("/", "/index.html", "/index.html/"):   # the last: the file as it is, from the static files
        r = client.get(path)
        assert r.status_code == 200 and "<html" in r.text, path
        assert r.headers.get("x-frame-options") == "SAMEORIGIN", path
        assert r.headers.get("content-security-policy") == "frame-ancestors 'self'", path


def _login_card(c):
    return next(x for x in c.get("/api/connections", headers=LOCAL).json()["connections"] if x["id"] == "login")


def test_the_login_card_says_who_gets_in_with_login_off(tmp_path, monkeypatch):
    """With Login off, a fresh install lets only the PC in (secure by default), before the first password and after
    Login is turned off again: the card says so, not that anyone who reaches the app can use it. An install from
    before the setup guide, or CHART_REMOTE_NEEDS_PASSWORD=0, is open to anyone, and the card says that."""
    (tmp_path / "fresh").mkdir()
    (tmp_path / "open").mkdir()
    _, tc = _client(tmp_path / "fresh", monkeypatch)
    with tc as c:
        for step in ("first start", "turned off"):
            card = _login_card(c)
            assert card["state"] == "warn" and card["status"] == "Off: only this PC can use the app until a password is set", step
            assert card["login"] == {"enabled": False, "pc_only": True}, step
            assert c.get("/api/health", headers=PHONE).status_code == 403, step   # as it says
            r = c.put("/api/auth/password", headers=LOCAL, json={"new": "correct horse"})
            pc = {**LOCAL, "cookie": r.headers["set-cookie"].split(";")[0]}   # a Secure cookie: the client keeps it for https
            assert c.get("/api/health", headers=PHONE).status_code == 401   # on: sign in, like any device
            assert c.put("/api/auth/password", headers=pc, json={"current": "correct horse", "new": None}).status_code == 200
    _, tc = _client(tmp_path / "open", monkeypatch, lock=False)
    with tc as c:
        card = _login_card(c)
        assert card["status"] == "Off: anyone who reaches the app can use it" and card["login"] == {"enabled": False, "pc_only": False}
        assert c.get("/api/health", headers=PHONE).status_code == 200


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_turn_off_button_says_who_gets_in():
    """The Login card's Turn off button (app.js loginPart) says what turning it off does on this install."""
    out = run_app_js(app_js("function loginPart("), """
Object.assign(ctx, {
  cnEl: (tag, props = {}, ...kids) => ({ tag, ...props, kids, append(...more) { kids.push(...more); } }),
  onMenuPress() {}, deviceLabel: () => 'PC',
  api: () => new Promise(() => {}), ax: { date: { format: () => '' } },
});
vm.runInContext(/*APP*/ + ';this.loginPart = loginPart;', ctx);
const find = (e, text) => e && (e.textContent === text ? e : (e.kids || []).map((k) => find(k, text)).find(Boolean));
out([true, false].map((pc_only) => find(ctx.loginPart({ login: { enabled: true, pc_only } }), 'Turn off').title));""")
    assert out == ["No password: only this PC can use the app, other devices are refused",
                   "No password: anyone who reaches the app can use it"]


ROOT = Path(__file__).resolve().parents[2]
NOT_UTF8 = {"LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}   # like Windows' cp1252: not UTF-8
READS = r"""
import json, locale, sys
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
from chartsrv import amt, config, cot, daily_levels, settlement, watchlists
from chartsrv.app import app
from chartsrv.feeds.yahoo import YahooQuotes
from chartsrv.market import Market
from chartsrv.store import Store
d = Path(sys.argv[1]) / "config"
c = TestClient(app)   # the page and its service worker only: no lifespan
page, sw = c.get("/"), c.get("/sw.js")
seeded = []
lv = daily_levels.Source()   # the daily levels file, read as the levels file or link setting reads it
daily_levels.DailyLevels._read_file(SimpleNamespace(docs_dir=lambda: None), str(d / "ES.txt"), lv, 0, True)
Market._seed_nt_settings(SimpleNamespace(s=SimpleNamespace(config_dir=d), store=SimpleNamespace(
    get_setting=lambda k, default=None: seeded.append(k) or 10 ** 9)))   # read, and applied already: nothing more
print(json.dumps({
    "utf8": locale.getencoding().lower().replace("-", "") == "utf8",
    "page": [page.status_code, chr(0x23EA) in page.text], "sw": [sw.status_code, "caf" + chr(0xE9) in sw.text],
    "instruments": sorted(config.instruments_file(d)), "futures": len(config.futures_catalog(d)),
    "holidays": len(config.load_holidays(d)["days"]), "watchlists": [x["name"] for x in watchlists.load_default(d)["lists"]],
    "settlements": sorted(settlement.load(d)), "cot": cot.Cot(Store(d.parent / "c.db"), None, d).codes.get("XX"),
    "yahoo": len(YahooQuotes._load_extra(d)), "nt_settings": seeded,
    "levels": amt.parse_levels(lv.text)[0][0].Price if lv.text else lv.error}))
"""


def test_files_are_read_as_utf8_whatever_the_pcs_own_encoding(tmp_path):
    """Without Docker on Windows, Python reads a text file in the PC's code page (cp1252) unless told otherwise:
    index.html's UTF-8 (the Replay button's ⏪) made every page load a 500, and a config file you saved with a
    'é' in it failed to load or was skipped. Read under a locale that isn't UTF-8, everything reads as it does on
    a UTF-8 PC."""
    note = "Café ⏪, saved as UTF-8"
    cfg, web = tmp_path / "config", tmp_path / "web"
    cfg.mkdir()
    web.mkdir()
    for f in ("instruments.json", "futures.json", "holidays.json", "watchlists.json", "settlements.json", "nt_settings.json"):
        (cfg / f).write_text(json.dumps({**json.loads((ROOT / "config" / f).read_text(encoding="utf-8")), "_note": note},
                                        ensure_ascii=False), encoding="utf-8")
    (cfg / "yahoo.json").write_text(json.dumps({"_note": note, "symbols": {"XX": "^XX"}}, ensure_ascii=False), encoding="utf-8")
    (cfg / "cot.json").write_text(json.dumps({"_note": note, "codes": {"XX": "123"}}, ensure_ascii=False), encoding="utf-8")
    (cfg / "ES.txt").write_text(f"# {note}\n2026-01-15, 6900.00, 6920.00, 6880.00\n", encoding="utf-8")
    shutil.copy(ROOT / "web" / "index.html", web)
    (web / "sw.js").write_text((ROOT / "web" / "sw.js").read_text(encoding="utf-8") + "\n// café\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("LC_")}
    env.update(PYTHONPATH=str(ROOT / "server"), CHART_WEB_DIR=str(web), CHART_DB=str(tmp_path / "chart.db"))

    def run(extra):
        r = subprocess.run([sys.executable, "-c", READS, str(tmp_path)], env={**env, **extra}, capture_output=True, text=True,
                           encoding="utf-8", cwd=tmp_path, check=False)
        assert r.returncode == 0, r.stderr[-3000:]
        return json.loads(r.stdout.strip().splitlines()[-1])
    utf8, other = run({"PYTHONUTF8": "1"}), run(NOT_UTF8)
    if other.pop("utf8"):
        pytest.skip("no locale here that isn't UTF-8")
    assert utf8.pop("utf8") and utf8["page"] == [200, True] and utf8["sw"] == [200, True] and utf8["cot"] == "123"
    assert utf8["yahoo"] == 1 and utf8["nt_settings"] == ["nt_seed"] and utf8["levels"] == 6900
    assert other == utf8

