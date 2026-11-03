"""Every CME trade of chosen futures from Databento's live feed (GLBX.MDP3, the trades schema) on the front month
by volume, calendar or open interest (Databento's continuous symbols: ES.v.0). Gaps while it was off can fill from
Databento's 1-minute bars (historical data: billed by Databento, so off unless chosen).

Needs a Databento account with live CME data and the databento Python package (docs/FEEDS.md, Packages)."""
import asyncio
import logging
from datetime import datetime, timezone

from chartsrv.feeds.sdk import FeedPlugin, choice, secret

log = logging.getLogger(__name__)
DATASET = "GLBX.MDP3"
MONTHS = "FGHJKMNQUVXZ"


def contract_name(raw: str) -> str:
    """Databento's raw symbol to NinjaTrader's name: "ESZ6" -> "ES 12-26" (this decade)."""
    if len(raw) < 3 or raw[-2] not in MONTHS or not raw[-1].isdigit():
        return raw
    year = datetime.now(timezone.utc).year
    y = year - year % 10 + int(raw[-1])
    y += 10 if y < year - 1 else 0
    return f"{raw[:-2]} {MONTHS.index(raw[-2]) + 1:02d}-{y % 100:02d}"


class DatabentoLive(FeedPlugin):
    TITLE = "Databento (live)"
    KIND = "futures"
    ABOUT = ("Every CME trade of the futures you choose, from Databento's live feed. Needs a Databento account with live "
             "CME data (databento.com; billed by Databento).")
    FIELDS = [secret("api_key", "API key", "databento.com > API Keys (starts with db-)"),
              choice("roll", "Contract", [("v", "Front month by volume"), ("c", "Front month by calendar"),
                                          ("n", "Front month by open interest")]),
              choice("fill", "Fill gaps", [("no", "No"), ("yes", "Yes: 1-minute bars (billed by Databento)")])]
    REQUIRES = ["databento"]

    async def run(self, market):
        import databento as db

        roll = self.values.get("roll") or "v"
        client = db.Live(key=self.values["api_key"])
        roots = {f"{s}.{roll}.0": s for s in self.symbols}
        names: dict[int, str] = {}   # Databento instrument id -> chart symbol
        try:
            await asyncio.to_thread(client.subscribe, dataset=DATASET, schema="trades", stype_in="continuous", symbols=list(roots))
            self.connected()
            async for rec in client:
                if isinstance(rec, db.TradeMsg):
                    sym = names.get(rec.instrument_id)
                    if sym:
                        side = str(getattr(rec.side, "value", rec.side))
                        self.trade(market, sym, rec.ts_event // 1_000_000, rec.price / 1e9, rec.size,
                                   1 if side == "B" else -1 if side == "A" else 0)
                elif isinstance(rec, db.SymbolMappingMsg):
                    sym = roots.get(rec.stype_in_symbol)
                    if sym:
                        names[rec.instrument_id] = sym
                        self.contract(market, sym, contract_name(rec.stype_out_symbol))
                elif isinstance(rec, db.ErrorMsg):
                    raise RuntimeError(f"Databento: {rec.err}")
            raise ConnectionError("Databento closed the connection")
        finally:
            try:
                client.terminate()
            except (ValueError, AttributeError):
                pass

    def fills(self) -> bool:
        return self.values.get("fill") == "yes"   # Fill gaps off: its holes are left for NinjaTrader, or for it once on

    async def history(self, symbol: str, start_ms: int, end_ms: int):
        """Databento's 1-minute bars over the gap, when Fill gaps is on."""
        if self.values.get("fill") != "yes":
            return
        import databento as db

        def fetch():
            data = db.Historical(self.values["api_key"]).timeseries.get_range(
                dataset=DATASET, schema="ohlcv-1m", stype_in="continuous", symbols=[f"{symbol}.{self.values.get('roll') or 'v'}.0"],
                start=datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat(),
                end=datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat())
            return [(r.ts_event // 1_000_000, r.open / 1e9, r.high / 1e9, r.low / 1e9, r.close / 1e9, r.volume)
                    for r in data if isinstance(r, db.OHLCVMsg)]
        for t, o, h, lo, c, v in await asyncio.to_thread(fetch):
            yield ("bar", t, o, h, lo, c, v)

    @classmethod
    async def test(cls, values: dict) -> tuple[bool, str]:
        key = values.get("api_key") or ""
        if not key:
            return False, "Enter the API key"
        try:
            import databento as db
        except ImportError:
            return False, "Needs the databento package: docs/FEEDS.md, Packages"

        def check():
            return db.Historical(key).metadata.list_datasets()
        try:
            sets = await asyncio.to_thread(check)
        except Exception as e:   # the client's own errors: auth, network
            return False, f"Databento refused it ({type(e).__name__})"
        return DATASET in sets, "The key works: CME (GLBX.MDP3) is on it" if DATASET in sets else "The key works, but without CME (GLBX.MDP3)"
