#!/usr/bin/env bash
# Daily board-tier regeneration: re-derive the generated board files from the
# aggregator feeds, the universe feeds, the VC portfolio sweeps, the YC sweep,
# and the website resolver, so newly-listed companies migrate to directly
# polled boards within a day instead of a week. Runs 13:00 UTC, an hour after
# the Monday feeds-discovery CI.
#
# The crawl lock is held only around git: `boards` never opens jobs.db, and
# the sweep plus resolver take 15+ minutes - holding the lock that long would
# cost the crawl a tick or two every day for nothing.
set -euo pipefail
cd "$HOME/jobcrawl"
. "$HOME/bin/jobcrawl-git.sh"
exec 9>"$HOME/.jobcrawl.lock"
flock 9
git_net pull --rebase --autostash origin main
flock -u 9
"$HOME/jobcrawl-venv/bin/python" -m jobcrawl.cli boards
flock 9
git add boards_broad.yaml boards_cold.yaml boards_vc.yaml boards_yc.yaml boards_resolved.yaml resolve_cache.json
if ! git diff --cached --quiet; then
  git commit -q -m "Daily board regen: $(date -u +%F)"
  git_net pull --rebase --autostash origin main
  git_net push -q origin main
fi
