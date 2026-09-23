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

from .body import mind_state
from .capture import SessionMap
from .client import ProtagineClient, Settings, SidecarUnavailable
from .commands import ROUTES_MISSING

ASK_CODE = re.compile(r"^[A-Z0-9]{3,8}$")
VERDICTS = ("actioned", "dismissed", "ignored", "useful", "not_useful", "wrong")
# Turns nobody typed: stock cron runs its agents with ``platform="cron"`` and no sender.
AUTOMATED_PLATFORMS = frozenset({"cron"})

SELF_SCHEMA = {
    "name": "protagine_self",
    "description": "The agent's own mind: its state (level, budgets, open asks with their codes), the audit "
                   "log, why an intention was decided, rating an intention's usefulness, and answering an "
                   "ask (yes/no with its code) for the owner. Mutations are refused for anyone but the owner, "
                   "and an answer is accepted only when the owner's own message contains the code.",
    "parameters": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": ["state", "log", "why", "rate", "yes", "no"]},
        "id": {"type": "string", "description": "Intention id for why and rate"},
        "verdict": {"type": "string", "enum": list(VERDICTS), "description": "Rating for rate"},
        "code": {"type": "string", "description": "Ask code for yes/no, as the owner typed it"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Log entries"}},
        "required": ["operation"], "additionalProperties": False},
}
PEOPLE_SCHEMA = {
    "name": "protagine_people",
    "description": "Known contacts: list them, show one, or (owner only) set whether the agent may reach "
                   "out to someone: never, ask or auto.",
    "parameters": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": ["list", "show", "set_permission"]},
        "contact_id": {"type": "string"},
        "permission": {"type": "string", "enum": ["never", "ask", "auto"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        "required": ["operation"], "additionalProperties": False},
}
SEARCH_SCHEMA = {
    "name": "protagine_memory_search",
    "description": "Search retained evidence for the current participant. Returns excerpts with their "
                   "source references; keep speaker, time and corrections as given.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 4096},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        "required": ["query"], "additionalProperties": False},
}
FORGET_SCHEMA = {
    "name": "protagine_memory_forget",
    "description": "Owner only: forget retained sources by their exact source_id values, when the owner "
                   "asks for something to be removed from memory.",
    "parameters": {"type": "object", "properties": {
        "source_ids": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "string"}}},
        "required": ["source_ids"], "additionalProperties": False},
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _error(message: str) -> str:
    return _json({"error": message})


class Tools:
    def __init__(self, client: ProtagineClient, sessions: SessionMap, settings: Settings):
        self.client, self.sessions, self.settings = client, sessions, settings

    def handlers(self) -> list[tuple[dict[str, Any], Callable[..., str]]]:
        return [(SELF_SCHEMA, self.self_tool), (PEOPLE_SCHEMA, self.people_tool),
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
        args = args if isinstance(args, dict) else {}
        operation = str(args.get("operation") or "")
        try:
            if operation == "list":
                response = self.client.get("/v1/host/contacts", params={"limit": int(args.get("limit") or 20)})
            elif operation == "show":
                if not args.get("contact_id"):
                    return _error("contact_id is required")
                response = self.client.get(f"/v1/host/contacts/{args['contact_id']}")
            elif operation == "set_permission":
                if not self._owner(session_id):
                    return _error("only the owner can change who may be contacted")
                if not args.get("contact_id") or args.get("permission") not in {"never", "ask", "auto"}:
                    return _error("contact_id and permission (never|ask|auto) are required")
                return self._mind("POST", f"/v1/mind/people/{args['contact_id']}/permission",
                                  json={"may_contact": args["permission"]})
            else:
                return _error("unknown operation")
        except SidecarUnavailable:
            return _error("the sidecar is unreachable")
        if not response.is_success:
            return _error(f"sidecar HTTP {response.status_code}")
        value = response.json()
        contacts = value.get("contacts") if isinstance(value, dict) and "contacts" in value else [value]
        keep = ("contact_id", "display_name", "trust_tier", "interaction_allowed", "tags", "last_interaction_at")
        return _json([{key: item.get(key) for key in keep if key in item} for item in contacts if isinstance(item, dict)])

    # -- memory tools ----------------------------------------------------------------

    def memory_search(self, args: Any = None, *, session_id: str = "", **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        query = str(args.get("query") or "").strip()
        if not query:
            return _error("query is required")
        contact = self.sessions.contact_id(session_id)
        if not contact:
            return _error("this turn has no resolved participant")
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


__all__ = ["ASK_CODE", "AUTOMATED_PLATFORMS", "FORGET_SCHEMA", "PEOPLE_SCHEMA", "SEARCH_SCHEMA", "SELF_SCHEMA",
           "Tools", "VERDICTS"]
