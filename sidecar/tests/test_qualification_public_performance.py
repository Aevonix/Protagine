"""Serving receipt exports preserve boundaries, samples and private payloads."""
from copy import deepcopy
import json

import pytest

from protagine.qualification.public_performance import export_serving_record


META = {'publication_scope': 'public_synthetic', 'public_id': 'synthetic-serving-cell',
    'expected_returned_model': 'PRIVATE_MODEL_SENTINEL',
    'deployment': {'id': 'example', 'model': 'Example', 'profile': 'Synthetic', 'weights_verified': False},
    'serving': {'target_input_tokens': 4096, 'client_concurrency_limit': 1,
        'cache_state': 'warm_engine_unique_prefixes', 'token_accounting': 'completion_includes_reasoning',
        'stream_interval_basis': 'sse_chunks'}}


def fixture(root):
    def write(name, body):
        (root/name).write_text(json.dumps(body))
    write('launch.json', {'created_at': '2026-09-20T10:00:00Z', 'finished_at': '2026-09-20T10:00:10Z',
        'dataset_sha256': 'a'*64, 'observer_sha256': 'b'*64,
        'docker_arguments': ['PRIVATE_ADDRESS_SENTINEL'], 'scope': 'PRIVATE_SCOPE_SENTINEL'})
    write('dataset.json', {'version': 'synthetic-1', 'datasets': {'4k': {'sha256': 'a'*64,
        'cases': [{'prompt_sha256': 'c'*64, 'id': 'PRIVATE_CASE_SENTINEL'}, {'prompt_sha256': 'd'*64, 'id': 'PRIVATE_CASE_SENTINEL_2'}]}}})
    write('sglang.json', {'max_concurrency': 1, 'max_concurrent_requests': 999, 'duration': 10,
        'itls': [[.05, .1], [.15]], 'generated_texts': ['PRIVATE_ANSWER_SENTINEL'],
        'server_info': {'endpoint': 'PRIVATE_ENDPOINT_SENTINEL'}})
    timing = [{'prompt_sha256': char*64, 'candidate_model': 'PRIVATE_MODEL_SENTINEL',
        'returned_models': ['PRIVATE_MODEL_SENTINEL'], 'success': True, 'deadline_exceeded': False,
        'content_contains_think_tag': False,
        'elapsed_ms': 5000, 'first_generated_delta_ms': 1000, 'first_content_delta_ms': 4000,
        'server_usage': {'prompt_tokens': 3700, 'completion_tokens': 100}} for char in ['c', 'd']]
    (root/'timing.jsonl').write_text('\n'.join(json.dumps(row) for row in timing))
    write('correctness-001.json', {'scorer': 'synthetic-exact-fields-1', 'passed': 1, 'independent_tasks': 2,
        'cases': [{'prompt_sha256': char*64, 'pass': passed, 'success': True, 'untruncated_final': True,
                   'checks': {'PRIVATE_ORACLE_SENTINEL': passed}} for char, passed in [('c', True), ('d', False)]]})
    return timing


def export(root, metadata=None):
    return export_serving_record(root, metadata or META, root/'dataset.json', '4k')


def test_allowlist_and_separate_latency_token_and_concurrency_boundaries(tmp_path):
    fixture(tmp_path)
    result = export(tmp_path)
    assert 'SENTINEL' not in json.dumps(result)
    assert result['counts'] == {'declared': 2, 'primary_passes': 1, 'outcomes': {'fail': 1, 'pass': 1}}
    assert result['performance']['transport_successes'] == 2
    assert result['performance']['observed_peak_inflight'] is None
    assert result['performance']['actual_input_tokens_max'] == 3700
    metrics = {row['id']: row for row in result['metrics']}
    assert metrics['completion_throughput_tps']['value'] == 20
    assert metrics['first_generated_median_ms']['value'] == 1000
    assert metrics['first_final_content_median_ms']['value'] == 4000
    assert metrics['stream_chunk_interval_median_ms']['value'] == 100
    assert metrics['stream_chunk_interval_median_ms']['samples'] == 3
    assert result['comparison_key'] is None


def test_failed_and_missing_requests_stay_in_correctness_denominator(tmp_path):
    rows = fixture(tmp_path)
    rows[0].update(success=False, deadline_exceeded=True, returned_models=[])
    (tmp_path/'timing.jsonl').write_text(json.dumps(rows[0]))
    result = export(tmp_path)
    assert result['counts']['outcomes'] == {'not_run': 1, 'timeout': 1}
    assert result['counts']['declared'] == 2
    assert result['status'] == 'aborted'
    assert result['performance']['transport_successes'] == 0
    assert result['counts']['primary_passes'] == 0


def test_missing_usage_is_unknown_and_wrong_model_cannot_earn_primary_credit(tmp_path):
    rows = fixture(tmp_path)
    rows[0]['server_usage'] = None
    rows[0]['returned_models'] = ['different-model']
    (tmp_path/'timing.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
    result = export(tmp_path)
    assert result['counts']['outcomes']['pass'] == 1
    assert result['counts']['primary_passes'] == 0
    assert result['performance']['actual_input_tokens_min'] is None
    assert result['metrics'][0]['value'] is None


@pytest.mark.parametrize('intervals,expected', [([(0, 4), (4, 9)], 1), ([(0, 5), (1, 6)], 2)])
def test_observed_overlap_uses_actual_monotonic_spans(tmp_path, intervals, expected):
    rows = fixture(tmp_path)
    for row, (start, end) in zip(rows, intervals):
        row.update(request_started_monotonic_s=start, request_finished_monotonic_s=end)
    (tmp_path/'timing.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
    assert export(tmp_path)['performance']['observed_peak_inflight'] == expected


def test_duplicate_or_unregistered_request_is_not_silently_counted(tmp_path):
    rows = fixture(tmp_path)
    (tmp_path/'timing.jsonl').write_text('\n'.join(json.dumps(row) for row in [*rows, rows[0]]))
    with pytest.raises(ValueError, match='Repeated prompts'):
        export(tmp_path)
    rows[0]['prompt_sha256'] = 'f'*64
    (tmp_path/'timing.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match='Unregistered'):
        export(tmp_path)


def test_recipe_change_creates_a_different_immutable_view(tmp_path):
    fixture(tmp_path)
    metadata = deepcopy(META)
    metadata['serving']['cache_state'] = 'warm_prefix_cache'
    assert export(tmp_path)['run_id'] != export(tmp_path, metadata)['run_id']


def test_mixed_reasoning_in_content_is_not_credited_as_early_final_output(tmp_path):
    rows = fixture(tmp_path)
    rows[0]['content_contains_think_tag'] = True
    rows[1].pop('content_contains_think_tag')
    (tmp_path/'timing.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
    metrics = {m['id']: m for m in export(tmp_path)['metrics']}
    assert metrics['first_generated_median_ms']['value'] == 1000
    assert metrics['first_final_content_median_ms']['value'] is None
    assert metrics['first_final_content_median_ms']['samples'] == 0
