"""Fixes from the market-core bug hunt: replay of small trade-built charts and across closed holidays, a failed store
flush, the study alerts' profiles after a gap fill, seconds charts on a weekend, and database paths with '#' or '%'."""
import asyncio
import sqlite3

import pytest

from chartsrv import backup, replay, sessions
from chartsrv import market as market_mod
from chartsrv.bars import make_series, parse_spec
from chartsrv.market import build_history
from chartsrv.models import Bar, Tick
from chartsrv.store import Store, iter_bars_1m, iter_ticks

from test_replay import BIG, ES, ms, ticks
from test_review_fixes import build_market


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path / "t.db")
    store.write(ticks(ms(2026, 10, 13, 17), ms(2026, 10, 19, 10)), [])   # a trade every 20 s, Tue evening to Monday
    return store.path


# --- replay ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("spec", ["1t", "5s"])
def test_replay_of_a_small_trade_built_chart_keeps_the_bars_before_the_start(db, monkeypatch, spec):
    """A session holds more of these bars than the window: it stops AFTER bars ahead, as time bars do."""
    monkeypatch.setattr(replay, "BEFORE", 100)
    monkeypatch.setattr(replay, "AFTER", 50)
    at, now = ms(2026, 10, 15, 8, 30), ms(2026, 10, 19, 10)
    bars, next_at, _ = replay.build(db, "ES", ES, parse_spec(spec), at, now, BIG)
    assert sum(b.t < at for b in bars) >= 100 and sum(b.t >= at for b in bars) == 50
    assert next_at > bars[-1].t                                    # the bar it stopped in is left out
    more, _, _ = replay.build(db, "ES", ES, parse_spec(spec), next_at, now, BIG)
    i = next(k for k, b in enumerate(more) if b.t == bars[-1].t)
    assert (more[i].o, more[i].c, more[i].v) == (bars[-1].o, bars[-1].c, bars[-1].v) and more[i + 1].t == next_at


def test_replay_of_a_trade_built_chart_plays_on_past_a_closed_holiday(tmp_path):
    sessions.set_calendar({"days": [{"date": "2026-01-01", "name": "New Year's Day", "closed": True}]})
    store = Store(tmp_path / "t.db")
    store.write([tk for tk in ticks(ms(2025, 12, 29, 17), ms(2026, 1, 2, 12), every=60_000)
                 if not ms(2025, 12, 31, 16) <= tk.ts < ms(2026, 1, 1, 17)], [])   # no trades on New Year's Day
    spec, now = parse_spec("100t"), ms(2026, 1, 2, 12)
    bars, next_at, _ = replay.build(store.path, "ES", ES, spec, ms(2025, 12, 31, 10), now, BIG)
    assert next_at == ms(2026, 1, 1, 17)            # Wednesday 17:00 opens New Year's Day: the next one that trades
    more, _, _ = replay.build(store.path, "ES", ES, spec, next_at, now, BIG)
    assert any(b.t == bars[-1].t for b in more) and more[-1].t > ms(2026, 1, 1, 17)


# --- the store flush ---------------------------------------------------------------------------------------
def _locked(*a):
    raise sqlite3.OperationalError("database is locked")


def test_a_failed_flush_keeps_the_trades_and_bars_for_the_next_one(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    t0 = ms(2026, 10, 14, 10)
    for i in range(100):
        m.on_tick(Tick("ES", t0 + i, 7000.0, 1, 1))
    real, m.store.write = m.store.write, _locked   # an import holds the write lock past the busy timeout
    with pytest.raises(sqlite3.OperationalError):
        m.flush()
    m.store.write = real
    m.flush()
    assert m.store.db.execute("SELECT COUNT(*) FROM ticks WHERE symbol='ES'").fetchone()[0] == 100
    assert [b.v for b in iter_bars_1m(m.store.path, "ES", t0, t0 + 60_000)] == [100]


def test_a_failed_flush_in_a_burst_of_trades_does_not_stop_the_feed(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    monkeypatch.setattr(market_mod, "UNWRITTEN_MAX", 25_000, raising=False)
    t0 = ms(2026, 10, 14, 10)
    real, m.store.write = m.store.write, _locked
    for i in range(30_000):   # the flush every 20k trades fails: the feed carries on, the newest 25k are kept
        m.on_tick(Tick("ES", t0 + i, 7000.0, 1, 1))
    assert len(m.tick_buf) == 30_000
    with pytest.raises(sqlite3.OperationalError):
        m.flush()
    assert len(m.tick_buf) == 25_000 and m.tick_buf[0].ts == t0 + 5_000
    m.store.write = real
    m.flush()
    assert m.store.db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 25_000 and not m.tick_buf


# --- study alerts' profiles ------------------------------------------------------------------------------------
def test_a_gap_fill_reloads_the_profiles_study_alerts_read(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_WORKERS", "0")

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        now = market_mod.now_ms()
        m.store.write([Tick("ES", now - 60_000, 6000.0, 5, 1)], [])
        m.store.add_alert("ES", "study", {"study": "poc", "profile": "eth", "tolerance_ticks": 1, "rearm_ticks": 4}, True, "")
        m.alerts.reload()
        m.study_sources.ensure()
        await m.book("ES")
        m.store.write([Tick("ES", now - 30_000, 6010.0, 300, 1)], [])   # a gap fill writes to the store, not on_tick
        m.history_changed("ES")
        await m.book("ES")
        await m.study_sources.watches["ES"].ready.wait()
        return m.study_sources.value("ES", {"study": "poc", "profile": "eth"}, now)
    assert asyncio.run(go())[0] == 6010.0   # (level, its session)


# --- seconds charts --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("spec", ["5s", "15s"])
def test_seconds_charts_reach_back_over_a_weekend(tmp_path, spec):
    """Their window counts stored minutes, like the minute charts': Saturday shows Friday's last bars."""
    store = Store(tmp_path / "t.db")
    thu, fri_close = ms(2026, 10, 15, 17), ms(2026, 10, 16, 16)
    store.write([Tick("ES", t, 7000.0 + t // 5000 % 8 * 0.25, 1, 1) for t in range(thu, fri_close, 5000)], [])
    store.insert_bars_if_missing("ES", [Bar(t=t, o=7000.0, h=7002.0, l=7000.0, c=7001.0, v=12)
                                        for t in range(thu, fri_close, 60_000)])
    s = make_series(parse_spec(spec), 0.25, 2, 3000, ES.rth)
    build_history(store.path, s, "ES", parse_spec(spec), BIG, 0, ms(2026, 10, 17, 12))   # Saturday noon
    size = int(spec[:-1]) * 1000
    assert len(s.bars) == 3000 and s.bars[-1].t == fri_close - size


# --- database paths ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("folder", ["Trading #2", "dir%20x", "pct 100%", "what?"])
def test_history_loads_and_backups_read_a_database_in_any_folder(tmp_path, folder):
    store = Store(tmp_path / folder / "chart.db")
    store.write([Tick("ES", 1_000 + i, 7000.0, 1, 1) for i in range(3)], [])
    assert len(list(iter_ticks(store.path, "ES", 0, BIG))) == 3
    out = backup.make(store.path, tmp_path / folder / "backups")
    assert backup.restore(store.path, out) is not None
    assert [p.name for p in tmp_path.iterdir()] == [folder]   # and no stray database named after a cut-off path
