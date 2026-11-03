"""Is there a newer version? Every 6 hours the server asks GitHub what the repository's main branch has that the
commit it was built from doesn't (update.cmd passes that commit to the build; run from a git checkout, .git says).
Connections > App > Updates shows the count and what changed; open pages get a note once per new version. Taking it
is still update.cmd on the PC: the server can't rebuild itself. A private repository needs a read-only token."""
import asyncio
import logging
import re
import time
from pathlib import Path

import httpx

from . import stopping
from .config import ROOT

log = logging.getLogger(__name__)
API = "https://api.github.com/repos/{repo}/compare/{base}...{branch}"
BRANCH = "main"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EVERY_S = 6 * 3600
FIRST_S = 60


def git_head(root: Path) -> str:
    """The commit a git checkout is on, read from .git without git; "" if it isn't one."""
    g = root / ".git"
    try:
        head = (g / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head if SHA_RE.match(head) else ""
        ref = head[5:]
        if (g / ref).is_file():
            sha = (g / ref).read_text().strip()
            return sha if SHA_RE.match(sha) else ""
        for line in (g / "packed-refs").read_text().splitlines():
            sha, _, name = line.partition(" ")
            if name == ref and SHA_RE.match(sha):
                return sha
    except OSError:
        pass
    return ""


def running_commit(settings) -> str:
    c = (getattr(settings, "commit", "") or "").strip().lower()
    return c if SHA_RE.match(c) else git_head(ROOT)


class Updates:
    def __init__(self, market):
        self.m = market
        self.commit = running_commit(market.s)
        self.state = {"behind": None, "latest": "", "changes": [], "error": "", "at": None}

    def public(self) -> dict:
        """What open pages hear: how many changes behind, and the newest one's id (a note once per new version)."""
        return {"behind": self.state["behind"], "latest": self.state["latest"][:12]}

    async def run(self):
        await asyncio.sleep(FIRST_S)
        while True:
            before = self.public()
            await self.check()
            if self.public() != before:
                self.m.broadcast({"type": "update", **self.public()})
            await stopping.pause(EVERY_S)   # not after a stop the request swallowed (stopping.py)

    async def check(self, repo: str | None = None, token: str | None = None) -> dict:
        """Asks GitHub; the state, with "message" saying the outcome in a line."""
        s = self.m.s
        repo = (repo if repo is not None else s.update_repo or "").strip()
        token = token if token is not None else s.github_token
        st = {**self.state, "at": int(time.time() * 1000), "error": ""}
        if not self.commit:
            st["error"] = "This server's version is unknown: update it once with update.cmd, then it can tell"
        elif not REPO_RE.match(repo):
            st["error"] = "Repository: owner/name, as on GitHub"
        elif self.m.http is None:
            st["error"] = "No network"
        else:
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "tatanka-charts", "X-GitHub-Api-Version": "2022-11-28"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                r = await self.m.http.get(API.format(repo=repo, base=self.commit, branch=BRANCH), headers=headers, timeout=20)
                if r.status_code == 404:
                    st["error"] = (f"GitHub didn't find {repo}" + (": a private repository needs a token" if not token else
                                   ", or this server's version isn't on it (built from changes not pushed?)"))
                elif r.status_code in (401, 403):
                    st["error"] = ("GitHub's hourly limit for checks: tried again later" if r.headers.get("x-ratelimit-remaining") == "0"
                                   else "GitHub refused the token (expired, or no access to this repository)")
                else:
                    r.raise_for_status()
                    j = r.json()
                    commits = j.get("commits") or []
                    st["behind"] = int(j.get("ahead_by") or 0)
                    st["latest"] = commits[-1]["sha"] if commits and st["behind"] else self.commit
                    st["changes"] = [str((c.get("commit") or {}).get("message") or "").split("\n")[0][:120]
                                     for c in reversed(commits)][:10] if st["behind"] else []
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                st["error"] = f"GitHub did not answer ({type(e).__name__})"
        if st["error"]:
            log.info("update check: %s", st["error"])
        self.state = st
        n = st["behind"]
        st["message"] = st["error"] or (f"{n} update{'s' if n != 1 else ''} to take: run update.cmd on the PC" if n
                                        else f"Up to date ({self.commit[:7]})")
        return st
