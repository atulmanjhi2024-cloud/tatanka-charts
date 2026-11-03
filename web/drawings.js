'use strict';
/* Chart drawings, as on TradingView: horizontal and vertical lines, rays, trend lines, arrows, parallel channels,
   rectangles, fib retracements and trend-based extensions, long and short positions, price and date ranges, text.
   Stored on the server per symbol, so every device shows the same ones and edits arrive live. Points are
   {t: epoch ms, p: price}, so a drawing keeps its place on every timeframe. Drawn by one series primitive;
   placed, selected, moved and edited with the mouse or a finger. */

const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.618];
const FIB_COLORS = {
  0: '#787b86', 0.236: '#f23645', 0.382: '#ff9800', 0.5: '#4caf50', 0.618: '#089981', 0.786: '#00bcd4',
  1: '#787b86', 1.618: '#2962ff', 2.618: '#f23645', 3.618: '#9c27b0', 4.236: '#e91e63',
};
const FIB_EXT_LEVELS = [0, 0.618, 1, 1.272, 1.618, 2, 2.618];
const DRAW_DEFAULTS = {
  hline: { color: '#2962ff', width: 1, dash: 'solid', showPrice: true, text: '' },
  hray: { color: '#2962ff', width: 1, dash: 'solid', showPrice: true, text: '' },
  trend: { color: '#2962ff', width: 2, dash: 'solid', extendLeft: false, extendRight: false, text: '' },
  rect: { color: '#9c27b0', width: 1, dash: 'solid', fill: '#9c27b0', fillOpacity: 15, mid: false, extendLeft: false, extendRight: false, text: '' },
  fib: { color: '#787b86', width: 1, dash: 'solid', levels: FIB_LEVELS, fillOpacity: 8, extendLeft: false, extendRight: false, reverse: false },
  text: { color: '#d1d4dc', fontSize: 14, background: true, text: 'Text' },
  vline: { color: '#2962ff', width: 1, dash: 'solid', text: '' },
  arrow: { color: '#f5d04a', width: 2, dash: 'solid', text: '' },
  channel: { color: '#2962ff', width: 1, dash: 'solid', fillOpacity: 10, mid: true, extendLeft: false, extendRight: false },
  fibext: { color: '#787b86', width: 1, dash: 'solid', levels: FIB_EXT_LEVELS, fillOpacity: 0, extendRight: false },
  long: { qty: 1, text: '' },
  short: { qty: 1, text: '' },
  prange: { color: '#2962ff', text: '' },
  drange: { color: '#2962ff', text: '' },
};
// Points each type is placed with (a position is placed with one click and gets its stop and target from it).
const POINTS = { trend: 2, rect: 2, fib: 2, arrow: 2, prange: 2, drange: 2, channel: 3, fibext: 3 };
const POSITION = { long: 1, short: 1 };
const NUM_LIMITS = { width: [1, 8], fontSize: [8, 48], fillOpacity: [0, 100] };   // chartsrv/drawings.py
const DASH = { solid: [], dashed: [6, 4], dotted: [1.5, 3] };

const rgbaHex = (hex, a) => `rgba(${parseInt(hex.slice(1, 3), 16)}, ${parseInt(hex.slice(3, 5), 16)}, ${parseInt(hex.slice(5, 7), 16)}, ${a})`;
const fibPrice = (d, level) => (d.style.reverse ? d.pts[0].p + (d.pts[1].p - d.pts[0].p) * level
  : d.pts[1].p + (d.pts[0].p - d.pts[1].p) * level);
const newDrawingId = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
const clone = (x) => JSON.parse(JSON.stringify(x));

// ---- market time ------------------------------------------------------------------------------------
// Time bars come only while the market trades: Sunday 17:00 to Friday 16:00 CT less the daily 16:00-17:00 break and
// CME's holiday closures (hello's holidays: halts, early closes, closed days, per holiday group). marketMs(ts) is the
// ms it has traded up to ts from a fixed start (chartsrv/sessions.py market_ms, its twin), so the gap between two
// moments in it is what a seconds, minute or hour chart has bars for: a trend line runs straight in it.
const MK_HOUR = 3600e3, MK_DAY = 24 * MK_HOUR, MK_WEEK = 7 * MK_DAY, MK_SESSION = 23 * MK_HOUR;
const MK_ORIGIN = 3 * MK_DAY + 17 * MK_HOUR;   // Sunday 4 January 1970 17:00, on Chicago's clock read as UTC
const mkFmt = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', hourCycle: 'h23' });
const mkOffs = new Map(), mkSpans = new WeakMap();
function mkWall(ms) {   // Chicago's clock read as UTC, its offset found once per UTC hour (app.js wallClock)
  const h = Math.floor(ms / MK_HOUR);
  let off = mkOffs.get(h);
  if (off === undefined) {
    if (mkOffs.size > 50000) mkOffs.clear();
    const p = Object.fromEntries(mkFmt.formatToParts(h * MK_HOUR).map((x) => [x.type, Number(x.value)]));
    mkOffs.set(h, (off = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute) - h * MK_HOUR));
  }
  return ms + off;
}
// Open ms on the weekly hours alone. DST changes on a Sunday at 02:00, while the market is shut, so an open hour is an
// hour on Chicago's clock too.
function mkWeek(ms) {
  const r = mkWall(ms) - MK_ORIGIN, w = Math.floor(r / MK_WEEK), d = r - w * MK_WEEK, k = Math.floor(d / MK_DAY);
  return w * 5 * MK_SESSION + (k < 5 ? k * MK_SESSION + Math.min(d - k * MK_DAY, MK_SESSION) : 5 * MK_SESSION);
}
function mkCt(day, hhmm, plusDays = 0) {   // "YYYY-MM-DD" + "HH:MM" on Chicago's clock, as epoch ms
  const [y, m, d] = day.split('-').map(Number), [hh, mm] = hhmm.split(':').map(Number), wall = Date.UTC(y, m - 1, d + plusDays, hh, mm);
  const t = wall - (mkWall(wall) - wall);
  return wall - (mkWall(t) - t);
}
// The holiday closures of a group, sorted, with the open ms shut before each (sessions.py _intervals, _shut_spans), and
// per bar length what each cuts off the bar it falls in, with the sums before each (_cut_bars).
function mkCuts(c, per) {
  let got = c.cuts.get(per);
  if (!got) {
    const cut = c.starts.map((s) => (mkWeek(s + 1) > mkWeek(s) ? ((-s % per) + per) % per : 0)), before = [0];
    for (const x of cut) before.push(before[before.length - 1] + x);
    c.cuts.set(per, (got = { cut, before }));
  }
  return got;
}
function mkClosures(holidays, group) {
  let byGroup = mkSpans.get(holidays);
  if (!byGroup) mkSpans.set(holidays, (byGroup = new Map()));
  let c = byGroup.get(group);
  if (!c) {
    const at = (v) => (v && typeof v === 'object' ? v[group] ?? v.default : v), spans = [];
    for (const [day, h] of Object.entries(holidays)) {
      if (h?.closed) spans.push([mkCt(day, '17:00', -1), mkCt(day, '17:00')]);
      else if (at(h?.halt) || at(h?.close)) spans.push([mkCt(day, at(h.halt) || at(h.close)), mkCt(day, '17:00')]);
    }
    spans.sort((a, b) => a[0] - b[0]);
    const before = [0];
    for (const [s, e] of spans) before.push(before[before.length - 1] + mkWeek(e) - mkWeek(s));
    byGroup.set(group, (c = { starts: spans.map((x) => x[0]), ends: spans.map((x) => x[1]), before, cuts: new Map() }));
  }
  return c;
}
// per: a seconds, minute or hour chart's bar length; a bar a closure cuts short counts whole, as that chart gives it a
// whole bar's room (the time runs on by the clock into the closure until the bar would have ended).
function marketMs(ms, holidays = null, group = 'equity', per = 0) {
  let m = mkWeek(ms);
  if (!holidays) return m;
  const c = mkClosures(holidays, group), { starts, ends, before } = c;
  let i = 0, hi = starts.length;
  while (i < hi) { const mid = (i + hi) >> 1; if (starts[mid] <= ms) i = mid + 1; else hi = mid; }   // closures begun by ms
  if (i) {
    m -= before[i];
    const inside = ms < ends[i - 1];
    if (inside) m += mkWeek(ends[i - 1]) - mkWeek(ms);   // inside one: only what it has shut so far
    if (per) { const { cut, before: cutBefore } = mkCuts(c, per); m += cutBefore[i - 1] + (inside ? Math.min(cut[i - 1], ms - starts[i - 1]) : cut[i - 1]); }
  }
  return m;
}
// The first ms at which the market has traded m (marketMs's inverse), searched from `near`.
function marketAt(m, holidays, group, near, per = 0) {
  let lo = Math.floor(near), hi = lo;
  for (let k = 0; k < 2000 && marketMs(lo, holidays, group, per) >= m; k++) lo -= MK_WEEK;
  for (let k = 0; k < 2000 && marketMs(hi, holidays, group, per) < m; k++) hi += MK_WEEK;
  while (hi - lo > 1) { const mid = Math.floor((lo + hi) / 2); if (marketMs(mid, holidays, group, per) >= m) hi = mid; else lo = mid; }
  return hi;
}

// Where an nM chart puts ms, in bars (chartsrv/drawings.py month_bars): the index of the bar holding it, in groups of n
// months from January of year 0 (bars.calendar_bucket), and how far into that bar, as timeToLogical goes (at n x 31
// days a bar, so at most 1).
function monthBars(ms, n) {
  const day = new Date(mkWall(ms) + 7 * MK_HOUR), g = Math.floor((day.getUTCFullYear() * 12 + day.getUTCMonth()) / n);   // a 17:00 open trades the next day
  const first = `${Math.floor((g * n) / 12)}-${String(((g * n) % 12) + 1).padStart(2, '0')}-01`;
  return g + Math.min(1, (ms - mkCt(first, '17:00', -1)) / (n * 31 * MK_DAY));
}

// A drawing alert's level at time ts (chartsrv/drawings.py line_price), or null where the line is not drawn: straight
// in market time, as the chart draws it straight in bars, or as the chart it was switched on from lays it out (its
// months, clock, per, log: lineLayout). holidays, group: hello's holidays and the symbol's group.
function drawingLinePrice(a, ts, holidays = null, group = 'equity') {
  if (a.t2 === a.t1) return null;
  const [x, x1, x2] = [ts, a.t1, a.t2].map((t) => (a.clock ? t : a.months ? monthBars(t, a.months) : marketMs(t, holidays, group, a.per)));
  if ((x < x1 && !a.left) || (x > x2 && !a.right)) return null;
  if (a.p1 === a.p2) return a.p1;   // a level line or ray: its price wherever it is drawn
  if (x2 === x1) return null;   // both ends in one closure: upright
  const k = (x - x1) / (x2 - x1);
  return a.log && a.p1 > 0 && a.p2 > 0 ? a.p1 * (a.p2 / a.p1) ** k : a.p1 + (a.p2 - a.p1) * k;
}

// Distance from (px, py) to the segment a-b.
function segDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy;
  const k = l2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / l2)) : 0;
  return Math.hypot(px - (ax + k * dx), py - (ay + k * dy));
}

/* env: chart, series, el (the chart element), bars() -> bars, periodMs() -> ms or 0 for untimed bars, tick(),
   fmt(price) -> text, symbol(), api(method, path, body), toast(text), alerts() -> alert list, ui {bar, edit, paste}. */
function createDrawings(env) {
  const D = {
    list: new Map(), selected: null, tool: 'select', creating: null, drag: null, hover: null, boxes: new Map(),
    hidden: false, magnet: false, undo: [], redo: [], loadedFor: null, measure: null, full: false,
  };
  try { D.hidden = localStorage.getItem('drawHidden') === '1'; D.magnet = localStorage.getItem('drawMagnet') === '1'; } catch { /* defaults */ }
  const ts = () => env.chart.timeScale();
  // A drawing is on every tab of its symbol, unless it was made for one tab only (style.tab).
  const onTab = (d) => !d.style.tab || d.style.tab === env.tab?.();
  const shown = (d) => !d.style.hidden && onTab(d);
  // A new drawing's tab: this one when the tab keeps its new drawings to itself.
  const stamp = (style) => { const s = { ...style }; delete s.tab; const id = env.tabLocal?.() && env.tab?.(); if (id) s.tab = id; return s; };

  // ---- time <-> x ------------------------------------------------------------------------------------
  // Range, Renko and volume bars can share a time to the ms (one big trade opens several): a point on one of them
  // says which in hundredths of a ms past that time (chartsrv/drawings.py keeps them).
  const dupStart = (bars, i) => { let j = i; while (j > 0 && bars[j - 1].t === bars[i].t) j--; return j; };
  // Past the bars it holds, a seconds, minute or hour chart of a future here whose bars fit the hour evenly
  // (env.market: hello's holidays and its group) goes on in market time, a bar a holiday closure cuts short counted
  // whole: a point a week back on a 1m chart, or right of the last bar across the break, sits where the bars will put
  // it. Otherwise at the bar length, by the clock.
  const market = (per) => { const mk = per && MK_HOUR % per === 0 && env.market?.(); return mk ? { ...mk, per } : null; };
  const gap = (mk, a, b) => (mk ? marketMs(b, mk.holidays, mk.group, mk.per) - marketMs(a, mk.holidays, mk.group, mk.per) : b - a);
  const past = (mk, t, x) => (mk ? marketAt(marketMs(t, mk.holidays, mk.group, mk.per) + x, mk.holidays, mk.group, t + x, mk.per) : Math.round(t + x));
  function timeToLogical(ms) {
    const bars = env.bars(), n = bars.length;
    if (!n) return null;
    const per = env.periodMs(), mk = market(per);
    const avg = per || (n > 1 ? (bars[n - 1].t - bars[Math.max(0, n - 51)].t) / Math.min(50, n - 1) : 0) || 60_000;
    if (ms <= bars[0].t) return gap(mk, bars[0].t, ms) / avg;
    if (per ? ms >= bars[n - 1].t : Math.floor(ms) > bars[n - 1].t) return n - 1 + gap(mk, bars[n - 1].t, ms) / avg;
    if (per) { const i = barIndexTo(bars, ms); return i + Math.min(1, (ms - bars[i].t) / per); }
    const base = Math.floor(ms), i = barIndexTo(bars, base);
    if (base > bars[i].t) return i + (base - bars[i].t) / (bars[i + 1].t - bars[i].t);
    const j = dupStart(bars, i), x = (ms - base) * 100, k = Math.min(Math.floor(x + 0.05), i - j);
    return j + k + (j + k < i ? Math.min(1, Math.max(0, x - k)) : 0);
  }
  function logicalToTime(l) {
    const bars = env.bars(), n = bars.length;
    if (!n) return null;
    const per = env.periodMs(), mk = market(per);
    const avg = per || (n > 1 ? (bars[n - 1].t - bars[Math.max(0, n - 51)].t) / Math.min(50, n - 1) : 0) || 60_000;
    if (l <= 0) return past(mk, bars[0].t, l * avg);
    if (l >= n - 1) {
      const x = (l - (n - 1)) * avg;
      return per || x >= 1 ? past(mk, bars[n - 1].t, x) : bars[n - 1].t + Math.min(n - 1 - dupStart(bars, n - 1), 99) / 100;
    }
    const i = Math.floor(l), f = l - i;
    if (per) return Math.round(bars[i].t + f * per);
    const k = Math.min(i - dupStart(bars, i), 99);
    if (bars[i + 1].t === bars[i].t) return bars[i].t + (k + f) / 100;
    const dt = bars[i + 1].t - bars[i].t, m = Math.round(f * dt);
    return m < dt ? bars[i].t + k / 100 + m : bars[i + 1].t;   // rounded up to the next bar's ms: that bar, not the k-th one there
  }
  const X = (t) => { const l = timeToLogical(t); return l == null ? null : logicalX(ts(), l); };
  const Y = (p) => env.series.priceToCoordinate(p);

  // The chart point under (x, y): tick-rounded, or snapped to the bar's open/high/low/close with the magnet.
  function pointAt(x, y, snap = true) {
    const l = ts().coordinateToLogical(x), p = env.series.coordinateToPrice(y);
    if (l == null || p == null) return null;
    const tick = env.tick(), bars = env.bars();
    if (snap && D.magnet) {
      const i = Math.round(l), b = bars[i];
      if (b) {
        let best = null;
        for (const v of [b.o, b.h, b.l, b.c]) {
          const dy = Math.abs(Y(v) - y);
          if (dy < 30 && (!best || dy < best.dy)) best = { v, dy };
        }
        if (best) return { t: env.periodMs() ? b.t : logicalToTime(i), p: best.v };   // of bars sharing a ms, this one
      }
    }
    return { t: logicalToTime(l), p: Math.round(p / tick) * tick };
  }

  // ---- drawing ---------------------------------------------------------------------------------------
  function lineEnds(d, W) {   // trend line end points on screen, extended to the pane edges if asked
    let [a, b] = d.pts.map((q) => ({ x: X(q.t), y: Y(q.p) }));
    if (a.x == null || b.x == null || a.y == null || b.y == null) return null;
    if (b.x < a.x) [a, b] = [b, a];
    const dx = b.x - a.x, dy = b.y - a.y;
    if (dx !== 0) {
      if (d.style.extendLeft) a = { x: 0, y: a.y - (a.x / dx) * dy };
      if (d.style.extendRight) b = { x: W, y: b.y + ((W - b.x) / dx) * dy };
    }
    return [a, b];
  }

  // ---- measure: Shift+drag (or the ruler tool, drag or tap-tap) shows the move between two points ------------
  function measurePoint(x, y) {
    const l = ts().coordinateToLogical(x), pt = pointAt(x, y);
    return l == null || !pt ? null : { l: Math.round(l), p: pt.p };
  }
  const span = (ms) => {
    const m = Math.round(ms / 60000), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
    return ms < 60000 ? `${Math.round(ms / 1000)}s` : d ? `${d}d ${h}h` : h ? `${h}h ${mm}m` : `${mm}m`;
  };
  const kfmt = (v) => (v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : v >= 1e4 ? `${(v / 1e3).toFixed(1)}K` : String(v));
  function measureLines(m) {
    const bars = env.bars(), tick = env.tick(), dp = m.b.p - m.a.p, sg = dp > 0 ? '+' : dp < 0 ? '−' : '';
    const n = Math.abs(m.b.l - m.a.l), lo = Math.max(0, Math.min(m.a.l, m.b.l)), hi = Math.min(bars.length - 1, Math.max(m.a.l, m.b.l));
    let vol = 0;
    for (let i = lo; i <= hi; i++) vol += bars[i].v || 0;
    const t0 = logicalToTime(m.a.l), t1 = logicalToTime(m.b.l), pv = env.pointValue ? env.pointValue() : 0;
    const money = pv ? `  ${sg}$${(Math.abs(dp) * pv).toLocaleString('en-US', { maximumFractionDigits: 2 })}` : '';
    return [`${sg}${env.fmt(Math.abs(dp))} (${sg}${Math.round(Math.abs(dp) / tick)} ticks)${money}`,
            `${sg}${(Math.abs(dp) / Math.abs(m.a.p || 1) * 100).toFixed(2)}%  ·  ${n} bar${n === 1 ? '' : 's'}${t0 != null && t1 != null && t0 !== t1 ? `, ${span(Math.abs(t1 - t0))}` : ''}`,
            `Vol ${kfmt(vol)}`];
  }
  function drawMeasure(ctx, m, W, H) {
    const xa = logicalX(ts(), m.a.l), xb = logicalX(ts(), m.b.l), ya = Y(m.a.p), yb = Y(m.b.p);
    if (xa == null || xb == null || ya == null || yb == null) return;
    const up = m.b.p >= m.a.p, color = up ? '#2962ff' : '#f23645';
    ctx.fillStyle = rgbaHex(color, 0.15);
    ctx.fillRect(Math.min(xa, xb), Math.min(ya, yb), Math.abs(xb - xa), Math.abs(yb - ya));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    const xm = (xa + xb) / 2, ym = (ya + yb) / 2;
    ctx.beginPath();
    ctx.moveTo(xm, ya); ctx.lineTo(xm, yb);   // the price leg, arrow at the end point
    ctx.moveTo(xa, ym); ctx.lineTo(xb, ym);   // the time leg
    const ah = yb > ya ? -6 : 6, aw = xb > xa ? -6 : 6;
    ctx.moveTo(xm - 4, yb + ah); ctx.lineTo(xm, yb); ctx.lineTo(xm + 4, yb + ah);
    ctx.moveTo(xb + aw, ym - 4); ctx.lineTo(xb, ym); ctx.lineTo(xb + aw, ym + 4);
    ctx.stroke();
    const lines = measureLines(m);
    ctx.font = '12px system-ui, sans-serif';
    const w = Math.max(...lines.map((t) => ctx.measureText(t).width)) + 16, h = lines.length * 16 + 8;
    let bx = Math.min(Math.max(4, xm - w / 2), W - w - 4), by = Math.max(ya, yb) + 8;
    if (by + h > H - 4) by = Math.min(ya, yb) - h - 8;
    by = Math.max(4, by);
    ctx.fillStyle = color;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(bx, by, w, h, 4); else ctx.rect(bx, by, w, h);
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.textBaseline = 'top';
    lines.forEach((t, i) => ctx.fillText(t, bx + 8, by + 6 + i * 16));
  }

  function render(ctx, size) {
    D.boxes.clear();
    if (D.hidden && !D.measure) return;   // hidden: the drawings are, the measure (a ruler, not a drawing) still shows
    const W = size.width, H = size.height;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, W, H);
    ctx.clip();
    const items = D.hidden ? [] : [...D.list.values()].filter(shown);   // hidden in the object tree, or another tab's
    if (D.creating && !D.hidden) items.push(D.creating);
    const alerts = env.alerts();
    for (const d of items) {
      try { drawOne(ctx, d, W, H, alerts); } catch (e) { console.warn('drawing', d.id, e); }
    }
    if (D.measure) { try { drawMeasure(ctx, D.measure, W, H); } catch (e) { console.warn('measure', e); } }
    const sel = D.hidden ? null : D.list.get(D.selected) || D.creating;
    if (sel) handles(sel, W).forEach(([x, y]) => {
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fillStyle = '#0d1117';
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = sel.style.color || '#2962ff';
      ctx.stroke();
    });
    ctx.restore();
  }

  function label(ctx, text, x, y, color, align = 'left', baseline = 'bottom') {
    if (!text) return;
    ctx.font = '12px system-ui, -apple-system, Segoe UI, sans-serif';
    ctx.textAlign = align;
    ctx.textBaseline = baseline;
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
  }

  function stroke(ctx, s, alpha = 1) {
    ctx.strokeStyle = alpha === 1 ? s.color : rgbaHex(s.color, alpha);
    ctx.lineWidth = s.width || 1;
    ctx.setLineDash((DASH[s.dash] || []).map((v) => v * Math.max(1, (s.width || 1) * 0.75)));
  }

  function drawOne(ctx, d, W, H, alerts) {
    const s = d.style;
    if (d.type === 'hline' || d.type === 'hray') {
      const y = Y(d.pts[0].p);
      if (y == null || y < -2 || y > H + 2) return;
      const x0 = d.type === 'hray' ? Math.max(0, X(d.pts[0].t) ?? 0) : 0;
      if (x0 >= W) return;
      stroke(ctx, s);
      ctx.beginPath();
      ctx.moveTo(x0, y);
      ctx.lineTo(W, y);
      ctx.stroke();
      ctx.setLineDash([]);
      const bell = d.alert?.on ? alertMark(d, alerts) : '';
      label(ctx, [s.text, bell].filter(Boolean).join(' '), W - 6, y - 3, s.color, 'right');
    } else if (d.type === 'trend') {
      const e = lineEnds(d, W);
      if (!e) return;
      stroke(ctx, s);
      ctx.beginPath();
      ctx.moveTo(e[0].x, e[0].y);
      ctx.lineTo(e[1].x, e[1].y);
      ctx.stroke();
      ctx.setLineDash([]);
      const a = { x: X(d.pts[0].t), y: Y(d.pts[0].p) }, b = { x: X(d.pts[1].t), y: Y(d.pts[1].p) };
      const bell = d.alert?.on ? alertMark(d, alerts) : '';
      label(ctx, [s.text, bell].filter(Boolean).join(' '), (a.x + b.x) / 2, Math.min(a.y, b.y) - 4, s.color, 'center');
    } else if (d.type === 'rect') {
      const xs = d.pts.map((q) => X(q.t)), ys = d.pts.map((q) => Y(q.p));
      if (xs.includes(null) || ys.includes(null)) return;
      const x1 = s.extendLeft ? 0 : Math.min(...xs), x2 = s.extendRight ? W : Math.max(...xs);
      const y1 = Math.min(...ys), y2 = Math.max(...ys);
      if (s.fillOpacity) { ctx.fillStyle = rgbaHex(s.fill || s.color, s.fillOpacity / 100); ctx.fillRect(x1, y1, x2 - x1, y2 - y1); }
      stroke(ctx, s);
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      if (s.mid) {
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(x1, (y1 + y2) / 2);
        ctx.lineTo(x2, (y1 + y2) / 2);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      label(ctx, s.text, x1 + 5, y1 + 4, s.color, 'left', 'top');
    } else if (d.type === 'fib') {
      const xa = X(d.pts[0].t), xb = X(d.pts[1].t);
      if (xa == null || xb == null) return;
      const x1 = s.extendLeft ? 0 : Math.min(xa, xb), x2 = s.extendRight ? W : Math.max(xa, xb);
      const levels = [...(s.levels || FIB_LEVELS)].sort((a, b) => a - b);
      const color = (lv) => FIB_COLORS[lv] || s.color;
      if (s.fillOpacity) {
        for (let i = 0; i + 1 < levels.length; i++) {
          const ya = Y(fibPrice(d, levels[i])), yb = Y(fibPrice(d, levels[i + 1]));
          if (ya == null || yb == null) continue;
          ctx.fillStyle = rgbaHex(color(levels[i + 1]), s.fillOpacity / 100);
          ctx.fillRect(x1, Math.min(ya, yb), x2 - x1, Math.abs(yb - ya));
        }
      }
      for (const lv of levels) {
        const price = fibPrice(d, lv), y = Y(price);
        if (y == null || y < -2 || y > H + 2) continue;
        stroke(ctx, { ...s, color: color(lv) });
        ctx.beginPath();
        ctx.moveTo(x1, y);
        ctx.lineTo(x2, y);
        ctx.stroke();
        label(ctx, `${lv} (${env.fmt(price)})`, x1 + 4, y - 2, color(lv));
      }
      ctx.setLineDash([3, 4]);   // the swing the levels are measured on
      ctx.strokeStyle = rgbaHex(s.color, 0.8);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xa, Y(d.pts[0].p));
      ctx.lineTo(xb, Y(d.pts[1].p));
      ctx.stroke();
      ctx.setLineDash([]);
    } else if (POSITION[d.type]) {
      drawPosition(ctx, d, W);
    } else if (d.type === 'vline') {
      const x = X(d.pts[0].t);
      if (x == null || x < -2 || x > W + 2) return;
      stroke(ctx, s);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
      ctx.setLineDash([]);
      const when = calSpec() ? fmtDay.format(d.pts[0].t + 7 * 3600e3) : ax.md.format(d.pts[0].t);   // a D/W/M bar's trade date, else the chosen zone (app.js)
      label(ctx, [s.text, when].filter(Boolean).join('  '), x + 4, H - 6, s.color);
    } else if (d.type === 'arrow') {
      const a = { x: X(d.pts[0].t), y: Y(d.pts[0].p) }, b = { x: X(d.pts[1].t), y: Y(d.pts[1].p) };
      if ([a.x, a.y, b.x, b.y].includes(null)) return;
      stroke(ctx, s);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      ctx.setLineDash([]);
      const ang = Math.atan2(b.y - a.y, b.x - a.x), L = 8 + (s.width || 1) * 2;
      ctx.fillStyle = s.color;
      ctx.beginPath();
      ctx.moveTo(b.x, b.y);
      ctx.lineTo(b.x - L * Math.cos(ang - 0.45), b.y - L * Math.sin(ang - 0.45));
      ctx.lineTo(b.x - L * Math.cos(ang + 0.45), b.y - L * Math.sin(ang + 0.45));
      ctx.closePath();
      ctx.fill();
      label(ctx, s.text, a.x, a.y + (a.y <= b.y ? -4 : 14), s.color, 'center');
    } else if (d.type === 'channel') {
      const c = channelLines(d, W);
      if (!c) return;
      const [l1, l2] = c;
      if (s.fillOpacity) {
        ctx.fillStyle = rgbaHex(s.color, s.fillOpacity / 100);
        ctx.beginPath(); ctx.moveTo(l1[0].x, l1[0].y); ctx.lineTo(l1[1].x, l1[1].y); ctx.lineTo(l2[1].x, l2[1].y); ctx.lineTo(l2[0].x, l2[0].y); ctx.closePath(); ctx.fill();
      }
      stroke(ctx, s);
      for (const l of [l1, l2]) { ctx.beginPath(); ctx.moveTo(l[0].x, l[0].y); ctx.lineTo(l[1].x, l[1].y); ctx.stroke(); }
      if (s.mid) {
        ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(l1[0].x, (l1[0].y + l2[0].y) / 2); ctx.lineTo(l1[1].x, (l1[1].y + l2[1].y) / 2); ctx.stroke();
      }
      ctx.setLineDash([]);
    } else if (d.type === 'fibext') {
      const [A, B, Cp] = d.pts, xc = X(Cp.t), xs = d.pts.map((q) => X(q.t));
      if (xs.includes(null)) return;
      const x1 = xc, x2 = s.extendRight ? W : Math.max(...xs) + Math.max(40, Math.abs(xs[1] - xs[0]));
      const levels = [...(s.levels || FIB_EXT_LEVELS)].sort((a, b) => a - b);
      const price = (lv) => Cp.p + (B.p - A.p) * lv, color = (lv) => FIB_COLORS[lv] || s.color;
      if (s.fillOpacity) {
        for (let i = 0; i + 1 < levels.length; i++) {
          const ya = Y(price(levels[i])), yb = Y(price(levels[i + 1]));
          if (ya == null || yb == null) continue;
          ctx.fillStyle = rgbaHex(color(levels[i + 1]), s.fillOpacity / 100);
          ctx.fillRect(x1, Math.min(ya, yb), x2 - x1, Math.abs(yb - ya));
        }
      }
      for (const lv of levels) {
        const y = Y(price(lv));
        if (y == null || y < -2 || y > H + 2) continue;
        stroke(ctx, { ...s, color: color(lv) });
        ctx.beginPath(); ctx.moveTo(x1, y); ctx.lineTo(x2, y); ctx.stroke();
        label(ctx, `${lv} (${env.fmt(price(lv))})`, x1 + 4, y - 2, color(lv));
      }
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = rgbaHex(s.color, 0.8);
      ctx.lineWidth = 1;
      ctx.beginPath();
      d.pts.forEach((q, i) => { const x = X(q.t), y = Y(q.p); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
      ctx.stroke();
      ctx.setLineDash([]);
    } else if (d.type === 'prange' || d.type === 'drange') {
      drawRange(ctx, d, W);
    } else if (d.type === 'text') {
      const x = X(d.pts[0].t), y = Y(d.pts[0].p);
      if (x == null || y == null) return;
      const fs = s.fontSize || 14, lines = (s.text || '').split('\n');
      ctx.font = `${fs}px system-ui, -apple-system, Segoe UI, sans-serif`;
      const w = Math.max(...lines.map((l) => ctx.measureText(l).width)), lh = fs * 1.25, h = lh * lines.length;
      if (s.background) { ctx.fillStyle = 'rgba(13, 17, 23, 0.85)'; ctx.fillRect(x - 4, y - 3, w + 8, h + 6); }
      ctx.fillStyle = s.color;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      lines.forEach((l, i) => ctx.fillText(l, x, y + i * lh));
      D.boxes.set(d.id, { x: x - 4, y: y - 3, w: w + 8, h: h + 6 });
    }
  }

  // Long / short position: entry over the time span, the target zone green and the stop zone red, risk to reward and
  // dollars per the quantity; from the entry on, the first bar to reach the target or the stop decides it.
  function positionOutcome(d) {
    const [e, tg, st] = d.pts, long = d.type === 'long', bars = env.bars();
    let i = barIndexFrom(bars, e.t);
    if (i < 0) return null;
    for (; i < bars.length && bars[i].t <= tg.t; i++) {
      const b = bars[i];
      const stop = long ? b.l <= st.p : b.h >= st.p, target = long ? b.h >= tg.p : b.l <= tg.p;
      if (stop) return { t: b.t, p: st.p, hit: 'Stop' };   // both in one bar: the stop, to be safe
      if (target) return { t: b.t, p: tg.p, hit: 'Target' };
    }
    const last = bars[Math.min(i, bars.length) - 1];
    return last && last.t >= e.t ? { t: last.t, p: last.c, hit: '' } : null;
  }
  function money(points, qty) {
    const pv = env.pointValue ? env.pointValue() : 0;
    return pv ? `${points < 0 ? '−' : ''}$${Math.abs(points * pv * qty).toLocaleString('en-US', { maximumFractionDigits: 2 })}` : '';
  }
  function drawPosition(ctx, d, W) {
    if (d.pts.length < 3) d = { ...d, pts: positionFrom(d.type, d.pts[0]) };   // being placed: one point so far
    const [e, tg, st] = d.pts, x0 = X(e.t), x1 = X(tg.t), ye = Y(e.p), yt = Y(tg.p), ys = Y(st.p);
    if ([x0, x1, ye, yt, ys].includes(null)) return;
    const xa = Math.min(x0, x1), xb = Math.max(x0, x1), tick = env.tick(), qty = d.style.qty || 1;
    ctx.fillStyle = 'rgba(8, 153, 129, 0.22)';
    ctx.fillRect(xa, Math.min(ye, yt), xb - xa, Math.abs(yt - ye));
    ctx.fillStyle = 'rgba(242, 54, 69, 0.22)';
    ctx.fillRect(xa, Math.min(ye, ys), xb - xa, Math.abs(ys - ye));
    ctx.setLineDash([]);
    ctx.lineWidth = 1;
    ctx.strokeStyle = '#787b86';
    ctx.beginPath(); ctx.moveTo(xa, ye); ctx.lineTo(xb, ye); ctx.stroke();
    const reward = Math.abs(tg.p - e.p), risk = Math.abs(e.p - st.p), rr = risk ? reward / risk : 0;
    const out = positionOutcome(d);
    if (out) {   // the path to the exit, and the result
      const xo = X(out.t), yo = Y(out.p);
      if (xo != null && yo != null) {
        ctx.strokeStyle = out.hit === 'Stop' ? '#f23645' : out.hit === 'Target' ? '#089981' : '#d1d4dc';
        ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.moveTo(x0, ye); ctx.lineTo(xo, yo); ctx.stroke();
        ctx.setLineDash([]);
      }
    }
    const pnl = out ? (d.type === 'long' ? out.p - e.p : e.p - out.p) : 0;
    const tag = (text, y, bg, below) => {
      ctx.font = '12px system-ui, -apple-system, Segoe UI, sans-serif';
      const w = ctx.measureText(text).width + 12, h = 18, cx = Math.max(xa + w / 2 + 2, Math.min((xa + xb) / 2, W - w / 2 - 2));
      const top = below ? y + 4 : y - h - 4;
      ctx.fillStyle = bg;
      ctx.fillRect(cx - w / 2, top, w, h);
      ctx.fillStyle = '#ffffff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(text, cx, top + h / 2);
    };
    const tUp = yt < ys;
    tag(`Target ${env.fmt(reward)} (${Math.round(reward / tick)} ticks) ${money(reward, qty)}`, yt, '#089981', !tUp);
    tag(`Stop ${env.fmt(risk)} (${Math.round(risk / tick)} ticks) ${money(-risk, qty)}`, ys, '#f23645', tUp);
    const mid = [`${d.type === 'long' ? 'Long' : 'Short'} ${qty}`, `R:R ${rr.toFixed(2)}`,
      out ? `${out.hit ? `${out.hit} hit` : 'Open'} ${money(pnl, qty) || env.fmt(pnl)}` : '', d.style.text].filter(Boolean).join(' · ');
    tag(mid, ye, out?.hit === 'Stop' ? '#b22833' : out?.hit === 'Target' ? '#067a68' : '#3a4446', yt > ye);
  }
  // Parallel channel: the line through the first two points and its copy through the third.
  function channelLines(d, W) {
    const base = lineEnds({ ...d, pts: d.pts.slice(0, 2) }, W);
    const [a, b, c] = d.pts.map((q) => ({ x: X(q.t), y: Y(q.p) }));
    if (!base || c.x == null || c.y == null) return null;
    const slope = b.x !== a.x ? (b.y - a.y) / (b.x - a.x) : 0, off = c.y - (a.y + slope * (c.x - a.x));
    return [base, base.map((q) => ({ x: q.x, y: q.y + off }))];
  }
  function drawRange(ctx, d, W) {
    const [a, b] = d.pts, xa = X(a.t), xb = X(b.t), ya = Y(a.p), yb = Y(b.p);
    if ([xa, xb, ya, yb].includes(null)) return;
    const color = d.style.color, x1 = Math.min(xa, xb), x2 = Math.max(xa, xb), y1 = Math.min(ya, yb), y2 = Math.max(ya, yb);
    ctx.fillStyle = rgbaHex(color, 0.15);
    ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    ctx.beginPath();
    const xm = (x1 + x2) / 2, ym = (y1 + y2) / 2;
    if (d.type === 'prange') {
      ctx.moveTo(x1, ya); ctx.lineTo(x2, ya); ctx.moveTo(x1, yb); ctx.lineTo(x2, yb);
      ctx.moveTo(xm, ya); ctx.lineTo(xm, yb);
      const ah = yb > ya ? -6 : 6;
      ctx.moveTo(xm - 4, yb + ah); ctx.lineTo(xm, yb); ctx.lineTo(xm + 4, yb + ah);
    } else {
      ctx.moveTo(xa, y1); ctx.lineTo(xa, y2); ctx.moveTo(xb, y1); ctx.lineTo(xb, y2);
      ctx.moveTo(xa, ym); ctx.lineTo(xb, ym);
      const aw = xb > xa ? -6 : 6;
      ctx.moveTo(xb + aw, ym - 4); ctx.lineTo(xb, ym); ctx.lineTo(xb + aw, ym + 4);
    }
    ctx.stroke();
    const m = { a: { l: Math.round(timeToLogical(a.t)), p: a.p }, b: { l: Math.round(timeToLogical(b.t)), p: b.p } };
    const all = measureLines(m), lines = d.type === 'prange' ? [all[0], all[1].split('  ·  ')[0]] : [all[1].split('  ·  ')[1] || all[1], all[2]];
    if (d.style.text) lines.unshift(d.style.text);
    ctx.font = '12px system-ui, sans-serif';
    const w = Math.max(...lines.map((t) => ctx.measureText(t).width)) + 16, h = lines.length * 16 + 8;
    const bx = Math.min(Math.max(4, xm - w / 2), W - w - 4), by = y2 + 6;
    ctx.fillStyle = color;
    ctx.fillRect(bx, by, w, h);
    ctx.fillStyle = '#ffffff';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    lines.forEach((t, i) => ctx.fillText(t, bx + 8, by + 6 + i * 16));
    D.boxes.set(d.id, { x: x1, y: y1, w: x2 - x1, h: y2 - y1 });
  }

  // How this chart lays a trend line out, kept with the line's alert when it is switched on here, so the server fires
  // where the line is drawn here (chartsrv/drawings.py LAYOUT): an nM chart's bars are calendar months or groups of n
  // (env.months), W bars run on the clock (env.byClock), a chart that goes on in market time has bars of its length
  // (one a holiday closure cuts short counts whole), and a logarithmic price scale draws the line straight in log
  // price. Other charts: market time, as the minute charts.
  function lineLayout(d) {
    const per = env.periodMs(), o = {};
    if (d.type !== 'trend') return o;
    if (env.months?.()) o.months = env.months();
    else if (env.byClock?.()) o.clock = true;
    else if (market(per)) o.per = per;
    if (env.logScale?.()) o.log = true;
    return o;
  }
  function alertMark(d, alerts) {
    const a = alerts.find((x) => x.kind === 'line' && x.params?.drawing === d.id);
    // switched on over the other price scale: the alert keeps to the line as that one draws it, not as this one does
    const [p, q] = d.type === 'trend' ? d.pts.map((x) => x.p) : [0, 0];
    const scale = p === q || p <= 0 || q <= 0 || !!d.alert.log === !!env.logScale?.() ? ''
      : d.alert.log ? ' (log scale)' : ' (regular scale)';
    return (!a ? '⏰?' : a.enabled ? '⏰' : '⏰ fired') + scale;
  }

  function handles(d, W) {
    if (d.type === 'hline') {
      const x = X(d.pts[0].t), y = Y(d.pts[0].p);
      return y == null ? [] : [[Math.max(24, Math.min(W - 24, x ?? W / 2)), y]];
    }
    return d.pts.map((q) => [X(q.t), Y(q.p)]).filter(([x, y]) => x != null && y != null);
  }

  // ---- hit testing --------------------------------------------------------------------------------------
  function hitTest(x, y, tol) {
    if (D.hidden) return null;   // hidden drawings can't be picked up (and moved) by a pan that starts on one
    const W = env.el.clientWidth - (env.chart.priceScale('right').width() || 0);
    const sel = D.list.get(D.selected);
    if (sel) {
      const hs = handles(sel, W);
      for (let i = 0; i < hs.length; i++) if (Math.hypot(hs[i][0] - x, hs[i][1] - y) <= tol + 3) return { id: sel.id, part: sel.type === 'hline' ? 'body' : `p${i}` };
    }
    const items = [...D.list.values()].reverse();
    for (const d of items) if (shown(d) && near(d, x, y, tol, W)) return { id: d.id, part: 'body' };
    return null;
  }

  function near(d, x, y, tol, W) {
    const s = d.style;
    if (d.type === 'hline' || d.type === 'hray') {
      const ly = Y(d.pts[0].p);
      if (ly == null || Math.abs(ly - y) > tol) return false;
      return d.type === 'hline' || x >= (X(d.pts[0].t) ?? 0) - tol;
    }
    if (d.type === 'trend') {
      const e = lineEnds(d, W);
      return !!e && segDist(x, y, e[0].x, e[0].y, e[1].x, e[1].y) <= tol;
    }
    if (d.type === 'rect' || d.type === 'fib') {
      const xs = d.pts.map((q) => X(q.t));
      if (xs.includes(null)) return false;
      const x1 = s.extendLeft ? 0 : Math.min(...xs), x2 = s.extendRight ? W : Math.max(...xs);
      if (x < x1 - tol || x > x2 + tol) return false;
      if (d.type === 'rect') {
        const ys = d.pts.map((q) => Y(q.p)), y1 = Math.min(...ys), y2 = Math.max(...ys);
        if (y < y1 - tol || y > y2 + tol) return false;
        const inside = x > x1 + tol && x < x2 - tol && y > y1 + tol && y < y2 - tol;
        return !inside || s.fillOpacity > 0;
      }
      return (s.levels || FIB_LEVELS).some((lv) => Math.abs((Y(fibPrice(d, lv)) ?? -1e9) - y) <= tol);
    }
    if (d.type === 'text' || d.type === 'prange' || d.type === 'drange') {
      const b = D.boxes.get(d.id);
      return !!b && x >= b.x - tol && x <= b.x + b.w + tol && y >= b.y - tol && y <= b.y + b.h + tol;
    }
    if (d.type === 'vline') { const lx = X(d.pts[0].t); return lx != null && Math.abs(lx - x) <= tol; }
    if (d.type === 'arrow') {
      const a = d.pts.map((q) => [X(q.t), Y(q.p)]);
      return !a.flat().includes(null) && segDist(x, y, a[0][0], a[0][1], a[1][0], a[1][1]) <= tol;
    }
    if (POSITION[d.type]) {
      const xs = [X(d.pts[0].t), X(d.pts[1].t)], ys = d.pts.map((q) => Y(q.p));
      if (xs.includes(null) || ys.includes(null)) return false;
      return x >= Math.min(...xs) - tol && x <= Math.max(...xs) + tol && y >= Math.min(...ys) - tol && y <= Math.max(...ys) + tol;
    }
    if (d.type === 'channel') {
      const c = channelLines(d, W);
      if (!c) return false;
      if (c.some((l) => segDist(x, y, l[0].x, l[0].y, l[1].x, l[1].y) <= tol)) return true;
      if (!s.fillOpacity || x < Math.min(c[0][0].x, c[0][1].x) || x > Math.max(c[0][0].x, c[0][1].x)) return false;
      const k = (x - c[0][0].x) / ((c[0][1].x - c[0][0].x) || 1), y1 = c[0][0].y + k * (c[0][1].y - c[0][0].y), y2 = c[1][0].y + k * (c[1][1].y - c[1][0].y);
      return y >= Math.min(y1, y2) && y <= Math.max(y1, y2);
    }
    if (d.type === 'fibext') {
      const [A, B, Cp] = d.pts, xs = d.pts.map((q) => X(q.t));
      if (xs.includes(null)) return false;
      const x2 = s.extendRight ? W : Math.max(...xs) + Math.max(40, Math.abs(xs[1] - xs[0]));
      if (x < xs[2] - tol || x > x2 + tol) return false;
      return (s.levels || FIB_EXT_LEVELS).some((lv) => Math.abs((Y(Cp.p + (B.p - A.p) * lv) ?? -1e9) - y) <= tol);
    }
    return false;
  }

  // ---- saving, undo, server sync -----------------------------------------------------------------------
  const path = (id, sym = env.symbol()) => `/api/drawings/${encodeURIComponent(sym)}${id ? `/${id}` : ''}`;
  async function save(d, before, sym = env.symbol()) {   // sym: a typed change saved as another symbol opens
    D.list.set(d.id, d);
    if (D.selected === d.id && !shown(d)) select(null);   // just hidden (the object tree's eye): no longer selected
    prim.update();
    if (before !== undefined) pushUndo(before, d, sym);
    try {
      const saved = await env.api('PUT', path(d.id, sym), { type: d.type, pts: d.pts, style: d.style, alert: d.alert || { on: false } });
      if (D.list.get(d.id) === d && D.drag?.id !== d.id) D.list.set(d.id, saved);   // not over a newer edit or a drag
    } catch (e) { env.toast(`Drawing not saved: ${e.message}`); }
    prim.update();
    renderEdit();
  }
  async function remove(id, record = true) {
    const d = D.list.get(id);
    if (!d) return;
    if (editFor?.id === id) dropEdit();   // a typed change still waiting must not bring it back
    D.list.delete(id);
    if (D.selected === id) select(null);
    if (record) pushUndo(d, null);
    prim.update();
    try { await env.api('DELETE', path(id)); } catch (e) { if (!/no such/.test(e.message)) env.toast(e.message); }
  }
  // One step: a drawing before and after (null: none), or a list in before, every drawing Delete all took. The steps
  // of every symbol share one list, but Ctrl+Z and Ctrl+Y only take the open symbol's: the others wait for theirs.
  function pushUndo(before, after, sym = env.symbol()) {
    D.undo.push({ symbol: sym, before: before && clone(before), after: after && clone(after) });
    if (D.undo.length > 50) D.undo.shift();
    D.redo = D.redo.filter((u) => u.symbol !== sym);   // a new change: what was undone here can't come back
  }
  const takeStep = (list) => { const i = list.findLastIndex((u) => u.symbol === env.symbol()); return i < 0 ? null : list.splice(i, 1)[0]; };
  function undo() {
    flushEdit();   // a change still being typed: saved as its own step, the one this takes back
    const u = takeStep(D.undo);
    if (!u) return;
    D.redo.push(u);
    if (Array.isArray(u.before)) u.before.forEach((d) => save(clone(d)));
    else if (u.before) save(clone(u.before));
    else if (u.after) remove(u.after.id, false);
  }
  function redo() {
    flushEdit();   // a change still being typed is the newest: nothing undone before it comes back
    const u = takeStep(D.redo);
    if (!u) return;
    D.undo.push(u);
    if (u.after) save(clone(u.after));
    else if (Array.isArray(u.before)) u.before.forEach((d) => remove(d.id, false));
    else if (u.before) remove(u.before.id, false);
  }
  // The ids a broadcast changed or deleted while load()'s answer was on its way: the server may have read the list
  // before those changes, so the answer's copies of them are older than what was heard.
  let heard = null;
  async function load() {
    const sym = env.symbol();
    cancelCreating();
    flushEdit();   // a change still being typed: saved before the list goes (a watchlist click, a reconnect)
    D.list.clear();
    D.loadedFor = sym;
    select(null);
    const ids = heard = new Set();
    try {
      const r = await env.api('GET', path());
      if (sym !== env.symbol()) return;
      for (const d of r.drawings) if (!ids.has(d.id)) D.list.set(d.id, d);   // heard since: newer, or deleted
    } catch (e) { env.toast(`Drawings: ${e.message}`); } finally { if (heard === ids) heard = null; }
    prim.update();
  }
  function onMessage(m) {
    if (m.alert_deleted) {   // its alert deleted in the Alerts tab: an undo or redo from before must not bring it back
      const off = (d) => { if (d?.id === m.drawing.id && d.alert) d.alert = { ...d.alert, on: false }; };
      for (const u of [...D.undo, ...D.redo]) {
        if (u.symbol !== m.symbol) continue;
        for (const d of [u.before, u.after].flat()) off(d);
      }
      if (editSym === m.symbol) { off(editFor); off(editBefore); }   // nor a typed change's save, still to come
      if (D.drag && m.symbol === env.symbol()) { off(D.list.get(D.drag.id)); off(D.drag.orig); }   // nor a drag's drop
    }
    if (m.symbol !== env.symbol()) return;
    if (m.type === 'drawing') {
      if (D.drag?.id === m.drawing.id) return;     // being moved here: ours wins
      D.list.set(m.drawing.id, m.drawing);
      heard?.add(m.drawing.id);
      if (D.selected === m.drawing.id && !shown(m.drawing)) select(null);   // hidden on another device
    } else if (m.type === 'drawing_deleted') {
      for (const id of m.ids) { D.list.delete(id); heard?.add(id); if (D.selected === id) select(null); }
    }
    prim.update();
    renderEdit();
  }

  // ---- pointer input ---------------------------------------------------------------------------------------
  let lockSaved = null;
  function lockChart(on) {
    if (on && !lockSaved) {
      const o = env.chart.options();   // the library's own object, changed in place by the lock: keep a copy
      lockSaved = structuredClone({ handleScroll: o.handleScroll, handleScale: o.handleScale });
      env.chart.applyOptions({ handleScroll: false, handleScale: false });
    } else if (!on && lockSaved) {
      env.chart.applyOptions(lockSaved);
      lockSaved = null;
    }
  }
  function local(e) {
    const r = env.el.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top, W: r.width - (env.chart.priceScale('right').width() || 0),
      H: env.chart.paneSize(0).height };   // the price pane: indicator panes below it are not drawn on
  }
  const tolFor = (e) => (e.pointerType === 'mouse' ? 6 : 14);

  function onDown(e) {
    if (e.button > 0 || !env.bars().length) return;
    const { x, y, W, H } = local(e);
    if (x < 0 || y < 0 || x > W || y > H) return;     // the price or time scale: leave it to the chart
    const measuring = D.tool === 'measure' || (e.shiftKey && D.tool === 'select') || (D.measure && !D.measure.done);
    if (D.measure && D.measure.done) { D.measure = null; prim.update(); }   // any click clears the last one
    if (measuring) {
      const m = measurePoint(x, y);
      if (!m) return;
      lockChart(true);
      e.preventDefault();
      if (D.measure) { D.measure.b = m; D.measure.done = true; setTool('select'); }   // tap-tap: the second point
      else D.measure = { a: m, b: { ...m }, done: false, down: { x, y } };
      prim.update();
      return;
    }
    if (D.tool !== 'select') {
      const pt = pointAt(x, y);
      if (!pt) return;
      lockChart(true);
      e.preventDefault();
      if (D.creating) { D.creating.pts[D.creating.stage] = pt; D.creating.stage += 1; }
      else {
        const type = D.tool, n = POINTS[type] || 1;
        D.creating = { id: '_new', type, pts: Array.from({ length: n }, () => ({ ...pt })), style: { ...DRAW_DEFAULTS[type], ...lastStyle(type) }, stage: 1, x, y };
      }
      D.down = { x, y };
      prim.update();
      return;
    }
    const hit = hitTest(x, y, tolFor(e));
    if (!hit) { if (D.selected) select(null); return; }
    const d = D.list.get(hit.id);
    select(hit.id);
    if (d.style.locked) return;   // locked: selected, not moved
    lockChart(true);
    e.preventDefault();
    D.drag = { id: d.id, part: hit.part, x, y, orig: clone(d), moved: false,
      l0: ts().coordinateToLogical(x), p0: env.series.coordinateToPrice(y) };
  }

  function onMove(e) {
    if (D.measure && !D.measure.done) {
      const { x, y } = local(e), m = measurePoint(x, y);
      if (m) { D.measure.b = m; prim.update(); }
      return;
    }
    if (!D.creating && !D.drag) { hoverCursor(e); return; }
    const { x, y } = local(e);
    if (D.creating) {
      const c = D.creating, n = c.pts.length;
      if (c.stage < n || D.down) {   // the next point follows the pointer (the third one keeps the base line)
        const pt = pointAt(x, y);
        if (pt) { for (let i = Math.min(c.stage, n - 1); i < n; i++) c.pts[i] = { ...pt }; prim.update(); }
      }
      return;
    }
    const g = D.drag, d = D.list.get(g.id);
    if (!d) return;
    if (!g.moved && Math.hypot(x - g.x, y - g.y) < 3) return;
    g.moved = true;
    if (g.part === 'body') {
      const l = ts().coordinateToLogical(x), p = env.series.coordinateToPrice(y), tick = env.tick();
      if (l == null || p == null) return;
      const dp = Math.round((p - g.p0) / tick) * tick, dl = l - g.l0;
      d.pts = g.orig.pts.map((q) => ({ t: d.type === 'hline' ? q.t : logicalToTime(timeToLogical(q.t) + dl), p: q.p + dp }));
    } else {
      const pt = pointAt(x, y), i = Number(g.part.slice(1));
      if (!pt) return;
      if (POSITION[d.type]) movePositionPoint(d, i, pt);
      else d.pts[i] = pt;
    }
    prim.update();
  }
  // A new position: entry where clicked, stop 5 % of the visible price range away (at least 4 ticks), target twice
  // that, 20 bars wide.
  function positionFrom(type, e) {
    const tick = env.tick(), r = env.series.priceScale().getVisibleRange?.() || null, bars = env.bars();
    const rangeP = r ? Math.abs(r.to - r.from) : (bars.length ? Math.max(...bars.slice(-100).map((b) => b.h)) - Math.min(...bars.slice(-100).map((b) => b.l)) : 0);
    const risk = Math.max(4, Math.round(rangeP * 0.05 / tick)) * tick, dir = type === 'long' ? 1 : -1;
    const t1 = logicalToTime(timeToLogical(e.t) + 20);
    return [e, { t: t1, p: e.p + dir * 2 * risk }, { t: t1, p: e.p - dir * risk }];
  }
  function movePositionPoint(d, i, pt) {
    const tick = env.tick(), dir = d.type === 'long' ? 1 : -1, e = d.pts[0];
    if (i === 0) {   // the entry: its price and start, between the stop and the target
      const p = Math.min(Math.max(pt.p, Math.min(d.pts[1].p, d.pts[2].p) + tick), Math.max(d.pts[1].p, d.pts[2].p) - tick);
      d.pts[0] = { t: Math.min(pt.t, d.pts[1].t - 1), p };
    } else {         // the target or the stop, and the right edge for both
      const p = i === 1 ? (dir > 0 ? Math.max(pt.p, e.p + tick) : Math.min(pt.p, e.p - tick)) : (dir > 0 ? Math.min(pt.p, e.p - tick) : Math.max(pt.p, e.p + tick));
      const t = Math.max(pt.t, e.t + 1);
      d.pts[i] = { t, p };
      d.pts[3 - i] = { ...d.pts[3 - i], t };
    }
  }

  function onUp(e) {
    if (D.measure && !D.measure.done) {
      const { x, y } = local(e), m = D.measure;
      lockChart(false);
      if (m.down && Math.hypot(x - m.down.x, y - m.down.y) > 6) { m.done = true; setTool('select'); }   // dragged
      m.down = null;   // a click: the next click ends it
      prim.update();
      return;
    }
    if (D.creating) {
      const c = D.creating, { x, y } = local(e);
      const dragged = D.down && Math.hypot(x - D.down.x, y - D.down.y) > 6;
      D.down = null;
      const n = c.pts.length, pt = pointAt(x, y);
      if (dragged && c.stage < n && pt) { c.pts[c.stage] = pt; c.stage += 1; }   // a drag places the next point
      if (c.stage < n) return;                                                  // click-click: wait for the next point
      D.creating = null;
      lockChart(false);
      if (n > 1 && c.pts[0].t === c.pts[1].t && c.pts[0].p === c.pts[1].p) { prim.update(); return; }
      if (POSITION[c.type]) c.pts = positionFrom(c.type, c.pts[0]);
      const d = { id: newDrawingId(), type: c.type, pts: c.pts, style: stamp(c.style), alert: { on: false, direction: 'cross', repeat: false } };
      setTool('select');
      save(d, null);
      select(d.id);
      if (d.type === 'text') setTimeout(() => env.ui.edit.querySelector('[data-k=text]')?.select(), 0);
      return;
    }
    if (!D.drag) { lockChart(false); return; }   // e.g. the second tap of a measurement
    const g = D.drag, d = D.list.get(g.id);
    D.drag = null;
    lockChart(false);
    if (d && g.moved) save(d, g.orig);
  }

  function hoverCursor(e) {
    if (e.pointerType !== 'mouse') return;
    const { x, y, W, H } = local(e);
    let cursor = '';
    if (D.tool !== 'select') cursor = 'crosshair';
    else if (x >= 0 && y >= 0 && x <= W && y <= H) {
      const hit = hitTest(x, y, 6);
      if (hit) cursor = D.list.get(hit.id)?.style.locked ? 'pointer' : hit.part === 'body' ? 'move' : 'grab';
    }
    if (env.el.style.cursor !== cursor) env.el.style.cursor = cursor;
  }

  env.el.addEventListener('pointerdown', onDown, true);
  window.addEventListener('pointermove', onMove, true);
  window.addEventListener('pointerup', onUp, true);
  window.addEventListener('pointercancel', () => {   // the system took the touch (a call, the app switcher)
    const g = D.drag, d = g && D.list.get(g.id);
    if (d && g.moved) d.pts = g.orig.pts;   // a drag never let go of is not saved: back where it is
    D.drag = null; D.creating = null; D.down = null; lockChart(false); prim.update();
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('textarea, select, input:not([type=checkbox]):not([type=color]):not([type=button])')) return;   // typing
    if (e.key === 'Escape') letGo();
    else if ((e.key === 'Delete' || e.key === 'Backspace') && D.selected) { e.preventDefault(); remove(D.selected); }
    else if ((e.ctrlKey || e.metaKey) && (e.code === 'KeyY' || (e.code === 'KeyZ' && e.shiftKey))) { e.preventDefault(); redo(); }
    else if ((e.ctrlKey || e.metaKey) && e.code === 'KeyC' && D.selected && !String(getSelection?.() || '')) { clip(D.list.get(D.selected)); env.toast('Drawing copied: Ctrl+V pastes it (on any symbol, in any chart)'); }
    else if ((e.ctrlKey || e.metaKey) && e.code === 'KeyV' && clip()) { e.preventDefault(); paste(clip()); }
    else if ((e.ctrlKey || e.metaKey) && e.code === 'KeyZ') { e.preventDefault(); undo(); }
    else if (e.altKey && !e.ctrlKey && !e.metaKey) {   // TradingView's drawing keys (by key position: Alt on a Mac types symbols)
      const tool = e.shiftKey ? { KeyR: 'rect' }[e.code] : { KeyT: 'trend', KeyH: 'hline', KeyJ: 'hray', KeyV: 'vline', KeyF: 'fib' }[e.code];
      if (!tool) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      const p = tool === 'hline' ? env.hoverPrice?.() : null;
      if (p != null) addHline(p);   // as TradingView: the line goes straight to the price under the pointer
      else setTool(tool);
    }
  });

  // ---- tools, the style bar, pasted levels ------------------------------------------------------------------
  function lastStyle(type) {
    try { return JSON.parse(localStorage.getItem(`drawStyle.${type}`) || '{}') || {}; } catch { return {}; }
  }
  function rememberStyle(d) {
    if (typeof tkWaiting === 'function' && tkWaiting()) return;   // a workspace setting: not while the window waits (layout.js)
    const keep = styleOf(d);
    try { localStorage.setItem(`drawStyle.${d.type}`, JSON.stringify(keep)); } catch { /* private mode */ }
    if (typeof workspaces !== 'undefined' && workspaces) workspaces.dirty(`drawStyle.${d.type}`);
  }
  function cancelCreating() {   // a drawing half placed when the tool changes is dropped
    if (!D.creating) return;
    D.creating = null;
    D.down = null;
    lockChart(false);
    prim.update();
  }
  // Esc, or a right-click on the chart: the drawing being made, the tool picked, a measure and the selection let go.
  // True if there was one (the right-click then opens no menu).
  function letGo() {
    const had = !!(D.creating || D.selected || D.tool !== 'select' || D.measure);
    if (D.creating) { D.creating = null; lockChart(false); }
    D.measure = null;
    setTool('select');
    select(null);
    prim.update();
    return had;
  }
  // Hide all (the eye in Draw, the chart's menu, the object tree): nothing is drawn or can be picked up, so what is
  // selected or being made is let go first (its bar would stay up for a drawing not drawn, and Delete remove it
  // unseen); a new drawing (a tool picked, a line added, a paste or a clone), or one picked in the object tree, shows
  // them all again.
  function setHidden(on) {
    if (on === D.hidden) return;
    if (on) letGo();
    D.hidden = on;
    try { localStorage.setItem('drawHidden', on ? '1' : '0'); } catch { /* private mode */ }
    env.ui.bar.querySelector('[data-act=hide]').classList.toggle('on', on);
    prim.update();
  }
  function setTool(t) {
    cancelCreating();
    if (t !== 'select' && t !== 'measure') setHidden(false);   // a drawing to be made: drawn as it is made
    D.tool = t;
    if (t !== 'select') select(null);
    env.ui.bar.querySelectorAll('[data-tool]').forEach((b) => b.classList.toggle('on', b.dataset.tool === t));
    env.el.style.cursor = t === 'select' ? '' : 'crosshair';
  }
  function select(id) {
    if (id && D.list.has(id) && !shown(D.list.get(id))) return;   // hidden in the object tree, or another tab's
    if (id !== D.selected) { D.full = false; flushEdit(); }   // another drawing: back to the small bar; this one's typing saved
    D.selected = id;
    prim.update();
    renderEdit();
  }

  const FIELDS = {
    hline: ['color', 'width', 'dash', 'text', 'showPrice', 'alert'],
    hray: ['color', 'width', 'dash', 'text', 'showPrice', 'alert'],
    trend: ['color', 'width', 'dash', 'text', 'extendLeft', 'extendRight', 'alert'],
    rect: ['color', 'width', 'dash', 'fill', 'fillOpacity', 'mid', 'extendLeft', 'extendRight', 'text'],
    fib: ['color', 'width', 'dash', 'levels', 'fillOpacity', 'extendLeft', 'extendRight', 'reverse'],
    text: ['color', 'fontSize', 'background', 'text'],
    vline: ['color', 'width', 'dash', 'text'],
    arrow: ['color', 'width', 'dash', 'text'],
    channel: ['color', 'width', 'dash', 'fillOpacity', 'mid', 'extendLeft', 'extendRight'],
    fibext: ['color', 'width', 'dash', 'levels', 'fillOpacity', 'extendRight'],
    long: ['qty', 'text'],
    short: ['qty', 'text'],
    prange: ['color', 'text'],
    drange: ['color', 'text'],
  };
  const TOOL_NAMES = { hline: 'Horizontal line', hray: 'Horizontal ray', trend: 'Trend line', rect: 'Rectangle', fib: 'Fib retracement',
    text: 'Text', vline: 'Vertical line', arrow: 'Arrow', channel: 'Parallel channel', fibext: 'Fib extension', long: 'Long position',
    short: 'Short position', prange: 'Price range', drange: 'Date range' };
  // Each point's price, editable: what the points are called on each tool (vertical lines and date ranges have none).
  const PT_NAMES = { hline: ['Price'], hray: ['Price'], text: ['Price'], trend: ['Start', 'End'], arrow: ['From', 'To'],
    rect: ['First corner', 'Second corner'], fib: ['Start (100%)', 'End (0%)'], prange: ['From', 'To'],
    channel: ['Start', 'End', 'Width'], fibext: ['Start', 'Middle', 'End'], long: ['Entry', 'Target', 'Stop'], short: ['Entry', 'Target', 'Stop'] };
  function renderPts(d) {
    const box = env.ui.edit.querySelector('[data-f=pts]'), names = PT_NAMES[d.type] || [];
    if (!D.full || !names.length) { box.replaceChildren(); return; }
    if (box.dataset.for === `${d.id}:${d.type}` && box.contains(document.activeElement)) return;   // being typed in
    box.dataset.for = `${d.id}:${d.type}`;
    const dec = Number(env.tick() || 0.01).toFixed(10).split('.')[1].replace(/0+$/, '').length;   // 0.25 -> 2, 5e-7 (6J) -> 7, as the price axis
    box.replaceChildren(...names.map((name, i) => {
      const row = document.createElement('label'), lb = document.createElement('span'), inp = document.createElement('input');
      lb.className = 'lb';
      lb.textContent = name;
      const b = bondTick(env.symbol());   // Treasuries: typed and shown in 32nds (110'165)
      Object.assign(inp, b ? { type: 'text', inputMode: 'text', value: bond32(d.pts[i].p, env.tick()) }
        : { type: 'number', step: String(env.tick() || 0.01), inputMode: 'decimal', value: d.pts[i].p.toFixed(dec) });
      inp.dataset.k = `pt${i}`;
      row.append(lb, inp);
      return row;
    }));
  }
  function renderEdit() {
    const ed = env.ui.edit, d = D.list.get(D.selected);
    ed.hidden = !d;
    if (!d) { ed.classList.remove('full'); return; }
    const f = FIELDS[d.type], wide = window.innerWidth >= 760;
    const small = ['color', 'width', 'dash', 'fontSize', 'qty', ...(wide ? ['text', 'alert', 'tpl', 'tab'] : [])];
    ed.classList.toggle('full', !!D.full);
    ed.querySelector('.de-title').textContent = TOOL_NAMES[d.type] || 'Drawing';
    ed.querySelectorAll('[data-f]').forEach((el) => {
      const k = el.dataset.f, always = k === 'tpl' || k === 'tpls' || k === 'pts' || k === 'tab';
      el.hidden = (!f.includes(k) && !always) || (k === 'alert' && env.canAlert?.() === false);
      el.classList.toggle('c', small.includes(k));
    });
    renderTemplates(d);
    renderPts(d);
    ed.querySelectorAll('[data-k]').forEach((el) => {
      const k = el.dataset.k;
      if (document.activeElement === el) return;
      if (k === 'alertOn') el.checked = !!d.alert?.on;
      else if (k === 'tabOnly') { el.checked = !!d.style.tab; el.disabled = !env.tab?.(); }
      else if (k === 'alertDir') el.value = d.alert?.direction || 'cross';
      else if (k === 'levels') el.value = (d.style.levels || (d.type === 'fibext' ? FIB_EXT_LEVELS : FIB_LEVELS)).join(', ');
      else if (k === 'tpl' || /^pt\d$/.test(k)) return;   // the points' prices: renderPts
      else if (el.type === 'checkbox') el.checked = !!d.style[k];
      else el.value = d.style[k] ?? (k === 'fill' ? d.style.color : '');
    });
    const lk = ed.querySelector('[data-act=lock]');
    lk.classList.toggle('on', !!d.style.locked);
    lk.textContent = d.style.locked ? '🔒' : '🔓';
    lk.title = d.style.locked ? 'Locked: click to unlock' : 'Lock: it can no longer be moved';
    const a = env.alerts().find((x) => x.kind === 'line' && x.params?.drawing === d.id);
    ed.querySelector('.alert-state').textContent = !d.alert?.on ? '' : !a ? '' : a.enabled ? 'armed' : 'fired';
  }
  // Typing (a price, a label, a color) saves once it pauses, as one undo step from before the first keystroke, on the
  // symbol it was typed on (another may be open by the time it saves).
  let textTimer = 0, editFor = null, editBefore = null, editSym = null;
  function flushEdit() {
    clearTimeout(textTimer);
    const d = editFor, before = editBefore, sym = editSym;
    editFor = editBefore = editSym = null;
    if (d && D.list.has(d.id)) save(d, before, sym);   // deleted meanwhile (here or on another device): stays deleted
  }
  function dropEdit() { clearTimeout(textTimer); editFor = editBefore = editSym = null; }
  env.ui.edit.addEventListener('input', (e) => {
    const el = e.target.closest('[data-k]'), d = D.list.get(D.selected);
    if (!el || !d) return;
    const k = el.dataset.k;
    if (k === 'tpl') return;
    if (editFor && editFor !== d) flushEdit();
    const before = editFor === d ? editBefore : clone(d);
    if (/^pt\d$/.test(k)) {   // a point's exact price
      const i = Number(k.slice(2)), p = bondTick(env.symbol()) ? parse32(el.value) : Number(el.value);
      if (el.value === '' || !Number.isFinite(p) || !d.pts[i]) return;   // any price: a spread or a yield goes below 0
      const tick = env.tick() || 0.01, pt = { ...d.pts[i], p: Math.round(p / tick) * tick };
      if (POSITION[d.type]) movePositionPoint(d, i, pt); else d.pts[i] = pt;
      prim.update();
      clearTimeout(textTimer);
      editFor = d; editBefore = before; editSym = env.symbol();
      textTimer = setTimeout(flushEdit, 500);
      return;
    }
    if (k === 'qty') { const q = Math.round(Number(el.value)); if (!(q >= 1 && q <= 100000)) return; d.style = { ...d.style, qty: q }; }
    else if (k === 'alertOn') {   // switched on: kept to the line as this chart draws it
      const a = { ...(d.alert || {}), on: el.checked, direction: d.alert?.direction || 'cross' };
      delete a.months; delete a.clock; delete a.per; delete a.log;
      d.alert = el.checked ? { ...a, ...lineLayout(d) } : a;
    }
    else if (k === 'alertDir') d.alert = { ...(d.alert || {}), direction: el.value };
    else if (k === 'tabOnly') { const s = { ...d.style }; delete s.tab; if (el.checked && env.tab?.()) s.tab = env.tab(); d.style = s; }
    else if (k === 'levels') {
      const lv = el.value.split(/[,\s]+/).filter(Boolean).map(Number);
      if (!lv.length || lv.some((v) => !Number.isFinite(v) || v < -10 || v > 10)) return;
      d.style = { ...d.style, levels: lv.slice(0, 24) };
    } else if (el.type === 'checkbox') d.style = { ...d.style, [k]: el.checked };
    else if (el.type === 'number' || el.tagName === 'SELECT' && /^\d+$/.test(el.value)) {
      const n = Number(el.value), lim = NUM_LIMITS[k];   // what the server takes: anything else is never saved
      if (lim && !(Number.isInteger(n) && n >= lim[0] && n <= lim[1])) return;
      d.style = { ...d.style, [k]: n };
    }
    else d.style = { ...d.style, [k]: el.value };
    prim.update();
    rememberStyle(d);
    clearTimeout(textTimer);
    editFor = d; editBefore = before; editSym = env.symbol();
    if (['text', 'color', 'number'].includes(el.type)) textTimer = setTimeout(flushEdit, 400); else flushEdit();
  });
  // Style templates, per tool (TradingView's "Template"): saved on the server, the same on every device.
  const tplKind = (type) => `draw:${type}`;
  function styleOf(d) {   // a drawing's look: not its text, group, tab, lock or eye
    const keep = { ...d.style };
    for (const k of ['text', 'group', 'tab', 'locked', 'hidden']) delete keep[k];
    return keep;
  }
  function renderTemplates(d) {
    const T = env.templates, names = T ? T.names(tplKind(d.type)) : [];
    const sel = env.ui.edit.querySelector('[data-k=tpl]');
    sel.replaceChildren(new Option('Template ▾', ''), ...names.map((n) => new Option(`Apply: ${n}`, `a:${n}`)),
      new Option('Save style as…', 's'), ...names.map((n) => new Option(`Delete: ${n}`, `d:${n}`)));
    sel.value = '';
    const list = env.ui.edit.querySelector('.tpl-list');
    list.replaceChildren(...(names.length ? names.map((n) => {
      const row = document.createElement('div'), use = document.createElement('button');
      row.className = 'tpl-row';
      row.dataset.name = n;
      Object.assign(use, { type: 'button', className: 'tpl-use', textContent: n, title: 'Apply this style (right-click, or a long press, to delete)' });
      use.dataset.tpl = `a:${n}`;
      row.append(use);
      return row;
    }) : [Object.assign(document.createElement('small'), { className: 'dim', textContent: `None yet for the ${(TOOL_NAMES[d.type] || 'tool').toLowerCase()}.` })]));
  }
  async function useTemplate(v) {
    const d = D.list.get(D.selected), T = env.templates;
    if (!d || !v || !T) return;
    const kind = tplKind(d.type);
    try {
      if (v === 's') {
        const asked = env.ask ? await env.ask({ title: 'Save this style as a template', value: '', ok: 'Save', placeholder: 'e.g. Key level', names: T.names(kind) }) : prompt('Template name');
        const name = asked?.trim().slice(0, 40);   // the name it is saved under, for the check too
        if (!name) return;
        await T.save(kind, name, styleOf(d));
        env.toast(`Template "${name}" saved on every device`);
      } else if (v.startsWith('a:')) {
        const before = clone(d);
        d.style = { ...d.style, ...T.data(kind, v.slice(2)) };
        rememberStyle(d);
        save(d, before);
      } else if (v.startsWith('d:')) await T.del(kind, v.slice(2));
    } catch (e) { env.toast(e.message); }
    renderTemplates(d);
  }
  env.ui.edit.querySelector('[data-k=tpl]').addEventListener('change', (e) => { const v = e.target.value; e.target.value = ''; useTemplate(v); });
  onMenuPress(env.ui.edit.querySelector('.tpl-list'), '.tpl-row', (row, x, y) => {   // a template's menu: Apply, Delete
    const n = row.dataset.name;
    env.menu(x, y, [
      { label: `Apply "${n}"`, fn: () => useTemplate(`a:${n}`) },
      'sep',
      { label: 'Delete…', fn: async () => {
        if (env.ask && (await env.ask({ title: `Delete the template "${n}"?`, input: false, ok: 'Delete', danger: true })) === null) return;
        useTemplate(`d:${n}`);
      } },
    ]);
  });
  env.ui.edit.querySelector('.tpls').addEventListener('click', (e) => {
    const b = e.target.closest('[data-tpl], [data-act=tpl-save]');
    if (b) useTemplate(b.dataset.tpl || 's');
  });
  const setFull = (on) => { D.full = on; renderEdit(); };
  env.ui.edit.querySelector('[data-act=more]').onclick = () => setFull(true);
  env.ui.edit.querySelector('[data-act=less]').onclick = () => setFull(false);
  env.el.addEventListener('dblclick', (e) => {   // a drawing double-clicked: all its settings, as on TradingView
    const { x, y } = local(e), hit = hitTest(x, y, 8);
    if (!hit) return;
    if (D.selected !== hit.id) select(hit.id);
    setFull(true);
  });
  env.ui.edit.querySelector('[data-act=delete]').onclick = () => D.selected && remove(D.selected);
  env.ui.edit.querySelector('[data-act=lock]').onclick = () => { const d = D.list.get(D.selected); if (d) setLocked(d, !d.style.locked); };
  env.ui.edit.querySelector('[data-act=clone]').onclick = () => { const d = D.list.get(D.selected); if (d) paste(d, true); };
  env.ui.edit.querySelector('[data-act=close]').onclick = () => select(null);

  env.ui.bar.addEventListener('click', async (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.tool) setTool(D.tool === b.dataset.tool && b.dataset.tool !== 'select' ? 'select' : b.dataset.tool);
    else if (b.dataset.act === 'magnet') {
      D.magnet = !D.magnet;
      try { localStorage.setItem('drawMagnet', D.magnet ? '1' : '0'); } catch { /* private mode */ }
      b.classList.toggle('on', D.magnet);
    } else if (b.dataset.act === 'hide') setHidden(!D.hidden);
    else if (b.dataset.act === 'paste') env.ui.paste.hidden = !env.ui.paste.hidden;
    else if (b.dataset.act === 'clear') {
      if (!D.list.size || (await env.ask({ title: `Delete all ${D.list.size} drawings on ${env.symbol()}?`, message: 'Ctrl+Z brings them all back.', input: false, ok: 'Delete', danger: true })) === null) return;
      const all = [...D.list.values()];
      pushUndo(all, null);   // one step, however many: the 50-step limit can't take some of them
      D.list.clear();
      select(null);
      env.api('DELETE', path()).catch((err) => env.toast(err.message));
    } else if (b.dataset.act === 'undo') undo();
    else if (b.dataset.act === 'redo') redo();
    else if (b.dataset.act === 'tree') showTree(env.ui.tree.hidden);
  });
  env.ui.bar.querySelector('[data-act=magnet]').classList.toggle('on', D.magnet);
  env.ui.bar.querySelector('[data-act=hide]').classList.toggle('on', D.hidden);

  // Pasted levels: "7845.75|VAH, 7830|POC; 7810" (commas, semicolons or new lines) become labelled lines.
  function parseLevels(text) {
    const out = [], bad = [];
    for (const raw of text.split(/[,;\n]+/)) {
      const item = raw.trim();
      if (!item) continue;
      const [price, ...rest] = item.split('|');
      const n = price.replace(/(\d)\s+/g, '$1').trim();   // spaces in a number go (7 845.75), not after a dash: '- 7845' is a bullet
      // Treasuries: in 32nds too, as their chart shows and copies them (110'165, 110'16.5, 110-16)
      const p = bondTick(env.symbol()) && /^[+-]?\d+['-]\d\d?(?:\.?\d)?$/.test(n) ? parse32(n, env.tick()) : Number(n);
      if (price.trim() && Number.isFinite(p)) out.push({ p, text: rest.join('|').trim() });   // below 0 too (FRED:T10Y2Y)
      else bad.push(item);
    }
    return { out, bad };
  }
  env.ui.paste.querySelector('[data-act=add]').onclick = async () => {
    const box = env.ui.paste, { out, bad } = parseLevels(box.querySelector('textarea').value);
    const color = box.querySelector('[data-k=pcolor]').value, bars = env.bars();
    const num = (k) => { const v = box.querySelector(`[data-k=${k}]`).value.trim(); return v === '' ? NaN : Number(v); };   // empty: no range
    const top = num('top'), bottom = num('bottom'), range = Number.isFinite(top) && Number.isFinite(bottom);
    if (!out.length && !range) { env.toast(bad.length ? `Not prices: ${bad.slice(0, 3).join(', ')}` : 'Nothing to add'); return; }
    if (box.querySelector('[data-k=replace]').checked) {   // the set this tab shows: another tab's own set stays
      await Promise.all([...D.list.values()].filter((d) => d.style.group === 'paste' && onTab(d)).map((d) => remove(d.id, false)));
    }
    const t = bars.length ? bars[bars.length - 1].t : Date.now();
    setHidden(false);
    for (const { p, text } of out) {
      save({ id: newDrawingId(), type: 'hline', pts: [{ t, p }], style: stamp({ ...DRAW_DEFAULTS.hline, color, text: text ? `${text} ${env.fmt(p)}` : '', group: 'paste' }), alert: { on: false, direction: 'cross', repeat: false } }, null);
    }
    if (range && top !== bottom && bars.length) {   // the range box, across the whole chart
      save({ id: newDrawingId(), type: 'rect', pts: [{ t: bars[0].t, p: top }, { t, p: bottom }],
        style: stamp({ ...DRAW_DEFAULTS.rect, color, fill: color, fillOpacity: 10, mid: true, extendLeft: true, extendRight: true, text: 'Range', group: 'paste' }),
        alert: { on: false, direction: 'cross', repeat: false } }, null);
    }
    env.toast(`Added ${out.length} level${out.length === 1 ? '' : 's'}${bad.length ? `; skipped ${bad.length}` : ''}`);
    box.hidden = true;
  };
  env.ui.paste.querySelector('[data-act=cancel]').onclick = () => { env.ui.paste.hidden = true; };

  // ---- the primitive ---------------------------------------------------------------------------------------------
  const prim = {
    p: null,
    views: [{ zOrder: () => 'top', renderer: () => (prim.p ? { draw: (target) => target.useMediaCoordinateSpace((sc) => render(sc.context, sc.mediaSize)) } : null) }],
    attached(p) { this.p = p; },
    detached() { this.p = null; axisStack.drop(prim); },
    paneViews() { return this.views; },
    priceAxisViews() {   // price tags for lines and rays, and for the selected drawing's points (stacked with the rest)
      if (D.hidden) return axisStack.views(prim, []);
      const out = [];
      const tag = (p, color) => out.push({ coordinate: () => Y(p) ?? -100, text: () => env.fmt(p), textColor: () => '#ffffff', backColor: () => color, visible: () => Y(p) != null });
      for (const d of D.list.values()) if ((d.type === 'hline' || d.type === 'hray') && d.style.showPrice !== false && shown(d)) tag(d.pts[0].p, d.style.color);
      const sel = D.list.get(D.selected) || D.creating;
      if (sel && sel.type !== 'hline' && sel.type !== 'hray') sel.pts.forEach((q) => tag(q.p, sel.style.color || '#2962ff'));
      return axisStack.views(prim, out);
    },
    update() { if (this.p) this.p.requestUpdate(); treeSoon(); },
  };
  env.series.attachPrimitive(prim);

  // ---- the object tree, as TradingView's: every drawing on this symbol, newest first. Click one to select it (and
  // bring it into view); its eye hides it, its lock stops it moving; right-click (a long press) has Delete. ---------
  let treeFrame = 0;
  function treeSoon() { if (env.ui.tree && !env.ui.tree.hidden && !treeFrame) treeFrame = requestAnimationFrame(() => { treeFrame = 0; renderTree(); }); }
  function treeDetail(d) {
    const p = d.pts.map((q) => env.fmt(q.p)), text = (d.style.text || '').trim();
    const where = d.type === 'vline' || d.type === 'drange' ? d.pts.map((q) => ax.md.format(q.t)).join(' → ')
      : ['hline', 'hray', 'text', 'long', 'short'].includes(d.type) ? p[0] : `${p[0]} → ${p.at(-1)}`;
    return text ? `${where} · ${text.length > 24 ? `${text.slice(0, 23)}…` : text}` : where;
  }
  function revealDrawing(d) {   // its first point in view, keeping the zoom
    if (d.type === 'hline') return;
    const l = timeToLogical(d.pts[0].t), r = ts().getVisibleLogicalRange();
    if (l == null || !r || (l >= r.from && l <= r.to)) return;
    const span = r.to - r.from;
    ts().setVisibleLogicalRange({ from: l - span / 2, to: l + span / 2 });
  }
  function renderTree() {
    const box = env.ui.tree, el = (tag, props = {}) => Object.assign(document.createElement(tag), props);
    if (box.hidden) return;
    const all0 = [...D.list.values()].reverse(), list = all0.filter(onTab), others = all0.filter((d) => !onTab(d));
    const all = el('button', { type: 'button', className: 'dt-all', textContent: D.hidden ? 'Show all' : 'Hide all', title: 'Every drawing on this chart' });
    all.onclick = () => env.ui.bar.querySelector('[data-act=hide]').click();
    const close = el('button', { type: 'button', className: 'dt-b', textContent: '✕', title: 'Close' });
    close.onclick = () => showTree(false);
    const hd = el('div', { className: 'dt-hd' });
    hd.append(el('b', { textContent: `Drawings · ${env.symbol()}` }), el('small', { textContent: String(list.length) }), el('span', { className: 'grow' }), all, close);
    const rows = list.map((d) => {
      const row = el('div', { className: `dt-row${d.id === D.selected ? ' on' : ''}${d.style.hidden ? ' off' : ''}` });
      const sw = el('i', { className: 'dt-sw' });
      sw.style.background = d.style.color || '#2962ff';
      const name = el('span', { className: 'dt-name', textContent: TOOL_NAMES[d.type] || d.type });
      const det = el('span', { className: 'dt-det', textContent: treeDetail(d) });
      const btn = (cls, html, title, fn) => { const b = el('button', { type: 'button', className: `dt-b ${cls}`, title, innerHTML: html }); b.onclick = (e) => { e.stopPropagation(); fn(); }; return b; };
      const eye = btn('eye', d.style.hidden ? '◌' : '◉', d.style.hidden ? 'Show' : 'Hide', () => restyle(d, (s) => ({ ...s, hidden: !s.hidden })));
      const lock = btn('lock', d.style.locked ? '🔒' : '🔓', d.style.locked ? 'Unlock' : 'Lock', () => setLocked(d, !d.style.locked));
      lock.classList.toggle('on', !!d.style.locked);
      row.dataset.id = d.id;
      row.onclick = () => { if (d.style.hidden) return; setHidden(false); select(d.id); revealDrawing(d); };
      if (d.style.tab) name.title = 'On this tab only';
      row.append(sw, name, det, eye, lock);
      return row;
    });
    if (others.length) {   // made for another tab (or one since closed): share them here, or delete them
      rows.push(el('div', { className: 'dt-sub', textContent: `On other tabs only (${others.length})` }));
      for (const d of others) {
        const row = el('div', { className: 'dt-row off' }), sw = el('i', { className: 'dt-sw' });
        sw.style.background = d.style.color || '#2962ff';
        const share = el('button', { type: 'button', className: 'dt-b', textContent: '⇄', title: 'Show it on every tab' });
        share.onclick = (e) => { e.stopPropagation(); restyle(d, (st) => { const s = { ...st }; delete s.tab; return s; }); };
        row.dataset.id = d.id;
        row.append(sw, el('span', { className: 'dt-name', textContent: TOOL_NAMES[d.type] || d.type }), el('span', { className: 'dt-det', textContent: treeDetail(d) }), share);
        rows.push(row);
      }
    }
    box.replaceChildren(hd, ...(rows.length ? rows : [el('p', { className: 'dt-none', textContent: `No drawings on ${env.symbol()} yet: pick a tool to draw.` })]));
  }
  if (env.ui.tree && env.menu) {
    onMenuPress(env.ui.tree, '.dt-row', (row, x, y) => {   // a drawing's menu: show or hide, lock, share, Delete
      const d = D.list.get(row.dataset.id);
      if (!d) return;
      const here = onTab(d);
      env.menu(x, y, [
        ...(here ? [{ label: d.style.hidden ? 'Show' : 'Hide', fn: () => restyle(d, (s) => ({ ...s, hidden: !s.hidden })) },
          { label: d.style.locked ? 'Unlock' : 'Lock', fn: () => setLocked(d, !d.style.locked) }]
          : [{ label: 'Show it on every tab', fn: () => restyle(d, (st) => { const s = { ...st }; delete s.tab; return s; }) }]),
        'sep',
        { label: 'Delete', key: 'Ctrl+Z undoes', fn: () => remove(d.id) },
      ]);
    }, 'button');
  }
  function showTree(on) {
    env.ui.tree.hidden = !on;
    env.ui.bar.querySelector('[data-act=tree]')?.classList.toggle('on', on);
    if (on) renderTree();
  }

  // A copy of a drawing where it is (clone) or on whatever symbol is open (paste), selected so it can be moved.
  function paste(src, here = false) {   // here: a clone, on the same tabs as the original
    const d = { ...clone(src), id: newDrawingId(), alert: { on: false, direction: 'cross', repeat: false } };
    delete d.style.group;   // the user's own line: not one the next Paste levels replaces
    d.style = here ? { ...d.style, locked: false } : stamp({ ...d.style, locked: false });
    setHidden(false);
    save(d, null);
    select(d.id);
  }
  // Ctrl+C's copy is kept as text in the page's top window, so that Ctrl+V pastes it in any chart of a layout (each is
  // this same page in its own frame, layout.js); in this chart if the top window is another site's. d: a drawing copied.
  function clip(d) {
    try {
      const w = window.top || window;
      if (d) w.tkDrawClip = JSON.stringify(d);
      return w.tkDrawClip ? JSON.parse(w.tkDrawClip) : null;
    } catch {
      if (d) D.clip = clone(d);
      return D.clip || null;
    }
  }
  // The lock, and the object tree's eye and share, on the drawing as it is now: a change still being typed is saved
  // first, or its save a moment later would put the old style back and take this change's undo step.
  function restyle(d, style) {
    flushEdit();
    d = D.list.get(d.id) || d;
    save({ ...d, style: style(d.style) }, clone(d));
  }
  function setLocked(d, on) {
    restyle(d, (s) => ({ ...s, locked: on }));
    env.toast(on ? 'Locked: it can no longer be moved by mistake' : 'Unlocked');
  }
  function addHline(p) {   // the chart's right-click menu, Alt+H: a horizontal line at that price
    p = Math.round(p / env.tick()) * env.tick();
    const d = { id: newDrawingId(), type: 'hline', pts: [{ t: env.bars().at(-1)?.t ?? Date.now(), p }], style: stamp({ ...DRAW_DEFAULTS.hline, ...lastStyle('hline') }),
      alert: { on: false, direction: 'cross', repeat: false } };
    setHidden(false);
    save(d, null);
    select(d.id);
  }
  return { load, onMessage, setTool, update: () => { prim.update(); renderEdit(); }, state: D, prim, parseLevels, timeToLogical, logicalToTime, addHline,
    select, letGo, showTree, templatesChanged: () => { const d = D.list.get(D.selected); if (d) renderTemplates(d); } };
}
