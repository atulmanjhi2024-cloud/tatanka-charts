"""AMT Toolkit v1.2.2: the current Session or Weekly profile (and its Developing POC/VAH/VAL) from its trades, the way
a tick volume profile builds it, not from 1-minute bars spread over their range. A minute whose trades don't add up to
its bar's volume (not stored, before the live trades began) is spread as before; Monthly and Yearly profiles and older
periods stay on 1-minute bars."""
import asyncio
import random
import time
from datetime import datetime

import pytest
from test_review_fixes import build_market

from chartsrv import amt, sessions
from chartsrv.amt import Profile
from chartsrv.amt_runner import AmtRunner, epoch_ms
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT
from amt_parity import synth

END = datetime(2026, 10, 16, 15, 30)   # a Friday afternoon


def trades_of(m):
    """A minute's trades adding up to its volume: two thirds at its close, the rest at its low."""
    third = m.v // 3
    return [(m.c, m.v - third), (m.l, third)] if third else [(m.c, m.v)]


def run(settings, days=10, skip=(), tick=.25, price=25000.0, root="NQ", trades=True):
    bars = synth(days, 5, END, tick, price)
    r = AmtRunner(root, tick, 5, amt.Settings(settings))
    start = r.period_start(bars[-1].t + 60_000)
    give, trades = trades, []
    for m in bars:
        if give and start is not None and m.t >= start and m.t not in skip:
            for px, v in trades_of(m):
                r.add_trade(m.t + 30_000, px, v)
                trades.append((m.t, px, v))
        r.add_minute(m)
    r.commit_ready()
    r.preview()
    return r, bars, trades, start


def naive(ms):
    return datetime.fromtimestamp(ms / 1000, CT).replace(tzinfo=None)


def test_the_weeks_profile_is_its_trades():
    """Weekly (the default): this week's profile has its trades' volume at each row, and its Developing POC is theirs;
    last week's is its minutes', as before."""
    r, bars, trades, start = run({})
    assert start == epoch_ms(datetime(2026, 10, 11, 17))                       # Sunday's 17:00 open
    main, forming = r.engine.main, r.chart.keys[-1]
    assert main.Rows == Profile.from_prices(main.Step, [(px, v) for t, px, v in trades if t < forming]).Rows
    f, _ = r.preview()
    dev = next(x.Price for x in f.Lines if x.Name == "Developing POC")
    assert dev == Profile.from_prices(main.Step, [(px, v) for _, px, v in trades]).Stats(.7).Poc
    plain, *_ = run({}, trades=False)
    assert r.engine.profiles[-1].Profile.Rows == plain.engine.profiles[-1].Profile.Rows
    assert main.Rows != plain.engine.main.Rows


def test_a_minute_without_all_its_trades_is_its_bar():
    """A minute with none of its trades, half of them, or twice them (counted twice) is spread over its range."""
    r, bars, trades, _ = run({})
    m = bars[-100]
    at = naive(m.t)
    assert r._minute_rows(at, m.v) == sorted(dict(trades_of(m)).items())
    r.minute_trades[m.t] = {m.c: m.v * .5}
    assert r._minute_rows(at, m.v) is None
    r.minute_trades[m.t] = {**dict(trades_of(m)), m.h: m.v}
    assert r._minute_rows(at, m.v) is None
    del r.minute_trades[m.t]
    assert r._minute_rows(at, m.v) is None
    one, *_ = run({}, skip={m.t})                                               # that minute's bar, the rest trades
    main = one.engine.main
    want = Profile.from_prices(main.Step, [(px, v) for t, px, v in trades if t < one.chart.keys[-1] and t != m.t])
    want.Add(amt.Bar(at, at, m.o, m.h, m.l, m.c, m.v), True)
    assert {k: round(v, 6) for k, v in main.Rows.items()} == {k: round(v, 6) for k, v in want.Rows.items()}


def test_session_mode_is_todays_session_from_its_trades():
    r, bars, trades, start = run({"mvp_tf_mode": "Session"})
    assert start == epoch_ms(datetime(2026, 10, 16, 8, 30))
    main = r.engine.main
    inside = [(px, v) for t, px, v in trades if t < epoch_ms(datetime(2026, 10, 16, 15)) and t < r.chart.keys[-1]]
    assert main.Rows == Profile.from_prices(main.Step, inside).Rows


@pytest.mark.parametrize("mode", ["Monthly", "Yearly"])
def test_monthly_and_yearly_stay_on_minutes(mode):
    r, *_ = run({"mvp_tf_mode": mode})
    plain, *_ = run({"mvp_tf_mode": mode}, trades=False)
    assert r.period_start(time.time() * 1000) is None and not r.minute_trades
    assert r.engine.main.Rows == plain.engine.main.Rows


# --- the hub: stored trades at load, live ones after --------------------------------------------------------
MIN = 60_000


def ct(d, h, mi=0):
    return int(datetime(2026, 10, d, h, mi, tzinfo=CT).timestamp() * 1000)


def stored(sym, tick, px, start, end, seed=3):
    """Minutes and their trades (four a minute, adding up to its volume)."""
    rnd = random.Random(seed)
    tk, bars = [], []
    t = start
    while t < end:
        if datetime.fromtimestamp(t / 1000, CT).hour != 16 and sessions.is_open(t):
            o = px
            c = round((px + rnd.choice((-6, -3, -1, 0, 1, 3, 6)) * tick) / tick) * tick
            h = max(o, c) + rnd.choice((0, 1, 2)) * tick
            lo = min(o, c) - rnd.choice((0, 1, 2)) * tick
            sizes = [rnd.randint(1, 30) for _ in range(4)]
            bars.append((sym, Bar(t=t, o=o, h=round(h, 6), l=round(lo, 6), c=round(c, 6), v=sum(sizes))))
            for k, (p, sz) in enumerate(zip((o, h, lo, c), sizes)):
                tk.append(Tick(sym, t + 5_000 + k * 15_000, round(p, 6), sz, 1 if k % 2 else -1))
            px = c
        t += MIN
    return tk, bars


def test_the_hub_loads_the_weeks_trades_and_takes_live_ones(tmp_path, monkeypatch):
    now = {"t": ct(15, 11, 2)}   # Thursday 11:02 CT
    monkeypatch.setattr(time, "time", lambda: now["t"] / 1000)
    monkeypatch.setenv("CHART_WORKERS", "0")
    sessions.set_calendar({"days": []})

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        tk, bars = stored("ES", .25, 6800.0, ct(8, 17), now["t"])
        m.store.write(tk, bars)
        e = await m.amt.entry("ES", 5)
        r = e.runner
        week = ct(11, 17)
        assert min(r.minute_trades) == week                                      # this week's only
        main = r.engine.main
        want = Profile.from_prices(main.Step, [(x.price, x.size) for x in tk if week <= x.ts < r.chart.keys[-1]])
        assert main.Rows == want.Rows
        t = now["t"] + 5_000
        m.on_tick(Tick("ES", t, 6800.0, 7, 1))                                   # a live trade
        assert r.minute_trades[t // MIN * MIN] == {6800.0: 7}
    asyncio.run(go())
