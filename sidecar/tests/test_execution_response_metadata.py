"""Native-normalized callback metadata crosses the real schema and registry."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from protagine.api.routers.executions import ExecutionObservation, ExecutionRuntimeObservation
from protagine.turns import TurnIdempotencyLedger
from protagine.turns.executions import ExecutionRegistry


@pytest.fixture
def observer_module():
    path = Path(__file__).resolve().parents[2] / 'plugins/hermes-plugin/executions.py'
    spec = importlib.util.spec_from_file_location('response_metadata_observer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_response_callbacks_validate_and_retain_distinct_requests(observer_module, tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_EXPECTATIONS', 'off')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    ledger = TurnIdempotencyLedger(tmp_path / 'turns.db')
    registry = ExecutionRegistry(ledger, clock=lambda: 1_800_000_010.0)
    sent = []

    class Client:
        def post(self, path, **kwargs):
            assert path == '/v1/host/executions/observe'
            # Match the real HTTP endpoint's typed-body -> model_dump -> registry path.
            body = ExecutionObservation.model_validate(kwargs['json'])
            assert registry.observe(body.model_dump(), principal_id='host', contact_id='owner')['accepted']
            sent.append(body)
            return NS(raise_for_status=lambda: None)

    observer = observer_module.ExecutionObserver(Client())
    scope = NS(valid_participant=True, contact_id='owner', platform='whatsapp')
    identity = {'session_id': 'session', 'turn_id': 'turn'}
    observer.start(scope, **identity)
    secret = 'PRIVATE-REASONING https://private.invalid/credential'
    for number in (1, 2):
        common = dict(identity, api_request_id=f'request-{number}', model='requested-alias',
                      provider='local', api_mode='chat_completions', api_call_count=number,
                      retry_count=number-1, started_at=1_800_000_000.0+number)
        observer.api('start', **common, request={'body': {}}, finish_reason='ignored-on-start',
                     assistant_content_chars=999, assistant_message={'reasoning': secret})
        message = (NS(content=secret, reasoning=secret, reasoning_content=secret,
                      reasoning_details=[{'text': secret}], tool_calls=[
                          NS(function=NS(name='read_file', arguments=secret)),
                          NS(function=NS(name='')), {'function': {}},
                      ]) if number == 1 else {'content': secret, 'reasoning': '',
                                            'reasoning_content': '', 'tool_calls': []})
        observer.api('response', **common, response_model=f'served-{number}',
                     ended_at=1_800_000_005.0+number, finish_reason='tool_calls' if number == 1 else 'stop',
                     assistant_content_chars=0 if number == 1 else 7,
                     assistant_tool_call_count=3 if number == 1 else 0,
                     api_duration=4.25 if number == 1 else 0,
                     first_chunk_at=1_800_000_001.5+number, assistant_message=message,
                     response={'private': secret}, base_url='https://private.invalid',
                     raw_response=secret)
    observer.end(**identity, completed=True)
    assert len(sent) == 6  # Schema/registry failure cannot disappear inside best-effort delivery.
    with closing(ledger._connect()) as db:
        retained = db.execute('SELECT metadata_json FROM execution_runtime_observations').fetchone()[0]
    assert secret not in retained and 'https://' not in retained
    data = json.loads(retained)
    assert data['incomplete'] is False
    assert set(data['requests']) == {'request-1', 'request-2'}
    first = data['requests']['request-1']['response']
    second = data['requests']['request-2']['response']
    assert first['response_model'] == 'served-1' and second['response_model'] == 'served-2'
    assert first['finish_reason'] == 'tool_calls' and second['finish_reason'] == 'stop'
    assert first['assistant_content_chars'] == 0 and second['assistant_content_chars'] == 7
    assert first['assistant_reasoning_chars'] == first['assistant_reasoning_content_chars'] == len(secret)
    assert first['assistant_reasoning_details_count'] == 1
    assert first['assistant_invalid_tool_name_count'] == 2
    assert second['assistant_reasoning_chars'] == second['assistant_reasoning_content_chars'] == 0
    assert second['assistant_invalid_tool_name_count'] == 0
    assert second['assistant_reasoning_details_count'] is None
    assert first['api_duration'] == 4.25 and second['api_duration'] == 0
    assert first['first_chunk_at'] == 1_800_000_002.5
    assert data['requests']['request-1']['start']['assistant_content_chars'] is None
    json.dumps(data, allow_nan=False)


@pytest.mark.parametrize('values', [
    {},
    {'finish_reason': '', 'assistant_content_chars': True, 'assistant_tool_call_count': False,
     'api_duration': float('nan'), 'first_chunk_at': float('inf')},
    {'finish_reason': 'x'*65, 'assistant_content_chars': -1, 'assistant_tool_call_count': 1.5,
     'api_duration': -1, 'first_chunk_at': 0},
    {'finish_reason': 'stop\x7f', 'assistant_content_chars': '0',
     'assistant_tool_call_count': 2147483648, 'api_duration': True, 'first_chunk_at': False},
    {'finish_reason': 'stop\x85', 'api_duration': 10**400, 'first_chunk_at': '123'},
    {'finish_reason': ['stop'], 'assistant_message': {'reasoning': 9, 'reasoning_content': [],
                                                   'reasoning_details': {}, 'tool_calls': ()}},
])
def test_missing_invalid_response_fields_stay_unknown(observer_module, values):
    metadata = observer_module.ExecutionObserver.response_metadata(values)
    assert metadata == {}
    parsed = ExecutionRuntimeObservation.model_validate({'event': 'response', **metadata})
    assert parsed.assistant_content_chars is None
    assert parsed.assistant_invalid_tool_name_count is None


@pytest.mark.parametrize('field', [
    'assistant_content_chars', 'assistant_tool_call_count', 'assistant_reasoning_chars',
    'assistant_reasoning_content_chars', 'assistant_reasoning_details_count',
    'assistant_invalid_tool_name_count',
])
@pytest.mark.parametrize('value', [True, False, -1, 1.5, '1', 2147483648, float('inf')])
def test_schema_rejects_noninteger_or_unbounded_counts(field, value):
    with pytest.raises(ValidationError):
        ExecutionRuntimeObservation.model_validate({'event': 'response', field: value})


@pytest.mark.parametrize('field,value', [
    ('finish_reason', ''), ('finish_reason', 'x'*65), ('finish_reason', 'stop\n'),
    ('finish_reason', 'stop\x7f'), ('finish_reason', 'stop\x85'), ('finish_reason', True),
    ('api_duration', True), ('api_duration', '2.5'), ('api_duration', -1),
    ('api_duration', float('nan')), ('api_duration', float('inf')),
    ('first_chunk_at', False), ('first_chunk_at', 0), ('first_chunk_at', -1),
    ('first_chunk_at', float('-inf')), ('first_chunk_at', float('nan')),
])
def test_schema_rejects_invalid_response_scalars(field, value):
    with pytest.raises(ValidationError):
        ExecutionRuntimeObservation.model_validate({'event': 'response', field: value})


def test_legacy_callbacks_and_unknown_raw_objects(observer_module):
    for event in ('start', 'response', 'error'):
        parsed = ExecutionRuntimeObservation.model_validate({'event': event})
        assert parsed.finish_reason is None and parsed.api_duration is None
    with pytest.raises(ValidationError):
        ExecutionRuntimeObservation.model_validate({'event': 'response', 'assistant_message': {'content': 'private'}})
    metadata = observer_module.ExecutionObserver.response_metadata({
        'assistant_message': NS(reasoning=None, reasoning_content='', reasoning_details=[], tool_calls=[]),
    })
    assert metadata == {'assistant_reasoning_content_chars': 0,
                        'assistant_reasoning_details_count': 0, 'assistant_invalid_tool_name_count': 0}
