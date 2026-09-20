from copy import deepcopy
from protagine.qualification.native_memory_diagnostics import narrative_attribution


def sample(predicate):
    return {'effects': {'formation': {'jobs': [{'turn_id': 'fiction', 'status': 'complete'}],
        'claims_before': [{'source_id': 'fiction', 'subject': 'I', 'subject_key': 'speaker',
                          'predicate': predicate, 'value': 'silver moonboat',
                          'evidence': 'In a story I am drafting, I own a silver moonboat.'}]}}}


def test_explicit_fiction_is_not_false_real_world_ownership():
    observed = sample('owns in story')
    original = deepcopy(observed)
    result = narrative_attribution(observed, 'fiction')
    assert result['attribution_preserved'] is True
    assert result['v1_zero_promoted_claims'] is False
    assert result['memory_usefulness'] == 'not_assessed'
    assert observed == original


def test_source_quote_alone_does_not_rescue_unqualified_ownership():
    assert narrative_attribution(sample('owns'), 'fiction')['attribution_preserved'] is False


def test_ambiguous_paraphrase_and_missing_evidence_stay_unknown():
    assert narrative_attribution(sample('imagines possessing'), 'fiction')['attribution_preserved'] is None
    assert narrative_attribution({}, 'fiction')['attribution_preserved'] is None


def test_rejected_fiction_preserves_attribution_without_claim():
    observed = sample('owns')
    observed['effects']['formation']['claims_before'] = []
    assert narrative_attribution(observed, 'fiction')['attribution_preserved'] is True
