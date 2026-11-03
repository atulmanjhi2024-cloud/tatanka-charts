"""HTF EMA on seconds charts (1s-59s) in the default Smooth mode: the scaled length (21 x 3600/15 = 5040 on a 15s chart,
75600 on a 1s chart) is far more bars than the chart holds, so the server seeds the chart EMA up to the chart's first
bar, as it does for minute charts: from the chart's own seconds bars rebuilt from stored trades for the last
SECONDS_EXACT lengths, from the 1-minute bars before that and in minutes with no stored trades."""
import random
import shutil
from datetime import datetime

import pytest

from chartsrv import htfema, sessions, store
from chartsrv.amt_runner import epoch_ms
from chartsrv.bars import make_series, parse_spec
from chartsrv.models import Bar, Tick
from chartsrv.sessions import is_open, to_ct
from htfema_parity import js_values
from test_htfema import client  # noqa: F401

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
S = htfema.settings({})       # the defaults: 1 hour, 21, SmoothScaled
END = datetime(2026, 10, 16, 12, 7)   # a Friday, 12:07 CT
HELD = 3000                   # the bars a seconds chart loads (CHART_MAX_BARS)


def trades(days: int, seed: int, busy_gap=1.0, quiet_gap=4.0) -> list[Tick]:
    """ES trades over `days` of Globex hours to END: one every `busy_gap` seconds on average in regular hours, every
    `quiet_gap` at night, so many seconds have none (as on NinjaTrader, a seconds bar only where one printed)."""
    rng = random.Random(seed)
    t, stop, p, out = epoch_ms(END) - days * 86_400_000, epoch_ms(END), 27_200, []
    while t < stop:
        if not is_open(t):
            t = t // 60_000 * 60_000 + 60_000
            continue
        p += rng.choice((-1, 0, 0, 1))
        out.append(Tick("ES", t, p * 0.25, rng.randint(1, 5), rng.choice((1, -1))))
        ct = to_ct(t)
        rth = 510 <= ct.hour * 60 + ct.minute < 900
        t += int(rng.expovariate(1 / (busy_gap if rth else quiet_gap)) * 1000) + 1
    return out


def build(spec: str, ticks: list[Tick]) -> list[Bar]:
    series = make_series(parse_spec(spec), 0.25, 2, 10**7, studies=False)
    for tk in ticks:
        series.on_tick(tk)
    return list(series.bars)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    """Four days of trades and the 1-minute bars built from them, stored as the live feed stores them."""
    sessions.set_calendar({"days": []})   # as every test starts (conftest): no holidays
    ticks = trades(4, 11)
    path = tmp_path_factory.mktemp("htfsec") / "chart.db"
    st = store.Store(path)
    st.write(ticks, [("ES", b) for b in build("1m", ticks)])
    st.close()
    return path, ticks


_charts: dict[str, list[Bar]] = {}


def chart(spec: str, ticks: list[Tick]) -> list[Bar]:
    """The chart's bars from the module's trades, built once per size."""
    if spec not in _charts:
        _charts[spec] = build(spec, ticks)
    return _charts[spec]


def reference(bars: list[Bar], scaled: int) -> list:
    """NinjaTrader with all of this history loaded: EMA(Closes[0], scaled) from the first bar, plotted from
    CurrentBar scaled - 1 on."""
    e = htfema.NtEma(scaled)
    return [v if i >= scaled - 1 else None for i, v in enumerate(e.add(b.c) for b in bars)]


def payload(path, spec: str, start: int, read=True) -> dict:
    """The server's answer for a chart whose first bar opens at `start`, as /api/htfema builds it."""
    sp = parse_spec(spec)
    mins = list(store.iter_bars_1m(path, "ES", 0, 2**53))
    scaled = htfema.GetScaledLength(S, *htfema.chart_period_spec(sp))
    reader = (lambda lo, hi: store.iter_closes_1s(path, "ES", lo, hi)) if read is True else read
    return htfema.compute(S, mins, None, scaled, start, mins[-1].t + 60_000, chart_seconds=sp.size,
                          second_closes=reader)


def shown(path, ticks, spec: str, read=True):
    """The chart's last HELD bars as the browser draws them with the server's seed, and the full run's values there."""
    bars = chart(spec, ticks)
    cut = len(bars) - HELD
    d = payload(path, spec, bars[cut].t, read)
    got = js_values([{"t": b.t, "c": b.c} for b in bars[cut:]], d, parse_spec(spec).size * 1000)
    return d, got, reference(bars, d["scaled"])[cut:]


@needs_node
@pytest.mark.parametrize("spec", ["1s", "5s", "7s", "15s", "30s"])
def test_seconds_chart_draws_the_settled_line_from_its_first_bar(db, spec):
    """With the trades stored, the line is there from the chart's first bar and is NinjaTrader's over all of this
    history: it was missing on 1s-15s charts (2040 more live bars on a 15s chart) and started cold on 30s ones."""
    path, ticks = db
    d, got, want = shown(path, ticks, spec)
    assert d["smooth"] is not None and d["smooth"]["count"] >= d["scaled"]
    assert all(v is not None for v in want)            # the full run plots every one of these bars
    assert all(v is not None for v in got)
    assert max(abs(a - b) for a, b in zip(got, want)) < 0.01   # well under ES's 0.25 tick


@needs_node
@pytest.mark.parametrize("spec, within", [("15s", 0.5), ("30s", 0.1)])
def test_without_stored_trades_the_one_minute_bars_stand_in(db, spec, within):
    """History imported as 1-minute bars only (no trades): each minute counts as the chart bars that open in it, at
    most its trade count, all at its close. Close to NinjaTrader's line on 15s and 30s charts, not equal."""
    path, ticks = db
    _, got, want = shown(path, ticks, spec, read=lambda lo, hi: iter(()))
    assert all(v is not None for v in got)
    assert max(abs(a - b) for a, b in zip(got, want)) < within


def test_seed_from_minutes_and_from_trades():
    """A minute with no stored trades stands for the chart bars that open in it (its trade count where known, all at
    its close); a minute with trades gives the chart's own bars, each closing at its last second's trade."""
    open_ = epoch_ms(datetime(2026, 10, 13, 17, 0))    # a session open (Tuesday's)
    mins = [Bar(t=open_, o=10, h=10, l=10, c=10),            # no trade count: four 15s bars at 10
            Bar(t=open_ + 60_000, o=11, h=11, l=11, c=11, n=2),   # two trades: at most two bars, at 11
            Bar(t=open_ + 120_000, o=12, h=14, l=12, c=13, n=5)]  # its trades are stored
    secs = [(open_ + 123_000, 12.0), (open_ + 124_000, 12.5),     # 0:03, 0:04: the bar at 0:00 closes at 12.5
            (open_ + 140_000, 14.0), (open_ + 170_000, 13.0)]      # the 0:15 bar at 14, the 0:45 bar at 13
    d = htfema.compute(S, mins, None, 9, open_ + 180_000, open_ + 180_000, chart_seconds=15,
                       second_closes=lambda lo, hi: iter([x for x in secs if lo <= x[0] < hi]))
    c2 = 1 - 2 / 10
    v = 10.0                       # four bars at 10: the first sets it, the others keep it
    v = 11 + (v - 11) * c2 ** 2    # two at 11
    e = htfema.NtEma(9)
    e.values.append(v)
    for c in (12.5, 14.0, 13.0):
        e.add(c)
    assert d["smooth"]["count"] == 4 + 2 + 3
    assert d["smooth"]["seed"] == pytest.approx(e.values[-1], abs=1e-12)


def test_seven_second_bars_count_from_the_session_open():
    """Sizes that don't divide a minute: the bars that open in each minute, counted from the session open (as
    TimeSeries buckets them), 60 bars in 7 minutes; the 16:00 cut is sent for seconds charts too."""
    open_ = epoch_ms(datetime(2026, 10, 13, 17, 0))
    mins = [Bar(t=open_ + i * 60_000, o=1, h=1, l=1, c=1) for i in range(7)]
    d = htfema.compute(S, mins, None, 10_800, open_ + 7 * 60_000, open_ + 7 * 60_000, chart_seconds=7)
    assert d["smooth"]["count"] == 60
    assert epoch_ms(datetime(2026, 10, 14, 16, 0)) in d["ends"]


def test_one_request_reads_at_most_the_trade_limit(db, monkeypatch):
    """The window of trades is sized from the 1-minute bars' trade counts and stops at SECONDS_TRADES: a long higher
    timeframe (4h, Day) can't make one request read weeks of them. Further back, the 1-minute bars."""
    path, ticks = db
    bars = chart("15s", ticks)
    start = bars[-HELD].t
    asked = []

    def read(lo, hi):
        asked.append((lo, hi))
        return store.iter_closes_1s(path, "ES", lo, hi)
    payload(path, "15s", start, read)
    full_lo = asked[-1][0]
    monkeypatch.setattr(htfema, "SECONDS_TRADES", 20_000)
    d = payload(path, "15s", start, read)
    lo, hi = asked[-1]
    assert hi == start and full_lo < lo
    assert sum(m.n for m in store.iter_bars_1m(path, "ES", lo, start) if m.t + 60_000 <= start) <= 20_000
    assert d["smooth"]["count"] >= d["scaled"]
    t = 10**13   # 15s bars load as far back as 1m bars: 5040 x 15 s = 1260 x 1 min
    assert htfema.history_ms(S, None, 5040, t, t, chart_seconds=15) == htfema.history_ms(S, 1, 1260, t, t)


def test_store_closes_each_second_at_its_last_trade(tmp_path):
    """Seconds with a trade in [since, before), of this symbol only, each at its last trade."""
    path = tmp_path / "chart.db"
    st = store.Store(path)
    st.write([Tick("ES", t, p, 1, 1) for t, p in [(1000, 1.0), (1500, 2.0), (1999, 3.0), (2000, 4.0), (5000, 5.0),
                                                   (9000, 6.0)]] + [Tick("NQ", 1700, 9.0, 1, 1)], [])
    st.close()
    assert list(store.iter_closes_1s(path, "ES", 1000, 9000)) == [(1000, 3.0), (2000, 4.0), (5000, 5.0)]


def test_htfema_endpoint_seeds_seconds_charts(in_session, client):  # noqa: F811
    """The simulator's trades (its clock inside a trading day, set before the app starts): a seconds chart gets a
    seed and the session closes, as a minute chart does."""
    start = in_session - 2 * 3600_000
    for spec, scaled in (("1s", 75600), ("15s", 5040), ("30s", 2520)):
        d = client.get("/api/htfema", params={"symbol": "ES", "spec": spec, "start": start}).json()
        assert d["scaled"] == scaled and d["smooth"] is not None and d["smooth"]["count"] > 0, spec
        assert d["smooth"]["seed"] > 0 and d["ends"], spec
    d = client.get("/api/htfema", params={"symbol": "ES", "spec": "15s", "start": start, "Mode": "ExactHTF"}).json()
    assert d["smooth"] is None and d["htf"]                           # Exact and Live don't use the chart EMA
    d = client.get("/api/htfema", params={"symbol": "ES", "spec": "2000t", "start": start}).json()
    assert d["smooth"] is None and d["ends"] == []                     # tick charts: unchanged
