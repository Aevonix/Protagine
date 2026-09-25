"""A kanban worker's model calls are attributed, and one it left unfinished does not void the episode.

In the second re-pilot every episode in which the mind dispatched a worker task was unscorable: the
body tick interrupts a worker at its deadline, the model call it was streaming ends with a 2xx status
and no returned model, and the rule that every successful call must name its returned model failed
the whole episode's attribution (primary outcome "unverified", pair unavailable). A call whose response
was cut before it finished is unfinished work, like any unreturned auxiliary call: it establishes its
requested model only. And the worker's calls are recorded as the body's background work.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from protagine.qualification import paired_container, paired_transport, paired_worker
from test_qualification_paired_container import (  # noqa: F401  (pytest fixture)
    Process, paired_cases, process_fixture, run_context, selected_config)


def _result_with(requests):
    return {'stage': 'returned', 'output': 'task complete', 'tool_evidence': {
        'model_requests': requests, 'artifacts': {'result.json': '{}'}, 'turns_completed': 1, 'declared_turns': 1}}


def test_an_interrupted_worker_call_leaves_the_episode_attributed(tmp_path, selected_config, monkeypatch):
    finished = {'model': 'candidate-model', 'status': 200, 'returned_models': ['served-model'],
                'response_complete': True, 'termination': 'closed'}
    cut = {'model': 'candidate-model', 'status': 200, 'returned_models': [], 'response_complete': False,
           'termination': 'stream_error', 'workload': 'background'}
    original = Process.communicate

    async def communicate(self, payload=None):
        if self.action == 'start':
            self.records['payload'] = json.loads(payload)
            self.options['stdout'].write((paired_container.RESULT_MARKER
                                          + json.dumps(_result_with([finished, cut, finished])) + '\n').encode())
            self.options['stdout'].flush()
            self.returncode = 0
            return b'', b''
        return await original(self, payload)
    monkeypatch.setattr(Process, 'communicate', communicate)
    process_fixture(monkeypatch)
    context = run_context(tmp_path, selected_config)
    asyncio.run(paired_container.consume(paired_cases.cases(arm='base_hermes')[0].inputs, context))
    observation = context.observations[0]
    assert observation['selected_binding'] == 'candidate'
    assert observation['attribution_basis'] == 'serialized_requests_and_returned_models'
    assert observation['unreturned_requests'] == 1


def test_a_completed_call_without_a_returned_model_still_voids_attribution(tmp_path, selected_config, monkeypatch):
    finished = {'model': 'candidate-model', 'status': 200, 'returned_models': ['served-model'],
                'response_complete': True}
    silent = {'model': 'candidate-model', 'status': 200, 'returned_models': [], 'response_complete': True}
    original = Process.communicate

    async def communicate(self, payload=None):
        if self.action == 'start':
            self.options['stdout'].write((paired_container.RESULT_MARKER
                                          + json.dumps(_result_with([finished, silent])) + '\n').encode())
            self.options['stdout'].flush()
            self.returncode = 0
            return b'', b''
        return await original(self, payload)
    monkeypatch.setattr(Process, 'communicate', communicate)
    process_fixture(monkeypatch)
    context = run_context(tmp_path, selected_config)
    asyncio.run(paired_container.consume(paired_cases.cases(arm='base_hermes')[0].inputs, context))
    assert context.observations[0]['selected_binding'] is None


def _observe(messages):
    body = json.dumps({'model': 'candidate-model', 'messages': messages}).encode()
    with paired_transport.observe_requests('http://model.invalid/v1',
                                           workload=paired_worker.request_workload) as rows:
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
                200, json={'model': 'served-model', 'choices': [{'message': {'content': 'ok'}}]}))) as client:
            client.post('http://model.invalid/v1/chat/completions', content=body)
    return rows[0]


def test_a_kanban_workers_calls_are_recorded_as_background_work():
    worker = _observe([{'role': 'system', 'content': 'x'},
                       {'role': 'user', 'content': paired_worker.KANBAN_WORKER_PROMPT + 't_1d2ad522\n\n<memory-context>'}])
    assert worker['workload'] == 'background'
    owner = _observe([{'role': 'user', 'content': '[Fri 2026-09-25 12:00:04 UTC] Send me the notes.'}])
    assert 'workload' not in owner
