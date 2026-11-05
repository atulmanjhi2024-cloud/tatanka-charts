'use strict';
/* Tatanka Charts front end. One chart, watchlist and alerts; all state lives on the server. */
const L = window.LightweightCharts;
const $ = (s) => document.querySelector(s);
// Browser storage. In a multi-chart layout each chart keeps its own per-chart settings (layout.js, tkCellKey), starting
// from the first chart's; a change to anything a workspace holds saves the open workspace (the page around the charts
// hears a chart's changes through the storage event).
let workspaces = null;   // layout.js createWorkspaces(), on the page itself (not in a chart of a layout)
let linkedChange = false;   // a chart of a layout applying a linked chart's symbol or timeframe: not passed on again
let specForced = '';   // a timeframe the chart's data source forced on it (free data has fewer): not passed on either
const local = {
  get(k, d) {
    try { return localStorage.getItem(tkCellKey(k)) ?? d; } catch { return d; }
  },
  set(k, v) {
    if (tkWaiting() && tkInWorkspace(k)) return;   // this window is about to open a workspace another one opened (layout.js)
    try { localStorage.setItem(tkCellKey(k), v); } catch { /* private mode */ }
    if (workspaces) workspaces.dirty(k);
  },
};
if (TK_CELL !== null) document.body.classList.add('cell');
// The installed app on an iPhone: the page is as tall as the viewport iOS reports, never taller. iOS can report one a
// status bar short of the screen; what lies below it is sometimes drawn and sometimes cut off (it hid the page bar at
// the bottom), so nothing goes there. Short like that, the home indicator is below the page: no room kept for it
// (style.css, body.ios-app, body.ios-short).
if (navigator.standalone && /iPhone/.test(navigator.userAgent)) {
  const fitScreen = () => {
    const long = Math.max(screen.width, screen.height), short = Math.min(screen.width, screen.height);
    const full = matchMedia('(orientation: landscape)').matches ? short : long;
    document.documentElement.style.setProperty('--app-h', `${innerHeight}px`);
    document.body.classList.toggle('ios-short', innerHeight < full - 20);
  };
  document.body.classList.add('ios-app');
  fitScreen();
  addEventListener('resize', fitScreen);
  addEventListener('orientationchange', () => setTimeout(fitScreen, 250));
}
// The keyboard can leave the page scrolled when it closes (iOS): put it back, or what is drawn and what a tap hits drift
// apart.
document.addEventListener('focusout', () => setTimeout(() => {
  if (!document.activeElement?.matches?.('input, textarea, select') && (scrollX || scrollY)) scrollTo(0, 0);
}, 60));
if (TK_CELL) {   // a chart of a layout takes the first chart's settings once (when it first opens), then keeps its own
  try {
    if (localStorage.getItem(tkCellKey('seeded')) === null) {
      for (const k of TK_PER_CHART) {
        const ck = tkCellKey(k), v = localStorage.getItem(k);
        if (localStorage.getItem(ck) === null && v !== null) localStorage.setItem(ck, v);
      }
      localStorage.setItem(tkCellKey('seeded'), '1');
    }
  } catch { /* private mode */ }
}
if (TK_MULTI) { document.body.classList.add('multi'); document.getElementById('grid').hidden = false; }

const TF_GROUPS = [
  ['Seconds', 's', [1, 5, 10, 15, 30], 'built from the trades'],
  ['Minutes', 'm', [1, 2, 3, 5, 10, 15, 20, 30, 45]],
  ['Hours', 'h', [1, 2, 3, 4, 6, 8, 12]],
  ['Days, weeks, months', '', ['1D', '2D', '3D', '1W', '2W', '1M', '3M', '6M', '12M'], 'trade dates (futures: 17:00-16:00 CT sessions), Monday-Friday weeks, calendar months'],
  ['Ticks', 't', [100, 250, 500, 1000, 1500, 2000, 3000, 5000], 'trades per bar'],
  ['Volume', 'v', [500, 1000, 2500, 5000, 10000], 'contracts per bar'],
  ['Range', 'r', [4, 6, 8, 10, 12, 16, 20, 30, 40], 'ticks high to low'],
  ['Renko', 'k', [2, 4, 6, 8, 10, 12, 16, 20], 'brick size in ticks'],
];
const DEFAULT_FAVS = ['1m', '5m', '15m', '1h', '4h', '1D', '1W', '1M', '2000t', '16r'];
const SPEC_RE = /^(\d+[smhtvrk]|\d+[DWM])$/;
const UNIT_ORDER = 'smhDWMtvrk';
// Custom timeframes' limits (chartsrv/bars.py): seconds below a minute, up to 365 days, 52 weeks, 12 months.
const TF_MAX = { s: 59, D: 365, W: 52, M: 12 };
const specLabel = (spec) => (/k$/.test(spec) ? `Renko ${parseInt(spec, 10)}` : spec);   // 8k: Renko bricks of 8 ticks
const calSpec = (spec = S.spec) => /^\d+[DWM]$/.test(spec);   // a bar per session(s), week(s) or month(s)
const specKey = (s) => [UNIT_ORDER.indexOf(s.slice(-1)), parseInt(s, 10)];
const bySpec = (a, b) => { const [ua, na] = specKey(a), [ub, nb] = specKey(b); return ua - ub || na - nb; };
function loadTfCustom() {
  try { const t = JSON.parse(local.get('tfCustom', '[]')); return Array.isArray(t) ? t.filter((x) => SPEC_RE.test(x)) : []; } catch { return []; }
}
function loadFavs() {
  try {
    let f = JSON.parse(local.get('favs', ''));
    if (Array.isArray(f)) {
      if (local.get('favsLong', '') !== '1') {   // once: the 4-hour, daily, weekly and monthly charts on the toolbar
        f = [...new Set([...f, '4h', '1D', '1W', '1M'])];
        local.set('favs', JSON.stringify(f));
      }
      local.set('favsLong', '1');
      return f.filter((s) => SPEC_RE.test(s));
    }
  } catch { /* default */ }
  local.set('favsLong', '1');
  return DEFAULT_FAVS.slice();
}
// The NinjaTrader chart (the built-in study set): Tatanka AMT Toolkit, If/Then Bias Table, Level Lines, Gap Analysis,
// HTF EMA and the price line, with executions shown. Those are on by default and nothing else.
const NT_CHART_SET = ['amt', 'ifthen', 'lines', 'gap', 'hema'];
const VP_KINDS = ['aavp', 'pvp', 'prof'];   // Tatanka Auto Anchored, Periodic and Session Volume Profile
const TATANKA_SET = [...NT_CHART_SET, 'tpo', ...VP_KINDS, 'dsma'];   // the Tatanka indicators with NinjaTrader settings
const STUDY_DEFAULTS = { lv: false, ve: false, vr: false, b1: false, b2: false, b3: false, prof: false, aavp: false, pvp: false, right: 'off', fp: 'off', amtDash: true,
  big: false, fills: true, econ: false, tpo: false, dsma: false, ...Object.fromEntries(NT_CHART_SET.map((k) => [k, true])) };
const ST_PRESET = 2;   // bumped when the defaults change in a way every device should take once
let stReset = false;
function loadStudies() {
  let st = {};
  try { st = JSON.parse(local.get('studies', '{}')) || {}; } catch { /* defaults */ }
  return studiesFrom(st, true);
}
function studiesFrom(st, note = false) {
  st = st && typeof st === 'object' ? { ...st } : {};
  // Once for every saved state, wherever it comes from (this device, or a workspace saved before): start from what
  // the NinjaTrader chart set shows. The mark travels with the studies, so a workspace opened elsewhere takes it too.
  if ((Number(st._preset) || 0) < ST_PRESET) {
    if (note) stReset = Object.keys(st).length > 0;
    st = {};
  }
  if (typeof st.prof === 'string') st.prof = st.prof !== 'off';   // the ETH / RTH session profiles became the Session Volume Profile
  return { ...STUDY_DEFAULTS, ...st, _preset: ST_PRESET };
}
const S = {
  symbol: local.get('sym', 'ES'), spec: local.get('spec', '5m'), st: loadStudies(),
  bars: [], rangeProfile: null, anchoring: false,
  favs: loadFavs(), tfCustom: loadTfCustom(), instruments: {}, levels: {}, alerts: [], events: [], quotes: {},
  subId: 0, kind: 'time', seqTime: new Map(), placing: false, ws: null, retry: 0, lastBar: null, hovering: false,
  loadedKey: '', loadedSym: '', loadNote: '',   // whose bars are on the chart (symbol, timeframe, session); what it waits for
};
// Chart tabs, as on a NinjaTrader chart window: each is a symbol and bar type. Each tab has its own indicators (the
// studies, indicators, their order, hidden ones, and its own Tatanka settings) unless it is linked to the chart's
// shared set (link). Drawings are the symbol's, on every tab of it, unless one is made for its tab only. Kept per
// chart in the workspace; the open tab sets the chart's symbol and timeframe at start.
const TABS_MAX = 30;
const TAB_NAME_MAX = 32;
const TAB_ID = /^[A-Za-z0-9_-]{1,24}$/;
function newTabId() { return `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`; }
const rawLocal = (k, d) => { try { const v = JSON.parse(local.get(k, '')); return v ?? d; } catch { return d; } };
// The chart's shared set, as saved: what linked tabs show (and what each tab started from).
const rawSet = () => { const o = rawLocal('indSet', {}); return o && typeof o === 'object' && !Array.isArray(o) ? o : {}; };
const sharedStudies = () => ({ st: rawLocal('studies', {}), inds: rawLocal('inds', []), layers: rawLocal('layers', []), hidden: rawLocal('stHidden', {}), set: rawSet() });
let cmpLines = [], cmpSeq = 0;   // the compared symbols' lines (compare, below): [{ sym, color, series, id, bars, timer }]
let tabsNew = false;   // a tab got its id (or its own indicators) here: saved at once, so a reload keeps them
// A chart of a layout starts as a copy of the first chart's tabs. The copies take ids of their own, or a drawing kept to
// one tab (This tab only) would show on both charts: any id a chart before this one has is replaced (also in layouts
// saved before).
const tabIdsTaken = new Set();
for (let c = 0; c < (TK_CELL || 0); c++) {
  try { for (const x of JSON.parse(localStorage.getItem(c ? `c${c}.chartTabs` : 'chartTabs')) || []) tabIdsTaken.add(x?.id); } catch { /* none */ }
}
S.tabs = (() => {
  try {
    const t = JSON.parse(local.get('chartTabs', ''));
    const ok = Array.isArray(t) ? t.filter((x) => x && typeof x.sym === 'string' && SPEC_RE.test(x.spec)).slice(0, TABS_MAX)
      .map((x) => ({ id: TAB_ID.test(x.id || '') && !tabIdsTaken.has(x.id) ? x.id : (tabsNew = true, newTabId()), sym: x.sym, spec: x.spec,
        ...(typeof x.name === 'string' && x.name.trim() ? { name: x.name.trim().slice(0, TAB_NAME_MAX) } : {}),
        ...(x.link === true ? { link: true } : x.own && typeof x.own === 'object' ? { own: x.own } : {}),
        ...(x.drawLocal === true ? { drawLocal: true } : {}), ...(x.look && typeof x.look === 'object' ? { look: x.look } : {}),
        ...(Array.isArray(x.cmp) && x.cmp.every((c) => typeof c === 'string') ? { cmp: x.cmp.slice(0, 3), cmpMode: typeof x.cmpMode === 'string' ? x.cmpMode : '' } : {}) })) : [];
    if (ok.length) return ok;
  } catch { /* one tab */ }
  tabsNew = true;
  return [{ id: newTabId(), sym: S.symbol, spec: S.spec }];
})();
for (const t of S.tabs) if (!t.link && !t.own) { t.own = sharedStudies(); tabsNew = true; }   // tabs from before: their own copy of what they showed
S.tab = Math.min(S.tabs.length - 1, Math.max(0, Math.round(Number(local.get('chartTab', '0'))) || 0));
// Each tab has its own chart look: the ⚙ chart settings, price bars on or off, RTH or all hours, the price scale's mode,
// the footprint's settings, the AMT dashboard folded or not. Tabs from before start from what they all showed; the
// open tab's look is the chart's at start.
function lookSaved() {
  const json = (k) => { try { const o = JSON.parse(local.get(k, '{}')); return o && typeof o === 'object' ? o : {}; } catch { return {}; } };
  return { cs: json('chartSet'), barsOff: local.get('barsOff', '0') === '1', rth: local.get('rthOnly', '0') === '1',
    scale: local.get('scaleMode', local.get('scaleLog', '0') === '1' ? 'log' : ''), fp: json('fpSet'), amt: local.get('amtCollapsed', '') };
}
{
  const saved = lookSaved();   // what is missing from a tab's look (all of it, or what came after it was saved)
  for (const t of S.tabs) if (!t.look || Object.keys(saved).some((k) => !(k in t.look))) { t.look = { ...saved, ...(t.look || {}) }; tabsNew = true; }
  const lk = S.tabs[S.tab].look;
  local.set('chartSet', JSON.stringify(lk.cs || {}));
  local.set('barsOff', lk.barsOff ? '1' : '0');
  local.set('rthOnly', lk.rth ? '1' : '0');
  local.set('scaleMode', lk.scale || '');
  local.set('fpSet', JSON.stringify(lk.fp || {}));
  local.set('amtCollapsed', lk.amt || '');   // '': as the device starts (phones folded)
}
// Keeps a change to the chart's look on the open tab (not on a layout's host page: it has no chart).
function lookStore(part, value) {
  const t = S.tabs?.[S.tab];
  if (!t || TK_MULTI) return;
  t.look = { ...(t.look || {}), [part]: value };
  saveTabs();
}
if (tabsNew) saveTabs();
// The open tab when it has its own indicators: on a single chart, and on each chart of a layout (chart 1 shares the
// single chart's storage, so both show the same tab's set). Not the layout's host page, which has no chart.
const ownTab = () => { const t = !TK_MULTI ? S.tabs?.[S.tab] : null; return t && !t.link ? t : null; };
if (ownTab()) S.st = studiesFrom(ownTab().own.st);
// Saves one part of the indicators (st, inds, layers, hidden, set) where the open tab keeps them: its own, or the shared set.
// On a layout's page (no chart of its own) the Studies menu edits the chart last clicked's: that chart saves it.
function studyStore(part, value) {
  if (TK_MULTI) { workspaces?.toActive({ type: 'stpart', part, value }); return true; }
  const t = ownTab();
  if (!t) return false;
  t.own = { ...t.own, [part]: value };
  saveTabs();
  return true;
}
// The Tatanka studies' settings (the AMT Toolkit's too) are each chart's: the open tab's own, the chart's shared set's
// (a linked tab), or on a layout's page the chart last clicked's. Each holds, per study, what differs from the
// server's starting settings (NinjaTrader's defaults, your NinjaTrader settings on top); Apply to all charts (the
// Indicators dialog) sets those and clears the study's from every chart, so they all show it.
S.sharedSet = rawSet();
S.cellSet = {};       // a layout's page: the active chart's open tab's, as it last said
S.cellWhere = '';     // and whose they are, for the dialog
const tabSet = () => (TK_MULTI ? S.cellSet : ownTab() ? ownTab().own.set : S.sharedSet) || {};
// Saves one study's settings (d: what differs from the starting settings) where the open tab keeps them, and shows them.
function tabSetSave(key, d) {
  const set = { ...tabSet() };
  if (d && Object.keys(d).length) set[key] = d; else delete set[key];
  if (TK_MULTI) {   // the chart last clicked saves them
    S.cellSet = set;
    workspaces.toActive({ type: 'indset', key, value: d || {} });
    indSets();
    renderAmtMenu();
    return;
  }
  if (!studyStore('set', set)) { S.sharedSet = set; local.set('indSet', JSON.stringify(set)); }
  if (key === 'amt') { amtFollow(); renderAmtMenu(); } else indApply();
  cellPost('state');
}
// Apply to all charts (the Indicators dialog), after the starting settings were set: no chart keeps its own for this
// study, so every one shows them. This chart's tabs and shared set here; the layout's charts and every other workspace
// through layout.js. Resolves to how many other workspaces took it.
async function studyAll(key) {
  if (TK_CELL !== null) {   // a chart of a layout: the page around it does the rest
    studyAllHere(key);
    window.parent.postMessage({ tk: true, type: 'all', key: 'study', study: key }, location.origin);
    return 0;
  }
  if (TK_MULTI) { const { [key]: _, ...rest } = S.cellSet; S.cellSet = rest; indSets(); renderAmtMenu(); } else studyAllHere(key);
  return workspaces ? workspaces.studyAll(key) : 0;
}
function cellPart(part, v) {   // the page around the charts: its Studies menu edited this chart's
  if (part === 'inds' && Array.isArray(v)) { S.indCfg = v.map(indClean).filter(Boolean).slice(0, IND_MAX); indsChanged(); }
  else if (part === 'layers' && Array.isArray(v)) setLayers(v);
  else if (part === 'hidden' && v && typeof v === 'object' && !Array.isArray(v)) { S.stHidden = { ...v }; saveHidden(); renderStudiesMenu(); }
}
function studyAllHere(key) {
  let n = 0;
  for (const t of S.tabs) {
    if (t.own?.set && key in t.own.set) { const { [key]: _, ...rest } = t.own.set; t.own = { ...t.own, set: rest }; n += 1; }
  }
  if (n) saveTabs();
  if (key in S.sharedSet) { const { [key]: _, ...rest } = S.sharedSet; S.sharedSet = rest; local.set('indSet', JSON.stringify(rest)); }
  if (key === 'amt') { renderAmtMenu(); amtFollow(); } else indApply();
  indDlg.refresh();
  cellPost('state');
}
// Whose settings the dialog edits: this tab's, the chart's shared ones, or (a layout's page) the active chart's.
function setWhere() {
  if (TK_MULTI) return S.cellWhere || "the chart last clicked's open tab";
  const t = S.tabs?.[S.tab], name = t ? (t.name || `${t.sym} ${specLabel(t.spec)}`) : S.symbol;
  const chart = TK_CELL !== null ? `chart ${TK_CELL + 1}'s` : "this chart's";
  return ownTab() ? `${chart} tab ${name}` : `${chart} linked tabs`;
}
S.symbol = S.tabs[S.tab].sym;
S.spec = S.tabs[S.tab].spec;
S.rth = local.get('rthOnly', '0') === '1';   // regular trading hours only (the server's RTH-only bars)
// style.css's theme values, read once: chart drawing asks for them every frame. (The ones set at run time, --chart-bg,
// --bar-up, --bar-down, --app-h, are not read here.)
const cssSeen = new Map();
const css = (v) => {
  let x = cssSeen.get(v);
  if (x === undefined) { x = getComputedStyle(document.documentElement).getPropertyValue(v).trim(); cssSeen.set(v, x); }
  return x;
};
// NinjaTrader's chart (your Chart style: candles #00B4FF / #FF5252 / doji #CCCCCC, outline and wick match the body, on
// #222222) and the AMT Toolkit's level colors. C.up / C.down follow the chart settings; the app's own --up / --down
// color text only.
const NT = { bg: '#222222', grid: '#2c2c2c', text: '#b4b4b4', up: '#00b4ff', down: '#ff5252', doji: '#cccccc',
  pivot: '#eae857', res: '#ff3333', sup: '#00b300' };
const C = { up: NT.up, down: NT.down, doji: NT.doji, pivot: css('--pivot'), accent: css('--accent'), dim: css('--fg-dim') };

// ---- time: every time shown is in the time zone picked at the clock (yours by default): the axis, crosshair, clock,
// Calendar, alerts, tape, journal, replay, drawings, watchlist. Exchange time (CT) stays for what the exchange defines:
// sessions and their settings, trade dates (D/W/M bars), and the logic behind them ---------------------------------------
const tz = { timeZone: 'America/Chicago' };
const fmtHM = new Intl.DateTimeFormat('en-US', { ...tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const fmtDay = new Intl.DateTimeFormat('en-US', { ...tz, month: 'short', day: 'numeric' });
const fmtDate = new Intl.DateTimeFormat('en-US', { ...tz, weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
const fmtMonth = new Intl.DateTimeFormat('en-US', { ...tz, month: 'short', year: 'numeric' });
const fmtYear = new Intl.DateTimeFormat('en-US', { ...tz, year: 'numeric' });
const fmtFull = new Intl.DateTimeFormat('en-US', { ...tz, weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const isTime = () => S.kind === 'time';
// Tick, range and volume bars use their sequence number as x so bars sharing a second stay distinct.
function labelMs(t) {
  let ms = isTime() ? t * 1000 : S.seqTime.get(t);
  if (ms != null && calSpec()) ms += 7 * 3600e3; // session opens 17:00 the day before; label the trade date
  return ms;
}
const inst = () => S.instruments[S.symbol] || (S.ext?.decimals != null
  ? { symbol: S.symbol, name: S.ext.name, decimals: S.ext.decimals, tick_size: S.ext.tick, point_value: 1, rth: S.ext.exchange?.rth || null }
  : { decimals: 2, tick_size: 0.01 });
const indFmt = () => `${inst().decimals} ${inst().tick_size}`;   // what the indicators' price format is made from (indsBuild)
const px = (p) => (p == null ? '–' : bondTick(inst().symbol) ? bond32(p, inst().tick_size) : Number(p).toFixed(inst().decimals));   // Treasuries: 32nds

// The time zone picked at the clock (kept in the workspace): 'local' (the device's), 'exchange' (the chart's exchange:
// Chicago for the futures, New York for US stocks, UTC for crypto, ...) or a zone name.
const TZ_EXCHANGE = 'America/Chicago';
// The chart's exchange clock: {tz, rth ["HH:MM", "HH:MM"] | null, day: when its trading day starts}. The futures here are
// CME's; a free-data chart's comes with its bars (ext.py).
const exch = () => (S.ext ? S.ext.exchange || { tz: 'America/New_York', rth: null, day: '00:00' } : { tz: TZ_EXCHANGE, rth: inst().rth || null, day: '17:00' });
const tzCity = (zone) => zone.split('/').pop().replace(/_/g, ' ');
const TZ_LOCAL = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || TZ_EXCHANGE; } catch { return TZ_EXCHANGE; } })();
const TZ_CHOICES = [['local', 'Local'], ['exchange', 'Exchange'], ['UTC', 'UTC'], ['America/New_York', 'New York'], ['America/Denver', 'Denver'],
  ['America/Phoenix', 'Phoenix'], ['America/Los_Angeles', 'Los Angeles'], ['Pacific/Honolulu', 'Honolulu'], ['America/Sao_Paulo', 'São Paulo'],
  ['Europe/London', 'London'], ['Europe/Berlin', 'Frankfurt'], ['Europe/Zurich', 'Zurich'], ['Asia/Dubai', 'Dubai'], ['Asia/Kolkata', 'Kolkata'],
  ['Asia/Singapore', 'Singapore'], ['Asia/Hong_Kong', 'Hong Kong'], ['Asia/Tokyo', 'Tokyo'], ['Australia/Sydney', 'Sydney']];
const TZ_SHORT = { 'America/Chicago': 'CT', 'America/New_York': 'ET', 'America/Denver': 'MT', 'America/Phoenix': 'MST', 'America/Los_Angeles': 'PT', UTC: 'UTC' };
const tzZone = (v) => (v === 'local' ? TZ_LOCAL : v === 'exchange' ? exch().tz : v);
// A zone's clock read as UTC (for where the axis marks a new day, hour, ...); its offset found once per UTC hour.
function wallClock(zone) {
  const f = new Intl.DateTimeFormat('en-US', { timeZone: zone, year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', hourCycle: 'h23' });
  const offs = new Map();
  return (ms) => {
    const h = Math.floor(ms / 3600e3);
    let off = offs.get(h);
    if (off === undefined) {
      const p = Object.fromEntries(f.formatToParts(h * 3600e3).map((x) => [x.type, Number(x.value)]));
      offs.set(h, (off = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute) - h * 3600e3));
    }
    return ms + off;
  };
}
const ctWall = wallClock(TZ_EXCHANGE);
// A datetime-local box's "YYYY-MM-DDTHH:MM" on a zone's clock, both ways.
function inputClock(zone) {
  const f = new Intl.DateTimeFormat('sv-SE', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const re = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/;
  const toInput = (ms) => f.format(ms).replace(' ', 'T');
  const toMs = (v) => {
    const m = re.exec(v || '');
    if (!m) return null;
    const want = Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5]);
    let ms = want;
    for (let k = 0; k < 3; k++) {
      const g = re.exec(toInput(ms));
      ms += want - Date.UTC(+g[1], g[2] - 1, +g[3], +g[4], +g[5]);
    }
    return ms;
  };
  return { toInput, toMs };
}
const { toInput: msToCtInput, toMs: ctInputToMs } = inputClock(TZ_EXCHANGE);   // the Chicago clock
let ax = null;   // the chosen zone: {pick, zone, short, hm, day, calDay, date, md, wkHm, full, tape, clock, wall, input}
function axZone(pick = local.get('timeZone', 'local')) {
  let zone = tzZone(pick);
  try { new Intl.DateTimeFormat('en-US', { timeZone: zone }); } catch { pick = 'local'; zone = TZ_LOCAL; }   // a zone this browser lacks
  const z = { timeZone: zone };
  const short = TZ_SHORT[zone] || new Intl.DateTimeFormat('en-US', { ...z, timeZoneName: 'short' }).formatToParts(Date.now()).find((x) => x.type === 'timeZoneName')?.value || zone;
  ax = { pick, zone, short, wall: wallClock(zone), input: inputClock(zone),
    hm: new Intl.DateTimeFormat('en-US', { ...z, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }),
    day: new Intl.DateTimeFormat('en-US', { ...z, month: 'short', day: 'numeric' }),
    calDay: new Intl.DateTimeFormat('en-US', { ...z, weekday: 'short', month: 'short', day: 'numeric' }),
    date: new Intl.DateTimeFormat('en-US', { ...z, month: 'short', day: 'numeric', year: 'numeric' }),
    md: new Intl.DateTimeFormat('en-US', { ...z, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }),
    wkHm: new Intl.DateTimeFormat('en-US', { ...z, weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }),
    tape: new Intl.DateTimeFormat('en-US', { ...z, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', fractionalSecondDigits: 3 }),
    full: new Intl.DateTimeFormat('en-US', { ...z, weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }),
    clock: new Intl.DateTimeFormat('en-US', { ...z, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }) };
}
axZone();
// The chosen zone's name at a moment: "GMT+1" in a London summer, "GMT" in its winter (CT, ET and the like stay).
const zoneShorts = new Map();
function zs(ms) {
  if (TZ_SHORT[ax.zone]) return TZ_SHORT[ax.zone];
  const k = `${ax.zone}|${Math.floor(ms / 3600e3)}`;
  if (!zoneShorts.has(k)) {
    zoneShorts.set(k, new Intl.DateTimeFormat('en-US', { timeZone: ax.zone, timeZoneName: 'short' }).formatToParts(ms)
      .find((x) => x.type === 'timeZoneName')?.value || ax.zone);
  }
  return zoneShorts.get(k);
}
// A time of day on an exchange's clock ("17:00"; CME's by default) as the chosen zone's clock shows it today.
const inputClocks = new Map();
const clockOf = (tz) => inputClocks.get(tz) || inputClocks.set(tz, inputClock(tz)).get(tz);
const exToZone = (hhmm, tz = TZ_EXCHANGE) => (ax.zone === tz ? hhmm : ax.hm.format(clockOf(tz).toMs(`${clockOf(tz).toInput(Date.now()).slice(0, 10)}T${hhmm}`)));
const zoneTimes = (v, tz = TZ_EXCHANGE) => `${v.split('-').map((x) => exToZone(x, tz)).join('-')} ${ax.short}`;   // "08:30-15:00" -> "09:30-16:00 ET"
// An indicator's "HHmm-HHmm" session, written for ES/NQ's 08:30-15:00 CT, at this instrument's own regular hours, as
// the server runs it (sessions.rth_mover): a time nearer the open moves with the open, one nearer the close with it.
function ownHours(v) {
  const r = inst().rth, hm = (s) => Number(s.slice(0, 2)) * 60 + Number(s.slice(-2));
  if (!r || typeof v !== 'string' || !/^\d{3,4}-\d{3,4}/.test(v)) return v;
  const [o, c] = r.map(hm);
  if (o === 510 && c === 900) return v;
  const [rng, ...days] = v.split(':');
  return [rng.split('-').map((e) => {
    const m = hm(e.padStart(4, '0')), n = m < 420 || m >= 960 ? m : m + (Math.abs(m - 510) <= Math.abs(m - 900) ? o - 510 : c - 900);
    return `${String(Math.floor(n / 60)).padStart(2, '0')}${String(n % 60).padStart(2, '0')}`;
  }).join('-'), ...days].join(':');
}
function zoneTexts() {   // the page's fixed texts: [data-ct] holds exchange times, [data-zone] the zone's name
  document.querySelectorAll('[data-ct]').forEach((e) => { e.textContent = zoneTimes(e.dataset.ct); });
  document.querySelectorAll('[data-zone]').forEach((e) => { e.textContent = ax.short; });
}
zoneTexts();

// Where the time axis marks a new day (the date), hour, 30 minutes: the library decides that in UTC, which put the
// date at 19:00 CT (18:00 in winter). These follow the axis zone's clock instead: the date at its midnight, as
// TradingView; on D/W/M charts at the trade date (CT; the session opens 17:00 the day before).
const WEIGHT_STEPS = [[43200e3, 33], [21600e3, 32], [10800e3, 31], [3600e3, 30], [1800e3, 22], [300e3, 21], [60e3, 20], [1e3, 10]];
function wallWeight(cur, prev) {   // the library's weights (year 70, month 60, day 50, 12h ... 1s), on a wall clock
  const a = new Date(cur), b = new Date(prev);
  if (a.getUTCFullYear() !== b.getUTCFullYear()) return 70;
  if (a.getUTCMonth() !== b.getUTCMonth()) return 60;
  if (a.getUTCDate() !== b.getUTCDate()) return 50;
  for (const [d, w] of WEIGHT_STEPS) if (Math.floor(cur / d) !== Math.floor(prev / d)) return w;
  return 0;
}
class ZoneTimeScale extends L.defaultHorzScaleBehavior() {
  fillWeightsForPoints(points, start = 0) {
    if (!isTime() || typeof points[0]?.originalTime !== 'number') { super.fillWeightsForPoints(points, start); return; }   // tick/range/volume: x is a sequence number
    const wall = calSpec() ? (p) => ctWall(p.originalTime * 1000) + 7 * 3600e3 : (p) => ax.wall(p.originalTime * 1000);
    let prev = start ? wall(points[start - 1]) : null, total = 0;
    for (let i = start; i < points.length; i++) {
      const cur = wall(points[i]);
      if (prev !== null) points[i].timeWeight = wallWeight(cur, prev);
      total += cur - (prev ?? cur);
      prev = cur;
    }
    if (!start && points.length > 1) points[0].timeWeight = wallWeight(wall(points[0]), wall(points[0]) - Math.ceil(total / (points.length - 1)));
  }
}
const axTick = (t, type) => {
  const ms = labelMs(t);
  if (ms == null) return '';
  if (/M$/.test(S.spec) || (calSpec() && type <= 1)) return type === 0 ? fmtYear.format(ms) : fmtMonth.format(ms);
  if (calSpec()) return fmtDay.format(ms);   // a trade date (CT), whatever the zone
  return type <= 2 ? ax.day.format(ms) : ax.hm.format(ms);
};
const axTime = (t) => {
  const ms = labelMs(t);
  return ms == null ? '' : /\dM$/.test(S.spec) ? fmtMonth.format(ms) : /\dW$/.test(S.spec) ? `Week of ${fmtDate.format(ms)}`
    : /\dD$/.test(S.spec) ? fmtDate.format(ms) : ax.full.format(ms);
};

// ---- chart --------------------------------------------------------------------------------
const chart = L.createChartEx($('#chart'), new ZoneTimeScale(), {
  autoSize: true,
  // No logo on the chart; the credit the library's license asks for is in the chart settings menu.
  layout: { background: { type: L.ColorType.Solid, color: NT.bg }, textColor: NT.text, fontFamily: css('--font'), attributionLogo: false },
  grid: { vertLines: { color: NT.grid }, horzLines: { color: NT.grid } },
  rightPriceScale: { borderColor: '#3a3a3a' },
  // A long press shows the crosshair while the finger is down; lifting it ends that (the library's default kept it
  // until a tap on the chart, and every drag till then moved the crosshair, not the chart: it looked frozen).
  trackingMode: { exitMode: L.TrackingModeExitMode.OnTouchEnd },
  timeScale: {
    borderColor: '#3a3a3a', rightOffset: 6, timeVisible: true, secondsVisible: false, tickMarkFormatter: axTick,
  },
  localization: { dateFormat: "dd MMM 'yy", timeFormatter: axTime },
  crosshair: { mode: L.CrosshairMode.Normal },
});
const candles = chart.addSeries(L.CandlestickSeries, { upColor: C.up, downColor: C.down, wickUpColor: C.up, wickDownColor: C.down, borderVisible: false });
const vol = chart.addSeries(L.HistogramSeries, { priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

// ---- chart settings (per device): bar style, colors, grid, session breaks, crosshair, price line, countdown ------
const CS_DEFAULTS = { style: 'candles', up: NT.up, down: NT.down, doji: NT.doji, bg: NT.bg, volume: true, priceOnly: false, priceLine: true,
  countdown: true, gridLines: 'both', gridColor: NT.grid, textColor: NT.text, xhairColor: '#9598a1', fontSize: 12, watermark: false,
  sessions: true, crosshair: 'normal', rightBars: 6, plAuto: true, plColor: '#ffffff', plStyle: 'dashed', plWidth: 1, plOpacity: 100, dataWin: false };
const LW_STYLE = { solid: 0, dotted: 1, dashed: 2, large: 3 };
// Background, grid and axis text together, as the chart themes in NinjaTrader and TradingView.
const CS_THEMES = { nt: { bg: NT.bg, gridColor: NT.grid, textColor: NT.text }, black: { bg: '#000000', gridColor: '#1a1a1a', textColor: '#a0a0a0' },
  navy: { bg: '#131722', gridColor: '#1e222d', textColor: '#b2b5be' } };
function csFrom(o) {   // saved or template settings over the defaults; the old Grid on/off is Grid Both or Off
  const cs = { ...CS_DEFAULTS, ...o };
  if (typeof o.grid === 'boolean' && !o.gridLines) cs.gridLines = o.grid ? 'both' : 'off';
  delete cs.grid;
  return cs;
}
function loadCs() {
  let o = {};
  try { o = JSON.parse(local.get('chartSet', '{}')) || {}; } catch { /* defaults */ }
  return csFrom(o);
}
S.cs = loadCs();
C.up = S.cs.up;
C.down = S.cs.down;
C.doji = S.cs.doji;
let altSeries = null;   // OHLC bars or a close line drawn over see-through candles (the candles keep the primitives)
const altPoint = (b) => (altSeries?._style === 'line' ? { time: timeOf(b), value: b.c } : { time: timeOf(b), open: b.o, high: b.h, low: b.l, close: b.c });
// Heikin Ashi (NinjaTrader's HeikenAshi): open = average of the previous HA open and close, close = (O+H+L+C)/4,
// high and low widened to include them, on the instrument's tick. Display only: studies use the real bars.
S.ha = [];
function haAt(i) {
  const b = S.bars[i], p = S.ha[i - 1], t = inst().tick_size, r = (v) => Math.round(v / t) * t;
  const o = r(p ? (p.o + p.c) / 2 : (b.o + b.c) / 2), c = r((b.o + b.h + b.l + b.c) / 4);
  return (S.ha[i] = { o, c, h: Math.max(b.h, o, c), l: Math.min(b.l, o, c) });
}
const haPoint = (i) => { const h = haAt(i); return { time: timeOf(S.bars[i]), open: h.o, high: h.h, low: h.l, close: h.c }; };
function altSetData() {
  if (!altSeries) return;
  if (S.cs.style === 'ha') { S.ha = []; altSeries.setData(S.bars.map((_, i) => haPoint(i))); } else altSeries.setData(S.bars.map(altPoint));
}
const CLEAR = 'rgba(0, 0, 0, 0)';
// Candles as the style asks; see-through when the footprint or another style draws the bars.
// Price bars off (this chart only, for analysis): the candles go see-through (they keep the studies and drawings
// attached to them) and other bar styles hide; the last price stays on the axis.
S.barsOff = local.get('barsOff', '0') === '1';
function setBarsOff(on) {
  S.barsOff = on;
  local.set('barsOff', on ? '1' : '0');
  lookStore('barsOff', on);
  barsLayout();
  barsUi();
}
function barsUi() {
  const b = $('#bars-eye');
  b.innerHTML = EYE_SVG(S.barsOff);
  b.title = S.barsOff ? 'Show the price bars' : 'Hide the price bars (studies and drawings stay)';
  $('#legend-line').classList.toggle('off', S.barsOff);
  $('#cs-bars').checked = !S.barsOff;
}
function barsLayout() {
  const want = extSeries() ? 'line' : ['bars', 'line', 'ha'].includes(S.cs.style) ? S.cs.style : null;   // FRED, EIA: a line
  if (altSeries && (!want || altSeries._style !== want)) { chart.removeSeries(altSeries); altSeries = null; }
  if (want && !altSeries) {
    altSeries = want === 'bars'
      ? chart.addSeries(L.BarSeries, { upColor: C.up, downColor: C.down, thinBars: false }, 0)
      : want === 'ha' ? chart.addSeries(L.CandlestickSeries, { upColor: C.up, downColor: C.down, wickUpColor: C.up, wickDownColor: C.down, borderVisible: false }, 0)
      : chart.addSeries(L.LineSeries, { color: css('--fg'), lineWidth: 2, crosshairMarkerVisible: false }, 0);
    altSeries._style = want;
    altSetData();
    if (S.layersReady) layerSeries();
  }
  const fp = S.st.fp !== 'off' && !S.ext, off = S.barsOff, hide = fp || !!altSeries || off, ha = altSeries?._style === 'ha';   // HA: the real price on the axis
  if (hide !== S.candlesHidden) {   // a doji's own colors would show through see-through candles
    S.candlesHidden = hide;
    if (S.bars?.length) candles.setData(S.bars.map((b) => toPoints(b)[0]));
  }
  const axis = !altSeries || ha || fp || off;   // the candles carry the last price unless the other style shows it
  candles.applyOptions(hide ? { upColor: CLEAR, downColor: CLEAR, wickUpColor: CLEAR, wickDownColor: CLEAR, borderVisible: false, lastValueVisible: axis, priceLineVisible: axis && S.cs.priceLine }
    : S.cs.style === 'hollow' ? { upColor: CLEAR, downColor: C.down, wickUpColor: C.up, wickDownColor: C.down, borderVisible: true, borderUpColor: C.up, borderDownColor: C.down, lastValueVisible: true, priceLineVisible: S.cs.priceLine }
    : { upColor: C.up, downColor: C.down, wickUpColor: C.up, wickDownColor: C.down, borderVisible: false, lastValueVisible: true, priceLineVisible: S.cs.priceLine });
  if (altSeries) altSeries.applyOptions({ visible: !fp && !off, upColor: C.up, downColor: C.down, priceLineVisible: !ha && S.cs.priceLine, lastValueVisible: !ha,
    ...(ha ? { wickUpColor: C.up, wickDownColor: C.down } : {}) });
  priceLineLayout(true);
}
// The last price line: the bar's color or one picked, solid to long dash, 1-4 px, see-through or not. The bar's color
// see-through is the up or down color of the last bar, set again when that bar turns.
let plKey = '';
function priceLineLayout(force = false) {
  const op = Math.min(1, Math.max(0.1, S.cs.plOpacity / 100)), b = S.lastBar;
  const color = !S.cs.plAuto ? (op < 1 ? `${rgbPrefix(S.cs.plColor)}${op})` : S.cs.plColor)
    : op >= 1 ? '' : `${rgbPrefix(!b || b.c > b.o ? C.up : b.c < b.o ? C.down : C.doji)}${op})`;
  const o = { priceLineColor: color, priceLineStyle: LW_STYLE[S.cs.plStyle] ?? 2, priceLineWidth: S.cs.plWidth };
  const key = `${color}|${o.priceLineStyle}|${o.priceLineWidth}`;
  if (!force && key === plKey) return;
  plKey = key;
  candles.applyOptions(o);
  if (altSeries) altSeries.applyOptions(o);
}
function chartLayout() {
  document.documentElement.style.setProperty('--chart-bg', S.cs.bg);
  document.documentElement.style.setProperty('--bar-up', C.up);     // the tape colors trades as the bars are colored
  document.documentElement.style.setProperty('--bar-down', C.down);
  chart.applyOptions({
    layout: { background: { type: L.ColorType.Solid, color: S.cs.bg }, textColor: S.cs.textColor, fontSize: S.cs.fontSize },
    grid: { vertLines: { visible: ['both', 'vert'].includes(S.cs.gridLines), color: S.cs.gridColor },
      horzLines: { visible: ['both', 'horz'].includes(S.cs.gridLines), color: S.cs.gridColor } },
    crosshair: { mode: S.cs.crosshair === 'magnet' ? L.CrosshairMode.MagnetOHLC : L.CrosshairMode.Normal,
      vertLine: { color: S.cs.xhairColor }, horzLine: { color: S.cs.xhairColor } },
  });   // the right margin: amtLayout (it never goes below the setting)
  vol.applyOptions({ visible: S.cs.volume });
  axisStack.height = Math.ceil(S.cs.fontSize * 1.5) + 1;   // the axis tags' height at this font, for stacking them
}
// Scale on price only: other series on the price pane stop counting toward its auto-scale.
const keepScale = (base) => base();
function priceOnlyLayout() {
  for (const s of chart.panes()[0].getSeries()) {
    if (s === candles || s === vol || s === altSeries) continue;
    s.applyOptions({ autoscaleInfoProvider: S.cs.priceOnly ? () => null : keepScale });
  }
}

// ---- VWAP lines: session (17:00) and RTH, each with 1-3 sigma bands, plus anchored VWAP ----------
const lineOpts = { lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, visible: false };
const VWAPS = [
  { key: 've', color: '#b39ddb', at: 0 },  // bar.st[0] = VWAP, st[1] = standard deviation
  { key: 'vr', color: '#4dd0e1', at: 2 },  // bar.st[2], st[3]: RTH, empty outside RTH
];
for (const w of VWAPS) {
  w.mid = chart.addSeries(L.LineSeries, { ...lineOpts, color: w.color, lineWidth: 2 });
  w.bands = [1, 2, 3].map((k) => ({
    k,
    up: chart.addSeries(L.LineSeries, { ...lineOpts, color: `${w.color}88`, lineWidth: 1, lineStyle: L.LineStyle.Dashed }),
    dn: chart.addSeries(L.LineSeries, { ...lineOpts, color: `${w.color}88`, lineWidth: 1, lineStyle: L.LineStyle.Dashed }),
  }));
}
const avwap = chart.addSeries(L.LineSeries, { ...lineOpts, color: '#f06292', lineWidth: 2 });
const studyLines = [...VWAPS.flatMap((w) => [w.mid, ...w.bands.flatMap((b) => [b.up, b.dn])]), avwap];

// ---- footprint: bid x ask per price from stored trades, fetched for the bars in view ---------------------
const fpFrom = (o) => ({ rows: 'auto', ratio: 300, poc: true, minVol: 0, ...o });
function loadFpSet() {
  let o = {};
  try { o = JSON.parse(local.get('fpSet', '{}')) || {}; } catch { /* defaults */ }
  return fpFrom(o);
}
S.fpSet = loadFpSet();
S.fp = new Map();   // bar number (seq: bars can share an open time) -> {rows: [[price / tick, bid, ask], ...]}
const rgbPrefix = (hex) => `rgba(${parseInt(hex.slice(1, 3), 16)}, ${parseInt(hex.slice(3, 5), 16)}, ${parseInt(hex.slice(5, 7), 16)}, `;
const fpPrim = new FootprintPrimitive(() => (S.st.fp === 'off' || S.ext ? null : {
  mode: S.st.fp, bars: S.bars, fp: S.fp, tick: inst().tick_size, rows: S.fpSet.rows === 'auto' ? 'auto' : Number(S.fpSet.rows),
  ratio: S.fpSet.ratio / 100, minVol: S.fpSet.minVol, poc: S.fpSet.poc, up: C.up, down: C.down,
  upRgb: rgbPrefix(C.up), downRgb: rgbPrefix(C.down),
}));
candles.attachPrimitive(fpPrim);
// The footprint draws every bar itself (cells zoomed in, candles zoomed out), so the bar series step aside.
function fpLayout() {
  barsLayout();
  fpPrim.update();
}
let fpBusy = false, fpAgain = false, fpTimer = 0;
const fpSchedule = (delay = 150) => { clearTimeout(fpTimer); fpTimer = setTimeout(fpFetch, delay); };
// Fetches the bars in view that are not cached yet, and the forming bar every time.
async function fpFetch(onlyLast = false) {
  // replay: the live bars until its own come; live again, or another chart: the old ones until the history comes
  if (S.st.fp === 'off' || !S.bars.length || S.ext || S.replay?.idx === 0 || S.liveWait) return;
  const ts = chart.timeScale(), r = ts.getVisibleLogicalRange(), n = S.bars.length;
  if (!r || ts.options().barSpacing < FP_MIN_SPACING) return;
  if (fpBusy) { fpAgain = true; return; }
  const i0 = Math.max(0, Math.floor(r.from) - 1), i1 = Math.min(n - 1, Math.ceil(r.to) + 1);
  const need = [];
  for (let i = onlyLast === true ? Math.max(i0, n - 1) : i0; i <= i1; i++) if (i === n - 1 || !S.fp.has(S.bars[i].seq)) need.push(i);
  if (!need.length) return;
  const b = need[need.length - 1], key = `${S.symbol} ${S.spec} ${S.rth}`, rp = S.replay;
  // Tick, volume, range and Renko bars: the bar after b can open in the millisecond of b's last trades, or with b (one
  // print finishing several), so it goes along to just past that millisecond for b's share of it, and is not kept.
  // In replay the one after the last bar shown is hidden in its data (past the bars loaded: the next stretch's first,
  // at nextAt, or at the present the one still forming, at endAt, which is where its hours end when none can be), and
  // a time bar there ends at its end, not at the replay clock (a time picked inside the next one).
  const after = rp && (rp.nextAt ?? rp.endAt);
  const next = b + 1 < n ? S.bars[b + 1] : rp && !isTime() ? rp.data[rp.idx] ?? (after != null ? { t: after, v: 0 } : null) : null;
  const c = !isTime() && next ? b + 1 : b;
  let a = Math.max(need[0], c - 399);
  const end = c > b ? next.t + 1 : next ? next.t
    : rp ? Math.min(rp.at, barEnd(rp.data, rp.idx - 1, rp.kind, rp.period, rp.days, rp.nextAt))
    : Math.max(Date.now() + 3600e3, S.bars[b].t + 1);   // a D chart's bar of a holiday halt: stamped at the next day's open
  while (a < b && end - S.bars[a].t > 8 * 86_400_000) a += 1;   // the server reads at most 8 days of trades a time
  if (end - S.bars[a].t > 8 * 86_400_000) return;                // a week or month bar: no footprint
  const all = S.bars, bars = all.slice(a, b + 1).concat(c > b ? [next] : []), starts = bars.map((x) => x.t);
  const live = all[n - 1].seq;   // the forming bar
  // Tick, volume, range and Renko bars: one print can finish several, so a trade at the time one opens is shared out by
  // their volumes; the first bar's pre (what its opening millisecond gave the bars before it, not sent) is left out.
  const vols = isTime() ? {} : { vols: bars.map((x) => x.v), pre: bars[0].pre || 0 };
  fpBusy = true;
  try {
    // RTH: not the night after each session's last bar; spec: a D chart's bars hold their trade dates' sessions
    const d = await api('POST', '/api/footprint', { symbol: S.symbol, starts, ...vols, end, rth: S.rth, spec: S.spec });
    if (key === `${S.symbol} ${S.spec} ${S.rth}` && all === S.bars) {   // the same load: bars loaded again can be numbered anew
      // The forming bar closed while this was out: the answer can hold the next bar's first trades, or miss its own
      // last ones. applyBars has dropped it and asks again, with the bar after it.
      d.bars.slice(0, b - a + 1).forEach((bar, k) => { if (bars[k].seq !== live || all.at(-1).seq === live) S.fp.set(bars[k].seq, bar); });
      if (S.fp.size > 3000) for (const seq of [...S.fp.keys()].slice(0, S.fp.size - 2000)) S.fp.delete(seq);
      fpPrim.update();
    }
  } catch (e) { console.warn('footprint', e); } finally { fpBusy = false; }
  if (fpAgain) { fpAgain = false; fpSchedule(); }
}
chart.timeScale().subscribeVisibleLogicalRangeChange(() => { if (S.st.fp !== 'off') fpSchedule(); });
// The forming bar fills in live: refresh it once a second while it is in view.
setInterval(() => {
  if (S.st.fp === 'off' || !S.bars.length || document.visibilityState !== 'visible') return;
  const r = chart.timeScale().getVisibleLogicalRange();
  if (!S.replay && r && r.to >= S.bars.length - 1) fpFetch(true);
}, 1000);
function renderFpMenu() {
  $('#fp-rows').value = String(S.fpSet.rows);
  $('#fp-ratio').value = S.fpSet.ratio;
  $('#fp-poc').checked = !!S.fpSet.poc;
}
function fpSettingsChanged() { local.set('fpSet', JSON.stringify(S.fpSet)); lookStore('fp', { ...S.fpSet }); fpPrim.update(); }
$('#fp-rows').onchange = (e) => { S.fpSet.rows = e.target.value; fpSettingsChanged(); };
$('#fp-ratio').onchange = (e) => {
  const v = Math.round(Number(e.target.value));
  if (!(v >= 101 && v <= 2000)) { e.target.value = S.fpSet.ratio; toast('Imbalance: 101 to 2000%'); return; }
  S.fpSet.ratio = v;
  fpSettingsChanged();
};
$('#fp-poc').onchange = (e) => { S.fpSet.poc = e.target.checked; fpSettingsChanged(); };

// ---- Tatanka indicator settings: every NinjaTrader property of the TPO Profile, Gap Analysis, HTF EMA, If/Then and
// Level Lines with NinjaTrader's default (the server's nt_schema.json), the changes stored on the server for every
// device, a few per symbol. The AMT Toolkit's are in S.amtServer. Edited in the Indicators dialog and the Plan tab. ----
S.ind = (() => { try { return JSON.parse(local.get('indCache', '')) || null; } catch { return null; } })()
  || { schema: {}, overrides: {}, symbols: {}, per_symbol: {} };
const indReady = (key) => !!S.ind.schema[key];
const indDefaults = (key) => Object.fromEntries((S.ind.schema[key]?.settings || []).map((x) => [x.name, x.default]));
function indValues(key, sym = S.symbol) {
  const own = S.ind.symbols[key]?.[sym] || {}, tab = tabSet()[key] || {};
  const v = { ...indDefaults(key), ...(S.ind.overrides[key] || {}), ...own, ...tab };   // this tab's settings last
  if (key === 'ifthen' && !own.InstrumentLabel) v.InstrumentLabel = sym;   // NinjaTrader's "ES" is per chart
  return v;
}
let gapDays = (() => {   // the gap history: not a NinjaTrader setting (it scans the chart's loaded days)
  let old = null;
  try { old = JSON.parse(local.get('gaps', '{}'))?.days; } catch { /* none */ }
  const n = Number(local.get('gapDays', old ?? 90));
  return n >= 1 && n <= 180 ? Math.round(n) : 90;
})();
function indSets() {
  S.tpoSet = indValues('tpo');
  S.gapSet = { ...indValues('gap'), days: gapDays };
  S.hemaSet = indValues('hema');
  S.ifSet = indValues('ifthen');
  S.llSet = indValues('lines');
  S.vpSet = Object.fromEntries(VP_KINDS.map((k) => [k, indValues(k)]));
  S.dsmaSet = indValues('dsma');
}
indSets();

// ---- Tatanka HTF EMA: OnBarUpdate runs here on the chart's bars (studies.js), the server supplies the
// higher-timeframe EMA and the chart EMA before the first bar --------------------------------------------
S.hema = null;
S.hemaVals = [];
const LW_DASH = { Solid: 0, Dot: 1, Dash: 2, DashDot: 3, DashDotDot: 4 };   // Lightweight's LineStyle
function hemaStyle() {
  const p = S.hemaSet.PlotHTFEMA || { color: '#808080', dash: 'Solid', width: 2 };
  return { color: p.color, lineWidth: Math.max(1, Math.round(p.width)), lineStyle: LW_DASH[p.dash] ?? 0 };
}
const hemaTitle = () => `HTF EMA(${S.hemaSet.HtfPeriod} ${S.hemaSet.HtfPeriodType},${S.hemaSet.EmaLength})`;
const hemaLine = chart.addSeries(L.LineSeries, { ...lineOpts, ...hemaStyle(), lastValueVisible: true, title: '' });
// Daily SMA x4 (below): four plots with price markers (PaintPriceMarkers), and its labels right of the last bar
S.dsma = null;
S.dsmaVals = [];
S.dsmaLive = new Map();   // bar time -> the values a live bar showed
S.dsmaPrep = null;
const dsmaLines = [0, 1, 2, 3].map(() => chart.addSeries(L.LineSeries, { ...lineOpts, lastValueVisible: true, title: '' }));

const timeOf = (b) => (isTime() ? b.t / 1000 : b.seq);
function vwapPoint(b, at, k) {  // k = 0 for the VWAP, +-n for the n-sigma bands
  const m = b.st?.[at], sd = b.st?.[at + 1];
  return m == null ? { time: timeOf(b) } : { time: timeOf(b), value: m + k * sd };
}
function setStudyData() {
  for (const w of VWAPS) {
    w.mid.setData(S.bars.map((b) => vwapPoint(b, w.at, 0)));
    for (const band of w.bands) {
      band.up.setData(S.bars.map((b) => vwapPoint(b, w.at, band.k)));
      band.dn.setData(S.bars.map((b) => vwapPoint(b, w.at, -band.k)));
    }
  }
  drawAvwap();
}
function updateStudyBar(b) {
  for (const w of VWAPS) {
    w.mid.update(vwapPoint(b, w.at, 0));
    for (const band of w.bands) { band.up.update(vwapPoint(b, w.at, band.k)); band.dn.update(vwapPoint(b, w.at, -band.k)); }
  }
}
function applyStudyVisibility() {
  for (const w of VWAPS) {
    const on = !!S.st[w.key];
    w.mid.applyOptions({ visible: on });
    for (const band of w.bands) {
      const v = on && !!S.st[`b${band.k}`];
      band.up.applyOptions({ visible: v });
      band.dn.applyOptions({ visible: v });
    }
  }
  avwap.applyOptions({ visible: anchorMs() != null });
  if (S.layersReady) applyLayers();   // which studies are on decides each one's pass
  const any = S.st.ve || S.st.vr || VP_KINDS.some((k) => S.st[k]) || S.st.right !== 'off' || S.st.amt || S.st.tpo || S.st.gap || S.st.hema || S.st.dsma || S.st.ifthen || S.st.fp !== 'off' || anchorMs() != null || S.indCfg.length > 0;
  $('#st-btn').classList.toggle('on', !!any);
}

const anchorKey = () => `anchor:${S.symbol}`;
function anchorMs() { const v = parseInt(local.get(anchorKey(), ''), 10); return Number.isFinite(v) ? v : null; }
function drawAvwap() {
  const a = anchorMs();
  // 2D and 3D bars hold a weekend or a holiday as often as not: each one runs to the next one's open (barEnd). An RTH
  // chart's bars end at the RTH close (barCloseMs), hours before a day or an hour is up: an anchor in the night is
  // the next day's.
  const period = !isTime() ? 0 : S.rth && !S.ext && inst().rth ? barCloseMs : daysSpec(S.spec) ? Infinity : specSeconds(S.spec) * 1000;
  avwap.setData(a == null ? [] : anchoredVwap(S.bars, a, timeOf, period));
  avwap.applyOptions({ visible: a != null });
  let info = 'Tap "Set anchor", then tap a bar.';
  if (a != null) info = `Anchored ${ax.full.format(a)} ${zs(a)}${S.bars.length && a < S.bars[0].t ? ' (before loaded bars: starts at the first bar)' : ''}`;
  $('#av-info').textContent = info;
}

// ---- standard indicators (runners in studies.js): averages on the price pane, the rest in panes below ---------
const IND_COLORS = { ema: ['#ffb74d', '#4fc3f7', '#aed581', '#f48fb1'], sma: ['#ce93d8', '#fff176', '#80cbc4', '#ff8a65'],
  wma: ['#80deea', '#ffcc80', '#c5e1a5'], hma: ['#f06292', '#9575cd', '#4db6ac'], vwma: ['#ffd54f', '#64b5f6'], dema: ['#a1887f', '#90caf9'],
  tema: ['#e57373', '#81c784'], smma: ['#ba68c8', '#4dd0e1'], boll: ['#ffb74d', '#4fc3f7'], kelt: ['#4dd0e1', '#ffb74d'], donch: ['#90caf9', '#ffb74d'],
  rsi: ['#b39ddb'], macd: ['#4fc3f7'], stoch: ['#4fc3f7'], stochrsi: ['#b39ddb'], willr: ['#4fc3f7'], cci: ['#ffb74d'], mom: ['#4fc3f7'],
  roc: ['#4fc3f7'], adx: ['#ffd54f'], atr: ['#ffb74d'], psar: ['#e0e0e0', '#ffb74d'], supertrend: ['#26a69a'], ichimoku: ['#2962ff'],
  obv: ['#4fc3f7'], mfi: ['#b39ddb'], volma: ['#ffb74d'], rvol: ['#90a4ae'], cvd: ['#4fc3f7'], pivots: ['#ffd54f', '#4fc3f7'],
  camarilla: ['#ffd54f', '#4fc3f7'], linreg: ['#4fc3f7', '#ffb74d'], cot: ['#ef5350'] };
const LEGACY_PLOTS = new Set(['ema', 'sma', 'rsi', 'macd', 'atr', 'cvd', 'cot']);   // drawn as before there were more
const COT_LINES = [['Commercials', '#ef5350'], ['Large specs', '#66bb6a'], ['Small traders', '#42a5f5']];
const IND_SIGNAL = '#ff9800';
function loadInds() {
  const a = ownTab() ? ownTab().own.inds : rawLocal('inds', []);
  return Array.isArray(a) ? a.map(indClean).filter(Boolean).slice(0, IND_MAX) : [];
}
S.indCfg = loadInds();
S.inds = [];          // per setting: {c, plots: [{s: series, pt}], run, vals, done, doneSeq, label}
S.indDirty = false;   // a closed bar changed: recompute from the first bar
S.hoverIdx = -1;      // bar under the crosshair, for the values in the legends
const valPt = (b, v) => (v == null || !Number.isFinite(v) ? { time: timeOf(b) } : { time: timeOf(b), value: v });
const indLineOpts = (color, width, priceFormat, extra = {}) => ({ color, lineWidth: width, priceFormat, priceLineVisible: false, crosshairMarkerVisible: false, ...extra });
function indFormat(c, v) {
  if (v == null || !Number.isFinite(v)) return '–';
  const f = IND_DEFS[c.type].fmt;
  if (typeof f === 'number') return v.toFixed(f);
  if (f === 'volume') return Math.round(v).toLocaleString('en-US');
  if (c.type === 'rsi') return v.toFixed(2);
  if (c.type === 'macd') return v.toFixed(inst().decimals + 2);
  if (c.type === 'cvd') return (v > 0 ? '+' : '') + Math.round(v);
  if (c.type === 'cot') return `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v) >= 1e6 ? `${(Math.abs(v) / 1e6).toFixed(2)}M` : `${(Math.abs(v) / 1e3).toFixed(1)}K`}`;
  return px(v);
}
const indColor = (pick, main) => ({ main, signal: IND_SIGNAL, up: C.up, down: C.down }[pick] || pick);
// An indicator from its plots (ta.js IND_DEFS): lines, dots, histograms; levels drawn as a segment a period
// (IndLevelsPrimitive); the Ichimoku cloud ahead of the bars (IchimokuPrimitive); a window drawn over its last bars.
function makePlots(c, color) {
  const def = IND_DEFS[c.type], pane = def.pane === true ? chart.panes().length : 0, add = (type, o) => chart.addSeries(type, o, pane);
  const dec = inst().decimals, scale = def.pane === 'vol' ? { priceScaleId: 'vol', lastValueVisible: false } : {};   // the volume's scale: no tag on the price's axis
  const pf = def.fmt === 'volume' ? { type: 'volume' } : typeof def.fmt === 'number' ? { type: 'price', precision: def.fmt, minMove: 10 ** -def.fmt }
    : { type: 'price', precision: dec, minMove: inst().tick_size };
  const range = def.range && (() => ({ priceRange: { minValue: def.range[0], maxValue: def.range[1] } }));
  const plots = def.plots.map((pl) => {
    const col = indColor(pl.color, color);
    if (pl.kind === 'hist') {
      return { s: add(L.HistogramSeries, { color: `${col}aa`, priceFormat: pf, priceLineVisible: false, lastValueVisible: false, ...scale }),
        pt: (b, o) => valPt(b, o[pl.i]), pl, color: col };
    }
    const o = indLineOpts(col, pl.width || 1, pf, { ...scale, ...(range ? { autoscaleInfoProvider: range } : {}),
      ...(pl.dash ? { lineStyle: L.LineStyle.Dashed } : {}),
      ...(pl.kind === 'dots' ? { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 1.5, lastValueVisible: false } : {}),
      ...(pl.kind === 'level' ? { lineVisible: false, lastValueVisible: false } : {}) });
    return { s: add(L.LineSeries, o), pl, color: col,
      pt: (b, v) => (pl.dir != null && v[1] !== pl.dir ? { time: timeOf(b) } : valPt(b, v[pl.i])) };
  });
  if (c.type === 'ichimoku') {   // the close, drawn displacement - 1 bars back
    plots.push({ s: add(L.LineSeries, indLineOpts('#43a047', 1, pf, { lastValueVisible: false })), pl: { i: -1 }, color: '#43a047', lagging: true, pt: (b) => ({ time: timeOf(b) }) });
  }
  for (const v of def.levels || []) plots[0].s.createPriceLine({ price: v, color: C.dim, lineWidth: 1, lineStyle: L.LineStyle.Dashed, axisLabelVisible: false });
  if (range) plots[0].s.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } });
  return plots;
}
function makeInd(c, color) {
  const def = IND_DEFS[c.type], pane = def.pane === true ? chart.panes().length : 0, add = (type, o) => chart.addSeries(type, o, pane);
  const dec = inst().decimals, fmt = { type: 'price', precision: dec, minMove: inst().tick_size };
  const fine = { type: 'price', precision: dec + 2, minMove: 10 ** -(dec + 2) };
  let plots;
  if (!LEGACY_PLOTS.has(c.type)) plots = makePlots(c, color);
  else if (c.type === 'ema' || c.type === 'sma') plots = [{ s: add(L.LineSeries, indLineOpts(color, 2, fmt)), pt: (b, o) => valPt(b, o[0]) }];
  else if (c.type === 'rsi') {
    const pf = { type: 'price', precision: 2, minMove: 0.01 }, range = () => ({ priceRange: { minValue: 0, maxValue: 100 } });
    plots = [{ s: add(L.LineSeries, indLineOpts(color, 2, pf, { autoscaleInfoProvider: range })), pt: (b, o) => valPt(b, o[0]) },
             { s: add(L.LineSeries, indLineOpts(IND_SIGNAL, 1, pf, { autoscaleInfoProvider: range, lastValueVisible: false })), pt: (b, o) => valPt(b, o[1]) }];
    plots[0].s.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } });
    for (const price of [70, 30]) plots[0].s.createPriceLine({ price, color: C.dim, lineWidth: 1, lineStyle: L.LineStyle.Dashed, axisLabelVisible: false });
  } else if (c.type === 'macd') {
    plots = [{ s: add(L.HistogramSeries, { priceFormat: fine, priceLineVisible: false, lastValueVisible: false }),
               pt: (b, o) => ({ time: timeOf(b), value: o[2], color: (o[2] >= 0 ? C.up : C.down) + '99' }) },
             { s: add(L.LineSeries, indLineOpts(color, 2, fine)), pt: (b, o) => valPt(b, o[0]) },
             { s: add(L.LineSeries, indLineOpts(IND_SIGNAL, 1, fine)), pt: (b, o) => valPt(b, o[1]) }];
  } else if (c.type === 'atr') plots = [{ s: add(L.LineSeries, indLineOpts(color, 2, fmt)), pt: (b, o) => valPt(b, o[0]) }];
  else if (c.type === 'cot') {   // net contracts of each group: commercials, large speculators, small traders
    plots = COT_LINES.map(([, col], k) => ({ s: add(L.LineSeries, indLineOpts(col, 2, { type: 'volume' }, { lineType: L.LineType.WithSteps })),
      pt: (b, o) => valPt(b, o[k]) }));
    plots[0].s.createPriceLine({ price: 0, color: C.dim, lineWidth: 1, lineStyle: L.LineStyle.Dotted, axisLabelVisible: false });
  }
  else {   // cumulative delta: each bar from the running total before it to the total after it
    plots = [{ s: add(L.CandlestickSeries, { upColor: C.up, downColor: C.down, borderVisible: false, wickVisible: false, priceLineVisible: false, priceFormat: { type: 'volume' } }),
               pt: (b, o) => ({ time: timeOf(b), open: o[0], high: Math.max(o[0], o[1]), low: Math.min(o[0], o[1]), close: o[1] }) }];
  }
  const ind = { c, color, plots, run: null, vals: [], done: 0, doneSeq: null, label: null, extra: null };
  if ((def.plots || []).some((pl) => pl.kind === 'level') && !LEGACY_PLOTS.has(c.type)) plots[0].s.attachPrimitive(ind.extra = new IndLevelsPrimitive(() => indLevelsState(ind)));
  if (c.type === 'ichimoku') plots[0].s.attachPrimitive(ind.extra = new IchimokuPrimitive(() => ichimokuState(ind)));
  if (def.pane === true) {
    plots[0].s.getPane().setStretchFactor(0.5);   // the price pane is 2: a third of its height each
    ind.label = new PaneLabelPrimitive(() => ({ parts: indParts(ind), x: labelX(plots[0].s) }));
    plots[0].s.attachPrimitive(ind.label);
  }
  return ind;
}
// A pane label steps right of the drawing toolbar where the toolbar reaches down into its pane.
function labelX(series) {
  const bar = $('#draw-bar');
  if (bar.hidden) return 6;
  const r = bar.getBoundingClientRect(), p = series.getPane().getHTMLElement()?.getBoundingClientRect();
  if (!p || r.bottom < p.top + 4 || r.top > p.top + 18 || r.left > p.left + 60) return 6;
  return Math.max(6, r.right - p.left + 6);
}
// Title and values at the crosshair (or the last bar), with each plot's color.
function indParts(ind) {
  const v = ind.vals[S.hoverIdx >= 0 && S.hoverIdx < ind.vals.length ? S.hoverIdx : ind.vals.length - 1];
  const parts = [[indTitle(ind.c), C.dim]];
  if (!v) return parts;
  if (ind.c.type === 'cot') {
    if (S.cot?.error) return [[`COT: ${S.cot.error}`, C.dim]];
    COT_LINES.forEach(([name, col], k) => parts.push([`${name} ${indFormat(ind.c, v[k])}`, col]));
  } else if (ind.c.type === 'cvd') parts.push([indFormat(ind.c, v[1]), v[1] >= v[0] ? C.up : C.down]);
  else if (ind.c.type === 'macd') parts.push([indFormat(ind.c, v[0]), ind.color], [indFormat(ind.c, v[1]), IND_SIGNAL], [indFormat(ind.c, v[2]), v[2] >= 0 ? C.up : C.down]);
  else if (ind.c.type === 'rsi') parts.push([indFormat(ind.c, v[0]), ind.color], [indFormat(ind.c, v[1]), IND_SIGNAL]);
  else if (!LEGACY_PLOTS.has(ind.c.type)) parts.push(...indValueParts(ind, v));
  else parts.push([indFormat(ind.c, v[0]), ind.color]);
  return parts;
}
// Each line's value in its plot's color (a line drawn by two plots, as SuperTrend's up and down, once: in the one drawn).
function indValueParts(ind, v) {
  const out = [], seen = new Set();
  for (const p of ind.plots) {
    const i = p.pl?.i;
    if (i == null || i < 0 || seen.has(i) || (p.pl.dir != null && v[1] !== p.pl.dir)) continue;
    seen.add(i);
    out.push([indFormat(ind.c, v[i]), p.color]);
  }
  return out;
}
function indFull(ind) {
  const run = indRunner(ind.c, isTime() && S.spec.endsWith('s')), n = S.bars.length, vals = new Array(n);
  for (let i = 0; i < n; i++) vals[i] = run.step(S.bars[i], i < n - 1);
  Object.assign(ind, { run, vals, done: Math.max(n - 1, 0), doneSeq: n > 1 ? S.bars[n - 2].seq : null });
  if (IND_DEFS[ind.c.type].window) indWindow(ind);
  else for (const p of ind.plots) p.s.setData(p.lagging ? indLagging(ind) : S.bars.map((b, i) => p.pt(b, vals[i])));
  ind.label?.update();
  ind.extra?.update();
}
// Ichimoku's lagging line: each bar's close drawn displacement - 1 bars back.
function indLagging(ind) {
  const n = S.bars.length, shift = Math.max(0, ind.c.disp - 1);
  return S.bars.map((b, i) => (i + shift < n ? { time: timeOf(b), value: S.bars[i + shift].c } : { time: timeOf(b) }));
}
// The regression channel: the line through the last bars as the last one has it (none before them).
function indWindow(ind) {
  const n = S.bars.length, o = ind.vals[n - 1];
  if (!o || o[0] == null) { for (const p of ind.plots) p.s.setData([]); return; }
  const [end, , , slope, band, m] = o;
  ind.plots.forEach((p, k) => p.s.setData(S.bars.slice(n - m).map((b, j) => {
    const mid = end - slope * (m - 1 - j);
    return { time: timeOf(b), value: k === 0 ? mid : k === 1 ? mid + band : mid - band };
  })));
}
// Levels (pivots): a segment for each run of bars with the same level, from half a bar before the first to half a bar
// after the last, and the latest one's name and price at its right end.
class IndLevelsPrimitive {
  constructor(getState) {
    this.getState = getState;
    this.p = null;
    this.views = [{ zOrder: () => 'normal', renderer: () => (this.p ? { draw: (t) => t.useMediaCoordinateSpace((sc) => this.draw(sc.context)) } : null) }];
  }
  draw(ctx) {
    const st = this.getState();
    if (!st || !st.vals.length) return;
    const ts = chart.timeScale(), r = ts.getVisibleLogicalRange(), n = st.vals.length;
    if (!r) return;
    const half = ts.options().barSpacing / 2, i0 = Math.max(0, Math.floor(r.from) - 1), i1 = Math.min(n - 1, Math.ceil(r.to) + 1);
    ctx.save();
    ctx.font = '10px system-ui, sans-serif';
    ctx.textBaseline = 'bottom';
    for (const lv of st.levels) {
      ctx.strokeStyle = ctx.fillStyle = lv.color;
      ctx.lineWidth = lv.width;
      let j = i0;
      while (j <= i1) {
        const v = st.vals[j]?.[lv.i];
        let k = j;
        while (k + 1 <= i1 && st.vals[k + 1]?.[lv.i] === v) k += 1;
        const y = v == null ? null : this.p.series.priceToCoordinate(v);
        if (y != null) {
          const x0 = ts.logicalToCoordinate(j) - half, x1 = ts.logicalToCoordinate(k) + half, yy = Math.round(y) + (lv.width % 2 ? 0.5 : 0);
          ctx.beginPath(); ctx.moveTo(x0, yy); ctx.lineTo(x1, yy); ctx.stroke();
          if (k === n - 1) ctx.fillText(`${lv.label} ${st.fmt(v)}`, x1 + 4, yy - 1);
        }
        j = k + 1;
      }
    }
    ctx.restore();
  }
  attached(p) { this.p = p; }
  detached() { this.p = null; }
  paneViews() { return this.views; }
  update() { if (this.p) this.p.requestUpdate(); }
}
function indLevelsState(ind) {
  const def = IND_DEFS[ind.c.type];
  return { vals: ind.vals, fmt: (v) => indFormat(ind.c, v),
    levels: ind.plots.filter((p) => p.pl?.kind === 'level').map((p) => ({ i: p.pl.i, color: p.color, width: p.pl.width || 1, label: def.lines[p.pl.i][1] })) };
}
// The Ichimoku cloud between Span A and Span B, displacement - 1 bars ahead of where they were made (past the last bar
// too), green where A is above, red below; and the spans' lines out there.
class IchimokuPrimitive extends IndLevelsPrimitive {
  draw(ctx) {
    const st = this.getState();
    if (!st || !st.vals.length) return;
    const ts = chart.timeScale(), r = ts.getVisibleLogicalRange(), n = st.vals.length, sh = st.shift;
    if (!r) return;
    const j0 = Math.max(0, Math.floor(r.from) - 1 - sh), j1 = Math.min(n - 1, Math.ceil(r.to) + 1 - sh);
    const Y = (v) => (v == null ? null : this.p.series.priceToCoordinate(v)), X = (j) => ts.logicalToCoordinate(j + sh);
    ctx.save();
    for (let j = j0; j < j1; j++) {
      const a0 = st.vals[j]?.[4], b0 = st.vals[j]?.[5], a1 = st.vals[j + 1]?.[4], b1 = st.vals[j + 1]?.[5];
      if (a0 == null || b0 == null || a1 == null || b1 == null) continue;
      const x0 = X(j), x1 = X(j + 1), ya0 = Y(a0), yb0 = Y(b0), ya1 = Y(a1), yb1 = Y(b1);
      if ([x0, x1, ya0, yb0, ya1, yb1].some((x) => x == null)) continue;
      ctx.fillStyle = a0 + a1 >= b0 + b1 ? 'rgba(67, 160, 71, 0.18)' : 'rgba(239, 83, 80, 0.18)';
      ctx.beginPath(); ctx.moveTo(x0, ya0); ctx.lineTo(x1, ya1); ctx.lineTo(x1, yb1); ctx.lineTo(x0, yb0); ctx.closePath(); ctx.fill();
      if (j + 1 + sh > n - 1) {   // past the last bar: the spans' own lines (the series stop at it)
        ctx.lineWidth = 1;
        for (const [ya, yb, c] of [[ya0, ya1, st.colorA], [yb0, yb1, st.colorB]]) { ctx.strokeStyle = c; ctx.beginPath(); ctx.moveTo(x0, ya); ctx.lineTo(x1, yb); ctx.stroke(); }
      }
    }
    ctx.restore();
  }
}
function ichimokuState(ind) {
  return { vals: ind.vals, shift: Math.max(0, ind.c.disp - 1), colorA: ind.plots[2]?.color, colorB: ind.plots[3]?.color };
}
// New and forming bars only: closed bars step the runner on, the forming bar is a step that is not kept.
function indLive(ind) {
  const n = S.bars.length;
  if (ind.done > n - 1 || (ind.done > 0 && S.bars[ind.done - 1].seq !== ind.doneSeq)) { indFull(ind); return; }
  const win = IND_DEFS[ind.c.type].window, closed = ind.done < n - 1;
  for (let i = ind.done; i < n; i++) {
    const v = ind.vals[i] = ind.run.step(S.bars[i], i < n - 1);
    if (!win) for (const p of ind.plots) if (!p.lagging) p.s.update(p.pt(S.bars[i], v));
  }
  ind.vals.length = n;
  if (win) indWindow(ind);
  for (const p of ind.plots) if (p.lagging && closed) p.s.setData(indLagging(ind));   // as bars close
  ind.extra?.update();
  ind.done = Math.max(n - 1, 0);
  ind.doneSeq = n > 1 ? S.bars[n - 2].seq : null;
  ind.label?.update();
}
function indsUpdate(full = false) {
  if (S.indDirty) { full = true; S.indDirty = false; }
  for (const ind of S.inds) {
    if (full || !ind.run) { indFull(ind); continue; }
    try { indLive(ind); } catch (e) { console.warn('indicator update', e); indFull(ind); }
  }
}
// Removes every indicator's series (their panes go with them) and builds them again from S.indCfg.
// ---- CFTC Commitments of Traders: weekly net positions by group, each week from when it was published -------------
S.cot = null;   // { symbol, rows: [{ t, comm, large, small, oi }], error }
function cotRunner() {
  const rows = S.cot?.symbol === S.symbol ? S.cot.rows || [] : [], span = isTime() ? specSeconds(S.spec) * 1000 : 0;
  let i = -1;
  return { step: (b) => {   // the last week published by the end of this bar
    while (i + 1 < rows.length && rows[i + 1].t < b.t + span) i += 1;
    const r = rows[i];
    return r ? [r.comm, r.large, r.small] : [null, null, null];
  } };
}
async function cotRequest() {
  const sym = S.symbol;
  if (!S.indCfg.some((c) => c.type === 'cot') || S.cot?.symbol === sym) return;
  S.cot = { symbol: sym, rows: [] };
  try {
    const d = await api('GET', `/api/cot?symbol=${encodeURIComponent(sym)}`);
    if (sym === S.symbol) S.cot = { symbol: sym, rows: d.rows, market: d.market };
  } catch (e) { if (sym === S.symbol) S.cot = { symbol: sym, rows: [], error: e.message }; }
  if (sym === S.symbol) indsUpdate(true);
}
function indsBuild() {
  cotRequest();
  S.indFmt = indFmt();
  for (const ind of S.inds) for (const p of ind.plots) chart.removeSeries(p.s);
  const seen = {};
  S.inds = S.indCfg.map((c) => {
    const k = seen[c.type] = (seen[c.type] ?? -1) + 1;
    const ind = makeInd(c, c.color || IND_COLORS[c.type][k % IND_COLORS[c.type].length]);
    if (c.hidden) ind.plots.forEach((p) => p.s.applyOptions({ visible: false }));   // the legend's eye
    return ind;
  });
  indsUpdate(true);
  priceOnlyLayout();   // new series: "Scale on price only" holds for them too
  if (S.layersReady) layerSeries();   // and back in their layers
}
function indsChanged() {
  if (!studyStore('inds', S.indCfg.map((c) => ({ ...c })))) local.set('inds', JSON.stringify(S.indCfg));
  cellPost('state');
  indsBuild();
  renderIndMenu();
  applyStudyVisibility();
  legend(S.lastBar);
}
function renderIndMenu() { stRender(); }   // each indicator's instances are in its Studies row
// The settings of every instance of one indicator type: its periods and color; right-click its name (a long press on
// a phone) to remove it.
function indEditors(type) {
  const el = (tag, props = {}) => Object.assign(document.createElement(tag), props);
  return S.indCfg.map((c, i) => [c, i]).filter(([c]) => c.type === type).map(([c, i]) => {
    const def = IND_DEFS[c.type], row = el('div', { className: 'row ind' });
    row.append(el('span', { className: 'ind-nm', textContent: indTitle(c), title: 'Right-click (a long press on a phone) to remove' }));
    onMenuPress(row, '.ind-nm', (_, x, y) => showMenu(x, y, [{ label: `Remove ${indTitle(c)}`, fn: () => {
      const j = S.indCfg.indexOf(c);
      if (j >= 0) { S.indCfg.splice(j, 1); indsChanged(); }
    } }]));
    for (const [k, label, , lo, hi, o] of def.params) {
      if (o?.choices) {   // Pivots' range: Daily, Weekly, Monthly
        const sel = el('select', { title: label });
        sel.setAttribute('aria-label', `${def.name} ${label}`);
        sel.append(...o.choices.map((x) => new Option(x, x)));
        sel.value = c[k];
        sel.onchange = () => { c[k] = sel.value; indsChanged(); };
        row.append(sel);
        continue;
      }
      const step = o?.step || 1, inp = el('input', { type: 'number', min: lo, max: hi, step, value: c[k], title: label });
      inp.setAttribute('aria-label', `${def.name} ${label}`);
      inp.onchange = () => {
        const v = Number((Math.round(Number(inp.value) / step) * step).toFixed(indDecimals(step)));
        if (inp.value === '' || !(v >= lo && v <= hi)) { inp.value = c[k]; toast(`${label}: ${lo} to ${hi}`); return; }
        c[k] = v;
        indsChanged();
      };
      row.append(inp);
    }
    if (c.type === 'cvd') {
      const sel = el('select', { title: 'Restart' });
      sel.setAttribute('aria-label', 'Cumulative delta restart');
      sel.append(el('option', { value: 'session', textContent: 'each session' }), el('option', { value: 'none', textContent: 'never' }));
      sel.value = c.reset;
      sel.onchange = () => { c.reset = sel.value; indsChanged(); };
      row.append(sel);
    } else {
      const col = el('input', { type: 'color', value: S.inds[i]?.color || '#ffffff', title: 'Color' });
      col.setAttribute('aria-label', `${def.name} color`);
      col.onchange = () => { c.color = col.value; indsChanged(); };
      row.append(col);
    }
    return row;
  });
}
function indAdd(type) {
  if (S.indCfg.length >= IND_MAX) { toast(`Up to ${IND_MAX} indicators`); return; }
  const c = indClean({ type });
  const taken = (len) => S.indCfg.some((x) => x.type === type && x.len === len);
  if (c.len != null && taken(c.len)) c.len = [9, 20, 50, 100, 200].find((n) => !taken(n)) ?? c.len;
  S.indCfg.push(c);
  indsChanged();
}

// ---- the visible-range or composite volume profile at the right edge ----------------------------------------------
const rangePrim = new ProfilePrimitive(() => ({ bars: S.bars, tick: inst().tick_size, sessions: [], range: S.st.right === 'off' ? null : S.rangeProfile }));
candles.attachPrimitive(rangePrim);

// ---- Tatanka volume profiles (AAVP, PVP, SVP): the NinjaTrader calculation cores run on the server (vp.py) over the
// stored 1-minute bars and trades, for every chart of the symbol; their OnRender is ported in studies.js -----------
S.vp = {};      // kind -> the server's answer (finished profiles kept from earlier answers)
S.vpErr = {};
const vpHeld = {};   // kind -> {sym, key, byStart}: the finished profiles held, named in the next request
const vpBusy = {};
const vpIntraday = () => /^\d+[smhtvrk]$/.test(S.spec);   // NinjaTrader's IsIntraday: seconds to hours, tick, volume, range, Renko
function vpBase(k) {
  if (!S.st[k] || S.ext || !indReady(k) || !S.bars.length) return null;
  const daily = S.spec === '1D';
  let error = S.vpErr[k] || S.vp[k]?.error || '';
  if (k === 'aavp' && !vpIntraday() && !daily) error = 'Use an intraday chart or a 1 Day chart. The anchor period can still be Year, Quarter, Month, Week, or Session.';
  if (k === 'pvp' && !vpIntraday()) error = 'Use an intraday chart (for example, 30 or 60 Minute). The profile Period can still be Daily, Weekly, Monthly, or Yearly.';
  if (k === 'prof' && !vpIntraday()) error = 'Use an intraday chart (for example, 30 Minute).';
  return { kind: k, d: S.vp[k]?.symbol === S.symbol ? S.vp[k] : null, set: S.vpSet[k], bars: S.bars, tick: inst().tick_size,
    periodMs: isTime() && !calSpec() ? specSeconds(S.spec) * 1000 : 0, daily, error, fmt: px };
}
// Texts stacked: the top messages below the legend, one NinjaTrader text box (46 px) apart; the bottom notices upward.
function vpState(k) {
  const st = vpBase(k);
  if (!st) return null;
  let msgY = Math.max(35, legendBox().bottom + 4), noticeY = 23;
  for (const j of VP_KINDS) {
    if (j === k) break;
    const o = vpBase(j);
    if (o && vpMessage(o)) msgY += 46;
    else if (o && vpNotice(o)) noticeY += 20;
  }
  const bar = $('#draw-bar'), r = bar && !bar.hidden ? bar.getBoundingClientRect() : null;   // the drawing tools cover the left edge
  return { ...st, msgY, noticeY, textX: r && r.width ? Math.max(12, r.right - $('#chart-wrap').getBoundingClientRect().left + 8) : 12 };
}
const vpPrims = Object.fromEntries(VP_KINDS.map((k) => [k, new VolumeProfilePrimitive(() => vpState(k))]));
Object.values(vpPrims).forEach((p) => candles.attachPrimitive(p));
const vpList = (k) => (k === 'pvp' ? 'profiles' : k === 'prof' ? 'sessions' : null);
// Profiles the server sent as "same" are the ones held; finished ones are held for the next request.
function vpMerge(k, d, sym) {
  const list = vpList(k);
  if (!list) return d;
  const h = vpHeld[k], held = h && h.sym === sym && h.key === d.key ? h.byStart : new Map(), keep = new Map();
  d[list] = d[list].map((p) => (p.same ? held.get(p.start) && { ...held.get(p.start), ...(p.touch ? { touch: p.touch } : {}) } : p)).filter(Boolean);
  for (const p of d[list]) if (k === 'pvp' ? p.closed : p.done) keep.set(p.start, p);
  vpHeld[k] = { sym, key: d.key, byStart: keep };
  return d;
}
// One request per kind at a time; the server keeps what it built, so asking again is cheap.
async function vpRequest(k, reset) {
  if (reset) { S.vp[k] = null; S.vpErr[k] = ''; delete vpHeld[k]; }
  if (!S.st[k] || S.ext || S.replay || !indReady(k) || !S.instruments[S.symbol]) { S.vp[k] = null; vpPrims[k].update(); renderVpInfo(k); return; }
  if (vpBusy[k]) { vpBusy[k] = 'again'; return; }
  vpBusy[k] = true;
  const sym = S.symbol, h = vpHeld[k], q = new URLSearchParams({ symbol: sym, s: JSON.stringify(S.vpSet[k]) });
  if (h && h.sym === sym && h.byStart.size) { q.set('key', h.key); q.set('have', [...h.byStart.keys()].join(',')); }
  try {
    const d = await api('GET', `/api/vp/${k}?${q}`);
    if (sym === S.symbol && S.st[k]) { S.vp[k] = vpMerge(k, d, sym); S.vpErr[k] = ''; }
  } catch (e) {
    if (sym === S.symbol) S.vpErr[k] = e.message;
  } finally {
    const again = vpBusy[k] === 'again';
    vpBusy[k] = false;
    if (again) vpRequest(k);
  }
  vpPrims[k].update();
  renderVpInfo(k);
  legend(S.lastBar);
}
function vpAll(reset) { for (const k of VP_KINDS) vpRequest(k, reset); }
setInterval(() => {   // as the indicators publish OnEachTick: the forming minute and the latest trades
  if (document.visibilityState === 'visible') for (const k of VP_KINDS) if (S.st[k] && !S.ext && !S.replay) vpRequest(k);
}, 3000);
function renderVpInfo(k) {
  const el = $(`#${k}-info`), d = S.vp[k], o = S.vpSet[k];
  if (!el) return;
  if (!S.st[k]) { el.textContent = ''; return; }
  const st = vpBase(k);
  if (st?.error) { el.textContent = st.error; return; }
  if (!d) { el.textContent = 'Loading…'; return; }
  if (k === 'aavp') {
    const p = d.profile;
    el.textContent = !p ? 'No 1-minute bars stored for this symbol yet.'
      : `${o.AnchorPeriod} from ${p.window[0]}${p.first ? ' (stored bars start later)' : ''}: ${p.rows.length} rows of ${p.tpr} tick${p.tpr > 1 ? 's' : ''}.`;
  } else if (k === 'pvp') {
    const p = d.profiles.at(-1);
    el.textContent = !p ? 'No 1-minute bars stored for this symbol yet.'
      : `${d.profiles.length} ${o.Period.toLowerCase()} profile${d.profiles.length > 1 ? 's' : ''}; the latest from ${p.window[0]}, ${p.rows.length} rows of ${p.tpr} tick${p.tpr > 1 ? 's' : ''}.`;
  } else {
    const n = d.sessions.filter((p) => p.approx).length;
    el.textContent = `${d.sessions.length} session${d.sessions.length === 1 ? '' : 's'} ${o.SessionStart}-${o.SessionEnd} ${['ExchangeTemplate', 'Chart', 'Central'].includes(o.SessionTimeZone) ? 'CT' : o.SessionTimeZone}, from every trade stored here`
      + (n ? `; ${n} with minutes from 1-minute bars (no trades stored then: keep the NinjaTrader add-on connected for every trade).` : '.');
  }
}

// ---- Tatanka If/Then: the pasted block lives on the server per symbol; parsing, the active band and drawing
// run here on every tick (studies.js) ------------------------------------------------------------------------
S.ifData = null;
S.ifRows = [];
const hexA = (h) => [...hexRgb(h), 1];
function ifPrice(b, src) {
  switch (src) {
    case 'Open': return b.o;
    case 'High': return b.h;
    case 'Low': return b.l;
    case 'HL2': return (b.h + b.l) / 2.0;
    case 'HLC3': return (b.h + b.l + b.c) / 3.0;
    case 'OHLC4': return (b.o + b.h + b.l + b.c) / 4.0;
    case 'HLCC4': return (b.h + b.l + b.c + b.c) / 4.0;
    default: return b.c;
  }
}
function ifFrame() {   // no rows: NinjaTrader still draws the table's header row
  if (!S.bars.length) return null;
  const price = ifPrice(S.bars[S.bars.length - 1], S.ifSet.PriceSource);
  return { rows: S.ifRows, price, atr: S.ifData?.atr ?? NaN, active: biasActiveRow(S.ifRows, price) };
}
const ifPrim = new IfThenPrimitive(() => {
  if (!S.st.ifthen || S.ext || !indReady('ifthen')) return null;
  const frame = ifFrame(), o = S.ifSet;
  if (!frame) return null;
  const style = {
    ...o, fmt: bondTick(inst().symbol) ? px : null, Above: hexA(o.AboveBrush), Below: hexA(o.BelowBrush),
    Within: hexA(o.WithinBrush), Background: hexA(o.BackgroundBrush), Border: hexA(o.BorderBrush),
  };
  // Labels sit at the chart's right edge (LabelOffset is the room kept for them right of the last candle).
  return {
    frame, style, full: ifFull(),
    legend: o.ShowTable ? legendBox() : null, avoidTop: o.ShowTable ? amtDashBottom() : 0,
  };
});
candles.attachPrimitive(ifPrim);
// On a phone-width chart the labels are cut to one line; a tap on one (or the table), or Studies > If/Then > Full text,
// shows every word (this device).
const ifFull = () => local.get('ifFull', '0') === '1';
function setIfFull(on) {
  local.set('ifFull', on ? '1' : '0');
  $('#if-full').checked = on;
  ifPrim.update();
}
$('#if-full').checked = ifFull();
$('#if-full').onchange = (e) => setIfFull(e.target.checked);

// ---- Tatanka Gap Analysis: the GapEngine runs on the server over the chart's bars, the live bar fills here ----
const GAP_SERVER = ['MinimumGapPoints', 'days'];
S.gaps = null;
S.gapError = '';
S.gapAlerts = {};
// Its alert switches are the symbol's, the same on every chart of it, as the server's gap alerts for the symbol have them
// (their new and fill), not a chart's own: another symbol's charts keep theirs.
const GAP_SWITCH = { AlertOnNewGap: 'new', AlertOnGapFill: 'fill' };
const GAP_SHARED = new Set(Object.keys(GAP_SWITCH));
S.gapSwitches = null;   // symbol -> { new, fill }, from the server
S.gapNotice = [];       // those its first start turned off (from each symbol's alerts as saved before): said once
const gapShown = (sym = S.symbol) => Object.fromEntries(Object.entries(GAP_SWITCH).map(([k, n]) => [k, S.gapSwitches?.[sym]?.[n] ?? true]));
const gapSpecOk = () => /^\d+[mh]$/.test(S.spec);
// Where the symbol/OHLC legend ends, in chart pixels: tables drawn at the top move below it if they would overlap.
// Measured once per frame (every drawn study asks; each measure can force a layout), then forgotten.
let legendSeen = null, dashSeen = null;
const forgetMeasures = () => { legendSeen = dashSeen = null; };
function legendBox() {
  if (!legendSeen) { legendSeen = measureLegend(); queueMicrotask(forgetMeasures); }
  return legendSeen;
}
function measureLegend(above = Infinity) {   // above: only the lines starting higher than this (px from the chart's top)
  const wrap = $('#chart-wrap').getBoundingClientRect();
  let right = 0, bottom = 0;
  for (const el of [$('#legend'), $('#dwin'), $('#legend-st'), $('#lv-meta')]) {
    if (!el.textContent || el.hidden) continue;
    const r = document.createRange();
    r.selectNodeContents(el);
    for (const q of r.getClientRects()) {
      if (q.top - wrap.top >= above) continue;
      right = Math.max(right, q.right - wrap.left); bottom = Math.max(bottom, q.bottom - wrap.top);
    }
  }
  return { right, bottom };
}
function amtDashBottom() {
  if (dashSeen == null) {
    const el = $('#amt-dash');
    dashSeen = el && !el.hidden ? el.offsetTop + el.offsetHeight : 0;
    queueMicrotask(forgetMeasures);
  }
  return dashSeen;
}
const gapPrim = new GapPrimitive(() => {
  if (!S.st.gap || S.ext || !indReady('gap') || !S.gapSet.EnabledAnalysis) return null;
  const error = !gapSpecOk() ? 'Use a minute or hour chart. Session boundaries require time-based bars.' : S.gapError;
  return {
    gaps: isTime() ? S.gaps : null, error, bars: S.bars, periodMs: specSeconds(S.spec) * 1000, set: S.gapSet,
    fmt: (p) => px(Math.round(p / inst().tick_size) * inst().tick_size), avoidTop: amtDashBottom(), legend: legendBox(),
  };
});
candles.attachPrimitive(gapPrim);

// ---- Tatanka TPO Profile: computed on the server from the chart's bars by the ported code, drawn here ----
const TPO_DRAWING = ['BoxTransparency', 'StartColor', 'EndColor', 'TextColor', 'PocColor', 'PolyColor', 'VaColor'];
S.tpo = null;
S.tpoError = '';
const tpoPrim = new TpoPrimitive(() => ({
  tpo: S.st.tpo && isTime() && indReady('tpo') ? S.tpo : null, bars: S.bars, periodMs: specSeconds(S.spec) * 1000, set: S.tpoSet,
}));
candles.attachPrimitive(tpoPrim);

// ---- Tatanka AMT Toolkit: computed on the server by the ported engine, drawn here ----------------
S.amt = null;
S.amtServer = { defaults: {}, overrides: {}, symbols: {}, alerts: {} };
const specSeconds = (spec) => { const n = parseInt(spec, 10), u = spec.slice(-1); return u === 's' ? n : u === 'm' ? n * 60 : u === 'h' ? n * 3600 : ({ D: 1, W: 7, M: 31 }[u] || 0) * 86400 * n; };
// The Toolkit runs on 1-30 minute bars: minute charts use their size, longer ones 30, tick/range/volume 5.
function amtMinutes() { const u = S.spec.slice(-1); return u === 's' ? 1 : u === 'm' ? Math.max(1, Math.min(30, parseInt(S.spec, 10))) : 'hDWM'.includes(u) ? 30 : 5; }
const amtPrim = new AmtPrimitive(() => ({
  amt: S.st.amt ? S.amt : null, bars: S.bars, periodMs: isTime() ? specSeconds(S.spec) * 1000 : 0,
  tick: inst().tick_size, font: css('--font'), fmt: (p) => px(Math.round(p / inst().tick_size) * inst().tick_size),
}));
candles.attachPrimitive(amtPrim);

// The Toolkit draws labels and the developing profile up to 50 bars right of the last bar, as on a
// NinjaTrader chart with a right margin; give the chart that room while it's on (and the If/Then and Daily SMA labels
// theirs).
function amtLayout() {
  let ro = Math.max(S.cs.rightBars, S.st.amt && !S.ext ? (window.innerWidth < 760 ? 25 : 55) : 0);   // never less than the setting
  if (S.st.ifthen && S.ifSet.LineLabels && S.ifRows.length) ro = Math.max(ro, S.ifSet.LabelOffset + 2);   // If/Then labels
  if (S.st.dsma && S.dsmaSet.ShowLabels) {   // Daily SMA x4's: LabelOffset bars on, then about "200 SMA - 6897.25"
    const fs = { Tiny: 10, Small: 12, Large: 18 }[S.dsmaSet.LabelSize] || 14;
    ro = Math.max(ro, S.dsmaSet.LabelOffset + Math.ceil((fs * 9) / chart.timeScale().options().barSpacing));
  }
  // Setting the margin scrolls the chart to the latest bars: only when it changes, and a chart scrolled back stays put.
  const ts = chart.timeScale();
  if (ts.options().rightOffset === ro) return;
  const lr = ts.getVisibleLogicalRange(), back = lr && S.bars.length && lr.to < S.bars.length - 1;
  ts.applyOptions({ rightOffset: ro });
  if (back) ts.setVisibleLogicalRange(lr);
}
// The chart's Toolkit settings: the server's starting ones with the open tab's on top (tabSet). The alert settings
// are the server's for the chart's symbol, the same on every chart of it (one runner per symbol alerts), and the levels
// file the server's on every chart (the levels are the symbol's): never a tab's own.
const amtShared = () => new Set(S.amtServer.shared || []);
const amtBySymbol = () => new Set(S.amtServer.per_symbol || []);
const pickSet = (o, keys, keep) => Object.fromEntries(Object.entries(o || {}).filter(([k]) => keys.has(k) === keep));
const amtOwn = () => pickSet(tabSet().amt, amtShared(), false);
// What a chart of the symbol starts from: every chart's starting settings with the symbol's alert settings (a symbol
// with none: NinjaTrader's).
const amtStart = (sym = S.symbol) => ({ ...(S.amtServer.overrides || {}), ...(S.amtServer.symbols?.[sym] || {}) });
const amtEff = () => ({ ...amtStart(), ...amtOwn() });
// What the server runs this chart's Toolkit on: '' the symbol's starting settings (no set sent: whatever the server's are now), else
// the chart's own; '?' its own, asked for before the server's settings had come (asked again when they do).
const amtKey = () => (Object.keys(amtOwn()).length ? JSON.stringify(amtEff()) : '');
let amtSent = '';
function amtRequest() {
  S.amt = null;
  amtSent = S.amtServer.schema ? amtKey() : Object.keys(amtOwn()).length ? '?' : '';
  const own = amtSent && amtSent !== '?' ? { set: amtEff() } : {};
  if (S.st.amt && S.subId && !S.ext) send({ op: 'amt', id: S.subId, symbol: S.symbol, minutes: amtMinutes(), ...own });
  renderAmtDash();
  amtPrim.update();
}
function amtFollow() { if (S.st.amt && amtKey() !== amtSent) amtRequest(); }   // its settings changed
function amtMerge(m) {
  const d = m.data, keep = S.amt;
  if (m.full || !keep) S.amt = { ...d, past: d.past || [], history: d.history || [], samples: d.samples || [], vwap: d.vwap || [] };
  else {   // only what changed is sent; keep the rest
    S.amt = { ...keep, ...d };
    for (const k of ['style', 'past', 'history', 'samples', 'vwap']) if (!d[k]) S.amt[k] = keep[k];
    if (d.vwap_add) {   // new RTH VWAP points; 20 days kept, as on the server
      const v = (keep.vwap || []).concat(d.vwap_add), cut = v.at(-1)[0] - 20 * 86400e3;
      S.amt.vwap = v[0][0] < cut ? v.filter((x) => x[0] >= cut) : v;
    }
  }
  renderAmtDash();
  amtPrim.update();
}
function renderAmtDash() {
  const el = $('#amt-dash'), warn = $('#amt-warn'), d = S.amt;
  const ready = S.st.amt && !S.ext && d && d.ready;
  warn.hidden = !S.st.amt || !!S.ext;
  warn.textContent = !S.st.amt ? '' : !d ? 'Tatanka AMT v1.1.0 — loading chart and secondary history…'
    : !d.ready ? 'Tatanka AMT v1.1.0 — waiting for bars' : [amtTzWarning(), d.warning].filter(Boolean).join(' ');
  el.hidden = !(ready && S.st.amtDash && d.style?.tbl_show !== false);
  if (!el.hidden) {
    // Folding to SESSION and DAY TYPE is this app's (phones start folded); NinjaTrader always shows every row.
    const phone = window.innerWidth < 760, folded = (local.get('amtCollapsed', '') || (phone ? '1' : '0')) === '1';
    el.classList.toggle('collapsed', folded);
    el.querySelector('.rows').replaceChildren(...d.dash.labels.slice(0, folded ? (phone ? 0 : 2) : undefined).map((label, i) => {
      const r = document.createElement('div'), k = document.createElement('span'), v = document.createElement('span');
      r.className = 'r';
      k.className = 'k';
      v.className = 'v';
      k.textContent = label;
      v.textContent = d.dash.values[i];
      v.style.color = argb(d.dash.colors[i]);
      r.append(k, v);
      return r;
    }));
  }
  placeAmtDash();
}
// Where NinjaTrader's Dashboard() and notices go, in the price pane: FontSize(tbl_size), rows fs + 10 high, the table
// max(310, 25 fs) wide, 10px from the side, 10px from the top, centred, or 55px up from the bottom; the notices 64px
// up from the bottom, 10px in. A table wider than the pane stays on screen. Folded on a phone it is only its title,
// a tab in the top corner beside the legend's first line instead of a bar across the chart.
const AMT_FS = { tiny: 10, small: 12, large: 18, huge: 24 };
let amtDashAt = '', amtMeasure = null;
function placeAmtDash() {
  const el = $('#amt-dash'), warn = $('#amt-warn'), d = S.amt;
  const left = chart.priceScale('left').width(), W = chart.timeScale().width(), H = chart.paneSize(0).height;
  let at = `${left} ${W} ${H} ${warn.hidden}`;
  if (!el.hidden && d) {
    const narrow = W < 520;   // a phone: smaller, and below the legend's two lines
    const pill = narrow && !el.querySelector('.rows').children.length;
    const fs = narrow ? 11 : AMT_FS[String(d.style?.tbl_size || '').toLowerCase()] || 14, rh = fs + 10;
    if (pill && !amtMeasure) amtMeasure = document.createElement('canvas').getContext('2d');
    if (pill) amtMeasure.font = `${fs}px ${getComputedStyle(el).fontFamily}`;
    let w = pill ? Math.ceil(amtMeasure.measureText(`${el.querySelector('.t').textContent} ▾`).width) + 18
      : narrow ? Math.min(W - 20, 220) : Math.max(310, fs * 25);
    const h = rh * (el.querySelector('.rows').children.length + 1), pos = d.style?.tbl_pos || 'Top Right';
    let below = narrow ? $('#overlay').offsetHeight + 10 : 10;   // a phone: under the legend, not over it
    if (narrow && !pos.includes('Left')) {   // in the top corner beside the legend when there is room; open, the table
      // drops down from the same corner (beside the legend's first line for the tab, all its lines for the table),
      // a little narrower if it must
      const right = pill ? measureLegend(4 + rh + 2).right : legendBox().right;   // the tab: the lines beside it
      const room = W - 6 - Math.ceil(right) - 6;
      if (room >= (pill ? w : 200)) { below = 4; w = Math.min(w, room); }
    }
    const x = Math.max(left, left + (pos.includes('Left') ? 10 : W - w - (narrow ? 6 : 10)));
    const y = pos.includes('Bottom') ? H - h - 55 : pos.includes('Middle') ? (H - h) / 2 : below;
    el.classList.toggle('pill', pill);
    at += ` ${x} ${y} ${w} ${h} ${fs}`;
    if (at !== amtDashAt) {
      Object.assign(el.style, { left: `${x}px`, top: `${y}px`, width: `${w}px`, height: `${h}px` });
      el.style.setProperty('--fs', `${fs}px`);
      el.style.setProperty('--rh', `${rh}px`);
    }
  }
  if (at === amtDashAt) return;
  amtDashAt = at;
  Object.assign(warn.style, { left: `${left + 10}px`, top: `${H - 64}px`, width: `${Math.max(0, W - 20)}px`, maxHeight: '64px' });
}
$('#amt-dash').onclick = () => {
  const c = !$('#amt-dash').classList.contains('collapsed');
  local.set('amtCollapsed', c ? '1' : '0');
  lookStore('amt', c ? '1' : '0');
  renderAmtDash();
};
let amtHeard = 0;   // 'amt_settings' broadcasts heard: a GET out meanwhile may answer with what they changed, from before
async function loadAmtServer() {
  const n = amtHeard;
  let got = null;
  try { got = await api('GET', '/api/amt'); } catch { /* keep defaults */ }
  if (n !== amtHeard) return loadAmtServer();   // one came meanwhile: asked again, after it
  if (got) S.amtServer = got;
  renderAmtMenu();
  amtFollow();   // a chart with its own settings, asked for before these came
}
function renderAmtMenu() {
  const period = amtValue('mvp_tf_mode') || 'Weekly';
  document.querySelectorAll('#amt-period button').forEach((b) => b.classList.toggle('on', b.dataset.v === period));
  const a = S.amtServer.alerts?.[S.symbol], set = amtValue('LevelAlertMode') || 'Cross';
  $('#amt-alerts').checked = !!a?.on;
  $('#amt-info').textContent = S.st.amt ? `Running on ${amtMinutes()}-minute bars. ${a?.on ? `Alerts: ${set}.` : ''}` : '';
  const def = S.amtServer.defaults || {}, n = Object.entries(amtEff()).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(def[k])).length;
  $('#amt-set-count').textContent = n ? `${n} changed from NinjaTrader's defaults` : "NinjaTrader's defaults";
}
// tzguard: the fields the Toolkit checks (ValidateSettings); any changed from its default puts the warning on the chart.
const AMT_TZ_FIELDS = ['mvp_sess_time', 'mvp_alert_time', 'ib_session', 'ib_alert_time', 'ib_stop_time', 'amt_sess', 'amt_del_sess',
  'pd_sess_time', 'cc_sess_def', 'sp_rth_session', 'spikeTimeRange', 'context_session', 'tbl_session', 'vwap_start_h',
  'vwap_start_m', 'vwap_end_h', 'vwap_end_m', 'inv_start_h', 'inv_end_h', 'inv_end_m'];
function amtTzWarning() {
  if (amtValue('tzguard_enable') === false) return '';
  const o = amtEff(), def = S.amtServer.defaults || {};
  return AMT_TZ_FIELDS.some((k) => k in o && o[k] !== def[k])
    ? 'Session fields differ from defaults. All session fields are Central exchange time.' : '';
}
const amtValue = (k) => { const o = amtEff(); return (k in o ? o : S.amtServer.defaults || {})[k]; };
document.querySelectorAll('#amt-period button').forEach((b) => {
  b.onclick = () => {   // this chart's, as the dialog's Profile Timeframe
    const own = { ...amtOwn() }, start = { ...(S.amtServer.defaults || {}), ...amtStart() };
    if (b.dataset.v === start.mvp_tf_mode) delete own.mvp_tf_mode; else own.mvp_tf_mode = b.dataset.v;
    tabSetSave('amt', own);
  };
});
// Saves changes to the server's AMT settings (only what changed is sent, over the server's as they are then): the alert
// settings the symbol's, for every chart of it (another symbol's charts keep theirs); the rest every chart's (the levels
// file; on Apply to all charts every setting shown). A chart of no instrument (no AMT alerts) has no alert settings.
async function amtPatch(d, sym = S.symbol) {
  const own = S.instruments[sym];
  if (!own && Object.keys(pickSet(d, amtBySymbol(), true)).length) {
    throw new Error(`The AMT alert settings are each instrument's and ${sym} is not one: change them on an instrument's chart`);
  }
  await api('PATCH', own ? `/api/amt/settings?symbol=${encodeURIComponent(sym)}` : '/api/amt/settings', d);
}
$('#amt-alerts').onchange = async (e) => {
  const on = e.target.checked;
  try {
    await api('PUT', `/api/amt/alerts/${S.symbol}`, { on });
    toast(on ? `AMT alerts on for ${S.symbol}` : `AMT alerts off for ${S.symbol}`);
    await loadAmtServer();
  } catch (err) { e.target.checked = !on; fail(err); }
};

// ---- Tatanka indicator settings: load, save, apply ------------------------------------------------------------------
const indCache = () => local.set('indCache', JSON.stringify(S.ind));
// Settings broadcast while loadInd's answer was on its way: the server may have read them before, so they go over it.
let indHeard = null;
async function loadInd() {
  const heard = indHeard = [];
  try { S.ind = await api('GET', '/api/indicators'); } catch { return; } finally { if (indHeard === heard) indHeard = null; }
  heard.forEach(indSaved);
  indCache();
  indApply();
  indDlg.refresh();
  renderPlanLines(false);
}
function indSymbolSaved(key, sym, values) {
  const all = (S.ind.symbols[key] ??= {});
  if (Object.keys(values || {}).length) all[sym] = values; else delete all[sym];
}
function indSaved(m) {   // an 'indicators' broadcast: a key's settings, or a symbol's own values
  if (m.symbol) indSymbolSaved(m.key, m.symbol, m.values); else S.ind.overrides[m.key] = m.overrides;
}
async function indPut(key, overrides) {
  S.ind.overrides[key] = await api('PUT', `/api/indicators/${key}`, overrides);
  indCache();
  indApply();
}
async function indPutSymbol(key, values, sym = S.symbol) {
  indSymbolSaved(key, sym, await api('PUT', `/api/indicators/${key}/${sym}`, values));
  if ('RowsBlock' in values && sym === S.symbol) {
    S.ifData = { ...(S.ifData || {}), rows: values.RowsBlock, updated: Date.now() };
    S.ifRows = biasParse(values.RowsBlock);
    renderPlanIf(true);
  }
  indCache();
  indApply();
}
// New settings: recompute, then ask the server again only where an engine input changed (else just redraw).
function indApply() {
  const was = { tpo: S.tpoSet, gap: S.gapSet, hema: S.hemaSet, ifthen: S.ifSet, vp: S.vpSet };
  indSets();
  const now = { tpo: S.tpoSet, gap: S.gapSet, hema: S.hemaSet, ifthen: S.ifSet };
  const moved = (k, names) => names.some((n) => JSON.stringify(was[k]?.[n]) !== JSON.stringify(now[k][n]));
  if (moved('tpo', Object.keys(now.tpo).filter((n) => !TPO_DRAWING.includes(n)))) tpoRequest(); else { tpoPrim.update(); renderTpoInfo(); }
  if (moved('gap', ['RthStart', 'RthEnd', 'MinimumGapPoints', 'EnabledAnalysis'])) gapRequest(); else { gapPrim.update(); renderGapInfo(); }
  if (moved('hema', ['HtfPeriodType', 'HtfPeriod', 'EmaLength', 'Mode'])) { S.hema = null; hemaRequest(); } else hemaDraw();
  // none yet (the settings came after the chart, or it failed) or a longer one: ask; else just draw
  if (S.st.dsma && (!S.dsma || Math.max(...dsmaPeriods()) > S.dsma.length)) dsmaRequest(); else dsmaDraw();
  if (moved('ifthen', ['AtrLength'])) ifRequest();
  for (const k of VP_KINDS) { if (JSON.stringify(was.vp?.[k]) !== JSON.stringify(S.vpSet[k])) vpRequest(k); vpPrims[k].update(); renderVpInfo(k); }
  amtLayout();
  ifPrim.update();
  renderIfInfo();
  llPrim.update();
  renderLlInfo();
  legend(S.lastBar);
}
function renderTatankaMenu() {
  VP_KINDS.forEach(renderVpInfo);
  renderAmtMenu();
  renderTpoInfo();
  renderGapMenu();
  renderHemaInfo();
  renderDsmaInfo();
  renderIfInfo();
  renderLlInfo();
  VP_KINDS.forEach(renderVpInfo);
}

// ---- TPO Profile ------------------------------------------------------------------------------------------------
const tpoSpecOk = () => /^\d+[mh]$/.test(S.spec);
let tpoBusy = false, tpoAgain = false;
// Asks again whenever a bar closes; one request at a time, the latest settings win. On an RTH chart the server builds
// it from the regular-hours bars (rth; left out on all hours, as an older server takes the request), and an answer
// asked for the other hours is not this chart's. Gap Analysis and HTF EMA ask the same way.
async function tpoRequest(reset) {
  if (reset) { S.tpo = null; S.tpoError = ''; }
  if (!S.st.tpo || !tpoSpecOk() || !S.instruments[S.symbol] || !indReady('tpo')) { S.tpo = null; tpoPrim.update(); renderTpoInfo(); return; }
  if (tpoBusy) { tpoAgain = true; return; }
  tpoBusy = true;
  const key = `${S.symbol} ${S.spec} ${S.rth}`;
  const q = new URLSearchParams({ symbol: S.symbol, spec: S.spec });
  if (S.rth) q.set('rth', 'true');
  for (const [k, v] of Object.entries(S.tpoSet)) if (!TPO_DRAWING.includes(k)) q.set(k, String(v));
  try {
    const d = await api('GET', `/api/tpo?${q}`);
    if (key === `${S.symbol} ${S.spec} ${S.rth}` && S.st.tpo) { S.tpo = d; S.tpoError = ''; }
  } catch (e) {
    S.tpo = null;
    S.tpoError = e.message;
  } finally {
    tpoBusy = false;
  }
  tpoPrim.update();
  renderTpoInfo();
  if (tpoAgain) { tpoAgain = false; tpoRequest(); }
}
function renderTpoInfo() {
  const el = $('#tpo-info');
  if (!S.st.tpo) { el.textContent = ''; return; }
  if (!tpoSpecOk()) { el.textContent = 'Needs a minute or hour chart.'; return; }
  if (S.tpoError) { el.textContent = S.tpoError; return; }
  const d = S.tpo, t = inst().tick_size;
  el.textContent = !d ? 'Loading…' : d.step ? `Rows ${px(d.step)} (${Math.round(d.step / t)} ticks), ${d.minutes}-minute bars, ${ownHours(S.tpoSet.SessionTime)} CT.` : 'Waiting for bars.';
}

// ---- Gap Analysis: RTH start and end are NinjaTrader's HHmmss numbers (83000 = 08:30:00 CT) ----------------------------
const hhmm = (v) => String(Math.floor(v / 100)).padStart(4, '0');
const gapRth = () => `${hhmm(S.gapSet.RthStart)}-${hhmm(S.gapSet.RthEnd)}`;
function gapRthOk(g = S.gapSet) {
  const ok = (v) => Number.isInteger(v) && v >= 0 && Math.floor(v / 10000) < 24 && Math.floor(v / 100) % 100 < 60 && v % 100 < 60;
  return ok(g.RthStart) && ok(g.RthEnd) && g.RthStart !== g.RthEnd;
}
const GAP_RTH_BAD = 'Enter different, valid RTH start/end times in HHmmss format.';
let gapBusy = false, gapAgain = false;
async function gapRequest(reset) {
  if (reset) { S.gaps = null; S.gapError = ''; }
  if (!S.st.gap || !gapSpecOk() || !S.instruments[S.symbol] || !indReady('gap') || !S.gapSet.EnabledAnalysis) { S.gaps = null; gapPrim.update(); renderGapInfo(); return; }
  if (!gapRthOk()) {
    S.gaps = null;
    S.gapError = GAP_RTH_BAD;
    gapPrim.update(); renderGapInfo(); return;
  }
  if (gapBusy) { gapAgain = true; return; }
  gapBusy = true;
  const key = `${S.symbol} ${S.spec} ${S.rth}`;
  const q = new URLSearchParams({ symbol: S.symbol, spec: S.spec, RthStart: S.gapSet.RthStart, RthEnd: S.gapSet.RthEnd });
  if (S.rth) q.set('rth', 'true');
  for (const k of GAP_SERVER) q.set(k, String(S.gapSet[k]));
  try {
    const d = await api('GET', `/api/gaps?${q}`);
    if (key === `${S.symbol} ${S.spec} ${S.rth}` && S.st.gap) {
      d.curBar = d.bar;
      d.curT = d.t;
      S.gaps = d;
      S.gapError = '';
      gapCatchUp();
    }
  } catch (e) {
    S.gaps = null;
    S.gapError = e.message;
  } finally {
    gapBusy = false;
  }
  gapPrim.update();
  renderGapInfo();
  if (gapAgain) { gapAgain = false; gapRequest(); }
}
// Bars the chart has from the server's current bar on (the one it computed, then any newer) fill gaps here.
function gapCatchUp() {
  const d = S.gaps;
  if (!d || d.t == null || !S.bars.length) return;
  const i0 = barIndexFrom(S.bars, d.t);
  if (i0 < 0 || S.bars[i0].t !== d.t) return;
  for (let i = i0; i < S.bars.length; i++) gapLiveBar(d, S.bars[i], d.bar + (i - i0));
}
function renderGapInfo() {
  const el = $('#gap-info');
  if (!S.st.gap) { el.textContent = ''; return; }
  if (!gapSpecOk()) { el.textContent = 'Needs a minute or hour chart.'; return; }
  if (indReady('gap') && !S.gapSet.EnabledAnalysis) { el.textContent = 'Off: Enabled Analysis is unchecked in its settings.'; return; }
  if (S.gapError) { el.textContent = S.gapError; return; }
  const d = S.gaps, a = S.gapAlerts[S.symbol];
  const alerts = a?.on ? ` Alerts: ${[a.new && 'new gap', a.fill && 'fill'].filter(Boolean).join(' + ') || 'none'}${a.rth ? ' (RTH)' : ''}.`
    : a?.error ? ` Alerts off: ${a.error}` : '';   // on, but its times make no session on its hours (the server's why)
  el.textContent = !d ? 'Loading…' : `${d.gaps.length} gaps in ${S.gapSet.days} days, ${d.gaps.filter((g) => g.fb < 0).length} open. RTH ${gapRth()} CT.${alerts}`;
}
function renderGapMenu() {
  $('#gap-alerts').checked = !!S.gapAlerts[S.symbol]?.on;
  $('#gap-days').value = gapDays;
  renderGapInfo();
}
let gapAlertsPut = 0;   // gap alert PUTs sent and changes heard: a GET out meanwhile may answer with them from before
async function loadGapAlerts() {
  const n = gapAlertsPut;
  let got = null;
  try { got = await api('GET', '/api/gaps/alerts'); } catch { /* keep */ }
  if (n !== gapAlertsPut) return loadGapAlerts();   // one went out meanwhile: asked again, after it
  if (got) { S.gapAlerts = got.alerts || {}; if (got.switches) S.gapSwitches = got.switches; S.gapNotice = got.notice || []; }
  renderGapMenu();
}
// rth: the alerts watch the regular hours only, as an RTH chart's Gap Analysis (one setting per symbol: this chart's).
// The alert switches are the server's for the symbol (every chart of it).
async function putGapAlerts(on, sym = S.symbol, rth = S.rth) {
  if (!gapRthOk()) throw new Error(GAP_RTH_BAD);
  const g = S.gapSet;
  gapAlertsPut++;
  const a = await api('PUT', `/api/gaps/alerts/${sym}`, {
    on, RthStart: g.RthStart, RthEnd: g.RthEnd, MinimumGapPoints: g.MinimumGapPoints, rth: !!rth && !!S.instruments[sym]?.rth,
  });
  S.gapAlerts[sym] = a;   // into the alerts as they are now (a GET's answer may have replaced them meanwhile)
}
// The server's gap alerts use the RTH, minimum size and alert switches: after a change, the symbol's (another symbol's
// alerts keep theirs: its points and hours are its own), keeping whether they are on and the hours they were switched on
// with as the server has them (this page's copy is from its hello: another chart, tab or device may have switched them
// since). g: the settings saved, this chart's by default; sw: the alert switches changed in the dialog (the symbol's,
// every chart of it: only those are sent, over the server's as they are now). A chart of no instrument has no gap alerts.
async function gapAlertsFollow(g = S.gapSet, sw = {}, sym = S.symbol) {
  if (!gapRthOk(g)) throw new Error(GAP_RTH_BAD);
  const switches = Object.fromEntries(Object.entries(sw).filter(([k]) => k in GAP_SWITCH).map(([k, v]) => [GAP_SWITCH[k], v]));
  if (!S.instruments[sym]) {
    if (Object.keys(switches).length) throw new Error(`Gap Analysis' alert switches are each instrument's and ${sym} is not one: change them on an instrument's chart`);
    return;
  }
  gapAlertsPut++;
  const got = await api('PUT', `/api/gaps/alerts?symbol=${encodeURIComponent(sym)}`, {
    ...switches, RthStart: g.RthStart, RthEnd: g.RthEnd, MinimumGapPoints: g.MinimumGapPoints,
  });
  S.gapAlerts = got.alerts || {};
  if (got.switches) S.gapSwitches = got.switches;
  renderGapInfo();
}
$('#gap-days').onchange = (e) => {
  const n = Math.round(Number(e.target.value));
  if (!(n >= 1 && n <= 180)) { e.target.value = gapDays; toast('History: 1 to 180 days'); return; }
  gapDays = n;
  local.set('gapDays', String(n));
  S.gapSet.days = n;
  gapRequest();
};
$('#gap-alerts').onchange = async (e) => {
  const on = e.target.checked;
  try {
    await putGapAlerts(on);
    toast(on ? `Gap alerts on for ${S.symbol}` : `Gap alerts off for ${S.symbol}`);
  } catch (err) { e.target.checked = !on; fail(err); }
  renderGapMenu();   // the box too: a GET's answer meanwhile may have set it back
};

// An alert switch a chart turned off in its own settings before the switches were shared (the AMT Toolkit's and Gap
// Analysis', each its symbol's on every chart of it): moved to the shared settings once, to the symbols of the tabs that
// had it (a tab's own set: its symbol; the chart's shared set: its linked tabs'), off only (it silences, never turns an
// alert on), and then every shared setting leaves the chart's own, where nothing reads it. Each chart does its own tabs,
// another workspace's when it opens. The gap alert switches the server turned off at its first start (S.gapNotice) are
// said too, by the one page that claims them.
let sharedMoved = false;
async function moveSharedOut() {
  if (sharedMoved || TK_MULTI || !S.amtServer.schema || !S.gapSwitches) return;   // a layout's page has no tabs of its own
  const amtSh = amtShared(), amtNow = (sym) => ({ ...(S.amtServer.defaults || {}), ...amtStart(sym) });
  const own = (set) => Object.keys(set.amt || {}).some((k) => amtSh.has(k)) || Object.keys(set.gap || {}).some((k) => GAP_SHARED.has(k));
  const linked = [...new Set(S.tabs.filter((t) => t.link).map((t) => t.sym))];
  const sets = [...S.tabs.filter((t) => t.own?.set).map((t) => [t.own.set, [t.sym]]), [S.sharedSet, linked]];   // [set, its symbols]
  sharedMoved = true;
  const moved = sets.some(([set]) => own(set));
  if (!moved && !S.gapNotice.length) return;
  // the Alert switches (not the storm cap: off, it sends more)
  const silent = new Map(S.amtServer.schema.filter((x) => x.type === 'bool' && /alert/i.test(x.name) && !x.group.startsWith('18.'))
    .map((x) => [x.name, `${x.group.replace(/^\d+\. /, '')}: ${x.label}`]));
  const amtOff = {}, gapOff = {};   // symbol -> its switches to turn off
  let said = [];
  for (const [set, syms] of sets) {
    for (const [k, v] of Object.entries(set.amt || {})) {
      if (v !== false || !silent.has(k)) continue;
      for (const sym of syms) if (S.instruments[sym] && amtNow(sym)[k] === true) (amtOff[sym] ||= {})[k] = false;
    }
    for (const [k, v] of Object.entries(set.gap || {})) {
      if (v !== false || !(k in GAP_SWITCH)) continue;
      for (const sym of syms) if (S.instruments[sym] && gapShown(sym)[k]) (gapOff[sym] ||= {})[GAP_SWITCH[k]] = false;
    }
  }
  try {
    for (const [sym, off] of Object.entries(amtOff)) await amtPatch(off, sym);
    if (Object.keys(amtOff).length) await loadAmtServer();
    for (const [sym, off] of Object.entries(gapOff)) {
      S.gapSwitches = (await api('PUT', `/api/gaps/alerts?symbol=${encodeURIComponent(sym)}`, off)).switches || S.gapSwitches;
    }
    if (S.gapNotice.length) said = (await api('DELETE', '/api/gaps/alerts/notice')).notice || [];
  } catch { sharedMoved = false; return; }   // kept as they are: tried again at the next hello
  const drop = (set) => {
    const out = { ...set, amt: pickSet(set.amt, amtSh, false), gap: pickSet(set.gap, GAP_SHARED, false) };
    for (const k of ['amt', 'gap']) if (!Object.keys(out[k]).length) delete out[k];
    return out;
  };
  for (const t of S.tabs) if (t.own?.set && own(t.own.set)) t.own = { ...t.own, set: drop(t.own.set) };
  if (moved) saveTabs();
  if (own(S.sharedSet)) { S.sharedSet = drop(S.sharedSet); local.set('indSet', JSON.stringify(S.sharedSet)); }
  const gapName = (k) => (k === 'new' ? 'Gap Analysis: Alert on new gap' : 'Gap Analysis: Alert on full gap fill');
  const it = (a) => (a.length > 1 ? 'them' : 'it');
  const why = [...new Set([...Object.keys(amtOff), ...Object.keys(gapOff)])].map((sym) => {   // each symbol's charts
    const names = [...Object.keys(amtOff[sym] || {}).map((k) => `AMT ${silent.get(k)}`), ...Object.keys(gapOff[sym] || {}).map(gapName)];
    return `Off on every ${sym} chart, as this chart had ${it(names)}: ${names.join('; ')}.`;
  });
  // the first start's: off for every symbol then; said for those that have it off still (not ticked again since)
  const insts = Object.keys(S.instruments), before = new Map();   // whose charts -> the switches
  for (const k of said) {
    const off = insts.filter((sym) => S.gapSwitches?.[sym]?.[k] === false);
    if (!off.length) continue;
    const whose = off.length === insts.length ? 'every chart' : `every ${off.join(', ')} chart`;
    before.set(whose, [...(before.get(whose) || []), gapName(k)]);
  }
  for (const [whose, names] of before) why.push(`Off on ${whose}, as the gap alerts had ${it(names)} before: ${names.join('; ')}.`);
  if (why.length) toast(`Alert switches are shared now by every chart of a symbol (the AMT Toolkit's and Gap Analysis'). ${why.join(' ')}`, 10000);
}

// ---- HTF EMA --------------------------------------------------------------------------------------------------
// hemaNoBars: not asked, no bars yet; hemaStart: the first bar last asked for; hemaFailed: when an answer last failed
let hemaBusy = false, hemaAgain = false, hemaError = '', hemaAsked = 0, hemaFailed = 0, hemaNoBars = false, hemaStart = null;
async function hemaRequest() {
  hemaNoBars = !S.bars.length;
  if (!S.st.hema || !S.bars.length || !S.instruments[S.symbol] || !indReady('hema')) { S.hema = null; hemaDraw(); return; }
  if (hemaBusy) { hemaAgain = true; return; }
  hemaBusy = true;
  hemaAsked = Date.now();
  hemaStart = S.bars[0].t;
  const key = `${S.symbol} ${S.spec} ${S.rth}`, o = S.hemaSet;
  const q = new URLSearchParams({ symbol: S.symbol, spec: S.spec, start: S.bars[0].t, HtfPeriodType: o.HtfPeriodType,
    HtfPeriod: o.HtfPeriod, EmaLength: o.EmaLength, Mode: o.Mode });
  if (S.rth) q.set('rth', 'true');
  try {
    const d = await api('GET', `/api/htfema?${q}`);
    if (key === `${S.symbol} ${S.spec} ${S.rth}` && S.st.hema) { S.hema = d; hemaError = ''; }
  } catch (e) {
    S.hema = null;
    hemaError = e.message;
    hemaFailed = Date.now();
  } finally {
    hemaBusy = false;
  }
  hemaDraw();
  if (hemaAgain) { hemaAgain = false; hemaRequest(); }
}
const hemaPeriodMs = () => (isTime() ? specSeconds(S.spec) * 1000 : 0);
const hemaCal = () => /^\d+[DWM]$/.test(S.spec) && S.spec !== '1D';   // bars of several sessions (1D: cut at its close)
// When the bar after the last one opens: in bar replay the next bar's (the HTF bars run to the present; past the bars
// loaded, the next stretch's, else the replay clock), else the server's once a calendar bar is over (its last HTF bar
// runs after it, as on a tie).
const hemaAfter = () => { const r = S.replay; return r ? r.data[r.idx]?.t ?? r.nextAt ?? r.at : S.hema?.next; };
const hemaPoint = (b, v) => (Number.isNaN(v) ? { time: timeOf(b) } : { time: timeOf(b), value: v });
function hemaDraw() {
  const d = S.st.hema ? S.hema : null;
  hemaLine.applyOptions({ visible: !!d, ...hemaStyle() });
  S.hemaVals = d ? htfEmaValues(S.bars, d, hemaPeriodMs(), hemaCal(), hemaAfter()) : [];
  hemaLine.setData(d ? S.bars.map((b, i) => hemaPoint(b, S.hemaVals[i])) : []);
  renderHemaInfo();
}
// Calculate.OnPriceChange: the forming bar's value moves with its close.
function hemaLive() {
  if (!S.st.hema || !S.hema || !S.bars.length) return;
  const vals = htfEmaValues(S.bars, S.hema, hemaPeriodMs(), hemaCal(), hemaAfter()), i = S.bars.length - 1;
  S.hemaVals = vals;
  try { hemaLine.update(hemaPoint(S.bars[i], vals[i])); } catch { hemaDraw(); }
}
// A bar's update: the line follows it. The server is asked again when a chart bar closes and its answer may have
// changed: the forming HTF bar closed (d.due; or none was forming), the chart's first bar is another (bars trimmed), a
// bar of several sessions was over (d.next), or a minute after a failed answer; on a seconds or tick chart a bar closes
// every second or so. Not while it is being asked: the bars closing meanwhile ask once it answers, if it is out of date
// (new bars and settings ask again then). For Exact and Live (the last closed HTF bar's EMA) also when an HTF bar
// closes inside the forming one, as on a 4h, day or week chart (at most once a minute, the clocks can differ).
function hemaTick(closed) {
  if (!S.st.hema) return;
  if (!S.hema && hemaNoBars) { hemaRequest(); return; }   // the first bars came (a chart or a replay that started empty)
  if (closed) hemaDraw(); else hemaLive();
  if (S.replay || hemaBusy) return;
  const d = S.hema, now = Math.max(Date.now(), S.bars.at(-1)?.t ?? 0);
  if (closed && (d ? now >= (d.due ?? 0) || S.bars[0].t !== hemaStart || d.next != null : now - hemaFailed > 60_000)) hemaRequest();
  else if (d && d.mode !== 'SmoothScaled' && now >= (d.due ?? 0) && now - hemaAsked > 60_000) hemaRequest();
}
function renderHemaInfo() {
  const el = $('#hema-info'), o = S.hemaSet;
  if (!S.st.hema) { el.textContent = ''; return; }
  if (hemaError) { el.textContent = hemaError; return; }
  const src = o.HtfPeriodType === 'Day' ? `${o.HtfPeriod}D` : o.HtfPeriod % 60 ? `${o.HtfPeriod}m` : `${o.HtfPeriod / 60}h`;
  const d = S.hema;
  el.textContent = !d ? 'Loading…'
    : o.Mode === 'SmoothScaled' ? `EMA ${d.scaled} of this chart's bars (${src} ${o.EmaLength} scaled).`
    : o.Mode === 'ExactHTF' ? `Last closed ${src} EMA ${o.EmaLength}.` : `${src} EMA ${o.EmaLength} with the live price.`;
}

// ---- Daily SMA x4: OnBarUpdate runs here on the chart's bars (studies.js), over the daily bars the server builds
// from its 1-minute bars; the developing day's close is the live price. -------------------------------------------
const dsmaPeriods = () => [1, 2, 3, 4].map((i) => S.dsmaSet[`SMA${i}Length`]);
const pxTick = (p) => px(Math.round(p / inst().tick_size) * inst().tick_size);   // NinjaTrader's FormatPrice
function dsmaStyle(i) {   // Configure: every plot's width is Line Width
  const p = S.dsmaSet[`PlotDailySMA${i + 1}`] || { color: '#808080', dash: 'Solid' };
  return { color: p.color, lineWidth: S.dsmaSet.LineWidth || 2, lineStyle: LW_DASH[p.dash] ?? 0 };
}
let dsmaBusy = false, dsmaAgain = false, dsmaError = '', dsmaAsked = 0, dsmaNoBars = false;   // dsmaNoBars: not asked, no bars yet
const dsmaKey = () => `${S.symbol} ${S.spec} ${S.rth} ${S.bars[0]?.t}`;
async function dsmaRequest() {
  dsmaNoBars = !S.bars.length;
  if (!S.st.dsma || !S.bars.length || !S.instruments[S.symbol] || !indReady('dsma')) { S.dsma = null; dsmaDraw(); return; }
  if (dsmaBusy) { dsmaAgain = true; return; }
  dsmaBusy = true;
  dsmaAsked = Date.now();
  const key = dsmaKey(), length = Math.max(...dsmaPeriods());
  try {
    const d = await api('GET', `/api/dsma?${new URLSearchParams({ symbol: S.symbol, start: S.bars[0].t, length, rth: S.rth })}`);
    if (key === dsmaKey() && S.st.dsma) { S.dsma = { ...d, length }; dsmaError = ''; }
  } catch (e) {
    S.dsma = null;
    dsmaError = e.message;
  } finally {
    dsmaBusy = false;
  }
  dsmaDraw();
  if (dsmaAgain) { dsmaAgain = false; dsmaRequest(); }
}
const dsmaOpts = () => ({ cal: /^\d+[DWM]$/.test(S.spec), periodMs: isTime() ? specSeconds(S.spec) * 1000 : 0, live: S.dsmaLive,
  price: S.bars.at(-1)?.c, until: S.replay ? S.replay.at : null });
const dsmaPoint = (b, v) => (Number.isFinite(v) ? { time: timeOf(b), value: v } : { time: timeOf(b) });
function dsmaDraw() {
  const d = S.st.dsma ? S.dsma : null;
  S.dsmaPrep = d ? dailySmaPrep(d.days, dsmaPeriods()) : null;
  S.dsmaVals = d ? dailySmaValues(S.bars, d, S.dsmaPrep, dsmaOpts()) : [];
  dsmaLines.forEach((l, i) => {
    l.applyOptions({ visible: !!d, ...dsmaStyle(i) });
    l.setData(d ? S.bars.map((b, j) => dsmaPoint(b, S.dsmaVals[i][j])) : []);
  });
  dsmaPrim.update();
  renderDsmaInfo();
  if (!S.hovering) legend(S.lastBar);
}
// OnEachTick: the last bar's values with the live price (the last bar's alone: they don't depend on the others).
function dsmaLive() {
  if (!S.dsmaPrep || !S.bars.length) return;
  const i = S.bars.length - 1, b = S.bars[i], out = dailySmaValues([b], S.dsma, S.dsmaPrep, dsmaOpts());
  S.dsmaVals.developing = out.developing;
  out.forEach((v, p) => {
    S.dsmaVals[p][i] = v[0];
    try { dsmaLines[p].update(dsmaPoint(b, v[0])); } catch { /* the next full draw */ }
  });
  dsmaPrim.update();
}
// A new bar: its values and the one before's (whose close is now known); the bars before keep theirs. Drawn whole
// when the one before changed (it took no live tick) or more than one bar came.
function dsmaAppend() {
  const n = S.bars.length, was = S.dsmaVals;
  if (!S.dsmaPrep || n < 2 || was.length !== 4 || was[0].length !== n - 1) { dsmaDraw(); return; }
  const out = dailySmaValues(S.bars.slice(-2), S.dsma, S.dsmaPrep, dsmaOpts());
  if (out.some((v, p) => !Object.is(v[0], was[p][n - 2]))) { dsmaDraw(); return; }
  was.developing = out.developing;
  out.forEach((v, p) => {
    was[p].push(v[1]);
    try { dsmaLines[p].update(dsmaPoint(S.bars[n - 1], v[1])); } catch { /* the next full draw */ }
  });
  dsmaPrim.update();
  if (!S.hovering) legend(S.lastBar);
}
// Live bars (and replay's, as NinjaTrader's playback): a bar that took the developing day's values keeps them, as
// NinjaTrader's realtime bars do; a new trading day is asked for once one has begun.
function dsmaTick(closed) {
  if (!S.st.dsma) return;
  if (!S.dsma) { if (dsmaNoBars || (dsmaError && !S.replay && Date.now() - dsmaAsked > 60_000)) dsmaRequest(); return; }   // the first bars came, or it failed: again
  if (closed) dsmaAppend(); else dsmaLive();
  const i = S.bars.length - 1;
  if (S.dsmaVals.developing) S.dsmaLive.set(S.bars[i].t, S.dsmaVals.map((v) => v[i]));
  if (S.replay) return;
  const last = S.dsma.days.at(-1), now = Math.max(Date.now(), S.bars[i].t);
  if ((!last || now >= last[1]) && now - dsmaAsked > 60_000) dsmaRequest();
}
// OnRender's labels: only while the last bar is on screen (ChartBars.ToIndex), LabelOffset bar spaces right of it.
const dsmaPrim = {
  p: null,
  views: [{ zOrder: () => 'top', renderer: () => (dsmaPrim.p ? { draw: (target) => target.useMediaCoordinateSpace(({ context, mediaSize }) => dsmaLabels(context, mediaSize)) } : null) }],
  attached(p) { this.p = p; },
  detached() { this.p = null; },
  paneViews() { return this.views; },
  update() { if (this.p) this.p.requestUpdate(); },
};
function dsmaLabels(ctx, size) {
  const n = S.bars.length, ts = chart.timeScale(), r = ts.getVisibleLogicalRange();
  if (!S.st.dsma || !S.dsma || !S.dsmaSet.ShowLabels || !n || S.dsmaVals.length !== 4 || !r || r.to < n - 1.5) return;
  const x = ts.logicalToCoordinate(n - 1);
  if (x == null) return;
  drawDailySmaLabels(ctx, size, candles, { x, spacing: ts.options().barSpacing, vals: S.dsmaVals.map((v) => v[n - 1]),
    periods: dsmaPeriods(), colors: [0, 1, 2, 3].map((i) => dsmaStyle(i).color), s: S.dsmaSet, fmt: pxTick });
}
function renderDsmaInfo() {
  const el = $('#dsma-info');
  if (!el) return;
  if (!S.st.dsma) { el.textContent = ''; return; }
  if (dsmaError) { el.textContent = dsmaError; return; }
  const d = S.dsma;
  if (!d) { el.textContent = 'Loading…'; return; }
  const p = dsmaPeriods(), short = p.filter((n) => n > d.days.length).sort((a, b) => a - b);
  el.textContent = `${d.days.length} daily bars from the 1-minute bars stored here${S.rth && inst().rth ? ' (regular hours)' : ''}.`
    + (short.length ? ` Too few for ${short.join(', ')}: those lines start once that many days are stored.` : '');
}

// ---- If/Then: the block is per symbol on the server (the Plan tab); fetched even while hidden, for the Plan tab ------
let ifFetched = 0, ifHeard = 0;   // ifHeard: blocks broadcast for the open symbol
async function ifRequest() {
  if (!S.instruments[S.symbol] || !indReady('ifthen')) { renderIfInfo(); return; }
  const sym = S.symbol, was = S.ifData, heard = ifHeard;
  ifFetched = Date.now();
  try {
    const d = await api('GET', `/api/ifthen/${sym}?atr=${S.ifSet.AtrLength}`);
    if (sym !== S.symbol) return;
    // a block broadcast while this was answered: the answer may have been read before it was saved
    const late = ifHeard !== heard && S.ifData;
    S.ifData = late ? { ...d, rows: late.rows, updated: late.updated } : d;
    S.ifRows = biasParse(S.ifData.rows);
  } catch (e) { $('#if-info').textContent = e.message; return; }
  amtLayout();
  ifPrim.update();
  renderPlanIf(false);
  renderIfInfo();
  if (was?.rows !== S.ifData.rows) indDlg.refresh();   // an open Indicators dialog shows the block that came
}
function renderIfInfo() {
  const el = $('#if-info');
  if (!S.st.ifthen) { el.textContent = ''; return; }
  const f = ifFrame(), d = S.ifData;
  if (!d) { el.textContent = 'Loading…'; return; }
  if (!S.ifRows.length) { el.textContent = d.rows ? 'No valid rows: use BIAS|LO|HI|TEXT, one band per line.' : `No block for ${S.symbol}: paste it on the Plan tab.`; return; }
  const act = f && f.active >= 0 ? `now in ${biasRange(S.ifRows[f.active])}` : '';
  el.textContent = `${S.ifRows.length} bands${act ? `, ${act}` : ''}. Daily ATR(${S.ifSet.AtrLength}) ${d.atr != null ? px(d.atr) : 'needs more days'}.`;
}

// ---- Level Lines: NinjaTrader's OnRender (studies.js); the range and levels are per symbol ------------------------------
const llPrim = new LevelLinesPrimitive(() => (S.st.lines && !S.ext && indReady('lines')
  ? { s: S.llSet, fmt: (p) => px(Math.round(p / inst().tick_size) * inst().tick_size) } : null));
candles.attachPrimitive(llPrim);
function renderLlInfo() {
  const el = $('#ll-info');
  if (!S.st.lines) { el.textContent = ''; return; }
  const n = indReady('lines') ? levelLinesOf(S.llSet).length : 0;
  el.textContent = n ? `${n} lines on ${S.symbol}.` : `No range or levels for ${S.symbol}: add them on the Plan tab.`;
}

// ---- Plan tab: this symbol's daily levels (as TradingView takes them), If/Then block and Level Lines ---------------------
const markDirty = (e) => { e.target.dataset.dirty = '1'; e.target.dataset.sym ||= S.symbol; };
// fill: replace what is in a box unless it has unsaved edits or the cursor is in it; says whether it did. A box holding
// another symbol's text (dataset.sym: the chart switched since) is replaced, edited or not; its own symbol's edits stay
// through a reconnect or new levels.
function fillBox(el, v, fill) {
  fill ||= !!el.dataset.sym && el.dataset.sym !== S.symbol;
  if ((fill || !el.dataset.dirty) && document.activeElement !== el) { el.value = v; delete el.dataset.dirty; el.dataset.sym = S.symbol; return true; }
  return false;
}
S.lvStatus = null;
async function loadPlanLevels() {
  const sym = S.symbol;
  if (isExtSym(sym)) { S.lvStatus = null; $('#pl-lv-src').textContent = 'Levels are for the live futures charts.'; return; }
  try {
    const d = await api('GET', `/api/amt/levels/${sym}`);
    if (sym === S.symbol) { S.lvStatus = d; renderPlanLevels(false); indDlg.refresh(); }   // and in an open Indicators dialog
  } catch (e) { $('#pl-lv-src').textContent = e.message; }
}
const LV_ORIGIN = { file: 'levels file', link: 'levels link', paste: 'pasted line' };
function renderPlanLevels(fill) {
  const d = S.lvStatus, src = $('#pl-lv-src');
  if (!d) { src.textContent = 'Loading…'; return; }
  src.textContent = (d.count ? `${S.symbol}: ${d.count} levels${d.date ? ` for ${d.date}` : ''} from the ${LV_ORIGIN[d.origin] || d.origin}`
    + `${(d.origin === 'file' || d.origin === 'link') && d.where ? ` (${d.where})` : ''}.` : `${S.symbol}: no levels yet.`) + (d.error ? ` ${d.error}` : '');
  src.classList.toggle('bad', !!d.error);
  fillBox($('#pl-lv'), d.paste || '', fill);
  $('#pl-lv-msg').textContent = d.origin === 'file' || d.origin === 'link' ? 'A levels file or link wins over a pasted line.' : '';
}
async function savePlanLevels(text) {
  S.lvStatus = await api('PUT', `/api/amt/levels/${S.symbol}`, { text });
  delete $('#pl-lv').dataset.dirty;
  renderPlanLevels(true);
  if (text.trim() && !S.st.amt) { setStudy('amt', true); toast('AMT Toolkit turned on: it draws the daily levels'); }
}
$('#pl-lv').oninput = markDirty;
$('#pl-lv-save').onclick = async () => {
  const text = $('#pl-lv').value;
  try {
    await savePlanLevels(text);
    if (S.st.amt) toast(text.trim() ? `${S.symbol}: ${S.lvStatus.count} levels applied` : `${S.symbol}: pasted levels cleared`);
  } catch (e) { fail(e); }
};
$('#pl-lv-clear').onclick = async () => {
  $('#pl-lv').value = '';
  try { await savePlanLevels(''); toast(`${S.symbol}: pasted levels cleared`); } catch (e) { fail(e); }
};

function renderPlanIf(fill = true) {
  const d = S.ifData, box = $('#pl-if');
  // a box holding another symbol's block (the chart switched since) is replaced, edited or not; empty until this one's
  const other = !!box.dataset.sym && box.dataset.sym !== S.symbol;
  if ((d || other) && fillBox(box, d?.rows || '', fill || other)) box.dataset.sym = S.symbol;
  $('#pl-if-msg').textContent = !d ? '' : `${S.ifRows.length} bands${d.updated ? `, saved ${ax.full.format(d.updated)} ${zs(d.updated)}` : ''}`;
}
$('#pl-if').oninput = markDirty;
$('#pl-if-save').onclick = async () => {
  const ta = $('#pl-if');
  try {
    const d = await api('PUT', `/api/ifthen/${S.symbol}`, { rows: ta.value });
    delete ta.dataset.dirty;
    S.ifData = { ...(S.ifData || {}), ...d };
    S.ifRows = biasParse(d.rows);
    toast(`If/Then saved for ${S.symbol}: ${S.ifRows.length} bands`);
    if (!S.st.ifthen) setStudy('ifthen', true);
  } catch (e) { fail(e); }
  amtLayout();
  ifPrim.update();
  renderPlanIf(false);
  renderIfInfo();
};

const LL_BOXES = ['#pl-ll-top', '#pl-ll-bot', '#pl-ll'];
function renderPlanLines(fill = true) {
  if (!indReady('lines')) return;
  const s = S.llSet;
  fillBox($('#pl-ll-top'), s.RangeTop || '', fill);
  fillBox($('#pl-ll-bot'), s.RangeBottom || '', fill);
  fillBox($('#pl-ll'), s.CustomLevels || '', fill);
  const n = levelLinesOf(s).length;
  $('#pl-ll-msg').textContent = n ? `${n} lines` : '';
}
LL_BOXES.forEach((q) => { $(q).oninput = markDirty; });
$('#pl-ll-save').onclick = async () => {
  const num = (q) => ($(q).value.trim() === '' ? 0 : Number($(q).value));
  const top = num('#pl-ll-top'), bot = num('#pl-ll-bot');
  if (!(top >= 0) || !(bot >= 0)) { toast('Range top and bottom: a price, or empty for none'); return; }
  try {
    await indPutSymbol('lines', { RangeTop: top, RangeBottom: bot, CustomLevels: $('#pl-ll').value });
    LL_BOXES.forEach((q) => delete $(q).dataset.dirty);
    renderPlanLines(true);
    toast(`Level Lines saved for ${S.symbol}`);
    if (!S.st.lines) setStudy('lines', true);
  } catch (e) { fail(e); }
};

// ---- the Indicators dialog (indicators.js): one adapter per indicator ---------------------------------------------
const setStudy = (k, on) => { if (replayBlocks(k)) return; S.st[k] = on; studiesChanged(k); };
const LVL_TEXT = { name: 'lvl_text', group: '2. Daily Levels', order: 9, label: 'Paste Levels Here', type: 'text',
  description: "This symbol's levels: the one-line paste (2026-09-30, pivot, prices...) or the Pivot Level / Resistance Levels / Support Levels block. Used when no levels file or link has levels. Also on the Plan tab." };
const IND_ORDER = ['amt', 'ifthen', 'lines', 'tpo', 'gap', 'hema', 'dsma', ...VP_KINDS];
const IND_NOTES = {
  amt: 'Runs on the server for every device. All session fields are Central exchange time. The alert settings (the Alerts group, every Alert switch and the level zone width, marked with the symbol) are the symbol\'s: the same on every chart of it (one alert runner per symbol), while another symbol\'s charts keep theirs, also on Apply to all charts. The levels file is the same on every chart. Presets leave both as they are. The alerts follow their Alert switches only: a group\'s Enable switch only shows or hides it on this chart.',
  ifthen: "The block and the instrument label are this symbol's; the block is also on the Plan tab.",
  lines: "The range and the custom levels are this symbol's; they are also on the Plan tab.",
  gap: 'How many days it scans is in the Studies menu (History). The alert switches (marked with the symbol) are the symbol\'s: the same on every chart of it (the server\'s gap alerts for it), while another symbol\'s charts keep theirs, also on Apply to all charts. The symbol\'s gap alerts take the RTH times and minimum gap size of the chart of it that last saved them or switched them on, never another symbol\'s. Presets leave the alert switches as they are.',
  hema: 'The source timeframe can be Minute or Day bars.',
  dsma: "Daily bars from the 1-minute bars stored here (the last 1000, as NinjaTrader loads), one per CME trading day, or per regular session on an RTH chart. Plot widths are Line Width.",
  aavp: "Runs on the server from the 1-minute bars stored here, as far back as they go. Pin to visible edge keeps a long anchor's histogram on screen.",
  pvp: "Runs on the server from the 1-minute bars stored here. A line ends at the first bar that touches it after its period.",
  prof: 'Runs on the server from every trade stored here, up or down by the tick rule. Session times are in CT (ExchangeTemplate, Chart and Central), or Eastern or UTC as picked.',
};
const same = (x, y) => JSON.stringify(x ?? {}) === JSON.stringify(y ?? {});
function indAdapter(key) {
  const whose = ` Saved for ${setWhere()} only; Apply to all charts puts them on every chart.`;
  if (key === 'amt') {
    const sch = [...(S.amtServer.schema || [])];
    const i = sch.findIndex((x) => x.group === LVL_TEXT.group && x.order > LVL_TEXT.order);
    sch.splice(i < 0 ? sch.length : i, 0, LVL_TEXT);
    const shared = amtShared(), server = () => amtStart();
    // the chart's settings over the starting ones; the shared ones (the symbol's alert settings, the levels file) as the
    // server has them now, so the draft holds only those changed here, and only those are sent (over the server's as
    // they are then)
    return {
      key, title: 'AMT Toolkit', version: S.amtServer.version, colorFormat: 'argb', note: `${IND_NOTES.amt}${whose}`,
      shown: () => !!S.st.amt, show: (on) => setStudy('amt', on),
      schema: () => sch, defaults: () => ({ ...(S.amtServer.defaults || {}), ...server() }),
      ntDefaults: () => S.amtServer.defaults || {}, overrides: () => amtOwn(), sharedNames: shared,
      symbolShared: amtBySymbol(), allNote: () => ` The alert settings go to every ${S.symbol} chart only: each symbol keeps its own.`,
      symbolNames: ['lvl_text'], perSymbol: () => ({ lvl_text: S.lvStatus?.paste ?? '' }), symbolDefault: () => '',
      async save(d, s) {
        const mine = pickSet(d, shared, false), all = pickSet(d, shared, true);
        if (Object.keys(all).length) {   // the alert settings (every chart of this symbol) or the levels file (every chart)
          await amtPatch(all);
          await loadAmtServer();
        }
        if (!same(mine, amtOwn())) {   // checked as the Toolkit does, then kept by the chart
          await api('POST', '/api/amt/settings/check', { ...pickSet(server(), shared, false), ...mine });
          tabSetSave('amt', mine);
        }
        if (s && s.lvl_text !== (S.lvStatus?.paste ?? '')) await savePlanLevels(s.lvl_text);
        else if ('LevelsFile' in d || 'LevelsFile' in server()) setTimeout(loadPlanLevels, 6000);   // read on the next check
      },
      async applyAll(d) {   // every chart: these become the starting settings, and no chart keeps its own
        // over the server's as they are now: the shared ones changed here only, the alert settings this symbol's
        await amtPatch(d);
        await loadAmtServer();
      },
    };
  }
  const sc = S.ind.schema[key], shared = key === 'gap' ? GAP_SHARED : new Set();
  const a = {
    key, title: sc.title.replace(/^Tatanka /, ''), version: sc.version, colorFormat: 'hex', note: `${IND_NOTES[key] || ''}${whose}`,
    shown: () => !!S.st[key], show: (on) => setStudy(key, on),
    // the chart's settings sit on top of the starting ones (the server's): "default" here is what those say; Gap
    // Analysis' alert switches as the server's gap alerts for the chart's symbol have them, as the AMT Toolkit's
    schema: () => sc.settings, defaults: () => ({ ...indDefaults(key), ...(S.ind.overrides[key] || {}), ...(key === 'gap' ? gapShown() : {}) }),
    ntDefaults: () => indDefaults(key), overrides: () => pickSet(tabSet()[key], shared, false), sharedNames: shared,
    ...(key === 'gap' ? { symbolShared: shared, allNote: () => ` The alert switches go to every ${S.symbol} chart only, and only ${S.symbol}'s gap alerts take the RTH times and minimum gap size: each symbol keeps its own.` } : {}),
    symbolNames: S.ind.per_symbol?.[key] || [],
    perSymbol: () => ({ ...(S.ind.symbols[key]?.[S.symbol] || {}), ...(key === 'ifthen' ? { RowsBlock: S.ifData?.rows ?? '' } : {}) }),
    symbolDefault: (n) => (n === 'InstrumentLabel' ? S.symbol : n === 'RowsBlock' ? '' : indDefaults(key)[n]),
    async save(d, s) {
      const mine = pickSet(d, shared, false);
      if (!same(mine, a.overrides())) tabSetSave(key, mine);
      if (s) {   // only what changed, so an unchanged If/Then block keeps its saved time
        const cur = a.perSymbol(), ch = Object.fromEntries(Object.entries(s).filter(([k, v]) => !same(v, cur[k] ?? a.symbolDefault(k))));
        if (Object.keys(ch).length) await indPutSymbol(key, ch);
        if (key === 'lines') renderPlanLines(true);
      }
      if (key === 'gap') await gapAlertsFollow(S.gapSet, pickSet(d, shared, true));   // the chart's symbol's alerts
    },
    async applyAll(d) {   // every chart: these become the starting settings, and no chart keeps its own
      const full = { ...a.defaults(), ...d }, nt = indDefaults(key);
      await indPut(key, Object.fromEntries(Object.entries(full).filter(([n, v]) => n in nt && !shared.has(n) && !same(v, nt[n]))));
      // as OK does, for this symbol's alerts (another's keep theirs); this tab's own may still be in S.gapSet until allCharts
      if (key === 'gap') await gapAlertsFollow(full, pickSet(d, shared, true));
    },
  };
  return a;
}
const indDlg = createIndicatorsDialog({
  root: $('#ind-dlg'), local, toast, fail: (e) => fail(e), symbol: () => S.symbol, ask: (o) => ask(o),
  templates: () => templates,   // NinjaTrader's "Save as template", on every device (defined further down)
  adapters: () => IND_ORDER.filter((k) => (k === 'amt' ? S.amtServer.schema : S.ind.schema[k])).map(indAdapter),
  allCharts: (key) => studyAll(key),
});
function openInd(key) {
  if (!S.amtServer.schema && !indReady('tpo')) { toast('Settings are still loading'); return; }
  openSt(false);
  indDlg.open(true, key);
}
function showPlan(focus) {
  openSt(false);
  document.querySelector('.tabs button[data-tab="plan"]').click();
  if (focus) setTimeout(() => $(`#${focus}`)?.focus(), 60);
}
document.querySelectorAll('[data-ind]').forEach((b) => { b.onclick = () => openInd(b.dataset.ind || undefined); });
document.querySelectorAll('[data-plan]').forEach((b) => { b.onclick = () => showPlan(b.dataset.plan); });
$('#pl-all').onclick = () => openInd();
$('#amt-set-open').onclick = () => openInd('amt');


let rangeTimer = 0, rangeReq = 0;
function scheduleRange(delay = 400) { clearTimeout(rangeTimer); rangeTimer = setTimeout(fetchRange, delay); }
async function fetchRange() {
  const mode = S.st.right, n = S.bars.length;
  if (mode === 'off' || !n || S.ext) { S.rangeProfile = null; rangePrim.update(); return; }
  let q;
  if (mode === 'visible') {
    const r = chart.timeScale().getVisibleLogicalRange();
    if (!r) return;
    const i0 = Math.min(Math.max(Math.floor(r.from), 0), n - 1), i1 = Math.min(Math.max(Math.ceil(r.to), 0), n - 1);
    q = `start=${S.bars[i0].t}&end=${i1 + 1 < n ? S.bars[i1 + 1].t : Date.now() + 60000}&rth=${S.rth}`;   // RTH: not the nights between the sessions
  } else q = `sessions=${mode === 'c5' ? 5 : 20}`;
  const id = ++rangeReq, sym = S.symbol;
  try {
    const p = await api('GET', `/api/profile?symbol=${sym}&${q}`);
    if (id === rangeReq && sym === S.symbol) { S.rangeProfile = p && p.v ? p : null; rangePrim.update(); }
  } catch { /* keep the last one */ }
}
chart.timeScale().subscribeVisibleLogicalRangeChange(() => { if (S.st.right === 'visible') scheduleRange(); });
setInterval(() => { if (S.st.right !== 'off' && document.visibilityState === 'visible') fetchRange(); }, 15000);

// Bar under the crosshair, with its delta and study values.
function barAt(time) {
  if (!S.bars.length) return null;
  if (isTime()) { const i = barIndexFrom(S.bars, time * 1000); return i >= 0 && S.bars[i].t === time * 1000 ? S.bars[i] : null; }
  return S.bars[time - S.bars[0].seq] || null;
}

function barIndexAt(time) {
  if (!S.bars.length) return -1;
  if (isTime()) { const i = barIndexFrom(S.bars, time * 1000); return i >= 0 && S.bars[i].t === time * 1000 ? i : -1; }
  const i = time - S.bars[0].seq;
  return i >= 0 && i < S.bars.length ? i : -1;
}

function toPoints(b) {
  const time = timeOf(b);
  if (!isTime()) S.seqTime.set(b.seq, b.t);
  const doji = b.c === b.o && !S.candlesHidden;   // NinjaTrader paints these in its doji color (when the candles show)
  return [doji ? { time, open: b.o, high: b.h, low: b.l, close: b.c, color: C.doji, wickColor: C.doji, borderColor: C.doji } : { time, open: b.o, high: b.h, low: b.l, close: b.c },
          { time, value: b.v, color: (b.c >= b.o ? C.up : C.down) + '66' }];
}

// Bars kept: past this the oldest go, from the chart too, so a bar's index stays the chart's.
const BAR_CAP = 20000;
function trimBars() {
  const cut = S.bars.length - BAR_CAP, r = chart.timeScale().getVisibleLogicalRange();
  S.bars.splice(0, cut);
  S.seqTime.clear();
  const c = [], v = [];
  for (const b of S.bars) { const [cb, vb] = toPoints(b); c.push(cb); v.push(vb); }
  candles.setData(c);
  vol.setData(v);
  altSetData();
  setStudyData();
  indsUpdate(true);
  hemaDraw();
  dsmaDraw();
  for (const c of cmpLines) c.series.setData(cmpAlign(c));
  if (r) chart.timeScale().setVisibleLogicalRange({ from: r.from - cut, to: r.to - cut });
}

function applyHistory(m) {
  $('#splash').classList.add('gone');   // first chart in: the loading screen fades
  // trade dates and session breaks on this chart's exchange's clock; D/W/M bars are stamped the app's way, at the
  // 17:00 CT open before their day (a free-data chart's too), so theirs on that clock
  setSessionClock(calSpec() ? TZ_EXCHANGE : exch().tz, calSpec() ? '17:00' : exch().day);
  setSessionHolidays(S.ext ? null : S.holidays, inst().calendar || 'equity');   // and CME's holiday halts (the pivots' days)
  cmpClear();   // their points sit on the old bars: gone before the new ones, so the view is the new bars'
  S.kind = m.kind;
  S.seqTime.clear();
  const c = [], v = [];
  for (const b of m.bars) { const [cb, vb] = toPoints(b); c.push(cb); v.push(vb); }
  vol.setData([]);   // the old bars' volume goes first: kept while the new candles land, it can trip the chart up
  candles.setData(c);
  vol.setData(v);
  S.bars = m.bars.slice();
  S.liveWait = false;   // these are the bars fpFetch asks for
  altSetData();
  S.fp.clear();
  fpSchedule(300);
  setStudyData();
  indsUpdate(true);
  S.hema = null;
  hemaDraw();
  hemaRequest();
  S.dsma = null;
  S.dsmaLive.clear();   // a new load: NinjaTrader's historical bars, none of them live
  dsmaDraw();
  dsmaRequest();
  renderIfInfo();
  scheduleRange(0);
  S.lastBar = m.bars.at(-1) || null;
  priceLineLayout();
  if (bigOn()) bigSchedule(300);
  fillsSchedule(300);
  const scaleKey = viewKey(), want = S.viewWant;
  S.loadedKey = scaleKey;
  S.loadedSym = S.symbol;   // whose bars the chart shows (subscribe())
  S.viewWant = null;
  if (viewApply(want)) S.scaleKey = scaleKey;   // the tab's view from before (or the same bars again: as they were)
  else {
    const ro = chart.timeScale().options().rightOffset;
    chart.timeScale().setVisibleLogicalRange({ from: c.length - 150 - ro, to: c.length + ro });
    if (S.lock) applyLock(true);   // a locked ratio centres on the new bars
    if (scaleKey !== S.scaleKey) {   // another symbol or bar type: a fixed price range from before would show nothing
      if (S.scaleKey && !S.lock && !PS().options().autoScale) setAuto(true);
      S.scaleKey = scaleKey;
    }
  }
  if (S.goTo != null) { const ms = S.goTo; S.goTo = null; goToTime(ms); }   // a journal trade on another symbol
  if (ax.pick === 'exchange' && exch().tz !== ax.zone) setChartZone('exchange', false);   // another exchange's clock
  cmpLoad();
  loadNote('');   // loaded: the legend says the source
  header();
}
// The chart's bars, gone: while another symbol's bars (a free-data chart's other timeframe or session) are on their
// way, and when they don't come. The tab's view to restore (S.viewWant) stays for them.
function barsClear() {
  cmpClear();
  S.seqTime.clear();
  vol.setData([]);
  candles.setData([]);
  S.bars = [];
  S.lastBar = null;
  S.loadedKey = S.loadedSym = '';
  altSetData();
  S.fp.clear();
  setStudyData();
  indsUpdate(true);
  S.hema = null;
  hemaDraw();
  S.dsma = null;
  S.dsmaLive.clear();
  dsmaDraw();
  priceLineLayout();
  header();
}
// What the chart is waiting for, or why it has no bars: after the legend's name (which every legend() keeps), and
// `over` the chart while it has no bars.
function loadNote(note, over = note) {
  S.loadNote = note;
  const el = $('#chart-msg');
  el.textContent = note && !S.bars.length ? over : '';
  el.hidden = !el.textContent;
  legend(S.lastBar);
}

function applyBars(m) {
  let closed = false;
  const was = S.bars[S.bars.length - 1]?.seq;   // the forming bar before this message
  for (const b of m.bars) {
    const [cb, vb] = toPoints(b);
    try { candles.update(cb); vol.update(vb); if (altSeries && altSeries._style !== 'ha') altSeries.update(altPoint(b)); updateStudyBar(b); } catch (e) { console.warn('bar update', e); }
    const last = S.bars[S.bars.length - 1];
    let at = S.bars.length;
    if (!last || b.seq > last.seq) { S.bars.push(b); if (last) closed = true; }
    else { const i = S.bars.length - 1 - (last.seq - b.seq); if (i >= 0) S.bars[i] = b; if (i < S.bars.length - 1) S.indDirty = true; at = i; }
    if (altSeries?._style === 'ha' && at >= 0) { try { altSeries.update(haPoint(at)); } catch { altSetData(); } }
    S.lastBar = b;
  }
  if (S.cs.plAuto && S.cs.plOpacity < 100) priceLineLayout();   // the bar turned: the line's color with it
  if (S.bars.length > BAR_CAP + 1000) trimBars();
  if (anchorMs() != null) drawAvwap();
  indsUpdate();
  if (closed && S.st.tpo) tpoRequest();
  if (closed && S.st.fp !== 'off' && S.bars.length > 1) {   // every bar the message closed: one print can close several
    for (let i = S.bars.length - 2; i >= 0 && S.bars[i].seq >= was; i--) S.fp.delete(S.bars[i].seq);
    fpSchedule();
  }
  if (S.st.gap && S.gaps) { gapCatchUp(); gapPrim.update(); }
  if (closed && S.st.gap) gapRequest();
  hemaTick(closed);
  dsmaTick(closed);
  if (S.st.ifthen && S.ifRows.length) {   // OnEachTick: the active band follows the price
    ifPrim.update();
    renderIfInfo();
    if (closed && Date.now() - ifFetched > 300_000) ifRequest();   // the daily ATR rolls once a day
  }
  cmpTail();
  if (!S.hovering) legend(S.lastBar);
  header();
}

// A clock where TradingView has it: left of % log auto at the time axis' end, on a phone and a desktop, in the chart's
// time zone, to the second. Click it for the zone (as TradingView's): the time axis, crosshair and clock follow it.
function clockTick() {
  $('#clock-t').textContent = ax.clock.format(Date.now());
  setTimeout(clockTick, 1005 - (Date.now() % 1000));   // just after each second turns
}
clockTick();
const tzOffset = (zone) => {   // "UTC-5", "UTC+5:30" now
  const m = Math.round((wallClock(zone)(Date.now()) - Date.now()) / 60e3), a = Math.abs(m);
  return `UTC${m < 0 ? '-' : '+'}${Math.floor(a / 60)}${a % 60 ? `:${String(a % 60).padStart(2, '0')}` : ''}`;
};
function clockLabel() {
  $('#clock .tz').textContent = ` ${ax.short}`;
  $('#clock').title = `Time zone: ${ax.pick === 'local' ? 'local, ' : ax.pick === 'exchange' ? "the chart's exchange, " : ''}${ax.zone.replace(/_/g, ' ')} (${tzOffset(ax.zone)}). Click to change.`;
}
clockLabel();
function tzMenu(open) {
  const m = $('#tz-menu');
  m.hidden = !open;
  if (!open) return;
  const rows = TZ_CHOICES.map(([v, name]) => {
    const zone = tzZone(v), b = document.createElement('button');
    b.type = 'button';
    b.dataset.v = v;
    b.className = v === ax.pick ? 'on' : '';
    const n = document.createElement('span'), o = document.createElement('small');
    n.textContent = v === 'local' ? `Local (${zone.replace(/_/g, ' ')})` : v === 'exchange' ? `Exchange (${tzCity(zone)})` : name;
    o.textContent = tzOffset(zone);
    b.append(n, o);
    return b;
  });
  m.replaceChildren(...rows);
  const r = $('#clock').getBoundingClientRect();
  Object.assign(m.style, { right: `${Math.max(6, innerWidth - r.right)}px`, bottom: `${innerHeight - r.top + 6}px`, maxHeight: `${Math.max(160, r.top - 12)}px` });
  m.querySelector('.on')?.scrollIntoView({ block: 'nearest' });
}
function setChartZone(v, store = true) {
  if (store) local.set('timeZone', v);   // kept in the workspace: every device and chart frame follows
  axZone(v);
  clockLabel();
  $('#clock-t').textContent = ax.clock.format(Date.now());
  renderAlerts(); renderEvents(); calRender(); zoneTexts(); renderRth(); renderPlanIf(false); drawLevels(); replayUi();   // their times too
  if (anchorMs() != null) drawAvwap();
  if (tapeOpen()) tapeLoad();
  if (journalOpen()) journalLoad();
  drawings.update();
  chart.applyOptions({ timeScale: { tickMarkFormatter: (t, k) => axTick(t, k) }, localization: { timeFormatter: (t) => axTime(t) } });   // new: labels drawn again
  if (!isTime() || !S.bars.length) return;
  // The library weighs the axis' points (where a day starts) only when they change: once more with a blank point
  // before the first bar, then as it was.
  const ts = chart.timeScale(), r = ts.getVisibleLogicalRange(), pts = S.bars.map((b) => toPoints(b)[0]);
  candles.setData([{ time: pts[0].time - 1 }, ...pts]);
  candles.setData(pts);
  if (r) ts.setVisibleLogicalRange(r);
}
// A clock change (DST) here or at the exchange while the page is open: the zone's name and the hours shown in it again.
const zoneShift = () => { const t = Date.now(); return `${ax.zone}|${zs(t)}|${ax.wall(t) - ctWall(t)}`; };
let zoneWas = zoneShift();
setInterval(() => { const z = zoneShift(); if (z !== zoneWas) { zoneWas = z; setChartZone(ax.pick, false); } }, 60e3);
$('#clock').onclick = (e) => { e.stopPropagation(); tzMenu($('#tz-menu').hidden); };
$('#tz-menu').onclick = (e) => {
  const b = e.target.closest('button[data-v]');
  if (!b) return;
  tzMenu(false);
  if (b.dataset.v !== ax.pick) setChartZone(b.dataset.v);
};
document.addEventListener('pointerdown', (e) => { if (!$('#tz-menu').hidden && !e.target.closest('#tz-menu, #clock')) tzMenu(false); }, true);
window.addEventListener('storage', (e) => { if (e.key === 'timeZone' && (e.newValue || 'local') !== ax.pick) setChartZone(e.newValue || 'local', false); });   // another chart frame
function legend(b) {
  const el = $('#legend');
  const sp = `${specLabel(S.spec)}${S.rth && (!S.ext || exch().rth) ? ' RTH' : ''}${S.ext?.source ? (extSeries() ? ` · ${S.ext.source.toUpperCase()}` : S.ext.source === 'alpaca' ? ' · real time' : S.ext.source === 'ibkr' ? (S.ext.feed === 'live' ? ' · IBKR real time' : ' · IBKR delayed') : ' · delayed') : ''}`;
  legendStudies(b);
  el.textContent = `${S.replay ? 'REPLAY  ' : ''}${S.symbol} ${sp}${S.loadNote ? ` · ${S.loadNote}` : ''}`;   // just what the chart is: no O H L C V Δ line
  dataWindow(b);
  cmpLegend();
}
// The data window (Chart settings, or the chart's menu): the bar under the crosshair (else the last): open, high, low,
// close, its change from the bar before, range, volume and delta, under the legend's first line.
const shortNum = (n) => (Math.abs(n) >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : Math.abs(n) >= 1e4 ? `${(n / 1e3).toFixed(1)}K` : String(Math.round(n)));
function dataWindow(b) {
  const el = $('#dwin');
  el.hidden = !S.cs.dataWin || !b;
  if (el.hidden) return;
  const i = S.bars[S.hoverIdx] === b ? S.hoverIdx : barIndexTo(S.bars, b.t), prev = S.bars[i - 1];
  const chg = prev ? b.c - prev.c : null, dir = chg > 0 ? 'up' : chg < 0 ? 'down' : '';
  const cells = [['O', px(b.o)], ['H', px(b.h)], ['L', px(b.l)], ['C', px(b.c), dir],
    ...(chg == null ? [] : [['', `${chg > 0 ? '+' : ''}${px(chg)} (${chg > 0 ? '+' : ''}${(chg / Math.abs(prev.c) * 100).toFixed(2)}%)`, dir]]),
    ['R', px(b.h - b.l)], ...(b.v ? [['V', shortNum(b.v)]] : []), ...(b.d != null && !S.ext ? [['Δ', `${b.d > 0 ? '+' : ''}${shortNum(b.d)}`, b.d > 0 ? 'up' : b.d < 0 ? 'down' : '']] : [])];
  el.replaceChildren(...cells.map(([k, v, c]) => {
    const s = document.createElement('span');
    if (k) { const ki = document.createElement('i'); ki.textContent = k; s.append(ki); }
    const vv = document.createElement('b'); vv.textContent = v; if (c) vv.className = c;
    s.append(vv);
    return s;
  }));
}

// ---- the studies in the legend, as TradingView's: a row each with its values (at the bar under the crosshair).
// Hovering a row (tapping it on a phone) shows its eye and ⚙ settings; double-click opens the settings; right-click
// (a long press on a phone) has Remove; ▴ folds the rows away. ----------------------------------------------------
let lgOpen = local.get('legendOpen', window.innerWidth < 760 ? '0' : '1') === '1', lgSig = '';
function legendRows(b) {
  const at = (vals) => vals[S.hoverIdx >= 0 && S.hoverIdx < vals.length ? S.hoverIdx : vals.length - 1];
  const rows = [];
  for (const it of ST_CAT) {
    if (it.ind || it.cs) continue;   // indicators: a row per one added, below; the price line is the chart's
    if (it.key in S.stHidden) { rows.push({ id: it.id, text: it.name, hidden: true }); continue; }
    if (!stOn(it)) continue;
    let text = it.name, color = '';
    if (it.id === 've') { text = 'VWAP (session)'; if (b?.st?.[0] != null) text += ` ${px(b.st[0])}`; color = '#ffb300'; }
    else if (it.id === 'vr') { text = 'VWAP (RTH)'; if (b?.st?.[2] != null) text += ` ${px(b.st[2])}`; }
    else if (it.id === 'hema') { const v = S.hemaVals.length ? at(S.hemaVals) : NaN; text = `${hemaTitle()}${Number.isFinite(v) ? ` ${px(v)}` : ''}`; color = hemaStyle().color; }
    else if (it.id === 'dsma') {   // each plot's value in its color, as the data box has them
      const p = dsmaPeriods();
      rows.push({ id: it.id, text: 'Tatanka Daily SMA x4', eye: true, parts: [[`Tatanka Daily SMA x4 ${p.join(' ')}`, ''],
        ...S.dsmaVals.map((vals, i) => { const v = at(vals); return [` ${Number.isFinite(v) ? pxTick(v) : '–'}`, dsmaStyle(i).color]; })] });
      continue;
    }
    else if (VP_KINDS.includes(it.id)) { const st = vpBase(it.id); if (st) text = vpTitle(st); }
    else if (TATANKA_SET.includes(it.id)) text = `Tatanka ${it.name.replace(/^Tatanka /, '')}`;
    rows.push({ id: it.id, text, color, eye: !it.anchor });
  }
  S.inds.forEach((x, n) => {
    if (IND_DEFS[x.c.type].pane === true) return;
    const v = at(x.vals) || [], one = LEGACY_PLOTS.has(x.c.type) || IND_DEFS[x.c.type].lines.length < 2;
    if (one || x.c.hidden) rows.push({ id: `ind:${n}`, text: `${indTitle(x.c)} ${x.c.hidden ? '' : indFormat(x.c, v[0])}`, color: x.color, hidden: !!x.c.hidden, eye: true });
    else rows.push({ id: `ind:${n}`, text: indTitle(x.c), eye: true, parts: [[indTitle(x.c), ''], ...indValueParts(x, v).map(([t, c]) => [` ${t}`, c])] });
  });
  return rows;
}
function legendStudies(b) {
  const box = $('#legend-st'), rows = S.ext ? [] : legendRows(b), sig = `${lgOpen}|${rows.map((r) => `${r.id}${r.hidden ? '-' : ''}`).join(',')}`;
  if (sig !== lgSig) {   // which studies changed: the rows again; else only their values
    lgSig = sig;
    const el = (tag, props = {}) => Object.assign(document.createElement(tag), props);
    const kids = lgOpen ? rows.map((r) => {
      const row = el('div', { className: `lg-row${r.hidden ? ' off' : ''}` }), set = el('button', { type: 'button', className: 'lg-b', textContent: '⚙', title: 'Settings' });
      const eye = el('button', { type: 'button', className: 'lg-b lg-eye', title: r.hidden ? 'Show' : 'Hide (stays on the chart\'s list)', innerHTML: EYE_SVG(r.hidden) });
      const t = el('span', { className: 'lg-t' });
      row.dataset.id = r.id;
      row.title = 'Right-click (a long press on a phone) for more, including Remove';
      set.onclick = (e) => { e.stopPropagation(); legendSettings(r.id); };
      eye.onclick = (e) => { e.stopPropagation(); hideStudy(r.id, !r.hidden); };
      row.onclick = () => { if (!matchMedia('(hover: hover)').matches) { const on = !row.classList.contains('sel'); box.querySelectorAll('.lg-row.sel').forEach((x) => x.classList.remove('sel')); row.classList.toggle('sel', on); } };
      row.ondblclick = () => { if (matchMedia('(hover: hover)').matches) legendSettings(r.id); };   // a phone: a quick second tap is not this
      row.append(t, el('span', { className: 'lg-bs' }));
      if (r.eye !== false) row.lastChild.append(eye);
      row.lastChild.append(set);
      return row;
    }) : [];
    if (rows.length) {
      const fold = el('button', { type: 'button', className: 'lg-fold', textContent: lgOpen ? '▴' : `▾ ${rows.length} ${rows.length === 1 ? 'study' : 'studies'}`,
        title: lgOpen ? 'Hide the studies in the legend' : 'Show the studies in the legend' });
      fold.onclick = () => { lgOpen = !lgOpen; local.set('legendOpen', lgOpen ? '1' : '0'); legendStudies(S.lastBar); };
      kids.push(fold);
    }
    box.replaceChildren(...kids);
  }
  if (!lgOpen) return;
  rows.forEach((r, i) => {
    const t = box.children[i]?.firstChild;
    if (!t) return;
    if (r.parts) {   // several values, each in its plot's color
      const sig = JSON.stringify(r.parts);
      if (t.dataset.parts !== sig) {
        t.dataset.parts = sig;
        t.replaceChildren(...r.parts.map(([x, c]) => { const sp = document.createElement('span'); sp.textContent = x; if (c) sp.style.color = c; return sp; }));
      }
      return;
    }
    delete t.dataset.parts;
    if (t.textContent !== r.text) t.textContent = r.text;
    t.style.color = r.color || '';
  });
}
function legendSettings(id) {
  if (id.startsWith('ind:')) { const c = S.inds[Number(id.slice(4))]?.c; if (c) { stOpen = `i:${c.type}`; openSt(true); } }
  else if (IND_ORDER.includes(id) && (id === 'amt' ? S.amtServer.schema : indReady(id))) openInd(id);
  else if (ST_CAT.find((x) => x.id === id)?.panel) { stOpen = id; openSt(true); }
  else { openSt(true); }
  $('#st-list .st-row.open')?.scrollIntoView({ block: 'nearest' });
}
onMenuPress($('#legend-st'), '.lg-row', (row, x, y) => {
  const id = row.dataset.id, off = row.classList.contains('off'), it = ST_CAT.find((s) => s.id === id);
  showMenu(x, y, [
    { label: 'Settings…', fn: () => legendSettings(id) },
    ...(it?.anchor ? [] : [{ label: off ? 'Show' : 'Hide', fn: () => hideStudy(id, !off) }]),
    'sep',
    { label: 'Remove from the chart', fn: () => legendRemove(id) },
  ]);
});
function legendRemove(id) {
  if (id.startsWith('ind:')) { S.indCfg.splice(Number(id.slice(4)), 1); indsChanged(); return; }
  const it = ST_CAT.find((x) => x.id === id);
  if (it?.key in S.stHidden) { delete S.stHidden[it.key]; saveHidden(); }
  if (it) stSet(it, false);
  legendStudies(S.lastBar);
}
// The eye, as TradingView's: a hidden study keeps its row (dimmed) and its settings, and draws (and computes) nothing
// until shown again. Remembered per chart.
function EYE_SVG(off) { return eyeSvg(off); }
const eyeSvg = (off) => `<svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><path d="M2 10s3-6 8-6 8 6 8 6-3 6-8 6-8-6-8-6zM10 8a2 2 0 1 0 0 4 2 2 0 0 0 0-4" fill="none" stroke="currentColor" stroke-width="1.6"/>${off ? '<path d="M3 17L17 3" stroke="currentColor" stroke-width="1.6"/>' : ''}</svg>`;
S.stHidden = (() => { const h = ownTab() ? ownTab().own.hidden : rawLocal('stHidden', {}); return h && typeof h === 'object' ? { ...h } : {}; })();
function saveHidden() { if (!studyStore('hidden', { ...S.stHidden })) local.set('stHidden', JSON.stringify(S.stHidden)); cellPost('state'); }
function hideStudy(id, hide) {
  if (id.startsWith('ind:')) {
    const c = S.indCfg[Number(id.slice(4))];
    if (!c) return;
    if (hide) c.hidden = true; else delete c.hidden;
    indsChanged();
    return;
  }
  const it = ST_CAT.find((x) => x.id === id);
  if (!it || !it.key || replayBlocks(it.key)) return;
  if (hide) {
    S.stHidden[it.key] = S.st[it.key];
    saveHidden();
    S.st[it.key] = it.key in ST_MODES ? 'off' : false;
  } else {
    S.st[it.key] = S.stHidden[it.key] ?? (it.key in ST_MODES ? stModeOf(it.key) : true);
    delete S.stHidden[it.key];
    saveHidden();
  }
  studiesChanged(it.key);
}

chart.subscribeCrosshairMove((p) => {
  const c = p.seriesData?.get(candles), v = p.seriesData?.get(vol);
  S.hovering = !!c;
  const hi = c ? barIndexAt(p.time) : -1;
  if (hi !== S.hoverIdx) { S.hoverIdx = hi; S.inds.forEach((x) => x.label?.update()); }
  legend(c ? (barAt(p.time) || { o: c.open, h: c.high, l: c.low, c: c.close, v: v?.value ?? 0 }) : S.lastBar);
  if (p.sourceEvent !== undefined) S.xhairLinked = false;   // the pointer is on this chart again
  if (!S.xhairLinked) alertPlus(p);   // another chart's crosshair is not where the pointer is: no + or Alt+H price there
  if (TK_CELL !== null && p.sourceEvent !== undefined) cellXhair(c && hi >= 0 ? S.bars[hi].t : null);
});

chart.subscribeClick((p) => {
  if (S.replayPick && p.time != null) {
    const i = barIndexAt(p.time), last = i === S.bars.length - 1;
    const at = S.replay && last ? S.replay.at : i >= 0 ? barEnd(S.bars, i, S.kind, isTime() ? specSeconds(S.spec) * 1000 : 0, daysSpec(S.spec)) : NaN;
    if (!Number.isFinite(at) || at > Date.now()) { toast('Pick a bar that has closed'); return; }
    replayStart(at);
    return;
  }
  if (S.anchoring && p.time != null) {
    const ms = isTime() ? p.time * 1000 : S.seqTime.get(p.time);
    setAnchoring(false);
    if (ms != null) { local.set(anchorKey(), String(ms)); drawAvwap(); applyStudyVisibility(); toast(`VWAP anchored at ${ax.full.format(ms)} ${zs(ms)}`); }
    return;
  }
  if (p.point && !p.paneIndex && !S.placing && drawings.state.tool === 'select' && ifPrim.hit(p.point.x, p.point.y)) { setIfFull(!ifFull()); return; }
  if (!p.point || p.paneIndex || !(S.placing || p.sourceEvent?.altKey)) return;
  const price = candles.coordinateToPrice(p.point.y);
  if (price == null) return;
  setPlacing(false);
  quickAlert(price);
});

// ---- levels ---------------------------------------------------------------------------------
let levelLines = [];
function drawLevels() {
  levelLines.forEach((l) => candles.removePriceLine(l));
  levelLines = [];
  const lv = S.levels[S.symbol], meta = $('#lv-meta');
  meta.classList.remove('stale');
  if (!S.st.lv) { meta.textContent = ''; return; }   // Daily levels off: the AMT Toolkit's levels say where they are from
  if (!lv) { meta.textContent = isExtSym(S.symbol) ? '' : 'no daily levels'; return; }
  const made = lv.created ? `${ax.full.format(Date.parse(lv.created))} ${zs(Date.parse(lv.created))}` : lv.date;   // when they were made, else their day
  meta.textContent = made ? `${lv.label} · ${made}` : lv.label;
  if (!S.st.lv || S.st.amt) return;   // the AMT Toolkit draws the levels itself, with zones
  for (const [name, price] of lv.lines) {
    const color = name === 'P' ? NT.pivot : name[0] === 'R' ? NT.res : NT.sup;   // the AMT Toolkit's level colors
    levelLines.push(candles.createPriceLine({ price, color, lineWidth: name === 'P' ? 2 : 1, lineStyle: L.LineStyle.Solid, axisLabelVisible: true, title: name }));
  }
}

// ---- header -------------------------------------------------------------------------------
function header() {
  // S.lastBar is this symbol's: subscribe() clears another symbol's bars (barsClear) before this one's come
  const last = S.lastBar?.c, q = S.quotes[wlKey(S.symbol, S.instruments)];   // ZM1!'s, not Zoom's; the live ES's: ES1!
  $('#q-last').textContent = px(last);
  const chg = $('#q-chg'), abs = chg.querySelector('.abs'), pct = chg.querySelector('.pct');
  // in replay the live change means nothing; nor does the simulator's invented close beside a free-data chart's real bars
  if (q?.prev && last != null && !S.replay && !(S.ext && q.src === 'sim')) {
    const d = last - q.prev;
    abs.textContent = `${d >= 0 ? '+' : ''}${px(d)}`;
    pct.textContent = `${d >= 0 ? '+' : ''}${(d / Math.abs(q.prev) * 100).toFixed(2)}%`;   // of its size: a rise from below 0 is up
    chg.className = d >= 0 ? 'up' : 'down';
  } else abs.textContent = pct.textContent = '';
}

function buildHeader() {
  renderSymBtn();
  renderTf();
}
function renderSymBtn() {
  const b = $('#sym');
  b.textContent = S.symbol;
  b.title = `${inst().name || S.ext?.name || S.symbol}: search for another symbol (or just type on the chart)`;
}

// ---- timeframe picker: dropdown of grouped presets + custom, starred ones become toolbar buttons ----
function renderTf() {
  const favs = S.favs.slice().sort(bySpec);
  $('#favs').replaceChildren(...favs.map((p) => {
    const b = document.createElement('button');
    b.textContent = specLabel(p);
    b.classList.toggle('on', p === S.spec);
    b.classList.toggle('na', !specOk(p));
    b.onclick = () => setSpec(p);
    return b;
  }));
  $('#tf-btn').textContent = `${specLabel(S.spec)} ▾`;
  $('#tf-btn').classList.toggle('on', !favs.includes(S.spec));
  const groups = [...TF_GROUPS, ...(S.tfCustom.length ? [['Custom', '', S.tfCustom, 'yours; right-click or long-press one to remove it', true]] : [])];
  $('#tf-groups').replaceChildren(...groups.map(([title, unit, sizes, note, custom]) => {   // unit '': sizes are specs
    const g = document.createElement('div');
    g.className = 'tf-group';
    const h = document.createElement('h5');
    h.textContent = title;
    if (note) { const s = document.createElement('small'); s.textContent = ` · ${note}`; h.append(s); }
    const items = document.createElement('div');
    items.className = 'tf-items';
    items.append(...sizes.map((n) => {
      const spec = unit ? `${n}${unit}` : n, fav = S.favs.includes(spec);
      const b = document.createElement('button');
      b.className = 'tf-item';
      b.classList.toggle('on', spec === S.spec);
      b.classList.toggle('na', !specOk(spec));
      b.title = unit === 'r' ? `${spec}: ${(n * inst().tick_size).toFixed(inst().decimals)} pts on ${S.symbol}` : spec;
      const label = document.createElement('span');
      label.textContent = unit === 'k' ? String(n) : custom ? specLabel(spec) : spec;
      const star = document.createElement('span');
      star.className = 'star' + (fav ? ' on' : '');
      star.textContent = fav ? '★' : '☆';
      star.title = fav ? 'Remove from toolbar' : 'Add to toolbar';
      star.onclick = (e) => { e.stopPropagation(); toggleFav(spec); };
      b.append(label, star);
      if (custom) { b.classList.add('custom'); b.dataset.spec = spec; b.title += ' (right-click, or a long press, to remove it)'; }
      b.onclick = () => { setSpec(spec); openTf(false); };
      return b;
    }));
    g.append(h, items);
    return g;
  }));
}
onMenuPress($('#tf-menu'), '.tf-item.custom', (b, x, y) => {
  const spec = b.dataset.spec;
  showMenu(x, y, [{ label: `Remove ${specLabel(spec)} from the menu`, fn: () => {
    S.tfCustom = S.tfCustom.filter((s) => s !== spec);
    local.set('tfCustom', JSON.stringify(S.tfCustom));
    renderTf();
  } }]);
});
function toggleFav(spec) {
  S.favs = S.favs.includes(spec) ? S.favs.filter((s) => s !== spec) : [...S.favs, spec];
  local.set('favs', JSON.stringify(S.favs));
  renderTf();
}
function openTf(open) {
  $('#tf-menu').hidden = !open;
  $('#tf-btn').setAttribute('aria-expanded', String(open));
  if (open) { openSt(false); renderTf(); }
}
$('#tf-btn').onclick = (e) => { e.stopPropagation(); openTf($('#tf-menu').hidden); };
$('#tf-menu').onclick = (e) => e.stopPropagation();
// pointerdown, not click: the chart cancels touch clicks, so a tap on it would not close the menu
document.addEventListener('pointerdown', (e) => {
  if (!$('#tf-menu').hidden && !e.target.closest('#tf, #ctx')) openTf(false);   // a pick in its right-click menu keeps it
  if (!$('#st-menu').hidden && !e.target.closest('#st, #ask-dlg, #ctx')) openSt(false);   // naming a template keeps it open
}, true);
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  openTf(false); openSt(false); setAnchoring(false); setPlacing(false);
  if (S.replayPick) { if (S.replay) replayPicking(false); else replayExit(); }
});
$('#tf-custom').onsubmit = (e) => {
  e.preventDefault();
  const n = parseInt($('#tf-n').value, 10), u = $('#tf-unit').value, max = TF_MAX[u] || 100000;
  if (!(n >= 1 && n <= max)) { toast(`Enter a whole number, 1 to ${max}`); return; }
  const spec = u === 's' && n % 60 === 0 ? `${n / 60}m` : `${n}${u}`;
  if (!TF_GROUPS.some(([, unit, sizes]) => sizes.includes(unit ? Number(spec.slice(0, -1)) : spec) && (!unit || spec.endsWith(unit)))
    && !S.tfCustom.includes(spec)) {   // kept in the menu's Custom group, as TradingView's custom intervals
    S.tfCustom = [...S.tfCustom, spec].sort(bySpec).slice(-30);
    local.set('tfCustom', JSON.stringify(S.tfCustom));
  }
  setSpec(spec);
  openTf(false);
};

// ---- symbols with no live chart here: stocks, ETFs, indices, forex, crypto (Yahoo, delayed) and FRED series ---------
// A watchlist or search symbol as the chart takes it: "ES1!" or "ES" is the live ES chart; a root no stock has ("6E",
// "ZB": the hello's only_futures) is its future, 6E1!, as the server charts and quotes it; any other single symbol
// charts from free data; a formula doesn't chart.
function chartSym(sym) {
  const b = wlBare(wlNorm(String(sym || ''))).trim(), k = wlRoot(b);   // in capitals first: "fred:" is FRED's, not an exchange
  if (S.instruments[k]) return k;
  if (S.onlyFutures?.has(b)) return `${b}1!`;
  if (S.catalog?.[k] && WL_FUT.test(b)) return b;   // RTY1!: added when opened (MET alone stays MetLife)
  if (!b || wlIsSection(b) || wlIsFormula(b) || wlTokens(b).length !== 1 || !WL_SYM.test(b)) return null;
  return b;
}
const isExtSym = (sym) => !S.instruments[sym];
const EXT_SPECS = { fred: ['1D', '1W', '1M'], eia: ['1D', '1W', '1M'], data: ['1D', '1W', '1M'], yahoo: ['1m', '5m', '15m', '30m', '1h', '1D', '1W', '1M'] };
const extSource = (sym) => (sym.startsWith('FRED:') ? 'fred' : sym.startsWith('EIA:') ? 'eia' : sym.startsWith('DATA:') ? 'data' : 'yahoo');
const extSeries = () => wlSeries(S.ext?.source);   // FRED or EIA: a line of values, daily or longer
const specOk = (spec) => !S.ext || ((S.ext.custom ?? extSource(S.symbol) === 'yahoo')   // built from shorter bars
  && ((/^\d+[mh]$/.test(spec) && specSeconds(spec) <= 86400) || calSpec(spec))) || (S.ext.specs || EXT_SPECS[extSource(S.symbol)]).includes(spec);
// Studies drawn from this app's own trades or its futures' settings: not on a free-data chart.
const EXT_OFF = { lv: 1, ve: 1, vr: 1, b1: 1, b2: 1, b3: 1, prof: 1, aavp: 1, pvp: 1, right: 1, fp: 1, fills: 1, big: 1, amt: 1, amtDash: 1,
  ifthen: 1, lines: 1, tpo: 1, gap: 1, hema: 1, dsma: 1 };
function extSpecFor(spec, src) {   // the nearest timeframe the chart's source can build
  const ok = EXT_SPECS[src];
  if (ok.includes(spec)) return spec;
  if (src !== 'yahoo') return '1D';
  if (/^\d+[mh]$/.test(spec) && specSeconds(spec) <= 86400) return spec;   // any minute or hour bars up to a day
  if (calSpec(spec)) return spec;                     // 2D, 2W, 3M: from days, weeks, months
  if (/^\d+s$/.test(spec)) return '1m';
  if (!/^\d+[mh]$/.test(spec)) return '5m';           // tick, volume, range, Renko: time bars
  const ms = specSeconds(spec);
  return ok.filter((x) => /[mh]$/.test(x) && specSeconds(x) <= ms).at(-1) || '1m';
}
let extTimer = 0, extStop = null;
// How long a free-data chart waits for its bars: past the server's longest answer (ext.py ANSWER_S: IBKR 10 s at most,
// then Alpaca, then Yahoo, each with a limit), so a source that hangs still leaves time for the next one's bars.
// Not answered (or the server's error): the chart says so, with none of the bars before, and asks again in a while.
const EXT_WAIT_MS = 60e3, EXT_RETRY_MS = 15e3;
const extEvery = () => (extSeries() ? 1800e3 : calSpec() ? 120e3 : S.ext?.source === 'alpaca' ? 10e3 : S.ext?.source === 'ibkr' ? 15e3 : 30e3);   // newest bars
async function extLoad(poll = false) {
  clearTimeout(extTimer);
  extStop?.abort();   // an answer still on its way: not waited for any more
  const stop = extStop = new AbortController(), sym = S.symbol, spec = S.spec, id = S.subId;
  const mine = () => id === S.subId && sym === S.symbol;   // still this chart (subscribe() since: another symbol, timeframe or session)
  try {
    const d = await api('GET', `/api/ext/bars?symbol=${encodeURIComponent(sym)}&spec=${encodeURIComponent(spec)}&rth=${S.rth ? 1 : 0}`, null,
      { timeout: EXT_WAIT_MS, signal: stop.signal });
    if (!mine() || spec !== S.spec || !S.ext) return;
    // another source took over (IBKR too slow for a while, or back): its bars in full, not joined onto the other's
    const moved = poll && (d.source !== S.ext.source || d.feed !== S.ext.feed);
    Object.assign(S.ext, { source: d.source, feed: d.feed, custom: d.custom, name: d.name, type: d.type, specs: d.specs, decimals: d.decimals, tick: d.tick,
      exchange: d.exchange });
    renderRth();
    if (d.spec !== S.spec) { S.spec = specForced = d.spec; local.set('spec', d.spec); tabSync(); }   // the nearest the source has
    renderTf();
    if (poll && S.bars.length && !moved) extMerge(d.bars);
    else {
      if (!d.bars?.length) throw new Error(`No ${sym} bars from ${d.source || 'the server'}`);   // the server says why instead (400)
      if (!S.viewWant && S.loadedKey === viewKey()) S.viewWant = viewSnap();   // the same chart's bars again: its view stays
      applyPriceFormat();
      if (S.indFmt !== indFmt()) indsBuild();   // the indicators were made before the source's decimals came
      barsLayout();
      vol.applyOptions({ visible: S.cs.volume && !extSeries() });
      renderSymBtn();
      applyHistory({ kind: 'time', bars: d.bars });
    }
  } catch (e) {
    if (!mine()) return;
    if (!poll) {   // no bars, and why: never the bars of what the chart showed before
      if (S.bars.length) barsClear();
      const again = e.status == null || e.status >= 500;   // no answer, or the server failed: asked again in a while
      loadNote(again ? `${e.message}; trying again…` : e.message);
      toast(`${sym}: ${e.message}`);
      if (again) extTimer = setTimeout(() => extLoad(), EXT_RETRY_MS);
      return;
    }
  }
  if (mine()) extTimer = setTimeout(() => extLoad(true), extEvery());
}
function extMerge(bars) {   // a poll's answer: the last bar again, and any newer
  const last = S.bars.at(-1);
  let seq = last.seq;
  const fresh = bars.filter((b) => b.t >= last.t).map((b) => ({ ...b, seq: b.t === last.t ? last.seq : ++seq }));
  if (fresh.length) applyBars({ bars: fresh });
}
let extWall = { tz: '', at: null };
// When the free-data bar opening at t ends. Months and N days run to the next one's 17:00 CT open, as the server
// groups them (N Monday-Friday trading days; calendar days for crypto, which trades every day); others, their length.
// Crypto's days are UTC days: its days, weeks and months end at 00:00 UTC (18:00 or 19:00 CT), past the 17:00 CT open.
function extBarEnd(t) {
  const n = parseInt(S.spec, 10), u = S.spec.slice(-1), x = exch(), utc = x.tz === 'UTC' && !x.rth;
  if (!(utc && calSpec()) && u !== 'M' && !(u === 'D' && n > 1)) return t + specSeconds(S.spec) * 1000;
  const d = new Date(`${tradeDate(t)}T12:00:00Z`);
  if (u === 'M') d.setUTCMonth(d.getUTCMonth() + n, 1);
  else if (utc) d.setUTCDate(d.getUTCDate() + n * (u === 'W' ? 7 : 1));
  else for (let k = 0; k < n;) { d.setUTCDate(d.getUTCDate() + 1); if (d.getUTCDay() % 6) k += 1; }
  if (utc) return Date.parse(`${d.toISOString().slice(0, 10)}T00:00:00Z`);   // the next bar's first day starts
  d.setUTCDate(d.getUTCDate() - 1);   // a day's session opens at 17:00 CT the evening before
  return ctInputToMs(`${d.toISOString().slice(0, 10)}T17:00`);
}
function extQuote(qs) {   // the symbol's quote (the server quotes a charted one) moves the last bar between polls
  if (!['yahoo', 'alpaca', 'ibkr'].includes(S.ext?.source) || !S.bars.length || S.replay) return;
  const q = qs.find((x) => x.s === wlKey(S.symbol, S.instruments)), last = S.bars.at(-1);
  // the simulator's quote (a fresh install's watchlist prices) is invented: it never moves real bars
  if (!q || q.last == null || q.src === 'sim' || q.ts < last.t || q.ts >= extBarEnd(last.t)) return;
  const x = exch();
  if (x.rth && (S.rth || calSpec())) {   // regular hours (and a stock's days): the pre- and post-market's trades stay off
    if (extWall.tz !== x.tz) extWall = { tz: x.tz, at: wallClock(x.tz) };
    const sec = Math.floor(extWall.at(q.ts) / 1000) % 86400, [a, b] = x.rth.map((v) => +v.slice(0, 2) * 3600 + +v.slice(3, 5) * 60);
    if (sec < a || sec > b) return;
  }
  applyBars({ bars: [{ ...last, h: Math.max(last.h, q.last), l: Math.min(last.l, q.last), c: q.last }] });
}

// ---- compare (TradingView's Compare): up to three other symbols as % lines over the chart. The price scale goes to %,
// so each line starts at 0% at the view's left edge. Each takes its symbol's close as of each of the chart's bars, so
// no times of its own reach the axis (a stock over a 24-hour future: flat while it is closed). Live symbols come as a
// subscription of their own, free-data ones from their bars, refreshed. Kept per chart tab. -----------------------------
const CMP_COLORS = ['#ff9800', '#e040fb', '#26c6da'];
const CMP_MAX = 3;
const cmpList = () => S.tabs[S.tab]?.cmp || [];
function cmpAlign(c) {
  const out = [], cb = c.bars;
  let j = -1;
  for (const b of S.bars) {
    while (j + 1 < cb.length && cb[j + 1].t <= b.t) j++;
    if (j >= 0) out.push({ time: timeOf(b), value: cb[j].c });
  }
  return out;
}
function cmpClear() {
  for (const c of cmpLines) { if (c.id) send({ op: 'unsub', id: c.id }); clearTimeout(c.timer); chart.removeSeries(c.series); }
  cmpLines = [];
}
function cmpLoad() {
  cmpClear();
  const list = cmpList();
  if (list.length && isTime() && !S.replay && S.bars.length) {
    if (PS().options().mode !== PSM.Percentage) setScaleMode(PSM.Percentage);
    list.forEach((sym, k) => {
      const color = CMP_COLORS[k % CMP_COLORS.length];
      const series = chart.addSeries(L.LineSeries, { color, lineWidth: 2, priceLineVisible: false, lastValueVisible: true, title: sym.replace(/^(FRED|EIA|DATA):/, '') });
      const c = { sym, color, series, id: 0, bars: [], timer: 0 };
      cmpLines.push(c);
      if (isExtSym(sym)) cmpExt(c);
      else { c.id = 1e6 + ++cmpSeq; send({ op: 'sub', id: c.id, symbol: sym, spec: S.spec, rth: S.rth }); }
    });
  }
  cmpLegend();
}
function cmpHistory(m) {
  const c = cmpLines.find((x) => x.id === m.id);
  if (!c) return m.id > 1e6;   // an old compare's answer: not the chart's either
  c.bars = m.bars;
  c.series.setData(cmpAlign(c));
  cmpLegend();
  return true;
}
function cmpBars(m) {
  const c = cmpLines.find((x) => x.id === m.id);
  if (!c) return;
  for (const b of m.bars) {   // the same bar again (its seq) or a new one
    const last = c.bars.at(-1);
    if (last && b.seq === last.seq) c.bars[c.bars.length - 1] = b; else if (!last || b.t >= last.t) c.bars.push(b);
  }
  cmpTail();
}
function cmpReload(id) {
  const c = cmpLines.find((x) => x.id === id);
  if (c) { c.id = 1e6 + ++cmpSeq; send({ op: 'unsub', id }); send({ op: 'sub', id: c.id, symbol: c.sym, spec: S.spec, rth: S.rth }); }
}
async function cmpExt(c) {
  try {
    const d = await api('GET', `/api/ext/bars?symbol=${encodeURIComponent(c.sym)}&spec=${encodeURIComponent(S.spec)}&rth=${S.rth ? 1 : 0}`);
    if (!cmpLines.includes(c)) return;
    c.bars = d.bars;
    c.series.setData(cmpAlign(c));
    cmpLegend();
  } catch (e) { if (cmpLines.includes(c) && !c.bars.length) toast(`${c.sym}: ${e.message}`); }
  if (cmpLines.includes(c)) c.timer = setTimeout(() => cmpExt(c), calSpec() ? 120e3 : 30e3);
}
function cmpTail() {   // the chart's newest bar: each line's value as of it
  const b = S.bars.at(-1);
  if (!b || !cmpLines.length) return;
  for (const c of cmpLines) {
    const j = barIndexTo(c.bars, b.t);
    if (j >= 0) { try { c.series.update({ time: timeOf(b), value: c.bars[j].c }); } catch { c.series.setData(cmpAlign(c)); } }
  }
  if (!S.hovering) cmpLegend();
}
// Under the legend: each line's change, as the scale shows it (from the view's first bar to the one under the crosshair).
function cmpLegend() {
  const el = $('#cmp-lg');
  el.hidden = !cmpLines.length;
  if (el.hidden) { el.replaceChildren(); return; }
  const r = chart.timeScale().getVisibleLogicalRange(), n = S.bars.length;
  const i0 = Math.max(0, Math.min(n - 1, Math.ceil(r?.from ?? 0))), i1 = S.hovering && S.hoverIdx >= 0 ? S.hoverIdx : n - 1;
  el.replaceChildren(...cmpLines.map((c) => {
    const at = (i) => { const j = barIndexTo(c.bars, S.bars[i]?.t ?? -Infinity); return j >= 0 ? c.bars[j].c : null; };
    const a = at(i0), z = at(i1), pct = a && z != null ? (z / a - 1) * 100 : null;
    const row = document.createElement('span'), dot = document.createElement('i'), v = document.createElement('b'), x = document.createElement('button');
    row.className = 'cmp-row';
    dot.style.background = c.color;
    v.textContent = pct == null ? '…' : `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
    v.className = pct > 0 ? 'up' : pct < 0 ? 'down' : '';
    x.type = 'button';
    x.textContent = '×';
    x.title = `Stop comparing ${c.sym}`;
    x.onclick = () => cmpRemove(c.sym);
    row.append(dot, `${c.sym} `, v, x);
    return row;
  }));
}
function cmpSave(list) {
  const t = S.tabs[S.tab];
  if (!t) return;
  if (list.length && !t.cmp?.length) t.cmpMode = local.get('scaleMode', '');   // the scale before: back to it after
  if (list.length) t.cmp = list; else delete t.cmp;
  saveTabs();
}
function cmpAsk() {   // the ⊕ Compare button and the chart's menu: the symbol search, picking a symbol to compare
  if (!isTime() || S.replay) { toast(S.replay ? 'Leave replay first' : 'Compare works on time charts (minutes, hours, days)'); return; }
  if (cmpList().length >= CMP_MAX) { toast(`${CMP_MAX} symbols at most: remove one (× in the legend)`); return; }
  quickOpen('', true);
}
function cmpAdd(pick) {
  const sym = chartSym(pick);
  if (!sym) { toast(`No chart for ${pick}`); return; }
  if (sym === S.symbol || cmpList().includes(sym)) { toast(`${sym} is on the chart already`); return; }
  if (cmpList().length >= CMP_MAX) return;
  cmpSave([...cmpList(), sym]);
  cmpLoad();
}
$('#cmp-btn').onclick = (e) => { e.stopPropagation(); cmpAsk(); };
function cmpRemove(sym) {
  const t = S.tabs[S.tab], list = cmpList().filter((x) => x !== sym);
  const back = !list.length ? t?.cmpMode ?? '' : null;
  cmpSave(list);
  if (back != null) { delete t.cmpMode; saveTabs(); setScaleMode(back === 'log' ? PSM.Logarithmic : back === 'pct' ? PSM.Percentage : PSM.Normal); }
  cmpLoad();
}

function setSymbol(sym) {
  const c = chartSym(sym);
  if (TK_MULTI) { if (c) workspaces.toActive({ type: 'symbol', sym: c }); return; }   // the chart last clicked
  if (!c || c === S.symbol) return;
  if (S.replay && isExtSym(c)) replayExit();   // replay runs on this app's own trades
  S.symbol = c; local.set('sym', c); renderSymBtn();
  onSymbolChanged();
  subscribe();
  tabSync();
}

// ---- chart tabs ------------------------------------------------------------------------------------------------
function saveTabs() { local.set('chartTabs', JSON.stringify(S.tabs)); local.set('chartTab', String(S.tab)); }
function tabSync() {   // the chart changed symbol or timeframe: so did the open tab
  cellPost('state');
  if (TK_MULTI) return;   // this page's symbol follows the active chart; its tabs are the first chart's
  const t = S.tabs[S.tab];
  if (t && t.sym === S.symbol && t.spec === S.spec) return;
  S.tabs[S.tab] = { ...t, sym: S.symbol, spec: S.spec };   // a renamed tab keeps its name
  saveTabs();
  renderTabs();
}
// As TradingView's tabs: click opens, double-click (or F2) renames, drag moves, right-click (or a long press) has the
// rest; middle-click closes.
function renderTabs() {
  $('#ctabs-list').replaceChildren(...S.tabs.map((t, i) => {
    const tab = document.createElement('div'), nm = document.createElement('span'), b = document.createElement('b');
    const what = `${t.sym} ${specLabel(t.spec)}`;
    tab.className = 'ctab';
    tab.classList.toggle('on', i === S.tab);
    tab.classList.toggle('named', !!t.name);
    tab.classList.toggle('linked', !!t.link);
    tab.dataset.i = i;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(i === S.tab));
    tab.tabIndex = 0;
    tab.title = `${t.name ? `${t.name}: ` : ''}${what}. ${t.link ? 'Linked: the shared indicators. ' : 'Its own indicators. '}${t.drawLocal ? 'New drawings stay on this tab. ' : ''}Double-click to rename, drag to move, right-click to close and more`;
    nm.className = 'nm';
    if (t.name) {
      const sm = document.createElement('small');
      b.textContent = t.name;
      sm.textContent = what;
      nm.append(b, sm);
    } else {
      b.textContent = t.sym;
      nm.append(b, specLabel(t.spec));
    }
    tab.onclick = () => { if (Date.now() > tabDrag.until) openTab(i); };   // not the click that ends a drag or long press
    tab.ondblclick = () => renameTab(i);
    tab.onkeydown = (e) => tabKey(e, i);
    tab.onauxclick = (e) => { if (e.button === 1) { e.preventDefault(); closeTab(i); } };
    tab.oncontextmenu = (e) => { e.preventDefault(); tabMenu(i, e.clientX, e.clientY); };
    tab.onpointerdown = (e) => tabPointer(e, i);
    tab.append(nm);   // no ✕ to hit by mistake: Close is in its menu (right-click, or a long press on a phone)
    return tab;
  }));
  $('#ctab-add').disabled = S.tabs.length >= TABS_MAX;
  $('#ctabs-list .ctab.on')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}
function openTab(i) {
  if (i === S.tab || !S.tabs[i]) return;
  if (S.replay && S.tab >= 0) { toast('Leave replay first'); return; }
  const was = S.tabs[S.tab];
  viewSave(true);   // where the tab was
  S.tab = i;
  S.viewWant = S.views[S.tabs[i].id] || null;
  saveTabs();
  renderTabs();
  if (!(was?.link && S.tabs[i].link)) applyStudyState(ownTab() ? ownTab().own : sharedStudies());   // its indicators
  drawings.select(null);   // and its drawings (the symbol's, and its own)
  drawings.update();
  const t = S.tabs[i], sym = chartSym(t.sym) || S.symbol, rth = applyLook(t.look);   // its chart settings
  if (sym !== S.symbol) {
    if (t.spec !== S.spec) { S.spec = t.spec; local.set('spec', t.spec); renderTf(); }
    setSymbol(sym);
  } else if (t.spec !== S.spec) setSpec(t.spec);
  else if (rth) subscribe();   // only RTH / all hours differs: the bars again
  else { viewApply(S.viewWant); S.viewWant = null; amtFollow(); cmpLoad(); }   // the same bars: its view now (its Toolkit settings, its Compare lines)
  tabSync();
}
// A tab's look on the chart: its chart settings, price bars, scale mode; RTH or all hours set for the next load (true
// when that changed, so the caller loads the bars again).
function applyLook(lk) {
  if (!lk) return false;
  applyCs(lk.cs || {});
  if (!!lk.barsOff !== S.barsOff) setBarsOff(!!lk.barsOff);
  const mode = lk.scale === 'log' ? PSM.Logarithmic : lk.scale === 'pct' ? PSM.Percentage : PSM.Normal;
  if (PS().options().mode !== mode) setScaleMode(mode);
  const fp = fpFrom(lk.fp || {});
  if (JSON.stringify(fp) !== JSON.stringify(S.fpSet)) { S.fpSet = fp; fpSettingsChanged(); renderFpMenu(); }
  if ((lk.amt || '') !== local.get('amtCollapsed', '')) { local.set('amtCollapsed', lk.amt || ''); renderAmtDash(); }
  if (!!lk.rth === S.rth) return false;
  S.rth = !!lk.rth;
  local.set('rthOnly', S.rth ? '1' : '0');
  renderRth();
  return true;
}
const lookNow = () => ({ cs: { ...S.cs }, barsOff: !!S.barsOff, rth: !!S.rth, scale: local.get('scaleMode', ''), fp: { ...S.fpSet },
  amt: local.get('amtCollapsed', '') });
function addTab(copy = S.tab, pick = true) {   // pick: then choose its symbol (Esc keeps the copied one)
  if (S.replay) { toast('Leave replay first'); return; }
  if (S.tabs.length >= TABS_MAX) { toast(`${TABS_MAX} tabs at most`); return; }
  const from = S.tabs[copy] || { sym: S.symbol, spec: S.spec };
  const ind = from.link ? { link: true } : { own: JSON.parse(JSON.stringify(copy === S.tab ? studySnapshot() : from.own || sharedStudies())) };
  const look = JSON.parse(JSON.stringify(copy === S.tab || !from.look ? lookNow() : from.look));   // its chart settings too
  S.tabs.splice(copy + 1, 0, { id: newTabId(), sym: from.sym, spec: from.spec, ...ind, ...(from.drawLocal ? { drawLocal: true } : {}), look,
    ...(from.cmp?.length ? { cmp: [...from.cmp], cmpMode: from.cmpMode || '' } : {}) });
  if (copy === S.tab) {
    S.tab += 1;
    saveTabs();
    renderTabs();
    drawings.select(null);   // a new tab: the one before's own drawings go
    drawings.update();
    if (pick) quickOpen('');
  } else {
    if (copy < S.tab) S.tab += 1;
    saveTabs();
    openTab(copy + 1);
  }
}
function closeTab(i) {
  if (S.tabs.length < 2) return;
  if (S.replay && i === S.tab) { toast('Leave replay first'); return; }   // the next tab's studies would mix with replay's
  const was = S.tab;
  S.tabs.splice(i, 1);
  if (i === was) { S.tab = -1; openTab(Math.min(i, S.tabs.length - 1)); return; }
  if (i < was) S.tab = was - 1;
  saveTabs();
  renderTabs();
}
// Keeps the tabs in `keep` (indexes), in order; the open one stays open if it is kept, else the first kept opens.
function keepTabs(keep) {
  if (S.replay && !keep.includes(S.tab)) { toast('Leave replay first'); return; }
  const open = S.tabs[S.tab];
  S.tabs = keep.map((i) => S.tabs[i]);
  const j = S.tabs.indexOf(open);
  if (j >= 0) { S.tab = j; saveTabs(); renderTabs(); } else { S.tab = -1; openTab(0); }
}
function moveTab(from, to) {
  to = Math.max(0, Math.min(S.tabs.length - 1, to));
  if (from === to || !S.tabs[from]) return;
  const open = S.tabs[S.tab], [t] = S.tabs.splice(from, 1);
  S.tabs.splice(to, 0, t);
  S.tab = S.tabs.indexOf(open);
  saveTabs();
  renderTabs();
  $(`#ctabs-list .ctab[data-i="${to}"]`)?.focus();
}
// In place, as TradingView renames a tab: Enter keeps it, Esc cancels, empty goes back to symbol and timeframe.
function renameTab(i) {
  const tab = $(`#ctabs-list .ctab[data-i="${i}"]`), t = S.tabs[i];
  if (!tab || !t) return;
  const inp = Object.assign(document.createElement('input'), { type: 'text', value: t.name || '', maxLength: TAB_NAME_MAX,
    placeholder: `${t.sym} ${specLabel(t.spec)}`, className: 'ctab-in', spellcheck: false, autocomplete: 'off' });
  inp.setAttribute('aria-label', 'Tab name');
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    if (save) {
      const name = inp.value.trim().slice(0, TAB_NAME_MAX);
      if (name) S.tabs[i] = { ...S.tabs[i], name }; else { const { name: _, ...rest } = S.tabs[i]; S.tabs[i] = rest; }
      saveTabs();
    }
    renderTabs();
  };
  inp.onkeydown = (e) => {
    e.stopPropagation();   // typing a name is not chart shortcuts
    if (e.key === 'Enter') finish(true);
    if (e.key === 'Escape') finish(false);
  };
  inp.onblur = () => finish(true);
  inp.onclick = inp.ondblclick = inp.onpointerdown = (e) => e.stopPropagation();
  tab.querySelector('.nm').replaceWith(inp);
  inp.focus();
  inp.select();
}
// What the chart shows now, as a tab keeps it.
function studySnapshot() {
  return { st: { ...S.st, ...S.replay?.hidden }, inds: S.indCfg.map((c) => ({ ...c })), layers: [...S.layers], hidden: { ...S.stHidden },
    set: JSON.parse(JSON.stringify(tabSet())) };
}
// Shows a tab's indicators (its own, or the shared set): studies on and off, indicators, order, hidden, settings.
function applyStudyState(t) {
  indDlg.open(false);   // its settings were the other tab's
  S.stHidden = t.hidden && typeof t.hidden === 'object' ? { ...t.hidden } : {};
  const st = studiesFrom(t.st);
  for (const k of Object.keys(STUDY_DEFAULTS)) if (S.st[k] !== st[k]) { S.st[k] = st[k]; studiesChanged(k); }
  const inds = (Array.isArray(t.inds) ? t.inds : []).map(indClean).filter(Boolean).slice(0, IND_MAX);
  if (JSON.stringify(inds) !== JSON.stringify(S.indCfg)) { S.indCfg = inds; indsChanged(); }
  setLayers(Array.isArray(t.layers) ? t.layers : []);
  indApply();
  renderStudiesMenu();
  legend(S.lastBar);
}
// Linked: the tab shows the chart's shared indicators (and its changes go there); unlinked: its own, starting from
// what it shows now.
function setTabLink(i, link) {
  const t = S.tabs[i];
  if (!t || !!t.link === link) return;
  if (link) { delete t.own; t.link = true; } else { delete t.link; t.own = JSON.parse(JSON.stringify(i === S.tab ? studySnapshot() : sharedStudies())); }
  saveTabs();
  renderTabs();
  if (i === S.tab) { applyStudyState(link ? sharedStudies() : t.own); amtFollow(); }
  toast(link ? 'This tab shows the shared indicators (with every linked tab)' : 'This tab has its own indicators now');
}
function tabMenu(i, cx, cy) {
  const t = S.tabs[i], n = S.tabs.length;
  if (!t) return;
  showMenu(cx, cy, [
    { label: 'Rename…', fn: () => renameTab(i), key: 'F2' },
    ...(t.name ? [{ label: `Name: back to ${t.sym} ${specLabel(t.spec)}`, fn: () => { const { name: _, ...rest } = S.tabs[i]; S.tabs[i] = rest; saveTabs(); renderTabs(); } }] : []),
    { label: 'Duplicate', fn: () => addTab(i, false), disabled: n >= TABS_MAX },
    'sep',
    { label: 'Own indicators (off: linked to the shared set)', check: !t.link, fn: () => setTabLink(i, !t.link) },
    { label: 'New drawings on this tab only', check: !!t.drawLocal, fn: () => { if (t.drawLocal) delete t.drawLocal; else t.drawLocal = true; saveTabs(); renderTabs(); } },
    'sep',
    { label: 'Move left', fn: () => moveTab(i, i - 1), disabled: i === 0, key: 'Shift+←' },
    { label: 'Move right', fn: () => moveTab(i, i + 1), disabled: i === n - 1, key: 'Shift+→' },
    'sep',
    { label: 'Close', fn: () => closeTab(i), disabled: n < 2, key: 'Del' },
    { label: 'Close other tabs', fn: () => keepTabs([i]), disabled: n < 2 },
    { label: 'Close tabs to the right', fn: () => keepTabs(S.tabs.map((_, j) => j).filter((j) => j <= i)), disabled: i === n - 1 },
  ]);
}
function tabKey(e, i) {
  const go = (j) => $(`#ctabs-list .ctab[data-i="${j}"]`)?.focus();
  const k = e.key, menuKey = k === 'ContextMenu' || (e.shiftKey && k === 'F10');
  if (!['Enter', ' ', 'F2', 'Delete', 'ArrowLeft', 'ArrowRight'].includes(k) && !menuKey) return;
  e.preventDefault();
  e.stopPropagation();   // not the chart's keys (arrows scroll it, Delete removes a drawing)
  if (k === 'Enter' || k === ' ') openTab(i);
  else if (k === 'F2') renameTab(i);
  else if (k === 'Delete') closeTab(i);
  else if (menuKey) { const r = e.currentTarget.getBoundingClientRect(); tabMenu(i, r.left, r.bottom); }
  else {
    const j = i + (k === 'ArrowLeft' ? -1 : 1);
    if (e.shiftKey) moveTab(i, j); else go(Math.max(0, Math.min(S.tabs.length - 1, j)));
  }
}
// Dragging a tab: with a mouse or pen it moves once it has gone 5 px; on a touch screen the bar scrolls instead, and a
// long press opens the tab's menu (Move left / right). Listeners on the document: the tab moves in the page while
// dragged, which would lose a capture on it.
const tabDrag = { moved: false, until: 0 };
function tabPointer(e, i) {
  if (e.target.closest('.x, input') || e.button !== 0) return;
  const tab = e.currentTarget, x0 = e.clientX, y0 = e.clientY;
  tabDrag.moved = false;
  if (e.pointerType === 'touch') {
    const timer = setTimeout(() => { tabDrag.until = Date.now() + 800; tabMenu(i, x0, y0 + 12); }, 550);
    const stop = () => { clearTimeout(timer); document.removeEventListener('pointermove', far); document.removeEventListener('pointerup', stop); document.removeEventListener('pointercancel', stop); };
    const far = (ev) => { if (Math.abs(ev.clientX - x0) > 8 || Math.abs(ev.clientY - y0) > 8) stop(); };
    document.addEventListener('pointermove', far);
    document.addEventListener('pointerup', stop);
    document.addEventListener('pointercancel', stop);
    return;
  }
  const list = $('#ctabs-list'), open = S.tabs[S.tab];
  let order = S.tabs.map((_, j) => j);
  const move = (ev) => {
    if (!tabDrag.moved) {
      if (Math.abs(ev.clientX - x0) < 5) return;
      tabDrag.moved = true;
      tab.classList.add('drag');
      document.body.classList.add('tab-dragging');
    }
    // the tab goes before the first other tab whose middle is right of the pointer
    const others = [...list.children].filter((c) => c !== tab);
    const before = others.find((c) => { const r = c.getBoundingClientRect(); return ev.clientX < r.left + r.width / 2; });
    if (before ? tab.nextSibling !== before : list.lastChild !== tab) list.insertBefore(tab, before || null);
  };
  const up = () => {
    document.removeEventListener('pointermove', move);
    document.removeEventListener('pointerup', up);
    document.removeEventListener('pointercancel', up);
    if (!tabDrag.moved) return;
    tab.classList.remove('drag');
    document.body.classList.remove('tab-dragging');
    order = [...list.children].map((c) => Number(c.dataset.i));
    S.tabs = order.map((j) => S.tabs[j]);
    S.tab = S.tabs.indexOf(open);
    saveTabs();
    renderTabs();
    tabDrag.until = Date.now() + 300;
    tabDrag.moved = false;
  };
  document.addEventListener('pointermove', move);
  document.addEventListener('pointerup', up);
  document.addEventListener('pointercancel', up);
}
$('#ctab-add').onclick = () => addTab();
renderTabs();
function applyPriceFormat() {
  const i = inst(), priceFormat = bondTick(i.symbol) ? { type: 'custom', minMove: i.tick_size, formatter: (p) => bond32(p, i.tick_size) }
    : { type: 'price', precision: i.decimals, minMove: i.tick_size };
  candles.applyOptions({ priceFormat });
  studyLines.forEach((l) => l.applyOptions({ priceFormat }));
  hemaLine.applyOptions({ priceFormat });
  dsmaLines.forEach((l) => l.applyOptions({ priceFormat }));
}
function onSymbolChanged() {
  renderRth();
  applyPriceFormat();
  indsBuild();
  indSets();
  llPrim.update();
  renderPlanLines(false);   // also on each hello: the same symbol's edits stay
  S.lvStatus = null;
  loadPlanLevels();
  S.rangeProfile = null;
  rangePrim.update();
  applyStudyVisibility();
  drawLevels(); alertPrim.update(); watch.markActive();
  renderAmtMenu();
  indDlg.refresh();   // an open Indicators dialog: the symbol's own settings (the AMT Toolkit's alert settings too)
}
function setSpec(spec) {
  if (!SPEC_RE.test(spec)) { toast(`Bad bar type "${spec}". Use 5m, 4h, 1D, 1W, 1M, 2000t, 5000v, 16r`); return; }
  if (TK_MULTI) { workspaces.toActive({ type: 'spec', spec }); return; }
  if (!specOk(spec)) {
    toast(`${S.symbol}: ${extSource(S.symbol) === 'yahoo' ? 'time bars only: minutes and hours up to 24h, days, weeks, months' : `comes in ${EXT_SPECS[extSource(S.symbol)].join(', ')}`}`);
    return;
  }
  if (spec === S.spec) return;
  S.spec = spec; local.set('spec', spec); renderTf(); subscribe(); tabSync();
}
function setPlacing(on) {
  S.placing = on;
  $('#place').classList.toggle('on', on);
  $('#chart-wrap').classList.toggle('placing', on);
}

$('#sym').onclick = () => quickOpen('');
$('#place').onclick = () => setPlacing(!S.placing);
// ETH / RTH: all Globex hours, or the instrument's regular hours only (bars count from the RTH open, as on a
// NinjaTrader chart with an RTH trading-hours template).
function renderRth() {
  const b = $('#rth-btn');
  b.textContent = S.rth ? 'RTH' : 'ETH';
  b.classList.toggle('on', S.rth);
  document.querySelectorAll('#cs-rth button').forEach((x) => x.classList.toggle('on', (x.dataset.v === 'rth') === S.rth));
  b.title = S.rth ? `Regular hours only (${exch().rth ? zoneTimes(exch().rth.join('-'), exch().tz) : 'all hours: no RTH for this symbol'}). Click for all hours.`
    : `All hours${S.ext ? '' : ' (Globex)'}. Click for regular hours only.`;
}
function setRth(on) {
  if (on === S.rth) return;
  S.rth = on;
  local.set('rthOnly', S.rth ? '1' : '0');
  lookStore('rth', on);
  renderRth();
  subscribe();
  cellPost('state');
}
$('#rth-btn').onclick = () => setRth(!S.rth);
renderRth();

// ---- studies menu ------------------------------------------------------------------------
function renderStudiesMenu() {
  const ext = (k) => !!S.ext && k in EXT_OFF;   // drawn from this app's own trades: not on a free-data chart
  document.querySelectorAll('input[data-st]').forEach((el) => {
    el.checked = !!S.st[el.dataset.st];
    el.disabled = (!!S.replay && el.dataset.st in REPLAY_OFF) || ext(el.dataset.st);
  });
  document.querySelectorAll('#st-menu .seg').forEach((seg) => seg.querySelectorAll('button')
    .forEach((b) => { b.classList.toggle('on', b.dataset.v === S.st[seg.dataset.st]); b.disabled = ext(seg.dataset.st); }));
  $('#st-ext').hidden = !S.ext;
  stRender();
}
function studiesChanged(key) {
  if (key in ST_MODES && S.st[key] !== 'off') local.set(`stMode.${key}`, S.st[key]);   // what it turns back on with
  if (key in S.stHidden && S.st[key] && S.st[key] !== 'off') { delete S.stHidden[key]; saveHidden(); }
  if (TK_MULTI) { workspaces.toActive({ type: 'study', key, on: S.st[key] }); return; }   // the active chart's
  cellPost('state');
  const st = S.replay ? { ...S.st, ...S.replay.hidden } : S.st;
  if (!studyStore('st', { ...st })) local.set('studies', JSON.stringify(st));
  renderStudiesMenu();
  if (key === 'lv') drawLevels();
  if (key === 'econ') { if (S.st.econ && !S.cal.at) calLoad(); econPrim.update(); }
  if (key === 'right') fetchRange();
  if (key === 'fp') { fpLayout(); fpSchedule(0); }
  if (key === 'amt') {
    amtLayout();
    if (S.st.amt) amtRequest(); else { send({ op: 'amt_off', id: S.subId }); S.amt = null; }
    drawLevels();
    renderAmtMenu();
  }
  if (key === 'amt' || key === 'amtDash') { renderAmtDash(); amtPrim.update(); }
  if (key === 'tpo') tpoRequest(true);
  if (VP_KINDS.includes(key)) vpRequest(key, true);
  if (key === 'gap') { gapRequest(true); renderGapMenu(); }
  if (key === 'ifthen') { amtLayout(); if (S.st.ifthen) ifRequest(); renderIfInfo(); ifPrim.update(); }
  if (key === 'hema') { if (S.st.hema) hemaRequest(); else { S.hema = null; hemaDraw(); } }
  if (key === 'dsma') { amtLayout(); if (S.st.dsma) dsmaRequest(); else { S.dsma = null; dsmaDraw(); } }
  if (key === 'lines') { llPrim.update(); renderLlInfo(); }
  if (key === 'big') { tapeSync(); bigSchedule(0); bigPrim.update(); }
  if (key === 'fills') fillsSchedule(0);
  if (TATANKA_SET.includes(key)) indDlg.refresh();
  if (key === 'amt' || key === 'amtDash') gapPrim.update();   // the gap table moves below the AMT dashboard
  applyStudyVisibility();
  legend(S.lastBar);
  rangePrim.update();
}
const replayBlocks = (k) => { if (!S.replay || !(k in REPLAY_OFF)) return false; toast('Not during replay: it is drawn from later data'); renderStudiesMenu(); return true; };
document.querySelectorAll('input[data-st]').forEach((el) => {   // the Studies menu and the Plan tab
  el.onchange = () => { if (replayBlocks(el.dataset.st)) return; S.st[el.dataset.st] = el.checked; studiesChanged(el.dataset.st); };
});
document.querySelectorAll('#st-menu .seg button').forEach((b) => {
  b.onclick = () => { if (replayBlocks(b.parentElement.dataset.st)) return; S.st[b.parentElement.dataset.st] = b.dataset.v; studiesChanged(b.parentElement.dataset.st); };
});
function openSt(open) {
  $('#st-menu').hidden = !open;
  $('#st-btn').setAttribute('aria-expanded', String(open));
  if (open) {
    openTf(false);
    $('#st-q').value = '';
    renderStudiesMenu(); renderTatankaMenu(); renderFpMenu(); renderBigMenu();
    if (matchMedia('(hover: hover)').matches) $('#st-q').focus();   // not on a phone: no keyboard popping up
  }
}

// ---- the Studies menu: a search, one row per study, and a study's settings only once it is opened -----------------
const ST_MODES = { right: 'visible', fp: 'bidask' };   // studies with modes: the one they turn on with
const ST_GROUPS = ['Tatanka', 'Price and levels', 'Volume', 'Moving averages', 'Bands and channels', 'Oscillators', 'Trend and volatility', 'Other'];
const ST_CAT = [
  { id: 'amt', name: 'Tatanka AMT Toolkit', group: 'Tatanka', key: 'amt', panel: 'amt', kw: 'auction market theory volume profile dashboard ib npoc levels' },
  { id: 'ifthen', name: 'If/Then Bias Table', group: 'Tatanka', key: 'ifthen', panel: 'ifthen', kw: 'bias plan' },
  { id: 'lines', name: 'Level Lines', group: 'Tatanka', key: 'lines', panel: 'lines', kw: 'range custom lines' },
  { id: 'gap', name: 'Gap Analysis', group: 'Tatanka', key: 'gap', panel: 'gap', kw: 'gaps fill' },
  { id: 'hema', name: 'HTF EMA', group: 'Tatanka', key: 'hema', panel: 'hema', kw: 'higher timeframe moving average' },
  { id: 'dsma', name: 'Daily SMA x4', group: 'Tatanka', key: 'dsma', panel: 'dsma', kw: 'daily simple moving averages 8 21 50 200 sma' },
  { id: 'tpo', name: 'TPO Profile', group: 'Tatanka', key: 'tpo', panel: 'tpo', kw: 'market profile letters single prints' },
  { id: 'aavp', name: 'Auto Anchored Volume Profile', group: 'Tatanka', key: 'aavp', panel: 'aavp', kw: 'aavp vp poc value area year quarter month week anchor' },
  { id: 'pvp', name: 'Periodic Volume Profile', group: 'Tatanka', key: 'pvp', panel: 'pvp', kw: 'pvp vp poc value area daily weekly monthly naked touched' },
  { id: 'prof', name: 'Session Volume Profile', group: 'Tatanka', key: 'prof', panel: 'prof', kw: 'svp vp poc value area session rth' },
  { id: 'pline', name: 'Price line', group: 'Price and levels', cs: 'priceLine', kw: 'last price' },
  { id: 'fills', name: 'Your fills', group: 'Price and levels', key: 'fills', panel: 'fills', kw: 'executions trades journal' },
  { id: 'lv', name: 'Daily levels', group: 'Price and levels', key: 'lv', kw: 'support resistance pivot plan file link paste' },
  { id: 'econ', name: 'Economic events', group: 'Price and levels', key: 'econ', panel: 'econ', kw: 'calendar news cpi fomc payrolls' },
  { id: 've', name: 'VWAP (session)', group: 'Volume', key: 've', panel: 'vwap', kw: 'volume weighted average price bands' },
  { id: 'vr', name: 'VWAP (RTH)', group: 'Volume', key: 'vr', panel: 'vwap', kw: 'volume weighted average price bands regular hours' },
  { id: 'avwap', name: 'Anchored VWAP', group: 'Volume', anchor: true, panel: 'avwap', kw: 'volume weighted average price' },
  { id: 'right', name: 'Volume profile (right edge)', group: 'Volume', key: 'right', panel: 'right', kw: 'visible range vrvp composite poc' },
  { id: 'fp', name: 'Footprint', group: 'Volume', key: 'fp', panel: 'fp', kw: 'order flow bid ask delta imbalance' },
  { id: 'big', name: 'Big trades', group: 'Volume', key: 'big', panel: 'big', kw: 'bubbles large orders block' },
  ...Object.entries(IND_DEFS).map(([t, d]) => ({ id: `i:${t}`, name: d.long || d.name, group: d.group, ind: t, panel: 'ind', kw: d.kw || '' })),
].map((it, k) => [it, k]).sort((a, b) => ST_GROUPS.indexOf(a[0].group) - ST_GROUPS.indexOf(b[0].group) || a[1] - b[1]).map(([it]) => it);   // each group together
let stOpen = '';   // the row whose settings show
const stModeOf = (k) => local.get(`stMode.${k}`, ST_MODES[k]);
function stOn(it) {
  if (it.cs) return !!S.cs[it.cs];
  if (it.anchor) return !!local.get(anchorKey(), '');
  if (it.ind) return S.indCfg.some((c) => c.type === it.ind);
  return it.key in ST_MODES ? !!S.st[it.key] && S.st[it.key] !== 'off' : !!S.st[it.key];
}
function stBlocked(it) {   // why it can't be switched now, or ''
  if (S.ext && (it.key in EXT_OFF || it.anchor)) return "Needs this app's own futures trades";
  if (S.replay && it.key in REPLAY_OFF) return 'Not during replay: drawn from later data';
  return '';
}
function stSet(it, on) {
  if (it.cs) { S.cs[it.cs] = on; csChanged(it.cs); stRender(); return; }
  if (it.anchor) {
    if (on) { openSt(false); setPlacing(false); setAnchoring(true); toast('Tap the bar to anchor VWAP at'); }
    else { local.set(anchorKey(), ''); drawAvwap(); applyStudyVisibility(); stRender(); }
    return;
  }
  if (it.ind) {
    if (on) indAdd(it.ind); else { S.indCfg = S.indCfg.filter((c) => c.type !== it.ind); indsChanged(); }
    return;
  }
  if (replayBlocks(it.key)) return;
  S.st[it.key] = it.key in ST_MODES ? (on ? stModeOf(it.key) : 'off') : on;
  studiesChanged(it.key);
}
function stRow(it, layer = false) {
  const el = (tag, props = {}) => Object.assign(document.createElement(tag), props);
  const on = stOn(it), why = stBlocked(it), open = !!it.panel && stOpen === it.id;
  const row = el('div', { className: `st-row${on ? ' on' : ''}${open ? ' open' : ''}` });
  row.dataset.id = it.id;
  if (layer) { row.dataset.layer = it.id; row.append(layerGrip(it.id, it.name)); }
  const box = el('input', { type: 'checkbox', checked: on, disabled: !!why, title: why || (on ? 'On the chart' : 'Add to the chart') });
  box.setAttribute('aria-label', `${it.name} on the chart`);
  box.onchange = () => stSet(it, box.checked);
  const name = el('button', { type: 'button', className: 'st-name', title: why || (it.panel ? 'Settings' : '') });
  name.append(el('span', { textContent: it.name }));
  if (it.panel) name.append(el('span', { className: 'st-chev', textContent: open ? '▾' : '›' }));
  name.onclick = () => {
    if (!it.panel) { if (!why) stSet(it, !on); return; }
    stOpen = open ? '' : it.id;
    renderStudiesMenu();
  };
  row.append(box, name);
  if (open) {
    const p = document.querySelector(`#st-panels .stp[data-p="${it.panel}"]`);
    if (p) {
      if (it.ind) {
        p.querySelector('.ind-eds').replaceChildren(...indEditors(it.ind));
        const more = p.querySelector('.ind-more');
        more.textContent = stOn(it) ? '+ Add another' : '+ Add';
        more.disabled = S.indCfg.length >= IND_MAX;
        more.onclick = () => indAdd(it.ind);
      }
      row.append(p);
    }
  }
  return row;
}
function stRender() {
  const list = $('#st-list');
  if (!list) return;
  document.querySelectorAll('#st-list .stp').forEach((p) => $('#st-panels').append(p));   // back to the store first
  const words = $('#st-q').value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const h = (text) => Object.assign(document.createElement('h5'), { textContent: text });
  const out = [];
  if (words.length) {
    const q = words.join(' ');
    const rank = (it) => { const n = it.name.toLowerCase(); return n.startsWith(q) ? 0 : n.split(/[\s(/]+/).some((w) => w.startsWith(words[0])) ? 1 : n.includes(q) ? 2 : 3; };
    const hits = ST_CAT.filter((it) => { const t = `${it.name} ${it.group} ${it.kw || ''}`.toLowerCase(); return words.every((w) => t.includes(w)); })
      .map((it, i) => [rank(it), i, it]).sort((a, b) => a[0] - b[0] || a[1] - b[1]).map((x) => x[2]);
    out.push(...(hits.length ? hits.map((it) => stRow(it)) : [Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Nothing by that name.' })]));
  } else {
    const on = ST_CAT.filter(stOn), drawn = S.layersReady ? layerPlan(S.layers).order : [];
    out.push(h('On this chart'));
    if (!on.length) out.push(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Nothing yet: tick one below.' }));
    else if (drawn.length > 1) {   // the layers, front first, to drag; then the rest (drawn with the bars, or in a pane of their own)
      const box = Object.assign(document.createElement('div'), { id: 'st-layers' });
      box.append(...drawn.map((id) => (id === 'bars' ? barsRow() : stRow(ST_CAT.find((x) => x.id === id), true))));
      out.push(box, ...on.filter((it) => !drawn.includes(it.id)).map((it) => stRow(it)),
        Object.assign(document.createElement('p'), { className: 'hint st-lhint', textContent: 'Drag ⋮⋮ to reorder: higher draws in front. Drawings and alerts are always in front.' }));
    } else out.push(...on.map((it) => stRow(it)));
    let group = '';
    for (const it of ST_CAT.filter((x) => !stOn(x))) {
      if (it.group !== group) { group = it.group; out.push(h(group)); }
      out.push(stRow(it));
    }
  }
  list.replaceChildren(...out);
}
function barsRow() {
  const el = (tag, props = {}) => Object.assign(document.createElement(tag), props);
  const row = el('div', { className: 'st-row on st-bars', title: 'The candles or bars, with volume, footprint, price line and daily levels' });
  row.dataset.layer = 'bars';
  row.append(layerGrip('bars', 'the price bars'), el('span', { className: 'st-nobox' }), el('span', { className: 'st-name', textContent: 'Price bars' }));
  return row;
}
// Drag the ⋮⋮ (mouse or finger), or press it and use the arrow keys: the row moves, and the chart takes the new order.
function layerGrip(id, name) {
  const g = Object.assign(document.createElement('button'), { type: 'button', className: 'st-grip', textContent: '⋮⋮',
    title: 'Drag up to draw in front, down to draw behind (or the arrow keys)' });
  g.setAttribute('aria-label', `Move ${name} in front or behind`);
  const shown = () => [...($('#st-layers')?.children || [])].map((r) => r.dataset.layer);
  g.onkeydown = (e) => {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    e.preventDefault();
    e.stopPropagation();
    const ids = shown(), i = ids.indexOf(id), j = i + (e.key === 'ArrowUp' ? -1 : 1);
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    layerMove(ids, id);
    $(`#st-layers [data-layer="${id}"] .st-grip`)?.focus();
  };
  g.onpointerdown = (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const row = g.closest('.st-row'), box = row.parentElement, menu = $('#st-menu'), pid = e.pointerId;
    row.classList.add('drag');
    const move = (ev) => {   // on the page, not the grip: moving its row ends a pointer capture
      if (ev.pointerId !== pid) return;
      const m = menu.getBoundingClientRect();   // near an edge of the menu: it scrolls
      if (ev.clientY < m.top + 28) menu.scrollTop -= 8; else if (ev.clientY > m.bottom - 28) menu.scrollTop += 8;
      for (const r of box.children) {   // past the middle of another row: the two swap
        const b = r.getBoundingClientRect();
        if (r === row || ev.clientY < b.top || ev.clientY > b.bottom) continue;
        box.insertBefore(row, ev.clientY < b.top + b.height / 2 ? r : r.nextSibling);
        break;
      }
    };
    const end = (ev) => {
      if (ev.pointerId !== pid) return;
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', end);
      document.removeEventListener('pointercancel', end);
      row.classList.remove('drag');
      layerMove(shown(), id);
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', end);
    document.addEventListener('pointercancel', end);
  };
  return g;
}
// The rows shown, in their new order. A study that is off stays right behind the shown one it was behind.
function layerMove(ids, moved) {
  const was = layerPlan(S.layers).order;
  if (ids.length !== was.length || ids.every((id, i) => was[i] === id)) { stRender(); return; }   // the menu changed under it: nothing moves
  const behind = new Map();
  let lead = '';
  for (const id of S.layers) {
    if (ids.includes(id)) lead = id;
    else behind.set(lead, [...(behind.get(lead) || []), id]);
  }
  setLayers([...(behind.get('') || []), ...ids.flatMap((id) => [id, ...(behind.get(id) || [])])]);
  const now = layerPlan(S.layers).order;
  if (now.indexOf(moved) !== ids.indexOf(moved)) toast(`${ST_CAT.find((x) => x.id === moved)?.name} can't draw there: the list shows where it draws`);
  else if (now.join() !== ids.join()) toast('Behind the bars, line studies (VWAP, EMA) draw in front of the others');
}
$('#st-q').addEventListener('input', () => { stOpen = ''; stRender(); });
$('#st-q').addEventListener('keydown', (e) => {
  e.stopPropagation();   // typing here is not chart shortcuts
  if (e.key === 'Escape') { if (e.target.value) { e.target.value = ''; stRender(); } else openSt(false); }
  if (e.key === 'Enter') {   // the first match: on, with its settings open
    const first = $('#st-list .st-row');
    const it = first && ST_CAT.find((x) => x.id === first.dataset.id);
    if (!it) return;
    if (!stOn(it) && !stBlocked(it)) stSet(it, true);
    if (it.panel) { stOpen = it.id; renderStudiesMenu(); }
  }
});
// ---- templates: indicator templates (which studies and indicators are on, in what order: Studies ▾) and chart
// templates (the look: ⚙ Chart). Each under a name, saved on the server, the same on every device. ------------------
const indTplData = () => ({ studies: Object.fromEntries(Object.entries({ ...S.st, ...S.replay?.hidden }).filter(([k]) => k !== '_preset')),   // replay hides some: saved as they will be
  inds: S.indCfg.map((c) => ({ ...c })), layers: [...S.layers] });
function applyIndTpl(t) {
  if (S.replay) { toast('Leave replay first'); return false; }
  const st = { ...STUDY_DEFAULTS, ...(t.studies || {}), _preset: ST_PRESET };
  if (typeof st.prof === 'string') st.prof = st.prof !== 'off';
  for (const k of Object.keys(st)) {
    if (k === '_preset' || !(k in STUDY_DEFAULTS) || S.st[k] === st[k]) continue;
    S.st[k] = st[k];
    studiesChanged(k);
  }
  if (Array.isArray(t.inds)) { S.indCfg = t.inds.map(indClean).filter(Boolean).slice(0, IND_MAX); indsChanged(); }
  if (Array.isArray(t.layers)) setLayers(t.layers);
  renderStudiesMenu();
  return true;
}
function applyCs(o) {   // chart settings: only what differs is redrawn
  const cs = csFrom(o || {});
  for (const k of Object.keys(CS_DEFAULTS)) if (S.cs[k] !== cs[k]) { S.cs[k] = cs[k]; csChanged(k); }
}
const TPL_UI = {
  indicators: { list: '#st-tpl-list', box: '#st-tpl', btn: '#st-tpl-btn', save: '#st-tpl-save', label: 'Indicator templates',
    title: 'Save these indicators as a template', data: indTplData, apply: applyIndTpl },
  chart: { list: '#cs-tpl-list', box: '#cs-tpl', btn: '#cs-tpl-btn', save: '#cs-tpl-save', label: 'Chart templates',
    title: 'Save these chart settings as a template', data: () => ({ cs: { ...S.cs } }), apply: (t) => { applyCs(t.cs); return true; } },
};
function renderTpl(kind) {
  const u = TPL_UI[kind], box = $(u.list);
  if (!box) return;
  const el = (tag, props) => Object.assign(document.createElement(tag), props), names = templates.names(kind);
  box.replaceChildren(...(!names.length && kind === 'chart' ? [el('p', { className: 'tpl-none', textContent: 'None saved yet.' })] : []),
    ...names.map((name) => {
      const row = el('div', { className: 'tpl-row' });
      row.dataset.name = name;
      row.append(el('button', { type: 'button', className: 'tpl-use', textContent: name, title: 'Apply to this chart (right-click, or a long press, to delete)',
        onclick: () => tplApply(kind, name) }));
      return row;
    }));
}
function tplApply(kind, name) {
  if (TPL_UI[kind].apply(templates.data(kind, name) || {})) toast(`${TPL_UI[kind].label.slice(0, -1)} "${name}" applied`);
}
for (const [kind, u] of Object.entries(TPL_UI)) {   // a template's menu: Apply, Delete
  onMenuPress($(u.list), '.tpl-row', (row, x, y) => {
    const name = row.dataset.name;
    showMenu(x, y, [
      { label: `Apply "${name}"`, fn: () => tplApply(kind, name) },
      'sep',
      { label: 'Delete…', fn: async () => {
        if ((await ask({ title: `Delete the template "${name}"?`, input: false, ok: 'Delete', danger: true })) === null) return;
        try { await templates.del(kind, name); } catch (e) { fail(e); }
      } },
    ]);
  });
}
function renderChartTpl() { renderTpl('indicators'); renderTpl('chart'); }   // the list changed (any device)
for (const [kind, u] of Object.entries(TPL_UI)) {
  $(u.btn).onclick = () => {
    const box = $(u.box), open = box.hidden;
    box.hidden = !open;
    $(u.btn).setAttribute('aria-expanded', String(open));
    $(u.btn).lastChild.textContent = ` ${u.label} ${open ? '▴' : '▾'}`;
    if (open) { renderTpl(kind); box.scrollIntoView({ block: 'nearest' }); }
  };
  $(u.save).onclick = async () => {
    const name = (await ask({ title: u.title, value: '', ok: 'Save', placeholder: kind === 'chart' ? 'e.g. Dark, no grid' : 'e.g. Scalping 2m',
      names: templates.names(kind) }))?.trim().slice(0, 40);
    if (!name) return;
    try { await templates.save(kind, name, u.data()); toast(`Template "${name}" saved on every device`); } catch (e) { fail(e); }
  };
}
$('#st-nt').onclick = () => {   // exactly what the NinjaTrader chart set shows
  if (S.replay) { toast('Leave replay first'); return; }
  const keys = Object.keys(STUDY_DEFAULTS);
  Object.assign(S.st, STUDY_DEFAULTS);
  keys.forEach((k) => studiesChanged(k));
  if (!S.cs.priceLine) { S.cs.priceLine = true; csChanged('priceLine'); }
  if (S.indCfg.length) { S.indCfg = []; indsChanged(); }   // the NinjaTrader chart set has no EMA, RSI, ...
  stOpen = '';
  renderStudiesMenu();
  toast('Studies match the NinjaTrader chart');
};
function setAnchoring(on) {
  S.anchoring = on;
  $('#chart-wrap').classList.toggle('anchoring', on);
}
$('#st-btn').onclick = (e) => { e.stopPropagation(); openSt($('#st-menu').hidden); };
$('#st-menu').onclick = (e) => e.stopPropagation();
$('#av-set').onclick = () => { openSt(false); setPlacing(false); setAnchoring(true); toast('Tap the bar to anchor VWAP at'); };
$('#av-clear').onclick = () => { local.set(anchorKey(), ''); drawAvwap(); applyStudyVisibility(); };
renderStudiesMenu();
indsBuild();
applyStudyVisibility();
amtLayout();
fpLayout();


// ---- alerts ---------------------------------------------------------------------------------
// timeout (ms): a request still unanswered then is given up (one on a dead connection can wait minutes for TCP).
async function api(method, path, body, { timeout, signal } = {}) {   // signal: the caller drops the request
  const stop = timeout || signal ? new AbortController() : null;
  const timer = timeout && setTimeout(() => stop.abort(new Error(`No answer from the server in ${timeout / 1000} s`)), timeout);
  const drop = () => stop.abort(signal.reason);
  signal?.addEventListener('abort', drop);
  try {
    const r = await fetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined, signal: stop?.signal });
    if (r.status === 401 && path !== '/api/auth/login') authCheck();   // signed out, or a request that crossed a login change
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw Object.assign(new Error(typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail ?? r.statusText)), { status: r.status });
    }
    return r.status === 204 ? null : await r.json();
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', drop);
  }
}
const fail = (e) => toast(e.message);

// Alerts work as on TradingView: a Create alert dialog (the button, Alt+A, the chart's right-click menu or the +
// by the price axis), "Add alert" for a one-click crossing alert, alert lines dragged to a new price, and a list
// that shows each alert's state.
const OPS = { cross: 'Crossing', above: 'Crossing up', below: 'Crossing down', gt: 'Greater than', lt: 'Less than', enter: 'Entering channel', exit: 'Exiting channel' };
const CHANNEL = new Set(['enter', 'exit']);
const symTick = (sym) => S.instruments[sym]?.tick_size || inst().tick_size;
const symDec = (sym) => S.instruments[sym]?.decimals ?? 2;
const snap = (p, sym = S.symbol) => +(Math.round(p / symTick(sym)) * symTick(sym)).toFixed(symDec(sym));
const fmtSym = (sym, p) => (bondTick(sym) ? bond32(p, symTick(sym)) : Number(p).toFixed(symDec(sym)));
const num = (v) => parse32(v);   // 110'165 (32nds) as well as 7450.25

function quickAlert(price) {   // "Add alert": price crossing this level, once
  if (S.ext) { toast('Alerts run on the live futures charts'); return Promise.resolve(); }
  const p = snap(price);
  return api('POST', '/api/alerts', { symbol: S.symbol, kind: 'price', price: p, direction: 'cross' })
    .then(() => toast(`Alert: ${S.symbol} crossing ${px(p)}`)).catch(fail);
}

// Study alerts: price touching a VWAP (or its sigma band) or a session profile's POC, VAH or VAL.
function studyName(p) {
  if (p.study === 'vwap' || p.study === 'rth_vwap') {
    return `${p.study === 'vwap' ? 'session VWAP' : 'RTH VWAP'}${p.band ? ` ${p.band > 0 ? '+' : '−'}${Math.abs(p.band)}σ` : ''}`;
  }
  return `${p.which === 'previous' ? 'previous' : 'developing'} ${(p.profile || 'eth').toUpperCase()} ${p.study.toUpperCase()}`;
}

// [condition, details] for the list.
// Indicator alert series, as the server names them (ind_alerts.line_name): "Bollinger(14,2) Upper", "EMA(21)".
function indLineName(d) {
  if ('value' in d) return String(d.value);
  if (d.ind === 'price') return 'price';
  const x = indLineNew(d), def = IND_DEFS[x.ind];
  if (!def) return d.ind;
  const label = def.lines.find(([k]) => k === x.line)?.[1] || x.line, args = def.params.map(([k]) => x[k]).join(',');
  return `${def.name}${args ? `(${args})` : ''}${label === def.name ? '' : ` ${label}`}`;
}
// A series as saved before there were more indicators ({ind: ema|rsi_avg|macd_diff..., len...}) as {ind, line, settings}.
function indLineNew(d) {
  if (!d || 'line' in d || 'value' in d || d.ind === 'price') return d;
  if (d.ind === 'ema' || d.ind === 'sma') return { ind: d.ind, line: 'v', len: d.len };
  if (d.ind.startsWith('rsi')) return { ind: 'rsi', line: d.ind === 'rsi' ? 'rsi' : 'avg', len: d.len, smooth: d.smooth ?? 3 };
  return { ind: 'macd', line: { macd: 'macd', macd_avg: 'avg', macd_diff: 'diff' }[d.ind], fast: d.fast ?? 12, slow: d.slow ?? 26, smooth: d.smooth ?? 9 };
}
function describe(a) {
  const p = a.params, f = (v) => fmtSym(a.symbol, v), tol = p.tolerance_ticks ? ` ±${p.tolerance_ticks}t` : '';
  let what;
  if (a.kind === 'ind') what = `${indLineName(p.a)} ${OPS[p.direction] || p.direction} ${indLineName(p.b)} on ${p.spec}${p.rth ? ' RTH' : ''}${p.on === 'close' ? ' bar close' : ''}`;
  else if (a.kind === 'price') what = CHANNEL.has(p.direction) ? `${OPS[p.direction]} ${f(p.price)} – ${f(p.price2)}` : `${OPS[p.direction] || p.direction} ${f(p.price)}`;
  else if (a.kind === 'study') what = `touches ${studyName(p)}${tol}`;
  else if (a.kind === 'line') {
    const kind = { hline: 'line', hray: 'ray', trend: 'trend line' }[p.type] || 'line';
    const now = drawingLinePrice(p, Date.now(), S.holidays, S.instruments[a.symbol]?.calendar || 'equity');   // the server's level
    what = `${OPS[p.direction] || p.direction} drawn ${kind}${now != null ? ` (now ${f(now)})` : ''}`;
  } else what = `touches ${p.levels === 'all' ? 'Daily levels' : p.levels.join(', ')}${tol}`;
  const details = [a.repeat ? (a.kind === 'ind' ? 'Once a bar' : 'Every time') : 'Only once', p.expires ? `until ${ax.full.format(p.expires)} ${zs(p.expires)}` : 'open-ended', a.note].filter(Boolean);
  return [`${a.symbol} ${what}`, details.join(' · ')];
}
function alertState(a) {
  if (a.enabled) return ['Active', 'on'];
  if (a.params.expires && a.params.expires <= Date.now()) return ['Expired', ''];
  if (a.last_fired && !a.repeat) return ['Triggered', 'fired'];
  return ['Paused', ''];
}

function renderAlerts() {
  $('#alert-count').textContent = S.alerts.filter((a) => a.enabled).length || '';
  const list = [...S.alerts].sort((a, b) => b.enabled - a.enabled || b.id - a.id);   // active first, newest first
  $('#alert-list').replaceChildren(...list.map((a) => {
    const li = document.createElement('li');
    li.classList.toggle('off', !a.enabled);
    const [cond, details] = describe(a), [state, cls] = alertState(a);
    const what = document.createElement('span'), b = document.createElement('b'), sm = document.createElement('small');
    what.className = 'what';
    b.textContent = cond;
    sm.textContent = details;
    what.append(b, sm);
    const st = document.createElement('span');
    st.className = `state ${cls}`;
    st.textContent = state;
    const tog = document.createElement('button');
    tog.textContent = a.enabled ? 'Pause' : 'Restart';
    tog.onclick = (e) => { e.stopPropagation(); api('PATCH', `/api/alerts/${a.id}`, { enabled: !a.enabled }).catch(fail); };
    li.dataset.id = a.id;
    li.title = `${a.kind === 'line' ? 'Set on a drawn line: select the line to change it' : 'Edit'}. Right-click (a long press on a phone) for more, including Delete`;
    li.onclick = () => {
      if (a.kind !== 'line') { openAlertDialog({ alert: a }); return; }
      setSymbol(a.symbol);
      toast('This alert belongs to a drawn line: click the line to change it');
    };
    li.append(what, st, tog);
    return li;
  }));
  $('#al-clear').hidden = !S.alerts.some((a) => !a.enabled && a.kind !== 'line');
  alertPrim.update();
}
onMenuPress($('#alert-list'), 'li', (li, x, y) => {
  const a = S.alerts.find((q) => String(q.id) === li.dataset.id);
  if (!a) return;
  showMenu(x, y, [
    ...(a.kind === 'line' ? [] : [{ label: 'Edit…', fn: () => openAlertDialog({ alert: a }) }]),
    { label: a.enabled ? 'Pause' : 'Restart', fn: () => api('PATCH', `/api/alerts/${a.id}`, { enabled: !a.enabled }).catch(fail) },
    'sep',
    { label: 'Delete', fn: () => api('DELETE', `/api/alerts/${a.id}`).catch(fail) },
  ]);
}, 'button');
$('#al-new').onclick = () => openAlertDialog({ price: S.lastBar?.c });
$('#al-clear').onclick = async () => {
  const off = S.alerts.filter((a) => !a.enabled && a.kind !== 'line');   // a drawn line's alert goes with the line
  if (!off.length || (await ask({ title: `Delete ${off.length} stopped alert${off.length > 1 ? 's' : ''}?`, input: false, ok: 'Delete', danger: true })) === null) return;
  for (const a of off) await api('DELETE', `/api/alerts/${a.id}`).catch(fail);
};

// ---- the Create / Edit alert dialog ---------------------------------------------------------------------------
const dlg = { edit: null, sym: null, repeat: false };
function setTrig(repeat) {
  dlg.repeat = repeat;
  $('#al-trig').querySelectorAll('button').forEach((b) => b.classList.toggle('on', (b.dataset.v === 'repeat') === repeat));
  $('#al-trig-hint').textContent = !repeat ? 'Fires once, then stops.'
    : $('#al-src').value === 'price' ? 'Fires each time the condition is met, at most once a minute.'
      : $('#al-src').value.startsWith('i:') ? 'Fires each bar the condition is met: on its close, or on the first trade that meets it (Intrabar).'
        : 'Fires on each touch, again after price has moved 8 ticks away.';
}
function syncDlg() {
  const src = $('#al-src').value, ind = src.startsWith('i:'), price = src === 'price', chan = price && CHANNEL.has($('#al-op').value);
  $('#al-op-row').hidden = !price && !ind;
  $('#al-v1-row').hidden = !price;
  [...$('#al-op').options].forEach((o) => { o.hidden = ind && CHANNEL.has(o.value); });
  if (ind && CHANNEL.has($('#al-op').value)) $('#al-op').value = 'cross';
  $('#al-i-row').hidden = $('#al-vs-row').hidden = $('#al-on-row').hidden = !ind;
  if (ind) indDlgSync();
  $('#al-v2-row').hidden = !chan;
  $('#al-v1-label').textContent = chan ? 'Upper bound' : 'Value';
  $('#al-band-row').hidden = !src.includes('vwap');
  $('#al-prof-row').hidden = !['poc', 'vah', 'val'].includes(src);
  $('#al-tol-row').hidden = price || ind;
  if (chan && !$('#al-v2').value) {   // a channel starts 20 ticks tall, under the value
    const v = num($('#al-v1').value);
    if (Number.isFinite(v)) $('#al-v2').value = fmtSym(dlg.sym, v - 20 * symTick(dlg.sym));
  }
  setTrig(dlg.repeat);
}
function openAlertDialog({ price, alert } = {}) {
  closeCtx();
  if (!alert && S.ext) { toast('Alerts run on the live futures charts'); return; }
  const a = alert || null, p = a?.params || {}, chan = a?.kind === 'price' && CHANNEL.has(p.direction);
  dlg.edit = a;
  dlg.sym = a?.symbol || S.symbol;
  // a new one starts at the last price: the chart's, or the quote's where there are no bars (the page around a layout's charts)
  const at = a?.kind === 'price' ? (chan ? p.price2 : p.price) : price ?? (dlg.sym === S.symbol && S.lastBar ? S.lastBar.c : S.quotes[wlKey(dlg.sym, S.instruments)]?.last);
  $('#al-title').textContent = `${a ? 'Edit' : 'Create'} alert on ${dlg.sym}`;
  $('#al-src').value = !a || a.kind === 'price' ? 'price' : a.kind === 'level' ? 'level' : a.kind === 'ind' ? `i:${indBase(p.a.ind)}` : p.study;
  indDlgFill(a?.kind === 'ind' ? p : null);
  $('#al-op').value = a?.kind === 'price' || a?.kind === 'ind' ? p.direction : 'cross';
  $('#al-v1').value = at != null ? fmtSym(dlg.sym, snap(at, dlg.sym)) : '';
  $('#al-v2').value = chan ? fmtSym(dlg.sym, p.price) : '';
  $('#al-band').value = String(p.band ?? 0);
  $('#al-prof').value = p.profile || 'eth';
  $('#al-which').value = p.which || 'current';
  $('#al-tol').value = p.tolerance_ticks ?? 0;
  const exp = p.expires > Date.now() ? p.expires : 0;
  $('#al-exp').value = ax.input.toInput(exp || Date.now() + 30 * 86400e3);   // offered when "Until…" is picked: a month out
  $('#al-exp-tz').textContent = ax.short;   // on the chart's time zone's clock
  setExp(!!exp);   // open-ended unless it has a date
  $('#al-msg').value = a?.note || '';
  dlg.repeat = a ? a.repeat : false;
  $('#al-del').hidden = !a;
  $('#al-ok').textContent = a ? 'Save' : 'Create';
  syncDlg();
  $('#al-dlg').hidden = false;
  if (a) $('#al-ok').focus(); else { $('#al-v1').focus(); $('#al-v1').select(); }
}
function closeAlertDialog() { $('#al-dlg').hidden = true; dlg.edit = null; }

// The dialog as an API body, or null (and a toast) when something is missing.
function dlgBody() {
  const src = $('#al-src').value;
  const b = { symbol: dlg.sym, repeat: dlg.repeat, note: $('#al-msg').value.trim() };
  if ($('#al-exp-on').checked) {
    const ms = ax.input.toMs($('#al-exp').value);
    if (ms == null || ms <= Date.now()) { toast(`Pick an expiration in the future (${ax.short})`); return null; }
    b.expires = ms;
  }
  if (src.startsWith('i:')) return indDlgBody(b, src.slice(2));
  if (src === 'price') {
    const v1 = num($('#al-v1').value);
    if (!Number.isFinite(v1)) { toast('Enter a price'); $('#al-v1').focus(); return null; }
    Object.assign(b, { kind: 'price', price: v1, direction: $('#al-op').value });
    if (CHANNEL.has(b.direction)) {
      const v2 = num($('#al-v2').value);
      if (!Number.isFinite(v2) || snap(v2, dlg.sym) === snap(v1, dlg.sym)) { toast('A channel needs two different prices'); $('#al-v2').focus(); return null; }
      b.price2 = v2;
    }
    return b;
  }
  const tol = parseInt($('#al-tol').value, 10);
  b.tolerance_ticks = Number.isFinite(tol) ? Math.max(0, Math.min(200, tol)) : 0;
  if (src === 'level') return { ...b, kind: 'level' };
  Object.assign(b, { kind: 'study', study: src });
  if (src.includes('vwap')) b.band = Number($('#al-band').value);
  else Object.assign(b, { profile: $('#al-prof').value, which: $('#al-which').value });
  return b;
}
// Can the edit be a PATCH, or is it a different alert (another study, band or profile, another indicator condition)?
function sameAlert(a, b) {
  if (a.kind !== b.kind) return false;
  const p = a.params;
  if (b.kind === 'ind') {   // only its trigger, message or expiry changed: kept, with the bar it last fired in (not sent again)
    const was = { ...p, a: indLineNew(p.a), b: indLineNew(p.b) };   // a series as saved before there were more, as the dialog shows it
    delete was.expires;
    return same(was, { a: b.a, b: b.b, spec: b.spec, on: b.on, direction: b.direction, ...(b.rth ? { rth: true } : {}) });
  }
  if (b.kind !== 'study') return true;
  return p.study === b.study && (b.study.includes('vwap') ? (p.band ?? 0) === b.band : p.profile === b.profile && p.which === b.which);
}
$('#al-form').onsubmit = async (e) => {
  e.preventDefault();
  if ($('#al-ok').disabled) return;   // still saving: a second tap or Enter would make a second alert
  const b = dlgBody(), a = dlg.edit;
  if (!b) return;
  $('#al-ok').disabled = true;
  try {
    if (!a) {
      const r = await api('POST', '/api/alerts', b);
      toast(`Alert created: ${describe(r)[0]}`);
    } else if (sameAlert(a, b)) {   // saving restarts a stopped alert, as on TradingView
      const patch = { enabled: true, repeat: b.repeat, note: b.note, expires: b.expires || 0 };
      if (b.kind === 'price') Object.assign(patch, { price: b.price, direction: b.direction, price2: b.price2 });
      else patch.tolerance_ticks = b.tolerance_ticks;
      await api('PATCH', `/api/alerts/${a.id}`, patch);
      toast('Alert saved');
    } else {
      await api('POST', '/api/alerts', b);
      await api('DELETE', `/api/alerts/${a.id}`);
      toast('Alert saved');
    }
    closeAlertDialog();
  } catch (err) { fail(err); } finally { $('#al-ok').disabled = false; }
};
$('#al-src').onchange = () => {
  const src = $('#al-src').value;
  if (!dlg.edit) dlg.repeat = src !== 'price';   // touches re-arm; indicator alerts fire once a bar
  if (src.startsWith('i:')) indDlgDefaults(src.slice(2));
  syncDlg();
};
// ---- indicator alerts in the dialog: an EMA, SMA, RSI or MACD line on a timeframe, against the price, a value or
// another line; checked on bar close or intrabar (ind_alerts.py) ----
const IND_SPECS = ['1m', '2m', '3m', '5m', '10m', '15m', '30m', '1h', '2h', '4h', '1D'];
const indBase = (k) => (k.startsWith('macd') ? 'macd' : k.startsWith('rsi') ? 'rsi' : k);
$('#al-ispec').replaceChildren(...IND_SPECS.map((s) => new Option(s, s)));
{   // every indicator with lines, by group, after the price and the studies
  const groups = new Map();
  for (const [t, d] of Object.entries(IND_DEFS)) if (d.lines.length) (groups.get(d.group) || groups.set(d.group, []).get(d.group)).push(new Option(d.long || d.name, `i:${t}`));
  for (const [g, opts] of groups) { const og = document.createElement('optgroup'); og.label = `Indicators: ${g}`; og.append(...opts); $('#al-src').append(og); }
}
dlg.on = 'close';
function setOn(v) { dlg.on = v; $('#al-on').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === v)); }
$('#al-on').onclick = (e) => { const b = e.target.closest('button'); if (b) setOn(b.dataset.v); };
// What a line starts against, as TradingView's: an oscillator its overbought level, MACD and Stochastics their signal
// line, the rest the price.
const IND_AGAINST = { rsi: 70, stoch: 'l:d', stochrsi: 0.8, willr: -25, cci: 100, mom: 0, roc: 0, adx: 25, atr: null, obv: null, mfi: 80,
  volma: null, rvol: 2, macd: 'l:avg' };
// The settings, line and what it crosses, for one indicator: fields built from its settings (ta.js IND_DEFS).
function indDlgBuild(kind, x = {}) {
  const def = IND_DEFS[kind], el = (tag, props = {}) => Object.assign(document.createElement(tag), props);
  $('#al-iparams').replaceChildren(...def.params.map(([k, label, d, lo, hi, o]) => {
    if (o?.choices) { const s = el('select', { id: `al-ip-${k}`, title: label }); s.append(...o.choices.map((c) => new Option(c, c))); s.value = x[k] ?? d; s.setAttribute('aria-label', label); return s; }
    const i = el('input', { id: `al-ip-${k}`, type: 'number', min: lo, max: hi, step: o?.step || 1, value: x[k] ?? d, title: label, style: 'width:60px' });
    i.setAttribute('aria-label', label);
    return i;
  }));
  $('#al-iline').replaceChildren(...def.lines.map(([k, label]) => new Option(label, k)));
  $('#al-iline').value = x.line || def.lines[0][0];
  $('#al-iline').hidden = def.lines.length < 2;
  const vs = [['price', 'Price'], ['value', 'Value'], ['ema', 'EMA'], ['sma', 'SMA'], ...(def.lines.length > 1 ? def.lines.map(([k, label]) => [`l:${k}`, label]) : [])];
  $('#al-vs').replaceChildren(...vs.map(([v, t]) => new Option(t, v)));
}
function indDlgDefaults(kind) {   // TradingView-like starting points
  indDlgBuild(kind);
  $('#al-ispec').value = IND_SPECS.includes(S.spec) ? S.spec : '5m';
  const def = IND_DEFS[kind], against = kind in IND_AGAINST ? IND_AGAINST[kind] : 'price';
  $('#al-vs').value = typeof against === 'string' ? against : 'value';
  $('#al-vsv').value = typeof against === 'number' ? against : '';
  if (def.lines.length > 1 && $('#al-vs').value === `l:${$('#al-iline').value}`) $('#al-vs').value = 'price';
}
function indDlgFill(p) {
  if (!p) { setOn('close'); return; }
  const a = indLineNew(p.a), b = indLineNew(p.b);
  indDlgBuild(a.ind, a);
  $('#al-ispec').value = p.spec;
  const sameRun = b.ind === a.ind && b.line !== a.line && IND_DEFS[a.ind].params.every(([k]) => b[k] === a[k]);   // another line of it
  $('#al-vs').value = 'value' in b ? 'value' : b.ind === 'price' ? 'price' : sameRun ? `l:${b.line}` : b.ind;   // else an EMA or SMA
  $('#al-vsv').value = 'value' in b ? b.value : b.len || '';
  $('#al-op').value = p.direction;
  setOn(p.on);
}
function indDlgSync() {
  const vs = $('#al-vs').value;
  $('#al-vsv').hidden = vs !== 'value' && vs !== 'ema' && vs !== 'sma';
  $('#al-vsv').placeholder = vs === 'value' ? 'value' : 'length';
  setOn(dlg.on);
}
$('#al-vs').onchange = () => {
  const vs = $('#al-vs').value;
  if (vs === 'ema' || vs === 'sma') $('#al-vsv').value = vs === 'ema' ? 50 : 200;
  else if (vs === 'value') $('#al-vsv').value = '';
  indDlgSync();
};
function indDlgBody(b, kind) {
  const def = IND_DEFS[kind], a = { ind: kind, line: $('#al-iline').value };
  for (const [k, label, , lo, hi, o] of def.params) {
    const el = $(`#al-ip-${k}`);
    if (o?.choices) { a[k] = el.value; continue; }
    const step = o?.step || 1, v = Number(el.value);
    if (el.value === '' || !(v >= lo && v <= hi) || (step === 1 && !Number.isInteger(v))) { toast(`${label}: ${step === 1 ? 'a whole number' : 'a number'} ${lo} to ${hi}`); el.focus(); return null; }
    a[k] = Number((Math.round(v / step) * step).toFixed(indDecimals(step)));
  }
  if (kind === 'macd' && a.fast >= a.slow) { toast('MACD: fast shorter than slow'); return null; }
  const vs = $('#al-vs').value, v = $('#al-vsv').value;
  let other;
  if (vs === 'price') other = { ind: 'price' };
  else if (vs.startsWith('l:')) {
    other = { ...a, line: vs.slice(2) };
    if (other.line === a.line) { toast('Pick another line, the price or a value'); return null; }
  } else if (vs === 'value') { const n = num(v); if (!Number.isFinite(n)) { toast('Enter the value it crosses'); $('#al-vsv').focus(); return null; } other = { value: n }; }
  else { const n = Number(v); if (!Number.isInteger(n) || n < 1 || n > 1000) { toast('Length: a whole number'); $('#al-vsv').focus(); return null; } other = { ind: vs, line: 'v', len: n }; }
  // on the bars the chart shows: regular hours only when it does (an edited alert keeps its own)
  const rth = dlg.edit?.kind === 'ind' ? !!dlg.edit.params.rth : S.rth && !!S.instruments[b.symbol]?.rth;
  return { ...b, kind: 'ind', a, b: other, spec: $('#al-ispec').value, on: dlg.on, direction: $('#al-op').value, rth };
}
$('#al-op').onchange = syncDlg;
function setExp(on) {
  $('#al-exp-on').checked = on;
  $('#al-exp-row').hidden = !on;
  $('#al-expseg').querySelectorAll('button').forEach((b) => b.classList.toggle('on', (b.dataset.v === 'date') === on));
}
$('#al-expseg').onclick = (e) => { const b = e.target.closest('button'); if (b) setExp(b.dataset.v === 'date'); };
$('#al-exp').oninput = () => setExp(true);
$('#al-trig').onclick = (e) => { const b = e.target.closest('button'); if (b) setTrig(b.dataset.v === 'repeat'); };
$('#al-del').onclick = () => { const a = dlg.edit; if (a) api('DELETE', `/api/alerts/${a.id}`).then(closeAlertDialog).catch(fail); };
$('#al-close').onclick = $('#al-cancel').onclick = closeAlertDialog;
$('#al-dlg').addEventListener('pointerdown', (e) => { if (e.target === e.currentTarget) closeAlertDialog(); });
$('#al-dlg').addEventListener('keydown', (e) => {   // keys typed here are not chart shortcuts
  e.stopPropagation();
  if (e.key === 'Escape') closeAlertDialog();
});

// ---- menus and small dialogs ------------------------------------------------------------------------------
// A menu at (cx, cy). items: { label, fn, key, check, disabled }, 'sep', or an element. Opened from a button
// (owner), a second click on it closes it.
let ctxOwner = null;
function closeCtx() { $('#ctx').hidden = true; ctxOwner = null; plusXhairOff(); }
function showMenu(cx, cy, items, owner = null) {
  const m = $('#ctx');
  if (owner && ctxOwner === owner && !m.hidden) { closeCtx(); return; }
  m.replaceChildren(...items.map((it) => {
    if (it === 'sep') return document.createElement('hr');
    if (it instanceof Element) return it;
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    if (it.check != null) { const c = document.createElement('span'); c.className = 'ck'; c.textContent = it.check ? '✓' : ''; b.append(c); }
    b.append(it.label);
    b.disabled = !!it.disabled;
    if (it.key) { const k = document.createElement('kbd'); k.textContent = it.key; b.append(k); }
    b.onclick = () => { closeCtx(); it.fn(); };
    return b;
  }));
  m.hidden = false;
  ctxOwner = owner;
  const r = m.getBoundingClientRect();
  m.style.left = `${Math.max(4, Math.min(cx, innerWidth - r.width - 4))}px`;
  m.style.top = `${Math.max(4, Math.min(cy, innerHeight - r.height - 4))}px`;
}
// A name, or a yes/no (input: false): resolves to the text ('' for yes), or null when cancelled. names: the saved
// ones (templates, presets): a dropdown under the name that narrows as you type (arrows, then Enter or Tab, take one);
// a name already saved turns OK into Replace. maxLength: how long the text can be; optional: an empty one is an answer.
function ask({ title, value = '', ok = 'OK', message = '', input = true, danger = false, placeholder = '', names = null, type = 'text',
  maxLength = names ? 40 : 60, optional = false }) {
  return new Promise((resolve) => {
    const dlg = $('#ask-dlg'), inp = $('#ask-in'), form = $('#ask-form'), list = $('#ask-list'), drop = $('#ask-drop'), hint = $('#ask-hint');
    $('#ask-title').textContent = title;
    $('#ask-msg').textContent = message;
    $('#ask-msg').hidden = !message;
    $('#ask-combo').hidden = !input;
    inp.type = type;   // 'datetime-local' for Go to date
    inp.value = value;
    inp.placeholder = placeholder;
    inp.maxLength = maxLength;   // by default what a template's name keeps
    $('#ask-ok').classList.toggle('danger', danger);
    const saved = input && names ? [...names].sort((a, b) => a.localeCompare(b)) : [];
    let open = false, all = false, hi = -1;   // all: the ▾ list, every saved one
    const matches = () => { const q = inp.value.trim().toLowerCase(); return q && !all ? saved.filter((n) => n.toLowerCase().includes(q)) : saved; };
    const render = () => {
      const items = matches(), q = inp.value.trim(), same = saved.includes(q);
      hi = Math.min(hi, items.length - 1);
      list.hidden = !open || !items.length;
      list.replaceChildren(...items.map((n, i) => {
        const b = document.createElement('button'), at = q && !all ? n.toLowerCase().indexOf(q.toLowerCase()) : -1;
        b.type = 'button';
        b.className = `ask-opt${i === hi ? ' on' : ''}`;
        b.setAttribute('role', 'option');
        b.setAttribute('aria-selected', String(i === hi));
        if (at >= 0) b.append(n.slice(0, at), Object.assign(document.createElement('mark'), { textContent: n.slice(at, at + q.length) }), n.slice(at + q.length));
        else b.textContent = n;
        b.onpointerdown = (e) => e.preventDefault();   // keeps the keyboard up
        b.onclick = () => pick(n);
        return b;
      }));
      drop.hidden = !saved.length;
      drop.setAttribute('aria-expanded', String(!list.hidden));
      $('#ask-ok').textContent = same ? 'Replace' : ok;
      hint.hidden = !same;
      hint.textContent = same ? `"${q}" is saved already: this replaces it.` : '';
    };
    const pick = (n) => { inp.value = n; open = all = false; hi = -1; render(); };
    inp.oninput = () => { open = true; all = false; hi = -1; render(); };
    inp.onkeydown = (e) => {
      const items = matches();
      if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && items.length) {
        e.preventDefault();
        if (!open) { open = true; hi = -1; }
        hi = e.key === 'ArrowDown' ? (hi + 1) % items.length : hi <= 0 ? items.length - 1 : hi - 1;
        render();
        list.children[hi]?.scrollIntoView({ block: 'nearest' });
      } else if ((e.key === 'Enter' || e.key === 'Tab') && !list.hidden && hi >= 0) {
        e.preventDefault();
        pick(items[hi]);
      } else if (e.key === 'Escape' && !list.hidden) {
        e.preventDefault();
        e.stopPropagation();   // the list closes, not the dialog
        open = false;
        render();
      }
    };
    drop.onpointerdown = (e) => e.preventDefault();
    drop.onclick = () => { open = all = list.hidden; hi = -1; render(); };
    open = saved.length > 0;   // the saved ones show at once
    render();
    const done = (v) => {
      dlg.hidden = true; form.onsubmit = null; inp.oninput = inp.onkeydown = null; list.hidden = true;
      if (dlg.contains(document.activeElement)) document.activeElement.blur();
      resolve(v);
    };
    form.onsubmit = (e) => {
      e.preventDefault();
      const v = inp.value.trim();
      if (input && !v && !optional) { inp.focus(); return; }
      done(input ? v : '');
    };
    $('#ask-cancel').onclick = () => done(null);
    dlg.onpointerdown = (e) => { if (e.target === dlg) done(null); };
    dlg.onkeydown = (e) => { e.stopPropagation(); if (e.key === 'Escape') done(null); };
    dlg.hidden = false;
    if (input) { inp.focus(); inp.select(); } else $('#ask-ok').focus();
  });
}

// ---- the chart's menu: right-click (or the + by the price axis) ----------------------------------------------
function openCtx(cx, cy, price) {
  const p = snap(price), t = px(p);
  showMenu(cx, cy, [
    { label: `Add alert at ${t}`, fn: () => quickAlert(p) },
    { label: `Create alert at ${t}…`, fn: () => openAlertDialog({ price: p }), key: 'Alt+A' },
    'sep',
    { label: `Horizontal line at ${t}`, fn: () => drawings.addHline(p) },
    ...(navigator.clipboard ? [{ label: `Copy price ${t}`, fn: () => navigator.clipboard.writeText(t).then(() => toast(`Copied ${t}`), fail) }] : []),
    'sep',
    { label: 'Reset chart view', fn: resetView, key: 'Alt+R' },
    { label: 'Go to date…', fn: goToDate, key: 'Alt+G' },
    { label: 'Compare symbol…', fn: cmpAsk },
    { label: 'Save picture', fn: savePicture, key: 'Alt+S' },
    ...postItems('Post chart to Discord…'),
    { label: S.barsOff ? 'Show price bars' : 'Hide price bars', fn: () => setBarsOff(!S.barsOff) },
    { label: S.cs.dataWin ? 'Hide data window' : 'Show data window', fn: () => { S.cs.dataWin = !S.cs.dataWin; csChanged('dataWin'); } },
    ...(calSpec() ? [] : [{ label: S.cs.sessions ? 'Hide session breaks' : 'Show session breaks', fn: () => { S.cs.sessions = !S.cs.sessions; csChanged('sessions'); } }]),
    ...(drawings.state.list.size ? [{ label: drawings.state.hidden ? 'Show drawings' : 'Hide drawings', fn: () => $('#draw-bar [data-act=hide]').click() },
      { label: `Object tree (${drawings.state.list.size} drawing${drawings.state.list.size > 1 ? 's' : ''})…`, fn: () => drawings.showTree(true) }] : []),
    'sep',
    { label: 'Studies…', fn: () => openSt(true), key: '/' },
    { label: 'Chart settings…', fn: () => openCs(true) },
    { label: 'Keyboard shortcuts', fn: () => showKeys(true), key: '?' },
  ]);
}
// The price axis' own menu, as TradingView's: the scale modes, the lock, and scaling on price only.
function openScaleCtx(cx, cy) {
  const m = PS().options().mode, auto = PS().options().autoScale && !S.lock;
  showMenu(cx, cy, [
    { label: 'Auto (fits the data)', check: auto, fn: () => setAuto(!auto), key: 'dbl-click' },
    { label: 'Lock price to bar ratio', check: !!S.lock, fn: () => $('#ps-lock').click() },
    { label: 'Scale price chart only', check: !!S.cs.priceOnly, fn: () => { S.cs.priceOnly = !S.cs.priceOnly; csChanged('priceOnly'); } },
    'sep',
    { label: 'Regular', check: m === PSM.Normal, fn: () => setScaleMode(PSM.Normal) },
    { label: 'Percent', check: m === PSM.Percentage, fn: () => setScaleMode(PSM.Percentage) },
    { label: 'Logarithmic', check: m === PSM.Logarithmic, fn: () => setScaleMode(PSM.Logarithmic) },
  ]);
}
function showKeys(on) {
  $('#keys-dlg').hidden = !on;
  if (on) $('#keys-close').focus(); else document.activeElement?.blur();   // keys go to the chart again
}
$('#keys-close').onclick = () => showKeys(false);
$('#keys-dlg').onpointerdown = (e) => { if (e.target.id === 'keys-dlg') showKeys(false); };
$('#keys-dlg').onkeydown = (e) => { e.stopPropagation(); if (e.key === 'Escape') showKeys(false); };
// A drag on the chart that goes clearly up or down, with Auto on: TradingView moves the chart and turns Auto off; the
// library alone ignores it (it only pans prices with Auto off). Not a long press first (that is the crosshair), a pinch,
// or a drag a drawing has; a sideways swipe's wobble doesn't count.
{
  const el = $('#chart');
  let g = null;
  el.addEventListener('pointerdown', (e) => {
    g = null;
    if (!e.isPrimary || (e.pointerType === 'mouse' && e.button !== 0)) return;
    const r = el.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    if (x > r.width - PS().width() || y > chart.paneSize(0).height) return;   // the axes and the panes below have their own
    g = { id: e.pointerId, x: e.clientX, y: e.clientY, t: performance.now(), moved: false, on: false };
  }, true);
  window.addEventListener('pointerdown', (e) => { if (g && e.pointerId !== g.id) g = null; }, true);   // a second finger
  window.addEventListener('pointermove', (e) => {
    if (!g || e.pointerId !== g.id) return;
    const dx = e.clientX - g.x, dy = e.clientY - g.y;
    if (!g.moved) {
      if (Math.hypot(dx, dy) < 6) return;
      g.moved = true;
      if (e.pointerType !== 'mouse' && performance.now() - g.t > 220) { g = null; return; }   // held first: the crosshair
    }
    if (!g.on) {
      const hs = chart.options().handleScroll, o = PS().options();
      if (!(e.pointerType === 'mouse' ? hs.pressedMouseMove : hs.vertTouchDrag) || !o.autoScale || S.lock || o.mode === PSM.Percentage) { g = null; return; }
      if (Math.abs(dy) < 24 || Math.abs(dy) < Math.abs(dx)) return;
      const r = PS().getVisibleRange();
      if (!r) { g = null; return; }
      PS().setAutoScale(false);
      psSetRange(r);
      Object.assign(g, { on: true, y: e.clientY, r });
      psUi();
    }
    const k = (e.clientY - g.y) / chart.paneSize(0).height, { from, to } = g.r;
    if (PS().options().mode === PSM.Logarithmic) {
      const d = (Math.log(to) - Math.log(from)) * k;
      psSetRange({ from: from * Math.exp(d), to: to * Math.exp(d) });
    } else psSetRange({ from: from + (to - from) * k, to: to + (to - from) * k });
  }, true);
  const end = (e) => { if (g && e.pointerId === g.id) g = null; };
  window.addEventListener('pointerup', end, true);
  window.addEventListener('pointercancel', end, true);
}
// The mouse wheel over the price axis scales the prices (as TradingView); anywhere else the chart scrolls and zooms.
$('#chart').addEventListener('wheel', (e) => {
  const r = $('#chart').getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  if (x < r.width - PS().width() || y > chart.paneSize(0).height || PS().options().mode === PSM.Percentage) return;
  const v = PS().getVisibleRange();
  if (!v) return;
  e.preventDefault();
  e.stopPropagation();
  const k = Math.exp(Math.sign(e.deltaY) * Math.min(Math.abs(e.deltaY), 120) / 600), mid = (v.from + v.to) / 2, half = (v.to - v.from) / 2 * k;
  if (S.lock) { lockZoom(chart.timeScale().options().barSpacing, 1 / k); return; }   // locked: both axes, as the drag
  PS().setAutoScale(false);
  psSetRange({ from: mid - half, to: mid + half });
  psUi();
}, { capture: true, passive: false });
$('#chart').addEventListener('contextmenu', (e) => {
  const r = $('#chart').getBoundingClientRect(), y = e.clientY - r.top, x = e.clientX - r.left;
  if (y < 0 || y > chart.paneSize(0).height) return;
  if (drawings.letGo()) { e.preventDefault(); return; }   // a drawing selected or being made, or a tool picked: let go, as Esc
  if (S.placing) { setPlacing(false); e.preventDefault(); return; }   // '+ Alert' picked: the same
  if (x > r.width - PS().width()) { e.preventDefault(); openScaleCtx(e.clientX, e.clientY); return; }
  const price = candles.coordinateToPrice(y);
  if (price == null) return;
  e.preventDefault();
  openCtx(e.clientX, e.clientY, price);
});
document.addEventListener('pointerdown', (e) => { if (!$('#ctx').hidden && !e.target.closest('#ctx, #ax-plus, .menu-btn')) closeCtx(); }, true);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeCtx(); });
window.addEventListener('blur', closeCtx);
window.addEventListener('resize', closeCtx);

// The + that follows the crosshair just left of the price axis; it stays a moment after the pointer leaves the
// chart so it can be clicked. On a phone, long-press the chart for the crosshair, lift, then tap the + (the crosshair
// ends when the finger lifts, so there the + stays 3 s).
let plusHide = 0, plusTouch = false;
$('#chart').addEventListener('pointerdown', (e) => { plusTouch = e.pointerType !== 'mouse'; }, true);
function alertPlus(p) {
  const b = $('#ax-plus'), y = p.point && !p.paneIndex ? p.point.y : null;
  S.hoverPrice = y != null ? candles.coordinateToPrice(y) : null;
  if (p.time != null) S.hoverTime = p.time;
  clearTimeout(plusHide);
  if (S.hoverPrice == null) {
    plusHide = setTimeout(() => { if (!b.matches(':hover')) b.hidden = true; }, plusTouch ? 3000 : 400);
    return;
  }
  b.dataset.price = S.hoverPrice;
  b.hidden = false;
  b.style.left = `${$('#chart').clientWidth - chart.priceScale('right').width() - b.offsetWidth - 2}px`;
  b.style.top = `${y - b.offsetHeight / 2}px`;
}
$('#ax-plus').onclick = (e) => { const r = e.currentTarget.getBoundingClientRect(); openCtx(r.right, r.top, Number(e.currentTarget.dataset.price)); };
// Over the + the pointer has left the chart, which takes its crosshair away: the crosshair is held at the +'s price
// (on the bar it was last over) while the pointer is on it or its menu is open, so the line and the price tag stay.
$('#ax-plus').addEventListener('pointerenter', (e) => {
  if (e.pointerType !== 'mouse' || !S.bars.length) return;
  const b = e.currentTarget, t = S.hoverTime ?? timeOf(S.bars[S.bars.length - 1]);
  S.plusXhair = true;
  requestAnimationFrame(() => {   // after the chart's own mouseleave, which takes its crosshair away
    if (S.plusXhair && (b.matches(':hover') || !$('#ctx').hidden)) chart.setCrosshairPosition(Number(b.dataset.price), t, candles);
  });
});
function plusXhairOff() { if (S.plusXhair) { S.plusXhair = false; chart.clearCrosshairPosition(); } }
$('#ax-plus').addEventListener('pointerleave', () => {
  if ($('#ctx').hidden) plusXhairOff();   // its menu open: held until the menu closes
  plusHide = setTimeout(() => { if (S.hoverPrice == null) $('#ax-plus').hidden = true; }, 400);
});

// ---- alert lines: the current symbol's active price alerts, dashed, with a bell label and an axis tag. Drag one
// to move the alert (it re-arms); click it to edit. -------------------------------------------------------------
let alDrag = null;   // { a, key: 'price' | 'price2', x0, y0, p, moved, touch }
function alertLevels() {
  const out = [];
  for (const a of S.alerts) {
    if (a.kind !== 'price' || !a.enabled || a.symbol !== S.symbol) continue;
    for (const key of a.params.price2 != null ? ['price', 'price2'] : ['price']) {
      const dragged = alDrag && alDrag.a.id === a.id && alDrag.key === key && alDrag.p != null;
      out.push({ a, key, p: dragged ? alDrag.p : a.params[key] });
    }
  }
  return out;
}
function paintAlertLines(ctx, size) {
  const lv = alertLevels(), Y = (p) => candles.priceToCoordinate(p);
  ctx.save();
  ctx.strokeStyle = ctx.fillStyle = C.accent;
  ctx.globalAlpha = 0.07;
  for (const l of lv) {   // a channel's inside, faintly
    if (l.key !== 'price2') continue;
    const y1 = Y(l.p), y2 = Y(lv.find((o) => o.a === l.a && o.key === 'price').p);
    if (y1 != null && y2 != null) ctx.fillRect(0, Math.min(y1, y2), size.width, Math.abs(y2 - y1));
  }
  ctx.font = '11px -apple-system, "Segoe UI", system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  for (const l of lv) {
    const y = Y(l.p);
    if (y == null) continue;
    const yy = Math.round(y) + 0.5, chan = l.a.params.price2 != null;
    ctx.globalAlpha = alDrag?.a.id === l.a.id ? 1 : 0.85;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(size.width, yy); ctx.stroke();
    if (chan && l.key === 'price') continue;   // one label, on a channel's upper edge
    const note = l.a.note.length > 24 ? `${l.a.note.slice(0, 23)}…` : l.a.note;
    const text = `🔔 ${OPS[l.a.params.direction] || ''}${note ? ` · ${note}` : ''}`, w = ctx.measureText(text).width + 10;
    const x = Math.max(4, size.width - w - 30);
    ctx.globalAlpha = 1;
    ctx.beginPath(); ctx.roundRect(x, yy - 8, w, 16, 3); ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.fillText(text, x + 5, yy);
    ctx.fillStyle = C.accent;
  }
  ctx.restore();
}
class AlertLinePrimitive {
  constructor() {
    this.p = null;
    this.views = [{ zOrder: () => 'top', renderer: () => (this.p ? { draw: (t) => t.useMediaCoordinateSpace((s) => paintAlertLines(s.context, s.mediaSize)) } : null) }];
  }
  attached(p) { this.p = p; }
  detached() { this.p = null; axisStack.drop(this); }
  paneViews() { return this.views; }
  priceAxisViews() {
    return axisStack.views(this, alertLevels().map((l) => ({ coordinate: () => candles.priceToCoordinate(l.p) ?? -100, text: () => px(l.p),
      textColor: () => '#ffffff', backColor: () => C.accent, visible: () => candles.priceToCoordinate(l.p) != null })));
  }
  update() { if (this.p) this.p.requestUpdate(); }
}
const alertPrim = new AlertLinePrimitive();
candles.attachPrimitive(alertPrim);

let alLockSaved = null;
function alLock(on) {   // the chart must not pan or scale under a dragged line
  if (on && !alLockSaved) {
    const o = chart.options();   // the library's own object, changed in place by the lock: keep a copy
    alLockSaved = structuredClone({ handleScroll: o.handleScroll, handleScale: o.handleScale });
    chart.applyOptions({ handleScroll: false, handleScale: false });
  } else if (!on && alLockSaved) {
    chart.applyOptions(alLockSaved);
    alLockSaved = null;
  }
}
// The alert line under the pointer, while no drawing tool or chart pick is in use.
function alHit(e) {
  const D = drawings.state;
  if (D.tool !== 'select' || D.creating || (D.measure && !D.measure.done) || S.placing || S.anchoring || S.replayPick) return null;
  const r = $('#chart').getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  if (x < 0 || y < 0 || x > r.width || y > chart.paneSize(0).height) return null;
  const tol = e.pointerType === 'mouse' ? 5 : 12;
  let best = null;
  for (const l of alertLevels()) {
    const ly = candles.priceToCoordinate(l.p), d = ly == null ? Infinity : Math.abs(ly - y);
    if (d <= tol && (!best || d < best.d)) best = { ...l, d, y };
  }
  return best;
}
// Capture on the wrapper: it runs before the drawings' and the chart's own handlers, and stops them.
$('#chart-wrap').addEventListener('pointerdown', (e) => {
  if (e.button > 0 || e.shiftKey || e.altKey || !e.target.closest('#chart')) return;
  const h = alHit(e);
  if (!h) return;
  e.preventDefault();
  e.stopPropagation();
  // A finger: the chart is held only once it moves up or down (sideways it pans, as it would off the line); a mouse
  // holds it at once.
  const touch = e.pointerType !== 'mouse';
  if (!touch) alLock(true);
  alDrag = { a: h.a, key: h.key, x0: e.clientX, y0: h.y, p: null, moved: false, touch };
}, true);
window.addEventListener('pointermove', (e) => {
  if (!alDrag) {
    if (e.pointerType === 'mouse' && !e.buttons) $('#chart-wrap').classList.toggle('al-hover', !!alHit(e));
    return;
  }
  const y = e.clientY - $('#chart').getBoundingClientRect().top, dx = Math.abs(e.clientX - alDrag.x0), dy = Math.abs(y - alDrag.y0);
  if (!alDrag.moved) {
    if (alDrag.touch && dx >= 3 && dx > dy) { alDrag = null; return; }   // sideways: the chart's pan, not the line's
    if (dy < 3) return;
    alLock(true);
  }
  alDrag.moved = true;
  const p = candles.coordinateToPrice(Math.max(0, Math.min(chart.paneSize(0).height, y)));
  if (p != null) { alDrag.p = snap(p); alertPrim.update(); }
}, true);
window.addEventListener('pointerup', () => {
  const g = alDrag;
  if (!g) return;
  alDrag = null;
  alLock(false);
  if (!g.moved || g.p == null || g.p === g.a.params[g.key]) { alertPrim.update(); if (!g.moved) openAlertDialog({ alert: g.a }); return; }
  const old = g.a.params;
  g.a.params = { ...old, [g.key]: g.p };   // drawn at the new price until the server's list arrives
  alertPrim.update();
  api('PATCH', `/api/alerts/${g.a.id}`, { [g.key]: g.p })
    .then(() => toast(`Alert moved to ${px(g.p)}`))
    .catch((err) => { g.a.params = old; alertPrim.update(); fail(err); });
}, true);
window.addEventListener('pointercancel', () => { if (alDrag) { alDrag = null; alLock(false); alertPrim.update(); } }, true);

function renderEvents() {
  $('#event-list').replaceChildren(...S.events.slice(0, 30).map((ev) => {
    const li = document.createElement('li');
    const t = document.createElement('time');
    t.textContent = ax.full.format(ev.ts) + (ev.delivered ? ` · sent ${ev.delivered}` : '');
    const m = document.createElement('span');
    m.textContent = ev.message;
    li.append(t, m);
    return li;
  }));
}

// The watchlist/alerts panel folds away (a strip on the side); a tab opens it again. On a phone, as TradingView's app,
// it is one page at a time: the chart (the panel folded to its bar at the bottom) or a panel's page, full screen.
// A chart of a layout has no panel, so it is never a panel's page: in a frame narrower than a phone's 760 px the
// phone's page rules (style.css) would hide its chart and header.
const phoneLayout = () => window.innerWidth < 760 && TK_CELL === null && !TK_MULTI;
const sideKey = () => (phoneLayout() ? 'sideOpenPhone' : 'sideOpen');
function setSide(open) {
  const page = open && TK_CELL === null;
  $('main').classList.toggle('side-off', !page);
  document.body.classList.toggle('side-page', page);
  const t = $('#side-toggle');
  t.textContent = open ? '⇥' : '⇤';
  t.title = open ? 'Hide the panel' : 'Show the panel';
  t.setAttribute('aria-expanded', String(open));
  local.set(sideKey(), open ? '1' : '0');
  pageUi();
}
// The phone bar's More: the pages that don't fit (Tape, Journal, Calendar), lit while one of them is open.
function pageUi() {
  const on = !$('main').classList.contains('side-off') && $('.tabs .pg-extra.on');
  $('.tabs .pg-more').classList.toggle('on', !!on);
  $('.tabs .pg-more').textContent = on ? `${on.textContent.trim()} ▴` : 'More';
}
setSide(local.get(sideKey(), phoneLayout() ? '0' : '1') === '1');   // a phone starts on the chart
{
  let wasPhone = phoneLayout();   // turned (landscape is wider): that layout's own page
  window.addEventListener('resize', () => { if (phoneLayout() !== wasPhone) { wasPhone = phoneLayout(); setSide(local.get(sideKey(), wasPhone ? '0' : '1') === '1'); } });
}
$('.tabs .pg-chart').onclick = () => { setSide(false); tapeSync(); };
$('.tabs .pg-more').onclick = (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  showMenu(r.left, r.top, [...document.querySelectorAll('.tabs .pg-extra')].map((b) => ({ label: b.textContent.trim(), check: b.classList.contains('on') && !$('main').classList.contains('side-off'), fn: () => b.click() })));
};
// The panel's size: its width beside the chart, or on a phone its height under it. Dragged at its edge, kept per device.
function sideSize(px) {
  const phone = window.innerWidth < 760, main = $('main').getBoundingClientRect();
  const key = phone ? 'sideH' : 'sideW', prop = phone ? '--side-h' : '--side-w';
  if (px === undefined) px = Number(local.get(key, '')) || null;
  if (px == null) { document.documentElement.style.removeProperty(prop); return; }
  const v = phone ? Math.round(Math.min(Math.max(px, 120), main.height - 160)) : Math.round(Math.min(Math.max(px, 220), Math.min(900, window.innerWidth - 360)));
  document.documentElement.style.setProperty(prop, `${v}px`);
  return v;
}
sideSize();
{
  const grip = $('#side-grip');
  grip.onpointerdown = (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const phone = window.innerWidth < 760, main = $('main').getBoundingClientRect(), pid = e.pointerId;
    grip.classList.add('drag');
    document.body.classList.add('resizing');
    let v = null;
    const move = (ev) => { if (ev.pointerId === pid) v = sideSize(phone ? main.bottom - ev.clientY : main.right - ev.clientX); };
    const end = (ev) => {
      if (ev.pointerId !== pid) return;
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', end);
      document.removeEventListener('pointercancel', end);
      grip.classList.remove('drag');
      document.body.classList.remove('resizing');
      if (v != null) local.set(phone ? 'sideH' : 'sideW', String(v));
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', end);
    document.addEventListener('pointercancel', end);
  };
  grip.ondblclick = () => { local.set(window.innerWidth < 760 ? 'sideH' : 'sideW', ''); sideSize(); };
  grip.onkeydown = (e) => {   // arrows: 20 px a press
    const phone = window.innerWidth < 760, d = { ArrowLeft: 20, ArrowRight: -20, ArrowUp: 20, ArrowDown: -20 }[e.key];
    if (!d) return;
    e.preventDefault();
    const r = $('#side').getBoundingClientRect(), v = sideSize((phone ? r.height : r.width) + d);
    local.set(phone ? 'sideH' : 'sideW', String(v));
  };
  window.addEventListener('resize', () => sideSize());
}
$('#side-toggle').onclick = () => { setSide($('main').classList.contains('side-off')); tapeSync(); if (tapeOpen()) tapeLoad(); if (journalOpen()) journalLoad(); };   // shown: fills heard while hidden
document.querySelectorAll('.tabs button[data-tab]').forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll('.tabs button[data-tab]').forEach((x) => x.classList.toggle('on', x === b));
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t.id === `tab-${b.dataset.tab}`));
    setSide(true);
    pageUi();
    if (typeof tapeSync === 'function') { tapeSync(); if (b.dataset.tab === 'tape') tapeLoad(); if (b.dataset.tab === 'journal') journalLoad(); }
    if (b.dataset.tab === 'calendar' && typeof calLoad === 'function') calLoad();
  };
});

let toastTimer;
// An alert you set (price, line, indicator, study) stays on screen until OK, as TradingView's; the Toolkit's and the
// gap alerts, which can come in runs, are a toast.
function firedShow(ev) {
  const li = document.createElement('li'), list = $('#fired-list');
  li.textContent = `${ax.hm.format(ev.ts)}  ${ev.message}`;
  list.prepend(li);
  while (list.children.length > 8) list.lastChild.remove();
  $('#fired').hidden = false;
}
$('#fired-ok').onclick = () => { $('#fired').hidden = true; $('#fired-list').replaceChildren(); };
function toast(msg, ms = 4000) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), ms);
}

// ---- bar replay: the chart leaves the live feed and steps through bars the server builds around a past moment --
const REPLAY_OFF = { lv: false, amt: false, tpo: false, gap: false, prof: false, aavp: false, pvp: false, right: 'off' };   // drawn from later data
// When bar i is complete: the next bar's open (after: where the last bar ends, where known), or a time bar's end
// if that comes first (the 16:00 close), or a replay's tick, volume or range bar's hours' end that the server sends
// (e) where the next one opens a session later. days: 2D or 3D bars, which hold a weekend or a holiday as often as
// not (a Friday and a Monday): the next bar's open, their days from their open only while that is not known.
const daysSpec = (spec) => /^\d+D$/.test(spec) && parseInt(spec, 10) > 1;
function barEnd(bars, i, kind, periodMs, days = false, after = null) {
  const next = bars[i + 1]?.t ?? after ?? Infinity;
  return kind === 'time' && !(days && next < Infinity) ? Math.min(next, bars[i].t + periodMs) : bars[i].e ?? next;
}
function shownCount(bars, at, kind, periodMs, days, after = null) {   // after: where the last bar ends, as barEnd
  let i = 0;
  while (i < bars.length && barEnd(bars, i, kind, periodMs, days, after) <= at) i++;
  return i;
}
S.replay = null;      // {at: the replay clock, data, idx: bars shown, kind, period, days, nextAt, endAt, playing, busy, hidden, req, empty}
S.replayPick = false; // waiting for a bar tap or a date
let replaySpeed = Number(local.get('rpSpeed', '1')) || 1;

function replayUi() {
  const r = S.replay, on = !!(r || S.replayPick);
  $('#replay-bar').hidden = !on;
  $('#replay-btn').classList.toggle('on', on);
  $('#replay-btn').setAttribute('aria-pressed', String(on));
  $('#rp-pick').hidden = !S.replayPick;
  for (const id of ['#rp-play', '#rp-step', '#rp-speed', '#rp-jump']) $(id).hidden = !r || S.replayPick;   // picking: the date, Go and ✕
  $('#rp-play').textContent = r?.playing ? '⏸' : '▶';
  $('#rp-play').title = r?.playing ? 'Pause (Space)' : 'Play (Space)';
  $('#rp-when').textContent = !r ? 'tap a bar, or:' : r.busy ? 'loading…' : r.idx ? `${ax.day.format(r.data[r.idx - 1].t)} ${ax.hm.format(r.data[r.idx - 1].t)}` : 'no bars yet';
  $('#chart-wrap').classList.toggle('picking', S.replayPick);
}
function replayPicking(on) {
  S.replayPick = on;
  if (on) {
    replayPause();
    const from = S.replay ? S.replay.at : S.bars.at(-1)?.t ?? Date.now();
    $('#rp-date').value = ax.input.toInput(from - 86400e3);   // on the chosen zone's clock
  }
  replayUi();
}
function replayStart(at) {
  S.replayPick = false;
  S.liveKey = null;   // back from replay: the latest bars, not the view replay left
  if (!S.replay) {
    const hidden = {};
    for (const [k, off] of Object.entries(REPLAY_OFF)) if (S.st[k] !== off) hidden[k] = S.st[k];
    S.replay = { at, data: [], idx: 0, kind: 'time', period: 0, nextAt: null, endAt: null, playing: false, timer: 0, busy: false, hidden, req: 0, empty: 0 };
    for (const k of Object.keys(hidden)) { S.st[k] = REPLAY_OFF[k]; studiesChanged(k); }
  }
  replayPause();
  S.replay.at = at;
  subscribe();   // leaves the live chart and calls replayLoad()
}
async function replayLoad() {
  const r = S.replay, req = ++r.req;
  r.busy = true;
  replayUi();
  try {
    const d = await api('GET', `/api/replay?symbol=${S.symbol}&spec=${S.spec}&at=${Math.round(r.at)}&rth=${S.rth}`);
    if (S.replay !== r || req !== r.req) return;
    Object.assign(r, { data: d.bars, kind: d.kind, period: d.kind === 'time' ? specSeconds(d.spec) * 1000 : 0, days: daysSpec(d.spec), nextAt: d.next_at, endAt: d.end_at, empty: 0 });
    r.idx = shownCount(d.bars, r.at, r.kind, r.period, r.days, d.end_at ?? d.next_at);
    applyHistory({ kind: d.kind, bars: d.bars.slice(0, r.idx) });
    if (!r.idx) toast(d.bars.length ? 'No bars before that time: press ▶ or ⏭ to start at the first one' : 'No stored bars around that time on this chart');
  } catch (e) {
    if (S.replay === r) { fail(e); loadNote(e.message); }
  } finally {
    if (S.replay === r && req === r.req) { r.busy = false; replayUi(); }
  }
}
function replayStep() {
  const r = S.replay;
  if (!r || r.busy) return;
  if (r.idx >= r.data.length) { replayMore(); return; }
  const i = r.idx++, end = barEnd(r.data, i, r.kind, r.period, r.days, r.endAt ?? r.nextAt);
  r.at = Number.isFinite(end) ? end : r.nextAt ?? r.endAt ?? r.data[i].t + 1;   // endAt: at the present, the forming bar's open
  applyBars({ bars: [r.data[i]] });
  replayUi();
}
// Past the loaded bars: ask for the next stretch and carry on from the last bar shown.
async function replayMore() {
  const r = S.replay;
  if (!r || r.busy) return;
  if (r.nextAt == null) { replayPause(); toast('Replay has caught up with the stored bars. ✕ goes back to live.'); return; }
  const req = ++r.req, last = r.data[r.idx - 1];
  r.busy = true;
  replayUi();
  try {
    const d = await api('GET', `/api/replay?symbol=${S.symbol}&spec=${S.spec}&at=${r.nextAt}&rth=${S.rth}`);
    if (S.replay !== r || req !== r.req) return;
    let j = -1;
    for (let k = d.bars.length - 1; last && k >= 0 && d.bars[k].t >= last.t; k--) {
      const b = d.bars[k];
      if (b.t === last.t && b.o === last.o && b.c === last.c && b.v === last.v) { j = k; break; }
    }
    if (j < 0) {   // not the same bars (data written since): start over at the replay clock
      Object.assign(r, { data: d.bars, idx: shownCount(d.bars, r.at, r.kind, r.period, r.days, d.end_at ?? d.next_at), nextAt: d.next_at, endAt: d.end_at });
      applyHistory({ kind: d.kind, bars: d.bars.slice(0, r.idx) });
      return;
    }
    const off = last.seq - d.bars[j].seq;   // keep the bar numbers the chart already uses
    for (const b of d.bars) b.seq += off;
    Object.assign(r, { data: d.bars, idx: j + 1, nextAt: d.next_at, endAt: d.end_at });
    if (r.idx >= r.data.length) {   // a weekend or holiday: nothing in that stretch, try the next
      r.empty += 1;
      if (r.nextAt != null && r.empty < 8) { r.busy = false; replayMore(); return; }
      replayPause();
      toast(r.nextAt == null ? 'Replay has caught up with the stored bars. ✕ goes back to live.' : 'No more stored bars found');
    } else r.empty = 0;
  } catch (e) {
    if (S.replay === r) { fail(e); replayPause(); }
  } finally {
    if (S.replay === r && req === r.req) { r.busy = false; replayUi(); }
  }
}
function replayPlay() {
  const r = S.replay;
  if (!r || r.playing) return;
  r.playing = true;
  const tick = () => {
    if (S.replay !== r || !r.playing) return;
    replayStep();
    r.timer = setTimeout(tick, 1000 / replaySpeed);
  };
  tick();
  replayUi();
}
function replayPause() {
  const r = S.replay;
  if (!r) return;
  r.playing = false;
  clearTimeout(r.timer);
  replayUi();
}
function replayExit() {
  const r = S.replay;
  S.replayPick = false;
  if (r) {
    replayPause();
    S.replay = null;
    Object.assign(S.st, r.hidden);
    subscribe();
    for (const k of Object.keys(r.hidden)) studiesChanged(k);
  }
  replayUi();
}
$('#replay-btn').onclick = () => {
  if (S.replayPick && S.replay) replayPicking(false);
  else if (S.replay || S.replayPick) replayExit();
  else replayPicking(true);
};
$('#rp-play').onclick = () => (S.replay?.playing ? replayPause() : replayPlay());
$('#rp-step').onclick = () => { replayPause(); replayStep(); };
$('#rp-speed').value = String(replaySpeed);
$('#rp-speed').onchange = (e) => { replaySpeed = Number(e.target.value) || 1; local.set('rpSpeed', String(replaySpeed)); };
$('#rp-jump').onclick = () => replayPicking(!S.replayPick);
$('#rp-exit').onclick = replayExit;
$('#rp-go').onclick = () => {
  const at = ax.input.toMs($('#rp-date').value);
  if (at == null || at >= Date.now()) { toast(`Pick a time in the past (${ax.short})`); return; }
  replayStart(at);
};
document.addEventListener('keydown', (e) => {
  if (!S.replay || e.target.closest?.('input, textarea, select')) return;
  if (e.key === 'ArrowRight' && e.shiftKey) { e.preventDefault(); replayPause(); replayStep(); }
  else if (e.key === ' ' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey && !e.target.closest?.('button')) {
    e.preventDefault();
    if (S.replay.playing) replayPause(); else replayPlay();
  }
});

// ---- chart controls: price-scale corner (auto, log, lock ratio), navigation, go to live, keys, quick switch,
// bar countdown, session breaks, chart settings menu, screenshot and full screen ---------------------------------
const PS = () => chart.priceScale('right');
S.lock = null;   // locked price-to-bar ratio: price units per bar width, or null
// As TradingView's corner: % (percent from the first bar in view) and log are each other's opposite; auto fits the
// data in view, goes off when the axis is dragged and on again with "auto" or a double-click on the axis; the lock
// (price to bar ratio) needs a regular scale and turns auto off. Each choice stays as it is when another is made.
const PSM = L.PriceScaleMode;
// A price range on the axis. On a log scale the library gives prices back but takes its own log units: convert, by
// the formula it uses for this scale (its default when that can't be read).
function psSetRange(r) {
  if (PS().options().mode !== PSM.Logarithmic) { PS().setVisibleRange(r); return; }
  let f = null;
  try { f = PS().Ki().so(); } catch { /* another library build */ }
  const vl = Number.isFinite(f?.Vl) ? f.Vl : 4, bl = Number.isFinite(f?.Bl) ? f.Bl : 1e-4;
  const lg = (x) => { const a = Math.abs(x); if (a < 1e-15) return 0; const y = Math.log10(a + bl) + vl; return x < 0 ? -y : y; };
  PS().setVisibleRange({ from: lg(r.from), to: lg(r.to) });
}
function psUi() {
  const mode = PS().options().mode;
  // Locked: a drag on the price axis zooms both axes (lockZoom) instead of the library scaling the prices alone.
  const axisPrice = !S.lock, hs = chart.options().handleScale;
  if (hs.axisPressedMouseMove && hs.axisPressedMouseMove.price !== axisPrice && (hs.axisPressedMouseMove.time || hs.axisPressedMouseMove.price)) {
    chart.applyOptions({ handleScale: { axisPressedMouseMove: { time: true, price: axisPrice } } });
  }
  for (const [id, on] of [['#ps-auto', !!PS().options().autoScale && !S.lock], ['#ps-log', mode === PSM.Logarithmic],
    ['#ps-pct', mode === PSM.Percentage], ['#ps-lock', !!S.lock]]) {
    $(id).classList.toggle('on', on);
    $(id).setAttribute('aria-pressed', String(on));
  }
}
function setAuto(on) {
  if (on) S.lock = null;
  PS().setAutoScale(on);
  psUi();
  viewSave();
}
function setScaleMode(mode) {
  if (mode !== PSM.Normal && S.lock) { S.lock = null; PS().setAutoScale(true); }   // no ratio to keep on log or %
  PS().applyOptions({ mode });
  local.set('scaleMode', mode === PSM.Logarithmic ? 'log' : mode === PSM.Percentage ? 'pct' : '');
  lookStore('scale', mode === PSM.Logarithmic ? 'log' : mode === PSM.Percentage ? 'pct' : '');
  psUi();
}
$('#ps-auto').onclick = () => setAuto(!(PS().options().autoScale && !S.lock));
$('#ps-log').onclick = () => setScaleMode(PS().options().mode === PSM.Logarithmic ? PSM.Normal : PSM.Logarithmic);
$('#ps-pct').onclick = () => setScaleMode(PS().options().mode === PSM.Percentage ? PSM.Normal : PSM.Percentage);
// Lock price to bar ratio (TradingView): zooming in or out keeps the chart's proportions, so a 45° move stays 45°.
function lockFromView() {
  const r = PS().getVisibleRange(), h = chart.paneSize(0).height, bs = chart.timeScale().options().barSpacing;
  return r && h > 0 ? (r.to - r.from) / h * bs : null;
}
function applyLock(recenter) {
  if (!S.lock) return;
  const h = chart.paneSize(0).height, bs = chart.timeScale().options().barSpacing, r = PS().getVisibleRange();
  if (!r || h <= 0) return;
  let mid = (r.from + r.to) / 2;
  if (recenter) {   // new bars: centre on what is in view
    const lr = chart.timeScale().getVisibleLogicalRange();
    let hi = -Infinity, lo = Infinity;
    for (let i = Math.max(0, Math.floor(lr?.from ?? 0)); i <= Math.min(S.bars.length - 1, Math.ceil(lr?.to ?? -1)); i++) { hi = Math.max(hi, S.bars[i].h); lo = Math.min(lo, S.bars[i].l); }
    if (hi >= lo) mid = (hi + lo) / 2;
  }
  const half = S.lock / bs * h / 2;
  PS().setVisibleRange({ from: mid - half, to: mid + half });
}
// Locked, as TradingView: zooming keeps the proportions, so the bars' spacing sets the price range. A drag up or down on
// the price axis (one finger is enough) or the wheel over it zooms in or out; a double-click (double-tap) on it unlocks
// and fits the data again.
function lockZoom(bs0, f) {   // also the time axis' one-finger zoom, locked or not
  const ts = chart.timeScale(), o = ts.options();
  ts.applyOptions({ barSpacing: Math.max(o.minBarSpacing || 0.5, Math.min(120, bs0 * f)) });   // applyLock follows
}
{
  const el = $('#chart');
  let z = null;
  el.addEventListener('pointerdown', (e) => {
    z = null;
    if (!S.lock || !e.isPrimary || (e.pointerType === 'mouse' && e.button !== 0)) return;
    const r = el.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    if (x < r.width - PS().width() || y > chart.paneSize(0).height) return;
    z = { id: e.pointerId, y: e.clientY, bs: chart.timeScale().options().barSpacing };
  }, true);
  window.addEventListener('pointerdown', (e) => { if (z && e.pointerId !== z.id) z = null; }, true);
  window.addEventListener('pointermove', (e) => {
    if (!z || e.pointerId !== z.id || !S.lock) return;
    lockZoom(z.bs, Math.exp(-(e.clientY - z.y) / 150));   // down: out, up: in, as dragging the axis does unlocked
  }, true);
  const end = (e) => { if (z && e.pointerId === z.id) z = null; };
  window.addEventListener('pointerup', end, true);
  window.addEventListener('pointercancel', end, true);
}
// One finger on the time axis zooms time, as TradingView's app (the library does it for a mouse only): right in, left
// out. Locked, the price range follows (the ratio holds).
{
  const el = $('#chart');
  let z = null;
  el.addEventListener('pointerdown', (e) => {
    z = null;
    if (e.pointerType === 'mouse' || !e.isPrimary) return;
    const r = el.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top, h = chart.paneSize(0).height;
    let below = 0;   // the time axis sits under the last pane
    for (let i = 0; i < chart.panes().length; i++) below += chart.paneSize(i).height;
    if (x > r.width - PS().width() || y < below || y > below + chart.timeScale().height() + 6 || h <= 0) return;
    z = { id: e.pointerId, x: e.clientX, bs: chart.timeScale().options().barSpacing };
  }, true);
  window.addEventListener('pointerdown', (e) => { if (z && e.pointerId !== z.id) z = null; }, true);
  window.addEventListener('pointermove', (e) => { if (z && e.pointerId === z.id) lockZoom(z.bs, Math.exp((e.clientX - z.x) / 150)); }, true);
  const end = (e) => { if (z && e.pointerId === z.id) z = null; };
  window.addEventListener('pointerup', end, true);
  window.addEventListener('pointercancel', end, true);
}
$('#ps-lock').onclick = () => {
  if (S.lock) { S.lock = null; psUi(); viewSave(); return; }   // unlocked: the view stays as it is (auto is one click away)
  if (PS().options().mode !== PSM.Normal) setScaleMode(PSM.Normal);
  S.lock = lockFromView();
  PS().setAutoScale(false);
  psUi();
  viewSave();
  toast('Price to bar ratio locked: zooming keeps the proportions');
};
chart.timeScale().subscribeVisibleLogicalRangeChange(() => { applyLock(false); goLiveUi(); viewSave(); });
// The axis was dragged or double-clicked: read back what the chart did, and re-derive a locked ratio.
for (const ev of ['pointerup', 'wheel', 'dblclick']) {
  $('#chart').addEventListener(ev, () => setTimeout(() => { if (S.lock && PS().options().autoScale) S.lock = null; else if (S.lock) S.lock = lockFromView() || S.lock; psUi(); viewSave(); }, 0), true);
}
{
  const m = local.get('scaleMode', local.get('scaleLog', '0') === '1' ? 'log' : '');   // scaleLog: before % was there
  if (m) PS().applyOptions({ mode: m === 'log' ? PSM.Logarithmic : PSM.Percentage });
  psUi();
}
// Each tab's view, as TradingView keeps it: how far it is zoomed and scrolled, and its price scale (auto, a range of
// its own, or locked). Back when the tab opens again, the app starts again, or the same bars load again. This
// device's (a phone shows fewer bars than a desktop), so not in the workspace; each chart of a layout keeps its own (the
// frames would write over each other's).
const VIEWS_MAX = 60, VIEWS_KEY = TK_CELL ? `c${TK_CELL}.tabViews` : 'tabViews';
S.views = (() => {
  try { const v = JSON.parse(local.get(VIEWS_KEY, '{}')); return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; } catch { return {}; }
})();
S.viewWant = !TK_MULTI ? S.views[S.tabs[S.tab]?.id] || null : null;   // the open tab's, for the first bars
function viewKey() { return `${S.symbol} ${S.spec} ${S.rth}`; }
// The view now: its width in bars and right edge (bars past the last one when the latest bar is in view, else the
// bar there), the price scale. Null while the bars on the chart are not this symbol's, timeframe's and session's.
function viewSnap() {
  const lr = chart.timeScale().getVisibleLogicalRange(), n = S.bars.length;
  if (!lr || !n || S.loadedKey !== viewKey()) return null;
  const auto = !!PS().options().autoScale && !S.lock, r = PS().getVisibleRange();
  const i = Math.max(0, Math.min(n - 1, Math.floor(lr.to)));
  return { key: viewKey(), w: lr.to - lr.from, ...(lr.to >= n - 1 ? { ro: lr.to - (n - 1) } : { t: S.bars[i].t, f: lr.to - i }), auto,
    ...(S.lock ? { lock: S.lock } : {}), ...(!auto && r ? { pr: [r.from, r.to] } : {}) };
}
function viewApply(v) {   // false: not for these bars (the tab shows another symbol, timeframe or session since)
  const n = S.bars.length;
  if (!v || v.key !== viewKey() || !n || !(v.w > 0)) return false;
  let to = n - 1 + (Number(v.ro) || 0);
  if (v.t != null) {   // scrolled back: to the same bar (not loaded any more: the latest ones)
    const i = barIndexTo(S.bars, v.t);
    if (i >= 0) to = i + (Number(v.f) || 0);
  }
  S.lock = v.lock > 0 ? v.lock : null;
  PS().setAutoScale(!!v.auto && !S.lock);
  chart.timeScale().setVisibleLogicalRange({ from: to - v.w, to });
  if (!v.auto && Array.isArray(v.pr) && v.pr[1] > v.pr[0]) psSetRange({ from: v.pr[0], to: v.pr[1] });
  else if (S.lock) applyLock(true);
  psUi();
  return true;
}
let viewTimer = 0;
function viewSave(now) {   // a moment after the view stops moving (now: at once)
  clearTimeout(viewTimer);
  if (!now) { viewTimer = setTimeout(() => viewSave(true), 800); return; }
  const t = S.tabs?.[S.tab], v = t && !TK_MULTI && !S.replay && !S.viewWant ? viewSnap() : null;
  if (!v) return;
  S.views[t.id] = { ...v, at: Date.now() };
  const ids = Object.keys(S.views);
  if (ids.length > VIEWS_MAX) for (const id of ids.sort((a, b) => S.views[a].at - S.views[b].at).slice(0, ids.length - VIEWS_MAX)) delete S.views[id];
  local.set(VIEWS_KEY, JSON.stringify(S.views));
}
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') viewSave(true); });
window.addEventListener('pagehide', () => viewSave(true));

// x axis: zoom around the right edge, scroll by a fifth of the view, reset to the latest bars with auto-scale.
function zoom(f) {
  const ts = chart.timeScale(), r = ts.getVisibleLogicalRange();
  if (!r) return;
  const w = Math.max(10, Math.min(5000, (r.to - r.from) * f));
  ts.setVisibleLogicalRange({ from: r.to - w, to: r.to });
}
function scrollBars(f) {
  const ts = chart.timeScale(), r = ts.getVisibleLogicalRange();
  if (!r) return;
  const d = Math.max(1, Math.round((r.to - r.from) * Math.abs(f)));
  ts.setVisibleLogicalRange({ from: r.from + d * Math.sign(f), to: r.to + d * Math.sign(f) });
}
function resetView() {
  const n = S.bars.length, ro = chart.timeScale().options().rightOffset;   // as a fresh load: the room amtLayout gives the labels
  chart.timeScale().setVisibleLogicalRange({ from: n - 150 - ro, to: n + ro });
  setAuto(true);
}
// Go to date (TradingView's Alt+G): a moment on the chosen zone's clock, centred at the zoom you have, the crosshair on
// its bar; on D/W/M charts a trade date, as their axis shows them. Only within the bars loaded: a longer timeframe
// reaches further back.
const tradeDate = (ms) => msToCtInput(ms + 7 * 3600e3).slice(0, 10);   // a D/W/M bar's: its 17:00 CT open the day before
async function goToDate() {
  if (!S.bars.length) return;
  const ts = chart.timeScale(), r = ts.getVisibleLogicalRange(), w = r ? r.to - r.from : 150;
  const mid = S.bars[Math.max(0, Math.min(S.bars.length - 1, Math.round(r ? (r.from + r.to) / 2 : S.bars.length - 1)))];
  let i;
  if (calSpec() && isTime()) {
    const v = await ask({ title: 'Go to date (trade date)', value: tradeDate(mid.t), ok: 'Go', type: 'date' });
    if (!/^\d{4}-\d\d-\d\d$/.test(v || '')) return;
    if (v < tradeDate(S.bars[0].t)) { toast(`The bars loaded start ${fmtDate.format(S.bars[0].t + 7 * 3600e3)}: a longer timeframe goes further back`); return; }
    for (i = S.bars.length - 1; i > 0 && tradeDate(S.bars[i].t) > v;) i -= 1;   // the bar holding that date
  } else {
    const v = await ask({ title: `Go to date (${ax.short})`, value: ax.input.toInput(mid.t), ok: 'Go', type: 'datetime-local' });
    const ms = v == null ? null : ax.input.toMs(v);
    if (ms == null) return;
    if (ms < S.bars[0].t) { toast(`The bars loaded start ${ax.md.format(S.bars[0].t)} ${zs(S.bars[0].t)}: a longer timeframe goes further back`); return; }
    i = Math.min(S.bars.length - 1, Math.max(0, barIndexTo(S.bars, ms)));
  }
  ts.setVisibleLogicalRange({ from: i - w / 2, to: i + w / 2 });
  chart.setCrosshairPosition(S.bars[i].c, timeOf(S.bars[i]), candles);
}
const NAV = { out: () => zoom(1.25), in: () => zoom(0.8), left: () => scrollBars(-0.2), right: () => scrollBars(0.2), reset: resetView, date: goToDate };
document.querySelectorAll('#nav [data-nav]').forEach((b) => { b.onclick = () => NAV[b.dataset.nav](); });
function goLiveUi() {
  const r = chart.timeScale().getVisibleLogicalRange();
  $('#go-live').hidden = !r || !S.bars.length || r.to >= S.bars.length - 1;
}
$('#go-live').onclick = () => { chart.timeScale().scrollToRealTime(); };
// Floating controls follow the price pane (indicator panes below it and the axis widths change).
function layoutControls() {
  const w = chart.priceScale('right').width(), th = chart.timeScale().height(), h0 = chart.paneSize(0).height, W = $('#chart').clientWidth;
  Object.assign($('#ps-ctl').style, { minWidth: `${w}px`, height: `${th}px` });   // wider than the axis: over the time axis' end
  Object.assign($('#nav').style, { left: `${(W - w) / 2}px`, top: `${h0 - 40}px` });
  Object.assign($('#go-live').style, { right: `${w + 8}px`, top: `${h0 - 40}px` });
  placeAmtDash();
}
new ResizeObserver(layoutControls).observe($('#chart'));
setInterval(() => { layoutControls(); psUi(); }, 1000);   // pane dividers dragged, axis widths changed

// Keys (not while typing): arrows scroll and zoom, Home/End, Alt+R; letters and digits open the quick switch.
document.addEventListener('keydown', (e) => {
  if (e.target.closest('input, textarea, select, [contenteditable]') || !$('#ind-dlg').hidden) return;
  if (TK_MULTI) {   // the charts are in the frames: a letter or digit opens the quick switch on the chart last clicked
    if (!e.ctrlKey && !e.metaKey && !e.altKey && /^[a-z0-9]$/i.test(e.key)) { e.preventDefault(); workspaces.toActive({ type: 'key', key: e.key }, true); }
    return;
  }
  if (e.altKey && !e.shiftKey && e.code === 'KeyR') { e.preventDefault(); resetView(); return; }
  if (e.altKey && !e.shiftKey && e.code === 'KeyG') { e.preventDefault(); goToDate(); return; }
  if (e.altKey && !e.shiftKey && e.code === 'KeyS') { e.preventDefault(); savePicture(); return; }   // TradingView's snapshot
  if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); openSt(true); $('#st-q').focus(); return; }   // as TradingView: / finds an indicator
  if (e.key === '?') { e.preventDefault(); showKeys(true); return; }
  if (e.altKey && e.code === 'KeyA') { e.preventDefault(); openAlertDialog({ price: S.hoverPrice ?? S.lastBar?.c }); return; }
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey && e.key.startsWith('Arrow')) return;
  const k = { ArrowLeft: () => scrollBars(-0.05), ArrowRight: () => scrollBars(0.05), ArrowUp: () => zoom(0.8), ArrowDown: () => zoom(1.25),
    '+': () => zoom(0.8), '=': () => zoom(0.8), '-': () => zoom(1.25), Home: () => chart.timeScale().scrollToPosition(-S.bars.length + 20, false),
    End: () => chart.timeScale().scrollToRealTime() }[e.key];
  if (k) { e.preventDefault(); k(); return; }
  if (/^[a-z0-9]$/i.test(e.key)) { e.preventDefault(); quickOpen(e.key); }
});
// The symbol switch searches the symbol database (symsearch.js); it takes timeframes too.
// Opened by Compare it picks a symbol to compare instead (no timeframes).
let quickCompare = false;
const quickSearch = createSymbolSearch({ input: $('#quick-in'), list: $('#quick-list'), chips: $('#quick-types'), api,
  onPick: (r) => { const cmp = quickCompare; quickClose(); if (cmp) cmpAdd(r.symbol); else setSymbol(r.symbol); } });
function quickOpen(first, compare = false) {
  const box = $('#quick'), inp = $('#quick-in');
  quickCompare = compare;
  box.classList.toggle('cmp', compare);
  inp.placeholder = compare ? 'Compare with: symbol or name (ES, RTY, AAPL, DXY…)' : 'Symbol, name or timeframe';
  box.hidden = false;
  inp.value = first;
  inp.focus();
  quickSearch.refresh(0);
}
function quickClose() { $('#quick').hidden = true; $('#quick-in').blur(); quickSearch.clear(); quickCompare = false; }
// "5" -> 5m, "1H" -> 1h, "D" -> 1D, "W" -> 1W, "M" -> 1M (as TradingView: a capital M is months), "2000T" -> 2000t;
// otherwise a symbol.
function quickSpec(v) {
  const m = /^(\d*)([a-z]?)$/i.exec(v);
  if (!m) return null;
  const cal = m[2] === 'M' || /^[dw]$/i.test(m[2]);
  if (!m[1] && !cal) return null;
  const u = !m[2] ? 'm' : cal ? m[2].toUpperCase() : m[2].toLowerCase();
  const spec = `${m[1] || 1}${u}`;
  return SPEC_RE.test(spec) ? spec : null;
}
$('#quick-in').addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { e.preventDefault(); quickClose(); return; }
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const v = $('#quick-in').value.trim(), sym = v.toUpperCase();
  // a result picked with the arrows; else a live chart's symbol, a timeframe, the best match, what was typed
  const r = quickSearch.active(true) || (!S.instruments[sym] && !quickSpec(v) && quickSearch.active());
  if (quickCompare) {
    const c = r?.symbol || (S.instruments[sym] ? sym : v && chartSym(v));
    quickClose();
    if (c) cmpAdd(c); else if (v) toast(`No chart for ${v}`);
    return;
  }
  quickClose();
  if (r) setSymbol(r.symbol);
  else if (S.instruments[sym]) setSymbol(sym);
  else if (quickSpec(v)) setSpec(quickSpec(v));
  else if (v && chartSym(v)) setSymbol(v);
  else if (v) toast(`"${v}" is not a symbol or timeframe here (5, 15m, 4h, D, W, M, 2000t, 5000v, 16r, ES, AAPL)`);
});
$('#quick-in').addEventListener('blur', () => setTimeout(() => { $('#quick').hidden = true; }, 100));

// Time left in the forming bar, under the last price (TradingView): time bars only, to the bar's end or the day's close:
// 16:00 CT, the RTH end on an RTH chart, or CME's holiday halt or early close (hello's holidays).
const fmtLeft = (ms) => {
  const s = Math.ceil(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h >= 24 ? `${Math.floor(h / 24)}d ${h % 24}h` : h ? `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
};
function dayCloseCt(day) {   // a trade date's close on this chart's clock, CT "HH:MM"; null: no session that day
  const h = S.holidays?.[day], g = S.instruments[S.symbol]?.calendar || 'equity';
  const at = (v) => (v && typeof v === 'object' ? v[g] || v.default : v);
  const end = S.rth && inst().rth ? inst().rth[1] : '16:00';
  if (h?.closed) return null;
  const t = at(h?.halt) || at(h?.close);
  return t && t < end ? t : end;
}
// A holiday halt (MLK Day, Thanksgiving, ...): the server counts that day's session toward the next day that trades.
function dayHalts(day) {
  const v = S.holidays?.[day]?.halt, g = S.instruments[S.symbol]?.calendar || 'equity';
  return !!(v && typeof v === 'object' ? v[g] || v.default : v);
}
function barCloseMs(b) {
  const cal = /^(\d+)([DWM])$/.exec(S.spec);
  if (cal && S.spec !== '1D') {   // the close of its last day: the Nth Monday-Friday day (closed ones count, as the
    // server groups them), the week's Friday or the month's last, back to one whose session is this bar's
    const n = Number(cal[1]), d = new Date(`${sessionDay(b.t)}T12:00:00Z`), iso = () => d.toISOString().slice(0, 10);
    const trades = () => d.getUTCDay() % 6 && dayCloseCt(iso()) != null && !dayHalts(iso());
    if (cal[2] === 'D') for (let k = 1; k < n;) { d.setUTCDate(d.getUTCDate() + 1); if (d.getUTCDay() % 6) k += 1; }
    else if (cal[2] === 'W') d.setUTCDate(d.getUTCDate() - (d.getUTCDay() + 6) % 7 + 7 * (n - 1) + 4);   // its Friday
    else d.setUTCMonth(d.getUTCMonth() + n, 0);
    for (let k = 0; k < 7 && !trades(); k += 1) d.setUTCDate(d.getUTCDate() - 1);   // Good Friday, a halt, a weekend
    return ctInputToMs(`${iso()}T${dayCloseCt(iso()) || '16:00'}`);
  }
  const close = ctInputToMs(`${sessionDay(b.t)}T${dayCloseCt(sessionDay(b.t)) || '16:00'}`);
  return S.spec === '1D' ? close : Math.min(b.t + specSeconds(S.spec) * 1000, close);
}
const countdown = {
  p: null,
  attached(p) { this.p = p; },
  detached() { this.p = null; },
  paneViews() { return []; },
  priceAxisViews() {
    const b = S.lastBar;
    if (!this.p || !S.cs.countdown || !b || !isTime() || S.replay || S.ext) return [];
    const left = barCloseMs(b) - Date.now(), y = this.p.series.priceToCoordinate(b.c);
    if (!(left > 0) || y == null) return [];
    return [{ coordinate: () => y + 18, text: () => fmtLeft(left), textColor: () => '#ffffff', backColor: () => (b.c >= b.o ? C.up : C.down), visible: () => true }];
  },
  update() { if (this.p) this.p.requestUpdate(); },
};
candles.attachPrimitive(countdown);
setInterval(() => { if (S.cs.countdown && document.visibilityState === 'visible') countdown.update(); }, 1000);

// Session breaks: a dashed line where each 17:00 CT session starts (not on 1D, where every bar is one).
// The Monday of a session's trading day ('YYYY-MM-DD'): which week it is.
function sessionWeek(ms) {
  const d = new Date(`${sessionDay(ms)}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
  return d.toISOString().slice(0, 10);
}
const sessionBreaks = {
  p: null,
  attached(p) { this.p = p; },
  detached() { this.p = null; },
  views: [{
    zOrder: () => 'bottom',
    renderer: () => ({
      draw: (target) => target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        const bars = S.bars, ts = chart.timeScale(), r = ts.getVisibleLogicalRange();
        if (!S.cs.sessions || !r || bars.length < 2 || calSpec()) return;
        ctx.save();
        ctx.strokeStyle = 'rgba(120, 123, 134, 0.45)';
        ctx.setLineDash([4, 4]);
        ctx.lineWidth = 1;
        const half = ts.options().barSpacing / 2;
        // Each session's open; zoomed out so far that they would be closer than 24 px, each week's (a Monday's
        // session) instead, and none when even those would be: lines every few pixels only hide the bars.
        const days = [];
        for (let i = Math.max(1, Math.floor(r.from)); i <= Math.min(bars.length - 1, Math.ceil(r.to)); i++) {
          if (sessionDay(bars[i].t) !== sessionDay(bars[i - 1].t)) days.push(i);
        }
        const gap = (list) => (list.length < 2 ? Infinity : (list.at(-1) - list[0]) / (list.length - 1) * ts.options().barSpacing);
        const monday = (i) => sessionWeek(bars[i].t) !== sessionWeek(bars[i - 1].t);
        let at = days;
        if (gap(days) < 24) at = days.filter(monday);
        if (gap(at) < 24) at = [];
        for (const i of at) {
          const x = ts.logicalToCoordinate(i);
          if (x == null) continue;
          ctx.beginPath();
          ctx.moveTo(Math.round(x - half) + 0.5, 0);
          ctx.lineTo(Math.round(x - half) + 0.5, mediaSize.height);
          ctx.stroke();
        }
        ctx.restore();
      }),
    }),
  }],
  paneViews() { return this.views; },
  update() { if (this.p) this.p.requestUpdate(); },
};
candles.attachPrimitive(sessionBreaks);

// The chart settings menu.
function renderCsMenu() {
  document.querySelectorAll('#cs-menu input[data-cs], #cs-menu select[data-cs]').forEach((el) => {
    const v = S.cs[el.dataset.cs];
    if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
  });
  $('#pl-op').textContent = `${S.cs.plOpacity}%`;
  $('#cs-menu .pl-opts').hidden = !S.cs.priceLine;
  $('#cs-menu [data-cs=plColor]').disabled = S.cs.plAuto;
  document.querySelectorAll('#cs-menu .seg[data-cs]').forEach((seg) => seg.querySelectorAll('button')
    .forEach((b) => b.classList.toggle('on', b.dataset.v === S.cs[seg.dataset.cs])));
}
function csChanged(k) {
  local.set('chartSet', JSON.stringify(S.cs));
  lookStore('cs', { ...S.cs });
  renderCsMenu();
  if (k === 'up' || k === 'down' || k === 'doji') {   // colors: candles, volume, footprint and MACD all take them
    C.up = S.cs.up; C.down = S.cs.down; C.doji = S.cs.doji;
    chartLayout();
    const c = [], v = [];
    for (const b of S.bars) { const [cb, vb] = toPoints(b); c.push(cb); v.push(vb); }
    candles.setData(c); vol.setData(v);
    indsBuild(); priceOnlyLayout();
    fpPrim.update();
  }
  if (['style', 'up', 'down', 'priceLine'].includes(k)) { barsLayout(); altSetData(); }
  if (/^pl[A-Z]/.test(k)) priceLineLayout(true);
  if (['gridLines', 'gridColor', 'textColor', 'xhairColor', 'fontSize', 'crosshair', 'rightBars', 'volume', 'bg'].includes(k)) chartLayout();
  if (k === 'rightBars') amtLayout();   // the room the AMT labels need stays
  if (k === 'watermark' || k === 'textColor') watermarkLayout();
  if (k === 'priceOnly') priceOnlyLayout();
  if (k === 'sessions') sessionBreaks.update();
  if (k === 'countdown') countdown.update();
  if (k === 'dataWin') legend(S.lastBar);
}
document.querySelectorAll('#cs-menu input[data-cs], #cs-menu select[data-cs]').forEach((el) => {
  if (el.type === 'range') el.oninput = () => { S.cs[el.dataset.cs] = Number(el.value); $('#pl-op').textContent = `${el.value}%`; priceLineLayout(true); };
  el.onchange = () => {
    const k = el.dataset.cs;
    if (el.type === 'checkbox') S.cs[k] = el.checked;
    else if (el.type === 'range' || el.tagName === 'SELECT') S.cs[k] = Number(el.value);
    else if (el.type === 'number') {
      const n = Math.round(Number(el.value));
      if (el.value === '' || !(n >= Number(el.min) && n <= Number(el.max))) { el.value = S.cs[k]; toast(`${el.min} to ${el.max}`); return; }
      S.cs[k] = n;
    } else S.cs[k] = el.value;
    csChanged(k);
  };
});
document.querySelectorAll('#cs-menu .seg[data-cs] button').forEach((b) => {
  b.onclick = () => { S.cs[b.parentElement.dataset.cs] = b.dataset.v; csChanged(b.parentElement.dataset.cs); };
});
document.querySelectorAll('#cs-theme button').forEach((b) => {
  b.onclick = () => { for (const [k, v] of Object.entries(CS_THEMES[b.dataset.t])) if (S.cs[k] !== v) { S.cs[k] = v; csChanged(k); } };
});
document.querySelectorAll('#cs-rth button').forEach((b) => { b.onclick = () => setRth(b.dataset.v === 'rth'); });
$('#st-cs').onclick = () => openCs(true);
// The symbol and timeframe, big and faint in the middle of the chart, as TradingView's watermark.
const watermark = L.createTextWatermark(chart.panes()[0], { horzAlign: 'center', vertAlign: 'center', lines: [] });
function watermarkLayout() {
  const on = S.cs.watermark && !!S.symbol;
  if (!on) { watermark.applyOptions({ visible: false, lines: [] }); return; }   // no layout read (a reflow) when off
  const big = Math.round(Math.min(72, Math.max(28, $('#chart').clientWidth / 9)));
  const color = `${rgbPrefix(S.cs.textColor)}0.12)`;
  watermark.applyOptions({ visible: true, lines: [{ text: S.symbol, color, fontSize: big, fontStyle: 'bold' },
    { text: `${specLabel(S.spec)}${S.rth && exch().rth ? ' · RTH' : ''}`, color, fontSize: Math.round(big / 2.6) }] });
}
// Apply to all charts: these settings on every tab of every chart, layout and workspace.
$('#cs-bars').onchange = (e) => setBarsOff(!e.target.checked);
$('#bars-eye').onclick = (e) => { e.stopPropagation(); setBarsOff(!S.barsOff); };
$('#legend-line').onclick = () => { if (!matchMedia('(hover: hover)').matches) $('#legend-line').classList.toggle('sel'); };   // a phone: tap for the eye
barsUi();
function csToAllTabs(cs) {   // this chart's tabs: each its own settings, all set to these
  for (const t of S.tabs) t.look = { ...(t.look || {}), cs: { ...cs } };
  saveTabs();
}
$('#cs-all').onclick = async () => {
  const value = JSON.stringify(S.cs);
  csToAllTabs(S.cs);
  if (TK_CELL !== null) { window.parent.postMessage({ tk: true, type: 'all', key: 'chartSet', value }, location.origin); return; }
  if (!workspaces) return;
  const n = await workspaces.applyAll('chartSet', value);
  toast(`Chart settings applied to every chart${n ? ` and ${n} other workspace${n > 1 ? 's' : ''}` : ''}`);
};
$('#cs-reset').onclick = () => {
  const old = S.cs;
  S.cs = { ...CS_DEFAULTS };
  for (const k of Object.keys(S.cs)) if (old[k] !== S.cs[k]) csChanged(k);
  local.set('chartSet', '{}');
};
function openCs(open) {
  $('#cs-menu').hidden = !open;
  $('#cs-btn').setAttribute('aria-expanded', String(open));
  if (open) { openSt(false); openTf(false); renderCsMenu(); }
}
$('#cs-btn').onclick = (e) => { e.stopPropagation(); openCs($('#cs-menu').hidden); };
$('#cs-menu').onclick = (e) => e.stopPropagation();
document.addEventListener('pointerdown', (e) => { if (!$('#cs-menu').hidden && !e.target.closest('#cs')) openCs(false); }, true);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') openCs(false); });

// Screenshot: the chart with a title line (symbol, bar type, time, the legend), saved as a PNG.
// A picture of the chart: the legend and time above it, the logo and "Powered by Tatanka Charts" on the right (Branding).
// With the panes' top layer (drawings, alert lines, fills, big trades and the studies in front of the bars), without the
// crosshair.
function chartImage() {
  const shot = chart.takeScreenshot(true), pad = 26, out = document.createElement('canvas');
  out.width = shot.width; out.height = shot.height + pad;
  const ctx = out.getContext('2d');
  ctx.fillStyle = css('--bg'); ctx.fillRect(0, 0, out.width, out.height);
  ctx.drawImage(shot, 0, pad);
  ctx.fillStyle = css('--fg'); ctx.font = '13px ' + css('--font');
  ctx.textBaseline = 'middle';
  ctx.fillText(`${$('#legend').textContent}   ${ax.full.format(Date.now())} ${zs(Date.now())}`, 8, pad / 2);
  const mark = $('#brand img');
  let sign = brand.powered ? 'Powered by Tatanka Charts' : '';
  ctx.fillStyle = css('--accent'); ctx.font = '600 12px ' + css('--font'); ctx.textAlign = 'right';
  ctx.fillText(sign, out.width - 8, pad / 2);
  if (mark?.complete && mark.naturalWidth) {
    const h = 18, w = mark.naturalWidth * h / mark.naturalHeight, tw = sign ? ctx.measureText(sign).width + 6 : 0;
    ctx.drawImage(mark, out.width - 8 - tw - w, (pad - h) / 2, w, h);
  }
  return out;
}
function savePicture() {
  chartImage().toBlob((blob) => {
    const a = document.createElement('a'), stamp = ax.input.toInput(Date.now()).replace(/[-:T]/g, '');
    a.href = URL.createObjectURL(blob);
    a.download = `${S.symbol}_${S.spec}_${stamp}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }, 'image/png');
}
// Post to Discord: the picture and a caption to a channel set in Connections > Discord: chart posts (the server
// holds the webhooks; the browser never sees them). More than one channel: the menu has one item each.
let shareChannels = [];
async function shareLoad() { try { shareChannels = (await api('GET', '/api/share/channels')).channels; } catch { /* the next connections change */ } }
const postItems = (label = 'Post to Discord…') => (shareChannels.length > 1
  ? shareChannels.map((ch) => ({ label: `Post to ${ch.name}…`, fn: () => postDiscord(ch) }))
  : [{ label, fn: () => postDiscord(shareChannels[0]) }]);
async function postDiscord(ch) {
  const img = chartImage().toDataURL('image/png');   // taken now, before the caption box covers anything
  const caption = await ask({ title: `Post chart to ${shareChannels.length > 1 && ch ? ch.name : 'Discord'}`, ok: 'Post', placeholder: 'Caption (optional)',
    value: `${$('#legend').textContent} · ${ax.md.format(Date.now())} ${zs(Date.now())}`, maxLength: 1800, optional: true });   // the server's caption
  if (caption == null) return;
  toast('Posting…');
  try { await api('POST', '/api/share/discord', { caption, image: img, channel: ch?.id || 'discord_charts' }); toast(`Posted to ${ch?.name || 'Discord'}`); } catch (e) { fail(e); }
}
$('#shot-btn').onclick = (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  showMenu(r.left, r.bottom + 4, [
    { label: 'Save picture', fn: savePicture, key: 'Alt+S' },
    ...postItems(),
  ]);
};
// Full screen (desktop browsers; an installed phone app is full screen already).
if (!document.fullscreenEnabled) $('#fs-btn').hidden = true;
$('#fs-btn').onclick = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen()).catch(() => {});
document.addEventListener('fullscreenchange', () => $('#fs-btn').classList.toggle('on', !!document.fullscreenElement));

barsLayout();
chartLayout();
priceOnlyLayout();
psUi();

// ---- server connection ---------------------------------------------------------------------
function send(m) { if (S.ws?.readyState === WebSocket.OPEN) S.ws.send(JSON.stringify(m)); }
// A CME future not charted here yet (RTY, ZN...): opening it adds it on the server, live at once; NinjaTrader sends its
// trades and its last 14 days. If that fails, it charts from free delayed data like any other symbol.
async function futureAdd(sym) {
  loadNote(`adding ${sym}: NinjaTrader sends its trades and last 14 days…`);
  try {
    S.instruments[sym] = await api('POST', '/api/instruments/add', { symbol: sym });
    delete S.catalog[sym];
    toast(`${sym} added: live from NinjaTrader, its last 14 days filling in`);
  } catch (e) {
    futureAdd.failed.add(sym);
    toast(`${sym}: ${e.message}`);
  }
  if (S.symbol !== `${sym}1!`) return;   // switched to something else meanwhile
  if (S.instruments[sym]) { S.symbol = sym; local.set('sym', sym); onSymbolChanged(); tabSync(); }   // live under its root: its tick size
  renderSymBtn(); subscribe();   // or, failed: RTY1! from free delayed data
}
futureAdd.failed = new Set();
function subscribe() {
  if (TK_MULTI) {   // the charts are in the frames; this page only needs the Plan tab's data for the active one
    tapeSymbol();
    S.ifData = null;
    S.ifRows = [];
    ifRequest();
    renderPlanIf();
    return;
  }
  if (S.subId) send({ op: 'unsub', id: S.subId });
  S.subId += 1;
  clearTimeout(extTimer);
  extStop?.abort();   // a free-data chart's answer on its way: not this chart's any more
  const fut = WL_FUT.exec(S.symbol);
  if (isExtSym(S.symbol) && fut && S.catalog?.[fut[1]] && !futureAdd.failed.has(fut[1])) {
    if (S.bars.length && S.loadedSym !== S.symbol) barsClear();
    futureAdd(fut[1]);
    return;
  }
  const ext = isExtSym(S.symbol);   // no live feed here: Yahoo or FRED history
  S.ext = !ext ? null : S.ext?.symbol === S.symbol ? S.ext : { symbol: S.symbol };
  if (ext) {
    const sp = extSpecFor(S.spec, extSource(S.symbol));
    if (sp !== S.spec) { S.spec = specForced = sp; local.set('spec', sp); }
    delete S.ext.source; delete S.ext.feed;   // until its answer says which source has it (the legend says loading)
  }
  // Never another symbol's bars and last price under this one's name (the header's change is of this symbol's close)
  // while its bars are on their way; a free-data chart's answer can take a while, so not another timeframe's or
  // session's either. A live chart keeps its own bars until the other timeframe comes, at once.
  if (S.bars.length && (S.loadedSym !== S.symbol || (ext && S.loadedKey !== viewKey()))) barsClear();
  renderTf();
  renderStudiesMenu();
  $('#replay-btn').disabled = ext;
  if (!ext) { vol.applyOptions({ visible: S.cs.volume }); barsLayout(); }   // back from a FRED line
  loadNote('loading…', `Loading ${S.symbol} ${specLabel(S.spec)}…`);
  watermarkLayout();
  if (ext) extLoad();
  else if (S.replay) replayLoad();
  else {   // the bars shown are the old ones (replay's, another chart's) until the history comes: no footprint for them
    S.liveWait = true;
    send({ op: 'sub', id: S.subId, symbol: S.symbol, spec: S.spec, rth: S.rth });
  }
  if (drawings.state.loadedFor !== S.symbol) drawings.load();
  tapeSymbol();
  amtRequest();
  tpoRequest(true);
  vpAll(VP_KINDS.some((k) => S.vp[k] && S.vp[k].symbol !== S.symbol));
  gapRequest(true);
  S.ifData = null;
  S.ifRows = [];
  amtLayout();
  ifRequest();
  renderPlanIf();
}

function onMessage(m) {
  switch (m.type) {
    case 'hello':
      versionCheck(m);
      updateNote(m.update);
      S.instruments = m.instruments; S.catalog = m.catalog || {}; S.levels = m.levels; S.holidays = m.holidays || {};
      S.onlyFutures = new Set(m.only_futures || []);
      S.alerts = m.alerts; S.events = m.events;
      m.quotes.forEach((q) => { S.quotes[q.s] = q; });
      watch.setDoc(m.watchlists);
      S.symbol = chartSym(S.symbol) || Object.keys(S.instruments)[0];
      if (!SPEC_RE.test(S.spec)) S.spec = '5m';
      tabSync();
      buildHeader(); renderAlerts(); renderEvents(); onSymbolChanged();
      Promise.all([loadAmtServer(), loadGapAlerts()]).then(moveSharedOut);
      loadInd();
      drawings.state.loadedFor = null;   // reconnected: reload, edits may have been missed
      if (workspaces && !workspaces.started) { workspaces.started = true; workspaces.start(); }
      else if (workspaces) workspaces.recheck();   // reconnected: a workspace saved meanwhile was not heard
      tapeWant = '';   // a new connection: ask again
      if (journalOpen() && !$('#jr-sym').checked) journalLoad();   // reconnected: fills made meanwhile were not heard (this symbol only: subscribe() asks)
      if (S.st.econ || calOpen()) calLoad();
      tplLoad();
      shareLoad();
      setupCheck();
      cellPost('state');
      if (TK_MULTI && workspaces.activeState()) hostActive(workspaces.activeState());   // heard before this hello
      notifyStatus(m.notify);
      subscribe();
      break;
    case 'update': updateNote(m); break;
    case 'instruments': {   // a future added (here, on another device, or in Connections): live without a reload
      const was = isExtSym(S.symbol);
      S.instruments = m.instruments; S.catalog = m.catalog || {};
      appConfig = m.config;
      loadGapAlerts();   // one put back runs the gap alerts kept for it (or says why not): its tick and Studies line
      watch.render();   // a list's bare root of it ("RTY") is that future now, its quote RTY1!'s
      const fut = WL_FUT.exec(S.symbol || '');
      if (!TK_MULTI && fut && S.instruments[fut[1]]) { S.symbol = fut[1]; local.set('sym', fut[1]); onSymbolChanged(); renderSymBtn(); subscribe(); tabSync(); }   // added on another device
      else if (!TK_MULTI && was && !isExtSym(S.symbol)) { onSymbolChanged(); renderSymBtn(); subscribe(); }
      break;
    }
    case 'history':
      if (cmpHistory(m)) break;
      if (m.id !== S.subId) break;
      if (!S.viewWant && S.liveKey === viewKey()) S.viewWant = viewSnap();   // the same bars again (a reconnect, a gap fill)
      applyHistory(m);
      S.liveKey = viewKey();
      break;
    case 'bars': if (m.id === S.subId) applyBars(m); else cmpBars(m); break;
    case 'amt': if (m.id === S.subId && S.st.amt) amtMerge(m); break;
    case 'amt_reset': if (m.id === S.subId) { amtSent = 'reset'; loadAmtServer(); } break;   // the starting settings changed: asked again with them
    case 'amt_settings':   // the starting settings, a symbol's alert settings or its alert switch changed (here, on another chart or device)
      amtHeard += 1;
      S.amtServer = { ...S.amtServer, overrides: m.overrides || {}, symbols: m.symbols || {}, alerts: m.alerts || {} };
      renderAmtMenu();
      amtFollow();   // a chart on its own settings: they sit on its symbol's starting ones
      indDlg.refresh();
      break;
    case 'gap_alerts':   // a symbol's gap alerts or its alert switches changed (each symbol's, so an ES chart shows ES's)
      gapAlertsPut += 1;   // a GET out meanwhile answers with them from before: asked again
      S.gapAlerts = m.alerts || {};
      if (m.switches) S.gapSwitches = m.switches;
      renderGapMenu();
      indDlg.refresh();
      break;
    case 'reload': if (m.id === S.subId && !S.replay) { toast(`${S.symbol}: history filled in from NinjaTrader`); subscribe(); } else cmpReload(m.id); break;
    case 'quotes': m.quotes.forEach((q) => { S.quotes[q.s] = q; }); watch.onQuotes(m.quotes); extQuote(m.quotes); header(); break;
    case 'watchlists': watch.setDoc(m.watchlists); break;
    case 'only_futures': S.onlyFutures = new Set(m.only_futures); break;   // the listings came in: a bare ZB charts as ZB1!
    case 'levels': if (m.levels) S.levels[m.symbol] = m.levels; else delete S.levels[m.symbol]; if (m.symbol === S.symbol) { drawLevels(); loadPlanLevels(); } break;
    case 'alerts': S.alerts = m.alerts; renderAlerts(); drawings.update(); break;
    case 'drawing': case 'drawing_deleted': drawings.onMessage(m); break;
    case 'ifthen':   // a block saved on another device (also while ifRequest's answer is on its way)
      if (m.symbol === S.symbol) {
        ifHeard += 1;
        S.ifData = { ...(S.ifData || {}), rows: m.rows, updated: m.updated };
        S.ifRows = biasParse(m.rows);
        amtLayout();
        ifPrim.update();
        renderPlanIf(false);
        renderIfInfo();
      }
      break;
    case 'indicators':   // settings saved on this or another device
      indSaved(m);
      indHeard?.push(m);
      indCache();
      indApply();
      indDlg.refresh();
      if (m.key === 'lines' && m.symbol === S.symbol) renderPlanLines(false);
      break;
    case 'amt_levels': if (m.symbol === S.symbol) { S.lvStatus = m.status; renderPlanLevels(false); } break;
    case 'alert':
      S.events.unshift(m.event); renderEvents();
      if (TK_CELL !== null) break;   // the page around the charts announces it once
      if (m.event.alert_id != null) firedShow(m.event); else toast(m.event.message);
      navigator.vibrate?.(200);
      chime(alertSound(m.event));
      break;
    case 'workspaces': if (workspaces) workspaces.heard(m); break;
    case 'calendar': if (calOpen() || S.st.econ || newsOn()) calLoad(); break;
    case 'templates': tplLoad(); break;
    case 'branding': applyBrand(m.branding); break;
    case 'connections': notifyStatus(m.notify); shareLoad(); if (!$('#cn-dlg').hidden) cnPoll(); break;
    case 'trades': onTrades(m); break;
    case 'fill': onFill(m); break;
    case 'error':
      if (m.op === 'sub' && m.id === S.subId) { splashSay(`The chart didn't load: ${m.message}`, true); loadNote(m.message); }
      toast(m.message);
      break;
  }
}

// ---- alert sounds: a short chime when an alert fires, made here (no sound files), on this device -------------------
// Price and drawn-line alerts: two notes; levels, studies, indicators, AMT and gaps: three rising; news warnings: four.
const TONES = { price: [[880, 0, 0.14], [1318.5, 0.15, 0.3]], level: [[659.3, 0, 0.12], [987.8, 0.13, 0.12], [1318.5, 0.26, 0.3]],
  news: [[523.3, 0, 0.1], [523.3, 0.16, 0.1], [523.3, 0.32, 0.1], [784, 0.48, 0.35]] };
const soundOn = () => local.get('sndOn', '1') === '1';
let actx = null;
function audioCtx() {   // browsers let a page play sound only after a click or tap on it: made on the first one
  if (!actx) { const A = window.AudioContext || window.webkitAudioContext; if (!A) return null; actx = new A(); }
  if (actx.state === 'suspended') actx.resume().catch(() => {});
  return actx;
}
document.addEventListener('pointerdown', () => { if (soundOn()) audioCtx(); }, { capture: true, passive: true });
function alertSound(ev) {
  if (ev.alert_id == null) return / impact\)/.test(ev.message || '') ? 'news' : 'level';   // the calendar's warnings; AMT, gaps
  const a = S.alerts.find((x) => x.id === ev.alert_id);
  return !a || a.kind === 'price' || a.kind === 'line' ? 'price' : 'level';
}
function chime(kind, force = false) {
  if (!force && !soundOn()) return;
  const a = audioCtx(), vol = Number(local.get('sndVol', '60')) / 100;
  if (!a || vol <= 0) return;
  for (const [f, at, len] of TONES[kind] || TONES.price) {
    const o = a.createOscillator(), g = a.createGain(), t = a.currentTime + at;
    o.type = 'sine';
    o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.5 * vol, t + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t + len);
    o.connect(g).connect(a.destination);
    o.start(t);
    o.stop(t + len + 0.02);
  }
}
$('#snd-on').checked = soundOn();
$('#snd-vol').value = local.get('sndVol', '60');
$('#snd-on').onchange = (e) => { local.set('sndOn', e.target.checked ? '1' : '0'); if (e.target.checked) chime('price'); };
$('#snd-vol').onchange = (e) => { local.set('sndVol', e.target.value); chime('price', true); };
let sndDemo = 0;
$('#snd-test').onclick = () => { chime(['price', 'level', 'news'][sndDemo++ % 3], true); };

function setConn(ok) { const d = $('#conn'); d.classList.toggle('ok', ok); $('#conn-btn').title = `${ok ? 'Live' : 'Disconnected'} · Connections: data feeds, keys, alert channels`; }
function notifyStatus(ch) {
  const names = { app: 'app notifications', discord: 'Discord', ntfy: 'ntfy' };
  $('#notify-status').textContent = ch.length ? `Alerts go to: ${ch.map((c) => names[c] || c).join(', ')}` : 'No alert channels set: alerts show here only.';
}

// ---- Connections: the data feeds, data keys and alert channels, set here (saved on the server) or in .env ---------
const cn = { list: [], timer: 0 };
function cnEl(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'dataset') Object.assign(e.dataset, v); else if (k in e) e[k] = v; else e.setAttribute(k, v);
  }
  e.append(...kids.filter((k) => k !== null && k !== undefined && k !== false));
  return e;
}
async function cnOpen() {
  $('#cn-dlg').hidden = false;
  try { cnRender((await api('GET', '/api/connections')).connections); } catch (e) { fail(e); }
  clearInterval(cn.timer);
  cn.timer = setInterval(cnPoll, 3000);
}
function cnClose() { $('#cn-dlg').hidden = true; clearInterval(cn.timer); }
async function cnPoll() {
  if ($('#cn-dlg').hidden) { clearInterval(cn.timer); return; }
  try {
    const list = (await api('GET', '/api/connections')).connections;
    const ids = (l) => l.map((x) => x.id + (x.install ? x.install + x.status : '')).join();   // a plugin added or removed, a package installed: drawn again, unless you're typing
    if (ids(list) !== ids(cn.list) && !$('#cn-list').contains(document.activeElement) && !$('#cn-list .cn-card.draft')) { cnRender(list); cnDots(); return; }
    list.forEach(cnHead);
    for (const c of list) { const i = cn.list.findIndex((x) => x.id === c.id); if (i >= 0) cn.list[i] = { ...cn.list[i], state: c.state, status: c.status }; }
    cnDots();
  } catch { /* the next poll */ }
}
function cnHead(c) {   // the parts that change by themselves: state, status, On, its info line
  const card = $(`#cn-list .cn-card[data-id="${c.id}"]:not(.draft)`);   // a draft keeps what is being typed and ticked
  if (!card) return;
  card.querySelector('.cn-dot').className = `cn-dot ${c.state}`;
  card.querySelector('.cn-status').textContent = c.status;
  const info = card.querySelector('.cn-info');
  if (info) info.textContent = c.info || '';
  const on = card.querySelector('.cn-on input');
  const f = c.fields.find((x) => x.key === 'enabled');
  if (on && f && document.activeElement !== on) on.checked = !!f.value;
}
// One list per kind (Futures feed, Quotes, Data, Alerts and Discord, App), each folding open or shut; folded, it shows
// what is on. Its + adds what isn't listed yet, or another Discord channel, chart-post channel or ntfy topic.
const cnOpenGroups = () => { try { return JSON.parse(localStorage.getItem('cnOpen') || '[]'); } catch { return []; } };
function cnRender(list) {
  cn.list = list;
  const box = $('#cn-list'), open = cnOpenGroups(), top = box.scrollTop;
  box.replaceChildren();
  for (const g of [...new Set(list.map((c) => c.group))]) {
    const items = list.filter((c) => c.group === g), shown = items.filter((c) => c.listed);
    const add = cnAddable(items);
    const plus = add.length ? cnEl('button', { type: 'button', className: 'cn-add', textContent: '+', title: `Add to ${g}`, 'aria-label': `Add to ${g}`,
      onclick: (e) => { e.preventDefault(); e.stopPropagation(); const r = e.currentTarget.getBoundingClientRect(); showMenu(r.left, r.bottom + 4, cnAddable(items)); } }) : null;
    const sec = cnEl('details', { className: 'cn-sec', dataset: { group: g } },
      cnEl('summary', {}, cnEl('b', { textContent: g }), cnEl('span', { className: 'cn-dots' }), plus),
      cnEl('div', { className: 'cn-sec-body' }, ...shown.map((c) => cnCard(c)), shown.length ? null : cnEl('p', { className: 'cn-about', textContent: 'Nothing here yet: + adds one.' })));
    sec.open = open.includes(g) || shown.some((c) => c.state === 'error');   // one failing: open, without remembering it
    sec.querySelector('summary').addEventListener('click', () => {   // opened or shut by hand: kept that way
      const now = cnOpenGroups().filter((x) => x !== g);
      try { localStorage.setItem('cnOpen', JSON.stringify(sec.open ? now : [...now, g])); } catch { /* private mode */ }
    });
    box.append(sec);
  }
  cnDots();
  box.scrollTop = top;
}
function cnDots() {   // on each list's fold: what is on, with its state
  for (const sec of $('#cn-list').querySelectorAll('.cn-sec')) {
    const on = cn.list.filter((c) => c.group === sec.dataset.group && c.listed && c.state !== 'off');
    sec.querySelector('.cn-dots').replaceChildren(...(on.length ? on.map((c) => cnEl('span', { title: c.status },
      cnEl('i', { className: `cn-dot ${c.state}` }), c.title)) : [cnEl('span', { className: 'dim', textContent: 'none on' })]));
  }
}
function cnAddable(items) {   // what a list's + offers
  const out = [];
  for (const c of items) {
    if (c.extra) continue;
    if (c.unavailable) out.push({ label: `${c.title} (not built yet)`, disabled: true, fn() {} });
    else if (!c.listed && !$(`#cn-list .cn-card[data-id="${c.id}"]`)) out.push({ label: c.title, fn: () => cnDraft(c) });
    else if (c.multi && c.state !== 'off' && items.filter((x) => x.type === c.type).length < 12) out.push({ label: c.multi, fn: () => cnDraft(c, true) });
  }
  return out;
}
function cnDraft(c, another = false) {   // a new card at the top of its list: fill it in, Test, Save (or Cancel)
  const sec = $(`#cn-list .cn-sec[data-group="${c.group}"]`);
  if (!sec) return;
  sec.open = true;
  const item = another ? { ...c, id: '', title: c.multi, name: '', extra: true, listed: false, state: 'off', status: 'New',
    fields: c.fields.filter((f) => f.key !== 'enabled').map((f) => ({ ...f, set: false, saved: false, env: false, source: '' })) } : c;
  const card = cnCard(item, true);
  sec.querySelector('.cn-sec-body > p.cn-about')?.remove();
  sec.querySelector('.cn-sec-body').prepend(card);
  card.scrollIntoView({ block: 'nearest' });
  card.querySelector('input:not([type=checkbox]), select')?.focus();
}
async function cnRefresh(id, note) {   // the lists again (one added or removed), a note on that card
  try { cnRender((await api('GET', '/api/connections')).connections); } catch (e) { fail(e); return; }
  const m = id && $(`#cn-list .cn-card[data-id="${id}"] .cn-msg`);
  if (m) { m.className = 'cn-msg ok'; m.textContent = note; m.closest('.cn-card').scrollIntoView({ block: 'nearest' }); }
}
async function cnRemove(c) {
  const what = c.extra ? `${c.title} (${c.kind})` : c.title;
  if (await ask({ title: `${c.extra ? 'Delete' : 'Remove'} ${what}?`, input: false, ok: c.extra ? 'Delete' : 'Remove', danger: true,
    message: c.extra ? 'Its URL is deleted from this server.' : 'Its keys and URLs saved here are cleared and it is turned off.' }) == null) return;
  try { await api('DELETE', `/api/connections/${c.id}`); toast(`${c.extra ? 'Deleted' : 'Removed'} ${what}`); await cnRefresh(); } catch (e) { fail(e); }
}
function cnCard(c, draft = false) {
  const on = c.fields.find((f) => f.key === 'enabled');
  const card = cnEl('section', { className: `cn-card${draft ? ' draft' : ''}`, dataset: { id: c.id || 'new' } });
  const toggle = on && !c.locked ? cnEl('label', { className: 'cn-on', title: 'On or off; applies at once' },
    cnEl('input', { type: 'checkbox', checked: !!on.value, onchange: (e) => (draft ? null : cnSave(c.id, { enabled: e.target.checked })) }), ' On') : null;
  card.append(cnEl('div', { className: 'cn-hd' }, cnEl('span', { className: `cn-dot ${c.state}` }), cnEl('b', { textContent: c.title }),
    c.multi && (c.name || c.extra) && !draft ? cnEl('small', { className: 'cn-kind', textContent: c.kind }) : null,
    cnEl('span', { className: 'cn-status', textContent: c.status }), toggle));
  card.append(cnEl('p', { className: 'cn-about', textContent: c.about }));
  if (c.locked) card.append(cnEl('p', { className: 'cn-lock', textContent: c.locked }));
  if (c.locked && c.id === 'ninjatrader') card.append(cnEl('div', { className: 'cn-ft' }, cnEl('button', { type: 'button', textContent: 'Leave the simulator…', onclick: leaveSim })));
  if (c.info) card.append(cnEl('p', { className: 'cn-about cn-info', textContent: c.info }));
  if (c.addon) card.append(cnAddon(c.addon));
  const named = c.multi && (c.extra || c.name || cn.list.some((x) => x.type === c.type && x.extra));   // a name once there are two
  const fields = [...(named ? [{ key: 'name', type: 'text', label: 'Name', value: c.name || '',
    note: c.type === 'discord_charts' ? 'shown in the Post to Discord menu' : 'shown in these lists' }] : []),
    ...c.fields.filter((f) => f.key !== 'enabled')];
  if (c.install?.length) card.append(cnInstall(c));
  if (c.unavailable) return card;
  if (c.push) { card.append(pushPart(c)); return card; }
  if (c.backup) { card.append(backupPart(c)); return card; }
  if (c.login) { card.append(loginPart(c)); return card; }
  if (c.branding) { card.append(brandingPart(c)); return card; }
  if (c.instruments) { card.append(instrumentsPart(c)); return card; }
  if (c.symbol_list) { card.append(symbolsPart(c)); return card; }
  const inputs = {};
  for (const f of fields) {
    let input;
    const extra = [];
    if (f.type === 'choice') {
      input = cnEl('select', {}, ...f.options.map(([v, label]) => cnEl('option', { value: String(v), textContent: label, selected: v === f.value })));
    } else if (f.type === 'secret') {
      const where = f.source === 'app' ? 'saved here' : f.source === '.env' ? 'from .env' : '';
      input = cnEl('input', { type: 'password', autocomplete: 'off', spellcheck: false, placeholder: f.set ? `set (${where}) · paste to replace` : 'not set' });
      if (f.set && !draft) extra.push(cnEl('button', { type: 'button', className: 'link', textContent: 'Clear', title: 'No value, whatever .env says',
        onclick: () => cnSave(c.id, { [f.key]: '' }) }));
      if (f.saved && f.env) extra.push(cnEl('button', { type: 'button', className: 'link', textContent: 'Use .env', title: "Forget the one saved here; .env's applies",
        onclick: () => cnSave(c.id, { [f.key]: null }) }));
    } else {
      input = cnEl('input', { value: f.value ?? '', inputMode: f.type === 'int' ? 'numeric' : 'text', autocomplete: 'off', spellcheck: false });
      if (f.type === 'int') { input.type = 'number'; input.min = f.min; input.max = f.max; input.step = 1; }
      if (f.key === 'name') { input.maxLength = 40; input.placeholder = `e.g. ${c.type === 'discord_charts' ? 'Charts room' : 'Scalps'}`; }
    }
    input.setAttribute('aria-label', `${c.title} ${f.label}`);
    inputs[f.key] = { f, input };
    card.append(cnEl('label', { className: 'cn-fld' }, cnEl('span', { textContent: f.label }),
      cnEl('span', { className: 'cn-in' }, input, ...extra),
      f.note ? cnEl('small', { textContent: f.note }) : null));
  }
  const msg = cnEl('span', { className: 'cn-msg' });
  const changed = () => {   // what was typed or picked, as the server takes it
    const out = {};
    for (const [k, { f, input }] of Object.entries(inputs)) {
      const v = input.value.trim();
      if (f.type === 'secret') { if (v) out[k] = v; continue; }
      const val = f.type === 'int' ? Number(v) : f.type === 'choice' ? (typeof f.value === 'number' ? Number(v) : v) : v;
      if (draft || val !== f.value) out[k] = val;   // a new one: all as shown (a plugin's may start from another card's)
    }
    if (draft && on) out.enabled = card.querySelector('.cn-on input').checked;
    return out;
  };
  const url = c.fields.find((f) => f.type === 'secret');
  const test = cnEl('button', { type: 'button', textContent: 'Test', onclick: async () => {
    const body = changed();
    if (draft && c.extra && url && !body[url.key]) { msg.className = 'cn-msg bad'; msg.textContent = `Paste the ${url.label} first`; return; }
    msg.className = 'cn-msg'; msg.textContent = 'Testing…'; test.disabled = true;
    try {
      const r = await api('POST', `/api/connections/${c.id || c.type}/test`, body);
      msg.className = `cn-msg ${r.ok ? 'ok' : 'bad'}`; msg.textContent = r.message;
    } catch (e) { msg.className = 'cn-msg bad'; msg.textContent = e.message; }
    test.disabled = false;
  } });
  const save = Object.keys(inputs).length || draft ? cnEl('button', { type: 'button', className: 'primary', textContent: draft ? 'Add' : 'Save', onclick: async () => {
    const body = changed();
    if (draft && c.extra) {   // another channel: the server gives it its id
      try { const n = await api('POST', '/api/connections', { type: c.type, ...body }); await cnRefresh(n.id, 'Added'); } catch (e) { msg.className = 'cn-msg bad'; msg.textContent = e.message; }
      return;
    }
    if (!Object.keys(body).length) { msg.className = 'cn-msg'; msg.textContent = 'Nothing changed'; return; }
    if (draft) { try { await api('PUT', `/api/connections/${c.id}`, body); await cnRefresh(c.id, 'Added'); } catch (e) { msg.className = 'cn-msg bad'; msg.textContent = e.message; } return; }
    cnSave(c.id, body);
  } }) : null;
  const cancel = draft ? cnEl('button', { type: 'button', textContent: 'Cancel', onclick: () => { card.remove(); cnDots(); } }) : null;
  const remove = !draft && (c.extra || !c.main) ? cnEl('button', { type: 'button', className: 'link cn-rm',   // any shown that isn't a main one
    textContent: c.extra ? 'Delete' : 'Remove from list', onclick: () => cnRemove(c) }) : null;
  card.append(cnEl('div', { className: 'cn-ft' }, remove, msg, cancel, test, save));
  return card;
}
function cnAddon(a) {   // the NinjaTrader add-on: its download, and the steps when the one running is older
  const link = cnEl('a', { href: a.file, download: 'TatankaChartFeed.cs', textContent: `Download the add-on (version ${a.version})` });
  if (!a.old) return cnEl('p', { className: 'cn-about' }, link);
  return cnEl('div', { className: 'cn-lock' }, cnEl('b', { textContent: 'The add-on in NinjaTrader is older than this server. To update it:' }),
    cnEl('ol', {}, cnEl('li', { textContent: 'On the PC with NinjaTrader, double-click update.cmd in the Tatanka Charts folder (it copies it in). Or:' }),
      cnEl('li', {}, link, cnEl('span', { textContent: ', then put it in Documents\\NinjaTrader 8\\bin\\Custom\\AddOns (replace the old one).' })),
      cnEl('li', { textContent: 'In NinjaTrader: New > NinjaScript Editor, press F5. This card turns green when it reconnects.' })));
}
function cnInstall(c) {   // the Python packages its plugin needs: pip on the server, then it starts by itself
  const busy = c.status.startsWith('Installing') && !c.status.includes('failed');
  const label = `Install ${c.install.map((x) => x.split(/[<>=!~[]/)[0]).join(', ')}`;   // the names; the versions are in the status
  const btn = cnEl('button', { type: 'button', className: 'primary', disabled: busy, textContent: busy ? 'Installing…' : label,
    onclick: async () => {
      btn.disabled = true; btn.textContent = 'Installing…';
      try { await api('POST', `/api/connections/${c.id}/install`); cnPoll(); } catch (e) { btn.disabled = false; btn.textContent = label; fail(e); }
    } });
  return cnEl('div', { className: 'cn-ft' }, cnEl('span', { className: 'cn-msg', textContent: 'Installed on this server; stays through updates.' }), btn);
}
// ---- branding (Connections > Branding): the app's name and logo, and "Powered by" on chart pictures, from the server
const BRAND = { name: 'Tatanka', powered: true, logo: false, logo_v: 0 };
let brand = { ...BRAND };
const brandLogo = (b) => (b.logo ? `/api/branding/logo?v=${b.logo_v}` : 'icons/mark.png');
function applyBrand(b) {
  brand = { ...BRAND, ...b };
  const a = $('#brand'), img = a.querySelector('img'), own = brand.name !== BRAND.name;
  a.querySelector('span').textContent = brand.name.toUpperCase();
  a.title = img.alt = own ? brand.name : 'Tatanka Trading';
  if (img.getAttribute('src') !== brandLogo(brand)) img.src = brandLogo(brand);
  document.title = `${brand.name} Charts`;
  document.querySelector('meta[name="apple-mobile-web-app-title"]')?.setAttribute('content', brand.name);
  $('#login-h').textContent = `${brand.name} Charts`;
}
async function brandLoad() {   // the last one seen at once (no flash of the built-in one), then the server's
  try { const b = JSON.parse(localStorage.getItem('brand') || 'null'); if (b) applyBrand(b); } catch { /* private mode */ }
  try {
    const b = await (await fetch('/api/branding')).json();
    applyBrand(b);
    try { localStorage.setItem('brand', JSON.stringify(b)); } catch { /* private mode */ }
  } catch { /* the built-in one stays */ }
}
function brandingPart(c) {
  const b = c.branding, box = cnEl('div', { className: 'cn-push' });
  const msg = cnEl('span', { className: 'cn-msg' });
  const say = (text, ok) => { msg.className = `cn-msg ${ok ? 'ok' : 'bad'}`; msg.textContent = text; };
  const text = (v, label, max) => cnEl('input', { value: v, maxLength: max, autocomplete: 'off', spellcheck: false, 'aria-label': label });
  const row = (label, input, note) => cnEl('label', { className: 'cn-fld' }, cnEl('span', { textContent: label }),
    cnEl('span', { className: 'cn-in' }, ...[input].flat()), note ? cnEl('small', { textContent: note }) : null);
  const name = text(b.name, 'Name', 24), powered = cnEl('input', { type: 'checkbox', checked: b.powered });
  const file = cnEl('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp', hidden: true });
  const preview = cnEl('img', { className: 'cn-logo', src: brandLogo(b), alt: '' });
  let logo;   // undefined: as it is; a data URL: the new one; null: the built-in one
  file.onchange = () => {
    const f = file.files[0];
    if (!f) return;
    if (f.size > 200_000) { say('The logo can be up to 200 KB', false); file.value = ''; return; }
    const r = new FileReader();
    r.onload = () => { logo = r.result; preview.src = logo; say('Save to use it', true); };
    r.readAsDataURL(f);
  };
  const pick = cnEl('button', { type: 'button', textContent: 'Choose…', title: 'A PNG, JPEG or WebP up to 200 KB; about 28 px high in the header', onclick: () => file.click() });
  const builtIn = cnEl('button', { type: 'button', className: 'link', textContent: 'Use the built-in one', hidden: !b.logo,
    onclick: () => { logo = null; file.value = ''; preview.src = 'icons/mark.png'; say('Save to use it', true); } });
  const save = cnEl('button', { type: 'button', className: 'primary', textContent: 'Save', onclick: async () => {
    const body = { name: name.value, powered: powered.checked };
    if (logo !== undefined) body.logo = logo;
    save.disabled = true;
    try {
      applyBrand(await api('PUT', '/api/branding', body));
      const card = cnCard((await api('GET', '/api/connections')).connections.find((x) => x.id === 'branding'));
      $('#cn-list .cn-card[data-id="branding"]')?.replaceWith(card);
      const m = card.querySelector('.cn-msg'); m.className = 'cn-msg ok'; m.textContent = 'Saved';
    } catch (e) { say(e.message, false); save.disabled = false; }
  } });
  const reset = cnEl('button', { type: 'button', textContent: 'Defaults', title: 'Tatanka: the built-in name and logo, "Powered by" on', onclick: () => {
    name.value = BRAND.name; powered.checked = BRAND.powered;
    if (b.logo) { logo = null; preview.src = 'icons/mark.png'; }
    say('Save to use them', true);
  } });
  const rows = [row('Name', name, 'In the header, the sign-in and the tab: "<name> Charts"'), row('Logo', [preview, pick, builtIn, file])];
  box.append(...rows, cnEl('label', { className: 'chk' }, powered, ' "Powered by Tatanka Charts" on chart pictures'),
    cnEl('div', { className: 'cn-ft' }, msg, reset, save));
  return box;
}
// Instruments: the futures charted from this server's own trades. Edited here, saved on the server, used after a restart
// (Docker starts it again by itself).
// Symbol list: what the search finds, per type, and each source (built in, Nasdaq, Coinbase, the connected feeds).
const SYM_SRC = { builtin: 'Built in (futures, indices, forex, crypto, FRED)', nasdaq: "Nasdaq's US listings (stocks, ETFs)",
  coinbase: 'Coinbase (crypto)', alpaca: 'Alpaca (US stocks, OTC too)', yahoo: 'Found on Yahoo by searches', fred: 'Found on FRED by searches' };
const SYM_TYPE = { stock: 'stocks', etf: 'ETFs', fund: 'funds', future: 'futures', index: 'indices', forex: 'forex', crypto: 'crypto', economic: 'economic', data: 'your data' };
function symbolsPart(c) {
  const box = cnEl('div', { className: 'cn-push' });
  const types = cnEl('p', { className: 'cn-about' }), list = cnEl('ul', { className: 'cn-devs' }), msg = cnEl('span', { className: 'cn-msg' });
  const name = (src) => SYM_SRC[src] || (src.startsWith('plugin:') ? (cn.list.find((x) => x.id === src.slice(7))?.title || src.slice(7)) : src);
  const load = async () => {
    try {
      const st = await api('GET', '/api/symbols/status');
      types.textContent = Object.entries(st.counts).sort((a, b) => b[1] - a[1]).map(([t, n]) => `${n.toLocaleString()} ${SYM_TYPE[t] || t}`).join(' · ');
      list.replaceChildren(...st.sources.map((s) => cnEl('li', {}, cnEl('span', { textContent: name(s.source) }),
        cnEl('small', { textContent: `${s.rows.toLocaleString()}${s.refreshed ? ` · updated ${ax.md.format(s.refreshed * 1000)}` : ''}${s.error ? ` · failed: ${s.error}` : ''}` }))));
    } catch (e) { msg.className = 'cn-msg bad'; msg.textContent = e.message; }
  };
  const now = cnEl('button', { type: 'button', className: 'primary', textContent: 'Update now', onclick: async () => {
    now.disabled = true; msg.className = 'cn-msg'; msg.textContent = 'Updating…';
    try { await api('POST', '/api/symbols/refresh'); } catch (e) { msg.className = 'cn-msg bad'; msg.textContent = e.message; now.disabled = false; return; }
    setTimeout(async () => { await load(); msg.className = 'cn-msg ok'; msg.textContent = 'Updated (big lists take a minute)'; now.disabled = false; }, 4000);
  } });
  box.append(types, list, cnEl('div', { className: 'cn-ft' }, msg, now));
  load();
  return box;
}
function instrumentsPart(c) {
  const box = cnEl('div', { className: 'cn-push' });
  const list = cnEl('div', { className: 'inst-list' }), msg = cnEl('span', { className: 'cn-msg' });
  const say = (text, ok) => { msg.className = `cn-msg ${ok ? 'ok' : 'bad'}`; msg.textContent = text; };
  const ft = cnEl('div', { className: 'cn-ft' });
  let st = null, rows = [], open = -1, dirty = false, picking = false;
  const pick = cnEl('div', { className: 'inst-pick' });
  // + Instrument: CME futures with their specs (config/futures.json), found by symbol or name; Other… for the rest
  function picker() {
    if (!picking) { pick.replaceChildren(); return; }
    const q = cnEl('input', { type: 'search', placeholder: 'Search: RTY, gold, yen, 10-year…', autocomplete: 'off', spellcheck: false, 'aria-label': 'Search futures' });
    const box = cnEl('div', { className: 'inst-cat' });
    const fill = () => {
      const t = q.value.trim().toLowerCase(), have = new Set(rows.map((r) => r.symbol));
      const hits = st.catalog.filter((c) => !have.has(c.symbol) && (!t || c.symbol.toLowerCase().startsWith(t) || `${c.name} ${c.group}`.toLowerCase().includes(t)));
      let group = '';
      const out = [];
      for (const c of hits) {
        if (c.group !== group) { group = c.group; out.push(cnEl('div', { className: 'inst-grp', textContent: group })); }
        out.push(cnEl('button', { type: 'button', className: 'inst-opt', onclick: () => {
          const { group: _g, ...row } = c;
          rows.push(row); dirty = true; q.value = ''; render(); say(`${c.symbol} added: Save to use it`, true);
        } }, cnEl('b', { textContent: c.symbol }), cnEl('span', { className: 'inst-txt' }, cnEl('span', { textContent: c.name }), cnEl('small', { textContent: line(c) }))));
      }
      if (!hits.length) out.push(cnEl('p', { className: 'cn-about', textContent: t ? 'None of the listed futures match: Other… types one in.' : 'Every listed future is added.' }));
      box.replaceChildren(...out);
    };
    q.oninput = fill;
    const other = cnEl('button', { type: 'button', className: 'link', textContent: 'Other… (type it in)', onclick: () => {
      rows.push({ symbol: q.value.trim().toUpperCase().slice(0, 6), name: '', tick_size: null, point_value: null, rth: null, calendar: 'equity' });
      picking = false; open = rows.length - 1; dirty = true; render(); list.querySelector('.inst-row.open input')?.focus();
    } });
    pick.replaceChildren(cnEl('div', { className: 'inst-q' }, q, other), box);
    fill();
  }
  const num = (v) => (v === '' || v == null ? null : Number(v));
  const plain = (x, group = false) => Number(x).toLocaleString('en-US', { maximumFractionDigits: 10, useGrouping: group });   // 0.0000005, not 5e-7
  const rolls = (r) => (!r.contract || r.contract === 'volume' ? '' : r.contract === 'dates' ? ' (NinjaTrader dates)' : ' (fixed)');
  const line = (r) => `${plain(r.tick_size)} tick · $${plain(r.point_value, true)}/pt${r.rth ? ` · RTH ${r.rth[0]}-${r.rth[1]}` : ''}${r.calendar && r.calendar !== 'equity' ? ` · ${r.calendar} holidays` : ''}${st.contracts?.[r.symbol] ? ` · ${st.contracts[r.symbol]}${rolls(r)}` : rolls(r)}`;
  function contractPick(r) {   // volume (the default), dates, or a fixed month "MM-YY"
    const mode = !r.contract || r.contract === 'volume' ? 'volume' : r.contract === 'dates' ? 'dates' : 'month';
    const sel = cnEl('select', { 'aria-label': 'Contract' }, ...[['volume', 'By volume'], ['dates', "NinjaTrader's roll dates"], ['month', 'A fixed month']]
      .map(([v, t]) => cnEl('option', { value: v, textContent: t, selected: v === mode })));
    const month = cnEl('input', { value: mode === 'month' ? r.contract : '', placeholder: 'MM-YY, e.g. 03-27', maxLength: 5, hidden: mode !== 'month', autocomplete: 'off', 'aria-label': 'Contract month' });
    sel.onchange = () => { month.hidden = sel.value !== 'month'; r.contract = sel.value === 'month' ? (month.value.trim() || undefined) : sel.value === 'volume' ? undefined : 'dates'; dirty = true; render(false); if (!month.hidden) month.focus(); };
    month.oninput = () => { r.contract = month.value.trim() || undefined; dirty = true; render(false); };
    return cnEl('span', { className: 'inst-rth' }, sel, month);
  }
  function editor(r, i) {
    const f = (label, input, note) => cnEl('label', { className: 'cn-fld' }, cnEl('span', { textContent: label }), cnEl('span', { className: 'cn-in' }, input),
      note ? cnEl('small', { textContent: note }) : null);
    const inp = (key, props = {}, conv = (v) => v) => {
      const e = cnEl('input', { value: r[key] ?? '', autocomplete: 'off', spellcheck: false, ...props });
      e.oninput = () => { r[key] = conv(e.value.trim()); dirty = true; render(false); };
      return e;
    };
    const rth = (k) => {
      const e = cnEl('input', { type: 'time', step: 60, value: r.rth?.[k] ?? '', 'aria-label': k ? 'RTH end' : 'RTH start' });
      e.onchange = () => { const v = [...(r.rth || ['', ''])]; v[k] = e.value; r.rth = v[0] || v[1] ? v : null; dirty = true; render(false); };
      return e;
    };
    const cal = cnEl('select', {}, ...st.calendars.map((g) => cnEl('option', { value: g, textContent: g, selected: (r.calendar || 'equity') === g })));
    cal.onchange = () => { r.calendar = cal.value; dirty = true; render(false); };
    return cnEl('div', { className: 'inst-ed' },
      f('Symbol', inp('symbol', { maxLength: 6, placeholder: 'RTY' }, (v) => v.toUpperCase()), "the root, as NinjaTrader names it"),
      f('Name', inp('name', { maxLength: 40, placeholder: 'E-mini Russell 2000' })),
      f('Tick size', inp('tick_size', { type: 'number', step: 'any', min: 0, placeholder: '0.1' }, num)),
      f('Point value', inp('point_value', { type: 'number', step: 'any', min: 0, placeholder: '50' }, num), '$ per point'),
      f('RTH (CT)', cnEl('span', { className: 'inst-rth' }, rth(0), ' to ', rth(1)), 'regular hours, for RTH VWAP, profiles and the RTH view; empty for none'),
      f('Holidays', cal, "CME's holiday hours for that group (config/holidays.json)"),
      f('Sim price', inp('sim_price', { type: 'number', step: 'any', min: 0, placeholder: 'optional' }, num), 'where the simulator starts it'),
      f('Contract', contractPick(r), 'which month it charts: rolls to the next one after a session in which that one traded more; or on NinjaTrader\'s roll dates; or a month you pick'),
      f('Stale alert', cnEl('input', { type: 'checkbox', checked: !r.quiet, onchange: (e) => { r.quiet = !e.target.checked || undefined; dirty = true; render(false); } }),
        'an alert when its trades stop for 2 minutes in market hours; off for thin markets (and ones added by opening them)'));
  }
  function render(full = true) {
    if (full) {
      list.replaceChildren(...rows.map((r, i) => {
        const row = cnEl('div', { className: `inst-row${i === open ? ' open' : ''}` },
          cnEl('div', { className: 'inst-hd' }, cnEl('b', { textContent: r.symbol || 'new' }),
            cnEl('span', { className: 'inst-txt' }, cnEl('span', { textContent: r.name || '' }), cnEl('small', { textContent: r.tick_size && r.point_value ? line(r) : '' })),
            cnEl('button', { type: 'button', className: 'link', textContent: i === open ? 'Done' : 'Edit', onclick: () => { open = open === i ? -1 : i; render(); } }),
            cnEl('button', { type: 'button', className: 'link', textContent: '×', title: `Take ${r.symbol || 'it'} off the list (its stored data stays)`,
              onclick: () => { rows.splice(i, 1); open = -1; dirty = true; render(); } })),
          i === open ? editor(r, i) : null);
        return row;
      }));
    }
    const add = cnEl('button', { type: 'button', textContent: '+ Instrument', onclick: () => { picking = !picking; render(); pick.querySelector('input')?.focus(); } });
    const apply = async (state) => {   // the server takes a new list at a start: it restarts by itself, pages reload
      st = state;
      if (!st.pending) { load(st, 'Saved'); return; }
      say('Saved: applying (the server restarts, about 10 s; charts reconnect by themselves)…', true);
      try { await api('POST', '/api/server/restart'); await backAfterRestart(); } catch (e) { load(st, 'Saved'); say(`Saved, not applied yet: ${e.message}`, false); }
    };
    const fileBtn = st.source === 'app' ? cnEl('button', { type: 'button', textContent: "File's list", title: 'Back to config/instruments.json', onclick: async () => {
      try { await apply(await api('DELETE', '/api/instruments/config')); } catch (e) { say(e.message, false); }
    } }) : null;
    const save = cnEl('button', { type: 'button', className: dirty ? 'primary' : '', textContent: 'Save', disabled: !dirty, title: 'Saves and applies the list (the server restarts by itself)', onclick: async () => {
      try { await apply(await api('PUT', '/api/instruments/config', { instruments: rows, version: st.version })); }
      catch (e) {
        if (!/changed meanwhile/.test(e.message)) { say(e.message, false); return; }
        api('GET', '/api/instruments/config').then((x) => { load(x); say('The list changed on another device meanwhile (a future opened there?): here it is now. Make your change again.', false); }).catch(() => say(e.message, false));
      }
    } });
    const restart = st.pending && !dirty ? cnEl('button', { type: 'button', className: 'primary', textContent: 'Apply now', onclick: () => apply(st) }) : null;
    ft.replaceChildren(...[msg, add, fileBtn, save, restart].filter(Boolean));
    if (full) picker();
  }
  function load(state, note) {
    st = state; rows = st.instruments.map((r) => ({ ...r })); open = -1; dirty = false; picking = false; render();
    if (note) say(note, true);
  }
  box.append(list, pick, ft);
  api('GET', '/api/instruments/config').then((x) => load(x)).catch((e) => say(e.message, false));
  return box;
}
async function serverRestart() {
  if (await ask({ title: 'Restart the server?', input: false, ok: 'Restart',
    message: 'Charts reconnect in a few seconds. Docker starts it again by itself; a server started by hand needs starting again.' }) == null) return;
  try { await api('POST', '/api/server/restart'); } catch (e) { fail(e); return; }
  await backAfterRestart();
}
async function backAfterRestart(limit = 90e3, note = '') {   // the server stops and Docker starts it again: reload once it answers
  toast(note || 'Restarting…', note ? limit : undefined);   // a note stays up while it waits
  const t0 = Date.now();
  await new Promise((r) => setTimeout(r, 2500));
  while (Date.now() - t0 < limit) {
    try { if ((await fetch('/api/health', { cache: 'no-store' })).ok) { location.reload(); return; } } catch { /* still down */ }
    await new Promise((r) => setTimeout(r, 1500));
  }
  toast('The server did not come back: start it again (docker compose up -d, or the command you start it with)');
}
// ---- staying current by itself: a page from another version of the app (an update), or opened before the instrument
// list changed, reloads once nothing is being typed or set; installed apps on phones included. -----------------------
const APP_VERSION = document.querySelector('meta[name="app-version"]')?.content || '';
let appConfig = null;
function versionCheck(m) {
  const newer = APP_VERSION && m.version && m.version !== APP_VERSION;
  const changed = appConfig !== null && m.config && m.config !== appConfig;
  if (appConfig === null) appConfig = m.config || '';
  if (newer || changed) reloadWhenIdle(newer ? 'Updated' : 'Instruments changed', newer ? m.version : '');
}
function updateNote(u) {   // a newer version on GitHub: said once per version, on each device
  if (!u?.behind || !u.latest || local.get('updateSeen') === u.latest) return;
  local.set('updateSeen', u.latest);
  toast(`Update available (${u.behind} change${u.behind > 1 ? 's' : ''}): run update.cmd on the PC. Connections > Updates lists them.`, 9000);
}
async function swOn(version) {   // the service worker serves `version` now (or there is none): up to 10 s
  if (!version || !navigator.serviceWorker?.controller || !window.caches) return;
  navigator.serviceWorker.getRegistration().then((r) => r?.update()).catch(() => {});
  const ready = async () => { try { return (await caches.keys()).every((k) => k === `charts-${version}`); } catch { return true; } };
  for (let i = 0; i < 20 && !(await ready()); i += 1) await new Promise((r) => setTimeout(r, 500));   // the old one's cache goes when the new one takes over
}
function reloadWhenIdle(why, version = '') {
  if (reloadWhenIdle.armed) return;
  reloadWhenIdle.armed = true;
  toast(`${why}: reloading…`);
  const go = () => {   // not in the middle of typing or a dialog (unless the page isn't being looked at)
    // The whole page reloads: in a layout, that is the page around the charts and every chart in it.
    const top = window.top || window, docs = [top.document, ...Array.from(top.frames, (f) => { try { return f.document; } catch { return null; } })];
    const busy = docs.some((doc) => doc && ([...doc.querySelectorAll('.modal')].some((d) => !d.hidden) || doc.activeElement?.matches?.('input, textarea, select')));
    if (busy && document.visibilityState === 'visible') { setTimeout(go, 2000); return; }
    swOn(version).catch(() => {}).finally(() => (window.top || window).location.reload());
  };
  setTimeout(go, 1500);
}
// ---- first run: a fresh install opens a short setup guide (the password, where futures trades come from, alerts) ----
// over the simulator. Each step is in Connections too; "Not now" ends it. An install with data never sees it.
async function leaveSim() {
  if (await ask({ title: 'Leave the simulator?', input: false, ok: 'Leave and restart', danger: true,
    message: "Its invented trades and bars are deleted (a copy of the database is kept next to it) and the server restarts on the real feeds: NinjaTrader's add-on, and the feeds added under Futures feed." }) == null) return false;
  try { await api('POST', '/api/setup/leave-sim'); } catch (e) { fail(e); return false; }
  // the server deletes the trades before it answers again (setup.leave_sim): after weeks on the simulator, minutes
  await backAfterRestart(30 * 60e3, "Deleting the simulator's trades, then restarting (after weeks on it, minutes)…");
  return true;
}
const SETUP = ['welcome', 'password', 'feed', 'alerts'];
async function setupCheck() {
  if (TK_CELL !== null || setupCheck.done) return;
  setupCheck.done = true;
  try { const st = await api('GET', '/api/setup'); if (!st.done) setupShow(st); } catch { /* next load */ }
}
function setupShow(st) {
  let dlg = $('#setup-dlg');
  if (!dlg) {
    dlg = cnEl('div', { id: 'setup-dlg', className: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Set up' });
    dlg.addEventListener('keydown', (e) => e.stopPropagation());
    document.body.append(dlg);
  }
  dlg.hidden = false;
  const save = (step, done = false) => api('PUT', '/api/setup', { step, done }).catch(() => {});
  const close = () => { dlg.hidden = true; };
  const go = (k) => { st.step = Math.max(0, Math.min(SETUP.length - 1, k)); save(st.step); render(); };
  const finish = () => { save(st.step, true); close(); toast('Set up. Connections (the dot, top right) has every setting.'); };
  const btn = (text, fn, primary = false) => cnEl('button', { type: 'button', className: primary ? 'primary' : '', textContent: text, onclick: fn });
  const p = (text) => cnEl('p', { textContent: text });
  function render() {
    const name = SETUP[st.step], body = cnEl('div', { className: 'setup-body' }), ft = cnEl('div', { className: 'setup-ft' });
    const msg = cnEl('span', { className: 'cn-msg' });
    const dots = cnEl('div', { className: 'setup-dots' }, ...SETUP.map((_, i) => cnEl('i', { className: i === st.step ? 'on' : i < st.step ? 'past' : '' })));
    if (name === 'welcome') {
      body.append(cnEl('h3', { textContent: `Welcome to ${brand.name} Charts` }),
        p('It runs on the simulator now: invented futures trades, so every chart and study works before anything is connected.'),
        p('Three short steps connect your own: a password, where your futures trades come from, and where alerts go. Each one can be changed later in Connections (the dot at the top right).'));
      ft.append(btn('Not now', finish), btn('Start', () => go(1), true));
    } else if (name === 'password') {
      body.append(cnEl('h3', { textContent: 'A password' }));
      if (st.login) {
        body.append(p('Login is on: each phone and PC signs in once and stays signed in.'));
        ft.append(btn('Back', () => go(0)), btn('Next', () => go(2), true));
      } else {
        const pw = (label, auto) => cnEl('input', { type: 'password', autocomplete: auto, placeholder: label, 'aria-label': label });
        const a = pw('Password (8+ characters)', 'new-password'), b = pw('The same again', 'new-password');
        body.append(p('Each phone and PC signs in once and stays signed in. Until a password is set, only this PC can open the app: your phone through Tailscale waits for it.'), a, b);
        const set = async () => {
          if (a.value.length < 8) { msg.className = 'cn-msg bad'; msg.textContent = 'At least 8 characters'; return; }
          if (a.value !== b.value) { msg.className = 'cn-msg bad'; msg.textContent = "The two don't match"; return; }
          try { await api('PUT', '/api/auth/password', { new: a.value, label: deviceLabel() }); st.login = true; go(2); } catch (e) { msg.className = 'cn-msg bad'; msg.textContent = e.message; }
        };
        ft.append(btn('Back', () => go(0)), st.local ? btn('Skip: only this PC opens it', () => go(2)) : null, btn('Set password', set, true));
      }
    } else if (name === 'feed') {
      body.append(cnEl('h3', { textContent: 'Futures trades' }));
      if (st.feed === 'sim') {
        body.append(p('Charts are built from every trade. Where will yours come from?'),
          cnEl('ul', { className: 'setup-list' },
            cnEl('li', {}, cnEl('b', { textContent: 'NinjaTrader' }), ': its add-on forwards the trades of your connection (Rithmic, Tradovate, CQG, Kinetick). The usual way for prop firm accounts.'),
            cnEl('li', {}, cnEl('b', { textContent: 'Interactive Brokers, Databento, or a program of your own' }), ': added under Connections > Futures feed > +.'),
            cnEl('li', {}, cnEl('b', { textContent: 'Not yet' }), ': keep the simulator and look around; Connections > NinjaTrader has "Leave the simulator" for later.')),
          p("Leaving the simulator deletes its invented trades (a copy is kept) and restarts the server; this guide picks up where it was."));
        ft.append(btn('Back', () => go(1)), btn('Keep the simulator', () => go(3)), btn('Leave the simulator', async () => { save(2); await leaveSim(); }, true));
      } else {
        body.append(p('From NinjaTrader:'),
          cnEl('ol', { className: 'setup-list' },
            cnEl('li', {}, cnEl('a', { href: '/api/addon/TatankaChartFeed.cs', download: 'TatankaChartFeed.cs', textContent: 'Download the add-on' }),
              cnEl('span', { textContent: ' (or double-click update.cmd in the Tatanka Charts folder, which copies it in) and put it in Documents\\NinjaTrader 8\\bin\\Custom\\AddOns\\' })),
            cnEl('li', { textContent: 'In NinjaTrader: New > NinjaScript Editor, press F5 (it compiles), close it.' }),
            cnEl('li', { textContent: 'Connect NinjaTrader to your data. The add-on sends every trade here (port 8801); Connections > NinjaTrader turns green.' })),
          p('Other brokers and data vendors: Connections > Futures feed > + (docs/FEEDS.md). Each supplies the futures you give it; NinjaTrader the rest.'));
        ft.append(btn('Back', () => go(1)), btn('Open Connections', () => { close(); cnOpen(); }), btn('Next', () => go(3), true));
      }
    } else {   // the watchlist's prices as they come now: Yahoo, the simulator's (a fresh install), or neither yet
      const q = st.quotes || [], more = 'Interactive Brokers, Alpaca (stocks) and Coinbase (crypto) are real time: Connections > Quotes.';
      body.append(cnEl('h3', { textContent: 'Alerts and quotes' }),
        p('Alerts reach you as app notifications on this phone or PC, in Discord or through ntfy: Connections > Alerts and Discord.'),
        p(q.includes('yahoo') ? `Watchlist prices: Yahoo is on (free; futures lag about 10 minutes). ${more}`
          : q.includes('sim') && st.feed === 'sim' ? `Watchlist prices: invented by the simulator for now, as its trades are (S by each price). Yahoo is free (futures lag about 10 minutes); ${more}`
            : `Watchlist prices: Yahoo is free (futures lag about 10 minutes); ${more}`));
      ft.append(btn('Back', () => go(2)), btn('Open Connections', () => { finish(); cnOpen(); }), btn('Finish', finish, true));
    }
    dlg.replaceChildren(cnEl('div', { className: 'sheet setup' }, dots, body, cnEl('div', { className: 'setup-row' }, msg), ft));
    dlg.querySelector('input, .setup-ft .primary')?.focus();
  }
  render();
}
function loginPart(c) {
  const box = cnEl('div', { className: 'cn-push' });
  const msg = cnEl('span', { className: 'cn-msg' });
  const say = (text, ok) => { msg.className = `cn-msg ${ok ? 'ok' : 'bad'}`; msg.textContent = text; };
  const pw = (label, auto) => cnEl('input', { type: 'password', autocomplete: auto, placeholder: label, 'aria-label': label });
  const refresh = async (note) => {
    const fresh = (await api('GET', '/api/connections')).connections.find((x) => x.id === 'login');
    if (!fresh) return;
    const card = cnCard(fresh);
    $('#cn-list .cn-card[data-id="login"]')?.replaceWith(card);
    if (note) { const m = card.querySelector('.cn-msg'); m.className = 'cn-msg ok'; m.textContent = note; }
  };
  const row = (label, input) => cnEl('label', { className: 'cn-fld' }, cnEl('span', { textContent: label }), cnEl('span', { className: 'cn-in' }, input));
  const cur = pw('Current password', 'current-password'), nw = pw('New password (8+ characters)', 'new-password'), again = pw('The same again', 'new-password');
  const setIt = async (body, note) => {
    try { await api('PUT', '/api/auth/password', { ...body, label: deviceLabel() }); await refresh(note); } catch (e) { say(e.message, false); }
  };
  const newOk = () => {
    if (nw.value.length < 8) { say('At least 8 characters', false); return false; }
    if (nw.value !== again.value) { say("The two don't match", false); return false; }
    return true;
  };
  if (!c.login.enabled) {
    box.append(row('Password', nw), row('Again', again), cnEl('div', { className: 'cn-ft' }, msg,
      cnEl('button', { type: 'button', className: 'primary', textContent: 'Turn on login', onclick: () => newOk() && setIt({ new: nw.value }, 'Login on. Other devices sign in once.') })));
    return box;
  }
  const list = cnEl('ul', { className: 'cn-devs' });
  let sessions = [];
  onMenuPress(list, 'li', (li, x, y) => {   // another device: signed out from its menu, not a ✕ beside it
    const d = sessions.find((q) => String(q.id) === li.dataset.id);
    if (d && !d.current) showMenu(x, y, [{ label: `Sign out ${d.label}`, fn: async () => {
      try { await api('DELETE', `/api/auth/sessions/${d.id}`); await refresh('Signed out'); } catch (e) { say(e.message, false); }
    } }]);
  }, 'button');
  api('GET', '/api/auth/sessions').then((r) => { sessions = r.sessions; list.append(...r.sessions.map((d) => cnEl('li', { dataset: { id: d.id } },
    cnEl('span', { textContent: `${d.label}${d.current ? ' (this one)' : ''}` }),
    cnEl('small', { textContent: `last used ${ax.date.format(d.seen)}` }),
    d.current ? cnEl('button', { type: 'button', className: 'link', textContent: 'Sign out', onclick: async () => { await api('POST', '/api/auth/logout'); location.reload(); } }) : null))); })
    .catch(() => {});
  box.append(cnEl('p', { className: 'cn-hint', textContent: 'Signed-in devices. Right-click one (a long press on a phone) to sign it out.' }), list, cnEl('div', { className: 'cn-ft' }, cnEl('button', { type: 'button', textContent: 'Sign out everywhere else', onclick: async () => {
    try { const r = await api('POST', '/api/auth/sessions/end-others'); await refresh(`${r.ended} signed out`); } catch (e) { say(e.message, false); }
  } })));
  box.append(row('Current', cur), row('New', nw), row('Again', again), cnEl('div', { className: 'cn-ft' }, msg,
    cnEl('button', { type: 'button', textContent: 'Turn off', title: c.login.pc_only ? 'No password: only this PC can use the app, other devices are refused' : 'No password: anyone who reaches the app can use it', onclick: () => setIt({ current: cur.value, new: null }, 'Login off') }),
    cnEl('button', { type: 'button', className: 'primary', textContent: 'Change password', onclick: () => newOk() && setIt({ current: cur.value, new: nw.value }, 'Changed. Other devices sign in again.') })));
  return box;
}

function backupPart(c) {
  const box = cnEl('div', { className: 'cn-push' });
  const msg = cnEl('span', { className: 'cn-msg' });
  const kb = (n) => `${Math.max(1, Math.round(n / 1024)).toLocaleString()} KB`;
  const list = cnEl('ul', { className: 'cn-devs' }, ...c.backup.files.slice(0, 5).map((f) => cnEl('li', {},
    cnEl('span', { textContent: `${ax.md.format(f.created)} ${zs(f.created)}` }), cnEl('small', { textContent: kb(f.size) }),
    c.backup.download ? cnEl('a', { className: 'link', href: `/api/backups/${encodeURIComponent(f.name)}`, download: f.name, textContent: 'Download' }) : null)));
  if (!c.backup.download) box.append(cnEl('p', { className: 'cn-about', textContent: 'Download needs Login on: backups hold your keys.' }));
  const now = cnEl('button', { type: 'button', className: 'primary', textContent: 'Back up now', onclick: async () => {
    now.disabled = true; msg.className = 'cn-msg'; msg.textContent = 'Backing up…';
    try {
      await api('POST', '/api/backups');
      const fresh = (await api('GET', '/api/connections')).connections.find((x) => x.id === 'backup');
      if (fresh) { const card = cnCard(fresh); $('#cn-list .cn-card[data-id="backup"]')?.replaceWith(card); const m = card.querySelector('.cn-msg'); m.className = 'cn-msg ok'; m.textContent = 'Backed up'; }
    } catch (e) { msg.className = 'cn-msg bad'; msg.textContent = e.message; now.disabled = false; }
  } });
  box.append(list, cnEl('div', { className: 'cn-ft' }, msg, now));
  const r = c.backup.retention;
  if (r) {   // how long each trade is kept; 1-minute bars always are
    const keep = cnEl('select', { 'aria-label': 'Keep each trade' }, ...r.choices.map((y) => cnEl('option', { value: y, selected: y === r.years,
      textContent: y ? `${y} year${y > 1 ? 's' : ''}` : 'Always' })));
    const said = cnEl('small', { textContent: r.deleted ? `Last tidy: ${r.deleted.toLocaleString()} older trades removed${r.more ? ', more tonight' : ''}.` : '' });
    keep.onchange = async () => {
      try { await api('PUT', '/api/backups/retention', { years: Number(keep.value) }); said.textContent = 'Saved: applies in the next daily break (16:00 CT).'; }
      catch (e) { said.textContent = e.message; }
    };
    box.append(cnEl('label', { className: 'cn-fld' }, cnEl('span', { textContent: 'Keep each trade' }), cnEl('span', { className: 'cn-in' }, keep),
      cnEl('small', { textContent: 'older ones are removed each day in the 16:00 CT break; 1-minute bars are always kept, so candle charts reach back as far as ever. Tick, footprint and tape detail goes for older days.' }),
      said));
  }
  return box;
}

// App notifications: this device subscribes to the server's Web Push; the list shows every device that did.
const pushIdGet = () => { try { return Number(localStorage.getItem('pushId')) || 0; } catch { return 0; } };
const pushIdSet = (v) => { try { if (v) localStorage.setItem('pushId', String(v)); else localStorage.removeItem('pushId'); } catch { /* private mode */ } };
const b64uBytes = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - s.length % 4) % 4)), (ch) => ch.charCodeAt(0));
function deviceLabel() {
  const ua = navigator.userAgent;
  const dev = /iPhone/.test(ua) ? 'iPhone' : /iPad|Macintosh.*Mobile/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1) ? 'iPad'
    : /Android/.test(ua) ? 'Android' : /Windows/.test(ua) ? 'Windows PC' : /Mac OS X/.test(ua) ? 'Mac' : /Linux/.test(ua) ? 'Linux PC' : 'Device';
  const app = matchMedia('(display-mode: standalone)').matches || navigator.standalone ? 'app' : /Edg\//.test(ua) ? 'Edge' : /Firefox/.test(ua) ? 'Firefox' : /Chrome/.test(ua) ? 'Chrome' : 'Safari';
  return `${dev} · ${app}`;
}
function pushWhyNot() {   // why this browser can't take app notifications, or ''
  const ios = /iPhone|iPad/.test(navigator.userAgent) || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  if (!isSecureContext) return 'Open the app from its https:// address (tailscale serve, see docs/PHONE.md) to turn them on here.';
  if (ios && !standalone) return 'On iPhone: Share > Add to Home Screen, then open the app from that icon and turn them on there (iOS 16.4 or later).';
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return "This browser can't show app notifications.";
  return '';
}
async function pushMine(devices) {   // this device's subscription, if the server still has it
  if (pushWhyNot()) return null;
  const reg = await navigator.serviceWorker.getRegistration();
  const sub = reg && await reg.pushManager.getSubscription();
  return sub && devices.some((d) => d.id === pushIdGet()) ? sub : null;
}
function pushPart(c) {
  const box = cnEl('div', { className: 'cn-push' });
  const why = pushWhyNot();
  const msg = cnEl('span', { className: 'cn-msg' });
  const say = (text, ok) => { msg.className = `cn-msg ${ok === undefined ? '' : ok ? 'ok' : 'bad'}`; msg.textContent = text; };
  const btn = cnEl('button', { type: 'button', className: 'primary', textContent: 'Turn on for this device', disabled: !!why });
  const refresh = async () => {
    try { const r = await api('GET', '/api/connections'); const fresh = r.connections.find((x) => x.id === 'push'); if (fresh) $('#cn-list .cn-card[data-id="push"]')?.replaceWith(cnCard(fresh)); } catch (e) { fail(e); }
  };
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      if (btn.dataset.on) {
        const sub = await pushMine(c.push.devices);
        if (sub) { await api('POST', '/api/push/unsubscribe', { endpoint: sub.endpoint }).catch(() => {}); await sub.unsubscribe(); }
        pushIdSet(0);
      } else {
        const perm = await Notification.requestPermission();   // first, inside the tap: iPhone asks only then
        if (perm !== 'granted') throw new Error(/iPhone|iPad/.test(navigator.userAgent) ? 'Notifications are off for this app: iPhone Settings > Notifications > Tatanka' : 'Notifications are blocked for this site: allow them in the browser (the icon left of the address)');
        const reg = await navigator.serviceWorker.ready;
        let sub = await reg.pushManager.getSubscription();
        const key = b64uBytes(c.push.key);
        if (sub && sub.options?.applicationServerKey && new Uint8Array(sub.options.applicationServerKey).join() !== key.join()) { await sub.unsubscribe(); sub = null; }
        sub = sub || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
        pushIdSet((await api('POST', '/api/push/subscribe', { subscription: sub.toJSON(), label: deviceLabel() })).id);
      }
      await refresh();
    } catch (e) { say(e.message, false); btn.disabled = false; }
  };
  const list = cnEl('ul', { className: 'cn-devs' }, ...c.push.devices.map((d) => cnEl('li', { dataset: { id: d.id } },
    cnEl('span', { textContent: `${d.label}${d.id === pushIdGet() ? ' (this one)' : ''}` }),
    cnEl('small', { textContent: d.last_error ? `last: ${d.last_error}` : d.last_ok ? `last delivered ${ax.md.format(d.last_ok)} ${zs(d.last_ok)}` : `added ${ax.date.format(d.created)}` }))));
  onMenuPress(list, 'li', (li, x, y) => {   // a device's menu: Remove (no ✕ beside it)
    const d = c.push.devices.find((q) => String(q.id) === li.dataset.id);
    if (d) showMenu(x, y, [{ label: `Remove ${d.label}`, fn: async () => {
      try { await api('DELETE', `/api/push/devices/${d.id}`); if (d.id === pushIdGet()) pushIdSet(0); await refresh(); } catch (e) { say(e.message, false); }
    } }]);
  });

  const test = cnEl('button', { type: 'button', textContent: 'Test', disabled: !c.push.devices.length, onclick: async () => {
    say('Sending…');
    try { const r = await api('POST', '/api/connections/push/test', {}); say(r.message, r.ok); } catch (e) { say(e.message, false); }
  } });
  if (why) box.append(cnEl('p', { className: 'cn-lock', textContent: why }));
  if (c.push.devices.length) box.append(cnEl('p', { className: 'cn-hint', textContent: 'Right-click a device (a long press on a phone) to remove it.' }));
  box.append(list, cnEl('div', { className: 'cn-ft' }, msg, test, btn));
  pushMine(c.push.devices).then((sub) => { if (sub) { btn.dataset.on = '1'; btn.textContent = 'Turn off for this device'; btn.className = ''; } }).catch(() => {});
  return box;
}
async function cnSave(id, body) {
  const card = $(`#cn-list .cn-card[data-id="${id}"]`);
  try {
    const c = await api('PUT', `/api/connections/${id}`, body);
    const i = cn.list.findIndex((x) => x.id === id);
    if (i >= 0) cn.list[i] = c;
    const fresh = cnCard(c);
    card?.replaceWith(fresh);
    cnDots();
    const msg = fresh.querySelector('.cn-msg');
    if (msg) { msg.className = 'cn-msg ok'; msg.textContent = 'Saved'; }
  } catch (e) {
    const msg = card?.querySelector('.cn-msg');
    if (msg) { msg.className = 'cn-msg bad'; msg.textContent = e.message; } else fail(e);
    const on = card?.querySelector('.cn-on input');
    if (on && 'enabled' in body) on.checked = !body.enabled;
  }
}
function deviceFacts() {   // what this browser can do: for the report
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  const perm = 'Notification' in window ? Notification.permission : 'not supported';
  return `this device: ${deviceLabel()} · ${location.protocol === 'https:' ? 'https' : 'http (no app notifications)'}`
    + ` · ${standalone ? 'installed app' : 'browser tab'} · notifications ${perm}${pushWhyNot() ? ` (${pushWhyNot()})` : ''}`;
}
async function cnCheck() {
  const btn = $('#cn-check'), sum = $('#cn-sum');
  btn.disabled = true;
  sum.hidden = false;
  sum.className = '';
  sum.textContent = 'Checking every connection in use…';
  try {
    const d = await api('POST', '/api/connections/check');
    const pad = Math.max(...d.results.map((r) => r.title.length));
    const lines = [`${brand.name} Charts check · ${ax.full.format(d.at)} ${zs(d.at)}`, `feeds running: ${d.feeds.join(', ') || 'none'}`,
      ...d.results.map((r) => `  ${r.ok ? 'OK  ' : r.ok === null ? 'off ' : 'FAIL'}  ${r.title.padEnd(pad)}  ${r.message}`), deviceFacts()];
    for (const r of d.results) {
      const m = $(`#cn-list .cn-card[data-id="${r.id}"] .cn-msg`);
      if (m && r.ok !== null) { m.className = `cn-msg ${r.ok ? 'ok' : 'bad'}`; m.textContent = r.message; }
    }
    const bad = d.results.filter((r) => r.ok === false).length, good = d.results.filter((r) => r.ok).length;
    sum.className = bad ? 'bad' : 'ok';
    sum.replaceChildren(cnEl('span', { textContent: `${good} working · ${bad} failing · ${d.results.length - good - bad} off` }),
      cnEl('button', { type: 'button', className: 'link', textContent: 'Copy report', title: 'No keys or URLs in it', onclick: async (e) => {
        try { await navigator.clipboard.writeText(lines.join('\n')); e.target.textContent = 'Copied'; } catch { prompt('Copy the report:', lines.join(' | ')); }
      } }));
    cnPoll();
  } catch (e) { sum.className = 'bad'; sum.textContent = e.message; }
  btn.disabled = false;
}
$('#cn-check').addEventListener('click', cnCheck);
$('#conn-btn').addEventListener('click', cnOpen);
$('#notify-conn').addEventListener('click', cnOpen);
$('#cn-close').addEventListener('click', cnClose);
$('#cn-dlg').addEventListener('pointerdown', (e) => { if (e.target === e.currentTarget) cnClose(); });
const cnEsc = () => { if (!$('#ctx').hidden) closeCtx(); else cnClose(); };   // a + menu open: that first
$('#cn-dlg').addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Escape') cnEsc(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#cn-dlg').hidden && !e.target.closest?.('#ctx')) cnEsc(); });   // focus left with a replaced card
// The loading screen says what it waits on when the chart doesn't come: no answer from the server (the PC asleep, or
// Tailscale off on this device), or the chart's own error; Start fresh drops this device's saved copy of the app.
let splashTimer = 0;
function splashSay(text, fresh = false) {
  const sp = $('#splash');
  if (sp.classList.contains('gone')) return;
  sp.querySelector('span').textContent = text;
  if (fresh) $('#splash-fresh').hidden = false;
}
function splashWatch() {
  clearTimeout(splashTimer);
  splashTimer = setTimeout(() => {
    if (S.ws?.readyState === WebSocket.OPEN) splashSay('Connected, but the chart hasn\'t come yet…', true);
    else splashSay('Can\'t reach the server. Is the PC on? On this device, switch Tailscale off and on. Trying again…', true);
  }, 8000);
}
$('#splash-fresh').addEventListener('click', async () => {
  try { for (const r of (await navigator.serviceWorker?.getRegistrations()) || []) await r.unregister(); } catch { /* none */ }
  try { for (const k of await caches.keys()) await caches.delete(k); } catch { /* none */ }
  location.reload();
});
splashWatch();
let reconnectTimer = 0;
// One socket at a time: events from a replaced socket are ignored, and a pending retry is cancelled.
function connect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = 0;
  if (S.ws && S.ws.readyState <= WebSocket.OPEN) return; // already connecting or open
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  let opened = false;
  S.ws = ws;
  ws.onopen = () => { if (S.ws !== ws) { ws.close(); return; } opened = true; S.retry = 0; setConn(true); splashSay('Loading the chart…'); splashWatch(); };
  ws.onmessage = (e) => { if (S.ws === ws) onMessage(JSON.parse(e.data)); };
  ws.onclose = (e) => {
    if (S.ws !== ws) return;
    setConn(false);
    if (e.code === 4401) { authCheck(true); return; }   // the server wants the password
    // never opened: the server is down, or it refused this address (start()'s ask may have failed while it was down)
    if (!opened) authCheck();
    reconnectTimer = setTimeout(connect, Math.min(15000, 500 * 2 ** S.retry++));
  };
}
// Phones freeze background tabs; reconnect as soon as the app is visible again.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && !loginShown && (!S.ws || S.ws.readyState === WebSocket.CLOSED)) { S.retry = 0; connect(); }
});

// ---- login: once a password is set on the server (Connections > Login), each device signs in once -----------------
let loginShown = false;
async function authCheck(reconnect = false) {   // ask before showing the sign-in: a password just set here is fine
  try {
    const a = await (await fetch('/api/auth')).json();
    if (a.wrong_host) { wrongHost(); return; }
    if (a.need_password) { needPassword(); return; }   // start() missed it: its ask failed while the PC was starting
    if (a.enabled && !a.signed_in) { showLogin(); return; }
  } catch { /* the server is down */ }
  if (reconnect) reconnectTimer = setTimeout(connect, 1000);
}
function showLogin() {
  if (loginShown) return;
  loginShown = true;
  clearTimeout(reconnectTimer);
  if (TK_CELL !== null) return;   // a chart of a layout: the page around it asks
  $('#login').hidden = false;
  $('#login-pw').hidden = $('#login-ok').hidden = false;   // needPassword() hid them: the password is set now
  $('#login-msg').textContent = '';
  setTimeout(() => $('#login-pw').focus(), 50);
}
$('#login').addEventListener('keydown', (e) => e.stopPropagation());   // typing here is not chart shortcuts
$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('#login-ok').disabled = true;
  $('#login-msg').textContent = '';
  try {
    await api('POST', '/api/auth/login', { password: $('#login-pw').value, label: deviceLabel() });
    location.reload();
  } catch (err) {
    $('#login-msg').textContent = err.message;
    $('#login-pw').select();
    $('#login-ok').disabled = false;
  }
});
function needPassword() {   // a fresh install opened from another device: a password first, on the PC itself
  $('#login').hidden = false;
  $('#login-pw').hidden = $('#login-ok').hidden = true;
  $('#login-msg').textContent = 'Set a password on the PC first: open the app there (http://localhost:8800).';
}
function wrongHost() {   // opened by a name the server doesn't answer to (it takes it for another site's): say so, and stop
  loginShown = true;   // no reconnecting: every try is refused
  clearTimeout(reconnectTimer);
  $('#login').hidden = false;
  $('#login-pw').hidden = $('#login-ok').hidden = true;
  $('#login-msg').textContent = `This app doesn't answer to ${location.hostname}: add it to CHART_ALLOWED_HOSTS in .env on the PC (docs/PHONE.md, Login).`;
}
async function start() {
  brandLoad();
  try {
    const a = await (await fetch('/api/auth')).json();
    if (a.wrong_host) { wrongHost(); return; }
    if (a.need_password) { needPassword(); return; }
    if (a.enabled && !a.signed_in) { showLogin(); return; }
  } catch { /* the server is down: connect() keeps trying */ }
  connect();
}

// ---- drawings (drawings.js): stored on the server per symbol --------------------------------------------
// ---- templates: chart, drawing and indicator templates, saved on the server, the same on every device -------------
S.tpl = {};
const tplPath = (kind, name) => `/api/templates/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`;
const templates = {
  names: (kind) => Object.keys(S.tpl[kind] || {}).sort((a, b) => a.localeCompare(b)),
  data: (kind, name) => S.tpl[kind]?.[name]?.data,
  async save(kind, name, data) { await api('PUT', tplPath(kind, name), { data }); await tplLoad(); },
  async del(kind, name) { await api('DELETE', tplPath(kind, name)); await tplLoad(); },
};
let tplMigrated = false;
async function tplLoad() {
  try { S.tpl = (await api('GET', '/api/templates')).templates || {}; } catch { return; }
  if (!tplMigrated && TK_CELL === null) {   // once: drawing templates this browser kept before they were on the server
    tplMigrated = true;
    try {
      for (const k of Object.keys(localStorage).filter((x) => /^drawTpl\.[a-z]+$/.test(x))) {
        const kind = `draw:${k.slice(8)}`, local_ = JSON.parse(localStorage.getItem(k) || '{}') || {};
        for (const [name, data] of Object.entries(local_)) if (!S.tpl[kind]?.[name] && data && typeof data === 'object') await api('PUT', tplPath(kind, name), { data }).catch(() => {});
        localStorage.removeItem(k);
      }
      S.tpl = (await api('GET', '/api/templates')).templates || {};
    } catch { /* next start */ }
  }
  drawings.templatesChanged?.();
  renderChartTpl?.();
  indDlg.templatesChanged?.();
}
const drawings = createDrawings({
  chart, series: candles, el: $('#chart'), bars: () => S.bars, symbol: () => S.symbol, api, toast, ask, menu: showMenu,
  periodMs: () => (isTime() ? specSeconds(S.spec) * 1000 : 0), tick: () => inst().tick_size, hoverPrice: () => S.hoverPrice,
  // CME's hours and holidays, which a future's intraday ETH bars follow (and so the server's trend-line alerts)
  market: () => (isTime() && !calSpec() && !S.ext && S.instruments[S.symbol] && !(S.rth && inst().rth)
    ? { holidays: S.holidays, group: inst().calendar || 'equity' } : null),
  // W and M bars, a week (or a month) apart on the clock, an nM chart's n (its bars calendar months) and the price
  // scale: how a trend line's alert switched on here keeps to the line as this chart draws it
  byClock: () => isTime() && /^\d+[WM]$/.test(S.spec), logScale: () => PS().options().mode === PSM.Logarithmic,
  months: () => (isTime() && /^\d+M$/.test(S.spec) ? parseInt(S.spec, 10) : 0),
  tab: () => (TK_MULTI ? null : S.tabs[S.tab]?.id), tabLocal: () => !TK_MULTI && !!S.tabs[S.tab]?.drawLocal,
  pointValue: () => inst().point_value || 0,
  fmt: (p) => px(Math.round(p / inst().tick_size) * inst().tick_size),
  alerts: () => S.alerts.filter((a) => a.symbol === S.symbol),
  canAlert: () => !S.ext,   // alerts run on live trades
  templates,
  ui: { bar: $('#draw-bar'), edit: $('#draw-edit'), paste: $('#draw-paste'), tree: $('#draw-tree') },
});
const watch = createWatchlist({
  root: $('#tab-watch'), api, fail, toast, ask, local, menu: showMenu, closeMenu: closeCtx,
  instruments: () => S.instruments, quotes: () => S.quotes, current: () => S.symbol, onlyFutures: () => S.onlyFutures,
  open: (sym) => {   // on a phone, as TradingView's app: its chart, full screen
    if (TK_MULTI) { workspaces.toActive({ type: 'symbol', sym }); return; }
    setSymbol(sym);
    if (phoneLayout()) { setSide(false); tapeSync(); }
  },
  alert: (sym) => {   // a live future's chart symbol ("ES"): its quote is ES1!'s
    const price = S.quotes[wlKey(sym, S.instruments)]?.last;
    if (TK_MULTI) { workspaces.toActive({ type: 'alert', sym, price }); return; }
    setSymbol(sym);
    openAlertDialog({ price });
  },
});
// On a computer the drawing tools are a column beside the chart, as TradingView's, so they cover none of it; a phone
// keeps them in a row over the bottom of the chart.
const phoneWidth = matchMedia('(max-width: 760px)');
function placeDrawBar() {
  const bar = $('#draw-bar'), wrap = $('#chart-wrap'), side = !phoneWidth.matches;
  if (side && bar.parentElement === wrap) wrap.before(bar);
  else if (!side && bar.parentElement !== wrap) wrap.insertBefore(bar, $('#replay-bar'));
  bar.classList.toggle('col', side);
}
placeDrawBar();
phoneWidth.addEventListener('change', placeDrawBar);
function showDrawBar(on) {
  $('#draw-bar').hidden = !on;
  $('#draw-btn').classList.toggle('on', on);
  $('#draw-btn').setAttribute('aria-pressed', String(on));
  local.set('drawBar', on ? '1' : '0');
  if (!on) drawings.setTool('select');
  S.inds.forEach((x) => x.label?.update());   // pane labels move clear of it
}
showDrawBar(local.get('drawBar', window.innerWidth < 760 ? '0' : '1') === '1');
$('#draw-btn').onclick = () => showDrawBar($('#draw-bar').hidden);

// ---- big trades (bubbles) and time & sales (the Tape tab): the server groups fills at the same millisecond and
// side into one trade, from stored trades and live as they print ----------------------------------------------------
const BIG_DEFAULT = { ES: 25, MES: 25, NQ: 10, MNQ: 10, CL: 20, GC: 10 };
function bigMins() { try { return JSON.parse(local.get('bigMin', '{}')) || {}; } catch { return {}; } }
const bigMin = () => bigMins()[S.symbol] || BIG_DEFAULT[S.symbol] || 20;
S.big = [];
S.bigKey = '';
let tapeMin = Math.max(1, Math.round(Number(local.get('tapeMin', '1'))) || 1);
const tapeOpen = () => $('#tab-tape').classList.contains('on') && !$('main').classList.contains('side-off');
const bigOn = () => S.st.big && !TK_MULTI && !S.ext;
let tapeWant = '';
// One live subscription per page: the smaller of the two sizes, each view keeps what it wants.
function tapeSync() {
  const on = !!S.instruments[S.symbol] && (bigOn() || tapeOpen());
  const min = Math.min(bigOn() ? bigMin() : Infinity, tapeOpen() ? tapeMin : Infinity);
  const want = on ? `${S.symbol} ${min}` : '';
  if (want === tapeWant) return;
  tapeWant = want;
  send(on ? { op: 'tape', symbol: S.symbol, min } : { op: 'tape', symbol: null });
}
let bigTimer = 0;
function bigSchedule(ms = 400) { clearTimeout(bigTimer); bigTimer = setTimeout(bigFetch, ms); }
async function bigFetch() {
  if (!bigOn() || !S.bars.length) { S.big = []; S.bigKey = ''; bigPrim.update(); return; }
  const r = chart.timeScale().getVisibleLogicalRange();
  if (!r) return;
  const i0 = Math.max(0, Math.floor(r.from) - 5), i1 = Math.min(S.bars.length - 1, Math.ceil(r.to));
  // to the replay clock in a replay (its last bar is not the live edge: each step past it asks again)
  const end = i1 + 1 < S.bars.length ? S.bars[i1 + 1].t : S.replay ? S.replay.at : Date.now() + 60_000;
  const start = Math.max(S.bars[i0].t, end - 3 * 86_400_000), sym = S.symbol, min = bigMin();
  const key = `${sym} ${min}`, live = !S.replay && i1 + 1 >= S.bars.length;
  // Already have it: fetched to the live edge, the stream (onTrades) has kept it up since.
  if (key === S.bigKey && start >= S.bigFrom && (S.bigTo === Infinity || end <= S.bigTo)) return;
  try {
    // the newest 20000 when there are more (a small size on a long range): the bars nearest now keep theirs
    const d = await api('GET', `/api/trades?symbol=${sym}&start=${start}&end=${end}&min=${min}&limit=20000&newest=true`);
    if (sym !== S.symbol || min !== bigMin()) return;
    // newest first, read once: at the live edge the stream's trades since then (onTrades) stay
    const last = d.trades.length ? d.trades[0][0] : start;
    S.big = [...d.trades.reverse(), ...(live ? S.big.filter((t) => t[0] > last) : [])];
    S.bigKey = key;
    S.bigFrom = d.trades.length >= 20000 ? S.big[0][0] : start;
    S.bigTo = live ? Infinity : end;
  } catch (e) { toast(`Big trades: ${e.message}`); }
  bigPrim.update();
}
// The bar a trade (a big one, a fill) printed in, or null where none of the chart's holds it: the hours an RTH chart
// leaves out (the night, the pre-market), on a time chart past the last bar's end, not the next bar. On tick, volume,
// range and Renko bars the forming one holds every trade since it opened, however much longer than the others it runs.
function tradeBar(t) {
  const bars = S.bars, rth = S.rth && !S.ext && inst().rth;
  if (rth) {
    const sec = Math.floor(ctWall(t) / 1000) % 86400, [a, b] = rth.map((v) => +v.slice(0, 2) * 3600 + +v.slice(3, 5) * 60);
    if (sec < a || sec >= b) return null;
  }
  if (isTime()) {
    const i = barIndexTo(bars, t);
    return i >= 0 && t < barEnd(bars, i, 'time', specSeconds(S.spec) * 1000, daysSpec(S.spec)) ? i : null;
  }
  if (t > bars.at(-1).t) return bars.length - 1;
  const l = drawings.timeToLogical(t);
  return l == null || l < 0 ? null : Math.floor(l + 1e-9);
}
// Bubbles at the bar each trade printed in, area by size (the size at least, then up to 30px across).
function drawBig(ctx) {
  if (!bigOn() || !S.big.length || !S.bars.length) return;
  const ts = chart.timeScale(), r = ts.getVisibleLogicalRange(), min = bigMin(), cut = S.replay ? S.replay.at : Infinity;
  ctx.save();
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  for (const [t, p, sz, side] of S.big) {
    if (t > cut || sz < min) continue;
    const li = tradeBar(t);
    if (li == null || (r && (li < r.from - 1 || li > r.to + 1))) continue;
    const x = logicalX(ts, li), y = candles.priceToCoordinate(p);
    if (x == null || y == null) continue;
    const rad = Math.min(30, 4 + 8 * Math.sqrt(sz / min)), col = side > 0 ? C.up : side < 0 ? C.down : '#c8c8c8';
    ctx.beginPath();
    ctx.arc(x, y, rad, 0, Math.PI * 2);
    ctx.globalAlpha = 0.32;
    ctx.fillStyle = col;
    ctx.fill();
    ctx.globalAlpha = 0.95;
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = col;
    ctx.stroke();
    if (rad >= 11) {
      ctx.globalAlpha = 1;
      ctx.fillStyle = '#ffffff';
      ctx.font = `600 ${Math.min(13, Math.round(rad * 0.75))}px ${css('--font')}`;
      ctx.fillText(String(sz), x, y + 0.5);
    }
  }
  ctx.restore();
}
// ---- economic calendar: the Calendar tab, lines on the chart, warnings before releases (the server's) ---------------
S.cal = { events: [], settings: null, error: '', at: 0, earnings: [], earnings_settings: null, earnings_error: '' };
const calOpen = () => $('#tab-calendar').classList.contains('on') && !$('main').classList.contains('side-off');
const IMPACT_COLOR = { High: '#f23645', Medium: '#ff9800', Low: '#f5d04a', Holiday: '#787b86' };
function calFilter() {
  let imp = ['High'];
  try { const v = JSON.parse(local.get('calImp', '["High"]')); if (Array.isArray(v)) imp = v; } catch { /* default */ }
  return { impacts: imp, ccy: local.get('calCcy', 'USD') };
}
const calShown = (e, f = calFilter()) => f.impacts.includes(e.impact) && (!f.ccy || e.country === f.ccy);
// Major earnings (the server's list: its market-cap floor and added tickers), with USD or All. Nasdaq gives no exact
// time: before the open sits at 06:00 CT, after the close at 15:05 CT.
const EARN_COLOR = '#42a5f5';
const EARN_WHEN = { BMO: 'Before the open', AMC: 'After the close', '': 'Time not given' };
const earnOn = () => local.get('calEarn', '1') === '1';   // the Earnings button (this device)
const earnShown = (f = calFilter()) => (earnOn() && (!f.ccy || f.ccy === 'USD') ? S.cal.earnings : []);
const fmtCap = (c) => (c >= 1e12 ? `$${(c / 1e12).toFixed(2)}T` : `$${Math.round(c / 1e9)}B`);
async function calLoad() {
  try { Object.assign(S.cal, await api('GET', '/api/calendar?days=14'), { at: Date.now() }); } catch (e) { S.cal.error = e.message; }
  calRender();
  econPrim.update();
}
function calRender() {
  const f = calFilter(), now = Date.now(), list = $('#cal-list');
  $('#cal-imp').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === 'Earnings' ? earnOn() : f.impacts.includes(b.dataset.v)));
  $('#cal-ccy').value = f.ccy;
  const st = S.cal.settings;
  if (st && document.activeElement !== $('#cal-min')) { $('#cal-warn').checked = st.warn; $('#cal-min').value = st.minutes; }
  const es = S.cal.earnings_settings;
  if (es) {
    if (document.activeElement !== $('#cal-cap')) $('#cal-cap').value = es.min_cap_b;
    if (document.activeElement !== $('#cal-syms')) $('#cal-syms').value = es.symbols.join(' ');
  }
  const shown = [...S.cal.events.filter((e) => calShown(e, f)), ...earnShown(f).map((e) => ({ ...e, earn: true, t: e.when || !e.day ? e.t : ax.input.toMs(`${e.day}T12:00`) ?? e.t }))]   // no time given: on its date wherever you are
    .filter((e) => e.t > now - 2 * 86400e3).sort((a, b) => a.t - b.t);
  const next = shown.find((e) => e.t >= now);
  const rows = [];
  let day = '';
  for (const e of shown) {
    const d = ax.calDay.format(e.t);   // days and times on the chart's time zone's clock
    if (d !== day) { day = d; const h = document.createElement('div'); h.className = 'cal-day'; h.textContent = d; rows.push(h); }
    const r = document.createElement('div');
    r.className = `cal-row${e.earn ? ' earn' : ''}${e.t < now ? ' past' : ''}${e === next ? ' next' : ''}`;
    const dot = document.createElement('i');
    dot.style.background = e.earn ? EARN_COLOR : IMPACT_COLOR[e.impact] || '#787b86';
    dot.title = e.earn ? 'Earnings' : `${e.impact} impact`;
    const nums = e.earn ? [e.eps != null && `EPS est ${e.eps}`, e.cap && fmtCap(e.cap), e.quarter && `qtr ${e.quarter}`].filter(Boolean).join('  ')
      : [e.actual && `A ${e.actual}`, e.forecast && `F ${e.forecast}`, e.previous && `P ${e.previous}`].filter(Boolean).join('  ');
    const cells = e.earn ? [e.when || '—', e.symbol, e.name, nums] : [ax.hm.format(e.t), e.country, e.title, nums];
    r.append(dot, ...cells.map((v, k) => { const s = document.createElement('span'); s.className = ['t', 'c', 'n', 'v'][k]; s.textContent = v; return s; }));
    if (e.earn) r.querySelector('.t').title = EARN_WHEN[e.when] ?? '';
    rows.push(r);
  }
  if (S.cal.earnings_error && S.cal.earnings_settings?.on && earnOn()) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = `Earnings not read from Nasdaq (${S.cal.earnings_error}); showing the last list.`; rows.unshift(p); }
  if (!rows.length) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = S.cal.error ? `Calendar: ${S.cal.error}` : S.cal.at ? 'Nothing scheduled with these filters.' : 'Loading…'; rows.push(p); }
  list.replaceChildren(...rows);
  if (calOpen() && next) list.querySelector('.cal-row.next')?.scrollIntoView({ block: 'nearest' });
}
function calWarnSave() {   // the warnings follow the tab's filters
  const f = calFilter(), ccys = f.ccy ? [f.ccy] : [...new Set(S.cal.events.map((e) => e.country))].filter((c) => /^[A-Z]{2,4}$/.test(c)).slice(0, 20);
  const minutes = Math.min(120, Math.max(1, Math.round(Number($('#cal-min').value) || 5)));
  api('PUT', '/api/calendar/settings', { warn: $('#cal-warn').checked, minutes, currencies: ccys, impacts: f.impacts.filter((x) => x !== 'Holiday') })
    .then((s) => { S.cal.settings = s; }).catch(fail);
}
$('#cal-imp').onclick = (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.v === 'Earnings') {   // shown or hidden here (list and chart); the server keeps reading them
    local.set('calEarn', earnOn() ? '0' : '1');
    if (earnOn() && S.cal.earnings_settings && !S.cal.earnings_settings.on) calEarnSave();   // switched off server-side before
    calRender(); econPrim.update();
    return;
  }
  const f = calFilter(), imp = f.impacts.includes(b.dataset.v) ? f.impacts.filter((x) => x !== b.dataset.v) : [...f.impacts, b.dataset.v];
  local.set('calImp', JSON.stringify(imp.length ? imp : ['High']));
  calRender(); econPrim.update();
  if ($('#cal-warn').checked) calWarnSave();
};
$('#cal-ccy').onchange = (e) => { local.set('calCcy', e.target.value); calRender(); econPrim.update(); if ($('#cal-warn').checked) calWarnSave(); };
$('#cal-warn').onchange = calWarnSave;
$('#cal-min').onchange = calWarnSave;
function calEarnSave() {
  const cap = Math.min(10000, Math.max(5, Number($('#cal-cap').value) || 200));
  const symbols = [...new Set($('#cal-syms').value.toUpperCase().split(/[\s,;]+/).filter(Boolean))];
  api('PUT', '/api/calendar/earnings', { on: true, min_cap_b: cap, symbols })
    .then((s) => { S.cal.earnings_settings = s; calLoad(); }).catch((e) => { fail(e); calRender(); });
}
$('#cal-cap').onchange = calEarnSave;
$('#cal-syms').onchange = calEarnSave;
setInterval(() => {
  if ((calOpen() || S.st.econ || newsOn()) && Date.now() - S.cal.at > 10 * 60e3) calLoad();
  else if (calOpen()) calRender();   // past rows grey out, the next one moves on
}, 30e3);
// The next release the Calendar shows (its impact and currency filters) within the hour, counting down left of the
// clock: red in its last 2 minutes, "now" for a minute after. A tap opens the Calendar.
const newsOn = () => local.get('newsCd', '1') === '1';
function newsTick() {
  const el = $('#news-pill'), now = Date.now(), f = calFilter();
  const e = newsOn() && !TK_MULTI ? S.cal.events.find((x) => x.impact !== 'Holiday' && calShown(x, f) && x.t > now - 60e3 && x.t - now <= 3600e3) : null;
  if (!e) { el.hidden = true; return; }
  const left = e.t - now, name = `${f.ccy ? '' : `${e.country} `}${e.title}`;
  el.textContent = `${name.length > 24 ? `${name.slice(0, 23)}…` : name} ${left > 0 ? `in ${fmtLeft(left)}` : 'now'}`;
  el.classList.toggle('hot', left <= 120e3);
  el.style.setProperty('--imp', IMPACT_COLOR[e.impact] || '#787b86');
  el.title = `${e.country} ${e.title} at ${ax.hm.format(e.t)} ${zs(e.t)}, ${e.impact.toLowerCase()} impact${e.forecast ? `, forecast ${e.forecast}` : ''}. Tap for the Calendar.`;
  el.hidden = false;
}
setInterval(newsTick, 1000);
$('#news-pill').onclick = () => document.querySelector('.tabs button[data-tab=calendar]')?.click();
$('#cal-cd').checked = newsOn();
$('#cal-cd').onchange = (e) => { local.set('newsCd', e.target.checked ? '1' : '0'); if (e.target.checked && Date.now() - S.cal.at > 10 * 60e3) calLoad(); newsTick(); };
if (newsOn()) setTimeout(() => { if (!S.cal.at) calLoad(); }, 2500);
// Dashed lines where releases fall (time charts shorter than a day), the title at the foot of the line.
const econPrim = {
  p: null,
  views: [{ zOrder: () => 'bottom', renderer: () => (econPrim.p ? { draw: (target) => target.useMediaCoordinateSpace(({ context, mediaSize }) => drawEcon(context, mediaSize)) } : null) }],
  attached(p) { this.p = p; },
  detached() { this.p = null; },
  paneViews() { return this.views; },
  update() { if (this.p) this.p.requestUpdate(); },
};
candles.attachPrimitive(econPrim);
function drawEcon(ctx, size) {
  if (!S.st.econ || !isTime() || calSpec() || !S.bars.length || !(S.cal.events.length || S.cal.earnings.length)) return;
  const ts = chart.timeScale(), r = ts.getVisibleLogicalRange();
  if (!r) return;
  const f = calFilter(), t0 = drawings.logicalToTime(r.from), t1 = drawings.logicalToTime(r.to + 2);
  const marks = S.cal.events.filter((e) => e.t >= t0 && e.t <= t1 && calShown(e, f))
    .map((e) => ({ t: e.t, col: IMPACT_COLOR[e.impact] || '#787b86', label: `${e.country} ${e.title}`.slice(0, 26) }));
  const earn = new Map();   // reports at the same time share one line: "MSFT GOOGL META +2 earnings"
  for (const e of earnShown(f)) if (e.t >= t0 && e.t <= t1) earn.set(e.t, [...(earn.get(e.t) || []), e.symbol]);
  for (const [t, syms] of earn) marks.push({ t, col: EARN_COLOR, label: `${syms.slice(0, 3).join(' ')}${syms.length > 3 ? ` +${syms.length - 3}` : ''} earnings` });
  marks.sort((a, b) => a.t - b.t);
  ctx.save();
  ctx.font = `10px ${css('--font')}`;
  ctx.textBaseline = 'bottom';
  let lastX = -1e9, row = 0;
  for (const e of marks) {
    const l = drawings.timeToLogical(e.t);
    if (l == null) continue;
    const x = Math.round(logicalX(ts, l)) + 0.5, col = e.col;
    ctx.strokeStyle = `${col}99`;
    ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, size.height); ctx.stroke();
    ctx.setLineDash([]);
    row = x - lastX < 90 ? row + 1 : 0;   // neighbours stack up instead of overlapping
    lastX = x;
    const label = e.label, w = ctx.measureText(label).width + 8, y = size.height - 34 - row * 15;
    ctx.fillStyle = 'rgba(10, 14, 15, 0.85)';
    ctx.fillRect(x + 2, y - 13, w, 14);
    ctx.fillStyle = col;
    ctx.fillText(label, x + 6, y);
  }
  ctx.restore();
}

const bigPrim = {
  p: null,
  views: [{ zOrder: () => 'top', renderer: () => (bigPrim.p ? { draw: (target) => target.useMediaCoordinateSpace(({ context }) => drawBig(context)) } : null) }],
  attached(p) { this.p = p; },
  detached() { this.p = null; },
  paneViews() { return this.views; },
  update() { if (this.p) this.p.requestUpdate(); },
};
candles.attachPrimitive(bigPrim);
chart.timeScale().subscribeVisibleLogicalRangeChange(() => { if (bigOn()) bigSchedule(); });
$('#big-min').onchange = (e) => {
  const n = Math.round(Number(e.target.value));
  if (!(n >= 1 && n <= 100000)) { e.target.value = bigMin(); toast('Big trades: 1 to 100000 contracts'); return; }
  local.set('bigMin', JSON.stringify({ ...bigMins(), [S.symbol]: n }));
  S.bigKey = '';
  tapeSync();
  bigSchedule(0);
  tapeLoad();   // its gold rows follow this size
};
function renderBigMenu() { $('#big-min').value = bigMin(); }

function tapeRow(t, fresh) {
  const row = document.createElement('div'), [ts, p, sz, side] = t;
  row.className = `tp-row ${side > 0 ? 'buy' : side < 0 ? 'sell' : ''}${sz >= bigMin() ? ' big' : ''}${fresh ? ' new' : ''}`;
  row.title = t[4] > 1 ? `${t[4]} fills at the same millisecond` : '';
  for (const v of [ax.tape.format(ts), px(p), String(sz)]) { const s = document.createElement('span'); s.textContent = v; row.append(s); }
  return row;
}
// Newest at the top; when scrolled down to read, new trades come in above without moving the view.
function tapeAdd(trades, fresh = true) {
  const list = $('#tp-list');
  if (!trades.length) return;
  const keep = list.scrollTop > 4 ? list.scrollHeight - list.scrollTop : null;
  list.prepend(...trades.slice().reverse().map((t) => tapeRow(t, fresh)));
  if (keep != null) list.scrollTop = list.scrollHeight - keep;   // before the oldest go: rows taken out below stay put
  while (list.childElementCount > 400) list.lastElementChild.remove();
}
let tapeLoaded = '', tapeHeard = [];   // the list's symbol and size, and the live trades it showed for them (the last 400)
async function tapeLoad() {
  if (!tapeOpen() || !S.instruments[S.symbol]) return;
  const sym = S.symbol, min = tapeMin, now = Date.now();
  $('#tp-sym').textContent = sym;
  $('#tp-min').value = tapeMin;
  if (tapeLoaded !== `${sym} ${min}`) tapeHeard = [];
  tapeLoaded = `${sym} ${min}`;
  try {
    const d = await api('GET', `/api/trades?symbol=${sym}&start=${now + 60_000 - 3 * 86_400_000}&end=${now + 60_000}&min=${min}&limit=200&newest=true`);
    if (tapeLoaded !== `${sym} ${min}`) return;
    const last = d.trades.length ? d.trades[0][0] : -Infinity;   // newest first, read once: what printed later stays
    $('#tp-list').replaceChildren();
    tapeAdd(d.trades.reverse(), false);
    tapeAdd(tapeHeard.filter((t) => t[0] > last), false);
  } catch (e) { toast(`Tape: ${e.message}`); }
}
$('#tp-min').onchange = (e) => {
  const n = Math.round(Number(e.target.value));
  if (!(n >= 1 && n <= 100000)) { e.target.value = tapeMin; return; }
  tapeMin = n;
  local.set('tapeMin', String(n));
  tapeSync();
  tapeLoad();
};
function onTrades(m) {
  if (m.symbol !== S.symbol) return;
  if (bigOn()) {
    const min = bigMin();
    for (const t of m.trades) if (t[2] >= min) S.big.push(t);
    if (S.big.length > 20000) { S.big.splice(0, S.big.length - 20000); S.bigFrom = S.big[0][0]; }
    bigPrim.update();
  }
  if (tapeOpen() && tapeLoaded === `${S.symbol} ${tapeMin}`) {
    const ts = m.trades.filter((t) => t[2] >= tapeMin);
    tapeHeard.push(...ts);
    if (tapeHeard.length > 400) tapeHeard.splice(0, tapeHeard.length - 400);
    tapeAdd(ts);
  }
}
// The symbol changed (or the chart the page follows): both start over.
function tapeSymbol() {
  S.fills = [];
  S.fillTrades = [];
  S.fillsFor = '';
  if (typeof journalLoad === 'function' && journalOpen() && $('#jr-sym').checked) journalLoad();
  S.big = [];
  S.bigKey = '';
  bigPrim.update();
  renderBigMenu();
  tapeSync();
  if (tapeOpen()) tapeLoad();
}

// ---- your NinjaTrader fills (read by the add-on; this app places no orders): markers and finished trades on the
// chart, and the Journal tab ------------------------------------------------------------------------------------------
S.fills = [];
S.fillTrades = [];
S.fillsFor = '';   // the symbol, account and first bar they are for; S.fillsOpen: the oldest open trade's entry (or null)
const fillAcct = () => local.get('fillAcct', '');
const money = (v) => `${v < 0 ? '−' : v > 0 ? '+' : ''}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
// The next ask (fillWant): everything since the chart's first bar (null), or only what new fills from that time on
// change (a new fill); undefined: none waiting. One at a time: what comes meanwhile is asked once that one is in.
let fillTimer = 0, fillBusy = false, fillAgain = false, fillWant;
function fillsSchedule(ms = 400, since = null) {
  fillWant = since == null || fillWant === null ? null : Math.min(fillWant ?? Infinity, since);
  clearTimeout(fillTimer);
  fillTimer = setTimeout(fillsFetch, ms);
}
// Where fills from `ts` on can change the trades: back past every trade still open or closed since, to where every
// position was flat. The fills and trades before it stay as they are.
function fillsFrom(ts) {
  let from = Math.min(ts, S.fillsOpen ?? Infinity);
  for (let i = S.fillTrades.length - 1; i >= 0; i--) {   // by entry, newest first: one pass goes back far enough
    const t = S.fillTrades[i];
    if (t.entry_ts < from && t.exit_ts >= from) from = t.entry_ts;
  }
  return from;
}
async function fillsFetch() {
  if (fillBusy) { fillAgain = true; return; }
  const want = fillWant;
  fillWant = undefined;
  if (!S.st.fills || TK_MULTI || !S.bars.length || S.ext) { S.fills = []; S.fillTrades = []; S.fillsFor = ''; fillPrim.update(); return; }
  const sym = S.symbol, first = S.bars[0].t, acct = fillAcct(), key = `${sym} ${acct} ${first}`;
  const from = want != null && S.fillsFor === key ? Math.max(fillsFrom(want), first) : first, part = from > first;
  const q = `start=${from}&end=${Date.now() + 60_000}&symbol=${sym}${acct ? `&account=${encodeURIComponent(acct)}` : ''}`;
  fillBusy = true;
  try {   // the chart's lines need no MAE or MFE
    const [f, j] = await Promise.all([api('GET', `/api/fills?${q}`), api('GET', `/api/journal?${q}&excursions=false${part ? '&flat=true' : ''}`)]);
    if (key !== `${S.symbol} ${fillAcct()} ${S.bars[0]?.t}` || (part && S.fillsFor !== key)) {   // another symbol or account
      fillWant = null;   // picked meanwhile: asked again
      fillAgain = true;
    } else {
      const done = j.trades.filter((t) => !t.open);
      S.fills = part ? [...S.fills.filter((x) => x.ts < from), ...f.fills] : f.fills;
      S.fillTrades = part ? [...S.fillTrades.filter((t) => t.exit_ts < from), ...done] : done;
      S.fillsOpen = j.open_since;
      S.fillsFor = key;
      fillAccounts(f.accounts);
    }
  } catch {
    S.fillsFor = '';   // none yet: the next ask is for everything
  } finally { fillBusy = false; }
  fillPrim.update();
  if (fillAgain) { fillAgain = false; fillsFetch(); }
}
function fillAccounts(list) {
  for (const sel of [$('#fills-acct'), $('#jr-acct')]) {
    const v = sel.id === 'fills-acct' ? fillAcct() : local.get('jrAcct', '');
    sel.replaceChildren(new Option('All accounts', ''), ...list.map((a) => new Option(a, a)));
    sel.value = list.includes(v) ? v : '';
  }
}
$('#fills-acct').onchange = (e) => { local.set('fillAcct', e.target.value); fillsSchedule(0); };
// A fill is an arrow beside its bar, pointing at the fill price from the left (off the candle, so it shows): a buy in
// the up color, a sell in the down color, its size beside it. A finished trade: a dotted line from the average entry
// to the average exit, its net P&L at the exit (none for one closed by a fill the app never got: no exit price).
function drawFills(ctx) {
  if (!S.st.fills || TK_MULTI || !S.bars.length) return;
  const ts = chart.timeScale(), gap = Math.max(2, ts.options().barSpacing * 0.4) + 2;   // the candle's half width and a gap
  const at = (t) => { const i = tradeBar(t); return i == null ? null : logicalX(ts, i); };
  const cut = S.replay ? S.replay.at : Infinity;
  ctx.save();
  ctx.font = `600 11px ${css('--font')}`;
  ctx.textAlign = 'center';
  for (const t of S.fillTrades) {
    if (t.exit_ts > cut || t.exit == null) continue;
    let x0 = at(t.entry_ts), x1 = at(t.exit_ts);
    const y0 = candles.priceToCoordinate(t.entry), y1 = candles.priceToCoordinate(t.exit);
    if ([x0, x1, y0, y1].includes(null)) continue;
    x0 -= gap; x1 -= gap;   // from arrow tip to arrow tip
    const col = t.net > 0 ? '#26a69a' : '#ef5350';
    ctx.strokeStyle = col;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
    ctx.setLineDash([]);
    const label = money(t.net), w = ctx.measureText(label).width + 10, lx = x1 + 2 * gap + 4;   // right of the exit bar
    ctx.fillStyle = col;
    ctx.fillRect(lx, y1 - 9, w, 18);
    ctx.fillStyle = '#ffffff';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, lx + w / 2, y1);
  }
  for (const f of S.fills) {
    if (f.ts > cut) continue;
    const x = at(f.ts), y = candles.priceToCoordinate(f.price);
    if (x == null || y == null) continue;
    const buy = f.side > 0, s = 6, tip = x - gap, base = tip - s * 1.6, col = buy ? C.up : C.down;
    ctx.fillStyle = col;
    ctx.strokeStyle = '#000000';
    ctx.lineWidth = 1;
    ctx.beginPath();   // ► its tip at the fill price, just left of the candle
    ctx.moveTo(tip, y); ctx.lineTo(base, y - s); ctx.lineTo(base, y + s);
    ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.fillRect(base - 5, y - 1, 5, 2);   // a short shaft
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillText(`${buy ? '+' : '−'}${f.qty}`, base - 7, y);
    ctx.textAlign = 'center';
  }
  ctx.restore();
}
const fillPrim = {
  p: null,
  views: [{ zOrder: () => 'top', renderer: () => (fillPrim.p ? { draw: (target) => target.useMediaCoordinateSpace(({ context }) => drawFills(context)) } : null) }],
  attached(p) { this.p = p; },
  detached() { this.p = null; },
  paneViews() { return this.views; },
  update() { if (this.p) this.p.requestUpdate(); },
};
candles.attachPrimitive(fillPrim);

// ---- layers: which study draws in front of which, set in the Studies menu (On this chart, front first) -------------
// A pane paints in three passes: primitives marked 'bottom', then the series in their order (each followed by its
// 'normal' primitives), then 'top'; within a pass, primitives paint in the order attached. The drawn studies hang on
// the candles: behind the bars they are 'bottom'; in front, 'top', or 'normal' (right after the bars) when a line study
// (VWAP, EMA, HTF EMA: series, ordered around the candles) is in front of them. So behind the bars the line studies are
// always in front of the drawn ones, and in front of the bars a drawn study can't sit between two line studies: the
// menu shows the order as drawn. Drawings and alert lines are always in front.
const LAYER_PRIMS = { fills: fillPrim, big: bigPrim, lines: llPrim, amt: amtPrim, tpo: tpoPrim, gap: gapPrim, ifthen: ifPrim,
  econ: econPrim, right: rangePrim, ...vpPrims };
const vwapLines = (k) => { const w = VWAPS.find((x) => x.key === k); return [w.mid, ...w.bands.flatMap((b) => [b.up, b.dn])]; };
const LAYER_LINES = { ve: () => vwapLines('ve'), vr: () => vwapLines('vr'), avwap: () => [avwap], hema: () => [hemaLine], dsma: () => dsmaLines,
  ...Object.fromEntries(Object.keys(IND_DEFS).filter((t) => !IND_DEFS[t].pane)
    .map((t) => [`i:${t}`, () => S.inds.filter((x) => x.c.type === t).flatMap((x) => x.plots.map((p) => p.s))])) };
// As it was drawn before there was a choice: front first.
const LAYER_DEFAULT = ['fills', 'big', 'lines', 'amt', 'tpo', 'gap', 'ifthen', ...VP_KINDS, ...Object.keys(LAYER_LINES).filter((id) => id.startsWith('i:')),
  'hema', 'dsma', 'avwap', 'vr', 've', 'bars', 'econ', 'right'];
for (const p of Object.values(LAYER_PRIMS)) for (const v of p.views) { const z = v.zOrder; v.zOrder = () => p._z || z(); }
function layersClean(a) {   // every layer once; one it has not seen goes next to its neighbour in the default
  const out = Array.isArray(a) ? [...new Set(a.filter((id) => LAYER_DEFAULT.includes(id)))] : [];
  LAYER_DEFAULT.forEach((id, i) => {
    if (out.includes(id)) return;
    const prev = LAYER_DEFAULT.slice(0, i).reverse().find((x) => out.includes(x));
    out.splice(prev ? out.indexOf(prev) + 1 : 0, 0, id);
  });
  return out;
}
function loadLayers() {
  return layersClean(ownTab() ? ownTab().own.layers : rawLocal('layers', []));
}
S.layers = loadLayers();
const layerOn = (id) => id === 'bars' || stOn(ST_CAT.find((x) => x.id === id));
// The studies that are on, as drawn (front first), and the pass each paints in.
function layerPlan(order) {
  const on = order.filter(layerOn), b = on.indexOf('bars'), front = on.slice(0, b), back = on.slice(b + 1), line = (id) => id in LAYER_LINES;
  const k = front.findIndex(line), top = k < 0 ? front : front.slice(0, k), under = k < 0 ? [] : front.slice(k);
  const lines = under.filter(line), normal = under.filter((id) => !line(id)), backLines = back.filter(line), bottom = back.filter((id) => !line(id));
  return { top, lines, normal, backLines, bottom, order: [...top, ...lines, ...normal, 'bars', ...backLines, ...bottom] };
}
// The line studies and the bars, back to front: the series order of the price pane.
function layerSeries() {
  const plan = layerPlan(S.layers), lines = (ids) => ids.slice().reverse().flatMap((id) => LAYER_LINES[id]());
  const want = [...lines(plan.backLines), vol, ...(altSeries ? [altSeries] : []), candles, ...lines(plan.lines)];   // alt bars under the see-through candles
  const all = chart.panes()[0].getSeries(), order = [...want.filter((s) => all.includes(s)), ...all.filter((s) => !want.includes(s))];
  if (order.every((s, i) => all[i] === s)) return;
  order.forEach((s, i) => s.setSeriesOrder(i));
}
let layerAttached = [];
function applyLayers() {
  const plan = layerPlan(S.layers);
  for (const [z, ids] of [['top', plan.top], ['normal', plan.normal], ['bottom', plan.bottom]]) for (const id of ids) LAYER_PRIMS[id]._z = z;
  const back2front = (ids) => ids.slice().reverse().map((id) => LAYER_PRIMS[id]);
  const drawn = [...back2front(plan.bottom), ...back2front(plan.normal), ...back2front(plan.top)];
  const seq = [...Object.values(LAYER_PRIMS).filter((p) => !drawn.includes(p)), sessionBreaks, fpPrim, dsmaPrim, ...drawn, alertPrim, drawings.prim, countdown];
  if (seq.some((p, i) => layerAttached[i] !== p)) {   // attach order is paint order within a pass
    for (const p of seq) if (p.p) candles.detachPrimitive(p);
    for (const p of seq) candles.attachPrimitive(p);
    layerAttached = seq;
  }
  S.layersReady = true;
  layerSeries();
}
function setLayers(order) {
  S.layers = layersClean(order);
  if (!studyStore('layers', [...S.layers])) local.set('layers', JSON.stringify(S.layers));
  cellPost('state');
  applyLayers();
  stRender();
}
applyLayers();

const journalOpen = () => $('#tab-journal').classList.contains('on') && !$('main').classList.contains('side-off');
let jrReq = 0;
async function journalLoad() {
  if (!journalOpen()) return;
  const id = ++jrReq;   // only the newest request's answer is shown: a longer period's comes later
  const days = Number($('#jr-range').value), acct = local.get('jrAcct', ''), only = $('#jr-sym').checked;
  $('#jr-sym-name').textContent = S.symbol;
  const now = Date.now(), end = now + 60_000, ct = new Date(new Date(now).toLocaleString('en-US', { timeZone: 'America/Chicago' }));
  const sinceToday = ((ct.getHours() * 60 + ct.getMinutes()) * 60 + ct.getSeconds()) * 1000 + now % 1000 + (ct.getHours() >= 17 ? -17 * 3600e3 : 7 * 3600e3);   // since the 17:00:00 open
  const start = days === 1 ? now - sinceToday : now - days * 86_400_000;
  const q = `start=${Math.round(start)}&end=${end}${acct ? `&account=${encodeURIComponent(acct)}` : ''}${only ? `&symbol=${S.symbol}` : ''}`;
  let j;
  try { j = await api('GET', `/api/journal?${q}`); } catch (e) { if (id === jrReq) $('#jr-list').replaceChildren(Object.assign(document.createElement('p'), { className: 'jr-empty', textContent: e.message })); return; }
  if (id !== jrReq) return;
  fillAccounts(j.accounts);
  const st = j.stats, cell = (label, v, cls = '') => { const d = document.createElement('div'); d.innerHTML = '<small></small><b></b>'; d.firstChild.textContent = label; d.lastChild.textContent = v; if (cls) d.lastChild.className = cls; return d; };
  const n2 = (v) => (v == null ? '—' : money(v));
  $('#jr-stats').replaceChildren(cell('Net P&L', n2(st.net), st.net > 0 ? 'win' : st.net < 0 ? 'loss' : ''), cell('Trades', `${st.trades}${st.open ? ` +${st.open} open` : ''}`),
    cell('Win rate', st.win_rate == null ? '—' : `${st.win_rate.toFixed(0)}%`), cell('Avg win', n2(st.avg_win)), cell('Avg loss', n2(st.avg_loss)),
    cell('Profit factor', st.profit_factor == null ? '—' : st.profit_factor.toFixed(2)));
  $('#jr-stats').querySelectorAll('.win').forEach((e) => { e.style.color = css('--up'); });
  $('#jr-stats').querySelectorAll('.loss').forEach((e) => { e.style.color = css('--down'); });
  drawJournalDays(st.days);
  const list = $('#jr-list');
  if (!j.trades.length) { list.replaceChildren(Object.assign(document.createElement('p'), { className: 'jr-empty', textContent: j.accounts.length ? 'No trades in this period.' : 'No fills yet: they arrive from the NinjaTrader add-on (re-copy TatankaChartFeed.cs and press F5).' })); return; }
  list.replaceChildren(...j.trades.slice().reverse().map((t) => {
    const row = document.createElement('div'), a = document.createElement('span'), p = document.createElement('span'), s1 = document.createElement('small'), s2 = document.createElement('small');
    row.className = 'jr-row';
    const dec = S.instruments[t.symbol]?.decimals ?? 2, f = (v) => (v == null ? '…' : v.toFixed(dec));
    a.className = 'a';
    a.innerHTML = `<span class="${t.side}"></span> `;
    a.firstChild.textContent = `${t.side === 'long' ? 'Long' : 'Short'} ${t.qty}`;
    a.append(` ${t.contract}`);
    p.className = `p ${t.net > 0 ? 'win' : t.net < 0 ? 'loss' : ''}`;
    p.textContent = t.open ? 'open' : t.net == null ? 'exit unseen' : money(t.net);
    s1.textContent = `${ax.md.format(t.entry_ts)} · ${f(t.entry)} → ${f(t.exit)}${t.points != null ? ` (${t.points >= 0 ? '+' : ''}${t.points.toFixed(dec)})` : ''}`;
    s2.textContent = [t.mae != null && `MAE ${t.mae.toFixed(dec)}`, t.mfe != null && `MFE ${t.mfe.toFixed(dec)}`, t.account].filter(Boolean).join(' · ');
    row.append(a, p, s1, s2);
    row.title = [`${t.fills} fills, commission $${t.commission.toFixed(2)}`,
      t.from_position && "Entry: NinjaTrader's average price for the position (fills before it never reached the app)",
      t.unseen_exit && 'Closed by a fill that never reached the app: no exit price or P&L'].filter(Boolean).join('\n');
    row.onclick = () => journalShow(t);
    return row;
  }));
}
function drawJournalDays(days) {
  const cv = $('#jr-days'), dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  if (!days.length) return;
  const max = Math.max(...days.map((d) => Math.abs(d.net)), 1), mid = H / 2, bw = Math.max(2, Math.min(18, (W - 8) / days.length - 2));
  ctx.strokeStyle = css('--line'); ctx.beginPath(); ctx.moveTo(0, mid + 0.5); ctx.lineTo(W, mid + 0.5); ctx.stroke();
  days.forEach((d, i) => {
    const h = (Math.abs(d.net) / max) * (mid - 6), x = W - 4 - (days.length - i) * (bw + 2);
    ctx.fillStyle = d.net >= 0 ? css('--up') : css('--down');
    ctx.fillRect(x, d.net >= 0 ? mid - h : mid, bw, Math.max(1, h));
  });
}
// A trade from the journal: its symbol on the chart (the active chart in a layout), scrolled to its entry.
function journalShow(t) {
  if (!S.instruments[t.symbol]) { toast(`${t.symbol} is not a chart symbol here`); return; }
  if (TK_MULTI) { workspaces.toActive({ type: 'journal', symbol: t.symbol, entry_ts: t.entry_ts }); return; }   // that chart scrolls to it itself
  if (S.symbol === t.symbol && S.bars.length) { goToTime(t.entry_ts); return; }
  S.goTo = t.entry_ts;   // when its history is in (applyHistory)
  setSymbol(t.symbol);
}
function goToTime(ms) {
  const i = barIndexTo(S.bars, ms);
  if (i < 0) { toast('That trade is older than the bars loaded: pick a longer timeframe'); return; }
  chart.timeScale().setVisibleLogicalRange({ from: i - 40, to: i + 60 });
}
['#jr-range', '#jr-sym'].forEach((q) => { $(q).onchange = journalLoad; });
$('#jr-acct').onchange = (e) => { local.set('jrAcct', e.target.value); journalLoad(); };
let jrTimer = 0;
function onFill(m) {   // a new fill: markers (the chart's symbol and account only, from the fill on) and the journal refresh
  const f = m.fill;
  if (!f || (f.symbol === S.symbol && (!fillAcct() || f.account === fillAcct()))) fillsSchedule(300, f?.ts);
  if (journalOpen()) { clearTimeout(jrTimer); jrTimer = setTimeout(journalLoad, 300); }   // several at once: asked once
}

// ---- workspaces and layouts (layout.js) --------------------------------------------------------------------------------
// The page itself (one chart, or the page around several) keeps the workspaces; a chart in a layout talks to it.
workspaces = TK_CELL === null ? createWorkspaces({
  api, toast, fail, ask, local,
  onActive: (m) => hostActive(m),
  reload: reloadWhenIdle,
}) : null;
workspaces?.render();
// Several charts: the Plan tab, watchlist highlight and the Studies boxes on the Plan tab follow the chart last clicked.
function hostActive(m) {
  if (m.set && typeof m.set === 'object') {   // its open tab's study settings: what the Indicators dialog here edits
    const moved = JSON.stringify(m.set) !== JSON.stringify(S.cellSet);
    S.cellSet = m.set;
    S.cellWhere = typeof m.where === 'string' ? m.where : '';
    if (moved) { indSets(); renderAmtMenu(); }
  }
  if (Array.isArray(m.inds)) {   // its indicators, order and hidden ones: what the Studies menu here shows and edits
    const inds = m.inds.map(indClean).filter(Boolean).slice(0, IND_MAX);
    if (JSON.stringify(inds) !== JSON.stringify(S.indCfg)) S.indCfg = inds;
  }
  if (Array.isArray(m.layers)) S.layers = layersClean(m.layers);
  if (m.hidden && typeof m.hidden === 'object') S.stHidden = { ...m.hidden };
  if (m.st) { S.st = { ...S.st, ...m.st }; renderStudiesMenu(); indDlg.refresh(); }
  if (SPEC_RE.test(m.spec || '')) S.spec = m.spec;   // the alert dialog's default timeframe
  if (typeof m.rth === 'boolean') S.rth = m.rth;   // and the hours a new indicator alert computes on, as the chart shows
  if (!chartSym(m.sym) || m.sym === S.symbol) return;
  S.symbol = m.sym;
  S.spec = m.spec;
  watch.markActive();
  indSets();
  renderAmtMenu();   // the symbol's AMT alert switch and alert settings
  indDlg.refresh();
  renderPlanLines(true);
  S.lvStatus = null;
  loadPlanLevels();
  subscribe();
  renderAlerts();
}
// A chart in a layout: says which symbol, timeframe and studies it shows, moves the other crosshairs, and takes
// symbols, timeframes, studies and alerts from the page around it.
function cellPost(type) {
  if (TK_CELL === null || !chartSym(S.symbol)) return;
  window.parent.postMessage({ tk: true, type, sym: S.symbol, spec: S.spec, st: S.st, set: tabSet(), where: setWhere(), inds: S.indCfg,
    layers: S.layers, hidden: S.stHidden, rth: S.rth, linked: linkedChange, forced: specForced === S.spec }, location.origin);
  specForced = '';
}
let xhairFrame = 0, xhairMs;
function cellXhair(ms) {
  xhairMs = ms;
  if (!xhairFrame) xhairFrame = requestAnimationFrame(() => { xhairFrame = 0; window.parent.postMessage({ tk: true, type: 'xhair', ms: xhairMs }, location.origin); });
}
if (TK_CELL !== null) {
  document.addEventListener('pointerdown', () => cellPost('active'), true);
  $('#chart').addEventListener('pointerleave', () => cellXhair(null));
  window.addEventListener('focus', () => cellPost('active'));
  window.addEventListener('message', (e) => {
    if (e.origin !== location.origin || !e.data?.tk) return;
    const m = e.data;
    if (m.type === 'symbol' || m.type === 'spec') {
      linkedChange = !!m.link;
      try { if (m.type === 'symbol') setSymbol(m.sym); else setSpec(m.spec); } finally { linkedChange = false; }
    }
    else if (m.type === 'study') { if (!replayBlocks(m.key)) { S.st[m.key] = m.on; studiesChanged(m.key); } }
    else if (m.type === 'indset' && typeof m.key === 'string' && m.value && typeof m.value === 'object') tabSetSave(m.key, m.value);
    else if (m.type === 'indall' && typeof m.key === 'string') studyAllHere(m.key);
    else if (m.type === 'stpart') cellPart(m.part, m.value);
    else if (m.type === 'set' && m.key === 'chartSet') { try { const cs = JSON.parse(m.value); applyCs(cs); csToAllTabs(csFrom(cs)); } catch { /* not settings */ } }
    else if (m.type === 'alert') { setSymbol(m.sym); openAlertDialog({ price: m.price }); }
    else if (m.type === 'key') quickOpen(m.key);
    else if (m.type === 'journal') journalShow({ symbol: m.symbol, entry_ts: m.entry_ts });   // a trade clicked in the page's Journal
    else if (m.type === 'xhair') {
      S.xhairLinked = true;   // the crosshair moves this makes are not the pointer's (the library tells them later, with no pointer event)
      if (m.ms == null || !S.bars.length) { chart.clearCrosshairPosition(); return; }
      let lo = 0, hi = S.bars.length - 1;   // the bar this chart has at that moment
      if (S.bars[0].t > m.ms) { chart.clearCrosshairPosition(); return; }
      while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (S.bars[mid].t <= m.ms) lo = mid; else hi = mid - 1; }
      chart.setCrosshairPosition(S.bars[lo].c, timeOf(S.bars[lo]), candles);
    }
  });
}

if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
start();
if (stReset && TK_CELL === null) {   // the studies were reset to the NinjaTrader chart set: say so, and save it in the workspace
  setTimeout(() => { toast('Studies now match the NinjaTrader chart. Studies ▾ adds the rest.'); local.set('studies', JSON.stringify(S.st)); }, 4000);
}
