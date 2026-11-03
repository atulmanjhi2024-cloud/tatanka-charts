/* Workspaces and multi-chart layouts, as TradingView's saved layouts.

   A workspace is what the app has open: the chart layout (1 to 6 charts), each chart's symbol, timeframe, tabs,
   studies and settings, which links between the charts are on, and the panel. Workspaces live on the server, so every
   device can open them; the open one saves itself as it changes (the last save wins).

   With more than one chart, each chart is this same page in a frame ("?cell=N"), and the page around them keeps the
   watchlist, alerts and Plan tab, and acts on the chart last clicked. Each chart keeps its own copy of the per-chart
   settings in the browser ("c2.spec"; the first chart uses the plain names), starting from the first chart's. */
'use strict';

const TK_CELL = (() => { const m = /[?&]cell=(\d)/.exec(location.search); return m ? Number(m[1]) : null; })();
const TK_PER_CHART = ['sym', 'spec', 'studies', 'ntChartSet', 'inds', 'fpSet', 'chartTabs', 'chartTab', 'chartSet', 'scaleLog', 'scaleMode',
  'gapDays', 'amtCollapsed', 'drawBar', 'rthOnly', 'seeded', 'layers', 'stHidden', 'barsOff', 'indSet'];
const TK_SHARED = ['favs', 'favsLong', 'sideOpen', 'wlList', 'wlCols', 'wlSort', 'wlCollapsed', 'wlDetail', 'layout', 'sync',
  'bigMin', 'tapeMin', 'fillAcct', 'jrAcct', 'tfCustom', 'timeZone'];
const TK_MAX_CELLS = 6;
// [label, cells, grid-template-columns, grid-template-rows, grid-template-areas or '']
const TK_LAYOUTS = {
  1: ['1 chart', 1, '1fr', '1fr', ''],
  2: ['2 side by side', 2, '1fr 1fr', '1fr', ''],
  '2v': ['2 stacked', 2, '1fr', '1fr 1fr', ''],
  3: ['1 large, 2 small', 3, '3fr 2fr', '1fr 1fr', '"a b" "a c"'],
  '3r': ['2 small, 1 large', 3, '2fr 3fr', '1fr 1fr', '"b a" "c a"'],   // mirrored: the first (large) chart on the right
  '3c': ['3 side by side', 3, '1fr 1fr 1fr', '1fr', ''],
  4: ['2 × 2', 4, '1fr 1fr', '1fr 1fr', ''],
  6: ['3 × 2', 6, '1fr 1fr 1fr', '1fr 1fr', ''],
};
const tkCellKey = (k) => (TK_CELL && TK_PER_CHART.includes(k) ? `c${TK_CELL}.${k}` : k);
const tkInWorkspace = (k) => {
  const base = k.replace(/^c[1-5]\./, '');
  return TK_PER_CHART.includes(base) || (base === k && (TK_SHARED.includes(k) || /^(anchor:|drawTpl\.|drawStyle\.)/.test(k)));
};
// This window waits to open a workspace another window of the browser opened (createWorkspaces, the 'storage'
// listener): until it reloads, neither its page nor its charts write a workspace setting, or it would be saved into that
// one. The page sets the mark; its charts read it there.
const tkWaiting = () => { try { return !!(window.top || window).tkWsWaiting; } catch { return false; } };
function tkLayout() {
  let v = null;
  try { v = localStorage.getItem('layout'); } catch { /* private mode */ }
  return TK_LAYOUTS[v] ? v : '1';
}
// Several charts only on a screen wide enough for them; a phone shows the first chart of the workspace.
const TK_MULTI = TK_CELL === null && tkLayout() !== '1' && window.innerWidth >= 760;

function createWorkspaces(env) {
  const { api, toast, fail, ask, local } = env;
  const raw = {   // device-only bookkeeping, never part of a workspace
    get: (k, d = null) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  if (!raw.get('device')) raw.set('device', `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`);
  const W = {
    list: [], id: raw.get('wsId'), device: raw.get('device'), auto: raw.get('wsAuto', '1') === '1',
    timer: 0, saving: false, pending: false, active: Math.min(Number(raw.get('activeCell', '0')) || 0, TK_MAX_CELLS - 1),
    frames: [], states: {},
  };
  const btn = document.getElementById('ws-btn'), menu = document.getElementById('ws-menu');
  const sync = () => { try { return { symbol: false, interval: false, crosshair: true, ...JSON.parse(local.get('sync', '{}')) }; } catch { return { symbol: false, interval: false, crosshair: true }; } };
  const current = () => W.list.find((w) => w.id === W.id);

  // ---- what a workspace holds ----------------------------------------------------------------------------------------
  function snapshot() {
    const keys = {};
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (tkInWorkspace(k)) keys[k] = localStorage.getItem(k);
      }
    } catch { /* private mode */ }
    return { layout: tkLayout(), sync: sync(), keys };
  }
  function apply(w) {
    clearTimeout(W.timer);
    W.id = null;   // nothing saves over another workspace while its settings are written
    try {
      for (const k of Object.keys(localStorage)) if (tkInWorkspace(k)) localStorage.removeItem(k);
      for (const [k, v] of Object.entries(w.data?.keys || {})) if (tkInWorkspace(k)) localStorage.setItem(k, v);
    } catch { /* private mode */ }
    raw.set('wsId', w.id);
    raw.set('wsStamp', String(w.updated || 0));
    raw.set('wsOpened', String(Math.random()));   // the other windows here open it too, the same id included
    if (raw.get('wsId') !== w.id) {   // the browser keeps no site data here: reloading would only start over
      toast('This browser is not keeping site data, so workspaces cannot open here');
      return;
    }
    location.reload();
  }

  // ---- saving ---------------------------------------------------------------------------------------------------------
  async function save(name) {
    if (!W.id) return;
    if (raw.get('wsId') !== W.id) {   // another window on this device opened another workspace: not this one's to save
      W.pending = true;
      render();
      return;
    }
    clearTimeout(W.timer);
    W.timer = 0;
    W.saving = true;
    try {
      const r = await api('PUT', `/api/workspaces/${W.id}`, { ...(name ? { name } : {}), data: snapshot(), by: W.device });
      raw.set('wsStamp', String(r.updated));
      const i = W.list.findIndex((w) => w.id === r.id);
      if (i >= 0) W.list[i] = r; else W.list.push(r);
      W.pending = false;
      render();
    } finally { W.saving = false; }
  }
  // A workspace setting changed here or in a chart frame: save the open workspace a moment later.
  function dirty(k) {
    if (!W.auto || !W.id || W.pending || !tkInWorkspace(k)) return;   // pending: a newer save elsewhere is not overwritten
    clearTimeout(W.timer);
    W.timer = setTimeout(() => save().catch(() => { /* next change retries */ }), 2000);
  }
  window.addEventListener('storage', (e) => {
    // Another window of this browser opened a workspace (another one, or a newer save of this one): the settings every
    // window here shares are that one's now, so this window opens it too, once nothing is being typed here. Until then
    // it saves nothing and writes no workspace setting (staying on this one, its changes would be saved into that one).
    if (e.key === 'wsId' || e.key === 'wsOpened') {
      if (W.id && (e.key === 'wsOpened' || (e.newValue && e.newValue !== W.id))) {
        clearTimeout(W.timer);
        W.id = null;
        window.tkWsWaiting = true;
        env.reload(`${W.list.find((x) => x.id === raw.get('wsId'))?.name || 'A workspace'} was opened in another window`);
      }
      return;
    }
    if (e.key) dirty(e.key);   // from the chart frames
  });

  // ---- the server's list ---------------------------------------------------------------------------------------------
  async function start() {
    let list;
    try { list = (await api('GET', '/api/workspaces')).workspaces; } catch { return; }
    W.list = list;
    if (!list.length) {   // first run anywhere: what is open now becomes "Main"
      W.id = 'main';
      raw.set('wsId', W.id);
      await save('Main').catch(() => {});
      return;
    }
    const w = current() || list[0];
    if (w.id !== W.id) { open(w.id); return; }   // a new device opens the first workspace
    if (w.updated > Number(raw.get('wsStamp', '0')) && w.by !== W.device) { open(w.id); return; }   // newer elsewhere
    render();
  }
  async function open(id) {
    try { apply(await api('GET', `/api/workspaces/${id}`)); } catch (e) { fail(e); }
  }
  // A reconnect: a save or delete broadcast while the socket was down was never heard; the list says it now.
  async function recheck() {
    let list;
    try { list = (await api('GET', '/api/workspaces')).workspaces; } catch { return; }
    const w = list.find((x) => x.id === W.id), newer = w && !W.pending && w.updated > Number(raw.get('wsStamp', '0'));
    heard({ workspaces: list, saved: newer ? w.id : null, by: w?.by });
  }
  function heard(m) {   // the server's broadcast: one was saved or deleted
    W.list = m.workspaces;
    if (W.id && m.saved === W.id && m.by !== W.device && !W.saving) {   // W.id: none open here, nothing to be newer than
      W.pending = true;
      toast(`${current()?.name || 'Workspace'} was changed on another device: ▦ → Load the newer one`);
    }
    if (W.id && !current() && W.list.length) W.pending = true;
    if (W.pending) { clearTimeout(W.timer); W.timer = 0; }   // an auto-save armed before this would save over the newer one
    render();
  }

  // ---- the ▦ menu ------------------------------------------------------------------------------------------------------
  const el = (tag, props = {}, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids); return e; };
  function render() {
    const w = current();
    btn.textContent = `▦ ${w ? w.name : 'Workspace'}${W.pending ? ' •' : ''}`;
    btn.title = W.pending ? 'Changed on another device' : 'Workspaces and chart layout';
    if (menu.hidden) return;
    const lay = el('div', { className: 'ws-lay' }, ...Object.entries(TK_LAYOUTS).map(([k, [label, n, cols, rows, areas]]) => {
      const b = el('button', { type: 'button', title: label, className: k === tkLayout() ? 'on' : '' });
      const g = el('span', { className: 'ws-ico' });
      Object.assign(g.style, { gridTemplateColumns: cols, gridTemplateRows: rows, gridTemplateAreas: areas || 'none' });
      for (let i = 0; i < n; i++) { const c = el('i'); if (areas) c.style.gridArea = 'abcdef'[i]; g.append(c); }
      b.append(g);
      b.onclick = () => setLayout(k);
      return b;
    }));
    const s = sync(), chk = (key, label, title) => {
      const c = el('input', { type: 'checkbox', checked: !!s[key] });
      c.onchange = () => { local.set('sync', JSON.stringify({ ...sync(), [key]: c.checked })); };
      return el('label', { className: 'chk', title }, c, ` ${label}`);
    };
    const rows = W.list.map((x) => {
      const r = el('button', { type: 'button', className: `ws-item${x.id === W.id ? ' on' : ''}` },
        el('span', { textContent: x.name }),
        el('small', { textContent: x.id === W.id ? (W.pending ? 'newer elsewhere' : 'open') : ax.date.format(x.updated) }));
      r.onclick = () => { if (x.id !== W.id || W.pending) open(x.id); };
      return r;
    });
    const act = (label, fn, cls = '') => { const b = el('button', { type: 'button', textContent: label, className: cls }); b.onclick = fn; return b; };
    const auto = el('input', { type: 'checkbox', checked: W.auto });
    auto.onchange = () => { W.auto = auto.checked; raw.set('wsAuto', W.auto ? '1' : '0'); if (W.auto && !W.pending) save().catch(fail); };   // pending: not over a newer save
    menu.replaceChildren(
      el('h5', { textContent: 'Layout' }), lay,
      window.innerWidth < 760 ? el('p', { className: 'hint', textContent: 'A phone shows the first chart.' }) : '',
      el('h5', { textContent: 'Link the charts' }),
      el('div', { className: 'row' }, chk('symbol', 'Symbol', 'A new symbol on one chart goes to all'),
        chk('interval', 'Timeframe', 'A new timeframe on one chart goes to all'),
        chk('crosshair', 'Crosshair', 'The crosshair moves on every chart at the same time')),
      el('h5', { textContent: 'Workspaces' }), ...rows,
      el('div', { className: 'row' },
        act('Save', () => save().then(() => toast('Workspace saved')).catch(fail)),
        act('Save as new…', saveAs), act('Rename…', rename), act('Delete', remove, 'danger')),
      el('label', { className: 'chk', title: 'Saves the open workspace as you change it' }, auto, ' Save changes automatically'),
      el('p', { className: 'hint', textContent: 'Saved on the server: every device can open them.' }));
  }
  async function saveAs() {
    const name = await ask({ title: 'Save as a new workspace', value: `${current()?.name || 'Workspace'} 2`, ok: 'Save' });
    if (!name) return;
    const was = W.id;
    W.id = `w${Date.now().toString(36)}`;
    raw.set('wsId', W.id);
    try { await save(name.trim()); toast(`Saved as ${name.trim()}`); } catch (e) {   // not saved: still the one before
      W.id = was;
      raw.set('wsId', was || '');
      fail(e);
    }
  }
  async function rename() {
    const name = (await ask({ title: 'Rename workspace', value: current()?.name || '', ok: 'Rename' }))?.trim();
    const w = current();
    if (!name) return;
    if (!w) { save(name).catch(fail); return; }   // deleted on another device: saved again, as it is here
    // The name only: what it holds stays as saved (a newer save from another device, or changes this one keeps with
    // auto-save off). A newer save waiting here stays that device's, so a reload here still opens it.
    W.saving = true;
    try {
      const r = await api('PUT', `/api/workspaces/${w.id}`, { name, by: W.pending ? w.by : W.device });
      if (!W.pending) raw.set('wsStamp', String(r.updated));
      W.list = W.list.map((x) => (x.id === r.id ? r : x));
      render();
    } catch (e) { fail(e); } finally { W.saving = false; }
  }
  async function remove() {
    const w = current();
    if (!w || (await ask({ title: `Delete ${w.name}?`, input: false, ok: 'Delete', danger: true,
      message: 'Every device loses it. The charts stay as they are here.' })) === null) return;   // OK answers ''
    try {
      await api('DELETE', `/api/workspaces/${w.id}`);
      W.list = W.list.filter((x) => x.id !== w.id);
      W.id = null;   // saves nothing until the next one is open
      raw.set('wsId', '');
      if (W.list.length) open(W.list[0].id); else render();
    } catch (e) { fail(e); }
  }
  // One per-chart setting (chartSet: the chart settings) on every chart: each chart of this layout now, every chart
  // position of this workspace, and every other saved workspace. Resolves to how many other workspaces took it.
  const cellKeys = (b) => [b, ...Array.from({ length: TK_MAX_CELLS - 1 }, (_, i) => `c${i + 1}.${b}`)];
  async function applyAll(base, value, from = -1) {
    const keys = cellKeys(base);
    // Chart settings are each tab's (in its look): every tab of every chart takes them too.
    const tabKeys = base === 'chartSet' ? cellKeys('chartTabs') : [];
    const withLook = (json) => {
      try {
        const tabs = JSON.parse(json), cs = JSON.parse(value);
        return Array.isArray(tabs) ? JSON.stringify(tabs.map((t) => (t && typeof t === 'object' ? { ...t, look: { ...(t.look || {}), cs } } : t))) : json;
      } catch { return json; }
    };
    try {
      for (const k of keys) localStorage.setItem(k, value);
      for (const k of tabKeys) { const v = localStorage.getItem(k); if (v) localStorage.setItem(k, withLook(v)); }
    } catch { /* private mode */ }
    W.frames.forEach((_, j) => { if (j !== from) post(j, { type: 'set', key: base, value }); });
    dirty(base);
    let n = 0;
    for (const w of W.list) {
      if (w.id === W.id) continue;
      try {
        const full = await api('GET', `/api/workspaces/${w.id}`), data = full.data || {};
        const had = data.keys || {}, tabs = Object.fromEntries(tabKeys.filter((k) => had[k]).map((k) => [k, withLook(had[k])]));
        await api('PUT', `/api/workspaces/${w.id}`, { data: { ...data, keys: { ...had, ...Object.fromEntries(keys.map((k) => [k, value])), ...tabs } }, by: W.device });
        n += 1;
      } catch { /* that one keeps its own */ }
    }
    return n;
  }
  // One study's settings on every chart (the Indicators dialog's Apply to all charts, once the server's starting
  // settings are set): no tab and no chart's shared set keeps its own, in this layout's charts now, every chart
  // position of this workspace, and every other saved workspace. Resolves to how many other workspaces took it.
  async function studyAll(key, from = -1) {
    const tabKeys = cellKeys('chartTabs'), setKeys = cellKeys('indSet');
    const omit = (o) => { const { [key]: _, ...rest } = o; return rest; };
    const fix = (k, json) => {
      try {
        const v = JSON.parse(json);
        if (tabKeys.includes(k) && Array.isArray(v)) {
          return JSON.stringify(v.map((t) => (t?.own?.set && key in t.own.set ? { ...t, own: { ...t.own, set: omit(t.own.set) } } : t)));
        }
        if (setKeys.includes(k) && v && typeof v === 'object' && key in v) return JSON.stringify(omit(v));
      } catch { /* not ours */ }
      return json;
    };
    try {
      for (const k of [...tabKeys, ...setKeys]) { const v = localStorage.getItem(k); if (v) localStorage.setItem(k, fix(k, v)); }
    } catch { /* private mode */ }
    W.frames.forEach((_, j) => { if (j !== from) post(j, { type: 'indall', key }); });
    dirty('chartTabs');
    let n = 0;
    for (const w of W.list) {
      if (w.id === W.id) continue;
      try {
        const full = await api('GET', `/api/workspaces/${w.id}`), data = full.data || {};
        const keys = Object.fromEntries(Object.entries(data.keys || {}).map(([k, v]) => [k, typeof v === 'string' ? fix(k, v) : v]));
        await api('PUT', `/api/workspaces/${w.id}`, { data: { ...data, keys }, by: W.device });
        n += 1;
      } catch { /* that one keeps its own */ }
    }
    return n;
  }
  function setLayout(k) {
    local.set('layout', k);
    if (W.auto && W.id && !W.pending) save().catch(() => {}).finally(() => location.reload());   // pending: the reload opens the newer one
    else location.reload();
  }
  btn.onclick = (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; render(); };
  menu.onclick = (e) => e.stopPropagation();
  document.addEventListener('pointerdown', (e) => { if (!menu.hidden && !e.target.closest('#ws')) menu.hidden = true; }, true);

  // ---- several charts: the frames, the chart last clicked, the links between them -------------------------------------
  function buildGrid() {
    const [, n, cols, rows, areas] = TK_LAYOUTS[tkLayout()];
    const grid = document.getElementById('grid');
    Object.assign(grid.style, { gridTemplateColumns: cols, gridTemplateRows: rows, gridTemplateAreas: areas || 'none' });
    W.active = Math.min(W.active, n - 1);
    W.frames = Array.from({ length: n }, (_, i) => {
      const f = el('iframe', { src: `index.html?cell=${i}`, title: `Chart ${i + 1}` });
      if (areas) f.style.gridArea = 'abcdef'[i];
      f.classList.toggle('on', i === W.active);
      f.addEventListener('load', () => cellWatch(f));
      grid.append(f);
      return f;
    });
    window.addEventListener('message', onCell);
  }
  // A chart's dialogs, menus, notes and drawing bar are in its frame, under whatever the page shows over the charts:
  // while one is open, the page's fired-alert panel steps aside (body.cell-over), as it is under the page's own.
  const CELL_OVER = '.modal:not([hidden]), .menu:not([hidden]), #ctx:not([hidden]), #tf-menu:not([hidden]), #quick:not([hidden]), '
    + '#draw-tree:not([hidden]), #draw-paste:not([hidden]), #draw-edit:not([hidden]), #toast.show';
  const cellOver = () => document.body.classList.toggle('cell-over', W.frames.some((f) => f.contentDocument?.querySelector(CELL_OVER)));
  function cellWatch(f) {   // each time a chart loads: a reload is a new page
    try { new MutationObserver(cellOver).observe(f.contentDocument.body, { subtree: true, attributes: true, attributeFilter: ['hidden', 'class'] }); } catch { /* not loaded */ }
    cellOver();
  }
  const post = (i, m) => W.frames[i]?.contentWindow?.postMessage({ tk: true, ...m }, location.origin);
  const others = (i, m) => W.frames.forEach((_, j) => { if (j !== i) post(j, m); });
  function onCell(e) {
    if (e.origin !== location.origin || !e.data?.tk) return;
    const m = e.data, i = W.frames.findIndex((f) => f.contentWindow === e.source);
    if (i < 0) return;
    if (m.type === 'active' || m.type === 'state') {
      const was = W.states[i];
      W.states[i] = m;
      if (m.type === 'active' && i !== W.active) {
        W.active = i;
        raw.set('activeCell', String(i));
        W.frames.forEach((f, j) => f.classList.toggle('on', j === i));
      }
      if (i === W.active) env.onActive(m);
      const s = sync();
      if (m.type === 'state' && was && !m.linked) {   // a chart's own change; one it took from a link is not sent back
        if (s.symbol && m.sym !== was.sym) others(i, { type: 'symbol', sym: m.sym, link: true });
        // forced: the chart's data source changed its timeframe (free data has fewer), not a timeframe picked there
        if (s.interval && m.spec !== was.spec && !m.forced) others(i, { type: 'spec', spec: m.spec, link: true });
      }
    } else if (m.type === 'xhair' && sync().crosshair) others(i, { type: 'xhair', ms: m.ms });
    else if (m.type === 'all' && m.key === 'chartSet') {
      applyAll(m.key, m.value, i).then((n) => toast(`Chart settings applied to all ${W.frames.length} charts${n ? ` and ${n} other workspace${n > 1 ? 's' : ''}` : ''}`));
    } else if (m.type === 'all' && m.key === 'study' && typeof m.study === 'string') {
      studyAll(m.study, i).then((n) => toast(`Applied to all ${W.frames.length} charts${n ? ` and ${n} other workspace${n > 1 ? 's' : ''}` : ''}`));
    }
  }

  if (TK_MULTI) buildGrid();
  return {
    start, recheck, heard, dirty, render, applyAll, studyAll,
    toActive: (m, focus = false) => { if (focus) W.frames[W.active]?.focus(); post(W.active, m); },
    activeState: () => W.states[W.active] || null,
  };
}
