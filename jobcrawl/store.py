"""
SQLite store. Deliberately not Supabase — this is a single-user cron job with no
concurrency and no frontend, so a file you can `sqlite3` into beats a network
round trip. The schema maps cleanly to Postgres if you ever want the dashboard.

Dedupe is the whole point: you only get told about a posting once, ever.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import sqlite3
import threading
import time
import datetime as dt
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import prefilter

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT,
    url         TEXT NOT NULL,
    source      TEXT,
    posted_at   TEXT,
    first_seen  TEXT NOT NULL,
    score       INTEGER,
    why         TEXT,
    cycle       TEXT,
    tag         TEXT,
    notified    INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'new'   -- new | applied | skipped | closed
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_seen   ON jobs(first_seen);

CREATE TABLE IF NOT EXISTS runs (
    ts          TEXT PRIMARY KEY,
    fetched     INTEGER,
    prefiltered INTEGER,
    scored      INTEGER,
    new_jobs    INTEGER,
    errors      TEXT
);

CREATE TABLE IF NOT EXISTS audits (
    ts          TEXT PRIMARY KEY,
    sampled     INTEGER,
    misses      INTEGER,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

-- Office list a detail page gave for an UNSTORED posting url (cmd_run's
-- pre-dedupe pass over vague locations). A url whose row filter_new drops
-- never reaches jobs.url, so without this it is re-fetched every poll.
-- location '' = the page answered with no office (Avature never has one).
CREATE TABLE IF NOT EXISTS detail_cache (
    url_key     TEXT PRIMARY KEY,
    location    TEXT,
    fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS boards (
    key         TEXT PRIMARY KEY,   -- "ats:token"
    name        TEXT,
    etag        TEXT,               -- for If-None-Match conditional fetches
    fail_streak INTEGER DEFAULT 0,  -- consecutive fetch errors
    empty_streak INTEGER DEFAULT 0, -- consecutive 0-posting successes
    last_ok     TEXT,
    last_error  TEXT,
    last_try    TEXT,               -- every attempt, success or not
    last_yield  TEXT                -- last run this board produced a new posting
);
"""


# Sources that relay other people's listings rather than a company's own
# board. Their rows lose to a direct fetch of the same board.
AGGREGATOR_SOURCES = {"simplify", "sndsh404"}


def row_id(job: dict) -> str:
    """The stored id a fetched row belongs to.

    Normally its job_id. cmd_run's pre-dedupe enrichment sets `_legacy_id`
    on a row whose stored twin was hashed from the EMPTY location before
    2026-09-25 and carries the same office: that row keeps matching the
    stored row by hash, as it did before enrichment moved ahead of the hash.
    """
    return job.get("_legacy_id") or job_id(job)


def job_id(job: dict) -> str:
    """Stable across reposts of the same role at the same company.

    All three fields are stripped: boards ship values with stray whitespace,
    and " Acme" vs "Acme" must not double-store one posting. (Rows stored
    before this normalization are still covered by filter_new's URL identity.)
    """
    basis = (f"{job['company'].lower().strip()}|{job['title'].lower().strip()}|"
             f"{job.get('location', '').lower().strip()}")
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


# 5+, not 7+: iCIMS job ids are 5 digits ("/jobs/11206/..."), and at 7 the
# same posting linked by the Simplify feed and by the direct board kept two
# identities and double-listed (By Light, 2026-08-24). 5 still excludes the
# one digit run that appears in nearly every posting slug - a 4-digit year.
_URL_ID = re.compile(r"\d{5,}")


def url_key(url: str) -> str:
    """Canonical identity of a posting URL, for dedupe and presence.

    The same posting reaches us in different URL dressings: one list links
    Microsoft's /careers/job/<id> page directly, another links a search-results
    page carrying the same id in a ?pid= query. Long digit runs in ATS URLs
    are job ids, so same host + same id set = same posting regardless of the
    path around them. URLs with no long digit run keep their exact string as
    identity, which is the pre-existing behavior.
    """
    if not url:
        return ""
    # A set: iCIMS serves one requisition both as /jobs/27742/<title-slug>
    # and as /jobs/27742/2026-27742/job, and counting the repeat made them
    # two identities (Waters, 2026-09-25).
    # Digits are read from the DECODED url: an escape next to a year
    # ('%282027' is '(2027') otherwise minted a fake id 282027 that two
    # different Analysis Group requisitions shared.
    ids = sorted(set(_URL_ID.findall(unquote(unquote(url)))))
    if not ids:
        # Lowercased: workday emits the same path in either case ("/En-US/"
        # vs "/en-US/"), and a case difference is never two postings.
        return url.lower()
    return f"{urlparse(url).netloc.lower()}|{','.join(ids)}"


# _status_rank values. Below _MACHINE_SKIP a row holds its posting: it is
# live or carries the user's decision.
_MACHINE_SKIP = 3


def _status_rank(r, min_score: int) -> int:
    """Applied 0, a human skip 1, new 2, a machine skip 3, closed 4.

    A human skip is the store.feedback definition: a surfaced row the user
    dismissed. A machine skip (stale, filtered, or below the threshold) was
    never shown to anyone.
    """
    st, tag, score = r["status"], r["tag"], r["score"]
    if st == "applied":
        return 0
    if st == "skipped":
        human = (tag is not None and tag not in ("stale", "filtered")
                 and ((score is not None and score >= min_score)
                      or (score is None and tag == "list")))
        return 1 if human else _MACHINE_SKIP
    return 2 if st == "new" else 4


def _twin_order(rows, min_score: int = 65) -> list:
    """Rows of one posting (a shared url_key), the one to keep first.

    Status first (_status_rank), then a board's own row over an
    aggregator's copy, then the newest first_seen.
    """
    newest = sorted(rows, key=lambda r: r["first_seen"] or "", reverse=True)
    return sorted(newest, key=lambda r: (_status_rank(r, min_score),
                                         r["source"] in AGGREGATOR_SOURCES))


def url_twin_groups(db: sqlite3.Connection, min_score: int = 65) -> list[dict]:
    """Rows stored more than once under one url_key, and how to resolve them.

    One url_key is one posting, but rows stored before a url_key refinement
    (5+ digit runs on 2026-08-24, the id SET on 2026-09-25) or through two
    feeds' different titles can hold the same posting twice - Zipline's
    greenhouse rows and their Simplify copies, an applied posting with a
    live un-applied twin. Per group: `keep` is the first row of _twin_order,
    `close` the other status='new' rows (they are what shows twice on the
    tracker), `leave` the rest - applied rows are never touched, skipped
    rows are already hidden and terminal, and closed rows stay closed.
    Firecrawl rows are left out: their failed extractions share the board
    url, which is why filter_new exempts them from url identity too.
    Takes a bare connection (row_factory sqlite3.Row) so a dry run can open
    the database read-only, without the single-writer lock.
    """
    groups: dict[str, list] = {}
    for r in db.execute("SELECT id, company, title, location, url, source, "
                        "status, tag, score, first_seen FROM jobs "
                        "WHERE url IS NOT NULL AND url != ''"):
        if r["source"] == "firecrawl":
            continue
        groups.setdefault(url_key(r["url"]), []).append(r)
    out = []
    for key, rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        ordered = _twin_order(rows, min_score)
        keep, rest = ordered[0], ordered[1:]
        out.append({"key": key, "keep": keep,
                    "close": [r for r in rest if r["status"] == "new"],
                    "leave": [r for r in rest if r["status"] != "new"]})
    return out


# Single-writer guard. The scheduled VM run and a manual CLI invocation
# share jobs.db, and a manual write concurrent with a run once replaced the db
# file under the run's open connection ("attempt to write a readonly
# database"). An exclusive flock on a sidecar "<db>.lock" makes any two
# Store-opening processes serialize instead of racing.
#
# Bounded wait: a scheduled run takes up to ~6 minutes, so a manual command
# should wait it out rather than fail instantly - but past this ceiling
# something is wedged and failing loudly beats hanging forever. The env var
# lets tests (and a stuck operator) shrink it; the module attribute is read at
# acquire time so tests can also patch it directly.
LOCK_TIMEOUT = float(os.environ.get("JOBCRAWL_LOCK_TIMEOUT", "600"))
_LOCK_POLL = 0.2

# flock treats a second fd on the same file - even in the same process - as a
# competing holder, so two Stores on one db path in one process would deadlock.
# Refcount per resolved path: the first Store acquires the flock, the last
# close releases it. realpath so a symlinked db and its target share one lock.
_LOCKS: dict[str, list] = {}  # realpath -> [fd, refcount]
_LOCKS_MU = threading.Lock()  # guards the dict; the check-then-flock window
                              # between threads is accepted - jobcrawl is
                              # single-threaded per process.


def _acquire_lock(path: str) -> str:
    """Take (or re-enter) the exclusive lock for a db path; returns the key."""
    key = os.path.realpath(path)
    with _LOCKS_MU:
        ent = _LOCKS.get(key)
        if ent:
            ent[1] += 1
            return key
    lockpath = key + ".lock"
    fd = os.open(lockpath, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise RuntimeError(
                    f"could not lock {lockpath} after {LOCK_TIMEOUT:.0f}s - "
                    "another jobcrawl process (the scheduled run?) is holding "
                    "the database; retry once it finishes")
            time.sleep(_LOCK_POLL)
    with _LOCKS_MU:
        _LOCKS[key] = [fd, 1]
    return key


def _release_lock(key: str) -> None:
    with _LOCKS_MU:
        ent = _LOCKS.get(key)
        if ent is None:
            return
        ent[1] -= 1
        if ent[1] > 0:
            return
        del _LOCKS[key]
    # The lockfile itself is never deleted: unlinking it would let a third
    # process lock a fresh inode while a waiter still spins on the old one,
    # giving two "exclusive" holders. The kernel drops the flock on close
    # (and on process death), which is all the release that is needed.
    fcntl.flock(ent[0], fcntl.LOCK_UN)
    os.close(ent[0])


class Store:
    def __init__(self, path: str = "jobs.db"):
        self._lock_key = None  # first, so __del__ is safe if init raises
        if path != ":memory:" and not path.startswith("file:"):
            # :memory: and file: URIs skip locking: tests use the former and
            # read-only experiments the latter - neither competes for jobs.db.
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._lock_key = _acquire_lock(path)
        self.db = sqlite3.connect(path, uri=path.startswith("file:"))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def close(self) -> None:
        """Release the connection and the single-writer lock. Idempotent."""
        db = getattr(self, "db", None)
        if db is not None:
            self.db = None
            db.close()
        key = getattr(self, "_lock_key", None)
        if key is not None:
            self._lock_key = None
            _release_lock(key)

    def __del__(self):
        # Defensive: normal paths call close() explicitly; this catches error
        # exits. Bare except because at interpreter shutdown the modules
        # close() needs may already be torn down.
        try:
            self.close()
        except Exception:
            pass

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for col, decl in (("cycle", "TEXT"),
                          ("missing_runs", "INTEGER DEFAULT 0")):
            if col not in have:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
        have_b = {r["name"] for r in self.db.execute("PRAGMA table_info(boards)")}
        for col in ("last_try", "last_yield"):
            if col not in have_b:
                self.db.execute(f"ALTER TABLE boards ADD COLUMN {col} TEXT")

    def known_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT id FROM jobs")}

    def known_urls(self) -> set[str]:
        return {url_key(r[0])
                for r in self.db.execute("SELECT url FROM jobs") if r[0]}

    def close_url_twins(self, ids: list[str]) -> int:
        """Close the duplicate twins url_twin_groups picked. Returns rows closed.

        Only status='new' rows flip: an applied or skipped row is never
        rewritten, even if a stale plan names it.
        """
        cur = self.db.executemany(
            "UPDATE jobs SET status='closed' WHERE id=? AND status='new'",
            [(i,) for i in ids])
        self.db.commit()
        return cur.rowcount

    def rows_of(self, ids) -> dict[str, sqlite3.Row]:
        """Stored id/location/url per id, for the ids that exist."""
        ids = list(ids)
        out: dict[str, sqlite3.Row] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            out.update((r["id"], r) for r in self.db.execute(
                f"SELECT id, location, url FROM jobs WHERE id IN "
                f"({','.join('?' * len(chunk))})", chunk))
        return out

    def cached_locations(self, keys, max_age_days: int = 30) -> dict[str, str]:
        """detail_cache entries for these url_keys younger than max_age_days.

        The expiry is a refresh, not a correctness bound: a posting's office
        rarely changes, and one call per url a month is cheap.
        """
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=max_age_days)).isoformat()
        keys = [k for k in keys if k]
        out: dict[str, str] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            out.update((r[0], r[1] or "") for r in self.db.execute(
                f"SELECT url_key, location FROM detail_cache WHERE fetched_at>=? "
                f"AND url_key IN ({','.join('?' * len(chunk))})", [cutoff, *chunk]))
        return out

    def cache_locations(self, found: dict[str, str], max_age_days: int = 30) -> None:
        """Remember detail-page offices by url_key; drop expired entries."""
        now = dt.datetime.now(dt.timezone.utc)
        self.db.executemany(
            "INSERT OR REPLACE INTO detail_cache (url_key, location, fetched_at) "
            "VALUES (?,?,?)",
            [(k, v, now.isoformat()) for k, v in found.items() if k])
        self.db.execute("DELETE FROM detail_cache WHERE fetched_at<?",
                        ((now - dt.timedelta(days=max_age_days)).isoformat(),))
        self.db.commit()

    def filter_new(self, jobs: list[dict]) -> list[dict]:
        """Drop postings we have already seen, by content hash or by URL.

        The URL is a second identity: a company editing a title ("Summer 2027"
        appended) changes the hash but not the URL, and re-notifying about an
        edited posting is noise. URLs compare by `url_key`, so the same job id
        in a differently-dressed URL (two lists linking the same posting) still
        collides. Firecrawl rows are exempt - failed extractions fall back to
        the shared board URL, which would wrongly collapse every posting on
        that board into one.
        """
        seen = self.known_ids()
        seen_urls = self.known_urls()
        out, batch_seen, batch_urls = [], set(), set()
        for j in jobs:
            jid = row_id(j)
            if jid in seen or jid in batch_seen:
                continue
            key = url_key(j.get("url", ""))
            if key and j.get("source") != "firecrawl":
                if key in seen_urls or key in batch_urls:
                    continue
                batch_urls.add(key)
            batch_seen.add(jid)
            out.append({**j, "id": jid})
        return out

    def resurface_reposts(self, jobs: list[dict], min_age_days: int,
                          min_score: int) -> list[str]:
        """A known role re-listed under a fresh requisition comes back.

        Trigger: incoming hash matches a stored `status='new'` row, the URL is
        genuinely new (url_key unseen anywhere), and the stored row is at
        least `min_age_days` old. That is a company closing a requisition and
        opening a new one for the same title (Ambarella re-opened its whole
        intern batch as JR1003xx on 2026-08-27; the JR1001xx hashes hid it) -
        a new application window the hash dedupe would otherwise bury forever.

        The row is updated in place - url, posted_at, first_seen=now - never
        re-inserted (the hash IS the primary key) and never re-scored (same
        title, same role; the cycle field may go stale, which rescore-time
        tooling can fix). Resetting first_seen is also the noise bound: a
        posting flip-flopping between two URL dressings can false-trigger at
        most once per `min_age_days`. notified drops to 0 only when the row
        would actually surface (score >= min_score, or a NULL-score
        pass-through row) - a suppressed score-5 repost must stay buried.
        Only 'new' rows qualify: applied is history, skipped is feedback
        (machine or human) that the new requisition inherits, and closed
        already reopens via reopen_present on the hash sighting (its URL may
        go stale; accepted). Firecrawl rows are exempt for the same reason
        as in filter_new. Returns the resurfaced ids.
        """
        rows = {r["id"]: r for r in self.db.execute(
            "SELECT id, url, first_seen, status, score, notified FROM jobs "
            "WHERE status='new'")}
        seen_urls = self.known_urls()
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = (now - dt.timedelta(days=min_age_days)).isoformat()
        # A repost REPLACES a requisition. When the stored row's own URL is
        # still in this fetch, the new URL is a sibling running alongside it
        # (Susquehanna lists one analyst title per office), not a
        # replacement, and swapping the row's URL would hide the original.
        live = {url_key(j.get("url", "")) for j in jobs}
        # Two live same-title requisitions collapse onto one stored row (the
        # hash basis cannot represent both); keep the newest posted_at.
        best: dict[str, dict] = {}
        for j in jobs:
            if j.get("source") == "firecrawl":
                continue
            # With no office in the location the hash cannot tell a repost
            # from another region's copy of the title, and the re-judge
            # would score the stored row with the sibling's page: Google's
            # US "Student Researcher" row took the Canada copy's URL and was
            # filtered as non-US (2026-09-25). cmd_run pre-enriches these,
            # so one still vague here is a failed or budget-clipped call.
            if prefilter.vague_location(j.get("location")):
                continue
            jid = row_id(j)
            r = rows.get(jid)
            if r is None or r["first_seen"] > cutoff:
                continue
            if r["url"] and url_key(r["url"]) in live:
                continue
            key = url_key(j.get("url", ""))
            if not key or key in seen_urls:
                continue
            cur = best.get(jid)
            if cur is None or (j.get("posted_at") or "") > (cur.get("posted_at") or ""):
                best[jid] = j
        hit = []
        for jid, j in best.items():
            r = rows[jid]
            surfaces = r["score"] is None or r["score"] >= min_score
            self.db.execute(
                "UPDATE jobs SET url=?, posted_at=?, first_seen=?, "
                "missing_runs=0, notified=? WHERE id=?",
                (j.get("url", ""), j.get("posted_at"), now.isoformat(),
                 0 if surfaces else r["notified"], jid))
            hit.append(jid)
        self.db.commit()
        return hit

    def apply_repost_scores(self, jobs: list[dict], min_score: int) -> int:
        """Fresh judgments for resurfaced reposts.

        A repost is a new application window and must be judged with today's
        profile and a full description - the resurrected row's frozen score
        may predate both. The 2026-08-28 repost sweep proved it: pre-profile-
        fix quant scores of 82 and title-only Ambarella scores that missed
        hard Master's/PhD requirements went straight back into the digest.
        Overwrites score/why/cycle/tag and sets notified strictly by the NEW
        judgment: notified=0 only at or above min_score, otherwise re-buried.
        tag='filtered' (the no-US-office convention) also flips status to
        'skipped', mirroring what cmd_run does for fresh rows. Callers must
        keep the score-50 "scoring failed" fallback out of `jobs` - it would
        overwrite real scores (the rescore invariant). Returns how many
        still surface.
        """
        surfaced = 0
        for j in jobs:
            filtered = j.get("tag") == "filtered"
            surfaces = not filtered and j["score"] >= min_score
            surfaced += surfaces
            self.db.execute(
                "UPDATE jobs SET score=?, why=?, cycle=?, tag=?, notified=?, "
                "status=CASE WHEN ? THEN 'skipped' ELSE status END WHERE id=?",
                (j["score"], j.get("why"), j.get("cycle"), j.get("tag"),
                 0 if surfaces else 1, filtered, j["id"]))
        self.db.commit()
        return surfaced

    def demote_legacy_listing_scores(self, ids: list[str]) -> int:
        """Strip pre-policy scores off resurfaced aggregator listings.

        Aggregator rows are never LLM-scored today, but rows stored before
        that policy carry real scores - judged title-only, and often from
        hosts the crawler cannot fetch, so the judgment can never be
        refreshed. When such a row resurfaces as a repost, the frozen score
        is false confidence: convert it to the pass-through convention the
        listing would get if it arrived today (score NULL, rendered '-',
        tag 'list'). Scoped to the given resurfaced ids only - stored
        history that is not resurfacing keeps its scores.
        """
        n = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            n += self.db.execute(
                f"UPDATE jobs SET score=NULL, tag='list', "
                f"why='curated list pass-through (legacy score dropped)' "
                f"WHERE id IN ({marks}) AND score IS NOT NULL", chunk).rowcount
        self.db.commit()
        return n

    def insert(self, jobs: list[dict]) -> int:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        rows = [(
            j.get("id") or job_id(j), j["company"], j["title"], j.get("location", ""),
            j["url"], j.get("source", ""), j.get("posted_at"), now,
            j.get("score"), j.get("why"), j.get("cycle"), j.get("tag"),
        ) for j in jobs]
        self.db.executemany(
            "INSERT OR IGNORE INTO jobs "
            "(id,company,title,location,url,source,posted_at,first_seen,score,why,cycle,tag) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.db.commit()
        return len(rows)

    def unnotified(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM jobs WHERE notified=0 ORDER BY score DESC"
        ))

    def mark_notified(self, ids: list[str]) -> None:
        self.db.executemany("UPDATE jobs SET notified=1 WHERE id=?", [(i,) for i in ids])
        self.db.commit()

    def mark_skipped(self, ids: list[str]) -> None:
        """Suppress rows entirely: no digest, and hidden from `list` too."""
        self.db.executemany(
            "UPDATE jobs SET notified=1, status='skipped' WHERE id=?",
            [(i,) for i in ids])
        self.db.commit()

    def match_rows(self) -> list[sqlite3.Row]:
        """Every stored posting's identity fields, for matching against an
        external export (the Simplify tracker CSV).

        Deliberately not a query per CSV row: the export carries hundreds of
        rows and the store tens of thousands, so one pass and two in-memory
        indexes beats a round trip each.
        """
        return list(self.db.execute(
            "SELECT id, company, title, url, status FROM jobs"))

    def mark_applied(self, ids: list[str]) -> list[str]:
        """Flip 'new'/'skipped' rows to 'applied'. Returns the ids flipped.

        The status filter is the whole point. A 'closed' row must never come
        back to life from an import - the posting is gone from its board, and
        an export saying you applied months ago is not evidence otherwise. An
        already-'applied' row is a no-op, which is what makes re-importing the
        same export (or a later export that still contains it) idempotent.
        """
        flipped: list[str] = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            flipped += [r[0] for r in self.db.execute(
                f"SELECT id FROM jobs WHERE status IN ('new','skipped') "
                f"AND id IN ({marks})", chunk)]
        self.db.executemany("UPDATE jobs SET status='applied' WHERE id=?",
                            [(i,) for i in flipped])
        self.db.commit()
        return flipped

    def set_status(self, job_id_prefix: str, status: str) -> int:
        cur = self.db.execute(
            "UPDATE jobs SET status=? WHERE id LIKE ?", (status, job_id_prefix + "%")
        )
        self.db.commit()
        return cur.rowcount

    # -- misc persistent state -----------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                        (key, value))
        self.db.commit()

    # -- posting lifecycle ---------------------------------------------------

    def reopen_present(self, present_ids: set[str], present_urls: set[str],
                       min_score: int = 65) -> list[str]:
        """Closed rows sighted on a board again come back to life.

        Closure is absence-based and deliberately conservative, but a
        transient glitch (a half-deployed board that still returned fresh,
        non-empty data) can close a live posting - and URL dedupe blocks
        reinsertion, so without this the row was gone for good. A sighting in
        any fresh fetch is positive evidence, so it reverses the closure.
        Reopened rows keep notified=1: they were surfaced once already, so
        they return to the tracker, never to the digest. Returns reopened ids.
        """
        rows = self.db.execute(
            "SELECT id, url, source, status, tag, score, first_seen "
            "FROM jobs").fetchall()
        # A closed row whose url_key another row still holds open is a twin
        # of that row (the same posting stored twice), not a lost posting:
        # reopening it put the twin back on the tracker next to the live
        # row (Zipline's retitled "Financial Analyst" rows, reopened by the
        # url_key id-set change, 2026-09-25). Same for a twin the user
        # skipped - the posting already has a decision. A MACHINE skip
        # (stale, filtered, below threshold) holds nothing: _twin_order
        # ranks it below 'new', and letting it pin the twin shut kept a
        # live posting off the tracker (Lennox's "AI Engineering Co-op",
        # held by an icims copy the US gate filtered). Firecrawl urls are
        # no identity.
        def key(r) -> str:
            if not r["url"] or r["source"] == "firecrawl":
                return ""
            return url_key(r["url"])
        held = {key(r) for r in rows
                if _status_rank(r, min_score) < _MACHINE_SKIP} - {""}
        hits = [r for r in rows if r["status"] == "closed"
                and (r["id"] in present_ids
                     or (r["url"] and url_key(r["url"]) in present_urls))
                and key(r) not in held]
        # Several closed twins of one posting come back as ONE row.
        hit, taken = [], set()
        for r in _twin_order(hits, min_score):
            k = key(r)
            if k and k in taken:
                continue
            taken.add(k)
            hit.append(r["id"])
        self.db.executemany(
            "UPDATE jobs SET status='new', missing_runs=0 WHERE id=?",
            [(i,) for i in hit])
        self.db.commit()
        return hit

    def live_rows(self) -> list[sqlite3.Row]:
        """Rows whose board presence is worth tracking."""
        return list(self.db.execute(
            "SELECT id, company, source, url FROM jobs "
            "WHERE status IN ('new','applied')"))

    def update_presence(self, eligible_ids: set[str], present_ids: set[str],
                        close_after: int) -> list[str]:
        """Track which live postings still appear on their boards; close the gone.

        Only ids in eligible_ids are touched - their board fetched fresh,
        complete, and non-empty this run. A board error, a 304, or a
        suspicious zero-posting response freezes streaks rather than growing
        them, so flakiness can never close a posting. Closure is likewise
        restricted to rows that went missing THIS run - a leftover streak
        from an earlier config or threshold change must not close a row on a
        run that never checked its board. Returns ids newly closed.
        Applied rows keep their streaks but are never flipped to closed - the
        application happened; that history stays.
        """
        present = eligible_ids & present_ids
        missing = eligible_ids - present_ids
        self.db.executemany("UPDATE jobs SET missing_runs=0 WHERE id=?",
                            [(i,) for i in present])
        self.db.executemany("UPDATE jobs SET missing_runs=missing_runs+1 WHERE id=?",
                            [(i,) for i in missing])
        closed: list[str] = []
        miss = list(missing)
        for i in range(0, len(miss), 500):
            chunk = miss[i:i + 500]
            marks = ",".join("?" * len(chunk))
            closed += [r[0] for r in self.db.execute(
                f"SELECT id FROM jobs WHERE status='new' AND missing_runs >= ? "
                f"AND id IN ({marks})", (close_after, *chunk))]
        self.db.executemany("UPDATE jobs SET status='closed' WHERE id=?",
                            [(i,) for i in closed])
        self.db.commit()
        return closed

    # -- board health --------------------------------------------------------

    def last_ok_map(self) -> dict[str, dt.datetime]:
        """Board key -> when it last returned successfully (fresh or 304)."""
        out = {}
        for r in self.db.execute("SELECT key, last_ok FROM boards WHERE last_ok IS NOT NULL"):
            try:
                ts = dt.datetime.fromisoformat(r["last_ok"])
            except (ValueError, TypeError):
                continue
            out[r["key"]] = ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)
        return out

    def producing_boards(self) -> set[str]:
        """Lowercased keys of boards whose last fetch succeeded with postings.

        This is what "we poll this board directly" has to mean for the
        aggregator overlap drop: a board that is failing, or that keeps
        answering with zero postings, is not covering anything. A 304 freezes
        both streaks, so an unchanged board keeps whatever standing it had.
        """
        return {r["key"].lower() for r in self.db.execute(
            "SELECT key FROM boards WHERE last_ok IS NOT NULL "
            "AND fail_streak = 0 AND empty_streak = 0")}

    def last_try_map(self) -> dict[str, dt.datetime]:
        """Board key -> when it was last attempted, success or not.

        Metered boards must pace off attempts: pacing off last_ok means a
        FAILING metered board is due again every single run, burning its
        budget on retries exactly while it is broken.
        """
        out = {}
        for r in self.db.execute("SELECT key, last_try FROM boards WHERE last_try IS NOT NULL"):
            try:
                ts = dt.datetime.fromisoformat(r["last_try"])
            except (ValueError, TypeError):
                continue
            out[r["key"]] = ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)
        return out

    def yield_map(self) -> dict[str, dt.datetime]:
        """Lowercased board key -> when it last produced a new posting.

        Drives cold-tier promotion. Keys are lowercased because the two sides
        never agree on case: the `boards` table is keyed with the config's
        spelling of a token, while the caller derives its key from a posting
        URL. Closure already compares the two case-insensitively; promotion
        that did not would simply never fire.
        """
        out = {}
        for r in self.db.execute(
                "SELECT key, last_yield FROM boards WHERE last_yield IS NOT NULL"):
            try:
                ts = dt.datetime.fromisoformat(r["last_yield"])
            except (ValueError, TypeError):
                continue
            out[r["key"].lower()] = ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)
        return out

    def record_yield(self, keys: list[str]) -> int:
        """Stamp boards that produced a new posting this run. Case-insensitive.

        Only touches rows that already exist: a board with no health row has
        never been fetched, so it cannot have yielded anything.
        """
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        cur = self.db.executemany(
            "UPDATE boards SET last_yield=? WHERE lower(key)=?",
            [(now, k.lower()) for k in keys])
        self.db.commit()
        return cur.rowcount

    def etags(self) -> dict[str, str]:
        return {r["key"]: r["etag"] for r in
                self.db.execute("SELECT key, etag FROM boards WHERE etag IS NOT NULL")}

    def save_etags(self, etags: dict[str, str]) -> None:
        """Persist this run's etags, called only once its rows are stored.

        record_board rolls health right after the fetch but never writes an
        etag: a run that dies between fetch and store must leave the old
        etags in place, so the next run fetches the boards in full again.
        """
        self.db.executemany("UPDATE boards SET etag=? WHERE key=?",
                            [(v, k) for k, v in etags.items()])
        self.db.commit()

    def clear_etags(self, keys: list[str]) -> int:
        """Forget the etags of the named boards so the next fetch is unconditional.

        Case-insensitive on the key, like closure and yield. Used when a row
        from the board failed scoring this run: "left unstored to retry next
        run" is only true if the posting is fetched again, and a persisted
        etag turns the next fetch into a 304 that never carries it. Snowflake's
        AI/ML Spring 2027 intern sat behind an unchanged Ashby etag for six
        runs (2026-09-25).
        """
        if not keys:
            return 0
        cur = self.db.executemany(
            "UPDATE boards SET etag=NULL WHERE lower(key)=? AND etag IS NOT NULL",
            [(k.lower(),) for k in keys])
        self.db.commit()
        return cur.rowcount

    def record_board(self, key: str, name: str, status: str, count: int = 0,
                     etag: str | None = None, error: str = "") -> None:
        """Roll a board's health streaks after a fetch.

        status: 'ok' (fresh data), 'unchanged' (304), 'error', or 'outage'.
        An unchanged response proves the board is alive but says nothing
        about emptiness, so it clears the fail streak and freezes the empty
        streak. An outage is a failure the whole platform shared this run
        (cli.detect_outages): it says nothing about THIS board, so both
        streaks freeze and last_ok stands; only the attempt and its error
        are noted.
        """
        row = self.db.execute(
            "SELECT fail_streak, empty_streak FROM boards WHERE key=?", (key,)).fetchone()
        fail, empty = (row["fail_streak"], row["empty_streak"]) if row else (0, 0)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if status == "error":
            fail += 1
        elif status != "outage":
            fail = 0
        if status == "ok":
            empty = empty + 1 if count == 0 else 0
        self.db.execute(
            "INSERT INTO boards "
            "(key, name, etag, fail_streak, empty_streak, last_ok, last_error, last_try) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET name=excluded.name, "
            "etag=COALESCE(excluded.etag, boards.etag), "
            "fail_streak=excluded.fail_streak, empty_streak=excluded.empty_streak, "
            "last_ok=COALESCE(excluded.last_ok, boards.last_ok), "
            "last_error=excluded.last_error, last_try=excluded.last_try",
            (key, name, etag, fail, empty,
             now if status in ("ok", "unchanged") else None,
             error if status in ("error", "outage") else "", now))
        self.db.commit()

    def dead_boards(self, min_fail: int) -> dict[str, sqlite3.Row]:
        """key -> (fail_streak, last_try, last_error) for boards that have
        failed min_fail runs straight.

        The broad board list is regenerated from public GitHub lists, so it
        keeps reintroducing tokens that no longer resolve. Nothing can rescue
        those, but they cost a request each every hour and drown the real
        warnings, so the caller polls them on a slow clock instead. The error
        rides along because its class decides how slow: a platform saying
        "this board does not exist" retires a board far sooner than a
        failure that might be transient (cli.gone_cause).
        """
        return {r["key"]: r for r in self.db.execute(
            "SELECT key, fail_streak, last_try, last_error FROM boards "
            "WHERE fail_streak >= ?", (min_fail,))}

    def problem_boards(self, min_fail: int = 3, min_empty: int = 2) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM boards WHERE fail_streak >= ? OR empty_streak >= ? "
            "ORDER BY fail_streak DESC, empty_streak DESC",
            (min_fail, min_empty)))

    # -- feedback ------------------------------------------------------------

    def feedback(self, limit: int = 8,
                 min_score: int = 65) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        """(applied, passed) examples of the candidate's actual decisions.

        Passed means a surfaced posting the user explicitly dismissed with
        `jobcrawl skipped`, so only rows the user actually saw qualify:
        rows that cleared the surfacing threshold, plus aggregator
        pass-through rows (score NULL, tag 'list'), which surface unscored.
        Machine suppressions stay out - stale-on-arrival rows (tag='stale'),
        retroactive filter sweeps (tag='filtered'), and the rows `unstale`
        re-scored below threshold, which keep status='skipped' but pick up a
        real score and tag. The user never saw those, so they were never a
        human decision.
        """
        applied = list(self.db.execute(
            "SELECT company, title, score FROM jobs WHERE status='applied' "
            "ORDER BY rowid DESC LIMIT ?", (limit,)))
        passed = list(self.db.execute(
            "SELECT company, title, score FROM jobs WHERE status='skipped' "
            "AND tag IS NOT NULL AND tag NOT IN ('stale', 'filtered') "
            "AND (score >= ? OR (score IS NULL AND tag = 'list')) "
            "ORDER BY rowid DESC LIMIT ?", (min_score, limit)))
        return applied, passed

    def listing_rows(self) -> list[sqlite3.Row]:
        """Live aggregator rows that surfaced without ever being judged.

        These are the pass-throughs: score NULL, tag 'list'. They carry no
        description, so no text gate and no scorer ever saw them - which is
        exactly how an ITAR-gated posting reached the tracker. `enrich-list`
        fetches the text Simplify's feed omits and judges them for the first
        time. NULL score is the identifying mark, not the tag alone: a row
        that already carries a real score has been judged.
        """
        return list(self.db.execute(
            "SELECT * FROM jobs WHERE status='new' AND tag='list' "
            "AND score IS NULL ORDER BY first_seen DESC"))

    def scored_band(self, lo: int, hi: int) -> list[sqlite3.Row]:
        """Live rows whose score landed in [lo, hi] - the rescore candidates."""
        # COALESCE: in SQLite `tag != 'stale'` is NULL for a NULL tag, which
        # would silently hide legacy untagged rows from every rescore.
        return list(self.db.execute(
            "SELECT * FROM jobs WHERE status='new' AND COALESCE(tag,'') != 'stale' "
            "AND score BETWEEN ? AND ? ORDER BY score DESC", (lo, hi)))

    def rows_by_id_prefix(self, prefixes: list[str]) -> list[sqlite3.Row]:
        """Live rows whose id starts with any of the given prefixes.

        Backs `rescore --ids`, where the caller names exact ids or 8-char id
        prefixes (from an audit report or a hand-written list) rather than a
        score band. Closed rows are excluded the same way `scored_band` scopes
        to live ones - a posting gone from its board is not worth re-judging.
        Chunked at 200 prefixes so one big `--ids` file never builds an
        unbounded SQL statement.
        """
        out: list[sqlite3.Row] = []
        for i in range(0, len(prefixes), 200):
            chunk = prefixes[i:i + 200]
            if not chunk:
                continue
            clause = " OR ".join("id LIKE ?" for _ in chunk)
            params = [f"{p}%" for p in chunk]
            out += list(self.db.execute(
                f"SELECT * FROM jobs WHERE status != 'closed' AND ({clause})",
                params))
        return out

    def update_scores(self, jobs: list[dict], resurface_min: int | None = None) -> int:
        """Overwrite score/why/cycle/tag after a rescore.

        Rows that were suppressed (old score below `resurface_min`) and now
        clear it get notified=0 so the next run's digest picks them up - a
        rescue from suppression, never a repeat. Rows whose old score already
        cleared the bar surfaced once and stay notified; resetting them would
        re-email the whole band every time the profile changes. NULL old
        scores (aggregator pass-throughs) surfaced by policy, so they count
        as already seen too.
        """
        old: dict[str, int | None] = {}
        if resurface_min is not None:
            all_ids = [j["id"] for j in jobs]
            for i in range(0, len(all_ids), 500):
                chunk = all_ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                old.update({r["id"]: r["score"] for r in self.db.execute(
                    f"SELECT id, score FROM jobs WHERE id IN ({marks})", chunk)})
        # A refetch can also learn a location the board's card never had
        # (iCIMS tenants with bare cards); fill it in only where the stored
        # one is empty - the row's id was hashed from the raw fetch and is
        # unaffected, and a real stored location is never overwritten.
        # A machine-suppressed row (tag 'filtered' or 'stale') that stays
        # skipped keeps its tag: store.feedback reads a skipped row with any
        # other tag and a score at or above the threshold as "the user saw
        # this and passed", so swapping in the scorer's tag would turn a
        # machine suppression into a fake human decision in the prompt.
        self.db.executemany(
            "UPDATE jobs SET score=?, why=?, cycle=?, "
            "tag=CASE WHEN status='skipped' AND tag IN ('filtered', 'stale') "
            "THEN tag ELSE ? END, "
            "location=CASE WHEN COALESCE(location, '')='' THEN ? ELSE location END "
            "WHERE id=?",
            [(j["score"], j.get("why"), j.get("cycle"), j.get("tag"),
              j.get("location") or "", j["id"])
             for j in jobs])
        resurfaced = 0
        if resurface_min is not None:
            ids = [(j["id"],) for j in jobs
                   if j["score"] >= resurface_min
                   and j["id"] in old
                   and old[j["id"]] is not None
                   and old[j["id"]] < resurface_min]
            self.db.executemany("UPDATE jobs SET notified=0 WHERE id=?", ids)
            resurfaced = len(ids)
        self.db.commit()
        return resurfaced

    def readmit(self, jobs: list[dict]) -> int:
        """Return machine-suppressed rows to the live set with their new scores.

        Backs `rescore --ids` for rows the post-enrich clearance gate buried
        (status 'skipped', tag 'filtered', score NULL) whose refetched text
        the fixed gate now passes and whose fresh score clears the threshold.
        They go back to status 'new' with notified=0, so the next run's digest
        emails them and the tracker lists them - the user never saw them.
        Only 'skipped' rows move: an applied or closed row is never touched.
        Status flips first so update_scores writes the scorer's tag.
        """
        if not jobs:
            return 0
        cur = self.db.executemany(
            "UPDATE jobs SET status='new', notified=0 WHERE id=? AND status='skipped'",
            [(j["id"],) for j in jobs])
        moved = cur.rowcount
        self.update_scores(jobs)
        return moved

    def stale_rows(self, exclude_sources: set[str]) -> list[sqlite3.Row]:
        """Stale-suppressed rows that did NOT come from an aggregator feed.

        These are the casualties of applying the age cutoff to directly polled
        boards: real, open postings that were merely old when the board was
        first crawled. `unstale` re-admits them.
        """
        if not exclude_sources:
            exclude_sources = {""}
        marks = ",".join("?" * len(exclude_sources))
        return list(self.db.execute(
            f"SELECT * FROM jobs WHERE status='skipped' AND tag='stale' "
            f"AND source NOT IN ({marks})", tuple(exclude_sources)))

    def unstale(self, jobs: list[dict], min_score: int) -> int:
        """Score a batch of stale-suppressed rows back into circulation.

        Rows clearing the threshold become normal `new` rows and go unnotified
        so the next digest and publish pick them up; the rest keep their real
        score and stay suppressed, exactly like any other below-threshold
        posting. Nothing is deleted either way, so dedupe is unaffected.
        """
        # A refetch can also learn a location the board's card never had
        # (iCIMS tenants with bare cards); fill it in only where the stored
        # one is empty - the row's id was hashed from the raw fetch and is
        # unaffected, and a real stored location is never overwritten.
        self.db.executemany(
            "UPDATE jobs SET score=?, why=?, cycle=?, tag=?, "
            "location=CASE WHEN COALESCE(location, '')='' THEN ? ELSE location END "
            "WHERE id=?",
            [(j["score"], j.get("why"), j.get("cycle"), j.get("tag"),
              j.get("location") or "", j["id"])
             for j in jobs])
        promoted = [(j["id"],) for j in jobs if j["score"] >= min_score]
        self.db.executemany(
            "UPDATE jobs SET status='new', notified=0 WHERE id=?", promoted)
        self.db.commit()
        return len(promoted)

    def surfaced(self, min_score: int = 0) -> list[sqlite3.Row]:
        """Everything worth showing on the public tracker, newest first.

        A NULL score on a live row means an aggregator pass-through - never
        judged, surfaced by policy - so it belongs on the tracker. Stale
        rows also carry NULL but sit at status='skipped', which the first
        clause already excludes.
        """
        # Day granularity, not timestamp: within the same day the exact posting
        # hour is noise, so rank by score there instead. Newest day first;
        # NULL sorts below every real score within a day.
        return list(self.db.execute(
            "SELECT * FROM jobs WHERE status != 'skipped' "
            "AND (score >= ? OR score IS NULL) "
            "ORDER BY date(COALESCE(posted_at, first_seen)) DESC, score DESC",
            (min_score,)))

    def pending(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM jobs WHERE status='new' ORDER BY score DESC LIMIT ?", (limit,)
        ))

    def log_run(self, **kw) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO runs (ts,fetched,prefiltered,scored,new_jobs,errors) "
            "VALUES (?,?,?,?,?,?)",
            (dt.datetime.now(dt.timezone.utc).isoformat(), kw.get("fetched", 0),
             kw.get("prefiltered", 0), kw.get("scored", 0), kw.get("new_jobs", 0),
             kw.get("errors", "")),
        )
        self.db.commit()

    def log_audit(self, sampled: int, misses: int, detail: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO audits (ts, sampled, misses, detail) VALUES (?,?,?,?)",
            (dt.datetime.now(dt.timezone.utc).isoformat(), sampled, misses, detail))
        self.db.commit()

    def recent_audits(self, limit: int = 5) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM audits ORDER BY ts DESC LIMIT ?", (limit,)))

    def stats(self, min_score: int = 65) -> dict:
        q = lambda s, *p: self.db.execute(s, p).fetchone()[0]
        return {
            "total": q("SELECT COUNT(*) FROM jobs"),
            "new": q("SELECT COUNT(*) FROM jobs WHERE status='new'"),
            "applied": q("SELECT COUNT(*) FROM jobs WHERE status='applied'"),
            # Mirrors feedback()'s definition of a human "pass": only rows the
            # user actually saw count, so the score gate keeps out machine
            # suppressions like the rows `unstale` demoted below threshold.
            "passed": q("SELECT COUNT(*) FROM jobs WHERE status='skipped' "
                        "AND tag IS NOT NULL AND tag NOT IN ('stale', 'filtered') "
                        "AND (score >= ? OR (score IS NULL AND tag = 'list'))",
                        min_score),
            "closed": q("SELECT COUNT(*) FROM jobs WHERE status='closed'"),
            "runs": q("SELECT COUNT(*) FROM runs"),
            "bad boards": q("SELECT COUNT(*) FROM boards WHERE fail_streak >= 3"),
        }
