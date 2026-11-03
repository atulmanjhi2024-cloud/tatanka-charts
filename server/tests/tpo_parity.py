"""Parity check: chartsrv/tpo.py against the original C# TPO Profile code (compiled with mono).

    tests/tpo_csharp/build.sh <tatanka-indicators checkout> <workdir>
    python tests/tpo_parity.py <workdir> [days] [minutes] [seed] [root] [settings json] [rth]

Generates synthetic 1-minute Globex data, runs it through tpo_service.run (recording every input the
indicator sees: daily bars, then each closed chart bar with its close stamp), replays the record through
both engines and compares every value they produce. rth: an RTH chart's bars, sessions and daily bars (the
instrument's regular hours), as on an RTH trading-hours template.

The port's one deliberate change (tpo.py) is undone for the comparison: the C# prices a finished session's rows with
the row height of the bar that saves it. That is a new height whenever the daily ATR moved before the save: with
Filter by Time off (saved at the 17:00 open), and on an RTH chart (saved at the next RTH open)."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from amt_parity import synth                  # noqa: E402
from chartsrv import tpo                      # noqa: E402
from chartsrv.tpo_service import Daily, run   # noqa: E402

ROOTS = {"ES": (0.25, 6800.0), "NQ": (0.25, 25000.0), "CL": (0.01, 65.0), "GC": (0.10, 3500.0)}
RTHS = {"ES": ("08:30", "15:00"), "NQ": ("08:30", "15:00"), "CL": ("08:00", "13:30"), "GC": ("07:20", "12:30")}
D = lambda x: repr(float(x))
T = lambda t: "MIN" if t == tpo.DMIN else t.strftime("%Y-%m-%dT%H:%M:%S")
PT = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


def write_record(rec, s, tick, path):
    lines = [f"S|{D(tick)}|{int(s['FilterByTime'])}|{s['SessionTime']}|{s['BlockMinutes']}|{int(s['AutoRowHeight'])}|"
             f"{s['TargetRows']}|{s['RowHeightTicks']}"]
    for r in rec:
        if r[0] == "D":
            lines.append(f"D|{D(r[1])}|{D(r[2])}|{D(r[3])}")
        else:
            lines.append(f"B|{T(r[1])}|{D(r[2])}|{D(r[3])}|{int(r[4])}|{T(r[5])}")
    lines.append("E")
    Path(path).write_text("\n".join(lines) + "\n")


def dump_profile(o, tag, p):
    vah, val = tpo.TpoProfile.GetValueArea(p)
    o.append(f"{tag}|{T(p.TimeStart)}|{T(p.TimeEnd)}|{D(p.TickStep)}|{D(p.PocPrice)}|{p.MaxCount}|{D(vah)}|{D(val)}|{len(p.Rows)}")
    for r in p.Rows:
        o.append(f"R|{D(r.Price)}|{','.join(map(str, r.Letters))}|{''.join(tpo.TpoProfile.GetLetter(i) for i in r.Letters)}")


def as_csharp_saves(x, p, step):
    """The profile as the C# saves it: the same rows, priced with the row height current then."""
    if p.TickStep == step:
        return p
    return x.BuildProfileFromMap({int(tpo.round_away(r.Price / p.TickStep)): r.Letters for r in p.Rows},
                                 p.TimeStart, p.TimeEnd, step)


def replay_python(rec_path, s):
    lines = Path(rec_path).read_text().splitlines()
    x, atr, o = None, tpo.NtAtr(), []
    for line in lines:
        p = line.split("|")
        if p[0] == "S":
            x = tpo.TpoProfile(s, float(p[1]))
        elif p[0] == "D":
            x.dailyAtr = atr.add(float(p[1]), float(p[2]), float(p[3]))
            x.CurrentBars[1] += 1
        elif p[0] == "B":
            x.CurrentBars[0] += 1
            x.Time0, x.High0, x.Low0 = PT(p[1]), float(p[2]), float(p[3])
            x.IsFirstBarOfSession, x.TemplateSessionOpen = p[4] == "1", PT(p[5])
            before = len(x.saved)
            x.OnBarUpdate()
            o.append(f"T|{D(x.tickStep)}|{int(x.hasLive)}|{int(x.liveInSession)}|{len(x.liveRows)}|{T(x.sessionStartTime)}|"
                     f"{T(x.drawStartTime)}|{T(x.lastSessBarTime)}")
            for prof in x.saved[before:]:
                dump_profile(o, "P", as_csharp_saves(x, prof, x.tickStep))
        elif p[0] == "E" and x.liveRows:
            live = x.BuildProfileFromMap(x.liveRows, x.drawStartTime, x.Time0, x.liveStep)
            dump_profile(o, "LIVE", as_csharp_saves(x, live, x.tickStep))
    return o


def main():
    argv = [a for a in sys.argv if a != "rth"]
    work = Path(argv[1])
    days = int(argv[2]) if len(argv) > 2 else 40
    minutes = int(argv[3]) if len(argv) > 3 else 5
    seed = int(argv[4]) if len(argv) > 4 else 7
    root = argv[5] if len(argv) > 5 else "ES"
    s = tpo.settings(json.loads(argv[6]) if len(argv) > 6 else {})
    rth = RTHS[root] if len(argv) < len(sys.argv) else None
    tick, price = ROOTS[root]
    bars = synth(days, seed, datetime(2026, 10, 16, 12, 7), tick, price)
    daily = Daily("equity", rth)
    daily.add_minutes(bars, 2**62)
    rec = []
    run(s, tick, minutes, bars, daily, 2**62, rec, rth)
    rec_path, cs_out = work / "tpo_record.txt", work / "tpo_cs.txt"
    write_record(rec, s, tick, rec_path)
    subprocess.run(["mono", str(work / "harness.exe"), str(rec_path), str(cs_out)], check=True)
    cs = cs_out.read_text().splitlines()
    py = replay_python(rec_path, s)

    def same(a, b):
        ta, tb = a.split("|"), b.split("|")
        if len(ta) != len(tb):
            return False
        for x, y in zip(ta, tb):
            if x == y:
                continue
            try:
                if float(x) != float(y):
                    return False
            except ValueError:
                return False
        return True

    bad = [(i, a, b) for i, (a, b) in enumerate(zip(cs, py)) if not same(a, b)]
    saved = sum(1 for l in py if l.startswith("P|"))
    if len(cs) != len(py) or bad:
        print(f"PARITY FAIL: {len(cs)} C# lines, {len(py)} Python lines, {len(bad)} differ")
        for i, a, b in bad[:10]:
            print(f"  line {i}\n    C#: {a}\n    py: {b}")
        sys.exit(1)
    steps = sorted({l.split("|")[1] for l in py if l.startswith("T|")}, key=float)
    print(f"PARITY OK: {root} {minutes}m{' RTH' if rth else ''}, {days} days, {sum(1 for r in rec if r[0] == 'B')} bars, {saved} profiles, "
          f"row heights {steps}, {len(py)} lines; every value equal")


if __name__ == "__main__":
    main()
