"""
Stage 1: free, deterministic filtering.

A big company board can return 2,000 postings. Sending all of them to an LLM is
wasteful and slow. This cuts to the plausible few dozen using nothing but the
title and location, then Stage 2 (score.py) does the actual judgment.

Bias here is deliberately toward false positives - it's cheap to let a maybe
through to the LLM, expensive to silently drop a real match.
"""

from __future__ import annotations

import datetime as _dt
import re

# Internships and co-ops only: "early career" and "university grad" are
# full-time new-grad markers, not internships, and the user wants them out.
# Plurals must match too: NVIDIA titles its 2027 batch "NVIDIA 2027
# Internships: Software Engineering", and \binternship\b misses the trailing
# "s". "internal"/"international" still fail the boundary after "intern".
INTERN_PAT = re.compile(
    r"\b(intern(ship)?s?|co-?ops?|summer analysts?|apprentices?|"
    r"university (program|talent)|student (program|researcher)|"
    r"residency|residents?)\b",
    re.I,
)

# Seniority markers that mean this is definitely not an internship, even if the
# word "intern" appears somewhere (e.g. "Manager, Internship Program").
# "staff" excludes "Member of Technical Staff", which AI labs use as the job
# title for everyone, interns included ("Member of Technical Staff, Intern" -
# Pace, Abundant, Architect Labs, 2026-09-15 sweep); the lookbehind is fixed
# width, as Python requires.
SENIOR_PAT = re.compile(
    r"\b(senior|(?<!member of technical )staff|principal|lead|head of|director|manager|vp|"
    r"vice president|architect|chief|iii|iv|l[4-9])\b",
    re.I,
)

# Roles gated on a US security clearance are unappliable regardless of fit.
# Titles state it outright; descriptions only count when a requirement-style
# word sits near the clearance term, because defense-adjacent postings often
# just mention that a clearance "is a plus" - those stay in.
CLEARANCE_TITLE = re.compile(
    r"\b(?:ts/?sci|top secret|secret clearance|security clearance|"
    r"clearance (?:required|eligible)|active clearance|polygraph|"
    r"cleared (?:professional|engineer|personnel))\b", re.I)
# Requirement words need \b on both sides ("current" was matching inside
# "concurrent", "hold" inside "stakeholder"), negations must not count as
# requirements ("no security clearance required" is an invitation, not a
# gate), and a softener after the clearance term ("is a plus", "preferred")
# means the trigger word was about eligibility, not a hard requirement.
CLEARANCE_DESC = re.compile(
    r"(?<!\bno )(?<!\bnot )"
    r"\b(?:must|require[ds]?|requirements?|active|current|possess(?:es)?|"
    r"hold|obtain)\b"
    # The span between trigger and term may not carry a negation either:
    # "requires no security clearance" is the opposite of a requirement.
    r"(?:(?!\b(?:no|not)\b)[^.\n]){0,60}?"
    r"(?:security clearance|ts/?sci|top secret|secret(?:-level)? clearance|"
    r"polygraph)"
    r"(?![^.\n]{0,40}(?:a plus|preferred|not required))"
    r"|(?<!\bno )(?:security clearance|ts/?sci|top secret)[^.\n]{0,30}"
    r"(?<!\bnot )(?:required\b|is required|a must)"
    # Citizenship / US-person (ITAR) gates belong here too: "Applicants must
    # be a U.S. person (U.S. citizen, permanent resident...)" is exactly as
    # unappliable as a clearance and surfaced from Advanced Space's board
    # (2026-08-25). Same conservatism: requirement wording must sit next to
    # the term. Work-authorization language ("authorized to work in the US")
    # is NOT a gate - an F-1 student has internship work authorization - and
    # never matches because the term must be followed by citizen/person.
    # The gap here is 30, not the 60 the clearance branches use: real
    # citizenship requirements bind tightly ("must be a U.S. citizen"),
    # while a 60-char reach let "Should the position require, ..." boilerplate
    # connect to "U.S. person status" across a whole subordinate clause and
    # bury a Kodiak posting that never required it (caught by audit,
    # 2026-08-25).
    # The citizenship branches are one named group so clearance_gated can
    # tell them apart and apply the work-authorization carve-out below.
    r"|(?P<cit>(?<!\bno )(?<!\bnot )\b(?:must|require[ds]?|requirements?|only)\b"
    r"(?:(?!\b(?:no|not)\b)[^.\n]){0,30}?"
    r"(?:u\.?s\.?|united states) (?:citizen(?:ship)?|persons?)\b"
    # "U.S. citizenship preferred" under a Requirements heading is not a gate.
    r"(?![^.\n]{0,40}(?:a plus|preferred|not required))"
    r"|(?<!\bno )(?:u\.?s\.?|united states) citizenship\b[^.\n]{0,30}"
    r"(?<!\bnot )(?:is )?required\b"
    r"|(?:u\.?s\.?|united states) persons? only\b)"
    r"|\bitar\b[^.\n]{0,60}\b(?:must|require[ds]?|requirements?)\b"
    r"|\b(?:must|require[ds]?|requirements?)\b[^.\n]{0,60}\bitar\b",
    re.I)

# A negation earlier in the same sentence cancels a clearance match: "You
# will not be required to obtain a security clearance" trips the "obtain"
# trigger, and no fixed-width lookbehind can see the "not" three words back.
_NEGATION = re.compile(r"\b(?:no|not|never|without)\b", re.I)


# A citizenship requirement that offers work authorization as an alternative
# is not a gate: "must be a US citizen or eligible to work in US without
# sponsorship" (Carrier), "U.S. Citizen or National of the U.S., an alien
# lawfully admitted for permanent residence, or an alien authorized to work in
# the U.S." (UPS), "US citizen, lawful permanent resident of the US or
# otherwise authorized to work in the US" (Merck). An F-1 student has
# internship work authorization, so these read to him as authorized-to-work
# boilerplate, which profile.md already tells the scorer is not a gate. Before
# 2026-09-25 the citizen term alone matched and these rows were stored skipped
# without the scorer ever reading them (37 of the 532 gated rows whose text
# was refetchable carry this phrasing; UPS alone is 28). The carve-out reads
# one sentence, so a later sentence that excludes F-1 visas (EDF, Shell) is
# left to the scorer's hard-gate rule.
#
# The alternative must follow "or" or "and/or" in the same sentence and name
# WORK authorization specifically; "or possess all required authorizations
# from the U.S. Department of State" is an ITAR export license, not a work
# permit, and still gates. A slash is not a connector: "F-1/H-1B visa holders"
# is a list of excluded visas far more often than an offered alternative.
# The span between "or" and the alternative may not carry a permanent-residence
# or refugee term, a negation, "and", a comma, or a parenthesis, so "U.S.
# Citizen or Permanent Resident (must be able to work ... without visa
# sponsorship)", "citizen or hold a permanent residence visa", and "citizen or
# national, able to work without visa sponsorship" (an added requirement after
# a comma, not an alternative) still gate.
_WORK_AUTH_CONN = re.compile(r"\band/or\b|\bor\b", re.I)
_WORK_AUTH_ALT = re.compile(
    r"(?:(?!\b(?:and|no|not|nor|non|permanent|green|refugees?|asylees?|"
    r"residen(?:ts?|ce)|unable|cannot|excluding|except)\b)[^(),]){0,50}?"
    r"\b(?:authori[sz](?:ed|ation) to (?:legally )?work|"
    r"(?:work|employment)[- ]authori[sz](?:ation|ed)|"
    r"eligib(?:le|ility) to (?:legally )?work|"
    r"(?:able|permitted) to (?:legally )?work|"
    r"(?:(?:the )?legal )?right to work|"
    r"(?:work |student |valid )visa|visa holders?|work permit|f-?1)\b",
    re.I)
# Something after the alternative that rules it out: "work visa holders are
# not eligible", "do not qualify", "cannot be considered". A statement about
# SPONSORSHIP is not one: "visa sponsorship is not available" and "visa
# holders will not be sponsored" leave the authorized-to-work alternative
# standing, and profile.md lists "we do not sponsor visas" as a non-gate. So
# "sponsored" is not a rule-out verb, and a rule-out whose clause is about
# sponsorship ("... (visa sponsorship is not available)") or that is
# followed by "for sponsorship" does not cancel.
_ALT_RULED_OUT = re.compile(
    r"\bineligible\b|\b(?:not|cannot|can't|won't)\s+(?:be\s+)?"
    r"(?:eligible|qualify|accepted|considered|allowed|permitted)\b"
    r"(?!\s+(?:for\s+)?(?:visa\s+|immigration\s+)?sponsor)"
    # A named exclusion: "(excluding F-1 and OPT)", "a valid work visa other
    # than F-1", "F-1 visa holders need not apply", "... are excluded",
    # "we are unable to hire visa holders".
    r"|\bexclud(?:ing|es)\b|\bexcept\b|\bother than\b|\bneed not apply\b"
    r"|\b(?:are|is|will be)\s+excluded\b"
    r"|\b(?:unable to|cannot|can't|do not|does not|will not|won't)\s+"
    r"(?:hire|employ|accept|consider)\b",
    re.I)
# The clause a rule-out belongs to starts after the nearest comma, parenthesis,
# dash, colon, or "and"/"as"/"but"; if that clause names sponsorship, the
# rule-out is about sponsorship, not about the alternative.
_CLAUSE_BREAK = re.compile(r"[,()\-\u2013\u2014:]|\b(?:and|as|but)\b", re.I)
_SPONSOR = re.compile(r"\bsponsor", re.I)


def _ruled_out(window: str, pos: int) -> bool:
    """Does window[pos:] rule out the alternative that ends at pos?"""
    for r in _ALT_RULED_OUT.finditer(window, pos):
        clause = pos
        for b in _CLAUSE_BREAK.finditer(window, pos, r.start()):
            clause = b.end()
        if not _SPONSOR.search(window, clause, r.start()):
            return True
    return False


# Before the connector, a refusal turns the rest of the sentence into a list of
# who is excluded: "(we cannot sponsor or accept work visa holders)". The bare
# word "sponsor" is not a refusal on its own: "citizen (sponsorship available)
# or authorized to work" still offers the alternative.
_ALT_REFUSED = re.compile(
    r"\b(?:no|not|never|without|nor|cannot|can't|unable)\b", re.I)
# The window for that alternative runs to the end of the sentence. "U.S." and
# "U.S.C." are abbreviations, not sentence ends - the UPS phrasing carries
# "of the U.S., an alien ..." in the middle of its alternative list - unless a
# capital letter follows, which starts a new sentence ("... national of the
# U.S. Student visa sponsorship is not available."). A capitalized word that
# reads as the noun "U.S." modifies does not start one: "a U.S. Citizen or
# have U.S. Work Authorization", "U.S. Permanent Resident", "U.S. Export
# control regulations".
_US_NOUN = (r"(?i:citizen|national|person|permanent|lawful|legal|work|"
            r"employment|government|department|export|armed|military|"
            r"territor|green)")
_SENTENCE_END = re.compile(
    r"[\n;]|(?<![uU]\.[sS])(?<![uU]\.[sS]\.[cC])\.(?=\s|$)"
    rf"|(?<=[uU]\.[sS])\.(?=\s+(?!{_US_NOUN})[A-Z])")
# A rule-out after the alternative is read up to the end of the sentence
# proper, past a semicolon: "citizen or visa holder; however, visa holders are
# not eligible for this role" rules its own alternative out. The alternative
# itself must still sit before the semicolon.
_PERIOD_END = re.compile(
    r"\n|(?<![uU]\.[sS])(?<![uU]\.[sS]\.[cC])\.(?=\s|$)"
    rf"|(?<=[uU]\.[sS])\.(?=\s+(?!{_US_NOUN})[A-Z])")
_CIT_TERM = re.compile(r"(?:u\.?s\.?|united states) (?:citizen(?:ship)?|persons?)\b", re.I)
# An ITAR/export requirement is a US-person gate however it is phrased; its
# own regex branch can lose the match position to the citizenship branch, so
# the carve-out never applies when ITAR, EAR, or export appears anywhere in
# the same sentence or the one right after it ("... or authorized to work in
# the U.S. This role is subject to ITAR."). An earlier sentence does not
# count: "We comply with export regulations." ahead of the requirement is
# boilerplate about the company, not a condition on the alternative.
_EXPORT = re.compile(r"\b(?:itar|ear|export)\b", re.I)
# A clearance named later in the same sentence ("... or be authorized to work
# and have a DoD Secret security clearance") keeps the sentence a clearance
# gate even when no requirement word sits within the clearance branch's reach.
# A softener or negation ("no security clearance needed", "clearance is a
# plus") leaves the carve-out alone - unless a requirement follows the
# clearance anyway: "candidates without a Secret clearance must obtain one".
_CLEARANCE_TERM = re.compile(
    r"\b(?:security clearance|ts/?sci|top secret|secret(?:-level)? clearance|"
    r"clearance holders?|polygraph)\b"
    r"(?![^.\n]{0,40}(?:a plus|preferred|not required|not needed))",
    re.I)
_CLEARANCE_DEMAND = re.compile(
    r"\b(?:must|required to|need to|will need|obtain|acquire)\b", re.I)


# How far back _sentence_start looks. The caller only needs the head of the
# current sentence (to spot export wording), and an unbounded scan from the
# start of the line made every citizenship match cost O(line length): a
# 213KB single-line description took 15 seconds.
_SENTENCE_LOOKBACK = 400


def _sentence_start(text: str, pos: int) -> int:
    """Index where the sentence holding text[pos] begins (bounded look-back)."""
    start = max(text.rfind("\n", max(0, pos - _SENTENCE_LOOKBACK), pos) + 1,
                pos - _SENTENCE_LOOKBACK, 0)
    for e in _SENTENCE_END.finditer(text, start, pos):
        start = e.end()
    return start


def _work_auth_alternative(text: str, m: re.Match) -> bool:
    """Does this citizenship match's sentence offer work authorization instead?"""
    terms = list(_CIT_TERM.finditer(text, m.start(), m.end()))
    start = terms[-1].end() if terms else m.end()
    stop = min(len(text), start + 250)
    end = _SENTENCE_END.search(text, start, stop)
    window = text[start:end.start() if end else stop]
    # The rest of the sentence proper (past a ';') for rule-outs, and the
    # next sentence for export wording.
    full_end = _PERIOD_END.search(text, start, stop)
    ruled_window = text[start:full_end.start() if full_end else stop]
    after = ""
    if end:
        nxt_stop = min(len(text), end.end() + 250)
        nxt = _SENTENCE_END.search(text, end.end(), nxt_stop)
        after = text[end.end():nxt.start() if nxt else nxt_stop]
    if (_EXPORT.search(window) or _EXPORT.search(after)
            or _EXPORT.search(text[_sentence_start(text, m.start()):start])):
        return False
    for clr in _CLEARANCE_TERM.finditer(window):
        if (not _NEGATION.search(window, 0, clr.start())
                or _CLEARANCE_DEMAND.search(window, clr.end())):
            return False
    for conn in _WORK_AUTH_CONN.finditer(window):
        if _ALT_REFUSED.search(window, 0, conn.start()):
            return False
        alt = _WORK_AUTH_ALT.match(window, conn.end())
        if alt and not _ruled_out(ruled_window, alt.end()):
            return True
    return False


def clearance_gated(text: str | None) -> bool:
    """True when the description states a clearance or citizenship requirement."""
    text = text or ""
    pos = 0
    while (m := CLEARANCE_DESC.search(text, pos)) is not None:
        start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        if _NEGATION.search(text[start:m.start()]):
            pos = m.end()
            continue
        if m.group("cit") and _work_auth_alternative(text, m):
            # Resume just past the match START, not its end: the citizenship
            # branch reaches back to its trigger word, so "must obtain U.S.
            # citizenship or be authorized to work and have a DoD Secret
            # security clearance" swallows the clearance branch's "obtain"
            # inside the skipped span, and resuming at the end would never
            # see that clearance requirement.
            pos = m.start() + 1
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Cycle matching
#
# A posting names a term ("Fall"), a year ("2027"), both, or neither. Each case
# needs different handling, and "neither" is common enough that dropping it
# would cost real matches - plenty of boards just say "Software Engineer Intern"
# and leave the dates to the description.
# ---------------------------------------------------------------------------

TERM_PAT = re.compile(r"\b(fall|autumn|spring|summer|winter)\b", re.I)
YEAR_PAT = re.compile(r"\b20(2[4-9]|3\d)\b")

TERM_ALIASES = {"autumn": "fall"}

# A year next to a graduation phrase is the candidate's class year, not the
# term being hired for: "Software Engineer Intern - Class of 2028" is a
# summer 2027 internship for a May 2028 graduate. Read as a cycle year it
# fails every target cycle and drops before the scorer sees it (found in the
# 2026-09-15 sweep; the user graduates in 2028). These spans are erased from
# the title before the cycle question is asked; a month or term in front of
# the year ("May 2028 Graduates", "Spring 2028 Graduates") goes with it so it
# cannot be mistaken for the hiring term either.
_MONTH_OR_TERM = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
                  r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
                  r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
                  r"spring|summer|fall|autumn|winter)")
_YEAR_SPAN = r"20\d\d(?:\s*[/-]\s*(?:20)?\d\d)?"
GRAD_PAT = re.compile(
    r"\b(?:class of|graduating(?: in)?|graduation(?: date)?(?: of)?)\s+"
    + r"(?:" + _MONTH_OR_TERM + r"\s+)?" + _YEAR_SPAN
    + r"|\b(?:" + _MONTH_OR_TERM + r"\s+)?" + _YEAR_SPAN
    + r"(?:\s+(?:and|or|&)\s+(?:" + _MONTH_OR_TERM + r"\s+)?" + _YEAR_SPAN + r")?"
    + r"[^,;()]{0,25}?\b(?:grads?|graduates?)\b",
    re.I,
)


# Class-year gate. A title that names graduation or class years ("Class of
# 2029", "2027 Graduates", "'28 grads", "Grad Year 2029") states who may
# apply, and when no named year or range includes the candidate's graduation
# year (filters.graduation_year) the posting is closed to him. This used to be
# left to the scorer's prompt, which missed it in mixed batches of ordinary
# "Intern - 2027" titles (claude-sonnet-5 scored "Class of 2029" at 68-80 in
# 7 of 18 runs, 2026-09-25) and in 1 run of 18 gated those ordinary titles
# instead. The title is plain text, so the rule is code.
# Only years attached to a graduation phrase count: a bare year or a season
# year ("Intern - 2027", "Summer 2027") is a work term and never triggers it.
# Ranges include every year between their ends ("Dec 2027 - May 2028",
# "'27-'28", "2027-2029"); "or later" / "+" open the top end and "or earlier"
# the bottom. Anything the pattern cannot read fails open to the scorer.
# After a year the grad word must be plural: singular "Graduate" there is an
# adjective for the student level ("2027 Graduate Software Internships" and
# "Centene 2027 Graduate Summer Intern" are 2027 work terms for graduate
# students, found in the 2026-09-25 sweep of stored titles).
_APOS = "['\u2018\u2019]"
_DASH = "\\-\u2010\u2011\u2012\u2013\u2014"
_CY_GAP = rf"[\s{_DASH}]+"
_CY_YEAR = rf"(?:{_MONTH_OR_TERM}\.?\s+)?(?:20\d\d|{_APOS}\d\d)(?!\d)"
_CY_YEAR2 = rf"(?:{_MONTH_OR_TERM}\.?\s+)?(?:20\d\d|{_APOS}?\d\d)(?!\d)"
# A comma lists years ("Class of 2027, 2028, or 2029"); each is its own span.
# After a phrase ("Class of", "Graduating"), a month or term after a comma
# may be another graduation date ("Graduating Dec 2027, May 2028") or a work
# term ("Class of 2029, Summer 2027"); _cy_cut tells them apart after the
# pattern has matched. Before a "Grads" word a comma list of months is one
# graduation window ("May 2028, Aug 2028 Grads"), but a season-prefixed
# item before a comma is a work term ("Summer 2028, May 2029 Grads");
# _class_year_matches trims it off the front of the match. A pattern
# lookahead cannot do that trim: the unanchored "Grads" alternative would
# just start its match at the bare year after the season word.
_CY_SEP = (rf"\s*(?:,(?:\s*(?:or|and)\b)?|[{_DASH}/&]|\bto\b|\bthrough\b|\bthru\b"
           r"|\band\b|\bor\b)\s*")
_CY_OPEN = (r"(?:\s*\+|\s+(?:or|and)\s+(?:later|after|beyond|above|"
            r"earlier|before|prior)\b|\s+onwards?\b)?")
_CY_RANGE = rf"{_CY_YEAR}(?:{_CY_SEP}{_CY_YEAR2})*{_CY_OPEN}"
# The "Grads" alternative has no phrase to anchor it, so it is tried at
# every position; an unbounded list there rescans the rest of a long list
# from each item, which is quadratic. No real graduation list runs past a
# handful of dates, so it reads at most eight.
_CY_RANGE_B = rf"{_CY_YEAR}(?:{_CY_SEP}{_CY_YEAR2}){{0,7}}{_CY_OPEN}"
CLASS_YEAR_PAT = re.compile(
    rf"\b(?:class{_CY_GAP}of"
    r"|graduating(?:\s+(?:in|by|between|from))?"
    r"|graduation(?:\s+(?:date|year|class))?(?:\s+(?:of|in|between))?"
    rf"|grad(?:uation)?{_CY_GAP}(?:year|date|class)|grads?\s+of)"
    rf"(?:\s*:\s*|{_CY_GAP})(?P<a>{_CY_RANGE})"
    rf"|(?P<b>{_CY_RANGE_B})\s+(?:(?:undergrad(?:uate)?|university|college)\s+)?"
    r"(?:grads|graduates)\b",
    re.I,
)
_CY_NUM = re.compile(rf"(?:20|{_APOS})?(\d\d)(?!\d)")
_CY_ITEM = re.compile(rf"(?:\b(?P<mt>{_MONTH_OR_TERM})\.?\s+)?(?:20|{_APOS})?(?P<yy>\d\d)(?!\d)", re.I)
_CY_RANGE_SEP = re.compile(rf"[{_DASH}]|\bto\b|\bthrough\b|\bthru\b", re.I)
_CY_SEASON = re.compile(r"spring|summer|fall|autumn|winter", re.I)


def _class_year_spans(text: str, between_kw: bool,
                      by_kw: bool = False) -> list[tuple[int, int]]:
    """The (low, high) graduation-year spans one CLASS_YEAR_PAT match names.
    `between_kw`: the phrase said "between", so "X and Y" is a range.
    `by_kw`: the phrase said "graduating by", which means no later than, so
    the bottom end is open."""
    nums = list(_CY_NUM.finditer(text))
    spans: list[list[int]] = []
    stopped = False
    for k, m in enumerate(nums):
        y = 2000 + int(m.group(1))
        if k == 0:
            spans.append([y, y])
            continue
        gap = text[nums[k - 1].end():m.start()]
        if _CY_RANGE_SEP.search(gap) or (between_kw and re.search(r"\band\b", gap, re.I)):
            if y < spans[-1][1]:
                # "Class of 2029 - Summer 2027": a descending "range" is a
                # work term running into the class year; stop reading there.
                stopped = True
                break
            spans[-1][1] = y
        else:
            spans.append([y, y])
    if nums and not stopped:
        tail = text[nums[-1].end():]
        if re.search(r"\+|later|after|beyond|above|onward", tail, re.I):
            spans[-1][1] = 9999
        elif re.search(r"earlier|before|prior", tail, re.I):
            spans[-1][0] = 0
    if by_kw and spans:
        spans = [[0, max(hi for _, hi in spans)]]
    return [tuple(s) for s in spans]


def _cy_cut(text: str) -> int:
    """Where a phrase-first class-year list ("Class of ...", "Graduating
    ...") ends. A real graduation list never goes down in year, so an item
    whose year is below the previous item's is a work term, and the list
    ends before it when the item is month- or term-prefixed ("Class of 2029,
    Summer 2027", "Class of 2029 / Summer 2028", "Graduating May 2029 and
    Summer 2028") or follows a dash, "to" or "through" ("Class of 2029 -
    2027"). A bare year after a comma, "or" or "/" is only an odd list order
    and stays in it. An item that does not go down continues the list
    ("Graduating Dec 2027, May 2028")."""
    items = list(_CY_ITEM.finditer(text))
    for k in range(1, len(items)):
        prev, cur = items[k - 1], items[k]
        gap = text[prev.end():cur.start()]
        if int(cur.group("yy")) < int(prev.group("yy")) and (
                cur.group("mt") or _CY_RANGE_SEP.search(gap)):
            return prev.end()
    return len(text)


def _cy_trim_season(text: str, offset: int) -> int:
    """Where a "... Grads" list really starts. A leading season-prefixed
    item followed by a comma is the work term, not a graduation date
    ("Summer 2028, May 2029 Grads", "Fall 2026, May 2028 Grads"), so it is
    left in the title for the cycle check. Calendar months continue the list
    ("May 2028, Aug 2028 Grads")."""
    items = list(_CY_ITEM.finditer(text))
    k = 0
    while (k + 1 < len(items) and items[k].group("mt")
           and _CY_SEASON.fullmatch(items[k].group("mt"))
           and "," in text[items[k].end():items[k + 1].start()]):
        k += 1
    return offset + (items[k].start() if k else 0)


def _class_year_matches(title: str):
    """(start, end, spans) for each class-year phrase in the title. A match
    cut short by _cy_cut ends at the cut, and the search resumes there so
    the work term after it stays in the title for the cycle check; a "Grads"
    match trimmed by _cy_trim_season starts after the work term instead."""
    pos = 0
    while True:
        m = CLASS_YEAR_PAT.search(title, pos)
        if not m:
            return
        if m.group("a"):
            a = m.group("a")[:_cy_cut(m.group("a"))]
            start, end = m.start(), m.start("a") + len(a)
            head = m.group(0)[:m.start("a") - m.start()]
            spans = _class_year_spans(a, re.search(r"\bbetween\b", head, re.I) is not None,
                                      re.search(r"\bby\b", head, re.I) is not None)
        else:
            start, end = _cy_trim_season(m.group("b"), m.start("b")), m.end()
            spans = _class_year_spans(title[start:m.end("b")], False)
        yield start, end, spans
        pos = max(end, m.start() + 1)


def _erase_class_years(title: str) -> str:
    out, pos = [], 0
    for start, end, _ in _class_year_matches(title):
        out += [title[pos:start], " "]
        pos = end
    return "".join(out) + title[pos:]


def class_year_ok(title: str, grad_year) -> tuple[bool, str]:
    """Does a class year named in the title admit the candidate's graduation
    year? A title that names none passes; so does one where any named year
    or range includes it."""
    if not grad_year:
        return True, ""
    spans = [s for _, _, ss in _class_year_matches(title or "") for s in ss]
    if spans and not any(lo <= int(grad_year) <= hi for lo, hi in spans):
        return False, "class-year gate (title)"
    return True, ""


def parse_cycles(raw: list) -> set[tuple[str, int]]:
    """config `target_cycles` entries -> {(term, year)}.

    Accepts the mapping form ({term: fall, year: 2026}) or the shorthand string
    form ("fall 2026").
    """
    out = set()
    for entry in raw or []:
        if isinstance(entry, dict):
            term, year = entry.get("term", ""), entry.get("year")
        else:
            parts = str(entry).split()
            if len(parts) != 2:
                continue
            term, year = parts
        term = TERM_ALIASES.get(str(term).lower(), str(term).lower())
        try:
            out.add((term, int(year)))
        except (TypeError, ValueError):
            continue
    return out


def _title_cycle(title: str) -> tuple[set[str], set[int]]:
    """The terms and years a title names, class-year phrases erased.

    CLASS_YEAR_PAT also reads phrasings GRAD_PAT does not ("Grad Year 2028",
    "Class-of-2028", "graduating between 2028 and 2029"), and it reads a
    month-prefixed range whole where GRAD_PAT stops at its first half:
    erasing GRAD_PAT first turned "Graduating Dec 2027 - May 2028" into
    " - May 2028", a 2028 work term. So CLASS_YEAR_PAT erases first.
    """
    title = GRAD_PAT.sub(" ", _erase_class_years(title))
    terms = {TERM_ALIASES.get(t.lower(), t.lower()) for t in TERM_PAT.findall(title)}
    years = {int("20" + y) for y in YEAR_PAT.findall(title)}
    return terms, years


def cycle_ok(title: str, cycles: set[tuple[str, int]]) -> tuple[bool, str]:
    """Does the cycle this title advertises overlap what we're recruiting for?"""
    if not cycles:
        return True, ""

    terms, years = _title_cycle(title)
    if terms and years:
        # Both named. Any combination present counts, since titles like
        # "Summer/Fall 2027 Intern" advertise several cycles at once.
        if not any((t, y) in cycles for t in terms for y in years):
            return False, f"wrong cycle: {sorted(terms)} {sorted(years)}"
    elif terms:
        if not (terms & {t for t, _ in cycles}):
            return False, f"wrong term: {sorted(terms)}"
    elif years:
        if not (years & {y for _, y in cycles}):
            return False, f"wrong cycle year: {sorted(years)}"
    # Neither named: unknown cycle, let the LLM read the description.
    return True, ""


def terms_ok(terms: list, cycles: set[tuple[str, int]]) -> tuple[bool, str]:
    """Check a listing's structured `terms` field against the target cycles.

    Aggregated feeds state the cycle in a dedicated field rather than the
    title, which is both more reliable and the only signal available - most of
    those titles are a bare "Software Engineer Intern". Unparseable values like
    "N/A" mean unknown, not wrong, so they pass through to the LLM.
    """
    if not cycles or not terms:
        return True, ""
    parsed = parse_cycles(terms)
    if not parsed:
        return True, ""
    if parsed & cycles:
        return True, ""
    return False, f"wrong cycle (terms): {sorted(t + ' ' + str(y) for t, y in parsed)[:2]}"


# Last day a season tag can still describe a new posting, as (month-day,
# year offset). A `terms` entry past that day cannot describe the row:
# Simplify often tags a new posting with last season's term, and a September
# 2026 posting tagged "Summer 2026" can only mean next summer (165 such
# active rows on 2026-09-25, among them PrizePicks' and Rippling's "...
# Intern - Summer 2027", dropped as "wrong cycle (terms)"). The cutoff is the
# start of each season's last month, not its end: nobody posts a summer
# internship in August of that same summer, and waiting for Aug 31 split one
# Fervo batch at midnight (its Aug 31 posting dropped, its Sept siblings
# passed) and dropped 40 July/August "Summer 2026" rows absent from the DB.
# Winter takes its latest reading - Simplify's "Winter 2026" is Dec 2026 -
# Feb 2027, a Canadian co-op's is Jan - Apr 2026 - so its last month is the
# April after its year.
_SEASON_LIVE_UNTIL = {"spring": ("04-30", 0), "summer": ("07-31", 0),
                      "fall": ("11-30", 0), "winter": ("03-31", 1)}
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _row_date(job: dict) -> str | None:
    """The row's posted date (YYYY-MM-DD), or updated_at when it has none.

    Only posted_at dates a tag: Simplify bumps date_updated for 180-300 rows
    a day on metadata touches (297 on 2026-09-03), so it says nothing about
    which season a posting is for. A string that is not a real calendar date
    is no date at all (sources._iso passes unparseable values through).
    """
    for field in ("posted_at", "updated_at"):
        d = job.get(field)
        if isinstance(d, str) and _ISO_DATE.match(d):
            try:
                return _dt.date.fromisoformat(d[:10]).isoformat()
            except ValueError:
                continue
    return None


def live_terms(job: dict) -> list:
    """The row's `terms` minus entries whose season is too late for its date.

    No usable date means no evidence either way, so every entry stays.
    Parseable entries that are not a season ("Co-op 2026") stay too - there
    is no season to judge them by. Unparseable entries ("N/A") stay only
    while a parseable entry is left or there never was one: once every
    parseable entry is stale, "N/A" would make terms_ok pass the row as
    unknown without the title ever being read, so the result is empty and
    the caller falls back to the title.
    """
    terms = job.get("terms") or []
    as_of = _row_date(job)
    if not as_of:
        return list(terms)
    out, any_parsed, any_live = [], False, False
    for t in terms:
        parsed = parse_cycles([t])
        if not parsed:
            out.append(t)
            continue
        any_parsed = True
        if (any(term not in _SEASON_LIVE_UNTIL for term, _ in parsed)
                or any(f"{y + _SEASON_LIVE_UNTIL[term][1]}-{_SEASON_LIVE_UNTIL[term][0]}"
                       >= as_of for term, y in parsed)):
            out.append(t)
            any_live = True
    return out if any_live or not any_parsed else []


def _title_beats_tag(title: str, tags: list, cycles: set[tuple[str, int]]) -> bool:
    """Does the title name a target cycle that CONTRADICTS the live tag?

    The title wins only on a real contradiction: it names a target (term,
    year) pair ("Software Engineering Intern - Summer 2027"), or it names
    only years and none of them is a year the tag names ("... - 2027 Start"
    against "Summer 2026"). A bare year the tag shares ("... - 2026 - BS/MS"
    against a live "Fall 2026") agrees with the tag, and the tag is the
    more specific of the two - winter 2026 being a target cycle must not
    turn every "2026" title into an override.
    """
    terms, years = _title_cycle(title)
    if not years or not cycle_ok(title, cycles)[0]:
        return False
    return bool(terms) or not (years & {y for _, y in parse_cycles(tags)})


def feed_cycle(job: dict, cycles: set[tuple[str, int]]) -> str | None:
    """Cycle label for a feed row that bypasses the scorer, from its tag.

    Only a tag that decided the cycle question counts: stale entries are
    left out, and a tag the title overrode gives no label at all, so
    publish.season_of falls back to the title instead of a cycle the title
    just contradicted.
    """
    terms = live_terms(job)
    if not terms or not terms_ok(terms, cycles)[0]:
        return None
    return ", ".join(terms).lower()


def tag_misfiled(job: dict, cycles: set[tuple[str, int]]) -> bool:
    """Is this row's `terms` tag one the cycle gate had to set aside?

    For a row that passed `passes`, a tag terms_ok rejects means the gate
    passed it anyway because the tag was stale or the title contradicted
    it. Such a listing sat on the public list under the wrong season, so
    nobody reading the list by season has seen it; cli.split_stale uses
    this to keep it out of the stale-on-arrival suppression.
    """
    terms = job.get("terms") or []
    return bool(terms) and not terms_ok(terms, cycles)[0]


# The closing \b sits OUTSIDE the whole group. It once sat inside, glued to
# the city-comma-state branch only, which left every other alternative
# open-ended on the right: bare "la" (Los Angeles) prefix-matched "LAtin
# American Students - Panama", "us" matched "USer", and non-US postings
# sailed through the us_only gate.
# Every US state spelled out. US_HINT accepts them bare; the state-code
# excuse below reads one in the fields before a three-part "..., ST" tail.
US_STATE_NAMES = (
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|"
    r"kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    r"new mexico|north carolina|north dakota|ohio|oklahoma|oregon|"
    r"pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|"
    r"utah|vermont|virginia|west virginia|wisconsin|wyoming|"
    r"district of columbia")

US_HINT = re.compile(
    # The dotted forms end on a letter ("u.s", "u.s.a") on purpose: a match
    # ending on the final period would put \b between "." and the following
    # space - two non-word chars, no boundary - and never match. Ending on
    # the letter leaves the trailing period unconsumed and the boundary real.
    r"\b(usa?|u\.s(?:\.a)?|united states|remote|hybrid|"
    r"new york|nyc|san francisco|sf|bay area|palo alto|mountain view|seattle|"
    r"boston|cambridge|austin|chicago|denver|los angeles|la|san jose|"
    r"sunnyvale|redmond|bellevue|atlanta|dallas|houston|miami|"
    r"pittsburgh|madison|chicago|ann arbor|washington|dc|"
    r"berkeley|hawthorne|alpharetta|evansville|"
    # Frequent "City, ST" offices whose state code is also a country code
    # (DE, IN, NC, PA, MN, ...): score.us_located reads a city from this
    # list as the US evidence that tells "Wilmington, DE" from "Göttingen,
    # DE", and "Chicago, Illinois, IL" from a "City, Region, IL" in Israel.
    r"wilmington|indianapolis|santa clara|charlotte|phoenix|minneapolis|"
    r"raleigh|philadelphia|san diego|nashville|baltimore|"
    # State-first spellings ("AL, Mobile") put the abbreviation where the
    # city-comma-state branch below can never see it. Lookahead keeps the
    # abbreviation's own \b intact. A lowercase "in," inside a title could
    # false-match, but that errs toward keeping - the gate's stated bias.
    r"(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|"
    r"mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|"
    r"va|wa|wv|wi|wy|dc|pr)(?=,\s)|"
    # Boards spell the state out in several shapes - "Irvine, California",
    # a bare "Massachusetts", Workday's "Indiana - West Lafayette" - so the
    # full names stand alone. Only the two-letter abbreviations stay gated
    # behind a preceding "city, ": bare they collide with ordinary words
    # ("in", "or", "me") and foreign spellings.
    + US_STATE_NAMES + "|"
    # The city part takes any letter, not just ASCII: "Española, NM" has no
    # word boundary inside "ñ" for \b to find and no plain word before the
    # comma, so an [a-z] city could never reach its state.
    r"[^\W\d_][^\W\d_\s]*(?:\s+[^\W\d_][^\W\d_\s]*)*,\s*(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|"
    r"md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|"
    r"tx|ut|vt|va|wa|wv|wi|wy|dc|pr))\b",
    re.I,
)

# A two-letter prefix before a dash is ambiguous by construction: Compeer's
# Greenhouse "MN-Lakeville" is Minnesota, but Workday's "IN-Pune" is India,
# "DE-Celle" is Germany and "CA - Esterhazy, SK" is Canada. It is never read
# as US evidence; NON_US catches the ones that also name a foreign city or
# province, and the rest pass to the scorer as ambiguous. A US state code
# does keep a bare foreign CITY name from being decisive ("OH-Dublin" is
# Ohio) - see STATE_COUNTRY_PLACES below NON_US.
#
# ISO_TAIL is SmartRecruiters' "City, Region, cc" shape, where the last
# field is an ISO country code that the city-comma-state branch of US_HINT
# otherwise mistakes for a state ("Reutlingen, BW, de" read as Delaware).
# Checked first, it settles the question either way: "us" is the United
# States, anything else is not.
#
# The two leading fields must be single names: a list separator inside them
# means the location is several offices, not one City-Region-Country. The
# permissive `[^,;]+` read Greenhouse's "San Francisco, CA • New York, NY"
# as city "San Francisco", region "CA • New York", country "NY" - not "us",
# dropped - and Figma's summer 2027 SWE, data science and product design
# internships never reached the store (2026-09-14; the bug shipped with this
# regex on 2026-09-08). Multi-office strings fall through to the US_HINT /
# NON_US pair below, which reads every office they name.
ISO_TAIL = re.compile(
    r"^(?!.*\s(?:or|and)\s)[^,;•|/&]+,\s*[^,;•|/&]+,\s*([a-z]{2})\s*$", re.I)

# The same three-field shape with a US STATE at the end is how Simplify and
# Workday write a neighbourhood or campus: "Hunt Valley, Cockeysville, MD",
# "Long Island City, Queens, NY", "Main Campus, Columbus, OH". ISO_TAIL read
# the state as a country code and dropped them (Textron's four summer 2027
# SWE interns, 2026-09-15). SmartRecruiters writes its ISO codes lowercase
# without exception (every code in the DB, "us" included), Simplify and
# Workday write states uppercase, so case tells them apart: an uppercase
# US-state tail is a state, and only a foreign marker in the leading fields
# can still drop it ("Toronto, ON, CA" is Canada because of Toronto).
US_STATES = frozenset("""al ak az ar ca co ct de fl ga hi id il in ia ks ky la
    me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx
    ut vt va wa wv wi wy dc pr""".split())

# What a non-US location looks like. The us_only gate drops a posting only
# when its location names one of these; a location that names NEITHER a US
# hint nor one of these is ambiguous and passes to the scorer, whose `us`
# flag knows every city. Before 2026-09-08 the gate dropped anything without
# a US hint, which killed US postings whose board labels the office instead
# of the city: Workday's "Wilmington, 125 South West Street" (Barclays'
# 2027 Technology Developer internship) and "Building 400-Whippany Campus".
# Measured against the day's drops on the Simplify feed and the Bosch and
# Micron boards, this list re-drops every one of them; only office labels
# come through. Countries and regions, the foreign cities that boards use
# bare, Canadian provinces after a city, and SmartRecruiters' trailing ISO
# code (", de", ", vn" - ", us" is the one exception).
#
# The words live in the lists below and every regex that reads places is
# built from them (NON_US, FOREIGN_COUNTRY, STATE_COUNTRY_PLACES), so a new
# place is added once. A country whose ISO code is also a US state code
# keeps its places in COUNTRY_PLACES: those are what still reads as foreign
# after that code ("IN-Pune" drops, "IN-Indianapolis" does not).
#
# ISO code -> (country and region names, cities and site labels). Names are
# decisive after ANY state code; cities only after their own country's code.
COUNTRY_PLACES = {
    # "ontario" is a city list entry, not a region name: Ontario is also a
    # city in California and Oregon ("OR-Ontario" is Oregon). London,
    # Kanata, Guelph and Kitchener are Ontario cities a "CA-" prefix names
    # ("CA - London" is London, Ontario); London stays a UK city for NON_US.
    "ca": (["canada", "british columbia", "qu[eé]bec", "alberta", "manitoba",
            "saskatchewan", "nova scotia", "new brunswick", "newfoundland"],
           ["ontario", "toronto", "vancouver", "montr[eé]al", "waterloo", "ottawa",
            "calgary", "edmonton", "mississauga", "halifax", "oakville", "winnipeg",
            r"st\.? john's", "laval", "burnaby", "richmond hill", "markham",
            "london", "kanata", "guelph", "kitchener", "brampton", "saskatoon",
            "regina", "kelowna", "sherbrooke", "gatineau", "pointe-claire",
            "saint-laurent", "moncton", "fredericton", "newmarket"]),
    "in": (["india"],
           ["bangalore", "bengaluru", "hyderabad", "pune", "chennai", "mumbai",
            "delhi", "gurgaon", "gurugram", "noida", "kolkata"]),
    "de": (["germany", "deutschland"],
           ["berlin", "munich", "m[uü]nchen", "stuttgart", "frankfurt", "hamburg",
            "karlsruhe", "jena", "schwalbach", "immenstaad", "oberkochen",
            "g[oö]rwihl"]),
    # score reads a two-field "City, CC" with one of these codes as the US
    # state unless NON_US names the city, so a country's tech-hub cities
    # belong here even when no board has written them yet.
    # Spelling variants matter here: "Tel-Aviv", "Petach Tikva" and
    # "Ra'anana" are how boards actually write them (2026-09-25 review).
    "il": (["israel"], ["tel aviv", "tel-aviv", "haifa", "herzliya", "petah tikva",
                        "petah-tikva", "petach tikva", "jerusalem", "yokneam",
                        "raanana", "ra'anana", "rehovot", "netanya", "beersheba",
                        "ramat gan", "kfar saba", "hod hasharon", "kiryat gat",
                        "migdal haemek"]),
    "co": (["colombia"], ["bogot[aá]", "medell[ií]n", "barranquilla", "cali"]),
    "ar": (["argentina"], ["buenos aires", "c[oó]rdoba", "rosario"]),
    "id": (["indonesia"], ["jakarta", "bandung"]),
    "ma": (["morocco"], ["casablanca", "rabat", "tangier"]),
    # Capitals of the other countries whose ISO code is a US state code.
    "az": (["azerbaijan"], ["baku"]),
    "al": (["albania"], ["tirana"]),
    "md": (["moldova"], ["chisinau"]),
    "mt": (["malta"], ["valletta"]),
    "tn": (["tunisia"], ["tunis"]),
    # "Panama City" is in Florida.
    "pa": (["panama(?! city)"], []),
}
# Country and region names of every other country. "New Mexico" is a state.
OTHER_COUNTRIES = [
    "united kingdom", "uk", r"u\.k", "england", "scotland", "wales", "ireland",
    "france", "netherlands", "belgium", "switzerland", "austria", "sweden",
    "denmark", "norway", "finland", "poland", "czech(?:ia| republic)?", "hungary",
    "romania", "portugal", "spain", "italy", "greece", "turkey", "t[uü]rkiye",
    "uae", "saudi arabia", "qatar", "monaco", "china", "hong kong", "taiwan",
    "japan", "korea", "singapore", "malaysia", "thailand", "vietnam", "viet nam",
    "philippines", "australia", "new zealand", "brazil", "brasil",
    "(?<!new )m[eé]xico", "chile", "peru", "costa rica", "guatemala", "uruguay",
    "south africa", "nigeria", "kenya", "egypt", "europe", "emea", "apac",
    "latam", "asia", "luxembourg", "sri lanka", "bulgaria", "russia", "croatia",
    "pakistan", "bangladesh",
]
# Foreign cities of every other country, bare.
OTHER_CITIES = [
    "manchester", "edinburgh", "canary wharf", "dublin", "paris", "amsterdam",
    "brussels", "zurich", "z[uü]rich", "geneva", "vienna", "stockholm",
    "copenhagen", "oslo", "helsinki", "warsaw", "krak[oó]w", "prague", "budapest",
    "bucharest", "lisbon", "madrid", "barcelona", "milan", "rome", "athens",
    "istanbul", "dubai", "abu dhabi", "riyadh", "doha", "shanghai", "beijing",
    "shenzhen", "hangzhou", "taipei", "hsinchu", "tokyo", "osaka", "seoul",
    "kuala lumpur", "bangkok", "ho chi minh", "hanoi", "manila", "sydney",
    "melbourne", "brisbane", "perth", "auckland", "s[aã]o paulo", "santiago",
    "lima", "johannesburg", "cape town", "lagos", "nairobi", "cairo", "sofia",
    "moscow", "tbilisi", "batumi",
    # Workday office labels that reached the scorer in the 2026-09-09 flush
    # and came back us=false: each is a plant or campus that a big tenant
    # posts to every week (Zeiss's Oberkochen, NXP's Gratkorn, Richemont's
    # Meyrin, Philips' Eindhoven, Valeo's Martos, GlobalFoundries' Woodlands).
    "gratkorn", "meyrin", "eindhoven", "quer[eé]taro", "martos", "milano",
    "schaffhausen", "scoppito", "tianjin", "quezon city", "sorocaba", "rio bravo",
    "bruxelles", "suzhou", "[lł][oó]d[zź]", "huixquilucan", "toluca", "osijek",
    "cariacica", "novomoskovsk", "guadalajara", "warszawa", "barueri", "roma",
    "gen[eè]ve", "duba[iï]", "basel", "san luis potosi", "hengelo", "taguig",
    "belo horizonte", "nijmegen", "zaventem", "delft", "rotterdam", "woodlands",
    "villars sur glane", "wuxi", "anagni", "ketrzyn", "zamudio",
]


def _words(alts: list[str]) -> str:
    return r"\b(?:" + "|".join(alts) + r")\b"


_ALL_COUNTRIES = [n for names, _ in COUNTRY_PLACES.values() for n in names] + OTHER_COUNTRIES
_ALL_CITIES = [c for _, cities in COUNTRY_PLACES.values() for c in cities] + OTHER_CITIES

# The place names alone, without the location-shape anchors below. A title
# is not a location: its "Co-op-" prefix matches the two-letter dash anchor,
# so score reads a title's foreign place with this and never with NON_US.
NON_US_PLACES = re.compile(_words(_ALL_COUNTRIES + _ALL_CITIES), re.I)

NON_US = re.compile(
    _words(_ALL_COUNTRIES + _ALL_CITIES)
    # Workday's "CCC - City" site prefix with an ISO-3 country code.
    + r"|^(?:sgp|gbr|deu|fra|ind|can|mex|chn|jpn|kor|aus|nld|che|ita|esp|pol|"
    r"irl|swe|bra|arg|isr|are|twn|hkg|mys|phl|vnm|tha|idn|bel|aut|cze|hun|"
    r"rou|prt|dnk|nor|fin|zaf|egy|tur|sau|nzl|col|chl|per|cri|lka|lux|pak|"
    r"bgd|nga|ken|mar|grc|rus|hrv|bgr|svk|svn|srb|ukr|ltu|lva|est)\s?-\s"
    r"|,\s*(?:on|bc|qc|ab|mb|sk|ns|nb|nl|pe)(?:,|\s*$)"
    r"|^(?!us-)[a-z]{2}-[a-z]{2,3}-"
    # SmartRecruiters' two-part "City, cc" ("Aveiro, pt", "Ha Noi, vn").
    # Only reached when US_HINT found no state, so a real "City, PA" never
    # lands here; ISO_TAIL handles the three-part form above it.
    # Lowercase only: SmartRecruiters writes ISO codes lowercase, boards
    # write US states uppercase, and ", NM" after a city the hint list
    # cannot parse must not read as a country.
    r"|,\s*(?!us\b)(?-i:[a-z]{2})\s*$",
    re.I,
)

# NON_US names foreign cities, and many of them are also US cities: Cardinal
# Health's Workday writes its Dublin, Ohio headquarters "OH-Dublin-Cardinal
# Place", CVS writes "FL - Melbourne" and "IL - Waterloo", and Simplify writes
# "Tysons, Vienna, VA". When the location also carries a US state code - as a
# "ST-City" prefix or an uppercase three-part "..., ST" tail - a city match
# alone is ambiguous and passes to the scorer. From 2026-09-08 (when NON_US
# became a drop rule) until 2026-09-25 the gate's verdict on every Cardinal
# Health Dublin HQ internship was a drop. The ones open on 2026-09-25 had
# been stored earlier anyway, so no row was lost, but a new one would have
# been.
#
# Some state codes are also ISO country codes, and Workday uses the same
# "CC-City" prefix for countries: "IN-Pune" is India, "DE-Berlin" Germany,
# "CA-Toronto" Canada. For those codes a place in THAT country is still
# decisive: STATE_COUNTRY_PLACES maps each shared code to its COUNTRY_PLACES.
# Shared codes with no entry (GA Gabon, VA Vatican, KY Cayman, ...) have no
# cities in NON_US, so "GA-Athens" and "KY-London" pass. PR is not listed:
# Puerto Rico is US either way.
STATE_COUNTRY_PLACES = {code: re.compile(_words(names + cities), re.I)
                        for code, (names, cities) in COUNTRY_PLACES.items()}
# Every US state code that is also an ISO 3166 country code.
STATE_ISO_CODES = frozenset("""al ar az ca co de ga id il in ky la ma md me mn mo
    ms mt nc ne pa sc sd tn va""".split())
# A country or world-region name stays decisive next to any state code:
# "WA - Perth, Australia" is Western Australia, and a state code never
# makes a country ambiguous. Only NON_US's CITY names are excused.
FOREIGN_COUNTRY = re.compile(_words(_ALL_COUNTRIES), re.I)
# A Canadian province after a comma is decisive in any form ("CA - Esterhazy,
# SK", "Toronto, ON, CA"): no US state has these as a city-level field.
PROVINCE_TAIL = re.compile(r",\s*(?:on|bc|qc|ab|mb|sk|ns|nb|nl|pe)(?:,|\s*$)", re.I)
# Boards join a code to a city with any dash: Thrivent's Workday writes
# "TN – Memphis" (en dash) beside "TX - Houston", and stored rows include
# "KY – Lexington". U+2010..U+2015 are the hyphen and dash characters.
DASH = "[-‐-―]"
# Where a board puts a state code next to its city. Every shape needs
# exactly two UPPERCASE letters: boards write state codes uppercase, and case
# keeps the words "In-Office" and "On-site" from reading as Indiana and a
# province. "IN-OFFICE" and "IN Person" are the same English words with an
# uppercase "IN", so the words boards put after "in" are ruled out by name
# ("IN-OFFICE; London" drops), while an all-caps city after a code is still
# a city ("IN-INDIANAPOLIS").
#   prefix    "OH-Dublin-Cardinal Place", "FL - Melbourne" (Workday, CVS)
#             "MA Boston - PanAgora", "DC Washington 820 1st Street NE" (MGB)
#   suffix    "Paris TN", "Perth Amboy NJ", "Ontario CA" (US Foods), "ARP
#             Manchester MI" (Amcor), "Vienna VA 22182", "Rome - GA",
#             "Dover-NH" (Verdantas), "Dublin (OH)"
#   adjacent  the code right after (or before) the city, then a site label:
#             "Columbus OH - Arena District", "York PA-Op Ctr" (NiSource),
#             "Marlborough MA Branch" (Agiliti), "Littleton CO JMTC" (Johns
#             Manville), "ITS - Ft. Worth TX (WH)" (Echo)
# A prefix or suffix code covers every city in its office; an adjacent code
# covers only the city it touches.
# A foreign region code in any of these positions reads as a state too:
# "WA-Perth" and "Perth WA" are Western Australia, "TN-Chennai" and "Chennai
# TN" Tamil Nadu, and Italian addresses put the province after the city
# ("Milano MI", "20121 Milano (MI)"). No stored row uses those forms, and the
# scorer's `us` flag catches them, so the gate lets them through rather than
# keep a second list of region codes.
_NOT_A_CODE = r"(?!IN\b\W*(?i:office|person|house|store)\b)"
STATE_PREFIX = re.compile(
    rf"^\s*{_NOT_A_CODE}([A-Z]{{2}})(?:\s*{DASH}\s*|\s+(?=[^\W\d_]))")
# The suffix is matched on a trimmed label from at most SUFFIX_SPAN characters
# before its end - the longest suffix, "(XX) 12345-6789" - so a label with a
# long run of spaces or dashes costs linear time, not quadratic.
STATE_SUFFIX = re.compile(
    rf"(?<=[\s(]|{DASH})\(?([A-Z]{{2}})\)?(?:\s+\d{{5}}(?:-\d{{4}})?)?$")
SUFFIX_SPAN = 16
# PNC's Workday ends an office with a branch id that starts with its state
# code: "Downtown Orlando Orange Avenue (FL361)", "PNC Center - Cincinnati
# (OHA45)", "PNC Center - Troy (MIJ40)"; Ferrovial's are "(GA0105)" and
# "(VAS095)". The id reads as a state suffix, so "Melbourne - Harris (FL361)"
# is ambiguous rather than Australian. An id needs three or four characters
# after the code: two digits alone are a UK postcode district or a short site
# id ("Manchester (WA14)", "London (DC12)"). A three-letter site code with
# digits ("Paris (PAR12)") still reads as a branch id, since PNC's "OHA45"
# has the same shape; the scorer's `us` flag judges those.
BRANCH_SUFFIX = re.compile(r"\(([A-Z]{2})(?:[A-Z]\d{2,3}|\d{3,4})\)$")
# A code touching a city: after it with spaces, a dash, parentheses or a
# field separator between ("Paris TX District", "Rome GA-Field", "Paris |
# TX"), or before it with a space or a dash. The code must end the word
# ("Paris ONE" has no code). ADJ_BEFORE is searched in the SUFFIX_SPAN
# characters before the city, for the same linear-time reason as the suffix.
# Digits right after the code make it a postcode or a site id, not a state
# ("Manchester WA14 2DT", "Dublin DC1", "Dublin DC 1"); the one number a
# state code takes after a space is a US ZIP ("Dublin OH 43017 - Site").
# Two letters after a 4-digit number are a Dutch postcode, before the city
# or after it ("Claude Debussylaan 34, 1082 MD Amsterdam", "Amsterdam, 1082
# MD"); POSTCODE_BEFORE is searched in the few characters before the code.
_ADJ_SEP = rf"(?:[\s(|;,/•]|{DASH})"
ADJ_AFTER = re.compile(
    rf"{_ADJ_SEP}+{_NOT_A_CODE}([A-Z]{{2}})(?![^\W_])(?!\s+\d(?!\d{{4}}(?:-\d{{4}})?(?!\d)))")
ADJ_BEFORE = re.compile(
    rf"(?<![^\W_])([A-Z]{{2}})(?:\s*{DASH}\s*|\s+)$")
POSTCODE_BEFORE = re.compile(r"(?<!\d)\d{4}\s*$")
# Uppercase labels spell out English and Spanish connector words that are
# also state codes: "LONDON OR PARIS", "BASED IN PARIS", "SANTIAGO DE
# QUERETARO". Such a word between two words, with only spaces around it, is
# not a code when the office has no lowercase letters, or when both words
# beside it are foreign place names ("London OR Paris"). In a mixed-case
# label an uppercase "IN" before a site label is a state ("Warsaw IN Campus"
# is Warsaw, Indiana), since English writes the word lowercase there. A code
# that starts or ends the office is still one ("Ontario OR", "IN Paris"), as
# is one set off by a dash ("Dublin OR - Branch").
CONNECTOR_CODES = frozenset({"OR", "IN", "DE", "AS", "AT", "BY", "TO"})
_CONNECTOR_BEFORE = re.compile(r"[^\W\d_]\s{1,3}$")
_CONNECTOR_AFTER = re.compile(r"\s{1,3}[^\W\d_]")
US_STATE_NAME = re.compile(rf"\b(?:{US_STATE_NAMES})\b", re.I)
CITY_WORDS = re.compile(_words(_ALL_CITIES), re.I)
# Offices in one location string. Greenhouse joins them with " • ", Walter P
# Moore's Workday with " | ", others with " / " or "; ". Each office is read
# on its own, so the verdict does not depend on which office comes first. A
# field that is only a state code is part of the office before it, not an
# office of its own: Saint Luke's writes "Hospital | address | City | ST".
OFFICE_SPLIT = re.compile(r"(;|\s[|/•]\s)")
CODE_ONLY = re.compile(r"\s*\(?[A-Z]{2}\)?(?:\s+\d{5}(?:-\d{4})?)?\s*")
# "CA-Ontario" is Ontario, California (Cardinal Health's "CA-Ontario-Med Prod
# #4551") when nothing, or a site label ending in a number, follows. A name
# or a street address after it is a Canadian city with the province spelled
# out ("CA-Ontario-Kanata", "CA-Ontario-Brampton 8 Nelson St"); Canada-form
# tenants that abbreviate ("CA-ON-") are caught by the country-region-city
# check. Bare "CA-Ontario" is taken as California: one Cardinal row uses it.
CA_ONTARIO_PREFIX = re.compile(rf"^\s*ontario(?=\s*$|\s*{DASH}.*\d\s*$)", re.I)
# US Foods' "Ontario CA" is the same city in "City CA" order; Canada is
# written out or abbreviated ON there, never "CA". After a comma
# ("Hamilton, Ontario CA") Ontario is a province field and stays Canadian.
CA_ONTARIO_BEFORE = re.compile(rf"(?<![,\s])(\s*)\bontario{_ADJ_SEP}*$", re.I)


def _offices(loc: str) -> list[str]:
    """Split a location into offices, keeping a code-only field with the
    office before it ("Paris | TX" is one office, as is "Pune; IN")."""
    parts = OFFICE_SPLIT.split(loc)
    offices = [parts[0]]
    for i in range(1, len(parts), 2):
        seg = parts[i + 1]
        if CODE_ONLY.fullmatch(seg):
            offices[-1] += parts[i] + seg
        else:
            offices.append(seg)
    return offices


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _blank_all(text: str, spans) -> str:
    """Blank every (start, end) span of `text` in one pass."""
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _connector(text: str, start: int, end: int, upper: bool,
               city_ends: set, city_starts: set) -> bool:
    """True when text[start:end] is a connector word between two words: in
    an all-caps office (`upper`) any two words, otherwise two city names
    (a city ends at an index in `city_ends`, starts at one in `city_starts`).
    """
    if text[start:end] not in CONNECTOR_CODES:
        return False
    before = _CONNECTOR_BEFORE.search(text, max(0, start - 4), start)
    after = _CONNECTOR_AFTER.match(text, end)
    if not (before and after):
        return False
    return upper or (before.start() + 1 in city_ends and after.end() - 1 in city_starts)


def _postcode_before(text: str, start: int) -> bool:
    """True when a 4-digit number sits just before index `start`."""
    return bool(POSTCODE_BEFORE.search(text, max(0, start - 8), start))


def _ontario_before(text: str, end: int) -> str:
    """Blank "Ontario" when it is the city just before a CA code at `end`."""
    m = CA_ONTARIO_BEFORE.search(text, max(0, end - 40), end)
    if m and not text[:m.start()].rstrip().endswith(","):
        return _blank(text, m.end(1), m.end(1) + len("ontario"))
    return text


def _state_excuses(code: str, text: str, dash: bool) -> bool:
    """True when US state `code` makes NON_US's match in `text` ambiguous.

    `text` is the rest of the location (after an "ST-" / "ST " prefix,
    around a code beside a city, or before a ", ST" tail). The only NON_US
    hit it may carry is a city name: a country or region name, a Canadian
    province, an ISO-3 site prefix, or any other NON_US marker left once the
    city names are removed stays decisive. So does a place in the country
    that shares the code ("IN-Pune"), and - for a shared code in dash-prefix
    form - the country-region-city shape ("CA-QC-Longueuil", "DE-BY-Erlangen").
    A US state spelled out in `text` settles it the other way ("Ontario,
    California, CA").
    """
    code = code.lower()
    if (code not in US_STATES or PROVINCE_TAIL.search(text)
            or FOREIGN_COUNTRY.search(text)
            or NON_US.search(CITY_WORDS.sub(" ", text))):
        return False
    if US_STATE_NAME.search(text):
        return True
    if code in STATE_ISO_CODES:
        if dash and re.match(rf"[a-z]{{2,3}}\s*{DASH}", text, re.I):
            return False
        places = STATE_COUNTRY_PLACES.get(code)
        if places and places.search(text):
            return False
    return True


def _office_excuses(office: str) -> bool:
    """True when one office pairs a US state code with its city in a shape
    NON_US cannot overrule ("OH-Dublin-Cardinal Place", "Paris TN",
    "Dublin OH - Arena District").

    Only US state codes count ("ITS - Paris TX (WH)" has no code in "WH").
    Every code found must excuse the office (see _state_excuses), every
    foreign city name in it must be covered by a code, and a code needs some
    text of its own: a bare "OH" names no office.
    """
    checks = []            # (code, text for _state_excuses, dash prefix)
    spans = set()          # code spans already read as a prefix or suffix
    m = STATE_PREFIX.match(office)
    if m and m.group(1).lower() in US_STATES and re.search(r"[^\W\d_]", office[m.end():]):
        rest = office[m.end():]
        if m.group(1) == "CA":
            rest = CA_ONTARIO_PREFIX.sub(" " * len("ontario"), rest)
        checks.append((m.group(1), rest, bool(re.search(DASH, m.group(0)))))
        spans.add(m.span(1))
    trimmed = office.rstrip()
    m = (STATE_SUFFIX.search(trimmed, max(0, len(trimmed) - SUFFIX_SPAN))
         or BRANCH_SUFFIX.search(trimmed, max(0, len(trimmed) - SUFFIX_SPAN)))
    if (m and m.group(1).lower() in US_STATES and not _postcode_before(trimmed, m.start(1))
            and re.search(r"[^\W\d_]", trimmed[:m.start()])):
        head = trimmed[:m.start()]
        if m.group(1) == "CA":
            head = _ontario_before(head, len(head))
        checks.append((m.group(1), head, False))
        spans.add(m.span(1))
    # Codes beside a city, grouped by code: one _state_excuses check per
    # distinct code, on the office with that code's spans blanked, keeps a
    # long office that repeats "Dublin OH" linear rather than one full copy
    # and check per city.
    adjacent = {}          # (code, Ontario before it) -> spans to blank
    cities = list(CITY_WORDS.finditer(office))
    city_ends = {c.end() for c in cities}
    city_starts = {c.start() for c in cities}
    upper = not any(ch.islower() for ch in office)
    for city in cities:
        a = ADJ_AFTER.match(office, city.end())
        b = ADJ_BEFORE.search(office, max(0, city.start() - SUFFIX_SPAN), city.start())
        near = [(n, after) for n, after in ((a, True), (b, False))
                if n and n.group(1).lower() in US_STATES
                and (after or not _postcode_before(office, n.start(1)))
                and not _connector(office, *n.span(1), upper, city_ends, city_starts)]
        if not near and not spans:
            return False
        for n, after in near:
            if n.span(1) in spans:
                continue
            # "Ontario CA" blanks Ontario only for the CA code after it, so
            # that check is kept apart from the other CA codes.
            ontario = after and n.group(1) == "CA"
            blank = adjacent.setdefault((n.group(1), ontario), [])
            blank.append(n.span(1))
            if ontario:
                o = CA_ONTARIO_BEFORE.search(office, max(0, n.start() - 40), n.start())
                if o:
                    blank.append((o.end(1), o.end(1) + len("ontario")))
    for (code, _), blank in adjacent.items():
        checks.append((code, _blank_all(office, blank), False))
    return bool(checks) and all(_state_excuses(*c) for c in checks)

# Workday collapses a posting that spans several offices to a locationsText
# of just "4 Locations" - no city, no state, no country. That is ambiguity,
# not evidence of a non-US role, and this stage is deliberately biased
# toward false positives: pass it through and let the scorer judge.
# Enrichment (sources.enrich) later swaps the placeholder for the detail
# call's real office list, and the scorer's "us" flag - not this regex -
# decides the us_only question for those rows: an LLM knows every US city,
# while US_HINT below is a short hand-picked list.
# "Multiple Locations Available" is the same collapse in words (seen live on
# a surfaced US posting, 2026-08-25), so it gets the same ambiguity pass.
MULTI_LOC = re.compile(r"^\s*(?:\d+|multiple)\s+locations\b", re.I)


def vague_location(loc: str | None) -> bool:
    """True when a location names no office: empty, or a MULTI_LOC collapse.

    Such a location cannot tell two regional requisitions of one title apart,
    so it must not feed job_id (cli pre-enriches first) and must not drive
    repost detection (store.resurface_reposts).
    """
    loc = loc or ""
    return not loc.strip() or bool(MULTI_LOC.match(loc))


def passes(job: dict, cfg: dict) -> tuple[bool, str]:
    """Return (kept, reason_if_dropped)."""
    title = job.get("title", "")

    if not INTERN_PAT.search(title):
        return False, "not an internship title"

    if SENIOR_PAT.search(title):
        return False, "seniority marker in title"

    for kw in cfg.get("exclude_keywords", []):
        if re.search(rf"\b{re.escape(kw)}\b", title, re.I):
            return False, f"excluded keyword: {kw}"

    # Hand-curated hard-no employers. Exists for gates that are invisible in
    # the posting text - Advanced Space asks for a security clearance in the
    # application form itself, which no fetch can see. Exact name match, case
    # insensitive: substring matching would let "Space" ban half of aerospace.
    for name in cfg.get("exclude_companies", []):
        if str(name).lower() == (job.get("company") or "").lower():
            return False, f"excluded company: {name}"

    if (CLEARANCE_TITLE.search(title)
            or clearance_gated(job.get("description", ""))):
        return False, "requires security clearance"

    # Before the cycle check, so a class year gets the reason that names it.
    ok, why = class_year_ok(title, cfg.get("graduation_year"))
    if not ok:
        return False, why

    cycles = parse_cycles(cfg.get("target_cycles", []))
    # A structured `terms` field beats guessing from a title that names no
    # year, so when a feed supplies a live one it decides the cycle question.
    # Two exceptions, both from feeds tagging new postings with last
    # season's term: entries posted in their season's last month or later
    # are dropped (live_terms), and a title that names a target cycle and
    # contradicts the tag ("Software Engineering Intern - Summer 2027")
    # beats it (_title_beats_tag).
    terms = live_terms(job)
    if terms:
        ok, why = terms_ok(terms, cycles)
        if not ok and _title_beats_tag(title, terms, cycles):
            ok, why = True, ""
    else:
        ok, why = cycle_ok(title, cycles)
    if not ok:
        return False, why

    return location_ok(job, cfg)


def location_ok(job: dict, cfg: dict) -> tuple[bool, str]:
    """The US-only gate of `passes`, alone: (kept, reason_if_dropped).

    Split out so cli.enrich_vague can re-run just this gate once a detail
    page has filled in an office. Re-running all of `passes` there would
    also fire the description clearance gate, which drops the row unstored
    instead of recording it the way split_cleared does.
    """
    title = job.get("title", "") or ""
    loc = job.get("location", "") or ""
    blob = f"{title} {loc}"
    # Drop only on evidence of a non-US location. No US hint AND no non-US
    # marker is an office label the list cannot place ("Wilmington, 125
    # South West Street") - ambiguity, which this stage passes through.
    if cfg.get("us_only", True) and loc and not MULTI_LOC.match(loc):
        iso = ISO_TAIL.match(loc)
        code = iso.group(1) if iso else ""
        if code and code.lower() != "us" and (
                code.islower() or code.lower() not in US_STATES):
            return False, f"location outside scope: {loc}"
        if code and code.lower() in US_STATES and not code.islower():
            # A US state tail: only the fields before it can say otherwise,
            # and a foreign city name there may be a US town ("Tysons,
            # Vienna, VA").
            head = loc[:iso.start(1)]
            if NON_US.search(head) and not _state_excuses(code, head, dash=False):
                return False, f"location outside scope: {loc}"
        elif not iso and not US_HINT.search(blob) and NON_US.search(loc):
            # Checked per office: "IN-Pune | OH-Dublin-Cardinal Place" has a
            # US office, and one is enough to pass.
            if not any(_office_excuses(seg) for seg in _offices(loc)):
                return False, f"location outside scope: {loc}"

    return True, ""


def partition(jobs: list[dict], cfg: dict) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Split into (kept, [(dropped_job, reason)]).

    Keeping the dropped postings themselves - not just a tally - is what makes
    the false-negative audit possible: the only way to know this stage is not
    silently eating good matches is to re-examine what it threw away.
    """
    kept, dropped = [], []
    for j in jobs:
        ok, why = passes(j, cfg)
        if ok:
            kept.append(j)
        else:
            dropped.append((j, why))
    return kept, dropped


def run(jobs: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    kept, dropped = partition(jobs, cfg)
    tally: dict[str, int] = {}
    for _, why in dropped:
        tally[why] = tally.get(why, 0) + 1
    return kept, tally
