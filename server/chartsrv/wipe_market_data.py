"""Deletes the stored market data (trades, 1-minute bars, and the session profiles and settlements worked out from
them) and keeps everything else: alerts and their history, drawings and settings. For switching from the dev sim feed
to a real one: the sim's random-walk bars sit at real-looking prices and would mix with real data in charts and studies.

Stop the server first, so nothing writes while it runs. Docker, from the Tatanka Charts folder:
    docker compose stop chart
    docker compose run --rm chart python -m chartsrv.wipe_market_data          # shows what is stored
    docker compose run --rm chart python -m chartsrv.wipe_market_data --yes    # backs up, then deletes
    docker compose up -d
Without Docker, from its server folder: python -m chartsrv.wipe_market_data [--yes]

--yes first copies the whole database next to it (chart.db.bak-<time>), then deletes and reports the counts."""
import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import load_settings
from .settlement import SAVED
from .store import connect


def summary(db: sqlite3.Connection) -> list[tuple]:
    """(table, symbol, rows, first, last) for both market-data tables."""
    out = []
    for table, col in (("ticks", "ts"), ("bars_1m", "t")):
        for sym, n, a, b in db.execute(f"SELECT symbol, COUNT(*), MIN({col}), MAX({col}) FROM {table} GROUP BY symbol ORDER BY symbol"):
            out.append((table, sym, n, a, b))
    return out


def counts(db: sqlite3.Connection) -> dict:
    """Rows per table, for the tables this database has (one made by an older version lacks the newer ones)."""
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("ticks", "bars_1m", "alerts", "alert_events", "drawings", "settings") if t in have}


def wipe(path: Path, yes: bool, out=print) -> Path | None:
    db = connect(path)
    try:
        when = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ms else "-"
        for table, sym, n, a, b in summary(db):
            out(f"  {table:8} {sym:5} {n:>12,} rows  {when(a)} .. {when(b)}")
        before = counts(db)
        out(f"before: {before}")
        if not yes:
            out("Nothing deleted. Run again with --yes to back up the database and delete the trades and 1-minute bars.")
            return None
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        dst = sqlite3.connect(backup)
        try:
            with dst:
                db.backup(dst)
        except sqlite3.Error:   # no room for it (a full disk): nothing is deleted, and no part-copy left taking the rest
            dst.close()
            for f in (backup, backup.with_name(backup.name + "-journal")):
                f.unlink(missing_ok=True)
            raise
        dst.close()
        out(f"backup: {backup} ({backup.stat().st_size:,} bytes)")
        with db:
            db.execute("DELETE FROM ticks")
            db.execute("DELETE FROM bars_1m")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vp_cache'").fetchone():
                db.execute("DELETE FROM vp_cache")   # session profiles built from those trades
            got = db.execute("SELECT value FROM settings WHERE key=?", (SAVED,)).fetchone() if "settings" in before else None
            saved = json.loads(got[0]) if got else None
            if isinstance(saved, dict):   # the settlements worked out from them too (the feed's published ones stay)
                saved = {root: {k: v for k, v in x.items() if not (k == "prev" and v.get("src") == "trades")}
                         for root, x in saved.items() if x.get("src") != "trades"}
                db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(saved), SAVED))
        try:
            db.execute("VACUUM")
        except sqlite3.OperationalError as e:   # no room to shrink the file: the trades are gone all the same
            out(f"VACUUM failed ({e}): the file keeps its size, and its free space is reused")
        after = counts(db)
        out(f"after:  {after}")
        kept = [k for k in ("alerts", "alert_events", "drawings", "settings") if after.get(k) != before.get(k)]
        if kept:
            raise RuntimeError(f"unexpected change in {kept}")
        return backup
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="back up the database, then delete the trades and 1-minute bars")
    ap.add_argument("--db", type=Path, default=None)
    a = ap.parse_args()
    path = a.db or load_settings().db_path
    if not path.exists():
        raise SystemExit(f"no database at {path}")
    wipe(path, a.yes)


if __name__ == "__main__":
    main()
