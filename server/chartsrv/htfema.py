# Copyright (c) 2026 Tatanka Trading LLC.
# This Source Code Form is subject to the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, obtain one at https://mozilla.org/MPL/2.0/.
"""Tatanka 1hr 21 EMA (HTF EMA) v1.0.0, ported from the NinjaTrader 8 source
(tatankatrading/tatanka-indicators, ninjatrader/TatankaHtfEma.cs).

The indicator's OnBarUpdate runs in the browser (web/studies.js, htfEmaValues), on the bars the chart holds,
live on every price change. The server supplies what the chart cannot compute from its own bars:
NinjaTrader's EMA over the higher-timeframe bars (closed ones only), and the chart-timeframe EMA up to the
chart's first bar, so the SmoothScaled line starts settled instead of from a cold seed. A seconds chart's EMA is
thousands of bars long (75600 on a 1s chart), far more than the chart holds: its seed comes from stored trades and,
further back, from the 1-minute bars (seconds_seed). On an RTH chart all of it comes from the regular hours, as
NinjaTrader's added series follows an RTH trading-hours template."""
import asyncio
import math
from datetime import timedelta
from itertools import chain

from . import sessions
from .amt_runner import ct, epoch_ms
from .replay import next_trading_session, trading_session
from .sessions import SessionClock
from .store import iter_bars_1m, iter_closes_1s
from .studies import RthClock
from .tpo_service import chart_bars, rth_day, session_end

VERSION = "1.0.0"
MODES = ("LiveHTF", "ExactHTF", "SmoothScaled")
DEFAULTS = {"HtfPeriodType": "Minute", "HtfPeriod": 60, "EmaLength": 21, "Mode": "SmoothScaled"}
SETTLE = 8              # EMA lengths of history loaded before what is shown: the seed's weight is ~e^-16
MAX_DAYS = 400
SECONDS_EXACT = 4       # seconds charts: lengths of their own bars, from stored trades, before the first bar
SECONDS_TRADES = 4_000_000   # at most this many stored trades read a request; further back, the 1-minute bars
SHARED = 16             # seconds charts' answers kept for identical requests (shared)


def settings(overrides: dict | None) -> dict:
    s = dict(DEFAULTS)
    for k, v in (overrides or {}).items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown HTF EMA setting {k}")
        s[k] = v
    if s["HtfPeriodType"] not in ("Minute", "Day"):
        raise ValueError("HtfPeriodType must be Minute or Day")
    if s["Mode"] not in MODES:
        raise ValueError(f"Mode must be one of {', '.join(MODES)}")
    for k, hi in (("HtfPeriod", 1440 if s["HtfPeriodType"] == "Minute" else 1), ("EmaLength", 500)):
        if isinstance(s[k], bool) or not isinstance(s[k], int) or not 1 <= s[k] <= hi:
            raise ValueError(f"{k} must be 1-{hi}")
    return s


class NtEma:
    """NinjaTrader 8's EMA: the first input, then Input * 2/(1+Period) + (1 - 2/(1+Period)) * previous."""

    def __init__(self, period: int):
        self.constant1 = 2.0 / (1 + period)
        self.constant2 = 1 - (2.0 / (1 + period))
        self.values: list[float] = []

    def add(self, x: float) -> float:
        v = x if not self.values else x * self.constant1 + self.constant2 * self.values[-1]
        self.values.append(v)
        return v


def GetPeriodSeconds(period_type: str, value: int) -> float:
    if period_type == "Second":
        return value
    if period_type == "Minute":
        return value * 60.0
    if period_type == "Day":
        return value * 86400.0
    if period_type == "Week":
        return value * 604800.0
    if period_type == "Month":
        return value * 2592000.0
    if period_type == "Year":
        return value * 31536000.0
    return 0


def GetScaledLength(s: dict, chart_type: str, chart_value: int) -> int:
    htfSec = GetPeriodSeconds(s["HtfPeriodType"], s["HtfPeriod"])
    chartSec = GetPeriodSeconds(chart_type, chart_value)
    if htfSec <= 0 or chartSec <= 0:
        return max(1, s["EmaLength"])
    scaled = int(round(s["EmaLength"] * (htfSec / chartSec)))    # Math.Round: midpoints to even, as round()
    return max(1, scaled)


def chart_period(spec_kind: str, spec_seconds: int) -> tuple[str, int]:
    """Our bar spec as NinjaTrader's BarsPeriodType and value: hour charts are 60n-minute charts."""
    if spec_kind != "time":
        return {"tick": "Tick", "volume": "Volume", "range": "Range", "renko": "Renko"}[spec_kind], spec_seconds
    if spec_seconds in (86400, 604800, 2678400):   # 1D, 1W, 1M
        return {86400: "Day", 604800: "Week", 2678400: "Month"}[spec_seconds], 1
    return "Minute", spec_seconds // 60


def chart_period_spec(sp) -> tuple[str, int]:
    """A BarSpec as NinjaTrader's BarsPeriodType and value: 15s is Second 15, 2D is Day 2, 3M is Month 3."""
    if sp.cal:
        return {"D": "Day", "W": "Week", "M": "Month"}[sp.cal], sp.mult
    if sp.kind == "time" and sp.size % 60:
        return "Second", sp.size
    return chart_period(sp.kind, sp.size)


def htf_minutes(s: dict) -> int:
    return s["HtfPeriod"] if s["HtfPeriodType"] == "Minute" else 1440   # a 1440-minute bucket is the session


def htf_bars(s: dict, mins, calendar: str | None, rth: tuple[str, str] | None = None) -> list[list]:
    """The higher-timeframe bars, as chart_bars gives them (rth: an RTH chart's). Day: one per trade date, as the
    chart's 1D bars: a holiday halt's short session (MLK Day, ...) is in the next day's bar, which closes at that day's
    16:00 close (on an RTH chart its RTH close)."""
    bars = chart_bars(mins, htf_minutes(s), rth)
    if s["HtfPeriodType"] != "Day" or not calendar:
        return bars
    days: list[list] = []
    for b in bars:
        k = sessions.counts_toward(sessions.session_start(b[4]) if rth else b[4], calendar)
        if days and days[-1][4] == k:
            d = days[-1]
            d[2], d[3], d[6] = max(d[2], b[2]), min(d[3], b[3]), b[6]
        else:
            days.append([b[0], rth_day(k, rth)[1] if rth else session_end(k), b[2], b[3], k, b[5], b[6]])
    return days


def history_ms(s: dict, chart_minutes: int | None, scaled: int, from_ms: int, now_ms: int,
               chart_seconds: int | None = None, rth: tuple[str, str] | None = None) -> int:
    """How far back to load: SETTLE lengths of both EMAs before the chart's first bar, in trading days (a seconds
    chart's as if it had a bar every period; rth: an RTH chart's, whose days are as long as its regular hours)."""
    hours = RthClock(rth) if rth else None
    per_day = hours.end - hours.start if hours else 1380   # Globex minutes per day, or the regular hours'

    def span(minutes):   # the trading days one bar spans: a bar longer than the regular hours is one session's
        return min(minutes, per_day) / per_day if minutes < 1380 else minutes / 1380
    chart = chart_minutes or (chart_seconds or 0) / 60
    htf = span(htf_minutes(s)) * s["EmaLength"] * SETTLE
    smooth = scaled * span(chart) * SETTLE if chart and s["Mode"] == "SmoothScaled" else 0
    days = math.ceil(max(htf, smooth) * 7 / 5) + 3
    return min(from_ms, now_ms) - min(days, MAX_DAYS) * 86_400_000


def compute(s: dict, minute_bars, chart_minutes: int | None, scaled: int, from_ms: int, now_ms: int,
            calendar: str | None = None, bucket=None, chart_seconds: int | None = None, second_closes=None,
            rth: tuple[str, str] | None = None) -> dict:
    """htf: [close ms, EMA or None until EmaLength bars] for each closed HTF bar from the last one before
    from_ms; due: when the forming HTF bar closes (None: none trading), for the browser to ask again;
    smooth: the chart EMA and bar count before from_ms (time charts in SmoothScaled mode);
    ends: session closes (16:00 CT) from from_ms, which cut the last chart bar of each session.
    calendar: the instrument's holiday group, for Day bars as the chart's 1D bars; bucket: D, 2D and 3D charts'
    TimeSeries.bucket, for their own bars in smooth. chart_seconds: a seconds chart's bar (1-59 s; chart_minutes is
    None), second_closes(since, before) its stored trades as (second ms, close), for seconds_seed. rth: the
    instrument's regular hours, for an RTH chart: only their minutes (and trades), the HTF and chart bars counted
    from the RTH open, the ends at the RTH close."""
    mins = list(minute_bars)
    if rth:
        hours, opens = RthClock(rth), {}
        mins = [m for m in mins if hours.day(m.t) is not None]
        if second_closes is not None:   # a seconds RTH chart's trades too: the regular hours', a day's at a time
            all_hours = second_closes
            second_closes = lambda lo, hi: chain.from_iterable(all_hours(*w) for w in hours.windows(lo, hi))  # noqa: E731

        def rth_open(ts):   # where an RTH chart's bars count from, worked out once a day (as Series.rth_open)
            day = hours.day(ts)
            if day not in opens:
                opens[day] = hours.window(sessions.to_ct(ts))[0]
            return opens[day]
    ema = NtEma(s["EmaLength"])
    htf, due = [], None
    for k, close, h, l, sess, o, c in htf_bars(s, mins, calendar, rth):
        if close > now_ms:
            due = close
            break                                         # the forming HTF bar: only closed ones count
        v = ema.add(c)
        htf.append([close, v if len(ema.values) >= s["EmaLength"] else None])
    first = next((i for i, x in enumerate(htf) if x[0] >= from_ms), len(htf))
    smooth = None
    if chart_minutes and s["Mode"] == "SmoothScaled":
        e = NtEma(scaled)
        if bucket:   # bars of several sessions, a holiday halt's in the next day's: not one bar per session
            k = c = None
            for m in mins:
                t = bucket(m.t)
                if t >= from_ms:
                    break
                if k is not None and t != k:
                    e.add(c)
                k, c = t, m.c
            if c is not None:
                e.add(c)
        else:
            for b in chart_bars(mins, chart_minutes, rth):
                if b[0] >= from_ms:
                    break
                e.add(b[6])
        smooth = {"seed": e.values[-1] if e.values else None, "count": len(e.values)}
    elif chart_seconds and s["Mode"] == "SmoothScaled":
        smooth = seconds_seed(mins, chart_seconds, scaled, from_ms, second_closes, rth_open if rth else None)
    ends = []
    if chart_minutes or chart_seconds:
        hh, mm = divmod(RthClock(rth).end, 60) if rth else (16, 0)
        d = ct(from_ms).replace(hour=hh, minute=mm, second=0, microsecond=0)
        while epoch_ms(d) <= now_ms + 86_400_000:
            ends.append(epoch_ms(d))
            d += timedelta(days=1)
    return {"version": VERSION, "ready": True, "len": s["EmaLength"], "scaled": scaled, "mode": s["Mode"],
            "htf": htf[max(0, first - 1):], "due": due, "smooth": smooth, "ends": ends}


def seconds_seed(mins: list, seconds: int, scaled: int, from_ms: int, second_closes=None, origin=None) -> dict:
    """A seconds chart's EMA and bar count before from_ms. Its bars are built from trades, and SETTLE lengths of them
    are millions of ES trades (8 x 21 hours of trading at the defaults), so: the last SECONDS_EXACT lengths are the
    chart's own bars, rebuilt from the stored trades (second_closes; at most SECONDS_TRADES of them, as the 1-minute
    bars count them, or their volume where they don't), each closing at its last second's trade. Before that, and in a
    minute with no stored trades (bars imported or filled from 1-minute history), each 1-minute bar stands for the chart
    bars that open in it, all at its close: no more than its trade count, as a seconds bar only exists where a trade
    printed (NinjaTrader's too). origin: where a timestamp's bars count from (an RTH chart's: its RTH open; else the
    session open)."""
    period = seconds * 1000
    constant1, constant2 = 2.0 / (1 + scaled), 1 - 2.0 / (1 + scaled)
    locate = origin or SessionClock().locate
    v, count = None, 0

    def add(c, k=1):   # k bars closing at c: NinjaTrader's EMA step, or k of them on the same close at once
        nonlocal v, count
        if v is None:
            v = c
        elif k == 1:
            v = c * constant1 + constant2 * v
        else:
            v = c + (v - c) * constant2 ** k
        count += k

    def held(m):   # the chart bars that open in a minute, counted from the session open (0 trades: not known)
        o = locate(m.t)
        first = o - (o - m.t) // period * period          # the first one opening at or after m.t
        k = len(range(first, m.t + 60_000, period))
        return min(k, m.n) if m.n else k

    before = [m for m in mins if m.t < from_ms]
    exact, need, trades = len(before), SECONDS_EXACT * scaled, 0   # before[exact:]: from the stored trades
    if second_closes is not None:
        while exact and need > 0:
            trades += before[exact - 1].n or before[exact - 1].v   # no count (imported, filled): its volume, no fewer
            if trades > SECONDS_TRADES:
                break
            exact -= 1
            need -= held(before[exact])
    for m in before[:exact]:
        if m.t + 60_000 <= from_ms:   # not the minute the chart starts in
            add(m.c, held(m))
    if exact == len(before):
        return {"seed": v, "count": count}
    secs = iter(second_closes(before[exact].t, from_ms))
    nxt = next(secs, None)
    k = c = None   # the chart bar being built: its open and close
    for m in before[exact:]:
        end = m.t + 60_000
        if nxt is None or nxt[0] >= end:   # no stored trade in this minute: its 1-minute bar
            if end <= from_ms:
                if k is not None:
                    add(c)
                    k = None
                add(m.c, held(m))
            continue
        while nxt is not None and nxt[0] < end:
            o = locate(nxt[0])
            t = o + (nxt[0] - o) // period * period   # as TimeSeries.bucket
            if k is not None and t != k:
                add(c)
            k, c = t, nxt[1]
            nxt = next(secs, None)
    if k is not None:
        add(c)
    return {"seed": v, "count": count}


def seconds_job(db_path, symbol: str, s: dict, since: int, split: int, live: list, scaled: int, from_ms: int,
                now_ms: int, calendar: str | None, chart_seconds: int, rth: tuple[str, str] | None = None) -> dict:
    """compute for a seconds chart in Smooth mode, a worker job (workers.run): its seed reads up to SECONDS_TRADES
    stored trades, seconds of CPU that in a thread hold the server's GIL. The 1-minute bars are the stored ones before
    split, then `live`."""
    return compute(s, chain(iter_bars_1m(db_path, symbol, since, split), live), None, scaled, from_ms, now_ms, calendar,
                   chart_seconds=chart_seconds, second_closes=lambda lo, hi: iter_closes_1s(db_path, symbol, lo, hi),
                   rth=rth)


_answers: dict[tuple, asyncio.Future] = {}


async def shared(key: tuple, now_ms: int, work) -> dict:
    """One computation for identical requests (key: symbol first, then the chart, its first bar and the settings),
    for a seconds chart's costly ones: the frames and devices asking for the same chart at once, a reload. One being
    worked out is waited for, one worked out is given while its HTF bar still forms (now_ms before its due), and to
    those that waited for it when none forms (the market shut: nothing changes meanwhile); a failure is every
    waiter's. The SHARED last are kept; forget() drops a symbol's. work(): a coroutine that works it out."""
    while (f := _answers.get(key)) is not None:
        waited = not f.done()
        d = await asyncio.shield(f)
        if d["due"] is not None and now_ms < d["due"] or waited and d["due"] is None:   # none forming: the market shut
            return d
        if _answers.get(key) is f:   # out of date (its HTF bar has closed since): this request works it out again
            break
    _answers.pop(key, None)
    f = _answers[key] = asyncio.ensure_future(work())

    def failed(f):   # not kept
        if (f.cancelled() or f.exception() is not None) and _answers.get(key) is f:
            del _answers[key]
    f.add_done_callback(failed)
    while len(_answers) > SHARED:
        del _answers[next(iter(_answers))]
    return await asyncio.shield(f)


def forget(symbol: str):
    """Stored history changed (a gap fill): worked out again."""
    for k in [k for k in _answers if k[0] == symbol]:
        del _answers[k]


def next_open(bucket, now_ms: int, rth: tuple[str, str] | None = None) -> int | None:
    """2D, 3D, week and month charts (bucket: their series' TimeSeries.bucket): when the next bar opens once the one
    holding the last session is over, from its close (16:00 CT; rth: an RTH chart's, at the RTH close) to then,
    where the HTF bar closing with it runs after it, as on a tie; None while it trades."""
    last = trading_session(now_ms)
    if now_ms < (rth_day(last, rth)[1] if rth else session_end(last)):
        return None
    t = bucket(next_trading_session(last))
    return t if t != bucket(last) else None
