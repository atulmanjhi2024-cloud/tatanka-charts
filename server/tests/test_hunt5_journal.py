"""Add-on 14's P lines, checked on the add-on itself: ninjatrader/TatankaChartFeed.cs compiled with mono against
stand-ins for NinjaTrader (tests/addon_csharp), its fill and position events raised as NinjaTrader raises them, and
the E and P lines it queues fed through the server's feed into the journal. A P line is stamped on the clock of the
account's own fills (the simulator stamps them with the PC's clock, a broker with its own: neither need be the
market data's), and goes a second after the position's last change (both events of each fill are in by then), so a
fill just after it, or one it already counts, never sorts on the wrong side of it."""
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chartsrv.market import DAY_MS
from test_redesign_journal_positions import E, P, _journal, _short

ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).resolve().parent / "addon_csharp"
SCENARIOS = ("sim_pc_behind", "position_first", "design_note", "keeps_changing 300 25", "keeps_changing 100 110",
             "cap", "restored", "jitter 1100 140", "jitter 1100 520", "jitter 2000 1520", "step 1000 300",
             "two_contracts 900 950", "two_contracts 300 580")
pytestmark = pytest.mark.skipif(not (shutil.which("mcs") and shutil.which("mono")), reason="needs mono")


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Per scenario of Harness.cs (its name and numbers): the lines it queued, as (PC ms read, line), and its marks
    {what: PC ms}. The scenarios run side by side (about 11 s: they wait out the add-on's real seconds)."""
    exe = tmp_path_factory.mktemp("addon") / "harness.exe"
    r = subprocess.run(["mcs", "-langversion:Latest", "-r:System.Core.dll", f"-out:{exe}", str(HARNESS / "Stubs.cs"),
                        str(ROOT / "ninjatrader" / "TatankaChartFeed.cs"), str(HARNESS / "Harness.cs")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr

    def run(name):
        r = subprocess.run(["mono", str(exe), *name.split()], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        lines, marks = [], {}
        for row in r.stdout.splitlines():
            if row.startswith("# "):
                what, ms = row[2:].split()
                marks[what] = int(ms)
            else:
                ms, line = row.split(" ", 1)
                lines.append((int(ms), line))
        return lines, marks
    with ThreadPoolExecutor(len(SCENARIOS)) as pool:
        return dict(zip(SCENARIOS, pool.map(run, SCENARIOS)))


def _ts(line):
    p = line.split("|")
    return int(p[4] if len(p) > 3 else p[2])


def _trades(tmp_path, monkeypatch, lines):
    (r,) = _journal(tmp_path, monkeypatch, lines, (min(map(_ts, lines)) - 60_000, max(map(_ts, lines)) + 60_000))
    return [_short(t) for t in r["trades"]]


def test_p_lines_carry_the_accounts_own_fills_clock(runs, tmp_path, monkeypatch):
    """The simulator's fills, the PC 3 s behind the exchange: the market data's clock ran 3 s ahead of the fills', so
    the first buy's P line was stamped after the add made a second later, and one trade became 'Long 2 exit unseen',
    'Long 1' from the position, exit unseen, and 'Long 2' from the position."""
    lines = [line for _, line in runs["sim_pc_behind"][0]]
    assert _trades(tmp_path, monkeypatch, lines) == [("Sim101", "long", 2, 5001.0, 5010.0, False, False, False)]


def test_a_connect_before_the_accounts_first_fill_keeps_the_market_datas_clock(runs, tmp_path, monkeypatch):
    """The design note's case: a broker's fills, the PC 5 s ahead of the exchange, position updates before
    executions. Long 1 from yesterday, first seen just after a trade and sold 4.5 s later: until the account's first
    fill its P lines go on the market data's clock (the earlier one), not the PC's, which would put that snapshot after
    the sale. Then a buy just after the sale's position was read: on the market data's clock that P line came out later
    than the buy (the fill's report takes longer than the market data's), and ended the buy's trade unseen."""
    lines = [line for _, line in runs["design_note"][0]]
    prior = [E(_ts(lines[0]) - DAY_MS, 1, 1, 4990, "p1"), P(_ts(lines[0]) - DAY_MS + 1000, 1, 4990)]
    assert _trades(tmp_path, monkeypatch, prior + lines) == [("Sim101", "long", 1, 4990.0, 5010.0, False, False, False),
                                                             ("Sim101", "long", 2, 5013.0, 5020.0, False, False, False)]


def test_a_p_line_is_stamped_early_enough_for_a_fill_whose_report_was_slower(runs, tmp_path, monkeypatch):
    """A broker's buy reported 20 ms after it filled set the account's clock; an add filled 139 ms before the buy's
    position was read but reported just after it (140 ms late). On the clock as measured, that P line came out after
    the add it does not count: 'Long 2 exit unseen', 'Long 1' from the position, exit unseen, and 'Long 2' from the
    position. 2.25 s early (the longest a position waits to be read), it clamps to the buy's own millisecond, before
    the add."""
    lines = [line for _, line in runs["jitter 1100 140"][0]]
    assert _trades(tmp_path, monkeypatch, lines) == [("Sim101", "long", 2, 5001.0, 5010.0, False, False, False)]


@pytest.mark.parametrize("scenario", ["jitter 1100 520", "jitter 2000 1520"])
def test_a_p_line_is_stamped_early_enough_for_a_fill_reported_a_second_or_two_slower(runs, scenario, tmp_path,
                                                                                       monkeypatch):
    """The same with the add's report 0.5 s and 1.5 s slower than the buy's (a fast market: the broker's or
    NinjaTrader's backlog), the buy's position read 1.1 s and 2 s after it. 250 ms early, that P line still came out
    after the add it does not count, and the trade split the same way."""
    lines = [line for _, line in runs[scenario][0]]
    assert _trades(tmp_path, monkeypatch, lines) == [("Sim101", "long", 2, 5001.0, 5010.0, False, False, False)]


@pytest.mark.parametrize("scenario", ["two_contracts 900 950", "two_contracts 300 580"])
def test_a_p_line_comes_out_at_its_own_contracts_last_fill_not_another_contracts(runs, scenario, tmp_path,
                                                                                  monkeypatch):
    """A broker's account trading ES and NQ: an ES add filled before the ES buy's position was read but reported after
    it (0.4 s and 1 s late), and an NQ buy filled between the add and the read. The P line came out at the account's
    last line, the NQ buy's millisecond, after the add it does not count, and the next one (after another NQ buy) did
    not replace it: 'Long 2 exit unseen', 'Long 1' from the position, exit unseen, and 'Long 2' from the position. It
    now comes out at the ES buy's own millisecond."""
    lines = [line for _, line in runs[scenario][0]]
    assert _trades(tmp_path, monkeypatch, lines) == [("Sim101", "long", 2, 5001.0, 5010.0, False, False, False),
                                                     ("Sim101", "long", 2, 20002.5, 20050.0, False, False, False)]


def test_a_connect_after_the_pcs_clock_stepped_forward_still_comes_before_the_next_fill(runs, tmp_path, monkeypatch):
    """A broker's buy set the account's clock; then the PC's clock was stepped 1 s forward (a time sync) and the
    server connected: the snapshot, on the clock measured before the step, came out 0.75 s late, after a sale made
    0.3 s on, and the trade was the sale's plus a 'Long 1' from the snapshot, exit unseen. 2.25 s early covers a step
    or drift of up to about 2 s."""
    lines = [line for _, line in runs["step 1000 300"][0]]
    assert _trades(tmp_path, monkeypatch, lines) == [("Sim101", "long", 1, 5000.0, 5010.0, False, False, False)]


def test_an_execution_from_hours_ago_raised_again_does_not_set_the_clock(runs, tmp_path, monkeypatch):
    """A sell from 2 h ago raised again (as a broker's reconnect may): no live fill's clock, so the next P line, read
    4 s after the buy, is still stamped now (2.25 s early), not 2 h back (where only the guard of the last line sent,
    the buy, held it)."""
    got = runs["restored"][0]
    read, last = got[-1]
    assert last.startswith("P|Sim101|ES|") and read - 3000 < _ts(last) <= read
    assert _trades(tmp_path, monkeypatch, [line for _, line in got]) == [
        ("Sim101", "long", 1, 4990.0, 4995.0, False, False, False), ("Sim101", "long", 1, 5000.0, None, True, False, False)]


def test_a_position_is_read_a_second_after_its_last_change(runs, tmp_path, monkeypatch):
    """A second buy's position update came a second after the first buy, and its execution update 0.1 s later. The
    position was read in between, a second after the first change, and stamped before the buy it counted: 'Long 1
    exit unseen', a 'Long 3' that never was, and the trade as 'Long 2' from the position."""
    lines = [line for _, line in runs["position_first"][0]]
    assert _trades(tmp_path, monkeypatch, lines) == [("Sim101", "long", 2, 5001.0, 5010.0, False, False, False)]


def test_a_position_that_keeps_changing_is_read_5_s_after_its_first_change(runs):
    """Position updates every 0.3 s put the read off each time, but not past 5 s after the first change."""
    got, marks = runs["keeps_changing 300 25"]
    reads = [read for read, line in got if line.startswith("P|Sim101|ES|")]
    assert reads and 4990 <= reads[0] - marks["first"] <= 6500 and got[-1][1].startswith("P|Sim101|ES|ES 12-26|")


def test_a_position_read_at_the_5_s_cap_waits_for_both_events_of_its_last_fill(runs, tmp_path, monkeypatch):
    """The simulator's fills every 600 ms: the read 5 s after the first change came between the 9th fill's position
    update and its execution update, so the P line counted a fill not yet sent and was stamped before it: 'Long 8
    exit unseen', a 'Long 10' that never was, and the trade as 'Long 9' from the position. At the cap the read now
    waits for 0.2 s with no change."""
    lines = [line for _, line in runs["cap"][0]]
    assert _trades(tmp_path, monkeypatch, lines) == [("Sim101", "long", 9, 5004.0, 5100.0, False, False, False)]


def test_a_position_that_changes_faster_than_the_caps_quiet_spell_is_read_10_s_after_its_first_change(runs):
    """Position updates every 0.1 s never leave the cap its 0.2 s with no change: it is read anyway, 10 s after the
    first."""
    got, marks = runs["keeps_changing 100 110"]
    reads = [read for read, line in got if line.startswith("P|Sim101|ES|")]
    assert reads and 9990 <= reads[0] - marks["first"] <= 11500 and got[-1][1].startswith("P|Sim101|ES|ES 12-26|")
