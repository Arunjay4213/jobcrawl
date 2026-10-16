# jobcrawl

jobcrawl watches company career boards at the source and emails a scored digest of new internship postings, so you see a posting the morning it goes up instead of whenever an aggregator gets around to indexing it.

It reads the same JSON that a company's careers page reads, straight from the applicant tracking system (Greenhouse, Lever, Ashby, Workday, and others).
A regex prefilter drops irrelevant postings for free, an LLM scores what is left against your profile, SQLite makes sure you never see the same posting twice, and the result arrives as an email digest and a markdown tracker.

## In production

I run it for my own internship search on a small Linux VM under systemd timers.
Numbers from its database on 2026-10-16:

- Running unattended since 2026-07-28: 80 days and 7,358 crawl runs.
- 5,894 career boards tracked: 14 applicant tracking systems, plus custom company APIs (Google, Amazon, Apple, Meta, and others) and public listing feeds.
  The largest are Workday (1,807 boards), Greenhouse (1,189), Ashby (833), iCIMS (394), and Lever (390).
- About 5,400 boards fetched successfully in a typical day.
- About 872 million postings fetched in total, about 118,000 per run on average, with a peak of 194,000 in one run.
- 2,248 boards answer conditional requests (ETags), so an unchanged board costs one empty `304` response instead of a full download.
- 33,038 postings tracked, of which 712 were detected as closed after disappearing from their boards.

## How it works

```
ATS JSON APIs  ->  regex prefilter  ->  LLM scoring  ->  SQLite dedupe  ->  email digest + tracker
```

Most ATSs serve public, unauthenticated JSON endpoints, so about 90% of boards need no scraping at all.
Workday needs a POST but is just as open.
Requests are throttled per host, and a board that keeps failing moves to a slower retry clock instead of being hammered.

Hand-curated boards are polled on every run.
More boards are discovered automatically from public internship lists, YC's directory, VC portfolio pages, and companies' own websites.
A cold tier of thousands of boards that hired last cycle is checked a fixed number at a time per run, least recently tried first, so the load is spread out instead of arriving all at once.

Postings are keyed by content hash and by normalized URL, which catches the same job reposted under a new ID.
A posting that stays missing from its board for several runs is marked closed.

An LLM rates each surviving posting from 0 to 100 against `profile.md`.
Postings you mark as applied or skipped go back to the scorer as calibration examples, and each morning a stronger model audits the previous day's scores.

A separate watch-list timer checks a short list of priority boards every minute and emails new intern titles right away.

[`docs.md`](docs.md) has the full design: board tiers, the posting lifecycle, politeness rules, tuning, and cost.

## Running it

```bash
pip install -r requirements.txt

# edit profile.md to describe yourself, then seed the database without spending on scoring
python -m jobcrawl.cli run --no-score

# later runs only score genuinely new postings
export OPENAI_API_KEY=...        # default scorer; a claude-* JOBCRAWL_MODEL uses ANTHROPIC_API_KEY instead
python -m jobcrawl.cli run
python -m jobcrawl.cli list
```

Email delivery, the tracker push, and the scheduled deployment are optional.
See the Setup section of [`docs.md`](docs.md), and [`deploy/`](deploy) for the wrapper scripts and systemd units the production VM uses.

## Notes

The board lists in this repository are public data.
My own database, digests, and application history live in a separate private repository.
