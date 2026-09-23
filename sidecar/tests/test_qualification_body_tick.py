"""The body tick against the real Hermes source: cron tick() and kanban dispatch_once, no model.

Runs in a child interpreter so the shifted wall clock and Hermes' process-wide
registries never leak into the test session. Skipped when Hermes is not installed.
"""
import importlib.util
import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(importlib.util.find_spec('hermes_constants') is None,
                                reason='Hermes source is not installed in this environment')

DRIVER = r'''
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
home = root / 'home'
home.mkdir()
os.environ.update(HOME=str(root), HERMES_HOME=str(home), HERMES_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1',
    HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_ENABLE_PROJECT_PLUGINS='0',
    HERMES_BUNDLED_PLUGINS=str(home / 'empty-bundled'))
(home / 'empty-bundled').mkdir()
from protagine.qualification import paired_body
config = {'model': {'default': 'test-model', 'provider': 'custom'}, 'plugins': {'enabled': []}}
outbox = root / 'outbox.json'
paired_body.install_capture_platform(home, config, outbox)
(home / 'config.yaml').write_text(json.dumps(config))
paired_body.install_clock(0)
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load(force=True)
from gateway.platform_registry import platform_registry
import hermes_time
report = {'registered': platform_registry.get('capture') is not None, 'protagine_tick': paired_body.protagine_tick_entry()}
(home / 'scripts').mkdir()
script = home / 'scripts' / 'notice.sh'
script.write_text('#!/bin/sh\necho "invoice for p-11 is overdue"\n')
script.chmod(0o755)
from cron.jobs import create_job, list_jobs
job = create_job(prompt=None, schedule='every 10 minutes', name='notice', deliver='capture:owner',
                 script='notice.sh', no_agent=True)
from hermes_cli import kanban_db, kanban_db_connect
with kanban_db_connect.connect_closing() as conn:
    task_id = kanban_db.create_task(conn, title='follow up on the invoice', body='synthetic', created_by='test')
ran = []
def run_task(task, workspace, seconds):
    ran.append({'task_id': task.id, 'status': task.status, 'assignee': task.assignee, 'bounded': 0 < seconds <= 5,
                'workspace_is_dir': os.path.isdir(workspace)})
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.complete_task(conn, task.id, result='done by the test worker')
    return {'completed': True}
before = hermes_time.now().isoformat()
ticks = [paired_body.run_tick(outbox=outbox, run_task=run_task, wait_seconds=5)]
paired_body.advance_clock(900)
after = hermes_time.now().isoformat()
ticks.append(paired_body.run_tick(outbox=outbox, run_task=run_task, wait_seconds=5))
ticks.append(paired_body.run_tick(outbox=outbox, run_task=run_task, wait_seconds=5))
def mind_tick():
    with kanban_db_connect.connect_closing() as conn:
        return {'created': kanban_db.create_task(conn, title='mind task', created_by='mind')}
ticks.append(paired_body.run_tick(outbox=outbox, run_task=run_task, wait_seconds=5, arm_tick=mind_tick))
from tools.send_message_tool import send_message_tool
sent = json.loads(send_message_tool({'action': 'send', 'target': 'capture:p-03', 'message': 'hello p-03'}))
report.update(job=[(j['id'], j.get('last_status')) for j in list_jobs()], task_id=task_id, ran=ran,
              outbox=paired_body.read_outbox(outbox), sent=sent, offset=paired_body.clock_offset(),
              clock=[before, after])
# A worker that returns without a terminal transition leaves a claim with no worker pid. Once the
# clock passes the claim TTL and the heartbeat staleness bound, dispatch reclaims it without
# signalling this process, and the retry runs in-process again.
import signal
signals = []
signal.signal(signal.SIGTERM, lambda *_: signals.append('SIGTERM'))
lingering = []
def linger(task, workspace, seconds):
    lingering.append(task.id)
    return {'completed': False}
with kanban_db_connect.connect_closing() as conn:
    lingering_id = kanban_db.create_task(conn, title='never finished', body='synthetic', created_by='test')
ticks.append(paired_body.run_tick(outbox=outbox, run_task=linger, wait_seconds=5))
with kanban_db_connect.connect_closing() as conn:
    left = kanban_db.get_task(conn, lingering_id)
report['left'] = {'status': left.status, 'worker_pid': left.worker_pid}
paired_body.advance_clock(2 * 3600)
ticks.append(paired_body.run_tick(outbox=outbox, run_task=linger, wait_seconds=5))
report.update(ticks=ticks, lingering_id=lingering_id, lingering=lingering, signals=signals,
              outbox_final=paired_body.read_outbox(outbox))
print('RESULT:' + json.dumps(report, default=str))
'''


def test_tick_drives_cron_and_kanban_once_and_deliveries_land_in_the_outbox(tmp_path):
    completed = subprocess.run([sys.executable, '-c', DRIVER, str(tmp_path)], capture_output=True, text=True,
                               timeout=300)
    assert completed.returncode == 0, completed.stderr[-4000:]
    report = json.loads(next(line[len('RESULT:'):] for line in completed.stdout.splitlines()
                             if line.startswith('RESULT:')))
    assert report['registered'] is True and report['protagine_tick'] is None
    first, second, third, fourth, fifth, sixth = report['ticks']
    # Nothing is due before the clock moves: zero deliveries, the ready task dispatched once.
    assert first['cron_jobs_run'] == 0 and first['outbox_after'] == 0
    assert first['dispatch']['spawned'] == 1 and first['workers'] == [{'task_id': report['task_id'],
        'outcome': {'completed': True}}]
    assert report['ran'][0] == {'task_id': report['task_id'], 'status': 'running', 'assignee': 'default',
                                'bounded': True, 'workspace_is_dir': True}
    assert [task['status'] for task in first['kanban']] == ['done'] and first['created_task_ids'] == []
    # After advancing the clock, the same tick fires the due job exactly once and delivers to the owner.
    assert second['cron_jobs_run'] == 1 and second['outbox_before'] == 0 and second['outbox_after'] == 1
    assert second['dispatch']['spawned'] == 0
    assert third['cron_jobs_run'] == 0 and third['outbox_after'] == 1 and third['dispatch']['spawned'] == 0
    assert report['job'] == [[report['job'][0][0], 'ok']]
    delivered, tool_sent = report['outbox']
    assert delivered['target'] == 'capture:owner' and delivered['text'] == 'invoice for p-11 is overdue'
    assert delivered['via'] == 'platform' and delivered['at'] >= report['clock'][1] > report['clock'][0]
    # The model's send_message tool reaches the same outbox with the recipient as written.
    assert report['sent'].get('success') is True, report['sent']
    assert tool_sent['target'] == 'capture:p-03' and tool_sent['text'] == 'hello p-03'
    assert report['offset'] == 900
    # A task created by the mind's tick is this tick's effect and is dispatched by the same tick.
    created = fourth['arm_tick']['created']
    assert fourth['created_task_ids'] == [created] and fourth['dispatch']['spawned'] == 1
    assert fourth['workers'] == [{'task_id': created, 'outcome': {'completed': True}}]
    assert [task['status'] for task in fourth['kanban']] == ['done', 'done'] and len(report['ran']) == 2
    # An in-process worker is never registered under this process's pid, so reclaiming its
    # expired claim signals nothing; the stale claim is released and the task retried.
    lingering = report['lingering_id']
    assert fifth['dispatch']['spawned'] == 1 and fifth['workers'] == [{'task_id': lingering, 'outcome': {'completed': False}}]
    assert report['left'] == {'status': 'running', 'worker_pid': None}
    assert sixth['dispatch']['reclaimed'] == 1 and sixth['dispatch']['spawned'] == 1
    assert sixth['cron_jobs_run'] == 1 and len(report['outbox_final']) == 3
    assert report['lingering'] == [lingering, lingering] and report['signals'] == []
