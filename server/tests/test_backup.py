"""Backups: what you made (alerts, drawings, settings, keys), not market data; kept 30 deep; restore."""
import asyncio
import os
import sqlite3
import time

import pytest

from chartsrv import backup
from chartsrv.models import Tick
from test_review_fixes import build_market


def _count(db, table):
    return sqlite3.connect(db).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_backup_and_restore(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, CHART_BACKUP_DIR=str(tmp_path / "bk"))
    s = m.store
    s.set_setting("watchlists_probe", {"a": 1})
    s.db.execute("INSERT INTO drawings VALUES ('ES', 'd1', '{}')")
    s.db.commit()
    m.connections.put("eia", {"eia_api_key": "abcdefgh1234"})
    m.on_tick(Tick("ES", int(time.time() * 1000), 7000.0, 1, 1))
    m.flush()

    async def go():
        return await m.backups.now()
    p = asyncio.run(go())
    assert p.parent == tmp_path / "bk" and backup.NAME_RE.match(p.name)
    b = sqlite3.connect(p)
    tables = {r[0] for r in b.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"alerts", "drawings", "settings", "push_subs", "backup_meta"} <= tables and not tables & backup.MARKET
    assert b.execute("SELECT data FROM drawings").fetchone() == ("{}",) and _count(s.path, "ticks") == 1
    b.close()

    # changes after the backup; then restore it (the server is stopped for this)
    s.db.execute("DELETE FROM drawings")
    s.db.execute("INSERT INTO drawings VALUES ('NQ', 'd2', '{}')")
    s.db.commit()
    s.close()
    counts = backup.restore(s.path, p)
    db = sqlite3.connect(s.path)
    assert db.execute("SELECT symbol FROM drawings").fetchall() == [("ES",)] and counts["drawings"] == 1
    assert db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1   # market data stays
    assert "abcdefgh1234" in db.execute("SELECT value FROM settings WHERE key='connections'").fetchone()[0]
    before = [f for f in backup.files(tmp_path / "bk") if "before-restore" in f.name]
    assert len(before) == 1 and sqlite3.connect(before[0]).execute("SELECT symbol FROM drawings").fetchall() == [("NQ",)]
    with pytest.raises(ValueError):
        backup.restore(s.path, s.path)   # not a backup


def test_prune_and_cli(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "bk"
    folder.mkdir()
    for i in range(35):
        f = folder / f"tatanka-202601{i // 10 + 1:02d}-0000{i % 10:02d}.db"
        f.write_bytes(b"x")
    (folder / "tatanka-20260101-000000-before-restore.db").write_bytes(b"x")
    (folder / "notes.txt").write_text("mine")
    assert backup.prune(folder) == 5
    names = [p.name for p in backup.files(folder)]
    assert len(names) == 31 and (folder / "notes.txt").exists() and "tatanka-20260101-000000.db" not in names

    m, _ = build_market(tmp_path, monkeypatch, CHART_BACKUP_DIR=str(folder))
    m.store.close()
    assert backup.main(["now"]) == 0
    newest = backup.files(folder)[0]
    assert backup.main(["restore", newest.name]) == 2 and "Stop the server first" in capsys.readouterr().out
    assert backup.main(["restore", newest.name, "--yes"]) == 0
    full = tmp_path / "full.db"
    assert backup.main(["full", str(full)]) == 0 and os.path.getsize(full) > 0


def test_a_full_copy_by_name_goes_in_the_backups_folder(tmp_path, monkeypatch, capsys):
    """In Docker the command runs in /app, inside the container, which the next update.cmd rebuilds away: a name alone
    goes in the backups folder (BACKUP_DIR on the PC), and a path on the PC is refused."""
    folder, app = tmp_path / "bk", tmp_path / "app"
    app.mkdir()
    monkeypatch.chdir(app)
    m, _ = build_market(tmp_path, monkeypatch, CHART_BACKUP_DIR=str(folder))
    m.store.close()
    assert backup.main(["full", "tatanka-full.db"]) == 0
    assert os.path.getsize(folder / "tatanka-full.db") > 0 and str(folder / "tatanka-full.db") in capsys.readouterr().out
    if os.name != "nt":
        assert backup.main(["full", r"C:\Users\you\Documents\tatanka-full.db"]) == 2
        assert "a path on the PC" in capsys.readouterr().out
    assert list(app.iterdir()) == [] and backup.files(folder) == []   # not one of the daily ones, never pruned


def test_low_disk_is_said(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, CHART_BACKUP_DIR=str(tmp_path / "bk"))
    running = {f.name: f for f in m.feed_objs}
    st = m.connections._status("backup", m, running)
    assert st["state"] == "warn" and "database 0.0 GB" in st["status"] and "GB free" in st["status"]
    monkeypatch.setattr(backup, "LOW_DISK", 10 ** 18)   # every disk is "almost full"
    st = m.connections._status("backup", m, running)
    assert st["state"] == "error" and st["status"].startswith("Disk almost full")
    sent = []

    async def send(title, msg):
        sent.append(title)
        return "discord"
    monkeypatch.setattr(m.notifier, "send", send)

    async def go():
        await m.backups.check_disk()
        await m.backups.check_disk()   # once a day, not every hour
    asyncio.run(go())
    assert sent == ["Disk almost full"]


def test_restore_keeps_what_describes_the_market_data(tmp_path, monkeypatch):
    """The simulator left, the setup done, the contract rolls and asked holes describe the trades in this database,
    which a restore keeps: a backup from the simulator days must not turn the simulator back on over real trades, and
    one from an install that left it must not turn it off over the simulator's invented ones."""
    from dataclasses import replace

    from chartsrv import gapfill, setup
    from chartsrv.store import Store
    m, _ = build_market(tmp_path, monkeypatch, CHART_BACKUP_DIR=str(tmp_path / "bk"))
    s = m.store
    s.set_setting(setup.SETTING, {"done": False, "step": 0, "fresh": True})
    s.set_setting("watchlists_probe", {"a": 1})
    on_sim = backup.make(s.path, tmp_path / "bk")              # the first backup, on the simulator
    s.set_setting(setup.SETTING, {"done": True, "step": 4, "fresh": True})
    s.set_setting(setup.LEAVE_SIM, "done")
    s.set_setting("contracts", {"ES": "ES 12-26"})
    s.set_setting(gapfill.ROLLED, {"ES": 1_700_000_000_000})
    s.set_setting(gapfill.ASKED, {"ES": [[1, 2]]})
    s.set_setting("watchlists_probe", {"a": 2})
    left = backup.make(s.path, tmp_path / "bk", "-left")       # a later one, the simulator left
    s.close()

    backup.restore(s.path, on_sim)
    s = Store(s.path)
    assert s.get_setting("watchlists_probe") == {"a": 1}       # what you made comes back
    assert s.get_setting(setup.LEAVE_SIM) == "done" and s.get_setting(setup.SETTING)["done"] is True
    assert s.get_setting("contracts") == {"ES": "ES 12-26"} and s.get_setting(gapfill.ROLLED) == {"ES": 1_700_000_000_000}
    assert s.get_setting(gapfill.ASKED) == {"ES": [[1, 2]]}
    assert setup.leave_sim(s, replace(m.s, feed="sim")).feed == "ninjatrader"   # still off the simulator

    # the other way: a fresh install still on the simulator, and a backup made after leaving it
    for k in (setup.LEAVE_SIM, "contracts", gapfill.ROLLED, gapfill.ASKED):
        s.db.execute("DELETE FROM settings WHERE key=?", (k,))
    s.set_setting(setup.SETTING, {"done": False, "step": 0, "fresh": True})
    s.db.commit()
    s.close()
    backup.restore(s.path, left)
    s = Store(s.path)
    assert s.get_setting("watchlists_probe") == {"a": 2}
    assert s.get_setting(setup.LEAVE_SIM) is None and s.get_setting(setup.SETTING)["done"] is False
    assert s.get_setting("contracts") is None and s.get_setting(gapfill.ROLLED) is None
    assert setup.leave_sim(s, replace(m.s, feed="sim")).feed == "sim"
    s.close()
