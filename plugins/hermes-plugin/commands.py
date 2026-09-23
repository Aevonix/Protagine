"""``/mind``: read-only in chat, plus ``off``.

Hermes hands a plugin command only its argument string, never the sender, so
every mutation other than the off switch goes through the CLI or the owner-
checked tools. ``status`` works on every sidecar version; ``log``, ``why`` and
``asks`` need the ``/v1/mind`` routes.
"""

from __future__ import annotations

from typing import Any, Callable

from . import __version__
from .body import Body, mind_status
from .capture import TurnOutbox
from .client import ProtagineClient, Settings, SidecarUnavailable

USAGE = "usage: /mind status | log | why <id> | asks | off (other changes: the protagine CLI)"
ROUTES_MISSING = "This sidecar does not serve the mind routes yet; only status and off work here."


def status_text(client: ProtagineClient, settings: Settings, outbox: TurnOutbox) -> str:
    mind = settings.mind()
    health = client.health()
    lines = [
        f"Protagine adapter {__version__}",
        f"sidecar: {settings.sidecar_url} ({'reachable' if health else 'unreachable'})",
        f"mind: {'on' if mind.get('enabled', True) is not False else 'off'}, "
        f"autonomy {mind.get('autonomy', 'standard')}",
        f"worker profile: {settings.worker_profile}",
    ]
    try:
        lines.append(f"turn outbox: {outbox.pending_count()} pending")
    except Exception as error:
        lines.append(f"turn outbox: unavailable ({type(error).__name__})")
    detail = mind_status(client)
    if detail:
        for key in ("enabled", "autonomy", "queued", "asks", "last_tick"):
            if key in detail:
                lines.append(f"{key}: {detail[key]}")
    return "\n".join(lines)


def _forward(client: ProtagineClient, path: str, params: dict[str, Any] | None = None) -> str:
    if client.has_mind_routes() is not True:
        return ROUTES_MISSING
    try:
        response = client.get(path, params=params, timeout=5)
    except SidecarUnavailable:
        return "The sidecar is unreachable."
    if response.status_code == 404:
        return ROUTES_MISSING
    if not response.is_success:
        return f"The sidecar answered HTTP {response.status_code}."
    value = response.json()
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return str(value)


def handler(client: ProtagineClient, settings: Settings, outbox: TurnOutbox,
            body: Body) -> Callable[[str], str]:
    def mind(raw_args: str = "") -> str:
        words = str(raw_args or "").split()
        verb = words[0].lower() if words else "status"
        if verb == "status":
            return status_text(client, settings, outbox)
        if verb == "log":
            return _forward(client, "/v1/mind/log", {"limit": 20})
        if verb == "asks":
            return _forward(client, "/v1/mind/asks")
        if verb == "why":
            return _forward(client, f"/v1/mind/log/{words[1]}") if len(words) > 1 else USAGE
        if verb == "off":
            notes = []
            if client.has_mind_routes() is True:
                try:
                    response = client.post("/v1/mind/off", timeout=5)
                    notes.append("sidecar: off" if response.is_success else f"sidecar: HTTP {response.status_code}")
                except SidecarUnavailable:
                    notes.append("sidecar: unreachable (turn it off again once it is back)")
            else:
                notes.append("sidecar: no mind routes in this version")
            cleanup = body.off_cleanup()
            notes.append(f"archived {len(cleanup.get('archived', []))} unstarted mind task(s)")
            if cleanup.get("error"):
                notes.append(f"kanban: {cleanup['error']}")
            return "Mind off. " + "; ".join(notes) + ". Turning it back on needs the protagine CLI."
        return USAGE
    return mind


__all__ = ["USAGE", "handler", "status_text"]
