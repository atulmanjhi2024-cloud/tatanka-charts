"""The standard indicators: the chart's (web/ta.js, run by Node) and the server's (ta.py, for indicator alerts) give the
same values on the same bars, with the same settings, also across holiday halts; and a few checked by hand against
NinjaTrader's formulas."""
import json
import math
import random
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from chartsrv import sessions, ta
from chartsrv.config import load_holidays
from chartsrv.sessions import CT

WEB = Path(__file__).resolve().parents[2] / "web" / "ta.js"
CONFIG = Path(__file__).resolve().parents[2] / "config"


def make_bars(n=2500, seed=7):
    """5-minute bars from Monday 17:00 CT, skipping CME's daily break and the weekend: a random walk on the tick."""
    rnd, out, p = random.Random(seed), [], 6800.0
    t = int(datetime(2026, 10, 19, 17, tzinfo=CT).timestamp() * 1000)
    while len(out) < n:
        ct = datetime.fromtimestamp(t / 1000, CT)
        if ct.hour != 16 and ct.weekday() != 5 and not (ct.weekday() == 6 and ct.hour < 17) and not (ct.weekday() == 4 and ct.hour >= 16):
            o = p
            c = round((p + (rnd.random() - 0.5) * 8) * 4) / 4
            out.append({"t": t, "o": o, "h": max(o, c) + round(rnd.random() * 12) / 4, "l": min(o, c) - round(rnd.random() * 12) / 4,
                        "c": c, "v": 0 if rnd.random() < 0.03 else rnd.randrange(50, 950), "d": rnd.randrange(-100, 100)})
            p = c
        t += 300_000
    return out


def configs():
    out = []
    for kind, d in ta.IND.items():
        base = {k: dv for k, dv, *_ in d["params"]}
        out.append({"type": kind, **base})
        alt = {k: "Weekly" if isinstance(kd, tuple) else v + 3 if kd == "int" else round(v * 1.7, 3) for k, v, _, _, kd in d["params"]}
        if kind == "macd":
            alt = {"fast": 5, "slow": 35, "smooth": 4}
        if alt:
            out.append({"type": kind, **alt})
    out.append({"type": "pivots", "range": "Monthly"})
    out.append({"type": "camarilla", "range": "Daily"})
    out.append({"type": "psar", "acc": 0.05, "inc": 0.01, "max": 0.3})
    return out


def py_values(bars, c):
    params = {k: ta.clean_value(kd, c[k], lo, hi, k) if isinstance(kd, tuple) or kd == "int" else ta.clean_value(kd, min(hi, max(lo, c[k])), lo, hi, k)
              for k, _, lo, hi, kd in ta.IND[c["type"]]["params"]}   # as the chart cleans them (indClean)
    r, out = ta.runner(c["type"], params), []
    for b in bars:
        bar = SimpleNamespace(**b)
        f = r.step(bar, False)
        v = r.step(bar, True)
        assert f == v, (c, b)   # the forming bar's value is the closed bar's, and leaves nothing behind
        out.append(v)
    return out


def close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_chart_and_server_compute_the_same(tmp_path):
    bars, cfgs = make_bars(), configs()
    (tmp_path / "bars.json").write_text(json.dumps(bars))
    (tmp_path / "cfg.json").write_text(json.dumps(cfgs))
    r = subprocess.run(["node", str(Path(__file__).with_name("ta_parity.js")), str(WEB), str(tmp_path / "bars.json"), str(tmp_path / "cfg.json")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    for kind, d in ta.IND.items():   # the same settings, bounds and lines on both sides
        js = got["defs"][kind]
        assert js["lines"] == d["lines"] and js["labels"] == ta.LABELS[kind] and js["name"] == d["name"], kind
        assert [[k, dv, lo, hi, list(kd) if isinstance(kd, tuple) else kd] for k, dv, lo, hi, kd in d["params"]] == js["params"], kind
    assert set(got["defs"]) - set(ta.IND) == {"cvd", "cot"}
    for c, js in zip(cfgs, got["out"]):
        py = py_values(bars, c)
        width = len(ta.IND[c["type"]]["lines"])
        bad = [(i, a, b) for i, (a, b) in enumerate(zip(py, js)) if not all(close(x, y) for x, y in zip(a[:width], b[:width]))]
        assert not bad, (c, bad[:3])
        have = sum(v[0] is not None for v in py)   # most bars have a value (weekly and monthly pivots: after the first period)
        assert have > (0 if c.get("range") in ("Weekly", "Monthly") else len(bars) // 2), c


def _b(t, o, h, lo, c, v=100):
    return SimpleNamespace(t=t, o=o, h=h, l=lo, c=c, v=v)


def test_formulas_by_hand():
    run = lambda kind, bars, **p: _all(kind, bars, p)   # noqa: E731
    closes = [10, 11, 12, 13, 14, 15]
    bars = [_b(i * 60_000, c, c + 1, c - 1, c) for i, c in enumerate(closes)]
    # WMA(3): (3*15 + 2*14 + 1*13) / 6; on the first bars, the weights there are
    assert run("wma", bars, len=3)[-1][0] == (45 + 28 + 13) / 6 and run("wma", bars, len=3)[1][0] == (2 * 11 + 10) / 3
    # Bollinger(4, 2): SMA of 12..15 is 13.5, population sd sqrt(1.25)
    up, mid, lo = run("boll", bars, len=4, dev=2)[-1]
    assert mid == 13.5 and math.isclose(up - mid, 2 * math.sqrt(1.25)) and math.isclose(mid - lo, 2 * math.sqrt(1.25))
    # Donchian(3): highest high 16, lowest low 12
    assert run("donch", bars, len=3)[-1] == [16, 14, 12]
    # Williams %R(3): -100 * (16 - 15) / (16 - 12)
    assert run("willr", bars, len=3)[-1] == [-25.0]
    # Momentum and ROC(2): 15 - 13, and that over 13 in percent; the first bar 0
    assert run("mom", bars, len=2)[-1] == [2] and math.isclose(run("roc", bars, len=2)[-1][0], 200 / 13) and run("mom", bars, len=2)[0] == [0]
    # OBV: volume on up closes; VWMA with no volume is the close
    assert run("obv", bars)[-1] == [500] and run("vwma", [_b(0, 5, 6, 4, 5, 0)], len=3)[0] == [5]
    # Stochastics: fast %K 100 * (close - lowest low) / range, 50 on a first bar with no range
    k = run("stoch", bars, k=3, smooth=1, d=1)[-1][0]
    assert k == 100 * (15 - 12) / (16 - 12) and run("stoch", [_b(0, 5, 5, 5, 5)], k=3, smooth=1, d=1)[0] == [50, 50]
    # floor pivots of the day before: H 20, L 10, C 15 -> PP 15, R1 20, S1 10, R2 25, S2 5, R3 35, S3 -5
    day = lambda d, h: int(datetime(2026, 10, d, h, tzinfo=CT).timestamp() * 1000)   # noqa: E731
    pv = run("pivots", [_b(day(20, 9), 12, 20, 10, 15), _b(day(21, 9), 15, 16, 14, 15)], range="Daily")
    assert pv[0] == [None] * 7 and pv[1] == [15, 20, 25, 35, 10, 5, -5]
    cam = run("camarilla", [_b(day(20, 9), 12, 20, 10, 15), _b(day(21, 9), 15, 16, 14, 15)], range="Daily")[1]
    assert [round(x, 6) for x in cam] == [round(15 + 11 / 12, 6), round(15 + 11 / 6, 6), 17.75, 20.5, round(15 - 11 / 12, 6), round(15 - 11 / 6, 6), 12.25, 9.5]
    # the regression channel through a straight line: the line itself, no band
    assert run("linreg", bars, len=4, width=2)[-1][:3] == [15, 15, 15]
    # relative volume: against the same minute of the days before
    rv = run("rvol", [_b(day(20, 9), 1, 1, 1, 1, 100), _b(day(21, 9), 1, 1, 1, 1, 300)], len=10)
    assert rv == [[None], [3.0]]


def _all(kind, bars, params, seconds=False):
    r = ta.runner(kind, params, seconds=seconds)
    return [r.step(b) for b in bars]


def test_settings_are_checked():
    assert ta.clean_value("int", 14, 1, 500, "Period") == 14
    assert ta.clean_value(0.1, 2.04, 0.1, 10, "Std dev") == 2.0
    assert ta.clean_value(("Daily", "Weekly"), "Weekly", None, None, "Range") == "Weekly"
    for bad in [("int", 2.5), ("int", 0), ("int", True), (0.1, math.nan), (("Daily",), "Hourly")]:
        with pytest.raises(ValueError):
            ta.clean_value(bad[0], bad[1], 1, 500, "x")


def js_values(tmp_path, bars, cfgs, calendar=None):
    """web/ta.js's outputs for each setting on the bars (ta_parity.js); calendar: {holidays: hello's, group}."""
    (tmp_path / "bars.json").write_text(json.dumps(bars))
    (tmp_path / "cfg.json").write_text(json.dumps(cfgs))
    args = [str(tmp_path / "bars.json"), str(tmp_path / "cfg.json")]
    if calendar is not None:
        (tmp_path / "cal.json").write_text(json.dumps(calendar))
        args.append(str(tmp_path / "cal.json"))
    r = subprocess.run(["node", str(Path(__file__).with_name("ta_parity.js")), str(WEB), *args], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["out"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_relative_volume_on_seconds_bars_is_against_the_same_bar_of_earlier_days(tmp_path):
    # 15-second bars 08:30:00-08:30:45 CT on two days with the same volumes: every bar of the second day is 1 (the
    # same 15 seconds the day before), and the first day has nothing to compare with (not the minute's earlier bars)
    at = lambda d, s: int(datetime(2026, 10, d, 8, 30, s, tzinfo=CT).timestamp() * 1000)   # noqa: E731
    bars = [{"t": at(d, 15 * k), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1000 if k == 0 else 100} for d in (21, 22) for k in range(4)]
    want = [None] * 4 + [1.0] * 4
    assert [v[0] for v in _all("rvol", [SimpleNamespace(**b) for b in bars], {"len": 10}, seconds=True)] == want
    assert [v[0] for v in js_values(tmp_path, bars, [{"type": "rvol", "len": 10, "seconds": True}])[0]] == want


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_relative_volume_on_tick_bars_is_against_the_same_minute(tmp_path):
    # tick bars opening at a trade's ms, 3-28 s apart, 08:30-09:30 CT on two days: each is measured against the bars
    # that opened in the same minute before it (the day before's, and that minute's earlier ones), so every bar of the
    # second day has a value, not only the few that share a second with one of the first day's
    rnd, bars = random.Random(5), []
    for d in (21, 22):
        t, end = (int(datetime(2026, 10, d, h, 30, tzinfo=CT).timestamp() * 1000) for h in (8, 9))
        while t < end:
            bars.append({"t": t, "o": 1, "h": 1, "l": 1, "c": 1, "v": rnd.randrange(1, 500)})
            t += rnd.randrange(3000, 28000)
    seen, want = {}, []
    for b in bars:
        key = ta.clock_minute(b["t"])
        past = seen.get(key, [])
        want.append(b["v"] / (sum(past) / len(past)) if past else None)
        seen[key] = (past + [b["v"]])[-5:]
    day2 = [w for b, w in zip(bars, want) if datetime.fromtimestamp(b["t"] / 1000, CT).day == 22]
    assert all(w is not None for w in day2)
    assert [v[0] for v in _all("rvol", [SimpleNamespace(**b) for b in bars], {"len": 5})] == want
    assert [v[0] for v in js_values(tmp_path, bars, [{"type": "rvol", "len": 5}])[0]] == want


def _halt_bars(start, end, seed=3):
    """30-minute ES bars from start to end (CT dates) in the hours CME trades, holiday halts and closed days left
    out: a random walk on the tick."""
    rnd, out, p = random.Random(seed), [], 6800.0
    t, stop = (int(datetime(d.year, d.month, d.day, tzinfo=CT).timestamp() * 1000) for d in (start, end))
    while t < stop:
        if sessions.is_open(t, "equity"):
            c = round((p + (rnd.random() - 0.5) * 20) * 4) / 4
            out.append({"t": t, "o": p, "h": max(p, c) + round(rnd.random() * 20) / 4, "l": min(p, c) - round(rnd.random() * 20) / 4,
                        "c": c, "v": rnd.randrange(50, 950)})
            p = c
        t += 1_800_000
    return out


def _hello_holidays() -> dict:
    """The holiday hours as hello gives them to the chart (Market.holiday_hours)."""
    return {d.isoformat(): {k: v for k, v in h.items() if k != "name"} for d, h in sessions.holidays().items()}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_daily_pivots_after_a_holiday_halt_are_the_prior_settled_days(tmp_path):
    # MLK Day 2026 (Monday Jan 19, halted at 12:00 CT, no settlement): its short session counts toward Tuesday, as the
    # 1D chart's bars. Tuesday's pivots are Friday's, Wednesday's from Monday's and Tuesday's trading together.
    sessions.set_calendar(load_holidays(CONFIG))
    at = lambda d, h: int(datetime(2026, 1, d, h, tzinfo=CT).timestamp() * 1000)   # noqa: E731
    bars = []
    for (d0, h0, d1, h1, lo, hi, c) in [(15, 17, 16, 16, 6900, 7000, 6950), (18, 17, 19, 12, 6940, 6960, 6955),
                                        (19, 17, 20, 16, 6950, 7010, 7000), (20, 17, 21, 16, 6990, 7020, 7005)]:
        bars += [{"t": t, "o": lo, "h": hi, "l": lo, "c": c, "v": 100} for t in range(at(d0, h0), at(d1, h1), 1_800_000)]
    day = lambda b: ta.trade_date(b["t"]).day   # noqa: E731   (the plain one: 19 for MLK Day's short session)
    friday, mlk_and_tuesday = ta._floor(7000, 6900, 6950), ta._floor(7010, 6940, 7000)
    want = {16: [None] * 7, 19: friday, 20: friday, 21: mlk_and_tuesday}
    run = ta.runner("pivots", {"range": "Daily"}, "equity")
    py = [run.step(SimpleNamespace(**b)) for b in bars]
    js = js_values(tmp_path, bars, [{"type": "pivots", "range": "Daily"}], {"holidays": _hello_holidays(), "group": "equity"})[0]
    for b, p, j in zip(bars, py, js):
        assert all(close(x, y) for x, y in zip(p, want[day(b)])) and all(close(x, y) for x, y in zip(j, want[day(b)])), (day(b), p, j)
    # with no holiday calendar (a free-data chart): every session a day of its own, Tuesday's from Monday's
    tuesday = next(i for i, b in enumerate(bars) if day(b) == 20)
    run = ta.runner("pivots", {"range": "Daily"})
    py = [run.step(SimpleNamespace(**b)) for b in bars]
    js = js_values(tmp_path, bars, [{"type": "pivots", "range": "Daily"}])[0]
    assert all(close(x, y) for v in (py[tuesday], js[tuesday]) for x, y in zip(v, ta._floor(6960, 6940, 6955)))


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_chart_and_server_fold_holiday_halts_the_same(tmp_path):
    # June-July 2026: Juneteenth (Friday) and July 3rd (Friday) halts count toward the next week, as the 1W chart's bars
    sessions.set_calendar(load_holidays(CONFIG))
    bars = _halt_bars(date(2026, 5, 24), date(2026, 8, 8))
    cfgs = [{"type": k, "range": r} for k in ("pivots", "camarilla") for r in ("Daily", "Weekly", "Monthly")]
    js = js_values(tmp_path, bars, cfgs, {"holidays": _hello_holidays(), "group": "equity"})
    for c, j in zip(cfgs, js):
        r = ta.runner(c["type"], {"range": c["range"]}, "equity")
        py = [r.step(SimpleNamespace(**b)) for b in bars]
        bad = [(i, a, b) for i, (a, b) in enumerate(zip(py, j)) if not all(close(x, y) for x, y in zip(a, b))]
        assert not bad, (c, bad[:3])
    # the weekly pivots on July 3rd's short session and the week after: from June 29 - July 2 alone
    week = [b for b in bars if date(2026, 6, 29) <= ta.trade_date(b["t"]) <= date(2026, 7, 2)]
    want = ta._floor(max(b["h"] for b in week), min(b["l"] for b in week), week[-1]["c"])
    weekly = js[1]
    for i, b in enumerate(bars):
        if date(2026, 7, 3) <= ta.trade_date(b["t"]) <= date(2026, 7, 10):
            assert all(close(x, y) for x, y in zip(weekly[i], want)), ta.trade_date(b["t"])
