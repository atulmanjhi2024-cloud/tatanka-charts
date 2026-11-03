"""Writes chartsrv/amt_schema.json, the AMT Toolkit's settings as NinjaTrader shows them (group, order, label,
description, type, choices, range), from the original source:

    python tools/gen_amt_schema.py <tatanka-indicators checkout>/ninjatrader/TatankaAmtToolkit.cs

Defaults are not copied: they come from chartsrv.amt.DEFAULTS, and the schema test checks they match the source's
InitializeSettings. Settings that only make sense inside NinjaTrader (levels file, sounds, its Discord hook, the
session-template guard) are left out; this app has its own levels sources (the levels file or link, the levels
line) and alert channels."""
import json
import re
import sys
from pathlib import Path

NT_ONLY = {"AboutVersion", "AboutWebsite", "LevelsFile", "EnableSound", "AlertSound", "EnableDiscord", "DiscordWebhook",
           "tzguard_enable"}
PROP = re.compile(r"^\s*public\s+([\w<>\[\],. ]+?)\s+(\w+)\s*\{")


def parse(src: str) -> list[dict]:
    choices = {k: json.loads("[" + v + "]") for k, v in re.findall(r'\{"(\w+)", new string\[\] \{([^}]*)\}\}', src)}
    out, attrs = [], []
    for ln in src.splitlines():
        s = ln.strip()
        m = PROP.match(ln.replace(s[:s.rfind("]") + 1], "") if s.startswith("[") else ln)
        if s.startswith("[") and not m:
            attrs.append(s)
            continue
        if m:
            a = " ".join(attrs + ([s[:s.rfind("]") + 1]] if s.startswith("[") else []))
            d = re.search(r'Display\((.*?)\)\]', a)
            if d and "Browsable(false)" not in a:
                t = d.group(1)
                g = lambda k: (re.search(k + r'\s*=\s*"((?:[^"\\]|\\.)*)"', t) or [None, None])[1]
                r = re.search(r"Range\(([^,]+),\s*([^)]+)\)", a)
                typ, name = m.group(1).strip(), m.group(2)
                kind = {"bool": "bool", "int": "int", "double": "float", "string": "str"}.get(typ, "color" if "Brush" in typ else typ)
                item = {"name": name, "group": g("GroupName"), "order": int(re.search(r"Order\s*=\s*(\d+)", t).group(1)),
                        "label": g("Name"), "type": kind}
                if g("Description"):
                    item["description"] = g("Description").replace('\\"', '"')
                if name in choices:
                    item["choices"] = choices[name]
                if r:
                    lo, hi = (x.strip() for x in r.groups())
                    num = lambda x: None if "MaxValue" in x or "MinValue" in x else float(x) if "." in x else int(x)
                    item["min"], item["max"] = num(lo), num(hi)
                if name not in NT_ONLY:
                    out.append(item)
        attrs = []
    rules(src, {x["name"]: x for x in out})
    return sorted(out, key=lambda x: (int(x["group"].split(".")[0]) if x["group"][0].isdigit() else 99, x["order"]))


def rules(src: str, by_name: dict):
    """ValidateSettings: bounds (inclusive), finite numbers and CT session strings, onto each setting. Its three
    cross-field rules are written out in chartsrv.amt_hub."""
    body = src[src.index("private void ValidateSettings(Settings s) {"):]
    body = body[:body.index("\n}")]
    num = lambda x: float(x) if "." in x else int(x)
    for k, op, v in re.findall(r"if \(s\.(\w+) ([<>]) ([-0-9.]+)\) throw", body):
        if k in by_name:
            by_name[k]["min" if op == "<" else "max"] = num(v)
    for k, lo, k2, hi in re.findall(r"if \(s\.(\w+) < ([-0-9.]+) \|\| s\.(\w+) > ([-0-9.]+)\) throw", body):
        if k in by_name and k == k2:
            by_name[k]["min"], by_name[k]["max"] = num(lo), num(hi)
    for k in re.findall(r"if \(!MathEx\.Valid\(s\.(\w+)\)\) throw", body):
        if k in by_name:
            by_name[k]["finite"] = True
    for k in re.findall(r"MathEx\.InSession\(new DateTime\(2026, 1, 5\), s\.(\w+)\);", body):
        if k in by_name:
            by_name[k]["session"] = True


if __name__ == "__main__":
    items = parse(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(__file__).resolve().parents[1] / "chartsrv" / "amt_schema.json"
    out.write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(items)} settings in {len({x['group'] for x in items})} groups -> {out}")
