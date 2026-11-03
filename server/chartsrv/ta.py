"""Technical indicators as NinjaTrader 8 computes them, one bar at a time: the same formulas as the chart's
(web/ta.js), so an indicator alert fires on the value the chart shows; a test runs both on the same bars. A runner's
step(bar, commit) returns its outputs for a bar (anything with t, o, h, l, c, v); commit False is the forming bar's
value without moving on. Where NinjaTrader has no such indicator (SuperTrend, Ichimoku, relative volume) or its exact
code is not followed (SMMA's first bars, the regression channel's band), the usual definition is used, as
TradingView's. Trade dates and the clock are CME's (17:00 CT), as for every instrument charted live."""
import math
from datetime import date, datetime, timedelta

from .sessions import CT, fold_trade_date


def _zero(x: float) -> bool:   # NinjaTrader's ApproxCompare(0) == 0
    return abs(x) < 1e-10


class Buf:
    """A look-back series: at(0) the newest value, at(k) k bars before; kept to what the longest look-back needs."""

    def __init__(self, keep: int):
        self.a: list = []
        self.keep = max(2, keep)

    def push(self, x):
        self.a.append(x)

    def pop(self):
        self.a.pop()

    def at(self, k: int):
        return self.a[len(self.a) - 1 - k]

    def trim(self):
        if len(self.a) > 2 * self.keep + 256:
            del self.a[:len(self.a) - self.keep]


def _max(buf: Buf, m: int) -> float:
    x = -math.inf
    for k in range(m):
        x = max(x, buf.at(k))
    return x


def _min(buf: Buf, m: int) -> float:
    x = math.inf
    for k in range(m):
        x = min(x, buf.at(k))
    return x


# ---- NinjaTrader's EMA, SMA, RSI, ATR and MACD -----------------------------------------------------------------------
class Ema:
    def __init__(self, n):
        self.k, self.v = 2 / (1 + n), None

    def step(self, x, commit=True):
        v = x if self.v is None else x * self.k + (1 - self.k) * self.v
        if commit:
            self.v = v
        return v


class Sma:
    def __init__(self, n):
        self.n, self.buf, self.sum = n, [], 0.0

    def step(self, x, commit=True):
        k = len(self.buf)
        s = self.sum + x - (self.buf[k - self.n] if k >= self.n else 0)
        if commit:
            self.buf.append(x)
            self.sum = s
            if len(self.buf) > 4 * self.n + 64:
                del self.buf[:len(self.buf) - self.n]
        return s / min(k + 1, self.n)


class Rsi:
    def __init__(self, n, smooth=3):
        self.n, self.c1, self.i, self.prev = n, 2 / (1 + smooth), 0, 0.0
        self.sma_up, self.sma_down = Sma(n), Sma(n)
        self.avg_up = self.avg_down = self.avg = 0.0

    def step(self, x, commit=True):
        n, i = self.n, self.i
        up = 0.0 if i == 0 else max(x - self.prev, 0.0)
        down = 0.0 if i == 0 else max(self.prev - x, 0.0)
        s_up, s_down = self.sma_up.step(up, commit), self.sma_down.step(down, commit)
        avg_up, avg_down, avg, rsi = self.avg_up, self.avg_down, self.avg, None
        if i == 0:
            if n < 3:
                avg = 50.0
        elif i + 1 < n:
            if i + 1 == n - 1:
                avg = 50.0
        else:
            if i + 1 == n:
                avg_down, avg_up = s_down, s_up
            else:
                avg_down, avg_up = (avg_down * (n - 1) + down) / n, (avg_up * (n - 1) + up) / n
            rsi = 100.0 if avg_down == 0 else 100 - 100 / (1 + avg_up / avg_down)
            avg = self.c1 * rsi + (1 - self.c1) * avg
        if commit:
            self.i, self.prev, self.avg_up, self.avg_down, self.avg = i + 1, x, avg_up, avg_down, avg
        return (None, None) if rsi is None else (rsi, avg)


class Atr:
    def __init__(self, n):
        self.n, self.i, self.v, self.close = n, 0, 0.0, 0.0

    def step(self, b, commit=True):
        if self.i == 0:
            v = b.h - b.l
        else:
            m = min(self.i + 1, self.n)
            v = ((m - 1) * self.v + max(abs(b.l - self.close), b.h - b.l, abs(b.h - self.close))) / m
        if commit:
            self.i, self.v, self.close = self.i + 1, v, b.c
        return v


class Macd:
    def __init__(self, fast=12, slow=26, smooth=9):
        self.fast, self.slow, self.avg = Ema(fast), Ema(slow), Ema(smooth)

    def step(self, x, commit=True):
        m = self.fast.step(x, commit) - self.slow.step(x, commit)
        a = self.avg.step(m, commit)
        return m, a, m - a


# ---- moving averages ---------------------------------------------------------------------------------------------------
class Wma:
    def __init__(self, n):
        self.n, self.x, self.i = n, Buf(n), 0

    def step(self, x, commit=True):
        self.x.push(x)
        back = min(self.n - 1, self.i)
        val, w = 0.0, 0
        for k in range(back, -1, -1):
            val += (k + 1) * self.x.at(back - k)
            w += k + 1
        if commit:
            self.i += 1
            self.x.trim()
        else:
            self.x.pop()
        return val / w


class Hma:
    def __init__(self, n):
        self.a, self.b, self.d = Wma(max(1, n // 2)), Wma(n), Wma(max(1, math.floor(math.sqrt(n))))

    def step(self, x, commit=True):
        return self.d.step(2 * self.a.step(x, commit) - self.b.step(x, commit), commit)


class Vwma:
    def __init__(self, n):
        self.n, self.x, self.v, self.i = n, Buf(n), Buf(n), 0

    def step(self, b, commit=True):
        self.x.push(b.c)
        self.v.push(b.v or 0)
        sv = sxv = 0.0
        for k in range(min(self.i, self.n - 1), -1, -1):
            sv += self.v.at(k)
            sxv += self.x.at(k) * self.v.at(k)
        if commit:
            self.i += 1
            self.x.trim()
            self.v.trim()
        else:
            self.x.pop()
            self.v.pop()
        return sxv / sv if sv > 0 else b.c


class Dema:
    def __init__(self, n):
        self.e1, self.e2 = Ema(n), Ema(n)

    def step(self, x, commit=True):
        a = self.e1.step(x, commit)
        return 2 * a - self.e2.step(a, commit)


class Tema:
    def __init__(self, n):
        self.e1, self.e2, self.e3 = Ema(n), Ema(n), Ema(n)

    def step(self, x, commit=True):
        a = self.e1.step(x, commit)
        b = self.e2.step(a, commit)
        return 3 * a - 3 * b + self.e3.step(b, commit)


class Smma:
    def __init__(self, n):
        self.n, self.sma, self.i, self.v = n, Sma(n), 0, 0.0

    def step(self, x, commit=True):
        s = self.sma.step(x, commit)
        v = s if self.i < self.n else (self.v * (self.n - 1) + x) / self.n
        if commit:
            self.i, self.v = self.i + 1, v
        return v


# ---- bands and channels ------------------------------------------------------------------------------------------------
class StdDev:
    def __init__(self, n):
        self.n, self.x, self.sma, self.i = n, Buf(n), Sma(n), 0

    def step(self, x, commit=True):
        self.x.push(x)
        avg = self.sma.step(x, commit)
        sd = 0.0
        if self.i >= 1:
            s = 0.0
            for k in range(min(self.i, self.n - 1), -1, -1):
                d = self.x.at(k) - avg
                s += d * d
            sd = math.sqrt(s / min(self.i + 1, self.n))
        if commit:
            self.i += 1
            self.x.trim()
        else:
            self.x.pop()
        return avg, sd


class Bollinger:
    def __init__(self, n, k):
        self.sd, self.k = StdDev(n), k

    def step(self, b, commit=True):
        m, sd = self.sd.step(b.c, commit)
        return [m + self.k * sd, m, m - self.k * sd]


class Keltner:
    def __init__(self, n, k):
        self.typ, self.rng, self.k = Sma(n), Sma(n), k

    def step(self, b, commit=True):
        mid = self.typ.step((b.h + b.l + b.c) / 3, commit)
        off = self.rng.step(b.h - b.l, commit) * self.k
        return [mid + off, mid, mid - off]


class Donchian:
    def __init__(self, n):
        self.n, self.h, self.l, self.i = n, Buf(n), Buf(n), 0

    def step(self, b, commit=True):
        self.h.push(b.h)
        self.l.push(b.l)
        m = min(self.i + 1, self.n)
        hi, lo = _max(self.h, m), _min(self.l, m)
        _done(self, commit, self.h, self.l)
        return [hi, (hi + lo) / 2, lo]


def _done(run, commit: bool, *bufs: Buf):
    """A step's end: kept (the bar count on, the buffers trimmed) or not (the forming bar's values taken back off)."""
    if commit:
        run.i += 1
        for b in bufs:
            b.trim()
    else:
        for b in bufs:
            b.pop()


# ---- oscillators -------------------------------------------------------------------------------------------------------
class Stoch:
    def __init__(self, period_k, smooth, period_d):
        self.n, self.h, self.l, self.i, self.fk = period_k, Buf(period_k), Buf(period_k), 0, 0.0
        self.k, self.d = Sma(smooth), Sma(period_d)

    def step(self, b, commit=True):
        self.h.push(b.h)
        self.l.push(b.l)
        m = min(self.i + 1, self.n)
        lo = _min(self.l, m)
        den = _max(self.h, m) - lo
        fk = (50.0 if self.i == 0 else self.fk) if _zero(den) else min(100.0, max(0.0, 100 * (b.c - lo) / den))
        k = self.k.step(fk, commit)
        d = self.d.step(k, commit)
        if commit:
            self.fk = fk
        _done(self, commit, self.h, self.l)
        return [k, d]


class StochRsi:
    def __init__(self, n):
        self.n, self.rsi, self.r, self.i = n, Rsi(n, 1), Buf(n), 0

    def step(self, b, commit=True):
        r = self.rsi.step(b.c, commit)[0]
        self.r.push(r)
        lo, hi = math.inf, -math.inf
        for k in range(min(self.i, self.n - 1), -1, -1):
            v = self.r.at(k)
            if v is not None:
                lo, hi = min(lo, v), max(hi, v)
        v = None if r is None else (r - lo) / (hi - lo) if r != lo and hi != lo else 0.0
        _done(self, commit, self.r)
        return [v]


class WilliamsR:
    def __init__(self, n):
        self.n, self.h, self.l, self.i = n, Buf(n), Buf(n), 0

    def step(self, b, commit=True):
        self.h.push(b.h)
        self.l.push(b.l)
        m = min(self.i + 1, self.n)
        hi = _max(self.h, m)
        den = hi - _min(self.l, m)
        _done(self, commit, self.h, self.l)
        return [-100 * (hi - b.c) / (1 if den == 0 else den)]


class Cci:
    def __init__(self, n):
        self.n, self.t, self.sma, self.i = n, Buf(n), Sma(n), 0

    def step(self, b, commit=True):
        typ = (b.h + b.l + b.c) / 3
        self.t.push(typ)
        avg = self.sma.step(typ, commit)
        v = 0.0
        if self.i > 0:
            mean = 0.0
            for k in range(min(self.i, self.n - 1), -1, -1):
                mean += abs(self.t.at(k) - avg)
            v = (typ - avg) / (1 if _zero(mean) else 0.015 * (mean / min(self.n, self.i + 1)))
        _done(self, commit, self.t)
        return [v]


class Momentum:
    def __init__(self, n, pct):
        self.n, self.pct, self.x, self.i = n, pct, Buf(n + 1), 0

    def step(self, b, commit=True):
        self.x.push(b.c)
        old = self.x.at(min(self.i, self.n))
        v = (b.c - old) / old * 100 if self.pct else 0.0 if self.i == 0 else b.c - old
        _done(self, commit, self.x)
        return [v]


# ---- trend and volatility ----------------------------------------------------------------------------------------------
class Adx:
    def __init__(self, n):
        self.n, self.i = n, 0
        self.ph = self.pl = self.pc = self.str = self.sp = self.sm = self.adx = 0.0

    def step(self, b, commit=True):
        n = self.n
        dip = dim = 0.0
        if self.i == 0:
            s_tr, sp, sm, adx = b.h - b.l, 0.0, 0.0, 50.0
        else:
            dmm = max(self.pl - b.l, 0) if self.pl - b.l > b.h - self.ph else 0
            dmp = max(b.h - self.ph, 0) if b.h - self.ph > self.pl - b.l else 0
            tr = max(b.h - b.l, max(abs(b.h - self.pc), abs(b.l - self.pc)))
            if self.i < n:
                s_tr, sp, sm = self.str + tr, self.sp + dmp, self.sm + dmm
            else:
                s_tr = self.str - self.str / n + tr
                sp = self.sp - self.sp / n + dmp
                sm = self.sm - self.sm / n + dmm
            dip = 100 * (0 if _zero(s_tr) else sp / s_tr)
            dim = 100 * (0 if _zero(s_tr) else sm / s_tr)
            total = dip + dim
            adx = 50.0 if _zero(total) else ((n - 1) * self.adx + 100 * abs(dip - dim) / total) / n
        if commit:
            self.i += 1
            self.ph, self.pl, self.pc, self.str, self.sp, self.sm, self.adx = b.h, b.l, b.c, s_tr, sp, sm, adx
        return [adx, dip, dim]


class Psar:
    def __init__(self, acc, inc, mx):
        self.acc, self.inc, self.max, self.i = acc, inc, mx, 0
        self.h, self.l, self.s = Buf(4), Buf(4), None

    def step(self, b, commit=True):
        self.h.push(b.h)
        self.l.push(b.l)
        H, L = self.h.at, self.l.at
        s, value = dict(self.s) if self.s else None, None
        if self.i == 3:
            hi, lo = max(H(0), H(1), H(2)), min(L(0), L(1), L(2))
            long = H(0) > H(3)
            s = {"long": long, "xp": hi if long else lo, "af": self.acc, "prev": 0.0, "value": 0.0}
            value = s["value"] = s["xp"] + (-1 if long else 1) * ((hi - lo) * s["af"])
        elif self.i > 3:
            t = s["value"] + s["af"] * (s["xp"] - s["value"])
            if s["long"]:
                low = min(t, L(0), L(1))
                if L(0) > low:
                    t = low
            else:
                high = max(t, H(0), H(1))
                if H(0) < high:
                    t = high
            for x in (1, 2):
                if s["long"]:
                    if t > L(x):
                        t = L(x)
                elif t < H(x):
                    t = H(x)
            value = s["prev"] = t
            if (H(0) > s["xp"]) if s["long"] else (L(0) < s["xp"]):
                s["xp"] = H(0) if s["long"] else L(0)
                s["af"] = min(self.max, s["af"] + self.inc)
            if (L(0) < t or L(1) < t) if s["long"] else (H(0) > t or H(1) > t):
                r = s["xp"]
                if (s["prev"] > L(0)) if s["long"] else (s["prev"] < H(0)):
                    s["long"] = not s["long"]
                    s["af"] = self.acc
                    s["xp"] = H(0) if s["long"] else L(0)
                    s["prev"] = r
                else:
                    r = s["prev"]
                value = r
            s["value"] = value
        if commit:
            self.s = s
        _done(self, commit, self.h, self.l)
        return [value]


class SuperTrend:
    def __init__(self, n, factor):
        self.n, self.f, self.i, self.sum, self.atr, self.pc = n, factor, 0, 0.0, None, math.nan
        self.up = self.lo = self.st = None

    def step(self, b, commit=True):
        n = self.n
        tr = b.h - b.l if self.i == 0 else max(b.h - b.l, abs(b.h - self.pc), abs(b.l - self.pc))
        total, atr, up, lo, d, st = self.sum, None, None, None, None, None
        if self.i < n:
            total += tr
            if self.i == n - 1:
                atr = total / n
        else:
            atr = (self.atr * (n - 1) + tr) / n
        if atr is not None:
            src = (b.h + b.l) / 2
            pu = 0.0 if self.up is None else self.up
            pl = 0.0 if self.lo is None else self.lo
            up, lo = src + self.f * atr, src - self.f * atr
            lo = lo if lo > pl or self.pc < pl else pl
            up = up if up < pu or self.pc > pu else pu
            if self.atr is None:
                d = 1
            elif self.st == pu:
                d = -1 if b.c > up else 1
            else:
                d = 1 if b.c < lo else -1
            st = lo if d == -1 else up
        if commit:
            self.i += 1
            self.sum, self.atr, self.pc, self.up, self.lo, self.st = total, atr, b.c, up, lo, st
        return [st, d]


class Ichimoku:
    def __init__(self, conv, base, span, disp):
        self.p, self.shift, self.i = (conv, base, span), max(0, disp - 1), 0
        keep = max(conv, base, span)
        self.h, self.l, self.a, self.b = Buf(keep), Buf(keep), Buf(self.shift + 1), Buf(self.shift + 1)

    def _mid(self, n):
        return None if self.i + 1 < n else (_max(self.h, n) + _min(self.l, n)) / 2

    def step(self, b, commit=True):
        self.h.push(b.h)
        self.l.push(b.l)
        conv, base, span = self.p
        t, k, sb = self._mid(conv), self._mid(base), self._mid(span)
        sa = None if t is None or k is None else (t + k) / 2
        self.a.push(sa)
        self.b.push(sb)
        shown = lambda buf: buf.at(self.shift) if self.i >= self.shift else None   # noqa: E731
        out = [t, k, shown(self.a), shown(self.b), sa, sb]
        _done(self, commit, self.h, self.l, self.a, self.b)
        return out


# ---- volume ------------------------------------------------------------------------------------------------------------
class Obv:
    def __init__(self):
        self.i, self.v, self.pc = 0, 0.0, 0.0

    def step(self, b, commit=True):
        vol = b.v or 0
        v = 0.0 if self.i == 0 else self.v + vol if b.c > self.pc else self.v - vol if b.c < self.pc else self.v
        if commit:
            self.i, self.v, self.pc = self.i + 1, v, b.c
        return [v]


class Mfi:
    def __init__(self, n):
        self.n, self.pos, self.neg, self.i, self.pt = n, Buf(n), Buf(n), 0, 0.0

    def step(self, b, commit=True):
        typ = (b.h + b.l + b.c) / 3
        flow = typ * (b.v or 0)
        self.pos.push(flow if self.i > 0 and typ > self.pt else 0)
        self.neg.push(flow if self.i > 0 and typ < self.pt else 0)
        v = 50.0
        if self.i > 0:
            p = q = 0.0
            for k in range(min(self.i, self.n - 1), -1, -1):
                p += self.pos.at(k)
                q += self.neg.at(k)
            v = 50.0 if _zero(q) else 100 - 100 / (1 + p / q)
        if commit:
            self.pt = typ
        _done(self, commit, self.pos, self.neg)
        return [v]


class VolMa:
    def __init__(self, n):
        self.e = Ema(n)

    def step(self, b, commit=True):
        return [self.e.step(b.v or 0, commit)]


def clock_minute(t: int) -> int:
    """The minute of the day a bar starts, Chicago time."""
    d = datetime.fromtimestamp(t / 1000, CT)
    return d.hour * 60 + d.minute


class RelVol:
    """The same bar is the one starting in the same minute of the day, on a seconds chart in the same second (its own
    slot), as web/ta.js TaRelVolRun."""

    def __init__(self, n, seconds=False):
        self.n, self.seconds, self.seen = n, seconds, {}

    def step(self, b, commit=True):
        key = clock_minute(b.t) * 60 + (b.t // 1000 % 60 if self.seconds else 0)
        past = self.seen.get(key, [])
        avg = sum(past) / len(past) if past else 0
        if commit:
            self.seen[key] = (past + [b.v or 0])[-self.n:]
        return [(b.v or 0) / avg if avg > 0 else None]


# ---- levels ------------------------------------------------------------------------------------------------------------
def trade_date(t: int) -> date:
    """The CME trade date of a bar: a 17:00 CT open trades on the next day."""
    return datetime.fromtimestamp(t / 1000 + 7 * 3600, CT).date()


def period_key(rng: str, t: int, group: str | None = None) -> str:
    d = fold_trade_date(trade_date(t), group)   # a holiday halt's short session: the next trade date's (CME's daily bars)
    if rng == "Monthly":
        return d.isoformat()[:7]
    if rng != "Weekly":
        return d.isoformat()
    return (d - timedelta(days=d.weekday())).isoformat()


def _floor(h, lo, c):
    p, r = (h + lo + c) / 3, h - lo
    return [p, 2 * p - lo, p + r, p + 2 * r, 2 * p - h, p - r, p - 2 * r]


def _camarilla(h, lo, c):
    r = (h - lo) * 1.1
    return [c + r / 12, c + r / 6, c + r / 4, c + r / 2, c - r / 12, c - r / 6, c - r / 4, c - r / 2]


class Pivots:
    def __init__(self, rng, kind, group=None):
        self.rng, self.f = rng, _floor if kind == "floor" else _camarilla
        self.n, self.cur, self.lv = 7 if kind == "floor" else 8, None, None
        self.group = group   # the instrument's holiday group (sessions); None: every session a day of its own

    def step(self, b, commit=True):
        k = period_key(self.rng, b.t, self.group)
        cur, lv = self.cur, self.lv
        if cur is None or cur[0] != k:
            if cur is not None:
                lv = self.f(cur[1], cur[2], cur[3])
            cur = (k, b.h, b.l, b.c)
        else:
            cur = (k, max(cur[1], b.h), min(cur[2], b.l), b.c)
        if commit:
            self.cur, self.lv = cur, lv
        return list(lv) if lv else [None] * self.n


class Regression:
    def __init__(self, n, width):
        self.n, self.w, self.x, self.i = n, width, Buf(n), 0

    def step(self, b, commit=True):
        self.x.push(b.c)
        m = min(self.i + 1, self.n)
        sx = sy = sxy = sxx = 0.0
        for j in range(m):
            y = self.x.at(m - 1 - j)
            sx += j
            sy += y
            sxy += j * y
            sxx += j * j
        den = m * sxx - sx * sx
        slope = 0.0 if den == 0 else (m * sxy - sx * sy) / den
        icpt = (sy - slope * sx) / m
        ss = 0.0
        for j in range(m):
            r = self.x.at(m - 1 - j) - (icpt + slope * j)
            ss += r * r
        end = icpt + slope * (m - 1)
        band = self.w * math.sqrt(ss / m)
        _done(self, commit, self.x)
        return [end, end + band, end - band, slope, band, m]


# ---- the indicators: their settings (as the chart's, web/ta.js IND_DEFS) and lines ----------------------------------
# params: (key, default, low, high, kind): kind "int", a decimal step, or a tuple of choices
PIVOT_RANGE = ("range", "Daily", None, None, ("Daily", "Weekly", "Monthly"))
_P = lambda d=14, lo=1, hi=500: [("len", d, lo, hi, "int")]   # noqa: E731
_BAND = ["upper", "middle", "lower"]
IND = {
    "ema": {"name": "EMA", "params": _P(21, 1, 2000), "lines": ["v"], "make": lambda p: _close(Ema(p["len"]))},
    "sma": {"name": "SMA", "params": _P(50, 1, 2000), "lines": ["v"], "make": lambda p: _close(Sma(p["len"]))},
    "wma": {"name": "WMA", "params": _P(14), "lines": ["v"], "make": lambda p: _close(Wma(p["len"]))},
    "hma": {"name": "HMA", "params": _P(21, 2), "lines": ["v"], "make": lambda p: _close(Hma(p["len"]))},
    "vwma": {"name": "VWMA", "params": _P(14), "lines": ["v"], "make": lambda p: _bar(Vwma(p["len"]))},
    "dema": {"name": "DEMA", "params": _P(14), "lines": ["v"], "make": lambda p: _close(Dema(p["len"]))},
    "tema": {"name": "TEMA", "params": _P(14), "lines": ["v"], "make": lambda p: _close(Tema(p["len"]))},
    "smma": {"name": "SMMA", "params": _P(14), "lines": ["v"], "make": lambda p: _close(Smma(p["len"]))},
    "boll": {"name": "Bollinger", "params": _P(14) + [("dev", 2, 0.1, 10, 0.1)], "lines": _BAND,
             "make": lambda p: Bollinger(p["len"], p["dev"])},
    "kelt": {"name": "Keltner", "params": _P(10) + [("mult", 1.5, 0.1, 10, 0.1)], "lines": _BAND,
             "make": lambda p: Keltner(p["len"], p["mult"])},
    "donch": {"name": "Donchian", "params": _P(14), "lines": ["upper", "mean", "lower"], "make": lambda p: Donchian(p["len"])},
    "rsi": {"name": "RSI", "params": [("len", 14, 2, 500, "int"), ("smooth", 3, 1, 200, "int")], "lines": ["rsi", "avg"],
            "make": lambda p: _close(Rsi(p["len"], p["smooth"]), many=True)},
    "macd": {"name": "MACD", "params": [("fast", 12, 1, 500, "int"), ("slow", 26, 1, 500, "int"), ("smooth", 9, 1, 500, "int")],
             "lines": ["macd", "avg", "diff"], "make": lambda p: _close(Macd(p["fast"], p["slow"], p["smooth"]), many=True)},
    "stoch": {"name": "Stochastics", "params": [("k", 14, 1, 500, "int"), ("smooth", 3, 1, 200, "int"), ("d", 7, 1, 200, "int")],
              "lines": ["k", "d"], "make": lambda p: Stoch(p["k"], p["smooth"], p["d"])},
    "stochrsi": {"name": "StochRSI", "params": _P(14, 2), "lines": ["v"], "make": lambda p: StochRsi(p["len"])},
    "willr": {"name": "Williams %R", "params": _P(), "lines": ["v"], "make": lambda p: WilliamsR(p["len"])},
    "cci": {"name": "CCI", "params": _P(), "lines": ["v"], "make": lambda p: Cci(p["len"])},
    "mom": {"name": "Momentum", "params": _P(), "lines": ["v"], "make": lambda p: Momentum(p["len"], False)},
    "roc": {"name": "ROC", "params": _P(), "lines": ["v"], "make": lambda p: Momentum(p["len"], True)},
    "adx": {"name": "ADX", "params": _P(), "lines": ["adx", "dip", "dim"], "make": lambda p: Adx(p["len"])},
    "atr": {"name": "ATR", "params": _P(), "lines": ["v"], "make": lambda p: _bar(Atr(p["len"]))},
    "psar": {"name": "Parabolic SAR", "params": [("acc", 0.02, 0.001, 1, 0.001), ("inc", 0.02, 0.001, 1, 0.001), ("max", 0.2, 0.001, 1, 0.001)],
             "lines": ["v"], "make": lambda p: Psar(p["acc"], p["inc"], p["max"])},
    "supertrend": {"name": "SuperTrend", "params": [("len", 10, 1, 500, "int"), ("mult", 3, 0.1, 20, 0.1)], "lines": ["v"],
                   "make": lambda p: SuperTrend(p["len"], p["mult"])},
    "ichimoku": {"name": "Ichimoku", "params": [("conv", 9, 1, 500, "int"), ("base", 26, 1, 500, "int"), ("span", 52, 1, 500, "int"),
                                                ("disp", 26, 1, 500, "int")],
                 "lines": ["conv", "base", "spanA", "spanB"], "make": lambda p: Ichimoku(p["conv"], p["base"], p["span"], p["disp"])},
    "obv": {"name": "OBV", "params": [], "lines": ["v"], "make": lambda p: Obv()},
    "mfi": {"name": "MFI", "params": _P(), "lines": ["v"], "make": lambda p: Mfi(p["len"])},
    "volma": {"name": "Volume MA", "params": _P(), "lines": ["v"], "make": lambda p: VolMa(p["len"])},
    "rvol": {"name": "Rel. volume", "params": _P(10, 1, 100), "lines": ["v"], "make": lambda p: RelVol(p["len"])},
    "pivots": {"name": "Pivots", "params": [PIVOT_RANGE], "lines": ["pp", "r1", "r2", "r3", "s1", "s2", "s3"],
               "make": lambda p: Pivots(p["range"], "floor")},
    "camarilla": {"name": "Camarilla", "params": [PIVOT_RANGE], "lines": ["r1", "r2", "r3", "r4", "s1", "s2", "s3", "s4"],
                  "make": lambda p: Pivots(p["range"], "camarilla")},
    "linreg": {"name": "Regression", "params": [("len", 35, 2, 1000, "int"), ("width", 2, 0.1, 10, 0.1)], "lines": ["middle", "upper", "lower"],
               "make": lambda p: Regression(p["len"], p["width"])},
}


# Each line's name, as the chart's (IND_DEFS lines); a line named as its indicator is just the indicator's name.
LABELS = {
    **{k: [IND[k]["name"]] for k in ("ema", "sma", "wma", "hma", "vwma", "dema", "tema", "smma", "mom", "roc", "atr", "obv", "mfi",
                                     "volma", "rvol", "cci", "stochrsi")},
    "boll": ["Upper", "Middle", "Lower"], "kelt": ["Upper", "Middle", "Lower"], "donch": ["Upper", "Mean", "Lower"],
    "rsi": ["RSI", "Avg"], "macd": ["MACD", "Signal", "Histogram"], "stoch": ["%K", "%D"], "willr": ["%R"], "adx": ["ADX", "+DI", "-DI"],
    "psar": ["SAR"], "supertrend": ["SuperTrend"], "ichimoku": ["Conversion", "Base", "Span A", "Span B"],
    "pivots": ["PP", "R1", "R2", "R3", "S1", "S2", "S3"], "camarilla": ["R1", "R2", "R3", "R4", "S1", "S2", "S3", "S4"],
    "linreg": ["Middle", "Upper", "Lower"],
}


class _close:
    """A runner of closes (EMA, SMA, ...) as one of bars; many: it returns several outputs already."""

    def __init__(self, run, many=False):
        self.run, self.many = run, many

    def step(self, b, commit=True):
        v = self.run.step(b.c, commit)
        return list(v) if self.many else [v]


class _bar:
    def __init__(self, run):
        self.run = run

    def step(self, b, commit=True):
        return [self.run.step(b, commit)]


def clean_value(kind, v, lo, hi, what: str):
    """One setting, checked: a whole number, a decimal on its step, or one of its choices; ValueError otherwise."""
    if isinstance(kind, tuple):
        if v not in kind:
            raise ValueError(f"{what}: one of {', '.join(kind)}")
        return v
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"{what}: {'a whole number' if kind == 'int' else 'a number'} {lo}-{hi}")
    if kind == "int":
        if v != int(v):
            raise ValueError(f"{what}: a whole number {lo}-{hi}")
        return int(v)
    dec = max(0, math.ceil(-math.log10(kind) - 1e-9))
    return round(math.floor(v / kind + 0.5) * kind, dec)


def runner(ind: str, params: dict, group: str | None = None, seconds: bool = False):
    """An indicator's runner with its settings; group: the instrument's holiday group, whose halts the pivots' days fold
    into the next trade date, as the chart's (web/ta.js setSessionHolidays); seconds: the bars are seconds bars."""
    run = IND[ind]["make"](params)
    if isinstance(run, Pivots):
        run.group = group
    if isinstance(run, RelVol):
        run.seconds = seconds
    return run
