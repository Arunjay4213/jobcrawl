"""
Output. Writes a markdown digest always; emails it if SMTP creds are present.

The digest is grouped by cycle and ordered by score, so the top of each section
is the part worth acting on before class.

Everything here is built around one constraint: the digest is read, not
searched. Coverage grew from a few dozen boards to several hundred, so a run
can now produce hundreds of scored postings, and a list that long is the same
as no list at all. This module deliberately shows less than it knows - it caps
each section, collapses repeats, and points at `jobcrawl list` for the rest.
The database keeps everything; the digest is only the daily read.
"""

from __future__ import annotations

import datetime as dt
import html
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path


# Winter sorts first: boards that name a winter term ("Winter 2027", per
# Figma et al.) mean the Jan-Apr window of that year - the same window as
# spring - not December. Sorting it after fall pushed the soonest deadlines
# to the bottom of the digest.
TERM_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}

# Per-section and whole-digest caps. Anything beyond them is counted in an
# overflow line rather than printed.
MAX_PER_CYCLE = 12
MAX_TOTAL = 60

# Gmail clips a message body over 102KB and hides the remainder behind a "View
# entire message" link, which is exactly where the low-ranked tail would go.
# Stay well under it.
MAX_EMAIL_BYTES = 80_000


def _get(row, key, default=""):
    """sqlite3.Row has no .get(), and older rows may predate a column."""
    try:
        return row[key] if row[key] is not None else default
    except (IndexError, KeyError):
        return default


def cycle_key(cycle) -> tuple:
    """Sort cycles chronologically; 'unknown' last.

    Accepts either the "summer 2027" string form or the (term, year) tuple the
    config parses into, so the CLI and the digest order cycles identically.
    """
    if isinstance(cycle, tuple):
        term, year = cycle
        return (0, int(year), TERM_ORDER.get(str(term).lower(), 9))
    parts = str(cycle).split()
    if len(parts) == 2 and parts[1].isdigit():
        return (0, int(parts[1]), TERM_ORDER.get(parts[0].lower(), 9))
    return (1, 0, 0)


# ---------------------------------------------------------------------------
# Collapsing near-duplicates
# ---------------------------------------------------------------------------

_PARENS = re.compile(r"\([^)]*\)")
_CYCLE_WORDS = re.compile(r"\b(spring|summer|fall|autumn|winter)\b|\b20\d{2}\b", re.I)
_NOISE = re.compile(r"[^a-z0-9 ]+")


def _dedupe_key(job) -> tuple:
    """Company, cycle, and a title stripped down to the role itself.

    Big employers post one role once per city - "Software Engineer Intern
    (Seattle)", "... (NYC)". That is one decision for the reader, not three, so
    they collapse into a single entry naming the extra locations.

    Cycle stays in the key even though cycle words are stripped from the title.
    The same role offered for summer 2027 and for fall 2026 is two different
    opportunities with deadlines months apart, and merging them would hide one.
    """
    title = _PARENS.sub(" ", _get(job, "title", "").lower())
    title = _CYCLE_WORDS.sub(" ", title)
    title = _NOISE.sub(" ", title)
    return (_get(job, "company", "").lower(),
            _get(job, "cycle", "unknown") or "unknown",
            " ".join(title.split()))


def _collapse(jobs: list) -> list[dict]:
    """Merge near-duplicate postings, keeping the best-scoring one as primary."""
    groups: dict[tuple, list] = {}
    for j in jobs:
        groups.setdefault(_dedupe_key(j), []).append(j)

    out = []
    for group in groups.values():
        group.sort(key=lambda j: -(j["score"] or 0))
        best = group[0]
        locations = []
        for j in group:
            loc = _get(j, "location")
            if loc and loc not in locations:
                locations.append(loc)
        out.append({
            "company": _get(best, "company"),
            "title": _get(best, "title"),
            "score": best["score"],  # may be None: aggregator pass-through
            "url": _get(best, "url"),
            "id": _get(best, "id"),
            "why": _get(best, "why"),
            "tag": _get(best, "tag", "other"),
            "cycle": _get(best, "cycle", "unknown") or "unknown",
            "posted_at": _get(best, "posted_at"),
            "locations": locations,
            "extra": len(group) - 1,
        })
    return out


def _entry_lines(j: dict) -> list[str]:
    locs = j["locations"]
    if not locs:
        loc = "-"
    elif len(locs) == 1:
        loc = locs[0]
    else:
        loc = f"{locs[0]} +{len(locs) - 1} more"

    meta = [loc]
    if j["posted_at"]:
        meta.append("posted " + str(j["posted_at"])[:10])
    if j.get("cycle"):
        meta.append(str(j["cycle"]))
    meta.append(f"`{j['tag']}`")
    if j["extra"]:
        meta.append(f"{j['extra']} similar posting{'s' if j['extra'] > 1 else ''}")

    lines = [
        f"**{j['company']} - {j['title']}**  "
        f"`{j['score'] if j['score'] is not None else 'list'}`",
        " · ".join(meta),
    ]
    if j["why"]:
        lines.append(f"> {j['why']}")
    lines.append(f"[Apply]({j['url']})  ·  `jobcrawl applied {str(j['id'])[:8]}`")
    lines.append("")
    return lines


def _warnings_block(warnings: list[str] | None) -> list[str]:
    """Board-health alerts belong in the digest - the one thing read daily.

    A board that silently 404s for weeks is a company quietly missing from
    the feed, which is invisible everywhere except here.
    """
    if not warnings:
        return []
    lines = [f"## ⚠ Board health  ({len(warnings)})", ""]
    lines += [f"- {w}" for w in warnings]
    lines.append("")
    return lines


def render(jobs: list, max_per_cycle: int = MAX_PER_CYCLE,
           max_total: int = MAX_TOTAL, warnings: list[str] | None = None) -> str:
    """Build the markdown digest: ONE list, best score first.

    The digest used to be grouped into cycle sections, each sorted by score.
    The user reads the email top to bottom and asked for a ranking (2026-09-25:
    "stop sending me mails without ranking"), so every posting now sits in one
    list ordered by score, with unscored pass-through rows at the end; the
    cycle rides along on each entry's meta line instead. `max_per_cycle` is
    kept for callers and ignored. Pass max_total=0 to disable the cap.
    """
    today = dt.date.today().isoformat()
    if not jobs:
        return "\n".join([f"# Job digest - {today}", "", "Nothing new.", ""]
                         + _warnings_block(warnings))

    entries = _collapse(jobs)
    entries.sort(key=lambda e: (e["score"] is None, -(e["score"] or 0)))

    collapsed = len(jobs) - len(entries)
    headline = f"**{len(entries)} postings, best first**"
    if collapsed:
        headline += f" · {collapsed} near-duplicate{'s' if collapsed > 1 else ''} merged"

    lines = [f"# Job digest - {today}", "", headline, ""]

    # A one-line map of the day by cycle, so the shape is clear before scrolling.
    by_cycle: dict[str, int] = {}
    for e in entries:
        by_cycle[e["cycle"]] = by_cycle.get(e["cycle"], 0) + 1
    if len(by_cycle) > 1:
        lines += [" · ".join(f"{c.title()}: {n}" for c, n in
                             sorted(by_cycle.items(), key=lambda kv: cycle_key(kv[0]))), ""]

    room = len(entries) if not max_total else min(len(entries), max_total)
    for e in entries[:room]:
        lines += _entry_lines(e)
    hidden = len(entries) - room
    if hidden > 0:
        lines.append(f"_...and {hidden} more. See them with_ `jobcrawl list`")
        lines.append("")

    lines += _warnings_block(warnings)
    return "\n".join(lines)


def to_html(md: str) -> str:
    """Minimal markdown -> HTML. Enough for an email body, not a full parser.

    Escaping happens first so that job titles containing &, < or > - "R&D",
    "C++ <2yrs>" - survive as text instead of corrupting the markup. The
    markdown markers themselves are untouched by escaping, so the regexes below
    still match, and URLs correctly end up with &amp; in href.
    """
    h = html.escape(md, quote=False)
    h = re.sub(r"^# (.+)$", r"<h2>\1</h2>", h, flags=re.M)
    h = re.sub(r"^## (.+)$", r"<h3>\1</h3>", h, flags=re.M)
    h = re.sub(r"^&gt; (.+)$", r"<p style='color:#666;margin:2px 0'><i>\1</i></p>",
               h, flags=re.M)
    # The href must be double-quoted: escape() above ran with quote=False, so
    # a URL's apostrophe stays raw and would terminate a single-quoted
    # attribute mid-link (real postings carry them - "Bachelor's-..."). The
    # rarer literal double quote gets neutralized here for the same reason.
    h = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               lambda m: '<a href="{}">{}</a>'.format(
                   m.group(2).replace('"', "&quot;"), m.group(1)), h)
    h = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", h)
    # Word-style emphasis only: the marker must start after whitespace and end
    # before it. A loose `_..._` corrupts hrefs - plenty of Workday and
    # utm-tagged URLs carry two underscores on one line, and those are always
    # flanked by word characters.
    h = re.sub(r"(?<!\S)_([^_\n]+)_(?!\S)", r"<i>\1</i>", h, flags=re.M)
    h = re.sub(r"`([^`]+)`", r"<code style='background:#eee;padding:1px 4px'>\1</code>", h)
    h = h.replace("\n\n", "</p><p>")
    # Remaining single newlines are intentional line breaks (the lines of one
    # digest entry); without <br> they render as one run-on line in the email.
    h = h.replace("\n", "<br>")
    return ("<body style='font-family:-apple-system,sans-serif;max-width:640px'>"
            f"<p>{h}</p></body>")


def write_file(md: str, outdir: str = "digests") -> str:
    Path(outdir).mkdir(exist_ok=True)
    path = Path(outdir) / f"{dt.date.today().isoformat()}.md"
    path.write_text(md, encoding="utf-8")
    return str(path)


def split_email(body: str, limit: int = MAX_EMAIL_BYTES) -> list[str]:
    """Split an over-long body into email-sized parts at paragraph boundaries.

    Replaces the old truncate-and-point-at-`jobcrawl list` backstop: the user
    wants every posting in their inbox, so a digest that outgrows one email
    becomes several, never a stub. The budget is measured against the
    *rendered HTML*, not the markdown, since that is what the mail client
    actually receives and clips; inline styles roughly double the size.

    A block that alone exceeds the limit ships as its own part rather than
    looping forever - the mail client clips it, which is the least-bad option
    for a pathological single entry.
    """
    if len(to_html(body).encode("utf-8")) <= limit:
        return [body]

    parts: list[list[str]] = [[]]
    for block in body.split("\n\n"):
        candidate = parts[-1] + [block]
        if parts[-1] and len(to_html("\n\n".join(candidate)).encode("utf-8")) > limit:
            parts.append([block])
        else:
            parts[-1] = candidate
    return ["\n\n".join(p) for p in parts if p]


def send_email(md: str, subject: str) -> bool:
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("DIGEST_TO")
    if not all([host, user, pw, to]):
        return False

    # A digest bigger than one email becomes several, sent on one SMTP
    # connection; the (i/n) subject keeps the inbox thread legible.
    parts = split_email(md)
    port = int(os.environ.get("SMTP_PORT", 587))
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pw)
        for i, body in enumerate(parts):
            msg = EmailMessage()
            msg["Subject"] = subject + (
                f" ({i + 1}/{len(parts)})" if len(parts) > 1 else "")
            msg["From"] = user
            msg["To"] = to
            msg.set_content(body)
            msg.add_alternative(to_html(body), subtype="html")
            s.send_message(msg)
    return True
