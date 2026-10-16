#!/usr/bin/env bash
# One tick of the minute-level board watch (config.yaml `watch:`): conditional
# GET on a few named boards, email at once when a new intern title appears.
# No lock and no git: it never opens jobs.db and keeps its own state file
# (watch_state.json, gitignored). The 10-minute run pulls code and stores/scores
# the same posting on its own; this only shortens the alert.
set -uo pipefail
cd "$HOME/jobcrawl"
exec "$HOME/jobcrawl-venv/bin/python" -m jobcrawl.cli watch
