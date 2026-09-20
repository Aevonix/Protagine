"""Opt-in actual Hermes tools/container check with a controlled HTTP transcript.

No model inference is used, and this result must never rank as model performance.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

import pytest

from protagine.qualification.coding import CONSUMERS, EVALUATORS, cases, load_pack
from protagine.qualification.native import configuration, native_context
from protagine.qualification.records import read
from protagine.qualification.runner import evaluate


@pytest.mark.skipif(not os.environ.get('PROTAGINE_TEST_CODING_SANDBOX_JSON'),
                    reason='Requires an explicitly selected owned Docker sandbox and Hermes interpreter')
def test_native_loop_really_edits_executes_and_cleans_owned_containers(tmp_path):
    sandbox = json.loads(Path(os.environ['PROTAGINE_TEST_CODING_SANDBOX_JSON']).read_text())
    python = os.environ['PROTAGINE_TEST_HERMES_PYTHON']
    requests = []
    fix = '''def solve(items, offset, limit):
 if offset < 0 or limit <= 0: raise ValueError()
 return {'items':items[offset:offset+limit], 'next_offset':offset+limit if offset+limit<len(items) else None}
'''
    sequence = [('terminal', {'command': 'cat /workspace/AGENTS.md; cat /workspace/solution.py'}),
                ('write_file', {'path': '/workspace/solution.py', 'content': fix}),
                ('terminal', {'command': 'python3 -I -B -m unittest discover -s /workspace'})]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'data': [{'id': 'controlled-coding-fixture'}]}).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            if 'messages' not in body:
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"Controlled fixture only supports chat completions"}}')
                return
            index = sum(message.get('role') == 'tool' for message in body['messages'])
            message = {'role': 'assistant', 'content': 'Repaired pagination boundaries. The smoke test passed.'}
            reason = 'stop'
            if index < len(sequence):
                name, arguments = sequence[index]
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': f'call-{index}',
                    'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
                reason = 'tool_calls'
            base = {'id': 'controlled', 'created': 1, 'model': 'controlled-coding-fixture'}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
            self.end_headers()
            if body.get('stream'):
                delta = dict(message)
                if 'tool_calls' in delta:
                    delta['tool_calls'] = [{**call, 'index': i} for i, call in enumerate(delta['tool_calls'])]
                chunks = [{**base, 'object': 'chat.completion.chunk',
                    'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                    {**base, 'object': 'chat.completion.chunk',
                     'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}],
                     'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}]
                for chunk in chunks:
                    self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            else:
                self.wfile.write(json.dumps({**base, 'object': 'chat.completion',
                    'choices': [{'index': 0, 'message': message, 'finish_reason': reason}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = {'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key': 'controlled-only',
                    'default_model': 'controlled-coding-fixture', 'request_timeout_seconds': 20,
                    'api_mode': 'chat_completions'}
        config_path = tmp_path/'host.json'
        config_path.write_text(json.dumps({'model': {'provider': 'fixture', 'default': 'controlled-coding-fixture'},
                                          'providers': {'fixture': provider}}))
        selected, recipe = configuration(config_path, 'fixture', hermes_python=python)
        pack = load_pack()
        pack['tasks'] = pack['tasks'][:1]
        suite = cases(pack, sandbox, deadline_seconds=180, max_output_tokens=1024, max_iterations=6)

        def router(case):
            value = native_context(selected, recipe)
            value.coding_oracle = case.oracle
            return value

        output = tmp_path/'run'
        asyncio.run(evaluate(output, recipe, suite, CONSUMERS, EVALUATORS, router,
                             evidence_mode='controlled', suite_version='coding-controlled-tools-v1'))
        receipts = list(output.glob('attempts/*/*/result.json'))
        if not receipts:
            receipts = list(output.rglob('result.json'))
        assert len(receipts) == 1
        result = read(receipts[0])
        assert result['outcome'] == 'pass', result
        assert len(requests) >= 4
        assert any(message.get('role') == 'tool' and 'Ran 1 test' in message.get('content', '')
                   for message in requests[-1]['messages'])
        artifact = read(receipts[0].parent/'coding-artifact.json')
        assert artifact['files']['solution.py'] == fix
    finally:
        (tmp_path/'controlled-http-requests.json').write_text(json.dumps(requests, indent=2))
        server.shutdown()
        server.server_close()
        thread.join(2)
