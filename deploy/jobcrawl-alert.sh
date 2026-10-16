#!/usr/bin/env bash
cd "$HOME/jobcrawl"
UNIT=${1:-jobcrawl.service}
LOG=$(journalctl -u "$UNIT" --no-pager -o cat --since "-30 min" | tail -30)
"$HOME/jobcrawl-venv/bin/python" - "$LOG" "$UNIT" <<'PY'
import sys
from jobcrawl import cli, notify
cli.load_dotenv()
notify.send_email(f"{sys.argv[2]} on the VM failed. Last journal lines:\n\n" + sys.argv[1],
                  f"ALERT: {sys.argv[2]} failed on the VM")
PY
