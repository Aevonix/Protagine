"""Model tools: ``protagine_self``, ``protagine_people`` and the memory tools.

Tool handlers receive ``session_id``; the session map turns it into a sender.
Mutations are accepted only from the owner's own interactive session: never
from a kanban worker, and never from a cron run, whose prompt is a stored job
anyone with the tool could have scheduled. ``yes``/``no`` also need the typed
ask code inside the owner's own message for that turn, so neither a guest, a
worker, a cron job nor an injected page can approve anything (architecture
7.7, 7.10).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable
from urllib.parse import quote

from .body import mind_state
from .capture import SessionMap
from .client import ProtagineClient, Settings, SidecarUnavailable
from .commands import ROUTES_MISSING

ASK_CODE = re.compile(r"^[A-Z0-9]{3,8}$")
VERDICTS = ("actioned", "dismissed", "ignored", "useful", "not_useful", "wrong")
# Turns nobody typed: stock cron runs its agents with ``platform="cron"`` and no sender.
AUTOMATED_PLATFORMS = frozenset({"cron"})
PEOPLE_OPERATIONS = ("who", "inspect", "propose_link", "merge", "set_permission", "set_cadence")
OWNER_PEOPLE_OPERATIONS = frozenset({"merge", "set_permission", "set_cadence"})
# A person as the model sees it: who they are to everyone; permission, cadence and handles to the owner.
PUBLIC_PERSON = ("contact_id", "display_name", "trust_tier")
OWNER_PERSON = PUBLIC_PERSON + ("may_contact", "cadence_minutes", "last_interaction_at", "digest", "handles")

# Every schema is sent with every model request, so each says only what the model needs to pick
# the tool and fill its arguments; the handlers validate and explain the rest.
SELF_SCHEMA = {
    "name": "protagine_self",
    "description": "Your own mind: state (level, budgets, open asks with codes), log, why <id>, rate <id> with a "
                   "verdict, or answer an ask yes/no by its code. rate, yes and no are owner only, and the code "
                   "must appear in the owner's own message.",
    "parameters": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": ["state", "log", "why", "rate", "yes", "no"]},
        "id": {"type": "string"}, "verdict": {"type": "string", "enum": list(VERDICTS)},
        "code": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["operation"]}}
PEOPLE_SCHEMA = {
    "name": "protagine_people",
    "description": "contact_id: a name, handle or id. propose_link: handle (gateway:address) is contact_id; owner "
                   "confirms. Owner only: set_permission, set_cadence (minutes, 0 clears), merge (drop into "
                   "contact_id).",
    "parameters": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": list(PEOPLE_OPERATIONS)}, "contact_id": {"type": "string"},
        "drop": {"type": "string"}, "handle": {"type": "string"},
        "permission": {"type": "string", "enum": ["never", "ask", "auto"]}, "minutes": {"type": "integer"}},
        "required": ["operation"]}}
# With mind.faculties.people false (the full-people ablation) the tool keeps its pre-M5 surface.
M4_PEOPLE_OPERATIONS = ("who", "inspect", "set_permission")
M4_PEOPLE_SCHEMA = {
    "name": "protagine_people", "description": "contact_id: a name, handle or id. Owner only: set_permission.",
    "parameters": {"type": "object", "required": ["operation"], "properties": {
        "operation": {"type": "string", "enum": list(M4_PEOPLE_OPERATIONS)}, "contact_id": {"type": "string"},
        "permission": {"type": "string", "enum": ["never", "ask", "auto"]}}}}
SEARCH_SCHEMA = {
    "name": "protagine_memory_search",
    "description": "Search retained evidence about the current participant beyond this turn's recall; returns "
                   "excerpts with speaker, time and source references.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                   "required": ["query"]}}
FORGET_SCHEMA = {
    "name": "protagine_memory_forget",
    "description": "Owner only: forget retained sources by exact source_id when the owner asks to remove "
                   "something from memory.",
    "parameters": {"type": "object", "properties": {"source_ids": {"type": "array", "items": {"type": "string"}}},
                   "required": ["source_ids"]}}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _error(message: str) -> str:
    return _json({"error": message})


def _unavailable(reason: str) -> str:
    """One final answer: the tool cannot work on this lane and a retry would only repeat it."""
    return _json({"unavailable": True, "retry": False, "reason": reason})


class Tools:
    def __init__(self, client: ProtagineClient, sessions: SessionMap, settings: Settings):
        self.client, self.sessions, self.settings = client, sessions, settings
        faculties = settings.mind().get("faculties")
        self.people_on = not isinstance(faculties, dict) or faculties.get("people") is not False

    def handlers(self) -> list[tuple[dict[str, Any], Callable[..., str]]]:
        return [(SELF_SCHEMA, self.self_tool), (PEOPLE_SCHEMA if self.people_on else M4_PEOPLE_SCHEMA, self.people_tool),
                (SEARCH_SCHEMA, self.memory_search), (FORGET_SCHEMA, self.memory_forget)]

    # -- helpers ------------------------------------------------------------------

    def _owner(self, session_id: str) -> bool:
        """The owner's own interactive session: never a kanban worker, whose turns carry no
        sender, and never a cron run, whose prompt is a stored job rather than a typed message."""
        if os.environ.get("HERMES_KANBAN_TASK"):
            return False
        info = self.sessions.get(session_id)
        if info is not None and info.platform.strip().lower() in AUTOMATED_PLATFORMS:
            return False
        return self.sessions.is_owner(session_id) is True

    def _mind(self, method: str, path: str, **kwargs: Any) -> str:
        if self.client.has_mind_routes() is not True:
            return _error(ROUTES_MISSING)
        try:
            response = self.client.request(method, path, timeout=5, **kwargs)
        except SidecarUnavailable:
            return _error("the sidecar is unreachable")
        if response.status_code == 404:
            return _error(ROUTES_MISSING if not response.text else f"not found: {response.text[:200]}")
        if not response.is_success:
            return _error(f"sidecar HTTP {response.status_code}")
        return response.text

    # -- protagine_self ---------------------------------------------------------------

    def self_tool(self, args: Any = None, *, session_id: str = "", **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        operation = str(args.get("operation") or "")
        if operation in {"state", "status"}:
            detail = mind_state(self.client) or {}
            mind = self.settings.mind()
            return _json({"enabled": mind.get("enabled", True) is not False and detail.get("enabled") is not False,
                          "autonomy": detail.get("autonomy") or mind.get("autonomy", "standard"),
                          "sidecar_reachable": self.client.health() is not None,
                          "mind_routes": self.client.has_mind_routes() is True, **detail})
        if operation == "log":
            limit = max(1, min(int(args.get("limit") or 20), 100))
            return self._mind("GET", "/v1/mind/log", params={"limit": limit})
        if operation == "why":
            return self._mind("GET", f"/v1/mind/why/{args.get('id')}") if args.get("id") else _error("id is required")
        if operation == "rate":
            if not args.get("id") or args.get("verdict") not in VERDICTS:
                return _error(f"id and verdict ({'|'.join(VERDICTS)}) are required")
            if not self._owner(session_id):
                return _error("only the owner can rate an intention")
            return self._mind("POST", "/v1/mind/rate", json={"id": str(args["id"]), "verdict": args["verdict"]})
        if operation in {"yes", "no"}:
            return self._answer_ask(operation, str(args.get("code") or ""), session_id)
        return _error("unknown operation")

    def _answer_ask(self, answer: str, code: str, session_id: str) -> str:
        """``POST /v1/mind/decide`` only for the owner's own turn whose message carries the typed code."""
        code = code.strip().upper()
        if not code:
            return _error("code is required")
        if not ASK_CODE.fullmatch(code):
            return _error("the ask code is 3 to 8 letters or digits, as shown in the notice")
        if not self._owner(session_id):
            return _error("only the owner can answer an ask")
        info = self.sessions.get(session_id)
        if info is None or not re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", info.user_message.upper()):
            return _error("the ask code must appear in the owner's own message")
        return self._mind("POST", "/v1/mind/decide", json={
            "code": code, "answer": answer, "session_id": session_id, "message": info.user_message[:8000],
            "contact_id": self.sessions.contact_id(session_id) or self.settings.owner_contact_id() or None})

    # -- protagine_people ----------------------------------------------------------

    def people_tool(self, args: Any = None, *, session_id: str = "", **_: Any) -> str:
        """``/v1/mind/people``: who, inspect and propose_link for everyone (a guest sees who someone is);
        permission, cadence and merge from the owner's session, checked again by the sidecar."""
        args = args if isinstance(args, dict) else {}
        operation, who = str(args.get("operation") or ""), str(args.get("contact_id") or "").strip()
        if operation not in (offered := PEOPLE_OPERATIONS if self.people_on else M4_PEOPLE_OPERATIONS):
            return _error(f"operation is one of {', '.join(offered)}")
        owner, viewer = self._owner(session_id), self.sessions.contact_id(session_id) or ""
        if operation in OWNER_PEOPLE_OPERATIONS and not owner:
            return _error("only the owner can change who may be contacted, cadences or merges")
        if operation != "who" and not who:
            return _error("contact_id (a name, handle or id) is required")
        path, seen = "/v1/mind/people/" + quote(who, safe=""), {} if owner else {"contact_id": viewer}
        change = {"contact_id": viewer or self.settings.owner_contact_id() or None, "by": "owner"}
        if operation in {"who", "inspect"}:
            params = {"q": who[:256], "limit": 10, **seen} if operation == "who" else seen
            return self._people("GET", "/v1/mind/people" if operation == "who" else path, owner, params=params)
        if operation == "propose_link":
            gateway, _, address = str(args.get("handle") or "").partition(":")
            if not gateway.strip() or not address.strip():
                return _error("handle is gateway:address")
            return self._people("POST", "/v1/mind/people/link", None, json={
                "contact_id": who, "gateway": gateway.strip(), "address": address.strip(),
                "evidence_refs": [f"session:{session_id}"[:256]], "by": viewer or ("owner" if owner else "guest")})
        if operation == "merge":
            drop = str(args.get("drop") or "").strip()
            return self._people("POST", "/v1/mind/people/merge", None, timeout=60, json={
                "keep": who, "drop": drop, **change}) if drop else _error("drop (folded into contact_id) is required")
        if operation == "set_permission":
            if args.get("permission") not in {"never", "ask", "auto"}:
                return _error("permission is never, ask or auto")
            return self._people("POST", path + "/permission", None, json={"may_contact": args["permission"], **change})
        try:
            minutes = int(args.get("minutes") or 0)
        except (TypeError, ValueError):
            return _error("minutes is a whole number (0 clears the cadence)")
        return self._people("POST", path + "/cadence", None, json={"minutes": minutes if minutes > 0 else None, **change})

    def _people(self, method: str, path: str, owner: bool | None, **kwargs: Any) -> str:
        """A read (``owner`` set) as the persons this viewer may see; a change (``owner`` None) as its outcome."""
        if self.client.has_mind_routes() is not True:
            return _error(ROUTES_MISSING)
        try:
            response = self.client.request(method, path, timeout=kwargs.pop("timeout", 5), **kwargs)
        except SidecarUnavailable:
            return _error("the sidecar is unreachable")
        try:
            value = response.json()
        except ValueError:
            value = {}
        if not response.is_success:
            detail = value.get("detail") if isinstance(value, dict) else None
            return _error(str(detail.get("message") or detail.get("code")) if isinstance(detail, dict) else
                          ROUTES_MISSING if response.status_code == 404 else f"sidecar HTTP {response.status_code}")
        if owner is None:
            return _json({key: value[key] for key in ("ok", "text", "status", "may_contact", "cadence_minutes",
                                                      "candidate_id", "dropped") if key in value})
        keep = OWNER_PERSON if owner else PUBLIC_PERSON

        def person(item: dict[str, Any]) -> dict[str, Any]:
            return {key: [f"{h.get('gateway')}:{h.get('address')}" for h in item[key] or []] if key == "handles"
                    else item[key] for key in keep if key in item}

        if "contacts" in value:
            return _json([person(item) for item in value["contacts"] if isinstance(item, dict)])
        record = person(value.get("contact") or {})
        if owner and value.get("proposals"):
            record["proposals"] = [f"{p.get('gateway')}:{p.get('address')}" for p in value["proposals"]]
        return _json(record)

    # -- memory tools ----------------------------------------------------------------

    def memory_search(self, args: Any = None, *, session_id: str = "", **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        query = str(args.get("query") or "").strip()
        if not query:
            return _error("query is required")
        contact = self.sessions.contact_id(session_id)
        if not contact:
            return _unavailable("this turn has no resolved participant; answer from the message")
        try:
            response = self.client.post("/v1/host/memory/search", timeout=10, json={
                "identity": {"host_id": "hermes"}, "person_id": contact, "session_id": session_id,
                "query": query[:4096], "limit": max(1, min(int(args.get("limit") or 5), 20))})
        except SidecarUnavailable:
            return _error("memory search is unavailable")
        if not response.is_success:
            return _error(f"memory search failed (HTTP {response.status_code})")
        value = response.json()
        return _json({"content": value.get("content", ""), "count": value.get("count", 0),
                      "source_refs": value.get("source_refs", [])})

    def memory_forget(self, args: Any = None, *, session_id: str = "", **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        ids = [str(item) for item in (args.get("source_ids") or []) if str(item).strip()]
        if not ids:
            return _error("source_ids is required")
        if not self._owner(session_id):
            return _error("only the owner can forget sources")
        contact = self.sessions.contact_id(session_id)
        try:
            response = self.client.post("/v1/host/memory/sources/forget", timeout=5,
                                        json={"contact_id": contact, "source_ids": list(dict.fromkeys(ids))})
        except SidecarUnavailable:
            return _error("source removal is unconfirmed; the sidecar is unreachable")
        if response.status_code in {409, 422}:
            return _json({"source_erased": False, "error": "no matching sources; use exact source_id values"})
        if not response.is_success:
            return _error(f"forget failed (HTTP {response.status_code})")
        return response.text


__all__ = ["ASK_CODE", "AUTOMATED_PLATFORMS", "FORGET_SCHEMA", "PEOPLE_OPERATIONS", "PEOPLE_SCHEMA", "SEARCH_SCHEMA",
           "SELF_SCHEMA", "Tools", "VERDICTS"]
