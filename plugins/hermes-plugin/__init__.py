"""Protagine adapter for Hermes: stock seams only.

Registration wires four hooks (``pre_llm_call``, ``post_llm_call``,
``pre_tool_call``, ``on_kanban_dispatch_tick``), one command (``/mind``), the
model tools and one system prompt section (the constitution from
``identity.yaml``, the owner, the self-narrative the sidecar keeps and two tool
notes), then starts the body thread, which delivers captured turns and runs the
mind loop (dispatch, outbox, reconciliation, observations) against
``/v1/mind``. Registration performs no network I/O and never changes the
Hermes configuration.
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
# The one prompt section, frozen per session by Hermes (architecture 4.2, seam 2): the constitution
# (<= 1,500 characters), the owner, the self-narrative (<= 800, the sidecar's own cap) and the two tool
# notes, <= 4,000 in all. Every model request of an owner session carries it (tests/hermes_adapter/
# test_overhead_budget.py pins its largest render).
SECTION_CHARS, CONSTITUTION_CHARS, NARRATIVE_CHARS = 4000, 1500, 800
NARRATIVE_LEAD = ("What you know about yourself, from your own record. Plain ids are your own actions "
                  "(protagine_self why explains them); prefixed ids are record references:")
NOTES = ("Protagine keeps your long-term memory: recalled evidence arrives with each message and "
         "protagine_memory_search finds more. Answer a mind ask with protagine_self yes or no only when "
         "its code appears in the owner's own message.")


def prompt_section(settings: Settings, client: ProtagineClient | None = None,
                   sessions: SessionMap | None = None) -> Callable[[Mapping[str, Any]], str]:
    """The block the plugin adds to every session prompt: who the agent is (the owner-authored constitution
    read from ``identity.yaml``), who the owner is, what the agent knows about itself from its own record
    (``GET /v1/mind/narrative``: 2 s, a failure cached 60 s, fail open to the constitution alone) and the two things
    the tool schemas cannot say. Hermes renders it once per session, so a nightly narrative change reaches
    the next session and the prompt cache holds within one. Recall guidance is the memory provider's block.

    The narrative is the owner's record (what the agent did for the owner, its working stances), so it is
    rendered only in a session that is the owner's alone (``SessionMap.owner_only``); every other session
    gets the constitution and the notes, and the sidecar is not asked on its behalf."""
    audience = sessions if sessions is not None or client is None else SessionMap(settings, client)

    def render(session_info: Mapping[str, Any]) -> str:
        identity = settings.identity()
        parts = [settings.constitution()[:CONSTITUTION_CHARS]]
        owner = identity.get("owner") if isinstance(identity.get("owner"), Mapping) else {}
        if owner.get("name"):
            parts.append(f"Your owner is {owner['name']}.")
        narrative: Any = None
        if client is not None and audience is not None and owner_only(audience, session_info):
            try:
                narrative = client.narrative()
            except Exception:  # the record is optional; the constitution is not
                logger.debug("narrative unavailable for the prompt section", exc_info=True)
        if isinstance(narrative, Mapping) and narrative.get("enabled") is True and narrative.get("text"):
            parts.append(NARRATIVE_LEAD + "\n" + str(narrative["text"])[:NARRATIVE_CHARS])
        parts.append(NOTES)
        return "\n\n".join(part for part in parts if part)[:SECTION_CHARS]
    return render


def owner_only(sessions: SessionMap, session_info: Mapping[str, Any]) -> bool:
    """``SessionMap.owner_only`` for the session being rendered; never raises (a failure withholds)."""
    try:
        return sessions.owner_only(session_info or {})
    except Exception:
        logger.debug("session audience unknown; the narrative is withheld", exc_info=True)
        return False


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
            # A forced tick waits up to 300 s for a nightly consolidation it found due.
            response = _BODY.client.post("/v1/mind/tick", timeout=420)
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

    ctx.register_system_prompt_section("protagine", prompt_section(settings, client, sessions))

    # Workers never run the body; a host that drives ticks itself (the paired benchmark) sets
    # PROTAGINE_BODY_THREAD=0 and calls tick() or flush() instead.
    if not os.environ.get("HERMES_KANBAN_TASK") and os.environ.get("PROTAGINE_BODY_THREAD", "1") != "0":
        body.start()
        on_unload = getattr(ctx, "on_unload", None)
        if callable(on_unload):
            on_unload(body.stop)
    logger.info("Protagine adapter %s registered (sidecar %s)", __version__, settings.sidecar_url)


__all__ = ["HOOKS", "NOTES", "TOOLSET", "__version__", "flush", "prompt_section", "register", "tick"]
