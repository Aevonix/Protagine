"""The audit log: ``log``, ``why`` and ``stats`` over the initiatives table (architecture 7.8).

Every intention row already holds the drive, concern, evidence, class,
decision, reason, Hermes reference, outcome, verification and verdict. This
module only renders them, with secrets redacted through ``P/redact``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from protagine.redact import redact_sensitive_text

from protagine.initiatives.models import StoredInitiative

MAX_TEXT = 400
# The mind's reporting to the owner: never a drive's work, never a contact message. The last four
# arrived with the people milestone: a grant the owner gave over a ``never`` contact, a recipient the
# owner named that the store cannot resolve, and a name-only identity link or cadence match that needs
# the owner's word. A contradiction question (the memory milestone) is not a notice: it is an action.
NOTICE_TYPES = ("ask_notice", "digest", "breaker_notice", "health_notice", "grant_refused", "recipient_unknown",
                "link_proposal", "cadence_confirm")
ACTION_KINDS = ("task", "goal", "message")


def _clip(text: Any, limit: int = MAX_TEXT) -> str:
    value = "" if text is None else str(text)
    value = redact_sensitive_text(value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _context(row: StoredInitiative) -> Dict[str, Any]:
    return row.context if isinstance(row.context, dict) else {}


def _when(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def entry(row: StoredInitiative) -> Dict[str, Any]:
    """One audit row as the API and the CLI show it."""
    context = _context(row)
    return {
        "id": row.id,
        "created_at": _when(row.created_at),
        "kind": row.kind,
        "type": row.type,
        "drive": row.drive,
        "cls": row.cls,
        "title": _clip(row.description, 160),
        "concern": _clip(context.get("concern"), 200),
        "evidence": [_clip(item, 160) for item in (context.get("evidence") or [])][:8],
        "recipient": row.entity_id,
        "decision": row.decision,
        "decision_reason": _clip(row.decision_reason, 200),
        "status": row.status,
        "ask_code": row.ask_code,
        "hermes_kind": row.hermes_kind,
        "hermes_ref": row.hermes_ref,
        "outcome": row.outcome,
        "verified": row.verified,
        "verdict": row.verdict,
        "summary": _clip(row.result, 300),
        "cost_tokens": row.cost_tokens,
        "due_at": _when(row.due_at),
        "expires_at": _when(row.expires_at),
    }


def is_action(entry: Dict[str, Any]) -> bool:
    """Whether an audit row is one of the agent's own actions: a task, goal or message it decided to act on
    or ask about. Internal notes (the nightly consolidation, a deliberation that formed nothing, owner
    switches) and notices (digest, ask notice, breaker and health notices) are not. The self-narrative's
    evidence and the benchmark's record of what the agent did both use this one predicate."""
    return (entry.get("kind") in ACTION_KINDS and entry.get("decision") in {"act", "ask"}
            and entry.get("type") not in NOTICE_TYPES)


def log(store: Any, *, limit: int = 20, since: Optional[datetime] = None,
        status: Optional[List[str]] = None, kind: Optional[List[str]] = None,
        recipient: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest first; every filter, ``recipient`` included, selects before ``limit`` applies."""
    extra = {"recipient": recipient} if recipient else {}
    return [entry(row) for row in store.intentions(status=status, kind=kind, since=since, limit=limit, **extra)]


def why(store: Any, intention_id: str) -> Optional[Dict[str, Any]]:
    """One row rendered as a sentence, with its fields alongside."""
    row = store.get(intention_id)
    if row is None or not row.kind:
        return None
    value = entry(row)
    history = [{"at": item.timestamp.isoformat(), "action": item.action, "details": item.details}
               for item in reversed(store.get_history(intention_id, limit=50))]
    parts = [f"{value['drive'] or 'unknown'} drive"]
    if value["concern"]:
        parts.append(f"concern: {value['concern']}")
    if value["evidence"]:
        parts.append("evidence: " + "; ".join(value["evidence"]))
    parts.append(f"class {value['cls']}, decided {value['decision']} ({value['decision_reason']})")
    if value["hermes_ref"]:
        parts.append(f"Hermes {value['hermes_kind']} {value['hermes_ref']}")
    elif value["ask_code"] and value["status"] == "asked":
        parts.append(f"waiting for the owner (code {value['ask_code']})")
    parts.append(f"outcome {value['outcome'] or value['status']}")
    parts.append(f"verified: {value['verified'] or 'none'}")
    if value["verdict"]:
        parts.append(f"owner verdict {value['verdict']}")
    value["sentence"] = f"{value['title']}: " + ", ".join(parts) + "."
    value["history"] = history
    value["text"] = value["sentence"]
    return value


def stats(store: Any, *, now: Optional[datetime] = None, days: int = 7,
          ledger_turns: Optional[int] = None) -> Dict[str, Any]:
    """The in-vivo panel (evals section 8), descriptive only."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    rows = store.intentions(since=since, limit=5000)
    acted = [row for row in rows if row.decision == "act" and row.type not in NOTICE_TYPES
             and row.kind in {"task", "message", "goal"}]
    asks = [row for row in rows if row.decision == "ask"]
    notices = [row for row in rows if row.kind == "message" and row.type in NOTICE_TYPES]
    done = [row for row in rows if row.outcome == "done"]
    failed = [row for row in rows if row.outcome == "failed"]
    verdicts = [row for row in rows if row.verdict]
    accepted = [row for row in verdicts if row.verdict in {"actioned", "useful"}]
    verified = [row for row in done if row.verified in {"owner", "check"}]
    commitments = [row for row in rows if row.type == "commitment_overdue"]
    off_uses = [row for row in rows if row.type == "off_switch"]
    blocked = [row for row in rows if row.decision == "defer"]
    by_class: Dict[str, int] = {}
    for row in failed:
        by_class[row.cls or "unknown"] = by_class.get(row.cls or "unknown", 0) + 1
    tokens = sum(int(row.cost_tokens or 0) for row in rows)
    span = max(1.0, (now - since).total_seconds() / 86400.0)
    return {
        "window_days": days,
        "intentions": len(rows),
        "acted": len(acted),
        "asks": len(asks),
        "asks_per_day": round(len(asks) / span, 2),
        "notices_per_day": round(len(notices) / span, 2),
        "initiative_acceptance": (round(len(accepted) / len(verdicts), 3) if verdicts else None),
        "commitment_fulfilment": (round(sum(1 for row in commitments if row.outcome == "done") / len(commitments), 3)
                                  if commitments else None),
        "corrections_per_100_turns": (round(100.0 * sum(1 for row in verdicts if row.verdict == "wrong")
                                            / ledger_turns, 2) if ledger_turns else None),
        "blocked_work": len(blocked),
        "off_switch_uses": len(off_uses),
        "verified_share": (round(len(verified) / len(done), 3) if done else None),
        "lesson_use_rate": None,
        "failures_per_class_per_week": {cls: round(count / span * 7, 2) for cls, count in by_class.items()},
        "mind_tokens": tokens,
    }


def render_log(entries: List[Dict[str, Any]]) -> str:
    lines = []
    for item in entries:
        when = (item.get("created_at") or "")[:16].replace("T", " ")
        tail = item.get("outcome") or item.get("status")
        code = f" [{item['ask_code']}]" if item.get("ask_code") and item.get("status") == "asked" else ""
        lines.append(f"{when}  {item['id'][:8]}  {item['kind']:<7} {item['decision']:<5} {tail:<10} "
                     f"{item['drive']}/{item['type']}: {item['title']}{code}")
    return "\n".join(lines) if lines else "(no intentions yet)"


def render_stats(value: Dict[str, Any]) -> str:
    return "\n".join(f"{key}: {json.dumps(item) if isinstance(item, (dict, list)) else item}"
                     for key, item in value.items())


__all__ = ["ACTION_KINDS", "NOTICE_TYPES", "entry", "is_action", "log", "render_log", "render_stats", "stats", "why"]
