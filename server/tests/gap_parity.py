"""Parity check: chartsrv/gaps.py against the original C# GapEngine (compiled with mono).

    tests/gap_csharp/build.sh <tatanka-indicators checkout> <workdir>
    python tests/gap_parity.py <workdir> [days] [minutes] [seed] [ES|NQ|CL|GC] [settings json] [rth]

Generates synthetic 1-minute Globex data, groups it into the chart's bars and records what the indicator
sees with Calculate.OnPriceChange: each bar several times as it grows, plus SessionKey probes and Snapshot
calls over random visible ranges. Replays the record through both engines and compares every value.
rth: an RTH chart's bars (the instrument's regular hours, from the RTH open), as on an RTH trading-hours template."""
import json
import random
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from amt_parity import synth                         # noqa: E402
from chartsrv import gaps                            # noqa: E402
from chartsrv.amt_runner import ct                   # noqa: E402
from chartsrv.sessions import SessionClock, to_ct    # noqa: E402
from chartsrv.studies import RthClock                # noqa: E402
from chartsrv.tpo_service import session_end         # noqa: E402

ROOTS = {"ES": (0.25, 6800.0), "NQ": (0.25, 25000.0), "CL": (0.01, 65.0), "GC": (0.10, 3500.0)}
RTHS = {"ES": ("08:30", "15:00"), "NQ": ("08:30", "15:00"), "CL": ("08:00", "13:30"), "GC": ("07:20", "12:30")}


def D(x):
    return repr(float(x))


def T(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def record(m1, minutes, s, seed, rth=None):
    rng = random.Random(seed)
    lines = [f"S|{s['RthStart']}|{s['RthEnd']}|{D(s['MinimumGapPoints'])}"]
    day = datetime(2026, 10, 14)
    for sec in range(0, 86400 + 1, 450):                 # SessionKey probes around the clock
        lines.append(f"K|{T(day + timedelta(seconds=sec))}")
    period = minutes * 60_000
    clock, hours = SessionClock(), RthClock(rth) if rth else None
    if hours:                                            # an RTH chart: its regular hours, from the RTH open
        m1 = [m for m in m1 if hours.day(m.t) is not None]

    def bucket(t):                                       # the chart bar's open and its session's close
        o, e = hours.window(to_ct(t)) if hours else (clock.locate(t), session_end(clock.locate(t)))
        return o + (t - o) // period * period, e
    i, key, bar, end = -1, None, None, None
    for n, m in enumerate(m1):
        k, s_end = bucket(m.t)
        if k != key:
            i, key, bar = i + 1, k, [m.o, m.h, m.l, m.c]
            end = ct(min(k + period, s_end))
        else:
            bar = [bar[0], max(bar[1], m.h), min(bar[2], m.l), m.c]
        last_minute = n + 1 == len(m1) or bucket(m1[n + 1].t)[0] != k
        if last_minute or rng.random() < 0.5:            # the bar as it grows, and always its final state
            lines.append(f"U|{i}|{T(end)}|{D(bar[0])}|{D(bar[1])}|{D(bar[2])}|{D(bar[3])}")
            if rng.random() < 0.03:
                first = rng.randint(0, i)
                lines.append(f"V|{first}|{rng.randint(first, i + 3)}|{rng.randint(0, 1)}|{rng.randint(0, 1)}|{rng.randint(0, 4)}")
    lines.append("E")
    return lines


def G(o, tag, g):
    o.append(f"{tag}|{g.Ordinal}|{g.StartBar}|{g.NextGapBar}|{g.FillBar}|{g.Direction}|{D(g.OpenPrice)}|"
             f"{D(g.SettlePrice)}|{D(g.FillEdge)}|{D(g.Size)}|{int(g.Filled)}|{g.BarsToFill}|{D(g.FillPercent)}")


def replay_python(lines):
    e, o = None, []
    PT = lambda x: datetime.strptime(x, "%Y-%m-%dT%H:%M:%S")   # noqa: E731
    for line in lines:
        p = line.split("|")
        if p[0] == "S":
            ok_a, a = gaps.GapEngine.TryParseTime(int(p[1]))
            ok_b, b = gaps.GapEngine.TryParseTime(int(p[2]))
            o.append(f"S|{int(ok_a)}|{int(ok_b)}")
            e = gaps.GapEngine(a, b, float(p[3]))
        elif p[0] == "K":
            k = e.SessionKey(PT(p[1]))
            o.append("K|" + ("MIN" if k == gaps.DMIN else k.strftime("%Y-%m-%d")))
        elif p[0] == "U":
            u = e.Update(int(p[1]), PT(p[2]), *map(float, p[3:7]))
            if u.NewGap is not None:
                G(o, "N", u.NewGap)
            for g in u.FilledGaps or []:
                G(o, "X", g)
        elif p[0] == "V":
            f = e.Snapshot(int(p[1]), int(p[2]), p[3] == "1", p[4] == "1", int(p[5]))
            o.append(f"F|{f.StoredCount}|{f.CurrentBar}|{f.Consecutive}|{len(f.VisibleGaps)}")
            if f.Latest is not None:
                G(o, "L", f.Latest)
            for g in f.VisibleGaps:
                G(o, "G", g)
        elif p[0] == "E":
            for g in e.History:
                G(o, "H", g)
    return o


def same(a, b):
    ta, tb = a.split("|"), b.split("|")
    if len(ta) != len(tb):
        return False
    for x, y in zip(ta, tb):
        if x != y:
            try:
                if float(x) != float(y):
                    return False
            except ValueError:
                return False
    return True


def main():
    argv = [a for a in sys.argv if a != "rth"]
    work = Path(argv[1])
    days = int(argv[2]) if len(argv) > 2 else 60
    minutes = int(argv[3]) if len(argv) > 3 else 5
    seed = int(argv[4]) if len(argv) > 4 else 7
    root = argv[5] if len(argv) > 5 else "ES"
    s = gaps.settings(json.loads(argv[6]) if len(argv) > 6 else {})
    rth = RTHS[root] if len(argv) < len(sys.argv) else None
    tick, price = ROOTS[root]
    m1 = synth(days, seed, datetime(2026, 10, 16, 12, 7), tick, price)
    lines = record(m1, minutes, s, seed, rth)
    rec_path, cs_out = work / "gap_record.txt", work / "gap_cs.txt"
    rec_path.write_text("\n".join(lines) + "\n")
    subprocess.run(["mono", str(work / "harness.exe"), str(rec_path), str(cs_out)], check=True)
    cs = cs_out.read_text().splitlines()
    py = replay_python(lines)
    bad = [(i, a, b) for i, (a, b) in enumerate(zip(cs, py)) if not same(a, b)]
    if len(cs) != len(py) or bad:
        print(f"PARITY FAIL: {len(cs)} C# lines, {len(py)} Python lines, {len(bad)} differ")
        for i, a, b in bad[:10]:
            print(f"  line {i}\n    C#: {a}\n    py: {b}")
        sys.exit(1)
    count = lambda tag: sum(1 for x in py if x.startswith(tag + "|"))   # noqa: E731
    print(f"PARITY OK: {root} {minutes}m{' RTH' if rth else ''}, {days} days, {count('U') or sum(1 for x in lines if x[0] == 'U')} updates, "
          f"{count('H')} gaps ({count('X')} filled), {count('F')} snapshots, {len(py)} lines; every value equal")


if __name__ == "__main__":
    main()
