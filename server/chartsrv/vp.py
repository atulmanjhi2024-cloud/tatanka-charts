"""Volume profiles, ported from three NinjaTrader 8 indicators (Tatanka Trading): their calculation cores, line by line.
The drawing is in web/studies.js; vp_service.py feeds them this server's 1-minute bars and trades.

- TatankaAutoAnchoredVolumeProfile 1.1.0 (AAVP): one profile from a calendar anchor (default the year) to now.
- TatankaPeriodicVolumeProfile 1.0.0 (PVP): one profile per day, week, month or year (default the week).
  Both read 1-minute bars and spread each bar's volume evenly over the ticks from its low to its high, as up volume
  (close >= open) or down. Their Profile classes are the same code.
- TatankaSessionVolumeProfile 1.0.0 (SVP): one profile per custom session (default 08:30-15:00) from every trade,
  split into up and down volume by the tick rule.

The C# sources are kept unchanged in tests/vp_csharp; tests/vp_parity.py compiles their cores and compares every
number with these."""
import math
import re
from datetime import date, datetime, timedelta

MAX_ROWS = 20_000
ANCHORS = ("Session", "Week", "Month", "Quarter", "Year", "Decade", "Century")
PERIODS = ("Daily", "Weekly", "Monthly", "Yearly")
_TIME_RE = re.compile(r"^(?:(\d{1,2}):(\d{2})|(?:(\d{2}):(\d{2}):(\d{2})))$")


def round_away(x: float) -> int:
    """Math.Round(x, MidpointRounding.AwayFromZero), as .NET does it (on the exact fraction)."""
    frac, whole = math.modf(x)
    if abs(frac) >= 0.5:
        whole += math.copysign(1.0, frac)
    return int(whole)


def _finite(*xs) -> bool:
    return all(not (math.isnan(x) or math.isinf(x)) for x in xs)


def _add_months(d: date, n: int) -> date:   # DateTime.AddMonths on the 1st of a month
    m = d.year * 12 + d.month - 1 + n
    return date(m // 12, m % 12 + 1, 1)


# --- calendar windows (trading days) -------------------------------------------------------------------------------
def anchor_window(td: date, anchor: str) -> tuple[date, date]:
    """TatankaAavpCore.AnchorClock.Get: [start, end) of the anchor period holding the trading day."""
    if anchor == "Session":
        return td, td + timedelta(days=1)
    if anchor == "Week":   # a whole Monday-Sunday week, also at New Year
        s = td - timedelta(days=td.weekday())
        return s, s + timedelta(days=7)
    if anchor == "Month":
        s = date(td.year, td.month, 1)
        return s, _add_months(s, 1)
    if anchor == "Quarter":
        s = date(td.year, (td.month - 1) // 3 * 3 + 1, 1)
        return s, _add_months(s, 3)
    if anchor == "Year":
        return date(td.year, 1, 1), date(td.year + 1, 1, 1)
    if anchor == "Decade":
        y = max(1, td.year // 10 * 10)
        return date(y, 1, 1), date(y + 10, 1, 1)
    if anchor == "Century":
        y = max(1, td.year // 100 * 100)
        return date(y, 1, 1), date(y + 100, 1, 1)
    raise ValueError(f"anchor {anchor!r}")


def period_window(td: date, period: str, multiple: int) -> tuple[date, date]:
    """TatankaPvpCore.PeriodClock.Get: [start, end) of the period holding the trading day. Days, weeks and months
    count from January 1 and stop at the year's end (a week across New Year is two); years count from 2000."""
    if not 1 <= multiple <= 100:
        raise ValueError("multiple")
    year_start, year_end = date(td.year, 1, 1), date(td.year + 1, 1, 1)
    if period == "Daily":
        start = year_start + timedelta(days=(td - year_start).days // multiple * multiple)
        end = start + timedelta(days=multiple)
    elif period == "Weekly":
        monday = year_start - timedelta(days=year_start.weekday())
        start = monday + timedelta(days=(td - monday).days // (7 * multiple) * 7 * multiple)
        end = start + timedelta(days=7 * multiple)
        if start < year_start:
            start = year_start
    elif period == "Monthly":
        start = date(td.year, (td.month - 1) // multiple * multiple + 1, 1)
        end = _add_months(start, multiple)
    elif period == "Yearly":
        first = 2000 + math.floor((td.year - 2000) / multiple) * multiple
        start = date(max(1, first), 1, 1)
        return start, date(start.year + multiple, 1, 1)
    else:
        raise ValueError(f"period {period!r}")
    return start, min(end, year_end)


# --- AAVP / PVP: profiles of 1-minute bars ------------------------------------------------------------------------------
def choose_ticks_per_row(span: int, target: int) -> int:
    smaller = max(1, math.floor(span / target))
    larger = max(1, math.ceil(span / target))
    se = abs(math.ceil(span / smaller) - target)
    le = abs(math.ceil(span / larger) - target)
    return smaller if se <= le else larger


def _add_range(events: dict, bar):
    """A source bar's volume as a constant density over its ticks: +density at its low, -density past its high."""
    if bar is None or bar[3] <= 0:
        return
    _, low, high, volume, up = bar[:5]
    density = volume / (float(high) - float(low) + 1)
    _add_event(events, low, density if up else 0, 0 if up else density)
    _add_event(events, high + 1, -density if up else 0, 0 if up else -density)


def _add_event(events: dict, key: int, up: float, down: float):
    v = events.get(key)
    if v is None:
        v = events[key] = [0.0, 0.0]
    v[0] += up
    v[1] += down


def calculate(rows: list, tick: float, percent: float, at_least: bool) -> dict:
    """Profile.Calculate: POC (ties: nearest the middle row), value area from the POC out (the larger neighbour
    first; TradingView style stops before a row that would pass the target), and their prices.
    rows: [low tick, high tick, up, down]."""
    out = {"rows": rows, "total": 0.0, "va_volume": 0.0, "max_volume": 0.0, "max_abs_delta": 0.0}
    if not rows:
        return out
    n = len(rows)
    middle = (n - 1) / 2.0
    poc = 0
    for i, r in enumerate(rows):
        t = r[2] + r[3]
        out["total"] += t
        out["max_abs_delta"] = max(out["max_abs_delta"], abs(r[2] - r[3]))
        if t > out["max_volume"] or (t == out["max_volume"] and abs(i - middle) < abs(poc - middle)):
            out["max_volume"], poc = t, i
    lo = hi = poc
    total = out["total"]
    included, target = rows[poc][2] + rows[poc][3], total * percent / 100
    while included < target and (lo > 0 or hi < n - 1):
        above = rows[hi + 1][2] + rows[hi + 1][3] if hi < n - 1 else -1
        below = rows[lo - 1][2] + rows[lo - 1][3] if lo > 0 else -1
        take_above = above > below or (above == below and hi + 1 - poc <= poc - (lo - 1))
        candidate = above if take_above else below
        if not at_least and included + candidate > target + total * 1e-12:
            break
        included += candidate
        if take_above:
            hi += 1
        else:
            lo -= 1
    out.update(poc_index=poc, val_index=lo, vah_index=hi, va_volume=included,
               poc=(rows[poc][0] + (rows[poc][1] - rows[poc][0]) // 2) * tick, val=rows[lo][0] * tick,
               vah=rows[hi][1] * tick, low_edge=(rows[0][0] - 0.5) * tick, high_edge=(rows[-1][1] + 0.5) * tick)
    return out


class MinuteProfile:
    """TatankaAavpCore.Profile (= TatankaPvpCore.Profile): one period's profile. Finished minutes are kept as density
    events; the forming minute is pending, replaced as it changes, so nothing counts twice."""

    def __init__(self, window: tuple, start_chart: int, first_loaded: bool, tick: float, number_of_rows: bool,
                 row_size: int, percent: float, at_least: bool):
        if tick <= 0 or row_size < 1 or percent < 1 or percent > 100:
            raise ValueError("Invalid profile settings.")
        self.window, self.start_chart, self.first_loaded = window, start_chart, first_loaded
        self.tick, self.number_of_rows, self.row_size, self.percent, self.at_least = tick, number_of_rows, row_size, percent, at_least
        self.committed: dict[int, list] = {}
        self.pending = None   # (index, low tick, high tick, volume, up, time)
        self.committed_up = self.committed_down = 0.0
        self.last_chart = None
        self.closed = False
        self.version, self._cached, self._cached_version = 0, None, -1

    def upsert(self, index: int, low: float, high: float, volume: float, up: bool, time: int) -> bool:
        if self.closed or volume < 0 or not _finite(volume, low, high) or high < low:
            return False
        lo_t, hi_t = round_away(low / self.tick), round_away(high / self.tick)
        p = self.pending
        if p is not None:
            if index < p[0]:
                return False
            if index == p[0] and lo_t == p[1] and hi_t == p[2] and volume == p[3] and up == p[4]:
                return False
            if index > p[0]:
                _add_range(self.committed, p)
                if p[4]:
                    self.committed_up += p[3]
                else:
                    self.committed_down += p[3]
        self.pending = (index, lo_t, hi_t, volume, up, time)
        self.last_chart = time
        self.version += 1
        return True

    def complete(self):
        self.snapshot()
        self.closed = True
        self.committed.clear()
        self.pending = None

    def snapshot(self) -> dict:
        if self._cached is not None and (self.closed or self._cached_version == self.version):
            return self._cached
        events = {k: [v[0], v[1]] for k, v in self.committed.items()}
        _add_range(events, self.pending)
        keys = sorted(events)
        if len(keys) < 2:
            return {"rows": []}
        low, high = keys[0], keys[-1] - 1
        span = high - low + 1
        ticks = choose_ticks_per_row(span, self.row_size) if self.number_of_rows else self.row_size
        capped = math.ceil(span / ticks) > MAX_ROWS
        if capped:
            ticks = math.ceil(span / MAX_ROWS)
        count = math.ceil(span / ticks)
        rows = [[low + i * ticks, min(high, low + i * ticks + ticks - 1), 0.0, 0.0] for i in range(count)]
        run_up = run_down = 0.0
        for i in range(len(keys) - 1):   # constant density between event keys, integrated over the rows
            ev = events[keys[i]]
            run_up += ev[0]
            run_down += ev[1]
            cursor, end = keys[i], keys[i + 1]
            while cursor < end:
                r = rows[(cursor - low) // ticks]
                stop = min(end, r[1] + 1)
                r[2] += max(0.0, run_up) * (stop - cursor)
                r[3] += max(0.0, run_down) * (stop - cursor)
                cursor = stop
        p = self.pending
        exp_up = self.committed_up + (p[3] if p is not None and p[4] else 0)
        exp_down = self.committed_down + (p[3] if p is not None and not p[4] else 0)
        up = down = 0.0
        for r in rows:
            up += r[2]
            down += r[3]
        fu = exp_up / up if up > 0 else 0
        fd = exp_down / down if down > 0 else 0
        for r in rows:
            r[2] *= fu
            r[3] *= fd
        out = calculate(rows, self.tick, self.percent, self.at_least)
        out["ticks_per_row"], out["capped"] = ticks, capped
        self._cached, self._cached_version = out, self.version
        return out


class AnchorSeries:
    """TatankaAavpCore.AnchorSeries: the one current anchored profile; a new anchor period replaces it."""

    def __init__(self, anchor: str, tick: float, number_of_rows: bool, row_size: int, percent: float, at_least: bool):
        self.anchor, self.args = anchor, (tick, number_of_rows, row_size, percent, at_least)
        self.last_index = -1
        self.current: MinuteProfile | None = None
        self.first_td = self.last_td = None

    def upsert(self, td: date, session_begin: int, index: int, low, high, volume, up: bool, time: int) -> bool:
        if index < 0 or index < self.last_index or volume < 0 or not _finite(volume, low, high) or high < low:
            return False
        w = anchor_window(td, self.anchor)
        cur = self.current
        if cur is not None and w[0] < cur.window[0]:
            return False
        if cur is not None and w[0] != cur.window[0] and index == self.last_index:
            return False
        if cur is None or w[0] != cur.window[0]:
            self.current = MinuteProfile(w, session_begin, cur is None, *self.args)
            self.first_td = td
        self.last_index = index
        changed = self.current.upsert(index, low, high, volume, up, time)
        if changed:
            self.last_td = td
        return changed


class PeriodicSeries:
    """TatankaPeriodicVolumeProfile.OnBarUpdate's profile list: a new period completes the last profile and starts
    one at the session it opens with; the newest `keep` are kept."""

    def __init__(self, period: str, multiple: int, keep: int, tick: float, number_of_rows: bool, row_size: int,
                 percent: float, at_least: bool):
        self.period, self.multiple, self.keep = period, multiple, keep
        self.args = (tick, number_of_rows, row_size, percent, at_least)
        self.profiles: list[MinuteProfile] = []
        self.current: MinuteProfile | None = None

    def add(self, td: date, session_begin: int, index: int, low, high, volume, up: bool, time: int):
        w = period_window(td, self.period, self.multiple)
        cur = self.current
        if cur is None or cur.window[0] != w[0]:
            if cur is not None and w[0] < cur.window[0]:
                return
            if cur is not None:
                cur.complete()
            self.current = MinuteProfile(w, session_begin, cur is None, *self.args)
            self.profiles.append(self.current)
            del self.profiles[:-self.keep]
        self.current.upsert(index, low, high, volume, up, time)


# --- SVP: custom-session profiles of every trade ------------------------------------------------------------------------
def parse_time(value: str) -> timedelta:
    """SessionClock.ParseTime: 24-hour HH:mm (also H:mm and HH:mm:ss)."""
    m = _TIME_RE.match(str(value))
    if m:
        h, mi, se = (int(x) if x else 0 for x in (m[1] or m[3], m[2] or m[4], m[5]))
        if h <= 23 and mi <= 59 and se <= 59:
            return timedelta(hours=h, minutes=mi, seconds=se)
    raise ValueError("Session times must use 24-hour HH:mm format, for example 08:30 and 15:00.")


def try_window(local: datetime, start: timedelta, end: timedelta) -> tuple[datetime, datetime] | None:
    """SessionClock.TryWindow: the session window [begin, finish) holding a local time, or None. An end at or before
    the start runs overnight."""
    day = datetime(local.year, local.month, local.day)
    begin, finish = day + start, day + end
    if end <= start:
        if local - day < start:
            begin -= timedelta(days=1)
        finish = datetime(begin.year, begin.month, begin.day) + timedelta(days=1) + end
    return (begin, finish) if begin <= local < finish else None


def svp_snapshot(rows: dict, tick: float, tpr: int, percent: float, at_least: bool) -> dict:
    """TatankaVpCore.Session.GetSnapshot. rows: {row key: [up, down]} (key = tick // ticks per row)."""
    keys = sorted(rows)
    row_size = tick * tpr
    out = {"rows": [[k, rows[k][0], rows[k][1]] for k in keys], "row_size": row_size, "max_volume": 0.0, "total": 0.0,
           "va_volume": 0.0}
    if not keys:
        return out
    mn, mx = keys[0], keys[-1]
    midpoint = mn / 2.0 + mx / 2.0
    poc_i = 0
    for i, k in enumerate(keys):
        t = rows[k][0] + rows[k][1]
        out["total"] += t
        if t > out["max_volume"] or (t == out["max_volume"] and abs(k - midpoint) < abs(keys[poc_i] - midpoint)):
            out["max_volume"], poc_i = t, i
    total = lambda i: rows[keys[i]][0] + rows[keys[i]][1]   # noqa: E731
    poc = keys[poc_i]
    low = high = poc
    below, above, n = poc_i - 1, poc_i + 1, len(keys)
    included = total(poc_i)
    target = out["total"] * max(1, min(100, percent)) / 100.0
    while included < target and (low > mn or high < mx):
        has_up, has_down = high < mx, low > mn
        up_vol = total(above) if has_up and above < n and keys[above] == high + 1 else 0
        down_vol = total(below) if has_down and below >= 0 and keys[below] == low - 1 else 0
        if up_vol == 0 and down_vol == 0:   # a run of empty rows: crossed at once, as row-by-row stepping would
            if not has_up:
                low = keys[below] + 1
            elif not has_down:
                high = keys[above] - 1
            else:
                up_d, down_d = keys[above] - poc, poc - keys[below]
                if up_d <= down_d:
                    high = keys[above] - 1
                    low = min(low, poc - min(down_d - 1, up_d - 2))
                else:
                    low = keys[below] + 1
                    high = max(high, poc + min(up_d - 1, down_d - 1))
            continue
        take_up = has_up and (not has_down or up_vol > down_vol or (up_vol == down_vol and high + 1 - poc <= poc - (low - 1)))
        candidate = up_vol if take_up else down_vol
        if not at_least and included + candidate > target + out["total"] * 1e-12:
            break
        included += candidate
        if take_up:
            high += 1
            if above < n and keys[above] == high:
                above += 1
        else:
            low -= 1
            if below >= 0 and keys[below] == low:
                below -= 1
    out.update(poc_key=poc, val_key=low, vah_key=high, va_volume=included,
               poc=poc * row_size + (tpr - 1) / 2.0 * tick, val=low * row_size, vah=high * row_size + (tpr - 1) * tick,
               low_edge=mn * row_size - tick / 2, high_edge=mx * row_size + (tpr - 0.5) * tick)
    return out


class SvpSession:
    """TatankaVpCore.Session: one session window's trades by row, up or down by the tick rule (a trade above the
    last price is up, below it down, at it the same as the last; the session's first trade is up)."""

    def __init__(self, start_local: datetime, start_chart: int, end_chart: int, tick: float, tpr: int):
        if tick <= 0 or tpr < 1:
            raise ValueError("Invalid row size.")
        self.start_local, self.start_chart, self.end_chart = start_local, start_chart, end_chart
        self.tick, self.tpr = tick, tpr
        self.rows: dict[int, list] = {}
        self.last_chart = None
        self.last_price = math.nan
        self.last_was_up = True

    def add(self, price: float, volume: float, time: int):
        if volume <= 0 or not _finite(volume, price):
            return
        key = math.floor(round_away(price / self.tick) / self.tpr)
        if not math.isnan(self.last_price):
            if price > self.last_price:
                self.last_was_up = True
            elif price < self.last_price:
                self.last_was_up = False
        r = self.rows.get(key)
        if r is None:
            r = self.rows[key] = [0.0, 0.0]
        if self.last_was_up:
            r[0] += volume
        else:
            r[1] += volume
        self.last_price = price
        self.last_chart = time

    def snapshot(self, percent: float, at_least: bool) -> dict:
        out = svp_snapshot(self.rows, self.tick, self.tpr, percent, at_least)
        out.update(start=self.start_chart, end=self.end_chart, last=self.last_chart)
        return out
