"""Settings from environment variables, plus the instrument and watchlist config files."""
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # the repository root


def _env(name, default=None):
    v = os.environ.get(name, "").strip()
    return v if v else default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    config_dir: Path
    web_dir: Path
    feed: str                 # sim | ninjatrader | none (feeds.TRADE_FEEDS)
    quotes_feed: str          # watchlist quote feeds, comma-separated: ibkr, alpaca, coinbase, yahoo, sim, none
    yahoo_poll_s: int
    discord_webhook: str | None
    discord_charts_webhook: str | None   # chart pictures posted from the app (Post to Discord), its own channel
    ntfy_url: str | None
    heartbeat_url: str | None
    ibkr_host: str
    ibkr_port: int
    ibkr_client_id: int
    ibkr_market_data_type: int  # 1 live, 3 delayed
    max_bars: int
    sim_warmup_min: int
    stale_after_s: int
    nt_host: str
    nt_port: int
    nt_token: str | None
    fred_api_key: str | None = None   # free, from fred.stlouisfed.org: the symbol search then finds every FRED series
    symbols_online: bool = True       # the symbol database refreshes from Nasdaq Trader and Coinbase, searches ask Yahoo
    eia_api_key: str | None = None    # free, from eia.gov/opendata: EIA's energy series
    alpaca_key_id: str | None = None  # free Alpaca account: real-time U.S. stock quotes (IEX)
    alpaca_secret: str | None = None
    alpaca_feed: str = "iex"          # iex (free) or sip (paid data plan: every exchange)
    backup_dir: Path | None = None    # daily backups of alerts, drawings, settings; default: next to the database
    feeds_dir: Path | None = None     # your feed plugins (docs/FEEDS.md); Docker: the feeds/ folder, at /feeds
    remote_needs_password: bool = True   # a fresh install: other devices wait until a password is set
    allowed_hosts: tuple[str, ...] = ()  # more names it answers to (a proxy of yours): app._own_host
    update_repo: str = "tatankatrading/tatanka-charts"   # where updates come from (Connections > App > Updates)
    github_token: str | None = None      # read-only: a private repository needs one to be checked
    commit: str = ""                     # the commit this server was built from (update.cmd passes it to the build)


def load_settings() -> Settings:
    return Settings(
        db_path=Path(_env("CHART_DB", ROOT / "data" / "chart.db")),
        config_dir=Path(_env("CHART_CONFIG_DIR", ROOT / "config")),
        web_dir=Path(_env("CHART_WEB_DIR", ROOT / "web")),
        feed=_env("CHART_FEED", "sim"),
        quotes_feed=_env("CHART_QUOTES_FEED", "sim" if _env("CHART_FEED", "sim") == "sim" else "yahoo"),
        yahoo_poll_s=int(_env("CHART_YAHOO_POLL_S", 15)),
        discord_webhook=_env("CHART_DISCORD_WEBHOOK"),
        discord_charts_webhook=_env("CHART_DISCORD_CHARTS_WEBHOOK"),
        ntfy_url=_env("CHART_NTFY_URL"),
        heartbeat_url=_env("CHART_HEARTBEAT_URL"),
        ibkr_host=_env("IBKR_HOST", "127.0.0.1"),
        ibkr_port=int(_env("IBKR_PORT", 4001)),
        ibkr_client_id=int(_env("IBKR_CLIENT_ID", 31)),
        ibkr_market_data_type=int(_env("IBKR_MARKET_DATA_TYPE", 1)),
        max_bars=int(_env("CHART_MAX_BARS", 3000)),
        sim_warmup_min=int(_env("CHART_SIM_WARMUP_MIN", 240)),
        stale_after_s=int(_env("CHART_STALE_AFTER_S", 120)),
        nt_host=_env("CHART_NT_HOST", "127.0.0.1"),
        nt_port=int(_env("CHART_NT_PORT", 8801)),
        nt_token=_env("CHART_NT_TOKEN"),
        fred_api_key=_env("FRED_API_KEY") or _env("CHART_FRED_API_KEY"),
        symbols_online=_env("CHART_SYMBOLS_ONLINE", "1") != "0",
        eia_api_key=_env("EIA_API_KEY"),
        alpaca_key_id=_env("ALPACA_KEY_ID"),
        alpaca_secret=_env("ALPACA_SECRET"),
        alpaca_feed=_env("ALPACA_FEED", "iex"),
        backup_dir=Path(_env("CHART_BACKUP_DIR")) if _env("CHART_BACKUP_DIR") else None,
        feeds_dir=Path(_env("CHART_FEEDS_DIR", ROOT / "feeds")),
        remote_needs_password=_env("CHART_REMOTE_NEEDS_PASSWORD", "1") != "0",
        allowed_hosts=tuple(h.strip().lower() for h in _env("CHART_ALLOWED_HOSTS", "").split(",") if h.strip()),
        update_repo=_env("CHART_UPDATE_REPO", "tatankatrading/tatanka-charts"),
        commit=_env("CHART_COMMIT", ""),
    )


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    tick_size: float
    point_value: float
    sim_price: float | None = None
    sim_follow: str | None = None
    rth: tuple[str, str] | None = None   # regular session, CT "HH:MM" start and end
    calendar: str = "equity"             # holiday group in config/holidays.json: equity, energy or metals
    quiet: bool = False                  # no "Feed stale" alert when its trades stop (thin markets, ones opened on demand)
    contract: str = "volume"             # how NinjaTrader's add-on rolls it: volume, dates (NinjaTrader's), or a month "03-27"

    @property
    def decimals(self) -> int:
        return max(0, -Decimal(str(self.tick_size)).as_tuple().exponent)

    def round(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, self.decimals)

    def fmt(self, price: float) -> str:
        """The price as its market quotes it: Treasuries in 32nds (ZN 110'165), others in decimals."""
        return bond32(price, self.tick_size) if self.symbol.upper() in BONDS else f"{price:.{self.decimals}f}"

    def to_dict(self):
        return {"symbol": self.symbol, "name": self.name, "tick_size": self.tick_size,
                "point_value": self.point_value, "decimals": self.decimals, "rth": self.rth, "calendar": self.calendar}


BONDS = {"ZT", "ZF", "ZN", "TN", "ZB", "UB", "TWE"}   # quoted in 32nds


def bond32(price: float, tick: float) -> str:
    """110.515625 -> 110'165: the whole points, then 32nds (two digits) and, for a tick finer than 1/32, the eighth of a
    32nd as one digit (0 1 2 3 5 6 7 8), as CME, NinjaTrader and TradingView write Treasury futures."""
    per, n = round(1 / tick), round(abs(price) / tick)
    whole = n // per
    x32 = (n - whole * per) * tick * 32
    t = int(x32 + 1e-9)
    tail = str(int((x32 - t) * 10 + 1e-9)) if tick < 1 / 32 else ""
    return f"{'-' if price < 0 and n else ''}{whole}'{t:02d}{tail}"


# ---- the chartable futures: config/instruments.json, or the list edited in the app (Connections > Instruments), which
# then lives in the database and applies at the next server start ----------------------------------------------------
INSTRUMENTS_SETTING = "instruments"
_SYM = re.compile(r"^(?=[A-Z0-9]*[A-Z])[A-Z0-9]{1,6}$")   # ES, RTY, 6E: a letter somewhere
_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CONTRACT = re.compile(r"^(volume|dates|(0[1-9]|1[0-2])-\d\d)$")   # how a future rolls (Instrument.contract)
_TEXT = re.compile(r"^[^<>\x00-\x1f]{1,40}$")
CALENDARS = ("equity", "energy", "metals")   # the holiday groups config/holidays.json knows


def instruments_file(config_dir: Path) -> dict[str, dict]:
    raw = json.loads((config_dir / "instruments.json").read_text(encoding="utf-8"))
    return {sym: v for sym, v in raw.items() if not sym.startswith("_")}


def _num(row, k, label, lo, hi, need=True):
    v = row.get(k)
    if v is None and not need:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo < v <= hi:
        raise ValueError(f"{label}: a number above {lo:g}" + (f", up to {hi:g}" if hi < 1e9 else ""))
    return v




def clean_instruments(rows) -> dict[str, dict]:
    """The list as sent ([{symbol, name, tick_size, point_value, rth, calendar, sim_price, sim_follow}]), checked:
    ValueError naming the symbol and the field otherwise."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= 60:
        raise ValueError("1 to 60 instruments")
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each instrument: {symbol, name, ...}")
        sym = str(row.get("symbol", "")).strip().upper()
        if not _SYM.match(sym):
            raise ValueError(f"symbol {sym or '(empty)'!r}: the root, 1-6 letters and digits (ES, RTY, 6E)")
        if sym in out:
            raise ValueError(f"{sym} is listed twice")
        try:
            name = str(row.get("name") or "").strip()
            if not _TEXT.match(name):
                raise ValueError("name: 1 to 40 characters of plain text")
            tick = _num(row, "tick_size", "tick size", 0, 10_000)
            if abs(round(tick, 8) - tick) > 1e-12:
                raise ValueError("tick size: up to 8 decimals")
            v = {"name": name, "tick_size": tick, "point_value": _num(row, "point_value", "point value", 0, 1e12)}
            rth = row.get("rth")
            if rth:
                if not (isinstance(rth, (list, tuple)) and len(rth) == 2 and all(isinstance(t, str) and _HM.match(t) for t in rth)):
                    raise ValueError("RTH: two times, HH:MM")
                if not (rth[0] < rth[1] <= "16:00"):
                    raise ValueError("RTH: start before end, ending by the 16:00 CT close")
                v["rth"] = [rth[0], rth[1]]
            cal = row.get("calendar") or "equity"
            if cal not in CALENDARS:
                raise ValueError(f"holidays: one of {', '.join(CALENDARS)}")
            if cal != "equity":
                v["calendar"] = cal
            price = _num(row, "sim_price", "simulator price", 0, 1e9, need=False)
            if price is not None:
                v["sim_price"] = price
        except ValueError as e:
            raise ValueError(f"{sym} {e}") from None
        if row.get("sim_follow"):
            v["sim_follow"] = str(row["sim_follow"]).strip().upper()
        if row.get("quiet") is True:   # no "Feed stale" alerts: one added by opening it, often a thin market
            v["quiet"] = True
        contract = str(row.get("contract") or "volume").strip()
        if not _CONTRACT.match(contract):
            raise ValueError(f"{sym} contract: by volume, NinjaTrader's dates, or a month as MM-YY (03-27)")
        if contract != "volume":
            v["contract"] = contract
        out[sym] = v
    for v in out.values():   # a micro kept without its parent (the editor shows no sim_follow): trades of its own
        if v.get("sim_follow") not in (None, *out):
            del v["sim_follow"]
    for sym, v in out.items():
        if v.get("sim_follow") and (out[v["sim_follow"]].get("sim_follow") or v["sim_follow"] == sym):
            raise ValueError(f"{sym} simulator follows {v['sim_follow']}: not a listed instrument of its own")
    return out


def _instruments(raw: dict[str, dict]) -> dict[str, Instrument]:
    out = {}
    for sym, v in raw.items():
        v = {**v, "rth": tuple(v["rth"]) if v.get("rth") else None}
        out[sym] = Instrument(symbol=sym, **v)
    return out


def futures_catalog(config_dir: Path) -> list[dict]:
    """config/futures.json: CME futures with their specs, to add to the list by picking one (each row checked)."""
    path = config_dir / "futures.json"
    if not path.exists():
        return []
    out = []
    for g in json.loads(path.read_text(encoding="utf-8")).get("groups", []):
        for sym, name, tick, pv, price, *more in g.get("items", []):
            row = {"symbol": sym, "name": name, "tick_size": tick, "point_value": pv, "rth": g.get("rth"),
                   "calendar": g.get("calendar", "equity"), "sim_price": price, **(more[0] if more else {})}
            out.append({**next(iter(clean_instruments([row]).values())), "symbol": sym, "group": g["name"]})
    return out


def instruments_state(config_dir: Path, store, running: dict[str, Instrument]) -> dict:
    """What Connections > Instruments shows: the list (the app's if saved, else the file's), where it is from, and
    whether it differs from what the server runs (pending: a restart applies it)."""
    saved = store.get_setting(INSTRUMENTS_SETTING, None)
    listed = clean_instruments([{"symbol": k, **v} for k, v in (saved or instruments_file(config_dir)).items()])
    now = {}
    for sym, i in running.items():
        v = {"name": i.name, "tick_size": i.tick_size, "point_value": i.point_value,
             "rth": list(i.rth) if i.rth else None, "calendar": i.calendar, "sim_price": i.sim_price, "sim_follow": i.sim_follow,
             "quiet": i.quiet or None, "contract": None if i.contract == "volume" else i.contract}
        now[sym] = {k: x for k, x in v.items() if x is not None}
    return {"instruments": [{"symbol": k, **v} for k, v in listed.items()], "source": "app" if saved else "file",
            "pending": listed != clean_instruments([{"symbol": k, **v} for k, v in now.items()]), "calendars": list(CALENDARS),
            "catalog": futures_catalog(config_dir)}


RTH_1500 = "instruments_rth_1500"   # done once: the equity index futures' RTH ends 15:00 CT (was 15:15)


def migrate_instruments(store):
    """Once: a list saved in the app keeps the old 08:30-15:15 for the equity index futures; their regular hours end
    at 15:00 CT, the cash close and their settlement. A 15:15 set after this stays."""
    if store.get_setting(RTH_1500, False):
        return
    saved = store.get_setting(INSTRUMENTS_SETTING, None)
    if isinstance(saved, dict):
        n = 0
        for v in saved.values():
            if isinstance(v, dict) and v.get("rth") == ["08:30", "15:15"]:
                v["rth"], n = ["08:30", "15:00"], n + 1
        if n:
            store.set_setting(INSTRUMENTS_SETTING, saved)
            logging.getLogger(__name__).info("%d instruments' RTH now ends 15:00 CT", n)
    store.set_setting(RTH_1500, True)


def load_instruments(config_dir: Path, store=None) -> dict[str, Instrument]:
    """config/instruments.json, or with `store` the list saved in the app (the file's if that one is unreadable)."""
    saved = store.get_setting(INSTRUMENTS_SETTING, None) if store is not None else None
    if saved:
        try:
            return _instruments(clean_instruments([{"symbol": k, **v} for k, v in saved.items()]))
        except (ValueError, TypeError) as e:
            logging.getLogger(__name__).error("the instruments saved in the app don't load (%s): config/instruments.json applies", e)
    return _instruments(instruments_file(config_dir))


def load_holidays(config_dir: Path | None, today: date | None = None) -> dict:
    """config/holidays.json, and for the years it doesn't list (from 2000, as 1-minute history is kept for ever, to two
    years ahead) CME's usual pattern (rule_holidays), so the calendar never runs out. A year in the file is taken as
    it is. config_dir None: the usual pattern only."""
    path = config_dir / "holidays.json" if config_dir else None
    cal = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {"days": []}
    listed = {d["date"][:4] for d in cal.get("days", [])}
    year = (today or date.today()).year
    extra = [d for y in range(2000, year + 3) if str(y) not in listed for d in rule_holidays(y)]
    return {**cal, "days": [*cal.get("days", []), *extra]}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (0 Monday) of the month; n = -1: the last."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:   # the Gregorian computus (Meeus/Jones/Butcher)
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> tuple[date, bool]:
    """A fixed-date holiday on a weekend: Friday before (Saturday) or Monday after (Sunday)."""
    if d.weekday() == 5:
        return d - timedelta(days=1), True
    if d.weekday() == 6:
        return d + timedelta(days=1), True
    return d, False


HALT = {"default": "12:00", "energy": "13:30", "metals": "13:30"}   # CME's holiday halts, CT
CLOSE = {"default": "12:15", "energy": "13:45", "metals": "13:45"}  # its early closes (Christmas Eve: energy, metals 12:45)


def rule_holidays(year: int) -> list[dict]:
    """CME Globex's usual US holiday hours for `year`, as config/holidays.json lays them out (CT): Monday holidays,
    Juneteenth, Independence Day and Thanksgiving halt at 12:00 (energy and metals 13:30); the day before
    Independence Day and the day after Thanksgiving close at 12:15 (energy and metals 13:45), Christmas Eve at 12:15
    (12:45); New Year's Day, Good Friday and Christmas are closed. CME publishes the final hours shortly before each
    one: a year listed in the file wins."""
    out = []

    def add(d: date, name: str, **kind):
        out.append({"date": d.isoformat(), "name": name, **kind})

    ny = date(year, 1, 1)
    if ny.weekday() != 5:   # on a Saturday nothing moves (Friday the 31st trades)
        d, obs = _observed(ny)
        add(d, "New Year's Day" + (" (observed)" if obs else ""), closed=True)
    add(_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day", halt=HALT)
    add(_nth_weekday(year, 2, 0, 3), "Presidents Day", halt=HALT)
    add(_easter(year) - timedelta(days=2), "Good Friday", closed=True)
    add(_nth_weekday(year, 5, 0, -1), "Memorial Day", halt=HALT)
    if year >= 2022:   # CME's first was June 20, 2022
        d, obs = _observed(date(year, 6, 19))
        add(d, "Juneteenth" + (" (observed)" if obs else ""), halt=HALT)
    j4, obs = _observed(date(year, 7, 4))
    if not obs and j4.weekday() != 0:
        add(j4 - timedelta(days=1), "Day before Independence Day", close=CLOSE)
    add(j4, "Independence Day" + (" (observed)" if obs else ""), halt=HALT)
    add(_nth_weekday(year, 9, 0, 1), "Labor Day", halt=HALT)
    tg = _nth_weekday(year, 11, 3, 4)
    add(tg, "Thanksgiving", halt=HALT)
    add(tg + timedelta(days=1), "Day after Thanksgiving", close=CLOSE)
    xmas, obs = _observed(date(year, 12, 25))
    if not obs and xmas.weekday() != 0:
        add(xmas - timedelta(days=1), "Christmas Eve", close={**CLOSE, "energy": "12:45", "metals": "12:45"})
    add(xmas, "Christmas" + (" (observed)" if obs else ""), closed=True)
    return sorted(out, key=lambda x: x["date"])

