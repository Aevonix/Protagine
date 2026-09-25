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

Value against interruption uses the one ranker: a candidate's ``salience`` is its expected value
``EV = relevance x novelty x timeliness`` and its ``cost`` the interruption cost. Hard holds (quiet
hours, the owner's pause, the daily budget, a muted topic, a topic's backoff) are checked here, so a
held candidate is never formed and never piles up into a burst; and one unprompted candidate at
most is proposed per tick, so two findings at once are one interruption. Every message says why,
quoting what the owner said where the ledger still holds it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

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


def interest_origin(causes: Iterable[Any]) -> str:
    """Who an interest in ``mind_state`` came from, by its causes: ``welcome`` when only appraisals that saw
    the owner welcome the topic raised it, ``own`` for the agent's identity interest, ``owner`` for anything
    the owner said or set (a declaration turn, a reaction, the CLI or the API)."""
    values = [str(cause) for cause in causes or []]
    if any(IDENTITY_INTEREST not in value and not value.startswith("appraisal:") for value in values):
        return "owner"
    if any(value.startswith("appraisal:") for value in values):
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


def lesson_factor(inputs: OutreachInputs, text: str) -> float:
    """x0.5 for a relevant owner-verified pitfall, x1.2 for a relevant owner-verified strategy."""
    from .lessons import relevant
    factor = 1.0
    for lesson in inputs.lessons:
        if not relevant(lesson, text):
            continue
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
        shared, share = overlap(terms(finding.topic), inputs.memory)
        if shared >= MIN_SHARED and share >= MIN_SHARE and MEMORY_WEIGHT * share > best:
            best, source, interest = MEMORY_WEIGHT * share, "memory", None
    return min(1.0, best * lesson_factor(inputs, f"{finding.topic} {text}")), interest, source


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


def holds(inputs: OutreachInputs, *, slug: str = "", topic: str = "", requested: bool = False) -> Optional[str]:
    """Why nothing may be proposed now, or None. Quiet hours and the owner's pause hold everything;
    the budget, a mute and a topic's backoff hold what the owner did not ask for."""
    if inputs.quiet:
        return "quiet hours"
    if inputs.paused():
        return "the owner paused check-ins"
    if requested:
        return None
    if inputs.budget:
        return inputs.budget
    if slug and muted(inputs, slug, topic):
        return f"the owner does not want messages about {topic}"
    until = backoff_until(inputs, slug) if slug else None
    if until is not None and inputs.now < until:
        return f"{topic} waits until {until.isoformat()}"
    return None


# -- words ----------------------------------------------------------------------------------------

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE = re.compile(r"(?<=[.!?;])\s+|\n+")


def excerpt(summary: str, topic: str, limit: int = EXCERPT_CHARS) -> str:
    """The sentences of a report that bear on the topic (a report that lists everything it read
    carries unrelated items); the first sentence when none does."""
    sentences = [" ".join(part.split()) for part in _SENTENCE.split(str(summary or "")) if part.strip()]
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
    said = quote(inputs, interest.turn if interest else None, topic)
    if said:
        return f'You said "{said}", so I looked into {topic}'
    if source == "welcome":
        return f"You seemed keen on {topic}, so I looked into it"
    if source == "goal":
        return f"This bears on something you are working on, so I looked into {topic}"
    if source == "memory":
        return f"You have mentioned {topic} lately, so I looked into it"
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
    r, interest, source = relevance(finding, inputs)
    if r <= 0:
        return None
    n, t, c = novelty(inputs, finding.slug, "outreach_finding"), finding_timeliness(finding, inputs.now), \
        interruption_cost(inputs)
    ev = round(r * n * t, 4)
    because = _because(inputs, interest, source, finding.topic)
    text = f"{because}: {excerpt(finding.summary, finding.topic)} {REPLY_HINT}"
    return _message("outreach_finding", inputs, key=f"outreach:finding:{finding.id}", topic=finding.topic,
                    text=text, why=because, ev={"r": round(r, 4), "n": n, "t": round(t, 4), "c": c}, salience=ev,
                    cost=c, source=f"intention:{finding.id}", expires=FINDING_EXPIRES_HOURS,
                    evidence=[f"intention:{finding.id}", *([f"turn:{interest.turn}"] if interest and interest.turn
                                                           else [])])


def loop_candidate(loop: Loop, inputs: OutreachInputs) -> Optional[Candidate]:
    topic = loop.description
    p = pressure(inputs, since=loop.created_at)
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
    if holds(inputs, requested=True):
        return None
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


def followup_candidate(item: Followup, inputs: OutreachInputs) -> Candidate:
    """The deeper dig the owner asked for: duty's (owed, never satiable), a template body with the owner's
    words quoted as data. Bound to an assistant promise captured from the same reply, it takes that
    promise's key, so duty never forms a second task for it."""
    description = (f"The owner asked to dig deeper into {item.topic}. What was shared: {item.shared} Look at the "
                   f"sources behind it and report specifics, with where each came from.")
    body = task_body(description=description, drive="duty", concern=f"the owner asked for more on {item.topic}",
                     evidence=[f"intention:{item.outreach_id}"],
                     context=f"The owner's reply: {item.words}" if item.words else "")
    key = f"outreach:followup:{item.outreach_id}"
    check: Dict[str, Any] = {"kind": "result_field", "field": "finding"}
    extra: Dict[str, Any] = {"requested": item.outreach_id, "topic_slug": item.slug}
    if item.commitment:
        from .drives import schedule_key
        if item.commitment_due is not None:
            key = schedule_key(item.commitment, "overdue", item.commitment_due)
        check = {"kind": "commitment_resolved", "commitment_id": item.commitment}
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
        if holds(inputs, slug=finding.slug, topic=finding.topic):
            continue
        made = finding_candidate(finding, inputs)
        if made is not None:
            unprompted.append(made)
    for loop in inputs.loops:
        loop_slug = _slug(loop.description)
        if holds(inputs, slug=loop_slug, topic=loop.description):
            continue
        made = loop_candidate(loop, inputs)
        if made is not None:
            unprompted.append(made)
    for care in inputs.cares:
        if holds(inputs, slug=care.slug, topic=care.thing):
            continue
        made = care_candidate(care, inputs)
        if made is not None:
            unprompted.append(made)
    chosen = sorted(unprompted, key=lambda item: (-(item.salience * (1 - item.cost)), item.dedup_key))
    answers = [made for made in (answer_candidate(finding, inputs) for finding in inputs.findings
                                 if finding.requested_by is not None) if made is not None]
    return round(level, 3), [*chosen, *answers]


def followups(inputs: OutreachInputs) -> List[Candidate]:
    return [followup_candidate(item, inputs) for item in inputs.followups]


def digest_value(finding: Finding, inputs: OutreachInputs) -> float:
    """What a finding that did not go now is worth in the digest: relevance x novelty. Timeliness is left
    aside (at 48 h it is e^-2: the reason it did not go now), as are holds and interruption."""
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


__all__ = ["CARE_HALF_LIFE", "CARE_PREFIX", "Care", "DIGEST_FLOOR", "FINDING_WINDOW", "Finding", "Followup",
           "INDEFINITE", "Interest", "Loop", "MESSAGE_CHARS", "MUTE_FLOOR", "MUTE_HALF_LIFE", "MUTE_PREFIX",
           "NOT_NOW_HOLD", "OWNER_TURN_KEY", "OutreachInputs", "PAUSE_KEY", "REPLY_HOURS", "Sent", "TIMING_PREFIX", "answer_candidate",
           "backoff_until", "candidates", "care_candidate", "digest_value", "excerpt", "finding_candidate",
           "followup_candidate", "followups", "holds", "interest_origin", "interruption_cost", "loop_candidate", "match", "muted",
           "novelty", "open_loops", "overlap", "pause_until", "pressure", "quote", "relevance", "similar", "terms", "weight"]
