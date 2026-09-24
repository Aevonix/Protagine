"""``/v1/mind/people``: who people are, and the owner's say over them (architecture 4.7, 7.4, 7.10).

Reads are for everyone; a caller that names a viewer ``contact_id`` other than the owner sees
only who a person is (id, name, tier), never their handles, digest or permission, and only for
the one person a reference names: no listing, partial matches or 404 candidates. Mutations
(``permission``, ``cadence``, ``merge``) need the viewer to be the owner, or ``by: cli`` from the
local CLI, whose API key is the owner's; anyone may propose a link, which only files a
candidate the owner confirms. ``may_contact`` is raised nowhere else (an opt-out only lowers it).

Interface (JSON; the plugin's ``protagine_people`` and ``protagine people`` are the clients):
  GET  /?q=&contact_id=&limit=            who: [{contact_id, display_name, trust_tier, may_contact,
                                          cadence_minutes, last_interaction_at, handles}]
  GET  /proposals                         pending link proposals (a name suggested a handle)
  GET  /{who}?contact_id=                 inspect: the record with its digest, handles, open
                                          proposals and permission history
  POST /{who}/permission                  {may_contact: never|ask|auto, contact_id?, by?, reason?}
  POST /{who}/cadence                     {minutes: int|null, contact_id?, by?}
  POST /merge                             {keep, drop, contact_id?, by?}
  POST /link                              {contact_id, gateway, address, evidence_refs?, by?}
``{who}``, ``keep``, ``drop`` and the link's ``contact_id`` are references: a contact id, a
phone number, an email, ``gateway:address`` or a unique name. With the mind's people faculty
off, ``merge``, ``link`` and ``cadence`` answer 409 ``people_off``.
"""

from __future__ import annotations

import json
import logging
from contextlib import closing
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from protagine.contacts.models import MAY_CONTACT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/mind/people", tags=["people"])

PUBLIC_FIELDS = ("contact_id", "display_name", "trust_tier")
PERMISSION_ACTIONS = ("may_contact_set", "opt_out", "cadence_set")
SOURCE_CONFLICTS = {"source_erased", "source_attribution_preimage_changed"}


class PermissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    may_contact: str = Field(min_length=1, max_length=8)
    contact_id: Optional[str] = Field(default=None, max_length=256)
    by: str = Field(default="owner", max_length=64)
    reason: str = Field(default="", max_length=400)


class CadenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: Optional[int] = None
    contact_id: Optional[str] = Field(default=None, max_length=256)
    by: str = Field(default="owner", max_length=64)


class MergeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep: str = Field(min_length=1, max_length=256)
    drop: str = Field(min_length=1, max_length=256)
    contact_id: Optional[str] = Field(default=None, max_length=256)
    by: str = Field(default="owner", max_length=64)


class LinkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: str = Field(min_length=1, max_length=256)
    gateway: str = Field(min_length=1, max_length=64)
    address: str = Field(min_length=1, max_length=512)
    evidence_refs: List[str] = Field(default_factory=list, max_length=10)
    by: str = Field(default="owner", max_length=256)


# -- wiring ------------------------------------------------------------------------------

def _store() -> Any:
    from protagine.api.routers import host
    if host._contacts_store is None:
        raise HTTPException(status_code=503, detail={"code": "contacts_unavailable",
                                                     "message": "the contact store is not running"})
    return host._contacts_store


def _owner_id() -> Optional[str]:
    from protagine.identity import get_owner_contact_id
    return get_owner_contact_id()


def _ledger() -> Any:
    try:
        from protagine import get_state_dir
        from protagine.turns import get_turn_idempotency_ledger
        return get_turn_idempotency_ledger(get_state_dir())
    except Exception as error:  # no ledger: a merge still moves handles; sources wait for the worker
        logger.warning("people: the source ledger is unavailable (%s)", type(error).__name__)
        return None


async def person_sources(contact_id: str) -> List[str]:
    """The person-scoped sources a contact holds in the ledger: what a merge moves (C2)."""
    ledger = _ledger()
    if ledger is None:
        return []
    with closing(ledger._connect()) as conn:
        rows = conn.execute("SELECT turn_id FROM turn_sources WHERE contact_id=? AND scope='person' AND turn_id "
                            "NOT IN (SELECT turn_id FROM source_erasures) ORDER BY turn_id", (contact_id,)).fetchall()
    return [str(row[0]) for row in rows]


def reattribute_hooks(*, reconcile: bool = True) -> List[Any]:
    """``reattribute(old_id, new_id)`` of the stores that key rows by contact (comms, affect,
    commitments), then (``reconcile``) the ledger move of the merge's sources. A row whose contact
    and ledger source disagree is purged as erased, so comms and affect move a sourced row only
    once its source moved: here they move what they can, and the reconciliation of the merge's
    sources (``social_state.reconcile_identity_sources``) moves the rest, now or on the source
    worker's retry. The router reconciles itself, to report what moved."""
    from protagine.api.routers import host
    hooks = []
    for name in ("_comms_log", "_affect_store", "_commitment_store"):
        hook = getattr(getattr(host, name, None), "reattribute", None)
        if callable(hook):
            hooks.append(hook)
    hooks.append(_reattribute_opinions)
    if reconcile:
        hooks.append(reconcile_merge)
    return hooks


def _reattribute_opinions(drop_id: str, keep_id: str) -> int:
    """The running mind's views about the dropped contact become views about the kept one (integration
    map X14): read when the merge runs, so a mind set up after the store is the one moved."""
    from protagine.api.routers import mind as mind_router
    store = getattr(getattr(mind_router.get_mind(), "opinions", None), "store", None)
    move = getattr(store, "reattribute_subject", None)
    return int(move(drop_id, keep_id) or 0) if callable(move) else 0


async def reconcile_merge(drop_id: str, keep_id: str) -> None:
    """Move a merge's sources in the ledger now (the source worker retries whatever fails)."""
    from protagine.api.routers import host
    if host._contacts_store is not None:
        await _reconcile(host._contacts_store, prefix=f"merge:{drop_id}:")


def _is_owner_viewer(contact_id: Optional[str]) -> bool:
    """No viewer named: the API key holder (the local owner). A named viewer must be the owner."""
    if contact_id is None:
        return True
    owner = _owner_id()
    return bool(owner) and contact_id == owner


def _require_owner(contact_id: Optional[str], by: str) -> str:
    """7.10: a mutation from the owner's session or the local CLI; returns who performed it."""
    if by == "cli":
        return "cli"
    owner = _owner_id()
    if not owner or contact_id != owner:
        raise HTTPException(status_code=403, detail={
            "code": "not_owner", "message": "only the owner can change who may be contacted, cadences or merges"})
    return owner


async def _resolve(reference: str, *, candidates_shown: bool = True) -> Any:
    """The one contact a reference names; otherwise 404 with the people it could mean (who they
    are only), so an ambiguous name is answered, never guessed. A caller that is not the owner
    (``candidates_shown`` False) gets the 404 alone: the contact list is not a guest's to browse."""
    store = _store()
    contact = await store.resolve_reference(reference)
    if contact is None:
        try:
            candidates = [_row(c, [], full=False) for c in await store.search(reference, limit=5)] \
                if candidates_shown else []
        except Exception:
            candidates = []
        raise HTTPException(status_code=404, detail={
            "code": "unknown_contact", "candidates": candidates,
            "message": f"no single contact matches {reference!r}"
                       + (": " + "; ".join(_line(row) for row in candidates) if candidates else "")})
    return contact


# -- rendering ---------------------------------------------------------------------------

def _handles(handles: List[Any]) -> List[Dict[str, Any]]:
    return [{"gateway": h.gateway, "address": h.address, "verified": bool(h.verified)} for h in handles]


def _row(contact: Any, handles: List[Any], *, full: bool) -> Dict[str, Any]:
    if not full:
        return {key: getattr(contact, key) for key in PUBLIC_FIELDS}
    from protagine.mind.authority import may_contact_of
    return {"contact_id": contact.contact_id, "display_name": contact.display_name, "trust_tier": contact.trust_tier,
            # The owner is ``auto`` by identity, whatever an older row's column says.
            "may_contact": may_contact_of(contact, owner_id=_owner_id()), "cadence_minutes": contact.cadence_minutes,
            "last_interaction_at": contact.last_interaction_at, "interaction_count": contact.interaction_count,
            "handles": _handles(handles)}


def _line(row: Dict[str, Any]) -> str:
    parts = [f"{row['contact_id']}  {row.get('display_name') or '(no name)'}  ({row.get('trust_tier')}"]
    if "may_contact" in row:
        parts[0] += f", may_contact={row['may_contact']}"
        if row.get("cadence_minutes"):
            parts[0] += f", every {row['cadence_minutes']} min"
        if row.get("last_interaction_at"):
            parts[0] += f", last {row['last_interaction_at']}"
    parts[0] += ")"
    handles = ", ".join(f"{h['gateway']}:{h['address']}" for h in row.get("handles") or [])
    return parts[0] + (f"  {handles}" if handles else "")


def _detail(raw: Any) -> Any:
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return raw


# -- reads -------------------------------------------------------------------------------

@router.get("")
@router.get("/")
async def who(q: str = "", contact_id: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    """Who is this: by id, handle or name; no query lists the newest contacts. A viewer who is not
    the owner gets only the one person the query names exactly: no listing and no partial matches,
    so a guest learns who someone is without browsing the contact list."""
    store = _store()
    full = _is_owner_viewer(contact_id)
    if full:
        found = await store.search(q, limit=max(1, min(int(limit), 100)))
    else:
        exact = await store.resolve_reference(q) if q.strip() else None
        found = [exact] if exact is not None else []
    rows = []
    for contact in found:
        rows.append(_row(contact, await store.get_handles(contact.contact_id) if full else [], full=full))
    return {"contacts": rows, "text": "\n".join(_line(row) for row in rows) or "(nobody matches)"}


@router.get("/proposals")
async def proposals(limit: int = 50) -> Dict[str, Any]:
    """Open link proposals: a name suggested that a handle is this person; the owner decides."""
    rows = await _store().list_handle_proposals(limit=max(1, min(int(limit), 100)))
    lines = [f"{row['candidate_id']}: is {row['gateway']}:{row['address']} {row.get('display_name') or row['contact_id']}?"
             for row in rows]
    return {"proposals": rows, "text": "\n".join(lines) or "(no open proposals)"}


@router.get("/{reference}")
async def inspect(reference: str, contact_id: Optional[str] = None) -> Dict[str, Any]:
    """One person: the record, digest, handles, open proposals and permission history (owner view)."""
    store = _store()
    contact = await _resolve(reference, candidates_shown=_is_owner_viewer(contact_id))
    if not _is_owner_viewer(contact_id):
        row = _row(contact, [], full=False)
        return {"contact": row, "text": _line(row)}
    row = _row(contact, await store.get_handles(contact.contact_id), full=True)
    row.update(digest=contact.digest, first_seen_at=contact.first_seen_at, notes=contact.notes, tags=contact.tags,
               introduced_by=contact.introduced_by, import_source=contact.import_source)
    pending = [p for p in await store.list_handle_proposals(limit=100) if p["contact_id"] == contact.contact_id]
    history = [{"action": a["action"], "at": a["created_at"], "by": a["performed_by"], "detail": _detail(a["detail"])}
               for a in await store.get_audit_log(contact.contact_id, limit=50) if a["action"] in PERMISSION_ACTIONS]
    text = [_line(row)]
    if contact.digest:
        text.append(contact.digest)
    text += [f"proposal {p['candidate_id']}: {p['gateway']}:{p['address']}" for p in pending]
    text += [f"{h['at']} {h['action']} by {h['by']}: {h['detail']}" for h in history[:5]]
    return {"contact": row, "proposals": pending, "permission_history": history, "text": "\n".join(text)}


# -- owner mutations -----------------------------------------------------------------------

def _require_people() -> None:
    """Merges, link proposals and cadences are the people faculty's (M5): with it off (the
    ``full-people`` ablation) they are refused, and who, inspect and permission stay."""
    from protagine.api.routers.mind import faculty_on
    if not faculty_on("people"):
        raise HTTPException(status_code=409, detail={"code": "people_off",
                                                     "message": "the people faculty is off"})


@router.post("/merge")
async def merge(body: MergeBody) -> Dict[str, Any]:
    """C2: fold ``drop`` into ``keep``; handles, sources, comms and affect follow the person."""
    _require_people()
    performed_by = _require_owner(body.contact_id, body.by)
    store = _store()
    keep, drop = await _resolve(body.keep), await _resolve(body.drop)
    if keep.contact_id == drop.contact_id:
        raise HTTPException(status_code=422, detail={"code": "same_contact", "message": "keep and drop are one contact"})
    if drop.contact_id == _owner_id():
        raise HTTPException(status_code=422, detail={"code": "owner_cannot_be_dropped",
                                                     "message": "merge the other record into the owner instead"})
    try:
        merged = await store.merge(keep.contact_id, drop.contact_id, performed_by=performed_by,
                                   reattribute=reattribute_hooks(reconcile=False), sources_of=person_sources)
    except ValueError as error:
        raise HTTPException(status_code=409, detail={"code": "merge_refused", "message": str(error)}) from None
    reconciled, pending = await _reconcile(store, prefix=f"merge:{drop.contact_id}:")
    row = _row(merged, await store.get_handles(merged.contact_id), full=True)
    return {"ok": True, "contact": row, "dropped": drop.contact_id, "sources_moved": reconciled,
            "sources_pending": pending, "text": f"merged {drop.contact_id} into {_line(row)}"}


@router.post("/link")
async def link(body: LinkBody) -> Dict[str, Any]:
    """Propose that a handle is this person: a candidate the owner confirms, never attribution."""
    _require_people()
    store = _store()
    contact = await _resolve(body.contact_id,
                             candidates_shown=body.by in {"owner", "cli"} or _is_owner_viewer(body.by))
    holder = await store.resolve_messaging_handle(body.gateway, body.address)
    if holder is not None and holder.contact_id == contact.contact_id:
        return {"ok": True, "status": "already_linked", "contact_id": contact.contact_id,
                "text": f"{body.gateway}:{body.address} is already {contact.display_name or contact.contact_id}"}
    try:
        candidate = await store.propose_handle_link(
            contact.contact_id, body.gateway, body.address, source="proposal",
            evidence_refs=list(body.evidence_refs) or [f"proposed_by:{body.by}"])
    except ValueError as error:
        raise HTTPException(status_code=422, detail={"code": str(error), "message": str(error)}) from None
    await store.record_audit(contact.contact_id, "link_proposed",
                             {"candidate_id": candidate["candidate_id"], "gateway": candidate["gateway"],
                              "held_by": holder.contact_id if holder else None}, performed_by=body.by)
    return {"ok": True, **candidate, "held_by": holder.contact_id if holder else None,
            "text": f"proposed {candidate['gateway']}:{candidate['address']} for "
                    f"{contact.display_name or contact.contact_id}; the owner confirms"}


@router.post("/{reference}/permission")
async def permission(reference: str, body: PermissionBody) -> Dict[str, Any]:
    """7.4: the owner sets ``may_contact`` in any direction; nothing else raises it."""
    performed_by = _require_owner(body.contact_id, body.by)
    value = body.may_contact.strip().lower()
    if value not in MAY_CONTACT:
        raise HTTPException(status_code=422, detail={"code": "unknown_permission", "message": "never, ask or auto"})
    contact = await _resolve(reference)
    if contact.contact_id == _owner_id():
        raise HTTPException(status_code=422, detail={"code": "owner_is_auto",
                                                     "message": "the owner is always reachable"})
    updated = await _store().set_may_contact(contact.contact_id, value, by=performed_by, reason=body.reason)
    return {"ok": True, "contact_id": updated.contact_id, "may_contact": updated.may_contact,
            "text": f"{updated.display_name or updated.contact_id}: may_contact={updated.may_contact}"}


@router.post("/{reference}/cadence")
async def cadence(reference: str, body: CadenceBody) -> Dict[str, Any]:
    """The owner's check-in cadence in minutes; null clears it."""
    _require_people()
    performed_by = _require_owner(body.contact_id, body.by)
    contact = await _resolve(reference)
    try:
        updated = await _store().set_cadence(contact.contact_id, body.minutes, by=performed_by)
    except ValueError as error:
        raise HTTPException(status_code=422, detail={"code": "bad_cadence", "message": str(error)}) from None
    every = f"every {updated.cadence_minutes} min" if updated.cadence_minutes else "no cadence"
    return {"ok": True, "contact_id": updated.contact_id, "cadence_minutes": updated.cadence_minutes,
            "text": f"{updated.display_name or updated.contact_id}: {every}"}


async def _reconcile(store: Any, *, prefix: str) -> tuple[int, int]:
    """Move the merge's sources in the ledger now; whatever fails stays for the source worker."""
    ledger = _ledger()
    operations = [op for op in await store.pending_identity_reconciliations(limit=500)
                  if str(op.get("operation_id", "")).startswith(prefix)]
    if ledger is None:
        return 0, len(operations)
    from protagine.api.routers.social_state import reconcile_identity_sources
    moved = 0
    for operation in operations:
        try:
            await reconcile_identity_sources(store, ledger, operation)
            moved += len(operation["affected_source_ids"])
        except ValueError as error:
            if str(error) in SOURCE_CONFLICTS:
                await store.mark_sources_conflicted(operation["operation_id"], str(error))
            else:
                logger.warning("people: source reconciliation for %s deferred (%s)", operation["operation_id"], error)
        except Exception as error:
            logger.warning("people: source reconciliation for %s deferred (%s)", operation["operation_id"],
                           type(error).__name__)
    left = [op for op in await store.pending_identity_reconciliations(limit=500)
            if str(op.get("operation_id", "")).startswith(prefix)]
    return moved, sum(len(op["affected_source_ids"]) for op in left)


__all__ = ["person_sources", "reattribute_hooks", "reconcile_merge", "router"]
