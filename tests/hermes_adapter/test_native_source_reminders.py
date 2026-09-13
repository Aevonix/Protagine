"""Source correction and forgetting reach a real native cron script, without inference."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = r'''
import asyncio, json, os, socket, sys, threading, types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, sys.argv[1])
if sys.argv[3]: sys.path.append(sys.argv[3])
if sys.argv[4]: sys.path.insert(0, sys.argv[4])
sys.path.insert(0,sys.argv[5])
package=types.ModuleType('pacomind_hermes'); package.__path__=[sys.argv[2]]
sys.modules['pacomind_hermes']=package
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pacomind.api.authority import RequestAuthority
from pacomind.api.routers import host
from pacomind.beliefs.source_claims import validated_claims
from pacomind.beliefs.source_projection import SourceClaimProjection
from pacomind.turns import get_turn_idempotency_ledger
from pacomind_hermes.reminders import NativeReminders
from pacomind_hermes.client import TurnOutbox
from pacomind_hermes.native_owned_copies import NativeOwnedCopies
from cron import jobs, scheduler, owned_output
from cron.scheduler_delivery import _maybe_mirror_cron_delivery
from gateway.session_context import set_session_vars, clear_session_vars
from hermes_state_registry import acquire, release_or_close
import httpx
import pacomind_memory.provider as selected_provider
assert Path(selected_provider.__file__).resolve()==Path(sys.argv[2]).parent/'pacomind-memory'/'provider.py'
assert Path(owned_output.__file__).resolve()==Path(sys.argv[4])/'cron'/'owned_output.py'

home=Path(os.environ['HERMES_HOME']); home.mkdir(mode=0o700,exist_ok=True)
state=Path(os.environ['PACOMIND_STATE_DIR']); state.mkdir(exist_ok=True)
ledger=get_turn_idempotency_ledger(state)
outbox=TurnOutbox(home/'state'/'pacomind-turn-outbox.sqlite3'); outbox.prepare()
db=acquire(home/'state.db')
db.create_session('owner-conversation',source='cli')
db.create_session('owner-native',source='telegram',session_key='route:owner')
db.record_gateway_session_peer('owner-native',source='telegram',session_key='route:owner',
    user_id='owner-user',chat_id='owner-chat',chat_type='dm')
db.save_gateway_routing_entry('route:owner',json.dumps({'session_key':'route:owner','session_id':'owner-native'}))
human=db.append_message('owner-native','user','Keep my reminder up to date.')
projection=SourceClaimProjection(ledger)
now=datetime.now(timezone.utc).replace(microsecond=0)
supplied=[]
source_native_rows={}

def source(identifier, instant, *, prior=None, subject='I'):
    value=instant.replace(microsecond=0).isoformat()
    text=('Correction: ' if prior else '')+subject+' workshop access ends at '+value+'.'
    message={'role':'user','content':text}
    source_native_rows[identifier]=db.append_message('owner-conversation','user',text)
    origin_scope=NS(contact_id='owner',session_id='owner-conversation',task_id=identifier,
        turn_id=identifier,valid_participant=True,platform='cli')
    assert owned.retain_origin(origin_scope,identifier,
        messages=[{**message,'_row_id':source_native_rows[identifier]}])
    ledger.record_source(identifier, contact_id='owner', session_id='owner-conversation',
        messages=[message], occurred_at=(now-timedelta(hours=1)).isoformat(), timezone_name='UTC')
    job=projection.claim_job(); assert job and job['turn_id']==identifier,job
    prior_rows=projection.prior(job,message)
    proposal={'subject':subject,'predicate':'access_until','value':value,'evidence':text,
        'operation':'correct' if prior else 'assert','prior_claim_id':prior,
        'memory_kind':'personal_context','recall_reason':'Use the access deadline when planning workshop visits.',
        'valid_from_text':None,'valid_to_text':value,'event_at_text':None}
    claims=validated_claims(json.dumps([proposal]),message=text,prior=prior_rows,
        observed_at=job['occurred_at'],timezone_name='UTC')
    assert len(claims)==1,claims
    assert projection.commit(job,message,claims,model='controlled-fixture',lease_token=job['lease_token'])==1
    projection.finish_job(job,model='controlled-fixture')
    with ledger._connect() as claims_db:
        claim_id=claims_db.execute('SELECT id FROM source_claims WHERE turn_id=?',(identifier,)).fetchone()[0]
    ref=ledger.source_references([identifier],contact_id='owner',session_id='owner-conversation')[0]
    supplied.append(ref)
    return {**ref,'claim_id':claim_id},value

app=FastAPI()
@app.middleware('http')
async def authority(request,next_call):
    request.state.pacomind_authority=RequestAuthority(principal_id='native-fixture',credential_id='fixture',
        scopes=frozenset({'context:read','memory:read','turns:write'}),viewer_person_id='owner',
        person_ids=frozenset({'owner'}),audiences=frozenset({'viewer'}),authenticated=True)
    return await next_call(request)
app.include_router(host.router)
api=TestClient(app)

class LocalAPI(BaseHTTPRequestHandler):
    def do_POST(self):
        assert self.path=='/v1/host/memory/sources/deadline',self.path
        body=self.rfile.read(int(self.headers.get('Content-Length','0')))
        result=api.post(self.path,content=body,headers={'Content-Type':'application/json'})
        self.send_response(result.status_code)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(result.content)))
        self.end_headers(); self.wfile.write(result.content)
    def do_GET(self):
        assert self.path.startswith('/v1/host/memory/sources/erasures?'),self.path
        result=api.get(self.path)
        self.send_response(result.status_code)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(result.content)))
        self.end_headers(); self.wfile.write(result.content)
    def log_message(self,*args): pass

server=ThreadingHTTPServer(('127.0.0.1',0),LocalAPI)
thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
url='http://127.0.0.1:'+str(server.server_address[1])
(home/'config.yaml').write_text(json.dumps({'timezone':'','plugins':{'enabled':[],
    'pacomind':{'url':url,'api_key':'fixture-key','owner_contact_id':'owner',
               'turn_outbox_path':str(outbox.path)}}}))
os.environ['PACOMIND_AGENT_TIMEZONE']='America/New_York'
from hermes_time import reset_cache
reset_cache()
expected_zone=os.environ.get('HERMES_TIMEZONE') or 'America/New_York'
expected_zone_basis='caller_override' if os.environ.get('HERMES_TIMEZONE') else 'communication_frame'
scope=NS(valid_participant=True,authority_lane='owner',contact_id='owner',platform='telegram',
    session_id='owner-conversation',task_id='owner-task',turn_id='owner-turn',sender_id='owner-user',
    user_message='Keep track of the workshop access deadline.',resolution_status='resolved')
client=httpx.Client(base_url=url,trust_env=False)
memory=NS(supplied_snapshot=lambda scope:list(supplied),client=client,outbox=outbox)
owned=NativeOwnedCopies(memory,NS())
reminders=NativeReminders(client,'owner',memory,home=home,outbox=outbox)
deliveries=[]
def deliver(job, text, **kwargs):
    deliveries.append((job['id'],job['origin'],text,kwargs.get('for_failure',False)))
    if not kwargs.get('for_failure',False):
        _maybe_mirror_cron_delivery(job,'telegram','owner-chat',text,user_id='owner-user',enabled=True)
    return None
scheduler._deliver_result=deliver

def operation(args, selected=scope, *, chat_id='owner-chat'):
    tokens=set_session_vars(platform=selected.platform,chat_id=chat_id,chat_type='dm',
        user_id='owner-user',session_id=selected.session_id)
    try: return json.loads(reminders.handle(args,selected))
    finally: clear_session_vars(tokens)

def scheduled(ref):
    value=operation({'operation':'schedule',**ref})
    assert 'error' not in value,value
    return value

def finish(job):
    before=len(deliveries)
    assert scheduler.run_one_job(job)
    completed=jobs.get_job(job['id'])
    assert completed['last_status']=='ok',completed
    return deliveries[-1][2] if len(deliveries)>before else '[SILENT]'

try:
    original,original_date=source('deadline-original',now+timedelta(minutes=30))
    with ThreadPoolExecutor(max_workers=2) as pool:
        repeated=list(pool.map(lambda _:scheduled(original),range(2)))
    selected=repeated[0]; job_id=selected['job_id']; binding_id=selected['binding_id']
    assert all(row['job_id']==job_id for row in repeated),repeated
    managed=jobs.get_job(job_id)
    binding=json.loads(managed['prompt'])
    assert binding['timezone_name']==expected_zone,binding
    assert binding['timezone_basis']==expected_zone_basis,binding
    assert selected['timezone_name']==expected_zone,selected
    assert selected['current_deadline']['timezone_name']==expected_zone,selected
    assert managed['no_agent'] and managed['repeat']['times']==1
    assert managed['schedule']['run_at']==original_date,managed
    assert managed['deliver']=='origin' and managed['origin']['chat_id']=='owner-chat',managed
    assert 'workshop' not in managed['prompt'].lower() and original_date not in managed['prompt']
    assert len(jobs.list_jobs(include_disabled=True))==1
    ordinary=jobs.create_job(prompt='An independent ordinary reminder.',schedule='every 2h',deliver='local')

    stale=managed
    corrected,corrected_date=source('deadline-corrected',now+timedelta(minutes=60),prior=original['claim_id'])
    reminders.reconcile(board='default')
    changed=jobs.get_job(job_id)
    assert changed['schedule']['run_at']==corrected_date,changed
    assert operation({'operation':'inspect','job_id':job_id})['job_id']==job_id
    second_scope=NS(**{**vars(scope),'session_id':'second-owner-conversation',
        'turn_id':'second-owner-turn','platform':'discord'})
    same=operation({'operation':'schedule',**corrected},second_scope,chat_id='second-owner-chat')
    assert same['job_id']==job_id,same
    assert jobs.get_job(job_id)['origin']['chat_id']=='owner-chat'
    assert len(jobs.list_jobs(include_disabled=True))==2

    # The source changes after the native scheduler captured its old job.
    # Its real script must suppress that obsolete occurrence, then the next
    # existing tick must preserve the useful corrected reminder.
    silent=finish(stale)
    assert scheduler._is_cron_silence_response(silent),silent
    reminders.reconcile(board='default')
    current=jobs.get_job(job_id)
    assert current['enabled'] and current['schedule']['run_at']==corrected_date,current
    due,due_date=source('deadline-due-now',datetime.now(timezone.utc)-timedelta(seconds=5),
        prior=corrected['claim_id'])
    current=jobs.get_job(job_id)
    text=finish(current)
    assert due_date in text and corrected_date not in text and original_date not in text,text
    assert 'access' in text.lower() and 'I' in text,text
    reminders.reconcile(board='default')
    completed=jobs.get_job(job_id)
    assert completed['state']=='completed' and not completed['enabled'],completed
    assert reminders.render(binding_id)=='', 'The same current deadline was rendered twice'
    assert len(deliveries)==1 and deliveries[0][0]==job_id,deliveries
    assert deliveries[0][1]['chat_id']=='owner-chat',deliveries

    # The normal source-erasure feed reaches the actual fired cron output and
    # its native mirrored row through the same existing ownership outbox.
    emitted=owned_output.snapshot(job_id)
    assert emitted['files'] and len(emitted['sessions'])==1,emitted
    selected_ids=[row['id'] for row in emitted['sessions'][0]['messages']]
    assert selected_ids
    before={row['id']:dict(row) for row in db._conn.execute('SELECT * FROM messages')}
    with outbox._connect() as conn:
        reservations=[json.loads(row[0]) for row in conn.execute('SELECT metadata_json FROM native_source_ownership')]
    reserved=next(row for row in reservations if row.get('kind')=='cron' and row['job_id']==job_id)
    assert {ref['source_id'] for ref in reserved['sources']}=={
        original['source_id'],corrected['source_id'],due['source_id']},reserved
    from gateway.run_agent_cache import GatewayAgentCacheMixin
    from gateway.session import SessionStore
    from gateway.config import GatewayConfig
    from gateway.turn_lease import SessionTurnLeaseRegistry
    class Gateway(GatewayAgentCacheMixin):
        def _running_agent_ids(self): return set()
        def _peek_session_state(self,key): return None
        def _spawn_release_thread(self,target,args,name,*,inline_fallback): target(*args)
    store=SessionStore(home/'sessions',GatewayConfig()); store._db=db
    store._entries['route:owner']=NS(session_id='owner-native')
    gateway=Gateway(); gateway.session_store=store
    gateway._agent_cache_lock=threading.Lock(); gateway._agent_cache={}
    gateway._turn_leases=SessionTurnLeaseRegistry()
    ledger.erase_sources(contact_id='owner',turn_ids=[corrected['source_id']])
    settled=asyncio.run(owned.reconcile(gateway=gateway,contact='owner'))
    erased_ids=selected_ids+[source_native_rows[corrected['source_id']]]
    assert settled['pending']==0 and settled['redacted_rows']==len(erased_ids),settled
    after=owned_output.snapshot(job_id)
    assert not after['sessions'] and not after['non_message_artifacts'],after
    after_rows={row['id']:dict(row) for row in db._conn.execute('SELECT * FROM messages')}
    assert after_rows[human]==before[human]
    assert {key:value for key,value in after_rows.items() if key not in erased_ids}=={
        key:value for key,value in before.items() if key not in erased_ids}
    assert all(due_date not in json.dumps(after_rows[key]) for key in selected_ids)
    assert jobs.get_job(ordinary['id'])['enabled']

    forget_ref,_=source('deadline-to-forget',datetime.now(timezone.utc)+timedelta(minutes=45),subject='Birch')
    forgotten=scheduled(forget_ref)
    snapshot=jobs.get_job(forgotten['job_id'])
    ledger.erase_sources(contact_id='owner',turn_ids=[forget_ref['source_id']])
    assert scheduler._is_cron_silence_response(finish(snapshot))
    reminders.reconcile(board='default')
    stopped=jobs.get_job(forgotten['job_id'])
    assert stopped is None or not stopped['enabled'],stopped
    assert jobs.get_job(ordinary['id'])['enabled']

    # A process may prepare its output and then fail before native cron accepts
    # successful stdout. Reconciliation repairs the packaged launcher and uses
    # the native one-shot retry, without resending an uncertain delivery.
    retry_ref,_=source('deadline-retry',datetime.now(timezone.utc)+timedelta(minutes=30),subject='Oak')
    retry=scheduled(retry_ref)
    _,retry_date=source('deadline-retry-due',datetime.now(timezone.utc)-timedelta(seconds=5),
        prior=retry_ref['claim_id'],subject='Oak')
    retry_job=jobs.get_job(retry['job_id'])
    launcher=home/'scripts'/retry_job['script']
    launcher.write_text(launcher.read_text()+'raise SystemExit(3)\n')
    before=len(deliveries)
    assert scheduler.run_one_job(retry_job)
    failed=jobs.get_job(retry['job_id'])
    assert failed['state']=='completed' and failed['last_status']=='error',failed
    assert failed['last_error'].startswith('Script exited with code 3'),failed
    assert all(row[3] for row in deliveries[before:]),deliveries[before:]
    reminders.reconcile(board='default')
    retry_job=jobs.get_job(retry['job_id'])
    assert retry_job['enabled'],retry_job
    recovered=finish(retry_job)
    assert retry_date in recovered,recovered
    reminders.reconcile(board='default')
    assert not jobs.get_job(retry['job_id'])['enabled']
    useful=[row for row in deliveries if row[0]==retry['job_id'] and not row[3]]
    assert len(useful)==1 and useful[0][1]['chat_id']=='owner-chat',useful
    print(json.dumps({'native_source_reminder':True,'correction_reschedules_same_job':True,
        'stale_fire_is_silent':True,'corrected_deadline_rendered_once':True,
        'forgetting_stops_reminder':True,'script_failure_recovers_once':True,
        'fired_output_and_exact_mirror_erased':True,'complete_chain_ownership':True,
        'model_calls':0,'external_network':0}))
finally:
    release_or_close(db)
    client.close(); api.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)
'''


@pytest.mark.parametrize('timezone_override', ['', 'Asia/Tokyo'])
def test_native_source_deadline_correction_and_forgetting(tmp_path, timezone_override):
    python = os.environ.get('PROTAGINE_HERMES_TEST_PYTHON')
    if not python:
        if importlib.util.find_spec('hermes_cli') is None:
            pytest.skip('Use qualified Hermes interpreter for native integration')
        python = sys.executable
    root = Path(__file__).resolve().parents[2]
    native = os.environ.get('PROTAGINE_HERMES_TEST_SOURCE') or os.environ.get('PACOMIND_TEST_HERMES_PATH')
    if not native:
        selected = importlib.util.find_spec('hermes_cli')
        if selected is None:
            pytest.skip('Use qualified Hermes source for native integration')
        native = str(Path(selected.origin).resolve().parents[1])
    selected_packages = tmp_path/'selected-packages'
    selected_packages.mkdir()
    (selected_packages/'pacomind_memory').symlink_to(root/'plugins/pacomind-memory',target_is_directory=True)
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'), PACOMIND_HERMES_HOME=str(tmp_path/'hermes'),
        PACOMIND_STATE_DIR=str(tmp_path/'state'), PACOMIND_OWNER_CONTACT_ID='owner',
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), PYTHONDONTWRITEBYTECODE='1',
        HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1',
        PACOMIND_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    if timezone_override:
        env['HERMES_TIMEZONE'] = timezone_override
    env['PYTHONPATH'] = os.pathsep.join(path for path in (native,str(selected_packages),
        os.environ.get('PACOMIND_TEST_DEPENDENCY_PATH','')) if path)
    result = subprocess.run([python, '-I', '-B', '-c', PROBE, str(root/'sidecar'),
        str(root/'plugins/hermes-plugin'), os.environ.get('PACOMIND_TEST_DEPENDENCY_PATH', ''), native,
        str(selected_packages)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout+result.stderr
    assert '"native_source_reminder": true' in result.stdout
