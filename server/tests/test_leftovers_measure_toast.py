"""Two small ones left from round 13: the measure shows while all drawings are hidden, and a phone toast wraps at the
screen's width, not at half of it."""
import shutil

import pytest

from test_hunt3_drawings import run as run_drawings
from test_hunt13_ui import decl

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


@needs_node
def test_the_measure_shows_while_all_drawings_are_hidden():
    """Hide all, then the ruler (or Shift+drag): its box and legs are drawn (before: nothing, as the drawings), and
    the hidden lines stay undrawn."""
    out = run_drawings(r"""
table('ES').set('a', hline('a', 7000));
await open('ES');
const rec = [];
const cv = new Proxy({ fillRect: (...a) => rec.push(a), measureText: () => ({ width: 10 }) },
  { get: (o, k) => (k in o ? o[k] : () => {}), set: () => true });
const boxes = () => { rec.length = 0; prim.paneViews()[0].renderer().draw({ useMediaCoordinateSpace: (f) => f({ context: cv, mediaSize: { width: 800, height: 400 } }) }); return rec.length; };
await press('hide');
const none = boxes();
D.setTool('measure');
pointer('pointerdown', 200, 220); await ticks();
pointer('pointermove', 400, 120); await ticks();
pointer('pointerup', 400, 120); await ticks();
console.log(JSON.stringify({ hidden: D.state.hidden, measure: !!D.state.measure, none, drawn: boxes() }));""")
    assert out["hidden"] is True and out["measure"] is True
    assert out["none"] == 0 and out["drawn"] > 0


def test_a_phone_toast_wraps_at_the_screens_width():
    """The toast is centered with left: 50% and a translate: without a width of its own, its box could only take the
    half of the screen right of its left edge, so on a phone a long AMT alert wrapped into a narrow column."""
    assert decl("#toast", "width") == "max-content"
    assert decl("#toast", "max-width") == "calc(100% - 32px)"
