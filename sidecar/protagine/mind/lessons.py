"""Lessons: experience memory learned from verified results (architecture 4.8).

A lesson has the ReasoningBank shape (title, when it applies, content, evidence) and is a strategy
(what to do) or a pitfall (what to avoid). It is stored as the mind's own record, never as a
source claim: a family of owner-audience ledger entries in the mind's session
(``scope='session'``), one per event (``mind:lesson:<id>:admitted``, then ``activated``,
``superseded`` or ``retired``). Ordinary recall therefore never shows a lesson as something the
owner said, and each entry's lineage (the owner turns it quotes, the outcome entries it cites)
erases it together with its evidence. There is no table of its own: the lesson is the fold of its
entries.

Uses are joins, not counters: an intention that carried a lesson lists it in its ``lesson_ids``
column (task bodies, deliberation), and a lesson in an owner turn's context leaves one
``lesson_use`` note per session. A use counts only when a verifier that could not be the worker's
own word scored it (``VERIFYING_SOURCES``; a ``result_field`` check reads only the worker's report,
so it is not one, ``EXTERNAL_CHECKS``). ``review`` activates a candidate after a verified win in
its class and retires a lesson that wins under 40% of at least five verified uses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

VERIFYING_SOURCES = ("owner", "check", "hermes_failure")
# The checks that read state the worker cannot write (outcomes.evaluate_check): only these verify a
# lesson. ``result_field`` passes on the worker's own summary, so for lessons it verifies nothing.
EXTERNAL_CHECKS = frozenset({"commitment_resolved", "reply_recorded"})
KINDS = ("strategy", "pitfall")
STATUSES = ("active", "candidate", "superseded", "retired")
CURRENT = ("active", "candidate")
EVENTS = ("admitted", "activated", "superseded", "retired")
TURN_LESSONS, TASK_LESSONS, LINE_CHARS, SECTION_CHARS = 1, 2, 300, 420
TITLE_CHARS, WHEN_CHARS, CONTENT_CHARS = 120, 200, 400
RELEVANCE, MIN_SHARED = 0.34, 2          # share of the lesson's title+when terms found in the query
RETIRE_USES, RETIRE_RATE = 5, 0.4
TALLY_WINDOW = timedelta(days=90)
SESSION = "mind"
PREFIX = "mind:lesson:"
WIN_VERDICTS = frozenset({"useful", "actioned"})
LOSS_VERDICTS = frozenset({"wrong", "not_useful"})
USE_TYPE = "lesson_use"


def _utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return default


def lesson_id(signature: str, kind: str, content: str) -> str:
    """The same lesson admitted twice (a night cut short and run again) is one lesson."""
    return "L-" + hashlib.sha256(f"{signature}|{kind}|{content}".encode()).hexdigest()[:10]


def lesson_ids_of(row: Any) -> List[str]:
    """The lessons an intention row carried."""
    value = _json(getattr(row, "lesson_ids", None), [])
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []


def check_kind(row: Any) -> str:
    check = _json(getattr(row, "success_check", None), None)
    return str(check.get("kind") or "") if isinstance(check, dict) else ""


@dataclass
class Lesson:
    id: str
    signature: str
    kind: str
    title: str
    when_to_use: str
    content: str
    evidence: List[str] = field(default_factory=list)
    verified: str = "none"
    origin: str = "night"
    status: str = "active"
    supersedes: Optional[str] = None
    correction: Optional[str] = None
    retrieval_source: Optional[str] = None
    admitted_at: Optional[str] = None
    closed_reason: Optional[str] = None
    closed_at: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def line(self) -> str:
        """The lesson as one line of a task body or a deliberation prompt."""
        text = f"[lesson {self.id}, {self.kind}] When {self.when_to_use}: {self.content}"
        return text if len(text) <= LINE_CHARS else text[: LINE_CHARS - 1].rstrip() + "…"

    def section(self) -> str:
        """The lesson as an owner turn's context: provenance, the lesson, and when not to apply it."""
        source = "from the owner's verdicts" if self.verified == "owner" else "from verified results"
        head = f"[lesson {self.id}, {source}] When {self.when_to_use}: "
        tail = []
        if self.correction == "retrieval" and self.retrieval_source:
            tail.append(f"The answer was already in your records ({self.retrieval_source}); "
                        "look there before answering.")
        tail.append("Apply it only when the request matches; the owner's word in this conversation comes first.")
        closing_text = " " + " ".join(tail)
        room = SECTION_CHARS - len(head) - len(closing_text)
        content = self.content if len(self.content) <= room else self.content[: max(0, room - 1)].rstrip() + "…"
        return (head + content + closing_text)[:SECTION_CHARS]


class Lessons:
    """The lesson record over the ledger and the uses over the initiative store."""

    def __init__(self, *, ledger: Any, store: Any, owner_id: str | None, autobiography: Any, clock=None,
                 enabled: bool = True) -> None:
        self.ledger = ledger
        self.store = store
        self.owner_id = owner_id or None
        self.autobiography = autobiography
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.enabled = bool(enabled)

    @property
    def available(self) -> bool:
        return self.ledger is not None and bool(self.owner_id)

    # -- the record ------------------------------------------------------------------------

    def _entries(self) -> List[Dict[str, Any]]:
        """Every lesson entry of the owner's mind session, oldest first."""
        if not self.available:
            return []
        with closing(self.ledger._connect()) as conn:
            rows = conn.execute(
                "SELECT turn_id, messages_json, coalesce(occurred_at, ingested_at) AS at FROM turn_sources "
                "WHERE contact_id=? AND session_id=? AND turn_id LIKE ? ORDER BY at, rowid",
                (self.owner_id, SESSION, PREFIX + "%")).fetchall()
        entries = []
        for row in rows:
            try:
                message = json.loads(row["messages_json"])[0]
            except (ValueError, IndexError, TypeError):
                continue
            metadata = message.get("metadata") if isinstance(message, dict) else None
            if not isinstance(metadata, dict):
                continue
            entries.append({"turn_id": row["turn_id"], "at": row["at"], "metadata": metadata})
        return entries

    def _fold(self) -> Dict[str, Lesson]:
        lessons: Dict[str, Lesson] = {}
        for entry in self._entries():
            metadata = entry["metadata"]
            event = str(metadata.get("event") or "")
            if event == "admitted" and isinstance(metadata.get("lesson"), dict):
                data = metadata["lesson"]
                try:
                    lesson = Lesson(**{key: data.get(key) for key in (
                        "id", "signature", "kind", "title", "when_to_use", "content", "verified", "origin",
                        "status", "supersedes", "correction", "retrieval_source")})
                except TypeError:
                    continue
                lesson.evidence = [str(item) for item in data.get("evidence") or []]
                lesson.admitted_at = str(entry["at"])
                lessons[lesson.id] = lesson
                continue
            lesson = lessons.get(str(metadata.get("lesson_id") or ""))
            if lesson is None or event not in EVENTS:
                continue
            if event == "activated" and lesson.status == "candidate":
                lesson.status = "active"
            elif event in {"superseded", "retired"} and lesson.status in CURRENT:
                lesson.status = event
                lesson.closed_reason = str(metadata.get("reason") or "") or None
                lesson.closed_at = str(entry["at"])
        return lessons

    def all(self, *, include_closed: bool = False) -> List[Lesson]:
        """Newest first; current lessons (active and candidate) unless ``include_closed``."""
        lessons = [lesson for lesson in self._fold().values() if include_closed or lesson.status in CURRENT]
        return sorted(lessons, key=lambda item: (item.admitted_at or "", item.id), reverse=True)

    def get(self, ident: str) -> Optional[Lesson]:
        return self._fold().get(str(ident or ""))

    def current(self, signature: str, kind: str) -> Optional[Lesson]:
        """The one current lesson of a signature and kind, if any."""
        return next((lesson for lesson in self.all() if lesson.signature == signature and lesson.kind == kind), None)

    def admit(self, fields: Mapping[str, Any], *, verified: str, origin: str, status: str,
              evidence: Sequence[str], lineage: Sequence[str], supersedes: str | None = None,
              correction: str | None = None, retrieval_source: str | None = None,
              now: datetime | None = None) -> Optional[Lesson]:
        """Write one lesson; an existing one with the same id is returned unchanged. An add that meets the
        current lesson of its signature and kind supersedes it (one current lesson per class and kind)."""
        now = now or self.clock()
        signature = _clean(fields.get("signature"), 160)
        kind = str(fields.get("kind") or "")
        title = _clean(fields.get("title"), TITLE_CHARS)
        when = _clean(fields.get("when_to_use"), WHEN_CHARS)
        content = _clean(fields.get("content"), CONTENT_CHARS)
        if (not self.available or kind not in KINDS or status not in CURRENT
                or not (signature and title and when and content)):
            return None
        ident = lesson_id(signature, kind, content)
        folded = self._fold()
        if ident in folded:
            return folded[ident]
        if supersedes is None:
            head = next((item for item in folded.values() if item.signature == signature and item.kind == kind
                         and item.status in CURRENT), None)
            supersedes = head.id if head is not None else None
        lesson = {"id": ident, "signature": signature, "kind": kind, "title": title, "when_to_use": when,
                  "content": content, "evidence": [str(item) for item in evidence][:12], "verified": verified,
                  "origin": origin, "status": status, "supersedes": supersedes, "correction": correction,
                  "retrieval_source": retrieval_source}
        text = f"Lesson ({kind}): when {when}: {content}"
        written = self.autobiography.record(f"lesson:{ident}", "admitted", text, lineage=list(lineage),
                                            scope="session", memory_kind="procedure", lesson=lesson)
        if not written:
            return None
        if supersedes and supersedes in folded and folded[supersedes].status in CURRENT:
            self.set_status(supersedes, "superseded", reason=f"superseded by {ident}", by=origin, now=now)
        return self.get(ident)

    def set_status(self, ident: str, event: str, *, reason: str, by: str,
                   now: datetime | None = None) -> Optional[Lesson]:
        """``activated``, ``superseded`` or ``retired``: one entry, erased with the lesson it names."""
        lesson = self.get(ident)
        if lesson is None or event not in EVENTS[1:]:
            return None
        if (event == "activated" and lesson.status != "candidate") or (
                event != "activated" and lesson.status not in CURRENT):
            return lesson
        text = f"Lesson {ident} {event}: {_clean(reason, 200)}."
        self.autobiography.record(f"lesson:{ident}", event, text, lineage=[f"{PREFIX}{ident}:admitted"],
                                  scope="session", lesson_id=ident, reason=_clean(reason, 200), by=by)
        return self.get(ident)

    # -- verification and uses -----------------------------------------------------------------

    def verified_source(self, row: Any) -> str:
        """Which verifier stands behind a row for lessons: ``owner``, an external ``check``, a
        ``hermes_failure`` with its reason, or ``none``. A ``result_field`` check is ``none`` here."""
        verified = str(getattr(row, "verified", None) or "none")
        if verified == "check":
            return "check" if check_kind(row) in EXTERNAL_CHECKS else "none"
        return verified if verified in VERIFYING_SOURCES else "none"

    def _check_passed(self, row: Any) -> Optional[bool]:
        if check_kind(row) not in EXTERNAL_CHECKS:
            return None
        metadata = getattr(row, "result_metadata", None) or {}
        passed = (metadata.get("check") or {}).get("passed") if isinstance(metadata, dict) else None
        return passed if isinstance(passed, bool) else None

    def use_result(self, row: Any) -> Optional[str]:
        """A task or goal that carried a lesson: ``win``, ``loss`` or ``None`` (unscored)."""
        outcome, verdict = getattr(row, "outcome", None), getattr(row, "verdict", None)
        source = self.verified_source(row)
        passed = self._check_passed(row)
        if outcome == "done":
            if (verdict in WIN_VERDICTS and source == "owner") or passed is True:
                return "win"
            if passed is False or (verdict in LOSS_VERDICTS and source == "owner"):
                return "loss"
            return None
        if outcome == "failed" and source in VERIFYING_SOURCES:
            return "loss"
        return None

    def _use_rows(self, now: datetime) -> List[Any]:
        rows = getattr(self.store, "lesson_rows", None)
        if not callable(rows):
            return []
        return [row for row in rows(since=now - TALLY_WINDOW)
                if row.kind in {"task", "goal"} or row.type == USE_TYPE]

    def _row_result(self, row: Any) -> Optional[str]:
        if row.type == USE_TYPE:
            use = (row.result_metadata or {}).get("use") if isinstance(row.result_metadata, dict) else None
            result = use.get("result") if isinstance(use, dict) else None
            return result if result in {"win", "loss"} else None
        return self.use_result(row)

    def tally(self, now: datetime | None = None) -> Dict[str, Dict[str, int]]:
        """``{lesson id: {uses, wins, losses, applied}}`` over the window: ``uses`` are the verified ones."""
        now = now or self.clock()
        tallies: Dict[str, Dict[str, int]] = {}
        for row in self._use_rows(now):
            result = self._row_result(row)
            for ident in lesson_ids_of(row):
                entry = tallies.setdefault(ident, {"uses": 0, "wins": 0, "losses": 0, "applied": 0})
                entry["applied"] += 1
                if result is not None:
                    entry["uses"] += 1
                    entry["wins" if result == "win" else "losses"] += 1
        return tallies

    def review(self, now: datetime | None = None, tallies: Mapping[str, Mapping[str, int]] | None = None) -> Dict[str, List[str]]:
        """Retire a lesson under ``RETIRE_RATE`` after ``RETIRE_USES`` verified uses; activate a candidate
        after a verified win in its class. Each change is also an audit note."""
        now = now or self.clock()
        tallies = self.tally(now) if tallies is None else tallies
        changed: Dict[str, List[str]] = {"activated": [], "retired": []}
        for lesson in self.all():
            counts = tallies.get(lesson.id) or {}
            uses, wins = int(counts.get("uses") or 0), int(counts.get("wins") or 0)
            if uses >= RETIRE_USES and wins / uses < RETIRE_RATE:
                reason = f"{wins} wins in {uses} verified uses"
                if self.set_status(lesson.id, "retired", reason=reason, by="mind", now=now) is not None:
                    self._note("lesson_retired", lesson, reason, now)
                    changed["retired"].append(lesson.id)
            elif lesson.status == "candidate" and wins >= 1:
                reason = f"a verified win in its class ({wins} of {uses} verified uses)"
                if self.set_status(lesson.id, "activated", reason=reason, by="mind", now=now) is not None:
                    self._note("lesson_activated", lesson, reason, now)
                    changed["activated"].append(lesson.id)
        return changed

    def _note(self, type_name: str, lesson: Lesson, reason: str, now: datetime) -> None:
        """What ``protagine mind log`` shows of a lesson's change."""
        try:
            row, created = self.store.create_intention(
                kind="note", type=type_name, title=f"{type_name.replace('_', ' ')}: {lesson.title}"[:160],
                drive="mastery", cls="internal", decision="act", decision_reason=reason[:200], status="done",
                dedup_key=f"{type_name}:{lesson.id}", hermes_kind="none", context={"lesson_id": lesson.id},
                created_at=now)
            if created == "created":
                self.store.update(row.id, lesson_ids=[lesson.id])
        except Exception as error:
            logger.warning("lesson note not written (%s)", type(error).__name__)

    def stats(self, now: datetime | None = None) -> Dict[str, Any]:
        """The lessons in the in-vivo panel: by status and source, the correction split and the uses."""
        now = now or self.clock()
        lessons = self.all(include_closed=True)
        by_status = Counter(lesson.status for lesson in lessons)
        tallies = self.tally(now)
        uses = sum(entry["uses"] for entry in tallies.values())
        wins = sum(entry["wins"] for entry in tallies.values())
        return {"enabled": self.enabled, **{status: by_status.get(status, 0) for status in STATUSES},
                "admitted_by_source": dict(Counter(lesson.verified for lesson in lessons)),
                "corrections": dict(Counter(lesson.correction for lesson in lessons if lesson.correction)),
                "uses": uses, "wins": wins, "losses": sum(entry["losses"] for entry in tallies.values()),
                "applied": sum(entry["applied"] for entry in tallies.values()),
                "use_rate": round(wins / uses, 3) if uses else None}


__all__ = ["CURRENT", "EXTERNAL_CHECKS", "KINDS", "LINE_CHARS", "Lesson", "Lessons", "MIN_SHARED", "RELEVANCE",
           "RETIRE_RATE", "RETIRE_USES", "SECTION_CHARS", "STATUSES", "TALLY_WINDOW", "TASK_LESSONS",
           "TURN_LESSONS", "VERIFYING_SOURCES", "lesson_id", "lesson_ids_of"]
