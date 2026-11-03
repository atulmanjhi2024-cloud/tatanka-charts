"""Parity check: chartsrv/vp.py against the calculation cores of the three NinjaTrader volume-profile indicators
(tests/vp_csharp, unchanged), compiled with mono.

    tests/vp_csharp/build.sh <workdir>
    python tests/vp_parity.py <workdir>/harness.exe [seed]

Generates synthetic 1-minute bars (across New Year, with a forming minute updated in place) and trades, runs every
case through the C# harness and through vp.py the same way, and compares every number as raw double bits."""
import math
import random
import struct
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chartsrv import vp   # noqa: E402

EPOCH = datetime(1970, 1, 1)
MS = lambda t: int((t - EPOCH) / timedelta(milliseconds=1))   # noqa: E731
DT = lambda ms: EPOCH + timedelta(milliseconds=int(ms))     # noqa: E731
B = lambda x: str(struct.unpack("<q", struct.pack("<d", float(x)))[0])   # noqa: E731


# --- inputs ---------------------------------------------------------------------------------------------------------
def minutes(rng, first: date, days: int, price: float, tick: float, jump: bool = False):
    """M lines: Globex sessions (17:00 the evening before to 16:00) of the trading days, a bar per minute, some
    updated in place as a forming bar is (same index, more volume, a wider range)."""
    out, index, p = [], 0, round(price / tick)
    d = first
    while days > 0:
        if d.weekday() < 5:
            days -= 1
            begin = datetime(d.year, d.month, d.day) - timedelta(hours=7)
            for m in range(23 * 60):
                if 0 < m % 233 < 3:   # a few missing minutes
                    continue
                index += 1
                t = begin + timedelta(minutes=m + 1)       # NinjaTrader stamps a minute at its end
                p += rng.choice((-2, -1, -1, 0, 0, 1, 1, 2))
                lo, hi = p - rng.randint(0, 3), p + rng.randint(0, 3)
                if jump and index == 500:
                    lo, hi = p - 21000, p + 400              # a bar too wide for 20,000 one-tick rows
                vol = rng.choice((0, rng.randint(1, 40), rng.randint(40, 900)))
                up = rng.random() < 0.5
                for k in range(rng.choice((1, 1, 1, 2, 3))):   # updates of the forming minute
                    out.append(f"M|{d.isoformat()}|{MS(begin)}|{index}|{lo * tick!r}|{hi * tick!r}|{float(vol + k * 7)!r}|{int(up)}|{MS(t)}")
                    hi += rng.randint(0, 1)
                    if k == 0 and rng.random() < 0.2:      # the same update again
                        out.append(out[-1])
                if rng.random() < 0.002:
                    out.append("S")
        d += timedelta(days=1)
    return out


def trades(rng, first: date, days: int, price: float, tick: float):
    """T lines: trades from 06:00 to 17:00 on weekdays (and Sunday evening), flat runs included, some with no
    size, a few updated in place (same index, more size: a live tick bar)."""
    out, index, p = [], 0, round(price / tick)
    d = first
    for _ in range(days):
        hours = [(17, 23)] if d.weekday() == 6 else [] if d.weekday() == 5 else [(6, 17)]
        for h0, h1 in hours:
            t = datetime(d.year, d.month, d.day, h0)
            while t < datetime(d.year, d.month, d.day, h1):
                t += timedelta(milliseconds=rng.randint(200, 9000))
                if rng.random() < 0.55:
                    p += rng.choice((-1, 1))
                index += 1
                size = rng.choice((0, 1, 1, 2, 3, 5, 10, rng.randint(11, 200)))
                out.append(f"T|{index}|{p * tick!r}|{float(size)!r}|{MS(t)}")
                if rng.random() < 0.03:
                    out.append(f"T|{index}|{p * tick!r}|{float(size + 4)!r}|{MS(t)}")
            if rng.random() < 0.3:
                out.append("S")
        d += timedelta(days=1)
    return out


def cases(seed: int):
    rng = random.Random(seed)
    yearend = minutes(rng, date(2025, 12, 1), 34, 6800.0, 0.25)
    cl = minutes(rng, date(2026, 3, 2), 12, 65.0, 0.01)
    wide = minutes(rng, date(2026, 5, 4), 3, 300.0, 0.01, jump=True)
    out = []
    for anchor in vp.ANCHORS:
        out.append((f"A|{anchor}|0.25|1|200|68|0", yearend))
    out += [("A|Year|0.25|0|1|70|1", yearend), ("A|Month|0.25|1|7|100|0", yearend), ("A|Week|0.01|1|200|68|0", cl),
            ("A|Session|0.01|0|3|1|0", cl), ("A|Year|0.01|0|1|68|0", wide), ("A|Year|0.01|1|50|68|1", wide)]
    for period, mult in (("Daily", 1), ("Daily", 3), ("Weekly", 1), ("Weekly", 2), ("Monthly", 1), ("Monthly", 5), ("Yearly", 1), ("Yearly", 3)):
        out.append((f"P|{period}|{mult}|20|0.25|1|200|68|0", yearend))
    out += [("P|Weekly|1|3|0.25|0|2|70|1", yearend), ("P|Daily|1|20|0.01|1|60|68|0", cl), ("P|Weekly|1|5|0.01|0|1|68|0", wide)]
    es_t, cl_t = trades(rng, date(2026, 2, 1), 9, 6800.0, 0.25), trades(rng, date(2026, 3, 1), 6, 65.0, 0.01)
    for start, end in (("08:30", "15:00"), ("17:00", "16:00"), ("00:00", "00:00"), ("9:30", "16:00"), ("06:00:00", "06:00")):
        out.append((f"V|{start}|{end}|0.25|1|20|68|0", es_t))
    out += [("V|08:30|15:00|0.25|2|20|70|1", es_t), ("V|08:30|15:00|0.25|4|3|68|0", es_t), ("V|08:30|15:00|0.01|1|20|100|0", cl_t),
            ("V|08:30|15:00|0.01|5|20|1|0", cl_t), ("V|17:00|16:00|0.01|1|2|68|1", cl_t)]
    return out


# --- the port, driven as the harness drives the C# ---------------------------------------------------------------------
def _minute(tag, p: vp.MinuteProfile, o: list):
    s = p.snapshot()
    rows = s["rows"]
    o.append(f"{tag}|{p.window[0].isoformat()}|{p.start_chart}|{p.last_chart}|{int(p.first_loaded)}|{len(rows)}")
    if not rows:
        return
    o.append("|".join(B(s[k]) for k in ("total", "va_volume", "max_volume", "max_abs_delta", "poc", "vah", "val", "low_edge", "high_edge"))
             + f"|{s['poc_index']}|{s['vah_index']}|{s['val_index']}|{s['ticks_per_row']}|{int(s['capped'])}")
    o.extend(f"{r[0]},{r[1]},{B(r[2])},{B(r[3])}" for r in rows)


def _session(s: vp.SvpSession, percent, at_least, o: list):
    p = s.snapshot(percent, at_least)
    rows = p["rows"]
    o.append(f"SESSION|{p['start']}|{p['end']}|{p['last']}|{len(rows)}")
    if not rows:
        return
    o.append("|".join(B(p[k]) for k in ("row_size", "max_volume", "total", "va_volume", "poc", "vah", "val", "low_edge", "high_edge"))
             + f"|{p['poc_key']}|{p['vah_key']}|{p['val_key']}")
    o.extend(f"{r[0]},{B(r[1])},{B(r[2])}" for r in rows)


def port(lines: list[str]) -> str:
    o, mode = [], None
    for line in lines:
        f = line.split("|")
        if f[0] == "A":
            mode, series = "A", vp.AnchorSeries(f[1], float(f[2]), f[3] == "1", int(f[4]), float(f[5]), f[6] == "1")
        elif f[0] == "P":
            mode = "P"
            series = vp.PeriodicSeries(f[1], int(f[2]), int(f[3]), float(f[4]), f[5] == "1", int(f[6]), float(f[7]), f[8] == "1")
        elif f[0] == "V":
            mode, start, end = "V", vp.parse_time(f[1]), vp.parse_time(f[2])
            tick, tpr, keep, percent, at_least = float(f[3]), int(f[4]), int(f[5]), float(f[6]), f[7] == "1"
            sessions, cur, last_index, counted = [], None, -1, 0.0
        elif f[0] == "M":
            td = date.fromisoformat(f[1])
            args = (td, int(f[2]), int(f[3]), float(f[4]), float(f[5]), float(f[6]), f[7] == "1", int(f[8]))
            if mode == "A":
                series.upsert(*args)
            else:
                series.add(*args)
        elif f[0] == "T":
            index, vol = int(f[1]), float(f[3])   # TickCounter.Consume
            if index < last_index or vol <= 0 or math.isnan(vol) or math.isinf(vol):
                continue
            if index != last_index:
                last_index, counted = index, 0.0
            volume = max(0.0, vol - counted)
            counted = max(counted, vol)
            if volume <= 0:
                continue
            t = DT(f[4])
            w = vp.try_window(t, start, end)
            if w:
                if cur is None or cur.start_local != w[0]:
                    if cur is not None and w[0] < cur.start_local:
                        continue
                    cur = vp.SvpSession(w[0], MS(w[0]), MS(w[1]), tick, tpr)
                    sessions.append(cur)
                    del sessions[:-keep]
                cur.add(float(f[2]), volume, int(f[4]))
        elif f[0] in ("S", "E"):
            if mode == "A":
                if series.current is not None:
                    _minute("AAVP", series.current, o)
                else:
                    o.append("AAVP|none")
            elif mode == "P":
                o.append(f"PVP|{len(series.profiles)}")
                for p in series.profiles:
                    _minute("PROFILE", p, o)
            else:
                o.append(f"SVP|{len(sessions)}")
                for s in sessions:
                    _session(s, percent, at_least, o)
            if f[0] == "E":
                o.append("END")
    return "\n".join(o) + "\n"


def run(harness: str, seed: int = 1) -> list[str]:
    """The cases where the two differ (empty: all the same), with the first differing line."""
    bad = []
    for head, body in cases(seed):
        lines = [head, *body, "E"]
        want = subprocess.run(["mono", harness], input="\n".join(lines) + "\n", capture_output=True, text=True, check=True).stdout
        got = port(lines)
        if want != got:
            w, g = want.splitlines(), got.splitlines()
            i = next((k for k in range(min(len(w), len(g))) if w[k] != g[k]), min(len(w), len(g)))
            bad.append(f"{head}: line {i}: C# {w[i] if i < len(w) else '<end>'!r} vs port {g[i] if i < len(g) else '<end>'!r}")
        else:
            print(f"same  {head}  ({len(want.splitlines())} lines)")
    return bad


if __name__ == "__main__":
    problems = run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    for p in problems:
        print("DIFF", p)
    sys.exit(1 if problems else 0)
