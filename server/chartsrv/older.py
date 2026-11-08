"""Older history: the bars before a chart's first one, for a chart scrolled or zoomed out past it (the page asks for
them as its left edge comes into view, as TradingView does). They are built the way the chart's own bars are:

- minute and longer bars (D, W and M too) from the stored 1-minute bars, exactly;
- seconds and tick bars from the stored trades, stopping where those do;
- volume, range and Renko bars from the stored trades, and for a session without all of its trades stored, from its
  1-minute bars instead: each minute's open, high and low (in the order its close suggests) and close as prints of a
  quarter of its volume, buys and sells as its delta has them. Close to what its trades would have made, not the
  same: `approx` says so.

Runs in a worker (workers.build_older)."""
import time

from . import sessions
from .bars import BarSpec, Series, make_series
from .models import Tick
from .store import bar_1m_start, connect, iter_bars_1m, iter_ticks_range

DAY_MS = 86_400_000
MAX_COUNT = 5000              # bars one request can ask for
MAX_SESSIONS = 40             # sessions one request reads (tick, volume, range and Renko bars)
BUDGET_S = 6.0                # and for about this long: the page asks again for more
JUMPS = 4                     # stretches with nothing stored stepped over in one request
FALLBACK = {"volume", "range", "renko"}           # can be built from 1-minute bars where no trades are stored
CAL_DAYS = {"D": 1.5, "W": 7.3, "M": 31}           # calendar days a D, W or M bar spans, weekends and holidays in
COMPLETE = 0.99               # a session's trades are all there when they add up to this much of its minutes' volume


def build_older(db_path, symbol: str, spec: BarSpec, tick_size: float, decimals: int, rth, rth_only: bool,
                group: str | None, before: int, count: int, max_rowid: int) -> dict:
    """Up to `count` bars before `before` (the chart's first bar's open, epoch ms), oldest first. more: older ones are
    stored still; approx: some were built from 1-minute bars; next: with no bars but more, the `before` to ask with
    next."""
    count = max(1, min(MAX_COUNT, int(count)))

    def make(n: int) -> Series:
        return make_series(spec, tick_size, decimals, n, rth, rth_only=rth_only, group=group)

    db = connect(db_path, readonly=True)
    try:
        if spec.from_minutes:
            out = _minutes(db_path, db, symbol, spec, make, before, count, rth_only)
        elif spec.kind == "time":
            out = _seconds(db_path, db, symbol, spec, make, before, count, rth_only, max_rowid)
        else:
            out = _sessions(db_path, db, symbol, spec, make, before, count, max_rowid)
    finally:
        db.close()
    bars, more, approx, nxt = out
    # next: where to ask from when no bars came (a long stretch with nothing stored, past what one request steps over)
    return {"bars": [b.to_dict() for b in bars if b.t < before], "more": more, "approx": approx,
            "next": nxt if more and not bars else None}


def _latest(db, symbol: str, end: int, minutes: bool, ticks: bool) -> int | None:
    """The newest stored minute (minutes) or trade (ticks) before end."""
    got = []
    if minutes:
        got.append(db.execute("SELECT MAX(t) FROM bars_1m WHERE symbol=? AND t<?", (symbol, end)).fetchone()[0])
    if ticks:
        got.append(db.execute("SELECT MAX(ts) FROM ticks WHERE symbol=? AND ts<?", (symbol, end)).fetchone()[0])
    got = [g for g in got if g is not None]
    return max(got) if got else None


def _vwap_from(db_path, series: Series, symbol: str, since: int):
    """As market._vwap_from_open: the session's minutes before `since` into the VWAPs only."""
    if series.studies and since:
        for b in iter_bars_1m(db_path, symbol, sessions.session_start(since), since):
            series.vwap_only(b)


def _minutes(db_path, db, symbol, spec, make, before, count, rth_only):
    """Minute and longer bars from stored 1-minute bars; a window with none stored (a long gap) is stepped over."""
    for _ in range(JUMPS):
        s = make(10 ** 6)
        s.bulk = True
        if spec.cal:
            since = before - int(CAL_DAYS[spec.cal] * spec.mult * count * DAY_MS)
        else:
            need = int(spec.size // 60 * count * 1.1) + 1   # margin for short session-edge bars
            if rth_only:
                need *= 4   # regular hours are under a third of the Globex day
            since = bar_1m_start(db_path, symbol, before, need)   # 0: fewer stored, all of them
            _vwap_from(db_path, s, symbol, since)
        for b in iter_bars_1m(db_path, symbol, since, before):
            s.on_bar(b)
        while s.bars and s.bars[0].t < since:   # the first bar began before the window: only part of it
            s.bars.popleft()
        s.end_bulk()
        latest = _latest(db, symbol, since, True, False) if since > 0 else None
        if s.bars or latest is None:   # more: stored before the window, or built past `count` (asked for next)
            return list(s.bars)[-count:], latest is not None or len(s.bars) > count, False, since
        before = latest // 60_000 * 60_000 + 60_000   # nothing in the window: on from the newest minute before it
    return [], True, False, before


def _seconds(db_path, db, symbol, spec, make, before, count, rth_only, max_rowid):
    """Seconds bars from stored trades, as far back as those go."""
    for _ in range(JUMPS):
        need = int(spec.size * count / 60 * 1.1) + 1   # in stored minutes, so a weekend doesn't empty it
        if rth_only:
            need *= 4
        since = bar_1m_start(db_path, symbol, before, need)
        s = make(10 ** 6)
        s.bulk = True
        _vwap_from(db_path, s, symbol, since)
        for tk in iter_ticks_range(db_path, symbol, since, before, max_rowid):
            s.on_tick(tk)
        while s.bars and s.bars[0].t < since:
            s.bars.popleft()
        s.end_bulk()
        latest = _latest(db, symbol, since, False, True) if since > 0 else None
        if s.bars or latest is None:
            return list(s.bars)[-count:], latest is not None or len(s.bars) > count, False, since
        before = latest + 1
    return [], True, False, before


def _sessions(db_path, db, symbol, spec, make, before, count, max_rowid):
    """Tick, volume, range and Renko bars restart at each session's open, so each session is built on its own, newest
    first, until there are enough. A stretch with nothing stored is stepped over."""
    fb = spec.kind in FALLBACK
    t0 = time.monotonic()
    chunks, total, approx, end = [], 0, False, before
    for _ in range(MAX_SESSIONS):
        latest = _latest(db, symbol, end, fb, True)
        if latest is None:
            break
        start = sessions.session_start(latest)
        bars, apx = _session(db_path, db, symbol, make, start, end, fb, max_rowid)
        if bars:
            chunks.append(bars)
            total += len(bars)
            approx |= apx
        end = start
        if total >= count or time.monotonic() - t0 > BUDGET_S:
            break
    more = _latest(db, symbol, end, fb, True) is not None
    bars = [b for c in reversed(chunks) for b in c]
    return bars[-2 * MAX_COUNT:], more, approx, end


def _session(db_path, db, symbol, make, start, end, fb, max_rowid):
    """The bars of the session opening at `start` before `end` (its next one's open, or the chart's first bar), from
    its trades, or for volume, range and Renko bars missing some of them, from its minutes."""
    tv = db.execute("SELECT COALESCE(SUM(size), 0) FROM ticks WHERE symbol=? AND ts>=? AND ts<?",
                    (symbol, start, end)).fetchone()[0]
    mv = db.execute("SELECT COALESCE(SUM(v), 0) FROM bars_1m WHERE symbol=? AND t>=? AND t<?",
                    (symbol, start, end)).fetchone()[0] if fb else 0
    s = make(1_000_000)
    s.bulk = True
    approx = False
    if tv > 0 and (not fb or tv >= COMPLETE * mv):
        # to just past `end`: the last bar before the chart's first can have prints in its opening millisecond
        for tk in iter_ticks_range(db_path, symbol, start, end + 1, max_rowid):
            s.on_tick(tk)
    elif fb and mv > 0:
        approx = True
        for m in iter_bars_1m(db_path, symbol, start, end, db):
            for tk in minute_prints(symbol, m):
                s.on_tick(tk)
    s.end_bulk()
    return [b for b in s.bars if b.t < end], approx


def minute_prints(symbol: str, m) -> list[Tick]:
    """A 1-minute bar as prints: open, then its low and high (the high first on a down minute), then close, 15 seconds
    apart, each a quarter of its volume, split into buys, sells and unknown as its own volume is."""
    up = m.c >= m.o
    path = (m.o, m.l, m.h, m.c) if up else (m.o, m.h, m.l, m.c)
    v = max(int(m.v), 0)
    out = []
    for i, px in enumerate(path):
        size = v // 4 + (1 if i < v % 4 else 0)
        if size <= 0 and i:
            continue
        buy = size * m.bv // v if v else 0
        sell = size * m.sv // v if v else 0
        t = m.t + i * 15_000
        for side, sz in ((1, buy), (-1, sell), (0, size - buy - sell)):
            if sz > 0:
                out.append(Tick(symbol, t, px, sz, side))
        if size <= 0:   # an empty minute: its open still moves range and Renko bars
            out.append(Tick(symbol, t, px, 0, 0))
    return out
