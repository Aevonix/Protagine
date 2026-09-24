"""The supervised-live rung, generically (H1.1).

The trust ladder has a structural catch-22: graduation ask_first ->
act_first requires a REAL (non-shadow) track record, but a domain that only
acts at act_first never produces one. This module is the one rung for ANY
domain, so every consumer shares one definition of "supervised" instead of
growing private variants.

The rung: at trust stage ask_first, a domain listed in
PROTAGINE_SUPERVISED_LIVE_DOMAINS (comma-separated, default EMPTY = rung off
everywhere) may perform REVERSIBLE operations only, journaled, with
outcomes recorded shadow=False — building exactly the track record
graduation needs. Destructive operations still require the full "live"
mode (act_first stage or explicit env override).

What counts as reversible is NOT the caller's judgment call: it is pinned
in ``REVERSIBLE_CONTRACT`` below, and ``reversible()`` fails CLOSED — an
unknown domain or an unlisted operation is non-reversible, full stop.
Trust-engine errors likewise degrade to the plain env mode (never upward).

No domain lists a reversible operation today: the belief engine and the world
model that did are gone (M8), so ``reversible()`` answers False everywhere
until a consumer registers its operations here.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, FrozenSet

logger = logging.getLogger(__name__)

# Domain -> operations that are reversible BY CONSTRUCTION (the operation
# preserves enough prior state to undo it, and never deletes or merges).
# Adding an entry here is a design review, not a config change: the
# operation's implementation must journal prior state and keep the old
# value recoverable.
REVERSIBLE_CONTRACT: Dict[str, FrozenSet[str]] = {}

_TRUTHY = ("1", "true", "yes")


def strict_trust_outcomes() -> bool:
    """H1.3: is trust-outcome integrity enforced? DEFAULT ON.

    Strict means a maintenance run only feeds the trust ladder what it
    earned: a run where a pass raised records a FAILURE; a run that
    actually mutated state records a SUCCESS; a no-op run records NOTHING.
    Without this, an engine that unconditionally logs "success" per run
    graduates to act_first on a streak of doing nothing.

    PROTAGINE_TRUST_STRICT_OUTCOMES=0 restores the legacy unconditional
    per-run success (a deliberate escape hatch, not a recommended mode).
    """
    return os.environ.get("PROTAGINE_TRUST_STRICT_OUTCOMES",
                          "1").strip().lower() in _TRUTHY


def supervised_domains() -> FrozenSet[str]:
    """Domains the generic flag enables the rung for (default: none)."""
    raw = os.environ.get("PROTAGINE_SUPERVISED_LIVE_DOMAINS", "")
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())


def supervised_enabled(domain: str) -> bool:
    """Is the supervised rung unlocked for ``domain``?"""
    domain = (domain or "").strip().lower()
    return bool(domain) and domain in supervised_domains()


def reversible(domain: str, op: str) -> bool:
    """Fail-closed reversibility check: True only for operations explicitly
    listed in REVERSIBLE_CONTRACT. Unknown domain or op => non-reversible."""
    ops = REVERSIBLE_CONTRACT.get((domain or "").strip().lower())
    if not ops:
        return False
    return (op or "").strip().lower() in ops


def effective_mode(domain: str, env_mode: str, trust: Any) -> str:
    """Env mode graduated by the trust engine, with the supervised rung.

    Resolution:
      * env "off"/"live" are the owner override — returned as-is.
      * no trust engine, or any trust error => plain env mode (degrade,
        never upgrade).
      * stage act_first => "live".
      * stage ask_first AND supervised_enabled(domain) => "supervised".
      * otherwise => "shadow".
    """
    if env_mode in ("off", "live"):
        return env_mode
    if trust is None:
        return env_mode
    try:
        stage = trust.stage(domain, default="shadow")
    except Exception:
        logger.debug("trust stage lookup failed for %s; using env mode",
                     domain, exc_info=True)
        return env_mode
    if stage == "act_first":
        return "live"
    if stage == "ask_first" and supervised_enabled(domain):
        return "supervised"
    return "shadow"
