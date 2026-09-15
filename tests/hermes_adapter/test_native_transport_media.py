"""Selected Hermes stages actual provider identity into its native hook."""
import importlib.util
import json
import os

import pytest
from conftest import run_python
from test_native_current_work import environment


PROBE = r'''
import base64,contextvars,io,json,logging,os,socket,sys,threading
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
if sys.argv[2]:sys.path.insert(0,sys.argv[2])
home=Path(os.environ['HERMES_HOME']);home.mkdir()
(home/'config.yaml').write_text('plugins: {enabled: []}\n')
def no_network(*args,**kwargs):raise AssertionError('Native attachment qualification is local')
socket.socket.connect=no_network;socket.create_connection=no_network
from PIL import Image
image=home/'cache/images/original.jpg';image.parent.mkdir(parents=True)
out=io.BytesIO();Image.new('RGB',(16,16),'blue').save(out,format='PNG');image.write_bytes(out.getvalue())
from gateway.platforms.event import MessageEvent,MessageType
from gateway.session_context import set_session_vars,clear_session_vars
from agent.image_routing import build_native_content_parts
from agent.turn_context import _stage_turn_user_message,_collect_pre_llm_call_context
from agent.turn_finalizer import _apply_output_hooks
from agent.relay_runtime import (ConversationLease,NoopRelayRuntime,SESSION_COORDINATOR,current_profile_key)
from hermes_cli import lifecycle
from hermes_cli.plugins import get_plugin_manager,PluginContext,PluginManifest
from protagine_hermes.transport_media import TransportMedia
from protagine_hermes.client import TurnOutbox
scope=NS(valid_participant=True,platform='whatsapp',sender_id='sender',session_id='session',task_id='task',turn_id='turn')
agent=NS(session_id='session',platform='whatsapp',model='fixture',_user_id='sender',_persist_disabled=False)
manager=get_plugin_manager()
context=PluginContext(PluginManifest(name='attachment-lifecycle-fixture'),manager)
caller=threading.current_thread()
isolated=contextvars.ContextVar('fixture_pre_hook',default='caller')
for mode in ('text','native'):
    carrier=TransportMedia()
    event=MessageEvent(text='This is my reference drawing.',message_type=MessageType.PHOTO,
        source=NS(platform=NS(value='whatsapp'),user_id='sender',chat_id='chat'),message_id=mode+'-image',
        media_urls=[str(image)],media_types=['image/png'])
    content=('Runtime vision interpretation. '+event.text if mode=='text'
             else build_native_content_parts(event.text,[str(image)])[0])
    native,_=_stage_turn_user_message(agent,content,None,None,event.message_id,None,None)
    seen=[]
    def pre(**kwargs):
        assert threading.current_thread() is not caller
        isolated.set('pre-worker')
        seen.append(kwargs)
        carrier.bind(scope,kwargs)
    def post(**kwargs):
        assert threading.current_thread() is not caller
        # The real dispatcher copied the caller context independently for this
        # hook. A ContextVar assigned by pre_llm_call cannot carry these bytes.
        assert isolated.get()=='caller'
        retained=carrier.for_turn(scope)
        assert retained and retained['provider_message_id']==event.message_id
        receipt=TurnOutbox(home/(mode+'-outbox.db')).enqueue(mode,{
            'session_id':kwargs['session_id'],'turn_id':mode,'contact_id':'fixture-person',
            'user_message':kwargs['user_message'],'transport_media':retained},capture_ordinary=True)
        assert receipt['state']=='pending'
        carrier.finish(**kwargs)
        seen.append('durable')
    registrations=[context.register_hook('pre_gateway_dispatch',carrier.observe),
        context.register_hook('pre_llm_call',pre),context.register_hook('post_llm_call',post),
        context.register_hook('on_session_end',carrier.finish)]
    tokens=set_session_vars(platform='whatsapp',source='whatsapp',chat_id='chat',user_id='sender',session_id='session')
    key=current_profile_key()
    turn=SESSION_COORDINATOR.begin_turn(ConversationLease(key,'session','whatsapp',
        NoopRelayRuntime(profile_key=key,reason='controlled local lifecycle'),None),turn_id='turn',task_id='task')
    try:
        lifecycle.invoke_hook('pre_gateway_dispatch',event=event)
        _collect_pre_llm_call_context(agent,effective_task_id='task',turn_id='turn',original_user_message=content,
                                     messages=[native],conversation_history=[])
        assert seen and seen[0]['conversation_history'][-1]['platform_message_id']==event.message_id
        retained=carrier.for_turn(scope)
        assert retained['provider_message_id']==event.message_id and retained['caption']==event.text
        assert carrier._bound[('session','task','turn')][1]() is turn
        selected=retained['images'][0]
        value=(content[selected['native_block_index']]['image_url']['url'] if 'native_block_index' in selected else selected['data_url'])
        assert base64.b64decode(value.split(',',1)[1])==out.getvalue()
        _apply_output_hooks(agent,'Retained.',logging.getLogger('fixture'),platform='whatsapp',
            effective_task_id='task',turn_id='turn',original_user_message=content,messages=[native])
        assert seen[-1]=='durable' and carrier.for_turn(scope) is None
        lifecycle.invoke_hook('on_session_end',session_id='session',task_id='task',turn_id='turn',outcome='completed')
    finally:
        SESSION_COORDINATOR.end_turn(turn,outcome='completed')
        clear_session_vars(tokens)
        for registration in registrations:registration.dispose()
# A timed-out pre hook can still finish in its inherited native context after
# the caller ended the turn. That closed context must not create a new carrier.
from contextvars import copy_context
carrier=TransportMedia()
turn=SESSION_COORDINATOR.begin_turn(ConversationLease(key,'session','whatsapp',
    NoopRelayRuntime(profile_key=key,reason='controlled abandoned hook'),None),turn_id='turn',task_id='task')
tokens=set_session_vars(platform='whatsapp',source='whatsapp',chat_id='chat',user_id='sender',session_id='session')
late_context=copy_context()
clear_session_vars(tokens)
SESSION_COORDINATOR.end_turn(turn,outcome='interrupted')
carrier.observe(event=event)
late_context.run(carrier.bind,scope,{'user_message':content,'conversation_history':[native]})
assert carrier.for_turn(scope) is None
print(json.dumps({'actual_native_hook':True,'actual_bounded_dispatch_and_finalizer':True,
    'text_and_native_originals':True,'model_calls':0,'external_delivery':False}))
'''


def test_selected_native_hook_retains_transport_originals(artifacts, tmp_path):
    native = os.environ.get('PROTAGINE_TEST_HERMES_PATH', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for actual native hook qualification')
    _, _, _, installed = artifacts
    result = run_python('-I', '-c', PROBE, installed, native, cwd=tmp_path, env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['actual_native_hook'] is True
