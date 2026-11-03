"""Settings for the Tatanka indicators other than the AMT Toolkit (TPO Profile, Gap Analysis, 1hr 21 EMA, If/Then,
Level Lines, the volume profiles, Daily SMA x4), as NinjaTrader's indicator dialog has them: nt_schema.json
(tools/gen_nt_schema.py) holds every property with NinjaTrader's default; the app stores only what differs, in the
settings table, for every device.

A few properties belong to one chart's symbol in NinjaTrader (the If/Then block, Level Lines' levels and range):
here they are kept per symbol."""
import json
import math
import re
from pathlib import Path

SCHEMA: dict = json.loads((Path(__file__).with_name("nt_schema.json")).read_text(encoding="utf-8"))
PER_SYMBOL = {
    "ifthen": {"RowsBlock", "InstrumentLabel"},   # RowsBlock is the block /api/ifthen keeps (settings key "ifthen")
    "lines": {"RangeTop", "RangeBottom", "CustomLevels"},
}
# Where this app's engines are narrower than NinjaTrader's (which takes any Int32).
LIMITS = {"tpo": {"XOffsetBars": (-500, 500)}, "hema": {"HtfPeriod": (1, 1440), "EmaLength": (1, 500)},
          "dsma": {f"SMA{i}Length": (1, 1000) for i in range(1, 5)}}   # NinjaTrader loads 1000 daily bars
DASHES = {"Solid", "Dash", "Dot", "DashDot", "DashDotDot"}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SESSION_RE = re.compile(r"^\s*\d{3,4}\s*-\s*\d{3,4}\s*$")   # NinjaTrader also takes 830-1500 and spaces
MAX_TEXT = 20_000


def _apply_limits():
    for key, limits in LIMITS.items():
        for x in SCHEMA[key]["settings"]:
            if x["name"] in limits:
                x["min"], x["max"] = limits[x["name"]]


_apply_limits()


def by_name(key: str) -> dict:
    return {x["name"]: x for x in SCHEMA[key]["settings"]}


def defaults(key: str) -> dict:
    return {x["name"]: x["default"] for x in SCHEMA[key]["settings"]}


def _bad(x, why):
    return ValueError(f"{x['label']}: {why}")


def clean_value(x: dict, v):
    t = x["type"]
    if t == "bool":
        if not isinstance(v, bool):
            raise _bad(x, "on or off")
        return v
    if t in ("int", "float"):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise _bad(x, "a number")
        if t == "int":
            if v != int(v):
                raise _bad(x, "a whole number")
            v = int(v)
        lo, hi = x.get("min"), x.get("max")
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            raise _bad(x, f"{lo if lo is not None else '…'} to {hi if hi is not None else '…'}")
        return v
    if t == "choice":
        if v not in x["choices"]:
            raise _bad(x, "one of " + ", ".join(map(str, x["choices"])))
        return v
    if t in ("str", "text"):
        if not isinstance(v, str) or len(v) > (MAX_TEXT if t == "text" else 200):
            raise _bad(x, "text, not too long")
        if x["name"] == "SessionTime" and not SESSION_RE.match(v):
            raise _bad(x, "HHmm-HHmm, e.g. 0830-1500")
        return v
    if t == "color":
        if v == "transparent" and (x.get("default") == "transparent" or x.get("none")):   # NinjaTrader's Brushes.Transparent
            return v
        if not isinstance(v, str) or not COLOR_RE.match(v):
            raise _bad(x, "a #rrggbb color")
        return v.lower()
    if t == "stroke":
        if not isinstance(v, dict) or not COLOR_RE.match(str(v.get("color", ""))) or v.get("dash") not in DASHES:
            raise _bad(x, "a color, a dash style and a width")
        w = v.get("width")
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not 0.5 <= w <= 10:
            raise _bad(x, "width 0.5 to 10")
        return {"color": v["color"].lower(), "dash": v["dash"], "width": float(w)}
    if t == "font":
        size = v.get("size") if isinstance(v, dict) else None
        if isinstance(size, bool) or not isinstance(size, (int, float)) or not 6 <= size <= 48:
            raise _bad(x, "a size of 6 to 48")
        fam = str(v.get("family") or "Arial")[:40]
        return {"family": fam, "size": int(size)}
    raise _bad(x, "unknown type")


def clean(key: str, overrides) -> dict:
    """Only real settings, valid values and ones that differ from NinjaTrader's default."""
    if key not in SCHEMA:
        raise ValueError(f"no indicator {key!r}")
    if not isinstance(overrides, dict):
        raise ValueError("expected {setting: value}")
    s, out = by_name(key), {}
    for name, v in overrides.items():
        if name not in s or name in PER_SYMBOL.get(key, ()):
            raise ValueError(f"unknown setting {name!r}")
        v = clean_value(s[name], v)
        if v != s[name]["default"]:
            out[name] = v
    return out


def clean_symbol(key: str, values) -> dict:
    if key not in SCHEMA:
        raise ValueError(f"no indicator {key!r}")
    allowed = PER_SYMBOL.get(key, set())
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValueError(f"per-symbol settings of {key}: {', '.join(sorted(allowed)) or 'none'}")
    s = by_name(key)
    return {k: clean_value(s[k], v) for k, v in values.items()}


class IndicatorSettings:
    def __init__(self, store):
        self.store = store
        self.overrides: dict = store.get_setting("ind_settings", {}) or {}
        self.symbols: dict = store.get_setting("ind_symbol", {}) or {}

    def snapshot(self) -> dict:
        return {"schema": SCHEMA, "overrides": self.overrides, "symbols": self.symbols,
                "per_symbol": {k: sorted(v) for k, v in PER_SYMBOL.items()}}

    def put(self, key: str, overrides) -> dict:
        self.overrides[key] = clean(key, overrides)
        self.store.set_setting("ind_settings", self.overrides)
        return self.overrides[key]

    def put_symbol(self, key: str, symbol: str, values, now_ms: int = 0) -> dict:
        vals = clean_symbol(key, values)
        if key == "ifthen" and "RowsBlock" in vals:   # the same store the chart's If/Then reads
            blocks = self.store.get_setting("ifthen", {}) or {}
            blocks[symbol] = {"rows": vals.pop("RowsBlock"), "updated": now_ms}
            self.store.set_setting("ifthen", blocks)
        d = self.defaults_for(key)
        cur = {**self.symbols.get(key, {}).get(symbol, {}), **vals}
        cur = {k: v for k, v in cur.items() if v != d[k] or k == "InstrumentLabel"}
        self.symbols.setdefault(key, {})[symbol] = cur
        if not cur:
            del self.symbols[key][symbol]
        self.store.set_setting("ind_symbol", self.symbols)
        return cur

    @staticmethod
    def defaults_for(key: str) -> dict:
        return defaults(key)

    def value(self, key: str, name: str, symbol: str | None = None):
        if key == "ifthen" and name == "RowsBlock" and symbol is not None:
            return (self.store.get_setting("ifthen", {}) or {}).get(symbol, {}).get("rows", defaults(key)[name])
        if symbol is not None and name in self.symbols.get(key, {}).get(symbol, {}):
            return self.symbols[key][symbol][name]
        return self.overrides.get(key, {}).get(name, defaults(key)[name])
