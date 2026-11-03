"""Fixes from round 15 of the bug hunt, a server left running for weeks: a full disk (Docker's) no longer stops the
server from starting again, nor its stop from closing the store, nor Back up now; a NinjaTrader data outage with the
add-on still connected has its hole asked for when the market ticks again; and a closed free-data chart's symbol is
quoted CHART_QUOTE_S after its last poll, not until another chart polls."""
import asyncio
import sqlite3
import time
from datetime import datetime

import pytest

from chartsrv import gapfill as gapfill_mod
from chartsrv import store as store_mod
from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT

from test_review_fixes import build_market


class FullDisk(sqlite3.Connection):
    """The server's connection to a database on a disk that fills when `full` is set: a transaction that changed
    anything fails at its commit, where SQLite writes the changed pages to the -wal, and is rolled back. Reads, and
    writes that change nothing, go on."""
    full = False

    def __enter__(self):
        self._at = self.total_changes
        return super().__enter__()

    def __exit__(self, kind, exc, tb):
        if kind is None and FullDisk.full and self.total_changes != self._at:
            self.rollback()
            raise sqlite3.OperationalError("database or disk is full")
        return super().__exit__(kind, exc, tb)


@pytest.fixture
def disk(monkeypatch):
    """Every Store opened from here on is on a FullDisk connection; set FullDisk.full to fill the disk."""
    real = store_mod.connect

    def connect(path, readonly=False):
        if readonly:   # a backup's snapshot, a check: as they are
            return real(path, readonly)
        db = sqlite3.connect(path, factory=FullDisk)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db
    monkeypatch.setattr(store_mod, "connect", connect)
    monkeypatch.setattr(FullDisk, "full", False)
    return FullDisk


def _server_env(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CHART_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("CHART_FEED", "none")
    monkeypatch.setenv("CHART_QUOTES_FEED", "none")


def _es_history(c) -> tuple[dict, dict]:
    """The hello an app gets, and the ES 1m chart's history."""
    with c.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        while hello["type"] != "hello":
            hello = ws.receive_json()
        ws.send_json({"op": "sub", "id": 1, "symbol": "ES", "spec": "1m"})
        while (msg := ws.receive_json())["type"] not in ("history", "error"):
            pass
    return hello, msg


# --- r15-longrun-1: a full disk and the server's restart ------------------------------------------------------------
def test_the_server_starts_again_with_its_disk_full(tmp_path, monkeypatch, disk, in_session, caplog):
    """Docker's disk filled while the server ran; then the PC restarted (or update.cmd, or a saved Instruments list).
    Each start's own writes (the watchlists' new version, the built-in symbols) can't be made: the server starts all
    the same and shows what is stored, rather than failing every start in Docker's restart loop."""
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    _server_env(tmp_path, monkeypatch)
    t0 = in_session // 60_000 * 60_000
    with TestClient(app) as c:
        m = app.state.market
        for i in range(5):
            c.portal.call(m.on_tick, Tick("ES", t0 - 300_000 + i * 60_000 + 1000, 7000.0 + i, 1, 1))
        version = m.wl["version"]
    disk.full = True
    with TestClient(app) as c:   # failed in the lifespan before: wl.load's set_setting, then SymbolDB's builtin rows
        m = app.state.market
        assert c.get("/api/health").status_code == 200
        hello, history = _es_history(c)
        assert hello["watchlists"]["version"] > version and hello["watchlists"]["lists"] == m.wl["lists"]
        assert history["type"] == "history" and [b["t"] for b in history["bars"]][:5] == \
            [t0 - 300_000 + i * 60_000 for i in range(5)]
        assert m.symbols.type_of("ES1!") == "future" and m.symbols.yahoo_for("ES1!") == "ES=F"   # the stored ones
        results = c.get("/api/symbols/search", params={"q": "ES"}).json()["results"]
        assert results[0]["symbol"] == "ES1!"
    said = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(x.startswith("watchlists: watchlists not saved (database or disk is full)") for x in said)
    assert any(x.startswith("built-in symbols not saved (database or disk is full)") for x in said)


def test_a_stop_with_the_disk_full_still_closes_the_store(tmp_path, monkeypatch, disk, in_session):
    """The disk fills while trades wait to be written: the stop's last write fails, and the store is closed all the
    same (a restart in place after it happens too), not left open with its -wal by an error out of the lifespan."""
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    _server_env(tmp_path, monkeypatch)
    with TestClient(app) as c:
        m = app.state.market
        assert c.get("/api/health").status_code == 200
        disk.full = True
        c.portal.call(m.on_tick, Tick("ES", in_session, 7000.0, 1, 1))
    assert [t.price for t in m.tick_buf] == [7000.0]   # not written
    with pytest.raises(sqlite3.ProgrammingError):   # closed
        m.store.db.execute("SELECT 1")


# --- r15-longrun-2: Back up now on a full disk ----------------------------------------------------------------------
def test_back_up_now_works_with_the_database_disk_full(tmp_path, monkeypatch, disk):
    """The database's disk (Docker's) is full and trades wait to be written; the backups go to another disk (Docker:
    Documents on the PC). The backup is written: it holds no market data, so the failed write has no part in it."""
    m, _ = build_market(tmp_path, monkeypatch, CHART_BACKUP_DIR=str(tmp_path / "bk"))
    m.store.db.execute("INSERT INTO drawings VALUES ('ES', 'd1', '{}')")
    m.store.db.commit()
    disk.full = True
    m.on_tick(Tick("ES", int(time.time() * 1000), 7000.0, 1, 1))
    p = asyncio.run(m.backups.now())   # raised the store's OperationalError before
    assert p.parent == tmp_path / "bk" and m.backups.error == ""
    assert sqlite3.connect(p).execute("SELECT symbol FROM drawings").fetchall() == [("ES",)]
    assert len(m.tick_buf) == 1   # still waiting for the database


# --- r15-longrun-3: a data outage with the add-on connected ---------------------------------------------------------
def test_a_data_outage_with_the_add_on_connected_has_its_hole_asked_for(tmp_path, monkeypatch):
    """NinjaTrader's data connection drops for 20 minutes mid-session and comes back by itself; the add-on's
    connection to the server stays up (no hello). When the watchdog sees ES tick again after Feed stale, the server
    asks for the hole the outage left, then takes the answer, rather than waiting for the next connect (days later,
    when the hole can be older than HOLE_DAYS)."""
    def ms(h, mi, s=0):
        return int(datetime(2026, 10, 14, h, mi, s, tzinfo=CT).timestamp() * 1000)
    clock = {"now": ms(10, 0)}
    monkeypatch.setattr(time, "time", lambda: clock["now"] / 1000)
    m, notifier = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    m.store.write([], [("ES", Bar(t=ms(9, k), o=7000, h=7001, l=6999, c=7000.5, v=10, bv=5, sv=5, n=3)) for k in range(60)])

    async def go():
        feed = NinjaTraderFeed("127.0.0.1", 0, "")
        m.feed_objs.append(feed)
        server = await asyncio.start_server(lambda r, w: feed._handle(m, r, w), "127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        w.write(b"HELLO||19\n")
        await w.drain()
        asked = []
        while not (line := (await asyncio.wait_for(r.readline(), 3)).decode().strip()).startswith("W|"):
            asked += [line.split("|")[1]] if line.startswith("GAP|") else []
        for sym in asked:   # the connect's asks answered: nothing there
            w.write(f"Z|{sym}|ticks|0|ok\n".encode())

        async def until(done):   # the server has read the lines sent
            for _ in range(300):
                if done():
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("not read")

        async def trades(minutes):
            for k in minutes:
                clock["now"] = ms(10, k, 1)
                w.write(f"T|ES|{clock['now']}|7000.25|1|1\n".encode())
            await w.drain()
            await until(lambda: "ES" in m.last_tick and m.last_tick["ES"].ts == clock["now"])

        await trades(range(5))           # 10:00-10:04 live
        clock["now"] = ms(10, 10)        # the outage: only heartbeats
        await m._watchdog_pass()
        await trades(range(25, 27))      # back 20 minutes later, on the same connection
        await m._watchdog_pass()
        line = (await asyncio.wait_for(r.readline(), 3)).decode().strip()
        _, _, start, end = line.split("|")
        for t in (ms(10, 5, 2), ms(10, 15, 2), ms(10, 24, 30)):   # NinjaTrader's history for it
            w.write(f"X|ES|{t}|7001.0|2\n".encode())
        w.write(b"Z|ES|ticks|3|ok\n")
        await w.drain()
        await until(lambda: "ES" not in m.gapfill.windows)
        w.close()
        server.close()
        return asked, line, int(start), int(end)
    asked, line, start, end = asyncio.run(go())
    assert "ES" not in asked   # nothing missing at the connect
    assert [t for t, say in notifier.sent if say.startswith(("No ES ", "ES "))] == ["Feed stale", "Feed back"]
    assert line.startswith("GAP|ES|") and (start, end) == (ms(10, 4, 1) - gapfill_mod.PROBE_MS, ms(10, 25))
    bars = dict(m.store.db.execute("SELECT t, v FROM bars_1m WHERE symbol='ES' AND t>=? AND t<?", (ms(10, 5), ms(10, 25))))
    assert bars == {ms(10, 5): 2, ms(10, 15): 2, ms(10, 24): 2}
    assert m.gapfill.holes("ES", clock["now"]) == [] and m.gapfill.again("ES", clock["now"]) is None   # each once


# --- r15-longrun-4: a closed free-data chart's quotes ---------------------------------------------------------------
def test_a_closed_free_data_charts_symbol_is_quoted_no_more(tmp_path, monkeypatch):
    """The user looks through stocks on free-data charts, then goes back to ES for the week: no other free-data chart
    polls. Each closed chart's symbol drops out of the feeds' subscriptions (IBKR's market-data lines) and out of every
    hello's quotes CHART_QUOTE_S after the chart's last poll; one still charted stays."""
    m, _ = build_market(tmp_path, monkeypatch)
    heard = []

    class Ibkr:
        name = "ibkr"

        def watch_changed(self, market):
            heard.append(set(market.watch_keys))
    m.feed_objs.append(Ibkr())
    for sym in ("KO", "PEP", "WMT"):
        m.watch_chart(sym)
        m.on_quote(sym, 100.0, 99.0, 1000, source="ibkr")
    assert {"KO", "PEP", "WMT"} <= heard[-1] and {"KO", "PEP", "WMT"} <= set(m.quotes)
    n = len(heard)
    for sym in ("KO", "PEP"):   # closed 10 minutes ago
        m.chart_keys[sym] = time.monotonic() - 1
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))   # the loop's minute

    async def go():
        task = asyncio.create_task(m._housekeeping_loop())
        for _ in range(5):
            await real_sleep(0)
        task.cancel()
    asyncio.run(go())
    assert len(heard) == n + 1   # once, with them gone
    assert not {"KO", "PEP"} & heard[-1] and "WMT" in heard[-1] and not {"KO", "PEP"} & m.watch_keys
    assert not {"KO", "PEP"} & set(m.quotes) and "WMT" in m.quotes and "KO" not in m.chart_keys
