"""Opted-in qualification receipts use real native callbacks and the gateway store."""
import importlib.util
import os
from pathlib import Path

import pytest

from conftest import run_python


PROBE = r'''
import asyncio, base64, copy, hashlib, json, socket, sqlite3, sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
sys.path[:0] = [sys.argv[1], *([sys.argv[2]] if sys.argv[2] else [])]
def no_network(*a, **kw): raise AssertionError('No network in request receipt tests')
socket.socket.connect = no_network
socket.create_connection = no_network
from agent import relay_runtime, relay_llm
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platform_registry import platform_registry, PlatformEntry
from gateway.session import SessionStore
from protagine_hermes.native_memory import NativeMemoryRequests
from protagine_hermes.task_handoffs import TaskHandoffs, TaskHandoffError
from protagine_hermes.native_task_platform import (
    ACTIVE, NativeTaskAdapter, bind_native_turn, request_image_observer,
    TASK_IMAGE_RECEIPTS_METADATA, _request_image_hashes)

home = Path('profile').absolute(); home.mkdir(exist_ok=True)
(home/'config.yaml').write_text('{}\n')
platform_registry.register(PlatformEntry(name='protagine_task',label='Protagine task',
    adapter_factory=lambda config: None,check_fn=lambda: True))
config = GatewayConfig(sessions_dir=home/'sessions')
store = SessionStore(config.sessions_dir, config)
owner = 'owner'
@contextmanager
def database():
    db = sqlite3.connect('tasks.sqlite3'); db.row_factory = sqlite3.Row
    try:
        with db: yield db
    finally: db.close()
source = {'version':1,'principal':'hermes:cli','source_session_id':'admitted-input',
    'input_refs':[{'source_id':'question','input_message_hash':'a'*64}],
    'source_refs':[{'source_id':'question','source_version':'b'*64}],
    'watermark':0,'contact_id':'owner'}
handoffs = TaskHandoffs(database,lambda value,dependencies=None: copy.deepcopy(value),
    lambda value,require_task_grant: owner)
adapter = NativeTaskAdapter(PlatformConfig(enabled=True),handoffs=handoffs)
adapter.set_session_store(store)
jpeg = Path(sys.argv[4]).read_bytes()
expected = hashlib.sha256(jpeg).hexdigest()
assert jpeg[:2] == b'\xff\xd8' and jpeg[-2:] == b'\xff\xd9'
url = 'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode()
body = {'model':'fixture/model','messages':[{'role':'user','content':[
    {'type':'text','text':'DO NOT RETAIN THIS PROMPT'},
    {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,c3RhbGU='}}]}]}
sent = []
replace_image = True
def memory(request,scope):
    result = copy.deepcopy(request)
    if replace_image:
        result['messages'][0]['content'][1]['image_url']['url'] = url
    return {'request':result}
boundary = NativeMemoryRequests(memory)
coordinator = relay_runtime.SESSION_COORDINATOR

def begin(name, *, purpose='qualification', opt_in=True):
    row = handoffs.admit(request_id=name,request='Read the admitted image',source_input=source,
        experience=purpose,request_image_receipts=opt_in)
    src = adapter.build_source(chat_id=row['id'],chat_type='dm',user_id='owner',message_id=row['id'])
    entry = store.get_or_create_session(src,touch_activity=False)
    lease = coordinator.acquire_conversation(profile_key=relay_runtime.current_profile_key(),
        session_id=entry.session_id,platform='protagine_task')
    turn = coordinator.begin_turn(lease,task_id=name+'-task',turn_id=name+'-turn')
    fields = {'platform':'protagine_task','session_id':entry.session_id,
              'task_id':turn.task_id,'turn_id':turn.turn_id}
    active = {'handoffs':handoffs,'id':row['id'],'adapter':adapter,'supplied':None,
              'session_key':entry.session_key,'source':src}
    token = ACTIVE.set(active)
    bind_native_turn(**fields)
    scope = NS(**fields,valid_participant=True,contact_id='owner')
    assert boundary.bind(scope)
    return NS(row=row,entry=entry,scope=scope,lease=lease,turn=turn,token=token)

def send(run, *, streaming=False):
    def provider(request):
        sent.append(copy.deepcopy(request))
        return {'model':'fixture/model','choices':[{'message':{'role':'assistant','content':'OK'},
                                                    'finish_reason':'stop'}]}
    metadata = {'api_mode':'chat_completions','api_request_id':'call-'+str(len(sent)),
                'call_role':'primary'}
    if streaming:
        def factory(request):
            provider(request)
            return iter([{'choices':[{'delta':{'content':'OK'},'finish_reason':'stop'}]}])
        list(relay_llm.stream(body,factory,session_id=run.scope.session_id,name='fixture',
            model_name='fixture/model',metadata=metadata,finalizer=lambda: {'choices':[]}))
    else:
        relay_llm.execute(body,provider,session_id=run.scope.session_id,name='fixture',
            model_name='fixture/model',metadata=metadata)

def receipt(run):
    return store.get_session_metadata(run.entry.session_key,TASK_IMAGE_RECEIPTS_METADATA)

def end(run):
    coordinator.end_turn(run.turn,outcome='success')
    coordinator.release_conversation(run.lease)
    asyncio.run(run.lease.host.relay.subscribers.flush_async())
    ACTIVE.reset(run.token)

def admission():
    for purpose in (None,'operational'):
        try:
            handoffs.admit(request_id=str(purpose),request='Read',source_input=source,
                           experience=purpose,request_image_receipts=True)
        except TaskHandoffError: pass
        else: raise AssertionError('Ordinary opt-in accepted')
    for invalid in (1,0,'true',None,{},[]):
        try:
            handoffs.admit(request_id='invalid',request='Read',source_input=source,
                           experience='qualification',request_image_receipts=invalid)
        except TaskHandoffError: pass
        else: raise AssertionError('Non-boolean opt-in accepted')
    assert handoffs.count() == 0
    row = handoffs.admit(request_id='one',request='Read',source_input=source,experience='qualification')
    assert 'request_image_receipts' not in row['source']
    try:
        handoffs.admit(request_id='one',request='Read',source_input=source,
                       experience='qualification',request_image_receipts=True)
    except TaskHandoffError: pass
    else: raise AssertionError('An existing task gained capture')

def filtered_callbacks():
    run = begin('selected')
    try:
        send(run); send(run,streaming=True)
        saved = receipt(run)
        assert saved['handoff_id'] == run.row['id']
        assert saved['provider_delivery'] == saved['network_wire'] == 'unobserved'
        assert [r['kind'] for r in saved['observations']] == ['nonstreaming','streaming']
        assert [r['sequence'] for r in saved['observations']] == [1,2]
        for observation, actual in zip(saved['observations'], sent):
            assert observation['complete'] and observation['session_id'] == run.scope.session_id
            image = observation['images'][0]
            delivered = base64.b64decode(actual['messages'][0]['content'][1]['image_url']['url'].split(',')[1])
            assert image['sha256'] == hashlib.sha256(delivered).hexdigest() == expected
            assert image['bytes'] == len(jpeg) and delivered == jpeg
        assert 'DO NOT RETAIN' not in json.dumps(saved) and url not in json.dumps(saved)
        assert 'c3RhbGU=' not in json.dumps(saved)
        reopened = SessionStore(config.sessions_dir,config)
        assert reopened.get_session_metadata(run.entry.session_key,TASK_IMAGE_RECEIPTS_METADATA) == saved
    finally: end(run)
    assert not boundary._turns

def scope_exclusions():
    global owner
    for name,purpose,opt_in in [('ordinary','operational',False),('unclassified',None,False),
                                ('unselected','qualification',False)]:
        run = begin(name,purpose=purpose,opt_in=opt_in)
        try:
            send(run); send(run,streaming=True)
            assert receipt(run) is None
        finally: end(run)
    run = begin('exact')
    try:
        for mismatch in ({'task_id':'another-task'},{'turn_id':'old-turn'},
                         {'session_id':'another-session'},{'contact_id':'another-owner'},
                         {'platform':'cli'}):
            assert request_image_observer(NS(**{**vars(run.scope),**mismatch})) is None
        # A joined child inherits context/Relay ancestry but not the root task's receipt.
        child_lease = coordinator.acquire_conversation(profile_key=relay_runtime.current_profile_key(),
            session_id='child',platform='subagent',parent_session_id=run.scope.session_id)
        child_turn = coordinator.begin_turn(child_lease,task_id='child-task',turn_id='child-turn')
        try:
            send(NS(scope=NS(session_id='child')))
            assert receipt(run) is None
        finally:
            coordinator.end_turn(child_turn,outcome='success')
            coordinator.release_conversation(child_lease)
        owner = 'revoked-owner'
        send(run)
        assert receipt(run) is None
        owner = 'owner'
        native = {key:getattr(run.scope,key) for key in ('session_id','task_id','turn_id')}
        handoffs.bind(run.row['id'],{**native,'task_id':'replacement-task'})
        send(run)
        assert receipt(run) is None
        handoffs.bind(run.row['id'],native)
        # A route reset between callback binding and writing cannot receive the old receipt.
        original = run.scope.session_id
        store._update_entry(run.entry.session_key,lambda entry: setattr(entry,'session_id','replacement'))
        send(run)
        assert receipt(run) is None
        store._update_entry(run.entry.session_key,lambda entry: setattr(entry,'session_id',original))
        persist = store._update_entry
        store._update_entry = lambda *_: False
        before = len(sent)
        send(run)
        assert len(sent) == before + 1 and receipt(run) is None
        store._update_entry = persist
        send(run)
        assert len(receipt(run)['observations']) == 1
    finally:
        owner = 'owner'; end(run)

def bounds():
    global replace_image
    run = begin('bounds'); replace_image = False
    try:
        observer = request_image_observer(run.scope)
        for i in range(33): observer(body,kind='nonstreaming')
        assert len(receipt(run)['observations']) == 32 and receipt(run)['limit_reached']
        assert len(json.dumps(receipt(run))) < 32768
    finally: end(run)
    def inspect(block): return _request_image_hashes({'messages':[{'role':'user','content':block}]})
    known = {'type':'input_image','image_url':url}
    # Valid small images with long structural paths exhaust metadata bytes
    # before the request quota; the limiting receipt retains no prompt payload.
    run = begin('receipt-bytes')
    try:
        blocks = [known] * 8
        for _ in range(12): blocks = {'content':blocks}
        bounded_body = {'messages':[{'role':'user','content':blocks}]}
        assert _request_image_hashes(bounded_body)['complete']
        observer = request_image_observer(run.scope)
        for _ in range(32): observer(bounded_body,kind='nonstreaming')
        saved = receipt(run)
        assert saved['limit_reason'] == 'receipt_byte_limit'
        assert len(saved['observations']) < 32 and len(json.dumps(saved).encode()) <= 65536
    finally: end(run)
    assert inspect([known])['images'][0]['sha256'] == expected
    assert inspect([{'type':'image','source':{'type':'base64','media_type':'image/jpeg',
        'data':base64.b64encode(jpeg).decode()}}])['images'][0]['sha256'] == expected
    for block,error in [([known]*9,'image_count_limit'),
        ([{'type':'image_url','image_url':{'url':'https://invalid.example/image.jpg'}}],'image_bytes_unavailable'),
        ([{'type':'input_image','image_url':'data:image/jpeg;base64,invalid!'}],'invalid_image_encoding'),
        ([{'type':'input_image','image_url':'data:image/jpeg;base64,'+base64.b64encode(b'x'*(2*1024*1024+1)).decode()}],'image_byte_limit'),
        ([{}]*4097,'request_structure_limit')]:
        result=inspect(block)
        assert not result['complete'] and error in result['errors'],result

globals()[sys.argv[3]]()
relay_runtime.HOST_REGISTRY.shutdown_all()
print(json.dumps({'case':sys.argv[3],'passed':True,'network_calls':0,'model_calls':0,
                  'native_source':sys.modules['agent.relay_runtime'].__file__}))
'''


@pytest.mark.parametrize('case', ['admission', 'filtered_callbacks', 'scope_exclusions', 'bounds'])
def test_task_request_image_receipts(artifacts, tmp_path, case):
    native = os.environ.get('PROTAGINE_TEST_HERMES_PATH', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native request receipt tests')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PYTHON_DOTENV_DISABLED='1')
    fixture = Path(__file__).parent/'fixtures/request-receipt.jpg'
    run_python('-I', '-c', PROBE, artifacts[3], native, case, fixture, cwd=tmp_path, env=env)
