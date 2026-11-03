"""Major earnings for the Calendar tab and the chart's event lines: the next two weeks' reports from Nasdaq's public
earnings calendar (the JSON behind nasdaq.com/market-activity/earnings; no account), one request per weekday.

"Major" is a market-cap floor (default $200B: the Magnificent 7, the big banks, ...) plus any tickers added by hand.
Reports come before the open (BMO) or after the close (AMC); Nasdaq gives no exact time, so the calendar places them
at 06:00 CT and 15:05 CT (just after the cash close), and at midnight when it doesn't say."""
import asyncio  # noqa: F401  (the tests reach asyncio.sleep through it; stopping.pause sleeps with it)
import logging
import re
import sqlite3
import time
from datetime import date, datetime, timedelta

import httpx

from . import stopping
from .sessions import CT

log = logging.getLogger(__name__)
URL = "https://api.nasdaq.com/api/calendar/earnings"
HEADERS = {   # Nasdaq turns away requests that don't look like its own page's
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
REFRESH_S = 6 * 3600
AHEAD_DAYS = 14
KEEP_CAP = 5e9          # rows under this are not kept (the floor can be lowered to it without asking again)
WHEN = {"time-pre-market": "BMO", "time-after-hours": "AMC"}
AT = {"BMO": (6, 0), "AMC": (15, 5), "": (0, 0)}
DEFAULTS = {"on": True, "min_cap_b": 200, "symbols": []}
_SYM = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _money(s) -> float | None:
    """'$1,234,567' -> 1234567.0; '($0.05)' -> -0.05; '', 'N/A' -> None."""
    s = str(s or "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse(doc, day: date) -> list[dict]:
    """One day's rows: [{id, t (epoch ms), day, symbol, name, when, cap, eps, ests, quarter}]."""
    rows = ((doc or {}).get("data") or {}).get("rows") or [] if isinstance(doc, dict) else []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").strip().upper()
        if not _SYM.match(sym):
            continue
        when = WHEN.get(str(r.get("time") or ""), "")
        h, m = AT[when]
        t = int(datetime(day.year, day.month, day.day, h, m, tzinfo=CT).timestamp() * 1000)
        ests = str(r.get("noOfEsts") or "").strip()
        eps = _money(r.get("epsForecast"))
        out.append({"id": f"{sym}|{day.isoformat()}", "t": t, "day": day.isoformat(), "symbol": sym,
                    "name": str(r.get("name") or "")[:80], "when": when, "cap": _money(r.get("marketCap")),
                    "eps": None if eps is None else round(eps, 4), "ests": int(ests) if ests.isdigit() else None,
                    "quarter": str(r.get("fiscalQuarterEnding") or "")[:12]})
    return out


def clean_settings(d: dict) -> dict:
    if not isinstance(d, dict):
        raise ValueError("expected {on, min_cap_b, symbols}")
    out = {**DEFAULTS}
    if "on" in d:
        out["on"] = bool(d["on"])
    if "min_cap_b" in d:
        v = d["min_cap_b"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 5 <= v <= 10_000:
            raise ValueError("min_cap_b: 5 to 10000 (billions of dollars)")
        out["min_cap_b"] = round(float(v), 1)
    if "symbols" in d:
        s = d["symbols"]
        if not isinstance(s, list) or len(s) > 100 or not all(isinstance(x, str) and _SYM.match(x.strip().upper()) for x in s):
            raise ValueError("symbols: tickers like ['AAPL', 'NVDA']")
        out["symbols"] = sorted({x.strip().upper() for x in s})
    return out


def major(rows: list[dict], cfg: dict) -> list[dict]:
    """The rows the settings ask for: over the market-cap floor, or one of the added tickers."""
    if not cfg["on"]:
        return []
    floor, extra = cfg["min_cap_b"] * 1e9, set(cfg["symbols"])
    return [r for r in rows if r["symbol"] in extra or (r["cap"] or 0) >= floor]


class Earnings:
    def __init__(self, market):
        self.m = market
        self.rows: list[dict] = list(market.store.get_setting("earnings_rows", []) or [])
        self.cfg = clean_settings({**DEFAULTS, **(market.store.get_setting("earnings_cfg", {}) or {})})
        self.fetched = 0.0
        self.error = ""
        self.missed: list[str] = []   # the days the last refresh could not read (their rows kept from before)

    def put_settings(self, d: dict) -> dict:
        old, self.cfg = self.cfg, clean_settings({**self.cfg, **d})
        self.m.store.set_setting("earnings_cfg", self.cfg)
        if (self.cfg["on"] and not old["on"]) or set(self.cfg["symbols"]) - set(old["symbols"]):
            self.fetched = 0.0   # turned on, or a ticker under the kept floor: read again within a minute
        return self.cfg

    def between(self, start: int, end: int) -> list[dict]:
        return [r for r in major(self.rows, self.cfg) if start <= r["t"] < end]

    async def refresh(self) -> int:
        today = datetime.now(CT).date()
        got, failed = {}, []
        for i in range(AHEAD_DAYS + 1):
            day = today + timedelta(days=i)
            if day.weekday() >= 5:
                continue
            try:
                r = await self.m.http.get(URL, params={"date": day.isoformat()}, headers=HEADERS, timeout=20)
                r.raise_for_status()
                for row in parse(r.json(), day):
                    if (row["cap"] or 0) >= KEEP_CAP or row["symbol"] in self.cfg["symbols"]:
                        got[row["id"]] = row
            except (httpx.HTTPError, ValueError) as e:
                failed.append(day.isoformat())
                if len(failed) >= 3 and not got:
                    raise
                log.info("earnings: %s not read (%s)", day, type(e).__name__)
            await stopping.pause(1)   # gently: one day at a time (none after a stop it swallowed: stopping.py)
        if got:   # the past two weeks' stay for the chart's lines, and a day not read now keeps what it had
            cut = int(time.time() * 1000) - 14 * 86_400_000
            start = int(datetime(today.year, today.month, today.day, tzinfo=CT).timestamp() * 1000)
            keep = {r["id"]: r for r in self.rows if cut < r["t"] < start or r["day"] in failed}
            self.rows = sorted({**keep, **got}.values(), key=lambda r: (r["t"], -(r["cap"] or 0)))
            self.m.store.set_setting("earnings_rows", self.rows)
        self.missed = failed
        return len(got)

    async def run(self):
        if not getattr(self.m.s, "symbols_online", True):   # CHART_SYMBOLS_ONLINE=0: no online refreshes
            return
        while True:
            if self.cfg["on"] and time.time() - self.fetched > REFRESH_S and self.m.http is not None:
                try:
                    n = await self.refresh()
                    self.fetched, self.error = time.time(), ""
                    if self.missed:   # some days not read: the tab says so, and they are asked again in 30 minutes
                        self.fetched -= REFRESH_S - 1800
                        self.error = f"{len(self.missed)} day{'s' if len(self.missed) > 1 else ''}"
                    self.m.broadcast({"type": "calendar"})
                    log.info("earnings: %d reports", n)
                except (httpx.HTTPError, ValueError, sqlite3.Error) as e:   # sqlite3: not saved (a full disk)
                    self.fetched = time.time() - REFRESH_S + 1800   # try again in 30 minutes
                    self.error = f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
                    log.warning("earnings not read (%s)", type(e).__name__)
            await stopping.pause(60)   # a stop the request that made it give up swallowed: ended here (stopping.py)
