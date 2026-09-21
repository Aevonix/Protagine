"""Frozen workflow contracts, independent expected deliverables, and paired release planning."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from protagine.qualification import paired, paired_cases, paired_container, paired_public, paired_report
from protagine.qualification.paired_workflow_runtime import PROTOCOL
from protagine.qualification.records import digest, read


DIRECTORY = Path(paired_cases.__file__).parent / 'fixtures' / paired_cases.WORKFLOW_VERSION
EXAMPLES = json.loads((Path(__file__).parent / 'fixtures/paired-workflow-outcomes.json').read_text())


def cases(arm='base_hermes'):
    return paired_cases.cases(arm, dataset_version=paired_cases.WORKFLOW_VERSION)


def test_reviewed_workflow_bytes_are_frozen_before_model_evaluation():
    manifest, inventory, checksum = paired_cases.load_dataset(DIRECTORY)
    assert checksum == '93f39753e75ea945da5199fb7a006497d76db28fef7d678682877c956f997c1d'
    assert manifest['scenario_count'] == len(inventory) == 12


def receipt(case, example):
    def files(values):
        return {key: value if isinstance(value, str) else json.dumps(value) for key, value in values.items()}
    workflow = case.inputs['workflow']
    bounds = [0, *workflow['restart_before'], len(case.inputs['episodes'])]
    phases, previous = [], {'roots': {'home': None, 'workspace': None}, 'sha256': '0' * 64}
    for index, (start, end) in enumerate(zip(bounds, bounds[1:])):
        after = {'roots': {'home': {'device': 1, 'inode': 7}, 'workspace': {'device': 1, 'inode': 8}},
                 'sha256': str(index + 1) * 64, 'files': 7, 'bytes': 100}
        phases.append({'index': index, 'start_turn': start, 'end_turn_exclusive': end,
            'pid': 100 + index, 'worker_pid': 100 + index, 'stage': 'returned', 'exit_code': 0,
            'agent_close_returned': True, 'worker_stopped': True, 'turns_attempted': end - start,
            'turns_completed': end - start, 'state_preserved': True,
            'state_before': previous, 'state_after': after})
        previous = after
    return {'effects': {'declared_turns': bounds[-1], 'turns_completed': bounds[-1],
        'artifacts': files(example['valid']), 'workflow': {
            'protocol': PROTOCOL, 'restart_kind': 'graceful_worker_process',
            'restart_before': workflow['restart_before'], 'restarts_completed': len(workflow['restart_before']),
            'phases': phases, 'state_preserved': True, 'all_declared_turns_attempted': True,
            'all_phases_closed': True, 'read_failures_declared': workflow['read_failures'],
            'read_failures_consumed': workflow['read_failures'],
            'read_recoveries': [{'turn_index': f['turn_index'], 'path': f['path']} for f in workflow['read_failures']],
            'snapshots': {str(i): files(values) for i, values in example['valid_snapshots'].items()}}}}


def test_inventory_has_twelve_distinct_eight_turn_workflows_and_four_controls():
    inventory = cases()
    assert len(inventory) == len({c.id for c in inventory}) == 12
    assert {c.inputs['scenario'] for c in inventory} == set(EXAMPLES)
    assert {f: sum(c.inputs['family'] == f for c in inventory)
            for f in paired_cases.WORKFLOW_FAMILIES} == dict.fromkeys(paired_cases.WORKFLOW_FAMILIES, 3)
    assert sum(c.inputs['memory_condition'] == 'irrelevant' for c in inventory) == 4
    assert len({tuple(t['user'] for t in c.inputs['episodes']) for c in inventory}) == 12
    for case in inventory:
        assert len(case.inputs['episodes']) == 8
        assert case.inputs['workflow']['restart_before'] == [3, 6]
        assert case.inputs['workflow']['snapshot_after'] == [2, 5]
        assert case.timeout_seconds == 600 and case.inputs['max_iterations'] == 8
        assert sum(len(v.encode()) for v in case.inputs['initial_files'].values()) >= 6000
        assert all('protagine_' not in turn['user'] for turn in case.inputs['episodes'])
        assert case.inputs['dataset']['split'] == 'frozen_public_evaluation'
        assert 'oracle' not in case.inputs


def test_both_arms_receive_identical_history_sources_budgets_events_and_oracle():
    for left, right in zip(cases(), cases('protagine')):
        a, b = deepcopy(left.inputs), deepcopy(right.inputs)
        assert a.pop('arm') == 'base_hermes' and b.pop('arm') == 'protagine'
        assert a == b and left.oracle == right.oracle
    assert len(paired_cases.cases('base_hermes')) == 18
    assert len(paired_cases.cases('base_hermes', dataset_version=paired_cases.REVIEWED_VERSION)) == 60


@pytest.mark.parametrize('case', cases(), ids=lambda case: case.inputs['scenario'])
def test_independent_deliverables_pass_and_substantive_mistakes_fail(case):
    example = EXAMPLES[case.inputs['scenario']]
    observed = receipt(case, example)
    assert all(paired_cases.assess(observed, case.oracle).values())
    bad = deepcopy(example)
    bad['valid'].update(example['invalid_overrides'])
    for index, changes in example.get('invalid_snapshots', {}).items():
        bad['valid_snapshots'][str(index)].update(changes)
    assert not all(paired_cases.assess(receipt(case, bad), case.oracle).values())


def test_late_correction_cannot_erase_a_failed_intermediate_checkpoint():
    case = cases()[0]
    observed = receipt(case, EXAMPLES[case.inputs['scenario']])
    observed['effects']['workflow']['snapshots']['2'] = {}
    checks = paired_cases.assess(observed, case.oracle)
    assert all(value for name, value in checks.items() if name.startswith('artifact:'))
    assert not all(value for name, value in checks.items() if name.startswith('checkpoint:2:'))


@pytest.mark.parametrize('mutation', ['same_pid', 'lost_state', 'missing_phase', 'not_closed', 'missing_fault'])
def test_claimed_completion_cannot_substitute_for_observed_lifecycle(mutation):
    case = next(c for c in cases() if c.inputs['workflow']['read_failures'])
    observed = receipt(case, EXAMPLES[case.inputs['scenario']])
    lifecycle = observed['effects']['workflow']
    if mutation == 'same_pid':
        lifecycle['phases'][1]['pid'] = lifecycle['phases'][1]['worker_pid'] = lifecycle['phases'][0]['pid']
    elif mutation == 'lost_state':
        lifecycle['phases'][1]['state_before'] = {}
    elif mutation == 'missing_phase':
        lifecycle['phases'].pop()
    elif mutation == 'not_closed':
        lifecycle['phases'][0]['agent_close_returned'] = False
    else:
        lifecycle['read_failures_consumed'] = []
    checks = paired_cases.assess(observed, case.oracle)
    assert all(v for k, v in checks.items() if k.startswith(('artifact:', 'semantic:', 'format:')))
    assert not all(checks.values())


def test_format_and_semantic_value_have_distinct_checks():
    case = cases()[0]
    observed = receipt(case, EXAMPLES[case.inputs['scenario']])
    artifact = json.loads(observed['effects']['artifacts']['procurement.json'])
    artifact['unexpected_note'] = 'additional unrequested field'
    observed['effects']['artifacts']['procurement.json'] = json.dumps(artifact)
    checks = paired_cases.assess(observed, case.oracle)
    assert checks['format:procurement.json'] is False
    assert checks['semantic:procurement.json'] is True
    artifact.pop('unexpected_note')
    artifact['cost_credits'] = 1
    observed['effects']['artifacts']['procurement.json'] = json.dumps(artifact)
    checks = paired_cases.assess(observed, case.oracle)
    assert checks['format:procurement.json'] is True
    assert checks['semantic:procurement.json'] is False


def test_unexercised_fault_only_makes_otherwise_correct_work_unavailable():
    case = next(c for c in cases() if c.inputs['workflow']['read_failures'])
    observed = receipt(case, EXAMPLES[case.inputs['scenario']])
    observed['effects']['workflow']['read_failures_consumed'] = []
    row = {'outcome': 'fail', 'primary_outcome': 'fail',
           'checks': paired_cases.assess(observed, case.oracle)}
    assert paired_report._workflow_exposure(row, case.record())['rule'] == 'workflow_fault_exposure_unavailable'
    row['checks']['semantic:task.json'] = False
    assert paired_report._workflow_exposure(row, case.record()) is None


def plan(tmp_path, monkeypatch, *, supported=True, repetitions=3):
    config, policy = tmp_path / 'config.json', tmp_path / 'policy.json'
    config.write_text(json.dumps({'providers': {'candidate': {'api_key': 'PRIVATE_TEST_KEY'}}}))
    policy.write_text(json.dumps({'version': 'paired-policy-1', 'budget_mode': 'matched_work',
        'budget_policy': {'description': 'Frozen workflow budgets; include all observed auxiliary work'},
        'environment': {'endpoint_usage': 'idle_declared'}}))
    monkeypatch.setattr(paired_container, 'configuration', lambda *args, **kw: (read(config), {
        'container': {'image_id': 'sha256:' + 'a' * 64},
        'container_payload': {'workflow_protocol': PROTOCOL if supported else None}, 'native_runtime': {}}))
    directory = tmp_path / 'run'
    manifest = paired.plan(directory, native_config=config, native_binding='candidate',
        comparison_policy=policy, container_image='sha256:' + 'a' * 64,
        dataset_version=paired_cases.WORKFLOW_VERSION, repetitions=repetitions)
    return directory, manifest


def test_full_release_plan_and_unrun_export_keep_twelve_workflows_seventy_two_attempts(tmp_path, monkeypatch):
    directory, manifest = plan(tmp_path, monkeypatch)
    assert manifest['declared_attempts'] == 72 and len(manifest['pairs']) == 36
    assert len(manifest['dataset']['episode_ids']) == 12
    assert manifest['dataset']['split'] == 'frozen_public_evaluation'
    assert len({m['path'] for p in manifest['pairs'] for m in p['arms'].values()}) == 72
    assert all(manifest['pairs'][i]['order'] == list(reversed(manifest['pairs'][i + 12]['order'])) for i in range(12))
    metadata = {'publication_scope': 'public_synthetic', 'deployment': {
        'id': 'candidate', 'model': 'Synthetic test model', 'profile': 'Frozen workflows'}}
    exported = paired_public.export_record(directory, metadata)
    assert exported['quality_status'] == exported['dataset']['split'] == 'frozen_public_evaluation'
    assert exported['paired_score'] is None
    assert exported['workflow_repetitions']['unique_workflows'] == 12
    raw = json.dumps(exported)
    assert 'PRIVATE_TEST_KEY' not in raw and 'initial_files' not in raw and 'oracle' not in raw


def test_report_exposure_projection_preserves_raw_failure_and_separate_dimensions(tmp_path, monkeypatch):
    directory, manifest = plan(tmp_path, monkeypatch, repetitions=1)
    inventory = {c.id: c for c in cases()}
    target = next(c.id for c in inventory.values() if c.inputs['workflow']['read_failures'])
    def observed_row(_directory, _manifest, member):
        case = inventory[member['case']['id']]
        observed = receipt(case, EXAMPLES[case.inputs['scenario']])
        if case.id == target and member['case']['inputs']['arm'] == 'protagine':
            observed['effects']['workflow']['read_failures_consumed'] = []
        checks = paired_cases.assess(observed, case.oracle)
        status = 'pass' if all(checks.values()) else 'fail'
        return {'case_id': case.id, 'outcome': status, 'primary_outcome': status,
                'checks': checks, 'elapsed_ms': 100, 'effects': observed['effects']}
    monkeypatch.setattr(paired_report, '_row', observed_row)
    report = paired_report.summarize(directory)
    assert report['paired_score'] is None and report['comparable_pairs'] == 11
    pair = next(p for p in report['pairs'] if p['episode_id'] == target)
    assert pair['results']['protagine']['outcome'] == 'fail'
    assert pair['completion']['protagine'] is None
    repeat = next(w for w in report['workflow_repetitions']['workflows'] if w['workflow_id'] == target)
    assert repeat['dimensions']['semantic']['protagine'] == {'passed_repetitions': 1, 'observed_repetitions': 1}
    assert repeat['dimensions']['lifecycle']['protagine']['passed_repetitions'] == 0
    assert report['workflow_repetitions']['strata']['memory_condition']['irrelevant']['unique_workflows'] == 4
    metadata = {'publication_scope': 'public_synthetic', 'deployment': {
        'id': 'candidate', 'model': 'Synthetic test model', 'profile': 'Frozen workflows'}}
    exported = paired_public.export_record(directory, metadata)
    row = next(e for e in exported['episodes'] if e['episode_id'] == target)
    assert row['results']['protagine']['workflow_exposure_projection']['rule'] == 'workflow_fault_exposure_unavailable'


def test_older_image_cannot_silently_skip_restarts(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='process-restart workflow support'):
        plan(tmp_path, monkeypatch, supported=False)
    assert not (tmp_path / 'run').exists()
