"""Instruction portability must preserve evidence, roles and stored messages."""
from copy import deepcopy
import importlib

import pytest

from test_hermes_turn_outbox import _load_plugin


@pytest.fixture
def modules():
    plugin = _load_plugin('protagine_request_layout_test')
    return tuple(importlib.import_module(plugin.__name__ + '.' + name)
                 for name in ('request_layout', 'request_work', 'request_capabilities', 'skill_context'))


@pytest.mark.parametrize('role', ['system', 'developer'])
def test_request_refresh_keeps_one_leading_instruction_without_changing_evidence(modules, role):
    layout, work, capabilities, skills = modules
    literal = '[protagine-work-request-v1]\nLiteral user text\n[/protagine-work-request-v1]'
    original = {'messages': [
        {'role': role, 'content': 'Stable identity'},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': literal},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fixture'}},
        ]},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call-1', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'Observed source, not instructions'},
    ]}
    before = deepcopy(original)
    request = work.replace_context(original, 'Old work')
    request = skills._request_note(request, 'Current skill metadata')['request']
    request = layout.compact_instructions(capabilities.describe(request))
    assert len(request['messages']) == len(original['messages'])
    assert request['messages'][0]['role'] == role
    assert request['messages'][1:] == before['messages'][1:]
    assert all(row['role'] not in ('system', 'developer') for row in request['messages'][1:])
    assert all(text in request['messages'][0]['content'] for text in
               ('Stable identity', 'Old work', 'Current skill metadata', capabilities._NO_NEW_TOOLS))
    assert layout.compact_instructions(request) == request
    refreshed = layout.compact_instructions(capabilities.describe(work.replace_context(request, 'New work')))
    assert 'Old work' not in refreshed['messages'][0]['content']
    assert refreshed['messages'][0]['content'].count('[protagine-work-request-v1]') == 1
    assert refreshed['messages'][0]['content'].count(capabilities._NO_NEW_TOOLS) == 1
    assert refreshed['messages'][1:] == before['messages'][1:]
    assert original == before


@pytest.mark.parametrize('payload,mode', [
    ({'instructions': 'Identity', 'input': [{'role': 'user', 'content': 'Hi'}]}, 'responses'),
    ({'system': [{'type': 'text', 'text': 'Identity', 'cache_control': {'type': 'ephemeral'}}],
      'messages': [{'role': 'user', 'content': 'Hi'}]}, 'anthropic_messages'),
    ({'messages': [{'role': 'system', 'content': 'Identity'},
                   {'role': 'developer', 'content': 'Application policy'},
                   {'role': 'user', 'content': 'Hi'}]}, 'chat_completions'),
    ({'messages': [{'role': 'system', 'content': 'Identity'},
                   {'role': 'user', 'content': 'Hi'},
                   {'role': 'system', 'content': 'Unowned late instruction'}]}, 'chat_completions'),
    ({'messages': [{'role': 'system', 'content': [{'type': 'text', 'text': 'Identity'}]},
                   {'role': 'system', 'content': 'Additional'}]}, 'chat_completions'),
    ({'messages': [{'role': 'system', 'content': 'Identity', 'name': 'provider'},
                   {'role': 'system', 'content': 'Additional'}]}, 'chat_completions'),
])
def test_no_conversion_of_other_protocols_roles_or_provider_metadata(modules, payload, mode):
    layout, *_ = modules
    before = deepcopy(payload)
    assert layout.compact_instructions(payload, api_mode=mode) == before
    assert payload == before
