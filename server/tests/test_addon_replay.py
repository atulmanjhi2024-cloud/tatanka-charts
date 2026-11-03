"""NinjaTrader replays the last trade when it subscribes or reconnects, and the add-on stamped it with the PC's
clock: in the 16:00 break it became the next session's first print at the last one's close (over a weekend, a
Saturday bar). Add-on 18 leaves out a trade whose own time is over 20 minutes old, unless several in a row are
(then the PC's clock is off and every trade would look old); add-on 19 counts them in a row only a few seconds apart,
so replays at separate reconnects never add up, and reads NinjaTrader's time zone once a minute, not every trade."""
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).resolve().parent / "addon_csharp"
SCENARIOS = {"replay": "ES", "skewed": "NQ", "live": "CL", "reconnects": "GC", "lull": "MNQ", "zone": "MES"}
pytestmark = pytest.mark.skipif(not (shutil.which("mcs") and shutil.which("mono")), reason="needs mono")


@pytest.fixture(scope="module")
def sent(tmp_path_factory):
    """Per scenario of TradeHarness.cs: the prices of the T lines the add-on's send loop wrote to the server (and what
    the harness printed, as "<scenario> out")."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    got = []

    def read(conn):
        with conn, conn.makefile("r", encoding="utf-8") as f:
            for line in f:
                got.append(line.strip())

    def accept():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=read, args=(conn,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    work = tmp_path_factory.mktemp("trades")
    src = (ROOT / "ninjatrader" / "TatankaChartFeed.cs").read_text(encoding="utf-8").replace("Port     = 8801;", f"Port     = {port};")
    (work / "TatankaChartFeed.cs").write_text(src, encoding="utf-8")
    exe = work / "trades.exe"
    r = subprocess.run(["mcs", "-langversion:Latest", "-r:System.Core.dll", f"-out:{exe}", str(HARNESS / "Stubs.cs"),
                        str(work / "TatankaChartFeed.cs"), str(HARNESS / "TradeHarness.cs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr

    def run(name):
        r = subprocess.run(["mono", str(exe), name, SCENARIOS[name]], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()
    try:
        with ThreadPoolExecutor(len(SCENARIOS)) as pool:
            out = dict(zip(SCENARIOS, pool.map(run, SCENARIOS)))
        time.sleep(0.3)
    finally:
        srv.close()
    return {**{name: [float(ln.split("|")[3]) for ln in got if ln.startswith(f"T|{root}|")] for name, root in SCENARIOS.items()},
            **{f"{name} out": text for name, text in out.items()}}


def test_the_last_trade_replayed_on_subscribing_is_left_out(sent):
    assert sent["replay"] == [6910.50, 6910.75]


def test_trades_that_all_look_old_go_once_several_come_in_a_row(sent):
    """A PC clock 30 minutes off: the first three are left out, then every one goes (none lost for good)."""
    assert sent["skewed"] == [6900.75, 6901.00, 6901.25]


def test_live_trades_all_go(sent):
    assert sent["live"] == [6920.0, 6920.25, 6920.5]


def test_replays_at_separate_reconnects_never_add_up_to_a_run(sent):
    """r14-review-6: NinjaTrader's data reconnects 4 times between Friday's close and Sunday 16:40, no live trade between:
    each replay of the last trade is left out, the 4th too (before: the 4th went, stamped Sunday 16:40, and the server
    moved it to the 17:00 open, so the week opened at Friday's close); the next live trade goes."""
    assert sent["reconnects"] == [6930.00, 6931.25]


def test_a_pc_clock_that_is_off_keeps_its_trades_after_a_lull(sent):
    """Once a burst of old-looking trades showed the PC's clock is off, they go, also after a minute with none."""
    assert sent["lull"] == [6940.75, 6941.00, 6941.25]


def test_ninjatraders_time_zone_is_read_once_a_minute_not_every_trade(sent):
    """A thousand trades read Tools > Options' time zone once (a scan of every assembly, by reflection, on NinjaTrader's
    instrument thread); a minute on it is read again, so a change there is taken."""
    assert sent["zone out"] == "1 2"
