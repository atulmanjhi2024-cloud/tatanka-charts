"""Bug hunt round 15, a fresh install: a backup from NinjaTrader restored onto the simulator, the add-on downloaded with
a token set, a micro kept without its parent, and a restore onto an install never started."""
import asyncio
import json
import socket
import sqlite3

from fastapi.testclient import TestClient

from chartsrv import backup, setup
from chartsrv.config import ROOT, _instruments, clean_instruments, instruments_file
from chartsrv.feeds.sim import SimFeed
from chartsrv.store import Store
from test_feed_plugins import _card, _client, _wait
from test_hunt4_sim import _count, _install
from test_review_fixes import build_market


def test_a_backup_from_an_install_on_ninjatrader_by_env_leaves_the_simulator(tmp_path, monkeypatch, capsys):
    """r15-install-1: an install that ran NinjaTrader the .env way (CHART_FEED=ninjatrader set in .env by hand,
    or every install from before the setup guide) never pressed Leave the simulator, so its backup has no leave_sim.
    Restored onto a new PC's fresh install, still on the simulator, it leaves the simulator at the next start as one
    that pressed it does: the backup notes the feed it ran on. Kept on, the simulator fires the restored alerts at its
    invented prices on the restored channels."""
    (tmp_path / "old").mkdir()
    m, _ = build_market(tmp_path / "old", monkeypatch, feed="ninjatrader", CHART_BACKUP_DIR=str(tmp_path / "bk"))
    m.store.set_setting("connections", {"ntfy": {"ntfy_url": "https://ntfy.sh/topic-zz91"}})
    m.store.add_alert("ES", "price", {"price": 6950.0, "direction": "below"}, False, "")

    async def go():
        return await m.backups.now()
    made = asyncio.run(go())   # the server's daily backup
    m.store.close()
    made = made.rename(made.with_name("tatanka-20261101-040000.db"))   # not the second the next is made in
    assert sqlite3.connect(made).execute("SELECT value FROM backup_meta WHERE key='feed'").fetchone() == ("ninjatrader",)
    monkeypatch.setenv("CHART_DB", str(tmp_path / "old" / "chart.db"))
    assert backup.main(["now"]) == 0   # and Back up now from the command line
    capsys.readouterr()
    by_cli = next(p for p in backup.files(tmp_path / "bk") if p != made)
    assert sqlite3.connect(by_cli).execute("SELECT value FROM backup_meta WHERE key='feed'").fetchone() == ("ninjatrader",)

    new = tmp_path / "chart.db"
    _install(new, left=False)   # the new PC: a fresh install, the simulator's trades stored
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setenv("CHART_DB", str(new))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_QUOTES_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "5")
    assert backup.main(["restore", made.name, "--yes"]) == 0
    assert "leaves the simulator" in capsys.readouterr().out
    st = Store(new)
    assert st.get_setting(setup.LEAVE_SIM) == "pending" and len(st.list_alerts()) == 1
    st.close()

    from chartsrv.app import app
    with TestClient(app) as c:   # the start after the restore: off the simulator, the one-shot alert still armed
        assert "sim" not in app.state.market.feed_names and "ninjatrader" in app.state.market.feed_names
        assert c.get("/api/setup").json()["left_sim"] is True
        assert [a["enabled"] for a in c.get("/api/alerts").json()] == [True]
    assert _count(new, "ticks") == 0 and _count(new, "alert_events") == 0


def test_an_older_backup_from_a_real_feed_is_known_by_what_only_a_real_feed_writes(tmp_path):
    """r15-install-1, backups from before the feed was noted: the contracts NinjaTrader reported (its C lines), your
    fills or positions say it came from a real feed. A simulator's backup has none, and stays on the simulator; a
    noted feed decides over them (an install back on the simulator)."""
    (tmp_path / "old").mkdir()
    old = tmp_path / "old" / "chart.db"
    _install(old, left=False)
    st = Store(old)
    st.add_alert("ES", "price", {"price": 6950.0, "direction": "below"}, False, "")
    st.close()
    new = tmp_path / "chart.db"
    _install(new, left=False)

    def bk(edit, feed=None):
        db = sqlite3.connect(old)
        db.execute("DELETE FROM settings WHERE key='contracts'")
        db.execute("DELETE FROM fills")
        db.execute("DELETE FROM positions")
        if edit:
            db.execute(edit)
        db.commit()
        db.close()
        p = backup.make(old, tmp_path / "bk", f"-{len(list((tmp_path / 'bk').glob('*')))}")
        b = sqlite3.connect(p)
        b.execute("DELETE FROM backup_meta WHERE key='feed'")   # one made before the feed was noted
        if feed:
            b.execute("INSERT INTO backup_meta VALUES ('feed', ?)", (feed,))
        b.commit()
        b.close()
        return p

    def leaves(p):
        db = sqlite3.connect(new)
        db.execute("DELETE FROM settings WHERE key=?", (setup.LEAVE_SIM,))
        db.commit()
        db.close()
        backup.restore(new, p, on_sim=True, say=lambda _: None)
        st = Store(new)
        try:
            return st.get_setting(setup.LEAVE_SIM) == "pending"
        finally:
            st.close()

    contracts = "INSERT INTO settings VALUES ('contracts', '{\"ES\": \"ES 12-26\"}')"
    assert leaves(bk(contracts))
    assert leaves(bk("INSERT INTO fills VALUES ('Sim101', 'x1', 'ES', 'ES 12-26', 1, 6900, 1, 1, 50, 0, 'o1', 'Entry')"))
    assert leaves(bk("INSERT INTO positions VALUES ('Sim101', 'ES 12-26', 1, 'ES', 1, 6900, 50)"))
    assert not leaves(bk(None))                          # the simulator's own backup
    assert not leaves(bk(contracts, feed="sim"))         # made on the simulator: what it ran on decides
    assert leaves(bk(None, feed="ninjatrader"))


def test_the_downloaded_add_on_carries_the_servers_token(tmp_path, monkeypatch):
    """r15-install-2: the older-add-on card's by-hand update (Download the add-on, replace the old one, F5) must keep
    the add-on connecting when the server sets a token: the download has the server's token in it (.env's or the one
    saved in Connections), as update.cmd keeps the copy's."""
    shipped = (ROOT / "ninjatrader" / "TatankaChartFeed.cs").read_bytes()
    assert b'private const string Token    = "";' in shipped
    monkeypatch.setenv("CHART_NT_TOKEN", 'ab"c\\d')
    app, client = _client(tmp_path, monkeypatch, feed="ninjatrader")
    with client as c:
        r = c.get("/api/addon/TatankaChartFeed.cs")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        assert 'private const string Token    = "ab\\"c\\\\d";' in r.text   # a C# string: the quote and backslash escaped
        assert r.content.replace(b'"ab\\"c\\\\d"', b'""') == shipped     # nothing else changed
        assert c.put("/api/connections/ninjatrader", json={"nt_token": "n3wTok"}).status_code == 200
        assert 'Token    = "n3wTok";' in c.get("/api/addon/TatankaChartFeed.cs").text
        assert c.put("/api/connections/ninjatrader", json={"nt_token": ""}).status_code == 200   # Clear: none
        assert c.get("/api/addon/TatankaChartFeed.cs").content == shipped


def test_an_add_on_with_the_wrong_token_is_told_and_the_card_says_so(tmp_path, monkeypatch):
    """r15-install-2: a HELLO with a token that is not the server's was closed with nothing said, the card went on
    with 'Waiting for the add-on' and NinjaScript Output with 'connected to'. Now it is answered BYE|wrong token (the
    add-on prints it and tries again in 30 s), and the card says an add-on was turned away for its Token."""
    from chartsrv.connections import DEFS
    nt = next(d for d in DEFS if d["id"] == "ninjatrader")
    assert "ChartFeedToken" not in json.dumps(nt) and "Token" in nt["fields"][1]["note"]
    monkeypatch.setenv("CHART_NT_TOKEN", "s3cretTok")
    app, client = _client(tmp_path, monkeypatch, feed="ninjatrader")
    with client as c:
        port = app.state.market.s.nt_port
        assert _card(c, "ninjatrader")["status"].startswith("Waiting for the add-on")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"HELLO||19\n")   # the add-on as shipped: Token ""
            got = s.recv(200)
        assert got.startswith(b"BYE|wrong token") and b"|" not in got[4:].strip()   # the add-on prints what follows BYE|
        card = _card(c, "ninjatrader")
        assert card["state"] == "error" and "Token" in card["status"] and "turned away" in card["status"]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"HELLO|s3cretTok|19\n")
            assert _wait(lambda: _card(c, "ninjatrader")["status"].startswith("Add-on connected ·"))
        assert _wait(lambda: _card(c, "ninjatrader")["status"].startswith("Waiting for the add-on"))   # the refusal is over


def test_the_readme_installs_without_docker_in_a_virtual_environment():
    """r15-install-3: README's no-Docker steps ran a bare `pip install -r requirements.txt`, which current Linux
    (Ubuntu 23.04+, Debian 12) and Homebrew's Python refuse (PEP 668: an externally managed environment), and then
    uvicorn had nothing to run. Each block, Windows and Mac/Linux, makes .venv and runs pip and uvicorn from its python."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    part = text.split("Without Docker", 1)[1].split("Open http://127.0.0.1:8800", 1)[0]
    blocks = part.split("```")[1::2]
    assert len(blocks) == 2
    for block, py in zip(blocks, (".venv\\Scripts\\python", ".venv/bin/python")):
        lines = [x.strip() for x in block.strip().splitlines()]
        assert lines[1].endswith("-m venv .venv"), block
        assert lines[2] == f"{py} -m pip install -r requirements.txt", block
        assert lines[3].startswith(f"{py} -m uvicorn chartsrv.app:app "), block
        assert not any(x.startswith(("pip ", "uvicorn ")) for x in lines), block
    assert ".venv/" in (ROOT / ".gitignore").read_text(encoding="utf-8").split()


def test_a_micro_stays_when_its_parent_is_taken_off_the_list(tmp_path):
    """r15-install-4: MES and MNQ ship following ES and NQ in the simulator (sim_follow), a field Instruments' editor
    never shows. Taking ES off while keeping MES was refused ('MES simulator follows ES'), on every feed. Now that
    follow goes, and the simulator makes MES's trades of its own, from its own simulator price."""
    rows = [{"symbol": k, **v} for k, v in instruments_file(ROOT / "config").items() if k not in ("ES", "NQ")]
    out = clean_instruments(rows)
    assert set(out) == {"CL", "GC", "MES", "MNQ"} and "sim_follow" not in out["MES"] and "sim_follow" not in out["MNQ"]
    sim = SimFeed(_instruments(out), None, 5)
    assert "MES" in sim.roots and sim.followers["MES"] == []
    kept = clean_instruments([{"symbol": k, **v} for k, v in instruments_file(ROOT / "config").items()])
    assert kept["MES"]["sim_follow"] == "ES"   # with its parent listed it follows as before


def test_a_restore_onto_an_install_never_started(tmp_path, monkeypatch, capsys):
    """r15-install-5: a new PC set up but never started has no database yet; the restore made its -before-restore copy
    of it read-only and ended in a traceback ('unable to open database file'), restoring nothing. Now the database is
    made as the first start would (a fresh install), then restored as usual."""
    (tmp_path / "old").mkdir()
    _install(tmp_path / "old" / "chart.db", left=True)
    made = backup.make(tmp_path / "old" / "chart.db", tmp_path / "bk")
    new = tmp_path / "data" / "chart.db"
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("CHART_DB", str(new))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CHART_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("CHART_FEED", "sim")
    assert backup.main(["restore", made.name, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "restored: " in out and "alerts 1" in out
    st = Store(new)
    assert len(st.list_alerts()) == 1 and st.get_setting("connections")["ntfy"]
    assert st.get_setting(setup.SETTING)["fresh"] is True              # as a fresh install started once, then restored
    assert st.get_setting(setup.LEAVE_SIM) == "pending"                # from an install off the simulator: left
    st.close()
    assert len([p for p in backup.files(tmp_path / "bk") if "before-restore" in p.name]) == 1
