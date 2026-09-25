"""``/v1/mind/opinions``: the owner's view of the agent's opinions, and the two owner controls.

Architecture 4.4. Reads are audience-filtered: the owner (``contact_id`` equal to
the owner's, or ``by=cli``) sees every stance; anyone else sees only the stances
whose audience is ``all``, and an owner-audience stance asked for by id is a 404,
the same answer as an unknown one. The owner's controls (withdraw and reconsider,
with their reasons) are owner-audience rows, and what a view rests on (the owner's
ledger rows) is shown to anyone else only by kind. Withdraw and reconsider are owner-only.

  GET  /v1/mind/opinions?q=&contact_id=&by=&history=false&limit=10 -> {enabled, opinions: [rows]}
  GET  /v1/mind/opinions/{id}?contact_id=&by=                       -> {opinion, history: [the topic's chain]}
  POST /v1/mind/opinions/{id}/withdraw    {reason, contact_id?, by?, correction_id?} -> {revision_id, status}
  POST /v1/mind/opinions/{id}/reconsider  {reason, contact_id?, by?, correction_id?} -> {revision_id, status}
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/v1/mind/opinions", tags=["mind"])
CHAIN = 50


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=1, max_length=1500)
    contact_id: Optional[str] = Field(default=None, max_length=256)
    by: Optional[str] = Field(default=None, max_length=64)
    correction_id: Optional[str] = Field(default=None, min_length=1, max_length=192)


def _opinions() -> Any:
    from protagine.api.routers.mind import get_mind
    mind = get_mind()
    opinions = getattr(mind, "opinions", None) if mind is not None else None
    if opinions is None:
        raise HTTPException(status_code=503, detail={"code": "mind_not_wired", "message": "the mind is not running"})
    return opinions


def _owner(opinions: Any, contact_id: Optional[str], by: Optional[str]) -> bool:
    owner = str(getattr(opinions.store, "owner_id", "") or "")
    return (by or "") == "cli" or bool(owner and (contact_id or "") == owner)


def _visible(row: Optional[Dict[str, Any]], owner: bool) -> bool:
    return row is not None and (owner or row.get("audience") == "all")


def _shown(row: Dict[str, Any], owner: bool) -> Dict[str, Any]:
    """The row as this viewer may read it: anyone but the owner gets no owner correction or decision, no
    premise text or ledger reference (only each premise's kind and role) and no model provenance."""
    if owner:
        return row
    return {**row, "owner_correction": None, "owner_decision": None, "processor": {},
            "premises": [{"kind": p.get("kind"), "role": p.get("role", "support")} for p in row.get("premises") or []]}


@router.get("")
async def list_opinions(q: str = "", contact_id: str = "", by: str = "", history: bool = False,
                        limit: int = 10) -> Dict[str, Any]:
    opinions = _opinions()
    if not opinions.enabled:
        return {"enabled": False, "opinions": []}
    owner = _owner(opinions, contact_id, by)
    audience = None if owner else "all"
    limit = max(1, min(int(limit), 100))
    if q.strip():
        rows = opinions.store.relevant(q, audience=audience, limit=limit)
    else:
        rows = opinions.store.revisions(history=history, audience=audience, limit=limit)
    return {"enabled": True, "opinions": [_shown(row, owner) for row in rows if _visible(row, owner)]}


@router.get("/{opinion_id}")
async def show_opinion(opinion_id: int, contact_id: str = "", by: str = "") -> Dict[str, Any]:
    opinions = _opinions()
    owner = _owner(opinions, contact_id, by)
    row = opinions.store.get(opinion_id)
    if not _visible(row, owner):
        raise HTTPException(status_code=404, detail={"code": "unknown_opinion"})
    head = None
    if row.get("topic"):
        head = opinions.store.head(subject_kind=row.get("subject_kind") or "topic", subject=row.get("subject") or "",
                                   topic=row["topic"])
    chain: List[Dict[str, Any]] = []
    current, seen = head or row, set()
    while current is not None and current.get("id") not in seen and len(chain) < CHAIN:
        seen.add(current.get("id"))
        chain.append(current)
        current = opinions.store.get(int(current["supersedes"])) if current.get("supersedes") else None
    if row["id"] not in seen:          # a row the head's chain does not reach (an erased topic): itself
        chain.insert(0, row)
    return {"opinion": _shown(row, owner), "history": [_shown(item, owner) for item in chain if _visible(item, owner)]}


@router.post("/{opinion_id}/{action}")
async def control(opinion_id: int, action: str, body: ControlBody) -> Dict[str, Any]:
    if action not in {"withdraw", "reconsider"}:
        raise HTTPException(status_code=404, detail={"code": "unknown_action"})
    opinions = _opinions()
    if not _owner(opinions, body.contact_id, body.by):
        raise HTTPException(status_code=403, detail={"code": "not_owner",
                                                     "message": "only the owner can withdraw or reconsider an opinion"})
    try:
        result = opinions.store.correct(opinion_id, action=action, correction_id=body.correction_id or uuid.uuid4().hex,
                                        reason=body.reason)
    except ValueError as error:
        raise HTTPException(status_code=409, detail={"code": str(error)}) from None
    return {"revision_id": result["revision_id"], "status": result["status"]}


__all__ = ["router"]
