"""Protagine adapter for Hermes: stock seams only.

Registration wires four hooks (``pre_llm_call``, ``post_llm_call``,
``pre_tool_call``, ``on_kanban_dispatch_tick``), one command (``/mind``), the
model tools and one system prompt section, then starts the body thread, which
delivers captured turns and runs the mind loop (dispatch, outbox,
reconciliation, observations) against ``/v1/mind``. Registration performs no
network I/O and never changes the Hermes configuration.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping

__version__ = "1.10.0"

from .body import Body, BodyLedger
from .capture import Capture, SessionMap, TurnOutbox
from .client import ProtagineClient, Settings, SidecarUnavailable, load_settings
from . import commands
from .guard import Guard
from .reminders import SCHEMA as REMINDER_SCHEMA, Reminders
from .tools import Tools

logger = logging.getLogger(__name__)

HOOKS = ("pre_llm_call", "post_llm_call", "pre_tool_call", "on_kanban_dispatch_tick")
TOOLSET = "protagine"


def prompt_section(settings: Settings) -> Callable[[Mapping[str, Any]], str]:
    def render(_session_info: Mapping[str, Any]) -> str:
        identity = settings.identity()
        owner = identity.get("owner") if isinstance(identity.get("owner"), Mapping) else {}
        lines = []
        if owner.get("name"):
            lines.append(f"Your owner is {owner['name']}.")
        lines.append(
            "Protagine keeps your long-term memory; recalled evidence for this turn arrives with the "
            "message. Use protagine_memory_search for more, protagine_self for your own mind's state, "
            "log and open asks (the owner answers an ask by typing yes or no with its code; call "
            "protagine_self yes/no with that code only when the owner's own message contains it), "
            "protagine_people for contacts, and protagine_reminder for recalled deadlines.")
        return "\n".join(lines)[:4000]
    return render


_BODY: Body | None = None


def flush() -> dict[str, Any]:
    """Deliver the turn outbox now, on the caller's thread.

    Capture callbacks only enqueue; the body thread delivers within moments of a
    turn. A host that ends right after a turn (a one-shot CLI run, a benchmark
    episode) can call this to hand every pending row to the sidecar first.
    """
    if _BODY is None:
        raise RuntimeError("the Protagine adapter is not registered")
    return _BODY.run_once()


def tick() -> dict[str, Any]:
    """One mind tick, then one body pass, on the caller's thread.

    ``POST /v1/mind/tick`` makes the sidecar form intentions now (its own
    60 s timer does the same in a gateway), then :meth:`Body.run_once` drains
    the turn outbox and runs dispatch, outbox, reconciliation and
    observations. A benchmark body tick calls this before Hermes cron and
    kanban dispatch; without the mind routes only the body pass runs.
    """
    if _BODY is None:
        raise RuntimeError("the Protagine adapter is not registered")
    result: dict[str, Any] = {"mind_tick": None}
    try:
        if _BODY.client.has_mind_routes() is True:
            response = _BODY.client.post("/v1/mind/tick", timeout=60)
            result["mind_tick"] = response.json() if response.is_success else {"http": response.status_code}
    except (SidecarUnavailable, ValueError) as error:
        result["mind_tick"] = {"error": type(error).__name__}
    result.update(_BODY.run_once(mind=True))  # the caller is the dispatcher here
    return result


def register(ctx: Any) -> None:
    global _BODY
    settings = load_settings()
    client = ProtagineClient(settings)
    sessions = SessionMap(settings, client)
    outbox = TurnOutbox(settings.outbox_path)
    body = Body(client, outbox, sessions, settings,
                ledger=BodyLedger(settings.body_ledger_path or settings.outbox_path.with_name("protagine-body.sqlite3")))
    capture = Capture(sessions, outbox, settings, on_enqueue=body.wake)
    guard = Guard(client, sessions, settings)
    _BODY = body

    ctx.register_hook("pre_llm_call", capture.pre_llm_call)
    ctx.register_hook("post_llm_call", capture.post_llm_call)
    ctx.register_hook("pre_tool_call", guard.pre_tool_call)
    ctx.register_hook("on_kanban_dispatch_tick", body.on_dispatch_tick)

    ctx.register_command("mind", commands.handler(client, settings, outbox, body),
                         description="Protagine mind: status, log, why <id>, asks, off",
                         args_hint="[status|log|why <id>|asks|off]")

    for schema, tool_handler in Tools(client, sessions, settings).handlers():
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=tool_handler)
    ctx.register_tool(name=REMINDER_SCHEMA["name"], toolset=TOOLSET, schema=REMINDER_SCHEMA,
                      handler=Reminders(client, sessions).handle)

    ctx.register_system_prompt_section("protagine", prompt_section(settings))

    # Workers never run the body; a host that drives ticks itself (the paired benchmark) sets
    # PROTAGINE_BODY_THREAD=0 and calls tick() or flush() instead.
    if not os.environ.get("HERMES_KANBAN_TASK") and os.environ.get("PROTAGINE_BODY_THREAD", "1") != "0":
        body.start()
        on_unload = getattr(ctx, "on_unload", None)
        if callable(on_unload):
            on_unload(body.stop)
    logger.info("Protagine adapter %s registered (sidecar %s)", __version__, settings.sidecar_url)


__all__ = ["HOOKS", "TOOLSET", "__version__", "flush", "register", "tick"]
