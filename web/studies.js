'use strict';
/* Chart drawing for Lightweight Charts (series primitives): the right-edge volume profile, anchored VWAP and the
   ports of the Tatanka NinjaTrader indicators' OnRender.
   Right-edge profiles arrive from the server in whole ticks: {kind, start, end, p0, tick, v:[...], d:[...], poc, val, vah}. */

const PROFILE_COLORS = {
  va: 'rgba(91, 156, 246, 0.30)',
  out: 'rgba(139, 150, 165, 0.16)',
  poc: 'rgba(255, 152, 0, 0.70)',
  pocLine: 'rgba(255, 152, 0, 0.90)',
  vaLine: 'rgba(91, 156, 246, 0.60)',
};
const MIN_ROW_PX = 3;   // rows are grouped so each is at least this tall
const NUM_FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';   // style.css --num: the app's numbers
// Treasury futures in 32nds, as CME, NinjaTrader and TradingView quote them: ZB 115'16 = 115 + 16/32, ZN 110'165 =
// 110 + 16.5/32, ZF 108'162 = 16.25/32, ZT 104'057 = 5.75/32 (the last digit: eighths of a 32nd, 0 1 2 3 5 6 7 8).
const BOND_TICK = { ZT: 1 / 256, ZF: 1 / 128, ZN: 1 / 64, TN: 1 / 64, ZB: 1 / 32, UB: 1 / 32, TWE: 1 / 32 };
const bondTick = (root) => BOND_TICK[String(root || '').toUpperCase()] || 0;
function bond32(p, tick) {
  if (p == null || !Number.isFinite(Number(p))) return '';
  p = Number(p);
  const per = Math.round(1 / tick), n = Math.round(Math.abs(p) / tick), whole = Math.floor(n / per);
  const x32 = (n - whole * per) * tick * 32, t = Math.floor(x32 + 1e-9);
  const tail = tick < 1 / 32 ? String(Math.floor((x32 - t) * 10 + 1e-9)) : '';
  return `${p < 0 && n ? '-' : ''}${whole}'${String(t).padStart(2, '0')}${tail}`;
}
// A typed price: 110'165, 110'16.5, 110'16 or 110-165 in 32nds, else a plain number (110.515625, 7,450.25).
function parse32(text, tick = 0) {
  const s = String(text ?? '').trim().replace(/,/g, '');
  const m = /^([+-]?)(\d+)['-](\d\d?)(?:\.?(\d))?$/.exec(s);
  if (!m) return parseFloat(s);
  const eighth = { 0: 0, 1: 0.125, 2: 0.25, 3: 0.375, 5: 0.5, 6: 0.625, 7: 0.75, 8: 0.875 }[m[4] ?? 0] ?? Number(m[4]) / 10;
  if (Number(m[3]) > 31) return NaN;
  const v = (m[1] === '-' ? -1 : 1) * (Number(m[2]) + (Number(m[3]) + eighth) / 32);
  return tick ? Math.round(v / tick) * tick : v;
}

// Price-axis tags from this app's primitives (drawn lines, alerts, the studies' price boxes) don't cover each other.
// The chart library spaces its own labels, not these, so two at one price showed only the last drawn. Every tag goes
// through here: once per paint, all of them are sorted by price and tags that would overlap are spread evenly around
// the middle of their prices, as TradingView stacks them. An owner hands over its whole list each time (an empty one
// too) and drops it when it leaves the chart.
const axisStack = {
  owners: new Map(),   // primitive -> its tags, each with its own price's coordinate as raw()
  pos: new Map(),      // tag -> where it is drawn
  laid: false,
  height: 19,          // a tag's height in px, for the axis font (app.js sets it)
  views(owner, list) {
    const tags = list.map((v) => {
      const t = { ...v, raw: v.coordinate };
      t.coordinate = () => axisStack.at(t);
      return t;
    });
    this.owners.set(owner, tags);
    this.laid = false;
    return tags;
  },
  drop(owner) { if (this.owners.delete(owner)) this.laid = false; },
  at(t) {
    if (!this.laid) this.layout();
    return this.pos.has(t) ? this.pos.get(t) : t.raw();
  },
  layout() {
    this.laid = true;
    queueMicrotask(() => { this.laid = false; });   // good for this paint; prices move between paints
    this.pos.clear();
    const items = [];
    for (const tags of this.owners.values()) {
      for (const t of tags) {
        let y = null;
        try { y = (!t.visible || t.visible()) ? t.raw() : null; } catch { y = null; }
        if (y != null && Number.isFinite(y) && y > -50) items.push({ t, y });
      }
    }
    items.sort((a, b) => a.y - b.y);
    const h = this.height, groups = [];
    const top = (g) => g.sum / g.items.length - (g.items.length - 1) * h / 2;
    for (const it of items) {
      groups.push({ items: [it], sum: it.y });
      while (groups.length > 1) {   // overlapping the group above: one group, centred on all their prices
        const g = groups[groups.length - 1], above = groups[groups.length - 2];
        if (top(above) + above.items.length * h <= top(g)) break;
        above.items.push(...g.items);
        above.sum += g.sum;
        groups.pop();
      }
    }
    for (const g of groups) {
      let y = top(g);
      for (const it of g.items) { this.pos.set(it.t, y); y += h; }
    }
  },
};

// Index of the first bar opening at or after ms, or -1.
function barIndexFrom(bars, ms) {
  let lo = 0, hi = bars.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (bars[m].t < ms) lo = m + 1; else hi = m; }
  return lo < bars.length ? lo : -1;
}
// Index of the last bar opening at or before ms, or -1.
function barIndexTo(bars, ms) {
  let lo = 0, hi = bars.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (bars[m].t <= ms) lo = m + 1; else hi = m; }
  return lo - 1;
}

function hline(ctx, y, x0, x1, color, dash) {
  if (y == null) return;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.setLineDash(dash);
  ctx.beginPath();
  ctx.moveTo(x0, Math.round(y) + 0.5);
  ctx.lineTo(x1, Math.round(y) + 0.5);
  ctx.stroke();
  ctx.restore();
}

// Draws one profile. dir +1 grows right from xBase (session profiles), -1 grows left (right-edge profile).
function drawProfile(ctx, p, rowTicks, priceY, xBase, maxLen, dir, lineX0, lineX1) {
  const tick = p.tick, base = Math.round(p.p0 / tick);
  const rows = new Map();
  for (let i = 0; i < p.v.length; i++) {
    if (!p.v[i]) continue;
    const r = Math.floor((base + i) / rowTicks);
    rows.set(r, (rows.get(r) || 0) + p.v[i]);
  }
  let max = 0;
  rows.forEach((v) => { if (v > max) max = v; });
  if (!max) return;
  const val = Math.round(p.val / tick), vah = Math.round(p.vah / tick);
  const pocRow = Math.floor(Math.round(p.poc / tick) / rowTicks);
  rows.forEach((v, r) => {
    const lo = r * rowTicks, hi = lo + rowTicks - 1;
    const yTop = priceY((hi + 0.5) * tick), yBot = priceY((lo - 0.5) * tick);
    if (yTop == null || yBot == null) return;
    const h = Math.max(1, yBot - yTop - (yBot - yTop > 3 ? 1 : 0));
    const len = (v / max) * maxLen;
    ctx.fillStyle = r === pocRow ? PROFILE_COLORS.poc : hi >= val && lo <= vah ? PROFILE_COLORS.va : PROFILE_COLORS.out;
    ctx.fillRect(dir > 0 ? xBase : xBase - len, yTop, len, h);
  });
  hline(ctx, priceY(p.poc), lineX0, lineX1, PROFILE_COLORS.pocLine, []);
  hline(ctx, priceY(p.vah), lineX0, lineX1, PROFILE_COLORS.vaLine, [4, 3]);
  hline(ctx, priceY(p.val), lineX0, lineX1, PROFILE_COLORS.vaLine, [4, 3]);
}

function drawProfiles(ctx, size, chart, series, s) {
  const bars = s.bars;
  if (!bars.length || (!s.sessions.length && !s.range)) return;
  const priceY = (p) => series.priceToCoordinate(p);
  const ref = bars[bars.length - 1].c, a = priceY(ref), b = priceY(ref + s.tick);
  if (a == null || b == null) return;
  const rowTicks = Math.max(1, Math.ceil(MIN_ROW_PX / Math.max(Math.abs(b - a), 1e-6)));
  const ts = chart.timeScale(), spacing = ts.options().barSpacing;
  for (const p of s.sessions) {
    const i0 = barIndexFrom(bars, p.start), i1 = barIndexTo(bars, p.end);
    if (i0 < 0 || i1 < i0) continue;
    const xa = ts.logicalToCoordinate(i0), xb = ts.logicalToCoordinate(i1);
    if (xa == null || xb == null) continue;
    // pinned to the left edge while the session's start is scrolled off but the rest is on screen
    const x0 = Math.max(xa - spacing / 2, 0), x1 = xb + spacing / 2;
    const maxLen = Math.min((x1 - x0) * 0.8, 260);
    if (x1 < 0 || x0 > size.width || maxLen < 8) continue;
    drawProfile(ctx, p, rowTicks, priceY, x0, maxLen, 1, x0, x1);
  }
  if (s.range) {
    const w = size.width, maxLen = Math.min(w * 0.22, 240);
    drawProfile(ctx, s.range, rowTicks, priceY, w - 1, maxLen, -1, w - maxLen, w);
  }
}

class ProfilePrimitive {
  constructor(getState) {
    this.getState = getState;
    this.p = null;
    this.views = [{
      zOrder: () => 'bottom',
      renderer: () => (this.p ? {
        draw: (target) => target.useMediaCoordinateSpace((scope) =>
          drawProfiles(scope.context, scope.mediaSize, this.p.chart, this.p.series, this.getState())),
      } : null),
    }];
  }
  attached(p) { this.p = p; }
  detached() { this.p = null; axisStack.drop(this); }
  paneViews() { return this.views; }
  update() { if (this.p) this.p.requestUpdate(); }
}

// Anchored VWAP from bar data: cumulative typical price (h+l+c)/3 x volume from the anchor bar on. The anchor is a bar's
// open on any timeframe: on a longer one, the bar it falls in (time bars, periodMs long, or to periodMs(bar) when that
// is a function: where the bar really ends, an RTH chart's at the RTH close), not the next one; the next one when the
// bar before ended first (a session break, the night), or on bars of no fixed length.
function anchoredVwap(bars, anchorMs, timeOf, periodMs = 0) {
  let i0 = barIndexTo(bars, anchorMs);
  const end = (b) => (typeof periodMs === 'function' ? periodMs(b) : b.t + periodMs);
  if (i0 < 0 || end(bars[i0]) <= anchorMs) i0 = barIndexFrom(bars, anchorMs);
  if (i0 < 0) return [];
  const out = [];
  let pv = 0, v = 0;
  for (let i = i0; i < bars.length; i++) {
    const b = bars[i];
    pv += ((b.h + b.l + b.c) / 3) * b.v;
    v += b.v;
    out.push(v > 0 ? { time: timeOf(b), value: pv / v } : { time: timeOf(b) });
  }
  return out;
}

/* ---- Tatanka AMT Toolkit drawing: a port of the NinjaTrader OnRender (lines, zones, single prints,
   volume profiles, 80% Rule fill). The server sends the engine's frame; this only draws it. ---------- */
const argb = (c) => `rgba(${(c >>> 16) & 255}, ${(c >>> 8) & 255}, ${c & 255}, ${((c >>> 24) & 255) / 255})`;
const withAlpha = (c, transparency) => `rgba(${(c >>> 16) & 255}, ${(c >>> 8) & 255}, ${c & 255}, ${Math.floor(255 * (100 - transparency) / 100) / 255})`;
// FontSize(): tiny 10, small 12, large 18, huge 24, anything else (Normal, auto) 14, in Segoe UI.
const AMT_FONT = { tiny: 10, small: 12, large: 18, huge: 24 };
const amtFontPx = (size) => AMT_FONT[(size || '').toLowerCase()] || 14;
// Direct2D's own dash styles with flat caps, as the Toolkit makes them: Dash {2,2} and Dot {0,2} times the width.
// Zero-length dots with flat caps draw nothing in Direct2D, so a Dotted line is not seen on NinjaTrader either.
function amtDash(ctx, style, width) {
  ctx.lineCap = 'butt';
  if (style === 'Dotted') ctx.setLineDash([0, width * 2]);
  else if (style === 'Dashed') ctx.setLineDash([width * 2, width * 2]);
  else ctx.setLineDash([]);
}
// "Force on Top" names (IsTopLayer) for lines named otherwise.
const AMT_TOP_NAMES = { 'Profile POC': 'Developing POC', 'Profile VAH': 'Developing VAH', 'Profile VAL': 'Developing VAL',
  'Prev Day High': 'pdHigh', 'Prev Day Low': 'pdLow', 'Prev Month VAH': 'pmVAH', 'Prev Month VAL': 'pmVAL' };

// x of a logical (bar) position. Lightweight Charts' logicalToCoordinate takes whole bar indexes only (a
// fraction comes back as 0), so the part inside a bar is added from the bar spacing.
function logicalX(ts, l) {
  const i = Math.floor(l), x = ts.logicalToCoordinate(i);
  return x == null ? null : x + (l - i) * ts.options().barSpacing;
}

// x for an epoch-ms time on whatever bars the chart shows (time bars interpolate inside a bar).
function amtTimeX(ts, bars, ms, periodMs) {
  const n = bars.length;
  if (!n) return null;
  let logical;
  if (ms <= bars[0].t) logical = periodMs ? (ms - bars[0].t) / periodMs : 0;
  else if (ms >= bars[n - 1].t) logical = n - 1 + (periodMs ? (ms - bars[n - 1].t) / periodMs : 0);
  else {
    const i = barIndexTo(bars, ms);
    logical = i + (periodMs ? Math.min(1, (ms - bars[i].t) / periodMs) : 0);
  }
  return logicalX(ts, logical);
}

// NinjaTrader stamps a bar with its close, and GetXByTime(t) is the bar that closes at t: a start time lands on
// the bar before, the current bar's end on the current bar. Times with no bar closing there (after the 16:00-17:00
// break) take the first bar at or after them; past the last bar they run on by the bar period.
function amtCloseX(ts, bars, ms, periodMs) {
  if (!periodMs || !bars.length) return amtTimeX(ts, bars, ms, periodMs);
  const open = ms - periodMs, i = barIndexFrom(bars, open);
  if (i >= 0 && bars[i].t === open) return logicalX(ts, i);
  if (i >= 0) return logicalX(ts, barIndexFrom(bars, ms) >= 0 ? barIndexFrom(bars, ms) : i);
  return amtTimeX(ts, bars, open, periodMs);
}

function drawAmt(ctx, size, chart, series, s) {
  const d = s.amt;
  if (!d || !d.ready || !s.bars.length) return;
  const W = size.width, H = size.height, st = d.style || {};
  const ts = chart.timeScale(), spacing = Math.max(1, ts.options().barSpacing);
  const X = (ms) => amtCloseX(ts, s.bars, ms, s.periodMs);
  const XO = (ms) => amtTimeX(ts, s.bars, ms, s.periodMs);   // a bar's open time: the bar itself
  const Y = (p) => series.priceToCoordinate(p);
  const scale = W < 600 ? 0.8 : 1;   // phones only; NinjaTrader's sizes otherwise
  const xEnd = X(d.bar_end);

  // 80% Rule fill: the prior day's value area in each bar's own slot while the rule is active or confirmed
  if (st.show_fill) {
    for (const [s0, e0, vah, val, conf] of [...(d.samples || []), ...(d.fill ? [d.fill] : [])]) {   // fill: the forming bar's
      if (vah == null || val == null) continue;
      const xa0 = XO(s0), xb0 = XO(e0), ya = Y(vah), yb = Y(val);
      const xa = xa0 == null ? null : xa0 - spacing / 2, xb = xb0 == null ? null : xb0 - spacing / 2;
      if (xa == null || xb == null || ya == null || yb == null || xb < 0 || xa > W) continue;
      ctx.fillStyle = argb(conf ? st.fill_conf : st.fill_active);
      ctx.fillRect(xa, Math.min(ya, yb), Math.max(1, xb - xa), Math.abs(yb - ya));
    }
  }
  // zones: daily-level bands (full width) and single prints (from their start to past the last bar)
  for (const [top, bottom, start, color, bc, bw, bs] of d.zones || []) {
    const ya = Y(top), yb = Y(bottom);
    if (ya == null || yb == null || yb < 0 || ya > H) continue;
    const xa = start == null ? 0 : X(start), xb = start == null ? W : X(d.bar_end + 30 * 60000);
    if (xa == null || xb == null) continue;
    const x0 = Math.max(0, xa), w = Math.max(1, Math.min(W, xb) - x0);
    ctx.fillStyle = argb(color);
    ctx.fillRect(x0, ya, w, Math.max(1, yb - ya));
    if (bw > 0) { ctx.strokeStyle = argb(bc); ctx.lineWidth = bw; amtDash(ctx, bs, bw); ctx.strokeRect(x0, ya, w, yb - ya); }
  }
  // volume profiles: past ones end at their period's end; the developing one sits right of the last bar
  const profiles = [...(d.past || []), ...(d.profile ? [d.profile] : [])];
  for (const h of profiles) {
    if (!h || !h.v || !h.v.length) continue;
    let max = 0;
    for (const v of h.v) if (v > max) max = v;
    if (max <= 0) continue;
    const width = (st.profile_width ?? 50) * spacing;
    let anchor = X(h.end);
    if (anchor == null) continue;
    if (h.cur) anchor += ((st.profile_offset ?? 50) - (st.align_right ? 0 : (st.profile_width ?? 50))) * spacing;
    if (anchor + width < 0 || anchor - width > W) continue;
    for (let i = 0; i < h.v.length; i++) {
      if (!h.v[i]) continue;
      const price = h.p0 + i * h.step, ya = Y(price + h.step / 2), yb = Y(price - h.step / 2);
      if (ya == null || yb == null || yb < 0 || ya > H) continue;
      const w = h.v[i] / max * width, va = h.vah != null && price >= h.val && price <= h.vah;
      ctx.fillStyle = va ? withAlpha(st.c_va, st.va_transp) : withAlpha(st.c_out, st.out_transp);
      ctx.fillRect(st.align_right ? anchor - w : anchor, ya, Math.max(1, w), Math.max(1, yb - ya));
    }
  }
  // lines: past days first, then this frame; the chosen top layer (daily levels by default) last
  const pick = st.top_layer, want = AMT_TOP_NAMES[pick] || pick;
  const top = (l) => (pick === 'Daily Levels (All)' ? l[11] === 'Both' : pick === 'Spike Base' ? l[0].startsWith('Spike Base') : l[0] === want);
  const lines = [...(d.history || []), ...(d.lines || []).filter((l) => !top(l)), ...(d.lines || []).filter(top)];
  ctx.textBaseline = 'alphabetic';
  for (const [name, price, start, end, color, textColor, width, style, offset, fsize, label, extend] of lines) {
    let y = Y(price);
    if (y == null || y < -25 || y > H + 25) continue;
    const x1 = start == null || extend === 'Left' || extend === 'Both' ? 0 : X(start);
    const x2 = extend === 'Right' || extend === 'Both' ? W : (X(end) ?? W) + offset * spacing;
    if (width > 0 && x1 != null) {
      const w = Math.max(1, width), yy = w % 2 ? Math.round(y) + 0.5 : Math.round(y);
      ctx.strokeStyle = argb(color); ctx.lineWidth = w; amtDash(ctx, style, w);
      ctx.beginPath(); ctx.moveTo(Math.max(0, x1), yy); ctx.lineTo(Math.min(W, x2), yy); ctx.stroke();
    }
    if (!label || xEnd == null) continue;
    const px = amtFontPx(fsize) * scale;
    const text = `${name}: ${s.fmt(price)}`;
    ctx.font = `${px}px "Segoe UI", ${s.font}`;
    const est = Math.max(80 * scale, text.length * px * 0.56);   // the Toolkit's width estimate for the right-edge clamp
    let lx = name.startsWith('IB ') && st.ib_label_left && x1 != null ? x1 : xEnd + offset * spacing + 4;
    lx = Math.max(2, Math.min(lx, W - est - 4));
    if (extend === 'Both' && name && st.zones) {
      const z = (d.zones || []).find((q) => q[2] == null && Math.abs((q[0] + q[1]) / 2 - price) < s.tick * 0.01);
      if (z) y = Y(price - (z[0] - z[1]) / 4) ?? y;
    }
    ctx.fillStyle = argb(textColor || color);
    ctx.fillText(text, lx, y + 0.579 * px - 2);   // DirectWrite box top at y - size/2 - 2, Segoe UI's ascent
  }
  // RTH VWAP (7. RTH VWAP, off by default): over the lines, one segment per pair of bars within each day's window
  if (st.vwap && d.vwap) {
    const [col, w, sty] = st.vwap, pts = d.vwap_live ? [...d.vwap, d.vwap_live] : d.vwap;
    ctx.strokeStyle = argb(col); ctx.lineWidth = Math.max(1, w); amtDash(ctx, sty, w);
    let prev = null;
    for (const [t, v] of pts) {
      const x = XO(t), y = Y(v);
      if (x == null || y == null) { prev = null; continue; }
      if (prev != null && t - prev[0] <= 3 * 3600e3) { ctx.beginPath(); ctx.moveTo(prev[1], prev[2]); ctx.lineTo(x, y); ctx.stroke(); }
      prev = [t, x, y];
    }
  }
  ctx.setLineDash([]);
}
// A price marker's text: black on a light plot color, white on a dark one, as NinjaTrader picks it.
const amtTagText = (c) => ((((c >>> 16) & 255) * 299 + ((c >>> 8) & 255) * 587 + (c & 255) * 114) / 1000 > 150 ? '#000000' : '#ffffff');
const amtOpaque = (c) => `rgb(${(c >>> 16) & 255}, ${(c >>> 8) & 255}, ${c & 255})`;

// NinjaTrader's price markers: the Toolkit plots these lines' prices on the axis, each plot colored like its line
// (State.Configure sets the plot brushes from the settings).
const AMT_MARKERS = ['Developing POC', 'pmPOC', 'pmVAH', 'pmVAL', 'pmHigh', 'pmLow', 'pwHigh', 'pwLow', 'pdHigh', 'pdLow', 'IB High', 'IB Low'];

class AmtPrimitive {
  constructor(getState) {
    this.getState = getState;
    this.p = null;
    this.views = [{
      zOrder: () => 'top',
      renderer: () => (this.p ? {
        draw: (target) => target.useMediaCoordinateSpace((scope) =>
          drawAmt(scope.context, scope.mediaSize, this.p.chart, this.p.series, this.getState())),
      } : null),
    }];
  }
  attached(p) { this.p = p; }
  detached() { this.p = null; axisStack.drop(this); }
  paneViews() { return this.views; }
  priceAxisViews() {
    const s = this.getState(), d = s && s.amt;
    if (!this.p || !d || !d.ready || !d.style?.price_tags) return axisStack.views(this, []);
    const Y = (p) => this.p.series.priceToCoordinate(p);
    return axisStack.views(this, (d.lines || []).filter((l) => AMT_MARKERS.includes(l[0])).map((l) => ({
      coordinate: () => Y(l[1]) ?? -100, text: () => s.fmt(l[1]), textColor: () => amtTagText(l[4]), backColor: () => amtOpaque(l[4]),
      visible: () => Y(l[1]) != null,
    })));
  }
  update() { if (this.p) this.p.requestUpdate(); }
}

/* ---- Tatanka TPO Profile drawing: a port of the NinjaTrader OnRender (letter boxes for recent days,
   outlines for older ones, POC and value area lines). The server sends the profiles; this only draws them.
   Letters sit one bar apart from the profile's first bar, as NinjaTrader draws them. ---------------------- */
const TPO_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';
const roundEven = (x) => { const r = Math.round(x); return Math.abs(x % 1) === 0.5 && r % 2 ? r - 1 : r; };  // C# Math.Round
const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) || 0);
const rgba = (c, a) => `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${a / 255})`;
const tpoLerp = (a, b, t) => { t = Math.max(0, Math.min(1, t)); return a.map((v, i) => roundEven(v + (b[i] - v) * t)); };

function tpoLine(ctx, x1, x2, y, color, width, dashed) {
  if (x1 == null || x2 == null || y == null) return;
  ctx.beginPath();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.setLineDash(dashed ? [2 * width, 2 * width] : []);
  ctx.moveTo(x1, y);
  ctx.lineTo(x2, y);
  ctx.stroke();
  ctx.setLineDash([]);
}

function drawTpo(ctx, size, chart, series, s) {
  const d = s.tpo;
  if (!d || !d.profiles.length || !s.bars.length) return;
  const W = size.width, H = size.height, o = s.set, col = d.col;
  const ts = chart.timeScale();
  // NinjaTrader's GetXByTime and GetYByValue return whole pixels: boxes abut without seams
  const X = (ms) => { const x = amtTimeX(ts, s.bars, ms, s.periodMs); return x == null ? null : Math.round(x); };
  const Y = (p) => { const y = series.priceToCoordinate(p); return y == null ? null : Math.round(y); };
  const cStart = hexRgb(o.StartColor), cEnd = hexRgb(o.EndColor), cTxt = hexRgb(o.TextColor);
  const poc = rgba(hexRgb(o.PocColor), 255), va = rgba(hexRgb(o.VaColor), 255), cPoly = hexRgb(o.PolyColor);
  const boxAlpha = Math.max(0, Math.min(255, roundEven((100 - o.BoxTransparency) * 2.55)));
  const fills = [];
  ctx.save();
  ctx.font = '10px Arial, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  for (const p of d.profiles) {
    const xs = [];
    const cx = (j) => xs[j] ?? (xs[j] = X(p.x0 + j * col));
    const xL = cx(0), xEnd = X(Math.max(p.poc_end, p.x0 + p.cols * col));
    if (xL == null || (xL > W && xEnd > W) || (xL < 0 && xEnd < 0)) continue;
    const half = p.step / 2;
    if (p.mode === 'P') {
      if (p.rows.length < 2) continue;
      ctx.beginPath();
      for (const [price, count] of p.rows) {
        const x = cx(count);
        ctx.lineTo(x, Y(price - half));
        ctx.lineTo(x, Y(price + half));
      }
      for (let i = p.rows.length - 1; i >= 0; i--) {
        ctx.lineTo(xL, Y(p.rows[i][0] + half));
        ctx.lineTo(xL, Y(p.rows[i][0] - half));
      }
      ctx.closePath();
      ctx.fillStyle = rgba(cPoly, 80);
      ctx.fill();
      ctx.strokeStyle = rgba(cPoly, 180);
      ctx.lineWidth = 1;
      ctx.stroke();
      tpoLine(ctx, xL, X(p.poc_end), Y(p.poc), poc, 1, true);
    } else {
      for (const [price, letters] of p.rows) {
        let yTop = Y(price + half), yBot = Y(price - half);
        if (yTop == null || yBot == null) continue;
        if (yBot < yTop) [yTop, yBot] = [yBot, yTop];
        const h = yBot - yTop;
        if (h < 0.5 || yBot < 0 || yTop > H) continue;
        const isPoc = Math.abs(price - p.poc) < 1e-10;
        for (let j = 0; j < letters.length; j++) {
          let x1 = cx(j), x2 = cx(j + 1);
          if (x2 < x1) [x1, x2] = [x2, x1];
          const w = x2 - x1;
          if (w < 0.5 || x1 > W || x2 < 0) continue;
          const tIdx = letters[j];
          ctx.fillStyle = fills[tIdx] ?? (fills[tIdx] = rgba(tpoLerp(cStart, cEnd, tIdx / 20), boxAlpha));
          ctx.fillRect(x1, yTop, w, h);
          if (isPoc) {
            ctx.strokeStyle = poc;
            ctx.lineWidth = 1.2;
            ctx.strokeRect(x1, yTop, w, h);
          }
          if (w >= 6 && h >= 7) {
            ctx.fillStyle = rgba(cTxt, 230);
            ctx.fillText(TPO_LETTERS[tIdx] ?? '?', x1 + w / 2, yTop + h / 2);
          }
        }
      }
      tpoLine(ctx, xL, X(p.poc_end), Y(p.poc), poc, 2, false);
    }
    if (p.va) {
      const vaEnd = X(p.x0 + p.cols * col);
      tpoLine(ctx, xL, vaEnd, Y(p.va[0]), va, 1, true);
      tpoLine(ctx, xL, vaEnd, Y(p.va[1]), va, 1, true);
    }
  }
  ctx.restore();
}

class TpoPrimitive {
  constructor(getState) {
    this.getState = getState;
    this.p = null;
    this.views = [{
      zOrder: () => 'top',
      renderer: () => (this.p ? {
        draw: (target) => target.useMediaCoordinateSpace((scope) =>
          drawTpo(scope.context, scope.mediaSize, this.p.chart, this.p.series, this.getState())),
      } : null),
    }];
  }
  attached(p) { this.p = p; }
  detached() { this.p = null; axisStack.drop(this); }
  paneViews() { return this.views; }
  update() { if (this.p) this.p.requestUpdate(); }
}

/* ---- Tatanka Gap Analysis: the chart side of the NinjaTrader indicator. The server runs the GapEngine over
   the chart's bars (forming bar included); here the live bar keeps filling gaps between server updates, and
   Snapshot / RenderGap / PaintZone / RenderTable are ported with bar indices replaced by bar open times. -- */
const GAP_COLORS = { white: [245, 245, 245], black: [0, 0, 0], warning: [218, 165, 32] };   // WhiteSmoke, Black, Goldenrod
const gapFilled = (g) => g.fb >= 0;
const gapPercent = (g) => (gapFilled(g) ? 100 : Math.min(100, Math.abs(g.open - g.edge) / Math.abs(g.open - g.settle) * 100));
// .NET "0.##" on .NET Framework: 15 significant digits, then 2 decimals rounded half away from zero.
function fmtPct(x) {
  if (Math.abs(x) >= 1e15) return x.toFixed(0);
  const s = Math.abs(x).toPrecision(15), [i, f = ''] = s.split('.');
  let n = BigInt(i + (f + '00').slice(0, 2));
  if ((f[2] || '0') >= '5') n += 1n;
  const t = n.toString().padStart(3, '0'), out = `${t.slice(0, -2)}.${t.slice(-2)}`.replace(/\.?0+$/, '');
  return (x < 0 && out !== '0' ? '-' : '') + out;
}
function gapEndT(g, curT, extend) {   // GapRecord.DrawingEndBar
  let end = gapFilled(g) ? g.ft : curT;
  if (!extend && g.nt != null) end = Math.min(end, g.nt);
  return Math.max(g.st, end);
}
// The live bar's part of GapEngine.Update: fills of the gaps still open. idx is its bar index on the server.
function gapLiveBar(d, b, idx) {
  for (const g of d.gaps) {
    if (gapFilled(g)) continue;
    g.edge = g.dir > 0 ? Math.max(g.settle, Math.min(g.edge, b.l)) : Math.min(g.settle, Math.max(g.edge, b.h));
    if (g.dir > 0 ? b.l <= g.settle : b.h >= g.settle) { g.edge = g.settle; g.fb = idx; g.ft = b.t; }
  }
  d.curBar = idx;
  d.curT = b.t;
}
function gapSnapshot(d, firstT, lastT, extend, hideFilled, maximumVisible) {   // GapEngine.Snapshot
  const candidates = hideFilled ? d.gaps.filter((g) => !gapFilled(g)) : d.gaps, visible = [];
  for (let i = candidates.length - 1; i >= 0; i--) {
    const g = candidates[i];
    if (g.st > lastT) continue;
    if (!((extend && !gapFilled(g)) || gapEndT(g, d.curT, extend) >= firstT)) continue;
    if (maximumVisible > 0 && visible.length >= maximumVisible) break;
    visible.push(g);
  }
  return visible.reverse();   // Newest zones are painted last.
}

function drawGaps(ctx, size, chart, series, s) {
  const W = size.width, H = size.height, o = s.set;
  if (W < 40 || H < 40) return;
  const k = W < 600 ? 0.8 : 1, labelPx = o.LabelFontSize * k, tablePx = o.TableFontSize * k;   // phones: 80% text
  const font = (px) => `${px}px "Segoe UI", system-ui, sans-serif`;
  const legend = s.legend || { right: 0, bottom: 0 };   // the symbol/OHLC legend over the top left
  if (s.error) {
    ctx.save();
    ctx.font = font(tablePx);
    ctx.fillStyle = rgba(GAP_COLORS.warning, 255);
    ctx.textBaseline = 'top';
    `Tatanka Gap Analysis\n${s.error}`.split('\n').forEach((line, i) => ctx.fillText(line, 12, legend.bottom + 4 + i * (tablePx + 5)));
    ctx.restore();
    return;
  }
  const d = s.gaps, bars = s.bars;
  if (!d || !bars.length) return;
  const ts = chart.timeScale(), range = ts.getVisibleLogicalRange();
  if (!range) return;
  const fromI = Math.max(0, Math.round(range.from)), toI = Math.min(bars.length - 1, Math.round(range.to));
  const firstT = bars[fromI].t, lastT = bars[toI].t;
  const visible = gapSnapshot(d, firstT, lastT, o.ExtendUnfilledRight, o.HideFilledGaps, o.MaximumGapsToShow);
  const X = (ms) => amtTimeX(ts, bars, ms, s.periodMs);
  const Y = (p) => series.priceToCoordinate(p);
  const half = Math.max(1, ts.options().barSpacing * 0.8 / 2);
  const barLeft = (t) => (t < firstT ? 0 : Math.max(0, X(t) - half));
  const barRight = (t) => (t > lastT ? W : Math.min(W, X(t) + half));
  const bull = hexRgb(o.BullGapBrush), bear = hexRgb(o.BearGapBrush), filled = hexRgb(o.FilledGapBrush);
  const col = (c, a) => `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${a})`;
  const stored = d.gaps.length;
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, W, H);
  ctx.clip();

  function paintZone(left, right, top, bottom, c, opacity, outline) {
    if (top <= bottom || right <= left) return;
    const y1 = Y(top), y2 = Y(bottom);
    if (y1 == null || y2 == null) return;
    const high = Math.max(0, Math.min(y1, y2)), low = Math.min(H, Math.max(y1, y2));
    if (low <= high) return;
    ctx.fillStyle = col(c, opacity);
    ctx.fillRect(left, high, right - left, low - high);
    if (outline) {
      ctx.strokeStyle = col(c, Math.min(0.7, opacity + 0.25));
      ctx.lineWidth = 1;
      ctx.strokeRect(left, high, right - left, low - high);
    }
  }

  if (o.ShowGapZones || o.ShowSettleLines || o.ShowGapLabels) {
    for (const g of visible) {
      const left = barLeft(g.st);
      const extendsRight = o.ExtendUnfilledRight && !gapFilled(g);
      const right = extendsRight ? W : barRight(gapEndT(g, d.curT, o.ExtendUnfilledRight));
      if (right <= left) continue;
      const direction = g.dir > 0 ? bull : bear, active = gapFilled(g) ? filled : direction;
      const fade = Math.max(0.4, 1 - Math.min(stored - 1 - g.n, 4) * 0.15);
      const top = Math.max(g.open, g.settle), bottom = Math.min(g.open, g.settle), pct = gapPercent(g);
      if (o.ShowGapZones) {
        if (gapFilled(g) || !o.ShrinkPartiallyFilledZones) {
          paintZone(left, right, top, bottom, active, (gapFilled(g) ? o.FilledOpacity : o.GapOpacity) / 100 * fade, true);
        } else {
          if (o.ShowTouchedArea && pct > 0) {
            const touchedRight = extendsRight ? barRight(d.curT) : right;
            paintZone(left, touchedRight, g.dir > 0 ? top : g.edge, g.dir > 0 ? g.edge : bottom, filled, o.FilledOpacity / 100 * fade, false);
          }
          paintZone(left, right, g.dir > 0 ? g.edge : top, g.dir > 0 ? bottom : g.edge, direction, o.GapOpacity / 100 * fade, true);
        }
      }
      const settleY = Y(g.settle);
      if (o.ShowSettleLines && settleY != null && settleY >= 0 && settleY <= H) {
        const w = o.SettleLineWidth;
        ctx.strokeStyle = col(active, 0.75 * fade);
        ctx.lineWidth = w;
        ctx.setLineDash(o.SettleLineStyle === 'Solid' ? [] : o.SettleLineStyle === 'Dotted' ? [w, 2 * w] : [2 * w, 2 * w]);
        ctx.beginPath();
        ctx.moveTo(left, settleY);
        ctx.lineTo(right, settleY);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      // Labels stay at the gap origin; old extended zones do not pile labels at the left edge.
      if (o.ShowGapLabels && g.st >= firstT && g.st <= lastT) {
        const anchorY = Y(g.open);
        if (anchorY != null && anchorY >= 0 && anchorY <= H) {
          const lines = [`${g.dir > 0 ? 'GAP UP' : 'GAP DOWN'}  ${s.fmt(Math.abs(g.open - g.settle))} pts`,
            gapFilled(g) ? 'FILLED' : `${fmtPct(pct)}% FILLED`];
          ctx.font = font(labelPx);
          const lh = Math.round(labelPx * 1.33), lw = Math.max(...lines.map((t) => ctx.measureText(t).width)), lhAll = lh * lines.length;
          let y = g.dir > 0 ? anchorY - lhAll - 5 : anchorY + 5;
          y = Math.max(0, Math.min(H - lhAll - 4, y));
          const x = Math.max(0, Math.min(left, W - lw - 8));
          ctx.fillStyle = col(GAP_COLORS.black, 0.7);
          ctx.fillRect(x, y, lw + 8, lhAll + 4);
          ctx.fillStyle = col(active, 1);
          ctx.textBaseline = 'alphabetic';   // DirectWrite: line top at y, Segoe UI's baseline 1.079 em below
          lines.forEach((t, i) => ctx.fillText(t, x + 4, y + 2 + i * lh + 1.079 * labelPx));
        }
      }
    }
  }

  if (o.ShowGapTable) {   // RenderTable
    const latest = d.gaps.at(-1), keys = ['TATANKA', 'HISTORY'], values = ['GAP ANALYSIS', `${stored} stored / ${visible.length} in view`];
    if (W < 520) {   // a phone: the latest gap in two rows instead of the whole table
      keys.length = values.length = 0;
      if (!latest) { keys.push('GAP'); values.push('none yet'); }
      else {
        keys.push(`GAP ${latest.dir > 0 ? 'UP' : 'DOWN'}`, 'STATUS');
        values.push(`${s.fmt(Math.abs(latest.open - latest.settle))} pts`, gapFilled(latest) ? `FILLED · ${latest.fb - latest.sb} bars` : `${fmtPct(gapPercent(latest))}% filled`);
      }
    } else if (!latest) { keys.push('STATUS'); values.push('Waiting for a qualifying RTH gap'); }
    else {
      keys.push('GAP', 'SIZE', 'STATUS', 'RTH CLOSE', 'BARS', 'CONSECUTIVE', 'EXHAUSTION');
      values.push(latest.dir > 0 ? 'UP' : 'DOWN', `${s.fmt(Math.abs(latest.open - latest.settle))} pts`,
        gapFilled(latest) ? 'FILLED' : `${fmtPct(gapPercent(latest))}% FILLED`, s.fmt(latest.settle),
        gapFilled(latest) ? `${latest.fb - latest.sb} to fill` : `${d.curBar - latest.sb} elapsed`,
        `${d.consecutive}${latest.dir > 0 ? ' bull' : ' bear'}`, d.consecutive >= 3 ? 'ELEVATED' : '---');
    }
    ctx.font = font(tablePx);
    const keyW = Math.max(...keys.map((k) => ctx.measureText(k).width)), valW = Math.max(...values.map((v) => ctx.measureText(v).width));
    const width = Math.min(W - 16, keyW + valW + 36), rowH = tablePx + 9, height = rowH * keys.length + 12;
    const pos = o.TablePosition, onLeft = pos.endsWith('Left'), onBottom = pos.startsWith('Bottom'), inMiddle = pos.startsWith('Middle');
    const x = onLeft ? 8 : W - width - 8;
    let y = onBottom ? H - height - 8 : inMiddle ? (H - height) / 2 : 20;   // where NinjaTrader puts it
    y = Math.max(2, y);
    ctx.fillStyle = col(GAP_COLORS.black, 0.83);
    ctx.fillRect(x, y, width, height);
    ctx.strokeStyle = col(filled, 0.6);
    ctx.lineWidth = 1;
    ctx.strokeRect(x, y, width, height);
    ctx.textBaseline = 'alphabetic';
    keys.forEach((k, row) => {
      const rowY = y + 6 + row * rowH + 1.079 * tablePx;
      let c = GAP_COLORS.white;
      if (latest && k.startsWith('GAP')) c = latest.dir > 0 ? bull : bear;
      if (latest && k === 'STATUS') c = gapFilled(latest) ? bull : gapPercent(latest) > 50 ? GAP_COLORS.warning : GAP_COLORS.white;
      if (k === 'EXHAUSTION' && d.consecutive >= 3) c = GAP_COLORS.warning;
      ctx.textAlign = 'left';
      ctx.fillStyle = col(GAP_COLORS.white, 1);
      ctx.fillText(k, x + 10, rowY);
      ctx.textAlign = 'right';
      ctx.fillStyle = col(c, 1);
      ctx.fillText(values[row], x + width - 10, rowY);
    });
  }
  ctx.restore();
}

class GapPrimitive {
  constructor(getState) {
    this.getState = getState;
    this.p = null;
    this.views = [{
      zOrder: () => 'top',
      renderer: () => (this.p ? {
        draw: (target) => target.useMediaCoordinateSpace((scope) => {
          const s = this.getState();
          if (s) drawGaps(scope.context, scope.mediaSize, this.p.chart, this.p.series, s);
        }),
      } : null),
    }];
  }
  attached(p) { this.p = p; }
  detached() { this.p = null; axisStack.drop(this); }
  paneViews() { return this.views; }
  update() { if (this.p) this.p.requestUpdate(); }
}

/* ---- Tatanka 1hr 21 EMA (HTF EMA): the NinjaTrader OnBarUpdate, run over the chart's bars. The server sends
   NinjaTrader's EMA of each closed higher-timeframe bar (d.htf: [close ms, EMA or null until EmaLength bars])
   and, for time charts, the chart EMA up to the first bar (d.smooth). A chart bar sees the HTF bars that
   closed before it did; on a tie NinjaTrader runs the chart bar first. cal: 2D, 3D, week or month bars.
   after: when the bar after the last one opens, where known (bar replay's next bar; d.next once a calendar bar
   is over); without it the last bar is still forming. ------------------------------------------------------ */
function htfEmaValues(bars, d, periodMs, cal, after) {
  const out = new Array(bars.length);
  const L = d.scaled, constant1 = 2.0 / (1 + L), constant2 = 1 - 2.0 / (1 + L), alpha = 2.0 / (d.len + 1.0);
  let n = d.smooth ? d.smooth.count : 0, v = d.smooth?.seed ?? 0;
  let h = 0, e = 0, htfSeen = false, htfEmaReady = false, lastClosedHtfEma = NaN;
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i], next = i + 1 < bars.length ? bars[i + 1].t : after > b.t ? after : null;
    // NinjaTrader's bar time is its close: time bars one period on, cut at the 16:00 close; tick, volume and
    // range bars when the next bar opens (approximate: their last trade time is not kept).
    let close;
    if (cal) {
      // A bar of several sessions closes with its last one, as does that session's last HTF bar: the last HTF bar
      // to close before the next chart bar opens (it runs after this bar, as on a tie). The forming bar: all of them.
      close = Infinity;
      if (next != null) {
        let j = h;
        while (j < d.htf.length && d.htf[j][0] <= next) j++;
        close = j > h ? d.htf[j - 1][0] : next;
      }
    } else if (periodMs) {
      close = b.t + periodMs;
      while (e < d.ends.length && d.ends[e] <= b.t) e++;
      if (e < d.ends.length) close = Math.min(close, d.ends[e]);
    } else close = next ?? Infinity;
    while (h < d.htf.length && d.htf[h][0] < close) {   // BarsInProgress == 1
      htfSeen = true;
      if (d.htf[h][1] != null) { lastClosedHtfEma = d.htf[h][1]; htfEmaReady = true; }
      h++;
    }
    v = n === 0 ? b.c : b.c * constant1 + constant2 * v;   // smoothEmaInd, EMA(Closes[0], scaledLen)
    const currentBar = n++;
    let plotVal = NaN;
    if (htfSeen) {                                        // CurrentBars[1] >= 0
      if (d.mode === 'ExactHTF') { if (htfEmaReady) plotVal = lastClosedHtfEma; }
      else if (d.mode === 'SmoothScaled') { if (currentBar >= L - 1) plotVal = v; }
      else if (htfEmaReady) plotVal = alpha * b.c + (1.0 - alpha) * lastClosedHtfEma;
    }
    out[i] = plotVal;
  }
  return out;
}

/* ---- Tatanka Daily SMA x4: the NinjaTrader OnBarUpdate run over the chart's bars. The server sends the daily bars
   (d.days: [open ms, close ms, close], the last one still forming while d.forming). NinjaTrader runs the chart bars
   and the daily bars in time order, each at its close (on a tie the chart bar first, then the daily bar refreshes
   it), so a chart bar shows the SMAs as of the last daily bar that closed by its own close. A bar that took live
   ticks keeps what it showed then (o.live: bar time -> values), as NinjaTrader's realtime bars do; the forming bar
   shows the developing day's, its close the live price. As Pine's ta.sma: nothing until a full window. -------- */
function dailySmaPrep(days, periods) {
  // NinjaTrader's SMA as a running sum: priorSum + Input[0] - Input[Period] (once CurrentBar >= Period)
  return periods.map((P) => {
    const sums = new Float64Array(days.length), vals = new Float64Array(days.length);
    let sum = 0;
    for (let j = 0; j < days.length; j++) {
      sum = sum + days[j][2] - (j >= P ? days[j - P][2] : 0);
      sums[j] = sum;
      vals[j] = j < P - 1 ? NaN : sum / (j < P ? j + 1 : P);   // Values[i].Reset() while CurrentBars < period - 1
    }
    return { P, sums, vals };
  });
}
// Day j's four SMAs; with `close`, as of that close (the developing day's live price).
function dailySmaAt(prep, days, j, close) {
  return prep.map(({ P, sums, vals }) => {
    if (j < P - 1) return NaN;
    if (close == null) return vals[j];
    return ((j ? sums[j - 1] : 0) + close - (j >= P ? days[j - P][2] : 0)) / (j < P ? j + 1 : P);
  });
}
// o: cal (calendar bars: D, W, M), periodMs (time bars; 0 for tick, volume and range bars), live, price (the live
// price), until (bar replay: its clock, the day it is in forming). Returns the four plots' values, NaN where none;
// developing: the last bar's are the forming day's.
function dailySmaValues(bars, d, prep, o) {
  const days = d.days, n = bars.length, out = prep.map(() => new Array(n).fill(NaN));
  let L = d.forming ? days.length - 1 : -1;   // the forming day
  if (o.until != null) { L = days.findIndex((x) => x[1] > o.until); if (L >= 0 && days[L][0] > o.until) L = -1; }
  const forming = L >= 0;
  let k = 0, e = 0, seenK = -1, seen = null;   // bars of one day share its values: worked out once
  for (let i = 0; i < n; i++) {
    const b = bars[i], next = i + 1 < n ? bars[i + 1].t : Infinity;
    // NinjaTrader's bar time is its close: a calendar bar's when the next one opens; a time bar's one period on,
    // cut at the day's close; a tick, volume or range bar's at its last trade, before the next bar and the close.
    let close = next;
    if (!o.cal) {
      while (e < days.length && days[e][1] <= b.t) e++;
      const end = e < days.length ? days[e][1] : Infinity;
      close = o.periodMs ? Math.min(b.t + o.periodMs, end) : Math.min(next, end) - 1;
    }
    if (i === n - 1 && o.until != null) close = Math.min(close, o.until);
    while (k < days.length && days[k][1] <= close) k++;   // the daily bars run before this bar's last update
    // The live price is the forming day's close only for a bar inside that day: after its close (16:00) the data
    // is stale until the next day's arrives, and a bar of the next session must not move the day before.
    const inDay = forming && b.t < days[L][1];
    let v = null;
    out.developing = i === n - 1 && inDay && (o.cal || b.t >= days[L][0]);
    if (out.developing) v = dailySmaAt(prep, days, L, o.price);
    else if (!(v = o.live?.get(b.t)) && k) {
      if (k - 1 === L && inDay) v = dailySmaAt(prep, days, k - 1, o.price);
      else { if (seenK !== k) { seenK = k; seen = dailySmaAt(prep, days, k - 1, null); } v = seen; }
    }
    if (v) for (let p = 0; p < v.length; p++) out[p][i] = v[p];
  }
  return out;
}
// OnRender: "{period} SMA - {price}" in each plot's color, LabelOffset bar spaces right of the last bar, centred on
// its price; only while the last bar is on screen. st: {x (the last bar's), spacing, vals, periods, colors, s, fmt}.
function drawDailySmaLabels(ctx, size, series, st) {
  const s = st.s, fontSize = s.LabelSize === 'Tiny' ? 10 : s.LabelSize === 'Small' ? 12 : s.LabelSize === 'Large' ? 18 : 14;
  const x = st.x + s.LabelOffset * st.spacing;
  if (x >= size.width) return;
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, size.width, size.height);
  ctx.clip();
  ctx.font = `${fontSize}px Arial, sans-serif`;
  ctx.textBaseline = 'alphabetic';
  const h = fontSize * 1.15, asc = 0.905 * fontSize;   // DirectWrite's line box for Arial; its baseline below the top
  st.vals.forEach((price, i) => {
    if (!Number.isFinite(price)) return;
    const y = series.priceToCoordinate(price);
    if (y == null) return;
    ctx.fillStyle = st.colors[i];
    ctx.fillText(`${st.periods[i]} SMA - ${st.fmt(price)}`, x, y - h / 2 + asc);
  });
  ctx.restore();
}

/* ---- Tatanka If/Then Bias Table: TatankaBiasLogic and TatankaBiasPainter from the NinjaTrader source, run on
   every tick. Rows are BIAS|LO|HI|TEXT; the server keeps each symbol's block and the previous day's ATR. ---- */
const BIAS_NUMBER = /^[\t\n\v\f\r ]*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?[\t\n\v\f\r ]*$/;   // NumberStyles.Float
function biasNumber(text) {
  const t = text.replace(/ /g, '');
  if (/^[+-]?\d+'\d/.test(t)) { const v = parse32(t); return Number.isFinite(v) ? v : null; }   // 32nds: 110'165
  if (!BIAS_NUMBER.test(t)) return null;
  const v = Number(t);
  return Number.isFinite(v) ? v : null;
}
function biasParse(block) {
  const result = [];
  // Normalize Windows line endings; do not trim bias/text or sort levels.
  for (const line of (block ?? '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n')) {
    if (line.length === 0 || line[0] === '#') continue;
    const parts = line.split('|');
    if (parts.length < 4) continue;
    const lo = biasNumber(parts[1]), hi = biasNumber(parts[2]);
    if (lo == null || hi == null) continue;
    const c = parts[0].length ? parts[0][0] : '', u = c.toUpperCase();   // ToUpperInvariant keeps one char
    result.push({ Bias: c ? (u.length === 1 ? u : c) : 'N', Low: lo, High: hi, Text: parts.slice(3).join('|').replaceAll('\\n', '\n') });
  }
  return result;
}
function biasActiveRow(rows, price) {
  let active = -1, best = 1e18;
  for (let i = 0; i < rows.length; i++) {
    const distance = price > rows[i].High ? price - rows[i].High : price < rows[i].Low ? rows[i].Low - price : 0;
    if (distance < best) { best = distance; active = i; }
  }
  return active;
}
function biasFormat(value) {   // Math.Round(value, 2, AwayFromZero).ToString("0.##")
  if (!Number.isFinite(value)) return 'NaN';
  const p = value * 100;
  let w = Math.trunc(p);
  if (Math.abs(p - w) >= 0.5) w += Math.sign(p - w);
  return fmtPct(w / 100);
}
const biasRange = (row, f = biasFormat) => (row.High > 1000000 ? `${f(row.Low)}+` : row.Low <= 0 ? `<${f(row.High)}`
  : `${f(row.Low)}–${f(row.High)}`);
function biasWrap(text, maxChars) {
  // Intentional translation of the source's empty-line/space behavior.
  let output = '';
  for (const segment of text.replaceAll('\\n', '\n').split('\n')) {
    let segmentOutput = '';
    if (maxChars <= 0) segmentOutput = segment;
    else {
      let current = '';
      for (const word of segment.split(' ')) {
        if (current.length === 0) current = word;
        else if (current.length + word.length + 1 <= maxChars) current += ` ${word}`;
        else { segmentOutput += (segmentOutput.length > 0 ? '\n' : '') + current; current = word; }
      }
      segmentOutput += (segmentOutput.length > 0 ? '\n' : '') + current;
    }
    output += (output.length > 0 ? '\n' : '') + segmentOutput;
  }
  return output;
}
const BIAS_COLORS = { U: [99, 153, 34], V: [186, 117, 23], D: [168, 68, 64], P: [235, 208, 52] };
const biasColor = (bias) => [...(BIAS_COLORS[bias] || [136, 135, 128]), 1];
const biasAlpha = (c, opacity) => [c[0], c[1], c[2], opacity];
function biasLevelColor(row, price, atr, s) {
  if (row.Bias === 'P' || s.LineColorMode === 'Bias') return biasColor(row.Bias);
  const buffer = atr * s.AtrPercent / 100.0;
  // NaN comparisons are false, reproducing Pine's neutral fallback.
  return row.Low > price + buffer ? s.Above : row.Low < price - buffer ? s.Below : s.Within;
}
const css4 = (c) => `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${c[3]})`;

function drawIfThen(ctx, size, chart, series, st) {   // returns where its labels and table are, for taps
  const f = st.frame, s = st.style, hits = [], bfmt = s.fmt || biasFormat;   // Treasuries: 32nds
  if (!f) return hits;
  const W = size.width, H = size.height;
  const fontSize = s.TextSize === 'Tiny' ? 10 : s.TextSize === 'Small' ? 12 : s.TextSize === 'Large' ? 20 : 14;
  const lh = fontSize * 1.15;   // Arial's line height
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, W, H);
  ctx.clip();
  ctx.font = `${fontSize}px Arial, sans-serif`;
  ctx.textBaseline = 'alphabetic';
  const asc = 0.905 * fontSize;   // DirectWrite lays a line from its top; Arial's baseline is 0.905 em below
  const layout = (text) => {
    const lines = (text ?? '').split('\n');
    return { lines, width: Math.max(...lines.map((l) => ctx.measureText(l).width)), height: lines.length * lh };
  };
  // A phone-width chart: the paragraphs would cover it, so labels are one line each (… where cut) and the table is
  // its header and the active row, wrapped to fit. st.full (a tap on a label or the table): every word, wrapped to fit.
  // The Plan tab has the whole block.
  const narrow = W < 520, whole = narrow && st.full;
  const oneLine = (text, max) => {
    let t = (text ?? '').replace(/\s+/g, ' ').trim();
    if (ctx.measureText(t).width <= max) return t;
    while (t.length > 1 && ctx.measureText(`${t}…`).width > max) t = t.slice(0, -1);
    return `${t.trimEnd()}…`;
  };
  const wrapTo = (text, max, maxLines) => {
    const out = [];
    let line = '';
    for (const w of (text ?? '').replace(/\s+/g, ' ').trim().split(' ')) {
      const t = line ? `${line} ${w}` : w;
      if (line && ctx.measureText(t).width > max) { out.push(line); line = w; } else line = t;
    }
    if (line) out.push(line);
    if (out.length > maxLines) { out.length = maxLines; out[maxLines - 1] = oneLine(`${out[maxLines - 1]} …`, max); }
    return out.join('\n');
  };
  const y = (p) => series.priceToCoordinate(p);
  if (s.ShowLines || s.LineLabels || s.ShowZones) {
    // Pine garbage-collects the oldest shapes beyond its max_*_count = 100.
    let skip = Math.max(0, f.rows.filter((r) => r.Low > 0).length - 100);
    const levels = [];
    f.rows.forEach((r, i) => { if (r.Low > 0) { if (skip > 0) skip--; else levels.push(i); } });
    const band = Number.isFinite(f.atr) ? f.atr * s.ZonePercent / 100.0 : 0;
    if (s.ShowZones && band > 0) {
      for (const i of levels) {
        const a = y(f.rows[i].Low + band), b = y(f.rows[i].Low - band);
        if (a == null || b == null) continue;
        const top = Math.max(0, Math.min(a, b)), bottom = Math.min(H, Math.max(a, b));
        if (bottom <= top) continue;
        const transparency = i === f.active ? s.ZoneOpacity : Math.min(100, s.ZoneOpacity + 8);
        ctx.fillStyle = css4(biasAlpha(biasLevelColor(f.rows[i], f.price, f.atr, s), (100 - transparency) / 100.0));
        ctx.fillRect(0, top, W, bottom - top);
      }
    }
    if (s.ShowLines) {
      const w = s.LineWidth;
      ctx.lineWidth = w;
      ctx.lineCap = s.LineStyle === 'Dotted' ? 'round' : 'butt';
      ctx.setLineDash(s.LineStyle === 'Solid' ? [] : s.LineStyle === 'Dotted' ? [0, 2 * w] : [2 * w, 2 * w]);
      for (const i of levels) {
        const lineY = y(f.rows[i].Low);
        if (lineY == null || lineY < 0 || lineY > H) continue;
        ctx.strokeStyle = css4(biasAlpha(biasLevelColor(f.rows[i], f.price, f.atr, s), i === f.active ? 1 : 0.55));
        ctx.beginPath();
        ctx.moveTo(0, lineY);
        ctx.lineTo(W, lineY);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.lineCap = 'butt';
    }
    if (s.LineLabels) {
      ctx.textAlign = 'right';
      const lx = W - 4;   // at the chart's right edge: they stay put as you zoom or scroll (NinjaTrader: bars after the last)
      for (const i of levels) {
        const row = f.rows[i], ly = y(row.Low);
        if (ly == null) continue;
        const full = s.LabelContent === 'Price' ? bfmt(row.Low)
          : (s.LabelContent === 'Price + Text' ? `${bfmt(row.Low)} - ` : '') + (narrow ? row.Text : biasWrap(row.Text, s.WrapCharacters));
        const text = whole ? wrapTo(full, Math.min(W * 0.75, 320), 99) : narrow ? oneLine(full, Math.min(W * 0.6, 260)) : full;
        const t = layout(text), width = Math.max(1, t.width), top = ly - t.height - 4;
        const px = lx - width - 8, py = top - 2, pw = width + 8, ph = t.height + 4;
        if (px + pw < 0 || px > W || py + ph < 0 || py > H) continue;
        if (s.LabelBackground) { ctx.fillStyle = css4(s.Background); ctx.fillRect(px, py, pw, ph); }
        hits.push([px, py, pw, ph]);
        ctx.fillStyle = css4(biasAlpha(biasLevelColor(row, f.price, f.atr, s), i === f.active ? 1 : 0.70));
        t.lines.forEach((l, k) => ctx.fillText(l, lx - 3, top + k * lh + asc));
      }
    }
  }
  if (s.ShowTable) {
    const shown = narrow ? (f.active >= 0 ? [f.active] : []) : f.rows.map((_, i) => i);   // rows of f.rows, in order
    const count = shown.length + 1, cells = [], widths = [0, 0, 0], heights = [];
    const at = (row) => shown[row - 1];   // a table row's index in f.rows
    for (let row = 0; row < count; row++) {
      const text = row === 0 ? ['', bfmt(f.price), `${s.InstrumentLabel}  if / then`]
        : [at(row) === f.active ? '►' : '', biasRange(f.rows[at(row)], bfmt), narrow ? wrapTo(f.rows[at(row)].Text, W * 0.55, whole ? 99 : 4) : f.rows[at(row)].Text];
      cells.push(text.map(layout));
      heights.push(Math.max(...cells[row].map((c) => c.height + 8)));
      cells[row].forEach((c, col) => { widths[col] = Math.max(widths[col], c.width + 16); });
    }
    const totalW = widths[0] + widths[1] + widths[2], totalH = heights.reduce((a, b) => a + b, 0);
    const left = s.TablePosition.endsWith('Right') ? W - totalW : 0;
    const top = s.TablePosition.startsWith('Top') ? 0 : s.TablePosition.startsWith('Bottom') ? H - totalH : (H - totalH) / 2;
    const white = [255, 255, 255, 1];
    let cellY = top;
    for (let row = 0; row < count; row++) {
      const active = row > 0 && at(row) === f.active;
      const bias = row === 0 ? (f.active >= 0 ? biasColor(f.rows[f.active].Bias) : [128, 128, 128, 1]) : biasColor(f.rows[at(row)].Bias);
      const background = active ? biasAlpha(bias, s.ActiveRowOpacity / 100.0) : s.Background;
      let cellX = left;
      for (let col = 0; col < 3; col++) {
        ctx.fillStyle = css4(row === 0 && col === 1 ? bias : background);
        ctx.fillRect(cellX, cellY, widths[col], heights[row]);
        const c = cells[row][col], available = Math.max(1, widths[col] - 16);
        const align = col === 0 || (row === 0 && col === 1) ? 'center' : col === 1 ? 'right' : 'left';
        ctx.textAlign = align;
        const tx = cellX + 8 + (align === 'center' ? available / 2 : align === 'right' ? available : 0);
        ctx.fillStyle = css4(row === 0 ? white : col < 2 ? bias : active ? white : [192, 192, 192, 0.70]);
        c.lines.forEach((l, k) => ctx.fillText(l, tx, cellY + (heights[row] - c.height) / 2 + k * lh + asc));
        cellX += widths[col];
      }
      cellY += heights[row];
    }
    // Draw shared edges once so transparent borders are not doubled.
    ctx.strokeStyle = css4(s.Border);
    ctx.lineWidth = 1;
    ctx.strokeRect(left, top, totalW, totalH);
    ctx.beginPath();
    let x = left;
    for (let col = 0; col < 2; col++) { x += widths[col]; ctx.moveTo(x, top); ctx.lineTo(x, top + totalH); }
    let edgeY = top;
    for (let row = 0; row < count - 1; row++) { edgeY += heights[row]; ctx.moveTo(left, edgeY); ctx.lineTo(left + totalW, edgeY); }
    ctx.stroke();
    hits.push([left, top, totalW, totalH]);
  }
  ctx.restore();
  return narrow ? hits : [];   // taps only matter where text is cut
}

class IfThenPrimitive {
  constructor(getState) {
    this.getState = getState;
    this.p = null;
    this.views = [{
      zOrder: () => 'top',
      renderer: () => (this.p ? {
        draw: (target) => target.useMediaCoordinateSpace((scope) => {
          const st = this.getState();
          this.hits = st ? drawIfThen(scope.context, scope.mediaSize, this.p.chart, this.p.series, st) : [];
        }),
      } : null),
    }];
  }
  attached(p) { this.p = p; }
  detached() { this.p = null; axisStack.drop(this); }
  paneViews() { return this.views; }
  update() { if (this.p) this.p.requestUpdate(); }
  hit(x, y) { return (this.hits || []).some(([l, t, w, h]) => x >= l && x <= l + w && y >= t && y <= t + h); }
}

/* ---- Footprint: each bar's bid x ask volume per price (or delta, or volume), NinjaTrader style. The server
   splits stored trades by aggressor; this draws them inside each bar's slot, with a thin candle on its left.
   Zoomed out (or with no trades stored), it draws plain candles, as the candle series is hidden meanwhile. ---- */
const FP_MIN_SPACING = 36;
const fpShort = (v) => (Math.abs(v) >= 10000 ? `${(v / 1000).toFixed(Math.abs(v) >= 100000 ? 0 : 1)}k` : String(v));

function fpCandle(ctx, x, w, b, color, Y) {
  const yh = Y(b.h), yl = Y(b.l), yo = Y(b.o), yc = Y(b.c);
  if ([yh, yl, yo, yc].includes(null)) return;
  ctx.fillStyle = color;
  ctx.fillRect(Math.round(x) - 0.5, yh, 1, Math.max(1, yl - yh));
  ctx.fillRect(x - w / 2, Math.min(yo, yc), w, Math.max(1, Math.abs(yc - yo)));
}

function drawFootprint(ctx, size, chart, series, s) {
  const ts = chart.timeScale(), range = ts.getVisibleLogicalRange(), bars = s.bars;
  if (!range || !bars.length) return;
  const W = size.width, sp = ts.options().barSpacing, Y = (p) => series.priceToCoordinate(p);
  const i0 = Math.max(0, Math.floor(range.from) - 1), i1 = Math.min(bars.length - 1, Math.ceil(range.to) + 1);
  const ref = bars[i1].c, pxTick = Math.abs((Y(ref) ?? 0) - (Y(ref + s.tick) ?? 0)) || 1e-6;
  const k = s.rows === 'auto' ? ([1, 2, 4, 5, 8, 10, 20, 25, 50, 100, 200, 500].find((n) => n * pxTick >= 13) || 1000) : s.rows;
  const rowH = k * pxTick, cells = sp >= FP_MIN_SPACING && rowH >= 5;
  const fs = Math.min(11, Math.floor(rowH - 2)), font = `${fs}px ${NUM_FONT}`;
  const bold = `600 ${fs}px ${NUM_FONT}`;
  ctx.save();
  for (let i = i0; i <= i1; i++) {
    const b = bars[i], x = logicalX(ts, i);
    if (x == null || x < -sp || x > W + sp) continue;
    const color = b.c >= b.o ? s.up : s.down, fp = cells ? s.fp.get(b.seq) : null;
    if (!fp || !fp.rows.length) { fpCandle(ctx, x, Math.max(1, Math.floor(sp * 0.8)), b, color, Y); continue; }
    const left = x - sp / 2 + 1, cx0 = left + 8, cw = x + sp / 2 - 1 - cx0;
    fpCandle(ctx, left + 3, 3, b, color, Y);
    const groups = new Map();
    for (const [pi, bid, ask] of fp.rows) {
      const g = Math.floor(pi / k), e = groups.get(g);
      if (e) { e[0] += bid; e[1] += ask; } else groups.set(g, [bid, ask]);
    }
    let maxVol = 0, maxDelta = 1, poc = null, tv = 0, td = 0;
    for (const [g, [bid, ask]] of groups) {
      const v = bid + ask;
      tv += v; td += ask - bid;
      if (v > maxVol) { maxVol = v; poc = g; }
      maxDelta = Math.max(maxDelta, Math.abs(ask - bid));
    }
    const text = fs >= 7 && cw >= 30;
    for (const [g, [bid, ask]] of groups) {
      const yTop = Y((g * k + k - 0.5) * s.tick), yBot = Y((g * k - 0.5) * s.tick);
      if (yTop == null || yBot == null) continue;
      const h = yBot - yTop, mid = yTop + h / 2, v = bid + ask, delta = ask - bid;
      ctx.fillStyle = s.mode === 'volume' ? `rgba(139, 150, 165, ${0.08 + 0.42 * v / maxVol})`
        : `${delta >= 0 ? s.upRgb : s.downRgb}${(0.08 + 0.5 * Math.abs(delta) / maxDelta).toFixed(3)})`;
      ctx.fillRect(cx0, yTop + 0.5, cw, h - 1);
      if (g === poc && s.poc) { ctx.strokeStyle = '#ffd600'; ctx.lineWidth = 1; ctx.strokeRect(cx0 + 0.5, yTop + 0.5, cw - 1, h - 1); }
      if (!text) continue;
      ctx.textBaseline = 'middle';
      if (s.mode === 'bidask') {
        // diagonal imbalances: buyers at this price against sellers one row down, and the reverse
        const below = groups.get(g - 1), above = groups.get(g + 1);
        const buyImb = !!below && ask > 0 && ask >= s.minVol && ask >= s.ratio * below[0];
        const sellImb = !!above && bid > 0 && bid >= s.minVol && bid >= s.ratio * above[1];
        const c = cx0 + cw / 2;
        ctx.textAlign = 'right';
        ctx.font = sellImb ? bold : font;
        ctx.fillStyle = sellImb ? s.down : '#c9d1d9';
        ctx.fillText(fpShort(bid), c - 4, mid);
        ctx.textAlign = 'center';
        ctx.font = font;
        ctx.fillStyle = '#6e7681';
        ctx.fillText('x', c, mid);
        ctx.textAlign = 'left';
        ctx.font = buyImb ? bold : font;
        ctx.fillStyle = buyImb ? s.up : '#c9d1d9';
        ctx.fillText(fpShort(ask), c + 4, mid);
      } else {
        ctx.textAlign = 'center';
        ctx.font = font;
        ctx.fillStyle = s.mode === 'delta' ? (delta > 0 ? s.up : delta < 0 ? s.down : '#c9d1d9') : '#c9d1d9';
        ctx.fillText(fpShort(s.mode === 'delta' ? delta : v), cx0 + cw / 2, mid);
      }
    }
    if (cw >= 30) {   // the bar's delta and volume under it
      const y = (Y((Math.floor(Math.min(...groups.keys())) * k - 0.5) * s.tick) ?? 0) + 4;
      ctx.font = `10px ${NUM_FONT}`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillStyle = td > 0 ? s.up : td < 0 ? s.down : '#c9d1d9';
      ctx.fillText(`Δ${fpShort(td)}`, cx0 + cw / 2, y);
      ctx.fillStyle = '#8b96a5';
      ctx.fillText(fpShort(tv), cx0 + cw / 2, y + 12);
    }
  }
  ctx.restore();
}

class FootprintPrimitive {
  constructor(getState) {
    this.getState = getState;
    this.p = null;
    this.views = [{
      zOrder: () => 'normal',
      renderer: () => (this.p ? {
        draw: (target) => target.useMediaCoordinateSpace((scope) => {
          const st = this.getState();
          if (st) drawFootprint(scope.context, scope.mediaSize, this.p.chart, this.p.series, st);
        }),
      } : null),
    }];
  }
  attached(p) { this.p = p; }
  detached() { this.p = null; axisStack.drop(this); }
  paneViews() { return this.views; }
  update() { if (this.p) this.p.requestUpdate(); }
}

/* ---- Tatanka volume profiles: OnRender of the NinjaTrader indicators Auto Anchored (AAVP), Periodic (PVP) and
   Session (SVP) Volume Profile. The server runs their calculation cores (vp.py) on this app's 1-minute bars and
   trades and sends what OnRender reads; this draws it the way they do. Times are epoch ms; NinjaTrader stamps a
   bar at its end, these bars at their start. ------------------------------------------------------------------- */
const VP_FONT_PX = 11;   // SimpleFont("Segoe UI", 11)
const VP_FONT = `${VP_FONT_PX}px "Segoe UI", system-ui, sans-serif`;
const vpColor = (c) => (c && c !== 'transparent' ? c : null);
// "0" and "+0;-0;0": whole numbers, halves away from zero; a value that rounds to 0 prints 0.
const vpWhole = (v) => String(Math.sign(v) * Math.round(Math.abs(v)) || 0);
const vpSigned = (v) => { const r = Math.round(Math.abs(v)); return r ? `${v > 0 ? '+' : '-'}${r}` : '0'; };
const vpPct = (v) => String(Math.round(v * 10) / 10);   // "0.#"
// DrawText into a box: word-wrapped to its width, the first line's top at y (up: the last line's, so a notice at the
// bottom that wraps on a narrow chart grows upward instead of off the panel).
function vpText(ctx, text, x, y, w, color, up = false) {
  if (!color) return;
  const words = text.split(' '), lines = [];
  let line = '';
  for (const word of words) {
    const t = line ? `${line} ${word}` : word;
    if (line && ctx.measureText(t).width > w) { lines.push(line); line = word; } else line = t;
  }
  lines.push(line);
  ctx.fillStyle = color;
  ctx.textBaseline = 'alphabetic';
  const top = up ? y - (lines.length - 1) * 1.33 * VP_FONT_PX : y;
  lines.forEach((l, i) => ctx.fillText(l, x, top + 1.079 * VP_FONT_PX + i * 1.33 * VP_FONT_PX));
}
function vpLine(ctx, x0, x1, y, color, width) {
  if (!color) return;
  const yy = width % 2 ? Math.round(y) + 0.5 : Math.round(y);
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.setLineDash([]);
  ctx.beginPath(); ctx.moveTo(x0, yy); ctx.lineTo(x1, yy); ctx.stroke();
}
function vpFill(ctx, x, y, w, h, color) {
  if (!color || !(w > 0) || !(h > 0)) return;
  ctx.fillStyle = color;
  ctx.fillRect(x, y, w, h);
}
// The chart as OnRender sees it: bar end times, the visible bars (FromIndex..ToIndex), the price range on screen, and
// GetXByTime: the x of the first bar whose end is at or after t. Time bars run on by the bar period before the first
// and after the last bar (this app can hold older profiles than bars); other bars stop at the ends.
function vpView(chart, series, bars, periodMs, H) {
  const ts = chart.timeScale(), n = bars.length, r = ts.getVisibleLogicalRange();
  if (!n || !r) return null;
  const P = periodMs;
  const endT = (i) => (P ? bars[i].t + P : i + 1 < n ? bars[i + 1].t : Math.max(bars[i].t, Date.now()));
  const clamp = (i) => Math.min(n - 1, Math.max(0, i));
  const from = clamp(Math.round(r.from)), to = clamp(Math.round(r.to));
  const xOf = (t) => {
    if (P) {
      const i = barIndexFrom(bars, t - P);
      if (i < 0) return logicalX(ts, n - 1 + (t - endT(n - 1)) / P);
      if (i === 0 && t <= bars[0].t) return logicalX(ts, (t - endT(0)) / P);
      return ts.logicalToCoordinate(i);
    }
    return ts.logicalToCoordinate(clamp(barIndexTo(bars, t)));
  };
  const a = series.coordinateToPrice(0), b = series.coordinateToPrice(H);
  if (a == null || b == null) return null;
  return { ts, n, from, to, endT, xOf, min: Math.min(a, b), max: Math.max(a, b), Y: (p) => series.priceToCoordinate(p) };
}
// TatankaAavpCore.ProfileLayout.Get
function vpLayout(anchorX, latestX, panelLeft, panelRight, widthPercent, pin, padding) {
  let left = anchorX, right = Math.max(anchorX + 2, latestX);
  if (pin) {
    const gap = Math.min(Math.max(0, padding), Math.max(0, (panelRight - panelLeft - 2) / 2));
    left = panelLeft + gap;
    right = Math.max(left + 1, panelRight - gap);
  }
  return { left, right, width: (right - left) * Math.max(1, Math.min(100, widthPercent)) / 100 };
}
// The AAVP / PVP histogram rows (the same code in both): rows [low tick, high tick, up, down].
function vpMinuteRows(ctx, p, tick, o, v, left, right, width, alignLeft) {
  const c = { up: vpColor(o.UpVolumeBrush), down: vpColor(o.DownVolumeBrush), vaUp: vpColor(o.ValueAreaUpBrush),
    vaDown: vpColor(o.ValueAreaDownBrush), text: vpColor(o.TextBrush) };
  for (let i = 0; i < p.rows.length; i++) {
    const [lt, ht, up, down] = p.rows[i], total = up + down;
    if (total <= 0) continue;
    const lo = (lt - 0.5) * tick, hi = (ht + 0.5) * tick;
    if (hi < v.min || lo > v.max) continue;
    const y1 = v.Y(hi), y2 = v.Y(lo);
    if (y1 == null || y2 == null) continue;
    const fullHeight = Math.abs(y2 - y1);
    const height = Math.max(0.15, fullHeight - Math.min(o.RowGapPixels, fullHeight * 0.35));
    const y = Math.min(y1, y2) + (fullHeight - height) / 2;
    const va = i >= p.val_i && i <= p.vah_i;
    let first = va ? c.vaUp : c.up;
    const second = va ? c.vaDown : c.down;
    let w1, w2 = 0, label;
    if (o.VolumeMode === 'UpDown') {
      w1 = width * up / p.max;
      w2 = width * down / p.max;
      label = `${vpWhole(up)} / ${vpWhole(down)}`;
    } else if (o.VolumeMode === 'Delta') {
      const delta = up - down;
      w1 = p.max_delta > 0 ? width * Math.abs(delta) / p.max_delta : 0;
      if (delta < 0) first = second;
      label = vpSigned(delta);
    } else {
      w1 = width * total / p.max;
      label = vpWhole(total);
    }
    const rowLeft = alignLeft ? left : right - w1 - w2;
    if (w1 > 0) vpFill(ctx, rowLeft, y, w1, height, first);
    if (w2 > 0) vpFill(ctx, rowLeft + w1, y, w2, height, second);
    if (o.ShowRowValues && fullHeight >= 12 && w1 + w2 >= 55) vpText(ctx, label, rowLeft + 3, y, w1 + w2 - 3, c.text);
  }
}
function vpBox(ctx, p, v, left, right, color) {
  const top = v.Y(p.high_edge), bottom = v.Y(p.low_edge);
  if (top != null && bottom != null) vpFill(ctx, left, Math.min(top, bottom), Math.max(1, right - left), Math.abs(bottom - top), vpColor(color));
}
// RenderLevel / DrawLevel: a level from left to `end`, inside the panel; its name and price at the right of the
// profile when labels are on.
function vpLevel(ctx, v, s, price, name, left, right, end, edge, color, label) {
  if (price < v.min || price > v.max) return;
  const y = v.Y(price), o = s.set;
  if (y == null) return;
  if (end >= 0 && left <= edge) vpLine(ctx, Math.max(0, left), Math.min(edge, end), y, vpColor(color), o.LineWidth);
  if (label != null) vpText(ctx, `${name} ${s.fmt(price)}`, label, y - 15, 110, vpColor(color));
}

// What an indicator writes at the top of the panel instead of drawing (an error, or no data yet), or null.
function vpMessage(s) {
  const d = s.d;
  if (s.kind === 'aavp') {
    const p = d?.profile;
    return s.error || (!p || !p.rows.length || p.max <= 0 ? 'Tatanka AAVP: waiting for 1-minute history. Check the connection, Days to load, and Trading Hours.' : null);
  }
  if (s.kind === 'pvp') return s.error || (!d?.profiles?.length ? 'Tatanka PVP: waiting for 1-minute history. Check the connection, Days to load, and Trading Hours.' : null);
  return s.error || (!d?.sessions?.length ? 'Tatanka SVP: no ticks in the custom session. Check historical tick data, Trading Hours, and session time zone.' : null);
}
// ... and at the bottom: the incomplete-history or row-cap notice, or null.
function vpNotice(s) {
  if (vpMessage(s)) return null;
  if (s.kind === 'prof') {   // this app's own: sessions with minutes when no trade feed ran here
    const n = s.d.sessions.filter((p) => p.approx).length;
    return n ? `SVP: ${n === s.d.sessions.length ? 'no' : `${n} of ${s.d.sessions.length} sessions have minutes with no`} trades stored here; those minutes use 1-minute bars (volume spread over each bar, up or down by its close).` : null;
  }
  const o = s.set, p = s.kind === 'aavp' ? s.d.profile : s.d.profiles.at(-1);
  if (o.ShowHistoryNotice && p.first) {
    return s.kind === 'aavp' ? `AAVP: load history from before ${p.window[0]} for the full ${o.AnchorPeriod} anchor. Missing history cannot be reconstructed.`
      : 'PVP: first loaded period may be incomplete. Load history from before its start.';
  }
  return p.capped ? `${s.kind.toUpperCase()}: row height increased to keep the profile within 20,000 rows.` : null;
}

function drawAavp(ctx, W, H, v, s) {
  const o = s.set, d = s.d, p = d.profile;
  let g = null;
  if (s.daily) {   // TatankaAavpCore.DailyBarRange: the daily bars of the trading days with data
    const bars = s.bars;
    const first = bars.findIndex((b) => sessionDay(b.t) >= d.first_td);
    let last = -1;
    for (let i = bars.length - 1; i >= 0; i--) if (sessionDay(bars[i].t) <= d.last_td) { last = i; break; }
    if (first >= 0 && last >= first && first <= v.to && last >= v.from) {
      const firstX = v.ts.logicalToCoordinate(first), lastX = v.ts.logicalToCoordinate(last);
      let spacing = Math.max(2, v.ts.options().barSpacing * 0.8);
      if (bars.length > 1) spacing = Math.max(2, Math.abs(firstX - v.ts.logicalToCoordinate(first > 0 ? first - 1 : first + 1)));
      const half = Math.max(2, Math.abs(spacing)) / 2;
      g = vpLayout(firstX - half, lastX + half, 0, W, o.WidthPercent, o.PinToVisibleEdge, o.EdgePaddingPixels);
    }
  } else if (!(p.start > v.endT(v.to) || p.last < v.endT(v.from))) {
    // NinjaTrader's minute series loads the chart's days, so its profile starts no earlier than the chart's first bar.
    // This one holds the anchor's whole stretch (a Year from Jan 1): from before the first bar it is drawn from that
    // bar, as there, not at a time counted back by the bar length over nights and weekends, far off the chart's left.
    const anchorX = p.start < s.bars[0].t ? v.ts.logicalToCoordinate(0) : v.xOf(p.start);
    g = vpLayout(anchorX, v.xOf(p.last), 0, W, o.WidthPercent, o.PinToVisibleEdge, o.EdgePaddingPixels);
  }
  if (g) {
    vpBox(ctx, p, v, g.left, g.right, o.HistogramBackgroundBrush);
    if (o.ShowProfile) vpMinuteRows(ctx, p, s.tick, o, v, g.left, g.right, g.width, o.Placement === 'Left');
    const label = o.ShowLevelLabels ? Math.max(4, Math.min(g.right + 4, W - 110)) : null;
    if (o.ShowVah) vpLevel(ctx, v, s, p.vah, 'VAH', g.left, g.right, g.right, W, o.ValueAreaLineBrush, label);
    if (o.ShowVal) vpLevel(ctx, v, s, p.val, 'VAL', g.left, g.right, g.right, W, o.ValueAreaLineBrush, label);
    if (o.ShowPoc) vpLevel(ctx, v, s, p.poc, 'POC', g.left, g.right, g.right, W, o.PocLineBrush, label);
  }
}

function drawPvp(ctx, W, H, v, s) {
  const o = s.set, list = s.d.profiles;
  const pinned = o.Placement === 'ChartRightLatest';
  for (let n = 0; n < list.length; n++) {
    const p = list[n], latest = n === list.length - 1;
    if (!p.rows.length || p.max <= 0 || p.start > v.endT(v.to)) continue;
    if (pinned && !latest) continue;
    let left = v.xOf(p.start), right = v.xOf(p.last);
    let width = Math.max(2, right - left) * o.WidthPercent / 100;
    if (pinned) {
      right = W - o.RightPaddingPixels;
      width = Math.max(1, W * o.WidthPercent / 100);
      left = right - width;
    }
    if (pinned || p.last >= v.endT(v.from)) {
      vpBox(ctx, p, v, left, right, o.HistogramBackgroundBrush);
      if (o.ShowProfile) vpMinuteRows(ctx, p, s.tick, o, v, left, right, width, o.Placement === 'Left');
    }
    const label = latest && o.ShowLevelLabels ? Math.max(4, Math.min(right + 4, W - 110)) : null;
    const end = (extend, touch) => (!extend ? right : o.ExtensionBehavior === 'UntilTouched' && touch != null ? Math.max(right, v.xOf(touch)) : W);
    const t = p.touch || {};
    if (o.ShowVah) vpLevel(ctx, v, s, p.vah, 'VAH', left, right, end(o.ExtendVah, t.vah), W, o.ValueAreaLineBrush, label);
    if (o.ShowVal) vpLevel(ctx, v, s, p.val, 'VAL', left, right, end(o.ExtendVal, t.val), W, o.ValueAreaLineBrush, label);
    if (o.ShowPoc) vpLevel(ctx, v, s, p.poc, 'POC', left, right, end(o.ExtendPoc, t.poc), W, o.PocLineBrush, label);
  }
}

function drawSvp(ctx, W, H, v, s) {
  const o = s.set, list = s.d.sessions;
  const c = { up: vpColor(o.UpVolumeBrush), down: vpColor(o.DownVolumeBrush), vaUp: vpColor(o.ValueAreaUpBrush),
    vaDown: vpColor(o.ValueAreaDownBrush), text: vpColor(o.TextBrush) };
  const visibleFirst = v.endT(v.from), visibleLast = v.endT(v.to), lastLoaded = v.endT(v.n - 1), tick = s.tick;
  const pinned = o.Alignment === 'ChartRightLatest';
  for (let i = 0; i < list.length; i++) {
    const p = list[i], latest = i === list.length - 1;
    if (!p.rows.length) continue;
    if (pinned && !latest) continue;
    if (pinned && visibleLast < p.start) continue;   // not the latest profile on a scrolled-back date
    if (!pinned && (p.end < visibleFirst || p.start > visibleLast)) continue;
    // A forming session's right edge follows its latest tick; a finished one uses its scheduled end.
    let edgeTime = p.last < p.end ? p.last : p.end;
    if (lastLoaded >= p.end) edgeTime = p.end;
    const sessionLeft = v.xOf(p.start), sessionRight = v.xOf(edgeTime);
    const right = pinned ? W - o.RightPaddingPixels : sessionRight;
    const span = pinned ? W : Math.max(2, sessionRight - sessionLeft);
    const width = Math.max(1, span * o.WidthPercent / 100);
    const lineLeft = pinned ? Math.max(0, right - width) : sessionLeft;
    const boxTop = v.Y(p.high_edge), boxBottom = v.Y(p.low_edge);
    if (boxTop != null && boxBottom != null) {
      vpFill(ctx, pinned ? right - width : sessionLeft, Math.min(boxTop, boxBottom), pinned ? width : Math.max(1, right - sessionLeft),
        Math.abs(boxBottom - boxTop), vpColor(o.HistogramBackgroundBrush));
    }
    if (o.ShowProfile) {
      for (const [key, up, down] of p.rows) {
        const lowEdge = key * p.row_size - tick / 2, highEdge = lowEdge + p.row_size;
        if (highEdge < v.min || lowEdge > v.max) continue;
        const top = v.Y(highEdge), bottom = v.Y(lowEdge);
        if (top == null || bottom == null) continue;
        const fullHeight = Math.abs(bottom - top);
        // Keep subpixel rows subpixel; forcing every row to 1px inflates dense profiles.
        const height = Math.max(0.2, fullHeight - Math.min(o.RowGapPixels, fullHeight * 0.35));
        const y = Math.min(top, bottom) + (fullHeight - height) / 2;
        const upWidth = width * up / p.max, downWidth = width * down / p.max;
        const inVa = key >= p.val_key && key <= p.vah_key, left = right - upWidth - downWidth;
        if (upWidth > 0) vpFill(ctx, left, y, upWidth, height, inVa ? c.vaUp : c.up);
        if (downWidth > 0) vpFill(ctx, right - downWidth, y, downWidth, height, inVa ? c.vaDown : c.down);
        if (o.ShowRowVolumes && fullHeight >= 12 && upWidth + downWidth >= 60) vpText(ctx, `${vpWhole(up)} / ${vpWhole(down)}`, left + 3, y, upWidth + downWidth - 3, c.text);
      }
    }
    const label = o.ShowLevelLabels && latest ? Math.max(4, Math.min(right + 5, W - 110)) : null;
    if (o.ShowVah) vpLevel(ctx, v, s, p.vah, 'VAH', lineLeft, right, o.ExtendVahToChartEdge ? W : right, W, o.ValueAreaLineBrush, label);
    if (o.ShowVal) vpLevel(ctx, v, s, p.val, 'VAL', lineLeft, right, o.ExtendValToChartEdge ? W : right, W, o.ValueAreaLineBrush, label);
    if (o.ShowPoc) vpLevel(ctx, v, s, p.poc, 'POC', lineLeft, right, o.ExtendPocToChartEdge ? W : right, W, o.PocLineBrush, label);
  }
}

// s.textX, s.msgY, s.noticeY: where its texts go (NinjaTrader: 12 px in, 35 px from the top, 23 from the bottom), moved
// by the caller so the legend, the drawing tools and the other profiles' texts are not written over.
function drawVp(ctx, size, chart, series, s) {
  const W = size.width, H = size.height;
  ctx.save();
  ctx.font = VP_FONT;
  const text = vpColor(s.set.TextBrush), m = vpMessage(s);
  const x = s.textX ?? 12;
  if (m) vpText(ctx, m, x, s.msgY ?? 35, Math.max(100, W - x - 12), text);
  else {
    const v = vpView(chart, series, s.bars, s.periodMs, H);
    if (v) {
      if (s.kind === 'aavp') drawAavp(ctx, W, H, v, s);
      else if (s.kind === 'pvp') drawPvp(ctx, W, H, v, s);
      else drawSvp(ctx, W, H, v, s);
    }
    const n = vpNotice(s);
    if (n) vpText(ctx, n, x, H - (s.noticeY ?? 23), Math.max(100, W - x - 12), text, true);
  }
  ctx.restore();
}
// The latest profile's levels, as the indicators publish them to their POC / VAH / VAL plots.
function vpLatest(s) {
  const d = s && !s.error ? s.d : null;
  const p = !d ? null : s.kind === 'aavp' ? d.profile : (s.kind === 'pvp' ? d.profiles : d.sessions)?.at(-1);
  return p && p.rows.length ? p : null;
}
// DisplayName: "Tatanka AAVP (Year, Total, VA 68%, 200 NumberOfRows)  POC ...  VAH ...  VAL ..."
function vpTitle(s) {
  const o = s.set, pct = vpPct(o.ValueAreaPercent);
  let name = `Tatanka ${{ aavp: 'AAVP', pvp: 'PVP', prof: 'SVP' }[s.kind]}`;
  if (o.ShowInputsInLabel) {
    name += s.kind === 'aavp' ? ` (${o.AnchorPeriod}, ${o.VolumeMode}, VA ${pct}%, ${o.RowSize} ${o.RowsLayout})`
      : s.kind === 'pvp' ? ` (${o.PeriodMultiplier} ${o.Period}, ${o.VolumeMode}, VA ${pct}%, ${o.RowSize} ${o.RowsLayout})`
        : ` (${o.SessionStart}-${o.SessionEnd}, ${o.SessionTimeZone}, VA ${pct}%, ${o.Alignment})`;
  }
  const p = vpLatest(s);
  if (o.ShowValuesInLabel && p) name += `  POC ${s.fmt(p.poc)}  VAH ${s.fmt(p.vah)}  VAL ${s.fmt(p.val)}`;
  return name;
}
// A price marker's text: black on a light plot color, white on a dark one.
const vpTagText = (hex) => { const [r, g, b] = hexRgb(hex); return (r * 299 + g * 587 + b * 114) / 1000 > 150 ? '#000000' : '#ffffff'; };

class VolumeProfilePrimitive {
  constructor(getState) {
    this.getState = getState;
    this.p = null;
    this.views = [{
      zOrder: () => 'top',
      renderer: () => (this.p ? {
        draw: (target) => target.useMediaCoordinateSpace((scope) => {
          const s = this.getState();
          if (s) drawVp(scope.context, scope.mediaSize, this.p.chart, this.p.series, s);
        }),
      } : null),
    }];
  }
  attached(p) { this.p = p; }
  detached() { this.p = null; axisStack.drop(this); }
  paneViews() { return this.views; }
  // PaintPriceMarkers with the POC / VAH / VAL plots (PlotStyle.PriceBox), a plot hidden when its level is off.
  priceAxisViews() {
    const s = this.p && this.getState(), p = vpLatest(s);
    if (!p) return axisStack.views(this, []);
    const o = s.set, Y = (x) => this.p.series.priceToCoordinate(x);
    return axisStack.views(this, [[o.ShowPoc, p.poc, o.PocLineBrush], [o.ShowVah, p.vah, o.ValueAreaLineBrush], [o.ShowVal, p.val, o.ValueAreaLineBrush]]
      .filter(([on, , c]) => on && vpColor(c)).map(([, price, c]) => ({
        coordinate: () => Y(price) ?? -100, text: () => s.fmt(price), textColor: () => vpTagText(c), backColor: () => c,
        visible: () => Y(price) != null,
      })));
  }
  update() { if (this.p) this.p.requestUpdate(); }
}

// The name and values of a lower-pane indicator at the pane's top left.
function drawPaneLabel(ctx, s) {
  ctx.save();
  ctx.font = '11px system-ui, sans-serif';
  ctx.textBaseline = 'top';
  let x = s.x;
  for (const [text, color] of s.parts) {
    ctx.fillStyle = color;
    ctx.fillText(text, x, 5);
    x += ctx.measureText(text).width + 8;
  }
  ctx.restore();
}
class PaneLabelPrimitive {
  constructor(getState) {
    this.getState = getState;
    this.p = null;
    this.views = [{
      zOrder: () => 'top',
      renderer: () => (this.p ? {
        draw: (target) => target.useMediaCoordinateSpace((scope) => {
          const st = this.getState();
          if (st) drawPaneLabel(scope.context, st);
        }),
      } : null),
    }];
  }
  attached(p) { this.p = p; }
  detached() { this.p = null; axisStack.drop(this); }
  paneViews() { return this.views; }
  update() { if (this.p) this.p.requestUpdate(); }
}

/* ---- Tatanka Level Lines 1.1.0: a port of the NinjaTrader OnRender. A range (top, bottom, optional midpoint and
   shading) and any number of custom lines "price|label|color|style|width"; labels on the right edge above each line,
   price appended, pushed down instead of overlapping. ------------------------------------------------------------ */
const NT_DASH = {   // NinjaTrader's DashStyleHelper, in line widths
  Solid: [], Dash: [3, 3], Dot: [1, 2], DashDot: [3, 3, 1, 3], DashDotDot: [3, 3, 1, 3, 1, 3],
};
function ntDash(dash, width) { return (NT_DASH[dash] || []).map((k) => k * Math.max(1, width)); }
// WPF colors: a name, #RRGGBB or #AARRGGBB (alpha first); null when it is not one.
function wpfColor(text) {
  const t = (text || '').trim();
  if (/^#[0-9a-f]{8}$/i.test(t)) {
    const n = parseInt(t.slice(1), 16);
    return `rgba(${(n >>> 16) & 255}, ${(n >>> 8) & 255}, ${n & 255}, ${((n >>> 24) & 255) / 255})`;
  }
  if (/^#[0-9a-f]{6}$/i.test(t)) return t;
  return t && /^[a-z]+$/i.test(t) && CSS.supports('color', t) ? t.toLowerCase() : null;
}
const LL_DASH_NAMES = { solid: 'Solid', dash: 'Dash', dashed: 'Dash', dot: 'Dot', dotted: 'Dot', dashdot: 'DashDot', dashdotdot: 'DashDotDot' };
// BuildCustomLines: separators , ; and new lines; price required (> 0), the rest falls back to the default line. The
// price is the whole field, as NinjaTrader's double.TryParse takes it, or in 32nds (110'16) as If/Then reads it.
function parseLevelLines(text, def) {
  const out = [];
  for (const raw of (text || '').split(/[,;\n\r]+/)) {
    if (!raw.trim()) continue;
    const f = raw.split('|'), price = biasNumber(f[0]);
    if (!(price > 0)) continue;
    const width = f.length > 4 && parseFloat(f[4]) > 0 ? parseFloat(f[4]) : def.width;
    out.push({ price, label: (f[1] || '').trim(), color: (f.length > 2 && wpfColor(f[2])) || def.color,
      dash: (f.length > 3 && LL_DASH_NAMES[f[3].replace(/\s/g, '').toLowerCase()]) || def.dash, width });
  }
  return out;
}
// s: the settings (nt_schema "lines") with this symbol's range and levels; fmt formats a price like the instrument.
function levelLinesOf(s) {
  const lines = [];
  if (s.RangeTop > 0) lines.push({ price: s.RangeTop, label: s.RangeTopLabel, ...s.RangeStroke });
  if (s.RangeBottom > 0) lines.push({ price: s.RangeBottom, label: s.RangeBottomLabel, ...s.RangeStroke });
  if (s.ShowRangeMid && s.RangeTop > 0 && s.RangeBottom > 0) lines.push({ price: (s.RangeTop + s.RangeBottom) / 2, label: s.RangeMidLabel, ...s.RangeMidStroke });
  return lines.concat(parseLevelLines(s.CustomLevels, s.DefaultStroke));
}
function drawLevelLines(ctx, size, series, s, fmt) {
  const x0 = 0, x1 = size.width, Y = (p) => series.priceToCoordinate(p);
  if (s.FillRange && s.RangeTop > 0 && s.RangeBottom > 0) {
    const yHi = Y(Math.max(s.RangeTop, s.RangeBottom)), yLo = Y(Math.min(s.RangeTop, s.RangeBottom));
    if (yHi != null && yLo != null) {
      ctx.save();
      ctx.globalAlpha = Math.max(0, Math.min(100, s.RangeFillOpacity)) / 100;
      ctx.fillStyle = s.RangeFillBrush;
      ctx.fillRect(x0, yHi, x1 - x0, yLo - yHi);
      ctx.restore();
    }
  }
  const labels = [];
  for (const l of levelLinesOf(s)) {
    const y = Y(l.price);
    if (y == null || y < 0 || y > size.height) continue;   // NinjaTrader skips lines outside the price scale
    ctx.save();
    ctx.strokeStyle = l.color;
    ctx.lineWidth = l.width;
    ctx.setLineDash(ntDash(l.dash, l.width));
    const yy = l.width % 2 ? Math.round(y) + 0.5 : Math.round(y);
    ctx.beginPath(); ctx.moveTo(x0, yy); ctx.lineTo(x1, yy); ctx.stroke();
    ctx.restore();
    const label = (l.label || '').trim(), text = s.ShowPriceInLabel ? (label ? `${label}  ${fmt(l.price)}` : fmt(l.price)) : label;
    if (text) labels.push({ y, text, color: l.color, width: l.width });
  }
  if (!labels.length) return;
  labels.sort((a, b) => a.y - b.y);   // top to bottom: one that would overlap the label above moves down
  const pad = 4, font = s.LabelFont || { family: 'Arial', size: 11 }, h = Math.ceil(font.size * 1.25);
  ctx.save();
  ctx.font = `${font.size}px ${font.family}, sans-serif`;
  ctx.textBaseline = 'top';
  let lastBottom = -Infinity;
  for (const l of labels) {
    const w = ctx.measureText(l.text).width;
    let ty = s.LabelAboveLine ? l.y - h - l.width / 2 - 1 : l.y + l.width / 2 + 1;
    if (ty < lastBottom + 1) ty = lastBottom + 1;
    const tx = s.LabelsOnRight ? x1 - w - pad * 2 : x0 + pad;
    if (s.LabelBackground) { ctx.fillStyle = 'rgba(0, 0, 0, 0.6)'; ctx.fillRect(tx - pad, ty, w + pad * 2, h); }
    ctx.fillStyle = l.color;
    ctx.fillText(l.text, tx, ty + 1);
    lastBottom = ty + h;
  }
  ctx.restore();
}
class LevelLinesPrimitive {
  constructor(getState) {
    this.getState = getState;
    this.p = null;
    this.views = [{
      zOrder: () => 'top',
      renderer: () => (this.p ? {
        draw: (target) => target.useMediaCoordinateSpace((scope) => {
          const st = this.getState();
          if (st) drawLevelLines(scope.context, scope.mediaSize, this.p.series, st.s, st.fmt);
        }),
      } : null),
    }];
  }
  attached(p) { this.p = p; }
  detached() { this.p = null; axisStack.drop(this); }
  paneViews() { return this.views; }
  update() { if (this.p) this.p.requestUpdate(); }
}
