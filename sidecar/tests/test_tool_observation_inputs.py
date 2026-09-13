"""Executed inputs stay exact evidence within the original retention budget."""
import hashlib
import json

import pytest
from pydantic import ValidationError

from pacomind.turns.tool_observations import MAX_BYTES, NativeToolInput, ToolObservation


def observation(content, arguments):
    encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=True,
                         separators=(',', ':'), allow_nan=False).encode()
    return dict(native=dict(profile_id='a'*64, session_id='session', task_id='task', turn_id='turn',
        tool_call_id='call', api_request_id='request', tool_name='fixture', message_id=1,
        timestamp=1234567890.0, result_sha256=hashlib.sha256(content.encode()).hexdigest()),
        content=content, reason='The model proposes this may help reproduce the result.',
        origin={'source_id':'instruction', 'source_version':'b'*64},
        input={'arguments':arguments, 'arguments_sha256':hashlib.sha256(encoded).hexdigest()})


def test_input_and_result_share_the_existing_utf8_budget():
    args = {'path': 'copper', 'settings': {'seed': 42, 'caption': 'é'}}
    template = observation('ok', args)
    input_size = len(NativeToolInput(**template['input']).encoded())
    content = 'x' * (MAX_BYTES-input_size)
    accepted = ToolObservation(**observation(content, args))
    assert accepted.content == content and accepted.input.arguments == args
    with pytest.raises(ValidationError, match='exceed_budget'):
        ToolObservation(**observation(content+'é', args))


@pytest.mark.parametrize('change', [
    {'arguments': ['not', 'an', 'object']},
    {'arguments': {'seed': 99}},
    {'arguments_sha256': '0'*64},
    {'arguments': {'seed': float('nan')}},
])
def test_unverified_or_malformed_inputs_are_rejected(change):
    value = observation('ok', {'seed':42})
    value['input'].update(change)
    with pytest.raises(ValidationError):
        ToolObservation(**value)
