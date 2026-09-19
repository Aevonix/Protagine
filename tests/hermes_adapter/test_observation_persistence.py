"""Retention retries keep exact evidence and wait for bounded erasure freshness."""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace

import httpx
import pytest


@pytest.fixture
def observation(monkeypatch, tmp_path):
    name = 'protagine_observation_persistence_test'
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[2] / 'plugins' / 'hermes-plugin')]
    monkeypatch.setitem(sys.modules, name, package)
    module = importlib.import_module(name + '.tool_observations')
    client_module = importlib.import_module(name + '.client')
    monkeypatch.setattr(module, '_ordinary_native_origin', lambda scope: True)
    clock = SimpleNamespace(now=time.monotonic())
    test_time = SimpleNamespace(monotonic=lambda: clock.now, time=time.time)
    monkeypatch.setattr(module, 'time', test_time)
    monkeypatch.setattr(client_module, 'time', test_time)
    outbox = client_module.TurnOutbox(tmp_path / 'private' / 'outbox.sqlite3')
    outbox.prepare()
    scope = SimpleNamespace(valid_participant=True, authority_lane='owner', platform='cli',
        contact_id='fixture-owner', session_id='fixture-session', task_id='fixture-task',
        turn_id='fixture-turn', user_message='Inspect the copper fixture.')
    page = {'contact_id': scope.contact_id, 'through': 0, 'events': [], 'complete': True}
    calls, deliveries = [], []
    behavior = SimpleNamespace(delay=0.0, status=200, error=None, delivered=True)

    def get(path, **kwargs):
        calls.append((path, kwargs))
        if behavior.error is not None:
            raise behavior.error
        remaining = min(kwargs['timeout'], kwargs['_deadline_monotonic'] - clock.now)
        clock.now += min(behavior.delay, remaining)
        if behavior.delay >= remaining:
            raise httpx.ReadTimeout('synthetic private response details')
        return httpx.Response(behavior.status, json=page,
            request=httpx.Request('GET', 'http://fixture' + path))

    def sync_turn(**stored):
        deliveries.append(stored)
        return behavior.delivered

    client = SimpleNamespace(get=get, sync_turn=sync_turn)
    memory = SimpleNamespace(supplied_snapshot=lambda scope: [], ownership=None)
    observer = module.ToolObservations(client, outbox, memory)
    context = {'tool_name': 'fixture_read', 'tool_call_id': 'fixture-call', 'api_request_id': 'api-1'}
    content = 'Copper fixture observation.'
    observer.completed(scope, context, content, arguments={})
    observer.checked({'messages': [
        {'role': 'assistant', 'tool_calls': [{'id': 'fixture-call',
            'function': {'name': 'fixture_read', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'fixture-call', 'content': content}],
        'tools': [{'name': 'protagine_memory_retain_observation'}]}, scope, 'api-2')
    # Resume a previously nominated exact native original. First-nomination
    # native-row/ownership qualification lives in test_native_tool_observations.
    payload = {'session_id': scope.session_id, 'contact_id': scope.contact_id,
        'turn_id': 'native-observation:fixture', 'require_source_receipt': True,
        'observation': {'content': content, 'reason': 'First immutable nomination.',
            'origin': {'source_id': 'fixture-instruction', 'source_version': 'a' * 64},
            'sources': [], 'native': {'tool_call_id': 'fixture-call', 'tool_name': 'fixture_read',
                'message_id': 7, 'result_sha256': hashlib.sha256(content.encode()).hexdigest()}}}
    observer._turns[module._key(scope)]['fixture-call']['payload'] = payload

    def retain(call_id='fixture-call'):
        return json.loads(observer.handle({'call_id': call_id, 'reason': 'Later nomination.'},
            scope, {'api_request_id': 'api-2'}))

    return SimpleNamespace(module=module, clock=clock, outbox=outbox, behavior=behavior,
        page=page, calls=calls, deliveries=deliveries, payload=payload, retain=retain)


def test_contended_erasure_read_can_deliver_exact_immutable_observation(observation):
    n = observation
    n.behavior.delay = 2.6
    receipt = n.retain()
    assert receipt['accepted'] and receipt['source_recorded']
    assert n.outbox.lookup(receipt['source_id'])['payload'] == n.payload
    assert len(n.calls) == len(n.deliveries) == 1
    assert n.deliveries[0]['timeout_seconds'] <= .25


def test_delivery_failure_stays_pending_without_reexecuting_original(observation):
    n = observation
    n.behavior.delivered = False
    receipt = n.retain()
    assert receipt['state'] == 'pending' and not receipt['source_recorded']
    assert n.outbox.lookup(receipt['source_id'])['payload'] == n.payload
    n.behavior.delivered = True
    assert n.retain()['source_recorded']
    assert len(n.outbox.snapshot()) == 1


@pytest.mark.parametrize('expiry_stage', ['watermark', 'http', 'apply'])
def test_entire_erasure_refresh_shares_one_deadline(observation, monkeypatch, expiry_stage):
    n = observation
    began = n.clock.now
    if expiry_stage == 'http':
        n.behavior.delay = 6.0
    else:
        method = 'erasure_watermark' if expiry_stage == 'watermark' else 'apply_erasure_page'
        original = getattr(n.outbox, method)
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            n.clock.now += 5.0
            return result
        monkeypatch.setattr(n.outbox, method, slow)
    receipt = n.retain()
    assert not receipt['accepted'] and not receipt['source_recorded']
    assert receipt['failure_stage'] == 'erasure_refresh' and receipt['retryable']
    assert n.clock.now - began == pytest.approx(5.0)
    assert not n.outbox.snapshot() and not n.deliveries
    if expiry_stage == 'watermark':
        assert not n.calls


def test_local_watermark_cost_reduces_http_allowance(observation, monkeypatch):
    n = observation
    original = n.outbox.erasure_watermark
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        n.clock.now += 3.0
        return result
    monkeypatch.setattr(n.outbox, 'erasure_watermark', slow)
    n.behavior.delay = 2.6
    receipt = n.retain()
    assert receipt['retryable'] and not receipt['accepted']
    assert n.calls[0][1]['timeout'] == pytest.approx(2.0)
    assert not n.outbox.snapshot()


@pytest.mark.parametrize('status,retryable', [(403, False), (429, True), (503, True)])
def test_erasure_http_failure_reports_stage_and_retryability(observation, status, retryable):
    n = observation
    n.behavior.status = status
    receipt = n.retain()
    assert receipt['failure_stage'] == 'erasure_refresh'
    assert receipt['error_type'] == 'HTTPStatusError' and receipt['retryable'] is retryable
    assert not receipt['accepted'] and not n.outbox.snapshot()


def test_incomplete_erasure_feed_cannot_enqueue(observation):
    n = observation
    n.page['complete'] = False
    receipt = n.retain()
    assert receipt['failure_stage'] == 'erasure_refresh' and not receipt['accepted']
    assert 'incomplete' in receipt['error']
    assert not n.outbox.snapshot()


def test_refreshed_erasure_prevents_republishing_original(observation):
    n = observation
    n.page.update(through=1, events=[{'sequence': 1, 'session_id': 'fixture-session',
        'turn_id': n.payload['turn_id'], 'message_hashes': []}])
    receipt = n.retain()
    assert receipt['state'] == 'erased' and not receipt['source_recorded']
    assert not n.outbox.snapshot() and not n.deliveries


def test_diagnostic_does_not_echo_exception_details_or_encourage_blind_retry(observation, caplog):
    n = observation
    n.behavior.error = RuntimeError('synthetic private secret and observation')
    receipt = n.retain()
    assert receipt['failure_stage'] == 'erasure_refresh' and receipt['retryable'] is False
    assert receipt['error_type'] == 'RuntimeError'
    assert 'synthetic private' not in json.dumps(receipt) + caplog.text
    assert 'retry this same call' not in receipt['error']
    assert 'stage=erasure_refresh, error=RuntimeError' in caplog.text


def test_unwitnessed_call_cannot_reach_persistence(observation):
    n = observation
    receipt = n.retain('invented-call')
    assert receipt['failure_stage'] == 'request_evidence' and not receipt['accepted']
    assert 'exact completed call' in receipt['error']
    assert not n.calls and not n.outbox.snapshot()
