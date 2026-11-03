"""Parity check: the HTF EMA port (chartsrv/htfema.py on the server, htfEmaValues in web/studies.js) against
the original C# indicator code (compiled with mono).

    tests/htfema_csharp/build.sh <tatanka-indicators checkout> <workdir>
    python tests/htfema_parity.py <workdir> [days] [chart: 5m | 1D | t3] [seed] [ES|NQ|CL|GC] [settings json] [rth]

Generates synthetic 1-minute Globex data and the chart and higher-timeframe bars, records the order
NinjaTrader processes them in (by bar close time; the chart bar first on a tie), runs the C# on that record,
and the browser code (node) on the chart bars with the server's payload. Every plotted value must match.
"t3" is a tick-style chart (3-minute buckets treated as untimed bars, closing when the next one opens). rth: an RTH
chart's bars and higher-timeframe bars (the instrument's regular hours), as on an RTH trading-hours template."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from amt_parity import synth                          # noqa: E402
from chartsrv import htfema                           # noqa: E402
from chartsrv.tpo_service import chart_bars           # noqa: E402

ROOTS = {"ES": (0.25, 6800.0), "NQ": (0.25, 25000.0), "CL": (0.01, 65.0), "GC": (0.10, 3500.0)}
RTHS = {"ES": ("08:30", "15:00"), "NQ": ("08:30", "15:00"), "CL": ("08:00", "13:30"), "GC": ("07:20", "12:30")}
WEB = Path(__file__).resolve().parents[2] / "web"


def js_values(bars, d, period_ms, cal=False, after=None):
    """htfEmaValues from web/studies.js, run by node on the given bars and server payload (cal: 2D, 3D, W, M bars;
    after: when the bar after the last one opens, where the chart knows it)."""
    js = f"""
const vm = require('vm'), fs = require('fs'), ctx = {{}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(WEB / 'studies.js'))}, 'utf8') + ';this.f=htfEmaValues;', ctx);
const a = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(ctx.f(a.bars, a.d, a.p, a.cal, a.after).map((x) => (Number.isNaN(x) ? null : x))));"""
    data = json.dumps({"bars": bars, "d": d, "p": period_ms, "cal": cal, "after": after})
    return json.loads(subprocess.run(["node", "-e", js], input=data, capture_output=True, text=True, check=True).stdout)


def setup(days, chart, seed, root, s, rth=None):
    tick, price = ROOTS[root]
    m1 = synth(days, seed, datetime(2026, 10, 16, 12, 7), tick, price)
    now = m1[-1].t + 7_200_000
    timed = chart != "t3"
    if timed:
        kind, size = ("time", 86400) if chart == "1D" else ("time", int(chart[:-1]) * 60)
    else:
        kind, size = "tick", 3
    scaled = htfema.GetScaledLength(s, *htfema.chart_period(kind, size))
    cb = chart_bars(m1, size // 60 if timed else 3, rth)
    bars = [{"t": b[0], "c": b[6]} for b in cb]
    closes = [b[1] for b in cb] if timed else [cb[i + 1][0] for i in range(len(cb) - 1)] + [2**53]
    return m1, now, timed, kind, size, scaled, bars, closes


def main():
    argv = [a for a in sys.argv if a != "rth"]
    work = Path(argv[1])
    days = int(argv[2]) if len(argv) > 2 else 40
    chart = argv[3] if len(argv) > 3 else "5m"
    seed = int(argv[4]) if len(argv) > 4 else 7
    root = argv[5] if len(argv) > 5 else "ES"
    s = htfema.settings(json.loads(argv[6]) if len(argv) > 6 else {})
    rth = RTHS[root] if len(argv) < len(sys.argv) else None
    m1, now, timed, kind, size, scaled, bars, closes = setup(days, chart, seed, root, s, rth)
    d = htfema.compute(s, m1, size // 60 if timed else None, scaled, bars[0]["t"], now, rth=rth)
    # NinjaTrader's processing order: by close time, the chart bar (BarsInProgress 0) first on a tie
    events = [(c, 0, b["c"]) for b, c in zip(bars, closes)]
    events += [(close, 1, c) for _, close, _, _, _, _, c in chart_bars(m1, htfema.htf_minutes(s), rth) if close <= now]
    events.sort(key=lambda e: (e[0], e[1]))
    ctype, cval = htfema.chart_period(kind, size)
    lines = [f"S|{s['HtfPeriodType']}|{s['HtfPeriod']}|{s['EmaLength']}|{s['Mode']}|{ctype}|{cval}"]
    lines += [f"{'P' if k == 0 else 'H'}|{x!r}" for _, k, x in events]
    rec, out = work / "htfema_record.txt", work / "htfema_cs.txt"
    rec.write_text("\n".join(lines) + "\n")
    subprocess.run(["mono", str(work / "harness.exe"), str(rec), str(out)], check=True)
    cs = out.read_text().splitlines()
    assert cs[0] == f"S|{scaled}", (cs[0], scaled)
    want = [None if x == "V|-" else float(x[2:]) for x in cs[1:]]
    got = js_values(bars, d, (size * 1000) if timed else 0)
    bad = [(i, a, b) for i, (a, b) in enumerate(zip(want, got)) if a != b]
    if len(want) != len(got) or bad:
        print(f"PARITY FAIL: {len(want)} C# values, {len(got)} JS values, {len(bad)} differ")
        for i, a, b in bad[:10]:
            print(f"  bar {i}: C# {a}  JS {b}")
        sys.exit(1)
    plotted = sum(v is not None for v in got)
    print(f"PARITY OK: {root} {chart}{' RTH' if rth else ''}, {s['Mode']} {s['HtfPeriodType']} {s['HtfPeriod']} x{s['EmaLength']}, scaled {scaled}, "
          f"{len(got)} bars ({plotted} plotted), {sum(1 for e in events if e[1])} HTF bars; every value equal")


if __name__ == "__main__":
    main()
