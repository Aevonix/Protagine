"""Durability and load properties of the turn-ingestion reservation ledger."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from protagine.turns import (
    ReservationOutcome,
    TurnIdempotencyLedger,
    canonical_turn_digest,
)


def _reserve_then_die(path, turn_id, digest):
    """A first attempt whose process is hard-killed right after reserving.

    Nothing ever calls complete() or mark_ambiguous() for this row.
    """
    ledger = TurnIdempotencyLedger(path)
    assert ledger.reserve(turn_id, digest).outcome == ReservationOutcome.CREATED
    del ledger


def _backdate(path, turn_id, state, *, minutes):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE turn_ingestion SET state=?, created_at="
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) WHERE turn_id=?",
            (state, f"-{minutes} minutes", turn_id),
        )


def test_ten_thousand_identical_submissions_have_one_creation(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / "turns.db")
    digest = canonical_turn_digest({"turn_id": "turn-load", "content": "same"})

    first = ledger.reserve("turn-load", digest)
    assert first.outcome == ReservationOutcome.CREATED
    ledger.complete(
        "turn-load",
        digest,
        {"accepted": True, "continuity_updated": True, "skipped_reason": None},
    )

    outcomes = [ledger.reserve("turn-load", digest).outcome for _ in range(9_999)]
    assert set(outcomes) == {ReservationOutcome.REPLAYED}


def test_concurrent_reservations_choose_exactly_one_creator(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / "turns.db")
    digest = canonical_turn_digest({"turn_id": "turn-race", "content": "same"})

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(
            lambda _: ledger.reserve("turn-race", digest).outcome,
            range(128),
        ))

    assert outcomes.count(ReservationOutcome.CREATED) == 1
    assert set(outcomes) <= {
        ReservationOutcome.CREATED,
        ReservationOutcome.IN_PROGRESS,
    }


def test_reservation_survives_new_ledger_instance_and_detects_conflict(tmp_path):
    path = tmp_path / "turns.db"
    first = TurnIdempotencyLedger(path)
    digest = canonical_turn_digest({"content": "first"})
    first.reserve("turn-durable", digest)
    first.complete(
        "turn-durable",
        digest,
        {"accepted": True, "continuity_updated": False},
    )

    reopened = TurnIdempotencyLedger(path)
    replay = reopened.reserve("turn-durable", digest)
    conflict = reopened.reserve(
        "turn-durable", canonical_turn_digest({"content": "changed"})
    )

    assert replay.outcome == ReservationOutcome.REPLAYED
    assert replay.response["accepted"] is True
    assert conflict.outcome == ReservationOutcome.CONFLICT


def test_hard_killed_reservation_is_reclaimed_by_identical_retry_after_lease(tmp_path):
    path = tmp_path / "turns.db"
    digest = canonical_turn_digest({"content": "orphaned"})
    _reserve_then_die(path, "turn-orphan", digest)

    # Inside the lease a retry still truthfully reports the first attempt.
    restarted = TurnIdempotencyLedger(path)
    assert restarted.reserve("turn-orphan", digest).outcome == ReservationOutcome.IN_PROGRESS

    # Once the lease lapses the retry takes the reservation over and finishes it.
    time.sleep(0.3)
    lapsed = TurnIdempotencyLedger(path, processing_lease_seconds=0.2)
    assert lapsed.reserve("turn-orphan", digest).outcome == ReservationOutcome.CREATED
    lapsed.complete("turn-orphan", digest, {"accepted": True, "continuity_updated": True})

    replay = restarted.reserve("turn-orphan", digest)
    assert replay.outcome == ReservationOutcome.REPLAYED
    assert replay.response == {"accepted": True, "continuity_updated": True}
    assert restarted.get("turn-orphan")["state"] == "completed"


def test_reclaim_renews_the_lease_so_a_second_retry_waits(tmp_path):
    path = tmp_path / "turns.db"
    digest = canonical_turn_digest({"content": "orphaned"})
    _reserve_then_die(path, "turn-renew", digest)
    _backdate(path, "turn-renew", "processing", minutes=10)

    ledger = TurnIdempotencyLedger(path)
    assert ledger.reserve("turn-renew", digest).outcome == ReservationOutcome.CREATED
    assert ledger.reserve("turn-renew", digest).outcome == ReservationOutcome.IN_PROGRESS


def test_concurrent_retries_on_lapsed_reservation_choose_exactly_one_reclaimer(tmp_path):
    path = tmp_path / "turns.db"
    digest = canonical_turn_digest({"content": "orphaned"})
    _reserve_then_die(path, "turn-lapsed-race", digest)
    _backdate(path, "turn-lapsed-race", "processing", minutes=10)

    ledger = TurnIdempotencyLedger(path)
    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(
            lambda _: ledger.reserve("turn-lapsed-race", digest).outcome,
            range(64),
        ))

    assert outcomes.count(ReservationOutcome.CREATED) == 1
    assert set(outcomes) == {ReservationOutcome.CREATED, ReservationOutcome.IN_PROGRESS}


def test_lapsed_reservation_keeps_its_content_binding(tmp_path):
    path = tmp_path / "turns.db"
    digest = canonical_turn_digest({"content": "orphaned"})
    _reserve_then_die(path, "turn-bound", digest)
    _backdate(path, "turn-bound", "processing", minutes=10)

    ledger = TurnIdempotencyLedger(path)
    changed = ledger.reserve("turn-bound", canonical_turn_digest({"content": "changed"}))
    assert changed.outcome == ReservationOutcome.CONFLICT
    assert ledger.get("turn-bound")["content_sha256"] == digest
    assert ledger.reserve("turn-bound", digest).outcome == ReservationOutcome.CREATED


def test_lease_never_replays_completed_or_ambiguous_rows(tmp_path):
    path = tmp_path / "turns.db"
    ledger = TurnIdempotencyLedger(path)
    digest = canonical_turn_digest({"content": "settled"})

    ledger.reserve("turn-done", digest)
    ledger.complete("turn-done", digest, {"accepted": True, "continuity_updated": False})
    ledger.reserve("turn-failed", digest)
    ledger.mark_ambiguous("turn-failed", digest, RuntimeError("creator crashed"))
    _backdate(path, "turn-done", "completed", minutes=10)
    _backdate(path, "turn-failed", "ambiguous", minutes=10)

    assert ledger.reserve("turn-done", digest).outcome == ReservationOutcome.REPLAYED
    assert ledger.reserve("turn-failed", digest).outcome == ReservationOutcome.AMBIGUOUS
    assert ledger.get("turn-failed")["error"].startswith("RuntimeError")
