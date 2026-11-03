"""Saved templates, the same on every device: chart templates (the chart settings: colors, bars, scale), indicator
templates (which studies and indicators are on, in what order), drawing style templates for each tool, and Tatanka
indicator settings presets, as NinjaTrader's "Save as template". Kept in the settings table, so the daily backups
carry them. What a template holds is the browser's to apply; the server keeps it small and well-formed."""
import json
import re
import time

SETTING = "templates"
KIND_RE = re.compile(r"^(chart|indicators|draw:[a-z]{2,10}|ind:[a-z]{2,10})$")
STUDY_PARTS = ("studies", "inds", "layers")   # what moved from chart templates to indicator templates
NAME_RE = re.compile(r"^[^\x00-\x1f\x7f/\\]{1,40}$")
MAX_PER_KIND = 50
MAX_BYTES = 20_000


def clean_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("a name of 1-40 characters, no slashes")
    return name


class Templates:
    def __init__(self, store):
        self.store = store

    def all(self) -> dict:
        t = self.store.get_setting(SETTING, {}) or {}
        if self._split_chart(t):
            self.store.set_setting(SETTING, t)
        return t

    @staticmethod
    def _split_chart(t: dict) -> bool:
        """Chart templates used to hold the studies and indicators as well: those become an indicator template of the
        same name (an existing one is kept), and the chart template keeps the chart settings."""
        moved = False
        charts = t.get("chart", {})
        for name, e in list(charts.items()):
            d = e.get("data") if isinstance(e, dict) else None
            if not isinstance(d, dict) or not any(k in d for k in STUDY_PARTS):
                continue
            t.setdefault("indicators", {}).setdefault(name, {"data": {k: d[k] for k in STUDY_PARTS if k in d},
                                                             "updated": e.get("updated", 0)})
            rest = {k: v for k, v in d.items() if k not in STUDY_PARTS}
            if rest:
                e["data"] = rest
            else:
                del charts[name]
            moved = True
        if "chart" in t and not t["chart"]:
            del t["chart"]
        return moved

    def put(self, kind: str, name: str, data) -> dict:
        if not KIND_RE.match(kind or ""):
            raise ValueError("kind: chart, indicators, draw:<tool> or ind:<indicator>")
        name = clean_name(name)
        if not isinstance(data, dict) or not data:
            raise ValueError("a template holds an object of settings")
        try:
            size = len(json.dumps(data, separators=(",", ":"), allow_nan=False))   # NaN saved would break every read
        except ValueError:
            raise ValueError("a template holds plain numbers (no NaN or infinity)") from None
        if size > MAX_BYTES:
            raise ValueError("that template is too large")
        t = self.all()
        k = t.setdefault(kind, {})
        if name not in k and len(k) >= MAX_PER_KIND:
            raise ValueError(f"up to {MAX_PER_KIND} templates of a kind")
        k[name] = {"data": data, "updated": int(time.time() * 1000)}
        self.store.set_setting(SETTING, t)
        return k[name]

    def delete(self, kind: str, name: str) -> bool:
        t = self.all()
        if name not in t.get(kind, {}):
            return False
        del t[kind][name]
        if not t[kind]:
            del t[kind]
        self.store.set_setting(SETTING, t)
        return True
