"""Watchlists as TradingView keeps them: named lists of symbols in which "###NAME" starts a section, stored in the
settings table and edited from the app. config/watchlists.json is the first-run default.

Symbols are written the TradingView way: "ES1!" is the front-month future of root ES, "AAPL", "EURUSD", an optional
exchange prefix ("CME_MINI:ES1!") and simple formulas ("AAPL+MSFT", "SKEW/VIX"). "FRED:" and "EIA:" are not
exchanges but sources: an economic series ("FRED:DGS10", "EIA:PET.WCESTUS1.W"), so they stay, in formulas too
("FRED:DGS10-FRED:DGS2"). Each needs one quote per plain
symbol in it, keyed by quote_key(): the symbol itself, so a future ("ZS1!", soybeans) and the stock with its root for a
ticker ("ZS", Zscaler) each have their own; a chart future running here owns its bare root ("ES"), as on the chart.

The document has a version, which grows with each save (next_version): the app keeps the newest lists it hears of,
and a save it made on an older version than the server's (another device saved meanwhile) is refused (Stale)."""
import json
import logging
import re
import secrets
import sqlite3
import time
from pathlib import Path

from .symbol_data import FUTURES

log = logging.getLogger(__name__)
SECTION = "###"
MAX_LISTS, MAX_ITEMS, MAX_NAME, MAX_SYM = 100, 2000, 60, 120
SYM_RE = re.compile(r"^[A-Za-z0-9_.:!&^=+\-*/() ]{1,120}$")
PREFIX_RE = re.compile(r"\b(?!FRED:|EIA:|DATA:)[A-Za-z0-9_]+:")   # "CME_MINI:" in "CME_MINI:ES1!"; FRED:, EIA:, DATA: stay
TOKEN_RE = re.compile(r"(?:FRED:|EIA:|DATA:)?[A-Za-z0-9_.!&^=]+")
NUMBER_RE = re.compile(r"^\d+(\.\d+)?$")
FUT_RE = re.compile(r"^([A-Z0-9]{1,6})1!$")                  # continuous front month; a root of 1-6, as config._SYM
TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")        # what a stock's ticker can look like
ONLY_FUTURES = {r for r in FUTURES if not TICKER_RE.match(r)}  # roots that cannot be a ticker: 6E, SR3, M2K
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
FLAGS = {"red", "orange", "green", "blue", "purple"}


def bare(sym: str) -> str:
    """The symbol without exchange prefixes: "CME_MINI:ES1!" -> "ES1!"."""
    return PREFIX_RE.sub("", sym).strip()


def quote_key(token: str, live=()) -> str:
    """The key a plain symbol's quote has: the symbol, "ZS1!" for the future and "ZS" for the stock. live: the chart
    futures running here, whose bare root ("ES") is that future, as on the chart."""
    return f"{token}1!" if token in live else token


def future_root(key: str) -> str | None:
    """A future's root ("ZS" for ZS1!); None for any other symbol."""
    m = FUT_RE.match(key)
    return m.group(1) if m else None


def symbols_in(sym: str) -> list[str]:
    """The plain symbols an item needs quotes for: one, or each one in a formula."""
    return [t for t in TOKEN_RE.findall(bare(sym)) if not NUMBER_RE.match(t)]


def quote_keys(doc: dict, live=()) -> set[str]:
    return {quote_key(t, live) for lst in doc["lists"] for it in lst["items"] if not it.startswith(SECTION)
            for t in symbols_in(it)}


def futures_roots(doc: dict) -> list[str]:
    return sorted({m.group(1) for lst in doc["lists"] for it in lst["items"] if not it.startswith(SECTION)
                   for t in symbols_in(it) if (m := FUT_RE.match(t))})


def only_futures(type_of=None) -> set[str]:
    """The built-in roots no stock has for a ticker. type_of: the symbol database's (symbols.py), once it holds the
    exchanges' listings: every root it knows as nothing else (ZB, YM, RTY; not ZS, Zscaler's, nor NG, NovaGold's).
    Without it, only those that cannot be a ticker (6E, SR3, M2K)."""
    return ONLY_FUTURES | ({r for r in FUTURES if type_of(r) in (None, "future")} if type_of else set())


def _as_future(it: str, only=ONLY_FUTURES, keep=False) -> str:
    """A root that only a future has (only: only_futures()), alone or in a formula ("6E", "CME:SR3", "6E/6B"),
    written as that future ("6E1!"): an older list's bare 6E keeps its quote and chart. A root that is a stock's
    ticker too (ZS) stays. keep: a symbol saved before, which stays as it is if this makes it longer than 120."""
    w = TOKEN_RE.sub(lambda m: f"{m[0]}1!" if m[0] in only and it[m.end():m.end() + 1] != ":" else m[0], it)
    return it if keep and not SYM_RE.match(w) else w


def items_in(doc) -> set[str]:
    """The symbols a document's lists hold (clean()'s stored); none from one that is not a document."""
    lists = doc.get("lists") if isinstance(doc, dict) else None
    return {it for lst in lists if isinstance(lst, dict) and isinstance(lst.get("items"), list) for it in lst["items"]
            if isinstance(it, str)} if isinstance(lists, list) else set()


def _clean_item(it, only=ONLY_FUTURES, stored=()) -> str:
    if not isinstance(it, str):
        raise ValueError("a symbol must be text")
    keep = it in stored   # saved so already: kept if only its roots written as futures make it too long
    it = it.strip()
    if it.startswith(SECTION):
        name = it[len(SECTION):].strip()
        if not name or len(name) > MAX_NAME:
            raise ValueError("a section needs a name of 1-60 characters")
        return SECTION + name
    it = it.upper()   # an exchange prefix and a FRED:, EIA: or DATA: series too: every quote is keyed in capitals
    it = _as_future(it, only, keep)   # checked as it is saved: 6E is 6E1!, so a formula of 119 characters typed can be longer
    if not SYM_RE.match(it) or not symbols_in(it):
        raise ValueError(f"not a symbol: {it[:40]!r}")
    return it


def clean(doc, dropped: list | None = None, only=ONLY_FUTURES, stored=()) -> dict:
    """Validates a whole watchlist document from the app; raises ValueError. dropped: a symbol refused is put there
    and left out instead (load(): lists saved before a rule that refuses it). only: the roots written as their future
    (only_futures()). stored: the symbols saved already (items_in()): one that writing its roots so would make longer
    than 120 characters keeps them as they are, where one typed now is refused, so no saved symbol refuses a save."""
    if not isinstance(doc, dict) or not isinstance(doc.get("lists"), list):
        raise ValueError("expected {lists: [...]}")
    if not 1 <= len(doc["lists"]) <= MAX_LISTS:
        raise ValueError(f"keep 1 to {MAX_LISTS} lists")
    lists, ids = [], set()
    for lst in doc["lists"]:
        if not isinstance(lst, dict):
            raise ValueError("a list must be an object")
        name = str(lst.get("name", "")).strip()
        if not name or len(name) > MAX_NAME:
            raise ValueError("a list needs a name of 1-60 characters")
        lid = str(lst.get("id") or "")
        if not ID_RE.match(lid) or lid in ids:
            lid = new_id(ids)
        ids.add(lid)
        items = lst.get("items", [])
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise ValueError(f"a list holds at most {MAX_ITEMS} symbols")
        kept = []
        for it in items:
            try:
                kept.append(_clean_item(it, only, stored))
            except ValueError:
                if dropped is None:
                    raise
                dropped.append(it)
        lists.append({"id": lid, "name": name, "items": kept})
    flags = doc.get("flags") or {}
    if not isinstance(flags, dict):
        raise ValueError("flags must be an object")
    # a flag's symbol as its item is written: one saved too long for its roots as futures keeps them
    flags = {_as_future(bare(str(k).upper()), only, True): v for k, v in flags.items() if isinstance(v, str) and v in FLAGS and SYM_RE.match(str(k))}
    names = doc.get("names") or {}
    names = {_as_future(str(k)[:MAX_SYM], only): str(v)[:200] for k, v in names.items()} if isinstance(names, dict) else {}
    return {"lists": lists, "flags": flags, "names": names}


class Stale(Exception):
    """A save made on an older version of the lists than the server's: another device saved meanwhile."""


def next_version(last) -> int:
    """The version of the lists' next save: when it is made (ms), one more than the last if the clock has not moved
    on (or was set back). It only grows, across restarts and restores too (load), so the newest lists have the
    highest."""
    return max((last if type(last) is int else 0) + 1, int(time.time() * 1000))


def new_id(taken=()) -> str:
    while (i := "l" + secrets.token_hex(4)) in taken:
        pass
    return i


def load_default(config_dir: Path) -> dict:
    """config/watchlists.json. The old format ({lists: {name: [[sym, IBKR spec], ...]}}) becomes one list with a
    section per old list, futures written TradingView's way."""
    path = config_dir / "watchlists.json"
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"lists": [{"name": "Main", "items": []}]}
    lists = raw.get("lists")
    if isinstance(lists, dict):
        items = []
        for section, entries in lists.items():
            items.append(SECTION + section.upper())
            for sym, spec in entries:
                items.append(f"{sym}1!" if spec.get("secType") in ("CONTFUT", "FUT") else sym)
        lists = [{"name": "Main", "items": items}]
    return clean({"lists": lists, "names": raw.get("names", {})})


# Once (Oct 2026): spot and foreign quotes with no exchange settlement here, as the futures that have one (live from
# NinjaTrader, the change from CME's settlement).
SWAPS = {"BRENT": ("BZ1!", "Brent Crude Oil Last Day Financial Futures"), "PALLADIUM": ("PA1!", "Palladium Futures"),
         "XCUUSD": ("HG1!", "Copper Futures"), "NK2251!": ("NIY1!", "Nikkei/Yen Futures")}
SWAPPED = "watchlists_futures_swap"


def _swap(doc: dict) -> int:
    n = 0
    for lst in doc["lists"]:
        items = []
        for it in lst["items"]:
            if bare(it) in SWAPS:
                new, name = SWAPS[bare(it)]
                doc["names"].setdefault(new, name)
                n += 1
                if new in lst["items"] or new in items:
                    continue   # the future is listed already: the old one goes
                it = new
            items.append(it)
        lst["items"] = items
    return n


def load(store, config_dir: Path) -> dict:
    """The saved lists, with a version above any an open app was given before (lists saved before versions have
    none; a backup restored has an older one): their reconnect's hello is the newest they have heard of. It is
    stored with them, so the next start's is above it with the clock set back too. A symbol saved before a rule that
    refuses it is left out, and lists that cannot be read at all show the default: either way the saved lists stay
    as they are (the log says why) until a list is edited, never replaced by the default."""
    saved = store.get_setting("watchlists")
    if not saved:   # the first run
        doc = {**load_default(config_dir), "version": next_version(None)}
        store.set_setting("watchlists", doc)
        store.set_setting(SWAPPED, True)   # nothing saved before the swap: what the user adds now is kept as it is
        return doc
    dropped, last = [], saved.get("version") if isinstance(saved, dict) else None
    try:
        doc = {**clean(saved, dropped, stored=items_in(saved)), "version": next_version(last)}
        whole = not dropped
        if dropped:
            log.warning("watchlists: %s left out, not a symbol now; the saved lists keep it until a list is edited",
                        ", ".join(repr(it)[:200] for it in dropped))
    except ValueError as e:
        log.error("the saved watchlists cannot be read (%s): the default list is shown, they stay saved until a list "
                  "is edited", e)
        doc, whole = {**load_default(config_dir), "version": next_version(last)}, False
    if whole:   # stored with the version given out (and swapped, once)
        if not store.get_setting(SWAPPED, False):
            _swap(doc)
            _save(store, SWAPPED, True)
        _save(store, "watchlists", doc)
    elif isinstance(saved, dict):   # as they are, with the version given out; swapped once saved whole
        _save(store, "watchlists", {**saved, "version": doc["version"]})
    return doc


def _save(store, key: str, value) -> None:
    """load()'s own writes, on every start. While the database can't be written (a full disk) the start goes on with
    the lists worked out here (the next start writes them): a server that won't start shows no charts at all."""
    try:
        store.set_setting(key, value)
    except sqlite3.Error as e:
        log.warning("watchlists: %s not saved (%s): the lists are shown as they are", key, e)
