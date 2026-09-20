"""A plausible answer cannot stand in for observed shared-work behavior."""
import json
from copy import deepcopy

import pytest

from protagine.qualification.native_unified import assess
from protagine.qualification.native_unified_cases import cases


def effects():
    return {'gateway_connected': True, 'distinct_sessions': True,
        'durable_before': [{'task_id': 'task-1'}], 'durable_after': [{'task_id': 'task-1'}],
        'foreground_before_release': True, 'foreground_requests': ['task-1 commitment-1'],
        'bootstrap_requests': ['commitment-1'], 'commitment_id': 'commitment-1',
        'commitment_status': 'pending', 'request_observations': [{'truncated': False}]}


@pytest.mark.parametrize('case', cases(), ids=lambda case: case.id)
def test_answer_only_fails_every_protagine_scenario(case):
    assert not all(assess({'output': json.dumps(case.oracle['answer'])}, case.oracle).values())


def test_shared_task_and_commitment_need_two_physical_contexts():
    case = cases()[0]
    observed = {'output': json.dumps(case.oracle['answer']), 'effects': effects()}
    assert all(assess(observed, case.oracle).values())
    for field, value in [('durable_before', []), ('durable_after', []), ('bootstrap_requests', []),
                         ('foreground_requests', []), ('foreground_before_release', False)]:
        changed = deepcopy(observed)
        changed['effects'][field] = value
        assert not all(assess(changed, case.oracle).values())


def test_control_ack_does_not_establish_applied_correction():
    case = cases()[2]
    effect = {**effects(), 'updates': [{'stage': 'native_control_acknowledged'}],
              'worker_requests': ['violet-953'], 'worker_result': {'label': 'amber-462', 'units': 3}}
    observed = {'output': json.dumps(case.oracle['answer']), 'effects': effect}
    assert not assess(observed, case.oracle)['corrected_worker_result']
    effect['worker_result']['label'] = 'violet-953'
    assert all(assess(observed, case.oracle).values())


def test_stop_requires_finalization_and_duplicate_suppression():
    case = cases()[3]
    effect = {**effects(), 'stop_status': 'stopping', 'worker_response_absent': True,
              'duplicate_suppressed': True}
    observed = {'output': json.dumps(case.oracle['answer']), 'effects': effect}
    assert not assess(observed, case.oracle)['stop_durably_finalized']
    effect['stop_status'] = 'cancelled'
    assert all(assess(observed, case.oracle).values())
    effect['worker_response_absent'] = False
    assert not all(assess(observed, case.oracle).values())


def test_base_arms_are_distinct_unknown_grounding_controls():
    base = cases(arm='base_hermes')
    assert len(base) == 2
    assert all(case.oracle['answer']['background_task_exists'] is None for case in base)
    assert not any(case.inputs['scenario'] in {'stop', 'steer'} for case in base)
    assert not set(case.id for case in base) & set(case.id for case in cases())
    base[0].inputs.clear()
    assert cases(arm='base_hermes')[0].inputs
