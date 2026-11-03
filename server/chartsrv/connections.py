"""Connections: the data feeds, data keys and alert channels, set in the app (the dot at the top right) or in .env.
A value saved in the app wins over .env, and clearing it goes back to .env. Saving one applies it at once: its feed
restarts, the rest keep running.

Secrets (keys, the add-on token, webhook, ntfy and heartbeat URLs) are kept in the server's database, never sent
back to a browser (only whether one is set, and where from) and never logged. The trade feed stays as .env starts
it when that is the simulator, so invented and real trades never mix.

Each kind (futures feed, quotes, data, alerts and Discord, app) is a list: the main connections are always on it,
the others once they are in use or added with its +. Alert and chart-post channels can be more than one of a kind
(discord-2, ntfy-3, discord_charts-2): those extra ones live only here, with a name, never in .env."""
import asyncio
import logging
import os
import re
import time
from dataclasses import replace
from urllib.parse import urlsplit

import httpx

from . import packages, setup
from .backup import files
from .branding import DEFAULTS as BRAND_DEFAULTS
from .config import instruments_state
from .feeds import quote_feed_names
from .feeds.ninjatrader import ADDON_VERSION
from .feeds.yahoo import UA, quote_of

log = logging.getLogger(__name__)

SETTING = "connections"
QUOTE_IDS = ("ibkr", "alpaca", "coinbase", "yahoo")
FEED_OF = {"ninjatrader": ("ninjatrader",), "ibkr": ("ibkr",), "alpaca": ("alpaca",),
           "coinbase": ("coinbase",), "yahoo": ("yahoo",)}
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_\-.]{8,200}$")
DISCORD_RE = re.compile(r"^https://(ptb\.|canary\.)?discord(app)?\.com/api/webhooks/\d+/[\w-]+$")
EXTRA_RE = re.compile(r"^([a-z][a-z0-9_]{1,30})-([2-9]|1[0-2])$")   # another one of a kind (discord-2), up to 12
NAME_RE = re.compile(r"^[^<>\x00-\x1f]{0,40}$")
# a new card's field -> the IBKR quotes card's, when that one is set (here or in .env): TWS on 7496 there, 7496 here too
START = {"ibkr_trades": {"host": "ibkr_host", "port": "ibkr_port"}}


def _f(key, kind, label, note="", **kw) -> dict:
    return {"key": key, "type": kind, "label": label, "note": note, **kw}


ON = _f("enabled", "bool", "On")
DEFS = [
    {"id": "ninjatrader", "title": "NinjaTrader", "group": "Futures feed", "main": True,
     "about": "Live futures trades from the TatankaChartFeed add-on, history to fill gaps, futures quotes for the "
              "watchlists and your fills and positions. Read-only: it never places an order.",
     "fields": [ON, _f("nt_token", "secret", "Token", "Token in TatankaChartFeed.cs; blank in both is allowed")]},
    {"id": "ibkr", "main": True, "title": "Interactive Brokers", "group": "Quotes",
     "about": "Watchlist quotes from IB Gateway or TWS with the API on (read-only). Stocks, futures, indices, FX. Live "
              "quotes need IBKR market data subscriptions; delayed ones are free.",
     "fields": [ON, _f("ibkr_host", "host", "Host", "host.docker.internal from Docker; 127.0.0.1 otherwise"),
                _f("ibkr_port", "int", "Port", "Gateway 4001 (paper 4002), TWS 7496 (paper 7497)", min=1, max=65535),
                _f("ibkr_client_id", "int", "Client ID", "any number no other API app uses", min=0, max=999999),
                _f("ibkr_market_data_type", "choice", "Data", options=[[1, "Live"], [3, "Delayed (free)"]])]},
    {"id": "alpaca", "main": True, "title": "Alpaca", "group": "Quotes",
     "about": "Real-time U.S. stock and ETF quotes. Free account at alpaca.markets (paper trading is enough, no "
              "deposit); keys from its dashboard, API Keys. Market data only.",
     "fields": [ON, _f("alpaca_key_id", "secret", "API key ID"), _f("alpaca_secret", "secret", "Secret key"),
                _f("alpaca_feed", "choice", "Data", "IEX is free; SIP (every exchange) needs a paid data plan",
                   options=[["iex", "IEX (free)"], ["sip", "SIP (paid)"]])]},
    {"id": "coinbase", "main": True, "title": "Coinbase", "group": "Quotes",
     "about": "Real-time crypto quotes from Coinbase's public feed. No account.", "fields": [ON]},
    {"id": "yahoo", "main": True, "title": "Yahoo Finance", "group": "Quotes",
     "about": "Backup quotes for everything the others lack (stocks, indices, FX, crypto, futures). No account. Can "
              "lag: futures about 10 minutes. Charts of symbols with no live feed use its history either way.",
     "fields": [ON, _f("yahoo_poll_s", "int", "Every", "seconds", min=5, max=300)]},
    {"id": "fred", "main": True, "title": "FRED", "group": "Data",
     "about": "Economic series: yields, rates, inflation, jobs. Works without a key; a free key "
              "(fred.stlouisfed.org, My Account, API Keys) lets the symbol search find every FRED series.",
     "fields": [_f("fred_api_key", "secret", "API key", "optional")]},
    {"id": "eia", "main": True, "title": "EIA", "group": "Data",
     "about": "Weekly petroleum and natural gas data (crude and Cushing stocks, storage). Needs a free key from "
              "eia.gov/opendata.", "fields": [_f("eia_api_key", "secret", "API key")]},
    {"id": "push", "title": "App notifications", "group": "Alerts and Discord", "main": True, "push": True,
     "about": "Alerts as notifications from this app itself, on each phone or PC that turns them on: iPhone (iOS 16.4+) "
              "from the app added to the Home Screen, Android, Windows or Mac from the app installed from Edge or Chrome. "
              "Needs the https:// address (tailscale serve).", "fields": []},
    {"id": "discord", "title": "Discord", "group": "Alerts and Discord", "main": True, "multi": "Another Discord channel",
     "about": "Alerts to a Discord channel webhook (a private channel).",
     "fields": [_f("discord_webhook", "secret", "Webhook URL", "channel, Edit, Integrations, Webhooks, Copy URL")]},
    {"id": "discord_charts", "title": "Discord: chart posts", "group": "Alerts and Discord", "main": True, "multi": "Another chart-post channel",
     "about": "Where Post to Discord sends a chart picture (the camera button or the chart's menu), with your caption. "
              "Its own webhook, so posts go to the channel you choose, not the alert channel.",
     "fields": [_f("discord_charts_webhook", "secret", "Webhook URL", "channel, Edit, Integrations, Webhooks, Copy URL")]},
    {"id": "ntfy", "title": "ntfy", "group": "Alerts and Discord", "multi": "Another ntfy topic",
     "about": "Phone push through the ntfy app (ntfy.sh or your own server). Use a long topic nobody can guess.",
     "fields": [_f("ntfy_url", "secret", "Topic URL", "e.g. https://ntfy.sh/a-long-random-topic")]},
    {"id": "heartbeat", "title": "Heartbeat", "group": "Alerts and Discord",
     "about": "Pinged every 30 s while the feed is healthy, so a service like healthchecks.io tells you when the PC "
              "or the server is down.", "fields": [_f("heartbeat_url", "secret", "Ping URL")]},
]
DEFS.insert(next(i for i, d in enumerate(DEFS) if d["id"] == "eia") + 1, {"id": "symbols", "title": "Symbol list", "group": "Data", "main": True, "symbol_list": True, "fields": [],
             "about": "What the symbol search finds: every US-listed stock and ETF (Nasdaq's list), Coinbase's crypto, futures, "
                      "indices, forex, FRED, plus what the connected feeds list (Alpaca with its keys, Finnhub, your DATA: "
                      "series). Each source is asked again daily and when its connection is saved."})
DEFS.append({"id": "login", "title": "Login", "group": "App", "main": True, "login": True, "fields": [],
             "about": "A password for the app. Each phone and PC signs in once and stays signed in (renewed as you use "
                      "it). Without one, a fresh install lets only the PC itself in; an older one, or one with "
                      "CHART_REMOTE_NEEDS_PASSWORD=0 in .env, lets in anyone who reaches the app: Tailscale is then the "
                      "only lock. Forgot it? On the PC: docker compose exec chart python -m chartsrv.auth off"})
DEFS.append({"id": "backup", "title": "Backups", "group": "App", "main": True, "backup": True, "fields": [],
             "about": "Your alerts, drawings, watchlists, workspaces, settings and these keys, every day, the newest 30 kept "
                      "(market data is not in them: it is large and comes back from NinjaTrader or an import). On the PC "
                      "in the backups folder (BACKUP_DIR in .env). They hold your keys: keep that folder private."})
DEFS.append({"id": "instruments", "title": "Instruments", "group": "App", "main": True, "instruments": True, "fields": [],
             "about": "The futures this server charts from its own trades (the NinjaTrader feed): symbol, tick size, "
                      "point value, regular hours, holidays and how it rolls (Edit > Contract: by volume unless set), on "
                      "CME Globex hours (17:00-16:00 CT). A future you open (RTY, ZN...) is added here by itself. Save "
                      "applies: an addition or a roll choice at once; another change or a removal restarts the server "
                      "and every page comes back in a few seconds."})
BRAND_ABOUT = ("The name and logo in the header, the sign-in and the browser tab, and \"Powered by Tatanka Charts\" "
               "on chart pictures. Applies on every device at once.")
DEFS.append({"id": "branding", "title": "Branding", "group": "App", "main": True, "branding": True, "fields": [],
             "about": BRAND_ABOUT})
DEFS.append({"id": "updates", "title": "Updates", "group": "App", "main": True,
             "about": "Checks every 6 hours for a newer version and lists what changed; update.cmd on the PC takes it. "
                      "A private repository needs a read-only token: github.com > Settings > Developer settings > "
                      "Fine-grained tokens > Generate: only this repository, Contents read-only. Test checks now.",
             "fields": [_f("update_repo", "text", "Repository", "owner/name on GitHub"),
                        _f("github_token", "secret", "GitHub token", "only for a private repository")]})
BY_ID = {d["id"]: d for d in DEFS}


def type_of(cid: str) -> str:
    """A connection's type: its id, or for another one of a kind (discord-2, ibkr_trades-2) that kind."""
    m = EXTRA_RE.match(cid)
    return m.group(1) if m and BY_ID.get(m.group(1), {}).get("multi") else cid


def builtin_ids() -> set[str]:
    """The connection ids of the app's own (a plugin may not take one)."""
    return {d["id"] for d in DEFS if not d.get("plugin")} | {"plugin_errors"}


def register_plugins(plugins) -> None:
    """The feed plugins found at start, each at the end of its kind's list; again on a new start (tests)."""
    DEFS[:] = [d for d in DEFS if not d.get("plugin") and d["id"] != "plugin_errors"]
    for pid in plugins.types:
        d = plugins.definition(pid)
        at = max(i for i, x in enumerate(DEFS) if x["group"] == d["group"]) + 1
        DEFS.insert(at, d)
    if plugins.errors:
        DEFS.insert(max(i for i, x in enumerate(DEFS) if x["group"] == "Futures feed") + 1, {
            "id": "plugin_errors", "title": "Feed plugins that did not load", "group": "Futures feed", "main": True,
            "unavailable": True, "fields": [], "requires": sorted({r for e in plugins.errors for r in e.get("requires", [])}),
            "about": " · ".join(f"{e['file']}: {e['error']}" for e in plugins.errors)[:1500]})
    BY_ID.clear()
    BY_ID.update({d["id"]: d for d in DEFS})


def _need(specs: list[str], installer) -> tuple[str, str]:
    """The status of a card whose plugin needs packages not installed yet."""
    what = f"the Python package{'s' if len(specs) > 1 else ''} {', '.join(specs)}"
    st = {installer.state(x) for x in specs} if installer else set()
    if "installing" in st:
        return "warn", f"Installing {what}… (a minute or two)"
    if "failed" in st:
        return "error", f"Installing {what} failed: {next(installer.failed[x] for x in specs if x in installer.failed)}"
    return "error", f"Needs {what}: Install below"


def plugin_symbols(v) -> str:
    """"rty, YM ,es" -> "RTY,YM,ES"; ValueError on anything that isn't a symbol."""
    if not isinstance(v, str) or len(v) > 400:
        raise ValueError("symbols: comma-separated, e.g. RTY, YM")
    out = [x.strip().upper() for x in v.split(",") if x.strip()]
    bad = [x for x in out if not re.match(r"^[A-Z0-9][A-Z0-9.\-_/]{0,19}$", x)]
    if bad:
        raise ValueError(f"symbols: {bad[0]!r} is not a symbol")
    return ",".join(dict.fromkeys(out))


def _app_name(market) -> str:   # as Branding sets it: "<name> Charts"
    b = getattr(market, "branding", None)
    return f"{b.get()['name']} Charts" if b is not None else "Tatanka Charts"


def _secret_ok(key: str, v: str, f: dict | None = None) -> bool:
    if f and f.get("plugin"):   # a plugin's key or password: anything printable
        return bool(re.match(r"^[^\x00-\x1f]{1,500}$", v))
    if key in ("discord_webhook", "discord_charts_webhook"):
        return bool(DISCORD_RE.match(v))
    if key in ("ntfy_url", "heartbeat_url"):
        u = urlsplit(v)
        try:
            httpx.URL(v)   # what sending will need (a port that isn't a number, say)
        except httpx.InvalidURL:
            return False
        return u.scheme in ("http", "https") and bool(u.netloc) and not re.search(r"\s", v)
    if key == "nt_token":
        return bool(re.match(r"^[^\s|]{1,200}$", v))
    return bool(KEY_RE.match(v))


def clean(cid: str, body: dict) -> dict:
    """The fields of one connection as sent (ValueError if any is wrong). None: back to .env. "" for a secret: none,
    whatever .env says."""
    d = BY_ID.get(type_of(cid))
    if not d or d.get("unavailable"):
        raise ValueError(f"no connection {cid!r}")
    if not isinstance(body, dict):
        raise ValueError("expected {field: value}")
    fields = {f["key"]: f for f in d["fields"]}
    out = {}
    for k, v in body.items():
        f = fields.get(k)
        if k == "name" and d.get("multi"):   # a channel's own name, as the lists show it
            if v is not None and (not isinstance(v, str) or not NAME_RE.match(v.strip())):
                raise ValueError("Name: plain text, up to 40 characters")
            out[k] = (v or "").strip() or None
            continue
        if f is None:
            raise ValueError(f"{d['title']} has no setting {k!r}")
        if v is None:
            out[k] = None
        elif f["type"] == "bool":
            if not isinstance(v, bool):
                raise ValueError(f"{f['label']}: true or false")
            out[k] = v
        elif f["type"] == "int":
            if isinstance(v, bool) or not isinstance(v, int) or not f["min"] <= v <= f["max"]:
                raise ValueError(f"{f['label']}: a whole number {f['min']}-{f['max']}")
            out[k] = v
        elif f["type"] == "choice":
            if v not in [o[0] for o in f["options"]]:
                raise ValueError(f"{f['label']}: one of {', '.join(str(o[0]) for o in f['options'])}")
            out[k] = v
        elif f["type"] == "host":
            if not isinstance(v, str) or not HOST_RE.match(v.strip()):
                raise ValueError(f"{f['label']}: a host name or IP address")
            out[k] = v.strip()
        elif f["type"] == "text":
            if not isinstance(v, str) or not re.match(r"^[^\x00-\x1f<>]{0,200}$", v.strip()):
                raise ValueError(f"{f['label']}: plain text, up to 200 characters")
            out[k] = v.strip()
        elif f["type"] == "symbols":
            out[k] = plugin_symbols(v)
        else:   # secret
            if not isinstance(v, str) or len(v) > 500:
                raise ValueError(f"{f['label']}: text")
            v = v.strip()
            if v and not _secret_ok(k, v, f):
                raise ValueError(f"{f['label']} doesn't look right" + (
                    ": a Discord webhook URL (https://discord.com/api/webhooks/...)" if k.startswith("discord") else ""))
            out[k] = v
    return out


def build(base, saved: dict):
    """The settings with the app's saved values over .env's."""
    ch = {}
    for d in DEFS:
        if d.get("plugin"):   # its values are its own (Plugins.make), not settings
            continue
        vals = saved.get(d["id"]) or {}
        for f in d["fields"]:
            if f["key"] != "enabled" and vals.get(f["key"]) is not None:
                v = vals[f["key"]]
                ch[f["key"]] = (v or None) if f["type"] == "secret" else v
    quotes = quote_feed_names(base)
    for qid in QUOTE_IDS:
        on = (saved.get(qid) or {}).get("enabled")
        if on is True and qid not in quotes:
            quotes.append(qid)
        elif on is False and qid in quotes:
            quotes.remove(qid)
    if quotes != quote_feed_names(base):
        ch["quotes_feed"] = ",".join(quotes) or "none"
    nt = (saved.get("ninjatrader") or {}).get("enabled")
    if nt is not None and base.feed != "sim":
        ch["feed"] = "ninjatrader" if nt else ("none" if base.feed == "ninjatrader" else base.feed)
    return replace(base, **ch) if ch else base


def _pc_only(market) -> bool:
    """With Login off, only the PC itself may use the app: a fresh install, before its first password and again once
    Login is turned off (setup.locked). Any other is open to whoever reaches it."""
    store = getattr(market, "store", None)
    st = (store.get_setting(setup.SETTING, None) if store is not None else None) or {}
    return setup.locked(bool(st.get("fresh")), False, market.s)


def _ago(ms) -> str:
    if not ms:
        return ""
    s = max(0, time.time() - ms / 1000)
    return "just now" if s < 5 else f"{int(s)}s ago" if s < 90 else f"{int(s // 60)} min ago" if s < 5400 else f"{int(s // 3600)} h ago"


class Connections:
    def __init__(self, store, base):
        self.store, self.base = store, base
        self.saved: dict = dict(store.get_setting(SETTING, {}) or {})
        self.instruments = None   # the chart instruments (Market sets it): what a futures plugin may supply

    def effective(self):
        try:
            return build(self.base, self.saved)
        except ValueError:   # .env's own mistake: make_feeds says so
            return self.base

    def known(self, cid: str) -> bool:
        return cid in BY_ID or (bool(EXTRA_RE.match(cid)) and cid in self.saved)

    def instances(self) -> list[tuple[str, dict]]:
        """Every connection, in the lists' order: each type, then the other channels of its kind (discord-2, ...)."""
        out = []
        for d in DEFS:
            out.append((d["id"], d))
            if d.get("multi"):
                more = [k for k in self.saved if k != d["id"] and type_of(k) == d["id"]]
                out += [(k, d) for k in sorted(more, key=lambda k: int(k.rsplit("-", 1)[1]))]
        return out

    def url(self, cid: str, s, body: dict | None = None) -> str | None:
        """An alert or chart-post channel's URL: as typed in `body`, else as saved (else .env's, for the first one)."""
        d = BY_ID[type_of(cid)]
        key = d["fields"][0]["key"]
        typed = clean(cid, body or {}).get(key) if body else None
        if typed is not None:
            return typed or None
        return getattr(s, key, None) if cid == d["id"] else (self.saved.get(cid) or {}).get(key) or None

    def extras(self) -> list[tuple[str, str, str]]:
        """The other alert and chart-post channels that have a URL: (id, kind, url)."""
        return [(cid, d["id"], u) for cid, d in self.instances()
                if cid != d["id"] and d["id"] in ("discord", "discord_charts", "ntfy") and (u := self.url(cid, None))]

    def owners(self) -> dict[str, str]:
        """Instrument -> the futures plugin that supplies its trades (turned on); the rest come from NinjaTrader. None
        with the simulator: real and invented trades never mix."""
        out: dict[str, str] = {}
        if self.base.feed == "sim":
            return out
        for cid, d in self.instances():
            v = self.saved.get(cid) or {}
            if d.get("plugin") == "futures" and v.get("enabled"):
                for sym in filter(None, (v.get("symbols") or "").split(",")):
                    out.setdefault(sym, cid)
        return out

    def _check_symbols(self, cid: str, d: dict, vals: dict):
        """A futures plugin supplies chart instruments only, and each one comes from one feed. One already saved in its
        Futures field and taken off Instruments since is kept (it supplies it again once it is put back): a save of
        anything else, turning it off included, is not refused for it."""
        if d.get("plugin") != "futures" or not vals.get("symbols"):
            return
        syms = vals["symbols"].split(",")
        if self.instruments is not None:
            kept = ((self.saved.get(cid) or {}).get("symbols") or "").split(",")
            missing = [x for x in syms if x not in self.instruments and x not in kept]
            if missing:
                raise ValueError(f"Futures: {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not in Connections > App > Instruments")
        if vals.get("enabled"):
            for other, od in self.instances():
                ov = self.saved.get(other) or {}
                both = set(syms) & set((ov.get("symbols") or "").split(","))
                if other != cid and od.get("plugin") == "futures" and ov.get("enabled") and both:
                    raise ValueError(f"Futures: {', '.join(sorted(both))} already come{'s' if len(both) == 1 else ''} from "
                                     f"{ov.get('name') or od['title']}; one feed per symbol")

    def add(self, kind: str, body: dict) -> str:
        """Another channel of a kind (Discord, chart posts, ntfy) with its URL and name: its new id."""
        d = BY_ID.get(kind)
        if not d or not d.get("multi"):
            raise ValueError(f"{d['title'] if d else kind!r}: only one")
        cid = next((f"{kind}-{n}" for n in range(2, 13) if f"{kind}-{n}" not in self.saved), None)
        if cid is None:
            raise ValueError(f"{d['title']}: 12 at most")
        vals = {k: v for k, v in clean(cid, body).items() if v not in (None, "")}
        if d.get("plugin"):   # a second account of a feed: its fields as typed
            self._check_symbols(cid, d, vals)
        elif not vals.get(d["fields"][0]["key"]):
            raise ValueError(f"{d['fields'][0]['label']}: needed")
        self.saved = {**self.saved, cid: vals}
        self.store.set_setting(SETTING, self.saved)
        return cid

    def remove(self, cid: str):
        """Off the list: another channel, or a plugin's card (its values are only here), is deleted; a connection that
        isn't a main one is cleared (none, whatever .env says) and off. A main one stays: its fields and On do that."""
        if not self.known(cid):
            raise LookupError(cid)
        d = BY_ID[type_of(cid)]
        if cid != d["id"] or d.get("plugin"):
            self.saved = {k: v for k, v in self.saved.items() if k != cid}
            self.store.set_setting(SETTING, self.saved)
            return
        if d.get("main") or d.get("unavailable") or not d["fields"]:
            raise ValueError(f"{d['title']} is always listed: clear its fields or turn it off")
        self.put(cid, {f["key"]: "" if f["type"] == "secret" else False if f["key"] == "enabled" else None for f in d["fields"]}
                 | ({"name": None} if d.get("multi") else {}))

    def put(self, cid: str, body: dict) -> dict:
        if EXTRA_RE.match(cid) and cid not in self.saved:
            raise LookupError(cid)
        vals = {**(self.saved.get(cid) or {}), **clean(cid, body)}
        vals = {k: v for k, v in vals.items() if v is not None}
        self._check_symbols(cid, BY_ID[type_of(cid)], vals)
        candidate = {**self.saved, cid: vals}
        build(self.base, candidate)
        self.saved = {k: v for k, v in candidate.items() if v}
        self.store.set_setting(SETTING, self.saved)
        return vals

    def candidate(self, cid: str, body: dict):
        """The settings as they would be with `body` saved: Test tries what is typed before it is saved."""
        vals = {**(self.saved.get(cid) or {}), **clean(cid, body or {})}
        return build(self.base, {**self.saved, cid: {k: v for k, v in vals.items() if v is not None}})

    # -- what the app shows ---------------------------------------------------------------------------------------
    def public(self, market) -> list[dict]:
        """Every connection as the lists show it. listed: on its kind's list (a main one, one in use, another channel,
        a plugin added with +, on or off, until Remove); the others are what its + offers."""
        s = market.s
        running = {f.name: f for f in market.feed_objs}
        out = []
        for cid, d in self.instances():
            saved, extra = self.saved.get(cid) or {}, cid != d["id"]
            fields = []
            for f in d["fields"]:
                k, row = f["key"], dict(f)
                row["saved"] = saved.get(k) is not None
                if d.get("plugin"):   # its values are its own, saved here only
                    if f["type"] == "secret":
                        row.update(set=bool(saved.get(k)), env=False, source="app" if saved.get(k) else "")
                    else:
                        v = saved.get(k, False if k == "enabled" else self._start(cid, d, f, s))
                        row["value"] = v.replace(",", ", ") if f["type"] == "symbols" else v
                elif k == "enabled":
                    row["value"] = self._enabled(d["id"], s)
                elif f["type"] == "secret":
                    env = None if extra else getattr(self.base, k, None)
                    row.update(set=bool(self.url(cid, s) if extra else getattr(s, k, None)), env=bool(env),
                               source="app" if row["saved"] else ".env" if env else "")
                else:
                    row["value"] = getattr(s, k, None)
                fields.append(row)
            item = {k: d[k] for k in ("group", "about")}
            name = saved.get("name") or ""
            item.update(id=cid, type=d["id"], kind=d["title"], name=name,
                        title=name or (f"{d['title']} {cid.rsplit('-', 1)[1]}" if extra else d["title"]),
                        main=bool(d.get("main")), multi=d.get("multi") or "", extra=extra,
                        fields=fields, unavailable=bool(d.get("unavailable")), locked="", **self._status(cid, market, running))
            item["listed"] = item["main"] or extra or (item["state"] != "off" and not item["unavailable"]) or \
                (bool(d.get("plugin")) and cid in self.saved)
            if d.get("login") and getattr(market, "auth", None) is not None:
                item["login"] = {"enabled": market.auth.enabled, "pc_only": _pc_only(market)}   # the Turn off button says which
            if d.get("backup") and getattr(market, "backups", None) is not None:
                item["backup"] = market.backups.status()
            if d.get("push") and getattr(market, "push", None) is not None:
                item["push"] = {"key": market.push.public, "devices": market.push.devices()}
            if d.get("branding") and getattr(market, "branding", None) is not None:
                item["branding"] = market.branding.get()
            if d.get("instruments"):
                item["instruments"] = True   # its list comes from /api/instruments/config
            if d.get("symbol_list"):
                item["symbol_list"] = True   # its sources come from /api/symbols/status
            if need := packages.missing(d.get("requires")):
                item["install"] = need   # an Install button: POST /api/connections/{id}/install
            if d.get("plugin") == "futures" and self.base.feed == "sim":
                item["locked"] = "This server runs the simulator (CHART_FEED=sim in .env); real and invented trades never mix."
            if d["id"] == "updates" and getattr(market, "updates", None) is not None and market.updates.state.get("changes"):
                item["info"] = "What's new: " + " · ".join(market.updates.state["changes"])
            if d["id"] == "ninjatrader":
                item["info"] = f"Listens on port {s.nt_port} (set in .env and docker-compose.yml)"
                nt = (running.get("ninjatrader") and running["ninjatrader"].stats) or {}
                item["addon"] = {"version": ADDON_VERSION, "file": "/api/addon/TatankaChartFeed.cs",   # its download
                                 "old": bool(nt.get("connected") and nt.get("version", ADDON_VERSION) < ADDON_VERSION)}
                own = sorted(self.owners())
                if own:
                    item["info"] += f" · {', '.join(own)} from other feeds"
                if self.base.feed == "sim":
                    item["locked"] = "This server runs the simulator (CHART_FEED=sim in .env); real and invented trades never mix."
            out.append(item)
        return out

    def _start(self, cid: str, d: dict, f: dict, s):
        """A plugin field's value on a card not saved yet: its default, or the IBKR quotes card's (START) when that one
        is set, saved here or in .env (IBKR_HOST, IBKR_PORT)."""
        key = START.get(d["id"], {}).get(f["key"])
        if key and cid not in self.saved and ((self.saved.get("ibkr") or {}).get(key) is not None
                                              or os.environ.get(key.upper(), "").strip()):
            return getattr(s, key)
        return f.get("default", "")

    @staticmethod
    def _enabled(cid: str, s) -> bool:
        if cid == "ninjatrader":
            return s.feed == "ninjatrader"
        return cid in quote_feed_names(s)

    def _status(self, cid: str, market, running: dict) -> dict:
        s, st = market.s, (getattr(running.get(cid), "stats", None) or {})

        def res(state, text):
            return {"state": state, "status": text}
        d = BY_ID.get(type_of(cid), {})
        if cid == "plugin_errors":
            if need := packages.missing(d.get("requires")):
                return res(*_need(need, getattr(market, "packages", None)))
            return res("error", "Did not load: what each says is below")
        if d.get("plugin"):
            v, futures = self.saved.get(cid) or {}, d["plugin"] == "futures"
            if not v.get("enabled"):
                return res("off", "Off")
            if futures and self.base.feed == "sim":
                return res("off", "Simulator running")
            if need := packages.missing(d.get("requires")):
                return res(*_need(need, getattr(market, "packages", None)))
            if d["plugin"] != "data" and (failed := getattr(market, "plugin_failed", {}).get(cid)):
                return res("error", failed)
            if d["plugin"] == "data":   # nothing runs: read when a chart or watchlist asks
                ds = getattr(market, "data_series", {}).get(cid)
                stats = ds.feed.stats if ds else {}
                if stats.get("last_error"):
                    return res("error", stats["last_error"])
                maker = getattr(market, "make_plugin", None) if d["id"] in getattr(getattr(market, "plugins", None), "types", {}) else None
                feed = maker(cid, d["id"], v) if maker else None
                if failed := getattr(market, "plugin_failed", {}).get(cid):
                    return res("error", failed)
                hint = feed.describe() if feed else ""
                read = f" · read {_ago(stats['last_msg_ms'])}" if stats.get("last_msg_ms") else " · add it to a watchlist or chart it"
                return res("ok", f"On{' · ' + hint if hint else ''}{read}")
            if d["id"] not in getattr(getattr(market, "plugins", None), "types", {}):
                return res("error", "Its plugin did not load (see Feed plugins that did not load)")
            if futures and not v.get("symbols"):
                return res("warn", "No futures chosen: it supplies nothing yet")
            if cid not in running:
                return res("warn", "Starting…")
            if st.get("connected"):
                n, what = (st.get("ticks", 0), "trades") if futures else (st.get("quotes", 0), "quotes")
                return res("ok", f"Connected · {n:,} {what}" + (f" · last {_ago(st['last_msg_ms'])}" if st.get("last_msg_ms") else ""))
            if st.get("last_error"):
                return res("error", st["last_error"])
            return res("warn", st.get("note") or "Connecting…")
        if cid == "ninjatrader":
            if self.base.feed == "sim":
                failed = market.store.get_setting(setup.LEAVE_SIM, None)
                if isinstance(failed, dict):   # setup.leave_sim: no room for the copy made first
                    return res("error", f"Simulator running: leaving it failed ({failed.get('failed')}). Free some disk "
                                        "space, then Leave the simulator again.")
                return res("off", "Simulator running")
            if "ninjatrader" not in running:
                return res("off", "Off")
            if st.get("connected") and st.get("version", ADDON_VERSION) < ADDON_VERSION:
                return res("warn", f"Add-on connected, an older version: update it (below) · {st.get('ticks', 0):,} trades")
            if st.get("connected"):
                return res("ok", f"Add-on connected · {st.get('ticks', 0):,} trades · last {_ago(st.get('last_msg_ms'))}")
            if time.time() - st.get("refused", 0) < 90:   # it tries again every 30 s
                return res("error", "An add-on was turned away: its Token is not this server's. Download the add-on "
                                    "again (below: it has this server's token) or set Token in TatankaChartFeed.cs")
            return res("warn", f"Waiting for the add-on on port {s.nt_port}")
        if cid in QUOTE_IDS:
            if cid not in running:
                return res("off", "Off")
            if cid == "alpaca" and not (s.alpaca_key_id and s.alpaca_secret):
                return res("error", "Needs the API key ID and secret")
            if st.get("last_error") and not (cid in ("ibkr", "coinbase") and st.get("connected")):
                return res("error", st["last_error"])
            if cid == "ibkr":
                return res("ok", f"Connected to {s.ibkr_host}:{s.ibkr_port} · {st.get('symbols', 0)} quotes") if st.get("connected") \
                    else res("warn", f"Connecting to {s.ibkr_host}:{s.ibkr_port}…")
            if not st.get("symbols"):
                return res("ok", "On · nothing in your lists for it")
            what = "pairs" if cid == "coinbase" else "symbols"
            if not st.get("last_msg_ms"):
                return res("warn", f"{st['symbols']} {what} · waiting for the first quotes")
            return res("ok", f"{st['symbols']} {what} · last quote {_ago(st['last_msg_ms'])}")
        if cid == "fred":
            return res("ok", "Key set: the search finds every FRED series" if s.fred_api_key else "No key: values and charts work")
        if cid == "eia":
            return res("ok", "Key set") if s.eia_api_key else res("warn", "Needs a free key")
        if cid == "symbols":
            sy = getattr(market, "symbols", None)
            if sy is None:
                return res("off", "")
            total, bad = sum(sy.count().values()), [k for k, v in sy.errors.items() if v]
            return res("warn" if bad else "ok", f"{total:,} symbols" + (f" · {', '.join(bad)} not updated" if bad else ""))
        if cid == "instruments":
            if getattr(market, "store", None) is None:
                return res("off", "")
            try:
                st = instruments_state(s.config_dir, market.store, market.instruments)
            except (OSError, ValueError) as e:
                return res("error", f"config/instruments.json: {type(e).__name__}")
            n, where = len(st["instruments"]), "edited here" if st["source"] == "app" else "config/instruments.json"
            if st["pending"]:
                return res("warn", f"{n} futures ({where}) · restart to apply")
            return res("ok", f"{n} futures · {where}")
        if cid == "branding":
            b = getattr(market, "branding", None)
            if b is None:
                return res("off", "")
            now = b.get()
            own = [k for k in BRAND_DEFAULTS if now[k] != BRAND_DEFAULTS[k]] + (["logo"] if now["logo"] else [])
            return res("ok", f"{now['name']} · {'own ' + ', '.join(own) if own else 'built in'}")
        if cid == "login":
            a = getattr(market, "auth", None)
            if not a or not a.enabled:
                return res("warn", "Off: only this PC can use the app until a password is set" if _pc_only(market)
                           else "Off: anyone who reaches the app can use it")
            n = len(a.sessions())
            return res("ok", f"On · {n} device{'s' if n != 1 else ''} signed in")
        if cid == "updates":
            u = getattr(market, "updates", None)
            st = u.state if u else {}
            if st.get("error"):
                return res("warn", st["error"])
            if st.get("behind"):
                return res("warn", st["message"])
            if st.get("behind") == 0:
                return res("ok", f"{st['message']} · checked {_ago(st['at'])}")
            return res("warn", "Not checked yet (Test checks now)")
        if cid == "backup":
            b = getattr(market, "backups", None)
            last = b.last() if b else None
            if b and b.error:
                return res("error", f"Last backup failed: {b.error}")
            d = b.disk() if b else {}
            space = f" · database {d['db'] / 1e9:.1f} GB, {d['free'] / 1e9:.0f} GB free" if d else ""
            if d.get("low"):
                return res("error", f"Disk almost full: {d['free'] / 1e9:.1f} GB free{space.split(',')[0]}")
            if not last:
                return res("warn", "None yet" + space)
            age = time.time() - last.stat().st_mtime
            return res("ok" if age < 2 * 86400 else "warn", f"Last {_ago(last.stat().st_mtime * 1000)} · {len(files(b.folder))} kept{space}")
        if cid == "push":
            n = market.push.count() if getattr(market, "push", None) is not None else 0
            if not n:
                return res("off", "No device has turned them on")
            last = getattr(market.notifier, "last", {}).get("app")
            text = f"{n} device{'s' if n != 1 else ''}"
            if not last:
                return res("ok", text)
            return res("ok", f"{text} · last alert delivered {_ago(last[1])}") if last[0] else res("error", f"{text} · last alert failed {_ago(last[1])}")
        if type_of(cid) in ("discord", "ntfy"):
            if not self.url(cid, s):
                return res("off", "Not set")
            last = getattr(market.notifier, "last", {}).get(cid)
            if not last:
                return res("ok", "Set")
            return res("ok", f"Last alert delivered {_ago(last[1])}") if last[0] else res("error", f"Last alert failed {_ago(last[1])}")
        if type_of(cid) == "discord_charts":
            if not self.url(cid, s):
                return res("off", "Not set")
            last = getattr(market.notifier, "last", {}).get(cid)
            if not last:
                return res("ok", "Set")
            return res("ok", f"Last post {_ago(last[1])}") if last[0] else res("error", f"Last post failed {_ago(last[1])}")
        if cid == "heartbeat":
            if not s.heartbeat_url:
                return res("off", "Not set")
            last = getattr(market, "heartbeat_last", None)
            if not last:
                return res("ok", "Set")
            return res("ok", f"Last ping {_ago(last[1])}") if last[0] else res("error", f"Last ping failed {_ago(last[1])}")
        return res("off", "")


# ---- Test: tries a connection with what is typed, before it is saved -------------------------------------------------
def _why(e: Exception) -> str:   # never the URL: it may hold a key
    return f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else type(e).__name__


async def test(market, cid: str, body: dict) -> dict:
    def res(ok, message):
        return {"ok": ok, "message": message}
    d = BY_ID.get(type_of(cid), {})
    if d.get("plugin"):   # the plugin's own test, with the fields as typed over the saved ones
        cls = getattr(getattr(market, "plugins", None), "types", {}).get(d["id"])
        if cls is None:
            return res(False, "Its plugin did not load")
        vals = {**(market.connections.saved.get(cid) or {}), **{k: v for k, v in clean(cid, body or {}).items() if v is not None}}
        feed = cls(cid, {f["key"]: vals.get(f["key"], f.get("default", "")) for f in cls.FIELDS}, [])
        try:
            ok, msg = await asyncio.wait_for(cls.test(feed.values), 30)
        except asyncio.TimeoutError:
            return res(False, "No answer in 30 s")
        except Exception as e:   # a plugin's own mistake: said, never with its keys
            ok, msg = False, f"Test failed: {type(e).__name__}: {e}"
        return res(bool(ok), feed.scrub(str(msg)))
    s = market.connections.candidate(cid, body)
    http = market.http
    try:
        if cid == "updates":
            st = await market.updates.check(s.update_repo, s.github_token)
            return res(not st["error"], st["message"])
        if cid == "ninjatrader":
            f = next((f for f in market.feed_objs if f.name == "ninjatrader"), None)
            if f and f.stats.get("connected"):
                return res(True, f"The add-on is connected ({f.stats.get('ticks', 0):,} trades so far)")
            return res(False, f"No add-on connected. NinjaTrader must be running with TatankaChartFeed on, sending to this "
                              f"server's port {s.nt_port}, with the same token.")
        if cid == "ibkr":
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(s.ibkr_host, s.ibkr_port), 5)
                w.close()
                return res(True, f"IB Gateway/TWS answers on {s.ibkr_host}:{s.ibkr_port}")
            except (OSError, asyncio.TimeoutError):
                return res(False, f"Nothing answers on {s.ibkr_host}:{s.ibkr_port}. Start IB Gateway or TWS and turn the API on "
                                  "(Configure, API, Settings: Enable ActiveX and Socket Clients; Read-Only API).")
        if cid == "alpaca":
            if not (s.alpaca_key_id and s.alpaca_secret):
                return res(False, "Enter the API key ID and the secret key")
            from .feeds.alpaca import API, parse_snapshots
            r = await http.get(API, params={"symbols": "SPY", "feed": s.alpaca_feed}, timeout=15,
                               headers={"APCA-API-KEY-ID": s.alpaca_key_id, "APCA-API-SECRET-KEY": s.alpaca_secret})
            if r.status_code in (401, 403):
                return res(False, "Alpaca refused the keys" + (" (or SIP data needs a paid plan: try IEX)" if s.alpaca_feed == "sip" else ""))
            r.raise_for_status()
            q = parse_snapshots(r.json()).get("SPY")
            return res(True, f"The keys work: SPY {q[0]:.2f} ({s.alpaca_feed.upper()})" if q else "The keys work")
        if cid == "coinbase":
            r = await http.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=15)
            r.raise_for_status()
            return res(True, f"Coinbase answers: BTC-USD {float(r.json()['price']):,.2f}")
        if cid == "yahoo":
            r = await http.get("https://query1.finance.yahoo.com/v8/finance/chart/SPY", params={"range": "1d", "interval": "1d"},
                               headers={"User-Agent": UA, "Accept": "application/json"}, timeout=15, follow_redirects=True)
            r.raise_for_status()
            q = quote_of(((r.json().get("chart") or {}).get("result") or [{}])[0].get("meta") or {})
            return res(bool(q), f"Yahoo answers: SPY {q[0]:.2f}" if q else "Yahoo answered without a price")
        if cid == "fred":
            if s.fred_api_key:
                r = await http.get("https://api.stlouisfed.org/fred/series", timeout=15,
                                   params={"series_id": "DGS10", "api_key": s.fred_api_key, "file_type": "json"})
                if r.status_code in (400, 401, 403):
                    return res(False, "FRED refused the key")
                r.raise_for_status()
                return res(True, "The key works: the symbol search now finds every FRED series")
            from .feeds.fred import CSV_URL
            r = await http.get(CSV_URL, params={"id": "DGS10"}, timeout=20)
            r.raise_for_status()
            return res(True, "FRED answers (no key needed for values and charts)")
        if cid == "eia":
            if not s.eia_api_key:
                return res(False, "Enter the key (free from eia.gov/opendata)")
            from .feeds.eia import API
            r = await http.get(API + "PET.WCESTUS1.W", params={"api_key": s.eia_api_key, "length": 1}, timeout=20)
            if r.status_code in (401, 403):
                return res(False, "EIA refused the key")
            r.raise_for_status()
            return res(True, "The key works")
        if cid == "push":
            total = market.push.count()
            if not total:
                return res(False, "No device has turned them on yet")
            n = await market.push.send("Test", "App notifications are wired up.", tag="test")
            return res(n > 0, f"Sent to {n} of {total} device{'s' if total != 1 else ''}" if n else "No device took it: see each device below")
        if type_of(cid) in ("discord", "ntfy"):
            url = market.connections.url(cid, s, body)
            if not url:
                return res(False, "Enter the URL first")
            n, kind = market.notifier, type_of(cid)
            ok = await (n._discord if kind == "discord" else n._ntfy)("Test", "Chart alerts are wired up.", url)
            return res(ok, "Sent: check the channel" if ok else f"{kind.capitalize()} did not take it: check the URL")
        if type_of(cid) == "discord_charts":
            url = market.connections.url(cid, s, body)
            if not url:
                return res(False, "Enter the URL first")
            ok = await market.notifier.post_chart(url, "Chart posts are wired up.", None, _app_name(market), cid)
            return res(ok, "Sent: check the channel" if ok else "Discord did not take it: check the URL")
        if cid == "heartbeat":
            if not s.heartbeat_url:
                return res(False, "Enter the URL first")
            r = await http.get(s.heartbeat_url, timeout=15)
            return res(r.is_success, "Pinged" if r.is_success else f"HTTP {r.status_code}")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError, TypeError, IndexError) as e:
        return res(False, f"No answer ({_why(e)})")
    return res(False, "Nothing to test")


async def check_all(market) -> list[dict]:
    """Every connection in use, tried at once: [{id, title, ok (None: not in use), message}]. Alert channels get a
    test message each."""
    c = market.connections
    running = {f.name: f for f in market.feed_objs}
    s = market.s

    def used(cid: str, state: str) -> bool:
        if cid in ("yahoo", "fred"):
            return True   # free-data charts use them whatever the quote feeds are
        if cid == "alpaca":
            return state != "off" or bool(s.alpaca_key_id and s.alpaca_secret)   # stock charts use the keys
        if cid == "push":
            return getattr(market, "push", None) is not None and market.push.count() > 0
        if cid == "eia":
            return bool(s.eia_api_key)
        return state != "off"

    async def one(cid, d):
        st = c._status(cid, market, running)
        name = (c.saved.get(cid) or {}).get("name")
        base = {"id": cid, "title": f"{d['title']}: {name}" if name else d["title"] if cid == d["id"] else f"{d['title']} {cid.rsplit('-', 1)[1]}"}
        if cid == "login":
            return {**base, "ok": True if st["state"] == "ok" else None, "message": st["status"]}
        if cid == "backup":
            return {**base, "ok": st["state"] == "ok", "message": st["status"]}
        if cid == "updates":   # as it stands, no extra call to GitHub: failing only when it can't check
            u = getattr(market, "updates", None)
            ust = u.state if u else {}
            return {**base, "ok": False if ust.get("error") else None if ust.get("behind") is None else True, "message": st["status"]}
        if d["id"] == "discord_charts":   # never a test post in a channel others may read: its status only
            return {**base, "ok": True if st["state"] == "ok" else False if st["state"] == "error" else None, "message": st["status"]}
        if not used(cid, st["state"]):
            return {**base, "ok": None, "message": st["status"] or "Off"}
        try:
            r = await asyncio.wait_for(test(market, cid, {}), 30)
        except asyncio.TimeoutError:
            r = {"ok": False, "message": "No answer in 30 s"}
        except Exception as e:   # one failing check must not lose the others
            log.warning("check of %s failed", cid, exc_info=True)
            r = {"ok": False, "message": f"Check failed ({type(e).__name__})"}
        msg = r["message"]
        if r["ok"] and st["state"] in ("warn", "error") and st["status"] not in msg:
            msg = f"{msg} · {st['status']}"
        return {**base, "ok": r["ok"] and st["state"] != "error", "message": msg}
    return list(await asyncio.gather(*(one(cid, d) for cid, d in c.instances()
                                       if not d.get("unavailable") and not d.get("branding") and not d.get("instruments")
                                       and not d.get("symbol_list"))))
