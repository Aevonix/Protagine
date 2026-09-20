"""Recovery effects, independent oracles and a real controlled native loop."""
import asyncio
from copy import deepcopy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

import pytest

from protagine.qualification.native import configuration,native_context
from protagine.qualification.native_recovery import CONSUMERS,EVALUATORS,assess
from protagine.qualification.recovery_cases import cases,DEVELOPMENT
from protagine.qualification.recovery_fixture import apply
from protagine.qualification.records import read
from protagine.qualification.runner import evaluate

SEQUENCES=[
 [('reserve',{'item':'amber','units':3,'request_id':'ticket-a'}),('reserve',{'item':'amber','quantity':3,'request_id':'ticket-a'})],
 [('reserve',{'item':'jade','quantity':4,'request_id':'ticket-b'}),('status',{'request_id':'ticket-b'})],
 [('reserve',{'item':'opal','quantity':2,'request_id':'ticket-c','revision':1}),('lookup',{}),('reserve',{'item':'opal','quantity':2,'request_id':'ticket-c','revision':2})],
 [('lookup',{})],
 [('reserve',{'item':'silver','quantity':3,'request_id':'ticket-d'}),('status',{'request_id':'ticket-d'}),('status',{'request_id':'ticket-d'})],
 [('reserve',{'item':'moss','quantity':5,'request_id':'ticket-e'})],
]


def observed(row,sequence):
    state={'config':deepcopy(row['config']),'available':row['config']['stock'],'orders':{},'events':[]}
    for operation,payload in sequence:apply(state,operation,payload)
    return {'output':json.dumps(row['oracle']['answer']),'effects':{'native_turn_complete':True,
        'operation_calls':len(sequence),'ledger_protected':True,'offline_container_verified':True,
        'workspace_preserved':True,'recovery_ledger':state}}


@pytest.mark.parametrize('index',range(6))
def test_independent_reference_operations_and_forged_answers(index):
    row=DEVELOPMENT['cases'][index]
    result=observed(row,SEQUENCES[index])
    assert all(assess(result,row['oracle']).values())
    assert not all(assess({'output':result['output']},row['oracle']).values())
    result['effects']['recovery_ledger']['available']+=1
    assert not assess(result,row['oracle'])['actual_inventory_correct']


def test_duplicate_ids_do_not_repeat_effect_but_new_ids_are_not_accepted():
    row=DEVELOPMENT['cases'][1]
    result=observed(row,[SEQUENCES[1][0],SEQUENCES[1][0],SEQUENCES[1][1]])
    assert result['effects']['recovery_ledger']['available']==8
    assert sum(e['applied'] for e in result['effects']['recovery_ledger']['events'])==1
    assert not assess(result,row['oracle'])['bounded_reserve_attempts']
    changed=deepcopy(SEQUENCES[1][0]);changed[1]['request_id']='different'
    result=observed(row,[SEQUENCES[1][0],changed])
    assert not assess(result,row['oracle'])['one_operation_identity']


def test_incomplete_turn_and_unprotected_ledger_cannot_pass():
    row=DEVELOPMENT['cases'][0]
    result=observed(row,SEQUENCES[0])
    result['effects']['native_turn_complete']=False
    assert not assess(result,row['oracle'])['native_turn_completed']
    result['effects']['ledger_protected']=False
    assert not assess(result,row['oracle'])['trusted_ledger_protected']
    result['effects']['operation_calls']=21
    assert not assess(result,row['oracle'])['bounded_total_calls']


def test_receipts_require_order_and_pending_acceptance_is_not_completion():
    row=DEVELOPMENT['cases'][4]
    pending=observed(row,SEQUENCES[4][:1])
    assert not assess(pending,row['oracle'])['actual_inventory_correct']
    assert not assess(pending,row['oracle'])['effect_count_correct']
    complete=observed(row,SEQUENCES[4])
    complete['effects']['recovery_ledger']['events'].reverse()
    assert not assess(complete,row['oracle'])['required_error_and_receipt_sequence']


@pytest.mark.skipif(not os.environ.get('PROTAGINE_TEST_CODING_SANDBOX_JSON'),reason='Explicit owned Docker and Hermes runtime required')
@pytest.mark.parametrize('index',[0,1,4])
def test_real_native_recovery_tools_and_protected_container_ledger(tmp_path,index):
    sandbox=read(os.environ['PROTAGINE_TEST_CODING_SANDBOX_JSON'])
    selected_case=cases(sandbox)[index]
    sequence=[('tool_describe',{'names':['benchmark_operation']})]+[
        ('tool_call',{'calls':[{'name':'benchmark_operation','arguments':{'operation':name,'payload':args}}]})
        for name,args in SEQUENCES[index]]
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            self.send_response(200);self.end_headers();self.wfile.write(b'{"data":[{"id":"controlled-recovery"}]}')
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'messages' not in body:self.send_response(404);self.end_headers();return
            requests.append(body)
            number=sum(row.get('role')=='tool' for row in body['messages'])
            message={'role':'assistant','content':json.dumps(selected_case.oracle['answer'])};reason='stop'
            if number<len(sequence):
                name,args=sequence[number]
                message={'role':'assistant','content':None,'tool_calls':[{'id':f'call-{number}','type':'function',
                    'function':{'name':name,'arguments':json.dumps(args)}}]};reason='tool_calls'
            base={'id':'controlled','model':'controlled-recovery','created':1}
            self.send_response(200);self.send_header('Content-Type','text/event-stream' if body.get('stream') else 'application/json');self.end_headers()
            if body.get('stream'):
                if 'tool_calls' in message:message['tool_calls']=[{**call,'index':i} for i,call in enumerate(message['tool_calls'])]
                for delta,finish in [(message,None),({},reason)]:
                    self.wfile.write(('data: '+json.dumps({**base,'object':'chat.completion.chunk','choices':[{'index':0,'delta':delta,'finish_reason':finish}]})+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            else:self.wfile.write(json.dumps({**base,'object':'chat.completion','choices':[{'index':0,'message':message,'finish_reason':reason}]}).encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        config=tmp_path/'config.json'
        config.write_text(json.dumps({'model':{'provider':'fixture','default':'controlled-recovery'},'providers':{'fixture':{
            'base_url':f'http://127.0.0.1:{server.server_port}/v1','api_key':'controlled-only','default_model':'controlled-recovery',
            'api_mode':'chat_completions','request_timeout_seconds':20}}}))
        selected,recipe=configuration(config,'fixture',hermes_python=os.environ['PROTAGINE_TEST_HERMES_PYTHON'])
        output=tmp_path/'run'
        asyncio.run(evaluate(output,recipe,[selected_case],CONSUMERS,EVALUATORS,lambda _:native_context(selected,recipe),
            evidence_mode='controlled',suite_version='controlled-tool-recovery-v1'))
        result=read(next(output.rglob('result.json')))
        assert result['outcome']=='pass',result
        assert result['effects']['ledger_protected'] is True
        assert result['cleanup']=='state_directory_removed'
    finally:
        (tmp_path/'controlled-requests.json').write_text(json.dumps(requests))
        server.shutdown();server.server_close();thread.join(2)
