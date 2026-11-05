"""Sixth bug-hunt fixes in the address checks and the footprint in replay: the session cookie's Secure flag behind a
proxy that passes the browser's address in X-Forwarded-Host (Apache's defaults), a ts.net name "proved" through a
proxy of yours that names no scheme, the last bar a replay that reaches the present shows, and the footprint's refresh
on the old bars after replay."""
import json
from datetime import datetime, timezone

import pytest

from chartsrv import replay
from chartsrv.bars import make_series, parse_spec
from chartsrv.models import Tick
from chartsrv.store import Store
from test_app import client  # noqa: F401  the app on the sim feed
from test_hunt5_footprint import bars_of, check_kept, replay_run
from test_hunt_web_chart import app_client, needs_node, run_app  # noqa: F401  (app_client: the app, as a fixture)
from test_redesign_tsnet_host import PC, SERVE, _names, _refused
from test_replay import BIG, ES

PW = "correct horse"
EVIL_TS = "evil.tail9.ts.net"   # another site's name under ts.net, pointed at this PC
LOCAL = {"host": "127.0.0.1:8800", "origin": "http://127.0.0.1:8800"}   # the PC's own browser


@pytest.fixture
def lan_name(monkeypatch):
    monkeypatch.setenv("CHART_ALLOWED_HOSTS", "testserver, lan.pc, old.tail3.ts.net")   # your proxy's name for the PC,
    # and the ts.net name of a tailscale serve before 1.44


def _secure(r) -> bool:
    return "secure" in [p.strip().lower() for p in r.headers["set-cookie"].split(";")]


def test_the_cookie_is_secure_only_where_it_comes_back(lan_name, client):  # noqa: F811
    """A Secure cookie is kept and sent back only on an https page (and the PC's own localhost). Behind Apache with its
    defaults the Host is the app's own (127.0.0.1:8800) and the browser's address, plain http://lan.pc, comes in
    X-Forwarded-Host: the cookie was marked Secure by the Host, the browser dropped it, and every sign-in from another
    device came back to the login form. tailscale serve and the PC's own browser still get a Secure one."""
    assert client.put("/api/auth/password", json={"new": PW, "label": "PC"}, headers=LOCAL).status_code == 200
    shapes = {   # the page's address: what each proxy sends
        "apache, its defaults": ("http://lan.pc", {"host": "127.0.0.1:8800", "x-forwarded-host": "lan.pc",
                                                   "x-forwarded-for": "192.168.1.20", "x-forwarded-server": "pc.lan"}),
        "apache, ProxyPreserveHost On": ("http://lan.pc", {"host": "lan.pc", "x-forwarded-host": "lan.pc",
                                                           "x-forwarded-for": "192.168.1.20"}),
        "nginx as docs/PHONE.md has it": ("http://lan.pc", {"host": "lan.pc", "x-forwarded-for": "192.168.1.20",
                                                            "x-forwarded-proto": "http"}),
        "nginx, X-Forwarded-Host $host": ("http://lan.pc", {"host": "127.0.0.1:8800", "x-forwarded-host": "lan.pc",
                                                            "x-forwarded-for": "192.168.1.20"}),
        "the PC's LAN address": ("http://192.168.1.5:8800", {"host": "192.168.1.5:8800"}),
        "tailscale serve": (f"https://{PC}", SERVE),
        "the PC itself": ("http://127.0.0.1:8800", {"host": "127.0.0.1:8800"}),
        "the PC itself, localhost": ("http://localhost:8800", {"host": "localhost:8800"}),
    }
    want = {"tailscale serve": True, "the PC itself": True, "the PC itself, localhost": True}
    for name, (page, h) in shapes.items():
        r = client.post("/api/auth/login", json={"password": PW, "label": name}, headers={**h, "origin": page})
        assert r.status_code == 200, name
        assert _secure(r) is want.get(name, False), name
        cookie = {"cookie": r.headers["set-cookie"].split(";")[0]}
        r = client.put("/api/auth/password", json={"current": PW, "new": PW, "label": name},
                       headers={**h, **cookie, "origin": page})   # a password change sets it again: the same flag
        assert r.status_code == 200 and _secure(r) is want.get(name, False), name


def test_a_proxy_that_names_no_scheme_proves_no_ts_net_name(lan_name, client):  # noqa: F811
    """A link or a form on https://<another site>.ts.net to the same name over plain http, through a proxy of yours
    that adds X-Forwarded-For and names no scheme (Apache with ProxyPreserveHost On, HAProxy's option forwardfor, nginx
    with no X-Forwarded-Proto line), looked like tailscale serve before 1.44: the name was kept as this PC's for good,
    and the rebound page got in by it. The proof now needs tailscale serve's X-Forwarded-Proto https (1.44 and later);
    an older one's name goes in CHART_ALLOWED_HOSTS. Whatever address the name points at: the PC, a LAN address, the
    PC's tailnet address, from the PC or from a phone."""
    for xff in ("127.0.0.1", "192.168.1.20", "100.101.102.103", "100.64.0.7", "fd7a:115c:a1e0::77"):
        for h in ({"host": EVIL_TS, "x-forwarded-host": EVIL_TS, "x-forwarded-for": xff, "x-forwarded-server": "pc.lan"},
                  {"host": EVIL_TS, "x-forwarded-for": xff},                       # HAProxy
                  {"host": EVIL_TS, "x-forwarded-for": xff, "x-real-ip": xff}):    # nginx, no X-Forwarded-Proto line
            assert client.get("/api/health", headers={**h, "referer": f"https://{EVIL_TS}/"}).status_code == 403, h
            assert client.post("/api/auth/login", json={"password": "x"},
                               headers={**h, "origin": f"https://{EVIL_TS}"}).status_code == 403, h
            assert _names(client) is None, h
    _refused(client, {"host": EVIL_TS, "x-forwarded-for": "127.0.0.1", "origin": f"http://{EVIL_TS}"})   # the rebound page
    assert client.get("/api/auth").json()["enabled"] is False   # nothing changed
    # tailscale serve 1.44 and later still proves this PC's name
    assert client.get("/api/health", headers={**SERVE, "referer": f"https://{PC}/"}).status_code == 200
    assert _names(client) == [PC]
    # an older one (the Host and X-Forwarded-For only) proves none, and gets in by CHART_ALLOWED_HOSTS
    old = {"host": "pc.tail3.ts.net", "x-forwarded-for": "100.64.0.7"}
    assert client.get("/api/health", headers={**old, "referer": "https://pc.tail3.ts.net/"}).status_code == 403
    old = {**old, "host": "old.tail3.ts.net"}
    assert client.get("/api/health", headers={**old, "referer": "https://old.tail3.ts.net/"}).status_code == 200
    assert client.put("/api/templates/chart/x", json={"data": {"studies": {}}},
                      headers={**old, "origin": "https://old.tail3.ts.net"}).status_code == 200
    assert _names(client) == [PC]


# ---- sf-3: a replay that reaches the present: the last bar shown ends where the forming one opens ---------------------

@needs_node
@pytest.mark.parametrize("spec", ["500v", "4t"])
def test_a_replay_to_the_present_ends_the_last_bar_at_the_forming_ones_open(app_client, in_session, spec):  # noqa: F811
    """Today's session: the server leaves out the bar still forming and answers next_at null, so the last bar sent had
    nothing after it. Stepped on to it, its footprint was asked up to its own open + 1 ms (the replay clock): its first
    trade only, while the candle showed the whole bar. /api/replay now says where it ends, end_at."""
    t0 = in_session - 40 * 60_000   # 40 minutes ago
    ticks = [Tick("ES", t0 + 1000 * k, 6000 + 0.25 * (k % 4), 100, 1 if k % 2 else -1) for k in range(23)]
    live = bars_of(spec, ticks)
    steps = len(live) - 2   # a tap on the first bar, then on to the last one sent
    with app_client(ticks) as c:
        out, data = replay_run(c, [(spec, live[1]["t"])], f"""
(async () => {{
  await start({live[1]["t"]}); await stage('tap 0');
  for (let k = 1; k <= {steps}; k++) {{ replayStep(); await stage('step ' + k); }}
  out.push({{ clock: S.replay.at }});""")
        assert [(b["t"], b["v"]) for b in data[spec]] == [(b["t"], b["v"]) for b in live[:-1]]   # all but the forming one
        assert c.get(f"/api/replay?symbol=ES&spec={spec}&at={live[1]['t']}").json()["end_at"] == live[-1]["t"]
        assert [o["shown"] for o in out["out"][:-1]] == list(range(1, len(live)))
        for o in out["out"][:-1]:
            assert len(o["kept"]) == o["shown"], o   # every bar shown has its footprint, with its own trades
            check_kept(c, out["bodies"], o["kept"], data[spec])
    assert out["out"][-1]["clock"] == live[-1]["t"]   # the replay clock: where the last bar ends


def test_on_a_weekend_the_replay_says_where_fridays_last_bar_sent_ends(tmp_path):
    """In the daily break or on a weekend the session's next one is still ahead, and no trade can join its last bar
    (round 7: it was left out as if still forming): it is sent, and end_at, the session's close, is where it ends."""
    ms = lambda *a: int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)  # noqa: E731
    t0 = ms(2026, 10, 23, 13, 30)   # Friday 08:30 CT
    ticks = [Tick("ES", t0 + 10_000 * k, 6000 + 0.25 * (k % 4), 100, 1 if k % 2 else -1) for k in range(60)]
    spec = parse_spec("500v")
    s = make_series(spec, ES.tick_size, ES.decimals, 1000, ES.rth)
    for tk in ticks:
        s.on_tick(tk)
    full = list(s.bars)   # 12 bars of 500, all complete
    db = Store(tmp_path / "chart.db")
    db.write(ticks, [])
    for now in (ms(2026, 10, 24, 12), ms(2026, 10, 23, 21, 30)):   # Saturday, Friday 16:30 CT (the break)
        bars, next_at, end_at = replay.build(db.path, "ES", ES, spec, t0 + 60_000, now, BIG)
        assert [b.t for b in bars] == [b.t for b in full] and next_at is None
        assert end_at == ms(2026, 10, 23, 21)   # Friday 16:00 CT
    bars, next_at, end_at = replay.build(db.path, "ES", ES, spec, t0 + 60_000, ms(2026, 10, 26, 12), BIG)
    assert len(bars) == len(full) and next_at is not None   # Monday: on at the next session, and Friday's last bar
    assert end_at == ms(2026, 10, 23, 21)   # still ends at its close (round 8: at the next session's open)


# ---- sf-4: back from replay, the footprint waits for the live history ------------------------------------------------

EXIT_JS = """
const DAY = 86_400_000, now = Date.now();
const mk = (t0, n, seq0) => Array.from({ length: n }, (_, k) => ({ t: t0 + 60_000 * k, seq: seq0 + k, v: 500, o: 1, h: 1, l: 1, c: 1 }));
const old = mk(now - 7 * DAY + 3_600_000, 5, 1), live = mk(now - 300_000, 5, 901);   // a replay 7 days back; today's bars
const S = { st: { fp: 'bidask' }, ext: null, symbol: 'ES', spec: '500v', rth: false, kind: 'volume', fp: new Map(), bars: old.slice(),
  replay: { at: old[4].t + 1, data: old, idx: 5, kind: 'volume', period: 0, days: false, nextAt: null, endAt: null, playing: false,
    timer: 0, hidden: {}, req: 1 },
  subId: 1, cs: {}, vp: {}, seqTime: new Map(), dsmaLive: new Map(), holidays: {} };
const el = { classList: { add() {}, toggle() {} }, setAttribute() {} }, $ = () => el;
const chart = { timeScale: () => ({ getVisibleLogicalRange: () => ({ from: 0, to: S.bars.length + 3 }),
  options: () => ({ barSpacing: 50, rightOffset: 0 }), setVisibleLogicalRange() {} }) };
const FP_MIN_SPACING = 40, fpPrim = { update() {} }, isTime = () => S.kind === 'time', TK_MULTI = false, extTimer = 0, extStop = null;
const VP_KINDS = [], WL_FUT = /^$/, ax = { pick: '' }, drawings = { state: { loadedFor: 'ES' }, load() {} };
const vol = { applyOptions() {}, setData() {} }, candles = { setData() {} }, toPoints = () => [{}, {}];
const viewKey = () => 'k', viewApply = () => true, bigOn = () => false, calSpec = () => false, exch = () => ({}), inst = () => ({});
const noop = () => {};
const [replayUi, studiesChanged, isExtSym, renderTf, renderStudiesMenu, barsLayout, specLabel, watermarkLayout, tapeSymbol,
  amtRequest, tpoRequest, vpAll, gapRequest, amtLayout, ifRequest, renderPlanIf, setSessionClock, setSessionHolidays, cmpClear,
  altSetData, setStudyData, indsUpdate, hemaDraw, hemaRequest, dsmaDraw, dsmaRequest, renderIfInfo, scheduleRange,
  priceLineLayout, fillsSchedule, cmpLoad, legend, header, loadNote, barsClear] = Array(35).fill(noop);
const sent = [], send = (m) => sent.push(m), asked = [];
const api = async (method, path, body) => { asked.push({ starts: body.starts, end: body.end, at: stage }); return { bars: body.starts.map((t) => ({ t, rows: [] })) }; };
let stage = '';
/*APP*/
(async () => {
  stage = 'back from replay'; replayExit();
  await fpFetch(true);   // the once-a-second refresh, before the live history comes
  stage = 'history'; applyHistory({ kind: 'volume', bars: live });
  await new Promise((r) => setTimeout(r, 400));   // its own fetch
  stage = 'refresh'; await fpFetch(true);
  stage = 'another symbol'; S.symbol = 'NQ'; subscribe();
  await fpFetch(true);   // ES's bars, still shown (barsClear left out here)
  console.log(JSON.stringify({ subs: sent.filter((m) => m.op === 'sub').map((m) => m.symbol), asked, live: live.map((b) => b.t), now }));
})();
"""


@needs_node
def test_back_from_replay_the_footprint_waits_for_the_live_history():
    """Leaving replay asks for the live history; until it comes the chart still holds the replay's bars, and the
    once-a-second refresh took the last of them for the forming bar: every trade from its open (a week back) to now
    + 1 h, seconds of server work thrown away, the live chart's own footprint waiting behind it. Another symbol or bar
    type meanwhile: the old chart's bars. fpFetch now waits for the history."""
    out = run_app(["specSeconds", "daysSpec", "barEnd", "replayPause", "replayExit", "fpBusy", "fpSchedule", "fpFetch",
                   "subscribe", "applyHistory"], EXIT_JS)
    assert out["subs"] == ["ES", "NQ"]
    assert [a["at"] for a in out["asked"]] == ["history", "refresh"], out["asked"]   # nothing before the history comes
    for a in out["asked"]:
        assert set(a["starts"]) <= set(out["live"]) and a["end"] - out["now"] < 2 * 3_600_000, a
