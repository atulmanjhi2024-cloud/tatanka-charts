"""Standard indicators (ta.js): EMA, SMA, RSI, MACD and ATR match NinjaTrader 8's formulas, the forming bar's
value is the same as if the bar had closed, and cumulative delta restarts at each 17:00 CT session open."""
import json
import random
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from chartsrv.htfema import NtEma
from chartsrv.sessions import CT
from chartsrv.tpo import NtAtr

WEB = Path(__file__).resolve().parents[2] / "web"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def run_js(bars, settings):
    """Every setting's plots on every bar, all closed; plus each bar's value as a forming bar (step without commit)."""
    js = f"""
const vm = require('vm'), fs = require('fs'), ctx = {{ Intl, window: {{}} }};
vm.runInNewContext(fs.readFileSync({json.dumps(str(WEB / 'ta.js'))}, 'utf8') + ';this.r=indRunner;this.c=indClean;', ctx);
const {{ bars, settings }} = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(settings.map((s) => {{
  const r = ctx.r(ctx.c(s)), closed = [], forming = [];
  for (const b of bars) {{ forming.push(r.step(b, false)); closed.push(r.step(b, true)); }}
  return {{ closed, forming, clean: ctx.c(s) }};
}})));"""
    out = subprocess.run(["node", "-e", js], input=json.dumps({"bars": bars, "settings": settings}),
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def nt_sma(xs, n):
    return [sum(xs[max(0, i - n + 1):i + 1]) / min(i + 1, n) for i in range(len(xs))]


def nt_rsi(xs, n, smooth):
    """NinjaTrader 8's RSI: up/down moves (0 on the first bar), averaged by SMA once there are Period bars, then
    Wilder smoothing; Avg is an EMA(Smooth) of RSI that starts from 50."""
    ups = [0.0] + [max(xs[i] - xs[i - 1], 0) for i in range(1, len(xs))]
    downs = [0.0] + [max(xs[i - 1] - xs[i], 0) for i in range(1, len(xs))]
    sma_up, sma_down = nt_sma(ups, n), nt_sma(downs, n)
    c1 = 2 / (1 + smooth)
    out, avg, au, ad = [], 50.0 if n < 3 else 0.0, 0.0, 0.0
    for i in range(len(xs)):
        if i == 0 or i + 1 < n:
            if i > 0 and i + 1 == n - 1:
                avg = 50.0
            out.append([None, None])
            continue
        if i + 1 == n:
            ad, au = sma_down[i], sma_up[i]
        else:
            ad, au = (ad * (n - 1) + downs[i]) / n, (au * (n - 1) + ups[i]) / n
        rsi = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
        avg = c1 * rsi + (1 - c1) * avg
        out.append([rsi, avg])
    return out


def walk(n, seed=3, start=None):
    rnd = random.Random(seed)
    t = start or int(datetime(2026, 10, 12, 8, 30, tzinfo=CT).timestamp() * 1000)
    px, bars = 7000.0, []
    for i in range(n):
        o = px
        c = round(o + rnd.choice([-1, 1]) * rnd.randint(0, 12) * 0.25, 2)
        h, l = max(o, c) + rnd.randint(0, 6) * 0.25, min(o, c) - rnd.randint(0, 6) * 0.25
        bars.append({"t": t + i * 60_000, "o": o, "h": h, "l": l, "c": c, "v": 100, "d": rnd.randint(-40, 40), "seq": i})
        px = c
    return bars


def close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-9 * max(1.0, abs(b))


def test_formulas_match_ninjatrader():
    bars = walk(600)
    closes = [b["c"] for b in bars]
    got = run_js(bars, [{"type": "ema", "len": 21}, {"type": "sma", "len": 50}, {"type": "rsi", "len": 14, "smooth": 3},
                        {"type": "macd", "fast": 12, "slow": 26, "smooth": 9}, {"type": "atr", "len": 14},
                        {"type": "rsi", "len": 2, "smooth": 1}])
    ema = NtEma(21)
    assert all(close(v[0], ema.add(x)) for v, x in zip(got[0]["closed"], closes))
    assert all(close(v[0], s) for v, s in zip(got[1]["closed"], nt_sma(closes, 50)))
    for g, (n, sm) in ((got[2], (14, 3)), (got[5], (2, 1))):
        want = nt_rsi(closes, n, sm)
        assert all(close(a, b) for v, w in zip(g["closed"], want) for a, b in zip(v, w))
        assert g["closed"][n - 2] == [None, None] and g["closed"][n - 1][0] is not None
    fast, slow, sig = NtEma(12), NtEma(26), NtEma(9)
    for v, x in zip(got[3]["closed"], closes):
        m = fast.add(x) - slow.add(x)
        a = sig.add(m)
        assert close(v[0], m) and close(v[1], a) and close(v[2], m - a)
    assert got[3]["closed"][0] == [0, 0, 0]
    atr = NtAtr(14)
    assert all(close(v[0], atr.add(b["h"], b["l"], b["c"])) for v, b in zip(got[4]["closed"], bars))
    for g in got:                             # a forming bar is computed exactly as if it had closed
        assert g["forming"] == g["closed"]


def test_cumulative_delta_restarts_each_session():
    start = int(datetime(2026, 11, 1, 15, 50, tzinfo=CT).timestamp() * 1000)   # Sunday 15:50 CT, the day DST ends
    bars = walk(90, start=start)
    bars = [b for b in bars if not (16 * 60 <= datetime.fromtimestamp(b["t"] / 1000, CT).hour * 60 + datetime.fromtimestamp(b["t"] / 1000, CT).minute < 17 * 60)]
    got = run_js(bars, [{"type": "cvd"}, {"type": "cvd", "reset": "none"}])
    cum, prev_day = 0, None
    for b, (o, c) in zip(bars, got[0]["closed"]):
        dt = datetime.fromtimestamp(b["t"] / 1000, CT)
        day = (dt.date(), dt.hour >= 17)
        if day != prev_day:
            cum = 0
        assert (o, c) == (cum, cum + b["d"])
        cum, prev_day = cum + b["d"], day
    first_new = next(i for i, b in enumerate(bars) if datetime.fromtimestamp(b["t"] / 1000, CT).hour >= 17)
    assert got[0]["closed"][first_new][0] == 0 and got[1]["closed"][first_new][0] != 0
    assert got[1]["closed"][-1][1] == sum(b["d"] for b in bars)


def test_settings_are_cleaned():
    got = run_js(walk(3), [{"type": "ema", "len": "9.6", "color": "red"}, {"type": "rsi", "len": 1, "smooth": 99999},
                           {"type": "macd"}, {"type": "cvd", "reset": "weekly", "color": "#AbCdEf"}])
    assert got[0]["clean"] == {"type": "ema", "color": None, "len": 10}
    assert got[1]["clean"] == {"type": "rsi", "color": None, "len": 2, "smooth": 200}
    assert got[2]["clean"] == {"type": "macd", "color": None, "fast": 12, "slow": 26, "smooth": 9}
    assert got[3]["clean"] == {"type": "cvd", "color": "#AbCdEf", "reset": "session"}
