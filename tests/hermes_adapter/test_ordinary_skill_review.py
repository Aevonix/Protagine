"""Fresh guided install executes the native cron and review fork with scripted I/O."""
import json
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from conftest import ROOT, run_python
from test_setup import INSTALL, _native_interpreter


def test_passive_capture_uses_current_native_profile_and_tolerates_invalid_manifest(monkeypatch,tmp_path):
    import hermes_constants
    spec=importlib.util.spec_from_file_location('ordinary_review_capture',
        ROOT/'plugins/hermes-plugin/ordinary_skill_review.py')
    review=importlib.util.module_from_spec(spec);spec.loader.exec_module(review)
    profile=tmp_path/'named';profile.mkdir()
    state=profile/'pacomind';state.mkdir()
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'gateway-root'))
    monkeypatch.setattr(hermes_constants,'get_hermes_home',lambda:profile)
    manifest={'hermes_home':str(profile),'ordinary_skill_review':{'enabled':True}}
    path=state/'instance.json';path.write_text(json.dumps(manifest))
    assert review.capture_enabled({'instance_dir':str(state)})
    monkeypatch.setattr(hermes_constants,'get_hermes_home',lambda:tmp_path/'another-profile')
    assert not review.capture_enabled({'instance_dir':str(state)})
    for invalid in ([],{**manifest,'ordinary_skill_review':[]}):
        path.write_text(json.dumps(invalid))
        assert not review.capture_enabled({'instance_dir':str(state)})


DRIVER = r'''
import json,os,sqlite3,subprocess,sys,time
from pathlib import Path
from hermes_cli.env_loader import load_hermes_dotenv
home=Path(os.environ['HERMES_HOME']);state=home/'pacomind'
load_hermes_dotenv(hermes_home=home)
import hermes_cli
assert Path(hermes_cli.__file__).is_relative_to(Path(sys.argv[1]))
manifest=json.loads((state/'instance.json').read_text());binding=manifest['ordinary_skill_review']
before=(home/'config.yaml').read_bytes()
from cron.jobs import trigger_job,create_job
from cron.scheduler import tick,get_running_job_ids
from tools import skill_ledger,write_approval
adapter=state/'adapter'
sys.path.insert(0,str(adapter))
from pacomind_hermes.client import TurnOutbox
import yaml
config=yaml.safe_load(before)['plugins']['pacomind']
successor=sys.argv[2]=='semantic_successor'
evaluated=sys.argv[2] in {'native_evaluated','semantic_successor'}
passive=sys.argv[2] in {'native_tools','native_evaluated'}
if passive:
 from hermes_cli.plugins import get_plugin_manager
 from run_agent import AIAgent
 from hermes_state import SessionDB
 get_plugin_manager().discover_and_load()
 for index in range(2):
  db=SessionDB()
  agent=AIAgent(model='fixture',provider='openai',api_key='local',base_url=sys.argv[3],
   platform='sms',enabled_toolsets=['file'],max_iterations=3,quiet_mode=True,
   skip_memory=True,skip_background_review=True,skip_context_files=True,session_db=db)
  agent._user_id='fixture-owner-address'
  result=agent.run_conversation('ORDINARY_MISSING_FILE: read the missing supplied neutral fixture.',task_id='ordinary-'+str(index))
  assert result['completed'] is True,result
  agent.close();db.close()
 failures=[r for r in skill_ledger.list_entries() if r['action']=='ordinary_tool_failure']
 assert len(failures)==2,failures
 assert len({r['evidence']['turn_id'] for r in failures})==2
 assert not (config.get('native_reviews') or {}).get('enabled'),config
if evaluated:
 selectors=[]
 if passive:
  observed=failures[0]['evidence']
  assert observed['tool_name']=='read_file' and observed['error_class']=='tool_returned_error'
  assert {r['evidence']['request_visible_result_sha256'] for r in failures}=={observed['request_visible_result_sha256']}
  selectors=[{'tool_name':observed['tool_name'],'error_class':observed['error_class'],
              'result_sha256':observed['request_visible_result_sha256']}]
 declaration=state/'native-failure-evaluator.json'
 declaration.write_text(json.dumps({'id':'controlled-native-failure','scope':'Read complete supplied sources before describing behavior.',
  'oracle':'_fixture_skill_oracle:assess','oracle_id':'controlled-source-procedure-v1','allow_apply':True,
  'environment':{'PACOMIND_FIXTURE_ORACLE_LOG':str(state/'oracle-measurements.jsonl'),
                 'PACOMIND_FIXTURE_ORACLE_REGRESSION':str(state/'oracle-regression')},
  'native_failures':selectors}))
 setup_code='from pathlib import Path; import sys; from pacomind.setup_skill_reviews import configure; configure(Path(sys.argv[1]), evaluator_path=sys.argv[2])'
 upgraded=subprocess.run([manifest['sidecar_python'],'-B','-c',setup_code,str(state),str(declaration)],
  env={**os.environ,'PYTHONPATH':manifest['sidecar_module_root']},capture_output=True,text=True,timeout=30)
 assert upgraded.returncode==0,upgraded.stdout+upgraded.stderr
 bound=json.loads((state/'instance.json').read_text())['ordinary_skill_review']
 assert bound['job_id']==binding['job_id'] and bound['evaluator_path']==str(declaration)
 binding=bound
outbox_before=TurnOutbox(config['turn_outbox_path']).snapshot()
script=home/'scripts/unrelated-review-probe.py'
script.write_text('import json,sys\nfrom pathlib import Path\n'
 +f'sys.path.insert(0,{str(adapter)!r})\n'
 +'from hermes_cli.config import load_config\n'
 +'from pacomind_hermes.ordinary_skill_review import plugin_configuration,run\n'
 +'config=load_config()["plugins"]["pacomind"]\n'
 +'assert plugin_configuration(config)==config\n'
 +'assert "cron" not in config.get("attested_system_platforms",["cli"])\n'
 +f'try: run(Path({str(state)!r}))\n'
 +'except ValueError: pass\nelse: raise AssertionError("Unrelated job inherited review authority")\n'
 +f'Path({str(home/"unrelated-checked")!r}).write_text("checked")\n'
 +'print("UNRELATED_AUTHORITY_UNCHANGED")\n')
other=create_job(prompt='',schedule='0 1 * * *',script=str(script),no_agent=True,deliver='local')
trigger_job(binding['job_id'])
tick(verbose=False,sync=False)
deadline=time.monotonic()+20
while not (home/'review-inference-started').exists() and time.monotonic()<deadline:time.sleep(.05)
assert (home/'review-inference-started').exists(),'Managed review did not reach native assessment'
trigger_job(other['id']);tick(verbose=False,sync=True)
assert (home/'unrelated-checked').exists(),'Concurrent unrelated job received managed authority'
deadline=time.monotonic()+30
while binding['job_id'] in get_running_job_ids() and time.monotonic()<deadline:time.sleep(.05)
assert binding['job_id'] not in get_running_job_ids()
pending=write_approval.list_pending(write_approval.SKILLS)
if successor:
 assert pending==[],pending
 original=[r for r in skill_ledger.list_entries() if r['evidence'].get('version')=='ordinary-review-failure-v1']
 assert len(original)==1 and original[0]['evidence']['kind']=='validation',original
 assert original[0]['evidence']['candidate_measured'] is False
 failed=original[0]
 assert not (state/'oracle-measurements.jsonl').exists(),'Validation failure ran an evaluator'
 trigger_job(binding['job_id']);tick(verbose=False,sync=True)
 pending=write_approval.list_pending(write_approval.SKILLS)
 assert skill_ledger.get_entry(failed['id'])==failed,'Original failure changed'
assert len(pending)==1,pending
payload=pending[0]['payload']
batch=payload.get('_pacomind_task_assessment_batch')
if passive:
 assert batch is None,payload
 assert len(payload['_pacomind_review_observation_ids'])==2,payload
 if evaluated:
  batch=payload['_pacomind_native_failure_batch']
  assert batch['source']=='ordinary_native_failure_batch'
  assert batch['evaluator']['id']=='controlled-native-failure'
  assert set(batch['observation_ids'])=={r['evidence']['observation_id'] for r in failures}
  assert len(batch['observations'])==2 and batch['native_execution_id']==payload['_pacomind_review_native_execution']
 else:assert '_pacomind_native_failure_batch' not in payload
else:
 assert batch['task_ids']==['task-1','task-2'],batch
 assert (batch['evaluator'] is not None)==successor,batch
assert payload['_pacomind_review_create_only'] is True
assert payload['name']=='neutral-source-handoff'
assert not (home/'skills/neutral-source-handoff/SKILL.md').exists()
assert not (home/'skills/illegal-parent-skill/SKILL.md').exists()
trigger_job(binding['job_id']);tick(verbose=False,sync=True)
if evaluated:
 skill=home/'skills/neutral-source-handoff/SKILL.md'
 assert skill.is_file(),'Declared native failure candidate was not activated'
 assert not write_approval.list_pending(write_approval.SKILLS)
 entries=skill_ledger.list_entries(skill='neutral-source-handoff')
 measured=[r for r in entries if r['action']=='evaluation' and r['evidence'].get('status')=='candidate_passed']
 assert len(measured)==1,entries
 accepted=measured[0]
 for phase in ('baseline','candidate'):
  ancestry=accepted['evidence'][phase]['task_assessment_evidence' if successor else 'native_failure_evidence']
  assert ancestry['source']==batch['source'] and ancestry['evaluator']==batch['evaluator']
  assert ancestry['observation_ids']==batch['observation_ids']
 assert any(r['action']=='evaluation' and r['evidence'].get('status')=='activated' for r in entries)
 trigger_job(binding['job_id']);tick(verbose=False,sync=True)
 audits=[r for r in skill_ledger.list_entries(skill='neutral-source-handoff')
  if r['action']=='evaluation' and r['evidence'].get('evaluation_id')==accepted['id']]
 assert len(audits)==2 and all(r['evidence']['status']=='activated' for r in audits),audits
 assert skill.is_file()
 (state/'oracle-regression').write_text('Controlled evaluator now observes a failing case.\n')
 trigger_job(binding['job_id']);tick(verbose=False,sync=True)
 assert not skill.exists(),'Measured later regression did not roll back the created skill'
 assert any(r['action']=='evaluation' and r['evidence'].get('status')=='rolled_back'
  and r['evidence'].get('evaluation_id')==accepted['id'] for r in skill_ledger.list_entries(skill='neutral-source-handoff'))
 measurements=[json.loads(line) for line in (state/'oracle-measurements.jsonl').read_text().splitlines()]
 assert [r['phase'] for r in measurements]==['baseline','candidate','post_activation','post_activation','post_activation'],measurements
 assert [r['passed'] for r in measurements]==[False,True,True,True,False],measurements
else:
 assert len(write_approval.list_pending(write_approval.SKILLS))==1
rows=skill_ledger.list_entries()
claims=[r for r in rows if r['action']=='ordinary_skill_review' and r['evidence'].get('status')=='claimed']
assert len(claims)==(2 if successor else 1),claims
if successor:
 child,parent=claims
 assert child['evidence']['successor']=={'root_claim_id':parent['id'],'parent_claim_id':parent['id'],
  'failure_entry_id':failed['id'],'attempt':1},claims
 assert child['evidence']['source_refs']==parent['evidence']['source_refs']
 assert child['evidence']['native_execution_id']!=parent['evidence']['native_execution_id']
 assert skill_ledger.get_entry(failed['id'])==failed
if not passive:assert claims[0]['evidence']['source_refs']==batch['source_refs'],claims
reports=list((home/'logs/ordinary-skill-reviews').glob('*/assessment.json'))
assert len(reports)==(2 if successor else 1)
assert all('Internal assessment cannot invoke tools' in report.read_text() for report in reports)
assert (home/'config.yaml').read_bytes()==before
with sqlite3.connect(home/'cron/executions.db') as db:
 executions=db.execute('SELECT job_id,status FROM executions ORDER BY started_at').fetchall()
assert len(executions)==(6 if successor else 5 if evaluated else 3) and all(row[1]=='completed' for row in executions),executions
assert TurnOutbox(config['turn_outbox_path']).snapshot()==outbox_before
print(json.dumps({'fresh_native_cron':True,'proposal_only':not evaluated,'native_evaluated':evaluated,'ordinary_claims':len(claims),
 'unrelated_cron_authority_unchanged':True,'review_owner_source_rows':0,'passive_tool_producer':passive,'quality_credit':False}))
'''


ORACLE = '''
import hashlib,json,os
from pathlib import Path

def assess(text, *, phase):
    passed=('Read every supplied source before describing behavior.' in (text or '')
            and not Path(os.environ['PACOMIND_FIXTURE_ORACLE_REGRESSION']).exists())
    with Path(os.environ['PACOMIND_FIXTURE_ORACLE_LOG']).open('a') as stream:
        stream.write(json.dumps({'phase':phase,'passed':passed,
            'text_sha256':hashlib.sha256((text or '').encode()).hexdigest()})+'\\n')
    return {'cases':[{'id':'controlled-source-procedure','passed':passed}]}
'''


@pytest.mark.parametrize('producer',['semantic','native_tools','native_evaluated','semantic_successor'])
def test_fresh_public_install_runs_native_review_and_keeps_unrelated_cron_unattested(artifacts, tmp_path,producer):
    native = os.environ.get('PACOMIND_TEST_HERMES_PATH')
    if not native:
        pytest.skip('Select the qualified native source')
    output, wheel, _, installed = artifacts
    python = _native_interpreter(tmp_path, wheel, 'absent')
    site = python.parent.parent/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'
    (site/'zz-reviewed-native.pth').write_text('import sys; sys.path.insert(0, '+repr(native)+')\n')
    sidecar = output/'ordinary-review-sidecar'
    run_python('-m', 'build', '--no-isolation', '--outdir', sidecar, cwd=ROOT/'sidecar')
    run_python('-m', 'pip', 'install', '--no-index', '--no-deps', '--target', installed,
               next(sidecar.glob('*.whl')), cwd=tmp_path)
    home = tmp_path/'profile'
    requests, assessment_reads, authorized_host_requests = [], [], []
    class ScriptedBoundary(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, value):
            raw=json.dumps(value).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

        def assert_host_authorization(self):
            if not self.path.startswith('/v1/host/'):
                return
            from dotenv import dotenv_values
            expected = dotenv_values(home/'.env').get('PACOMIND_NATIVE_API_KEY')
            configured = yaml.safe_load((home/'config.yaml').read_bytes())['plugins']['pacomind']
            if configured.get('api_key') != '${PACOMIND_NATIVE_API_KEY}':
                raise AssertionError('The installer root credential placeholder changed')
            if not expected or self.headers.get('Authorization') != 'Bearer '+expected:
                raise AssertionError('Host request did not use the actual installer root credential')
            authorized_host_requests.append(self.path)

        def do_GET(self):
            self.assert_host_authorization()
            owner=yaml.safe_load((home/'config.yaml').read_bytes())['plugins']['pacomind']['owner_contact_id']
            self.send({'contact_id':owner,'events':[],'through':0,'head':0,'complete':True})

        def do_POST(self):
            self.assert_host_authorization()
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/v1/host/memory/sources/erasures':
                # The actual native history reader supplies exact message hashes.
                # This fixture has no canonical ingestion; retain that limitation.
                self.send({'contact_id':body['contact_id'],'events':[],'through':0,'head':0,'complete':True,
                    'native_history_matches':[{**ref,'erased':False,'source_refs':[]}
                        for ref in body.get('native_history_refs',[])]});return
            if self.path=='/v1/host/executions/assessments/read':
                owner=yaml.safe_load((home/'config.yaml').read_bytes())['plugins']['pacomind']['owner_contact_id']
                assert body['contact_id']==owner
                assessment_reads.append(body)
                rows=[{'source_id':'assessment-'+str(i),'source_version':str(i)*64,
                    'task_id':'task-'+str(i),'execution_id':'execution-'+str(i),'complete':True,
                    'attribution':'host_reported_machine_assessment_unverified','owner_approval':'unobserved',
                    'content':'Complete original artifact and fallible source review '+str(i)} for i in (1,2)] if producer in {'semantic','semantic_successor'} else []
                self.send({'assessments':rows,'sources_current':True,'next_offset':None});return
            if not self.path.endswith('/chat/completions'):
                self.send({});return
            requests.append(body)
            messages=body.get('messages',[]); full=json.dumps(messages)
            calls=None;answer='A recurring source-reading hypothesis may be useful; no improvement is established.'
            choice=body.get('tool_choice')
            if isinstance(choice,dict) and choice.get('function',{}).get('name')=='pacomind_setup_echo':
                name,args='pacomind_setup_echo',{'token':'pacomind-ready'}
            elif 'This is system-generated assessment evidence' in full:
                attempted=any(call.get('function',{}).get('name')=='skill_manage'
                    and json.loads(call['function']['arguments']).get('name')=='neutral-source-handoff'
                    for m in messages for call in m.get('tool_calls',[]))
                if not attempted:
                    description=('x'*80 if producer=='semantic_successor'
                        and 'system_recorded_failed_proposal' not in full else 'Consult complete supplied sources.')
                    name,args='skill_manage',{'action':'create','name':'neutral-source-handoff',
                        'content':'---\nname: neutral-source-handoff\ndescription: '+description+'\n---\nRead every supplied source before describing behavior.\n',
                            **({'_pacomind_task_assessment_batch':{'evaluator':{'oracle':'model_chosen:forbidden'}}} if producer in {'semantic','semantic_successor'} else
                               {'_pacomind_native_failure_batch':{'evaluator':{'oracle':'model_chosen:forbidden'}}} if producer=='native_evaluated' else {})}
                else:name=args=None
            elif ('SYSTEM-GENERATED REVIEW OF DISTINCT OPERATIONAL TASK ASSESSMENTS' in full
                    or 'SYSTEM-GENERATED ASSESSMENT OF RECURRING NATIVE TOOL FAILURES' in full):
                if not any(m.get('role')=='tool' for m in messages):
                    (home/'review-inference-started').write_text('started')
                    deadline=time.monotonic()+20
                    while not (home/'unrelated-checked').exists() and time.monotonic()<deadline:time.sleep(.05)
                    assert (home/'unrelated-checked').exists(),'Concurrent authority probe did not finish'
                    name,args='skill_manage',{'action':'create','name':'illegal-parent-skill',
                        'content':'---\nname: illegal-parent-skill\ndescription: Forbidden parent mutation.\n---\nInvalid.\n'}
                else:name=args=None
            elif 'ORDINARY_MISSING_FILE' in full:
                if not any(m.get('role')=='tool' for m in messages):
                    name,args='read_file',{'path':str(home/'missing-neutral.txt')}
                else:name=args=None
            else:name=args=None
            if name:calls=[{'id':'call-'+str(len(requests)),'type':'function',
                           'function':{'name':name,'arguments':json.dumps(args)}}]
            message={'role':'assistant','content':'' if calls else answer}
            if calls:message['tool_calls']=calls
            response={'id':'controlled','object':'chat.completion','created':1,'model':'fixture',
                'choices':[{'index':0,'message':message,'finish_reason':'tool_calls' if calls else 'stop'}],
                'usage':{'prompt_tokens':20,'completion_tokens':4,'total_tokens':24}}
            if not body.get('stream'):
                self.send(response);return
            chunk={**response,'object':'chat.completion.chunk','choices':[{'index':0,
                'delta':{**message,**({'tool_calls':[{'index':i,**call} for i,call in enumerate(calls)]} if calls else {})},
                'finish_reason':None}]}
            ending={**chunk,'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls' if calls else 'stop'}]}
            raw=('data: '+json.dumps(chunk)+'\n\ndata: '+json.dumps(ending)+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),ScriptedBoundary)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1',0));api_port=reserved.getsockname()[1]
    api=None
    bundled=tmp_path/'bundled';bundled.mkdir()
    env={**os.environ,'HOME':str(tmp_path),'HERMES_HOME':str(home),'HERMES_BUNDLED_PLUGINS':str(bundled),
         'HERMES_BIN':str(python.parent/'hermes'),'HERMES_DISABLE_LAZY_INSTALLS':'1',
         'HERMES_DISABLE_TELEMETRY':'1','TIRITH_ENABLED':'false','PACOMIND_GUARD_CHAT_MODE':'off',
         'LITELLM_LOCAL_MODEL_COST_MAP':'True','PYTHONDONTWRITEBYTECODE':'1'}
    try:
        install=INSTALL.replace("'--hermes-home', sys.argv[3]", "'--ordinary-skill-review', '--hermes-home', sys.argv[3]")
        run_python('-I','-c',install,installed,os.environ.get('PACOMIND_TEST_DEPENDENCY_PATH',''),
            home,wheel,f'http://127.0.0.1:{server.server_port}/v1',api_port,python,cwd=tmp_path,env=env)
        if producer in {'native_evaluated','semantic_successor'}:
            (home/'pacomind/adapter/_fixture_skill_oracle.py').write_text(ORACLE)
        api=ThreadingHTTPServer(('127.0.0.1',api_port),ScriptedBoundary)
        api_thread=threading.Thread(target=api.serve_forever,daemon=True);api_thread.start()
        result=subprocess.run([str(python),'-B','-c',DRIVER,native,producer,f'http://127.0.0.1:{server.server_port}/v1'],cwd=tmp_path,env=env,
            capture_output=True,text=True,timeout=120)
        assert result.returncode==0,result.stdout[-5000:]+result.stderr[-6000:]
        assert json.loads(result.stdout.splitlines()[-1])['fresh_native_cron']
        assert authorized_host_requests
        assert len(assessment_reads)>=(2 if producer in {'semantic','semantic_successor'} else 1)
        native_calls=[row for row in requests if 'SYSTEM-GENERATED' in json.dumps(row.get('messages',[]))]
        assert len(native_calls)==(8 if producer=='semantic_successor' else 4)
        if producer=='semantic_successor':
            feedback_calls=[row for row in native_calls if 'system_recorded_failed_proposal' in json.dumps(row['messages'])]
            assert len(feedback_calls)==4
            assert all('description' in json.dumps(row['messages']) for row in feedback_calls)
    finally:
        if api is not None:
            api.shutdown();api.server_close();api_thread.join(timeout=2)
        server.shutdown();server.server_close();thread.join(timeout=2)
