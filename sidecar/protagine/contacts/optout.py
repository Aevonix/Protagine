"""A contact's opt-out (architecture 4.7 item 9, 7.4).

Two detectors end here: this deterministic phrase match on the contact's own
words and the appraisal call's ``opt_out`` flag (Part B's writer). Both call
``lower_may_contact``; nothing in this module, or anywhere but the owner's
``set_may_contact`` path, ever raises ``may_contact``.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_VERBS = r"(?:message|messaging|text|texting|contact|contacting|write to|writing to|dm|dming)"

OPT_OUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*stop[.!]*\s*$", re.IGNORECASE),                       # a bare STOP is the whole message
    re.compile(r"\bunsubscribe\b", re.IGNORECASE),
    # Anchored at the end of the clause: "don't text me the file, email it" and "do not message me
    # before 9" say how or when to write, not that the person wants no messages at all.
    re.compile(rf"\b(?:don't|do not|please don't|please do not|stop) {_VERBS} me"
               r"(?:\s+(?:again|anymore|any more|ever again))?(?=\s*(?:[.!,;]|$))", re.IGNORECASE),
    re.compile(r"\bno more (?:messages|check-ins|check ins|texts|reminders) from you\b", re.IGNORECASE),
    re.compile(r"\bstop the (?:check-ins|check ins|messages|reminders|texts)\b", re.IGNORECASE),
    re.compile(r"\b(?:rather|prefer) (?:you|that you) (?:did not|didn't|do not|don't|not) "
               r"(?:message|text|contact|write to|write|dm) me\b", re.IGNORECASE),
    re.compile(r"\bleave me alone\b", re.IGNORECASE),
    re.compile(r"\bremove me\b", re.IGNORECASE),
)


def detects_opt_out(text: Optional[str]) -> Optional[str]:
    """The matched phrase when ``text`` asks not to be contacted again, else None."""
    if not text or not isinstance(text, str):
        return None
    for pattern in OPT_OUT_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).strip()
    return None


async def apply_opt_out(store: Any, contact_id: str, text: str, *, source_ref: str,
                        owner_id: Optional[str]) -> Any:
    """Lower ``may_contact`` to ``never`` when the contact's words opt out.

    Never for the owner (the owner's permission is by identity) and never for the
    ``system`` sentinel. Returns the updated contact, or None when nothing changed.
    """
    if not contact_id or contact_id == "system" or (owner_id and contact_id == owner_id):
        return None
    phrase = detects_opt_out(text)
    if phrase is None:
        return None
    return await store.lower_may_contact(contact_id, reason=phrase, source_ref=source_ref)


__all__ = ["OPT_OUT_PATTERNS", "apply_opt_out", "detects_opt_out"]
