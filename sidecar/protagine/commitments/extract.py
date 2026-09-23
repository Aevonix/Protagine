"""Commitment capture: the ``commitment_extract`` function task in the projection worker.

Architecture 3.1 and 4.6. After every person-scoped turn the projection worker
judges the turn on the sidecar router (no tools, no private endpoint) and
records durable commitments and immediate owed deliverables in the commitment
store, on by default whenever a router is configured. The dedupe against
open and recently rejected items is enforced in code, not only in the prompt:
a repeated mention of an open item is never a second item.

Jobs are durable rows in the ledger database (``commitment_runs``), claimed
with a lease exactly like the appraisal and judgment projections, so a
process loss resumes from SQLite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from contextlib import closing
from typing import Any, Dict, List, Optional

from protagine.util.model_output import final_text

logger = logging.getLogger(__name__)

TASK = "commitment_extract"

SYSTEM = (
    "You audit ONE finished assistant turn and extract any follow-up worth recording, as STRICT JSON.\n"
    "You get what the person SAID and what the assistant REPLIED. Decide only from the literal words.\n\n"
    "Record an item only when the turn clearly contains one of:\n"
    "1. A DURABLE COMMITMENT: an explicit promise, obligation, or reminder to do something later "
    "(\"remind me to X\", \"I'll get back to you on X\", \"I'll send you X by 3pm\", \"follow up on X by Friday\").\n"
    "2. An IMMEDIATE OWED DELIVERABLE: the person asked to be SENT something through a channel the reply "
    "did NOT satisfy (email it, text a DIFFERENT number, send it to someone else, send it later), AND the "
    "actual content to send is present in the exchange. IMPORTANT: in a chat the assistant's reply already "
    "IS a message to the person, so a plain \"text me\"/\"message me\" is ALREADY satisfied; do NOT record "
    "that; only record a deliverable for a genuinely different channel, recipient, or time.\n\n"
    "Do NOT record small talk, questions, hypotheticals, vague intentions, or anything the reply already "
    "fully handled. Fewer items beats wrong items.\n\n"
    "Output ONLY JSON, nothing else (no prose, no markdown, no code fence): an array, or an object "
    '{"items": [...]} when a schema asks for one. Empty array [] when nothing qualifies. Each element:\n'
    '{"description": string, "due_at": ISO-8601-UTC string or null, "priority": integer 0-100, '
    '"source_type": "cognition" | "introspection", "metadata": null or '
    '{"kind":"deliverable","content":"<exact text to send, ready as-is>","channel_hint":"sms"|"dm"|"email"}}\n'
    "Use \"introspection\" + the deliverable metadata (due_at about two minutes from now) for case 2; "
    "\"cognition\" + metadata null for case 1. Resolve relative times against the turn time given.\n\n"
    "Examples:\n"
    "They said: Remind me to call the dentist Friday at 9am. | Assistant replied: Got it.\n"
    '[{"description":"Remind them to call the dentist Friday 9am","due_at":"2026-06-26T13:00:00+00:00",'
    '"priority":70,"source_type":"cognition","metadata":null}]\n'
    "They said: Email me the Q3 revenue number. | Assistant replied: Q3 revenue was 4.2 million.\n"
    '[{"description":"Email them the Q3 revenue","due_at":"2026-06-21T21:40:00+00:00","priority":80,'
    '"source_type":"introspection","metadata":{"kind":"deliverable","content":"Q3 revenue was 4.2 million.",'
    '"channel_hint":"email"}}]\n'
    "They said: What's the weather? | Assistant replied: 72 and sunny.\n"
    "[]\n"
    "They said: Text me that. | Assistant replied: The address is 5 Main St.\n"
    "[]   (a plain text-me in chat is already satisfied by the reply)")

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "due_at": {"type": ["string", "null"]},
        "priority": {"type": "integer"},
        "source_type": {"type": "string", "enum": ["cognition", "introspection"]},
        "metadata": {"type": ["object", "null"]},
    },
    "required": ["description", "due_at", "priority", "source_type", "metadata"],
}
# The router's output contract is a named object schema (``P/router/router.py``); a
# strict endpoint answers ``{"items": [...]}`` and ``parse_items`` reads the array either way.
RESPONSE_SCHEMA = {
    "name": "commitment_extract",
    "schema": {"type": "object", "properties": {"items": {"type": "array", "items": ITEM_SCHEMA}},
               "required": ["items"], "additionalProperties": False},
}


def initialize(conn) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS commitment_runs (
        turn_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
        lease_token TEXT NOT NULL DEFAULT '', disposition TEXT, error TEXT)''')


def enqueue(conn, turn_id, contact_id, messages, *, scope) -> None:
    """A person-scoped turn with the person's own words is a capture job."""
    if contact_id and scope == "person" and any(m.get("role") == "user" for m in messages):
        conn.execute("INSERT OR IGNORE INTO commitment_runs(turn_id) VALUES (?)", (turn_id,))


def erase_removed(conn, turn_id, session_id, retained) -> None:
    if not any(m.get("role") == "user" for m in retained):
        conn.execute("DELETE FROM commitment_runs WHERE turn_id=?", (turn_id,))


def _text(message: Dict[str, Any]) -> str:
    value = message.get("content")
    if isinstance(value, list):
        value = "\n".join(x["text"] for x in value if isinstance(x, dict)
                          and x.get("type") in {"text", "input_text", "output_text"} and isinstance(x.get("text"), str))
    return value if isinstance(value, str) else ""


def parse_items(text: str) -> List[Dict[str, Any]]:
    """The first JSON array in the model's reply; [] on anything malformed."""
    if not text:
        return []
    try:
        chunk = text[text.index("["): text.rindex("]") + 1]
        data = json.loads(chunk)
    except (ValueError, TypeError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def build_prompt(*, user_message: str, assistant_message: str, conversation_text: str,
                 existing: List[Dict[str, Any]], rejections: List[Dict[str, Any]], turn_time: str = "") -> str:
    parts = []
    if turn_time:
        parts.append(f"Turn time: {turn_time}\n")
    if conversation_text:
        parts.append(f"Recent conversation:\n{conversation_text}\n")
    parts.append("This turn, verbatim:\n"
                 f"  They said: {user_message}\n"
                 f"  Assistant replied: {assistant_message}\n")
    if existing:
        parts.append("\nAlready-recorded OPEN items for this person (do not duplicate; "
                     "mentioning an open item again is NOT a new item):")
        for item in existing[:6]:
            parts.append(f"\n- {(item.get('description') or '?')[:80]}")
    if rejections:
        parts.append("\n\nRecently REJECTED items (judged invalid or duplicate; do NOT "
                     "record these or anything similar again):")
        for item in rejections[:6]:
            parts.append(f"\n- {(item.get('description') or '?')[:80]} [{item.get('outcome') or 'rejected'}]")
    return "".join(parts)


def record_items(items: List[Dict[str, Any]], *, person_id: str, commitment_store: Any,
                 existing: List[Dict[str, Any]], rejections: List[Dict[str, Any]],
                 source_context: str = "turn commitment extraction") -> Dict[str, Any]:
    """Create the commitments the model proposed, skipping open and rejected duplicates.

    Deadlines are resolved against the turn's own time, so a promise captured
    late (an outage, a restart, a backlog) may already be due: it is imported
    with its original deadline as ``overdue`` rather than dropped.
    """
    from protagine.commitments.store import _normalize_desc, _similar_desc
    known = [_normalize_desc(c.get("description") or "") for c in existing]
    known += [_normalize_desc(r.get("description") or "") for r in rejections]
    known = [k for k in known if k]
    created: List[str] = []
    skipped = 0
    for item in items:
        description = str(item.get("description") or "").strip()
        if not description:
            continue
        norm = _normalize_desc(description)
        if any(_similar_desc(norm, k) for k in known):
            skipped += 1
            continue
        try:
            row = commitment_store.create(
                person_id=person_id, description=description[:1000], dedupe=True, allow_overdue=True,
                due_at=(item.get("due_at") or None), priority=int(item.get("priority") or 60),
                source_type=(item.get("source_type") or "introspection"), source_context=source_context,
                metadata=(item.get("metadata") if isinstance(item.get("metadata"), dict) else None))
        except Exception as error:  # e.g. a malformed due time is rejected by the store
            logger.debug("commitment candidate skipped (%s)", type(error).__name__)
            continue
        if row.get("deduped"):
            skipped += 1
        else:
            created.append(row.get("id"))
        known.append(norm)
    return {"created": created, "candidates": len(items), "skipped_duplicates": skipped}


class CommitmentExtractor:
    """One durable job per person-scoped turn, processed on the router."""

    def __init__(self, ledger, commitments_provider, *, clock=time.time) -> None:
        self.ledger, self.commitments_provider, self.clock = ledger, commitments_provider, clock
        with closing(ledger._connect()) as conn, conn:
            initialize(conn)

    def _claim(self, deadline: float):
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM commitment_runs WHERE (status='pending' AND next_attempt<=?) "
                "OR (status='running' AND lease_until<=?) ORDER BY rowid LIMIT 1",
                (self.clock(), self.clock())).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            conn.execute("UPDATE commitment_runs SET status='running',attempts=attempts+1,lease_token=?,lease_until=? "
                         "WHERE turn_id=?", (token, self.clock() + deadline + 30, row["turn_id"]))
            return dict(row) | {"lease_token": token, "attempts": row["attempts"] + 1}

    def _finish(self, job, disposition: str, error: str | None = None) -> None:
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute("UPDATE commitment_runs SET status='complete',disposition=?,error=?,lease_until=0 "
                         "WHERE turn_id=? AND lease_token=?", (disposition, error, job["turn_id"], job["lease_token"]))

    def _retry(self, job, error: str) -> None:
        with closing(self.ledger._connect()) as conn, conn:
            if job["attempts"] >= 3:
                conn.execute("UPDATE commitment_runs SET status='complete',disposition='failed',error=?,lease_until=0 "
                             "WHERE turn_id=? AND lease_token=?", (error[:200], job["turn_id"], job["lease_token"]))
            else:
                conn.execute("UPDATE commitment_runs SET status='pending',next_attempt=?,error=?,lease_until=0 "
                             "WHERE turn_id=? AND lease_token=?",
                             (self.clock() + 60 * job["attempts"], error[:200], job["turn_id"], job["lease_token"]))

    def _source(self, turn_id: str) -> Optional[Dict[str, Any]]:
        with closing(self.ledger._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM turn_sources s WHERE turn_id=? AND scope='person' AND NOT EXISTS "
                "(SELECT 1 FROM source_projection_erasures e WHERE e.turn_id=s.turn_id)", (turn_id,)).fetchone()
        if row is None:
            return None
        source = dict(row)
        source["messages"] = json.loads(source["messages_json"])
        return source

    async def process_one(self, router) -> bool:
        commitments = self.commitments_provider() if callable(self.commitments_provider) else self.commitments_provider
        if commitments is None or router is None or getattr(router, "supports_function_routing", False) is not True:
            return False
        job = self._claim(0)
        if job is None:
            return False
        try:
            source = self._source(job["turn_id"])
            if source is None:
                self._finish(job, "unsupported_source")
                return True
            users = [_text(m) for m in source["messages"] if m.get("role") == "user"]
            assistants = [_text(m) for m in source["messages"] if m.get("role") == "assistant"]
            user_message = "\n".join(t for t in users if t.strip())[-6000:]
            assistant_message = "\n".join(t for t in assistants if t.strip())[-6000:]
            if not user_message.strip():
                self._finish(job, "no_user_message")
                return True
            person_id = source["contact_id"]
            existing = commitments.get_pending_for_person(person_id) or []
            rejections = commitments.recent_rejections(limit=6) or []
            prompt = build_prompt(user_message=user_message, assistant_message=assistant_message,
                                  conversation_text="", existing=existing, rejections=rejections,
                                  turn_time=str(source.get("occurred_at") or source.get("ingested_at") or ""))
            deadline = router.function_deadline_seconds(context={"task": TASK})
            if (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)
                    or not 0 < deadline <= 600):
                raise ValueError("invalid_commitment_function_deadline")
            with closing(self.ledger._connect()) as conn, conn:
                owned = conn.execute("UPDATE commitment_runs SET lease_until=? WHERE turn_id=? AND status='running' "
                                     "AND lease_token=? AND lease_until>?",
                                     (self.clock() + deadline + 35, job["turn_id"], job["lease_token"], self.clock()))
                if not owned.rowcount:
                    return True
            response = await asyncio.wait_for(router.complete(
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                context={"task": TASK, "allow_fallback": True, "max_output_tokens": 400,
                         "response_schema": RESPONSE_SCHEMA}), deadline + 5)
            items = parse_items(final_text(response))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("commitment extraction deferred for %s (%s)", job["turn_id"], type(error).__name__)
            self._retry(job, type(error).__name__)
            return True
        result = record_items(items, person_id=person_id, commitment_store=commitments, existing=existing,
                              rejections=rejections)
        self._finish(job, "recorded" if result["created"] else "nothing")
        if result["created"]:
            logger.info("commitment extraction recorded %d item(s) for %s", len(result["created"]), person_id)
        return True


__all__ = ["CommitmentExtractor", "ITEM_SCHEMA", "RESPONSE_SCHEMA", "SYSTEM", "TASK", "build_prompt", "enqueue",
           "erase_removed", "initialize", "parse_items", "record_items"]
