"""Deadline timezone handling and worker-outcome outbox drain robustness."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from protagine.task_queue import queue_manager
from protagine.task_queue.models import Job, JobStatus, WorkerCapabilities
from protagine.task_queue.queue_manager import TaskQueueManager


async def _manager(tmp_path):
    TaskQueueManager._instance = None
    return await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")


async def _set_stored_deadline(manager, job_id, text):
    """Write a raw deadline string the way an older release stored it."""
    await manager.queue._db.execute(
        "UPDATE jobs SET deadline = ? WHERE job_id = ?", (text, job_id),
    )
    await manager.queue._db.commit()


# ---------------------------------------------------------------------------
# (a) naive deadlines must not break claiming/sorting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_normalizes_naive_deadline_to_utc(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        naive = datetime(2099, 1, 1, 12, 0, 0)
        job = Job(deadline=naive)
        await manager.queue.post(job)
        stored = await manager.queue.get_job(job.job_id)
        assert stored.deadline == naive.replace(tzinfo=timezone.utc)
        assert stored.deadline.utcoffset() == timedelta(0)

        offset = datetime(2099, 1, 1, 8, 0, 0,
                          tzinfo=timezone(timedelta(hours=-4)))
        other = Job(deadline=offset)
        await manager.queue.post(other)
        stored = await manager.queue.get_job(other.job_id)
        assert stored.deadline == offset
        assert stored.deadline.utcoffset() == timedelta(0)

        # Claiming sorts by deadline urgency against an aware "now".
        claimed = await manager.queue.claim_job(
            "worker-a", WorkerCapabilities(node_id="worker-a"),
        )
        assert claimed is not None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_legacy_naive_deadline_row_does_not_break_claim(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        legacy = Job()
        await manager.queue.post(legacy)
        await _set_stored_deadline(manager, legacy.job_id, "2099-01-01T12:00:00")
        plain = Job()
        await manager.queue.post(plain)

        now = datetime.now(timezone.utc)
        queued = await manager.queue.get_queued_jobs_sorted(now)
        assert {j.job_id for j in queued} == {legacy.job_id, plain.job_id}
        stored = await manager.queue.get_job(legacy.job_id)
        assert stored.deadline == datetime(2099, 1, 1, 12, tzinfo=timezone.utc)

        claimed = await manager.queue.claim_job(
            "worker-a", WorkerCapabilities(node_id="worker-a"),
        )
        assert claimed is not None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_api_post_stores_naive_deadline_as_utc(tmp_path, monkeypatch):
    from protagine.api.routers import task_queue as router_module

    monkeypatch.setenv("PROTAGINE_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    monkeypatch.setattr(router_module, "_get_queue", lambda: manager)
    app = FastAPI()
    app.include_router(router_module.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            resp = await client.post("/v1/host/queue/jobs", json={
                "job_type": "custom",
                "deadline": "2099-01-01T12:00:00",
            })
        assert resp.status_code == 200, resp.text
        job = await manager.queue.get_job(resp.json()["job_id"])
        assert job.deadline == datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
        assert job.deadline.utcoffset() == timedelta(0)
        claimed = await manager.queue.claim_job(
            "worker-a", WorkerCapabilities(node_id="worker-a"),
        )
        assert claimed is not None and claimed.job_id == job.job_id
    finally:
        await manager.stop()


def test_router_parse_dt_returns_aware_utc():
    from protagine.api.routers.task_queue import _parse_dt

    assert _parse_dt("2099-01-01T12:00:00") == datetime(
        2099, 1, 1, 12, tzinfo=timezone.utc,
    )
    parsed = _parse_dt("2099-01-01T08:00:00-04:00")
    assert parsed == datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
    assert parsed.utcoffset() == timedelta(0)
    assert _parse_dt("2099-01-01T12:00:00Z").utcoffset() == timedelta(0)
    assert _parse_dt(None) is None
    assert _parse_dt("not a date") is None


# ---------------------------------------------------------------------------
# (b) deadline expiry must compare instants, not ISO text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expire_past_deadlines_compares_instants(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)

        # 09:30-04:00 == 13:30Z: still in the future, but sorts before the
        # UTC "now" as text.
        future_neg = Job()
        await manager.queue.post(future_neg)
        await _set_stored_deadline(
            manager, future_neg.job_id, "2026-09-22T09:30:00-04:00",
        )
        # 10:30+05:00 == 05:30Z: already past, but sorts after "now" as text.
        past_pos = Job()
        await manager.queue.post(past_pos)
        await _set_stored_deadline(
            manager, past_pos.job_id, "2026-09-22T10:30:00+05:00",
        )
        # Legacy naive rows are UTC.
        past_naive = Job()
        await manager.queue.post(past_naive)
        await _set_stored_deadline(
            manager, past_naive.job_id, "2026-09-22T09:00:00",
        )
        future_naive = Job()
        await manager.queue.post(future_naive)
        await _set_stored_deadline(
            manager, future_naive.job_id, "2026-09-22T11:00:00",
        )
        # A freshly posted offset deadline goes through normalisation.
        posted = Job(deadline=datetime(
            2026, 9, 22, 4, 0, tzinfo=timezone(timedelta(hours=-4)),
        ))
        await manager.queue.post(posted)

        assert await manager.queue.expire_past_deadlines(now) == 3

        async def status(job_id):
            return (await manager.queue.get_job(job_id)).status

        assert await status(future_neg.job_id) is JobStatus.QUEUED
        assert await status(future_naive.job_id) is JobStatus.QUEUED
        assert await status(past_pos.job_id) is JobStatus.FAILED
        assert await status(past_naive.job_id) is JobStatus.FAILED
        assert await status(posted.job_id) is JobStatus.FAILED
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# (c) poisoned outbox rows must neither starve the drain nor pin jobs
# ---------------------------------------------------------------------------

class _PoisonGovernor:
    """Delivers every outcome except those for the poisoned job ids."""

    def __init__(self, poisoned=()):
        self.poisoned = set(poisoned)
        self.events = []

    def audit_report(self, job, output):  # noqa: ARG002
        return {"verdict": "clean", "findings": []}

    def classify_completion_outcome(self, output, verdict):  # noqa: ARG002
        return "success", "verified_completion"

    async def record_outcome(self, job, *args, **kwargs):  # noqa: ARG002
        if job.job_id in self.poisoned:
            raise RuntimeError("worker action journal is unavailable")
        self.events.append(kwargs.get("event_id"))


async def _complete(manager, job, worker="worker-a"):
    await manager.queue.post(job)
    claimed = await manager.queue.claim_job(
        worker, WorkerCapabilities(node_id=worker, max_concurrent=4),
    )
    assert claimed is not None and claimed.job_id == job.job_id
    assert await manager.queue.start_job(
        job.job_id, worker, claimed.claim_attempt_id,
    )
    result = await manager.queue.complete_job(
        job.job_id, worker, {"status": "verified"},
        claim_attempt_id=claimed.claim_attempt_id,
    )
    assert result["transitioned"] is True
    return claimed


@pytest.mark.asyncio
async def test_drain_delivers_fresh_rows_before_repeat_failures(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_WORKERS_MODE", "off")
    manager = await _manager(tmp_path)
    try:
        poisoned = Job()
        healthy = Job()
        await _complete(manager, poisoned)
        await _complete(manager, healthy)
        pending = await manager.queue.pending_worker_outcomes()
        by_job = {row["job_id"]: row["event_id"] for row in pending}
        assert set(by_job) == {poisoned.job_id, healthy.job_id}
        # An earlier drain already failed on the older row.
        await manager.queue._mark_worker_outcome(
            by_job[poisoned.job_id], delivered=False, error="journal down",
        )

        governor = _PoisonGovernor(poisoned={poisoned.job_id})
        manager.queue.configure_governance(governor)
        assert await manager.queue.drain_worker_outcomes(limit=1) == 1
        assert governor.events == [by_job[healthy.job_id]]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_poisoned_outcome_row_is_retired_after_attempt_cap(
        tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PROTAGINE_WORKERS_MODE", "off")
    monkeypatch.setattr(
        queue_manager, "WORKER_OUTCOME_MAX_DELIVERY_ATTEMPTS", 3,
    )
    manager = await _manager(tmp_path)
    try:
        old_job = Job(posted_at=datetime.now(timezone.utc) - timedelta(days=60))
        await _complete(manager, old_job)
        governor = _PoisonGovernor(poisoned={old_job.job_id})
        manager.queue.configure_governance(governor)

        with caplog.at_level(logging.WARNING, logger=queue_manager.__name__):
            for _ in range(2):
                assert await manager.queue.drain_worker_outcomes() == 0
            pending = await manager.queue.pending_worker_outcomes()
            assert len(pending) == 1 and pending[0]["delivery_attempts"] == 2
            assert await manager.queue.prune_old_jobs() == 0

            assert await manager.queue.drain_worker_outcomes() == 0
        assert await manager.queue.pending_worker_outcomes() == []
        rows = await manager.queue.pending_worker_outcomes(include_delivered=True)
        assert len(rows) == 1
        assert rows[0]["state"] == "dead"
        assert rows[0]["delivery_attempts"] == 3
        assert "journal is unavailable" in rows[0]["last_error"]
        assert any("retired" in rec.getMessage() for rec in caplog.records)

        # A dead row is left alone by later drains and no longer pins its job.
        assert await manager.queue.drain_worker_outcomes() == 0
        rows = await manager.queue.pending_worker_outcomes(include_delivered=True)
        assert rows[0]["delivery_attempts"] == 3
        assert await manager.queue.prune_old_jobs() == 1
        assert await manager.queue.get_job(old_job.job_id) is None
    finally:
        await manager.stop()
