#!/usr/bin/env bash
# Daily scorer audit (11:00 UTC, 6am Central): a stronger model re-reads the
# last day's judgments (`jobcrawl audit-daily`), the flagged rows are
# re-judged with fresh descriptions (`rescore --ids`), and the report,
# any config.yaml effort bump, and jobs.db are committed back. Added
# 2026-09-28 when the scorer moved to gpt-6-luna: the user asked for the
# effort to climb on its own if the small model makes mistakes.
#
# The crawl lock is held around git and the rescore (a DB writer), not
# around the audit call itself, which reads jobs.db read-only and can take
# minutes at high effort - the crawl keeps ticking meanwhile.
set -uo pipefail
cd "$HOME/jobcrawl"
. "$HOME/bin/jobcrawl-git.sh"
PY="$HOME/jobcrawl-venv/bin/python"
exec 9>"$HOME/.jobcrawl.lock"
flock 9
git_net pull --rebase --autostash origin main || { git rebase --abort 2>/dev/null; exit 1; }
flock -u 9
OUT=$("$PY" -m jobcrawl.cli audit-daily 2>&1)
rc=$?
echo "$OUT"
[ "$rc" -ne 0 ] && exit "$rc"
report=$(echo "$OUT" | sed -n "s/^Report: //p")
flock 9
if [ -n "$report" ] && "$PY" -c "import sys; from jobcrawl import cli; sys.exit(0 if cli.parse_ids_file(sys.argv[1]) else 1)" "$report"; then
  "$PY" -m jobcrawl.cli rescore --ids "$report" || echo "rescore failed (rc=$?)"
fi
git add audits config.yaml
git add -f jobs.db
if ! git diff --cached --quiet; then
  git commit -q -m "Daily scorer audit: $(date -u +%F)"
  git_net pull --rebase --autostash origin main || exit 1
  git_net push -q origin main || exit 1
fi
