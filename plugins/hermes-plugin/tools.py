"""Model tools: ``protagine_self``, ``protagine_people`` and the memory tools.

Tool handlers receive ``session_id``; the session map turns it into a sender.
Mutations are accepted only from the owner's own interactive session: never
from a kanban worker, and never from a cron run, whose prompt is a stored job
anyone with the tool could have scheduled. ``yes``/``no`` also need the typed
ask code inside the owner's own message for that turn, so neither a guest, a
worker, a cron job nor an injected page can approve anything (architecture
7.7, 7.10). The mind's record (what it works on, its log, why it acted, the
self-narrative) is the owner's too: shown only in the owner's own session,
never to a guest, a group the owner shares or a worker.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable
from urllib.parse import quote

from .body import mind_state
from .capture import SessionMap, session_env
from .client import ProtagineClient, Settings, SidecarUnavailable
from .commands import ROUTES_MISSING

RECORD_IS_OWNERS = "the mind's record is the owner's; ask in the owner's own chat"

ASK_CODE = re.compile(r"^[A-Z0-9]{3,8}$")
VERDICTS = ("actioned", "dismissed", "ignored", "useful", "not_useful", "wrong")
# Turns nobody typed: stock cron runs its agents with ``platform="cron"`` and no sender.
AUTOMATED_PLATFORMS = frozenset({"cron"})

# Every schema is sent with every model request, so each says only what the model needs to pick
# the tool and fill its arguments; the handlers validate and explain the rest.
SELF_SCHEMA = {
    "name": "protagine_self",
    "description": "Your mind and record, the only source for what you did or decided: state (level, asks with "
                   "codes, working_on, narrative), log (newest first; since_hours, kind, recipient; cite ids; not in "
                   "the log means it did not happen), why <id>, rate <id> <verdict>, yes|no <code> (owner only, "
                   "code typed by the owner).",
    "parameters": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": ["state", "log", "why", "rate", "yes", "no"]},
        "id": {"type": "string"}, "verdict": {"type": "string", "enum": list(VERDICTS)},
        "code": {"type": "string"}, "limit": {"type": "integer"}, "since_hours": {"type": "number"},
        "kind": {"type": "string"}, "recipient": {"type": "string"}},
        "required": ["operation"]},
}
PEOPLE_SCHEMA = {
    "name": "protagine_people",
    "description": "Known contacts: list, show one, or set_permission (owner only) for whether the agent may "
                   "reach out to them: never, ask or auto.",
    "parameters": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": ["list", "show", "set_permission"]},
        "contact_id": {"type": "string"}, "permission": {"type": "string", "enum": ["never", "ask", "auto"]},
        "limit": {"type": "integer"}},
        "required": ["operation"]},
}
SEARCH_SCHEMA = {
    "name": "protagine_memory_search",
    "description": "Search retained evidence about the current participant beyond this turn's recall; returns "
                   "excerpts with speaker, time and source references.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                   "required": ["query"]},
}
FORGET_SCHEMA = {
    "name": "protagine_memory_forget",
    "description": "Owner only: forget retained sources by exact source_id when the owner asks to remove "
                   "something from memory.",
    "parameters": {"type": "object", "properties": {
        "source_ids": {"type": "array", "items": {"type": "string"}}},
        "required": ["source_ids"]},
}


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

    def _owners_own(self, session_id: str) -> bool:
        """The owner's session with no one else in it (a direct chat, the CLI): the mind's record (what it
        works on, whom it messaged, what it knows about itself) is shown there only, never to a guest, in a
        group the owner shares, or to a worker."""
        return self._owner(session_id) and session_env()[3].lower() in {"", "dm"}

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
            switches = {"enabled": mind.get("enabled", True) is not False and detail.get("enabled") is not False,
                        "autonomy": detail.get("autonomy") or mind.get("autonomy", "standard"),
                        "sidecar_reachable": self.client.health() is not None}
            if not self._owners_own(session_id):
                return _json(switches)
            narrative = self.client.narrative() or {}
            return _json({**switches, "mind_routes": self.client.has_mind_routes() is True, **detail, **switches,
                          # From the record, never free generation: the tasks in flight and the narrative.
                          "working_on": self._working_on(),
                          "narrative": str(narrative.get("text") or "") if narrative.get("enabled") is True else ""})
        if operation in {"log", "why"} and not self._owners_own(session_id):
            return _error(RECORD_IS_OWNERS)
        if operation == "log":
            params: dict[str, Any] = {"limit": max(1, min(int(args.get("limit") or 20), 100))}
            if args.get("since_hours") is not None and str(args.get("since_hours")).strip():
                try:
                    params["since_hours"] = max(0.0, float(args["since_hours"]))
                except (TypeError, ValueError):
                    return _error("since_hours is a number of hours")
            for name in ("kind", "recipient"):
                if str(args.get(name) or "").strip():
                    params[name] = str(args[name]).strip()
            return self._mind("GET", "/v1/mind/log", params=params)
        if operation == "why":
            return self._why(str(args["id"])) if args.get("id") else _error("id is required")
        if operation == "rate":
            if not args.get("id") or args.get("verdict") not in VERDICTS:
                return _error(f"id and verdict ({'|'.join(VERDICTS)}) are required")
            if not self._owner(session_id):
                return _error("only the owner can rate an intention")
            return self._mind("POST", "/v1/mind/rate", json={"id": str(args["id"]), "verdict": args["verdict"]})
        if operation in {"yes", "no"}:
            return self._answer_ask(operation, str(args.get("code") or ""), session_id)
        return _error("unknown operation")

    def _working_on(self) -> list[dict[str, Any]]:
        """The tasks the mind has in flight (approved or dispatched), with their audit ids."""
        if self.client.has_mind_routes() is not True:
            return []
        try:
            response = self.client.get("/v1/mind/log", timeout=5,
                                       params={"status": "dispatched,approved", "kind": "task", "limit": 20})
            entries = response.json().get("entries") if response.is_success else None
        except (SidecarUnavailable, ValueError):
            return []
        return [{"id": item.get("id"), "title": item.get("title"), "status": item.get("status")}
                for item in (entries or []) if isinstance(item, dict) and item.get("id")]

    def _why(self, intention_id: str) -> str:
        """``GET /v1/mind/why/{id}``; an id the audit log does not hold gets the sidecar's refusal sentence,
        so a false premise about the agent's own actions is answered from the record."""
        if self.client.has_mind_routes() is not True:
            return _error(ROUTES_MISSING)
        try:
            response = self.client.get(f"/v1/mind/why/{quote(intention_id, safe='')}", timeout=5)
        except SidecarUnavailable:
            return _error("the sidecar is unreachable")
        if response.status_code == 404:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            message = detail.get("message") if isinstance(detail, dict) else None
            return _error(str(message or f"no intention {intention_id} exists in the audit log"))
        if not response.is_success:
            return _error(f"sidecar HTTP {response.status_code}")
        return response.text

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


__all__ = ["ASK_CODE", "AUTOMATED_PLATFORMS", "FORGET_SCHEMA", "PEOPLE_SCHEMA", "SEARCH_SCHEMA", "SELF_SCHEMA",
           "Tools", "VERDICTS"]
