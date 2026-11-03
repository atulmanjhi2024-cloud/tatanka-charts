"""Your own data as a series: a CSV link (a Google Sheet published as CSV, a file any site serves) or a CSV file in
the feeds/ folder, with a date and a value on each row. It charts and goes in the watchlists as DATA:<symbol>, like a
FRED series: daily, weekly or monthly, the latest value and its change from the one before.

Dates: 2026-10-17, 10/17/2026, 2026-10, 17.10.2026 or 20261017. Values: 1234.5, "1,234.5", $12.30, 4.5%. Any row
whose first two cells aren't a date and a number (a header) is skipped. More columns: Column picks the one to use."""
import asyncio
import csv
import io
import re
from datetime import date, datetime
from pathlib import Path

from chartsrv.feeds import sdk
from chartsrv.feeds.sdk import FeedPlugin, number, text

MAX_BYTES = 5_000_000
CODE_RE = re.compile(r"^[A-Z0-9_.]{1,40}$")
FILE_RE = re.compile(r"^[\w.\- ]{1,80}\.(csv|txt)$", re.I)


def parse_date(s: str) -> str | None:
    s = s.strip().strip('"')
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d", "%Y-%m", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            d = datetime.strptime(s, fmt).date()
            return d.isoformat() if date(1800, 1, 1) <= d <= date(2200, 1, 1) else None
        except ValueError:
            continue
    return None


def parse_value(s: str) -> float | None:
    s = s.strip().strip('"').replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def parse(text_: str, column: int = 2) -> tuple[list[tuple[str, float]], int]:
    """(observations oldest first, decimals) from CSV text: the first column the date, `column` (1-based) the value."""
    obs, dec = {}, 0
    sample = text_[:2048]
    delim = ";" if sample.count(";") > sample.count(",") else "\t" if sample.count("\t") > sample.count(",") else ","
    for row in csv.reader(io.StringIO(text_), delimiter=delim):
        if len(row) < max(2, column):
            continue
        d, raw = parse_date(row[0]), row[column - 1]
        v = parse_value(raw) if d else None
        if v is None:
            continue
        obs[d] = v
        frac = raw.strip().strip('"').replace(",", "").rstrip("%").split(".")
        dec = max(dec, min(6, len(frac[1]) if len(frac) > 1 else 0))
    if not obs:
        raise ValueError("no rows with a date and a number")
    return sorted(obs.items()), dec


class CsvData(FeedPlugin):
    TITLE = "Your own data (CSV link)"
    KIND = "data"
    ABOUT = ("A series of your own (a Google Sheet, a spreadsheet export, any CSV with a date and a value per row) that "
             "charts and goes in the watchlists as DATA:<symbol>. Read again every 3 hours.")
    FIELDS = [text("code", "Symbol", "letters, digits, _ or . : it charts as DATA:<this>, e.g. GOLDOIL"),
              text("source", "CSV link or file", "https://… (Google Sheets: File > Share > Publish to web > CSV), or a file name in feeds/"),
              text("name", "Name", "shown on the chart (optional)"),
              number("column", "Column", "which column holds the value (the date is the first)", default=2, min=2, max=50)]

    def code(self) -> str:
        return (self.values.get("code") or "").strip().upper()

    def serves(self, code: str) -> bool:
        return bool(self.code()) and code == self.code()

    def title(self, code: str) -> str:
        return (self.values.get("name") or "").strip() or code

    async def listing(self, http):   # its series, for the search box
        return [(f"DATA:{self.code()}", self.title(self.code()), "data", "Your data")] if CODE_RE.match(self.code()) else []

    def describe(self) -> str:
        return f"DATA:{self.code()}" if CODE_RE.match(self.code()) else "its Symbol: letters, digits, _ or ."

    async def series(self, http, code: str):
        return parse(await self.read(http, self.values.get("source") or "", sdk.FEEDS_DIR), int(self.values.get("column") or 2))

    @staticmethod
    async def read(http, src: str, feeds_dir: Path) -> str:
        src = src.strip()
        if re.match(r"^https?://", src, re.I):
            async with http.stream("GET", src, timeout=30, follow_redirects=True,
                                   headers={"User-Agent": "Mozilla/5.0 (tatanka-charts)"}) as r:
                if r.status_code in (404, 410):
                    raise ValueError(f"the link answered HTTP {r.status_code}")
                r.raise_for_status()   # down for now (5xx, 429): the stored copy meanwhile
                if (r.headers.get("content-length") or "").isdigit() and int(r.headers["content-length"]) > MAX_BYTES:
                    raise ValueError("over 5 MB")
                data = bytearray()
                async for chunk in r.aiter_bytes():   # read up to the limit, never the whole of a big file
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise ValueError("over 5 MB")
                try:
                    return data.decode(r.charset_encoding or "utf-8", "replace")
                except LookupError:
                    return data.decode("utf-8", "replace")
        if FILE_RE.match(src):
            path = feeds_dir / src
            if not path.is_file():
                raise ValueError(f"no file {src} in the feeds folder")
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("over 5 MB")
            return await asyncio.to_thread(path.read_text, "utf-8", "replace")
        raise ValueError("the source: an https:// link, or a .csv file name in the feeds folder")

    @classmethod
    async def test(cls, values: dict) -> tuple[bool, str]:
        import httpx
        code = (values.get("code") or "").strip().upper()
        if not CODE_RE.match(code):
            return False, "Symbol: letters, digits, _ or ."
        async with httpx.AsyncClient() as http:
            try:
                obs, _ = parse(await cls.read(http, values.get("source") or "", sdk.FEEDS_DIR), int(values.get("column") or 2))
            except ValueError as e:
                return False, str(e)
            except httpx.HTTPStatusError as e:
                return False, f"the link answered HTTP {e.response.status_code}"
            except httpx.HTTPError as e:
                return False, f"The link did not answer ({type(e).__name__})"
        return True, f"{len(obs):,} rows, {obs[0][0]} to {obs[-1][0]} (last {obs[-1][1]:g}): DATA:{code}"
