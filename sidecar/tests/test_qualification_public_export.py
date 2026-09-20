"""Private receipts become allowlisted public scalars, never copied traces."""
from copy import deepcopy
import hashlib
import json

import pytest

from protagine.qualification.public_export import export_records, publish_snapshot
from protagine.qualification.records import CaseSpec, encode, write_once

META = {'publication_scope': 'public_synthetic', 'phase': 'screen',
    'deployment': {'id': 'example-stock', 'model': 'Example model', 'profile': 'practical',
        'source_url': 'https://huggingface.co/example/model', 'weights_verified': False}}


def fixture(tmp_path, *, outcome='pass', primary='pass', private=False):
    case = CaseSpec(id='screen.grounding', version='1', role='chat', boundary='role_completion',
        consumer='example', evaluator='fields', inputs={'content': 'PUBLIC SYNTHETIC'},
        oracle={'expected': 'ORACLE_SENTINEL'}, provenance='private' if private else 'public').record()
    root = tmp_path / 'source'
    run = {'schema': 1, 'id': 'abc123', 'created_at': '2026-09-20T12:00:00Z',
        'recipe': {'binding': 'PRIVATE_BINDING_SENTINEL', 'api_key': 'SECRET_SENTINEL'},
        'cases': [case], 'suite_version': 'screen-1', 'evidence_mode': 'actual_inference',
        'suite_sha256': 'a' * 64, 'implementation_sha256': 'b' * 64,
        'evaluator_identities': {'fields': {'source_sha256': 'c' * 64}}}
    row = {'run_id': 'abc123', 'case_sha256': case['sha256'], 'outcome': outcome,
        'primary_outcome': primary, 'elapsed_ms': 52,
        'ended_at': '2026-09-20T12:00:01Z', 'checks': {'secret-related-check': True},
        'output': 'PRIVATE_ANSWER_SENTINEL', 'observations': [{'endpoint': 'http://10.0.0.1:8000'}],
        'effects': {'home': '/home/private'}, 'failure_category': 'SECRET_ERROR_SENTINEL'}
    write_once(root / 'run.json', run)
    write_once(root / 'attempts' / case['id'] / 'result.json', row)
    return root


def test_export_keeps_counts_and_drops_every_private_payload(tmp_path):
    runs = export_records(fixture(tmp_path), META)
    raw = json.dumps(runs)
    for value in ('SENTINEL', '10.0.0.1', '/home/private', 'secret-related-check'):
        assert value not in raw
    run = runs[0]
    assert run['counts'] == {'declared': 1, 'primary_passes': 1, 'outcomes': {'pass': 1}}
    assert run['comparison_key'] is None
    assert run['deployment']['runtime'] is None
    assert run['cases'][0]['summary'] is None
    assert run['boundary'] == 'role_completion'


@pytest.mark.parametrize('outcome', ['timeout', 'unsupported', 'setup_error', 'error', 'interrupted'])
def test_failures_stay_in_denominator(tmp_path, outcome):
    run = export_records(fixture(tmp_path, outcome=outcome, primary='unverified'), META)[0]
    assert run['counts'] == {'declared': 1, 'primary_passes': 0, 'outcomes': {outcome: 1}}
    assert run['metrics'][0]['samples'] == 1


def test_success_without_primary_attribution_is_not_model_pass(tmp_path):
    run = export_records(fixture(tmp_path, primary='unverified'), META)[0]
    assert run['counts']['outcomes'] == {'pass': 1}
    assert run['counts']['primary_passes'] == 0
    assert run['cases'][0]['failure_stage'] == 'attribution'


def test_private_fixture_and_unapproved_export_are_rejected(tmp_path):
    root = fixture(tmp_path, private=True)
    with pytest.raises(ValueError, match='Private cases'):
        export_records(root, META)
    with pytest.raises(ValueError, match='publication scope'):
        export_records(root, {**META, 'publication_scope': 'private'})


def test_public_metadata_cannot_link_internal_endpoint_or_auth_query(tmp_path):
    root = fixture(tmp_path)
    for url in ('http://10.0.0.1:8000', 'https://huggingface.co/x?token=secret',
                'https://secret@github.com/example/model'):
        metadata = deepcopy(META)
        metadata['deployment']['source_url'] = url
        with pytest.raises(ValueError, match='source URL'):
            export_records(root, metadata)


def test_comparison_requires_fixed_protocol_and_tracks_harness(tmp_path):
    root = fixture(tmp_path)
    meta = {**META, 'comparison_protocol': {'id': 'v1', 'budget_policy': 'practical-v1',
        'supporting_models': {'embedding': 'fixed-revision'}, 'hardware_policy': 'same-task-budget'}}
    first = export_records(root, meta)[0]['comparison_key']
    second = export_records(root, {**meta, 'comparison_protocol': {
        **meta['comparison_protocol'], 'budget_policy': 'deliberate-v1'}})[0]['comparison_key']
    assert first and second and first != second


def test_snapshot_hashes_match_and_never_overwrite(tmp_path):
    runs = export_records(fixture(tmp_path), META)
    out = tmp_path / 'public'
    manifest = publish_snapshot(runs, out)
    entry = manifest['runs'][0]
    assert entry['sha256'] == hashlib.sha256(encode(runs[0])).hexdigest()
    assert (out / 'runs' / f"{entry['run_id']}.json").read_bytes() == encode(runs[0])
    assert manifest['progress']['completed'] == 1
    with pytest.raises(FileExistsError):
        publish_snapshot(runs, out)
