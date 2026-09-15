"""Native API owner recall uses the current attested turn, never ambient identity."""
import importlib.util
import os

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import copy,json,os,socket,sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2])
if sys.argv[3]:sys.path.append(sys.argv[3])
if sys.argv[5]:sys.path.insert(0,sys.argv[5])
mode=sys.argv[4];trusted=mode=='attested'
owner='contact-b' if mode=='other_owner' else 'contact-a'
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import host
from protagine.turns import get_turn_idempotency_ledger
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
secret='neutral-api-recall-key'
keyring=home/'keyring.json'
keyring.write_text(json.dumps({'version':1,'principals':[{
    'principal':'neutral-fixture','status':'active','viewer_person_id':'contact-a','person_ids':['contact-a'],
    'scopes':['context:read','turns:write','memory:read'],'audiences':['viewer'],
    'credentials':[{'id':'one','secret':secret,'status':'active'}]}]}));keyring.chmod(0o600)
credential='' if mode=='unauthenticated' else secret
home.joinpath('config.yaml').write_text(json.dumps({
    'plugins':{'enabled':['protagine'],'protagine':{'url':'http://fixture','owner_contact_id':owner,
        'api_key':credential,
        'attested_system_platforms':[] if mode=='unattested' else ['api_server'],
        'turn_writer_platforms':['api_server']}},
    'memory':{'provider':'protagine-memory','config':{'url':'http://fixture','contact_id':'contact-a','api_key':credential}}}))
home.joinpath('SOUL.md').write_text('Neutral API fixture. Answer the current question.')
fact='My neutral orchard badge is cobalt-716.'
ledger=get_turn_idempotency_ledger(os.environ['PROTAGINE_STATE_DIR'])
ledger.record_source('earlier-neutral-source',contact_id='contact-a',session_id='earlier-channel',
    messages=[{'role':'user','content':fact}],derive_claims=False)
app=FastAPI();calls=[]
app.add_middleware(ApiKeyMiddleware,keyring_path=str(keyring))
app.include_router(host.router);app.include_router(host.v2_router)
api=TestClient(app);original_client=httpx.Client
def respond(request):
    if request.url.path=='/v1/host/mind/facts':return httpx.Response(200,json={'facts':[]})
    if request.url.path=='/v1/host/contacts/resolve':
        # An unknown participant must not acquire the configured owner's memory.
        return httpx.Response(200,json={'contact_id':None})
    response=api.request(request.method,request.url.path,params=request.url.params,
                         headers=dict(request.headers),content=request.content)
    calls.append((request.url.path,response.status_code))
    return httpx.Response(response.status_code,content=response.content,headers=response.headers)
httpx.Client=lambda **kwargs:original_client(**{**kwargs,'transport':httpx.MockTransport(respond)})
def no_network(*args,**kwargs):raise AssertionError('Controlled API and provider callback only')
socket.socket.connect=no_network;socket.create_connection=no_network
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
assert get_plugin_manager()._plugins['protagine'].enabled
from run_agent import AIAgent
from hermes_state import SessionDB
from agent import relay_runtime
from gateway.platforms.api_server import APIServerAdapter
from gateway.session_context import clear_session_vars,set_session_vars
import protagine_hermes,run_agent
openai_target='run_agent.OpenAI' if 'OpenAI' in vars(run_agent) else 'agent.process_bootstrap.OpenAI'
tools_target='run_agent' if 'get_tool_definitions' in vars(run_agent) else 'model_tools'
physical=[];scopes=[];client=MagicMock()
def answer(**kwargs):
    physical.append(copy.deepcopy(kwargs))
    turn=relay_runtime.active_turn(agent.session_id)
    assert turn is not None and turn.lease.platform=='api_server'
    scope=protagine_hermes._TRANSPORT_SCOPES.for_execution(
        session_id=agent.session_id,task_id=turn.task_id,turn_id=turn.turn_id)
    assert scope is not None
    scopes.append(scope)
    assert provider._prefetch_contact(agent.session_id+'-unrelated')==''
    return NS(choices=[NS(message=NS(content='NEUTRAL_API_DONE',tool_calls=None),finish_reason='stop')],
              model='neutral/served',usage=None)
client.chat.completions.create.side_effect=answer
with patch(openai_target,return_value=client),patch(tools_target+'.get_tool_definitions',return_value=[]),patch(tools_target+'.check_toolset_requirements',return_value={}):
    agent=AIAgent(api_key='neutral',base_url='http://127.0.0.1:1/v1',provider='openai',model='neutral/model',
        quiet_mode=True,skip_context_files=True,skip_memory=False,platform='api_server',max_iterations=1,
        session_db=SessionDB(home/'state.db'))
    agent._use_prompt_caching=False;agent.save_trajectories=False;agent.compression_enabled=False
    provider=agent._memory_manager.get_provider('protagine')
    assert provider is not None
    # Use the public native ingress's real session binder, including its chat ID.
    tokens=APIServerAdapter._bind_api_server_session(chat_id=agent.session_id,
        session_id=agent.session_id,session_history_delivery='1')
    if mode=='other_sender':
        tokens=set_session_vars(platform='api_server',chat_id=agent.session_id,
            session_id=agent.session_id,user_id='unknown-participant',async_delivery=False)
    try:
        assert provider._prefetch_contact(agent.session_id)==''
        result=agent.run_conversation('What is my neutral orchard badge?',task_id='ordinary-api')
        assert result['final_response']=='NEUTRAL_API_DONE',result
        assert len(physical)==1
        contents=json.dumps(physical[0]['messages'])
        assert (fact in contents)==trusted,(mode,calls,physical)
        assert ('earlier-neutral-source' in contents)==trusted
        assert any(path=='/v1/host/context/assemble' and status==200 for path,status in calls)==trusted,calls
        if mode=='unauthenticated':
            assert any(status==401 for path,status in calls),calls
        if mode!='unattested':
            assert scopes[0].resolution_status=='attested_system'
            assert scopes[0].contact_id==owner
        # Finished scopes stay registered, but cannot authorize another prefetch.
        assert protagine_hermes._TRANSPORT_SCOPES.for_session(agent.session_id) is not None
        assert relay_runtime.active_turn(agent.session_id) is None
        count=len(calls)
        assert provider.prefetch('What is my neutral orchard badge?',session_id=agent.session_id)==''
        assert len(calls)==count
    finally:
        clear_session_vars(tokens)
        agent.close()
print(json.dumps({'mode':mode,'actual_native_api_turn':True,'current_recollection_visible':trusted,
    'finished_scope_rejected':True,'unrelated_session_rejected':True,'model_calls':0,
    'controlled_provider_calls':len(physical),'default_context_authority':os.environ['PROTAGINE_MEMORY_DEFAULT_CONTEXT_AUTHORITY']}))
'''


@pytest.mark.parametrize('mode', ['attested', 'unattested', 'unauthenticated', 'other_owner', 'other_sender'])
def test_native_api_prefetch_uses_current_resolved_scope(artifacts, tmp_path, mode):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install the qualified native Hermes release')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path / 'profile'), PROTAGINE_STATE_DIR=str(tmp_path / 'protagine'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path / 'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PROTAGINE_GENERAL_PLUGIN_ACTIVE='1',
        PROTAGINE_MEMORY_TURN_WRITER='disabled', PROTAGINE_MEMORY_WORKER_TOOLS='0',
        PROTAGINE_MEMORY_DEFAULT_CONTEXT_AUTHORITY='none', PROTAGINE_OWNER_CONTACT_ID='contact-a',
        PROTAGINE_GUARD_CHAT_MODE='off')
    run_python('-I', '-c', PROBE, artifacts[3], ROOT / 'sidecar',
        os.environ.get('PROTAGINE_TEST_DEPENDENCY_PATH', ''), mode,
        os.environ.get('HERMES_TEST_SOURCE', ''), cwd=tmp_path, env=env)
