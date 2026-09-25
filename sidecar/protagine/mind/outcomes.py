"""Outcomes: reconciliation, verification, expectations, feedback, the breaker and the autobiography.

Architecture 3.1 (outcome events), 3.3 (``success_check`` and ``verified``),
4.1 (autobiography) and 4.6 (implicit feedback). The body reports the state of
every ``mind:*`` task from its run row; that report is the source of truth
for ``outcome``. ``verified`` records which verifier ran: the owner, a
deterministic check over mind-observable state, or a Hermes failure. Only a
check that actually ran counts, and only the mind grants ``owner`` or
``check``: the body may report a Hermes failure (``BODY_VERIFIERS``), and only
with a reason; a blocked task with a reason is a Hermes failure too.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from protagine.initiatives.models import StoredInitiative
from protagine.util.temporal import now_utc

from .rank import CHECK_IN_TYPES, OUTREACH_ANSWER, OUTREACH_TOPIC, OUTREACH_TYPES

logger = logging.getLogger(__name__)

# The body's task states -> the intention outcome. ``None`` is progress only.
STATUS_TO_OUTCOME: Dict[str, Optional[str]] = {
    "done": "done", "completed": "done", "complete": "done", "success": "done",
    "failed": "failed", "error": "failed", "failure": "failed",
    "blocked": "blocked",
    "archived": "cancelled", "cancelled": "cancelled", "canceled": "cancelled",
    "expired": "expired", "timeout": "expired", "denied": "denied",
    "running": None, "in_progress": None, "claimed": None, "todo": None, "ready": None,
    "triage": None, "dispatched": None, "uncertain": "uncertain",
}
TERMINAL_OUTCOMES = frozenset({"done", "failed", "expired", "denied", "cancelled", "uncertain"})
IMPLICIT_VERDICT = {"cancelled": "dismissed", "expired": "ignored", "denied": "dismissed"}
VERDICTS = ("actioned", "dismissed", "ignored", "useful", "not_useful", "wrong")
# Task types whose completion summary is a finding the agent keeps (architecture 4.5): research and
# investigations write what they learned as an autobiography entry a later turn recalls. A follow-up the
# owner asked for in reply to an outreach is research too (architecture 4.10).
FINDING_TYPES = frozenset({"research", "question", "mastery_investigation", "goal_step", "outreach_followup"})
FINDING_CHARS = 800
OWNER_EVIDENCE = ("appraisal:", "turn:", "claim:")   # evidence read from what the owner said
VERIFIERS = frozenset({"owner", "check", "hermes_failure", "none"})
# What a body report may claim (architecture 4.8): a Hermes failure with a reason. ``owner`` and
# ``check`` are the mind's to grant; a claimed one is ignored and the verifier computed.
BODY_VERIFIERS = frozenset({"hermes_failure"})


def _reason(summary: Any, error: Any) -> str:
    return str(error or summary or "").strip()


# Stock kanban's words for a run it killed at ``max_runtime_seconds`` (kanban_db_dispatch.enforce_max_runtime).
_RUNTIME_LIMIT = re.compile(r"^elapsed \d+s > limit \d+s$")


def timed_out(run: Any, error: Any = None) -> bool:
    """True when the reported run was stopped at its runtime limit."""
    if isinstance(run, dict) and "timed_out" in {str(run.get("outcome") or ""), str(run.get("status") or "")}:
        return True
    return bool(_RUNTIME_LIMIT.match(str(error or "").strip()))


def hermes_reason(row: StoredInitiative) -> str:
    """The reason Hermes gave for a failed or blocked task it reported, or ``""`` when the row is not
    a Hermes failure with a reason (what the nightly lesson packet quotes)."""
    if row is None or row.verified != "hermes_failure" or row.outcome not in {"failed", "blocked"}:
        return ""
    return str(row.failed_reason or row.result or "").strip()


class Autobiography:
    """Owner-audience ledger entries with ``origin='mind'`` (architecture 4.1).

    ``scope='person'`` under the owner contact, role ``assistant``, no claim
    extraction: ordinary recall finds them in any later session, and nothing
    the mind did is ever read as something the owner said.
    """

    SESSION = "mind"
    # Intentions built from what people said (a contradiction question quotes two statements): the record
    # names them without those words.
    QUOTING_TYPES = frozenset({"contradiction"})

    def __init__(self, ledger: Any, *, owner_id: str | None, clock=None) -> None:
        self.ledger = ledger
        self.owner_id = owner_id
        self.clock = clock or (lambda: now_utc())

    @classmethod
    def name(cls, row: StoredInitiative) -> str:
        """How the record names an intention: its title, except for one that quotes people's statements.
        Erasure follows source lineage only inside one contact's sources, so an owner-side entry that
        repeated another person's words would outlive that person's erasure."""
        if row.type in cls.QUOTING_TYPES:
            return "a question about two recorded statements that disagree"
        return row.description

    def record(self, intention_id: str, event: str, text: str, *, contact_id: str | None = None,
               lineage: Sequence[str] = (), scope: str = "person", **metadata: Any) -> bool:
        """One entry under the owner, or under ``contact_id`` (an episode summary lands with its own
        contact, so that person's later sessions recall it); never a claim. ``lineage``: the audience's
        own source turns the text was made from, recorded as the entry's supplied sources, so erasing any
        of them erases the entry too (the ledger's erasure closure). ``scope='session'`` keeps an entry
        out of every other session's recall (a lesson is the mind's working record, not a memory)."""
        audience = contact_id or self.owner_id
        if self.ledger is None or not audience or not text.strip():
            return False
        now = self.clock()
        message = {"role": "assistant", "content": text.strip(),
                   "metadata": {"origin": "mind", "intention_id": intention_id, "event": event, **metadata}}
        try:
            if lineage:
                message["_supplied_sources"] = self.ledger.source_references(
                    list(lineage), contact_id=audience, session_id=self.SESSION)
            return bool(self.ledger.record_source(
                f"mind:{intention_id}:{event}", contact_id=audience, session_id=self.SESSION,
                messages=[message], scope=scope, occurred_at=now.isoformat(), derive_claims=False))
        except Exception as error:
            logger.warning("autobiography entry not written (%s)", type(error).__name__)
            return False


def evaluate_check(check: Any, *, commitments: Any = None, followups: Any = None,
                   summary: str = "", result: Any = None, steps_done: int | None = None) -> Optional[bool]:
    """A deterministic check over state the mind can observe without tools.

    Returns True or False when the check ran, None when it cannot run (an
    unknown kind or a store that is not wired), so ``verified`` stays honest.
    ``steps_done`` is the number of finished steps of an agent-owned goal,
    for the ``steps_done`` check kind.
    """
    if isinstance(check, str):
        try:
            check = json.loads(check)
        except ValueError:
            return None
    if not isinstance(check, dict):
        return None
    kind = str(check.get("kind") or "")
    if kind == "commitment_resolved":
        if commitments is None or not check.get("commitment_id"):
            return None
        row = commitments.get(str(check["commitment_id"]))
        if row is None:
            return None
        # Only a resolution from outside the intention (the owner, the conversation) verifies it:
        # the worker cannot close the row itself, and the body's report closes it after this check
        # runs, so an open row says nothing about whether the work was done.
        return True if row.get("status") == "fulfilled" else None
    if kind == "reply_recorded":
        if followups is None or not check.get("wait_id"):
            return None
        try:
            row = followups.get(str(check["wait_id"]))
        except ValueError:
            return None
        return bool(row.get("reply"))
    if kind == "result_field":
        field = str(check.get("field") or "")
        if not field:
            return None
        if isinstance(result, dict) and field in result:
            return bool(result[field])
        text = summary or ""
        return bool(re.search(rf"(?im)(^|[\s\"'*_]){re.escape(field)}\s*[:=]", text)) or f'"{field}"' in text
    if kind == "steps_done":
        if steps_done is None:
            return None
        try:
            return int(steps_done) >= int(check.get("count") or 1)
        except (TypeError, ValueError):
            return None
    return None


def invalidation_reason(condition: Any, *, commitments: Any = None, followups: Any = None) -> Optional[str]:
    """Why an intention's ``invalidates_if`` condition holds now, or None while it does not.

    ``commitment:<id>:resolved`` holds once the commitment is fulfilled or
    cancelled; ``reply_wait:<id>:reply`` once the awaited reply is recorded.
    An unknown condition, or a store that is not wired, never invalidates.
    """
    if not isinstance(condition, str) or condition.count(":") < 2:
        return None
    kind, _, rest = condition.partition(":")
    ident, _, event = rest.rpartition(":")
    if not ident:
        return None
    if kind == "commitment" and event == "resolved" and commitments is not None:
        row = commitments.get(ident)
        status = row.get("status") if isinstance(row, dict) else None
        return f"commitment {ident} is {status}" if status in {"fulfilled", "cancelled"} else None
    if kind == "reply_wait" and event == "reply" and followups is not None:
        try:
            row = followups.get(ident)
        except ValueError:
            return None
        return f"reply wait {ident} has its reply" if isinstance(row, dict) and row.get("reply") else None
    return None


class Outcomes:
    def __init__(self, store: Any, *, authority: Any = None, feedback: Any = None, expectations: Any = None,
                 commitments: Any = None, followups: Any = None, autobiography: Autobiography | None = None,
                 clock=None) -> None:
        self.store = store
        self.authority = authority
        self.feedback = feedback
        self.expectations = expectations
        self.commitments = commitments
        self.followups = followups
        self.autobiography = autobiography
        self.clock = clock or (lambda: now_utc())
        self.on_breaker_trip = None  # callable(cls, state) set by the tick
        self.on_settled = None       # callable(row, outcome, check_result) set by the tick: concerns and satiation

    # -- reconciliation -------------------------------------------------------------

    def record(self, intention_id: str, *, status: str, outcome: str | None = None, final: bool | None = None,
               hermes_ref: str | None = None, summary: str = "", error: str | None = None,
               verified: str | None = None, result: Any = None, run: Any = None,
               by: str = "body", implicit_verdict: bool = True) -> Optional[StoredInitiative]:
        """Apply one body report. Idempotent: a terminal row is left as it is.

        ``status`` is the Hermes (kanban) status. When the body also names the
        ``outcome`` it read from the task and its run row, that reading is
        used; ``final: False`` (a failed run that Hermes requeued) is progress
        and is logged without settling the intention. ``implicit_verdict``
        False keeps a cancellation the world caused (an obligation that
        resolved itself) from counting as the owner's dismissal of the type.
        """
        row = self.store.get(intention_id)
        if row is None or not row.kind:
            return None
        status_key = str(status or "").strip().lower()
        outcome_key = str(outcome or "").strip().lower()
        if outcome_key:
            resolved = STATUS_TO_OUTCOME.get(outcome_key, outcome_key if outcome_key in TERMINAL_OUTCOMES else "uncertain")
        else:
            resolved = STATUS_TO_OUTCOME.get(status_key, "uncertain" if status_key else None)
        updates: Dict[str, Any] = {}
        if hermes_ref and hermes_ref != row.hermes_ref:
            updates["hermes_ref"] = hermes_ref
            updates["hermes_kind"] = row.hermes_kind if row.hermes_kind not in {None, "none"} else \
                ("message" if row.kind == "message" else "kanban")
        if row.outcome in TERMINAL_OUTCOMES:
            if updates:
                self.store.update(intention_id, **updates)
            return self.store.get(intention_id)
        if final is False and resolved in TERMINAL_OUTCOMES:
            # ``summary`` on its own: whether a retry made progress is read against it (``_breaker_count``).
            return self.store.transition(intention_id, row.status, action=f"run_{resolved}", at=self.clock(),
                                         details={"by": by, "status": status_key, "error": (error or summary or "")[:500],
                                                  "summary": str(summary or "")[:500],
                                                  "run": run if isinstance(run, dict) else None}, **updates)
        if resolved is None:
            if row.status == "approved" and hermes_ref:
                return self.store.transition(intention_id, "dispatched", action="bound", at=self.clock(), **updates)
            if updates:
                self.store.update(intention_id, **updates)
            return self.store.get(intention_id)
        if resolved == "blocked":
            # A block Hermes gives a reason for is a Hermes failure (a pitfall may be learned from it); the
            # row stays open, and a later report settles it and recomputes the verifier.
            reason = _reason(summary, error)
            if reason:
                updates.update(verified="hermes_failure", failed_reason=reason[:2000])
            return self.store.update(intention_id, outcome="blocked", result=summary or error or row.result, **updates)
        return self._settle(row, resolved, summary=summary, error=error, verified=verified, result=result, run=run,
                            by=by, updates=updates, implicit_verdict=implicit_verdict)

    def _settle(self, row: StoredInitiative, outcome: str, *, summary: str, verified: str | None,
                result: Any, by: str, updates: Dict[str, Any], error: str | None = None,
                run: Any = None, implicit_verdict: bool = True) -> Optional[StoredInitiative]:
        now = self.clock()
        check_result = evaluate_check(row.success_check, commitments=self.commitments, followups=self.followups,
                                      summary=summary, result=result) if row.success_check else None
        reason = _reason(summary, error)
        # The mind's own callers name their verifier (an owner switch, a goal it closed); a body report
        # may claim only a Hermes failure, which it is only with a reason.
        claimed = verified in VERIFIERS and (by != "body" or (
            verified in BODY_VERIFIERS and outcome == "failed" and bool(reason)))
        if claimed:
            verifier = verified
        elif outcome == "failed" and reason:
            verifier = "hermes_failure"
        elif check_result is not None:
            verifier = "check"
        else:
            verifier = "none"
        status = {"done": "done", "failed": "failed", "expired": "expired", "denied": "cancelled",
                  "cancelled": "cancelled", "uncertain": "uncertain"}[outcome]
        metadata = dict(row.result_metadata or {})
        if result is not None:
            metadata["result"] = result   # the structured report; a parent goal's check reads it too
        if check_result is not None:
            metadata["check"] = {"passed": check_result, "at": now.isoformat()}
        if isinstance(run, dict):
            metadata["run"] = run
        if error:
            metadata["error"] = str(error)[:500]
        counted = True
        if outcome == "failed":
            counted, why = self._breaker_count(row, run=run, error=error, summary=summary, result=result)
            metadata["breaker"] = {"counted": counted, **({"reason": why} if why else {})}
        stamps = {"done": {"completed_at": now},
                  "failed": {"failed_at": now, "failed_reason": reason or outcome},
                  "expired": {"cancelled_at": now, "cancelled_reason": "expired"},
                  "denied": {"cancelled_at": now, "cancelled_by": by, "cancelled_reason": "denied"},
                  "cancelled": {"cancelled_at": now, "cancelled_by": by, "cancelled_reason": summary or "cancelled"},
                  "uncertain": {}}[outcome]
        # A cancellation while the mind is off is the switch, and one the world caused (the
        # obligation resolved itself) is not the owner's verdict on the type either.
        enabled = getattr(self.authority, "enabled", True) if self.authority is not None else True
        # An expired ask for a check-in is the owner's silence, not the contact's: it teaches nothing.
        implicit_verdict = implicit_verdict and not (outcome == "expired" and row.drive == "social")
        verdict = row.verdict or (IMPLICIT_VERDICT.get(outcome) if enabled and implicit_verdict else None)
        updated = self.store.transition(
            row.id, status, action=f"outcome_{outcome}", at=now,
            details={"by": by, "verified": verifier, "check": check_result},
            outcome=outcome, verified=verifier, result=summary or error or row.result, result_metadata=metadata,
            verdict=verdict, **stamps, **updates)
        self._resolve_expectation(updated, outcome, check_result)
        if verdict and not row.verdict:
            self._feedback(updated, verdict)
        if outcome == "failed" and counted and self.authority is not None and updated is not None:
            self._breaker(updated)
        if self.autobiography is not None and updated is not None:
            self.autobiography.record(updated.id, f"outcome_{outcome}", self._narrate(updated, check_result),
                                      outcome=outcome, verified=verifier)
            if outcome == "done" and updated.type in FINDING_TYPES and str(summary or "").strip():
                self.autobiography.record(updated.id, "finding", self._finding(updated, summary),
                                          topic=self._topic(updated), verified=verifier,
                                          audience=self.finding_audience(updated))
        if callable(self.on_settled) and updated is not None:
            try:
                self.on_settled(updated, outcome, check_result)
            except Exception as error:
                logger.warning("settle hook failed for %s (%s)", updated.id, type(error).__name__)
        return updated

    # -- owner verdicts ---------------------------------------------------------------

    def rate(self, intention_id: str, verdict: str, *, by: str = "owner") -> Optional[StoredInitiative]:
        """An explicit verdict: recorded, fed back to the ranker, and ``verified='owner'``
        when it says whether the outcome was right."""
        verdict = str(verdict or "").strip().lower()
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
        row = self.store.get(intention_id)
        if row is None or not row.kind:
            return None
        updates: Dict[str, Any] = {"verdict": verdict}
        if verdict in {"useful", "not_useful", "wrong"} and row.outcome in TERMINAL_OUTCOMES:
            updates["verified"] = "owner"
        updated = self.store.transition(row.id, row.status, action="rated", details={"verdict": verdict, "by": by},
                                        at=self.clock(), **updates)
        self._feedback(updated, verdict)
        if self.autobiography is not None and updated is not None:
            name = Autobiography.name(updated)
            text = (f"The owner rated '{name}' as {verdict}." if by == "owner"
                    else f"'{name}' was scored {verdict} ({by}).")
            self.autobiography.record(updated.id, "rated", text, verdict=verdict, by=by)
        return updated

    # -- helpers -------------------------------------------------------------------------

    def _feedback(self, row: Optional[StoredInitiative], verdict: str) -> None:
        """One contribution per intention and key: a later verdict on the same intention replaces
        the earlier one (an owner rating after an implicit verdict), it never adds to it."""
        if row is None or self.feedback is None:
            return
        outcome = {"useful": "actioned", "not_useful": "dismissed", "wrong": "dismissed"}.get(verdict, verdict)
        context = row.context if isinstance(row.context, dict) else {}
        if row.type in OUTREACH_TYPES or row.type == OUTREACH_ANSWER:
            # Outreach teaches which kinds of message and which topics the owner values: its type and its
            # topic, never ``reach_out:<owner>``, which weighs every discretionary owner notice.
            keys = [f"{row.type}:{row.drive}"]
            if context.get("topic_slug"):
                keys.append(OUTREACH_TOPIC + str(context["topic_slug"]))
        else:
            # A check-in teaches the timing with that one contact: one silent contact must not lower
            # check-ins with everyone, so a contact check-in feeds only ``reach_out:<contact>``.
            keys = [] if row.drive == "social" and row.type in CHECK_IN_TYPES else [f"{row.type}:{row.drive}"]
            if row.kind == "message" and row.entity_id:
                keys.append(f"reach_out:{row.entity_id}")
        for key in keys:
            try:
                self.feedback.record(key, outcome, source=row.id)
            except Exception as error:
                logger.warning("feedback not recorded for %s (%s)", key, type(error).__name__)

    def _resolve_expectation(self, row: Optional[StoredInitiative], outcome: str,
                             check_result: Optional[bool]) -> None:
        if row is None or self.expectations is None or not row.expectation_id:
            return
        if outcome == "done":
            verdict = "miss" if check_result is False else "hit"
        elif outcome in {"failed", "expired", "denied", "cancelled"}:
            verdict = "miss"
        else:
            return
        try:
            self.expectations.store.resolve(row.expectation_id, verdict)
        except Exception as error:
            logger.warning("expectation %s not resolved (%s)", row.expectation_id, type(error).__name__)

    def _breaker_count(self, row: StoredInitiative, *, run: Any, error: Any, summary: Any,
                       result: Any) -> tuple[bool, str]:
        """Whether a failed task counts toward the breaker (3 in 24 h demote its class), and why not.

        A timeout fails the task (its concern, affect and lessons read the failure) but is not a
        wrong act: the worker was working when its run's budget ran out, a sizing miss the breaker
        cannot fix by demoting the mind for 72 hours. It counts once a retry exists and timed out
        too with nothing new to show (no report summary or structured result beyond what an earlier
        run left): a worker that spends whole runs without progress is a fault to stop. Any other
        failure counts as before.
        """
        if not timed_out(run, error):
            return True, ""
        earlier = [item for item in self.store.get_history(row.id, limit=100) if item.action == "run_failed"]
        if not earlier:
            return False, "timed out on its only run"
        seen = {str((item.details or {}).get(key) or "").strip() for item in earlier for key in ("summary", "error")}
        text = str(summary or "").strip()
        if (text and text not in seen) or (isinstance(result, dict) and result):
            return False, "timed out after progress on its retry"
        return True, "timed out on a retry with no progress"

    def _breaker(self, row: StoredInitiative) -> None:
        state = self.authority.breaker_state(row.cls or "internal")
        if not state.get("tripped"):
            return
        note, created = self.store.create_intention(
            kind="note", type=f"breaker_trip:{row.cls}", title=f"breaker tripped for {row.cls}",
            drive="upkeep", cls="internal", decision="act", decision_reason=(
                f"{state['failures']} failures within the window; {row.cls} asks until {state['until']}"),
            status="done", dedup_key=f"breaker_trip:{row.cls}:{state['until']}", hermes_kind="none",
            created_at=self.clock())
        if created == "created":
            self.store.transition(note.id, "done", action="breaker_trip", outcome="done", verified="none",
                                  completed_at=self.clock(), at=self.clock())
            if callable(self.on_breaker_trip):
                self.on_breaker_trip(row.cls, state)

    @staticmethod
    def finding_audience(row: StoredInitiative) -> str:
        """Who a view formed from this finding may reach: ``all`` only for research into an interest
        of the agent's own. An owner's question, a goal step, an investigation of work done for the
        owner, or an interest the owner's own words raised (evidence from the owner's turns, claims or
        appraisals) is the owner's."""
        context = row.context if isinstance(row.context, dict) else {}
        raised = any(str(ref).startswith(OWNER_EVIDENCE) for ref in context.get("evidence") or [])
        return "all" if row.type == "research" and not raised else "owner"

    @staticmethod
    def _topic(row: StoredInitiative) -> str:
        context = row.context if isinstance(row.context, dict) else {}
        return str(context.get("topic") or context.get("concern") or row.description)[:120]

    @classmethod
    def _finding(cls, row: StoredInitiative, summary: str) -> str:
        text = " ".join(str(summary).split())[:FINDING_CHARS]
        return f"What I learned about {cls._topic(row)}: {text}"

    @staticmethod
    def _narrate(row: StoredInitiative, check_result: Optional[bool]) -> str:
        what = {"task": "task", "message": "message", "goal": "goal", "note": "note"}.get(row.kind or "", "intention")
        text = f"My {what} '{Autobiography.name(row)}' ended {row.outcome}"
        if row.hermes_ref:
            text += f" ({row.hermes_ref})"
        if row.result:
            text += f": {str(row.result)[:300]}"
        if check_result is True:
            text += ". The check confirmed it."
        elif check_result is False:
            text += ". The check found it not achieved."
        elif row.verified == "hermes_failure":
            text += ". Hermes reported the failure."
        else:
            text += ". Unverified."
        return text


__all__ = ["Autobiography", "BODY_VERIFIERS", "FINDING_TYPES", "IMPLICIT_VERDICT", "Outcomes", "STATUS_TO_OUTCOME",
           "TERMINAL_OUTCOMES", "VERDICTS", "VERIFIERS", "evaluate_check", "hermes_reason", "invalidation_reason"]
