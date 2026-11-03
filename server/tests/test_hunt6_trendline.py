"""Round 6 of the bug hunt, trend-line alerts: a log-scale line that has risen past what a float holds is a level no
price crosses, not an error on every trade."""
import math

from chartsrv import drawings

T = 1_789_398_000_000   # a Monday 09:30 CT


def test_a_log_line_risen_past_a_float_is_never_crossed_and_raises_nothing():
    # one 1s bar from 21000 to 21005, extended right: by weeks later the level is beyond any float
    line = {"t1": T, "p1": 21000.0, "t2": T + 1000, "p2": 21005.0, "left": False, "right": True, "per": 1000,
            "log": True}
    assert drawings.line_price(line, T + 500) > 21000
    assert drawings.line_price(line, T + 60 * 86_400_000) == math.inf
    falling = {**line, "p1": 21005.0, "p2": 21000.0, "left": True, "right": False}   # extended back, far before
    assert drawings.line_price(falling, T - 60 * 86_400_000) == math.inf
    assert drawings.line_price({**line, "p2": 20995.0}, T + 60 * 86_400_000) == 0.0   # falling away: toward 0
