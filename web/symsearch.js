'use strict';
/* Symbol search, as TradingView's: type a ticker or a name, pick from the server's symbol database (US stocks and
   ETFs, futures, indices, currency pairs, crypto, FRED economic series; a search that finds little there asks Yahoo
   and FRED). Used by the watchlist's Add box and the chart's symbol switch. */

const SS_TYPES = [['', 'All'], ['future', 'Futures'], ['stock', 'Stocks'], ['etf', 'ETFs'], ['index', 'Indices'],
  ['forex', 'Forex'], ['crypto', 'Crypto'], ['economic', 'Economy']];
const SS_LABEL = { future: 'Futures', stock: 'Stock', fund: 'Fund', etf: 'ETF', index: 'Index', forex: 'Forex', crypto: 'Crypto', economic: 'FRED', data: 'Data' };

// input: the text box; list: where results go; chips: where the type filters go (or null).
// onPick(result): a result was clicked (Enter is the owner's: it calls pick()).
function createSymbolSearch({ input, list, chips, api, onPick }) {
  const S = { type: '', results: [], at: -1, moved: false, seq: 0, timer: 0, q: null };
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  if (chips) {
    chips.replaceChildren(...SS_TYPES.map(([t, label]) => {
      const b = el('button', 'ss-chip', label);
      b.type = 'button';
      b.dataset.t = t;
      b.onmousedown = (e) => e.preventDefault();   // the text box keeps the focus
      b.onclick = () => { S.type = t; renderChips(); refresh(0); };
      return b;
    }));
  }
  function renderChips() { chips?.querySelectorAll('.ss-chip').forEach((b) => b.classList.toggle('on', b.dataset.t === S.type)); }
  renderChips();

  function render() {
    list.replaceChildren(...S.results.map((r, i) => {
      const row = el('div', `ss-row${i === S.at ? ' on' : ''}`);
      row.dataset.i = String(i);
      row.append(el('b', 'ss-sym', r.symbol), el('span', 'ss-name', r.name), el('span', 'ss-ex', r.exchange || ''),
        el('span', `ss-type${r.live ? ' live' : ''}`, r.live ? 'Live' : SS_LABEL[r.type] || r.type));
      row.title = r.live ? 'Live chart from NinjaTrader' : r.type === 'economic' ? 'FRED economic series' : 'Delayed, free data';
      return row;
    }));
    if (!S.results.length && S.q != null) list.append(el('div', 'ss-none', S.q ? 'No symbol found. Try its ticker, FRED:ID for a FRED series, or DATA:SYMBOL for your own data.' : ''));
    list.hidden = false;
  }
  list.addEventListener('mousedown', (e) => e.preventDefault());
  list.addEventListener('click', (e) => {
    const row = e.target.closest('.ss-row');
    if (row) onPick(S.results[Number(row.dataset.i)]);
  });

  async function fetchNow() {
    const q = input.value.trim(), seq = ++S.seq;
    if (q.includes(',')) { S.results = []; S.q = null; list.replaceChildren(); return; }   // several symbols: added as typed
    try {
      const d = await api('GET', `/api/symbols/search?q=${encodeURIComponent(q)}&type=${S.type}&limit=30`);
      if (seq !== S.seq) return;   // a newer search went out meanwhile
      S.results = d.results;
      S.q = q;
      S.at = S.results.length ? 0 : -1;
      S.moved = false;
      render();
    } catch { /* offline: the box still takes what is typed */ }
  }
  function refresh(delay = 120) { clearTimeout(S.timer); S.timer = setTimeout(fetchNow, delay); }
  input.addEventListener('input', () => refresh());
  input.addEventListener('keydown', (e) => {
    if ((e.key !== 'ArrowDown' && e.key !== 'ArrowUp') || !S.results.length) return;
    e.preventDefault();
    S.at = (S.at + (e.key === 'ArrowDown' ? 1 : -1) + S.results.length) % S.results.length;
    S.moved = true;
    render();
    list.querySelector('.ss-row.on')?.scrollIntoView({ block: 'nearest' });
  });

  return {
    refresh,
    // The highlighted result, or null; `chosen` only when picked with the arrow keys.
    active: (chosen = false) => (S.at >= 0 && (!chosen || S.moved) && S.q === input.value.trim() ? S.results[S.at] : null),
    pick() { const r = this.active(); if (r) onPick(r); return r; },
    clear() { clearTimeout(S.timer); S.seq += 1; S.results = []; S.q = null; S.at = -1; list.replaceChildren(); list.hidden = true; },
  };
}
