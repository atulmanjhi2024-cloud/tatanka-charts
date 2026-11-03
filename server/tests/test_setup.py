"""First run: the setup guide on a fresh install only; other devices wait for a password; leaving the simulator."""
import shutil
import signal
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from chartsrv import setup
from chartsrv.models import Bar, Tick
from chartsrv.store import Store

LOCAL = {"host": "localhost:8800"}
PHONE = {"host": "pc.tail1234.ts.net", "x-forwarded-host": "pc.tail1234.ts.net", "x-forwarded-proto": "https",
         "x-forwarded-for": "100.64.0.7",
         "referer": "https://pc.tail1234.ts.net/"}   # the app's page, through tailscale serve: this PC's name


def _client(tmp_path, monkeypatch, feed="none", lock=True):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", feed)
    monkeypatch.setenv("CHART_QUOTES_FEED", "sim" if feed == "sim" else "none")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "5")
    monkeypatch.setenv("CHART_REMOTE_NEEDS_PASSWORD", "1" if lock else "0")
    from chartsrv.app import app
    return app, TestClient(app)


def test_fresh_install_guide_and_lock(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    with client as c:
        st = c.get("/api/setup", headers=LOCAL).json()
        assert st["done"] is False and st["step"] == 0 and st["local"] and not st["login"]
        # another device: refused until a password is set, and told why
        assert c.get("/api/health", headers=PHONE).status_code == 403
        assert c.get("/api/auth", headers=PHONE).json()["need_password"] is True
        assert c.get("/api/branding", headers=PHONE).status_code == 200   # the sign-in still shows the name
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect("/ws", headers=PHONE) as ws:
                ws.receive_json()
        assert e.value.code == 4401
        assert c.get("/api/setup", headers=PHONE).status_code == 403
        assert c.get("/api/health", headers=LOCAL).status_code == 200   # the PC itself
        assert c.put("/api/setup", headers=LOCAL, json={"step": 2}).json() == {"ok": True}
        assert c.get("/api/setup", headers=LOCAL).json()["step"] == 2
        r = c.put("/api/auth/password", headers=LOCAL, json={"new": "correct horse", "label": "PC"})
        assert r.status_code == 200
        c.cookies.clear()
        assert c.get("/api/health", headers=PHONE).status_code == 401   # now: sign in, like any device
        assert "need_password" not in c.get("/api/auth", headers=PHONE).json()
        r = c.post("/api/auth/login", headers=PHONE, json={"password": "correct horse"})
        phone = {**PHONE, "cookie": r.headers["set-cookie"].split(";")[0]}   # a Secure cookie: https in real life
        assert c.get("/api/health", headers=phone).status_code == 200
        assert c.put("/api/setup", headers=phone, json={"step": 3, "done": True}).status_code == 200
        assert c.get("/api/setup", headers=phone).json()["done"] is True


def test_an_existing_install_never_sees_it(tmp_path, monkeypatch):
    st = Store(tmp_path / "chart.db")
    st.set_setting("connections", {"discord": {"discord_webhook": "https://discord.com/api/webhooks/1/x"}})
    st.close()
    _, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert c.get("/api/setup", headers=PHONE).json()["done"] is True   # not locked either
        assert c.get("/api/health", headers=PHONE).status_code == 200


def test_leave_the_simulator(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch, feed="sim", lock=False)
    with client as c:
        st = c.get("/api/setup").json()
        assert st["feed"] == "sim" and st["quotes"] == ["sim"]   # the guide's last step: the watchlist's prices are invented
        c.portal.call(app.state.market.flush)   # on the app's own thread, where its database connection lives
        kills = []
        monkeypatch.setattr(signal, "raise_signal", kills.append)   # uvicorn's own handler stops it cleanly
        assert c.post("/api/setup/leave-sim").json() == {"ok": True}
        time.sleep(0.8)
        assert kills == [signal.SIGTERM]
    db = sqlite3.connect(tmp_path / "chart.db")
    assert db.execute("SELECT COUNT(*) FROM bars_1m").fetchone()[0] > 0   # the simulator wrote its warm-up
    db.close()

    app, client = _client(tmp_path, monkeypatch, feed="sim", lock=False)   # the restart; .env still says sim
    with client as c:
        st = c.get("/api/setup").json()
        assert st["feed"] == "ninjatrader" and st["left_sim"] and st["quotes"] == ["yahoo"]
        m = app.state.market
        assert "sim" not in m.feed_names and "ninjatrader" in m.feed_names and "yahoo" in m.feed_names
        nt = next(x for x in c.get("/api/connections").json()["connections"] if x["id"] == "ninjatrader")
        assert nt["locked"] == ""
        assert c.post("/api/setup/leave-sim").status_code == 400
    db = sqlite3.connect(tmp_path / "chart.db")
    assert db.execute("SELECT COUNT(*) FROM bars_1m").fetchone()[0] == 0 and db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 0
    db.close()
    backups = list(tmp_path.glob("chart.db.bak-*"))
    assert len(backups) == 1 and sqlite3.connect(backups[0]).execute("SELECT COUNT(*) FROM bars_1m").fetchone()[0] > 0


def test_init_and_again(tmp_path, monkeypatch, capsys):
    st = Store(tmp_path / "a.db")
    assert setup.init(st) == {"done": False, "step": 0, "fresh": True}
    st.write([Tick("ES", 1, 1.0, 1, 1)], [("ES", Bar(t=0, o=1, h=1, l=1, c=1, v=1))])
    assert setup.init(st)["fresh"] is True   # decided once, at the first start
    st.set_setting(setup.SETTING, {"done": True, "step": 3, "fresh": True})
    st.close()
    monkeypatch.setenv("CHART_DB", str(tmp_path / "a.db"))
    monkeypatch.setattr("sys.argv", ["setup", "again"])
    setup.main()
    assert "next page load" in capsys.readouterr().out
    st = Store(tmp_path / "a.db")
    assert st.get_setting(setup.SETTING) == {"done": False, "step": 0, "fresh": True}
    st.close()
    assert setup.is_local({"host": "127.0.0.1:8800"}, None) and setup.is_local({"host": "[::1]:8800"}, None)
    assert not setup.is_local({"host": "localhost", "x-forwarded-for": "1.2.3.4"}, None)
    assert not setup.is_local({"host": "192.168.1.5:8800"}, None)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_last_step_says_where_the_watchlist_prices_come_from():
    """The guide's last step, from what /api/setup says (app.js setupShow): a fresh install that keeps the simulator
    is told its watchlist prices are invented, not that Yahoo is on; Yahoo on says so; neither, that Yahoo is free."""
    from test_hunt_web_ui import app_js, run_app_js
    out = run_app_js(app_js("const SETUP", "function setupShow("), """
const el = (tag, props = {}, ...kids) => ({ tag, ...props, kids: kids.filter(Boolean), addEventListener() {},
  append(...k) { this.kids.push(...k.filter(Boolean)); }, replaceChildren(...k) { this.kids = k.filter(Boolean); },
  querySelector() { return null; } });
const texts = (e) => [...(e.tag === 'p' ? [e.textContent] : []), ...(e.kids || []).flatMap(texts)];
let dlg = null;
Object.assign(ctx, { $: () => dlg, cnEl: el, document: { body: { append(d) { dlg = d; } } }, api: async () => ({}),
  toast() {}, brand: { name: 'Tatanka' }, deviceLabel: () => '', leaveSim() {}, cnOpen() {} });
vm.runInContext(/*APP*/ + ';this.setupShow = setupShow;', ctx);
const said = [];
for (const st of [{ feed: 'sim', quotes: ['sim'] }, { feed: 'ninjatrader', quotes: ['yahoo'] }, { feed: 'ninjatrader', quotes: [] }]) {
  dlg = null;
  ctx.setupShow({ step: 3, ...st });
  said.push(texts(dlg).find((t) => t.startsWith('Watchlist prices')));
}
out(said);""")
    assert out[0].startswith("Watchlist prices: invented by the simulator for now") and "Yahoo is on" not in out[0]
    assert out[1].startswith("Watchlist prices: Yahoo is on (free; futures lag about 10 minutes).")
    assert out[2].startswith("Watchlist prices: Yahoo is free (futures lag about 10 minutes);")
