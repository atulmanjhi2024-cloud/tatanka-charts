"""Serves the Tatanka Gap Analysis (chartsrv.gaps).

/api/gaps runs the engine over the chart's bars, rebuilt from stored 1-minute bars (tpo_service.chart_bars; an RTH
chart's of the regular hours only, from the RTH open), for the last N days, forming bar included, as the indicator
does with Calculate.OnPriceChange. The chart then applies fills from its own live bar until it asks again at the
next bar.

Alerts are opt-in per symbol: one 1-minute engine per alerting symbol, loaded from stored history and fed
every live price change (switched on from an RTH chart, only the regular hours' minutes, so gaps fill as there). Like
the indicator, history never alerts; only live updates do (those that come while it loads once it has; a new gap in
buffered trades with the next live one), and none while the stretch up to the live trades is still being filled
(gapfill.filling: the engine would compare them with the history before it)."""
import asyncio
import logging
import sqlite3
import time
from dataclasses import replace
from itertools import chain
from types import SimpleNamespace

from . import gaps, sessions
from .amt import price_str
from .amt_runner import ct
from .config import futures_catalog, load_instruments
from .gapfill import filling
from .models import Bar
from .sessions import session_start
from .store import iter_bars_1m
from .studies import RthClock
from .tpo_service import BarBuilder, chart_bars

log = logging.getLogger(__name__)
DAYS_DEFAULT = 90
DAYS_MAX = 180
ALERT_HISTORY_DAYS = 90
LIVE_BAR_AGE_MS = 6 * 60_000   # minutes older than this (buffered data catching up) never alert
SEEN = SimpleNamespace(answered=True)   # held for a new gap in buffered trades: sent with the next live update
SWITCHES = ("new", "fill")   # the alert switches (Alert on new gap, Alert on full gap fill); on where none is saved
SYMBOL_SWITCHES = "gap_symbol_switches"   # the store's: symbol -> its alert switches (every chart of it has them)


def run(s: dict, minutes: int, minute_bars, record: list | None = None, rth: tuple[str, str] | None = None):
    """Every chart bar through the forming one, once each (rth: an RTH chart's). Returns the engine and each bar's
    open by index."""
    eng = gaps.engine_for(s)
    opens: list[int] = []
    for i, (k, close, h, l, _, o, c) in enumerate(chart_bars(minute_bars, minutes, rth)):
        t = ct(close)
        if record is not None:
            record.append((i, t, o, h, l, c))
        eng.Update(i, t, o, h, l, c)
        opens.append(k)
    return eng, opens


def payload(eng: gaps.GapEngine, opens: list[int], minutes: int) -> dict:
    """Every stored gap with its bars as indices (for bar counts) and as bar open times (for drawing):
    st/ft are the start and fill bars, nt the bar before the next gap's start."""
    def at(i):
        return opens[i] if 0 <= i < len(opens) else None
    return {
        "version": gaps.VERSION, "minutes": minutes, "ready": True, "bar": eng.currentBar,
        "t": at(eng.currentBar), "consecutive": eng.consecutive,
        "gaps": [{"n": g.Ordinal, "dir": g.Direction, "open": g.OpenPrice, "settle": g.SettlePrice, "edge": g.FillEdge,
                  "sb": g.StartBar, "fb": g.FillBar, "st": at(g.StartBar), "ft": at(g.FillBar),
                  "nt": at(g.NextGapBar - 1) if g.NextGapBar >= 0 else None} for g in eng.History],
    }


def own_hours(s: dict, rth) -> dict:
    """The RTH times, written for ES's 08:30-15:00 CT, moved to the instrument's own regular hours (sessions.rth_mover)."""
    move = sessions.rth_mover(rth)
    return {**s, "RthStart": sessions.move_hhmmss(s["RthStart"], move), "RthEnd": sessions.move_hhmmss(s["RthEnd"], move)}


def check_hours(s: dict, instruments: dict):
    """The RTH times on each instrument's own hours: ValueError naming the first they make no session on, or another
    one (a time nearer the open moves with the open, one nearer the close with the close, so two can land on the same
    minute, 11:00-12:00 is 10:30-10:30 on CL's 08:00-13:30, or past each other, 09:50-09:30 on GC's 07:20-12:30: a
    session through the night, GapEngine.SessionKey's start > end)."""
    clock = lambda v: f"{v // 10000:02d}:{v // 100 % 100:02d}"   # noqa: E731
    for sym, inst in instruments.items():
        own = own_hours(s, inst.rth)
        try:
            gaps.engine_for(own)
        except ValueError:
            why = "the start and end to the same time"
        else:   # a day session entered stays one, an overnight one overnight
            if (own["RthStart"] < own["RthEnd"]) == (s["RthStart"] < s["RthEnd"]):
                continue
            why = f"the start and end past each other ({clock(own['RthStart'])}-{clock(own['RthEnd'])})"
        raise ValueError(f"RTH {clock(s['RthStart'])}-{clock(s['RthEnd'])} is no session on {sym}: its regular hours "
                         f"({'-'.join(inst.rth or ())} CT) move {why}. Enter other times.")


class GapWatch:
    """One symbol's alerting engine on the bars of the chart its alerts were switched on from (cfg["minutes"], its
    timeframe; cfg["rth"]: its regular hours only), as that chart's Gap table runs it: the same open, RTH close, size,
    fill and bars to fill. A config from before the timeframe was kept: 1-minute bars."""

    def __init__(self, cfg: dict, rth=None):
        self.cfg = cfg
        self.engine = gaps.engine_for(own_hours(gaps.settings({k: cfg[k] for k in gaps.DEFAULTS}), rth))
        self.hours = RthClock(rth) if rth and cfg.get("rth") else None   # a config without the flag: all hours
        self.bars = BarBuilder(max(1, int(cfg.get("minutes") or 1)), rth if self.hours else None)
        self.bar = -1
        self.t: int | None = None
        self.gap_t: int | None = None   # the minute its newest gap opened in
        self.ready = asyncio.Event()
        self.pending: list[tuple] = []   # (minute, live, hold) of each update that came while it loaded (on_minute)

    def add(self, b: Bar) -> gaps.GapUpdate | None:
        got = self.bars.add(b)      # None: outside the RTH chart's hours, or a late print for a minute already passed
        if got is None:
            return None
        self.bar, _, close, o, h, low, c = got
        self.t = b.t
        u = self.engine.Update(self.bar, ct(close), o, h, low, c)   # the chart bar so far, as NinjaTrader on each price
        if u.NewGap is not None:
            self.gap_t = b.t
        return u


def _load(db_path, watch: GapWatch, sym: str, split: int):
    for b in iter_bars_1m(db_path, sym, split - ALERT_HISTORY_DAYS * 86_400_000, split):
        watch.add(b)


class GapService:
    def __init__(self, market):
        self.m = market
        self.alert_cfg: dict = market.store.get_setting("gap_alerts", {})   # symbol -> {on, new, fill, rth, RthStart, ...}
        # The alert switches (Alert on new gap, Alert on full gap fill) are each symbol's, the same on every chart of
        # it (symbol_switches), and its config has them: unticking one on an NQ chart turns it off for NQ's alerts and
        # leaves ES's as they are. The RTH times and minimum gap size are in each symbol's config, from the chart of it
        # that last switched its alerts or saved its Gap settings. The first start since the switches are each symbol's
        # copies the ones every chart had to every symbol, once (_migrate); those the first start before that turned
        # off wait in notice for the next page to say so (told()).
        self.notice: list = market.store.get_setting("gap_alert_notice", None) or []
        per = market.store.get_setting(SYMBOL_SWITCHES, None)
        if not isinstance(per, dict):
            per = self._migrate(market.store.get_setting("gap_alert_switches", None))
        self.symbol_switches: dict = {sym: {k: sw[k] for k in SWITCHES if k in sw}
                                      for sym, sw in per.items() if isinstance(sw, dict)}
        for sym, cfg in self.alert_cfg.items():
            self.alert_cfg[sym] = {**cfg, **self.switches_of(sym)}
        self.watches: dict[str, GapWatch] = {}
        self.held: dict = {}   # symbol -> the fill window of the stretch before the live trades a new gap came during
                               # (SEEN: it came in buffered trades)
        self.refused: dict[str, str] = {}   # symbol -> why its alerts were not started (start())

    def _migrate(self, every: dict | None) -> dict:
        """The first start since the alert switches are each symbol's: the ones every chart had (every, as stored)
        become every symbol's, each instrument's and each one's whose alerts are kept for when it is back on
        Instruments, so an install keeps what it has. Once: from then on the symbols' are stored (SYMBOL_SWITCHES), even
        with none changed. From a version before they were every chart's (none stored) they are worked out first, as
        that version did: off where a symbol whose alerts are on had it off (it silences, never turns one on). Each
        config is as last saved, by the chart that switched it or saved Gap settings, its own over the starting ones;
        with none on, every stored one; with none stored, the starting Gap settings. Those it turns off wait in notice
        for the next page to say so."""
        store = self.m.store
        if not every:
            start = (store.get_setting("ind_settings", {}) or {}).get("gap", {})
            saved = [cfg for cfg in self.alert_cfg.values() if cfg.get("on")] or list(self.alert_cfg.values()) or [
                {k: start.get(name) is not False for k, name in (("new", "AlertOnNewGap"), ("fill", "AlertOnGapFill"))}]
            every = {k: all(cfg.get(k, True) for cfg in saved) for k in SWITCHES}
            self.notice = [k for k, on in every.items() if not on]
            store.set_setting("gap_alert_notice", self.notice)
        every = {k: every.get(k, True) is not False for k in SWITCHES}
        per = {sym: dict(every) for sym in sorted({*self.m.instruments, *self.alert_cfg})}
        store.set_setting(SYMBOL_SWITCHES, per)
        if per:
            log.info("gap alert switches are each symbol's now: %s copied to %s",
                     ", ".join(f"{k} {'on' if on else 'off'}" for k, on in every.items()), ", ".join(per))
        return per

    def switches_of(self, sym: str) -> dict:
        """The symbol's alert switches, {new, fill}: every chart of it has them (on where none is saved: one added
        later)."""
        own = self.symbol_switches.get(sym, {})
        return {k: own.get(k, True) for k in SWITCHES}

    def switches(self) -> dict:
        """Every instrument's alert switches (GET /api/gaps/alerts and what every page hears): symbol -> {new, fill}."""
        return {sym: self.switches_of(sym) for sym in self.m.instruments}

    # --- charts ---------------------------------------------------------------------------------------
    async def get(self, sym: str, minutes: int, s: dict, days: int, rth: bool = False, until: int | None = None) -> dict:
        """rth: an RTH chart's gaps (ignored for an instrument without regular hours). until: bar replay's clock, the
        gaps as they stood then."""
        split, live = self.m.recent_minutes(sym) if until is None else self.m.recent_minutes(sym, until)
        start = session_start(int(time.time() * 1000) if until is None else until // 60_000 * 60_000) - days * 86_400_000
        db = self.m.store.path
        hours = self.m.instruments[sym].rth if rth else None

        def work():
            eng, opens = run(s, minutes, chain(iter_bars_1m(db, sym, start, split), live), rth=hours)
            # the hours it ran on: the chart's RTH times moved to the instrument's own (CL's 0800-1330), as its info says
            return {**payload(eng, opens, minutes), "hours": f"{s['RthStart'] // 100:04d}-{s['RthEnd'] // 100:04d}"}
        return await asyncio.to_thread(work)

    # --- alerts -----------------------------------------------------------------------------------------
    def set_alerts(self, sym: str, cfg: dict):
        """cfg: on, rth and the chart's RthStart, RthEnd and MinimumGapPoints (and minutes, its timeframe: the alerts are
        on its bars); the alert switches are the symbol's."""
        cfg = {k: v for k, v in cfg.items() if v is not None}   # minutes not given: 1-minute bars, as before
        s = gaps.settings({k: cfg[k] for k in gaps.DEFAULTS})     # raises ValueError on bad times...
        if cfg.get("on"):   # ...or on ones its own hours make no session of (switching off: never refused)
            check_hours(s, {sym: self.m.instruments[sym]})
        self.alert_cfg[sym] = {**cfg, **self.switches_of(sym)}
        self.m.store.set_setting("gap_alerts", self.alert_cfg)
        self.watches.pop(sym, None)
        self.start()

    def follow(self, s: dict, sym: str | None = None):
        """A chart's Gap settings saved (s: what it gives of new, fill, RthStart, RthEnd, MinimumGapPoints; the alert
        switches new and fill only when changed), for its symbol's alerts (sym: every chart of it shares the switches,
        and its config takes the times and size), or with none given every symbol's: the switches of every instrument
        and every one kept, the rest in every config. A config keeps its own on and rth, as set on whichever chart or
        device last switched it, and what s leaves out. A symbol no longer an instrument (removed in Connections >
        Instruments) takes them too with none given, for when it is back; it has no watch."""
        if ("RthStart" in s) != ("RthEnd" in s):
            raise ValueError("Give RthStart and RthEnd together.")
        # raises ValueError on bad times, or on ones the symbol's own hours make no session of (with no symbol: any
        # instrument's, or one's off Instruments whose alerts are kept on, at the hours it would be back with): nothing
        # stored
        hours = {sym: self.m.instruments[sym]} if sym else {**self.m.instruments, **self._kept()}
        check_hours(gaps.settings({k: s[k] for k in gaps.DEFAULTS if k in s}), hours)
        syms = [sym] if sym else sorted({*self.m.instruments, *self.symbol_switches, *self.alert_cfg})
        sw = {k: s[k] for k in SWITCHES if k in s}
        per = {x: {**self.switches_of(x), **sw} for x in syms if sw}
        if any(v != self.symbol_switches.get(x) for x, v in per.items()):
            self.symbol_switches = {**self.symbol_switches, **per}
            self.m.store.set_setting(SYMBOL_SWITCHES, self.symbol_switches)
        changed = [x for x in syms if x in self.alert_cfg and {**self.alert_cfg[x], **s} != self.alert_cfg[x]]
        for x in changed:
            self.alert_cfg[x] = {**self.alert_cfg[x], **s}
            self.watches.pop(x, None)
        if changed:
            self.m.store.set_setting("gap_alerts", self.alert_cfg)
            self.start()

    def _kept(self) -> dict:
        """The instruments off Instruments whose alerts are kept on, with the regular hours they would be back with: the
        saved list's (the next start's), else the futures catalog's. One in neither is checked when it is back."""
        off = [sym for sym, cfg in self.alert_cfg.items() if cfg.get("on") and sym not in self.m.instruments]
        if not off:
            return {}
        rth = {r["symbol"]: r.get("rth") for r in futures_catalog(self.m.s.config_dir)}
        rth.update({sym: i.rth for sym, i in load_instruments(self.m.s.config_dir, self.m.store).items()})
        return {sym: SimpleNamespace(rth=rth[sym]) for sym in off if sym in rth}

    def told(self) -> list:
        """The alert switches the first start turned off (notice), handed once: to the page that says so, none after."""
        out, self.notice = self.notice, []
        if out:
            self.m.store.set_setting("gap_alert_notice", [])
        return out

    def shown(self) -> dict:
        """The instruments' configs (one removed in Connections > Instruments is kept for when it is back, unused); one
        whose alerts start() refused as off, with why (the Studies panel shows it)."""
        out = {sym: cfg for sym, cfg in self.alert_cfg.items() if sym in self.m.instruments}
        for sym, why in self.refused.items():
            if out.get(sym, {}).get("on") and sym not in self.watches:
                out[sym] = {**out[sym], "on": False, "error": why}
        return out

    def start(self):
        for sym, cfg in self.alert_cfg.items():
            if cfg.get("on") and sym in self.m.instruments and sym not in self.watches:
                inst = self.m.instruments[sym]
                # times its own hours make no session of (stored before they were checked, or kept for it while it was
                # off Instruments): left off, the others run
                try:
                    check_hours(cfg, {sym: inst})
                    w = GapWatch(cfg, inst.rth)
                except ValueError as e:
                    log.warning("gap alerts for %s not started: %s", sym, e)
                    self.refused[sym] = str(e)
                    continue
                self.refused.pop(sym, None)
                self.watches[sym] = w
                # the cut between stored and in-memory minutes, made now: every update after it goes to w.pending and
                # alerts once loaded (cut when the load started, those before it were taken as history)
                asyncio.get_running_loop().create_task(self._fill(sym, w, *self.m.recent_minutes(sym)))

    async def _fill(self, sym: str, w: GapWatch, split: int, recent: list[Bar]):
        try:
            t0 = time.perf_counter()
            await asyncio.to_thread(_load, self.m.store.path, w, sym, split)
            for b in recent:
                w.add(b)
            # the updates that came while it loaded alert now, as they would have then (a gap opens or fills once).
            # Each watch alerts its own: one replaced meanwhile (a gap fill, a save) too while the symbol's alerts are
            # on, as the one replacing it has them as history
            on = sym in self.watches
            for b, live, hold in w.pending:
                u = w.add(b)
                if on:
                    self._alert(sym, w, u, b, live, hold)
            w.pending = []
            log.info("gap alerts for %s ready in %.1fs (%d gaps stored)", sym, time.perf_counter() - t0,
                     len(w.engine.History))
        except Exception:
            log.exception("gap alert history failed for %s", sym)
            if self.watches.get(sym) is w:
                del self.watches[sym]
        finally:
            w.ready.set()

    def on_minute(self, sym: str, b: Bar, live: bool):
        w = self.watches.get(sym)
        if w is None:
            return
        hold = self.m.gapfill.windows[sym] if filling(self.m, sym) else None   # the fill holding its alerts
        if not w.ready.is_set():
            w.pending.append((replace(b), live, hold))
            return
        self._alert(sym, w, w.add(b), b, live, hold)

    def _alert(self, sym: str, w: GapWatch, u: gaps.GapUpdate | None, b: Bar, live: bool, hold):
        """What an update of the minute b sends; hold: the gap fill window holding the symbol's alerts when it came."""
        if u is None or time.time() * 1000 - b.t > LIVE_BAR_AGE_MS:
            return
        # sent with the next live update, if its minute is still live: once the fill is done (its watch loaded again
        # with it); one in buffered trades (the add-on's, sent when the server is back), which alert nothing themselves
        if not live or hold is not None:
            if u.NewGap is not None:
                self.held[sym] = hold if hold is not None else SEEN
            elif hold is not None and sym in self.held:   # the stretch's rest, asked after its trades: it waits for that too
                self.held[sym] = hold
            return
        held = self.held.pop(sym, None)
        # when the stretch's history failed or never came, only if it had no regular-hours minute (overnight, a weekend):
        # the gap is measured from the last RTH close before it
        if held is not None and u.NewGap is None and w.gap_t is not None \
                and time.time() * 1000 - w.gap_t <= LIVE_BAR_AGE_MS \
                and (held.answered or not self._rth_in(sym, w, held.start)):
            u.NewGap = g = w.engine.History[-1]
            if g.Filled and g not in (u.FilledGaps or ()):   # filled meanwhile: its fill goes with it
                u.FilledGaps = [*(u.FilledGaps or ()), g]
        tick = self.m.instruments[sym].tick_size
        px = lambda v: price_str(v, sym, tick)   # noqa: E731
        if w.cfg.get("new", True) and u.NewGap is not None:
            g = u.NewGap
            self._emit(sym, b.c, f"[{sym}] GAP {'UP' if g.Direction > 0 else 'DOWN'} {px(g.Size)} pts | "
                                 f"Open: {px(g.OpenPrice)} | RTH close: {px(g.SettlePrice)}")
        if w.cfg.get("fill", True) and u.FilledGaps:
            for g in u.FilledGaps:
                self._emit(sym, b.c, f"[{sym}] GAP {'UP' if g.Direction > 0 else 'DOWN'} FILLED | {px(g.Size)} pts "
                                     f"in {g.BarsToFill} bars")

    def _rth_in(self, sym: str, w: GapWatch, start: int) -> bool:
        """Whether a regular-hours minute the watch takes, the market open, came from `start` to its newest gap's:
        one missed there moves the RTH close the gap is measured from (a stretch overnight or over a weekend has none)."""
        group = getattr(self.m.instruments.get(sym), "calendar", None)
        for k in range(start // 60_000 * 60_000, w.gap_t, 60_000):
            if (w.hours is None or w.hours.day(k) is not None) and w.engine.SessionKey(ct(k + 60_000)) != gaps.DMIN \
                    and sessions.is_open(k, group):
                return True
        return False

    def _emit(self, sym: str, price: float, text: str):
        ev = {"alert_id": None, "symbol": sym, "ts": int(time.time() * 1000), "price": price, "message": text}
        try:
            ev["id"] = self.m.store.add_event(ev)
        except sqlite3.Error as e:   # the database can't be written (a full disk, another program's lock): sent all the same
            log.warning("gap alert for %s not saved: %s", sym, e)
            ev["id"] = None
        self.m.broadcast({"type": "alert", "event": ev})
        asyncio.get_running_loop().create_task(self.m._deliver(ev))

    def health(self) -> dict:
        return {s: {"ready": w.ready.is_set(), "gaps": len(w.engine.History), "active": len(w.engine.Active)}
                for s, w in self.watches.items()}
