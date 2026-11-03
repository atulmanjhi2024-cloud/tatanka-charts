"""Parity check: the Python AMT engine against the original C# engine (compiled with mono).

    tests/amt_csharp/build.sh <tatanka-indicators checkout> <workdir>
    python tests/amt_parity.py <workdir> [days] [minutes] [seed] [root] [settings_file]

Generates synthetic 1-minute Globex data, drives AmtRunner (recording every engine input), replays the
record through both engines, and diffs their per-frame dumps."""
import math
import random
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chartsrv import amt                      # noqa: E402
from chartsrv.amt_runner import AmtRunner, epoch_ms   # noqa: E402
from chartsrv.models import Bar as MBar       # noqa: E402
from chartsrv.sessions import is_open         # noqa: E402

def D(x):
    """C# double.ToString("R"): 2 not 2.0, 1E-05 not 1e-05."""
    x = float(x)
    if x != x:
        return "NaN"
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return repr(x).replace("e", "E")
T = lambda t: "MIN" if t == amt.DMIN else t.strftime("%Y-%m-%dT%H:%M:%S")


def synth(days: int, seed: int, end: datetime, tick=0.25, start_price=6800.0):
    """1-minute bars, Globex hours only, with regimes: trend days, gaps, quiet one-sided nights."""
    rng = random.Random(seed)
    t = end - timedelta(days=days)
    t = t.replace(second=0, microsecond=0)
    p, out, drift, last_day = start_price, [], 0.0, None
    while t < end:
        ms = epoch_ms(t)
        if is_open(ms):
            if t.date() != last_day and t.hour == 17:
                last_day = t.date()
                drift = rng.choice([0, 0, 0, 0.6, -0.6, 1.2, -1.2]) * tick / 4   # trend days
                if rng.random() < .25:
                    p += rng.choice([-1, 1]) * rng.randint(8, 40) * tick       # gap at the Globex open
            rth = (t.hour * 60 + t.minute) >= 510 and (t.hour * 60 + t.minute) < 900
            vol_scale = 3.0 if rth else 1.0
            o = p
            path = [o]
            for _ in range(rng.randint(3, 12)):
                p += drift + rng.choice([-1, 0, 1]) * tick * rng.randint(1, 3 if rth else 2)
                p = round(p / tick) * tick
                path.append(p)
            v = int(rng.expovariate(1 / (400 * vol_scale))) + 1
            out.append(MBar(t=ms, o=o, h=max(path), l=min(path), c=p, v=v))
        t += timedelta(minutes=1)
    return out


def write_record(rec, path):
    lines = []
    for r in rec:
        k = r[0]
        if k == "E":
            lines.append(f"E|{r[1]}|{D(r[2])}|{D(r[3])}|{r[4]}")
        elif k == "L":
            lines.append("L|" + ";".join(f"{n},{D(p)},{kind}" for n, p, kind in r[1]))
        elif k == "D":
            lines.append(f"D|{T(r[1])}|{D(r[2])}|{D(r[3])}|{D(r[4])}")
        elif k == "M":
            b = r[1]
            lines.append(f"M|{T(b.Start)}|{T(b.End)}|{D(b.Open)}|{D(b.High)}|{D(b.Low)}|{D(b.Close)}|{D(b.Volume)}")
        elif k == "B":
            b, subs, clone = r[1], r[2], r[3]
            lines.append(f"B|{b.Index}|{T(b.Start)}|{T(b.End)}|{D(b.Open)}|{D(b.High)}|{D(b.Low)}|{D(b.Close)}|{D(b.Volume)}|{len(subs)}|{1 if clone else 0}")
            lines += [f"S|{T(x.Start)}|{T(x.End)}|{D(x.Open)}|{D(x.High)}|{D(x.Low)}|{D(x.Close)}|{D(x.Volume)}" for x in subs]
        elif k == "O":
            lines.append(f"O|{D(r[1])}|{r[2]}")
    Path(path).write_text("\n".join(lines) + "\n")


def dump(o, f):
    o.append(f"F|{f.Bar.Index}")
    o += [f"N|{n.Key}|{n.Text}" for n in f.Notices]
    o += [f"W|{w.Name}|{D(w.Price)}|{D(w.HalfWidth)}|{int(w.Enabled)}|{int(w.Moving)}" for w in f.Watches]
    o += [f"LN|{l.Name}|{l.Group}|{D(l.Price)}|{T(l.Start)}|{T(l.End)}|{l.Color}|{l.TextColor}|{l.Width}|{l.Style}|{l.Offset}|{l.Size}|{int(l.Label)}|{l.Extend}" for l in f.Lines]
    o += [f"Z|{D(z.Top)}|{D(z.Bottom)}|{T(z.Start)}|{T(z.Created)}|{z.Color}|{int(z.Live)}|{z.BorderColor}|{z.BorderWidth}|{z.BorderStyle}" for z in f.Zones]
    for p in f.Profiles:
        keys = sorted(p.Profile.Rows)
        s = w = 0.0
        for k in keys:
            s += p.Profile.Rows[k]
            w += k * p.Profile.Rows[k]
        o.append(f"P|{T(p.Start)}|{T(p.End)}|{p.EndIndex}|{D(p.Profile.Step)}|{D(p.Stats.Poc)}|{D(p.Stats.Vah)}|{D(p.Stats.Val)}|{D(p.Stats.Total)}|{len(keys)}|{keys[0] if keys else 0}|{D(s)}|{D(w)}")
    if f.Labels:
        o.append("DB|" + ";".join(f.Labels) + "|" + ";".join(f.Values) + "|" + ";".join(str(c) for c in f.Colors))
    o.append(f"X|{D(f.RthVwap)}|{D(f.DailyAtr)}|{int(f.RuleActive)}|{int(f.RuleConfirmed)}|{D(f.VaHigh)}|{D(f.VaLow)}|{f.Warning}")


def replay_python(rec_path, settings=None):
    s = amt.Settings(settings)
    e, daily, tracker, last, out = None, amt.DailyHistory(s.daily_atr_length), amt.LevelAlertTracker(), None, []
    P = lambda x: float(x)
    PT = lambda x: datetime.strptime(x, "%Y-%m-%dT%H:%M:%S")
    PB = lambda p, o: amt.Bar(PT(p[o]), PT(p[o + 1]), P(p[o + 2]), P(p[o + 3]), P(p[o + 4]), P(p[o + 5]), P(p[o + 6]))
    lines = Path(rec_path).read_text().split("\n")
    i = 0
    while i < len(lines):
        p = lines[i].split("|")
        k = p[0]
        if k == "E":
            e = amt.Engine(s, p[1], P(p[2]), P(p[3]), int(p[4]))
        elif k == "L":
            lv = [] if len(p) < 2 or not p[1] else [amt.Level(n, P(x), int(kind)) for n, x, kind in (q.split(",") for q in p[1].split(";"))]
            e.ReplaceLevels(lv)
        elif k == "D":
            daily.Add(PT(p[1]), P(p[2]), P(p[3]), P(p[4]))
        elif k == "M":
            e.AddMacro(PB(p, 1))
        elif k == "O":
            out += [f"O|{n.Key}|{n.Text}" for n in tracker.Observe(last.Watches, P(p[1]), p[2], e.Root, e.Tick)]
        elif k == "B":
            b = PB(p, 2)
            b.Index = int(p[1])
            subs = [PB(lines[i + j + 1].split("|"), 1) for j in range(int(p[9]))]
            i += int(p[9])
            target = e.Clone() if p[10] == "1" else e
            last = target.Process(b, subs, daily.Before(amt.trading_date(b.Start)))
            dump(out, last)
        i += 1
    return out


def main():
    work = Path(sys.argv[1])
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 80
    minutes = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 7
    root = sys.argv[5] if len(sys.argv) > 5 else "ES"
    settings_file = sys.argv[6] if len(sys.argv) > 6 else None
    overrides = {}
    if settings_file:
        for kv in Path(settings_file).read_text().split():
            k, v = kv.split("=", 1)
            dv = amt.DEFAULTS[k]
            overrides[k] = (v == "True") if isinstance(dv, bool) else type(dv)(v)
    overrides.setdefault("ib_stop_time", "0830-1515")   # the C# defaults (this port's: 0830-1500, ES's RTH close,
    overrides.setdefault("amt_del_sess", "1500-1505")   # and the OVN lines kept to the 17:00 open)
    tick, price = {"ES": (0.25, 6800.0), "NQ": (0.25, 25000.0), "CL": (0.01, 65.0), "GC": (0.10, 3500.0)}[root]
    bars = synth(days, seed, datetime(2026, 10, 16, 15, 30), tick, price)
    rec = []
    r = AmtRunner(root, tick, minutes, amt.Settings(overrides), record=rec)
    r.commit_visuals = True
    r.set_levels(round(price * 1.002 / tick) * tick, [round(price * (1 + k / 400) / tick) * tick for k in (-6, -4, -2, 2, 4, 6)],
                 None, None)
    rng = random.Random(seed)
    for i, m in enumerate(bars):
        r.add_minute(m)
        if i % 37 == 0:
            r.commit_ready()
        if i % 997 == 0 and r.engine:
            r.preview()                                   # exercises Clone()
            for _ in range(5):
                rec.append(("O", m.c + rng.choice([-2, -1, 0, 1, 2]) * tick * 4, rng.choice(["Cross", "Zone Entry", "Both"])))
    r.commit_ready()
    r.preview()
    rec_path, cs_out = work / "record.txt", work / "cs.txt"
    write_record(rec, rec_path)
    subprocess.run(["mono"] + (["--optimize=-all"] if __import__("os").environ.get("MONO_NOOPT") else []) + [str(work / "harness.exe"), str(rec_path), str(cs_out)] + ([settings_file] if settings_file else []), check=True)
    py = replay_python(rec_path, overrides)
    cs = cs_out.read_text().rstrip("\n").split("\n")
    frames = sum(1 for x in py if x.startswith("F|"))

    def tok(line):
        return [t for part in line.split("|") for t in part.split(";")]

    def same_token(a, b):
        if a == b:
            return True
        try:
            fa, fb = float(a), float(b)
        except ValueError:
            return False
        return fa == fb or (fa != fa and fb != fb)   # exact double equality; NaN == NaN

    def same(a, b):
        ta, tb = tok(a), tok(b)
        return len(ta) == len(tb) and all(same_token(x, y) for x, y in zip(ta, tb))

    if len(py) == len(cs) and all(same(a, b) for a, b in zip(py, cs)):
        print(f"PARITY OK: {root} {minutes}m, {days} days, {len(bars)} minutes, {frames} frames, {len(py)} output lines; every value equal")
        return 0
    for n, (a, b) in enumerate(zip(py, cs)):
        if not same(a, b):
            ctx = next(x for x in reversed(py[:n]) if x.startswith("F|"))
            print(f"FIRST DIFF at line {n} ({ctx}):\n  py: {a}\n  cs: {b}")
            break
    else:
        print(f"length differs: py {len(py)} cs {len(cs)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
