"""Connections cards as the dialog shows them, found adding the Interactive Brokers futures plugin: a feed plugin added
with + stays on its list, on or off, until Remove from list (which deletes what is saved for it); NinjaTrader's "from
other feeds" note follows the plugins turned on as they change; a new IBKR futures card starts from the IBKR quotes
card's host and port. The dialog's part of web/app.js runs in node (cn_dom.js) against the test's server, as the user
clicked it."""
import asyncio
import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEB = Path(__file__).resolve().parents[2] / "web"
DOM = Path(__file__).with_name("cn_dom.js")
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The app on NinjaTrader (CHART_FEED=ninjatrader: plugins may supply futures), RTY on Instruments as opening it
    adds it, and the IBKR futures plugin with a stand-in for TWS: its Test answers, it runs without connecting.
    server() starts it; again, it stops it and starts it again on the same database (a restart, as update.cmd's)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    for k, v in {"CHART_DB": tmp_path / "chart.db", "CHART_LEVELS_DIR": logs, "CHART_FEED": "ninjatrader",
                 "CHART_QUOTES_FEED": "none", "CHART_NT_PORT": _port(), "CHART_FEEDS_DIR": tmp_path / "nofeeds"}.items():
        monkeypatch.setenv(k, str(v))
    for k in ("IBKR_HOST", "IBKR_PORT"):
        monkeypatch.delenv(k, raising=False)
    from chartsrv.app import app

    async def answers(cls, values):
        return True, f"TWS answers on {values['host']}:{values['port']}"

    async def runs(self, market):
        await asyncio.sleep(3600)

    def start():
        while started:
            started.pop().__exit__(None, None, None)
        c = TestClient(app)
        c.__enter__()
        cls = app.state.market.plugins.types["ibkr_trades"]
        monkeypatch.setattr(cls, "test", classmethod(answers))
        monkeypatch.setattr(cls, "run", runs)
        assert c.post("/api/instruments/add", json={"symbol": "RTY"}).status_code == 200
        started.append(c)
        return app, c
    started = []
    yield start
    while started:
        started.pop().__exit__(None, None, None)


def _by(c):
    return {x["id"]: x for x in c.get("/api/connections").json()["connections"]}


def _values(card):
    return {f["key"]: f.get("value") for f in card["fields"]}


def _nt_trade(m, price: float) -> bool:
    """A trade of RTY (on its 0.1 tick) from the NinjaTrader add-on: True when it is taken (no other feed supplies RTY)."""
    nt = next(f for f in m.feed_objs if f.name == "ninjatrader")
    nt._line(m, f"T|RTY|{int(time.time() * 1000)}|{price}|1|1\n".encode())
    return m.last_tick.get("RTY") is not None and m.last_tick["RTY"].price == price


def _app_js() -> str:
    """app.js's api() and its Connections part."""
    src = (WEB / "app.js").read_text()
    i = src.index("\nasync function api(")
    return src[i + 1:src.index("\n}", i + 1) + 2] + "\n" + src[src.index("// ---- Connections:"):src.index("\nfunction deviceFacts(")]


def browse(c, js) -> list:
    """Runs `js` (an async body; D is the dialog, cn_dom.js) in node, its requests answered by client `c`; returns what
    it passes to out()."""
    code = f"""
const {{ dialog, say }} = require({json.dumps(str(DOM))});
const D = dialog({json.dumps(_app_js())});
const out = (v) => say('OUT', v);
(async () => {{ {js}; await D.settle(); }})().then(() => process.exit(0), (e) => {{ console.error(e); process.exit(1); }});"""
    p = subprocess.Popen(["node", "-e", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    outs, fails = [], []
    for line in p.stdout:
        kind, _, v = line.partition(" ")
        v = json.loads(v)
        if kind == "FETCH":
            r = c.request(v["method"], v["path"], json=v["body"])
            p.stdin.write(json.dumps({"status": r.status_code, "json": r.json() if r.content else None}) + "\n")
            p.stdin.flush()
        elif kind == "OUT":
            outs.append(v)
        else:
            fails.append(v)
    assert p.wait() == 0 and not fails, fails
    return outs


def test_an_added_plugin_stays_listed_on_or_off_until_removed(server):
    """Its draft's Add with On unticked saves it off: it was then off the list (listed only while on), gone from the
    dialog at once and after a restart, with no Remove; the + offered it as new again. An off one takes no future
    from NinjaTrader. Remove deletes what is saved for it (it left {"enabled": false} behind)."""
    app, c = server()
    m = app.state.market
    by = _by(c)
    assert not by["ibkr_trades"]["listed"] and by["ibkr_trades"]["state"] == "off"
    r = c.put("/api/connections/ibkr_trades", json={"port": 7496, "symbols": "RTY", "enabled": False})   # what Add sent
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["status"], r.json()["listed"]) == ("off", "Off", True)
    assert m.trade_owner == {} and "from other feeds" not in _by(c)["ninjatrader"]["info"] and _nt_trade(m, 2450.5)

    assert c.put("/api/connections/ibkr_trades", json={"enabled": True}).json()["listed"]
    assert m.trade_owner == {"RTY": "ibkr_trades"} and _by(c)["ninjatrader"]["info"].endswith(" · RTY from other feeds")
    assert not _nt_trade(m, 2451.5)   # the IBKR feed's now
    r = c.put("/api/connections/ibkr_trades", json={"enabled": False}).json()
    assert (r["state"], r["listed"]) == ("off", True)
    assert m.trade_owner == {} and "from other feeds" not in _by(c)["ninjatrader"]["info"]   # RTY: NinjaTrader's again
    assert _nt_trade(m, 2452.5)
    assert not any(f.name == "ibkr_trades" for f in m.feed_objs)

    app, c = server()   # a restart (update.cmd)
    card = _by(c)["ibkr_trades"]
    assert (card["state"], card["listed"]) == ("off", True)
    assert _values(card) == {"enabled": False, "host": "host.docker.internal", "port": 7496, "client_id": 32, "symbols": "RTY"}
    assert c.put("/api/connections/ibkr_trades", json={"enabled": True}).json()["state"] == "warn"   # Connecting…
    assert c.delete("/api/connections/ibkr_trades").status_code == 204
    card = _by(c)["ibkr_trades"]
    assert not card["listed"] and "ibkr_trades" not in app.state.market.connections.saved
    assert _values(card) == {"enabled": False, "host": "host.docker.internal", "port": 4001, "client_id": 32, "symbols": ""}
    assert app.state.market.trade_owner == {} and not any(f.name == "ibkr_trades" for f in app.state.market.feed_objs)
    assert c.delete("/api/connections/ninjatrader").status_code == 400   # a main one stays


@needs_node
def test_the_dialog_keeps_an_added_feed_until_it_is_removed(server):
    """The user's steps: + > Interactive Brokers futures, its fields, On ticked, Test, Add; then On unticked. The card
    stayed only because the save redrew it in place (the server no longer listed it), without Remove from list, and
    a restart took it away. Now it is listed off too, with Remove, which takes it off the list."""
    app, c = server()
    first = browse(c, """
await D.open();
await D.plus('Futures feed', 'Interactive Brokers futures');
const d = D.card('ibkr_trades');
D.field(d, 'Port').value = '7496'; D.field(d, 'Client ID').value = '32'; D.field(d, 'Futures').value = 'RTY';
await D.tick(d, true);
await D.press(d, 'Test');
out(D.cards('ibkr_trades'));
await D.press(d, 'Add');
out(D.cards('ibkr_trades'));
await D.tick(D.card('ibkr_trades'), false);
out(D.cards('ibkr_trades'));
await D.poll();
await D.open();   // the dialog opened again
out(D.cards('ibkr_trades'));""")
    tested, added, off, again = first
    assert tested[0]["draft"] and tested[0]["msg"] == "TWS answers on host.docker.internal:7496"
    assert (added[0]["draft"], added[0]["status"], added[0]["on"], added[0]["msg"]) == (False, "Connecting…", True, "Added")
    assert "Remove from list" in added[0]["buttons"]
    assert (off[0]["status"], off[0]["on"], off[0]["buttons"]) == ("Off", False, ["Remove from list", "Test", "Save"])
    assert [(x["status"], x["on"], x["buttons"]) for x in again] == [("Off", False, ["Remove from list", "Test", "Save"])]
    assert app.state.market.connections.saved["ibkr_trades"] == {"enabled": False, "host": "host.docker.internal", "port": 7496,
                                                                 "client_id": 32, "symbols": "RTY"}   # as shown

    app, c = server()   # after update.cmd: still there, off, and Remove takes it away
    shown, gone, offers = browse(c, """
await D.open();
out(D.cards('ibkr_trades'));
await D.press(D.card('ibkr_trades'), 'Remove from list');
out(D.cards('ibkr_trades'));
out(D.offers('Futures feed'));""")
    assert [(x["status"], x["on"], x["buttons"]) for x in shown] == [("Off", False, ["Remove from list", "Test", "Save"])]
    assert gone == [] and "Interactive Brokers futures" in offers and "ibkr_trades" not in app.state.market.connections.saved


@needs_node
def test_ninjatraders_note_follows_the_futures_feeds_turned_on(server):
    """NinjaTrader's card said "RTY from other feeds" while the IBKR futures card was Off: the note was drawn with the
    list when the feed was on, and a poll (the server's "connections" message after each save) redrew only each card's
    state, status and On. The server had RTY back on NinjaTrader all along (an off plugin supplies nothing)."""
    app, c = server()
    seen = browse(c, """
const note = () => D.card('ninjatrader').querySelectorAll('p').map((x) => x.textContent).find((t) => t.startsWith('Listens on'));
await D.open();
await D.plus('Futures feed', 'Interactive Brokers futures');
const d = D.card('ibkr_trades');
D.field(d, 'Port').value = '7496'; D.field(d, 'Futures').value = 'RTY';
await D.tick(d, true);
await D.press(d, 'Add');
out(note());
await D.tick(D.card('ibkr_trades'), false);
await D.poll();
out([note(), D.cards('ibkr_trades')[0].status]);
await D.tick(D.card('ibkr_trades'), true);
await D.poll();
out(note());""")
    on, (off, status), again = seen
    assert on.endswith(" · RTY from other feeds") and again == on
    assert status == "Off" and off.startswith("Listens on port") and "from other feeds" not in off
    assert app.state.market.trade_owner == {"RTY": "ibkr_trades"}


@needs_node
def test_a_new_ibkr_futures_card_starts_from_the_quotes_cards_host_and_port(server, monkeypatch):
    """With TWS on 7496 set for the IBKR quotes, a new IBKR futures card offered Gateway's 4001. It starts from the
    quotes card's host and port when those are set (saved there, or in .env); not its client ID, which must differ.
    Its draft's Add saves what it shows: only the fields changed, the port would have stayed the plugin's 4001."""
    monkeypatch.setenv("IBKR_PORT", "7497")   # .env's
    app, c = server()
    assert {k: v for k, v in _values(_by(c)["ibkr_trades"]).items() if k in ("host", "port", "client_id")} == \
        {"host": "host.docker.internal", "port": 7497, "client_id": 32}   # no IBKR_HOST: its own default
    assert c.put("/api/connections/ibkr", json={"ibkr_host": "10.0.0.5", "ibkr_port": 7496}).status_code == 200
    assert _values(_by(c)["ibkr_trades"]) == {"enabled": False, "host": "10.0.0.5", "port": 7496, "client_id": 32, "symbols": ""}
    tested, added = browse(c, """
await D.open();
await D.plus('Futures feed', 'Interactive Brokers futures');
const d = D.card('ibkr_trades');
D.field(d, 'Futures').value = 'RTY';
await D.press(d, 'Test');
out(D.cards('ibkr_trades')[0].msg);
await D.press(d, 'Add');
out(D.cards('ibkr_trades')[0].msg);""")
    assert (tested, added) == ("TWS answers on 10.0.0.5:7496", "Added")
    m = app.state.market
    saved = m.connections.saved["ibkr_trades"]
    assert saved == {"enabled": False, "host": "10.0.0.5", "port": 7496, "client_id": 32, "symbols": "RTY"}
    assert m.plugins.make("ibkr_trades", "ibkr_trades", saved).values == {"host": "10.0.0.5", "port": 7496, "client_id": 32}
    assert c.put("/api/connections/ibkr", json={"ibkr_port": 4002}).status_code == 200   # the quotes card changed later
    assert _values(_by(c)["ibkr_trades"])["port"] == 7496   # this one keeps its own
