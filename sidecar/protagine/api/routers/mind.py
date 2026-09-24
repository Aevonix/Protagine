"""``/v1/mind``: the body's pull protocol, the guard, the audit log and the switches.

Architecture 6.2 (dispatch and outbox), 7.5 (guard), 7.7 (asks), 7.8 (audit
log) and 7.9 (off switch). Every route sits behind the one API key. The body
(the Hermes plugin) pulls; the sidecar never pushes.

Interface (JSON; the body half is ``plugins/hermes-plugin/body.py``, the same
text in docs/HERMES-ADAPTER.md):
  GET  /dispatch                  -> [ {id, kind: task, dedup_key, idempotency_key, title, body, assignee,
                                         max_runtime_seconds, max_retries, goal_mode, created_at, ...} ]
  POST /dispatch/{id}/bound       {hermes_ref, hermes_kind?, status?, bound_at?}  -> audit entry (idempotent)
  POST /outcome                   {id, hermes_ref, status, outcome?, final?, summary?, error?, verified?,
                                   run?, ...}                                    -> audit entry
  GET  /outbox                    -> [ {id, kind: message|notice, recipient, recipient_is_owner,
                                         recipient_handles, text, ...} ]
  POST /outbox/{id}/sending       {target?, at?} -> audit entry; 409 unless the message was ready and the
                                   claim names a target (no_target: it stays ready, handles re-read per pull)
  POST /outbox/{id}/sent          {result: sent|failed|uncertain, error?, hermes_ref?, summary?} -> audit entry
  POST /observations              {observed_at, board, body, counts, stale_tasks, blocked_tasks, goals,
                                   mind_tasks} (or the flat {observations: [{kind, ...}]})
  POST /guard                     {tool, args, session|session_id, run, task_id, recipients?}
                                                                                -> {allow, action, reason}
  POST /decide                    {code, answer: yes|no, contact_id?, session_id?, message?} -> {ok, ...}
  GET  /log, /log/{id}, /why/{id}, /asks, /state (/status), /stats
  GET  /concerns, /goals          the workspace (open concerns, the broadcast set) and the open goals
  POST /interests                 {topic, why?} -> a seeded interest the curiosity drive researches
  POST /asks/{code}/yes|no        {contact_id?, message?}
  POST /off {reason?}, /on, /tick, /rate {id, verdict}, /level {autonomy}, /reset {cls}
  POST /people/{contact_id}/permission  {may_contact: never|ask} -> {contact_id, may_contact}; 404 unknown contact
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from protagine.mind import audit
from protagine.mind.authority import CLASSES, LEVELS
from protagine.mind.outcomes import VERDICTS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/mind", tags=["mind"])

_mind: Any = None


def set_mind(mind: Any) -> None:
    global _mind
    _mind = mind


def get_mind() -> Any:
    return _mind


def _require() -> Any:
    if _mind is None:
        raise HTTPException(status_code=503, detail={"code": "mind_not_wired", "message": "the mind is not running"})
    return _mind


def _entry(row: Any) -> Dict[str, Any]:
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_intention"})
    return audit.entry(row)


class BoundBody(BaseModel):
    """The body's ack after ``kanban_db.create_task``; the same ref may arrive again."""
    model_config = ConfigDict(extra="ignore")
    hermes_ref: str = Field(min_length=1, max_length=256)
    hermes_kind: str = Field(default="kanban", max_length=32)
    status: Optional[str] = Field(default=None, max_length=32)
    bound_at: Optional[str] = Field(default=None, max_length=64)


class OutcomeBody(BaseModel):
    """One report on a ``mind:*`` task from its kanban state and last run row.

    ``status`` is the kanban status; ``outcome`` (done, blocked, failed,
    cancelled, uncertain) and ``final`` are the body's reading of it. A report
    with ``final: false`` (a failed run that was requeued) is progress, not a
    settlement.
    """
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=32)
    outcome: Optional[str] = Field(default=None, max_length=32)
    final: Optional[bool] = None
    hermes_ref: Optional[str] = Field(default=None, max_length=256)
    hermes_kind: Optional[str] = Field(default=None, max_length=32)
    summary: Optional[str] = Field(default=None, max_length=8000)
    error: Optional[str] = Field(default=None, max_length=2000)
    verified: Optional[str] = Field(default=None, max_length=32)
    result: Optional[Dict[str, Any]] = None
    run: Optional[Dict[str, Any]] = None
    block_kind: Optional[str] = Field(default=None, max_length=64)
    consecutive_failures: Optional[int] = None
    completed_at: Optional[str] = Field(default=None, max_length=64)
    observed_at: Optional[str] = Field(default=None, max_length=64)


class SendingBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    target: Optional[str] = Field(default=None, max_length=256)
    at: Optional[str] = Field(default=None, max_length=64)


class SentBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    result: str = Field(default="uncertain", max_length=32)
    hermes_ref: Optional[str] = Field(default=None, max_length=256)
    summary: str = Field(default="", max_length=2000)
    error: Optional[str] = Field(default=None, max_length=2000)
    at: Optional[str] = Field(default=None, max_length=64)


class DecideBody(BaseModel):
    """The owner's answer to an ask through the plugin (7.7): the code, the answer and what
    the plugin knows about the session, which the sidecar checks again."""
    model_config = ConfigDict(extra="ignore")
    code: str = Field(min_length=1, max_length=16)
    answer: str = Field(min_length=1, max_length=8)
    contact_id: Optional[str] = Field(default=None, max_length=256)
    session_id: Optional[str] = Field(default=None, max_length=256)
    message: Optional[str] = Field(default=None, max_length=8000)
    by: str = Field(default="owner", max_length=64)


class GuardBody(BaseModel):
    """``recipients``: the contact ids an effect reaches later (a delivering cron job), resolved by
    the plugin; they are authorized like a message's recipient."""
    model_config = ConfigDict(extra="allow")
    tool: str = Field(min_length=1, max_length=128)
    args: Dict[str, Any] = Field(default_factory=dict)
    session: Optional[str] = Field(default=None, max_length=256)
    session_id: Optional[str] = Field(default=None, max_length=256)
    run: str = Field(default="mind", max_length=32)
    task_id: Optional[str] = Field(default=None, max_length=256)
    recipients: Optional[List[str]] = Field(default=None, max_length=32)


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: Optional[str] = Field(default=None, max_length=256)
    message: Optional[str] = Field(default=None, max_length=8000)
    by: str = Field(default="owner", max_length=64)


class OffBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="owner", max_length=200)
    by: str = Field(default="owner", max_length=64)


class RateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    verdict: str = Field(min_length=1, max_length=32)


class LevelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    autonomy: str = Field(min_length=1, max_length=16)


class ResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cls: str = Field(min_length=1, max_length=16)


class PermissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    may_contact: str = Field(min_length=1, max_length=16)
    by: str = Field(default="owner", max_length=64)


class InterestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=160)
    why: str = Field(default="", max_length=400)
    by: str = Field(default="owner", max_length=64)


# -- state --------------------------------------------------------------------------------

@router.get("/state")
async def state() -> Dict[str, Any]:
    return _require().state()


@router.get("/status")
async def status() -> Dict[str, Any]:
    """The plugin's route probe and its status line (same as ``/state``)."""
    return _require().state()


@router.get("/stats")
async def stats() -> Dict[str, Any]:
    value = _require().stats()
    return {**value, "text": audit.render_stats(value)}


@router.get("/concerns")
async def concerns(limit: int = 24) -> Dict[str, Any]:
    """The workspace: open concerns by salience, and the broadcast set (architecture 4.5)."""
    mind = _require()
    rows = [c.as_dict() for c in mind.concerns.open(limit=max(1, min(int(limit), 200)))]
    broadcast = [c.id for c in mind.broadcast()]
    lines = [f"{'*' if c['id'] in broadcast else ' '} {c['salience']:.2f} {c['drive']}/{c['kind']}: {c['summary']}"
             for c in rows]
    return {"concerns": rows, "broadcast": broadcast, "drives": mind.state()["drives"],
            "text": "\n".join(lines) if lines else "(nothing on my mind)"}


@router.get("/goals")
async def goals() -> Dict[str, Any]:
    """The agent-owned goals that are open (at most ``budgets.open_goals``)."""
    mind = _require()
    rows = [mind.goals.render(goal) for goal in mind.goals.open()]
    lines = [f"{g['id'][:8]}  {g['title']} ({g['steps_done']}/{g['tasks']} steps, until {str(g['horizon'])[:10]})"
             for g in rows]
    return {"goals": rows, "open_goals": mind.policy.budgets.open_goals,
            "text": "\n".join(lines) if lines else "(no open goals)"}


@router.post("/interests")
async def interests(body: InterestBody) -> Dict[str, Any]:
    """Seed an interest for the curiosity drive (the CLI's ``protagine mind interest <topic>``)."""
    try:
        value = _require().add_interest(body.topic, why=body.why, by=body.by)
    except ValueError as error:
        raise HTTPException(status_code=422, detail={"code": "invalid_interest", "message": str(error)}) from None
    return {"ok": True, **value}


# -- the body's pull protocol (6.2) -------------------------------------------------------

@router.get("/dispatch")
async def dispatch() -> List[Dict[str, Any]]:
    return _require().dispatch()


@router.post("/dispatch/{intention_id}/bound")
async def bound(intention_id: str, body: BoundBody) -> Dict[str, Any]:
    return _entry(_require().bound(intention_id, body.hermes_ref, hermes_kind=body.hermes_kind))


@router.post("/outcome")
async def outcome(body: OutcomeBody) -> Dict[str, Any]:
    return _entry(_require().outcomes.record(
        body.id, status=body.status, outcome=body.outcome, final=body.final, hermes_ref=body.hermes_ref,
        summary=body.summary or "", error=body.error, verified=body.verified, result=body.result, run=body.run))


@router.get("/outbox")
async def outbox() -> List[Dict[str, Any]]:
    return await _require().outbox_ready()


@router.post("/outbox/{intention_id}/sending")
async def outbox_sending(intention_id: str, body: SendingBody | None = None) -> Dict[str, Any]:
    """2xx only when the message was ready; 409 tells the body not to send."""
    mind = _require()
    row = mind.store.get(intention_id)
    if row is None or row.kind != "message":
        raise HTTPException(status_code=404, detail={"code": "unknown_message"})
    if row.status != "approved":
        raise HTTPException(status_code=409, detail={"code": "not_ready", "status": row.status})
    body = body or SendingBody()
    claimed = mind.outbox.sending(intention_id, target=body.target)
    if claimed is None:
        raise HTTPException(status_code=409, detail={"code": "no_target" if not body.target else "not_ready",
                                                     "status": row.status})
    return _entry(claimed)


@router.post("/outbox/{intention_id}/sent")
async def outbox_sent(intention_id: str, body: SentBody) -> Dict[str, Any]:
    return _entry(_require().outbox.sent(intention_id, result=body.result, hermes_ref=body.hermes_ref,
                                         summary=body.summary, error=body.error))


@router.post("/observations")
async def observations(body: Dict[str, Any]) -> Dict[str, Any]:
    """The board as the body sees it (docs/HERMES-ADAPTER.md), or the flat ``observations`` list."""
    return _require().observe(body)


# -- the guard (7.5) ---------------------------------------------------------------------

@router.post("/guard")
async def guard(body: GuardBody) -> Dict[str, Any]:
    mind = _require()
    session_id = body.session_id or body.session or ""
    try:
        return await mind.guard(tool=body.tool, args=body.args, run=body.run, session_id=session_id,
                                recipients=body.recipients)
    except Exception as error:  # the guard fails closed
        logger.warning("guard check failed (%s)", type(error).__name__)
        return {"allow": False, "action": "block", "reason": f"guard error ({type(error).__name__})"}


# -- asks (7.7) --------------------------------------------------------------------------

def _answer(code: str, answer: str, *, by: str, contact_id: Optional[str], message: Optional[str]) -> Dict[str, Any]:
    if answer not in {"yes", "no"}:
        raise HTTPException(status_code=422, detail={"code": "unknown_answer", "message": "answer is yes or no"})
    try:
        row = _require().answer(code, yes=answer == "yes", by=by, contact_id=contact_id, message=message)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail={"code": "not_owner", "message": str(error)}) from None
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_ask", "message": f"no open ask {code.upper()}"})
    return {"ok": True, **audit.entry(row), "text": f"{answer}: {row.description} -> {row.status}"}


@router.post("/decide")
async def decide(body: DecideBody) -> Dict[str, Any]:
    """The plugin's ``protagine_self yes|no <code>``: the sidecar checks the owner and the code again."""
    return _answer(body.code, body.answer.strip().lower(), by=body.by, contact_id=body.contact_id,
                   message=body.message)


# -- the audit log (7.8) -----------------------------------------------------------------

@router.get("/log")
async def log(limit: int = 20, status: Optional[str] = None, kind: Optional[str] = None) -> Dict[str, Any]:
    mind = _require()
    entries = audit.log(mind.store, limit=max(1, min(int(limit), 500)),
                        status=[s for s in (status or "").split(",") if s] or None,
                        kind=[k for k in (kind or "").split(",") if k] or None)
    return {"entries": entries, "text": audit.render_log(entries)}


@router.get("/why/{intention_id}")
async def why(intention_id: str) -> Dict[str, Any]:
    value = audit.why(_require().store, intention_id)
    if value is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_intention"})
    return value


@router.get("/log/{intention_id}")
async def log_entry(intention_id: str) -> Dict[str, Any]:
    return await why(intention_id)


@router.get("/asks")
async def asks() -> Dict[str, Any]:
    entries = _require().asks()
    lines = [f"[{item['ask_code']}] {item['title']} ({item['decision_reason']}; expires {item['expires_at']})"
             for item in entries]
    return {"asks": entries, "text": "\n".join(lines) if lines else "(no open asks)"}


@router.post("/asks/{code}/{answer}")
async def answer(code: str, answer: str, body: AnswerBody | None = None) -> Dict[str, Any]:
    if answer not in {"yes", "no"}:
        raise HTTPException(status_code=404, detail={"code": "unknown_answer"})
    body = body or AnswerBody()
    return _answer(code, answer, by=body.by, contact_id=body.contact_id, message=body.message)


# -- switches and settings (7.9, 7.10) ----------------------------------------------------

@router.post("/off")
async def off(body: OffBody | None = None) -> Dict[str, Any]:
    body = body or OffBody()
    return _require().off(reason=body.reason, by=body.by)


@router.post("/on")
async def on(body: OffBody | None = None) -> Dict[str, Any]:
    body = body or OffBody()
    return _require().on(by=body.by)


@router.post("/tick")
async def tick() -> Dict[str, Any]:
    return await _require().tick(force=True)


@router.post("/rate")
async def rate(body: RateBody) -> Dict[str, Any]:
    if body.verdict.lower() not in VERDICTS:
        raise HTTPException(status_code=422, detail={"code": "unknown_verdict", "message": ", ".join(VERDICTS)})
    return _entry(_require().rate(body.id, body.verdict.lower()))


@router.post("/level")
async def level(body: LevelBody) -> Dict[str, Any]:
    if body.autonomy.lower() not in LEVELS:
        raise HTTPException(status_code=422, detail={"code": "unknown_level", "message": ", ".join(LEVELS)})
    return _require().set_level(body.autonomy.lower())


@router.post("/people/{contact_id}/permission")
async def permission(contact_id: str, body: PermissionBody) -> Dict[str, Any]:
    """The plugin's owner-only ``protagine_people set_permission``: may the mind reach this contact."""
    try:
        value = await _require().set_permission(contact_id, body.may_contact.strip().lower(), by=body.by)
    except ValueError as error:
        raise HTTPException(status_code=422, detail={"code": "bad_permission", "message": str(error)}) from None
    if value is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_contact"})
    return value


@router.post("/reset")
async def reset(body: ResetBody) -> Dict[str, Any]:
    if body.cls not in CLASSES:
        raise HTTPException(status_code=422, detail={"code": "unknown_class", "message": ", ".join(CLASSES)})
    return _require().reset(body.cls)


__all__ = ["get_mind", "router", "set_mind"]
