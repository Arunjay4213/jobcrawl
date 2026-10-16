"""
Fetch job postings straight from ATS public APIs.

Greenhouse / Lever / Ashby / SmartRecruiters / Workable / Rippling all expose
unauthenticated JSON endpoints for their hosted job boards. Workday (both its
myworkdayjobs.com and myworkdaysite.com domains) needs a POST but is still
open. The `simplify` adapter ingests the SimplifyJobs GitHub feed as a safety
net for hosts with no public API. Anything else falls back to Firecrawl + an
LLM extraction pass.

Every adapter returns a list of dicts with the same shape:
    {company, title, location, url, posted_at, description, source}
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import threading
import time
from collections import defaultdict
from http.cookiejar import DefaultCookiePolicy
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests
import requests.adapters

from . import prefilter

TIMEOUT = 25
UA = {"User-Agent": "jobcrawl/1.0 (personal job search tool)"}


class Unchanged(Exception):
    """Raised when a conditional fetch got a 304 - the board did not change."""


# ---------------------------------------------------------------------------
# Politeness: per-host throttling and retry
# ---------------------------------------------------------------------------
#
# Boards are fetched from a thread pool, and Workday needs up to ~30 POSTs
# per board (facet discovery, then up to three queries x ten pages). Fired
# unthrottled that is thousands of
# requests an hour, and Workday answers with 429s - which cost real coverage,
# since a rate-limited board looks exactly like an empty one. Two limits fix
# it: one request in flight per host at a time with a minimum gap between
# them, and a ceiling on how many hosts of one family we hammer at once.

RETRY_STATUS = {429, 500, 502, 503, 504}
RETRIES = 3
MAX_BACKOFF = 45

# host suffix -> (min seconds between requests to one host, max hosts at once)
# The workday family ceiling counts DIFFERENT tenants in flight, not load on
# any one of them - each tenant is its own host and keeps the 0.35s gap. At 6,
# a 2026-08-31 py-spy profile showed ~2/3 of fetch workers parked on this
# semaphore while every other ATS was long done; the whole run was a workday
# queue. 429 risk is per-tenant, so widening cross-tenant parallelism is safe.
# Correction, 2026-09-09: the limit is per CLUSTER, not per tenant. Widening
# the family ceiling 12 -> 24 drew 429s from 16 then 25 tenants in one run,
# every one on wd1 or wd5 - the two clusters holding 255 and 190 of the
# ~620 due boards - and none had ever 429'd before. Workday rate-limits the
# source IP per wdN cluster, shared across its tenants. So the ceiling is
# keyed by cluster (_policy_for rewrites the family to "wd1.myworkdayjobs.
# com"): at the old global 12, wd1 carried ~5 in flight without incident,
# so 6 per cluster keeps each cluster at its proven load while the clusters
# run beside each other. cli.fetch_all's pool must stay wider than the sum
# of the busy clusters' ceilings or nothing else gets a worker.
_HOST_POLICY = {
    "myworkdayjobs.com": (0.35, 6),
    "myworkdaysite.com": (0.35, 6),
    "icims.com": (0.5, 4),
    # Detail pages, one per aggregator listing: a backfill is 500+ calls in a
    # row against a site that owes us nothing. One in flight, half a second
    # apart.
    "simplify.jobs": (0.5, 1),
    # The weekly YC sweep walks ~1,500 company jobs pages in a row; the yc
    # boards it mints then poll the same host on their 6h clock.
    "ycombinator.com": (0.3, 1),
}
_DEFAULT_POLICY = (0.0, 0)  # no gap, no family ceiling

_locks_guard = threading.Lock()
_host_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_host_last: dict[str, float] = {}
_family_sems: dict[str, threading.Semaphore] = {}


_WORKDAY_CLUSTER = re.compile(r"(?:^|\.)(wd\d+\.myworkday(?:jobs|site)\.com)$")


def _policy_for(host: str) -> tuple[str, float, int]:
    for suffix, (gap, ceiling) in _HOST_POLICY.items():
        if host == suffix or host.endswith("." + suffix):
            # Workday's ceiling is per wdN cluster (see _HOST_POLICY), so
            # the family that owns the semaphore is the cluster, not the
            # whole platform. myworkdaysite hosts ARE the cluster host.
            m = _WORKDAY_CLUSTER.search(host)
            return (m.group(1) if m else suffix), gap, ceiling
    return "", *_DEFAULT_POLICY


class _Throttle:
    """Context manager holding a host's turn, and its family's, for one request."""

    def __init__(self, url: str):
        self.host = (urlparse(url).hostname or "").lower()
        self.family, self.gap, ceiling = _policy_for(self.host)
        self.sem = None
        if ceiling:
            with _locks_guard:
                self.sem = _family_sems.setdefault(
                    self.family, threading.Semaphore(ceiling))

    def __enter__(self):
        if not self.family:
            return self
        self.sem.acquire()
        with _locks_guard:
            lock = _host_locks[self.host]
        lock.acquire()
        wait = self.gap - (time.monotonic() - _host_last.get(self.host, 0.0))
        if wait > 0:
            time.sleep(wait)
        return self

    def __exit__(self, *exc):
        if not self.family:
            return False
        _host_last[self.host] = time.monotonic()
        _host_locks[self.host].release()
        self.sem.release()
        return False


def _retry_after(r) -> float | None:
    raw = r.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:  # HTTP-date form; a fixed pause is close enough
        return 30.0


def request(method: str, url: str, **kw):
    """One HTTP call, throttled per host and retried on rate limits.

    Retries only the statuses that mean "try again" (429 and the 5xx family).
    A 404 is a dead board and must surface immediately so board health records
    it. Honors Retry-After when the server sends one, otherwise backs off
    exponentially, and returns the final response either way - the caller's
    raise_for_status still decides what counts as failure.
    """
    delay = 3.0
    for attempt in range(RETRIES + 1):
        with _Throttle(url):
            r = _send(method, url, **kw)
        if r.status_code not in RETRY_STATUS or attempt == RETRIES:
            return r
        time.sleep(min(_retry_after(r) or delay, MAX_BACKOFF))
        delay *= 2


_tls = threading.local()


def _send(method: str, url: str, **kw):
    """One raw HTTP call on this thread's keep-alive session. The test seam.

    This used to be `requests.request`, which builds a throwaway Session per
    call: a fresh TLS context, the CA bundle re-read from disk, and a full
    handshake for every one of the ~15k requests a run makes. A 2026-09-09
    py-spy profile put 78% of the run's 5+ CPU-minutes in urllib3's
    ssl_wrap_socket. A per-thread Session keeps connections alive, so a
    Workday board's 6-20 POSTs ride one handshake, and requests >= 2.32
    builds the SSL context once per adapter. Thread-local rather than shared
    because a Session's cookie jar and pool bookkeeping are not guarded.
    Cookies are refused outright: the old path was stateless, and a
    Workday session cookie carried between tenants is a behavior change
    nobody asked for.
    """
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        adapter = requests.adapters.HTTPAdapter(pool_connections=8,
                                                pool_maxsize=4)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _tls.session = s
    return s.request(method, url, **kw)


def get(url: str, **kw):
    return request("GET", url, **kw)


def post(url: str, **kw):
    return request("POST", url, **kw)


def _conditional_get_json(url: str, key: str, etags: dict | None,
                          timeout: int = TIMEOUT):
    """GET with If-None-Match when we hold an ETag for this board.

    Greenhouse, Lever, Ashby, and raw.githubusercontent all honor it (verified
    live), which turns most hourly requests into 304s - polite to them, faster
    for us. Raises Unchanged on 304; updates etags in place on 200.
    """
    headers = dict(UA)
    if etags is not None and etags.get(key):
        headers["If-None-Match"] = etags[key]
    r = get(url, headers=headers, timeout=timeout)
    if r.status_code == 304:
        raise Unchanged(key)
    r.raise_for_status()
    data = r.json()
    # Only after the body parses: storing the ETag for a response we then
    # failed to process would make the next run 304 right past postings we
    # never ingested.
    if etags is not None:
        et = r.headers.get("ETag")
        if et:
            etags[key] = et
    return data


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    # Headings end a line too: without it "<h3>Requirements</h3><li>U.S.
    # citizenship preferred" flattens onto one line and the citizenship gate
    # reads the heading as its requirement word.
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def _clean_title(s: str | None) -> str:
    """Collapse whitespace in a title.

    Scraped boards hand back the whole job card, newlines and all. Left alone
    those newlines break the digest layout and, worse, make the dedupe hash
    sensitive to invisible formatting changes.
    """
    return re.sub(r"\s+", " ", (s or "")).strip()


def _iso(value: Any) -> str | None:
    """Normalize the many timestamp formats these APIs use into ISO-8601."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        # Lever and Ashby use epoch milliseconds.
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat()
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return value
    return None


def _dicts(seq) -> list[dict]:
    """Defensive iteration for API job lists.

    The repair taxonomy for malformed payloads, applied consistently across
    adapters: a null-shaped FIELD is coerced to its empty value (this
    function turns a null list into []), a malformed ENTRY in an otherwise
    good list (a JSON null, a bare string) is skipped so one bad row never
    costs the whole board, and a wrong-shaped TOP-LEVEL response is the
    adapter's job to raise on - total damage must reach board health as a
    fetch error, not masquerade as an empty board.
    """
    return [x for x in (seq or []) if isinstance(x, dict)]


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------

def fetch_greenhouse(company: str, token: str, etags: dict | None = None) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    data = _conditional_get_json(url, f"greenhouse:{token}", etags)
    out = []
    for j in _dicts(data.get("jobs")):
        out.append({
            "company": company,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            # first_published is the honest posting date; updated_at gets
            # bumped whenever the board touches the posting, which made a
            # February role show as "posted today" in the digest.
            "posted_at": _iso(j.get("first_published") or j.get("updated_at")),
            "description": _strip_html(j.get("content")),
            "source": "greenhouse",
        })
    return out


def fetch_lever(company: str, token: str, etags: dict | None = None) -> list[dict]:
    # Lever runs a separate EU region with its own API host; a board hosted at
    # jobs.eu.lever.co does not exist on api.lever.co (it 404s there, which
    # looks identical to a dead board). Such boards carry an "eu:" token
    # prefix, assigned by derive_board and usable in config files too.
    api_host, slug = "api.lever.co", token
    if token.startswith("eu:"):
        api_host, slug = "api.eu.lever.co", token[3:]
    url = f"https://{api_host}/v0/postings/{slug}?mode=json"
    data = _conditional_get_json(url, f"lever:{token}", etags)
    if not isinstance(data, list):
        raise ValueError(f"lever: expected a list, got {type(data).__name__}")
    out = []
    for j in _dicts(data):
        cats = j.get("categories") or {}
        body = _strip_html(j.get("descriptionPlain") or j.get("description"))
        for lst in j.get("lists", []) or []:
            body += "\n" + _strip_html(lst.get("text", "")) + "\n" + _strip_html(lst.get("content", ""))
        out.append({
            "company": company,
            "title": j.get("text", ""),
            "location": _join_locations(
                [cats.get("location")] + list(cats.get("allLocations") or [])),
            "url": j.get("hostedUrl", ""),
            "posted_at": _iso(j.get("createdAt")),
            "description": body.strip(),
            "source": "lever",
        })
    return out


def _join_locations(locs) -> str:
    """Every office a posting lists, primary first, as one '; '-joined string.

    Ashby (`secondaryLocations`) and Lever (`categories.allLocations`) keep
    a posting's other offices outside the primary location field. Reading
    only the primary dropped US-eligible internships whose first office is
    abroad: Cohere's Winter 2027 SWE intern is primary "Canada" with
    "United States" secondary, Waabi's research intern is "Toronto, ON" plus
    "San Francisco, CA" - the US prefilter saw one foreign office and cut
    them before storage (2026-09-25). The joined string lets the prefilter
    find the US hint and hands the scorer's `us` flag the full office list.
    Duplicates (Lever repeats the primary in allLocations) collapse, case-
    insensitively, so a single-office posting keeps its old string - and so
    its old job_id hash.
    """
    out, seen = [], set()
    for loc in locs:
        loc = (loc or "").strip() if isinstance(loc, str) else ""
        if loc and loc.lower() not in seen:
            seen.add(loc.lower())
            out.append(loc)
    return "; ".join(out)


def _ashby_app_data(text: str) -> dict:
    """The window.__appData JSON on a jobs.ashbyhq.com page.

    raw_decode parses exactly one object and ignores the page tail, so no
    regex has to guess where the blob ends. Raises on anything unexpected:
    ashby is a CLOSABLE source, and a partial parse passed off as a complete
    board would close live rows, while a silent [] would roll fail_streak
    into empty_streak and keep a truly dead board off the health report.
    """
    marker = "window.__appData ="
    i = text.find(marker)
    if i < 0:
        raise ValueError("ashby: no __appData on hosted page")
    obj, _ = json.JSONDecoder().raw_decode(text[i + len(marker):].lstrip())
    return obj


def _find_key(obj, key: str):
    """First value under `key` anywhere in a nested dict/list, or None."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _fetch_ashby_hosted(company: str, token: str) -> list[dict]:
    """Fallback for orgs that disabled Ashby's public posting API.

    The API 404s for them, but their hosted page (jobs.ashbyhq.com/<token>)
    still embeds the full board in window.__appData - Whatnot's live SWE
    intern posting was invisible this way (2026-09-01). List entries carry
    no description, so rows get a detail_key and the enrich pass fills the
    text from the posting page before scoring.
    """
    r = get(f"https://jobs.ashbyhq.com/{quote(token)}",
            headers=_BROWSER_UA, timeout=TIMEOUT)
    r.raise_for_status()
    postings = _find_key(_ashby_app_data(r.text or ""), "jobPostings")
    if postings is None:
        raise ValueError(f"ashby: no jobPostings in appData for {token}")
    out = []
    for j in _dicts(postings):
        locs = [j.get("locationName")] + [
            s.get("locationName") for s in _dicts(j.get("secondaryLocations"))]
        url = f"https://jobs.ashbyhq.com/{quote(token)}/{j.get('id')}"
        out.append({
            "company": company,
            "title": j.get("title", ""),
            "location": _join_locations(locs),
            "url": url,
            "posted_at": None,
            "description": "",
            "detail_key": f"ashbypage||{url}",
            "source": "ashby",
        })
    return out


def fetch_ashby(company: str, token: str, etags: dict | None = None) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    try:
        data = _conditional_get_json(url, f"ashby:{token}", etags)
    except requests.HTTPError as e:
        # 404 only: the org disabled the posting API, not a transient
        # failure - 5xx keeps raising so retry/backoff semantics hold.
        if getattr(e.response, "status_code", None) == 404:
            return _fetch_ashby_hosted(company, token)
        raise
    out = []
    for j in _dicts(data.get("jobs")):
        primary = j.get("location") or ((j.get("address") or {})
            .get("postalAddress") or {}).get("addressLocality", "")
        out.append({
            "company": company,
            "title": j.get("title", ""),
            "location": _join_locations([primary] + [
                s.get("location") for s in _dicts(j.get("secondaryLocations"))]),
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "posted_at": _iso(j.get("publishedAt")),
            "description": _strip_html(j.get("descriptionHtml") or j.get("descriptionPlain")),
            "source": "ashby",
        })
    return out


def fetch_smartrecruiters(company: str, token: str) -> list[dict]:
    # Paged: a single limit=100 request silently truncated boards with more
    # postings (Arista's 226-posting board kept its one SWE internship on
    # page 2, invisible for as long as the board was polled). The cap bounds
    # a pathological board; none of ours comes near it.
    out = []
    for offset in range(0, 1000, 100):
        url = (f"https://api.smartrecruiters.com/v1/companies/{token}"
               f"/postings?limit=100&offset={offset}")
        r = get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        content = data.get("content") or []
        for j in _dicts(content):
            loc = j.get("location") or {}
            out.append({
                "company": company,
                "title": j.get("name", ""),
                "location": ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x),
                "url": f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}",
                "posted_at": _iso(j.get("releasedDate")),
                "description": "",  # filled by enrich() for prefilter survivors
                "detail_key": f"smartrecruiters|{token}|{j.get('id')}",
                "source": "smartrecruiters",
            })
        if offset + 100 >= data.get("totalFound", 0) or not content:
            break
    return out


_POSTED_AGO = re.compile(r"(\d+)\+?\s*days?\s*ago", re.I)


def _workday_posted(text: str | None) -> str | None:
    """Turn Workday's "Posted 3 Days Ago" blurb into an approximate ISO date.

    Workday exposes no real timestamp, only this relative string. Approximating
    it is worth doing because it is the only recency signal those boards give -
    but note "30+ Days Ago" is a floor, so old postings all collapse to the
    same date.
    """
    if not text:
        return None
    today = dt.datetime.now(dt.timezone.utc)
    low = text.lower()
    if "today" in low:
        days = 0
    elif "yesterday" in low:
        days = 1
    else:
        m = _POSTED_AGO.search(low)
        if not m:
            return None
        days = int(m.group(1))
    return (today - dt.timedelta(days=days)).date().isoformat()


def fetch_workable(company: str, token: str) -> list[dict]:
    """Workable's public board widget. No descriptions - title filter only."""
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}"
    r = get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in _dicts(r.json().get("jobs")):
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        out.append({
            "company": company,
            "title": j.get("title", ""),
            "location": "Remote" if j.get("telecommuting") and not loc else loc,
            "url": j.get("shortlink") or j.get("url", ""),
            "posted_at": _iso(j.get("published_on") or j.get("created_at")),
            "description": "",  # filled by enrich() for prefilter survivors
            "detail_key": (f"workable|{token}|{j['shortcode']}"
                           if j.get("shortcode") else None),
            "source": "workable",
        })
    return out


_INTERN_FACET_VAL = re.compile(
    r"\b(interns?(?:hips?)?|co-?op|students?|campus|university)\b", re.I)

# CXS facet parameters whose values can classify a posting as an internship,
# most specific first. Word boundaries keep jobFamilyGroup noise like
# "Internal Audit" or "International" from matching.
_WD_FACET_PRIORITY = ("workerSubType", "timeType", "jobType", "jobFamilyGroup")


def _workday_intern_facet(facets: list) -> dict:
    """Find the board's own intern/co-op classification, if it has one.

    Returns {facetParameter: [value ids]} for the highest-priority facet
    carrying an intern-flavored value (ids are tenant-specific GUIDs, which
    is why this is discovered per board), or {} when the tenant does not
    classify internships at all.
    """
    for param in _WD_FACET_PRIORITY:
        for f in _dicts(facets):
            if f.get("facetParameter") != param:
                continue
            ids = [v["id"] for v in _dicts(f.get("values"))
                   if v.get("id")
                   and _INTERN_FACET_VAL.search(v.get("descriptor") or "")]
            if ids:
                return {param: ids}
    return {}


def fetch_workday(company: str, token: str, host: str = "myworkdayjobs.com") -> list[dict]:
    """token format: 'tenant|wd_number|site'  e.g. 'nvidia|5|NVIDIAExternalCareerSite'"""
    tenant, wd, site = token.split("|")
    if host == "myworkdaysite.com":
        # Same CXS API, different domain layout: the tenant lives in the path.
        base = f"https://wd{wd}.myworkdaysite.com"
        job_prefix = f"{base}/recruiting/{tenant}/{site}"
    else:
        base = f"https://{tenant}.wd{wd}.myworkdayjobs.com"
        job_prefix = f"{base}/en-US/{site}"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    # DNS forbids underscores, so a tenant like "osv_cci" is forced to
    # "osv-cci" in the hostname - and tokens derived from posting URLs carry
    # that hyphenated form. The CXS path wants the real tenant name and
    # answers 422 to the hyphenated one, so on 422 retry once with
    # underscores before giving up. The hostname keeps its hyphens.
    api_fallback = None
    if "-" in tenant:
        api_fallback = f"{base}/wday/cxs/{tenant.replace('-', '_')}/{site}/jobs"
    def query(payload: dict) -> dict:
        nonlocal api, api_fallback
        r = post(api, json=payload,
                 headers={**UA, "Content-Type": "application/json",
                          "Accept": "application/json"},
                 timeout=TIMEOUT)
        if r.status_code == 422 and api_fallback:
            api, api_fallback = api_fallback, None
            r = post(api, json=payload,
                     headers={**UA, "Content-Type": "application/json",
                              "Accept": "application/json"},
                     timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    # Two text searches: a posting titled only "Software Engineering Co-op"
    # never matches "intern". "coop" (no hyphen) is deliberate - Workday's
    # tokenizer splits "co-op" into two terms and matches half the board.
    queries = [("intern", {}), ("coop", {})]
    # Boards that classify postings with an intern/co-op facet get an exact
    # query on top. Text search alone is not enough on a big tenant: it also
    # matches "internal"/"international" in descriptions, relevance ranking
    # puts those first, and the real internships land past the page cap
    # below - Palo Alto Networks (737 "intern" hits) polled healthy for
    # weeks while storing zero jobs. The facet query returns only true
    # internships, newest first, so the cap stops biting.
    facet = _workday_intern_facet(
        query({"appliedFacets": {}, "limit": 1, "offset": 0,
               "searchText": ""}).get("facets") or [])
    if facet:
        queries.insert(0, ("", facet))

    out, seen_paths = [], set()
    for search, applied in queries:
        offset = 0
        while offset < 200:  # cap: we only care about the recent pages
            data = query({"appliedFacets": applied, "limit": 20,
                          "offset": offset, "searchText": search})
            posts = _dicts(data.get("jobPostings"))
            if not posts:
                break
            for j in posts:
                path = j.get("externalPath", "")
                # No path means no per-job URL: skipping beats a phantom row
                # keyed to the board root (which would also dedupe every
                # later pathless entry against it).
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                out.append({
                    "company": company,
                    "title": j.get("title", ""),
                    "location": j.get("locationsText", ""),
                    "url": f"{job_prefix}{path}",
                    "posted_at": _workday_posted(j.get("postedOn")),
                    # Deliberately empty. This field used to carry postedOn,
                    # which meant every posting's "description" read "Posted
                    # 30+ Days Ago" - and the scorer duly marked good roles
                    # down as stale.
                    "description": "",
                    # The full CXS detail URL, derived from `api` at append
                    # time so a 422 tenant-form swap is already resolved.
                    "detail_key": f"workday|{tenant}|{api[:-len('/jobs')]}{path}",
                    "source": "workday",
                })
            offset += 20
            # Workday reports total=0 on every page after the first (it wants
            # session cookies we don't carry), so a plain offset>=total check
            # stops at 40 postings per board. Trust total only when nonzero;
            # a short page is the reliable end-of-results signal.
            total = data.get("total", 0)
            if len(posts) < 20 or (total and offset >= total):
                break
    return out


def fetch_workdaysite(company: str, token: str) -> list[dict]:
    """Workday's other domain (wdN.myworkdaysite.com). Same token format."""
    return fetch_workday(company, token, host="myworkdaysite.com")


def fetch_rippling(company: str, token: str) -> list[dict]:
    """Rippling ATS public board API. token = board slug from ats.rippling.com/<slug>."""
    url = f"https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs"
    r = get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError(f"rippling: expected a list, got {type(data).__name__}")
    out = []
    for j in _dicts(data):
        out.append({
            "company": company,
            "title": j.get("name", ""),
            "location": (j.get("workLocation") or {}).get("label", ""),
            "url": j.get("url", ""),
            "posted_at": None,  # enrich() backfills from the detail endpoint
            "description": (j.get("department") or {}).get("label", ""),
            "detail_key": f"rippling|{token}|{j.get('uuid')}",
            "source": "rippling",
        })
    return out


def fetch_avature(company: str, token: str) -> list[dict]:
    """Avature ATS portal (IBM, Bain, Two Sigma et al.). token = 'tenant|site-path'.

    e.g. 'ibmglobal|en_US/careers' polls
    https://ibmglobal.avature.net/en_US/careers/SearchJobs/intern, and a
    tenant with a dot in it is a full host: 'careers.bain.com|jobs' polls
    https://careers.bain.com/jobs/SearchJobs/intern (Bain's portal answers
    on its vanity domain and has no reachable *.avature.net alias).

    Quirks that make this adapter different from the JSON ones:
    - IBM's portal 301s every request to its vanity domain (careers.ibm.com),
      which sits behind a bot wall that serves an empty page. But the 301
      response itself carries the fully rendered HTML, so fetch with
      redirects disabled and read that body. Bain's serves a plain 200.
    - There is no JSON API, and the search is keyword-scoped to "intern" to
      keep the page count down.
    - Not every portal has a SearchJobs page. Two Sigma's lists roles under
      OpenRoles/ instead, and its SearchJobs answers a bodiless redirect to
      a 404. When the search page is missing - a 404, or a redirect that
      carries no page at all - the same keyword search runs against
      OpenRoles/<keyword>. A search page that exists but finds nothing is a
      full page with no cards, and stays an empty board.
    - Three card templates exist. IBM's "article--card" links
      JobDetail?jobId=<id> and pages with jobOffset; its detail pages are
      client-rendered, so those rows stay title-only (the scorer treats
      title-only as an explicit non-penalty). Bain's "article--result" links
      FolderDetail/<slug>/<id> and pages with folderOffset; its detail pages
      ARE server-rendered, so those rows get a detail_key and the enrich
      pass fills the description in. Two Sigma's "article--result" links
      JobDetail/<slug>/<id> with the location in a paragraph span, and its
      detail pages are server-rendered the same way as Bain's. Each portal
      ignores the other's offset parameter, so both are sent.
    """
    tenant, site = token.split("|", 1)
    host = tenant if "." in tenant else f"{tenant}.avature.net"
    base = f"https://{host}/{site}"
    rows = _avature_walk(company, f"{base}/SearchJobs/intern")
    if rows is None:
        rows = _avature_walk(company, f"{base}/OpenRoles/intern")
    if rows is None:
        # Neither listing exists: a dead portal, and it must read as one
        # (a 404, which board health retires) rather than as an empty board.
        raise requests.HTTPError(
            f"404 Client Error: no SearchJobs or OpenRoles listing at {base}")
    return rows


def _avature_walk(company: str, listing: str) -> list[dict] | None:
    """Page through one Avature listing URL. None means the listing itself
    is missing (404, or a redirect with no page behind it), so the caller
    can try the portal's other listing; a 4xx/5xx on any other page raises."""
    out, seen = [], set()
    offset = 0
    for page in range(40):  # cap: 40 pages is far beyond any real intern search
        r = get(f"{listing}?jobOffset={offset}&folderOffset={offset}",
                headers=UA, timeout=TIMEOUT, allow_redirects=False)
        if page == 0 and (r.status_code == 404
                          or (300 <= r.status_code < 400 and not (r.text or "").strip())):
            return None
        if r.status_code >= 400:
            r.raise_for_status()
        cards = re.split(r'<article class="article article--(?:card|result)', r.text)[1:]
        new = 0
        for art in cards:
            m = re.search(
                r'__text__title[^>]*>\s*<a[^>]*href="([^"]*?(?:jobId=(\d+)'
                r'|(?:Folder|Job)Detail/[^"?]*?/(\d+))[^"]*)"[^>]*>\s*(.*?)\s*</a>',
                art, re.S)
            if not m:
                continue
            jid = m.group(2) or m.group(3)
            if jid in seen:
                continue
            seen.add(jid)
            new += 1
            loc = (re.search(r'card-item-location">\s*([^<]*?)\s*<', art)
                   or re.search(r'__text__subtitle">\s*<span>\s*([^<]*?)\s*<', art)
                   or re.search(r'paragraph_inner-span">\s*([^<]*?)\s*<', art))
            url = m.group(1)
            out.append({
                "company": company,
                "title": _strip_html(m.group(4)),
                "location": _strip_html(loc.group(1)) if loc else "",
                "url": url,
                "posted_at": None,  # the portal exposes no dates at all
                "description": "",
                "source": "avature",
                # Path-form detail links (FolderDetail/<slug>/<id>,
                # JobDetail/<slug>/<id>) are server-rendered; the query
                # form (JobDetail?jobId=) is IBM's client-rendered page.
                "detail_key": f"avature||{url}" if m.group(3) else None,
            })
        if not cards or not new:
            break
        offset += len(cards)
    return out


_GEM_GRAPHQL = "https://jobs.gem.com/api/public/graphql"
_GEM_LIST_QUERY = """query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      extId title firstPublishedTsSec
      locations { name isRemote }
    }
  }
}"""


def fetch_gem(company: str, token: str) -> list[dict]:
    """Gem ATS public job board. token = vanity slug from jobs.gem.com/<slug>.

    The board pages are client-rendered (a scraper sees an empty shell), but
    they feed off an unauthenticated GraphQL endpoint, so poll that directly.
    The list response is complete and unpaginated.
    """
    r = post(
        _GEM_GRAPHQL,
        json={"variables": {"boardId": token}, "query": _GEM_LIST_QUERY},
        headers={**UA, "Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"gem graphql error: {payload['errors'][0].get('message', '?')[:80]}")
    posts = ((payload.get("data") or {}).get("oatsExternalJobPostings")
             or {}).get("jobPostings") or []
    out = []
    for j in _dicts(posts):
        locs = _dicts(j.get("locations"))
        names = [x.get("name") for x in locs if x.get("name")]
        if any(x.get("isRemote") for x in locs):
            names.append("Remote")
        out.append({
            "company": company,
            "title": j.get("title", ""),
            "location": "; ".join(dict.fromkeys(names)),
            "url": f"https://jobs.gem.com/{token}/{j.get('extId')}",
            "posted_at": _iso(j.get("firstPublishedTsSec")),
            "description": "",
            "detail_key": f"gem|{token}|{j.get('extId')}",
            "source": "gem",
        })
    return out


def fetch_simplify(company: str, token: str, etags: dict | None = None) -> list[dict]:
    """The SimplifyJobs listings.json that generates their README repos.

    token = raw.githubusercontent.com URL of the file. This is a safety net for
    boards we cannot poll directly (Meta, Amazon, TikTok, iCIMS, Oracle...): we
    will never beat Simplify's scraper on those hosts, but ingesting their feed
    means we are at most one poll behind it. Listings whose apply URL points at
    a board we already poll directly are dropped in the CLI (derive_board), so
    the direct - faster - fetch wins and near-duplicate formatting from two
    sources never reaches the digest.
    """
    data = _conditional_get_json(token, f"simplify:{token}", etags, timeout=60)
    if not isinstance(data, list):
        raise ValueError(f"simplify: expected a list, got {type(data).__name__}")
    out = []
    for x in _dicts(data):
        if not x.get("active") or not x.get("is_visible", True):
            continue
        terms = [str(t) for t in (x.get("terms") or []) if t]
        if not terms and x.get("season"):
            # Some forks (vanshb03) fill `season` ("Summer") instead of
            # `terms`. No year, but it still places the row in the right
            # season section.
            terms = [str(x["season"])]
        # Simplify's own rows carry an id that is also their posting page's
        # id, so enrich() can pull the description the feed omits. Forks
        # (vanshb03) mint their own ids, which resolve to nothing - the
        # source check keeps their rows on the old description-less path.
        detail = (f"simplify||{x['id']}"
                  if x.get("source") == "Simplify" and x.get("id") else None)
        out.append({
            "company": x.get("company_name", company),
            "title": x.get("title", ""),
            "location": "; ".join(x.get("locations") or []),
            "url": x.get("url", ""),
            "detail_key": detail,
            "posted_at": _iso(x.get("date_posted")),
            # date_updated is bumped when Simplify reactivates a listing, so
            # a reopened posting keeps an old date_posted. The stale gate
            # reads this so reactivated roles count as fresh, while
            # posted_at stays the honest posting date for display.
            "updated_at": _iso(x.get("date_updated")),
            # Kept structured as well as prose: `terms` is the only place most
            # of these listings state their cycle, and it is far more reliable
            # than guessing from the title. The prefilter reads it directly.
            "terms": terms,
            "description": f"Terms: {', '.join(terms)}" if terms else "",
            "source": "simplify",
        })
    return out


# --------------------------------------------------------------------------
# Board derivation: apply URL -> (ats, token) when the host is one we can
# poll directly. Used to expand coverage from external lists and to drop
# simplify-sourced listings that a direct board already covers.
# --------------------------------------------------------------------------

_WD_JOBS_PAT = re.compile(r"^([^.]+)\.wd(\d+)\.myworkdayjobs\.com$")
# Path segments like "en-US" or "en-gb" that prefix some ATS posting URLs.
_LOCALE_PAT = re.compile(r"^[a-z]{2}-[a-z]{2}$", re.I)
_WD_SITE_PAT = re.compile(r"^wd(\d+)\.myworkdaysite\.com$")


def derive_board(url: str) -> tuple[str, str] | None:
    """Best-effort mapping of a posting URL to a directly pollable board."""
    from urllib.parse import unquote, urlparse
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = u.netloc.lower().removeprefix("www.")
    parts = [p for p in u.path.split("/") if p]

    if host.endswith("greenhouse.io"):
        # boards.greenhouse.io/<token>/... or job-boards.greenhouse.io/<token>/...
        if parts and parts[0] not in ("embed", "jobs"):
            return ("greenhouse", parts[0])
        # Embed URLs carry no token in the path, but the aggregator feeds'
        # /embed/job_app links often name the board in the query:
        # ...embed/job_app?for=<token>&token=<job id>. Without this, such a
        # listing never maps to its directly-polled board and escapes
        # drop_covered_simplify.
        for_param = parse_qs(u.query).get("for")
        if for_param and for_param[0]:
            return ("greenhouse", for_param[0])
        return None
    if host == "jobs.ashbyhq.com" and parts:
        return ("ashby", unquote(parts[0]))
    if host == "jobs.lever.co" and parts:
        return ("lever", parts[0])
    if host == "jobs.eu.lever.co" and parts:
        # EU boards live on a separate API host; the prefix tells fetch_lever.
        return ("lever", f"eu:{parts[0]}")
    if host == "jobs.smartrecruiters.com" and parts:
        return ("smartrecruiters", parts[0])
    if host == "apply.workable.com" and parts:
        # /j/<code> is Workable's shortlink namespace, not an account slug.
        # Deriving it would invent a bogus "j" board - and worse, closure's
        # display-name fallback only runs for UNderivable URLs, so shortlink
        # rows must return None here or they can never be closed.
        if parts[0] == "j":
            return None
        return ("workable", parts[0])
    if host == "ats.rippling.com" and parts:
        # Locale-prefixed URLs (ats.rippling.com/en-GB/<slug>/jobs/...) are
        # what the aggregator lists carry; the slug is the next segment.
        # Taking parts[0] blindly collapsed every such company into one
        # bogus "en-GB" board.
        idx = 1 if _LOCALE_PAT.match(parts[0]) else 0
        if len(parts) > idx:
            return ("rippling", parts[idx])
        return None
    if host == "jobs.gem.com" and parts:
        return ("gem", parts[0])
    if host == "ycombinator.com" and len(parts) >= 2 and parts[0] == "companies":
        return ("yc", parts[1])
    if host in _ATLASSIAN_PORTALS:
        return ("atlassian", _ATLASSIAN_TOKEN)
    if host.endswith(".icims.com"):
        return ("icims", host[:-len(".icims.com")])
    if host.endswith(".avature.net"):
        # <tenant>.avature.net/<site...>/JobDetail/<slug>/<id> (or
        # JobDetail?jobId=, or FolderDetail/...): the site path is whatever
        # sits before the detail segment. Vanity-domain portals
        # (careers.bain.com) stay underivable - nothing in the URL names
        # the host the config polls.
        for i, p in enumerate(parts):
            if p.split("?")[0] in ("JobDetail", "FolderDetail") and i:
                return ("avature", f"{host[:-len('.avature.net')]}|{'/'.join(parts[:i])}")
        return None
    m = _WD_JOBS_PAT.match(host)
    if m and parts:
        # Workday writes the locale segment in either case (en-US, en-us).
        site_idx = 1 if _LOCALE_PAT.match(parts[0]) else 0
        # A posting URL is /<locale>?/<site>/job/<location>/<slug>_<id>, so
        # the site is whatever sits before "job" - even when it is literally
        # named "jobs" (paypal.wd1.myworkdayjobs.com/en-US/jobs/job/...).
        # Rejecting that segment blindly left PayPal's rows underivable, so
        # they could neither close nor cover the feed's copies (2026-09-04).
        if "job" in parts[site_idx + 1:]:
            site_idx = parts.index("job", site_idx + 1) - 1
            if site_idx >= 0 and not _LOCALE_PAT.match(parts[site_idx]):
                return ("workday", f"{m.group(1)}|{m.group(2)}|{parts[site_idx]}")
            return None
        if len(parts) > site_idx and parts[site_idx] not in ("job", "jobs"):
            return ("workday", f"{m.group(1)}|{m.group(2)}|{parts[site_idx]}")
        return None
    m = _WD_SITE_PAT.match(host)
    if m and len(parts) >= 3 and parts[0] == "recruiting":
        return ("workdaysite", f"{parts[1]}|{m.group(1)}|{parts[2]}")
    # Big-tech sites with their own adapters (2026-09-28). Tokens must equal
    # the config.yaml entries: that equality is what lets a feed row map to
    # its directly-polled board for coverage, and a stored row for closure.
    if host.endswith(".oraclecloud.com") and "sites" in parts:
        i = parts.index("sites")
        if len(parts) > i + 1:
            return ("oraclecloud", f"{host}|{parts[i + 1]}")
    if parse_qs(u.query).get("icims") and len(parts) >= 2 and parts[0] == "jobs":
        return ("jibe", host)
    if host == "jobs.apple.com":
        return ("apple", "internships-STDNT-INTRN")
    if host == "lifeattiktok.com":
        return ("bytedance", "tiktok")
    if host in ("jobs.bytedance.com", "joinbytedance.com"):
        return ("bytedance", "bytedance")
    if host == "higher.gs.com":
        return ("goldman", "students")
    if host == "metacareers.com":
        return ("meta", "intern")
    if host == "tesla.com" and parts[:3] == ["careers", "search", "job"]:
        return ("tesla", "US")
    if host in ("citadel.com", "citadelsecurities.com"):
        return ("citadel", host)
    domain = _EIGHTFOLD_DOMAINS.get(host)
    if domain and parts[:2] == ["careers", "job"]:
        return ("eightfold", f"{host}|{domain}|intern")
    # SuccessFactors (jobs2web) posting URLs: /<region>?/job/<slug>/<id>/ on
    # the tenant's own host, which is also the token fetch_sapsf polls. The
    # shape is checked last so hosts with their own adapters never reach it.
    if _SF_URL_PAT.match(u.path) and host:
        return ("sapsf", host)
    return None


# Eightfold hosts polled directly, host -> tenant domain (the token's middle
# field). A posting URL names only the host, so derive_board needs the map.
_EIGHTFOLD_DOMAINS = {
    "apply.careers.microsoft.com": "microsoft.com",
    "careers.qualcomm.com": "qualcomm.com",
    "explore.jobs.netflix.net": "netflix.com",
    "bostonscientific.eightfold.ai": "bostonscientific.com",
}


FIRECRAWL_PROMPT = """Extract every individual open job posting listed on this \
page.

Rules:
- One entry per actual job opening. Do NOT return category links, department \
headings, filter options, or navigation items such as "Students and New Grads" \
or "Experienced Candidates" - those are not jobs.
- Keep the full posting title verbatim, including any term or year it names \
(e.g. "Software Engineering Internship - Summer 2027").
- `url` must be the direct link to that specific posting, not the listing page.
- If the page shows no individual postings, return an empty list."""


def fetch_firecrawl(company: str, token: str) -> list[dict]:
    """Fallback for custom career pages with no public JSON. token = page URL.

    These boards are client-rendered, so the two settings that matter are
    `waitFor` (let the job list paint) and `onlyMainContent: False` (listings
    routinely sit outside <main>, and cutting to main content silently returns
    an empty page).
    """
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        raise RuntimeError("FIRECRAWL_API_KEY not set")
    r = post(
        "https://api.firecrawl.dev/v2/scrape",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "url": token,
            "formats": [{
                "type": "json",
                "prompt": FIRECRAWL_PROMPT,
                "schema": {
                    "type": "object",
                    "properties": {
                        "jobs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "location": {"type": "string"},
                                    "url": {"type": "string"},
                                },
                                "required": ["title"],
                            },
                        }
                    },
                },
            }],
            "onlyMainContent": False,
            "waitFor": 8000,
            "maxAge": 0,
        },
        timeout=180,
    )
    r.raise_for_status()
    jobs = _dicts(((r.json().get("data") or {}).get("json") or {}).get("jobs"))
    return [{
        "company": company,
        "title": j.get("title", ""),
        "location": j.get("location", ""),
        "url": j.get("url") or token,
        "posted_at": None,
        "description": "",
        "source": "firecrawl",
    } for j in jobs]


_BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                             "Gecko/20100101 Firefox/128.0"}
_GOOGLE_JOB_PAT = re.compile(r"jobs/results/(\d+)-([a-z0-9-]+)")


def fetch_google(company: str, token: str) -> list[dict]:
    """Google Careers. token = search query, e.g. 'intern'.

    Google killed its public careers API, but the search page is server
    rendered, so the job links sit in the raw HTML. Titles are rebuilt from
    the URL slug - crude but exactly enough for the title prefilter, and
    Google intern postings close within days, so polling this hourly matters
    more than polish. Needs a browser UA; the jobcrawl one gets a shell page.
    """
    from urllib.parse import quote
    base = "https://www.google.com/about/careers/applications/jobs/results"
    seen, out = set(), []
    for page in range(1, 6):
        r = get(
            f"{base}?q=%22{quote(token)}%22&target_level=INTERN_AND_APPRENTICESHIP"
            f"&page={page}",
            headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        found = [(i, s) for i, s in dict.fromkeys(_GOOGLE_JOB_PAT.findall(r.text))
                 if i not in seen]
        if not found:
            break
        for jid, slug in found:
            seen.add(jid)
            url = f"{base}/{jid}-{slug}"
            out.append({
                "company": company,
                "title": slug.replace("-", " ").title(),
                "location": "",
                "url": url,
                "posted_at": None,
                "description": "",
                # The search page carries no location or description, so the
                # US gate and the clearance gate had nothing to inspect and a
                # Canada-only SWE intern posting reached the digest as an 85
                # (2026-08-31). The job page is server rendered and free;
                # enrich fills both in before scoring.
                "detail_key": f"google||{url}",
                "source": "google",
            })
    return out


def _amazon_posted(text: str | None) -> str | None:
    """amazon.jobs "September 15, 2025" -> ISO date, or None."""
    if not text:
        return None
    try:
        return dt.datetime.strptime(text.strip(), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def fetch_amazon(company: str, token: str) -> list[dict]:
    """amazon.jobs public search API. token = search query, e.g. 'intern'.

    Amazon runs its own ATS, but the careers site's search.json endpoint is
    plain unauthenticated JSON with real posted dates and inline short
    descriptions - no detail calls needed. The query is US-scoped server-side
    (normalized_country_code) because the raw search is global and Amazon's
    location strings for foreign offices ("IT, NO, Agognate") are not ones
    the prefilter's US gate can read. Needs a browser UA.
    """
    base = "https://www.amazon.jobs/en/search.json"
    out, seen = [], set()
    offset = 0
    for _ in range(10):  # 100/page; 1000 rows is far beyond any intern search
        r = get(base, params={
            "base_query": token,
            "normalized_country_code[]": "USA",
            "result_limit": 100,
            "offset": offset,
        }, headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        jobs = _dicts(data.get("jobs"))
        for j in jobs:
            path = j.get("job_path") or ""
            jid = str(j.get("id_icims") or path)
            if not path or jid in seen:
                continue
            seen.add(jid)
            desc = " ".join(x for x in (j.get("description_short"),
                                        j.get("basic_qualifications")) if x)
            out.append({
                "company": company,
                "title": j.get("title") or "",
                "location": j.get("normalized_location") or j.get("location") or "",
                "url": f"https://www.amazon.jobs{path}",
                "posted_at": _amazon_posted(j.get("posted_date")),
                "description": _strip_html(desc),
                "source": "amazon",
            })
        if len(jobs) < 100:
            break
        offset += 100
    return out


def _eightfold_epoch(value: Any) -> str | None:
    """pcsx timestamps arrive as epoch-second strings ("1787861513")."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return _iso(value)


def _eightfold_pcsx(company: str, host: str, domain: str, query: str) -> list[dict]:
    """Eightfold's newer search API, for tenants that refuse /api/apply/v2.

    Some shared-domain tenants answer the v2 list with 403 "Not authorized
    for PCSX" (Boston Scientific, 2026-09-23): their portal runs on
    /api/pcsx/search, which pages at 10 rows per call whatever `num` says
    and reports the total in data.count. Rows carry the id, the title,
    standardized locations ("Arden Hills, MN, US"), epoch-second strings
    and a relative positionUrl; no description.
    """
    out, seen = [], set()
    start, total = 0, None
    for _ in range(30):  # 10/page, so 300 rows - the query is "intern"
        r = get(f"https://{host}/api/pcsx/search", params={
            "domain": domain, "query": query, "num": 10, "start": start,
        }, headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        positions = _dicts(data.get("positions"))
        if total is None:
            total = data.get("count") or 0
        for p in positions:
            pid = str(p.get("id") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            raw = [l for l in (p.get("locations") or []) if isinstance(l, str)]
            std = [l for l in (p.get("standardizedLocations") or [])
                   if isinstance(l, str)]
            # A country-wide posting standardizes to the bare code ("IN");
            # the raw form ("India") is the one the location gate can read.
            locs = [s if "," in s or i >= len(raw) else raw[i]
                    for i, s in enumerate(std)] or raw
            locs = [l for l in locs if l]
            path = p.get("positionUrl") or f"/careers/job/{pid}"
            out.append({
                "company": company,
                "title": p.get("name") or "",
                "location": "; ".join(l.replace(",", ", ").replace(",  ", ", ")
                                      for l in locs[:8]),
                "url": f"https://{host}{path}",
                "posted_at": _eightfold_epoch(p.get("postedTs")
                                              or p.get("creationTs")),
                "description": "",
                "source": "eightfold",
                "detail_key": f"eightfold|{host}|{domain}|{pid}",
            })
        start += len(positions)
        if not positions or start >= total:
            break
    return out


def fetch_eightfold(company: str, token: str) -> list[dict]:
    """Eightfold ATS (Netflix et al.). token = 'host|domain[|query]'.

    e.g. 'explore.jobs.netflix.net|netflix.com|intern' polls
    https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com.
    The list response carries no descriptions; t_create is the posting's
    epoch, which _iso already reads. Tenants on the shared eightfold.ai
    domain often sit behind a bot wall (Qualcomm 403s from EC2, 2026-08-27);
    self-hosted domains like Netflix's answer plainly. A 403 whose body says
    "Not authorized for PCSX" is not the bot wall but a tenant that only
    serves the newer pcsx search API - _eightfold_pcsx takes over.

    The v2 list answers at most 10 positions per call whatever `num` asks
    for (Netflix, 2026-09-25: num=100 returned 10 of count 192), so paging
    advances by the rows actually returned and stops at data.count, on an
    empty page, or on a page with no new ids (a tenant that ignored `start`
    would otherwise spin to the cap). The loop once asked for 100 and quit
    on any shorter page, which cut every tenant to its first 10 matches.
    """
    host, domain, *rest = token.split("|")
    query = rest[0] if rest else "intern"
    out, seen = [], set()
    start = 0
    for _ in range(30):  # 10/page, so 300 rows - the query is "intern"
        r = get(f"https://{host}/api/apply/v2/jobs", params={
            "domain": domain, "query": query, "num": 10, "start": start,
        }, headers=_BROWSER_UA, timeout=TIMEOUT)
        if r.status_code == 403 and "PCSX" in (r.text or "") and start == 0:
            return _eightfold_pcsx(company, host, domain, query)
        r.raise_for_status()
        data = r.json() or {}
        positions = _dicts(data.get("positions"))
        ids = {str(p.get("id")) for p in positions if p.get("id")}
        repeat = bool(ids) and ids <= seen
        for p in positions:
            pid = str(p.get("id") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            locs = [l for l in (p.get("locations") or [p.get("location")])
                    if isinstance(l, str) and l]
            out.append({
                "company": company,
                "title": p.get("name") or "",
                "location": "; ".join(l.replace(",", ", ") for l in locs[:8]),
                "url": p.get("canonicalPositionUrl")
                       or f"https://{host}/careers/job/{pid}",
                "posted_at": _iso(p.get("t_create")),
                "description": "",
                "source": "eightfold",
                "detail_key": f"eightfold|{host}|{domain}|{pid}",
            })
        start += len(positions)
        total = data.get("count")
        if (not positions or repeat
                or (isinstance(total, int) and start >= total)):
            break
    return out


_ICIMS_CARD = re.compile(
    r'<li class="iCIMS_JobCardItem".*?(?=<li class="iCIMS_JobCardItem"|</ul>)', re.S)


def _icims_posted(title_attr: str) -> str | None:
    """iCIMS "Posted Date" title attribute ("8/24/2026 3:26 PM") -> ISO date.

    No timezone is stated, so only the date is kept - a day of slack beats
    inventing a timestamp in the wrong zone.
    """
    try:
        return dt.datetime.strptime(title_attr.strip(),
                                    "%m/%d/%Y %I:%M %p").date().isoformat()
    except ValueError:
        return None


def fetch_icims(company: str, token: str) -> list[dict]:
    """iCIMS hosted careers site. token = subdomain, e.g. 'careers-sig'.

    iCIMS has no public JSON, but its search page is plain server-rendered
    HTML with one <li> per job and the description inline - so unlike most
    scraped boards these postings arrive already enriched. Two keyword passes
    ("intern", "co-op") because a co-op title never matches "intern", 20 rows
    a page via &pr=N. Needs a browser UA.
    """
    base = f"https://{token}.icims.com/jobs/search"
    out, seen = [], set()
    for keyword in ("intern", "co-op"):
        for pr in range(6):  # cap: 120 results per keyword is plenty
            r = get(
                base,
                params={"ss": 1, "searchKeyword": keyword, "in_iframe": 1, "pr": pr},
                headers=_BROWSER_UA, timeout=TIMEOUT)
            r.raise_for_status()
            fresh = 0
            for card in _ICIMS_CARD.findall(r.text):
                m = re.search(r'href="([^"]*?/jobs/(\d+)/[^"]*?)"', card)
                if not m or m.group(2) in seen:
                    continue
                seen.add(m.group(2))
                fresh += 1
                title = re.search(r"<h3[^>]*>(.*?)</h3>", card, re.S)
                desc = re.search(
                    r'class="col-xs-12 description"[^>]*>(.*?)</div>', card, re.S)
                # Tenant-dependent card fields. Some tenants render a "Job
                # Locations" value ("CA-ON-Greater Toronto Area") - without
                # parsing it, every posting on a foreign-only board sailed
                # past us_only on an empty location (Mackenzie Investments,
                # 2026-08-24). Same for "Posted Date", whose title attribute
                # carries a real timestamp. Both stay empty when a tenant
                # omits them: fail open, exactly like an empty location
                # anywhere else.
                loc = re.search(
                    r'Job Locations</span>\s*<span[^>]*>\s*(.*?)\s*</span>',
                    card, re.S)
                posted = re.search(
                    r'Posted Date</span>\s*<span title="([^"]+)"', card)
                url = m.group(1).split("?")[0]
                location = _strip_html(loc.group(1)) if loc else ""
                description = _strip_html(desc.group(1)) if desc else ""
                out.append({
                    "company": company,
                    "title": _strip_html(title.group(1)) if title else "",
                    "location": location,
                    "url": url,
                    "posted_at": _icims_posted(posted.group(1)) if posted else None,
                    "description": description,
                    "source": "icims",
                    # A tenant whose cards omit the location or the text
                    # (Kinaxis renders neither) gets both from the job page,
                    # whose JSON-LD names the city and country. Without it an
                    # Ottawa-only posting reached the digest title-only with
                    # an empty location, and both gates fail open on empty
                    # (2026-09-09).
                    "detail_key": (None if location and description
                                   else f"icims|{token}|{url}"),
                })
            if not fresh:
                break
    return out


_ICIMS_LD = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def _parse_icims_detail(html: str) -> tuple[str, str | None, str | None]:
    """(description, posted_at, location) from an iCIMS job page.

    The page embeds a schema.org JobPosting as JSON-LD with the description,
    datePosted and a jobLocation address list - the one place every tenant
    states the city and country, whatever its card template shows. When the
    block is missing or malformed, the <meta name="description"> line
    ("<Org> is now hiring a <title> in Ottawa, Ontario.") gives the location
    and the iCIMS_Expandable_Text blocks give the body.
    """
    desc, posted, loc = "", None, None
    for block in _ICIMS_LD.findall(html or ""):
        try:
            d = json.loads(block)
        except ValueError:
            continue
        items = d if isinstance(d, list) else [d]
        for item in _dicts(items):
            if item.get("@type") != "JobPosting":
                continue
            desc = _strip_html(item.get("description") or "")
            posted = _iso(item.get("datePosted"))
            places = item.get("jobLocation") or []
            if isinstance(places, dict):
                places = [places]
            parts = []
            for pl in _dicts(places):
                a = pl.get("address") or {}
                if not isinstance(a, dict):
                    continue
                # iCIMS writes "UNAVAILABLE" into fields a tenant left blank.
                seg = ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"),
                                            a.get("addressCountry"))
                                if x and str(x).upper() != "UNAVAILABLE")
                if seg:
                    parts.append(seg)
            loc = "; ".join(parts) or None
            break
        if desc or loc:
            break
    if not loc:
        m = re.search(r'<meta name="description" content="[^"]*? in ([^".]+?)\.', html or "")
        if m:
            loc = m.group(1).strip()
    if not desc:
        blocks = re.findall(r'class="iCIMS_Expandable_Text"[^>]*>(.*?)</div>', html or "", re.S)
        desc = _strip_html("\n".join(blocks)).strip()
    return desc, posted, loc


_SNDSH_ROW = re.compile(
    r"^\| ([^|]+) \| ([^|]+) \| ([^|]+) \| \[apply\]\((\S+?)\)[^|]*\|([^|]*)\|?",
    re.M)
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️]")


def fetch_sndsh404(company: str, token: str) -> list[dict]:
    """The hand-maintained summer-2027-internships markdown list.

    token = raw README URL. A human curates exactly the postings the ATS
    sweep can't see (Google, Uber, Capital One...), so their rows come in as
    listings, not just board hints. The Added column becomes posted_at, which
    lets the stale gate age-filter them. 🔒-marked rows are closed and skipped.
    """
    r = get(token, headers=UA, timeout=60)
    r.raise_for_status()
    out = []
    for m in _SNDSH_ROW.finditer(r.text):
        co, role, loc, url, added = (g.strip() for g in m.groups())
        if co.lower() == "company" or "🔒" in co or "🔒" in role:
            continue
        added = added.strip()
        out.append({
            "company": _EMOJI.sub("", co).strip(),
            "title": _EMOJI.sub("", role).strip(),
            "location": loc,
            "url": url,
            "posted_at": added if re.match(r"^\d{4}-\d{2}-\d{2}$", added) else None,
            "description": "",
            "source": "sndsh404",
        })
    return out


# WaaS company pages (ycombinator.com/companies/<slug>/jobs) are
# Inertia-rendered: the full props JSON sits HTML-escaped in the root div's
# data-page attribute, on the job list and on each job page alike.
_YC_DATA_PAGE = re.compile(r'data-page="([^"]*)"')
_YC_AGO = re.compile(r"(\d+)\+?\s*(minute|hour|day|week|month|year)s?", re.I)


def _yc_page(path: str) -> dict:
    r = get(f"https://www.ycombinator.com{path}", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    m = _YC_DATA_PAGE.search(r.text or "")
    if not m:
        raise ValueError(f"yc: no data-page payload at {path}")
    return json.loads(html.unescape(m.group(1)))


def _yc_posted(text: str | None) -> str | None:
    """WaaS shows only relative age ("4 days", "about 1 month").

    Like workday's blurb this is the board's only recency signal; month/year
    values are coarse floors, and an unparseable string yields None rather
    than a fabricated date.
    """
    if not text:
        return None
    low = text.lower()
    if "today" in low or "just now" in low:
        days = 0
    elif "yesterday" in low:
        days = 1
    else:
        m = _YC_AGO.search(low)
        if not m:
            return None
        per = {"minute": 0, "hour": 0, "day": 1, "week": 7,
               "month": 30, "year": 365}
        days = int(m.group(1)) * per[m.group(2).lower()]
    now = dt.datetime.now(dt.timezone.utc)
    return (now - dt.timedelta(days=days)).date().isoformat()


_YC_ALGOLIA_OPTS = re.compile(r"window\.AlgoliaOpts\s*=\s*(\{.*?\});")


def yc_algolia_opts() -> tuple[str, str]:
    """The YC directory's public Algolia app id and search key.

    Re-read from the /companies page every sweep, like the a16z server-action
    id - the key is a generated secured key and rotates without notice.
    """
    r = get("https://www.ycombinator.com/companies", headers=_BROWSER_UA,
            timeout=TIMEOUT)
    r.raise_for_status()
    m = _YC_ALGOLIA_OPTS.search(r.text or "")
    if not m:
        raise ValueError("yc: no AlgoliaOpts on /companies")
    d = json.loads(m.group(1))
    return d["app"], d["key"]


def yc_hiring_companies() -> list[dict]:
    """[{name, slug}] for every YC company flagged isHiring in the public
    directory index.

    Algolia caps plain pagination at 1,000 hits and there are ~1,500 hiring
    companies, so the query is partitioned by the `batch` facet (40 values,
    ~100 companies each). The facet counts double as a completeness check:
    silently missing a partition would be the rotating-sample trap the /jobs
    directory itself has.
    """
    app, key = yc_algolia_opts()
    url = f"https://{app}-dsn.algolia.net/1/indexes/YCCompany_production/query"
    headers = {"X-Algolia-Application-Id": app, "X-Algolia-API-Key": key,
               "Content-Type": "application/json"}

    def query(body):
        r = post(url, headers=headers,
                 json={"query": "", "filters": "isHiring:true", **body},
                 timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    facets = (query({"hitsPerPage": 0, "facets": ["batch"]})
              .get("facets") or {}).get("batch") or {}
    if not facets:
        raise ValueError("yc: batch facet came back empty")
    out, seen = [], set()
    for batch in facets:
        d = query({"filters": f'isHiring:true AND batch:"{batch}"',
                   "hitsPerPage": 1000,
                   "attributesToRetrieve": ["name", "slug"]})
        for h in d.get("hits", []):
            slug = h.get("slug")
            if slug and slug not in seen:
                seen.add(slug)
                out.append({"name": h.get("name") or slug, "slug": slug})
    expected = sum(facets.values())
    if len(out) < expected * 0.9:
        raise ValueError(
            f"yc: sweep incomplete - {len(out)} companies vs {expected} in facets")
    return out


def fetch_yc(company: str, token: str) -> list[dict]:
    """YC's Work at a Startup listing for one company.

    Complete and unpaginated, so the source is closable. The list payload
    carries no description; each job page's own data-page does, so rows get
    a detail_key and the enrich pass fills the text in. Job page URLs embed
    short non-numeric ids ("WfW1xjj-..."), below url_key's digit-run rule,
    so they keep exact-string identity - stable as long as nobody rewrites
    them between runs.
    """
    props = _yc_page(f"/companies/{token}/jobs").get("props") or {}
    out = []
    for j in _dicts(props.get("jobPostings")):
        path = j.get("url") or ""
        out.append({
            "company": company,
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": f"https://www.ycombinator.com{path}",
            "posted_at": _yc_posted(j.get("createdAt")),
            "description": "",
            "detail_key": f"yc|{token}|{path}",
            "source": "yc",
        })
    return out


# --------------------------------------------------------------------------
# Per-posting enrichment: full descriptions for the boards whose list APIs
# return none. Only prefilter survivors get here, so this is a handful of
# requests per run buying the scorer real text instead of a bare title.
# --------------------------------------------------------------------------

_ATLASSIAN_LISTINGS = "https://www.atlassian.com/endpoint/careers/listings"
# Atlassian posts through several iCIMS portals underneath its own careers
# site. Their search pages are shells that redirect to atlassian.com, so
# fetch_icims sees nothing there (careers-americas sat at empty_streak 50
# while the feeds listed its 2027 SWE intern, 2026-09-02). derive_board
# maps these hosts to the atlassian adapter so closure and the aggregator
# overlap drop attribute the rows to the board that actually fetches them.
_ATLASSIAN_PORTALS = {
    "careers-americas.icims.com",
    "careers-apac-atlassian.icims.com",
    "globalcareers-atlassian.icims.com",
    "campus-globalcareers-atlassian.icims.com",
}
_ATLASSIAN_TOKEN = "listings"


def _atlassian_loc(value: str) -> str:
    """'San Francisco - United States -   San Francisco, California 94104 United States'
    -> 'San Francisco, California United States'. The last ' - ' segment is
    the full address; the zip code adds nothing the US gate needs."""
    seg = value.split(" - ")[-1]
    seg = re.sub(r"\b\d[\d-]*\b", " ", seg)
    return re.sub(r"\s+", " ", seg).strip(" ,")


def _atlassian_date(value: str | None) -> str | None:
    """'2026-08-18 08:11 AM' -> '2026-08-18'. Day granularity is all the
    tracker shows, and the endpoint gives no timezone."""
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d %I:%M %p").date().isoformat()
    except ValueError:
        return None


def fetch_atlassian(company: str, token: str, etags: dict | None = None) -> list[dict]:
    """Atlassian's own careers site. The token is nominal ('listings').

    atlassian.com/company/careers/all-jobs is fed by an unauthenticated JSON
    endpoint holding every open requisition (~250) with the description
    sections inline, and it honors If-None-Match. Each row's url is the
    iCIMS portal job page, the same URL the aggregator feeds carry, so the
    two identities collide the way dedupe expects. The only date is
    `updatedDate`, which iCIMS bumps on edits; it is the honest floor we
    have, so it becomes posted_at.
    """
    data = _conditional_get_json(_ATLASSIAN_LISTINGS, f"atlassian:{token}", etags)
    if not isinstance(data, list):
        raise ValueError("atlassian: listings endpoint did not return a list")
    out = []
    for j in _dicts(data):
        portal = j.get("portalJobPost")
        portal = portal if isinstance(portal, dict) else {}
        url = portal.get("portalUrl") or (j.get("applyUrl") or "").split("?")[0]
        if not url:
            continue
        locs = [_atlassian_loc(x) for x in (j.get("locations") or [])
                if isinstance(x, str)]
        desc = "\n\n".join(
            _strip_html(j.get(k)) for k in ("overview", "responsibilities", "qualifications")
            if j.get(k))
        out.append({
            "company": company,
            "title": j.get("title", ""),
            "location": "; ".join(dict.fromkeys(x for x in locs if x)),
            "url": url,
            "posted_at": _atlassian_date(portal.get("updatedDate")),
            "description": desc,
            "source": "atlassian",
        })
    return out


def _fetch_detail(detail_key: str) -> tuple[str, str | None, str | None]:
    """detail_key -> (description, posted_at or None, locations or None).

    Raises on any failure. The locations slot is filled only by sources
    whose list API hides them (workday's "N Locations" collapse).
    """
    kind, token, ident = detail_key.split("|", 2)
    if kind == "smartrecruiters":
        d = get(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{ident}",
            headers=UA, timeout=TIMEOUT).json()
        sections = (d.get("jobAd") or {}).get("sections") or {}
        text = "\n".join(_strip_html(s.get("text", ""))
                         for s in sections.values() if isinstance(s, dict))
        return text.strip(), None, None
    if kind == "workable":
        d = get(
            f"https://apply.workable.com/api/v2/accounts/{token}/jobs/{ident}",
            headers=UA, timeout=TIMEOUT).json()
        return (_strip_html(d.get("description") or d.get("full_description")),
                None, None)
    if kind == "rippling":
        d = get(
            f"https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs/{ident}",
            headers=UA, timeout=TIMEOUT).json()
        return _strip_html(d.get("description")), _iso(d.get("createdOn")), None
    if kind == "workday":
        # ident is the full CXS detail URL (see fetch_workday); the list
        # fetch already resolved the hyphen/underscore tenant form.
        d = get(ident, headers={**UA, "Accept": "application/json"},
                timeout=TIMEOUT).json()
        info = d.get("jobPostingInfo") or {}
        locs = [l for l in ([info.get("location") or ""]
                            + list(info.get("additionalLocations") or [])) if l]
        return (_strip_html(info.get("jobDescription")), None,
                "; ".join(locs) or None)
    if kind == "gem":
        r = post(
            _GEM_GRAPHQL,
            json={"variables": {"boardId": token, "extId": ident},
                  "query": "query ExternalJobPosting($boardId: String!, $extId: String!) {"
                           " oatsExternalJobPosting(boardId: $boardId, extId: $extId) {"
                           " descriptionHtml firstPublishedTsSec } }"},
            headers={**UA, "Content-Type": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        d = ((r.json().get("data") or {}).get("oatsExternalJobPosting") or {})
        return (_strip_html(d.get("descriptionHtml")),
                _iso(d.get("firstPublishedTsSec")), None)
    if kind == "sapsf":
        # ident is the posting path; the page is plain server-rendered HTML.
        r = get(f"https://{token}{ident}", headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        return _sf_detail(r.text)
    if kind == "simplify":
        # The feed carries no description at all - that is what made its rows
        # unjudgeable and let Northwood Space's ITAR-gated internships onto
        # the tracker (2026-08-30). The posting page on simplify.jobs holds
        # the full text, and the feed's `id` IS that page's id, so no search
        # or matching is needed. The page is a Next.js SSR shell; the payload
        # sits in __NEXT_DATA__.
        r = get(f"https://simplify.jobs/p/{ident}", headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        m = _NEXT_DATA.search(r.text or "")
        if not m:
            raise ValueError(f"simplify: no __NEXT_DATA__ for {ident}")
        jp = ((json.loads(m.group(1)).get("props") or {})
              .get("pageProps") or {}).get("jobPosting") or {}
        posted = (jp.get("date_display") or {}).get("date")
        locs = "; ".join(l.get("value", "") for l in (jp.get("locations") or [])
                         if isinstance(l, dict) and l.get("value"))
        return (_strip_html(jp.get("description")), _iso(posted) if posted
                else None, locs or None)
    if kind == "avature":
        # ident is a FolderDetail page URL (Bain's template, see
        # fetch_avature). The posting body is server-rendered into the
        # article__content field blocks; the surrounding page is chrome.
        r = get(ident, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        fields = re.findall(
            r'class="article__content__view__field__value"[^>]*>(.*?)'
            r'(?=<div class="article__content__view__field|</article>|$)',
            r.text or "", re.S)
        return _strip_html("\n".join(fields)).strip(), None, None
    if kind == "icims":
        # ident is the job page URL (see fetch_icims); the iframe form is the
        # one the cards link and the one that carries the JSON-LD.
        r = get(f"{ident}?in_iframe=1", headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        return _parse_icims_detail(r.text or "")
    if kind == "ashbypage":
        # ident is a jobs.ashbyhq.com posting URL (API-disabled org, see
        # _fetch_ashby_hosted); the posting page's appData carries the
        # full descriptionHtml the board listing lacks.
        r = get(ident, headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        desc = _find_key(_ashby_app_data(r.text or ""), "descriptionHtml")
        return _strip_html(desc), None, None
    if kind == "google":
        # ident is the job page URL. Server rendered like the search page,
        # same browser UA requirement. The location chips truncate past two
        # cities ("+30 more"), but multi-location postings restate the full
        # list in their "preferred working location" note; single-location
        # postings lack the note, so the chips are the fallback.
        r = get(ident, headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        page = r.text or ""
        # Exclude backslashes so the match can only hit the rendered note,
        # never its <b>-escaped copy inside the page's JS data blob
        # (a closed posting redirects to the search shell, where only the
        # escaped copies of OTHER jobs' notes exist to mismatch against).
        m = re.search(r"preferred working location from the following:\s*"
                      r"(?:<b>)?([^<\"\\]+)", page)
        locs = m.group(1).strip().rstrip(".") if m else ""
        if not locs:
            m = re.search(r"place</i>((?:<span[^>]*>[^<]*</span>)+)", page)
            if m:
                parts = [p.strip("; ") for p in re.findall(r">([^<]+)<",
                                                           m.group(1))]
                locs = "; ".join(p for p in parts
                                 if p and not p.startswith("+"))
        desc = ""
        i = page.find("<h3>Minimum qualifications:</h3>")
        if i >= 0:
            seg = page[i:i + 20000]
            end = seg.find("Follow Life at Google")
            if end < 0:
                end = seg.find("equal opportunity")
            desc = _strip_html(seg[:end] if end > 0 else seg[:8000])
        return desc, None, locs or None
    if kind == "yc":
        # ident is the job page path; its data-page holds the markdown
        # description plus salary/experience/visa fields worth showing the
        # scorer (WaaS quotes intern pay as an hourly range).
        j = (_yc_page(ident).get("props") or {}).get("job") or {}
        text = (j.get("description") or "").strip()
        extras = "\n".join(f"{label}: {j[key]}" for label, key in
                            (("Salary", "salaryRange"),
                             ("Experience", "minExperience"),
                             ("Visa", "visa")) if j.get(key))
        if extras:
            text = f"{text}\n\n{extras}" if text else extras
        return text, _yc_posted(j.get("createdAt")), None
    if kind == "oraclecloud":
        # token is the host; ident is 'site|id'. The details resource holds
        # the three external text blocks the list omits.
        site, rid = ident.split("|", 1)
        r = get(f"https://{token}/hcmRestApi/resources/latest/"
                f"recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                f"&finder=ById;siteNumber={site},Id=%22{rid}%22",
                headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        items = _dicts((r.json() or {}).get("items"))
        d = items[0] if items else {}
        return (_join_text(d.get("ExternalDescriptionStr"),
                           d.get("ExternalResponsibilitiesStr"),
                           d.get("ExternalQualificationsStr")), None, None)
    if kind == "eightfold":
        # token is the host; ident is 'domain|position id'. The v2 position
        # resource carries job_description for pcsx tenants too.
        domain, pid = ident.split("|", 1)
        r = get(f"https://{token}/api/apply/v2/jobs/{pid}",
                params={"domain": domain}, headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json() or {}
        return (_strip_html(d.get("job_description")),
                _eightfold_epoch(d.get("t_create")), None)
    if kind == "apple":
        # ident is the posting URL; the page redirects to its slug form and
        # embeds jobDetails.jobsData in the same hydration blob as search.
        r = get(ident, headers={**_BROWSER_UA, "Accept": "text/html"},
                timeout=TIMEOUT)
        r.raise_for_status()
        jd = (((_apple_hydration(r.text).get("loaderData") or {})
               .get("jobDetails") or {}).get("jobsData") or {})
        quals = [f"{label}:\n{jd[key]}" for label, key in
                 (("Minimum Qualifications", "minimumQualifications"),
                  ("Preferred Qualifications", "preferredQualifications"))
                 if jd.get(key)]
        locs = "; ".join(_apple_location(l) for l in _dicts(jd.get("locations")))
        return (_join_text(jd.get("jobSummary"), jd.get("description"), *quals),
                None, locs or None)
    if kind == "goldman":
        # ident is the role page URL; __NEXT_DATA__ carries the role.
        r = get(ident, headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        m = _NEXT_DATA.search(r.text or "")
        if not m:
            raise ValueError(f"goldman: no __NEXT_DATA__ for {ident}")
        role = (((json.loads(m.group(1)).get("props") or {})
                 .get("pageProps") or {}).get("role") or {})
        return _strip_html(role.get("descriptionHtml")), None, None
    if kind == "meta":
        r = _impersonate("GET", ident)
        if r.status_code != 200:
            raise RuntimeError(f"meta: job page answered {r.status_code}")
        job = _meta_job(r.text or "")
        locs = "; ".join(l for l in (job.get("locations") or [])
                         if isinstance(l, str))
        return _meta_text(job), None, locs or None
    if kind == "tesla":
        # ident is the numeric job id; the detail JSON is behind the same
        # Akamai wall as the list, so it rides Firecrawl too.
        d = _firecrawl_json(f"https://www.tesla.com/cua-api/careers/job/{ident}")
        text = _join_text(d.get("jobDescription"), d.get("description"))
        # The detail writes the city in capitals ("PALO ALTO, California").
        parts = [p.strip() for p in (d.get("location") or "").split(",")]
        loc = ", ".join(p.title() if p.isupper() else p for p in parts if p)
        return text, None, loc or None
    if kind == "citadel":
        text, offices = _citadel_detail(_firecrawl_raw(ident))
        return text, None, offices
    raise ValueError(f"unknown detail kind: {kind}")


# One run's detail-call budget, shared by cli's pre-dedupe pass over vague
# locations and the main enrich pass after it.
ENRICH_CAP = 400


def needs_detail(j: dict) -> bool:
    """A row is worth a detail call when it lacks text OR a real location.

    Shure's iCIMS cards carry a full description and no location at all, and
    skipping them on text length alone left 25 surfaced rows blank
    (2026-09-09) - an empty location fails open through every gate.
    A row already tried this run (`_detail` set by enrich) is never called
    again: a failed detail page stays failed within one run.
    """
    if not j.get("detail_key") or j.get("_detail"):
        return False
    return (len(j.get("description") or "") < 200
            or prefilter.vague_location(j.get("location")))


def enrich(jobs: list[dict], cap: int = ENRICH_CAP) -> int:
    """Fill in descriptions (and missing dates/locations) via detail calls.

    Mutates the jobs in place; failures leave a job exactly as it was, since a
    title-only posting is still scoreable. A workday "N Locations" placeholder
    is replaced with the detail call's real office list, so the scorer can
    judge the us_only question the prefilter had to pass on. The cap is a cost
    guardrail for the first run after adding a big board.

    The cap was 80 until 2026-08-28: the Ambarella backfill exceeded it, its
    rows were scored title-only, and hard Master's/PhD requirements sitting
    in the descriptions never reached the judge. 200 detail calls is still
    cheap (throttled per host, 8 threads) and covers every wave seen so far.
    Raised to 400 on 2026-09-01 for the YC board minting: ~150 new boards'
    first crawl arrives as one wave of description-less rows, and a clipped
    row is scored title-only ONCE, forever.

    Each attempted row is marked `_detail` = "ok" or "failed" (copies made
    by filter_new carry the mark along), so cmd_run's pre-dedupe pass and
    its main pass never call one row twice.
    """
    import concurrent.futures as cf
    targets = [j for j in jobs if needs_detail(j)][:max(0, cap)]
    done = 0
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_detail, j["detail_key"]): j for j in targets}
        for f in cf.as_completed(futures):
            j = futures[f]
            try:
                desc, posted, locations = f.result()
            except Exception:
                j["_detail"] = "failed"
                continue
            j["_detail"] = "ok"
            if desc:
                j["description"] = desc
                done += 1
            if posted and not j.get("posted_at"):
                j["posted_at"] = posted
            # Replace the collapsed placeholder AND a fully empty location -
            # some workday postings carry no locationsText at all, and an
            # empty location fails open through us_only, which is how a
            # Norwegian-language Oslo internship reached the digest
            # (2026-08-25). A real locationsText ("Santa Clara, CA") is
            # already the better string and stays untouched - rewriting it
            # would churn tracker rows for nothing.
            if locations and prefilter.vague_location(j.get("location")):
                j["location"] = locations
    return done


HN_API = "https://hn.algolia.com/api/v1"
_HN_URL_PAT = re.compile(r"https?://\S+|\(\s*\)")
# A location segment in an HN header line: a remote/onsite marker or a
# ", XX" state/country abbreviation. Deliberately narrow - a wrong guess
# feeds the us_only gate, while an empty location fails open to the scorer.
_HN_LOC_PAT = re.compile(r"(?i:\b(?:remote|hybrid|on-?site)\b)|,\s*[A-Z]{2}\b")


def _hn_latest_thread(author: str) -> int:
    """Story id of the newest 'Ask HN: Who is hiring?' thread by `author`."""
    r = get(f"{HN_API}/search_by_date?tags=story,author_{author}&hitsPerPage=10",
            headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    for hit in _dicts(r.json().get("hits")):
        if (hit.get("title") or "").lower().startswith("ask hn: who is hiring"):
            return int(hit["objectID"])
    raise ValueError(f"hn: no 'Who is hiring?' story by {author}")


def fetch_hn(company: str, token: str) -> list[dict]:
    """Intern-relevant top-level comments of HN's monthly Who-is-hiring thread.

    Token is the posting account (`whoishiring`); the adapter finds the
    newest thread itself, so month rollover needs no config change. Each
    top-level comment is one employer's ad. The adapter pre-narrows to
    comments whose TEXT matches the intern pattern because the thread is
    ~95% full-time senior roles - this is the same deterministic regex the
    prefilter uses, just applied to the body, since the freeform text has no
    real title field.

    Parsing is regex-only on the conventional "Company | Role | Location |
    ..." header line and must stay deterministic: company/title/location
    feed job_id(), and an unstable parse makes old comments look new. A
    comment EDIT changing the parse is absorbed by URL identity - the
    item?id= permalink is the row's stable second identity in filter_new.
    Prefilter gates on the title, so when only the body says intern, the
    title gets an explicit "- internship" suffix rather than loosening the
    global gate.
    """
    story = _hn_latest_thread(token)
    r = get(f"{HN_API}/items/{story}", headers=UA, timeout=60)
    r.raise_for_status()
    out = []
    for c in _dicts(r.json().get("children")):
        raw = c.get("text") or ""
        text = _strip_html(raw)
        if not text or not prefilter.INTERN_PAT.search(text):
            continue
        # HN comment HTML separates paragraphs with bare `<p>` tags, which
        # _strip_html turns into spaces - splitting on the first one BEFORE
        # stripping is what keeps the body out of the header line.
        header = _strip_html(re.split(r"<p[^>]*>", raw, maxsplit=1,
                                      flags=re.I)[0]).split("\n", 1)[0]
        segs = [s.strip() for s in header.split("|") if s.strip()]
        # Cap the company at 60 chars: pipe-less headers ("Beacon AI builds
        # intelligent...") would otherwise put a whole sentence in the
        # tracker's company column. Deterministic, so job_id stays stable.
        comp = _HN_URL_PAT.sub("", segs[0]).strip(" -,;:()*") if segs else ""
        comp = comp[:60].rstrip()
        title = next((s for s in segs[1:] if prefilter.INTERN_PAT.search(s)),
                     "") or (segs[1] if len(segs) > 1 else "")
        if not prefilter.INTERN_PAT.search(title):
            title = f"{title} - internship" if title else "Internship"
        loc = next((s for s in segs[1:] if s is not title
                    and _HN_LOC_PAT.search(s)), "")
        out.append({
            "company": comp or company,
            "title": title,
            "location": loc,
            "url": f"https://news.ycombinator.com/item?id={c.get('id')}",
            "posted_at": _iso(c.get("created_at")),
            "description": text,
            "source": "hn",
        })
    return out


def _flight_chunk(text: str, key: str):
    """The `<key>:` row of an RSC flight stream, decoded.

    Flight rows are newline-separated but their JSON strings may themselves
    contain newlines (job descriptions do), so the stream cannot be split on
    lines - the payload is decoded from the row's offset instead.
    """
    marker = f"\n{key}:"
    i = 0 if text.startswith(f"{key}:") else text.find(marker) + 1
    if i <= 0 and not text.startswith(f"{key}:"):
        return None
    try:
        return json.JSONDecoder(strict=False).raw_decode(text, i + len(key) + 1)[0]
    except ValueError:
        return None


_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

_A16Z_CHUNK = re.compile(r'src="(/_next/static/chunks/[^"]+\.js)"')
_A16Z_ACTION = re.compile(
    r'createServerReference\)\("([0-9a-f]{32,})"[^)]*?"loadMorePublicJobs"')


def a16z_jobs(host: str, max_pages: int = 60, per_page: int = 25) -> list[dict]:
    """Every internship on a16z's own portfolio board (jobs.a16z.com).

    a16z replatformed off the Consider API onto a Next.js app whose job list
    loads through the `loadMorePublicJobs` server action, so `consider_jobs`
    stopped working there. The action id is a build hash - it changes on every
    deploy - so it is read out of the page's JS chunks at sweep time rather
    than pinned. Args mirror the site's own call: (filters, pagination); the
    `internships` filter is applied server-side, which turns a 19k-job sweep
    into a few hundred rows. Pages are 0-indexed - the site renders page 0
    server-side and only ever asks the action for 1 onward, so starting at 1
    silently drops the newest `limit` postings. The response is an RSC flight
    stream whose "1:" row is the JSON payload.
    """
    page = get(f"https://{host}/", headers=UA, timeout=60)
    page.raise_for_status()
    action = ""
    for chunk in dict.fromkeys(_A16Z_CHUNK.findall(page.text or "")):
        r = get(f"https://{host}{chunk}", headers=UA, timeout=60)
        m = _A16Z_ACTION.search(r.text or "")
        if m:
            action = m.group(1)
            break
    if not action:
        raise ValueError(f"a16z: no loadMorePublicJobs action id on {host}")
    headers = {**UA, "Next-Action": action, "Content-Type": "application/json"}
    out: list[dict] = []
    seen: set[str] = set()
    for n in range(max_pages):
        r = post(f"https://{host}/", headers=headers, timeout=90,
                 data=json.dumps([{"internships": True},
                                  {"page": n, "limit": per_page}]))
        r.raise_for_status()
        d = _flight_chunk(r.text or "", "1")
        if d is None:
            raise ValueError(f"a16z: no action payload from {host}")
        jobs = _dicts(d.get("jobs"))
        fresh = [j for j in jobs if str(j.get("id")) not in seen]
        seen.update(str(j.get("id")) for j in fresh)
        out.extend({"title": j.get("title"),
                    "applyUrl": j.get("apply_url"),
                    "companyName": j.get("company_name")} for j in fresh)
        if d.get("isDone") or not fresh:
            break
    return out


_CONSIDER_CSRF = re.compile(r'"csrfToken":"([^"]+)"')
_CONSIDER_BOARD = re.compile(r'"board":\{"id":"([^"]+)"')


_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def getro_jobs(host: str, query: str = "intern", per_page: int = 200,
               max_pages: int = 40) -> list[dict]:
    """Every `query`-matching job on a Getro-hosted VC portfolio board.

    Most of the top-tier funds' boards (Lux, Khosla, General Catalyst, Accel,
    8VC, Thrive, Menlo, Insight, NFX...) moved off Consider onto Getro, whose
    page only server-renders 20 rows. The list itself comes from Getro's
    public search API, keyed by the network id embedded in the page's
    __NEXT_DATA__; the endpoint and its parameter names were read out of the
    app bundle (2026-09-11) - it takes no auth. `query` is matched
    server-side ("intern" also hits "Internship"), which turns an 8k-job
    board into a couple thousand rows. Discovery only, like consider_jobs:
    the rows are never stored, the boards their apply URLs derive to are
    what gets polled. Nominal's Summer 2027 SWE intern (Gem, on no GitHub
    list) was the first hit on Lux's board, which is exactly the miss this
    exists to close.
    """
    page = get(f"https://{host}/jobs", headers=UA, timeout=60)
    page.raise_for_status()
    m = _NEXT_DATA.search(page.text or "")
    network = None
    if m:
        try:
            network = (json.loads(m.group(1)).get("props", {}).get("pageProps", {})
                       .get("network") or {}).get("id")
        except (ValueError, AttributeError):
            network = None
    if not network:
        raise ValueError(f"getro: no network id on {host}")
    out: list[dict] = []
    for n in range(max_pages):
        r = post(f"https://api.getro.com/api/v2/collections/{network}/search/jobs",
                 headers={**UA, "Content-Type": "application/json",
                          "Accept": "application/json"},
                 json={"hits_per_page": per_page, "page": n, "filters": "",
                       "query": query}, timeout=60)
        r.raise_for_status()
        jobs = _dicts((r.json().get("results") or {}).get("jobs"))
        for j in jobs:
            org = j.get("organization") or {}
            out.append({
                "title": j.get("title", ""),
                "url": j.get("url", ""),
                "companyName": org.get("name", ""),
                "location": "; ".join(_dicts_str(j.get("locations"))),
                "posted_at": _iso(j.get("createdAt")),
            })
        if len(jobs) < per_page:
            break
    return out


def _dicts_str(seq) -> list[str]:
    return [str(x) for x in (seq or []) if isinstance(x, (str, int))]


def consider_jobs(host: str, max_pages: int = 40) -> list[dict]:
    """Every job on a Consider-platform VC portfolio board (jobs.a16z.com).

    Deliberately NOT an ADAPTERS entry: these boards aggregate tens of
    thousands of description-less portfolio jobs, so they feed the weekly
    board DISCOVERY in `cmd_boards`, never the 10-minute posting crawl.
    The search API ignores its own query field, so the sweep pages through
    everything (500/page, `meta.sequence` cursor) and the caller filters.
    The CSRF token and board id are embedded in the /jobs page; the token
    only validates alongside the session cookie from that same response.
    """
    page = get(f"https://{host}/jobs", headers=UA, timeout=60)
    page.raise_for_status()
    csrf = _CONSIDER_CSRF.search(page.text or "")
    board = _CONSIDER_BOARD.search(page.text or "")
    if not (csrf and board):
        raise ValueError(f"consider: no csrf/board id on {host}")
    headers = {**UA, "X-CSRF-Token": csrf.group(1),
               "Cookie": "; ".join(f"{k}={v}" for k, v in
                                   page.cookies.get_dict().items())}
    out: list[dict] = []
    seq = None
    for _ in range(max_pages):
        meta: dict[str, Any] = {"size": 500}
        if seq:
            meta["sequence"] = seq
        r = post(f"https://{host}/api-boards/search-jobs", headers=headers,
                 json={"board": {"id": board.group(1), "isParent": True},
                       "query": {}, "meta": meta}, timeout=90)
        r.raise_for_status()
        d = r.json()
        jobs = _dicts(d.get("jobs"))
        if not jobs:
            break
        out.extend(jobs)
        seq = (d.get("meta") or {}).get("sequence")
        if not seq:
            break
    return out



# ---------------------------------------------------------------------------
# Big-tech career sites with no hosted ATS. Added 2026-09-28 after an audit
# found Meta, Apple, Microsoft, Qualcomm, AMD, Oracle, JPMorgan, American
# Express, TikTok, ByteDance, Tesla, Citadel and Goldman reaching jobs.db only
# through the aggregator feeds, hours to days after the posting went up.

_LOOKBACK_DAYS = 30


def _lookback_floor(days: int = _LOOKBACK_DAYS) -> str:
    """YYYY-MM-DD of the oldest posting a date-sorted board reads."""
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=days)).strftime("%Y-%m-%d")


def _join_text(*parts: Any) -> str:
    return "\n\n".join(p for p in (_strip_html(x) for x in parts if x) if p)


def fetch_oraclecloud(company: str, token: str) -> list[dict]:
    """Oracle Cloud HCM career sites. token = 'host|site'.

    e.g. 'jpmc.fa.oraclecloud.com|CX_1001' - the site number sits in every
    posting URL (/hcmUI/CandidateExperience/en/sites/<site>/job/<id>), which
    is also the form the aggregator feeds carry. The public REST finder
    answers 200 requisitions a page. Its keyword match reads descriptions
    ("internal" counts), so `keyword=intern` trims rather than filters -
    JPMorgan still answers 3,800 rows - and the list is read newest-first
    down to _LOOKBACK_DAYS instead; the title prefilter does the real work.
    No description in the list; each row gets a detail_key.
    """
    host, site = token.split("|", 1)
    floor = _lookback_floor()
    out, seen, offset = [], set(), 0
    for _ in range(25):  # 5,000 rows a poll is far past any lookback
        url = (f"https://{host}/hcmRestApi/resources/latest/"
               f"recruitingCEJobRequisitions?onlyData=true"
               f"&expand=requisitionList.secondaryLocations"
               f"&finder=findReqs;siteNumber={site},keyword=intern,limit=200,"
               f"offset={offset},sortBy=POSTING_DATES_DESC")
        r = get(url, headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        items = _dicts((r.json() or {}).get("items"))
        reqs = _dicts(items[0].get("requisitionList")) if items else []
        stale = False
        for q in reqs:
            rid = str(q.get("Id") or "")
            if not rid or rid in seen:
                continue
            posted = str(q.get("PostedDate") or "")[:10]
            if posted and posted < floor:
                stale = True
                break
            seen.add(rid)
            locs = [q.get("PrimaryLocation") or ""] + [
                l.get("Name") or "" for l in _dicts(q.get("secondaryLocations"))]
            out.append({
                "company": company,
                "title": q.get("Title") or "",
                "location": "; ".join(l for l in locs[:8] if l),
                "url": (f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                        f"{site}/job/{rid}"),
                "posted_at": _iso(q.get("PostedDate")),
                "description": "",
                "source": "oraclecloud",
                "detail_key": f"oraclecloud|{host}|{site}|{rid}",
            })
        if stale or len(reqs) < 200:
            break
        offset += 200
    return out


def fetch_jibe(company: str, token: str) -> list[dict]:
    """iCIMS Attract (Jibe) career sites: careers.amd.com, careers.rivian.com.

    token = host. /api/jobs is a plain JSON search, 100 rows a page, with the
    description, the posting date and a structured location inline. Posting
    URLs are /jobs/<req_id>?icims=1, the form the aggregator feeds carry.
    """
    out, seen = [], set()
    for page in range(1, 11):
        r = get(f"https://{token}/api/jobs", params={
            "keywords": "intern", "page": page, "limit": 100,
            "sortBy": "posted_date", "descending": "true",
        }, headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json() or {}
        rows = [j.get("data") for j in _dicts(data.get("jobs"))]
        rows = [j for j in rows if isinstance(j, dict)]
        for j in rows:
            rid = str(j.get("req_id") or j.get("slug") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            loc = j.get("full_location") or ", ".join(
                x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
            out.append({
                "company": company,
                "title": j.get("title") or "",
                "location": loc,
                "url": f"https://{token}/jobs/{rid}?icims=1",
                "posted_at": _iso(j.get("posted_date")),
                "description": _join_text(j.get("description"),
                                          j.get("responsibilities"),
                                          j.get("qualifications")),
                "source": "jibe",
            })
        total = data.get("totalCount")
        if len(rows) < 100 or (isinstance(total, int) and len(seen) >= total):
            break
    return out


# SAP SuccessFactors career sites (the "jobs2web" hosted front end: Sephora's
# jobs.sephora.com, NetApp's jobs.netapp.com). No public JSON; the search
# page is server-rendered HTML, 25 rows a page, paged with `startrow`.
_SF_ROW = re.compile(r'<tr class="data-row">(.*?)</tr>', re.S)
_SF_LINK = re.compile(r'<a\b[^>]*class="jobTitle-link"[^>]*>(.*?)</a>', re.S)
_SF_HREF = re.compile(r'href="([^"]+)"')
_SF_LOC = re.compile(r'class="jobLocation"[^>]*>(.*?)</span>', re.S)
_SF_DATE = re.compile(r'class="jobDate"[^>]*>(.*?)</span>', re.S)
_SF_DESC = re.compile(r'<span class="jobdescription">(.*?)</span>\s*</div>', re.S)
_SF_META = re.compile(r'<meta itemprop="(addressLocality|addressRegion|addressCountry)" content="([^"]*)"')
# /USA/job/San-Francisco-Intern%2C-AIML-Engineering-CA-USA_519958/1371166455/
# Every jobs2web posting URL ends in /job/<slug>/<numeric id>/; the region
# segment in front is optional and varies per tenant.
_SF_URL_PAT = re.compile(r'^/(?:[^/]+/)?job/[^/]+/(\d{6,})/?$')


def _sf_date(text: str) -> str | None:
    """'Oct 11, 2026' -> ISO date; anything else -> None."""
    text = _strip_html(text).strip()
    for fmt in ("%b %d, %Y", "%d %b %Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).replace(
                tzinfo=dt.timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def fetch_sapsf(company: str, token: str) -> list[dict]:
    """SuccessFactors (jobs2web) career site. token = host.

    Reads https://<host>/search/?q=intern newest-first, 25 rows a page, and
    stops at the first short page. The list carries title, location and the
    posting's reference date but no text; `detail_key` points `enrich` at
    the posting page, whose `span.jobdescription` holds the description.
    The keyword search matches descriptions too, so prefilter does the real
    title gating, as for every other keyword-scoped adapter.
    """
    out, seen = [], set()
    for start in range(0, 250, 25):
        r = get(f"https://{token}/search/", params={
            "q": "intern", "sortColumn": "referencedate",
            "sortDirection": "desc", "startrow": start,
        }, headers=_BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
        rows = _SF_ROW.findall(r.text or "")
        new = 0
        for row in rows:
            m = _SF_LINK.search(row)
            if not m:
                continue
            href_m = _SF_HREF.search(m.group(0))
            if not href_m:
                continue
            href = html.unescape(href_m.group(1))
            if href in seen:
                continue
            seen.add(href)
            new += 1
            loc = _SF_LOC.search(row)
            date = _SF_DATE.search(row)
            out.append({
                "company": company,
                "title": _strip_html(m.group(1)),
                "location": _sf_location(_strip_html(loc.group(1)) if loc else ""),
                "url": f"https://{token}{href}" if href.startswith("/") else href,
                "posted_at": _sf_date(date.group(1)) if date else None,
                "description": "",
                "source": "sapsf",
                "detail_key": f"sapsf|{token}|{href}",
            })
        if len(rows) < 25 or not new:
            break
    return out


def _sf_location(text: str) -> str:
    """'Perryman, MD, US, USA_522982' -> 'Perryman, MD, US'.

    jobs2web appends the tenant's site code or a postal code and a "+N more"
    marker after the place; the gates would read those as place words.
    """
    text = re.sub(r"\s*\+\d+\s*more\S*$", "", text)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    # Site codes (USA_522982) and postal codes (02451, "1118 BH", "SL4 1RS")
    # trail the real place; a bare country code ("US") has no digit and stays.
    while parts and (re.fullmatch(r"[A-Za-z]{2,4}_\d+", parts[-1])
                     or (re.fullmatch(r"[\dA-Za-z -]{3,10}", parts[-1])
                         and re.search(r"\d", parts[-1]))):
        parts = parts[:-1]
    return ", ".join(parts)


def _sf_detail(page: str) -> tuple[str, str | None, str | None]:
    m = _SF_DESC.search(page or "")
    text = _strip_html(m.group(1)) if m else ""
    meta = dict((k, v) for k, v in _SF_META.findall(page or ""))
    loc = ", ".join(v for v in (meta.get("addressLocality"), meta.get("addressRegion"),
                                meta.get("addressCountry")) if v) or None
    return text, None, loc


_APPLE_HYDRATION = re.compile(
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\("(.*?)"\);', re.S)


def _js_string(lit: str) -> str:
    """Decode the body of a JS double-quoted string literal."""
    try:
        return json.loads('"' + lit + '"')
    except ValueError:
        return lit.encode("latin-1", "backslashreplace").decode("unicode_escape")


def _apple_hydration(page: str) -> dict:
    m = _APPLE_HYDRATION.search(page or "")
    if not m:
        raise ValueError("apple: no hydration data on the page")
    return json.loads(_js_string(m.group(1)))


def _apple_location(l: dict) -> str:
    country = l.get("countryName") or ""
    if country == "United States of America":
        country = "United States"
    parts: list[str] = []
    for x in (l.get("name") or l.get("city"), l.get("stateProvince"), country):
        # A country-wide posting names the country as its "city" too.
        if x and x not in parts:
            parts.append(x)
    return ", ".join(parts)


def fetch_apple(company: str, token: str) -> list[dict]:
    """jobs.apple.com. token = a team filter; 'internships-STDNT-INTRN' is
    Students > Internships (81 rows worldwide on 2026-09-28).

    Apple's search API needs a CSRF dance and its free-text search ranks
    descriptions ("intern" answers 1,389 rows led by CPU engineers), but the
    HTML search page embeds its results as React Router hydration JSON, 20 a
    page, newest first. Locations come structured (the US ones say
    'United States of America'); the list carries only a summary, so each
    row gets a detail_key for the posting page, which embeds the full text
    and the qualification lists the same way.
    """
    out, seen = [], set()
    for page in range(1, 21):
        r = get("https://jobs.apple.com/en-us/search",
                params={"team": token, "sort": "newest", "page": page},
                headers={**_BROWSER_UA, "Accept": "text/html"}, timeout=TIMEOUT)
        r.raise_for_status()
        data = ((_apple_hydration(r.text).get("loaderData") or {})
                .get("search") or {})
        rows = _dicts(data.get("searchResults"))
        for j in rows:
            pid = str(j.get("positionId") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            url = f"https://jobs.apple.com/en-us/details/{pid}"
            out.append({
                "company": company,
                "title": j.get("postingTitle") or "",
                "location": "; ".join(_apple_location(l) for l in
                                      _dicts(j.get("locations"))[:8]),
                "url": url,
                "posted_at": _apple_date(j),
                "description": j.get("jobSummary") or "",
                "source": "apple",
                "detail_key": f"apple||{url}",
            })
        total = data.get("totalRecords")
        if len(rows) < 20 or (isinstance(total, int) and len(seen) >= total):
            break
    return out


def _apple_date(j: dict) -> str | None:
    iso = _iso(j.get("postDateInGMT"))
    if iso and iso[:4].isdigit():
        return iso
    try:
        return dt.datetime.strptime(j.get("postingDate") or "",
                                    "%b %d, %Y").date().isoformat()
    except ValueError:
        return None


_BYTEDANCE_BRANDS = {
    # brand: (api host, website-path header, origin, posting url template)
    "tiktok": ("api.lifeattiktok.com", "tiktok", "https://lifeattiktok.com",
               "https://lifeattiktok.com/search/{id}"),
    "bytedance": ("jobs.bytedance.com", "en", "https://joinbytedance.com",
                  "https://jobs.bytedance.com/en/position/{id}/detail"),
}


def _bd_parent(node: dict, level: int) -> str:
    """en_name of the location ancestor at `level` (1 country, 2 state)."""
    while isinstance(node, dict):
        if node.get("location_type") == level:
            return node.get("en_name") or ""
        node = node.get("parent")
    return ""


def fetch_bytedance(company: str, token: str) -> list[dict]:
    """ByteDance's careers platform. token = 'tiktok' (lifeattiktok.com) or
    'bytedance' (joinbytedance.com); both front the same supplier API.

    A keyword search with no location answers ~3,000 rows worldwide, so the
    filters config is read first and every US city code goes into
    location_code_list (1,145 rows for TikTok, 381 for ByteDance on
    2026-09-28), 100 a page. Rows carry the description and requirements
    inline but no date. Posting URLs follow the aggregator feeds' forms so
    url identity lines up. The 'website-path' header is the site's language
    path, not the brand ('bytedance' is refused).
    """
    api_host, site, origin, url_tpl = _BYTEDANCE_BRANDS[token]
    headers = {**_BROWSER_UA, "Content-Type": "application/json",
               "accept-language": "en-US", "website-path": site,
               "Origin": origin, "Referer": origin + "/"}
    base = f"https://{api_host}/api/v1/public/supplier"
    r = post(f"{base}/config/job/filters", json={}, headers=headers,
             timeout=TIMEOUT)
    r.raise_for_status()
    cities = _dicts(((r.json() or {}).get("data") or {}).get("city_list"))
    us = [c["code"] for c in cities
          if c.get("code") and _bd_parent(c, 1) == "United States of America"]
    if not us:
        raise ValueError(f"bytedance: no US cities in the {token} filters")
    out, seen, offset = [], set(), 0
    for _ in range(40):
        r = post(f"{base}/search/job/posts", json={
            "keyword": "intern", "limit": 100, "offset": offset,
            "job_category_id_list": [], "location_code_list": us,
            "subject_id_list": [], "recruitment_id_list": [],
            "job_function_id_list": [],
        }, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        posts = _dicts(data.get("job_post_list"))
        for p in posts:
            pid = str(p.get("id") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            city = p.get("city_info") or {}
            out.append({
                "company": company,
                "title": p.get("title") or "",
                "location": ", ".join(x for x in (city.get("en_name"),
                                                  _bd_parent(city, 2)) if x),
                "url": url_tpl.format(id=pid),
                "posted_at": None,
                "description": _join_text(p.get("description"),
                                          p.get("requirement")),
                "source": "bytedance",
            })
        offset += len(posts)
        total = data.get("count")
        if not posts or (isinstance(total, int) and offset >= total):
            break
    return out


_GS_GRAPHQL = "https://api-higher.gs.com/gateway/api/v1/graphql"
_GS_QUERY = (
    "query GetCampusRoles($searchQueryInput: RoleSearchQueryInput!) {"
    " roleSearch(searchQueryInput: $searchQueryInput) { totalCount items {"
    " roleId jobTitle division status locations { primary state country city }"
    " } } }")


def fetch_goldman(company: str, token: str) -> list[dict]:
    """higher.gs.com, Goldman's own portal. token = 'students' (the CAMPUS
    experience: analyst programs and summer analyst internships, 277 roles
    worldwide on 2026-09-28) or 'early-career'.

    The site is a Next.js app over a GraphQL gateway; the query is the one
    its results page runs, 100 roles a page (the page asks for 20). Titles
    are pipe-separated ("2027 | Americas | New York | Engineering | Summer
    Analyst"); locations come structured. No date in the list. The role
    page server-renders descriptionHtml into __NEXT_DATA__, so each row
    gets a detail_key for it.
    """
    import uuid
    experiences = {"students": ["CAMPUS"],
                   "early-career": ["EARLY_CAREER", "PROFESSIONAL"]}[token]
    out, seen, page = [], set(), 0
    for _ in range(20):
        r = post(_GS_GRAPHQL, json={
            "operationName": "GetCampusRoles",
            "variables": {"searchQueryInput": {
                "experiences": experiences, "filters": [],
                "page": {"pageNumber": page, "pageSize": 100}}},
            "query": _GS_QUERY,
        }, headers={**_BROWSER_UA, "Content-Type": "application/json",
                    "Origin": "https://higher.gs.com",
                    "Referer": "https://higher.gs.com/",
                    "x-higher-request-id": str(uuid.uuid4())},
            timeout=TIMEOUT)
        r.raise_for_status()
        body = r.json() or {}
        rs = (body.get("data") or {}).get("roleSearch") or {}
        if not rs and body.get("errors"):
            raise ValueError(f"goldman: {body['errors'][0].get('message')}")
        items = _dicts(rs.get("items"))
        for it in items:
            num = str(it.get("roleId") or "").split("_", 1)[0]
            if not num.isdigit() or num in seen:
                continue
            seen.add(num)
            locs = [", ".join(x for x in (l.get("city"), l.get("state"),
                                          l.get("country")) if x)
                    for l in _dicts(it.get("locations"))]
            url = f"https://higher.gs.com/roles/{num}?type={token}"
            out.append({
                "company": company,
                "title": it.get("jobTitle") or "",
                "location": "; ".join(l for l in locs[:8] if l),
                "url": url,
                "posted_at": None,
                "description": "",
                "source": "goldman",
                "detail_key": f"goldman||{url}",
            })
        page += 1
        total = rs.get("totalCount")
        if not items or (isinstance(total, int) and len(seen) >= total):
            break
    return out


def _impersonate(method: str, url: str, **kw):
    """One HTTP call through curl_cffi's browser TLS fingerprint.

    metacareers.com answers every plain client - requests, curl, a browser
    UA on either - with 400; only a Chrome-shaped TLS handshake gets the
    page (2026-09-28). Same per-host throttle as request(). The import is
    lazy so nothing else needs the dependency.
    """
    from curl_cffi import requests as cffi
    timeout = kw.pop("timeout", TIMEOUT)
    with _Throttle(url):
        return cffi.request(method, url, impersonate="chrome", timeout=timeout,
                            **kw)


# Relay operation id of CareersJobSearchResultsDataQuery, read from the
# site's JS on 2026-09-28. Facebook rotates these rarely; a rotation answers
# with a GraphQL error, which reaches board health as a fetch failure.
_META_SEARCH_DOC = "27506805582236862"
_META_LSD = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')


def fetch_meta(company: str, token: str) -> list[dict]:
    """metacareers.com. token = search query ('intern').

    The site is a Relay app: the search page yields an LSD token, and one
    GraphQL call with the results query's doc id answers every match in one
    list (422 rows for 'intern', worldwide, on 2026-09-28) - id, title,
    offices, teams; no date, no text. The job page embeds the full
    description and both qualification lists, so rows get a detail_key.
    """
    # Any metacareers page carries the LSD token. The search page is the one
    # Meta rate-limits per IP (429 for hours after a burst, 2026-09-29, while
    # the home page and the GraphQL endpoint kept answering), so the token
    # comes from the home page.
    page = _impersonate("GET", "https://www.metacareers.com/")
    if page.status_code != 200:
        raise RuntimeError(f"meta: home page answered {page.status_code}")
    m = _META_LSD.search(page.text or "")
    if not m:
        raise ValueError("meta: no LSD token on the home page")
    lsd = m.group(1)
    variables = {"search_input": {
        "q": token, "divisions": [], "offices": [], "roles": [],
        "leadership_levels": [], "saved_jobs": [], "saved_searches": [],
        "sub_teams": [], "teams": [], "is_leadership": False,
        "is_remote_only": False, "sort_by_new": True, "page": 1,
    }, "isLoggedIn": False, "viewasUserID": None}
    r = _impersonate("POST", "https://www.metacareers.com/graphql", data={
        "av": "0", "__user": "0", "__a": "1", "lsd": lsd,
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": "CareersJobSearchResultsDataQuery",
        "variables": json.dumps(variables), "server_timestamps": "true",
        "doc_id": _META_SEARCH_DOC,
    }, headers={"X-FB-LSD": lsd,
                "X-FB-Friendly-Name": "CareersJobSearchResultsDataQuery",
                "Origin": "https://www.metacareers.com",
                "Referer": "https://www.metacareers.com/jobs/"})
    if r.status_code != 200:
        raise RuntimeError(f"meta: graphql answered {r.status_code}")
    text = r.text or ""
    body = json.loads(text[text.find("{"):])
    js = (body.get("data") or {}).get("job_search_with_featured_jobs") or {}
    if not js and body.get("errors"):
        raise ValueError(f"meta: {body['errors'][0].get('message')}")
    out, seen = [], set()
    for j in _dicts(js.get("all_jobs")) + _dicts(js.get("featured_jobs")):
        jid = str(j.get("id") or "")
        if not jid or jid in seen:
            continue
        seen.add(jid)
        url = f"https://www.metacareers.com/jobs/{jid}"
        out.append({
            "company": company,
            "title": j.get("title") or "",
            "location": "; ".join(l for l in (j.get("locations") or [])
                                  if isinstance(l, str))[:400],
            "url": url,
            "posted_at": None,
            "description": "",
            "source": "meta",
            "detail_key": f"meta||{url}",
        })
    return out


def _meta_job(page: str) -> dict:
    """The xcp_requisition_job_description object a Meta job page embeds."""
    key = '"xcp_requisition_job_description"'
    i = (page or "").find(key)
    if i < 0:
        raise ValueError("meta: no job description on the page")
    start = page.rfind('<script type="application/json"', 0, i)
    start = page.find(">", start) + 1
    end = page.find("</script>", i)
    blob = json.loads(page[start:end])

    def find(o):
        if isinstance(o, dict):
            if "xcp_requisition_job_description" in o:
                return o["xcp_requisition_job_description"]
            for v in o.values():
                r = find(v)
                if r is not None:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = find(v)
                if r is not None:
                    return r
        return None
    return find(blob) or {}


def _meta_text(job: dict) -> str:
    desc = job.get("description")
    if isinstance(desc, str) and desc.lstrip().startswith("{"):
        # The page serializes the rich-text field as a JSON string of its
        # own ({"__html": ...}), not as a nested object.
        try:
            desc = json.loads(desc)
        except ValueError:
            pass
    if isinstance(desc, dict):
        desc = desc.get("__html")
    parts = [desc]
    for label, key in (("Responsibilities", "responsibilities"),
                       ("Minimum Qualifications", "minimum_qualifications"),
                       ("Preferred Qualifications", "preferred_qualifications")):
        items = [x.get("item") for x in _dicts(job.get(key)) if x.get("item")]
        if items:
            parts.append(f"{label}:\n" + "\n".join(f"- {x}" for x in items))
    return _join_text(*parts)


def _firecrawl_raw(url: str, wait_ms: int = 0) -> str:
    """Fetch a page's raw HTML (or a JSON document's text) through Firecrawl.

    For hosts that refuse this crawler's own IP range outright: Tesla's
    Akamai and Citadel's edge answer 403 to every client shape from EC2.
    One credit per call, so callers fetch documents, not pages of results.
    """
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        raise RuntimeError("FIRECRAWL_API_KEY not set")
    r = post("https://api.firecrawl.dev/v2/scrape",
             headers={"Authorization": f"Bearer {key}",
                      "Content-Type": "application/json"},
             json={"url": url, "formats": ["rawHtml"], "onlyMainContent": False,
                   "waitFor": wait_ms, "maxAge": 0},
             timeout=180)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    status = (data.get("metadata") or {}).get("statusCode")
    if isinstance(status, int) and status >= 400:
        raise RuntimeError(f"firecrawl: {url} answered {status}")
    return data.get("rawHtml") or ""


def _firecrawl_json(url: str) -> dict:
    raw = _firecrawl_raw(url)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"firecrawl: no JSON document at {url}")
    return json.loads(html.unescape(raw[start:end + 1]))


def fetch_tesla(company: str, token: str) -> list[dict]:
    """tesla.com. token = site code ('US').

    Akamai refuses every non-browser client, from EC2 and a laptop alike,
    TLS impersonation included (2026-09-28), so the careers state document
    goes through Firecrawl: one 1.5 MB JSON listing every opening (8,292
    rows) with lookup tables for locations and job types and a geo tree of
    location ids per site. Rows of type 'intern' or with an intern-pattern
    title in one of the site's locations are kept. No dates, no text: the
    detail JSON is one more Firecrawl fetch per new row.
    """
    d = _firecrawl_json("https://www.tesla.com/cua-api/apps/careers/state")
    lookup = d.get("lookup") or {}
    loc_names = lookup.get("locations") or {}
    types = lookup.get("types") or {}
    site_locs: set[str] = set()
    for region in _dicts(d.get("geo")):
        for site in _dicts(region.get("sites")):
            if str(site.get("id") or "").upper() != token.upper():
                continue
            for st in _dicts(site.get("states")):
                for ids in (st.get("cities") or {}).values():
                    site_locs.update(str(i) for i in (ids or []))
    if not site_locs:
        raise ValueError(f"tesla: site {token} missing from the geo tree")
    out = []
    for j in _dicts(d.get("listings")):
        jid, title = str(j.get("id") or ""), j.get("t") or ""
        loc_id = str(j.get("l") or "")
        if not jid or loc_id not in site_locs:
            continue
        if (types.get(str(j.get("y"))) != "intern"
                and not prefilter.INTERN_PAT.search(title)):
            continue
        out.append({
            "company": company,
            "title": title,
            "location": loc_names.get(loc_id) or "",
            "url": f"https://www.tesla.com/careers/search/job/{jid}",
            "posted_at": None,
            "description": "",
            "source": "tesla",
            "detail_key": f"tesla||{jid}",
        })
    return out


_CITADEL_REGIONS = {
    "us": ("US", "United States"), "europe": ("Europe", "Europe"),
    "emea": ("EMEA", "Europe"), "asia": ("Asia", "Asia"),
    "apac": ("APAC", "Asia"), "australia": ("Australia", "Australia"),
    "canada": ("Canada", "Canada"), "uk": ("UK", "United Kingdom"),
    "london": ("London", "London, United Kingdom"),
    "hong-kong": ("Hong Kong", "Hong Kong"),
    "singapore": ("Singapore", "Singapore"),
}
_CITADEL_CASE = {"phd": "PhD", "ml": "ML", "ai": "AI", "fpga": "FPGA",
                 "ui": "UI", "dmm": "DMM", "it": "IT", "hr": "HR",
                 "and": "and", "of": "of", "a": "a", "the": "the"}


def _citadel_title(slug: str) -> tuple[str, str]:
    """('Quantitative Researcher PhD Intern (US)', 'United States') from
    'quantitative-researcher-phd-intern-us'."""
    words = slug.split("-")
    label, region = "", ""
    for n in (2, 1):
        key = "-".join(words[-n:])
        if len(words) > n and key in _CITADEL_REGIONS:
            label, region = _CITADEL_REGIONS[key]
            words = words[:-n]
            break
    title = " ".join(_CITADEL_CASE.get(w, w.capitalize()) for w in words if w)
    return (f"{title} ({label})" if label else title), region


def fetch_citadel(company: str, token: str) -> list[dict]:
    """citadel.com and citadelsecurities.com. token = the bare host.

    Both sites answer this crawler's IP range with 403 whatever the client
    looks like, and their listing pages paint cards from an ajax call that
    a fetch cannot follow, so the read goes through Firecrawl against the
    WordPress career sitemap: every open posting's URL, complete. The slug
    is the title (rebuilt here, regional suffix and all) and names the
    region, which is enough for the gates; the posting page - one Firecrawl
    fetch per new row - fills in the office list and the description.
    """
    xml = _firecrawl_raw(f"https://{token}/career-sitemap.xml")
    out, seen = [], set()
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", html.unescape(xml)):
        m = re.search(r"/careers/details/([a-z0-9-]+)/?$", loc)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        title, region = _citadel_title(m.group(1))
        out.append({
            "company": company,
            "title": title,
            "location": region,
            "url": loc,
            "posted_at": None,
            "description": "",
            "source": "citadel",
            "detail_key": f"citadel||{loc}",
        })
    return out


def _citadel_detail(page: str) -> tuple[str, str | None]:
    """(description, offices) from a citadel career page."""
    offices = None
    m = re.search(r"<h1[^>]*>.*?</h1>\s*<p>(.*?)</p>", page or "", re.S)
    if m:
        offices = _strip_html(m.group(1)).replace(",", ";")
    i = page.find("single-job-post-description")
    if i >= 0:
        i = page.find(">", i) + 1
    else:
        i = page.find("Job Description")
    text = ""
    if i >= 0:
        seg = page[i:i + 60000]
        end = seg.find('<div class="page-section')
        text = _strip_html(seg[:end] if end > 0 else seg)
        text = re.sub(r"^\s*Job Description\s*", "", text)
    return text.strip(), offices

ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "workday": fetch_workday,
    "workdaysite": fetch_workdaysite,
    "rippling": fetch_rippling,
    "gem": fetch_gem,
    "avature": fetch_avature,
    "simplify": fetch_simplify,
    "google": fetch_google,
    "icims": fetch_icims,
    "amazon": fetch_amazon,
    "eightfold": fetch_eightfold,
    "sndsh404": fetch_sndsh404,
    "hn": fetch_hn,
    "yc": fetch_yc,
    "atlassian": fetch_atlassian,
    "firecrawl": fetch_firecrawl,
    "oraclecloud": fetch_oraclecloud,
    "jibe": fetch_jibe,
    "apple": fetch_apple,
    "bytedance": fetch_bytedance,
    "goldman": fetch_goldman,
    "meta": fetch_meta,
    "tesla": fetch_tesla,
    "citadel": fetch_citadel,
    "sapsf": fetch_sapsf,
}

# Sources whose adapters support If-None-Match revalidation (verified live).
CONDITIONAL = {"greenhouse", "lever", "ashby", "simplify", "atlassian"}


def fetch(company: str, ats: str, token: str,
          etags: dict | None = None) -> list[dict]:
    if ats not in ADAPTERS:
        raise ValueError(f"unknown ats type: {ats}")
    if ats in CONDITIONAL and etags is not None:
        jobs = ADAPTERS[ats](company, token, etags=etags)
    else:
        jobs = ADAPTERS[ats](company, token)
    for j in jobs:
        # A null company_name in an aggregator feed row would otherwise flow
        # to job_id's .lower() and kill the whole run, not just the board.
        # Coercion only, no cleaning: company strings are part of the dedupe
        # hash, and rewriting stored ones would make old rows look new.
        j["company"] = j.get("company") or company
        j["title"] = _clean_title(j.get("title"))
        j["location"] = _clean_title(j.get("location"))
    return [j for j in jobs if j.get("title") and j.get("url")]
