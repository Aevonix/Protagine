"""Authority: levels x action classes, the floor, the deny list, budgets and the breaker.

Architecture section 7. The decision is made here, in the sidecar, before
anything is queued; the worker profile's toolsets and the plugin guard enforce
it inside Hermes. Learning never writes to this module's inputs: the level,
the classes, the floor, the deny list and the budgets all come from the
owner's configuration.

``decide_table`` is the pure decision function the property tests enumerate.
``Authority`` binds it to the store for the budget counters and the breaker.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional
from protagine.util.temporal import now_utc

from .rank import OUTREACH_ANSWER, OUTREACH_TYPES, TASK_OUTCOME

LEVELS = ("off", "suggest", "standard", "trusted")
CLASSES = ("internal", "owner", "contact", "external", "floor")
DECISIONS = ("act", "ask", "drop", "defer")
MAY_CONTACT = ("never", "ask", "auto")
INTERNAL_SAFE_TOOLSETS = frozenset({"web", "file", "session_search", "memory", "todo"})

# The floor (architecture 7.3): four classes, matched conservatively on
# intention text, message text and, in mind-originated runs, tool arguments.
# The one floor since M2 (the trust ladder that also held it is gone); the plugin guard carries the same set.
FLOOR_PATTERNS: Dict[str, re.Pattern[str]] = {
    "money_movement": re.compile(
        r"\b(?:wire|transfer|send|move)\s+(?:\$|money|funds|payment)|"
        r"\b(?:purchase|buy|pay|spend|subscribe)\b.{0,30}\$|"
        r"\bpayment\s+(?:of|for)\b|\$\d{2,}", re.IGNORECASE),
    "irreversible_deletion": re.compile(
        r"\b(?:rm\s+-rf|drop\s+(?:table|database)|delete\s+permanently|"
        r"wipe|purge\s+all|force[- ]?push|push\s+--force(?:-with-lease)?|"
        r"delete\s+(?:the\s+)?(?:repo|repository|database|account)|"
        r"erase\s+(?:all|everything))\b",
        re.IGNORECASE),
    "credential_change": re.compile(
        r"\b(?:rotate|change|reset|revoke|create)\b.{0,40}\b(?:credential|"
        r"password|api[_ ]?key|secret|token|ssh[- ]?key|certificate)\b|"
        r"\bsecurity\s+settings?\b", re.IGNORECASE),
    "bulk_third_party_messaging": re.compile(
        r"\b(?:bulk|mass|broadcast|blast|everyone|all\s+contacts)\b.{0,40}"
        r"\b(?:message|text|email|sms|dm)\b|"
        r"\b(?:message|text|email|sms|dm)\b.{0,40}\b(?:bulk|mass|broadcast|"
        r"blast|everyone|all\s+contacts)\b", re.IGNORECASE),
}

ASK_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


def floor_class(text: str) -> Optional[str]:
    """The floor class this text falls into, if any."""
    value = text or ""
    for name, pattern in FLOOR_PATTERNS.items():
        if pattern.search(value):
            return name
    return None


def new_ask_code(taken: Iterable[str], *, rng: random.Random | None = None) -> str:
    """A short code the owner can type back; unique among the open asks."""
    rng = rng or random.SystemRandom()
    used = {code.upper() for code in taken}
    for _ in range(1000):
        code = "".join(rng.choice(ASK_ALPHABET) for _ in range(3))
        if code not in used:
            return code
    return "".join(rng.choice(ASK_ALPHABET) for _ in range(6))


def may_contact_of(contact: Any, *, owner_id: str | None) -> str:
    """``may_contact`` for a contact record or id (architecture 7.4).

    The owner is ``auto`` by identity; everyone else is what the
    ``contacts.may_contact`` column says, and ``ask`` when the record has no
    usable value. Nothing else (a tier, a legacy flag, familiarity) grants.
    """
    contact_id = contact if isinstance(contact, str) else getattr(contact, "contact_id", None)
    if contact_id is None and isinstance(contact, Mapping):
        contact_id = contact.get("contact_id")
    if owner_id and contact_id == owner_id:
        return "auto"
    if contact is None or isinstance(contact, str):
        return "ask"
    value = contact.get("may_contact") if isinstance(contact, Mapping) else getattr(contact, "may_contact", None)
    return str(value) if value in MAY_CONTACT else "ask"


def classify(*, kind: str, recipient: str | None, owner_id: str | None,
             toolsets: Iterable[str] = (), text: str = "") -> str:
    """The class of an intention from its kind and target (architecture 7.2)."""
    if floor_class(text):
        return "floor"
    if kind == "message":
        return "owner" if recipient and recipient == owner_id else "contact"
    if kind in {"task", "goal"}:
        extra = {str(name) for name in toolsets} - INTERNAL_SAFE_TOOLSETS
        if extra:
            return "external"
        if recipient and recipient != owner_id:
            return "contact"
        return "owner" if recipient == owner_id and recipient else "internal"
    return "internal"


def decide_table(*, level: str, cls: str, may_contact: str = "ask", floor: bool = False,
                 deny: bool = False, budget_exhausted: bool = False,
                 breaker_tripped: bool = False, enabled: bool = True, requested: bool = False) -> str:
    """The pure decision (architecture 7.2, 7.3, 7.5, 7.6), in precedence order:

    1. the off switch or ``off`` level: ``drop``
    2. the deny list: ``drop``
    3. the floor: ``ask``, and nothing raises it
    4. the level x class table, with ``may_contact`` for the contact class; at ``suggest`` a
       ``requested`` word to the owner (``REQUESTED_TYPES``) acts like internal work: the level
       holds the mind's initiative for the digest, not what the owner asked to be told
    5. a tripped breaker demotes an ``act`` one level, to ``ask``
    6. an exhausted budget defers an ``act``; it is not an error
    """
    if not enabled or level == "off":
        return "drop"
    if deny:
        return "drop"
    if floor or cls == "floor":
        return "ask"
    if level == "suggest":
        decision = "act" if cls == "internal" or (requested and cls == "owner") else "ask"   # digest only
    elif level == "standard":
        if cls in {"internal", "owner"}:
            decision = "act"
        elif cls == "contact":
            decision = {"auto": "act", "ask": "ask", "never": "drop"}[may_contact]
        else:
            decision = "ask"                                  # external
    elif level == "trusted":
        if cls == "contact":
            decision = {"auto": "act", "ask": "ask", "never": "drop"}[may_contact]
        else:
            decision = "act"
    else:
        raise ValueError(f"unknown autonomy level {level!r}")
    if decision == "act" and breaker_tripped:
        decision = "ask"
    if decision == "act" and budget_exhausted:
        decision = "defer"
    return decision


@dataclass
class Verdict:
    decision: str
    reason: str
    cls: str
    floor: Optional[str] = None
    notice: bool = True  # False for suggest-level asks: they wait for the digest

    def as_dict(self) -> Dict[str, Any]:
        return {"decision": self.decision, "reason": self.reason, "cls": self.cls,
                "floor": self.floor, "notice": self.notice}


# The research-shaped task types (the ones whose report is a finding, ``outcomes.FINDING_TYPES``): one
# worker run reads sources and writes them up, so it gets a longer run and a second attempt.
RESEARCH_TASK_BUDGET = {"max_runtime_s": 1800, "max_retries": 2}
DEFAULT_TASK_TYPES = {name: dict(RESEARCH_TASK_BUDGET)
                      for name in ("research", "question", "mastery_investigation", "goal_step", "outreach_followup")}


@dataclass
class Budgets:
    tasks_per_hour: int = 4
    concurrent_tasks: int = 2
    owner_messages_per_day: int = 3
    outreach_per_day: int = 3      # unprompted outreach to the owner in any 24 h (architecture 4.10)
    contact_messages_per_day: int = 5
    per_contact_cooldown_hours: float = 24
    llm_tokens_per_day: int = 200000
    learn_share: float = 0.25
    open_goals: int = 2
    goal_tasks: int = 4            # steps an agent-owned goal may spend
    goal_horizon_days: int = 7     # the longest horizon an adopted goal may have
    task_max_runtime_s: int = 600  # one worker run of a task type ``task_types`` does not name
    task_max_retries: int = 1
    # Per task type: ``{max_runtime_s, max_retries}``, either one falling back to the two above.
    task_types: Dict[str, Dict[str, int]] = field(
        default_factory=lambda: {name: dict(value) for name, value in DEFAULT_TASK_TYPES.items()})

    @classmethod
    def from_config(cls, value: Mapping[str, Any] | None) -> "Budgets":
        budgets = cls()
        for name, default in vars(budgets).items():
            raw = (value or {}).get(name, default)
            if name == "task_types":
                # Over the defaults, one type and one field at a time: overriding research's runtime
                # keeps its retries, and the other types keep theirs.
                merged = {kind: dict(entry) for kind, entry in default.items()}
                for kind, entry in (raw.items() if isinstance(raw, Mapping) else ()):
                    if not isinstance(entry, Mapping):
                        continue
                    target = merged.setdefault(str(kind), {})
                    for key in ("max_runtime_s", "max_retries"):
                        try:
                            if key in entry:
                                target[key] = int(entry[key])
                        except (TypeError, ValueError):
                            pass
                budgets.task_types = merged
                continue
            try:
                setattr(budgets, name, type(default)(raw))
            except (TypeError, ValueError):
                setattr(budgets, name, default)
        return budgets

    def for_task(self, task_type: str | None) -> tuple[int, int]:
        """``(max_runtime_s, max_retries)``: one worker run's limit and the retries after a failed run,
        for a task of this type."""
        entry = self.task_types.get(str(task_type or "")) or {}
        return (int(entry.get("max_runtime_s", self.task_max_runtime_s)),
                int(entry.get("max_retries", self.task_max_retries)))


@dataclass
class Breaker:
    failures: int = 3
    window_hours: float = 24
    demotion_hours: float = 72

    @classmethod
    def from_config(cls, value: Mapping[str, Any] | None) -> "Breaker":
        value = value or {}
        return cls(int(value.get("failures", 3)), float(value.get("window_hours", 24)),
                   float(value.get("demotion_hours", 72)))


@dataclass
class Policy:
    """The ``mind:`` section as the authority reads it."""

    level: str = "suggest"
    enabled: bool = True
    deny_tools: frozenset[str] = frozenset()
    deny_text: tuple[re.Pattern[str], ...] = ()
    deny_commands: tuple[str, ...] = ()
    worker_toolsets: tuple[str, ...] = ("web", "file", "session_search", "memory", "todo")
    budgets: Budgets = field(default_factory=Budgets)
    breaker: Breaker = field(default_factory=Breaker)
    ask_expires_hours: float = 72
    quiet_hours: str = "22:00-07:00"

    @classmethod
    def from_config(cls, mind: Mapping[str, Any] | None) -> "Policy":
        mind = mind or {}
        deny = mind.get("deny") if isinstance(mind.get("deny"), Mapping) else {}
        patterns = []
        for raw in deny.get("text") or []:
            try:
                patterns.append(re.compile(str(raw), re.IGNORECASE))
            except re.error:
                continue
        level = str(mind.get("autonomy") or "suggest").lower()
        return cls(
            level=level if level in LEVELS else "suggest",
            enabled=mind.get("enabled", True) is not False,
            deny_tools=frozenset(str(name) for name in deny.get("tools") or []),
            deny_text=tuple(patterns),
            deny_commands=tuple(str(item) for item in deny.get("commands") or []),
            worker_toolsets=tuple(str(item) for item in mind.get("worker_toolsets") or cls.worker_toolsets),
            budgets=Budgets.from_config(mind.get("budgets")),
            breaker=Breaker.from_config(mind.get("breaker")),
            ask_expires_hours=float(mind.get("ask_expires_hours", 72) or 72),
            quiet_hours=str(mind.get("quiet_hours") or ""),
        )

    def denied(self, text: str, tools: Iterable[str] = ()) -> Optional[str]:
        """The deny-list reason for this text or tool set, if any (about 30 lines, 7.5)."""
        for tool in tools:
            if str(tool) in self.deny_tools:
                return f"tool {tool} is on the deny list"
        for pattern in self.deny_text:
            if pattern.search(text or ""):
                return f"text matches the denied pattern {pattern.pattern!r}"
        return None


def demote(level: str) -> str:
    """One level down; ``suggest`` and ``off`` stay where they are."""
    index = LEVELS.index(level) if level in LEVELS else 2
    return LEVELS[max(1, index - 1)]


class Authority:
    """Bind the decision table to the intention store: budgets, breaker, guard."""

    BUDGET_ACTION = "queued"  # the history action counted by the budgets
    DIGEST_TYPES = ("digest", "ask_notice", "breaker_notice", "health_notice")
    # Words to the owner that are not the mind's initiative: its own reports, the reminders and
    # heads-ups the owner asked for, and the question a message the owner asked for raises (who is
    # the recipient the owner named). ``suggest`` does not hold them for the digest.
    # Words owed to the owner for what they asked: the answer to a follow-up, and the report of a task done
    # for an obligation to them. Decided like every word to the owner (the off switch, the level, the floor,
    # the deny list), they are the obligation's own delivery: bounded by what was asked (a task budget, a
    # follow-up), never held by the daily owner budget and never spending a slot of it.
    OWED_OWNER_TYPES = (OUTREACH_ANSWER, TASK_OUTCOME)
    REQUESTED_TYPES = DIGEST_TYPES + ("commitment_reminder", "commitment_due_soon", "recipient_unknown",
                                      *OWED_OWNER_TYPES)
    # Unprompted outreach has its own daily budget and never takes a slot of the owner budget, so it can
    # never defer a reminder the owner asked for; the answer to a follow-up the owner asked for is neither.
    OUTREACH_OWNER_TYPES = tuple(sorted(OUTREACH_TYPES)) + (OUTREACH_ANSWER,)

    def __init__(self, policy: Policy, store: Any, *, owner_id: str | None, clock=None) -> None:
        self.policy = policy
        self.store = store
        self.owner_id = owner_id
        self.clock = clock or (lambda: now_utc())
        self._enabled_override: Optional[bool] = None

    # -- state ----------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """The off switch and the ``off`` level both mean no further effects (7.9)."""
        if self.policy.level == "off":
            return False
        if self._enabled_override is not None:
            return self._enabled_override
        return self.policy.enabled

    def set_enabled(self, value: Optional[bool]) -> None:
        self._enabled_override = value

    @property
    def level(self) -> str:
        return self.policy.level

    # -- breaker -----------------------------------------------------------------

    def breaker_state(self, cls: str, now: datetime | None = None) -> Dict[str, Any]:
        """3 failures of a class within 24 h demote it for 72 h, or until a reset (7.6)."""
        now = now or self.clock()
        breaker = self.policy.breaker
        since = now - timedelta(hours=breaker.window_hours)
        reset_at = self.store.last_transition_at("breaker_reset", type=f"breaker_reset:{cls}") \
            if self.store is not None else None
        # A failure the outcome marked uncounted (a timeout, ``Outcomes._breaker_count``) fails its task only.
        failures = [row for row in (self.store.failures_since(cls, since) if self.store is not None else [])
                    if (reset_at is None or (row.failed_at and row.failed_at > reset_at))
                    and ((row.result_metadata or {}).get("breaker") or {}).get("counted") is not False]
        tripped = len(failures) >= breaker.failures
        until = None
        if tripped:
            latest = max(row.failed_at for row in failures if row.failed_at)
            until = latest + timedelta(hours=breaker.demotion_hours)
            tripped = now < until
        return {"cls": cls, "tripped": tripped, "failures": len(failures),
                "until": until.isoformat() if until and tripped else None}

    def reset_breaker(self, cls: str, *, by: str = "owner") -> Dict[str, Any]:
        """Record an owner reset as an audit row: failures before it no longer count."""
        if cls not in CLASSES:
            raise ValueError(f"unknown class {cls!r}")
        row, _ = self.store.create_intention(
            kind="note", type=f"breaker_reset:{cls}", title=f"breaker reset for {cls} by {by}",
            drive="upkeep", cls="internal", decision="act", decision_reason="owner reset",
            status="done", dedup_key=None, hermes_kind="none")
        self.store.transition(row.id, "done", action="breaker_reset", details={"cls": cls, "by": by},
                              outcome="done", verified="owner", completed_at=self.clock())
        return self.breaker_state(cls)

    # -- budgets -------------------------------------------------------------------

    def budget_check(self, *, kind: str, recipient: str | None, type: str = "",
                     now: datetime | None = None, cooldown_hours: float | None = None) -> Optional[str]:
        """The reason an act must wait, or None when the budgets allow it (7.6).

        ``cooldown_hours`` is a message's own per-contact cooldown (a check-in's
        reply-and-silence backoff, architecture 4.7 item 6); it replaces the
        flat ``per_contact_cooldown_hours`` for that one decision. The daily
        contact-message cap still applies.
        """
        if self.store is None:
            return None
        now = now or self.clock()
        budgets = self.policy.budgets
        if kind in {"task", "goal"}:
            hour_ago = now - timedelta(hours=1)
            if self.store.count_transitions(self.BUDGET_ACTION, hour_ago, kind="task") >= budgets.tasks_per_hour:
                return f"budget: {budgets.tasks_per_hour} tasks per hour reached"
            if kind == "goal":
                # A goal owns no Hermes object (its steps are tasks); only the goal count is budgeted.
                open_goals = len(self.store.intentions(status=["approved", "asked", "proposed"], kind=["goal"],
                                                       limit=1000))
                if open_goals >= budgets.open_goals:
                    return f"budget: {budgets.open_goals} open goals reached"
                return None
            # A blocked task waits on someone in Hermes and runs nothing: it holds no slot.
            running = len([row for row in self.store.intentions(status=["approved", "dispatched"], kind=["task"],
                                                                limit=1000) if row.outcome != "blocked"])
            if running >= budgets.concurrent_tasks:
                return f"budget: {budgets.concurrent_tasks} concurrent tasks reached"
            return None
        if kind == "message":
            if type in self.DIGEST_TYPES:
                return None
            day_ago = now - timedelta(days=1)
            if recipient and recipient == self.owner_id:
                if type in self.OWED_OWNER_TYPES:
                    return None
                if type in OUTREACH_TYPES:
                    sent = self.store.count_transitions(self.BUDGET_ACTION, day_ago, kind="message",
                                                        recipient=recipient,
                                                        include_types=tuple(sorted(OUTREACH_TYPES)))
                    if sent >= budgets.outreach_per_day:
                        return f"budget: {budgets.outreach_per_day} outreach messages per day reached"
                    return None
                sent = self.store.count_transitions(self.BUDGET_ACTION, day_ago, kind="message",
                                                    recipient=recipient,
                                                    exclude_types=(self.DIGEST_TYPES + self.OUTREACH_OWNER_TYPES
                                                                   + self.OWED_OWNER_TYPES))
                if sent >= budgets.owner_messages_per_day:
                    return f"budget: {budgets.owner_messages_per_day} owner messages per day reached"
                return None
            sent = self.store.count_transitions(self.BUDGET_ACTION, day_ago, kind="message",
                                                exclude_types=self.DIGEST_TYPES)
            owner_sent = self.store.count_transitions(self.BUDGET_ACTION, day_ago, kind="message",
                                                      recipient=self.owner_id, exclude_types=self.DIGEST_TYPES) \
                if self.owner_id else 0
            if sent - owner_sent >= budgets.contact_messages_per_day:
                return f"budget: {budgets.contact_messages_per_day} contact messages per day reached"
            if recipient:
                hours = budgets.per_contact_cooldown_hours if cooldown_hours is None else max(0.0, float(cooldown_hours))
                last = self.store.last_transition_at(self.BUDGET_ACTION, recipient=recipient)
                if last is not None and now - last < timedelta(hours=hours):
                    return f"budget: {hours:g} h cooldown for this contact"
        return None

    def tokens_allowed(self, now: datetime | None = None) -> bool:
        if self.store is None:
            return True
        now = now or self.clock()
        return self.store.tokens_since(now - timedelta(days=1)) < self.policy.budgets.llm_tokens_per_day

    # -- the decision ----------------------------------------------------------------

    def decide(self, *, kind: str, recipient: str | None, text: str, type: str = "",
               may_contact: str = "ask", toolsets: Iterable[str] = (), tools: Iterable[str] = (),
               now: datetime | None = None, cooldown_hours: float | None = None) -> Verdict:
        """act | ask | drop | defer for one intention, with the reason (7.2-7.6)."""
        now = now or self.clock()
        cls = classify(kind=kind, recipient=recipient, owner_id=self.owner_id, toolsets=toolsets, text=text)
        matched = floor_class(text)
        denied = self.policy.denied(text, tools)
        breaker = self.breaker_state(cls if cls != "floor" else "owner", now) if cls != "floor" else {"tripped": False}
        budget = self.budget_check(kind=kind, recipient=recipient, type=type, now=now, cooldown_hours=cooldown_hours)
        decision = decide_table(level=self.level, cls=cls, may_contact=may_contact, floor=matched is not None,
                                deny=denied is not None, budget_exhausted=budget is not None,
                                breaker_tripped=bool(breaker.get("tripped")), enabled=self.enabled,
                                requested=kind == "message" and type in self.REQUESTED_TYPES)
        if self.level == "off":
            reason = "autonomy level off"
        elif not self.enabled:
            reason = "the mind is off"
        elif denied:
            reason = denied
        elif matched:
            reason = f"floor: {matched.replace('_', ' ')} always asks"
        elif decision == "defer":
            reason = budget or "budget"
        elif decision == "ask" and breaker.get("tripped"):
            reason = f"breaker: {breaker['failures']} recent {cls} failures; asking until {breaker.get('until')}"
        elif self.level == "suggest" and decision == "ask":
            reason = f"{self.level}: {cls} effects are suggested in the digest"
        elif cls == "contact":
            reason = f"{self.level}: contact may_contact={may_contact}"
        else:
            reason = f"{self.level}: {cls} -> {decision}"
        return Verdict(decision=decision, reason=reason, cls=cls, floor=matched,
                       notice=not (decision == "ask" and self.level == "suggest" and matched is None))

    # -- the guard (7.5) ------------------------------------------------------------------

    def guard(self, *, tool: str, args: Mapping[str, Any] | None, run: str = "mind",
              recipient_may_contact: str | None = None) -> Dict[str, Any]:
        """The sidecar half of ``pre_tool_call``: allow | block | ask with a reason.

        Off means every effect from a mind-originated run is blocked. The
        deny list and the floor apply to the tool name and its arguments; a
        messaging tool needs a permitted recipient and budget. Read-only
        tools never reach here.
        """
        text = json.dumps(dict(args or {}), ensure_ascii=False, sort_keys=True)
        if run == "mind" and not self.enabled:
            why = "autonomy level off" if self.level == "off" else "the mind is off"
            return {"allow": False, "action": "block", "reason": f"{why}; no effects until it is turned on"}
        denied = self.policy.denied(text, [tool])
        if denied:
            return {"allow": False, "action": "block", "reason": denied}
        matched = floor_class(text)
        if matched:
            return {"allow": False, "action": "ask", "reason": f"floor: {matched.replace('_', ' ')} needs the owner",
                    "floor": matched}
        if tool == "kanban_create" and run == "mind":
            if str((args or {}).get("assignee") or "") != "protagine-act":
                return {"allow": False, "action": "block", "reason": "mind tasks may only create protagine-act tasks"}
            if str((args or {}).get("idempotency_key") or "").startswith("mind:"):
                return {"allow": False, "action": "block", "reason": "mind: idempotency keys belong to the sidecar"}
            budget = self.budget_check(kind="task", recipient=None)
            if budget:
                return {"allow": False, "action": "block", "reason": budget}
        if recipient_may_contact is not None:
            if recipient_may_contact == "never":
                return {"allow": False, "action": "block", "reason": "the recipient may not be contacted"}
            if recipient_may_contact == "ask" and self.level != "trusted":
                return {"allow": False, "action": "ask", "reason": "messaging this contact needs the owner's approval"}
        return {"allow": True, "action": "allow", "reason": "allowed"}


def ask_expiry(now: datetime, policy: Policy) -> datetime:
    return now + timedelta(hours=policy.ask_expires_hours)


def parse_quiet_hours(value: str) -> Optional[tuple[int, int]]:
    """``"22:00-07:00"`` as minutes of the day; None when unset or malformed."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", value or "")
    if not match:
        return None
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    if not (0 <= start < 1440 and 0 <= end < 1440):
        return None
    return start, end


def in_quiet_hours(local_minute: int, window: Optional[tuple[int, int]]) -> bool:
    if window is None:
        return False
    start, end = window
    if start == end:
        return False
    if start < end:
        return start <= local_minute < end
    return local_minute >= start or local_minute < end


def last_boundary(now: datetime, tz: Any, minute: int) -> datetime:
    """The latest local time of day ``minute`` (minutes after local midnight) at or before ``now``."""
    local = now.astimezone(tz)
    at = time(int(minute) // 60 % 24, int(minute) % 60)
    boundary = datetime.combine(local.date(), at, tzinfo=tz)
    if boundary > local:
        boundary = datetime.combine(local.date() - timedelta(days=1), at, tzinfo=tz)
    return boundary


def boundary_crossed(since: datetime, now: datetime, *, tz: Any, minute: int) -> bool:
    """Whether the local time of day ``minute`` fell in ``(since, now]``.

    The rule for anything nightly: it holds once per night crossed, whatever hour
    the clock started at, never twice for the same night, and once for a machine
    that slept through the hour. ``since`` is the persisted moment of the last run.
    """
    return since < last_boundary(now, tz, minute) <= now


__all__ = [
    "ASK_ALPHABET", "Authority", "Breaker", "Budgets", "CLASSES", "DECISIONS", "FLOOR_PATTERNS",
    "INTERNAL_SAFE_TOOLSETS", "LEVELS", "MAY_CONTACT", "Policy", "Verdict", "ask_expiry", "boundary_crossed",
    "classify", "decide_table", "demote", "floor_class", "in_quiet_hours", "last_boundary", "may_contact_of",
    "new_ask_code", "parse_quiet_hours",
]
