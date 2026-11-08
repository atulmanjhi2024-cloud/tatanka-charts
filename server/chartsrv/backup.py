"""Backups of what you made: alerts and their history, drawings, watchlists, workspaces, indicator and chart settings,
Connections (keys included), app-notification devices, sessions, fills and positions. Market data (ticks, 1m bars)
and caches the server fills again by itself (symbols, FRED, EIA, COT) are left out, so a backup is small.

The server writes one a day (and on Back up now) to CHART_BACKUP_DIR (Docker: the backups folder on the PC, BACKUP_DIR
in .env) and keeps the newest KEEP. Each is an ordinary SQLite file. The keys inside are as secret as .env: keep the folder
private and out of git.

    python -m chartsrv.backup now                  # one now
    python -m chartsrv.backup list
    python -m chartsrv.backup restore <file> --yes # the server must be stopped first (it holds these in memory)
    python -m chartsrv.backup full <file>          # a copy of the whole database, market data too (can be large),
                                                   # in the backups folder unless <file> is a whole path
"""
import argparse
import asyncio
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from . import setup
from .gapfill import ASKED, OLDER_LIMIT, ROLLED
from .store import Store, connect

log = logging.getLogger(__name__)
KEEP = 30
LOW_DISK = 5_000_000_000   # bytes free under which the app warns (Connections > Backups, and an alert a day)
EVERY_S = 24 * 3600
NAME_RE = re.compile(r"^tatanka-\d{8}-\d{6}(-before-restore)?\.db$")
MARKET = {"ticks", "bars_1m", "symbols", "fred_series", "eia_series", "cot_series", "vp_cache"}   # refilled by the server itself
# settings about the market data in this database, which a restore leaves: the simulator left (its trades deleted), the
# setup guide, the contracts it is on (and the old names noted to rename once: market.OLD_NAMES), their last rolls and
# the holes asked for, where NinjaTrader's older history ends; and this PC's own tailscale serve names. Not the backup's
# (but see restore()'s on_sim).
KEPT = (setup.LEAVE_SIM, setup.SETTING, "contracts", "contracts_old_names", ROLLED, ASKED, OLDER_LIMIT, setup.TS_NAMES)


def backup_dir(settings) -> Path:
    return Path(getattr(settings, "backup_dir", None) or Path(settings.db_path).parent / "backups")


def _user_tables(src: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(n, sql) for n, sql in src.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL "
                                               "AND name NOT LIKE 'sqlite_%' ORDER BY name") if n not in MARKET]


def make(db_path, folder, suffix: str = "", feed: str | None = None) -> Path:
    """Writes a backup file and returns it. Blocking: run it in a thread. Reads one consistent snapshot. feed: the
    trade feed it runs on, noted for restore() (with .env's sim, its leave_sim says whether the simulator was left)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"tatanka-{datetime.now().strftime('%Y%m%d-%H%M%S')}{suffix}.db"
    out, tmp = folder / name, folder / (name + ".part")
    tmp.unlink(missing_ok=True)
    src = connect(db_path, readonly=True)
    dst = sqlite3.connect(tmp)
    try:
        src.execute("BEGIN")   # one snapshot of every table
        counts = {}
        for table, sql in _user_tables(src):
            dst.execute(sql)
            cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
            rows = src.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"')
            dst.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols))})', rows)
            counts[table] = dst.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        dst.execute("CREATE TABLE backup_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        dst.executemany("INSERT INTO backup_meta VALUES (?,?)", [("created", str(int(time.time() * 1000))), ("version", "1"),
                                                                ("counts", repr(counts))]
                       + ([("feed", feed)] if feed else []))
        dst.commit()
    finally:
        src.close()
        dst.close()
    os.replace(tmp, out)
    return out


def files(folder) -> list[Path]:
    folder = Path(folder)
    return sorted((p for p in folder.glob("tatanka-*.db") if NAME_RE.match(p.name)), key=lambda p: p.name, reverse=True) \
        if folder.is_dir() else []


def prune(folder, keep: int = KEEP) -> int:
    """Keeps the newest `keep` daily backups (the ones made before a restore are kept too, apart)."""
    daily = [p for p in files(folder) if "before-restore" not in p.name]
    for p in daily[keep:]:
        p.unlink(missing_ok=True)
    return max(0, len(daily) - keep)


def _real_feed(db) -> bool:
    """The backup attached as bk comes from an install on a real trade feed: the one it ran on (make() notes it), or in
    a backup from before that, what only a real feed writes: the contracts it is on, your fills and positions."""
    feed = db.execute("SELECT value FROM bk.backup_meta WHERE key='feed'").fetchone()
    if feed:
        return feed[0] != "sim"
    has = {r[0] for r in db.execute("SELECT name FROM bk.sqlite_master WHERE type='table'")}
    if "settings" in has and db.execute("SELECT 1 FROM bk.settings WHERE key='contracts' "
                                        "AND value NOT IN ('{}', 'null')").fetchone():
        return True
    return any(t in has and db.execute(f'SELECT 1 FROM bk."{t}" LIMIT 1').fetchone() for t in ("fills", "positions"))


def restore(db_path, backup: Path, on_sim: bool = False, say=log.info) -> dict:
    """Replaces the user data in the database with the backup's; market data stays, and the KEPT settings about it.
    A backup of what is replaced is written first, next to the backup. Returns {table: rows}. The server must be
    stopped. on_sim: this server runs the simulator (.env's CHART_FEED=sim, not left). A backup from an install off it
    (a new PC's fresh install, the old PC's backup: it had left the simulator, or ran NinjaTrader from .env) leaves it
    here too at the next start, as Leave the simulator does: its invented trades deleted, a copy kept. Kept on, it would
    fire the restored alerts at its invented prices on the restored channels, and use up the one-shot ones."""
    backup = Path(backup)
    probe = connect(backup, readonly=True)
    try:
        if not probe.execute("SELECT 1 FROM sqlite_master WHERE name='backup_meta'").fetchone():
            raise ValueError(f"{backup.name} is not a Tatanka Charts backup")
    finally:
        probe.close()
    if not Path(db_path).exists():   # an install never started yet: its database made as its first start would
        st = Store(db_path)
        setup.init(st)
        st.close()
    make(db_path, backup.parent, "-before-restore", "sim" if on_sim else None)
    db = sqlite3.connect(db_path)
    try:
        db.execute("ATTACH DATABASE ? AS bk", (str(backup),))
        out, leaving, real = {}, False, _real_feed(db)
        marks = ",".join("?" * len(KEPT))
        kept = db.execute(f"SELECT key, value FROM main.settings WHERE key IN ({marks})", KEPT).fetchall() \
            if db.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='settings'").fetchone() else []
        with db:
            for table, sql in db.execute("SELECT name, sql FROM bk.sqlite_master WHERE type='table' AND name != 'backup_meta' "
                                         "AND name NOT LIKE 'sqlite_%'").fetchall():
                if table in MARKET:
                    continue
                if not db.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    db.execute(sql)   # unqualified: made in main
                mine = {r[1] for r in db.execute(f'PRAGMA main.table_info("{table}")')}
                cols = [r[1] for r in db.execute(f'PRAGMA bk.table_info("{table}")') if r[1] in mine]
                q = ", ".join(f'"{c}"' for c in cols)
                db.execute(f'DELETE FROM main."{table}"')
                db.execute(f'INSERT INTO main."{table}" ({q}) SELECT {q} FROM bk."{table}"')
                if table == "settings":   # these stay as they were, set or not
                    left = db.execute("SELECT value FROM bk.settings WHERE key=?", (setup.LEAVE_SIM,)).fetchone()
                    db.execute(f"DELETE FROM main.settings WHERE key IN ({marks})", KEPT)
                    db.executemany("INSERT INTO main.settings (key, value) VALUES (?,?)", kept)
                    here = dict(kept).get(setup.LEAVE_SIM)
                    leaving = on_sim and here not in ('"done"', '"pending"') and (left == ('"done"',) or real)
                    if leaving:
                        db.execute("INSERT OR REPLACE INTO main.settings (key, value) VALUES (?,?)",
                                   (setup.LEAVE_SIM, '"pending"'))
                out[table] = db.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0]
        db.execute("DETACH DATABASE bk")
    finally:
        db.close()
    if leaving:
        say("The backup comes from an install off the simulator, and this one still runs it: the next start "
            "leaves the simulator (its invented trades are deleted, a copy of the database kept next to it) and runs on "
            "the real feeds, NinjaTrader and the futures feeds.")
    return out


class Backups:
    """The server's side: a backup a day, kept KEEP deep, and Back up now / Download in the app."""

    def __init__(self, market):
        self.m = market
        self.folder = backup_dir(market.s)
        self.error = ""
        self.warned = 0.0   # when Disk almost full was last sent

    def last(self) -> Path | None:
        return next(iter(files(self.folder)), None)

    def disk(self) -> dict:
        """The database's size and the free space where it lives (in Docker: Docker's disk, not Windows')."""
        p = self.m.store.path
        size = sum(f.stat().st_size for f in (p, p.with_name(p.name + "-wal")) if f.exists())
        du = shutil.disk_usage(p.parent)
        return {"db": size, "free": du.free, "low": du.free < LOW_DISK}

    async def check_disk(self):
        """Low on space: said once a day on every alert channel, before market data stops being saved."""
        d = self.disk()
        if d["low"] and time.time() - self.warned > 86400:
            self.warned = time.time()
            await self.m.notifier.send("Disk almost full", f"{d['free'] / 1e9:.1f} GB free where the chart database is "
                                       f"({d['db'] / 1e9:.1f} GB). Free some space, or the charts stop saving new trades.")

    def status(self) -> dict:
        fl = files(self.folder)
        up = getattr(self.m, "upkeep", None)
        return {"folder": self.folder.name, "keep": KEEP, "error": self.error, "download": self.m.auth.enabled,
                "retention": up.status() if up else None,
                "files": [{"name": p.name, "size": p.stat().st_size, "created": int(p.stat().st_mtime * 1000)} for p in fl[:KEEP + 5]]}

    async def now(self) -> Path:
        try:
            # a full disk is logged, not raised: the trades waiting for it are no part of a backup (MARKET), and the
            # backups folder can be on another disk (Docker: Documents on the PC), so Back up now still works
            self.m.flush_for_read()   # what is in memory reaches the database first
            p = await asyncio.to_thread(make, self.m.store.path, self.folder, "", self.m.s.feed)
            await asyncio.to_thread(prune, self.folder)
            self.error = ""
            log.info("backup written: %s", p.name)
            return p
        except (OSError, sqlite3.Error) as e:
            self.error = f"{type(e).__name__}: {e}"[:200]
            log.warning("backup failed: %s", self.error)
            raise

    async def run(self):
        while True:
            last = self.last()
            if last is None or time.time() - last.stat().st_mtime > EVERY_S:
                try:
                    await self.now()
                except (OSError, sqlite3.Error):
                    pass   # said in the log and in Connections; tried again in an hour
            try:
                await self.check_disk()
            except OSError as e:
                log.warning("disk check failed: %s", e)
            await asyncio.sleep(3600)


def main(argv=None):
    from .config import load_settings
    s = load_settings()
    ap = argparse.ArgumentParser(prog="python -m chartsrv.backup", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("now", help="write a backup now")
    sub.add_parser("list", help="list the backups")
    r = sub.add_parser("restore", help="put a backup's alerts, drawings and settings back (server stopped)")
    r.add_argument("file")
    r.add_argument("--yes", action="store_true", help="really replace them")
    f = sub.add_parser("full", help="copy the whole database, market data too")
    f.add_argument("file")
    a = ap.parse_args(argv)
    folder = backup_dir(s)
    if a.cmd == "now":
        p = make(s.db_path, folder, feed=s.feed)
        prune(folder)
        print(f"wrote {p}")
    elif a.cmd == "list":
        for p in files(folder):
            print(f"{p.name}  {p.stat().st_size / 1024:,.0f} KB")
        print(f"({folder})")
    elif a.cmd == "restore":
        p = Path(a.file)
        if not p.exists():
            p = folder / a.file
        if not a.yes:
            print(f"This replaces the alerts, drawings, watchlists, workspaces, settings and keys in {s.db_path} with "
                  f"{p.name}'s (market data stays). Stop the server first, then run again with --yes.")
            return 2
        counts = restore(s.db_path, p, on_sim=s.feed == "sim", say=print)
        print("restored: " + ", ".join(f"{t} {n}" for t, n in counts.items()))
        print("the replaced data was saved first as a -before-restore backup")
    elif a.cmd == "full":
        if os.name != "nt" and re.match(r"[A-Za-z]:\\|\\\\", a.file):   # in Docker: the PC's folders aren't here
            print(f"{a.file} is a path on the PC. Give a name alone: the copy goes in the backups folder ({folder}; "
                  "in Docker, the backups folder on the PC: BACKUP_DIR in .env).")
            return 2
        p = Path(a.file)
        if not p.is_absolute():   # in the backups folder: in Docker the working folder is the container's, rebuilt away
            p = folder / p
            p.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(p)
        src = connect(s.db_path, readonly=True)
        src.backup(dst)
        src.close()
        dst.close()
        print(f"wrote {p} ({p.stat().st_size / 1e6:,.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
