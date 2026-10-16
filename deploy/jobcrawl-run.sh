#!/usr/bin/env bash
# One jobcrawl pipeline cycle: pull, run, commit state back. Mirrors the old
# crawl.yml semantics; flock prevents overlap if a run exceeds the interval.
# State commits are throttled: a quiet run (nothing new, nothing closed)
# defers the commit unless the last State commit is >55 min old. The VM
# is persistent, so jobs.db is always current locally; pushing is backup and
# laptop visibility, and hourly granularity matches the old cloud runner.
set -uo pipefail
cd "$HOME/jobcrawl"
. "$HOME/bin/jobcrawl-git.sh"
exec 9>"$HOME/.jobcrawl.lock"
flock -n 9 || exit 0
git_net pull --rebase --autostash origin main || { git rebase --abort 2>/dev/null; exit 1; }
OUT=$("$HOME/jobcrawl-venv/bin/python" -m jobcrawl.cli run 2>&1)
rc=$?
echo "$OUT"
quiet=1
echo "$OUT" | grep -q "Nothing new. Done." || quiet=0
echo "$OUT" | grep -q "^Closed:" && quiet=0
echo "$OUT" | grep -q "^Digest:" && quiet=0   # a flushed batch email changed notified flags
echo "$OUT" | grep -q "^Reopened:" && quiet=0
last_state=$(git log -1 --format=%ct --grep="^State:" 2>/dev/null || echo 0)
age=$(( $(date +%s) - ${last_state:-0} ))
if [ "$quiet" = 0 ] || [ "$age" -gt 3300 ]; then
  git add -f jobs.db
  [ -d digests ] && git add -f digests
  if ! git diff --cached --quiet; then
    git commit -q -m "State: $(date -u "+%Y-%m-%d %H:%M UTC")"
    git_net pull --rebase --autostash origin main || exit 1
    git_net push -q origin main || exit 1
  fi
fi
exit $rc
