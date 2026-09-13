"""Canonical instruction publication requires its exact native origin first."""
import importlib

from test_native_tool_observations import native, source_app


def test_observation_does_not_publish_instruction_without_native_ownership(native, monkeypatch):
    n = native
    n.complete()
    n.request()
    ownership = importlib.import_module(n.plugin.__name__ + '.native_owned_copies').NativeOwnedCopies
    retain = ownership.retain_origin
    monkeypatch.setattr(ownership, 'retain_origin', lambda self, scope, source_id, **kwargs:
        False if source_id.startswith('task-instruction:') else retain(self, scope, source_id, **kwargs))
    with n.ledger._connect() as db:
        before = db.execute('SELECT count(*) FROM turn_sources').fetchone()[0]
    result = n.retain()
    assert result['accepted'] is False and result['source_recorded'] is False, result
    assert 'ownership' in result['error'].lower(), result
    with n.ledger._connect() as db:
        assert db.execute('SELECT count(*) FROM turn_sources').fetchone()[0] == before
    assert not n.outbox.snapshot()
