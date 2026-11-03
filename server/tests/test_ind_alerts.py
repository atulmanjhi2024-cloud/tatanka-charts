"""Indicator alerts: NinjaTrader 8 formulas, crossings on bar close or intrabar once a bar, and the API."""
import pytest

from chartsrv import ind_alerts as ia
from chartsrv.alerts import Alert
from chartsrv.bars import make_series, parse_spec
from chartsrv.models import Tick
from test_review_fixes import build_market

T0 = 1_760_000_000_000 - 1_760_000_000_000 % 60_000


def test_formulas_match_ninjatrader():
    e = ia.Ema(3)
    assert [round(e.step(x), 6) for x in (10, 11, 12, 13)] == [10, 10.5, 11.25, 12.125]
    s = ia.Sma(3)
    assert [s.step(x) for x in (3, 6, 9, 12)] == [3, 4.5, 6, 9]
    assert s.step(100, False) == (9 + 12 + 100) / 3 and s.step(15) == 12   # peeking does not move it
    r = ia.Rsi(3, 3)
    vals = [r.step(x) for x in (10, 11, 12, 11, 13)]
    assert vals[0] == (None, None) and vals[1] == (None, None)
    assert vals[2][0] == 100.0                     # the third bar: SMA seed, no down closes yet
    assert 0 < vals[3][0] < 100 and vals[4][0] > vals[3][0]
    m = ia.Macd(2, 4, 2)
    assert m.step(10) == (0.0, 0.0, 0.0)


def _alert(params, repeat=True, i=1):
    return Alert(id=i, symbol="ES", kind="ind", params=params, enabled=True, repeat=repeat, note="", created=0, last_fired=None)


def _feed(ind, alert, prices, per_min=4):
    out = []
    for i, p in enumerate(prices):
        tk = Tick("ES", T0 + i * (60_000 // per_min), p, 1, 1)
        ind.on_tick(tk)
        msg = ind.check(alert, tk)
        if msg:
            out.append((i, msg))
    return out


def _engine(tmp_path, monkeypatch, spec="1m"):
    m, _ = build_market(tmp_path, monkeypatch)
    ind = ia.IndicatorAlerts(m)
    w = ia._Watch(make_series(parse_spec(spec), 0.25, 2, studies=False))
    w.ready.set()
    w.version = 1
    ind.watches[("ES", spec, False)] = w
    return ind


def test_price_crossing_up_an_ema_on_bar_close(tmp_path, monkeypatch):
    ind = _engine(tmp_path, monkeypatch)
    p = ia.clean_params({"ind": "price"}, {"ind": "ema", "len": 3}, "1m", "close", "above")
    a = _alert(p)
    falling = [100 - k * 0.25 for k in range(24)]   # 6 one-minute bars down
    rise = [99.0, 99.5, 100.5, 101.0]                # a minute that closes far above the EMA
    got = _feed(ind, a, falling + rise + [101.0] * 4)
    assert len(got) == 1 and got[0][0] == len(falling) + len(rise)   # on the first trade after that bar closed
    assert "price" in got[0][1] and "crossed above EMA(3)" in got[0][1]


def test_rsi_above_a_value_intrabar_once_a_bar(tmp_path, monkeypatch):
    ind = _engine(tmp_path, monkeypatch)
    p = ia.clean_params({"ind": "rsi", "len": 3}, {"value": 70}, "1m", "bar", "gt")
    a = _alert(p)
    up = [100 + k * 0.5 for k in range(40)]          # ten rising minutes: RSI 100 throughout
    got = _feed(ind, a, up)
    bars = {i // 4 for i, _ in got}
    assert len(got) == len(bars) and len(got) >= 6    # once in each bar where it holds, not every trade
    assert "RSI(3,3)" in got[0][1] and "above 70" in got[0][1]


def test_params_are_checked():
    ok = ia.clean_params({"ind": "macd"}, {"ind": "macd_avg"}, "15m", "close", "cross")
    assert ok["a"] == {"ind": "macd", "fast": 12, "slow": 26, "smooth": 9}
    assert ia.describe(ok) == "MACD(12,26,9) crossing MACD(12,26,9) Signal on 15m bar close"   # as the new series name it
    for bad in [({"ind": "ema", "len": 0}, {"ind": "price"}, "5m"), ({"value": 3}, {"ind": "price"}, "5m"),
                ({"ind": "ema", "len": 9}, {"ind": "price"}, "2000t"), ({"ind": "ema", "len": 9}, {"ind": "price"}, "1W"),
                ({"ind": "macd", "fast": 30, "slow": 26}, {"value": 0}, "5m"), ({"ind": "foo"}, {"value": 0}, "5m")]:
        with pytest.raises(ValueError):
            ia.clean_params(*bad, "close", "cross")


def test_api_creates_indicator_alerts(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    with TestClient(app) as c:
        r = c.post("/api/alerts", json={"symbol": "ES", "kind": "ind", "a": {"ind": "ema", "len": 9}, "b": {"ind": "ema", "len": 21},
                                         "spec": "5m", "on": "close", "direction": "above", "repeat": True})
        assert r.status_code == 201, r.text
        assert r.json()["params"]["spec"] == "5m" and r.json()["repeat"] is True
        assert c.post("/api/alerts", json={"symbol": "ES", "kind": "ind", "a": {"ind": "ema", "len": 9}, "b": {"value": 1},
                                           "spec": "5m", "direction": "enter"}).status_code == 400


def test_turned_off_alert_starts_afresh(tmp_path, monkeypatch):
    """A crossing that happened while the alert was off (or fired and done) is not sent when it is turned on."""
    import asyncio
    ind = _engine(tmp_path, monkeypatch)
    a = _alert(ia.clean_params({"ind": "price"}, {"ind": "ema", "len": 3}, "1m", "close", "above"))
    falling = [100 - k * 0.25 for k in range(24)]
    assert _feed(ind, a, falling) == [] and a.id in ind.states
    a.enabled = False
    ind.m.alerts.alerts = {a.id: a}
    ind.ensure()
    assert a.id not in ind.states and not ind.watches
    # on again: the history is loaded afresh (it crossed while off); the first look never fires
    w = ia._Watch(make_series(parse_spec("1m"), 0.25, 2, studies=False))
    asyncio.run(ind._load(("ES", "1m", False), w, 0, T0))
    w2 = ia._Watch(make_series(parse_spec("1m"), 0.25, 2, studies=False))
    asyncio.run(ind._load(("ES", "1m", False), w2, 0, T0))
    assert w.version and w2.version and w.version != w2.version   # a rebuilt watch never matches an old state
    for i, price in enumerate(falling + [99.0, 99.5, 100.5, 101.0]):
        w2.series.on_tick(Tick("ES", T0 + i * 15_000, price, 1, 1))
    ind.watches = {("ES", "1m", False): w2}
    a.enabled = True
    later = Tick("ES", T0 + 29 * 15_000, 101.0, 1, 1)
    w2.series.on_tick(later)
    assert ind.check(a, later) is None
    assert [ind.check(a, Tick("ES", T0 + k * 15_000, 101.0, 1, 1)) for k in range(30, 34)] == [None] * 4


def test_intrabar_crossing_is_against_the_last_close(tmp_path, monkeypatch):
    """As TradingView's crossover: a dip under the line and back inside one bar is not a crossing up."""
    ind = _engine(tmp_path, monkeypatch)
    a = _alert(ia.clean_params({"ind": "price"}, {"ind": "ema", "len": 3}, "1m", "bar", "above"))
    flat_above = [105.0] * 12                       # three minutes at 105: price on the EMA
    rising = [106.0, 107.0, 108.0, 109.0]           # closes above it: the crossing up, intrabar
    dip = [100.0, 109.5, 110.0, 110.0]              # under the line and back, all in one minute
    got = _feed(ind, a, flat_above + rising + dip)
    assert [i for i, _ in got] == [12]


def test_rth_and_value_formatting():
    p = ia.clean_params({"ind": "ema", "len": 9}, {"value": 25000.75}, "5m", "close", "cross", True)
    assert p["rth"] is True and ia.describe(p) == "EMA(9) crossing 25000.75 on 5m RTH bar close"
    assert ia._key(_alert(p)) == ("ES", "5m", True)
    assert "rth" not in ia.clean_params({"ind": "ema", "len": 9}, {"value": 1}, "5m", "close", "cross")
    with pytest.raises(ValueError):
        ia.clean_params({"ind": "macd", "fast": 12, "slow": 501}, {"value": 0}, "5m", "close", "cross")


def test_any_indicator_line_alerts(tmp_path, monkeypatch):
    """The indicators beyond EMA, SMA, RSI and MACD: {ind, line, settings}, checked, named, and stepped on whole bars."""
    p = ia.clean_params({"ind": "price"}, {"ind": "boll", "line": "upper", "len": 4, "dev": 1}, "1m", "close", "above")
    assert p["b"] == {"ind": "boll", "line": "upper", "len": 4, "dev": 1.0}
    assert ia.describe(p) == "price crossing up Bollinger(4,1) Upper on 1m bar close"
    assert ia.line_name({"ind": "adx", "line": "dip", "len": 14}) == "ADX(14) +DI"
    assert ia.line_name({"ind": "ema", "line": "v", "len": 21}) == "EMA(21)"
    assert ia.line_name({"ind": "pivots", "line": "r1", "range": "Weekly"}) == "Pivots(Weekly) R1"
    k_d = ia.clean_params({"ind": "stoch", "line": "k"}, {"ind": "stoch", "line": "d"}, "5m", "bar", "cross")
    assert k_d["a"] == {"ind": "stoch", "line": "k", "k": 14, "smooth": 3, "d": 7}   # NinjaTrader's defaults filled in
    for bad in [{"ind": "boll", "line": "top"}, {"ind": "nope", "line": "v"}, {"ind": "boll", "line": "upper", "dev": 0},
                {"ind": "pivots", "line": "pp", "range": "Hourly"}, {"ind": "macd", "line": "macd", "fast": 30, "slow": 20}]:
        with pytest.raises(ValueError):
            ia.clean_params({"ind": "price"}, bad, "1m", "close", "cross")
    # price through a 4-bar Bollinger upper band (1 sd): flat, then a minute that closes far above it
    ind = _engine(tmp_path, monkeypatch)
    a = _alert(p)
    got = _feed(ind, a, [100.0, 100.25] * 12 + [100.0, 101.0, 102.0, 103.0] + [103.0] * 4)
    assert len(got) == 1 and "Bollinger(4,1) Upper" in got[0][1] and got[0][0] == 28
    # ATR needs the bars' highs and lows: a value alert on it fires when ranges widen
    (tmp_path / "b").mkdir()
    ind2 = _engine(tmp_path / "b", monkeypatch)
    atr = _alert(ia.clean_params({"ind": "atr", "line": "v", "len": 3}, {"value": 2}, "1m", "close", "gt"), i=2)
    quiet = [100.0, 100.25, 100.0, 100.25] * 4
    wide = [100.0, 104.0, 98.0, 101.0] * 3
    got = _feed(ind2, atr, quiet + wide + [101.0] * 4)
    assert got and got[0][0] >= len(quiet) and "ATR(3) " in got[0][1]


def _ct(d, h, mi=0):
    """Epoch ms of a CT time in October 2025 (the 13th a Monday)."""
    from datetime import datetime
    from chartsrv import sessions
    return int(datetime(2025, 10, d, h, mi, tzinfo=sessions.CT).timestamp() * 1000)


def _rth_engine(tmp_path, monkeypatch, spec):
    """An RTH-only watch, as ensure() builds one for an alert made on an RTH chart."""
    m, _ = build_market(tmp_path, monkeypatch)
    inst = m.instruments["ES"]
    ind = ia.IndicatorAlerts(m)
    w = ia._Watch(make_series(parse_spec(spec), inst.tick_size, inst.decimals, 3000, inst.rth, studies=False,
                              rth_only=True, group=inst.calendar))
    w.ready.set()
    w.version = 1
    ind.watches[("ES", spec, True)] = w
    return ind


def _feed_at(ind, alert, trades):
    out = []
    for ts, price in trades:
        tk = Tick("ES", ts, price, 1, 1)
        ind.on_tick(tk)
        if msg := ind.check(alert, tk):
            out.append((ts, msg))
    return out


def test_rth_intrabar_alert_ignores_overnight_trades(tmp_path, monkeypatch):
    """An alert made on an RTH chart checks intrabar only on trades that chart has: none overnight."""
    ind = _rth_engine(tmp_path, monkeypatch, "5m")
    a = _alert(ia.clean_params({"ind": "price"}, {"ind": "ema", "line": "v", "len": 5}, "5m", "bar", "above", True))
    day = [(_ct(13, 8, 30) + k * 60_000, 7500 - k * 0.25) for k in range(390)]   # Monday's RTH: falls to the close
    night = [(_ct(13, 17) + k * 60_000, 7410 + k) for k in range(40)]         # far above it overnight
    assert _feed_at(ind, a, day + night) == []
    got = _feed_at(ind, a, [(_ct(14, 8, 30), 7450.0)])   # Tuesday's RTH open, above it: the crossing the chart shows
    assert len(got) == 1 and "crossed above EMA(5)" in got[0][1]


def test_restarted_alert_starts_afresh_after_it_stopped_itself(tmp_path, monkeypatch):
    """An alert that switched itself off (Only once, or expired) and is restarted does not send a crossing from
    while it was off. Each on its own watch, so neither one's switching off clears the other's."""
    import asyncio
    p = ia.clean_params({"ind": "price"}, {"ind": "ema", "len": 3}, "1m", "close", "above")
    for how in ("once", "expired"):
        (tmp_path / how).mkdir()
        m, _ = build_market(tmp_path / how, monkeypatch)
        w = ia._Watch(make_series(parse_spec("1m"), 0.25, 2, studies=False))
        w.ready.set()
        w.version = 1
        m.ind_alerts.watches[("ES", "1m", False)] = w
        if how == "once":
            a = m.store.add_alert("ES", "ind", p, False, "")
        else:
            a = m.store.add_alert("ES", "ind", {**p, "expires": T0 + 15 * 60_000}, True, "")
        m.store.add_alert("ES", "ind", ia.clean_params({"ind": "price"}, {"value": 1e6}, "1m", "close", "gt"), True, "")
        m.alerts_changed()
        n = 0
        fired = lambda: [e["alert_id"] for e in m.store.list_events(100)]   # noqa: E731

        def feed(prices):
            nonlocal n
            for px in prices:
                tk = Tick("ES", T0 + n * 15_000, px, 1, 1)
                m.ind_alerts.on_tick(tk)
                m.alerts.on_tick(tk)
                n += 1

        def crossing_while_off():   # falls under the EMA, closes far above it, then stays
            feed([100 - k * 0.5 for k in range(24)] + [95.0, 97.0, 99.0, 101.0] + [101.0] * 40)

        async def go():
            feed([100 - k * 0.25 for k in range(24)] + [99.0, 99.5, 100.5, 101.0] + [101.0] * 4)
            assert fired().count(a) == 1
            crossing_while_off()
            assert not m.alerts.alerts[a].enabled   # fired and done, or expired meanwhile
            crossing_while_off()
            before = len(fired())
            m.store.update_alert(a, enabled=True, params=p)   # Restart (an expired one no longer expires)
            m.alerts_changed()
            feed([101.0] * 4)
            assert len(fired()) == before, how
        asyncio.run(go())


def test_intrabar_alert_fires_once_a_bar_across_a_restart(tmp_path, monkeypatch):
    """A restart, or a gap fill that reloads the series, inside the bar an intrabar alert fired in: not again in it."""
    ind = _engine(tmp_path, monkeypatch, "1h")
    a = _alert(ia.clean_params({"ind": "price"}, {"ind": "sma", "line": "v", "len": 3}, "1h", "bar", "above"))
    trades = [(_ct(13, 4) + k * 60_000, 7500 - k * 0.1) for k in range(6 * 60)]   # six falling hours
    trades += [(_ct(13, 10) + k * 60_000, 7464 + k) for k in range(15)]           # 10:00 bar: crosses up
    got = _feed_at(ind, a, trades)
    assert len(got) == 1
    a.last_fired = got[0][0]   # as AlertEngine stores it
    w = ind.watches[("ES", "1h", False)]
    w2 = ia._Watch(make_series(parse_spec("1h"), 0.25, 2, studies=False))   # loaded again from the same trades
    for ts, price in trades:
        w2.series.on_tick(Tick("ES", ts, price, 1, 1))
    w2.ready.set()
    w2.version = w.version + 1
    ind.watches[("ES", "1h", False)] = w2
    assert _feed_at(ind, a, [(_ct(13, 10, k), 7480.0) for k in range(30, 40)]) == []   # still above, same bar


def test_daily_alert_has_the_older_days_the_daily_chart_has(tmp_path, monkeypatch):
    """A future with under a year stored: a daily alert's bars take Yahoo's older days, as the daily chart's do, so a
    long average is the one the chart shows."""
    import asyncio
    from datetime import date, timedelta
    from chartsrv import market as market_mod
    from chartsrv.models import Bar
    from chartsrv.sessions import day_open
    from test_review_fixes import Client, history
    m, _ = build_market(tmp_path, monkeypatch)
    today = date(2026, 10, 20)
    monkeypatch.setattr(market_mod, "now_ms", lambda: day_open(today) + 20 * 3_600_000)
    first = day_open(today - timedelta(days=1))   # two days stored
    m.store.write([], [("ES", Bar(t=first + 22 * 3_600_000, o=6900, h=6905, l=6898, c=6903, v=10, n=4)),
                       ("ES", Bar(t=day_open(today) + 3_600_000, o=6903, h=6910, l=6901, c=6908, v=12, n=5))])
    days = [today - timedelta(days=i) for i in range(80, 0, -1) if (today - timedelta(days=i)).weekday() < 5]
    rows = [{"t": day_open(d), "o": 6800 + i, "h": 6810 + i, "l": 6790 + i, "c": 6805 + i, "v": 1000} for i, d in enumerate(days)]

    async def yahoo(ticker, spec):
        return [dict(r) for r in rows], {}
    monkeypatch.setattr(m.ext, "_yahoo", yahoo)
    sma50 = ia.clean_params({"ind": "price"}, {"ind": "sma", "line": "v", "len": 50}, "1D", "close", "cross")
    m.store.add_alert("ES", "ind", sma50, True, "")

    async def go():
        m.alerts_changed()
        w = m.ind_alerts.watches[("ES", "1D", False)]
        await w.ready.wait()
        chart = await history(m, Client(), 1, "ES", "1D")
        return [(b.t, b.o, b.c) for b in w.series.bars], [(b["t"], b["o"], b["c"]) for b in chart["bars"]]
    alert_bars, chart_bars = asyncio.run(go())
    assert len(alert_bars) == len(rows) + 1 and alert_bars == chart_bars


def test_rth_bar_close_is_checked_when_regular_hours_end(tmp_path, monkeypatch):
    """An RTH chart's last bar of the day closes with its regular hours: checked on the first trade after them,
    not at the next RTH open (TradingView's Once Per Bar Close)."""
    ind = _rth_engine(tmp_path, monkeypatch, "1D")
    a = _alert(ia.clean_params({"ind": "price"}, {"ind": "sma", "line": "v", "len": 3}, "1D", "close", "above", True))
    trades = []
    for d, px in [(6, 7500.0), (7, 7480.0), (8, 7460.0), (9, 7440.0), (10, 7500.0)]:   # Friday the 10th closes above
        trades += [(_ct(d, 8, 30) + k * 300_000, px) for k in range(78)]   # its RTH
        trades += [(_ct(d, 15, 30), px), (_ct(d, 17, 5), px)]             # the trades after it
    got = _feed_at(ind, a, trades)
    assert [ts for ts, _ in got] == [_ct(10, 15, 30)] and "crossed above SMA(3)" in got[0][1]
    ind.watches[("ES", "1D", True)].version += 1   # reloaded after the close (a restart, a gap fill): not sent again
    assert _feed_at(ind, a, [(_ct(10, 17, 10), 7500.0), (_ct(10, 17, 15), 7500.0)]) == []
    assert _feed_at(ind, a, [(_ct(12, 17), 7500.0), (_ct(13, 8, 30), 7500.0), (_ct(13, 8, 35), 7500.0)]) == []
    # intraday: the day's last 5-minute bar, 14:55, crosses up
    (tmp_path / "b").mkdir()
    ind = _rth_engine(tmp_path / "b", monkeypatch, "5m")
    a = _alert(ia.clean_params({"ind": "price"}, {"ind": "ema", "line": "v", "len": 3}, "5m", "close", "above", True))
    day = [(_ct(13, 8, 30) + k * 60_000, 7500 - k * 0.25) for k in range(385)] + [(_ct(13, 14, 59), 7500.0)]
    assert _feed_at(ind, a, day) == []
    got = _feed_at(ind, a, [(_ct(13, 15, 0) + 30_000, 7500.0), (_ct(14, 8, 30), 7500.0)])
    assert [ts for ts, _ in got] == [_ct(13, 15, 0) + 30_000]


def test_rth_bar_of_several_days_is_checked_when_its_last_days_regular_hours_end(tmp_path, monkeypatch):
    """A 2D RTH bar closes with the regular hours of its second trading day (as the chart's countdown has it): checked
    on the first trade after them, also when that day is a Friday, not at the next session's open (nor before them)."""
    ind = _rth_engine(tmp_path, monkeypatch, "2D")
    a = _alert(ia.clean_params({"ind": "price"}, {"value": 0}, "2D", "close", "gt", True))
    trades = []
    for d in (6, 7, 8, 9, 10, 12, 13, 14, 15, 16):   # Monday the 6th to Thursday the 16th, Sunday the 12th's open
        if d != 12:
            trades += [_ct(d, 7)] + [_ct(d, 8, 30) + k * 1_800_000 for k in range(13)] + [_ct(d, 15, 30)]
        trades.append(_ct(d, 17, 5))
    got = _feed_at(ind, a, [(ts, 7500.0) for ts in trades])
    # the groups of two trading days: (Fri 3, Mon 6), (Tue 7, Wed 8), (Thu 9, Fri 10), (Mon 13, Tue 14), (Wed 15, Thu 16)
    assert [ts for ts, _ in got] == [_ct(6, 15, 30), _ct(8, 15, 30), _ct(10, 15, 30), _ct(14, 15, 30), _ct(16, 15, 30)]


def test_globex_bar_close_is_checked_when_the_market_shuts(tmp_path, monkeypatch):
    """A Globex (ETH) chart's last bar of a session closes at 16:00 CT, and no trade comes until the next open (17:00,
    or Sunday's after a Friday): the watchdog checks bar-close alerts once the market has been shut a minute, not on
    the next session's first trade (a daily close 49 hours late)."""
    import asyncio
    from chartsrv import market as market_mod
    m, _ = build_market(tmp_path, monkeypatch)
    inst = m.instruments["ES"]
    for spec in ("1D", "1h"):
        w = m.ind_alerts.watches[("ES", spec, False)] = ia._Watch(make_series(
            parse_spec(spec), inst.tick_size, inst.decimals, 3000, inst.rth, studies=False, group=inst.calendar))
        w.ready.set()
        w.version = 1
    sma3 = {"ind": "sma", "line": "v", "len": 3}
    daily = m.store.add_alert("ES", "ind", ia.clean_params({"ind": "price"}, sma3, "1D", "close", "above"), False, "")
    hourly = m.store.add_alert("ES", "ind", ia.clean_params({"ind": "price"}, {"value": 7000}, "1h", "close", "gt"), True, "")
    fired = lambda: [(e["alert_id"], e["ts"]) for e in reversed(m.store.list_events(1000))]   # noqa: E731

    async def go():
        m.alerts_changed()
        for d, px in [(6, 7500.0), (7, 7480.0), (8, 7460.0), (9, 7440.0), (10, 7500.0)]:   # Friday the 10th closes above
            for k in range(137):                                                          # the session, 17:05 .. 15:45
                m.on_tick(Tick("ES", _ct(d - 1, 17, 5) + k * 600_000, px, 1, 1))
            m.on_tick(Tick("ES", _ct(d, 15, 59), px, 1, 1))
        n = len(fired())   # the hourly one on each bar a later trade closed
        assert n and daily not in {i for i, _ in fired()}
        for now in (_ct(10, 16, 0) + 20_000, _ct(10, 16, 1) + 30_000, _ct(11, 12), _ct(12, 16, 59)):
            monkeypatch.setattr(market_mod, "now_ms", lambda now=now: now)
            await m._watchdog_pass()
        assert fired()[n:] == [(daily, _ct(10, 16, 1) + 30_000), (hourly, _ct(10, 16, 1) + 30_000)]   # 15:00's, once
        m.on_tick(Tick("ES", _ct(12, 17) + 1000, 7500.0, 1, 1))   # Sunday's open: neither again
        assert len(fired()) == n + 2
    asyncio.run(go())

def test_intrabar_daily_alert_fired_on_a_holiday_halt_is_once_a_bar_across_a_restart(tmp_path, monkeypatch):
    """MLK Day's short session counts toward Tuesday's daily bar, stamped after its trades (Monday 17:00, or Tuesday's
    RTH open on an RTH chart): a fire on the Monday, then a restart or a gap fill, and not again in that bar."""
    from datetime import datetime
    from chartsrv import sessions
    sessions.set_calendar({"days": [{"date": "2026-01-19", "name": "MLK", "halt": "12:00"}]})
    ct = lambda d, h, mi=0: int(datetime(2026, 1, d, h, mi, tzinfo=sessions.CT).timestamp() * 1000)   # noqa: E731
    for rth, later in [(False, [ct(19, 17, 30), ct(19, 18)]), (True, [ct(20, 8, 30), ct(20, 9)])]:
        (tmp_path / str(rth)).mkdir()
        m, _ = build_market(tmp_path / str(rth), monkeypatch)
        inst, ind = m.instruments["ES"], ia.IndicatorAlerts(m)
        new = lambda: make_series(parse_spec("1D"), inst.tick_size, inst.decimals, 3000, inst.rth,   # noqa: E731
                                  studies=False, rth_only=rth, group=inst.calendar)
        w = ind.watches[("ES", "1D", rth)] = ia._Watch(new())
        w.ready.set()
        w.version = 1
        a = _alert(ia.clean_params({"ind": "price"}, {"ind": "sma", "line": "v", "len": 3}, "1D", "bar", "above", rth))
        trades = [(t, 7600 - k * 0.5) for k, t in enumerate(range(ct(6, 0), ct(19, 9), 1_800_000))
                  if sessions.is_open(t, inst.calendar)]                       # falling days
        trades += [(ct(19, 9) + k * 1_800_000, 7700.0) for k in range(4)]     # MLK morning, far above: crosses up
        got = _feed_at(ind, a, trades)
        assert [ts for ts, _ in got] == [ct(19, 9)]
        a.last_fired = got[0][0]   # as AlertEngine stores it
        w2 = ind.watches[("ES", "1D", rth)] = ia._Watch(new())   # loaded again from the same trades
        for ts, price in trades:
            w2.series.on_tick(Tick("ES", ts, price, 1, 1))
        w2.ready.set()
        w2.version = 2
        assert w2.series.bars[-1].t > ct(19, 9)   # Tuesday's bar, stamped after the fire
        assert _feed_at(ind, a, [(ts, 7700.0) for ts in later]) == [], rth
