"""The queue's approval hold is decided directly by the owner: no ledger, no grants.

An effectful agent action enters BLOCKED awaiting the owner; the approve
route records the authenticated actor in the job's tags and releases it, the
reject route cancels it, and an exact retry with the same decision_id reads
the recorded decision back.
"""

from __future__ import annotations

import pytest

from protagine.api.routers import task_queue as queue_router
from protagine.task_queue.models import Job, JobStatus, JobType
from protagine.task_queue.queue_manager import TaskQueueManager


@pytest.fixture
async def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    manager = TaskQueueManager(db_path=str(tmp_path / "queue.db"))
    await manager.queue.start()
    TaskQueueManager._instance = manager
    try:
        yield manager
    finally:
        await manager.queue.stop()
        TaskQueueManager._instance = None


async def test_effect_job_is_held_then_approved_directly(manager):
    job = Job(job_type=JobType.AGENT_ACTION,
              payload={"action_hint": "agent_deliver_message", "risk": "outbound", "description": "say hi"})
    await manager.queue.post(job)
    held = await manager.queue.get_job(job.job_id)
    assert held.status is JobStatus.BLOCKED and held.tags.get("blocked_reason") == "awaiting_owner_approval"

    pending = await queue_router.list_blocked_jobs(task_type=None, limit=50, after=None, response=None)
    assert [item["id"] for item in pending] == [job.job_id]
    assert pending[0]["risk"] == "outbound" and pending[0]["projection_status"] == "direct"

    decided = await queue_router.approve_job(job.job_id, queue_router.JobApproveRequest(decision_id="decision-1"))
    assert decided["status"] == "queued" and decided["authority_mode"] == "direct"
    assert decided["decided_by"] == "trusted-internal" and decided["bounded_grant"] is None
    approved = await manager.queue.get_job(job.job_id)
    assert approved.status is JobStatus.QUEUED and approved.tags["approved_by"] == "trusted-internal"
    assert manager.queue._server_approval_provenance_valid(approved)

    replay = await queue_router.approve_job(job.job_id, queue_router.JobApproveRequest(decision_id="decision-1"))
    assert replay["replayed"] is True and replay["status"] == "queued"
    with pytest.raises(Exception) as error:
        await queue_router.approve_job(job.job_id, queue_router.JobApproveRequest(decision_id="decision-2"))
    assert getattr(error.value, "status_code", None) == 409


async def test_reject_cancels_and_spoofed_tags_never_count(manager):
    job = Job(job_type=JobType.AGENT_ACTION,
              payload={"action_hint": "commitment_mark_complete", "risk": "mutating"})
    await manager.queue.post(job)
    rejected = await queue_router.reject_job(job.job_id, queue_router.JobRejectRequest(reason="not now"))
    assert rejected["status"] == "cancelled" and rejected["reason"] == "not now"
    row = await manager.queue.get_job(job.job_id)
    assert row.status is JobStatus.CANCELLED and row.tags["rejected_by"] == "trusted-internal"

    spoofed = Job(job_type=JobType.AGENT_ACTION, payload={"action_hint": "agent_git_push", "risk": "mutating"},
                  tags={"approved_by": "fabricated-owner"})
    assert queue_router._reserved_job_tags(spoofed.tags) == ["approved_by"]


async def test_read_only_actions_queue_without_a_hold(manager):
    job = Job(job_type=JobType.AGENT_ACTION, payload={"action_hint": "commitment_list_open", "risk": "read_only"})
    await manager.queue.post(job)
    assert (await manager.queue.get_job(job.job_id)).status is JobStatus.QUEUED
