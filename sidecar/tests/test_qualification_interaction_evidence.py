import asyncio
import base64
from copy import deepcopy
from io import BytesIO
import json

from PIL import Image
import pytest

from protagine.qualification.interaction_evidence import CASES, CONSUMERS, EVALUATORS
from protagine.qualification.evidence_cases import evidence_fields
from protagine.qualification.records import read
from protagine.qualification.runner import evaluate, inspect_binding, router_for
from test_function_routing import config, endpoint


# Independently checked against the rendered images and authored transcript.
ANSWERS = [
    {'top_label': 'R7K2', 'caption_matches': False},
    {'first_count': 3, 'second_count': 1, 'net_change': -2, 'who_moved_them': None},
    {'sample_count': 50, 'day': 'Tuesday', 'hour_24': None, 'reservation_completed': False},
    {'resolved_contact': None, 'clarification_required': True, 'message_sent': False},
    {'selected_report': 'green', 'send_authorized': False, 'message_sent': False, 'awaiting_confirmation': True},
]


@pytest.mark.parametrize('case,answer', list(zip(CASES, ANSWERS)), ids=[c.id for c in CASES])
def test_semantics_accept_complete_fence_but_reject_fabricated_fields(case, answer):
    assert all(evidence_fields({'output': '```json\n'+json.dumps(answer)+'\n```'}, case.oracle).values())
    wrong = deepcopy(answer)
    unknown = next((key for key, value in wrong.items() if value is None), next(iter(wrong)))
    wrong[unknown] = 'invented'
    assert not all(evidence_fields({'output': json.dumps(wrong)}, case.oracle).values())
    assert not all(evidence_fields({'output': json.dumps(answer)+'\nUnrequested assertion.'}, case.oracle).values())


def test_actual_router_preserves_both_ordered_images_and_never_sends_oracle(tmp_path):
    case = CASES[1]
    expected = deepcopy(case.inputs['messages'])
    def answer(payload):
        assert payload['messages'] == expected
        assert len(payload['messages'][-1]['content']) == 3
        assert 'first_count' not in json.dumps(payload.get('response_format'))
        assert 'who_moved_them": null' not in json.dumps(payload)
        for part in payload['messages'][-1]['content'][1:]:
            with Image.open(BytesIO(base64.b64decode(part['image_url']['url'].split(',', 1)[1]))) as im:
                assert im.size == (720,450) and not im.info
        return json.dumps(ANSWERS[1])
    with endpoint(content=answer) as (url, calls):
        cfg = config(url,url)
        asyncio.run(evaluate(tmp_path/'run', inspect_binding(cfg,'deliberate'), [case], CONSUMERS,
            EVALUATORS, lambda c: router_for(cfg,'deliberate',[c]), evidence_mode='controlled',
            suite_version='interaction-evidence-transport-test'))
        result = read(next((tmp_path/'run').rglob('result.json')))
        assert result['outcome'] == 'pass'
        assert len(calls) == 1
        assert result['observations'][0]['returned_model'] == 'strong-neutral'
