"""Futures' daily settlements, as their exchanges set them, and the watchlist's change from them: a future's change is
from its latest settlement, from the market's next open (CME's 17:00 CT Globex open) until the open after the next
settlement, as TradingView: after 15:00 ES still shows the day's change, against yesterday's settlement, until 17:00.

Each market's window is in config/settlements.json, in the exchange's own time (CME's E-mini S&P: the volume-weighted
average of the lead month's trades from 14:59:30 to 15:00:00 CT, rounded to the tick). A future charted here has every
trade stored, so its settlement is worked out from them the exchange's way, a minute after its window ends (and again
when a gap fill brings more of them); one only quoted gets the settlement its exchange published, from NinjaTrader
(S lines). On a CME holiday: no settlement on a halt day or a closed day; on an early-close day at the time CME sets
for it (`early`: by the holiday's name, or its "default"), never after the market's close that day
(config/holidays.json). A settlement older than the one before the latest that counts is out of date: the feed's
previous close applies until a new one comes. A contract roll forgets the old contract's (the trades stored before the
roll settle no window): the new contract's comes from NinjaTrader, else the feed's previous close applies, until its
own trades settle a window."""
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import sessions
from .gapfill import ROLLED

log = logging.getLogger(__name__)
SAVED = "settlements"        # {root: {"t": the window's end ms, "price", "src": "trades" | "feed", "prev": the one before}}
RECHECK_MS = 60_000          # how often a market's reference is looked at again
PUBLISH_GRACE_MS = 30 * 60_000   # a feed's settlement this soon after a window may still be the one before


@dataclass(frozen=True)
class Window:
    tz: ZoneInfo
    start: dtime
    end: dtime
    calendar: str | None = None   # a CME holiday group in config/holidays.json; None: weekdays only
    same_as: str | None = None    # settles to another market's settlement (MES to ES), while on the same contract month
    early: tuple = ()             # ((holiday name or "default", "HH:MM" or "normal"), ...): on an early-close day
    opens: dtime | None = None    # its daily open (tz), weekdays; None: CME Globex's 17:00 CT


def load(config_dir: Path | None) -> dict[str, Window]:
    out: dict[str, Window] = {}
    try:
        doc = json.loads((Path(config_dir) / "settlements.json").read_text(encoding="utf-8")) if config_dir else {}
    except (OSError, ValueError) as e:
        log.warning("config/settlements.json not read (%s): the feeds' previous closes apply", type(e).__name__)
        return out
    entries = [(root, v) for g in doc.get("groups") or [] for root, v in (g.get("markets") or {}).items()
               for v in [{**{k: x for k, x in g.items() if k not in ("name", "markets")}, **(v or {})}]]
    for root, v in [*entries, *(doc.get("markets") or {}).items()]:
        try:
            a, b = v["window"]
            early = tuple((str(k), t if t == "normal" else dtime.fromisoformat(t).isoformat("minutes"))
                          for k, t in (v.get("early") or {}).items())
            out[root] = Window(ZoneInfo(v.get("tz", "America/Chicago")), dtime.fromisoformat(a), dtime.fromisoformat(b),
                               v.get("calendar"), v.get("same_as"), early,
                               dtime.fromisoformat(v["opens"]) if v.get("opens") else None)
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            log.warning("settlement window for %s not read (%s)", root, e)
    return out


def _ms(day: date, t: dtime, tz: ZoneInfo) -> int:
    return int(datetime.combine(day, t, tz).timestamp() * 1000)


class Settlements:
    def __init__(self, market, config_dir: Path | None):
        self.m = market
        self.markets = load(config_dir)
        got = market.store.get_setting(SAVED, {}) or {}
        self.saved: dict[str, dict] = got if isinstance(got, dict) else {}
        self.cache: dict[str, tuple[int, dict | None]] = {}   # root -> (look again at, its reference)

    def windows(self, root: str, now: int, days: int = 10) -> list[tuple[int, int]]:
        """The market's settlement windows (start, end) that ended by now, newest first."""
        w = self.markets.get(root)
        if w is None:
            return []
        out = []
        today = datetime.fromtimestamp(now / 1000, w.tz).date()
        hol = sessions.holidays() if w.calendar else {}
        for k in range(days):
            day = today - timedelta(days=k)
            if day.weekday() >= 5:
                continue
            start, end = _ms(day, w.start, w.tz), _ms(day, w.end, w.tz)
            h = hol.get(day)
            if h:
                if h.get("closed") or sessions._time_for(h.get("halt"), w.calendar):
                    continue   # no settlement that day
                early = sessions._time_for(h.get("close"), w.calendar)
                if early:
                    when = dict(w.early).get(h.get("name"), dict(w.early).get("default"))
                    if when and when != "normal":   # the time its exchange set for that day
                        at = _ms(day, dtime.fromisoformat(when), w.tz)
                        start, end = at - (end - start), at
                    hh, mm = map(int, early.split(":"))
                    close = int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=sessions.CT).timestamp() * 1000)
                    if close < end:   # never after the market's close
                        start, end = close - (end - start), close
            if end <= now:
                out.append((start, end))
        return out

    def opens_after(self, root: str, t: int) -> int:
        """The market's first open after t, when a settlement set at t starts to count: CME's next 17:00 CT Globex
        open that trades (Sunday's after a Friday, none on a closed holiday), or its own daily open."""
        w = self.markets.get(root)
        if w and w.opens:
            day = datetime.fromtimestamp(t / 1000, w.tz).date()
            for k in range(8):
                d = day + timedelta(days=k)
                if d.weekday() < 5 and (at := _ms(d, w.opens, w.tz)) > t:
                    return at
        n = sessions.next_session_start(sessions.session_start(t))
        for _ in range(10):
            if sessions.is_open(n + 60_000, w.calendar if w else None) and not sessions._no_session(n + 60_000):
                break
            n = sessions.next_session_start(n)
        return n

    def reference(self, root: str, now: int | None = None) -> dict | None:
        """{"price", "t" (its window's end), "src"}: the settlement the change is from now, or None."""
        now = int(time.time() * 1000) if now is None else now
        c = self.cache.get(root)
        if c and c[0] > now:
            return c[1]
        w = self.markets.get(root)
        if w and w.same_as and w.same_as != root and self._same_month(root, w.same_as):
            ref = self.reference(w.same_as, now)
            until = self.cache[w.same_as][0]
            if ref is None:
                ref, until = self._current(root, now)
        else:
            ref, until = self._current(root, now)
        self.cache[root] = (until, ref)
        return ref

    def _same_month(self, root: str, other: str) -> bool:
        """Both on the same contract month ("MES 03-27", "ES 03-27"), or either's not known. Each rolls by its own
        volume: between a micro's roll and its E-mini's, the micro has its own settlement."""
        c = getattr(self.m, "contracts", None) or {}
        a, b = (str(c.get(k) or "").partition(" ")[2] for k in (root, other))
        return not a or not b or a == b

    def _current(self, root: str, now: int) -> tuple[dict | None, int]:
        """The settlement that counts now, and when to look again."""
        latest, until = self._latest(root, now), now + RECHECK_MS
        if latest is None or root not in self.markets:
            return (None if latest is None else {k: latest[k] for k in ("t", "price", "src")}), until
        ref, opens = latest, self.opens_after(root, latest["t"])
        if now < opens:   # set, and the market hasn't opened since: the day's change is still from the one before
            ref, until = latest.get("prev"), min(until, opens)
        if ref is None:
            return None, until
        due = [x for x in self.windows(root, now) if self.opens_after(root, x[1]) <= now]
        if len(due) > 1 and ref["t"] < due[1][1]:
            return None, until   # out of date (no trades in the window, no word from the feed): its previous close
        return {k: ref[k] for k in ("t", "price", "src")}, until

    def _latest(self, root: str, now: int) -> dict | None:
        """The latest settlement known, and the one before it, worked out from the trades for a future charted here."""
        saved = self.saved.get(root)
        wins = self._trade_windows(root, now)
        if not wins or root not in getattr(self.m, "instruments", {}):
            return saved
        if not (saved and (saved["t"] > wins[0][1] or saved["t"] == wins[0][1] and saved["src"] == "trades")):
            price = self._vwap(root, *wins[0])   # charted here: from its own trades, the exchange's way (over the feed's)
            if price is not None:
                saved = self._save(root, wins[0][1], price, "trades")
        if saved and len(wins) > 1 and saved["t"] == wins[0][1] and not (saved.get("prev") and saved["prev"]["t"] >= wins[1][1]):
            price = self._vwap(root, *wins[1])   # the one before (a restart between a settlement and the open)
            if price is not None:
                saved["prev"] = {"t": wins[1][1], "price": price, "src": "trades"}
                self.m.store.set_setting(SAVED, self.saved)
        return saved

    def _trade_windows(self, root: str, now: int) -> list[tuple[int, int]]:
        """The windows its stored trades can settle: from a minute after each ends (its last trades are in), none from
        before its last contract roll (the old contract's)."""
        got = self.m.store.get_setting(ROLLED, {}) or {}
        since = int(got.get(root, 0)) if isinstance(got, dict) else 0
        return [w for w in self.windows(root, now) if w[0] >= since and w[1] + RECHECK_MS <= now]

    def history_changed(self, root: str, now: int | None = None):
        """Stored trades of `root` changed under the running server (a gap fill): its settlements from the trades are
        worked out again, with the trades the fill brought into their windows."""
        saved = self.saved.get(root)
        if not saved or root not in getattr(self.m, "instruments", {}):
            return
        now = int(time.time() * 1000) if now is None else now
        wins = {end: (start, end) for start, end in self._trade_windows(root, now)}
        for x in (saved, saved.get("prev")):
            if x and x["src"] == "trades" and x["t"] in wins and (price := self._vwap(root, *wins[x["t"]])) is not None:
                x["price"] = price
        self.m.store.set_setting(SAVED, self.saved)
        self._forget(root)

    def rolled(self, root: str):
        """`root` moved to another contract: the settlements known are the old one's and no longer count."""
        if self.saved.pop(root, None) is not None:
            self.m.store.set_setting(SAVED, self.saved)
        self._forget(root)

    def _vwap(self, root: str, start: int, end: int) -> float | None:
        """The volume-weighted average of the trades in [start, end), rounded to the tick; None without trades. While
        the database can't be written (a full disk), the trades waiting for it count too."""
        waiting = [tk for tk in self.m.flush_for_read(root) if start <= tk.ts < end]
        pv, v = self.m.store.db.execute("SELECT SUM(price * size), SUM(size) FROM ticks WHERE symbol=? AND ts>=? AND ts<?",
                                        (root, start, end)).fetchone()
        pv, v = (pv or 0) + sum(tk.price * tk.size for tk in waiting), (v or 0) + sum(tk.size for tk in waiting)
        if not v:
            return None
        return self.m.instruments[root].round(pv / v)

    def on_feed(self, root: str, price: float, now: int | None = None):
        """The settlement its exchange published, from a feed (NinjaTrader's S lines): the latest window's once it is
        published, so a value much sooner after a window than that is taken as the window before's, unless it changed.
        A change then also replaces the feed's own taken for the latest window that soon: on a connect NinjaTrader
        sends the one it holds, which may still be the one before."""
        now = int(time.time() * 1000) if now is None else now
        saved = self.saved.get(root)
        if saved and saved["price"] == price:
            return
        wins = self.windows(root, now)
        if not wins:
            t = now   # a market with no window known: the feed's latest, as it comes
        elif now - wins[0][1] >= PUBLISH_GRACE_MS or len(wins) < 2 or \
                (saved and (saved["t"] < wins[0][1] or saved["t"] == wins[0][1] and saved["src"] == "feed")):
            t = wins[0][1]
        else:
            t = wins[1][1]
        if saved and (saved["t"] > t or (saved["t"] == t and saved["src"] == "trades")):
            return   # its own from the trades, or a newer one, stays
        self._save(root, t, price, "feed")

    def _save(self, root: str, t: int, price: float, src: str) -> dict:
        old = self.saved.get(root)
        prev = old.get("prev") if old and old["t"] == t else {k: old[k] for k in ("t", "price", "src")} if old and old["t"] < t else None
        ref = self.saved[root] = {"t": t, "price": price, "src": src, **({"prev": prev} if prev else {})}
        self.m.store.set_setting(SAVED, self.saved)
        self._forget(root)
        return ref

    def _forget(self, root: str):
        """Its reference is looked at again, and those of the markets that settle to it."""
        self.cache.pop(root, None)
        for k, w in self.markets.items():
            if w.same_as == root:
                self.cache.pop(k, None)
