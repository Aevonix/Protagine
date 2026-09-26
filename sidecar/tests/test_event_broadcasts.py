"""Tests for Phase 2 — sidecar subsystems broadcast typed events."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from protagine.events import broadcaster
from protagine.events.broadcaster import emit, reset_broadcaster_for_tests


class _Collector:
    """Captures emitted events for assertions."""

    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


@pytest.fixture
def collector():
    c = _Collector()
    reset_broadcaster_for_tests(c)
    yield c
    reset_broadcaster_for_tests(None)
    broadcaster._broadcast_fn = None  # force lazy-import on next call


def test_emit_wraps_payload_with_type_and_timestamp(collector):
    emit("custom", {"foo": 1})
    assert len(collector.events) == 1
    e = collector.events[0]
    assert e["type"] == "custom"
    assert e["payload"] == {"foo": 1}
    assert "occurred_at" in e


def test_emit_with_no_payload_defaults_to_empty_dict(collector):
    emit("pinged")
    assert collector.events[0]["payload"] == {}


def test_emit_swallows_broadcaster_exceptions(collector):
    def _boom(_event):
        raise RuntimeError("sink dead")
    reset_broadcaster_for_tests(_boom)
    emit("custom", {"foo": 1})  # must not raise


def test_briefing_save_broadcasts_briefing(collector, tmp_path, monkeypatch):
    from protagine.briefings.store import BriefingStore
    from protagine.briefings.models import (
        Briefing, BriefingPriority, BriefingStatus, BriefingType,
    )

    store = BriefingStore(str(tmp_path / "b.db"))
    briefing = Briefing(
        briefing_id="b1",
        briefing_type=BriefingType.DAILY,
        status=BriefingStatus.DRAFT,
        priority=BriefingPriority.NORMAL,
        sections=[],
        triggered_by="test",
        gateway=None,
        created_at=datetime.now(timezone.utc),
    )
    store.save(briefing)

    types = [e["type"] for e in collector.events]
    assert "briefing" in types
    payload = next(e for e in collector.events if e["type"] == "briefing")["payload"]
    assert payload["briefing_id"] == "b1"
    assert payload["priority"] == "normal"


def test_goal_save_broadcasts_goal_update(collector, tmp_path):
    from protagine.goals.store import GoalStore
    from protagine.goals.models import (
        Goal, GoalPriority, GoalSource, GoalStatus,
    )

    store = GoalStore(db_path=str(tmp_path / "goals.db"))
    goal = Goal(
        goal_id="g1",
        title="Test goal",
        description="",
        source=GoalSource.EXPLICIT,
        status=GoalStatus.ACTIVE,
        priority=GoalPriority.NORMAL,
        outcome=None,
        progress_pct=0.5,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    store.save_goal(goal)

    types = [e["type"] for e in collector.events]
    assert "goal_update" in types
    payload = next(e for e in collector.events if e["type"] == "goal_update")["payload"]
    assert payload["goal_id"] == "g1"
    assert payload["status"] == "active"
    assert payload["progress_pct"] == 0.5
