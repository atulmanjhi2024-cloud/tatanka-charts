// web/layout.js and web/watchlist.js in node, for test_hunt_web_ui.py: a browser's windows as vm contexts that share
// one localStorage (a write in one fires 'storage' in the others, as in a browser), a server that keeps the
// workspaces and broadcasts each save to the windows connected to it, timers run by hand, and elements that take
// whatever the code builds.
'use strict';
const vm = require('vm');
const fs = require('fs');

// One window's localStorage over the browser's shared one; written(key, old, new) tells the other windows.
function storageFor(shared, written) {
  const api = {
    get length() { return shared.size; },
    key: (i) => [...shared.keys()][i] ?? null,
    getItem: (k) => (shared.has(k) ? shared.get(k) : null),
    setItem: (k, v) => { const old = api.getItem(k); shared.set(k, String(v)); written(k, old, String(v)); },
    removeItem: (k) => { const old = api.getItem(k); if (shared.delete(k)) written(k, old, null); },
  };
  return new Proxy(api, {   // Object.keys(localStorage) lists the stored keys, as a browser's does
    ownKeys: () => [...shared.keys()],
    getOwnPropertyDescriptor: (t, k) => (shared.has(k) ? { value: shared.get(k), enumerable: true, configurable: true } : undefined),
    get: (t, k) => (k in t ? t[k] : shared.get(k)),
  });
}

function makeTimers() {
  let id = 0;
  const q = new Map();
  return {
    add(f) { id += 1; q.set(id, f); return id; },
    clear(i) { q.delete(i); },
    get size() { return q.size; },
    async runAll() {   // each queued timer once, in order, with the promises it starts settled in between
      for (let n = 0; q.size && n < 100; n++) {
        const [[i, f]] = q;
        q.delete(i);
        f();
        for (let k = 0; k < 5; k++) await new Promise((r) => setImmediate(r));
      }
    },
  };
}
const settle = async () => { for (let k = 0; k < 10; k++) await new Promise((r) => setImmediate(r)); };

// The classes the code adds through classList are kept apart from className (which a test compares as set).
// replaceWith works on a child of an element (append, replaceChildren).
function el(tag = 'div') {
  const cls = new Set(), own = (p, k) => { for (const c of k) if (c && typeof c === 'object') c.parentNode = p; return k; };
  const e = {
    tagName: tag.toUpperCase(), children: [], style: { setProperty() {} }, dataset: {}, hidden: false, textContent: '',
    className: '', listeners: {}, sel: {},
    classList: { toggle: (c, on = !cls.has(c)) => { if (on) cls.add(c); else cls.delete(c); return !!on; },
      add: (...c) => c.forEach((x) => cls.add(x)), remove: (...c) => c.forEach((x) => cls.delete(x)), contains: (c) => cls.has(c) },
    append(...k) { this.children.push(...own(this, k)); }, replaceChildren(...k) { this.children = own(this, k); },
    replaceWith(n) { const p = this.parentNode; p.children[p.children.indexOf(this)] = n; own(p, [n]); },
    addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }, setAttribute() {}, focus() {}, remove() {},
    closest: () => null, contains: () => true, scrollIntoView() {}, getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, height: 0 }),
    querySelector(s) { return (this.sel[s] ||= el()); }, querySelectorAll: () => [],
  };
  if (tag === 'iframe') e.contentWindow = { posted: [], postMessage(m) { this.posted.push(m); } };
  return e;
}
function walk(e, f) { if (!e || typeof e !== 'object') return; f(e); for (const k of e.children || []) walk(k, f); }

// One browser: windows share its storage. open() starts a window (a page load) running layout.js's createWorkspaces();
// reload stands for app.js's reloadWhenIdle (by default nothing is being typed: the window reloads at once).
function makeBrowser(web, { device = 'pc', keys = {} } = {}) {
  const shared = new Map(Object.entries({ device, ...keys }));
  const B = { shared, windows: [] };
  const src = fs.readFileSync(`${web}/layout.js`, 'utf8');
  B.open = ({ api, timers, ask = async () => null, width = 1400, reload = null }) => {
    const listeners = { storage: [], message: [] };
    const els = { 'ws-btn': el('button'), 'ws-menu': el('div'), grid: el('div') };
    els['ws-menu'].hidden = true;
    const w = { listeners, els, toasts: [], reloads: 0, connected: true, timers };
    const storage = storageFor(shared, (key, oldValue, newValue) => {
      for (const o of B.windows) if (o !== w) for (const f of o.listeners.storage) f({ key, oldValue, newValue });
    });
    const ctx = {
      console,
      localStorage: storage,
      window: { innerWidth: width, addEventListener(t, f) { (listeners[t] ||= []).push(f); } },
      location: { search: '', origin: 'http://x', reload() { w.reloads += 1; B.windows = B.windows.filter((o) => o !== w); } },
      document: { getElementById: (id) => els[id], createElement: (t) => el(t), addEventListener() {} },
      setTimeout: (f) => timers.add(f), clearTimeout: (i) => timers.clear(i),
      ax: { date: { format: (t) => String(t) } },
    };
    vm.createContext(ctx);
    vm.runInContext(`${src}\n;this.L = { createWorkspaces, tkCellKey, tkInWorkspace, tkWaiting };`, ctx);
    w.local = {   // as app.js's: a workspace key changed here saves the workspace (none is written while the window waits)
      get: (k, d) => storage.getItem(ctx.L.tkCellKey(k)) ?? d,
      set: (k, v) => { if (ctx.L.tkWaiting() && ctx.L.tkInWorkspace(k)) return; storage.setItem(ctx.L.tkCellKey(k), v); w.ws.dirty(k); },
    };
    w.ws = ctx.L.createWorkspaces({ api, toast: (m) => w.toasts.push(m), fail: (e) => w.toasts.push(`FAIL ${e.message}`), ask,
      local: w.local, onActive() {}, reload: reload || (() => ctx.location.reload()) });
    w.button = (label) => {   // a button of the ▦ menu, opened
      els['ws-menu'].hidden = false;
      w.ws.render();
      let b = null;
      walk(els['ws-menu'], (e) => { if (!b && e.tagName === 'BUTTON' && (e.textContent === label || e.children[0]?.textContent === label)) b = e; });
      return b;
    };
    w.frames = () => els.grid.children.map((f) => f.contentWindow);
    w.fromFrame = (i, data) => listeners.message.forEach((f) => f({ origin: 'http://x', source: w.frames()[i], data: { tk: true, ...data } }));
    B.windows.push(w);
    return w;
  };
  return B;
}

// The server's workspaces: GET, PUT (name or data left out keep theirs) and a broadcast of each save to the windows
// connected now, as /api/workspaces and Market.broadcast() do.
function makeServer(items = []) {
  const S = { items, windows: [], now: 2000 };
  const summary = () => S.items.map(({ id, name, updated, by }) => ({ id, name, updated, by }));
  S.connect = (w) => { S.windows.push(w); return w; };
  S.api = async (method, path, body) => {
    const id = /^\/api\/workspaces(?:\/(.+))?$/.exec(path)?.[1];
    if (method === 'GET' && !id) return { workspaces: summary() };
    const w = S.items.find((x) => x.id === id);
    if (method === 'GET') { if (!w) throw new Error('no such workspace'); return JSON.parse(JSON.stringify(w)); }
    if (method !== 'PUT') throw new Error(`${method} ${path}`);
    const nw = { id, name: body.name ?? w?.name, updated: (S.now += 10), by: body.by || '', data: body.data ?? w?.data ?? {} };
    if (w) S.items[S.items.indexOf(w)] = nw; else S.items.push(nw);
    for (const c of S.windows) if (c.connected) c.ws.heard({ type: 'workspaces', workspaces: summary(), saved: id, by: nw.by });
    return { id, name: nw.name, updated: nw.updated, by: nw.by };
  };
  S.get = (id) => S.items.find((x) => x.id === id);
  return S;
}

// web/watchlist.js's createWatchlist() over elements that take anything; quotes as the server sends them. env: the
// app's functions to use instead of the do-nothing ones (api, ask, menu...). win: its window listeners (a drag's).
// timers (makeTimers): its timers, run by hand; without, none runs.
function makeWatchlist(web, { quotes = {}, current = 'ES', env = {}, timers = null } = {}) {
  let menuPress = null;
  const win = {};
  const ctx = {
    console, navigator: {},
    document: { createElement: (t) => el(t) },
    window: { addEventListener(t, f) { (win[t] ||= []).push(f); } },
    matchMedia: () => ({ matches: false }),
    setTimeout: (f) => (timers ? timers.add(f) : 0), clearTimeout: (i) => timers?.clear(i),
    onMenuPress: (box, sel, f) => { menuPress = f; }, createSymbolSearch: () => ({ refresh() {}, clear() {}, pick: () => false }),   // press.js, symsearch.js
    bondTick: () => 0, zs: () => '', ax: { clock: { format: () => '' }, wkHm: { format: () => '' } },   // studies.js, app.js
  };
  vm.createContext(ctx);
  vm.runInContext(`${fs.readFileSync(`${web}/watchlist.js`, 'utf8')}\n;this.create = createWatchlist;`, ctx);
  const root = el(), store = new Map();
  const list = ctx.create({
    root, api: async () => ({}), fail() {}, toast() {}, ask: async () => null, menu() {}, closeMenu() {}, open() {}, alert() {},
    local: { get: (k, d) => store.get(k) ?? d, set: (k, v) => store.set(k, v) },
    instruments: () => ({ ES: { symbol: 'ES', name: 'E-mini S&P 500', tick_size: 0.25, decimals: 2 } }),
    quotes: () => quotes, current: () => current, ...env,
  });
  const body = root.querySelector('.wl-body');
  return {
    list, quotes, root, win,
    detail: root.querySelector('#wl-detail'),
    click: (i) => body.listeners.click.forEach((f) => f({ target: { closest: (s) => (s === '.wl-row' ? { dataset: { i: String(i) } } : null) } })),
    rightClick: (i) => menuPress({ dataset: { i: String(i) }, classList: { contains: (c) => c === 'wl-row' } }, 0, 0),   // a row's menu
  };
}

module.exports = { makeBrowser, makeServer, makeTimers, makeWatchlist, settle, walk };
