"""Native fixture tool backed by a private ledger in the existing Docker sandbox."""
from contextlib import contextmanager
import json
import shlex
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from protagine.qualification.coding_worker import prepare as coding_prepare
from protagine.qualification.coding_sandbox import terminal_configuration
from protagine.qualification.native_worker import main

SCHEMA={'name':'benchmark_operation','description':'Operate on synthetic inventory. lookup reads item, available and revision. reserve accepts item, quantity, request_id, optional revision. status and cancel accept request_id. Preserve a request ID across retries.',
 'parameters':{'type':'object','properties':{'operation':{'type':'string','enum':['lookup','reserve','status','cancel']},
  'payload':{'type':'object'}},'required':['operation','payload'],'additionalProperties':False}}


@contextmanager
def prepare(request,state,arguments,config):
    from tools.registry import registry
    from tools.terminal_tool import ensure_task_env
    with coding_prepare(request,state,arguments,config) as observe_coding:
        env=ensure_task_env(arguments['session_id'])
        container=env._container_id
        root='/workspace/.fixture-'+uuid.uuid4().hex
        helper=Path(__file__).with_name('recovery_fixture.py').read_text()
        setup="""import json,pathlib,sys
payload=json.loads(sys.stdin.read());root=pathlib.Path(payload['root']);root.mkdir(mode=0o700)
(root/'operation.py').write_text(payload['helper'])
(root/'ledger.json').write_text(json.dumps({'config':payload['config'],'available':payload['config']['stock'],'orders':{},'events':[]}))
"""
        def execute(script,args=(),payload=None):
            result=subprocess.run([env._docker_exe,'exec','--user','0:0','-i',container,
                'python3','-I','-B',*script,*args],input=json.dumps(payload or {}),
                capture_output=True,text=True,timeout=15,check=True)
            return result.stdout
        execute(['-c',setup],payload={'root':root,'helper':helper,'config':request['inputs']['fixture']})
        probe_code="""import json,pathlib,sys
root=pathlib.Path(sys.argv[1]);checks=[]
for operation in [lambda:(root/'ledger.json').read_text(),lambda:(root/'ledger.json').write_text('{}'),lambda:(root/'operation.py').unlink(),lambda:root.rename(str(root)+'-moved')]:
 try:operation();checks.append(False)
 except PermissionError:checks.append(True)
print(json.dumps(checks))
"""
        probe=env.execute('python3 -I -c '+shlex.quote(probe_code)+' '+shlex.quote(root),timeout=5)
        protected=probe.get('returncode')==0 and json.loads(probe.get('output',''))==[True]*4
        if not protected:raise RuntimeError('Fixture ledger is accessible to candidate tools')
        calls={'total':0};captured={};lock=threading.Lock()
        def operation(args,**kwargs):
            with lock:
                calls['total']+=1
                if calls['total']>20:return json.dumps({'error':'fixture_call_limit','effect_performed':False})
                if set(args)!={'operation','payload'} or args['operation'] not in {'lookup','reserve','status','cancel'} or not isinstance(args['payload'],dict):
                    return json.dumps({'error':'invalid_operation_arguments'})
                response=json.loads(execute([root+'/operation.py'],[root],args))['reply']
            return response if isinstance(response,str) else json.dumps(response)
        registry.register(name='benchmark_operation',toolset='terminal',schema=SCHEMA,
                          handler=operation,description=SCHEMA['description'],max_result_size_chars=10000)
        original_cleanup=env.cleanup
        def snapshot():
            return json.loads(execute([root+'/operation.py'],[root],{'operation':'__snapshot'}))
        def cleanup(*args,**kwargs):
            if getattr(env,'_container_id',None) and not captured:captured.update(snapshot())
            return original_cleanup(*args,**kwargs)
        def evidence(agent,response):
            ordinary=observe_coding(agent,response)
            files=ordinary.pop('coding_files')
            return {**ordinary,'recovery_ledger':dict(captured) if captured else snapshot(),
                'ledger_protected':protected,'operation_calls':calls['total'],
                'workspace_preserved':files==request['inputs']['repository']}
        try:
            with patch.object(env,'cleanup',cleanup):yield evidence
        finally:
            registry.deregister('benchmark_operation')


if __name__=='__main__':
    state=Path(sys.argv[1]).parent
    request=json.loads(Path(sys.argv[1]).read_text())
    config=json.loads((state/'config.yaml').read_text())
    config['terminal']=terminal_configuration(request['inputs']['sandbox'])
    (state/'config.yaml').write_text(json.dumps(config))
    raise SystemExit(main(prepare))
