"""Cheap token estimation for routing, without a tokenizer dependency.

English prose runs about 4 characters per token; code and symbol-dense text
about 3. A crude density probe picks the ratio. Accuracy within about 20%
is enough for candidate selection.
"""

from __future__ import annotations

import math
import os

__all__ = ["estimate_tokens"]

_DEFAULT_CHARS_PER_TOKEN = 4.0
_CODE_CHARS_PER_TOKEN = 3.0


def _chars_per_token() -> float:
    try:
        value = float(os.environ.get("PROTAGINE_CONTEXT_CHARS_PER_TOKEN", ""))
        if value > 0:
            return value
    except ValueError:
        pass
    return _DEFAULT_CHARS_PER_TOKEN


def _looks_dense(text: str, sample_limit: int = 20000) -> bool:
    """True when the text is symbol/whitespace-dense (code, logs, JSON)."""
    sample = text[:sample_limit]
    if not sample:
        return False
    symbolish = sum(1 for c in sample if not (c.isalpha() or c in " .,;:'\"!?-"))
    return symbolish / len(sample) > 0.25


def estimate_tokens(text: str) -> int:
    """Estimate the token count of *text*."""
    if not text:
        return 0
    cpt = _chars_per_token()
    if _looks_dense(text):
        cpt = min(cpt, _CODE_CHARS_PER_TOKEN)
    return math.ceil(len(text) / cpt)
