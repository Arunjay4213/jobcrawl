"""Minute-level watch on a few named boards, alerting the moment a title lands.

The crawl polls every board on a 10-minute timer and a run takes ~7 minutes,
so a posting reaches the digest 10-17 minutes after it goes live. For a
handful of boards whose postings gate on application order (DoorDash's
summer intern req went live "end of week" in September 2026) that is queue
position lost. `watch` fetches only the boards under `config.yaml` `watch:`,
keeps its own tiny state file (ETags + URL keys already seen), and emails
straight away for any new intern-titled posting. It never opens jobs.db and
never takes the crawl lock: the regular run stores, scores, and tracks the
same posting a few minutes later, exactly as if the watch did not exist.

Alerting is deliberately looser than the prefilter: only the intern-title and
seniority regexes apply, no cycle or location gate. A watched board fires
rarely, and a false alert costs one email, while a gate that swallowed the
one posting the watch exists for would cost the whole point of it.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import sources
from .prefilter import INTERN_PAT, SENIOR_PAT
from .store import url_key

STATE_FILE = "watch_state.json"


def load_state(path: str = STATE_FILE) -> dict:
    p = Path(path)
    if not p.is_file():
        return {"etags": {}, "seen": {}, "fail_streak": {}}
    try:
        state = json.loads(p.read_text())
    except (OSError, ValueError):
        # A torn write (the box died mid-save) must not wedge every tick
        # that follows; a fresh baseline re-seeds silently on the next fetch.
        return {"etags": {}, "seen": {}, "fail_streak": {}}
    for k in ("etags", "seen", "fail_streak"):
        state.setdefault(k, {})
    return state


def save_state(state: dict, path: str = STATE_FILE) -> None:
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(p)


def board_key(entry: dict) -> str:
    return f"{entry['ats']}:{entry['token']}"


def wanted(job: dict) -> bool:
    title = job.get("title") or ""
    return bool(INTERN_PAT.search(title)) and not SENIOR_PAT.search(title)


def check(entries: list[dict], state: dict, fetch=sources.fetch
          ) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Fetch each watched board once; return ({name: [new intern jobs]}, {name: error}).

    Mutates `state`: ETags, seen URL keys, and per-board failure streaks.
    A board's first ever fetch only seeds `seen` - everything on it has been
    live for a while, and the crawl already has it. Seen keys accumulate
    (union, never replace) so a board that briefly answers with a partial
    list cannot re-alert on postings it merely dropped for one tick.
    """
    alerts: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for entry in entries:
        name, key = entry["name"], board_key(entry)
        try:
            jobs = fetch(name, entry["ats"], entry["token"], etags=state["etags"])
        except sources.Unchanged:
            state["fail_streak"][key] = 0
            continue
        except Exception as e:  # noqa: BLE001 - one dead board must not stop the others
            state["fail_streak"][key] = state["fail_streak"].get(key, 0) + 1
            errors[name] = f"{type(e).__name__}: {str(e)[:120]}"
            continue
        state["fail_streak"][key] = 0
        keys = {url_key(j["url"]) for j in jobs}
        if key in state["seen"]:
            seen = set(state["seen"][key])
            fresh = [j for j in jobs if url_key(j["url"]) not in seen and wanted(j)]
            if fresh:
                alerts[name] = fresh
            state["seen"][key] = sorted(seen | keys)
        else:
            state["seen"][key] = sorted(keys)
    return alerts, errors


def render(name: str, jobs: list[dict]) -> tuple[str, str]:
    """(subject, markdown body) for one board's alert email."""
    titles = "; ".join(j["title"] for j in jobs)
    subject = f"WATCH {name}: {titles}"[:200]
    lines = [f"# {name} just posted", ""]
    for j in jobs:
        lines.append(f"**{j['title']}**  ")
        if j.get("location"):
            lines.append(f"{j['location']}  ")
        if j.get("posted_at"):
            lines.append(f"posted {j['posted_at'][:16]}  ")
        lines.append(f"[Apply]({j['url']})")
        lines.append("")
    lines.append("The crawl will score and track this on its next run.")
    return subject, "\n".join(lines)
