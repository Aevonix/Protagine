"""Tests for outcome-driven per-type priority feedback.

Every contribution is keyed by the intention or initiative it is about: the
first outcome counts once, an exact repeat changes nothing, a corrected
outcome replaces the earlier one, independent sources both count, and an
install from before contributions were keyed keeps its learned multiplier.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from protagine.feedback import TypeFeedbackStore
from protagine.feedback.store import LEGACY_SOURCE


def _counts(store: TypeFeedbackStore, itype: str) -> tuple[int, int, int, int]:
    row = {r["itype"]: r for r in store.snapshot()}[itype]
    return row["actioned"], row["dismissed"], row["ignored"], row["other"]


def test_actioned_boosts_dismissed_decays():
    s = TypeFeedbackStore(db_path=None)
    assert s.multiplier("research") == 1.0
    m1 = s.record("research", "actioned", source="i-1")
    assert m1 > 1.0
    m2 = s.record("relationship", "dismissed", source="i-2")
    assert m2 < 1.0
    assert s.multiplier("research") > 1.0
    assert s.multiplier("relationship") < 1.0


def test_multiplier_is_clamped():
    s = TypeFeedbackStore(db_path=None)
    for n in range(50):
        s.record("t", "dismissed", source=f"d-{n}")
    assert s.multiplier("t") >= 0.5   # floor
    for n in range(80):
        s.record("t", "actioned", source=f"a-{n}")
    assert s.multiplier("t") <= 1.5   # ceiling


def test_snapshot_counts_outcomes():
    s = TypeFeedbackStore(db_path=None)
    s.record("research", "actioned", source="i-1")
    s.record("research", "dismissed", source="i-2")
    assert _counts(s, "research") == (1, 1, 0, 0)


def test_first_rating_counts_once():
    s = TypeFeedbackStore(db_path=None)
    assert s.record("research:curiosity", "actioned", source="i-1") == pytest.approx(1.12)
    assert s.multiplier("research:curiosity") == pytest.approx(1.12)
    assert _counts(s, "research:curiosity") == (1, 0, 0, 0)


def test_exact_duplicate_changes_nothing():
    s = TypeFeedbackStore(db_path=None)
    s.record("research:curiosity", "actioned", source="i-1")
    assert s.record("research:curiosity", "actioned", source="i-1") == pytest.approx(1.12)
    assert s.multiplier("research:curiosity") == pytest.approx(1.12)
    assert _counts(s, "research:curiosity") == (1, 0, 0, 0)


def test_corrected_rating_replaces_the_earlier_contribution():
    s = TypeFeedbackStore(db_path=None)
    s.record("research:curiosity", "actioned", source="i-1")                                  # useful
    assert s.record("research:curiosity", "dismissed", source="i-1") == pytest.approx(0.85)  # then wrong
    assert _counts(s, "research:curiosity") == (0, 1, 0, 0)
    assert s.record("research:curiosity", "actioned", source="i-1") == pytest.approx(1.12)   # and back
    assert _counts(s, "research:curiosity") == (1, 0, 0, 0)


def test_independent_intentions_both_count():
    s = TypeFeedbackStore(db_path=None)
    s.record("research:curiosity", "actioned", source="i-1")
    assert s.record("research:curiosity", "actioned", source="i-2") == pytest.approx(1.12 * 1.12)
    assert _counts(s, "research:curiosity") == (2, 0, 0, 0)


def test_replacement_is_exact_among_other_contributions():
    s = TypeFeedbackStore(db_path=None)
    s.record("t", "dismissed", source="i-1")
    s.record("t", "actioned", source="i-2")
    s.record("t", "ignored", source="i-3")
    s.record("t", "actioned", source="i-1")                       # the correction keeps its place
    assert s.multiplier("t") == pytest.approx(1.12 * 1.12 * 0.9)
    assert _counts(s, "t") == (2, 0, 1, 0)


def test_source_is_required():
    s = TypeFeedbackStore(db_path=None)
    with pytest.raises(ValueError):
        s.record("t", "actioned", source="")
    assert s.snapshot() == []


def test_existing_compounded_row_migrates_in_place(tmp_path):
    path = tmp_path / "protagine-feedback.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE type_feedback (
        itype TEXT PRIMARY KEY, multiplier REAL DEFAULT 1.0,
        actioned INTEGER DEFAULT 0, dismissed INTEGER DEFAULT 0,
        ignored INTEGER DEFAULT 0, other INTEGER DEFAULT 0, updated_at REAL)""")
    conn.execute("INSERT INTO type_feedback VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("research:curiosity", 1.2544, 2, 0, 0, 0, time.time() - 3600))
    conn.execute("INSERT INTO type_feedback VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("reach_out:p-02", 0.5, 0, 9, 0, 0, time.time() - 60))
    conn.commit()
    conn.close()

    s = TypeFeedbackStore(db_path=str(path))
    assert s.multiplier("research:curiosity") == pytest.approx(1.2544)
    assert s.multiplier("reach_out:p-02") == pytest.approx(0.5)
    rows = s._conn.execute(
        "SELECT itype, source, nudge FROM type_feedback_contributions ORDER BY itype").fetchall()
    assert [tuple(r) for r in rows] == [("reach_out:p-02", LEGACY_SOURCE, 0.5),
                                        ("research:curiosity", LEGACY_SOURCE, 1.2544)]
    # The baseline continues as learned, keyed sources follow it, and the counts it came with survive.
    assert s.record("research:curiosity", "actioned", source="i-3") == pytest.approx(1.2544 * 1.12)
    assert s.record("research:curiosity", "actioned", source="i-3") == pytest.approx(1.2544 * 1.12)
    assert _counts(s, "research:curiosity") == (3, 0, 0, 0)
    assert _counts(s, "reach_out:p-02") == (0, 9, 0, 0)
    # Reopening does not migrate twice.
    again = TypeFeedbackStore(db_path=str(path))
    assert again.multiplier("research:curiosity") == pytest.approx(1.2544 * 1.12)
    assert again._conn.execute("SELECT COUNT(*) FROM type_feedback_contributions WHERE source=?",
                               (LEGACY_SOURCE,)).fetchone()[0] == 2
