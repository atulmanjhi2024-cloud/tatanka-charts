/* The Indicators dialog, laid out like NinjaTrader's: the Tatanka indicators on the left (a box shows or hides
   each), every property of the selected one on the right in its NinjaTrader groups, each group collapsible, with
   NinjaTrader's defaults. Edits stay in a draft until Apply or OK; the server keeps only what differs.
   Each indicator is an adapter from app.js: { key, title, version, shown(), show(on), schema(), defaults() (what a
   chart starts from), ntDefaults() (NinjaTrader's), overrides() (the chart's changes from defaults()), perSymbol(),
   save(draft, symbolDraft), applyAll(draft), colorFormat ('argb' for the AMT Toolkit, else 'hex'), sharedNames (a Set:
   the settings that are the same on every chart, such as the alert switches; optional), symbolShared (a Set: those of
   them that are the same on every chart of the symbol, such as the AMT Toolkit's alert settings; optional), allNote()
   (what Apply to all charts' question adds; optional) }. The settings are each chart's; Apply to all charts puts the
   shown ones on every chart (env.allCharts). */
'use strict';

const IND_DASHES = ['Solid', 'Dash', 'Dot', 'DashDot', 'DashDotDot'];

function createIndicatorsDialog(env) {
  const dlg = env.root, $q = (s) => dlg.querySelector(s);
  const list = $q('.ind-list'), body = $q('.ind-body'), search = $q('.ind-q');
  const el = (tag, props = {}) => Object.assign(document.createElement(tag), props);
  const D = { key: null, drafts: {}, symDrafts: {}, sym: null, shown: '', open: JSON.parse(env.local.get('indOpen', '{}') || '{}') };

  const ad = (k = D.key) => env.adapters().find((a) => a.key === k);
  const draft = (k = D.key) => (D.drafts[k] ??= { ...(ad(k).overrides() || {}) });
  // A symbol's own settings: the draft keeps only what was edited, and for the symbol the dialog shows (D.sym). The rest
  // shows as the symbol has it now, so a value that loads after the dialog opened is neither hidden nor saved over.
  const symDraft = (k = D.key) => {
    if (D.symDrafts[k]?.sym !== D.sym) D.symDrafts[k] = { sym: D.sym, v: {} };
    return D.symDrafts[k].v;
  };
  // What Apply saves of them: the symbol's values with the edits on top, or null if none were edited. Edits made while
  // the dialog showed another symbol (the chart switched since) are not saved onto this one.
  function symSave(a) {
    const s = D.symDrafts[a.key];
    return s && s.sym === env.symbol() && Object.keys(s.v).length ? { ...(a.perSymbol() || {}), ...s.v } : null;
  }
  const isSym = (a, name) => (a.symbolNames || []).includes(name);
  function value(a, x) {
    if (isSym(a, x.name)) {
      const s = symDraft(a.key), now = a.perSymbol() || {};
      return x.name in s ? s[x.name] : x.name in now ? now[x.name] : (a.symbolDefault?.(x.name) ?? a.defaults()[x.name]);
    }
    const d = draft(a.key);
    return x.name in d ? d[x.name] : a.defaults()[x.name];
  }
  function setValue(a, x, v) {
    if (isSym(a, x.name)) { symDraft(a.key)[x.name] = v; return; }
    const d = draft(a.key);
    if (JSON.stringify(v) === JSON.stringify(a.defaults()[x.name])) delete d[x.name]; else d[x.name] = v;
  }
  const ntDef = (a, name) => (a.ntDefaults ? a.ntDefaults() : a.defaults())[name];
  // changed from NinjaTrader's default (not from where the chart started): what the marks and counts show
  const changed = (a, x) => (isSym(a, x.name) ? false : JSON.stringify(value(a, x)) !== JSON.stringify(ntDef(a, x.name)));
  const nChanged = (a) => (a.schema() || []).filter((x) => changed(a, x)).length;
  // What the dialog shows (D.shown, as last drawn or edited): refresh() redraws only when that changed, since a box
  // keeps what is typed only on 'change' and a redraw for nothing (the same levels or block again) would drop it.
  const shownNow = () => {
    const a = ad();
    return JSON.stringify([env.adapters().map((b) => [b.key, b.title, b.version, b.shown(), nChanged(b)]), a.note,
      (a.schema() || []).map((x) => [x, value(a, x), ntDef(a, x.name), defaultText(a, x)])]);
  };

  // ---- colors: '#rrggbb' (NinjaTrader brush, alpha separate) or the Toolkit's ARGB integers ----------------------------
  const argbHex = (c) => `#${((c >>> 0) & 0xffffff).toString(16).padStart(6, '0')}`;
  const argbOpacity = (c) => Math.round((((c >>> 0) >>> 24) & 255) / 255 * 100);
  const toArgb = (hex, pct) => ((Math.round(Math.min(100, Math.max(0, pct)) / 100 * 255) << 24) >>> 0) + parseInt(hex.slice(1), 16);

  function control(a, x, row) {
    const v = value(a, x), done = (nv) => { setValue(a, x, nv); row.classList.toggle('changed', changed(a, x)); row.classList.remove('bad'); info(); };
    let c;
    if (x.type === 'bool') {
      c = el('input', { type: 'checkbox', checked: !!v });
      c.onchange = () => done(c.checked);
    } else if (x.choices) {
      c = el('select');
      c.append(...x.choices.map((o) => new Option(o, o)));
      c.value = v;
      c.onchange = () => done(c.value);
    } else if (x.type === 'int' || x.type === 'float') {
      c = el('input', { type: 'number', value: v, step: x.type === 'int' ? 1 : 'any' });
      if (x.min != null) c.min = x.min;
      if (x.max != null) c.max = x.max;
      c.onchange = () => {
        const n = Number(c.value), whole = x.type !== 'int' || Number.isInteger(n);
        if (c.value === '' || !Number.isFinite(n) || !whole || (x.min != null && n < x.min) || (x.max != null && n > x.max)) {
          row.classList.add('bad');
          env.toast(`${x.label}: ${x.type === 'int' ? 'a whole number' : 'a number'}${x.min != null ? ` from ${x.min}` : ''}${x.max != null ? ` to ${x.max}` : ''}`);
          return;
        }
        done(n);
      };
    } else if (x.type === 'color' && a.colorFormat === 'argb') {
      c = el('span', { className: 'col' });
      const pick = el('input', { type: 'color', value: argbHex(v) }), op = el('input', { type: 'number', min: 0, max: 100, step: 1, value: argbOpacity(v), title: 'Opacity %' });
      pick.setAttribute('aria-label', `${x.label} color`);
      op.setAttribute('aria-label', `${x.label} opacity, percent`);
      const set = () => done(toArgb(pick.value, Number(op.value) || 0));
      pick.onchange = set;
      op.onchange = set;
      c.append(pick, op, '%');
    } else if (x.type === 'color' && (x.none || x.default === 'transparent')) {   // NinjaTrader's Transparent: "None"
      c = el('span', { className: 'col' });
      const off = v === 'transparent', pick = el('input', { type: 'color', value: off ? '#000000' : v, disabled: off });
      const none = el('input', { type: 'checkbox', checked: off });
      pick.setAttribute('aria-label', `${x.label} color`);
      none.setAttribute('aria-label', `${x.label}: none (transparent)`);
      pick.onchange = () => done(pick.value);
      none.onchange = () => { pick.disabled = none.checked; done(none.checked ? 'transparent' : pick.value); };
      const lab = el('label', { className: 'chk' });
      lab.append(none, ' None');
      c.append(pick, lab);
    } else if (x.type === 'color') {
      c = el('input', { type: 'color', value: v });
      c.setAttribute('aria-label', `${x.label} color`);
      c.onchange = () => done(c.value);
    } else if (x.type === 'stroke') {   // NinjaTrader's Stroke: color, dash style, width (nowidth: another setting's)
      c = el('span', { className: 'col' });
      const pick = el('input', { type: 'color', value: v.color }), dash = el('select'), w = el('input', { type: 'number', min: 0.5, max: 10, step: 0.5, value: v.width, title: 'Width', hidden: !!x.nowidth });
      dash.append(...IND_DASHES.map((o) => new Option(o, o)));
      dash.value = v.dash;
      pick.setAttribute('aria-label', `${x.label} color`);
      dash.setAttribute('aria-label', `${x.label} dash style`);
      w.setAttribute('aria-label', `${x.label} width`);
      const set = () => { const n = Number(w.value); if (!(n >= 0.5 && n <= 10)) { row.classList.add('bad'); env.toast('Width 0.5 to 10'); return; } done({ color: pick.value, dash: dash.value, width: n }); };
      pick.onchange = dash.onchange = w.onchange = set;
      c.append(pick, dash, w);
    } else if (x.type === 'font') {
      c = el('span', { className: 'col' });
      const fam = el('select'), size = el('input', { type: 'number', min: 6, max: 48, step: 1, value: v.size, title: 'Size' });
      fam.append(...['Arial', 'Segoe UI', 'Consolas', 'Verdana', 'Tahoma'].map((o) => new Option(o, o)));
      if (![...fam.options].some((o) => o.value === v.family)) fam.append(new Option(v.family, v.family));
      fam.value = v.family;
      const set = () => { const n = Number(size.value); if (!(n >= 6 && n <= 48)) { row.classList.add('bad'); env.toast('Size 6 to 48'); return; } done({ family: fam.value, size: n }); };
      fam.onchange = size.onchange = set;
      c.append(fam, size, 'pt');
    } else if (x.type === 'text' || x.name === 'lvl_text') {
      c = el('textarea', { value: v, spellcheck: false, rows: x.name === 'RowsBlock' || x.name === 'CustomLevels' ? 8 : 3 });
      c.onchange = () => done(c.value);
    } else {
      c = el('input', { type: 'text', value: v, spellcheck: false, autocomplete: 'off' });
      c.onchange = () => done(c.value);
    }
    if (c.tagName !== 'SPAN') c.id = `ind-${a.key}-${x.name}`;
    return c;
  }
  function defaultText(a, x) {
    const v = isSym(a, x.name) ? (a.symbolDefault?.(x.name) ?? a.defaults()[x.name]) : ntDef(a, x.name);
    if (x.type === 'color') return a.colorFormat === 'argb' ? `${argbHex(v)}, ${argbOpacity(v)}% opaque` : v === 'transparent' ? 'none' : v;
    if (x.type === 'stroke') return x.nowidth ? `${v.color} ${v.dash}` : `${v.color} ${v.dash} ${v.width}`;
    if (x.type === 'font') return `${v.family} ${v.size}`;
    if (x.type === 'bool') return v ? 'on' : 'off';
    return String(v).length > 40 ? `${String(v).slice(0, 40)}…` : String(v) || 'empty';
  }

  // ---- left: the indicators ------------------------------------------------------------------------------------------
  function renderList() {
    list.replaceChildren(...env.adapters().map((a) => {
      const li = el('div', { className: 'ind-item' });
      li.classList.toggle('on', a.key === D.key);
      const box = el('input', { type: 'checkbox', checked: a.shown(), title: 'Show on the chart' });
      box.setAttribute('aria-label', `Show ${a.title}`);
      box.onclick = (e) => e.stopPropagation();
      box.onchange = () => a.show(box.checked);
      const name = el('span', { className: 'nm', textContent: a.title });
      const n = nChanged(a);
      const tag = el('small', { textContent: n ? `${n} changed` : a.version ? `v${a.version}` : '' });
      li.append(box, name, tag);
      li.onclick = () => select(a.key);
      return li;
    }));
    const sel = $q('.ind-pick');
    sel.replaceChildren(...env.adapters().map((a) => new Option(a.title, a.key)));
    sel.value = D.key;
  }

  // ---- right: the selected indicator's properties ------------------------------------------------------------------
  function renderBody() {
    const a = ad(), schema = a.schema() || [], groups = new Map();
    if (D.sym !== null && D.sym !== env.symbol()) {   // the chart's symbol changed: edits of the last one's settings that
      // every chart of a symbol shares are not this one's (it shows its own), as a symbol's own settings
      for (const [k, d] of Object.entries(D.drafts)) {
        const b = ad(k);
        if (b?.symbolShared) D.drafts[k] = Object.fromEntries(Object.entries(d).filter(([n]) => !b.symbolShared.has(n)));
      }
    }
    D.sym = env.symbol();   // the symbol whose own settings it shows
    for (const x of schema) { if (!groups.has(x.group)) groups.set(x.group, []); groups.get(x.group).push(x); }
    const open = D.open[a.key] || {};
    $q('.ind-title').textContent = `${a.title}${a.version ? ` v${a.version}` : ''}`;
    $q('.ind-note').textContent = a.note || '';
    body.replaceChildren(...[...groups].map(([g, items]) => {
      const det = el('details'), sum = el('summary', { textContent: g });
      det.dataset.group = g;
      det.open = open[g] !== false;   // NinjaTrader shows its groups expanded
      det.ontoggle = () => { (D.open[a.key] ??= {})[g] = det.open; env.local.set('indOpen', JSON.stringify(D.open)); };
      det.append(sum, ...items.map((x) => {
        const row = el('div', { className: 'set' });
        row.dataset.name = x.name;
        row.dataset.text = `${g} ${x.label} ${x.description || ''} ${x.name}`.toLowerCase();
        const lab = el('label', { textContent: x.label, title: x.description || '' });
        if (isSym(a, x.name) || a.symbolShared?.has(x.name)) lab.append(el('em', { className: 'sym', textContent: ` (${env.symbol()})` }));
        const ctl = control(a, x, row);
        lab.htmlFor = ctl.id || '';
        const undo = el('button', { type: 'button', className: 'undo', textContent: '↺', title: `Back to NinjaTrader's default (${defaultText(a, x)})` });
        undo.setAttribute('aria-label', `Reset ${x.label}`);
        undo.onclick = () => {
          if (isSym(a, x.name)) symDraft(a.key)[x.name] = a.symbolDefault?.(x.name) ?? a.defaults()[x.name];
          else setValue(a, x, ntDef(a, x.name));
          row.children[1].replaceWith(control(a, x, row));
          row.classList.remove('changed', 'bad');
          info();
        };
        row.classList.toggle('changed', changed(a, x));
        row.append(lab, ctl, undo);
        if (x.description) row.append(el('small', { textContent: x.description }));
        return row;
      }));
      return det;
    }));
    filter();
    info();
    renderTpl();
  }
  function info() {
    const a = ad(), n = nChanged(a);
    $q('.ind-info').textContent = n ? `${n} changed from NinjaTrader's defaults` : "All at NinjaTrader's defaults";
    body.querySelectorAll('details').forEach((d) => {
      const k = d.querySelectorAll('.set.changed').length, sum = d.querySelector('summary');
      sum.querySelector('.chg')?.remove();
      if (k) sum.append(el('span', { className: 'chg', textContent: `${k} changed` }));
    });
    const item = list.querySelector('.ind-item.on small');
    if (item) item.textContent = n ? `${n} changed` : a.version ? `v${a.version}` : '';
    D.shown = shownNow();
  }
  function filter() {
    const q = search.value.trim().toLowerCase();
    body.querySelectorAll('details').forEach((d) => {
      let any = false;
      d.querySelectorAll('.set').forEach((r) => { const on = !q || r.dataset.text.includes(q); r.hidden = !on; any ||= on; });
      d.hidden = !any;
      if (q && any) d.open = true;
    });
  }
  // Presets, NinjaTrader's "Save as template": this indicator's settings under a name, on every device. Loading one
  // fills the dialog; Apply or OK uses it. Only the chart's own: the settings charts share (the alert switches, every
  // chart of the symbol's or every chart's; the levels file) are not saved in one and stay as they are when one loads.
  // (Chart and indicator templates are the chart's, in its own menus.)
  const presetPart = (a, d, shared = false) => Object.fromEntries(Object.entries(d || {}).filter(([k]) => !!a.sharedNames?.has(k) === shared));
  function renderTpl() {
    const sel = $q('.ind-tpl'), T = env.templates?.();
    if (!sel || !T || !D.key) return;
    const kind = `ind:${D.key}`, names = T.names(kind);
    sel.replaceChildren(new Option('Presets ▾', ''), ...names.map((n) => new Option(`Load: ${n}`, `a:${n}`)),
      new Option('Save as preset…', 's'), ...names.map((n) => new Option(`Delete: ${n}`, `d:${n}`)));
    sel.value = '';
  }
  $q('.ind-tpl').onchange = async (e) => {
    const v = e.target.value, T = env.templates?.(), kind = `ind:${D.key}`;
    e.target.value = '';
    if (!v || !T) return;
    try {
      if (v === 's') {
        const name = (await env.ask({ title: `Save ${ad().title} settings as a preset`, value: '', ok: 'Save', placeholder: 'e.g. ES scalping', names: T.names(kind) }))?.trim().slice(0, 40);
        if (!name) return;
        await T.save(kind, name, { settings: presetPart(ad(), draft()) });
        env.toast(`Preset "${name}" saved on every device`);
      } else if (v.startsWith('a:')) {
        D.drafts[D.key] = { ...presetPart(ad(), draft(), true), ...presetPart(ad(), T.data(kind, v.slice(2))?.settings) };
        renderList();
        renderBody();
        env.toast('Loaded: Apply or OK uses it');
      } else if (v.startsWith('d:')) await T.del(kind, v.slice(2));
    } catch (err) { env.fail(err); }
    renderTpl();
  };
  function select(key) {
    D.key = key;
    env.local.set('indKey', key);
    renderList();
    renderBody();
  }
  function setAll(open) {
    body.querySelectorAll('details').forEach((d) => { d.open = open; });
  }

  async function apply(close) {
    if (body.querySelector('.set.bad')) { env.toast('Fix the settings marked in red first'); return; }
    const keys = new Set([...Object.keys(D.drafts), ...Object.keys(D.symDrafts)]);
    const btns = dlg.querySelectorAll('.ind-apply, .ind-ok');
    btns.forEach((b) => { b.disabled = true; });
    try {
      for (const k of keys) {
        const a = ad(k);
        const same = (x, y) => JSON.stringify(x || {}) === JSON.stringify(y || {});
        const d = D.drafts[k], s = symSave(a);
        if ((d && !same(d, a.overrides())) || (s && !same(s, a.perSymbol()))) await a.save(d ?? a.overrides(), s);
      }
      D.drafts = {};
      D.symDrafts = {};
      if (close) open(false);
      else { renderList(); renderBody(); }
      env.toast('Settings applied');
    } catch (e) {
      env.fail(e);
      const x = (ad().schema() || []).find((s) => e.message.startsWith(s.label) || e.message.includes(`Invalid ${s.label}`));
      if (x) body.querySelector(`.set[data-name="${x.name}"]`)?.classList.add('bad');
    } finally { btns.forEach((b) => { b.disabled = false; }); }
  }
  function open(on, key) {
    dlg.hidden = !on;
    if (!on) { D.drafts = {}; D.symDrafts = {}; return; }
    const keys = env.adapters().map((a) => a.key);
    D.key = keys.includes(key) ? key : keys.includes(env.local.get('indKey', '')) ? env.local.get('indKey', '') : keys[0];
    search.value = '';
    renderList();
    renderBody();
    if (window.innerWidth >= 760) search.focus();
  }

  search.oninput = filter;
  $q('.ind-pick').onchange = (e) => select(e.target.value);
  $q('.ind-expand').onclick = () => setAll(true);
  $q('.ind-collapse').onclick = () => setAll(false);
  $q('.ind-reset').onclick = () => {   // NinjaTrader's defaults, whatever the chart started from
    const a = ad(), d = {};
    for (const x of a.schema() || []) {
      if (!isSym(a, x.name) && JSON.stringify(ntDef(a, x.name)) !== JSON.stringify(a.defaults()[x.name])) d[x.name] = ntDef(a, x.name);
    }
    D.drafts[D.key] = d;
    renderBody();
    renderList();
  };
  // These settings (as shown, applied or not) on every chart: each tab of every chart, layout and workspace.
  $q('.ind-all').onclick = async () => {
    if (body.querySelector('.set.bad')) { env.toast('Fix the settings marked in red first'); return; }
    const a = ad();
    if (!a.applyAll || !env.allCharts) return;
    const ok = await env.ask({ title: `${a.title} on every chart?`, input: false, ok: 'Apply to all charts',
      message: `Every tab of every chart, layout and workspace shows these settings; what each had of its own for this indicator goes.${a.allNote?.() || ''}` });
    if (ok === null) return;
    const btns = dlg.querySelectorAll('.ind-apply, .ind-ok, .ind-all');
    btns.forEach((b) => { b.disabled = true; });
    try {
      const s = symSave(a);
      if (s && JSON.stringify(s) !== JSON.stringify(a.perSymbol() || {})) await a.save(a.overrides(), s);   // the symbol's own, as Apply
      await a.applyAll(draft(a.key));
      const n = await env.allCharts(a.key);
      delete D.drafts[a.key];
      delete D.symDrafts[a.key];
      renderList();
      renderBody();
      env.toast(`${a.title}: applied to every chart${n ? ` and ${n} other workspace${n > 1 ? 's' : ''}` : ''}`);
    } catch (e) { env.fail(e); } finally { btns.forEach((b) => { b.disabled = false; }); }
  };
  $q('.ind-apply').onclick = () => apply(false);
  $q('.ind-ok').onclick = () => apply(true);
  $q('.ind-cancel').onclick = $q('.ind-close').onclick = () => open(false);
  dlg.addEventListener('pointerdown', (e) => { if (e.target === dlg) open(false); });
  dlg.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Escape') open(false); });
  return { open, refresh: () => { if (!dlg.hidden && (D.sym !== env.symbol() || shownNow() !== D.shown)) { renderList(); renderBody(); } },
    templatesChanged: renderTpl };
}
