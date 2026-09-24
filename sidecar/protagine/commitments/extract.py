"""Commitment capture: the ``commitment_extract`` function task in the projection worker.

Architecture 3.1 and 4.6. After every person-scoped turn the projection worker
judges the turn on the sidecar router (no tools, no private endpoint) and
records durable commitments and immediate owed deliverables in the commitment
store, on by default whenever a router is configured. The dedupe against
open and recently rejected items is enforced in code, not only in the prompt:
a repeated mention of an open item is never a second item. An item withdrawn
or dismissed as obsolete is only shown to the model as closed, so a clear fresh
commitment to it can be recorded again.

A later turn that changes an open item is an action against it, not a new
row: the prompt lists the person's open items by number and the model answers
``reschedule``, ``complete`` or ``cancel`` with that number as ``target``.
The apply step demands the row's identity, not a resemblance: the listed
wording, and the listed deadline when two rows share it. The write itself is
a compare-and-set in the store against what was listed, so an extraction
still thinking while the owner corrects the row never overwrites the
correction; the job is rerun once against the fresh state instead.

An obligation has a counterpart: the contact it is owed to or owed by, recorded
as ``metadata.counterpart`` the way the conversation named them. A turn
attributed to that contact (a message that arrived with sender metadata) lists
the item next to the contact's own, so "I need it earlier" or "got it, thanks"
from the other side reaches the owner's row. It also says who owes the work
(``metadata.obligor``: ``owner``, ``assistant``, or the other party), which is
how the mind tells a reminder to the owner from work the body performs.

Jobs are durable rows in the ledger database (``commitment_runs``), claimed
with a lease exactly like the appraisal and judgment projections, so a
process loss resumes from SQLite. One person's jobs land in order: a job is
claimable only when no earlier job for the same person is still pending,
running or waiting out a backoff, so a cancellation never runs before the
creation it cancels. The order must not become a stall: a job that is
backing off after a transport failure holds the person's later jobs for at
most ``HOLD_RETRY_SECONDS`` at a time, after which the worker retries it early
and uncharged (its scheduled attempts alone spend its budget), so one failed
call delays a person's captures by seconds, not by its whole backoff. A job
with nothing queued behind it keeps the gentle backoff. ``drain`` lets the
mind land what capture still owes before it decides over the store.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from protagine.util.model_output import final_text

logger = logging.getLogger(__name__)

TASK = "commitment_extract"

ACTIONS = ("create", "reschedule", "complete", "cancel")
# Open items the prompt numbers; an action's ``target`` indexes this list.
OPEN_ITEMS_LISTED = 12
# The reasoning model spends its budget in the thinking field before the
# JSON; complete outputs observed so far were 97-324 tokens, and the router
# clamps to the tier's maximum anyway.
OUTPUT_BUDGET_TOKENS = 1500
# Earlier turns shown with the audited turn, so an amendment ("make that
# noon") or an acceptance ("fine, I'll do it") is judged with its referent.
CONTEXT_TURNS, CONTEXT_WINDOW_SECONDS, CONTEXT_CHARS = 3, 3600, 3000
# Failures that mean "the model's answer was unusable", retried at once; a
# transport failure keeps the growing backoff.
OUTPUT_DEFECTS = ("incomplete_final_answer", "missing_final_answer", "unparsable_output")
MAX_ATTEMPTS = 3
# A transport failure backs a job off by BACKOFF_SECONDS times its attempts. While later jobs of the
# same person wait behind it, the worker retries it this soon after the failure instead (uncharged):
# the router's own endpoint cooldown is 15 s, so an earlier call would not even test the endpoint.
BACKOFF_SECONDS = 60
HOLD_RETRY_SECONDS = 15
POLL_SECONDS = 0.1

SYSTEM = (
    "You audit ONE finished assistant turn and extract any follow-up worth recording, as STRICT JSON.\n"
    "You get what the person SAID and what the assistant REPLIED, the recent conversation before it, and the "
    "person's already-recorded OPEN items, numbered. Decide only from the literal words.\n\n"
    "Record a NEW item (action \"create\", target null) only when the turn clearly contains one of:\n"
    "1. A DURABLE COMMITMENT: an explicit promise, obligation, or reminder to do something later "
    "(\"remind me to X\", \"I'll get back to you on X\", \"I'll send you X by 3pm\", \"follow up on X by Friday\").\n"
    "2. An IMMEDIATE OWED DELIVERABLE: the person asked to be SENT something through a channel the reply "
    "did NOT satisfy (email it, text a DIFFERENT number, send it to someone else, send it later), AND the "
    "actual content to send is present in the exchange. IMPORTANT: in a chat the assistant's reply already "
    "IS a message to the person, so a plain \"text me\"/\"message me\" is ALREADY satisfied; do NOT record "
    "that; only record a deliverable for a genuinely different channel, recipient, or time.\n\n"
    "Record an UPDATE to a numbered open item (action \"reschedule\", \"complete\" or \"cancel\", target = its "
    "number, description = its listed wording EXACTLY as shown, listed_due = the due time shown next to it, or null "
    "when it showed \"no due\") when the turn changes it. An update whose wording or listed_due does not match the "
    "numbered item is discarded, so copy both from the list. An update is never a new item:\n"
    "- A new time for a listed item, EARLIER or LATER, is \"reschedule\" with the new due_at.\n"
    "- The person saying it is done, sent or handled, or the other party (also relayed in an inbound message) "
    "confirming they have it, that someone else did it, or that they no longer need it, is \"complete\" "
    "(it happened) or \"cancel\" (no longer wanted).\n"
    "- A stall (\"not yet\", \"still on it\") or a partial update (one part done, the rest pending) is NEITHER: "
    "record nothing for it.\n"
    "- \"Do not remind me about X for now\" / \"park X, I'll say when it is live again\" is a HOLD: \"reschedule\" "
    "the listed item with due_at null. A hold is never a reminder and never a cancel. Reinstating a held item "
    "(\"remind me about X again, at T\") is \"reschedule\" with the new time, not a new item.\n\n"
    "due_at: resolve relative times against the turn time given. No clear time means due_at null. Something "
    "that should happen only if another event happens first (\"only if they write again\") gets due_at null. "
    "Two deliverables or two dates in one turn are two items. When the person asks for a word BEFORE a deadline "
    "(\"give me a heads-up ten minutes before\", \"warn me at half three\"), due_at stays the deadline and metadata is "
    '{"heads_up_at": "<ISO-8601-UTC>"} (or {"lead_minutes": N}); the heads-up is part of that one item, '
    "never a second one.\n"
    "counterpart: for a NEW item, the other party, the one it is owed to or who owes it, written exactly as the "
    "conversation identifies them (a contact id such as p-07, a name, or a handle); \"owner\" when the other party is "
    "the assistant's owner and no name is given; null when there is no other party. null for every update.\n"
    "obligor: for a NEW item, who owes the work: \"owner\" when the person is the assistant's owner and owes it "
    "themselves (their own promise, a reminder they asked for, a word they want if something does not turn up); "
    "\"assistant\" when the assistant took the work on (\"I'll send you X by 3pm\", a deliverable, a chase the "
    "owner handed to the assistant: \"ask them yourself, leave me out of it\"); otherwise the other party who "
    "promised it, written as counterpart is. null for every update.\n"
    "Do NOT record small talk, questions, hypotheticals, vague intentions, an obligation between OTHER people "
    "that the person does not own, or anything the reply already fully handled. A dated request that came from "
    "someone else (in the recent conversation or an inbound message) and that the person now takes on IS the "
    "person's commitment with that deadline, whatever the assistant replied. Fewer items beats wrong items.\n\n"
    "Output ONLY JSON, nothing else (no prose, no markdown, no code fence): an array, or an object "
    '{"items": [...]} when a schema asks for one. Empty array [] when nothing qualifies. Each element:\n'
    '{"action": "create" | "reschedule" | "complete" | "cancel", "target": open item number or null, '
    '"description": string, "due_at": ISO-8601-UTC string or null, "priority": integer 0-100, '
    '"source_type": "cognition" | "introspection", "metadata": null or '
    '{"kind":"deliverable","content":"<exact text to send, ready as-is>","channel_hint":"sms"|"dm"|"email"} or '
    '{"heads_up_at": ISO-8601-UTC string}, "listed_due": ISO-8601-UTC string or null, "counterpart": string or null, '
    '"obligor": string or null}\n'
    "Use \"introspection\" + the deliverable metadata (due_at about two minutes from now) for case 2; "
    "\"cognition\" + metadata null (or the heads-up metadata when one was asked for) for case 1, and "
    "metadata null for every update, unless the turn states a NEW heads-up time for a rescheduled item (then the "
    "heads-up metadata; an unchanged heads-up moves with the deadline by itself).\n\n"
    "Examples:\n"
    "They said: Remind me to call the dentist Friday at 9am. | Assistant replied: Got it.\n"
    '[{"action":"create","target":null,"description":"Remind them to call the dentist Friday 9am",'
    '"due_at":"2026-06-26T13:00:00+00:00","priority":70,"source_type":"cognition","metadata":null,'
    '"listed_due":null,"counterpart":null,"obligor":"owner"}]\n'
    "They said: Email me the Q3 revenue number. | Assistant replied: Q3 revenue was 4.2 million.\n"
    '[{"action":"create","target":null,"description":"Email them the Q3 revenue","due_at":"2026-06-21T21:40:00+00:00",'
    '"priority":80,"source_type":"introspection","metadata":{"kind":"deliverable",'
    '"content":"Q3 revenue was 4.2 million.","channel_hint":"email"},"listed_due":null,"counterpart":null,'
    '"obligor":"assistant"}]\n'
    "They said: The invoice has to reach Kim by 4pm, give me a heads-up at half three. | Assistant replied: Will do.\n"
    '[{"action":"create","target":null,"description":"Send Kim the invoice","due_at":"2026-06-26T20:00:00+00:00",'
    '"priority":70,"source_type":"cognition","metadata":{"heads_up_at":"2026-06-26T19:30:00+00:00"},'
    '"listed_due":null,"counterpart":"Kim","obligor":"owner"}]\n'
    "They said (a message from contact p-07): I'll have the signed form to you by Friday. | "
    "Assistant replied: Thanks, I'll pass that on.\n"
    '[{"action":"create","target":null,"description":"p-07 sends the signed form","due_at":"2026-06-26T17:00:00+00:00",'
    '"priority":60,"source_type":"cognition","metadata":null,"listed_due":null,"counterpart":"owner",'
    '"obligor":"p-07"}]\n'
    "They said: What's the weather? | Assistant replied: 72 and sunny.\n"
    "[]\n"
    "They said: Text me that. | Assistant replied: The address is 5 Main St.\n"
    "[]   (a plain text-me in chat is already satisfied by the reply)\n"
    "With open item [1] Send Sam the build recap (due 2026-06-26T17:00:00+00:00):\n"
    "They said: Sam needs the recap by noon now, not five. | Assistant replied: Noted.\n"
    '[{"action":"reschedule","target":1,"description":"Send Sam the build recap","due_at":"2026-06-26T12:00:00+00:00",'
    '"priority":70,"source_type":"cognition","metadata":null,"listed_due":"2026-06-26T17:00:00+00:00",'
    '"counterpart":null,"obligor":null}]\n'
    "They said: Sam wrote back that the recap arrived, all good. | Assistant replied: Great.\n"
    '[{"action":"complete","target":1,"description":"Send Sam the build recap","due_at":null,"priority":70,'
    '"source_type":"cognition","metadata":null,"listed_due":"2026-06-26T17:00:00+00:00","counterpart":null,'
    '"obligor":null}]\n'
    "They said: Sam says forget the recap, the meeting is off. | Assistant replied: Understood.\n"
    '[{"action":"cancel","target":1,"description":"Send Sam the build recap","due_at":null,"priority":70,'
    '"source_type":"cognition","metadata":null,"listed_due":"2026-06-26T17:00:00+00:00","counterpart":null,'
    '"obligor":null}]\n'
    "They said: Stop reminding me about the recap for now, I'll tell you when it is back on. | "
    "Assistant replied: OK.\n"
    '[{"action":"reschedule","target":1,"description":"Send Sam the build recap","due_at":null,"priority":70,'
    '"source_type":"cognition","metadata":null,"listed_due":"2026-06-26T17:00:00+00:00","counterpart":null,'
    '"obligor":null}]\n'
    "They said: Still working on the recap. | Assistant replied: Take your time.\n"
    "[]   (a stall changes nothing)")

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "target": {"type": ["integer", "null"]},
        "description": {"type": "string"},
        "due_at": {"type": ["string", "null"]},
        "priority": {"type": "integer"},
        "source_type": {"type": "string", "enum": ["cognition", "introspection"]},
        "metadata": {"type": ["object", "null"]},
        # An update's identity check: the due time shown next to the numbered item (null when
        # it showed no due). A new item's other party, as the conversation named them, and who
        # owes the work ("owner", "assistant", or that other party).
        "listed_due": {"type": ["string", "null"]},
        "counterpart": {"type": ["string", "null"]},
        "obligor": {"type": ["string", "null"]},
    },
    "required": ["action", "target", "description", "due_at", "priority", "source_type", "metadata",
                 "listed_due", "counterpart", "obligor"],
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
        lease_token TEXT NOT NULL DEFAULT '', disposition TEXT, error TEXT,
        hold_until REAL NOT NULL DEFAULT 0, enqueued_at REAL NOT NULL DEFAULT 0)''')
    columns = {row[1] for row in conn.execute("PRAGMA table_info(commitment_runs)").fetchall()}
    # ``hold_until``: while a job backs off, the person's later jobs are held only until this time;
    # after it the worker retries the job early. Rows from before the column count as held out.
    if "hold_until" not in columns:
        conn.execute("ALTER TABLE commitment_runs ADD COLUMN hold_until REAL NOT NULL DEFAULT 0")
    # When a job was enqueued, so health can tell a queue that is landing from one that is
    # stuck. A table from before the column carries 0, which reads as "unknown".
    if "enqueued_at" not in columns:
        conn.execute("ALTER TABLE commitment_runs ADD COLUMN enqueued_at REAL NOT NULL DEFAULT 0")
    # The claim reads unfinished rows twice per candidate (the row itself, and any earlier one of the
    # same person); finished rows are the bulk of the table and are never among them.
    conn.execute("CREATE INDEX IF NOT EXISTS commitment_runs_status ON commitment_runs(status)")


def enqueue(conn, turn_id, contact_id, messages, *, scope) -> None:
    """A person-scoped turn with the person's own words is a capture job."""
    if contact_id and scope == "person" and any(m.get("role") == "user" for m in messages):
        conn.execute("INSERT OR IGNORE INTO commitment_runs(turn_id, enqueued_at) VALUES (?, ?)",
                     (turn_id, time.time()))


def erase_removed(conn, turn_id, session_id, retained) -> None:
    if not any(m.get("role") == "user" for m in retained):
        conn.execute("DELETE FROM commitment_runs WHERE turn_id=?", (turn_id,))


def _text(message: Dict[str, Any]) -> str:
    value = message.get("content")
    if isinstance(value, list):
        value = "\n".join(x["text"] for x in value if isinstance(x, dict)
                          and x.get("type") in {"text", "input_text", "output_text"} and isinstance(x.get("text"), str))
    return value if isinstance(value, str) else ""


def _sides(messages: List[Dict[str, Any]], limit: int) -> tuple:
    users = [_text(m) for m in messages if m.get("role") == "user"]
    assistants = [_text(m) for m in messages if m.get("role") == "assistant"]
    return ("\n".join(t for t in users if t.strip())[-limit:],
            "\n".join(t for t in assistants if t.strip())[-limit:])


def _utc(value: Any) -> Optional[datetime]:
    """An ISO time as an aware UTC datetime (a naive value means UTC), or None when it is not one."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _when(row: Any) -> Optional[datetime]:
    return _utc(str(row["occurred_at"] or row["ingested_at"] or ""))


def _listed_wording(item: Dict[str, Any]) -> str:
    """The wording the prompt shows for an open item, and so the wording an update must echo."""
    return (item.get("description") or "?")[:100]


def parse_items(text: str) -> List[Dict[str, Any]]:
    """The first JSON array in the model's reply.

    Empty text is "nothing". Non-empty text without a readable array is an
    unusable answer (a cut-off completion, prose), raised so the job retries
    instead of finishing as "nothing" and losing the turn.
    """
    if not text or not text.strip():
        return []
    try:
        chunk = text[text.index("["): text.rindex("]") + 1]
        data = json.loads(chunk)
    except (ValueError, TypeError):
        raise ValueError("unparsable_output")
    if not isinstance(data, list):
        raise ValueError("unparsable_output")
    return [item for item in data if isinstance(item, dict)]


def _output_defect(error: BaseException) -> Optional[str]:
    """The output-shape reason behind a failure, through the router's wrapping, or None."""
    pending, seen = [error], set()
    while pending and len(seen) < 16:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        text = str(current)
        for reason in OUTPUT_DEFECTS:
            if reason in text:
                return reason
        pending.extend(item for item in (current.__cause__, current.__context__) if isinstance(item, BaseException))
    return None


def build_prompt(*, user_message: str, assistant_message: str, conversation_text: str,
                 existing: List[Dict[str, Any]], rejections: List[Dict[str, Any]], turn_time: str = "") -> str:
    parts = []
    if turn_time:
        parts.append(f"Turn time: {turn_time}\n")
    if conversation_text:
        parts.append(f"Recent conversation (earlier turns, oldest first):\n{conversation_text}\n")
    parts.append("This turn, verbatim:\n"
                 f"  They said: {user_message}\n"
                 f"  Assistant replied: {assistant_message}\n")
    if existing:
        parts.append("\nAlready-recorded OPEN items for this person, numbered (a change to one of these is an "
                     "action with its number as target, never a new item; mentioning one again is NOT an item):")
        for index, item in enumerate(existing[:OPEN_ITEMS_LISTED], start=1):
            due = item.get("due_at")
            when = f"due {due}" if due else "no due"
            parts.append(f"\n[{index}] {_listed_wording(item)} ({when})")
    if rejections:
        parts.append("\n\nRecently CLOSED items, with why. [invalid] or [duplicate]: do NOT record it or anything "
                     "similar again. [obsolete]: it was withdrawn or dismissed; record it again only when this turn "
                     "clearly commits to it afresh:")
        for item in rejections[:6]:
            parts.append(f"\n- {(item.get('description') or '?')[:80]} [{item.get('outcome') or 'rejected'}]")
    return "".join(parts)


def _target_row(listed: List[Dict[str, Any]], target: Any, description: str,
                listed_due: Any = None) -> Optional[Dict[str, Any]]:
    """The listed open item an action points at, or None when the action does not identify it.

    Every action moves or closes a deadline, so a resemblance is not enough:
    the number must be one the prompt showed and the wording must be that
    item's own (the listed wording, or the full description behind it). When
    two listed items share their wording, the deadline shown next to the item
    (``listed_due``) must single out the pointed one; an empty wording, a
    missing tiebreak or a pointer the tiebreak disagrees with is ignored, not
    guessed.
    """
    from protagine.commitments.store import _normalize_desc
    if isinstance(target, bool) or not isinstance(target, (int, str)):
        return None
    try:
        index = int(target)
    except ValueError:
        return None
    if not 1 <= index <= len(listed):
        return None
    row = listed[index - 1]
    wording = _normalize_desc(description)
    if not wording or wording not in {_normalize_desc(_listed_wording(row)),
                                      _normalize_desc(row.get("description") or "")}:
        return None
    shown = _normalize_desc(_listed_wording(row))
    twins = [item for item in listed if _normalize_desc(_listed_wording(item)) == shown]
    if len(twins) > 1:
        due = _utc(listed_due)
        if due is None and str(listed_due or "").strip():
            return None                                            # a tiebreak that is not a time
        if [item for item in twins if _utc(item.get("due_at")) == due] != [row]:
            return None
    return row


def record_items(items: List[Dict[str, Any]], *, person_id: str, commitment_store: Any,
                 existing: List[Dict[str, Any]], rejections: List[Dict[str, Any]],
                 source_context: str = "turn commitment extraction", turn_id: str = "") -> Dict[str, Any]:
    """Apply what the model proposed: create new items, act on listed ones.

    Deadlines are resolved against the turn's own time, so a promise captured
    late (an outage, a restart, a backlog) may already be due: it is imported
    with its original deadline as ``overdue`` rather than dropped. A new
    item's counterpart and obligor travel as ``metadata.counterpart`` and
    ``metadata.obligor`` (the mind reads a missing obligor as the owner's own). A ``reschedule``
    without a time is a hold: the deadline is cleared, the row stays open; an
    absolute heads-up moves with the deadline (``_heads_up_patch``). Actions
    whose target or wording does not fit are ignored, not guessed. Every
    action is written against the listed description and deadline
    (``expect``); a row that changed since is a ``conflict``, counted for the
    caller to rerun the extraction, and left as the newer writer left it.
    """
    from protagine.commitments.store import CommitmentConflict, _normalize_desc, _similar_desc
    listed = list(existing[:OPEN_ITEMS_LISTED])
    known = [_normalize_desc(c.get("description") or "") for c in existing]
    # A rejected extraction (invalid, duplicate) is a hard block on the same wording; an item
    # withdrawn or dismissed as obsolete is shown to the model as a closed item but a fresh, clear
    # commitment to it may be recorded again.
    known += [_normalize_desc(r.get("description") or "") for r in rejections if r.get("outcome") != "obsolete"]
    known = [k for k in known if k]
    note = f"turn:{turn_id}" if turn_id else source_context
    created: List[str] = []
    updated: List[str] = []
    resolved: List[str] = []
    skipped = ignored = conflicts = 0
    for item in items:
        description = str(item.get("description") or "").strip()
        action = str(item.get("action") or "create").strip().lower()
        stated = item.get("metadata") if isinstance(item.get("metadata"), dict) else None
        if action != "create":
            target = (_target_row(listed, item.get("target"), description, item.get("listed_due"))
                      if action in ACTIONS else None)
            if target is None:
                ignored += 1
                continue
            expect = {"description": target.get("description"), "due_at": target.get("due_at")}
            try:
                if action == "reschedule":
                    due_at = item.get("due_at") or None
                    metadata = {"reschedule": {"from": target.get("due_at"), "by": "conversation", "note": note}}
                    metadata.update(_heads_up_patch(target, due_at, stated))
                    row = commitment_store.update(target["id"], due_at=due_at, clear_due_at=due_at is None,
                                                  metadata=metadata, expect=expect)
                    if row is not None:
                        updated.append(row["id"])
                else:
                    outcome = "done" if action == "complete" else "obsolete"
                    row = commitment_store.resolve(target["id"], outcome=outcome, resolved_by="conversation",
                                                   note=note, expect=expect)
                    if row is not None:
                        resolved.append(row["id"])
                    # A closed row no longer blocks a fresh item worded like it.
                    closed = _normalize_desc(target.get("description") or "")
                    known = [k for k in known if k != closed]
            except CommitmentConflict:
                logger.info("commitment %s skipped: the row changed since it was listed", action)
                conflicts += 1
            except Exception as error:  # e.g. a malformed due time is rejected by the store
                logger.debug("commitment %s skipped (%s)", action, type(error).__name__)
                ignored += 1
            continue
        if not description:
            continue
        norm = _normalize_desc(description)
        if any(_similar_desc(norm, k) for k in known):
            skipped += 1
            continue
        metadata = dict(stated or {})
        for field in ("counterpart", "obligor"):
            value = str(item.get(field) or "").strip()[:120]
            if value:
                metadata[field] = value
        try:
            row = commitment_store.create(
                person_id=person_id, description=description[:1000], dedupe=True, allow_overdue=True,
                due_at=(item.get("due_at") or None), priority=int(item.get("priority") or 60),
                source_type=(item.get("source_type") or "introspection"), source_context=source_context,
                metadata=metadata or None)
        except Exception as error:  # e.g. a malformed due time is rejected by the store
            logger.debug("commitment candidate skipped (%s)", type(error).__name__)
            continue
        if row.get("deduped"):
            skipped += 1
        else:
            created.append(row.get("id"))
        known.append(norm)
    return {"created": created, "updated": updated, "resolved": resolved, "candidates": len(items),
            "skipped_duplicates": skipped, "ignored_actions": ignored, "conflicts": conflicts}


def _heads_up_patch(target: Dict[str, Any], new_due: Optional[str], stated: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The heads-up metadata a reschedule writes over the listed row's.

    A warning time the turn states replaces whatever was recorded; a key the
    model filled with null states nothing. Otherwise an absolute
    ``heads_up_at`` keeps its lead: it moves by the same delta as the
    deadline, and is dropped when a hold removes the deadline or the shift
    leaves it no earlier than the new one. A relative ``lead_minutes`` needs
    nothing: the drive reads it against whatever the deadline is.
    """
    given = {key: (stated or {}).get(key) for key in ("heads_up_at", "lead_minutes")}
    if any(value is not None for value in given.values()):
        warn = _utc(given["heads_up_at"])
        return {"heads_up_at": warn.isoformat() if warn is not None else None,
                "lead_minutes": given["lead_minutes"]}
    recorded = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
    if recorded.get("heads_up_at") is None:
        return {}
    warn, old_due, due = _utc(recorded.get("heads_up_at")), _utc(target.get("due_at")), _utc(new_due)
    if warn is None or old_due is None or due is None:
        return {"heads_up_at": None}
    shifted = warn + (due - old_due)
    return {"heads_up_at": shifted.isoformat() if shifted < due else None}


def _landed(result: Dict[str, Any]) -> int:
    return sum(len(result.get(key) or ()) for key in ("created", "updated", "resolved"))


class CommitmentExtractor:
    """One durable job per person-scoped turn, processed on the router."""

    def __init__(self, ledger, commitments_provider, *, aliases=None, clock=time.time) -> None:
        """``aliases(contact_id)`` names the contact the way a conversation does (a display name, a
        handle), possibly awaitable; ``contact_aliases`` builds one over the contacts store."""
        self.ledger, self.commitments_provider, self.clock = ledger, commitments_provider, clock
        self.aliases = aliases
        with closing(ledger._connect()) as conn, conn:
            initialize(conn)

    def _commitments(self):
        return self.commitments_provider() if callable(self.commitments_provider) else self.commitments_provider

    @staticmethod
    def _usable(router, commitments) -> bool:
        routed = getattr(router, "supports_function_routing", False) is True
        return commitments is not None and router is not None and routed

    def _claim(self, deadline: float, *, ignore_backoff: bool = False, skip: tuple = ()):
        """Lease the next job; ``ignore_backoff`` takes a pending row before its retry time.

        One person's jobs are taken in order: a job is claimable only when no
        earlier job for the same person is unfinished (pending, running, or
        pending in a backoff), whoever holds it, so a cancellation never
        lands before the creation it cancels and an older reschedule never
        finishes after a newer one. Decided under the same ``BEGIN IMMEDIATE``
        as the lease. A job whose source is gone belongs to nobody: it blocks
        no one and is taken so it can finish as unsupported.

        The order never becomes a stall. A job backing off after a transport
        failure holds the person's later jobs only until its ``hold_until``;
        past that, with a later job of the same person waiting, it is taken
        early. Such an attempt is uncharged (``charged`` False): only the
        scheduled attempts spend the job's ``MAX_ATTEMPTS``, so a dead
        endpoint still gets its full backoff before the job is failed, while
        a blip costs the person's captures seconds.
        """
        now = self.clock()
        if ignore_backoff:
            pending, pending_params = "r.status='pending'", []
        else:
            pending = ("r.status='pending' AND (r.next_attempt<=? OR (r.hold_until<=? AND EXISTS ("
                       "SELECT 1 FROM commitment_runs l JOIN turn_sources ls ON ls.turn_id=l.turn_id "
                       "WHERE ls.contact_id=s.contact_id AND l.rowid>r.rowid AND l.status IN ('pending','running'))))")
            pending_params = [now, now]
        exclude = f" AND r.turn_id NOT IN ({','.join('?' for _ in skip)})" if skip else ""
        params = [int(ignore_backoff), now, *pending_params, now, *skip]
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT r.*, (? OR r.status='running' OR r.next_attempt<=?) AS charged "
                "FROM commitment_runs r LEFT JOIN turn_sources s ON s.turn_id=r.turn_id "
                f"WHERE ({pending} OR (r.status='running' AND r.lease_until<=?)){exclude} "
                "AND NOT EXISTS (SELECT 1 FROM commitment_runs e JOIN turn_sources es ON es.turn_id=e.turn_id "
                "WHERE es.contact_id=s.contact_id AND e.rowid<r.rowid AND e.status IN ('pending','running')) "
                "ORDER BY r.rowid LIMIT 1", params).fetchone()
            if row is None:
                return None
            token, charged = uuid.uuid4().hex, bool(row["charged"])
            conn.execute("UPDATE commitment_runs SET status='running',attempts=attempts+?,lease_token=?,lease_until=? "
                         "WHERE turn_id=?", (int(charged), token, self.clock() + deadline + 30, row["turn_id"]))
            return dict(row) | {"lease_token": token, "attempts": row["attempts"] + int(charged), "charged": charged}

    def _finish(self, job, disposition: str, error: str | None = None) -> None:
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute("UPDATE commitment_runs SET status='complete',disposition=?,error=?,lease_until=0 "
                         "WHERE turn_id=? AND lease_token=?", (disposition, error, job["turn_id"], job["lease_token"]))

    def _retry(self, job, error: str, *, immediate: bool = False) -> None:
        """Re-queue a failed attempt: at once for an unusable answer, backed off for a transport failure.

        An early, uncharged attempt (``_claim``) keeps the job's scheduled retry time and only re-arms
        the hold, so a dead endpoint is probed once per ``HOLD_RETRY_SECONDS`` while the person's
        later jobs wait, and the scheduled attempts alone decide when the job is failed.
        """
        charged, now = job.get("charged", True), self.clock()
        with closing(self.ledger._connect()) as conn, conn:
            if charged and job["attempts"] >= MAX_ATTEMPTS:
                conn.execute("UPDATE commitment_runs SET status='complete',disposition='failed',error=?,lease_until=0 "
                             "WHERE turn_id=? AND lease_token=?", (error[:200], job["turn_id"], job["lease_token"]))
            elif immediate:
                conn.execute("UPDATE commitment_runs SET status='pending',next_attempt=?,error=?,lease_until=0 "
                             "WHERE turn_id=? AND lease_token=?", (now, error[:200], job["turn_id"], job["lease_token"]))
            elif not charged:
                conn.execute("UPDATE commitment_runs SET status='pending',hold_until=?,error=?,lease_until=0 "
                             "WHERE turn_id=? AND lease_token=?",
                             (now + HOLD_RETRY_SECONDS, error[:200], job["turn_id"], job["lease_token"]))
            else:
                conn.execute("UPDATE commitment_runs SET status='pending',next_attempt=?,hold_until=?,error=?,"
                             "lease_until=0 WHERE turn_id=? AND lease_token=?",
                             (now + BACKOFF_SECONDS * job["attempts"], now + HOLD_RETRY_SECONDS, error[:200],
                              job["turn_id"], job["lease_token"]))

    def _release(self, job) -> None:
        """A cancelled attempt (a drain's budget, a shutdown) hands the job straight back, uncharged."""
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute("UPDATE commitment_runs SET status='pending',attempts=attempts-?,lease_until=0 "
                         "WHERE turn_id=? AND lease_token=? AND status='running'",
                         (int(job.get("charged", True)), job["turn_id"], job["lease_token"]))

    def _requeue(self, job, reason: str) -> None:
        """The extraction was sound but the store moved under it: run it again, now, against the fresh state."""
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute("UPDATE commitment_runs SET status='pending',next_attempt=?,error=?,lease_until=0 "
                         "WHERE turn_id=? AND lease_token=?", (self.clock(), reason, job["turn_id"], job["lease_token"]))

    async def _existing(self, commitments, person_id: str) -> List[Dict[str, Any]]:
        """The open items this turn may act on: the person's own, then those recorded with the person
        as counterpart (by contact id, by the configured owner's "owner", by any alias the lookup adds)."""
        own = list(commitments.get_pending_for_person(person_id) or [])
        related = getattr(commitments, "get_open_for_counterpart", None)
        if related is None:
            return own
        aliases = {person_id}
        from protagine.identity import get_owner_contact_id
        if person_id == (get_owner_contact_id() or ""):
            aliases.add("owner")
        if self.aliases is not None:
            try:
                more = self.aliases(person_id)
                if inspect.isawaitable(more):
                    more = await more
                aliases.update(str(name) for name in (more or ()) if str(name or "").strip())
            except Exception as error:
                logger.debug("contact aliases unavailable for %s (%s)", person_id, type(error).__name__)
        seen = {row.get("id") for row in own}
        for row in related(sorted(aliases)) or []:
            if row.get("id") not in seen and row.get("person_id") != person_id:
                own.append(row)
                seen.add(row.get("id"))
        return own

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

    def _recent_turns(self, source: Dict[str, Any]) -> str:
        """The person's last few turns before this one, any session, within the hour."""
        with closing(self.ledger._connect()) as conn:
            rows = conn.execute(
                "SELECT s.turn_id, s.messages_json, s.occurred_at, s.ingested_at FROM turn_sources s "
                "WHERE s.contact_id=? AND s.scope='person' AND s.turn_id<>? "
                "AND s.rowid<(SELECT rowid FROM turn_sources WHERE turn_id=?) "
                "AND NOT EXISTS (SELECT 1 FROM source_projection_erasures e WHERE e.turn_id=s.turn_id) "
                "ORDER BY s.rowid DESC LIMIT ?",
                (source["contact_id"], source["turn_id"], source["turn_id"], CONTEXT_TURNS)).fetchall()
        anchor = _when(source)
        rendered = []
        for row in rows:
            when = _when(row)
            if anchor is not None and when is not None:
                if not 0 <= (anchor - when).total_seconds() <= CONTEXT_WINDOW_SECONDS:
                    continue
            user_message, assistant_message = _sides(json.loads(row["messages_json"]), CONTEXT_CHARS // CONTEXT_TURNS)
            if not user_message.strip():
                continue
            stamp = f" at {when.isoformat()}" if when is not None else ""
            rendered.append(f"[earlier turn{stamp}]\n  They said: {user_message}\n"
                            f"  Assistant replied: {assistant_message}")
        return "\n".join(reversed(rendered))[-CONTEXT_CHARS:]

    def oldest_unfinished_seconds(self) -> Optional[float]:
        """How long the oldest job still pending or running has waited, or None when nothing waits.

        Health reads this: capture jobs that sit for hours mean the projection worker and the
        tick's drain are both not landing them, whatever the reason. Rows from before the
        ``enqueued_at`` column (0) are of unknown age and never counted.
        """
        with closing(self.ledger._connect()) as conn:
            row = conn.execute("SELECT MIN(enqueued_at) AS oldest FROM commitment_runs "
                               "WHERE status IN ('pending', 'running') AND enqueued_at > 0").fetchone()
        oldest = row[0] if row is not None else None
        if not oldest:
            return None
        return max(0.0, float(self.clock()) - float(oldest))

    def pending_counts(self) -> Dict[str, int]:
        """``commitment_runs`` rows by status; ``pending`` and ``running`` are always present."""
        with closing(self.ledger._connect()) as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM commitment_runs GROUP BY status").fetchall()
        counts = {"pending": 0, "running": 0}
        counts.update({row["status"]: int(row["n"]) for row in rows})
        return counts

    async def process_one(self, router, *, ignore_backoff: bool = False) -> bool:
        commitments = self._commitments()
        if not self._usable(router, commitments):
            return False
        job = self._claim(0, ignore_backoff=ignore_backoff)
        if job is None:
            return False
        await self._run(job, router, commitments)
        return True

    async def drain(self, router, *, budget_seconds: float, ignore_backoff: bool = True) -> Dict[str, Any]:
        """Land what capture still owes, within a budget, before a decision is made over the store.

        Claimable jobs are processed here (with ``ignore_backoff`` a pending
        row is taken before its retry time); a row leased elsewhere, by the
        projection worker on its own loop or thread, is polled until its lease
        clears. ``_claim`` leases under ``BEGIN IMMEDIATE`` on the shared
        ledger, so neither side processes a job the other holds. A job this
        drain already attempted is not taken again within the same drain, so a
        dead endpoint cannot burn a job's attempts in one tick.
        """
        started = time.monotonic()
        summary: Dict[str, Any] = {"recorded": 0, "processed": 0, "items": 0, "waited_seconds": 0.0,
                                   "pending_left": 0}
        commitments = self._commitments()
        usable = self._usable(router, commitments)
        attempted: List[str] = []
        while True:
            remaining = budget_seconds - (time.monotonic() - started)
            if remaining <= 0:
                break
            job = self._claim(0, ignore_backoff=ignore_backoff, skip=tuple(attempted)) if usable else None
            if job is not None:
                attempted.append(job["turn_id"])
                try:
                    result = await asyncio.wait_for(self._run(job, router, commitments), remaining)
                except asyncio.TimeoutError:
                    break
                summary["processed"] += 1
                landed = _landed(result)
                summary["items"] += landed
                summary["recorded"] += 1 if landed else 0
                continue
            if not self.pending_counts()["running"]:
                break
            await asyncio.sleep(min(POLL_SECONDS, remaining))
        counts = self.pending_counts()
        summary["waited_seconds"] = round(time.monotonic() - started, 3)
        summary["pending_left"] = counts["pending"] + counts["running"]
        return summary

    async def _run(self, job, router, commitments) -> Dict[str, Any]:
        """Process one leased job; the ``record_items`` result, or {} when nothing landed."""
        try:
            source = self._source(job["turn_id"])
            if source is None:
                self._finish(job, "unsupported_source")
                return {}
            user_message, assistant_message = _sides(source["messages"], 6000)
            if not user_message.strip():
                self._finish(job, "no_user_message")
                return {}
            person_id = source["contact_id"]
            existing = await self._existing(commitments, person_id)
            rejections = commitments.recent_rejections(limit=6) or []
            prompt = build_prompt(user_message=user_message, assistant_message=assistant_message,
                                  conversation_text=self._recent_turns(source), existing=existing,
                                  rejections=rejections,
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
                    return {}
            response = await asyncio.wait_for(router.complete(
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                context={"task": TASK, "allow_fallback": True, "max_output_tokens": OUTPUT_BUDGET_TOKENS,
                         "response_schema": RESPONSE_SCHEMA}), deadline + 5)
            items = parse_items(final_text(response))
        except asyncio.CancelledError:
            self._release(job)
            raise
        except Exception as error:
            defect = _output_defect(error)
            logger.warning("commitment extraction deferred for %s (%s)", job["turn_id"], defect or type(error).__name__)
            self._retry(job, defect or type(error).__name__, immediate=defect is not None)
            return {}
        result = record_items(items, person_id=person_id, commitment_store=commitments, existing=existing,
                              rejections=rejections, turn_id=job["turn_id"])
        if result.get("conflicts") and job.get("error") != "stale_snapshot":
            # A row moved between the listing and the write (the owner corrected it while the model
            # was thinking): what landed stays, the job runs once more against the fresh state.
            logger.info("commitment extraction rerun for %s: %d row(s) changed since listed",
                        job["turn_id"], result["conflicts"])
            self._requeue(job, "stale_snapshot")
            return result
        self._finish(job, "recorded" if _landed(result) else "nothing")
        if _landed(result):
            logger.info("commitment extraction landed %d change(s) for %s", _landed(result), person_id)
        return result


def contact_aliases(contacts_provider):
    """An ``aliases`` lookup for ``CommitmentExtractor`` over the contacts store: the display name
    and the messaging addresses a conversation may have used for a contact. ``contacts_provider``
    returns the (async) store, or None while it is not up; a lookup failure names nobody."""
    async def lookup(contact_id: str) -> List[str]:
        store = contacts_provider() if callable(contacts_provider) else contacts_provider
        if store is None:
            return []
        names: List[str] = []
        contact = await store.get(contact_id)
        if contact is not None and getattr(contact, "display_name", None):
            names.append(str(contact.display_name))
        for handle in await store.get_handles(contact_id) or []:
            if getattr(handle, "address", None):
                names.append(str(handle.address))
        return names
    return lookup


__all__ = ["ACTIONS", "BACKOFF_SECONDS", "CommitmentExtractor", "HOLD_RETRY_SECONDS", "ITEM_SCHEMA", "MAX_ATTEMPTS",
           "OPEN_ITEMS_LISTED", "OUTPUT_BUDGET_TOKENS", "RESPONSE_SCHEMA", "SYSTEM", "TASK", "build_prompt",
           "contact_aliases", "enqueue", "erase_removed", "initialize", "parse_items", "record_items"]
