"""The per-contact template digest (architecture 4.7 item 4).

Who the person is, how they are reachable, when the agent last talked with
them, what is open, and what they have said themselves (their own sources'
claims), in at most ``MAX_CHARS`` characters. The digest is read in the
contact's own context ("About this person") and composes messages to them, so
it never carries what the owner set for them or thinks of them: permission,
cadence, the tier and who introduced them stay in the owner's
``protagine_people inspect``, read from the columns. The mind writes it daily for contacts with a recent interaction
(``Mind._digests``), with sources ``["template"]``; the LLM digest that
consolidates it arrives with the memory milestone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Mapping, Optional

MAX_CHARS = 600
MAX_CLAIMS = 4


def _utc(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ago(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _field(contact: Any, name: str, default: Any = None) -> Any:
    if isinstance(contact, Mapping):
        return contact.get(name, default)
    return getattr(contact, name, default)


TEMPLATE_SOURCES = ["template"]


def render_digest(contact: Any, *, claims: Iterable[str], counts: Mapping[str, Any], last_interaction_at: Any,
                  now: datetime) -> str:
    """The digest text, at most ``MAX_CHARS`` characters (an ellipsis marks a cut)."""
    contact_id = str(_field(contact, "contact_id", "") or "")
    name = str(_field(contact, "display_name", None) or _field(contact, "given_name", None) or contact_id or "unknown")
    parts: List[str] = [f"{name}."]
    known: List[str] = []
    first_seen = _utc(_field(contact, "first_seen_at", None))
    if first_seen is not None:
        known.append(f"known since {first_seen.date().isoformat()}")
    handles = _field(contact, "handles", None) or []
    rendered = [f"{h.get('gateway')}:{h.get('address')}" for h in handles
                if isinstance(h, Mapping) and h.get("gateway") and h.get("address")][:3]
    if rendered:
        known.append("reachable at " + ", ".join(rendered))
    if known:
        parts.append("Known: " + "; ".join(known) + ".")
    last = _utc(last_interaction_at)
    conversations = int(_field(contact, "interaction_count", 0) or 0)
    if last is None:
        parts.append("Never talked.")
    else:
        talk = f"Last talked {_ago(now - last)}"
        if conversations:
            talk += f" ({conversations} conversation{'s' if conversations != 1 else ''})"
        parts.append(talk + ".")
    inbound, outbound = counts.get("inbound"), counts.get("outbound")
    if inbound is not None or outbound is not None:
        parts.append(f"Exchanges: {int(inbound or 0)} in / {int(outbound or 0)} out.")
    open_items = counts.get("open")
    if open_items:
        parts.append(f"{int(open_items)} open item{'s' if int(open_items) != 1 else ''}.")
    notes = [" ".join(str(claim).split()) for claim in claims if str(claim or "").strip()][:MAX_CLAIMS]
    if notes:
        parts.append("Notes: " + " ".join(note if note.endswith(".") else note + "." for note in notes))
    text = " ".join(parts)
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 1].rstrip() + "…"
    return text


__all__ = ["MAX_CHARS", "MAX_CLAIMS", "TEMPLATE_SOURCES", "render_digest"]
