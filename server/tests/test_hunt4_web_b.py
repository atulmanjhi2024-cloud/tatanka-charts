"""Bug hunt round 4, chart page: settings and If/Then blocks broadcast while the page is asking for them are not
undone by the answer (web/app.js loadInd and ifRequest; the drawings' own case is in test_hunt3_drawings.py), and
the chart's relative volume is keyed to the second only on seconds charts (the runner itself is in test_ta.py). The
app.js parts run in node with what they call stubbed."""
from test_hunt_web_chart import needs_node, run_app

# A server that reads what it keeps when asked and answers 30 ms later (the network); onMessage is app.js's socket
# handler, given the broadcasts that arrive before that answer.
BROADCAST_JS = """
const noop = () => {}, J = (x) => JSON.parse(JSON.stringify(x)), wait = (ms) => new Promise((r) => setTimeout(r, ms));
const server = { ind: { schema: { ifthen: {} }, overrides: { tpo: { Ticks: 4 } }, symbols: {}, per_symbol: {} },
  ifthen: { ES: { rows: 'LONG|1|2|old', updated: 1 } } };
async function api(method, path) {
  const snap = J(path === '/api/indicators' ? server.ind : { ...server.ifthen.ES, atr: 12.5 });
  await wait(30);
  return snap;
}
const S = { symbol: 'ES', instruments: { ES: {} }, ind: { schema: {}, overrides: {}, symbols: {}, per_symbol: {} },
  ifData: null, ifRows: [], ifSet: { AtrLength: 14 } };
const local = { set: noop }, indDlg = { refresh: noop }, ifPrim = { update: noop }, $ = () => ({});
const [indApply, renderPlanLines, renderIfInfo, renderPlanIf, amtLayout] = Array(5).fill(noop);
const indReady = () => true, biasParse = (rows) => rows.split('\\n');
/*APP*/
(async () => {
  // the socket comes back: loadInd asks; another device saves the TPO's ticks and ES's level lines meanwhile
  const loading = loadInd();
  await wait(10);
  server.ind.overrides.tpo = { Ticks: 8 };
  onMessage({ type: 'indicators', key: 'tpo', overrides: { Ticks: 8 } });
  server.ind.symbols.lines = { ES: { Top: '7000' } };
  onMessage({ type: 'indicators', key: 'lines', symbol: 'ES', values: { Top: '7000' } });
  await loading;
  const ind = { tpo: S.ind.overrides.tpo, lines: S.ind.symbols.lines, schema: Object.keys(S.ind.schema) };
  // subscribe(): no block yet, ifRequest asks; the block is saved on another device before the answer
  const ifs = [];
  for (const [i, before] of [[2, null], [3, 'kept']]) {   // also the refetch on a bar close, with a block shown
    S.ifData = before && { ...server.ifthen.ES, atr: 12.5 };
    const asking = ifRequest();
    await wait(10);
    server.ifthen.ES = { rows: `SHORT|3|4|new ${i}`, updated: i };
    onMessage({ type: 'ifthen', symbol: 'ES', ...server.ifthen.ES });
    await asking;
    ifs.push({ rows: S.ifData.rows, updated: S.ifData.updated, atr: S.ifData.atr, ifRows: S.ifRows });
  }
  const quiet = ifRequest();   // nothing heard meanwhile: the answer as it is
  await quiet;
  ifs.push({ rows: S.ifData.rows, updated: S.ifData.updated, atr: S.ifData.atr });
  console.log(JSON.stringify({ ind, ifs }));
})();
"""
BROADCAST_PARTS = ["indCache", "indHeard", "loadInd", "indSymbolSaved", "indSaved", "ifHeard", "ifRequest", "onMessage"]


@needs_node
def test_settings_and_if_then_blocks_broadcast_while_asked_for_are_kept():
    """Settings saved on another device while this page's GET /api/indicators is answered (it reconnected) are
    applied over the answer, which may have been read before them, so the next Apply does not save them back; an
    If/Then block broadcast while GET /api/ifthen is answered is kept too, also right after subscribe() (no block
    yet), with the answer's ATR."""
    out = run_app(BROADCAST_PARTS, BROADCAST_JS)
    assert out["ind"] == {"tpo": {"Ticks": 8}, "lines": {"ES": {"Top": "7000"}}, "schema": ["ifthen"]}
    assert out["ifs"] == [
        {"rows": "SHORT|3|4|new 2", "updated": 2, "atr": 12.5, "ifRows": ["SHORT|3|4|new 2"]},
        {"rows": "SHORT|3|4|new 3", "updated": 3, "atr": 12.5, "ifRows": ["SHORT|3|4|new 3"]},
        {"rows": "SHORT|3|4|new 3", "updated": 3, "atr": 12.5},
    ]


# indFull as written: which runner the chart's indicator gets, for each kind of chart
RVOL_JS = """
const IND_DEFS = { rvol: {} }, S = { bars: [{ t: 1, v: 1, seq: 1 }] }, timeOf = (b) => b.t, got = [];
const indRunner = (c, seconds) => { got.push(seconds); return { step: () => [null] }; };
const isTime = () => S.kind === 'time';
/*APP*/
for (const [kind, spec] of [['time', '15s'], ['time', '5m'], ['time', '1D'], ['tick', '500t'], ['volume', '5000v'], ['range', '16r'], ['renko', '8k']]) {
  Object.assign(S, { kind, spec });
  indFull({ c: { type: 'rvol', len: 5 }, plots: [] });
}
console.log(JSON.stringify(got));
"""


@needs_node
def test_relative_volume_is_keyed_to_the_second_only_on_seconds_charts():
    """A seconds chart's bars open on its whole seconds: each is measured against the same second of earlier days.
    Tick, volume, range and Renko bars open at a trade's ms, so theirs go by the minute, as minute and longer bars."""
    assert run_app(["indFull"], RVOL_JS) == [True, False, False, False, False, False, False]
