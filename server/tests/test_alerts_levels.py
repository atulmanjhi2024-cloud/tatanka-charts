"""The daily levels as the Daily levels study, level alerts, /api/levels, the hello and health payloads and the
simulator take them, from the app's own sources only (a levels file, a pasted line, a levels link), as the public copy
runs: there is no other levels reader there. Price alerts too."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import chartsrv
from chartsrv import daily_levels, levels
from chartsrv import market as market_mod
from chartsrv.alerts import AlertEngine
from chartsrv.config import Instrument, load_settings
from chartsrv.feeds.sim import SimFeed
from chartsrv.models import Tick
from chartsrv.store import Store

from test_review_fixes import FakeHttp, build_market

ES = Instrument("ES", "E-mini", 0.25, 50)
# Invented levels: pivot 7820; R1-R5 7850, 7862.50, 7875, 7890, 7905; S1-S5 7800, 7787.50, 7775, 7760, 7745.
LINE = "2026-01-15, 7820.00, 7850.00, 7862.50, 7875.00, 7890.00, 7905.00, 7800.00, 7787.50, 7775.00, 7760.00, 7745.00"


def generic_market(tmp_path, monkeypatch, **kw):
    """A market on the app's own levels sources only (daily_levels.py, levels.py), whatever else this copy has; its
    Documents folder (where the default levels file is) and what it broadcasts."""
    monkeypatch.setattr(market_mod, "DailyLevels", daily_levels.DailyLevels)
    monkeypatch.setattr(market_mod, "Levels", levels.Levels)
    docs = tmp_path / "docs"
    (docs / "Tatanka" / "levels").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CHART_DOCUMENTS_DIR", str(docs))
    m, _ = build_market(tmp_path, monkeypatch, **kw)
    sent = []
    m.broadcast = sent.append
    m.fired = []
    m.alerts.on_fire = m.fired.append
    return m, docs / "Tatanka" / "levels", sent


def feed(m, prices, sym="ES"):
    for i, p in enumerate(prices):
        m.alerts.on_tick(Tick(sym, 1_000_000 + i * 1000, p, 1, 0))


def pasted(tmp_path, monkeypatch, line=LINE):
    """A market with ES's pasted line, as the Plan tab saves it (PUT /api/amt/levels/ES)."""
    m, folder, sent = generic_market(tmp_path, monkeypatch)
    m.daily.set_paste("ES", line)
    m.levels_changed()
    return m


def test_a_levels_file_feeds_the_study_and_micros_read_their_parents(tmp_path, monkeypatch):
    m, folder, sent = generic_market(tmp_path, monkeypatch)
    assert m.levels.get("ES") is None and m.hello()["levels"] == {}
    (folder / "ES.txt").write_text(LINE + "\n")
    asyncio.run(m.daily.poll())
    ls = m.levels.get("MES")
    assert ls == m.levels.get("ES") and ls.symbol == "ES" and ls.origin == "file" and ls.date == "2026-01-15"
    assert ls.lines()[:2] == [("P", 7820.0), ("R1", 7850.0)] and ("S1", 7800.0) in ls.lines() and len(ls.lines()) == 11
    assert sorted(x["symbol"] for x in sent if x["type"] == "levels") == ["ES", "MES"]
    hello = m.hello()["levels"]["MES"]
    assert hello["label"] == "Daily levels ES" and hello["lines"][0] == ("P", 7820.0) and hello["date"] == "2026-01-15"
    assert m.health()["levels"]["ES"] == "2026-01-15"
    sent.clear()
    asyncio.run(m.daily.poll())   # unchanged: nothing new
    assert m.levels.poll() == [] and not [x for x in sent if x["type"] == "levels"]
    (folder / "ES.txt").unlink()   # the default file gone: no levels, and every page hears
    asyncio.run(m.daily.poll())
    assert m.levels.get("ES") is None and {"type": "levels", "symbol": "MES", "levels": None} in sent


def test_a_pasted_line_feeds_the_study_and_a_new_one_starts_level_alerts_afresh(tmp_path, monkeypatch):
    m = pasted(tmp_path, monkeypatch)
    assert m.levels.get("ES").origin == "paste" and m.levels.get("MES") is None   # a pasted line is the symbol's own
    m.store.add_alert("ES", "level", {"levels": ["R1"], "tolerance_ticks": 0, "rearm_ticks": 8}, True, "")
    m.alerts.reload()
    feed(m, [7840.0, 7845.0])
    assert m.alerts.armed[(1, "R1")] is True
    m.daily.set_paste("ES", LINE.replace("7850.00", "7851.00"))   # R1 moved
    m.levels_changed()
    assert (1, "R1") not in m.alerts.armed and m.levels.get("ES").resistance[0] == 7851.0
    m.daily.set_paste("ES", "")                                    # cleared: no levels, no level alerts
    m.levels_changed()
    assert m.levels.get("ES") is None and m.hello()["levels"] == {}
    feed(m, [7840.0, 7860.0])
    assert m.fired == []


def test_a_levels_link_feeds_the_study_and_the_alerts(tmp_path, monkeypatch):
    class Link(FakeHttp):
        async def get(self, url, **kw):
            self.asked = [*getattr(self, "asked", []), url]
            return SimpleNamespace(status_code=200, text=LINE, raise_for_status=lambda: None)
    http = Link()
    m, folder, sent = generic_market(tmp_path, monkeypatch, http=http)
    monkeypatch.setattr(m.daily, "spec", lambda: "https://levels.example/key/{root}.txt")
    asyncio.run(m.daily.poll())
    assert sorted(set(http.asked)) == ["https://levels.example/key/CL.txt", "https://levels.example/key/ES.txt",
                                       "https://levels.example/key/GC.txt", "https://levels.example/key/NQ.txt"]
    ls = m.levels.get("MES")
    assert ls.origin == "link" and ls.source == "https://levels.example/…/ES.txt" and ls.pivot == 7820.0
    m.store.add_alert("MES", "level", {"levels": "all", "tolerance_ticks": 1, "rearm_ticks": 8}, True, "")
    m.alerts.reload()
    feed(m, [7823.0, 7821.0, 7820.25], sym="MES")   # within 1 tick of the pivot
    assert [e["message"].split(" (")[0] for e in m.fired] == ["MES touched P 7820.00"]


def test_level_alert_arms_rearms_and_skips_startup(tmp_path, monkeypatch):
    m = pasted(tmp_path, monkeypatch)
    m.store.add_alert("ES", "level", {"levels": ["R1"], "tolerance_ticks": 0, "rearm_ticks": 8}, True, "")
    m.alerts.reload()
    feed(m, [7850.0])                            # starts on the level: no fire
    feed(m, [7850.25, 7850.0])                   # not rearmed yet (needs 8 ticks away)
    assert m.fired == []
    feed(m, [7852.0, 7849.75])                   # 8 ticks away rearms, then trades through 7850
    assert len(m.fired) == 1 and "touched R1 7850.00" in m.fired[0]["message"]
    feed(m, [7850.0, 7850.25, 7850.0])           # chop: stays quiet
    assert len(m.fired) == 1
    feed(m, [7848.0, 7850.0])
    assert len(m.fired) == 2


def test_level_alert_on_micro_uses_parent_levels(tmp_path, monkeypatch):
    m, folder, _ = generic_market(tmp_path, monkeypatch)
    (folder / "ES.txt").write_text(LINE)
    asyncio.run(m.daily.poll())
    m.store.add_alert("MES", "level", {"levels": "all", "tolerance_ticks": 1, "rearm_ticks": 8}, True, "")
    m.alerts.reload()
    feed(m, [7823.0, 7821.0, 7820.25], sym="MES")  # within 1 tick of the pivot
    assert [e["message"].split(" (")[0] for e in m.fired] == ["MES touched P 7820.00"]


def test_level_alert_turned_back_on_or_edited_takes_a_first_look(tmp_path, monkeypatch):
    """Restarted (Pause then Restart, an Only-once one after it fired) or edited, a level alert starts afresh, as a
    new one in the same spot: no fire for a touch while it was off, and none missed for the state it stopped in."""
    m = pasted(tmp_path, monkeypatch)
    store, fired = m.store, m.fired
    store.add_alert("ES", "level", {"levels": ["R1"], "tolerance_ticks": 0, "rearm_ticks": 8}, True, "")
    m.alerts.reload()
    feed(m, [7837.0, 7837.25])                   # armed
    store.update_alert(1, enabled=False)         # Pause
    m.alerts.reload()
    feed(m, [7842.0, 7850.0, 7850.0])            # to R1 while it was off
    store.update_alert(1, enabled=True)          # Restart
    m.alerts.reload()
    feed(m, [7850.0, 7850.0])                    # no move: nothing new
    assert fired == []
    feed(m, [7837.0, 7849.0])                    # armed again, then just under R1
    store.update_alert(1, params={"levels": ["R1"], "tolerance_ticks": 8, "rearm_ticks": 8})   # edited: a wider band
    m.alerts.reload()
    feed(m, [7849.0, 7849.0])                    # already inside it
    assert fired == []
    # Only once: fires and stops; restarted just under R1 (inside the re-arm distance), the next touch fires
    store.update_alert(1, enabled=False)
    once = store.add_alert("ES", "level", {"levels": ["R1"], "tolerance_ticks": 0, "rearm_ticks": 8}, False, "")
    m.alerts.reload()
    feed(m, [7837.0, 7850.0])
    assert [e["alert_id"] for e in fired] == [once]
    feed(m, [7897.0, 7849.75])
    store.update_alert(once, enabled=True)
    m.alerts.reload()
    feed(m, [7849.75, 7850.0])
    assert [e["alert_id"] for e in fired] == [once, once]


def test_the_simulator_starts_at_the_daily_levels(tmp_path, monkeypatch):
    m = pasted(tmp_path, monkeypatch)
    sim = SimFeed(m.instruments, m.levels, 0)
    assert sim._anchor_price("ES") == 7820.0 and sim._anchor_price("NQ") == m.instruments["NQ"].sim_price


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(market_mod, "DailyLevels", daily_levels.DailyLevels)
    monkeypatch.setattr(market_mod, "Levels", levels.Levels)
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_FEED", "none")
    monkeypatch.setenv("CHART_DOCUMENTS_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as c:
        yield c


def test_api_levels_and_level_alerts_from_a_pasted_line(client):
    assert client.get("/api/levels/ES").status_code == 404 and client.get("/api/health").json()["levels"] == {}
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["levels"] == {}   # hello
        assert client.put("/api/amt/levels/ES", json={"text": LINE}).json()["origin"] == "paste"
        while (msg := ws.receive_json())["type"] != "levels":
            pass
        assert msg["symbol"] == "ES" and msg["levels"]["lines"][1] == ["R1", 7850.0]
    got = client.get("/api/levels/es").json()
    assert got["label"] == "Daily levels ES" and got["origin"] == "paste" and got["date"] == "2026-01-15"
    assert got["lines"][:2] == [["P", 7820.0], ["R1", 7850.0]] and got["lines"][6] == ["S1", 7800.0]
    assert client.get("/api/health").json()["levels"] == {"ES": "2026-01-15"}
    a = client.post("/api/alerts", json={"symbol": "ES", "kind": "level", "levels": ["R1", "S1"], "tolerance_ticks": 2}).json()
    assert a["params"]["levels"] == ["R1", "S1"] and a["repeat"] is True
    m = client.app.state.market
    m.alerts.on_fire, fired = (lambda e: fired.append(e["message"])), []
    feed(m, [7830.0, 7849.5])
    assert [x.split(" (")[0] for x in fired] == ["ES touched R1 7850.00"]
    client.put("/api/amt/levels/ES", json={"text": ""})
    assert client.get("/api/levels/ES").status_code == 404


def test_without_the_private_reader_nothing_refers_to_it():
    """The public copy leaves out the author's own levels reader (levels_ml.py, release/private_files.txt) and the
    PRIVATE regions that wire it in: no file there names it, and the settings and instruments have no levels folder or
    `levels` field. Here, with it, this checks only that the generic parts never import it."""
    pkg = Path(chartsrv.__file__).parent
    root = pkg.parents[1]
    assert "levels_ml" not in (pkg / "levels.py").read_text() + (pkg / "daily_levels.py").read_text()
    if (pkg / "levels_ml.py").exists():
        return
    for p in [*root.glob("*.*"), *(f for d in ("config", "docs", "server", "web") for f in (root / d).rglob("*"))]:
        if p.is_file() and p.suffix in (".py", ".js", ".html", ".md", ".json", ".yml", ".example", ".txt", ".css") \
                and p.resolve() != Path(__file__).resolve():
            assert "levels_ml" not in p.read_text(encoding="utf-8", errors="replace"), p
    assert not hasattr(load_settings(), "levels_dir") and "levels" not in Instrument.__dataclass_fields__


def test_price_alert_one_shot(tmp_path):
    store = Store(tmp_path / "t.db")
    fired = []
    eng = AlertEngine(store, {"ES": ES}, None, fired.append)
    store.add_alert("ES", "price", {"price": 7850.0, "direction": "above"}, False, "")
    eng.reload()
    m = SimpleNamespace(alerts=eng)
    feed(m, [7849.0, 7849.75, 7849.5])
    assert fired == []
    feed(m, [7850.0, 7849.0, 7851.0])
    assert len(fired) == 1 and "crossed above 7850.00" in fired[0]["message"]
    assert store.list_alerts()[0]["enabled"] is False
    assert store.list_events()[0]["alert_id"] == 1


def test_price_alert_direction_filter(tmp_path):
    store = Store(tmp_path / "t.db")
    fired = []
    eng = AlertEngine(store, {"ES": ES}, None, fired.append)
    store.add_alert("ES", "price", {"price": 7850.0, "direction": "below"}, False, "")
    eng.reload()
    m = SimpleNamespace(alerts=eng)
    feed(m, [7849.0, 7851.0])
    assert fired == []
    feed(m, [7849.75])
    assert len(fired) == 1 and "below" in fired[0]["message"]
