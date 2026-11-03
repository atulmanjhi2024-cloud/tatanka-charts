"""Checks every connection the running server uses and prints a report: what works, what doesn't and why. No keys or
URLs in it, so it can be pasted anywhere. Alert channels get a test message each.

    docker compose exec chart python -m chartsrv.check     # Docker
    python -m chartsrv.check                               # the server run by hand, on this PC

It reaches the server with a token the server writes next to its database at start-up, so it works with the login on
and only from the PC itself."""
import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

TOKEN_FILE = "local-token"


def token_path(settings) -> Path:
    return Path(settings.db_path).parent / TOKEN_FILE


def report(doc: dict) -> str:
    at = datetime.fromtimestamp(doc["at"] / 1000, ZoneInfo("America/Chicago")).strftime("%Y-%m-%d %H:%M CT")
    lines = [f"Tatanka Charts check · {at}", f"feeds running: {', '.join(doc.get('feeds') or []) or 'none'}"]
    w = max(len(r["title"]) for r in doc["results"])
    for r in doc["results"]:
        mark = "OK  " if r["ok"] else "off " if r["ok"] is None else "FAIL"
        lines.append(f"  {mark}  {r['title']:<{w}}  {r['message']}")
    bad = sum(r["ok"] is False for r in doc["results"])
    lines.append(f"{bad} failing" if bad else "everything in use works")
    return "\n".join(lines)


def main(argv=None) -> int:
    from .config import load_settings
    ap = argparse.ArgumentParser(prog="python -m chartsrv.check", description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8800", help="the server (default: %(default)s)")
    a = ap.parse_args(argv)
    p = token_path(load_settings())
    try:
        token = p.read_text().strip()
    except OSError:
        print(f"no {TOKEN_FILE} next to the database: is the server running here? ({p})")
        return 2
    try:
        r = httpx.post(a.url.rstrip("/") + "/api/connections/check", headers={"X-Local-Token": token}, timeout=90)
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"the server at {a.url} did not answer ({type(e).__name__}): is it running?")
        return 2
    print(report(r.json()))
    return 1 if any(x["ok"] is False for x in r.json()["results"]) else 0


if __name__ == "__main__":
    sys.exit(main())
