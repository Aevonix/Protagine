"""Retained goal records remain usable without a second execution engine."""
import json
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.routers import host
from protagine.goals import Goal, GoalDAG, GoalStatus, GoalStore, Subtask
from protagine.autonomy.loop import AutonomyLoop
from protagine.tools.handlers import handle_task_complete, handle_task_snooze, handle_task_dismiss


@pytest.fixture
def records(tmp_path, monkeypatch):
    store = GoalStore(str(tmp_path / 'goals.db'))
    monkeypatch.setattr(host, '_goals_store', store)
    yield store
    store.close()


def seed(store, status=GoalStatus.ACCEPTED):
    goal = Goal(title='Existing project comparison', status=status,
                context={'origin': 'previous-session', 'dispatch_unavailable': 'queue_backend_unconfigured'})
    store.save_goal(goal)
    return goal


def api():
    app = FastAPI()
    app.include_router(host.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


@pytest.mark.parametrize('mode', ['off', 'shadow', 'live'])
async def test_creation_removed_in_every_mode_without_changing_records(records, monkeypatch, mode):
    monkeypatch.setenv('PROTAGINE_COGNITION_SPINE', mode)
    old = seed(records)
    async with api() as client:
        response = await client.post('/v1/host/goals', json={
            'identity': {'host_id': 'test'}, 'title': 'Unattested executable request'})
        assert response.status_code == 405
        assert (await client.get('/v1/host/goals/' + old.goal_id)).json()['status'] == 'accepted'
    assert [g.goal_id for g in records.list_goals()] == [old.goal_id]
    assert records.get_dag(old.goal_id) is None
    assert records.get_audit_trail(old.goal_id) == []
    assert not hasattr(records, 'activate_goal')


async def test_reported_completion_and_saved_dag_survive_restart(records, monkeypatch):
    old = seed(records)
    dag = GoalDAG(goal_id=old.goal_id)
    task = Subtask(goal_id=old.goal_id, title='Previously saved step')
    dag.add_subtask(task)
    records.save_dag(dag)
    url = '/v1/host/goals/' + old.goal_id
    async with api() as client:
        result = await client.patch(url, json={'identity': {'host_id': 'test'}, 'status': 'done'})
        assert result.status_code == 200
        completed = result.json()
        assert completed['status'] == 'completed'
        assert completed['completion_basis'] == 'reported_completion'
        assert completed['dispatch_unavailable'] is None
        assert records.get_goal(old.goal_id).context['origin'] == 'previous-session'
        records.close()
        reopened = GoalStore(records._db_path)
        monkeypatch.setattr(host, '_goals_store', reopened)
        try:
            assert (await client.patch(url, json={'identity': {'host_id': 'test'}, 'status': 'completed'})).json() == completed
            assert (await client.get('/v1/host/goals')).json()['goals'] == [completed]
            assert list(reopened.get_dag(old.goal_id).subtasks) == [task.subtask_id]
            assert [(t.from_status, t.to_status, t.trigger) for t in reopened.get_audit_trail(old.goal_id)] == [
                ('accepted', 'completed', 'reported_completion')]
        finally:
            reopened.close()


async def test_record_lifecycle_and_no_resurrection(records):
    old = seed(records, GoalStatus.ACTIVE)
    url = '/v1/host/goals/' + old.goal_id
    async with api() as client:
        for status, expected in [('blocked', 'blocked'), ('unblocked', 'active'), ('abandoned', 'abandoned')]:
            response = await client.patch(url, json={'identity': {'host_id': 'test'}, 'status': status})
            assert response.status_code == 200
            assert response.json()['status'] == expected
        assert (await client.patch(url, json={'identity': {'host_id': 'test'}, 'status': 'done'})).status_code == 409
        assert (await client.patch('/v1/host/goals/missing', json={'identity': {'host_id': 'test'}, 'status': 'done'})).status_code == 404
    assert records.get_dag(old.goal_id) is None
    assert [r.to_status for r in records.get_audit_trail(old.goal_id)] == ['blocked', 'active', 'abandoned']


def test_completion_and_audit_roll_back_together(records):
    goal = seed(records)
    conn = records._get_conn()
    conn.execute("""CREATE TRIGGER reject_completion BEFORE INSERT ON goal_audit_log
        WHEN NEW.trigger='reported_completion' BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='simulated storage failure'):
        records.complete_task(goal.goal_id)
    assert records.get_goal(goal.goal_id).status == GoalStatus.ACCEPTED
    assert records.get_audit_trail(goal.goal_id) == []


async def test_attention_sweep_never_activates_accepted_record(records):
    goal = seed(records)
    stats = SimpleNamespace(goals_checked=0, errors=0)
    loop = SimpleNamespace(_registry=SimpleNamespace(goals=records), stats=stats)
    await AutonomyLoop._phase_goals(loop)
    assert stats.goals_checked == 1
    assert stats.errors == 0
    assert records.get_goal(goal.goal_id).status == GoalStatus.ACCEPTED
    assert records.get_dag(goal.goal_id) is None


@pytest.mark.parametrize('handler,expected', [(handle_task_complete, 'completed'), (handle_task_snooze, 'snoozed'), (handle_task_dismiss, 'dismissed')])
async def test_record_tools_use_actual_store_methods(records, handler, expected):
    goal = seed(records)
    registry = SimpleNamespace(goals=records)
    result = json.loads(await handler({'task_id': goal.goal_id}, registry))
    assert result['status'] == expected
    missing = json.loads(await handler({'task_id': 'missing'}, registry))
    assert missing['status'] == 'unavailable'
