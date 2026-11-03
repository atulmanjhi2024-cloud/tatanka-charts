"""Indicator alerts, as TradingView's: one series (an indicator's line on a minute, hour or daily chart, or the price)
crossing, crossing up, crossing down, above or below a value or another series.

The formulas are NinjaTrader 8's, the same as the chart's (ta.py, web/ta.js), so an alert fires on the value the chart
shows. Each (symbol, timeframe) with an indicator alert gets its own series, built from history as a chart's is, and
keeps running with no chart open. Checked when a bar closes (TradingView's "Once Per Bar Close"), or on every trade
with the forming bar's value, at most once a bar ("Once Per Bar"). A series is {ind, line, its settings} (any
indicator in ta.IND), or as saved before there were more: {ind: ema|sma, len}, {ind: rsi|rsi_avg, len, smooth},
{ind: macd|macd_avg|macd_diff, fast, slow, smooth}.

An alert's first look (after a restart, a gap fill, turning it on) steps its series through the closed bars, up to
3000. Long settings (Regression 1000, Ichimoku 500) make that seconds of work: whatever doesn't fit in a few
milliseconds on the event loop goes on in a worker process (workers.py), and the alert is checked once it is done. One
whose rest takes only a few more passes of the loop (Ichimoku at its defaults, HMA 100) is finished there instead."""
import asyncio
import itertools
import logging
import math
import time
from datetime import timedelta

from . import sessions, ta, workers
from .bars import calendar_bucket, make_series, parse_spec
from .gapfill import filling
from .models import Bar, Tick
from .ta import Ema, Macd, Rsi, Sma   # noqa: F401  (where they lived before ta.py)

log = logging.getLogger(__name__)
LINES = {"ema", "sma", "rsi", "rsi_avg", "macd", "macd_avg", "macd_diff"}   # the series saved before {ind, line}
DIRECTIONS = ("cross", "above", "below", "gt", "lt")
TWO_DECIMALS = {"rsi", "macd", "stoch", "stochrsi", "willr", "cci", "roc", "adx", "mfi", "rvol"}   # else the price's decimals
VOLUME = {"obv", "volma"}
INLINE_S = 0.02   # CPU time a first look may replay history on the event loop, one at a time; the rest later:
LOOP_REST_S = 5 * INLINE_S   # one no longer than that on the loop's next passes (INLINE_S each), a longer one in a worker


class _Now:
    """The forming bar as of a trade: what an intrabar check steps on."""

    def __init__(self, bar, price: float):
        self.t, self.o, self.v = bar.t, bar.o, bar.v
        self.h, self.l, self.c = max(bar.h, price), min(bar.l, price), price


class Line:
    """One series of an alert: the price, a value, or an indicator line; group: the instrument's holiday group."""

    def __init__(self, d: dict, group: str | None = None):
        self.d, kind = d, d.get("ind")
        self.run, self.idx = None, 0
        if "line" in d:
            spec = ta.IND[kind]
            self.run = ta.runner(kind, {k: d[k] for k, *_ in spec["params"]}, group)
            self.idx = spec["lines"].index(d["line"])
        elif kind in ("ema", "sma"):
            self.run = (Ema if kind == "ema" else Sma)(d["len"])
        elif kind in ("rsi", "rsi_avg"):
            self.run = Rsi(d["len"], d.get("smooth", 3))
        elif kind in ("macd", "macd_avg", "macd_diff"):
            self.run = Macd(d.get("fast", 12), d.get("slow", 26), d.get("smooth", 9))

    def step(self, bar, commit=True):
        kind = self.d.get("ind")
        if "value" in self.d:
            return self.d["value"]
        if kind == "price":
            return bar.c
        if "line" in self.d:
            return self.run.step(bar, commit)[self.idx]
        v = self.run.step(bar.c, commit)
        if kind in ("rsi", "rsi_avg"):
            return v[0 if kind == "rsi" else 1]
        if kind in ("macd", "macd_avg", "macd_diff"):
            return v[("macd", "macd_avg", "macd_diff").index(kind)]
        return v


def line_name(d: dict) -> str:
    if "value" in d:
        return f"{d['value']:.10g}"
    kind = d["ind"]
    if kind == "price":
        return "price"
    if "line" in d:   # "Bollinger(14,2) Upper", "ADX(14) +DI", "EMA(21)": the line's name when it isn't the indicator's
        spec = ta.IND[kind]
        label = ta.LABELS[kind][spec["lines"].index(d["line"])]
        args = ",".join(f"{d[k]:g}" if isinstance(d[k], (int, float)) else str(d[k]) for k, *_ in spec["params"])
        return f"{spec['name']}{f'({args})' if args else ''}{'' if label == spec['name'] else ' ' + label}"
    return line_name(as_line(d))


def as_line(d: dict) -> dict:
    """A series as saved before there were more indicators, as {ind, line, settings} (the chart's indLineNew)."""
    kind = d["ind"]
    if kind in ("ema", "sma"):
        return {"ind": kind, "line": "v", "len": d["len"]}
    if kind in ("rsi", "rsi_avg"):
        return {"ind": "rsi", "line": "rsi" if kind == "rsi" else "avg", "len": d["len"], "smooth": d.get("smooth", 3)}
    return {"ind": "macd", "line": {"macd": "macd", "macd_avg": "avg", "macd_diff": "diff"}[kind],
            "fast": d.get("fast", 12), "slow": d.get("slow", 26), "smooth": d.get("smooth", 9)}


def _int(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{what}: a whole number {lo}-{hi}")
    return v


def clean_line(d, allow_value: bool) -> dict:
    if not isinstance(d, dict):
        raise ValueError("a series is {ind, line, its settings}, {ind: price} or {value}")
    if "line" in d:
        kind = d.get("ind")
        spec = ta.IND.get(kind) if isinstance(kind, str) else None
        if spec is None:
            raise ValueError(f"unknown indicator {kind!r}")
        if d["line"] not in spec["lines"]:
            raise ValueError(f"{spec['name']} has no line {d['line']!r}")
        out = {"ind": kind, "line": d["line"]}
        for k, dv, lo, hi, kd in spec["params"]:
            out[k] = ta.clean_value(kd, d.get(k, dv), lo, hi, f"{spec['name']} {k}")
        if kind == "macd" and out["fast"] >= out["slow"]:
            raise ValueError("MACD fast must be shorter than slow")
        return out
    if "value" in d:
        v = d["value"]
        if not allow_value or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError("value: a number (the second series only)")
        return {"value": float(v)}
    kind = d.get("ind")
    if kind == "price":
        return {"ind": "price"}
    if kind not in LINES:
        raise ValueError(f"unknown indicator {kind!r}")
    if kind in ("ema", "sma"):
        return {"ind": kind, "len": _int(d.get("len"), 1, 1000, "length")}
    if kind in ("rsi", "rsi_avg"):
        return {"ind": kind, "len": _int(d.get("len", 14), 2, 500, "RSI length"), "smooth": _int(d.get("smooth", 3), 1, 100, "RSI smooth")}
    fast, slow = _int(d.get("fast", 12), 1, 500, "MACD fast"), _int(d.get("slow", 26), 2, 500, "MACD slow")
    if fast >= slow:
        raise ValueError("MACD fast must be shorter than slow")
    return {"ind": kind, "fast": fast, "slow": slow, "smooth": _int(d.get("smooth", 9), 1, 500, "MACD smooth")}


def clean_params(a, b, spec: str, on: str, direction: str, rth: bool = False) -> dict:
    sp = parse_spec(spec)
    if sp.kind != "time" or sp.cal in ("W", "M") or sp.size < 60 or (not sp.cal and sp.size % 60):
        raise ValueError("indicator alerts run on minute, hour or daily bars")
    if on not in ("close", "bar"):
        raise ValueError("on: close (bar close) or bar (intrabar, once a bar)")
    if direction not in DIRECTIONS:
        raise ValueError(f"direction: one of {', '.join(DIRECTIONS)}")
    a, b = clean_line(a, False), clean_line(b, True)
    if a == {"ind": "price"} and b.get("ind") == "price":
        raise ValueError("price against price is always equal")
    out = {"a": a, "b": b, "spec": sp.text, "on": on, "direction": direction}
    if rth:
        out["rth"] = True
    return out


def describe(p: dict) -> str:
    ops = {"cross": "crossing", "above": "crossing up", "below": "crossing down", "gt": "greater than", "lt": "less than"}
    return (f"{line_name(p['a'])} {ops[p['direction']]} {line_name(p['b'])} on {p['spec']}{' RTH' if p.get('rth') else ''}"
            f"{' bar close' if p['on'] == 'close' else ''}")


# ---- evaluation --------------------------------------------------------------------------------------------------
_GEN = itertools.count(1)   # watch versions: a rebuilt watch never matches an alert's state from before


def _key(a) -> tuple[str, str, bool]:
    """The bars an alert runs on: symbol, bar spec, regular hours only."""
    return a.symbol, a.params["spec"], bool(a.params.get("rth"))


class _Watch:
    def __init__(self, series):
        self.series = series
        self.ready = asyncio.Event()
        self.pending: list[Tick] = []
        self.version = 0   # set when its history is loaded


class _State:
    """An alert's two series, moved on through the closed bars, and what it last saw. budget: that long (this
    thread's CPU time) stepping through them now, the rest left in `todo` for replay(); None: all of them."""

    def __init__(self, p: dict, w: _Watch, last_fired: int | None = None, ended: bool = False,
                 budget: float | None = None):
        self.a, self.b = Line(p["a"], w.series.group), Line(p["b"], w.series.group)
        self.version, self.done_t = w.version, None
        self.pa = self.pb = None     # at the last closed bar
        self.fired_t = None          # the bar it last fired in
        self.catch_up = False        # back from a worker: the bars that closed meanwhile checked, intrabar too
        self.rate = 0.0              # CPU time a bar as replay() last stopped short: what one of the rest takes
        bars = list(w.series.bars)
        self.todo = bars if ended else bars[:-1]   # the closed bars still to step through (ended: the last one too, _ended)
        self.replay(budget)
        # an intrabar alert that fired in the forming bar before a restart or a reload: still once a bar (a holiday
        # halt's fire is in the next day's bar, stamped after it)
        if (p["on"] == "bar" and bars and last_fired is not None and w.series.in_hours(last_fired)
                and w.series.bucket(last_fired) == bars[-1].t):
            self.fired_t = bars[-1].t

    def replay(self, budget: float | None = None) -> bool:
        """Steps on through the closed bars still to do, for up to `budget` seconds (None: to the end): whether it is
        done. The thread's own CPU time, so a busy PC pausing it doesn't count. Stopped short, it notes what a bar took
        in the second half of its time: about what the rest's bars take (not all of it: a long setting's first bars
        cost less, its windows still filling, Ichimoku's next to nothing)."""
        t0, todo, half = time.thread_time(), self.todo, None
        for i, bar in enumerate(todo):
            if budget is not None and i % 16 == 0:
                spent = time.thread_time() - t0
                if spent >= budget:
                    j, s = half or (0, 0.0)
                    self.todo, self.rate = todo[i:], (spent - s) / max(1, i - j)
                    return False
                if half is None and spent >= budget / 2:
                    half = i, spent
            self.close(bar)
        self.todo = []
        return True

    def __getstate__(self):
        # to a worker process: the bars still to do as plain tuples, several times quicker to pickle than Bars
        return {**self.__dict__, "todo": [(b.t, b.o, b.h, b.l, b.c, b.v) for b in self.todo]}

    def __setstate__(self, d: dict):
        self.__dict__.update(d, todo=[Bar(*x) for x in d["todo"]])

    def close(self, bar) -> tuple:
        was = (self.pa, self.pb)
        self.pa, self.pb = self.a.step(bar), self.b.step(bar)
        self.done_t = bar.t
        return was, (self.pa, self.pb)


def _ended(series, ts: int) -> bool:
    """Whether a series' last bar has closed by `ts`, a time no bar opens: for an RTH-only series a trade after its
    regular hours (until the next RTH open) or a holiday's halt or early close in them, for a Globex one a moment the
    market is shut (16:00-17:00 CT, a weekend, a holiday; the watchdog checks then, as no trade comes). Not one stamped
    later (a holiday's short session counts toward the next day's bar), nor one of several days that goes on into the
    next session that trades."""
    if ts < series.bars[-1].t or ((not series.rth_only or series.in_hours(ts)) and sessions.is_open(ts, series.group)):
        return False
    if not series.calendar or series.spec.text == "1D" or series.bucket(ts) > series.bars[-1].t:
        return True
    # several days: also once its last day's regular hours are over, when the next session that trades (past a
    # weekend or a closed day) starts another group
    start = series.clock.locate(ts)
    if series.rth_only and ts < sessions.first_closure(*series.rth_clock.window(sessions.to_ct(start) + timedelta(hours=7)),
                                                       series.group):   # its trade date's RTH to come (to an early close)
        return False
    t = sessions.next_session_start(start)
    for _ in range(10):
        if sessions.is_open(t + 60_000, series.group) and not sessions._no_session(t + 60_000):
            break
        t = sessions.next_session_start(t)
    return calendar_bucket(sessions.counts_toward(t, series.group), series.calendar, series.spec.mult) > series.bars[-1].t


def met(direction: str, was: tuple, now: tuple) -> str | None:
    """'above' or 'below' when the condition holds (crossed, or is past), else None."""
    a, b = now
    if a is None or b is None:
        return None
    if direction in ("gt", "lt"):
        return ("above" if a > b else None) if direction == "gt" else ("below" if a < b else None)
    pa, pb = was
    if pa is None or pb is None:
        return None
    d0, d1 = pa - pb, a - b
    if d0 <= 0 < d1 and direction in ("cross", "above"):
        return "above"
    if d0 >= 0 > d1 and direction in ("cross", "below"):
        return "below"
    return None


class IndicatorAlerts:
    def __init__(self, market):
        self.m = market
        self.watches: dict[tuple[str, str, bool], _Watch] = {}
        self.states: dict[int, _State] = {}
        self.replaying: dict[int, _State] = {}   # first looks whose history goes on later: not checked until done
        self._looked = None                      # the trade the last first look on the event loop was made on
        # first looks put off to the loop's next passes: alert id -> its trade, or the state of one a little short
        self._later: dict[int, Tick | _State] = {}

    def ensure(self):
        """Loads the series the enabled indicator alerts need (on the event loop, after alerts change)."""
        wanted = {_key(a) for a in self.m.alerts.alerts.values() if a.kind == "ind" and a.enabled}
        for key in wanted:
            if key in self.watches or key[0] not in self.m.instruments:
                continue
            inst = self.m.instruments[key[0]]
            w = self.watches[key] = _Watch(make_series(parse_spec(key[1]), inst.tick_size, inst.decimals, 3000, inst.rth,
                                                       studies=False, rth_only=key[2], group=inst.calendar))
            # the cut between stored rows and queued ticks, made now: a trade arriving before the load starts must
            # land on one side only
            from .market import now_ms
            m, sym = self.m, key[0]
            w.pending = m.flush_for_read(sym)
            cutoff = m.store.max_tick_rowid()
            last = m.m1[sym].last
            split = last.t if last and sym in m.m1_ticked else now_ms() // 60_000 * 60_000
            if w.pending:   # not written yet (a full disk): stored minutes are complete only before them
                split = min(split, min(tk.ts for tk in w.pending) // 60_000 * 60_000)
            asyncio.get_running_loop().create_task(self._load(key, w, cutoff, split))
        for key in list(self.watches):
            if key not in wanted:
                del self.watches[key]
        # an alert turned off, or fired and done, starts afresh when turned on: no crossing from while it was off
        live = {a.id for a in self.m.alerts.alerts.values() if a.kind == "ind" and a.enabled}
        self.states = {i: s for i, s in self.states.items() if i in live}
        self.replaying = {i: s for i, s in self.replaying.items() if i in live}   # what comes back for them is dropped

    async def _load(self, key, w: _Watch, cutoff: int, split: int):
        from .market import build_history, now_ms
        m, (sym, spec, _) = self.m, key
        try:
            t0 = time.perf_counter()
            await asyncio.to_thread(build_history, m.store.path, w.series, sym, parse_spec(spec), cutoff, split, now_ms())
            await m._older_days(sym, w.series, parse_spec(spec))   # little stored: daily bars from Yahoo, as the chart
            for tk in w.pending:
                w.series.on_tick(tk)
            log.info("indicator alerts for %s %s ready in %.1fs", sym, spec, time.perf_counter() - t0)
        except Exception:
            log.exception("indicator alert history failed for %s %s", sym, spec)
        finally:
            w.pending = []
            w.version = next(_GEN)
            w.ready.set()

    def on_tick(self, tk: Tick):
        for (sym, *_), w in self.watches.items():
            if sym != tk.symbol:
                continue
            if w.ready.is_set():
                w.series.on_tick(tk)
            else:
                w.pending.append(tk)

    def check(self, a, tk: Tick) -> str | None:
        """The alert's message if it fires on this trade, else None."""
        p = a.params
        w = self.watches.get(_key(a))
        if w is None or not w.ready.is_set() or not w.series.bars or filling(self.m, a.symbol):
            return None
        st = self.states.get(a.id)
        ended = _ended(w.series, tk.ts)   # a session's last bar closes with its hours (an RTH chart's with its regular hours)
        if st is None or st.version != w.version:
            if (busy := self.replaying.get(a.id)) is not None and busy.version == w.version:
                return None   # its history still replays in a worker
            budget = self._budget(tk)
            if budget == 0:   # this trade made a first look already: this one right after it, with time of its own
                self._put_off(a, tk)
                return None
            st = _State(p, w, a.last_fired, ended, budget)
            self.replaying.pop(a.id, None)   # one of older bars (before a gap fill): dropped when it comes back
            if st.todo:   # not done in its time: the rest later, so ticks, pages and other alerts go on meanwhile
                self.states.pop(a.id, None)
                self.replaying[a.id] = st
                if st.rate * len(st.todo) <= LOOP_REST_S:   # a few passes more (Ichimoku at its defaults, HMA 100):
                    self._put_off(a, st)   # on the loop's next passes, not behind long replays
                else:   # long settings: in a worker
                    asyncio.get_running_loop().create_task(self._replay(a.id, st))
            else:
                self.states[a.id] = st
            return None   # the first look never fires
        bars, out = w.series.bars, None
        closed = []   # bars that closed since the last trade (normally none or one), from the newest back
        for i in range(len(bars) - (1 if ended else 2), -1, -1):
            if st.done_t is not None and bars[i].t <= st.done_t:
                break
            closed.append(bars[i])
        for bar in reversed(closed):
            was, now = st.close(bar)
            # right after a worker's replay an intrabar alert checks them too: no trade saw a crossing whose bar closed
            # while it replayed. Only those of this trade's session: not the last session's, an hour or a weekend late
            catch_up = st.catch_up and bar.t >= sessions.session_start(tk.ts)
            if (p["on"] == "close" or catch_up) and st.fired_t != bar.t and (side := met(p["direction"], was, now)):
                st.fired_t = bar.t
                out = self._message(a, side, now, bar)
        st.catch_up = False
        # the forming bar against the last closed one, as TradingView's crossover; an RTH chart's bars take no
        # overnight trade, so neither does its intrabar check
        if p["on"] == "bar" and w.series.in_hours(tk.ts):
            forming = bars[-1]
            cur = _Now(forming, tk.price)
            now = (st.a.step(cur, False), st.b.step(cur, False))
            was = (st.pa, st.pb)
            if st.fired_t != forming.t and (side := met(p["direction"], was, now)):
                st.fired_t = forming.t
                out = self._message(a, side, now, forming)
        return out

    def _budget(self, tk: Tick) -> float | None:
        """How long a first look may replay history on the event loop now: INLINE_S for a trade's first, 0 for the
        others (AlertEngine checks every alert on the same trade): those are put off. None with no event loop (a
        script, a test calling check()): all of it, there and then."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        if self._looked is tk:
            return 0.0
        self._looked = tk
        return INLINE_S

    def _put_off(self, a, item: Tick | _State):
        """A first look its trade had no time left for: made on the event loop's next passes, one a pass, each with
        the whole INLINE_S. So a symbol's first looks are all made right after its first trade (about 20 ms each),
        however far apart its trades are (none steps over the bars of the trades to come). Only one whose rest takes
        longer than LOOP_REST_S leaves it to a worker: one a little over (Stochastics on a busy PC, Ichimoku at its
        defaults, HMA 100) is queued again with its _State, its rest on the next passes, not seconds behind long
        replays there. Queued once, in its place."""
        if not self._later:
            asyncio.get_running_loop().call_soon(self._look_later)
        self._later[a.id] = item

    def _look_later(self):
        """The next first look put off, as on a trade of its own, or the rest of one a little short; the others on
        the passes after it. Only a first look, which never fires: one paused or deleted meanwhile, or made by a trade
        since, is passed over, and a rest whose bars loaded again too."""
        while self._later:
            aid, item = next(iter(self._later.items()))   # queued while it is made: one a little short stays first
            a = self.m.alerts.alerts.get(aid)   # paused or deleted meanwhile: none
            w = self.watches.get(_key(a)) if a is not None and a.kind == "ind" and a.enabled else None
            if isinstance(item, _State):   # the rest of one, with the whole INLINE_S again
                if w is None or self.replaying.get(aid) is not item or item.version != w.version:
                    del self._later[aid]
                    continue
                if item.replay(INLINE_S):   # done (else the rest on the next pass): checked from the next trade on,
                    del self._later[aid], self.replaying[aid]   # the bars that closed meanwhile too
                    item.catch_up = True
                    self.states[aid] = item
                break
            if w is None or any((s := d.get(aid)) is not None and s.version == w.version
                                for d in (self.states, self.replaying)):   # a trade made it since
                del self._later[aid]
                continue
            try:
                self.check(a, Tick(item.symbol, item.ts, item.price, 0, 0))   # a Tick of its own: the whole INLINE_S
            except Exception:   # one bad alert must not stop the others
                log.exception("alert %s failed", aid)
            if self._later.get(aid) is item:   # not a little short
                del self._later[aid]
            break
        if self._later:
            asyncio.get_running_loop().call_soon(self._look_later)

    async def _replay(self, aid: int, st: _State):
        """The rest of a first look's replay, in a worker process. Kept unless the alert was paused or deleted
        meanwhile, or a newer one started (its bars loaded again: a gap fill); bars that closed meanwhile are checked
        on the next trade, an intrabar alert's too (those of that trade's session)."""
        t0 = time.perf_counter()
        try:
            done = await workers.run("study", workers.replay_alert, st)
        except Exception:   # left as it is: tried again when its bars load again or it is turned on, not on every trade
            log.exception("indicator alert %s history failed", aid)
            return
        if self.replaying.get(aid) is st:
            del self.replaying[aid]
            done.catch_up = True
            self.states[aid] = done
            log.info("indicator alert %s ready in %.1fs", aid, time.perf_counter() - t0)

    def _message(self, a, side: str, now: tuple, bar) -> str:
        p, dec = a.params, self.m.instruments[a.symbol].decimals

        def f(d, v):
            kind = d.get("ind", "")
            if kind in VOLUME:
                return f"{v:.0f}"
            return f"{v:.2f}" if kind in TWO_DECIMALS or kind.startswith(("rsi", "macd")) else f"{v:.{dec}f}"
        verb = {"gt": "above", "lt": "below"}.get(p["direction"], f"crossed {side}")
        return (f"{a.symbol} {p['spec']}: {line_name(p['a'])} {f(p['a'], now[0])} {verb} {line_name(p['b'])}"
                f"{'' if 'value' in p['b'] or p['b'].get('ind') == 'price' else ' ' + f(p['b'], now[1])}")
