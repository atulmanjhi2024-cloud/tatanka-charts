"""The watchlist's details panel: today's open, high and low, the 52-week range and the performance over 1W, 1M, 3M,
YTD and 1Y; for a live future also its contract and how it rolls; for a stock its next earnings date. From what the server already has: its own minutes for a live future's day, Yahoo's daily
bars (cached a few minutes) for the rest."""
import logging
from datetime import date, timedelta

from . import sessions
from . import watchlists as wl
from .feeds.yahoo import yahoo_symbol

log = logging.getLogger(__name__)
PERIODS = (("1W", 7), ("1M", 30), ("3M", 91), ("1Y", 365))   # calendar days back
EARNINGS_DAYS = 90


def _trade_date(t: int) -> date:   # a daily bar is stamped at its session's 17:00 CT open, the evening before
    return sessions.to_ct(t + 12 * 3_600_000).date()


def _day_live(m, sym: str, now: int) -> dict | None:
    """Today's session so far, or the last one (a weekend, the break, a closed holiday), from this server's own minutes
    (open, high, low)."""
    m.flush_for_read()
    start = sessions.previous_session_start(now + 1)
    row = m.store.db.execute("SELECT MIN(l), MAX(h) FROM bars_1m WHERE symbol=? AND t>=?", (sym, start)).fetchone()
    first = m.store.db.execute("SELECT o FROM bars_1m WHERE symbol=? AND t>=? ORDER BY t LIMIT 1", (sym, start)).fetchone()
    if not first or row[0] is None:
        return None
    return {"o": first[0], "h": row[1], "l": row[0]}


def _from_daily(rows: list[dict], last: float | None, today: date) -> dict:
    """52-week range and performance from daily bars (oldest first), against `last` (the quote) or the last close."""
    if not rows:
        return {}
    days = [(_trade_date(r["t"]), r) for r in rows]
    price = last if last is not None else days[-1][1]["c"]
    year = [r for d, r in days if d > today - timedelta(days=365)]
    out = {"w52": [min(r["l"] for r in year), max(r["h"] for r in year)] if year else None, "perf": {}}

    def close_on_or_before(d: date):
        for dd, r in reversed(days):
            if dd <= d:
                return r["c"]
        return None
    for label, back in PERIODS:
        c = close_on_or_before(today - timedelta(days=back))
        if c:
            out["perf"][label] = round((price - c) / abs(c) * 100, 2)   # of c's size: a rise from below 0 is up
    c = close_on_or_before(date(today.year - 1, 12, 31))
    if c:
        out["perf"]["YTD"] = round((price - c) / abs(c) * 100, 2)
    out["perf"] = {k: out["perf"][k] for k in ("1W", "1M", "3M", "YTD", "1Y") if k in out["perf"]}
    return out


async def details(m, raw: str, now: int) -> dict:
    toks = wl.symbols_in(raw)
    if len(toks) != 1 or any(ch in wl.bare(raw) for ch in "+-*/()"):
        return {"symbol": wl.bare(raw)}   # a formula: its parts have their own
    key = wl.quote_key(toks[0], m.instruments)   # ZS1! the future, ZS the stock; ES (charted here) ES1!
    root = wl.future_root(key)
    out: dict = {"symbol": key}
    live = root in m.instruments
    q = m.quotes.get(key) or {}
    if not live and q.get("src") == "sim":   # the simulator's invented price, not one to set against Yahoo's real closes
        q = {}
    tick = m.last_tick.get(root) if live else None
    last = tick.price if tick else q.get("last")
    if live:
        inst = m.instruments[root]
        out["day"] = _day_live(m, root, now)
        out["contract"] = m.contracts.get(root)
        out["roll"] = inst.contract
    if live:
        ticker = yahoo_symbol(key)   # its front month there: RTY=F
    elif key.startswith(("FRED:", "EIA:", "DATA:")):
        ticker = None   # a series: its own dates, no day or 52-week range
    else:
        ticker = m.ext.yahoo_ticker(toks[0])
    if ticker:
        try:
            rows, _ = await m.ext._yahoo(ticker, "1D")
            today = sessions.to_ct(now).date()
            out.update(_from_daily(rows, last, today))
            # today's, as Yahoo has it (delayed): a stock's until midnight, a future's from its 17:00 CT open
            if not live and rows and _trade_date(rows[-1]["t"]) in (today, sessions.to_ct(now + 7 * 3_600_000).date()):
                r = rows[-1]
                out["day"] = {"o": r["o"], "h": r["h"], "l": r["l"]}
        except Exception as e:   # the panel shows what it has
            log.info("details for %s: no daily bars (%s)", key, e if isinstance(e, ValueError) else type(e).__name__)
    if not live:
        nxt = sorted((e for e in m.earnings.between(now, now + EARNINGS_DAYS * 86_400_000) if e.get("symbol") == key),
                     key=lambda e: e["t"])   # the major companies' (Calendar > Earnings), as the chart's event lines
        if nxt:
            out["earnings"] = nxt[0]["day"]
    return out
