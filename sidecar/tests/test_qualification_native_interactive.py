"""Real native task transitions using controlled model HTTP responses."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading

import pytest

from protagine.qualification.native import configuration, native_context
from protagine.qualification.native_interactive import CONSUMERS, EVALUATORS, assess
from protagine.qualification.native_interactive_cases import cases
from protagine.qualification.records import read
from protagine.qualification.runner import evaluate


def test_answer_without_task_effects_cannot_pass():
    case = cases()[0]
    checks = assess({'output': json.dumps(case.oracle['answer'])}, case.oracle)
    assert checks['grounded_foreground_answer'] is True
    assert checks['exact_durable_task_count'] is False
    assert checks['canonical_owner_preserved'] is False


@pytest.mark.skipif(not os.environ.get('PROTAGINE_TEST_HERMES_PYTHON'), reason='Explicit isolated native interpreter required')
@pytest.mark.parametrize('index', range(11))
def test_controlled_native_interactive_transition(tmp_path, index):
    run_case(tmp_path, cases()[index])


@pytest.mark.skipif(not (os.environ.get('PROTAGINE_TEST_HERMES_PYTHON') and os.environ.get('PROTAGINE_INTERACTIVE_HELDOUT')),
                   reason='Explicit isolated interpreter and private fixture required')
@pytest.mark.parametrize('index', range(9))
def test_controlled_private_interactive_transition(tmp_path, index):
    run_case(tmp_path, cases(os.environ['PROTAGINE_INTERACTIVE_HELDOUT'])[index])


def run_case(tmp_path, original):
    selected = replace(original, timeout_seconds=240)
    task_ids, requests = {}, []
    instructions = {item['name']: item for item in selected.inputs['tasks']}

    def tool(name, arguments):
        return {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'controlled-'+str(len(requests)), 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}

    def call(arguments):
        return tool('tool_call', {'calls': [{'name': 'protagine_task', 'arguments': arguments}]})

    def answer(value):
        return {'role': 'assistant', 'content': json.dumps(value)}

    def respond(body):
        messages = body['messages']
        users = [str(row.get('content', '')) for row in messages if row.get('role') == 'user']
        user = users[-1]
        text = json.dumps(messages)
        tools = [row for row in messages if row.get('role') == 'tool']
        bootstrap = next((item for item in instructions.values() if user.startswith(item['bootstrap'])), None)
        if bootstrap:
            name = bootstrap['name']
            found = re.findall(r'"task_id"\s*:\s*"([a-f0-9]{64})"', '\n'.join(str(row.get('content', '')) for row in tools))
            if found:
                task_ids[name] = found[-1]
            if not tools:
                return tool('tool_describe', {'names': ['protagine_task']})
            if len(tools) == 1:
                return call({'operation': 'handoff' if selected.inputs['scenario'] == 'terminal_handoff' else 'submit',
                             'request': bootstrap['instruction']})
            if selected.inputs['scenario'] == 'existing_handoff' and len(tools) == 2:
                return call({'operation': 'handoff', 'task_id': task_ids[name]})
            if selected.inputs['scenario'] == 'duplicate_submit' and len(tools) == 2:
                return call({'operation': 'submit', 'request': bootstrap['instruction']})
            return answer({'accepted': True})
        is_foreground = user.startswith(selected.inputs['messages'][-1]['content'])
        is_prior = any(user.startswith(text) for text in selected.inputs.get('prior_turns', []))
        if is_foreground or is_prior:
            mode = selected.inputs['scenario']
            operation = ('steer' if is_prior or mode in {'ordered_steer', 'targeted_steer', 'channel_steer', 'queued_steer'}
                         else 'stop' if mode in {'cancel_sibling', 'queued_cancel'}
                         else 'resume' if mode in {'stale_resume', 'failed_resume', 'completed_resume'} else 'status')
            if mode in {'short_question', 'source_withdrawal'}:
                return answer(selected.oracle['answer'])
            if not tools:
                return tool('tool_describe', {'names': ['protagine_task']})
            if len(tools) == 1:
                found = re.findall(r'"task_id"\s*:\s*"([a-f0-9]{64})"', text)
                identity = task_ids.get('alpha') or (found[0] if found else '0'*64)
                if mode == 'failed_resume':
                    return call({'operation': 'status', 'task_id': identity})
                arguments = {'operation': operation, 'task_id': identity}
                if operation == 'steer':
                    code = 'indigo-254' if is_prior else selected.oracle['results']['alpha']['label']
                    arguments['request'] = f'Use label {code}, preserving units=3. Do not create another task.'
                if operation == 'resume':
                    observed_turn = re.findall(r'"native_turn_id"\s*:\s*"([^"]+)"', text)
                    arguments['expected_turn_id'] = 'obsolete-fixture-turn' if mode == 'stale_resume' else observed_turn[-1] if observed_turn else 'missing'
                return call(arguments)
            if mode == 'failed_resume' and len(tools) == 2:
                actual = '\n'.join(str(row.get('content', '')) for row in tools)
                turn = re.findall(r'"native_turn_id"\s*:\s*"([^"]+)"', actual)[-1]
                return call({'operation': 'resume', 'task_id': task_ids['alpha'], 'expected_turn_id': turn})
            return answer({'correction_requested': True} if is_prior else selected.oracle['answer'])
        item = next((item for item in instructions.values() if user.startswith(item['marker'])), next(iter(instructions.values())))
        code = item['code']
        for marker in selected.inputs.get('observed_markers', []):
            if item['name'] == 'alpha' and marker in text:
                code = marker
        return answer({'label': code, 'units': item['units']})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":[{"id":"controlled-interactive"}]}')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'messages' not in body:
                self.send_response(404)
                self.end_headers()
                return
            requests.append(body)
            message = respond(body)
            reason = 'tool_calls' if message.get('tool_calls') else 'stop'
            base = {'id': 'controlled', 'model': 'controlled-interactive', 'created': 1}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
            self.end_headers()
            if body.get('stream'):
                if 'tool_calls' in message:
                    message['tool_calls'] = [{**item, 'index': index} for index, item in enumerate(message['tool_calls'])]
                for delta, finish in [(message, None), ({}, reason)]:
                    self.wfile.write(('data: '+json.dumps({**base, 'object': 'chat.completion.chunk',
                        'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            else:
                self.wfile.write(json.dumps({**base, 'object': 'chat.completion',
                    'choices': [{'index': 0, 'message': message, 'finish_reason': reason}]}).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path/'config.json'
        config.write_text(json.dumps({'model': {'provider': 'fixture', 'default': 'controlled-interactive'},
            'providers': {'fixture': {'base_url': f'http://127.0.0.1:{server.server_port}/v1',
                'api_key': 'controlled-only', 'default_model': 'controlled-interactive',
                'api_mode': 'chat_completions', 'request_timeout_seconds': 20}}}))
        native, recipe = configuration(config, 'fixture', hermes_python=os.environ['PROTAGINE_TEST_HERMES_PYTHON'])
        output = tmp_path/'run'
        asyncio.run(evaluate(output, recipe, [selected], CONSUMERS, EVALUATORS,
            lambda _: native_context(native, recipe), evidence_mode='controlled', suite_version='controlled-native-interactive-v1'))
        result = read(next(output.rglob('result.json')))
        if selected.inputs['scenario'] == 'ordered_steer' and result['outcome'] == 'fail':
            rows = result.get('effects', {}).get('interactive', {}).get('after', [])
            if any((row.get('terminal') or {}).get('failure_reason') == 'source_update_ownership_unavailable' for row in rows):
                assert {key for key, value in result['checks'].items() if not value} == {
                    'correct_retained_result.alpha', 'latest_correction_in_worker_request'}
                assert result['cleanup'] == 'state_directory_removed'
                pytest.xfail('Native integration rejects ownership of two registered queued updates; strict result remains fail')
        assert result['outcome'] == 'pass', result
        assert result['cleanup'] == 'state_directory_removed'
        missing_effect = deepcopy(result)
        missing_effect['effects']['interactive']['after'] = []
        assert assess(missing_effect, selected.oracle)['exact_durable_task_count'] is False
        wrong_owner = deepcopy(result)
        wrong_owner['effects']['interactive']['after'][0]['owner_matches'] = False
        assert assess(wrong_owner, selected.oracle)['canonical_owner_preserved'] is False
    finally:
        (tmp_path/'controlled-requests.json').write_text(json.dumps(requests))
        server.shutdown()
        server.server_close()
        thread.join(2)
