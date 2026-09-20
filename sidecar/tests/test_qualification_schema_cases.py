import asyncio
import json
from types import SimpleNamespace

import httpx

from protagine.qualification.schema_cases import CASES, assess, consume


def test_format_semantics_and_wire_contract_are_independent():
    case = CASES[0]
    effects = {'serialized_requests': [{'response_format_matches': True}]}
    answer = case.oracle['answer']
    assert all(assess({'output': json.dumps(answer), 'effects': effects}, case.oracle).values())
    wrong = {**answer, 'owner': 'invented'}
    graded = assess({'output': json.dumps(wrong), 'effects': effects}, case.oracle)
    assert graded['schema_valid'] and not graded['grounded_fields']
    assert not assess({'output': json.dumps(answer), 'effects': {}}, case.oracle)['schema_in_serialized_request']
    assert not assess({'output': '```json\n'+json.dumps(answer)+'\n```', 'effects': effects}, case.oracle)['valid_json']
    for case in CASES:
        assert all(assess({'output': json.dumps(case.oracle['answer']), 'effects': effects}, case.oracle).values())
        case.record()


def test_observer_sees_actual_http_body_and_does_not_inject_expected_answer():
    case = CASES[0]
    seen = []
    class Router:
        async def complete(self, messages, context):
            assert 'allow_fallback' in context and context['allow_fallback'] is False
            body = {'messages': messages, 'response_format': {'type': 'json_schema',
                'json_schema': {**context['response_schema'], 'strict': True}}}
            async def respond(request):
                seen.append(json.loads(request.content))
                return httpx.Response(200, json={'ok': True})
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                await client.post('http://localhost/v1/chat/completions', json=body)
            return SimpleNamespace(content='{}')
    result = asyncio.run(consume(case.inputs, SimpleNamespace(router=Router())))
    assert result['effects']['serialized_requests'][0]['response_format_matches'] is True
    assert seen[0]['messages'] == case.inputs['messages']
    assert result['output'] == '{}'
