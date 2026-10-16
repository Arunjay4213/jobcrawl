# jobcrawl

Watches company career pages at the source and emails you a scored digest of new
internship postings every morning, across every cycle you're recruiting for.

The point isn't filling forms faster - Simplify already does that. The point is
**seeing a posting the morning it goes up** instead of whenever an aggregator
gets around to indexing it. For competitive roles that close after a few hundred
applicants, that gap decides whether you get to apply at all.

## How it works

```
ATS JSON APIs  ->  regex prefilter  ->  Claude scoring  ->  SQLite dedupe  ->  email digest
  (free)           (free, ~99% cut)     (~$0.01/day)        (never repeat)
```

Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Rippling, and Gem all
serve their hosted job boards from public unauthenticated endpoints (Gem's is
GraphQL; its board pages are client-rendered shells, so scraping them returns
nothing - poll the API instead).
Some Ashby orgs disable the public posting API (Whatnot, found 2026-09-01); on a 404 the adapter falls back to parsing the board's hosted page, whose embedded JSON carries the full listing, and those rows get their descriptions from a per-posting detail fetch like the Workday-style sources below. Workday
(both its `myworkdayjobs.com` and `myworkdaysite.com` domains) needs a POST but
is equally open. So for ~90% of companies there's no scraping at all - you're
reading the same data their careers page reads, seconds after it changes.
Firecrawl is the fallback for custom pages only, which keeps your credits for
the handful of companies that actually need it.
As of 2026-08-25 all five `ats: firecrawl` boards are PAUSED (commented out in
`config.yaml`): the JSON-extraction scrape format bills ~5 credits per scrape
(~100/day at four scrapes per board per day), which was draining the credit
balance for a trickle of postings the aggregator feeds mostly cover anyway.
Re-enable them after switching the adapter to 1-credit markdown scrapes with
extraction done by our own LLM (`FIRECRAWL_PROMPT` already holds the prompt to
port), or selectively - Citadel and Jane Street earned their slots, Two Sigma
and Hudson River (which has a free direct Greenhouse board) never did.

On a live run that's 5,700 postings fetched and 16 worth a human look.

The prefilter also drops any posting gated on a US security clearance.
A clearance term in the title (TS/SCI, Secret clearance, polygraph) is enough on its own; in descriptions it only counts when requirement wording sits next to the clearance term, so defense-adjacent postings that merely call a clearance "a plus" stay in.
Requirement words match as whole words only, negated phrasings ("no security clearance required") do not count, and a softener right after the clearance term ("is a plus", "preferred") disarms an earlier requirement word like "obtain".
Boards whose list APIs carry no text (Workday, SmartRecruiters, Workable, Rippling, Gem, Google Careers, and iCIMS tenants whose cards omit the location or text - the job page's JSON-LD supplies the city, country, date, and body) get their descriptions from a per-posting detail fetch that runs after the prefilter, so for them the same description check re-runs post-enrichment; postings it sweeps are recorded as skipped with `tag='filtered'`, and a posting whose description could not be fetched is always kept.
Google Careers also gets its real location list from that detail fetch - the search page carries none, so before 2026-08-31 the US-only gate had nothing to inspect and a Canada-only intern posting could reach the digest.
The scorer backstops subtler phrasings via a hard rule in `profile.md`.
Rows swept out retroactively carry `tag='filtered'`; like stale rows, they are machine suppressions and never count as feedback.

## Board tiers

Boards come from seven files, merged at load time and deduped by `(ats, token)` case-insensitively.
Earlier files win, so a board that reaches several tiers keeps the hottest definition of itself:

**`config.yaml`** - the curated list: companies picked for fit, annotated by
hand. This file always wins on conflict.

**`boards_broad.yaml`** - ~460 boards auto-derived from the apply URLs on every configured aggregator feed: the public GitHub internship lists in `config.yaml` ([SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships), [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships), [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships)) plus anything the weekly `feeds --add` discovery has vetted into `feeds_discovered.yaml` (currently [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships)).
Regenerate it, don't hand-edit it:

```bash
python -m jobcrawl.cli boards
```

Regeneration skips any board whose derived company name matches a curated entry or a name in `filters.exclude_companies`.
A curated entry means the right board is already polled, and the public lists keep linking dead slugs for months after a company renames its board.
An excluded company's postings all die at the prefilter's name match, so polling its boards is pure waste - which also makes `exclude_companies` the tombstone for a company whose board died with no successor anywhere (AcreTrader, 2026-09-01): without it, the feeds' inactive history would resurrect the dead token every regen and its health warning would sit in the digest forever.

**`boards_applied.yaml`** - boards mined from your own Simplify/Handshake application exports by `jobcrawl import-simplify`.
A posting you reached by hand is proof that a board exists which nothing here polls, and those are the gaps the public lists structurally do not cover.
This file is appended to, never regenerated, and every token in it was verified live when it was added.

**`boards_vc.yaml`** - boards revealed by the VC portfolio sweep.
`config.yaml`'s `vc_boards:` names portfolio job boards (one board per fund covering its whole portfolio; jobs.a16z.com and jobs.sequoiacap.com alone hold ~26k jobs).
Each entry's `kind:` picks the fetcher: `consider` (the default) is the hosted Consider API; `a16z` is a16z's own Next.js app, which serves the same Consider data through a `loadMorePublicJobs` server action and applies an internships filter server-side.
a16z replatformed between 2026-08-26 and 2026-08-30 and the Consider fetcher stopped resolving there; no weekly regen fell in that window, so nothing was actually lost, but the next one would have dropped every a16z-derived board from the file. A portfolio board that fails is reported and skipped, never fatal, so that loss would have been silent.
The action id is a build hash and is read out of the page's JS chunks on every sweep rather than pinned, so the next a16z deploy does not break it again.
`boards` sweeps them, keeps jobs whose title passes the intern/seniority regexes (which still matters on a16z: its own internships filter admits senior-titled roles), and derives each apply URL to a pollable ATS board; boards the hotter tiers already poll are excluded.
On a16z the recall of that server-side flag is taken on trust - the regexes narrow it, they cannot widen it, so an internship the flag misses is missed.
Discovery only - the portfolio jobs themselves carry no descriptions and are never stored; the boards they reveal are what gets polled, on a 4-hour clock (`every_hours: 4` per entry) so a batch of discovered startups never joins the hourly clock blind.
Regenerated wholesale, never hand-edited.

On top of a16z and Sequoia, `vc_boards:` now names eight more Consider-hosted funds (Kleiner Perkins, Bessemer, Bain Capital Ventures, Felicis, Initialized, GV, Contrary, Abstract) and fourteen Getro-hosted ones (`kind: getro`: Lux, Khosla, General Catalyst, Accel, 8VC, Thrive, Menlo, Craft, Insight, NFX, Foundation Capital, DCVC, Point Nine, Human Capital).
Getro boards only server-render 20 rows, so `sources.getro_jobs` reads the network id off the page and pages Getro's public search API with the query `intern`, which the server matches against "Internship" too.
This is the channel that would have caught Nominal: its Summer 2027 SWE intern (posted on Gem, on no GitHub list) was the first hit on Lux's board on 2026-09-11.

**`boards_resolved.yaml`** - boards found by reading each company's own website.
The lists name far more companies than they can point us at: for ~1,300 of them the link is a careers page, a redirector, or an ATS the company has since left (Nominal went Lever to Gem between its 2026 and 2027 reqs).
`boards` resolves those through `jobcrawl/resolve.py`: the feed's `company_url` is the company's simplify.jobs page, whose data names the real website; the resolver fetches that site, follows its careers/jobs links, and pulls every hosted-ATS URL it finds (Greenhouse, Lever, Ashby, Gem, Workday, SmartRecruiters, Workable, Rippling, iCIMS, including the embed and JSON-API shapes only careers pages use) through `derive_board`.
When the website walk names no board, or names one that fails its verification fetch (Jump Trading's careers page embeds a Greenhouse `talent_community` form, not its job board), the company's name is tried as a slug on Greenhouse, Lever and Ashby (`resolve.probe_slugs`: spaces removed, then hyphenated, then the simplify.jobs slug; corporate suffixes stripped; slugs under six characters skipped because short words collide); a probe counts only when the board answers with at least one job.
Results, hits and misses alike, live in `resolve_cache.json` (generated, committed) with a check date; each company is re-checked every `filters.resolve_recheck_days` (30), `filters.resolve_per_run` (300) caps the work per regen so the first sweep spreads over days, and this file is rebuilt from the cache every time.
A company whose derived board the health table says is dead is re-resolved on every regen regardless of cache age, because a 404 on the old token is the one signal that a company moved ATS.
Cold tier, regenerated wholesale, never hand-edited; `boards --no-resolve` skips the stage.

**`universe_feeds:`** (config.yaml) widens the company universe without touching the hot list.
These are listings.json files whose rows are not internships but whose companies run intern programs; today that is the SimplifyJobs New-Grad list, which alone named ~1,850 boards on no tier (2026-09-11).
Every row counts as history, so they only ever feed the cold tier and the resolver.

**`boards_cold.yaml`** - the cold tier: ~3,500 boards that appear only on the listings feeds' *inactive* history and the universe feeds.
Also written by `boards`, and inert until you switch it on.
See [the cold tier](#the-cold-tier).

The point of polling these directly instead of reading the GitHub lists: the
lists lag. Simplify's feed commits every 30 minutes but its upstream scraper
revisits boards slowly - measured median lag from posting to feed is ~15
hours. Hitting the ATS API directly closes that to one poll interval.

On top of that, the `simplify` source in config.yaml ingests their
machine-readable `listings.json` as a safety net for hosts with no public API
(Meta, Amazon, TikTok, Tesla, iCIMS, Oracle...). Listings whose apply URL maps
to a board polled directly are dropped at run time, so the faster fetch wins
and nothing appears twice. "Polled directly" means the board's last fetch
succeeded with postings (`Store.producing_boards`): a configured board that is
failing, or that answers with zero postings run after run, covers nothing, and
the feed's copy goes through. Before that rule (2026-09-02) the cold-tier
iCIMS entry for Atlassian, a shell portal that returns no jobs, silently ate
the feed's listing of Atlassian's 2027 SWE intern. On those safety-net hosts
you're at most one poll behind Simplify; everywhere else you're ahead of them.

Aggregator listings skip Claude scoring only while they have no description.
The GitHub lists are human-curated internship boards - someone already judged these worth listing - and the feeds carry no description text, so an LLM re-judging them from a bare title was burying good postings.
That changed on 2026-08-30: a Simplify feed entry's `id` turns out to be the id of its posting page on simplify.jobs, where the full description sits, so the normal enrichment pass now fetches it.
A listing that arrives with real text is scored like any directly-polled posting and passes through the same clearance/ITAR and US-office gates.
That matters beyond ranking - a description-less row is the one thing no text gate can inspect, which is how Northwood Space's ITAR-gated internships (every one of them requiring US-person status) reached the tracker.
Listings that stay bare - forks that mint their own ids, or a detail call that fails - keep the old behavior.
All of them still pass the prefilter (US-only, cycle, seniority) and the staleness gate; the unjudged ones show `-`/`list` instead of a score on the tracker and in the digest, and sort below scored postings within a day.
`jobcrawl enrich-list` is the backfill for listings stored before this, with `--dry-run` to see what would be gated and scored first.
Their cycle comes from the feed's structured `terms` field (or the `season` field on vanshb03-style feeds), which is what places them in the right season section of the tracker.
A `terms` entry whose season was already in its last month or over when the listing was posted is left out, since it cannot describe that listing (see Cycles).
Skipping one with `jobcrawl skipped` still counts as real negative feedback.

Two lists often carry the same posting under differently-dressed URLs (a direct `/job/<id>` link vs a search page with the id in a `?pid=` parameter, tracking suffixes, locale prefixes).
URL dedupe therefore compares a canonical key - host plus the set of the URL's embedded job-id digit runs (5 or more digits) - so the second dressing of a known posting is suppressed instead of double-listed.
The digits are read from the decoded URL, so a percent-escape next to a year (`%282027`, which is `(2027`) is not taken for a job id.
It is a set, so an id repeated inside one URL (iCIMS's `/jobs/27742/2026-27742/job`) keys the same as the id once (`/jobs/27742/<title-slug>/job`).
Rows stored before a change to this key can hold one posting twice: the set change on 2026-09-25 joined 22 such pairs, mostly Zipline's greenhouse rows and their Simplify copies.
`jobcrawl dedupe-urls` lists every group of rows that share a key, and with `--apply` it closes the duplicates.
It keeps one row per group: an applied row first, then one you skipped, then a new row, then the board's own row over an aggregator's copy, then the newest.
Only the other `status='new'` rows are closed; applied and skipped rows are never changed.
Without `--apply` it opens the database read-only and changes nothing.
A closed row sighted on its board again is not reopened while another row holds the same key and is new, applied, or skipped by you.
A row the pipeline skipped on its own (stale, filtered as non-US, or scored below the threshold) does not hold the key, so it cannot keep a live copy of the posting closed.
Several closed copies of one posting reopen as one row.

**Reposts** are the mirror case: a company closes a requisition and opens a
new one for the same title (Ambarella re-opened its whole intern batch that
way on 2026-08-27, JR1001xx becoming JR1003xx), which the content hash would
otherwise bury forever.
When a stored `status='new'` row at least `filters.repost_after_days` old
(default 14, 0 disables) is seen again under a genuinely new canonical URL,
it resurfaces in place: new URL and posted date, fresh `first_seen` (so it
tops the tracker and, via the urgent rules, can email immediately).
It is then re-judged in the same run with an enriched description and the
current profile - the frozen score is exactly the one not to trust (the
first repost sweep re-emailed quant scores from before the no-quant profile
fix and title-only scores that had never seen the Master's/PhD requirement
in the description).
Whether it emails follows the new judgment only; a failed scoring batch
keeps the old score instead of persisting the scoring-failed fallback.
Applied and skipped rows never resurface (history and feedback stay), and
re-judged reposts below the threshold stay suppressed.
Two cases are never a repost, because they are sibling requisitions running side by side, not a replacement.
The first is a new URL arriving while the stored row's own URL is still in the same fetch (Susquehanna lists one analyst title per office).
The second is a new URL whose location is empty or a "N Locations" placeholder, since then nothing tells another region's copy of the title apart from a repost.
Treating those as reposts once rewrote Google's US "Student Researcher" row with its Canada sibling's URL, re-judged it against the Canada page, and suppressed it as non-US (2026-09-25).

**Rows with no office are enriched before they are hashed.**
The content hash is `company|title|location`, so a row whose location is empty (Google's search page, iCIMS tenants such as Susquehanna whose cards omit it) or a Workday "N Locations" placeholder cannot tell regional requisitions of one title apart.
Until 2026-09-25 every regional copy shared one hash, only the first card on the page was stored, and a Taipei or Montreal copy could hide the US posting for good.
A row like that whose URL has never been seen now gets its detail page fetched before dedupe, so its hash carries the real office list.
Rows already stored keep matching by URL, so the change did not make old postings look new.
A row stored before this change has an id hashed from the empty location.
When a new URL for the same title turns out to have the same office as such a row, it is treated as that row (a replacement requisition resurfaces it in place, a same-office sibling collapses onto it), because Google, Workday and iCIMS boards can never close the old row and a second row would sit in the tracker forever.
A new URL whose office differs from the stored row gets its own row.
A row whose detail page names only foreign offices goes through the free US gate again and is dropped there instead of reaching the scorer.
The gate checks each office on its own and drops the row only when every office fails.
A list with one US or unplaceable office goes on to the scorer, whose US flag judges it, because the gate misreads some US labels ("AF New London WI", "The Woodlands Hospital") and a mixed list ("Detroit; Windsor, Ontario") is a US posting.
Only the US gate runs again there; a clearance requirement found in the fetched text is left to the later clearance gate, which records the row as gated instead of dropping it unseen.
The old repost bug left some stored rows showing one office with the URL of another office's requisition.
When such a row's URL is still on the board and its page names a different office, a new requisition for the row's own office is not treated as that row, so it gets its own row instead of being hidden.
These calls share the per-run detail budget (`sources.ENRICH_CAP`, 400) with the main enrichment pass, and no row is fetched twice in one run.
A URL whose row is then dropped (a same-office sibling, a foreign copy, an Avature page that never names an office) is never stored, so the office its page gave is kept in the `detail_cache` table for 30 days and reused instead of fetching the page on every poll.
A detail call that fails outright is not remembered and is retried on the next run.
If it fails, the row keeps the empty location and still collapses onto the stored hash - the old behavior, minus the repost rewrite above.
Expect the first runs after this change to store several hundred rows that the empty-location hash had been hiding.
A read-only simulation of the hourly boards estimated 338 in the first hour, spread over about five runs as board clocks come due.
They are real, separate requisitions, but some are other offices of titles you already skipped or applied to, so those titles will appear again as new rows.

**`max_age_days`** (filters, default 14) keeps the broad tier from flooding
you with history: a posting first seen when its posted date is already older
than the cutoff gets recorded as seen-and-suppressed, silently. It's been on
the public lists that long, so it has been applied to already. Undated
postings are treated as fresh.

## Cycles

Unlike a summer-only tracker, this watches several cycles at once. `config.yaml`:

```yaml
filters:
  target_cycles:
    - {term: winter, year: 2026}   # Simplify's name for the Dec 2026 - Feb 2027 window
    - {term: winter, year: 2027}
    - {term: spring, year: 2027}
    - {term: summer, year: 2027}
    - {term: fall,   year: 2027}
```

`config.yaml` and profile.md must agree on which cycles are live: profile.md is where the candidate's own eligible cycles are stated, and `target_cycles` is what the scorer treats as in scope.
Fall 2026 was dropped from both on 2026-09-03 once the semester started; if a cycle is in profile.md but missing from `target_cycles`, the scorer buries every posting for that whole cycle as "wrong cycle" no matter how good the fit.
The test suite compares the bolded cycles in profile.md with `target_cycles` and fails when they differ.

A posting is dropped only when its title advertises a cycle that isn't on that
list. Titles naming no cycle at all - which is most of them - survive to the LLM,
which reads the description for start dates and reports the cycle it finds. The
digest is one list ranked by score, best first, with unscored pass-through rows at the end and each entry's cycle on its meta line (until 2026-09-25 it was grouped into cycle sections, which buried the best postings under whichever cycle sorted first).
A year attached to a graduation phrase is a class year, not a cycle: "Software Engineer Intern - Class of 2028", "(May 2028 Graduates)" and "(2028 grads)" are erased before the title is read, so a 2027 internship for 2028 graduates is not dropped as a 2028 cycle (`prefilter.GRAD_PAT`, 2026-09-15).
The scorer gets the same rule: its prompt states the May 2028 graduation and says a posting for the class of 2028 (graduating December 2027 through August 2028) is the candidate's own class, never a cycle mismatch.
Only a work term in summer 2028 or later is out of scope as a cycle (2026-09-25).
The scorer compares a stated graduation window by month, not by year.
Sophomore programs write their window as "December 2028 - August 2029", which mentions 2028 but excludes May 2028.
A window can also end too soon: "graduate in December 2026 or 2027", "Graduation Dates: December 2026 - June 2027" and a "2027 Graduates" title all want students who finish before May 2028.
A window that excludes May 2028 on either side, a class of 2027 or earlier, a class of 2029 or later, or a class-standing requirement he does not meet (freshman, sophomore, second-year) is a class-year gate and scores 15 or below.
A graduation or class year in the title is checked in code before any scoring (`prefilter.class_year_ok`, 2026-09-25).
When a title names class or graduation years ("Class of 2029", "2027 Grads", "2027 Graduates", "'28 grads", "2028-2029 grads", "Grad Year 2029", "graduating 2027") and none of them includes `filters.graduation_year` (2028), the posting drops with the reason "class-year gate (title)".
Ranges include every year between their ends, so "2027-2028", "Dec 2027 - May 2028" and "'27-'28" pass, and "or later" or "2027+" opens the top end.
"Graduating by May 2029" means no later than 2029, so it opens the bottom end and passes.
A comma list names each year, so "Class of 2027, 2028, or 2029" passes and "Class of 2026, 2027" drops.
A comma list of graduation months is one class window too, so "Graduating Dec 2027, May 2028" passes.
A graduation list never goes down in year, so an item whose year is below the one before it is read as a work term instead: a month or term after any list separator (comma, "/", "&", "and", "or"), or anything after a dash.
So "Class of 2029, Summer 2027", "Class of 2029 / Summer 2028" and "Graduating May 2029, Summer 2028" drop on the class year, and "Class of 2029 - Summer 2027" keeps "Summer 2027" as the cycle.
Before a "Grads" word a comma list of months is one class window ("May 2028, Aug 2028 Grads"), but a leading season item before a comma is the work term, so in "Fall 2026, May 2028 Grads" only May 2028 is a class year and Fall 2026 is the cycle.
The cycle check never reads these class-year spans as work terms, so "Graduating Dec 2027 - May 2028" is not a 2028 cycle.
A bare year or a season year in a title ("Software Engineer Intern - 2027", "Summer 2027") is a work term and never triggers the gate, and neither does a singular "Graduate" after a year ("2027 Graduate Software Internships" means graduate students).
The check is year-level; month-level windows in a title ("Dec 2028 - May 2029 grads") pass to the scorer, whose prompt keeps the same rule as a second line of defense.
The gate was moved into code because the scorer missed title class years in batches of ordinary "2027" titles and once gated those ordinary titles instead.
An open-ended window that starts by May 2028 ("December 2027 or later", "Fall 2027 or later") includes him.
In summer 2027 he is a rising senior, so "rising sophomore" and "rising junior" are standings he does not have: "Open to rising juniors only" is a sophomore program and is gated.
Only an un-prefixed "junior" or "senior" (read by the 2026-27 school year), upperclassmen, or "rising senior" counts as a standing he has.
A spring graduation counts as May, so "Expected graduation Spring 2028" is his class.
Long descriptions are trimmed to a head plus the requirement lines from the rest, and graduation and class-standing lines count as requirement lines, so a class-year line late in a posting still reaches the scorer.
Those lines match with any whitespace between words, including a non-breaking space or a leftover "&nbsp;" entity, and short forms such as "Grad date", "Class year", "grads", "Class-of-2029", "2nd-year", "final-year" and "Open to juniors only" count too, with Unicode hyphens accepted in "first-year" and "second-year".

Aggregator feeds state the cycle in a structured `terms` field, and when it holds a real season and year it decides the cycle question instead of the title.
Two exceptions exist because Simplify often tags a new posting with last season's term (on 2026-09-25, 165 active rows posted in September 2026 were tagged "Summer 2026").
First, a `terms` entry is ignored as stale when the listing was posted in that season's last month or later; a September posting tagged "Summer 2026" can only mean next summer, so it goes on to the scorer, which reads the description.
The cutoff is the start of the last month, not the season's end, because nobody posts a summer internship in August of that same summer.
Waiting for August 31 split one Fervo Energy batch at midnight (its August 31 posting dropped while its September siblings passed) and dropped 40 July and August "Summer 2026" listings that were never stored.
The updated date is used only when a listing has no posted date, because Simplify bumps it for hundreds of rows a day on metadata edits (297 on 2026-09-03), so it says nothing about the season.
An entry that is not a season (for example "Co-op 2026") has no cutoff and is never treated as stale.
A tag stays live through April 30 (spring), July 31 (summer), November 30 (fall), and March 31 of the next year (winter, whose latest reading runs January to April).
If every entry with a season and year is stale, the title's own cycle decides, as for a listing with no `terms` at all; a leftover "N/A" entry does not stop that.
Second, a title that names a target cycle and contradicts the tag beats it, which is how PrizePicks' and Rippling's Summer 2027 internships were being dropped as "wrong cycle (terms)".
The title contradicts the tag when it names a target season with its year ("Software Engineering Intern - Summer 2027"), or when it names only years and none of them is a year in the tag ("... - 2027 Start" against "Summer 2026").
A title year that the tag shares does not count: TikTok's "... Project Intern - 2026 - BS/MS" tagged "Fall 2026" is dropped, even though winter 2026 is a target cycle.
A current tag still decides: "Summer 2026" on a bare title posted in February 2026 is dropped, and so is "Fall 2026" posted in September 2026.
When the title beats the tag on a listing that bypasses the scorer, the listing gets no cycle label, so the tracker places it by its title rather than by the tag.
A listing whose tag was set aside either way is also exempt from the stale-on-arrival cutoff (`max_age_days`): it sat on the public list under the wrong season, so nobody reading the list by season has seen it.

Roll the list forward as cycles close, in both `target_cycles` and profile.md.
The out-of-scope examples and the graduation date in `score.SYSTEM` are written out by hand, so check them at the same time.

## Setup

```bash
pip install -r requirements.txt

export OPENAI_API_KEY=sk-proj-...   # the scorer (gpt-6-luna by default); the org must be Tier 1 or higher
export ANTHROPIC_API_KEY=sk-ant-... # the daily audit (Fable), audit-scorer, or a claude-* JOBCRAWL_MODEL
export DIGEST_TO=you@example.com,friend@example.com   # comma-separated list; every email (digest, alerts, audit) goes to all of them

# optional - without these you just get a markdown file in digests/
export SMTP_HOST=smtp.gmail.com
export SMTP_USER=you@gmail.com
export SMTP_PASS=<gmail app password, not your real password>

# optional - only needed for the five `ats: firecrawl` entries
export FIRECRAWL_API_KEY=fc-...
```

### Verify your boards

Every token in `config.yaml` was checked against its live API on 2026-07-27 and
all 33 API-backed boards resolved. They still rot - companies change ATS
providers without notice, and a wrong token fails silently, which means a
company quietly vanishes from your feed forever. Re-run monthly:

```bash
python -m jobcrawl.cli verify
```

```
  OK    Anthropic                 416 postings (0 intern-ish)
  OK    NVIDIA                     40 postings (14 intern-ish)
  SKIP  Citadel                  needs FIRECRAWL_API_KEY
```

For anything that fails: open the company's careers page, look at the URL it
redirects to, and read the token off it.

| URL you land on | ats | token |
|---|---|---|
| `boards.greenhouse.io/acme` | `greenhouse` | `acme` |
| `jobs.lever.co/acme` | `lever` | `acme` |
| `jobs.ashbyhq.com/acme` | `ashby` | `acme` |
| `jobs.smartrecruiters.com/Acme` | `smartrecruiters` | `Acme` |
| `apply.workable.com/acme` | `workable` | `acme` |
| `acme.wd5.myworkdayjobs.com/en-US/Careers` | `workday` | `acme\|5\|Careers` |
| `wd3.myworkdaysite.com/recruiting/acme/Careers` | `workdaysite` | `acme\|3\|Careers` |
| `ats.rippling.com/acme` (a locale like `/en-GB/` may sit in front) | `rippling` | `acme` |
| `jobs.gem.com/acme` | `gem` | `acme` |
| `acme.avature.net/en_US/careers` (often behind a vanity domain like careers.ibm.com) | `avature` | `acme\|en_US/careers` |
| an Avature portal that only answers on its vanity domain, like `careers.bain.com/jobs` (a token with a dot is taken as the host) | `avature` | `careers.bain.com\|jobs` |
| an Avature portal with no SearchJobs page, like Two Sigma's `careers.twosigma.com/careers/OpenRoles/` (the adapter falls back to `OpenRoles/intern` by itself; the token names the `*.avature.net` tenant so aggregator rows map back to it) | `avature` | `twosigma\|careers` |
| `jobs.eu.lever.co/acme` (Lever's EU region has its own API host) | `lever` | `eu:acme` |
| `amazon.jobs` (Amazon only; token is the search query, US-scoped server-side) | `amazon` | `intern` |
| an Eightfold site like `explore.jobs.netflix.net` (self-hosted domains work; shared `*.eightfold.ai` tenants often bot-wall; a tenant whose v2 list answers 403 "Not authorized for PCSX" is polled through its `/api/pcsx/search` endpoint instead, e.g. Boston Scientific) | `eightfold` | `host\|domain[\|query]` |
| `atlassian.com/company/careers` (Atlassian only; its iCIMS portals are shells that redirect there, so use this, not `icims`) | `atlassian` | `listings` |
| an Oracle Cloud HCM site like `jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001` (JPMorgan, American Express, Oracle itself; the site number is in every posting URL; read newest-first back 30 days, since the keyword search matches descriptions too) | `oraclecloud` | `host\|site` |
| an iCIMS Attract (Jibe) site like `careers.amd.com` (posting URLs end in `?icims=1`; descriptions come inline) | `jibe` | `careers.amd.com` |
| a SAP SuccessFactors career site like `jobs.sephora.com` or `jobs.netapp.com` (posting URLs end in `/job/<slug>/<10-digit id>/`; the `/search/?q=intern` page is read newest-first, 25 rows a page, and the posting page fills in the description) | `sapsf` | `jobs.sephora.com` |
| `jobs.apple.com` (Apple only; token is a team filter, `internships-STDNT-INTRN` is Students > Internships; the search page's embedded JSON is read, 20 a page) | `apple` | `internships-STDNT-INTRN` |
| `lifeattiktok.com` or `joinbytedance.com` (both front ByteDance's supplier API; every US city from the site's filter config goes into the search, descriptions come inline, no dates) | `bytedance` | `tiktok` or `bytedance` |
| `higher.gs.com` (Goldman only; its GraphQL gateway, campus roles) | `goldman` | `students` |
| `metacareers.com` (Meta only; one Relay GraphQL call answers every match; needs `curl_cffi`, since the site refuses every plain TLS client) | `meta` | the search query, `intern` |
| `tesla.com/careers` (Tesla only; the site's careers state JSON, read through Firecrawl because Akamai refuses EC2; token is the site code) | `tesla` | `US` |
| `citadel.com` or `citadelsecurities.com` (their WordPress career sitemap, read through Firecrawl because both edges refuse EC2; titles are rebuilt from the slugs, the posting page fills the rest) | `citadel` | `citadel.com` |
| anything else | `firecrawl` | the full URL |

Two Workday spellings of one name: DNS forbids underscores, so a tenant like
`osv_cci` appears as `osv-cci` in the hostname.
Use the hostname spelling in the token; the adapter retries the API path with
underscores when Workday answers 422.

Verify checks the token resolves, not that it belongs to the right company.
Ashby slugs in particular collide - `ashby/runway` is a company called cfo.ai,
not Runway, which lives at `ashby/runway-ml`. When a board resolves but shows
implausible titles, read a few before trusting it.

## Firecrawl policy

Firecrawl is the only metered source, so it is the last resort and runs on a
slower clock than everything else. `filters.firecrawl_every_hours` (default 6)
means scraped boards poll four times a day instead of twenty-four, cutting
credit burn ~6x without losing coverage - custom career pages do not change
hourly. Override per board with `every_hours:` (0 puts a board back on the
hourly clock). A skipped board produces no fetch outcome, so closure tracking
and health streaks ignore it automatically.

Before adding a `firecrawl` entry, check whether the "custom" page is really a
standard ATS behind a vanity domain: fetch it and grep for `greenhouse`,
`ashbyhq`, `lever`, `myworkdayjobs`, `icims`. That is how Mistral turned out to
be `ashby: mistral.ai` (169 postings, free, replacing a firecrawl entry whose
URL no longer resolved) and how Hudson River turned out to keep a free
Greenhouse board alongside its scraped page.

The GitHub aggregators cost nothing and never touch Firecrawl: Simplify,
vansh, and sndsh404 are plain `raw.githubusercontent.com` fetches with ETag
revalidation, polled hourly like any other free board.

Two own-site adapters ride Firecrawl as a plain fetcher rather than an extractor, because their edges refuse this crawler's IP range whatever the client looks like (2026-09-28: Tesla's Akamai and Citadel's edge both answer 403 to curl, requests and a browser-fingerprinted client from EC2).
`tesla` fetches one document, the site's careers state JSON, and reads every intern-typed opening in the US out of it; `citadel` fetches the WordPress career sitemap.
Each costs one credit per poll, and both entries sit on a 6h `every_hours:` clock.
Their rows carry no description, so each NEW row costs one more credit for its posting page in the enrich pass, once, and only after the prefilter has kept it.

## The HN Who's Hiring thread

The `hn` source polls Hacker News' monthly "Ask HN: Who is hiring?" thread through the free Algolia API - the one place tiny startups with no ATS at all announce openings.
The configured token is the posting account (`whoishiring`), and the adapter finds the newest thread itself, so month rollover needs no config change.
Each top-level comment is one employer's ad; the adapter keeps only comments whose text matches the same intern regex the prefilter uses (the thread is ~95% senior full-time roles), parses the conventional `Company | Role | Location` header line with regexes only, and ships the full comment text as the description - so these rows are LLM-scored like any direct board.
When only the body mentions internships, the constructed title gets an explicit `- internship` suffix, because the prefilter gates on titles.
The comment permalink (`news.ycombinator.com/item?id=...`) is each row's stable URL identity, which absorbs comment edits.
Expect a handful of rows a month; the first crawl of a mid-month thread backfills its older comments once.
`hn` is not in `CLOSABLE_SOURCES` - comments never disappear from a thread, and month rollover must not mass-close last month's rows.

## YC Work at a Startup boards

The `yc` source polls a single YC company's jobs page (`ycombinator.com/companies/<slug>/jobs`).
The page is Inertia-rendered: the complete job list sits as HTML-escaped JSON in the root div's `data-page` attribute, so a plain GET gives structured data with no API key and no scraping budget.
The list payload carries no descriptions; each row gets a `detail_key`, and the enrich pass reads the job's own page, whose `data-page` holds the full markdown description plus salary, experience, and visa fields (appended to the text so the scorer sees them).
WaaS shows only relative posting ages ("4 days", "about 1 month"); the adapter converts them the way the Workday one does, with month-granularity values acting as coarse floors.
The page lists every open posting unpaginated, so `yc` is in `CLOSABLE_SOURCES`.
Use it for YC startups that hire nowhere else - Relari (W24) was the first, found via a LinkedIn alert on 2026-08-31 after the ATS probe confirmed it has no Greenhouse/Lever/Ashby board.

Since 2026-09-01 these boards are also minted automatically: when `filters.yc_sweep` is on, the weekly `boards` regen queries YC's public directory Algolia index for every `isHiring` company (the app id and search key are re-read from the /companies page each sweep; the ~1,500 hits exceed Algolia's 1,000-hit pagination cap, so the query partitions by the `batch` facet), fetches each company's jobs page, and writes companies with an internship-typed or intern-titled role to `boards_yc.yaml` on a 6h clock.
This replaces the paused Work-at-a-Startup firecrawl scrape at zero credit cost.
The design boundary: a YC company posting its FIRST intern role appears at the next weekly sweep (up to ~7 days), while already-minted boards surface new postings within 6 hours.
An Algolia failure or a failing wave of company pages aborts the regen loudly and leaves the old file in place - a wholesale-regenerated file must never be replaced by a partial sweep.

## Discovering new aggregators

```bash
python -m jobcrawl.cli feeds          # report new candidates
python -m jobcrawl.cli feeds --add    # write vetted ones to feeds_discovered.yaml
```

Searches GitHub for recently-updated internship-list repos and probes each for
a Simplify-shaped `listings.json`. Auto-add requires 50+ stars, 20+ active
listings, and a push within 21 days; everything else is reported only, because
a job feed is untrusted input written by a stranger. Vetted feeds land in
`feeds_discovered.yaml` (included by `config.yaml`), so the next hourly crawl
ingests them through the normal prefilter, scorer, and dedupe - no separate
dedupe path, no duplicates. Run it weekly (`jobcrawl feeds --add`).

## Importing your own applications

```bash
python -m jobcrawl.cli import-simplify ~/Downloads/Simplify_Tracked_Jobs.csv --dry-run
python -m jobcrawl.cli import-simplify ~/Downloads/Simplify_Tracked_Jobs.csv
```

Simplify exports your tracker as a CSV (Job Title, Company Name, Location, Job URL, Status, ...).
The command reads two different things out of it.

**What you applied to.**
Rows with `Status=APPLIED` are matched against stored postings - by URL first, using the same canonical key the rest of the pipeline dedupes with, so a link dressed with tracking parameters still hits; by lowercased company plus title as a fallback, for exports whose URL went through a redirector.
Matches move to `applied`, which puts a ✅ on the tracker and feeds the row into the scorer's calibration examples.

Only rows sitting at `new` or `skipped` flip.
An `applied` row is left alone, which is what makes re-importing a later export - one that still contains everything you imported last time - a no-op.
A `closed` row is left alone too, deliberately: the posting is gone from its board, and an export saying you applied to it in March is not evidence that it reopened.
Applied rows that match nothing stored are counted and sampled in the report, never inserted - this command reconciles the store, it does not populate it.

**Where you applied.**
Every row's URL, `APPLIED` and `SAVED` alike, is mapped back to a pollable board and diffed against every board already configured.
A saved posting names a board just as well as an applied one.
New candidates are fetched live, and only the ones that resolve are written to `boards_applied.yaml`; failures are printed with their error so you can read the real token off the company's careers page.
Verification runs under `--dry-run` too - it is a read-only GET, and a report that has not checked the tokens is not worth much.
`derive_board` never produces a firecrawl entry, so nothing metered can be touched from this path.

Verification proves a token resolves, not that it belongs to an employer.
A first import will pick up boards that answer but are not companies - Simplify's own integration sandbox, generic "US Summer Internships" landing boards - so read `boards_applied.yaml` once after the first run and delete the obvious junk.

Boards that exist only in the cold tier are treated as *not* configured here.
The cold tier is a slow clock that ships switched off; a board you applied to by hand belongs on the hourly one, and writing it to `boards_applied.yaml` promotes it.

## The cold tier

The listings feeds carry their entire history, not just what is open today: roughly 14,000 rows of which 2,000 are active.
The inactive 12,000 are last cycle's postings - and a company that ran an internship last cycle almost always runs one this cycle.
Their boards are proven leads that simply have nothing open right now.
`jobcrawl boards` mines them out of the same fetch it already makes and writes them to `boards_cold.yaml`, about 1,800 boards that no other tier reaches.

They cannot go on the hourly clock.
A run over today's ~670 boards already takes five of every ten minutes, and adding 1,800 more - half of them Workday, which costs ~20 throttled POSTs a board against one conditional GET - would take over an hour.
So the cold tier has two limits, and the second one is the one that matters:

```yaml
filters:
  cold_boards_per_run: 80    # master switch; boards per run, cheap ATSes (ships 0 = tier off)
  cold_workday_per_run: 20   # Workday's own, much smaller budget (ships 5)
  cold_every_hours: 4        # a board polled this recently is not due (ships 8)
  cold_promote_days: 30      # how long a yielding board stays promoted
```

The clock alone is not enough, because on the first run every cold board is equally overdue and they all come due in the same breath.
The **budget** is what bounds a run: it takes the least-recently-tried boards that are due and skips the rest, which staggers the whole tier permanently after one sweep.

The tier splits almost exactly in half by cost: ~907 cheap boards (Greenhouse, Ashby, Lever, iCIMS, SmartRecruiters, Workable, Rippling) and ~911 Workday ones.
At the live budget of 80 a run on a 12-minute crawl the cheap half sweeps in ~12 runs, about two and a half hours, so its real cadence is the 4-hour clock.
Workday's own budget of 20 a run takes ~46 runs, about 7.6 hours, for a full rotation - there the budget binds and the clock is irrelevant, which is why lowering `cold_every_hours` speeds up only the cheap half.
That is the deliberate trade - Workday boards are worth having (the applied import alone turned up Fidelity, PwC, State Street and ING) but not worth starving every other board for.

`cold_boards_per_run: 0` is the master switch and the shipped default.
It turns the whole tier off, Workday budget included.
This matters because the crawl runs unattended every 12 minutes: without it, committing `boards_cold.yaml` would start backfilling thousands of boards into your digest and your public tracker within minutes, with nobody watching.

**Promotion.**
A cold board that produces a genuinely new posting is moved to the normal clock for `cold_promote_days`, renewed each time it produces another - that is what makes the tier self-sorting rather than a permanent demotion.
A board's *first* successful crawl never counts.
Directly polled boards are exempt from `max_age_days`, so a first crawl backfills everything the board holds as brand-new rows; without that guard every cold board with anything open at all would promote on contact and the tier would evaporate into exactly the run-time explosion it exists to prevent.
Backfill is not activity.

**Closure at cold cadence.**
`close_after_misses` counts consecutive fresh fetches, so at the cheap half's 4-hour cadence a cold posting takes about three days to be marked closed instead of three hours; on Workday's ~7.6-hour rotation, about six.
That is conservative in the right direction and needs no special handling.

**Turning it on is not a one-line change.**
The documented seeding path (`run --no-score` to store everything suppressed, then `rescore`) assumes a manual first run, and the scheduled VM runs never pass `--no-score`.
A full cold sweep spans 23 runs for the cheap half and 183 for Workday - four hours and thirty hours - so nobody is going to babysit it.
Either stop the timer, loop `run --no-score` until the sweep completes, `rescore`, and restart the timer; or accept a scored trickle and understand that the real cost is not LLM spend (the prefilter cuts ~99%) but a tracker filling with thousands of backfilled rows at `min_score: 20`.
Decide which before raising the budget above zero.

## Watch list

The crawl runs every 12 minutes and a run takes about 9, so a posting reaches the digest 12 to 21 minutes after it goes live, and the batched email can hold it up to 30 minutes more unless the company is on `email_urgent_companies`.
For a board whose posting is expected imminently and gates on application order, that is queue position lost.
`watch:` in `config.yaml` names a few boards to poll every minute instead.
On the VM `jobcrawl-watch.timer` runs `python -m jobcrawl.cli watch` once a minute.
Each tick does one conditional GET per watched board (a 304 costs nothing), compares the URLs it sees against the state file `watch_state.json`, and emails at once for any new title that matches the intern regex and no seniority marker.
There is no cycle or location gate on purpose: a watched board fires rarely, a false alert costs one email, and a gate that swallowed the one posting the watch exists for would defeat it.
The first tick on a board only seeds the state and never alerts, because everything already on the board has been live for a while and the crawl has it.
The watch never opens `jobs.db` and never takes the crawl lock; the regular run stores, scores, tracks, and emails the same posting a few minutes later exactly as if the watch did not exist, so a watched board must also be in `companies:`.
A failing board never fails the systemd unit (that would fire the failure alert sixty times an hour); misses count in the state file and one email goes out per `filters.watch_alert_after` (60) consecutive misses.
Keep the list short and prune it once the posting has landed; every entry is one request per minute against that ATS.

## Tracker

The tracker IS this repo's root `README.md`, plus `QUANT.md`, `SUMMER.md`, and `COOPS.md` - Simplify-style
HTML tables (clickable Apply buttons, age column, ↳ for repeat companies),
while these docs live in `docs.md`.
Every page is one flat table, newest first, regardless of cycle.

Quant roles are split onto their own page because the quant track and
the general software track are different job searches with different
timelines, and one interleaved table buries whichever you are working on
today. The split is by role, not firm: `QUANT.md` gets everything the scorer
tagged `quant`, plus untagged aggregator rows whose title names a quant role
("Quantitative...", "Trader") - those rows never see the scorer, so the title
is the only signal.
`README.md` gets every other CS role, including a "Software Engineer Intern"
at a trading firm.
The pages link to each other with counts. The split is computed at render
time (`publish.is_quant`) and nothing about it is stored, so changing the
rule re-sorts every row on the next publish.

`SUMMER.md` and `COOPS.md` are season views of the general list (`publish.split_seasons`).
`SUMMER.md` carries the summer cycle plus every posting whose season could not be determined - season-less postings mostly mean summer, and a separate "unknown" page would just hide them.
`COOPS.md` carries the fall, winter, and spring terms plus anything calling itself a co-op.
The season comes from the scored `cycle`, with the title as fallback for the many rows whose cycle came back unknown; a title that says "co-op" always counts as a co-op, even a summer co-op, because the co-op/internship distinction is what the split is for.
A title that names a year but no season ("NVIDIA 2027 Internships: Software Engineering", "2027 Intern - Software Developer") counts as summer: such umbrella postings state no season in the description either, and summer is the main cycle they recruit for.
Like the quant split, this is computed at render time (`publish.season_of`) and nothing about it is stored.

Every page groups by company the way the Simplify list does, but bounded to one day at a time: rows are ordered
newest-first, then within each day every role a firm surfaced that day is
pulled together into a block, with the follow-on rows indented under a ↳.
A firm that posted on three different days appears three times, each block
at its own day's position - grouping never lifts an old posting above a
newer day. Grouping is name-normalized, so the same firm reaching us
under two board tokens ("Jane Street" and "Jane Street (intern)") or two
truncations ("Hudson River" and "Hudson River Trading") lands in one block
under the cleanest of its names.

Every run that surfaces new postings regenerates both pages and pushes them
as one commit; `jobcrawl applied` and `jobcrawl skipped` also republish,
flipping that row to ✅ or removing it. Push manually any time with
`python -m jobcrawl.cli publish`. The commit is skipped when both tables are
unchanged, only the two generated pages are ever staged (a dirty working tree
is never swept along), and a failed push never kills a run - the next
successful one republishes the full current state.

## Politeness and board health

Every HTTP call goes through `sources.request()`, which holds one request in
flight per host with a minimum gap between them, caps how many hosts of one
family run at once, and retries 429s and 5xx with `Retry-After` or exponential
backoff. This matters most for Workday: up to 20 POSTs per board across ~200
boards, fired unthrottled, drew rate limits - and a rate-limited board looks
exactly like an empty one, so the loss was silent. 404s are never retried,
because a dead board should reach board health immediately.

Boards that fail `filters.dead_board_fails` runs in a row drop to a probe every
`dead_board_retry_hours` (4 by default since 2026-09-01; it was 24, which left a recovered board invisible for up to a day).
The broad list is regenerated from public GitHub lists and keeps reintroducing tokens that no longer resolve; the slow retry still picks one up if it comes back, and a single success restores the hourly clock.

Boards that fail `retire_board_fails` runs in a row (40 by default, about five days on the 4h probe) are retired to a probe every `retire_board_retry_hours` (168, one a week).
The digest names a retired board once, in the week it crosses the line, and then stops mentioning it.

A board the platform itself reports gone skips those five days and retires the moment it counts as dead (`dead_board_fails` runs).
The crawler reads that from the response, not the streak (`cli.gone_cause`): a 404 or 410 from any ATS API, a Workday 422 (the tenant is decommissioned - an unknown site on a live tenant answers 404 instead), and an Ashby hosted page whose app data carries no job board at all (the organization was deleted or renamed; a live board with no openings still carries an empty list).
Such a board is named once in the digest with its cause, as "Gone from its platform", and is silent after that.
Anything that could be transient - 5xx, 429, malformed JSON, timeouts - keeps the slow path, and a platform-wide outage freezes streaks before any of this applies.
This is what happened on 2026-09-08: Greenhouse purged two boards in the same minute, Dell's Workday tenant answered 422 after Dell moved to Oracle, and an Ashby slug went null; each would otherwise have been a daily digest line for a week.
One success still puts any retired board straight back on the hourly clock.

A failure the whole platform shares is not board rot.
When at least `outage_min_boards` (5) boards on one family fail the same way in a single run, and they are at least half of that family's fetches, the run treats it as an outage: those boards' health streaks freeze instead of counting, and the next digest carries one line naming the outage.
A family is an ATS, except for Workday, where it is one data center (`wd3`, `wd5`...), because those go down independently.
This is what turned an 81-line board-health section on 2026-09-05 (75 of them one Workday data center answering non-JSON for seven hours) into a single line - and stopped every board on that data center being demoted to the slow probe for hours after the platform recovered.

## Daily use

```bash
python -m jobcrawl.cli run            # the whole pipeline
python -m jobcrawl.cli unstale        # score postings the old age cutoff buried
python -m jobcrawl.cli run --no-score # skip Claude: rows stored suppressed just under min_score, judged later by a plain `rescore`
python -m jobcrawl.cli run -v         # show why postings were dropped
python -m jobcrawl.cli list           # pending postings, with detected cycle
python -m jobcrawl.cli applied a3f9b201   # applied: ✅ on tracker, positive signal
python -m jobcrawl.cli skipped a3f9b201   # not interested: off tracker, negative signal
python -m jobcrawl.cli import-simplify export.csv  # bulk-mark applied + mine boards
python -m jobcrawl.cli stats
python -m jobcrawl.cli watch           # one tick of the minute-level board watch (config.yaml `watch:`)
```

`applied` and `skipped` feed back into scoring: the prompt includes your recent
real decisions as calibration examples, so the scorer converges on what you
actually apply to instead of guessing from the profile alone.
Only skips of postings that actually surfaced (scored at or above the threshold) count - rows suppressed by machinery, like stale-on-arrival aggregator listings or `unstale` re-scores that stayed below the bar, are never treated as your decisions.

Runs are cheap on the network side: Greenhouse, Lever, Ashby, and the Simplify
feed support ETag revalidation, so an hourly run turns most requests into 304s
and skips re-downloading unchanged boards entirely.

### Posting lifecycle

A posting that disappears from its board for 3 consecutive fresh fetches is
marked closed: 🔒 on the tracker, gone from `list`. Only sources whose
responses are complete (Greenhouse, Lever, Ashby, Workable, Rippling, the
Simplify feed) can close postings - Workday and SmartRecruiters paginate with
caps and Firecrawl extraction is flaky, so absence there proves nothing and
their postings simply age out visually. Board errors, 304s, and zero-posting
responses freeze the countdown rather than advancing it, applied rows are
never auto-closed, and a posting still present under an edited title (same
URL, different hash) counts as present - each row is matched to its own board
by URL, so one board's silence can never be mistaken for another's answer.

The digest also carries a **Board health** section when a board has failed 3+
consecutive fetches (or a Firecrawl board returned zero postings twice) - a
board that silently dies is a company quietly missing from your feed, and the
digest is the one place you actually look daily.
It stays short by construction: boards failing the same way on the same infrastructure collapse into one line per cause, a platform outage is one line, and boards retired to the weekly probe are named once.

Cron it hourly - the fetch is free, dedupe means quiet hours cost nothing, and
an hourly poll is the whole speed advantage over the GitHub lists:

```bash
crontab -e
0 * * * * cd ~/projects/job-finder && /usr/bin/python3 -m jobcrawl.cli run >> run.log 2>&1
```

Prefer one digest email a day? Keep the 7am-only line instead:

```bash
0 7 * * * cd ~/projects/job-finder && /usr/bin/python3 -m jobcrawl.cli run >> run.log 2>&1
```

**This is already live.** As of 2026-08-23 the run happens every 12 minutes on (10 until 2026-09-17)
a Linux VM (systemd `jobcrawl.timer` running `~/bin/jobcrawl-run.sh` against
the clone at `~/jobcrawl`), migrated from hourly GitHub Actions because
GitHub's cron scheduler is best-effort and delayed or skipped runs by up to
two hours.
`jobs.db` is committed and pushed whenever a run finds or closes postings,
and at least hourly otherwise - quiet 12-minute runs defer the commit so the
repo history does not grow 6x faster for boards-table noise.
The VM pushes with a repo-scoped deploy key, and secrets live in
`jobcrawl/.env` on the VM.
A failed run (including a failed push) triggers `jobcrawl-alert.service`
via systemd `OnFailure`, which emails the journal tail to `DIGEST_TO` -
the replacement for GitHub's workflow-failure emails.
Every git call that reaches GitHub in `jobcrawl-run.sh` and `jobcrawl-boards.sh` goes through `git_net` (sourced from `~/bin/jobcrawl-git.sh`), which retries up to three times 10 seconds apart:
GitHub intermittently rejects a valid deploy key with `Permission denied (publickey)` (six runs across 2026-09-23/24), and before the retry one rejection failed the run, sent an alert, and skipped a crawl cycle.
A rejection that persists through all three attempts still fails the run, so a revoked key is not hidden.
`crawl.yml` keeps a manual `workflow_dispatch` trigger as a fallback runner;
never re-enable its schedule while the VM timer is active - two writers
to `jobs.db` diverge.

Consequences worth knowing:

- **`jobs.db` in a laptop working copy goes stale within minutes.** `git pull`
  before `list`, `stats`, `applied`, or `skipped`; commit and push afterward
  (`git add -f jobs.db`) or the next VM run overwrites the change.
  The laptop's `jc` shell alias sidesteps all of this by running the CLI on
  VM against the live database.
- **Never add a second writer** - no local cron, no re-enabled Actions
  schedule - while the VM timer is active; two writers fork the history.
- **Concurrent commands on one machine serialize on a lock.** Every CLI
  command takes an exclusive flock on a `<db>.lock` sidecar next to the
  database for its whole lifetime, so a manual command started during a
  scheduled run waits for it (up to `JOBCRAWL_LOCK_TIMEOUT` seconds,
  default 600) instead of racing it, then fails with a "could not lock"
  error if the holder never lets go.
  The lockfile is deliberately never deleted (unlinking it would hand a
  fresh, unlocked inode to a third process while a waiter still spins on
  the old one) and never committed (`*.db.lock` is gitignored).
  Note this serializes jobcrawl processes only - a `git pull`/`stash` that
  swaps the db file under a running process is a separate hazard, which is
  why the pull-before-command discipline above still matters.
- **Run duration**: `filters.run_warn_seconds` (default 660, i.e. 11 minutes)
  warns in the digest when a single crawl approaches its own timer interval.
  This is a silent failure mode otherwise: the VM wrapper takes the crawl
  lock with `flock -n`, so a run that overruns does not overlap the next
  cycle - the next cycle simply exits, and the crawl halves its rate with
  nothing anywhere saying so. Runs reached 9.5 minutes on 2026-09-17 against a 12-minute
  timer, so the headroom is genuinely thin. Set to 0 to disable.
- **Scoring outage alert**: `filters.scoring_alert_after` (default 2) and
  `filters.scoring_alert_hours` (default 6).
  A scoring batch that fails is left unstored so the next run retries it.
  That is right for a transient failure and invisible for a permanent one: with a revoked API key every batch fails, nothing is ever pending, and no digest goes out to carry a warning.
  The Anthropic key was invalidated on 2026-09-26 and the crawl ran silent for two days.
  The run now counts consecutive runs in which every scored row failed (`meta.scoring_fail_streak`, reset by any run that scores a row).
  Once the streak reaches `scoring_alert_after`, the run emails a "scoring is down" alert naming the most common error and prepends the same warning to any digest, repeating the email every `scoring_alert_hours` while the streak lasts.
- **Email floor**: `filters.email_min_score` (25 in the shipped config, unset means the surfacing threshold) is the digest's own minimum.
  A row scoring between `min_score` and it is surfaced on the tracker pages but marked seen without an email, so the inbox carries only rows worth a look; NULL-score list rows are not compared and still email.
- **Email pacing**: `filters.email_batch_minutes` (default 30 in the shipped
  config) caps digests at one email per window; postings found in between are
  held and batched into the next one, and a held digest also flushes on a
  quiet run once the window opens. The tracker itself updates every run
  regardless. Set it to 0 to email immediately on every find.
  `filters.email_urgent_score` (default 70) punches through the window: when
  any pending posting scores at or above it, the digest sends immediately.
  Batching is for the near-threshold trickle, never for a strong match - an
  85-scored Amazon posting once waited 20+ minutes behind closed windows.
  `filters.email_urgent_companies` does the same for any pending posting
  from a listed company at ANY score, NULL-score list rows included (the
  big names that resist direct polling only ever arrive as list rows).
  Matching is whole-word-prefix and case-insensitive, so `Amazon` also
  catches "Amazon Web Services" but `Meta` does not catch "Metagenomi".
  An urgent send still stamps the window, so it re-times batching rather
  than stacking extra emails. Unset both to batch everything.
  If the SMTP send raises, the postings stay queued (`notified=0`) and the
  next run retries them - `notified=1` is a one-way door, and a reader who
  only checks email would never learn what a swallowed batch contained.
  Requeuing costs nothing: the rows are already stored and scored, so the
  retry re-renders and re-sends without any LLM spend. No SMTP configured at
  all is not an outage - there the digest file under `digests/` is the
  delivery, and those rows are marked seen as before.
- Watch runs on the VM with
  `ssh <vm> journalctl -u jobcrawl.service -n 50 --no-pager`.


## Tuning

Everything lives in two files.

**`config.yaml`** - which boards, which cycles, score threshold.
`min_score` currently sits at 20: it hides only clear misfits and lets the score mostly rank.
If the digest is noisy, raise it (at 65-75 it becomes a real filter); if it's empty for several days running, drop it and check with `-v` whether the prefilter is eating things it shouldn't.

After editing `profile.md`, run `python -m jobcrawl.cli rescore`.
It defaults to the near-miss band just under `min_score` (threshold minus 20 up to threshold minus 1), so the band follows the threshold wherever you set it; `--min`/`--max` override it.
Scores are frozen at first sight - dedupe means a posting is judged once, ever - so a profile change does nothing for postings already in the store until they get this second look.
Rows that newly clear the email floor (`email_min_score`, or the threshold when no floor is set) land in the next digest; rows that were already emailed keep their place and are never re-notified.
A row between the threshold and the floor was on the tracker but marked notified without a send, so lifting it over the floor counts as a rescue, not a repeat (Decagon's Summer 2027 intern went 20 -> 92 silently on 2026-10-11 before this rule).

`rescore --ids FILE` re-judges a named set of rows instead of the near-miss band, and tries to get each one a real description first.
FILE can be a plain list, one id or 8-char id prefix per line, with blank lines and `#` comments ignored.
FILE can instead be an `audits/*.md` report from `audit-scorer`; only the ids under its `## Rows to rescore now` section are used, everything else in the report is ignored.
Before scoring, the command refetches descriptions where the board allows: it re-fetches the row's board and matches postings back to stored rows by `url_key`, and for aggregator rows from Simplify it looks up the same detail page `enrich-list` uses.
Firecrawl rows stay title-only because firecrawl is metered and a hand rescore should not spend credits; hn and sndsh404 rows stay title-only because those sources expose no per-posting endpoint to refetch from.
A board that fails to refetch is reported and skipped; one dead board never aborts the rescore.
The command prints each rescored row as `old -> new` so you can see exactly what moved and by how much.
Applied and skipped rows are rescored like any other row - their score and tag update - but their status never changes and they are never re-notified, since a status the user already set by hand is not something a rescore should undo.
A skipped row whose tag is `filtered` or `stale` keeps that tag when it stays skipped (`store.update_scores`).
Those tags mark machine suppressions, and `store.feedback` ignores them; with the scorer's tag in their place, a high-scoring suppressed row would read as a posting the user saw and passed on.
One exception to "status never changes": a row the clearance gate buried (status `skipped`, tag `filtered`, why starting `requires security clearance`) is re-checked against the gate on its refetched text (`cli.split_regated`).
If the text still gates, or no text could be refetched, the row is left untouched and never scored.
If the text now passes, the row is scored; a score at or above the threshold returns it to status `new` with `notified=0` and the scorer's tag (`store.readmit`), so it reaches the next digest and the tracker, and a lower score leaves it skipped with tag `filtered`.
A lower score also keeps the clearance marker at the front of `why` (the scorer's reason follows it as `requires security clearance (rescored: ...)`), so a later `rescore --ids` still recognizes the row and can readmit it; one bad score cannot bury it for good.
This is how postings buried before the 2026-09-25 citizen-or-work-authorized fix get recovered.
Only `status='new'` rows that cross the threshold get `notified` reset, which is what puts them in the next digest.
The tracker republishes whenever any row crosses the threshold in either direction, since a row dropping below threshold must leave the tracker just as one clearing it must join.

**`profile.md`** - what Claude scores against. This matters more than the
threshold. The "Fit rules" section at the bottom is doing most of the work: it's
where you say what you *don't* want, which is what actually keeps the digest
short. Update it as your interests shift.

## audit-scorer

The scorer judges each posting once, and its mistakes are systematic - profile wording, prompt wording, model behavior - so one long-context read of every judgment finds patterns that no per-row rescoring can.
`audit-scorer` sends Claude Fable 5.1 (1M context) `profile.md` and the scorer's own system prompt.
Alongside those it sends every judged row: id, score, status, tag, cycle, date, company, title, location, and why.
Descriptions are not stored, so they are absent; the audit judges from title, company, location, and the scorer's own reasoning.
It asks for a markdown report with seven sections: Summary, Buried postings that look like strong fits, Surfaced postings that look like poor fits, Disagreements with the candidate's own decisions, Profile and prompt problems, Proposed edits, and Rows to rescore now.
The report lands at `audits/scorer-audit-<date>.md` and is never overwritten.
A filename collision gets a `-2` suffix, then `-3`, and so on.
The command opens the database read-only and writes nothing else - no scores, no statuses, no notified flags.
Defaults are model `claude-fable-5-1` and effort `high`, with effort choices from `low` up to `max`.
`--dry-run` calls the token-count endpoint and prints the row count, input tokens, and an estimated cost at $10/M input and $50/M output, without spending anything.
A refusal (`stop_reason: refusal`) exits 1 and writes no report.

## Which model scores

`JOBCRAWL_MODEL` names the scorer model, and the provider follows the name: a `claude-*` model posts to Anthropic's Messages API with `ANTHROPIC_API_KEY`, any other name posts to OpenAI's Chat Completions API with `OPENAI_API_KEY`.
The default is `gpt-6-luna` (16/16 on a rule battery, about $5 a month).
On 2026-09-28 the OpenAI org's free-tier cap of 50 requests a day stopped it for an afternoon (claude-sonnet-5 filled in on a fresh Anthropic key, 16/16 too at about $90 a month) until the org reached Tier 1; the OpenAI org must stay at Tier 1 or higher, which needs a payment method on file, credits alone do not count. An OpenAI 429 is retried only when the reset headers promise the window reopens within 60 seconds; a per-day cap fails the batch at once, because a blind backoff once stretched runs past the 25-minute service timeout.
A batch the primary model still refuses for capacity reasons (HTTP 429 or 402) is re-sent to the fallback model, `claude-sonnet-5` (`JOBCRAWL_FALLBACK_MODEL` overrides it; empty disables the fallback), at the same effort, and the run logs which model scored it.
The fallback covers rate limits and exhausted credit only; an invalid key (401) or a rejected request (400) is a configuration problem that the scoring-outage alert reports instead.
`filters.scorer_effort` sets the reasoning effort (low, medium, high; medium since 2026-09-28).
Both providers get the same system prompt and user message and both answers go through the same parser, so the prompt, the gates, and the failure handling below do not depend on the provider.
Cost: a full batch of 12 postings with descriptions is about 17k tokens in and 1k out; at a typical 450 scored postings and 95 batches a day that is roughly $0.15 a day, about $5 a month, at Luna's $0.10/M input and $0.50/M output.

## Daily scorer audit

`jobcrawl audit-daily` has a stronger model (`claude-fable-5-1` at effort high by default; `--model`, `--effort`; a non-Claude name goes to OpenAI) re-read the last day's judgments (`--hours`, default 24) and answer in JSON: a summary plus a list of suspected mistakes, each with a row id, a kind (surfaced-bad, buried-good, gate-missed, cycle-wrong, inconsistent), and a confidence.
Like the one-off audit it sees titles, locations, scores and the scorer's one-line reasons, never descriptions, so "high" confidence is reserved for a row whose own reason or title contradicts the scorer's rules (a gate named in the reason of a surfaced row, a class year or graduate title scored 20 or above, an unknown cycle penalized, a quant trader role scored 70 or above, the same posting scored 40 points apart).
Ids the auditor invents are dropped.
The report goes to `audits/daily-<date>.md`; its last section is the id list `rescore --ids` reads, and the report is emailed whenever anything was flagged.
When a day's high-confidence mistakes reach `filters.audit_bump_after` (default 3) and `filters.scorer_effort` is below high, the command raises it to high in config.yaml in place - the user's standing instruction from 2026-09-28, when the scorer moved to a small model.
On the VM `jobcrawl-audit.timer` runs `~/bin/jobcrawl-audit.sh` at 11:00 UTC daily: pull, audit (lock released, the DB is read read-only), then under the crawl lock `rescore --ids` on the flagged rows with fresh descriptions, and a commit of the report, config.yaml, and jobs.db.
A failure mails the journal tail through `jobcrawl-audit-alert.service`.
Cost is about $0.50 a day at 450 rows on Fable.

## When scoring fails

A scoring batch can come back unusable: the model answers with prose, a truncated body, or a shape the prompt did not ask for.
Those rows are never stored with the score-50 "scoring failed" fallback (scored-once would freeze it); they are left out of the run so the next run sees them as new and judges them again.
Two things make that retry real.
The parser accepts every answer shape seen so far: a bare array, a single object where a one-posting batch should have been a one-element array (about half of all one-posting answers from claude-sonnet-5 came back this way, which is how Snowflake's AI/ML Spring 2027 intern was dropped on 2026-09-25), a wrapper object around the array, and an array followed by a reasoning paragraph.
Whatever still fails is logged with the head of the raw answer.
And the boards those rows came from forget their etags, because a persisted etag would turn the next fetch into a 304 that never carries the posting again - on Ashby and Greenhouse boards dropped rows used to wait hours to days for something else on the board to change.
An aggregator row cannot name the feed it came from, so a failed aggregator row makes every configured feed re-fetch once.

## Cost

The ATS APIs are free. Claude scoring only sees postings that survive the
prefilter, which on a normal day is single digits - well under a cent. The
expensive day is the very first run, when everything currently open looks new.
Run the first one with `--no-score` to seed the database, then let the real runs
handle the genuine deltas.

## Known limits

- **Workday gives no real post dates**, just "Posted 3 Days Ago" text, which
  is approximated into a date; "30+ Days Ago" is a floor. Workday boards are
  searched twice ("intern" and "coop" - their tokenizer splits "co-op" into
  two terms and matches half the board) with results deduped by path.
  On top of the text searches, each board is probed once for an intern/co-op
  facet (`workerSubType` and friends); tenants that classify internships get
  an exact facet-filtered query too.
  Text search alone silently starved big tenants: "intern" also matches
  "internal" in descriptions, relevance ranking puts those first, and the
  real internships landed past the 200-posting page cap (Palo Alto Networks,
  737 hits for "intern", polled healthy for weeks while storing zero jobs).
- **A clearance or citizenship match is cancelled by a negation earlier in its sentence** ("will not be required to obtain a security clearance", "requires no security clearance") or a softener after it ("U.S. citizenship preferred"), and HTML headings end their line so "Requirements" cannot bind to the bullet under it (`prefilter.clearance_gated`, 2026-09-15).
- **A citizenship requirement that offers work authorization as an alternative is not a gate** (`prefilter._work_auth_alternative`, 2026-09-25).
  "Must be a U.S. Citizen or National of the U.S., an alien lawfully admitted for permanent residence, or an alien authorized to work in the U.S." (UPS), "US citizen, lawful permanent resident of the US or otherwise authorized to work in the US" (Merck), and "US Citizen or eligible to work in US without sponsorship" (Carrier) all pass to the scorer, because an F-1 student is authorized to work an internship.
  Before this, the citizen term alone matched, and those postings were stored skipped with `why='requires security clearance'` without the scorer ever reading them.
  The alternative must follow "or" or "and/or" in the same sentence and name work authorization itself: authorized to work, work authorization, eligible to work, able to work, the legal right to work, a work or student visa, an F-1 visa, visa holder, or work permit.
  A slash is not a connector, because "F-1/H-1B visa holders" is usually a list of excluded visas.
  A sentence ends at a period, a semicolon, or a line break; "U.S." ends a sentence only when a capital letter follows it, so "of the U.S., an alien ..." stays inside the UPS sentence.
  A capitalized noun that "U.S." modifies does not start a new sentence: "U.S. Work Authorization", "U.S. Permanent Resident", "U.S. Person", and "U.S. Export control" keep the sentence open.
  These still gate: a bare "must be a U.S. citizen", permanent residence or refugee status as the only alternative ("U.S. Citizen or Permanent Resident (must be able to work ... without visa sponsorship)"), a comma-joined added requirement ("citizen or national, able to work without visa sponsorship"), any sentence that mentions ITAR, EAR, or export, or is followed by one that does ("... or authorized to work in the U.S. This role is subject to ITAR.") (their "or eligible to obtain all required authorizations from the U.S. Department of State" is an export license, not a work permit), a refusal before the "or" ("we cannot sponsor or accept work visa holders"), an alternative the posting then rules out ("not eligible", "do not qualify", "cannot be considered", "need not apply", "are excluded", "excluding F-1", "other than F-1", "unable to hire visa holders"), even past a semicolon ("citizen or visa holder; however, visa holders are not eligible"), and a security clearance named later in the same sentence ("... or be authorized to work and have a DoD Secret security clearance", "or a clearance holder", "candidates without a Secret clearance must obtain one").
  A statement about sponsorship does not rule the alternative out: "visa sponsorship is not available", "visa holders will not be sponsored", and "not eligible for sponsorship" leave "or authorized to work" standing, because profile.md lists "we do not sponsor visas" as a non-gate.
  After a skipped citizenship match the scan resumes just past the match start, so a clearance requirement whose trigger word sits inside the skipped span is still found.
  The carve-out reads one sentence only (plus the next one for export wording), so a later sentence that excludes F-1 visas (EDF, Shell) is left to the scorer's hard-gate rule.
  Passing the prefilter is only half of it: the scorer prompt and profile.md name a status list that ends in "or an alien / otherwise authorized to work" as authorized-to-work wording, because without that example claude-sonnet-5 re-gated the UPS sentence as "citizenship gate" in about half of its runs.
  Plural "U.S. citizens" ("Only U.S. citizens are authorized to work on this program") is not matched by the citizenship pattern at all; that gap is older than the carve-out and fails open to the scorer.
- **The US-only gate drops on evidence, not on absence.**
  A location passes when it names a US hint (a state, a listed city, "remote"), drops when it names a recognized non-US marker (`prefilter.NON_US`: countries, regions, the foreign cities boards use bare, Canadian provinces, iCIMS's country-first `CA-ON-...`, SmartRecruiters' trailing ISO code such as `, de` or `, vn`, Workday's `SGP - ` ISO-3 site prefix, and the plant and campus labels big Workday tenants post to weekly), and passes as ambiguous when it names neither.
A bare two-letter prefix is never read as a US state: Compeer's `MN-Lakeville` is Minnesota but Workday's `IN-Pune` is India, so both go to the scorer unless something else in the string settles it.
  A US state code does keep a foreign CITY name from being decisive, because many of them are US towns too: Cardinal Health's Workday writes its Dublin, Ohio headquarters `OH-Dublin-Cardinal Place`, and NON_US's "dublin" made the gate's verdict on every internship there a drop from 2026-09-08 to 2026-09-25 (`prefilter._state_excuses`).
  No row was lost that way: the six Cardinal postings the old rule hid on 2026-09-25 were already stored (five from 2026-09-09, one through its Simplify copy), so the fix protects future postings.
  This covers the code before the city with any dash or a space (`OH-Dublin`, `FL - Melbourne`, Thrivent's en-dash `TN – Memphis`, `TX-The Woodlands;address`, Mass General Brigham's `MA Boston - PanAgora`), the code after the city with a space, a dash or parentheses and an optional ZIP (US Foods' `Paris TN`, `Perth Amboy NJ` and `Ontario CA`, Amcor's `ARP Manchester MI`, `Rome - GA`, `Dublin (OH)`, `Vienna VA 22182`), and the uppercase three-part tail ("Tysons, Vienna, VA").
  A US Foods or Amcor internship at one of those sites was dropped by every shape but `ST-City` until a second review on 2026-09-25.
  A code right beside the city also counts when a site label follows it (NiSource's `Columbus OH - Arena District` and `York PA-Op Ctr`, Agiliti's `Marlborough MA Branch`, Johns Manville's `Littleton CO JMTC`, Echo's `ITS - Ft. Worth TX (WH)`); such a code covers only the city it touches, while a code that starts or ends the office covers every city in it.
  No intern posting on those boards sat at a town `NON_US` names when this was added (a third review on 2026-09-25), so nothing was lost.
  Only a bare city name is excused: once the city names are removed, any other `NON_US` marker left in the office (a country, a province, an ISO-3 site prefix) still drops it.
  Multi-office strings are split on `;`, ` | `, ` / ` and ` • ` and each office is read on its own, so one US office passes the string whichever position it holds (`IN-Pune | OH-Dublin-Cardinal Place`).
  A field that is only a state code stays with the office before it, because Saint Luke's writes `Hospital | address | City | ST`: `Paris | TX` passes, and `Pune; IN`, `Munich, Bavaria; DE` and `London | CA` drop the same way for every separator.
  A code with no city of its own excuses nothing (`OH; Dublin` drops).
  A foreign region code in the same positions reads as a state too, before or after the city (`WA-Perth` and `Perth WA` are Western Australia, `TN-Chennai` and `Chennai TN` Tamil Nadu, and Italian addresses such as `Milano MI` or `20121 Milano (MI)` put the province code after the city); no stored row uses those forms, so those go to the scorer's `us` flag rather than a second list of region codes.
  A code followed by digits is a state only before a US ZIP (`Dublin OH 43017 - Site`), so UK postcodes and site ids (`Manchester WA14 2DT`, `Dublin DC1`) drop.
  Two letters after a 4-digit number are a Dutch postcode, not a state, before or after the city and however it is spaced (`Claude Debussylaan 34, 1082 MD Amsterdam`, `1082  MD Amsterdam` and `Amsterdam, 1082 MD` drop).
  A code set off from its digits by a dash still counts (`Moscow ID-6750` passes), so `Manchester WA-14` passes too and goes to the scorer's `us` flag.
  An uppercase connector word between two words (`OR`, `IN`, `DE`, `AS`, `AT`, `BY`, `TO`) is English or Spanish, not a state, when the office has no lowercase letters or both words beside it are foreign place names (`LONDON OR PARIS`, `London OR Paris`, `BASED IN PARIS` and `SANTIAGO DE QUERETARO` drop).
  In a mixed-case label an uppercase `IN` or `OR` before a site label is a state, since English writes those words lowercase there (`Warsaw IN Campus`, Warsaw, Indiana, and `Ontario OR Plant` pass).
  The same code at the start or end of an office, or set off by a dash, still counts in any case (`Ontario OR`, `Dublin OR - Branch`).
  PNC's Workday ends an office with a branch id that starts with the state code (`PNC Center - Cincinnati (OHA45)`, `Downtown Orlando Orange Avenue (FL361)`), and that id counts as a state code at the end of the office, so `Melbourne - Harris (FL361)` passes.
  A branch id needs three or four characters after the code (PNC's `FL361` and `OHA45`, Ferrovial's `GA0105` and `VAS095`); two digits alone are a UK postcode district or a short site id, so `Manchester (WA14)` and `London (DC12)` drop.
  A three-letter site code with digits (`Paris (PAR12)`) has the same shape as PNC's `OHA45`, so it passes to the scorer's `us` flag.
  A country word inside a US town or street name is an accepted loss: `Peru IN`, `Mexico MO`, `NY-Mexico` and UMB's `MN - Edina - 7701 France Avenue S` drop, because a state code never excuses a country word; no live intern posting used such a location on 2026-09-25.
  A code that is also a country code keeps that country's places decisive (`IN-Pune`, `DE-Berlin`, `CA-Toronto` and `CA - London` still drop, `IL - Waterloo` and `GA-Athens` pass), and a country or region name, a Canadian province, or the country-region-city form (`CA-QC-...`, `DE-BY-...`) still drops after any code.
  `CA-Ontario` is Ontario, California when nothing follows or a site label ending in a number does (`CA-Ontario-Med Prod #4551`), and so is US Foods' `Ontario CA`; a name or a street address after it is a Canadian city with the province spelled out (`CA-Ontario-Kanata`, `CA-Ontario-Brampton 8 Nelson St`) and drops, as does `Hamilton, Ontario CA`.
  A spelled-out US state before a three-part tail settles it the other way ("Ontario, California, CA" passes), and "IN-OFFICE" is an English word, not Indiana.
  The place words are listed once, per country (`prefilter.COUNTRY_PLACES`, `OTHER_COUNTRIES`, `OTHER_CITIES`), and `NON_US`, the country-name check and the per-code place lists are all built from those lists, so a new city added for India drops after `IN-` the moment it drops bare.
  Before 2026-09-08 the gate dropped anything without a US hint, and a board that labels its office instead of its city lost every US posting: Barclays' Workday "Wilmington, 125 South West Street" (its 2027 Technology Developer internship) and Compeer's "MN-Lakeville; WI-Eau Claire".
  Measured on the day's drops across the Simplify feed and the Bosch and Micron boards, the marker list re-drops every one of them; what comes through is office labels, which the scorer's `us` flag settles.
  The ISO-code check applies only to a single `City, Region, cc` string with a lowercase code, which is how SmartRecruiters writes it; an uppercase US state in that last position is a neighbourhood or campus label ("Hunt Valley, Cockeysville, MD", Workday's "US, Louisville, CO") and passes unless the fields before it name a foreign place ("Toronto, ON, CA" still drops).
  Accented city names ("Española, NM"), Puerto Rico and DC count as US.
  A location that joins several offices (Greenhouse's `San Francisco, CA • New York, NY`, or `|`, `/`, `or`, `and` lists) is read office by office through the hint and marker lists instead: from 2026-09-08 to 2026-09-15 the ISO check swallowed those strings whole, took the last state for a country code, and dropped every multi-office US posting, including Figma's summer 2027 internships.
  Ashby and Lever keep a posting's other offices outside its primary location (Ashby's `secondaryLocations`, Lever's `categories.allLocations`), so both adapters join every office into one `; `-separated string, primary first.
  Until 2026-09-25 they read only the primary office, and a posting whose first office is abroad was dropped even when it also listed a US one: Cohere's Winter 2027 internships (primary "Canada", secondary "United States") and Waabi's research internship ("Toronto, ON" plus "San Francisco, CA") never reached the store.
The first run on the new gate flushed 872 previously dropped Workday rows in one 19.6-minute pass; 458 came back `us=false` and were suppressed, 19 surfaced, all of them US, and the runs after it were back at the 8 to 9 minute baseline.
- **A Workday posting spanning several offices reports its location as just
  "4 Locations"**, so the US-only regex gate treats that pattern as ambiguous
  and passes it through.
  Enrichment then swaps the placeholder for the real office list from the
  detail call, and the scorer - which knows every city, unlike the hardcoded
  `US_HINT` list - returns a `us` boolean alongside the score.
  The flag is honored only with evidence to judge from - a real location that names no US office, or a non-US city in the title.
  A `false` on a location that unambiguously names a US office is overridden to `true` (`score.us_located`): "United States", "USA", "U.S.", an uppercase "US", iCIMS's `US-XX-` prefix, SmartRecruiters' trailing `, us`, a spelled-out state name, or an uppercase state code after a real place name ("Charlotte, NC", "Durham NC").
  After a comma the code may carry the decoration boards add ("Boise, ID - Main Site", "Seattle, WA (Hybrid)", "Plymouth, MN 55441", "Charlotte, NC, 28202", "Charlotte, NC.", "Charlotte, N.C."); without a comma it must end the string or precede a ZIP code, so "Engineering Building ME - Floor 2" is not Maine.
  Unicode dashes are read as "-" and zero-width characters are dropped before any of this runs.
  A state code that is also an ISO country code (AL, AR, AZ, CA, CO, DE, GA, ID, IL, IN, KY, LA, MA, MD, ME, MN, MO, MS, MT, NC, NE, PA, SC, SD, TN, VA) needs more than its shape when it is the bare last comma field of an office.
  YC and Eightfold write foreign offices as "City, Region, CC" ("Tel Aviv-Yafo, Tel Aviv District, IL", "Bogota, Bogota, CO"), so after a region field the code is a country unless that field is a US state name or code: "Charlotte, North Carolina, NC" is US.
  The two-field "City, CC" ("Westminster, CO", "Durham, NC", "El Segundo, CA") is read as the US state.
  Of about 5,100 stored offices of that shape on 2026-09-25, every one was a US town except iCIMS' "Remote, CA" (Kinaxis, Ottawa).
  A foreign one is vetoed by the `NON_US` city list, which runs first: "Toronto, CA", "Haifa, IL", "Bogota, CO", "Casablanca, MA" stay ambiguous, and the scorer's verdict stands.
  That list now holds the main tech-hub cities of Israel, Colombia, Morocco and Canada for this reason (Haifa, Herzliya, Petah Tikva, Jerusalem, Medellin, Rabat, Brampton, Saskatoon, among others).
  Canadian cities that share a name with a US town cannot go on that list, so after ", CA" they stay ambiguous too: Cambridge, Hamilton, Kingston, Windsor, Burlington, Victoria.
  A head that is not a place ("Remote, CA", "Hybrid, IL") stays ambiguous.
  DE and IN are stricter, because Workday writes its German and Indian offices the same way ("Göttingen, DE", "Bangalore, IN"): there the city before the code must be one the `US_HINT` list names ("Wilmington, DE", "Indianapolis, IN") or a spelled-out state.
  That known-city rule also settles a code after any number of fields ("Chicago, Illinois, IL").
  Before the city is read, the decoration around it is dropped: a site in parentheses, nested or not ("Chicago (Green St (West)), IL"), an office wrapped in parentheses ("HQ (San Francisco, CA)"), a label before " - " or a work-mode prefix ("Hybrid - San Francisco", "Hybrid-San Francisco"), and area wording ("Greater Chicago Area", "San Francisco Bay Area", "Denver Metro", "Downtown Boston").
  A decorated code ("Boise, ID - Main Site") is US as well, since country tails never carry decoration.
  "Dublin, OH" stays ambiguous: Dublin is a `NON_US` city and vetoes the office before any of this runs.
  Until 2026-09-25 only a spelled-out state before the code counted, so a wrong `false` on "San Francisco, CA", "Westminster, CO" or "Durham, NC" was honored and buried the posting.
  "Puerto Rico" counts as a US country marker.
  The location is read office by office (split on `;`, bullets, pipes, ` / `, ` & `, " or ", " and "), and one US office is enough.
  An explicit country field wins over a `NON_US` word in the same office, because those are US towns named after foreign places: Merck's "USA - Pennsylvania - North Wales", J&J's "New Brunswick, New Jersey, United States of America", Northrop's "United States-Florida-Melbourne".
  A state name or code is weaker, so a `NON_US` marker in the same office still vetoes it ("Toronto, ON, CA", "Dublin, OH"), unless the foreign-sounding word sits right before a spelled-out state ("Ontario, California", "Vancouver, Washington"; not "Georgia", which is also the country).
  Text that names the US to exclude it ("Non-US", "ex-US", "outside the US", "excluding US", "not US", "no US", "Anywhere but the US", "US excluded", "US-India Offices") is never a marker.
  Neither is a US mention that only names working hours or who the work is for ("US hours", "US team", "US Delivery", "US-based team", "US-based clients").
  "US-based" alone still means located in the US ("Remote (US-based)"), and so does "but" before a narrowed US ("Remote, but US-based", "Remote but US only").
  The "US-India" exclusion needs a hyphen and an offshore country name (India, China, the Philippines, Mexico, Brazil, Poland, and similar).
  A slash list ("US/Canada"), "US-Canada", and Stripe's "US-<City>" offices whose town shares a foreign name ("US-Dublin", "US-Melbourne", "US-Woodlands, TX") are US.
  `DE` and `IN` codes are not markers, because YC and Workday also write Germany and India that way ("Munich, Bavaria, DE", "Pune, IN").
  A title that names a non-US place keeps the `false` whatever the location says.
  The title is read with `prefilter.NON_US_PLACES`, the place names alone: the location-shape anchors in `NON_US` misread titles ("Co-op-" looks like a country-city prefix).
  A title place right before a US state code ("Vancouver, WA", "Ontario, CA"), "New England", and "University of <name>" are not foreign places.
  Neither is a title place that a US office of the same posting also names: J&J's "Athens Manufacturing Plant 2027 Co-Op" at "Athens, Georgia, United States of America".
  A parenthesized practice or subject names the work, not the office: UHY's "2027 Audit (China Practice) Winter Internship - New York".
  Known gaps: a foreign region code that is a US state code but no ISO country code, after a city `NON_US` does not list ("Fremantle, WA"), reads as US.
  So do foreign towns that hold a state word ("Kutaisi, Georgia", "Washington, Tyne and Wear", "Virginia Water, Surrey").
  So do a bare code with no comma ("Haifa IL"), a decorated foreign tail ("Haifa, IL - Hybrid"), and "Puerto Rico, Misiones, Argentina", since "Puerto Rico" is a country-strength marker.
  No stored row has those shapes.
  LSEG's summer 2027 "Engineering Summer Internship Programme" at "Greater Charlotte Area, United States" came back `us=false` on 2026-09-17 and was suppressed with a real score of 55 before this rule.
  A `false` on a bare title with a blank or placeholder location is overridden to `true` (the model guessed non-US on ten bare US titles in one rescore, 2026-09-09), and the enrich pass now fetches a detail page for any row missing a location, not only rows missing text, so blank locations are rare to begin with.
  That flag is a blocker, never a score input: a posting whose every office
  resolved outside the US keeps its real fit score but is stored suppressed
  (`status='skipped'`, `tag='filtered'`, off the tracker and out of the
  feedback loop), while a role offered both in the US and abroad surfaces
  normally.
  Anything ambiguous - detail fetch failed, flag missing, scoring fell back -
  fails open and surfaces.
  (Before 2026-08-24 the placeholder survived all the way to the digest, which
  is how all-China NVIDIA and all-Vietnam Hitachi internships got emailed.)
- **SmartRecruiters, Workable, and Rippling list APIs carry no descriptions**,
  but prefilter survivors get a per-posting detail call (`sources.enrich`)
  that fills in the full text before scoring - and backfills Rippling's
  posted date. Only postings that fail enrichment fall back to title-only.
- **The simplify feed's undated hosts give no post dates**, so `max_age_days`
  can't age-gate them; dedupe still ensures you see each posting only once.
- **Simplify's feed tracks Summer/off-season 2026-2027 cycles.** When they
  roll to a new repo for later cycles, update the URL in `config.yaml`'s
  simplify entry - the `boards` command reads it from there.
- **Citadel's careers page 403s plain HTTP clients.** Firecrawl renders JS and
  should get through, but that entry is the least certain of the five.
- **No pagination on Greenhouse/Lever/Ashby** - they return everything in one
  response, which is fine up to a few thousand postings.
- **Jane Street needs two entries.** Its Greenhouse board carries full-time
  roles only; internships are posted exclusively on janestreet.com.
