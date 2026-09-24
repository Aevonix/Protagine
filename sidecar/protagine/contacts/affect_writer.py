"""The appraisal call's contact signal, written where it belongs (architecture 4.7 items 5 and 9).

The one background appraisal of a non-owner speaker's turn may carry the
speaker's apparent valence and an opt-out (``AppraisalStore(on_contact=...)``).
The valence becomes a contact-affect event linked to its canonical source, so
the social drive's ``trend`` read sees it and an erased source takes it along;
an opt-out lowers ``may_contact`` to ``never``, which it can only ever lower.
The owner's own turns never arrive here, and nothing here raises a permission.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

SOURCE = "appraisal"
OPT_OUT_REASON = "appraisal opt_out"


def _call(value: Any) -> Any:
    return value() if callable(value) else value


def _already_recorded(affect: Any, contact_id: str, turn_id: str) -> bool:
    """One affect event per appraised turn: a re-run of the same source adds nothing."""
    try:
        events = affect.list_events(contact_id=contact_id, limit=50)
    except Exception:
        return False
    return any(event.get("source") == SOURCE and (event.get("source_lineage") or {}).get("turn_id") == turn_id
               for event in events or [])


def _record(affect: Any, contact_id: str, turn_id: str, valence: float, signal: Dict[str, Any]) -> None:
    if _already_recorded(affect, contact_id, turn_id):
        return
    lineage, _ = affect.source_input(turn_id, contact_id)
    lineage = {**lineage, "source_version": signal.get("source_version")}
    affect.create_event(contact_id=contact_id, valence=valence, source=SOURCE, trigger="turn",
                        session_id=lineage.get("session_id"), timestamp=signal.get("occurred_at") or None,
                        source_lineage=lineage)


def _own_connection(affect: Any) -> Any:
    path = getattr(affect, "_db_path", None)
    if not path or str(path) == ":memory:":
        return None
    return type(affect)(str(path), source_ledger=getattr(affect, "_source_ledger", None))


def contact_signal_writer(affect_store_provider: Any, contacts_store_provider: Any, *,
                          owner_id_provider: Any) -> Callable[[Dict[str, Any]], Awaitable[None]]:
    """The ``on_contact`` callback: the providers are read at call time, so a store the host sets
    up later (or swaps in a test) is the one written."""

    async def write(signal: Dict[str, Any]) -> None:
        contact_id = str(signal.get("contact_id") or "")
        turn_id = str(signal.get("turn_id") or "")
        owner_id: Optional[str] = _call(owner_id_provider)
        if not contact_id or not turn_id or (owner_id and contact_id == owner_id):
            return
        valence = signal.get("their_valence")
        affect = _call(affect_store_provider)
        if valence is not None and affect is not None:
            try:
                _record(affect, contact_id, turn_id, float(valence), signal)
            except sqlite3.ProgrammingError:
                # The store's connection belongs to the thread that serves the host routes; a worker
                # on another thread writes the same database through a connection of its own.
                own = _own_connection(affect)
                if own is not None:
                    try:
                        _record(own, contact_id, turn_id, float(valence), signal)
                    except Exception as error:
                        logger.warning("contact affect not recorded for %s (%s)", turn_id, type(error).__name__)
                    finally:
                        own.close()
            except Exception as error:
                logger.warning("contact affect not recorded for %s (%s)", turn_id, type(error).__name__)
        if signal.get("opt_out") is True:
            contacts = _call(contacts_store_provider)
            lower = getattr(contacts, "lower_may_contact", None)
            if callable(lower):
                result = lower(contact_id, reason=OPT_OUT_REASON, source_ref=f"turn:{turn_id}")
                if inspect.isawaitable(result):
                    await result

    return write


__all__ = ["OPT_OUT_REASON", "SOURCE", "contact_signal_writer"]
