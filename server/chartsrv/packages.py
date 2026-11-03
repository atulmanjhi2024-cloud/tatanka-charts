"""Python packages feed plugins need (their REQUIRES), installed from Connections with one click. In Docker they go
in the data volume (PYTHONUSERBASE=/data/pylib), so they stay through rebuilds and updates; a newer Python starts
an empty folder and the card offers Install again. Only what a plugin in the server's folders lists is installed:
pip never sees anything a browser sent."""
import ast
import asyncio
import importlib
import importlib.util
import logging
import os
import re
import site
import sys
from pathlib import Path

log = logging.getLogger(__name__)
# "databento", "databento>=0.40", "python-dateutil:dateutil" (the name it is imported by, when it differs)
REQ_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,60})(\[[A-Za-z0-9,_-]{1,60}\])?"
                    r"((?:==|>=|<=|~=|!=|<|>)[0-9][0-9A-Za-z.*+!-]{0,30})?(?::([A-Za-z_][A-Za-z0-9_.]{0,60}))?$")
TIMEOUT_S = 900


def parse(req: str) -> tuple[str, str] | None:
    """(what pip installs, the module it imports) or None for anything else."""
    m = REQ_RE.match(req.strip()) if isinstance(req, str) else None
    if not m:
        return None
    return m.group(1) + (m.group(2) or "") + (m.group(3) or ""), m.group(4) or m.group(1).replace("-", "_")


def found(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def missing(reqs) -> list[str]:
    """The pip specs among `reqs` whose module can't be imported; a malformed one counts as missing."""
    out = []
    for r in reqs or []:
        p = parse(r)
        if p is None or not (found(p[1]) or found(p[1].lower())):   # MetaTrader5 imports as it is; PyYAML as yaml (say so: "PyYAML:yaml")
            out.append(p[0] if p else str(r)[:80])
    return out


def requires_in(path: Path) -> list[str]:
    """REQUIRES read from a plugin file without running it: for one that failed to load on an import."""
    try:
        tree = ast.parse(path.read_text("utf-8", "replace"))
    except (OSError, SyntaxError, ValueError):
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "REQUIRES" for t in node.targets):
            try:
                v = ast.literal_eval(node.value)
            except ValueError:
                return []
            return [x for x in v if isinstance(x, str)][:20] if isinstance(v, (list, tuple)) else []
    return []


def in_venv() -> bool:
    return sys.prefix != sys.base_prefix


def use_user_site():
    """The installed packages' folder on the import path, AFTER the app's own (Docker sets PYTHONNOUSERSITE, so Python
    doesn't put it first): a plugin's dependency can't replace a package the server itself uses. At start, and after
    each install."""
    if in_venv():
        return
    p = site.getusersitepackages()
    if Path(p).is_dir() and p not in sys.path:
        site.addsitedir(p)   # appended: the image's packages come first


class Installer:
    def __init__(self):
        self.busy: list[str] = []     # being installed now
        self.failed: dict[str, str] = {}   # spec -> pip's last words
        self.lock = asyncio.Lock()

    def state(self, spec: str) -> str:
        return "installing" if spec in self.busy else "failed" if spec in self.failed else ""

    async def install(self, specs: list[str]) -> tuple[bool, str]:
        specs = [p[0] for p in map(parse, specs) if p]
        if not specs:
            self.busy = []
            return False, "nothing pip can install"
        async with self.lock:
            self.busy = specs
            for s in specs:
                self.failed.pop(s, None)
            try:
                ok, out = await self._pip(specs)
            finally:
                self.busy = []
            if not ok:
                for s in specs:
                    self.failed[s] = out
            importlib.invalidate_caches()
            use_user_site()
            log.info("pip install %s: %s", " ".join(specs), "done" if ok else out)
            return ok, out

    @staticmethod
    async def _pip(specs: list[str]) -> tuple[bool, str]:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONNOUSERSITE"}   # pip sees what is installed there
        args = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-cache-dir",
                "--root-user-action=ignore", *([] if in_venv() else ["--user"]), *specs]
        try:
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        except OSError as e:
            return False, f"pip did not start ({type(e).__name__})"
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            return False, f"pip took over {TIMEOUT_S // 60} minutes"
        lines = [x for x in raw.decode("utf-8", "replace").splitlines() if x.strip()]
        if proc.returncode == 0:
            return True, ""
        err = [x for x in lines if x.startswith("ERROR")] or lines[-1:] or [f"pip exited {proc.returncode}"]
        return False, re.sub(r"^ERROR:\s*", "", err[0])[:300]
