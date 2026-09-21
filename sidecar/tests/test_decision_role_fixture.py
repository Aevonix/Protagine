"""Keep the small decision screen frozen, scoped and free of oracle exposure."""
from collections import Counter
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[1] / 'protagine/qualification/fixtures/decision-role-screen-1'
MANIFEST = json.loads((ROOT / 'manifest.json').read_text())
CASES = json.loads((ROOT / 'scenarios.json').read_text())
BY_ID = {case['id']: case for case in CASES}


def test_fixture_bytes_and_inventory_are_frozen():
    raw = (ROOT / 'scenarios.json').read_bytes()
    assert MANIFEST['files']['scenarios.json'] == {
        'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    assert len(CASES) == len(BY_ID) == 34
    assert Counter(c['kind'] for c in CASES) == {'base': 24, 'perturbation': 8, 'overflow': 2}
    assert Counter(c['family'] for c in CASES if c['kind'] == 'base') == {
        'memory_usefulness': 8, 'retrieval_need': 8, 'evidence_completion': 8}
    assert MANIFEST['status'] == 'frozen-before-inference'
    assert MANIFEST['source']['kind'] == 'ai_authored_synthetic'
    assert 'no independent human review' in MANIFEST['source']['description']


def test_every_answer_and_evidence_set_is_declared_and_uses_real_source_ids():
    for case in CASES:
        sources = case['state']['current_context'] + case['state']['candidate_memories']
        ids = {source['id'] for source in sources}
        assert len(ids) == len(sources)
        assert case['question']['type'] == case['evidence_question']['type'] == 'choice'
        assert case['expected_label'] in case['question']['criteria']
        assert set(case['evidence_options']) == set(case['evidence_question']['criteria'])
        assert case['evidence_options'][case['expected_evidence_label']] == sorted(case['evidence_ids'])
        assert set(case['evidence_ids']) <= ids
        assert all(set(group) <= ids for group in case['evidence_options'].values())
        wire = {'state': case['state'], 'questions': {
            'decision': case['question'], 'evidence': case['evidence_question']}}
        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value))
            return set()
        assert not keys(wire).intersection({
            'expected_label', 'expected_evidence_label', 'rationale', 'evidence_rationale', 'relevant_ids'})
        assert 'supports the classification' not in case['evidence_question']['instructions']
        assert case['evidence_rationale']


def test_parent_and_perturbations_keep_labels_and_evidence_but_change_wire_input():
    for case in CASES:
        if case['kind'] != 'perturbation':
            continue
        parent = BY_ID[case['perturbation']['parent_id']]
        assert parent['kind'] == 'base'
        assert case['group_id'] == parent['group_id'] == parent['id']
        for key in ('expected_label', 'evidence_ids', 'expected_evidence_label', 'family'):
            assert case[key] == parent[key]
        # Preserve option ordering: it is part of the fixed perturbation.
        wire = lambda c: json.dumps([c['state'], c['question'], c['evidence_question']])
        assert wire(case) != wire(parent)
        if case['perturbation']['type'] == 'option_order':
            assert list(case['question']['criteria']) == list(reversed(parent['question']['criteria']))


def test_labels_are_balanced_without_fabricating_extra_independent_cases():
    for family in ('memory_usefulness', 'retrieval_need'):
        assert sorted(Counter(c['expected_label'] for c in CASES
                              if c['kind'] == 'base' and c['family'] == family).values()) == [2, 2, 2, 2]
    assert Counter(c['expected_label'] for c in CASES
                   if c['kind'] == 'base' and c['family'] == 'evidence_completion') == {
                       'completed': 3, 'not_completed': 2, 'unknown': 3}


def test_overflows_preserve_evidence_and_allow_larger_context_models_to_answer():
    probes = [case for case in CASES if case['kind'] == 'overflow']
    assert {case['overflow']['decisive_evidence_position'] for case in probes} == {'front', 'back'}
    for case in probes:
        assert case['overflow']['expectation'] == 'reject_or_answer_without_truncation'
        assert case['expected_label'] == 'completed'
        raw = json.dumps({'state': case['state'], 'questions': {
            'decision': case['question'], 'evidence': case['evidence_question']}})
        assert 8000 < len(raw) < 16384


def test_memory_usefulness_is_candidate_selection_not_automatic_retention():
    for case in CASES:
        if case['family'] != 'memory_usefulness':
            assert 'relevant_ids' not in case
            continue
        candidates = case['state']['candidate_memories']
        assert len(candidates) == 1
        assert case['relevant_ids'] == ([candidates[0]['id']]
            if case['expected_label'] in {'useful', 'protected'} else [])
