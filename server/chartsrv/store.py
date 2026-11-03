"""SQLite storage: raw ticks (kept for footprint/delta later), 1m bars, alerts and alert history.

One writer connection lives on the event loop. History loads open their own read connection in a
worker thread; WAL mode lets them read while the writer commits. Once, the start's clean-up of the prints stamped
over a weekend or a closed holiday writes on one of its own too, a slice at a time (drop_weekend_prints)."""
import json
import sqlite3
import time
from pathlib import Path

from . import sessions
from .models import Bar, Tick

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    symbol TEXT NOT NULL, ts INTEGER NOT NULL, price REAL NOT NULL, size INTEGER NOT NULL, side INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ticks_sym_ts ON ticks(symbol, ts);
CREATE TABLE IF NOT EXISTS bars_1m (
    symbol TEXT NOT NULL, t INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v INTEGER, bv INTEGER, sv INTEGER, n INTEGER,
    PRIMARY KEY (symbol, t)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, kind TEXT NOT NULL, params TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, repeat INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
    created INTEGER NOT NULL, last_fired INTEGER
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fills (
    account TEXT NOT NULL, exec_id TEXT NOT NULL, symbol TEXT NOT NULL, contract TEXT NOT NULL, ts INTEGER NOT NULL,
    price REAL NOT NULL, qty INTEGER NOT NULL, side INTEGER NOT NULL, point_value REAL NOT NULL, commission REAL NOT NULL,
    order_id TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (account, exec_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS ix_fills_key ON fills(account, contract, ts);
CREATE TABLE IF NOT EXISTS positions (
    account TEXT NOT NULL, contract TEXT NOT NULL, ts INTEGER NOT NULL, symbol TEXT NOT NULL, qty INTEGER NOT NULL,
    avg_price REAL NOT NULL, point_value REAL NOT NULL, PRIMARY KEY (account, contract, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS drawings (
    symbol TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (symbol, id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY, alert_id INTEGER, symbol TEXT, ts INTEGER, price REAL, message TEXT,
    delivered TEXT NOT NULL DEFAULT ''
);
"""

UPSERT_BAR = """
INSERT INTO bars_1m VALUES (?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(symbol, t) DO UPDATE SET o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c,
    v=excluded.v, bv=excluded.bv, sv=excluded.sv, n=excluded.n
"""


def _bar_row(symbol: str, b: Bar):
    return (symbol, b.t, b.o, b.h, b.l, b.c, b.v, b.bv, b.sv, b.n)


def connect(path: Path, readonly=False) -> sqlite3.Connection:
    if readonly:   # a URI: a '%', '?' or '#' in a folder name is escaped, or it would change or cut the path
        uri = str(path).replace("%", "%25").replace("?", "%3F").replace("#", "%23")
        return sqlite3.connect(f"file:{uri}?mode=ro", uri=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA journal_size_limit=67108864")   # the write-ahead log cut back to 64 MB after a checkpoint
    return db


DROP_BATCH = 5_000   # trades per slice of drop_weekend_prints: the server's writes wait for one at most


def drop_weekend_prints(path: Path, batch: int = DROP_BATCH):
    """Removes the ticks and 1-minute bars stamped over a weekend or a closed holiday (sessions.weekend: a feed's
    replay of its last trade, stored before the feeds left them out), a slice at a time, each its own transaction on
    a connection of its own. Each next() removes one, in a worker thread (Market._drop_weekend_prints), so the
    server's writer has its turn between them; it gives the rows removed so far, by symbol (those it removed any of)."""
    db = sqlite3.connect(path, check_same_thread=False)   # each slice on whichever worker thread is free
    db.execute("PRAGMA synchronous=NORMAL")               # as connect()'s
    db.execute("PRAGMA journal_size_limit=67108864")
    try:
        out = {}
        for sym, lo, hi in db.execute("SELECT symbol, MIN(t), MAX(t) FROM bars_1m GROUP BY symbol").fetchall():
            closed = sessions.closed_spans(lo, hi + 60_000)
            for a, b in sessions.weekends(lo, hi + 60_000):
                if (a, b) in closed:   # a closed holiday's: taken out only if it is a replay, every minute at one price
                    low, high = db.execute("SELECT MIN(l), MAX(h) FROM bars_1m WHERE symbol=? AND t>=? AND t<?",
                                           (sym, a, b)).fetchone()
                    if low is None or low != high:   # nothing there, or a real (short) session: kept
                        continue
                with db:
                    n = db.execute("DELETE FROM bars_1m WHERE symbol=? AND t>=? AND t<?", (sym, a, b)).rowcount
                while True:
                    with db:
                        k = db.execute("DELETE FROM ticks WHERE rowid IN (SELECT rowid FROM ticks WHERE symbol=? AND ts>=? "
                                       "AND ts<? LIMIT ?)", (sym, a, b, batch)).rowcount
                    if n + k:
                        out[sym] = out.get(sym, 0) + n + k
                    n = 0
                    yield out
                    if k < batch:
                        break
    finally:
        db.close()


SETTLEMENT_V = 50_000   # contracts: a one-price minute this big is NinjaTrader's settlement bar, never trading


def drop_settlement_bars(path: Path) -> dict[str, int]:
    """Removes the 1-minute bars a gap fill stored from NinjaTrader's history for the close (gapfill.minute leaves
    them out now): one price, SETTLEMENT_V contracts or more (the session's whole volume), in a minute the market is
    shut; with any trade of that size in it. On a connection of its own, in a worker thread; gives the bars removed
    by symbol."""
    db = sqlite3.connect(path, check_same_thread=False)
    try:
        out: dict[str, int] = {}
        rows = db.execute("SELECT symbol, t FROM bars_1m WHERE l = h AND v >= ?", (SETTLEMENT_V,)).fetchall()
        with db:
            for sym, t in rows:
                if sessions.is_open(t):
                    continue
                db.execute("DELETE FROM bars_1m WHERE symbol=? AND t=?", (sym, t))
                db.execute("DELETE FROM ticks WHERE symbol=? AND ts>=? AND ts<? AND size>=?", (sym, t, t + 60_000, SETTLEMENT_V))
                out[sym] = out.get(sym, 0) + 1
        return out
    finally:
        db.close()


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def data_version(self) -> int:
        """Changes when another connection (another program: an import, a tool) commits; never for this one's own."""
        return self.db.execute("PRAGMA data_version").fetchone()[0]

    # --- market data ---------------------------------------------------------------------------
    def write(self, ticks: list[Tick], bars: list[tuple[str, Bar]]) -> None:
        with self.db:
            if ticks:
                self.db.executemany("INSERT INTO ticks VALUES (?,?,?,?,?)",
                                    [(t.symbol, t.ts, t.price, t.size, t.side) for t in ticks])
            if bars:
                self.db.executemany(UPSERT_BAR, [_bar_row(s, b) for s, b in bars])

    def insert_bars_if_missing(self, symbol: str, bars: list[Bar]) -> int:
        with self.db:
            before = self.db.total_changes
            self.db.executemany("INSERT OR IGNORE INTO bars_1m VALUES (?,?,?,?,?,?,?,?,?,?)",
                                [_bar_row(symbol, b) for b in bars])
            return self.db.total_changes - before

    def insert_rows_if_missing(self, symbol: str, rows: list[tuple]) -> int:
        """Imported 1m bars as (t, o, h, l, c, v, trades) tuples, without an aggressor split; existing minutes are kept."""
        with self.db:
            before = self.db.total_changes
            self.db.executemany("INSERT OR IGNORE INTO bars_1m VALUES (?,?,?,?,?,?,?,0,0,?)",
                                [(symbol, *r) for r in rows])
            return self.db.total_changes - before

    def max_tick_rowid(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(rowid), 0) FROM ticks").fetchone()[0]

    def last_bar_1m(self, symbol: str) -> Bar | None:
        r = self.db.execute("SELECT t,o,h,l,c,v,bv,sv,n FROM bars_1m WHERE symbol=? ORDER BY t DESC LIMIT 1",
                            (symbol,)).fetchone()
        return Bar(*r) if r else None

    def last_tick(self, symbol: str) -> Tick | None:
        r = self.db.execute("SELECT symbol,ts,price,size,side FROM ticks WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                            (symbol,)).fetchone()
        return Tick(*r) if r else None

    # --- your NinjaTrader fills (journal.py) ----------------------------------------------------------------
    def add_fill(self, f: dict) -> bool:
        """Stores a fill once (the add-on sends today's again on each connect). True if it is new."""
        with self.db:
            before = self.db.total_changes
            self.db.execute("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (f["account"], f["exec_id"], f["symbol"], f["contract"], f["ts"], f["price"], f["qty"], f["side"],
                             f["point_value"], f["commission"], f["order_id"], f["name"]))
            return self.db.total_changes > before

    def fills(self, start_ms: int, end_ms: int, symbol: str | None = None, account: str | None = None,
              db: sqlite3.Connection | None = None) -> list[dict]:
        """db: another connection to read on (one of a worker thread's, its rows as sqlite3.Row), else the store's."""
        q, args = "SELECT * FROM fills WHERE ts>=? AND ts<?", [start_ms, end_ms]
        if symbol:
            q, args = q + " AND symbol=?", args + [symbol]
        if account:
            q, args = q + " AND account=?", args + [account]
        return [dict(r) for r in (db or self.db).execute(q + " ORDER BY ts", args).fetchall()]

    def from_flat(self, start_ms: int, end_ms: int, symbol: str | None = None, account: str | None = None,
                  within_ms: int = 7 * 86_400_000, db: sqlite3.Connection | None = None) -> tuple[list[dict], list[dict]]:
        """fills() and positions(), and before start_ms those of each account and contract that may be held then, back
        to where it was flat: a trade that began earlier and closes inside needs them all. That is NinjaTrader's last
        flat position before start_ms; never flat since the first position the add-on sent, where the stored fills put
        it flat up to a week before that one (not found: the trade starts at it). With no positions stored (an add-on
        before 13): where the stored fills were flat, up to a week back; one never flat that week is read as flat a
        week before, as a fill whose other side was never stored (a position opened before the add-on was installed,
        or while NinjaTrader was off) would otherwise leave it looking held for good."""
        db = db or self.db
        where, args = "", []
        if symbol:
            where, args = where + " AND symbol=?", args + [symbol]
        if account:
            where, args = where + " AND account=?", args + [account]
        since = {}   # (account, contract) -> (its fills from, its positions from)
        q = "SELECT account, contract, MAX(ts), qty FROM positions WHERE ts<?" + where   # the last of each before it
        for acct, contract, last, pos in db.execute(q + " GROUP BY account, contract", [start_ms] + args).fetchall():
            flat = last if not pos else db.execute("SELECT MAX(ts) FROM positions WHERE account=? AND contract=? AND ts<? "
                                                   "AND qty=0", (acct, contract, last)).fetchone()[0]
            if flat is not None:
                since[(acct, contract)] = (flat + 1, flat + 1)
            else:   # never flat since the first position the add-on sent: back from it through the stored fills
                first, pos = db.execute("SELECT ts, qty FROM positions WHERE account=? AND contract=? ORDER BY ts LIMIT 1",
                                        (acct, contract)).fetchone()
                at = self._flat_at(acct, contract, first + 1, pos, first - within_ms, db)
                since[(acct, contract)] = (first + 1, first) if at is None else (at, at)
        q = "SELECT account, contract, SUM(side * qty) FROM fills WHERE ts<?" + where
        for acct, contract, pos in db.execute(q + " GROUP BY account, contract HAVING SUM(side * qty) != 0",
                                              [start_ms] + args).fetchall():
            if (acct, contract) not in since:   # no positions before start_ms: the stored fills alone
                at = self._flat_at(acct, contract, start_ms, pos, start_ms - within_ms, db)
                since[(acct, contract)] = (start_ms - within_ms,) * 2 if at is None else (at, at)
        fills = self.fills(start_ms, end_ms, symbol, account, db)
        positions = self.positions(start_ms, end_ms, symbol, account, db)
        for (acct, contract), (f_from, p_from) in since.items():   # each one's own, from where it was flat
            fills += [dict(r) for r in db.execute("SELECT * FROM fills WHERE account=? AND contract=? AND ts>=? AND ts<?",
                                                  (acct, contract, f_from, start_ms))]
            positions += [dict(r) for r in db.execute("SELECT * FROM positions WHERE account=? AND contract=? AND ts>=? "
                                                      "AND ts<?", (acct, contract, p_from, start_ms))]
        return fills, positions

    @staticmethod
    def _flat_at(acct: str, contract: str, before_ms: int, pos: int, since_ms: int, db: sqlite3.Connection) -> int | None:
        """Back from position `pos` just before before_ms, a millisecond at a time (fills in one can close and open
        again), to where the stored fills put it at zero: the first fill's time from there, or None (none since
        since_ms)."""
        for ts, n in db.execute("SELECT ts, SUM(side * qty) FROM fills WHERE account=? AND contract=? AND ts>=? AND ts<? "
                                "GROUP BY ts ORDER BY ts DESC", (acct, contract, since_ms, before_ms)):
            pos -= n
            if pos == 0:
                return ts
        return None

    # NinjaTrader's own positions (add-on 13): known points for the journal, where a fill the app never got is put right
    def held(self, account: str, contract: str, ts: int) -> int:
        """The position the stored positions and fills put an account and contract at, at ts (+ long, - short)."""
        r = self.db.execute("SELECT ts, qty FROM positions WHERE account=? AND contract=? AND ts<=? ORDER BY ts DESC "
                            "LIMIT 1", (account, contract, ts)).fetchone()
        after, pos = (r[0], r[1]) if r else (None, 0)
        q, args = "SELECT COALESCE(SUM(side * qty), 0) FROM fills WHERE account=? AND contract=? AND ts<=?", \
            [account, contract, ts]
        if after is not None:
            q, args = q + " AND ts>?", args + [after]
        return pos + self.db.execute(q, args).fetchone()[0]

    def add_position(self, p: dict) -> bool:
        """Stores one (the add-on sends the open ones again on each connect: kept once). One at the same millisecond as
        a stored one replaces it: the add-on stamps a P line no earlier than its contract's last line or the account's
        last connect, so its next one clamped to that millisecond puts right one that came out late (after a fill it
        did not count). True if the stored data had that account and contract at another position then: a fill the app
        never got, put right from there."""
        with self.db:
            had = self.held(p["account"], p["contract"], p["ts"])
            self.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?)",
                            (p["account"], p["contract"], p["ts"], p["symbol"], p["qty"], p["avg_price"], p["point_value"]))
            return had != p["qty"]

    def flat_others(self, account: str, ts: int) -> list[str]:
        """The add-on's connect: the account's contracts with no position stored at ts (the open ones it just sent)
        or after it are flat then. A flat position is stored for each that the stored data has held; those are
        returned."""
        out = []
        with self.db:
            for contract, symbol in self.db.execute(
                    "SELECT contract, MAX(symbol) FROM (SELECT contract, symbol FROM fills WHERE account=? UNION "
                    "SELECT contract, symbol FROM positions WHERE account=?) GROUP BY contract", (account, account)
            ).fetchall():
                if not self.db.execute("SELECT 1 FROM positions WHERE account=? AND contract=? AND ts>=? LIMIT 1",
                                       (account, contract, ts)).fetchone() and self.held(account, contract, ts):
                    self.db.execute("INSERT INTO positions VALUES (?,?,?,?,0,0,0)", (account, contract, ts, symbol))
                    out.append(contract)
        return out

    def positions(self, start_ms: int, end_ms: int, symbol: str | None = None, account: str | None = None,
                  db: sqlite3.Connection | None = None) -> list[dict]:
        """NinjaTrader's positions in [start_ms, end_ms), oldest first (db: as fills())."""
        q, args = "SELECT * FROM positions WHERE ts>=? AND ts<?", [start_ms, end_ms]
        if symbol:
            q, args = q + " AND symbol=?", args + [symbol]
        if account:
            q, args = q + " AND account=?", args + [account]
        return [dict(r) for r in (db or self.db).execute(q + " ORDER BY ts", args).fetchall()]

    def fill_accounts(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT account FROM fills UNION SELECT account FROM positions "
                                              "ORDER BY account").fetchall()]

    def counts(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {"ticks": q("SELECT COUNT(*) FROM ticks"), "bars_1m": q("SELECT COUNT(*) FROM bars_1m"),
                "alerts": q("SELECT COUNT(*) FROM alerts"), "alert_events": q("SELECT COUNT(*) FROM alert_events")}

    # --- settings (JSON values) -------------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_setting(self, key: str, value) -> None:
        with self.db:
            self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)))

    # --- alerts --------------------------------------------------------------------------------
    def list_alerts(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM alerts ORDER BY id").fetchall()
        return [dict(r, params=json.loads(r["params"]), enabled=bool(r["enabled"]), repeat=bool(r["repeat"]))
                for r in rows]

    def add_alert(self, symbol, kind, params: dict, repeat: bool, note: str) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO alerts (symbol, kind, params, enabled, repeat, note, created) VALUES (?,?,?,1,?,?,?)",
                (symbol, kind, json.dumps(params), int(repeat), note, int(time.time() * 1000)))
            return cur.lastrowid

    def update_alert(self, alert_id: int, **fields) -> bool:
        allowed = {"enabled", "repeat", "note", "last_fired", "params"}
        sets = {k: (json.dumps(v) if k == "params" else int(v) if isinstance(v, bool) else v)
                for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        with self.db:
            cur = self.db.execute(f"UPDATE alerts SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                                  (*sets.values(), alert_id))
            return cur.rowcount > 0

    def delete_alert(self, alert_id: int) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM alerts WHERE id=?", (alert_id,)).rowcount > 0

    def add_event(self, ev: dict) -> int:
        with self.db:
            return self.db.execute(
                "INSERT INTO alert_events (alert_id, symbol, ts, price, message) VALUES (?,?,?,?,?)",
                (ev.get("alert_id"), ev["symbol"], ev["ts"], ev.get("price"), ev["message"])).lastrowid

    def set_event_delivered(self, event_id: int, delivered: str) -> None:
        with self.db:
            self.db.execute("UPDATE alert_events SET delivered=? WHERE id=?", (delivered, event_id))

    def list_drawings(self, symbol: str) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM drawings WHERE symbol=?", (symbol,))]

    def count_drawings(self, symbol: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM drawings WHERE symbol=?", (symbol,)).fetchone()[0]

    def put_drawing(self, symbol: str, d: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO drawings VALUES (?,?,?) ON CONFLICT(symbol, id) DO UPDATE SET data=excluded.data",
                            (symbol, d["id"], json.dumps(d)))

    def delete_drawing(self, symbol: str, drawing_id: str) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM drawings WHERE symbol=? AND id=?", (symbol, drawing_id)).rowcount > 0

    def list_events(self, limit=50) -> list[dict]:
        rows = self.db.execute("SELECT * FROM alert_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# --- read-only history loaders, run in a worker thread ---------------------------------------------
def iter_ticks(path: Path, symbol: str, since_ms: int, max_rowid: int, chunk=50_000):
    db = connect(path, readonly=True)
    try:
        cur = db.execute("SELECT symbol, ts, price, size, side FROM ticks "
                         "WHERE symbol=? AND ts>=? AND rowid<=? ORDER BY ts, rowid", (symbol, since_ms, max_rowid))
        while rows := cur.fetchmany(chunk):
            for r in rows:
                yield Tick(*r)
    finally:
        db.close()


def iter_ticks_range(path: Path, symbol: str, start_ms: int, end_ms: int, max_rowid: int, chunk=50_000):
    """Stored trades in [start_ms, end_ms) written up to max_rowid, in time order."""
    db = connect(path, readonly=True)
    try:
        cur = db.execute("SELECT symbol, ts, price, size, side FROM ticks WHERE symbol=? AND ts>=? AND ts<? AND rowid<=? "
                         "ORDER BY ts, rowid", (symbol, start_ms, end_ms, max_rowid))
        while rows := cur.fetchmany(chunk):
            for r in rows:
                yield Tick(*r)
    finally:
        db.close()


def bar_1m_start(path: Path, symbol: str, before_ms: int, n: int) -> int:
    """Open time of the n-th stored 1m bar back from before_ms (0 if fewer are stored), so history windows
    are sized in bars, not clock time, and weekends or breaks do not shrink them."""
    db = connect(path, readonly=True)
    try:
        r = db.execute("SELECT t FROM bars_1m WHERE symbol=? AND t<? ORDER BY t DESC LIMIT 1 OFFSET ?",
                       (symbol, before_ms, max(n - 1, 0))).fetchone()
        return r[0] if r else 0
    finally:
        db.close()


def iter_bars_1m(path: Path, symbol: str, since_ms: int, before_ms: int, db: sqlite3.Connection | None = None):
    """db: a connection already open on path to read on (left open), else one of its own."""
    own = db is None
    db = connect(path, readonly=True) if own else db
    try:
        cur = db.execute("SELECT t,o,h,l,c,v,bv,sv,n FROM bars_1m WHERE symbol=? AND t>=? AND t<? ORDER BY t",
                         (symbol, since_ms, before_ms))
        while rows := cur.fetchmany(10_000):
            for r in rows:
                yield Bar(*r)
    finally:
        if own:
            db.close()


def iter_closes_1m(path: Path, symbol: str, since_ms: int, before_ms: int):
    """(open ms, close) of each stored 1m bar: what daily closes need, read lean."""
    db = connect(path, readonly=True)
    try:
        cur = db.execute("SELECT t,c FROM bars_1m WHERE symbol=? AND t>=? AND t<? ORDER BY t", (symbol, since_ms, before_ms))
        while rows := cur.fetchmany(50_000):
            yield from rows
    finally:
        db.close()


def iter_closes_1s(path: Path, symbol: str, since_ms: int, before_ms: int):
    """(second ms, close) of each second in [since_ms, before_ms) with a stored trade, grouped by SQLite: what seconds
    bars' closes need. The close is the second's last trade (of several in its last millisecond, any one)."""
    db = connect(path, readonly=True)
    try:
        cur = db.execute("SELECT ts / 1000 * 1000 AS s, price, MAX(ts) FROM ticks WHERE symbol=? AND ts>=? AND ts<? "
                         "GROUP BY s ORDER BY s", (symbol, since_ms, before_ms))
        while rows := cur.fetchmany(50_000):
            for s, price, _ in rows:
                yield s, price
    finally:
        db.close()


def big_trades(path: Path, symbol: str, start_ms: int, end_ms: int, min_size: int, limit: int = 5000,
               newest_first: bool = False) -> list[list]:
    """Stored trades in [start_ms, end_ms) as the tape and the big-trade bubbles show them: fills at the same
    millisecond on the same side (one order sweeping the book) count as one trade of their total size, at their
    volume-weighted price. [[ts, price, size, side, fills], ...] of at least min_size, oldest first unless
    newest_first (then the newest `limit`)."""
    db = connect(path, readonly=True)
    try:
        rows = db.execute(
            "SELECT ts, SUM(price * size) / SUM(size), SUM(size), side, COUNT(*) FROM ticks "
            "WHERE symbol=? AND ts>=? AND ts<? GROUP BY ts, side HAVING SUM(size)>=? "
            f"ORDER BY ts {'DESC' if newest_first else 'ASC'}, MIN(rowid) {'DESC' if newest_first else 'ASC'} LIMIT ?",
            (symbol, start_ms, end_ms, min_size, limit)).fetchall()
        return [list(r) for r in rows]
    finally:
        db.close()


def sweeps(ticks) -> list[list]:
    """The same grouping for live ticks in time order: [[ts, price, size, side, fills], ...], each (ts, side) once,
    where its first fill was."""
    out, at = [], {}
    for t in ticks:
        g = at.get((t.ts, t.side))
        if g is not None:
            g[1] = (g[1] * g[2] + t.price * t.size) / (g[2] + t.size)
            g[2] += t.size
            g[4] += 1
        else:
            g = at[(t.ts, t.side)] = [t.ts, t.price, t.size, t.side, 1]
            out.append(g)
    return out
