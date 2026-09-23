"""Typed acceptance and canonical native assignment over real HTTP/SQLite."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from protagine.api.routers import host
from protagine.commitments.local_work import LocalWork
from protagine.commitments.store import CommitmentStore
from protagine.initiatives.store import InitiativeStore
from protagine.turns.local_work import local_work_view
from test_commitment_work import work_app
from onekey import KEY


@pytest.fixture
def local_api(tmp_path, monkeypatch):
    state = tmp_path/'state'; state.mkdir()
    native = tmp_path/'native'; (native/'cron').mkdir(parents=True)
    monkeypatch.setenv('PROTAGINE_STATE_DIR', str(state))
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', 'cid-owner')
    monkeypatch.setenv('PROTAGINE_HERMES_HOME', str(native))
    monkeypatch.setenv('HERMES_HOME', str(native))
    monkeypatch.setenv('PROTAGINE_LOCAL_WORK_ENABLED', 'true')
    monkeypatch.setenv('PROTAGINE_LOCAL_WORK_JOB_ID', 'selected-job')
    with sqlite3.connect(native/'cron/executions.db') as db:
        db.execute('CREATE TABLE executions(id TEXT PRIMARY KEY,job_id TEXT,status TEXT)')
        db.executemany('INSERT INTO executions VALUES(?,?,?)', [(identifier*32, 'selected-job', 'running') for identifier in 'abc'])
    commitments = CommitmentStore(state/'commitments.db')
    initiatives = InitiativeStore(state)
    monkeypatch.setattr(host, '_initiative_store', initiatives)
    obligation = commitments.create('cid-owner', 'Compare the two neutral repair notes')
    with TestClient(work_app(tmp_path, monkeypatch, commitments)) as api:
        yield api, commitments, initiatives, obligation, native
    initiatives.close()


def body(tmp_path):
    return {'contact_id': 'cid-owner', 'session_id': 'owner-chat', 'turn_id': 'owner-turn',
            'question': 'Summarize the differences', 'sources': [str(tmp_path/'one.txt'), str(tmp_path/'two.txt')]}


def post(api, path, value):
    return api.post(path, json=value, headers={'Authorization': 'Bearer ' + KEY})


def native_run(identifier='a'):
    return {'contact_id': 'cid-owner', 'native_job_id': 'selected-job', 'native_execution_id': identifier*32}


def test_terminal_native_reconciliation_transient_retry_and_parent_cancel(local_api, tmp_path):
    api, commitments, initiatives, obligation, native = local_api
    accepted = post(api, '/v1/host/commitments/'+obligation['id']+'/local-draft', body(tmp_path)).json()
    path = '/v1/host/commitments/local-work/'+accepted['id']
    first = post(api, '/v1/host/commitments/local-work/next', native_run()).json()['assignment']
    with sqlite3.connect(native/'cron/executions.db') as db:
        db.execute("UPDATE executions SET status='failed' WHERE id=?", ('a'*32,))
    recovered = post(api, '/v1/host/commitments/local-work/next', native_run('b')).json()['assignment']
    assert recovered['reconcile_only'] and recovered['context'] == first['context']
    failure = post(api, path+'/finish', {**native_run(), 'result':{'status':'unavailable','error_type':'TimeoutError'}})
    assert failure.status_code == 200, failure.text
    second = post(api, '/v1/host/commitments/local-work/next', native_run('b')).json()['assignment']
    assert second['id'] == first['id'] and second['attempt_count'] == 2
    with sqlite3.connect(initiatives._db_path) as db:
        details = json.loads(db.execute("SELECT details FROM assignment_history WHERE action='retry'").fetchone()[0])
    assert details['previous_result']['error_type'] == 'TimeoutError'
    commitments.update(obligation['id'], status='cancelled')
    cancelled = api.get(path, params={'contact_id':'cid-owner'}, headers={'Authorization':'Bearer ' + KEY}).json()
    assert cancelled['status'] == 'cancelled'
    result = {'status':'draft_created','summary':'Unverified comparison', 'report_path':str(tmp_path/'report.md'),
              'report_sha256':'a'*64,'sources':{str(tmp_path/'one.txt'):'b'*64}}
    assert post(api, path+'/finish', {**native_run('b'),'result':result}).status_code == 409
    assert post(api, '/v1/host/commitments/local-work/next', native_run('c')).json()['assignment'] is None


def test_completed_draft_replay_does_not_fulfil_broader_commitment(local_api, tmp_path):
    api, commitments, _, obligation, _ = local_api
    accepted = post(api, '/v1/host/commitments/'+obligation['id']+'/local-draft', body(tmp_path)).json()
    post(api, '/v1/host/commitments/local-work/next', native_run())
    result = {'status':'draft_created','summary':'Unverified comparison', 'report_path':str(tmp_path/'report.md'),
              'report_sha256':'a'*64,'sources':{str(tmp_path/'one.txt'):'b'*64}}
    path = '/v1/host/commitments/local-work/'+accepted['id']+'/finish'
    complete = post(api, path, {**native_run(),'result':result})
    assert complete.status_code == 200, complete.text
    assert post(api, path, {**native_run(),'result':result}).json() == complete.json()
    assert complete.json()['parent_commitment_fulfilled'] is False
    assert commitments.get(obligation['id'])['status'] == 'pending'
    assert local_work_view()['recent'][0]['result']['report_sha256'] == 'a'*64
    later = post(api, '/v1/host/commitments/'+obligation['id']+'/local-draft',
                 {**body(tmp_path),'turn_id':'later-explicit-acceptance','new_draft':True}).json()
    assert later['id'] != accepted['id'] and later['status'] == 'pending'
