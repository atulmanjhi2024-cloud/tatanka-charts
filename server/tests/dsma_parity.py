"""Parity check: the Daily SMA x4 port (chartsrv/dsma.py on the server, dailySmaValues in web/studies.js) against
the original C# indicator code (tests/dsma_csharp, compiled with mono).

    tests/dsma_csharp/build.sh <workdir>
    python tests/dsma_parity.py <workdir> [days] [chart: 5m | 30m | 4h | t3] [seed] [ES|NQ|CL|GC] [lengths, e.g. 8,21,50,200]

Generates synthetic 1-minute Globex data (CME holidays shut, as config/holidays.json has them), the chart bars and
the server's daily bars, records the order NinjaTrader processes them in (by close time; the chart bar first on a
tie), then goes realtime for the last 90 minutes (a tick a minute). Runs the C# on that record, and the browser code
(node) as the app does: the history, then each tick, new bars keeping the values they took live. Every plotted value
must match. "t3" is a tick-style chart (3-minute buckets as untimed bars, stamped at their last trade)."""
import json
import subprocess
import sys
from bisect import bisect_left, bisect_right
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from amt_parity import synth                          # noqa: E402
from chartsrv import dsma, sessions                   # noqa: E402
from chartsrv.config import load_holidays             # noqa: E402
from chartsrv.tpo_service import chart_bars           # noqa: E402

ROOTS = {"ES": (0.25, 6800.0, "equity"), "NQ": (0.25, 25000.0, "equity"), "CL": (0.01, 65.0, "energy"),
         "GC": (0.10, 3500.0, "metals")}
WEB = Path(__file__).resolve().parents[2] / "web"
CONFIG = Path(__file__).resolve().parents[2] / "config"
RT_MINUTES = 90


def js_run(a: dict):
    """dailySmaValues from web/studies.js, run by node as app.js runs it: the history, then each realtime event."""
    js = f"""
const vm = require('vm'), fs = require('fs'), ctx = {{}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(WEB / 'studies.js'))}, 'utf8') + ';this.prep=dailySmaPrep;this.vals=dailySmaValues;', ctx);
const a = JSON.parse(fs.readFileSync(0, 'utf8')), bars = a.hist.map((b) => ({{ ...b }})), live = new Map();
const prep = ctx.prep(a.d.days, a.periods), o = () => ({{ cal: a.cal, periodMs: a.periodMs, live, price: bars.at(-1).c, until: null }});
let vals = ctx.vals(bars, a.d, prep, o());
for (const [kind, t, c] of a.rt) {{
  const i = bars.length - (kind === 'N' ? 0 : 1);
  if (kind === 'N') {{ bars.push({{ t, c }}); vals = ctx.vals(bars, a.d, prep, o()); }}
  else {{ bars[i].c = c; const out = ctx.vals([bars[i]], a.d, prep, o()); vals.developing = out.developing; out.forEach((v, p) => {{ vals[p][i] = v[0]; }}); }}
  if (vals.developing) live.set(bars[i].t, vals.map((v) => v[i]));
}}
vals = ctx.vals(bars, a.d, prep, o());
console.log(JSON.stringify(bars.map((_, i) => vals.map((v) => (Number.isNaN(v[i]) ? null : v[i])))));"""
    out = subprocess.run(["node", "-e", js], input=json.dumps(a), capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def setup(days: int, chart: str, seed: int, root: str, periods: list[int], chart_days: int = 12):
    tick, price, group = ROOTS[root]
    sessions.set_calendar(load_holidays(CONFIG))
    m1 = [b for b in synth(days, seed, datetime(2026, 10, 16, 12, 7), tick, price) if sessions.is_open(b.t, group)]
    split = m1[-RT_MINUTES].t                         # realtime from here
    hist, rt = m1[:-RT_MINUTES], m1[-RT_MINUTES:]
    timed = chart != "t3"
    size = (int(chart[:-1]) * (60 if chart.endswith("h") else 1)) if timed else 3
    every = chart_bars(m1, size)
    first = bisect_left([b[0] for b in every], m1[-1].t - chart_days * 86_400_000)
    every = every[first:]
    opens = [b[0] for b in every]
    mins = [m.t for m in m1]

    def stamp(i):   # NinjaTrader's bar time: a time bar's close; an untimed bar's last trade
        b = every[i]
        return b[1] if timed else mins[bisect_right(mins, b[1] - 1) - 1] + 30_000

    d = dsma.compute("parity", root, group, None, every[0][0], max(periods), split, [], split,
                     lambda db, sym, a, b: [(m.t, m.c) for m in hist if a <= m.t < b])
    n_hist = bisect_right(opens, split - 1)           # the chart bars begun before realtime: the last one partial
    last_close = {}                                   # each historical bar's close so far
    for m in hist:
        j = bisect_right(opens, m.t) - 1
        if j >= 0:
            last_close[j] = m.c
    bars = [{"t": every[i][0], "c": last_close[i]} for i in range(n_hist)]
    events = [(stamp(i), 0, f"P|{stamp(i)}") for i in range(n_hist)]
    events += [(e, 1, f"D|{e}|{c!r}") for _, e, c in d["days"]]
    events.sort(key=lambda e: (e[0], e[1]))
    lines = ["S|" + "|".join(map(str, periods))] + [e[2] for e in events] + ["R"]
    rt_js, cur = [], n_hist - 1
    for m in rt:
        j = bisect_right(opens, m.t) - 1
        if j != cur:
            cur = j
            lines.append(f"N|{stamp(j)}")
            rt_js.append(["N", every[j][0], m.c])
        else:
            rt_js.append(["T", None, m.c])
        lines.append(f"T|{m.c!r}")
    a = {"hist": bars, "d": d, "periods": periods, "cal": False, "periodMs": size * 60_000 if timed else 0, "rt": rt_js}
    return lines, a, len(d["days"])


def main():
    work = Path(sys.argv[1])
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 330
    chart = sys.argv[3] if len(sys.argv) > 3 else "5m"
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 7
    root = sys.argv[5] if len(sys.argv) > 5 else "ES"
    periods = [int(x) for x in sys.argv[6].split(",")] if len(sys.argv) > 6 else [8, 21, 50, 200]
    lines, a, n_days = setup(days, chart, seed, root, periods)
    rec, out = work / "dsma_record.txt", work / "dsma_cs.txt"
    rec.write_text("\n".join(lines) + "\n")
    subprocess.run(["mono", str(work / "harness.exe"), str(rec), str(out)], check=True)
    want = [[None if x == "-" else float(x) for x in ln.split("|")[1:]] for ln in out.read_text().splitlines()]
    got = js_run(a)
    bad = [(i, w, g) for i, (w, g) in enumerate(zip(want, got)) if w != g]
    if len(want) != len(got) or bad:
        print(f"PARITY FAIL: {len(want)} C# bars, {len(got)} JS bars, {len(bad)} differ")
        for i, w, g in bad[:10]:
            print(f"  bar {i}: C# {w}  JS {g}")
        sys.exit(1)
    plotted = [sum(v[p] is not None for v in got) for p in range(4)]
    steps = len({tuple(v) for v in got})
    print(f"PARITY OK: {root} {chart}, lengths {periods}, {len(got)} chart bars ({RT_MINUTES} realtime minutes), "
          f"{n_days} daily bars; plotted {plotted}, {steps} distinct values; every value equal")


if __name__ == "__main__":
    main()
