"""Retirement preserves ownership/effects and does not silently replay work."""
import json
import sqlite3

import pytest

from protagine.initiatives.executor_retirement import MIGRATION, reconcile
from protagine.initiatives.store import InitiativeStore


@pytest.fixture
def queue(tmp_path):
    store = InitiativeStore(tmp_path)
    yield store, tmp_path
    store._db.close()


def row(store, identifier):
    return dict(store._db.execute('SELECT * FROM initiatives WHERE id=?', (identifier,)).fetchone())


def test_retirement_accounts_for_every_row_preserving_native_owner_and_terminal_effects(queue):
    store, path = queue
    active = []
    for status in ('assigned', 'acknowledged', 'pending'):
        item = store.create(type='research', description='Distinct work', entity_id='same', dedup_key=status)
        store.assign(item.id, 'historical-executor')
        store.update(item.id, status=status, assigned_agent_id=None if status == 'pending' else 'historical-executor')
        active.append(item.id)
    native = store.create(type='operational', description='Native running')
    store.assign(native.id, 'historical-executor')
    store.update(native.id, assigned_agent_id='native-task:existing')
    done = store.create(type='research', description='Completed effect')
    store.assign(done.id, 'historical-executor')
    store.update(done.id, status='completed', result='retained output', result_metadata={'receipt':'verified-independent-effect'})
    original_native, original_done = row(store, native.id), row(store, done.id)
    unsupported = store.create(type='unknown', description='Never convert this prose to a command', priority=1)
    review = store.create(type='operational', source_type='operational', created_by='autonomy_loop',
                          action_hint='operational_review', description='Read-only review', priority=.1)
    before = [dict(r) for r in store._db.execute('SELECT * FROM initiatives ORDER BY id')]
    preview = reconcile(path, executor_ids=['historical-executor'])
    assert preview['total'] == len(before) == 7
    assert [dict(r) for r in store._db.execute('SELECT * FROM initiatives ORDER BY id')] == before
    dispositions = {r['id']: r['disposition'] for r in preview['rows']}
    assert dispositions[unsupported.id] == 'proposal_only_native_execution_capability_missing'
    assert dispositions[review.id] == 'existing_native_review'  # No priority-page starvation.
    with pytest.raises(ValueError, match='stop_old_executor'):
        reconcile(path, executor_ids=['historical-executor'], apply=True)
    receipt = reconcile(path, executor_ids=['historical-executor'], apply=True, executor_stopped=True)
    assert receipt['counts']['reconcile_effect_before_retry'] == 3
    for identifier in active:
        changed = row(store, identifier)
        assert changed['status'] == 'cancelled'
        assert changed['cancelled_reason'] == 'executor_retired_effect_unverified'
        assert changed['recovery_reason']
        assert changed['completed_at'] is None and changed['result'] is None
    # No automatic retry of a cancelled unknown effect, even for the exact same key.
    same, disposition = store.create_with_outcome(type='research', description='Retry', dedup_key='assigned')
    assert same.id == active[0] and disposition == 'deduped_terminal'
    assert row(store, native.id) == original_native
    assert row(store, done.id) == original_done
    history = store._db.execute('SELECT details FROM assignment_history WHERE action=?', (MIGRATION,)).fetchall()
    assert len(history) == 4
    assert all(json.loads(r[0])['quality_credit'] is False for r in history)
    second = reconcile(path, executor_ids=['historical-executor'], apply=True, executor_stopped=True)
    assert second['counts']['already_reconciled'] == 4
    assert store._db.execute('SELECT COUNT(*) FROM assignment_history WHERE action=?', (MIGRATION,)).fetchone()[0] == 4


def test_failed_history_write_rolls_back_row_and_receipt(queue):
    store, path = queue
    item = store.create(type='research', description='Old effect unknown')
    store.assign(item.id, 'protagine-executor')
    original = row(store, item.id)
    store._db.execute("""CREATE TRIGGER fail_retirement BEFORE INSERT ON assignment_history
        WHEN NEW.action='retire-builtin-executor-v1' BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
    store._db.commit()
    with pytest.raises(sqlite3.IntegrityError, match='disk failure'):
        reconcile(path, apply=True, executor_stopped=True)
    assert row(store, item.id) == original
    assert store._db.execute('SELECT COUNT(*) FROM assignment_history WHERE action=?', (MIGRATION,)).fetchone()[0] == 0


def test_missing_database_not_silently_created(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        reconcile(tmp_path)
    assert not (tmp_path / 'initiatives.db').exists()


def test_retired_executor_cannot_be_reactivated_by_preset_or_old_environment(monkeypatch):
    import importlib.util
    from protagine.util.autonomy_preset import snapshot
    monkeypatch.setenv('PROTAGINE_EXECUTOR_ENABLED', 'true')
    monkeypatch.setenv('PROTAGINE_AUTONOMY_PRESET', 'autonomous')
    assert 'PROTAGINE_EXECUTOR_ENABLED' not in snapshot()
    assert importlib.util.find_spec('protagine.services.initiative_executor') is None
