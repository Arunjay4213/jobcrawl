# Shared by jobcrawl-run.sh and jobcrawl-boards.sh (sourced, not executed).
# git_net: run a git command that talks to GitHub, retrying transient failures.
# GitHub intermittently rejects a valid deploy key ("Permission denied
# (publickey)", 2026-09-23/24: six runs in two days), and one rejection used
# to fail the whole run, fire the alert email, and skip a crawl cycle. Three
# attempts 10s apart cost at most 20s against the 12-minute timer; a
# rejection that persists still fails so a real key revocation is not hidden.
git_net() {
  local attempt
  for attempt in 1 2 3; do
    git "$@" && return 0
    [ "$attempt" = 3 ] && return 1
    echo "git $1 failed (attempt $attempt/3), retrying in 10s" >&2
    sleep 10
  done
}
