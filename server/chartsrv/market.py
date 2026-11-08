"""The hub. Feeds push ticks and quotes in; this stores them, keeps a bar series for every open chart,
runs alerts, and pushes coalesced updates to each connected browser."""
import asyncio
import hashlib
import json
import logging
import sqlite3
import time
import traceback
from dataclasses import asdict, replace

import httpx

from . import dsma, htfema, packages, sessions, stopping, workers
from . import watchlists as wl
from .alerts import AlertEngine
from .amt_hub import AmtHub
from .bars import BarSpec, Series, make_series, parse_spec
from .config import INSTRUMENTS_SETTING, Instrument, Settings, _instruments, clean_instruments, futures_catalog, instruments_state
from .levels import Levels
from .models import Bar, Tick
from .notify import Notifier
from .store import Store, bar_1m_start, drop_settlement_bars, drop_weekend_prints, iter_bars_1m, iter_ticks, sweeps
from .studies import ProfileBook, range_profile
from .vp_service import VpService
from .gapfill import GapFill
from .settlement import Settlements
from .daily_levels import DailyLevels
from .indicators import IndicatorSettings
from .gaps_service import GapService
from .ext import ExtBars
from .auth import Auth
from .backup import Backups
from .branding import Branding
from . import connections as conn_defs
from .connections import FEED_OF, Connections
from .cot import Cot
from .earnings import Earnings
from .econ_calendar import EconCalendar
from .feeds import feed_names, make_feed
from .feeds.eia import EiaData
from .feeds.fred import FredData
from .feeds.sdk import without_keys
from .feeds.yahoo import CloseFill, market_day
from .ind_alerts import IndicatorAlerts
from .packages import Installer
from .updates import Updates
from .upkeep import Upkeep
from .plugins import Plugins
from .symbols import SymbolDB
from .study_alerts import StudySources
from .templates import Templates
from .tpo_service import TpoService
from .webpush import Push
from .workspaces import Workspaces

log = logging.getLogger(__name__)

CLIENT_FLUSH_S = 0.15     # bar updates to browsers
QUOTES_EVERY = 4          # quotes go out every 4th client flush
STORE_FLUSH_S = 1.0
UNWRITTEN_MAX = 200_000   # while the database can't be written (a full disk), the newest this many trades wait for it
UNWRITABLE_LOG_MS = 60_000   # ...and that it still can't is logged once this often
WATCHDOG_S = 30
IDLE_SERIES_S = 6 * 3600   # a chart nobody shows stays loaded this long, so coming back to it is instant...
IDLE_SERIES_MAX = 24        # ...unless more are idle than this: the longest idle go first
RECENT_KEEP = 8             # the charts (and AMT runs) used last, loaded again in the background after a restart
PREWARM_DELAY_S = 5
SHORT_HISTORY_MS = 365 * 86_400_000   # a daily or longer chart with less stored than this: older bars from Yahoo
DAY_MS = 86_400_000
RANGE_TICKS_MAX_MS = 3 * DAY_MS   # longer visible-range/composite profiles use 1m bars


def now_ms() -> int:
    return int(time.time() * 1000)




QUOTE_RANK = {"ninjatrader": 3, "ibkr": 2, "alpaca": 2, "coinbase": 2, "yahoo": 1, "fred": 1, "eia": 1, "sim": 0}   # best first
QUOTE_HOLD_MS = 120_000   # a lesser source fills in after the better one is silent this long
CHART_QUOTE_S = 600       # a free-data chart's symbol is quoted this long after the chart last asked for bars
SETTLEMENT_DROPPED = "settlement_bars_dropped"   # once: NinjaTrader's settlement bars a gap fill stored
WEEKEND_DROPPED = "closed_prints_dropped"   # once: the prints stored over a weekend or a closed holiday before the
                                            # feeds left them out ("weekend_prints_dropped" before the holidays were in)
OLD_NAMES = "contracts_old_names"   # {instrument: its stored contract}, noted once at the first start after the IBKR
                                    # futures plugin named contracts by their month; each used up at its next report


class Client:
    def __init__(self):
        self.outbox: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.pending: dict[int, dict[int, Bar]] = {}   # sub id -> seq -> bar (latest state wins)
        self.subs: dict[int, "SeriesEntry"] = {}
        self.loading: set[int] = set()   # sub ids waiting for history; an unsub removes the id
        self.amt_entries: dict = {}           # sub id -> AMT Toolkit runner entry
        self.amt_state: dict = {}             # sub id -> what of the AMT payload this client already has
        self.amt_asked: dict = {}             # sub id -> its newest AMT request still loading (amt_off drops it)
        self.tape: tuple[str, int] | None = None   # (symbol, smallest trade) this client wants live
        self.closed = False

    def put(self, msg: dict):
        try:
            self.outbox.put_nowait(msg)
        except asyncio.QueueFull:
            log.warning("client too slow; dropping connection")
            self.closed = True

    def queue_bars(self, sid: int, bars: list[Bar]):
        p = self.pending.setdefault(sid, {})
        for b in bars:
            p[b.seq] = b

    def drain(self):
        for sid, p in self.pending.items():
            if p:
                self.put({"type": "bars", "id": sid, "bars": [b.to_dict() for _, b in sorted(p.items())]})
        self.pending.clear()


class SeriesEntry:
    def __init__(self, symbol: str, series: Series):
        self.symbol = symbol
        self.series = series
        self.ready = asyncio.Event()
        self.pending_ticks: list[Tick] = []
        self.subs: set[tuple[Client, int]] = set()
        self.idle_since = time.monotonic()
        self.error: str | None = None


class BookEntry:
    def __init__(self, book: ProfileBook):
        self.book = book
        self.ready = asyncio.Event()
        self.pending: list[Tick] = []


def _vwap_from_open(db_path, series: Series, symbol: str, since: int):
    """History starting at `since`, inside a session: the session's stored 1m bars before it go into the VWAPs only, so
    the first session's VWAP and bands count from its 17:00 open as every other's do, without reading its trades."""
    if series.studies:
        for b in iter_bars_1m(db_path, symbol, sessions.session_start(since), since):
            series.vwap_only(b)


def build_history(db_path, series: Series, symbol: str, spec: BarSpec, cutoff_rowid: int, split_t: int, now: int):
    """Runs in a worker (workers.py). Time bars come from stored 1m bars up to split_t (the current minute), then
    raw ticks. Tick, volume and range bars are rebuilt from raw ticks since the previous session's open."""
    series.bulk = True
    if spec.from_minutes:
        since = split_t - (spec.history_days or 400) * DAY_MS
        if not spec.cal:
            need = int(spec.size // 60 * series.bars.maxlen * 1.1) + 1   # margin for short session-edge bars
            if series.rth_only:
                need *= 4   # regular hours are under a third of the Globex day
            since = max(since, bar_1m_start(db_path, symbol, split_t, need))
            _vwap_from_open(db_path, series, symbol, since)
        for b in iter_bars_1m(db_path, symbol, since, split_t):
            series.on_bar(b)
        while series.bars and series.bars[0].t < since:   # the first bar began before the window: only part of it
            series.bars.popleft()
        since = split_t
    else:
        since = sessions.previous_session_start(sessions.session_start(now))
        if spec.kind == "time":   # seconds bars: only as far back as the chart holds, not two sessions of trades
            need = int(spec.size * series.bars.maxlen / 60 * 1.1) + 1   # in stored minutes, so a weekend doesn't empty it
            if series.rth_only:
                need *= 4   # regular hours are under a third of the Globex day
            since = max(since, bar_1m_start(db_path, symbol, now, need))
            _vwap_from_open(db_path, series, symbol, since)
    for tk in iter_ticks(db_path, symbol, since, cutoff_rowid):
        series.on_tick(tk)
    if spec.kind == "time" and not spec.from_minutes:   # a 7s or 45s bar can begin before the window: only part of it
        while series.bars and series.bars[0].t < since:
            series.bars.popleft()
    series.end_bulk()


class Market:
    def __init__(self, settings: Settings, instruments: dict[str, Instrument], watchlists: dict,
                 store: Store, notifier: Notifier, http: httpx.AsyncClient, plugins: Plugins | None = None):
        self.connections = Connections(store, settings)   # the feeds, keys and channels saved in the app, over .env's
        self.connections.instruments = instruments
        self.plugins = plugins or Plugins([], set())   # feed plugins (feeds/sdk.py): futures and quote feeds of your own
        self.packages = Installer()                     # the Python packages they need, installed from Connections
        self.trade_owner: dict[str, str] = self.connections.owners()   # instrument -> the plugin feed supplying it
        self.plugin_rank: dict[str, int] = {}   # quote plugin -> its QUOTE_RANK
        self.plugin_failed: dict[str, str] = {}   # connection -> why its plugin did not start (on its card)
        self.data_series: dict = {}   # data plugin id -> its PluginSeries
        self.s = settings = self.connections.effective()
        notifier.discord, notifier.ntfy = settings.discord_webhook, settings.ntfy_url
        notifier.extra = self.connections.extras()
        self.push = Push(store, http)   # the app's own notifications, on each device that turned them on
        notifier.push = self.push
        self.instruments = instruments
        self.wl = watchlists                  # the watchlist document (watchlists.py)
        self.chart_keys: dict[str, float] = {}   # free-data charts' quote keys -> when to stop quoting them (monotonic)
        # the quotes the lists and charts need: a chart future's as its front month (ES1!), as the lists write it
        self.watch_keys = wl.quote_keys(watchlists, instruments) | {f"{s}1!" for s in instruments}
        self.store = store
        self.notifier = notifier
        self.http = http
        one_min = parse_spec("1m")
        self.m1 = {sym: make_series(one_min, i.tick_size, i.decimals, maxlen=5, studies=False)
                   for sym, i in instruments.items()}
        self.books: dict[str, BookEntry] = {}
        self.contracts: dict[str, str] = dict(store.get_setting("contracts", {}) or {})   # symbol -> "ES 03-27"
        self.old_names = self._old_names()   # symbol -> its stored contract, perhaps in the IBKR plugin's old naming
        self.gapfill = GapFill(self)
        self.settlements = Settlements(self, getattr(settings, "config_dir", None))
        self.workspaces = Workspaces(store)
        self.symbols = SymbolDB(store, instruments, http, settings.fred_api_key, settings.symbols_online)
        self.symbols.on_demand = lambda: self.catalog()   # futures that chart live once opened
        self.symbols.feed_sources = self.symbol_sources   # the connected feeds' symbols too
        self.symbols.on_only_futures = lambda only: self.broadcast({"type": "only_futures", "only_futures": sorted(only)})
        self.fred = FredData(store, http)
        self.eia = EiaData(store, http, lambda: self.s.eia_api_key)
        self.cot = Cot(store, http, settings.config_dir, instruments)   # CFTC Commitments of Traders
        self.calendar = EconCalendar(self)                 # economic releases, and warnings before them
        self.earnings = Earnings(self)                     # major companies' earnings dates, for the same tab
        self.auth = Auth(store)        # the app's password and signed-in devices
        self.templates = Templates(store)   # chart, drawing and indicator templates, on every device
        self.branding = Branding(store)     # the app's name and logo (Connections > Branding)
        self.backups = Backups(self)   # a backup of alerts, drawings and settings every day
        self.upkeep = Upkeep(self)     # trades kept 5 years (or as set), the database tidied, each day in CME's break
        self.updates = Updates(self)   # a newer version on GitHub? (Connections > App > Updates)
        self.ext = ExtBars(self)   # charts of what has no live feed here: Yahoo and FRED history
        self.ind = IndicatorSettings(store)   # TPO, Gap, 1hr EMA, If/Then, Level Lines, volume profile settings
        self.vp = VpService(self)              # the Session, Auto Anchored and Periodic volume profiles
        self.amt = AmtHub(self)
        self.daily = DailyLevels(self)   # the AMT Toolkit's daily levels: levels file or link, else the pasted line
        self.levels = Levels(self)       # ...as one set per symbol: the Daily levels study, level alerts, /api/levels
        self._seed_nt_settings()
        self.tpo = TpoService(self)
        self.gaps = GapService(self)
        self.dirty: dict[tuple[str, int], Bar] = {}
        self.tick_buf: list[Tick] = []
        self.write_failed_at: int | None = None   # when a failed store write was last logged; None while writes work
        self.unwritten_dropped = 0                # the oldest trades waiting for it dropped meanwhile (UNWRITTEN_MAX)
        self.last_tick: dict[str, Tick] = {}
        self.tape_subs: dict[str, set[Client]] = {}   # symbol -> clients with a live tape on it
        self.tape_buf: dict[str, list[Tick]] = {}     # their trades since the last client flush
        self.m1_ticked: set[str] = set()   # symbols whose 1m bar has been updated by a live tick since start-up or a gap fill
        self._snap_min, self._snap_open = None, True
        self.series: dict[tuple[str, str], SeriesEntry] = {}
        self.by_symbol: dict[str, list[SeriesEntry]] = {sym: [] for sym in instruments}
        self.clients: set[Client] = set()
        self.quotes: dict[str, dict] = {}
        self.quotes_dirty: set[str] = set()
        self.quote_from: dict[str, tuple[str, int]] = {}   # symbol -> (source, when it last quoted)
        # symbol -> (its trading day, the previous close its quotes are set against that day): any source's, or the
        # fill's, for a quote that comes without one (not a future's: its settlement's)
        self.closes: dict[str, tuple] = {}
        self.close_fill = CloseFill(self)   # Yahoo's previous close of a stock no source gave one for
        self.alerts = AlertEngine(store, instruments, self.levels, self._on_alert)
        self.alerts.on_change = self._alert_expired
        self.study_sources = StudySources(self)
        self.alerts.studies = self.study_sources.value
        self.ind_alerts = IndicatorAlerts(self)
        self.alerts.indicators = self.ind_alerts.check
        self.stale: set[str] = set()
        self.started = now_ms()
        self.tasks: list[asyncio.Task] = []
        self.cleaning: asyncio.Task | None = None   # the start's one-time clean-up (_drop_weekend_prints)
        self.feed_names: list[str] = []
        self.feed_objs: list = []
        self.feed_tasks: dict[str, asyncio.Task] = {}
        self._conn_lock = asyncio.Lock()
        self.heartbeat_last: tuple[bool, int] | None = None   # (answered, when)
        rec = store.get_setting("recent_charts", {}) or {}
        self.recent = {"charts": [x for x in rec.get("charts", []) if isinstance(x, list) and len(x) == 3][:RECENT_KEEP],
                       "amt": [x for x in rec.get("amt", []) if isinstance(x, list) and len(x) == 2][:RECENT_KEEP]}
        self._bare_futures()

    # --- lifecycle -------------------------------------------------------------------------------
    def seed(self):
        """Continue the stored current minute after a restart instead of overwriting it."""
        for sym, s in self.m1.items():
            b = self.store.last_bar_1m(sym)
            if b:
                s._seq += 1
                b.seq = s._seq
                s.bars.append(b)
            tk = self.store.last_tick(sym)
            if tk:
                self.last_tick[sym] = tk
                self.alerts.last_px[sym] = tk.price   # a cross during a restart still fires on the next tick
                self.alerts.last_ts[sym] = tk.ts

    async def start(self, feeds: list):
        self.seed()
        self.levels.poll()
        loops = [self._client_loop(), self._store_loop(), self._watchdog_loop(),
                 self._housekeeping_loop(), self.amt.loop(), self.daily.run(), self.symbols.run(),
                 self.calendar.run(), self.earnings.run(), self.backups.run(), self._prewarm(), self.watch_plugins(), self.upkeep.run(), self.updates.run(),
                 self.close_fill.run()]
        self.tasks = [asyncio.create_task(c) for c in loops]
        self.cleaning = asyncio.create_task(self._clean_up())   # in the background: the start never waits for it
        self.tasks.append(self.cleaning)
        self.amt.ensure_alert_runners()
        self.gaps.start()
        self.study_sources.ensure()
        self.ind_alerts.ensure()
        for feed in feeds:
            self._add_feed(feed)
        for cid, d in self.connections.instances():
            if d.get("plugin") and d["plugin"] != "data" and (feed := self._plugin_feed(cid)) is not None:
                self._add_feed(feed)

    async def _clean_up(self):
        await self._drop_weekend_prints()
        await self._drop_settlement_bars()

    async def _drop_settlement_bars(self):
        """Once: the settlement bars a gap fill stored from NinjaTrader's history (store.drop_settlement_bars), a day's
        volume in one minute after the close that squeezed every volume profile holding it to a sliver."""
        if self.store.get_setting(SETTLEMENT_DROPPED, False):
            return
        try:
            removed = await asyncio.to_thread(drop_settlement_bars, self.store.path)
            self.store.set_setting(SETTLEMENT_DROPPED, True)
        except sqlite3.Error as e:   # not now (a full disk, a lock): the next start tries again
            log.warning("settlement bars not removed: %s", e)
            return
        for sym in removed:
            if sym in self.instruments:
                self.history_changed(sym)
        if removed:
            log.info("removed %d settlement bars a gap fill stored: %s", sum(removed.values()), removed)

    async def _drop_weekend_prints(self):
        """Once: prints a feed stamped over a weekend or a closed holiday, stored before the feeds left them out
        (sessions.weekend), are taken out, and with them the Saturday (and Christmas) bars of one price they made on
        the daily charts. In a worker thread, a slice at a time, while the server runs; the charts of a symbol it took
        any out of load again. Not on the simulator, which trades all weekend (millions of trades a weekend): leaving
        it deletes its trades anyway."""
        if self.store.get_setting(WEEKEND_DROPPED, False):
            return
        if self.s.feed == "sim":
            self.store.set_setting(WEEKEND_DROPPED, True)
            return
        slices, removed = drop_weekend_prints(self.store.path), {}
        try:
            while (got := await asyncio.to_thread(next, slices, None)) is not None:   # the server's writes go between
                removed = got
            self.store.set_setting(WEEKEND_DROPPED, True)
        except sqlite3.Error as e:   # not now (a full disk, a lock): the next start tries again
            log.warning("weekend and holiday prints not removed: %s", e)
            return
        for sym in removed:
            if sym in self.instruments:
                self.history_changed(sym)
        if removed:
            log.info("removed %d ticks and 1-minute bars stamped over a weekend or a closed holiday", sum(removed.values()))

    def _plugin_feed(self, cid: str):
        """The plugin feed saved as `cid`, if it is on (a futures one never beside the simulator)."""
        d = conn_defs.BY_ID.get(conn_defs.type_of(cid), {})
        v = self.connections.saved.get(cid) or {}
        if not d.get("plugin") or d["plugin"] == "data" or not v.get("enabled") or d["id"] not in self.plugins.types \
                or packages.missing(d.get("requires")):
            return None   # (data plugins are read when asked, never run; a package missing: started once it's installed)
        if d["plugin"] == "futures" and (self.s.feed == "sim" or not v.get("symbols")):
            return None
        feed = self.make_plugin(cid, d["id"], v)
        if feed is None:
            return None
        if d["plugin"] == "quotes":
            self.plugin_rank[cid] = int(getattr(feed, "QUOTE_RANK", 2))
        return feed

    def make_plugin(self, cid: str, pid: str, values: dict):
        """The plugin's object for connection `cid`, or None when it raises as it is made (a value it can't use, a file
        it can't read): said on its card, and the server goes on without it."""
        try:
            feed = self.plugins.make(cid, pid, values)
        except Exception as e:
            err = without_keys(f"{type(e).__name__}: {e}", getattr(self.plugins.types.get(pid), "FIELDS", []), values)   # its keys never shown
            err = f"Did not start: {err}"[:200]
            if self.plugin_failed.get(cid) != err:
                log.warning("feed plugin %s: %s", cid, err)
            self.plugin_failed[cid] = err
            return None
        self.plugin_failed.pop(cid, None)
        return feed

    async def watch_plugins(self):
        """The feeds folder, every few seconds: a plugin file added, changed or removed loads again at once; the
        connections using a changed one restart."""
        from .plugins import stamps
        while True:
            await asyncio.sleep(3)
            if stamps(self.plugins.dirs) != self.plugins.files:
                try:
                    await self.reload_plugins()
                except Exception:   # a plugin's own mistake: logged, and the next change is still picked up
                    log.exception("feed plugins did not reload")

    async def reload_plugins(self):
        from .plugins import Plugins
        new = Plugins(self.plugins.dirs, conn_defs.builtin_ids(), previous=self.plugins)
        changed, self.plugins = new.changed(self.plugins), new
        conn_defs.register_plugins(new)
        log.info("feed plugins reloaded: %s", ", ".join(sorted(changed)) or "no change in classes")
        async with self._conn_lock:
            for f in list(self.feed_objs):   # running on a class that changed or went: stopped, started again if it can
                pid = getattr(f, "type_id", None)
                if pid is not None and new.types.get(pid) is not type(f):
                    await self.set_feed(f.name, None)
            running = {f.name for f in self.feed_objs}
            for cid, d in self.connections.instances():   # on, and not running: a class just added or fixed
                if d.get("plugin") and cid not in running and (feed := self._plugin_feed(cid)) is not None:
                    await self.set_feed(cid, feed)
            for cid, ds in list(self.data_series.items()):
                if new.types.get(getattr(ds.feed, "type_id", None)) is not type(ds.feed):
                    del self.data_series[cid]
            self._owners_changed()
        self.symbols.refresh_soon([s for s in self.symbols.sources() if s.startswith("plugin:")])
        self.broadcast({"type": "connections", "notify": self.notifier.channels, "feeds": self.feed_names})

    async def install_packages(self, cid: str) -> list[str]:
        """The packages connection `cid`'s plugin needs, installed in the background; the plugins load again after, and
        the connections that waited for them start. ValueError: nothing to install, or an install already running."""
        d = conn_defs.BY_ID.get(conn_defs.type_of(cid))
        need = [x for x in (packages.missing(d.get("requires")) if d else []) if packages.parse(x)]
        if not need:
            raise ValueError("Nothing pip can install: its packages are there, or REQUIRES names them wrong")
        if self.packages.busy:
            raise ValueError(f"Installing {', '.join(self.packages.busy)} already: wait for it")
        self.packages.busy = list(need)   # shown at once, before pip starts

        async def go():
            try:
                await self.packages.install(need)
                await self.reload_plugins()
            except Exception:
                log.exception("install of %s failed", need)
        task = asyncio.create_task(go())
        self.tasks.append(task)
        task.add_done_callback(lambda t: t in self.tasks and self.tasks.remove(t))
        self.broadcast({"type": "connections", "notify": self.notifier.channels, "feeds": self.feed_names})
        return need

    # --- the chart futures: added while it runs ----------------------------------------------------
    def catalog(self) -> dict[str, str]:
        """The CME futures (config/futures.json) not listed yet that chart live the moment one is opened: root -> name.
        Only with NinjaTrader as the trade feed, which has every one of them."""
        if self.s.feed != "ninjatrader":
            return {}
        if not hasattr(self, "_catalog"):
            self._catalog = futures_catalog(self.s.config_dir)
        return {r["symbol"]: r["name"] for r in self._catalog if r["symbol"] not in self.instruments}

    def config_hash(self, saved: bool = False) -> str:
        """The running instruments' version, every field (open pages reload when it changes at a restart); `saved`:
        with the list saved but not applied yet, for Connections > Instruments (a save from an older view is refused)."""
        what = {s: asdict(i) for s, i in self.instruments.items()}
        if saved:
            what = {"running": what, "saved": self.store.get_setting(INSTRUMENTS_SETTING)}
        return hashlib.sha1(json.dumps(what, sort_keys=True, default=str).encode()).hexdigest()[:12]

    async def add_future(self, sym: str) -> Instrument:
        """A catalog future opened in a chart: on Connections > Instruments for good, and live at once (NinjaTrader
        sends its trades and its last 14 days). ValueError if it isn't one."""
        if sym in self.instruments:
            return self.instruments[sym]
        if sym not in self.catalog():
            raise ValueError(f"{sym} is not a future this server can add")
        row = next(r for r in self._catalog if r["symbol"] == sym)
        listed = {r.pop("symbol"): r for r in instruments_state(self.s.config_dir, self.store, self.instruments)["instruments"]}
        listed[sym] = {**{k: v for k, v in row.items() if k not in ("symbol", "group")}, "quiet": True}
        new = clean_instruments([{"symbol": k, **v} for k, v in listed.items()])
        self.store.set_setting(INSTRUMENTS_SETTING, new)
        await self.add_instruments(_instruments({sym: new[sym]}))
        log.info("added %s (%s) on demand", sym, row["name"])
        return self.instruments[sym]

    async def add_instruments(self, added: dict[str, Instrument]):
        """New chart futures, live without a restart: the parts that keep one thing per symbol get theirs, the
        NinjaTrader add-on forwards them and fills their history, and every open page hears."""
        if not added:
            return
        one_min = parse_spec("1m")
        self.instruments.update(added)   # in place: alerts, connections and the symbol list hold this same dict
        for sym, i in added.items():
            self.m1[sym] = make_series(one_min, i.tick_size, i.decimals, maxlen=5, studies=False)
            self.by_symbol.setdefault(sym, [])
        self.watch_keys = self._watch_set()   # their futures, and a list's bare root of one is that future now
        for f in self.feed_objs:
            if hasattr(f, "charts_added"):
                f.charts_added(self, list(added))
            elif hasattr(f, "watch_changed"):
                f.watch_changed(self)
            if hasattr(f, "fill_added"):   # a futures plugin: the stretch of the ones it supplies, from its history
                f.fill_added(self, list(added))
        self.broadcast({"type": "instruments", "instruments": {s: i.to_dict() for s, i in self.instruments.items()},
                        "catalog": self.catalog(), "config": self.config_hash()})
        self.levels_changed()   # their daily levels' sets (a micro's are its parent's, which may be read already)
        # the alerts kept for one taken off Instruments before (gap, AMT, study and indicator alerts) run again; last, so
        # that whatever goes wrong there leaves it live. They send nothing until the fill of the stretch it was off is
        # done (gapfill.filling)
        self.amt.ensure_alert_runners()
        self.gaps.start()
        self.study_sources.ensure()
        self.ind_alerts.ensure()

    def data_source(self, code: str):
        """DATA:<code>: (the series store, code, "data") from the data plugin turned on that has it, or None."""
        from .plugins import CODE_RE, PluginSeries
        if not CODE_RE.match(code):
            return None
        for cid, d in self.connections.instances():
            if d.get("plugin") != "data" or d["id"] not in self.plugins.types or not (self.connections.saved.get(cid) or {}).get("enabled"):
                continue
            ds = self.data_series.get(cid)
            if ds is None:
                if (feed := self.make_plugin(cid, d["id"], self.connections.saved.get(cid) or {})) is None:
                    continue
                ds = self.data_series[cid] = PluginSeries(self.store, self.http, feed)
            if ds.feed.serves(code):
                return ds, code, "data"
        return None

    def symbol_sources(self) -> dict:
        """The connected feeds that list symbols, for the search box: source name -> async () -> rows."""
        from .feeds import alpaca
        from .feeds.sdk import FeedPlugin
        out = {}
        if self.s.alpaca_key_id and self.s.alpaca_secret:
            out["alpaca"] = lambda: alpaca.assets(self.http, self.s.alpaca_key_id, self.s.alpaca_secret)
        for cid, d in self.connections.instances():
            cls = self.plugins.types.get(d["id"]) if d.get("plugin") in ("quotes", "data") else None
            if cls is None or cls.listing is FeedPlugin.listing or not (self.connections.saved.get(cid) or {}).get("enabled"):
                continue
            if (feed := self.make_plugin(cid, d["id"], self.connections.saved.get(cid) or {})) is None:
                continue

            async def rows(feed=feed):
                return [(s, n, t, ex, "", 0 if t != "data" else 20) for s, n, t, ex in await feed.listing(self.http)]
            out[f"plugin:{cid}"] = rows
        return out

    def _add_feed(self, feed):
        self.feed_names.append(feed.name)
        self.feed_objs.append(feed)
        task = asyncio.create_task(self._run_feed(feed))
        self.feed_tasks[feed.name] = task
        self.tasks.append(task)

    async def set_feed(self, name: str, feed=None):
        """Stops the running feed called `name` (if any) and starts `feed` in its place (if given)."""
        old = next((f for f in self.feed_objs if f.name == name), None)
        task = self.feed_tasks.pop(name, None)
        if old is not None:
            if hasattr(old, "close"):
                old.close()
            self.feed_objs.remove(old)
            self.feed_names.remove(name)
        if task is not None:
            task.cancel()
            await asyncio.wait({task}, timeout=5)
            if task in self.tasks:
                self.tasks.remove(task)
        if feed is not None:
            self._add_feed(feed)

    def _owners_changed(self):
        """Which feed supplies each future may have changed: NinjaTrader hears the chart list again, so it forwards a
        future another feed no longer supplies (a feed that takes one over has its trades preferred: trade_owner). One
        a plugin takes over has the window NinjaTrader's history was filling closed: its answer is dropped from now on
        (another feed's), so the window would hold that instrument's alerts for good (gapfill.filling); the plugin
        opens its own when it starts, if the stretch needs one. One handed back to NinjaTrader has the add-on asked for
        the stretch since its stored data stops, its alerts held meanwhile, as for one put back on Instruments. Either
        way the holes the feed before was asked for are asked of the new one too."""
        before, self.trade_owner = self.trade_owner, self.connections.owners()
        gf = self.gapfill
        for sym in [s for s in self.trade_owner if s not in before]:
            gf.queue.pop(sym, None)
            if gf.windows.pop(sym, None) is not None:
                gf.status[sym] = {**gf.status.get(sym, {}), "state": "dropped", "note": "another feed supplies it now"}
            gf.settle(sym)   # holes NinjaTrader filled before: what came reloads now
        gf.forget_asked([s for s in set(before) | set(self.trade_owner) if before.get(s) != self.trade_owner.get(s)])
        back = [s for s in before if s not in self.trade_owner and s in self.instruments]
        for f in self.feed_objs:
            if hasattr(f, "charts_added"):
                f.charts_added(self, back)

    async def apply_connections(self, cid: str):
        """A connection was saved: the settings, alert channels and data keys follow at once, and its feed restarts
        (or stops). The other feeds keep running. One save at a time: two at once (a double click) would start a
        feed twice and leave one that can't be stopped."""
        async with self._conn_lock:
            await self._apply_connections(cid)

    async def _apply_connections(self, cid: str):
        self.s = self.connections.effective()
        self.notifier.discord, self.notifier.ntfy = self.s.discord_webhook, self.s.ntfy_url
        self.notifier.extra = self.connections.extras()
        self.symbols.fred_key = self.s.fred_api_key
        self._owners_changed()
        if cid == "alpaca" or conn_defs.BY_ID.get(conn_defs.type_of(cid), {}).get("plugin") in ("quotes", "data"):
            self.symbols.refresh_soon({"alpaca"} if cid == "alpaca" else {f"plugin:{cid}"})   # its symbols now; gone ones go
        if (ds := self.data_series.pop(cid, None)) is not None:   # a data plugin saved: read its series again
            ds.forget()
            for f in self.feed_objs:
                if f.name == "fred" and hasattr(f, "watch_changed"):
                    f.watch_changed(self)
        if conn_defs.BY_ID.get(conn_defs.type_of(cid), {}).get("plugin"):   # that feed restarts, or stops
            await self.set_feed(cid, None)
            self.plugin_rank.pop(cid, None)
            if (feed := self._plugin_feed(cid)) is not None:
                await self.set_feed(cid, feed)
        want = feed_names(self.s)
        for name in FEED_OF.get(cid, ()):
            if name in want or any(f.name == name for f in self.feed_objs):
                await self.set_feed(name, make_feed(name, self.s, self.instruments, self.levels) if name in want else None)
        if cid in ("fred", "eia"):   # values for the new key now, not at the next round
            for f in self.feed_objs:
                if f.name == "fred" and hasattr(f, "watch_changed"):
                    f.watch_changed(self)
        self.broadcast({"type": "connections", "notify": self.notifier.channels, "feeds": self.feed_names})

    async def stop(self):
        # first, before a pass of the loop: the chain of first looks put off to its passes ends (each would send its
        # replay to the workers this shutdown stops, starting new ones that outlive a restart in place)
        self.ind_alerts._later.clear()
        for f in self.feed_objs:   # their connections first: Python 3.12's listener, stopped, waits for them to go
            if hasattr(f, "close"):
                f.close()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.flush_for_read()   # a failed write (a full disk) is logged: the store still closes, and a restart happens

    async def _run_feed(self, feed):
        while True:
            try:
                await (feed.serve(self) if hasattr(feed, "serve") else feed.run(self))   # plugins: serve()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if hasattr(feed, "scrub"):   # a plugin's error can hold its key (a URL's): the traceback without it
                    log.error("feed %s crashed; restarting in 10s\n%s", feed.name,
                              without_keys(traceback.format_exc().rstrip(), feed.FIELDS, feed.values))
                else:
                    log.exception("feed %s crashed; restarting in 10s", feed.name)
                await stopping.pause(10)   # never again after a stop it swallowed (stopping.py)

    # --- input from feeds ------------------------------------------------------------------------
    def on_tick(self, tk: Tick, live: bool = True):
        if tk.symbol not in self.instruments:
            return
        minute = tk.ts // 60_000
        if minute != self._snap_min:   # session state only changes on the hour; cache it per minute
            self._snap_min, self._snap_open = minute, sessions.is_open(tk.ts)
        if not self._snap_open:
            tk.ts = sessions.snap(tk.ts)
        self.m1_ticked.add(tk.symbol)
        self.tick_buf.append(tk)
        for b in self.m1[tk.symbol].on_tick(tk):
            self.dirty[(tk.symbol, b.t)] = b
            self.amt.on_minute(tk.symbol, b)
            self.gaps.on_minute(tk.symbol, b, live)
        self.amt.on_trade(tk)   # after its minute: an AMT profile drawn from trades checks them against it
        self.last_tick[tk.symbol] = tk
        if tk.symbol in self.tape_subs:
            self.tape_buf.setdefault(tk.symbol, []).append(tk)
        book = self.books.get(tk.symbol)
        if book:
            if book.ready.is_set():
                book.book.add(tk)
            else:
                book.pending.append(tk)
        for entry in self.by_symbol[tk.symbol]:
            if not entry.ready.is_set():
                entry.pending_ticks.append(tk)
                continue
            bars = entry.series.on_tick(tk)
            if bars:
                for client, sid in entry.subs:
                    client.queue_bars(sid, bars)
        self.study_sources.on_tick(tk)
        self.ind_alerts.on_tick(tk)
        if live:
            self.alerts.on_tick(tk)
            self.amt.on_live_tick(tk)
        if len(self.tick_buf) % 20_000 == 0:   # a burst of trades: written every 20k, not only once a second
            try:
                self.flush()
            except Exception:   # they stay buffered for the store loop's next try; the feed carries on
                self._write_failed("store flush failed")

    def history_changed(self, sym: str, charts: bool = True):
        """Stored history of `sym` changed under the running server (a gap fill): open charts reload, the studies
        built from it (AMT, TPO's daily bars, gap and study alerts) load again, and its settlement is worked out again.
        charts False: older history filled in (gapfill's older requests): the charts load it as they are scrolled back,
        not again now."""
        if not any(s == sym for s, _ in self.dirty):   # no live minute waits to be written: the fill's may run past the
            self.m1_ticked.discard(sym)                  # last one (an outage), so what loads again splits at the current minute
        for entry in list(self.by_symbol.get(sym, []) if charts else []):
            for client, sid in list(entry.subs):
                client.put({"type": "reload", "id": sid})
            self.by_symbol[sym].remove(entry)
            for key, e in list(self.series.items()):
                if e is entry:
                    del self.series[key]
        self.amt.reload(sym)
        try:
            self.settlements.history_changed(sym)
        except Exception:   # a failed write (the database locked by an import) never skips the reloads below
            log.exception("%s settlement not worked out again", sym)
        self.vp.forget(sym)
        self.tpo.forget(sym)
        dsma.forget(sym)
        htfema.forget(sym)
        if self.gaps.watches.pop(sym, None) is not None:
            self.gaps.start()
        self.books.pop(sym, None)   # the profiles POC/VAH/VAL alerts read: ensure() loads them again with the rest
        if self.study_sources.watches.pop(sym, None) is not None:
            self.study_sources.ensure()
        if any(k[0] == sym for k in self.ind_alerts.watches):
            self.ind_alerts.watches = {k: w for k, w in self.ind_alerts.watches.items() if k[0] != sym}
            self.ind_alerts.ensure()

    def on_fill(self, f: dict):
        """One of your NinjaTrader fills: stored once, and shown on open charts and the journal."""
        if self.store.add_fill(f):
            log.info("fill %s %s %s %+d @ %s", f["account"], f["contract"], f["name"], f["side"] * f["qty"], f["price"])
            self.broadcast({"type": "fill", "fill": f})

    def _old_names(self) -> dict[str, str]:
        """Once, at the first start after the IBKR futures plugin came to name contracts by their contract month ("CL
        12-26" for CLZ6): the stored contract of each instrument it supplied then. It named CL, NG, RB, HO, MCL and SR3
        by the month of their last trading day ("CL 11-26"), which the first start after would take for a roll. A fresh
        install has none."""
        got = self.store.get_setting(OLD_NAMES, None)
        if got is None:
            got = {sym: self.contracts[sym] for sym, cid in self.trade_owner.items()
                   if conn_defs.type_of(cid) == "ibkr_trades" and self.contracts.get(sym)}
            self.store.set_setting(OLD_NAMES, got)
        return {k: v for k, v in got.items() if isinstance(v, str)} if isinstance(got, dict) else {}

    def on_position(self, p: dict):
        """One of NinjaTrader's positions (add-on 13): stored, and where it puts right a fill the app never got, the
        open charts and the journal ask again ("fill" with no fill: from the start)."""
        if self.store.add_position(p):
            log.info("position %s %s %s: not what the fills stored had (one never reached the app)", p["account"],
                     p["contract"], f"{p['qty']:+d}" if p["qty"] else "flat")
            self.broadcast({"type": "fill"})

    def on_flat(self, account: str, ts: int):
        """The add-on's connect: the account's contracts it sent no position for are flat."""
        if gone := self.store.flat_others(account, ts):
            log.info("positions %s %s: flat in NinjaTrader (a fill closing them never reached the app)", account,
                     ", ".join(gone))
            self.broadcast({"type": "fill"})

    def on_contract(self, symbol: str, contract: str, was: str | None = None):
        """The contract the feed now sends for `symbol`. A change is a roll: kept, logged and sent to the alert
        channels, since prices jump by the spread between the contracts (stored bars are not adjusted). `was`: the
        name the feed gave this same contract before (the IBKR futures plugin, by its last trading day). The stored
        name is renamed without a roll only when it is that, at the first report after the update (OLD_NAMES), never
        later: CLF7's old name ("CL 12-26") is CLZ6's new one."""
        old = self.contracts.get(symbol)
        noted = self.old_names.pop(symbol, None)   # its first report since: whatever it says, the old naming is done
        if noted is not None:
            self.store.set_setting(OLD_NAMES, self.old_names)
        if old == contract:
            return
        self.contracts[symbol] = contract
        self.store.set_setting("contracts", self.contracts)
        if old and old == noted == was:
            log.info("%s: %s is now named %s (the same contract)", symbol, old, contract)
            return
        if old:
            log.info("%s rolled from %s to %s", symbol, old, contract)
            self.gapfill.rolled(symbol, now_ms())   # no hole from before it: history now is the new contract's
            self.settlements.rolled(symbol)          # the change from the new contract's settlement, not the old one's
            self.alerts.rolled(symbol)               # the jump to the new contract's first trade crosses nothing
            asyncio.get_running_loop().create_task(self.notifier.send(
                "Contract roll", f"{symbol} is now {contract} (was {old}). Earlier bars are not adjusted for the roll."))

    def quote_wanted(self, symbol: str, source: str) -> bool:
        """False while a better-ranked source, or another of the same rank, has quoted the symbol in the last
        QUOTE_HOLD_MS: two alike (IBKR and Alpaca) don't take turns on every update. IBKR's delayed data ranks
        behind the real-time ones."""
        held = self.quote_from.get(symbol)
        if not held or held[0] == source or now_ms() - held[1] >= QUOTE_HOLD_MS:
            return True

        def rank(src):
            r = QUOTE_RANK.get(src, self.plugin_rank.get(src, 0))
            return r - 0.5 if src == "ibkr" and self.s.ibkr_market_data_type != 1 else r
        return rank(held[0]) < rank(source)

    def on_quote(self, symbol: str, last: float | None, prev_close: float | None, volume: float | None = None,
                 source: str = "ninjatrader", at: int | None = None):
        """A watchlist quote. NinjaTrader's (live futures) beat IBKR's, which beat Yahoo's (can lag); a lower source
        fills in when the better one has been silent for QUOTE_HOLD_MS. `at`: the quote's own time, if it has one.
        `symbol` is its quote key (wl.quote_key: "ZS1!" the future, "ZS" the stock); a chart future's root ("ES", as
        the TCP lines and plugins name it) is its future's. A future's change is from its settlement; anything else's
        from the quote's previous close, else the one known for its trading day (_close_for)."""
        if last is None or last != last:  # None or NaN
            return
        symbol = wl.quote_key(symbol, self.instruments)
        if not self.quote_wanted(symbol, source):
            return
        now = now_ms()
        self.quote_from[symbol] = (source, now)
        prev = prev_close if prev_close is not None and prev_close == prev_close else None
        if prev == 0 and source not in ("fred", "eia", "data"):   # a series' 0 is a value (FRED:USREC); a price's is none
            prev = None
        ts = min(at or now, now)
        root = wl.future_root(symbol)
        try:   # a future: from its latest settlement until the next one; a stock never from its root's (ZS from ZS1!'s)
            ref = self.settlements.reference(root, now) if root else None
        except Exception as e:   # not saved (a full disk): the feed's previous close, and the feed carries on
            log.warning("%s settlement not worked out: %s", symbol, e)
            ref = None
        if ref:
            prev = ref["price"]
        elif root is None and source not in ("fred", "eia", "data", "sim"):
            prev = self._close_for(symbol, prev, ts)
        chg = last - prev if prev is not None else None   # pct of prev's size: from below 0 (NFCI), a rise is up
        vol = volume if volume is not None and volume == volume and volume >= 0 else None
        self.quotes[symbol] = {"s": symbol, "last": last, "prev": prev, "chg": chg,
                               "pct": chg / abs(prev) * 100 if prev else None, "vol": vol, "ts": ts, "src": source,
                               "rt": self._rt(source, at, now), **({"settle_t": ref["t"]} if ref else {})}
        self.quotes_dirty.add(symbol)

    def _close_for(self, symbol: str, prev: float | None, ts: int) -> float | None:
        """The previous close a stock's, ETF's, index's, currency pair's or crypto's quote at `ts` is set against: its
        own, kept for its trading day (yahoo.market_day: a stock's from midnight New York); without one (IBKR sends
        none for some tickers), the one known for that day, from an earlier quote of any source or the fill
        (close_hint). Never one of another day: a stock's previous close changes with its next session. With none
        known, a stock's is asked of Yahoo (CloseFill), once that day, and the quote goes out without it meanwhile."""
        day = market_day(symbol, ts)
        if prev is not None:
            self.closes[symbol] = (day, prev)
            return prev
        known = self.closes.get(symbol)
        if known and known[0] == day:
            return known[1]
        self.close_fill.want(symbol, day, ts)
        return None

    def close_hint(self, symbol: str, day, prev: float) -> bool:
        """A previous close from elsewhere (CloseFill: Yahoo's daily bars) for `symbol`'s trading day `day`: kept as
        its quotes' reference that day, unless a source gave one meanwhile (or a later day's is known). The quote shown
        now, if it has none and is of that day, gets its change at once; its price, source and time stay. True if
        kept."""
        if not isinstance(prev, (int, float)) or prev != prev or prev == 0 or wl.future_root(symbol):
            return False
        known = self.closes.get(symbol)
        if known and known[0] >= day:
            return False
        self.closes[symbol] = (day, float(prev))
        q = self.quotes.get(symbol)
        if q and q.get("prev") is None and q.get("src") not in ("fred", "eia", "data", "sim") \
                and market_day(symbol, q["ts"]) == day:
            chg = q["last"] - prev
            self.quotes[symbol] = {**q, "prev": float(prev), "chg": chg, "pct": chg / abs(prev) * 100}
            self.quotes_dirty.add(symbol)
        return True

    def _rt(self, source: str, at: int | None, now: int) -> str:
        """How current a quote is, for the watchlist's marker: live, delayed, closed (its last trade is over an hour
        old: the market is shut), eod (daily data: FRED, EIA) or sim."""
        if source in ("fred", "eia", "data"):
            return "eod"
        if source == "sim":
            return "sim"
        if at and now - at > 3_600_000:
            return "closed"
        if source == "ibkr" and self.s.ibkr_market_data_type != 1:
            return "delayed"
        if source == "yahoo" and (not at or now - at > 120_000):   # Yahoo's futures and indexes run 10-15 min behind
            return "delayed"
        return "live"

    def _seed_nt_settings(self):
        """config/nt_settings.json: starting settings that differ from NinjaTrader's defaults, applied once per
        version; a setting already changed in the app is kept."""
        path = self.s.config_dir / "nt_settings.json"
        if not path.exists():
            return
        try:
            seed = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("config/nt_settings.json is not valid JSON; skipped")
            return
        if self.store.get_setting("nt_seed", 0) >= seed.get("version", 1):
            return
        try:
            reset = set((seed.get("reset") or {}).get("amt", []))   # back to NinjaTrader's default, whatever the app had
            self.amt.seed(seed.get("amt", {}), reset)   # its alert settings every symbol's
            for key, values in seed.get("indicators", {}).items():
                self.ind.put(key, {**values, **self.ind.overrides.get(key, {})})
        except (KeyError, ValueError, TypeError) as e:
            log.warning("config/nt_settings.json not applied: %s", e)
            return
        self.store.set_setting("nt_seed", seed.get("version", 1))
        log.info("applied config/nt_settings.json")

    # --- watchlists ------------------------------------------------------------------------------
    def set_watchlists(self, doc, base: int | None = None) -> dict:
        """Saves the app's edited lists (ValueError if malformed) with the next version, tells every open app, and
        moves the quote subscriptions: a feed with watch_changed(market) hears about it. base: the version the edits
        were made on; another one than the lists' (another device saved meanwhile) is refused (wl.Stale). None (an
        app from before versions): last write wins."""
        if base is not None and base != self.wl.get("version"):
            raise wl.Stale(f"The lists changed meanwhile (version {self.wl.get('version')}, not {base}): "
                           "read them again and make the edit there")
        doc = {**wl.clean(doc, only=self.symbols.only_futures, stored=wl.items_in(self.wl)),
               "version": wl.next_version(self.wl.get("version"))}
        self.store.set_setting("watchlists", doc)
        self.wl = doc
        self.watch_keys = self._watch_set()
        for f in self.feed_objs:
            if hasattr(f, "watch_changed"):
                f.watch_changed(self)
        self.broadcast({"type": "watchlists", "watchlists": doc})
        return doc

    def _bare_futures(self):
        """At start: a list's bare root that no stock has for a ticker by the symbol database's listings (ZB, YM),
        written as its future (ZB1!) and saved, as a save from the app writes it: an older list's ZB keeps its quote
        and chart. One that would be too long written so keeps its roots as they are (wl.clean's stored). The drawings
        made on an older list's bare ZB chart stay as ZB's: the chart reads them under both (app._drawing_keys)."""
        try:
            doc = {**wl.clean(self.wl, only=self.symbols.only_futures, stored=wl.items_in(self.wl)),
                   "version": self.wl.get("version")}
        except ValueError:   # lists clean() refuses (none from load()): they stay as they are
            return
        if doc != self.wl:
            # saved over the lists shown only: a symbol load() left out (or lists it could not read) stays saved
            if self.store.get_setting("watchlists") == self.wl:
                self.store.set_setting("watchlists", doc)
            self.wl = doc
            self.watch_keys = self._watch_set()

    def _watch_set(self) -> set[str]:
        now = time.monotonic()
        for k in [k for k, until in self.chart_keys.items() if until <= now]:
            del self.chart_keys[k]
        # a free-data chart's MET (MetLife) once MET runs here is MET1!'s (on_quote): the stock feeds stop quoting it
        keys = wl.quote_keys(self.wl, self.instruments) | {f"{s}1!" for s in self.instruments} \
            | {k for k in self.chart_keys if k not in self.instruments}
        for k in [k for k in self.quotes if k not in keys]:   # off the lists, its chart closed: not kept, nor in each hello
            del self.quotes[k]
            self.quote_from.pop(k, None)
            self.quotes_dirty.discard(k)
        for k in [k for k in self.closes if k not in keys]:
            del self.closes[k]
        return keys

    def watch_chart(self, key: str):
        """A free-data chart's symbol is quoted as a listed one is, while the chart polls for its bars (ext.py), so
        its live quote moves the last bar: from the best feed that has it (IBKR's real-time one, say). `key` is its
        quote key: KC1! (coffee) and KC (Kingsoft Cloud) are quoted each as itself, both when both are charted."""
        now = time.monotonic()
        moved = key not in self.chart_keys or any(until <= now for until in self.chart_keys.values())
        self.chart_keys[key] = now + CHART_QUOTE_S
        if moved:
            keys = self._watch_set()
            if keys != self.watch_keys:
                self.watch_keys = keys
                for f in self.feed_objs:
                    if hasattr(f, "watch_changed"):
                        f.watch_changed(self)

    def recent_minutes(self, symbol: str, until: int | None = None) -> tuple[int, list[Bar]]:
        """For studies built from 1m bars: everything is flushed; stored minutes before the returned split are
        complete, and copies of the in-memory minutes from it on (the same cut as chart history). While the database
        can't be written, the minutes waiting for it are left out. until: bar replay's clock, the minutes ending by it
        only (it is cut to the minute: none of a minute's trades after it)."""
        self.flush_for_read()
        last = self.m1[symbol].last
        split = last.t if last and symbol in self.m1_ticked else now_ms() // 60_000 * 60_000
        live = [replace(b) for b in self.m1[symbol].bars if b.t >= split]
        if until is None:
            return split, live
        cut = until // 60_000 * 60_000
        return (cut, []) if cut <= split else (split, [b for b in live if b.t < cut])

    def flush(self):
        """Writes the buffered trades and changed 1m bars. A failed write (the database locked by an import past the
        busy timeout, a full disk) raises and keeps them for the next try."""
        if not self.tick_buf and not self.dirty:
            return
        try:
            self.store.write(self.tick_buf, [(sym, b) for (sym, _), b in self.dirty.items()])
        except Exception:
            if len(self.tick_buf) > UNWRITTEN_MAX:   # counted in _write_failed's line
                self.unwritten_dropped += len(self.tick_buf) - UNWRITTEN_MAX
                del self.tick_buf[:-UNWRITTEN_MAX]
            raise
        self.tick_buf = []
        self.dirty.clear()
        if self.write_failed_at is not None:
            log.info("database writable again (%d of the trades waiting for it dropped)", self.unwritten_dropped)
        self.write_failed_at, self.unwritten_dropped = None, 0

    def _write_failed(self, msg: str, *args):
        """A failed store write the caller goes on from: logged in full the first time, then in a line at most once a
        minute until a write works again (a full disk: a traceback from the store loop every second would fill it)."""
        now = now_ms()
        if self.write_failed_at is None:
            log.exception(msg, *args)
        elif now - self.write_failed_at >= UNWRITABLE_LOG_MS:
            log.warning("database still not writable: %d trades waiting for it, %d older ones dropped",
                        len(self.tick_buf), self.unwritten_dropped)
        else:
            return
        self.write_failed_at = now

    def flush_for_read(self, symbol: str = "") -> list[Tick]:
        """flush() before reading the database. A failed write is logged, not raised: while the database can't be
        written (a full disk), charts, studies and the add-on's hello go on with what is stored. Returns the trades of
        `symbol` still waiting to be written, as they came ([] once written): they are not in the database up to the
        cutoff the caller takes next, so they go first in its queue of later trades."""
        try:
            self.flush()
        except Exception:
            self._write_failed("database not writable: reading on without the %d trades waiting for it", len(self.tick_buf))
        return [tk for tk in self.tick_buf if tk.symbol == symbol]

    # --- chart subscriptions -------------------------------------------------------------------
    async def subscribe(self, client: Client, sid: int, symbol: str, spec_text: str, rth: bool = False):
        """rth: regular trading hours only (the instrument's RTH; ignored for one without)."""
        if symbol not in self.instruments:
            raise ValueError(f"unknown symbol {symbol}")
        spec = parse_spec(spec_text)
        rth = bool(rth and self.instruments[symbol].rth)
        key, entry = self._entry(symbol, spec, rth)
        client.loading.add(sid)
        try:
            await entry.ready.wait()
        finally:
            wanted = sid in client.loading
            client.loading.discard(sid)
        if entry.error:
            raise RuntimeError(entry.error)
        if client.closed or not wanted:
            return
        if self.series.get(key) is not entry:   # a gap fill replaced the history while it loaded: load it again
            client.put({"type": "reload", "id": sid})
            return
        self._detach(client, sid)   # not unsubscribe: the AMT subscription made with this id while loading stays
        self.note_recent("charts", [symbol, spec.text, rth])
        entry.subs.add((client, sid))
        client.subs[sid] = entry
        client.put({"type": "history", "id": sid, "symbol": symbol, "spec": spec.text, "kind": spec.kind, "rth": rth,
                    "bars": [b.to_dict() for b in entry.series.bars]})

    def _entry(self, symbol: str, spec: BarSpec, rth: bool) -> tuple[tuple, SeriesEntry]:
        """The loaded (or loading) series for a chart, its history load started if it is new."""
        key = (symbol, spec.text + ("@rth" if rth else ""))
        entry = self.series.get(key)
        if entry is None:
            inst = self.instruments[symbol]
            # Flush, cut, and register in one synchronous step: ticks up to the cutoff come from the
            # database, everything after lands in pending_ticks. Nothing is counted twice or missed.
            # The steps that can fail run before anything is registered.
            unwritten = self.flush_for_read(symbol)
            cutoff = self.store.max_tick_rowid()
            m1_last = self.m1[symbol].last
            # Before any live tick, stored 1m bars are complete; split at the current minute so none are lost.
            split_t = m1_last.t if m1_last and symbol in self.m1_ticked else now_ms() // 60_000 * 60_000
            if unwritten:   # stored minutes are complete only before the first trade not written
                split_t = min(split_t, min(tk.ts for tk in unwritten) // 60_000 * 60_000)
            entry = SeriesEntry(symbol, make_series(spec, inst.tick_size, inst.decimals, self.s.max_bars, inst.rth,
                                                    rth_only=rth, group=inst.calendar))
            entry.pending_ticks = unwritten
            self.series[key] = entry
            self.by_symbol[symbol].append(entry)
            asyncio.create_task(self._load(entry, spec, cutoff, split_t, key))
        return key, entry

    def note_recent(self, kind: str, item: list):
        """Remembers a chart ([symbol, spec, rth]) or AMT run ([symbol, minutes]) as used last."""
        lst = self.recent[kind]
        if lst and lst[0] == item:
            return
        self.recent[kind] = [item, *[x for x in lst if x != item]][:RECENT_KEEP]
        try:
            self.store.set_setting("recent_charts", self.recent)
        except Exception as e:   # only for the prewarm after a restart: no reason for a chart not to open
            log.warning("charts used last not saved: %s", e)

    async def _prewarm(self):
        """After a restart (a reboot, an update), loads the charts used last, their session profiles and AMT runs
        in the background, so the first look is as quick as any other."""
        await asyncio.sleep(PREWARM_DELAY_S)   # the feeds connect first
        t0 = time.perf_counter()
        for sym, spec_text, rth in list(self.recent["charts"]):
            try:
                if sym in self.instruments:
                    _, entry = self._entry(sym, parse_spec(spec_text), bool(rth and self.instruments[sym].rth))
                    await entry.ready.wait()
            except Exception:   # a spec or symbol from an older version: skip it
                log.warning("prewarm of %s %s skipped", sym, spec_text, exc_info=True)
        for sym, minutes in list(self.recent["amt"]):
            try:
                if sym in self.instruments:
                    await self.amt.entry(sym, max(1, min(30, int(minutes))))
            except Exception:
                log.warning("AMT prewarm of %s %sm skipped", sym, minutes, exc_info=True)
        if self.recent["charts"] or self.recent["amt"]:
            log.info("prewarmed %d charts and %d AMT runs in %.1fs", len(self.recent["charts"]), len(self.recent["amt"]),
                     time.perf_counter() - t0)

    # --- volume profiles -----------------------------------------------------------------------
    async def book(self, symbol: str) -> ProfileBook:
        """Session profiles for a symbol, loaded from history on first use (same cutoff scheme as series)."""
        be = self.books.get(symbol)
        if be is None:
            unwritten = self.flush_for_read(symbol)
            cutoff = self.store.max_tick_rowid()
            be = self.books[symbol] = BookEntry(ProfileBook(self.instruments[symbol]))
            be.pending = unwritten
            try:
                be.book = await workers.run("chart", workers.load_book, self.store.path, be.book, symbol, now_ms(), cutoff)
            except Exception:
                log.exception("profile history failed for %s", symbol)
            for tk in be.pending:
                be.book.add(tk)
            be.pending = []
            be.ready.set()
        await be.ready.wait()
        return be.book

    async def range_profile(self, symbol: str, start: int, end: int, rth: bool = False) -> dict | None:
        """Visible-range or composite profile (rth: the regular hours only). Raw ticks up to RANGE_TICKS_MAX_MS, 1m
        bars beyond."""
        self.flush_for_read()
        inst = self.instruments[symbol]
        return await asyncio.to_thread(range_profile, self.store.path, inst, symbol, start, end,
                                       end - start <= RANGE_TICKS_MAX_MS, rth)

    async def _older_days(self, symbol: str, sr: Series, spec: BarSpec):
        """A daily, weekly or monthly chart of a future with under a year stored (one just added), or a daily indicator
        alert's series: the days before its stored data from Yahoo's front-month bars, so it opens with years of
        history, not two weeks. The bars this app stored stay its own; the first one, a partial day, takes Yahoo's
        open, high, low and volume. Not on the simulator: its trades are invented, and real ones never mix in."""
        from .feeds.yahoo import yahoo_symbol
        if spec.kind != "time" or not spec.cal or sr.rth_only or len(sr.bars) >= (sr.bars.maxlen or 0) or self.s.feed == "sim":
            return
        first = sr.bars[0].t if sr.bars else now_ms()
        ticker = yahoo_symbol(symbol, None, True)
        if now_ms() - first > SHORT_HISTORY_MS or not ticker or self.http is None:
            return
        try:
            rows, _ = await self.ext._yahoo(ticker, "1D")
        except Exception as e:   # never the chart's failure: it opens with what is stored
            log.info("older days for %s not read: %s", symbol, e if isinstance(e, ValueError) else type(e).__name__)
            return
        inst = self.instruments[symbol]
        days = make_series(spec, inst.tick_size, inst.decimals, sr.bars.maxlen, studies=False, group=inst.calendar)
        for r in rows:   # stamped at their session's 17:00 CT open, as this app's daily bars are (a halt's folded)
            days._bar(Bar(t=r["t"] + 12 * 3_600_000, o=inst.round(r["o"]), h=inst.round(r["h"]), l=inst.round(r["l"]),
                          c=inst.round(r["c"]), v=int(r.get("v") or 0)))
        older = [b for b in days.bars if b.t < first]
        same = next((b for b in days.bars if b.t == first), None)
        if same is not None and sr.bars:
            b0 = sr.bars[0]
            b0.o, b0.h, b0.l, b0.v = same.o, max(b0.h, same.h), min(b0.l, same.l), max(b0.v, same.v)
        older = older[-(sr.bars.maxlen - len(sr.bars)):]
        for i, b in enumerate(reversed(older)):
            b.seq = -1 - i   # before the stored bars' 1, 2, ...
            sr.bars.appendleft(b)

    async def _load(self, entry: SeriesEntry, spec: BarSpec, cutoff: int, split_t: int, key: tuple):
        t0 = time.perf_counter()
        try:
            entry.series = await workers.run("chart", workers.build_series, self.store.path, entry.series, entry.symbol,
                                             spec, cutoff, split_t, now_ms())
            await self._older_days(entry.symbol, entry.series, spec)   # first: trades arriving while it asks Yahoo wait in pending_ticks
            for tk in entry.pending_ticks:
                entry.series.on_tick(tk)
            log.info("built %s %s: %d bars in %.2fs", entry.symbol, spec.text, len(entry.series.bars),
                     time.perf_counter() - t0)
        except Exception as e:
            log.exception("history load failed for %s %s", entry.symbol, spec.text)
            entry.error = str(e)
            if self.series.get(key) is entry:
                del self.series[key]
            if entry in self.by_symbol[entry.symbol]:
                self.by_symbol[entry.symbol].remove(entry)
        finally:
            entry.pending_ticks = []
            entry.ready.set()

    def unsubscribe(self, client: Client, sid: int):
        client.loading.discard(sid)
        self.amt.unsubscribe(client, sid)
        self._detach(client, sid)

    def _detach(self, client: Client, sid: int):
        """Takes the chart subscription `sid` off its series."""
        entry = client.subs.pop(sid, None)
        client.pending.pop(sid, None)
        if entry:
            entry.subs.discard((client, sid))
            if not entry.subs:
                entry.idle_since = time.monotonic()

    def connect(self) -> Client:
        c = Client()
        self.clients.add(c)
        c.put(self.hello())
        return c

    def set_tape(self, c: Client, symbol: str | None, min_size: int = 1):
        """Live trades (grouped like store.sweeps) of at least min_size for one symbol, or none."""
        if c.tape:
            subs = self.tape_subs.get(c.tape[0])
            if subs:
                subs.discard(c)
                if not subs:
                    del self.tape_subs[c.tape[0]]
        c.tape = None
        if symbol:
            if symbol not in self.instruments:
                raise ValueError(f"unknown symbol {symbol}")
            c.tape = (symbol, max(1, int(min_size)))
            self.tape_subs.setdefault(symbol, set()).add(c)

    def disconnect(self, c: Client):
        self.set_tape(c, None)
        c.closed = True
        replaying = {sid for (cl, sid) in [*self.amt.replays, *self.amt.replay_locks] if cl is c}
        for sid in set(c.subs) | set(c.amt_entries) | replaying:   # AMT may be on while its chart was still loading
            self.unsubscribe(c, sid)
        self.clients.discard(c)

    def drop_clients(self):
        """Every open app reconnects, signing in again if it has to: the login changed."""
        for c in list(self.clients):
            c.closed = True
            try:
                c.outbox.put_nowait({"type": "reconnect"})   # wakes its writer, which then closes the socket
            except asyncio.QueueFull:
                pass

    def broadcast(self, msg: dict):
        for c in self.clients:
            c.put(msg)

    def hello(self) -> dict:
        return {
            "type": "hello",
            "instruments": {s: i.to_dict() for s, i in self.instruments.items()},
            "watchlists": self.wl,
            "levels": {s: ls.to_dict() for s in self.instruments if (ls := self.levels.get(s))},
            "alerts": self.alerts.list(),
            "events": self.store.list_events(30),
            "quotes": list(self.quotes.values()),
            "last": {s: t.price for s, t in self.last_tick.items()},
            "notify": self.notifier.channels,
            "feeds": self.feed_names,
            "version": getattr(self, "app_version", ""),   # the web app's: a page from another reloads
            "catalog": self.catalog(),
            "only_futures": sorted(self.symbols.only_futures),   # a bare root of one charts as its future (6E1!)
            "update": self.updates.public(),
            "config": self.config_hash(),
            "holidays": self.holiday_hours(),
        }

    def holiday_hours(self) -> dict:
        """CME's holiday hours, by date, all the server's calendar holds (from 2000 to two years ahead, about 20 KB):
        the browser's bar-close countdown, its pivots' trade dates (a halt day's short session counts toward the next)
        as far back as a chart or a replay goes, and its trend lines' market time as far ahead as the server's alerts
        count it (a line with a point placed months ahead)."""
        return {d.isoformat(): {k: v for k, v in h.items() if k != "name"} for d, h in sessions.holidays().items()}

    # --- alerts --------------------------------------------------------------------------------
    def alerts_changed(self):
        self.alerts.reload()
        self.study_sources.ensure()
        self.ind_alerts.ensure()
        self.broadcast({"type": "alerts", "alerts": self.alerts.list()})

    def _alert_expired(self):
        self.ind_alerts.ensure()   # restarted, an indicator alert starts afresh: no crossing from while it was off
        self.broadcast({"type": "alerts", "alerts": self.alerts.list()})

    def _on_alert(self, ev: dict):
        log.info("ALERT %s", ev["message"])
        a = self.alerts.alerts.get(ev["alert_id"])
        if a is not None and not a.enabled:   # fired and done (Only once): the same as an expired one
            self.ind_alerts.ensure()
        self.broadcast({"type": "alert", "event": ev})
        self.broadcast({"type": "alerts", "alerts": self.alerts.list()})
        asyncio.get_running_loop().create_task(self._deliver(ev))

    async def _deliver(self, ev: dict):
        delivered = await self.notifier.send(f"{ev['symbol']} alert", ev["message"])
        if ev["id"] is not None:   # None: its event couldn't be written
            self.store.set_event_delivered(ev["id"], delivered)

    # --- background loops ------------------------------------------------------------------------
    async def _client_loop(self):
        n = 0
        while True:
            await asyncio.sleep(CLIENT_FLUSH_S)
            n += 1
            quotes = None
            if n % QUOTES_EVERY == 0 and self.quotes_dirty:
                quotes = [self.quotes[s] for s in self.quotes_dirty]
                self.quotes_dirty.clear()
            tape = {sym: sweeps(ticks) for sym, ticks in self.tape_buf.items() if ticks}
            self.tape_buf.clear()
            for sym, trades in tape.items():   # a sweep's price on the tick, as /api/trades (so a reload) shows it
                inst = self.instruments.get(sym)
                for t in trades if inst else ():
                    t[1] = round(round(t[1] / inst.tick_size) * inst.tick_size, 10)
            for c in list(self.clients):
                c.drain()
                if c.tape and c.tape[0] in tape:
                    trades = [t for t in tape[c.tape[0]] if t[2] >= c.tape[1]][-500:]
                    if trades:
                        c.put({"type": "trades", "symbol": c.tape[0], "trades": trades})
                if quotes:
                    c.put({"type": "quotes", "quotes": quotes})

    async def _store_loop(self):
        while True:
            await asyncio.sleep(STORE_FLUSH_S)
            try:
                self.flush()
            except Exception:
                self._write_failed("store flush failed")

    def levels_changed(self):
        """The daily levels changed (daily_levels.py: a file, link or pasted line): each symbol's set made again; one
        that changed starts its level alerts afresh, and every page hears. Each day's are kept for bar replay."""
        self.daily.remember()
        for sym in self.levels.poll():
            self.alerts.reset_levels(sym)
            ls = self.levels.get(sym)
            self.broadcast({"type": "levels", "symbol": sym, "levels": ls.to_dict() if ls else None})

    async def _watchdog_loop(self):
        while True:
            await stopping.pause(WATCHDOG_S)   # not after a stop the heartbeat's request swallowed (stopping.py)
            try:
                await self._watchdog_pass()
            except Exception:
                log.exception("watchdog pass failed")

    async def _watchdog_pass(self):
        """Notifies when a symbol stops ticking during Globex hours, and pings an external heartbeat URL
        (e.g. healthchecks.io) so a dead PC still gets noticed. Each session open gets the same
        stale_after_s grace as process start. Outside session hours the heartbeat always pings, and bar-close
        indicator alerts check the session's last bars (those the feed filled to the close)."""
        if hasattr(self, "gapfill"):   # older history: the next month, or one NinjaTrader never answered dropped
            self.gapfill.kick_older()
        now = now_ms()
        is_open = {sym: sessions.is_open(now, inst.calendar) for sym, inst in self.instruments.items()}
        limit = self.s.stale_after_s * 1000
        for sym, tk in list(self.last_tick.items()):   # shut a minute (a late print is in): no trade comes to close its last bars
            cal = self.instruments[sym].calendar
            if is_open[sym] or sessions.is_open(now - 60_000, cal):
                continue
            # not when the feed stopped before the close (NinjaTrader closed, its connection lost): the bar lacks the
            # session's last trades (a gap fill brings them), so it waits for the next trade, as before
            if tk.ts >= sessions.last_close(now, cal) - limit:
                self.alerts.on_shut(Tick(sym, now, tk.price, 0, 0))
        if self.s.feed != "none" and now - self.started > limit:
            open_ms = sessions.session_start(now)
            for sym, inst in self.instruments.items():
                if not is_open[sym] or inst.quiet:   # weekend, daily break, exchange holiday; or no alerts asked
                    continue
                tk = self.last_tick.get(sym)
                stale = now - max(tk.ts if tk else 0, open_ms) > limit
                if stale and sym not in self.stale:
                    self.stale.add(sym)
                    await self.notifier.send("Feed stale", f"No {sym} ticks for {self.s.stale_after_s}s+")
                elif not stale and sym in self.stale:
                    self.stale.discard(sym)
                    for f in self.feed_objs:   # the hole it left, asked for while the add-on stays connected
                        if hasattr(f, "ticking_again"):
                            f.ticking_again(self, sym)
                    await self.notifier.send("Feed back", f"{sym} ticking again")
        if self.s.heartbeat_url and not any(is_open[s] for s in self.stale):
            try:
                r = await self.http.get(self.s.heartbeat_url)
                self.heartbeat_last = (r.is_success, now_ms())
            except Exception as e:   # never log the URL: it is a secret
                self.heartbeat_last = (False, now_ms())
                log.warning("heartbeat ping failed: %s", type(e).__name__)

    async def _housekeeping_loop(self):
        while True:
            await asyncio.sleep(60)
            # a free-data chart closed CHART_QUOTE_S ago: its symbol is quoted no more, with no other one polling since
            if any(until <= time.monotonic() for until in self.chart_keys.values()):
                keys = self._watch_set()
                if keys != self.watch_keys:
                    self.watch_keys = keys
                    for f in self.feed_objs:
                        if hasattr(f, "watch_changed"):
                            f.watch_changed(self)
            cutoff = time.monotonic() - IDLE_SERIES_S
            idle = sorted(((e.idle_since, key) for key, e in self.series.items() if not e.subs and e.ready.is_set()),
                          key=lambda x: x[0])
            for i, (since, key) in enumerate(idle):
                if since < cutoff or i < len(idle) - IDLE_SERIES_MAX:
                    entry = self.series.pop(key)
                    self.by_symbol[entry.symbol].remove(entry)

    def health(self) -> dict:
        now = now_ms()
        return {
            "feeds": self.feed_names,
            "feed_status": {f.name: {**f.stats, "last_msg_age_s": round((now - f.stats["last_msg_ms"]) / 1000, 1) if f.stats.get("last_msg_ms") else None}
                            for f in self.feed_objs if isinstance(getattr(f, "stats", None), dict)},
            "session_open": {s: sessions.is_open(now, i.calendar) for s, i in self.instruments.items()},
            "contracts": self.contracts,
            "gap_fill": self.gapfill.health(),
            "holiday": next((d["name"] for day, d in sessions.holidays().items() if day == sessions.to_ct(now).date()), None),
            "symbols": {s: {"last": t.price, "age_s": round((now - t.ts) / 1000, 1)}
                        for s, t in self.last_tick.items()},
            "stale": sorted(self.stale),
            "clients": len(self.clients),
            "open_series": [f"{s} {k}" for s, k in self.series],
            "notify": self.notifier.channels,
            "levels": {s: ls.created or ls.date for s in self.instruments if (ls := self.levels.get(s))},
            "amt": self.amt.health(),
            "gaps": self.gaps.health(),
            "close_fill": {**self.close_fill.stats, "waiting": len(self.close_fill.queue)},
        }
