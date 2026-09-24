"""``protagine_opinions``: the agent's recorded opinions, and the owner's two controls over them.

``list`` and ``why`` pass the session's participant, so the sidecar returns only
what that participant may see (a guest never sees an owner-audience stance).
``withdraw`` and ``reconsider`` are refused unless the turn is the owner's own
interactive session: never a guest, a kanban worker or a cron run (architecture
4.4, 7.10). The tool is read-only for the guard; its mutations are gated here and
again by the sidecar.
"""

from __future__ import annotations

from typing import Any

from .tools import Tools, _error

OPERATIONS = ("list", "why", "withdraw", "reconsider")
OPINIONS_SCHEMA = {
    "name": "protagine_opinions",
    "description": "Your recorded opinions: list [query], why <id> (what it rests on, how it changed); owner only: "
                   "withdraw|reconsider <id> with the owner's reason.",
    "parameters": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": list(OPERATIONS)},
        "id": {"type": "integer"}, "query": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["operation"]},
}


class Opinions:
    def __init__(self, tools: Tools):
        self.tools = tools

    def handle(self, args: Any = None, *, session_id: str = "", **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        operation = str(args.get("operation") or "")
        if operation not in OPERATIONS:
            return _error("unknown operation")
        contact = self.tools.sessions.contact_id(session_id) or ""
        if operation == "list":
            params = {"contact_id": contact, "limit": 10}
            if str(args.get("query") or "").strip():
                params["q"] = str(args["query"])[:1000]
            return self.tools._mind("GET", "/v1/mind/opinions", params=params)
        identifier = args.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, (int, str)) or not str(identifier).isdigit():
            return _error("id is required (an opinion number from list)")
        if operation == "why":
            return self.tools._mind("GET", f"/v1/mind/opinions/{int(identifier)}", params={"contact_id": contact})
        if not self.tools._owner(session_id):
            return _error(f"only the owner can {operation} an opinion")
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return _error("reason is required: the owner's own words for why")
        return self.tools._mind("POST", f"/v1/mind/opinions/{int(identifier)}/{operation}", json={
            "reason": reason[:1500], "contact_id": contact or self.tools.settings.owner_contact_id() or None})


__all__ = ["OPERATIONS", "OPINIONS_SCHEMA", "Opinions"]
