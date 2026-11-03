// web/app.js's Connections dialog in node, for test_connections_cards.py: a small DOM (elements that take what cnEl
// gives them; querySelector for the selectors the dialog uses: tag, #id, .class, [attr="v"], :not(...), descendant and
// >), and a fetch answered by the test's server, one request at a time over stdin/stdout ("FETCH {...}" out, its
// answer back as a line). The test's results go out as "OUT {...}" lines.
'use strict';
const vm = require('vm');
const readline = require('readline');

const say = (kind, v) => process.stdout.write(`${kind} ${JSON.stringify(v)}\n`);
const PROPS = ['id', 'type', 'value', 'title', 'href', 'placeholder'];

function tokens(sel) {   // 'a > b.c[data-x="y z"]' -> [{comb, tok}]
  const out = [];
  let comb = ' ';
  for (const tok of sel.trim().replace(/\s*>\s*/g, ' > ').match(/(?:\[[^\]]*\]|\([^)]*\)|[^\s[(])+/g)) {
    if (tok === '>') { comb = '>'; continue; }
    out.push({ comb, tok });
    comb = ' ';
  }
  return out;
}
function matchOne(e, tok) {
  const m = tok.match(/^([a-z]*)((?:[#.][\w-]+|\[[^\]]+\]|:not\([^)]*\))*)$/i);
  if (!m) throw new Error(`cn_dom.js: selector ${tok}`);
  if (m[1] && e.tagName !== m[1].toUpperCase()) return false;
  for (const part of m[2].match(/[#.][\w-]+|\[[^\]]+\]|:not\([^)]*\)/g) || []) {
    if (part[0] === '#' && e.id !== part.slice(1)) return false;
    if (part[0] === '.' && !e.className.split(/\s+/).includes(part.slice(1))) return false;
    if (part[0] === '[') {
      const [, k, v] = part.match(/^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
      const have = k.startsWith('data-') ? e.dataset[k.slice(5)] : e.getAttribute(k);
      if (v === undefined ? have == null : String(have) !== v) return false;
    }
    if (part.startsWith(':not(') && matchOne(e, part.slice(5, -1))) return false;
  }
  return true;
}
function matches(e, sel) {
  return sel.split(',').some((one) => {
    const parts = tokens(one);
    const at = (x, i) => {   // x matches parts[i], and what is before it matches up the tree
      if (!matchOne(x, parts[i].tok)) return false;
      if (i === 0) return true;
      if (parts[i].comb === '>') return !!x.parentNode && at(x.parentNode, i - 1);
      for (let p = x.parentNode; p; p = p.parentNode) if (at(p, i - 1)) return true;
      return false;
    };
    return at(e, parts.length - 1);
  });
}
function walk(e, f) { for (const k of e.children) { f(k); walk(k, f); } }

const doc = { activeElement: null };
function el(tag) {
  return {
    tagName: tag.toUpperCase(), children: [], parentNode: null, dataset: {}, attrs: {}, style: {},
    id: '', className: '', textContent: '', type: tag === 'input' ? 'text' : '', checked: false, disabled: false, v: '',
    get value() { return this.v; }, set value(x) { this.v = String(x ?? ''); },   // text, as a browser's
    title: '', href: '', download: '', placeholder: '', autocomplete: '', spellcheck: true, inputMode: '', min: '', max: '',
    step: '', maxLength: -1, selected: false, open: false, hidden: false, onclick: null, onchange: null, scrollTop: 0,
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : PROPS.includes(k) && this[k] !== '' ? String(this[k]) : null; },
    append(...kids) {
      for (const k of kids) {
        const n = typeof k === 'string' ? Object.assign(el('#text'), { textContent: k }) : k;
        n.remove(); n.parentNode = this; this.children.push(n);
      }
    },
    prepend(...kids) { const old = this.children; this.children = []; this.append(...kids); this.children.push(...old); },
    replaceChildren(...kids) { for (const k of this.children) k.parentNode = null; this.children = []; this.append(...kids); },
    replaceWith(n) {
      const p = this.parentNode;
      if (!p) return;
      n.remove(); p.children[p.children.indexOf(this)] = n; n.parentNode = p; this.parentNode = null;
    },
    remove() { const p = this.parentNode; if (p) { p.children.splice(p.children.indexOf(this), 1); this.parentNode = null; } },
    contains(x) { for (let p = x; p; p = p.parentNode) if (p === this) return true; return false; },
    closest(s) { for (let p = this; p; p = p.parentNode) if (matches(p, s)) return p; return null; },
    querySelectorAll(s) { const out = []; walk(this, (k) => { if (k.tagName !== '#TEXT' && matches(k, s)) out.push(k); }); return out; },
    querySelector(s) { return this.querySelectorAll(s)[0] || null; },
    addEventListener() {}, scrollIntoView() {}, focus() { doc.activeElement = this; },
    getBoundingClientRect: () => ({ left: 0, top: 0, bottom: 0, right: 0, width: 0, height: 0 }),
    get text() { return [this.textContent, ...this.children.map((k) => k.text)].filter(Boolean).join(' '); },
  };
}
doc.body = el('body');
doc.activeElement = doc.body;
Object.assign(doc, { createElement: el, querySelector: (s) => doc.body.querySelector(s), querySelectorAll: (s) => doc.body.querySelectorAll(s) });

// The dialog with `src` (app.js's api() and its Connections part) run in a vm; cnOpen() is the dot clicked.
function dialog(src) {
  const waiting = [];
  readline.createInterface({ input: process.stdin }).on('line', (l) => waiting.shift()(JSON.parse(l)));
  const fetch = (path, o) => new Promise((res) => {
    waiting.push((r) => res({ status: r.status, ok: r.status < 400, statusText: '', json: async () => r.json }));
    say('FETCH', { path, method: o.method, body: o.body ? JSON.parse(o.body) : null });
  });
  const box = Object.assign(el('div'), { id: 'cn-dlg', hidden: true }), list = Object.assign(el('div'), { id: 'cn-list' });
  box.append(list);
  doc.body.append(box);
  const ls = new Map();
  const ctx = {
    console, document: doc, fetch, setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {}, AbortController,
    localStorage: { getItem: (k) => ls.get(k) ?? null, setItem: (k, v) => ls.set(k, v), removeItem: (k) => ls.delete(k) },
    navigator: { userAgent: 'node', maxTouchPoints: 0 }, matchMedia: () => ({ matches: false }), isSecureContext: false,
    location: { protocol: 'http:', host: 'testserver' }, $: (s) => doc.querySelector(s),
    toasts: [], toast: (m) => ctx.toasts.push(m), fail(e) { say('FAIL', String(e.message)); }, authCheck() {},
    ask: async () => true, menu: [], showMenu(x, y, items) { ctx.menu = items; }, onMenuPress() {},
    ax: { date: { format: (t) => new Date(t).toISOString().slice(0, 10) }, md: { format: (t) => new Date(t).toISOString().slice(5, 10) } },
    zs: () => 'UTC',   // the backups' and devices' times
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(`${src}\n;Object.assign(this, { cn, cnOpen, cnPoll });`, ctx);
  const settle = async () => {   // every request answered, and what they started done (each may make another)
    for (let quiet = 0; quiet < 10;) {
      await new Promise((r) => setTimeout(r, waiting.length ? 5 : 0));
      quiet = waiting.length ? 0 : quiet + 1;
    }
  };
  const card = (id) => doc.querySelector(`#cn-list .cn-card[data-id="${id}"]`);
  const D = {
    ctx, settle, card,
    async open() { await ctx.cnOpen(); await settle(); },
    async poll() { await ctx.cnPoll(); await settle(); },   // as a 'connections' message from the server does
    cards: (...ids) => doc.querySelectorAll('#cn-list .cn-card').filter((c) => !ids.length || ids.includes(c.dataset.id)).map((c) => ({
      id: c.dataset.id, draft: c.className.includes('draft'), status: c.querySelector('.cn-status').textContent,
      on: c.querySelector('.cn-on input')?.checked ?? null, buttons: c.querySelectorAll('button').map((b) => b.textContent),
      text: c.querySelectorAll('p.cn-about').map((p) => p.textContent).join(' | '), msg: c.querySelector('.cn-msg')?.textContent ?? '' })),
    field: (c, label) => c.querySelectorAll('label.cn-fld').find((l) => l.children[0].textContent === label).querySelector('input'),
    async press(c, label) { await c.querySelectorAll('button').find((b) => b.textContent === label).onclick(); await settle(); },
    async tick(c, on) {   // its On box, ticked or unticked by hand
      const b = c.querySelector('.cn-on input');
      b.checked = on;
      b.focus();
      if (b.onchange) await b.onchange({ target: b });
      await settle();
    },
    async plus(group, label) {   // a list's +, then one of what it offers
      const b = doc.querySelector(`#cn-list .cn-sec[data-group="${group}"] .cn-add`);
      b.onclick({ preventDefault() {}, stopPropagation() {}, currentTarget: b });
      const it = ctx.menu.find((m) => m.label === label);
      if (!it) throw new Error(`no ${label} under ${group}'s +: ${ctx.menu.map((m) => m.label)}`);
      await it.fn();
      await settle();
    },
    offers(group) {
      const b = doc.querySelector(`#cn-list .cn-sec[data-group="${group}"] .cn-add`);
      if (!b) return [];
      b.onclick({ preventDefault() {}, stopPropagation() {}, currentTarget: b });
      return ctx.menu.map((m) => m.label);
    },
  };
  return D;
}
module.exports = { dialog, say };
