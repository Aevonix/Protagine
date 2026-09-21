"""Protect denominators, oracle isolation and model-independent probability grading."""
import pytest

from protagine.qualification.decision_screen import evaluate, report, request_for, request_hash


def case(identity='base-1', kind='base'):
    return {'id': identity, 'group_id': 'group-1', 'family': 'evidence_completion', 'kind': kind,
        'state': {'request': 'Was the file written?', 'current_context': [
            {'id': 'receipt-1', 'text': 'write_file succeeded; read-back matches'}], 'candidate_memories': []},
        'question': {'type': 'choice', 'instructions': 'Choose the observed status.',
                     'criteria': {'complete': 'verified complete', 'unknown': 'no evidence'}},
        'expected_label': 'complete', 'rationale': 'ORACLE_ONLY_DO_NOT_SEND',
        'evidence_question': {'type': 'choice', 'instructions': 'Which evidence supports the status?',
                              'criteria': {'a': 'receipt-1', 'none': 'no source'}},
        'expected_evidence_label': 'a', 'evidence_ids': ['receipt-1']}


def record(c, correct=True):
    return {'case_id': c['id'], 'request_sha256': request_hash(c), 'status': 'ok',
        'input_truncated': False, 'latency_ms': 20,
        'answers': {'decision': {'choice': 'complete' if correct else 'unknown',
                    'probabilities': {'complete': .9 if correct else .1, 'unknown': .1 if correct else .9},
                    'confidence': .123},
                    'evidence': {'choice': 'a', 'probabilities': {'a': 1, 'none': 0}}}}


BACKEND = {'id': 'fake', 'revision': 'fixture-1', 'latency_boundary': 'test only'}


def test_model_request_never_contains_oracles():
    c = case()
    request = request_for(c)
    assert set(request) == {'state', 'questions'}
    assert set(request['questions']) == {'decision', 'evidence'}
    assert 'ORACLE_ONLY' not in str(request)
    c['rationale'] = 'changed grade notes'
    assert request_hash(c) == request_hash(case())
    c['state']['request'] = 'New task'
    assert request_hash(c) != request_hash(case())


def test_uses_probabilities_not_provider_entropy_confidence():
    c = case()
    graded = evaluate(c, record(c))
    assert graded['confidence'] == .9
    assert graded['brier'] == pytest.approx(.02)
    assert graded['joint_correct'] is True


def test_missing_and_failed_attempts_cannot_inflate_accuracy():
    cases = [case('a'), case('b'), case('c')]
    error = record(cases[1])
    error.update(status='error')
    result = report(cases, [record(cases[0]), error], backend=BACKEND, dataset_sha256='fixture')
    assert result['base']['accuracy'] == 1/3
    assert result['base']['valid'] == 1
    assert result['complete'] is False
    assert result['base']['statuses'] == {'error': 1, 'missing': 1, 'ok': 1}


@pytest.mark.parametrize('mutation', [
    {'probabilities': {'complete': float('nan'), 'unknown': .1}},
    {'probabilities': {'complete': .9, 'unknown': .9}},
    {'probabilities': {'wrong_label': 1}},
    {'choice': 'unknown'},
])
def test_invalid_distribution_or_argmax_is_a_failed_attempt(mutation):
    c = case()
    r = record(c)
    r['answers']['decision'].update(mutation)
    assert evaluate(c, r)['status'] == 'invalid_answer'


def test_probability_rounding_is_accepted_without_changing_choice():
    c = case()
    c['question']['criteria'] = {'complete': '', 'unknown': '', 'partial': ''}
    r = record(c)
    r['answers']['decision']['probabilities'] = dict.fromkeys(c['question']['criteria'], .3333)
    result = evaluate(c, r)
    assert result['status'] == 'ok'
    assert result['confidence'] == pytest.approx(1/3)


def test_source_choice_is_graded_independently():
    c = case()
    r = record(c)
    r['answers']['evidence'] = {'choice': 'none', 'probabilities': {'a': 0, 'none': 1}}
    result = evaluate(c, r)
    assert result['correct'] is True
    assert result['joint_correct'] is False


def test_missing_evidence_does_not_erase_a_valid_decision():
    c = case()
    r = record(c)
    del r['answers']['evidence']
    result = evaluate(c, r)
    assert result['correct'] is True
    assert result['confidence'] == .9
    assert result['evidence_status'] == 'invalid_answer'
    assert result['joint_correct'] is False


def test_invalid_decision_does_not_erase_valid_evidence():
    c = case()
    r = record(c)
    del r['answers']['decision']
    result = evaluate(c, r)
    assert result['status'] == 'invalid_answer'
    assert result['evidence_correct'] is True
    assert result['joint_correct'] is False


def test_rejects_retries_and_changed_inputs():
    c = case()
    r = record(c)
    with pytest.raises(ValueError, match='Duplicate'):
        report([c], [r, r], backend=BACKEND, dataset_sha256='fixture')
    r['request_sha256'] = 'different-prompt'
    with pytest.raises(ValueError, match='frozen input'):
        evaluate(c, r)


def test_known_truncation_is_not_a_valid_success():
    c = case()
    r = record(c)
    r['input_truncated'] = True
    assert evaluate(c, r)['status'] == 'input_truncated'


def test_unknown_input_coverage_does_not_fabricate_a_wrong_answer():
    c = case()
    r = record(c)
    r['input_truncated'] = None
    result = report([c], [r], backend=BACKEND, dataset_sha256='fixture')
    assert result['base']['accuracy'] == 1
    assert result['base']['complete_input_comparison'] is False
    assert result['base']['input_coverage_verified'] == 0


def test_overflow_accepts_either_budget_rejection_or_correct_full_input():
    c = case(kind='overflow')
    r = record(c)
    assert evaluate(c, r)['safe_overflow'] is True
    r.update(status='input_rejected', rejection_reason='input_would_truncate')
    assert evaluate(c, r)['safe_overflow'] is True
    r['input_truncated'] = None
    assert evaluate(c, r)['safe_overflow'] is False
    r['input_truncated'] = False
    r['rejection_reason'] = 'server_failed'
    assert evaluate(c, r)['safe_overflow'] is False


def test_perturbations_do_not_increase_independent_case_denominator():
    parent = case()
    child = case('variant', 'perturbation')
    child['perturbation'] = {'parent_id': parent['id'], 'type': 'source_order'}
    result = report([parent, child], [record(parent), record(child, False)],
                    backend=BACKEND, dataset_sha256='fixture')
    assert result['base']['count'] == 1
    assert result['base']['accuracy'] == 1
    assert result['perturbations']['accuracy'] == 0
    assert result['perturbations']['pairs'][0]['both_correct'] is False
