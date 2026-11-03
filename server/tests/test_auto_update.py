"""Updates without a hand: the pages know the server's version, and the server restarts itself after another
program writes its database."""
import asyncio
import sqlite3

from fastapi.testclient import TestClient

from chartsrv import app as app_mod
from chartsrv.store import Store


def _client(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", "none")
    monkeypatch.setenv("CHART_QUOTES_FEED", "none")
    return TestClient(app_mod.app)


def test_pages_know_the_version(tmp_path, monkeypatch):
    v = app_mod.WEB_VERSION
    with _client(tmp_path, monkeypatch) as c:
        assert f'<meta name="app-version" content="{v}">' in c.get("/").text
        sw = c.get("/sw.js")
        assert f"const CACHE = 'charts-{v}';" in sw.text and "no-cache" in sw.headers.get("cache-control", "")
        with c.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["version"] == v and len(hello["config"]) == 12
        page = c.get("/").text   # its files named by version: kept for good, renamed by an update
        assert f'<script src="app.js?v={v}"></script>' in page and f'href="style.css?v={v}"' in page
        assert c.get(f"/app.js?v={v}").headers["cache-control"] == "public, max-age=31536000, immutable"
        assert c.get("/app.js").headers["cache-control"] == "no-cache"
        assert c.get("/app.js?v=old").headers["cache-control"] == "no-cache"   # another version's name: not kept
        assert f"const VER = CACHE.slice('charts-'.length);" in sw.text


def test_restarts_after_another_program_writes(tmp_path, monkeypatch):
    st = Store(tmp_path / "a.db")
    restarts = []
    monkeypatch.setattr(app_mod, "restart_server", lambda: restarts.append(1))
    monkeypatch.setattr(app_mod, "DB_QUIET_S", 0.3)

    async def go():
        task = asyncio.create_task(app_mod.watch_db(st, every=0.05))
        st.set_setting("x", 1)   # its own write: nothing
        await asyncio.sleep(0.5)
        assert not restarts and not task.done()
        other = sqlite3.connect(tmp_path / "a.db")   # an import, in another process
        for i in range(3):
            with other:
                other.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (f"k{i}", "1"))
            await asyncio.sleep(0.1)
            assert not restarts   # still writing
        other.close()
        await asyncio.wait_for(task, 2)   # quiet: restarted once
        assert restarts == [1]
    asyncio.run(go())
    st.close()
