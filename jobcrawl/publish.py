"""
Publish surfaced postings as Simplify-style README tables in a GitHub repo.

Same table UI as SimplifyJobs/Summer2026-Internships: an HTML table with a
clickable Apply button per row, repeat companies collapsed to an arrow, and an
age column. Each page is one flat table, newest first, regardless of cycle.
Four pages: README.md carries every CS role (including engineering roles at
trading firms), QUANT.md carries quant/trading roles proper - different job
searches with different timelines - and SUMMER.md / COOPS.md are season views
of the general list for when only one cycle is being worked.

The repo is the reading surface; the SQLite store stays the source of truth.
Rendering is pure (rows -> markdown string) so tests never touch git; the git
half is a thin commit/push wrapper that must never crash a run - a digest that
fails to publish is still a digest.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import subprocess
from pathlib import Path

APPLY_BUTTON = "https://i.imgur.com/6cFAMUo.png"  # the button Simplify's list uses

_COOP_RE = re.compile(r"\bco-?op\b", re.I)
_SUMMER_RE = re.compile(r"\bsummer\b", re.I)
_OFFCYCLE_RE = re.compile(r"\b(?:fall|autumn|winter|spring)\b", re.I)
_YEAR_RE = re.compile(r"\b20(2[4-9]|3\d)\b")


def season_of(row) -> str:
    """Which season section a posting belongs to: 'summer', 'coop', 'unknown'.

    A title that says co-op is a co-op no matter the term - summer co-ops sit
    with the co-ops, since the co-op/internship distinction is what the reader
    is scanning for. Otherwise the scored cycle decides, and the title is only
    a fallback for the many rows whose cycle came back unknown.

    A title naming a year but no term ("NVIDIA 2027 Internships: ...",
    "2027 Intern - Software Developer") is an umbrella req whose description
    states no season either - summer is the main cycle such reqs recruit for,
    and the bottom bucket is where good postings go to be missed.
    """
    title = _field(row, "title") or ""
    if _COOP_RE.search(title):
        return "coop"
    for text in ((_field(row, "cycle") or ""), title):
        if _SUMMER_RE.search(text):
            return "summer"
        if _OFFCYCLE_RE.search(text):
            return "coop"
    if _YEAR_RE.search(title):
        return "summer"
    return "unknown"


def split_seasons(rows: list) -> tuple[list, list]:
    """(summer rows, co-op rows), each keeping the input ordering.

    Feeds the per-season pages: SUMMER.md gets the summer cycle plus every
    posting whose season could not be determined (season-less postings mostly
    mean summer, and a third page for "unknown" would just hide them), and
    COOPS.md gets the off-cycle terms and anything calling itself a co-op.
    """
    summer, coop = [], []
    for r in rows:
        (coop if season_of(r) == "coop" else summer).append(r)
    return summer, coop

QUANT_PAGE = "QUANT.md"
MAIN_PAGE = "README.md"
SUMMER_PAGE = "SUMMER.md"
COOP_PAGE = "COOPS.md"

# The split is by ROLE, not firm: a trading shop's "Software Engineer Intern"
# stays on the general page with every other CS role, and only quant/trading
# roles proper move to QUANT.md. The scorer's `tag` is the primary signal.
# Aggregator pass-through rows carry no scorer tag, so the title is the only
# signal left. Deliberately narrow: "quant"/"quantitative"/"trader" name the
# role itself, while words like "trading" also appear in engineering titles
# ("SWE Intern - Trading Systems") that belong on the general page.
# (?!um) keeps "Quantum Computing Intern" - a physics role, not a finance
# one - off the quant page while still matching "quantitative".
_QUANT_TITLE_RE = re.compile(r"\bquant(?!um)|\btraders?\b", re.I)


_LEGAL_SUFFIX = re.compile(
    r"\b(?:inc|llc|ltd|l\.?p|corp|corporation|co|company|group|holdings|"
    r"technologies|labs?)\b\.?$", re.I)


def _group_key(company: str) -> str:
    """Normalize a company name down to what should share one table group.

    The same firm reaches us under several display names - a board titled
    "Jane Street (intern)" and one titled "Jane Street", or "IMC" and
    "IMC Trading" - because the name comes from whichever ATS token we polled.
    Strip the parenthetical, the legal suffix, and the punctuation, and they
    collapse. This is a grouping key only; each row still renders its own
    title and link.
    """
    name = re.sub(r"\s*\([^)]*\)", " ", company or "")
    name = re.sub(r"[^\w\s]", " ", name).strip()
    name = _LEGAL_SUFFIX.sub("", name).strip()
    return re.sub(r"\s+", " ", name).lower()


def group_by_company(rows: list) -> list[tuple[str, list]]:
    """[(display name, rows)], groups newest-first, rows keeping input order.

    A group takes the position of its newest posting, so the page still reads
    newest-first at the company level while every role from one firm sits in
    one block. Groups whose key is a whole-word prefix of another's fold into
    the longer one ("Hudson River" into "Hudson River Trading"), which is how
    the same firm split across two boards gets reunited.
    """
    groups: dict[str, list] = {}
    for i, r in enumerate(rows):
        groups.setdefault(_group_key(_field(r, "company") or ""), []).append((i, r))

    # Fold prefix keys into the longest key they prefix, shortest first so
    # "hudson river" reaches "hudson river trading" and not some middle name.
    for key in sorted(groups, key=len):
        longer = [k for k in groups
                  if k != key and (k + " ").startswith(key + " ")]
        if longer:
            target = max(longer, key=len)
            groups[target] += groups.pop(key)

    out = []
    for members in groups.values():
        members.sort(key=lambda pair: pair[0])  # input order within the group
        names = [(_field(r, "company") or "").strip() for _, r in members]
        # Label the group with the cleanest of the names it was built from:
        # no board-token parenthetical ("Jane Street" over "Jane Street
        # (intern)"), no legal suffix ("Acme" over "Acme, Inc."), then the
        # fullest ("Hudson River Trading" over the truncated "Hudson River").
        display = max(set(names), key=lambda n: (
            "(" not in n,
            not _LEGAL_SUFFIX.search(re.sub(r"[^\w\s]", " ", n).strip()),
            names.count(n),
            len(n),
        ))
        out.append((members[0][0], display, [r for _, r in members]))
    # A group sits where its newest posting sat, so the page still reads
    # newest-first at the company level.
    out.sort(key=lambda g: g[0])
    return [(display, rs) for _, display, rs in out]


def _day_key(row) -> str:
    """The day-granularity key store.surfaced orders the page by.

    store.surfaced sorts by SQLite's date(), which converts an offset-bearing
    timestamp to UTC before taking the day. Slicing the raw string instead
    disagrees with that order for offsets that cross midnight, and a
    non-monotonic day sequence makes group_by_company_daily split one
    company's same-day block in two. Normalize the same way SQLite does.
    """
    raw = str(_field(row, "posted_at") or _field(row, "first_seen") or "")
    try:
        d = dt.datetime.fromisoformat(raw)
    except ValueError:
        return raw[:10]
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc)
    return d.date().isoformat()


def group_by_company_daily(rows: list) -> list[tuple[str, list]]:
    """group_by_company, but bounded to one day at a time.

    Grouping across the whole page let a company block sit at its newest
    posting's slot and drag the firm's entire history to the top - one new
    posting from a 100-row firm hoisted every old row above every other
    company's fresh ones. Grouping within a day keeps the page honestly
    newest-first: a firm that posted on three days appears three times, each
    block at its own day's position. Rows arrive sorted by day, so one pass
    over consecutive same-day runs is enough.
    """
    out, batch, day = [], [], None
    for r in rows:
        key = _day_key(r)
        if key != day and batch:
            out += group_by_company(batch)
            batch = []
        day = key
        batch.append(r)
    if batch:
        out += group_by_company(batch)
    return out


def _field(row, key):
    """Read a column that may not exist (sqlite3.Row raises, dicts vary)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def is_quant(row) -> bool:
    """Does this posting belong on the quant page?"""
    if (_field(row, "tag") or "") == "quant":
        return True
    return bool(_QUANT_TITLE_RE.search(_field(row, "title") or ""))


def split_quant(rows: list) -> tuple[list, list]:
    """(quant rows, everything else), each keeping the input ordering."""
    quant, rest = [], []
    for r in rows:
        (quant if is_quant(r) else rest).append(r)
    return quant, rest


def _age(row) -> str:
    """Days since the posting went up (or since we first saw it), as '3d'/'2mo'."""
    raw = row["posted_at"] or row["first_seen"]
    try:
        when = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return "-"
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    days = max(0, (dt.datetime.now(dt.timezone.utc) - when).days)
    return f"{days // 30}mo" if days > 30 else f"{days}d"


def _location_cell(location: str) -> str:
    """Multi-location strings fold into a <details> block past two entries.

    Two, not three: location is the widest cell after the role title, and a
    wide table scrolls sideways on GitHub, hiding the columns past it.
    """
    locs = [p.strip() for p in (location or "").split(";") if p.strip()] or ["-"]
    joined = "<br>".join(html.escape(p) for p in locs)
    if len(locs) <= 2:
        return joined
    return (f"<details><summary><strong>{len(locs)} locations</strong></summary>"
            f"{joined}</details>")


def _apply_cell(row) -> str:
    if row["status"] == "applied":
        return '<div align="center">✅ Applied</div>'
    if row["status"] == "closed":
        return '<div align="center">🔒</div>'
    url = html.escape(row["url"], quote=True)
    # width 100 -> ~34px tall. 80 copied Simplify's row-button height (27px),
    # which reads as tiny; the source image is 880x300 so it scales cleanly.
    return (f'<div align="center"><a href="{url}">'
            f'<img src="{APPLY_BUTTON}" width="100" alt="Apply"></a></div>')


def render_readme(rows: list, *, title: str = "Job Tracker",
                  blurb: str | None = None, cross_link: str = "") -> str:
    """Rows (newest first) -> the full page markdown."""
    now = dt.datetime.now(dt.timezone.utc)
    lines = [
        f"# {title}",
        "",
        blurb or ("Auto-generated by jobcrawl - company career pages are polled "
                  "at the\nATS source, scored against my profile by Claude, and "
                  "new matches\nland here the moment they are found."),
        "",
        f"**{len(rows)} tracked postings** · last updated "
        f"{now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    if cross_link:
        lines += [cross_link, ""]
    lines += [
        "Legend: ✅ applied · 🔒 no longer listed · ↳ same company as the row above",
        "",
    ]

    # No Cycle column: the season pages carry that split, most titles state
    # their cycle anyway, and every column here costs width - a wide table
    # scrolls sideways on GitHub, which had been hiding the Age column.
    lines += [
        "<table>",
        "<thead>",
        "<tr>",
        "<th>Company</th>",
        "<th>Role</th>",
        "<th>Location</th>",
        "<th>Score</th>",
        "<th>Age</th>",
        "<th>Apply</th>",
        "</tr>",
        "</thead>",
        "<tbody>",
    ]
    for display, members in group_by_company_daily(rows):
        for n, r in enumerate(members):
            cell = (f"<strong>{html.escape(display)}</strong>" if n == 0
                    else "&nbsp;&nbsp;&nbsp;↳")
            lines += [
                "<tr>",
                f"<td>{cell}</td>",
                f"<td>{html.escape(r['title'])}</td>",
                f"<td>{_location_cell(r['location'])}</td>",
                f"<td>{r['score'] if r['score'] is not None else '-'}</td>",
                f"<td>{_age(r)}</td>",
                f"<td>{_apply_cell(r)}</td>",
                "</tr>",
            ]
    lines += ["</tbody>", "</table>", ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git plumbing
# ---------------------------------------------------------------------------

def _git(repo_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        check=True, capture_output=True, text=True, timeout=120,
    ).stdout.strip()


def push_pages(pages: dict[str, str], repo_dir: str | Path = ".",
               new_count: int = 0) -> bool:
    """Write the tracker pages into this repo and push them in one commit.

    The code and the tracker share one repo (Job_tracker), so publishing is a
    plain commit of the rendered pages - only those paths are staged, so
    uncommitted dev work is never swept along. Pulls with --rebase --autostash
    first so a hand-edit on GitHub or a dirty working tree never wedges the
    hourly cron, and skips the commit entirely when every rendered table is
    byte-identical to what is already on disk.
    """
    repo_dir = Path(repo_dir)
    changed = {
        name: md for name, md in pages.items()
        if _strip_volatile((repo_dir / name).read_text()
                           if (repo_dir / name).is_file() else "")
        != _strip_volatile(md)
    }
    if not changed:
        return False

    if _git(repo_dir, "ls-remote", "--heads", "origin"):
        _git(repo_dir, "pull", "--rebase", "--autostash", "origin", "main")
    for name, md in changed.items():
        (repo_dir / name).write_text(md)
        _git(repo_dir, "add", name)
    tail = f": {new_count} new" if new_count else ""
    _git(repo_dir, "commit", "-m",
         f"Update job list{tail} ({dt.date.today().isoformat()})")
    _git(repo_dir, "push", "-u", "origin", "main")
    return True


def _strip_volatile(md: str) -> str:
    """Drop the timestamp line so an unchanged table doesn't commit hourly."""
    return "\n".join(l for l in md.splitlines() if not l.startswith("**") or
                     "last updated" not in l)
