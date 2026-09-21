"""Resource views join diagnostics conservatively, without rerunning scored work."""
from copy import deepcopy
import json

import pytest

from protagine.qualification import paired_report


def request(identity, *, workload=None, usage=None, complete=True, elapsed=1000):
    result = {'trace_request_id': identity, 'timing_protocol': 'paired-transport-2',
        'response_mode': 'buffered_json', 'response_complete': complete,
        'status': 200, 'elapsed_ms': elapsed, 'usage': usage}
    if workload is not None:
        result['workload'] = workload
    return result


def trace(tmp_path, events):
    member = {'path': 'runs/one', 'case': {'id': 'paired.test'}}
    path = tmp_path / member['path'] / 'attempts' / member['case']['id'] / 'private-trace.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(json.dumps({'protocol': 'paired-private-trace-1',
        'kind': 'model_request', 'thread': thread, 'data': {'request_id': identity,
        'payload': {'messages': ['PRIVATE_BODY_MUST_NOT_BE_COPIED']}}}) for identity, thread in events))
    return member, path


def test_workloads_keep_generic_threads_unknown_and_preserve_usage_and_missingness(tmp_path):
    member, path = trace(tmp_path, [(1, 'Thread-3 (<lambda>)'), (2, 'paired-source-worker'),
                                  (3, 'paired-source-worker'), (4, 'MainThread')])
    requests = [request(1, usage={'prompt_tokens': 100, 'completion_tokens': 20}),
        request(2, usage={'prompt_tokens': 40, 'completion_tokens': 10}, elapsed=500),
        request(3, complete=False), request(4, workload='foreground',
            usage={'prompt_tokens': 80, 'completion_tokens': 30}, elapsed=2000)]
    original, raw_trace = deepcopy(requests), path.read_bytes()
    result = paired_report._workloads([paired_report._workload_observations(
        tmp_path, member, {'effects': {'model_requests': requests}})])
    attribution = result['attribution']
    assert attribution['observed_requests'] == 4
    assert attribution['attributed_requests'] == 3 and attribution['unknown_requests'] == 1
    assert attribution['coverage'] == 'partial'
    groups = result['groups']
    assert groups['background']['usage']['observed_requests'] == 2
    assert groups['background']['usage']['requests_missing_usage'] == 1
    assert groups['background']['usage']['incomplete_or_failed_requests'] == 1
    assert groups['background']['usage']['input_tokens']['observed_total'] == 40
    assert groups['background']['usage']['usage_coverage'] == 'partial'
    assert groups['background']['timing']['metrics']['request_elapsed_ms']['median'] == 500
    assert groups['background']['timing']['metrics']['request_output_tokens_per_second']['median'] == 20
    assert groups['foreground']['timing']['metrics']['request_output_tokens_per_second']['median'] == 15
    assert groups['unknown']['usage']['observed_requests'] == 1
    assert all('episode_elapsed_ms' not in group['timing']['metrics'] for group in groups.values())
    assert all(group['timing']['decode_tokens_per_second'] is None for group in groups.values())
    assert 'PRIVATE_BODY' not in json.dumps(result)
    assert requests == original and path.read_bytes() == raw_trace


@pytest.mark.parametrize('events,requests', [
    ([(1, 'Thread-3 (<lambda>)')], [request(1)]),
    ([(1, 'MainThread')], [request(1)]),
    ([(1, 'paired-source-worker')], [request(2)]),
    ([(1, 'paired-source-worker'), (1, 'paired-source-worker')], [request(1)]),
    ([(1, 'paired-source-worker')], [request(1), request(1)]),
    ([(1, 'paired-source-worker')], [request(1, workload='foreground')]),
    ([(True, 'paired-source-worker')], [request(True)]),
])
def test_ambiguous_or_conflicting_evidence_never_implies_foreground(tmp_path, events, requests):
    member, _ = trace(tmp_path, events)
    observations, _ = paired_report._workload_observations(tmp_path, member,
        {'effects': {'model_requests': requests}})
    assert all(group == 'unknown' for _, group, _ in observations)


@pytest.mark.parametrize('problem', ['absent', 'malformed', 'symlink', 'oversized'])
def test_unusable_trace_preserves_explicit_labels_and_does_not_break_reporting(tmp_path, problem):
    member, path = trace(tmp_path, [(1, 'paired-source-worker')])
    if problem == 'absent':
        path.unlink()
    elif problem == 'malformed':
        path.write_text('{broken')
    elif problem == 'symlink':
        target = tmp_path / 'other-private-trace'
        path.rename(target)
        path.symlink_to(target)
    else:
        with path.open('wb') as output:
            output.truncate(8 * 1024 * 1024 + 1)
    result = paired_report._workloads([paired_report._workload_observations(tmp_path, member,
        {'effects': {'model_requests': [request(1), request(2, workload='foreground')]}})])
    assert result['attribution']['attributed_requests'] == 1
    assert result['attribution']['unknown_requests'] == 1
    assert result['attribution']['trace_episodes'] == {('absent' if problem == 'absent' else 'invalid'): 1}


def test_usage_counts_partial_fields_zero_and_incomplete_response_without_inventing_totals():
    requests = [request(1, usage={'prompt_tokens': 0, 'completion_tokens': 0}),
        request(2, usage={'prompt_tokens': True, 'completion_tokens': 20}),
        request(3, usage={'prompt_tokens': 5, 'completion_tokens': -1}),
        request(4, usage={'prompt_tokens': 7, 'completion_tokens': 3}, complete=False), request(5)]
    usage = paired_report._request_usage(requests)
    assert usage['requests_with_usage'] == 2 and usage['requests_missing_usage'] == 3
    assert usage['completed_requests_missing_usage'] == 3
    assert usage['input_tokens'] == {'observed_total': 12,
        'requests_with_observation': 3, 'requests_missing_observation': 2}
    assert usage['output_tokens'] == {'observed_total': 23,
        'requests_with_observation': 3, 'requests_missing_observation': 2}
    assert paired_report._request_usage([])['input_tokens']['observed_total'] is None
    assert paired_report._request_usage([request(1)])['usage_coverage'] == 'unobserved'
    assert paired_report._request_usage([request(1, usage={'completion_tokens': 0})])['usage_coverage'] == 'partial'


def test_workload_summary_preserves_scores_rows_and_existing_aggregate_keys(tmp_path, monkeypatch):
    member, path = trace(tmp_path, [(1, 'paired-source-worker')])
    row = {'outcome': 'pass', 'primary_outcome': 'pass', 'elapsed_ms': 4000,
        'effects': {'model_requests': [request(1, usage={'prompt_tokens': 20, 'completion_tokens': 10})]}}
    original, raw_trace = deepcopy(row), path.read_bytes()
    manifest = {'recipe': {}, 'pairs': [{'episode_id': 'paired.test', 'order': list(paired_report.ARMS),
        'task_sha256': 'b' * 64, 'oracle_sha256': 'c' * 64,
        'arms': dict.fromkeys(paired_report.ARMS, member)}], 'sha256': 'd' * 64,
        'comparison_key': 'e' * 64, 'label': 'test', 'evidence_mode': 'controlled',
        'dataset': {'version': 'test', 'split': 'development'},
        'comparison': {'policy': {'environment': {'endpoint_usage': 'unknown'}}}}
    monkeypatch.setattr(paired_report, 'load_manifest', lambda _: manifest)
    monkeypatch.setattr(paired_report, '_row', lambda *args: row)
    report = paired_report.summarize(tmp_path)
    assert report['paired_score']['ties'] == report['paired_score']['both_completed'] == 1
    assert report['report_protocol'] == 'paired-attribution-3' and report['schema'] == 1
    for arm in paired_report.ARMS:
        assert report['arms'][arm]['accounting'] == paired_report._accounting([original])
        assert report['arms'][arm]['timing'] == paired_report._timing([original])
        assert report['pairs'][0]['results'][arm] == original
    rendered = paired_report.markdown(report)
    assert 'Usage missing' in rendered and 'workload attribution: 1/1' in rendered
    assert 'PRIVATE_BODY' not in rendered
    assert row == original and path.read_bytes() == raw_trace
