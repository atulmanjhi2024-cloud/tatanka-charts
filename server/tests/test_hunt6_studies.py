"""Round 6 of the bug hunt, the studies' round-5 fixes: gap alerts saved before the hours flag keep all hours when the
gap settings are saved on an RTH chart, and identical HTF EMA seconds requests share an answer with no HTF bar
forming (the market shut) too."""
import asyncio
import shutil

import pytest

from chartsrv import htfema

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

FOLLOW_JS = """
const calls = [], els = {};
const $ = (q) => (els[q] ||= { textContent: '', checked: false });
const api = async (method, url, body) => { calls.push([url, body.rth]); return { ...body }; };
const gapSpecOk = () => true, indReady = () => true;
const S = { symbol: 'ES', rth: true, st: { gap: true }, gaps: { gaps: [] }, gapError: '',
  instruments: { ES: { rth: ['08:30', '15:00'] }, NQ: { rth: ['08:30', '15:00'] } },
  gapAlerts: { ES: { on: true, new: true, fill: true }, NQ: { on: true, new: true, fill: true } },   // saved before it
  gapSet: { RthStart: 83000, RthEnd: 150000, MinimumGapPoints: 0, AlertOnNewGap: true, AlertOnGapFill: true, EnabledAnalysis: true, days: 90 } };
/*APP*/
(async () => {
  await gapAlertsFollow();                         // a Gap settings save on ES's RTH chart
  console.log(JSON.stringify({ follow: calls }));
})();
"""


@needs_node
def test_gap_alerts_saved_before_the_hours_flag_keep_all_hours_on_a_settings_save():
    from test_hunt_web_chart import run_app
    out = run_app(["hhmm", "gapRth", "gapRthOk", "GAP_RTH_BAD", "gapAlertsPut", "renderGapInfo", "putGapAlerts",
                   "gapAlertsFollow"], FOLLOW_JS)
    # one request with the shared settings and no hours: the server keeps each config's own, none for these (all
    # hours; test_hunt7_gapsmonthly)
    assert out["follow"] == [["/api/gaps/alerts", None]]


def test_identical_requests_with_no_htf_bar_forming_share_one_answer():
    # a Saturday: every HTF bar has closed (due None); three identical requests at once are one computation
    runs = []

    async def work():
        runs.append(1)
        await asyncio.sleep(0.05)
        return {"due": None, "n": len(runs)}

    async def go():
        key = ("ZZ", "15s", 0, "settings")
        htfema.forget("ZZ")
        got = await asyncio.gather(*(htfema.shared(key, 1, work) for _ in range(3)))
        later = await htfema.shared(key, 2, work)   # asked after: worked out again (nothing kept without a due)
        htfema.forget("ZZ")
        return got, later

    got, later = asyncio.run(go())
    assert [d["n"] for d in got] == [1, 1, 1] and later["n"] == 2
