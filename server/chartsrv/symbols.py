"""The symbol database behind the search box, in the chart database: every US-listed stock and ETF (Nasdaq Trader's
symbol directory, refreshed daily), futures roots, indices, currency pairs, cryptocurrencies (built in, plus Coinbase's
list, daily) and FRED economic series (popular ones built in; all of them with a free FRED API key). A search that
finds little here asks Yahoo Finance (and FRED) and keeps what they answer, so the database grows as it is used.

The feeds that are connected add theirs, daily and when they are saved: Alpaca's asset list (with its keys: OTC stocks
too), and any feed plugin that lists symbols (Finnhub's US symbols, your own DATA: series). Connections > Data >
Symbol list shows each source and updates them now.

Symbols are the watchlist's: "AAPL", "BRK.B", "ES1!", "EURUSD", "BTCUSD", "SPX", "FRED:DGS10". `yahoo` holds the
Yahoo ticker where the usual mapping (feeds/yahoo.py) would not find it."""
import asyncio
import json
import logging
import re
import sqlite3
import time

import httpx

from . import stopping, symbol_data as D
from .watchlists import SYM_RE, only_futures

log = logging.getLogger(__name__)
LISTINGS = ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
            "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")
COINBASE = "https://api.exchange.coinbase.com"
YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
FRED_SEARCH = "https://api.stlouisfed.org/fred/series/search"
REFRESH_S = 20 * 3600               # the listings change daily
ONLINE_TTL_S = 24 * 3600            # an online search is asked again after a day
OTHER_EXCHANGES = {"A": "NYSE American", "N": "NYSE", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}
STOCK_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")      # BRK.B; not preferreds ($), units or warrants
TYPES = ("future", "stock", "etf", "fund", "index", "forex", "crypto", "economic", "data")
NO_YAHOO_FUTURES = {"FDAX", "FESX", "FGBL", "VX", "M6E", "MET", "MHG"}   # no Yahoo twin: unquoted, never another's

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, exchange TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL, yahoo TEXT NOT NULL DEFAULT '', rank INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_symbols_type ON symbols(type);
"""


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _ok(sym: str) -> bool:
    return bool(SYM_RE.match(sym)) and len(sym) <= 40


# ---- the sources -----------------------------------------------------------------------------------------------------
def builtin(instruments: dict) -> list[tuple]:
    """(symbol, name, type, exchange, yahoo, rank) for what the app knows by itself."""
    rows = []
    for root, (name, ex) in D.FUTURES.items():
        live = root in instruments
        rows.append((f"{root}1!", instruments[root].name if live else name, "future", ex,
                     "" if root in NO_YAHOO_FUTURES else f"{root}=F", 100 if live else 60))
    for root, i in instruments.items():   # chart symbols the list above lacks
        if root not in D.FUTURES:
            rows.append((f"{root}1!", i.name, "future", "", "", 100))
    for sym, (name, y) in D.INDICES.items():
        rows.append((sym, name, "index", "", y, 50))
    for pair in D.fx_pairs():
        a, b = pair[:3], pair[3:]
        rows.append((pair, f"{D.CURRENCIES[a]} / {D.CURRENCIES[b]}", "forex", "FX", "", 40 if b in D.MAJORS else 20))
    for i, (base, name) in enumerate(D.CRYPTO.items()):   # biggest first
        rows.append((f"{base}USD", f"{name} / U.S. Dollar", "crypto", "", f"{base}-USD", 30 + len(D.CRYPTO) - i))
    for sid, (title, freq) in D.FRED.items():
        rows.append((f"FRED:{sid}", title, "economic", f"FRED · {D.FREQ.get(freq, freq)}", "", 25))
    for sid, (title, freq) in D.EIA.items():
        rows.append((f"EIA:{sid}", title, "economic", f"EIA · {D.FREQ.get(freq, freq)}", "", 24))
    return rows


def _clean_name(name: str) -> str:
    return re.sub(r"\s*-?\s*(Common Stock|Ordinary Shares|Common Shares)$", "", name.strip()) or name.strip()


def parse_listing(text: str) -> list[tuple]:
    """Nasdaq Trader's nasdaqlisted.txt or otherlisted.txt (pipe-separated, a header line, a closing "File Creation
    Time" line): every listed stock and ETF that is not a test issue."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    head = lines[0].split("|")
    col = {h.strip(): i for i, h in enumerate(head)}
    sym_col = col.get("Symbol", col.get("ACT Symbol"))
    if sym_col is None or "Security Name" not in col:
        raise ValueError("not a Nasdaq Trader symbol file")
    other = "Exchange" in col
    rows = []
    for ln in lines[1:]:
        p = ln.split("|")
        if len(p) < len(head) or ln.startswith("File Creation Time"):
            continue
        if p[col["Test Issue"]].strip() == "Y" if "Test Issue" in col else False:
            continue
        sym = p[sym_col].strip().upper()
        if not STOCK_RE.match(sym):
            continue
        etf = "ETF" in col and p[col["ETF"]].strip() == "Y"
        ex = OTHER_EXCHANGES.get(p[col["Exchange"]].strip(), "") if other else "NASDAQ"
        rows.append((sym, _clean_name(p[col["Security Name"]]), "etf" if etf else "stock", ex, "", 0))
    return rows


def parse_coinbase(products: list, currencies: list) -> list[tuple]:
    names = {c.get("id"): c.get("name") for c in currencies or [] if isinstance(c, dict)}
    rows = []
    for p in products or []:
        if not isinstance(p, dict) or p.get("quote_currency") != "USD" or p.get("status") != "online" or p.get("trading_disabled"):
            continue
        base = str(p.get("base_currency", "")).upper()
        if re.match(r"^[A-Z0-9]{2,10}$", base) and base not in ("USDT", "USDC", "DAI", "PYUSD"):
            rows.append((f"{base}USD", f"{names.get(base) or base} / U.S. Dollar", "crypto", "Coinbase", f"{base}-USD", 5))
    return rows


INDEX_BY_YAHOO = {y: s for s, (_, y) in D.INDICES.items() if s not in ("US10Y", "US30Y", "US05Y")}


def parse_yahoo_search(doc: dict) -> list[tuple]:
    """Yahoo Finance's search answer, in the watchlist's symbols: stocks, ETFs and funds anywhere, indices, currency
    pairs, cryptocurrencies against the dollar, futures."""
    rows = []
    for q in (doc or {}).get("quotes") or []:
        if not isinstance(q, dict) or not isinstance(q.get("symbol"), str):
            continue
        y, kind = q["symbol"].upper(), q.get("quoteType")
        name = (q.get("longname") or q.get("shortname") or y).strip()
        ex = str(q.get("exchDisp") or q.get("exchange") or "")[:30]
        sym = None
        if kind in ("EQUITY", "ETF", "MUTUALFUND"):
            sym = y.replace("-", ".") if re.match(r"^[A-Z]{1,5}-[A-Z]{1,2}$", y) else y   # BRK-B is BRK.B
            kind = {"EQUITY": "stock", "ETF": "etf", "MUTUALFUND": "fund"}[kind]
        elif kind == "INDEX":
            sym, kind = INDEX_BY_YAHOO.get(y, y), "index"
        elif kind == "CURRENCY" and (m := re.match(r"^([A-Z]{3})([A-Z]{3})?=X$", y)):
            sym, kind = (m.group(1) + m.group(2)) if m.group(2) else "USD" + m.group(1), "forex"
        elif kind == "CRYPTOCURRENCY" and (m := re.match(r"^([A-Z0-9]{2,10})-USD$", y)):
            sym, kind = m.group(1) + "USD", "crypto"
        elif kind == "FUTURE" and (m := re.match(r"^([A-Z0-9]{1,5})=F$", y)):
            sym, kind = m.group(1) + "1!", "future"
        if sym and _ok(sym):
            rows.append((sym, name, kind, ex, y, 1))
    return rows


def parse_fred_search(doc: dict) -> list[tuple]:
    rows = []
    for s in (doc or {}).get("seriess") or []:
        sid = str(s.get("id", "")).upper()
        if re.match(r"^[A-Z0-9_]{1,40}$", sid):
            freq = s.get("frequency_short") or ""
            rows.append((f"FRED:{sid}", str(s.get("title") or sid)[:200], "economic", f"FRED · {D.FREQ.get(freq, freq)}".strip(" ·"),
                         "", min(20, int(s.get("popularity") or 0) // 5)))
    return rows


# ---- the database ----------------------------------------------------------------------------------------------------
class SymbolDB:
    def __init__(self, store, instruments: dict, http: httpx.AsyncClient | None = None, fred_key: str | None = None,
                 online: bool = True):
        self.store, self.db = store, store.db
        self.db.executescript(SCHEMA)
        self.instruments = instruments
        self.on_demand = lambda: {}   # Market: the futures that chart live once opened
        self.http, self.fred_key, self.online = http, fred_key, online
        self.asked: dict[tuple[str, str], float] = {}   # (where, query) -> when it was asked online
        self.yahoo_map: dict[str, str] = {}
        self.feed_sources = lambda: {}   # Market sets it: source name -> async () -> rows, for the feeds connected
        self.errors: dict[str, str] = {}   # source -> why its last refresh failed
        self.due: set[str] = set()         # sources to refresh at once (saved, or Update now)
        self.wake = asyncio.Event()
        self.only_futures: set[str] = set()
        self.on_only_futures = lambda only: None   # Market: every open page hears the new set
        try:
            self._put(builtin(instruments), "builtin", replace=True, override=True)
        except sqlite3.Error as e:   # the database can't be written (a full disk): the server starts all the same
            log.warning("built-in symbols not saved (%s): searching the ones stored", e)
            self._known()

    # -- writing --
    def _put(self, rows: list[tuple], source: str, replace: bool = False, override: bool = False) -> int:
        now = int(time.time() * 1000)
        with self.db:
            if replace:
                self.db.execute("DELETE FROM symbols WHERE source=?", (source,))
            n = self.db.total_changes
            self.db.executemany(f"INSERT OR {'REPLACE' if override else 'IGNORE'} INTO symbols "
                                "(symbol, name, type, exchange, source, yahoo, rank, updated) VALUES (?,?,?,?,?,?,?,?)",
                                [(s, nm[:200], t, ex, source, y, r, now) for s, nm, t, ex, y, r in rows if _ok(s)])
            n = self.db.total_changes - n
        self._known()
        return n

    def _known(self) -> None:
        """What the database knows each symbol as, kept in memory: its type and Yahoo ticker, and the bare roots
        that are futures."""
        self.yahoo_map, self.types, listed = {}, {}, False
        for s, t, y, src in self.db.execute("SELECT symbol, type, yahoo, source FROM symbols"):   # a symbol is its quote's key: ZS1!, ZS
            self.types[s] = t
            if y:
                self.yahoo_map[s] = y
            listed |= src == "nasdaq"
        # a list's bare root written as its future (ZB1!): by the listings once they are in, no stock with that ticker
        only = only_futures(self.types.get if listed else None)
        if only != self.only_futures:   # the listings in (a page opened before charts a bare ZB as ZB1! from now on)
            self.only_futures = only
            self.on_only_futures(only)

    def yahoo_for(self, key: str) -> str | None:
        """The Yahoo ticker the database knows for a watchlist quote key ("ZS1!" the future, "ZS" the stock), if any."""
        return self.yahoo_map.get(key)

    def type_of(self, key: str) -> str | None:
        """What the database knows a watchlist quote key as (future, stock, etf, crypto, ...), if anything."""
        return self.types.get(key)

    def count(self) -> dict:
        return dict(self.db.execute("SELECT type, COUNT(*) FROM symbols GROUP BY type").fetchall())

    def info(self, symbol: str) -> dict | None:
        r = self.db.execute("SELECT symbol, name, type, exchange FROM symbols WHERE symbol=?", (symbol,)).fetchone()
        return self._row(r) if r else None

    def _row(self, r) -> dict:
        root = r[0][:-2] if r[2] == "future" and r[0].endswith("1!") else None
        live = bool(root) and (root in self.instruments or root in self.on_demand())   # charts live (or will, once opened)
        return {"symbol": r[0], "name": r[1], "type": r[2], "exchange": r[3], "live": live}

    # -- searching --
    def query(self, q: str, kind: str = "", limit: int = 50) -> list[dict]:
        """Symbol first (exact, then starting with it), then names starting with it or with a word of it; every word
        typed must match the symbol or the name."""
        q = q.strip()[:60]
        if kind and kind not in TYPES:
            raise ValueError(f"type: one of {', '.join(TYPES)}")
        where, args = [], {"limit": max(1, min(limit, 200))}
        if kind:
            where.append("type IN ('stock', 'fund')" if kind == "stock" else "type = :kind")
            args["kind"] = kind
        if not q:
            sql = f"SELECT symbol, name, type, exchange FROM symbols {'WHERE ' + where[0] if where else ''} " \
                  "ORDER BY rank DESC, symbol LIMIT :limit"
            return [self._row(r) for r in self.db.execute(sql, args)]
        strip = lambda x: re.sub(r"^(FRED|EIA|DATA):", "", x.upper())   # noqa: E731  the source is matched as a prefix
        bare = strip(q)
        for i, w in enumerate(q.split()[:5]):
            W = strip(w)
            args.update({f"s{i}": _like(W) + "%", f"f{i}": "FRED:" + _like(W) + "%", f"e{i}": "EIA:%" + _like(W) + "%",
                         f"d{i}": "DATA:" + _like(W) + "%", f"n{i}": "%" + _like(w) + "%"})
            where.append(f"(symbol LIKE :s{i} ESCAPE '\\' OR symbol LIKE :f{i} ESCAPE '\\' OR symbol LIKE :e{i} ESCAPE '\\'"
                         f" OR symbol LIKE :d{i} ESCAPE '\\' OR name LIKE :n{i} ESCAPE '\\')")
        args.update(q=bare, q1=bare + "1!", qf="FRED:" + bare, qd="DATA:" + bare, qp=_like(bare) + "%", qfp="FRED:" + _like(bare) + "%",
                     qn=_like(q) + "%", qw="% " + _like(q) + "%")
        sql = ("SELECT symbol, name, type, exchange FROM symbols WHERE " + " AND ".join(where) +
               " ORDER BY CASE WHEN symbol IN (:q, :q1, :qf, :qd) THEN 0"
               " WHEN symbol LIKE :qp ESCAPE '\\' OR symbol LIKE :qfp ESCAPE '\\' THEN 1"
               " WHEN name LIKE :qn ESCAPE '\\' OR name LIKE :qw ESCAPE '\\' THEN 2 ELSE 3 END,"
               " rank DESC, length(symbol), length(name), symbol LIMIT :limit")
        return [self._row(r) for r in self.db.execute(sql, args)]

    async def search(self, q: str, kind: str = "", limit: int = 50) -> list[dict]:
        """query(), and when it finds little, Yahoo (and FRED, with a key) asked once a day per query; what they
        answer is kept."""
        rows = self.query(q, kind, limit)
        q = q.strip()
        if not (self.online and self.http) or len(q) < 2:
            return rows
        grew = False
        if self.fred_key and kind in ("", "economic") and sum(r["type"] == "economic" for r in rows) < 8:
            grew |= await self._ask("fred", q, self._fred)
        if kind != "economic" and len(rows) < 5:
            grew |= await self._ask("yahoo", q, self._yahoo)
        return self.query(q, kind, limit) if grew else rows

    async def _ask(self, where: str, q: str, fn) -> bool:
        key = (where, q.lower())
        if time.time() - self.asked.get(key, 0) < ONLINE_TTL_S:
            return False
        self.asked[key] = time.time()
        if len(self.asked) > 5000:
            self.asked = dict(sorted(self.asked.items(), key=lambda kv: kv[1])[-2500:])
        try:
            rows = await fn(q)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.info("%s symbol search failed: %s", where, type(e).__name__)
            self.asked.pop(key, None)
            return False
        return self._put(rows, where) > 0

    async def _yahoo(self, q: str) -> list[tuple]:
        r = await self.http.get(YAHOO_SEARCH, params={"q": q, "quotesCount": 20, "newsCount": 0, "listsCount": 0},
                                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=5)
        r.raise_for_status()
        return parse_yahoo_search(r.json())

    async def _fred(self, q: str) -> list[tuple]:
        r = await self.http.get(FRED_SEARCH, params={"search_text": q, "api_key": self.fred_key, "file_type": "json",
                                                     "limit": 25, "order_by": "popularity", "sort_order": "desc"}, timeout=5)
        r.raise_for_status()
        return parse_fred_search(r.json())

    # -- the daily refresh --
    def refreshed(self) -> dict:
        return self.store.get_setting("symbols_refreshed", {}) or {}

    async def listings_rows(self) -> list[tuple]:
        texts = []
        for url in LISTINGS:
            r = await self.http.get(url, timeout=60)
            r.raise_for_status()
            texts.append(r.text)
        rows = [row for t in texts for row in parse_listing(t)]
        if len(rows) < 3000:   # a cut-off or changed file: keep what is stored
            raise ValueError(f"only {len(rows)} listings")
        return rows

    async def coinbase_rows(self) -> list[tuple]:
        p = await self.http.get(f"{COINBASE}/products", timeout=30, headers={"User-Agent": "tatanka-charts"})
        p.raise_for_status()
        c = await self.http.get(f"{COINBASE}/currencies", timeout=30, headers={"User-Agent": "tatanka-charts"})
        c.raise_for_status()
        rows = parse_coinbase(p.json(), c.json())
        if not rows:
            raise ValueError("no products")
        return rows

    async def refresh_listings(self) -> int:
        return self._put(await self.listings_rows(), "nasdaq", replace=True)

    async def refresh_coinbase(self) -> int:
        return self._put(await self.coinbase_rows(), "coinbase", replace=True)

    def sources(self) -> dict:
        """Every source refreshed daily: name -> async () -> rows (symbol, name, type, exchange, yahoo, rank)."""
        out = {"nasdaq": self.listings_rows, "coinbase": self.coinbase_rows} if self.online and self.http else {}
        return {**out, **self.feed_sources()}

    def status(self) -> list[dict]:
        """Each source: its rows, when it was last refreshed, why it last failed."""
        done, have = self.refreshed(), dict(self.db.execute("SELECT source, COUNT(*) FROM symbols GROUP BY source").fetchall())
        names = ["builtin", *self.sources(), *(k for k in ("yahoo", "fred") if have.get(k))]
        return [{"source": n, "rows": have.get(n, 0), "refreshed": done.get(n), "error": self.errors.get(n, "")} for n in names]

    def refresh_soon(self, names=None):
        """Refresh these sources (all: None) now, not at their daily time."""
        self.due |= set(self.sources()) if names is None else set(names)
        self.wake.set()

    async def refresh(self, name: str, fn) -> int:
        try:
            rows = await fn()
            n = self._put(rows, name, replace=True)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            self.errors[name] = (str(e) if isinstance(e, ValueError) else type(e).__name__)[:200]
            log.warning("symbols: %s refresh failed (%s); next try in an hour", name, self.errors[name])
            return 0
        except Exception as e:   # a plugin's listing: whatever it raises, the other lists go on
            self.errors[name] = type(e).__name__
            log.warning("symbols: %s refresh failed (%s); next try in an hour", name, type(e).__name__, exc_info=True)
            return 0
        self.errors.pop(name, None)
        done = self.refreshed()
        done[name] = time.time()
        self.store.set_setting("symbols_refreshed", done)
        log.info("symbols: %s refreshed (%d rows, %d new)", name, len(rows), n)
        return n

    def _forget_gone(self, sources: dict):
        """A feed no longer connected: its symbols leave the list (the built-in and searched ones stay)."""
        for (src,) in self.db.execute("SELECT DISTINCT source FROM symbols").fetchall():
            if (src == "alpaca" or src.startswith("plugin:")) and src not in sources:
                with self.db:
                    self.db.execute("DELETE FROM symbols WHERE source=?", (src,))
                self._put([], "builtin")   # the type and Yahoo maps again

    async def run(self):
        while True:
            self.wake.clear()
            try:
                sources, done = self.sources(), self.refreshed()
                self._forget_gone(sources)
                for name, fn in sources.items():
                    if name in self.due or time.time() - done.get(name, 0) >= REFRESH_S:
                        stopping.check()   # a stop the list before swallowed (stopping.py): the rest not asked
                        self.due.discard(name)
                        await self.refresh(name, fn)
            except Exception:   # never the end of the daily refresh
                log.exception("symbols: refresh round failed; next try in an hour")
            stopping.check()   # nor the hour's wait
            try:
                await asyncio.wait_for(self.wake.wait(), 3600)
            except asyncio.TimeoutError:
                pass
