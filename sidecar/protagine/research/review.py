"""Content review of a research artifact: credential/PII patterns and injection markers.

Two deterministic scans over the artifact text. Neither is a recipient or
trust decision; a research artifact has no recipient. ``scan_pii`` returns
the name of the first matching pattern with a redacted excerpt, and
``scan_injection`` the reason the text looks like an instruction injection.
Both return ``None`` when the text is clean.
"""

from __future__ import annotations

import base64
import re
from typing import Optional

from protagine.redact import (
    _AUTH_HEADER_RE,
    _ENV_ASSIGN_RE,
    _JSON_FIELD_RE,
    _PREFIX_RE,
    _PRIVATE_KEY_RE,
    _SIGNAL_PHONE_RE,
)

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_CARD_CONTEXT_RE = re.compile(
    r"\b(card|cc|credit|debit|visa|mastercard|amex|payment|pan|cvv|cvc)\b", re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}(?:[A-Z0-9]?){0,16}\b")

_PII_PATTERNS = [
    ("ssn", _SSN_RE),
    ("credit_card", _CARD_RE),
    ("email", _EMAIL_RE),
    ("iban", _IBAN_RE),
    ("api_key", _PREFIX_RE),
    ("env_secret", _ENV_ASSIGN_RE),
    ("json_secret", _JSON_FIELD_RE),
    ("auth_header", _AUTH_HEADER_RE),
    ("private_key", _PRIVATE_KEY_RE),
    ("phone", _SIGNAL_PHONE_RE),
]

_INJECTION_PATTERNS = [
    re.compile(
        r"(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+(instructions|directives|context)",
        re.IGNORECASE,
    ),
    re.compile(r"you\s+are\s+(now|actually|secretly|really)\s+[A-Z]", re.IGNORECASE),
    re.compile(
        r"(act|behave|respond)\s+as\s+(if\s+)?(you\s+are\s+|a\s+)?(DAN|GPT|jailbreak|unrestricted)",
        re.IGNORECASE,
    ),
    re.compile(r"(system\s+prompt|system\s+message|instructions)\s*:\s*", re.IGNORECASE),
    re.compile(r"<\s*(system|admin|root|operator)\s*>", re.IGNORECASE),
    re.compile(r"send\s+(this|it|the\s+following)\s+to\s+\S+@\S+", re.IGNORECASE),
    re.compile(r"forward\s+(this\s+message\s+)?to\s+", re.IGNORECASE),
    re.compile(r"reply\s+to\s+\S+\s+instead", re.IGNORECASE),
    re.compile(
        r"(print|show|display|output|repeat|reveal)\s+(your\s+)?(system\s+prompt|instructions|memory|context)",
        re.IGNORECASE,
    ),
    re.compile(r"what\s+(are|were)\s+your\s+(original\s+)?instructions", re.IGNORECASE),
]
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{32,}={0,2}")
_HOMOGLYPH_RE = re.compile(r"[Ѐ-ӿͰ-Ͽ＀-￯]")
_INVISIBLE_CHARS = frozenset("​‌‍﻿­⁠᠎")


def _luhn_valid(number: str) -> bool:
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13:
        return False
    total = sum(digits[-1::-2])
    for d in digits[-2::-2]:
        total += sum(divmod(d * 2, 10))
    return total % 10 == 0


def _redact(raw: str) -> str:
    return "***" if len(raw) <= 4 else raw[:2] + "***" + raw[-2:]


def scan_pii(text: str) -> Optional[tuple[str, str]]:
    """The first credential or personal-data pattern in *text*, as (name, redacted excerpt)."""
    for name, pattern in _PII_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        if name == "credit_card":
            digits = re.sub(r"[ -]", "", match.group())
            window = text[max(0, match.start() - 50):match.end() + 50]
            if not _luhn_valid(digits) or not _CARD_CONTEXT_RE.search(window):
                continue
        return name, _redact(match.group())
    return None


def _decoded_injection(text: str) -> bool:
    for match in _BASE64_RE.finditer(text):
        stripped = match.group().rstrip("=")
        try:
            decoded = base64.b64decode(stripped + "=" * ((4 - len(stripped) % 4) % 4)).decode("latin-1")
        except Exception:
            continue
        if any(pattern.search(decoded) for pattern in _INJECTION_PATTERNS):
            return True
    return False


def scan_injection(text: str) -> Optional[str]:
    """Why *text* reads as an instruction injection, or None."""
    text = text or ""
    if not text:
        return None
    if any(pattern.search(text) for pattern in _INJECTION_PATTERNS):
        return "injection pattern detected"
    if _decoded_injection(text):
        return "base64-encoded injection content detected"
    homoglyphs = len(_HOMOGLYPH_RE.findall(text))
    if homoglyphs and (homoglyphs / len(text) > 0.02 or homoglyphs >= 2):
        return "homoglyph substitution detected"
    if any(c in _INVISIBLE_CHARS for c in text):
        return "invisible characters detected"
    return None


__all__ = ["scan_injection", "scan_pii"]
