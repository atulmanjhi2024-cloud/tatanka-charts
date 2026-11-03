"""Keeping the database lean, once a day while CME is shut (the 16:00-17:00 CT break, or a weekend): trades older
than the retention (Connections > Backups; 5 years unless changed) deleted a slice at a time, the fired-alert history
cut to the newest EVENTS_KEPT, the write-ahead log folded back in and trimmed, and SQLite's planner statistics
refreshed. 1-minute bars are always kept, so candle charts reach as far back as ever; tick, footprint and tape detail
goes for the older days. The file doesn't shrink: the freed space is reused for new trades, so it stops growing. All
on the server's own connection, so charts get their turn between slices and the database watcher (app.watch_db)
doesn't take it for an import."""
import asyncio
import logging
import sqlite3
import time

from . import sessions

log = logging.getLogger(__name__)
SETTING = "trade_retention_years"
DEFAULT_YEARS = 5
CHOICES = (1, 2, 3, 5, 10, 0)   # years; 0 keeps every trade
BATCH = 20_000                  # trades per slice
BUDGET_S = 600                  # at most this long a run; a large first clear-out goes on at the next check
EVERY_S = 300
YEAR_MS = int(365.25 * 86_400_000)
EVENTS_KEPT = 10_000            # fired alerts kept: the app shows the newest 500 at most, and every backup has them
# Every symbol in ticks without reading the whole index: the next one after each, through the index (a skip scan).
SYMBOLS = """WITH RECURSIVE s(sym) AS (SELECT MIN(symbol) FROM ticks
             UNION ALL SELECT (SELECT MIN(symbol) FROM ticks WHERE symbol > s.sym) FROM s WHERE s.sym IS NOT NULL)
             SELECT sym FROM s WHERE sym IS NOT NULL"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Upkeep:
    def __init__(self, market):
        self.m = market
        self.done_day = None   # the CT date of the last complete run
        self.last = {"at": None, "deleted": 0, "more": False, "error": ""}

    def years(self) -> int:
        v = self.m.store.get_setting(SETTING, DEFAULT_YEARS)
        return v if v in CHOICES else DEFAULT_YEARS

    def set_years(self, years) -> int:
        if isinstance(years, bool) or years not in CHOICES:
            raise ValueError(f"keep trades: one of {', '.join(str(c) for c in CHOICES if c)} years, or 0 for always")
        self.m.store.set_setting(SETTING, years)
        return years

    def status(self) -> dict:
        return {"years": self.years(), "choices": list(CHOICES), **self.last}

    async def run(self):
        while True:
            await asyncio.sleep(EVERY_S)
            now = now_ms()
            day = sessions.to_ct(now).date()
            if self.done_day == day or sessions.is_open(now):
                continue
            await self.once(now)
            if not self.last["more"]:
                self.done_day = day

    async def once(self, now: int | None = None):
        now = now or now_ms()
        db = self.m.store.db
        try:
            self.m.flush()
            deleted, more = await self.prune(now)
            with db:   # the fired-alert history: the newest EVENTS_KEPT
                db.execute("DELETE FROM alert_events WHERE id <= "
                           "(SELECT id FROM alert_events ORDER BY id DESC LIMIT 1 OFFSET ?)", (EVENTS_KEPT,))
            # On the server's loop: it never waits for a reader still in the log (a chart's history load); with one
            # in, the log is folded back in as far as it can be and trimmed at a later run.
            busy = db.execute("PRAGMA busy_timeout").fetchone()[0]
            db.execute("PRAGMA busy_timeout=0")
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            finally:
                db.execute(f"PRAGMA busy_timeout={int(busy)}")
            db.execute("PRAGMA optimize")
            self.last = {"at": now, "deleted": deleted, "more": more, "error": ""}
            if deleted:
                log.info("upkeep: %s trades older than %d years removed%s", f"{deleted:,}", self.years(),
                         " (more at the next run)" if more else "")
        except sqlite3.Error as e:
            self.last = {**self.last, "at": now, "error": f"{type(e).__name__}: {e}"[:200]}
            log.warning("upkeep failed: %s", self.last["error"])

    async def prune(self, now: int) -> tuple[int, bool]:
        """(trades deleted, more left for the next run)."""
        years = self.years()
        if not years:
            return 0, False
        cutoff, db = now - years * YEAR_MS, self.m.store.db
        deleted, t0 = 0, time.monotonic()
        for (sym,) in db.execute(SYMBOLS).fetchall():
            while True:
                if time.monotonic() - t0 > BUDGET_S:
                    return deleted, True
                with db:
                    n = db.execute("DELETE FROM ticks WHERE rowid IN (SELECT rowid FROM ticks WHERE symbol=? AND ts<? LIMIT ?)",
                                   (sym, cutoff, BATCH)).rowcount
                deleted += n
                if n < BATCH:
                    break
                await asyncio.sleep(0)   # the charts' turn between slices
        return deleted, False
