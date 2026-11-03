"""Writes the settings of the NinjaTrader Daily SMA x4 indicator into server/chartsrv/nt_schema.json (key dsma), read
from its source (server/tests/dsma_csharp, unchanged): every browsable property with its [Display] name, group, order
and description, its [Range], its type (the label size enum becomes choices) and its default from SetDefaults; and
the four plots from AddPlot, whose colors and dash styles NinjaTrader's Plots section edits (their width is the
Line Width property: Configure sets every plot's width to it).

    python tools/gen_dsma_schema.py"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "server" / "tests" / "dsma_csharp" / "TatankaDailySMAx4.cs"
OUT = ROOT / "server" / "chartsrv" / "nt_schema.json"
BRUSHES = {"Yellow": "#ffff00", "Blue": "#0000ff", "Red": "#ff0000", "White": "#ffffff"}   # WPF's named brushes


def parse(text: str) -> dict:
    text = text.replace("\r", "")
    title = re.search(r'Name = "([^"]+)";', text)[1]
    enums = {m[1]: [x.strip() for x in m[2].split(",")] for m in re.finditer(r"public enum (\w+)\s*\{([^}]*)\}", text)}
    body = text[text.index("State.SetDefaults"):text.index("State.Configure")]
    defaults = dict(re.findall(r"^\s*([A-Z]\w*) = ([^;]+);", body, re.MULTILINE))
    out = []
    # a property's attributes: the bracket groups right before it, one per line
    for m in re.finditer(r"((?:^[ \t]*\[[^\n]*\]\n)+)[ \t]*public (\w+) (\w+) \{ get; set; \}", text, re.MULTILINE):
        attrs, typ, name = m.groups()
        if "Browsable(false)" in attrs or "Display(" not in attrs:
            continue
        d = dict(re.findall(r'(\w+) = "([^"]*)"', attrs))
        x = {"name": name, "group": d["GroupName"], "order": int(re.search(r"Order = (\d+)", attrs)[1]), "label": d["Name"]}
        if "Description" in d:
            x["description"] = d["Description"]
        raw = defaults[name]
        if typ in enums:
            x.update(type="choice", choices=enums[typ], default=raw.split(".")[-1])
        elif typ == "bool":
            x.update(type="bool", default=raw == "true")
        elif typ == "int":
            x.update(type="int", default=int(raw))
        else:
            raise ValueError(f"{name}: type {typ}")
        rng = re.search(r"Range\((-?\d+), (-?\d+|int\.MaxValue)\)", attrs)
        if rng:
            x["min"], x["max"] = int(rng[1]), None if rng[2] == "int.MaxValue" else int(rng[2])
        out.append(x)
    plots = re.findall(r'AddPlot\(new Stroke\(Brushes\.(\w+), ([\d.]+)\), PlotStyle\.Line, "(\w+)"\);', body)
    for i, (brush, width, name) in enumerate(plots):
        out.append({"name": f"Plot{name}", "group": "Plots", "order": i + 1, "label": name, "type": "stroke", "nowidth": True,
                    "description": "Its width is Line Width (Style).",
                    "default": {"color": BRUSHES[brush], "dash": "Solid", "width": float(width)}})
    return {"title": title, "version": "", "settings": out}


def main():
    schema = json.loads(OUT.read_text())
    schema["dsma"] = parse(SRC.read_text())
    print(f"dsma: {schema['dsma']['title']}, {len(schema['dsma']['settings'])} settings")
    OUT.write_text(json.dumps(schema, indent=1) + "\n")


if __name__ == "__main__":
    main()
