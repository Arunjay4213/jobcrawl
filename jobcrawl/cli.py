from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as _dt
import json
import os
import re
import sys
import time as _time
from pathlib import Path

import yaml

from . import audit, notify, prefilter, publish, resolve, score, sources, watch
from .store import (AGGREGATOR_SOURCES, Store, job_id, row_id, url_key,
                    url_twin_groups)


def load_dotenv() -> None:
    """Read KEY=value pairs from .env into the environment.

    Keeps secrets out of both the repo and your shell profile, and means cron
    works without a wrapper script. A real environment variable always wins, so
    you can override a file value for one run. Not python-dotenv - this is
    twenty lines against a dependency, and the format here is trivial.
    """
    here = Path(__file__).resolve().parent
    for path in (here.parent / ".env", here / ".env"):
        if not path.is_file():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if val[:1] == val[-1:] and val[:1] in ("'", '"'):
                val = val[1:-1]
            os.environ.setdefault(key, val)


# Which command regenerates each optional include. Anything not listed here is
# hand-maintained, so its absence is silent rather than a nag.
SLOW_DERIVED_ATS = {"oraclecloud", "jibe"}

GENERATED_INCLUDES = {"boards_broad.yaml": "boards", "boards_vc.yaml": "boards",
                      "boards_yc.yaml": "boards", "boards_resolved.yaml": "boards",
                      "boards_cold.yaml": "boards"}


def load_config(path="config.yaml") -> dict:
    """Load config, merging `include:` files' companies into the main list.

    Include files (e.g. the auto-derived broad board list) only contribute
    `companies` entries. The curated list wins: an included board with the same
    (ats, token) as a main entry is dropped, so renaming or annotating a
    company in config.yaml never produces a duplicate.
    """
    cfg = yaml.safe_load(Path(path).read_text())
    companies = list(cfg.get("companies", []))
    # Case-insensitive: ATS tokens are case-preserving but not case-sensitive,
    # so 'Perplexity' and 'perplexity' are the same board and polling both
    # would put every posting in the digest twice under two company names.
    seen = {(c["ats"], str(c["token"]).lower()) for c in companies}
    for inc in cfg.get("include", []):
        inc_path = Path(path).parent / inc
        if not inc_path.is_file():
            # Derived files are regenerable, not required. Missing one should
            # cost coverage, not abort the run. Name the command that actually
            # builds THIS file - feeds_discovered.yaml legitimately does not
            # exist until some candidate clears the vetting bar, so pointing
            # at the wrong command turns a normal state into a false alarm.
            builder = GENERATED_INCLUDES.get(inc)
            if builder:
                print(f"  ! include {inc} missing; `jobcrawl {builder}` builds it")
            continue
        extra = yaml.safe_load(inc_path.read_text()) or {}
        # `companies:` with no entries parses as None, not [] - a freshly
        # seeded or emptied generated file must not abort every command.
        for c in extra.get("companies") or []:
            key = (c["ats"], str(c["token"]).lower())
            if key not in seen:
                seen.add(key)
                companies.append(c)
    cfg["companies"] = companies
    return cfg


def load_profile(path="profile.md") -> str:
    return Path(path).read_text()


def drop_covered_simplify(jobs: list[dict], companies: list[dict],
                          producing: set[str] | None = None) -> list[dict]:
    """Drop aggregator listings whose board we already poll directly.

    The direct fetch is fresher and formats titles/locations the way the ATS
    does, so letting both through would put near-duplicates past the dedupe
    hash and into the digest.

    "Poll directly" has to mean the direct fetch is actually producing:
    `producing` is the set of lowercased board keys whose last fetch
    succeeded with postings (`Store.producing_boards`). A configured board
    that 404s, or that answers with zero postings run after run, covers
    nothing - and dropping the feed's copy on its account is how Atlassian's
    2027 SWE intern vanished (cold icims board at empty_streak 50, 2026-09-02).
    None keeps every configured board covering, for callers without a store.
    """
    covered = {(c["ats"], str(c["token"]).lower()) for c in companies
               if producing is None
               or f"{c['ats']}:{c['token']}".lower() in producing}
    have_google = any(c["ats"] == "google" for c in companies)
    out = []
    for j in jobs:
        if j.get("source") in AGGREGATOR_SOURCES:
            board = sources.derive_board(j.get("url", ""))
            if board and (board[0], board[1].lower()) in covered:
                continue
            # Google URLs don't derive to a board (the token is a search
            # query), but the direct adapter formats titles differently, so
            # aggregator copies would slip past dedupe and double-list.
            if have_google and "google.com/about/careers" in j.get("url", ""):
                continue
        out.append(j)
    return out


# The `why` of a row the post-enrich clearance gate stored skipped. A
# rescore that ungates such a row but scores it below the threshold keeps
# this prefix, so the row stays recoverable by the next `rescore --ids`.
CLEARANCE_WHY = "requires security clearance"


def split_cleared(jobs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition enriched postings into (kept, clearance_gated).

    The prefilter's description clearance check runs before enrichment, and
    the detail-enriched sources (workday, smartrecruiters, workable, rippling,
    gem) have no description at all at that point - so for them the check
    could never fire, and defense boards like RTX flooded the tracker. Re-run
    the same deliberately-conservative pattern (requirement wording only;
    "clearance is a plus" survives) now that the text exists. Rows without a
    description are always kept: absence of evidence is not a drop reason.
    """
    kept, cleared = [], []
    for j in jobs:
        if prefilter.clearance_gated(j.get("description")):
            cleared.append(j)
        else:
            kept.append(j)
    return kept, cleared


def split_stale(jobs: list[dict], max_age_days: int | None,
                cycles: set | None = None) -> tuple[list[dict], list[dict]]:
    """Partition never-before-seen postings into (fresh, stale) by posted date.

    Stale means the posting has been sitting on the public GitHub lists past
    the cutoff, so it has been seen and applied to already and surfacing it now
    would only add noise. The caller records stale ones as seen-and-suppressed
    rather than dropping them, so they stay suppressed on every later run too.
    Undated postings count as fresh: better to score a maybe than to silently
    bury it.

    The age test applies ONLY to the aggregator feeds. On a directly polled
    board, age says nothing about whether a posting is worth seeing: a company
    that opened its Summer 2027 internship in March still has it open in July,
    and the first crawl of any board is a backfill of everything it holds, not
    a stream of new arrivals. Ageing those out buried hundreds of live
    postings and was the single biggest gap against the public lists.

    Given the target `cycles`, a listing whose season tag the cycle gate had
    to set aside (stale or contradicted by the title, prefilter.tag_misfiled)
    is fresh too: it sat on the list under the wrong season, so the "seen
    already" premise does not hold. Without this, the fix that stopped
    dropping those listings would have filed its 33 older recoveries on the
    2026-09-25 feed as skipped/stale forever, beyond `unstale`'s reach.
    """
    if not max_age_days:
        return jobs, []
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=max_age_days)
    fresh, stale = [], []
    for j in jobs:
        if (j.get("source") not in AGGREGATOR_SOURCES
                or (cycles and prefilter.tag_misfiled(j, cycles))):
            fresh.append(j)
            continue
        # Freshness is the newer of posted and updated: Simplify bumps
        # date_updated when it reactivates a listing, and a reopened role
        # must not be buried just because its original date is old.
        ts = None
        for field in ("posted_at", "updated_at"):
            if not j.get(field):
                continue
            try:
                t = _dt.datetime.fromisoformat(str(j[field]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=_dt.timezone.utc)
            ts = max(ts, t) if ts else t
        (stale if ts and ts < cutoff else fresh).append(j)
    return fresh, stale


# Sources whose list responses are complete (no pagination caps, no flaky
# extraction), so a posting's absence there really means it is gone. Workday
# is capped at 200 results per search, so absence can be truncation;
# SmartRecruiters now paginates past its 100-per-page limit but stays out
# until that has proven itself; Firecrawl extraction just fails some days.
CLOSABLE_SOURCES = {"greenhouse", "lever", "ashby", "workable", "rippling", "gem", "simplify", "yc",
                    "atlassian",
                    # Complete documents, not searches: Apple's team listing
                    # paged to its end, Goldman's campus list, Tesla's whole
                    # state file, Citadel's sitemap. Meta, ByteDance, Oracle
                    # and Jibe are keyword searches and stay out.
                    "apple", "goldman", "tesla", "citadel"}


# Cold-tier boards on these ATSes cost ~20 throttled POSTs each instead of one
# conditional GET, so they draw from their own, much smaller per-run budget.
EXPENSIVE_ATS = {"workday", "workdaysite"}


def board_family(key: str) -> str:
    """The infrastructure a board shares failures with.

    Workday tenants live on numbered data centers (wd1, wd3, wd5...) that go
    down independently: wd3 answered non-JSON for seven hours on 2026-09-05
    while wd1 and wd5 were fine. Everything else fails per ATS.
    """
    ats, _, token = key.partition(":")
    if ats in EXPENSIVE_ATS:
        parts = token.split("|")
        if len(parts) >= 2:
            return f"{ats} wd{parts[1]}"
    return ats


def error_class(err: str | None) -> str:
    """'HTTPError: 404 Client Error...' -> 'HTTPError: 404'; keeps just enough
    to tell one failure mode from another without the per-board URL tail."""
    err = (err or "").strip()
    head, _, rest = err.partition(":")
    m = re.match(r"\s*(\d{3})\b", rest)
    return f"{head}: {m.group(1)}" if m else head


_ASHBY_GONE = "no jobPostings in appData"


def gone_cause(key: str, err: str | None) -> str | None:
    """A short label when the platform itself says this board no longer
    exists, None when the failure could still be transient.

    Rot and outage look the same from a fail streak, but the platforms tell
    them apart in the response, and the difference is a week of digest noise:
    a board that is gone gets forty identical warnings on its way to the
    weekly probe. Verified live, 2026-09-08:
    - 404/410 from any ATS API - the token no longer resolves. Greenhouse
      purged Evolver's and Haize Labs' boards in the same minute.
    - Workday 422 - the TENANT is decommissioned (an unknown site on a live
      tenant is a 404). Dell's answered 422 after it moved to Oracle HCM.
    - Ashby's hosted page with no jobPostings in its app data - the
      organization is null, i.e. the slug was deleted or renamed; a live
      board with no openings still carries an empty list.
    """
    ats = key.partition(":")[0]
    cls = error_class(err)
    if cls.endswith((" 404", " 410")):
        return f"{ats} {cls[-3:]}"
    if ats in EXPENSIVE_ATS and cls.endswith(" 422"):
        return "workday tenant decommissioned"
    if ats == "ashby" and _ASHBY_GONE in (err or ""):
        return "ashby org removed"
    return None


def retired_boards(store: Store, dead_after: int, retire_after: int) -> dict:
    """key -> health row, for boards on the weekly probe: failing
    retire_after runs straight, or dead_after runs straight with the
    platform saying the board is gone (gone_cause)."""
    if not (dead_after or retire_after):
        return {}
    out = {}
    for key, row in store.dead_boards(min(x for x in (dead_after, retire_after) if x)).items():
        if ((retire_after and row["fail_streak"] >= retire_after)
                or (dead_after and row["fail_streak"] >= dead_after
                    and gone_cause(key, row["last_error"]))):
            out[key] = row
    return out


def detect_outages(outcomes: list[dict], min_boards: int = 5,
                   min_share: float = 0.5) -> dict[str, str]:
    """key -> outage label, for failures that are a platform's, not a board's.

    When most of a family's boards fail the same way in one run, the family
    is down, and counting that against each board is wrong twice over: the
    digest fills with dozens of identical lines (81 on 2026-09-05, 75 of
    them one Workday data center), and after `dead_board_fails` runs every
    healthy board in the family is demoted to the slow probe, so coverage
    stays lost for hours after the platform recovers. Both thresholds must
    hold - a big family with five stale tokens is five dead boards, not an
    outage, and three boards all failing is too small a sample to call.
    """
    attempts: dict[str, int] = {}
    failures: dict[tuple[str, str], list[str]] = {}
    for o in outcomes:
        fam = board_family(o["key"])
        attempts[fam] = attempts.get(fam, 0) + 1
        if o["status"] == "error":
            failures.setdefault((fam, error_class(o.get("error"))), []).append(o["key"])
    out: dict[str, str] = {}
    for (fam, cls), keys in failures.items():
        if len(keys) >= min_boards and len(keys) >= min_share * attempts[fam]:
            label = f"{fam}: {len(keys)}/{attempts[fam]} boards failed ({cls})"
            for k in keys:
                out[k] = label
    return out

# Boards a long way in the past have no last_try at all; sort them first.
_NEVER = _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)


def due_boards(companies: list[dict], store: Store, cfg: dict,
               now=None) -> tuple[list[dict], int]:
    """Drop boards that were polled too recently to be worth polling again.

    Only metered or explicitly-throttled boards have an interval; the free API
    boards stay hourly, which is where the speed advantage lives. A board that
    is skipped produces no outcome, so closure tracking already ignores its
    postings this run and its health streaks freeze - skipping is invisible to
    the rest of the pipeline by construction.

    Long-dead boards drop to a daily retry on the same mechanism. The broad
    list is regenerated from public GitHub lists, so tokens that 404 keep
    coming back; retrying one hourly forever buys nothing and buries the
    warnings that matter. A daily probe still catches a board that returns.

    Cold boards (`cold: true`, mined from historical listings) get both a slow
    clock AND a hard per-run budget. The clock alone is not enough: on the
    first run every cold board is equally overdue, so a thousand of them come
    due in the same breath and a run that takes five minutes takes an hour.
    The budget picks the least-recently-tried ones, which staggers the tier
    for good after one sweep. A cold board that has recently produced a new
    posting is promoted to the normal clock and stops drawing from the budget.
    """
    default_fc = cfg.get("filters", {}).get("firecrawl_every_hours", 0)
    f = cfg.get("filters", {})
    dead_after = f.get("dead_board_fails", 12)
    dead_every = f.get("dead_board_retry_hours", 24)
    retire_after = f.get("retire_board_fails", 40)
    retire_every = f.get("retire_board_retry_hours", 168)
    cold_hours = f.get("cold_every_hours", 8)
    # 0 is the master switch for the whole cold tier, expensive budget
    # included: a cold board file can be committed without the next scheduled
    # run immediately backfilling thousands of boards into the tracker.
    cold_budget = f.get("cold_boards_per_run", 0)
    cold_expensive = f.get("cold_workday_per_run", 5)
    promote_days = f.get("cold_promote_days", 30)
    last = store.last_ok_map()
    tries = store.last_try_map()
    yields = store.yield_map() if any(c.get("cold") for c in companies) else {}
    dead = store.dead_boards(dead_after) if dead_after else {}
    # Dead for days, not hours: the 4h probe found nothing ~30 times in a
    # row, so the board is gone (moved ATS, closed, token rotted). It keeps
    # a weekly probe - one request - and stops costing anything else. A
    # board the platform itself reports gone (404, decommissioned tenant)
    # skips the days and retires the moment it counts as dead.
    retired = retired_boards(store, dead_after, retire_after)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    due, cold, skipped = [], [], 0
    for c in companies:
        key = f"{c['ats']}:{c['token']}"
        hours = c.get("every_hours")
        if hours is None:
            hours = default_fc if c["ats"] == "firecrawl" else 0
        is_cold = bool(c.get("cold"))
        if is_cold:
            y = yields.get(key.lower())
            if y and (now - y) < _dt.timedelta(days=promote_days):
                is_cold = False
        if is_cold:
            if not cold_budget:
                skipped += 1
                continue
            hours = max(hours, cold_hours)
        ts = last.get(key)
        # Metered boards pace off the last ATTEMPT, not the last success: a
        # failing Firecrawl board has no recent success, and pacing off
        # last_ok would put it back on the every-run clock precisely while
        # each retry still costs money and fails.
        if hours:
            t2 = tries.get(key)
            if t2 and (ts is None or t2 > ts):
                ts = t2
        if key in retired:
            hours = max(hours, retire_every)
            ts = _parse_ts(retired[key]["last_try"]) or ts
        elif key in dead:
            hours = max(hours, dead_every)
            # A dead board has no recent success to pace against, so pace off
            # the last attempt instead - otherwise it retries every run.
            ts = _parse_ts(dead[key]["last_try"]) or ts
        if hours and ts and (now - ts) < _dt.timedelta(hours=hours):
            skipped += 1
            continue
        if is_cold:
            cold.append((tries.get(key) or _NEVER, c))
        else:
            due.append(c)

    # Least-recently-tried first, never-tried before that, so a fresh cold
    # file drains in file order and later sweeps follow the natural rotation.
    cold.sort(key=lambda tc: tc[0])
    spent, spent_expensive = 0, 0
    for _, c in cold:
        if c["ats"] in EXPENSIVE_ATS:
            if spent_expensive >= cold_expensive:
                skipped += 1
                continue
            spent_expensive += 1
        else:
            if spent >= cold_budget:
                skipped += 1
                continue
            spent += 1
        due.append(c)
    return due, skipped


def retry_board_keys(jobs: list[dict], companies: list[dict],
                     outcomes: list[dict]) -> list[str]:
    """Board keys whose etags must go so these rows are fetched again next run.

    A board row is matched to this run's fetch outcomes by (ats, company
    name) - the pair every adapter stamps on its rows - with derive_board on
    the URL as the fallback (a vanity-domain Greenhouse link derives to
    nothing, but its outcome row is right there). Two boards sharing a name
    both forget their etag, which costs one unconditional fetch each. An
    aggregator row carries no pointer to the feed it came from, so any such
    row names every configured aggregator feed. Boards without conditional
    fetches (Workday, iCIMS, ...) are named too - clearing a NULL etag is a
    no-op, and the caller need not know which adapters use etags.
    """
    by_board: dict[tuple[str, str], list[str]] = {}
    for o in outcomes:
        ats = o["key"].split(":", 1)[0]
        by_board.setdefault((ats, (o.get("company") or "").lower()), []).append(o["key"])
    out = set()
    for j in jobs:
        if j.get("source") in AGGREGATOR_SOURCES:
            out.update(f"{c['ats']}:{c['token']}" for c in companies
                       if c.get("ats") in AGGREGATOR_SOURCES)
            continue
        keys = by_board.get((j.get("source") or "", (j.get("company") or "").lower()))
        if keys:
            out.update(keys)
            continue
        b = sources.derive_board(j.get("url") or "")
        if b:
            out.add(f"{b[0]}:{b[1]}")
    return sorted(out)


def board_yield_keys(jobs: list[dict], prior_ok: set[str]) -> list[str]:
    """Lowercased board keys that produced a genuinely new posting this run.

    This is the promotion signal for the cold tier, and the `prior_ok` guard
    is what keeps it meaningful. Directly polled boards are exempt from the
    age cutoff, so a board's FIRST successful crawl backfills its whole
    contents as brand-new rows - every cold board with anything open at all
    would promote on contact and the tier would evaporate into the run-time
    explosion it exists to prevent. Only boards that had already fetched
    successfully before this run count: backfill is not activity.

    Aggregator rows are excluded - they are a feed's yield, not a board's -
    and a URL that does not derive to a board simply never promotes, which
    costs nothing but speed on those few sources.
    """
    out = set()
    for j in jobs:
        if j.get("source") in AGGREGATOR_SOURCES:
            continue
        b = sources.derive_board(j.get("url") or "")
        if not b:
            continue
        key = f"{b[0]}:{b[1]}".lower()
        if key in prior_ok:
            out.add(key)
    return sorted(out)


def _parse_ts(raw) -> _dt.datetime | None:
    if not raw:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=_dt.timezone.utc)


def fetch_all(companies: list[dict], etags: dict | None = None,
              workers: int = 32) -> tuple[list[dict], list[dict]]:
    """Fetch every board; return (jobs, per-board outcomes).

    The pool has to be wider than the sum of the busy workday clusters'
    ceilings in sources._HOST_POLICY (6 per wdN cluster, ~5 busy clusters):
    workers parked on those semaphores are the run's critical path, and with
    16 workers only 4 were left for every other ATS.
    Outcome status is 'ok' (fresh data), 'unchanged' (ETag 304), or 'error'.
    The distinction matters downstream: closure tracking must only trust
    boards that returned fresh, complete data this run.
    """
    jobs, outcomes = [], []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(sources.fetch, c["name"], c["ats"], c["token"],
                             etags): c
                   for c in companies}
        for f in cf.as_completed(futures):
            c = futures[f]
            o = {"company": c["name"], "ats": c["ats"],
                 "key": f"{c['ats']}:{c['token']}"}
            try:
                res = f.result()
                jobs.extend(res)
                o.update(status="ok", count=len(res))
            except sources.Unchanged:
                o.update(status="unchanged", count=0)
            except Exception as e:
                o.update(status="error", count=0,
                         error=f"{type(e).__name__}: {str(e)[:80]}")
            outcomes.append(o)
    return jobs, outcomes


def close_gone_postings(store: Store, jobs: list[dict], outcomes: list[dict],
                        close_after: int, min_score: int = 65) -> list[str]:
    """Mark stored postings closed once their board stops listing them.

    A row is only eligible when its source is reliable (CLOSABLE_SOURCES) and
    ITS board - identified by (ats, token) derived from the row's URL, never
    by company display name, which two boards can share - returned fresh,
    non-empty data this run. Errors, 304s, and zero-posting responses freeze
    the count instead of growing it, so a flaky board can never close
    anything. Presence is dual-identity, hash or URL, mirroring filter_new:
    a title edit changes the hash but not the URL, and closing a live posting
    over a retitle would be unrecoverable (URL dedupe blocks reinsertion).
    Checked against the raw fetch, before prefilter and simplify-overlap
    dropping, because a posting can be alive on its board while invisible to
    the pipeline's filters.
    """
    present_ids = {job_id(j) for j in jobs}
    present_urls = {url_key(j["url"]) for j in jobs
                    if j.get("url") and j.get("source") != "firecrawl"}
    # Positive sightings first: a closed row seen again on any board was
    # closed by a transient glitch and comes back to life (see
    # store.reopen_present). Runs before the eligibility pass so a reopened
    # row immediately resumes normal presence tracking below.
    reopened = store.reopen_present(present_ids, present_urls, min_score)
    ok_keys = {o["key"].lower() for o in outcomes
               if o["status"] == "ok" and o["count"] > 0}
    ok_names = {(o["ats"], o["company"].lower()) for o in outcomes
                if o["status"] == "ok" and o["count"] > 0}
    # Rows record source='simplify' but not WHICH feed listed them, so with
    # several feeds configured, closure needs every feed fresh and non-empty
    # this run - one feed 304ing while another changes must freeze, not close.
    simp = [o for o in outcomes if o["ats"] == "simplify"]
    simplify_ok = bool(simp) and all(o["status"] == "ok" and o["count"] > 0
                                     for o in simp)

    eligible, present = set(), set()
    for r in store.live_rows():
        src = r["source"]
        if src not in CLOSABLE_SOURCES:
            continue
        if src == "simplify":
            ok = simplify_ok
        else:
            board = sources.derive_board(r["url"] or "")
            if board:
                ok = f"{board[0]}:{board[1]}".lower() in ok_keys
            else:
                # Underivable URL (e.g. Workable shortlinks): fall back to the
                # display name, case-insensitively. Ambiguity here fails open
                # to "never closed", which only costs tracker tidiness.
                ok = (src, (r["company"] or "").lower()) in ok_names
        if not ok:
            continue
        eligible.add(r["id"])
        if r["id"] in present_ids or (r["url"] and url_key(r["url"]) in present_urls):
            present.add(r["id"])
    return store.update_presence(eligible, present, close_after), reopened


def board_warnings(store: Store, companies: list[dict] | None = None,
                   filters: dict | None = None) -> list[str]:
    """Human-readable alerts for boards that look broken.

    Surfaced in the digest because that is the one place actually read daily -
    a board that silently 404s for a month is a company quietly missing from
    the feed.

    Health rows outlive the config: the broad list is regenerated every week,
    so a board dropped from it leaves its last failure behind and would warn
    forever about something nobody polls. Pass the current board list to keep
    the warnings to boards that actually still run.

    The list is kept short by construction, because 81 lines is the same as
    none - nobody reads them. A platform outage last run is one line. Boards
    failing the same way on the same infrastructure collapse into one line
    per cause. Boards dead long enough to be retired to the weekly probe are
    named once, in the week they cross the line, and then drop out.
    """
    f = filters or {}
    min_boards = f.get("outage_min_boards", 5)
    dead_after = f.get("dead_board_fails", 12)
    retire_after = f.get("retire_board_fails", 40)
    out = []
    try:
        for label in json.loads(store.get_meta("last_outages") or "[]"):
            out.append(f"Platform outage last run - {label}; those boards' "
                       f"health is frozen, not failing")
    except ValueError:
        pass
    live = ({f"{c['ats']}:{c['token']}" for c in companies}
            if companies is not None else None)
    failing, retired, gone = [], [], []
    for b in store.problem_boards():
        if live is not None and b["key"] not in live:
            continue
        # A board that has NEVER fetched successfully is a speculative token
        # (cold-tier mining, broad derivation) that was dead on arrival -
        # nothing was lost, so nothing warns. Without this, the cold tier's
        # rot parades through the digest for days as its rotation discovers
        # each dead token (nine-warning digest, 2026-08-25). A board that
        # WORKED and then broke is lost coverage and still warns.
        if not b["last_ok"]:
            continue
        cause = gone_cause(b["key"], b["last_error"])
        # Named in the week it crosses the line (the weekly probe adds one
        # to the streak), then silent: a board gone for good must not be a
        # permanent fixture of every digest.
        if dead_after and cause and b["fail_streak"] >= dead_after:
            if b["fail_streak"] <= dead_after + 1:
                gone.append(f"{b['name']} ({cause})")
        elif retire_after and b["fail_streak"] >= retire_after:
            if b["fail_streak"] <= retire_after + 1:
                retired.append(b["name"])
        elif b["fail_streak"] >= 3:
            failing.append(b)
        elif b["empty_streak"] >= 2 and b["key"].startswith("firecrawl:"):
            out.append(f"{b['name']}: 0 postings for {b['empty_streak']} runs - "
                       f"Firecrawl extraction may be failing silently")
    groups: dict[tuple[str, str], list] = {}
    for b in failing:
        groups.setdefault((board_family(b["key"]), error_class(b["last_error"])),
                          []).append(b)
    for (fam, cls), bs in groups.items():
        if len(bs) >= min_boards:
            streaks = sorted(b["fail_streak"] for b in bs)
            span = (f"{streaks[0]} runs" if streaks[0] == streaks[-1]
                    else f"{streaks[0]}-{streaks[-1]} runs")
            out.append(f"{fam}: {len(bs)} boards failing {span} ({cls}) - one "
                       f"shared cause, almost certainly platform-side")
            continue
        for b in bs:
            err = (b["last_error"] or "").strip()
            out.append(f"{b['name']}: fetch failing {b['fail_streak']} runs"
                       + (f" ({err[:60]})" if err else ""))
    if gone:
        # "Once" has to be remembered, not inferred from the streak: a
        # retired board is probed weekly, so its streak sat inside the
        # one-line window for a week and the same 16 names rode every
        # digest of the day (2026-09-29).
        try:
            named = set(json.loads(store.get_meta("gone_named") or "[]"))
        except ValueError:
            named = set()
        fresh = sorted(set(gone) - named)
        if fresh:
            store.set_meta("gone_named", json.dumps(sorted(named | set(fresh))))
            out.append(f"Gone from its platform, retired to a weekly probe "
                       f"({len(fresh)}): " + ", ".join(fresh))
    if retired:
        names = sorted(set(retired))
        out.append(f"Retired to a weekly probe after {retire_after}+ failed runs "
                   f"({len(names)}): " + ", ".join(names))
    return out


def cmd_verify(args):
    """Check every configured board actually resolves. Run this first."""
    cfg = load_config(args.config)
    ok = bad = skipped = 0
    have_firecrawl = bool(os.environ.get("FIRECRAWL_API_KEY"))
    for c in cfg["companies"]:
        if c["ats"] == "firecrawl" and not have_firecrawl:
            print(f"  SKIP  {c['name']:<24} needs FIRECRAWL_API_KEY")
            skipped += 1
            continue
        try:
            res = sources.fetch(c["name"], c["ats"], c["token"])
            interns = sum(1 for j in res if prefilter.INTERN_PAT.search(j["title"]))
            print(f"  OK    {c['name']:<24} {len(res):>4} postings ({interns} intern-ish)")
            ok += 1
        except Exception as e:
            print(f"  FAIL  {c['name']:<24} {type(e).__name__}: {str(e)[:70]}")
            bad += 1
    tail = f", {skipped} skipped" if skipped else ""
    print(f"\n{ok} working, {bad} broken{tail}. "
          f"Fix or remove the broken ones in {args.config}.")


def email_due(store: Store, hold_minutes: int, now=None) -> bool:
    """Is the batched-email window open?

    With `filters.email_batch_minutes` set, at most one digest email goes out
    per window; postings found in between wait for the next one. 0 or unset
    means every run emails immediately (the pre-batching behavior).
    """
    if not hold_minutes:
        return True
    last = _parse_ts(store.get_meta("last_email_at"))
    if not last:
        return True
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return (now - last) >= _dt.timedelta(minutes=hold_minutes)


def scorer_effort(cfg: dict) -> str:
    """The scorer's reasoning effort: `filters.scorer_effort`, else the default."""
    return cfg.get("filters", {}).get("scorer_effort") or score.DEFAULT_EFFORT


def scoring_outage(store: Store, cfg: dict, failed: list, total: int,
                   now=None) -> str | None:
    """Notice a scorer that fails wholesale, and email once it persists.

    Failed rows are left unstored to retry next run, which is right for a
    transient failure and invisible for a permanent one: with a dead API
    key every batch fails, nothing is ever pending, and no digest - warning
    or otherwise - ever goes out. The key was revoked on 2026-09-26 and the
    crawl ran silent for two days (61 postings looping through the retry).
    Counts consecutive runs where EVERY scored row failed
    (`meta.scoring_fail_streak`, reset by any run with a real score), and
    once the streak reaches `filters.scoring_alert_after` runs emails an
    alert, repeated every `filters.scoring_alert_hours` while it lasts.
    Returns the warning line for the digest once the streak counts, else
    None. Email stays best-effort like the digest's.
    """
    f = cfg.get("filters", {})
    if not total or len(failed) < total:
        if store.get_meta("scoring_fail_streak") not in (None, "0"):
            store.set_meta("scoring_fail_streak", "0")
        return None
    streak = int(store.get_meta("scoring_fail_streak") or 0) + 1
    store.set_meta("scoring_fail_streak", str(streak))
    if streak < f.get("scoring_alert_after", 2):
        return None
    errors = [j.get("error") or "unknown error" for j in failed]
    error = max(set(errors), key=errors.count)
    warning = (f"Scoring is down: all {total} rows failed in each of the "
               f"last {streak} runs ({error}). Nothing is scored or "
               f"emailed until this is fixed.")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    last = _parse_ts(store.get_meta("last_scoring_alert_at"))
    every = _dt.timedelta(hours=f.get("scoring_alert_hours", 6))
    if last and now - last < every:
        return warning
    body = (f"# jobcrawl scoring outage\n\n{warning}\n\n"
            f"A 401 means the scorer's API key in jobcrawl/.env is "
            f"invalid; anything else, check the journal on the VM.\n")
    try:
        if notify.send_email(body, "jobcrawl: scoring is down"):
            store.set_meta("last_scoring_alert_at", now.isoformat())
    except Exception as e:
        print(f"  ! outage alert email failed: {type(e).__name__}: "
              f"{str(e)[:80]}")
    return warning


def send_pending_digest(store: Store, cfg: dict, warnings=None) -> list:
    """Render, write, and email everything unnotified, honoring the batch window.

    Returns the rows emailed (and marked notified); [] when nothing was
    pending or the window is still closed. Email stays best-effort: rows are
    already stored, and an SMTP hiccup that crashed here would re-score the
    same postings every run until it healed.
    """
    pending = store.unnotified()
    # The email has its own floor above the surfacing one: a 20-24 row still
    # goes on the tracker (store.surfaced ignores `notified`) but is marked
    # seen here without a send, so the inbox carries only rows worth a look
    # (the user set 25 on 2026-09-28 after a digest of FOX News variants).
    # NULL-score list rows have nothing to compare and keep emailing.
    floor = cfg.get("filters", {}).get("email_min_score")
    if floor is not None:
        quiet = [r for r in pending
                 if r["score"] is not None and r["score"] < floor]
        if quiet:
            store.mark_notified([r["id"] for r in quiet])
            print(f"  {len(quiet)} below the email floor {floor}: tracker only")
            pending = [r for r in pending if r["score"] is None
                       or r["score"] >= floor]
    if not pending:
        return []
    hold = cfg.get("filters", {}).get("email_batch_minutes", 0)
    # A strong match must never wait out the batch window: the Annapurna Labs
    # SDE posting (score 85, 2026-08-27) sat 20+ minutes behind two closed
    # windows while the user found it elsewhere. Batching exists to bundle
    # the near-threshold trickle, not to delay the postings that are the
    # whole point of source polling. Sending still stamps last_email_at, so
    # an urgent send re-times the window rather than stacking extra emails.
    f = cfg.get("filters", {})
    urgent = f.get("email_urgent_score")
    # Company names vary by board ("Amazon" vs "Amazon Web Services",
    # "Hudson River (GH)"), so match on whole-word prefix, the same idea
    # publish.group_by_company_daily uses - "Meta" hits "Meta Platforms"
    # but not "Metagenomi". NULL-score list rows qualify too: the companies
    # on this list that resist direct polling only ever arrive as list rows.
    urgent_cos = [c.strip().lower()
                  for c in (f.get("email_urgent_companies") or []) if c]

    def _urgent_row(r) -> bool:
        if urgent is not None and (r["score"] or 0) >= urgent:
            return True
        name = (r["company"] or "").lower()
        return any(name == c or name.startswith(c + " ") for c in urgent_cos)

    urgent_hit = any(_urgent_row(r) for r in pending)
    if not urgent_hit and not email_due(store, hold):
        print(f"  {len(pending)} pending held for the batched digest "
              f"(at most one email per {hold} min)")
        return []
    # Uncapped on purpose (2026-08-25): the user wants every posting in the
    # email, never a "...and 69 more, see jobcrawl list" stub. An over-long
    # digest splits into multiple emails downstream (notify.split_email).
    md = notify.render(pending, max_per_cycle=0, max_total=0,
                       warnings=warnings or [])
    path = notify.write_file(md)
    sent, broke = False, False
    try:
        sent = notify.send_email(md, f"{len(pending)} new internship postings")
    except Exception as e:
        broke = True
        print(f"  ! email failed: {type(e).__name__}: {str(e)[:80]}")
    if sent:
        store.set_meta("last_email_at",
                       _dt.datetime.now(_dt.timezone.utc).isoformat())
    # notified=1 means "the user has seen this", and it is a one-way door: a
    # marked row never enters another digest. Only claim it when the mail
    # actually went out, or when there is no mailbox configured to send to
    # (send_email returns False without raising) - there the digest file on
    # disk IS the delivery. An SMTP outage must leave rows queued instead,
    # because for a reader who only checks email a consumed row is a lost
    # posting, and nothing downstream would ever say so. Requeuing is free:
    # the rows are already stored and scored, so a retry costs no LLM spend,
    # only a re-render.
    if broke:
        print(f"  {len(pending)} postings stay queued for the next run")
    else:
        store.mark_notified([r["id"] for r in pending])
    print(f"\nDigest: {path}" + ("  (emailed)" if sent else
                                 "  (send failed, queued)" if broke else
                                 "  (no SMTP configured)"))
    for r in pending[:5]:
        shown = r['score'] if r['score'] is not None else '-'
        print(f"  {shown:>3}  {r['company']} — {r['title']}")
    return pending


def _same_office(a: str | None, b: str | None) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def _fill_offices(rows: list[dict], store: Store, cap: int) -> int:
    """Fill vague rows' offices from detail_cache, else their detail page.

    Mutates the rows in place and remembers each page's answer by url_key
    ('' when the page names no office). A call that raised is not
    remembered, so it is retried next run. Returns the detail calls made.
    """
    cached = store.cached_locations(url_key(j.get("url") or "") for j in rows)
    todo = []
    for j in rows:
        k = url_key(j.get("url") or "")
        if k in cached:
            if cached[k]:
                j["location"] = cached[k]
        elif sources.needs_detail(j):
            todo.append(j)
    todo = todo[:max(0, cap)]
    if todo:
        sources.enrich(todo, cap=len(todo))
        store.cache_locations({
            url_key(j.get("url") or ""):
                "" if prefilter.vague_location(j.get("location")) else j["location"]
            for j in todo if j.get("_detail") == "ok"})
    return len(todo)


def enrich_vague(kept: list[dict], store: Store,
                 f: dict) -> tuple[list[dict], int]:
    """Give unseen rows with no office their real one before job_id hashes them.

    job_id hashes company|title|location, so a row that arrives with no
    office (Google's search page, iCIMS tenants whose cards omit it,
    Workday's "N Locations") cannot tell regional requisitions of one title
    apart: Google's Taipei and US copies of "Software Engineering Intern,
    PhD" shared one id, whichever card came first was the only one stored,
    and the US posting was lost (2026-09-25). Only rows whose url_key is
    unseen qualify - a stored row keeps matching by url identity.

    Four follow-ons keep the pass from undoing the dedupe around it:
    - A url whose row filter_new then drops (a same-office sibling, an
      Avature page with no office) never reaches jobs.url, so its answer is
      kept in detail_cache and reused instead of re-fetched every poll.
    - Rows stored before this pass were hashed from the EMPTY location. A
      row whose empty-location hash is such a stored row AND whose office
      equals the stored row's office is that row's requisition (a repost,
      a new URL dressing, a same-office sibling): `_legacy_id` keeps it on
      the stored row, exactly as before, instead of inserting a duplicate
      that non-closable Google/Workday/iCIMS boards would never retire.
      Not when the stored row's own url is live in this fetch and its page
      names a DIFFERENT office: the old sibling-repost bug rewrote such rows'
      urls to another office's requisition without touching the location
      (HNTB's Baton Rouge row held a South Portland url), and linking to
      one would hide the real posting for its office.
    - A row whose offices are ALL foreign goes through the free US gate
      again (`prefilter.location_ok`, office by office: `_some_office_ok`),
      instead of reaching the paid scorer on its `us` flag; a mixed or
      ambiguous list still goes to the scorer. Only that gate: the
      description clearance gate is split_cleared's, which records the row
      instead of dropping it unseen.
    - Detail calls are capped at sources.ENRICH_CAP, shared with the main
      enrich pass; the count used is returned.
    """
    known = store.known_urls()
    vague = [j for j in kept
             if j.get("source") != "firecrawl"
             and prefilter.vague_location(j.get("location"))
             and sources.needs_detail(j)
             and url_key(j.get("url") or "") not in known]
    if not vague:
        return kept, 0
    pre = {id(j): job_id(j) for j in vague}
    calls = _fill_offices(vague, store, sources.ENRICH_CAP)

    moved = [j for j in vague if not prefilter.vague_location(j.get("location"))]
    stored = store.rows_of({pre[id(j)] for j in moved}
                           | {job_id(j) for j in moved})
    live: dict[str, dict] = {}
    for j in kept:
        k = url_key(j.get("url") or "")
        if k:
            live.setdefault(k, j)
    links, probes = [], {}
    for j in moved:
        old = pre[id(j)]
        r = stored.get(old)
        if (r is None or job_id(j) in stored
                or not _same_office(r["location"], j.get("location"))):
            continue
        twin = live.get(url_key(r["url"] or ""))
        links.append((j, old, twin))
        if twin is not None and prefilter.vague_location(twin.get("location")):
            # A copy: the live stored row must keep its fetched fields.
            probes.setdefault(id(twin), {**twin})
    calls += _fill_offices(list(probes.values()), store,
                           sources.ENRICH_CAP - calls)
    for j, old, twin in links:
        if twin is not None:
            office = probes.get(id(twin), twin).get("location")
            if (not prefilter.vague_location(office)
                    and not _same_office(office, j.get("location"))):
                continue
        j["_legacy_id"] = old

    recheck = {id(j) for j in moved}
    out = [j for j in kept if id(j) not in recheck or _some_office_ok(j, f)]
    return out, calls


def _some_office_ok(job: dict, f: dict) -> bool:
    """True unless EVERY office of an enriched list fails the US gate alone.

    The gate reads a location as one string, so over a joined list one
    NON_US lookalike ('AF New London WI', 'The Woodlands Hospital',
    'OH-Dublin-Cardinal Place') or one foreign office beside a US one
    ('Detroit; Windsor, Ontario') dropped the whole row, unrecorded, and
    detail_cache repeated the drop every run. A list with any office the
    gate keeps goes on to the scorer, whose `us` flag judges enriched
    office lists - the path these rows took before the early pass existed.
    """
    offices = [o.strip() for o in (job.get("location") or "").split(";")]
    offices = [o for o in offices if o] or [job.get("location") or ""]
    return any(prefilter.location_ok({**job, "location": o}, f)[0]
               for o in offices)


def cmd_run(args):
    started = _time.monotonic()
    cfg = load_config(args.config)
    profile = load_profile(args.profile)
    store = Store(cfg.get("db", "jobs.db"))
    f = cfg["filters"]

    cycles = prefilter.parse_cycles(f.get("target_cycles", []))
    print("Cycles: " + (", ".join(f"{t} {y}" for t, y in
                                  sorted(cycles, key=notify.cycle_key)) or "any"))
    boards, throttled = due_boards(cfg["companies"], store, cfg)
    print(f"Fetching {len(boards)} boards"
          + (f" ({throttled} on a slower clock, not due yet)" if throttled else "") + "...")
    # Snapshot which boards had ever fetched successfully BEFORE this run's
    # outcomes are recorded; board_yield_keys needs it to tell a cold board's
    # first-crawl backfill apart from real new activity.
    prior_ok = {k.lower() for k in store.last_ok_map()}
    etags = store.etags()
    jobs, outcomes = fetch_all(boards, etags=etags)
    errors = [f"{o['company']}: {o['error']}" for o in outcomes
              if o["status"] == "error"]
    unchanged = sum(o["status"] == "unchanged" for o in outcomes)
    print(f"  {len(jobs)} total postings, {unchanged} boards unchanged (304)"
          + (f", {len(errors)} board errors" if errors else ""))
    for e in errors[:5]:
        print(f"  ! {e}")
    outages = detect_outages(outcomes, f.get("outage_min_boards", 5))
    for label in sorted(set(outages.values())):
        print(f"  ! Outage: {label} - health streaks frozen, not counted")
    store.set_meta("last_outages", json.dumps(sorted(set(outages.values()))))
    # Etags are held back until this run's rows are stored (save_etags
    # below). A stored etag means "everything up to this version is
    # ingested": saved at fetch time, a run that died before store.insert
    # (exception, systemd's timeout kill) let the next run 304 straight past
    # postings that were never stored, until the board changed again. Never
    # an etag from a failed fetch either: the adapter may have captured it
    # before a later parse error.
    new_etags = {o["key"]: etags[o["key"]] for o in outcomes
                 if o["status"] in ("ok", "unchanged") and etags.get(o["key"])}
    for o in outcomes:
        status = "outage" if o["key"] in outages else o["status"]
        store.record_board(o["key"], o["company"], status, o["count"],
                           error=o.get("error", ""))

    closed, reopened = close_gone_postings(store, jobs, outcomes,
                                           f.get("close_after_misses", 3),
                                           f.get("min_score", 65))
    if closed:
        print(f"Closed: {len(closed)} postings no longer on their boards")
    if reopened:
        print(f"Reopened: {len(reopened)} closed postings sighted on their "
              f"boards again")

    jobs_kept = drop_covered_simplify(jobs, cfg["companies"], store.producing_boards())

    kept, dropped = prefilter.run(jobs_kept, f)
    print(f"Prefilter: {len(kept)} survive")
    if args.verbose:
        for why, n in sorted(dropped.items(), key=lambda x: -x[1])[:6]:
            print(f"    -{n:<5} {why}")

    # Rows with no office get their detail page BEFORE hashing (see
    # enrich_vague); the calls come out of the main pass's budget below.
    kept, early_calls = enrich_vague(kept, store, f)

    fresh = store.filter_new(kept)
    print(f"Dedupe: {len(fresh)} not seen before")

    # A known role re-listed under a fresh requisition id is a new
    # application window, not a duplicate: resurface the stored row (see
    # store.resurface_reposts for the trigger conditions and noise bound).
    repost_days = f.get("repost_after_days", 0)
    repost_jobs: list[dict] = []
    if repost_days:
        reposts = store.resurface_reposts(kept, repost_days,
                                          f.get("min_score", 0))
        if reposts:
            print(f"Reposts: {len(reposts)} roles resurfaced with fresh "
                  f"requisitions")
            # Collect the fetched rows behind those ids for re-judging.
            # Aggregator rows stay out: they are never LLM-scored, and their
            # resurface already carried the pass-through policy.
            idset, seen_r, agg_reposts = set(reposts), set(), []
            for j in kept:
                jid = row_id(j)
                if jid in idset and jid not in seen_r:
                    seen_r.add(jid)
                    if j.get("source") in AGGREGATOR_SOURCES:
                        agg_reposts.append(jid)
                    else:
                        repost_jobs.append({**j, "id": jid})
            # A resurfaced aggregator row can carry a real score from before
            # the never-score-aggregators policy - a title-only judgment,
            # often from an unfetchable host (a TikTok "70" re-emailed on
            # 2026-08-28 while the description said Master's preferred).
            # Nobody can re-judge it, so it must not wear that confidence:
            # demote to the pass-through convention it would get today.
            if agg_reposts:
                demoted = store.demote_legacy_listing_scores(agg_reposts)
                if demoted:
                    print(f"  {demoted} resurfaced listings dropped their "
                          f"legacy title-only scores (now unvetted '-')")

    # Enrich BEFORE the stale gate: the detail call backfills posted dates
    # (Rippling), and an undated ancient posting must not slip past
    # max_age_days just because its list API hid the date. Reposts enrich
    # too - they are about to be re-judged and the frozen score they carry
    # may have been a title-only judgment in the first place.
    # Rows the pre-dedupe pass already tried carry `_detail` and are not
    # called twice; it counts here only for rows that survived dedupe.
    early_ok = sum(1 for j in fresh + repost_jobs if j.get("_detail") == "ok")
    enriched = sources.enrich(fresh + repost_jobs,
                              cap=sources.ENRICH_CAP - early_calls) + early_ok
    if enriched:
        print(f"Enriched: full descriptions fetched for {enriched} postings")

    # Re-judge reposts with today's profile and the full description before
    # anything can email them. The frozen score is the one that was wrong
    # (pre-profile-fix quant 82s, title-only scores that missed Master's/PhD
    # requirements - 2026-08-28); a failed batch keeps it rather than
    # overwriting real scores with the score-50 fallback.
    if repost_jobs and not args.no_score:
        rated_r = score.run(repost_jobs, profile, cycles=cycles,
                            effort=scorer_effort(cfg),
                            feedback=store.feedback(
                                min_score=f.get("min_score", 65)))
        ok_r = [j for j in rated_r if j.get("why") != "scoring failed"]
        for j in ok_r:
            if not j.get("us", True):
                j["tag"] = "filtered"
        if ok_r:
            n_surf = store.apply_repost_scores(ok_r, f.get("min_score", 65))
            print(f"  reposts re-judged: {n_surf} still surface, "
                  f"{len(ok_r) - n_surf} suppressed by the fresh judgment")

    fresh, cleared = split_cleared(fresh)
    if cleared:
        store.insert([{**j, "score": None,
                       "why": CLEARANCE_WHY,
                       "tag": "filtered"} for j in cleared])
        store.mark_skipped([j["id"] for j in cleared])
        print(f"Clearance: {len(cleared)} postings require a security "
              f"clearance, recorded silently")

    fresh, stale = split_stale(fresh, f.get("max_age_days"), cycles)
    if stale:
        store.insert([{**j, "score": None,
                       "why": f"stale: older than {f.get('max_age_days')}d when first seen",
                       "tag": "stale"} for j in stale])
        store.mark_skipped([j["id"] for j in stale])
        print(f"Stale: {len(stale)} aggregator listings already older than "
              f"{f.get('max_age_days')}d, recorded silently")

    if not fresh:
        store.save_etags(new_etags)
        store.log_run(fetched=len(jobs), prefiltered=len(kept), new_jobs=0,
                      errors="; ".join(errors[:3]))
        if closed or reopened:
            do_publish(cfg, store)  # status flips need the tracker refreshed
        # Rows held by the email batch window flush on quiet runs too - a
        # lone posting must never wait for the next busy run to be emailed.
        send_pending_digest(store, cfg, board_warnings(store, cfg["companies"], f))
        print("Nothing new. Done.")
        return

    threshold = f.get("min_score", 65)
    # Aggregator listings skip the LLM judge WHEN THEY ARRIVE BARE. The
    # rule was never "lists are exempt"; it was that judging a naked title
    # buried good postings, so an unvetted "-" beat a bad number. Since
    # 2026-08-30 enrichment pulls the description off simplify.jobs's
    # posting page for Simplify-sourced rows (sources._fetch_detail,
    # kind 'simplify'), and a row with real text has no claim to the
    # exemption - the reason for it is gone. It also has to lose it:
    # description-less rows are the one path no text gate can inspect,
    # which is how Northwood Space's ITAR-gated internships reached the
    # tracker. Rows still bare (fork feeds with no resolvable id, a failed
    # detail call) keep the old pass-through: score NULL (rendered "-"),
    # tag 'list', cycle from the feed's structured `terms`, sorted below
    # scored rows. --no-score does not change that (they cost nothing to
    # admit); it only silences their email.
    def _bare(j):
        return (j.get("source") in AGGREGATOR_SOURCES
                and len(j.get("description") or "") < 200)
    passthrough = [{**j, "score": None, "why": "curated list pass-through",
                    "cycle": prefilter.feed_cycle(j, cycles),
                    "tag": "list"}
                   for j in fresh if _bare(j)]
    to_score = [j for j in fresh if not _bare(j)]
    n_enriched_list = sum(1 for j in to_score
                          if j.get("source") in AGGREGATOR_SOURCES)
    if n_enriched_list:
        print(f"  {n_enriched_list} aggregator listings arrived with "
              f"descriptions and go to the judge")
    outage = None
    if args.no_score:
        # Seeding mode: store silently, spend nothing, surface nothing. The
        # sentinel sits just BELOW the threshold, whatever it is - a fixed 50
        # was quietly promoted to "surfaced" when min_score dropped from 65
        # to 20, and every seeded row got emailed and pushed to the public
        # tracker (store.surfaced ignores `notified`, so no explicit
        # suppression can save a row whose score clears the bar). Below the
        # bar, the digest, tracker, and email all skip these rows by
        # construction, and the default `rescore` band [threshold-20,
        # threshold-1] covers them, so a plain `jobcrawl rescore` is the
        # recovery path for seeded rows - exactly as documented.
        rated = [{**j, "score": max(0, threshold - 1), "why": "",
                  "cycle": "unknown", "tag": "unscored"}
                 for j in to_score]
    else:
        rated = []
        if to_score:
            print(f"Scoring {len(to_score)} with Claude...")
            rated = score.run(to_score, profile, cycles=cycles,
                              effort=scorer_effort(cfg),
                              feedback=store.feedback(min_score=threshold))
            # A failed batch's rows are NOT stored. The score-50 fallback was
            # calibrated for min_score 65, where failures stayed hidden; at
            # threshold 20 it EMAILED five junk "scoring failed" rows in one
            # digest (2026-08-25) - and scored-once means a stored failure is
            # frozen. Unstored rows look brand-new next run and simply retry;
            # failures are transient (truncation, API hiccups), so the retry
            # loop converges.
            failed = [j for j in rated if j.get("why") == "scoring failed"]
            outage = scoring_outage(store, cfg, failed, len(rated))
            if failed:
                rated = [j for j in rated if j.get("why") != "scoring failed"]
                # "Retry next run" needs the posting to be FETCHED next run,
                # and once this run's etag is saved (after the insert below) -
                # so on Ashby/Greenhouse/Lever/simplify the next
                # fetch would 304 and the row would stay invisible until
                # something else on that board changed (Ashby rows took a
                # median 16.6 h to come back, Greenhouse up to 126 h;
                # Snowflake's AI/ML intern never did). Forget those etags so
                # the retry is real. Aggregator rows do not derive to the
                # feed that carried them, so every configured feed forgets.
                retry = retry_board_keys(failed, cfg["companies"], outcomes)
                lowered = {k.lower() for k in retry}
                new_etags = {k: v for k, v in new_etags.items()
                             if k.lower() not in lowered}
                cleared = store.clear_etags(retry)
                print(f"  {len(failed)} rows failed scoring; left unstored "
                      f"to retry next run"
                      + (f" ({cleared} board etags cleared so they are "
                         f"re-fetched)" if cleared else ""))
    # The scorer's "us" flag is a location blocker, never a score input: a
    # multi-office posting whose every location resolved abroad keeps its
    # real fit score but must not surface. tag 'filtered' + status 'skipped'
    # is the machine-suppression convention (see the clearance gate above):
    # store.surfaced ignores `notified`, so only 'skipped' keeps a
    # high-scoring foreign row off the tracker, and 'filtered' keeps it out
    # of the feedback loop - the user never passed on it.
    offscope = [j for j in rated if not j.get("us", True)]
    for j in offscope:
        j["tag"] = "filtered"
    if offscope:
        print(f"Location: {len(offscope)} postings with no US office, "
              f"recorded silently")
    good = ([] if args.no_score else
            [j for j in rated if j["score"] >= threshold and j.get("us", True)])
    if args.no_score:
        print(f"  {len(rated)} stored suppressed at {max(0, threshold - 1)} "
              f"(--no-score); `jobcrawl rescore` judges them later"
              + (f"; {len(passthrough)} aggregator listings pass through"
                 if passthrough else ""))
    else:
        print(f"  {len(good)} above threshold {threshold}"
              + (f"; {len(passthrough)} aggregator listings pass through unscored"
                 if passthrough else ""))
    if args.verbose:
        for j in sorted(rated, key=lambda j: -j["score"]):
            if j["score"] < threshold:
                print(f"    {j['score']:>3}  {j['company'][:16]:<16} "
                      f"{j['title'][:44]:<44} {j.get('why','')[:38]}")

    # Store everything we saw, keeping real scores so a threshold that is set
    # too high stays visible after the fact, then suppress the low ones from
    # the digest rather than dropping them on the floor.
    good_ids = {j["id"] for j in good}
    store.insert(rated + passthrough)
    store.save_etags(new_etags)
    # A cold board that is actually producing postings earns the normal clock.
    store.record_yield(board_yield_keys(rated, prior_ok))
    if offscope:
        store.mark_skipped([j["id"] for j in offscope])
    if args.no_score:
        # Seed runs email nothing: pass-through rows keep their tracker
        # presence (NULL score surfaces by policy), but a first crawl's
        # backlog of them must not become a flood digest.
        store.mark_notified([j["id"] for j in rated + passthrough])
    else:
        store.mark_notified([j["id"] for j in rated if j["id"] not in good_ids])

    elapsed = _time.monotonic() - started
    store.set_meta("last_run_seconds", f"{elapsed:.0f}")
    # A run that approaches its own timer interval is a slow squeeze nobody
    # would otherwise see: the wrapper's `flock -n` makes the next cycle exit
    # silently rather than overlap, so the crawl just quietly halves its rate.
    # The digest is the only channel the user actually reads, so the warning
    # goes there rather than into a journal.
    budget = f.get("run_warn_seconds", 0)
    warnings = board_warnings(store, cfg["companies"], cfg.get("filters"))
    if budget and elapsed > budget:
        warnings.insert(0, f"Run took {elapsed / 60:.1f} min against a "
                           f"{budget / 60:.0f} min budget - cycles will start "
                           f"being skipped. Check board count and enrichment.")
    if outage:
        warnings.insert(0, outage)
    print(f"Run took {elapsed / 60:.1f} min")
    for w in warnings[:5]:
        print(f"  ⚠ {w}")
    emailed = send_pending_digest(store, cfg, warnings)
    store.log_run(fetched=len(jobs), prefiltered=len(kept), scored=len(good),
                  new_jobs=len(emailed), errors="; ".join(errors[:3]))

    # The tracker is never batched: fresh rows stored this run (surfaced or
    # not), closures, and reopens all need the pages regenerated even while
    # the email itself is being held for the batch window.
    if fresh or closed or reopened:
        do_publish(cfg, store, new_count=len(emailed))


def do_publish(cfg: dict, store: Store, new_count: int = 0) -> None:
    """Regenerate the tracker pages and push them. Never fatal.

    The tracker is a mirror of the store; if GitHub is unreachable the next
    successful run republishes the full current state, so nothing is lost by
    swallowing the error here.
    """
    pub = cfg.get("publish")
    if not pub or not pub.get("enabled", True):
        return
    rows = store.surfaced(cfg.get("filters", {}).get("min_score", 65))
    quant, rest = publish.split_quant(rows)
    summer, coop = publish.split_seasons(rest)
    pages = {
        publish.MAIN_PAGE: publish.render_readme(
            rest,
            cross_link=f"Season views: "
                       f"**[{publish.SUMMER_PAGE}]({publish.SUMMER_PAGE})** "
                       f"({len(summer)}) · "
                       f"**[{publish.COOP_PAGE}]({publish.COOP_PAGE})** "
                       f"({len(coop)}). Quant and trading roles live on "
                       f"their own page: "
                       f"**[{publish.QUANT_PAGE}]({publish.QUANT_PAGE})** "
                       f"({len(quant)} postings).",
        ),
        publish.SUMMER_PAGE: publish.render_readme(
            summer,
            title="Summer Internships",
            blurb="Every surfaced posting recruiting for a summer cycle, "
                  "plus postings that state no season - a year-only umbrella "
                  "req usually means summer.",
            cross_link=f"Full list: **[{publish.MAIN_PAGE}]"
                       f"({publish.MAIN_PAGE})** · co-ops: "
                       f"**[{publish.COOP_PAGE}]({publish.COOP_PAGE})**",
        ),
        publish.COOP_PAGE: publish.render_readme(
            coop,
            title="Co-ops (Fall / Winter / Spring)",
            blurb="Every surfaced posting recruiting for an off-cycle term, "
                  "or calling itself a co-op.",
            cross_link=f"Full list: **[{publish.MAIN_PAGE}]"
                       f"({publish.MAIN_PAGE})** · summer: "
                       f"**[{publish.SUMMER_PAGE}]({publish.SUMMER_PAGE})**",
        ),
        publish.QUANT_PAGE: publish.render_readme(
            quant,
            title="Quant & Trading Roles",
            blurb="Every quantitative research and trading posting jobcrawl "
                  "surfaced.\nEngineering roles, including those at trading "
                  "firms, are on the main page.",
            cross_link=f"Everything else is on **[{publish.MAIN_PAGE}]"
                       f"({publish.MAIN_PAGE})** ({len(rest)} postings).",
        ),
    }
    try:
        pushed = publish.push_pages(pages, pub.get("dir", "."), new_count=new_count)
        print(f"Tracker: {'pages pushed' if pushed else 'no changes'} "
              f"({len(rest)} general, {len(quant)} quant)")
    except Exception as e:
        detail = getattr(e, "stderr", "") or str(e)
        print(f"  ! tracker push failed: {str(detail).strip()[:120]}")


BOARD_LISTS = {
    # Machine-readable feed behind the SimplifyJobs README repos.
    "simplify": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships"
                "/dev/.github/scripts/listings.json",
    # Hand-maintained Summer 2027 list; markdown, so we just harvest its links.
    "sndsh404": "https://raw.githubusercontent.com/sndsh404/summer-2027-internships"
                "/main/README.md",
}


def _board_yaml_file(path: Path, entries: list[dict], header: list[str]) -> None:
    """Write a generated board file: header comments, then one entry a line."""
    lines = list(header) + ["companies:"]
    for e in entries:
        row = yaml.safe_dump(e, default_flow_style=True, width=10_000,
                             sort_keys=False, allow_unicode=True).strip()
        lines.append(f"  - {row}")
    path.write_text("\n".join(lines) + "\n")


def mine_vc_boards(specs: list[dict]) -> dict[tuple[str, str], set[str]]:
    """Sweep Consider-platform VC portfolio boards for pollable ATS boards.

    Returns {(ats, token): {company names}} for every job whose title passes
    the intern/seniority regexes and whose apply URL derives to a board.
    Discovery only: the portfolio jobs themselves are never stored (they
    carry no descriptions), the boards they reveal are what gets polled.
    A failing portfolio board costs its own coverage, never the regen run.

    `kind:` picks the fetcher: most of these boards are hosted Consider, but
    a16z serves the same Consider data from its own Next.js app and needs a
    different one. The title regexes run on every kind - a16z's own
    "internships" filter admits senior-titled roles that SENIOR_PAT drops.
    """
    fetchers = {"consider": sources.consider_jobs, "a16z": sources.a16z_jobs,
                "getro": sources.getro_jobs}
    found: dict[tuple[str, str], set[str]] = {}
    for spec in specs:
        host = spec["host"]
        try:
            jobs = fetchers[spec.get("kind", "consider")](host)
        except Exception as e:
            print(f"  ! vc board {host} failed: {e}")
            continue
        kept = 0
        for j in jobs:
            title = j.get("title") or ""
            if (not prefilter.INTERN_PAT.search(title)
                    or prefilter.SENIOR_PAT.search(title)):
                continue
            b = sources.derive_board(j.get("applyUrl") or j.get("url") or "")
            if b:
                kept += 1
                found.setdefault(b, set()).add(j.get("companyName") or "")
        print(f"  vc board {host}: {len(jobs)} jobs, "
              f"{kept} intern-titled with derivable boards")
    return found


def mine_yc_boards() -> dict[tuple[str, str], set[str]]:
    """Sweep every hiring YC company's WaaS jobs page for intern roles.

    The Algolia directory index says who is hiring (~1,500 companies); each
    one's server-rendered jobs page says what. Companies with at least one
    internship-typed or intern-titled posting become `ats: yc` boards -
    discovery only, exactly like the VC sweep, and free (the paused firecrawl
    Work-at-a-Startup scrape rendered the same data for ~100 credits/day).

    A single failed company page costs its own coverage and heals on the next
    weekly sweep, but a failing WAVE means YC changed something - raise so
    the regen run fails loudly and the old boards_yc.yaml survives untouched.
    """
    companies = sources.yc_hiring_companies()
    found: dict[tuple[str, str], set[str]] = {}
    failures = 0
    for c in companies:
        try:
            props = sources._yc_page(f"/companies/{c['slug']}/jobs").get("props") or {}
        except Exception:
            failures += 1
            continue
        for j in props.get("jobPostings") or []:
            title = j.get("title") or ""
            if j.get("type") == "Internship" or (
                    prefilter.INTERN_PAT.search(title)
                    and not prefilter.SENIOR_PAT.search(title)):
                found.setdefault(("yc", c["slug"]), set()).add(c["name"])
                break
    if failures > len(companies) // 10:
        raise RuntimeError(
            f"yc sweep: {failures}/{len(companies)} company pages failed")
    print(f"  yc sweep: {len(companies)} hiring companies, "
          f"{len(found)} with intern roles, {failures} pages failed")
    return found


def resolve_boards(rows: list[dict], cfg: dict, cache_path: Path,
                   hot: set[tuple[str, str]], curated_names: set[str],
                   budget: int | None = None) -> list[dict]:
    """Resolve unpollable feed companies to boards via their websites.

    Candidates are every feed row whose apply URL derives to no board (the
    company site, a redirector, an ATS we cannot poll), plus every company
    whose derived board the health table says is dead - the second group is
    forced through even when the cache is fresh, because a dead token is the
    one signal that a company changed ATS. Each candidate is looked up once
    per `filters.resolve_recheck_days` (30); `filters.resolve_per_run`
    caps the work per regen. The cache is a committed JSON file keyed by
    lowercased company name; the file this returns is rebuilt from it every
    time, so a board found last month stays polled with no further fetches.
    """
    import concurrent.futures as _cf
    import sqlite3 as _sq

    f = cfg.get("filters", {})
    recheck = f.get("resolve_recheck_days", 30)
    budget = f.get("resolve_per_run", 300) if budget is None else budget
    cache: dict[str, dict] = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text()) or {}
        except ValueError:
            cache = {}

    candidates: dict[str, str] = {}   # name_lower -> company_url
    names: dict[str, str] = {}        # name_lower -> display name
    derived_names: dict[str, str] = {}  # name_lower -> company_url, for dead-board lookups
    for x in rows:
        name = (x.get("company_name") or "").strip()
        if not name or name.lower() in curated_names:
            continue
        key = name.lower()
        names.setdefault(key, name)
        if sources.derive_board(x.get("url") or ""):
            derived_names.setdefault(key, x.get("company_url") or "")
        else:
            candidates.setdefault(key, x.get("company_url") or "")

    # Dead boards: the health table names them by the config's display
    # name, which for derived boards is the feed's company name.
    forced: set[str] = set()
    db_path = cfg.get("db", "jobs.db")
    dead_after = f.get("dead_board_fails", 12)
    if dead_after and Path(db_path).is_file():
        try:
            con = _sq.connect(f"file:{db_path}?mode=ro", uri=True)
            for (nm,) in con.execute(
                    "SELECT name FROM boards WHERE fail_streak >= ?", (dead_after,)):
                key = (nm or "").strip().lower()
                if key and key not in curated_names and (key in derived_names or key in candidates):
                    forced.add(key)
                    candidates.setdefault(key, derived_names.get(key, ""))
            con.close()
        except _sq.Error as e:
            print(f"  ! resolver: could not read board health: {e}")

    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = (now - _dt.timedelta(days=recheck)).isoformat()

    def _age(key):
        return (cache.get(key) or {}).get("checked") or ""

    due = [k for k in candidates
           if k in forced or k not in cache or _age(k) < cutoff]
    due.sort(key=lambda k: (k not in forced, _age(k), k))
    todo = due[:budget] if budget else []
    print(f"  resolver: {len(candidates)} companies without a pollable board, "
          f"{len(forced)} with a dead board, {len(due)} due, "
          f"{len(todo)} checked this run")

    def work(key):
        entry = cache.get(key) or {}
        r = resolve.resolve_company(names.get(key, key), candidates.get(key) or entry.get("company_url"),
                                    website=None)
        return key, r

    found_now = 0
    if todo:
        with _cf.ThreadPoolExecutor(max_workers=6) as ex:
            for key, r in ex.map(work, todo):
                prev = cache.get(key) or {}
                same = prev.get("ats") == r["ats"] and prev.get("token") == r["token"]
                cache[key] = {"name": r["name"], "company_url": candidates.get(key) or prev.get("company_url"),
                              "website": r["website"], "ats": r["ats"], "token": r["token"],
                              "checked": now.isoformat()}
                if same and "verified" in prev:
                    cache[key]["verified"] = prev["verified"]
                if r["ats"] and not prev.get("ats"):
                    found_now += 1
                    print(f"    + {r['name']}: {r['ats']}:{r['token']}")

    # A page can name a board that does not exist - a Greenhouse "users"
    # sign-in link, an iCIMS cookie-script host, a JSON-escaped slug - and
    # a token that 404s forever would sit in board health as a dead board.
    # Every hit is fetched once through its adapter before it may enter the
    # tier, the same rule import-simplify applies; a verdict sticks to the
    # (ats, token) it was made for and is redone when the resolver finds a
    # different board.
    def verify(key):
        e = cache[key]
        try:
            sources.fetch(e.get("name") or key, e["ats"], e["token"])
            return key, True
        except Exception:  # noqa: BLE001 - any failure means "not a board"
            return key, False

    # A failed verdict is retried every regen (a dozen fetches), so a board
    # that was merely down on the day it was found still makes the tier.
    unverified = [k for k, e in cache.items() if e.get("ats") and e.get("verified") is not True]
    if unverified:
        with _cf.ThreadPoolExecutor(max_workers=6) as ex:
            for key, ok in ex.map(verify, unverified):
                e = cache[key]
                if not ok:
                    # The page named a board that is not one (Jump Trading's
                    # careers page embeds a `talent_community` Greenhouse
                    # form, not its job board). Before dropping the company,
                    # try its name as a slug; a probe hit has jobs, so it
                    # counts as verified.
                    hit = resolve.probe_slugs(e.get("name") or key, e.get("company_url"))
                    if hit:
                        print(f"    ~ {e.get('name') or key}: {e['ats']}:{e['token']} does not resolve; "
                              f"name probe found {hit[0]}:{hit[1]}")
                        e["ats"], e["token"] = hit
                        ok = True
                    else:
                        print(f"    - {e.get('name') or key}: {e['ats']}:{e['token']} does not resolve, dropped")
                e["verified"] = ok
    cache_path.write_text(json.dumps(dict(sorted(cache.items())), indent=1, sort_keys=True) + "\n")

    entries, seen = [], set()
    for key, e in sorted(cache.items()):
        if not e.get("ats") or not e.get("verified") or key in curated_names:
            continue
        b = (e["ats"], str(e["token"]).lower())
        if b in hot or b in seen:
            continue
        seen.add(b)
        entries.append({"name": e.get("name") or key, "ats": e["ats"],
                        "token": e["token"], "cold": True})
    print(f"  resolver: {found_now} new boards found this run, "
          f"{len(entries)} resolved boards on the tier")
    return entries


def cmd_boards(args):
    """Regenerate boards_broad.yaml and boards_cold.yaml from the public lists.

    Every apply URL in those lists that points at an ATS we can poll directly
    becomes a board entry, so the next `run` sees new postings on those boards
    the moment the ATS serves them instead of when the list gets around to it.
    Boards already in config.yaml are left out - the curated file owns those.

    The listings feeds carry their whole history, not just what is open today:
    ~14k rows of which ~2k are active. The inactive 12k are last cycle's
    postings, and a company that ran an internship last year almost always
    runs one this year - so their boards are proven leads, just not ones worth
    an hourly request. Those become the cold tier in boards_cold.yaml, polled
    on a slow clock under a per-run budget (see `due_boards`). One fetch, two
    files: the active rows decide the hot list exactly as before.
    """
    import re as _re

    cfg = yaml.safe_load(Path(args.config).read_text())
    curated = {(c["ats"], str(c["token"]).lower()) for c in cfg.get("companies", [])}
    # Exclusion by name as well as by token: when a company renames its board
    # (Atomic Semi's ashby slug became fab2), the public lists keep linking
    # the dead slug for months, and regenerating would keep resurrecting a
    # token that can only 404. A curated entry for that company means we
    # already poll the right board - skip every derived variant of it.
    curated_names = {str(c["name"]).lower() for c in cfg.get("companies", [])}
    # exclude_companies too: every posting a hard-no employer yields dies at
    # the prefilter's name match, so polling their boards is pure waste. This
    # doubles as the tombstone for a company whose board died with no
    # successor anywhere (AcreTrader, 2026-09-01) - with no curated entry to
    # win the name match, the feeds' inactive history would resurrect the
    # dead token every weekly regen, and its health row would warn forever.
    curated_names |= {str(n).lower()
                      for n in cfg.get("filters", {}).get("exclude_companies", [])}

    # Every aggregator feed the crawl polls also contributes to board
    # derivation - the curated entries plus anything `feeds --add` has vetted.
    # The merged config is the single source of truth for the list URLs
    # (boards_broad.yaml never contains aggregator entries, so including it
    # here is harmless); BOARD_LISTS only supplies fallbacks.
    merged = load_config(args.config).get("companies", [])
    json_urls = list(dict.fromkeys(
        str(c["token"]) for c in merged if c["ats"] == "simplify"
    )) or [BOARD_LISTS["simplify"]]
    md_urls = list(dict.fromkeys(
        str(c["token"]) for c in merged if c["ats"] == "sndsh404"
    )) or [BOARD_LISTS["sndsh404"]]

    boards: dict[tuple[str, str], set[str]] = {}
    historical: dict[tuple[str, str], set[str]] = {}
    active, inactive = [], []
    for feed_url in json_urls:
        r = sources.get(feed_url, headers=sources.UA, timeout=60)
        r.raise_for_status()
        for x in r.json():
            (active if x.get("active") else inactive).append(x)
    # Company-universe feeds (config `universe_feeds:`): same listings.json
    # shape, but their rows are never internships - the SimplifyJobs
    # New-Grad list is the big one. A company hiring new grads almost
    # always runs an intern program, and ~1,850 of its boards were on no
    # tier at all (2026-09-11). Every row counts as history, active or not,
    # so these only ever feed the cold tier and the resolver.
    universe_rows = []
    for feed_url in cfg.get("universe_feeds") or []:
        try:
            r = sources.get(feed_url, headers=sources.UA, timeout=60)
            r.raise_for_status()
            rows = [x for x in r.json() if isinstance(x, dict)]
        except Exception as e:
            print(f"  ! universe feed {feed_url}: {type(e).__name__}: {str(e)[:60]}")
            continue
        universe_rows.extend(rows)
        inactive.extend(rows)
    for x in active:
        b = sources.derive_board(x.get("url", ""))
        if b:
            boards.setdefault(b, set()).add(x.get("company_name", ""))
    for x in inactive:
        b = sources.derive_board(x.get("url", ""))
        if b:
            historical.setdefault(b, set()).add(x.get("company_name", ""))

    for feed_url in md_urls:
        r = sources.get(feed_url, headers=sources.UA, timeout=60)
        r.raise_for_status()
        for company, url in _re.findall(
                r"^\| ([^|]+) \|[^|]*\|[^|]*\| \[apply\]\((\S+?)\)",
                r.text, _re.M):
            b = sources.derive_board(url)
            if b:
                boards.setdefault(b, set()).add(company.strip())

    def pick(items, exclude):
        out = []
        for (ats, token), names in sorted(items.items()):
            if (ats, token.lower()) in exclude:
                continue
            if any(n.lower() in curated_names for n in names if n):
                continue
            name = min((n for n in names if n), key=len, default=token)
            out.append({"name": name, "ats": ats, "token": token})
        return out

    entries = pick(boards, curated)
    # A derived Oracle HCM or Jibe board pages hundreds of rows a poll
    # (Hilton: 508), and the 2026-09-29 regen minted 85 of them at once,
    # which put the run over its budget; two hours matches the curated ones.
    for e in entries:
        if e["ats"] in SLOW_DERIVED_ATS:
            e["every_hours"] = 2

    applied_path = Path(args.config).parent / args.applied
    applied = set()
    if applied_path.is_file():
        for c in (yaml.safe_load(applied_path.read_text()) or {}).get("companies", []):
            applied.add((c["ats"], str(c["token"]).lower()))

    # VC portfolio boards (Consider platform): discovery only. Intern-titled
    # jobs whose apply URL derives to a pollable board become boards_vc.yaml;
    # boards the hotter tiers already poll are left out.
    hot = curated | applied | {(e["ats"], e["token"].lower()) for e in entries}
    vc_entries = []
    if cfg.get("vc_boards"):
        vc_entries = [{**e, "every_hours": 4}
                      for e in pick(mine_vc_boards(cfg["vc_boards"]), hot)]
        hot |= {(e["ats"], e["token"].lower()) for e in vc_entries}

    # YC sweep: intern-hiring YC companies become yc boards. 6h clock - a
    # hundred-plus discovered boards on the every-run clock would add minutes
    # to every cycle against the 12-minute flock timer.
    yc_entries = []
    if cfg.get("filters", {}).get("yc_sweep"):
        yc_entries = [{**e, "every_hours": 6}
                      for e in pick(mine_yc_boards(), hot)]
        hot |= {(e["ats"], e["token"].lower()) for e in yc_entries}

    # Companies the lists name but cannot point us at: their link is a
    # careers page or an ATS they have since left. Resolve their CURRENT
    # board from their own website (resolve.py), cached so each company
    # costs a few requests a month, budgeted so the first sweep of ~1,300
    # spreads over days. Dead boards re-resolve on every regen - a 404 on
    # the old token is exactly when a company has moved ATS.
    resolved_entries = []
    if not getattr(args, "no_resolve", False):
        cache_path = Path(args.config).parent / args.resolve_cache
        resolved_entries = resolve_boards(
            active + inactive, cfg, cache_path, hot, curated_names,
            budget=args.resolve_budget)
        hot |= {(e["ats"], e["token"].lower()) for e in resolved_entries}

    # The cold list is everything the hot list did not take. A board with any
    # active listing belongs on the hourly clock, and one already reached by
    # the curated or applied-import tiers is not a lead at all.
    cold = [{**e, "cold": True} for e in pick(historical, hot)]

    out = Path(args.config).parent / args.out
    _board_yaml_file(out, entries, [
        "# Auto-generated by `python -m jobcrawl.cli boards` - do not hand-edit.",
        "# Boards derived from the apply URLs on the public GitHub internship",
        "# lists. Curated entries live in config.yaml and always win on conflict.",
    ])
    print(f"{out}: {len(entries)} boards "
          f"({len(active)} active listings across "
          f"{len(json_urls) + len(md_urls)} lists scanned, "
          f"curated overlap excluded)")

    if cfg.get("vc_boards"):
        vc_out = Path(args.config).parent / args.vc_out
        _board_yaml_file(vc_out, vc_entries, [
            "# Auto-generated by `python -m jobcrawl.cli boards` - do not hand-edit.",
            "# Boards derived from intern-titled jobs on the Consider-platform VC",
            "# portfolio boards in config.yaml's `vc_boards:`. Active leads, but",
            "# hundreds of them, so they poll on a 4h clock instead of the hourly",
            "# one. Boards the hotter tiers already poll are excluded.",
        ])
        print(f"{vc_out}: {len(vc_entries)} vc boards "
              f"({len(cfg['vc_boards'])} portfolio boards swept)")

    if cfg.get("filters", {}).get("yc_sweep"):
        yc_out = Path(args.config).parent / args.yc_out
        _board_yaml_file(yc_out, yc_entries, [
            "# Auto-generated by `python -m jobcrawl.cli boards` - do not hand-edit.",
            "# YC companies whose Work-at-a-Startup page lists an intern role,",
            "# found by sweeping every isHiring company in YC's public directory",
            "# index. They poll on a 6h clock; boards the hotter tiers already",
            "# poll are excluded.",
        ])
        print(f"{yc_out}: {len(yc_entries)} yc boards")

    if not getattr(args, "no_resolve", False):
        res_out = Path(args.config).parent / args.resolved_out
        _board_yaml_file(res_out, resolved_entries, [
            "# Auto-generated by `python -m jobcrawl.cli boards` - do not hand-edit.",
            "# Boards found by reading each company's OWN website for the ATS it",
            "# uses today (jobcrawl/resolve.py), for companies the public lists",
            "# name but link to a careers page or a board that no longer exists.",
            "# Cold tier; resolve_cache.json is the memory behind this file.",
        ])
        print(f"{res_out}: {len(resolved_entries)} resolved boards")

    cold_out = Path(args.config).parent / args.cold_out
    _board_yaml_file(cold_out, cold, [
        "# Auto-generated by `python -m jobcrawl.cli boards` - do not hand-edit.",
        "# The COLD tier: boards that appear only on the listings feeds'",
        "# INACTIVE history, i.e. companies that ran an internship last cycle",
        "# but have nothing open right now. Proven leads, not urgent ones, so",
        "# they poll on filters.cold_every_hours under a per-run budget",
        "# (filters.cold_boards_per_run, 0 = the whole tier is off).",
    ])
    print(f"{cold_out}: {len(cold)} cold boards "
          f"({len(inactive)} inactive listings scanned, "
          f"hot and curated overlap excluded)")


def read_simplify_csv(path: str) -> list[dict]:
    """Rows of a Simplify tracker export, keys normalized to what we use.

    utf-8-sig because the export carries a BOM, which would otherwise become
    part of the first column's name and make 'Job Title' unreachable. The
    date columns are read but never used: '24-08-26' on 2026-08-24 is
    DD-MM-YY, and nothing here needs a date badly enough to guess.
    """
    import csv
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = []
        for r in csv.DictReader(fh):
            rows.append({
                "title": (r.get("Job Title") or "").strip(),
                "company": (r.get("Company Name") or "").strip(),
                "location": (r.get("Location") or "").strip(),
                "url": (r.get("Job URL") or "").strip(),
                "status": (r.get("Status") or "").strip().upper(),
            })
    return rows


def match_export(rows: list[dict], stored) -> tuple[dict, list[dict]]:
    """Map export rows to stored posting ids. Returns ({id: how}, unmatched).

    URL first: it is the posting's other identity everywhere else in this
    codebase, compares by `url_key` so a differently-dressed link to the same
    job id still hits, and is the only match that cannot confuse two roles.
    Company+title is the fallback, stripped and lowercased the way `job_id`
    normalizes them, for exports whose URL went through a redirector.

    Several stored rows can share a company+title (one role posted in five
    cities); the fallback takes the lowest id deterministically rather than
    flipping all five, because the user applied to one of them.
    """
    by_url: dict[str, str] = {}
    by_name: dict[tuple[str, str], str] = {}
    for r in stored:
        key = url_key(r["url"] or "")
        if key and key not in by_url:
            by_url[key] = r["id"]
        nk = ((r["company"] or "").lower().strip(),
              (r["title"] or "").lower().strip())
        if nk not in by_name or r["id"] < by_name[nk]:
            by_name[nk] = r["id"]
    matched: dict[str, str] = {}
    unmatched = []
    for row in rows:
        jid = by_url.get(url_key(row["url"])) if row["url"] else None
        how = "url"
        if not jid:
            jid = by_name.get((row["company"].lower(), row["title"].lower()))
            how = "name"
        if jid:
            matched.setdefault(jid, how)
        else:
            unmatched.append(row)
    return matched, unmatched


# Placeholders the export uses where it never learned the employer's name.
# Written into a board file they become the display name in board-health
# warnings and the fallback company on any posting an adapter leaves unnamed,
# so a real name from another row for the same board is always better.
_JUNK_NAMES = {"", "unknown company", "unknown", "unavailable", "n/a", "none"}


def board_name(names: list[str], token: str) -> str:
    """Pick a display name for a board out of every export row naming it.

    Most common wins, shortest breaks the tie - one board reaches us as both
    "Rocket Lab" and "Rocket Lab USA, Inc.", and the plain form reads better
    in a digest warning.

    Two kinds of non-name are thrown out first. Simplify's placeholders
    ("Unknown Company") are obvious. Less obvious: where its scraper failed on
    a Workday tenant it recorded the SITE slug as the employer, so Warner
    Bros' board arrives called "global" and ZEISS' called "External" - which
    is exactly the segment already sitting in the token. When nothing is left,
    the tenant is a better name than the whole token: "warnerbros" beats
    "warnerbros|5|global".

    Only the segments AFTER the first count as slugs. A one-segment token is
    a Greenhouse/Lever/Ashby slug, which is normally just the company name
    lowercased - "Merge" on `greenhouse:merge` is the real answer, not noise.
    """
    segments = {s.lower() for s in token.split("|")[1:]}
    real = [n.strip() for n in names
            if n.strip().lower() not in _JUNK_NAMES
            and n.strip().lower() not in segments]
    if not real:
        return token.split("|")[0]
    return min(real, key=lambda n: (-real.count(n), len(n), n))


def cmd_import_simplify(args):
    """Import a Simplify tracker CSV export: mark applied, discover boards.

    Two jobs from one file, because the export answers two questions at once.
    What the user applied to is feedback the scorer should be calibrated on
    and a row the tracker should show as ✅. Where they applied is a list of
    boards worth polling: a posting reached by hand is a board nothing here
    watches, and those are exactly the gaps the public lists do not cover.

    Discovery writes only to the include file - config.yaml stays
    hand-curated, and boards_broad.yaml is regenerated wholesale, so an append
    there would not survive the next `boards` run.
    """
    cfg = load_config(args.config)
    rows = read_simplify_csv(args.csv)
    applied_rows = [r for r in rows if r["status"] == "APPLIED"]
    print(f"{args.csv}: {len(rows)} rows "
          f"({len(applied_rows)} applied, {len(rows) - len(applied_rows)} other)")

    store = Store(cfg.get("db", "jobs.db"))
    stored = store.match_rows()
    matched, unmatched = match_export(applied_rows, stored)
    status = {r["id"]: r["status"] for r in stored}
    by_url = sum(1 for how in matched.values() if how == "url")
    eligible = [i for i in matched if status.get(i) in ("new", "skipped")]
    already = [i for i in matched if status.get(i) == "applied"]
    closed = [i for i in matched if status.get(i) == "closed"]

    print(f"\nApplied rows: {len(matched)} matched stored postings "
          f"({by_url} by URL, {len(matched) - by_url} by company+title), "
          f"{len(unmatched)} matched nothing")
    print(f"  {len(eligible)} to flip, {len(already)} already applied, "
          f"{len(closed)} closed (left alone)")
    for i in eligible[:5]:
        r = next(x for x in stored if x["id"] == i)
        print(f"    {i[:8]}  {r['company'][:22]:<22} {r['title'][:44]}")
    for r in unmatched[:5]:
        print(f"    unmatched: {r['company'][:22]:<22} {r['title'][:44]}")

    # Board discovery reads every row, applied and saved alike: a saved
    # posting names a board just as well as an applied one. Candidates are
    # keyed case-insensitively, since the same board reached under two
    # spellings is one board, but each entry keeps its token's real case -
    # ATS tokens are case-preserving, and a lowercased 'ATPCO1' 404s.
    #
    # Cold entries deliberately do NOT count as configured: that tier is a
    # slow clock which ships switched off, and a board the user applied to by
    # hand belongs on the hourly one. Writing it to the applied file promotes
    # it, since `include:` lists that file ahead of the cold one.
    have = {(c["ats"], str(c["token"]).lower())
            for c in cfg["companies"] if not c.get("cold")}
    cands: dict[tuple[str, str], tuple[list[str], str]] = {}
    derived = 0
    for r in rows:
        b = sources.derive_board(r["url"])
        if not b:
            continue
        derived += 1
        key = (b[0], b[1].lower())
        if key in have:
            continue
        names, _ = cands.setdefault(key, ([], b[1]))
        names.append(r["company"])
    print(f"\nBoards: {derived} rows map to a pollable board, "
          f"{len(cands)} of those are not configured")

    # Verify live even in --dry-run: it is a read-only GET and the report is
    # only worth anything if it says which candidates actually resolve. A
    # token that 404s here would otherwise join the config and sit in board
    # health forever. derive_board never yields firecrawl, so nothing metered
    # can be touched from this path.
    # --no-verify exists because that check is the slow half of this command -
    # dozens of boards, and a Workday tenant among them costs ~20 throttled
    # POSTs on its own. When you only want the applied/matched counts, paying
    # for verification is paying for the wrong answer.
    entries = [{"name": board_name(names, token), "ats": ats, "token": token}
               for (ats, _lc), (names, token) in sorted(cands.items())]
    verified, failed = [], []
    if getattr(args, "no_verify", False):
        # Skips only the board check. The applied flip below still happens -
        # until 2026-09-11 this branch returned early and a --no-verify import
        # silently marked nothing, which is the opposite of what the flag
        # promises ("skip the slow half", not "skip the point").
        print(f"  --no-verify: {len(entries)} candidates left unchecked "
              f"(unverified tokens are never written to the board file)")
        for e in entries[:15]:
            print(f"  ?     {e['name'][:24]:<24} {e['ats']}:{e['token']}")
    else:
        # fetch_all, not a serial loop: a few dozen candidates with a Workday
        # or two among them takes minutes one at a time, and this is the same
        # thread-pool-plus-throttle path a real run uses.
        _, outcomes = fetch_all(entries, etags=None)
        by_key = {f"{e['ats']}:{e['token']}": e for e in entries}
        for o in sorted(outcomes, key=lambda o: o["key"]):
            e = by_key[o["key"]]
            if o["status"] == "error":
                failed.append((e, o["error"]))
                print(f"  FAIL  {e['name'][:24]:<24} {o['key']} {o['error']}")
            else:
                verified.append(e)
                print(f"  OK    {e['name'][:24]:<24} {o['key']} ({o['count']} postings)")
        print(f"  {len(verified)} verified, {len(failed)} failed")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        store.close()
        return

    flipped = store.mark_applied(eligible)
    print(f"\nFlipped {len(flipped)} postings to applied.")
    out = Path(args.config).parent / args.out
    existing = []
    if out.is_file():
        existing = (yaml.safe_load(out.read_text()) or {}).get("companies", [])
    seen = {(c["ats"], str(c["token"]).lower()) for c in existing}
    new = [v for v in verified if (v["ats"], v["token"].lower()) not in seen]
    if new:
        _board_yaml_file(out, existing + new, [
            "# Boards mined from your own Simplify/Handshake application exports",
            "# (`python -m jobcrawl.cli import-simplify <export.csv>`). Appended to,",
            "# never regenerated, so entries survive later imports; every token was",
            "# live-verified when it was added. config.yaml still wins on conflict.",
        ])
        print(f"wrote {out}: {len(existing) + len(new)} boards ({len(new)} new). "
              f"Add it to `include:` in {args.config} if it is not there yet.")
    if flipped:
        do_publish(cfg, store)  # flipped rows show ✅ on the tracker
    store.close()


def cmd_list(args):
    store = Store(load_config(args.config).get("db", "jobs.db"))
    rows = store.pending(args.limit)
    if not rows:
        print("Nothing pending.")
        return
    for r in rows:
        cycle = (r["cycle"] or "unknown")[:12]
        score = r["score"] if r["score"] is not None else "-"
        print(f"{r['id'][:8]}  {score:>3}  {cycle:<12} "
              f"{r['company'][:18]:<18} {r['title'][:46]}")


def cmd_applied(args):
    cfg = load_config(args.config)
    store = Store(cfg.get("db", "jobs.db"))
    n = store.set_status(args.id, "applied")
    print(f"Marked {n} as applied." if n else f"No job matching id {args.id}")
    if n:
        do_publish(cfg, store)  # flip the row to ✅ on the tracker


FEED_PATHS = (".github/scripts/listings.json", "listings.json", "data/listings.json")

# A candidate must clear all of these before it can be polled automatically.
# Job feeds are untrusted input from strangers; the bar is deliberately high,
# and everything that fails it is still reported for a human to look at.
FEED_MIN_STARS = 50
FEED_MAX_AGE_DAYS = 21
FEED_MIN_ACTIVE = 20


def _probe_feed(full_name: str) -> dict | None:
    """Does this repo expose a Simplify-shaped listings.json we can read?"""
    for branch in ("dev", "main", "master"):
        for path in FEED_PATHS:
            url = f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"
            try:
                r = requests_mod().get(url, headers=sources.UA, timeout=45)
                if r.status_code != 200:
                    continue
                data = r.json()
                if not isinstance(data, list) or not data:
                    continue
                row = data[0]
                if not isinstance(row, dict) or not {"company_name", "title", "url"} <= set(row):
                    continue
                active = [x for x in data if x.get("active")]
                return {"url": url, "total": len(data), "active": len(active)}
            except Exception:
                continue
    return None


def cmd_feeds(args):
    """Find GitHub internship aggregators we are not already reading.

    The three feeds in config.yaml were hand-picked; new ones appear every
    cycle and the good ones publish the same machine-readable listings.json,
    so discovery can be automatic. Vetted candidates are appended to an
    include file rather than config.yaml, which stays hand-curated.
    """
    import yaml as _yaml

    cfg = load_config(args.config)
    known = {str(c["token"]) for c in cfg["companies"]}
    known_repos = {t.split("/")[3] + "/" + t.split("/")[4]
                   for t in known if t.startswith("https://raw.githubusercontent.com/")
                   and len(t.split("/")) > 4}

    headers = {**sources.UA, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    query = ("internships in:name,description "
             "pushed:>" + (_dt.date.today() - _dt.timedelta(days=FEED_MAX_AGE_DAYS)).isoformat())
    r = sources.get("https://api.github.com/search/repositories",
                    params={"q": query, "sort": "stars", "per_page": 40},
                    headers=headers, timeout=45)
    r.raise_for_status()
    repos = r.json().get("items", [])
    print(f"{len(repos)} candidate repos updated in the last {FEED_MAX_AGE_DAYS} days")

    found, vetted = [], []
    for repo in repos:
        name = repo["full_name"]
        if name in known_repos:
            continue
        feed = _probe_feed(name)
        if not feed:
            continue
        ok = (repo["stargazers_count"] >= FEED_MIN_STARS
              and feed["active"] >= FEED_MIN_ACTIVE)
        found.append((name, repo["stargazers_count"], feed, ok))
        if ok:
            vetted.append({"name": f"{name.split('/')[0]} feed",
                           "ats": "simplify", "token": feed["url"]})

    if not found:
        print("No new machine-readable feeds found.")
        return
    print(f"\n{len(found)} new feeds with a readable listings.json:")
    for name, stars, feed, ok in sorted(found, key=lambda x: -x[1]):
        mark = "ADD " if ok else "skip"
        print(f"  {mark} {stars:>5}*  {name:<44} {feed['active']:>4} active / {feed['total']}")

    if not vetted:
        print("\nNone cleared the auto-add bar; nothing written.")
        return
    if not args.add:
        print(f"\n{len(vetted)} would be added. Re-run with --add to write "
              f"{args.out}.")
        return

    out = Path(args.config).parent / args.out
    existing = []
    if out.is_file():
        existing = (_yaml.safe_load(out.read_text()) or {}).get("companies", [])
    have = {c["token"] for c in existing}
    merged = existing + [v for v in vetted if v["token"] not in have]
    lines = ["# Auto-discovered aggregator feeds (`jobcrawl feeds --add`).",
             "# Vetted: readable listings.json, "
             f"{FEED_MIN_STARS}+ stars, {FEED_MIN_ACTIVE}+ active listings.",
             "# Remove an entry here and it will be re-added; blacklist in config.yaml instead.",
             "companies:"]
    for c in merged:
        lines.append("  - " + _yaml.safe_dump(c, default_flow_style=True, width=10_000,
                                              sort_keys=False).strip())
    out.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {out}: {len(merged)} feeds "
          f"({len(merged) - len(existing)} new). Add it to `include:` if not already there.")


def cmd_audit(args):
    """Measure the prefilter's false-negative rate.

    The prefilter is the one stage that can lose a good posting with no trace:
    everything it drops is invisible downstream, so a rule that is slightly
    too aggressive looks exactly like a quiet job market. This re-scores a
    sample of what was thrown away and reports anything that would have
    cleared the threshold.

    Sampling is stratified by drop reason rather than uniform. A uniform
    sample would be ~97% "not an internship title" and would almost never
    test the keyword, cycle, and location rules - which are the ones with
    judgment in them, and the ones worth catching.

    Read-only with respect to `jobs`: sampled postings are deliberately NOT
    inserted, or the audit would mark them seen and suppress them for real.
    """
    import random

    cfg = load_config(args.config)
    profile = load_profile(args.profile)
    store = Store(cfg.get("db", "jobs.db"))
    f = cfg["filters"]
    threshold = f.get("min_score", 65)
    cycles = prefilter.parse_cycles(f.get("target_cycles", []))

    # The cold tier is excluded: its ~1,800 boards share the exact same
    # prefilter rules as the hot tier, so they add nothing to the sample -
    # but fetching 900 cold workday boards unconditionally would turn a
    # 5-minute audit into an hour of pointless hammering.
    boards = [c for c in cfg["companies"] if not c.get("cold")]
    print(f"Fetching {len(boards)} hot-tier boards (unconditional - an audit "
          f"needs full board contents, not 304s)...")
    # etags=None deliberately: a 304 board contributes nothing to the pool.
    jobs, outcomes = fetch_all(boards, etags=None)
    jobs = drop_covered_simplify(jobs, cfg["companies"])
    kept, dropped = prefilter.partition(jobs, f)
    print(f"  {len(jobs)} fetched, {len(kept)} survived, {len(dropped)} dropped")
    if not dropped:
        print("Nothing was dropped; nothing to audit.")
        return

    by_reason: dict[str, list] = {}
    for job, why in dropped:
        by_reason.setdefault(why, []).append(job)
    if args.reason:
        by_reason = {k: v for k, v in by_reason.items() if args.reason.lower() in k.lower()}
        if not by_reason:
            print(f"No drop reason matching {args.reason!r}.")
            return

    rng = random.Random(args.seed)
    sample: list[tuple[dict, str]] = []
    for why, group in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        for job in rng.sample(group, min(args.per_reason, len(group))):
            sample.append((job, why))
    sample = sample[:args.sample]
    print(f"Scoring {len(sample)} dropped postings across "
          f"{len({w for _, w in sample})} drop reasons...")

    # The drop reason rides along on the job dict itself: score.run rebuilds
    # every row with {**j, ...}, so extra keys survive its sorting. Mapping
    # back by object identity or URL after the fact mislabels rows whenever
    # score.run copies them or two sampled drops share a URL.
    rated = score.run([{**dict(j), "audit_reason": why} for j, why in sample],
                      profile, cycles=cycles, effort=scorer_effort(cfg),
                      feedback=store.feedback(min_score=threshold))

    misses = [j for j in rated if j["score"] >= threshold]
    tally: dict[str, list[int]] = {}
    for j in rated:
        tally.setdefault(j.get("audit_reason", "?"), []).append(j["score"])

    print(f"\n{len(misses)}/{len(rated)} sampled drops would have scored "
          f">= {threshold}")
    for j in sorted(misses, key=lambda j: -j["score"]):
        why = j.get("audit_reason", "?")
        print(f"  MISS {j['score']:>3}  {j['company'][:20]:<20} {j['title'][:44]:<44}")
        print(f"       dropped for: {why}")
        print(f"       {j.get('why','')[:70]}")

    print("\nper-rule results (sampled, max score seen):")
    for why, scores in sorted(tally.items(), key=lambda kv: -max(kv[1])):
        n_miss = sum(1 for s in scores if s >= threshold)
        flag = "  <-- leaking" if n_miss else ""
        print(f"  {max(scores):>3}  {n_miss}/{len(scores):<3} {why[:52]}{flag}")

    detail = "; ".join(f"{why}:{sum(1 for s in ss if s >= threshold)}/{len(ss)}"
                       for why, ss in tally.items())
    store.log_audit(len(rated), len(misses), detail[:500])
    past = store.recent_audits()
    if len(past) > 1:
        print("\nhistory:", ", ".join(
            f"{r['ts'][:10]} {r['misses']}/{r['sampled']}" for r in past))


def cmd_skipped(args):
    """Dismiss a surfaced posting: off the tracker, and a negative signal.

    Applied and skipped rows together form the calibration examples fed back
    into the scoring prompt, so passing on postings actively sharpens future
    scores rather than just hiding rows.
    """
    cfg = load_config(args.config)
    store = Store(cfg.get("db", "jobs.db"))
    n = store.set_status(args.id, "skipped")
    print(f"Marked {n} as passed." if n else f"No job matching id {args.id}")
    if n:
        do_publish(cfg, store)


# A markdown audit report or a plain hand-written list both name rows the
# same way: one id or 8-char id prefix per line. Comments and blank lines
# never match this pattern, so no separate stripping logic is needed for the
# plain-list case - the regex alone does the filtering.
_ID_LINE = re.compile(r"[0-9a-f]{8,64}")


def parse_ids_file(path: str) -> list[str]:
    """Extract job-id prefixes named in a `rescore --ids` file.

    Two accepted shapes: a plain list (one id/prefix per line; '#' comments
    and blank lines are simply lines that never match the id pattern, so they
    fall out on their own), or a markdown audit report - once a line reads
    exactly "## Rows to rescore now", only fully-matching lines after it
    count, since the rest of such a report is prose and tables, not ids.
    """
    lines = Path(path).read_text().splitlines()
    marker = "## Rows to rescore now"
    if marker in lines:
        lines = lines[lines.index(marker) + 1:]
    return [ln.strip() for ln in lines if _ID_LINE.fullmatch(ln.strip())]


def refetch_descriptions(rows: list[dict], cfg: dict) -> int:
    """Best-effort description backfill for a hand-picked `--ids` batch.

    jobs.db never stores a description, so an ordinary rescore judges
    title-only. `--ids` names a small, deliberately chosen set of rows, so
    it's worth paying to refetch what each row's board currently serves
    before re-judging it. Mutates `rows` in place (description, detail_key)
    and returns how many ended up with a non-empty description.

    Firecrawl is metered per call, hn and sndsh404 have no derivable board,
    and simplify's "board" is the feed itself (handled separately below via
    the same per-posting detail-page lookup `enrich-list` uses) - all four
    are skipped in the per-board pass. A Workday board costs ~20 throttled
    POSTs to refetch fully; acceptable here because --ids is a one-off
    hand-run command, not the 12-minute crawl.
    """
    SKIP_ATS = {"firecrawl", "hn", "sndsh404", "simplify"}
    by_board: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        board = sources.derive_board(r.get("url") or "")
        if board is None or board[0] in SKIP_ATS:
            continue
        by_board.setdefault(board, []).append(r)

    by_board_hits = 0
    for (ats, token), board_rows in by_board.items():
        company = board_rows[0]["company"]
        try:
            fetched = sources.fetch(company, ats, token)
        except Exception as e:
            print(f"  ! {ats}:{token}: {type(e).__name__}: {str(e)[:80]}")
            continue
        by_key = {url_key(j["url"]): j for j in fetched if j.get("url")}
        for r in board_rows:
            match = by_key.get(url_key(r.get("url") or ""))
            if match is None:
                continue
            r["description"] = match.get("description") or ""
            if match.get("detail_key"):
                r["detail_key"] = match["detail_key"]
            if r["description"]:
                by_board_hits += 1

    # Simplify rows carry no pollable board of their own; a posting page's id
    # comes from the feed instead, exactly the lookup `cmd_enrich_list` does.
    simplify_rows = [r for r in rows
                     if r.get("source") == "simplify" and not r.get("description")]
    if simplify_rows:
        ids = listing_ids(cfg["companies"])
        for r in simplify_rows:
            pid = ids.get(url_key(r.get("url") or ""))
            if pid:
                r["detail_key"] = f"simplify||{pid}"

    by_detail_hits = sources.enrich(rows, cap=len(rows))

    total = sum(1 for r in rows if r.get("description"))
    print(f"Descriptions: {total} of {len(rows)} refetched "
          f"({by_board_hits} by board, {by_detail_hits} by detail call); "
          f"rest are title-only")
    return total


def cmd_rescore(args):
    """Re-judge rows against the current profile.

    Exists because scores are frozen at first sight: dedupe means a posting is
    scored once, ever, so tightening or loosening profile.md does nothing for
    postings already in the store. The default is a score band (title-only,
    descriptions are not stored). `--ids FILE` instead re-judges exactly the
    rows it names, first refetching each one's description where possible -
    worth the cost for a small, hand-picked batch.
    """
    cfg = load_config(args.config)
    profile = load_profile(args.profile)
    store = Store(cfg.get("db", "jobs.db"))
    f = cfg["filters"]
    threshold = f.get("min_score", 65)
    cycles = prefilter.parse_cycles(f.get("target_cycles", []))

    if args.ids:
        prefixes = parse_ids_file(args.ids)
        rows = store.rows_by_id_prefix(prefixes)
        found = {r["id"] for r in rows}
        resolved = [p for p in prefixes if any(i.startswith(p) for i in found)]
        unresolved = [p for p in prefixes if p not in resolved]
        print(f"{len(resolved)} of {len(prefixes)} ids resolved to a live row")
        if unresolved:
            print(f"  ! not found: {', '.join(unresolved)}")
        if not rows:
            print("No live postings match the given ids.")
            return
        jobs = [{**dict(r), "description": ""} for r in rows]
        refetch_descriptions(jobs, cfg)
        jobs, regated = split_regated(jobs)
        if not jobs:
            print("Nothing left to score.")
            return
    else:
        regated = set()
        # The near-miss band tracks the configured threshold: the 20 points
        # just under it. Hardcoded 45-64 defaults were designed for
        # min_score 65 and silently pointed at already-surfaced rows once
        # the threshold moved.
        lo = args.min if args.min is not None else max(0, threshold - 20)
        hi = args.max if args.max is not None else max(0, threshold - 1)
        rows = store.scored_band(lo, hi)
        if not rows:
            print(f"No live postings scored {lo}-{hi}.")
            return
        print(f"Rescoring {len(rows)} postings scored {lo}-{hi} "
              f"against the current profile...")
        jobs = [{**dict(r), "description": ""} for r in rows]

    # Captured before score.run overwrites the "score" key on each dict.
    old_scores = {j["id"]: j["score"] for j in jobs}

    rated = score.run(jobs, profile, cycles=cycles,
                      effort=scorer_effort(cfg),
                      feedback=store.feedback(min_score=threshold))
    # A failed batch comes back as the score-50 "scoring failed" fallback.
    # Fine for never-scored postings; catastrophic here, where it would
    # overwrite real scores that exist nowhere else. Keep those rows as-is.
    failed = [j for j in rated if j.get("why") == "scoring failed"]
    if failed:
        print(f"  ! {len(failed)} rows kept their old scores (batch failed)")
        rated = [j for j in rated if j.get("why") != "scoring failed"]
    # Rescore predates the scorer's us location flag; without this a foreign
    # row re-judged with a high fit score would resurface into the digest.
    # Same treatment as cmd_run: real score kept, tag 'filtered', suppressed.
    offscope = [j for j in rated if not j.get("us", True)]
    if offscope:
        for j in offscope:
            j["tag"] = "filtered"
        rated = [j for j in rated if j.get("us", True)]
        store.update_scores(offscope)
        # mark_skipped forces status='skipped' unconditionally - fine for a
        # 'new' row (the only status scored_band ever returns), but --ids can
        # reach an 'applied' row, and flipping "I applied" to "skipped" over
        # a location re-judgment is real data loss. Only rows still 'new' get
        # suppressed this way; an already-applied/skipped row keeps its
        # status (its score/tag are still updated above).
        store.mark_skipped([j["id"] for j in offscope if j.get("status") == "new"])
        print(f"  {len(offscope)} resolved to no US office; suppressed")

    # A clearance-gated row the fixed gate now passes was never seen by the
    # user, so one that clears the threshold goes back to status 'new' and
    # into the next digest. One that does not stays skipped, and
    # update_scores keeps its tag 'filtered' so store.feedback never reads it
    # as a posting the user passed on.
    regated_rows = [j for j in rated if j["id"] in regated]
    rated_rest = [j for j in rated if j["id"] not in regated]
    readmitted = store.readmit([j for j in regated_rows if j["score"] >= threshold])
    buried = [j for j in regated_rows if j["score"] < threshold]
    if buried:
        # The why keeps its clearance marker (the scorer's reason goes after
        # it), so split_regated recognizes the row on any later `rescore
        # --ids`: one low score from a model that re-read the citizenship
        # sentence as a gate must not bury the row beyond recovery.
        for j in buried:
            j["why"] = f"{CLEARANCE_WHY} (rescored: {j.get('why') or ''})"
        store.update_scores(buried)
    if regated_rows:
        print(f"  {readmitted} clearance-gated rows readmitted; "
              f"{len(buried)} scored below {threshold} and stay suppressed")

    # update_scores' resurface_min only makes sense for rows still 'new' - an
    # applied/skipped row (only reachable via --ids; scored_band already
    # scopes to 'new') must keep its status and never jump back into the
    # digest just because a rescore lifted its score.
    # "Suppressed" means never emailed: a row between min_score and the
    # email floor sat on the tracker but was marked notified without a send
    # (send_pending_digest), so lifting it over the floor is a rescue, not
    # a repeat. Resurfacing only at min_score left Decagon's Summer 2027
    # intern silent at 20 -> 92 on 2026-10-11, after the row had been
    # "notified" at 20 without ever reaching the inbox.
    live = [j for j in rated_rest if j.get("status") == "new"]
    others = [j for j in rated_rest if j.get("status") != "new"]
    email_floor = f.get("email_min_score")
    rescue_min = max(threshold, email_floor) if email_floor is not None else threshold
    resurfaced = store.update_scores(live, resurface_min=rescue_min)
    if others:
        store.update_scores(others)
    print(f"  {resurfaced} now clear {rescue_min} and will be in the next digest")

    up = down = 0
    for j in sorted(rated, key=lambda j: -j["score"]):
        old = old_scores.get(j["id"])
        old_disp = old if old is not None else "-"
        print(f"  {old_disp:>3} -> {j['score']:>3}  {j['company']} - "
              f"{j['title'][:56]}")
        if old is not None:
            if old < threshold <= j["score"]:
                up += 1
            elif old >= threshold > j["score"]:
                down += 1
    print(f"  {up} crossed {threshold} upward, {down} crossed it downward")

    # A row that fell below threshold must leave the tracker just as surely
    # as a resurfaced one must join it.
    dropped = any(old_scores.get(j["id"]) is not None
                  and old_scores[j["id"]] >= threshold > j["score"]
                  for j in live)
    if resurfaced or dropped or readmitted:
        do_publish(cfg, store)


def split_regated(jobs: list[dict]) -> tuple[list[dict], set[str]]:
    """Re-run the clearance gate on refetched rows it buried; drop the still-gated.

    A row the post-enrich gate stored as skipped (tag 'filtered', why
    starting 'requires security clearance'; score NULL until a rescore
    scores it below the threshold) was never seen by the user. When the gate is fixed, `rescore --ids` is how such
    rows get a second look: the refetched text goes back through
    prefilter.clearance_gated. A row whose text is still gated, or that has
    no text to check (closed posting, failed refetch), is left untouched -
    scoring it title-only would undo the gate on no evidence. Returns the
    jobs to score and the ids of the gated rows among them.
    """
    keep, regated, held = [], set(), 0
    for j in jobs:
        if (j.get("status") == "skipped" and j.get("tag") == "filtered"
                and (j.get("why") or "").startswith(CLEARANCE_WHY)):
            if not j.get("description") or prefilter.clearance_gated(j["description"]):
                held += 1
                continue
            regated.add(j["id"])
        keep.append(j)
    if held or regated:
        print(f"  clearance-gated rows: {len(regated)} pass the gate now, "
              f"{held} still gated or without text (left untouched)")
    return keep, regated


def listing_ids(companies: list[dict]) -> dict[str, str]:
    """url_key -> simplify posting id, from every configured Simplify feed.

    Reads the feed WHOLE, inactive history included: a stored row whose
    listing has since been deactivated still has a posting page, and the
    active slice alone reaches only 83% of them against 97% for the full
    file. Forks that mint their own ids are skipped - `source` must say
    Simplify, or the id resolves to a 404.
    """
    out: dict[str, str] = {}
    for c in companies:
        if c.get("ats") != "simplify":
            continue
        try:
            data = sources.get(c["token"], headers=sources.UA, timeout=60).json()
        except Exception as e:
            print(f"  ! feed {c['name']} failed: {type(e).__name__}")
            continue
        for x in data if isinstance(data, list) else []:
            if not isinstance(x, dict) or x.get("source") != "Simplify":
                continue
            u, i = x.get("url") or "", x.get("id")
            if u.startswith("http") and i:
                out.setdefault(url_key(u), str(i))
    return out


def cmd_enrich_list(args):
    """Judge the aggregator rows that surfaced without ever being read.

    Feed listings carry no description, so they bypassed both the text gates
    and the scorer and surfaced on policy alone as an unvetted "-". That was
    the right call while the text was unreachable; it stopped being right on
    2026-08-30, when the feed's own `id` turned out to be the id of its
    posting page on simplify.jobs, where the full description sits. This is
    the backfill for rows stored before that: fetch the text, run the same
    clearance/US gates a directly-polled posting gets, and score what
    survives.

    Rows that score below the threshold keep status 'new' and simply stop
    surfacing - `store.surfaced` ranks on score, so no suppression flag is
    needed and none is set. That also keeps them out of `store.feedback`:
    nobody passed on them, a machine did.
    """
    cfg = load_config(args.config)
    profile = load_profile(args.profile)
    store = Store(cfg.get("db", "jobs.db"))
    f = cfg["filters"]
    threshold = f.get("min_score", 65)

    rows = store.listing_rows()
    if not rows:
        print("No unjudged listing rows.")
        return
    ids = listing_ids(cfg["companies"])
    print(f"{len(rows)} unjudged listing rows; {len(ids)} feed entries indexed")

    jobs, unmatched = [], 0
    for r in rows:
        pid = ids.get(url_key(r["url"] or ""))
        if not pid:
            unmatched += 1
            continue
        jobs.append({**dict(r), "description": "",
                     "detail_key": f"simplify||{pid}"})
    jobs = jobs[:args.limit] if args.limit else jobs
    print(f"  {len(jobs)} matched to a posting page, {unmatched} unreachable "
          f"(listing gone from the feed entirely)")
    if not jobs:
        return

    got = sources.enrich(jobs, cap=len(jobs))
    jobs = [j for j in jobs if len(j.get("description") or "") >= 200]
    print(f"  {got} descriptions fetched, {len(jobs)} usable")
    if not jobs:
        return

    jobs, cleared = split_cleared(jobs)
    if cleared:
        print(f"  {len(cleared)} require a security clearance or US-person "
              f"status:")
        for j in cleared[:20]:
            print(f"      {j['company']} - {j['title'][:60]}")

    if args.dry_run:
        print(f"\n[dry run] would gate {len(cleared)} and score {len(jobs)}; "
              f"nothing written")
        return

    if cleared:
        for j in cleared:
            j["tag"] = "filtered"
            j["why"] = CLEARANCE_WHY
            j["score"] = None
        store.update_scores(cleared)
        store.mark_skipped([j["id"] for j in cleared])

    cycles = prefilter.parse_cycles(f.get("target_cycles", []))
    print(f"Scoring {len(jobs)} with Claude...")
    rated = score.run(jobs, profile, cycles=cycles,
                      effort=scorer_effort(cfg),
                      feedback=store.feedback(min_score=threshold))
    # Same rule as rescore: the score-50 fallback must never be persisted.
    # Here it would be worse than a bad score - it would look like a real
    # judgment on a row whose whole problem was that nobody had judged it.
    failed = [j for j in rated if j.get("why") == "scoring failed"]
    if failed:
        print(f"  ! {len(failed)} rows left unjudged (batch failed); rerun")
        rated = [j for j in rated if j.get("why") != "scoring failed"]
    offscope = [j for j in rated if not j.get("us", True)]
    if offscope:
        for j in offscope:
            j["tag"] = "filtered"
        rated = [j for j in rated if j.get("us", True)]
        store.update_scores(offscope)
        store.mark_skipped([j["id"] for j in offscope])
        print(f"  {len(offscope)} resolved to no US office; suppressed")
    store.update_scores(rated)
    keep = [j for j in rated if j["score"] >= threshold]
    print(f"  {len(keep)} of {len(rated)} clear {threshold} and keep their "
          f"place; {len(rated) - len(keep)} drop off the tracker")
    for j in sorted(keep, key=lambda x: -x["score"])[:25]:
        print(f"  {j['score']:>3}  {j['company']} - {j['title'][:56]}")
    do_publish(cfg, store)


def cmd_unstale(args):
    """Re-admit postings that the age cutoff wrongly buried.

    The cutoff is meant for the GitHub aggregator feeds, where an old listing
    really has been seen already. It used to apply to directly polled boards
    too, where age means nothing: a board's first crawl backfills everything
    it holds, so every long-open internship got filed as stale and never
    scored. This scores those rows for the first time and promotes the ones
    that clear the threshold. Aggregator rows are left alone - for those the
    cutoff is doing its job.
    """
    cfg = load_config(args.config)
    profile = load_profile(args.profile)
    store = Store(cfg.get("db", "jobs.db"))
    f = cfg["filters"]
    threshold = f.get("min_score", 65)

    rows = store.stale_rows(exclude_sources=AGGREGATOR_SOURCES)
    if not rows:
        print("No wrongly-staled postings to recover.")
        return
    print(f"Recovering {len(rows)} postings suppressed as stale on directly "
          f"polled boards...")
    if args.dry_run:
        for r in rows[:20]:
            print(f"  {r['source']:<16} {r['company']} - {r['title'][:56]}")
        print(f"  ... {len(rows)} total; re-run without --dry-run to score them")
        return

    jobs = [{**dict(r), "description": ""} for r in rows]
    cycles = prefilter.parse_cycles(f.get("target_cycles", []))
    rated = score.run(jobs, profile, cycles=cycles,
                      effort=scorer_effort(cfg),
                      feedback=store.feedback(min_score=threshold))
    # These rows have no real score to protect (they were never scored), but a
    # failed batch would still promote junk at the fallback 50, so drop them
    # and leave those rows for the next attempt.
    failed = [j for j in rated if j.get("why") == "scoring failed"]
    if failed:
        print(f"  ! {len(failed)} left suppressed (batch failed); rerun to retry")
        rated = [j for j in rated if j.get("why") != "scoring failed"]
    promoted = store.unstale(rated, threshold)
    print(f"  {promoted} clear {threshold} and are back in circulation")
    for j in sorted(rated, key=lambda x: -x["score"])[:15]:
        if j["score"] >= threshold:
            print(f"  {j['score']:>3}  {j['company']} - {j['title'][:56]}")
    if promoted:
        do_publish(cfg, store)


def cmd_publish(args):
    cfg = load_config(args.config)
    if not cfg.get("publish"):
        print("No publish block configured in config.yaml.")
        return 1
    do_publish(cfg, Store(cfg.get("db", "jobs.db")))


def _twin_line(tag: str, r) -> str:
    score = "-" if r["score"] is None else r["score"]
    return (f"  {tag:<5} {r['id']} {r['status']:<7} {r['source']:<10} "
            f"{score!s:>3} {(r['first_seen'] or '')[:10]} "
            f"{r['company']} | {r['title']}")


def cmd_dedupe_urls(args):
    """Close duplicate rows of one posting (rows sharing a url_key).

    Read-only unless --apply: the dry run opens the database mode=ro and
    takes no lock, so it is safe next to a scheduled run. See
    store.url_twin_groups for which row is kept and which are closed.
    """
    cfg = load_config(args.config)
    path = cfg.get("db", "jobs.db")
    min_score = cfg["filters"].get("min_score", 65)
    if args.apply:
        store = Store(path)
        db = store.db
    else:
        import sqlite3
        if not Path(path).exists():
            print(f"No database at {path}.")
            return 1
        db = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
    groups = url_twin_groups(db, min_score)
    to_close = [r["id"] for g in groups for r in g["close"]]
    for g in groups:
        print(f"== {g['key']}")
        print(_twin_line("keep", g["keep"]))
        for r in g["close"]:
            print(_twin_line("CLOSE", r))
        for r in g["leave"]:
            print(_twin_line("leave", r))
    print(f"\n{len(groups)} postings stored more than once; "
          f"{len(to_close)} duplicate 'new' rows to close "
          f"({sum(1 for g in groups if g['close'])} groups)")
    if not args.apply:
        print("[dry run] nothing written; rerun with --apply to close them")
        return 0
    n = store.close_url_twins(to_close)
    print(f"Closed {n} rows. The tracker pages refresh on the next publish.")
    return 0


def cmd_stats(args):
    cfg = load_config(args.config)
    store = Store(cfg.get("db", "jobs.db"))
    min_score = cfg.get("filters", {}).get("min_score", 65)
    for k, v in store.stats(min_score=min_score).items():
        print(f"  {k:<10} {v}")


def cmd_watch(args):
    """One tick of the minute-level board watch (see watch.py).

    Exits 0 even when a board fails: this runs from a per-minute timer, and
    a service failure per tick would fire the OnFailure alert sixty times an
    hour during any outage. Failures count in the state file instead, and
    one alert goes out per `watch_alert_after` consecutive misses.
    """
    cfg = load_config(args.config)
    entries = cfg.get("watch") or []
    if not entries:
        print("  nothing under `watch:` in config.yaml")
        return 0
    state = watch.load_state(args.state)
    alerts, errors = watch.check(entries, state)
    for name, jobs in alerts.items():
        subject, md = watch.render(name, jobs)
        try:
            sent = notify.send_email(md, subject)
        except Exception as e:
            sent = False
            print(f"  ! alert email failed: {type(e).__name__}: {str(e)[:80]}")
        for j in jobs:
            print(f"  NEW {name}: {j['title']} | {j.get('location', '')} | {j['url']}")
        if not sent:
            # No mailbox (or SMTP down): the printout above, in the journal,
            # is the delivery. The crawl still emails it within the window.
            print(f"  {len(jobs)} alert(s) for {name} not emailed")
    limit = cfg.get("filters", {}).get("watch_alert_after", 60)
    for name, err in errors.items():
        key = watch.board_key(next(e for e in entries if e["name"] == name))
        streak = state["fail_streak"].get(key, 0)
        print(f"  ! {name}: {err} ({streak} in a row)")
        if limit and streak and streak % limit == 0:
            try:
                notify.send_email(
                    f"The `{name}` watch has failed {streak} ticks in a row.\n\n"
                    f"Last error: {err}",
                    f"ALERT: jobcrawl watch on {name} failing")
            except Exception as e:
                print(f"  ! failure alert email failed: {type(e).__name__}")
    watch.save_state(state, args.state)
    if not alerts and not errors:
        print(f"  {len(entries)} watched board(s), nothing new")
    return 0


_EFFORTS = ["low", "medium", "high"]


def bump_effort(config_path: str, current: str, to: str = "high") -> str | None:
    """Raise `filters.scorer_effort` in config.yaml; the new value, or None.

    Edits the one line in place so the file's comments survive; the change
    is committed by the audit wrapper and pulled by the next crawl. Nothing
    to do when the effort is already at or above `to`, or the line is
    missing (then the file was hand-edited past this shape - say so).
    """
    if _EFFORTS.index(current) >= _EFFORTS.index(to):
        return None
    text = Path(config_path).read_text()
    new, n = re.subn(r"(?m)^(\s*scorer_effort:\s*)\w+", rf"\g<1>{to}", text, count=1)
    if not n:
        print(f"  ! no scorer_effort line in {config_path}; effort not raised")
        return None
    Path(config_path).write_text(new)
    return to


def cmd_audit_daily(args):
    """Audit one day's judgments with a stronger model, act on the answer.

    Read-only on the store. Writes audits/daily-<date>.md (whose tail is the
    id list `rescore --ids` takes, so the wrapper re-judges the flagged rows
    with fresh descriptions), mails the report when anything was flagged,
    and raises `filters.scorer_effort` in config.yaml once a day's
    high-confidence mistakes reach `filters.audit_bump_after` - the scorer
    switched to a small model on 2026-09-28 and the user asked for the
    effort to climb on its own if it makes mistakes. Prints "Report: <path>"
    for the wrapper.
    """
    cfg = load_config(args.config)
    f = cfg.get("filters", {})
    profile = load_profile(args.profile)
    cycles_text = score._describe_cycles(prefilter.parse_cycles(f.get("target_cycles", [])))
    scorer_system = score.SYSTEM.format(cycles=cycles_text)
    rows = audit.collect_recent_rows(cfg.get("db", "jobs.db"), args.hours)
    if not rows:
        print(f"No rows judged in the last {args.hours} h; nothing to audit.")
        return
    print(f"Auditing {len(rows)} rows judged in the last {args.hours} h "
          f"with {args.model} at effort {args.effort}...")
    sys.stdout.flush()
    result = {"summary": [], "mistakes": []}
    usage_in = usage_out = 0
    for start in range(0, len(rows), args.chunk):
        chunk = rows[start:start + args.chunk]
        body = audit.build_daily_request(profile, scorer_system, chunk,
                                         args.model, args.effort)
        text, usage, stop = audit.call_daily(body, args.model)
        part = audit.parse_daily(text)
        result["summary"].append(part["summary"])
        result["mistakes"] += part["mistakes"]
        usage_in += usage.get("prompt_tokens", 0)
        usage_out += usage.get("completion_tokens", 0)
    # One paragraph per chunk: a backfill day audits 1,100 rows in ten
    # chunks, and joined with spaces that read as one 4,000-word paragraph
    # (2026-09-29).
    result["summary"] = "\n\n".join(s for s in result["summary"] if s)
    known = {str(r["id"])[:8] for r in rows}
    invented = [m["id"] for m in result["mistakes"] if m["id"] not in known]
    if invented:
        print(f"  ! auditor named {len(invented)} ids not in the rows; dropped")
    seen = set()
    result["mistakes"] = [m for m in result["mistakes"]
                          if m["id"] in known and not (m["id"] in seen or seen.add(m["id"]))]
    high = [m for m in result["mistakes"] if m["confidence"] == "high"]
    meta = {"model": args.model, "effort": args.effort, "rows": len(rows),
            "date": f"{_dt.datetime.now(_dt.timezone.utc):%Y-%m-%d}",
            "input_tokens": usage_in, "output_tokens": usage_out}
    current = scorer_effort(cfg)
    if len(high) >= f.get("audit_bump_after", 3):
        raised = bump_effort(args.config, current)
        if raised:
            meta["effort_raised"] = raised
    report = audit.render_daily_report(result, rows, meta)
    out = args.out or f"audits/daily-{meta['date']}.md"
    path = audit.write_report(out, report, meta)
    print(f"{len(result['mistakes'])} mistakes flagged ({len(high)} high confidence) "
          f"in {len(rows)} rows; {usage_in} in / {usage_out} out tokens")
    if meta.get("effort_raised"):
        print(f"Scorer effort raised {current} -> {meta['effort_raised']} in {args.config}")
    if result["mistakes"] or meta.get("effort_raised"):
        subject = (f"jobcrawl scorer audit: {len(result['mistakes'])} mistakes "
                   f"({len(high)} high)")
        try:
            sent = notify.send_email(report, subject)
        except Exception as e:
            sent = False
            print(f"  ! audit email failed: {type(e).__name__}: {str(e)[:80]}")
        print("Emailed." if sent else "Not emailed (no SMTP or send failed).")
    print(f"Report: {path}")


def cmd_audit_scorer(args):
    """One-off deep audit of the scorer against every judged row in jobs.db.

    Unlike `rescore`, this never writes anything to the store - it is a
    single Fable 5.1 call over the whole judged history (scores, tags,
    reasons) plus the scorer's own prompt and the candidate's profile,
    looking for systematic mistakes rather than re-judging individual rows.
    `--dry-run` prices the call with count_tokens and spends nothing.
    """
    cfg = load_config(args.config)
    f = cfg.get("filters", {})
    db = cfg.get("db", "jobs.db")
    profile = load_profile(args.profile)
    cycles_text = score._describe_cycles(prefilter.parse_cycles(f.get("target_cycles", [])))
    scorer_system = score.SYSTEM.format(cycles=cycles_text)

    rows = audit.collect_rows(db)
    if not rows:
        print("No judged rows in the store to audit.")
        return

    body = audit.build_request(profile, scorer_system, rows, args.model, args.effort, cycles_text)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set (jobcrawl/.env)")

    if args.dry_run:
        n = audit.count_tokens(body, key)
        print(f"Rows: {len(rows)}")
        print(f"Input tokens: {n}")
        print(f"Estimated cost: ${audit.estimate_cost(n, 20000):.2f} "
              f"(input at $10/M plus an assumed 20k output at $50/M)")
        return

    print(f"Auditing {len(rows)} rows with {args.model} at effort {args.effort} "
          f"- this can take several minutes...")
    sys.stdout.flush()
    text, usage, stop = audit.call_streaming(body, key)
    if stop == "refusal":
        print(f"Refused: {usage.get('stop_details')}")
        raise SystemExit(1)
    if not text.strip():
        raise RuntimeError(f"empty response (stop_reason={stop}, usage={usage})")

    out = args.out or f"audits/scorer-audit-{_dt.datetime.now(_dt.timezone.utc):%Y-%m-%d}.md"
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cost = round(audit.estimate_cost(input_tokens, output_tokens), 2)
    meta = {
        "model": args.model,
        "effort": args.effort,
        "rows": len(rows),
        "date": f"{_dt.datetime.now(_dt.timezone.utc):%Y-%m-%d}",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "stop_reason": stop,
        "estimated_cost": cost,
    }
    path = audit.write_report(out, text, meta)
    print(f"Report: {path}")
    print(f"Input tokens: {input_tokens}  Output tokens: {output_tokens}  "
          f"Estimated cost: ${cost:.2f}")


def rotate_log(path: str = "run.log", max_bytes: int = 2_000_000) -> None:
    """Keep the cron log bounded: one rotation, run.log -> run.log.1.

    The rename is safe mid-write: cron's shell holds an fd that follows the
    renamed file, and the next run's `>>` creates a fresh run.log.
    """
    p = Path(path)
    try:
        if p.is_file() and p.stat().st_size > max_bytes:
            p.replace(f"{path}.1")
    except OSError:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="jobcrawl")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="check all configured boards resolve")
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("run", help="fetch, filter, score, notify")
    r.add_argument("--profile", default="profile.md")
    r.add_argument("--no-score", action="store_true", help="skip the LLM pass")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("boards", help="regenerate the derived board files from public lists")
    b.add_argument("--out", default="boards_broad.yaml")
    b.add_argument("--cold-out", default="boards_cold.yaml",
                   help="where the historical (cold-tier) boards are written")
    b.add_argument("--applied", default="boards_applied.yaml",
                   help="import-simplify's board file, excluded from the cold tier")
    b.add_argument("--vc-out", default="boards_vc.yaml",
                   help="where the VC-portfolio-derived boards are written")
    b.add_argument("--yc-out", default="boards_yc.yaml",
                   help="where the YC-sweep-derived boards are written")
    b.add_argument("--resolved-out", default="boards_resolved.yaml",
                   help="where website-resolved boards are written")
    b.add_argument("--resolve-cache", default="resolve_cache.json",
                   help="per-company resolver memory (generated, committed)")
    b.add_argument("--resolve-budget", type=int, default=None,
                   help="companies to resolve this run (default filters.resolve_per_run)")
    b.add_argument("--no-resolve", action="store_true",
                   help="skip the website resolver stage entirely")
    b.set_defaults(func=cmd_boards)

    im = sub.add_parser("import-simplify",
                        help="import a Simplify tracker CSV: mark applied, mine boards")
    im.add_argument("csv", help="path to the exported CSV")
    im.add_argument("--dry-run", action="store_true",
                    help="report only; write neither the database nor the board file")
    im.add_argument("--no-verify", action="store_true",
                    help="skip the live board check (the slow half); reports "
                         "candidates unverified and writes nothing")
    im.add_argument("--out", default="boards_applied.yaml")
    im.set_defaults(func=cmd_import_simplify)

    l = sub.add_parser("list", help="show pending postings")
    l.add_argument("--limit", type=int, default=30)
    l.set_defaults(func=cmd_list)

    a = sub.add_parser("applied", help="mark a posting as applied")
    a.add_argument("id")
    a.set_defaults(func=cmd_applied)

    sk = sub.add_parser("skipped", help="dismiss a surfaced posting (negative feedback)")
    sk.add_argument("id")
    sk.set_defaults(func=cmd_skipped)

    pb = sub.add_parser("publish", help="push the tracker README to GitHub")
    pb.set_defaults(func=cmd_publish)

    fd = sub.add_parser("feeds", help="discover new GitHub aggregator feeds")
    fd.add_argument("--add", action="store_true", help="write vetted feeds to the include file")
    fd.add_argument("--out", default="feeds_discovered.yaml")
    fd.set_defaults(func=cmd_feeds)

    au = sub.add_parser("audit", help="measure the prefilter's false-negative rate")
    au.add_argument("--sample", type=int, default=30, help="max postings to score")
    au.add_argument("--per-reason", type=int, default=3,
                    help="how many to sample from each drop reason")
    au.add_argument("--reason", help="only audit drop reasons matching this text")
    au.add_argument("--seed", type=int, default=None, help="fix the sample for reproducibility")
    au.add_argument("--profile", default="profile.md")
    au.set_defaults(func=cmd_audit)

    rs = sub.add_parser("rescore", help="re-judge a score band (or an --ids "
                                        "list, with description refetch) "
                                        "after a profile change")
    rs.add_argument("--min", type=int, default=None,
                    help="band floor (default: min_score - 20)")
    rs.add_argument("--max", type=int, default=None,
                    help="band ceiling (default: min_score - 1)")
    rs.add_argument("--ids", default=None,
                    help="rescore exactly the ids/prefixes named in this file "
                         "(a scorer-audit report or a plain list), with "
                         "description refetch where possible - overrides "
                         "--min/--max")
    rs.add_argument("--profile", default="profile.md")
    rs.set_defaults(func=cmd_rescore)

    el = sub.add_parser("enrich-list",
                        help="fetch descriptions for unjudged aggregator rows "
                             "and gate/score them")
    el.add_argument("--profile", default="profile.md")
    el.add_argument("--limit", type=int, default=0,
                    help="only process the N newest matched rows")
    el.add_argument("--dry-run", action="store_true",
                    help="show what would be gated and scored, write nothing")
    el.set_defaults(func=cmd_enrich_list)

    us = sub.add_parser("unstale",
                        help="score postings the age cutoff wrongly buried")
    us.add_argument("--profile", default="profile.md")
    us.add_argument("--dry-run", action="store_true",
                    help="list what would be recovered without spending on the LLM")
    us.set_defaults(func=cmd_unstale)

    as_ = sub.add_parser("audit-scorer",
                         help="one-off Fable audit of the scorer's systematic "
                              "mistakes -> audits/*.md; --dry-run prints "
                              "token count and cost")
    as_.add_argument("--dry-run", action="store_true")
    as_.add_argument("--model", default=audit.DEFAULT_MODEL)
    as_.add_argument("--effort", default="high",
                     choices=["low", "medium", "high", "xhigh", "max"])
    as_.add_argument("--out", default=None)
    as_.add_argument("--profile", default="profile.md")
    as_.set_defaults(func=cmd_audit_scorer)

    ad = sub.add_parser("audit-daily",
                        help="audit the last day's judgments with a stronger "
                             "model -> audits/daily-*.md; mails mistakes and "
                             "raises filters.scorer_effort when they pile up")
    ad.add_argument("--hours", type=int, default=24)
    ad.add_argument("--model", default=audit.DAILY_MODEL)
    ad.add_argument("--effort", default=audit.DAILY_EFFORT,
                    choices=["low", "medium", "high", "xhigh", "max"])
    ad.add_argument("--chunk", type=int, default=audit.DAILY_CHUNK)
    ad.add_argument("--out", default=None)
    ad.add_argument("--profile", default="profile.md")
    ad.set_defaults(func=cmd_audit_daily)

    w = sub.add_parser("watch", help="one tick of the minute-level board watch "
                       "(config.yaml `watch:`); emails new intern titles at once")
    w.add_argument("--state", default=watch.STATE_FILE,
                   help="ETag + seen-URL state file (default watch_state.json)")
    w.set_defaults(func=cmd_watch)

    dd = sub.add_parser("dedupe-urls",
                        help="close duplicate rows of one posting (a shared "
                             "url_key); read-only unless --apply")
    dd.add_argument("--apply", action="store_true",
                    help="write: close the duplicate 'new' rows listed")
    dd.set_defaults(func=cmd_dedupe_urls)

    s = sub.add_parser("stats")
    s.set_defaults(func=cmd_stats)

    load_dotenv()
    rotate_log()
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
