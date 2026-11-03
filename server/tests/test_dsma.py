"""Daily SMA x4 port: its settings, the daily bars the server builds, /api/dsma, and the browser code (node).
Parity with the original C# is checked by tests/dsma_parity.py (needs mono and node)."""
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chartsrv import dsma, indicators, sessions
from chartsrv.sessions import CT

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
MIN = 60_000


def ms(*a) -> int:
    return int(datetime(*a, tzinfo=CT).timestamp() * 1000)


def minutes(start: int, end: int, step=30, group="equity", price=lambda t: 100 + (t // MIN) % 7):
    """(open ms, close) every `step` minutes the market is open."""
    return [(t, float(price(t))) for t in range(start, end, step * MIN) if sessions.is_open(t, group)]


def test_settings_come_from_the_source():
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    from gen_dsma_schema import SRC, parse
    want = parse(SRC.read_text())
    got = indicators.SCHEMA["dsma"]
    assert [x["name"] for x in got["settings"]] == [x["name"] for x in want["settings"]]
    d = indicators.defaults("dsma")
    assert [d[f"SMA{i}Length"] for i in range(1, 5)] == [8, 21, 50, 200]
    assert (d["LineWidth"], d["ShowLabels"], d["LabelOffset"], d["LabelSize"]) == (2, True, 10, "Normal")
    assert [d[f"PlotDailySMA{i}"]["color"] for i in range(1, 5)] == ["#ffff00", "#0000ff", "#ff0000", "#ffffff"]
    assert indicators.clean("dsma", {"SMA4Length": 100, "LabelSize": "Large", "SMA1Length": 8}) == {"SMA4Length": 100, "LabelSize": "Large"}
    for bad in ({"SMA1Length": 0}, {"SMA2Length": 1001}, {"LineWidth": 5}, {"LabelOffset": 101}, {"LabelSize": "Huge"}):
        with pytest.raises(ValueError):
            indicators.clean("dsma", bad)


def test_daily_bars_as_cme_has_them():
    """A session running into a holiday halt joins the next day's bar, which ends at the early close."""
    sessions.set_calendar({"days": [{"date": "2026-11-26", "halt": "12:00"}, {"date": "2026-11-27", "close": "12:15"}]})
    m = minutes(ms(2026, 11, 22, 17), ms(2026, 12, 1, 16))
    d = dsma.DailyBars("equity", None)
    opens, ends, closes = [], [], []
    d.add(m, opens, ends, closes)
    got = [(sessions.to_ct(o).strftime("%a %d %H:%M"), sessions.to_ct(e).strftime("%a %d %H:%M")) for o, e in zip(opens, ends)]
    assert got == [("Sun 22 17:00", "Mon 23 16:00"), ("Mon 23 17:00", "Tue 24 16:00"), ("Tue 24 17:00", "Wed 25 16:00"),
                   ("Wed 25 17:00", "Fri 27 12:15"), ("Sun 29 17:00", "Mon 30 16:00"), ("Mon 30 17:00", "Tue 01 16:00")]
    last_fri = max(t for t, _ in m if t < ms(2026, 11, 27, 12, 15))
    assert closes[3] == dict(m)[last_fri]                          # the merged day closes with Friday's last trade
    # an RTH-only chart: one bar per regular session, the halt's joining the next day's (the RTH 1D chart's), cut at
    # the early close
    r = dsma.DailyBars("equity", ("08:30", "15:00"))
    opens, ends, closes = [], [], []
    r.add(m, opens, ends, closes)
    got = [(sessions.to_ct(o).strftime("%a %d %H:%M"), sessions.to_ct(e).strftime("%a %d %H:%M")) for o, e in zip(opens, ends)]
    assert got[2:5] == [("Wed 25 08:30", "Wed 25 15:00"), ("Thu 26 08:30", "Fri 27 12:15"), ("Mon 30 08:30", "Mon 30 15:00")]


def test_compute_trims_caches_and_caps(monkeypatch):
    m = minutes(ms(2022, 1, 3), ms(2026, 10, 16, 12), step=240)    # about 1200 trading days
    reads = []

    def read(db, sym, a, b):
        reads.append((a, b))
        return [x for x in m if a <= x[0] < b]
    now = ms(2026, 10, 16, 12)
    start = ms(2026, 9, 1, 8, 30)
    split = now - 10 * MIN
    live = [(t, 200.0) for t in range(split, now, MIN)]
    dsma.forget("ES")
    d = dsma.compute("db", "ES", "equity", None, start, 50, split, live, now, read)
    ends = [x[1] for x in d["days"]]
    at = next(i for i, e in enumerate(ends) if e > start)
    assert at == 50                                                 # the first chart bar shows the day before's SMA
    assert d["forming"] and d["days"][-1][2] == 200.0 and ends[-1] == ms(2026, 10, 16, 16)
    # the next request reads only the minutes stored since; the same days come out
    d2 = dsma.compute("db", "ES", "equity", None, start, 50, now, [], now, read)
    assert reads[-1] == (split, now) and d2["days"][:-1] == d["days"][:-1]
    # further back than built: built again from there; never more than NinjaTrader's 1000 daily bars
    d3 = dsma.compute("db", "ES", "equity", None, ms(2022, 6, 1), 1000, now, [], now, read)
    assert reads[-1][0] < ms(2023, 1, 2) and len(d3["days"]) == len({o for o, _, _ in d3["days"]}) == 1000
    full = dsma.DailyBars("equity", None)
    o, e, c = [], [], []
    full.add(m, o, e, c)
    assert d3["days"] == [list(x) for x in zip(o, e, c)][-1000:]
    dsma.forget("ES")
    assert not any(k[1] == "ES" for k in dsma._cache)


def test_api(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "20")
    from chartsrv.app import app
    with TestClient(app) as c:
        now = int(datetime.now(CT).timestamp() * 1000)
        r = c.get("/api/dsma", params={"symbol": "ES", "start": now - 3_600_000, "length": 200})
        assert r.status_code == 200
        d = r.json()
        assert set(d) == {"days", "forming"} and all(len(x) == 3 and x[0] < x[1] for x in d["days"])
        assert c.get("/api/dsma", params={"symbol": "ES", "start": now, "rth": "true"}).status_code == 200
        assert c.get("/api/dsma", params={"symbol": "NOPE", "start": now}).status_code == 404
        assert c.get("/api/dsma", params={"symbol": "ES", "start": now, "length": 1001}).status_code == 400
        assert c.put("/api/indicators/dsma", json={"SMA1Length": 9}).status_code == 200
        assert c.get("/api/indicators").json()["overrides"]["dsma"] == {"SMA1Length": 9}


def js(snippet: str, data):
    code = f"""
const vm = require('vm'), fs = require('fs'), ctx = {{}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(WEB / 'studies.js'))}, 'utf8') + ';this.prep=dailySmaPrep;this.vals=dailySmaValues;', ctx);
const a = JSON.parse(fs.readFileSync(0, 'utf8'));
const nan = (out) => out.map((v) => v.map((x) => (Number.isNaN(x) ? null : x)));
{snippet}"""
    return json.loads(subprocess.run(["node", "-e", code], input=json.dumps(data), capture_output=True, text=True, check=True).stdout)


def reference(days, periods, k, close=None):
    """Day k's SMAs by NinjaTrader's running sum (close: the developing day's live price)."""
    cl = [x[2] for x in days]
    if close is not None:
        cl[k] = close
    out = []
    for P in periods:
        s = 0.0
        for j in range(k + 1):
            s = s + cl[j] - (cl[j - P] if j >= P else 0)
        out.append(None if k < P - 1 else s / (k + 1 if k < P else P))
    return out


@needs_node
def test_calendar_bars_replay_and_live_bars():
    """Weekly bars take the SMAs of their week's last day, the forming week the developing day's; in replay the day
    its clock is in forms, with the replayed price, and nothing after counts; a bar that took live values keeps them."""
    m = minutes(ms(2026, 6, 1), ms(2026, 10, 15, 11), step=60)
    d = dsma.DailyBars("equity", None)
    o, e, c = [], [], []
    d.add(m, o, e, c)
    days = [list(x) for x in zip(o, e, c)]
    data = {"days": days, "forming": True}
    weeks = sorted({x[0] - ((sessions.to_ct(x[0]).weekday() + 1) % 7) * 86_400_000 for x in days})   # Sunday 17:00
    bars = [{"t": w, "c": 1} for w in weeks]
    periods = [1, 3, 8, 21]
    out = js("""const p = ctx.prep(a.d.days, a.periods);
console.log(JSON.stringify([nan(ctx.vals(a.bars, a.d, p, { cal: true, periodMs: 0, price: 150 })),
  nan(ctx.vals(a.bars, a.d, p, { cal: true, periodMs: 0, price: 150, until: a.until })),
  nan(ctx.vals(a.bars, a.d, p, { cal: true, periodMs: 0, price: 150, live: new Map([[a.bars[3].t, [1, 2, 3, 4]]]) }))]));""",
             {"d": data, "bars": bars, "periods": periods, "until": ms(2026, 10, 13, 12)})
    plain, replay, live = out
    for i, b in enumerate(bars):
        if i == len(bars) - 1:
            want = reference(days, periods, len(days) - 1, 150.0)
        else:
            want = reference(days, periods, sum(1 for x in days if x[1] <= bars[i + 1]["t"]) - 1)
        assert [v[i] for v in plain] == want, i
    k = sum(1 for x in days if x[1] <= ms(2026, 10, 13, 12))       # Tuesday: the day the replay clock is in, forming
    assert days[k][0] <= ms(2026, 10, 13, 12) < days[k][1] and [v[-1] for v in replay] == reference(days, periods, k, 150.0)
    assert [v[3] for v in live] == [1, 2, 3, 4] and [v[4] for v in live] == [v[4] for v in plain]


@needs_node
def test_after_the_close_the_day_before_stays_put():
    """Data fetched while a day formed, then the 17:00 reopen: the new session's first bar must not take the live
    price as that day's close (it is the next day's), on intraday or daily bars, until the new day's data comes."""
    m = minutes(ms(2026, 9, 1), ms(2026, 10, 14, 15, 30), step=60)
    d = dsma.DailyBars("equity", None)
    o, e, c = [], [], []
    d.add(m, o, e, c)
    days = [list(x) for x in zip(o, e, c)]
    assert days[-1][1] == ms(2026, 10, 14, 16)                     # Wednesday, forming when fetched
    data = {"days": days, "forming": True}
    intraday = [{"t": ms(2026, 10, 14, 15, 55), "c": 1}, {"t": ms(2026, 10, 14, 17), "c": 1}]
    daily = [{"t": days[-2][0], "c": 1}, {"t": days[-1][0], "c": 1}, {"t": ms(2026, 10, 14, 17), "c": 1}]
    out = js("""const p = ctx.prep(a.d.days, a.periods);
const r = (bars, o) => { const v = ctx.vals(bars, a.d, p, { price: 150, ...o }); return [nan(v).map((x) => x.at(-1)), v.developing]; };
console.log(JSON.stringify([r(a.intraday.slice(0, 1), { periodMs: 300000 }), r(a.intraday, { periodMs: 300000 }), r(a.daily, { cal: true })]));""",
             {"d": data, "intraday": intraday, "daily": daily, "periods": [1, 3, 8, 21]})
    L = len(days) - 1
    assert out[0] == [reference(days, [1, 3, 8, 21], L, 150.0), True]     # 15:55: the developing day, live price
    for vals, developing in out[1:]:                                        # 17:00: Wednesday as stored, not moved
        assert developing is False and vals == reference(days, [1, 3, 8, 21], L)
