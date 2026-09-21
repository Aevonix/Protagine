"""Reviewed-2 clarifies two output contracts without changing answers or old data."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from protagine.qualification import paired_cases as paired


DIRECTORY = Path(paired.__file__).parent / 'fixtures'


def baseline(arm='base_hermes'):
    return paired.cases(arm, dataset_version=paired.BASELINE_VERSION)


def test_baseline_changes_only_two_instructions_and_keeps_prior_fixture_bytes():
    previous = DIRECTORY / paired.REVIEWED_VERSION
    assert hashlib.sha256((previous / 'scenarios.json').read_bytes()).hexdigest() == (
        'd27806af21aaa29ec653cbbacfb7ff90029d7b0137bb095c4b1254c89d70b5de')
    manifest, cases, checksum = paired.load_dataset(DIRECTORY / paired.BASELINE_VERSION)
    _, old, old_checksum = paired.load_dataset(previous)
    assert len(cases) == len(old) == manifest['scenario_count'] == 60
    assert checksum != old_checksum
    assert manifest['derived_from'] == {
        'dataset_id': paired.REVIEWED_VERSION,
        'manifest_sha256': hashlib.sha256((previous / 'manifest.json').read_bytes()).hexdigest()}
    changed = []
    for before, after in zip(old, cases):
        if before != after:
            changed.append(after['id'])
            normalized = deepcopy(after)
            normalized['episodes'] = before['episodes']
            assert normalized == before
            for first, second in zip(before['episodes'], after['episodes']):
                assert first['session_id'] == second['session_id']
    assert changed == ['paired.persistent-memory.separate-peoples-preferences',
                       'paired.planning-toolrecovery.independent-resources-critical-path']


def test_both_baseline_arms_keep_identical_inputs_oracles_and_existing_budgets():
    old = paired.cases('base_hermes', dataset_version=paired.REVIEWED_VERSION)
    for before, left, right in zip(old, baseline(), baseline('protagine')):
        assert left.version == right.version == paired.BASELINE_VERSION
        a, b = deepcopy(left.inputs), deepcopy(right.inputs)
        assert a.pop('arm') == 'base_hermes' and b.pop('arm') == 'protagine'
        assert a == b and before.oracle == left.oracle == right.oracle
        assert before.timeout_seconds == left.timeout_seconds == right.timeout_seconds
        assert before.max_output_bytes == left.max_output_bytes == right.max_output_bytes
        for name in ('max_output_tokens', 'max_iterations', 'settle_seconds', 'cleanup_seconds'):
            assert before.inputs[name] == left.inputs[name]


@pytest.mark.parametrize('scenario,path,valid,invalid', [
    ('separate-peoples-preferences', 'refreshments.json',
     {'people': {'Mira': {'drink': 'water', 'avoid': ['peanuts']},
                 'Rowan': {'drink': 'coffee', 'avoid': []}}},
     {'Mira': {'drink': 'water', 'avoid': ['peanuts']},
      'Rowan': {'drink': 'coffee', 'avoid': []}}),
    ('independent-resources-critical-path', 'pipeline.json',
     {'tasks': [{'id': 'A', 'start': 0, 'finish': 2},
                {'id': 'B', 'start': 0, 'finish': 3},
                {'id': 'C', 'start': 2, 'finish': 4},
                {'id': 'D', 'start': 4, 'finish': 5}]},
     {'tasks': [{'source_id': 'A', 'start': 0, 'finish': 2},
                {'source_id': 'B', 'start': 0, 'finish': 3},
                {'source_id': 'C', 'start': 2, 'finish': 4},
                {'source_id': 'D', 'start': 4, 'finish': 5}]}),
])
def test_explicit_contract_accepts_correct_shape_and_rejects_previous_ambiguity(
        scenario, path, valid, invalid):
    case = next(c for c in baseline() if c.inputs['scenario'] == scenario)
    user = case.inputs['episodes'][-1]['user']
    assert 'top-level property named ' + ('people' if scenario.startswith('separate') else 'tasks') in user
    for value, passes in ((valid, True), (invalid, False)):
        effects = {'turns_completed': len(case.inputs['episodes']),
                   'declared_turns': len(case.inputs['episodes']),
                   'artifacts': {path: json.dumps(value)}}
        assert all(paired.assess({'effects': effects}, case.oracle).values()) is passes


def test_baseline_preserves_semantic_failures_despite_valid_shape():
    case = next(c for c in baseline() if c.inputs['scenario'] == 'separate-peoples-preferences')
    stale = {'people': {'Mira': {'drink': 'tea', 'avoid': ['peanuts']},
                        'Rowan': {'drink': 'coffee', 'avoid': []}}}
    effects = {'turns_completed': 3, 'declared_turns': 3,
               'artifacts': {'refreshments.json': json.dumps(stale)}}
    assert not all(paired.assess({'effects': effects}, case.oracle).values())
