"""The projection worker runs each job family on its own lane.

An upgraded store carried a backlog of 28,169 conversation sources waiting to
be vector-indexed, on a table where each indexing job took minutes. The worker
awaited each indexing job in line and started the next capture job only
between two of them, so the owner's capture jobs sat pending for 25 minutes
(late, never lost). These tests pin the lanes: capture, media and the other
projections do not wait for an indexing job, and indexing does not wait for a
model call.
"""

import asyncio
import json
import time
from contextlib import closing, suppress
from types import SimpleNamespace

import pytest

from protagine.beliefs.source_projection import SourceClaimProjection, run_source_claim_worker
from protagine.commitments.store import CommitmentStore
from protagine.self_model.appraisals import AppraisalStore
from protagine.self_model.judgments import SelfJudgments
from protagine.turns import media, source_vectors
from protagine.turns.idempotency import TurnIdempotencyLedger

OWNER = 'owner-c16'


class Router:
    """Answers the capture call with one item; counts every call by task."""

    supports_function_routing = True

    def __init__(self):
        self.tasks = []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.tasks.append((context or {}).get('task'))
        content = json.dumps([{'action': 'create', 'target': None, 'description': 'Send the signed lease',
                               'due_at': None, 'priority': 70, 'source_type': 'cognition', 'metadata': None}])
        choice = SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=content))
        return SimpleNamespace(raw=SimpleNamespace(choices=[choice]), content=content)


async def no_work(*_args, **_kwargs):
    return False


def capture_job(ledger, turn_id):
    with closing(ledger._connect()) as conn:
        row = conn.execute('SELECT status, disposition, attempts FROM commitment_runs WHERE turn_id=?',
                           (turn_id,)).fetchone()
    return dict(row) if row is not None else None


@pytest.fixture
def lanes(tmp_path, monkeypatch):
    """A worker whose every source-vector job takes longer than the test: a large indexing backlog
    on a never-compacted table. Judgments, appraisals, claims and media have nothing to do."""
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', OWNER)
    backlog = SimpleNamespace(started=asyncio.Event(), release=asyncio.Event(), calls=0, finished=0)

    class Backlog:
        def __init__(self, *_args):
            pass

        def backfill(self):
            pass

        async def process_one(self, batch_size=16):
            backlog.calls += 1
            backlog.started.set()
            await backlog.release.wait()
            backlog.finished += 1
            return True

    monkeypatch.setattr(source_vectors, 'SourceVectors', Backlog)
    for worker in (SelfJudgments, AppraisalStore, SourceClaimProjection, media.SourceMedia):
        monkeypatch.setattr(worker, 'process_one', no_work)
    monkeypatch.setattr(media.SourceMedia, 'recover_unowned_files', lambda _self: None)
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    commitments = CommitmentStore(db_path=tmp_path / 'commitments.db')
    return SimpleNamespace(ledger=ledger, commitments=commitments, backlog=backlog)


async def test_an_owner_capture_job_is_not_held_behind_a_slow_indexing_job(lanes):
    router = Router()
    worker = asyncio.create_task(run_source_claim_worker(
        lanes.ledger, lambda: router, commitments_provider=lambda: lanes.commitments))
    try:
        await asyncio.wait_for(lanes.backlog.started.wait(), 5)     # an indexing job is in hand
        await asyncio.sleep(0.2)                                      # and the capture lane found nothing yet
        lanes.ledger.record_source('promise', contact_id=OWNER, session_id='owner-1', messages=[
            {'role': 'user', 'content': 'Dana promised to send me the signed lease by Friday.'},
            {'role': 'assistant', 'content': 'Noted.'}])
        assert capture_job(lanes.ledger, 'promise')['status'] == 'pending'
        started = time.monotonic()
        while time.monotonic() - started < 10:
            if capture_job(lanes.ledger, 'promise')['status'] == 'complete':
                break
            await asyncio.sleep(0.05)
        waited = time.monotonic() - started
        assert capture_job(lanes.ledger, 'promise') == {'status': 'complete', 'disposition': 'recorded',
                                                         'attempts': 1}
        assert waited < 5                                             # an idle lane looks again within 2 s
        assert [row['description'] for row in lanes.commitments.get_pending_for_person(OWNER)] == [
            'Send the signed lease']
        # The indexing job is still in hand: capture neither waited for it nor displaced it.
        assert (lanes.backlog.calls, lanes.backlog.finished) == (1, 0)
        lanes.backlog.release.set()
        for _ in range(100):
            if lanes.backlog.finished >= 2:
                break
            await asyncio.sleep(0.02)
        assert lanes.backlog.finished >= 2                            # and it goes on once its job returns
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    assert worker.cancelled()


async def test_indexing_does_not_wait_for_a_model_call(lanes, monkeypatch):
    """The other direction: a capture call that hangs holds no indexing job back."""
    lanes.backlog.release.set()
    hung = asyncio.Event()

    class Hanging(Router):
        async def complete(self, messages, *, context=None, **_):
            hung.set()
            await asyncio.Event().wait()

    lanes.ledger.record_source('slow', contact_id=OWNER, session_id='owner-1', messages=[
        {'role': 'user', 'content': 'Remind me to call the landlord tomorrow.'}, {'role': 'assistant', 'content': 'Noted.'}])
    worker = asyncio.create_task(run_source_claim_worker(
        lanes.ledger, Hanging, commitments_provider=lambda: lanes.commitments))
    try:
        await asyncio.wait_for(hung.wait(), 5)
        calls = lanes.backlog.calls
        await asyncio.sleep(0.3)
        assert lanes.backlog.calls > calls + 2                       # indexing keeps going (50 ms between jobs)
        assert capture_job(lanes.ledger, 'slow')['status'] == 'running'
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


async def test_a_lane_that_fails_logs_and_keeps_its_pace(lanes, monkeypatch, caplog):
    """A lane's error is logged under its old wording and the lane tries again; the others go on."""
    lanes.backlog.release.set()
    failures = []

    async def failing(self, router):
        failures.append(time.monotonic())
        raise RuntimeError('endpoint gone')

    monkeypatch.setattr(media.SourceMedia, 'process_one', failing)
    worker = asyncio.create_task(run_source_claim_worker(
        lanes.ledger, Router, commitments_provider=lambda: lanes.commitments))
    try:
        for _ in range(200):
            if len(failures) >= 2:
                break
            await asyncio.sleep(0.05)
        assert len(failures) == 2
        assert 1.5 < failures[1] - failures[0] < 5                   # an idle pace after a failure
        assert lanes.backlog.calls > 10
        assert any('source claim worker deferred (RuntimeError)' in record.getMessage() for record in caplog.records)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker

