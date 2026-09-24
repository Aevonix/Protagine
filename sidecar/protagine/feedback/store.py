"""TypeFeedbackStore -- outcome-driven priority feedback.

Records how the owner responded to each class of proactive work (intentions,
proposals, reach-outs) and turns that into a per-type priority multiplier:
classes the owner acts on are boosted; classes he dismisses or ignores decay.

Every contribution is keyed by ``(itype, source)``: the intention or initiative
the outcome is about. Recording the same outcome for the same source again
changes nothing, and a different outcome for the same source replaces the
earlier contribution, so a corrected verdict counts once, as corrected. The
multiplier is derived from the current contributions, replayed in the order
they were first recorded and clamped at every step, so a replacement is exact
and a type never fully dies or runs away.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Multiplicative nudges per outcome, with clamps so a type never fully dies or
# runs away.
_NUDGE = {"actioned": 1.12, "acted": 1.12, "dismissed": 0.85, "ignored": 0.9,
          "acknowledged": 1.0, "snoozed": 0.97}
_MIN, _MAX = 0.5, 1.5
# The source of the one baseline contribution a type recorded before
# contributions were keyed carries over: its compounded multiplier, as learned.
LEGACY_SOURCE = "legacy"


def _column(outcome: str) -> str:
    nudge = _NUDGE.get(outcome, 1.0)
    return ("actioned" if nudge > 1.0 else
            "dismissed" if outcome == "dismissed" else
            "ignored" if outcome == "ignored" else "other")


class TypeFeedbackStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS type_feedback (
                    itype TEXT PRIMARY KEY, multiplier REAL DEFAULT 1.0,
                    actioned INTEGER DEFAULT 0, dismissed INTEGER DEFAULT 0,
                    ignored INTEGER DEFAULT 0, other INTEGER DEFAULT 0,
                    updated_at REAL
                )""")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS type_feedback_contributions (
                    itype TEXT NOT NULL, source TEXT NOT NULL, outcome TEXT NOT NULL,
                    nudge REAL NOT NULL, recorded_at REAL NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY (itype, source)
                )""")
            # A type recorded before contributions were keyed holds one compounded
            # multiplier and its counts. The multiplier becomes that type's single
            # baseline contribution, so the learned state carries over and the
            # sources that follow key properly; the counts stay on the row.
            now = time.time()
            self._conn.execute(
                """INSERT INTO type_feedback_contributions
                       (itype, source, outcome, nudge, recorded_at, updated_at)
                   SELECT itype, ?, ?, COALESCE(multiplier, 1.0), COALESCE(updated_at, ?),
                          COALESCE(updated_at, ?)
                   FROM type_feedback AS t
                   WHERE NOT EXISTS (SELECT 1 FROM type_feedback_contributions AS c
                                     WHERE c.itype = t.itype)""",
                (LEGACY_SOURCE, LEGACY_SOURCE, now, now))
            self._conn.commit()

    def record(self, itype: str, outcome: str, *, source: str) -> float:
        """Record ``source``'s outcome for a type; return the multiplier derived from it.

        ``source`` is the intention or initiative the outcome is about. The same
        outcome again is a no-op; a different one replaces the earlier contribution.
        """
        itype = (itype or "unknown").strip().lower()
        outcome = (outcome or "").strip().lower()
        source = str(source or "").strip()
        if not source:
            raise ValueError("feedback needs the intention or initiative it is about")
        col = _column(outcome)
        now = time.time()
        with self._lock:
            prior = self._conn.execute(
                "SELECT outcome FROM type_feedback_contributions WHERE itype=? AND source=?",
                (itype, source)).fetchone()
            if prior is not None and prior["outcome"] == outcome:
                return self.multiplier(itype)
            if prior is None:
                self._conn.execute(
                    """INSERT INTO type_feedback_contributions
                           (itype, source, outcome, nudge, recorded_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (itype, source, outcome, _NUDGE.get(outcome, 1.0), now, now))
                counts = f"{col}={col}+1, "
            else:
                self._conn.execute(
                    """UPDATE type_feedback_contributions SET outcome=?, nudge=?, updated_at=?
                       WHERE itype=? AND source=?""",
                    (outcome, _NUDGE.get(outcome, 1.0), now, itype, source))
                was = _column(prior["outcome"])
                counts = f"{col}={col}+1, {was}={was}-1, " if was != col else ""
            mult = self._derive(itype)
            self._conn.execute(
                "INSERT OR IGNORE INTO type_feedback (itype, multiplier, updated_at) VALUES (?, 1.0, ?)",
                (itype, now))
            self._conn.execute(
                f"UPDATE type_feedback SET {counts}multiplier=?, updated_at=? WHERE itype=?",
                (mult, now, itype))
            self._conn.commit()
        logger.info("feedback[%s] %s: %s -> multiplier=%.3f", itype, source, outcome, mult)
        return mult

    def _derive(self, itype: str) -> float:
        """Replay the type's contributions in the order first recorded, clamped at each step."""
        value = 1.0
        for row in self._conn.execute(
                "SELECT nudge FROM type_feedback_contributions WHERE itype=? ORDER BY recorded_at, rowid",
                (itype,)):
            value = max(_MIN, min(_MAX, value * float(row["nudge"])))
        return value

    def multiplier(self, itype: str) -> float:
        itype = (itype or "unknown").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT multiplier FROM type_feedback WHERE itype=?", (itype,)).fetchone()
        return float(row["multiplier"]) if row else 1.0

    def snapshot(self) -> List[Dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM type_feedback ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]
