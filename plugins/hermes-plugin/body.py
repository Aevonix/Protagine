"""The body thread: turn delivery, then the mind loop on ``/v1/mind``.

One daemon thread per Hermes process drains the turn outbox. In the gateway
the kanban dispatch tick (every 60 s) also wakes it. When the sidecar serves
``/v1/mind/*`` the same tick, in this order (architecture 6.2):

1. pulls ``GET /v1/mind/dispatch`` and creates one kanban task per intention
   on the ``protagine-act`` profile with idempotency key ``mind:<id>``, then
   ``POST dispatch/{id}/bound``
2. sends ``GET /v1/mind/outbox`` messages verbatim through stock
   ``send_message_tool`` with ``sending`` / ``sent`` bookkeeping on both sides;
   a send that was begun but never confirmed is reported ``uncertain`` and
   never repeated
3. reconciles every ``mind:*`` task from its state and last run row into
   ``POST /v1/mind/outcome``, and archives ``mind:*`` tasks the sidecar has no
   dispatched intention for (a hand-made task, an unapproved ask)
4. posts board observations (stale owner tasks, blocked tasks, goal tasks,
   the mind's own tasks and a body heartbeat) to ``POST /v1/mind/observations``

Every mind tick also reads the skills generation from ``/v1/mind/state``: when
the sidecar reports a change to its skills (written into Protagine's own
``skills.external_dirs`` entry), Hermes' skills prompt cache is cleared, so the
next session lists them without a restart.

With the mind off (``GET /v1/mind/state`` says ``enabled: false``, or
``protagine.yaml`` does) steps 1 and 2 are skipped and unstarted ``mind:*``
tasks are archived; nothing here needs a model endpoint. While Hermes is
paused (``hermes pause``, the stock ESTOP sentinel) steps 1 and 2 wait and
only bookkeeping runs.

One writer per board: the mind part runs only in the process that owns the
kanban dispatcher, which stock Hermes announces with ``on_kanban_dispatch_tick``
after every tick of the singleton dispatcher. A CLI session, a gateway that
lost the dispatcher lock and a worker (``HERMES_KANBAN_TASK`` set) only drain
the turn outbox; ``tick()`` (a host that drives ticks itself) runs it on
request.
"""

from __future__ import annotations

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

from . import capture as capture_module
from .capture import SessionMap, TurnOutbox
from .client import ProtagineClient, Settings, SidecarUnavailable

logger = logging.getLogger(__name__)

MIND_KEY_PREFIX = "mind:"
UNSTARTED_STATUSES = ("todo", "ready", "triage", "blocked", "scheduled")
TERMINAL_STATUSES = frozenset({"done", "blocked", "review", "archived"})
# Intention lifecycles that own no Hermes object (architecture 3.3): a ``mind:*``
# task for one of these was not created by dispatch and is archived.
NOT_DISPATCHED = frozenset({"proposed", "asked", "denied", "expired", "cancelled", "dropped"})
FAILED_RUN_OUTCOMES = frozenset({"crashed", "timed_out", "spawn_failed", "gave_up", "stale", "reclaimed", "failed"})
SEND_STATES = ("sending", "sent", "failed", "uncertain")
DEFAULT_STALE_TASK_HOURS = 72
OBSERVATION_INTERVAL = 300.0
INTENTION_CACHE_TTL = 300.0


def _iso(timestamp: float | int | None) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()


def paused() -> bool:
    """Stock ``hermes pause`` (``agent.estop``): True while its sentinel exists; False outside Hermes."""
    try:
        from agent.estop import is_engaged
    except ImportError:
        return False
    try:
        return bool(is_engaged())
    except Exception:
        return False


def failure_limit(task: Any) -> int:
    """The failures after which stock parks a task as blocked: its ``max_retries``, else the dispatcher's."""
    if getattr(task, "max_retries", None) is not None:
        return int(task.max_retries)
    try:
        from hermes_cli.kanban_db_dispatch import DEFAULT_FAILURE_LIMIT
        return int(DEFAULT_FAILURE_LIMIT)
    except Exception:
        return 2


DM_CHAT_TYPES = frozenset({"dm", "direct", "private"})


def dm_chat_id(platform: str, user_id: str) -> str | None:
    """The chat id of the gateway's direct-message session with ``user_id`` on ``platform``.

    A handle in ``identity.yaml`` or a contact handle names a sender; on
    Discord the sender's id is not a channel, so the message goes to the DM
    the gateway has already had with that sender, read from the stock session
    rows (the same source the channel directory lists DMs from). None when no
    such session was seen; the handle is then used as it is.
    """
    if not platform or not user_id:
        return None
    try:
        from hermes_state_registry import acquire, release_or_close
        db = acquire()
    except Exception:
        return None
    try:
        rows = db.list_gateway_sessions(platform=platform, active_only=False)
    except Exception:
        return None
    finally:
        release_or_close(db)
    wanted = user_id.strip().lower()
    for row in rows:  # newest activity first
        if str(row.get("user_id") or "").strip().lower() != wanted or not row.get("chat_id"):
            continue
        if str(row.get("chat_type") or "dm").strip().lower() in DM_CHAT_TYPES:
            return str(row["chat_id"])
    return None


def send_message(target: str, text: str) -> dict[str, Any]:
    """Send ``text`` verbatim through stock ``send_message_tool`` (``tools/send_message_tool.py``).

    Returns ``{"result": "sent" | "failed" | "uncertain", "error": str, "detail": dict}``.
    The tool answers a JSON string: ``success`` means delivered; an error raised
    before the platform call (unknown target, unconfigured platform, no home
    channel) means nothing left; ``Send failed: ...`` and any exception mean the
    platform may or may not have delivered, which is ``uncertain``.
    """
    try:
        from tools.send_message_tool import send_message_tool
        raw = send_message_tool({"action": "send", "target": target, "message": text})
    except Exception as error:
        return {"result": "uncertain", "error": f"{type(error).__name__}: {error}"[:500], "detail": {}}
    try:
        detail = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        detail = {"raw": str(raw)[:500]}
    if not isinstance(detail, dict):
        detail = {"raw": str(detail)[:500]}
    if detail.get("success"):
        return {"result": "sent", "error": "", "detail": detail}
    error = str(detail.get("error") or detail.get("raw") or "no result")
    uncertain = error.startswith("Send failed") or "raw" in detail
    return {"result": "uncertain" if uncertain else "failed", "error": error[:500], "detail": detail}


class BodyLedger:
    """Durable, body-private bookkeeping: message sends and posted outcomes.

    The sidecar owns intention state; this ledger only keeps the body from
    sending a message twice (a message begun here is never begun again, even
    after a crash or a sidecar that offers it again) and from posting the same
    task outcome every tick.
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
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        with self._lock:
            if not self._ready:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS mind_sends (
                        id TEXT PRIMARY KEY, state TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT '', reported INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL)
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS mind_outcomes (
                        task_id TEXT PRIMARY KEY, intention_id TEXT NOT NULL,
                        fingerprint TEXT NOT NULL, updated_at REAL NOT NULL)
                """)
                self._ready = True
        return connection

    # -- sends -------------------------------------------------------------------

    def send(self, message_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM mind_sends WHERE id = ?", (str(message_id),)).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def begin_send(self, message_id: str, target: str) -> bool:
        """Record that a send starts now; False when this message was begun before."""
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM mind_sends WHERE id = ?", (str(message_id),)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO mind_sends (id, state, target, created_at, updated_at) VALUES (?, 'sending', ?, ?, ?)",
                    (str(message_id), target, now, now))
            connection.execute("COMMIT")
            return row is None
        finally:
            connection.close()

    def end_send(self, message_id: str, result: str, error: str = "", *, reported: bool = False) -> None:
        if result not in SEND_STATES:
            raise ValueError(f"unknown send result {result!r}")
        connection = self._connect()
        try:
            connection.execute("UPDATE mind_sends SET state = ?, error = ?, reported = ?, updated_at = ? WHERE id = ?",
                               (result, error[:500], 1 if reported else 0, time.time(), str(message_id)))
        finally:
            connection.close()

    def mark_reported(self, message_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute("UPDATE mind_sends SET reported = 1, updated_at = ? WHERE id = ?",
                               (time.time(), str(message_id)))
        finally:
            connection.close()

    def unreported_sends(self) -> list[dict[str, Any]]:
        """Sends the sidecar has not acknowledged, oldest first; ``sending`` rows are crashes."""
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM mind_sends WHERE reported = 0 ORDER BY created_at").fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    # -- outcomes ------------------------------------------------------------------

    def outcome_fingerprint(self, task_id: str) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT fingerprint FROM mind_outcomes WHERE task_id = ?", (task_id,)).fetchone()
            return row["fingerprint"] if row else None
        finally:
            connection.close()

    def record_outcome(self, task_id: str, intention_id: str, fingerprint: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO mind_outcomes (task_id, intention_id, fingerprint, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(task_id) DO UPDATE SET fingerprint = excluded.fingerprint, updated_at = excluded.updated_at",
                (task_id, intention_id, fingerprint, time.time()))
        finally:
            connection.close()

    def prune(self, *, older_than: float = 30 * 86400) -> None:
        cutoff = time.time() - older_than
        connection = self._connect()
        try:
            connection.execute("DELETE FROM mind_sends WHERE reported = 1 AND updated_at < ?", (cutoff,))
            connection.execute("DELETE FROM mind_outcomes WHERE updated_at < ?", (cutoff,))
        finally:
            connection.close()


def _kanban():
    """The stock kanban module and a connection to the current board."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    return kb, connect()


def _items(payload: Any, key: str) -> list[dict[str, Any]]:
    """The list under ``key`` (or a bare list) as dicts with an ``id``."""
    if isinstance(payload, Mapping):
        payload = payload.get(key) or payload.get("items") or []
    if not isinstance(payload, list):
        return []
    return [dict(item) for item in payload if isinstance(item, Mapping) and item.get("id") is not None]


class Body:
    def __init__(self, client: ProtagineClient, outbox: TurnOutbox, sessions: SessionMap,
                 settings: Settings, *, interval: float = 60.0, idle_interval: float = 5.0,
                 ledger: BodyLedger | None = None):
        self.client, self.outbox, self.sessions, self.settings = client, outbox, sessions, settings
        self.interval, self.idle_interval = interval, idle_interval
        self.ledger = ledger or BodyLedger(settings.body_ledger_path
                                           or settings.outbox_path.with_name("protagine-body.sqlite3"))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ticks = 0
        self._mind_ticks = 0
        self._started_at = time.time()
        self._last_tick_at: float | None = None
        self._last_mind_tick_at: float | None = None
        self._last_pull_at: float | None = None
        self._last_observation: tuple[float, str] | None = None
        self._intentions: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._dispatcher = False  # set once this process's dispatcher ticked: it holds the singleton lock
        self._skills_generation: int | None = None
        self._skills_cache_warned = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="protagine-body", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def on_dispatch_tick(self, **_: Any) -> None:
        """``on_kanban_dispatch_tick`` observer: the gateway heartbeat.

        Stock fires it from ``dispatch_once`` in the one process holding the
        dispatcher lock, so it also tells the body it may write to the board.
        """
        self._dispatcher = True
        self.wake()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                pending = self.run_once().get("pending", 0)
            except Exception as error:
                logger.warning("body tick failed (%s)", type(error).__name__)
                pending = 0
            self._wake.wait(self.idle_interval if pending else self.interval)
            self._wake.clear()

    def heartbeat(self) -> dict[str, Any]:
        """What the body has been doing; shown by ``/mind status`` and posted with observations."""
        return {
            "pid": os.getpid(), "profile": os.environ.get("HERMES_PROFILE") or "default",
            "started_at": _iso(self._started_at), "interval_s": self.interval,
            "ticks": self._ticks, "mind_ticks": self._mind_ticks, "dispatcher": self._dispatcher,
            "last_tick_at": _iso(self._last_tick_at), "last_mind_tick_at": _iso(self._last_mind_tick_at),
            "last_pull_at": _iso(self._last_pull_at),
            "stale": self._last_mind_tick_at is not None and time.time() - self._last_mind_tick_at > 5 * self.interval,
        }

    # -- one tick --------------------------------------------------------------

    def run_once(self, *, mind: bool | None = None) -> dict[str, Any]:
        """Drain the turn outbox, then the mind part when this process may write to the board.

        ``mind`` forces the decision (``tick()`` passes True: the caller drives
        the dispatcher itself); by default the mind part runs only after the
        dispatcher tick has been observed here, so a CLI session or a gateway
        without the dispatcher lock never becomes a second writer. Such a
        process only reads the mind state, to clear its own skills cache.
        """
        self._ticks += 1
        self._last_tick_at = time.time()
        delivered = self.outbox.drain(
            lambda payload: capture_module.deliver(self.client, self.sessions, self.settings, payload))
        if self._ticks % 100 == 0:
            self.outbox.prune()
            self.ledger.prune()
        result = {"delivered": delivered, "pending": self.outbox.pending_count(), "mind": False,
                  "dispatcher": self._dispatcher}
        if os.environ.get("HERMES_KANBAN_TASK"):
            return result  # workers never dispatch; one writer per gateway
        if not (self._dispatcher if mind is None else mind):
            # Not the dispatcher owner: capture only, never the board. Hermes caches the skills index per
            # process, so this process still follows the sidecar's skills generation.
            self.skills_changed(self.client.mind_state() or {})
            return result
        if self.client.has_mind_routes():
            result["mind"] = True
            result.update(self.mind_tick())
        return result

    def mind_tick(self) -> dict[str, Any]:
        """Dispatch, outbox, reconciliation and observations, each step on its own."""
        self._mind_ticks += 1
        state = self.client.mind_state() or {}
        self.skills_changed(state)
        enabled = state.get("enabled") is not False and self.settings.mind().get("enabled") is not False
        held = paused()
        result: dict[str, Any] = {"enabled": enabled, "paused": held, "dispatched": 0, "sent": 0,
                                  "reconciled": 0, "archived": 0, "observed": False}
        steps: list[tuple[str, Callable[[], Any]]] = []
        if held:
            steps += [("sent", self.settle_sends)]  # hermes pause: nothing new lands, bookkeeping only
        elif enabled:
            steps += [("dispatched", self.dispatch), ("sent", self.send_outbox)]
        else:
            steps += [("sent", self.settle_sends), ("archived", lambda: len(self.off_cleanup().get("archived", [])))]
        steps += [("reconciled", self.reconcile), ("observed", self.observe)]
        for name, step in steps:
            try:
                value = step()
            except SidecarUnavailable:
                logger.debug("mind %s skipped: sidecar unreachable", name)
                continue
            except Exception as error:
                logger.warning("mind %s failed (%s)", name, type(error).__name__)
                continue
            if isinstance(value, Mapping):
                for key, item in value.items():
                    result[key] = result.get(key, 0) + item if isinstance(item, int) else item
            else:
                result[name] = value
        self._last_mind_tick_at = time.time()
        return result

    def skills_changed(self, state: Mapping[str, Any]) -> bool:
        """Clear Hermes' skills prompt cache when the sidecar's skills generation moved. The first value a
        process sees is only recorded; without the stock function a new skill appears after a restart."""
        skills = state.get("skills") if isinstance(state, Mapping) else None
        generation = skills.get("generation") if isinstance(skills, Mapping) else None
        if isinstance(generation, bool) or not isinstance(generation, int):
            return False
        last, self._skills_generation = self._skills_generation, generation
        if last is None or last == generation:
            return False
        try:
            from agent.prompt_builder import clear_skills_system_prompt_cache
        except ImportError:
            if not self._skills_cache_warned:
                logger.info("Hermes has no skills prompt cache to clear; new skills appear after a restart")
                self._skills_cache_warned = True
            return False
        clear_skills_system_prompt_cache()
        return True

    # -- dispatch ------------------------------------------------------------------

    def dispatch(self) -> int:
        """``GET /v1/mind/dispatch`` → one kanban task per intention → ``POST dispatch/{id}/bound``."""
        response = self.client.get("/v1/mind/dispatch", timeout=5)
        self._last_pull_at = time.time()
        if response.status_code == 404 or not response.is_success:
            return 0
        items = _items(response.json(), "intentions")
        if not items:
            return 0
        created = 0
        kb, conn = _kanban()
        try:
            # Stock's key lookup ignores archived tasks. A task created here, archived before its
            # ack landed (the body died in between), must settle its intention, not be recreated.
            live: set[str] = set()
            archived: dict[str, Any] = {}
            for task in kb.list_tasks(conn, include_archived=True):
                key = str(getattr(task, "idempotency_key", "") or "")
                if not key.startswith(MIND_KEY_PREFIX):
                    continue
                if task.status != "archived":
                    live.add(key)
                elif key not in archived or int(task.created_at or 0) > int(archived[key].created_at or 0):
                    archived[key] = task
            for item in items:
                if str(item.get("kind") or "task") != "task":
                    continue  # messages and notices travel through the outbox; a goal owns no Hermes object
                intention_id = str(item["id"])
                key = self._key(intention_id, item)
                predecessor = archived.get(key) if key not in live else None
                if predecessor is not None:
                    task, task_id = predecessor, predecessor.id
                else:
                    task_id = self._create_task(kb, conn, intention_id, item)
                    task = kb.get_task(conn, task_id)
                self._intentions.pop(intention_id, None)
                ack = self.client.post(f"/v1/mind/dispatch/{intention_id}/bound", timeout=5, json={
                    "hermes_ref": task_id, "hermes_kind": "kanban",
                    "status": task.status if task else None, "bound_at": _iso(time.time())})
                if not ack.is_success and ack.status_code != 404:
                    logger.warning("bound ack for %s answered HTTP %s", intention_id, ack.status_code)
                if predecessor is not None:
                    self._report(kb, conn, predecessor, intention_id)  # settled as cancelled, now
                    logger.info("bound %s to its archived task %s instead of a new one", intention_id, task_id)
                created += 1
        finally:
            conn.close()
        return created

    @staticmethod
    def _key(intention_id: str, item: Mapping[str, Any]) -> str:
        key = str(item.get("dedup_key") or "")
        return key if key.startswith(MIND_KEY_PREFIX) else MIND_KEY_PREFIX + intention_id

    def _create_task(self, kb: Any, conn: Any, intention_id: str, item: Mapping[str, Any]) -> str:
        """``kanban_db.create_task`` with the mind's key and profile; an existing key returns its task."""
        key = self._key(intention_id, item)
        title = str(item.get("title") or "").strip() or f"Mind intention {intention_id}"
        body = item.get("body") if item.get("body") is not None else item.get("text")
        runtime = item.get("max_runtime_seconds") or self.settings.budget("task_max_runtime_s", 600)
        retries = item.get("max_retries")
        retries = self.settings.budget("task_max_retries", 1) if retries is None else retries
        return kb.create_task(
            conn, title=title[:200], body=str(body or ""), assignee=self.settings.worker_profile,
            created_by="protagine", workspace_kind="scratch", idempotency_key=key,
            priority=int(item.get("priority") or 0), max_runtime_seconds=int(runtime),
            max_retries=int(retries), goal_mode=bool(item.get("goal_mode")),
            goal_max_turns=int(item["goal_max_turns"]) if item.get("goal_max_turns") else None)

    # -- outbox ----------------------------------------------------------------------

    def message_target(self, message: Mapping[str, Any]) -> str:
        """``platform:chat_id`` for ``send_message_tool``.

        In order: an explicit ``target``; a ``recipient`` given as ``platform:id``
        or as ``{platform, chat_id|address|handle}``; for the owner (``recipient_is_owner``,
        or a recipient that is the owner contact or unnamed) the first handle in
        ``identity.yaml``; then the sidecar's ``recipient_handles`` (``{gateway|platform,
        address}``, primary first). A contact with no handle anywhere gets nothing.
        """
        target = message.get("target")
        if isinstance(target, str) and ":" in target:
            return target.strip()
        recipient = message.get("recipient")
        contact = ""
        if isinstance(recipient, str):
            if ":" in recipient:
                return recipient.strip()
            contact = recipient.strip()
        elif isinstance(recipient, Mapping):
            platform = str(recipient.get("platform") or recipient.get("gateway") or "").strip().lower()
            address = str(recipient.get("chat_id") or recipient.get("address") or recipient.get("handle") or "").strip()
            if platform and address:
                return f"{platform}:{address}"
            contact = str(recipient.get("contact_id") or "").strip()
        owner = message.get("recipient_is_owner") is True or not contact or contact in {"owner", self.settings.owner_contact_id()}
        if owner:
            handle = self.settings.owner_handle()
            if handle:
                return self._handle_target(handle[0], handle[1])
        handles = message.get("recipient_handles")
        for item in handles if isinstance(handles, list) else []:
            if not isinstance(item, Mapping):
                continue
            platform = str(item.get("gateway") or item.get("platform") or "").strip().lower()
            address = str(item.get("address") or item.get("chat_id") or item.get("handle") or "").strip()
            if platform and address:
                return self._handle_target(platform, address)
        return ""

    @staticmethod
    def _handle_target(platform: str, handle: str) -> str:
        """A sender handle as a send target: the DM the gateway has had with that sender, else the handle."""
        platform = platform.strip().lower()
        return f"{platform}:{dm_chat_id(platform, handle) or handle}"

    def settle_sends(self) -> int:
        """Report every send the sidecar has not acknowledged; a crashed ``sending`` becomes ``uncertain``."""
        settled = 0
        for row in self.ledger.unreported_sends():
            result, error = row["state"], row["error"]
            if result == "sending":
                result, error = "uncertain", "the body stopped between sending and sent"
                self.ledger.end_send(row["id"], result, error)
            if self._report_sent(row["id"], result, error, hermes_ref=None):
                settled += 1
        return settled

    def _report_sent(self, message_id: str, result: str, error: str, *, hermes_ref: str | None) -> bool:
        payload = {"result": result, "error": error or None, "hermes_ref": hermes_ref, "at": _iso(time.time())}
        response = self.client.post(f"/v1/mind/outbox/{message_id}/sent", timeout=5, json=payload)
        if response.is_success or response.status_code in {404, 409, 410}:
            self.ledger.mark_reported(message_id)
            return True
        return False

    def send_outbox(self) -> int:
        """``GET /v1/mind/outbox`` → ``sending`` → ``send_message_tool`` → ``sent``; each message once."""
        self.settle_sends()
        response = self.client.get("/v1/mind/outbox", timeout=5)
        if response.status_code == 404 or not response.is_success:
            return 0
        sent = 0
        for message in _items(response.json(), "messages"):
            message_id = str(message["id"])
            if self.ledger.send(message_id) is not None:
                continue  # begun before: never twice, whatever the sidecar lists
            text = str(message.get("text") if message.get("text") is not None else message.get("body") or "")
            target = self.message_target(message)
            claim = self.client.post(f"/v1/mind/outbox/{message_id}/sending", timeout=5,
                                     json={"target": target or None, "at": _iso(time.time())})
            if not claim.is_success:
                continue  # 409: already claimed or settled on the sidecar; anything else: not ours
            if not self.ledger.begin_send(message_id, target):
                continue
            if not target or not text.strip():
                outcome = {"result": "failed", "error": "no recipient handle" if not target else "empty message"}
            else:
                outcome = send_message(target, text)
            self.ledger.end_send(message_id, outcome["result"], outcome.get("error") or "")
            ref = outcome.get("detail", {}).get("message_id") if isinstance(outcome.get("detail"), dict) else None
            self._report_sent(message_id, outcome["result"], outcome.get("error") or "",
                              hermes_ref=str(ref) if ref else None)
            if outcome["result"] == "sent":
                sent += 1
        return sent

    # -- reconciliation ----------------------------------------------------------------

    def intention(self, intention_id: str) -> dict[str, Any] | None:
        """``GET /v1/mind/why/{id}``: the sidecar's row, or None only on a definite 404.

        A transient failure raises, so an unreachable sidecar never archives anything.
        """
        cached = self._intentions.get(intention_id)
        if cached is not None and time.time() - cached[0] < INTENTION_CACHE_TTL:
            return cached[1]
        response = self.client.get(f"/v1/mind/why/{intention_id}", timeout=5)
        if response.status_code == 404:
            value: dict[str, Any] | None = None
        elif response.is_success:
            loaded = response.json()
            value = loaded if isinstance(loaded, dict) else {}
            if isinstance(value.get("intention"), Mapping):
                value = dict(value["intention"])
        else:
            raise SidecarUnavailable(f"HTTP {response.status_code}")
        self._intentions[intention_id] = (time.time(), value)
        return value

    def reconcile(self) -> dict[str, int]:
        """State plus last run of every ``mind:*`` task → ``POST /v1/mind/outcome``; orphans archived."""
        reconciled = archived = 0
        cutoff = time.time() - 7 * 86400
        kb, conn = _kanban()
        try:
            for task in kb.list_tasks(conn, include_archived=True):
                key = str(getattr(task, "idempotency_key", "") or "")
                if not key.startswith(MIND_KEY_PREFIX):
                    continue
                if task.status == "archived" and int(task.created_at or 0) < cutoff:
                    continue
                intention_id = key[len(MIND_KEY_PREFIX):]
                if task.status != "archived":
                    try:
                        known = self.intention(intention_id)
                    except SidecarUnavailable:
                        continue
                    if known is None or str(known.get("status") or "") in NOT_DISPATCHED:
                        if kb.archive_task(conn, task.id):
                            archived += 1
                            logger.info("archived %s: no dispatched intention %s", task.id, intention_id)
                        continue
                reported = self._report(kb, conn, task, intention_id)
                reconciled += reported == "reconciled"
                archived += reported == "archived"
        finally:
            conn.close()
        return {"reconciled": reconciled, "archived": archived}

    def _report(self, kb: Any, conn: Any, task: Any, intention_id: str) -> str:
        """One ``POST /v1/mind/outcome`` per state change of a ``mind:*`` task.

        Returns ``reconciled`` (posted), ``archived`` (the intention is gone and
        the task was archived) or ``""`` (nothing to report yet, or seen before).
        """
        run = kb.latest_run(conn, task.id)
        ended = run is not None and run.ended_at is not None
        if task.status not in TERMINAL_STATUSES and not ended:
            return ""
        fingerprint = f"{task.status}:{run.id if run else ''}:{run.outcome if run else ''}:{run.ended_at if run else ''}"
        if self.ledger.outcome_fingerprint(task.id) == fingerprint:
            return ""
        payload = self._outcome(kb, conn, task, run, intention_id)
        response = self.client.post("/v1/mind/outcome", timeout=5, json=payload)
        if response.is_success:
            self.ledger.record_outcome(task.id, intention_id, fingerprint)
            return "reconciled"
        if response.status_code == 404:  # no such intention: nothing to report to
            self._intentions.pop(intention_id, None)
            self.ledger.record_outcome(task.id, intention_id, fingerprint)
            if task.status != "archived" and kb.archive_task(conn, task.id):
                return "archived"
        return ""

    @staticmethod
    def _outcome(kb: Any, conn: Any, task: Any, run: Any, intention_id: str) -> dict[str, Any]:
        """The body's reading of a task: its status, its last run and, for a failure, whether stock
        will retry it (``final: false``) or has given up (``blocked`` with ``consecutive_failures``
        at the limit; an operator's ``kanban_block`` ends its run ``blocked`` instead)."""
        run_outcome = str(run.outcome or "") if run else ""
        final = task.status in TERMINAL_STATUSES
        if task.status == "done":
            outcome = "done"
        elif task.status == "archived":
            outcome = "cancelled"
        elif task.status == "blocked" and run_outcome in FAILED_RUN_OUTCOMES:
            outcome = "failed"
            final = int(task.consecutive_failures or 0) >= failure_limit(task)
        elif task.status in {"blocked", "review"}:
            outcome = "blocked"
        elif run_outcome in FAILED_RUN_OUTCOMES:
            outcome = "failed"
        else:
            outcome = "uncertain"
        failed = run_outcome in FAILED_RUN_OUTCOMES or bool(run and run.error)
        summary = (run.summary if run and run.summary else None) or task.result or kb.latest_summary(conn, task.id)
        return {
            "id": intention_id, "hermes_ref": task.id, "hermes_kind": "kanban",
            "status": task.status, "outcome": outcome, "final": final,
            "summary": (summary or "")[:4000] or None,
            "error": (run.error if run and run.error else task.last_failure_error) or None,
            "verified": "hermes_failure" if failed and outcome != "done" else None,
            "run": {"id": run.id, "outcome": run.outcome, "status": run.status, "profile": run.profile,
                    "started_at": _iso(run.started_at), "ended_at": _iso(run.ended_at)} if run else None,
            "block_kind": getattr(task, "block_kind", None), "consecutive_failures": int(task.consecutive_failures or 0),
            "completed_at": _iso(task.completed_at), "observed_at": _iso(time.time()),
        }

    # -- off switch --------------------------------------------------------------

    def off_cleanup(self) -> dict[str, Any]:
        """Archive unstarted ``mind:*`` tasks; running workers end on their own."""
        archived: list[str] = []
        try:
            kb, conn = _kanban()
        except ImportError:
            return {"archived": archived, "error": "kanban unavailable"}
        except Exception as error:
            return {"archived": archived, "error": type(error).__name__}
        try:
            for task in kb.list_tasks(conn):
                key = str(getattr(task, "idempotency_key", "") or "")
                if key.startswith(MIND_KEY_PREFIX) and task.status in UNSTARTED_STATUSES:
                    if kb.archive_task(conn, task.id):
                        archived.append(task.id)
        finally:
            conn.close()
        return {"archived": archived}

    # -- observations ----------------------------------------------------------------

    def observe(self, *, force: bool = False) -> bool:
        """``POST /v1/mind/observations`` when the board changed or every 5 minutes."""
        payload = self.observations()
        digest = hashlib.sha256(json.dumps(
            [payload["counts"], [(t["id"], t["status"]) for t in payload["stale_tasks"] + payload["blocked_tasks"]
                                 + payload["goals"] + payload["mind_tasks"]]], sort_keys=True).encode()).hexdigest()
        last = self._last_observation
        if not force and last is not None and last[1] == digest and time.time() - last[0] < OBSERVATION_INTERVAL:
            return False
        response = self.client.post("/v1/mind/observations", timeout=5, json=payload)
        if response.status_code == 404 or not response.is_success:
            return False
        self._last_observation = (time.time(), digest)
        return True

    def observations(self) -> dict[str, Any]:
        now = time.time()
        stale_after = float(self.settings.mind().get("stale_task_hours") or DEFAULT_STALE_TASK_HOURS) * 3600
        kb, conn = _kanban()
        try:
            tasks = kb.list_tasks(conn)
            try:
                board = kb.get_current_board()
            except Exception:
                board = None
            counts: dict[str, int] = {}
            stale: list[dict[str, Any]] = []
            blocked: list[dict[str, Any]] = []
            goals: list[dict[str, Any]] = []
            mine: list[dict[str, Any]] = []
            for task in tasks:
                counts[task.status] = counts.get(task.status, 0) + 1
                key = str(getattr(task, "idempotency_key", "") or "")
                touched = max(int(task.started_at or 0), int(task.last_heartbeat_at or 0), int(task.created_at or 0))
                entry = {
                    "id": task.id, "title": str(task.title or "")[:200], "status": task.status,
                    "assignee": task.assignee, "created_by": task.created_by,
                    "created_at": _iso(task.created_at), "started_at": _iso(task.started_at),
                    "age_s": int(now - int(task.created_at or now)), "idle_s": int(now - touched),
                    "block_kind": getattr(task, "block_kind", None), "goal": bool(task.goal_mode),
                }
                if key.startswith(MIND_KEY_PREFIX):
                    run = kb.latest_run(conn, task.id)
                    mine.append({**entry, "intention_id": key[len(MIND_KEY_PREFIX):],
                                 "run": {"id": run.id, "outcome": run.outcome, "ended_at": _iso(run.ended_at)} if run else None})
                    continue
                if task.goal_mode:
                    goals.append({**entry, "goal_max_turns": task.goal_max_turns})
                if task.status == "blocked":
                    blocked.append(entry)
                elif task.status in {"todo", "ready", "review", "triage", "scheduled"} and now - touched >= stale_after:
                    stale.append({**entry, "stale_after_s": int(stale_after)})
        finally:
            conn.close()
        limit = 100
        return {"observed_at": _iso(now), "board": board, "body": self.heartbeat(), "counts": counts,
                "stale_tasks": stale[:limit], "blocked_tasks": blocked[:limit], "goals": goals[:limit],
                "mind_tasks": mine[:limit]}


def mind_state(client: ProtagineClient) -> dict[str, Any] | None:
    """``GET /v1/mind/state`` when the route exists; None otherwise."""
    return client.mind_state()


__all__ = ["Body", "BodyLedger", "DM_CHAT_TYPES", "MIND_KEY_PREFIX", "NOT_DISPATCHED", "TERMINAL_STATUSES",
           "UNSTARTED_STATUSES", "dm_chat_id", "failure_limit", "mind_state", "paused", "send_message"]
