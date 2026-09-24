"""The capture queue can say how long its oldest unfinished job has waited."""
from __future__ import annotations

import sqlite3

from protagine.commitments.extract import CommitmentExtractor
from protagine.turns.idempotency import TurnIdempotencyLedger


def _turn(ledger, turn_id, at):
    ledger.record_source(turn_id, contact_id="p-02", session_id="session-1", messages=[
        {"role": "user", "content": "I will send the report by five."}, {"role": "assistant", "content": "Noted."}],
        occurred_at="2026-01-01T00:00:00+00:00")


def test_oldest_unfinished_job_age_follows_the_queue(tmp_path, monkeypatch):
    import protagine.commitments.extract as extract
    now = [1_000_000.0]
    monkeypatch.setattr(extract.time, "time", lambda: now[0])
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    extractor = CommitmentExtractor(ledger, lambda: None, clock=lambda: now[0])
    assert extractor.oldest_unfinished_seconds() is None
    _turn(ledger, "turn-1", now[0])
    now[0] += 1800
    _turn(ledger, "turn-2", now[0])
    now[0] += 1800
    assert extractor.oldest_unfinished_seconds() == 3600.0
    with sqlite3.connect(tmp_path / "turn-idempotency.db") as conn:
        conn.execute("UPDATE commitment_runs SET status='complete' WHERE turn_id='turn-1'")
    assert extractor.oldest_unfinished_seconds() == 1800.0
    with sqlite3.connect(tmp_path / "turn-idempotency.db") as conn:
        conn.execute("UPDATE commitment_runs SET status='running' WHERE turn_id='turn-2'")
    assert extractor.oldest_unfinished_seconds() == 1800.0


def test_a_table_from_before_the_column_gains_it_and_reads_old_rows_as_unknown(tmp_path):
    path = tmp_path / "turn-idempotency.db"
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE commitment_runs (
            turn_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
            lease_token TEXT NOT NULL DEFAULT '', disposition TEXT, error TEXT)''')
        conn.execute("INSERT INTO commitment_runs(turn_id) VALUES ('old-turn')")
    ledger = TurnIdempotencyLedger(path)
    extractor = CommitmentExtractor(ledger, lambda: None, clock=lambda: 5_000_000.0)
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(commitment_runs)")}
        assert "enqueued_at" in columns
        assert conn.execute("SELECT status FROM commitment_runs WHERE turn_id='old-turn'").fetchone() == ("pending",)
    assert extractor.oldest_unfinished_seconds() is None      # an unknown age never flags
    assert extractor.pending_counts()["pending"] == 1
