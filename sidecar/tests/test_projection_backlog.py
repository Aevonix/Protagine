"""A store upgraded with work its earlier release never ran drains that backlog at a small budget.

The live upgrade: a store from 1.9 carried pending claim-extraction and appraisal jobs over its old
conversations (its single worker loop started one model projection per source-vector commit, with
tens of thousands of sources queued for indexing). 1.10 gives every projection its own lane that
takes the next job at once, oldest first, so the whole history went through the brain: 140-190
small calls (an empty claim list, an empty appraisal) every ten minutes, continuously, and the
owner's chat decoded at half speed. New turns still go first and at once.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing, suppress
from types import SimpleNamespace

import pytest

import protagine
from protagine.beliefs.source_projection import SourceClaimProjection, run_source_claim_worker
from protagine.commitments.extract import CommitmentExtractor
from protagine.commitments.store import CommitmentStore
from protagine.self_model.appraisals import AppraisalStore
from protagine.turns import media
from protagine.turns.idempotency import TurnIdempotencyLedger

OWNER = 'owner-backlog'
DAY = 86400.0
EMPTY = {
    'source_claim_extraction': '{"claims": []}',
    'source_appraisal': json.dumps({'observations': [], 'incident_decisions': [], 'outcomes': [],
                                    'contact': {'their_valence': None, 'opt_out': False}}),
    'commitment_extract': '{"items": []}',
}


class Router:
    """A function-routed model that finds nothing; each call is counted by task and by source."""

    supports_function_routing = True

    def __init__(self):
        self.calls = []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        task = (context or {}).get('task')
        text = json.dumps(messages)
        self.calls.append((task, 'old turn' in text, 'fresh turn' in text))
        content = EMPTY.get(task, '{"action": "none"}')
        choice = SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=content))
        return SimpleNamespace(raw=SimpleNamespace(choices=[choice]), content=content, model_id='fixture-model')


def _turn(ledger, turn_id, text, *, contact, occurred_at=None):
    ledger.record_source(turn_id, contact_id=contact, session_id=contact + '-session', occurred_at=occurred_at,
                         messages=[{'role': 'user', 'content': text}, {'role': 'assistant', 'content': 'Noted.'}])


@pytest.fixture
def upgraded(tmp_path, monkeypatch):
    """Forty person turns from three days ago, recorded under the previous release, whose model
    projections never ran; then this release opens the store."""
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', OWNER)
    path = tmp_path / 'turn-idempotency.db'
    then = time.time() - 3 * DAY
    with monkeypatch.context() as past:
        past.setattr(time, 'time', lambda: then)
        old = TurnIdempotencyLedger(path)
        for number in range(40):
            _turn(old, f'old-{number}', f'old turn {number}: I moved into the flat on Elm Street.',
                  contact=f'contact-{number % 4}', occurred_at='2026-09-01T09:00:00+00:00')
    monkeypatch.setattr(protagine, '__version__', protagine.__version__ + '+next')
    return SimpleNamespace(path=path, ledger=TurnIdempotencyLedger(path), then=then)


async def test_an_upgraded_store_does_not_run_its_history_through_the_model(upgraded, monkeypatch):
    from protagine.turns import projection_backlog
    monkeypatch.setattr(projection_backlog, 'configured_per_hour', lambda: 12)
    monkeypatch.setattr(media.SourceMedia, 'recover_unowned_files', lambda _self: None)
    commitments = CommitmentStore(db_path=upgraded.path.parent / 'commitments.db')
    router = Router()
    _turn(upgraded.ledger, 'fresh', 'fresh turn: I will send Dana the lease by Friday.', contact=OWNER)
    worker = asyncio.create_task(run_source_claim_worker(
        upgraded.ledger, lambda: router, commitments_provider=lambda: commitments, vectors=False))
    wanted = {'source_claim_extraction', 'source_appraisal', 'commitment_extract'}
    try:
        started = time.monotonic()
        while time.monotonic() - started < 10 and not wanted <= {task for task, _, fresh in router.calls if fresh}:
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.0)       # long enough for the lanes to run dozens of jobs back to back
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    # The new turn is extracted, appraised and captured at once.
    assert wanted <= {task for task, _, fresh in router.calls if fresh}
    # Of the 120 historical projections, one starts (the budget's first slot). Before, the lanes ran
    # them back to back (64 in 1.5 s against this instant model) and the new turn waited behind them.
    old = [task for task, is_old, _ in router.calls if is_old]
    assert len(old) <= 1, old


def _clock(monkeypatch, start):
    now = [start]
    monkeypatch.setattr(time, 'time', lambda: now[0])
    return now


def test_claims_take_new_jobs_first_and_one_backlog_job_per_slot(upgraded, monkeypatch):
    from protagine.turns import projection_backlog
    monkeypatch.setattr(projection_backlog, 'configured_per_hour', lambda: 6)     # one every ten minutes
    _turn(upgraded.ledger, 'fresh', 'fresh turn: the lease is signed.', contact=OWNER)
    now = _clock(monkeypatch, time.time())
    projection = SourceClaimProjection(upgraded.ledger)

    def take(**kwargs):
        job = projection.claim_job(**kwargs)
        if job is not None:
            projection.finish_job(job, model='fixture-model')
        return job and job['turn_id']

    assert take() == 'fresh'
    assert take().startswith('old-')                        # nothing new is due: one backlog job
    assert take() is None                                   # the next waits for its slot
    now[0] += 599
    assert take() is None
    now[0] += 2
    assert take().startswith('old-')
    now[0] += 3600
    assert take(backlog=False) is None                      # a caller that wants only new work
    with closing(upgraded.ledger._connect()) as conn:
        state = projection_backlog.status(conn)
    assert state['admitted'] == 2 and state['waiting']['claim'] == 38


def test_a_zero_budget_leaves_the_backlog_pending(upgraded, monkeypatch):
    from protagine.turns import projection_backlog
    monkeypatch.setattr(projection_backlog, 'configured_per_hour', lambda: 0)
    now = _clock(monkeypatch, time.time())
    projection = SourceClaimProjection(upgraded.ledger)
    for _ in range(3):
        assert projection.claim_job() is None
        now[0] += 7 * DAY


def test_the_turns_of_the_day_before_an_upgrade_are_not_backlog(tmp_path, monkeypatch):
    path = tmp_path / 'turn-idempotency.db'
    then = time.time() - 3600
    with monkeypatch.context() as past:
        past.setattr(time, 'time', lambda: then)
        old = TurnIdempotencyLedger(path)
        for number in range(3):
            _turn(old, f'recent-{number}', f'old turn {number}: call the plumber.', contact='contact-1')
    monkeypatch.setattr(protagine, '__version__', protagine.__version__ + '+next')
    projection = SourceClaimProjection(TurnIdempotencyLedger(path))
    assert [projection.claim_job()['turn_id'] for _ in range(3)] == ['recent-0', 'recent-1', 'recent-2']


def test_appraisals_take_new_jobs_first_and_the_tick_waits_only_for_those(upgraded, monkeypatch):
    from protagine.turns import projection_backlog
    monkeypatch.setattr(projection_backlog, 'configured_per_hour', lambda: 12)
    _turn(upgraded.ledger, 'fresh', 'fresh turn: I prefer mornings.', contact=OWNER)
    store = AppraisalStore(upgraded.ledger, owner_id=OWNER)
    assert store.pending_jobs() == {'pending': 1, 'running': 0}          # the backlog is not waited for
    assert store._claim()['turn_id'] == 'fresh'
    assert store._claim()['turn_id'].startswith('old-')
    assert store._claim() is None


def test_capture_does_not_hold_a_persons_new_turn_behind_their_backlog(upgraded, monkeypatch):
    from protagine.turns import projection_backlog
    monkeypatch.setattr(projection_backlog, 'configured_per_hour', lambda: 12)
    _turn(upgraded.ledger, 'fresh', 'fresh turn: I will call Dana at five.', contact='contact-1')
    extractor = CommitmentExtractor(upgraded.ledger, lambda: None)
    assert extractor.oldest_unfinished_seconds() < 60                  # health reads new work only
    job = extractor._claim(0)
    assert job['turn_id'] == 'fresh'                                   # not behind contact-1's backlog
    # contact-1's backlog waits while their new turn is in hand; another person's may start.
    other = extractor._claim(0)
    assert other['turn_id'].startswith('old-') and other['turn_id'] != 'old-1'
    with closing(upgraded.ledger._connect()) as conn:
        contact = conn.execute('SELECT contact_id FROM turn_sources WHERE turn_id=?', (other['turn_id'],)).fetchone()[0]
    assert contact != 'contact-1'
    # The tick's drain lands new captures only.
    assert extractor._claim(0, backlog=False) is None


def test_media_takes_a_new_image_before_an_old_one(tmp_path, monkeypatch):
    path = tmp_path / 'turn-idempotency.db'
    then = time.time() - 3 * DAY
    with monkeypatch.context() as past:
        past.setattr(time, 'time', lambda: then)
        TurnIdempotencyLedger(path)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("INSERT INTO source_media(asset_hash,mime_type,size_bytes,width,height,enqueued_at) "
                         "VALUES ('old-image','image/png',1,1,1,?)", (then,))
    monkeypatch.setattr(protagine, '__version__', protagine.__version__ + '+next')
    ledger = TurnIdempotencyLedger(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO source_media(asset_hash,mime_type,size_bytes,width,height,enqueued_at) "
                     "VALUES ('new-image','image/png',1,1,1,?)", (time.time(),))
    from protagine.turns import projection_backlog
    monkeypatch.setattr(projection_backlog, 'configured_per_hour', lambda: 12)
    worker = media.SourceMedia(ledger)
    assert worker.claim_job()['asset_hash'] == 'new-image'
    assert worker.claim_job()['asset_hash'] == 'old-image'


def test_a_queue_from_before_the_column_dates_its_jobs_by_their_source(tmp_path):
    """A 1.9 store: ``source_claim_jobs`` and ``appraisal_runs`` without ``enqueued_at``."""
    path = tmp_path / 'turn-idempotency.db'
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('''CREATE TABLE turn_sources (turn_id TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL,
            contact_id TEXT NOT NULL, session_id TEXT NOT NULL, scope TEXT NOT NULL, messages_json TEXT NOT NULL,
            occurred_at TEXT, ingested_at TEXT NOT NULL)''')
        conn.execute("INSERT INTO turn_sources VALUES ('t-1','x','c','s','person','[]',NULL,'2026-09-20T10:00:00.000Z')")
        conn.execute('''CREATE TABLE source_claim_jobs (turn_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
            timezone TEXT NOT NULL DEFAULT 'UTC', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
            error TEXT, model TEXT, extraction_version TEXT, lease_token TEXT NOT NULL DEFAULT '')''')
        conn.execute("INSERT INTO source_claim_jobs(turn_id) VALUES ('t-1')")
        conn.execute('''CREATE TABLE appraisal_runs (turn_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, disposition TEXT, error TEXT)''')
        conn.execute("INSERT INTO appraisal_runs(turn_id) VALUES ('t-1')")
    TurnIdempotencyLedger(path)
    with closing(sqlite3.connect(path)) as conn:
        stamps = [conn.execute(f"SELECT enqueued_at FROM {table}").fetchone()[0]
                  for table in ('source_claim_jobs', 'appraisal_runs')]
    from datetime import datetime, timezone
    expected = datetime(2026, 9, 20, 10, tzinfo=timezone.utc).timestamp()
    assert stamps == [pytest.approx(expected, abs=0.01)] * 2
