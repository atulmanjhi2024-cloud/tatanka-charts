"""Feed plugins: found at start in chartsrv/feeds/plugins/ (built in) and the feeds folder (CHART_FEEDS_DIR, /feeds
in Docker: yours), each listed in Connections under the + of its kind. A file dropped into the feeds folder, changed
or removed is loaded again while the server runs (Market.watch_plugins): no restart. A plugin is Python code running with the
server's rights: only files you put in that folder are loaded, never anything sent from a browser."""
import importlib.util
import inspect
import logging
import math
import re
import sys
import time
from datetime import date
from pathlib import Path

import httpx

from .feeds.fred import NotReady, SeriesData
from .feeds.sdk import FeedPlugin
from .packages import requires_in

log = logging.getLogger(__name__)
BUILTIN_DIR = Path(__file__).parent / "feeds" / "plugins"
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
GROUP = {"futures": "Futures feed", "quotes": "Quotes", "data": "Data"}


def stamps(dirs: list[Path]) -> dict:
    """Every plugin file and its (modified, size): what a reload compares."""
    out = {}
    for d in dirs:
        if d and d.is_dir():
            for path in sorted(d.glob("*.py")):
                if not path.name.startswith("_"):
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    out[path] = (st.st_mtime_ns, st.st_size)
    return out


class Plugins:
    def __init__(self, dirs: list[Path], reserved: set[str], previous: "Plugins | None" = None):
        self.dirs = dirs
        self.types: dict[str, type[FeedPlugin]] = {}   # connection type id -> its class
        self.errors: list[dict] = []                    # [{file, error}]: shown in Connections
        self.files = stamps(dirs)                       # file -> (modified, size)
        self.by_file: dict[Path, list[str]] = {}        # file -> the type ids it gave
        for path, stamp in self.files.items():
            if previous is not None and previous.files.get(path) == stamp and \
                    not any(e.get("path") == str(path) for e in previous.errors) and \
                    not any(pid in self.types for pid in previous.by_file.get(path, [])):   # unchanged: the classes already loaded
                self.by_file[path] = list(previous.by_file.get(path, []))
                self.types.update({pid: previous.types[pid] for pid in self.by_file[path]})
                continue
            self._load(path, reserved)

    def changed(self, other: "Plugins") -> set[str]:
        """The type ids whose class differs between this load and `other` (new, changed, removed)."""
        return {pid for pid in set(self.types) | set(other.types) if self.types.get(pid) is not other.types.get(pid)}

    def _load(self, path: Path, reserved: set[str]):
        mod_name = f"chartsrv_feed_{'b' if path.parent == BUILTIN_DIR else 'u'}_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        except Exception as e:   # a broken plugin must not stop the server
            log.warning("feed plugin %s did not load: %s", path.name, e)
            self.errors.append({"file": path.name, "path": str(path), "error": f"{type(e).__name__}: {e}"[:300],
                                "requires": requires_in(path) if isinstance(e, ImportError) else []})
            sys.modules.pop(mod_name, None)
            return
        found = [c for _, c in inspect.getmembers(mod, inspect.isclass)
                 if issubclass(c, FeedPlugin) and c is not FeedPlugin and c.__module__ == mod_name]
        if not found:
            self.errors.append({"file": path.name, "path": str(path), "error": "no class deriving from FeedPlugin"})
        for cls in found:   # checked here, so its Connections entry (made at start) can't stop the server
            pid = getattr(cls, "ID", None) or (path.stem if len(found) == 1 else f"{path.stem}_{cls.__name__.lower()}")
            err = ("its ID must be 2-31 lowercase letters, digits or _" if not ID_RE.match(pid) else
                   f"ID {pid!r} is taken" if pid in reserved or pid in self.types else
                   f"KIND must be one of {', '.join(GROUP)}" if cls.KIND not in GROUP else
                   "series() is missing" if cls.KIND == "data" and cls.series is FeedPlugin.series else
                   "run() is missing" if cls.KIND != "data" and cls.run is FeedPlugin.run else
                   "REQUIRES: a list of package names, e.g. [\"databento\"]" if not isinstance(cls.REQUIRES, (list, tuple))
                   or not all(isinstance(x, str) for x in cls.REQUIRES) else
                   "TITLE and ABOUT: text" if not isinstance(cls.TITLE, str) or not isinstance(cls.ABOUT or "", str) else
                   "FIELDS: a list of fields, e.g. [secret(\"api_key\", \"API key\")]" if not isinstance(cls.FIELDS, (list, tuple))
                   or not all(isinstance(f, dict) and isinstance(f.get("key"), str) and isinstance(f.get("type"), str) for f in cls.FIELDS)
                   else "")
            if err:
                self.errors.append({"file": path.name, "path": str(path), "error": err})
                continue
            self.types[pid] = cls
            self.by_file.setdefault(path, []).append(pid)
            log.info("feed plugin %s (%s) from %s", pid, cls.TITLE, path)

    def definition(self, pid: str) -> dict:
        """Its Connections entry: On, its own fields, and the symbols it supplies."""
        from .connections import ON, _f
        cls = self.types[pid]
        what = (_f("symbols", "symbols", "Futures", "chart symbols it supplies (Instruments), e.g. RTY, YM; NinjaTrader keeps the rest")
                if cls.KIND == "futures" else
                _f("symbols", "symbols", "Symbols", "watchlist symbols it quotes, e.g. AAPL, BTCUSD; empty: every one it can")
                if cls.KIND == "quotes" else None)
        return {"id": pid, "title": cls.TITLE[:40], "group": GROUP[cls.KIND], "plugin": cls.KIND, "multi": f"Another {cls.TITLE[:40]}",
                "about": (cls.ABOUT or "A feed plugin.")[:600], "requires": list(cls.REQUIRES or [])[:20], "fields": [ON, *[dict(f) for f in cls.FIELDS], *([what] if what else [])]}

    def make(self, cid: str, pid: str, values: dict) -> FeedPlugin:
        cls = self.types[pid]
        vals = {f["key"]: values.get(f["key"], f.get("default", "")) for f in cls.FIELDS}
        syms = [x for x in (values.get("symbols") or "").split(",") if x]
        feed = cls(cid, vals, syms)
        feed.type_id = pid   # which plugin type it is: a reload restarts it when that class changes
        return feed


CODE_RE = re.compile(r"^[A-Z0-9_.]{1,40}$")   # DATA:<code>
DATE_RE = re.compile(r"^\d{4}-\d\d-\d\d$")


def _rows(obs) -> list[tuple[str, float]]:
    """A data plugin's observations by date, without the rows that can't be charted: a missing value (None), one
    that isn't a finite number, a date that isn't YYYY-MM-DD (a datetime, a date object)."""
    out = {}
    for d, v in obs:
        try:
            if DATE_RE.match(d) and date.fromisoformat(d) and math.isfinite(v := float(v)):
                out[d] = v
        except (TypeError, ValueError):
            continue
    return sorted(out.items())


class PluginSeries(SeriesData):
    """A data plugin's series, kept like FRED's: asked again after 3 hours, the stored copy when it can't answer."""

    def __init__(self, store, http, feed: FeedPlugin):
        self.feed, self.name, self.id_re = feed, feed.TITLE, CODE_RE
        self.table = "plugin_" + re.sub(r"[^a-z0-9_]", "_", feed.name)
        super().__init__(store, http)

    def forget(self):   # saved again: the next read asks the source
        with self.store.db:
            self.store.db.execute(f"DELETE FROM {self.table}")

    async def _fetch(self, sid: str):
        try:
            obs, dec = await self.feed.series(self.http, sid)
            obs, dec = _rows(obs), max(0, min(8, int(dec)))
            if not obs:
                raise ValueError("no rows of a YYYY-MM-DD date and a number")
        except Exception as e:
            self.feed.stats["last_error"] = self.feed.scrub(f"{sid}: {e}" if isinstance(e, ValueError) else f"{sid}: {type(e).__name__}")
            if isinstance(e, (ValueError, httpx.HTTPError)):
                raise   # no such data (asked again in 3 hours) / the source down: the stored copy meanwhile
            raise NotReady(self.feed.stats["last_error"]) from None   # the plugin's own error: not taken as missing
        self.feed.stats.update(last_error="", last_msg_ms=int(time.time() * 1000), quotes=self.feed.stats["quotes"] + 1)
        return obs, dec

