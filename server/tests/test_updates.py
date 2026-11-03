"""Is there a newer version: the commit the server was built from, against the repository's main branch on GitHub."""
import asyncio

import httpx

from chartsrv import updates
from test_review_fixes import build_market

SHA = "a" * 40
NEW = "b" * 40


def test_the_running_commit(tmp_path, monkeypatch):
    g = tmp_path / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "refs" / "heads" / "main").write_text(SHA + "\n")
    assert updates.git_head(tmp_path) == SHA
    (g / "refs" / "heads" / "main").unlink()
    (g / "packed-refs").write_text(f"# pack-refs\n{NEW} refs/heads/main\n")
    assert updates.git_head(tmp_path) == NEW
    assert updates.git_head(tmp_path / "nope") == ""

    class S:
        commit = SHA.upper()
    assert updates.running_commit(S()) == SHA   # update.cmd's, over .git


def _http(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_check(tmp_path, monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        if "nobody/private" in str(req.url):
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"status": "behind", "ahead_by": 2, "commits": [
            {"sha": "c" * 40, "commit": {"message": "Older change\n\nbody"}}, {"sha": NEW, "commit": {"message": "Newest change"}}]})

    async def go():
        m, _ = build_market(tmp_path, monkeypatch, http=_http(handler), CHART_COMMIT=SHA)
        assert m.updates.commit == SHA and m.updates.public() == {"behind": None, "latest": ""}
        st = await m.updates.check()
        assert st["behind"] == 2 and st["changes"] == ["Newest change", "Older change"] and st["latest"] == NEW
        assert st["message"].startswith("2 updates to take") and m.updates.public() == {"behind": 2, "latest": NEW[:12]}
        assert str(seen[-1].url) == f"https://api.github.com/repos/tatankatrading/tatanka-charts/compare/{SHA}...main"
        assert "authorization" not in seen[-1].headers
        card = next(x for x in m.connections.public(m) if x["id"] == "updates")
        assert card["state"] == "warn" and card["info"] == "What's new: Newest change · Older change"
        st = await m.updates.check("nobody/private", "")
        assert "a private repository needs a token" in st["error"]
        await m.updates.check("nobody/private", "github_pat_" + "x" * 30)
        assert seen[-1].headers["authorization"].startswith("Bearer github_pat_")
        st = await m.updates.check("not a repo", None)
        assert st["error"].startswith("Repository")
        m.connections.put("updates", {"github_token": "github_pat_" + "s3cret" * 6})
        assert "s3cret" not in str(m.connections.public(m))   # a saved token never comes back
    asyncio.run(go())


def test_unknown_version(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "ROOT", tmp_path)   # no .git, no CHART_COMMIT

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        st = await m.updates.check()
        assert "unknown" in st["error"] and st["behind"] is None
    asyncio.run(go())
