from datetime import datetime

import pytest

from chartsrv.bars import parse_spec, make_series
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT, is_open, previous_session_start, session_start


def ms(y, mo, d, h, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=CT).timestamp() * 1000)


def series(spec, tick=0.25, dec=2):
    return make_series(parse_spec(spec), tick, dec)


def tk(ts, price, size=1, side=1):
    return Tick("ES", ts, price, size, side)


def test_parse_spec():
    assert parse_spec("5m").size == 300
    assert parse_spec("1h").kind == "time"
    assert parse_spec("2000t").kind == "tick"
    assert parse_spec("16r").kind == "range"
    assert parse_spec("4h").size == 14400 and parse_spec("1W").kind == parse_spec("1M").kind == "time"
    for bad in ("0m", "5x", "366D", "53W", "13M", "90s", "0s", ""):
        with pytest.raises(ValueError):
            parse_spec(bad)
    assert parse_spec("15s").size == 15 and not parse_spec("15s").from_minutes and parse_spec("60s").text == "1m"
    assert (parse_spec("2D").cal, parse_spec("2D").mult, parse_spec("3M").history_days) == ("D", 2, 20 * 366)


def test_custom_calendar_bars():
    """2D: trading days in pairs from a fixed start; 2W: Monday weeks in pairs; 3M: quarters; 12M: years. Each bar is
    stamped at the 17:00 CT open before its first day."""
    from datetime import date

    from chartsrv.bars import calendar_bucket
    from chartsrv.sessions import day_open
    open_before = lambda y, m, d: day_open(date(y, m, d)) - 0   # noqa: E731  the session trading on that day
    mon, tue, wed, fri = (open_before(2026, 10, d) for d in (12, 13, 14, 16))
    pairs = {calendar_bucket(s, "D", 2) for s in (mon, tue, wed)}
    assert len(pairs) == 2 and calendar_bucket(mon, "D", 2) in (day_open(date(2026, 10, 12)), day_open(date(2026, 10, 9)))
    assert calendar_bucket(fri, "D", 5) == day_open(date(2026, 10, 12))   # a 5-day bar is a Monday-Friday week
    w = sorted({calendar_bucket(open_before(2026, 10, d), "W", 2) for d in (5, 12, 19, 26)})
    assert len(w) in (2, 3) and all(b - a == 14 * 86_400_000 for a, b in zip(w, w[1:]))   # fortnights
    assert calendar_bucket(open_before(2026, 8, 14), "M", 3) == day_open(date(2026, 7, 1))   # Q3
    assert calendar_bucket(open_before(2026, 11, 2), "M", 12) == day_open(date(2026, 1, 1))
    assert calendar_bucket(tue, "W") == day_open(date(2026, 10, 12)) and calendar_bucket(tue, "M") == day_open(date(2026, 10, 1))


def test_seconds_bars():
    s = make_series(parse_spec("15s"), 0.25, 2)
    t0 = 1_760_000_400_000 - 1_760_000_400_000 % 60_000
    for i, px in enumerate([100, 101, 102, 101.5, 103]):
        s.on_tick(Tick("ES", t0 + i * 7_000, px, 1, 1))   # 0, 7, 14 | 21, 28
    assert [(b.t - t0, b.o, b.c, b.v) for b in s.bars] == [(0, 100, 102, 3), (15_000, 101.5, 103, 2)]


def test_session_math():
    # Monday 10:00 belongs to the session that opened Sunday 17:00
    assert session_start(ms(2026, 10, 12, 10)) == ms(2026, 10, 11, 17)
    assert session_start(ms(2026, 10, 12, 17)) == ms(2026, 10, 12, 17)
    assert not is_open(ms(2026, 10, 12, 16, 30))       # daily break
    assert not is_open(ms(2026, 10, 17, 12))           # Saturday
    assert is_open(ms(2026, 10, 18, 17, 1))            # Sunday open
    # previous session before Sunday's open is Thursday 17:00 (Friday's session)
    assert previous_session_start(ms(2026, 10, 18, 17)) == ms(2026, 10, 15, 17)
    # DST: session spanning the Nov 1 2026 fall-back still opens at 17:00 CT
    assert session_start(ms(2026, 11, 2, 9)) == ms(2026, 11, 1, 17)


def test_time_bars_align_to_session_and_merge():
    s = series("5m")
    s.on_tick(tk(ms(2026, 10, 12, 9, 31), 100))
    s.on_tick(tk(ms(2026, 10, 12, 9, 34, 59), 101))
    s.on_tick(tk(ms(2026, 10, 12, 9, 35), 99, side=-1))
    assert [b.t for b in s.bars] == [ms(2026, 10, 12, 9, 30), ms(2026, 10, 12, 9, 35)]
    b = s.bars[0]
    assert (b.o, b.h, b.l, b.c, b.v, b.n) == (100, 101, 100, 101, 2, 2)
    assert s.on_tick(tk(ms(2026, 10, 12, 9, 33), 50)) == []   # late print ignored
    assert s.bars[1].bv - s.bars[1].sv == -1


def test_time_bars_from_1m_history():
    s = series("15m")
    for i in range(20):
        s.on_bar(Bar(t=ms(2026, 10, 12, 9, 30 + i), o=100 + i, h=101 + i, l=99 + i, c=100.5 + i, v=10, n=3))
    assert len(s.bars) == 2
    assert s.bars[0].t == ms(2026, 10, 12, 9, 30)
    assert s.bars[0].v == 150 and s.bars[0].o == 100 and s.bars[0].c == 114.5
    assert s.bars[1].h == 120


def test_daily_bar_is_one_session():
    s = series("1D")
    s.on_tick(tk(ms(2026, 10, 12, 18), 100))      # Mon evening -> Tuesday's session
    s.on_tick(tk(ms(2026, 10, 13, 15), 105))      # Tue afternoon, same session
    s.on_tick(tk(ms(2026, 10, 13, 17, 1), 104))   # new session
    assert len(s.bars) == 2 and s.bars[0].t == ms(2026, 10, 12, 17)


def test_weekly_and_monthly_bars_follow_trade_dates():
    w, mo = series("1W"), series("1M")
    for s in (w, mo):
        s.on_bar(Bar(t=ms(2026, 10, 18, 17), o=100, h=101, l=99, c=100, v=1))     # Sunday open: Mon 19 Oct
        s.on_bar(Bar(t=ms(2026, 10, 23, 15, 59), o=100, h=110, l=99, c=108, v=1))  # Friday close, same week
        s.on_bar(Bar(t=ms(2026, 10, 30, 10), o=108, h=109, l=90, c=95, v=1))      # next week, still October
        s.on_bar(Bar(t=ms(2026, 11, 1, 17, 30), o=95, h=96, l=94, c=96, v=1))     # Sunday 1 Nov: Mon 2 Nov, November
    assert [b.t for b in w.bars] == [ms(2026, 10, 18, 17), ms(2026, 10, 25, 17), ms(2026, 11, 1, 17)]
    assert (w.bars[0].h, w.bars[0].c) == (110, 108)
    assert [b.t for b in mo.bars] == [ms(2026, 9, 30, 17), ms(2026, 10, 31, 17)]
    assert (mo.bars[0].o, mo.bars[0].h, mo.bars[0].l, mo.bars[0].c, mo.bars[0].v) == (100, 110, 90, 95, 3)


def test_tick_bars_count_and_session_reset():
    s = series("3t")
    t0 = ms(2026, 10, 12, 9)
    for i in range(7):
        s.on_tick(tk(t0 + i, 100 + i))
    assert [b.n for b in s.bars] == [3, 3, 1]
    s.on_tick(tk(ms(2026, 10, 12, 17, 0, 1), 200))  # new session starts a new bar
    assert [b.n for b in s.bars] == [3, 3, 1, 1]
    assert [b.seq for b in s.bars] == [1, 2, 3, 4]


def test_volume_bars_split_prints():
    s = series("10v")
    t0 = ms(2026, 10, 12, 9)
    s.on_tick(tk(t0, 100, size=7))
    out = s.on_tick(tk(t0 + 1, 101, size=25))
    assert [b.v for b in s.bars] == [10, 10, 10, 2]
    assert len(out) == 4


def test_range_bars_close_at_limit_and_fill_gaps():
    s = series("4r")  # 4 ticks = 1.00 point
    t0 = ms(2026, 10, 12, 9)
    s.on_tick(tk(t0, 100.00))
    s.on_tick(tk(t0 + 1, 100.75))
    assert len(s.bars) == 1
    s.on_tick(tk(t0 + 2, 101.25))           # breaks above 101.00
    b0, b1 = s.bars[0], s.bars[1]
    assert (b0.l, b0.h, b0.c) == (100.00, 101.00, 101.00)
    assert (b1.o, b1.l, b1.c) == (101.25, 101.25, 101.25)
    s.on_tick(tk(t0 + 3, 104.00))           # gap: phantom bars, each exactly 4 ticks, next opens +1 tick
    for b in list(s.bars)[1:-1]:
        assert round((b.h - b.l) / 0.25) == 4 and b.c == b.h
    for a, b in zip(list(s.bars)[1:], list(s.bars)[2:]):
        assert round((b.o - a.c) / 0.25) == 1
    assert s.bars[-1].c == 104.00 and s.bars[-1].h - s.bars[-1].l <= 1.00
    n = len(s.bars)
    s.on_tick(tk(t0 + 4, 102.50))           # break down from the last bar
    last_full = s.bars[n - 1]
    assert last_full.l == round(last_full.h - 1.00, 2) and last_full.c == last_full.l
    assert s.bars[-1].o == round(last_full.c - 0.25, 2)


def test_range_bars_gold_tick_rounding():
    s = series("10r", tick=0.1, dec=1)
    t0 = ms(2026, 10, 12, 9)
    s.on_tick(tk(t0, 3500.0))
    s.on_tick(tk(t0 + 1, 3501.3))
    assert s.bars[0].h == 3501.0 and s.bars[1].o == 3501.1


def test_renko_bricks_continue_and_reverse():
    s = series("4k")   # 4 ticks = 1.00 on ES
    t0 = ms(2026, 10, 12, 9)
    for i, p in enumerate([100, 100.75, 101.0, 102.0, 101.0, 100.0, 99.75, 98.0]):
        s.on_tick(tk(t0 + i, p))
    bricks = [(b.o, b.c) for b in s.bars]
    # 101 and 102 make two up bricks; 101 is no reversal (one brick down from 102); 100 is the last brick's open
    # less a brick: a down brick 101 -> 100; 99.75 is not yet a brick lower; 98 makes 100 -> 99 and 99 -> 98.
    assert bricks[:5] == [(100.0, 101.0), (101.0, 102.0), (101.0, 100.0), (100.0, 99.0), (99.0, 98.0)]
    assert s.bars[-1].o == 98.0 and s.bars[-1].c == 98.0 and len(bricks) == 6   # the brick forming
    assert all(b.h == max(b.o, b.c) and b.l == min(b.o, b.c) for b in list(s.bars)[:-1])   # no wicks
    gap = series("4k")
    gap.on_tick(tk(t0, 100))
    out = gap.on_tick(tk(t0 + 1, 103.5))          # 3 bricks at once, the last one forming
    assert [(b.o, b.c) for b in out[:-1]] == [(100.0, 101.0), (101.0, 102.0), (102.0, 103.0)] and out[-1].c == 103.5


def test_rth_only_series_keeps_regular_hours_and_aligns_to_the_open():
    s = make_series(parse_spec("1h"), 0.25, 2, rth=("08:30", "15:15"), rth_only=True)
    for t in [ms(2026, 10, 12, 7, 59), ms(2026, 10, 12, 8, 30), ms(2026, 10, 12, 9, 29), ms(2026, 10, 12, 9, 30),
              ms(2026, 10, 12, 15, 14), ms(2026, 10, 12, 15, 20), ms(2026, 10, 12, 18)]:
        s.on_tick(tk(t, 100))
    assert [b.t for b in s.bars] == [ms(2026, 10, 12, 8, 30), ms(2026, 10, 12, 9, 30), ms(2026, 10, 12, 14, 30)]
    d = make_series(parse_spec("1D"), 0.25, 2, rth=("08:30", "15:15"), rth_only=True)
    d.on_bar(Bar(t=ms(2026, 10, 12, 3), o=1, h=200, l=1, c=1, v=9))      # overnight: left out
    d.on_bar(Bar(t=ms(2026, 10, 12, 10), o=100, h=101, l=99, c=100.5, v=5))
    assert len(d.bars) == 1 and d.bars[0].t == ms(2026, 10, 12, 8, 30) and d.bars[0].h == 101
