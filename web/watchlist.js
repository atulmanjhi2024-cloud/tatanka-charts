/* Tatanka Charts watchlist, as on TradingView: lists with sections, symbols added, removed, flagged and dragged
   into order, sortable columns, quotes that flash as they change, arrow keys through the list, a details panel,
   and TradingView's .txt list import and export. The lists live on the server (PUT /api/watchlists, sent back to
   every open app, each save with a newer version); quotes arrive over the socket. Symbols are TradingView's: ES1! is
   ES's front month. */
'use strict';

const WL_SECTION = '###';
const WL_FUT = /^([A-Z0-9]{1,6})1!$/;   // a root of 1-6, as the server takes an instrument (config._SYM)
const WL_SYM = /^[A-Za-z0-9_.:!&^=+\-*/() ]{1,120}$/;
const WL_FLAGS = ['red', 'orange', 'green', 'blue', 'purple'];
const WL_FX = new Set(['USD', 'EUR', 'GBP', 'JPY', 'CHF', 'CAD', 'AUD', 'NZD', 'CNH', 'HKD', 'SEK', 'NOK', 'MXN', 'SGD']);
const WL_ROOT_DEC = { NG: 3, SI: 3, HG: 4, SR3: 4, ZT: 4, ZF: 4, ZN: 4, ZB: 3, UB: 3, LE: 3, ZC: 2, ZS: 2, '6E': 5, '6B': 4, '6J': 7 };
const WL_NUMBER = /^\d+(\.\d+)?$/;
const WL_WAIT = { timeout: 15000 };   // a save of the lists, or a read of them, still unanswered then has failed (app.js api())
const wlBare = (s) => s.replace(/\b(?!FRED:|EIA:|DATA:)[A-Za-z0-9_]+:/g, '').trim();   // "CME_MINI:ES1!" -> "ES1!"; FRED:, EIA:, DATA: stay
const wlTokens = (s) => (wlBare(s).match(/(?:FRED:|EIA:|DATA:)?[A-Za-z0-9_.!&^=]+/g) || []).filter((t) => !WL_NUMBER.test(t));
// A symbol as typed, written as the server does (watchlists._clean_item): in capitals, an exchange prefix and a FRED:,
// EIA: or DATA: series too, as every quote is keyed ("spy/fred:dgs10" -> "SPY/FRED:DGS10", "nasdaq:aapl" -> "NASDAQ:AAPL").
const wlNorm = (t) => t.toUpperCase();
const wlSeries = (src) => src === 'fred' || src === 'eia' || src === 'data';   // a series: a value as of a date
const wlRoot = (t) => { const m = WL_FUT.exec(t); return m ? m[1] : t; };        // a future's root ("ES" for ES1!), else the symbol
// The key its quote has (watchlists.quote_key): the symbol, so ZS1! (soybeans) and ZS (Zscaler) each have their own;
// a chart future running here (live: the instruments) owns its bare root, as on the chart: "ES" is ES1!'s.
const wlKey = (t, live) => (live?.[t] ? `${t}1!` : t);
// A root only a future has (only: the hello's only_futures, 6E, ZB), alone or in a formula, written as that future, as
// the server saves it (watchlists._as_future): "ZB" -> "ZB1!", "CBOT:ZB" -> "CBOT:ZB1!", "6E/6B" -> "6E1!/6B1!".
const wlAsFuture = (s, only) => s.replace(/(?:FRED:|EIA:|DATA:)?[A-Za-z0-9_.!&^=]+/g, (t, i) => (only?.has(t) && s[i + t.length] !== ':' ? `${t}1!` : t));
const wlIsFormula = (s) => /[+\-*/()]/.test(wlBare(s));
const wlIsSection = (s) => s.startsWith(WL_SECTION);

// + - * / and parentheses over symbols and numbers ("AAPL+MSFT", "SKEW/VIX", "FRED:DGS10-FRED:DGS2"); NaN when a part
// has no value. Its parts are wlTokens' (a series keeps its FRED:, EIA: or DATA:, as its quote is keyed).
function wlEval(expr, val) {
  const toks = expr.match(/(?:FRED:|EIA:|DATA:)?[A-Za-z0-9_.!&^=]+|[+\-*/()]/g) || [];
  let i = 0;
  const atom = () => {
    const t = toks[i++];
    if (t === '(') { const v = sum(); i++; return v; }
    if (t === '-') return -atom();
    if (t === '+') return atom();
    if (t == null) return NaN;
    return WL_NUMBER.test(t) ? Number(t) : val(t);
  };
  const prod = () => {
    let v = atom();
    while (toks[i] === '*' || toks[i] === '/') { const op = toks[i++], r = atom(); v = op === '*' ? v * r : v / r; }
    return v;
  };
  function sum() {
    let v = prod();
    while (toks[i] === '+' || toks[i] === '-') { const op = toks[i++], r = prod(); v = op === '+' ? v + r : v - r; }
    return v;
  }
  const v = sum();
  return i === toks.length && Number.isFinite(v) ? v : NaN;
}

const wlNF = new Map();
function wlNum(v, d) {   // d: decimals, or {bond: tick} for 32nds
  if (v == null || !Number.isFinite(v)) return '';
  if (d && typeof d === 'object') return bond32(v, d.bond);
  let f = wlNF.get(d);
  if (!f) wlNF.set(d, f = new Intl.NumberFormat('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }));
  return f.format(v);
}
const wlSettleFmt = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
function wlSettleTime(t) {   // when a future's settlement was set: "Oct 20, 3:00 PM CT"
  return `${wlSettleFmt.format(new Date(t))} CT`;
}
function wlVol(v) {
  if (v == null || !Number.isFinite(v)) return '';
  const a = Math.abs(v);
  return a >= 1e9 ? `${(v / 1e9).toFixed(2)}B` : a >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : a >= 1e3 ? `${(v / 1e3).toFixed(2)}K` : String(Math.round(v));
}

function createWatchlist(env) {
  const root = env.root, $q = (s) => root.querySelector(s);
  const scroll = $q('.wl-scroll'), body = $q('.wl-body'), head = $q('.wl-head'), detail = $q('#wl-detail');
  const readJson = (k, d) => { try { return JSON.parse(env.local.get(k, '')) ?? d; } catch { return d; } };
  const W = {
    doc: null, listId: env.local.get('wlList', ''), sel: -1,
    sort: readJson('wlSort', null),                                   // { col, dir: 1 | -1 }
    cols: { chg: true, pct: true, vol: false, ...readJson('wlCols', {}) },
    collapsed: readJson('wlCollapsed', {}),                           // list id -> [section names]
    detailOpen: env.local.get('wlDetail', matchMedia('(max-width: 760px)').matches ? '0' : '1') !== '0',
    editing: false, rows: new Map(), secs: new Map(), byKey: new Map(), press: null, drag: null, pending: null,
    noClick: false, addAt: null,                                      // Add symbol here…: { list, sec, n } (its section header, nthOf)
    base: null, saving: false, again: false, heard: null,             // the server's lists the edits are made on; see save()
    det: new Map(),                                                   // symbol -> {at, data}: the details panel's numbers
    detailSym: null,                                                  // the symbol the details panel shows
    lastList: env.local.get('wlLastList', ''),                        // the list to go back to from a flagged one
  };

  // A flagged list, as on TradingView: every symbol flagged that color, from all the lists (each once). Read-only: its
  // symbols come and go with their flags.
  const flagged = (color) => {
    const seen = new Set(), its = [];
    for (const l of W.doc.lists) {
      for (const it of l.items) {
        const b = wlBare(it);
        if (!wlIsSection(it) && W.doc.flags?.[b] === color && !seen.has(b)) { seen.add(b); its.push(it); }
      }
    }
    return { id: `flag:${color}`, name: `${color[0].toUpperCase()}${color.slice(1)} flags`, items: its, virtual: true };
  };
  const list = () => W.doc && (W.listId?.startsWith('flag:') && WL_FLAGS.includes(W.listId.slice(5)) ? flagged(W.listId.slice(5))
    : W.doc.lists.find((l) => l.id === W.listId) || W.doc.lists[0]);
  const flagView = () => !!list()?.virtual;
  const items = () => list()?.items || [];
  const name = (sym) => {
    const b = wlBare(sym), c = chartFor(sym);
    return W.doc?.names?.[b] || (c && env.instruments()[c]?.name) || '';
  };
  // The chart symbol: a live chart's root for "ES1!" (or "ES"); any other single symbol charts from free data (Yahoo,
  // FRED); a formula doesn't chart.
  function chartFor(sym) {
    const b = wlBare(sym), k = wlRoot(b);
    if (wlIsFormula(b) || wlIsSection(sym) || wlTokens(b).length !== 1) return null;
    return env.instruments()[k] ? k : b;
  }
  const quoteKey = (t) => wlKey(t, env.instruments());   // its quote's key, as the server has it
  const isLive = (c) => !!(c && env.instruments()[c]);
  function decimals(sym, v) {
    const c = chartFor(sym), b = wlBare(sym), m = WL_FUT.exec(b);
    if (bondTick(isLive(c) ? c : m?.[1])) return { bond: isLive(c) ? env.instruments()[c].tick_size : bondTick(m[1]) };   // 32nds
    if (isLive(c)) return env.instruments()[c].decimals ?? 2;
    if (m && WL_ROOT_DEC[m[1]] != null) return WL_ROOT_DEC[m[1]];
    if (/^[A-Z]{6}$/.test(b) && WL_FX.has(b.slice(0, 3)) && WL_FX.has(b.slice(3))) return b.endsWith('JPY') ? 3 : 5;
    return v != null && Math.abs(v) < 1 ? 4 : 2;
  }
  function quoteOf(sym) {
    const Q = env.quotes();
    if (!wlIsFormula(sym)) return Q[quoteKey(wlBare(sym))] || null;
    const b = wlBare(sym), last = wlEval(b, (t) => Q[quoteKey(t)]?.last ?? NaN);
    if (!Number.isFinite(last)) return null;
    const prev = wlEval(b, (t) => Q[quoteKey(t)]?.prev ?? NaN), chg = Number.isFinite(prev) ? last - prev : null;
    const rts = wlTokens(sym).map((t) => Q[quoteKey(t)]?.rt);   // as current as its least current part
    return { last, prev: Number.isFinite(prev) ? prev : null, chg, pct: chg != null && prev ? chg / Math.abs(prev) * 100 : null, vol: null,
      ts: Math.min(...wlTokens(sym).map((t) => Q[quoteKey(t)]?.ts ?? 0)), formula: true,
      rt: ['eod', 'closed', 'delayed', 'sim', 'live'].find((x) => rts.includes(x)) };
  }
  // How current a quote is (the server says: live, delayed, closed, eod or sim): the marker after the symbol.
  const RT = { live: ['●', 'Live'], delayed: ['D', 'Delayed'], closed: ['◐', 'Not trading now'], eod: ['E', 'End-of-day data'], sim: ['S', 'Simulated'] };
  const SRC = { ninjatrader: 'NinjaTrader', ibkr: 'IBKR', alpaca: 'Alpaca (IEX)', coinbase: 'Coinbase', yahoo: 'Yahoo', fred: 'FRED', eia: 'EIA', data: 'your data', sim: 'simulated' };
  function rtText(q) {
    if (!q?.rt || !RT[q.rt]) return '';
    const age = Math.max(0, Date.now() - (q.ts || 0)), min = Math.round(age / 60000);
    const when = q.rt === 'delayed' && q.ts && min >= 1 ? `: the last price is ${min} min old` : q.rt === 'closed' && q.ts
      ? `: last price ${ax.wkHm.format(q.ts)} ${zs(q.ts)}` : '';   // the chosen time zone (app.js)
    return `${RT[q.rt][1]}${when}${SRC[q.src] ? ` (${SRC[q.src]})` : q.formula ? ' (its parts)' : ''}`;
  }
  // [{ name, head (item index, -1 for symbols above the first section), rows: [item indexes] }]
  function sections() {
    const out = [{ name: null, head: -1, rows: [] }];
    items().forEach((it, i) => { if (wlIsSection(it)) out.push({ name: it.slice(WL_SECTION.length), head: i, rows: [] }); else out.at(-1).rows.push(i); });
    return out.filter((s, k) => k > 0 || s.rows.length);
  }
  const isClosed = (sec) => sec.head >= 0 && (W.collapsed[list().id] || []).includes(sec.name);

  // ---- rendering ---------------------------------------------------------------------------------------------
  function setCols() {
    const weight = { chg: 0.9, pct: 0.85, vol: 0.85 }, shown = ['chg', 'pct', 'vol'].filter((c) => W.cols[c]);
    root.style.setProperty('--wl-cols', ['minmax(0, 1fr)', 'minmax(0, 1.2fr)', ...shown.map((c) => `minmax(0, ${weight[c]}fr)`)].join(' '));
    root.classList.toggle('wide', shown.length > 2);   // four number columns: a smaller font fits them
    for (const c of ['chg', 'pct', 'vol']) root.classList.toggle(`hide-${c}`, !W.cols[c]);
    head.querySelectorAll('[data-col]').forEach((s) => {
      s.classList.toggle('asc', W.sort?.col === s.dataset.col && W.sort.dir > 0);
      s.classList.toggle('desc', W.sort?.col === s.dataset.col && W.sort.dir < 0);
    });
  }
  function rowEl(i, sym) {
    const r = document.createElement('div'), b = wlBare(sym), nm = name(sym);
    r.className = 'wl-row';
    r.dataset.i = i;
    r.classList.toggle('chartable', !!chartFor(sym));
    r.classList.toggle('sel', i === W.sel);
    r.title = nm ? `${b} · ${nm}` : b;
    r.innerHTML = '<span class="grip" aria-hidden="true">⋮⋮</span><span class="sym"><i class="flag"></i><span class="tk"></span><i class="rt"></i></span>'
      + '<span class="nm"></span><span class="c last"></span><span class="c chg"></span><span class="c pct"></span><span class="c vol"></span>';
    r.querySelector('.tk').textContent = b;
    r.querySelector('.nm').textContent = nm || '';
    const f = W.doc.flags?.[b];
    if (f) r.querySelector('.flag').classList.add(`f-${f}`);
    const cells = { last: r.querySelector('.last'), chg: r.querySelector('.chg'), pct: r.querySelector('.pct'), vol: r.querySelector('.vol'),
      rt: r.querySelector('.rt') };
    return { el: r, sym, cells, last: null };
  }
  function secEl(sec) {
    const s = document.createElement('div');
    s.className = 'wl-sec';
    s.dataset.i = sec.head;
    s.innerHTML = '<span class="grip" aria-hidden="true">⋮⋮</span><span class="caret">▾</span><span class="nm"></span><small class="n"></small>'
      + '<button type="button" class="sec-more" title="Section menu" aria-label="Section menu">⋯</button>';
    s.querySelector('.nm').textContent = sec.name;
    s.querySelector('.n').textContent = sec.rows.length;
    return s;
  }
  function flagDots() {   // the flagged lists, one click away: a dot per color by the list name
    const box = $q('#wl-flags');
    if (!box) return;
    box.replaceChildren(...WL_FLAGS.map((c) => {
      const fl = flagged(c), b = document.createElement('button');
      b.type = 'button';
      b.className = `f-${c}${fl.items.length ? ' has' : ''}${list()?.id === fl.id ? ' on' : ''}`;
      b.title = list()?.id === fl.id ? `${fl.name}: back to ${W.doc.lists.find((l) => l.id === W.lastList)?.name || 'your list'}` : `${fl.name} (${fl.items.length})`;
      b.setAttribute('aria-label', b.title);
      b.onclick = () => {
        if (list()?.id === fl.id) { switchList(W.doc.lists.some((l) => l.id === W.lastList) ? W.lastList : W.doc.lists[0].id); return; }
        if (!list()?.virtual) { W.lastList = list().id; env.local.set('wlLastList', W.lastList); }
        switchList(fl.id);
      };
      return b;
    }));
  }
  function render() {
    const lst = list();
    if (!lst) return;
    W.listId = lst.id;
    $q('#wl-name span').textContent = lst.name;
    flagDots();
    setCols();
    W.rows = new Map();
    W.secs = new Map();
    W.byKey = new Map();
    lst.items.forEach((it, i) => {
      if (wlIsSection(it)) return;
      W.rows.set(i, rowEl(i, it));
      for (const t of wlTokens(it)) {
        const k = quoteKey(t);
        if (!W.byKey.has(k)) W.byKey.set(k, []);
        W.byKey.get(k).push(i);
      }
    });
    for (const sec of sections()) if (sec.head >= 0) W.secs.set(sec.head, secEl(sec));
    if (!W.rows.has(W.sel)) W.sel = -1;
    W.rows.forEach((_, i) => paint(i));
    arrange();
    markActive();
    renderDetail();
  }
  const sortVal = (i, col) => {
    const r = W.rows.get(i);
    if (col === 'sym') return wlBare(r.sym);
    const q = quoteOf(r.sym);
    return q?.[col === 'pct' ? 'pct' : col] ?? null;
  };
  // Puts the elements in list order (sorted within each section when a column is sorted); collapsed rows leave.
  function arrange() {
    const kids = [];
    for (const sec of sections()) {
      const closed = isClosed(sec);
      if (sec.head >= 0) {
        const s = W.secs.get(sec.head);
        s.classList.toggle('closed', closed);
        kids.push(s);
      }
      if (closed) continue;
      let rows = sec.rows;
      if (W.sort) {
        const { col, dir } = W.sort;
        rows = rows.slice().sort((a, b) => {
          const x = sortVal(a, col), y = sortVal(b, col);
          if (x == null || y == null) return x == null ? (y == null ? 0 : 1) : -1;   // no quote: at the bottom
          return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
        });
      }
      for (const i of rows) kids.push(W.rows.get(i).el);
    }
    if (!kids.length) {
      const e = document.createElement('div');
      e.className = 'wl-empty';
      e.innerHTML = 'This list is empty.<br>Add symbols with <b>+</b> above.';
      kids.push(e);
    }
    if (kids.length !== body.children.length || kids.some((k, i) => k !== body.children[i])) body.replaceChildren(...kids);   // else left in place
    root.classList.toggle('sorted', !!W.sort);
  }
  function paint(i) {
    const r = W.rows.get(i);
    if (!r) return;
    const q = quoteOf(r.sym), c = r.cells;
    if (!q) {
      c.last.textContent = '—';
      c.last.className = 'c last nodata';
      c.chg.textContent = c.pct.textContent = c.vol.textContent = c.rt.textContent = '';
      return;
    }
    c.rt.textContent = RT[q.rt]?.[0] || '';
    c.rt.className = `rt ${q.rt || ''}`;
    c.rt.title = rtText(q);
    const d = decimals(r.sym, q.last), dir = q.chg > 0 ? 'up' : q.chg < 0 ? 'down' : '';
    if (r.last != null && q.last !== r.last) {   // flash the price the way it moved
      // A new animation name restarts it (two of each, taken in turn) with no forced layout; it fades an overlay's
      // opacity, which the compositor runs without repainting the row.
      r.fl = !r.fl;
      c.last.classList.remove('fl-up', 'fl-dn', 'fl-up2', 'fl-dn2');
      c.last.classList.add(`${q.last > r.last ? 'fl-up' : 'fl-dn'}${r.fl ? '2' : ''}`);
    }
    r.last = q.last;
    c.last.textContent = wlNum(q.last, d);
    c.last.classList.remove('nodata');
    c.chg.textContent = q.chg == null ? '' : `${q.chg > 0 ? '+' : ''}${wlNum(q.chg, d)}`;
    c.pct.textContent = q.pct == null ? '' : `${q.pct > 0 ? '+' : ''}${q.pct.toFixed(2)}%`;
    c.chg.className = `c chg ${dir}`;
    c.pct.className = `c pct ${dir}`;
    c.vol.textContent = wlVol(q.vol);
  }
  function markActive() {
    const cur = env.current();
    W.rows.forEach((r) => r.el.classList.toggle('active', chartFor(r.sym) === cur));
    if (W.sel < 0) renderDetail();
  }
  let resortTimer = 0;
  function onQuotes(qs) {
    const touched = new Set();
    for (const q of qs) for (const i of W.byKey.get(q.s) || []) touched.add(i);
    touched.forEach(paint);
    if (touched.has(detailIndex())) renderDetail(true);
    if (W.sort && W.sort.col !== 'sym' && touched.size && !resortTimer && !W.drag) {
      const resort = () => {   // not while the mouse is down on a row: moved under it, the row would get no click
        if (W.press) { resortTimer = setTimeout(resort, 300); return; }
        resortTimer = 0;
        arrange();
      };
      resortTimer = setTimeout(resort, 1500);   // live re-sort, but calmly
    }
  }

  // ---- details panel ------------------------------------------------------------------------------------------
  function detailIndex() {
    if (W.rows.has(W.sel)) return W.sel;
    const cur = env.current();
    for (const [i, r] of W.rows) if (chartFor(r.sym) === cur) return i;
    return -1;
  }
  const btn = (label, fn, cls) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    if (cls) b.className = cls;
    b.onclick = (e) => { e.stopPropagation(); fn(); };
    return b;
  };
  function flagPicker(sym, after) {
    const box = document.createElement('span');
    box.className = 'flag-pick';
    const cur = W.doc.flags?.[wlBare(sym)];
    for (const f of [...WL_FLAGS, null]) {
      const b = btn('', () => { setFlag(sym, f); after?.(); }, f ? `f-${f}` : 'f-none');
      b.title = f ? `Flag ${f}` : 'No flag';
      b.classList.toggle('on', (cur || null) === f);
      box.append(b);
    }
    return box;
  }
  // quote: a new quote (or the server's numbers) of the symbol shown changes its price, change and stats in place: the
  // header and the flag buttons stay, as a browser gives no click to a button replaced while the mouse is down on it.
  function renderDetail(quote) {
    const i = detailIndex(), el = detail;
    el.classList.toggle('folded', !W.detailOpen);
    const hd = document.createElement('div');
    hd.className = 'wd-hd';
    hd.onclick = () => { W.detailOpen = !W.detailOpen; env.local.set('wlDetail', W.detailOpen ? '1' : '0'); renderDetail(); };
    const fold = document.createElement('span');
    fold.className = 'wd-fold';
    fold.textContent = W.detailOpen ? '▾' : '▸';
    if (i < 0) {
      const s = document.createElement('span');
      s.className = 'wd-name';
      s.textContent = 'Select a symbol for its details';
      hd.append(fold, s);
      el.replaceChildren(hd);
      W.detailSym = null;
      return;
    }
    const sym = items()[i], b = wlBare(sym), q = quoteOf(sym), chart = chartFor(sym), d = decimals(sym, q?.last);
    const tk = document.createElement('b'), nm = document.createElement('span');
    tk.className = 'wd-sym';
    tk.textContent = b;
    nm.className = 'wd-name';
    nm.textContent = name(sym);
    hd.append(fold, tk, nm);
    const pxl = document.createElement('div'), meta = document.createElement('div'), act = document.createElement('div');
    pxl.className = 'wd-px';
    meta.className = 'wd-meta';
    act.className = 'wd-act';
    if (q) {
      const last = document.createElement('span'), chg = document.createElement('span');
      last.className = 'wd-last';
      last.textContent = wlNum(q.last, d);
      chg.className = `wd-chg ${q.chg > 0 ? 'up' : q.chg < 0 ? 'down' : ''}`;
      chg.textContent = q.chg == null ? '' : `${q.chg > 0 ? '+' : ''}${wlNum(q.chg, d)}${q.pct == null ? '' : `  ${q.pct > 0 ? '+' : ''}${q.pct.toFixed(2)}%`}`;   // a formula from 0: no %
      pxl.append(last, chg);
      const age = Date.now() - q.ts, when = q.ts ? ax.clock.format(q.ts) : '';
      const asOf = wlSeries(q.src) && q.ts ? `as of ${new Date(q.ts).toLocaleDateString('en-US', { timeZone: 'America/Chicago', month: 'short', day: 'numeric', year: 'numeric' })}` : '';
      meta.textContent = [rtText(q), q.prev != null && `${wlSeries(q.src) ? 'Before' : q.settle_t ? 'Settlement' : 'Prev close'} ${wlNum(q.prev, d)}${q.settle_t ? ` (${wlSettleTime(q.settle_t)})` : ''}`, q.vol != null && `Vol ${wlVol(q.vol)}`,
        q.formula ? 'computed from its parts' : '', asOf || (when && `${age > 120e3 ? 'last quote' : 'quote'} ${when} ${zs(q.ts)}`)].filter(Boolean).join(' · ');
    } else {
      meta.textContent = wlIsFormula(sym) ? 'Waiting for quotes on every part of this formula.'
        : WL_FUT.test(b) ? 'No quote yet: NinjaTrader quotes the futures your data connection carries, once the add-on is running.'
          : b.startsWith('FRED:') ? 'Asking FRED for this series…'
          : b.startsWith('EIA:') ? 'Asking EIA for this series (it needs a free API key: Connections > EIA)…'
          : b.startsWith('DATA:') ? 'Reading this series (Connections > Data, its symbol)…'
          : 'No quote yet. Stocks, ETFs, indices, FX and crypto come from IBKR, or Yahoo as the backup; a symbol Yahoo spells differently can be added in config/yahoo.json.';
    }
    const flagLbl = document.createElement('span');
    flagLbl.className = 'wd-l';
    flagLbl.textContent = 'Flag';
    const stats = statsEl(sym, details(sym), q, d);
    if (quote && W.detailSym === sym) { [pxl, meta, stats].forEach((x, k) => el.children[k + 1].replaceWith(x)); return; }
    act.append(flagLbl, flagPicker(sym, renderDetail));   // open, alert and remove: click, right-click, Del
    W.detailSym = sym;
    el.replaceChildren(hd, pxl, meta, stats, act);
  }
  // The panel's numbers from the server (/api/symbols/details), kept a minute per symbol.
  function details(sym) {
    const b = wlBare(sym), hit = W.det.get(b);
    if (!hit || (Date.now() - hit.at > 60e3 && !hit.loading)) {
      W.det.set(b, { at: hit?.at || 0, data: hit?.data || null, loading: true });
      env.api('GET', `/api/symbols/details?symbol=${encodeURIComponent(sym)}`)
        .then((data) => { W.det.set(b, { at: Date.now(), data }); if (items()[detailIndex()] === sym) renderDetail(true); })
        .catch(() => W.det.set(b, { at: Date.now(), data: hit?.data || null }));
    }
    return W.det.get(b)?.data || null;
  }
  function statsEl(sym, dt, q, dec) {
    const box = document.createElement('div');
    box.className = 'wd-stats';
    if (!dt) return box;
    const f = (v) => (v == null ? '—' : wlNum(v, dec)), last = q?.last;
    const span = (t, cls) => { const x = document.createElement('span'); x.textContent = t; if (cls) x.className = cls; return x; };
    const row = (label, ...vals) => {   // the label, then its values: a wrapped line starts under the first value
      const r = document.createElement('div'), v = span('', 'wd-vals');
      r.className = 'wd-row';
      v.append(...vals);
      r.append(span(label, 'wd-l'), v);
      box.append(r);
    };
    const range = (lo, hi) => {   // low, a bar with today's price on it, high
      const w = span('', 'wd-range'), bar = document.createElement('i'), dot = document.createElement('b');
      w.textContent = '';
      if (hi > lo && last != null) { dot.style.left = `${Math.min(100, Math.max(0, (last - lo) / (hi - lo) * 100))}%`; bar.append(dot); }
      w.append(span(f(lo)), bar, span(f(hi)));
      return w;
    };
    if (dt.day) { row('Day', range(dt.day.l, dt.day.h)); row('Open', span(f(dt.day.o), 'wd-v')); }
    if (dt.w52) row('52 wk', range(dt.w52[0], dt.w52[1]));
    const perf = Object.entries(dt.perf || {});
    if (perf.length) row('Perf', ...perf.map(([k, v]) => span(`${k} ${v > 0 ? '+' : ''}${v.toFixed(1)}%`, `wd-pf ${v > 0 ? 'up' : v < 0 ? 'down' : ''}`)));
    if (dt.contract) {   // a live future, once NinjaTrader said which contract
      const how = dt.roll === 'dates' ? "on NinjaTrader's dates" : !dt.roll || dt.roll === 'volume' ? 'by volume' : `fixed: ${dt.roll}`;
      row('Contract', span(`${dt.contract || '—'} · rolls ${how}`, 'wd-v'));
    }
    if (dt.earnings) row('Earnings', span(new Date(`${dt.earnings.slice(0, 10)}T12:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }), 'wd-v'));
    return box;
  }

  // ---- selection, keys ------------------------------------------------------------------------------------------
  // A symbol can be listed twice (in two sections: the first-run list's USDJPY), so a row is found again in lists
  // reloaded meanwhile as the n-th item with its text: nthOf(its, i) says which, findNth finds it (-1: gone).
  const nthOf = (its, i) => its.slice(0, i).filter((x) => x === its[i]).length;
  const findNth = (its, x, n) => { let i = -1; for (let k = 0; k <= n && (i = its.indexOf(x, i + 1)) >= 0; k++); return i; };
  // The selected row as its list, item and which of that item it is: found again in the lists that replace these
  // (another device's save above it), so Del, Enter and the details panel stay on the symbol selected.
  const selected = () => (W.rows.has(W.sel) ? { list: list().id, it: items()[W.sel], n: nthOf(items(), W.sel) } : null);
  const reselect = (s) => { W.sel = s && list()?.id === s.list ? findNth(items(), s.it, s.n) : -1; };
  function select(i, open) {
    W.sel = i;
    W.rows.forEach((r, k) => r.el.classList.toggle('sel', k === i));
    renderDetail();
    const c = W.rows.has(i) && chartFor(W.rows.get(i).sym);
    if (open && c) env.open(c);
  }
  const visibleRows = () => [...body.children].filter((e) => e.classList.contains('wl-row')).map((e) => Number(e.dataset.i));
  function step(d) {
    const vis = visibleRows();
    if (!vis.length) return;
    const k = vis.indexOf(W.sel), next = vis[k < 0 ? (d > 0 ? 0 : vis.length - 1) : Math.max(0, Math.min(vis.length - 1, k + d))];
    select(next, true);
    W.rows.get(next).el.scrollIntoView({ block: 'nearest' });
  }
  scroll.addEventListener('keydown', (e) => {
    if (e.target.closest('input')) return;
    const k = e.key;
    if (k === 'ArrowDown' || k === 'ArrowUp') step(k === 'ArrowDown' ? 1 : -1);
    else if ((k === 'Delete' || k === 'Backspace') && W.rows.has(W.sel)) remove(W.sel);
    else if (k === 'Enter' && W.rows.has(W.sel)) select(W.sel, true);
    else return;
    e.preventDefault();
    e.stopPropagation();   // the chart's arrow keys and Delete stay off while the list has focus
  });

  // ---- edits (each saved whole to the server) ---------------------------------------------------------------
  // The server gives each save of the lists a version, which only grows. A save says the version its edits were made
  // on (base): made on an older one (another device saved meanwhile), it is refused (409), and this page merges its
  // edits into the lists as they are now (merged, below) and saves that. One save is on its way at a time: edits made
  // meanwhile go in the next, sent once it is answered. What the server sends meanwhile (a save's broadcast, this
  // page's own too, or a reconnect's hello: heard, below) waits for the answer, then the newer of the two is shown:
  // newer than the answer, it is another device's save made after this one. Lists older than the ones here are
  // dropped: a broadcast that arrives after a newer save's answer would undo the edits since. A save with no answer
  // in 15 s has failed (WL_WAIT); one that fails shows the error and the server's lists, unless edits were made while
  // it was on its way or while the lists were read: those, and its own, are merged into them and saved, not dropped.
  const clone = (d) => JSON.parse(JSON.stringify(d));
  const known = (d) => Number.isFinite(d?.version);   // lists without one: from a server before versions
  const newer = (a, b) => known(a) && known(b) && a.version > b.version;
  const latest = (doc) => { const h = W.heard; W.heard = null; return newer(h, doc) ? h : doc; };   // doc, or a newer one heard
  async function save() {
    if (W.saving) { W.again = true; return; }
    W.saving = true;
    try {
      for (let tries = 1; ;) {
        W.again = false;
        let doc;
        try {
          doc = await env.api('PUT', '/api/watchlists', { ...W.doc, base: W.base?.version }, WL_WAIT);
        } catch (e) {   // another device saved first (409), or this save failed with edits made since it was sent
          if ((e.status !== 409 && !W.again) || ++tries > 5) throw e;
          mergeInto(W.base, latest(await env.api('GET', '/api/watchlists', null, WL_WAIT)));
          continue;
        }
        const now = latest(doc);
        if (!W.again) {   // shown as the server keeps it (a new list's id), unless a row is being dragged (the drop saves)
          if (W.drag && now === doc) W.base = clone(doc); else setDoc(now);
          break;
        }
        if (now !== doc) mergeInto(doc, now);   // edited meanwhile: those edits, in the newest lists
        else W.base = clone(now);
      }
    } catch (e) {
      env.fail(e);
      try {
        const now = latest(await env.api('GET', '/api/watchlists', null, WL_WAIT));
        if (W.again) mergeInto(W.base, now); else setDoc(now);   // edits not sent yet (made while they were read): saved below
      } catch { /* keep */ }
    } finally {
      W.saving = false;
      W.heard = null;
    }
    if (W.again) save();
  }
  function mergeInto(base, now) {   // the edits on screen (made on base) into the server's newer lists, shown
    if (W.drag) endDrag(false);   // its rows may have moved
    const sel = selected();
    W.doc = merged(base, W.doc, now);
    W.base = clone(now);
    reselect(sel);
    render();
  }
  // This page's edits (ours: the lists on screen, made on base) in the server's newer lists (theirs). A list, a flag
  // or a symbol's name changed on one side only: that side's; changed on both: this page's, the later save. A list
  // deleted on one side and changed on the other stays. In a list both changed, this page's order, with the symbols and
  // sections the other added (after the item before them there) and those it removed, each in its own section.
  function merged(base, ours, theirs) {
    const byId = (d) => new Map(d.lists.map((l) => [l.id, l]));
    const B = byId(base), O = byId(ours), T = byId(theirs);
    const changed = (l, b) => !b || l.name !== b.name || JSON.stringify(l.items) !== JSON.stringify(b.items);
    const lists = [];
    for (const id of new Set([...O.keys(), ...T.keys()])) {   // this page's order, then the lists added there
      const b = B.get(id), o = O.get(id), t = T.get(id);
      if (o && t) lists.push({ id, name: o.name !== b?.name ? o.name : t.name, items: mergedItems(b?.items || [], o.items, t.items) });
      else if (changed(o || t, b)) lists.push(o || t);   // added on one side, or deleted on one and changed on the other
    }
    const keyed = (k) => {
      const b = base[k] || {}, o = ours[k] || {}, t = theirs[k] || {}, out = {};
      for (const s of new Set([...Object.keys(b), ...Object.keys(o), ...Object.keys(t)])) {
        const v = o[s] !== b[s] ? o[s] : t[s];
        if (v != null) out[s] = v;
      }
      return out;
    };
    return { ...theirs, lists: lists.length ? lists : theirs.lists, flags: keyed('flags'), names: keyed('names') };
  }
  // Each item of a list as its section and itself ("###FOREX#1\nUSDJPY": USDJPY in the first section named FOREX), so a
  // symbol listed in two sections is two, each added, removed and placed in its own. A section renamed on one side
  // (its header not in base, nor base's at its place there) is still base's.
  const heads = (its) => {
    const n = new Map();
    return its.filter(wlIsSection).map((x) => { n.set(x, (n.get(x) || 0) + 1); return `${x}#${n.get(x)}`; });
  };
  function tagged(its, base) {
    const own = heads(its), was = heads(base);
    const key = own.map((k, i) => (!was.includes(k) && was[i] && !own.includes(was[i]) ? was[i] : k));
    let s = -1;
    return its.map((x) => (wlIsSection(x) ? key[++s] : `${key[s] ?? ''}\n${x}`));
  }
  function mergedItems(bItems, oItems, tItems) {
    const same = (x, y) => x.length === y.length && x.every((v, i) => v === y[i]);
    if (same(oItems, bItems)) return tItems.slice();
    if (same(tItems, bItems)) return oItems.slice();
    const b = tagged(bItems, bItems), o = tagged(oItems, bItems), t = tagged(tItems, bItems);
    const was = new Map(b.map((x, i) => [x, bItems[i]])), text = new Map([...was, ...t.map((x, i) => [x, tItems[i]])]);
    o.forEach((x, i) => { if (!text.has(x) || oItems[i] !== was.get(x)) text.set(x, oItems[i]); });   // renamed here: this page's name
    const count = (a) => a.reduce((m, x) => m.set(x, (m.get(x) || 0) + 1), new Map());
    const cb = count(b), co = count(o), ct = count(t), out = o.slice();
    for (const x of new Set([...b, ...t])) {
      const nb = cb.get(x) || 0, no = co.get(x) || 0, nt = ct.get(x) || 0;
      if (no !== nb || nt === nb) continue;   // changed here (this page's stands), or not there
      for (let k = no; k > nt; k--) out.splice(out.lastIndexOf(x), 1);
      for (let k = no; k < nt; k++) {   // its k-th one there goes after the nearest item before it there that is here
        let i = -1;
        for (let c = 0; c <= k; c++) i = t.indexOf(x, i + 1);
        let at = -1;
        while (--i >= 0 && (at = out.indexOf(t[i])) < 0);
        out.splice(at + 1, 0, x);
      }
    }
    return out.map((x) => text.get(x));
  }
  function commit() { render(); save(); }
  function remove(i) {
    const it = items()[i];
    if (it == null || wlIsSection(it)) return;
    if (flagView()) { setFlag(it, null); env.toast(`${wlBare(it)}: flag removed`); return; }   // off the flagged list
    items().splice(i, 1);
    W.sel = W.sel === i ? -1 : W.sel > i ? W.sel - 1 : W.sel;
    commit();
    env.toast(`Removed ${wlBare(it)}`);
  }
  function setFlag(sym, f) {
    W.doc.flags = { ...(W.doc.flags || {}) };
    if (f) W.doc.flags[wlBare(sym)] = f; else delete W.doc.flags[wlBare(sym)];
    commit();
  }
  function addSymbols(text, names = {}) {
    if (flagView()) { env.toast('A flagged list shows the symbols flagged that color: flag one in any list'); return; }
    const want = text.split(/[,\s]+/).map((t) => t.trim()).filter(Boolean).map(wlNorm);
    const bad = want.filter((t) => !wlIsSection(t) && (!WL_SYM.test(t) || !wlTokens(t).length));
    const only = env.onlyFutures?.();   // a bare ZB as ZB1!, as the server saves it: so it is found in its section
    const good = want.filter((t) => !bad.includes(t)).map((t) => (wlIsSection(t) ? t : wlAsFuture(t, only)));
    if (bad.length) env.toast(`Not a symbol: ${bad.join(', ')}`);
    if (!good.length) return;
    const its = items(), here = W.addAt?.list === list().id ? findNth(its, W.addAt.sec, W.addAt.n) : -1;   // found again: the list may have reloaded
    let at = here >= 0 ? here + blockOf(here) : W.rows.has(W.sel) ? W.sel + 1 : its.length;   // its section's end, else below the selected row
    const secOf = (i) => { let h = -1; for (let k = 0; k < i; k++) if (wlIsSection(its[k])) h = k; return h; };
    const sec = secOf(at), end = its.findIndex((it, k) => k > sec && k >= at && wlIsSection(it));
    const inSec = new Set(its.slice(sec + 1, end < 0 ? its.length : end).map(wlBare));
    const added = [];
    for (const t of good) {
      if (inSec.has(wlBare(t))) continue;
      its.splice(at, 0, t);
      inSec.add(wlBare(t));
      added.push(wlBare(t));
      W.sel = at;
      at += 1;
    }
    W.addAt = null;
    if (!added.length) { env.toast('Already in this section'); return; }
    for (const b of added) if (names[b] && !W.doc.names?.[b]) W.doc.names = { ...(W.doc.names || {}), [b]: names[b] };
    commit();
    W.rows.get(W.sel)?.el.scrollIntoView({ block: 'nearest' });
    env.toast(`Added ${added.join(', ')}`);
  }
  async function addSection(above, nth = 0) {   // above: the symbol (its nth copy) it goes above, found once named (the list may have reloaded)
    if (flagView()) return;
    const n = await env.ask({ title: 'Add section', value: '', ok: 'Add', placeholder: 'e.g. ENERGY' });
    if (!n) return;
    const its = items(), at = above == null ? (W.rows.has(W.sel) ? W.sel : its.length) : findNth(its, above, nth);
    if (at < 0) return;
    its.splice(at, 0, WL_SECTION + n.toUpperCase());
    if (W.sel >= at) W.sel += 1;
    commit();
  }
  function moveBlock(from, n, to) {   // `to` is an index in the list as it is now: the block goes before it
    if (to >= from && to <= from + n) return false;
    const its = items(), block = its.splice(from, n);
    its.splice(to > from ? to - n : to, 0, ...block);
    return true;
  }
  const blockOf = (i) => {   // a row, or a section header with its rows
    const its = items();
    if (!wlIsSection(its[i])) return 1;
    let k = i + 1;
    while (k < its.length && !wlIsSection(its[k])) k++;
    return k - i;
  };

  // ---- lists ----------------------------------------------------------------------------------------------------
  function switchList(id) {
    W.listId = id;
    W.sel = -1;
    env.local.set('wlList', id);
    render();
  }
  const newId = () => `l${Math.random().toString(36).slice(2, 10)}`;
  async function newList(itemsIn, suggested) {
    const n = await env.ask({ title: itemsIn ? 'Import as a new list' : 'Create new list', value: suggested || '', ok: itemsIn ? 'Import' : 'Create' });
    if (!n) return;
    const id = newId();
    W.doc.lists.push({ id, name: n, items: itemsIn || [] });
    switchList(id);
    save();
  }
  // Rename and Delete find their list again by its id once answered: the lists may have reloaded meanwhile (a save
  // from another device, a reconnect).
  async function renameList() {
    const lst = list(), n = await env.ask({ title: 'Rename list', value: lst.name, ok: 'Rename' });
    const l = n && W.doc.lists.find((x) => x.id === lst.id);
    if (!l) return;
    l.name = n;
    commit();
  }
  function copyList() {
    const lst = list(), id = newId();
    W.doc.lists.push({ id, name: `${lst.name} copy`.slice(0, 60), items: lst.items.slice() });
    switchList(id);
    save();
  }
  async function deleteList() {
    const lst = list();
    if (W.doc.lists.length < 2) return;
    if (await env.ask({ title: `Delete "${lst.name}"?`, message: `${lst.items.filter((t) => !wlIsSection(t)).length} symbols. This cannot be undone.`, input: false, ok: 'Delete', danger: true }) == null) return;
    const left = W.doc.lists.filter((l) => l.id !== lst.id);
    if (!left.length || left.length === W.doc.lists.length) return;   // the last list now, or deleted elsewhere already
    W.doc.lists = left;
    switchList(W.doc.lists[0].id);
    save();
  }
  function importList() {
    const f = document.createElement('input');
    f.type = 'file';
    f.accept = '.txt,text/plain';
    f.onchange = async () => {
      const file = f.files[0];
      if (!file) return;
      const raw = (await file.text()).split(/[,\r\n]+/).map((t) => t.trim()).filter(Boolean);
      const ok = raw.filter((t) => wlIsSection(t) ? t.length > WL_SECTION.length : WL_SYM.test(t) && wlTokens(t).length);
      if (!ok.length) { env.toast('No symbols in that file'); return; }
      const n = raw.length - ok.length;
      if (n) env.toast(`Skipped ${n} ${n > 1 ? 'entries that are not symbols' : 'entry that is not a symbol'}`);
      newList(ok.slice(0, 2000), file.name.replace(/\.txt$/i, '').slice(0, 60));
    };
    f.click();
  }
  function exportList() {   // TradingView's format: one line, commas, ###SECTION markers
    const lst = list(), a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([lst.items.join(',')], { type: 'text/plain' }));
    a.download = `${lst.name.replace(/[\\/:*?"<>|]/g, '_')}.txt`;
    document.body.append(a);   // Firefox only follows a link that is in the page
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  function setSort(col) {
    const numeric = col !== 'sym';
    if (W.sort?.col !== col) W.sort = { col, dir: numeric ? -1 : 1 };
    else if (W.sort.dir === (numeric ? -1 : 1)) W.sort.dir = -W.sort.dir;
    else W.sort = null;   // third click: the list's own order
    env.local.set('wlSort', JSON.stringify(W.sort));
    setCols();
    arrange();
  }
  function setEditing(on) {
    if (on && flagView()) return;
    W.editing = on;
    root.classList.toggle('editing', on);
  }

  // ---- menus ----------------------------------------------------------------------------------------------------
  const at = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.bottom + 2]; };
  function listMenu() {
    const lst = list();
    env.menu(...at($q('#wl-name')), [
      ...W.doc.lists.map((l) => ({ label: l.name, check: l.id === lst.id, fn: () => switchList(l.id) })),
      'sep',
      ...WL_FLAGS.map((c) => { const f = flagged(c); return { label: `${f.name} (${f.items.length})`, check: f.id === lst.id, fn: () => switchList(f.id) }; }),
      'sep',
      { label: 'Create new list…', fn: () => newList() },
      { label: 'Rename…', fn: renameList, disabled: !!lst.virtual },
      { label: 'Make a copy', fn: copyList },
      { label: 'Delete list…', fn: deleteList, disabled: !!lst.virtual || W.doc.lists.length < 2 },
      'sep',
      { label: 'Import TradingView list (.txt)…', fn: importList },
      { label: 'Export list (.txt)', fn: exportList },
    ], $q('#wl-name'));
  }
  function moreMenu() {
    const col = (c, label) => ({ label, check: W.cols[c], fn: () => { W.cols[c] = !W.cols[c]; env.local.set('wlCols', JSON.stringify(W.cols)); setCols(); } });
    env.menu(...at($q('#wl-more')), [
      { label: 'Add section…', fn: () => addSection() },
      { label: W.editing ? 'Done editing' : 'Edit list: drag to reorder', fn: () => setEditing(!W.editing) },
      'sep', col('chg', 'Change column'), col('pct', 'Change % column'), col('vol', 'Volume column'),
      'sep', ...[['sym', 'Symbol'], ['last', 'Last'], ['pct', 'Change %']].map(([c, l]) => ({   // a phone has no headers to click
        label: `Sort by ${l}${W.sort?.col === c ? (W.sort.dir > 0 ? ' ▲' : ' ▼') : ''}`, check: W.sort?.col === c, fn: () => setSort(c) })),
      ...(W.sort ? ['sep', { label: 'Clear sort (list order)', fn: () => { W.sort = null; env.local.set('wlSort', 'null'); setCols(); arrange(); } }] : []),
    ], $q('#wl-more'));
  }
  // Menu actions find their row again when picked: the list may have reloaded meanwhile (a save from another device).
  function rowMenu(x, y, i) {
    const sym = items()[i], n = nthOf(items(), i), c = chartFor(sym), flags = flagPicker(sym, env.closeMenu);
    const at = (fn) => () => { const j = findNth(items(), sym, n); if (j >= 0) fn(j); };
    flags.classList.add('menu-flags');
    env.menu(x, y, [
      ...(c ? [{ label: `Open ${c} chart`, fn: () => env.open(c), key: 'Enter' }, ...(isLive(c) ? [{ label: 'Create alert…', fn: () => env.alert(c) }] : []), 'sep'] : []),
      flags,
      ...(flagView() ? [] : [{ label: 'Add section above…', fn: () => addSection(sym, n) }]),
      ...(navigator.clipboard ? [{ label: 'Copy symbol', fn: () => navigator.clipboard.writeText(wlBare(sym)).then(() => env.toast(`Copied ${wlBare(sym)}`), env.fail) }] : []),
      'sep',
      { label: flagView() ? `Remove ${wlBare(sym)}'s flag` : `Remove ${wlBare(sym)}`, fn: at(remove), key: 'Del' },
    ]);
  }
  function sectionMenu(x, y, i) {
    const sec = items()[i], nth = nthOf(items(), i), nm = sec.slice(WL_SECTION.length), n0 = blockOf(i) - 1;
    const at = (fn) => async () => { const its = items(), j = findNth(its, sec, nth); if (j >= 0) await fn(its, j, blockOf(j) - 1); };
    env.menu(x, y, [
      { label: 'Add symbol here…', fn: at(() => { W.addAt = { list: list().id, sec, n: nth }; openAdd(true); }) },
      { label: 'Rename section…', fn: async () => {
        const v = await env.ask({ title: 'Rename section', value: nm, ok: 'Rename' });
        if (v) await at((its, j) => { its[j] = WL_SECTION + v.toUpperCase(); commit(); })();
      } },
      'sep',
      { label: 'Remove section, keep its symbols', fn: at((its, j) => { its.splice(j, 1); if (W.sel > j) W.sel -= 1; commit(); }) },
      { label: `Delete section and its ${n0} symbols…`, fn: async () => {
        if (await env.ask({ title: `Delete ${nm}?`, message: `The section and its ${n0} symbols leave this list.`, input: false, ok: 'Delete', danger: true }) == null) return;
        await at((its, j, n) => { its.splice(j, n + 1); W.sel = -1; commit(); })();
      } },
    ]);
  }

  // ---- pointer: click, right-click, drag to reorder -----------------------------------------------------------------
  body.addEventListener('click', (e) => {
    if (W.noClick) return;
    const more = e.target.closest('.sec-more');
    const row = e.target.closest('.wl-row'), sec = e.target.closest('.wl-sec');
    if (more && sec) { const r = more.getBoundingClientRect(); sectionMenu(r.left, r.bottom, Number(sec.dataset.i)); return; }
    if (sec) {
      const lid = list().id, nm = items()[Number(sec.dataset.i)].slice(WL_SECTION.length), set = new Set(W.collapsed[lid] || []);
      if (set.has(nm)) set.delete(nm); else set.add(nm);
      W.collapsed[lid] = [...set];
      env.local.set('wlCollapsed', JSON.stringify(W.collapsed));
      arrange();
      return;
    }
    if (row) {
      select(Number(row.dataset.i), true);
      scroll.focus({ preventScroll: true });
    }
  });
  // Right-click or a long press: the row's menu (Remove is there, not behind a ✕ on the row).
  onMenuPress(body, '.wl-row, .wl-sec', (el, x, y) => {
    const i = Number(el.dataset.i);
    if (el.classList.contains('wl-row')) { select(i, false); rowMenu(x, y, i); } else sectionMenu(x, y, i);
  }, '.grip, .sec-more');
  body.addEventListener('pointerdown', (e) => {
    const el = e.target.closest('.wl-row, .wl-sec');
    if (!el || e.button > 0 || e.target.closest('.sec-more')) return;
    const grip = !!e.target.closest('.grip');
    if (e.pointerType !== 'mouse' && !grip) return;   // a finger scrolls the list; drag by the grip (Edit list)
    if (grip) e.preventDefault();
    W.press = { el, i: Number(el.dataset.i), y: e.clientY, id: e.pointerId };
  });
  window.addEventListener('pointermove', (e) => {
    const p = W.press;
    if (!p || e.pointerId !== p.id) return;
    if (!W.drag) {
      if (Math.abs(e.clientY - p.y) < 5) return;
      if (W.sort) { env.toast('Sorted by a column: clear the sort (⋯) to reorder'); W.press = null; return; }
      if (flagView()) { W.press = null; return; }   // a flagged list keeps the lists' order
      const n = blockOf(p.i), line = document.createElement('div');
      line.className = 'wl-drop';
      body.append(line);
      W.drag = { i: p.i, n, line, to: null, section: wlIsSection(items()[p.i]) };
      body.querySelectorAll(`[data-i]`).forEach((x) => { const k = Number(x.dataset.i); if (k >= p.i && k < p.i + n) x.classList.add('dragging'); });
    }
    e.preventDefault();
    dragTo(e.clientY);
  });
  function dragTo(y) {
    const g = W.drag, sr = scroll.getBoundingClientRect();
    if (y < sr.top + 28) scroll.scrollTop -= 10; else if (y > sr.bottom - 20) scroll.scrollTop += 10;
    const kids = [...body.children].filter((x) => x.dataset.i != null && !x.classList.contains('dragging')
      && (!g.section || x.classList.contains('wl-sec')));   // a section moves between sections
    let target = null;
    for (const x of kids) { const r = x.getBoundingClientRect(); if (y < r.top + r.height / 2) { target = x; break; } }
    g.to = target ? Number(target.dataset.i) : items().length;
    const br = body.getBoundingClientRect(), last = kids.at(-1);
    const top = target ? target.getBoundingClientRect().top : last ? last.getBoundingClientRect().bottom : br.top;
    g.line.style.top = `${top - br.top - 1}px`;
  }
  function endDrag(drop) {
    const g = W.drag;
    W.press = null;
    if (!g) return;
    W.drag = null;
    g.line.remove();
    body.querySelectorAll('.dragging').forEach((x) => x.classList.remove('dragging'));
    W.noClick = true;
    setTimeout(() => { W.noClick = false; }, 0);
    const sel = W.sel >= g.i && W.sel < g.i + g.n ? W.sel - g.i : null, pending = W.pending;
    W.pending = null;
    if (pending) {   // a save from another device during the drag: shown, and the move made in it, its rows found by name
      const its = items(), lid = list().id, it = its[g.i], end = g.to === its.length, before = its[g.to];
      setDoc(pending);
      const now = items(), i = list().id === lid ? now.indexOf(it) : -1, to = end ? now.length : now.indexOf(before);
      Object.assign(g, i < 0 || to < 0 ? { to: null } : { i, n: blockOf(i), to });   // gone there: that list as it is
    }
    if (drop && g.to != null && moveBlock(g.i, g.n, g.to)) {
      const start = g.to > g.i ? g.to - g.n : g.to;
      if (sel != null) W.sel = start + sel;
      else if (W.sel >= 0) W.sel = -1;
      commit();
    }
  }
  window.addEventListener('pointerup', (e) => { if (W.press && e.pointerId === W.press.id) endDrag(true); });
  window.addEventListener('pointercancel', () => endDrag(false));

  // ---- header buttons, add box ------------------------------------------------------------------------------------
  head.addEventListener('click', (e) => { const s = e.target.closest('[data-col]'); if (s) setSort(s.dataset.col); });
  $q('#wl-name').onclick = listMenu;
  $q('#wl-more').onclick = moreMenu;
  const addBox = $q('#wl-addbox'), addIn = $q('#wl-add-in');
  // The Add box searches the symbol database (symsearch.js): click a result or press Enter for the highlighted one;
  // "AAPL, MSFT" (commas) adds them as typed.
  const search = createSymbolSearch({ input: addIn, list: $q('#wl-add-list'), chips: $q('#wl-add-types'), api: env.api,
    onPick: (r) => { addSymbols(r.symbol, { [r.symbol]: r.name }); addIn.value = ''; search.refresh(0); addIn.focus(); } });
  function openAdd(on) {
    addBox.hidden = !on;
    $q('#wl-add').classList.toggle('on', on);
    if (!on) { W.addAt = null; search.clear(); return; }
    addIn.value = '';
    addIn.focus();
    search.refresh(0);
  }
  $q('#wl-add').onclick = () => openAdd(addBox.hidden);
  addBox.onsubmit = (e) => {
    e.preventDefault();
    if (addIn.value.includes(',') || !search.pick()) { addSymbols(addIn.value); addIn.value = ''; search.refresh(0); }
    addIn.focus();
  };
  addIn.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Escape') openAdd(false); });
  $q('#wl-add-close').onclick = () => openAdd(false);

  function setDoc(doc) {   // the server's lists
    if (W.drag) { if (!newer(W.pending, doc)) W.pending = doc; return; }   // after the drop
    const sel = selected();
    W.doc = doc;
    W.base = clone(doc);
    reselect(sel);
    render();
  }
  function heard(doc) {   // from the server (app.js: a save's broadcast, a reconnect's hello): see save()
    if (newer(W.base, doc)) return;
    if (W.saving) { if (!newer(W.heard, doc)) W.heard = doc; return; }
    setDoc(doc);
  }
  return { setDoc: heard, onQuotes, markActive, render };
}
