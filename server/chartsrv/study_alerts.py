"""Where study alerts read their levels: session and RTH VWAP (with sigma bands) and the session profiles'
POC, VAH and VAL. Each symbol with a study alert gets its own 1-minute series, built from history exactly as a
chart's is, so the VWAP an alert fires on is the line on the chart; profiles come from the symbol's profile
book. All of it keeps running with no chart open."""
import asyncio
import logging
import time

from .bars import make_series, parse_spec
from .gapfill import filling
from .models import Tick

log = logging.getLogger(__name__)
STUDIES = ("vwap", "rth_vwap", "poc", "vah", "val")
PROFILE_TTL_MS = 1000       # POC and value area move slowly: recomputed at most once a second


class _Watch:
    def __init__(self, series):
        self.series = series
        self.ready = asyncio.Event()
        self.pending: list[Tick] = []


class StudySources:
    def __init__(self, market):
        self.m = market
        self.watches: dict[str, _Watch] = {}
        self.cache: dict[tuple, tuple[int, dict | None]] = {}

    def ensure(self):
        """Loads what the enabled study alerts need (call on the event loop after alerts change)."""
        wanted = {a.symbol for a in self.m.alerts.alerts.values() if a.kind == "study" and a.enabled}
        for sym in wanted:
            if sym in self.watches or sym not in self.m.instruments:
                continue
            inst = self.m.instruments[sym]
            w = self.watches[sym] = _Watch(make_series(parse_spec("1m"), inst.tick_size, inst.decimals, 3000, inst.rth))
            # the same cut as chart history, made now: stored rows up to the cutoff, later ticks queued in `pending`
            from .market import now_ms
            m = self.m
            w.pending = m.flush_for_read(sym)
            cutoff = m.store.max_tick_rowid()
            last = m.m1[sym].last
            split = last.t if last and sym in m.m1_ticked else now_ms() // 60_000 * 60_000
            if w.pending:   # not written yet (a full disk): stored minutes are complete only before them
                split = min(split, min(tk.ts for tk in w.pending) // 60_000 * 60_000)
            asyncio.get_running_loop().create_task(self._load(sym, w, cutoff, split))
            asyncio.get_running_loop().create_task(self.m.book(sym))
        for sym in list(self.watches):
            if sym not in wanted:
                del self.watches[sym]

    async def _load(self, sym: str, w: _Watch, cutoff: int, split: int):
        from .market import build_history, now_ms
        m = self.m
        try:
            t0 = time.perf_counter()
            await asyncio.to_thread(build_history, m.store.path, w.series, sym, parse_spec("1m"), cutoff, split, now_ms())
            for tk in w.pending:
                w.series.on_tick(tk)
            log.info("study alerts for %s ready in %.1fs", sym, time.perf_counter() - t0)
        except Exception:
            log.exception("study alert history failed for %s", sym)
        finally:
            w.pending = []
            w.ready.set()

    def on_tick(self, tk: Tick):
        w = self.watches.get(tk.symbol)
        if w is None:
            return
        if w.ready.is_set():
            w.series.on_tick(tk)
        else:
            w.pending.append(tk)

    def value(self, sym: str, p: dict, ts: int) -> tuple[float, int] | None:
        """The level a study alert watches at time ts and the session (or RTH day) it belongs to, or None when
        there is none (no data, outside RTH, the stretch up to the live trades still being filled). A new session's
        level starts over at its first trade."""
        if filling(self.m, sym):
            return None
        study = p["study"]
        if study in ("vwap", "rth_vwap"):
            w = self.watches.get(sym)
            if w is None or not w.ready.is_set():
                return None
            s = w.series
            vw = s.vwap if study == "vwap" else s.vwap_rth
            if study == "vwap":
                v = s.vwap.value() if s.vwap.key == s.clock.locate(ts) else None
            else:
                day = s.rth_clock.day(ts) if s.rth else None
                v = s.vwap_rth.value() if day is not None and s.vwap_rth.key == day else None
            return None if v is None else (v[0] + p.get("band", 0) * v[1], vw.key)
        be = self.m.books.get(sym)
        if be is None or not be.ready.is_set():
            return None
        rth = p.get("profile", "eth") != "eth"
        q = be.book.rth if rth else be.book.eth
        i = -1 if p.get("which", "current") == "current" else -2
        if rth and q and be.book.rth_clock.day(ts) != q[-1].key:
            # from the RTH close to the next open the last RTH profile is done: the previous one, none developing
            if i == -1:
                return None
            i = -1
        if len(q) < -i:
            return None
        prof = q[i]
        key = (sym, prof.kind, prof.key)
        hit = self.cache.get(key)
        if hit is None or ts - hit[0] > PROFILE_TTL_MS:
            hit = self.cache[key] = (ts, prof.to_dict(self.m.instruments[sym]))
            if len(self.cache) > 200:
                self.cache = {k: v for k, v in self.cache.items() if ts - v[0] <= PROFILE_TTL_MS}
        d = hit[1]
        return None if d is None else (d[study], prof.key)


def describe(p: dict) -> str:
    """The level's name in alert messages."""
    if p["study"] in ("vwap", "rth_vwap"):
        band = p.get("band", 0)
        return ("session VWAP" if p["study"] == "vwap" else "RTH VWAP") + (f" {band:+d}σ" if band else "")
    which = "developing" if p.get("which", "current") == "current" else "previous"
    return f"{which} {p.get('profile', 'eth').upper()} {p['study'].upper()}"
