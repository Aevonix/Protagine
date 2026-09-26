"""One Mind factory and one router list for every process that serves a mind (integration map X4d).

The sidecar (``server.py``) and the benchmark's mind arm (``native_memory_worker.serve_mind``)
used to build their Mind separately, and each milestone had to add its wiring to both; the
benchmark Mind had already drifted (M5 audit M1). Both now call ``build_mind`` with the host
router module whose stores they opened, and mount ``mind_routers()``: what a faculty reads is
wired here once. Only process choices stay with the caller (``PROCESS_OPTIONS``): the clock,
the timer interval, whether the Mind takes its own backups, how a setting is persisted, the
tick heartbeat and the identity's declared interests.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Mapping

from .tick import Mind

logger = logging.getLogger(__name__)

# The keyword arguments a caller may add; everything else comes from ``build_mind`` itself.
PROCESS_OPTIONS = frozenset({"clock", "interval", "backups", "persist", "heartbeat", "interests"})


def mind_routers() -> List[Any]:
    """The routes a mind serves next to the host routes: its own (``/v1/mind``, the narrative and the
    nightly consolidation included), the people routes an owner's ``protagine_people`` reaches and the
    opinion routes (``/v1/mind/opinions``) ``protagine_self opinions|why|withdraw|reconsider`` reaches."""
    from protagine.api.routers import mind as mind_router
    from protagine.api.routers import opinions
    from protagine.api.routers import people
    return [mind_router.router, people.router, opinions.router]


def mind_kwargs(host: Any, *, config: Mapping[str, Any] | None, store: Any, state_dir: Any, ledger: Any,
                owner_id: str | None, feedback: Any = None, expectations: Any = None) -> dict:
    """The Mind's wiring over the stores ``host`` (the host router module) holds: the commitment store and
    its reply waits, contacts, the comms ledger, the contacts' affect, the recipient packet and the
    contact-scoped claims (people), the owner's interest appraisals, the capture jobs a tick drains first
    (sharing the source worker's ledger, so a job the worker holds is waited for, never run twice) and
    the model router."""
    from protagine.commitments.extract import CommitmentExtractor, contact_aliases
    from protagine.initiatives.temporal_followup import TemporalFollowups
    commitments = getattr(host, "_commitment_store", None)
    appraisals = None
    if owner_id and ledger is not None:
        try:
            from protagine.self_model.appraisals import AppraisalStore
            appraisals = AppraisalStore(ledger, owner_id=owner_id)
        except Exception:
            logger.debug("appraisal store unavailable to the mind", exc_info=True)
    return dict(
        config=dict(config or {}), store=store, state_dir=state_dir, owner_id=owner_id or None,
        commitments=commitments,
        followups=TemporalFollowups(commitments) if commitments is not None else None,
        feedback=feedback, expectations=expectations,
        contacts=getattr(host, "_contacts_store", None), ledger=ledger,
        router=getattr(host, "_llm_router", None), appraisals=appraisals,
        capture=CommitmentExtractor(ledger, lambda: getattr(host, "_commitment_store", None),
                                    aliases=contact_aliases(lambda: getattr(host, "_contacts_store", None))),
        comms=getattr(host, "_comms_log", None), contact_affect=getattr(host, "_affect_store", None),
        packet_for=getattr(host, "assemble_packet", None), claims_for=getattr(host, "claims_for", None),
        timezone_name=os.environ.get("PROTAGINE_AGENT_TIMEZONE") or os.environ.get("PROTAGINE_TIMEZONE") or None,
    )


def build_mind(host: Any, *, config: Mapping[str, Any] | None, store: Any, state_dir: Any, ledger: Any,
               owner_id: str | None, feedback: Any = None, expectations: Any = None, **process: Any) -> Mind:
    """The one Mind constructor (see the module docstring); ``process`` takes only ``PROCESS_OPTIONS``."""
    unknown = set(process) - PROCESS_OPTIONS
    if unknown:
        raise TypeError(f"build_mind: {sorted(unknown)} are wired here, not by the caller")
    return Mind(**mind_kwargs(host, config=config, store=store, state_dir=state_dir, ledger=ledger,
                              owner_id=owner_id, feedback=feedback, expectations=expectations), **process)


__all__ = ["PROCESS_OPTIONS", "build_mind", "mind_kwargs", "mind_routers"]
