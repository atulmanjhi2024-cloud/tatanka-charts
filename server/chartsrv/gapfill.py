"""Gap fill from NinjaTrader's own history. When the add-on connects, each symbol's stored data ends somewhere
(NinjaTrader closed, the PC off, the server down); the server sends GAP|SYM|from|to and the add-on answers with
NinjaTrader's history for that stretch: trades (X lines) or, when the data provider has no tick history, 1-minute
bars (Y lines), then Z|SYM|kind|count|note.

Only the stretch is filled: trades strictly after the last stored data and before the first live trade the add-on
sends after connecting (its buffer included), so nothing is counted twice. That end is open until the first live
trade comes: NinjaTrader starts the add-on before its data connection is up, so the add-on (6+) waits for the
connection, then asks up to its own now (GAP|...|live). Minutes wholly inside get bars built from those trades; the
minute where stored data ended is merged; the minute where live data starts is left to the live feed.

Then the holes inside the stored data of the last days (minutes with no bar while the market was open: NinjaTrader
closed and its history not reached then, a fill that failed, its data connection lost while the add-on stayed
connected) are asked for too, and looked for again when a symbol ticks after the watchdog found it stale (again()),
by add-on 6+ (it waits for its data
connection, so an empty answer means NinjaTrader has none), one at a time per symbol, each once, never from before
the symbol's last contract roll (the history would be the other contract's); each is asked from PROBE_MS before it,
and when NinjaTrader's prices there don't match the stored ones (a roll before rolls were recorded), its answer is
another contract's and is dropped. A hole runs from its last stored trade
to the next stored minute's open: that minute is left as stored (the add-on stamps live trades when it sends them,
history with the exchange's time, so a cut inside it could count a trade twice). Charts and studies of the symbol
reload once its holes are done."""
import logging
import math
import time

from . import sessions
from .models import Bar, Tick

log = logging.getLogger(__name__)
MAX_GAP_MS = 14 * 86_400_000     # older than this comes from backfill
LONG_GAP_MS = 90 * 86_400_000    # add-on 8+: each contract of a stretch across rolls in turn, so this far back
MIN_GAP_MS = 120_000
CHUNK = 20_000
HOLE_DAYS = 7                    # holes inside the stored data looked for this far back
HOLE_MIN_MS = 5 * 60_000         # open-market minutes with no bar that make a hole
QUIET_HOLE_MIN_MS = 30 * 60_000  # for a thin market (quiet: no stale alerts) minutes without a trade are normal
MAX_HOLES = 30                   # per symbol and connect, the newest first
ASKED = "gapfill_holes_asked2"   # {symbol: [[from, to], ...]}: holes asked for once already (2: since add-on 7 asks the
                                 # data provider, not only NinjaTrader's own recordings, each is asked once more)
ROLLED = "contract_rolled_at"    # {symbol: ms}: its last contract roll; no hole from before it
PROBE_MS = 3 * 60_000            # a hole is asked from this much earlier: the stored prices there tell the contract apart


def filling(market, sym: str) -> bool:
    """Whether the stretch from `sym`'s stored data up to its live trades is still being filled (the add-on just
    connected, the instrument just put back on Instruments) and is MIN_GAP_MS or more. Its alert watches (gap,
    indicator, study, AMT), loaded from history that stops before it, keep learning from the live minutes but send
    nothing meanwhile: they would compare live prices with days-old bars. history_changed loads them again with the
    fill. A shorter stretch (the add-on reconnecting after a server restart or a network blip) holds nothing back:
    the watches miss a minute or two at most, nothing days old. Nor does a hole."""
    w = getattr(getattr(market, "gapfill", None), "windows", {}).get(sym)
    return w is not None and w.stale


class _Window:
    def __init__(self, start: int, end: float, hole: bool = False):
        self.start, self.end = start, end          # trades with start < ts < end (math.inf: until live trades come)
        self.cutoff = end                          # lowered to the first live trade after connecting
        self.hole = hole                           # inside the stored data
        self.stale = False                         # the stretch before the live trades, MIN_GAP_MS or more (filling),
                                                   # or its rest asked again as minutes
        self.discard = False                       # its answer is another contract's: nothing kept
        self.mismatch = False                      # known by its prices (not by a roll while it was asked)
        self.probe: dict[int, float] = {}          # a hole: the history's last price per minute just before it
        self.first = self.last = None              # the first and last trade the answer brought inside
        self.checked = not hole                    # those compared with the stored ones
        self.ticks: list[Tick] = []
        self.rows: list[tuple] = []                # 1-minute bars from history, not written yet
        self.minutes: dict[int, list] = {}         # minute open -> [o, h, l, c, v, n]
        self.count = 0
        self.t0 = time.monotonic()
        self.answered = False                      # done() with its history stored: no error, nor "none" (none to give)


def shut(ts: int, group: str | None) -> bool:
    """History stamped while the market is shut (the 16:00 break, a weekend, a holiday) is left out: no trade prints
    then. NinjaTrader's 1-minute history has a bar after the close, the session's whole volume at the settlement price
    (a million contracts on NQ); stored, it was a day's volume in one minute on every chart and volume profile holding
    it (store.drop_settlement_bars takes out the ones stored before)."""
    return not sessions.is_open(ts, group)


class GapFill:
    def __init__(self, market):
        self.m = market
        self.windows: dict[str, _Window] = {}
        self.status: dict[str, dict] = {}
        self.queue: dict[str, list[tuple[int, int]]] = {}   # holes still to ask for, per symbol
        self.changed: set[str] = set()   # filled, its charts not reloaded yet (once its holes are done)
        self.minutes_ok = False          # the add-on takes GAP|...|minutes (7+): what trades left out comes as minutes
        self.contracts_ok = False        # the add-on answers a stretch across rolls from each contract in turn (8+)

    def last_stored(self, sym: str) -> int | None:
        """End of the stored data: the last trade when trades reach the last 1-minute bar, else that bar's end
        (imported bars have no trades). Trades still waiting to be written (a full disk) count: asked for again, they
        would be stored twice."""
        db = self.m.store.db
        t = db.execute("SELECT MAX(ts) FROM ticks WHERE symbol=?", (sym,)).fetchone()[0]
        held = [tk.ts for tk in self.m.tick_buf if tk.symbol == sym]
        if held:
            t = max(t or 0, max(held))
        b = db.execute("SELECT MAX(t) FROM bars_1m WHERE symbol=?", (sym,)).fetchone()[0]
        if t is not None and (b is None or t >= b):
            return t
        return b + 60_000 if b is not None else None

    def requests(self, now: int, live: bool = False, minutes: bool = False, contracts: bool = False) -> list[str]:
        """GAP lines for every symbol whose stored data stops short of now, or else its first hole (called when the
        add-on says hello); not the ones a feed plugin supplies (it fills its own: open()). `live`: the add-on asks
        up to its first live trade once its data connection is up, and answers holes (add-on 6+)."""
        self.m.flush_for_read()
        self.minutes_ok, self.contracts_ok = minutes, contracts
        owned = getattr(self.m, "trade_owner", {})
        out = []
        for sym in self.m.instruments:
            if sym in owned:
                continue
            self.windows.pop(sym, None)
            self.queue[sym] = self.holes(sym, now) if live else []   # older add-ons answer before NinjaTrader connects
            span = self.open(sym, now, flush=False, least=1 if live else MIN_GAP_MS,   # open end: any stretch at all
                             reach=self.reach())
            if span:
                out.append(f"GAP|{sym}|{span[0]}|{span[1]}" + ("|live" if live else ""))
            elif line := self.next(sym):
                out.append(line)
        return out

    def again(self, sym: str, now: int) -> str | None:
        """`sym` ticks again after the watchdog found it stale, the add-on connected all along (NinjaTrader's data
        connection lost and back): no hello asks for the hole that left, so its holes are queued as at a connect and
        the GAP line for the first one returned. Not while it has a window or holes still to ask (those go on), nor
        for another feed's."""
        if sym in self.windows or self.queue.get(sym) or sym in getattr(self.m, "trade_owner", {}):
            return None
        self.m.flush_for_read()   # the minute it ticks again, still in memory, is where the hole ends
        self.queue[sym] = self.holes(sym, now)
        return self.next(sym)

    def reach(self) -> int:
        """How far back a stretch is filled: across contract rolls only when the add-on fills each from its contract."""
        return LONG_GAP_MS if self.contracts_ok else MAX_GAP_MS

    def open(self, sym: str, now: int, flush: bool = True, least: int = MIN_GAP_MS, reach: int = MAX_GAP_MS) -> tuple[int, int] | None:
        """The stretch of `sym` to fill, (start, now), if its stored data stops short of now; its trades then go to
        tick() or minute(), and done() ends it. It stays open past now until the first live trade."""
        if flush:
            self.m.flush_for_read()
        last = self.last_stored(sym)   # nothing stored (an instrument just added): the last 14 days
        start = now - reach if last is None else max(last, now - reach)
        rolled = self.m.store.get_setting(ROLLED, {}) or {}
        if last is not None and isinstance(rolled, dict) and int(rolled.get(sym, 0)) > start \
                and (not self.contracts_ok or sym in getattr(self.m, "trade_owner", {})):
            # stored data that stops before a roll recorded since (nothing stored after it: a restart over the weekend)
            # is the old contract's: as when the roll came (rolled()), the new one's history from the next session open
            start = sessions.next_session_start(sessions.session_start(start)) - 1
        if now - start < least:
            return None
        w = self.windows[sym] = _Window(start, math.inf)
        w.stale = now - start >= MIN_GAP_MS   # (what older add-ons and the plugins are asked for at all)
        self.status[sym] = {"from": start, "to": None, "state": "asked"}
        return start, now

    def holes(self, sym: str, now: int) -> list[tuple[int, int]]:
        """Stretches inside `sym`'s stored data of the last HOLE_DAYS, since its last contract roll, with no 1-minute
        bar while the market was open (start, end): from its last trade before (a bar's edge when that minute has no
        trades) to the next stored minute's open. The newest MAX_HOLES, oldest first; not the ones asked for already."""
        inst = self.m.instruments.get(sym)
        if inst is None:
            return []
        db = self.m.store.db
        least = QUIET_HOLE_MIN_MS if getattr(inst, "quiet", False) else HOLE_MIN_MS
        group = getattr(inst, "calendar", None)
        asked = self._asked().get(sym, [])
        rolled = self.m.store.get_setting(ROLLED, {}) or {}
        since = max(now - HOLE_DAYS * 86_400_000, int(rolled.get(sym, 0)) if isinstance(rolled, dict) else 0)
        out = []
        for t, prev in db.execute("SELECT t, prev FROM (SELECT t, LAG(t) OVER (ORDER BY t) AS prev FROM bars_1m "
                                  "WHERE symbol=? AND t>=?) WHERE t - prev > ?", (sym, since, least)):
            a, b = prev + 60_000, t   # the minutes with no bar
            if self._open_ms(group, a, b) < least:
                continue   # the daily break, a weekend, a holiday
            if any(x <= a and b <= y for x, y in asked):
                continue
            # from its last trade before, or after the trades a fill cut short stored (none twice)
            last = db.execute("SELECT MAX(ts) FROM ticks WHERE symbol=? AND ts>=? AND ts<?", (sym, prev, b)).fetchone()[0]
            out.append((last if last is not None else a - 1, b))
        return out[-MAX_HOLES:]

    @staticmethod
    def _open_ms(group, a: int, b: int) -> int:
        """How long the market was open between a and b, by whole minutes."""
        return sum(sessions.is_open(k, group) for k in range(a // 60_000 * 60_000, b, 60_000)) * 60_000

    def next(self, sym: str) -> str | None:
        """The GAP line for `sym`'s next hole, its window open (one at a time: the add-on's lines name the symbol
        only); None when they are done, and its charts reload if any was filled."""
        if sym in self.windows:
            return None
        if not self.queue.get(sym):
            self.settle(sym)
            return None
        item = self.queue[sym].pop(0)   # (start, end[, probe[, minutes[, stale]]])
        span = self.open_hole(sym, item[0], item[1], item[2] if len(item) > 2 else True)
        self.windows[sym].stale = len(item) > 4 and item[4]   # the rest of a stretch that holds alerts: they wait for it
        return f"GAP|{sym}|{span[0]}|{span[1]}" + ("|minutes" if len(item) > 3 and item[3] else "")

    def settle(self, sym: str):
        """`sym`'s charts and studies reload, once, if anything was filled since the last time."""
        if sym in self.changed:
            self.changed.discard(sym)
            self.m.history_changed(sym)

    def rolled(self, sym: str, now: int):
        """`sym` moved to another contract: no hole from before now is asked for; the one being asked is dropped
        unless its answer has begun (asked before the roll: the old contract's, right for it). The gap at the end
        keeps only what comes from the next session open: the new contract's history, not merged into the old one's
        minute or session (the add-on's rolls land in the break)."""
        got = self.m.store.get_setting(ROLLED, {}) or {}
        got = got if isinstance(got, dict) else {}
        self.m.store.set_setting(ROLLED, {**got, sym: now})
        self.queue.pop(sym, None)
        w = self.windows.get(sym)
        if w is not None and w.hole and not (w.probe or w.count):
            w.discard = True
        elif w is not None and not w.hole and (not self.contracts_ok or sym in getattr(self.m, "trade_owner", {})):
            # (add-on 8+ fills each contract from its own; a feed plugin's history is the contract it is on now)
            w.start = max(w.start, sessions.next_session_start(sessions.session_start(w.start)) - 1)

    def open_hole(self, sym: str, start: int, end: int, probe: bool = True) -> tuple[int, int]:
        """A hole's window: trades with start < ts < end; done() ends it. The span to ask for starts PROBE_MS
        earlier: those prices are compared with the stored ones, not kept (`probe` False: checked already)."""
        self.windows[sym] = _Window(start, end, hole=True)
        self.windows[sym].checked = not probe
        self.status[sym] = {"from": start, "to": end, "state": "asked", "hole": True}
        return (start - PROBE_MS if probe else start), end

    def _same_contract(self, sym: str, w: _Window) -> bool:
        """The history's prices just before a hole against the stored bars' closes, minute by minute: one contract
        matches to a few ticks somewhere, another is off by the spread between them everywhere."""
        if not w.probe:
            return True   # nothing to compare: taken as it comes
        rows = self.m.store.db.execute("SELECT t, c FROM bars_1m WHERE symbol=? AND t>=? AND t<=?",
                                       (sym, min(w.probe), max(w.probe))).fetchall()
        diffs = [abs(c - w.probe[t]) for t, c in rows if t in w.probe]
        if not diffs:
            return True
        tick = self.m.instruments[sym].tick_size
        return min(diffs) <= max(10 * tick, 0.0005 * max(w.probe.values()))

    def _check(self, sym: str, w: _Window):
        """Once, at the first answer inside the hole (the history comes oldest first, its probe before it)."""
        if not w.checked:
            w.checked = True
            if not self._same_contract(sym, w):
                w.discard = w.mismatch = True
                log.warning("gap fill %s: NinjaTrader's prices before the hole are another contract's; not kept", sym)

    def _asked(self) -> dict[str, list]:
        got = self.m.store.get_setting(ASKED, {}) or {}
        return got if isinstance(got, dict) else {}

    def _remember(self, sym: str, w: _Window):
        """A hole asked for and answered: not asked again (NinjaTrader had what it had). Ones over a week older than it
        are forgotten (holes are only looked for in the last HOLE_DAYS)."""
        keep = w.end - (HOLE_DAYS + 1) * 86_400_000
        asked = {s: [x for x in v if x[1] >= keep] for s, v in self._asked().items()}
        asked.setdefault(sym, []).append([w.start + 1, w.end])   # its minutes: from the one after the last trade
        self.m.store.set_setting(ASKED, {s: v for s, v in asked.items() if v})

    def forget_asked(self, syms):
        """Another feed supplies these now: the holes asked of the one before are asked of it too (what one had none
        for, the other may have: IBKR keeps 1-minute bars for months)."""
        asked = self._asked()
        if any(s in asked for s in syms):
            self.m.store.set_setting(ASKED, {s: v for s, v in asked.items() if s not in syms})

    def until(self, sym: str, now: int) -> tuple[int, int] | None:
        """An open window's span, up to now unless a live trade already marked where live data starts (a feed plugin
        opens its windows before it connects, then asks for the history once connected)."""
        w = self.windows.get(sym)
        if w is None:
            return None
        end = int(min(w.cutoff, now))
        self.status[sym]["to"] = end
        return w.start, end

    def live(self, sym: str, ts: int):
        """A live trade (or one from the add-on's buffer): history stops before it."""
        w = self.windows.get(sym)
        if w and ts < w.cutoff:
            w.cutoff = max(ts, w.start)

    def tick(self, sym: str, ts: int, price: float, size: int):
        w = self.windows.get(sym)
        if not w or w.discard or size <= 0:
            return
        if w.hole and w.start - PROBE_MS < ts <= w.start:   # just before the hole: to compare, not to keep
            w.probe[ts // 60_000 * 60_000] = self.m.instruments[sym].round(price)
            return
        if not (w.start < ts < w.cutoff):
            return
        inst = self.m.instruments[sym]
        if shut(ts, getattr(inst, "calendar", None)):
            return
        self._check(sym, w)
        if w.discard:
            return
        w.first = ts if w.first is None else min(w.first, ts)
        w.last = ts if w.last is None else max(w.last, ts)
        price = inst.round(price)
        w.ticks.append(Tick(sym, ts, price, size, 0))
        k = ts // 60_000 * 60_000
        b = w.minutes.get(k)
        if b is None:
            w.minutes[k] = [price, price, price, price, size, 1]
        else:
            b[1], b[2], b[3], b[4], b[5] = max(b[1], price), min(b[2], price), price, b[4] + size, b[5] + 1
        w.count += 1
        if len(w.ticks) % CHUNK == 0:   # a failed write (the database not writable) keeps them for the next chunk
            self._write_ticks(w)

    def minute(self, sym: str, t: int, o: float, h: float, lo: float, c: float, v: int):
        """A 1-minute bar (by its open) when the provider has no tick history: only minutes wholly in the gap."""
        w = self.windows.get(sym)
        if not w or w.discard:
            return
        if w.hole and w.start - PROBE_MS - 60_000 <= t < w.start:   # just before the hole: to compare
            w.probe[t] = self.m.instruments[sym].round(c)
            return
        if t < w.start or t + 60_000 > w.cutoff:
            return
        if shut(t, getattr(self.m.instruments[sym], "calendar", None)):
            return
        self._check(sym, w)
        if w.discard:
            return
        r = self.m.instruments[sym].round
        w.rows.append((t, r(o), r(h), r(lo), r(c), v, 0))   # written in batches: months of minutes come at once
        w.count += 1
        if len(w.rows) % CHUNK == 0:
            self._write_rows(sym, w)

    def done(self, sym: str, kind: str, count: int, note: str):
        w = self.windows.pop(sym, None)
        if w is None:
            return
        if w.hole and kind == "ticks" and not w.count and w.probe and not w.discard:
            self._check(sym, w)   # only trades before the hole came: the provider keeps no tick history for it
            if not w.discard:     # the same contract: asked again from its start, so the add-on sends 1-minute bars
                self.queue.setdefault(sym, []).insert(0, (w.start, w.end, False))
                self.status[sym] = {"from": w.start, "to": w.end, "state": "asked", "hole": True, "note": "again for minutes"}
                return
        if w.discard:   # another contract's trades: nothing kept
            if w.mismatch:   # NinjaTrader's history is the current contract's: asking again won't help
                self._remember(sym, w)
            self.status[sym] = {"from": w.start, "to": w.end, "state": "dropped", "hole": True,
                                "note": "another contract's prices" if w.mismatch else "contract rolled meanwhile"}
            if not self.queue.get(sym):
                self.settle(sym)
            return
        open_end = w.cutoff == math.inf
        if open_end:   # no live trade yet: up to now, the minute it is now kept going by the live feed
            w.cutoff = int(time.time() * 1000)
        self._write_ticks(w)
        self._write_rows(sym, w)
        merged = self._write_minutes(sym, w, open_end)
        if w.hole and kind != "error":
            self._remember(sym, w)
        w.answered = kind not in ("error", "none")   # (a gap alert held meanwhile is sent: gaps_service)
        if kind == "ticks" and w.first is not None and self.minutes_ok and sym not in getattr(self.m, "trade_owner", {}):
            self._rest_as_minutes(sym, w)
        took = time.monotonic() - w.t0
        self.status[sym] = {"from": w.start, "to": w.cutoff, "state": "done", "kind": kind, "sent": count,
                            "stored": w.count, "minutes": merged, "note": note, "seconds": round(took, 1),
                            **({"hole": True} if w.hole else {})}
        log.info("gap fill %s: %s, %d sent, %d stored, %d minutes (%s) in %.1fs", sym, kind, count, w.count, merged, note, took)
        if w.count or merged:
            self.changed.add(sym)
        # the gap at the end reloads now, and again once its rest is in (the alerts it held go on then); holes once they
        # are done
        more = self.queue.get(sym)
        if not w.hole or not more or w.stale and not (len(more[0]) > 4 and more[0][4]):
            self.settle(sym)

    def _write_rows(self, sym: str, w: _Window):
        if w.rows:
            self.m.store.insert_rows_if_missing(sym, w.rows)
            w.rows = []

    def _write_ticks(self, w: _Window):
        if w.ticks:
            self.m.store.write(w.ticks, [])
            w.ticks = []

    def _rest_as_minutes(self, sym: str, w: _Window):
        """The trades came for part of the stretch only (NinjaTrader recorded them itself; the provider keeps no tick
        history that far back): the rest before and after them is asked again as 1-minute bars, first, holding the
        alerts as the stretch did (filling)."""
        group = getattr(self.m.instruments.get(sym), "calendar", None)
        rest = []
        if self._open_ms(group, w.start, w.first) >= HOLE_MIN_MS:
            rest.append((w.start, w.first // 60_000 * 60_000, False, True, w.stale))
        end = int(w.cutoff)
        if self._open_ms(group, w.last, end) >= HOLE_MIN_MS:
            rest.append(((w.last // 60_000 + 1) * 60_000 - 1, end, False, True, w.stale))
        if rest:
            self.queue[sym] = rest + self.queue.get(sym, [])

    def _write_minutes(self, sym: str, w: _Window, open_end: bool = False) -> int:
        """Bars from the gap's trades: whole minutes replace, the minute where stored data ended is merged, the
        minute the live feed is building is left alone. With no live trade yet (open_end), the minute it is now is
        written too and handed to the live feed, which goes on with it. Trades a fill cut short stored before it (no
        bars: those come at the end) get theirs too, the minute it was cut in from both fills' trades."""
        if w.hole:
            return self._hole_minutes(sym, w)
        db = self.m.store.db
        before = db.execute("SELECT MAX(t) FROM bars_1m WHERE symbol=? AND t<=?", (sym, w.start)).fetchone()[0]
        cut: dict[int, list] = {}   # minute open -> [o, h, l, c, v, n] of the stored trades after the last bar
        for ts, px, size in db.execute("SELECT ts, price, size FROM ticks WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
                                       (sym, 0 if before is None else before + 60_000, w.start)):
            b = cut.get(k := ts // 60_000 * 60_000)
            if b is None:
                cut[k] = [px, px, px, px, size, 1]
            else:
                b[1], b[2], b[3], b[4], b[5] = max(b[1], px), min(b[2], px), px, b[4] + size, b[5] + 1
        if not w.minutes and not cut:
            return 0
        first = w.start // 60_000 * 60_000
        last_live = int(w.cutoff) // 60_000 * 60_000
        bars = []
        for k in sorted(set(w.minutes) | set(cut)):
            if k > last_live or (k == last_live and not open_end):
                continue
            o, h, lo, c, v, n = w.minutes.get(k) or cut[k]
            bv = sv = 0   # history has no aggressor side: a merged minute keeps the stored split
            if k in cut and k in w.minutes:   # where the fill was cut: its stored trades came first
                so, sh, sl, _, sv_, sn = cut[k]
                o, h, lo, v, n = so, max(sh, h), min(sl, lo), sv_ + v, sn + n
            elif k == first and w.start % 60_000:
                old = db.execute("SELECT o,h,l,c,v,bv,sv,n FROM bars_1m WHERE symbol=? AND t=?", (sym, k)).fetchone()
                if old:
                    o, h, lo, v, bv, sv, n = old[0], max(old[1], h), min(old[2], lo), old[4] + v, old[5], old[6], old[7] + n
            bars.append((sym, Bar(t=k, o=o, h=h, l=lo, c=c, v=v, bv=bv, sv=sv, n=n)))
        self.m.store.write([], bars)
        s = getattr(self.m, "m1", {}).get(sym)
        if open_end and bars and s is not None and bars[-1][1].t == last_live:   # the live feed goes on with it
            b = bars[-1][1]
            if s.last is not None and s.last.t == last_live:   # its own copy (seeded, or from before): now the merged one
                x = s.last   # in place: no live trade since the flush at hello, so nothing else holds a newer one
                x.o, x.h, x.l, x.c, x.v, x.bv, x.sv, x.n = b.o, b.h, b.l, b.c, b.v, b.bv, b.sv, b.n
            elif s.last is None or s.last.t < last_live:
                s._seq += 1   # a live trade in this minute extends it, as after a restart (Market.seed)
                b.seq = s._seq
                s.bars.append(b)
        return len(bars)

    def _hole_minutes(self, sym: str, w: _Window) -> int:
        """A hole's bars: each minute with no bar from its trades (those a fill cut short stored before, then this
        fill's); the minute before it, stored already, merged with what came after its last trade."""
        db = self.m.store.db
        first = db.execute("SELECT MAX(t) FROM bars_1m WHERE symbol=? AND t<=?", (sym, w.start)).fetchone()[0]   # its bar before
        first = w.start // 60_000 * 60_000 if first is None else first
        last = int(w.cutoff) // 60_000 * 60_000
        stored = {r[0]: r[1:] for r in db.execute("SELECT t,o,h,l,c,v,bv,sv,n FROM bars_1m WHERE symbol=? AND t>=? AND t<=?",
                                                  (sym, first, last))}
        built: dict[int, list] = {}

        def add(k, o, h, lo, c, v, n):
            b = built.get(k)
            if b is None:
                built[k] = [o, h, lo, c, v, n]
            else:
                b[1], b[2], b[3], b[4], b[5] = max(b[1], h), min(b[2], lo), c, b[4] + v, b[5] + n
        for ts, px, size in db.execute("SELECT ts, price, size FROM ticks WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
                                       (sym, first, w.start)):   # stored before this fill, with no bar
            if ts // 60_000 * 60_000 not in stored:
                add(ts // 60_000 * 60_000, px, px, px, px, size, 1)
        for k, (o, h, lo, c, v, n) in sorted(w.minutes.items()):   # this fill's, all after w.start
            if k not in stored and k < last:
                add(k, o, h, lo, c, v, n)
        bars = [Bar(t=k, o=o, h=h, l=lo, c=c, v=v, n=n) for k, (o, h, lo, c, v, n) in built.items()]
        if first in stored and first in w.minutes:   # where stored data ended: it came first
            (so, sh, sl, _, sv_, sbv, ssv, sn), (o, h, lo, c, v, n) = stored[first], w.minutes[first]
            bars.append(Bar(t=first, o=so, h=max(sh, h), l=min(sl, lo), c=c, v=sv_ + v, bv=sbv, sv=ssv, n=sn + n))
        self.m.store.write([], [(sym, b) for b in bars])
        return len(bars)

    def health(self) -> dict:
        return self.status
