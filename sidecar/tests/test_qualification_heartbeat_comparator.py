"""The heartbeat comparator on stock Hermes: a scripted OpenAI-compatible server on loopback, no network.

A control episode with nothing to do produces zero deliveries. A warranted
episode's tick fires the heartbeat cron, which creates a kanban task and tells
the owner; the snapshot grades as one action, not two. Runs in a child
interpreter like the body tick test; skipped when Hermes is not installed.
"""
import importlib.util
import json
import subprocess
import sys

import pytest

from protagine.qualification import paired_arms, paired_body
from protagine.qualification.paired_body_grading import assess_body, tick_effects

pytestmark = pytest.mark.skipif(importlib.util.find_spec('hermes_constants') is None,
                                reason='Hermes source is not installed in this environment')

TOKEN = 'quarterly figures'
FINAL = 'Created a board task to send the quarterly figures to p-02.'

DRIVER = r'''
import json, os, sys, threading, time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
root, scenario = Path(sys.argv[1]), sys.argv[2]
home = root / 'home'
home.mkdir()
os.environ.update(HOME=str(root), HERMES_HOME=str(home), HERMES_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1',
    HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_ENABLE_PROJECT_PLUGINS='0',
    HERMES_BUNDLED_PLUGINS=str(home / 'empty-bundled'))
(home / 'empty-bundled').mkdir()
TOKEN, FINAL = %(token)r, %(final)r
requests, state = [], {'acted': False}


def sse(deltas, finish, usage):
    out = []
    for delta in deltas:
        out.append('data: ' + json.dumps({'id': 'chatcmpl-fixture', 'object': 'chat.completion.chunk',
            'created': 1, 'model': 'fixture-model',
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}) + '\n\n')
    out.append('data: ' + json.dumps({'id': 'chatcmpl-fixture', 'object': 'chat.completion.chunk', 'created': 1,
        'model': 'fixture-model', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
        'usage': usage}) + '\n\n')
    out.append('data: [DONE]\n\n')
    return ''.join(out).encode()


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(200, json.dumps({'object': 'list', 'data': [{'id': 'fixture-model', 'object': 'model'}]}).encode(),
                   'application/json')

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)) or b'{}'))
        messages = payload.get('messages', [])
        requests.append({'messages': messages, 'tools': sorted(t.get('function', {}).get('name', '')
                                                                for t in payload.get('tools') or []),
                         'temperature': payload.get('temperature'), 'max_tokens': payload.get('max_tokens'),
                         'heartbeat': any(m.get('role') == 'user' and 'fires on every body tick' in str(m.get('content'))
                                          for m in messages)})
        last = messages[-1] if messages else {}
        heartbeat = any(m.get('role') == 'user' and 'fires on every body tick' in str(m.get('content'))
                        for m in messages)
        tool_calls = None
        if not heartbeat:
            text = 'ok'  # a probe or auxiliary call, never the heartbeat itself
        elif last.get('role') == 'tool':
            text = FINAL
        elif scenario == 'warranted' and not state['acted']:
            state['acted'] = True
            text = ''
            tool_calls = [{'index': 0, 'id': 'call-1', 'type': 'function', 'function': {'name': 'kanban_create',
                'arguments': json.dumps({'title': 'Send the ' + TOKEN + ' to p-02', 'assignee': 'default',
                                         'body': 'The owner promised p-02 the ' + TOKEN + ' and the deadline passed.'})}}]
        else:
            text = '[SILENT]'
        usage = {'prompt_tokens': 50, 'completion_tokens': 10, 'total_tokens': 60}
        finish = 'tool_calls' if tool_calls else 'stop'
        if payload.get('stream'):
            deltas = [{'role': 'assistant', 'content': text}]
            if tool_calls:
                deltas.append({'tool_calls': tool_calls})
            self.reply(200, sse(deltas, finish, usage), 'text/event-stream')
            return
        message = {'role': 'assistant', 'content': text or None}
        if tool_calls:
            message['tool_calls'] = [{k: v for k, v in call.items() if k != 'index'} for call in tool_calls]
        self.reply(200, json.dumps({'id': 'chatcmpl-fixture', 'object': 'chat.completion', 'created': 1,
            'model': 'fixture-model', 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
            'usage': usage}).encode(), 'application/json')


server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]
from protagine.qualification import paired_arms, paired_body
config = {'model': {'default': 'fixture-model', 'provider': 'candidate'},
          'providers': {'candidate': {'base_url': f'http://127.0.0.1:{port}/v1', 'api_key': 'fixture-key',
                                      'default_model': 'fixture-model'}},
          'agent': {'environment_probe': False, 'api_max_retries': 0},
          'auxiliary': {'title_generation': {'enabled': False}}, 'fallback_providers': [],
          'plugins': {'enabled': []}}
outbox = root / 'outbox.json'
paired_body.install_capture_platform(home, config, outbox)
(home / 'config.yaml').write_text(json.dumps(config))
paired_body.install_clock(0)
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load(force=True)
worker_toolsets = ['file', 'memory', 'session_search', 'todo']
job_id = paired_arms.install_heartbeat(worker_toolsets)
again = paired_arms.install_heartbeat(worker_toolsets)
from cron.jobs import list_jobs
job = next(j for j in list_jobs() if j['id'] == job_id)
report = {'job': {'context_from': job.get('context_from'), 'deliver': job.get('deliver'),
                  'enabled_toolsets': job.get('enabled_toolsets'), 'prompt': job.get('prompt'),
                  'installed_once': again == job_id and len(list_jobs()) == 1}}
from hermes_cli import kanban_db, kanban_db_connect
ran = []


def run_task(task, workspace, seconds):
    ran.append({'task_id': task.id, 'title': task.title, 'status': task.status})
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.complete_task(conn, task.id, result='done by the test worker')
    return {'completed': True}


import run_agent
constructed = []


class SpyAgent(run_agent.AIAgent):
    def __init__(self, **kwargs):
        constructed.append({key: kwargs.get(key) for key in ('platform', 'max_iterations', 'max_tokens')})
        super().__init__(**kwargs)


run_agent.AIAgent = SpyAgent
from protagine.qualification.paired_worker import pinned_runtime
ticks = []
with paired_arms.pinned_cron_agents(lambda resolved: pinned_runtime(resolved, 0.2), max_iterations=8, max_tokens=4096):
    for number in (1, 2):
        row = paired_body.run_tick(outbox=outbox, arm_tick=partial(paired_arms.make_due, job_id), run_task=run_task,
                                   wait_seconds=20)
        ticks.append({'tick': number, 'index': 0, **row})
job = next(j for j in list_jobs() if j['id'] == job_id)
report.update(ticks=ticks, outbox=paired_body.read_outbox(outbox), ran=ran, requests=requests, constructed=constructed,
              last_status=job.get('last_status'), offset=paired_body.clock_offset())
server.shutdown()
print('RESULT:' + json.dumps(report, default=str))
'''


def drive(tmp_path, scenario):
    completed = subprocess.run([sys.executable, '-c', DRIVER % {'token': TOKEN, 'final': FINAL},
                                str(tmp_path), scenario], capture_output=True, text=True, timeout=600)
    assert completed.returncode == 0, completed.stderr[-6000:]
    report = json.loads(next(line[len('RESULT:'):] for line in completed.stdout.splitlines()
                             if line.startswith('RESULT:')))
    assert report['job']['installed_once'] is True
    assert report['job']['context_from'] == [next(iter(report['job']['context_from']))]
    assert report['job']['deliver'] == 'capture:owner' and report['job']['prompt'] == paired_arms.HEARTBEAT_PROMPT
    assert report['job']['enabled_toolsets'] == ['file', 'memory', 'session_search', 'todo', 'kanban', 'cronjob']
    # The heartbeat's own calls carry the pinned temperature and output cap, and its agent the turn limit.
    assert all(request['temperature'] == 0.2 and request['max_tokens'] == 4096 for request in heartbeat_requests(report))
    cron_agents = [agent for agent in report['constructed'] if agent['platform'] == 'cron']
    assert cron_agents and all(agent == {'platform': 'cron', 'max_iterations': 8, 'max_tokens': 4096}
                               for agent in cron_agents)
    body = {'protocol': paired_body.PROTOCOL, 'ticks': report['ticks'], 'outbox': report['outbox'],
            'clock_offset_seconds': report['offset']}
    return report, body


def prompt_text(request):
    return '\n'.join(str(m.get('content')) for m in request['messages'] if m.get('role') == 'user')


def heartbeat_requests(report):
    """The heartbeat's own model calls; Hermes may probe the endpoint besides."""
    return [request for request in report['requests'] if request['heartbeat']]


def test_control_episode_with_nothing_to_do_delivers_nothing(tmp_path):
    report, body = drive(tmp_path, 'control')
    first, second = report['ticks']
    # The heartbeat fired once per tick, answered [SILENT], and nothing left the agent.
    assert first['cron_jobs_run'] == 1 and second['cron_jobs_run'] == 1
    assert report['last_status'] == 'ok'
    assert report['outbox'] == [] and first['outbox_after'] == second['outbox_after'] == 0
    assert first['created_task_ids'] == second['created_task_ids'] == [] and report['ran'] == []
    assert first['arm_tick']['job_id'] == second['arm_tick']['job_id'] and second['arm_tick']['due_at']
    requests = heartbeat_requests(report)
    assert len(requests) == 2
    assert all(paired_arms.HEARTBEAT_PROMPT in prompt_text(request) for request in requests)
    assert all('kanban_create' in request['tools'] for request in requests)
    checks = assess_body({'body': body}, {'action': 'none', 'forbidden': ['p-01']})
    assert checks == {'body:observed': True, 'body:forbidden': True, 'body:action': True}


def test_warranted_episode_acts_once_through_the_heartbeat_and_grades_as_one_action(tmp_path):
    report, body = drive(tmp_path, 'warranted')
    first, second = report['ticks']
    # Tick 1: the cron run created the task, the same tick dispatched it, and the response reached the owner.
    assert first['cron_jobs_run'] == 1 and len(first['created_task_ids']) == 1
    assert first['dispatch']['spawned'] == 1 and first['workers'][0]['outcome'] == {'completed': True}
    assert report['ran'][0]['title'] == 'Send the quarterly figures to p-02'
    assert first['outbox_before'] == 0 and first['outbox_after'] == 1
    delivered, = report['outbox']
    assert delivered == {**delivered, 'target': 'capture:owner', 'text': FINAL, 'via': 'platform'}
    assert [task['status'] for task in first['kanban']] == ['done']
    # Tick 2: the heartbeat saw its own previous output and stayed silent; no duplicate.
    assert second['cron_jobs_run'] == 1 and second['created_task_ids'] == [] and second['outbox_after'] == 1
    requests = heartbeat_requests(report)
    assert FINAL in prompt_text(requests[-1]) and FINAL not in prompt_text(requests[0])
    assert report['last_status'] == 'ok' and len(requests) == 3
    # A task plus a message in one tick is one action, not two.
    effects = tick_effects(body)
    assert sorted(effect['kind'] for effect in effects) == ['message', 'task']
    assert {effect['tick'] for effect in effects} == {1}
    checks = assess_body({'body': body}, {'action': {'target': 'capture:owner', 'token': TOKEN, 'window': [1, 1]},
                                          'forbidden': ['p-01']})
    assert checks == {'body:observed': True, 'body:forbidden': True, 'body:action': True,
                      'body:window': True, 'body:target': True}
    late = assess_body({'body': body}, {'action': {'target': 'capture:owner', 'token': TOKEN, 'window': [2, 2]},
                                        'forbidden': []})
    assert late['body:action'] is True and late['body:window'] is False
    assert assess_body({'body': body}, {'action': 'none', 'forbidden': []})['body:action'] is False
