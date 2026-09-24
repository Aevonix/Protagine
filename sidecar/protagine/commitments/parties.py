"""Who an obligation is between, as capture records it.

A captured item carries ``obligor`` (who owes the work) and ``counterpart`` (the other party),
written the way the conversation named them: ``owner``, ``assistant``, a contact id, a name or a
handle. When both are named third parties, and not the same one, the obligation is between other
people: the owner does not owe it, is not owed it and did not hand it to the assistant. Capture does
not record such an item and the duty drive never turns one into a word to the owner.

Only a clear case counts. A missing or unnamed party, the owner (``owner``, ``me``, or any name the
owner is known by), the assistant, or two spellings of one person (``same``) never make an item
someone else's, so an ambiguous item is kept.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

OWNER, ASSISTANT = "owner", "assistant"
# Words that name the owner or the assistant rather than a third party, on any lane.
OWNER_WORDS = frozenset({"owner", "the owner", "me", "myself", "i", "us", "we"})
ASSISTANT_WORDS = frozenset({"assistant", "the assistant", "agent", "the agent", "you", "yourself"})
# Items whose work is the assistant's by their kind (a deliverable, a message it sends, a cadence it
# keeps), whatever the obligor field says: never someone else's obligation.
ASSISTANT_KINDS = frozenset({"deliverable", "notice", "check_in", "cadence"})
# No party, or none named.
UNNAMED = frozenset({"", "null", "none", "nobody", "no one", "n/a", "unknown", "someone", "somebody",
                     "they", "them", "anyone"})


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).strip("\"'").lstrip("@").casefold()


def _names(values: Iterable[Any]) -> set[str]:
    return {name for name in (_norm(value) for value in values or ()) if name}


def party(value: Any, *, owner_names: Iterable[Any] = (), assistant_names: Iterable[Any] = ()) -> Optional[str]:
    """``owner``, ``assistant``, None (no party named) or the third party's normalized name."""
    text = _norm(value)
    if text in UNNAMED:
        return None
    if text in OWNER_WORDS or text in _names(owner_names):
        return OWNER
    if text in ASSISTANT_WORDS or text in _names(assistant_names):
        return ASSISTANT
    return text


def between_others(obligor: Any, counterpart: Any, *, owner_names: Iterable[Any] = (),
                   assistant_names: Iterable[Any] = (), same: Iterable[Any] = ()) -> bool:
    """True when the obligor and the counterpart are two different named third parties.

    ``owner_names`` are the other names the owner goes by (their contact id, display name, handles),
    ``assistant_names`` the assistant's, and ``same`` the names of one person known under several
    spellings (the speaker's id and display name): a pair drawn from it is one party."""
    first = party(obligor, owner_names=owner_names, assistant_names=assistant_names)
    second = party(counterpart, owner_names=owner_names, assistant_names=assistant_names)
    if first in (None, OWNER, ASSISTANT) or second in (None, OWNER, ASSISTANT) or first == second:
        return False
    alike = _names(same)
    return not (first in alike and second in alike)


__all__ = ["ASSISTANT", "ASSISTANT_KINDS", "OWNER", "between_others", "party"]
