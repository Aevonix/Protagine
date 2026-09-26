"""Who may set ``verified`` (architecture 4.8): the owner and a check that ran are the mind's to grant;
the body may report only a Hermes failure, and only with a reason. A blocked task with a reason is a
Hermes failure too and stays open."""

from __future__ import annotations

import json

import pytest

from protagine.api.routers import mind as mind_router
from protagine.mind import outcomes as outcomes_module
from protagine.mind.drives import slug
from protagine.mind.rank import Candidate
from test_mind_loop import OWNER, Fixture, pinned_clock  # noqa: F401  (pinned_clock: autouse pytest fixture)

ROOMY = {"breaker": {"failures": 50}, "budgets": {"tasks_per_hour": 50, "concurrent_tasks": 50}}


@pytest.fixture
def fx(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fixture = Fixture(tmp_path, config=ROOMY)
    yield fixture
    mind_router.set_mind(None)
    fixture.store.close()


async def task(fx, n: int, *, check=None):
    fx.shift(minutes=5)
    topic = f"topic {n}"
    row = await fx.mind._form(Candidate(type="research", drive="curiosity", kind="task", title=f"Research {topic}",
                                        dedup_key=f"research:{slug(topic)}", salience=0.9, cost=0.1,
                                        text=f"Find out about {topic}.", topic=topic, concern=f"research {topic}",
                                        evidence=[f"interest:{slug(topic)}"]), 0.9, fx.now)
    assert row is not None and row.status == "approved", row
    if check is not None:
        fx.store.update(row.id, success_check=json.dumps(check))
    fx.mind.bound(row.id, f"kanban:{n}")
    return row


def report(fx, row, **body):
    """What POST /v1/mind/outcome passes on: a body report."""
    return fx.mind.outcomes.record(row.id, hermes_ref=row.hermes_ref, **body)


async def test_the_body_cannot_claim_owner_or_check(fx):
    assert outcomes_module.BODY_VERIFIERS == frozenset({"hermes_failure"})
    for n, claim in enumerate(("owner", "check"), 1):
        row = await task(fx, n)
        done = report(fx, row, status="done", summary="all good", verified=claim)
        assert done.outcome == "done" and done.verified == "none"
    # A check that actually ran is recorded as ``check`` whatever the body claimed.
    row = await task(fx, 3, check={"kind": "result_field", "field": "finding"})
    done = report(fx, row, status="done", summary="finding: the tide turns at noon", verified="owner")
    assert done.verified == "check" and done.result_metadata["check"]["passed"] is True
    # The mind's own callers still name their verifier (an owner switch, a goal the mind closed).
    row = await task(fx, 4)
    owned = fx.mind.outcomes.record(row.id, status="done", summary="the owner confirmed it", verified="owner",
                                    by="owner")
    assert owned.verified == "owner"


async def test_a_failure_without_a_reason_is_not_hermes_verified(fx):
    row = await task(fx, 1)
    failed = report(fx, row, status="failed")
    assert failed.outcome == "failed" and failed.verified == "none"
    row = await task(fx, 2)
    failed = report(fx, row, status="failed", error="   ")
    assert failed.verified == "none"
    row = await task(fx, 3)
    failed = report(fx, row, status="failed", error="the archive site timed out")
    assert failed.verified == "hermes_failure" and failed.failed_reason == "the archive site timed out"
    assert outcomes_module.hermes_reason(failed) == "the archive site timed out"


async def test_a_blocked_report_with_a_reason_is_hermes_verified_and_stays_open(fx):
    row = await task(fx, 1)
    blocked = report(fx, row, status="blocked", summary="the repository needs credentials I do not have")
    assert blocked.outcome == "blocked" and blocked.verified == "hermes_failure"
    assert blocked.status == "dispatched" and blocked.failed_reason == "the repository needs credentials I do not have"
    assert outcomes_module.hermes_reason(blocked) == "the repository needs credentials I do not have"
    # Still open: a later report settles it and recomputes the verifier.
    done = report(fx, row, status="done", summary="unblocked and finished")
    assert done.outcome == "done" and done.verified == "none"
    # Without a reason a blocked report verifies nothing.
    row = await task(fx, 2)
    bare = report(fx, row, status="blocked")
    assert bare.outcome == "blocked" and bare.verified in (None, "none")
    assert outcomes_module.hermes_reason(bare) == ""


async def test_a_claimed_hermes_failure_without_a_reason_is_ignored(fx):
    row = await task(fx, 1)
    failed = report(fx, row, status="failed", verified="hermes_failure")
    assert failed.verified == "none"
    # A finished task is not a failure, whatever the body calls it.
    row = await task(fx, 2)
    done = report(fx, row, status="done", summary="finished", verified="hermes_failure")
    assert done.verified == "none"
    row = await task(fx, 3)
    failed = report(fx, row, status="failed", error="the build broke", verified="hermes_failure")
    assert failed.verified == "hermes_failure"
