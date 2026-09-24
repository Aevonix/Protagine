"""Turn and sender capture: the session map and the durable turn outbox.

Hook callbacks only enqueue. ``pre_llm_call`` records who is speaking in each
session (the guard and the tools read that map); ``post_llm_call`` writes one
row to a private SQLite outbox. The body thread delivers rows to the sidecar.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping
import uuid

from .client import ProtagineClient, Settings, SidecarUnavailable

logger = logging.getLogger(__name__)

# Turns without a sender on these platforms belong to the owner's own terminal.
INTERNAL_PLATFORMS = frozenset({"", "cli", "internal", "system", "owner", "api", "worker", "cron"})
# The skills the sidecar writes into its own skills.external_dirs entry (P/mind/skills.py).
SKILL_PREFIX = "protagine-"


def session_env() -> tuple[str, str, str, str]:
    """The platform, sender, chat and chat type the gateway bound for the current turn, or blanks."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return "", "", "", ""
    return tuple(str(get_session_env(name, "") or "").strip() for name in (  # type: ignore[return-value]
        "HERMES_SESSION_PLATFORM", "HERMES_SESSION_USER_ID", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_CHAT_TYPE"))


def text_of(content: Any) -> str:
    """Plain text of a message content value (string or content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(block.get("text") or "") for block in content
                 if isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"}]
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


@dataclass
class SessionInfo:
    session_id: str
    platform: str = ""
    sender_id: str = ""
    user_message: str = ""
    parent_session_id: str = ""
    seen_at: float = field(default_factory=time.time)
    owner: bool | None = None
    contact_id: str | None = None


class SessionMap:
    """``session_id -> (sender, platform, last user message)``, bounded."""

    def __init__(self, settings: Settings, client: ProtagineClient, *, limit: int = 2048):
        self.settings, self.client, self.limit = settings, client, limit
        self._sessions: OrderedDict[str, SessionInfo] = OrderedDict()
        self._lock = threading.Lock()

    def observe(self, *, session_id: str = "", platform: str = "", sender_id: str = "",
                user_message: Any = None, parent_session_id: str = "", **_: Any) -> SessionInfo | None:
        session_id = str(session_id or "")
        if not session_id:
            return None
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None or info.sender_id != str(sender_id or "") or info.platform != str(platform or ""):
                info = SessionInfo(session_id, str(platform or ""), str(sender_id or ""))
            info.user_message = text_of(user_message)
            info.parent_session_id = str(parent_session_id or "")
            info.seen_at = time.time()
            self._sessions[session_id] = info
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > self.limit:
                self._sessions.popitem(last=False)
        return info

    def get(self, session_id: str) -> SessionInfo | None:
        with self._lock:
            return self._sessions.get(str(session_id or ""))

    def _owner_handles(self, platform: str) -> set[str]:
        return {item.lower() for item in self.settings.owner_handles().get(platform.lower(), [])}

    def is_owner(self, session_id: str) -> bool | None:
        """True for the owner, False for anyone else, None for an unknown session."""
        info = self.get(session_id)
        if info is None:
            return None
        if info.owner is not None:
            return info.owner
        if not info.sender_id and info.platform.lower() in INTERNAL_PLATFORMS:
            info.owner = True
            return True
        if info.sender_id.strip().lower() in self._owner_handles(info.platform):
            info.owner = True
            return True
        owner_id = self.settings.owner_contact_id()
        contact = self.contact_id(session_id)
        if contact is None:
            return False  # unresolved senders never gain owner authority
        info.owner = bool(owner_id) and contact == owner_id
        return info.owner

    def sender_is_owner(self, platform: str, sender_id: str) -> bool:
        """One of the owner's handles, or a handle the sidecar resolves to the owner's contact. Nothing is
        recorded and no contact is created; an unresolvable sender is not the owner."""
        platform, sender = str(platform or "").strip().lower(), str(sender_id or "").strip()
        if not sender:
            return False
        if sender.lower() in self._owner_handles(platform):
            return True
        owner_id = self.settings.owner_contact_id()
        if not owner_id:
            return False
        try:
            contact = self.client.resolve_contact(platform, sender)
        except Exception as error:
            logger.debug("sender not resolved (%s)", type(error).__name__)
            return False
        return bool(contact) and str(contact.get("contact_id")) == owner_id

    def owner_only(self, session_info: Mapping[str, Any]) -> bool:
        """Whether the session whose prompt Hermes is rendering is the owner's alone.

        Hermes renders a session's prompt before its first ``pre_llm_call``, so the
        sender comes from the gateway's session variables, as the memory provider
        reads it. The owner's own direct chat counts, and so does an internal lane
        with no chat at all (the CLI, the benchmark). A guest, an unresolved
        sender, a group or channel the owner shares, and a chat with no sender
        never do. This map is only read here: a render in mid-turn must not reset
        the message the tools check an ask code against.
        """
        platform, sender, chat, chat_type = session_env()
        platform = (platform or str(session_info.get("platform") or "")).strip().lower()
        if chat_type.lower() not in {"", "dm"}:
            return False
        if not sender:
            return not chat and platform in INTERNAL_PLATFORMS
        known = self.get(str(session_info.get("session_id") or ""))
        if known is not None and known.sender_id == sender and known.owner is not None:
            return known.owner
        return self.sender_is_owner(platform, sender)

    def contact_id(self, session_id: str) -> str | None:
        """The sidecar contact for a session's sender; the owner for internal turns."""
        info = self.get(session_id)
        if info is None:
            return None
        if info.contact_id:
            return info.contact_id
        if not info.sender_id:
            if info.platform.lower() in INTERNAL_PLATFORMS:
                info.contact_id = self.settings.owner_contact_id() or "owner"
                return info.contact_id
            return None
        try:
            contact = self.client.resolve_contact(info.platform, info.sender_id, create=True)
        except (SidecarUnavailable, Exception) as error:
            logger.debug("contact resolution deferred: %s", type(error).__name__)
            return None
        if contact:
            info.contact_id = str(contact["contact_id"])
        return info.contact_id


# A hook writes one row before the reply returns, while the body thread reads the same file.
# Five seconds of busy waiting was not enough under a burst of concurrent turns.
BUSY_TIMEOUT_SECONDS = 30
ENQUEUE_ATTEMPTS = 4
ENQUEUE_RETRY_SECONDS = 0.05


class TurnOutbox:
    """Durable SQLite ledger of captured turns shared across Hermes processes.

    The row commits with ``synchronous=FULL`` before the hook returns. Delivery
    claims rows under a lease so several processes can drain the same file.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if not self._ready:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not self.path.exists():
                    self.path.touch(mode=0o600)
                os.chmod(self.path, 0o600)
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # WAL keeps the body thread's reads from blocking a hook's write: with the rollback
        # journal, a few dozen turns arriving at once made writers wait on readers until the
        # busy timeout expired and the turn was dropped. A filesystem without shared memory
        # refuses the mode and keeps the old one, which still works.
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        connection.execute("PRAGMA synchronous=FULL")
        with self._lock:
            if not self._ready:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS turn_outbox (
                        turn_id TEXT PRIMARY KEY, envelope_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('pending', 'delivered')),
                        attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                        updated_at REAL NOT NULL, last_error TEXT NOT NULL DEFAULT '',
                        lease_id TEXT NOT NULL DEFAULT '', lease_expires_at REAL NOT NULL DEFAULT 0)
                """)
                self._ready = True
        return connection

    def enqueue(self, turn_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Commit one turn; a repeated turn id keeps the first row."""
        turn_id = str(turn_id or "").strip()
        if not turn_id:
            raise ValueError("turn_id is required")
        encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if len(encoded) > 8 * 1024 * 1024:
            raise ValueError("turn payload exceeds 8 MiB")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        now = time.time()
        for attempt in range(ENQUEUE_ATTEMPTS):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT state FROM turn_outbox WHERE turn_id = ?", (turn_id,)).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO turn_outbox (turn_id, envelope_sha256, payload_json, state, attempts,"
                        " created_at, updated_at) VALUES (?, ?, ?, 'pending', 0, ?, ?)",
                        (turn_id, digest, encoded, now, now))
                connection.execute("COMMIT")
                return {"turn_id": turn_id, "state": row["state"] if row else "pending", "new": row is None}
            except sqlite3.OperationalError as error:
                # A busy database is the one failure worth retrying here: losing the row loses
                # the turn, and with it anything the person committed to in it.
                if "locked" not in str(error) and "busy" not in str(error):
                    raise
                if attempt == ENQUEUE_ATTEMPTS - 1:
                    raise
                time.sleep(ENQUEUE_RETRY_SECONDS * (attempt + 1))
            finally:
                connection.close()

    def pending_count(self) -> int:
        connection = self._connect()
        try:
            return int(connection.execute("SELECT COUNT(*) FROM turn_outbox WHERE state = 'pending'").fetchone()[0])
        finally:
            connection.close()

    def rows(self, state: str | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            query = "SELECT turn_id, payload_json, state, attempts, last_error FROM turn_outbox"
            rows = connection.execute(query + (" WHERE state = ?" if state else "") + " ORDER BY created_at",
                                      (state,) if state else ()).fetchall()
            return [{"turn_id": r["turn_id"], "payload": json.loads(r["payload_json"]), "state": r["state"],
                     "attempts": int(r["attempts"]), "last_error": r["last_error"]} for r in rows]
        finally:
            connection.close()

    def drain(self, deliver: Callable[[dict[str, Any]], bool], *, limit: int = 32,
              lease_seconds: float = 60.0) -> int:
        """Deliver up to ``limit`` pending rows; returns how many were delivered.

        ``deliver`` returns True when the sidecar accepted the row and False to
        retry later; an :class:`Undeliverable` marks the row delivered with its
        error recorded, so a rejected payload never blocks the queue.
        """
        delivered, lease = 0, uuid.uuid4().hex
        for _ in range(max(1, limit)):
            now = time.time()
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT turn_id, payload_json, attempts FROM turn_outbox WHERE state = 'pending'"
                    " AND lease_expires_at < ? ORDER BY created_at LIMIT 1", (now,)).fetchone()
                if row is not None:
                    connection.execute("UPDATE turn_outbox SET lease_id = ?, lease_expires_at = ? WHERE turn_id = ?",
                                       (lease, now + lease_seconds, row["turn_id"]))
                connection.execute("COMMIT")
            finally:
                connection.close()
            if row is None:
                break
            payload, error, done = json.loads(row["payload_json"]), "", False
            try:
                done = bool(deliver(payload))
            except Undeliverable as failure:
                done, error = True, f"rejected: {failure}"
            except Exception as failure:
                error = type(failure).__name__
            attempts = int(row["attempts"]) + 1
            backoff = min(300.0, 2.0 ** min(attempts, 8))
            connection = self._connect()
            try:
                if done:
                    connection.execute("UPDATE turn_outbox SET state = 'delivered', attempts = ?, updated_at = ?,"
                                       " last_error = ?, lease_id = '', lease_expires_at = 0 WHERE turn_id = ?",
                                       (attempts, time.time(), error, row["turn_id"]))
                    delivered += 1
                else:
                    connection.execute("UPDATE turn_outbox SET attempts = ?, updated_at = ?, last_error = ?,"
                                       " lease_id = '', lease_expires_at = ? WHERE turn_id = ?",
                                       (attempts, time.time(), error, time.time() + backoff, row["turn_id"]))
                    if not error or error.startswith("SidecarUnavailable"):
                        break  # the sidecar is down: stop the pass, keep the rest for later
            finally:
                connection.close()
        return delivered

    def prune(self, keep_delivered: int = 4096) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM turn_outbox WHERE state = 'delivered' AND turn_id NOT IN (SELECT turn_id"
                " FROM turn_outbox WHERE state = 'delivered' ORDER BY updated_at DESC LIMIT ?)", (keep_delivered,))
        finally:
            connection.close()


class Undeliverable(Exception):
    """The sidecar rejected the payload; retrying cannot help."""


class Capture:
    """Enqueue-only lifecycle callbacks."""

    def __init__(self, sessions: SessionMap, outbox: TurnOutbox, settings: Settings,
                 *, on_enqueue: Callable[[], Any] | None = None):
        self.sessions, self.outbox, self.settings = sessions, outbox, settings
        self.on_enqueue = on_enqueue

    def pre_llm_call(self, **kwargs: Any) -> None:
        self.sessions.observe(**kwargs)
        return None

    def post_llm_call(self, **kwargs: Any) -> None:
        try:
            receipt = self._capture(**kwargs)
        except Exception as error:  # capture must never disturb the reply
            logger.warning("turn capture failed for %s (%s: %s)",
                           kwargs.get("turn_id") or "?", type(error).__name__, error)
            return None
        if receipt and receipt.get("new") and self.on_enqueue is not None:
            try:
                self.on_enqueue()  # wake the body thread; delivery stays off the hook
            except Exception as error:
                logger.debug("body wake failed (%s)", type(error).__name__)
        return None

    def skill_loaded(self, *, action: str = "", skill_name: str = "", session_id: str = "", task_id: str = "",
                     **_: Any) -> None:
        """``on_skill_lifecycle``: a load of one of Protagine's own skills (``protagine-*``) is queued for
        ``POST /v1/mind/skills/used``; every other skill and action is Hermes' business. Enqueue only."""
        name = str(skill_name or "")
        if action != "loaded" or not name.startswith(SKILL_PREFIX):
            return None
        payload = {"kind": "skill_use", "skill": name, "session_id": str(session_id or ""),
                   "task_id": str(task_id or "")}
        try:  # a load is never disturbed by its bookkeeping
            self.outbox.enqueue(f"skill:{uuid.uuid4().hex}", payload)
            if self.on_enqueue is not None:
                self.on_enqueue()
        except Exception as error:
            logger.debug("skill load not queued (%s)", type(error).__name__)
        return None

    def _capture(self, *, session_id: str = "", task_id: str = "", turn_id: str = "",
                 user_message: Any = None, assistant_response: Any = None, model: str = "",
                 platform: str = "", **_: Any) -> dict[str, Any] | None:
        session_id = str(session_id or "")
        if not session_id or os.environ.get("HERMES_KANBAN_TASK"):
            return None  # worker runs are task work, reconciled through kanban, not conversation
        info = self.sessions.get(session_id)
        if info is not None and info.parent_session_id:
            return None  # delegated child turns are the parent's work
        user, assistant = text_of(user_message), text_of(assistant_response)
        if not user.strip() and not assistant.strip():
            return None
        stable = str(turn_id or "").strip() or "hermes:" + hashlib.sha256(json.dumps(
            [session_id, task_id, user, assistant], ensure_ascii=True).encode()).hexdigest()
        payload = {
            "session_id": session_id, "turn_id": stable, "task_id": str(task_id or ""),
            "platform": str(platform or (info.platform if info else "") or ""),
            "sender_id": info.sender_id if info else "", "user_message": user,
            "assistant_message": assistant, "model": str(model or ""),
            "occurred_at": datetime.fromtimestamp(time.time(), timezone.utc).isoformat(),
        }
        return self.outbox.enqueue(stable, payload)


def checkpoint(messages: list[dict[str, Any]], *, session_id: str, contact_id: str,
               outbox: TurnOutbox) -> dict[str, Any]:
    """Enqueue direct user/assistant content before Hermes compresses it away."""
    evidence = [{"role": m["role"], "content": m["content"]} for m in messages
                if isinstance(m, dict) and m.get("role") in {"user", "assistant"}
                and not m.get("_compressed_summary") and text_of(m.get("content")).strip()]
    if not evidence:
        return {"state": "empty", "messages": 0}
    if not session_id or not contact_id:
        raise ValueError("checkpoint requires a bound session and participant")
    payload = {"session_id": session_id, "contact_id": contact_id, "checkpoint_messages": evidence}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    payload["turn_id"] = "checkpoint:" + digest
    receipt = outbox.enqueue(payload["turn_id"], payload)
    return {"state": receipt["state"], "messages": len(evidence), "turn_id": payload["turn_id"]}


def deliver(client: ProtagineClient, sessions: SessionMap, settings: Settings,
            payload: Mapping[str, Any]) -> bool:
    """POST one outbox row to ``/v1/host/turns/sync`` (a skill load to ``/v1/mind/skills/used``); True when
    accepted."""
    if payload.get("kind") == "skill_use":
        response = client.post("/v1/mind/skills/used", timeout=5, json={
            "skill": payload.get("skill"), "session_id": payload.get("session_id") or None,
            "task_id": payload.get("task_id") or None})
        if response.status_code in {400, 404, 409, 413, 422}:
            raise Undeliverable(f"HTTP {response.status_code}")
        return response.is_success
    contact = str(payload.get("contact_id") or "")
    platform, sender = str(payload.get("platform") or ""), str(payload.get("sender_id") or "")
    if not contact:
        if sender:
            found = client.resolve_contact(platform, sender, create=True)
            if not found:
                raise Undeliverable("sender has no contact")
            contact = str(found["contact_id"])
        else:
            contact = settings.owner_contact_id() or "owner"
    body: dict[str, Any] = {
        "identity": {"host_id": "hermes"},
        "context": {"session_id": payload["session_id"], "contact_id": contact,
                    "turn_id": payload.get("turn_id") or None,
                    "channel_id": f"{platform}:{sender}" if platform and sender else None,
                    "metadata": {"occurred_at": payload["occurred_at"]} if payload.get("occurred_at") else None},
    }
    if payload.get("checkpoint_messages") is not None:
        body["checkpoint_messages"] = list(payload["checkpoint_messages"])
    else:
        if sender:
            body["sender"] = {"platform": platform or "unknown", "user_id": sender}
        if payload.get("user_message"):
            body["user_message"] = {"role": "user", "content": payload["user_message"]}
        if payload.get("assistant_message"):
            body["assistant_message"] = {"role": "assistant", "content": payload["assistant_message"]}
        if payload.get("model"):
            body["model"] = payload["model"]
    response = client.post("/v1/host/turns/sync", json=body, timeout=8)
    if response.status_code in {400, 404, 409, 413, 422}:
        raise Undeliverable(f"HTTP {response.status_code}")
    if not response.is_success:
        return False
    try:
        return response.json().get("accepted") is not False
    except ValueError:
        return True


__all__ = ["Capture", "INTERNAL_PLATFORMS", "SessionInfo", "SessionMap", "TurnOutbox",
           "Undeliverable", "checkpoint", "deliver", "text_of"]
