"""The mind tick: one ranked producer of self-initiated work (architecture 3.2).

Every tick: the timers (ask expiry, deferred intentions, expectations,
retention, the nightly backup), a bounded drain of the capture jobs still
pending (a promise made seconds ago must be a row before the drives look),
then decay, the drives over a snapshot of stored state, the concerns they
raise, reconsideration of active intentions on matching events, the goals,
and the top concerns ranked into intentions: a template, or one tool-less
deliberation call per tick, then the authority decision and the intention row.

The body pulls the dispatch queue and the outbox from the router in
``P/api/routers/mind.py``; when its last pull is older than five minutes the
tick stops forming intentions until it is back. The off switch works
without the model endpoint: it is a marker file and an in-memory flag.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from zoneinfo import ZoneInfo

from protagine.initiatives.models import MIND_ACTIVE_STATUSES, StoredInitiative

from . import audit, drives as drive_functions
from .authority import (
    Authority, CLASSES, LEVELS, MAY_CONTACT, Policy, ask_expiry, in_quiet_hours, may_contact_of, new_ask_code,
    parse_quiet_hours,
)
from .concerns import BROADCAST, MIND_DB, RESOLVED_RETENTION, SETTLED_FOR, Concern, Concerns
from .deliberate import Deliberation, refresh_context
from .drives import DRIVES, DriveInputs, slug, task_body
from .goals import DEFAULT_MAX_TURNS, Goals, goal_lines
from .outbox import Outbox
from .outcomes import Autobiography, Outcomes, evaluate_check, invalidation_reason
from .rank import Candidate, DEFAULT_ACT_THRESHOLD, eligible

logger = logging.getLogger(__name__)

OFF_MARKER = "mind.off"
STALE_AFTER = timedelta(minutes=5)
TASK_WINDOW = timedelta(hours=48)
RETENTION = timedelta(days=90)
KEEP_BACKUPS = 7
HEALTH_STRIKES = drive_functions.HEALTH_STRIKES
STALE_TASK_HOURS = drive_functions.STALE_TASK_HOURS
SATIETY_HALF_LIFE_S = 4 * 3600.0
INTEREST_HALF_LIFE_S = 30 * 86400.0
FAILURE_WINDOW = drive_functions.FAILURE_WINDOW
MIND_SECTION_CHARS = 600
MESSAGING_TOOLS = frozenset({"send_message", "react_to_message", "discord", "discord_admin", "yb_send_dm",
                             "yb_send_sticker"})
# Stock ``send_message`` targets: ``platform:chat_id``, and on these platforms ``platform:chat_id:thread_id``.
THREADED_PLATFORMS = frozenset({"telegram", "discord"})
WORKER_PROFILE = "protagine-act"
DEFAULT_FACULTIES = {"initiative": True, "drives": True, "deliberation": True, "goals": True, "broadcast": True}
# How long a tick waits for capture jobs still pending before the drives read the store: a
# forced tick (the CLI, the harness) is a decision point and waits longer than the 60 s timer.
DRAIN_FORCED_S, DRAIN_TIMER_S = 30.0, 5.0
# Intention types formed because a commitment row was due; a deadline that moves back into the
# future, or away, makes them stale.
DUE_TYPES = frozenset({"commitment_overdue", "commitment_reminder", "commitment_deliverable"})


def split_target(target: str) -> tuple[str, str]:
    """``(platform, chat_id)`` of a stock ``send_message`` target; a thread suffix is dropped."""
    platform, _, rest = str(target or "").partition(":")
    platform, rest = platform.strip().lower(), rest.strip()
    if platform in THREADED_PLATFORMS and rest.count(":") == 1:
        rest = rest.split(":", 1)[0]
    return platform, rest


def _utc(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _wall_clock() -> Callable[[], datetime]:
    """UTC now; ``PROTAGINE_MIND_CLOCK_OFFSET_SECONDS`` shifts it (a test seam for the 72 h ask expiry)."""
    try:
        offset = float(os.environ.get("PROTAGINE_MIND_CLOCK_OFFSET_SECONDS") or 0)
    except ValueError:
        offset = 0.0
    if not offset:
        return lambda: datetime.now(timezone.utc)
    return lambda: datetime.now(timezone.utc) + timedelta(seconds=offset)


def faculties_of(config: Mapping[str, Any] | None) -> Dict[str, bool]:
    """The binary faculty flags with their defaults; each is one benchmark arm."""
    values = dict(DEFAULT_FACULTIES)
    for name, raw in ((config or {}).get("faculties") or {}).items():
        values[str(name)] = raw is not False and str(raw).strip().lower() not in {"0", "false", "no", "off"}
    return values


class Mind:
    def __init__(self, *, config: Mapping[str, Any] | None, store: Any, state_dir: str | os.PathLike[str],
                 owner_id: str | None, commitments: Any = None, followups: Any = None, feedback: Any = None,
                 expectations: Any = None, contacts: Any = None, ledger: Any = None, clock=None,
                 interval: float = 60.0, backups: bool = True, timezone_name: str | None = None,
                 persist: Callable[[Dict[str, Any]], None] | None = None, router: Any = None,
                 appraisals: Any = None, interests: Iterable[str] = (), concerns: Concerns | None = None,
                 backlog: Callable[[], Mapping[str, int]] | None = None, capture: Any = None) -> None:
        mind = dict(config or {})
        self.config = mind
        self.policy = Policy.from_config(mind)
        self.store = store
        self.state_dir = Path(state_dir)
        self.owner_id = owner_id or None
        self.commitments = commitments
        self.capture = capture      # the CommitmentExtractor over the same ledger, drained before each decision
        self.drain_forced_s, self.drain_timer_s = DRAIN_FORCED_S, DRAIN_TIMER_S
        try:
            grace = float(mind.get("heads_up_grace_minutes", drive_functions.HEADS_UP_GRACE.total_seconds() / 60))
        except (TypeError, ValueError):
            grace = drive_functions.HEADS_UP_GRACE.total_seconds() / 60
        self.heads_up_grace = timedelta(minutes=max(0.0, grace))
        self.followups = followups
        self.feedback = feedback
        self.expectations = expectations
        self.contacts = contacts
        self.ledger = ledger
        self.appraisals = appraisals
        self.backlog_probe = backlog
        self.clock = clock or _wall_clock()
        self.interval = float(interval)
        self.backups = backups
        self.persist = persist
        self.faculties = faculties_of(mind)
        self.drive_weights = drive_functions.weights(mind.get("drives"), faculty_on=self.faculties["drives"])
        self.act_threshold = float(mind.get("act_threshold") or DEFAULT_ACT_THRESHOLD)
        self.digest_hour = int(mind.get("digest_hour", 8) or 0)
        try:
            self.tz = ZoneInfo(timezone_name) if timezone_name else timezone.utc
        except Exception:
            self.tz = timezone.utc
        self.quiet = parse_quiet_hours(self.policy.quiet_hours)

        self.authority = Authority(self.policy, store, owner_id=self.owner_id, clock=self.clock)
        self.autobiography = Autobiography(ledger, owner_id=self.owner_id, clock=self.clock)
        self.outbox = Outbox(store, owner_id=self.owner_id, clock=self.clock)
        self.outcomes = Outcomes(store, authority=self.authority, feedback=feedback, expectations=expectations,
                                 commitments=commitments, followups=followups, autobiography=self.autobiography,
                                 clock=self.clock)
        self.outcomes.on_settled = self._on_settled
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.concerns = concerns if concerns is not None else Concerns(self.state_dir / MIND_DB, clock=self.clock)
        self.mind_state = self.concerns.state
        self.deliberation = Deliberation(router, clock=self.clock, tokens_allowed=self.authority.tokens_allowed,
                                         enabled=self.faculties["deliberation"], budgets=self.policy.budgets)
        self.goals = Goals(store, budgets=self.policy.budgets, clock=self.clock,
                           enabled=self.faculties["goals"] and self.faculties["drives"])
        if expectations is not None and hasattr(expectations, "register_resolver"):
            expectations.register_resolver("intention:", self._resolve_intention_expectation)
        for topic in interests or []:
            if str(topic).strip() and self.mind_state.get(f"interest:{slug(topic)}") is None:
                self.add_interest(str(topic), why="a declared identity interest", by="owner")

        self.started_at = self.clock()
        self.last_pull_at: Optional[datetime] = None
        self.last_tick_at: Optional[datetime] = None
        self.ticks = 0
        self.observations: Dict[str, List[Dict[str, Any]]] = {}
        self.observed_at: Optional[datetime] = None
        self.body_heartbeat: Dict[str, Any] = {}
        self.board_counts: Dict[str, int] = {}
        self.off_reason: Optional[str] = None
        self.drive_levels: Dict[str, float] = {name: 0.0 for name in DRIVES}
        self._health_failures: Dict[str, int] = {}
        self._daily: Dict[str, str] = {}
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        if self.off_marker.exists():
            self.authority.set_enabled(False)
            try:
                self.off_reason = self.off_marker.read_text(encoding="utf-8").strip() or "off"
            except OSError:
                self.off_reason = "off"
        self.outbox.recover()

    # -- switches --------------------------------------------------------------------

    @property
    def off_marker(self) -> Path:
        return self.state_dir / OFF_MARKER

    @property
    def enabled(self) -> bool:
        return self.authority.enabled

    @property
    def level(self) -> str:
        return self.policy.level

    @property
    def router(self) -> Any:
        return self.deliberation.router

    @router.setter
    def router(self, value: Any) -> None:
        self.deliberation.router = value

    def off(self, *, reason: str = "owner", by: str = "owner") -> Dict[str, Any]:
        """No further effects, at once and without the model endpoint (7.9)."""
        now = self.clock()
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.off_marker.write_text(f"{reason} at {now.isoformat()}\n", encoding="utf-8")
        except OSError as error:
            logger.warning("off marker not written (%s)", type(error).__name__)
        self.authority.set_enabled(False)
        self.off_reason = reason
        cancelled = self.outbox.cancel_unsent("mind off")
        row, created = self.store.create_intention(
            kind="note", type="off_switch", title=f"mind off ({reason}) by {by}", drive="upkeep", cls="internal",
            decision="act", decision_reason="off switch", status="done", dedup_key=None, hermes_kind="none",
            created_at=now)
        self.store.transition(row.id, "done", action="off", outcome="done", verified="owner", completed_at=now, at=now)
        return {"enabled": False, "reason": reason, "cancelled_messages": cancelled}

    def on(self, *, by: str = "owner") -> Dict[str, Any]:
        try:
            self.off_marker.unlink(missing_ok=True)
        except OSError as error:
            logger.warning("off marker not removed (%s)", type(error).__name__)
        self.policy.enabled = True
        self.authority.set_enabled(None)
        self.off_reason = None
        if self.persist is not None:
            self.persist({"mind": {"enabled": True}})
        now = self.clock()
        row, _ = self.store.create_intention(
            kind="note", type="on_switch", title=f"mind on by {by}", drive="upkeep", cls="internal", decision="act",
            decision_reason="on switch", status="done", dedup_key=None, hermes_kind="none", created_at=now)
        self.store.transition(row.id, "done", action="on", outcome="done", verified="owner", completed_at=now, at=now)
        return {"enabled": True}

    def set_level(self, level: str, *, by: str = "owner") -> Dict[str, Any]:
        level = str(level or "").strip().lower()
        if level not in LEVELS:
            raise ValueError(f"autonomy must be one of {', '.join(LEVELS)}")
        previous = self.policy.level
        self.policy.level = level
        self.config["autonomy"] = level
        if self.persist is not None:
            self.persist({"mind": {"autonomy": level}})
        now = self.clock()
        row, _ = self.store.create_intention(
            kind="note", type="level_change", title=f"autonomy {previous} -> {level} by {by}", drive="upkeep",
            cls="internal", decision="act", decision_reason="owner setting", status="done", dedup_key=None,
            hermes_kind="none", created_at=now)
        self.store.transition(row.id, "done", action="level", outcome="done", verified="owner", completed_at=now, at=now)
        result: Dict[str, Any] = {"autonomy": level, "previous": previous}
        if level == "off" and previous != "off":
            # Level off is the off switch by another name: nothing queued goes out either.
            result["cancelled_messages"] = self.outbox.cancel_unsent("autonomy off")
        return result

    def reset(self, cls: str, *, by: str = "owner") -> Dict[str, Any]:
        return self.authority.reset_breaker(cls, by=by)

    def add_interest(self, topic: str, *, why: str = "", by: str = "owner") -> Dict[str, Any]:
        """A seeded or declared interest: curiosity raises a research concern for it."""
        topic = " ".join(str(topic or "").split())[:160]
        if not topic:
            raise ValueError("an interest needs a topic")
        key = f"interest:{slug(topic)}"
        # An interest fades over a month unless it is reinforced; below 0.25 curiosity leaves it alone.
        entry = self.mind_state.set(key, level=min(3.0, float((self.mind_state.get(key) or {}).get("level") or 0) + 1.0),
                                    text=topic, causes=[f"{by}: {why}" if why else by],
                                    half_life_s=INTEREST_HALF_LIFE_S)
        return {"key": key, "topic": topic, "weight": entry.get("level"), "causes": entry.get("causes")}

    # -- the loop -------------------------------------------------------------------------

    async def run(self) -> None:
        self._running = True
        try:
            while not self._stop.is_set():
                try:
                    await self.tick()
                except Exception:
                    logger.exception("mind tick failed")
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
        finally:
            self._running = False

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.get_running_loop().create_task(self.run(), name="protagine-mind")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        self._task = None

    def wake(self) -> None:
        self._wake.set()

    def body_stale(self, now: datetime | None = None) -> bool:
        now = now or self.clock()
        anchor = self.last_pull_at or self.started_at
        return now - anchor > STALE_AFTER

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        local = (now or self.clock()).astimezone(self.tz)
        return in_quiet_hours(local.hour * 60 + local.minute, self.quiet)

    async def tick(self, now: datetime | None = None, *, force: bool = False) -> Dict[str, Any]:
        """One tick. ``force`` (the CLI and the harness) ignores body staleness."""
        async with self._lock:
            now = now or self.clock()
            self.ticks += 1
            self.last_tick_at = now
            summary: Dict[str, Any] = {"tick": self.ticks, "at": now.isoformat(), "formed": [], "skipped": None,
                                       "model_calls": 0}
            if not self.enabled:
                summary["skipped"] = "off"
                summary["expired_asks"] = self._expire_asks(now)
                return summary
            summary["expired_asks"] = self._expire_asks(now) + self._expire_messages(now)
            summary["invalidated"] = self._invalidate(now)
            summary["expectations"] = self._resolve_expectations(now)
            summary["retention"] = self._retention(now)
            summary["backup"] = await self._backup(now)
            if self.body_stale(now) and not force:
                summary["skipped"] = "body stale"
                return summary
            self.deliberation.begin_tick()
            summary["reconsidered"] = await self._reconsider(now)
            summary["capture_drained"] = await self._drain_capture(force)
            summary["overdue_flipped"] = self._flip_overdue(now)
            self.mind_state.decay(now)
            summary["decay"] = self.concerns.decay(now)
            inputs = self._gather(now)
            summary["drives"], events = self._raise_concerns(inputs, now)
            summary["revised"] = self._bdi(now, events)
            summary["goals"] = self._tend_goals(now)
            summary["formed"], summary["below_threshold"] = await self._act(now)
            summary["model_calls"] = self.deliberation.calls_this_tick
            summary["digest"] = self._digest(now)
            notice = self.outbox.notify_asks(self.store.intentions(status=["asked"], limit=200))
            summary["ask_notice"] = notice.id if notice is not None else None
            return summary

    # -- timers ----------------------------------------------------------------------------

    def _expire_asks(self, now: datetime) -> int:
        count = 0
        for row in self.store.intentions(status=["asked"], limit=500):
            if row.expires_at and row.expires_at <= now:
                self.outcomes.record(row.id, status="expired", summary="no answer before the ask expired", by="mind")
                count += 1
        for row in self.store.intentions(status=["approved", "proposed"], kind=["task"], limit=500):
            if row.expires_at and row.expires_at <= now:
                self.outcomes.record(row.id, status="expired", summary="not dispatched inside its window", by="mind")
                count += 1
        return count

    def _expire_messages(self, now: datetime) -> int:
        """A message unsent past its window (the body never pulled, no handle ever resolved) expires
        through the same settle path as a task, so its concern is dropped rather than left intended,
        and an open obligation it never reported gets its key back: the reminder forms again at the
        next tick instead of being lost until the deadline moves. Not the owner's verdict on anything."""
        count = 0
        for row in self.store.intentions(status=["approved"], kind=["message"], limit=500):
            if row.expires_at and row.expires_at < now:
                self.outcomes.record(row.id, status="expired", summary="unsent before expiry", by="mind",
                                     implicit_verdict=False)
                self._free_open_obligation(row)
                count += 1
        return count

    def _stale_reason(self, row: StoredInitiative) -> Optional[str]:
        """Why an intention still waiting should not act: its obligation resolved itself
        (``invalidates_if``), the owner turned its drive off (weight 0), or the goal it is a
        step of is no longer open. Notices, the digest and messages other subsystems asked
        for are the mind's reporting, not a drive's work: a weight of 0 does not cancel them."""
        reason = invalidation_reason(row.invalidates_if, commitments=self.commitments, followups=self.followups)
        if reason is None and row.source_type == "commitment" and row.source_id and self.commitments is not None:
            reason = self._commitment_stale_reason(row)
        drive_work = row.type not in audit.NOTICE_TYPES and not str(row.type or "").startswith("reach_out:")
        if reason is None and drive_work and row.drive in DRIVES and float(self.drive_weights.get(row.drive, 1.0)) <= 0:
            reason = f"the {row.drive} drive is off"
        if reason is None and row.parent_goal_id:
            goal = self.store.get(row.parent_goal_id)
            if goal is None or goal.status != "approved":
                reason = f"goal {row.parent_goal_id} is {goal.status if goal is not None else 'gone'}"
        return reason

    def _commitment_stale_reason(self, row: StoredInitiative) -> Optional[str]:
        """Why an intention about a commitment row no longer fits the row: the row is gone, a
        conversation pushed its deadline out (or put it on hold) after the intention was approved,
        or a heads-up came due before it went out. ``dispatch`` and the outbox check this too, so
        a push-out is safe across ticks."""
        ident = str(row.source_id)
        try:
            record = self.commitments.get(ident)
        except Exception as error:
            logger.debug("commitment %s unavailable (%s)", ident, type(error).__name__)
            return None
        if not isinstance(record, dict):
            return f"commitment {ident} was removed"
        now = self.clock()
        due = _utc(record.get("due_at"))
        if row.type in DUE_TYPES:
            if due is None:
                return f"commitment {ident} no longer has a deadline"
            if due > now:
                return f"commitment {ident} is no longer due (now due {due.isoformat()})"
        elif row.type == "commitment_due_soon":
            if due is None or due <= now:
                return f"commitment {ident} is already due"
            warn_at = drive_functions.heads_up_at(record, due)
            if warn_at is None or warn_at > now:
                return f"commitment {ident} no longer wants a heads-up now"
        return None

    def _invalidated(self, row: StoredInitiative) -> bool:
        """Cancel an intention whose justification is gone (the check's cancellation, not a dismissal)."""
        reason = self._stale_reason(row)
        if reason is None:
            return False
        self._cancel_stale(row, reason)
        return True

    def _cancel_stale(self, row: StoredInitiative, reason: str) -> None:
        self.outcomes.record(row.id, status="cancelled", summary=f"invalidated: {reason}", verified="check",
                             by="mind", implicit_verdict=False)
        self._free_open_obligation(row)

    def _free_open_obligation(self, row: StoredInitiative) -> None:
        """An intention about a commitment that is still open and was never reported (a push-out, a
        hold, a heads-up that came due, a message that expired unsent) gives its key back, so the
        obligation competes again at its time. A resolved obligation keeps its key: reported once
        (architecture 3.3)."""
        if row.dedup_key and row.source_type == "commitment" and self._commitment_open(row.source_id):
            self.store.update(row.id, dedup_key=None)

    def _commitment_open(self, ident: Any) -> bool:
        if self.commitments is None or not ident:
            return False
        try:
            record = self.commitments.get(str(ident))
        except Exception:
            return False
        return isinstance(record, dict) and record.get("status") in {"pending", "overdue"}

    def _invalidate(self, now: datetime) -> int:
        """Every tick: an intention still waiting (deferred, asked or approved) whose source is
        resolved is cancelled before it is approved, dispatched or sent."""
        count = 0
        for row in self.store.intentions(status=["proposed", "asked", "approved"], limit=500):
            if self._invalidated(row):
                count += 1
        return count

    def _resolve_expectations(self, now: datetime) -> Dict[str, int]:
        if self.expectations is None or not hasattr(self.expectations, "check"):
            return {}
        try:
            return dict(self.expectations.check(now=now.timestamp()))
        except Exception as error:
            logger.warning("expectation check failed (%s)", type(error).__name__)
            return {}

    def _resolve_intention_expectation(self, prediction: Any) -> Optional[bool]:
        intention_id = str(getattr(prediction, "subject", "") or "").partition(":")[2]
        row = self.store.get(intention_id) if intention_id else None
        if row is None:
            return None
        if row.outcome == "done":
            check = (row.result_metadata or {}).get("check") if isinstance(row.result_metadata, dict) else None
            return not (isinstance(check, dict) and check.get("passed") is False)
        if row.outcome in {"failed", "expired", "denied", "cancelled"}:
            return False
        return None

    def _retention(self, now: datetime) -> Optional[Dict[str, int]]:
        local_date = now.astimezone(self.tz).date().isoformat()
        if self._daily.get("retention") == local_date:
            return None
        self._daily["retention"] = local_date
        counts = self.store.prune_intentions(now - RETENTION)
        pruned = self.concerns.prune(now - RESOLVED_RETENTION)
        if pruned:
            counts = {**counts, "concerns": pruned}
        if counts:
            summary = ", ".join(f"{count} {key}" for key, count in sorted(counts.items()))
            self.autobiography.record(f"retention-{local_date}", "retention",
                                      f"Audit rows older than {RETENTION.days} days were summarized: {summary}.")
        return counts

    async def _backup(self, now: datetime) -> Optional[str]:
        if not self.backups:
            return None
        local = now.astimezone(self.tz)
        local_date = local.date().isoformat()
        if self._daily.get("backup") == local_date:
            return None
        quiet = self.in_quiet_hours(now) if self.quiet else local.hour >= 3
        if not quiet:
            return None
        self._daily["backup"] = local_date
        try:
            return await asyncio.to_thread(self._backup_stores, local_date)
        except Exception as error:
            logger.warning("nightly backup failed (%s)", type(error).__name__)
            return None

    def _backup_stores(self, local_date: str) -> str:
        root = self.state_dir / "backups" / "nightly"
        target = root / local_date
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in sorted(self.state_dir.glob("*.db")):
            with sqlite3.connect(path) as source, sqlite3.connect(target / path.name) as copy:
                source.backup(copy)
        for old in sorted(item for item in root.iterdir() if item.is_dir())[:-KEEP_BACKUPS]:
            for item in old.iterdir():
                item.unlink(missing_ok=True)
            old.rmdir()
        return str(target)

    async def _reconsider(self, now: datetime) -> int:
        """Deferred intentions are re-decided every tick; a budget frees up, they proceed."""
        count = 0
        for row in self.store.intentions(status=["proposed"], limit=200):
            if row.decision != "defer":
                continue
            context = row.context if isinstance(row.context, dict) else {}
            may_contact = await self._may_contact(row.entity_id)
            verdict = self.authority.decide(kind=row.kind or "task", recipient=row.entity_id,
                                            text=f"{row.description}\n{context.get('text') or context.get('body') or ''}",
                                            type=row.type, may_contact=may_contact,
                                            toolsets=self.policy.worker_toolsets, now=now)
            if verdict.decision == "defer":
                continue
            count += 1
            self._apply_decision(row, verdict, now, reconsidered=True)
        return count

    def _flip_overdue(self, now: datetime) -> int:
        """The pending -> overdue commitment flip, moved from the old condition checks."""
        if self.commitments is None:
            return 0
        flipped = 0
        try:
            rows = self.commitments.list(status=["pending"], limit=500).get("commitments", [])
        except Exception as error:
            logger.warning("commitments unavailable (%s)", type(error).__name__)
            return 0
        for row in rows:
            due = _utc(row.get("due_at"))
            if due is not None and due <= now:
                try:
                    self.commitments.update(row["id"], status="overdue")
                    flipped += 1
                except Exception as error:
                    logger.debug("overdue flip skipped for %s (%s)", row.get("id"), type(error).__name__)
        return flipped

    async def _drain_capture(self, force: bool) -> Optional[Dict[str, Any]]:
        """Land the capture jobs still pending before the drives read the store.

        Capture is asynchronous (one router call per turn on the projection
        worker), so a promise made seconds before a tick may not be a row yet;
        deciding over the store then means deciding without it. The drain is
        bounded, and a job leased elsewhere is only waited for.
        """
        if self.capture is None:
            return None
        budget = self.drain_forced_s if force else self.drain_timer_s
        try:
            result = await self.capture.drain(self.router, budget_seconds=budget)
        except Exception as error:
            logger.warning("capture drain failed (%s)", type(error).__name__)
            return {"error": type(error).__name__, "budget_seconds": budget}
        return {**dict(result or {}), "budget_seconds": budget}

    # -- the drives: a snapshot of stored state, then concerns -------------------------------

    def _gather(self, now: datetime) -> DriveInputs:
        """Everything the drives read, gathered once; every store is optional."""
        inputs = DriveInputs(now=now, owner_id=self.owner_id, worker_profile=WORKER_PROFILE)
        if self.commitments is not None:
            try:
                inputs.commitments = list(self.commitments.list(status=["pending", "overdue"], limit=500)
                                          .get("commitments", []))
            except Exception as error:
                logger.warning("commitments unavailable (%s)", type(error).__name__)
        inputs.heads_up_grace = self.heads_up_grace
        for row in inputs.commitments:
            # A heads-up that went out holds the overdue reminder for the grace (one word at a time).
            due = _utc(row.get("due_at"))
            if due is None or drive_functions.heads_up_at(row, due) is None:
                continue
            sent = self.store.get_by_dedup_key(drive_functions.schedule_key(row["id"], "heads_up", due))
            if sent is not None and sent.status in {"sent", "sending", "uncertain"}:
                went_out = _utc(sent.completed_at) or _utc(sent.created_at)
                if went_out is not None:
                    inputs.heads_ups[str(row["id"])] = went_out
        if self.followups is not None:
            try:
                inputs.reply_waits = list(self.followups.due(now=now.timestamp(), limit=100))
            except Exception as error:
                logger.warning("reply waits unavailable (%s)", type(error).__name__)
        inputs.stale_tasks = list(self.observations.get("stale_task", []))
        inputs.hermes_goals = list(self.observations.get("goal", []))
        inputs.blocked_tasks = list(self.observations.get("blocked_task", []))
        inputs.expectation_misses = self._expectation_misses(now)
        inputs.interests = self._interests()
        inputs.questions = [{"topic": item.get("text") or item["key"].partition(":")[2], "sources": item.get("causes") or []}
                            for item in self.mind_state.items("question:")]
        since = now - FAILURE_WINDOW
        recent = self.store.intentions(since=since, limit=1000)
        inputs.failures = [row.to_dict() for row in recent if row.kind == "task" and row.outcome == "failed"]
        inputs.corrections = [row.to_dict() for row in recent if row.verdict in {"wrong", "not_useful"}]
        for name, ok in self._health_probes().items():
            self._health_failures[name] = 0 if ok else self._health_failures.get(name, 0) + 1
        inputs.health = dict(self._health_failures)
        if callable(self.backlog_probe):
            try:
                inputs.backlog = {str(k): int(v) for k, v in dict(self.backlog_probe() or {}).items()}
            except Exception as error:
                logger.debug("backlog probe failed (%s)", type(error).__name__)
        inputs.settled = self.concerns.settled_keys(now - SETTLED_FOR)
        # Recurring work is keyed by period over a stable base: while one instance is active (deferred,
        # asked, queued or running) the next period does not start another.
        inputs.settled |= {row.dedup_base for row in self.store.intentions(status=list(MIND_ACTIVE_STATUSES), limit=500)
                           if row.dedup_base}
        return inputs

    def _expectation_misses(self, now: datetime) -> List[Dict[str, Any]]:
        store = getattr(self.expectations, "store", None)
        if store is None or not hasattr(store, "resolved_since"):
            return []
        try:
            rows = store.resolved_since((now - timedelta(days=1)).timestamp())
        except Exception as error:
            logger.debug("expectation misses unavailable (%s)", type(error).__name__)
            return []
        return [{"id": getattr(row, "prediction_id", None), "domain": getattr(row, "domain", ""),
                 "subject": getattr(row, "subject", ""), "expectation": getattr(row, "expectation", "")}
                for row in rows if getattr(row, "outcome", None) == "miss"
                and not str(getattr(row, "subject", "")).startswith("intention:")]

    def _interests(self) -> List[Dict[str, Any]]:
        """Seeded and declared interests from ``mind_state`` plus the owner's own ``interest`` appraisals."""
        found: Dict[str, Dict[str, Any]] = {}
        for item in self.mind_state.items("interest:"):
            topic = str(item.get("text") or item["key"].partition(":")[2])
            found[slug(topic)] = {"topic": topic, "weight": float(item.get("level") or 1.0),
                                  "sources": list(item.get("causes") or []), "why": "a declared interest"}
        if self.appraisals is not None and self.owner_id:
            try:
                view = self.appraisals.view(self.owner_id, viewer_contact_id=self.owner_id, limit=20)
                records = view.get("records", []) if isinstance(view, dict) else []
            except Exception as error:
                logger.debug("appraisals unavailable (%s)", type(error).__name__)
                records = []
            for record in records:
                if record.get("kind") != "appraisal" or record.get("dimension") != "interest":
                    continue
                topic = str(record.get("topic") or "").strip()
                if not topic:
                    continue
                entry = found.setdefault(slug(topic), {"topic": topic, "weight": 0.0, "sources": [],
                                                       "why": "the owner showed interest"})
                entry["weight"] = float(entry["weight"]) + 1.0
                if record.get("id"):
                    entry["sources"].append(f"appraisal:{record['id']}")
        return list(found.values())

    def _health_probes(self) -> Dict[str, bool]:
        probes: Dict[str, Callable[[], Any]] = {"initiatives": lambda: self.store.count()}
        if self.commitments is not None:
            probes["commitments"] = lambda: self.commitments.list(limit=1)
        if self.ledger is not None and hasattr(self.ledger, "_connect"):
            def ledger_probe() -> None:
                conn = self.ledger._connect()
                try:
                    conn.execute("SELECT 1")
                finally:
                    conn.close()
            probes["ledger"] = ledger_probe
        results: Dict[str, bool] = {}
        for name, probe in probes.items():
            try:
                probe()
                results[name] = True
            except Exception as error:
                logger.warning("health probe %s failed (%s)", name, type(error).__name__)
                results[name] = False
        return results

    def effective_weights(self) -> Dict[str, float]:
        satiety = {name: float((self.mind_state.get(f"satiety.{name}") or {}).get("level") or 0.0) for name in DRIVES}
        return drive_functions.effective_weights(self.drive_weights, satiety, faculty_on=self.faculties["drives"])

    def _raise_concerns(self, inputs: DriveInputs, now: datetime) -> tuple[Dict[str, Any], List[str]]:
        """Every enabled drive over the snapshot; each candidate bumps its concern by ``dedup_key``.

        Returns the drive levels and the keys of concerns that were raised
        again while an intention is under way (the events reconsideration
        looks at).
        """
        events: List[str] = []
        raised = 0
        results = drive_functions.run(inputs, self.drive_weights)
        for name, (level, candidates) in results.items():
            self.drive_levels[name] = level
            self.mind_state.set(f"drive.{name}", level=level, now=now)
            for candidate in candidates:
                concern, outcome = self.concerns.bump(
                    drive=name, kind=candidate.concern_kind, summary=candidate.concern or candidate.title,
                    dedup_key=candidate.dedup_key, salience=candidate.salience, sources=candidate.evidence,
                    detail=candidate.as_detail(), now=now)
                if outcome in {"created", "reopened"}:
                    raised += 1
                elif outcome == "bumped" and concern is not None and concern.status == "intended":
                    events.append(concern.dedup_key)
        for name in DRIVES:
            if name not in results:
                self.drive_levels[name] = 0.0
        return {"levels": dict(self.drive_levels), "raised": raised, "weights": self.effective_weights()}, events

    def _bdi(self, now: datetime, events: List[str]) -> int:
        """Reconsider an active intention only when an event matches it (architecture 3.3)."""
        if not events:
            return 0
        count = 0
        for row in self.store.intentions(status=list(MIND_ACTIVE_STATUSES), limit=500):
            if not Deliberation.matches(row, events):
                continue
            concern = self.concerns.by_key(row.dedup_key)
            reason = self._stale_reason(row)
            decision = Deliberation.reconsider(row, concern, invalidated=reason)
            if decision == "cancel":
                self._cancel_stale(row, str(reason))
            elif decision == "refresh" and concern is not None:
                self.store.transition(row.id, row.status, action="reconsidered", at=now,
                                      details={"event": row.dedup_key, "decision": decision},
                                      context=refresh_context(row, concern))
            else:
                continue
            count += 1
        return count

    # -- goals -------------------------------------------------------------------------------

    def _evaluate_goal_check(self, check: Any, **state: Any) -> Optional[bool]:
        return evaluate_check(check, commitments=self.commitments, followups=self.followups, **state)

    def _tend_goals(self, now: datetime) -> Dict[str, Any]:
        """Close goals that are satisfied, spent or past their horizon; raise the next step of the rest.

        A closed goal takes its pending steps with it: a step still waiting
        (deferred, asked or approved) is cancelled and a step concern not yet
        formed is dropped; a dispatched step is running in Hermes and its
        outcome still comes back. Steps are raised only for approved goals: a
        goal still asked or deferred owns no work yet.
        """
        closed = []
        for item in self.goals.due(now, evaluate=self._evaluate_goal_check):
            goal, outcome, why = item["goal"], item["outcome"], item["why"]
            if outcome == "done":
                self.outcomes.record(goal.id, status="done", outcome="done", summary=f"goal satisfied: {why}",
                                     verified="check", by="mind")
            else:
                self.outcomes.record(goal.id, status="expired", outcome="expired", summary=f"goal expired: {why}",
                                     by="mind", implicit_verdict=False)
            closed.append({"id": goal.id, "outcome": outcome, "why": why,
                           "steps_retired": self._retire_steps(goal, why, now)})
        steps = 0
        for goal in self.goals.approved():
            candidate = self.goals.next_step(goal)
            if candidate is None:
                continue
            self.concerns.bump(drive=candidate.drive, kind="goal_step", summary=candidate.concern,
                               dedup_key=candidate.dedup_key, salience=candidate.salience,
                               sources=candidate.evidence, detail=candidate.as_detail(), now=now)
            steps += 1
        return {"open": len(self.goals.open()), "closed": closed, "steps_raised": steps}

    def _retire_steps(self, goal: StoredInitiative, why: str, now: datetime) -> int:
        retired = 0
        for step in self.goals.steps(goal):
            if step.status in {"proposed", "asked", "approved"}:
                self.outcomes.record(step.id, status="cancelled", summary=f"goal closed: {why}", verified="none",
                                     by="mind", implicit_verdict=False)
                retired += 1
        for concern in self.concerns.open(limit=10000):
            if concern.detail.get("parent_goal_id") == goal.id:
                self.concerns.drop(concern.id, note="goal closed", now=now)
                retired += 1
        return retired

    # -- forming intentions ------------------------------------------------------------------

    async def _act(self, now: datetime) -> tuple[List[Dict[str, Any]], int]:
        """The top concerns, ranked on the effective score, become intentions through authority."""
        pairs = []
        for concern in self.concerns.top(k=BROADCAST):
            if not concern.detail.get("type"):
                continue
            if self.store.get_by_dedup_key(concern.dedup_key) is not None:
                # The obligation was reported once already (architecture 3.3); it does not compete again.
                self.concerns.drop(concern.id, note="already intended", now=now)
                continue
            pairs.append((concern, Candidate.from_detail(concern.detail)))
        by_key = {candidate.dedup_key: concern for concern, candidate in pairs}
        weights = self.effective_weights()
        # The configured weight is factored out of the threshold; satiation is not, for self-chosen work (rank.py).
        ranked = eligible([candidate for _, candidate in pairs], threshold=self.act_threshold, drives=weights,
                          base=self.drive_weights, feedback=self.feedback)
        formed: List[Dict[str, Any]] = []
        for candidate, score in ranked:
            concern = by_key.get(candidate.dedup_key)
            if concern is None:
                continue
            may_adopt = (self.goals.may_adopt() and candidate.drive in {"curiosity", "mastery"}
                         and not candidate.parent_goal_id and not self.goals.taken(candidate))
            steps_done: List[str] = []
            if candidate.parent_goal_id:
                goal = self.store.get(candidate.parent_goal_id)
                if goal is None or goal.status != "approved":
                    self.concerns.drop(concern.id, note="goal not open", now=now)
                    continue
                steps_done = self.goals.summaries(goal)
            shaped = await self.deliberation.form(concern, candidate, open_goals=len(self.goals.open()),
                                                  may_adopt_goal=may_adopt, steps_done=steps_done)
            if shaped.open_ended and not shaped.text:
                continue  # the tick's one call is spent; the concern waits for the next tick
            if shaped.kind == "goal":
                goal_candidate = self.goals.candidate(shaped)
                if goal_candidate is None:
                    shaped.kind, shaped.goal = "task", None  # the budget is full or the topic has its goal: one task
                else:
                    shaped = goal_candidate
            row = await self._form(shaped, score, now)
            if row is None:
                # A thought was spent and formed nothing: charge it (anti-rumination bounds the retries)
                # and keep the tokens of the call on the books even without an intention row.
                self.concerns.progress(concern.id, progressed=False, note="not formed", now=now)
                self._charge_unformed(shaped, concern, now)
                continue
            self.concerns.intended(concern.id, row.id, now=now)
            if row.kind == "note":
                # A note has no body to run and nothing to wait for: it is what the mind concluded.
                self.outcomes.record(row.id, status="done", summary=shaped.text, verified="none", by="mind")
                row = self.store.get(row.id) or row
            elif row.kind == "goal":
                self.autobiography.record(row.id, "goal_adopted",
                                          f"I adopted a goal: {row.description} ({row.drive} drive), to be met by "
                                          f"{(row.due_at or row.expires_at).date().isoformat() if (row.due_at or row.expires_at) else 'its horizon'}.")
            formed.append({"id": row.id, "type": row.type, "kind": row.kind, "drive": row.drive,
                           "decision": row.decision, "status": row.status, "score": round(score, 3),
                           "concern": concern.id})
        return formed, len(pairs) - len(ranked)

    def _charge_unformed(self, candidate: Candidate, concern: Concern, now: datetime) -> None:
        """The tokens of a deliberation call that formed no intention, as an audit note (the daily
        token budget reads ``cost_tokens`` off the rows)."""
        if not candidate.cost_tokens:
            return
        row, created = self.store.create_intention(
            kind="note", type="deliberation", title=f"thought about: {concern.summary}"[:160], drive=candidate.drive,
            cls="internal", decision="act", decision_reason="formed no intention", status="done", dedup_key=None,
            hermes_kind="none", context={"concern": concern.summary, "evidence": list(candidate.evidence)},
            source_type="concern", source_id=concern.id, created_at=now)
        self.store.update(row.id, cost_tokens=int(candidate.cost_tokens))
        self.store.transition(row.id, "done", action="deliberated", outcome="done", verified="none",
                              completed_at=now, at=now)

    async def _may_contact(self, recipient: str | None) -> str:
        if not recipient:
            return "ask"
        if self.owner_id and recipient == self.owner_id:
            return "auto"
        contact = None
        if self.contacts is not None:
            try:
                contact = await self.contacts.get(recipient)
            except Exception as error:
                logger.debug("contact %s unavailable (%s)", recipient, type(error).__name__)
        record = contact.to_dict() if hasattr(contact, "to_dict") else contact
        return may_contact_of(record if record is not None else recipient, owner_id=self.owner_id)

    async def _handles(self, recipient: str | None) -> List[Dict[str, Any]]:
        if not recipient or self.contacts is None or not hasattr(self.contacts, "get_handles"):
            return []
        try:
            handles = await self.contacts.get_handles(recipient)
        except Exception:
            return []
        rendered = [{"gateway": h.gateway, "address": h.address, "is_primary": bool(h.is_primary),
                     "verified": bool(h.verified)} for h in handles or []]
        rendered.sort(key=lambda h: (not h["is_primary"], not h["verified"]))
        return rendered[:5]

    async def _form(self, candidate: Candidate, score: float, now: datetime) -> Optional[StoredInitiative]:
        if self.store.get_by_dedup_key(candidate.dedup_key) is not None:
            return None
        may_contact = await self._may_contact(candidate.recipient)
        verdict = self.authority.decide(kind=candidate.kind, recipient=candidate.recipient,
                                        text=f"{candidate.title}\n{candidate.text}", type=candidate.type,
                                        may_contact=may_contact, toolsets=self.policy.worker_toolsets, now=now)
        status = {"act": "approved", "ask": "asked", "drop": "dropped", "defer": "proposed"}[verdict.decision]
        context: Dict[str, Any] = {
            "concern": candidate.concern, "evidence": list(candidate.evidence), "score": round(score, 3),
            "may_contact": may_contact, "notice": verdict.notice,
        }
        if candidate.topic:
            context["topic"] = candidate.topic
        if candidate.kind == "message":
            context["text"] = candidate.text
            context["recipient_handles"] = await self._handles(candidate.recipient)
        elif candidate.kind == "goal":
            context.update(candidate.goal or {})
            context["description"] = candidate.text
        else:
            context["body"] = candidate.text
            context["max_runtime_seconds"] = self.policy.budgets.task_max_runtime_s
            context["max_retries"] = self.policy.budgets.task_max_retries
            if candidate.parent_goal_id:
                context["goal_mode"] = True
                context["goal_max_turns"] = DEFAULT_MAX_TURNS
        horizon = candidate.due_at if candidate.kind == "goal" else None
        expires_at = ask_expiry(now, self.policy) if verdict.decision == "ask" else (horizon or now + TASK_WINDOW)
        code = new_ask_code(self.store.open_ask_codes()) if verdict.decision == "ask" else None
        row, created = self.store.create_intention(
            kind=candidate.kind, type=candidate.type, title=candidate.title, drive=candidate.drive, cls=verdict.cls,
            decision=verdict.decision, decision_reason=verdict.reason, status=status, dedup_key=candidate.dedup_key,
            rationale=candidate.rationale, recipient=candidate.recipient, context=context,
            priority=candidate.priority, expires_at=expires_at, due_at=_utc(candidate.due_at), ask_code=code,
            invalidates_if=candidate.invalidates_if, success_check=candidate.success_check,
            hermes_kind="none", source_type=candidate.source_type, source_id=candidate.source_id, created_at=now)
        if created != "created":
            return None
        extra: Dict[str, Any] = {}
        if candidate.parent_goal_id:
            extra["parent_goal_id"] = candidate.parent_goal_id
        if candidate.dedup_base:
            extra["dedup_base"] = candidate.dedup_base
        if candidate.cost_tokens:
            extra["cost_tokens"] = int(candidate.cost_tokens)
        if extra:
            self.store.update(row.id, **extra)
        return self._apply_decision(row, verdict, now, reconsidered=False, code=code)

    def _apply_decision(self, row: StoredInitiative, verdict: Any, now: datetime, *, reconsidered: bool,
                        code: str | None = None) -> Optional[StoredInitiative]:
        decision = verdict.decision
        if decision == "act":
            window = (row.due_at or row.expires_at) if row.kind == "goal" else None
            updated = self.store.transition(row.id, "approved", action="queued", at=now, decision="act",
                                            decision_reason=verdict.reason, cls=verdict.cls,
                                            expires_at=window or now + TASK_WINDOW)
        elif decision == "ask":
            code = code or row.ask_code or new_ask_code(self.store.open_ask_codes())
            updated = self.store.transition(row.id, "asked", action="asked", at=now, decision="ask",
                                            decision_reason=verdict.reason, cls=verdict.cls, ask_code=code,
                                            expires_at=ask_expiry(now, self.policy),
                                            details={"code": code, "notice": verdict.notice})
        elif decision == "drop":
            updated = self.store.transition(row.id, "dropped", action="dropped", at=now, decision="drop",
                                            decision_reason=verdict.reason, cls=verdict.cls, outcome="denied",
                                            verified="none", cancelled_at=now, cancelled_reason=verdict.reason)
        else:
            updated = self.store.transition(row.id, "proposed", action="deferred", at=now, decision="defer",
                                            decision_reason=verdict.reason, cls=verdict.cls)
        if updated is None:
            return None
        if decision in {"act", "ask"} and not reconsidered and updated.kind != "note":
            self._register_expectation(updated, now)
        verb = {"act": "will act on", "ask": "asked the owner about", "drop": "dropped", "defer": "deferred"}[decision]
        self.autobiography.record(updated.id, f"decided_{decision}",
                                  f"I {verb} '{updated.description}' ({updated.drive} drive, {updated.cls} class): "
                                  f"{updated.decision_reason}." + (f" Ask code {code}." if decision == "ask" else ""),
                                  decision=decision)
        return updated

    def _register_expectation(self, row: StoredInitiative, now: datetime) -> None:
        if self.expectations is None or not hasattr(self.expectations, "store"):
            return
        horizon = row.expires_at or (now + TASK_WINDOW)
        try:
            prediction = self.expectations.store.create(
                subject=f"intention:{row.id}", domain="intention",
                expectation=f"'{row.description}' completes with outcome done by {horizon.isoformat()}",
                confidence=0.7, horizon=horizon.timestamp(), source="mind", dedup_key=f"intention:{row.id}",
                detail={"intention_id": row.id, "kind": row.kind, "type": row.type})
        except Exception as error:
            logger.warning("expectation not registered for %s (%s)", row.id, type(error).__name__)
            return
        if prediction is not None:
            self.store.update(row.id, expectation_id=prediction.prediction_id)

    def _on_settled(self, row: StoredInitiative, outcome: str, check_result: Optional[bool]) -> None:
        """An outcome settles its concern and satiates its drive (architecture 3.1, 4.5); a finished
        commitment intention also closes the commitment it was raised for."""
        concern = self.concerns.by_intention(row.id)
        now = self.clock()
        if outcome == "done":
            self._close_commitment(row)
            if concern is not None:
                self.concerns.resolve(concern.id, note=f"{row.kind} done", now=now)
            if self.faculties["drives"] and row.drive in DRIVES and row.type not in audit.NOTICE_TYPES:
                self.mind_state.bump(f"satiety.{row.drive}", 1.0, half_life_s=SATIETY_HALF_LIFE_S,
                                     causes=[f"intention:{row.id}"], now=now)
        elif outcome == "failed":
            if concern is not None:
                self.concerns.progress(concern.id, progressed=False, note=str(row.failed_reason or "failed"), now=now)
        elif concern is not None:
            self.concerns.drop(concern.id, note=outcome, now=now)

    def _close_commitment(self, row: StoredInitiative) -> None:
        """The body's report of a done intention is what settles its commitment. The dispatched
        worker cannot mark the row fulfilled itself (the plugin guard blocks that), so the
        resolution names the body and the intention, never the worker's own word."""
        check = row.success_check
        if isinstance(check, str):
            try:
                check = json.loads(check)
            except ValueError:
                return
        if not isinstance(check, dict) or check.get("kind") != "commitment_resolved" or not check.get("commitment_id"):
            return
        resolve = getattr(self.commitments, "resolve", None)
        if not callable(resolve):
            return
        ident = str(check["commitment_id"])
        try:
            current = self.commitments.get(ident)
            if not isinstance(current, dict) or current.get("status") not in {"pending", "overdue"}:
                return
            resolve(ident, "done", note=f"intention {row.id}: {row.result or 'done'}", resolved_by="body")
        except Exception as error:
            logger.warning("commitment %s not closed for %s (%s)", ident, row.id, type(error).__name__)

    # -- digest ----------------------------------------------------------------------------

    def _digest(self, now: datetime) -> Optional[str]:
        local = now.astimezone(self.tz)
        local_date = local.date().isoformat()
        if local.hour < self.digest_hour or self._daily.get("digest") == local_date or self.in_quiet_hours(now):
            return None
        if self.outbox.digest_sent_today(local_date):
            self._daily["digest"] = local_date
            return None
        last = self.store.last_transition_at("queued", type="digest")
        since = last or (now - timedelta(days=1))
        breakers = [self.authority.breaker_state(cls, now) for cls in CLASSES if cls != "floor"]
        text = self.outbox.build_digest(since=since, level=self.level, breaker_states=breakers,
                                        goals=goal_lines([self.goals.render(g) for g in self.goals.open()]))
        self._daily["digest"] = local_date
        if text.endswith("Nothing to report."):
            return None
        row = self.outbox.queue_digest(local_date=local_date, text=text)
        return row.id if row is not None else None

    async def request_message(self, payload: Mapping[str, Any], *, source: str = "reach_out") -> bool:
        """A message another subsystem wants sent, through authority into the outbox.

        This replaces the reach-out delivery path: the text is queued verbatim
        for the owner, or becomes an ask for anyone else the level does not
        cover. True when it was queued or asked; False when nothing was done.
        """
        import hashlib
        text = str(payload.get("message") or payload.get("text") or payload.get("description") or "").strip()
        if not text:
            return False
        recipient = str(payload.get("entity_id") or payload.get("person_id") or payload.get("recipient")
                        or self.owner_id or "") or None
        title = str(payload.get("title") or text[:80]).strip()
        item_id = str(payload.get("id") or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])
        candidate = Candidate(
            type=f"reach_out:{payload.get('type') or source}", drive="duty", kind="message", title=title,
            dedup_key=f"reach_out:{source}:{item_id}", salience=0.9, cost=0.0, recipient=recipient, text=text,
            rationale=f"requested by {source}", evidence=[f"{source}:{item_id}"], concern=title,
            source_type=source, source_id=item_id)
        row = await self._form(candidate, 0.9, self.clock())
        return row is not None and row.status in {"approved", "asked"}

    # -- the body API -------------------------------------------------------------------------

    def dispatch(self) -> List[Dict[str, Any]]:
        """Approved task intentions the body may create now, oldest first; at most once.

        Only ``task`` rows are offered: a goal owns no Hermes object of its own
        (architecture 3.3), its steps are task intentions with kanban ``goal_mode``.
        """
        now = self.clock()
        self.last_pull_at = now
        if not self.enabled:
            return []
        rows = self.store.intentions(status=["approved"], kind=["task"], limit=200)
        payloads = []
        for row in sorted(rows, key=lambda item: item.created_at):
            if self._invalidated(row):
                continue
            context = row.context if isinstance(row.context, dict) else {}
            payloads.append({
                "id": row.id, "kind": row.kind, "type": row.type, "drive": row.drive,
                "dedup_key": f"mind:{row.id}", "idempotency_key": f"mind:{row.id}",
                "title": row.description, "body": context.get("body") or row.description,
                "assignee": WORKER_PROFILE, "recipient": row.entity_id, "reason": row.decision_reason,
                "max_runtime_seconds": int(context.get("max_runtime_seconds") or self.policy.budgets.task_max_runtime_s),
                "max_retries": int(context.get("max_retries") or self.policy.budgets.task_max_retries),
                "goal_mode": bool(context.get("goal_mode")), "goal_max_turns": context.get("goal_max_turns"),
                "parent_goal_id": row.parent_goal_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            })
        return payloads

    def bound(self, intention_id: str, hermes_ref: str, *, hermes_kind: str = "kanban") -> Optional[StoredInitiative]:
        """The body's ack, idempotent: the same reference may be reported again after a lost ack."""
        row = self.store.get(intention_id)
        if row is None or not row.kind:
            return None
        kind = str(hermes_kind or "kanban")
        if row.status == "approved":
            return self.store.transition(intention_id, "dispatched", action="bound", at=self.clock(),
                                         hermes_kind=kind, hermes_ref=str(hermes_ref), assigned_at=self.clock(),
                                         details={"hermes_ref": str(hermes_ref)})
        if not row.hermes_ref and hermes_ref:
            return self.store.update(intention_id, hermes_ref=str(hermes_ref), hermes_kind=kind)
        return row

    async def outbox_ready(self) -> List[Dict[str, Any]]:
        """The messages the body may send now, each re-checked against its source, carrying the
        recipient's handles as they are at this pull rather than as they were when it formed: a
        handle added after a message waited unroutable lets it go out on the next pull."""
        self.last_pull_at = self.clock()
        self._expire_messages(self.last_pull_at)
        ready = []
        for payload in self.outbox.ready(enabled=self.enabled, quiet=self.in_quiet_hours()):
            row = self.store.get(str(payload["id"]))
            if row is None or self._invalidated(row):
                continue
            handles = await self._handles(payload["recipient"])
            if handles != payload["recipient_handles"]:
                context = dict(row.context) if isinstance(row.context, dict) else {}
                self.store.update(row.id, context={**context, "recipient_handles": handles})
                payload["recipient_handles"] = handles
            ready.append(payload)
        return ready

    OBSERVATION_LISTS = (("stale_tasks", "stale_task"), ("blocked_tasks", "blocked_task"), ("goals", "goal"),
                         ("mind_tasks", "mind_task"))

    def observe(self, payload: Any) -> Dict[str, Any]:
        """Board observations from the body: stale owner tasks, blocked tasks, goals, the mind's tasks.

        The body posts ``{observed_at, board, body, counts, stale_tasks, blocked_tasks,
        goals, mind_tasks}`` with ``idle_s`` per task (docs/HERMES-ADAPTER.md); the
        flat ``{observations: [{kind, ...}]}`` shape is accepted too. Stale owner
        tasks and Hermes goals are inputs to the duty drive.
        """
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        if isinstance(payload, dict) and "observations" not in payload:
            for key, kind in self.OBSERVATION_LISTS:
                for item in payload.get(key) or []:
                    if not isinstance(item, dict):
                        continue
                    entry = dict(item)
                    entry.setdefault("kind", kind)
                    if entry.get("age_hours") is None:
                        seconds = entry.get("idle_s") if entry.get("idle_s") is not None else entry.get("age_s")
                        entry["age_hours"] = round(float(seconds or 0) / 3600.0, 2)
                    grouped.setdefault(kind, []).append(entry)
            if isinstance(payload.get("body"), dict):
                self.body_heartbeat = dict(payload["body"])
            if isinstance(payload.get("counts"), dict):
                self.board_counts = {str(k): int(v) for k, v in payload["counts"].items() if isinstance(v, int)}
        else:
            items = payload.get("observations") if isinstance(payload, dict) else payload
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                grouped.setdefault(str(item.get("kind") or "other"), []).append(item)
        self.observations = grouped
        self.observed_at = self.clock()
        self.last_pull_at = self.observed_at
        self._wake.set()
        return {"accepted": sum(len(v) for v in grouped.values()), "kinds": sorted(grouped)}

    def asks(self) -> List[Dict[str, Any]]:
        return audit.log(self.store, status=["asked"], limit=100)

    def answer(self, code: str, *, yes: bool, by: str = "owner", contact_id: str | None = None,
               message: str | None = None) -> Optional[StoredInitiative]:
        """``yes|no <code>``. The sidecar checks what it can: the sender must be the owner
        and, when the owner's message is given, the code must appear in it (7.7)."""
        code = str(code or "").strip().upper()
        if contact_id and self.owner_id and contact_id != self.owner_id:
            raise PermissionError("only the owner can answer an ask")
        if message is not None and code not in str(message).upper():
            raise PermissionError("the ask code must appear in the owner's own message")
        row = self.store.get_by_ask_code(code)
        if row is None:
            return None
        now = self.clock()
        if not yes:
            updated = self.outcomes.record(row.id, status="denied", summary=f"the owner said no ({by})", by=by)
            return updated
        updated = self.store.transition(row.id, "approved", action="queued", at=now, verdict="actioned",
                                        expires_at=now + TASK_WINDOW, details={"by": by, "code": code})
        if self.feedback is not None:
            try:
                self.feedback.record(f"{row.type}:{row.drive}", "actioned", source=row.id)
            except Exception:
                pass
        self.autobiography.record(row.id, "approved", f"The owner approved '{row.description}' (code {code}).")
        return updated

    def rate(self, intention_id: str, verdict: str, *, by: str = "owner") -> Optional[StoredInitiative]:
        return self.outcomes.rate(intention_id, verdict, by=by)

    async def _recipient_of(self, args: Mapping[str, Any]) -> str:
        """The contact a messaging call reaches: ``contact_id``, or the handle behind ``platform`` +
        ``target|chat_id|to``, or the stock ``target="platform:chat_id[:thread_id]"``."""
        recipient = str(args.get("contact_id") or "")
        if recipient or self.contacts is None:
            return recipient
        platform = str(args.get("platform") or "")
        address = str(args.get("target") or args.get("chat_id") or args.get("to") or "")
        if not platform and ":" in address:
            platform, address = split_target(address)
        if not platform or not address:
            return ""
        try:
            contact = await self.contacts.resolve_handle(platform, address)
        except Exception:
            return ""
        return getattr(contact, "contact_id", "") or ""

    async def guard(self, *, tool: str, args: Mapping[str, Any] | None, run: str = "mind",
                    session_id: str = "", recipients: Iterable[str] | None = None) -> Dict[str, Any]:
        """``POST /v1/mind/guard``: the sidecar half of the plugin guard (7.5).

        A messaging tool's recipient comes from its arguments; ``recipients``
        are the contacts an effect reaches later (a delivering cron job),
        resolved by the plugin. Every one of them is authorized like an
        immediate message: ``may_contact`` and the message budgets, the most
        restrictive recipient deciding.
        """
        args = dict(args or {})
        wanted = [str(item) for item in (recipients or []) if str(item)]
        if tool in MESSAGING_TOOLS:
            recipient = await self._recipient_of(args)
            if not recipient:
                return {"allow": False, "action": "block", "reason": "messaging an unknown recipient"}
            wanted = [recipient]
        may_contact = None
        for recipient in wanted:
            permission = await self._may_contact(recipient)
            if permission != "never":
                budget = self.authority.budget_check(kind="message", recipient=recipient)
                if budget:
                    return {"allow": False, "action": "block", "reason": budget}
            if may_contact is None or MAY_CONTACT.index(permission) < MAY_CONTACT.index(may_contact):
                may_contact = permission
        return self.authority.guard(tool=tool, args=args, run=run, recipient_may_contact=may_contact)

    # -- what the mind shows ----------------------------------------------------------------

    def broadcast(self) -> List[Concern]:
        """The top 3 concerns, when the broadcast faculty is on (architecture 4.5)."""
        if not self.faculties["broadcast"] or not self.enabled:
            return []
        return self.concerns.top(k=BROADCAST)

    def section(self, *, limit: int = MIND_SECTION_CHARS) -> str:
        """The Mind section of an owner turn's context: at most ``limit`` characters.

        The affect line, stances and lessons join it with their milestones.
        """
        if not self.enabled:
            return ""
        lines: List[str] = []
        broadcast = self.broadcast()
        if broadcast:
            lines.append("On my mind: " + "; ".join(f"{c.summary}"[:120] for c in broadcast) + ".")
        goals = [self.goals.render(goal) for goal in self.goals.open()]
        if goals:
            lines.append("Working toward: " + "; ".join(goal_lines(goals)) + ".")
        asks = self.store.intentions(status=["asked"], limit=5)
        if asks:
            lines.append("Waiting for your say on: " + "; ".join(
                f"[{row.ask_code}] {row.description}"[:100] for row in asks if row.ask_code) + ".")
        text = "\n".join(lines)
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return text

    def state(self) -> Dict[str, Any]:
        now = self.clock()
        asks = [{"id": row.id, "code": row.ask_code, "ask_code": row.ask_code, "title": row.description,
                 "kind": row.kind, "decision_reason": row.decision_reason,
                 "expires_at": row.expires_at.isoformat() if row.expires_at else None}
                for row in self.store.intentions(status=["asked"], limit=500)]
        weights = self.effective_weights()
        return {
            "enabled": self.enabled,
            "autonomy": self.level,
            "off_reason": self.off_reason,
            "queued": len(self.store.intentions(status=["approved"], kind=["task"], limit=500)),
            "outbox": len(self.store.intentions(status=["approved"], kind=["message"], limit=500)),
            "asks": asks,
            "open_asks": len(asks),
            "dispatched": len(self.store.intentions(status=["dispatched"], limit=500)),
            "ticks": self.ticks,
            "last_tick": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_pull": self.last_pull_at.isoformat() if self.last_pull_at else None,
            "body_stale": self.body_stale(now),
            "quiet_hours": self.in_quiet_hours(now),
            "breaker": [self.authority.breaker_state(cls, now) for cls in CLASSES if cls != "floor"],
            "budgets": vars(self.policy.budgets),
            "faculties": dict(self.faculties),
            "drives": {name: {"weight": self.drive_weights.get(name, 0.0), "effective": weights.get(name, 0.0),
                              "level": self.drive_levels.get(name, 0.0),
                              "satiety": float((self.mind_state.get(f"satiety.{name}") or {}).get("level") or 0.0)}
                       for name in DRIVES},
            "concerns": {"open": self.concerns.count("open"), "intended": self.concerns.count("intended"),
                         "broadcast": [c.as_dict() for c in self.broadcast()]},
            "goals": [self.goals.render(goal) for goal in self.goals.open()],
            "interests": [{"topic": item.get("text"), "weight": item.get("level")} for item in self.mind_state.items("interest:")],
            "deliberation": {"available": self.deliberation.available, "calls_this_tick": self.deliberation.calls_this_tick,
                             "calls_total": self.deliberation.calls_total, "tokens_total": self.deliberation.tokens_total,
                             "last_error": self.deliberation.last_error},
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "body": self.body_heartbeat or None,
            "running": self._running,
        }

    def stats(self) -> Dict[str, Any]:
        return audit.stats(self.store, now=self.clock())


__all__ = ["DEFAULT_FACULTIES", "DRAIN_FORCED_S", "DRAIN_TIMER_S", "DUE_TYPES", "MIND_SECTION_CHARS", "Mind",
           "OFF_MARKER", "STALE_AFTER", "TASK_WINDOW", "THREADED_PLATFORMS", "WORKER_PROFILE", "faculties_of",
           "split_target", "task_body"]
