"""Bar replay: the bars around a past moment match the chart's own bars, the window stops at a complete bar, and
asking again at next_at continues where the last answer stopped (the browser joins the two on the last bar)."""
import random
from datetime import datetime

import pytest

from chartsrv import replay
from chartsrv.bars import parse_spec, make_series
from chartsrv.config import Instrument
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT, is_open
from chartsrv.store import Store

ES = Instrument("ES", "E-mini", 0.25, 50, "ES", rth=("08:30", "15:15"))
BIG = 2 ** 62


def ms(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=CT).timestamp() * 1000)


def minutes(start, end, seed=1):
    rnd, out, px, t = random.Random(seed), [], 7000.0, start
    while t < end:
        if is_open(t):
            o = px
            px = round(px + rnd.randint(-4, 4) * 0.25, 2)
            out.append(Bar(t=t, o=o, h=max(o, px) + 0.25, l=min(o, px) - 0.25, c=px, v=rnd.randint(1, 50)))
        t += 60_000
    return out


def ticks(start, end, every=20_000, seed=2):
    rnd, out, px, t = random.Random(seed), [], 7000.0, start
    while t < end:
        if is_open(t):
            px = round(px + rnd.randint(-2, 2) * 0.25, 2)
            out.append(Tick("ES", t, px, rnd.randint(1, 5), rnd.choice([1, -1])))
        t += every
    return out


def full(spec, src, on):
    s = make_series(parse_spec(spec), 0.25, 2, 10**6, ES.rth)
    for x in src:
        getattr(s, on)(x)
    return list(s.bars)


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path / "t.db")
    store.insert_bars_if_missing("ES", minutes(ms(2026, 10, 7, 17), ms(2026, 10, 15, 12)))
    store.write(ticks(ms(2026, 10, 13, 17), ms(2026, 10, 19, 10)), [])   # Tue evening to Monday morning
    return store.path


def test_time_bars_match_the_chart_and_continue_at_next_at(db, monkeypatch):
    monkeypatch.setattr(replay, "BEFORE", 300)
    monkeypatch.setattr(replay, "AFTER", 40)
    spec, at, now = parse_spec("5m"), ms(2026, 10, 13, 10, 2), ms(2026, 10, 15, 12)
    bars, next_at, _ = replay.build(db, "ES", ES, spec, at, now, BIG)
    chart = {b.t: b for b in full("5m", minutes(ms(2026, 10, 7, 17), now), "on_bar")}
    assert bars[0].t < ms(2026, 10, 12, 17) and 300 <= sum(b.t < at for b in bars) <= 332   # 10% margin for short bars and sum(b.t >= at for b in bars) == 40
    for b in bars:
        c = chart[b.t]
        assert (b.o, b.h, b.l, b.c, b.v) == (c.o, c.h, c.l, c.c, c.v)
        if b.t >= ms(2026, 10, 12, 17):                      # VWAP from the first whole session in the window
            assert b.st == c.st
    assert next_at == bars[-1].t + 300_000                  # the bar it stopped in is left out
    more, _, _ = replay.build(db, "ES", ES, spec, next_at, now, BIG)
    i = next(k for k, b in enumerate(more) if b.t == bars[-1].t)
    assert (more[i].o, more[i].c, more[i].v) == (bars[-1].o, bars[-1].c, bars[-1].v) and more[i + 1].t == next_at


def test_reaching_the_present_drops_the_forming_bar(db):
    now = ms(2026, 10, 15, 11, 58)                           # mid 5m bar
    bars, next_at, _ = replay.build(db, "ES", ES, parse_spec("5m"), ms(2026, 10, 15, 9), now, BIG)
    assert next_at is None and bars[-1].t == ms(2026, 10, 15, 11, 50)


def test_tick_bars_run_to_the_session_end_and_skip_the_weekend(db):
    spec = parse_spec("50t")
    fri = ms(2026, 10, 16, 9)
    bars, next_at, _ = replay.build(db, "ES", ES, spec, fri, ms(2026, 10, 19, 10), BIG)
    src = [tk for tk in ticks(ms(2026, 10, 13, 17), ms(2026, 10, 19, 10)) if ms(2026, 10, 14, 17) <= tk.ts < ms(2026, 10, 16, 16)]
    chart = full("50t", src, "on_tick")
    assert [(b.t, b.o, b.c, b.v) for b in bars] == [(b.t, b.o, b.c, b.v) for b in chart]   # Wed evening and Thu-Fri sessions
    assert next_at == ms(2026, 10, 18, 17)                   # Friday's session ends: next is Sunday 17:00
    sun, nxt, _ = replay.build(db, "ES", ES, spec, next_at, ms(2026, 10, 19, 10), BIG)
    assert sun[0].t == ms(2026, 10, 15, 17) and sun[-1].t >= ms(2026, 10, 18, 17) and nxt is None
    assert any(b.t == bars[-1].t and b.c == bars[-1].c for b in sun)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    store = Store(tmp_path / "chart.db")
    store.insert_bars_if_missing("ES", minutes(ms(2026, 10, 12, 17), ms(2026, 10, 14, 12)))
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as c:
        yield c


def test_api(client):
    r = client.get("/api/replay", params={"symbol": "es", "spec": "15m", "at": ms(2026, 10, 13, 10)})
    d = r.json()
    assert r.status_code == 200 and d["kind"] == "time" and d["symbol"] == "ES" and d["next_at"] is None
    assert d["bars"][0]["t"] >= ms(2026, 10, 12, 17) and "st" in d["bars"][-1]
    for params, code in [({"symbol": "ZZ", "spec": "5m", "at": 1}, 404), ({"symbol": "ES", "spec": "5x", "at": 1}, 400),
                         ({"symbol": "ES", "spec": "5m", "at": 4_000_000_000_000}, 400)]:
        assert client.get("/api/replay", params=params).status_code == code, params
