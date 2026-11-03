"""Bar builders: time, tick, volume and range bars from a tick stream.

Spec strings: 15s 1m 5m 15m 1h 4h (time), 1D 2D 1W 2W 1M 3M 12M (calendar), 2000t (trades), 5000v (contracts),
16r (range in ticks), 8k (Renko, brick size in ticks). Seconds bars are built from trades; minute and longer bars from
stored 1-minute bars. N days are N trading days (counted Monday-Friday from a fixed start, as one long chart would),
N weeks and N months group from fixed starts too (3M: quarters, 12M: years). An RTH-only series (rth_only) keeps only the instrument's regular hours, its time bars aligned to
the RTH open, as a NinjaTrader chart on an RTH trading-hours template; its session VWAP still counts every trade from the
17:00 open, as the ETH chart's and the Session VWAP alert's do.
Time bars align to the 17:00 CT session open; tick, volume and range bars restart at each session open,
like NinjaTrader's default. A week holds the sessions of one Monday-Friday trade week, a month those whose trade
date falls in it; their bars are stamped at the nominal 17:00 open the day before the first trade date."""
import re
from collections import deque
from dataclasses import dataclass
from datetime import date, timedelta

from .models import Bar, Tick
from . import sessions
from .sessions import SessionClock
from .studies import RthClock, Vwap

SPEC_RE = re.compile(r"^(\d+)([smhDWMtvrk])$")
CALENDAR = {"1D": 400, "1W": 5 * 366 + 7, "1M": 20 * 366}   # spec -> days of 1-minute history to build from
CAL_MAX = {"D": 365, "W": 52, "M": 12}                       # the longest custom calendar bars
MAX_PHANTOM_BARS = 1000


@dataclass(frozen=True)
class BarSpec:
    text: str
    kind: str   # time | tick | volume | range
    size: int   # seconds for time bars; trades, contracts or ticks otherwise
    cal: str = ""   # calendar bars: D, W or M
    mult: int = 1   # how many of them (2D, 3M)

    @property
    def time_based(self) -> bool:
        return self.kind == "time"

    @property
    def from_minutes(self) -> bool:
        """Built from stored 1-minute bars (minute and longer time bars); seconds bars need the trades."""
        return self.kind == "time" and self.size >= 60

    @property
    def history_days(self) -> int:
        """Days of 1-minute history a calendar chart is built from (enough for its bars, at most 20 years)."""
        return min(CALENDAR[f"1{self.cal}"] * self.mult, 20 * 366) if self.cal else 0


def parse_spec(text: str) -> BarSpec:
    m = SPEC_RE.match(text.strip())
    if not m or int(m[1]) <= 0:
        raise ValueError(f"bad bar spec {text!r}; use e.g. 5m, 4h, 1D, 1W, 1M, 2000t, 5000v, 16r")
    n, unit = int(m[1]), m[2]
    if unit == "s":
        if n % 60 == 0:
            return parse_spec(f"{n // 60}m")
        if n > 59:
            raise ValueError("seconds bars: 1s to 59s (longer ones are minutes)")
        return BarSpec(f"{n}s", "time", n)
    if unit == "m":
        return BarSpec(f"{n}m", "time", n * 60)
    if unit == "h":
        return BarSpec(f"{n}h", "time", n * 3600)
    if unit in "DWM":
        if n > CAL_MAX[unit]:
            raise ValueError(f"at most {CAL_MAX[unit]}{unit}")
        return BarSpec(f"{n}{unit}", "time", n * {"D": 1, "W": 7, "M": 31}[unit] * 86400, unit, n)
    return BarSpec(f"{n}{unit}", {"t": "tick", "v": "volume", "r": "range", "k": "renko"}[unit], n)


class Series:
    def __init__(self, spec: BarSpec, tick_size: float, decimals: int, maxlen: int = 3000,
                 rth: tuple[str, str] | None = None, studies: bool = True, rth_only: bool = False, group: str | None = None):
        """group: the instrument's CME holiday group, for D/W/M bars that fold a holiday halt's short session into
        the next trade date (CME's daily bars, TradingView); None: every session a day of its own."""
        self.spec = spec
        self.group = group
        self._fold = (None, 0)   # (session open, the one whose trade date it counts toward)
        self.studies = studies
        self.rth = rth
        self.rth_only = bool(rth_only and rth)
        self._rth_day = (None, 0)   # (RTH day, its open epoch ms)
        self.vwap, self.vwap_rth, self.rth_clock = Vwap(), Vwap(), RthClock(rth)
        self.tick_size = tick_size
        self.decimals = decimals
        self.bars: deque[Bar] = deque(maxlen=maxlen)
        self.clock = SessionClock()
        self._seq = 0
        self._session = None
        self._ms_t = self._ms_v = 0   # the last trade's millisecond and the contracts traded in it so far
        self.bulk = False      # loading history: a bar's VWAP values are set once it is done, not on every trade
        self._lazy: list[Bar] = []
        self._lazy_rth = False

    @property
    def last(self) -> Bar | None:
        return self.bars[-1] if self.bars else None

    def _open(self, t: int, price: float, taken: int = 0) -> Bar:
        """A bar opening in the millisecond of the last trades starts after them (and after `taken`, the part of the
        print being added that the bars before took): its pre. A time bar's trades all come after it opens."""
        self._seq += 1
        pre = self._ms_v + taken if t == self._ms_t else 0
        b = Bar(t=t, o=price, h=price, l=price, c=price, seq=self._seq, pre=pre)
        self.bars.append(b)
        return b

    def _new_session(self, ts: int) -> bool:
        start = self.clock.locate(ts)
        changed = start != self._session
        self._session = start
        return changed

    def in_hours(self, ts: int) -> bool:
        return not self.rth_only or self.rth_clock.day(ts) is not None

    def rth_open(self, ts: int) -> int:
        """Epoch ms of the RTH open of ts's day (ts inside RTH)."""
        day = self.rth_clock.day(ts)
        if self._rth_day[0] != day:
            self._rth_day = (day, self.rth_clock.window(sessions.to_ct(ts))[0])
        return self._rth_day[1]

    def on_tick(self, tk: Tick) -> list[Bar]:
        if not self.in_hours(tk.ts):   # in no bar of an RTH chart, but in its session VWAP
            if self.studies:
                self._study(tk.ts, tk.price, tk.size, [])
            return []
        if tk.ts > self._ms_t:   # a late print (older than the last trade) is in no bar's opening millisecond
            self._ms_t, self._ms_v = tk.ts, 0
        bars = self._tick(tk)
        if tk.ts == self._ms_t:
            self._ms_v += tk.size
        if self.studies and bars:
            self._study(tk.ts, tk.price, tk.size, bars)
        return bars

    def on_bar(self, m: Bar) -> list[Bar]:
        """Merge a stored 1m bar; its VWAP contribution uses the bar's typical price (h+l+c)/3."""
        if not self.in_hours(m.t):
            self.vwap_only(m)
            return []
        bars = self._bar(m)
        if self.studies and bars:
            self._study(m.t + 59_999, (m.h + m.l + m.c) / 3, m.v, bars)
        return bars

    def vwap_only(self, m: Bar):
        """A stored 1m bar in none of this series' bars (outside an RTH chart's hours, or before its history's first
        bar): only into the VWAPs, by its typical price."""
        if self.studies:
            self._study(m.t + 59_999, (m.h + m.l + m.c) / 3, m.v, [])

    def _study(self, ts: int, price: float, vol: float, bars: list[Bar]):
        if self.bulk and self._lazy and not (len(bars) == 1 and len(self._lazy) == 1 and bars[0] is self._lazy[0]):
            self._set_st(self._lazy, self._lazy_rth)   # the bars before this trade are done: their values as of now
            self._lazy = []
        self.vwap.add(self.clock.locate(ts), price, vol)
        day = self.rth_clock.day(ts) if self.rth else None
        if day is not None:
            self.vwap_rth.add(day, price, vol)
        if self.bulk:
            if not self._lazy:
                self._lazy = list(bars)
            self._lazy_rth = day is not None
        else:
            self._set_st(bars, day is not None)

    def _set_st(self, bars: list[Bar], rth: bool):
        e = self.vwap.value()
        r = self.vwap_rth.value() if rth else None
        d = self.decimals + 2
        st = [round(e[0], d), round(e[1], d), None, None] if e else [None, None, None, None]
        if r:
            st[2], st[3] = round(r[0], d), round(r[1], d)
        for b in bars:
            b.st = st

    def end_bulk(self):
        """History is loaded: the last bars get their values, and every trade from now on sets them."""
        if self._lazy:
            self._set_st(self._lazy, self._lazy_rth)
        self._lazy, self.bulk = [], False

    def _tick(self, tk: Tick) -> list[Bar]:
        raise NotImplementedError

    def _bar(self, m: Bar) -> list[Bar]:
        raise TypeError(f"{self.spec.kind} bars can only be built from ticks")


def calendar_bucket(start: int, unit: str, mult: int = 1) -> int:
    """The day group (D), week (W) or month (M) of the session opening at `start`, as the 17:00 CT open before its
    first trade date. mult > 1: groups of that many, counted from fixed starts (trading days Monday-Friday, Monday
    weeks, months from January of year 0), so the bars don't shift as the chart's history does."""
    trade = (sessions.to_ct(start) + timedelta(hours=7)).date()   # a 17:00 open trades the next day
    if unit == "D":
        week = (trade.toordinal() - 1) // 7                       # date(1, 1, 1) was a Monday
        bd = week * 5 + min(trade.weekday(), 4)
        bd -= bd % mult
        first = date.fromordinal(bd // 5 * 7 + 1 + bd % 5)
    elif unit == "W":
        week = (trade.toordinal() - 1) // 7
        first = date.fromordinal((week - week % mult) * 7 + 1)
    else:
        mi = trade.year * 12 + trade.month - 1
        mi -= mi % mult
        first = date(mi // 12, mi % 12 + 1, 1)
    return sessions.day_open(first)


class TimeSeries(Series):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.period_ms = self.spec.size * 1000
        self.calendar = self.spec.cal
        self._cal = (0, 0)   # (session start, its bucket)

    def bucket(self, ts: int) -> int:
        start = folded = self.clock.locate(ts)
        if self.calendar and self.group:   # MLK Day's short session: Tuesday's bar, stamped at Tuesday's open
            if self._fold[0] != start:
                self._fold = (start, sessions.counts_toward(start, self.group))
            folded = self._fold[1]
        if self.rth_only and (not self.calendar or self.spec.text == "1D"):   # bars count from the RTH open
            o = self.rth_open(ts) if folded == start else self.rth_clock.window(sessions.to_ct(folded + 16 * 3_600_000))[0]
            return o if self.calendar else o + (ts - o) // self.period_ms * self.period_ms
        if not self.calendar:
            return start + (ts - start) // self.period_ms * self.period_ms
        if self.spec.text == "1D":
            return folded
        if self._cal[0] != folded:
            self._cal = (folded, calendar_bucket(folded, self.calendar, self.spec.mult))
        return self._cal[1]

    def _tick(self, tk: Tick) -> list[Bar]:
        t = self.bucket(tk.ts)
        b = self.last
        if b is None or t > b.t:
            b = self._open(t, tk.price)
        elif t < b.t:
            return []  # late print for a closed bar
        b.add(tk.price, tk.size, tk.side)
        return [b]

    def _bar(self, m: Bar) -> list[Bar]:
        t = self.bucket(m.t)
        b = self.last
        if b is None or t > b.t:
            self._seq += 1
            b = Bar(t=t, o=m.o, h=m.h, l=m.l, c=m.c, v=m.v, bv=m.bv, sv=m.sv, n=m.n, seq=self._seq)
            self.bars.append(b)
        elif t == b.t:
            b.merge(m)
        else:
            return []
        return [b]


class TickSeries(Series):
    def _tick(self, tk: Tick) -> list[Bar]:
        b = self.last
        if self._new_session(tk.ts) or b is None or b.n >= self.spec.size:
            b = self._open(tk.ts, tk.price)
        b.add(tk.price, tk.size, tk.side)
        return [b]


class VolumeSeries(Series):
    def _tick(self, tk: Tick) -> list[Bar]:
        if tk.size <= 0:
            return []
        out, left = [], tk.size
        b = self.last
        if self._new_session(tk.ts) or b is None:
            b = self._open(tk.ts, tk.price)
        while left > 0:
            if b.v >= self.spec.size:
                b = self._open(tk.ts, tk.price, tk.size - left)
            take = min(left, self.spec.size - b.v)
            b.add(tk.price, take, tk.side)
            left -= take
            if not out or out[-1] is not b:
                out.append(b)
        return out


class RangeSeries(Series):
    """Each bar spans exactly `size` ticks high to low. When price breaks out, the bar closes at its range
    limit and the next bar opens one tick beyond it; gaps are filled with zero-volume bars."""

    def _px(self, n: int) -> float:
        return round(n * self.tick_size, self.decimals)

    def _tick(self, tk: Tick) -> list[Bar]:
        b = self.last
        if self._new_session(tk.ts) or b is None:
            b = self._open(tk.ts, tk.price)
            b.add(tk.price, tk.size, tk.side)
            return [b]
        R = self.spec.size
        p = round(tk.price / self.tick_size)
        lo, hi = round(b.l / self.tick_size), round(b.h / self.tick_size)
        out = [b]
        if p - lo > R:
            while p - lo > R and len(out) < MAX_PHANTOM_BARS:
                b.h = b.c = self._px(lo + R)
                lo = lo + R + 1
                b = self._open(tk.ts, self._px(lo))
                out.append(b)
        elif hi - p > R:
            while hi - p > R and len(out) < MAX_PHANTOM_BARS:
                b.l = b.c = self._px(hi - R)
                hi = hi - R - 1
                b = self._open(tk.ts, self._px(hi))
                out.append(b)
        b.add(tk.price, tk.size, tk.side)
        return out


class RenkoSeries(Series):
    """NinjaTrader-style Renko: bricks `size` ticks tall with no wicks. Another brick in the trend's direction once
    price goes a brick past the last one's close; a reversal brick once it goes a brick past the last one's open (two
    bricks from its close). The last bar is the brick forming: it opens at the last brick's close and closes at the
    price now. Gaps are filled with zero-volume bricks; it restarts at each session open."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.lo = self.hi = None   # the last completed brick's open and close, in ticks (lo is its open)

    def _px(self, n: int) -> float:
        return round(n * self.tick_size, self.decimals)

    def _tick(self, tk: Tick) -> list[Bar]:
        p, R = round(tk.price / self.tick_size), self.spec.size
        b = self.last
        if self._new_session(tk.ts) or b is None:
            self.lo = self.hi = p   # a flat start: the first brick either way is one brick away
            b = self._open(tk.ts, tk.price)
            b.add(tk.price, tk.size, tk.side)
            return [b]
        out = [b]
        o, c = self.lo, self.hi   # the last brick's open and close
        up = c >= o
        while len(out) < MAX_PHANTOM_BARS:
            if (up and p >= c + R) or (not up and o != c and p >= o + R) or (o == c and p >= c + R):
                new_o = c if up or o == c else o
                new_c = new_o + R
            elif (not up and p <= c - R) or (up and o != c and p <= o - R) or (o == c and p <= c - R):
                new_o = c if (not up) or o == c else o
                new_c = new_o - R
            else:
                break
            # the forming bar becomes this brick; a new one forms at its close
            b.o, b.c = self._px(new_o), self._px(new_c)
            b.h, b.l = max(b.o, b.c), min(b.o, b.c)
            o, c, up = new_o, new_c, new_c > new_o
            b = self._open(tk.ts, self._px(c))
            out.append(b)
        self.lo, self.hi = o, c
        b.add(tk.price, tk.size, tk.side)
        return out


KINDS = {"time": TimeSeries, "tick": TickSeries, "volume": VolumeSeries, "range": RangeSeries, "renko": RenkoSeries}


def make_series(spec: BarSpec, tick_size: float, decimals: int, maxlen: int = 3000,
                rth: tuple[str, str] | None = None, studies: bool = True, rth_only: bool = False,
                group: str | None = None) -> Series:
    return KINDS[spec.kind](spec, tick_size, decimals, maxlen, rth, studies, rth_only, group)
