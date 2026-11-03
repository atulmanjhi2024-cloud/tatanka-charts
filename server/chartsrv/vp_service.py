"""Feeds the volume-profile ports (vp.py) this server's data the way NinjaTrader feeds the indicators theirs, and keeps
what they built between requests (GET /api/vp/{prof|aavp|pvp}).

- AAVP and PVP: 1-minute bars (NinjaTrader's AddDataSeries(Minute, 1)), stamped at their end as NinjaTrader stamps
  them, each with its session's open and trading day (the CME 17:00-16:00 CT session). A series is built once, in a
  worker, from the stored minutes it needs; after that each request feeds it the minutes stored since (in the worker
  too, when that is over a day) and the forming one. PVP's "until touched" ends come from the same minutes: a closed
  profile's POC, VAH or VAL is touched by the first minute after it whose range holds the price (NinjaTrader checks
  the chart's own bars; a minute is the chart bar holding it, so the line ends on the same bar).
- SVP (the "prof" study): every trade of each session window, up or down by the tick rule. A finished session is
  worked out once, in a worker, and kept in the vp_cache table; the current one grows from the trades stored since.
  Trades are stored only while a trade feed runs (the NinjaTrader add-on, or a Databento import); a minute of the
  session with no stored trades takes its 1-minute bar instead (gap-filled history is bars only): the bar's volume
  spread evenly over its ticks, up when it closed at or above its open, as the AAVP/PVP read bars. The answer says
  how many minutes came from bars ("approx").

The settings are the indicators' (nt_schema.json keys prof, aavp, pvp), saved for every device. A client that
already holds the finished profiles (PVP) or sessions (SVP) of the same build names them in `have` and gets only
what changed."""
import asyncio
import bisect
import hashlib
import json
import logging
import math
import secrets
import sqlite3
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import sessions, vp, workers
from .sessions import CT, day_open, next_session_start, session_start, to_ct
from .store import connect, iter_bars_1m

log = logging.getLogger(__name__)
MINUTE = 60_000
KINDS = ("prof", "aavp", "pvp")
MAX_BUILDERS = 24
SVP_LOOKBACK_DAYS = 45       # how far back sessions are looked for (SessionsToKeep of them, weekends skipped), at least
SVP_CATCH_UP = 20_000        # at most this many trades (every symbol's) since the last request are read on the event loop
MINUTES_CATCH_UP = 1_440     # AAVP/PVP: at most this many minutes since the last request are read on the event loop
ZONES = {"ExchangeTemplate": CT, "Central": CT, "Chart": CT,   # this app's chart time is Central, as the CME's
         "Eastern": ZoneInfo("America/New_York"), "UTC": ZoneInfo("UTC")}
SCHEMA_SQL = "CREATE TABLE IF NOT EXISTS vp_cache (key TEXT PRIMARY KEY, data TEXT NOT NULL) WITHOUT ROWID"


def trading_day(begin_ms: int) -> date:
    """The trade date of the session opening at begin_ms (a 17:00 CT open trades the next day)."""
    return (to_ct(begin_ms) + timedelta(hours=7)).date()


class MinuteSource:
    """A stored or live 1-minute bar as the indicators' BarsInProgress 1 sees it: (trading day, session open, index,
    low, high, volume, up, end time). The session is looked up only when a minute leaves the last one."""

    def __init__(self):
        self.begin = self.end = None
        self.td = None

    def args(self, b) -> tuple:
        if self.end is None or b.t >= self.end or b.t < self.begin:
            self.begin = session_start(b.t)
            self.end = next_session_start(self.begin)
            self.td = trading_day(self.begin)
        return (self.td, self.begin, b.t // MINUTE, float(b.l), float(b.h), float(b.v or 0), b.c >= b.o, b.t + MINUTE)


class MinuteState:
    """An AAVP or PVP series and how far it has been fed. Pickled to and from a worker for the first build."""

    def __init__(self, series):
        self.series = series
        self.src = MinuteSource()
        self.pend_t = None      # open time of the last minute fed: read again next time (it may have been forming)
        self.last = None        # PVP: the profile being fed
        self.watch: list[tuple] = []        # PVP: (price, profile, level) of closed profiles' untouched levels ...
        self.watch_p: list[float] = []      # ... and their prices, both sorted by price

    def feed(self, bars):
        ser = self.series
        if isinstance(ser, vp.AnchorSeries):
            for b in bars:
                ser.upsert(*self.src.args(b))
                self.pend_t = b.t
            return
        for b in bars:
            a = self.src.args(b)
            ser.add(*a)
            self.pend_t = b.t
            if ser.current is not self.last:
                if self.last is not None and self.last.closed:
                    self._watch(self.last)
                self.last = ser.current
                kept = {id(p) for p in ser.profiles}
                self.watch = [w for w in self.watch if id(w[1]) in kept]
                self.watch_p = [w[0] for w in self.watch]
            self._touch(a[3], a[4], a[7])

    def _watch(self, p: vp.MinuteProfile):
        s = p.snapshot()
        p.touch = {}
        if not s["rows"]:
            return
        for name in ("poc", "vah", "val"):
            i = bisect.bisect_right(self.watch_p, s[name])
            self.watch_p.insert(i, s[name])
            self.watch.insert(i, (s[name], p, name))

    def _touch(self, low: float, high: float, time: int):
        """Touches.Observe for every closed profile: low <= level <= high, after the profile's last minute."""
        i, j = bisect.bisect_left(self.watch_p, low), bisect.bisect_right(self.watch_p, high)
        if i == j:
            return
        keep = []
        for w in self.watch[i:j]:
            if time > w[1].last_chart:
                w[1].touch[w[2]] = time
            else:
                keep.append(w)
        self.watch[i:j] = keep
        self.watch_p[i:j] = [w[0] for w in keep]


def build_minutes(db_path, symbol: str, state: MinuteState, since: int, before: int) -> MinuteState:
    """Worker job: the series fed every stored minute in [since, before)."""
    state.feed(iter_bars_1m(db_path, symbol, since, before))
    return state


class SvpState:
    """The forming session of an SVP: its window, the trades seen (by rowid) and the tick rule's state."""

    def __init__(self, begin: int, end: int):
        self.begin, self.end = begin, end
        self.rowid = 0
        self.ticks: dict[int, list] = {}     # tick index -> [up, down]
        self.last_price = math.nan
        self.last_up = True
        self.last = None
        self.trades = 0      # trades taken
        self.approx = 0      # minutes taken from 1-minute bars

    def add_bar(self, t: int, o: float, h: float, low: float, c: float, v: float, tick: float):
        """A minute with no stored trades: its volume spread evenly over its ticks, up or down by its close."""
        if not v or v <= 0 or not all(math.isfinite(x) for x in (o, h, low, c)) or h < low:
            return
        lo_t, hi_t = vp.round_away(low / tick), vp.round_away(h / tick)
        each, side = v / (hi_t - lo_t + 1), 0 if c >= o else 1
        for k in range(lo_t, hi_t + 1):
            r = self.ticks.get(k)
            if r is None:
                r = self.ticks[k] = [0.0, 0.0]
            r[side] += each
        self.approx += 1
        if self.last is None or t + MINUTE > self.last:
            self.last = min(t + MINUTE, self.end)

    def add(self, price: float, size: float, ts: int, tick: float):
        """TatankaVpCore.Session.Add at one-tick rows (rows of more ticks are summed from these)."""
        if size <= 0 or not math.isfinite(price) or not math.isfinite(size):
            return
        if not math.isnan(self.last_price):
            if price > self.last_price:
                self.last_up = True
            elif price < self.last_price:
                self.last_up = False
        k = vp.round_away(price / tick)
        r = self.ticks.get(k)
        if r is None:
            r = self.ticks[k] = [0.0, 0.0]
        r[0 if self.last_up else 1] += size
        self.last_price = price
        self.last = ts if self.last is None else max(self.last, ts)
        self.trades += 1


def svp_job(db_path, symbol: str, windows: list, max_rowid: int, tick: float, now: int | None = None) -> list:
    """Worker job: each window's trades in time order through the tick rule, and the 1-minute bar of each minute
    with none. In the forming window (it ends after `now`), with trades coming in, bars only for minutes before this
    one (the trades of this minute and on come later). [SvpState]"""
    db = connect(db_path, readonly=True)
    out = []
    try:
        for b, e in windows:
            st = SvpState(b, e)
            # the minutes with trades first, then the trades streamed (a busy session has a million: not held at once)
            traded = {m for (m,) in db.execute("SELECT DISTINCT ts / ? FROM ticks WHERE symbol=? AND ts>=? AND ts<? "
                                               "AND rowid<=? AND size>0", (MINUTE, symbol, b, e, max_rowid))}
            until = now // MINUTE * MINUTE if (now is not None and e > now and traded) else e
            for t, o, h, low, c, v in db.execute("SELECT t, o, h, l, c, v FROM bars_1m WHERE symbol=? AND t>=? AND t<? ORDER BY t",
                                                 (symbol, b, until)):
                if t // MINUTE not in traded:
                    st.add_bar(t, o, h, low, c, v, tick)
            for price, size, ts in db.execute("SELECT price, size, ts FROM ticks WHERE symbol=? AND ts>=? AND ts<? "
                                              "AND rowid<=? ORDER BY ts, rowid", (symbol, b, e, max_rowid)):
                st.add(price, size, ts, tick)
            st.rowid = max_rowid
            out.append(st)
    finally:
        db.close()
    return out


def _r(x: float) -> float:
    return round(x, 4)


def minute_json(p: vp.MinuteProfile, rows: bool = True) -> dict | None:
    """rows=False: a finished profile the client already has, its touches only."""
    if not rows:
        return {"start": p.start_chart, "same": True, "touch": getattr(p, "touch", {})}
    s = p.snapshot()
    if not s["rows"]:
        return None
    return {"start": p.start_chart, "last": p.last_chart, "first": p.first_loaded, "closed": p.closed,
            "touch": getattr(p, "touch", {}),
            "window": [p.window[0].isoformat(), p.window[1].isoformat()],
            "rows": [[r[0], r[1], _r(r[2]), _r(r[3])] for r in s["rows"]],
            "poc": s["poc"], "vah": s["vah"], "val": s["val"], "poc_i": s["poc_index"], "vah_i": s["vah_index"],
            "val_i": s["val_index"], "low_edge": s["low_edge"], "high_edge": s["high_edge"], "max": _r(s["max_volume"]),
            "max_delta": _r(s["max_abs_delta"]), "tpr": s["ticks_per_row"], "capped": s["capped"]}


def svp_json(ticks: dict, begin: int, end: int, last, tick: float, tpr: int, percent: float, at_least: bool, approx: int = 0) -> dict | None:
    rows: dict[int, list] = {}
    for k, (up, down) in ticks.items():
        r = rows.setdefault(math.floor(k / tpr), [0.0, 0.0])
        r[0] += up
        r[1] += down
    if not rows:
        return None
    s = vp.svp_snapshot(rows, tick, tpr, percent, at_least)
    return {"start": begin, "end": end, "last": last, "rows": [[r[0], _r(r[1]), _r(r[2])] for r in s["rows"]],
            "row_size": s["row_size"], "poc": s["poc"], "vah": s["vah"], "val": s["val"], "poc_key": s["poc_key"],
            "vah_key": s["vah_key"], "val_key": s["val_key"], "low_edge": s["low_edge"], "high_edge": s["high_edge"],
            "max": _r(s["max_volume"]), "approx": approx}


class _Entry:
    def __init__(self, state, gen: int):
        self.state = state
        self.gen = gen          # the symbol's history it is built from (VpService.gen)
        self.lock = asyncio.Lock()
        self.ready = False


class VpService:
    def __init__(self, market):
        self.m = market
        self.entries: OrderedDict = OrderedDict()   # (kind, symbol, settings) -> _Entry
        self.gen: dict[str, int] = {}                # symbol -> times its history changed
        self.boot = secrets.token_hex(4)   # in every build key: a page's key from before a restart (an import) never matches
        market.store.db.execute(SCHEMA_SQL)
        with market.store.db:   # sessions worked out before minutes without trades took their bars; and ones partly
            market.store.db.execute("DELETE FROM vp_cache WHERE key LIKE 'svp|%'")   # from bars, as an import of
            market.store.db.execute(   # their trades (then a restart) brings the real ones
                "DELETE FROM vp_cache WHERE key LIKE 'svp2|%' AND json_extract(data, '$.approx') > 0")

    def settings(self, kind: str, given: dict | None = None) -> dict:
        """The saved settings, or `given` (checked, over the defaults) when a chart sends its own."""
        from .indicators import defaults
        return {**defaults(kind), **(self.m.ind.overrides.get(kind, {}) if given is None else given)}

    def forget(self, symbol: str):
        """The symbol's history changed (a gap fill): everything built from it goes. A request still building from
        the old history answers under its old build key and keeps nothing."""
        for k in [k for k in self.entries if k[1] == symbol]:
            del self.entries[k]
        self.gen[symbol] = self.gen.get(symbol, 0) + 1
        with self.m.store.db:
            self.m.store.db.execute("DELETE FROM vp_cache WHERE key LIKE ?", (f"svp2|{symbol}|%",))

    def _entry(self, key, make):
        e = self.entries.get(key)
        if e is None:
            e = self.entries[key] = _Entry(make(), self.gen.get(key[1], 0))
            while len(self.entries) > MAX_BUILDERS:
                self.entries.popitem(last=False)
        self.entries.move_to_end(key)
        return e

    def build_key(self, key: tuple, gen: int) -> str:
        """Names a build (its settings, the symbol's history and this server run): what a client's `have` refers to."""
        return hashlib.sha1(repr((key, gen, self.boot)).encode()).hexdigest()[:12]

    async def get(self, kind: str, symbol: str, key: str = "", have: set[int] = frozenset(), given: dict | None = None) -> dict:
        """key and have: the build the client holds profiles of, and the starts of its finished ones. given: the
        settings the chart uses, if not the saved ones."""
        s = self.settings(kind, given)
        tick = self.m.instruments[symbol].tick_size
        base = {"kind": kind, "symbol": symbol, "tick": tick}
        if kind == "prof":
            return {**base, **await self._svp(symbol, s, tick, key, have)}
        return {**base, **await self._minutes(kind, symbol, s, tick, key, have)}

    # --- AAVP / PVP ------------------------------------------------------------------------------------------------
    async def _minutes(self, kind: str, symbol: str, s: dict, tick: float, held: str, have) -> dict:
        rows = (s["RowsLayout"] == "NumberOfRows", s["RowSize"], float(s["ValueAreaPercent"]), s["ValueAreaMethod"] == "AtLeastTarget")
        if kind == "aavp":
            key, make = (kind, symbol, s["AnchorPeriod"], *rows), lambda: MinuteState(vp.AnchorSeries(s["AnchorPeriod"], tick, *rows))
        else:
            key = (kind, symbol, s["Period"], s["PeriodMultiplier"], s["ProfilesToKeep"], *rows)
            make = lambda: MinuteState(vp.PeriodicSeries(s["Period"], s["PeriodMultiplier"], s["ProfilesToKeep"], tick, *rows))   # noqa: E731
        e = self._entry(key, make)
        async with e.lock:
            m = self.m
            split, live = m.recent_minutes(symbol)
            st = e.state
            if not e.ready:
                t0 = time.perf_counter()
                since = self._first_minute(kind, symbol, s, split)
                st = e.state = await workers.run("chart", build_minutes, m.store.path, symbol, st, since, split)
                first = m.store.db.execute("SELECT 1 FROM bars_1m WHERE symbol=? AND t<? LIMIT 1", (symbol, since)).fetchone() is None
                ser = st.series
                profiles = [ser.current] if kind == "aavp" else ser.profiles
                if profiles and profiles[0] is not None:
                    profiles[0].first_loaded = first   # history before what was read: the oldest one is whole
                e.ready = True
                log.info("%s %s built in %.1fs", kind.upper(), symbol, time.perf_counter() - t0)
            elif st.pend_t is not None and split - st.pend_t > MINUTES_CATCH_UP * MINUTE:
                # nobody asked for a while (the page was hidden, or on other symbols): the minutes since, in a worker
                st = e.state = await workers.run("chart", build_minutes, m.store.path, symbol, st, st.pend_t, split)
            else:
                st.feed(iter_bars_1m(m.store.path, symbol, st.pend_t if st.pend_t is not None else split, split))
            st.feed(live)
            ser = st.series
            if kind == "aavp":
                return {"profile": minute_json(ser.current) if ser.current else None,
                        "first_td": ser.first_td.isoformat() if ser.first_td else None,
                        "last_td": ser.last_td.isoformat() if ser.last_td else None}
            bk = self.build_key(key, e.gen)
            have = have if held == bk else ()
            return {"key": bk, "profiles": [j for p in ser.profiles if (j := minute_json(p, not (p.closed and p.start_chart in have)))]}

    def _first_minute(self, kind: str, symbol: str, s: dict, split: int) -> int:
        """Where a new series starts reading: the open of its anchor period (AAVP), or of the oldest of the
        ProfilesToKeep periods with stored minutes (PVP), from the trading day of the latest stored minute."""
        db = self.m.store.db
        row = db.execute("SELECT MAX(t) FROM bars_1m WHERE symbol=? AND t<?", (symbol, split)).fetchone()
        td = trading_day(session_start(row[0] if row and row[0] is not None else split))
        if kind == "aavp":
            return day_open(vp.anchor_window(td, s["AnchorPeriod"])[0])
        w = vp.period_window(td, s["Period"], s["PeriodMultiplier"])
        for _ in range(s["ProfilesToKeep"] - 1):
            # the period of the latest minute stored before this one: a weekend, a holiday or a stretch with nothing
            # stored makes no profile, so it is not one of those kept
            row = db.execute("SELECT MAX(t) FROM bars_1m WHERE symbol=? AND t<?", (symbol, day_open(w[0]))).fetchone()
            if row[0] is None:
                break
            w = vp.period_window(trading_day(session_start(row[0])), s["Period"], s["PeriodMultiplier"])
        return day_open(w[0])

    # --- SVP --------------------------------------------------------------------------------------------------------
    def _windows(self, symbol: str, s: dict, now: int) -> list[tuple[int, int]]:
        """The newest SessionsToKeep session windows with trades or 1-minute bars, oldest first, as epoch ms [begin, end)."""
        tz = ZONES[s["SessionTimeZone"]]
        start, end = vp.parse_time(s["SessionStart"]), vp.parse_time(s["SessionEnd"])
        today = datetime.fromtimestamp(now / 1000, tz).date()
        out, db = [], self.m.store.db
        inst = self.m.instruments.get(symbol)
        group = inst.calendar if inst and tz is CT else None
        for back in range(max(SVP_LOOKBACK_DAYS, s["SessionsToKeep"] * 7 // 5 + 20)):   # 7 days for 5 sessions, and holidays
            d = today - timedelta(days=back)
            if group and sessions.holiday_halt(d, group):
                continue   # a holiday halt (MLK Day): no regular session that day
            day = datetime(d.year, d.month, d.day)
            b_local = day + start
            e_local = day + end if end > start else day + timedelta(days=1) + end
            b = int(b_local.replace(tzinfo=tz).timestamp() * 1000)
            e = int(e_local.replace(tzinfo=tz).timestamp() * 1000)
            if b > now or not (db.execute("SELECT 1 FROM ticks WHERE symbol=? AND ts>=? AND ts<? LIMIT 1", (symbol, b, e)).fetchone()
                                or db.execute("SELECT 1 FROM bars_1m WHERE symbol=? AND t>=? AND t<? AND v>0 LIMIT 1", (symbol, b, e)).fetchone()):
                continue
            out.append((b, e))
            if len(out) >= s["SessionsToKeep"]:
                break
        return out[::-1]

    async def _svp(self, symbol: str, s: dict, tick: float, held: str, have) -> dict:
        m = self.m
        if s["SessionTimeZone"] in ("ExchangeTemplate", "Central", "Chart") and symbol in m.instruments:   # its own hours
            move = sessions.rth_mover(m.instruments[symbol].rth)
            s = {**s, "SessionStart": sessions.move_clock(s["SessionStart"], move),
                 "SessionEnd": sessions.move_clock(s["SessionEnd"], move)}
        try:
            vp.parse_time(s["SessionStart"])
            vp.parse_time(s["SessionEnd"])
        except ValueError as ex:
            return {"error": str(ex), "sessions": []}
        key = ("prof", symbol, s["SessionStart"], s["SessionEnd"], s["SessionTimeZone"])
        e = self._entry(key, lambda: None)
        async with e.lock:
            m.flush_for_read()
            dv = m.store.data_version()   # another program's commits (an import) from here on: see below
            now, max_rowid = int(time.time() * 1000), m.store.max_tick_rowid()
            windows = self._windows(symbol, s, now)
            tpr, pct, at_least = s["TicksPerRow"], float(s["ValueAreaPercent"]), s["ValueAreaMethod"] == "AtLeastTarget"
            bk = self.build_key((*key, tpr, pct, at_least, s["SessionsToKeep"]), e.gen)
            have = have if held == bk else ()
            ckey = lambda b, en: f"svp2|{symbol}|{b}|{en}|{tick!r}"   # noqa: E731
            done: dict[tuple, dict] = {}
            missing = []
            for b, en in windows:
                if en > now or b in have:   # forming, or a finished one the client holds: not read again
                    continue
                row = m.store.db.execute("SELECT data FROM vp_cache WHERE key=?", (ckey(b, en),)).fetchone()
                if row:
                    d = json.loads(row[0])
                    done[(b, en)] = {"ticks": {int(k): v for k, v in d["ticks"]}, "last": d["last"], "approx": d.get("approx", 0)}
                else:
                    missing.append((b, en))
            cur_w = windows[-1] if windows and windows[-1][1] > now else None
            cur = e.state
            if cur is not None and (cur_w is None or (cur.begin, cur.end) != cur_w or not cur.trades
                                    or max_rowid - cur.rowid > SVP_CATCH_UP):
                # that session ended (it is worked out again from storage, in time order), or it has no trades yet:
                # its newest minutes are bars, read again each time; or nobody asked for a while: worked out again
                # in a worker, not caught up here through every symbol's trades since
                cur = e.state = None
            jobs = missing + ([cur_w] if cur_w and cur is None else [])
            if jobs:
                t0 = time.perf_counter()
                built = await workers.run("chart", svp_job, m.store.path, symbol, jobs, max_rowid, tick, now)
                for st in built:
                    if (st.begin, st.end) == cur_w:
                        cur = e.state = st
                    else:
                        done[(st.begin, st.end)] = {"ticks": st.ticks, "last": st.last, "approx": st.approx}
                # kept only if the history they were read from is still the stored one: a gap fill (forget()) or an
                # import (another program) that landed meanwhile has them worked out again by the next request
                if e.gen == self.gen.get(symbol, 0) and m.store.data_version() == dv:
                    rows = [(ckey(st.begin, st.end), json.dumps({"ticks": [[k, v] for k, v in st.ticks.items()], "last": st.last,
                                                                 "approx": st.approx})) for st in built if (st.begin, st.end) != cur_w]
                    try:
                        with m.store.db:
                            m.store.db.executemany("INSERT OR REPLACE INTO vp_cache VALUES (?, ?)", rows)
                    except sqlite3.Error as ex:   # a full disk: answered all the same, worked out again next time
                        log.warning("SVP %s: sessions not kept (%s)", symbol, ex)
                log.info("SVP %s: %d sessions worked out in %.1fs", symbol, len(jobs), time.perf_counter() - t0)
            if cur is not None and cur.rowid < max_rowid:   # the trades stored since, in the order they came
                for price, size, ts, sym in m.store.db.execute(
                        "SELECT price, size, ts, symbol FROM ticks WHERE rowid>? AND rowid<=? ORDER BY rowid", (cur.rowid, max_rowid)):
                    if sym == symbol and cur.begin <= ts < cur.end:
                        cur.add(price, size, ts, tick)
                cur.rowid = max_rowid
            out = []
            for b, en in windows:
                if (b, en) != cur_w and b in have:
                    out.append({"start": b, "same": True})
                    continue
                d = done.get((b, en)) if (b, en) != cur_w else ({"ticks": cur.ticks, "last": cur.last, "approx": cur.approx} if cur else None)
                if d and (j := svp_json(d["ticks"], b, en, d["last"], tick, tpr, pct, at_least, d["approx"])):
                    j["done"] = (b, en) != cur_w
                    out.append(j)
        return {"key": bk, "sessions": out}
