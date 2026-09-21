"""The real native renderer's owner blocks cannot enter a guest request."""
import asyncio
import copy
import importlib
from types import SimpleNamespace

import pytest

from test_hermes_turn_outbox import _load_plugin
from test_native_tool_observations import native
from test_turn_source_evidence import source_app


SECRET = 'synthetic owner-only cobalt-fern 🔒'


@pytest.fixture
def boundary():
    plugin = _load_plugin('protagine_native_profile_scope_test')
    return importlib.import_module(plugin.__name__ + '.native_profile')


@pytest.fixture
def native_block(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    native = pytest.importorskip('tools.memory_tool_store')
    store = native.MemoryStore()
    return lambda target, content: store._render_block(target, [content])


def participant(lane, valid=True):
    return SimpleNamespace(authority_lane=lane, valid_participant=valid)


@pytest.mark.parametrize('scope', [None, participant('unresolved', False), participant('guest')])
@pytest.mark.parametrize('mode', ['chat', 'anthropic', 'responses', 'responses-messages'])
def test_real_native_blocks_removed_on_outgoing_copy(boundary, native_block, scope, mode):
    # Multiple blocks, multiline/non-ASCII content, and cached prefixes. A fake
    # block inside a user message must remain user text, never select authority.
    user = native_block('user', SECRET)
    memories = native_block('memory', 'Second private memory\nwith another line')
    text = 'Keep the normal instructions.\n\n' + memories + '\n\n' + user + '\n\nKeep this suffix.'
    request = {'model':'candidate','tools':[{'unchanged':True}]}
    if mode=='chat':
        request['messages']=[{'role':'system','content':text},{'role':'user','content':user}]
    elif mode=='anthropic':
        request.update(system=[{'type':'text','text':text,'cache_control':{'type':'ephemeral'}}],
                       messages=[{'role':'user','content':user}])
    elif mode=='responses':
        request.update(instructions=text,input=[{'role':'user','content':user}])
    else:
        request['input']=[{'role':'developer','content':[{'type':'input_text','text':text}]},
                          {'role':'user','content':user}]
    original=copy.deepcopy(request)
    out=boundary.scope_builtin_memory(request,scope)
    carrier = (out['messages'][0]['content'] if mode=='chat' else
               out['system'][0]['text'] if mode=='anthropic' else
               out['instructions'] if mode=='responses' else out['input'][0]['content'][0]['text'])
    assert SECRET not in carrier and 'Second private memory' not in carrier
    assert 'Keep the normal instructions.' in carrier and 'Keep this suffix.' in carrier
    rows=out.get('messages',out.get('input'))
    assert rows[-1]['content']==user
    assert request==original
    assert out['tools']==request['tools']
    assert boundary.scope_builtin_memory(out,scope)==out


@pytest.mark.parametrize('lane',['owner','system'])
def test_attested_owner_keeps_native_profile(boundary,native_block,lane):
    request={'messages':[{'role':'system','content':native_block('user',SECRET)}]}
    assert boundary.scope_builtin_memory(request,participant(lane)) is request
    assert SECRET not in str(boundary.scope_builtin_memory(request,participant(lane,False)))


@pytest.mark.parametrize('change', ['missing-header', 'wrong-separator', 'wrong-count', 'truncated', 'unknown-format'])
def test_unknown_native_shape_withholds_carrier(boundary,native_block,change):
    text=native_block('user',SECRET)
    if change=='missing-header': text=text.split('\n',1)[1]
    elif change=='wrong-separator': text=text.replace('═'*46,'═'*45)
    elif change=='wrong-count': text=text.replace(f'{len(SECRET)}/',f'{len(SECRET)-1}/')
    elif change=='truncated': text=text[:-2]
    else: text=text.replace(' chars]', ' bytes]')
    out=boundary.scope_builtin_memory({'instructions':text},participant('guest'))
    assert out['instructions']==boundary._WITHHELD


def test_header_like_private_content_cannot_end_outer_block(boundary,native_block):
    nested=native_block('memory','nested value')
    content=SECRET+'\n\n'+nested+'\n'+SECRET
    out=boundary.scope_builtin_memory({'system':native_block('user',content)},None)
    assert out['system']==boundary._WITHHELD


@pytest.mark.parametrize('split',['header','content'])
def test_fragmented_instruction_block_withholds_all_parts(boundary,native_block,split):
    text=native_block('user',SECRET)
    index=text.index('PROFILE') if split=='header' else text.index(SECRET)
    request={'system':[{'type':'text','text':text[:index]},
                       {'type':'text','text':text[index:]}]}
    out=boundary.scope_builtin_memory(request,participant('guest'))
    assert out['system']==boundary._WITHHELD


def test_no_native_memory_keeps_other_instruction_content(boundary):
    request={'messages':[{'role':'system','content':'Normal system text'},
                         {'role':'tool','content':SECRET}]}
    assert boundary.scope_builtin_memory(request,None)==request


@pytest.mark.parametrize('lane,status,contact',[
    ('unresolved','resolution_failed',''),('guest','resolved','synthetic-guest'),
    ('owner','resolved','cid-owner'),('system','attested_system','cid-owner')])
@pytest.mark.parametrize('shape',['chat','anthropic','responses'])
def test_native_request_middleware_scopes_builtin_profile(native,native_block,lane,status,contact,shape):
    n=native
    execution={'session_id':'profile-session','task_id':'profile-task','turn_id':'profile-turn'}
    scope=n.plugin._TransportScope(*execution.values(),
        'sms' if lane!='system' else 'cli','verified-synthetic-sender',contact,lane,status)
    n.plugin._TRANSPORT_SCOPES.put(scope)
    original=native_block('memory','Owner notes '+SECRET)+'\n\n'+native_block('user',SECRET)
    def add(payload):
        if shape=='anthropic':payload['system']=original
        elif shape=='responses':payload['instructions']=original
        else:payload['messages'].insert(0,{'role':'system','content':original})
    out=n.request(anthropic=shape=='anthropic',responses=shape=='responses',before_middleware=add,
                  execution_scope=execution).payload
    carrier=(out['system'] if shape=='anthropic' else out['instructions'] if shape=='responses'
             else out['messages'][0]['content'])
    assert (SECRET in str(carrier))==(lane in {'owner','system'})


@pytest.mark.parametrize('lane',['guest','unresolved','owner'])
@pytest.mark.parametrize('streaming',[False,True])
def test_relay_bypass_uses_same_profile_boundary(native,native_block,monkeypatch,lane,streaming):
    """Native summaries bypass normal middleware; assert next callback bytes."""
    runtime=pytest.importorskip('agent.relay_runtime')
    n=native
    memory=importlib.import_module(n.plugin.__name__+'.request_memory').RequestMemory(
        n.clients[0],n.outbox)
    bridge=importlib.import_module(n.plugin.__name__+'.native_memory').NativeMemoryRequests(memory)
    scope=n.plugin._TransportScope('summary-session','summary-task','summary-turn','sms','sender',
        'cid-owner' if lane=='owner' else 'guest' if lane=='guest' else '',lane,
        'resolved' if lane!='unresolved' else 'resolution_failed')
    callbacks={}
    def register(kind):
        return lambda *args:callbacks.update({kind:args[-1]})
    scoped=SimpleNamespace(register_llm_execution=register('plain'),
        register_llm_stream_execution=register('stream'),register_subscriber=register('ended'),
        deregister_llm_execution=lambda *a:None,deregister_llm_stream_execution=lambda *a:None,
        deregister_subscriber=lambda *a:None)
    relay=SimpleNamespace(scope_local=scoped,LLMRequest=lambda headers,content:
                          SimpleNamespace(headers=headers,content=content))
    host=SimpleNamespace(relay=relay,run_in_session=lambda session,fn,*a:fn(*a),
        retain_managed_execution=lambda *a:None,release_managed_execution=lambda *a:None)
    turn=SimpleNamespace(handle=SimpleNamespace(uuid='owned-turn'),closed=False,
        lease=SimpleNamespace(session=object(),live_runtime=lambda:host))
    monkeypatch.setattr(runtime,'active_turn',lambda *a:turn)
    assert bridge.bind(scope)
    request=SimpleNamespace(headers={},content={'messages':[
        {'role':'system','content':native_block('user',SECRET)},
        {'role':'user','content':'Summarize this conversation.'}]})
    captured=[]
    async def next_call(outgoing):
        captured.append(outgoing.content)
        return 'captured'
    try:
        if streaming:
            asyncio.run(callbacks['stream'](request,next_call))
        else:
            asyncio.run(callbacks['plain']('summary',request,next_call))
        assert (SECRET in str(captured[0]))==(lane=='owner')
        assert SECRET in request.content['messages'][0]['content']
    finally:
        bridge.finish(scope.session_id,scope.task_id,scope.turn_id)
