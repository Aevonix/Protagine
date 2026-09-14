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
import json,os,sqlite3,sys,time
from pathlib import Path
from dotenv import load_dotenv
home=Path(os.environ['HERMES_HOME']);state=home/'pacomind'
load_dotenv(home/'.env',override=True)
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
passive=sys.argv[2]=='native_tools'
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
trigger_job(binding['job_id']);tick(verbose=False,sync=True)
pending=write_approval.list_pending(write_approval.SKILLS)
assert len(pending)==1,pending
payload=pending[0]['payload']
batch=payload.get('_pacomind_task_assessment_batch')
if passive:
 assert batch is None,payload
 assert len(payload['_pacomind_review_observation_ids'])==2,payload
else:
 assert batch['evaluator'] is None and batch['task_ids']==['task-1','task-2'],batch
assert payload['_pacomind_review_create_only'] is True
assert payload['name']=='neutral-source-handoff'
assert not (home/'skills/neutral-source-handoff/SKILL.md').exists()
assert not (home/'skills/illegal-parent-skill/SKILL.md').exists()
rows=skill_ledger.list_entries()
claims=[r for r in rows if r['action']=='ordinary_skill_review' and r['evidence'].get('status')=='claimed']
assert len(claims)==1,claims
if not passive:assert claims[0]['evidence']['source_refs']==batch['source_refs'],claims
reports=list((home/'logs/ordinary-skill-reviews').glob('*/assessment.json'))
assert len(reports)==1 and 'Internal assessment cannot invoke tools' in reports[0].read_text()
assert (home/'config.yaml').read_bytes()==before
with sqlite3.connect(home/'cron/executions.db') as db:
 executions=db.execute('SELECT job_id,status FROM executions ORDER BY started_at').fetchall()
assert len(executions)==3 and all(row[1]=='completed' for row in executions),executions
assert TurnOutbox(config['turn_outbox_path']).snapshot()==outbox_before
print(json.dumps({'fresh_native_cron':True,'proposal_only':True,'ordinary_claims':len(claims),
 'unrelated_cron_authority_unchanged':True,'review_owner_source_rows':0,'passive_tool_producer':passive,'quality_credit':False}))
'''


@pytest.mark.parametrize('producer',['semantic','native_tools'])
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
    requests, assessment_reads = [], []
    class ScriptedBoundary(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, value):
            raw=json.dumps(value).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

        def do_GET(self):
            owner=yaml.safe_load((home/'config.yaml').read_bytes())['plugins']['pacomind']['owner_contact_id']
            self.send({'contact_id':owner,'events':[],'through':0,'head':0,'complete':True})

        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/v1/host/executions/assessments/read':
                owner=yaml.safe_load((home/'config.yaml').read_bytes())['plugins']['pacomind']['owner_contact_id']
                assert body['contact_id']==owner
                assessment_reads.append(body)
                rows=[{'source_id':'assessment-'+str(i),'source_version':str(i)*64,
                    'task_id':'task-'+str(i),'execution_id':'execution-'+str(i),'complete':True,
                    'attribution':'host_reported_machine_assessment_unverified','owner_approval':'unobserved',
                    'content':'Complete original artifact and fallible source review '+str(i)} for i in (1,2)] if producer=='semantic' else []
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
                if not any(m.get('role')=='tool' and '"staged": true' in str(m.get('content')) for m in messages):
                    name,args='skill_manage',{'action':'create','name':'neutral-source-handoff',
                        'content':'---\nname: neutral-source-handoff\ndescription: Consult complete supplied sources.\n---\nRead every supplied source before describing behavior.\n',
                            **({'_pacomind_task_assessment_batch':{'evaluator':{'oracle':'model_chosen:forbidden'}}} if producer=='semantic' else {})}
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
        api=ThreadingHTTPServer(('127.0.0.1',api_port),ScriptedBoundary)
        api_thread=threading.Thread(target=api.serve_forever,daemon=True);api_thread.start()
        result=subprocess.run([str(python),'-B','-c',DRIVER,native,producer,f'http://127.0.0.1:{server.server_port}/v1'],cwd=tmp_path,env=env,
            capture_output=True,text=True,timeout=120)
        assert result.returncode==0,result.stdout[-5000:]+result.stderr[-6000:]
        assert json.loads(result.stdout.splitlines()[-1])['fresh_native_cron']
        assert len(assessment_reads)>=(2 if producer=='semantic' else 1)
        native_calls=[row for row in requests if 'SYSTEM-GENERATED' in json.dumps(row.get('messages',[]))]
        assert len(native_calls)==4
    finally:
        if api is not None:
            api.shutdown();api.server_close();api_thread.join(timeout=2)
        server.shutdown();server.server_close();thread.join(timeout=2)
