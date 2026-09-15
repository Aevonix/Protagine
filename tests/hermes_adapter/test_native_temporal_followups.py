"""Qualified native Kanban, real HTTP routes, no inference or network."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = r'''
import json,os,socket,sys,types,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,sys.argv[1])
if sys.argv[3]:sys.path.append(sys.argv[3])
if len(sys.argv)>4 and sys.argv[4]:sys.path.insert(0,sys.argv[4])
package=types.ModuleType('protagine_hermes');package.__path__=[sys.argv[2]];sys.modules['protagine_hermes']=package
def no_network(*a,**kw):raise AssertionError('No network in native followup qualification')
socket.socket.connect=no_network
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from fastapi import FastAPI
from fastapi.testclient import TestClient
from protagine.api.authority import RequestAuthority
from protagine.api.routers import temporal_followups,host
from protagine.commitments.store import CommitmentStore
from protagine.commitments.work import CommitmentWork
from protagine.initiatives.temporal_followup import TemporalFollowups
from protagine.turns import get_turn_idempotency_ledger
from protagine_hermes.initiative_work import NativeFollowups
root=Path(os.environ['HERMES_HOME']);root.mkdir()
(root/'config.yaml').write_text('plugins: {enabled: []}\n')
state=Path(os.environ['PROTAGINE_STATE_DIR']);state.mkdir()
store=CommitmentStore(state/'commitments.db');host._commitment_store=store
waiting=TemporalFollowups(store)
source_ledger=get_turn_idempotency_ledger(state)
app=FastAPI()
@app.middleware('http')
async def authority(request,next_call):
 person=request.headers.get('fixture-person','owner')
 request.state.protagine_authority=RequestAuthority(principal_id='fixture-native',credential_id='fixture',
     scopes=frozenset({'turns:write','context:read'}),viewer_person_id=person,person_ids=frozenset({person}),
     audiences=frozenset({'viewer'}),authenticated=True)
 return await next_call(request)
app.include_router(temporal_followups.router)
clients=[TestClient(app),TestClient(app)]
reviews=[NativeFollowups(client,'owner') for client in clients]
base='/v1/host/temporal-followups'
def register(label):
 parent=store.create(person_id='owner',description='Obtain '+label)
 claim=CommitmentWork(store).operate(parent['id'],operation='claim',principal_id='fixture-native',contact_id='owner',session_id='owner-turn',task_id='parent-'+label,turn_id='turn-'+label)
 source_ledger.record_source('source:'+label,contact_id='owner',session_id='owner-turn',messages=[{'role':'user','content':'Please obtain the task result for '+label}],derive_claims=False)
 source_version=source_ledger.source_references(['source:'+label],contact_id='owner',session_id='owner-turn')[0]['source_version']
 body=dict(contact_id='owner',recipient_id='colleague',commitment_id=parent['id'],work_id='parent-'+label,
           session_id='owner-turn',turn_id='turn-'+label,claim_id=claim['claim_id'],outbound_ref='message:'+label,
           source_refs=['source:'+label],source_versions={'source:'+label:source_version},expected_after_seconds=10,expires_at=time.time()+3600)
 stale=clients[0].post(base,json={**body,'source_versions':{'source:'+label:'0'*64}})
 assert stale.status_code==409,stale.text
 denied=clients[0].post(base,json={**body,'authority_scope':{'send':True}})
 assert denied.status_code==422,denied.text
 response=clients[0].post(base,json=body)
 assert response.status_code==200,response.text
 value=response.json()
 assert value['expected_at'] is None and not value['effect_authorized']
 assert clients[1].post(base,json=body).json()['wait_id']==value['wait_id']
 assert clients[0].get(base,params={'contact_id':'guest'},headers={'fixture-person':'guest'}).status_code==403
 # Independent transport producer observed a real timestamp in its ledger;
 # no HTTP/model assertion can start this clock.
 waiting.acknowledge_dispatch(value['wait_id'],receipt_ref='receipt:'+label,occurred_at=time.time()-30)
 return value['wait_id'],parent['id']
def reply(identifier):
 row=waiting.get(identifier)
 return waiting.apply_reply(identifier,{'status':'matched','contact_id':'colleague','outbound_ref':row['outbound_ref'],
    'matches':[{'external_ref':'message:reply-'+identifier,'reply_to_ref':row['outbound_ref'],
                'receipt_ref':'receipt:reply-'+identifier,'ts':time.time(),'channel':'verified-other-channel','reaction':None}]})

from protagine_hermes import review_worker
from protagine_hermes.review_worker import ReviewWorker,validate_profile
import yaml
profile=root/'profiles/protagine-reviews';profile.mkdir(parents=True)
lane={'worker':True,'source_home':str(root),'owner_contact_id':'owner','log_directory':str(root/'logs')}
config={'toolsets':['protagine_review'],'platform_toolsets':{'cli':['protagine_review']},
 'agent':{'disabled_toolsets':['kanban']},'tools':{'tool_search':{'enabled':False}},
 'plugins':{'enabled':['protagine'],'protagine':{'native_reviews':lane}},
 'kanban':{'dispatch_in_gateway':False},'mcp_servers':{}}
(profile/'config.yaml').write_text(yaml.safe_dump(config))
assert validate_profile(config,root,'owner')==lane
package.register=lambda ctx:review_worker.register_worker(ctx,lane)
# Profile provisioning is separately qualified. This fixture uses that exact
# validated bounded profile, real native task claims, and the actual HTTP API.
def selected_profile(config,home,owner):
 assert config=={'enabled':True} and home==root and owner=='owner'
 return 'protagine-reviews'
review_worker.refresh_profile=selected_profile
# Native multiplex dispatch deliberately omits the root's .env from the worker
# environment. Resolve the selected root scope without replacing the worker's.
from agent.secret_scope import (build_profile_secret_scope,current_secret_scope,
    is_multiplex_active,reset_secret_scope,set_multiplex_active,set_secret_scope)
(root/'.env').write_text('PROTAGINE_NATIVE_API_KEY=controlled-root-key\n')
(profile/'.env').write_text('PROTAGINE_NATIVE_API_KEY=controlled-worker-key\n')
(root/'config.yaml').write_text(yaml.safe_dump({'plugins':{'protagine':{
 'url':'http://sidecar.fixture','api_key':'${PROTAGINE_NATIVE_API_KEY}'}}}))
assert 'PROTAGINE_NATIVE_API_KEY' not in os.environ
multiplex=is_multiplex_active();set_multiplex_active(True)
worker_scope=build_profile_secret_scope(profile);token=set_secret_scope(worker_scope)
try:
 selected=review_worker._selected_client(root,'owner')
 assert selected._headers().get('Authorization')=='Bearer controlled-root-key'
 assert current_secret_scope() is worker_scope
 assert 'PROTAGINE_NATIVE_API_KEY' not in os.environ
finally:
 reset_secret_scope(token);set_multiplex_active(multiplex)
review_worker._selected_client=lambda home,owner:clients[0]
reviews=[NativeFollowups(client,'owner',{'enabled':True}) for client in clients]

def complete_review(identifier,parent,late=None):
 value=reviews[0].work(identifier)
 with connect(board='default') as db:
  task=kb.get_task(db,value['native_task_id'])
  assert task.assignee=='protagine-reviews' and task.status=='ready'
  task=kb.claim_task(db,task.id)
 previous=dict(os.environ)
 os.environ.update(HERMES_HOME=str(profile),HERMES_KANBAN_DB=str(root/'kanban.db'),
  HERMES_KANBAN_TASK=task.id,HERMES_KANBAN_RUN_ID=str(task.current_run_id),
  HERMES_KANBAN_CLAIM_LOCK=task.claim_lock,HERMES_KANBAN_BOARD='default')
 try:
  worker=ReviewWorker(lane)
  assert worker.before_tool(tool_name='terminal')['action']=='block'
  observation=json.loads(worker.read({'source':0}))
  assert observation['review_allowed'] and observation['wait']['wait_id']==identifier,observation
  assert observation['parent']['id']==parent and observation['parent']['status']=='pending'
  assert observation['wait']['dispatch_receipt_ref'] is not None
  source=json.loads(worker.read({'source':1}))
  assert source['source_id']==observation['sources'][0]['source_id']
  assert 'Please obtain the task result' in source['content'],source
  assert 'error' in json.loads(worker.read({'source':40}))
  binding={'contact_id':'owner','native_board':'default','native_task_id':task.id,
   'native_run_id':task.current_run_id,'native_claim_lock':'wrong',
   'contract_sha256':__import__('hashlib').sha256(task.body.encode()).hexdigest(),'source':0}
  assert clients[0].post(base+'/'+identifier+'/evidence',json=binding).status_code==409
  if late:late(identifier)
  result=worker.report({'disposition':'complete','summary':'Current expected reply remains due; keep the parent task open. No message was sent.'})
  with connect(board='default') as db:
   finished=kb.get_task(db,task.id)
   assert finished.status=='done',result
   if late:assert 'no longer due' in kb.latest_run(db,task.id).summary
  assert worker.before_tool(tool_name='protagine_read_work_source')['action']=='block'
 finally:
  os.environ.clear();os.environ.update(previous)
 observed=reviews[1].work(identifier)
 assert observed['status']=='completed' and observed['native_terminal_observed'],observed
 assert not observed['effect_authorized'] and not waiting.get(identifier)['followup_receipt_ref']
 assert store.get(parent)['status']=='pending'
 return observed

# Two dispatch ticks attach and promote one bounded review, not duplicate work.
first,parent=register('first')
with ThreadPoolExecutor(2) as pool:
 values=list(pool.map(lambda index:reviews[index].work(first),range(2)))
assert values[0]['native_task_id']==values[1]['native_task_id']
complete_review(first,parent)
for review in reviews:review.reconcile(board='default')
with connect(board='default') as db:
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1

# Actual reply and explicit cancellation between read and report supersede stale
# model prose at the normal completion boundary, without fulfilling the parent.
second,parent=register('reply-during-review')
assert complete_review(second,parent,reply)['state']=='resolved'
third,parent=register('cancel-during-review')
assert complete_review(third,parent,lambda identifier:waiting.cancel(identifier,evidence_ref='owner-cancel'))['state']=='cancelled'

# A reply racing attachment never reaches a runnable task.
fourth,parent=register('reply-during-attach')
class ReplyDuringAttach:
 def get(self,*a,**kw):return clients[0].get(*a,**kw)
 def post(self,path,**kw):
  if path.endswith('/native-task'):reply(fourth)
  return clients[0].post(path,**kw)
raced=NativeFollowups(ReplyDuringAttach(),'owner',{'enabled':True}).work(fourth)
assert raced['state']=='resolved' and raced['status']=='cancelled',raced
with connect(board='default') as db:
 assert kb.get_task(db,raced['native_task_id']).status=='archived'
 assert kb.latest_run(db,raced['native_task_id']) is None
assert store.get(parent)['status']=='pending'

# Losing an attachment acknowledgement reuses the same native task on retry.
fifth,parent=register('lost-ack')
class LostAck:
 def get(self,*a,**kw):return clients[0].get(*a,**kw)
 def post(self,path,**kw):
  result=clients[0].post(path,**kw)
  if path.endswith('/native-task'):raise RuntimeError('lost attachment acknowledgment')
  return result
try:NativeFollowups(LostAck(),'owner',{'enabled':True}).work(fifth)
except RuntimeError:pass
else:raise AssertionError('missing lost ACK')
complete_review(fifth,parent)
with connect(board='default') as db:
 assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==5

# Later source correction invalidates the actual wait; no source is relabeled.
sixth,parent=register('corrected-during-review')
def correct(identifier):
 ref=source_ledger.source_references(['source:corrected-during-review'],contact_id='owner',session_id='owner-turn')[0]
 source_ledger.append_source_annotation(contact_id='owner',session_id='owner-turn',annotation_id='correction-one',
  source_id=ref['source_id'],source_version=ref['source_version'],excerpt='Please obtain',
  correction='This requested response is no longer needed.',author_principal='fixture-native')
assert complete_review(sixth,parent,correct)['state']=='cancelled'
assert waiting.due()==[]
print(json.dumps({'bounded_due_dispatch_and_result':True,'reply_and_cancel_races':True,
 'source_correction_rechecked':True,'parent_commitments_preserved':True,
 'completion_is_not_send_or_fulfillment':True,'model_calls':0,'network':0}))
'''


def test_actual_native_reply_wait_lifecycle(tmp_path):
    python = os.environ.get('PROTAGINE_HERMES_TEST_PYTHON')
    if not python:
        if importlib.util.find_spec('hermes_cli') is None:
            pytest.skip('Use qualified Hermes interpreter for native integration')
        python = sys.executable
    root = Path(__file__).resolve().parents[2]
    env = {key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'),HERMES_KANBAN_HOME=str(tmp_path/'hermes'),
        PROTAGINE_HERMES_HOME=str(tmp_path/'hermes'),PROTAGINE_HERMES_WORK_BOARDS='["default"]',
        PROTAGINE_STATE_DIR=str(tmp_path/'state'),PROTAGINE_OWNER_CONTACT_ID='owner',
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),PYTHONDONTWRITEBYTECODE='1',
        HERMES_DISABLE_TELEMETRY='1',HERMES_DISABLE_LAZY_INSTALLS='1',
        PROTAGINE_SKIP_DOTENV='1',PYTHON_DOTENV_DISABLED='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = subprocess.run([python,'-I','-B','-c',PROBE,str(root/'sidecar'),
        str(root/'plugins/hermes-plugin'),os.environ.get('PROTAGINE_TEST_DEPENDENCY_PATH',''),
        os.environ.get('PROTAGINE_HERMES_TEST_SOURCE','')],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode == 0,result.stdout+result.stderr
    assert '"bounded_due_dispatch_and_result": true' in result.stdout
