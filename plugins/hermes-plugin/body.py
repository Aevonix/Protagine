"""The body thread: outbox delivery now, mind dispatch when ``/v1/mind`` exists.

One daemon thread per Hermes process drains the turn outbox. In the gateway
the kanban dispatch tick (every 60 s) also wakes it. Dispatch, outbox,
reconciliation and observations are pulled from ``/v1/mind/*`` only when the
sidecar serves those routes; until then the mind part of the tick is a no-op.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from . import capture as capture_module
from .capture import SessionMap, TurnOutbox
from .client import ProtagineClient, Settings, SidecarUnavailable

logger = logging.getLogger(__name__)

MIND_KEY_PREFIX = "mind:"
UNSTARTED_STATUSES = ("todo", "ready", "triage", "blocked")


class Body:
    def __init__(self, client: ProtagineClient, outbox: TurnOutbox, sessions: SessionMap,
                 settings: Settings, *, interval: float = 60.0, idle_interval: float = 5.0):
        self.client, self.outbox, self.sessions, self.settings = client, outbox, sessions, settings
        self.interval, self.idle_interval = interval, idle_interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ticks = 0

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
        """``on_kanban_dispatch_tick`` observer: the gateway heartbeat."""
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

    # -- one tick --------------------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        self._ticks += 1
        delivered = self.outbox.drain(
            lambda payload: capture_module.deliver(self.client, self.sessions, self.settings, payload))
        if self._ticks % 100 == 0:
            self.outbox.prune()
        result = {"delivered": delivered, "pending": self.outbox.pending_count(), "mind": False}
        if os.environ.get("HERMES_KANBAN_TASK"):
            return result  # workers never dispatch; one writer per gateway
        if self.client.has_mind_routes():
            result["mind"] = True
            result.update(self.mind_tick())
        return result

    def mind_tick(self) -> dict[str, Any]:
        """Dispatch, outbox, reconciliation and observations (filled in by M2).

        The order is fixed by the architecture: pull approved intentions and
        create kanban tasks, send outbox messages verbatim, reconcile ``mind:*``
        task outcomes, then post observations. Each step is a stub here.
        """
        return {"dispatched": 0, "sent": 0, "reconciled": 0}

    # -- off switch --------------------------------------------------------------

    def off_cleanup(self) -> dict[str, Any]:
        """Archive unstarted ``mind:*`` tasks; running workers end on their own."""
        archived: list[str] = []
        try:
            from hermes_cli import kanban_db as kb
            from hermes_cli.kanban_db_connect import connect
        except ImportError:
            return {"archived": archived, "error": "kanban unavailable"}
        try:
            conn = connect()
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


def mind_status(client: ProtagineClient) -> dict[str, Any] | None:
    """``GET /v1/mind/status`` when the route exists; None otherwise."""
    try:
        if client.has_mind_routes() is not True:
            return None
        response = client.get("/v1/mind/status", timeout=3)
        return response.json() if response.is_success else None
    except (SidecarUnavailable, ValueError):
        return None


__all__ = ["Body", "MIND_KEY_PREFIX", "mind_status"]
