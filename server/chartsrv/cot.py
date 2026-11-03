"""CFTC Commitments of Traders, the Legacy report (futures only): each week's net positions of commercials (hedgers),
non-commercials (large speculators) and non-reportables (small traders), and the open interest. From the CFTC's
public reporting site, no account or key. Positions are as of Tuesday and published Friday 3:30 pm ET; each week is
stamped at its publication, so a chart never shows a week before it was known. Kept in the database; asked again
when a new week should be out."""
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .watchlists import future_root, quote_key

log = logging.getLogger(__name__)
URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
ET = ZoneInfo("America/New_York")
FRESH_S = 6 * 3600
# Futures root -> CFTC contract market code (the code stays when a market is renamed). config/cot.json adds or
# replaces any: {"codes": {"MYROOT": "123456"}}.
CODES = {
    "ES": "13874A", "MES": "13874A", "NQ": "209742", "MNQ": "209742", "RTY": "239742", "M2K": "239742",
    "YM": "124603", "MYM": "124603", "VX": "1170E1", "BTC": "133741", "MBT": "133741",
    "CL": "067651", "MCL": "067651", "QM": "067651", "NG": "023651", "QG": "023651",
    "GC": "088691", "MGC": "088691", "SI": "084691", "SIL": "084691", "HG": "085692", "MHG": "085692",
    "ZT": "042601", "ZF": "044601", "ZN": "043602", "ZB": "020601",
    "6E": "099741", "M6E": "099741", "6J": "097741", "6B": "096742", "6A": "232741", "6C": "090741", "6S": "092741",
    "6M": "095741", "ZC": "002602", "ZS": "005602", "ZW": "001602", "LE": "057642", "HE": "054642",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS cot_series (code TEXT PRIMARY KEY, fetched INTEGER NOT NULL, market TEXT NOT NULL,
    data TEXT NOT NULL) WITHOUT ROWID;
"""


def released(day: date) -> int:
    """Epoch ms the week with positions as of `day` (a Tuesday) was published: that Friday, 3:30 pm ET."""
    fri = day + timedelta(days=(4 - day.weekday()) % 7)
    return int(datetime(fri.year, fri.month, fri.day, 15, 30, tzinfo=ET).timestamp() * 1000)


def _field(row: dict, pattern: str) -> float | None:
    """A row's number whose column name matches (the dataset's names carry a few typos and variants)."""
    rx = re.compile(pattern)
    for k, v in row.items():
        if rx.fullmatch(k):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def parse_rows(rows: list) -> tuple[list[dict], str]:
    """The CFTC's rows (any order) as weeks, oldest first: {t (published), day, comm, large, small, oi}, and the
    market's name."""
    out, market = {}, ""
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        d = str(r.get("report_date_as_yyyy_mm_dd", ""))[:10]
        try:
            day = date.fromisoformat(d)
        except ValueError:
            continue
        nums = {k: _field(r, p) for k, p in {
            "cl": r"comm_positions_long_all", "cs": r"comm_positions_short_all",
            "ll": r"noncomm_positions_long_all", "ls": r"noncomm_positions_short_all",
            "sl": r"nonrept_positions_long_all", "ss": r"nonrept_positions_short_all", "oi": r"open_interest_all"}.items()}
        if any(v is None for v in nums.values()):
            continue
        market = r.get("market_and_exchange_names") or market
        out[d] = {"t": released(day), "day": d, "comm": nums["cl"] - nums["cs"], "large": nums["ll"] - nums["ls"],
                  "small": nums["sl"] - nums["ss"], "oi": nums["oi"]}
    return [out[k] for k in sorted(out)], str(market)


class Cot:
    def __init__(self, store, http: httpx.AsyncClient | None, config_dir=None, instruments=()):
        self.store, self.http = store, http
        self.instruments = instruments   # the chart futures running here: each one's bare root is that future
        store.db.executescript(SCHEMA)
        self.codes = dict(CODES)
        if config_dir and (config_dir / "cot.json").exists():
            try:
                self.codes.update({k.upper(): str(v) for k, v in json.loads((config_dir / "cot.json").read_text(encoding="utf-8")).get("codes", {}).items()})
            except (ValueError, AttributeError):
                log.warning("config/cot.json is not valid; ignored")

    def code_for(self, symbol: str) -> str | None:
        """A future's code: "CBOT:ZN1!" is ZN's; a bare root only while it charts live here ("ZN"), else it is a stock
        (ZS: Zscaler, which has no report)."""
        return self.codes.get(future_root(quote_key(symbol.upper().split(":")[-1], self.instruments)))

    async def weeks(self, symbol: str) -> dict:
        """{symbol, code, market, rows}; ValueError for a market the CFTC doesn't report (or that has no code here)."""
        code = self.code_for(symbol)
        if not code:
            raise ValueError(f"no COT report for {symbol} (config/cot.json can name its CFTC code)")
        r = self.store.db.execute("SELECT fetched, market, data FROM cot_series WHERE code=?", (code,)).fetchone()
        rows, market = (json.loads(r[2]), r[1]) if r else ([], "")
        stale = not r or (time.time() - r[0] / 1000 > FRESH_S and (not rows or time.time() * 1000 - rows[-1]["t"] > 7 * 86_400_000))
        if stale and self.http is not None:
            try:
                got = await self.http.get(URL, params={"cftc_contract_market_code": code, "$order": "report_date_as_yyyy_mm_dd",
                                                       "$limit": 5000, "$where": "report_date_as_yyyy_mm_dd >= '2006-01-01'"},
                                          timeout=30, headers={"Accept": "application/json"})
                got.raise_for_status()
                new, name = parse_rows(got.json())
                if new:
                    rows, market = new, name
                with self.store.db:
                    self.store.db.execute("INSERT OR REPLACE INTO cot_series (code, fetched, market, data) VALUES (?,?,?,?)",
                                          (code, int(time.time() * 1000), market, json.dumps(rows, separators=(",", ":"))))
            except (httpx.HTTPError, ValueError) as e:
                log.info("COT %s not refreshed (%s)", code, type(e).__name__)
        if not rows:
            raise ValueError(f"no COT data for {symbol} yet")
        return {"symbol": symbol, "code": code, "market": market, "rows": rows}
