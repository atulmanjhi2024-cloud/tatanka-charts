"""EIA (U.S. Energy Information Administration) series: the weekly petroleum status report (crude, Cushing,
gasoline and distillate stocks, production, refinery runs) and natural gas storage. A watchlist symbol
"EIA:PET.WCESTUS1.W" shows the latest week and its change; the chart draws it. EIA's API needs a free key
(eia.gov/opendata), set under Connections or as EIA_API_KEY."""
import re

from .fred import NotReady, SeriesData

API = "https://api.eia.gov/v2/seriesid/"
ID_RE = re.compile(r"^[A-Z0-9_.\-]{3,80}$")
PREFIX = "EIA:"


def series_id(symbol: str) -> str | None:
    """"EIA:PET.WCESTUS1.W" -> "PET.WCESTUS1.W", else None."""
    if not symbol.upper().startswith(PREFIX):
        return None
    sid = symbol[len(PREFIX):].upper()
    return sid if ID_RE.match(sid) else None


def _day(period: str) -> str | None:
    """EIA's period ("2026-10-09", "2026-09", "2026") as a day."""
    if re.match(r"^\d{4}-\d\d-\d\d$", period):
        return period
    if re.match(r"^\d{4}-\d\d$", period):
        return f"{period}-01"
    if re.match(r"^\d{4}$", period):
        return f"{period}-01-01"
    return None


def parse_series(doc: dict) -> tuple[list[tuple[str, float]], int]:
    rows = (((doc or {}).get("response") or {}).get("data")) or []
    out, dec = {}, 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        d, v = _day(str(r.get("period", ""))), r.get("value")
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if d:
            out[d] = x
            if isinstance(v, str) and "." in v:
                dec = max(dec, min(3, len(v.split(".", 1)[1])))
            elif isinstance(v, float) and not x.is_integer():
                dec = max(dec, min(3, len(repr(v).split(".", 1)[1])))
    return sorted(out.items()), dec


class EiaData(SeriesData):
    name, table, id_re = "EIA", "eia_series", ID_RE

    def __init__(self, store, http, key=lambda: None):
        super().__init__(store, http)
        self.key = key   # () -> the API key now (Connections can change it)

    async def _fetch(self, sid: str):
        key = self.key()
        if not key:
            raise NotReady("EIA needs a free API key: Connections > EIA (eia.gov/opendata)")
        r = await self.http.get(API + sid, params={"api_key": key, "length": 5000}, timeout=30,
                                headers={"Accept": "application/json"})
        if r.status_code in (401, 403):
            raise NotReady("EIA refused the API key: check it under Connections > EIA")
        if r.status_code in (400, 404):
            raise ValueError(f"EIA has no series {sid}")
        r.raise_for_status()
        return parse_series(r.json())
