"""HTTP + websocket server. Run: uvicorn chartsrv.app:app --host 127.0.0.1 --port 8800 --loop asyncio"""
import asyncio
import base64
import binascii
import json
import hashlib
import heapq
import hmac
import ipaddress
import logging
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette._utils import get_route_path   # the path the routes are found by (FastAPI's routing uses it too)
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field

from . import auth
from . import connections as conns
from . import packages, sessions, setup, workers
from . import watchlists as wl
from .config import INSTRUMENTS_SETTING, Instrument, clean_instruments, futures_catalog, instruments_file, \
    instruments_state, load_holidays, load_instruments, load_settings, migrate_instruments
from .feeds import make_feeds, quote_feed_names
from .market import DAY_MS, Market, now_ms
from .notify import Notifier
from .feeds import sdk
from .plugins import BUILTIN_DIR as BUILTIN_PLUGINS, Plugins
from .sessions import previous_session_start, session_start
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # its INFO lines print request URLs: webhook secrets
log = logging.getLogger("chartsrv")
settings = load_settings()


def _web_version(web_dir) -> str:
    """The web app's version: a hash of its files. A page from another version reloads itself (hello's "version")."""
    h = hashlib.sha1()
    for p in sorted(web_dir.rglob("*")) if web_dir.is_dir() else []:
        if p.is_file():
            h.update(p.relative_to(web_dir).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


WEB_VERSION = _web_version(settings.web_dir)


DB_QUIET_S = 60


async def watch_db(store, every: float = 5, after: asyncio.Task | None = None):
    """Another program wrote the database (docker compose exec ... backfill or databento, a tool): once it has been
    quiet a minute, the server restarts, so charts and studies show what it wrote. Its own writes don't count, nor
    those of the start's clean-up on a connection of its own (after: watched from when it is done)."""
    if after is not None:
        await asyncio.wait([after])
    seen, since = store.data_version(), None
    while True:
        await asyncio.sleep(every)
        v = store.data_version()
        if v != seen:
            seen, since = v, time.monotonic()
        elif since is not None and time.monotonic() - since >= DB_QUIET_S:
            log.warning("another program changed the database: restarting so the charts load it")
            restart_server()
            return


def restart_server():
    """Stops the server cleanly and starts it again in the same process (no Docker needed for it): a saved instrument
    list, leaving the simulator. Every open page reconnects and, if anything it shows changed, reloads by itself."""
    app.state.reexec = os.environ.get("CHART_RESTART_EXEC", "1") != "0"   # tests: a plain stop
    # raised inside the process, so uvicorn's own handler stops it cleanly (os.kill would end it at once on Windows)
    asyncio.get_running_loop().call_later(0.5, signal.raise_signal, signal.SIGTERM)


def _local_token(s) -> str:
    """A token for `python -m chartsrv.check` on this PC: written next to the database, readable only there."""
    from .check import token_path
    token = secrets.token_urlsafe(24)
    p = token_path(s)
    try:
        p.write_text(token)
        p.chmod(0o600)
    except OSError as e:
        log.warning("check token not written (%s)", type(e).__name__)
    return token


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = load_settings()
    try:   # edited by hand before each holiday: a typo is logged, not a server that won't start
        sessions.set_calendar(load_holidays(s.config_dir))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.error("config/holidays.json not read (%s: %s): CME's usual holiday hours apply until it is fixed",
                  type(e).__name__, e)
        sessions.set_calendar(load_holidays(None))
    store = Store(s.db_path)
    app.state.fresh = setup.init(store)["fresh"]   # a fresh install: the setup guide, and other devices wait
    s = setup.leave_sim(store, s)                 # the simulator left in the setup guide: the real feeds
    migrate_instruments(store)
    instruments = load_instruments(s.config_dir, store)   # the file's, or the list edited in the app
    watchlists = wl.load(store, s.config_dir)
    http = httpx.AsyncClient(timeout=10)
    notifier = Notifier(http, s.discord_webhook, s.ntfy_url)
    packages.use_user_site()   # packages installed from Connections, after the app's own
    plugins = Plugins([BUILTIN_PLUGINS, s.feeds_dir], conns.builtin_ids())   # feed plugins: built in, then yours
    sdk.FEEDS_DIR = s.feeds_dir
    conns.register_plugins(plugins)
    market = Market(s, instruments, watchlists, store, notifier, http, plugins)
    workers.start()   # the worker processes import while the feeds connect
    await market.start(make_feeds(market.s, instruments, market.levels))   # .env, with what Connections saved over it
    app.state.market = market
    market.app_version = WEB_VERSION
    if os.environ.get("CHART_DB_WATCH", "1") != "0":   # tests: off
        market.tasks.append(asyncio.create_task(watch_db(store, after=market.cleaning)))
    app.state.local_token = _local_token(s)
    log.info("feeds=%s notify=%s db=%s", market.feed_names, notifier.channels, s.db_path)
    yield
    await market.stop()
    workers.shutdown(wait=getattr(app.state, "reexec", False))   # a restart: they end first, this process runs on
    await http.aclose()
    store.close()
    if getattr(app.state, "reexec", False):   # restart_server(): this same command again, as a fresh process
        log.warning("restarting")
        if os.name == "nt":   # Windows has no exec in place: a new process, and this one ends
            subprocess.Popen(sys.orig_argv)
        else:
            os.execv(sys.executable, sys.orig_argv)


# Every route is async so it runs on the event loop: the market and its SQLite connection are not thread-safe.
app = FastAPI(title="Tatanka Charts", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)   # the app's files and JSON over a phone's link


@app.exception_handler(RequestValidationError)
async def _invalid(request: Request, exc: RequestValidationError):
    """FastAPI's 422 without the rejected input echoed back (a NaN there made the answer itself fail, a 500)."""
    return JSONResponse(status_code=422, content={"detail": [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")),
                                                              "type": str(e.get("type", ""))} for e in exc.errors()]})


def M() -> Market:
    return app.state.market


# ---- login (auth.py): once a password is set, /api and /ws need a signed-in device -------------------------------------
OPEN = {("GET", "/api/auth"), ("POST", "/api/auth/login"), ("GET", "/api/branding"), ("GET", "/api/branding/logo")}   # the sign-in shows the name


def _who(request) -> str:
    """The address a login try came from: the last X-Forwarded-For entry, the one the proxy in front (tailscale
    serve) wrote; anything before it is whatever the client sent."""
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[-1].strip()[:64] or (request.client.host if request.client else "?")


def _page_scheme(request) -> str:
    """The scheme of the page the browser is on, where the request shows it: the X-Forwarded-Proto a proxy in front
    names (tailscale serve 1.44+, nginx as docs/PHONE.md has it), else the page's own address, which the browser sends
    (the Origin of a sign-in, the Referer of a page's GET: Apache and tailscale serve before 1.44 name no scheme) where
    it is the address the browser asked for, port included, and the browser doesn't say the request comes from another
    page (Sec-Fetch-Site, which it sends to https only). Another site's link sends that site's (http://evil.example/),
    a URL typed in none, and a page on this name's plain http (which an attacker on the network can answer) is another
    page to an https one: "" then, and the cookie is not renewed on such a request (_login, auth_status)."""
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if scheme or request.headers.get("sec-fetch-site", "same-origin") != "same-origin":
        return scheme
    address = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host", "")
    fwd = request.headers.get("x-forwarded-port", "").split(",")[0].strip()   # an address with no port: on this one
    for page in (request.headers.get("origin", ""), request.headers.get("referer", "")):
        try:   # the browser's address: a proxy's X-Forwarded-Host first (Apache's defaults send the app's as the Host)
            u, a = urlsplit(page), urlsplit("//" + address)
            port = a.port or (int(fwd) if fwd.isdigit() else PORTS.get(u.scheme))   # as _same_origin reads it
            if u.scheme in ("http", "https") and u.hostname and u.hostname == a.hostname \
                    and (u.port or PORTS[u.scheme]) == port:   # an Origin "null": no address
                return u.scheme
        except ValueError:   # a broken address, a port that is no number
            pass
    return ""


def _secure(request) -> bool:
    """A Secure cookie on an https page, and on the PC itself (browsers take localhost as secure): one on a plain http
    page would never come back, and every sign-in would end at the login form again. The page's scheme where the
    request shows it (_page_scheme), else the request's own."""
    address = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host", "")
    try:   # the browser's: a proxy's X-Forwarded-Host first (Apache's defaults send the app's own as the Host)
        host = (urlsplit("//" + address).hostname or "").lower()
    except ValueError:   # a broken [IPv6] address
        host = ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    return (_page_scheme(request) or request.url.scheme) == "https"


def _set_cookie(response, request, token: str):
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=_secure(request), path="/")


PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}   # the port an address with none is on


def _same_origin(headers, scheme: str) -> bool:
    """A browser says which page a request comes from (Origin). Another site's page, open in the same browser, may
    not use this app: its Origin must be this app's own address, port included (the Host, or the X-Forwarded-Host
    that tailscale serve passes on). A page on another port of the same name (another program on the PC, another
    tailscale serve port) is another site's too, though the browser sends it this app's cookie. An address with no
    port is on the one X-Forwarded-Port names (a proxy of yours whose Host has none, nginx's $host), else on its
    scheme's default: X-Forwarded-Proto's (tailscale serve's https). Without that, the PC's own address straight from
    its browser is on the scheme the request came in on (`scheme`: the app on port 80), and any other may have come
    through a proxy that names no scheme (tailscale serve before 1.44, Apache), so the page's is taken. Programs
    that aren't browsers send no Origin."""
    origin = headers.get("origin")
    if not origin:
        return True
    proto = headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()   # tailscale serve's https
    port = headers.get("x-forwarded-port", "").split(",")[0].strip()   # nginx: proxy_set_header X-Forwarded-Port $server_port
    try:
        o = urlsplit(origin)
        page = ((o.hostname or "").lower(), o.port or PORTS.get(o.scheme))   # the page's name and port
        names = [(headers.get("host", ""), proto or (scheme if setup.is_local(headers, None) else o.scheme)),
                 *((h, proto or o.scheme) for h in headers.get("x-forwarded-host", "").split(","))]
        own = {((u.hostname or "").lower(), u.port or (int(port) if port else PORTS.get(s)))
               for u, s in ((urlsplit("//" + h.strip()), s) for h, s in names if h.strip())}
    except ValueError:   # a port that is no number, a broken [IPv6] address
        return False
    return bool(page[0]) and page in own


TS_NAMES_MAX = 8   # this PC's tailscale serve names kept (setup.TS_NAMES): a renamed PC or tailnet adds its new one


def _own_host(headers, m) -> bool:
    """The address the browser asked for (Host, which a page can't change) is one of this app's: the PC (localhost), an
    IP address, this PC's own tailscale serve name (_own_ts_name), or one in CHART_ALLOWED_HOSTS. Another site's page
    whose name was pointed at this PC (DNS rebinding) sends its own name, and an Origin that matches it, so without this
    it would pass as this app, password or not. A name with no dots counts as another site's too: whoever runs the
    network (a public Wi-Fi) answers for it, so the PC's own name goes in CHART_ALLOWED_HOSTS. A proxy may send the
    app's own address as the Host and the browser's in X-Forwarded-Host (Apache's defaults: 127.0.0.1:8800), which
    _same_origin takes as this app's: every name in it must be one of this app's too."""
    fwd = [h for h in headers.get("x-forwarded-host", "").split(",") if h.strip()]
    return all(_own_name(h, headers, m) for h in (headers.get("host", ""), *fwd))


def _own_name(address: str, headers, m) -> bool:   # one address of _own_host's: the Host, or one X-Forwarded-Host
    try:
        host = (urlsplit("//" + address.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host or host == "localhost" or host in getattr(m.s, "allowed_hosts", ()):
        return True
    if host.endswith(".ts.net"):
        return _own_ts_name(headers, host, m.store)
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _own_ts_name(headers, host: str, store) -> bool:
    """A *.ts.net name is this PC's once a browser opened the app by it over https: tailscale serve holds a certificate
    for this PC's name only. Another name under ts.net pointed at this PC comes straight to the app's port, over plain
    http, and the page's own address says so: the Origin, or the Referer a GET sends instead, neither of which a page
    can change. Through tailscale serve the request has an X-Forwarded-For too: a page's own request can add one, but
    with its http:// address, and a link or a form on an https page can add none. A proxy of yours on plain http adds
    one for it, though, and many say nothing else (Apache, HAProxy, nginx with no X-Forwarded-Proto line), so the
    request must also say it came in over https, X-Forwarded-Proto, and name no other address in X-Forwarded-Host, as
    tailscale serve 1.44 and later does. An older one's name goes in CHART_ALLOWED_HOSTS (docs/PHONE.md). The name is
    kept, so a request that names no page gets in by it from then on (a program, /api/health typed into Safari)."""
    names = store.get_setting(setup.TS_NAMES, None) or []
    if host in names:
        return True
    proto = headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()   # tailscale serve 1.44+: https
    fwd_host = headers.get("x-forwarded-host", headers.get("host", "")).strip().lower()   # tailscale serve's: the Host
    try:
        page = urlsplit(headers.get("origin") or headers.get("referer") or "")
        mine = page.scheme == "https" and (page.hostname or "").rstrip(".") == host and bool(headers.get("x-forwarded-for")) \
            and proto == "https" and fwd_host == headers.get("host", "").strip().lower()
    except ValueError:   # a broken address
        return False
    if mine:
        store.set_setting(setup.TS_NAMES, [*names, host][-TS_NAMES_MAX:])
        log.info("this PC's tailscale serve name: %s (the app opened by it over https)", host)
    return mine


OPEN_BODY_MAX = 16_384   # an open route's body (a sign-in: a password and a device name), read before any login


async def _small_body(request, most: int = OPEN_BODY_MAX) -> bool:
    """Reads the body of a request no login guards, refusing it once it passes `most` bytes: anyone who reaches the
    port could otherwise have gigabytes held in memory. The route then reads it from here."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > most:
        return False
    body = b""
    async for chunk in request.stream():   # sent in chunks, with no length: counted as it comes
        body += chunk
        if len(body) > most:
            return False
    request._body = body   # where Starlette's own body() keeps it, which the route is given
    return True


@app.middleware("http")
async def _login(request: Request, call_next):
    path = get_route_path(request.scope)   # not request.url.path: an odd Host (Starlette < 1.1.0) or a --root-path changes it
    if path.startswith("/api") and not _own_host(request.headers, M()):
        log.warning("refused %s %s for another site's address: %s", request.method, path,   # a proxy's: and X-Forwarded-Host
                    " / ".join(filter(None, (request.headers.get("host"), request.headers.get("x-forwarded-host")))))
        return JSONResponse(status_code=403, content={"detail": "not this app's address (CHART_ALLOWED_HOSTS)",
                                                      "wrong_host": True})   # the page says so (app.js wrongHost)
    if path.startswith("/api") and request.method not in ("GET", "HEAD", "OPTIONS") \
            and not _same_origin(request.headers, request.scope.get("scheme", "http")):
        log.warning("refused %s %s from another site: %s (Host %s)", request.method, path, request.headers.get("origin"),
                    request.headers.get("host"))   # a proxy of yours: it passes on the port too (docs/PHONE.md)
        return JSONResponse(status_code=403, content={"detail": "not from this app"})
    if request.method == "POST" and (request.method, path.rstrip("/")) in OPEN and not await _small_body(request):
        return JSONResponse(status_code=413, content={"detail": "too big"})
    if not path.startswith("/api") or (request.method, path.rstrip("/")) in OPEN:
        return await call_next(request)
    a = M().auth
    if setup.locked(app.state.fresh, a.enabled, M().s) and not setup.is_local(request.headers, None):
        return JSONResponse(status_code=403, content={"detail": "Set a password on the PC first", "need_password": True})
    local = request.headers.get("x-local-token")
    if not a.enabled or (local and hmac.compare_digest(local.encode(), (getattr(app.state, "local_token", "") or "-").encode())):
        return await call_next(request)
    sess = a.session(request.cookies.get(auth.COOKIE), touch=bool(_page_scheme(request)))   # renewed from its page only
    if sess is None:
        return JSONResponse(status_code=401, content={"detail": "sign in"})
    request.state.session = sess
    response = await call_next(request)
    sets = any(c.startswith(f"{auth.COOKIE}=") for c in response.headers.getlist("set-cookie"))
    if sess["renew"] and not sets:   # a device in use stays signed in (not over a cookie the request itself set)
        _set_cookie(response, request, request.cookies[auth.COOKIE])
    return response


class LoginIn(BaseModel):
    password: str = Field(max_length=auth.MAX_LEN)
    label: str = Field("", max_length=60)


class PasswordIn(BaseModel):
    current: str = Field("", max_length=auth.MAX_LEN)
    new: str | None = Field(None, max_length=auth.MAX_LEN)   # None: login off
    label: str = Field("", max_length=60)                     # this device, which stays signed in


def _same_password(a, cfg: dict) -> bool:
    """The password is still the one a slow check (scrypt, in a thread) was given: a change made meanwhile, here or
    with `python -m chartsrv.auth set`, has signed every device out, and the old password must not sign one in."""
    return a.cfg().get("hash") == cfg.get("hash")


@app.get("/api/auth")
async def auth_status(request: Request, response: Response):
    a = M().auth
    sess = a.session(request.cookies.get(auth.COOKIE), touch=bool(_page_scheme(request))) if a.enabled else None
    if sess and sess["renew"]:   # a page load asks this first: the day's renewal reaches the cookie here
        _set_cookie(response, request, request.cookies[auth.COOKIE])
    out = {"enabled": a.enabled, "signed_in": not a.enabled or sess is not None,
           "label": sess["label"] if sess else "", "session": sess["id"] if sess else ""}
    if setup.locked(app.state.fresh, a.enabled, M().s) and not setup.is_local(request.headers, None):
        out.update(signed_in=False, need_password=True)   # a fresh install: set a password on the PC first
    return out


@app.post("/api/auth/login")
async def auth_login(body: LoginIn, request: Request):
    a, who = M().auth, _who(request)
    if not a.enabled:
        raise HTTPException(400, "no password is set")
    wait = a.attempt(who)
    if wait:
        raise HTTPException(429, f"too many wrong passwords: try again in {wait // 60 + 1} min")
    cfg = a.cfg()
    if not await asyncio.to_thread(auth.verify, body.password, cfg):
        log.warning("wrong password from %s", who)
        raise HTTPException(401, "wrong password")
    if not _same_password(a, cfg):   # changed while it was checked: the old one signs no device in
        raise HTTPException(401, "the password changed: sign in again")
    a.ok(who)
    response = JSONResponse({"ok": True})
    _set_cookie(response, request, a.start(body.label))
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get(auth.COOKIE)
    if token and M().auth.end(token):
        M().drop_clients()
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.put("/api/auth/password")
async def auth_password(body: PasswordIn, request: Request):
    """Sets, changes (the current one needed) or with new=null turns off the password. Setting it signs this device
    in and every other device out."""
    a, who = M().auth, _who(request)
    cfg = a.cfg()
    if a.enabled:
        wait = a.attempt(who)
        if wait:
            raise HTTPException(429, f"too many wrong passwords: try again in {wait // 60 + 1} min")
        if not await asyncio.to_thread(auth.verify, body.current, cfg):
            raise HTTPException(403, "the current password is wrong")
        a.ok(who)
    if body.new is None:
        if not a.enabled:
            raise HTTPException(400, "no password is set")
        if not _same_password(a, cfg):
            raise HTTPException(409, "the password changed meanwhile: try again")
        a.turn_off()
        M().drop_clients()
        response = JSONResponse({"enabled": False})
        response.delete_cookie(auth.COOKIE, path="/")
        return response
    try:
        new = await asyncio.to_thread(auth.make_hash, body.new)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if not _same_password(a, cfg):   # another device (or the CLI) changed it while this one was hashed: that one stays
        raise HTTPException(409, "the password changed meanwhile: try again")
    a.save(new)
    a.end_all()
    M().drop_clients()
    response = JSONResponse({"enabled": True})
    _set_cookie(response, request, a.start(body.label))
    return response


@app.get("/api/auth/sessions")
async def auth_sessions(request: Request):
    sess = getattr(request.state, "session", None)
    return {"sessions": [{**s, "current": bool(sess and s["id"] == sess["id"])} for s in M().auth.sessions()]}


@app.delete("/api/auth/sessions/{sid}", status_code=204)
async def auth_session_end(sid: str):
    if not M().auth.end_id(sid):
        raise HTTPException(404, "no such session")
    M().drop_clients()


@app.post("/api/auth/sessions/end-others")
async def auth_end_others(request: Request):
    n = M().auth.end_all(keep=request.cookies.get(auth.COOKIE))
    M().drop_clients()
    return {"ended": n}


class AlertIn(BaseModel):
    symbol: str
    kind: Literal["price", "level", "study", "ind"]
    study: Literal["vwap", "rth_vwap", "poc", "vah", "val"] | None = None
    band: int = Field(0, ge=-3, le=3)               # VWAP sigma band; 0 = the VWAP itself
    profile: Literal["eth", "rth"] = "eth"          # POC/VAH/VAL of the ETH (17:00) or RTH session
    which: Literal["current", "previous"] = "current"
    price: float | None = Field(None, allow_inf_nan=False)
    direction: Literal["cross", "above", "below", "gt", "lt", "enter", "exit"] = "cross"
    price2: float | None = Field(None, allow_inf_nan=False)   # the channel's other edge (enter, exit)
    expires: int | None = None                      # epoch ms; the alert switches itself off then
    levels: list[str] | Literal["all"] = "all"
    tolerance_ticks: int = Field(0, ge=0, le=200)
    rearm_ticks: int = Field(8, ge=1, le=2000)
    repeat: bool | None = None
    note: str = Field("", max_length=200)
    a: dict | None = None                           # indicator alerts: the series that crosses...
    b: dict | None = None                           # ...the one it crosses: a series or {value}
    spec: str = "5m"                                # ...on these bars
    on: Literal["close", "bar"] = "close"           # ...checked on bar close, or intrabar once a bar
    rth: bool = False                               # ...regular-hours bars only, as an RTH chart


class AlertPatch(BaseModel):
    enabled: bool | None = None
    repeat: bool | None = None
    note: str | None = Field(None, max_length=200)
    price: float | None = Field(None, allow_inf_nan=False)   # price alerts: moved (re-arms it)
    price2: float | None = Field(None, allow_inf_nan=False)
    direction: Literal["cross", "above", "below", "gt", "lt", "enter", "exit"] | None = None
    expires: int | None = None                      # 0 = never
    tolerance_ticks: int | None = Field(None, ge=0, le=200)


def _price_params(inst, price: float, direction: str, price2: float | None) -> dict:
    params = {"price": inst.round(price), "direction": direction}
    if direction in ("enter", "exit"):
        if price2 is None:
            raise HTTPException(400, "a channel needs both edges")
        lo, hi = sorted((inst.round(price), inst.round(price2)))
        if lo == hi:
            raise HTTPException(400, "a channel's edges must differ")
        params.update(price=lo, price2=hi)
    return params


def _expiry(expires: int | None) -> int | None:
    if not expires:
        return None
    if expires <= int(time.time() * 1000):
        raise HTTPException(400, "the expiration is in the past")
    return expires


@app.get("/api/health")
async def health():
    return M().health()


@app.get("/api/instruments")
async def instruments():
    return {s: i.to_dict() for s, i in M().instruments.items()}


# ---- Connections > Instruments: the chartable futures; one added applies at once, other changes at a restart ---------
@app.get("/api/instruments/config")
async def instruments_config():
    """The list as edited (or config/instruments.json's), where it is from, and pending: it differs from what runs."""
    m = M()
    return _inst_state(m)


def _inst_state(m) -> dict:
    """The Instruments card's state, with the running list's version: a save made from an older view is refused."""
    return {**instruments_state(m.s.config_dir, m.store, m.instruments), "version": m.config_hash(saved=True),
            "contracts": dict(m.contracts)}   # the contract each is on now (NinjaTrader's C lines)


class InstrumentsIn(BaseModel):
    instruments: list[dict] = Field(..., max_length=60)
    version: str | None = Field(None, max_length=40)   # the list it was edited from (GET's version)


@app.put("/api/instruments/config")
async def instruments_put(body: InstrumentsIn):
    """Saves the list ([{symbol, name, tick_size, point_value, rth, calendar, sim_price, sim_follow}]); the
    file's own list saved again means the file applies. Restart (POST /api/server/restart) to use it."""
    m = M()
    if body.version is not None and body.version != m.config_hash(saved=True):
        raise HTTPException(409, "The list changed meanwhile (a future opened on another device?): reopen it and edit again")
    try:
        new = clean_instruments(body.instruments)
        file = clean_instruments([{"symbol": k, **v} for k, v in instruments_file(m.s.config_dir).items()])
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    m.store.set_setting(INSTRUMENTS_SETTING, None if new == file else new)
    await _apply_live(m)
    m.broadcast({"type": "connections", "notify": m.notifier.channels, "feeds": m.feed_names})
    return _inst_state(m)


async def _apply_live(m):
    """Futures added, or how they roll changed: live at once, no restart. Any other change or a removal still restarts
    the server (the page asks, as pending says), as does the simulator, which makes its trades for the list it
    started with."""
    want = load_instruments(m.s.config_dir, m.store)
    if m.s.feed == "sim" or not all(s in want and replace(want[s], contract=i.contract) == i for s, i in m.instruments.items()):
        return
    rolls = {s: want[s] for s, i in m.instruments.items() if want[s].contract != i.contract}
    if rolls:   # only how some roll: the add-on hears it at once
        m.instruments.update(rolls)
        m._owners_changed()
    added = {s: i for s, i in want.items() if s not in m.instruments}
    await m.add_instruments(added)
    if rolls and not added:   # other pages hear the new version (add_instruments says it otherwise)
        m.broadcast({"type": "instruments", "instruments": {s: i.to_dict() for s, i in m.instruments.items()},
                     "catalog": m.catalog(), "config": m.config_hash()})


class FutureIn(BaseModel):
    symbol: str = Field(..., max_length=8)


@app.post("/api/instruments/add")
async def instruments_add(body: FutureIn):
    """A future from the catalog opened in a chart: added for good and live at once, no restart."""
    try:
        return (await M().add_future(body.symbol.strip().upper())).to_dict()
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.delete("/api/instruments/config")
async def instruments_reset():
    """Back to config/instruments.json at the next start."""
    m = M()
    m.store.set_setting(INSTRUMENTS_SETTING, None)
    await _apply_live(m)
    m.broadcast({"type": "connections", "notify": m.notifier.channels, "feeds": m.feed_names})
    return _inst_state(m)


@app.get("/api/setup")
async def setup_get(request: Request):
    """The setup guide's state: done, the step it is on, the trade feed (sim: the simulator still runs), the watchlist's
    quote feeds, login, and whether this device is the PC itself (a password can be skipped only there)."""
    m = M()
    st = m.store.get_setting(setup.SETTING, None) or {"done": True, "step": 0}
    return {"done": bool(st.get("done")), "step": int(st.get("step", 0)), "feed": m.s.feed,
            "quotes": quote_feed_names(m.s), "login": m.auth.enabled, "local": setup.is_local(request.headers, None),
            "left_sim": m.store.get_setting(setup.LEAVE_SIM, None) == "done"}


class SetupIn(BaseModel):
    step: int = Field(0, ge=0, le=10)
    done: bool = False


@app.put("/api/setup")
async def setup_put(body: SetupIn):
    m = M()
    st = m.store.get_setting(setup.SETTING, None) or {}
    m.store.set_setting(setup.SETTING, {**st, "step": body.step, "done": body.done})
    return {"ok": True}


@app.post("/api/setup/leave-sim")
async def setup_leave_sim():
    """Off the simulator: its invented trades and bars go (a copy of the database is kept), then a restart on the real
    feeds. Only while the simulator runs."""
    m = M()
    if m.s.feed != "sim":
        raise HTTPException(400, "the simulator is not running")
    d = m.backups.disk()   # the restart copies the whole database first: room for it, and a little over
    if d["free"] < d["db"] * 1.1 + 50_000_000:
        raise HTTPException(507, f"Not enough free disk space to leave the simulator: the database ({d['db'] / 1e9:.1f} "
                                 f"GB) is copied first, and {d['free'] / 1e9:.1f} GB are free where it is. Free some "
                                 "space, then try again.")
    m.store.set_setting(setup.LEAVE_SIM, "pending")
    log.warning("leaving the simulator: market data is deleted at the restart")
    restart_server()
    return {"ok": True}


@app.post("/api/server/restart")
async def server_restart():
    """Stops the server cleanly and starts it again (restart_server)."""
    log.warning("restart asked from the app")
    restart_server()
    return {"ok": True}


@app.get("/api/levels/{symbol}")
async def levels(symbol: str):
    """The symbol's daily levels as the Daily levels study draws them (P, R1..., S1...)."""
    ls = M().levels.get(symbol.upper())
    if not ls:
        raise HTTPException(404, f"no levels for {symbol}")
    return ls.to_dict()


@app.get("/api/vp/{kind}")
async def volume_profile(kind: str, symbol: str, key: str = "", have: str = "", s: str = ""):
    """The Session (prof), Auto Anchored (aavp) or Periodic (pvp) volume profile of a symbol, ported from the Tatanka
    NinjaTrader indicators, with their saved settings (/api/indicators). key and have: the build named by an earlier
    answer and the starts (epoch ms, comma-separated) of the finished profiles held from it, sent back without rows.
    s: the settings to use (JSON, as the chart shows them: a chart tab can have its own) instead of the saved ones."""
    from .vp_service import KINDS
    m = M()
    sym = symbol.upper()
    if kind not in KINDS:
        raise HTTPException(404, f"no volume profile {kind!r}")
    if sym not in m.instruments:
        raise HTTPException(404, f"unknown symbol {symbol}")
    try:
        held = {int(x) for x in have.split(",")[:200] if x}
    except ValueError:
        raise HTTPException(400, "have: epoch ms, comma-separated") from None
    settings = None
    if s:
        from .indicators import clean
        try:
            settings = clean(kind, json.loads(s[:20_000]))
        except (ValueError, TypeError) as e:
            raise HTTPException(400, f"s: {e}") from None
    return await m.vp.get(kind, sym, key[:64], held, settings)


@app.get("/api/profile")
async def profile(symbol: str, start: int | None = None, end: int | None = None, sessions: int | None = None,
                  rth: bool = False):
    """Volume profile for [start, end) in epoch ms (visible range), or for the last N sessions (composite); rth: their
    regular hours only (an RTH chart's bars)."""
    m = M()
    sym = symbol.upper()
    if sym not in m.instruments:
        raise HTTPException(404, f"unknown symbol {symbol}")
    now = now_ms()
    if sessions:
        start = previous_session_start(now + 1)   # the session trading now, or the last one (weekend, break)
        for _ in range(max(1, min(sessions, 60)) - 1):
            start = previous_session_start(start)
        end = now + 1
    if start is None:
        raise HTTPException(400, "give start (and end) or sessions")
    end = end or now + 1
    if end <= start or end - start > 90 * DAY_MS:
        raise HTTPException(400, "range must be positive and at most 90 days")
    return await m.range_profile(sym, start, end, rth) or {}


class AmtAlertsIn(BaseModel):
    on: bool


@app.get("/api/amt")
async def amt_info():
    """The Toolkit's version, its settings (schema: groups, labels, choices and bounds as in NinjaTrader; defaults:
    NinjaTrader's), this server's changes from them and the per-symbol alert switches."""
    from . import amt
    from .amt_hub import DEFAULTS, SCHEMA, SHARED
    m = M()
    return {"version": amt.VERSION, "schema": SCHEMA, "defaults": DEFAULTS, "overrides": m.amt.overrides, "shared": sorted(SHARED),
            "alerts": m.amt.alert_cfg, "runners": m.amt.health()}


def _amt_said(m):
    """Every page hears the shared AMT settings and the per-symbol alert switches as they are now (a layout's page, a
    chart not showing the Toolkit, another device), so none saves over them from an old copy."""
    m.broadcast({"type": "amt_settings", "overrides": m.amt.overrides, "alerts": m.amt.alert_cfg})


@app.put("/api/amt/settings")
async def amt_settings(overrides: dict):
    """The shared settings, all of them: what is left out goes back to NinjaTrader's default."""
    m = M()
    try:
        m.amt.set_overrides(overrides)
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, str(e).strip("'\""))
    _amt_said(m)
    return {"overrides": m.amt.overrides}


@app.patch("/api/amt/settings")
async def amt_settings_change(changes: dict):
    """Only the shared settings that changed, over the server's as they are now (one at its default goes back to it)."""
    m = M()
    try:
        m.amt.merge_overrides(changes)
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, str(e).strip("'\""))
    _amt_said(m)
    return {"overrides": m.amt.overrides}


@app.post("/api/amt/settings/check")
async def amt_settings_check(overrides: dict):
    """A chart's own AMT settings, checked as the Toolkit does, without saving them: the chart keeps them."""
    try:
        return {"overrides": M().amt.chart_settings(overrides)[1]}
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, str(e).strip("'\""))


class LevelsPasteIn(BaseModel):
    text: str = Field("", max_length=1_048_576)


@app.get("/api/amt/levels/{symbol}")
async def amt_levels(symbol: str):
    """Where the symbol's daily levels come from: the levels file or link, else its pasted line."""
    return M().daily.status(_ifthen_symbol(symbol))


@app.put("/api/amt/levels/{symbol}")
async def amt_levels_paste(symbol: str, body: LevelsPasteIn):
    """The symbol's "Paste Levels Here" line, as TradingView takes it (date, pivot, prices) or the labeled block."""
    m = M()
    sym = _ifthen_symbol(symbol)
    try:
        m.daily.set_paste(sym, body.text)
    except ValueError as e:
        raise HTTPException(400, f"Levels rejected: {e}") from None
    m.amt.levels_changed(sym)
    m.levels_changed()   # the Daily levels study's and level alerts' too
    status = m.daily.status(sym)
    m.broadcast({"type": "amt_levels", "symbol": sym, "status": status})
    return status


@app.put("/api/amt/alerts/{symbol}")
async def amt_alerts(symbol: str, body: AmtAlertsIn):
    m = M()
    sym = symbol.upper()
    if sym not in m.instruments:
        raise HTTPException(404, f"unknown symbol {symbol}")
    m.amt.set_alerts(sym, body.on)
    _amt_said(m)
    return m.amt.alert_cfg[sym]


@app.get("/api/tpo")
async def tpo_profile(request: Request, symbol: str, spec: str, rth: bool = False):
    """Tatanka TPO Profile for a minute or hour chart. Any TPO setting can be added as a query parameter,
    e.g. &SessionTime=0800-1330&ShowLive=true. rth: an RTH-only chart's (its regular-hours bars and daily bars)."""
    from . import tpo
    m = M()
    sym = symbol.upper()
    if sym not in m.instruments:
        raise HTTPException(404, f"unknown symbol {symbol}")
    minutes = _time_chart(spec)
    overrides = {}
    for k, v in request.query_params.items():
        if k in ("symbol", "spec", "rth"):
            continue
        d = tpo.DEFAULTS.get(k)
        if isinstance(d, bool):
            v = {"true": True, "false": False}.get(v.lower(), v)
        elif isinstance(d, int):
            try:
                v = int(v)
            except ValueError:
                pass
        overrides[k] = v
    try:
        s = tpo.settings(overrides)
    except ValueError as e:
        raise HTTPException(400, str(e))
    s["SessionTime"] = sessions.move_session(s["SessionTime"], sessions.rth_mover(m.instruments[sym].rth))   # its own hours
    return await m.tpo.get(sym, minutes, s, rth)


def _time_chart(spec: str):
    from .bars import parse_spec
    try:
        sp = parse_spec(spec)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if sp.kind != "time" or sp.cal or sp.size % 60:
        raise HTTPException(400, "use a minute or hour chart")
    return sp.size // 60


@app.get("/api/gaps")
async def gap_analysis(symbol: str, spec: str, RthStart: int = 83000, RthEnd: int = 150000,
                       MinimumGapPoints: float = 0.0, days: int = 90, rth: bool = False):
    """Tatanka Gap Analysis over the last `days` of the chart's bars, forming bar included. rth: an RTH-only
    chart's bars (the gap opens at its first regular-hours bar)."""
    from . import gaps
    from .gaps_service import DAYS_MAX
    m = M()
    sym = symbol.upper()
    if sym not in m.instruments:
        raise HTTPException(404, f"unknown symbol {symbol}")
    minutes = _time_chart(spec)
    if not 1 <= days <= DAYS_MAX:
        raise HTTPException(400, f"days must be 1-{DAYS_MAX}")
    from .gaps_service import check_hours, own_hours
    try:
        s = gaps.settings({"RthStart": RthStart, "RthEnd": RthEnd, "MinimumGapPoints": MinimumGapPoints})
        check_hours(s, {sym: m.instruments[sym]})   # times its own hours make no session of
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await m.gaps.get(sym, minutes, own_hours(s, m.instruments[sym].rth), days, rth)


class GapSettingsIn(BaseModel):                     # what is left out stays as it is
    new: bool | None = None                         # the alert switches, every chart's: sent when changed
    fill: bool | None = None
    RthStart: int | None = None
    RthEnd: int | None = None
    MinimumGapPoints: float | None = Field(None, ge=0)


class GapAlertsIn(BaseModel):                       # the alert switches are every chart's (PUT /api/gaps/alerts)
    on: bool
    rth: bool = False                               # regular-hours minutes only, as the RTH chart it was switched on from
    RthStart: int = 83000
    RthEnd: int = 150000
    MinimumGapPoints: float = Field(0.0, ge=0)


def _gaps_said(m):
    """Every page hears the gap alerts as they are now (each symbol's and the alert switches), as _amt_said."""
    m.broadcast({"type": "gap_alerts", "alerts": m.gaps.shown(), "switches": m.gaps.switches})


@app.get("/api/gaps/alerts")
async def gap_alerts_info():
    m = M()
    return {"alerts": m.gaps.shown(), "switches": m.gaps.switches, "watches": m.gaps.health(), "notice": m.gaps.notice}


@app.delete("/api/gaps/alerts/notice")
async def gap_alerts_told():
    """The alert switches the first start turned off, from each symbol's alerts as saved before they were every chart's:
    the page that asks first says so; any after gets none."""
    return {"notice": M().gaps.told()}


@app.put("/api/gaps/alerts")
async def gap_alerts_follow(body: GapSettingsIn):
    """A chart's Gap settings saved: every symbol's alerts take them, each keeping its own on and rth (the page's copy
    of those is from its hello; another chart, tab or device may have switched them since)."""
    m = M()
    try:
        m.gaps.follow({k: v for k, v in body.model_dump().items() if v is not None})
    except ValueError as e:
        raise HTTPException(400, str(e))
    _gaps_said(m)
    return {"alerts": m.gaps.shown(), "switches": m.gaps.switches}


@app.put("/api/gaps/alerts/{symbol}")
async def gap_alerts(symbol: str, body: GapAlertsIn):
    m = M()
    sym = symbol.upper()
    if sym not in m.instruments:
        raise HTTPException(404, f"unknown symbol {symbol}")
    try:
        m.gaps.set_alerts(sym, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    _gaps_said(m)
    return m.gaps.alert_cfg[sym]


@app.get("/api/htfema")
async def htf_ema(symbol: str, spec: str, start: int, HtfPeriodType: str = "Minute", HtfPeriod: int = 60,
                  EmaLength: int = 21, Mode: str = "SmoothScaled", rth: bool = False):
    """Tatanka 1hr 21 EMA: the higher-timeframe EMA and, for time charts, the chart EMA up to `start` (the
    chart's first bar, epoch ms; on seconds charts partly from stored trades). The browser computes the line from
    these and its own bars. rth: an RTH-only chart's (both from the regular hours, the sessions ending at the RTH
    close)."""
    from itertools import chain

    from . import htfema
    from .bars import make_series, parse_spec
    from .store import iter_bars_1m
    m = M()
    sym = symbol.upper()
    if sym not in m.instruments:
        raise HTTPException(404, f"unknown symbol {symbol}")
    try:
        sp = parse_spec(spec)
        s = htfema.settings({"HtfPeriodType": HtfPeriodType, "HtfPeriod": HtfPeriod, "EmaLength": EmaLength, "Mode": Mode})
    except ValueError as e:
        raise HTTPException(400, str(e))
    scaled = htfema.GetScaledLength(s, *htfema.chart_period_spec(sp))
    chart_minutes = sp.size // 60 if sp.kind == "time" and sp.cal not in ("W", "M") and sp.size >= 60 else None
    chart_seconds = sp.size if sp.kind == "time" and sp.size < 60 else None   # 1s-59s: bars built from trades
    now = now_ms()
    inst = m.instruments[sym]
    hours = inst.rth if rth else None   # an RTH chart: its regular hours only (none for an instrument without them)
    since = htfema.history_ms(s, chart_minutes, scaled, start, now, chart_seconds, hours)
    split, live = m.recent_minutes(sym)
    db = m.store.path
    bucket = make_series(sp, inst.tick_size, inst.decimals, 1, inst.rth, rth_only=bool(hours),
                         group=inst.calendar).bucket if sp.cal else None
    if chart_seconds and s["Mode"] == "SmoothScaled":
        # the seed from stored trades, seconds of CPU: in a chart worker, once for frames and devices asking the same
        d = await htfema.shared((sym, sp.text, bool(hours), start, *s.values()), now, lambda: workers.run(
            "chart", htfema.seconds_job, db, sym, s, since, split, live, scaled, start, now, inst.calendar, chart_seconds,
            hours))
    else:
        d = await asyncio.to_thread(lambda: htfema.compute(
            s, chain(iter_bars_1m(db, sym, since, split), live), chart_minutes, scaled, start, now, inst.calendar,
            bucket, chart_seconds, rth=hours))
    nxt = (htfema.next_open(bucket, now, hours)
           if sp.cal and sp.text != "1D" else None)   # 2D, 3D, W, M: the next bar's open, once the last is over
    return {**d, "next": nxt}   # a copy: a seconds chart's answer is shared


@app.get("/api/dsma")
async def daily_sma(symbol: str, start: int, length: int = 200, rth: bool = False):
    """Tatanka Daily SMA x4: the daily bars (open, close time, close) its SMAs of up to `length` days need for a
    chart starting at `start` (epoch ms), to the forming one. The browser computes the lines from these and the live
    price. rth: an RTH-only chart's (one bar per regular session)."""
    from . import dsma
    from .store import iter_closes_1m
    m = M()
    sym = symbol.upper()
    if sym not in m.instruments:
        raise HTTPException(404, f"unknown symbol {symbol}")
    if not 1 <= length <= dsma.MAX_DAYS:
        raise HTTPException(400, f"length must be 1-{dsma.MAX_DAYS}: NinjaTrader loads {dsma.MAX_DAYS} daily bars")
    i = m.instruments[sym]
    split, live = m.recent_minutes(sym)
    return await asyncio.to_thread(dsma.compute, m.store.path, sym, i.calendar, i.rth if rth else None, start, length,
                                   split, [(b.t, b.c) for b in live], now_ms(), iter_closes_1m)


class FootprintIn(BaseModel):
    symbol: str
    starts: list[int] = Field(min_length=1, max_length=400)
    end: int
    vols: list[float] | None = Field(None, max_length=400)   # tick, volume, range and Renko bars: each one's volume
    pre: int = Field(0, ge=0)   # and the first one's pre: the contracts at its open time that went to the bars before it
    rth: bool = False   # an RTH chart: only the trades in its regular hours, as its bars
    spec: str | None = Field(None, max_length=16)   # the chart's: a D chart's bars hold their trade dates' sessions


FOOTPRINT_MAX_MS = 8 * DAY_MS


@app.post("/api/footprint")
async def footprint(body: FootprintIn):
    """Bid and ask volume per price for the bars opening at `starts` (the last one runs to `end`), from the
    stored trades plus those not yet written."""
    from . import footprint as fp
    from .bars import make_series, parse_spec
    from .store import iter_ticks_range
    from .studies import RthClock
    m = M()
    sym = body.symbol.upper()
    inst = m.instruments.get(sym)
    if not inst:
        raise HTTPException(404, f"unknown symbol {body.symbol}")
    s = body.starts
    if any(b < a for a, b in zip(s, s[1:])) or body.end <= s[-1]:   # bars can share an open time (their vols split it)
        raise HTTPException(400, "starts must not go back and end must come after the last one")
    if body.vols is not None and len(body.vols) != len(s):
        raise HTTPException(400, "vols: one for each start")
    if body.end - s[0] > FOOTPRINT_MAX_MS:   # every trade in the span is read
        raise HTTPException(400, "footprints cover at most 8 days of trades a request")
    # A D, W or M chart's bar holds the sessions of its trade dates, as the chart's bars are made: a holiday halt's
    # session (Thanksgiving's) is in the next day's bar, stamped at that one's open, after the trades.
    bucket, lo = None, s[0]
    try:
        spec = parse_spec(body.spec) if body.spec else None
    except ValueError as e:
        raise HTTPException(400, str(e))
    if spec and spec.kind == "time" and spec.cal:
        bucket = make_series(spec, inst.tick_size, inst.decimals, 1, inst.rth, studies=False, rth_only=bool(body.rth and inst.rth),
                             group=inst.calendar).bucket
        k = sessions.session_start(s[0])
        if (first := sessions.trade_key(k, inst.calendar)) != k:   # the first bar's: from the halt session before it
            lo = first
    # Same cut as chart history: rows written so far from the database, the rest from the write buffer.
    cutoff = m.store.max_tick_rowid()
    buf = [tk for tk in m.tick_buf if tk.symbol == sym and lo <= tk.ts < body.end]
    db = m.store.path

    def work():   # streamed in time order: a day of trades never sits in memory at once
        ticks = heapq.merge(iter_ticks_range(db, sym, lo, body.end, cutoff), sorted(buf, key=lambda tk: tk.ts),
                            key=lambda tk: tk.ts)
        if body.rth and inst.rth:   # the night between two sessions is no bar's (the last one's would take it)
            clock = RthClock(inst.rth)
            ticks = (tk for tk in ticks if clock.day(tk.ts) is not None)
        return fp.compute(ticks, s, body.end, inst.tick_size, body.vols, body.pre, bucket)
    return {"tick": inst.tick_size, "bars": await asyncio.to_thread(work)}


@app.get("/api/replay")
async def replay(symbol: str, spec: str, at: int, rth: bool = False):
    """Bars around a past moment for bar replay: about 1500 before it and 1500 after (tick, volume and range
    bars: at most to the end of its session), plus `next_at`, the moment to ask for to play on (null at the present),
    and `end_at`, where the last bar ends if not there (tick, volume and range bars: at the present the open of the one
    still forming, else where its hours end when no trade can join it: the RTH close on an RTH chart, the session's
    close). A tick, volume or range bar that the next one opens a session later than has `e`, where its hours end."""
    from . import replay as rp
    from .bars import parse_spec
    m = M()
    sym = symbol.upper()
    inst = m.instruments.get(sym)
    if not inst:
        raise HTTPException(404, f"unknown symbol {symbol}")
    try:
        bs = parse_spec(spec)
    except ValueError as e:
        raise HTTPException(400, str(e))
    now = now_ms()
    if not 0 < at < now:
        raise HTTPException(400, "at must be in the past")
    rth_only = bool(rth and inst.rth)
    bars, next_at, end_at = await asyncio.to_thread(rp.build, m.store.path, sym, inst, bs, at, now,
                                                    m.store.max_tick_rowid(), rth_only)
    out = [b.to_dict() for b in bars]
    for i, e in ({} if bs.from_minutes else rp.hours_ends(bars, inst, rth_only)).items():
        out[i]["e"] = e
    return {"symbol": sym, "spec": bs.text, "kind": bs.kind, "at": at, "next_at": next_at, "end_at": end_at,
            "bars": out}


@app.get("/api/fills")
async def fills(start: int, end: int, symbol: str | None = None, account: str | None = None):
    """Your NinjaTrader fills (read from the add-on; this app places no orders) for chart markers."""
    m = M()
    return {"fills": m.store.fills(start, end, symbol.upper() if symbol else None, account or None),
            "accounts": m.store.fill_accounts()}


@app.get("/api/journal")
async def journal_get(start: int, end: int, account: str | None = None, symbol: str | None = None,
                      excursions: bool = True, flat: bool = False):
    """Your trades from start to end (ms), flat to flat, with P&L, MAE/MFE (excursions=false: without, as the
    chart's lines need) and daily totals, and `open_since`: the entry of the oldest trade still open (null: none).
    flat=true: every position was flat at `start` (the chart asking again from there after a new fill). NinjaTrader's
    own positions (add-on 13) put right a fill the app never got: from_position and unseen_exit on a trade."""
    from . import journal
    from .gapfill import ROLLED
    from .store import connect
    m = M()
    sym = symbol.upper() if symbol else None
    ticks = {s: i.tick_size for s, i in m.instruments.items()}
    rolled = m.store.get_setting(ROLLED, {}) or {}   # each symbol's last contract roll
    rolled = rolled if isinstance(rolled, dict) else {}

    def work():   # in one thread on one connection: every open chart asks again on each new fill
        db = connect(m.store.path, readonly=True)
        db.row_factory = sqlite3.Row
        try:
            # a trade that began before `start` and closed inside it needs its earlier fills, back to where it was flat
            q = (start, end, sym, account or None)
            fl, pos = (m.store.fills(*q, db=db), m.store.positions(*q, db=db)) if flat else m.store.from_flat(*q, db=db)
            every = journal.trades(fl, pos)
            trs = [t for t in every if (t["exit_ts"] or t["entry_ts"]) >= start]
            if excursions:
                for t in trs:
                    if t["symbol"] in ticks:
                        journal.excursions(m.store.path, t, ticks[t["symbol"]], db, end, int(rolled.get(t["symbol"], 0)))
            return trs, journal.stats(trs), min((t["entry_ts"] for t in every if t["open"]), default=None)
        finally:
            db.close()
    trs, stats, open_since = await asyncio.to_thread(work)
    return {"trades": trs, "stats": stats, "open_since": open_since, "accounts": m.store.fill_accounts()}


@app.get("/api/trades")
async def trades(symbol: str, start: int, end: int, min: int = 1, limit: int = 5000, newest: bool = False):
    """Stored trades for the tape and big-trade bubbles: fills at the same millisecond and side grouped into one
    (an order sweeping the book), of at least `min` contracts, over at most 3 days."""
    from .store import big_trades
    m = M()
    sym = symbol.upper()
    if sym not in m.instruments:
        raise HTTPException(404, f"unknown symbol {symbol}")
    if not 0 <= end - start <= 3 * DAY_MS:
        raise HTTPException(400, "start..end must be 0 to 3 days")
    if not 1 <= min <= 100_000 or not 1 <= limit <= 20_000:
        raise HTTPException(400, "min 1-100000, limit 1-20000")
    m.flush_for_read()   # the last second's trades are in the database too
    rows = await asyncio.to_thread(big_trades, m.store.path, sym, start, end, min, limit, newest)
    tick = m.instruments[sym].tick_size
    return {"symbol": sym, "trades": [[t, round(round(p / tick) * tick, 10), s, sd, n] for t, p, s, sd, n in rows]}


class IfThenIn(BaseModel):
    rows: str = Field("", max_length=20_000)


def _ifthen_symbol(symbol: str) -> str:
    sym = symbol.upper()
    if sym not in M().instruments:
        raise HTTPException(404, f"unknown symbol {symbol}")
    return sym


@app.get("/api/ifthen/{symbol}")
async def ifthen_get(symbol: str, atr: int = 14):
    """The symbol's pasted If/Then block and the previous day's ATR(atr) for the Position colors and zones."""
    from . import ifthen
    m = M()
    sym = _ifthen_symbol(symbol)
    if not 1 <= atr <= 500:
        raise HTTPException(400, "atr must be 1-500")
    now = now_ms()
    cur = session_start(now)
    last = m.m1[sym].last
    today = bool(last and last.t >= sessions.trade_key(cur, m.instruments[sym].calendar))   # today's trade date
    daily = await asyncio.to_thread(m.tpo.daily_for, sym, cur)
    block = m.store.get_setting("ifthen", {}).get(sym) or {}
    return {"version": ifthen.VERSION, "rows": block.get("rows", ""), "updated": block.get("updated"),
            "atr": ifthen.previous_atr(daily.hlc, atr, today)}


@app.put("/api/ifthen/{symbol}")
async def ifthen_put(symbol: str, body: IfThenIn):
    """Saves the block for every device; open charts get it right away."""
    m = M()
    sym = _ifthen_symbol(symbol)
    all_blocks = m.store.get_setting("ifthen", {})
    all_blocks[sym] = {"rows": body.rows, "updated": now_ms()}
    m.store.set_setting("ifthen", all_blocks)
    m.broadcast({"type": "ifthen", "symbol": sym, **all_blocks[sym]})
    return all_blocks[sym]


def _drawing_symbol(symbol: str) -> str:
    """A live chart's symbol, or any one the app charts from free data (AAPL, FRED:DGS10)."""
    from .ext import chart_symbol
    sym = symbol.upper()
    if sym in M().instruments:
        return sym
    try:
        return chart_symbol(symbol, M().instruments, M().symbols.only_futures)
    except ValueError:
        raise HTTPException(404, f"unknown symbol {symbol}") from None


def _drawing_keys(m, sym: str) -> list[str]:
    """The symbols a chart's drawings are stored under, the chart's own first (sym: _drawing_symbol's, or an alert's).
    A root only a future has (ZB, NQ: no stock's ticker, the symbol database's only_futures) and that future (ZB1!)
    are one chart's, ZB's while it runs here and ZB1!'s on free data: it shows the lines drawn on an older list's bare
    ZB chart (stored as ZB), on its free-data chart and on it live, each left where it was stored, and a drawn line's
    alert (NQ's) still finds its line on the free-data chart once NQ is not live. A root a stock has too (NG,
    NovaGold's) is never one with its future: its chart is the stock's while the future is not live."""
    root = wl.future_root(sym) or sym
    if root not in m.symbols.only_futures:
        return [sym]
    return [root, f"{root}1!"] if root in m.instruments else [f"{root}1!", root]


def _drawings_of(m, keys: list[str]) -> list[dict]:
    """The chart's drawings (keys: _drawing_keys), in id order as one symbol's come; an id stored under both is the
    chart's own key's."""
    got = {}
    for k in reversed(keys):
        got.update((d["id"], d) for d in m.store.list_drawings(k))
    return [got[i] for i in sorted(got)]


def _find_drawing(m, keys: list[str], drawing_id: str) -> tuple[str, dict | None]:
    """Where one of the chart's drawings is stored, and it; the chart's own key and None for a new one."""
    for k in keys:
        d = next((x for x in m.store.list_drawings(k) if x["id"] == drawing_id), None)
        if d is not None:
            return k, d
    return keys[0], None


def _sync_drawing_alert(m, keys: list[str], d: dict | None, drawing_id: str):
    """Keep the drawing's `line` alert in step: created when switched on, moved (and re-armed) with the drawing,
    removed with it or when switched off. keys: the chart's (_drawing_keys). One is created on a live chart (alerts
    run on live trades), as its symbol's, or on the free-data chart of a future-only root (NQ1!, which has no alert
    box: a line put back there with Ctrl+Z after a delete) as the root's, NQ's, for when NQ is live again; one made
    while NQ ran here keeps its symbol, and follows its line on the free-data NQ1! chart too."""
    from . import drawings
    linked = [a for a in m.alerts.alerts.values()
              if a.kind == "line" and a.symbol in keys and a.params.get("drawing") == drawing_id]
    live = next((k for k in keys if k in m.instruments), None)
    own = live or (keys[-1] if len(keys) > 1 else None)   # the free-data chart of a future-only root: the root's

    # The line's prices on the tick grid trades come in on: the browser's round(p / tick) * tick is often a float
    # beside the level (2000.1000000000001 on GC), which a trade exactly at the line does not reach. A future not
    # live here now (taken off Instruments): on the tick the futures list gives it.
    def on_grid(p, sym):
        inst = m.instruments.get(sym) or next((Instrument(sym, r["name"], r["tick_size"], r["point_value"])
                                               for r in futures_catalog(m.s.config_dir) if r["symbol"] == sym), None)
        return {**p, "p1": inst.round(p["p1"]), "p2": inst.round(p["p2"])} if inst else p

    params = drawings.alert_params(d) if d else None
    note = ((d or {}).get("style", {}).get("text") or "")[:200]
    if params is None:
        for a in linked:
            m.store.delete_alert(a.id)
    elif not linked:
        if own is None:   # a line on a free-data chart
            return
        m.store.add_alert(own, "line", on_grid(params, own), d["alert"]["repeat"], note)
    else:
        a = linked[0]
        params = on_grid(params, a.symbol)
        changes = {"params": params, "repeat": d["alert"]["repeat"], "note": note}
        if on_grid(a.params, a.symbol) != params:   # moved (one saved off the grid before has not)
            changes["enabled"] = True
        m.store.update_alert(a.id, **changes)
        for extra in linked[1:]:
            m.store.delete_alert(extra.id)
    if linked or params is not None:
        m.alerts_changed()


@app.get("/api/drawings/{symbol}")
async def drawings_list(symbol: str):
    m = M()
    return {"drawings": _drawings_of(m, _drawing_keys(m, _drawing_symbol(symbol)))}


@app.put("/api/drawings/{symbol}/{drawing_id}")
async def drawing_put(symbol: str, drawing_id: str, body: dict):
    """Creates or replaces a drawing; every open chart of the symbol gets it."""
    from . import drawings
    m = M()
    sym = _drawing_symbol(symbol)
    keys = _drawing_keys(m, sym)
    try:
        d = drawings.clean(body, drawing_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    known = {x["id"] for x in _drawings_of(m, keys)}
    if drawing_id not in known and len(known) >= drawings.MAX_PER_SYMBOL:
        raise HTTPException(400, f"at most {drawings.MAX_PER_SYMBOL} drawings per symbol")
    m.store.put_drawing(_find_drawing(m, keys, drawing_id)[0], d)   # where it is stored already
    _sync_drawing_alert(m, keys, d, drawing_id)
    m.broadcast({"type": "drawing", "symbol": sym, "drawing": d})
    return d


@app.delete("/api/drawings/{symbol}/{drawing_id}", status_code=204)
async def drawing_delete(symbol: str, drawing_id: str):
    m = M()
    sym = _drawing_symbol(symbol)
    keys = _drawing_keys(m, sym)
    if not [k for k in keys if m.store.delete_drawing(k, drawing_id)]:   # an id stored under both: gone from both
        raise HTTPException(404, "no such drawing")
    _sync_drawing_alert(m, keys, None, drawing_id)
    m.broadcast({"type": "drawing_deleted", "symbol": sym, "ids": [drawing_id]})


@app.delete("/api/drawings/{symbol}")
async def drawings_clear(symbol: str, group: str | None = None):
    """Deletes all of the symbol's drawings, or those of one group (e.g. pasted levels)."""
    m = M()
    sym = _drawing_symbol(symbol)
    keys = _drawing_keys(m, sym)
    ids = [d["id"] for d in _drawings_of(m, keys) if group is None or d["style"].get("group") == group]
    for i in ids:
        for k in keys:
            m.store.delete_drawing(k, i)
        _sync_drawing_alert(m, keys, None, i)
    m.broadcast({"type": "drawing_deleted", "symbol": sym, "ids": ids})
    return {"deleted": len(ids)}


@app.get("/api/alerts")
async def list_alerts():
    return M().alerts.list()


@app.post("/api/alerts", status_code=201)
async def create_alert(a: AlertIn):
    m = M()
    sym = a.symbol.upper()
    inst = m.instruments.get(sym)
    if not inst:
        raise HTTPException(400, f"unknown symbol {a.symbol}")
    if a.kind == "price":
        if a.price is None:
            raise HTTPException(400, "price alerts need a price")
        params = _price_params(inst, a.price, a.direction, a.price2)
        repeat = bool(a.repeat)
    elif a.kind == "ind":
        from .ind_alerts import clean_params
        if a.rth and not inst.rth:
            raise HTTPException(400, f"{sym} has no RTH session configured")
        try:
            params = clean_params(a.a, a.b, a.spec, a.on, a.direction if a.direction not in ("enter", "exit") else "?", a.rth)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from None
        repeat = bool(a.repeat)
    elif a.kind == "study":
        if a.study is None:
            raise HTTPException(400, "study alerts need a study: vwap, rth_vwap, poc, vah or val")
        if (a.study == "rth_vwap" or a.study in ("poc", "vah", "val") and a.profile == "rth") and not inst.rth:
            raise HTTPException(400, f"{sym} has no RTH session configured")
        params = {"study": a.study, "tolerance_ticks": a.tolerance_ticks, "rearm_ticks": a.rearm_ticks}
        if a.study in ("vwap", "rth_vwap"):
            params["band"] = a.band
        else:
            params.update(profile=a.profile, which=a.which)
        repeat = True if a.repeat is None else a.repeat
    else:
        params = {"levels": a.levels, "tolerance_ticks": a.tolerance_ticks, "rearm_ticks": a.rearm_ticks}
        repeat = True if a.repeat is None else a.repeat
    if _expiry(a.expires):
        params["expires"] = a.expires
    alert_id = m.store.add_alert(sym, a.kind, params, repeat, a.note)
    m.alerts_changed()
    return next(x for x in m.alerts.list() if x["id"] == alert_id)


@app.get("/api/indicators")
async def indicators_get():
    """Every Tatanka indicator's NinjaTrader settings (schema with defaults), what differs, and per-symbol values."""
    return M().ind.snapshot()


@app.put("/api/indicators/{key}")
async def indicators_put(key: str, overrides: dict):
    m = M()
    try:
        saved = m.ind.put(key, overrides)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    m.broadcast({"type": "indicators", "key": key, "overrides": saved})
    return saved


@app.put("/api/indicators/{key}/{symbol}")
async def indicators_put_symbol(key: str, symbol: str, values: dict):
    """A symbol's own values: the If/Then block and instrument label, Level Lines' range and levels."""
    m = M()
    sym = _ifthen_symbol(symbol)
    try:
        saved = m.ind.put_symbol(key, sym, values, now_ms())
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    m.broadcast({"type": "indicators", "key": key, "symbol": sym, "values": saved})
    if key == "ifthen" and "RowsBlock" in values:
        m.broadcast({"type": "ifthen", "symbol": sym, **m.store.get_setting("ifthen", {})[sym]})
    return saved


@app.get("/api/symbols/search")
async def symbols_search(q: str = "", type: str = "", limit: int = 50):
    """The symbol database: symbols whose ticker or name match q, best first; `type` narrows it (future, stock, etf,
    index, forex, crypto, economic). A search that finds little asks Yahoo (and FRED) once a day."""
    try:
        return {"results": await M().symbols.search(q, type, limit)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/symbols/status")
async def symbols_status():
    """What the search box knows: symbols per type, and each source (rows, last refresh, last error)."""
    m = M()
    return {"counts": m.symbols.count(), "refreshed": m.symbols.refreshed(), "online": m.symbols.online,
            "fred_search": bool(m.s.fred_api_key), "sources": m.symbols.status()}


@app.post("/api/symbols/refresh")
async def symbols_refresh():
    """Every source of the symbol list asked again now (in the background; Connections > Data > Symbol list)."""
    m = M()
    m.symbols.refresh_soon()
    return {"sources": list(m.symbols.sources())}


@app.get("/api/calendar")
async def calendar(days: int = 14):
    """Economic releases from a week back to `days` ahead (CT times are the browser's to show), and the warning
    settings."""
    c, er, now = M().calendar, M().earnings, now_ms()
    start, end = now - 7 * DAY_MS, now + max(1, min(days, 21)) * DAY_MS
    return {"events": c.between(start, end), "settings": c.cfg, "error": c.error, "fetched": int(c.fetched * 1000),
            "earnings": er.between(start, end), "earnings_settings": er.cfg, "earnings_error": er.error}


@app.put("/api/calendar/settings")
async def calendar_settings(body: dict):
    try:
        cfg = M().calendar.put_settings(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    M().broadcast({"type": "calendar"})
    return cfg


@app.put("/api/calendar/earnings")
async def earnings_settings(body: dict):
    """Major earnings: {on, min_cap_b (market cap floor, $ billions), symbols (tickers shown whatever their size)}."""
    try:
        cfg = M().earnings.put_settings(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    M().broadcast({"type": "calendar"})
    return cfg


@app.get("/api/cot")
async def cot(symbol: str):
    """CFTC Commitments of Traders (Legacy, futures only) for a futures symbol: weekly net positions of commercials,
    large speculators and small traders, stamped when each week was published."""
    try:
        return await M().cot.weeks(symbol)
    except ValueError as e:
        raise HTTPException(404, str(e)) from None


@app.get("/api/ext/bars")
async def ext_bars(symbol: str, spec: str = "1D", rth: bool = True):
    """Bars for a symbol with no live chart here: Alpaca (US stocks intraday, real time, with its keys), Yahoo
    (delayed), FRED or EIA. rth=false: with a stock's pre- and post-market."""
    from .ext import chart_symbol
    m = M()
    try:
        return await m.ext.bars(chart_symbol(symbol, m.instruments, m.symbols.only_futures), spec, rth)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


class WorkspaceIn(BaseModel):
    name: str | None = None
    data: dict | None = None
    by: str = ""


@app.get("/api/workspaces")
async def workspaces_list(full: bool = False):
    """Saved workspaces: id, name, when and from which device last saved; full=true adds what each holds."""
    ws = M().workspaces
    return {"workspaces": ws.items if full else ws.summary()}


@app.get("/api/workspaces/{wid}")
async def workspace_get(wid: str):
    w = M().workspaces.get(wid)
    if w is None:
        raise HTTPException(404, "no such workspace")
    return w


@app.put("/api/workspaces/{wid}")
async def workspace_put(wid: str, body: WorkspaceIn):
    """Creates or replaces one; name or data left out keep theirs. Every open app hears of it."""
    m = M()
    try:
        w = m.workspaces.put(wid, body.name, body.data, body.by, now_ms())
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    m.broadcast({"type": "workspaces", "workspaces": m.workspaces.summary(), "saved": wid, "by": w["by"]})
    return {k: w[k] for k in ("id", "name", "updated", "by")}


@app.delete("/api/workspaces/{wid}")
async def workspace_delete(wid: str):
    m = M()
    if not m.workspaces.delete(wid):
        raise HTTPException(404, "no such workspace")
    m.broadcast({"type": "workspaces", "workspaces": m.workspaces.summary()})
    return {"ok": True}


@app.get("/api/watchlists")
async def get_watchlists():
    return M().wl


@app.put("/api/watchlists")
async def put_watchlists(doc: dict):
    """The whole watchlist document (lists, sections, flags), as the app edits it, and base: the version it was
    edited on (GET's, a broadcast's). Another device's save since is a 409: the app merges its edits into the lists
    as they are now and saves again. Without base (an app from before versions) last write wins."""
    base = doc.pop("base", None)
    if base is not None and type(base) is not int:
        raise HTTPException(400, "base: the version the lists were edited on (a whole number)")
    try:
        return M().set_watchlists(doc, base)
    except wl.Stale as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.patch("/api/alerts/{alert_id}")
async def patch_alert(alert_id: int, p: AlertPatch):
    """Pause, arm, change the trigger or message; move a price alert or change its condition (it re-arms);
    set or clear (0) the expiration."""
    m = M()
    a = m.alerts.alerts.get(alert_id)
    if a is None:
        raise HTTPException(404, "no such alert")
    fields = {k: v for k, v in p.model_dump(exclude_none=True).items() if k in ("enabled", "repeat", "note")}
    params = dict(a.params)
    if p.price is not None or p.direction is not None or p.price2 is not None:
        if a.kind != "price":
            raise HTTPException(400, "only price alerts move")
        d = p.direction or params.get("direction", "cross")
        price2 = p.price2 if p.price2 is not None else params.get("price2")
        params.update(_price_params(m.instruments[a.symbol], p.price if p.price is not None else params["price"], d, price2))
        if d not in ("enter", "exit"):
            params.pop("price2", None)
        fields.setdefault("enabled", True)
    if p.tolerance_ticks is not None and "tolerance_ticks" in params:
        params["tolerance_ticks"] = p.tolerance_ticks
    if p.expires is not None:
        if p.expires == 0:
            params.pop("expires", None)
        else:
            params["expires"] = _expiry(p.expires)
            fields.setdefault("enabled", True)
    if fields.get("enabled") and params.get("expires", float("inf")) <= int(time.time() * 1000):
        params.pop("expires")                       # restarting an expired alert: it no longer expires
    if params != a.params:
        fields["params"] = params
    if fields:
        m.store.update_alert(alert_id, **fields)
    m.alerts_changed()
    return next(x for x in m.alerts.list() if x["id"] == alert_id)


@app.delete("/api/alerts/{alert_id}", status_code=204)
async def delete_alert(alert_id: int):
    m = M()
    a = m.alerts.alerts.get(alert_id)
    if not m.store.delete_alert(alert_id):
        raise HTTPException(404, "no such alert")
    if a and a.kind == "line":   # a drawn line's: switched off on the line too, or its next edit would bring it back
        keys = _drawing_keys(m, a.symbol)   # NQ's line may be stored as NQ1!, and charted so while NQ is not live
        key, d = _find_drawing(m, keys, a.params.get("drawing"))
        if d and d["alert"]["on"]:
            d = {**d, "alert": {**d["alert"], "on": False}, "updated": int(time.time() * 1000)}
            m.store.put_drawing(key, d)
            m.broadcast({"type": "drawing", "symbol": keys[0], "drawing": d, "alert_deleted": True})   # nor an undo
    m.alerts_changed()


@app.get("/api/alert-events")
async def alert_events(limit: int = 50):
    return M().store.list_events(max(1, min(limit, 500)))


@app.get("/api/connections")
async def connections_list():
    """Every connection: its settings (secrets only as set or not, and where from), whether it is on, its state."""
    m = M()
    return {"connections": m.connections.public(m)}


@app.post("/api/connections/check")
async def connections_check():
    """Tries every connection in use at once (alert channels get a test message): the report to send along when
    something doesn't work. No keys or URLs in it."""
    m = M()
    return {"at": now_ms(), "feeds": m.feed_names, "results": await conns.check_all(m)}


@app.put("/api/connections/{cid}")
async def connections_put(cid: str, body: dict):
    """Saves one connection's fields ({field: value}; null goes back to .env, "" clears a secret) and applies it."""
    m = M()
    try:
        m.connections.put(cid, body)
    except LookupError:
        raise HTTPException(404, f"no connection {cid!r}") from None
    except ValueError as e:
        raise HTTPException(404 if not m.connections.known(cid) else 400, str(e)) from None
    await m.apply_connections(cid)
    return next(c for c in m.connections.public(m) if c["id"] == cid)


@app.post("/api/connections")
async def connections_add(body: dict):
    """Another channel of a kind: {type: discord | discord_charts | ntfy, name, <its URL field>}. Returns it."""
    m = M()
    try:
        cid = m.connections.add(str(body.get("type", "")), {k: v for k, v in body.items() if k != "type"})
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    await m.apply_connections(cid)
    return next(c for c in m.connections.public(m) if c["id"] == cid)


@app.delete("/api/connections/{cid}", status_code=204)
async def connections_remove(cid: str):
    """Off its list: another channel is deleted; one that isn't a main connection is cleared and turned off."""
    m = M()
    try:
        m.connections.remove(cid)
    except LookupError:
        raise HTTPException(404, f"no connection {cid!r}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    await m.apply_connections(cid)


@app.post("/api/connections/{cid}/test")
async def connections_test(cid: str, body: dict | None = None):
    """Tries the connection with the fields in the body over the saved ones, without saving them."""
    m = M()
    try:
        return await conns.test(m, cid, body or {})
    except ValueError as e:
        raise HTTPException(404 if not m.connections.known(cid) else 400, str(e)) from None


@app.get("/api/symbols/details")
async def symbol_details(symbol: str = ""):
    """The watchlist's details panel for one item: today's range, 52-week range, performance; a live future's contract,
    roll and nearest levels; a stock's next earnings."""
    from .details import details
    if not 1 <= len(symbol.strip()) <= 80:
        raise HTTPException(400, "symbol: 1-80 characters")
    return await details(M(), symbol.strip(), now_ms())


@app.get("/api/addon/TatankaChartFeed.cs")
def addon_file():
    """The NinjaTrader add-on this server expects, to download (Connections > NinjaTrader), with this server's token in
    it when one is set: put in by hand in place of the old one, it connects as that one did (update.cmd keeps it)."""
    m = M()
    path = m.s.web_dir.parent / "ninjatrader" / "TatankaChartFeed.cs"
    if not path.is_file():
        raise HTTPException(404, "the add-on file is not on this server")
    text = path.read_bytes()
    if m.s.nt_token:   # a C# string: its backslashes and quotes escaped
        lit = m.s.nt_token.replace("\\", "\\\\").replace('"', '\\"').encode()
        text = re.sub(rb'(private const string Token\s*=\s*)""', lambda g: g[1] + b'"' + lit + b'"', text, count=1)
    return Response(text, media_type="text/plain; charset=utf-8", headers={
        "Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="TatankaChartFeed.cs"'})


@app.post("/api/connections/{cid}/install")
async def connections_install(cid: str):
    """Installs the Python packages its plugin lists (REQUIRES) and doesn't have, in the background: its card shows
    Installing…, then the connection starts by itself."""
    m = M()
    if not m.connections.known(cid):
        raise HTTPException(404, "no such connection")
    try:
        return {"installing": await m.install_packages(cid)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


class ChartPostIn(BaseModel):
    caption: str = Field("", max_length=1800)
    image: str = Field(..., max_length=12_000_000)   # a PNG, base64 (a data: URL is fine)
    channel: str = Field("discord_charts", max_length=40)   # which chart-post channel (discord_charts-2, ...)


def _chart_channels(m) -> list[dict]:
    return [{"id": c["id"], "name": c["name"] or ("Discord" if not c["extra"] else c["title"])} for c in m.connections.public(m)
            if c["type"] == "discord_charts" and c["state"] != "off"]


@app.get("/api/share/channels")
async def share_channels():
    """The chart-post channels that are set: [{id, name}] (never their URLs)."""
    return {"channels": _chart_channels(M())}


@app.post("/api/share/discord")
async def share_discord(body: ChartPostIn):
    """Post to Discord: a chart picture and caption to a chart-posts webhook (Connections > Discord: chart posts)."""
    m = M()
    if conns.type_of(body.channel) != "discord_charts" or not m.connections.known(body.channel):
        raise HTTPException(400, "no such chart-post channel")
    url = m.connections.url(body.channel, m.s)
    if not url:
        raise HTTPException(400, "Set the webhook first: Connections > Discord: chart posts")
    try:
        png = base64.b64decode(body.image.split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "image: a base64 PNG") from None
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > 8_000_000:
        raise HTTPException(400, "image: a PNG up to 8 MB")
    try:
        ok = await m.notifier.post_chart(url, body.caption.strip(), png, f"{m.branding.get()['name']} Charts", body.channel)
    except httpx.HTTPError as e:   # never the URL: it is the key
        raise HTTPException(502, f"Discord did not answer ({type(e).__name__})") from None
    if not ok:
        raise HTTPException(502, "Discord did not take it: check the webhook in Connections")
    return {"ok": True}


@app.get("/api/branding")
async def branding_get():
    """The Branding settings (the app's name, "Powered by" on chart pictures: branding.DEFAULTS), and whether it has a
    logo of its own (logo_v: its version). Open without signing in: the sign-in shows the name."""
    return M().branding.get()


@app.get("/api/branding/logo")
async def branding_logo():
    """The logo set in Connections > Branding (404 without one: the built-in icons/mark.png applies)."""
    logo = M().branding.logo()
    if not logo:
        raise HTTPException(404, "no logo of its own")
    return Response(logo[0], media_type=logo[1], headers={"Cache-Control": "public, max-age=31536000, immutable",
                                                          "X-Content-Type-Options": "nosniff"})


@app.put("/api/branding")
async def branding_put(body: dict):
    """Any of the Branding settings (branding.DEFAULTS: name, powered) and logo: a PNG, JPEG or WebP (base64 or a data:
    URL), null for the built-in one."""
    m = M()
    try:
        b = m.branding.put(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    m.broadcast({"type": "branding", "branding": b})
    return b


@app.get("/api/backups")
async def backups_list():
    return M().backups.status()


@app.post("/api/backups")
async def backups_now():
    try:
        p = await M().backups.now()
    except (OSError, sqlite3.Error) as e:
        raise HTTPException(500, f"backup failed: {type(e).__name__}") from None
    return {"name": p.name, "size": p.stat().st_size}


class RetentionIn(BaseModel):
    years: int


@app.put("/api/backups/retention")
async def backups_retention(body: RetentionIn):
    """How long each trade is kept (1-minute bars always are): 1, 2, 3, 5 or 10 years, or 0 for always."""
    m = M()
    try:
        m.upkeep.set_years(body.years)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return m.backups.status()


@app.get("/api/backups/{name}")
async def backups_download(name: str):
    """One backup file, to keep elsewhere. It holds the keys saved in Connections."""
    from .backup import NAME_RE, files
    if not M().auth.enabled:   # keys are never handed to a browser that hasn't signed in
        raise HTTPException(403, "turn on Login first (Connections > Login): backups hold your keys")
    p = next((f for f in files(M().backups.folder) if f.name == name), None) if NAME_RE.match(name) else None
    if p is None:
        raise HTTPException(404, "no such backup")
    return FileResponse(p, media_type="application/vnd.sqlite3", filename=p.name)


@app.get("/api/templates")
async def templates_list():
    """Every saved template: {kind: {name: {data, updated}}}; kinds: chart, draw:<tool>, ind:<indicator>."""
    return {"templates": M().templates.all()}


class TemplateIn(BaseModel):
    data: dict


@app.put("/api/templates/{kind}/{name}")
async def templates_put(kind: str, name: str, body: TemplateIn):
    m = M()
    try:
        t = m.templates.put(kind, name, body.data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    m.broadcast({"type": "templates"})
    return t


@app.delete("/api/templates/{kind}/{name}", status_code=204)
async def templates_delete(kind: str, name: str):
    m = M()
    if not m.templates.delete(kind, name):
        raise HTTPException(404, "no such template")
    m.broadcast({"type": "templates"})


@app.post("/api/push/subscribe")
async def push_subscribe(body: dict):
    """This device's push subscription ({subscription, label}): alerts then reach it as notifications."""
    m = M()
    try:
        did = m.push.subscribe(body.get("subscription"), body.get("label", ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    m.broadcast({"type": "connections", "notify": m.notifier.channels, "feeds": m.feed_names})
    return {"id": did}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(body: dict):
    m = M()
    endpoint = body.get("endpoint")
    if not isinstance(endpoint, str) or not m.push.remove(endpoint=endpoint):
        raise HTTPException(404, "this device is not subscribed")
    m.broadcast({"type": "connections", "notify": m.notifier.channels, "feeds": m.feed_names})


@app.delete("/api/push/devices/{did}", status_code=204)
async def push_device_delete(did: int):
    m = M()
    if not m.push.remove(id=did):
        raise HTTPException(404, "no such device")
    m.broadcast({"type": "connections", "notify": m.notifier.channels, "feeds": m.feed_names})


@app.post("/api/notify-test")
async def notify_test():
    m = M()
    if not m.notifier.channels:
        raise HTTPException(400, "no alert channels set: add Discord or ntfy under Connections")
    delivered = await m.notifier.send("Test", "Chart alerts are wired up.")
    return {"configured": m.notifier.channels, "delivered": delivered.split(",") if delivered else []}


@app.websocket("/ws")
async def ws(sock: WebSocket):
    if not _same_origin(sock.headers, sock.scope.get("scheme", "ws")) or not _own_host(sock.headers, M()):
        log.warning("refused a live connection from another site: %s (%s)", sock.headers.get("origin"), sock.headers.get("host"))
        await sock.close(code=1008)
        return
    await sock.accept()
    m = M()
    if m.auth.enabled and m.auth.session(sock.cookies.get(auth.COOKIE), touch=False) is None:
        await sock.close(code=4401)   # the app asks for the password
        return
    if setup.locked(app.state.fresh, m.auth.enabled, m.s) and not setup.is_local(sock.headers, None):
        await sock.close(code=4401)   # a fresh install: a password first
        return
    client = m.connect()

    async def writer():
        while not client.closed:
            msg = await client.outbox.get()
            await sock.send_json(msg)
        await sock.close()

    async def handle(msg: dict):
        op = msg.get("op")
        try:
            if op == "sub":
                await m.subscribe(client, int(msg["id"]), str(msg["symbol"]).upper(), str(msg["spec"]), bool(msg.get("rth")))
            elif op == "unsub":
                m.unsubscribe(client, int(msg["id"]))
            elif op == "amt":
                await m.amt.subscribe(client, int(msg["id"]), str(msg["symbol"]).upper(), int(msg["minutes"]), msg.get("set"))
            elif op == "tape":
                m.set_tape(client, str(msg["symbol"]).upper() if msg.get("symbol") else None, int(msg.get("min", 1)))
            elif op == "amt_off":
                m.amt.unsubscribe(client, int(msg["id"]))
        except (ValueError, KeyError, RuntimeError) as e:
            client.put({"type": "error", "op": op, "id": msg.get("id"), "message": str(e)})
        except Exception:
            log.exception("websocket %s failed", op)
            client.put({"type": "error", "op": op, "id": msg.get("id"), "message": "server error; see the server log"})

    w = asyncio.create_task(writer())
    try:
        while True:
            msg = await sock.receive_json()
            asyncio.create_task(handle(msg))   # a slow history load must not block later messages
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        m.disconnect(client)
        w.cancel()


_BUILT: dict[str, str] = {}
# only the app itself may show its page in a frame (layout.js's charts): another site's page can't lay it under its own
# buttons, where its clicks would be the app's own requests (clickjacking)
NO_FRAMES = {"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "frame-ancestors 'self'"}
VERSIONED = re.compile(r'\b(src|href)="((?!https?:|/|data:)[^"?#]+\.(?:js|css))"')


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def index_page():
    """index.html with the app's version in it: a page kept from an older one (a phone's app, a tab left open)
    sees hello's version differ and reloads."""
    if "index" not in _BUILT:   # the files don't change while it runs (an update is a new start)
        html = (settings.web_dir / "index.html").read_text(encoding="utf-8")   # not the PC's own code page (Windows)
        html = VERSIONED.sub(rf'\1="\2?v={WEB_VERSION}"', html)   # app.js?v=<version>: cached for good, a new name each update
        _BUILT["index"] = html.replace("<head>", f'<head>\n  <meta name="app-version" content="{WEB_VERSION}">', 1)
    return HTMLResponse(_BUILT["index"], headers={"Cache-Control": "no-cache", **NO_FRAMES})


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """sw.js with its cache named after the version: an update installs a new worker by itself."""
    if "sw" not in _BUILT:
        _BUILT["sw"] = re.sub(r"const CACHE = '[^']*';", f"const CACHE = 'charts-{WEB_VERSION}';", (settings.web_dir / "sw.js").read_text(encoding="utf-8"), count=1)
    return Response(_BUILT["sw"], media_type="application/javascript", headers={"Cache-Control": "no-cache"})


class NoCacheStatic(StaticFiles):
    """A file named with this version (app.js?v=<version>, as the page asks for them) is kept for good: an update
    renames it. Anything else is checked every load (cheap 304s via ETag)."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        r = super().file_response(full_path, stat_result, scope, status_code)
        mine = f"v={WEB_VERSION}".encode() in scope.get("query_string", b"").split(b"&")
        r.headers["Cache-Control"] = "public, max-age=31536000, immutable" if mine else "no-cache"
        r.headers.update(NO_FRAMES)   # index.html by another path too (/index.html/, //index.html): the file as it is
        return r


app.mount("/", NoCacheStatic(directory=settings.web_dir, html=True), name="web")
