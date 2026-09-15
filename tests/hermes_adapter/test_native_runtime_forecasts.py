"""Native attachment -> measured completion -> next actual learned horizon."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = r'''
import json,os,socket,sys,types,time,hashlib,shutil
from pathlib import Path
sys.path.insert(0,sys.argv[1])
if sys.argv[3]:sys.path.append(sys.argv[3])
if sys.argv[4]:sys.path.insert(0,sys.argv[4])
package=types.ModuleType('protagine_hermes');package.__path__=[sys.argv[2]];sys.modules['protagine_hermes']=package
def no_network(*a,**kw):raise AssertionError('No network in native forecast qualification')
socket.socket.connect=no_network
from hermes_cli import kanban_db as kb
from fastapi import FastAPI
from fastapi.testclient import TestClient
from protagine.api.authority import RequestAuthority
from protagine.api.routers import initiative_work,host
from protagine.initiatives.store import InitiativeStore
from protagine.self_model.expectations import ExpectationStore,ExpectationEngine
from protagine.self_model import runtime_forecasts
from protagine.turns import get_turn_idempotency_ledger
from protagine.turns.hermes_kanban import task_snapshot
from protagine_hermes.initiative_work import NativeReviews
from protagine_hermes.review_worker import register_worker
from unittest.mock import patch
root=Path(os.environ['HERMES_HOME']);root.mkdir()
(root/'config.yaml').write_text('plugins: {enabled: [], protagine: {owner_contact_id: owner}}\n')
state=Path(os.environ['PROTAGINE_STATE_DIR']);state.mkdir()
shutil.copytree(sys.argv[2],state/'adapter/protagine_hermes')
for name in ('catalog.py','contract.py'):
 shutil.copyfile(Path(sys.argv[2]).parents[1]/'hostworker/protagine_hostworker'/name,state/'adapter/protagine_hermes/protagine_hostworker'/name)
(state/'instance.json').write_text(json.dumps({'version':1,'profile':'local','hermes_home':str(root),
 'hermes_python':sys.executable,'sidecar_python':sys.argv[5],'sidecar_module_root':sys.argv[1],
 'adapter_binding':{'mode':'private-directory'}}))
(state/'.protagine-llm-config.json').write_text(json.dumps({'provider':'vllm','models':{},
 'modelPool':{'planning-fixture':{'model':'replaceable-planning-model',
 'baseUrl':'http://127.0.0.1:9/v1','apiKey':'fixture-private-token','supportsTools':True}},'functionRoles':{'planning':['planning-fixture']}}))
from protagine.setup_native_reviews import configure
configure(state,install=True)
store=InitiativeStore(state);host._initiative_store=store
host._expectations=ExpectationEngine(ExpectationStore(str(state/'expectations.db')))
sources=get_turn_idempotency_ledger(state)
app=FastAPI()
@app.middleware('http')
async def authority(request,next_call):
 request.state.protagine_authority=RequestAuthority(principal_id='fixture-native',credential_id='fixture',
     scopes=frozenset({'turns:write','context:read'}),viewer_person_id='owner',person_ids=frozenset({'owner'}),
     audiences=frozenset({'viewer'}),authenticated=True)
 return await next_call(request)
app.include_router(initiative_work.router)
client=TestClient(app);worker=NativeReviews(client,'owner',{'enabled':True,'instance_dir':str(state)})
selection={'event':runtime_forecasts.OUTCOME_VERSION,'cohort':'fixture-prospective-cohort','expires_at':time.time()+3600}
def proposal(label,selected=True,expired=False):
 context={'evidence_scope':'local_observation'}
 if selected:context['probability_forecast']={**selection,**({'expires_at':time.time()-1} if expired else {})}
 return store.create(type='operational',description=label,priority=.5,
    action_hint='operational_review',source_type='operational',created_by='autonomy_loop',
    context=context)
def history(identifier):
 fid='native-task:'+runtime_forecasts._digest({k:v for k,v in identifier.items() if k in {'source_home_id','native_board','native_task_id'}})
 return host._expectations.store.forecast_history(fid)
def outcomes(value):
 fid=history(value['native_work'])['forecasts'][0]['detail']['forecast_id']+':first-outcome'
 return host._expectations.store.forecast_history(fid)
def readback(identifier,value):
 response=client.post('/v1/host/initiative-work/'+identifier+'/observe',json={
  'contact_id':'owner','native_board':'default','native_task_id':value['native_work']['native_task_id'],
  'contract_sha256':value['review']['sha256']})
 assert response.status_code==200,response.text
 return response.json()

first=proposal('Inspect local fixture metadata')
started=worker.work(first.id)
_,attached_snapshot=task_snapshot(first.id,'owner',started['native_work'],review=True)
assert attached_snapshot['before_first_attempt_intervention'] is None,attached_snapshot
first_history=history(started['native_work']);assert len(first_history['forecasts'])==1,started
prediction=first_history['forecasts'][0]
outcome_id=prediction['detail']['forecast_id']+':first-outcome'
original_outcome=host._expectations.store.forecast_history(outcome_id)['forecasts'][0]
assert original_outcome['confidence']==.7
assert original_outcome['horizon']==original_outcome['detail']['origin_at']+480
assert original_outcome['detail']['conditions']['role_recipe']['role']=='planning'
assert 'fixture-private-token' not in json.dumps(original_outcome)
assert 'base_url' not in json.dumps(original_outcome['detail']['conditions']['role_recipe'])
assert prediction['detail']['conditions']['estimate']['sample_n']==0
assert prediction['detail']['model_provenance']['served_model'] is None
# A queued profile change does not turn the frozen attachment recipe into an
# observation of the configuration eventually loaded by a worker.
configuration_path=state/'.protagine-llm-config.json'
original_configuration=configuration_path.read_text()
queued_configuration=json.loads(original_configuration)
queued_configuration['modelPool']['planning-fixture']['model']='changed-before-first-claim'
configuration_path.write_text(json.dumps(queued_configuration))
configure(state)
native,queued_snapshot=task_snapshot(first.id,'owner',started['native_work'],review=True)
attachment_revision=original_outcome['detail']['conditions']['role_recipe']['configuration_revision']
assert queued_snapshot['outcome_role_recipe']['configuration_revision']!=attachment_revision
original_probability_history=host._expectations.store.forecast_history(outcome_id)
queued_forecast=runtime_forecasts.task_outcome('project',started,native,queued_snapshot,'owner')
assert queued_forecast['configuration_revision']==attachment_revision
assert queued_forecast['configuration_basis']=='attachment_time'
assert queued_forecast['execution_configuration_observed'] is False
assert host._expectations.store.forecast_history(outcome_id)==original_probability_history
with kb.connect(board='default') as db:
 task=kb.claim_task(db,started['native_work']['native_task_id'])
 os.environ.update(HERMES_KANBAN_TASK=task.id,HERMES_KANBAN_RUN_ID=str(task.current_run_id),
                   HERMES_KANBAN_CLAIM_LOCK=task.claim_lock)
 # Register the actual managed-worker branch. The general plugin returns before
 # its observer registration, so constructing an observer here hides a missing
 # production integration.
 import yaml
 worker_configuration=yaml.safe_load((root/'profiles/protagine-reviews/config.yaml').read_text())
 hooks={};registered_tools={}
 with patch('hermes_cli.config.load_config',return_value=worker_configuration), patch(
   'protagine_hermes.review_worker._selected_client',return_value=client) as selected_client:
  register_worker(types.SimpleNamespace(register_hook=lambda n,f:hooks.update({n:f}),
   register_tool=lambda **kw:registered_tools.update({kw['name']:kw})),
   worker_configuration['plugins']['protagine']['native_reviews'])
  selected_client.assert_not_called()
 assert set(registered_tools)=={'protagine_read_work_source','protagine_review_report'}
 with patch('agent.delegation_context.is_dispatcher_owned_worker_context',return_value=True), patch(
   'protagine_hermes.review_worker._selected_client',return_value=client) as selected_client:
  hooks['pre_api_request'](model='requested-alias',provider='custom')
  selected_client.assert_not_called()
  hooks['pre_api_request'](api_request_id='fixture-request',model='requested-alias',provider='custom')
  hooks['post_api_request'](api_request_id='fixture-request',model='requested-alias',provider='custom',
                            response_model='provider-reported-fixture',response={'content':'not retained'})
  assert selected_client.call_count==2
  assert all(call.args==(root,'owner') for call in selected_client.call_args_list)
 # Credential/service unavailability remains a missed receipt, never a failure
 # of the already accepted review. The existing complete pair still settles.
 with patch('agent.delegation_context.is_dispatcher_owned_worker_context',return_value=True), patch(
   'protagine_hermes.review_worker._selected_client',side_effect=OSError('fixture unavailable')):
  hooks['api_request_error'](api_request_id='unavailable-request',model='requested-alias')
 assert kb.complete_task(db,task.id,summary='Metadata read; result quality not independently assessed.',expected_run_id=task.current_run_id,fire_lifecycle_hook=False)
for key in ('HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_KANBAN_CLAIM_LOCK'):os.environ.pop(key)
completed=worker.work(first.id)
assert completed['forecast']['status']=='observed',completed
first_history=history(started['native_work'])
assert first_history['outcomes'][0]['status']=='observed'
assert first_history['forecasts'][0]['outcome']=='hit'
fixed=completed['forecast']['task_outcome']
assert fixed['comparison']['observed_binary']==1
assert abs(fixed['comparison']['forecast_brier']-.09)<1e-10
assert fixed['comparison']['forecast_brier']==fixed['comparison']['baseline_brier']
assert fixed['comparison']['always_completes_brier']==0
assert not fixed['suggestion_enabled'] and not fixed['quality_evaluated']
assert fixed['configuration_revision']==attachment_revision
assert fixed['configuration_basis']=='attachment_time' and not fixed['execution_configuration_observed']
settled_probability_history=host._expectations.store.forecast_history(outcome_id)
assert settled_probability_history['forecasts'][0]['detail']==original_outcome['detail']
assert settled_probability_history['forecasts'][0]['detail']['model_provenance']['served_model'] is None
frozen_material=tuple(host._expectations.store._conn.execute(
 'SELECT p.detail,f.material_digest FROM predictions p JOIN forecast_revisions f USING(prediction_id) WHERE f.forecast_id=?',
 (outcome_id,)).fetchone())
frozen_outcomes=host._expectations.store._conn.execute(
 'SELECT payload FROM forecast_outcomes WHERE forecast_id=? ORDER BY revision',(outcome_id,)).fetchall()
native,snapshot=task_snapshot(first.id,'owner',started['native_work'],review=True)
projection=runtime_forecasts.project(started,native,snapshot,'owner')
assert projection['status']=='shadow' and projection['decision']=='terminal',projection
assert projection['served_model']=='provider-reported-fixture' and projection['conditions_comparable'],projection
assert projection['original_served_model'] is None
assert projection['processor_observation']['complete_observed_pairs']
assert 'not_weight_attestation' in projection['processor_observation']['basis']
# Later configuration changes cannot rebind a finished outcome or its forecast.
changed_snapshot={**snapshot,'forecast_configuration':{'runtime_budget_seconds':1,'served_model':'later-model'}}
again=runtime_forecasts.project(started,native,changed_snapshot,'owner')
assert again['conditions_comparable'] and again['served_model']=='provider-reported-fixture'
assert first_history['forecasts'][0]['detail']==prediction['detail']
assert again['task_outcome']==fixed
assert host._expectations.store.forecast_history(outcome_id)==settled_probability_history
assert tuple(host._expectations.store._conn.execute(
 'SELECT p.detail,f.material_digest FROM predictions p JOIN forecast_revisions f USING(prediction_id) WHERE f.forecast_id=?',
 (outcome_id,)).fetchone())==frozen_material
assert host._expectations.store._conn.execute(
 'SELECT payload FROM forecast_outcomes WHERE forecast_id=? ORDER BY revision',(outcome_id,)).fetchall()==frozen_outcomes
configuration_path.write_text(original_configuration)
configure(state)
stale=client.post('/v1/host/initiative-work/'+first.id+'/model-observation',json={
 'contact_id':'owner','native_board':'default','native_task_id':task.id,
 'native_run_id':task.current_run_id,'native_claim_lock':task.claim_lock,
 'contract_sha256':started['review']['sha256'],'api_request_id':'late','phase':'response',
 'response_model':'not-accepted'})
assert stale.status_code==409,stale.text
assert not projection['suggestion_enabled'] and projection['comparison']['receipt_ref']==first_history['outcomes'][0]['receipt_ref']
assert runtime_forecasts.project(started,native,snapshot,'other')['status']=='source_unavailable'
from protagine.turns.local_work import local_work_view
from protagine.turns.executions import request_work_context
from protagine.turns import hermes_kanban
original_snapshot=hermes_kanban.task_snapshot
snapshot_reads=[]
def counted_snapshot(*args,**kwargs):
 snapshot_reads.append(args[0]);return original_snapshot(*args,**kwargs)
hermes_kanban.task_snapshot=counted_snapshot
work_view=local_work_view()
hermes_kanban.task_snapshot=original_snapshot
projected=next(item for item in work_view['recent'] if item['initiative_id']==first.id)
assert projected['forecast']['decision']=='terminal',projected
assert snapshot_reads==[first.id],snapshot_reads
request=request_work_context({'items':[],'local_work':work_view})
assert 'shadow observation only' in request['text'] and '"suggestion_enabled": false' in request['text']
assert 'source_versions' not in request['text'] and 'inspect_recorded_state' not in request['text']
second=proposal('Inspect another local fixture')
second_value=worker.work(second.id)
second_prediction=history(second_value['native_work'])['forecasts'][0]
estimate=second_prediction['detail']['conditions']['estimate']
assert estimate['sample_n']==1 and estimate['seconds']<480,estimate
second_outcome=second_value['forecast']['task_outcome']
assert second_outcome['sample_n']==1 and abs(second_outcome['probability']-.76)<1e-10
# Check the absolute timestamp: subtracting an epoch-sized origin can lose
# fractional seconds from the learned estimate through float cancellation.
assert second_prediction['horizon']==second_prediction['detail']['origin_at']+estimate['seconds']
assert second_prediction['detail']['model_provenance']['served_model'] is None
# Confirm the canonical record is runtime-origin source-only, not owner facts.
with sources._connect() as db:
 rows=db.execute('SELECT turn_id,messages_json FROM turn_sources ORDER BY turn_id').fetchall()
 assert len(rows)==6,rows
 assert db.execute('SELECT count(*) FROM source_claim_jobs').fetchone()[0]==0
 for row in rows:
  message=json.loads(row['messages_json'])[0]
  assert message['_native_runtime_observation']=='native-task-forecast-v1'
  assert message['role']=='assistant'
# Erasing the exact completed outcome stops it influencing the next forecast.
receipt=first_history['outcomes'][0]['receipt_ref'].removeprefix('receipt:')
sources.erase_sources(turn_ids=[receipt],contact_id='owner')
assert runtime_forecasts.project(started,native,snapshot,'owner')['decision']=='source_unavailable'
third=proposal('Inspect later fixture after erased outcome')
third_value=worker.work(third.id)
third_prediction=history(third_value['native_work'])['forecasts'][0]
assert third_prediction['detail']['conditions']['estimate']['sample_n']==0
assert third_prediction['detail']['conditions']['estimate']['seconds']==480
# General expectation enablement and a role profile do not enroll ordinary work.
ordinary=worker.work(proposal('An ordinary unenrolled review',selected=False).id)
assert outcomes(ordinary)['forecasts']==[]
expired=worker.work(proposal('An expired finite selection',expired=True).id)
assert outcomes(expired)['forecasts']==[]
# The first attempt's native failure remains false after a successful retry.
# These are SQLite lifecycle fixtures, with no worker process or fault injection.
failed_proposal=proposal('A review with a retained first-attempt failure')
failed=worker.work(failed_proposal.id)
failed_original=outcomes(failed)['forecasts'][0]
from hermes_cli.kanban_db_dispatch import _record_task_failure
with kb.connect(board='default') as db:
 first_run=kb.claim_task(db,failed['native_work']['native_task_id'])
 _record_task_failure(db,first_run.id,'Fixture terminal execution receipt',outcome='spawn_failed',release_claim=True,end_run=True)
 assert kb.promote_task(db,first_run.id,actor='fixture')[0]
 retried=kb.claim_task(db,first_run.id)
 assert kb.complete_task(db,retried.id,summary='The later attempt completed.',expected_run_id=retried.current_run_id,fire_lifecycle_hook=False)
settled=worker.work(failed_proposal.id)
assert settled['forecast']['task_outcome']['comparison']['observed_binary']==0,settled
failed_history=outcomes(failed)
assert failed_history['forecasts'][0]['detail']==failed_original['detail']
assert failed_history['outcomes'][0]['status']=='observed' and failed_history['outcomes'][0]['value'] is False
with sources._connect() as db:
 raw=db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',
     (failed_history['outcomes'][0]['receipt_ref'].removeprefix('receipt:'),)).fetchone()[0]
 facts=json.loads(raw)[0]['_native_forecast_facts']
 assert facts['first_attempt']['id']==first_run.current_run_id
 assert facts['first_attempt']['outcome'] in {'spawn_failed','gave_up'}
 assert facts['processor_observation']['served_model'] is None
assert readback(failed_proposal.id,settled)['forecast']['task_outcome']['comparison']['observed_binary']==0
# A pause/intervention followed by completion stays censored, including before
# any attempt. An observer arriving after the resumption sees native history.
for before_claim in (False,True):
 paused_proposal=proposal('An interrupted review '+str(before_claim))
 paused=worker.work(paused_proposal.id)
 assert paused['forecast']['task_outcome']['sample_n']==2
 assert abs(paused['forecast']['task_outcome']['probability']-(2.8+1)/6)<1e-10
 with kb.connect(board='default') as db:
  task_id=paused['native_work']['native_task_id']
  if not before_claim:kb.claim_task(db,task_id)
  assert kb.block_task(db,task_id,kind='needs_input',reason='initial_status')
  assert kb.promote_task(db,task_id,actor='fixture')[0]
  resumed=kb.claim_task(db,task_id)
  assert kb.complete_task(db,task_id,summary='The resumed review completed.',expected_run_id=resumed.current_run_id,fire_lifecycle_hook=False)
 paused=worker.work(paused_proposal.id)
 assert paused['forecast']['task_outcome']['status']=='censored',paused
 assert 'comparison' not in paused['forecast']['task_outcome']
cancelled_proposal=proposal('A review cancelled before any attempt')
cancelled=worker.work(cancelled_proposal.id)
with kb.connect(board='default') as db:assert kb.archive_task(db,cancelled['native_work']['native_task_id'])
cancelled=worker.work(cancelled_proposal.id)
assert cancelled['forecast']['task_outcome']['status']=='censored',cancelled
assert outcomes(cancelled)['outcomes'][0]['reason']=='cancelled'
# Native manual completion synthesizes a history row without a claimed worker.
manual_proposal=proposal('A manually completed review without execution')
manual=worker.work(manual_proposal.id)
with kb.connect(board='default') as db:
 assert kb.complete_task(db,manual['native_work']['native_task_id'],summary='Manual fixture completion.',fire_lifecycle_hook=False)
manual=worker.work(manual_proposal.id)
assert manual['forecast']['task_outcome']['status']=='censored',manual
# Source erasure removes probability samples independently of duration sources.
sources.erase_sources(turn_ids=[fixed['comparison']['receipt_ref'].removeprefix('receipt:')],contact_id='owner')
# A fresh finite window still learns prior exact-recipe outcomes. Its label and
# expiry are immutable audit metadata, not another statistical cohort identity.
selection={**selection,'cohort':'next-fixture-window','expires_at':time.time()+1800}
after_erasure=worker.work(proposal('A review after an erased probability sample').id)['forecast']['task_outcome']
assert after_erasure['sample_n']==1 and abs(after_erasure['probability']-.56)<1e-10
# A different attachment recipe starts at the frozen prior in its own cohort.
configuration=json.loads((state/'.protagine-llm-config.json').read_text())
configuration['modelPool']['planning-fixture']['model']='another-replaceable-model'
(state/'.protagine-llm-config.json').write_text(json.dumps(configuration))
swapped=worker.work(proposal('A review after a planning-role swap').id)
swap_forecast=swapped['forecast']['task_outcome']
assert swap_forecast['sample_n']==0 and swap_forecast['probability']==.7
assert swap_forecast['configuration_revision']!=fixed['configuration_revision']
assert swap_forecast['configuration_basis']=='attachment_time' and not swap_forecast['execution_configuration_observed']
assert 'role_recipe' not in swap_forecast and 'configuration' not in swap_forecast
# Installing/replaying observation never creates retrospective forecasts.
assert runtime_forecasts.observe({'native_work':{},'review':{'action':'operational_review'}},started['native_work'],{},'owner')['status']=='disabled_or_unselected'
print(json.dumps({'native_forecast_issued':True,'independent_outcome':True,'next_horizon_changed':True,
                  'erasure_removes_learning':True,'processor_unknown_honest':True,'models':0,'network':0}))
'''


def test_actual_native_forecast_learning(tmp_path):
    python = os.environ.get('PROTAGINE_HERMES_TEST_PYTHON')
    if not python:
        pytest.skip('Use qualified Hermes interpreter for native integration')
    root = Path(__file__).resolve().parents[2]
    env = {key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'hermes'),HERMES_KANBAN_HOME=str(tmp_path/'hermes'),
        PROTAGINE_HERMES_HOME=str(tmp_path/'hermes'),PROTAGINE_HERMES_WORK_BOARDS='["default"]',
        PROTAGINE_STATE_DIR=str(tmp_path/'state'),PROTAGINE_OWNER_CONTACT_ID='owner',PROTAGINE_EXPECTATIONS='on',
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),PYTHONDONTWRITEBYTECODE='1',
        HERMES_DISABLE_TELEMETRY='1',HERMES_DISABLE_LAZY_INSTALLS='1',
        PROTAGINE_SKIP_DOTENV='1',PYTHON_DOTENV_DISABLED='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    result=subprocess.run([python,'-I','-B','-c',PROBE,str(root/'sidecar'),
        str(root/'plugins/hermes-plugin'),os.environ.get('PROTAGINE_TEST_DEPENDENCY_PATH',''),
        os.environ.get('PROTAGINE_HERMES_TEST_SOURCE',os.environ.get('PROTAGINE_TEST_HERMES_PATH','')),sys.executable],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stdout+result.stderr
    assert '"next_horizon_changed": true' in result.stdout
