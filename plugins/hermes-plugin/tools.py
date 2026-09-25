"""Model tools: ``protagine_self``, ``protagine_people`` and the memory tools.

Tool handlers receive ``session_id``; the session map turns it into a sender.
Reads of the mind and the contact list are the owner's and the owner's own
senderless lanes', never a guest's. Mutations are accepted only from the
owner's own interactive session: never
from a kanban worker, and never from a cron run, whose prompt is a stored job
anyone with the tool could have scheduled. ``yes``/``no`` also need the typed
ask code inside the owner's own message for that turn, so neither a guest, a
worker, a cron job nor an injected page can approve anything (architecture
7.7, 7.10). The mind's record (what it works on, its log, why it acted, the
self-narrative) is the owner's too: shown only in the owner's own session,
never to a guest, a group the owner shares or a worker. A call that cannot succeed this turn (a refusal,
an id or code nobody listed, nothing retained, an unreachable sidecar) gets ``final_answer``, never an
error the model would try again with other arguments; an argument it can correct names the valid form.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable
from urllib.parse import quote

from .body import mind_state
from .capture import SessionMap, session_env
from .client import ProtagineClient, Settings, SidecarUnavailable, final_answer
from .commands import ROUTES_MISSING

RECORD_IS_OWNERS = "the mind's record is the owner's and is shown only in the owner's own chat"
# What a read of the record takes. Anything else it is given (content, text, a reason) is an attempt to write
# through it; the record is written after the turn, from what the turn said, never by a tool call.
READ_KEYS = frozenset({"operation", "id", "limit", "since_hours", "kind", "recipient", "query"})
READS_ONLY = "protagine_self only reads the record; what this turn says is recorded after it, with no tool call"

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
# M7's opinions ride this tool (integration map X8): opinions [query], why <opinion number>, and the owner's
# withdraw|reconsider <number> with a reason; the sidecar filters every read by the session's participant.
SELF_OPERATIONS = ("state", "log", "why", "rate", "yes", "no", "opinions", "withdraw", "reconsider")
SELF_SCHEMA = {
    "name": "protagine_self",
    "description": "Your mind and record, the only source for what you did or decided: state (level, asks with "
                   "codes, working_on, narrative), log (read-only, newest first; since_hours, kind, recipient; cite "
                   "ids; an action of yours not in it did not happen), why <id or opinion number>, rate <id> "
                   "<verdict>, opinions [query]; owner only: yes|no <code> (typed by the owner), withdraw|reconsider "
                   "<opinion> <reason>. What a turn says is recorded after it, with no tool call.",
    "parameters": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": list(SELF_OPERATIONS)},
        "id": {"type": "string"}, "verdict": {"type": "string", "enum": list(VERDICTS)},
        "code": {"type": "string"}, "limit": {"type": "integer"}, "since_hours": {"type": "number"},
        "kind": {"type": "string"}, "recipient": {"type": "string"}, "query": {"type": "string"},
        "reason": {"type": "string"}},
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


# A forget answers in about a second on a long history (the closure is indexed; the
# vector tables are compacted routinely). Past this bound the tool says the removal
# is unconfirmed, never that it failed: the sidecar finishes a forget it received.
FORGET_TIMEOUT_SECONDS = 5.0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _error(message: str) -> str:
    return _json({"error": message})


def _opinion(value: Any) -> int | None:
    """An opinion number (an intention id is a UUID, never all digits)."""
    text = "" if isinstance(value, bool) or not isinstance(value, (int, str)) else str(value).strip()
    return int(text) if text.isdigit() else None


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

    def _owners_own(self, session_id: str) -> bool:
        """The owner's session with no one else in it (a direct chat, the CLI): the mind's record (what it
        works on, whom it messaged, what it knows about itself) is shown there only, never to a guest, in a
        group the owner shares, or to a worker."""
        return self._owner(session_id) and session_env()[3].lower() in {"", "dm"}

    def _mind(self, method: str, path: str, **kwargs: Any) -> str:
        if self.client.has_mind_routes() is not True:
            return final_answer(ROUTES_MISSING)
        try:
            response = self.client.request(method, path, timeout=5, **kwargs)
        except SidecarUnavailable:
            return final_answer("the sidecar is unreachable")
        if response.status_code == 404:
            return final_answer(ROUTES_MISSING if not response.text else f"not found: {response.text[:200]}")
        if not response.is_success:
            return final_answer(f"sidecar HTTP {response.status_code}")
        return response.text

    # -- protagine_self ---------------------------------------------------------------

    def self_tool(self, args: Any = None, *, session_id: str = "", **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        operation = str(args.get("operation") or "")
        if operation not in (*SELF_OPERATIONS, "status"):
            return final_answer(f"unknown operation (one of {', '.join(SELF_OPERATIONS)}); {READS_ONLY}")
        if operation in {"state", "status", "log", "why", "opinions"} and any(
                value not in (None, "", [], {}) for key, value in args.items() if key not in READ_KEYS):
            return final_answer(READS_ONLY)
        if operation in {"state", "status"}:
            detail = mind_state(self.client) or {}
            mind = self.settings.mind()
            switches = {"enabled": mind.get("enabled", True) is not False and detail.get("enabled") is not False,
                        "autonomy": detail.get("autonomy") or mind.get("autonomy", "standard"),
                        "sidecar_reachable": self.client.health() is not None}
            if not self._owners_own(session_id):
                # The record is the owner's, the agent's feelings included (they quote the owner's
                # obligations and reports): any other session gets the switches only.
                return _json(switches)
            narrative = self.client.narrative() or {}
            return _json({"mind_routes": self.client.has_mind_routes() is True, **detail, **switches,
                          # From the record, never free generation: the tasks in flight and the narrative.
                          "working_on": self._working_on(),
                          "narrative": str(narrative.get("text") or "") if narrative.get("enabled") is True else ""})
        if operation in {"opinions", "withdraw", "reconsider"} or (operation == "why" and _opinion(args.get("id"))):
            return self._opinions(operation, args, session_id)
        if operation in {"log", "why"} and not self._owners_own(session_id):
            return final_answer(RECORD_IS_OWNERS)
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
                return final_answer("only the owner can rate an intention")
            return self._mind("POST", "/v1/mind/rate", json={"id": str(args["id"]), "verdict": args["verdict"]})
        return self._answer_ask(operation, str(args.get("code") or ""), session_id)

    def _opinions(self, operation: str, args: dict[str, Any], session_id: str) -> str:
        """The agent's recorded opinions. Reads pass the session's participant, so the sidecar shows a guest,
        a worker or a cron run only the views meant for them; withdraw and reconsider need the owner's own
        interactive session and the owner's reason (the sidecar checks again)."""
        viewer, number = self.sessions.contact_id(session_id) or "", _opinion(args.get("id"))
        if operation == "opinions":
            return self._mind("GET", "/v1/mind/opinions", params={
                "contact_id": viewer, "limit": 10, "q": str(args.get("query") or "").strip()[:1000]})
        if operation == "why":
            return self._mind("GET", f"/v1/mind/opinions/{number}", params={"contact_id": viewer})
        if number is None:
            return _error("id is required (an opinion number from opinions)")
        if not self._owner(session_id):
            return final_answer(f"only the owner can {operation} an opinion")
        if not str(args.get("reason") or "").strip():
            return _error("reason is required: the owner's own words for why")
        return self._mind("POST", f"/v1/mind/opinions/{number}/{operation}", json={
            "reason": str(args["reason"]).strip()[:1500],
            "contact_id": viewer or self.settings.owner_contact_id() or None})

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
            return final_answer(ROUTES_MISSING)
        try:
            response = self.client.get(f"/v1/mind/why/{quote(intention_id, safe='')}", timeout=5)
        except SidecarUnavailable:
            return final_answer("the sidecar is unreachable")
        if response.status_code == 404:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            message = detail.get("message") if isinstance(detail, dict) else None
            return final_answer(str(message or f"no intention {intention_id} exists in the audit log"))
        if not response.is_success:
            return final_answer(f"sidecar HTTP {response.status_code}")
        return response.text

    def _answer_ask(self, answer: str, code: str, session_id: str) -> str:
        """``POST /v1/mind/decide`` only for the owner's own turn whose message carries the typed code. A code
        the owner did not type is never a guess away: the answer names an open ask the message does carry, or
        is final (no ask open, or none typed)."""
        code, info = code.strip().upper(), self.sessions.get(session_id)
        if not self._owner(session_id) or info is None:
            return final_answer("only the owner answers an ask, in their own message")
        state, message = mind_state(self.client), info.user_message.upper()
        open_codes = [str(ask.get("code")).upper() for ask in (state or {}).get("asks") or [] if ask.get("code")]
        if state is not None and not open_codes:
            return final_answer("no ask is open; nothing to answer")

        def typed(value: str) -> bool:
            return bool(re.search(rf"(?<![A-Z0-9]){re.escape(value)}(?![A-Z0-9])", message))
        if not ASK_CODE.fullmatch(code) or not typed(code):
            answered = [value for value in open_codes if typed(value)]
            return _error(f"the owner's message answers ask {' or '.join(answered)}; pass that code") if answered \
                else final_answer("an ask is answered only with its code as the owner typed it in their own "
                                  "message, and this one has none")
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
            return final_answer("only the owner can change who may be contacted, cadences or merges")
        if operation != "who" and not who:
            return _error("contact_id (a name, handle or id) is required")
        if operation == "who" and not who and self.sessions.is_owner(session_id) is not True:
            # Listing everyone is the owner's (and the owner's senderless lanes'); a guest names one person.
            return _error("only the owner can list contacts; name who you mean")
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
            return final_answer(ROUTES_MISSING)
        try:
            response = self.client.request(method, path, timeout=kwargs.pop("timeout", 5), **kwargs)
        except SidecarUnavailable:
            return final_answer("the sidecar is unreachable")
        try:
            value = response.json()
        except ValueError:
            value = {}
        if not response.is_success:
            detail = value.get("detail") if isinstance(value, dict) else None
            return final_answer(str(detail.get("message") or detail.get("code")) if isinstance(detail, dict) else
                                ROUTES_MISSING if response.status_code == 404 else
                                f"sidecar HTTP {response.status_code}")
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
            return final_answer("this turn has no resolved participant; answer from the message")
        try:
            response = self.client.post("/v1/host/memory/search", timeout=10, json={
                "identity": {"host_id": "hermes"}, "person_id": contact, "session_id": session_id,
                "query": query[:4096], "limit": max(1, min(int(args.get("limit") or 5), 20))})
        except SidecarUnavailable:
            return final_answer("memory search is unavailable")
        if not response.is_success:
            return final_answer(f"memory search failed (HTTP {response.status_code})")
        value = response.json()
        retrieval = value.get("retrieval") if isinstance(value.get("retrieval"), dict) else {}
        degraded = "failed" in retrieval.values()   # e.g. semantic recall down: only exact words were matched
        if not value.get("count") and not degraded:
            return final_answer("nothing retained matches; for a participant with no history there is nothing to "
                                "find, so answer from the message and the recalled context")
        found = {"content": value.get("content", ""), "count": value.get("count", 0),
                 "source_refs": value.get("source_refs", [])}
        return _json({**found, "note": "part of the search failed, so only exact words were matched; other words "
                                       "may find more"} if degraded else found)

    def memory_forget(self, args: Any = None, *, session_id: str = "", **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        ids = [str(item) for item in (args.get("source_ids") or []) if str(item).strip()]
        if not ids:
            return _error("source_ids is required")
        if not self._owner(session_id):
            return final_answer("only the owner can forget sources")
        contact = self.sessions.contact_id(session_id)
        try:
            response = self.client.post("/v1/host/memory/sources/forget", timeout=FORGET_TIMEOUT_SECONDS,
                                        json={"contact_id": contact, "source_ids": list(dict.fromkeys(ids))})
        except SidecarUnavailable as error:
            if not error.delivered:
                return final_answer("nothing was removed: the sidecar is unreachable")
            return _json({"source_erased": None, "status": "unconfirmed",
                          "note": f"The removal was sent and may have completed, but its answer did not arrive "
                                  f"within {FORGET_TIMEOUT_SECONDS:g} s. Forgetting the same source_ids again "
                                  "is safe and reports the result."})
        if response.status_code in {409, 422}:
            return _json({"source_erased": False, "error": "no matching sources; use exact source_id values"})
        if not response.is_success:
            return final_answer(f"forget failed (HTTP {response.status_code})")
        return response.text


__all__ = ["ASK_CODE", "AUTOMATED_PLATFORMS", "FORGET_SCHEMA", "FORGET_TIMEOUT_SECONDS", "PEOPLE_OPERATIONS", "PEOPLE_SCHEMA", "SEARCH_SCHEMA",
           "SELF_SCHEMA", "Tools", "VERDICTS"]
