"""Dev feeds: random-walk ticks and watchlist quotes, so charts and alerts work before a real feed is connected.
Prices start at the symbol's daily levels when it has any (levels.py), so the levels sit where they would on a real
chart, else at its sim_price."""
import asyncio
import random
import time
import zlib

from .. import watchlists as wl
from ..config import Instrument
from ..levels import Levels
from ..models import Tick

RATES = {"NQ": 8.0, "ES": 3.0}   # ticks per second; others default to 4
DEFAULT_RATE = 4.0
WARMUP_RATE_SCALE = 0.5


def _now_ms():
    return int(time.time() * 1000)


class SimFeed:
    name = "sim"

    def __init__(self, instruments: dict[str, Instrument], levels: Levels | None, warmup_min: int, seed=None):
        self.instruments = instruments
        self.levels = levels
        self.warmup_ms = warmup_min * 60_000
        self.rng = random.Random(seed)
        self.roots = [s for s, i in instruments.items() if not i.sim_follow]
        self.followers = {r: [s for s, i in instruments.items() if i.sim_follow == r] for r in self.roots}
        self.anchor: dict[str, int] = {}
        self.price: dict[str, int] = {}   # in ticks

    def _anchor_price(self, sym: str) -> float:
        ls = self.levels.get(sym) if self.levels else None
        if ls:
            return ls.price or ls.pivot
        return self.instruments[sym].sim_price or 100.0

    def _step(self, sym: str) -> tuple[int, int]:
        p, a = self.price[sym], self.anchor[sym]
        bias = max(-0.15, min(0.15, (a - p) / max(a * 0.004, 1) * 0.05))
        r = self.rng.random()
        if r < 0.6:
            return p, 0
        up = self.rng.random() < 0.5 + bias
        self.price[sym] = p + (1 if up else -1)
        return self.price[sym], (1 if up else -1)

    def _size(self) -> int:
        r = self.rng.random()
        return 1 if r < 0.6 else self.rng.randint(2, 5) if r < 0.9 else self.rng.randint(10, 50)

    def _emit(self, market, sym: str, ts: int, live: bool):
        inst = self.instruments[sym]
        p, side = self._step(sym)
        side = side or self.rng.choice((1, -1))
        price = round(p * inst.tick_size, inst.decimals)
        market.on_tick(Tick(sym, ts, price, self._size(), side), live=live)
        for f in self.followers[sym]:
            market.on_tick(Tick(f, ts, price, max(1, self._size() // 3), side), live=live)

    async def run(self, market):
        now = _now_ms()
        for sym in self.roots:
            inst = self.instruments[sym]
            self.anchor[sym] = round(self._anchor_price(sym) / inst.tick_size)
            last = market.last_tick.get(sym)
            fresh = last and now - last.ts < self.warmup_ms
            self.price[sym] = round(last.price / inst.tick_size) if fresh else self.anchor[sym]
            # back-fill synthetic history up to now so charts open with data
            t = last.ts + 1 if fresh else now - self.warmup_ms
            rate = RATES.get(sym, DEFAULT_RATE) * WARMUP_RATE_SCALE
            while True:
                t += int(self.rng.expovariate(rate) * 1000) + 1
                if t >= now:
                    break
                self._emit(market, sym, t, live=False)
            market.flush()
            await asyncio.sleep(0)
        while True:
            await asyncio.sleep(0.1)
            ts = _now_ms()
            for sym in self.roots:
                expected, acc = RATES.get(sym, DEFAULT_RATE) * 0.1, self.rng.expovariate(1)
                while acc < expected:  # Poisson arrivals
                    self._emit(market, sym, ts, live=True)
                    acc += self.rng.expovariate(1)


class SimQuotes:
    name = "sim-quotes"

    def __init__(self, seed=None):
        self.rng = random.Random(seed)

    async def run(self, market):
        prices, prev, vol = {}, {}, {}
        while True:
            # follows the lists; FRED, EIA and data plugins' series (DATA:) are real
            for sym in sorted(k for k in market.watch_keys if not k.startswith(("FRED:", "EIA:", "DATA:"))):
                root = wl.future_root(sym)   # a chart future's quote (ES1!) follows its trades (ES)
                tk = market.last_tick.get(root) if root else None
                if tk is None and root in market.instruments:
                    continue  # chart symbols wait for the tick feed's first price
                if sym not in prev:
                    base = tk.price if tk else 20 + zlib.crc32(sym.encode()) % 480
                    prev[sym] = base * (1 + self.rng.uniform(-0.01, 0.01))
                    prices[sym] = base
                    vol[sym] = self.rng.randrange(10_000, 5_000_000)
                if tk:
                    prices[sym] = tk.price
                elif self.rng.random() < 0.3:   # not every symbol trades every second
                    prices[sym] *= 1 + self.rng.gauss(0, 0.0006)
                vol[sym] += self.rng.randrange(0, 500)
                market.on_quote(sym, round(prices[sym], 2), round(prev[sym], 2), vol[sym], source="sim")
            await asyncio.sleep(1)
