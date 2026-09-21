"""Report-only correction of returned native failures; no inference or regrading."""
from copy import deepcopy

import pytest

from protagine.qualification import paired_report
from protagine.qualification.records import digest


@pytest.fixture
def native_no_output():
    # The structural evidence observed in case 016, without task content or paths.
    recipe = {'binding': 'candidate', 'configured_model': 'candidate-model',
              'consumer': 'disposable_paired_native_container',
              'container': {'image_id': 'sha256:' + 'a' * 64}}
    case = {'id': 'paired.fixture', 'role': 'extraction', 'boundary': 'native_hermes',
            'consumer': 'native_paired', 'evaluator': 'paired_artifacts',
            'inputs': {'episodes': [{'session_id': 'main'}]}, 'oracle': {'declared_turns': 1}}
    row = {'case_id': case['id'], 'role': case['role'], 'outcome': 'fail',
           'failure_category': 'no_output', 'primary_outcome': 'unverified', 'checks': {},
           'output': '', 'evidence_mode': 'actual_inference', 'elapsed_ms': 100,
           'cleanup': 'state_directory_removed',
           'qualification_routing': {'binding': 'candidate', 'role': 'extraction'},
           'observations': [{'boundary': 'paired_native_container', 'role': 'extraction',
               'arm': 'protagine', 'selected_binding': 'candidate', 'prior_attempts': [],
               'outcome': 'returned', 'exit_code': 0, 'error_type': None, 'error_origin_stage': None,
               'dispatch_observed': True, 'attribution_basis': 'serialized_requests_and_returned_models',
               'returned_model': 'candidate-model', 'container_removed': True,
               'image_id': recipe['container']['image_id'],
               'state_isolation': 'fresh_tmpfs_per_arm_and_episode'}],
           'effects': {'container_removed': True, 'image_id': recipe['container']['image_id'],
               'state_isolation': 'fresh_tmpfs_per_arm_and_episode', 'declared_turns': 1,
               'turns_completed': 0, 'turns': [{'completed': False, 'final_response': ''}],
               'artifacts': {}, 'model_requests': [
                   {'model': 'candidate-model', 'returned_models': ['candidate-model'],
                    'status': 200, 'response_complete': True} for _ in range(8)]}}
    return row, recipe, case


@pytest.mark.parametrize('arm', paired_report.ARMS)
def test_returned_native_incomplete_no_output_gets_explicit_projection(native_no_output, arm):
    row, recipe, case = native_no_output
    row['observations'][0]['arm'] = arm
    original = deepcopy(row)
    assert paired_report._completion(row) is None
    assert paired_report._no_output_projection(row, recipe, case) == {
        'rule': paired_report.NO_OUTPUT_RULE, 'source_row_sha256': digest(original)}
    assert row == original


@pytest.mark.parametrize('path,value', [
    (('outcome',), value) for value in ('setup_error', 'error', 'unsupported', 'interrupted', 'not_run', 'pass')
] + [
    (('failure_category',), 'evaluator'), (('output',), 'nonempty'),
    (('primary_outcome',), 'pass'), (('checks',), {'artifact': True}),
    (('evidence_mode',), 'controlled'), (('cleanup',), 'unconfirmed'),
    (('qualification_routing',), ['unknown']), (('effects',), ['unknown']),
    (('cleanup',), None), (('qualification_routing', 'binding'), 'different'),
    (('qualification_routing', 'role'), 'different'), (('observations',), []),
    (('observations', 0, 'boundary'), 'other'), (('observations', 0, 'role'), 'other'),
    (('observations', 0, 'outcome'), 'error'), (('observations', 0, 'exit_code'), 1),
    (('observations', 0, 'error_origin_stage'), 'preparing'),
    (('observations', 0, 'selected_binding'), 'other'),
    (('observations', 0, 'prior_attempts'), ['fallback']),
    (('observations', 0, 'dispatch_observed'), False),
    (('observations', 0, 'attribution_basis'), 'claimed'),
    (('observations', 0, 'returned_model'), 'other'),
    (('observations', 0, 'container_removed'), False),
    (('effects', 'container_removed'), False), (('effects', 'image_id'), 'other'),
    (('effects', 'model_requests'), []),
    (('effects', 'model_requests', 0, 'model'), 'other'),
    (('effects', 'model_requests', 0, 'returned_models'), ['other']),
    (('effects', 'model_requests', 0, 'returned_models'), []),
    (('effects', 'turns_completed'), 1), (('effects', 'turns_completed'), False),
    (('effects', 'declared_turns'), 2), (('effects', 'turns'), []),
    (('effects', 'turns', 0, 'completed'), True),
    (('effects', 'turns', 0, 'final_response'), 'different'),
])
def test_unavailable_or_inconsistent_evidence_is_not_projected(native_no_output, path, value):
    row, recipe, case = native_no_output
    target = row
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    assert paired_report._no_output_projection(row, recipe, case) is None


def test_complete_native_turns_with_empty_prose_are_not_regraded(native_no_output):
    row, recipe, case = native_no_output
    row['effects'].update(turns_completed=1, turns=[{'completed': True, 'final_response': ''}])
    row['effects']['artifacts'] = {'answer.json': {'correct': True}}
    assert paired_report._no_output_projection(row, recipe, case) is None


def test_later_incomplete_turn_and_unreturned_auxiliary_request(native_no_output):
    row, recipe, case = native_no_output
    case['inputs']['episodes'].append({'session_id': 'second'})
    case['oracle']['declared_turns'] = 2
    row['effects'].update(declared_turns=2, turns_completed=1)
    row['effects']['turns'].insert(0, {'completed': True, 'final_response': 'first turn done'})
    row['effects']['model_requests'].append({'model': 'candidate-model', 'returned_models': []})
    assert paired_report._no_output_projection(row, recipe, case) is not None


def test_returned_response_and_frozen_recipe_are_required(native_no_output):
    row, recipe, case = native_no_output
    for request in row['effects']['model_requests']:
        request['response_complete'] = False
    assert paired_report._no_output_projection(row, recipe, case) is None
    for request in row['effects']['model_requests']:
        request['response_complete'] = True
    recipe['configured_model'] = 'other-model'
    assert paired_report._no_output_projection(row, recipe, case) is None


def test_v3_projection_is_symmetric_preserves_rows_and_keeps_v2_available(native_no_output, monkeypatch):
    row, recipe, case = native_no_output
    original = deepcopy(row)
    manifest = {'recipe': recipe, 'pairs': [{'episode_id': case['id'],
        'order': list(paired_report.ARMS), 'task_sha256': 'b' * 64, 'oracle_sha256': 'c' * 64,
        'arms': {arm: {'case': case, 'path': 'runs/' + arm} for arm in paired_report.ARMS}}],
        'sha256': 'd' * 64, 'comparison_key': 'e' * 64, 'label': 'test',
        'evidence_mode': 'actual_inference', 'dataset': {'version': 'test'},
        'comparison': {'policy': {}}}
    monkeypatch.setattr(paired_report, 'load_manifest', lambda _: manifest)
    monkeypatch.setattr(paired_report, '_row', lambda *args: row)
    v2 = paired_report.summarize('unused', report_protocol='paired-attribution-2')
    v3 = paired_report.summarize('unused')
    assert v2['paired_score'] is None and v2['unavailable_pairs'] == 1
    assert 'completion_projection' not in v2
    assert v3['report_protocol'] == 'paired-attribution-3'
    assert v3['paired_score']['ties'] == v3['paired_score']['neither_completed'] == 1
    assert v3['completion_projection']['corrected_episodes'] == 2
    assert v3['completion_projection']['raw_results_preserved'] is True
    assert v3['completion_projection']['artifact_verifier_reexecuted'] is False
    assert v3['pairs'][0]['completion'] == dict.fromkeys(paired_report.ARMS, False)
    assert all(value == original for value in v3['pairs'][0]['results'].values())
    assert row == original
