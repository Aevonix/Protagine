"""The mind's open questions to the owner, carried to the owner's own turn about their subject.

The nightly consolidation raises a contradiction (two live statements about one subject disagree) as
one question to the owner. Once asked, it is the mind's open business until the statements agree again
(the owner's answer corrects one, or one is withdrawn). An owner turn about that subject must not be
answered as if either value were settled, so the reply's context carries the question, and only while
the disagreement stands (``Consolidation.open_conflict``).
"""

from __future__ import annotations

import logging
import re
from typing import Any, List

from .consolidate import QUESTION_KEY

logger = logging.getLogger(__name__)

QUESTION_TYPES = frozenset({"contradiction"})
# Asked and not withdrawn: waiting for a word, queued, on its way or delivered.
ASKED_STATUSES = ("proposed", "asked", "approved", "sending", "sent", "uncertain")
MAX_QUESTIONS = 2
QUESTION_CHARS = 400
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# Words every subject key or predicate may carry that say nothing about which subject it is.
_GENERIC = frozenset({"speaker", "the", "and", "for", "with", "your", "their", "our", "his", "her"})


def _words(text: Any) -> set:
    return {word for word in (item.casefold() for item in _WORD.findall(str(text or "")))
            if len(word) >= 3 and word not in _GENERIC}


def open_questions(mind: Any, query: str, *, limit: int = MAX_QUESTIONS) -> List[str]:
    """The questions the mind asked the owner that are still open and about what ``query`` is about: a
    word of the question's subject (its subject and property as recorded) is a word of the query.
    Newest first, at most ``limit``; nothing without a mind, a store or a consolidation."""
    store = getattr(mind, "store", None)
    consolidation = getattr(mind, "consolidation", None)
    asked = _words(query)
    if store is None or consolidation is None or not asked:
        return []
    now = mind.clock()
    lines: List[str] = []
    for row in store.intentions(status=list(ASKED_STATUSES), kind=["message"], limit=200):
        if row.type not in QUESTION_TYPES or not str(row.dedup_key or "").startswith(QUESTION_KEY):
            continue
        evidence = (row.context or {}).get("evidence") if isinstance(row.context, dict) else None
        try:
            conflict = consolidation.open_conflict(evidence or [], now)
        except Exception as error:
            logger.debug("open question %s not checked (%s)", row.id, type(error).__name__)
            continue
        if conflict is None:
            continue
        subject = _words(str(conflict["predicate"]).replace("_", " ")) | _words(conflict["subject_key"])
        if not subject & asked:
            continue
        text = " ".join(str((row.context or {}).get("text") or row.description or "").split())[:QUESTION_CHARS]
        when = row.completed_at or row.created_at
        stamp = f" on {when.date().isoformat()}" if when is not None else ""
        lines.append(f"{text} (you asked the owner this{stamp}; no answer yet)")
        if len(lines) >= limit:
            break
    return lines


__all__ = ["open_questions"]
