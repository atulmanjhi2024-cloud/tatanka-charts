"""Tatanka Daily SMA x4, ported from the NinjaTrader 8 source (tests/dsma_csharp/TatankaDailySMAx4.cs, unchanged).

The indicator's OnBarUpdate runs in the browser (web/studies.js, dailySmaValues) on the chart's bars, the developing
day's close moving with the live price. The server supplies the daily series it reads (AddDataSeries Day 1, 1000
bars): the last 1000 daily bars, built from the stored 1-minute bars as CME's daily bars are (one per trading day,
17:00-16:00 CT; a session that runs into a holiday halt has no settlement and joins the next day's), or one per
regular session on an RTH-only chart (the halt's joining the next day's there too, as on the RTH 1D chart). Each bar
is stamped with its close, as NinjaTrader stamps it: 16:00 CT on its trade date, the RTH close, or a holiday's early
close."""
import threading
from bisect import bisect_right
from datetime import timedelta
from math import ceil

from . import sessions
from .amt_runner import ct, epoch_ms
from .sessions import SessionClock
from .studies import RthClock
from .tpo_service import rth_day

MAX_DAYS = 1000      # AddDataSeries(..., 1000, ...): the daily bars NinjaTrader loads
DAY_MS = 86_400_000


def lookback_ms(days: int) -> int:
    """Calendar time holding `days` trading days, with room for about ten holidays a year."""
    return (ceil(days * 1.5) + 20) * DAY_MS


def _session_end(s: int) -> int:
    """16:00 CT on the trade date of the Globex session opening at s."""
    return epoch_ms((ct(s) + timedelta(days=1)).replace(hour=16, minute=0))


class DailyBars:
    """One symbol's daily bars from its 1-minute bars, as parallel lists: open (the trade date's first 17:00 open,
    or the RTH open), close time and close. Built once from `lo`, then extended with the minutes stored since."""

    def __init__(self, calendar: str, rth: tuple[str, str] | None):
        self.calendar, self.rth = calendar, rth
        self.opens: list[int] = []
        self.ends: list[int] = []
        self.closes: list[float] = []
        self.lo = self.through = 0
        self.lock = threading.Lock()

    def add(self, minutes, opens: list[int], ends: list[int], closes: list[float]):
        """Adds (open ms, close) minutes, in time order, to the lists given."""
        clock, rth, seen = SessionClock(), RthClock(self.rth) if self.rth else None, {}   # per call: they cache
        for t, c in minutes:
            if rth and rth.day(t) is None:
                continue
            s = clock.locate(t)
            k = seen.get(s)
            if k is None:
                # keyed by its trade date's first session, ending at the close of the trade date it counts toward: a
                # holiday halt's session is in the next day's bar, as on the 1D chart, and forms as part of it
                key, to = sessions.trade_key(s, self.calendar), sessions.counts_toward(s, self.calendar)
                if rth:   # an RTH chart's: those dates' regular hours, as its 1D bars
                    k = (rth_day(key, self.rth)[0], sessions.first_closure(*rth_day(to, self.rth), self.calendar))
                else:
                    k = (key, sessions.first_closure(to, _session_end(to), self.calendar))
                seen[s] = k
            if opens and k[0] == opens[-1]:
                closes[-1] = c
                ends[-1] = max(ends[-1], k[1])   # a holiday-halted session's bar closes with the next day's
            elif not opens or k[0] > opens[-1]:
                opens.append(k[0])
                ends.append(k[1])
                closes.append(c)


_cache: dict[tuple, DailyBars] = {}
_cache_lock = threading.Lock()


def forget(symbol: str):
    """Stored history changed (a gap fill): build again."""
    with _cache_lock:
        for k in [k for k in _cache if k[1] == symbol]:
            del _cache[k]


def compute(db_path, symbol: str, calendar: str, rth: tuple[str, str] | None, start: int, length: int,
            split: int, live: list, now: int, read) -> dict:
    """The daily bars a chart starting at `start` needs for SMAs of up to `length` days: from `length` bars before
    the first day closing after `start` (the first chart bar shows the day before's SMA), or the 1000-bar limit, to
    the last, forming one. read(db, symbol, since, before) gives the stored (open ms, close) minutes; `live` the
    minutes from `split` on, not all stored yet."""
    since = max(start - lookback_ms(length), now - lookback_ms(MAX_DAYS))
    key = (str(db_path), symbol, calendar, rth)
    with _cache_lock:
        d = _cache.get(key)
        if d is None:
            d = _cache[key] = DailyBars(calendar, rth)
    with d.lock:
        if not d.through or since < d.lo:   # first time, or further back than built: from the start
            d.opens, d.ends, d.closes = [], [], []
            d.lo = d.through = since
        if split > d.through:
            d.add(read(db_path, symbol, d.through, split), d.opens, d.ends, d.closes)
            d.through = split
        opens, ends, closes = d.opens[:], d.ends[:], d.closes[:]
    d.add(live, opens, ends, closes)
    n = len(opens)
    first = max(0, n - MAX_DAYS)
    lo = max(first, bisect_right(ends, start, first) - length)
    return {"days": [[opens[i], ends[i], closes[i]] for i in range(lo, n)], "forming": bool(n and ends[-1] > now)}
