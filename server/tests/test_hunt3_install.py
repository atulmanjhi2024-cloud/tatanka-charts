"""Bug hunt, round 3, install: the update script on a PC without docker or git, the processes a restart in place
leaves, leaving the simulator after weeks on it, and the docs' commands in a Command Prompt."""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from test_hunt_web_ui import app_js, run_app_js

ROOT = Path(__file__).resolve().parents[2]


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.mark.parametrize("missing", ["docker", "git"])
@pytest.mark.parametrize("script", ["update.sh", "tools/update.ps1"])
def test_the_update_stops_when_docker_or_git_is_missing(tmp_path, monkeypatch, script, missing):
    """README's install without Docker, or a clone made with GitHub Desktop (no git on PATH): update.cmd says what is
    missing and stops. It doesn't copy the new add-on into NinjaTrader for a server it didn't rebuild, or say Done."""
    run = {"update.sh": ["sh"], "tools/update.ps1": ["pwsh", "-NoProfile", "-NonInteractive", "-File"]}[script]
    tools = {"sh": shutil.which("sh"), "dirname": shutil.which("dirname"), "git": shutil.which("git"),
             run[0]: shutil.which(run[0])}
    if os.name == "nt" or not all(tools.values()):
        pytest.skip("sh, dirname, git and " + run[0])
    (tmp_path / "gitconfig").write_text("")
    for k, v in {"GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                 "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                 "HOME": str(tmp_path)}.items():   # HOME: update.ps1 looks for NinjaTrader in Documents
        monkeypatch.setenv(k, v)
    up, me, bin_ = tmp_path / "up", tmp_path / "charting", tmp_path / "bin"
    for name in ("update.sh", "tools/update.ps1", "ninjatrader/TatankaChartFeed.cs"):
        (up / name).parent.mkdir(parents=True, exist_ok=True)
        (up / name).write_text((ROOT / name).read_text())
    (up / "config.json").write_text("{}\n")
    _git(up, "init", "-q", "-b", "main")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "one")
    _git(tmp_path, "clone", "-q", str(up), str(me))
    (up / "config.json").write_text('{"new": 1}\n')   # the update
    _git(up, "commit", "-qam", "two")
    addon = tmp_path / "Documents" / "NinjaTrader 8" / "bin" / "Custom" / "AddOns" / "TatankaChartFeed.cs"
    addon.parent.mkdir(parents=True)
    addon.write_text("// the add-on NinjaTrader has\n")
    bin_.mkdir()   # the PC's PATH: everything the updaters use but the missing one
    for name, path in tools.items():
        if name != missing:
            os.symlink(path, bin_ / name)
    if missing != "docker":
        (bin_ / "docker").write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/docker.txt"\n')
        (bin_ / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_))

    r = subprocess.run([*run, str(me / script)], capture_output=True, text=True, timeout=120)
    assert r.returncode != 0 and "Done" not in r.stdout, r.stdout + r.stderr
    assert addon.read_text() == "// the add-on NinjaTrader has\n"
    if script.endswith(".ps1"):
        assert missing + " was not found" in r.stdout and not (tmp_path / "docker.txt").exists()


def _children() -> set[int]:
    """This process's children, running or ended but not yet waited for (zombies), from Linux's /proc."""
    return {int(p) for f in Path(f"/proc/{os.getpid()}/task").glob("*/children") for p in f.read_text().split()}


@pytest.mark.skipif(os.name == "nt" or not Path("/proc/self/task").exists(), reason="Linux's /proc")
def test_a_restart_in_place_leaves_no_child_processes(tmp_path, monkeypatch):
    """restart_server() ends in os.execv, which keeps this process: a worker process or multiprocessing's resource
    tracker still running then is never waited for by the new program, and stays a zombie for as long as the
    container runs (5 more after each import, saved instrument list or Restart)."""
    from fastapi.testclient import TestClient

    from chartsrv.app import app
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_WORKERS", "2")
    before, left = _children(), []
    monkeypatch.setattr(os, "execv", lambda *a: left.extend(_children() - before))   # the new program starts here
    with TestClient(app):
        end = time.monotonic() + 60
        while len(_children() - before) < 2 and time.monotonic() < end:   # the workers started
            time.sleep(0.2)
        assert len(_children() - before) >= 2
        monkeypatch.setattr(app.state, "reexec", True, raising=False)   # what restart_server() sets
    assert left == []


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_leaving_the_simulator_waits_for_its_trades_to_be_deleted():
    """Leaving the simulator, the server deletes its trades before it answers again: after five weeks on it, longer
    than the 90 s a restart is given. The page waits and reloads, not 'the server did not come back'; a plain restart
    still gives up after 90 s."""
    out = run_app_js(app_js("async function backAfterRestart(", "async function leaveSim("), """
let now = 0;
Object.assign(ctx, {
  Date: { now: () => now }, setTimeout: (f, ms) => { now += ms; Promise.resolve().then(f); },
  ask: async () => true, api: async () => ({ ok: true }), fail(e) { throw e; }, toasts: [], toast: (m) => ctx.toasts.push(m),
  back: 5 * 60e3, fetch: async () => { if (now < ctx.back) throw new TypeError('Failed to fetch'); return { ok: true }; },
  reloads: 0, location: { reload() { ctx.reloads += 1; } },
});
vm.runInContext(/*APP*/ + ';this.leaveSim = leaveSim; this.backAfterRestart = backAfterRestart;', ctx);
await ctx.leaveSim();
const left = { reloads: ctx.reloads, at: now, toasts: ctx.toasts };
now = 0; ctx.back = Infinity; ctx.reloads = 0; ctx.toasts = [];
await ctx.backAfterRestart();
out({ left, restart: { reloads: ctx.reloads, at: now, toasts: ctx.toasts } });""")
    left, restart = out["left"], out["restart"]
    assert left["reloads"] == 1 and left["at"] >= 5 * 60e3 and len(left["toasts"]) == 1, left
    assert left["toasts"][0].startswith("Deleting the simulator's trades")
    assert restart["reloads"] == 0 and 90e3 <= restart["at"] < 95e3
    assert restart["toasts"][0] == "Restarting…" and restart["toasts"][-1].startswith("The server did not come back")


def test_the_docs_commands_carry_no_comments():
    """The docs' commands are pasted into a Command Prompt, which has no '#' comments: 'copy env.example .env  # then
    fill in...' stops at 'The syntax of the command is incorrect.' (no .env), and a command gets '# lists ...' as
    arguments. What a command does goes in the text around it."""
    found = []
    for doc in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        block = None   # inside a code block: its language ('' for commands)
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"\s*```(\S*)", line)
            if m:
                block = m[1] if block is None else None
            elif block == "" and re.search(r"\s#", line):
                found.append(f"{doc.name}:{n}: {line.strip()}")
    assert found == []
