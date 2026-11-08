"""Serves the Tatanka TPO Profile (chartsrv.tpo) for /api/tpo.

The chart's N-minute bars are rebuilt from stored 1-minute bars the way the chart builds them (aligned to the
17:00 CT open, the last one cut at the 16:00 close; on an RTH chart only the regular hours, aligned to the RTH open
and cut at the RTH close, as NinjaTrader on an RTH trading-hours template) and stamped with their close time, as on
NinjaTrader. Each bar sees the daily ATR(10) of the completed sessions before its own, as the indicator's daily
series does (on an RTH chart, daily bars of the regular hours). Only closed bars are processed
(Calculate.OnBarClose). Results are recomputed per request: the chart asks again when one of its bars closes."""
import asyncio
import threading
import time
from bisect import bisect_left
from datetime import timedelta
from itertools import chain

from . import sessions, tpo
from .amt_runner import ct, epoch_ms
from .sessions import SessionClock, previous_session_start, session_start
from .store import iter_bars_1m
from .studies import RthClock

EXTRA_SESSIONS = 3      # loaded beyond LetterDays + PolylineDays, so the oldest drawn day is complete
DAILY_DAYS = 300        # calendar days of daily bars behind the first request, so the ATR(10) has settled
DAILY_TTL_S = 900       # reread daily bars this often, so a backfill shows up without a restart
DAILY_SETTLED = 200     # bar replay: the kept daily bars do if they start this many calendar days before its day


def session_end(start_ms: int) -> int:
    """16:00 CT on the trading date of the session opening at start_ms."""
    return epoch_ms((ct(start_ms) + timedelta(days=1)).replace(hour=16, minute=0))


def rth_day(session_open: int, rth: tuple[str, str]) -> tuple[int, int]:
    """The RTH open and close (epoch ms) on the trade date of the Globex session opening at session_open."""
    return RthClock(rth).window(sessions.to_ct(session_open + 16 * sessions.HOUR_MS))


def chart_bars(minute_bars, minutes: int, rth: tuple[str, str] | None = None) -> list[list]:
    """[open ms, close ms, high, low, session open ms, open, close] per N-minute chart bar, bucketed like
    TimeSeries. rth: the instrument's regular hours, for an RTH chart's bars (TimeSeries with rth_only): only the
    minutes inside them, counted from each day's RTH open (the session open) and the last bar cut at its close."""
    period = minutes * 60_000
    clock = SessionClock()
    hours = RthClock(rth) if rth else None
    day, window = None, (0, 0)
    out: list[list] = []
    cur = None
    s_end = (0, 0)
    for m in minute_bars:
        if hours:
            d = hours.day(m.t)
            if d is None:
                continue        # the night and the weekend: no RTH bar's
            if d != day:
                day, window = d, hours.window(sessions.to_ct(m.t))
            s = window[0]
        else:
            s = clock.locate(m.t)
        k = s + (m.t - s) // period * period
        if cur is not None and k == cur[0]:
            if m.h > cur[2]:
                cur[2] = m.h
            if m.l < cur[3]:
                cur[3] = m.l
            cur[6] = m.c
            continue
        if cur is not None and k < cur[0]:
            continue
        if s_end[0] != s:
            s_end = (s, window[1] if hours else session_end(s))
        cur = [k, min(k + period, s_end[1]), m.h, m.l, s, m.o, m.c]
        out.append(cur)
    return out


class BarBuilder:
    """chart_bars as the minutes come, a minute again as it forms: each minute's chart bar (its index, open, close time,
    open, high, low, close so far) as chart_bars has it, or None (outside an RTH chart's hours, or a minute already passed)."""

    def __init__(self, minutes: int, rth: tuple[str, str] | None = None):
        self.period = minutes * 60_000
        self.clock = SessionClock()
        self.hours = RthClock(rth) if rth else None
        self.day, self.window, self.s_end = None, (0, 0), (0, 0)
        self.index, self.key, self.close = -1, None, 0
        self.mins: dict[int, tuple] = {}   # the forming bar's minutes, by their time
        self.last_t: int | None = None

    def add(self, m) -> tuple | None:
        if self.hours:
            d = self.hours.day(m.t)
            if d is None:
                return None
            if d != self.day:
                self.day, self.window = d, self.hours.window(sessions.to_ct(m.t))
            s = self.window[0]
        else:
            s = self.clock.locate(m.t)
        if self.last_t is not None and m.t < self.last_t:
            return None                     # a late print for a minute already passed
        self.last_t = m.t
        k = s + (m.t - s) // self.period * self.period
        if k != self.key:
            if self.key is not None and k < self.key:
                return None
            if self.s_end[0] != s:
                self.s_end = (s, self.window[1] if self.hours else session_end(s))
            self.index += 1
            self.key, self.close, self.mins = k, min(k + self.period, self.s_end[1]), {}
        self.mins[m.t] = (m.o, m.h, m.l, m.c)
        first = self.mins[min(self.mins)]
        last = self.mins[max(self.mins)]
        return (self.index, k, self.close, first[0], max(v[1] for v in self.mins.values()),
                min(v[2] for v in self.mins.values()), last[3])


class Daily:
    """Completed daily bars (one per Globex session, keyed by its open; rth: one per regular session, of its regular
    hours only, keyed by its RTH open) and NinjaTrader's ATR(10) over them."""

    def __init__(self, calendar: str = "equity", rth: tuple[str, str] | None = None):
        self.calendar = calendar           # holiday group: a holiday's no-settlement session joins the next day
        self.rth = rth
        self.keys: list[int] = []
        self.hlc: list[list[float]] = []
        self.atr = tpo.NtAtr()
        self.through = 0       # sessions before this open are complete and added
        self.since = 0         # the minutes were read from here on (what is stored from then)
        self.loaded = time.monotonic()

    def add_minutes(self, minute_bars, through: int):
        clock = SessionClock()
        hours = RthClock(self.rth) if self.rth else None
        cur_key, cur = None, None
        keys: dict[int, int] = {}
        for m in minute_bars:
            if hours and hours.day(m.t) is None:
                continue
            s = clock.locate(m.t)
            s = keys.get(s) or keys.setdefault(s, self.key(s))
            if s != cur_key:
                if cur is not None:
                    self._add(cur_key, cur)
                cur_key, cur = s, [m.h, m.l, m.c]
            else:
                cur[0], cur[1], cur[2] = max(cur[0], m.h), min(cur[1], m.l), m.c
        if cur is not None:
            self._add(cur_key, cur)
        self.through = through

    def _add(self, key: int, hlc: list[float]):
        if self.keys and key <= self.keys[-1]:
            return
        self.keys.append(key)
        self.hlc.append(hlc)
        self.atr.add(*hlc)

    def key(self, session: int) -> int:
        """The daily bar of the Globex session opening at `session`: the first session of its trade date, or on an RTH
        chart that date's RTH open (a holiday halt's short session joins the next day's in both, as on the 1D chart)."""
        k = sessions.trade_key(session, self.calendar)
        return rth_day(k, self.rth)[0] if self.rth else k

    def before(self, session_open: int) -> int:
        """How many daily bars close before this session (a Globex open, or on an RTH chart an RTH open) opens."""
        return bisect_left(self.keys, self.key(session_start(session_open) if self.rth else session_open))


def run(s: dict, tick: float, minutes: int, minute_bars, daily: Daily, now_ms: int, record: list | None = None,
        rth: tuple[str, str] | None = None):
    """Feeds closed chart bars to a TpoProfile (rth: an RTH chart's, its sessions the regular ones). Returns the
    profile and each processed bar's open by close time."""
    eng = tpo.TpoProfile(s, tick)
    bars = chart_bars(minute_bars, minutes, rth)
    open_of: dict = {}
    prev_sess = None
    shown = 0
    for i, (k, close, h, l, sess, *_) in enumerate(bars):
        if i == len(bars) - 1 and close > now_ms:
            break       # the forming bar
        n = daily.before(sess)
        if record is not None:
            while shown < n:
                record.append(("D", *daily.hlc[shown]))
                shown += 1
            record.append(("B", ct(close), h, l, sess != prev_sess, ct(sess)))
        t = ct(close)
        eng.CurrentBars = [i, n - 1]
        eng.dailyAtr = daily.atr.values[n - 1] if n else None
        eng.Time0, eng.High0, eng.Low0 = t, h, l
        eng.IsFirstBarOfSession = sess != prev_sess
        eng.TemplateSessionOpen = ct(sess)
        prev_sess = sess
        eng.OnBarUpdate()
        open_of[t] = k
    return eng, open_of


def payload(eng: tpo.TpoProfile, open_of: dict, minutes: int) -> dict:
    """What the browser draws. x0 is the open of the profile's first bar plus the bar offset; letters sit one
    bar (col ms) apart from there, as NinjaTrader draws them one bar slot apart from the first bar."""
    col_s = minutes * 60
    col = col_s * 1000
    off = eng.XOffsetBars * col
    profiles = []
    if open_of:
        last_open = open_of[eng.Time0]
        for d in eng.Frame(col_s):
            p = d["profile"]
            x0 = open_of[p.TimeStart] + off
            ext = d["poc_end"]
            if ext is None:
                poc_end = x0 + d["poc_cols"] * col
            else:
                poc_end = open_of[ext] if ext in open_of else last_open + col
            letters = d["mode"] == "L"
            profiles.append({
                "mode": d["mode"], "live": d["live"], "x0": x0, "step": p.TickStep, "poc": p.PocPrice,
                "max": p.MaxCount, "cols": d["poc_cols"], "poc_end": poc_end,
                "va": list(d["va"]) if d["va"] else None,
                "rows": [[r.Price, r.Letters if letters else len(r.Letters)] for r in p.Rows],
            })
    return {"version": tpo.VERSION, "minutes": minutes, "col": col, "ready": True,
            "through": open_of[eng.Time0] if open_of else None, "step": eng.tickStep, "profiles": profiles}


class TpoService:
    def __init__(self, market):
        self.m = market
        self.lock = threading.Lock()
        self.daily: dict[str, dict[bool, Daily]] = {}   # symbol -> its daily bars: all hours' (False), the RTH's (True)
        self.replay_daily: dict[tuple[str, bool], Daily] = {}   # bar replay's, where the kept ones start too late

    async def get(self, sym: str, minutes: int, s: dict, rth: bool = False, until: int | None = None) -> dict:
        """rth: an RTH chart's profile (ignored for an instrument without regular hours). until: bar replay's clock,
        the profile as it stood then (the bars closed by then)."""
        split, live = self.m.recent_minutes(sym) if until is None else self.m.recent_minutes(sym, until)
        # replay: the clock is the replay's minute (split can be older: the newest minute held, the market quiet since)
        now = int(time.time() * 1000) if until is None else min(until // 60_000 * 60_000, int(time.time() * 1000))
        inst = self.m.instruments[sym]
        hours = inst.rth if rth else None
        return await asyncio.to_thread(self._compute, sym, inst.tick_size, minutes, s, split, live, now, hours, until is not None)

    def _compute(self, sym, tick, minutes, s, split, live, now, rth=None, replay=False):
        cur = session_start(now)
        start = cur
        for _ in range(s["LetterDays"] + s["PolylineDays"] + EXTRA_SESSIONS):
            start = previous_session_start(start)
        db = self.m.store.path
        daily = self.daily_for(sym, cur, bool(rth), replay)
        eng, open_of = run(s, tick, minutes, chain(iter_bars_1m(db, sym, start, split), live), daily, now, rth=rth)
        return payload(eng, open_of, minutes)

    def forget(self, sym: str):
        """The symbol's history changed (a gap fill): its daily bars are read again, bar replay's too."""
        with self.lock:
            self.daily.pop(sym, None)
            for k in [k for k in self.replay_daily if k[0] == sym]:
                del self.replay_daily[k]

    def daily_for(self, sym: str, cur: int, rth: bool = False, replay: bool = False) -> Daily:
        """The symbol's completed daily bars before the session opening at `cur` (blocking: call in a thread); rth:
        those of its regular hours, for an RTH chart. replay: `cur` is in the past (bar replay): the kept bars if they
        go back far enough before it for the ATR to have settled as it had then, else ones read for it (only the
        bars before it count: Daily.before)."""
        db = self.m.store.path
        calendar = self.m.instruments[sym].calendar
        hours = self.m.instruments[sym].rth if rth else None
        cut = sessions.trade_key(cur, calendar)     # after a holiday halt, today's trade date began a session earlier
        with self.lock:
            kept = self.daily.setdefault(sym, {})
            daily = kept.get(bool(hours))
            fresh = lambda d: d is not None and time.monotonic() - d.loaded <= DAILY_TTL_S   # noqa: E731
            if replay:   # read from far enough before it (what was stored from then: a short history is all there is)
                settled = cut - DAILY_SETTLED * 86_400_000
                if fresh(daily) and daily.since <= settled and daily.through >= cut:
                    return daily
                daily = self.replay_daily.get((sym, bool(hours)))
                if not fresh(daily) or daily.since > settled or daily.since < settled - 150 * 86_400_000:
                    daily = self.replay_daily[(sym, bool(hours))] = Daily(calendar, hours)
                    daily.since = cut - DAILY_DAYS * 86_400_000
                    daily.add_minutes(iter_bars_1m(db, sym, daily.since, cut), cut)
                elif daily.through < cut:
                    daily.add_minutes(iter_bars_1m(db, sym, daily.through, cut), cut)
                return daily
            if not fresh(daily):
                daily = kept[bool(hours)] = Daily(calendar, hours)
                daily.since = cur - DAILY_DAYS * 86_400_000
                daily.add_minutes(iter_bars_1m(db, sym, daily.since, cut), cut)
            elif daily.through < cut:
                daily.add_minutes(iter_bars_1m(db, sym, daily.through, cut), cut)
            return daily
