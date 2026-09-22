"""Evidence oracles reject fabricated facts and code-fence substring fishing."""
import pytest

from protagine.qualification.evidence_cases import CASES, evidence_fields, long_document


def test_fixtures_grade_only_independent_expected_fields():
    for case in CASES:
        record = case.record()
        assert record['boundary'] == 'role_completion'
        expected = case.oracle['fields'][0]['equals']
        assert all(evidence_fields({'output': expected}, case.oracle).values())
        changed = {**expected, next(iter(expected)): 'fabricated'}
        assert not all(evidence_fields({'output': changed}, case.oracle).values())


def test_semantic_format_accepts_one_complete_fence_not_prose_or_reasoning():
    oracle = {'fields': [{'name': 'answer', 'path': ['output', 'value'], 'equals': 7}]}
    assert evidence_fields({'output': '```json\n{"value":7}\n```'}, oracle) == {'answer': True}
    assert evidence_fields({'output': '<think>{"value":7}</think>'}, oracle) == {'output_is_json': False}
    assert evidence_fields({'output': 'Possibly {"value":7}'}, oracle) == {'output_is_json': False}
    assert evidence_fields({'output': '{"value":true}'}, oracle) == {'answer': False}


def test_evidence_positions_are_exact_and_background_contains_no_answer():
    for position in (0, 511, 1023):
        document = long_document({position: 'TARGET evidence'})
        assert document.splitlines()[position] == 'TARGET evidence'
        assert document.count('TARGET evidence') == 1
        assert len(document.splitlines()) == 1024
    with pytest.raises(ValueError):
        long_document({1024: 'outside'})


def test_development_inventory_has_no_execution_claim_or_private_holdout():
    assert len(CASES) == 15
    assert len({case.id for case in CASES}) == 15
    assert all(case.provenance == 'public' and case.boundary == 'role_completion' for case in CASES)
