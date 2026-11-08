"""Seventh bug-hunt fixes: the session cookie's Secure flag by the scheme of the page the browser is on (the ts.net
name over plain http, Apache with TLS), and a replay to the present that left out a complete last bar (an RTH chart
from the RTH close to the next RTH open, the daily break, a weekend) or hid the bar tapped."""
import json

import pytest

from chartsrv import auth, replay, sessions
from chartsrv.bars import make_series, parse_spec
from chartsrv.config import Instrument
from chartsrv.models import Tick
from chartsrv.store import Store
from test_app import client  # noqa: F401  the app on the sim feed
from test_hunt5_footprint import BARS_JS, check_kept
from test_hunt6_secfoot import LOCAL, PW, _secure
from test_hunt_web_chart import app_client, needs_node, run_app  # noqa: F401  (app_client: the app, as a fixture)
from test_redesign_tsnet_host import PC, SERVE
from test_replay import BIG, minutes, ms

TS = "pc.tail3.ts.net"   # the ts.net name over plain http, in CHART_ALLOWED_HOSTS as docs/PHONE.md has it
OLD = "old.tail3.ts.net"   # a tailscale serve before 1.44's name, in CHART_ALLOWED_HOSTS too
XFF = {"x-forwarded-for": "100.64.0.7"}
APACHE = {"host": "127.0.0.1:8800", "x-forwarded-server": "pc.lan", **XFF}   # its defaults: the app's own as the Host


@pytest.fixture
def hosts(monkeypatch):
    monkeypatch.setenv("CHART_ALLOWED_HOSTS", f"testserver, lan.pc, pc.example.com, {TS}, {OLD}")
    monkeypatch.setattr(auth, "RENEW_S", -1)   # every request renews: the renewal's cookie is checked too


# (the page the browser is on, what the proxy in front sends, path prefix): Secure or not
SHAPES = {
    "tailscale serve 1.44+ (https)": ((f"https://{PC}", SERVE, ""), True),
    "tailscale serve before 1.44 (https, no scheme named)": ((f"https://{OLD}", {"host": OLD, **XFF}, ""), True),
    "tailscale serve --http": ((f"http://{TS}", {"host": TS, "x-forwarded-host": TS, **XFF}, ""), False),
    "nginx per PHONE.md, the ts.net name over http": ((f"http://{TS}", {"host": TS, "x-forwarded-proto": "http", **XFF}, ""), False),
    "Apache's defaults, the ts.net name over http": ((f"http://{TS}", {**APACHE, "x-forwarded-host": TS}, ""), False),
    "Apache's defaults, plain http": (("http://lan.pc", {**APACHE, "x-forwarded-host": "lan.pc"}, ""), False),
    "Apache's defaults with TLS": (("https://pc.example.com", {**APACHE, "x-forwarded-host": "pc.example.com"}, ""), True),
    "Apache, ProxyPreserveHost On, with TLS": (("https://pc.example.com", {"host": "pc.example.com",
                                                "x-forwarded-host": "pc.example.com", **XFF}, ""), True),
    "nginx per PHONE.md on http": (("http://lan.pc", {"host": "lan.pc", "x-forwarded-proto": "http", **XFF}, ""), False),
    "nginx per PHONE.md with TLS": (("https://pc.example.com", {"host": "pc.example.com", "x-forwarded-proto": "https",
                                     **XFF}, ""), True),
    "a LAN address": (("http://192.168.1.5:8800", {"host": "192.168.1.5:8800"}, ""), False),
    "the PC's own browser": (("http://127.0.0.1:8800", {"host": "127.0.0.1:8800"}, ""), True),
    "the PC's own browser, localhost": (("http://localhost:8800", {"host": "localhost:8800"}, ""), True),
    "the PC's own browser, [::1]": (("http://[::1]:8800", {"host": "[::1]:8800"}, ""), True),
    "--root-path, Apache with TLS": (("https://pc.example.com", {**APACHE, "x-forwarded-host": "pc.example.com"},
                                      "/charts"), True),
    "--root-path, nginx on http": (("http://lan.pc", {"host": "lan.pc", "x-forwarded-proto": "http", **XFF}, "/charts"), False),
}


def test_the_cookie_is_secure_by_the_pages_scheme(hosts, client):  # noqa: F811
    """A Secure cookie is kept and sent back only by an https page (and the PC's own localhost, which browsers take as
    secure). The ts.net name over plain http (nginx per docs/PHONE.md, Apache's defaults, tailscale serve --http) got
    one whatever the proxy said, and every sign-in came back to the login form; Apache with TLS names no scheme, and
    its https page got a cookie without Secure. The page's own address (the Origin of a sign-in, the Referer of a page
    load's GET) now says, where no X-Forwarded-Proto does. The sign-in, the password change and the day's renewal."""
    assert client.put("/api/auth/password", json={"new": PW, "label": "PC"}, headers=LOCAL).status_code == 200
    got = {}
    for name, ((page, h, prefix), _) in SHAPES.items():
        client._transport.root_path = prefix   # what --root-path does to every request (the client's own setting)
        client.cookies.clear()
        r = client.post(f"{prefix}/api/auth/login", json={"password": PW, "label": name}, headers={**h, "origin": page})
        assert r.status_code == 200, name
        login, cookie = _secure(r), {"cookie": r.headers["set-cookie"].split(";")[0]}
        r = client.get(f"{prefix}/api/auth", headers={**h, **cookie, "referer": f"{page}{prefix}/"})   # a page load
        assert r.json()["signed_in"] is True, name
        renewal = _secure(r)
        r = client.put(f"{prefix}/api/auth/password", json={"current": PW, "new": PW, "label": name},
                       headers={**h, **cookie, "origin": page})
        assert r.status_code == 200, name
        got[name] = (login, renewal, _secure(r))
    assert got == {name: (want,) * 3 for name, (_, want) in SHAPES.items()}
    client._transport.root_path = ""


# ---- k-sf7-3, k-sf7-4: a replay to the present leaves out the last bar only while it can still be forming --------------

ES = Instrument("ES", "E-mini", 0.25, 50, "ES", rth=("08:30", "15:00"))   # as config/instruments.json has it


def rth_day_trades():
    """Tuesday 20 October 2026's RTH, a trade of 100 every 30 s (156 bars of 500), and two in the evening session."""
    out = [Tick("ES", ms(2026, 10, 20, 8, 30) + 30_000 * k, 6000 + 0.25 * (k % 4), 100, 1 if k % 2 else -1)
           for k in range(780)]
    return out + [Tick("ES", ms(2026, 10, 20, 17, 5) + h * 3_600_000, 5990.0, 100, 1) for h in range(2)]


def built(spec, src, on, rth_only):
    s = make_series(parse_spec(spec), 0.25, 2, 10**6, ES.rth, rth_only=rth_only)
    for x in src:
        getattr(s, on)(x)
    return [(b.t, b.v) for b in s.bars]


def test_a_trade_built_replay_keeps_a_last_bar_that_no_trade_can_join(tmp_path):
    """Tick, volume and range bars restart at each session open, and an RTH chart takes no trades outside RTH: from the
    RTH close to the next RTH open no bar of it can be forming, nor in the daily break or a weekend. The last one was
    left out all the same: in the evening, played to the end, the next stretch left out the bar already shown, the
    browser's join failed and the chart went back two bars. It is kept now, and ends with its hours (end_at)."""
    ticks = rth_day_trades()
    db = Store(tmp_path / "chart.db")
    db.write(ticks, [])
    rth = built("500v", ticks, "on_tick", True)
    assert len(rth) == 156 and rth[-1] == (ms(2026, 10, 20, 14, 57) + 30_000, 500)   # complete
    spec, at, close, evening = parse_spec("500v"), ms(2026, 10, 20, 14), ms(2026, 10, 20, 15), ms(2026, 10, 20, 19)

    def got(at, now, rth_only=True):
        bars, next_at, end_at = replay.build(db.path, "ES", ES, spec, at, now, BIG, rth_only)
        return [(b.t, b.v) for b in bars], next_at, end_at
    assert got(at, evening) == (rth, ms(2026, 10, 20, 17), close)   # the first stretch: on at the evening's open
    assert got(ms(2026, 10, 20, 17), evening) == (rth, None, close)    # the next one: the same bars, the last one too
    assert got(at, ms(2026, 10, 20, 15, 30)) == (rth, None, close)    # after the RTH close, the session still trading
    assert got(ms(2026, 10, 20, 17), ms(2026, 10, 21, 7)) == (rth, None, close)   # the next session, before its RTH
    # still forming: inside RTH, and on an all-hours chart the evening's bar
    assert got(at, ms(2026, 10, 20, 14, 58) + 40_000) == (rth[:-1], None, rth[-1][0])
    assert got(ms(2026, 10, 20, 17), evening, False) == (built("500v", ticks, "on_tick", False)[:-1], None,
                                                         ms(2026, 10, 20, 17, 5))
    # an all-hours chart in the daily break: the session's last bar, to its close
    assert got(at, ms(2026, 10, 20, 16, 30), False) == (rth, None, ms(2026, 10, 20, 16))


def test_a_holidays_early_close_ends_the_last_bar(tmp_path):
    """Christmas Eve shuts at 12:15 CT, and Christmas (a Friday) does not trade: at 13:00 the day's last bar is done."""
    sessions.set_calendar({"days": [{"date": "2026-12-24", "close": {"default": "12:15"}},
                                    {"date": "2026-12-25", "closed": True}]})
    ticks = [Tick("ES", ms(2026, 12, 24, 8, 30) + 30_000 * k, 6000.0, 100, 1) for k in range(450)]   # to 12:15
    db = Store(tmp_path / "chart.db")
    db.write(ticks, [])
    spec, at = parse_spec("500v"), ms(2026, 12, 24, 10)
    for rth_only in (True, False):
        bars, next_at, end_at = replay.build(db.path, "ES", ES, spec, at, ms(2026, 12, 24, 13), BIG, rth_only)
        assert len(bars) == 90 and next_at is None and end_at == ms(2026, 12, 24, 12, 15), rth_only
        bars, next_at, end_at = replay.build(db.path, "ES", ES, spec, at, ms(2026, 12, 24, 11), BIG, rth_only)
        assert len(bars) == 59 and end_at == bars[-1].t + 150_000, rth_only   # trading: the forming one left out


TUE = lambda h, m=0: ms(2026, 10, 20, h, m)  # noqa: E731
TIME_BARS = {   # (spec, RTH only, replay from, now): the last bar sent
    "RTH 30m, the evening": ("30m", True, TUE(13), TUE(19), TUE(14, 30)),
    "RTH 30m, after the RTH close": ("30m", True, TUE(13), TUE(15, 30), TUE(14, 30)),
    "RTH 30m, before the next RTH open": ("30m", True, TUE(13), ms(2026, 10, 21, 7), TUE(14, 30)),
    "RTH 5m, the evening": ("5m", True, TUE(13), TUE(19), TUE(14, 55)),
    "30m, the daily break": ("30m", False, TUE(13), TUE(16, 30), TUE(15, 30)),
    "4h, the daily break": ("4h", False, TUE(9), TUE(16, 30), TUE(13)),
    "30m, Saturday": ("30m", False, ms(2026, 10, 16, 13), ms(2026, 10, 17, 12), ms(2026, 10, 16, 15, 30)),
    "1D, Saturday": ("1D", False, ms(2026, 10, 14, 12), ms(2026, 10, 17, 12), ms(2026, 10, 15, 17)),   # Friday's
    # still forming: left out
    "30m, Tuesday 10:10": ("30m", False, TUE(8), TUE(10, 10), TUE(9, 30)),
    "RTH 30m, Tuesday 10:10": ("30m", True, TUE(8), TUE(10, 10), TUE(9, 30)),
    "1D, Tuesday's evening": ("1D", False, ms(2026, 10, 14, 12), TUE(19), ms(2026, 10, 19, 17)),   # Tuesday's
    "RTH 1W, Tuesday's evening": ("1W", True, ms(2026, 10, 14, 12), TUE(19), ms(2026, 10, 11, 17)),   # last week's
    "1W, the daily break": ("1W", False, ms(2026, 10, 14, 12), TUE(16, 30), ms(2026, 10, 11, 17)),
}


def test_a_time_bar_replay_keeps_a_last_bar_that_has_ended(tmp_path):
    """Time bars: the last one was always left out when the bars reached the present, so the RTH day's closing bar
    could not be replayed from the RTH close to the next RTH open, nor the session's last one in the daily break or on
    a weekend. It is left out now only while the next trade the chart can take goes into it."""
    db = Store(tmp_path / "chart.db")
    db.insert_bars_if_missing("ES", minutes(ms(2026, 10, 4, 17), ms(2026, 10, 21, 7)))
    got = {}
    for name, (spec, rth_only, at, now, _) in TIME_BARS.items():
        bars, next_at, end_at = replay.build(db.path, "ES", ES, parse_spec(spec), at, now, BIG, rth_only)
        assert next_at is None and end_at is None, name
        got[name] = bars[-1].t
    assert got == {name: want for name, (*_, want) in TIME_BARS.items()}


def answers(db, spec, clocks, now, rth_only):
    """/api/replay's answers at these clocks, as the app gives them with its clock at `now`."""
    out = {}
    for at in clocks:
        bars, next_at, end_at = replay.build(db.path, "ES", ES, parse_spec(spec), at, now, BIG, rth_only)
        out[f"/api/replay?symbol=ES&spec={spec}&at={at}&rth={'true' if rth_only else 'false'}"] = {
            "symbol": "ES", "spec": spec, "kind": parse_spec(spec).kind, "at": at, "next_at": next_at, "end_at": end_at,
            "bars": [b.to_dict() for b in bars]}
    return out


EVENING_JS = BARS_JS + """
const REPLAY = __REPLAY__, log = [];
const api = async (method, path, body) => (method === 'GET' ? JSON.parse(JSON.stringify(REPLAY[path])) : answer(body));
const fpSchedule = () => {}, replayUi = () => {}, replayStudies = () => {}, fail = (e) => { throw e; }, toast = (m) => log.push({ toast: m });
function applyHistory(m) { S.kind = m.kind; S.bars = m.bars.slice(); S.fp.clear(); log.push({ history: m.bars.length }); }
/*APP*/
(async () => {
  S.rth = true;
  S.replay = { at: __AT__, data: [], idx: 0, kind: 'time', period: 0, nextAt: null, endAt: null, playing: false, timer: 0,
    busy: false, hidden: {}, req: 0, empty: 0 };
  await replayLoad(); await fpFetch();
  const shown = [S.bars.length];
  for (let k = 0; k < 60 && !log.at(-1).toast; k++) {   // played to the end
    replayStep(); await new Promise((r) => setTimeout(r, 0)); await fpFetch(); shown.push(S.bars.length);
  }
  console.log(JSON.stringify({ log, shown, bars: S.bars, bodies, kept: [...S.fp].map(([seq, f]) => [seq, f.q, f.k]) }));
})();
"""


@needs_node
def test_an_rth_replay_played_to_the_end_in_the_evening_keeps_its_bars(app_client):  # noqa: F811
    """An RTH 500v chart at 19:00 CT, replayed from 14:00 and played to the end: the next stretch (asked at the
    evening's open) left out the RTH day's last bar as if forming, the join on the bar shown failed, and the chart went
    back to 154 bars and stopped one short. Now it plays on to the last bar, says it has caught up, and every bar's
    footprint has its own trades: the last one's up to the RTH close (end_at)."""
    ticks = rth_day_trades()
    with app_client(ticks) as c:
        store = c.app.state.market.store
        at = ms(2026, 10, 20, 14)
        got = answers(store, "500v", [at, ms(2026, 10, 20, 17)], ms(2026, 10, 20, 19), True)
        out = run_app(["specSeconds", "daysSpec", "barEnd", "shownCount", "replayLoad", "replayStep", "replayMore",
                       "replayPause", "fpBusy", "fpFetch", "applyBars"],
                      "const SPEC = '500v';" + EVENING_JS.replace("__REPLAY__", json.dumps(got)).replace("__AT__", str(at)))
        assert out["log"] == [{"history": 132}, {"toast": "Replay has caught up with the stored bars. ✕ goes back to live."}]
        assert out["shown"] == [*range(132, 157), 156]
        assert [(b["t"], b["v"]) for b in out["bars"]] == built("500v", ticks, "on_tick", True)
        last = out["bodies"][-1]
        assert last["starts"][-2:] == [out["bars"][-1]["t"], ms(2026, 10, 20, 15)] and last["vols"][-1] == 0
        assert last["end"] == ms(2026, 10, 20, 15) + 1 and last["rth"] is True
        assert len(out["kept"]) == 156
        check_kept(c, out["bodies"], out["kept"], out["bars"])


# ---- k-sf7-5: a tap on the last complete bar shows it ------------------------------------------------------------------

TAP_JS = """
const S = { symbol: 'ES', spec: '500v', rth: false, bars: [], replay: null };
const REPLAY = __REPLAY__, CLOCKS = __CLOCKS__, out = {};
const api = async (m, path) => JSON.parse(JSON.stringify(REPLAY[path]));
const replayUi = () => {}, replayStudies = () => {}, replayPause = () => {}, fail = (e) => { throw e; }, toast = () => {};
function applyHistory(m) { S.bars = m.bars.slice(); }
/*APP*/
(async () => {
  for (const [name, at] of Object.entries(CLOCKS)) {
    S.replay = { at, data: [], idx: 0, kind: 'time', period: 0, nextAt: null, endAt: null, req: 0, empty: 0 };
    await replayLoad();
    out[name] = S.bars.at(-1)?.t;
  }
  // the next stretch's bars are not the ones shown (data written since): it starts over at the replay clock, the
  // open of the one still forming
  const mk = (t, c) => ({ t, o: 1, h: 1, l: 1, c, v: 500, d: 0, seq: t / 1000 });
  const r = S.replay = { at: 3000, data: [mk(1000, 1), mk(2000, 1)], idx: 2, kind: 'volume', period: 0, days: false,
    nextAt: 3000, endAt: null, req: 0, empty: 0 };
  REPLAY['/api/replay?symbol=ES&spec=500v&at=3000&rth=false'] = { kind: 'volume', spec: '500v', next_at: null, end_at: 3000,
    bars: [mk(1000, 1), mk(2000, 2)] };   // the 2000 bar took a trade since
  await replayMore();
  out['start over'] = S.bars.at(-1)?.t;
  console.log(JSON.stringify(out));
})();
"""


@needs_node
def test_a_tap_on_the_last_complete_bar_shows_it(tmp_path):
    """Today's session, 500v: bars at 09:00, 09:05, 09:10 and 09:15 done, 09:20 forming. A tap on the 09:15 bar starts
    the replay at the forming one's open (the click handler's clock, where 09:15 ends), and the server leaves out the
    forming one and says where 09:15 ends, end_at; the count of bars shown took the last one sent as ending nowhere, so
    09:15 was hidden too, as a tap on 09:10 would. A time picked inside the forming bar the same; and a stretch the
    replay starts over on, its last bar."""
    t0 = ms(2026, 10, 20, 9)
    ticks = [Tick("ES", t0 + 60_000 * k, 6000 + 0.25 * (k % 4), 100, 1 if k % 2 else -1) for k in range(23)]
    db = Store(tmp_path / "chart.db")
    db.write(ticks, [])
    live = [t for t, _ in built("500v", ticks, "on_tick", False)]
    assert [(t - t0) // 60_000 for t in live] == [0, 5, 10, 15, 20]
    clocks = {"tap 09:15": live[-1], "tap 09:10 (09:15's open)": live[-2], "09:21 picked": live[-1] + 60_000}
    got = answers(db, "500v", clocks.values(), t0 + 40 * 60_000, False)
    out = run_app(["specSeconds", "daysSpec", "barEnd", "shownCount", "replayLoad", "replayMore"],
                  TAP_JS.replace("__REPLAY__", json.dumps(got)).replace("__CLOCKS__", json.dumps(clocks)))
    assert out == {"tap 09:15": live[3], "tap 09:10 (09:15's open)": live[2], "09:21 picked": live[3], "start over": 2000}
