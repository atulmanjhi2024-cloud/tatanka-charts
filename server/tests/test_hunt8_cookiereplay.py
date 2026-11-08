"""Eighth bug-hunt fixes: behind a proxy that names no scheme (Apache with TLS, tailscale serve before 1.44), another
site's link or a URL typed in brought the day's renewal of the session cookie without Secure, on an https page; and
a replay of tick, volume and range bars at a clock past a session's last bar hid that bar, complete."""
import json
import time
from types import SimpleNamespace

import pytest

import chartsrv.app as A
from chartsrv import auth, sessions
from chartsrv.models import Tick
from chartsrv.studies import RthClock
from test_app import client  # noqa: F401  the app on the sim feed
from test_hunt6_secfoot import LOCAL, PW
from test_hunt7_cookiereplay import APACHE, OLD, XFF
from test_hunt_web_chart import app_client, needs_node, run_app  # noqa: F401  (app_client: the app, as a fixture)
from test_replay import ms

# ---- k-cr8-1: only the app's own page renews the session cookie ----------------------------------------------------


@pytest.fixture
def names(monkeypatch):
    monkeypatch.setenv("CHART_ALLOWED_HOSTS", f"testserver, pc.example.com, {OLD}")


def cookies(r) -> list[bool]:
    """The session cookies an answer sends: Secure or not."""
    return ["secure" in [p.strip().lower() for p in c.split(";")] for c in r.headers.get_list("set-cookie")
            if c.startswith(f"{auth.COOKIE}=")]


PROXIES = {   # https pages, through a proxy that names no scheme: (the page, what the proxy sends)
    "Apache's defaults with TLS": ("https://pc.example.com", {**APACHE, "x-forwarded-host": "pc.example.com"}),
    "Apache's defaults with TLS, port 8443": ("https://pc.example.com:8443",
                                               {**APACHE, "x-forwarded-host": "pc.example.com:8443"}),
    "tailscale serve before 1.44": (f"https://{OLD}", {"host": OLD, **XFF}),
}
LINK = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}   # a top-level GET:
# the browser sends the cookie along (SameSite=lax), and no Origin
NOT_THE_PAGE = {   # requests that don't come from the app's own page: (path, headers), the page's name for {name}
    "another site's link": ("/api/auth", {**LINK, "referer": "http://evil.example/"}),
    "another site's link to an /api URL": ("/api/alerts", {**LINK, "referer": "http://evil.example/"}),
    "a link on a page that sends no Referer": ("/api/auth", LINK),
    "a URL typed in (docs/PHONE.md: /api/health)": ("/api/health", {**LINK, "sec-fetch-site": "none"}),
    "a page on another port of the name": ("/api/auth", {**LINK, "referer": "http://{name}:8080/"}),
    # an attacker on the network answers for the name over plain http: its page links to the app, or fetches from it
    "the name's plain http page, a link": ("/api/auth", {**LINK, "referer": "http://{name}/"}),
    "the name's plain http page, a fetch": ("/api/alerts", {"origin": "http://{name}", "referer": "http://{name}/",
                                                            "sec-fetch-site": "same-site", "sec-fetch-mode": "cors"}),
}


def test_only_the_apps_own_page_renews_the_session(names, client, monkeypatch):  # noqa: F811
    """The day's renewal sends the session cookie again, Secure by the scheme of the page the browser is on. Behind
    Apache with TLS or a tailscale serve before 1.44 nothing names it but the page's own address, and the first request
    after the day was up took it from whatever page linked to the app (another site's http page) or from no page (a
    link on a page that sends no Referer, a URL typed in): the cookie came back without Secure, and from then on went
    out on any plain-http request to that name, where an attacker on the network reads it. Only a request from the
    app's own page renews now, the app's next fetch after one of those (the browser sends its page as the Referer)."""
    assert client.put("/api/auth/password", json={"new": PW, "label": "PC"}, headers=LOCAL).status_code == 200
    real, got = time.time, {}

    def days_later(n):   # the sessions' clock
        monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: real() + n * 86400))
    for proxy, (page, h) in PROXIES.items():
        name = page.split("//")[1].split(":")[0]
        for how, (path, extra) in NOT_THE_PAGE.items():
            days_later(0)
            client.cookies.clear()
            r = client.post("/api/auth/login", json={"password": PW, "label": "phone"}, headers={**h, "origin": page})
            assert r.status_code == 200 and cookies(r) == [True], (proxy, how)
            cookie = {"cookie": r.headers["set-cookie"].split(";")[0]}
            days_later(2)   # the day is up: the next request renews
            r = client.get(path, headers={**h, **cookie, **{k: v.format(name=name) for k, v in extra.items()}})
            assert r.status_code == 200, (proxy, how)
            other = cookies(r)
            own = {"referer": f"{page}/", "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"}   # the app's fetch
            r = client.get("/api/auth", headers={**h, **cookie, **own})
            assert r.json()["signed_in"] is True, (proxy, how)
            got[proxy, how] = (other, cookies(r), cookies(client.get("/api/alerts", headers={**h, **cookie, **own})))
    assert got == {k: ([], [True], []) for k in got}   # renewed by the app's own fetch, Secure, and once a day


# ---- k-cr8-2: a replay at a clock past a session shows its last bar -------------------------------------------------

def week_trades():
    """Monday 19 to Friday 23 October 2026: a trade of 100 every 30 s in RTH (08:30-15:00, 156 bars of 500 a day),
    every 5 min outside it while the market trades."""
    out, t, rth = [], ms(2026, 10, 18, 17), RthClock(("08:30", "15:00"))
    while t < ms(2026, 10, 23, 16):
        if sessions.is_open(t):
            out.append(Tick("ES", t, 6000 + 0.25 * (len(out) % 4), 100, 1 if len(out) % 2 else -1))
        t += 30_000 if rth.day(t) is not None else 300_000
    return out


DAY = lambda d, h, m=0, s=0: ms(2026, 10, d, h, m) + s * 1000  # noqa: E731
PAST = {   # (clock, RTH only, steps, "now"): the last bar shown, and the clock, after the load and after each step
    "RTH, a 5m chart stepped to its 14:55 bar": (DAY(20, 15), True, 0, None, [(DAY(20, 14, 57, 30), DAY(20, 15))]),
    "RTH, Go at 16:30": (DAY(20, 16, 30), True, 0, None, [(DAY(20, 14, 57, 30), DAY(20, 16, 30))]),
    "RTH, Go at 18:00 (Wednesday's bars sent too)": (DAY(20, 18), True, 0, None, [(DAY(20, 14, 57, 30), DAY(20, 18))]),
    "a 5m chart stepped to Friday's 15:55 bar": (DAY(23, 16), False, 0, None, [(DAY(23, 15, 45), DAY(23, 16))]),
    "Go at Saturday 12:00": (DAY(24, 12), False, 0, None, [(DAY(23, 15, 45), DAY(24, 12))]),
    "Go at 16:30, the daily break": (DAY(20, 16, 30), False, 0, None, [(DAY(20, 15, 45), DAY(20, 16, 30))]),
    "Go at 17:30, the evening": (DAY(20, 17, 30), False, 0, None, [(DAY(20, 17), DAY(20, 17, 30))]),
    "the present, 19:00: RTH, Go at 15:00": (DAY(20, 15), True, 0, DAY(20, 19), [(DAY(20, 14, 57, 30), DAY(20, 15))]),
    "RTH, Go at 14:56, stepped on": (DAY(20, 14, 56), True, 4, None, [
        (DAY(20, 14, 52, 30), DAY(20, 14, 56)), (DAY(20, 14, 55), DAY(20, 14, 57, 30)),
        (DAY(20, 14, 57, 30), DAY(20, 15)), (DAY(20, 14, 57, 30), DAY(20, 15)),   # the next stretch loaded
        (DAY(21, 8, 30), DAY(21, 8, 32, 30))]),
}
PAST_JS = """
const S = { symbol: 'ES', spec: '500v', rth: false, bars: [], replay: null };
const CASES = JSON.parse(require('fs').readFileSync(__CASES__, 'utf8')), out = {};
let answers = null;
const api = async (m, path) => JSON.parse(JSON.stringify(answers[path]));
const replayUi = () => {}, replayStudies = () => {}, replayPause = () => {}, fail = (e) => { throw e; }, toast = () => {};
function applyHistory(m) { S.bars = m.bars.slice(); }
function applyBars(m) { S.bars.push(...m.bars); }
/*APP*/
(async () => {
  for (const [name, c] of Object.entries(CASES)) {
    [S.rth, answers] = [c.rth, c.answers];
    S.replay = { at: c.at, data: [], idx: 0, kind: 'time', period: 0, nextAt: null, endAt: null, req: 0, empty: 0 };
    await replayLoad();
    out[name] = [[S.bars.at(-1)?.t, S.replay.at]];
    for (let k = 0; k < c.steps; k++) {
      replayStep(); await new Promise((r) => setTimeout(r, 0));
      out[name].push([S.bars.at(-1)?.t, S.replay.at]);
    }
  }
  console.log(JSON.stringify(out));
})();
"""


@needs_node
def test_a_replay_past_a_sessions_end_shows_its_last_bar(app_client, monkeypatch, tmp_path):  # noqa: F811
    """Tick, volume and range bars restart at each session open, so a session's last bar is complete with its hours
    (the RTH close, the session's 16:00 close). Replayed at a clock between that and the next session (a time chart
    stepped to its close and switched to 500v, which keeps the clock; a Go in the evening, the daily break, a weekend)
    it was hidden all the same: it was taken to end where the next one opens, the next session's open, a night or a
    weekend later. The server now says where it ends: end_at for the last bar of a stretch that ends with its session,
    and `e` on a bar the next one opens a session later than; the page ends it there, also as a step's clock."""
    with app_client(week_trades()) as c:
        cases = {}
        for name, (at, rth, steps, now, _) in PAST.items():
            answers, ask = {}, [at]
            while ask:   # the load, and for steps past the bars loaded the next stretch too
                path = f"/api/replay?symbol=ES&spec=500v&at={ask.pop()}&rth={'true' if rth else 'false'}"
                with monkeypatch.context() as m:
                    if now:
                        m.setattr(A, "now_ms", lambda: now)
                    answers[path] = d = c.get(path).json()
                if steps and len(answers) == 1 and d["next_at"]:
                    ask.append(d["next_at"])
            cases[name] = {"at": at, "rth": rth, "steps": steps, "answers": answers}

    def ends(name):   # the bars sent with an end of their own: (open, end)
        return [(b["t"], b["e"]) for d in cases[name]["answers"].values() for b in d["bars"] if "e" in b]
    assert ends("RTH, Go at 18:00 (Wednesday's bars sent too)") == [(DAY(20, 14, 57, 30), DAY(20, 15))]   # RTH close
    assert ends("Go at 17:30, the evening") == [(DAY(20, 15, 45), DAY(20, 16))]   # the session's 16:00 close
    (tmp_path / "cases.json").write_text(json.dumps(cases))   # too long for node's command line
    out = run_app(["specSeconds", "daysSpec", "barEnd", "shownCount", "replayLoad", "replayStep", "replayMore"],
                  PAST_JS.replace("__CASES__", json.dumps(str(tmp_path / "cases.json"))))
    assert out == {name: [list(x) for x in want] for name, (*_, want) in PAST.items()}
