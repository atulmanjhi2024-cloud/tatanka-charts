/* Technical indicators as NinjaTrader 8 computes them (its @Indicators source), one bar at a time. A runner's
   step(bar, commit) returns its plots for a bar ({t, o, h, l, c, v, d}); commit false is the forming bar: a step that
   is not kept, so a live tick costs one step and the forming bar is computed from the closed bars as OnEachTick would.
   Where NinjaTrader has no such indicator (SuperTrend, Ichimoku, relative volume) or its exact code is not followed
   (SMMA's first bars, the regression channel's band), the usual definition is used, as TradingView's; each says so.
   The server's indicator alerts run the same formulas (server/chartsrv/ta.py) and a test runs both on the same bars.
   No page code here: Node runs this file in that test. */
'use strict';

// A look-back series: at(0) the newest value, at(k) k bars before. Kept to what the longest look-back needs.
class TaBuf {
  constructor(keep) { this.a = []; this.keep = Math.max(2, keep); }
  push(x) { this.a.push(x); }
  pop() { this.a.pop(); }
  at(k) { return this.a[this.a.length - 1 - k]; }
  trim() { if (this.a.length > 2 * this.keep + 256) this.a.splice(0, this.a.length - this.keep); }
}
const taMax = (buf, m) => { let x = -Infinity; for (let k = 0; k < m; k++) x = Math.max(x, buf.at(k)); return x; };   // NinjaTrader's MAX
const taMin = (buf, m) => { let x = Infinity; for (let k = 0; k < m; k++) x = Math.min(x, buf.at(k)); return x; };    // and MIN
const taZero = (x) => Math.abs(x) < 1e-10;   // NinjaTrader's ApproxCompare(0) == 0

// ---- NinjaTrader's EMA, SMA, RSI, ATR and MACD, and the cumulative delta -----------------------------------
class NtEmaRun {   // the first input, then x * 2/(1+n) + (1 - 2/(1+n)) * previous
  constructor(n) { this.k = 2 / (1 + n); this.v = null; }
  step(x, commit = true) {
    const v = this.v == null ? x : x * this.k + (1 - this.k) * this.v;
    if (commit) this.v = v;
    return v;
  }
}
class NtSmaRun {   // the average of the last n inputs, or of all of them while there are fewer
  constructor(n) { this.n = n; this.buf = []; this.sum = 0; }
  step(x, commit = true) {
    const len = this.buf.length, sum = this.sum + x - (len >= this.n ? this.buf[len - this.n] : 0);
    if (commit) {
      this.buf.push(x);
      this.sum = sum;
      if (this.buf.length > 4 * this.n + 64) this.buf.splice(0, this.buf.length - this.n);
    }
    return sum / Math.min(len + 1, this.n);
  }
}
class NtRsiRun {   // Wilder averages of up and down closes seeded with their SMA, and Avg, an EMA(Smooth) of RSI
  constructor(n, smooth) {
    this.n = n; this.c1 = 2 / (1 + smooth); this.i = 0; this.prev = 0;
    this.smaUp = new NtSmaRun(n); this.smaDown = new NtSmaRun(n); this.avgUp = 0; this.avgDown = 0; this.avg = 0;
  }
  step(x, commit = true) {
    const { n, i } = this;
    const up = i === 0 ? 0 : Math.max(x - this.prev, 0), down = i === 0 ? 0 : Math.max(this.prev - x, 0);
    const smaUp = this.smaUp.step(up, commit), smaDown = this.smaDown.step(down, commit);
    let { avgUp, avgDown, avg } = this, rsi = null;
    if (i === 0) { if (n < 3) avg = 50; }
    else if (i + 1 < n) { if (i + 1 === n - 1) avg = 50; }
    else {
      if (i + 1 === n) { avgDown = smaDown; avgUp = smaUp; }
      else { avgDown = (avgDown * (n - 1) + down) / n; avgUp = (avgUp * (n - 1) + up) / n; }
      rsi = avgDown === 0 ? 100 : 100 - 100 / (1 + avgUp / avgDown);
      avg = this.c1 * rsi + (1 - this.c1) * avg;
    }
    if (commit) Object.assign(this, { i: i + 1, prev: x, avgUp, avgDown, avg });
    return rsi == null ? [null, null] : [rsi, avg];
  }
}
class NtAtrRun {   // the first bar's range, then ((min(bars, n) - 1) * previous + true range) / min(bars, n)
  constructor(n) { this.n = n; this.i = 0; this.v = 0; this.close = 0; }
  step(b, commit = true) {
    const v = this.i === 0 ? b.h - b.l
      : ((Math.min(this.i + 1, this.n) - 1) * this.v + Math.max(Math.abs(b.l - this.close), b.h - b.l, Math.abs(b.h - this.close))) / Math.min(this.i + 1, this.n);
    if (commit) { this.i += 1; this.v = v; this.close = b.c; }
    return v;
  }
}
class NtMacdRun {  // EMA(fast) - EMA(slow), Avg an EMA(smooth) of it from 0 on the first bar, Diff the histogram
  constructor(fast, slow, smooth) { this.fast = new NtEmaRun(fast); this.slow = new NtEmaRun(slow); this.avg = new NtEmaRun(smooth); }
  step(x, commit = true) {
    const macd = this.fast.step(x, commit) - this.slow.step(x, commit), avg = this.avg.step(macd, commit);
    return [macd, avg, macd - avg];
  }
}
// The trade date a bar belongs to, on its exchange's clock, cached per hour. CME (the default): sessions open 17:00 CT,
// so 17:00 CT + 7h is midnight; a stock's or crypto's day starts at its exchange's midnight (setSessionClock).
let sessionDayFmt = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Chicago', year: 'numeric', month: '2-digit', day: '2-digit' });
let sessionShift = 7 * 3600e3, sessionClockKey = 'America/Chicago 17:00';
const sessionDayCache = new Map();
function setSessionClock(tz = 'America/Chicago', day = '17:00') {
  if (`${tz} ${day}` === sessionClockKey) return;
  const [h, m] = day.split(':').map(Number), start = (h * 60 + m) * 60e3;
  sessionDayFmt = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' });
  sessionShift = start >= 12 * 3600e3 ? 24 * 3600e3 - start : -start;   // an evening open trades on the next date
  sessionClockKey = `${tz} ${day}`;
  sessionDayCache.clear();
}
function sessionDay(ms) {
  const h = Math.floor(ms / 3600e3);
  let d = sessionDayCache.get(h);
  if (d == null) {
    if (sessionDayCache.size > 50000) sessionDayCache.clear();
    d = sessionDayFmt.format(h * 3600e3 + sessionShift);
    sessionDayCache.set(h, d);
  }
  return d;
}
// CME's holiday hours by date (hello's holidays) and the chart's holiday group; none on a free-data chart.
let sessionHolidays = {}, sessionGroup = null;
function setSessionHolidays(days, group) { sessionHolidays = days || {}; sessionGroup = days ? group : null; }
// The trade date a bar counts toward: sessionDay, or after a holiday halt (no settlement that day: MLK Day,
// Thanksgiving, ...) the next day that trades, as CME's daily bars (the server's sessions.fold_trade_date).
function taTradeDay(ms) {
  const d = sessionDay(ms), at = (v) => (v && typeof v === 'object' ? v[sessionGroup] ?? v.default : v);
  if (!sessionGroup || !at(sessionHolidays[d]?.halt)) return d;
  const x = new Date(`${d}T12:00:00Z`), iso = () => x.toISOString().slice(0, 10);
  for (let k = 0; k < 10; k++) {
    x.setUTCDate(x.getUTCDate() + 1);
    const h = sessionHolidays[iso()];
    if (x.getUTCDay() % 6 && !(h && (h.closed || at(h.halt)))) break;
  }
  return iso();
}
class CvdRun {     // running sum of bar delta (aggressive buys - sells), restarting each session or never
  constructor(reset) { this.reset = reset; this.cum = 0; this.day = null; }
  step(b, commit = true) {
    const day = this.reset === 'session' ? sessionDay(b.t) : null;
    const open = day !== this.day ? 0 : this.cum, close = open + (b.d || 0);
    if (commit) { this.cum = close; this.day = day; }
    return [open, close];
  }
}

// ---- moving averages ------------------------------------------------------------------------------------------------
class TaWmaRun {   // WMA: weights 1..n, the newest the heaviest; over the bars there are while fewer
  constructor(n) { this.n = n; this.x = new TaBuf(n); this.i = 0; }
  step(x, commit = true) {
    this.x.push(x);
    const back = Math.min(this.n - 1, this.i);
    let val = 0, w = 0;
    for (let k = back; k >= 0; k--) { val += (k + 1) * this.x.at(back - k); w += k + 1; }
    if (commit) { this.i += 1; this.x.trim(); } else this.x.pop();
    return val / w;
  }
}
class TaHmaRun {   // HMA: WMA(sqrt n) of 2 * WMA(n / 2) - WMA(n), the halves and the root whole numbers
  constructor(n) { this.a = new TaWmaRun(Math.max(1, Math.floor(n / 2))); this.b = new TaWmaRun(n); this.d = new TaWmaRun(Math.max(1, Math.floor(Math.sqrt(n)))); }
  step(x, commit = true) { return this.d.step(2 * this.a.step(x, commit) - this.b.step(x, commit), commit); }
}
class TaVwmaRun {   // VWMA: the closes weighted by their volume over the last n bars (the close itself with no volume)
  constructor(n) { this.n = n; this.x = new TaBuf(n); this.v = new TaBuf(n); this.i = 0; }
  step(b, commit = true) {
    this.x.push(b.c); this.v.push(b.v || 0);
    let sv = 0, sxv = 0;
    for (let k = Math.min(this.i, this.n - 1); k >= 0; k--) { sv += this.v.at(k); sxv += this.x.at(k) * this.v.at(k); }
    if (commit) { this.i += 1; this.x.trim(); this.v.trim(); } else { this.x.pop(); this.v.pop(); }
    return sv > 0 ? sxv / sv : b.c;
  }
}
class TaDemaRun {   // DEMA: 2 * EMA - EMA of the EMA
  constructor(n) { this.e1 = new NtEmaRun(n); this.e2 = new NtEmaRun(n); }
  step(x, commit = true) { const a = this.e1.step(x, commit); return 2 * a - this.e2.step(a, commit); }
}
class TaTemaRun {   // TEMA: 3 * EMA - 3 * EMA(EMA) + EMA(EMA(EMA))
  constructor(n) { this.e1 = new NtEmaRun(n); this.e2 = new NtEmaRun(n); this.e3 = new NtEmaRun(n); }
  step(x, commit = true) { const a = this.e1.step(x, commit), b = this.e2.step(a, commit); return 3 * a - 3 * b + this.e3.step(b, commit); }
}
class TaSmmaRun {   // SMMA (Wilder's): the SMA over the first n bars, then (previous * (n - 1) + x) / n
  constructor(n) { this.n = n; this.sma = new NtSmaRun(n); this.i = 0; this.v = 0; }
  step(x, commit = true) {
    const s = this.sma.step(x, commit), v = this.i < this.n ? s : (this.v * (this.n - 1) + x) / this.n;
    if (commit) { this.i += 1; this.v = v; }
    return v;
  }
}

// ---- bands and channels -----------------------------------------------------------------------------------------------
class TaStdDevRun {   // StdDev: the population standard deviation of the last n inputs (0 on the first bar); [SMA, it]
  constructor(n) { this.n = n; this.x = new TaBuf(n); this.sma = new NtSmaRun(n); this.i = 0; }
  step(x, commit = true) {
    this.x.push(x);
    const avg = this.sma.step(x, commit);
    let sd = 0;
    if (this.i >= 1) {
      let s = 0;
      for (let k = Math.min(this.i, this.n - 1); k >= 0; k--) { const d = this.x.at(k) - avg; s += d * d; }
      sd = Math.sqrt(s / Math.min(this.i + 1, this.n));
    }
    if (commit) { this.i += 1; this.x.trim(); } else this.x.pop();
    return [avg, sd];
  }
}
class TaBollingerRun {   // Bollinger: SMA(n) +- k standard deviations; [upper, middle, lower]
  constructor(n, k) { this.sd = new TaStdDevRun(n); this.k = k; }
  step(b, commit = true) { const [m, sd] = this.sd.step(b.c, commit); return [m + this.k * sd, m, m - this.k * sd]; }
}
class TaKeltnerRun {   // KeltnerChannel: SMA(n) of the typical price +- k * SMA(n) of the bar's range; [upper, middle, lower]
  constructor(n, k) { this.typ = new NtSmaRun(n); this.rng = new NtSmaRun(n); this.k = k; }
  step(b, commit = true) {
    const mid = this.typ.step((b.h + b.l + b.c) / 3, commit), off = this.rng.step(b.h - b.l, commit) * this.k;
    return [mid + off, mid, mid - off];
  }
}
class TaDonchianRun {   // DonchianChannel: the highest high and lowest low of the last n bars and their mean; [upper, mean, lower]
  constructor(n) { this.n = n; this.h = new TaBuf(n); this.l = new TaBuf(n); this.i = 0; }
  step(b, commit = true) {
    this.h.push(b.h); this.l.push(b.l);
    const m = Math.min(this.i + 1, this.n), hi = taMax(this.h, m), lo = taMin(this.l, m);
    if (commit) { this.i += 1; this.h.trim(); this.l.trim(); } else { this.h.pop(); this.l.pop(); }
    return [hi, (hi + lo) / 2, lo];
  }
}

// ---- oscillators ---------------------------------------------------------------------------------------------------------
class TaStochRun {   // Stochastics: fast %K over PeriodK, %K its SMA(Smooth), %D the SMA(PeriodD) of %K; [K, D]
  constructor(periodK, smooth, periodD) {
    this.n = periodK; this.h = new TaBuf(periodK); this.l = new TaBuf(periodK); this.i = 0; this.fk = 0;
    this.k = new NtSmaRun(smooth); this.d = new NtSmaRun(periodD);
  }
  step(b, commit = true) {
    this.h.push(b.h); this.l.push(b.l);
    const m = Math.min(this.i + 1, this.n), lo = taMin(this.l, m), den = taMax(this.h, m) - lo;
    const fk = taZero(den) ? (this.i === 0 ? 50 : this.fk) : Math.min(100, Math.max(0, 100 * (b.c - lo) / den));
    const k = this.k.step(fk, commit), d = this.d.step(k, commit);
    if (commit) { this.i += 1; this.fk = fk; this.h.trim(); this.l.trim(); } else { this.h.pop(); this.l.pop(); }
    return [k, d];
  }
}
class TaStochRsiRun {   // StochRSI: where RSI(n) sits in its range over the last n bars, 0 to 1 (RSI's warm-up bars left out)
  constructor(n) { this.n = n; this.rsi = new NtRsiRun(n, 1); this.r = new TaBuf(n); this.i = 0; }
  step(b, commit = true) {
    const r = this.rsi.step(b.c, commit)[0];
    this.r.push(r);
    let lo = Infinity, hi = -Infinity;
    for (let k = Math.min(this.i, this.n - 1); k >= 0; k--) { const v = this.r.at(k); if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
    const v = r == null ? null : r !== lo && hi !== lo ? (r - lo) / (hi - lo) : 0;
    if (commit) { this.i += 1; this.r.trim(); } else this.r.pop();
    return [v];
  }
}
class TaWilliamsRRun {   // WilliamsR: -100 * (highest high - close) / range over the last n bars
  constructor(n) { this.n = n; this.h = new TaBuf(n); this.l = new TaBuf(n); this.i = 0; }
  step(b, commit = true) {
    this.h.push(b.h); this.l.push(b.l);
    const m = Math.min(this.i + 1, this.n), hi = taMax(this.h, m), den = hi - taMin(this.l, m);
    if (commit) { this.i += 1; this.h.trim(); this.l.trim(); } else { this.h.pop(); this.l.pop(); }
    return [-100 * (hi - b.c) / (den === 0 ? 1 : den)];
  }
}
class TaCciRun {   // CCI: (typical - its SMA) / (0.015 * mean deviation), 0 on the first bar
  constructor(n) { this.n = n; this.t = new TaBuf(n); this.sma = new NtSmaRun(n); this.i = 0; }
  step(b, commit = true) {
    const typ = (b.h + b.l + b.c) / 3;
    this.t.push(typ);
    const avg = this.sma.step(typ, commit);
    let v = 0;
    if (this.i > 0) {
      let mean = 0;
      for (let k = Math.min(this.i, this.n - 1); k >= 0; k--) mean += Math.abs(this.t.at(k) - avg);
      v = (typ - avg) / (taZero(mean) ? 1 : 0.015 * (mean / Math.min(this.n, this.i + 1)));
    }
    if (commit) { this.i += 1; this.t.trim(); } else this.t.pop();
    return [v];
  }
}
class TaMomentumRun {   // Momentum: close - the close n bars ago (or the first); ROC: that in percent of the old close
  constructor(n, pct) { this.n = n; this.pct = pct; this.x = new TaBuf(n + 1); this.i = 0; }
  step(b, commit = true) {
    this.x.push(b.c);
    const old = this.x.at(Math.min(this.i, this.n));
    const v = this.pct ? (b.c - old) / old * 100 : this.i === 0 ? 0 : b.c - old;
    if (commit) { this.i += 1; this.x.trim(); } else this.x.pop();
    return [v];
  }
}

// ---- trend and volatility --------------------------------------------------------------------------------------------
class TaAdxRun {   // ADX, with DM's +DI and -DI: Wilder's sums of true range and directional movement; [ADX, +DI, -DI]
  constructor(n) { this.n = n; this.i = 0; this.ph = 0; this.pl = 0; this.pc = 0; this.str = 0; this.sp = 0; this.sm = 0; this.adx = 0; }
  step(b, commit = true) {
    const n = this.n;
    let str, sp, sm, adx, dip = 0, dim = 0;
    if (this.i === 0) { str = b.h - b.l; sp = 0; sm = 0; adx = 50; }
    else {
      const dmm = this.pl - b.l > b.h - this.ph ? Math.max(this.pl - b.l, 0) : 0;
      const dmp = b.h - this.ph > this.pl - b.l ? Math.max(b.h - this.ph, 0) : 0;
      const tr = Math.max(b.h - b.l, Math.max(Math.abs(b.h - this.pc), Math.abs(b.l - this.pc)));
      if (this.i < n) { str = this.str + tr; sp = this.sp + dmp; sm = this.sm + dmm; }
      else { str = this.str - this.str / n + tr; sp = this.sp - this.sp / n + dmp; sm = this.sm - this.sm / n + dmm; }
      dip = 100 * (taZero(str) ? 0 : sp / str);
      dim = 100 * (taZero(str) ? 0 : sm / str);
      const sum = dip + dim;
      adx = taZero(sum) ? 50 : ((n - 1) * this.adx + 100 * Math.abs(dip - dim) / sum) / n;
    }
    if (commit) Object.assign(this, { i: this.i + 1, ph: b.h, pl: b.l, pc: b.c, str, sp, sm, adx });
    return [adx, dip, dim];
  }
}
class TaPsarRun {   // ParabolicSAR: from the fourth bar; the SAR, reversing to the extreme point when price crosses it
  constructor(acc, step, max) { this.acc = acc; this.inc = step; this.max = max; this.i = 0; this.h = new TaBuf(4); this.l = new TaBuf(4); this.s = null; }
  step(b, commit = true) {
    this.h.push(b.h); this.l.push(b.l);
    const H = (k) => this.h.at(k), Lo = (k) => this.l.at(k);
    let s = this.s && { ...this.s }, value = null;
    if (this.i === 3) {   // MAX/MIN over CurrentBar (3) bars; long when this high is above the first bar's
      const hi = Math.max(H(0), H(1), H(2)), lo = Math.min(Lo(0), Lo(1), Lo(2)), long = H(0) > H(3);
      s = { long, xp: long ? hi : lo, af: this.acc, prev: 0, value: 0 };
      value = s.value = s.xp + (long ? -1 : 1) * ((hi - lo) * s.af);
    } else if (this.i > 3) {
      let t = s.value + s.af * (s.xp - s.value);
      if (s.long) { const low = Math.min(t, Lo(0), Lo(1)); if (Lo(0) > low) t = low; }   // TodaySAR
      else { const high = Math.max(t, H(0), H(1)); if (H(0) < high) t = high; }
      for (let x = 1; x <= 2; x++) { if (s.long) { if (t > Lo(x)) t = Lo(x); } else if (t < H(x)) t = H(x); }
      value = s.prev = t;
      if (s.long ? H(0) > s.xp : Lo(0) < s.xp) { s.xp = s.long ? H(0) : Lo(0); s.af = Math.min(this.max, s.af + this.inc); }
      if (s.long ? Lo(0) < t || Lo(1) < t : H(0) > t || H(1) > t) {   // Reverse()
        let r = s.xp;
        if (s.long ? s.prev > Lo(0) : s.prev < H(0)) { s.long = !s.long; s.af = this.acc; s.xp = s.long ? H(0) : Lo(0); s.prev = r; }
        else r = s.prev;
        value = r;
      }
      s.value = value;
    }
    if (commit) { this.i += 1; this.s = s; this.h.trim(); this.l.trim(); } else { this.h.pop(); this.l.pop(); }
    return [value];
  }
}
class TaSuperTrendRun {   // SuperTrend (TradingView's; NinjaTrader has none): hl2 +- factor * ATR (Wilder's, its SMA first); [line, direction -1 up 1 down]
  constructor(n, factor) { this.n = n; this.f = factor; this.i = 0; this.sum = 0; this.atr = null; this.pc = NaN; this.up = null; this.lo = null; this.st = null; }
  step(b, commit = true) {
    const n = this.n, tr = this.i === 0 ? b.h - b.l : Math.max(b.h - b.l, Math.abs(b.h - this.pc), Math.abs(b.l - this.pc));
    let sum = this.sum, atr = null, up = null, lo = null, dir = null, st = null;
    if (this.i < n) { sum += tr; if (this.i === n - 1) atr = sum / n; }
    else atr = (this.atr * (n - 1) + tr) / n;
    if (atr != null) {
      const src = (b.h + b.l) / 2, pu = this.up ?? 0, pl = this.lo ?? 0;
      up = src + this.f * atr;
      lo = src - this.f * atr;
      lo = lo > pl || this.pc < pl ? lo : pl;
      up = up < pu || this.pc > pu ? up : pu;
      if (this.atr == null) dir = 1;
      else if (this.st === pu) dir = b.c > up ? -1 : 1;
      else dir = b.c < lo ? 1 : -1;
      st = dir === -1 ? lo : up;
    }
    if (commit) Object.assign(this, { i: this.i + 1, sum, atr, pc: b.c, up, lo, st });
    return [st, dir];
  }
}
// Ichimoku (TradingView's; NinjaTrader has none): conversion and base lines (the midpoints of the last 9 and 26 bars),
// Span A (their mean) and Span B (the 52-bar midpoint) drawn displacement - 1 bars ahead, the close drawn as many bars
// back. [conversion, base, span A and B as drawn at this bar, span A and B made at this bar]
class TaIchimokuRun {
  constructor(conv, base, span, disp) {
    this.p = [conv, base, span]; this.shift = Math.max(0, disp - 1); this.i = 0;
    const keep = Math.max(conv, base, span);
    this.h = new TaBuf(keep); this.l = new TaBuf(keep); this.a = new TaBuf(this.shift + 1); this.b = new TaBuf(this.shift + 1);
  }
  mid(n) { return this.i + 1 < n ? null : (taMax(this.h, n) + taMin(this.l, n)) / 2; }
  step(b, commit = true) {
    this.h.push(b.h); this.l.push(b.l);
    const [conv, base, span] = this.p, t = this.mid(conv), k = this.mid(base), sb = this.mid(span), sa = t == null || k == null ? null : (t + k) / 2;
    this.a.push(sa); this.b.push(sb);
    const shown = (buf) => (this.i >= this.shift ? buf.at(this.shift) : null);
    const out = [t, k, shown(this.a), shown(this.b), sa, sb];
    if (commit) { this.i += 1; for (const x of [this.h, this.l, this.a, this.b]) x.trim(); }
    else for (const x of [this.h, this.l, this.a, this.b]) x.pop();
    return out;
  }
}

// ---- volume ---------------------------------------------------------------------------------------------------------------
class TaObvRun {   // OBV: the running total of volume, added on an up close, taken off on a down close
  constructor() { this.i = 0; this.v = 0; this.pc = 0; }
  step(b, commit = true) {
    const v = this.i === 0 ? 0 : b.c > this.pc ? this.v + (b.v || 0) : b.c < this.pc ? this.v - (b.v || 0) : this.v;
    if (commit) Object.assign(this, { i: this.i + 1, v, pc: b.c });
    return [v];
  }
}
class TaMfiRun {   // MFI: 100 - 100 / (1 + money flowing in / out) over n bars, typical price times volume; 50 on the first bar
  constructor(n) { this.n = n; this.pos = new TaBuf(n); this.neg = new TaBuf(n); this.i = 0; this.pt = 0; }
  step(b, commit = true) {
    const typ = (b.h + b.l + b.c) / 3, flow = typ * (b.v || 0);
    this.pos.push(this.i > 0 && typ > this.pt ? flow : 0);
    this.neg.push(this.i > 0 && typ < this.pt ? flow : 0);
    let v = 50;
    if (this.i > 0) {
      let p = 0, q = 0;
      for (let k = Math.min(this.i, this.n - 1); k >= 0; k--) { p += this.pos.at(k); q += this.neg.at(k); }
      v = taZero(q) ? 50 : 100 - 100 / (1 + p / q);
    }
    if (commit) { this.i += 1; this.pt = typ; this.pos.trim(); this.neg.trim(); } else { this.pos.pop(); this.neg.pop(); }
    return [v];
  }
}
class TaVolMaRun {   // VOLMA: an EMA of the volume
  constructor(n) { this.e = new NtEmaRun(n); }
  step(b, commit = true) { return [this.e.step(b.v || 0, commit)]; }
}
// The minute of the day a bar starts on its exchange's clock (the trade-date clock, setSessionClock), cached per hour.
let taClockFmt = null, taClockKey = '';
const taClockCache = new Map();
function taClockMinute(ms) {
  if (taClockKey !== sessionClockKey) { taClockFmt = new Intl.DateTimeFormat('en-US', { timeZone: sessionClockKey.split(' ')[0], hour: 'numeric', minute: 'numeric', hourCycle: 'h23' }); taClockKey = sessionClockKey; taClockCache.clear(); }
  const h = Math.floor(ms / 3600e3);
  let m = taClockCache.get(h);
  if (m == null) {
    if (taClockCache.size > 50000) taClockCache.clear();
    const p = Object.fromEntries(taClockFmt.formatToParts(h * 3600e3).map((x) => [x.type, Number(x.value)]));
    taClockCache.set(h, m = p.hour * 60 + p.minute);
  }
  return m + Math.floor((ms - h * 3600e3) / 60e3);
}
// The same bar is the one starting in the same minute of the day, on a seconds chart in the same second (its own slot).
// Tick, volume, range and Renko bars open at a trade's ms, which another day's bars rarely share to the second.
class TaRelVolRun {   // relative volume at time: the bar's volume over the mean of the same bar of the last n days
  constructor(n, seconds = false) { this.n = n; this.seconds = seconds; this.seen = new Map(); }
  step(b, commit = true) {
    const key = taClockMinute(b.t) * 60 + (this.seconds ? Math.floor(b.t / 1000) % 60 : 0), past = this.seen.get(key) || [];
    const avg = past.length ? past.reduce((s, x) => s + x, 0) / past.length : 0;
    if (commit) { const next = [...past, b.v || 0]; if (next.length > this.n) next.shift(); this.seen.set(key, next); }
    return [avg > 0 ? (b.v || 0) / avg : null];
  }
}

// ---- levels -----------------------------------------------------------------------------------------------------------------
// The period a bar's trade date falls in: the day, its Monday-to-Friday week (by its Monday), or its month.
function taPeriodKey(range, t) {
  const d = taTradeDay(t);
  if (range === 'Monthly') return d.slice(0, 7);
  if (range !== 'Weekly') return d;
  const x = new Date(`${d}T12:00:00Z`);
  return new Date(x.getTime() - ((x.getUTCDay() + 6) % 7) * 864e5).toISOString().slice(0, 10);
}
const TA_PIVOTS = {   // from the period before's high, low and close
  floor: (h, l, c) => { const p = (h + l + c) / 3, r = h - l; return [p, 2 * p - l, p + r, p + 2 * r, 2 * p - h, p - r, p - 2 * r]; },   // PP R1 R2 R3 S1 S2 S3
  camarilla: (h, l, c) => { const r = (h - l) * 1.1; return [c + r / 12, c + r / 6, c + r / 4, c + r / 2, c - r / 12, c - r / 6, c - r / 4, c - r / 2]; },   // R1-R4 S1-S4
};
class TaPivotsRun {   // Pivots / CamarillaPivots: each period's levels from the one before (none in the first)
  constructor(range, kind) { this.range = range; this.f = TA_PIVOTS[kind]; this.n = kind === 'floor' ? 7 : 8; this.cur = null; this.lv = null; }
  step(b, commit = true) {
    const k = taPeriodKey(this.range, b.t);
    let cur = this.cur, lv = this.lv;
    if (!cur || cur.k !== k) { if (cur) lv = this.f(cur.h, cur.l, cur.c); cur = { k, h: b.h, l: b.l, c: b.c }; }
    else cur = { k, h: Math.max(cur.h, b.h), l: Math.min(cur.l, b.l), c: b.c };
    if (commit) { this.cur = cur; this.lv = lv; }
    return lv || new Array(this.n).fill(null);
  }
}
// The regression channel over the last n closes (fewer at first): the least-squares line, and the band at width times
// the standard deviation of the closes from it. [line, upper, lower] at this bar, then its slope per bar and the band.
class TaRegressionRun {
  constructor(n, width) { this.n = n; this.w = width; this.x = new TaBuf(n); this.i = 0; }
  step(b, commit = true) {
    this.x.push(b.c);
    const m = Math.min(this.i + 1, this.n);
    let sx = 0, sy = 0, sxy = 0, sxx = 0;
    for (let j = 0; j < m; j++) { const y = this.x.at(m - 1 - j); sx += j; sy += y; sxy += j * y; sxx += j * j; }
    const den = m * sxx - sx * sx, slope = den === 0 ? 0 : (m * sxy - sx * sy) / den, icpt = (sy - slope * sx) / m;
    let ss = 0;
    for (let j = 0; j < m; j++) { const r = this.x.at(m - 1 - j) - (icpt + slope * j); ss += r * r; }
    const end = icpt + slope * (m - 1), band = this.w * Math.sqrt(ss / m);
    if (commit) { this.i += 1; this.x.trim(); } else this.x.pop();
    return [end, end + band, end - band, slope, band, m];
  }
}

// ---- the indicators: Studies ▾ rows, their settings, their lines (what an indicator alert can use) and how they draw ----
// params: [key, label, default, low, high, {step} for a decimal or {choices}]; lines: [key, label], the runner's first
// outputs in that order; pane: its own pane (true), on the price (false) or on the volume's scale ('vol'); plots: what
// is drawn ({i: output, kind: line | hist | dots | level, color: main | signal | up | down | #hex, width, dash, dir: only
// where output 1 is this, from: draw from output i on}); levels: dashed lines; range: a fixed scale; fmt: price | volume
// | decimals.
const MA = (name, long, len, kw = '') => ({ name, long, group: 'Moving averages', pane: false, params: [['len', 'Period', len, 1, 2000]],
  lines: [['v', name]], plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }], fmt: 'price', kw: `moving average ${kw}` });
const BAND = { pane: false, group: 'Bands and channels', fmt: 'price', lines: [['upper', 'Upper'], ['middle', 'Middle'], ['lower', 'Lower']],
  plots: [{ i: 0, kind: 'line', color: 'main', width: 1 }, { i: 1, kind: 'line', color: 'main', width: 1, dash: true }, { i: 2, kind: 'line', color: 'main', width: 1 }] };
const PIVOT_RANGE = ['range', 'Range', 'Daily', null, null, { choices: ['Daily', 'Weekly', 'Monthly'] }];
const IND_DEFS = {
  ema: MA('EMA', 'EMA (exponential moving average)', 21, 'exponential'),
  sma: { ...MA('SMA', 'SMA (simple moving average)', 50, 'simple'), params: [['len', 'Period', 50, 1, 2000]] },
  wma: { ...MA('WMA', 'WMA (weighted moving average)', 14, 'weighted linear'), params: [['len', 'Period', 14, 1, 500]] },
  hma: { ...MA('HMA', 'HMA (Hull moving average)', 21, 'hull'), params: [['len', 'Period', 21, 2, 500]] },
  vwma: { ...MA('VWMA', 'VWMA (volume-weighted moving average)', 14, 'volume weighted'), params: [['len', 'Period', 14, 1, 500]] },
  dema: { ...MA('DEMA', 'DEMA (double exponential)', 14, 'double exponential'), params: [['len', 'Period', 14, 1, 500]] },
  tema: { ...MA('TEMA', 'TEMA (triple exponential)', 14, 'triple exponential'), params: [['len', 'Period', 14, 1, 500]] },
  smma: { ...MA('SMMA', "SMMA (smoothed, Wilder's)", 14, 'smoothed wilder rma'), params: [['len', 'Period', 14, 1, 500]] },
  boll: { ...BAND, name: 'Bollinger', long: 'Bollinger Bands', kw: 'standard deviation volatility',
    params: [['len', 'Period', 14, 1, 500], ['dev', 'Std dev', 2, 0.1, 10, { step: 0.1 }]] },
  kelt: { ...BAND, name: 'Keltner', long: 'Keltner Channels', kw: 'atr range volatility',
    params: [['len', 'Period', 10, 1, 500], ['mult', 'Offset', 1.5, 0.1, 10, { step: 0.1 }]] },
  donch: { ...BAND, name: 'Donchian', long: 'Donchian Channels', kw: 'highest high lowest low breakout', params: [['len', 'Period', 14, 1, 500]],
    lines: [['upper', 'Upper'], ['mean', 'Mean'], ['lower', 'Lower']] },
  rsi: { name: 'RSI', long: 'RSI (relative strength index)', group: 'Oscillators', pane: true, kw: 'relative strength oscillator',
    params: [['len', 'Period', 14, 2, 500], ['smooth', 'Smooth', 3, 1, 200]], lines: [['rsi', 'RSI'], ['avg', 'Avg']] },
  macd: { name: 'MACD', long: 'MACD', group: 'Oscillators', pane: true, kw: 'moving average convergence divergence',
    params: [['fast', 'Fast', 12, 1, 500], ['slow', 'Slow', 26, 1, 500], ['smooth', 'Smooth', 9, 1, 500]],
    lines: [['macd', 'MACD'], ['avg', 'Signal'], ['diff', 'Histogram']] },
  stoch: { name: 'Stochastics', long: 'Stochastics', group: 'Oscillators', pane: true, kw: 'stochastic oscillator %k %d slow fast',
    params: [['k', 'Period K', 14, 1, 500], ['smooth', 'Smooth', 3, 1, 200], ['d', 'Period D', 7, 1, 200]], lines: [['k', '%K'], ['d', '%D']],
    plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }, { i: 1, kind: 'line', color: 'signal', width: 1 }], levels: [20, 80], range: [0, 100], fmt: 2 },
  stochrsi: { name: 'StochRSI', long: 'Stochastic RSI', group: 'Oscillators', pane: true, kw: 'stochastic rsi',
    params: [['len', 'Period', 14, 2, 500]], lines: [['v', 'StochRSI']], plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }],
    levels: [0.2, 0.5, 0.8], range: [0, 1], fmt: 2 },
  willr: { name: 'Williams %R', long: 'Williams %R', group: 'Oscillators', pane: true, kw: 'williams r percent range',
    params: [['len', 'Period', 14, 1, 500]], lines: [['v', '%R']], plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }],
    levels: [-25, -75], range: [-100, 0], fmt: 2 },
  cci: { name: 'CCI', long: 'CCI (commodity channel index)', group: 'Oscillators', pane: true, kw: 'commodity channel index',
    params: [['len', 'Period', 14, 1, 500]], lines: [['v', 'CCI']], plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }],
    levels: [200, 100, 0, -100, -200], fmt: 2 },
  mom: { name: 'Momentum', long: 'Momentum', group: 'Oscillators', pane: true, kw: 'momentum change',
    params: [['len', 'Period', 14, 1, 500]], lines: [['v', 'Momentum']], plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }], levels: [0], fmt: 'price' },
  roc: { name: 'ROC', long: 'ROC (rate of change)', group: 'Oscillators', pane: true, kw: 'rate of change momentum percent',
    params: [['len', 'Period', 14, 1, 500]], lines: [['v', 'ROC']], plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }], levels: [0], fmt: 2 },
  adx: { name: 'ADX', long: 'ADX / DMI (+DI, -DI)', group: 'Trend and volatility', pane: true, kw: 'average directional index dmi di directional movement trend strength',
    params: [['len', 'Period', 14, 1, 500]], lines: [['adx', 'ADX'], ['dip', '+DI'], ['dim', '-DI']],
    plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }, { i: 1, kind: 'line', color: 'up', width: 1 }, { i: 2, kind: 'line', color: 'down', width: 1 }],
    levels: [25, 75], fmt: 2 },
  atr: { name: 'ATR', long: 'ATR (average true range)', group: 'Trend and volatility', pane: true, kw: 'average true range volatility',
    params: [['len', 'Period', 14, 1, 500]], lines: [['v', 'ATR']] },
  psar: { name: 'Parabolic SAR', long: 'Parabolic SAR', group: 'Trend and volatility', pane: false, kw: 'psar stop and reverse trailing',
    params: [['acc', 'Acceleration', 0.02, 0.001, 1, { step: 0.001 }], ['inc', 'Step', 0.02, 0.001, 1, { step: 0.001 }], ['max', 'Max', 0.2, 0.001, 1, { step: 0.001 }]],
    lines: [['v', 'SAR']], plots: [{ i: 0, kind: 'dots', color: 'main', width: 2 }], fmt: 'price' },
  supertrend: { name: 'SuperTrend', long: 'SuperTrend', group: 'Trend and volatility', pane: false, kw: 'super trend atr trailing',
    params: [['len', 'ATR period', 10, 1, 500], ['mult', 'Factor', 3, 0.1, 20, { step: 0.1 }]], lines: [['v', 'SuperTrend']],
    plots: [{ i: 0, kind: 'line', color: 'up', width: 2, dir: -1 }, { i: 0, kind: 'line', color: 'down', width: 2, dir: 1 }], fmt: 'price' },
  ichimoku: { name: 'Ichimoku', long: 'Ichimoku Cloud', group: 'Trend and volatility', pane: false, kw: 'ichimoku kinko hyo cloud tenkan kijun senkou chikou',
    params: [['conv', 'Conversion', 9, 1, 500], ['base', 'Base', 26, 1, 500], ['span', 'Span B', 52, 1, 500], ['disp', 'Displacement', 26, 1, 500]],
    lines: [['conv', 'Conversion'], ['base', 'Base'], ['spanA', 'Span A'], ['spanB', 'Span B']],
    plots: [{ i: 0, kind: 'line', color: '#2962ff', width: 1 }, { i: 1, kind: 'line', color: '#b71c1c', width: 1 },
      { i: 2, kind: 'line', color: '#43a047', width: 1 }, { i: 3, kind: 'line', color: '#ef5350', width: 1 }], fmt: 'price' },
  obv: { name: 'OBV', long: 'OBV (on-balance volume)', group: 'Volume', pane: true, kw: 'on balance volume',
    params: [], lines: [['v', 'OBV']], plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }], fmt: 'volume' },
  mfi: { name: 'MFI', long: 'MFI (money flow index)', group: 'Volume', pane: true, kw: 'money flow index',
    params: [['len', 'Period', 14, 1, 500]], lines: [['v', 'MFI']], plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }], levels: [20, 80], range: [0, 100], fmt: 2 },
  volma: { name: 'Volume MA', long: 'Volume moving average', group: 'Volume', pane: 'vol', kw: 'volma average volume',
    params: [['len', 'Period', 14, 1, 500]], lines: [['v', 'Volume MA']], plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }], fmt: 'volume' },
  rvol: { name: 'Rel. volume', long: 'Relative volume (at time)', group: 'Volume', pane: true, kw: 'rvol relative volume at time',
    params: [['len', 'Days', 10, 1, 100]], lines: [['v', 'Rel. volume']], plots: [{ i: 0, kind: 'hist', color: 'main' }], levels: [1], fmt: 2 },
  cvd: { name: 'Cum. delta', long: 'Cumulative delta', group: 'Volume', pane: true, kw: 'order flow buy sell', params: [], lines: [] },
  pivots: { name: 'Pivots', long: 'Pivot points (floor)', group: 'Price and levels', pane: false, kw: 'pivot points floor support resistance daily weekly monthly',
    params: [PIVOT_RANGE], lines: [['pp', 'PP'], ['r1', 'R1'], ['r2', 'R2'], ['r3', 'R3'], ['s1', 'S1'], ['s2', 'S2'], ['s3', 'S3']],
    plots: [0, 1, 2, 3, 4, 5, 6].map((i) => ({ i, kind: 'level', color: i === 0 ? 'main' : i < 4 ? 'down' : 'up', width: 1 })), fmt: 'price' },
  camarilla: { name: 'Camarilla', long: 'Camarilla pivots', group: 'Price and levels', pane: false, kw: 'camarilla pivot points support resistance',
    params: [PIVOT_RANGE], lines: [['r1', 'R1'], ['r2', 'R2'], ['r3', 'R3'], ['r4', 'R4'], ['s1', 'S1'], ['s2', 'S2'], ['s3', 'S3'], ['s4', 'S4']],
    plots: [0, 1, 2, 3, 4, 5, 6, 7].map((i) => ({ i, kind: 'level', color: i < 4 ? 'down' : 'up', width: i % 4 === 2 || i % 4 === 3 ? 2 : 1 })), fmt: 'price' },
  linreg: { name: 'Regression', long: 'Linear regression channel', group: 'Price and levels', pane: false, kw: 'linear regression channel trend',
    params: [['len', 'Period', 35, 2, 1000], ['width', 'Width', 2, 0.1, 10, { step: 0.1 }]], lines: [['middle', 'Middle'], ['upper', 'Upper'], ['lower', 'Lower']],
    plots: [{ i: 0, kind: 'line', color: 'main', width: 2 }, { i: 1, kind: 'line', color: 'main', width: 1, dash: true }, { i: 2, kind: 'line', color: 'main', width: 1, dash: true }],
    fmt: 'price', window: true },
  cot: { name: 'COT', long: 'COT (Commitments of Traders)', group: 'Other', pane: true, kw: 'cftc positions commercials speculators', params: [], lines: [] },
};
const IND_MAX = 12;
const indDecimals = (step) => Math.max(0, Math.ceil(-Math.log10(step) - 1e-9));
// A stored indicator setting with every parameter present and in range, or null.
function indClean(c) {
  const def = c && IND_DEFS[c.type];
  if (!def) return null;
  const out = { type: c.type, color: /^#[0-9a-f]{6}$/i.test(c.color || '') ? c.color : null };
  for (const [k, , d, lo, hi, o] of def.params) {
    if (o?.choices) { out[k] = o.choices.includes(c[k]) ? c[k] : d; continue; }
    const step = o?.step || 1, v = Number((Math.round(Number(c[k]) / step) * step).toFixed(indDecimals(step)));
    out[k] = Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d;
  }
  if (c.type === 'cvd') out.reset = c.reset === 'none' ? 'none' : 'session';
  if (c.hidden === true) out.hidden = true;   // the legend's eye: kept, not drawn
  return out;
}
// A runner for a cleaned setting: step(bar, commit) returns the indicator's outputs for that bar. seconds: the chart's
// bars are seconds bars (15s, 30s, ...).
function indRunner(c, seconds = false) {
  const one = (r, field = 'c') => ({ step: (b, k) => [r.step(field ? b[field] : b, k)] });
  switch (c.type) {
    case 'ema': return one(new NtEmaRun(c.len));
    case 'sma': return one(new NtSmaRun(c.len));
    case 'wma': return one(new TaWmaRun(c.len));
    case 'hma': return one(new TaHmaRun(c.len));
    case 'vwma': return one(new TaVwmaRun(c.len), null);
    case 'dema': return one(new TaDemaRun(c.len));
    case 'tema': return one(new TaTemaRun(c.len));
    case 'smma': return one(new TaSmmaRun(c.len));
    case 'boll': return new TaBollingerRun(c.len, c.dev);
    case 'kelt': return new TaKeltnerRun(c.len, c.mult);
    case 'donch': return new TaDonchianRun(c.len);
    case 'rsi': { const r = new NtRsiRun(c.len, c.smooth); return { step: (b, k) => r.step(b.c, k) }; }
    case 'macd': { const r = new NtMacdRun(c.fast, c.slow, c.smooth); return { step: (b, k) => r.step(b.c, k) }; }
    case 'stoch': return new TaStochRun(c.k, c.smooth, c.d);
    case 'stochrsi': return new TaStochRsiRun(c.len);
    case 'willr': return new TaWilliamsRRun(c.len);
    case 'cci': return new TaCciRun(c.len);
    case 'mom': return new TaMomentumRun(c.len, false);
    case 'roc': return new TaMomentumRun(c.len, true);
    case 'adx': return new TaAdxRun(c.len);
    case 'atr': return one(new NtAtrRun(c.len), null);
    case 'psar': return new TaPsarRun(c.acc, c.inc, c.max);
    case 'supertrend': return new TaSuperTrendRun(c.len, c.mult);
    case 'ichimoku': return new TaIchimokuRun(c.conv, c.base, c.span, c.disp);
    case 'obv': return new TaObvRun();
    case 'mfi': return new TaMfiRun(c.len);
    case 'volma': return new TaVolMaRun(c.len);
    case 'rvol': return new TaRelVolRun(c.len, seconds);
    case 'cvd': { const r = new CvdRun(c.reset); return { step: (b, k) => r.step(b, k) }; }
    case 'pivots': return new TaPivotsRun(c.range, 'floor');
    case 'camarilla': return new TaPivotsRun(c.range, 'camarilla');
    case 'linreg': return new TaRegressionRun(c.len, c.width);
    case 'cot': return cotRunner();
    default: return null;
  }
}
const indTitle = (c) => `${IND_DEFS[c.type].name}${IND_DEFS[c.type].params.length ? ` ${IND_DEFS[c.type].params.map(([k]) => c[k]).join(' ')}` : ''}${c.type === 'cvd' && c.reset === 'none' ? ' (no reset)' : ''}`;
