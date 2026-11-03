"""The AMT Toolkit's daily levels, per symbol, from where NinjaTrader's Toolkit reads them:

1. "Levels File or Link" (the Toolkit's LevelsFile setting): a file path, or an http(s) link. {root} becomes the
   symbol's root; micros read their parent (MES reads ES). A Windows path under Documents
   (C:\\Users\\<you>\\Documents\\..., its OneDrive\\Documents, %USERPROFILE%\\Documents\\...) reads the Documents folder the
   container mounts; run on Windows without Docker, the server opens any path as it is. With the setting blank,
   Documents\\Tatanka\\levels\\{root}.txt is used if it exists.
2. The symbol's pasted line (the Plan tab; NinjaTrader's "Paste Levels Here"), as TradingView takes it:
   "2026-10-15, pivot, prices..." or the Pivot Level / Resistance Levels / Support Levels block.

A file is re-read when it changes (checked every 5 s). A link is asked at once when the setting is set or changed
(and when the server starts), then every 2 minutes while the market is open, and not while it is shut. A file or
link that fails keeps the last good levels and says why. The Daily levels study, level alerts and /api/levels take
these levels too (levels.py), made again on every change (Market.levels_changed)."""
import logging
import ntpath
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import amt, sessions, stopping

log = logging.getLogger(__name__)
MICRO_PARENT = {"MES": "ES", "MNQ": "NQ", "MCL": "CL", "MGC": "GC"}   # MathEx.LevelsRoot
DEFAULT_FILE = r"%USERPROFILE%\Documents\Tatanka\levels\{root}.txt"
FILE_CHECK_S = 5
LINK_EVERY_MS = 120_000   # a levels link, while the market is open
DOCS_RE = re.compile(r"^(?:%USERPROFILE%|[A-Za-z]:\\Users\\[^\\]+)\\(?:OneDrive[^\\]*\\)?Documents\\(.+)$", re.I)
WINDOWS = os.name == "nt"   # the server runs on Windows itself (no Docker)


def levels_root(sym: str) -> str:
    return MICRO_PARENT.get(sym.upper(), sym.upper())


def local_path(spec: str, docs_dir: Path | None) -> Path | None:
    """A levels file setting as a path this server can open, or None (a Windows path outside Documents)."""
    spec = spec.strip().strip('"')
    if docs_dir is None and WINDOWS:   # not in a container: the path as it is
        return Path(os.path.expanduser(ntpath.expandvars(spec)))
    m = DOCS_RE.match(spec)
    if m:
        return docs_dir / Path(*m.group(1).split("\\")) if docs_dir else None
    if re.match(r"^[A-Za-z]:\\", spec):
        return None
    return Path(os.path.expanduser(spec))


@dataclass
class Source:
    """What a symbol's levels came from, for the Plan tab."""
    origin: str = "none"          # file, link, paste, none
    where: str = ""               # the file or link (a link without its private part)
    text: str = ""                # the levels document as read
    date: datetime | None = None
    read_ms: int | None = None
    error: str = ""
    mtime: float | None = None
    next_ask_ms: int = 0          # a link's next ask (0: at once)
    url: str = ""                 # the link asked


def _redact(url: str) -> str:
    """For logs and the status line: the host and file name, never a path or query that may carry a key."""
    return re.sub(r"(https?://[^/]+/).*(/[^/]*)$", r"\1…\2", re.split(r"[?#]", url, maxsplit=1)[0])


class DailyLevels:
    def __init__(self, market):
        self.m = market
        self.src: dict[str, Source] = {}   # by levels root
        self.paste: dict[str, str] = dict(market.store.get_setting("amt_paste", {}) or {})   # by symbol

    # --- settings ---------------------------------------------------------------------------------------
    def spec(self) -> str:
        return str(self.m.amt.settings_value("LevelsFile") or "").strip()

    def docs_dir(self) -> Path | None:
        d = os.environ.get("CHART_DOCUMENTS_DIR") or "/import/documents"
        return Path(d) if Path(d).is_dir() else None

    def set_paste(self, sym: str, text: str):
        if text.strip():
            amt.parse_levels(text)   # raises ValueError with the Toolkit's message
            self.paste[sym] = text.strip()
        else:
            self.paste.pop(sym, None)
        self.m.store.set_setting("amt_paste", self.paste)

    # --- the levels for a symbol ----------------------------------------------------------------------------
    def current(self, sym: str) -> tuple[list, datetime | None, Source]:
        """(levels, date, source) by NinjaTrader's order: file or link, else the paste."""
        root = levels_root(sym)
        s = self.src.get(root)
        if s and s.text and s.origin in ("file", "link"):
            try:
                levels, date, _ = amt.parse_levels(s.text)
                if levels:
                    return levels, date, s
            except ValueError:
                pass
        if sym in self.paste:
            levels, date, _ = amt.parse_levels(self.paste[sym])
            return levels, date, Source(origin="paste", text=self.paste[sym], date=date, error=s.error if s else "")
        return [], None, Source(origin="none", error=s.error if s else "")

    def status(self, sym: str) -> dict:
        try:
            levels, date, s = self.current(sym)
        except ValueError as e:
            return {"origin": "paste", "error": str(e), "paste": self.paste.get(sym, ""), "count": 0}
        return {"origin": s.origin, "where": s.where, "date": date.strftime("%Y-%m-%d") if date else None,
                "read_ms": s.read_ms, "error": s.error, "count": len(levels), "paste": self.paste.get(sym, ""),
                "spec": self.spec() or DEFAULT_FILE + " (if it exists)"}

    # --- reading files and links -------------------------------------------------------------------------
    async def run(self):
        while True:
            try:
                await self.poll()
            except Exception:
                log.exception("daily levels poll failed")
            await stopping.pause(FILE_CHECK_S)   # not after a stop a link's request swallowed (stopping.py)

    async def poll(self, now_ms: int | None = None):
        now_ms = now_ms or int(time.time() * 1000)
        spec = self.spec()
        roots = sorted({levels_root(s) for s in self.m.instruments})
        changed = set()
        for root in roots:
            target = (spec or DEFAULT_FILE).replace("{root}", root)
            s = self.src.setdefault(root, Source())
            before = (s.text, s.error)
            if re.match(r"^https?://", target, re.I):
                await self._read_link(root, target, s, now_ms)
            else:
                self._read_file(target, s, now_ms, explicit=bool(spec))
            if s.text != before[0]:
                changed.add(root)
            if (s.text, s.error) != before:
                for sym in self.m.instruments:
                    if levels_root(sym) == root:
                        self.m.broadcast({"type": "amt_levels", "symbol": sym, "status": self.status(sym)})
        for sym in self.m.instruments:
            if levels_root(sym) in changed:
                self.m.amt.levels_changed(sym)
        if changed:
            self.m.levels_changed()   # the Daily levels study's and level alerts' too

    def _read_file(self, target: str, s: Source, now_ms: int, explicit: bool):
        docs = self.docs_dir()
        path = local_path(target, docs)
        held = s.origin == "file" and s.where == target   # the text held is this file's, not a link's or another file's
        s.origin, s.where = "file", target
        if path is None:
            s.error = (f"{target} is outside Documents: the server only sees the Documents folder (DOCUMENTS_DIR)."
                       if explicit else "")
            return
        try:
            st = path.stat()
        except OSError:   # read from the mounted Documents, which may not be the folder the path names (OneDrive's)
            seen = " in the Documents folder the server sees (DOCUMENTS_DIR)" if docs in path.parents else ""
            s.error = f"No levels file at {target}{seen}." if explicit else ""
            if not explicit:
                s.text, s.origin = "", "none"
            return
        if held and s.mtime == st.st_mtime and s.text:
            return
        try:
            text = path.read_text(encoding="utf-8-sig")
            levels, date, _ = amt.parse_levels(text)
            if not levels:
                raise ValueError("The levels file is empty.")
        except (OSError, UnicodeError, ValueError) as e:
            s.error = f"{target}: {e}. Keeping the last good levels."
            s.mtime = st.st_mtime
            return
        s.text, s.date, s.read_ms, s.mtime, s.error = text, date, now_ms, st.st_mtime, ""

    async def _read_link(self, root: str, url: str, s: Source, now_ms: int):
        if s.origin != "link" or s.url != url:   # set, changed, or the server just started: asked at once
            s.next_ask_ms = 0
        s.origin, s.where, s.url = "link", _redact(url), url
        if now_ms < s.next_ask_ms:
            return
        groups = [self.m.instruments[x].calendar for x in self.m.instruments if levels_root(x) == root]
        if s.next_ask_ms and not any(sessions.is_open(now_ms, g) for g in groups):
            return   # shut: the last levels stand, and it is asked at the first check once the market opens
        s.next_ask_ms = now_ms + LINK_EVERY_MS
        try:
            r = await self.m.http.get(url, timeout=15, follow_redirects=True)
            if r.status_code in (401, 403, 404, 410):
                s.error = f"The levels link was refused (HTTP {r.status_code}): check the link."
                return
            r.raise_for_status()
            text = r.text
            levels, date, _ = amt.parse_levels(text)
            if not levels:
                raise ValueError("no levels in the answer")
        except Exception as e:   # network, HTTP or parse: keep the last good levels
            s.error = f"Levels link unreachable ({type(e).__name__}): keeping the last good levels."
            return
        if text != s.text:
            s.text, s.date, s.read_ms = text, date, now_ms
        s.error = ""
