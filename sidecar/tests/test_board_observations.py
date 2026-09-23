"""The owner's work view reads the body's board observations, never Hermes' kanban database."""
from datetime import datetime, timezone
from types import SimpleNamespace

from protagine.api.routers import mind as mind_router
from protagine.turns.board_observations import kanban_view


def test_without_a_mind_or_an_observation_the_view_says_so():
    mind_router.set_mind(None)
    assert kanban_view()["reason"] == "mind_not_running" and kanban_view()["available"] is False
    mind_router.set_mind(SimpleNamespace(observed_at=None, observations={}, board_counts={}))
    try:
        assert kanban_view()["reason"] == "no_board_observation_yet"
    finally:
        mind_router.set_mind(None)


def test_the_last_observation_projects_goals_blocked_stale_and_mind_tasks_once():
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    mind = SimpleNamespace(observed_at=now, board_counts={"ready": 3, "blocked": 1, "done": 9}, observations={
        "goal": [{"id": "g-1", "title": "finish the site", "assignee": "default", "status": "ready", "idle_s": 3600,
                  "goal": True, "goal_max_turns": 9}],
        "blocked_task": [{"id": "b-1", "title": "stuck", "assignee": "default", "status": "blocked", "idle_s": 60,
                          "block_kind": "capability"}],
        "stale_task": [{"id": "t-1", "title": "old", "assignee": "default", "status": "ready", "idle_s": 80 * 3600,
                        "age_s": 90 * 3600}, {"id": "g-1", "title": "finish the site", "status": "ready"}],
        "mind_task": [{"id": "m-1", "title": "mind child", "assignee": "protagine-act", "status": "done",
                       "intention_id": "i-1"}],
    })
    mind_router.set_mind(mind)
    try:
        view = kanban_view(limit=3, now=now.timestamp())
    finally:
        mind_router.set_mind(None)
    assert view["available"] is True and view["source"] == "mind_board_observations" and view["selection"] == "body_observation"
    assert [item["native_task_id"] for item in view["items"]] == ["g-1", "b-1", "t-1"]   # deduplicated, capped
    assert view["total"] == 4 and view["truncated"] is True and view["state_counts"] == {"ready": 3, "blocked": 1, "done": 9}
    goal, blocked, stale = view["items"]
    assert goal["goal_mode"] is True and goal["goal_max_turns"] == 9 and goal["liveness"] == "unknown"
    assert blocked["block_kind"] == "capability" and stale["heartbeat_age_seconds"] == 80 * 3600.0
    assert stale["created_at"] == now.timestamp() - 90 * 3600
    assert view["boards"] == [{"board": "default", "available": True, "total": 4}] and view["recent"] == []
