"""``pre_tool_call`` rules for mind-originated and non-owner runs.

A mind-originated run is a kanban worker spawned on the ``protagine-act``
profile. A non-owner run is a session whose sender is not the owner. The
guard fails closed for effectful tools: its own errors and a silent sidecar
both block. Read-only tools never wait on the sidecar.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping

from .capture import SessionMap
from .client import ProtagineClient, Settings, SidecarUnavailable

logger = logging.getLogger(__name__)

READ_ONLY_TOOLS = frozenset({
    "read_file", "search_files", "web_search", "web_extract", "x_search", "vision_analyze",
    "session_search", "kanban_get", "kanban_list", "kanban_attachments", "skills_list", "skill_view",
    "todo_list", "tool_search", "tool_describe", "protagine_memory_search", "protagine_self",
    "protagine_people", "protagine_check_commitments", "protagine_get_affect", "protagine_get_facts",
    "protagine_get_patterns", "protagine_list_goals", "protagine_timeline",
})
MESSAGING_TOOLS = frozenset({
    "send_message", "react_to_message", "discord", "discord_admin", "yb_send_dm", "yb_send_sticker",
    "feishu_drive_reply_comment", "feishu_drive_add_comment",
})
WRITE_TOOLS = frozenset({"write_file", "patch"})
GUARD_ROUTE = "/v1/mind/guard"
GUARD_TIMEOUT = 2.0
# The V4A headers stock ``patch`` writes to (tools/file_tools.py checks the same two shapes).
V4A_HEADER = re.compile(r"^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$", re.MULTILINE)
V4A_MOVE = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$", re.MULTILINE)

# The floor (architecture 7.3): matched on tool arguments inside mind runs.
FLOOR_PATTERNS: dict[str, re.Pattern[str]] = {
    "money_movement": re.compile(
        r"\b(?:wire|transfer|send|move)\s+(?:\$|money|funds|payment)|"
        r"\b(?:purchase|buy|pay|spend|subscribe)\b.{0,30}\$|"
        r"\bpayment\s+(?:of|for)\b|\$\d{2,}", re.IGNORECASE),
    "irreversible_deletion": re.compile(
        r"\b(?:rm\s+-rf|drop\s+(?:table|database)|delete\s+permanently|"
        r"wipe|purge\s+all|force[- ]?push|erase\s+(?:all|everything))\b", re.IGNORECASE),
    "credential_change": re.compile(
        r"\b(?:rotate|change|reset|revoke|create)\b.{0,40}\b(?:credential|"
        r"password|api[_ ]?key|secret|token|ssh[- ]?key|certificate)\b|"
        r"\bsecurity\s+settings?\b", re.IGNORECASE),
    "bulk_third_party_messaging": re.compile(
        r"\b(?:bulk|mass|broadcast|blast|everyone|all\s+contacts)\b.{0,40}"
        r"\b(?:message|text|email|sms|dm)\b|"
        r"\b(?:message|text|email|sms|dm)\b.{0,40}\b(?:bulk|mass|broadcast|"
        r"blast|everyone|all\s+contacts)\b", re.IGNORECASE),
}


def block(message: str) -> dict[str, str]:
    return {"action": "block", "message": f"BLOCKED by Protagine guard: {message}"}


def ask(message: str, rule_key: str) -> dict[str, str]:
    return {"action": "approve", "message": f"Protagine floor: {message}", "rule_key": rule_key}


def mind_run(settings: Settings) -> bool:
    """A kanban worker on the mind's profile (the dispatcher sets both variables)."""
    return bool(os.environ.get("HERMES_KANBAN_TASK")) and (
        os.environ.get("HERMES_PROFILE") == settings.worker_profile)


def workspace() -> Path | None:
    value = os.environ.get("HERMES_KANBAN_WORKSPACE") or ""
    return Path(value).resolve() if value else None


def path_inside(path: Any, root: Path | None) -> bool:
    if root is None or not isinstance(path, str) or not path.strip():
        return False
    resolved = Path(os.path.expanduser(path))
    if not resolved.is_absolute():
        resolved = Path(os.environ.get("TERMINAL_CWD") or os.getcwd()) / resolved
    try:
        return resolved.resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def write_targets(tool: str, args: Mapping[str, Any]) -> list[Any]:
    """Every path a ``write_file`` or ``patch`` call writes.

    ``patch(mode="patch")`` writes the paths named in the V4A headers, with or
    without ``path``; the other modes write ``path``. An empty list means the
    call names nothing the guard can allow.
    """
    targets: list[Any] = [args["path"]] if args.get("path") else []
    if tool == "patch" and args.get("mode") == "patch":
        patch = str(args.get("patch") or "")
        targets += [match.group(1).strip() for match in V4A_HEADER.finditer(patch)]
        targets += [group.strip() for match in V4A_MOVE.finditer(patch) for group in match.groups()]
    return targets


def floor_match(text: str) -> str | None:
    return next((name for name, pattern in FLOOR_PATTERNS.items() if pattern.search(text)), None)


def parse_deliver(value: Any) -> list[tuple[str, str]]:
    """``deliver`` targets as (platform, chat_id); 'origin' and 'local' are literal."""
    if isinstance(value, (list, tuple)):
        value = ",".join(str(item) for item in value)
    targets: list[tuple[str, str]] = []
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part in {"local", "origin"}:
            targets.append((part, ""))
        elif part == "all" or part.startswith("bot-chat"):
            targets.append(("*", part))
        else:
            pieces = part.split(":")
            targets.append((pieces[0], pieces[1] if len(pieces) > 1 else ""))
    return targets


class Guard:
    def __init__(self, client: ProtagineClient, sessions: SessionMap, settings: Settings):
        self.client, self.sessions, self.settings = client, sessions, settings
        self._enabled_cache: tuple[float, bool] | None = None

    # -- hook ---------------------------------------------------------------

    def pre_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "",
                      **_: Any) -> dict[str, str] | None:
        tool = str(tool_name or "")
        try:
            return self.decide(tool, args if isinstance(args, Mapping) else {}, str(session_id or ""))
        except Exception as error:  # a failing guard blocks effects, never permits them
            logger.warning("guard error on %s (%s)", tool, type(error).__name__)
            return None if tool in READ_ONLY_TOOLS else block(f"guard error ({type(error).__name__})")

    def decide(self, tool: str, args: Mapping[str, Any], session_id: str) -> dict[str, str] | None:
        mind = mind_run(self.settings)
        guest = not mind and self.sessions.is_owner(session_id) is False
        if not mind and not guest:
            return None
        if tool in READ_ONLY_TOOLS:
            if guest and tool == "session_search":
                return block("session_search is unavailable to non-owner sessions")
            return None
        text = json.dumps(args, ensure_ascii=False, sort_keys=True)
        if mind:
            if not self.mind_enabled():
                return block("the mind is off; no effects until it is turned on")
            verdict = self._deny(tool, text)
            if verdict is not None:
                return verdict
            if tool == "kanban_create":
                if str(args.get("assignee") or "") != self.settings.worker_profile:
                    return block(f"mind tasks may only create tasks assigned to {self.settings.worker_profile}")
                if str(args.get("idempotency_key") or "").startswith("mind:"):
                    return block("mind: idempotency keys belong to the sidecar")
                # The dispatcher hands a child whatever workspace it names; a child
                # of this task gets a fresh scratch workspace or a directory inside this one.
                if args.get("project") or args.get("project_id"):
                    return block("child tasks inherit the task's project")
                if (str(args.get("workspace_kind") or "scratch") != "scratch" or args.get("workspace_path")) \
                        and not path_inside(args.get("workspace_path"), workspace()):
                    return block("child task workspaces must stay inside the task workspace")
            if tool in WRITE_TOOLS:
                targets = write_targets(tool, args)
                if not targets or not all(path_inside(target, workspace()) for target in targets):
                    return block("writes must stay inside the task workspace")
            matched = floor_match(text)
            if matched:
                return ask(matched.replace("_", " "), f"protagine.floor.{matched}")
        cron_action = str(args.get("action") or "").strip().lower()  # what stock cronjob() runs
        if tool in MESSAGING_TOOLS or (tool == "cronjob_manage" and cron_action in {"create", "update"}):
            verdict = self._outbound(tool, args, session_id, cron_action)
            if verdict is not None:
                return verdict
        return self._sidecar_verdict(tool, args, session_id, "mind" if mind else "guest")

    # -- rules ----------------------------------------------------------------

    def _deny(self, tool: str, text: str) -> dict[str, str] | None:
        deny = self.settings.mind().get("deny")
        deny = deny if isinstance(deny, Mapping) else {}
        if tool in {str(name) for name in (deny.get("tools") or [])}:
            return block(f"{tool} is on the deny list")
        for pattern in deny.get("text") or []:
            try:
                if re.search(str(pattern), text, re.IGNORECASE):
                    return block("the arguments match a denied text pattern")
            except re.error:
                logger.warning("ignoring invalid deny pattern %r", pattern)
        return None

    def mind_enabled(self) -> bool:
        if self.settings.mind().get("enabled") is False:
            return False
        cached = self._enabled_cache
        if cached is not None and time.monotonic() - cached[0] < 30:
            return cached[1]
        enabled = True
        try:
            if self.client.has_mind_routes():
                response = self.client.get("/v1/mind/status", timeout=GUARD_TIMEOUT)
                if response.is_success and response.json().get("enabled") is False:
                    enabled = False
        except (SidecarUnavailable, ValueError):
            pass  # unreachable: the sidecar verdict below blocks effects anyway
        self._enabled_cache = (time.monotonic(), enabled)
        return enabled

    def _outbound(self, tool: str, args: Mapping[str, Any], session_id: str,
                  cron_action: str = "") -> dict[str, str] | None:
        """Messaging tools and delivering cron jobs need a permitted recipient."""
        if tool in MESSAGING_TOOLS:
            if self.client.has_mind_routes():
                return None  # the sidecar verdict decides with may_contact and budgets
            return block(f"{tool} needs the sidecar's contact permissions, which this version lacks")
        # The job delivers to ``deliver`` and, on failure, ``failure_deliver``; an update
        # keeps whatever the stored job has for the fields it does not supply.
        stored = self._stored_job(args.get("job_id")) if cron_action == "update" else {}
        fields = {"deliver": [("origin", "")] if cron_action == "create" else [], "failure_deliver": []}
        targets: list[tuple[str, str]] = []
        for field, default in fields.items():
            if field in args:
                targets += parse_deliver(args.get(field))
            elif field in stored:
                targets += parse_deliver(stored.get(field))
            else:
                targets += default
        for platform, chat_id in targets:
            if platform == "local":
                continue
            if platform == "*":
                return block("cron delivery to every channel is not allowed in this run")
            if platform == "origin":
                contact = self._session_contact(session_id)
            else:
                contact = self.client.resolve_contact(platform, chat_id, timeout=GUARD_TIMEOUT)
            if not contact:
                return block("cron delivery to an unknown recipient")
            if contact.get("interaction_allowed") is False:
                return block("cron delivery to a contact who may not be contacted")
        return None

    @staticmethod
    def _stored_job(job_id: Any) -> dict[str, Any]:
        """The stock cron record behind ``job_id`` (id or name), or {} when there is none."""
        if not job_id:
            return {}
        try:
            from cron import jobs
            job = jobs.resolve_job_ref(str(job_id))
        except Exception:  # outside Hermes, or an unreadable store: nothing to merge
            return {}
        return dict(job) if isinstance(job, Mapping) else {}

    def _session_contact(self, session_id: str) -> dict[str, Any] | None:
        if self.sessions.is_owner(session_id):
            return {"contact_id": self.settings.owner_contact_id() or "owner", "interaction_allowed": True}
        info = self.sessions.get(session_id)
        if info is None or not info.sender_id:
            return None
        return self.client.resolve_contact(info.platform, info.sender_id, timeout=GUARD_TIMEOUT)

    def _sidecar_verdict(self, tool: str, args: Mapping[str, Any], session_id: str,
                         run: str) -> dict[str, str] | None:
        """``POST /v1/mind/guard`` with a 2 s timeout; silence blocks, 404 allows.

        Every effect asks the sidecar itself: a cached "no mind routes" answer
        says nothing about whether the sidecar is still there.
        """
        response = self.client.post(GUARD_ROUTE, timeout=GUARD_TIMEOUT, json={
            "tool": tool, "args": dict(args), "session_id": session_id, "run": run,
            "task_id": os.environ.get("HERMES_KANBAN_TASK") or ""})
        if response.status_code == 404:
            return None
        if not response.is_success:
            return block(f"the sidecar refused the guard check (HTTP {response.status_code})")
        verdict = response.json()
        action, message = verdict.get("action"), str(verdict.get("message") or "")
        if action == "block":
            return block(message or f"{tool} was refused by the mind")
        if action == "ask":
            return ask(message or f"{tool} needs the owner's approval", f"protagine.ask.{tool}")
        return None


__all__ = ["FLOOR_PATTERNS", "Guard", "MESSAGING_TOOLS", "READ_ONLY_TOOLS", "WRITE_TOOLS", "ask",
           "block", "floor_match", "mind_run", "parse_deliver", "path_inside", "workspace", "write_targets"]
