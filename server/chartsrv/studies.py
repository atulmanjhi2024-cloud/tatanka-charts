"""Studies computed on the server from every tick: session and RTH VWAP with standard deviation, and
volume-at-price profiles per ETH (17:00 CT) and RTH session with POC and value area.

Profiles are kept in whole ticks. History comes from raw ticks; for each minute with no ticks (backfilled 1m
bars only) the bar's volume is spread evenly over its high-low range and the profile is marked approx."""
import math
import sqlite3
from collections import deque
from datetime import timedelta

from .config import Instrument
from .models import Tick
from .sessions import SessionClock, next_session_start, previous_session_start, session_start, to_ct
from .store import connect

VALUE_AREA = 0.70
SESSIONS_KEPT = 5


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class RthClock:
    """Maps a timestamp to its RTH day (CT date ordinal), or None outside RTH. Cached per minute."""

    def __init__(self, rth: tuple[str, str] | None):
        self.start, self.end = (_minutes(rth[0]), _minutes(rth[1])) if rth else (0, 0)
        self._min, self._day = None, None

    def day(self, ts: int) -> int | None:
        m = ts // 60_000
        if m != self._min:
            self._min = m
            dt = to_ct(ts)
            mod = dt.hour * 60 + dt.minute
            self._day = dt.toordinal() if dt.weekday() < 5 and self.start <= mod < self.end else None
        return self._day

    def window(self, day_start_ct) -> tuple[int, int]:
        """RTH start/end epoch ms for the CT date of the given aware datetime."""
        d = day_start_ct.replace(hour=0, minute=0, second=0, microsecond=0)
        s = d.replace(hour=self.start // 60, minute=self.start % 60)
        e = d.replace(hour=self.end // 60, minute=self.end % 60)
        return int(s.timestamp() * 1000), int(e.timestamp() * 1000)

    def windows(self, start: int, end: int) -> list[tuple[int, int]]:
        """The RTH hours inside [start, end) as epoch ms spans, one per weekday."""
        out, d = [], to_ct(start).replace(hour=0, minute=0, second=0, microsecond=0)
        while d.timestamp() * 1000 < end:
            if d.weekday() < 5:
                s, e = self.window(d)
                if s < end and e > start:
                    out.append((max(s, start), min(e, end)))
            d += timedelta(days=1)
        return out


class Vwap:
    """Volume-weighted average price and volume-weighted standard deviation, reset when the key changes.
    Sums are taken relative to the session's first price to keep the variance numerically clean."""
    __slots__ = ("key", "ref", "v", "pv", "p2v")

    def __init__(self):
        self.key, self.ref, self.v, self.pv, self.p2v = None, 0.0, 0.0, 0.0, 0.0

    def add(self, key, price: float, vol: float):
        if key != self.key:
            self.key, self.ref, self.v, self.pv, self.p2v = key, price, 0.0, 0.0, 0.0
        x = price - self.ref
        self.v += vol
        self.pv += x * vol
        self.p2v += x * x * vol

    def value(self) -> tuple[float, float] | None:
        if self.v <= 0:
            return None
        m = self.pv / self.v
        return self.ref + m, math.sqrt(max(self.p2v / self.v - m * m, 0.0))


def value_area(vols: list[float], pct: float = VALUE_AREA) -> tuple[int, int, int]:
    """Index of the POC and the low/high index of the value area: start at the POC and keep adding the
    heavier neighbouring row until pct of the volume is covered. Ties for POC go to the row nearest the
    middle of the range."""
    n = len(vols)
    if n == 0:
        return 0, 0, 0
    top = max(vols)
    mid = (n - 1) / 2
    poc = min((i for i, v in enumerate(vols) if v == top), key=lambda i: abs(i - mid))
    target, acc, lo, hi = sum(vols) * pct, vols[poc], poc, poc
    while acc < target and (lo > 0 or hi < n - 1):
        up = vols[hi + 1] if hi < n - 1 else -1
        dn = vols[lo - 1] if lo > 0 else -1
        if up >= dn:
            hi += 1
            acc += up
        else:
            lo -= 1
            acc += dn
    return poc, lo, hi


class Profile:
    """Volume at price for one session or range: {price_in_ticks: [volume, buy, sell]}."""

    def __init__(self, kind: str, key: int, start: int, end: int | None = None):
        self.kind, self.key, self.start, self.end = kind, key, start, end or start
        self.bins: dict[int, list[float]] = {}
        self.approx = False

    def add(self, pt: int, vol: float, side: int, ts: int):
        b = self.bins.get(pt)
        if b is None:
            b = self.bins[pt] = [0.0, 0.0, 0.0]
        b[0] += vol
        if side > 0:
            b[1] += vol
        elif side < 0:
            b[2] += vol
        if ts > self.end:
            self.end = ts

    def add_bar(self, lo_pt: int, hi_pt: int, vol: float, ts: int):
        n = hi_pt - lo_pt + 1
        for pt in range(lo_pt, hi_pt + 1):
            self.add(pt, vol / n, 0, ts)
        self.approx = True

    def to_dict(self, inst: Instrument) -> dict | None:
        if not self.bins:
            return None
        lo, hi = min(self.bins), max(self.bins)
        rows = [self.bins.get(pt, (0.0, 0.0, 0.0)) for pt in range(lo, hi + 1)]
        vols = [r[0] for r in rows]
        poc, val, vah = value_area(vols)
        px = lambda pt: round(pt * inst.tick_size, inst.decimals)
        return {"kind": self.kind, "key": self.key, "start": self.start, "end": self.end,
                "p0": px(lo), "tick": inst.tick_size, "v": [round(v) for v in vols],
                "d": [round(r[1] - r[2]) for r in rows], "poc": px(lo + poc), "val": px(lo + val),
                "vah": px(lo + vah), "total": round(sum(vols)), "approx": self.approx}


def _fill(db: sqlite3.Connection, prof: Profile, inst: Instrument, symbol: str, start: int, end: int,
          max_rowid: int | None, now: int | None = None):
    """Fill a profile from raw ticks in [start, end), and from the 1m bar of each minute with none (a gap filled as
    minutes), as the Session Volume Profile does. now: a book loading, whose trades after max_rowid come live; in
    the window still forming, with trades, bars only for the minutes before now's (that minute's trades come later)."""
    where = "FROM ticks WHERE symbol=? AND ts>=? AND ts<?"
    args = [symbol, start, end]
    if max_rowid is not None:
        where += " AND rowid<=?"
        args.append(max_rowid)
    rows = db.execute("SELECT price, SUM(size), SUM(CASE WHEN side > 0 THEN size ELSE 0 END), "
                      "SUM(CASE WHEN side < 0 THEN size ELSE 0 END), MAX(ts) " + where + " GROUP BY price", args).fetchall()
    tick = inst.tick_size
    for price, vol, buy, sell, last in rows:
        b = prof.bins.setdefault(round(price / tick), [0.0, 0.0, 0.0])
        b[0] += vol
        b[1] += buy
        b[2] += sell
        prof.end = max(prof.end, last)
    traded = {m for (m,) in db.execute("SELECT DISTINCT ts / 60000 " + where, args)} if rows else set()
    until = now // 60_000 * 60_000 if now is not None and end > now and rows else end
    for t, h, l, v in db.execute("SELECT t, h, l, v FROM bars_1m WHERE symbol=? AND t>=? AND t<? ORDER BY t",
                                 (symbol, start, until)):
        if v and t // 60_000 not in traded:
            prof.add_bar(round(l / tick), round(h / tick), v, t + 59_999)


def _sessions(start: int, end: int) -> list[tuple[int, int]]:
    """[start, end) cut at each 17:00 CT session open."""
    out = []
    while start < end:
        out.append((start, min(next_session_start(session_start(start)), end)))
        start = out[-1][1]
    return out


def range_profile(db_path, inst: Instrument, symbol: str, start: int, end: int, use_ticks: bool,
                  rth: bool = False) -> dict | None:
    """Composite or visible-range profile for [start, end); rth: its regular hours only. Runs in a worker thread."""
    db = connect(db_path, readonly=True)
    try:
        prof = Profile("range", start, start)
        clock = RthClock(inst.rth) if rth and inst.rth else None
        if use_ticks:   # trades, and the bars of the minutes with none (each session or RTH day read on its own)
            for s, e in clock.windows(start, end) if clock else _sessions(start, end):
                _fill(db, prof, inst, symbol, s, e, None)
        else:
            for t, h, l, v in db.execute("SELECT t, h, l, v FROM bars_1m WHERE symbol=? AND t>=? AND t<?",
                                         (symbol, start, end)):
                if v and (clock is None or clock.day(t) is not None):
                    prof.add_bar(round(l / inst.tick_size), round(h / inst.tick_size), v, t + 59_999)
        return prof.to_dict(inst)
    finally:
        db.close()


class ProfileBook:
    """The last SESSIONS_KEPT ETH and RTH session profiles for one symbol, kept current tick by tick."""

    def __init__(self, inst: Instrument):
        self.inst = inst
        self.eth: deque[Profile] = deque(maxlen=SESSIONS_KEPT)
        self.rth: deque[Profile] = deque(maxlen=SESSIONS_KEPT)
        self.clock = SessionClock()
        self.rth_clock = RthClock(inst.rth)
        self.dirty = False

    def add(self, tk: Tick):
        pt = round(tk.price / self.inst.tick_size)
        s = self.clock.locate(tk.ts)
        cur = self.eth[-1] if self.eth else None
        if cur is None or s > cur.key:
            cur = Profile("eth", s, s)
            self.eth.append(cur)
        if s == cur.key:
            cur.add(pt, tk.size, tk.side, tk.ts)
        if self.inst.rth:
            day = self.rth_clock.day(tk.ts)
            if day is not None:
                r = self.rth[-1] if self.rth else None
                if r is None or day > r.key:
                    r = Profile("rth", day, self.rth_clock.window(to_ct(tk.ts))[0])
                    self.rth.append(r)
                if day == r.key:
                    r.add(pt, tk.size, tk.side, tk.ts)
        self.dirty = True

    def load(self, db_path, symbol: str, now: int, max_rowid: int):
        """History for the last SESSIONS_KEPT sessions. Runs in a worker thread before the book goes live."""
        starts = [session_start(now)]
        while len(starts) < SESSIONS_KEPT:
            starts.append(previous_session_start(starts[-1]))
        starts.reverse()
        db = connect(db_path, readonly=True)
        try:
            for i, s in enumerate(starts):
                end = starts[i + 1] if i + 1 < len(starts) else now + 1
                p = Profile("eth", s, s)
                _fill(db, p, self.inst, symbol, s, end, max_rowid, now)
                if p.bins or i == len(starts) - 1:
                    self.eth.append(p)
                if self.inst.rth:
                    trade_date = to_ct(s) + timedelta(days=1)      # the 17:00 open belongs to the next day
                    rs, re_ = self.rth_clock.window(trade_date)
                    if rs < end:
                        r = Profile("rth", trade_date.toordinal(), rs)
                        _fill(db, r, self.inst, symbol, rs, min(re_, end), max_rowid, now)
                        if r.bins:
                            self.rth.append(r)
        finally:
            db.close()

    def snapshot(self) -> list[dict]:
        return [d for p in (*self.eth, *self.rth) if (d := p.to_dict(self.inst))]

    def current(self) -> list[dict]:
        return [d for q in (self.eth, self.rth) if q and (d := q[-1].to_dict(self.inst))]
