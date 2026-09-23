"""Bounded operational context, preserving participant and request semantics."""
import copy
import importlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from protagine.turns.executions import request_work_context


def scope(**changes):
    return SimpleNamespace(valid_participant=True, authority_lane='owner',
        resolution_status='resolved', platform='sms', contact_id='owner',
        session_id='session-a', turn_id='turn-a', **changes)


def response(text='A neutral task is running.'):
    return httpx.Response(200, request=httpx.Request('GET', 'http://localhost/v1/host/executions'),
        json={'schema': 'ProtagineRequestWorkV1', 'observed_at': 1234.5,
              'text': text, 'truncated': False})


def test_generic_work_projection_does_not_instruct_an_owner_only_tool():
    from test_work_ancestry_projection import execution, rows
    task = execution(2)
    task.update(task_id='a'*64, platform='protagine_task')
    projected = request_work_context({'items': [task]})
    assert not any(line.startswith('Use task_id with protagine_task') for line in projected['text'].splitlines())
    assert projected['native_task_ids'] == ['a'*64]
    assert any(row.get('task_id') == 'a'*64 for row in rows(projected))
    assert 'terminal observation is not proof of useful completion' in projected['text']


def test_projection_retains_operational_evidence_without_task_or_draft_prose():
    report_hash = 'a' * 64
    view = {'items': [], 'local_work': {'available': True, 'items': [], 'recent': [
        {'initiative_id': 'completed-1', 'status': 'completed', 'question': 'Private task prose',
         'result': {'draft': 'Private draft prose', 'report_path': '/private/retained/report',
                    'report_sha256': report_hash}},
        {'initiative_id': 'older', 'status': 'failed'}]},
        'native_cron': {'available': False, 'items': []},
        'reported_worker': {'available': True, 'items': [
            {'label': 'Download', 'state': 'running', 'freshness': 'recent', 'pid': 123,
             'liveness': 'unverified', 'age_seconds': 2.5, 'path': '/private/status'},
            {'label': 'Offline reporter', 'available': False, 'liveness': 'unverified'}]}}
    result = request_work_context(view)
    text = result['text']
    assert 'completed-1' in text and report_hash in text
    assert 'Download' in text and 'unverified' in text
    offline = next(json.loads(line) for line in text.splitlines()
                   if line.startswith('{') and 'Offline reporter' in line)
    assert offline['available'] is False
    assert 'Unavailable sources: native_cron' in text
    assert result['truncated'] is True and 'Additional operational records omitted' in text
    assert 'Private' not in text and '/private' not in text and '"pid"' not in text
    assert len(text) <= 4000


def test_projection_bounds_many_active_records_and_discloses_omissions():
    rows = [{'execution_id': str(i), 'phase': 'tool', 'tool_name': 'x' * 128} for i in range(30)]
    result = request_work_context({'items': rows, 'truncated': False})
    assert result['truncated'] is True and len(result['text']) <= 4000
    assert result['text'].count('"source": "execution"') == 8
