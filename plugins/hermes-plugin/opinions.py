"""``protagine_opinions``: the agent's recorded opinions, and the owner's two controls over them.

Reads pass the session's participant, so a guest never sees an owner-audience stance.
``withdraw`` and ``reconsider`` need the owner's own interactive session (never a guest, a
kanban worker or a cron run); the tool is read-only for the guard, and the sidecar checks again.
"""

from __future__ import annotations

from typing import Any

from .tools import Tools, _error

OPERATIONS = ("list", "why", "withdraw", "reconsider")
OPINIONS_SCHEMA = {"name": "protagine_opinions", "description": "Your recorded opinions: list [query], why <id> (what "
                   "it rests on, how it changed); owner only: withdraw|reconsider <id> with the owner's reason.",
                   "parameters": {"type": "object", "required": ["operation"], "properties": {
                       "operation": {"type": "string", "enum": list(OPERATIONS)}, "id": {"type": "integer"},
                       "query": {"type": "string"}, "reason": {"type": "string"}}}}


class Opinions:
    def __init__(self, tools: Tools):
        self.tools = tools

    def handle(self, args: Any = None, *, session_id: str = "", **_: Any) -> str:
        args, mind = args if isinstance(args, dict) else {}, self.tools._mind
        operation, contact = str(args.get("operation") or ""), self.tools.sessions.contact_id(session_id) or ""
        identifier, query = args.get("id"), str(args.get("query") or "").strip()[:1000]
        if operation not in OPERATIONS:
            return _error("unknown operation")
        if operation == "list":
            return mind("GET", "/v1/mind/opinions", params={"contact_id": contact, "limit": 10, "q": query})
        if isinstance(identifier, bool) or not isinstance(identifier, (int, str)) or not str(identifier).isdigit():
            return _error("id is required (an opinion number from list)")
        if operation == "why":
            return mind("GET", f"/v1/mind/opinions/{int(identifier)}", params={"contact_id": contact})
        if not self.tools._owner(session_id):
            return _error(f"only the owner can {operation} an opinion")
        if not str(args.get("reason") or "").strip():
            return _error("reason is required: the owner's own words for why")
        owner = contact or self.tools.settings.owner_contact_id() or None
        return mind("POST", f"/v1/mind/opinions/{int(identifier)}/{operation}",
                    json={"reason": str(args["reason"]).strip()[:1500], "contact_id": owner})
