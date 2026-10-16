"""Find a company's CURRENT applicant-tracking board from its own website.

The public GitHub lists only tell us where a company posted LAST time. When
the link there is the company's own careers page, or an ATS it has since
left (Nominal went Lever -> Gem between its 2026 and 2027 intern reqs), the
list gives us no board to poll and the posting is missed until some list
catches up. The company's website does not lie about this: its careers page
links to or embeds whatever ATS it uses today, and every hosted ATS we can
poll has a recognizable URL shape. So: fetch the site, find the careers
page, pull the ATS links, and hand them to `derive_board`.

Two entry points feed `cmd_boards`:
  - `simplify_website(company_url)` turns a feed row's simplify.jobs company
    page into the company's real website (the feed's `company_url` field
    only ever points at simplify.jobs).
  - `resolve_website(site)` walks the site for ATS boards.
Results are cached in resolve_cache.json (generated, committed) so each
company costs a handful of requests once a month, not every regen.
"""
from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin, urlparse

from . import sources

# Hosted-ATS URL shapes as they appear in page HTML: links, iframes, embed
# scripts, and the JSON APIs the embeds call. Each maps straight to a
# (ats, token) the crawl can poll. derive_board handles the common posting
# and board URLs; these are the extra shapes only careers pages use.
_API_SHAPES = [
    (re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9_.-]+)", re.I), "lever"),
    (re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_.-]+)", re.I), "greenhouse"),
    (re.compile(r"boards\.greenhouse\.io/embed/job_board(?:/js)?\?(?:[^\"'\s]*&)?for=([A-Za-z0-9_.-]+)", re.I), "greenhouse"),
    (re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.%-]+)", re.I), "ashby"),
    (re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)/embed", re.I), "ashby"),
    (re.compile(r"jobs\.gem\.com/([A-Za-z0-9_-]+)", re.I), "gem"),
]
_URL_IN_HTML = re.compile(r"https?://[^\s\"'<>()]+", re.I)
_HREF = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
_CAREERS_WORDS = re.compile(r"career|jobs?\b|join[- ]?us|work[- ]with[- ]us|open(?:ings|[- ]roles|[- ]positions)|we.?re[- ]hiring|hiring", re.I)
_CAREERS_PATHS = ["/careers", "/jobs", "/join-us"]
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def ats_boards(html: str) -> list[tuple[str, str]]:
    """Every pollable (ats, token) a page's HTML points at, first seen first."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    # Careers pages embed their ATS URLs inside JSON as often as inside
    # href attributes, where the slashes arrive escaped ("https:\/\/boards
    # .greenhouse.io\/c3ascend\/jobs"); left alone, the trailing backslash
    # becomes part of the token.
    text = unescape(html or "").replace("\\/", "/")

    def add(b):
        if b and (b[0], b[1].lower()) not in seen:
            seen.add((b[0], b[1].lower()))
            found.append(b)

    for pat, ats in _API_SHAPES:
        for m in pat.finditer(text):
            add((ats, m.group(1)))
    for m in _URL_IN_HTML.finditer(text):
        url = m.group(0).rstrip(".,;:)\\")
        add(sources.derive_board(url))
    return found


def careers_links(html: str, base: str, limit: int = 3) -> list[str]:
    """Same-site links whose href or text says careers/jobs/join, best first."""
    host = urlparse(base).netloc.lower().removeprefix("www.")
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for m in _HREF.finditer(html or ""):
        href, label = m.group(1).strip(), re.sub(r"<[^>]+>", " ", m.group(2))
        full = urljoin(base, href)
        u = urlparse(full)
        if u.scheme not in ("http", "https"):
            continue
        if u.netloc.lower().removeprefix("www.") != host:
            # A careers subdomain (careers.acme.com) counts as the same site;
            # anything else is a link out, and the ATS ones are caught by
            # ats_boards directly.
            if not u.netloc.lower().endswith("." + host):
                continue
        hit_path = bool(_CAREERS_WORDS.search(u.path))
        hit_text = bool(_CAREERS_WORDS.search(label))
        if not (hit_path or hit_text):
            continue
        key = full.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        scored.append((-(hit_path + hit_text), key))
    return [u for _, u in sorted(scored)][:limit]


def resolve_website(site: str, fetch=None, max_pages: int = 4) -> tuple[str, str] | None:
    """Walk a company website for the ATS board it uses today.

    Root page first (many sites embed the board or link it in the footer),
    then the careers-looking links on it, then a few conventional paths.
    Stops at the first page that names a board. Any fetch error just moves
    on: an unreachable site resolves to nothing, never raises.
    """
    fetch = fetch or _get
    if not site.startswith("http"):
        site = "https://" + site
    base = site.rstrip("/")
    tried: set[str] = set()
    queue = [base]
    pages = 0
    while queue and pages < max_pages:
        url = queue.pop(0)
        key = url.rstrip("/")
        if key in tried:
            continue
        tried.add(key)
        html = fetch(url)
        pages += 1
        if html is None:
            continue
        boards = ats_boards(html)
        if boards:
            return boards[0]
        if url == base:
            queue.extend(careers_links(html, base))
            queue.extend(base + p for p in _CAREERS_PATHS)
    return None


def simplify_website(company_url: str, fetch=None) -> str | None:
    """The company's own website, read off its simplify.jobs company page."""
    fetch = fetch or _get
    if not company_url or "simplify.jobs/c/" not in company_url:
        return None
    html = fetch(company_url)
    if not html:
        return None
    m = _NEXT_DATA.search(html)
    if not m:
        return None
    try:
        company = (json.loads(m.group(1)).get("props", {}).get("pageProps", {})
                   .get("company") or {})
    except (ValueError, AttributeError):
        return None
    url = company.get("url") or company.get("website")
    if not url or "simplify.jobs" in url:
        return None
    return url


def _get(url: str) -> str | None:
    try:
        r = sources.get(url, headers=sources.UA, timeout=12)
        if r.status_code != 200:
            return None
        ctype = r.headers.get("Content-Type", "")
        if ctype and "html" not in ctype and "json" not in ctype and "javascript" not in ctype:
            return None
        return r.text or ""
    except Exception:  # noqa: BLE001 - any failure means "no page here"
        return None


_SLUG_SUFFIXES = re.compile(r"\b(?:inc|llc|ltd|corp|corporation|co|company|group|holdings|capital|technologies|labs)\b\.?", re.I)
# Hosted-ATS boards whose public API answers for a bare slug; each returns
# the board's job list, so an answer with rows is a board that exists.
_SLUG_APIS = (
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "jobs"),
    ("lever", "https://api.lever.co/v0/postings/{slug}?mode=json", None),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/{slug}", "jobs"),
)


def slug_candidates(name: str, company_url: str | None = None) -> list[str]:
    """Board slugs a company's name suggests, most specific first.

    Greenhouse, Lever and Ashby tokens are almost always the company name
    with the spaces removed (jumptrading, epicgames, squarepointcapital),
    sometimes hyphenated; the simplify.jobs company slug is a third
    spelling. Short slugs are left out: a five-letter word is as likely to
    be someone else's board (Ashby's `runway` is cfo.ai) as this company's.
    """
    out: list[str] = []
    bases = [name or ""]
    stripped = _SLUG_SUFFIXES.sub("", name or "").strip()
    if stripped and stripped.lower() != (name or "").lower():
        bases.append(stripped)
    if company_url:
        tail = company_url.rstrip("/").rsplit("/", 1)[-1]
        if tail:
            bases.append(tail.replace("-", " "))
    for b in bases:
        words = re.findall(r"[a-z0-9]+", b.lower())
        if not words:
            continue
        for slug in ("".join(words), "-".join(words)):
            if len(slug) >= 6 and slug not in out:
                out.append(slug)
    return out


def probe_slugs(name: str, company_url: str | None = None,
                probe=None) -> tuple[str, str] | None:
    """Ask the hosted ATSes directly whether a name-shaped board exists.

    The website walk finds nothing when a company fronts its board with its
    own careers page (jumptrading.com embeds Greenhouse through JS, Epic
    Games links its roles one at a time) - which is how Jump Trading, a
    name on the urgent list, sat on no tier with 33 intern postings open
    (2026-10-11). A probe hit needs at least one job: an empty board proves
    only that the slug is taken. `probe(url) -> parsed JSON or None` is
    injectable for tests; the default goes through sources.get.
    """
    probe = probe or _probe_json
    for slug in slug_candidates(name, company_url):
        for ats, tmpl, key in _SLUG_APIS:
            data = probe(tmpl.format(slug=slug))
            rows = data.get(key) if (key and isinstance(data, dict)) else data
            if isinstance(rows, list) and rows:
                return ats, slug
    return None


def _probe_json(url: str):
    try:
        r = sources.get(url, headers=sources.UA, timeout=sources.TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001 - any failure is "no board here"
        return None


def resolve_company(name: str, company_url: str | None, website: str | None = None,
                    fetch=None, probe=None) -> dict:
    """One company: {website, ats, token} (ats/token None when nothing found).

    The website walk runs first; when it names no board, the company's
    name is tried as a slug on the hosted ATSes (`probe_slugs`).
    """
    site = website or simplify_website(company_url or "", fetch=fetch)
    board = resolve_website(site, fetch=fetch) if site else None
    if not board:
        board = probe_slugs(name, company_url, probe=probe)
    return {"name": name, "website": site,
            "ats": board[0] if board else None,
            "token": board[1] if board else None}
