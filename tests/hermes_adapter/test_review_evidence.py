"""Actual request result references, isolated from model-proposed provenance."""
from conftest import run_python


PROBE = r'''
import copy,hashlib,json,sys
from contextvars import copy_context
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
from protagine_hermes.review_evidence import capture,current
scope=NS(valid_participant=True,authority_lane='owner',platform='cli',session_id='session-a',turn_id='turn-a')
raw=json.dumps({'error':'regex parse error: look-around is not supported'})
messages=[{'role':'assistant','tool_calls':[{'id':'call-a','function':{'name':'search_files','arguments':'{}'}}]},
          {'role':'tool','tool_call_id':'call-a','content':raw}]
capture(scope,{'messages':messages})
actual=current();assert actual['session_id']=='session-a'
assert actual['source']=='native_request_tool_results'
assert actual['failures']==[{'tool_call_id':'call-a','tool_name':'search_files',
    'request_visible_result_sha256':hashlib.sha256(raw.encode()).hexdigest(),'error_class':'unsupported_regex_features'}]
assert raw not in json.dumps(actual)
fork=copy_context()
scope.session_id='session-b';scope.turn_id='turn-b'
capture(scope,{'messages':[]});assert current() is None
assert fork.run(current)==actual
snapshot=fork.run(current);snapshot['failures'].clear();assert fork.run(current)==actual
# A user message or a tool result with no native assistant call is not evidence.
for request in [
    {'messages':[{'role':'user','content':raw}]},
    {'messages':[messages[-1]]},
    {'messages':[messages[0],{'role':'tool','tool_call_id':'unmatched','content':raw}]},
    {'messages':[messages[0],{'role':'tool','tool_call_id':'call-a','content':json.dumps({'content':'error: not supported'})}]},
]:
    capture(scope,request);assert current() is None
capture(None,{'messages':messages});assert current() is None
guest=NS(**{**vars(scope),'authority_lane':'guest'})
capture(guest,{'messages':messages});assert current() is None
# Responses uses the same linked call/result contract, without prompt parsing.
capture(scope,{'input':[{'type':'function_call','call_id':'call-a','name':'search_files'},
    {'type':'function_call_output','call_id':'call-a','output':raw}]})
assert current()['failures']==actual['failures']
# A replayed result counts once; bounded evidence cannot retain a transcript.
capture(scope,{'messages':messages+[messages[-1]]*40})
assert len(current()['failures'])==1
print('REQUEST_VISIBLE_REVIEW_EVIDENCE_OK')
'''


def test_packaged_review_evidence_is_linked_and_context_scoped(artifacts,tmp_path):
    _,_,_,installed=artifacts
    result=run_python('-c',PROBE,installed,cwd=tmp_path)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'REQUEST_VISIBLE_REVIEW_EVIDENCE_OK' in result.stdout


TERMINAL_PROBE = r'''
import copy,hashlib,json,sqlite3,sys
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
from protagine_hermes.review_evidence import capture,current
from protagine_hermes.review_experience import observations,next_tool_batch,selected_pairs
scope=NS(valid_participant=True,authority_lane='owner',resolution_status='resolved',
    contact_id='owner',platform='whatsapp',session_id='first',turn_id='turn-first')
raw=json.dumps({'output':'fatal: fixture repository is unavailable','exit_code':128,'error':None})
args={'command':'git -C fixture-repository status'}
def request(raw=raw,args=args,name='terminal'):
    return {'messages':[{'role':'user','content':'Neutral fixture task'},
        {'role':'assistant','tool_calls':[{'id':'call','function':{'name':name,'arguments':json.dumps(args)}}]},
        {'role':'tool','tool_call_id':'call','content':raw}]}
entries=[]
with sqlite3.connect('state.db') as db:
    db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,'
        'tool_calls TEXT,tool_call_id TEXT,tool_name TEXT)')
    for i,session in enumerate(('first','second')):
        scope.session_id=session;scope.turn_id='turn-'+session
        req=request();capture(scope,req);failure=current()['failures'][0]
        assert failure['error_class']=='terminal_nonzero_exit'
        assert failure['request_visible_result_sha256']==hashlib.sha256(raw.encode()).hexdigest()
        name,value=observations(scope,req['messages'],[failure])[0]
        assert name is None
        entries.insert(0,{'action':'ordinary_tool_failure','skill':None,'evidence':value})
        db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',[
            (i*3+1,session,'user','Original task text',None,None,None),
            (i*3+2,session,'assistant','',json.dumps(req['messages'][1]['tool_calls']),None,None),
            (i*3+3,session,'tool',raw,None,'call','terminal')])
assert next_tool_batch(entries[:1]) is None
assert next_tool_batch([entries[0],entries[0]]) is None
batch=next_tool_batch(entries)
pairs=selected_pairs(batch,Path.cwd(),'owner')
assert len(pairs)==2 and all(p['error']=={'exit_code':128,'output':json.loads(raw)['output']} for p in pairs)
assert all(p['arguments']==json.dumps(args) for p in pairs)
assert raw not in json.dumps(entries) and args['command'] not in json.dumps(entries)
for field in ('arguments_sha256','request_visible_result_sha256'):
    changed=copy.deepcopy(entries);changed[0]['evidence'][field]='0'*64
    assert next_tool_batch(changed) is None
# A distinct command producing identical empty output and exit1 is not recurrence.
raw_one=json.dumps({'output':'','exit_code':1,'error':None})
mixed=[]
for session,command in [('first','grep absent first-file'),('second','grep absent second-file')]:
    scope.session_id=session;scope.turn_id='turn-'+session
    req=request(raw_one,{'command':command});capture(scope,req)
    name,value=observations(scope,req['messages'],current()['failures'])[0]
    mixed.insert(0,{'action':'ordinary_tool_failure','skill':name,'evidence':value})
assert next_tool_batch(mixed) is None
# Reused native call IDs still distinguish later turns after the same skill view.
view=[{'role':'assistant','tool_calls':[{'id':'view','function':{'name':'skill_view',
    'arguments':json.dumps({'name':'fixture-skill'})}}]},
    {'role':'tool','tool_call_id':'view','content':json.dumps({'success':True,
        'name':'fixture-skill','content':'Neutral fixture instructions'})}]
scope.session_id='same-session';attributed=[]
for turn,command in [('one','git status'),('two','git status'),('two','git diff')]:
    scope.turn_id=turn;req=request(raw,args={'command':command})
    req['messages'][1:1]=view;capture(scope,req)
    skill,evidence=observations(scope,req['messages'],current()['failures'])[0]
    assert skill=='fixture-skill';attributed.append(evidence['observation_id'])
assert len(set(attributed))==3
# Cancellation, running/unknown status and untyped values provide no new failure.
for value in (0,None,-1,130,137,143,'1',True,1.0):
    capture(scope,request(json.dumps({'output':'fatal: arbitrary text','exit_code':value,'error':None})))
    assert current() is None
for status in ('interrupted','cancelled','canceled'):
    capture(scope,request(json.dumps({'output':'','exit_code':1,'status':status,'error':'interrupted'})))
    assert current() is None
for req in [request(raw_one,{}),request(raw_one,{'command':' '}),request(raw_one,name='execute_code'),
    request(json.dumps({'output':raw_one,'exit_code':0,'status':'success'}))]:
    capture(scope,req);assert current() is None
# Responses API carries the same exact command witness.
capture(scope,{'input':[{'type':'function_call','call_id':'call','name':'terminal','arguments':json.dumps(args)},
    {'type':'function_call_output','call_id':'call','output':raw}]})
assert current()['failures'][0]['arguments_sha256']==entries[0]['evidence']['arguments_sha256']
# Diagnostic revalidation rejects changed persisted command bytes even if the result is unchanged.
with sqlite3.connect('state.db') as db:
    db.execute('UPDATE messages SET tool_calls=? WHERE id=2',
        (json.dumps(request(args={'command':'different command'})['messages'][1]['tool_calls']),))
try:selected_pairs(batch,Path.cwd(),'owner')
except ValueError as error:assert 'command no longer matches' in str(error)
else:raise AssertionError('Changed native command was accepted')
print('TERMINAL_EXIT_EVIDENCE_AND_DIAGNOSTICS_OK')
'''


def test_packaged_terminal_exit_evidence_keeps_exact_process_outcomes(artifacts,tmp_path):
    _,_,_,installed=artifacts
    result=run_python('-c',TERMINAL_PROBE,installed,cwd=tmp_path)
    assert 'TERMINAL_EXIT_EVIDENCE_AND_DIAGNOSTICS_OK' in result.stdout
