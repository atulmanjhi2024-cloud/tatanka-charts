"""Charts of a formula of symbols, as the watchlist quotes one (watchlist.js wlEval): "VIXM/VIXY", "VIX9D/VIX",
"ES1!/NQ1!", "AAPL+MSFT", "FRED:DGS10-FRED:DGS2", "(GC1!/SI1!)*100". + - * / and parentheses over symbols and numbers.

Each symbol's bars at the chart's timeframe, the formula worked out bar by bar on their opens and on their closes. A
future charted live here comes from its own chart's series (the stored trades); any other symbol from what a
free-data chart uses (ext.py: IBKR, Alpaca, Yahoo, FRED, EIA). The first symbol's bars set the times; another's
value at each is its bar opening then, else its last close by then (a stock's 09:30 hour against a future's 09:00, a
holiday one side has): never a close from after the moment. Daily, weekly and monthly bars meet by their trade date,
week or month (a live RTH day is stamped at its RTH open, a free-data day at 17:00 CT the evening before). High and low
are the bar's open and close (the parts' highs don't come at the same moment), as TradingView draws a spread. No
volume. A live future's root in a formula is its chart's symbol: ES/NQ is ES1!/NQ1! (canonical)."""
import asyncio
import bisect
import math
import re
import time
from datetime import timedelta

from . import amt
from . import watchlists as wl
from .amt_runner import ct
from .bars import parse_spec
from .ext import CME, FRED_SPECS, YAHOO_SPECS, split_spec

TOKENS = re.compile(r"(?:FRED:|EIA:|DATA:)?[A-Za-z0-9_.!&^=]+|[+\-*/()]")
SYMBOL = re.compile(r"(?:FRED:|EIA:|DATA:)?[A-Za-z0-9_.!&^=]+")
MAX_SYMBOLS = 10
SIG_DIGITS = 6


def is_formula(sym: str) -> bool:
    return any(ch in wl.bare(sym) for ch in "+-*/()")


def compile_formula(expr: str):
    """A function of a value per symbol (a dict) giving the formula's value, NaN when a part has none or it divides by
    0; ValueError when it doesn't read as one (the watchlist's own rules: wlEval)."""
    toks = TOKENS.findall(wl.bare(expr))
    if "".join(toks) != re.sub(r"\s+", "", wl.bare(expr)):
        raise ValueError(f"not a formula: {expr[:60]!r}")
    pos = 0

    def atom():
        nonlocal pos
        if pos >= len(toks):
            raise ValueError("the formula ends too soon")
        t = toks[pos]
        pos += 1
        if t == "(":
            f = add()
            if pos >= len(toks) or toks[pos] != ")":
                raise ValueError("a ( without its )")
            pos += 1
            return f
        if t == "-":
            f = atom()
            return lambda v: -f(v)
        if t == "+":
            return atom()
        if t in "*/)":
            raise ValueError(f"{t} where a symbol or number goes")
        if wl.NUMBER_RE.match(t):
            x = float(t)
            return lambda v: x
        return lambda v, t=t: v.get(t, math.nan)

    def mul():
        nonlocal pos
        f = atom()
        while pos < len(toks) and toks[pos] in "*/":
            op = toks[pos]
            pos += 1
            g, h = f, atom()
            f = (lambda v, g=g, h=h: g(v) * h(v)) if op == "*" else (lambda v, g=g, h=h: _div(g(v), h(v)))
        return f

    def add():
        nonlocal pos
        f = mul()
        while pos < len(toks) and toks[pos] in "+-":
            op = toks[pos]
            pos += 1
            g, h = f, mul()
            f = (lambda v, g=g, h=h: g(v) + h(v)) if op == "+" else (lambda v, g=g, h=h: g(v) - h(v))
        return f

    f = add()
    if pos != len(toks):
        raise ValueError(f"not a formula: {expr[:60]!r}")
    return f


def _div(a: float, b: float) -> float:
    return a / b if b else math.nan


def symbols(expr: str) -> list[str]:
    """Its symbols, each once, in the order written (the first sets the bars' times)."""
    out = []
    for t in wl.symbols_in(expr):
        if t not in out:
            out.append(t)
    return out


def canonical(expr: str, instruments) -> str:
    """A live chart's bare root in a formula as its chart's symbol (ES/NQ -> ES1!/NQ1!): one formula, one chart."""
    return SYMBOL.sub(lambda m: f"{m.group(0)}1!" if m.group(0) in instruments else m.group(0), expr)


def live_root(token: str, instruments) -> str | None:
    """The live chart a symbol is (ES1! or ES for the ES chart), else None."""
    root = wl.future_root(token) or token
    return root if root in instruments and ":" not in token else None


def calendar_key(spec):
    """Daily, weekly and monthly bars: a bar's trade date, week or month (their stamps differ by source); None for
    intraday bars (their times meet as they are)."""
    if not spec.cal:
        return None

    def day(t: int):
        return amt.trading_date(ct(t))
    if spec.cal == "W":
        return lambda t: (day(t) - timedelta(days=day(t).weekday())).toordinal()
    if spec.cal == "M":
        return lambda t: day(t).year * 12 + day(t).month
    return lambda t: day(t).toordinal()


def combine(formula, names: list[str], parts: dict[str, list[dict]], period: int = 0, key=None) -> list[dict]:
    """The formula's bars from each symbol's ({t, o, c, ...}, oldest first): at the first symbol's times, the others
    as of then. period: an intraday bar's length (ms); key: a calendar bar's period (calendar_key). Another symbol's bar
    opening at the same time (period) gives its open and close; else its value at the open is the close of its last bar
    over by then (else the open of the one running, its first), at the close its last bar's close."""
    k = key or (lambda t: t)
    first, rest = names[0], names[1:]
    keys = {n: [k(b["t"]) for b in parts[n]] for n in rest}
    out = []
    for b in parts[first]:
        kb = k(b["t"])
        o, c = {first: b["o"]}, {first: b["c"]}
        for n in rest:
            ks, bars = keys[n], parts[n]
            i = bisect.bisect_right(ks, kb) - 1
            if i < 0:
                break   # it has no bar yet: none from it before its first
            x = bars[i]
            if ks[i] == kb:
                o[n], c[n] = x["o"], x["c"]
                continue
            c[n] = x["c"]   # it began before this bar (as long, or a period before): over by this one's end
            j = i if key is not None or x["t"] + period <= b["t"] else i - 1   # still running at this bar's open
            o[n] = bars[j]["c"] if j >= 0 else x["o"]
        else:
            vo, vc = formula(o), formula(c)
            if math.isfinite(vo) and math.isfinite(vc):
                out.append({"t": b["t"], "o": vo, "h": max(vo, vc), "l": min(vo, vc), "c": vc, "v": 0})
    return out


def decimals_for(x: float) -> int:
    """Decimals for SIG_DIGITS significant digits of a value this size (at least 2, at most 10): a ratio's small moves
    show (ES1!/NQ1! 0.276050), a big value keeps 2."""
    a = abs(x)
    if not a or not math.isfinite(a):
        return 2
    return min(10, max(2, SIG_DIGITS - 1 - math.floor(math.log10(a))))


async def formula_bars(market, expr: str, spec: str, rth: bool = True) -> dict:
    """What /api/ext/bars answers for a single symbol (ext.ExtBars.bars), for a formula; ValueError naming the part
    that has no bars."""
    formula = compile_formula(expr)
    names = symbols(expr)
    if not names:
        raise ValueError("a formula needs a symbol")
    if len(names) > MAX_SYMBOLS:
        raise ValueError(f"a formula of up to {MAX_SYMBOLS} symbols")
    split_spec(spec)   # time bars only: what free-data charts take
    sp = parse_spec(spec)
    series = any(n.startswith(("FRED:", "EIA:", "DATA:")) for n in names)
    if series and spec not in FRED_SPECS:
        raise ValueError(f"a formula with an economic series is daily or longer: {', '.join(FRED_SPECS)}")

    async def one(name: str) -> tuple[list[dict], dict]:
        root = live_root(name, market.instruments)
        try:
            if root is not None:
                inst = market.instruments[root]
                _, entry = market._entry(root, sp, bool(rth and inst.rth))
                entry.idle_since = time.monotonic()   # in use: not let go as an idle chart's while the formula polls
                await entry.ready.wait()
                if entry.error:
                    raise ValueError(entry.error)
                entry.idle_since = time.monotonic()
                return [b.to_dict() for b in entry.series.bars], {**CME, "rth": list(inst.rth) if inst.rth else None}
            got = await market.ext.bars(name, spec, rth)
            return got["bars"], got["exchange"]
        except ValueError as e:
            raise ValueError(f"{name}: {e}") from None

    got = await asyncio.gather(*(one(n) for n in names))
    parts = {n: g[0] for n, g in zip(names, got)}
    for n in names:
        if not parts[n]:
            raise ValueError(f"{n}: no bars")
    bars = combine(formula, names, parts, sp.size * 1000, calendar_key(sp))
    if not bars:
        if all(parts[n][-1]["t"] >= parts[names[0]][0]["t"] for n in names[1:]):   # they overlap: it has no value there
            raise ValueError(f"{expr} has no value on these bars (a division by 0?)")
        raise ValueError(f"no bars where every part of {expr} has one")
    dec = decimals_for(max(max(abs(b["o"]), abs(b["c"])) for b in bars))
    for i, b in enumerate(bars):
        b["seq"] = i + 1
        for k in "ohlc":
            b[k] = round(b[k], dec)
    return {"symbol": expr, "spec": spec, "source": "formula", "name": expr, "type": "formula", "decimals": dec,
            "tick": round(10 ** -dec, dec), "specs": list(FRED_SPECS if series else YAHOO_SPECS), "bars": bars,
            "feed": "", "exchange": got[0][1], "custom": not series, "parts": names,
            "line": all(n.startswith(("FRED:", "EIA:", "DATA:")) for n in names)}   # values only: a line
