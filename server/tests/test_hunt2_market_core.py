"""Fixes from the review of the market-core bug hunt: what reads the database while it can't be written (a full disk)
goes on with what is stored and the trades waiting for it; replay of trade-built charts counts the bars a trade opens
and stops between milliseconds, so the browser joins the next stretch on the right bar."""
import asyncio
import random
import sqlite3

import pytest

from chartsrv import ind_alerts as ia
from chartsrv import market as market_mod
from chartsrv import replay
from chartsrv.bars import make_series, parse_spec
from chartsrv.market import Client
from chartsrv.models import Tick
from chartsrv.sessions import is_open
from chartsrv.store import Store

from test_replay import BIG, ES, ms, ticks
from test_review_fixes import build_market, history

NOW = ms(2026, 10, 14, 10, 30)   # a Wednesday morning
FIRST, HELD = 120, 300           # trades written, then trades the database can't take: 2 minutes, then 5 more


def _full(*a):
    raise sqlite3.OperationalError("database or disk is full")


def _unwritable(tmp_path, monkeypatch):
    """A market whose database stopped taking writes after FIRST trades: HELD more wait in its buffer, and the store
    loop's flush fails."""
    monkeypatch.setenv("CHART_WORKERS", "0")
    monkeypatch.setattr(market_mod, "now_ms", lambda: NOW)
    m, _ = build_market(tmp_path, monkeypatch)
    base = NOW // 60_000 * 60_000 - 10 * 60_000
    for i in range(FIRST + HELD):
        if i == FIRST:
            m.flush()
            m.store.write = _full
        m.on_tick(Tick("ES", base + i * 1000, 7000.0 + i % 4 * 0.25, 1, 1))
    with pytest.raises(sqlite3.OperationalError):
        m.flush()
    return m, base


def _stored(m) -> int:
    return m.store.db.execute("SELECT COUNT(*) FROM ticks WHERE symbol='ES'").fetchone()[0]


# --- the store flush ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("spec", ["1m", "5m", "1t", "5s"])
def test_a_chart_opens_with_every_trade_while_the_database_cant_be_written(tmp_path, monkeypatch, spec):
    async def go():
        m, _ = _unwritable(tmp_path, monkeypatch)
        m.store.set_setting = _full   # nor the charts used last
        chart = await history(m, Client(), 1, "ES", spec)
        return sum(b["v"] for b in chart["bars"])
    assert asyncio.run(go()) == FIRST + HELD


@pytest.mark.parametrize("spec", ["1m", "5m", "1t"])
def test_a_chart_cut_while_the_database_cant_be_written_counts_each_trade_once(tmp_path, monkeypatch, spec):
    """Space is freed while it loads: the store loop writes the waiting trades and their minutes meanwhile."""
    async def go():
        m, _ = _unwritable(tmp_path, monkeypatch)
        _, entry = m._entry("ES", parse_spec(spec), False)
        del m.store.write
        m.flush()
        await entry.ready.wait()
        return m, entry
    m, entry = asyncio.run(go())
    assert entry.error is None and _stored(m) == FIRST + HELD
    assert sum(b.v for b in entry.series.bars) == FIRST + HELD


def test_profiles_load_with_every_trade_while_the_database_cant_be_written(tmp_path, monkeypatch):
    async def go():
        m, _ = _unwritable(tmp_path, monkeypatch)
        return await m.book("ES")
    book = asyncio.run(go())
    assert sum(b[0] for b in book.eth[-1].bins.values()) == FIRST + HELD


def test_visible_range_profile_loads_while_the_database_cant_be_written(tmp_path, monkeypatch):
    """From what is stored (a 500 for every visible-range or composite profile while the disk was full)."""
    async def go():
        m, base = _unwritable(tmp_path, monkeypatch)
        return await m.range_profile("ES", base, NOW)
    assert asyncio.run(go())["total"] == FIRST


def test_the_add_ons_hello_works_while_the_database_cant_be_written(tmp_path, monkeypatch):
    """It asks from the last trade waiting to be written, not the last stored one: those would come twice."""
    m, base = _unwritable(tmp_path, monkeypatch)
    lines = m.gapfill.requests(NOW, live=True)
    es = next(x for x in lines if x.startswith("GAP|ES|")).split("|")
    assert int(es[2]) == base + (FIRST + HELD - 1) * 1000 and int(es[3]) == NOW
    assert m.gapfill.open("ES", NOW, least=1) == (base + (FIRST + HELD - 1) * 1000, NOW)


def test_study_and_indicator_alerts_load_while_the_database_cant_be_written(tmp_path, monkeypatch):
    async def go():
        m, _ = _unwritable(tmp_path, monkeypatch)
        m.store.add_alert("ES", "study", {"study": "vwap", "tolerance_ticks": 1, "rearm_ticks": 4}, True, "")
        m.store.add_alert("ES", "ind", ia.clean_params({"ind": "price"}, {"value": 1e6}, "1m", "close", "gt"), True, "")
        m.alerts_changed()
        del m.store.write   # space freed while they load
        m.flush()
        sw, iw = m.study_sources.watches["ES"], m.ind_alerts.watches[("ES", "1m", False)]
        await sw.ready.wait()
        await iw.ready.wait()
        return [sum(b.v for b in w.series.bars) for w in (sw, iw)]
    assert asyncio.run(go()) == [FIRST + HELD, FIRST + HELD]


def test_amt_and_the_minute_studies_load_while_the_database_cant_be_written(tmp_path, monkeypatch):
    async def go():
        m, _ = _unwritable(tmp_path, monkeypatch)
        e = await m.amt.entry("ES", 30)
        split, live = m.recent_minutes("ES")
        return e, split, live
    e, split, live = asyncio.run(go())
    assert e.error is None and e.ready.is_set() and live


# --- replay ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("spec", ["1v", "3v", "1k"])
def test_replay_counts_the_bars_a_trade_opens(tmp_path, monkeypatch, spec):
    """A 3-lot opens three 1v bars, a jump of a few ticks several Renko bricks: the bars before the start stay."""
    monkeypatch.setattr(replay, "BEFORE", 100)
    monkeypatch.setattr(replay, "AFTER", 50)
    store = Store(tmp_path / "t.db")
    store.write(ticks(ms(2026, 10, 13, 17), ms(2026, 10, 16, 16)), [])   # sizes 1-5, up to 2 ticks apart
    at = ms(2026, 10, 15, 8, 30)
    bars, next_at, _ = replay.build(store.path, "ES", ES, parse_spec(spec), at, ms(2026, 10, 19, 10), BIG)
    assert sum(b.t < at for b in bars) >= 100 and 50 <= sum(b.t >= at for b in bars) < 55 and next_at > bars[-1].t


def _sweeps(start, end):
    """A trade every 3 s; a third of them sweeps: several 1-lot fills in one ms at one price."""
    rnd, px, out, t = random.Random(7), 7000.0, [], start
    while t < end:
        if is_open(t):
            px = round(px + rnd.choice([-1, 0, 0, 1]) * 0.25, 2)
            out += [Tick("ES", t, px, 1, 1)] * (1 if rnd.random() < .7 else rnd.randint(2, 12))
        t += 3000
    return out


@pytest.mark.parametrize("spec", ["1t", "5t", "1v", "5v"])
def test_replay_plays_on_through_bars_that_open_in_one_millisecond(tmp_path, monkeypatch, spec):
    """Played like the browser does: each next stretch joined on the last bar shown (its t, o, c and v, the latest
    such bar), the bars shown are the chart's own."""
    monkeypatch.setattr(replay, "BEFORE", 100)
    monkeypatch.setattr(replay, "AFTER", 50)
    trades = _sweeps(ms(2026, 10, 14, 17), ms(2026, 10, 15, 10))
    store = Store(tmp_path / "t.db")
    store.write(trades, [])
    at, now, sp = ms(2026, 10, 15, 8, 30), ms(2026, 10, 19, 10), parse_spec(spec)
    chart = make_series(sp, 0.25, 2, 10**6, ES.rth)
    for tk in trades:
        chart.on_tick(tk)
    want = [(b.t, b.o, b.h, b.l, b.c, b.v) for b in chart.bars if b.t >= at][:600]
    bars, next_at, _ = replay.build(store.path, "ES", ES, sp, at, now, BIG)
    shown = [b for b in bars if b.t >= at]
    while len(shown) < len(want):
        last = shown[-1]
        more, next_at, _ = replay.build(store.path, "ES", ES, sp, next_at, now, BIG)
        j = next(k for k in range(len(more) - 1, -1, -1)
                 if (more[k].t, more[k].o, more[k].c, more[k].v) == (last.t, last.o, last.c, last.v))
        shown += more[j + 1:]
    assert [(b.t, b.o, b.h, b.l, b.c, b.v) for b in shown][:len(want)] == want


def test_replay_plays_on_past_a_sweep_longer_than_its_window(tmp_path, monkeypatch):
    """More bars than AFTER open in the start's own ms: the window takes them all and goes on from the next ms."""
    monkeypatch.setattr(replay, "BEFORE", 100)
    monkeypatch.setattr(replay, "AFTER", 50)
    at = ms(2026, 10, 15, 8, 30)
    trades = sorted(ticks(ms(2026, 10, 15, 8), ms(2026, 10, 15, 9), every=3000) + [Tick("ES", at, 7000.0, 1, 1)] * 80,
                    key=lambda tk: tk.ts)
    store = Store(tmp_path / "t.db")
    store.write(trades, [])
    bars, next_at, _ = replay.build(store.path, "ES", ES, parse_spec("1t"), at, ms(2026, 10, 19, 10), BIG)
    assert next_at > at and sum(b.t == at for b in bars) == 81 and bars[-1].t < next_at
