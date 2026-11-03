"""Server-side alert evaluation, run on every live tick.

price: TradingView's conditions on `price`: cross, above (crossing up), below (crossing down), gt and lt (greater
       or less than: true while price is past it), enter and exit (a channel `price`..`price2`). One-shot, or with
       repeat at most once a minute. An alert created while the last print sits exactly on its price fires on the
       first move off it. Any alert with params.expires (epoch ms) switches itself off then.
line:  the same for a drawn line, ray or trend line (drawings.line_price), whose level can move with market time (or
       as the chart its alert was switched on from lays the line out: drawings.LAYOUT).
ind:   an indicator series crossing, or above or below, a value or another series (ind_alerts), on bar close or
       intrabar once a bar.
study: fires when price trades within `tolerance_ticks` of a study level (session or RTH VWAP and its sigma
       bands, a session profile's POC, VAH or VAL), re-armed like level alerts. The level comes from `studies`.
level: fires when price trades within `tolerance_ticks` of one of the symbol's daily levels (levels.py: the Daily
       levels study's). Each level then stays quiet until price moves `rearm_ticks` beyond that band, so chop around a
       level fires once, not on every print."""
import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable

from . import drawings, study_alerts
from .config import Instrument
from .levels import Levels
from .models import Tick
from .store import Store

log = logging.getLogger(__name__)
REPEAT_COOLDOWN_MS = 60_000


@dataclass
class Alert:
    id: int
    symbol: str
    kind: str
    params: dict
    enabled: bool
    repeat: bool
    note: str
    created: int
    last_fired: int | None

    def to_dict(self):
        return dict(self.__dict__)


class AlertEngine:
    def __init__(self, store: Store, instruments: dict[str, Instrument], levels: Levels | None,
                 on_fire: Callable[[dict], None]):
        self.store = store
        self.instruments = instruments
        self.levels = levels
        self.on_fire = on_fire
        self.alerts: dict[int, Alert] = {}
        self.last_px: dict[str, float] = {}
        self.last_ts: dict[str, int] = {}     # ...and its time: where a sloped line was then
        self.armed: dict[tuple[int, str], bool] = {}
        self.seen: set[int] = set()        # price and line alerts evaluated at least once since (re)enabled
        self.at_price: set[int] = set()    # ...whose first look found the last print exactly on their price
        self.studies: Callable[[str, dict, int], tuple[float, int] | None] | None = None   # study alert levels (market)
        self.study_session: dict[int, int] = {}   # study alerts: the session their armed state belongs to
        self.indicators: Callable[[Alert, Tick], str | None] | None = None    # indicator alerts (ind_alerts.check)
        self.on_change: Callable[[], None] | None = None   # an alert switched itself off (expired)
        self.reload()

    def reload(self):
        old = self.alerts
        self.alerts = {r["id"]: Alert(**r) for r in self.store.list_alerts()}
        # turned back on or edited, an alert starts afresh with a first look: nothing kept from while it was off
        steady = {i for i, a in self.alerts.items() if a.enabled and i in old and old[i].enabled and old[i].params == a.params}
        self.armed = {k: v for k, v in self.armed.items() if k[0] in steady}
        self.study_session = {k: v for k, v in self.study_session.items() if k in steady}
        self.seen &= steady
        self.at_price &= steady

    def list(self) -> list[dict]:
        return [a.to_dict() for a in self.alerts.values()]

    def reset_levels(self, symbol: str):
        """New levels: forget arm state so each new level starts fresh."""
        ids = {a.id for a in self.alerts.values() if a.symbol == symbol and a.kind == "level"}
        self.armed = {k: v for k, v in self.armed.items() if k[0] not in ids}

    def rolled(self, symbol: str):
        """The feed now sends another contract for `symbol` (market.on_contract): prices jump by the spread between the
        two, through no line or level between. Its alerts start afresh, as at a first start: the new contract's first
        trade is only where they look from."""
        self.last_px.pop(symbol, None)
        self.last_ts.pop(symbol, None)
        ids = {i for i, a in self.alerts.items() if a.symbol == symbol}
        self.armed = {k: v for k, v in self.armed.items() if k[0] not in ids}
        self.study_session = {k: v for k, v in self.study_session.items() if k not in ids}
        self.seen -= ids
        self.at_price -= ids

    def on_tick(self, tk: Tick):
        prev = self.last_px.get(tk.symbol)
        prev_ts = self.last_ts.get(tk.symbol, tk.ts)
        self.last_px[tk.symbol] = tk.price
        self.last_ts[tk.symbol] = tk.ts
        for a in list(self.alerts.values()):
            if a.symbol != tk.symbol or not a.enabled:
                continue
            exp = a.params.get("expires")
            if exp and tk.ts >= exp:
                a.enabled = False
                try:
                    self.store.update_alert(a.id, enabled=False)
                except sqlite3.Error as e:   # not written (a full disk): off all the same, and the trades go on
                    log.warning("alert %s expired, not saved: %s", a.id, e)
                if self.on_change:
                    self.on_change()
                continue
            if a.kind in ("price", "line") and a.repeat and a.last_fired and tk.ts - a.last_fired < REPEAT_COOLDOWN_MS:
                continue
            try:
                if a.kind == "price":
                    self._check_price(a, prev, tk)
                elif a.kind == "level":
                    self._check_levels(a, prev, tk)
                elif a.kind == "line":
                    self._check_line(a, prev, prev_ts, tk)
                elif a.kind == "study":
                    self._check_study(a, prev, tk)
                elif a.kind == "ind" and self.indicators:
                    msg = self.indicators(a, tk)
                    if msg:
                        self._fire(a, tk, msg)
            except Exception:  # one bad alert must not stop the others
                log.exception("alert %s failed", a.id)

    def on_shut(self, tk: Tick):
        """The market is shut (16:00-17:00 CT, a weekend, a holiday): no trade will close the session's last bar, so
        bar-close indicator alerts check it now. tk: this moment, at the last trade's price."""
        for a in list(self.alerts.values()):
            if a.symbol != tk.symbol or not a.enabled or a.kind != "ind" or a.params.get("on") != "close" or not self.indicators:
                continue
            exp = a.params.get("expires")
            if exp and tk.ts >= exp:   # switched off on the next trade
                continue
            try:
                msg = self.indicators(a, tk)
                if msg:
                    self._fire(a, tk, msg)
            except Exception:  # one bad alert must not stop the others
                log.exception("alert %s failed", a.id)

    def _check_price(self, a: Alert, prev: float | None, tk: Tick):
        if prev is None:
            return
        p, d, px = a.params["price"], a.params.get("direction", "cross"), tk.price
        if d in ("gt", "lt"):
            if px > p if d == "gt" else px < p:
                self._fire(a, tk, f"{a.symbol} {'above' if d == 'gt' else 'below'} {self._fmt(a.symbol, p)}")
            return
        if d in ("enter", "exit"):
            lo, hi = p, a.params["price2"]
            was, now = lo <= prev <= hi, lo <= px <= hi
            if (d == "enter" and now and not was) or (d == "exit" and was and not now):
                self._fire(a, tk, f"{a.symbol} {'entered' if d == 'enter' else 'left'} {self._fmt(a.symbol, lo)}–{self._fmt(a.symbol, hi)}")
            return
        if a.id not in self.seen:
            self.seen.add(a.id)
            if prev == p:
                self.at_price.add(a.id)
        if a.id in self.at_price:          # created on the price: the first move off it is the cross
            if px == p:
                return
            self.at_price.discard(a.id)
            up, down = px > p, px < p
        else:
            up, down = prev < p <= px, prev > p >= px
        if (up and d in ("cross", "above")) or (down and d in ("cross", "below")):
            word = "crossed above" if up else "crossed below"
            self._fire(a, tk, f"{a.symbol} {word} {self._fmt(a.symbol, p)}")

    def _check_line(self, a: Alert, prev: float | None, prev_ts: int, tk: Tick):
        if prev is None:
            return
        group = self.instruments[a.symbol].calendar   # its holidays: the hours its bars, and so the line, skip
        lvl = drawings.line_price(a.params, tk.ts, group)
        if lvl is None:
            return
        # each print against the line's level at its own time, so a sloped line moving through price crosses too
        then = drawings.line_price(a.params, prev_ts, group)
        was, now = prev - (lvl if then is None else then), tk.price - lvl
        d = a.params.get("direction", "cross")
        if a.id not in self.seen:
            self.seen.add(a.id)
            if was == 0:
                self.at_price.add(a.id)
        if a.id in self.at_price:          # set on the last print: the first move off the line is the cross
            if now == 0:
                return
            self.at_price.discard(a.id)
            up, down = now > 0, now < 0
        else:
            up, down = was < 0 <= now, was > 0 >= now
        if (up and d in ("cross", "above")) or (down and d in ("cross", "below")):
            what = {"hline": "line", "hray": "ray", "trend": "trend line"}.get(a.params.get("type"), "line")
            self._fire(a, tk, f"{a.symbol} crossed {'above' if up else 'below'} {what} {self._fmt(a.symbol, lvl)}")

    def _check_study(self, a: Alert, prev: float | None, tk: Tick):
        got = self.studies(a.symbol, a.params, tk.ts) if self.studies else None
        if got is None:
            return
        lvl, session = got
        inst = self.instruments[a.symbol]
        tol = a.params.get("tolerance_ticks", 0) * inst.tick_size
        rearm = a.params.get("rearm_ticks", 8) * inst.tick_size
        eps = inst.tick_size * 0.01
        key, px = (a.id, "study"), tk.price
        armed = self.armed.get(key)
        if armed is None or self.study_session.get(a.id) != session:
            # first look: never fire on startup, nor at a session's open, where the level starts over at the price
            self.study_session[a.id] = session
            self.armed[key] = abs(px - lvl) > tol + eps
            return
        if armed:
            lo, hi = (px, px) if prev is None else (min(prev, px), max(prev, px))
            if lo <= lvl + tol + eps and hi >= lvl - tol - eps:
                self.armed[key] = False
                self._fire(a, tk, f"{a.symbol} touched {study_alerts.describe(a.params)} {self._fmt(a.symbol, lvl)}")
        elif abs(px - lvl) >= tol + rearm - eps:    # re-arm measured from the band edge
            self.armed[key] = True

    def _check_levels(self, a: Alert, prev: float | None, tk: Tick):
        ls = self.levels.get(a.symbol) if self.levels else None
        if not ls:
            return
        inst = self.instruments[a.symbol]
        tol = a.params.get("tolerance_ticks", 0) * inst.tick_size
        rearm = a.params.get("rearm_ticks", 8) * inst.tick_size
        wanted = a.params.get("levels", "all")
        eps = inst.tick_size * 0.01        # CL/GC levels carry float32 noise (93.83999633789062)
        px = tk.price
        for name, lvl in ls.lines():
            if wanted != "all" and name not in wanted:
                continue
            key = (a.id, name)
            dist = abs(px - lvl)
            armed = self.armed.get(key)
            if armed is None:                       # first look: never fire on startup
                self.armed[key] = dist > tol + eps
                continue
            if armed:
                lo, hi = (px, px) if prev is None else (min(prev, px), max(prev, px))
                if lo <= lvl + tol + eps and hi >= lvl - tol - eps:
                    self.armed[key] = False
                    self._fire(a, tk, f"{a.symbol} touched {name} {self._fmt(a.symbol, lvl)}")
                    if not a.enabled:
                        return
            elif dist >= tol + rearm - eps:   # re-arm measured from the band edge
                self.armed[key] = True

    def _fmt(self, symbol, price):
        return self.instruments[symbol].fmt(price)

    def _fire(self, a: Alert, tk: Tick, msg: str):
        a.last_fired = tk.ts
        if not a.repeat:
            a.enabled = False
        if a.note:
            msg = f"{msg} · {a.note}"
        ev = {"alert_id": a.id, "symbol": a.symbol, "ts": tk.ts, "price": tk.price,
              "message": f"{msg} (last {self._fmt(a.symbol, tk.price)})"}
        try:
            self.store.update_alert(a.id, last_fired=tk.ts, enabled=a.enabled)
            ev["id"] = self.store.add_event(ev)
        except sqlite3.Error as e:   # the database can't be written (a full disk, another program's lock): sent all the same
            log.warning("alert %s fired, not saved: %s", a.id, e)
            ev["id"] = None
        self.on_fire(ev)
