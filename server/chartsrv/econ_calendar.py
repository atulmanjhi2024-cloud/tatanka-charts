"""Economic calendar: this week's and next week's scheduled releases (CPI, payrolls, FOMC, PMIs, claims, ...) with
their impact, forecast and previous value, from ForexFactory's public weekly feed (no account). The Calendar tab
lists them, the chart can mark them, and it warns a few minutes before the ones you pick (by default high-impact USD
events) through the alert channels."""
import logging
import math
import sqlite3
import time
from datetime import datetime

import httpx

from . import stopping

log = logging.getLogger(__name__)
URLS = ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "https://nfs.faireconomy.media/ff_calendar_nextweek.json")
REFRESH_S = 3600                    # the feed asks to be polled gently
IMPACTS = ("High", "Medium", "Low", "Holiday")
DEFAULTS = {"warn": True, "minutes": 5, "currencies": ["USD"], "impacts": ["High"]}


def parse(doc) -> list[dict]:
    """The feed's events: [{id, t (epoch ms), title, country, impact, forecast, previous, actual}], by time."""
    out = []
    for e in doc if isinstance(doc, list) else []:
        if not isinstance(e, dict) or not e.get("title") or not e.get("date"):
            continue
        try:
            t = int(datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            continue
        country = str(e.get("country") or "")[:8].upper()
        out.append({"id": f"{country}|{e['title']}|{t}"[:200], "t": t, "title": str(e["title"])[:120], "country": country,
                    "impact": e.get("impact") if e.get("impact") in IMPACTS else "Low",
                    **{k: str(e.get(k) or "")[:24] for k in ("forecast", "previous", "actual")}})
    return sorted(out, key=lambda x: x["t"])


def clean_settings(d: dict) -> dict:
    if not isinstance(d, dict):
        raise ValueError("expected {warn, minutes, currencies, impacts}")
    out = {**DEFAULTS}
    if "warn" in d:
        out["warn"] = bool(d["warn"])
    if "minutes" in d:
        m = d["minutes"]
        if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 120:
            raise ValueError("minutes: 1-120")
        out["minutes"] = m
    if "currencies" in d:
        c = d["currencies"]
        if not isinstance(c, list) or not all(isinstance(x, str) and x.isalpha() and len(x) <= 4 for x in c) or len(c) > 20:
            raise ValueError("currencies: a list like ['USD', 'EUR']")
        out["currencies"] = sorted({x.upper() for x in c})
    if "impacts" in d:
        i = d["impacts"]
        if not isinstance(i, list) or not set(i) <= set(IMPACTS):
            raise ValueError(f"impacts: some of {', '.join(IMPACTS)}")
        out["impacts"] = [x for x in IMPACTS if x in i]
    return out


class EconCalendar:
    def __init__(self, market):
        self.m = market
        self.events: list[dict] = list(market.store.get_setting("calendar_events", []) or [])
        self.fetched = 0.0
        self.error = ""
        self.cfg = {**DEFAULTS, **(market.store.get_setting("calendar_cfg", {}) or {})}
        self.warned: set[str] = set(market.store.get_setting("calendar_warned", []) or [])   # kept across a restart

    def put_settings(self, d: dict) -> dict:
        self.cfg = clean_settings({**self.cfg, **d})
        self.m.store.set_setting("calendar_cfg", self.cfg)
        return self.cfg

    def between(self, start: int, end: int) -> list[dict]:
        return [e for e in self.events if start <= e["t"] < end]

    async def refresh(self) -> int:
        got: dict[str, dict] = {}
        weeks = 0   # how many of the weeks were read
        for i, url in enumerate(URLS):
            try:
                r = await self.m.http.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (tatanka-charts)"})
                if r.status_code == 404 and i:   # next week's is posted later in the week
                    continue
                r.raise_for_status()
                got.update({e["id"]: e for e in parse(r.json())})
                weeks += 1
            except (httpx.HTTPError, ValueError) as e:
                if not i:
                    raise
                log.info("calendar: next week not read (%s)", type(e).__name__)
        if got:   # keep last week's for the chart's markers a while
            # from this week's first event (or now) on, the weeks read are as the feed has them now: an event it moved
            # or dropped goes from where it was (its id holds its time); with next week not read, what was known of it stays
            now, ts = time.time() * 1000, [e["t"] for e in got.values()]
            lo, hi = min(min(ts), now), (max(ts) if weeks < len(URLS) else math.inf)
            keep = {e["id"]: e for e in self.events if e["t"] > now - 21 * 86_400_000 and not lo <= e["t"] <= hi}
            self.events = sorted({**keep, **got}.values(), key=lambda x: x["t"])
            self.m.store.set_setting("calendar_events", self.events)
        return len(got)

    def _warn(self):
        c, now = self.cfg, time.time() * 1000
        if not c["warn"]:
            return
        for e in self.events:
            if (e["id"] in self.warned or e["country"] not in c["currencies"] or e["impact"] not in c["impacts"]
                    or not 0 <= e["t"] - now <= c["minutes"] * 60_000):
                continue
            self.warned.add(e["id"])
            self.warned = {x["id"] for x in self.events if x["id"] in self.warned and x["t"] >= now}   # the ones to come
            mins = max(1, round((e["t"] - now) / 60_000))
            extra = " · ".join(x for x in (e["forecast"] and f"forecast {e['forecast']}", e["previous"] and f"previous {e['previous']}") if x)
            ev = {"alert_id": None, "symbol": e["country"], "ts": int(now), "price": None,
                  "message": f"{e['country']} {e['title']} in {mins} min ({e['impact']} impact){' · ' + extra if extra else ''}"}
            try:   # the warned ids saved, so a restart inside the window doesn't warn of it again
                self.m.store.set_setting("calendar_warned", sorted(self.warned))
                ev["id"] = self.m.store.add_event(ev)
            except sqlite3.Error as err:   # the database can't be written (a full disk): warned all the same
                log.warning("calendar warning not saved: %s", err)
                ev["id"] = None
            self.m._on_alert(ev)

    async def run(self):
        if not getattr(self.m.s, "symbols_online", True):   # CHART_SYMBOLS_ONLINE=0: no online refreshes
            return
        while True:
            if time.time() - self.fetched > REFRESH_S and self.m.http is not None:
                try:
                    n = await self.refresh()
                    self.fetched, self.error = time.time(), ""
                    self.m.broadcast({"type": "calendar"})
                    log.info("calendar: %d events", n)
                except (httpx.HTTPError, ValueError, sqlite3.Error) as e:   # sqlite3: not saved (a full disk)
                    self.fetched = time.time() - REFRESH_S + 600   # try again in 10 minutes
                    self.error = f"{type(e).__name__}"
                    log.warning("calendar not read (%s)", type(e).__name__)
            self._warn()
            await stopping.pause(20)   # not after a stop the request swallowed (stopping.py)
