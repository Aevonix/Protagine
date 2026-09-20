"""Controlled native integration, distinct from real-model performance."""
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading

import pytest

from protagine.qualification.native import configuration, native_context
from protagine.qualification.native_unified import CONSUMERS, EVALUATORS
from protagine.qualification.native_unified_cases import CASES, cases
from protagine.qualification.records import read
from protagine.qualification.runner import evaluate


@pytest.mark.skipif(not os.environ.get('PROTAGINE_TEST_HERMES_PYTHON'), reason='Explicit isolated Hermes interpreter required')
@pytest.mark.parametrize('case_index,arm', [(index, 'protagine') for index in range(4)]+[(0, 'base_hermes'), (1, 'base_hermes')])
def test_real_shared_task_reaches_other_native_session(tmp_path, case_index, arm):
    requests = []
    selected_case = replace(cases(arm=arm)[case_index], timeout_seconds=150)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(b'{"data":[{"id":"controlled-unified-fixture"}]}')
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'messages' not in body:
                self.send_response(404); self.end_headers(); return
            requests.append(body)
            users = [row.get('content', '') for row in body['messages'] if row.get('role') == 'user']
            user = str(users[-1])
            reason = 'stop'
            if user.startswith('Use protagine_commitment_work'):
                tools = [row for row in body['messages'] if row.get('role') == 'tool']
                if not tools:
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'describe-work',
                        'type': 'function', 'function': {'name': 'tool_describe',
                            'arguments': json.dumps({'names': ['protagine_commitment_work', 'protagine_task']})}}]}
                    reason = 'tool_calls'
                elif len(tools) == 1:
                    text = json.dumps(body['messages'])
                    commitment = re.search(r'id=([a-f0-9-]{36});', text).group(1)
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'claim-work',
                        'type': 'function', 'function': {'name': 'tool_call', 'arguments': json.dumps({'calls': [{
                            'name': 'protagine_commitment_work', 'arguments': {'operation': 'claim',
                            'commitment_id': commitment}}]})}}]}
                    reason = 'tool_calls'
                elif len(tools) == 2:
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'submit-work',
                        'type': 'function', 'function': {'name': 'tool_call', 'arguments': json.dumps({'calls': [{
                            'name': 'protagine_task', 'arguments': {'operation': 'submit',
                            'request': selected_case.inputs['worker_instruction']}}]})}}]}
                    reason = 'tool_calls'
                else:
                    message = {'role': 'assistant', 'content': 'Accepted the background task.'}
            elif user.startswith(('Change the running', 'Stop the workshop')):
                tools = [row for row in body['messages'] if row.get('role') == 'tool']
                if not tools:
                    name, args = 'tool_describe', {'names': ['protagine_task']}
                elif len(tools) == 1:
                    text = '\n'.join(str(row.get('content', '')) for row in body['messages'])
                    found = re.findall(r'"task_id"\s*:\s*"([a-f0-9]{64})"', text)
                    operation = 'steer' if user.startswith('Change') else 'stop'
                    task_args = {'operation': operation, 'task_id': found[0] if found else '0'*64}
                    if operation == 'steer':
                        task_args['request'] = 'Use label violet-953 instead of amber-462; keep units at 3.'
                    name, args = 'tool_call', {'calls': [{'name': 'protagine_task', 'arguments': task_args}]}
                else:
                    name = None
                if name:
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'control-'+str(len(tools)),
                        'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
                    reason = 'tool_calls'
                else:
                    message = {'role': 'assistant', 'content': json.dumps(selected_case.oracle['answer'])}
            elif user.startswith(('What is happening', 'Give me the current')):
                message = {'role': 'assistant', 'content': json.dumps(selected_case.oracle['answer'])}
            else:
                label = 'violet-953' if 'violet-953' in json.dumps(body['messages']) else 'amber-462'
                message = {'role': 'assistant', 'content': json.dumps({'label': label, 'units': 3})}
            base = {'id': 'controlled', 'model': 'controlled-unified-fixture', 'created': 1}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
            self.end_headers()
            if body.get('stream'):
                delta = dict(message)
                if 'tool_calls' in delta:
                    delta['tool_calls'] = [{**call, 'index': i} for i, call in enumerate(delta['tool_calls'])]
                for chunk in [{**base, 'object': 'chat.completion.chunk',
                    'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                    {**base, 'object': 'chat.completion.chunk',
                    'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}]}]:
                    self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            else:
                self.wfile.write(json.dumps({**base, 'object': 'chat.completion',
                    'choices': [{'index': 0, 'message': message, 'finish_reason': reason}]}).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        config_path = tmp_path/'config.json'
        config_path.write_text(json.dumps({'model': {'provider': 'fixture', 'default': 'controlled-unified-fixture'},
            'providers': {'fixture': {'base_url': f'http://127.0.0.1:{server.server_port}/v1',
                'api_key': 'controlled-only', 'default_model': 'controlled-unified-fixture',
                'api_mode': 'chat_completions', 'request_timeout_seconds': 20}}}))
        config, recipe = configuration(config_path, 'fixture', hermes_python=os.environ['PROTAGINE_TEST_HERMES_PYTHON'])
        output = tmp_path/'run'
        asyncio.run(evaluate(output, recipe, [selected_case], CONSUMERS, EVALUATORS,
            lambda _: native_context(config, recipe), evidence_mode='controlled', suite_version='unified-controlled-v1'))
        result = read(next(output.rglob('result.json')))
        assert result['outcome'] == 'pass', result
        if arm == 'base_hermes':
            assert 'protagine-work-request-v1' not in json.dumps(requests)
    finally:
        (tmp_path/'controlled-http-requests.json').write_text(json.dumps(requests, indent=2))
        server.shutdown(); server.server_close(); thread.join(2)
