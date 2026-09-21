"""Historical runtime counters remain inspectable, never quality evidence."""
import json
import sqlite3

import pytest

from protagine.skills_memory.models import Skill
from protagine.skills_memory.retrieve import relevant_skills
from protagine.skills_memory.store import SkillStore


def legacy_database(path):
    store = SkillStore(path)
    skill = store.add(Skill(title='Inspect backup receipt', situation='backup receipt inspect',
                           steps=['Read receipt'], source_ref='original-task', uses=3))
    # Restore the pre-migration schema/data shape without constructing a second
    # schema definition that could drift from the shipped store.
    store._conn.execute('DROP TABLE skill_counter_archive')
    store._conn.execute('DROP TABLE skill_migrations')
    store._conn.execute('UPDATE skills SET wins=8, losses=2')
    store._conn.commit()
    original = dict(store._conn.execute('SELECT * FROM skills').fetchone())
    store._conn.close()
    return skill, original


def test_atomic_archive_preserves_procedure_and_source_and_runs_once(tmp_path):
    path = tmp_path / 'skills.db'
    skill, original = legacy_database(path)
    store = SkillStore(path)
    current = dict(store._conn.execute('SELECT * FROM skills').fetchone())
    assert current == {**original, 'wins': 0, 'losses': 0}
    archive = dict(store._conn.execute('SELECT * FROM skill_counter_archive').fetchone())
    assert archive['skill_id'] == skill.id and (archive['wins'], archive['losses']) == (8, 2)
    receipt = json.loads(store._conn.execute('SELECT receipt FROM skill_migrations').fetchone()[0])
    assert receipt['wins_archived'] == 8 and receipt['losses_archived'] == 2
    assert receipt['quality_credit'] is False and receipt['verification'] == 'unverified'
    store._conn.close()
    reopened = SkillStore(path)
    assert dict(reopened._conn.execute('SELECT * FROM skill_counter_archive').fetchone()) == archive
    assert reopened._conn.execute('SELECT COUNT(*) FROM skill_migrations').fetchone()[0] == 1
    assert not hasattr(reopened, 'record_outcome')
    assert reopened.snapshot()['quality_credit'] is False
    reopened._conn.close()


def test_failed_reset_does_not_archive_or_claim_migration_succeeded(tmp_path):
    path = tmp_path / 'skills.db'
    _, original = legacy_database(path)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TRIGGER fail_reset BEFORE UPDATE ON skills
            BEGIN SELECT RAISE(ABORT, 'write unavailable'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='write unavailable'):
        SkillStore(path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert dict(db.execute('SELECT * FROM skills').fetchone()) == original
        assert db.execute('SELECT COUNT(*) FROM skill_counter_archive').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM skill_migrations').fetchone()[0] == 0
        db.execute('DROP TRIGGER fail_reset')
    store = SkillStore(path)
    assert store.get(original['id']).wins == 0
    store._conn.close()


def test_imported_or_later_written_counters_cannot_change_retrieval_or_retention():
    store = SkillStore()
    match = store.add(Skill(id='best', title='Backup receipt', situation_signature='backup receipt',
                           created_at=10, wins=4, losses=3))
    other = store.add(Skill(id='weaker', title='Backup', situation_signature='backup', created_at=10))
    assert match.wins == match.losses == store.get(match.id).wins == 0
    expected = [s.id for s in relevant_skills(store, 'backup receipt')]
    store._conn.execute("UPDATE skills SET wins=1000, losses=0 WHERE id='weaker'")
    store._conn.execute("UPDATE skills SET wins=0, losses=1000 WHERE id='best'")
    store._conn.commit()
    assert [s.id for s in relevant_skills(store, 'backup receipt')] == expected == ['best', 'weaker']
    a, b = store.get(match.id), store.get(other.id)
    a.last_used_at = b.last_used_at = 20
    assert a.score(now=30) == b.score(now=30)
    store._conn.close()
