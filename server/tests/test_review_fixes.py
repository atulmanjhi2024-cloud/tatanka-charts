"""Regression tests for the review findings fixed after the first build."""
import asyncio
import dataclasses
import logging
from datetime import datetime
from types import SimpleNamespace

from chartsrv import amt
from chartsrv import market as market_mod
from chartsrv.alerts import AlertEngine
from chartsrv.backfill import read_csv
from chartsrv import watchlists as wl
from chartsrv.config import Instrument, load_instruments, load_settings
from chartsrv.daily_levels import Source
from chartsrv.feeds import make_feeds
from chartsrv.levels import level_set
from chartsrv.market import Client, Market
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT, snap
from chartsrv.store import Store

ES = Instrument("ES", "E-mini", 0.25, 50)
CL = Instrument("CL", "Crude", 0.01, 1000)
LINE = "2026-01-15, 7820.00, 7850.00, 7862.50, 7800.00"   # pivot, R1, R2, S1 (invented)


def ms(y, mo, d, h, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=CT).timestamp() * 1000)


# --- alerts --------------------------------------------------------------------------------------------
def engine(tmp_path, pasted=None):
    """An alert engine on these symbols' daily levels: {symbol: pasted line}."""
    sets = {sym: level_set(sym, *amt.parse_levels(text)[:2], Source(origin="paste")) for sym, text in (pasted or {}).items()}
    store = Store(tmp_path / "t.db")
    fired = []
    return store, AlertEngine(store, {"ES": ES, "CL": CL}, SimpleNamespace(get=sets.get), fired.append), fired


def feed(eng, sym, prices, t0=1_000_000):
    for i, p in enumerate(prices):
        eng.on_tick(Tick(sym, t0 + i * 1000, p, 1, 0))


def test_level_alert_with_wide_tolerance_does_not_storm(tmp_path):
    store, eng, fired = engine(tmp_path, {"ES": LINE})
    store.add_alert("ES", "level", {"levels": ["R1"], "tolerance_ticks": 10, "rearm_ticks": 8}, True, "")
    eng.reload()
    feed(eng, "ES", [7857.0, 7847.75] + [7847.75] * 9)   # sits 9 ticks under R1 7850, inside the band
    assert len(fired) == 1
    feed(eng, "ES", [7846.0, 7847.75])                   # 16 ticks away: still inside band + rearm
    assert len(fired) == 1
    feed(eng, "ES", [7845.0, 7847.75])                   # 20 ticks away = band edge + 10: re-armed
    assert len(fired) == 2


def test_float32_noise_levels_fire_on_exact_touch(tmp_path):
    store, eng, fired = engine(tmp_path, {"CL": "2026-01-15, 91.00, 93.83999633789062, 90.00"})   # a level file's float32
    store.add_alert("CL", "level", {"levels": ["R1"], "tolerance_ticks": 0, "rearm_ticks": 8}, True, "")
    eng.reload()
    feed(eng, "CL", [93.95, 93.90, 93.84, 93.84])          # retest from above, exactly on the level
    assert [e["message"].split(" (")[0] for e in fired] == ["CL touched R1 93.84"]


def test_price_alert_created_on_the_price_fires_on_first_move(tmp_path):
    store, eng, fired = engine(tmp_path)
    feed(eng, "ES", [7850.0])
    store.add_alert("ES", "price", {"price": 7850.0, "direction": "below"}, False, "")
    store.add_alert("ES", "price", {"price": 7850.0, "direction": "above"}, False, "")
    eng.reload()
    feed(eng, "ES", [7850.0, 7849.75, 7848.0])
    assert [e["alert_id"] for e in fired] == [1]          # below fires; above saw a move the wrong way
    feed(eng, "ES", [7850.25])
    assert [e["alert_id"] for e in fired] == [1, 2]       # and above fires on the later cross


def test_restart_seeds_alert_engine_with_last_stored_tick(tmp_path, monkeypatch):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        m.store.write([Tick("ES", 1_000, 7840.0, 1, 0)], [])
        m.store.add_alert("ES", "price", {"price": 7850.0, "direction": "above"}, False, "")
        m.alerts.reload()
        m.seed()
        m.on_tick(Tick("ES", 2_000, 7855.0, 1, 0))
        return m.store.list_events()
    assert len(asyncio.run(go())) == 1


# --- sessions ------------------------------------------------------------------------------------------
def test_snap_moves_break_prints_into_their_session():
    assert snap(ms(2026, 10, 14, 16, 0, 0) + 200) == ms(2026, 10, 14, 16) - 1      # late print -> close
    assert snap(ms(2026, 10, 14, 16, 59, 59)) == ms(2026, 10, 14, 17)              # early print -> open
    assert snap(ms(2026, 10, 16, 16, 45)) == ms(2026, 10, 16, 16) - 1              # Friday: always the close
    assert snap(ms(2026, 10, 18, 16, 10)) == ms(2026, 10, 18, 17)                  # Sunday pre-open -> open
    for ts in (ms(2026, 10, 14, 10), ms(2026, 10, 17, 16, 10)):                    # open, Saturday: unchanged
        assert snap(ts) == ts


# --- market hub ----------------------------------------------------------------------------------------
class FakeNotifier:
    channels: list = []

    def __init__(self):
        self.sent = []

    async def send(self, title, message):
        self.sent.append((title, message))
        return ""


class FakeHttp:
    def __init__(self, exc=None):
        self.calls, self.exc = 0, exc

    async def get(self, url):
        self.calls += 1
        if self.exc:
            raise self.exc


def build_market(tmp_path, monkeypatch, feed="none", http=None, **env):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_FEED", feed)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    s = load_settings()
    inst = load_instruments(s.config_dir)
    notifier = FakeNotifier()
    store = Store(s.db_path)
    m = Market(s, inst, wl.load(store, s.config_dir), store, notifier, http or FakeHttp())
    return m, notifier


async def history(m, client, sid, symbol, spec):
    await m.subscribe(client, sid, symbol, spec)
    while True:
        msg = client.outbox.get_nowait()
        if msg["type"] == "history":
            return msg


def test_unsub_during_history_load_cancels_the_subscription(tmp_path, monkeypatch):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        for i in range(500):
            m.on_tick(Tick("ES", market_mod.now_ms() - 60_000 + i, 7000 + i % 7 * 0.25, 1, 1))
        c = Client()
        sub = asyncio.create_task(m.subscribe(c, 1, "ES", "50t"))
        await asyncio.sleep(0)            # sub is now parked waiting for the history load
        m.unsubscribe(c, 1)
        await sub
        msgs = []
        while not c.outbox.empty():
            msgs.append(c.outbox.get_nowait())
        return msgs, c.subs, [e.subs for e in m.by_symbol["ES"]]
    msgs, subs, entry_subs = asyncio.run(go())
    assert msgs == [] and subs == {} and entry_subs == [set()]


def test_backfilled_bars_are_not_dropped_before_live_ticks(tmp_path, monkeypatch):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        t0 = market_mod.now_ms() // 60_000 * 60_000 - 30 * 60_000
        m.store.insert_bars_if_missing("ES", [Bar(t=t0 + i * 60_000, o=1, h=2, l=0.5, c=1.5, v=10) for i in range(30)])
        m.seed()
        return await history(m, Client(), 1, "ES", "1m")
    assert len(asyncio.run(go())["bars"]) == 30


def test_history_window_is_sized_in_bars_not_clock_time(tmp_path, monkeypatch):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch, CHART_MAX_BARS=200)
        now = market_mod.now_ms() // 60_000 * 60_000
        old = [Bar(t=now - 5 * 86_400_000 + i * 60_000, o=1, h=2, l=0.5, c=1.5, v=1) for i in range(300)]
        recent = [Bar(t=now - 50 * 60_000 + i * 60_000, o=1, h=2, l=0.5, c=1.5, v=1) for i in range(50)]
        m.store.insert_bars_if_missing("ES", old + recent)   # a 5-day gap, like a weekend
        return await history(m, Client(), 1, "ES", "1m")
    assert len(asyncio.run(go())["bars"]) == 200            # was 50: only the last 200 minutes of clock


def test_failed_flush_does_not_leave_a_stuck_series(tmp_path, monkeypatch):
    """The chart opens with the trade the database didn't take, and once it is written, opens again."""
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        m.on_tick(Tick("ES", market_mod.now_ms(), 7000.0, 1, 1))

        def boom(*a):
            raise RuntimeError("database is locked")
        real = m.store.write
        m.store.write = boom
        first = await history(m, Client(), 1, "ES", "100t")
        m.store.write = real
        m.flush()
        return first, await history(m, Client(), 2, "ES", "200t")
    first, again = asyncio.run(go())
    assert [b["v"] for b in first["bars"]] == [1] and [b["v"] for b in again["bars"]] == [1]


def test_watchdog_gives_each_session_open_a_grace_period(tmp_path, monkeypatch):
    async def go():
        m, notifier = build_market(tmp_path, monkeypatch, feed="sim")
        m.started = 0
        for sym in m.instruments:
            m.last_tick[sym] = Tick(sym, ms(2026, 10, 14, 15, 59, 59), 1.0, 1, 0)
        m.last_tick["ES"] = Tick("ES", ms(2026, 10, 14, 17, 0, 1), 1.0, 1, 0)
        m.instruments["MNQ"] = dataclasses.replace(m.instruments["MNQ"], quiet=True)   # asked for no stale alerts
        monkeypatch.setattr(market_mod, "now_ms", lambda: ms(2026, 10, 14, 17, 0, 5))
        await m._watchdog_pass()
        quiet = list(notifier.sent)
        monkeypatch.setattr(market_mod, "now_ms", lambda: ms(2026, 10, 14, 17, 2, 30))
        await m._watchdog_pass()
        return quiet, notifier.sent
    quiet, later = asyncio.run(go())
    assert quiet == []
    assert sorted(msg.split()[1] for _, msg in later) == ["CL", "ES", "GC", "MES", "NQ"]


def test_watchdog_with_no_tick_feed_stays_quiet_and_keeps_heartbeat(tmp_path, monkeypatch):
    async def go():
        http = FakeHttp()
        m, notifier = build_market(tmp_path, monkeypatch, feed="none", http=http,
                                   CHART_HEARTBEAT_URL="https://hc-ping.example/uuid")
        m.started = 0
        monkeypatch.setattr(market_mod, "now_ms", lambda: ms(2026, 10, 14, 10))
        await m._watchdog_pass()
        return notifier.sent, http.calls
    assert asyncio.run(go()) == ([], 1)


def test_heartbeat_error_of_any_kind_does_not_kill_the_watchdog(tmp_path, monkeypatch, caplog):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="none", http=FakeHttp(exc=ValueError("bad url")),
                            CHART_HEARTBEAT_URL="https://x.example/secret-uuid")
        await m._watchdog_pass()
    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert "heartbeat ping failed: ValueError" in caplog.text and "secret-uuid" not in caplog.text


# --- feeds, backfill, app ------------------------------------------------------------------------------
def test_sim_quotes_never_run_with_a_real_feed(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.delenv("CHART_QUOTES_FEED", raising=False)
    assert load_settings().quotes_feed == "yahoo"   # the backup quote source by default with a real feed
    monkeypatch.setenv("CHART_QUOTES_FEED", "sim")
    names = [f.name for f in make_feeds(load_settings(), m.instruments, m.levels)]
    assert names == ["ninjatrader", "fred"]   # FRED values come from FRED itself, never invented
    monkeypatch.setenv("CHART_QUOTES_FEED", "ibkr, yahoo")
    assert [f.name for f in make_feeds(load_settings(), m.instruments, m.levels)] == ["ninjatrader", "ibkr", "yahoo", "fred"]


def test_backfill_skips_blank_rows(tmp_path):
    p = tmp_path / "ibkr.csv"
    p.write_text("date,open,high,low,close,volume,average,barCount\n"
                 "2024-12-30 17:00:00-06:00,5955.0,5955.0,5952.0,5954.0,637.0,5953.275,249\n"
                 ",,,,,,,\n")
    bars, skipped = read_csv(p, CT)
    assert len(bars) == 1 and skipped == 1 and bars[0].n == 249


def test_static_files_are_revalidated_and_secrets_not_logged(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_FEED", "none")
    from chartsrv.app import app
    with TestClient(app) as c:
        for path in ("/", "/app.js", "/sw.js"):
            assert c.get(path).headers["cache-control"] == "no-cache"
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_contract_roll_is_kept_and_announced(tmp_path, monkeypatch):
    async def go():
        m, notifier = build_market(tmp_path, monkeypatch)
        m.on_contract("ES", "ES 12-26")              # first sighting: recorded, no message
        m.on_contract("ES", "ES 12-26")
        m.on_contract("ES", "ES 03-27")
        await asyncio.sleep(0)
        again, _ = build_market(tmp_path, monkeypatch)   # a restart remembers it
        return notifier.sent, m.health()["contracts"], again.contracts
    sent, health, kept = asyncio.run(go())
    assert sent == [("Contract roll", "ES is now ES 03-27 (was ES 12-26). Earlier bars are not adjusted for the roll.")]
    assert health == kept == {"ES": "ES 03-27"}
