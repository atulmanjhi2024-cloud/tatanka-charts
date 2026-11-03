"""FRED (Federal Reserve Bank of St. Louis) economic series: rates, yields, spreads, inflation, jobs, money, the Fed's
balance sheet. A watchlist symbol "FRED:DGS10" shows the latest value and its change from the one before; the chart
draws the series. The data is FRED's public CSV download, no account or key; each series is kept in the database
and asked again after FRESH_S (FRED publishes at most once a day)."""
import asyncio
import json
import logging
import re
import time
from datetime import date

import httpx

from .. import stopping
from ..sessions import day_open

log = logging.getLogger(__name__)
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
ID_RE = re.compile(r"^[A-Z0-9_]{1,40}$")
FRESH_S = 3 * 3600
QUOTE_EVERY_S = 900
PREFIX = "FRED:"


def series_id(symbol: str) -> str | None:
    """"FRED:DGS10" -> "DGS10", else None."""
    if not symbol.upper().startswith(PREFIX):
        return None
    sid = symbol[len(PREFIX):].upper()
    return sid if ID_RE.match(sid) else None


def parse_csv(text: str) -> tuple[list[tuple[str, float]], int]:
    """FRED's CSV ("observation_date,DGS10" or the older "DATE,DGS10"; a missing value is "" or "."): the
    observations, oldest first, as (YYYY-MM-DD, value), and the decimals it is published with."""
    lines = text.strip().splitlines()
    if not lines or not re.match(r"^\s*(observation_date|date)\s*,", lines[0], re.I):
        raise ValueError("not a FRED series")
    out, dec = [], 0
    for ln in lines[1:]:
        d, _, v = ln.partition(",")
        d, v = d.strip(), v.strip()
        if not v or v == "." or not re.match(r"^\d{4}-\d\d-\d\d$", d):
            continue
        try:
            out.append((d, float(v)))
        except ValueError:
            continue
        if "." in v:
            dec = max(dec, min(4, len(v.split(".", 1)[1])))
    return out, dec


class NotReady(ValueError):
    """The source can't be asked yet (no API key): not remembered as a missing series."""


class SeriesData:
    """Economic series kept in the database (one table per source), asked again after FRESH_S; the stored copy when
    the source can't be reached. A subclass fetches: _fetch(id) -> (observations, decimals)."""
    name, table, id_re = "", "", ID_RE

    def __init__(self, store, http: httpx.AsyncClient | None):
        self.store, self.http = store, http
        store.db.executescript(f"CREATE TABLE IF NOT EXISTS {self.table} (id TEXT PRIMARY KEY, fetched INTEGER NOT NULL, "
                               "decimals INTEGER NOT NULL, data TEXT NOT NULL) WITHOUT ROWID;")
        self.locks: dict[str, asyncio.Lock] = {}
        self.failed: dict[str, float] = {}   # id -> when the source last said it has no such series

    def cached(self, sid: str) -> tuple[list, int, float] | None:
        r = self.store.db.execute(f"SELECT fetched, decimals, data FROM {self.table} WHERE id=?", (sid,)).fetchone()
        return (json.loads(r[2]), r[1], r[0] / 1000) if r else None

    async def _fetch(self, sid: str) -> tuple[list[tuple[str, float]], int]:
        raise NotImplementedError

    async def series(self, sid: str, max_age_s: float = FRESH_S) -> tuple[list[tuple[str, float]], int]:
        """The observations (YYYY-MM-DD, value), oldest first, and decimals. ValueError for an id it doesn't have."""
        if not self.id_re.match(sid):
            raise ValueError(f"not a {self.name} series id: {sid!r}")
        async with self.locks.setdefault(sid, asyncio.Lock()):
            have = self.cached(sid)
            if have and time.time() - have[2] < max_age_s:
                return [tuple(x) for x in have[0]], have[1]
            if not have and time.time() - self.failed.get(sid, 0) < FRESH_S:
                raise ValueError(f"{self.name} has no series {sid}")
            if self.http is None:
                if have:
                    return [tuple(x) for x in have[0]], have[1]
                raise ValueError(f"{self.name} is not reachable from here")
            try:
                obs, dec = await self._fetch(sid)
                if not obs:
                    raise ValueError(f"{self.name} has no data for {sid}")
            except NotReady:
                if have:
                    return [tuple(x) for x in have[0]], have[1]
                raise
            except ValueError as e:
                if have:   # a stored series exists: a bad answer (a page that isn't the data, an empty one) is a glitch
                    log.info("%s %s not refreshed (%s): the stored copy", self.name, sid, e)
                    return [tuple(x) for x in have[0]], have[1]
                self.failed[sid] = time.time()
                raise
            except httpx.HTTPError as e:
                if have:
                    log.info("%s %s not refreshed (%s): the stored copy", self.name, sid, type(e).__name__)
                    return [tuple(x) for x in have[0]], have[1]
                raise ValueError(f"{self.name} did not answer ({type(e).__name__})") from None
            with self.store.db:
                self.store.db.execute(f"INSERT OR REPLACE INTO {self.table} (id, fetched, decimals, data) VALUES (?,?,?,?)",
                                      (sid, int(time.time() * 1000), dec, json.dumps(obs, separators=(",", ":"))))
            return obs, dec


class FredData(SeriesData):
    name, table = "FRED", "fred_series"

    async def _fetch(self, sid: str):
        r = await self.http.get(CSV_URL, params={"id": sid}, timeout=30, follow_redirects=True,
                                headers={"User-Agent": "Mozilla/5.0 (tatanka-charts)"})
        if r.status_code in (400, 404):
            raise ValueError(f"FRED has no series {sid}")
        r.raise_for_status()
        return parse_csv(r.text)


def obs_ms(d: str) -> int:
    """An observation date stamped as the app stamps a day: 17:00 CT the evening before (the session trading on it)."""
    return day_open(date.fromisoformat(d))


def source_of(market, key: str):
    """(the series store, its id, source name) for a FRED, EIA or DATA: symbol, else None."""
    from .eia import series_id as eia_id
    if sid := series_id(key):
        return market.fred, sid, "fred"
    if (sid := eia_id(key)) and getattr(market, "eia", None):
        return market.eia, sid, "eia"
    if key[:5].upper() == "DATA:" and hasattr(market, "data_source"):   # a data plugin's series (Connections > Data)
        return market.data_source(key[5:].upper())
    return None


class FredQuotes:
    """Watchlist values for FRED and EIA symbols: the latest observation, and its change from the one before."""
    name = "fred"

    def __init__(self):
        self.changed = asyncio.Event()
        self.stats = {"ok": 0, "errors": 0, "symbols": 0, "last_error": ""}

    def watch_changed(self, market):
        self.changed.set()

    async def run(self, market):
        while True:
            self.changed.clear()
            keys = sorted(k for k in market.watch_keys if source_of(market, k))
            self.stats["symbols"] = len(keys)
            for k in keys:
                data, sid, src = source_of(market, k)
                try:
                    obs, _ = await data.series(sid)
                    at = obs_ms(obs[-1][0]) + 7 * 3_600_000
                except (TypeError, ValueError) as e:   # one symbol's (or a bad date a data plugin's copy kept): the rest go on
                    self.stats["errors"] += 1
                    self.stats["last_error"] = str(e)[:200]
                    continue
                last, prev = obs[-1][1], (obs[-2][1] if len(obs) > 1 else None)
                market.on_quote(k, last, prev, None, source=src, at=at)
                self.stats["ok"] += 1
            stopping.check()   # a stop a request swallowed (stopping.py) ends it here, not after the wait
            try:
                await asyncio.wait_for(self.changed.wait(), QUOTE_EVERY_S)
            except asyncio.TimeoutError:
                pass
