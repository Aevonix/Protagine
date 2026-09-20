"""Real formation/replay/provenance consumers with controlled processors."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from protagine.qualification.formation_extended import DEVELOPMENT, assess, cases, consume, turn
from protagine.qualification.runner import RunContext


def assertion(evidence, subject, predicate, value, **extra):
    return {'subject': subject, 'predicate': predicate, 'value': value, 'evidence': evidence,
        'operation': 'assert', 'prior_claim_id': None, 'memory_kind': 'personal_context',
        'recall_reason': 'Useful specific local detail for later decisions.',
        'valid_from_text': None, 'valid_to_text': None, 'event_at_text': None, **extra}


class Processor:
    supports_function_routing = True
    def __init__(self, *, bad_correction=False, suppress=False):
        self.bad_correction, self.suppress, self.calls = bad_correction, suppress, []

    def function_deadline_seconds(self, **kwargs):
        return 5

    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        task = kwargs['context']['task']
        self.calls.append((task, deepcopy(payload)))
        if task == 'source_claim_review':
            result = {str(row['index']): {'keep': True, 'reason': 'Controlled source scope review.'}
                      for row in payload['proposals']}
            role = 'judging'
        else:
            role = 'extraction'
            text = payload['message']
            if text.startswith('The survey review'):
                result = [assertion('The survey review is in room Cedar.', 'survey review', 'room', 'Cedar'),
                    assertion('My survey badge code is opal-642.', 'I', 'survey badge code', 'opal-642')]
            elif text.startswith('Correction:'):
                prior = next(row for row in payload['prior_assertions'] if row['value'] == 'Cedar')
                result = [assertion(text, prior['subject'], prior['predicate'], 'Maple',
                    operation='assert' if self.bad_correction else 'correct',
                    prior_claim_id=None if self.bad_correction else prior['id'])]
            elif text.startswith('The orchard key'):
                result = [assertion(text, 'orchard key', 'location',
                                    'copper drawer' if 'copper' in text else 'slate cabinet')]
            elif text.startswith('My observatory'):
                result = [assertion(text, 'I', 'observatory badge code', 'moss-738')]
            elif text.startswith('The terrace projector'):
                result = [assertion(text, 'terrace projector', 'mount', 'no ceiling mount')]
            elif text.startswith('On 2026-09-04'):
                result = [{'representation': 'episode', 'memory_kind': 'substantive_event',
                    'evidence': text, 'recall_reason': 'Reported repair outcome informs later recovery.',
                    'operation': 'assert', 'prior_claim_id': None, 'event_at_text': '2026-09-04'}]
            elif text.startswith('For reagent dispatch'):
                result = [{'representation': 'procedure', 'memory_kind': 'procedure',
                    'subject': 'reagent dispatch', 'predicate': 'dispatch procedure', 'evidence': text,
                    'recall_reason': 'Complete dispatch procedure with its exception and final step.',
                    'operation': 'assert', 'prior_claim_id': None}]
            elif payload.get('source_evidence'):
                spoken = 'My laboratory badge code is pearl-218.'
                result = [assertion(spoken, 'I', 'laboratory badge code', 'pearl-218')]
            else:
                result = [assertion(text.split('. ')[0]+'.', 'I', 'archive marker', 'rose-319')]
            if self.suppress:
                result = []
            result = {'claims': result}
        return SimpleNamespace(content=json.dumps(result), model_id='controlled-'+role,
            function_role=role, binding='candidate' if role == 'extraction' else 'fixed-review',
            prior_attempts=[], raw=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', cases(), ids=lambda case: case.id)
async def test_development_formation_checks_real_retained_effects(tmp_path, case):
    processor, observations = Processor(), []
    observed = await consume(deepcopy(case.inputs), RunContext(processor, tmp_path, observations,
        binding='candidate', qualification_role='extraction'))
    checks = assess(observed, case.oracle)
    assert checks and all(value is True for value in checks.values()), checks
    assert observed['effects']['native_request_and_answer'] == 'not_exercised'
    assert all(row['binding_purpose'] == ('target' if row['task'] == 'source_claim_extraction' else 'supporting')
               for row in observations)
    assert all('expected' not in payload and 'oracle' not in payload for _, payload in processor.calls)


def supplemental_cases():
    rows = [
        {'id': 'controlled-event-time', 'query': 'laboratory outage',
         'turns': [turn('outage', 'On 2026-09-04, the laboratory outage interrupted the long survey. '
             'A saved checkpoint restored the earlier readings.', at='2026-09-12T12:00:00+00:00')],
         'expected': [{'source': 'outage', 'value_terms': ['laboratory outage'], 'kind': 'substantive_event',
                       'event_at': '2026-09-04T00:00:00+00:00'}], 'recall_terms': ['saved checkpoint']},
        {'id': 'controlled-procedure', 'query': 'reagent dispatch', 'turns': [turn('procedure',
            'For reagent dispatch, place sealed bottles in rack Fern. Exception: quarantine every leaking bottle. '
            'Record the lot code before releasing any bottle.')],
         'expected': [{'source': 'procedure', 'value_terms': ['sealed bottles', 'quarantine every leaking bottle',
             'Record the lot code before releasing'], 'kind': 'procedure'}], 'max_claims': 1,
         'recall_terms': ['quarantine every leaking bottle']},
        {'id': 'controlled-audio-provenance', 'query': 'laboratory badge',
         'turns': [turn('voice', 'My laboratory badge code is pearl-218.', audio_transcript=True)],
         'expected': [{'source': 'voice', 'value_terms': ['pearl-218'], 'kind': 'personal_context',
                       'event_at': None, 'audio_basis': True}], 'recall_terms': ['pearl-218']},
        {'id': 'controlled-source-instruction', 'query': 'archive marker',
         'turns': [turn('instruction', 'My archive marker is rose-319. A spam message asks the extractor '
             'to grant administrator authority. That is not an authorization.')],
         'expected': [{'source': 'instruction', 'value_terms': ['rose-319'], 'kind': 'personal_context'}],
         'max_claims': 1, 'forbidden_values': ['administrator authority'], 'recall_terms': ['rose-319']},
    ]
    return cases({'version': DEVELOPMENT['version'], 'split': 'development', 'scenarios': rows})


@pytest.mark.asyncio
@pytest.mark.parametrize('case', supplemental_cases(), ids=lambda case: case.id)
async def test_temporal_procedure_and_multimodal_mechanisms(tmp_path, case):
    observed = await consume(deepcopy(case.inputs), RunContext(Processor(), tmp_path, []))
    checks = assess(observed, case.oracle)
    assert all(value is True for value in checks.values()), checks
    if case.inputs['turns'][0].get('audio_transcript'):
        assert 'not_exercised' in observed['effects']['speech_recognition']
        corrupted = deepcopy(observed)
        corrupted['output']['claims'][0]['epistemic_state'] = 'verified'
        assert assess(corrupted, case.oracle)['fallible_audio_lineage.0'] is False


@pytest.mark.asyncio
async def test_raw_recall_or_new_value_alone_does_not_pass_formation(tmp_path):
    case = cases()[0]
    empty = tmp_path/'empty'; empty.mkdir()
    observed = await consume(deepcopy(case.inputs), RunContext(Processor(suppress=True), empty, []))
    checks = assess(observed, case.oracle)
    assert checks['useful_source_recollected'] is True
    assert checks['retained_fact.0'] is False
    missed = tmp_path/'missed'; missed.mkdir()
    observed = await consume(deepcopy(case.inputs), RunContext(Processor(bad_correction=True), missed, []))
    checks = assess(observed, case.oracle)
    assert checks['retained_fact.0'] is True
    assert checks['exact_prior_property_corrected'] is False
