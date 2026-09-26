"""Owner outreach: the social drive turned toward the owner (architecture 4.10).

Pure functions over a snapshot the tick gathers (``OutreachInputs``): no store, no model, no clock
of their own. A candidate exists only when there is something concrete to say, from three sources:

- a **finding**: a research-shaped task of the mind's own finished in the last 48 h whose report
  bears on what the owner said they care about (``outreach_finding``);
- an **open loop**: the owner's own open item, not due soon (inside 48 h duty's heads-up and
  reminder speak), not parked, offered after a quiet stretch (``outreach_loop``);
- **care**: the owner said a named thing is stressing them or that they are behind on it
  (``outreach_care``).

There is no "anything you need?" type. The answer to a follow-up the owner asked for
(``outreach_answer``) is requested, and the follow-up itself (``outreach_followup``) is duty's.

A finding must say something: a report that found nothing ("nothing new on X this week") is no
finding, and one whose sentences were already shared (sent, or listed in a digest) is a repeat; both
are settled without a message (``settle``).

Value against interruption uses the one ranker: a candidate's ``salience`` is its expected value
``EV = relevance x novelty x timeliness`` and its ``cost`` the interruption cost. Hard holds (quiet
hours, the owner's pause, the daily budget, the minimum gap after the last outreach, a muted topic,
a topic's backoff) are checked here, so a held candidate is never formed and never piles up into a
burst; and one unprompted candidate at most is proposed per tick, so two findings at once are one
interruption. Every message says why, quoting what the owner said where the ledger still holds it,
and never claims more than the source it came from (an interest the owner declared, one they asked
more about, one they seemed keen on, their open item, their own recent sentence, or the mind's own).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .drives import slug as _slug, task_body
from .rank import OUTREACH_ANSWER, OUTREACH_FOLLOWUP, Candidate

FINDING_WINDOW = timedelta(hours=48)
FINDING_EFOLD_HOURS = 24.0
CARE_HALF_LIFE = timedelta(hours=72)
CARE_EFOLD_HOURS = 12.0
CARE_MIN_LEVEL = 0.25
REPLY_HOURS = 24
RECENT_EFOLD_MINUTES = 60.0
NOT_NOW_HOLD = timedelta(hours=4)
LOOP_MIN_LEAD = timedelta(hours=48)
LOOP_MIN_AGE = timedelta(hours=1)
PRESSURE_FULL_HOURS = 24.0
TOPIC_BACKOFF = timedelta(hours=24)
# Unprompted outreach never goes within two hours of the last outreach, so a day's budget is never one burst.
MIN_GAP = timedelta(hours=2)
# The owner's turn this close before an outreach went out means they were talking to the assistant: a
# bare reply after it answers that conversation, not the outreach (``Mind._link``).
CONVERSATION_GAP = timedelta(minutes=10)
# Offers that check in on the owner (not news): one per quiet stretch, the pressure restarts after each.
CHECK_IN_OFFERS = frozenset({"outreach_loop", "outreach_care"})
TOPIC_BACKOFF_CAP = timedelta(days=30)
MUTE_HALF_LIFE = timedelta(days=90)
MUTE_FLOOR = 0.25
NOVELTY_WINDOW = timedelta(days=7)
DIGEST_FLOOR = 0.3
MIN_SHARED, MIN_SHARE = 2, 0.34
MESSAGE_CHARS = 400
EXCERPT_CHARS = 200
QUOTE_CHARS = 90
FINDING_EXPIRES_HOURS, LOOP_EXPIRES_HOURS = 12, 24
# Interest origins and their weight: the owner's own word counts in full, an appraisal that only saw
# the topic mentioned counts a little, the agent's own identity interest counts least.
OWNER_ORIGINS = frozenset({"owner", "welcome"})
MENTIONED_WEIGHT, OWN_WEIGHT = 0.4, 0.2
GOAL_WEIGHT, MEMORY_WEIGHT = 0.8, 0.6
REPLY_HINT = "Say 'dig deeper' for more, or 'not interested' and I will drop it."

# mind_state keys (the existing table): a pause, a per-hour timing mark, a mute per topic, care per thing.
PAUSE_KEY = "outreach.pause"
OWNER_TURN_KEY = "outreach.owner_turn"      # when the owner last spoke (``Mind.owner_turn``)
TIMING_PREFIX = "outreach.timing:"
MUTE_PREFIX = "outreach.mute:"
CARE_PREFIX = "care:"
INDEFINITE = "indefinite"


IDENTITY_INTEREST = "a declared identity interest"
# Causes that lowered an interest, never raised it: they say nothing about whose it is.
LOWERING_CAUSES = ("silence:", "muted:")
# Causes of an interest the owner welcomed without declaring it: an appraisal that saw it, a reply engaging it.
WELCOME_CAUSES = ("appraisal:", "engaged:")


def interest_origin(causes: Iterable[Any]) -> str:
    """Who an interest in ``mind_state`` came from, by the causes that raised it: ``welcome`` when only
    appraisals that saw the owner welcome the topic, or the owner's engaged replies, raised it; ``own`` for
    the agent's identity interest; ``owner`` for anything the owner said or set (a declaration turn, a
    "dig deeper", the CLI or the API). A silence or a mute lowered it and says nothing about whose it is."""
    values = [str(cause) for cause in causes or [] if not str(cause).startswith(LOWERING_CAUSES)]
    if any(IDENTITY_INTEREST not in value and not value.startswith(WELCOME_CAUSES) for value in values):
        return "owner"
    if any(value.startswith(WELCOME_CAUSES) for value in values):
        return "welcome"
    return "own" if values else "owner"


def terms(text: Any) -> set:
    from .lessons import terms as lesson_terms
    return lesson_terms(text)


def overlap(wanted: Iterable[str], text: Any) -> Tuple[int, float]:
    """(shared terms, the share of ``wanted``'s terms found in ``text``)."""
    mine = set(wanted)
    if not mine:
        return 0, 0.0
    shared = len(mine & terms(text))
    return shared, shared / len(mine)


def similar(topic: str, text: Any) -> bool:
    """A topic is named in ``text`` when at least two of its terms, and a third of them, are there; a
    one-term topic when that term is."""
    mine = terms(topic)
    shared, share = overlap(mine, text)
    return (shared >= MIN_SHARED and share >= MIN_SHARE) or (len(mine) == 1 and shared == 1)


@dataclass
class Interest:
    topic: str
    slug: str
    level: float = 1.0
    origin: str = "owner"          # owner | welcome | mentioned | own
    turn: Optional[str] = None     # the owner turn that declared it, quoted at render time
    asked: bool = False            # the owner asked for more on it ("dig deeper"), not only declared it


@dataclass
class Finding:
    id: str
    type: str
    topic: str
    slug: str
    summary: str
    completed_at: datetime
    requested_by: Optional[str] = None      # the outreach the owner answered with "dig deeper"
    bound_commitment: Optional[str] = None  # an assistant promise the answer keeps


@dataclass
class Loop:
    id: str
    description: str
    created_at: Optional[datetime] = None
    due_at: Optional[datetime] = None


@dataclass
class Care:
    slug: str
    thing: str
    level: float
    turn: Optional[str] = None
    commitment: Optional[str] = None
    due_at: Optional[datetime] = None


@dataclass
class Sent:
    """An outreach row of the last month: when it was queued, its topic, and how the owner took it."""
    id: str
    type: str
    slug: str
    at: datetime
    verdict: Optional[str] = None
    reaction: Optional[str] = None
    source: str = ""
    text: str = ""                  # what the message said (a later finding repeating it is no news)


@dataclass
class Followup:
    """A "dig deeper" the owner sent about an outreach, not yet a task."""
    outreach_id: str
    topic: str
    slug: str
    shared: str                 # what was shared, as the owner saw it
    words: str = ""             # the owner's reply, quoted as data
    commitment: Optional[str] = None
    commitment_due: Optional[datetime] = None
    offer: bool = False         # the owner took up an offer of help (a loop or care), not a finding


@dataclass
class OutreachInputs:
    now: datetime
    owner_id: str
    quiet: bool = False
    paused_until: Optional[datetime] = None     # datetime.max: until the owner resumes
    budget: Optional[str] = None                # the daily budget's reason, when it is spent
    last_owner_turn: Optional[datetime] = None
    local_hour: int = 12
    interests: List[Interest] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    loops: List[Loop] = field(default_factory=list)
    cares: List[Care] = field(default_factory=list)
    mutes: Dict[str, str] = field(default_factory=dict)        # slug -> topic
    timing: Dict[int, float] = field(default_factory=dict)     # local hour -> 0..1
    sent: List[Sent] = field(default_factory=list)             # newest first
    queued_24h: int = 0                                        # unprompted owner messages queued in 24 h
    goals: List[str] = field(default_factory=list)             # the owner's open goals and items
    memory: str = ""                                           # the owner's own words of the last 30 days
    quotes: Dict[str, str] = field(default_factory=dict)       # turn id -> the owner's words, while held
    lessons: List[Any] = field(default_factory=list)           # owner-verified outreach lessons
    followups: List[Followup] = field(default_factory=list)
    seen: List[str] = field(default_factory=list)              # findings the digest listed or holds (30 days)
    held: Dict[str, str] = field(default_factory=dict)         # commitment id -> a matter the owner holds

    def paused(self) -> bool:
        return self.paused_until is not None and self.now < self.paused_until


# -- value ----------------------------------------------------------------------------------------

def pressure(inputs: OutreachInputs, since: Optional[datetime] = None) -> float:
    """Social pressure toward the owner: rises with the hours since the owner last spoke (or since
    ``since``, whichever is later) and is satiated by the next turn."""
    anchors = [moment for moment in (inputs.last_owner_turn, since) if moment is not None]
    if not anchors:
        return 0.0
    hours = max(0.0, (inputs.now - max(anchors)).total_seconds() / 3600.0)
    return min(1.0, hours / PRESSURE_FULL_HOURS)


def weight(interest: Interest) -> float:
    """What an interest counts for relevance by its origin; one decayed or muted to nothing counts nothing."""
    if interest.level <= 0:
        return 0.0
    if interest.origin in OWNER_ORIGINS:
        return min(1.0, 0.75 + 0.25 * max(0.0, interest.level))
    return MENTIONED_WEIGHT if interest.origin == "mentioned" else OWN_WEIGHT


def match(interest: Interest, finding: Finding, excerpt_text: str) -> float:
    if interest.slug == finding.slug:
        return 1.0
    mine = terms(interest.topic)
    shared, share = overlap(mine, f"{finding.topic} {excerpt_text}")
    return share if shared >= MIN_SHARED and share >= MIN_SHARE else 0.0


def muted(inputs: OutreachInputs, slug: str, topic: str) -> bool:
    """A mute on the slug itself, or on a topic sharing two terms and a third of its own."""
    if slug in inputs.mutes:
        return True
    return any(similar(text, topic) for text in inputs.mutes.values())


def bears_on(lesson: Any, topic: str, text: str) -> bool:
    """Whether an outreach lesson bears on a topic: its signature names the topic (``topic:<slug>``,
    ``outreach_finding:<slug>``, the same slug or two shared terms), else its words are relevant to the text."""
    from .lessons import relevant
    named = str(getattr(lesson, "signature", "") or "").partition(":")[2]
    if named and (named == _slug(topic) or similar(named.replace("-", " "), topic)):
        return True
    return relevant(lesson, text)


def lesson_factor(inputs: OutreachInputs, topic: str, text: str) -> float:
    """x0.5 for each owner-verified pitfall bearing on the topic, x1.2 for each such strategy."""
    factor = 1.0
    for lesson in inputs.lessons:
        if bears_on(lesson, topic, text):
            factor *= 0.5 if getattr(lesson, "kind", "") == "pitfall" else 1.2
    return factor


def relevance(finding: Finding, inputs: OutreachInputs) -> Tuple[float, Optional[Interest], str]:
    """``(r, the interest it came from, the source)``: the best of what the owner said they care about,
    their open goals and items, and their own recent words. A muted topic is 0."""
    if muted(inputs, finding.slug, finding.topic):
        return 0.0, None, "muted"
    text = excerpt(finding.summary, finding.topic)
    best, source, interest = 0.0, "", None
    for item in inputs.interests:
        value = match(item, finding, text) * weight(item)
        if value > best:
            best, source, interest = value, item.origin, item
    for goal in inputs.goals:
        shared, share = overlap(terms(finding.topic), goal)
        if shared >= MIN_SHARED and share >= MIN_SHARE and GOAL_WEIGHT * share > best:
            best, source, interest = GOAL_WEIGHT * share, "goal", None
    if inputs.memory:
        # The topic named in one sentence of the owner's, never its words scattered over a month of turns.
        wanted, said = terms(finding.topic), 0.0
        for sentence in _SENTENCE.split(inputs.memory):
            shared, share = overlap(wanted, sentence)
            if shared >= MIN_SHARED and share >= MIN_SHARE:
                said = max(said, share)
        if MEMORY_WEIGHT * said > best:
            best, source, interest = MEMORY_WEIGHT * said, "memory", None
    return min(1.0, best * lesson_factor(inputs, finding.topic, f"{finding.topic} {text}")), interest, source


# A report that found nothing: no finding, whatever its topic words.
NULL_REPORT = re.compile(
    r"\b(?:nothing\s+(?:new|notable|of\s+note|relevant|further|else|to\s+report|found|turned\s+up|came\s+up)"
    r"|no\s+(?:new|fresh|recent|further)\b[^.;:!?]{0,60}?\b(?:found|stud(?:y|ies)|updates?|developments?|news|results?"
    r"|findings?|information|papers?|items?|changes?|reports?|sources?|articles?)"
    r"|(?:found|turned\s+up|there\s+(?:is|was|were|are))\s+(?:nothing|no\s+(?:new|relevant|recent))"
    r"|(?:could|did|can|was|were)(?:n't|\s+not)\s+(?:find|able\s+to\s+find|turn\s+up|locate)\s+(?:anything|any)"
    r"|without\s+(?:anything|any\s+(?:news|change|update|development)s?)"
    r"|no\s+results?|finding\s*[:=]\s*(?:none|nothing|n/?a|null|-)(?=\W|$))",
    re.IGNORECASE)
REPEAT_OVERLAP = 0.8
_TOKEN = re.compile(r"[\w-]+")


def _tokens(text: str) -> set:
    """Words for comparing what was said: codes and numbers kept ("QX-41" is not "RB-17")."""
    return {word for word in _TOKEN.findall(str(text or "").casefold()) if len(word) > 2 or any(
        ch.isdigit() for ch in word)}


def _sentences(text: str) -> List[str]:
    return [" ".join(part.split()) for part in _SENTENCE.split(str(text or "")) if part.strip()]


# Whether a report's sentence is a finding is a typed decision (``substance``): FINDING, EMPTY or UNCERTAIN, read
# from word classes. A report about the research itself (``PROCESS_WORDS``) or holding only a null marker
# (``NULL_MARKERS``) says nothing; a state the topic itself reached (``EVENT_WORDS`` with the topic named: "the
# Orkney project was completed") is a milestone, a finding; facts beyond the topic are findings. What the rule
# cannot decide (one stray word) is UNCERTAIN: it goes to the digest, never sent at once nor discarded.
FINDING, EMPTY, UNCERTAIN = "finding", "empty", "uncertain"
PROCESS_WORDS = frozenset("""
research researched researching report reports reported reporting finding findings search searched searching
searches look looked looking checked check checking review reviewed reviewing scan scanned scanning monitoring
monitored investigation investigated investigating dig digging sources source summary results result status
""".split())
NULL_WORDS = frozenset("""
nil nothing none quiet unchanged same remains remain remained significant notable noteworthy developments
development news new updates update changes change changed further additional anything something everything
relevant material major meaningful interesting successfully success without yet still more else add added
""".split())
NULL_MARKERS = frozenset("nil nothing none quiet unchanged remains remain remained without".split())
EVENT_WORDS = frozenset("""
complete completed completes done finished finish started starts launched launches approved approves opened opens
closed closes cancelled canceled announced published released delayed postponed paused resumed awarded signed
rejected
""".split())
CONNECTIVES = frozenset("""
the and for with about into from was were are has have had been being any all its it's can could would will not
this that these those there here today yesterday week month currently now time again got
""".split())
NO_SUBSTANCE = PROCESS_WORDS | NULL_WORDS | EVENT_WORDS | CONNECTIVES
_LABEL = re.compile(r"^\s*(?:findings?|report|update|result|summary)\s*[:=-]\s*", re.IGNORECASE)


def substance(sentence: str, topic: str) -> str:
    """The typed decision on one sentence of a report: ``FINDING``, ``EMPTY`` or ``UNCERTAIN`` (see above)."""
    sentence = _LABEL.sub("", str(sentence or ""))
    if NULL_REPORT.search(sentence):
        return EMPTY
    words, about = _tokens(sentence), _tokens(topic)
    content = words - about - NO_SUBSTANCE
    if len(content) >= 2 or any(any(ch.isdigit() for ch in word) for word in content):
        return FINDING
    if words & PROCESS_WORDS:
        return UNCERTAIN if content else EMPTY
    if words & EVENT_WORDS and words & about:
        return FINDING
    if words & NULL_MARKERS or re.search(r"\bno\b", sentence, re.IGNORECASE):
        return UNCERTAIN if content else EMPTY
    return UNCERTAIN if content else EMPTY


def says_something(sentence: str, topic: str) -> bool:
    """A sentence that is not certainly empty (``substance``): a finding, or one the rule cannot decide."""
    return substance(sentence, topic) != EMPTY


def repeated(text: str, inputs: OutreachInputs) -> bool:
    """Every sentence of ``text`` was already shared: sent in an outreach, or listed in a digest."""
    mine = [_tokens(sentence) for sentence in _sentences(text)]
    mine = [tokens for tokens in mine if tokens]
    if not mine:
        return False
    shared = [_tokens(sentence) for said in [*(item.text for item in inputs.sent if item.text), *inputs.seen]
              for sentence in _sentences(said)]
    return all(any(tokens <= other or len(tokens & other) / len(tokens | other) >= REPEAT_OVERLAP for other in shared)
               for tokens in mine)


def settle(finding: Finding, inputs: OutreachInputs) -> Optional[str]:
    """``empty`` for a report that found nothing, ``repeat`` for one already shared, ``uncertain`` for one whose
    sentences the typed decision (``substance``) cannot call a finding, else None. An answer the owner asked for
    is never settled here: "I looked and found nothing more" is its honest answer."""
    if finding.requested_by is not None:
        return None
    text = excerpt(finding.summary, finding.topic, substantive=True)
    if not text:
        return EMPTY
    if repeated(text, inputs):
        return "repeat"
    if not any(substance(sentence, finding.topic) == FINDING for sentence in _sentences(finding.summary)):
        return UNCERTAIN            # the digest's, never a message of its own nor discarded
    return None


def novelty(inputs: OutreachInputs, slug: str, type: str) -> float:
    recent = [item for item in inputs.sent if item.slug == slug and inputs.now - item.at <= NOVELTY_WINDOW]
    if not recent:
        return 1.0
    return 0.2 if type == "outreach_loop" else 0.3


def finding_timeliness(finding: Finding, now: datetime) -> float:
    return math.exp(-max(0.0, (now - finding.completed_at).total_seconds()) / 3600.0 / FINDING_EFOLD_HOURS)


def care_age_hours(care: Care) -> float:
    """Care decays from 1 with a 72 h half-life and is set to 1 by each strain, so its level says how
    long ago the owner last said it."""
    level = min(1.0, max(1e-6, care.level))
    return CARE_HALF_LIFE.total_seconds() / 3600.0 * math.log2(1.0 / level)


# -- interruption ---------------------------------------------------------------------------------

def ignored_streak(inputs: OutreachInputs) -> int:
    streak = 0
    for item in inputs.sent:
        if item.verdict == "ignored":
            streak += 1
        elif item.verdict is not None or item.reaction is not None:
            break
    return min(3, streak)


def topic_streak(inputs: OutreachInputs, slug: str) -> int:
    streak = 0
    for item in inputs.sent:
        if item.slug != slug:
            continue
        if item.verdict in {"ignored", "not_useful"}:
            streak += 1
        elif item.verdict in {"useful", "actioned"}:
            break
    return streak


def backoff_until(inputs: OutreachInputs, slug: str) -> Optional[datetime]:
    """The next outreach on a topic waits ``24 h x 2^streak`` after the last one on it, capped at 30 d."""
    last = next((item.at for item in inputs.sent if item.slug == slug), None)
    if last is None:
        return None
    wait = min(TOPIC_BACKOFF_CAP, TOPIC_BACKOFF * (2 ** topic_streak(inputs, slug)))
    return last + wait


def interruption_cost(inputs: OutreachInputs) -> float:
    """``min(0.9, 0.35 e^(-m/60) + 0.15 s + 0.10 k + 0.20 h)``: m minutes since the last outreach went
    out, s the owner's ignored streak, k unprompted owner messages in the last day, h the "not now"
    mark on this hour."""
    last = max((item.at for item in inputs.sent), default=None)
    recent = 0.35 * math.exp(-max(0.0, (inputs.now - last).total_seconds()) / 60.0 / RECENT_EFOLD_MINUTES) \
        if last is not None else 0.0
    hour = min(1.0, max(0.0, float(inputs.timing.get(inputs.local_hour, 0.0))))
    return round(min(0.9, recent + 0.15 * ignored_streak(inputs) + 0.10 * inputs.queued_24h + 0.20 * hour), 4)


def held(inputs: OutreachInputs, topic: str = "", *, commitment: Optional[str] = None, turn: Optional[str] = None
         ) -> Optional[str]:
    """The matter the owner holds that an outreach is about, or None: a held item (a hold, listed or from its
    first mention) it names or is about, or a turn of the owner's that asked for no reminders (its care). Every
    kind of outreach (a finding, its answer, a follow-up, an open loop, care) is held by it alike."""
    from protagine.commitments.extract import asks_no_reminders
    if commitment and str(commitment) in inputs.held:
        return inputs.held[str(commitment)]
    for matter in inputs.held.values():
        if topic and (similar(topic, matter) or similar(matter, topic)):
            return matter
    if turn and asks_no_reminders(inputs.quotes.get(turn, "")):
        return topic or "what the owner asked no reminders about"
    return None


def holds(inputs: OutreachInputs, *, slug: str = "", topic: str = "", requested: bool = False) -> Optional[str]:
    """Why nothing may be proposed now, or None. Quiet hours and the owner's pause hold everything;
    the budget, the gap after the last outreach, a mute and a topic's backoff hold what the owner did
    not ask for."""
    if inputs.quiet:
        return "quiet hours"
    if inputs.paused():
        return "the owner paused check-ins"
    if requested:
        return None
    if inputs.budget:
        return inputs.budget
    last = max((item.at for item in inputs.sent), default=None)
    if last is not None and inputs.now - last < MIN_GAP:
        return f"the last outreach went out at {last.isoformat()}"
    if slug and muted(inputs, slug, topic):
        return f"the owner does not want messages about {topic}"
    until = backoff_until(inputs, slug) if slug else None
    if until is not None and inputs.now < until:
        return f"{topic} waits until {until.isoformat()}"
    return None


# -- words ----------------------------------------------------------------------------------------

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE = re.compile(r"(?<=[.!?;])\s+|\n+")


def excerpt(summary: str, topic: str, limit: int = EXCERPT_CHARS, *, substantive: bool = False) -> str:
    """The sentences of a report that bear on the topic (a report that lists everything it read
    carries unrelated items); the first sentence when none does. ``substantive``: only sentences that
    report something (``says_something``), "" when none does."""
    sentences = _sentences(summary)
    if substantive:
        sentences = [sentence for sentence in sentences if says_something(sentence, topic)]
    mine = terms(topic)
    kept = [sentence for sentence in sentences if mine & terms(sentence)] or sentences[:1]
    text = ""
    for sentence in kept:
        candidate = f"{text} {sentence}".strip()
        if len(candidate) > limit:
            if not text:
                text = sentence[: limit - 1].rstrip() + "…"
            break
        text = candidate
    return text


def quote(inputs: OutreachInputs, turn: Optional[str], topic: str) -> str:
    """The owner's own sentence naming the topic, from the turn while the ledger holds it."""
    words = inputs.quotes.get(str(turn or "")) if turn else None
    if not words:
        return ""
    from .reactions import strip_prefix
    clauses = [" ".join(part.split()) for part in _CLAUSE.split(strip_prefix(words)) if part.strip()]
    mine = terms(topic)
    chosen = next((clause for clause in clauses if mine & terms(clause)), "")
    chosen = chosen.rstrip(".!?;:,")
    return chosen if len(chosen) <= QUOTE_CHARS else chosen[: QUOTE_CHARS - 1].rstrip() + "…"


def _fit(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MESSAGE_CHARS else text[: MESSAGE_CHARS - 1].rstrip() + "…"


def _because(inputs: OutreachInputs, interest: Optional[Interest], source: str, topic: str) -> str:
    """The reason a finding carries, from the source its relevance came from and no more: the owner's
    words while the ledger holds them, else what that source is."""
    said = quote(inputs, interest.turn if interest else None, topic)
    if said:
        return f'You said "{said}", so I looked into {topic}'
    if source == "welcome":
        return f"You seemed keen on {topic}, so I looked into it"
    if source == "goal":
        return f"This bears on something you are working on, so I looked into {topic}"
    if source == "memory":
        return f"You have mentioned {topic} lately, so I looked into it"
    if source == "mentioned":
        return f"You showed some interest in {topic}, so I looked into it"
    if source == "own":
        return f"I have been following {topic} myself, and this looked worth passing on"
    if interest is not None and interest.asked:
        return f"You asked me for more on {topic} before, so I looked into it"
    return f"You told me {topic} matters to you, so I looked into it"


def _due(due: Optional[datetime]) -> str:
    return f", due {due.astimezone(timezone.utc).strftime('%a %d %b')}" if due is not None else ""


# -- candidates -----------------------------------------------------------------------------------

def _message(type: str, inputs: OutreachInputs, *, key: str, topic: str, text: str, why: str, ev: Dict[str, float],
             salience: float, cost: float, source: str, expires: int, evidence: List[str],
             invalidates_if: Optional[str] = None, success_check: Optional[Dict[str, Any]] = None,
             extra: Optional[Dict[str, Any]] = None) -> Candidate:
    """Every outreach message: to the owner and no one else, a template text, the reason it carries."""
    slug = _slug(topic)
    return Candidate(
        type=type, drive="social", kind="message", title=f"{why}"[:160], dedup_key=key,
        salience=round(max(0.0, min(1.0, salience)), 4), cost=round(max(0.0, min(0.9, cost)), 4),
        recipient=inputs.owner_id, text=_fit(text), rationale=why, evidence=evidence,
        concern=f"tell the owner: {topic}"[:160], topic=topic, concern_kind="social", source_type="outreach",
        source_id=source, invalidates_if=invalidates_if, success_check=success_check,
        extra={"topic_slug": slug, "why": why, "source_ref": source, "ev": ev, "expires_hours": expires,
               **(extra or {})})


def finding_candidate(finding: Finding, inputs: OutreachInputs) -> Optional[Candidate]:
    if settle(finding, inputs):
        return None
    r, interest, source = relevance(finding, inputs)
    if r <= 0:
        return None
    n, t, c = novelty(inputs, finding.slug, "outreach_finding"), finding_timeliness(finding, inputs.now), \
        interruption_cost(inputs)
    ev = round(r * n * t, 4)
    because = _because(inputs, interest, source, finding.topic)
    text = f"{because}: {excerpt(finding.summary, finding.topic, substantive=True)} {REPLY_HINT}"
    return _message("outreach_finding", inputs, key=f"outreach:finding:{finding.id}", topic=finding.topic,
                    text=text, why=because, ev={"r": round(r, 4), "n": n, "t": round(t, 4), "c": c}, salience=ev,
                    cost=c, source=f"intention:{finding.id}", expires=FINDING_EXPIRES_HOURS,
                    evidence=[f"intention:{finding.id}", *([f"turn:{interest.turn}"] if interest and interest.turn
                                                           else [])])


def loop_candidate(loop: Loop, inputs: OutreachInputs) -> Optional[Candidate]:
    """An offer of help with the owner's open item after a quiet stretch: the pressure counts from the
    owner's last turn, the item's creation or the last check-in offer, whichever is latest, so one quiet
    stretch is one check-in."""
    topic = loop.description
    offered = max((item.at for item in inputs.sent if item.type in CHECK_IN_OFFERS), default=None)
    anchors = [moment for moment in (loop.created_at, offered) if moment is not None]
    p = pressure(inputs, since=max(anchors) if anchors else None)
    n, c = novelty(inputs, _slug(topic), "outreach_loop"), interruption_cost(inputs)
    ev = round(0.9 * n * p, 4)
    if ev <= 0:
        return None
    why = f"You mentioned this open item: {loop.description}{_due(loop.due_at)}"
    text = (f"{why}. It has been quiet for a while, so I am checking in: want a hand with it? I could draft it, "
            f"break it into steps or keep track of what is left.")
    year, week, _ = inputs.now.isocalendar()
    return _message("outreach_loop", inputs, key=f"outreach:loop:{loop.id}:{year}w{week:02d}", topic=topic,
                    text=text, why=why, ev={"r": 0.9, "n": n, "t": round(p, 4), "c": c}, salience=ev, cost=c,
                    source=f"commitment:{loop.id}", expires=LOOP_EXPIRES_HOURS,
                    evidence=[f"commitment:{loop.id}"], invalidates_if=f"commitment:{loop.id}:resolved")


def care_candidate(care: Care, inputs: OutreachInputs) -> Optional[Candidate]:
    if care.level < CARE_MIN_LEVEL:
        return None
    t = math.exp(-care_age_hours(care) / CARE_EFOLD_HOURS)
    n, c = novelty(inputs, care.slug, "outreach_care"), interruption_cost(inputs)
    said = quote(inputs, care.turn, care.thing)
    why = f'You said "{said}"' if said else f"You said the {care.thing} is weighing on you"
    text = (f"{why}. Want some help with the {care.thing}{_due(care.due_at)}? I could break it into steps, draft "
            f"a first pass, or keep track of what is left.")
    return _message("outreach_care", inputs, key=f"outreach:care:{care.slug}:{care.turn or 'turn'}", topic=care.thing,
                    text=text, why=why, ev={"r": 1.0, "n": n, "t": round(t, 4), "c": c}, salience=round(n * t, 4),
                    cost=c, source=f"care:{care.slug}", expires=LOOP_EXPIRES_HOURS,
                    evidence=[f"care:{care.slug}", *([f"turn:{care.turn}"] if care.turn else []),
                              *([f"commitment:{care.commitment}"] if care.commitment else [])],
                    invalidates_if=f"commitment:{care.commitment}:resolved" if care.commitment else None)


def answer_candidate(finding: Finding, inputs: OutreachInputs) -> Optional[Candidate]:
    """The report of a follow-up the owner asked for: requested, so its value is whole and it costs
    nothing to send; only quiet hours and the owner's pause hold it."""
    if holds(inputs, requested=True) or muted(inputs, finding.slug, finding.topic):
        return None     # a mute after the request is the owner's later word: it stands
    why = f"You asked me to dig deeper into {finding.topic}"
    text = f"{why}. Here is what I found: {excerpt(finding.summary, finding.topic, limit=260)}"
    check = {"kind": "commitment_resolved", "commitment_id": finding.bound_commitment} \
        if finding.bound_commitment else None
    return _message(OUTREACH_ANSWER, inputs, key=f"outreach:answer:{finding.id}", topic=finding.topic, text=text,
                    why=why, ev={"r": 1.0, "n": 1.0, "t": 1.0, "c": 0.0}, salience=1.0, cost=0.0,
                    source=f"intention:{finding.id}", expires=FINDING_EXPIRES_HOURS,
                    evidence=[f"intention:{finding.id}",
                              *([f"intention:{finding.requested_by}"] if finding.requested_by else [])],
                    success_check=check,
                    extra={"requested": finding.requested_by or True,
                           **({"bound_commitment": finding.bound_commitment} if finding.bound_commitment else {})})


# The kind capture gives the assistant's promise to find something out and report it back (``commitments.extract``).
ANSWER_KIND = "answer"


def binds_followup(record: Mapping[str, Any], item: Followup) -> bool:
    """Whether an assistant promise is the follow-up an owner's reply asked for: an answer (find out and report
    back, ``ANSWER_KIND``) that capture linked to THAT outreach (``metadata.outreach``, recorded only when the
    reply was linked to the outreach and the extractor marked the promise as following it up). Never by type
    and topic: "find out whether my grant application was approved", said in the same reply and about the same
    subject, is its own question, so delivering the research never closes it; duty keeps it."""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    return (metadata.get("kind") == ANSWER_KIND and bool(item.outreach_id)
            and str(metadata.get("outreach") or "") == str(item.outreach_id))


def followup_candidate(item: Followup, inputs: OutreachInputs) -> Candidate:
    """The deeper dig the owner asked for: duty's (owed, never satiable), a template body with the owner's
    words quoted as data. Bound to an assistant promise captured from the same reply, it takes that
    promise's key, so duty never forms a second task for it."""
    if item.offer:
        description = (f"The owner took up an offer of help with {item.topic}. What was offered: {item.shared} Do "
                       f"what their reply asks, from their own words, and report what you prepared.")
    else:
        description = (f"The owner asked to dig deeper into {item.topic}. What was shared: {item.shared} Look at "
                       f"the sources behind it and report specifics, with where each came from.")
    body = task_body(description=description, drive="duty", concern=f"the owner asked for more on {item.topic}",
                     evidence=[f"intention:{item.outreach_id}"],
                     context=f"The owner's reply: {item.words}" if item.words else "")
    key = f"outreach:followup:{item.outreach_id}"
    # The dig reports a finding; a promise it is bound to is kept when the answer is sent (``outreach_answer``).
    check: Dict[str, Any] = {"kind": "result_field", "field": "finding"}
    extra: Dict[str, Any] = {"requested": item.outreach_id, "topic_slug": item.slug}
    if item.commitment:
        from .drives import schedule_key
        if item.commitment_due is not None:
            key = schedule_key(item.commitment, "overdue", item.commitment_due)
        extra["bound_commitment"] = item.commitment
    return Candidate(
        type=OUTREACH_FOLLOWUP, drive="duty", kind="task", title=f"Dig deeper: {item.topic}"[:160], dedup_key=key,
        salience=0.9, cost=0.15, text=body, rationale="the owner asked for more on something the mind shared",
        evidence=[f"intention:{item.outreach_id}"], concern=f"the owner asked for more on {item.topic}"[:160],
        topic=item.topic, concern_kind="obligation", source_type="outreach", source_id=f"intention:{item.outreach_id}",
        priority=0.8, success_check=check, extra=extra)


def _when(value: Any) -> Optional[datetime]:
    from .drives import _utc
    return _utc(value)


def open_loops(commitments: Iterable[Dict[str, Any]], *, owner_id: str, now: datetime,
               skip: Iterable[str] = ()) -> List[Loop]:
    """The owner's own open items worth an offer of help: pending (an overdue one is duty's reminder), due
    more than 48 h ahead or undated (inside 48 h duty's heads-up and reminder speak), not parked (a hold:
    rescheduled with no date), not a message to someone else (a granted notice, check-in, cadence or
    deliverable), not owed between two other people, and at least an hour old. ``skip``: rows a care
    offer already names."""
    from .drives import CADENCE_KIND, GRANTED_KINDS, _obligor, owed_between_others
    skipped = set(skip)
    loops = []
    for row in commitments:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        ident = str(row.get("id") or "")
        if not ident or ident in skipped or str(row.get("status") or "") != "pending":
            continue
        person = str(row.get("person_id") or "") or None
        if _obligor(row, metadata, person, owner_id) != "owner" or owed_between_others(row, owner_id=owner_id):
            continue
        if str(metadata.get("kind") or "") in {*GRANTED_KINDS, CADENCE_KIND, "deliverable"}:
            continue
        due, created = _when(row.get("due_at")), _when(row.get("made_at") or row.get("created_at"))
        if due is None and metadata.get("reschedule"):
            continue
        if due is not None and due - now <= LOOP_MIN_LEAD:
            continue
        if created is not None and now - created < LOOP_MIN_AGE:
            continue
        description = " ".join(str(row.get("description") or "").split())
        if description:
            loops.append(Loop(id=ident, description=description[:160], created_at=created, due_at=due))
    return loops


def candidates(inputs: OutreachInputs) -> Tuple[float, List[Candidate]]:
    """The social drive's owner branch: its level is the pressure toward the owner; every source not held,
    and every answer the owner asked for. The ranker (with affect) and the tick choose what goes: at most
    one unprompted message a tick (``Mind._act``), so two findings at once are one interruption."""
    level = pressure(inputs)
    unprompted: List[Candidate] = []
    for finding in inputs.findings:
        if finding.requested_by is not None:
            continue
        if holds(inputs, slug=finding.slug, topic=finding.topic) or held(inputs, finding.topic,
                                                                          commitment=finding.bound_commitment):
            continue
        made = finding_candidate(finding, inputs)
        if made is not None:
            unprompted.append(made)
    for loop in inputs.loops:
        loop_slug = _slug(loop.description)
        if holds(inputs, slug=loop_slug, topic=loop.description) or held(inputs, loop.description, commitment=loop.id):
            continue
        made = loop_candidate(loop, inputs)
        if made is not None:
            unprompted.append(made)
    for care in inputs.cares:
        if holds(inputs, slug=care.slug, topic=care.thing) or held(inputs, care.thing, commitment=care.commitment,
                                                                     turn=care.turn):
            continue
        made = care_candidate(care, inputs)
        if made is not None:
            unprompted.append(made)
    chosen = sorted(unprompted, key=lambda item: (-(item.salience * (1 - item.cost)), item.dedup_key))
    answers = [made for made in (answer_candidate(finding, inputs) for finding in inputs.findings
                                 if finding.requested_by is not None
                                 and not held(inputs, finding.topic, commitment=finding.bound_commitment))
               if made is not None]
    return round(level, 3), [*chosen, *answers]


def followups(inputs: OutreachInputs) -> List[Candidate]:
    """The digs the owner asked for, except about a matter they hold (``held``): formed once the hold lifts."""
    return [followup_candidate(item, inputs) for item in inputs.followups
            if not held(inputs, item.topic, commitment=item.commitment)]


def digest_value(finding: Finding, inputs: OutreachInputs) -> float:
    """What a finding that did not go now is worth in the digest: relevance x novelty. Timeliness is left
    aside (at 48 h it is e^-2: the reason it did not go now), as are holds and interruption."""
    if settle(finding, inputs) in {EMPTY, "repeat"}:
        return 0.0
    r, _, _ = relevance(finding, inputs)
    return round(r * novelty(inputs, finding.slug, "outreach_finding"), 4)


def pause_until(entry: Optional[Dict[str, Any]]) -> Optional[datetime]:
    """The end of the owner's pause from its ``mind_state`` row; ``datetime.max`` when indefinite."""
    if not entry or float(entry.get("level") or 0.0) <= 0.0:
        return None
    text = str(entry.get("text") or INDEFINITE)
    if text == INDEFINITE:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


__all__ = ["CARE_HALF_LIFE", "CARE_PREFIX", "CHECK_IN_OFFERS", "CONVERSATION_GAP", "bears_on", "Care", "DIGEST_FLOOR",
           "FINDING_WINDOW", "Finding", "Followup", "INDEFINITE", "Interest", "Loop", "MESSAGE_CHARS", "MIN_GAP",
           "MUTE_FLOOR", "MUTE_HALF_LIFE", "MUTE_PREFIX", "NO_SUBSTANCE", "NULL_REPORT", "repeated", "says_something", "settle", "substance",
           "NOT_NOW_HOLD", "OWNER_TURN_KEY", "OutreachInputs", "PAUSE_KEY", "REPLY_HOURS", "Sent", "TIMING_PREFIX", "answer_candidate",
           "backoff_until", "candidates", "care_candidate", "digest_value", "excerpt", "finding_candidate",
           "followup_candidate", "followups", "held", "holds", "binds_followup", "interest_origin", "interruption_cost", "loop_candidate", "match", "muted",
           "novelty", "open_loops", "overlap", "pause_until", "pressure", "quote", "relevance", "similar", "terms", "weight"]
