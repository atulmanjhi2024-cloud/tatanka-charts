'use strict';
/* A row's menu: right-click with a mouse, a long press with a finger (iOS sends no contextmenu for one). Removing or
   deleting lives in these menus, not behind a ✕ a stray click or tap can hit.

   onMenuPress(root, selector, fn, skip): fn(el, x, y) for the element matching `selector` under the press, inside
   root; not on what matches `skip` (a drag grip). A press that moves (a scroll or a drag) is not one; the click that
   ends a long press is swallowed, so the row's own click (open, select) doesn't run as well. In a text field the
   browser's own menu stays. */
const PRESS_MS = 500;
const PRESS_SLOP = 10;   // px a finger may wander and still be pressing
const FIELD = 'input, textarea, select';
function onMenuPress(root, selector, fn, skip = '') {
  let timer = 0, p = null, quiet = 0;
  const cancel = () => { clearTimeout(timer); p = null; };
  const target = (e) => {
    const el = e.target.closest(selector);
    return el && root.contains(el) && !e.target.closest(skip ? `${FIELD}, ${skip}` : FIELD) ? el : null;
  };
  root.addEventListener('contextmenu', (e) => {
    const el = target(e);
    if (!el) return;
    e.preventDefault();
    if (Date.now() < quiet) return;   // the long press opened it already (Android sends both)
    fn(el, e.clientX, e.clientY);
  });
  root.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse') return;
    const el = target(e);
    if (!el) return;
    cancel();
    p = { x: e.clientX, y: e.clientY, id: e.pointerId };
    timer = setTimeout(() => {
      const { x, y } = p;
      p = null;
      quiet = Date.now() + 700;
      if (navigator.vibrate) navigator.vibrate(10);
      fn(el, x, y + 12);
    }, PRESS_MS);
  });
  root.addEventListener('pointermove', (e) => {
    if (p && e.pointerId === p.id && Math.hypot(e.clientX - p.x, e.clientY - p.y) > PRESS_SLOP) cancel();
  });
  root.addEventListener('pointerup', cancel);
  root.addEventListener('pointercancel', cancel);   // the browser took it for a scroll
  root.addEventListener('click', (e) => {   // the finger lifting after the menu opened
    if (Date.now() < quiet && e.target.closest(selector)) { e.preventDefault(); e.stopPropagation(); quiet = 0; }
  }, true);
}
