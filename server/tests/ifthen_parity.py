"""Parity check: the If/Then port (web/studies.js on the chart, chartsrv/ifthen.py for the daily ATR) against the
original C# TatankaBiasLogic and TatankaBiasDailyAtr (compiled with mono).

    tests/ifthen_csharp/build.sh <tatanka-indicators checkout> <workdir>
    python tests/ifthen_parity.py <workdir> [blocks] [seed]

Fuzzes pasted blocks (CRLF, comments, spaces inside numbers, pipes and literal \\n in text, odd biases, junk
numbers), then active rows, price formats, label wrapping, line colors and daily ATR sequences, and compares
every output of the C#, the browser code (node) and the server's ATR."""
import base64
import json
import math
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chartsrv.ifthen import TatankaBiasDailyAtr   # noqa: E402

WEB = Path(__file__).resolve().parents[2] / "web"
B64 = lambda s: base64.b64encode(s.encode()).decode()   # noqa: E731
BIASES = ["U", "V", "N", "D", "P", "u", "d", "x", "", "Up", " D", "ß", "p"]
NUMS = ["5050", "5 050", "5050.5", ".5", "5.", "+12", "-3", "1e3", "1E+2", "abc", "", "5,050", "NaN", "Infinity",
        "1e400", " 7\t", "0", "9999999", "4995.25", "7401.125", "1.005", "0x10", "1e", "--5"]
WORDS = ["Accepts", "above", "5050", "→", "continuation,", "next", "target", "", "x|y", "\\n", "2nd\\nline", "a" * 30,
         "pivot", "  ", "end "]


def block(rng):
    lines = []
    for _ in range(rng.randint(0, 12)):
        r = rng.random()
        if r < 0.08:
            lines.append("# comment " + rng.choice(WORDS))
        elif r < 0.14:
            lines.append("")
        elif r < 0.2:
            lines.append("|".join(rng.choice(NUMS) for _ in range(rng.randint(0, 3))))
        else:
            text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(0, 9)))
            lines.append(f"{rng.choice(BIASES)}|{rng.choice(NUMS)}|{rng.choice(NUMS)}|{text}")
    return rng.choice(["\n", "\r\n", "\r"]).join(lines)


def record(n, seed):
    rng = random.Random(seed)
    out = []
    num = lambda: rng.choice([rng.uniform(-10, 9000), round(rng.uniform(0, 9000), 3), rng.randint(0, 9000) + rng.choice(  # noqa: E731
        [0, .005, .015, .125, .255, .5, .995]), math.nan, 0.0, -0.004, 1e7])
    for _ in range(n):
        out.append(f"B|{B64(block(rng))}")
        for _ in range(8):
            out.append(f"A|{num()!r}".replace("nan", "NaN"))
        for _ in range(6):
            out.append(f"F|{num()!r}".replace("nan", "NaN"))
        out.append(f"W|{rng.randint(0, 60)}|{B64(' '.join(rng.choice(WORDS) for _ in range(rng.randint(0, 20))))}")
        for mode in ("Bias", "Position"):
            atr = rng.choice([math.nan, rng.uniform(5, 120)])
            out.append(f"C|{mode}|{rng.choice([0, 10, 25.5])!r}|{num()!r}|{atr!r}|#f23645|#4caf50|#ffeb3b".replace("nan", "NaN"))
    for length in (1, 2, 14):
        out.append(f"N|{length}")
        i, close = 0, 5000.0
        for _ in range(60):
            h = close + rng.uniform(0, 40)
            l = close - rng.uniform(0, 40)
            close = rng.uniform(l, h)
            out.append(f"U|{i}|{h!r}|{l!r}|{close!r}")
            if rng.random() < 0.7:
                i += 1                       # otherwise the same (developing) day again
    return out


JS = f"""
const vm = require('vm'), fs = require('fs'), ctx = {{}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(WEB / 'studies.js'))}, 'utf8') +
  ';this.L={{biasParse, biasActiveRow, biasFormat, biasWrap, biasRange, biasLevelColor}};', ctx);
const L = ctx.L, b64 = (s) => Buffer.from(s, 'utf8').toString('base64'), u64 = (s) => Buffer.from(s, 'base64').toString('utf8');
const num = (s) => (s === 'NaN' ? NaN : Number(s)), hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)).concat(1);
let rows = [];
const out = [];
for (const line of fs.readFileSync(0, 'utf8').split('\\n')) {{
  const p = line.split('|');
  if (p[0] === 'B') {{
    rows = L.biasParse(u64(p[1]));
    out.push(`R|${{rows.length}}`);
    for (const r of rows) out.push(`r|${{b64(r.Bias)}}|${{r.Low}}|${{r.High}}|${{b64(r.Text)}}|${{b64(L.biasRange(r))}}`);
  }} else if (p[0] === 'A') out.push(`a|${{L.biasActiveRow(rows, num(p[1]))}}`);
  else if (p[0] === 'F') out.push(`f|${{L.biasFormat(num(p[1]))}}`);
  else if (p[0] === 'W') out.push(`w|${{b64(L.biasWrap(u64(p[2]), Number(p[1])))}}`);
  else if (p[0] === 'C') {{
    const s = {{ LineColorMode: p[1], AtrPercent: num(p[2]), Above: hex(p[5]), Below: hex(p[6]), Within: hex(p[7]) }};
    out.push('c|' + rows.map((r) => L.biasLevelColor(r, num(p[3]), num(p[4]), s)).map((c) => `${{c[0]}},${{c[1]}},${{c[2]}},${{Math.round(c[3] * 1000) / 1000}}`).join(';'));
  }}
}}
console.log(out.join('\\n'));"""


def same(a, b):
    """Exact text, except the row prices and ATR values, which are compared as numbers (G17 vs JS digits)."""
    if a[:1] not in ("r", "u"):
        return a == b
    ta, tb = a.split("|"), b.split("|")
    return len(ta) == len(tb) and all(x == y or (_num(x) is not None and _num(x) == _num(y)) for x, y in zip(ta, tb))


def _num(x):
    try:
        return float(x)
    except ValueError:
        return None


def main():
    work = Path(sys.argv[1])
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 7
    lines = record(n, seed)
    rec, out = work / "ifthen_record.txt", work / "ifthen_cs.txt"
    rec.write_text("\n".join(lines) + "\n")
    subprocess.run(["mono", str(work / "harness.exe"), str(rec), str(out)], check=True)
    cs = out.read_text().splitlines()
    js = subprocess.run(["node", "-e", JS], input="\n".join(lines), capture_output=True, text=True, check=True).stdout.splitlines()
    py = []
    for line in lines:                                   # the server's ATR
        p = line.split("|")
        if p[0] == "N":
            atr = TatankaBiasDailyAtr(int(p[1]))
        elif p[0] == "U":
            atr.Update(int(p[1]), float(p[2]), float(p[3]), float(p[4]))
            py.append(f"u|{atr.Previous!r}|{atr.Current!r}".replace("nan", "NaN"))
    cs_logic, cs_atr = [x for x in cs if not x.startswith("u|")], [x for x in cs if x.startswith("u|")]
    fails = 0
    for name, a, b in (("browser", cs_logic, js), ("server ATR", cs_atr, py)):
        bad = [(i, x, y) for i, (x, y) in enumerate(zip(a, b)) if not same(x, y)]
        if len(a) != len(b) or bad:
            fails += 1
            print(f"PARITY FAIL ({name}): {len(a)} C# lines, {len(b)} port lines, {len(bad)} differ")
            for i, x, y in bad[:8]:
                print(f"  line {i}\n    C#:   {x}\n    port: {y}")
    if fails:
        sys.exit(1)
    rows = sum(int(x[2:]) for x in cs if x.startswith("R|"))
    print(f"PARITY OK: {n} blocks ({rows} rows parsed), {sum(x[0] == 'a' for x in cs)} active rows, "
          f"{sum(x[0] == 'f' for x in cs)} formats, {sum(x[0] == 'w' for x in cs)} wraps, {sum(x[0] == 'c' for x in cs)} color sets, "
          f"{len(cs_atr)} ATR updates; every value equal")


if __name__ == "__main__":
    main()
