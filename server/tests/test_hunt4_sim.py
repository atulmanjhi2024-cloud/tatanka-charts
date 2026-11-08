"""Bug hunt round 4, the simulator: a backup restored onto it, history imported on it, its quotes on real charts, and
leaving it with little disk space."""
import asyncio
import sqlite3
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from chartsrv import backup, setup
from chartsrv.config import load_settings
from chartsrv.models import Bar, Tick
from chartsrv.store import Store
from test_hunt_web_chart import needs_node


def _install(path, *, left: bool, trades: bool = True) -> None:
    """A database with trades stored: an install on the simulator, or one that left it (ntfy saved, a one-shot ES
    alert)."""
    st = Store(path)
    setup.init(st)
    if trades:
        st.write([Tick("ES", 1_760_000_000_000 + i, 6900.0, 1, 1) for i in range(5)],
                 [("ES", Bar(t=1_760_000_000_000, o=6900, h=6900, l=6900, c=6900, v=5))])
    if left:
        st.set_setting(setup.LEAVE_SIM, "done")
        st.set_setting("connections", {"ntfy": {"ntfy_url": "https://ntfy.sh/topic-zz91"}})
        st.add_alert("ES", "price", {"price": 6900.5, "direction": "above"}, False, "")
    st.close()


def _count(path, table):
    db = sqlite3.connect(path)
    try:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        db.close()


def test_a_backup_from_off_the_simulator_restored_onto_it_leaves_it(tmp_path, monkeypatch, capsys):
    """r4-fresh-install-0: a new PC's fresh install runs the simulator; the old PC's backup (it had left the simulator)
    is restored there. Its alerts and channels must not fire on the simulator's invented prices: the next start leaves
    the simulator, as the old PC had (its invented trades deleted, a copy kept), and runs on the real feeds."""
    (tmp_path / "old").mkdir()
    (tmp_path / "logs").mkdir()
    _install(tmp_path / "old" / "chart.db", left=True)
    bk = backup.make(tmp_path / "old" / "chart.db", tmp_path / "bk")
    new = tmp_path / "chart.db"
    _install(new, left=False)   # the new PC: a fresh install, the simulator's trades stored
    monkeypatch.setenv("CHART_DB", str(new))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CHART_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_QUOTES_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "5")
    assert backup.main(["restore", bk.name, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "leaves the simulator" in out and "invented trades are deleted" in out
    st = Store(new)
    assert st.get_setting(setup.LEAVE_SIM) == "pending" and len(st.list_alerts()) == 1
    st.close()

    from chartsrv.app import app
    with TestClient(app) as c:   # the start after the restore
        m = app.state.market
        assert "sim" not in m.feed_names and "ninjatrader" in m.feed_names and m.s.quotes_feed == "yahoo"
        assert c.get("/api/setup").json()["left_sim"] is True
        assert [a["enabled"] for a in c.get("/api/alerts").json()] == [True]   # the one-shot alert is still armed
    assert _count(new, "ticks") == 0 and _count(new, "bars_1m") == 0 and _count(new, "alert_events") == 0
    assert len(list(tmp_path.glob("chart.db.bak-*"))) == 1


def test_a_restore_never_wipes_an_install_that_is_not_on_the_simulator(tmp_path, monkeypatch, capsys):
    """The other cases stay as they were: a server on NinjaTrader (CHART_FEED=ninjatrader, never on the simulator) keeps
    its trades, and a backup made on the simulator restored onto it keeps the simulator."""
    (tmp_path / "old").mkdir()
    (tmp_path / "logs").mkdir()
    _install(tmp_path / "old" / "chart.db", left=True)
    left = backup.make(tmp_path / "old" / "chart.db", tmp_path / "bk")
    new = tmp_path / "chart.db"
    _install(new, left=False)
    monkeypatch.setenv("CHART_DB", str(new))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CHART_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("CHART_FEED", "ninjatrader")
    assert backup.main(["restore", left.name, "--yes"]) == 0
    assert "simulator" not in capsys.readouterr().out
    st = Store(new)
    assert st.get_setting(setup.LEAVE_SIM) is None
    st.close()
    assert _count(new, "ticks") == 5
    monkeypatch.delenv("CHART_FEED")   # the same restore run where .env isn't read (the server set to NinjaTrader)
    assert backup.main(["restore", left.name, "--yes"]) == 0
    capsys.readouterr()
    st = Store(new)
    assert setup.leave_sim(st, replace(load_settings(), feed="ninjatrader")).feed == "ninjatrader"
    assert st.get_setting(setup.LEAVE_SIM) is None   # the server's own feed decides: nothing to leave, nothing deleted
    st.close()
    assert _count(new, "ticks") == 5 and list(tmp_path.glob("chart.db.bak-*")) == []

    (tmp_path / "sim").mkdir()
    _install(tmp_path / "sim" / "chart.db", left=False)
    on_sim = backup.make(tmp_path / "sim" / "chart.db", tmp_path / "bk", "-sim")
    monkeypatch.setenv("CHART_FEED", "sim")
    assert backup.main(["restore", on_sim.name, "--yes"]) == 0
    assert "leaves the simulator" not in capsys.readouterr().out
    st = Store(new)
    assert st.get_setting(setup.LEAVE_SIM) is None
    st.close()
    assert _count(new, "ticks") == 5


def test_history_is_not_imported_onto_the_simulator(tmp_path, monkeypatch, capsys):
    """r4-fresh-install-1: leaving the simulator deletes every stored trade and bar, so history imported while it runs
    (DATA.md's backfill or Databento import) would go with its invented ones, after mixing with them in the charts. The
    imports wait until it is left, and say so; a dry run still shows what would be imported."""
    from chartsrv import backfill, databento
    db = tmp_path / "chart.db"
    _install(db, left=False)
    csv = tmp_path / "ES_1min.csv"
    csv.write_text("Datetime,Open,High,Low,Close,Volume\n2026-10-15 08:30:00,6610.0,6611.0,6609.75,6610.5,300\n")
    monkeypatch.setenv("CHART_DB", str(db))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "sim")
    for argv, main in ((["backfill", "ES", str(csv)], backfill.main), (["databento", str(tmp_path)], databento.main)):
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit) as e:
            main()
        assert "Leave the simulator first" in str(e.value.code)
    assert _count(db, "bars_1m") == 1
    monkeypatch.setattr("sys.argv", ["databento", str(tmp_path), "--dry-run"])
    databento.main()
    assert "Dry run" in capsys.readouterr().out

    st = Store(db)
    st.set_setting(setup.LEAVE_SIM, "done")   # left: imported as before
    st.close()
    monkeypatch.setattr("sys.argv", ["backfill", "ES", str(csv)])
    backfill.main()
    assert _count(db, "bars_1m") == 2
    db2 = tmp_path / "nt.db"
    _install(db2, left=False)
    monkeypatch.setenv("CHART_DB", str(db2))
    monkeypatch.setenv("CHART_FEED", "ninjatrader")   # never on the simulator
    backfill.main()
    assert _count(db2, "bars_1m") == 2


# ---- r4-fresh-install-2: the simulator's quotes stay off real charts -------------------------------------------------

SIM_QUOTE_JS = """
const NY = { tz: 'America/New_York', rth: ['09:30', '16:00'], day: '00:00' }, TZ_EXCHANGE = 'America/Chicago';
const S = { rth: false, replay: null, symbol: 'SPY', spec: '5m', bars: [], quotes: {}, lastBar: null };
const wlKey = (s) => s, wlIsFormula = () => false, exch = () => S.ext.exchange, applyBars = (m) => { S.bars[S.bars.length - 1] = m.bars[0]; };
const els = {}, el = () => ({ textContent: '', className: '', parts: {}, querySelector(k) { return (this.parts[k] ||= { textContent: '' }); } });
const $ = (k) => (els[k] ||= el()), px = (v) => (v == null ? '' : v.toFixed(2));
/*APP*/
const { toInput: msToCtInput, toMs: ctInputToMs } = inputClock(TZ_EXCHANGE);
const t = Date.UTC(2026, 9, 26, 15, 0), out = [];   // a 5m bar at 11:00 ET, Yahoo's (SPY, about 680)
for (const [ext, src] of [[{ source: 'yahoo', exchange: NY }, 'sim'], [{ source: 'yahoo', exchange: NY }, 'yahoo'], [null, 'sim']]) {
  const q = { s: 'SPY', last: 243, prev: 241.28, ts: t + 60_000, src };
  Object.assign(S, { ext, bars: [{ seq: 1, t, o: 680, h: 680.5, l: 679.5, c: 680.25 }], quotes: { SPY: q } });
  if (ext) extQuote([q]);
  S.lastBar = S.bars[0];
  els['#q-chg'] = el();
  header();
  out.push([S.bars[0].l, S.bars[0].c, els['#q-chg'].querySelector('.abs').textContent]);
}
console.log(JSON.stringify(out));
"""


@needs_node
def test_the_simulators_quote_never_moves_a_free_data_chart():
    """A fresh install on the simulator, SPY's free-data chart (Yahoo's real bars about 680): the simulator's invented
    quote (243) neither drags the forming bar down to it nor gives the header a change against its invented close. A
    real quote still moves the bar; the simulator's own charts keep its quote."""
    from test_hunt_web_chart import run_app
    got = run_app(["calSpec", "specSeconds", "inputClock", "wallClock", "extWall", "tradeDate", "extBarEnd", "extQuote",
                   "symQuote", "header"], SIM_QUOTE_JS)
    assert got[0] == [679.5, 680.25, ""]          # the simulator's quote on Yahoo's bars: left off
    assert got[1] == [243, 243, "+1.72"]          # a real quote moves the bar, as before
    assert got[2] == [679.5, 680.25, "+438.97"]   # a chart of the simulator's own trades


def test_the_details_panel_leaves_the_simulators_quote_off_real_closes(tmp_path, monkeypatch):
    """The details panel on the simulator: SPY's performance comes from Yahoo's real closes, not from the simulator's
    invented quote against them (-64%)."""
    from chartsrv import details
    from test_review_fixes import build_market
    m, _ = build_market(tmp_path, monkeypatch)
    day = 86_400_000
    now = 1_793_030_400_000   # 2026-10-26
    rows = [{"t": now - k * day, "o": 680.0, "h": 681.0, "l": 679.0, "c": 680.0 - k * 0.1} for k in range(400, 0, -1)]

    async def yahoo(ticker, spec):
        return rows, {}
    monkeypatch.setattr(m.ext, "_yahoo", yahoo)
    monkeypatch.setattr(m.ext, "yahoo_ticker", lambda sym: sym)
    m.quotes["SPY"] = {"s": "SPY", "last": 243.0, "prev": 241.28, "src": "sim"}
    perf = asyncio.run(details.details(m, "SPY", now))["perf"]
    assert perf and all(abs(v) < 10 for v in perf.values()), perf
    m.quotes["SPY"] = {"s": "SPY", "last": 340.0, "prev": 680.0, "src": "yahoo"}   # a real quote is still the price
    assert asyncio.run(details.details(m, "SPY", now))["perf"]["1W"] < -40


# ---- r4-crashes-2: leaving the simulator on a nearly full disk ---------------------------------------------------------

def _sim_env(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_QUOTES_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "5")


def test_leaving_the_simulator_without_room_for_the_copy_still_starts(tmp_path, monkeypatch):
    """The restart that leaves the simulator copies the whole database first. With no room for that copy (a full
    disk), the server still starts, on the simulator with nothing deleted, leaves no part-copy taking what room is
    left, does not try again at every start, and Connections > NinjaTrader says why."""
    db = tmp_path / "chart.db"
    _install(db, left=False)
    st = Store(db)
    st.set_setting(setup.LEAVE_SIM, "pending")   # Leave the simulator was pressed: the restart wipes
    st.close()
    real = sqlite3.connect

    def full_disk(path, *a, **k):   # the copy runs out of room after a page or two
        c = real(path, *a, **k)
        if ".bak-" in str(path):
            c.execute("PRAGMA max_page_count=2")
        return c
    monkeypatch.setattr(sqlite3, "connect", full_disk)
    _sim_env(tmp_path, monkeypatch)
    from chartsrv.app import app
    with TestClient(app) as c:
        assert "sim" in app.state.market.feed_names
        assert c.get("/api/setup").json()["left_sim"] is False
        nt = next(x for x in c.get("/api/connections").json()["connections"] if x["id"] == "ninjatrader")
        assert nt["state"] == "error" and "disk is full" in nt["status"] and "Leave the simulator again" in nt["status"]
    mine = sqlite3.connect(db)
    assert mine.execute("SELECT COUNT(*) FROM ticks WHERE ts < 1760000000005").fetchone()[0] == 5   # nothing deleted
    mine.close()
    assert list(tmp_path.glob("chart.db.bak-*")) == []
    st = Store(db)
    assert st.get_setting(setup.LEAVE_SIM)["failed"]
    st.close()


def test_leave_the_simulator_is_refused_without_room_for_the_copy(tmp_path, monkeypatch):
    """Leave the simulator says at once when the disk can't hold the copy of the database made first, rather than
    restarting into a wipe that can't finish."""
    import shutil
    import signal
    from collections import namedtuple
    _sim_env(tmp_path, monkeypatch)
    kills = []
    monkeypatch.setattr(signal, "raise_signal", kills.append)
    from chartsrv.app import app
    with TestClient(app) as c:
        size = app.state.market.backups.disk()["db"]
        usage = namedtuple("usage", "total used free")
        monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10 ** 12, 10 ** 12, size // 2))
        r = c.post("/api/setup/leave-sim")
        assert r.status_code == 507 and "Not enough free disk space" in r.json()["detail"]
        assert c.get("/api/setup").json()["left_sim"] is False
        monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10 ** 12, 0, 10 ** 12))
        assert c.post("/api/setup/leave-sim").json() == {"ok": True}
        time.sleep(0.8)
    assert kills and Store(tmp_path / "chart.db").get_setting(setup.LEAVE_SIM) == "pending"


def test_a_vacuum_without_room_still_leaves_the_simulator(tmp_path, monkeypatch):
    """The trades are deleted and committed before the VACUUM that shrinks the file. With no room for it, the
    simulator is left all the same (its space is reused), not tried again over a database already wiped."""
    from chartsrv import wipe_market_data

    class NoRoom(sqlite3.Connection):
        def execute(self, sql, *a):
            if sql == "VACUUM":
                raise sqlite3.OperationalError("database or disk is full")
            return super().execute(sql, *a)
    monkeypatch.setattr(wipe_market_data, "connect", lambda p: sqlite3.connect(p, factory=NoRoom))
    db = tmp_path / "chart.db"
    _install(db, left=False)
    st = Store(db)
    st.set_setting(setup.LEAVE_SIM, "pending")
    _sim_env(tmp_path, monkeypatch)
    said = []
    assert setup.leave_sim(st, load_settings(), said.append).feed == "ninjatrader"
    assert st.get_setting(setup.LEAVE_SIM) == "done" and any("VACUUM" in x for x in said)
    st.close()
    assert _count(db, "ticks") == 0 and len(list(tmp_path.glob("chart.db.bak-*"))) == 1
