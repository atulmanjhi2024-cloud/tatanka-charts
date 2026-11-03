"""Connections > Instruments: the chartable futures edited in the app, saved in the database, used at the next start."""
import signal
import time

import pytest
from fastapi.testclient import TestClient

from chartsrv import config

RTY = {"symbol": "rty", "name": "E-mini Russell 2000", "tick_size": 0.1, "point_value": 50, "rth": ["08:30", "15:15"],
       "calendar": "equity", "sim_price": 2300}


def _client(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", "none")
    monkeypatch.setenv("CHART_SYMBOLS_ONLINE", "0")
    from chartsrv.app import app
    return app, TestClient(app)


def test_edit_then_restart(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        st = c.get("/api/instruments/config").json()
        file_syms = [r["symbol"] for r in st["instruments"]]
        assert st["source"] == "file" and not st["pending"] and st["calendars"] == ["equity", "energy", "metals"]
        assert "ES" in file_syms and "RTY" not in c.get("/api/instruments").json()
        card = next(x for x in c.get("/api/connections").json()["connections"] if x["id"] == "instruments")
        assert card["state"] == "ok" and card["status"] == f"{len(file_syms)} futures · config/instruments.json"

        rows = st["instruments"]
        for bad, why in [([], "1 to 60"), ([{**RTY, "symbol": "R TY"}], "symbol"), ([{**RTY, "tick_size": 0}], "tick size"),
                         ([{**RTY, "tick_size": True}], "tick size"), ([{**RTY, "point_value": "50"}], "point value"),
                         ([{**RTY, "rth": ["15:15", "08:30"]}], "RTH"), ([{**RTY, "rth": ["08:30", "17:00"]}], "16:00"),
                         ([{**RTY, "calendar": "grains"}], "holidays"), ([{**RTY, "name": "<script>"}], "name"),
                         ([RTY, RTY], "twice"), ([{**RTY, "sim_follow": "RTY"}], "simulator")]:
            r = c.put("/api/instruments/config", json={"instruments": bad})
            assert r.status_code in (400, 422), bad
            assert r.status_code == 422 or why in r.json()["detail"], (why, r.json())
        assert c.get("/api/instruments/config").json()["source"] == "file"   # nothing half-saved

        st = c.put("/api/instruments/config", json={"instruments": [r for r in rows if r["symbol"] != "GC"] + [RTY]}).json()
        assert st["source"] == "app" and st["pending"]
        assert [r["symbol"] for r in st["instruments"]][-1] == "RTY" and "GC" not in [r["symbol"] for r in st["instruments"]]
        card = next(x for x in c.get("/api/connections").json()["connections"] if x["id"] == "instruments")
        assert card["state"] == "warn" and card["status"].endswith("edited here) · restart to apply")
        assert "RTY" not in c.get("/api/instruments").json()   # the running server keeps its list until the restart

        kills = []
        monkeypatch.setattr(signal, "raise_signal", kills.append)   # uvicorn's own handler stops it cleanly
        assert c.post("/api/server/restart").json() == {"ok": True}
        time.sleep(0.8)
        assert kills == [signal.SIGTERM]

    _, client = _client(tmp_path, monkeypatch)   # the restart
    with client as c:
        inst = c.get("/api/instruments").json()
        assert inst["RTY"]["tick_size"] == 0.1 and inst["RTY"]["rth"] == ["08:30", "15:15"] and "GC" not in inst
        assert not c.get("/api/instruments/config").json()["pending"]

        st = c.put("/api/instruments/config", json={"instruments": file_rows(tmp_path)}).json()   # the file's own list again
        assert st["source"] == "file" and st["pending"]
        c.put("/api/instruments/config", json={"instruments": [RTY]})
        st = c.delete("/api/instruments/config").json()
        assert st["source"] == "file"


def file_rows(_):
    from chartsrv.config import instruments_file, load_settings
    return [{"symbol": k, **v} for k, v in instruments_file(load_settings().config_dir).items()]


def test_a_broken_saved_list_falls_back_to_the_file(tmp_path, caplog):
    class Store:
        def get_setting(self, k, d=None):
            return {"ES": {"name": "x", "tick_size": -1, "point_value": 50}}
    from chartsrv.config import load_instruments, load_settings
    inst = load_instruments(load_settings().config_dir, Store())
    assert inst["ES"].tick_size == 0.25 and "don't load" in caplog.text


def test_not_a_number():
    with pytest.raises(ValueError, match="tick size"):
        config.clean_instruments([{**RTY, "tick_size": float("nan")}])


@pytest.mark.parametrize("rth,ok", [(["08:30", "15:15"], True), (["00:00", "16:00"], True), (None, True), ([], True),
                                    (["8:30", "15:15"], False), (["08:30"], False), (["15:00", "15:00"], False)])
def test_rth(rth, ok):
    row = {**RTY, "rth": rth}
    if ok:
        config.clean_instruments([row])
    else:
        with pytest.raises(ValueError):
            config.clean_instruments([row])


def test_the_futures_catalog(tmp_path, monkeypatch):
    """+ Instrument offers CME futures with their specs: each passes the same checks as a typed one."""
    _, client = _client(tmp_path, monkeypatch)
    with client as c:
        st = c.get("/api/instruments/config").json()
        cat = {r["symbol"]: r for r in st["catalog"]}
        assert len(cat) >= 40 and {"ES", "RTY", "6E", "ZN", "SI", "BTC", "MCL"} <= set(cat)
        assert cat["SI"]["rth"] == ["07:25", "12:25"] and cat["CL"]["calendar"] == "energy"
        assert "rth" not in cat["BTC"] and cat["ZN"]["tick_size"] == 0.015625
        rows = st["instruments"] + [{k: v for k, v in cat[s].items() if k != "group"} for s in ("6E", "ZN", "RTY")]
        st = c.put("/api/instruments/config", json={"instruments": rows}).json()
        assert [r["symbol"] for r in st["instruments"]][-3:] == ["6E", "ZN", "RTY"] and not st["pending"]   # added: live at once
        assert {"6E", "ZN", "RTY"} <= set(c.get("/api/instruments").json())


def test_a_future_opened_is_added_live(tmp_path, monkeypatch):
    import socket
    from test_feed_plugins import _port, _wait
    monkeypatch.setenv("CHART_NT_PORT", str(port := _port()))
    app, client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("CHART_FEED", "ninjatrader")   # read when the app starts
    with client as c:
        m = app.state.market
        with c.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["catalog"]["RTY"] == "E-mini Russell 2000" and "ES" not in hello["catalog"]
            nt = socket.create_connection(("127.0.0.1", port))
            nt.sendall(b"HELLO||2\n")
            nt.settimeout(5)
            got = b""
            while b"W|" not in got:
                got += nt.recv(65536)
            assert b"I|ES,NQ,CL,GC,MES,MNQ\n" in got   # the chart futures; RTY only quoted (W) for the watchlist

            assert c.post("/api/instruments/add", json={"symbol": "zz"}).status_code == 400
            r = c.post("/api/instruments/add", json={"symbol": "rty"})
            assert r.status_code == 200 and r.json()["tick_size"] == 0.1
            assert m.instruments["RTY"].point_value == 50 and "RTY" in m.connections.instruments
            assert m.instruments["RTY"].quiet and not m.instruments["ES"].quiet   # no "Feed stale" pages for it
            while True:   # the page hears, without a reload
                msg = ws.receive_json()
                if msg["type"] == "instruments":
                    break
            assert "RTY" in msg["instruments"] and "RTY" not in msg["catalog"] and msg["config"] == m.config_hash()

            got = b""   # the add-on: forward RTY, and its last 14 days
            while b"GAP|RTY|" not in got:
                got += nt.recv(65536)
            assert b"RTY" in got.split(b"I|", 1)[1].split(b"\n", 1)[0]
            now = int(time.time() * 1000)
            nt.sendall(f"T|RTY|{now}|2450.3|2|1\n".encode())
            assert _wait(lambda: m.last_tick.get("RTY") and m.last_tick["RTY"].price == 2450.3)
            nt.close()
        assert [x["symbol"] for x in c.get("/api/instruments/config").json()["instruments"]][-1] == "RTY"   # kept
        assert not c.get("/api/instruments/config").json()["pending"]

        assert hello.get("update") == {"behind": None, "latest": ""}   # the update note's state, for pages opened later
        # Connections: one more added there applies at once too; a removal still asks for the restart
        st0 = c.get("/api/instruments/config").json()
        rows = st0["instruments"]
        assert c.put("/api/instruments/config", json={"instruments": rows[:-1], "version": "stale"}).status_code == 409
        ym = next(x for x in c.get("/api/instruments/config").json()["catalog"] if x["symbol"] == "YM")
        st = c.put("/api/instruments/config", json={"instruments": rows + [{k: v for k, v in ym.items() if k != "group"}],
                                                    "version": st0["version"]}).json()
        assert not st["pending"] and "YM" in m.instruments
        st = c.put("/api/instruments/config", json={"instruments": [x for x in rows if x["symbol"] != "GC"]}).json()
        assert st["pending"] and "GC" in m.instruments

    _, client = _client(tmp_path, monkeypatch)   # the next start keeps what was added
    monkeypatch.setenv("CHART_FEED", "ninjatrader")
    with client as c:
        assert "RTY" in c.get("/api/instruments").json()


def test_no_catalog_on_the_simulator(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_QUOTES_FEED", "none")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "5")
    app, client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("CHART_FEED", "sim")
    with client as c:
        assert app.state.market.catalog() == {}
        assert c.post("/api/instruments/add", json={"symbol": "RTY"}).status_code == 400


def test_how_each_future_rolls(tmp_path, monkeypatch):
    """Connections > Instruments > Contract: by volume (the default), NinjaTrader's dates, or a fixed month; sent to the
    add-on on connect (R lines, with the contract charted last) and again at once when changed."""
    import socket
    from test_feed_plugins import _port
    with pytest.raises(ValueError, match="contract"):
        config.clean_instruments([{**RTY, "contract": "13-27"}])
    with pytest.raises(ValueError, match="contract"):
        config.clean_instruments([{**RTY, "contract": "march"}])
    assert config.clean_instruments([{**RTY, "contract": "volume"}])["RTY"].get("contract") is None   # the default, not stored
    assert config.clean_instruments([{**RTY, "contract": "03-27"}])["RTY"]["contract"] == "03-27"
    monkeypatch.setenv("CHART_NT_PORT", str(port := _port()))
    app, client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("CHART_FEED", "ninjatrader")
    with client as c:
        m = app.state.market
        m.contracts["ES"] = "ES 03-27"   # charted last
        for _ in range(50):   # the feed starts listening a moment after the app
            try:
                nt = socket.create_connection(("127.0.0.1", port))
                break
            except ConnectionRefusedError:
                time.sleep(0.1)
        nt.sendall(b"HELLO||3\n")
        nt.settimeout(5)
        got = b""
        while b"W|" not in got:
            got += nt.recv(65536)
        assert b"R|ES|volume|ES 03-27\n" in got and b"R|NQ|volume|\n" in got
        st = c.get("/api/instruments/config").json()
        assert st["contracts"]["ES"] == "ES 03-27"
        rows = [{**r, "contract": "dates"} if r["symbol"] == "ES" else {**r, "contract": "12-26"} if r["symbol"] == "NQ" else r
                for r in st["instruments"]]
        st = c.put("/api/instruments/config", json={"instruments": rows, "version": st["version"]}).json()
        assert not st["pending"] and m.instruments["ES"].contract == "dates"   # applied live: no restart
        got = b""
        while b"R|NQ|12-26|" not in got:
            got += nt.recv(65536)
        assert b"R|ES|dates|ES 03-27\n" in got
        nt.close()


def test_the_equity_index_rth_ends_at_15_00_once(tmp_path):
    from chartsrv.config import INSTRUMENTS_SETTING, migrate_instruments
    from chartsrv.store import Store
    store = Store(tmp_path / "c.db")
    store.set_setting(INSTRUMENTS_SETTING, {"ES": {**RTY, "rth": ["08:30", "15:15"]}, "CL": {**RTY, "rth": ["08:00", "13:30"]}})
    migrate_instruments(store)
    got = store.get_setting(INSTRUMENTS_SETTING)
    assert got["ES"]["rth"] == ["08:30", "15:00"] and got["CL"]["rth"] == ["08:00", "13:30"]
    got["ES"]["rth"] = ["08:30", "15:15"]   # set again after: it stays
    store.set_setting(INSTRUMENTS_SETTING, got)
    migrate_instruments(store)
    assert store.get_setting(INSTRUMENTS_SETTING)["ES"]["rth"] == ["08:30", "15:15"]


def test_treasuries_in_32nds():
    from chartsrv.config import Instrument
    zn, zb, zf, zt = (Instrument(s, s, t, 1000, "") for s, t in (("ZN", 1 / 64), ("ZB", 1 / 32), ("ZF", 1 / 128), ("ZT", 1 / 256)))
    assert (zn.fmt(110.515625), zn.fmt(110.5), zb.fmt(115.5), zf.fmt(108.5078125), zt.fmt(104.1796875)) == \
        ("110'165", "110'160", "115'16", "108'162", "104'057")
    assert Instrument("ES", "ES", 0.25, 50, "ES").fmt(6900.5) == "6900.50"
