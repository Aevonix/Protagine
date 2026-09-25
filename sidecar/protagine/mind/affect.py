"""The agent's own feelings (architecture 4.3): capped, decaying levels with cited causes.

Every tick ``Affect.gather`` reads one snapshot of stored records (``AffectInputs``): the owner's
reported outcomes and appraisal records of the last 7 days (``AppraisalStore.affect_events``),
intention rows (failed, blocked, check-verified, owner verdicts), expectation misses, novel owner
topics, near-term owed obligations and the worker counts. Two readings share it:

- **the state** (``mind.faculties.affect``): the snapshot's new events are folded, once each, into
  decaying ``mind_state`` rows (``affect.frustration:<topic>``, ``affect.worry``,
  ``affect.curiosity``, ``affect.satisfaction``, ``affect.dismissed``), each capped at 0.7 with at
  most 5 cited causes; a late event is applied as if at its time and then decayed;
- **the stateless rules** (``mind.faculties.affect_rules``, ``affect_rules.py``): windowed counts
  over the same snapshot, the mechanism arm of the affect family. With it on the rules replace the
  state (nothing is kept), so the two switches make three modes: off, the state, the rules.

Four consumers read one ``AffectView`` whichever source produced it: ``strategy_switch`` (a
topic that keeps failing gets a different approach or one question), ``overload`` (optional work
waits), ``priority`` (worry lifts owed duty, curiosity lifts research) and ``satiation``
(optional owner nudges wait after dismissals or recent success). Affect only ever lowers the
priority of discretionary work and never touches owed obligations or authority. The tone line
is rendered calmly from the state alone. Contacts' turns do not move the agent's affect.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from protagine.initiatives.models import MIND_ACTIVE_STATUSES

from .audit import NOTICE_TYPES
from .drives import DUTY_DOMAINS, _obligor, _utc, slug
from protagine.util.temporal import now_utc

logger = logging.getLogger(__name__)

CAP = 0.7                      # architecture 4.3: levels stay calm
SWITCH_AT = 0.5                # frustration at which a topic switches strategy
SWITCH_FAILURES = 2            # ... given this many failed attempts since the topic's last success (both sources)
OVERLOAD_AT = 0.6              # load at which optional work waits
RENDER_FLOOR = 0.05            # below this a level is not rendered and a frustration row is pruned
SATIATED_DISMISSED, SATIATED_SATISFACTION = 0.4, 0.5
SECTION_CHARS, TONE_CHARS = 360, 160
CONSUMERS = ("strategy_switch", "overload", "priority", "satiation")
DISCRETIONARY_PRIORITY = 0.5   # a candidate below this priority is optional (the extractor's "nice to have")
OWED_PRIORITY = 50             # the same line on the commitment store's 0-100 scale

WINDOW = timedelta(days=7)
MISS_WINDOW = timedelta(days=1)
NOVEL_WINDOW, NOVEL_MAX = timedelta(hours=12), 50
NEAR, DUE_SOON, UNDATED_RECENT = timedelta(hours=48), timedelta(hours=24), timedelta(hours=24)
STALE_VIEW = timedelta(minutes=10)   # how long the consumers keep the last full view while a source is unreadable

PREFIX = "affect."
FRUSTRATION = "affect.frustration:"
APPLIED = "affect.applied"
LEVEL_KEYS = {"worry": "affect.worry", "curiosity": "affect.curiosity", "satisfaction": "affect.satisfaction",
              "dismissed": "affect.dismissed"}
HALF_LIVES = {"frustration": 86400.0, "worry": 21600.0, "curiosity": 43200.0, "satisfaction": 43200.0,
              "dismissed": 86400.0}

# The rule table (architecture 4.3; the failure increment is 0.3, not 0.25, so two recent failures
# stay at the switch threshold for hours instead of dropping under it the first second: M6 plan R1).
FAILURE, CORRECTION, SUCCESS, MISS, NOVEL, DISMISSAL = 0.3, 0.2, 0.3, 0.2, 0.1, 0.25
DEADLINE_STEP, DEADLINE_CAP = 0.1, 0.3
NOVEL_WORDS, NOVEL_CAP = 3, 0.3   # a novel topic has 3+ content words; novelty alone never lifts curiosity above 0.3
APPRAISAL_INTENSITY = {"low": 0.1, "moderate": 0.2}
APPRAISAL_TARGET = {"frustration": "frustration", "interest": "curiosity", "satisfaction": "satisfaction"}
SUCCESS_KINDS = frozenset({"succeeded", "verified", "useful", "resolved"})
FRUSTRATING = frozenset({"failed", "corrected", "appraisal"})   # causes that keep a frustration row's topic
OWNER_EVENTS = frozenset({"failed", "succeeded", "dismissed", "corrected", "appraisal", "resolved"})

BANDS = ((0.2, "a little"), (0.45, "somewhat"), (math.inf, "quite"))
WORDS = {"worry": "uneasy", "curiosity": "curious", "satisfaction": "content"}
INTENSE = frozenset({"very", "extremely", "desperate", "desperately", "furious", "panic", "panicked", "panicking",
                     "terrified", "urgent", "urgently"})
STOPWORDS = frozenset({"the", "and", "for", "with", "from", "about", "into", "onto", "that", "this", "these",
                       "those", "its", "our", "your", "their", "was", "were", "are", "has", "have", "had", "not",
                       "but", "all", "any", "out", "off", "via", "using", "per"})
NOTE_PREFIX = "Prior attempts at "
# Katakana and CJK ideographs: Chinese and Japanese are written without spaces between words.
IDEOGRAPHIC = re.compile(r"[\u30a0-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


# -- small pure helpers ------------------------------------------------------------------------

def topic_words(text: Any) -> frozenset:
    """Casefolded words of 3+ letters or digits in any script, a few stopwords dropped, a plural ``s`` dropped."""
    words = set()
    for word in re.findall(r"[^\W_]{3,}", str(text or "").casefold()):
        if word in STOPWORDS:
            continue
        words.add(word[:-1] if len(word) >= 5 and word.endswith("s") else word)
    return frozenset(words)


def content_size(text: Any) -> int:
    """How much a text is about: its content words; a run of Chinese or Japanese, written without spaces,
    counts one per two ideographs or katakana (kana endings and particles are not content)."""
    size = 0
    for word in topic_words(text):
        dense = len(IDEOGRAPHIC.findall(word))
        size += dense // 2 if dense else 1
    return size


def topic_key(topic: Any) -> str:
    """The key of a topic's frustration row: its slug, with a digest when the topic is not plain ASCII (so
    topics in other scripts never share one row)."""
    text = " ".join(str(topic or "").split())
    if text.isascii():
        return slug(text)
    digest = hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:10]
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]
    return f"{base}-{digest}" if base else f"t-{digest}"


def topic_matches(a: Any, b: Any) -> bool:
    """Whether two topic phrases name the same thing: their word sets (``topic_words``) overlap by at
    least 0.6 of the smaller one, and by two words unless both are that short (one shared word is too
    little to tie "the export" to "export the photo album")."""
    left, right = topic_words(a), topic_words(b)
    if not left or not right:
        first, second = " ".join(str(a or "").split()).casefold(), " ".join(str(b or "").split()).casefold()
        return bool(first) and first == second
    shared = len(left & right)
    return shared >= 0.6 * min(len(left), len(right)) and (shared >= 2 or max(len(left), len(right)) <= 2)


def plan_hash(body: Any) -> str:
    """The signature of a task plan: its body without the strategy-switch note, whitespace collapsed."""
    lines = [line for line in str(body or "").splitlines() if not line.lstrip().startswith(NOTE_PREFIX)]
    return hashlib.sha256(" ".join(" ".join(lines).split()).encode("utf-8")).hexdigest()[:16]


def discretionary(candidate: Any) -> bool:
    """Work the agent may hold without anyone being owed it: recurring self-chosen work, curiosity and
    social outreach, and anything below the owed priority. The step of an adopted goal is owed whatever
    its drive (rank.py), and an unreadable priority counts as owed."""
    if getattr(candidate, "parent_goal_id", None):
        return False
    if getattr(candidate, "dedup_base", None) is not None:
        return True
    if str(getattr(candidate, "drive", "") or "") in {"curiosity", "social"}:
        return True
    try:
        return float(getattr(candidate, "priority", DISCRETIONARY_PRIORITY)) < DISCRETIONARY_PRIORITY
    except (TypeError, ValueError):
        return False


def open_work_ask(row: Any) -> bool:
    """An open ask the load counts: one about work the owner must decide (kind task or goal). Owner
    notices, check-ins and link questions waiting for a word, and contradiction questions, are the mind's
    reporting; counting them would let the asks about check-ins postpone the check-ins (integration map
    X5)."""
    return (getattr(row, "status", None) == "asked" and getattr(row, "kind", None) in {"task", "goal"}
            and getattr(row, "type", None) not in NOTICE_TYPES)


def postponable(candidate: Any) -> bool:
    """What overload postpones: curiosity and social work, and optional messages; never the step of an
    adopted goal (owed to the goal, whatever its drive)."""
    if getattr(candidate, "parent_goal_id", None):
        return False
    drive = str(getattr(candidate, "drive", "") or "")
    if drive in {"curiosity", "social"}:
        return True
    return getattr(candidate, "kind", "") == "message" and discretionary(candidate)


def band(level: float) -> str:
    return next(word for limit, word in BANDS if level < limit)


def _calm(text: str) -> str:
    return " ".join(word for word in str(text).split() if word.casefold().strip(".,;:!?\"'") not in INTENSE)


def _listing(items: Sequence[str]) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _distinct(values: Iterable[str], limit: int) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))[:limit]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# -- data types (I-3) --------------------------------------------------------------------------

@dataclass(frozen=True)
class AffectEvent:
    kind: str      # failed|succeeded|corrected|dismissed|verified|useful|resolved|appraisal|miss|novel
    ref: str
    at: datetime
    topic: str = ""
    approach: str = ""
    dimension: str = ""
    intensity: str = ""
    turn_id: str = ""
    reason: str = ""
    body_hash: str = ""

    def cause(self) -> str:
        return f"{self.kind} {self.ref}" + (f" via {self.approach}" if self.approach else "")


@dataclass(frozen=True)
class Obligation:
    id: str
    description: str
    due_at: Optional[datetime]
    started: bool


@dataclass(frozen=True)
class AffectInputs:
    """The one snapshot both the state and the rules read each tick."""

    now: datetime
    owner_id: Optional[str]
    events: Tuple[AffectEvent, ...] = ()      # the 7-day window, oldest first
    obligations: Tuple[Obligation, ...] = ()  # near-term, owed by the owner or the assistant, priority >= 50
    due_soon: Tuple[Obligation, ...] = ()     # due in (now, now + 24 h], not started
    running: int = 0
    cap: int = 0
    asks: int = 0
    unread: Tuple[str, ...] = ()               # sources whose read failed this tick (their events are missing)


@dataclass(frozen=True)
class Frustration:
    topic: str
    key: str
    level: Optional[float]                    # None when a rule produced it
    failures: int                             # failures since the last success-type event on the topic
    approaches: Tuple[str, ...] = ()
    pitfalls: Tuple[str, ...] = ()
    body_hashes: frozenset = frozenset()
    causes: Tuple[str, ...] = ()

    def note(self) -> str:
        if self.failures <= 0:
            failed = "have not worked"
        else:
            failed = "failed once" if self.failures == 1 else f"failed {self.failures} times"
        using = f" using {_listing(self.approaches)}" if self.approaches else ""
        return f"{NOTE_PREFIX}{self.topic} {failed}{using}; choose a different approach or ask one question."


@dataclass(frozen=True)
class AffectView:
    """What the consumers read, each consumer's fields from its routed source."""

    route: Mapping[str, str]
    owner_id: Optional[str] = None
    frustrations: Tuple[Frustration, ...] = ()                                  # strategy_switch
    tried: Tuple[Frustration, ...] = ()        # the failure record, whatever the level: the identical-plan refusal
    overloaded: bool = False
    load: float = 0.0
    obligations: Tuple[Obligation, ...] = ()                                    # overload
    worry: float = 0.0
    curiosity: float = 0.0
    due_soon: Tuple[Obligation, ...] = ()                                       # priority
    satiated: bool = False
    boost: float = 0.0
    dismissals: int = 0                                                         # satiation
    line: str = ""                                                              # tone, the state only

    def failing(self, text: Any) -> Optional[Frustration]:
        return next((item for item in self.frustrations if topic_matches(item.topic, text)), None)

    def score_factor(self, candidate: Any) -> float:
        """Worry lifts owed duty (1 + 0.5 x worry); curiosity lifts the curiosity drive."""
        drive = str(getattr(candidate, "drive", "") or "")
        if drive == "duty" and not discretionary(candidate):
            return 1.0 + 0.5 * self.worry
        if drive == "curiosity":
            return 1.0 + self.curiosity
        return 1.0

    def threshold_factor(self, candidate: Any) -> float:
        """Overload postpones (an infinite threshold); satiation raises the bar for optional owner nudges."""
        if self.overloaded and postponable(candidate):
            return math.inf
        if (self.satiated and getattr(candidate, "kind", "") == "message" and self.owner_id
                and getattr(candidate, "recipient", None) == self.owner_id and discretionary(candidate)):
            return 1.0 + self.boost
        return 1.0

    def notes(self) -> List[str]:
        """The consumers' notes, most important first: switch (at most 2), due soon, overload, satiation."""
        lines = [item.note() for item in self.frustrations[:2]]
        if self.due_soon:
            items = "; ".join(f"{item.description} (due {item.due_at.strftime('%H:%M %Z')})"
                              if item.due_at else item.description for item in self.due_soon[:2])
            lines.append(f"Due soon and not started: {items}.")
        if self.overloaded:
            count = len(self.obligations)
            listed = "; ".join(item.description for item in self.obligations[:3])
            what = (f"{count} open obligation{'s' if count != 1 else ''} ({listed}); " if count else "")
            lines.append(f"Stretched: {what}optional work waits and replies stay brief.")
        if self.satiated:
            if self.dismissals:
                were = "was" if self.dismissals == 1 else "were"
                lines.append(f"Holding back optional nudges: {self.dismissals} {were} waved off recently.")
            else:
                lines.append("Holding back optional nudges for now.")
        return lines


FIELDS = {"strategy_switch": ("frustrations", "tried"), "overload": ("overloaded", "load", "obligations"),
          "priority": ("worry", "curiosity", "due_soon"), "satiation": ("satiated", "boost", "dismissals")}


def compose(state_view: Optional[AffectView], rules_view: Optional[AffectView], route: Mapping[str, str], *,
            owner_id: Optional[str] = None) -> AffectView:
    """Each consumer's fields from its routed source; the tone line from the state alone."""
    values: Dict[str, Any] = {}
    for consumer, names in FIELDS.items():
        source = rules_view if route.get(consumer) == "rules" else state_view
        if source is not None:
            values.update({name: getattr(source, name) for name in names})
    return AffectView(route=dict(route), owner_id=owner_id, line=state_view.line if state_view else "", **values)


# -- readings shared by the state and the rules --------------------------------------------------

def recent_failures(events: Sequence[AffectEvent], topic: str, *,
                    since: Optional[datetime] = None) -> List[AffectEvent]:
    """The ``failed`` events on a topic after its latest success-type event (and after ``since``)."""
    last = max((e.at for e in events if e.kind in SUCCESS_KINDS and topic_matches(topic, e.topic)), default=None)
    return [e for e in events if e.kind == "failed" and topic_matches(topic, e.topic)
            and (last is None or e.at > last) and (since is None or e.at >= since)]


def frustration(topic: str, key: str, level: Optional[float], failures: Sequence[AffectEvent],
                causes: Iterable[str] = ()) -> Frustration:
    return Frustration(topic=topic, key=key, level=level, failures=len(failures),
                       approaches=_distinct((e.approach for e in failures), 3),
                       pitfalls=_distinct((e.reason for e in failures), 3),
                       body_hashes=frozenset(e.body_hash for e in failures if e.body_hash), causes=tuple(causes))


def failure_record(events: Sequence[AffectEvent], *, since: Optional[datetime] = None,
                   prefix: str = "record:") -> List[Frustration]:
    """Every topic with ``SWITCH_FAILURES`` or more failed attempts since its last success (and after
    ``since``), with the plans that failed: what the identical-plan refusal reads, whatever the level."""
    topics: List[str] = []
    for event in events:
        if event.kind == "failed" and event.topic and (since is None or event.at >= since) \
                and not any(topic_matches(topic, event.topic) for topic in topics):
            topics.append(event.topic)
    record = []
    for topic in topics:
        failures = recent_failures(events, topic, since=since)
        if len(failures) >= SWITCH_FAILURES:
            record.append(frustration(topic, prefix + topic_key(topic), None, failures,
                                      [event.cause() for event in failures][-5:]))
    return record


def failures_last_hour(inputs: AffectInputs) -> int:
    since = inputs.now - timedelta(hours=1)
    return sum(1 for e in inputs.events if e.kind == "failed" and e.at >= since)


def load_of(inputs: AffectInputs) -> float:
    """``min(1, 0.3 x running/cap + 0.2 x near obligations + 0.1 x failures in the last hour + 0.1 x asks)``."""
    busy = inputs.running / inputs.cap if inputs.cap > 0 else float(inputs.running > 0)
    value = 0.3 * busy + 0.2 * len(inputs.obligations) + 0.1 * failures_last_hour(inputs) + 0.1 * inputs.asks
    return round(min(1.0, value), 3)


def dismissals_of(inputs: AffectInputs, window: timedelta = WINDOW) -> int:
    since = inputs.now - window
    return sum(1 for e in inputs.events if e.kind == "dismissed" and e.at >= since)


def effects(event: AffectEvent) -> List[Tuple[str, str, float]]:
    """The rule table: ``(dimension, "add" | "halve", amount)`` for one event."""
    kind = event.kind
    if kind == "failed":
        return [("frustration", "add", FAILURE)]
    if kind == "corrected":
        return [("frustration", "add", CORRECTION)]
    if kind in {"succeeded", "resolved"}:
        return [("frustration", "halve", 0.5)]
    if kind in {"verified", "useful"}:
        return [("satisfaction", "add", SUCCESS), ("frustration", "halve", 0.5)]
    if kind == "miss":
        return [("worry" if event.dimension == "duty" else "curiosity", "add", MISS)]
    if kind == "novel":
        return [("curiosity", "add", NOVEL)]
    if kind == "dismissed":
        return [("dismissed", "add", DISMISSAL)]
    if kind == "appraisal":
        if event.dimension == "annoyance":
            return [("frustration", "add", CORRECTION)]
        target, amount = APPRAISAL_TARGET.get(event.dimension), APPRAISAL_INTENSITY.get(event.intensity, 0.0)
        return [(target, "add", amount)] if target and amount else []
    return []


# -- the facade (I-1) ------------------------------------------------------------------------------

class Affect:
    """The mind's one entry point to affect; every method is safe to call from the tick."""

    def __init__(self, mind_state: Any, *, store: Any, commitments: Any = None, appraisals: Any = None,
                 expectations: Any = None, budgets: Any = None, owner_id: Optional[str] = None,
                 state_on: bool = True, rules_on: bool = False, tz: Optional[tzinfo] = timezone.utc,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        self.mind_state = mind_state
        self.store = store
        self.commitments = commitments
        self.appraisals = appraisals
        self.expectations = expectations
        self.budgets = budgets
        self.owner_id = owner_id or None
        # Two switches, three modes: with ``affect_rules`` on the rules replace the state, whatever
        # ``affect`` says, so no unmeasured mix of rule-driven decisions and a kept state exists.
        self.rules_on = bool(rules_on)
        self.state_on = bool(state_on) and not self.rules_on
        self.tz = tz or timezone.utc
        self.clock = clock or (lambda: now_utc())
        self._novel: Deque[AffectEvent] = deque(maxlen=NOVEL_MAX)
        self._inputs: Optional[AffectInputs] = None
        self._view: Optional[AffectView] = None
        self._updated_at: Optional[datetime] = None

    @property
    def active(self) -> bool:
        return self.state_on or self.rules_on

    def route(self) -> Dict[str, str]:
        """``affect_rules`` on: every consumer reads its rule; else the gate's ``RULE_CONSUMERS`` do."""
        if not self.active:
            return {}
        if self.rules_on:
            return {name: "rules" for name in CONSUMERS}
        from . import affect_rules
        return {name: "rules" if name in affect_rules.RULE_CONSUMERS else "state" for name in CONSUMERS}

    @property
    def source(self) -> str:
        values = set(self.route().values())
        return "off" if not values else values.pop() if len(values) == 1 else "mixed"

    # -- the snapshot -------------------------------------------------------------------------

    def gather(self, now: Optional[datetime] = None) -> AffectInputs:
        """One snapshot of every input; an unavailable store contributes nothing and is named in
        ``unread`` (a failed read is not an empty one)."""
        now = _aware(now or self.clock())
        unread: List[str] = []

        def read(what: str, reader: Callable[[], Any]) -> Any:
            try:
                return reader()
            except Exception as error:
                logger.warning("affect input %s unavailable (%s)", what, type(error).__name__)
                unread.append(what)
                return []
        active_rows = read("active intentions", lambda: self.store.intentions(
            status=list(MIND_ACTIVE_STATUSES), limit=500))
        events = [*read("appraisal events", lambda: self._owner_events(now)),
                  *read("intention events", lambda: self._intention_events(now)),
                  *read("expectation misses", lambda: self._misses(now)),
                  *(event for event in self._novel if event.at >= now - NOVEL_WINDOW)]
        unique: Dict[str, AffectEvent] = {}
        for event in sorted(events, key=lambda item: (item.at, item.ref)):
            unique.setdefault(event.ref, event)
        started = {str(row.source_id) for row in active_rows if row.source_type == "commitment" and row.source_id}
        obligations = read("commitments", lambda: self._obligations(now, started))
        due_soon = [item for item in obligations
                    if item.due_at is not None and now < item.due_at <= now + DUE_SOON and not item.started]
        try:
            cap = int(getattr(self.budgets, "concurrent_tasks", 2) if self.budgets is not None else 2)
        except (TypeError, ValueError):
            cap = 2
        return AffectInputs(now=now, owner_id=self.owner_id, events=tuple(unique.values()),
                            obligations=tuple(obligations), due_soon=tuple(due_soon),
                            running=sum(1 for row in active_rows if row.status == "dispatched"), cap=max(0, cap),
                            asks=sum(1 for row in active_rows if open_work_ask(row)), unread=tuple(unread))

    def _owner_events(self, now: datetime) -> List[AffectEvent]:
        reader = getattr(self.appraisals, "affect_events", None)
        if reader is None or not self.owner_id:
            return []
        events = []
        for row in reader(since=(now - WINDOW).timestamp(), limit=1000) or []:
            kind, ref = str(row.get("kind") or ""), str(row.get("ref") or "")
            if kind not in OWNER_EVENTS or not ref:
                continue
            events.append(AffectEvent(
                kind=kind, ref=ref, at=_utc(row.get("occurred_at")) or _utc(row.get("created_at")) or now,
                topic=" ".join(str(row.get("topic") or "").split())[:120],
                approach=" ".join(str(row.get("approach") or "").split())[:80],
                dimension=str(row.get("dimension") or ""), intensity=str(row.get("intensity") or ""),
                turn_id=str(row.get("turn_id") or "")))
        return events

    def _intention_events(self, now: datetime) -> List[AffectEvent]:
        events = []
        for row in self.store.intentions(since=now - WINDOW, limit=1000):
            if row.kind == "note" or row.type in NOTICE_TYPES:
                continue
            context = row.context if isinstance(row.context, dict) else {}
            topic = str(context.get("topic") or context.get("concern") or row.description or "")[:120]
            base, created = f"intention:{row.id}", _utc(row.created_at) or now
            approach = " ".join(str(context.get("approach") or "").split())[:80]
            plan = context.get("plan_body") or context.get("body")
            body_hash = plan_hash(plan) if row.kind == "task" and plan else ""
            # One failed attempt per task: ``blocked`` is not terminal and the same row can fail later,
            # so both reports share one reference and a blocked-then-failed task is applied once.
            if row.outcome == "failed":
                events.append(AffectEvent("failed", f"{base}:failed", _utc(row.failed_at) or created, topic,
                                          approach, reason=str(row.failed_reason or "")[:160], body_hash=body_hash))
            elif row.outcome == "blocked":
                events.append(AffectEvent("failed", f"{base}:failed", _utc(row.assigned_at) or created, topic,
                                          approach, reason=str(row.result or "")[:160], body_hash=body_hash))
            metadata = row.result_metadata if isinstance(row.result_metadata, dict) else {}
            check = metadata.get("check") if isinstance(metadata.get("check"), dict) else {}
            finished = _utc(row.completed_at) or _utc(row.cancelled_at) or _utc(row.failed_at) or created
            passed = row.outcome == "done" and check.get("passed") is True
            if passed:   # one success per row: an owner's later "useful" on it is the same success
                events.append(AffectEvent("verified", f"{base}:verified", finished, topic))
            verdict = row.verdict
            if verdict == "useful" and not passed:
                events.append(AffectEvent("useful", f"{base}:useful", finished, topic))
            elif verdict in {"wrong", "not_useful"}:
                events.append(AffectEvent("corrected", f"{base}:{verdict}", finished, topic))
            elif self._waved_off(row):
                events.append(AffectEvent("dismissed", f"{base}:{verdict}", finished, topic))
        return events

    def _waved_off(self, row: Any) -> bool:
        """An owner's dismissal: a ``dismissed`` or ``ignored`` verdict on owner-facing work. A message to a
        contact is not the owner's, and nothing that expired is a dismissal: approved work that expired
        undispatched was never seen, and an ask that lapsed unanswered (a check-in to confirm, a link to
        confirm, a contradiction question) is silence, not a wave-off (integration map X5)."""
        if row.verdict not in {"dismissed", "ignored"}:
            return False
        if row.kind == "message" and row.entity_id != self.owner_id:
            return False
        return not (row.verdict == "ignored" and row.outcome == "expired")

    def _misses(self, now: datetime) -> List[AffectEvent]:
        reader = getattr(getattr(self.expectations, "store", None), "resolved_since", None)
        if reader is None:
            return []
        events = []
        for row in reader((now - MISS_WINDOW).timestamp()) or []:
            ident, subject = getattr(row, "prediction_id", None), str(getattr(row, "subject", "") or "")
            if getattr(row, "outcome", None) != "miss" or not ident or subject.startswith("intention:"):
                continue
            domain = str(getattr(row, "domain", "") or "")
            events.append(AffectEvent("miss", f"expectation:{ident}", _utc(getattr(row, "resolved_at", None)) or now,
                                      str(getattr(row, "expectation", "") or "")[:120],
                                      dimension="duty" if domain in DUTY_DOMAINS else "knowledge"))
        return events

    def _obligations(self, now: datetime, started: set) -> List[Obligation]:
        if self.commitments is None:
            return []
        found = []
        for row in self.commitments.list(status=["pending", "overdue"], limit=500).get("commitments", []):
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            # Who owes it, as duty reads it: a contact's own promise (stated, or captured on their turn
            # without an obligor) is theirs, not the owner's load.
            if _obligor(row, metadata, str(row.get("person_id") or "") or None, self.owner_id) not in {"owner",
                                                                                                   "assistant"}:
                continue
            try:
                priority = OWED_PRIORITY if row.get("priority") is None else int(row["priority"])
            except (TypeError, ValueError):
                priority = OWED_PRIORITY
            if priority < OWED_PRIORITY:
                continue
            due = _utc(row.get("due_at"))
            if due is not None and due > now + NEAR:
                continue
            if due is None:
                made = _utc(row.get("made_at"))
                if made is None or now - made > UNDATED_RECENT:
                    continue
            ident = str(row["id"])
            found.append(Obligation(id=ident, description=" ".join(str(row.get("description") or "").split())[:160],
                                    due_at=due.astimezone(self.tz) if due else None, started=ident in started))
        far = datetime.max.replace(tzinfo=timezone.utc)
        return sorted(found, key=lambda item: (item.due_at or far, item.id))

    # -- the tick ---------------------------------------------------------------------------------

    def update(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Gather, fold new events into the state, apply the deadline rule, prune, compose the view."""
        if not self.active:
            return {"source": None}
        try:
            now = _aware(now or self.clock())
            inputs = self.gather(now)
            applied = self._apply(inputs) if self.state_on else 0
            # A failed read is not an empty one: while a source is unreadable the consumers keep the last
            # full view, for at most STALE_VIEW; then what can be read is better than a stale picture.
            keep = (inputs.unread and self._view is not None and self._updated_at is not None
                    and now - self._updated_at <= STALE_VIEW)
            view = self._view if keep else self._compose(inputs)
        except Exception as error:
            logger.warning("affect update failed (%s)", type(error).__name__)
            return {"source": self.source, "error": type(error).__name__}
        if not keep:
            self._inputs, self._view, self._updated_at = inputs, view, now
        result = {"source": self.source, "applied": applied, "load": view.load, "overloaded": view.overloaded,
                  "satiated": view.satiated, "switch": [item.topic for item in view.frustrations]}
        return {**result, "unread": list(inputs.unread)} if inputs.unread else result

    # -- the state ----------------------------------------------------------------------------------

    def _frustration_rows(self) -> List[Dict[str, Any]]:
        """The frustration rows, most frustrated first."""
        return sorted(self.mind_state.items(FRUSTRATION), key=lambda row: (-float(row.get("level") or 0.0), row["key"]))

    def _level(self, key: str) -> float:
        return float((self.mind_state.get(key) or {}).get("level") or 0.0)

    def _decay(self, now: datetime) -> None:
        """The affect rows relax toward 0 up to ``now`` (the tick's own decay does the same at the same time)."""
        for row in self.mind_state.items(PREFIX):
            half, level = float(row.get("half_life_s") or 0.0), float(row.get("level") or 0.0)
            updated = _utc(row.get("updated_at"))
            if half <= 0 or level <= 0 or updated is None or now <= updated:
                continue
            relaxed = level * 0.5 ** ((now - updated).total_seconds() / half)
            self.mind_state.set(row["key"], level=relaxed if relaxed >= 0.005 else 0.0, now=now)

    def _frustration_key(self, topic: str) -> str:
        for row in self.mind_state.items(FRUSTRATION):
            if topic_matches(row.get("text") or row["key"][len(FRUSTRATION):], topic):
                return row["key"]
        return FRUSTRATION + topic_key(topic)

    def _key(self, dimension: str, event: AffectEvent) -> Optional[str]:
        if dimension == "frustration":
            return self._frustration_key(event.topic) if event.topic else None
        return LEVEL_KEYS[dimension]

    def _add(self, key: str, dimension: str, amount: float, cause: str, now: datetime, topic: str = "") -> None:
        current = self.mind_state.get(key)
        level = min(CAP, max(0.0, float((current or {}).get("level") or 0.0) + amount))
        self.mind_state.set(key, level=level, half_life_s=HALF_LIVES[dimension], causes=[cause], now=now,
                            text=topic if current is None and topic else None)

    def _apply(self, inputs: AffectInputs) -> int:
        """Fold every event not applied yet, oldest first, as if at its own time; then the deadline
        rule and the prune. An outcome and an appraisal record of one turn on one key count once
        (the larger), while every reported occurrence counts.

        ``affect.applied`` maps each applied reference to the time it stays applied from; it is
        dropped only once that is older than the window, never because a read missed it, so a
        source that fails to read for a tick is not applied again when it reads."""
        now = inputs.now
        self._decay(now)
        entry = self.mind_state.get(APPLIED) or {}
        try:
            applied = json.loads(entry.get("text") or "{}")
        except (TypeError, ValueError):
            applied = {}
        applied = applied if isinstance(applied, dict) else {}
        window = {event.ref for event in inputs.events}
        # (turn, key) -> [sum of reported occurrences, largest appraisal record]
        tallies: Dict[Tuple[str, str], List[float]] = {}

        def tally(event: AffectEvent, key: str, amount: float) -> float:
            """The turn's contribution to ``key`` after this event minus before it."""
            sums = tallies.setdefault((event.turn_id, key), [0.0, 0.0])
            before = max(sums)
            if event.kind == "appraisal":
                sums[1] = max(sums[1], amount)
            else:
                sums[0] += amount
            return max(sums) - before

        fresh = [event for event in inputs.events if event.ref not in applied]
        # A reference is kept as long as a read can still return it: stamped when first applied (or at
        # its own time, if later), dropped once the stamp is older than the window.
        horizon = (now - WINDOW).timestamp()
        kept = {ref: stamp for ref, stamp in applied.items() if isinstance(stamp, (int, float)) and stamp >= horizon}
        kept.update({event.ref: round(max(now, event.at).timestamp(), 3) for event in fresh})
        if kept != applied:
            # Recorded first: an update that fails halfway leaves an event applied at most once.
            self.mind_state.set(APPLIED, text=json.dumps(kept, sort_keys=True), now=now)
        turns = {event.turn_id for event in fresh if event.turn_id}
        for event in inputs.events:
            if event.ref in applied and event.turn_id in turns:
                for dimension, op, amount in effects(event):
                    key = self._key(dimension, event)
                    if op == "add" and key:
                        tally(event, key, amount)
        for event in fresh:
            age = max(0.0, (now - event.at).total_seconds())
            for dimension, op, amount in effects(event):
                if op == "halve":
                    # A success calms what the failures since the last success built up, once: a second
                    # report of it (the repair receipt of the same statement, the owner's "it worked" about
                    # a verified task) finds the row's latest cause already a success and changes nothing.
                    for row in self.mind_state.items(FRUSTRATION):
                        causes = row.get("causes") or []
                        calmed = bool(causes) and str(causes[-1]).split(" ")[0] in SUCCESS_KINDS
                        if event.topic and not calmed and topic_matches(row.get("text") or "", event.topic):
                            self.mind_state.set(row["key"], level=float(row.get("level") or 0.0) * amount,
                                                causes=[event.cause()], now=now)
                    continue
                key = self._key(dimension, event)
                if key is None:
                    continue
                delta = tally(event, key, amount) if event.turn_id else amount
                delta *= 0.5 ** (age / HALF_LIVES[dimension])
                if event.kind == "novel":
                    delta = min(delta, NOVEL_CAP - self._level(key))
                if delta > 0:
                    self._add(key, dimension, delta, event.cause(), now,
                              topic=event.topic if dimension == "frustration" else "")
        # Without the active intentions a started obligation looks unstarted: no deadline worry this tick.
        for item in inputs.due_soon if "active intentions" not in inputs.unread else ():
            worry = self._level(LEVEL_KEYS["worry"])
            if worry >= DEADLINE_CAP:
                break
            self.mind_state.set(LEVEL_KEYS["worry"], level=min(DEADLINE_CAP, worry + DEADLINE_STEP),
                                half_life_s=HALF_LIVES["worry"], causes=[f"due_soon commitment:{item.id}"], now=now)
        for row in self.mind_state.items(FRUSTRATION):
            evidence = {parts[1] for parts in (str(cause).split(" ") for cause in row.get("causes") or [])
                        if len(parts) >= 2 and parts[0] in FRUSTRATING}
            # Erased or aged-out evidence leaves no topic text behind; a source that failed to read is
            # neither, so a tick with an unread source prunes nothing by evidence.
            gone = not inputs.unread and not evidence & window
            if float(row.get("level") or 0.0) < RENDER_FLOOR or gone:
                self.mind_state.delete(row["key"])
        return len(fresh)

    def _state_view(self, inputs: AffectInputs) -> AffectView:
        # The level is the feeling; the switch also needs SWITCH_FAILURES failed attempts since the topic's last
        # success, so a success followed by one miss, or corrections alone, never abandon an approach.
        frustrations = []
        for row in self._frustration_rows():
            failures = recent_failures(inputs.events, row.get("text") or "")
            if float(row.get("level") or 0.0) >= SWITCH_AT and len(failures) >= SWITCH_FAILURES:
                frustrations.append(frustration(row.get("text") or row["key"][len(FRUSTRATION):], row["key"],
                                                round(float(row["level"]), 3), failures, row.get("causes") or []))
        levels = {name: self._level(key) for name, key in LEVEL_KEYS.items()}
        load = load_of(inputs)
        satiated = levels["dismissed"] >= SATIATED_DISMISSED or levels["satisfaction"] >= SATIATED_SATISFACTION
        return AffectView(
            route={name: "state" for name in CONSUMERS}, owner_id=self.owner_id, frustrations=tuple(frustrations),
            tried=tuple(failure_record(inputs.events)),
            overloaded=load >= OVERLOAD_AT, load=load, obligations=inputs.obligations,
            worry=round(levels["worry"], 3), curiosity=round(levels["curiosity"], 3),
            due_soon=inputs.due_soon if levels["worry"] >= RENDER_FLOOR else (),
            satiated=satiated,
            boost=round(min(CAP, max(levels["dismissed"], levels["satisfaction"])), 3) if satiated else 0.0,
            dismissals=dismissals_of(inputs), line=self._tone(levels))

    def _tone(self, levels: Mapping[str, float]) -> str:
        """One calm line about the agent's own state, e.g. ``Mood: somewhat frustrated about X; a little uneasy.``"""
        parts: List[Tuple[float, int, str]] = []
        for row in self.mind_state.items(FRUSTRATION):
            level = float(row.get("level") or 0.0)
            topic = _calm(row.get("text") or "")[:60].strip()
            if level >= RENDER_FLOOR and topic:
                parts.append((level, 0, f"{band(level)} frustrated about {topic}"))
        for name, word in WORDS.items():
            if levels[name] >= RENDER_FLOOR:
                parts.append((levels[name], 1, f"{band(levels[name])} {word}"))
        chosen = [text for _, _, text in sorted(parts, key=lambda item: (-item[0], item[1], item[2]))[:3]]
        while chosen and len("Mood: " + "; ".join(chosen) + ".") > TONE_CHARS:
            chosen.pop()
        return "Mood: " + "; ".join(chosen) + "." if chosen else ""

    def _compose(self, inputs: AffectInputs) -> AffectView:
        route = self.route()
        state_view = self._state_view(inputs) if self.state_on else None
        rules_view = None
        if "rules" in route.values():
            from . import affect_rules
            rules_view = affect_rules.view(inputs)
        return compose(state_view, rules_view, route, owner_id=self.owner_id)

    # -- the consumers' reads -------------------------------------------------------------------------

    def view(self) -> Optional[AffectView]:
        return self._view if self.active else None

    def failing(self, text: Any) -> Optional[Frustration]:
        try:
            view = self.view()
            return view.failing(text) if view is not None else None
        except Exception as error:
            logger.warning("affect failing() failed (%s)", type(error).__name__)
            return None

    def note_for(self, text: Any) -> str:
        found = self.failing(text)
        return found.note() if found is not None else ""

    def section_lines(self, limit: int = SECTION_CHARS) -> List[str]:
        """The consumer notes for the decision context; whole lines drop from the end to fit ``limit``. The
        tone line is self-report only (``state``): a mood in the decision context had no decision value."""
        try:
            view = self.view()
            if view is None or limit < 2:
                return []
            lines = list(view.notes())
            while len(lines) > 1 and len("\n".join(lines)) > limit:
                lines.pop()
            if lines and len(lines[0]) > limit:
                lines = [lines[0][: max(0, limit - 1)].rstrip() + "…"]
            return lines
        except Exception as error:
            logger.warning("affect section failed (%s)", type(error).__name__)
            return []

    def note_novel_topic(self, text: Any, at: Optional[datetime] = None) -> None:
        """An owner turn on a topic memory knew nothing about: a little curiosity at the next update.
        Small talk ("ok", "thanks", "yes K7F") names no topic: fewer than ``NOVEL_WORDS`` content words."""
        try:
            text = " ".join(str(text or "").split())
            if not self.state_on or content_size(text) < NOVEL_WORDS:
                return
            at = _aware(at or self.clock())
            ref = (f"novel:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}@"
                   f"{at.astimezone(timezone.utc).strftime('%Y%m%dT%H%MZ')}")
            if all(event.ref != ref for event in self._novel):
                self._novel.append(AffectEvent("novel", ref, at, topic=text[:80]))
        except Exception as error:
            logger.warning("novel topic not noted (%s)", type(error).__name__)

    # -- self-report (I-7) ----------------------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        """Levels with their cited causes, the load, the routing and what the consumers read now."""
        inputs, view = self._inputs, self.view()
        report: Dict[str, Any] = {
            "enabled": self.active, "source": self.source, "route": self.route(), "levels": {},
            "load": {"level": 0.0, "overloaded": False, "obligations": 0, "running": 0, "cap": 0,
                     "failures_last_hour": 0, "asks": 0},
            "satiated": False, "boost": 0.0, "due_soon": [], "switch": [], "notes": [], "line": "",
            "updated_at": self._updated_at.isoformat() if self._updated_at else None}
        if not self.active:
            return report
        try:
            if self.state_on:
                report["levels"] = self._levels(inputs)
            if inputs is not None and view is not None:
                report["load"] = {"level": view.load, "overloaded": view.overloaded,
                                  "obligations": len(inputs.obligations), "running": inputs.running,
                                  "cap": inputs.cap, "failures_last_hour": failures_last_hour(inputs),
                                  "asks": inputs.asks}
            if view is not None:
                report.update(
                    satiated=view.satiated, boost=view.boost, switch=[item.topic for item in view.frustrations],
                    due_soon=[{"id": item.id, "description": item.description,
                               "due_at": item.due_at.isoformat() if item.due_at else None} for item in view.due_soon],
                    notes=view.notes(), line=view.line)
        except Exception as error:
            logger.warning("affect state failed (%s)", type(error).__name__)
        return report

    def _levels(self, inputs: Optional[AffectInputs]) -> Dict[str, Any]:
        events = inputs.events if inputs is not None else ()
        frustrations = []
        for row in self._frustration_rows():
            level = float(row.get("level") or 0.0)
            if level < RENDER_FLOOR:
                continue
            failures = recent_failures(events, row.get("text") or "")
            frustrations.append({"topic": row.get("text"), "level": round(level, 3), "failures": len(failures),
                                 "approaches": list(_distinct((e.approach for e in failures), 3)),
                                 "causes": list(row.get("causes") or [])})
        levels: Dict[str, Any] = {"frustration": frustrations}
        for name, key in LEVEL_KEYS.items():
            entry = self.mind_state.get(key) or {}
            levels[name] = {"level": round(float(entry.get("level") or 0.0), 3),
                            "causes": list(entry.get("causes") or [])}
        return levels


__all__ = ["Affect", "AffectEvent", "AffectInputs", "AffectView", "CAP", "CONSUMERS", "DISCRETIONARY_PRIORITY",
           "Frustration", "OVERLOAD_AT", "Obligation", "RENDER_FLOOR", "SECTION_CHARS", "SWITCH_AT", "SWITCH_FAILURES",
           "compose", "content_size", "discretionary", "effects", "failure_record", "frustration", "load_of", "open_work_ask",
           "plan_hash", "postponable",
           "recent_failures", "topic_key", "topic_matches"]
