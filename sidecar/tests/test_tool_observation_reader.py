"""The rollback reader preserves and erases original tool evidence without a writer."""
import copy

import httpx

from protagine.turns import TurnIdempotencyLedger


def stored_origin(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'canonical.db')
    ledger.record_source('origin', contact_id='owner', session_id='native',
        messages=[{'role': 'user', 'content': 'Inspect the copper synchronization.'}],
        derive_claims=False)
    origin = ledger.source_references(['origin'], contact_id='owner', session_id='native')[0]
    return ledger, origin


def test_persisted_original_is_recalled_and_erased_with_its_origin(tmp_path):
    ledger, origin = stored_origin(tmp_path)
    ledger.record_source('original', contact_id='owner', session_id='native', messages=[{
        'role': 'tool', 'content': 'Copper synchronization completed with exit code 3.',
        '_native_tool_observation': 'native-tool-observation-v1',
        '_observation_sources': [origin],
    }], derive_claims=False)
    reader = TurnIdempotencyLedger(ledger.db_path)
    rows = reader.search_sources('copper synchronization', contact_id='owner', session_id='later')
    assert any(row['turn_id'] == 'original' and row['role'] == 'tool' for row in rows)
    erased = reader.erase_sources(contact_id='owner', turn_ids=['origin'])
    assert 'original' in erased['affected_source_ids']
    reopened = TurnIdempotencyLedger(ledger.db_path)
    assert not reopened.search_sources('copper synchronization', contact_id='owner', session_id='later')


