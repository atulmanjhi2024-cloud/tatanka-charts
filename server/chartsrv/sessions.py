"""CME Globex session math. ES, NQ, CL and GC all trade 17:00-16:00 CT, Sunday evening to Friday.

Exchange holidays come from config/holidays.json (set_calendar): halts and early closes shut the market early,
closed days shut it all day, per product group. Session boundaries stay at 17:00; is_open(ts, group) and
trade_key(session, group) apply the holidays (the watchdog, and daily bars that fold a holiday's no-settlement
session into the next trade date), and market_ms(ts, group) counts only the time the market trades (drawn lines)."""
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
OPEN_HOUR = 17
CLOSE_HOUR = 16
HOUR_MS = 3_600_000


def to_ct(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, CT)


def day_open(day: date) -> int:
    """The 17:00 CT open of the session that trades on `day` (the evening before): how a day's bar is stamped."""
    eve = day - timedelta(days=1)
    return int(datetime(eve.year, eve.month, eve.day, OPEN_HOUR, tzinfo=CT).timestamp() * 1000)


def session_start(ts_ms: int) -> int:
    """Epoch ms of the 17:00 CT open of the session containing ts_ms."""
    dt = to_ct(ts_ms)
    start = dt.replace(hour=OPEN_HOUR, minute=0, second=0, microsecond=0)
    if dt < start:
        start -= timedelta(days=1)
    return int(start.timestamp() * 1000)


def next_session_start(start_ms: int) -> int:
    # 25h always lands inside the next session, whatever DST does to day length
    return session_start(start_ms + 25 * HOUR_MS)


def is_open(ts_ms: int, group: str | None = None) -> bool:
    """Inside the weekly Globex hours, and, given a product group, not shut for a holiday."""
    if group is not None and _closed(ts_ms, group):
        return False
    dt = to_ct(ts_ms)
    wd, h = dt.weekday(), dt.hour  # Mon=0 .. Sun=6
    if wd == 5:
        return False
    if wd == 6:
        return h >= OPEN_HOUR
    if wd == 4:
        return h < CLOSE_HOUR
    return h != CLOSE_HOUR


def weekend(ts_ms: int) -> bool:
    """From a Friday's 17:00 CT to the Sunday's 16:00 CT, and a closed holiday's session (Christmas, New Year's Day,
    Good Friday: from the evening before's 17:00 to the day's 16:00), when no future trades. The 16:00 hours either
    side are snap()'s, which moves a late print into its session, unless it has none to move it into (a closed
    Friday's, the Sunday's before a closed Monday). A print stamped here is a feed replaying its last trade on
    (re)connecting (NinjaTrader does, stamped with the PC's clock): a Saturday or Christmas bar of one price on a
    daily chart."""
    dt = to_ct(ts_ms)
    if dt.hour == CLOSE_HOUR and dt.weekday() != 5:   # the break: where snap() moves the print
        ts_ms = snap(ts_ms)
        dt = to_ct(ts_ms)
    wd = dt.weekday()
    return wd == 5 or (wd == 4 and dt.hour >= OPEN_HOUR) or (wd == 6 and dt.hour < CLOSE_HOUR) or _no_session(ts_ms)


def closed_spans(start_ms: int, end_ms: int) -> set[tuple[int, int]]:
    """The closed holidays' sessions among weekends(): CME has traded some of them after all (a short Good Friday
    session for the jobs report: ES and NQ on 2021-04-02 and 2023-04-07), so stored data there is not always a
    replay."""
    return {(a, b) for day, v in _days.items() if v.get("closed")
            for a, b in [(day_open(day), _ct_ms(day, "16:00"))] if a < end_ms and b > start_ms}


def weekends(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """The weekend() spans, [from, to) in epoch ms, that touch start_ms..end_ms, in time order: the weekends and the
    closed holidays' sessions."""
    out = [(day_open(day), _ct_ms(day, "16:00")) for day, v in _days.items() if v.get("closed")]
    d = to_ct(start_ms).date()
    d -= timedelta(days=(d.weekday() - 4) % 7)   # the Friday on or before
    while True:
        a = int(datetime(d.year, d.month, d.day, OPEN_HOUR, tzinfo=CT).timestamp() * 1000)
        s = d + timedelta(days=2)
        b = int(datetime(s.year, s.month, s.day, CLOSE_HOUR, tzinfo=CT).timestamp() * 1000)
        if a >= end_ms:
            return sorted((a, b) for a, b in out if a < end_ms and b > start_ms)
        out.append((a, b))
        d += timedelta(days=7)


def snap(ts_ms: int) -> int:
    """Moves a print stamped inside the 16:00-17:00 CT break (clock skew, late reports) into the session it
    belongs to: before 16:30 (and all of Friday's 16:00 hour) to the last ms of the closing session, from
    16:30 (and Sunday's pre-open) to the next 17:00 open. A closed holiday's session is never one: the print goes
    the other way then (weekend() tells the feeds when neither way trades). Anything else is returned unchanged."""
    if is_open(ts_ms):
        return ts_ms
    dt = to_ct(ts_ms)
    wd = dt.weekday()
    if dt.hour != CLOSE_HOUR or wd == 5:
        return ts_ms
    back = int(dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000) - 1   # the closing session's last ms
    ahead = session_start(ts_ms + HOUR_MS)                                              # the next 17:00 open
    early = wd != 6 and (dt.minute < 30 or wd == 4)
    if _no_session(back if early else ahead):   # Christmas Eve's 16:40 back to Christmas Eve, not on into Christmas
        early = not early
    return back if early else ahead


def previous_session_start(start_ms: int) -> int:
    """Open of the last session that traded before start_ms; skips the weekend and closed holidays (New Year's,
    Good Friday, Christmas: no session for any product)."""
    t = start_ms - 1
    for _ in range(120):
        if is_open(t) and not _no_session(t):
            return session_start(t)
        t -= HOUR_MS
    return session_start(start_ms - 1)


def last_close(ts_ms: int, group: str | None = None) -> int:
    """When the market last shut, at a moment ts_ms it is shut: 16:00 CT, Friday's close over a weekend, a holiday's
    early close or halt."""
    t = ts_ms // 60_000 * 60_000
    for _ in range(120):   # whole hours back to one it traded in (a long weekend is under 100)...
        if is_open(t - HOUR_MS, group):
            break
        t -= HOUR_MS
    for _ in range(60):    # ...then minutes back to its last one
        if is_open(t - 60_000, group):
            break
        t -= 60_000
    return t


def _no_session(ts_ms: int) -> bool:
    d = _days.get((to_ct(ts_ms) + timedelta(hours=24 - OPEN_HOUR)).date()) if _days else None   # its trade date
    return bool(d and d.get("closed"))


class SessionClock:
    """Caches the current session bounds so per-tick lookups stay cheap."""

    def __init__(self):
        self.start = self.end = 0

    def locate(self, ts_ms: int) -> int:
        if not (self.start <= ts_ms < self.end):
            self.start = session_start(ts_ms)
            self.end = next_session_start(self.start)
        return self.start


# --- exchange holidays ---------------------------------------------------------------------------------
_days: dict[date, dict] = {}                         # CT date -> {"name", "halt" | "close" | "closed"}
_closures: dict[str, tuple[list[int], list[int]]] = {}   # group -> (starts, ends), sorted epoch ms


def set_calendar(cal: dict | None):
    """Loads config/holidays.json's content ({"days": [{"date", "name", "halt"|"close": "HH:MM" or {group: "HH:MM"},
    "closed": true}]}); raises ValueError on a malformed entry."""
    days = {}
    for d in (cal or {}).get("days", []):
        kinds = [k for k in ("halt", "close", "closed") if k in d]
        if len(kinds) != 1:
            raise ValueError(f"holiday {d.get('date')}: give one of halt, close or closed")
        day = date.fromisoformat(d["date"])
        v = d[kinds[0]]
        if kinds[0] != "closed":
            for t in (v.values() if isinstance(v, dict) else [v]):
                h, m = map(int, t.split(":"))
                if not (0 <= h < 24 and 0 <= m < 60):
                    raise ValueError(f"holiday {day}: bad time {t}")
        days[day] = {"name": d.get("name", ""), kinds[0]: v}
    _days.clear()
    _days.update(days)
    _closures.clear()
    _shut.clear()
    _cut.clear()
    market_ms.cache_clear()


def holidays() -> dict[date, dict]:
    return dict(_days)


def calendar() -> dict:
    """The calendar loaded, in set_calendar's form (a worker process loads the same one)."""
    return {"days": [{"date": d.isoformat(), **v} for d, v in sorted(_days.items())]}


def _ct_ms(day: date, hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return int(datetime(day.year, day.month, day.day, h, m, tzinfo=CT).timestamp() * 1000)


def _time_for(v, group: str) -> str | None:
    return v.get(group, v.get("default")) if isinstance(v, dict) else v


def _intervals(group: str) -> tuple[list[int], list[int]]:
    got = _closures.get(group)
    if got is None:
        spans = []
        for day, d in _days.items():
            if d.get("closed"):
                spans.append((_ct_ms(day - timedelta(days=1), "17:00"), _ct_ms(day, "17:00")))
            else:
                t = _time_for(d.get("halt") or d.get("close"), group)
                if t:
                    spans.append((_ct_ms(day, t), _ct_ms(day, "17:00")))
        spans.sort()
        got = _closures[group] = ([a for a, _ in spans], [b for _, b in spans])
    return got


def _closed(ts_ms: int, group: str) -> bool:
    starts, ends = _intervals(group)
    i = bisect_right(starts, ts_ms) - 1
    return i >= 0 and ts_ms < ends[i]


def first_closure(start_ms: int, end_ms: int, group: str) -> int:
    """When the first holiday closure (a halt, an early close or a closed day) in [start_ms, end_ms) begins, else end_ms."""
    starts, _ = _intervals(group)
    i = bisect_left(starts, start_ms)
    return starts[i] if i < len(starts) and starts[i] < end_ms else end_ms


# --- market time ---------------------------------------------------------------------------------------
DAY_MS = 24 * HOUR_MS
WEEK_MS = 7 * DAY_MS
SESSION_MS = (24 - OPEN_HOUR + CLOSE_HOUR) * HOUR_MS   # 17:00 to 16:00: 23 hours
_ORIGIN = (3 * 24 + OPEN_HOUR) * HOUR_MS              # Sunday 4 January 1970 17:00, on Chicago's clock read as UTC
_shut: dict[str, tuple[list[int], list[int], list[int]]] = {}   # group -> closures' starts, ends, open ms shut before each
_cut: dict[tuple[str, int], tuple[list[int], list[int]]] = {}   # (group, bar ms) -> what each closure cuts off its bar, sums
_offsets: dict[int, int] = {}   # UTC hour -> Chicago's offset then, in ms (it changes only on the hour)


def _week_ms(ts_ms):
    """Open ms up to ts_ms on the weekly hours alone (Sunday 17:00 to Friday 16:00 CT less the daily break), counted on
    Chicago's clock from a fixed Sunday open: DST changes on a Sunday at 02:00, while the market is shut, so an open hour
    is an hour on that clock too."""
    hour = int(ts_ms // HOUR_MS)
    off = _offsets.get(hour)
    if off is None:   # every trade asks (trend-line alerts): the zone looked up once an hour
        if len(_offsets) > 50_000:
            _offsets.clear()
        off = _offsets[hour] = to_ct(hour * HOUR_MS).utcoffset() // timedelta(milliseconds=1)
    w, r = divmod(ts_ms + off - _ORIGIN, WEEK_MS)
    k, s = divmod(r, DAY_MS)   # the k-th 17:00 open of the week, s past it
    return w * 5 * SESSION_MS + (k * SESSION_MS + min(s, SESSION_MS) if k < 5 else 5 * SESSION_MS)


def _shut_spans(group: str) -> tuple[list[int], list[int], list[int]]:
    got = _shut.get(group)
    if got is None:
        starts, ends = _intervals(group)
        before = [0]
        for a, b in zip(starts, ends):
            before.append(before[-1] + _week_ms(b) - _week_ms(a))
        got = _shut[group] = (starts, ends, before)
    return got


def _cut_bars(group: str, per: int) -> tuple[list[int], list[int]]:
    """What each holiday closure cuts off the bar of length per (one that fits the hour evenly, so its bars start on
    the hour's marks from the 17:00 open) it falls in, and the sums before each: CL's 13:30 halt leaves its 13:00 1h
    bar half an hour short. A closure that starts while the market is shut cuts nothing."""
    got = _cut.get((group, per))
    if got is None:
        starts, _ = _intervals(group)
        cut = [-s % per if _week_ms(s + 1) > _week_ms(s) else 0 for s in starts]
        before = [0]
        for c in cut:
            before.append(before[-1] + c)
        got = _cut[(group, per)] = (cut, before)
    return got


@lru_cache(maxsize=4096)   # a trend line's two ends, asked again on every trade
def market_ms(ts_ms, group: str | None = None, per: int | None = None):
    """Market time: the ms the market has traded up to ts_ms from a fixed start, the weekly hours less, given a product
    group, its holiday closures. It stands still while the market is shut, so the gap between two moments in it is what
    a seconds, minute or hour chart has bars for: a drawn line runs straight in it (drawings.line_price; web/drawings.js
    marketMs is its twin). Given per, the bar length of such a chart, a bar a closure cuts short counts whole, as that
    chart gives it a whole bar's room: the time runs on by the clock into the closure until the bar would have ended."""
    m = _week_ms(ts_ms)
    if group is None or not _days:
        return m
    starts, ends, before = _shut_spans(group)
    i = bisect_right(starts, ts_ms)
    if i:
        m -= before[i]
        inside = ts_ms < ends[i - 1]
        if inside:   # inside a closure: only what it has shut so far
            m += _week_ms(ends[i - 1]) - _week_ms(ts_ms)
        if per:
            cut, cut_before = _cut_bars(group, per)
            m += cut_before[i - 1] + (min(cut[i - 1], ts_ms - starts[i - 1]) if inside else cut[i - 1])
    return m


def holiday_halt(day: date, group: str) -> bool:
    """A holiday halt: the session trading into it settles on the next trade date."""
    d = _days.get(day)
    return bool(d and _time_for(d.get("halt"), group))


def fold_trade_date(day: date, group: str | None) -> date:
    """The trade date a day's session counts toward: itself, or for a holiday halt (no settlement that day: MLK Day,
    Thanksgiving, ...) the next day that trades, as CME's daily bars and TradingView fold it (MLK Day's into Tuesday's)."""
    if not group or not holiday_halt(day, group):
        return day
    n = day + timedelta(days=1)
    for _ in range(10):
        d = _days.get(n)
        if n.weekday() < 5 and not (d and (d.get("closed") or _time_for(d.get("halt"), group))):
            return n
        n += timedelta(days=1)
    return n


def counts_toward(start_ms: int, group: str | None) -> int:
    """The open of the session whose trade date a session counts toward: itself, or after a holiday halt the next
    session that trades (fold_trade_date). A folded daily bar is stamped there, so it reads as that trade date."""
    if not group or not _days or not holiday_halt((to_ct(start_ms) + timedelta(hours=24 - OPEN_HOUR)).date(), group):
        return start_ms
    t = next_session_start(start_ms)
    for _ in range(10):
        if is_open(t + 60_000, group) and not _no_session(t + 60_000):
            return t
        t = next_session_start(t)
    return start_ms


# --- the indicators' session settings ---------------------------------------------------------------------
ES_RTH = (8 * 60 + 30, 15 * 60)   # the regular hours (CT) the indicators' session settings are written for


def rth_mover(rth):
    """For an instrument whose regular hours aren't ES's 08:30-15:00, a function moving a CT time (minutes of the
    day) written for ES's hours to the same place in its own: one nearer the open moves with the open, one nearer
    the close with the close (CL: the IB 08:30-09:30 becomes 08:00-09:00, the 15:00 close 13:30); the overnight
    (before 07:00, from 16:00) stays. None when nothing moves."""
    if not rth:
        return None
    o, c = (int(x[:2]) * 60 + int(x[3:5]) for x in rth)
    if (o, c) == ES_RTH:
        return None

    def move(m: int) -> int:
        if m < 7 * 60 or m >= 16 * 60:
            return m
        return m + (o - ES_RTH[0] if abs(m - ES_RTH[0]) <= abs(m - ES_RTH[1]) else c - ES_RTH[1])
    return move


def move_session(v: str, move) -> str:
    """"HHmm-HHmm" (optionally ":days") moved by rth_mover's function; anything else as it is."""
    if not move or not isinstance(v, str):
        return v
    rng, sep, days = v.partition(":")
    ends = rng.split("-")
    if len(ends) != 2 or not all(e.strip().isdigit() and 3 <= len(e.strip()) <= 4 for e in ends):
        return v
    out = []
    for e in ends:
        e = e.strip().zfill(4)
        m = move(int(e[:2]) * 60 + int(e[2:]))
        out.append(f"{m // 60:02d}{m % 60:02d}")
    return "-".join(out) + sep + days


def move_hhmmss(v: int, move) -> int:
    """HHmmss (Gap Analysis' 83000) moved by rth_mover's function."""
    if not move or isinstance(v, bool) or not isinstance(v, int):
        return v
    m = move(v // 10000 * 60 + v // 100 % 100)
    return m // 60 * 10000 + m % 60 * 100 + v % 100


def move_clock(v: str, move) -> str:
    """"HH:mm" (Session VP's 08:30) moved by rth_mover's function; anything else as it is."""
    if not move or not isinstance(v, str) or len(v) != 5 or v[2] != ":" or not (v[:2] + v[3:]).isdigit():
        return v
    m = move(int(v[:2]) * 60 + int(v[3:]))
    return f"{m // 60:02d}:{m % 60:02d}"


def trade_key(start_ms: int, group: str) -> int:
    """The daily bar a session belongs to, keyed by the first session of its trade date: a session that runs into
    a holiday halt has no settlement and belongs with the next trading session (CME's daily bars)."""
    if not _days:
        return start_ms
    prev = previous_session_start(start_ms)
    prev_trade_date = (to_ct(prev) + timedelta(days=1)).date()
    return prev if holiday_halt(prev_trade_date, group) else start_ms
