"""Selected Hermes stages actual provider identity into its native hook."""
import importlib.util
import json
import os

import pytest
from conftest import run_python
from test_native_current_work import environment


PROBE = r'''
import base64,io,json,os,socket,sys
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
from hermes_cli import lifecycle
from pacomind_hermes.transport_media import TransportMedia
scope=NS(valid_participant=True,platform='whatsapp',sender_id='sender',session_id='session',task_id='task',turn_id='turn')
agent=NS(session_id='session',platform='whatsapp',model='fixture',_user_id='sender',_persist_disabled=False)
for mode in ('text','native'):
    carrier=TransportMedia()
    event=MessageEvent(text='This is my reference drawing.',message_type=MessageType.PHOTO,
        source=NS(platform=NS(value='whatsapp'),user_id='sender',chat_id='chat'),message_id=mode+'-image',
        media_urls=[str(image)],media_types=['image/png'])
    carrier.observe(event=event)
    content=('Runtime vision interpretation. '+event.text if mode=='text'
             else build_native_content_parts(event.text,[str(image)])[0])
    native,_=_stage_turn_user_message(agent,content,None,None,event.message_id,None,None)
    seen=[]
    def invoke(name,**kwargs):
        assert name=='pre_llm_call'
        seen.append(kwargs)
        carrier.bind(scope,kwargs)
        return []
    lifecycle.invoke_hook=invoke
    tokens=set_session_vars(platform='whatsapp',source='whatsapp',chat_id='chat',user_id='sender',session_id='session')
    try:
        _collect_pre_llm_call_context(agent,effective_task_id='task',turn_id='turn',original_user_message=content,
                                     messages=[native],conversation_history=[])
    finally:clear_session_vars(tokens)
    assert seen and seen[0]['conversation_history'][-1]['platform_message_id']==event.message_id
    retained=carrier.for_turn(scope)
    assert retained['provider_message_id']==event.message_id and retained['caption']==event.text
    selected=retained['images'][0]
    value=(content[selected['native_block_index']]['image_url']['url'] if 'native_block_index' in selected else selected['data_url'])
    assert base64.b64decode(value.split(',',1)[1])==out.getvalue()
print(json.dumps({'actual_native_hook':True,'text_and_native_originals':True,'model_calls':0,'external_delivery':False}))
'''


def test_selected_native_hook_retains_transport_originals(artifacts, tmp_path):
    native = os.environ.get('PACOMIND_TEST_HERMES_PATH', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for actual native hook qualification')
    _, _, _, installed = artifacts
    result = run_python('-I', '-c', PROBE, installed, native, cwd=tmp_path, env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['actual_native_hook'] is True
