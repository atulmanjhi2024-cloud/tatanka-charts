"""Bar replay: the bars around a past moment, built the way chart history is (stored 1m bars for time bars,
stored trades for tick, volume and range bars, VWAP included), for the browser to step through."""
from datetime import timedelta

from . import sessions
from .bars import BarSpec, Series, make_series
from .config import Instrument
from .models import Bar
from .store import bar_1m_start, iter_bars_1m, iter_ticks_range
from .studies import RthClock

BEFORE = 1500        # bars before the replay start, to look back on
AFTER = 1500         # bars after it; tick, volume and range bars stop sooner at the end of its session
DAY_MS = 86_400_000


def trading_session(at: int) -> int:
    """Open of the session trading at `at`, or of the last one before it (in the break, a weekend, a holiday)."""
    return sessions.previous_session_start(at + 1)


def next_trading_session(start: int) -> int:
    """Open of the next session that trades after the one opening at `start` (Friday and Saturday 17:00 never do,
    nor the evening before New Year's, Good Friday or Christmas)."""
    end = sessions.next_session_start(start)
    for _ in range(10):
        if sessions.is_open(end + 60_000) and not sessions._no_session(end + 60_000):
            break
        end = sessions.next_session_start(end)
    return end


def next_trade(now: int, inst: Instrument, rth_only: bool) -> int:
    """The first moment from `now` on that a chart can take a trade: now while the market trades (inside RTH on an RTH
    chart), else when it opens again (the next session that trades, or on an RTH chart the next RTH open)."""
    clock = RthClock(inst.rth) if rth_only else None
    t = now
    for _ in range(30):   # a weekend, a holiday: a few steps
        if not sessions.is_open(t, inst.calendar):
            t = next_trading_session(trading_session(t))
        elif clock and clock.day(t) is None:
            o = clock.window(sessions.to_ct(t))[0]
            t = o if o > t else clock.window(sessions.to_ct(t) + timedelta(days=1))[0]
        else:
            break
    return t


def can_form(s: Series, bar: Bar, now: int, inst: Instrument) -> bool:
    """The last bar may still be forming: the next trade the chart can take goes into it. Tick, volume and range bars
    restart at each session open, so it must have opened in that trade's session; a time bar must be the one it falls
    in (a week's bar takes the next day's trades too)."""
    t = next_trade(now, inst, s.rth_only)
    return s.bucket(t) == bar.t if s.spec.kind == "time" else bar.t >= trading_session(t)


def hours_end(t: int, inst: Instrument, rth_only: bool) -> int:
    """Where the hours of a bar opening at t end: on an RTH chart its RTH close, else its session's (16:00 CT), or a
    holiday's early close before it."""
    close = (RthClock(inst.rth).window(sessions.to_ct(t))[1] if rth_only
             else sessions.next_session_start(trading_session(t)) - sessions.HOUR_MS)
    return sessions.first_closure(t, close, inst.calendar)


def hours_ends(bars: list[Bar], inst: Instrument, rth_only: bool) -> dict[int, int]:
    """Tick, volume and range bars that the next one opens a session later than (an RTH day's last bar, a session's):
    where each ends, by its index. That is where its hours end, not the next one's open, a night or a weekend later."""
    out, close = {}, None
    for i in range(len(bars) - 1):
        if close is None or bars[i].t >= close:   # the hours this bar is in
            close = hours_end(bars[i].t, inst, rth_only)
        if bars[i + 1].t > close:
            out[i] = close
    return out


def build(db_path, symbol: str, inst: Instrument, spec: BarSpec, at: int, now: int, max_rowid: int,
          rth_only: bool = False) -> tuple[list[Bar], int | None, int | None]:
    """Bars around `at`, where to ask next to play on past the last one (None: they reach the present), and where the
    last one ends if not there: tick, volume and range bars, at the present the open of the one left out, else the end
    of its hours when no trade can join it (the stretch ends with its session, or none is forming); None: the next
    stretch's first bar's open. The last bar that might still be forming, or cut short by the window, is left out."""
    s = make_series(spec, inst.tick_size, inst.decimals, int(BEFORE * 1.2) + AFTER + 10, inst.rth, rth_only=rth_only,
                    group=inst.calendar)
    after, next_at, end_at = 0, None, None
    if spec.from_minutes:
        since = (at - spec.history_days * DAY_MS if spec.cal
                 else bar_1m_start(db_path, symbol, at, int(spec.size // 60 * BEFORE * 1.1 * (4 if rth_only else 1)) + 1))
        if not spec.cal:   # inside a session: its VWAPs from its open, as chart history's (market.build_history)
            from .market import _vwap_from_open   # here: market imports this module (through htfema)
            _vwap_from_open(db_path, s, symbol, since)
        for m in iter_bars_1m(db_path, symbol, since, now):
            new = s.last
            s.on_bar(m)
            if s.last is not new and s.last.t >= at:
                after += 1
                if after > AFTER:
                    next_at = s.last.t
                    break
        bars = list(s.bars)
        if bars and (next_at is not None or can_form(s, bars[-1], now, inst)):
            bars.pop()   # the one it stopped in (the next stretch starts with it), or the one still forming
        bars = [b for b in bars if b.t >= since]   # the first bar may have started before `since`
    else:
        cur = trading_session(at)
        since, end = sessions.previous_session_start(cur), next_trading_session(cur)
        for tk in iter_ticks_range(db_path, symbol, since, min(end, now), max_rowid):
            new = s.last
            s.on_tick(tk)
            if s.last is new or s.last.t < at:
                continue
            # A small chart's session holds more bars than the series keeps: it stops AFTER bars on, at a bar opening
            # later than the one before it, so the last bar shown is the last of its ms (the browser joins on it).
            if after >= AFTER and new is not None and s.last.t > new.t:
                next_at = s.last.t
                break
            after += s.last.seq - (new.seq if new else 0)   # one trade can open several (a big print, a gap)
        bars = list(s.bars)
        if next_at is not None:
            bars = [b for b in bars if b.t < next_at]   # the bars it stopped in: the next stretch starts with them
        elif end < now:   # the next stretch: the next session's; the last bar ends with its hours, before it opens
            next_at, end_at = end, hours_end(bars[-1].t, inst, s.rth_only) if bars else None
        elif bars and can_form(s, bars[-1], now, inst):
            end_at = bars.pop().t   # the one still forming: the last one shown ends at its open, not at its own
        elif bars:   # none can be (the RTH close, the daily break, a weekend): the last one ends with its hours
            end_at = hours_end(bars[-1].t, inst, s.rth_only)
    return bars, next_at, end_at
