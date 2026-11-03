#!/bin/sh
# Updates Tatanka Charts (Mac, Linux): the newest version, then the server rebuilt and restarted. Open pages reload
# by themselves. (NinjaTrader runs on Windows: there, update.cmd also copies its add-on in.)
set -e
cd "$(dirname "$0")"
echo "1/2  Getting the newest version (git pull)..."
git pull --ff-only --autostash   # your own edits (config/holidays.json, docs/DATA.md) go back on top of the new version
clash=$(git diff --name-only --diff-filter=U)
for f in $clash; do   # yours and the update's on the same lines: the update's is used, yours kept in git's stash
  if git cat-file -e "HEAD:$f" 2>/dev/null; then
    git checkout HEAD -- "$f"
    echo "     Your change to $f clashed with the update's: the update's is in place, yours is kept ('git stash show -p')."
  else   # the update removed a file you changed
    git rm -q -- "$f"
    echo "     The update removed $f, which you had changed: yours is kept ('git stash show -p')."
  fi
done
echo "2/2  Rebuilding and restarting the server (docker compose)..."
CHART_COMMIT=$(git rev-parse HEAD) docker compose up -d --build
echo "Done. Open pages and the phone app reload by themselves in a few seconds."
