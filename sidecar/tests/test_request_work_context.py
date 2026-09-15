"""Bounded operational context, preserving participant and request semantics."""
import copy
import importlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from pacomind.turns.executions import request_work_context
from test_hermes_turn_outbox import _load_plugin


@pytest.fixture
def module():
    plugin = _load_plugin('pacomind_request_work_test')
    return importlib.import_module(plugin.__name__ + '.request_work')


def scope(**changes):
    return SimpleNamespace(valid_participant=True, authority_lane='owner',
        resolution_status='resolved', platform='sms', contact_id='owner',
        session_id='session-a', **changes)


def response(text='A neutral task is running.'):
    return httpx.Response(200, request=httpx.Request('GET', 'http://localhost/v1/host/executions'),
        json={'schema': 'PacoMindRequestWorkV1', 'observed_at': 1234.5,
              'text': text, 'truncated': False})


def test_task_revision_requires_current_owner_and_never_falls_back(module, tmp_path):
    controller_module = importlib.import_module(module.__package__ + '.task_controller')
    revoked = set()
    sources = SimpleNamespace(resolve_source=lambda value, dependencies=None: value,
        resolve_owner=lambda value, **kwargs: None if value['principal'] in revoked else value['contact_id'],
        actor_contact=lambda actor: actor.contact_id)
    controller = controller_module.NativeTasks(SimpleNamespace(), SimpleNamespace(path=tmp_path/'outbox', erasure_watermark=lambda *a,**k:0),
        'owner', sources=sources)
    def source(name):
        return {'version':1, 'principal':name, 'source_session_id':name, 'contact_id':'owner',
            'watermark':0, 'source_refs':[{'source_id':name,'source_version':'b'*64}],
            'input_refs':[{'source_id':name,'input_message_hash':'c'*64}]}
    task = controller.handoffs.admit(request_id='task', request='Original task', source_input=source('original'))
    controller.handoffs.admit_update(task['id'], instruction='Earlier revision',
        source_input=source('earlier'), principal='earlier')
    latest = controller.handoffs.admit_update(task['id'], instruction='Current revision',
        source_input=source('latest'), principal='latest')
    read = lambda actor: controller.request_revision(actor, [task['id']], deadline_monotonic=time.monotonic()+1)
    assert read(SimpleNamespace(contact_id='guest')) is None
    assert read(scope())['instruction'] == 'Current revision'
    revoked.add('latest')
    assert read(scope()) is None
    revoked.clear()
    with controller.database() as db:
        db.execute("UPDATE native_voice_updates SET instruction='' WHERE id=?", (latest['id'],))
    assert read(scope()) is None


def test_missing_voice_revision_pins_use_existing_exact_input_resolver(module, tmp_path):
    controller_module = importlib.import_module(module.__package__ + '.task_controller')
    calls = []
    refs = [{'source_id':name, 'source_version':'b'*64} for name in ('original','revision')]
    def post(path, **kwargs):
        calls.append((path, kwargs))
        return httpx.Response(200, request=httpx.Request('POST','http://localhost'+path),
            json={'complete':True, 'sources_current':False, 'input_source_refs':refs})
    outbox = SimpleNamespace(path=tmp_path/'outbox', erasure_state=lambda *a,**k:(0,[]), erasure_watermark=lambda *a,**k:0)
    sources = SimpleNamespace(resolve_source=lambda value, dependencies=None: value,
        resolve_owner=lambda value, **kwargs:value['contact_id'], actor_contact=lambda actor:actor.contact_id)
    controller = controller_module.NativeTasks(SimpleNamespace(post=post), outbox, 'owner', sources=sources)
    def source(name):
        return {'version':1,'principal':name,'source_session_id':name,'contact_id':'owner',
            'watermark':0,'source_refs':[],
            'input_refs':[{'source_id':name,'input_message_hash':'c'*64}]}
    task=controller.handoffs.admit(request_id='voice-task',request='Original task',source_input=source('original'))
    controller.handoffs.admit_update(task['id'],instruction='Voice correction',
        source_input=source('revision'),principal='revision')
    found=controller.request_revision(scope(),[task['id']],deadline_monotonic=time.monotonic()+1)
    assert found['source_refs']==refs
    assert len(calls)==1 and calls[0][0]=='/v1/host/memory/sources/erasures'
    assert calls[0][1]['json']['source_refs']==[]
    assert {r['source_id'] for r in calls[0][1]['json']['unannotated_input_refs']}=={'original','revision'}
    refs.pop()
    assert controller.request_revision(scope(),[task['id']],deadline_monotonic=time.monotonic()+1) is None


def test_no_revision_keeps_full_context_and_text_cannot_nominate_task(module):
    calls=[]
    native=SimpleNamespace(request_revision=lambda *a,**k:calls.append(a) or None)
    full=response('Original task purpose plus other current work.')
    refresh=module.RequestWork(SimpleNamespace(get=lambda *a,**k:full),native)
    request={'messages':[{'role':'user','content':'Inspect task '+('a'*64)}]}
    actual,_,_=refresh.prepare(request,scope())
    assert not calls
    assert 'Original task purpose plus other current work.' in json.dumps(actual)
    value=full.json();value['native_task_ids']=['a'*64]
    value['reserved']={'text':'Shorter context'}
    full=httpx.Response(200,request=full.request,json=value)
    actual,_,_=refresh.prepare(request,scope())
    assert len(calls)==1 and 'Shorter context' not in json.dumps(actual)


def test_optional_owner_lookups_share_deadline_and_restore_normal_control(module):
    sources_module = importlib.import_module(module.__package__ + '.task_sources')
    calls = []
    def get(path, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, request=httpx.Request('GET', 'http://localhost'+path),
                              json={'contact_id':'owner'})
    sources = sources_module.NativeTaskSources(SimpleNamespace(get=get), None, 'owner')
    origin = {'platform':'sms', 'authority_gateway':'sms', 'sender_id':'neutral-owner'}
    deadline = time.monotonic() + .05
    with sources_module.owner_lookup_deadline(deadline):
        assert sources._owner(origin) == 'owner'
        assert calls[-1]['_deadline_monotonic'] == deadline
        assert 0 < calls[-1]['timeout'] <= .05
    with sources_module.owner_lookup_deadline(time.monotonic()-1):
        with pytest.raises(sources_module.TaskHandoffError):
            sources._owner(origin)
    assert len(calls) == 1
    assert sources._owner(origin) == 'owner'
    assert calls[-1]['timeout'] > .4


@pytest.mark.parametrize('terminal', [False, True])
def test_crowded_current_task_preserves_purpose_and_revision_within_existing_budget(module, terminal):
    from test_work_ancestry_projection import concurrent_view, execution
    task = execution(2, age=8)
    task.update(task_id='a'*64, platform='pacomind_task')
    original = {'source_id':'original-task', 'source_version':'b'*64}
    original_input = {'source_id':'original-task', 'input_message_hash':'c'*64}
    purpose = ('Repair the input-validation defect without changing valid output. ' * 5)[:240]
    task['request_input'] = {'status':'admitted_input_excerpt', **original,
        'excerpt':purpose, 'partial':True, 'input_count':1,
        'input_message_hash':'c'*64, '_provenance':{'contact_id':'owner','watermark':3,
            'source_refs':[original], 'unannotated_input_refs':[original_input]}}
    view = concurrent_view()
    view['items'] = [execution(1, session='observer'), task, *view['items'][2:]]
    previous = execution(40, age=20)
    previous.update(task_id='d'*64, platform='pacomind_task', phase='ended', state='completed')
    view['recent'] = [previous]
    if terminal:
        view['items'].remove(task)
        task.update(phase='ended', state='completed')
        # A settled registry row has no fresh original-input excerpt.
        task.pop('request_input')
        view['recent'] = [task]
    full = request_work_context(view, session_id='observer')
    full['reserved'] = request_work_context(view, session_id='observer', max_chars=3360, limit=7)
    revision = {'task_id':'a'*64, 'instruction':'Identify the offending task_id in duration errors.',
        'update':{'update_id':'e'*64, 'accepted':True, 'native_control_acknowledged':True,
            'middleware_visible':False, 'native_request_visible':False,
            'provider_delivery':'unobserved','behavior_applied':'unobserved'},
        'contact_id':'owner','watermark':3,'source_refs':[original,
            {'source_id':'revision','source_version':'f'*64}],
        'unannotated_input_refs':[original_input,
            {'source_id':'revision','input_message_hash':'0'*64}]}
    refresh = module.RequestWork(None, SimpleNamespace(request_revision=lambda *a,**k:revision))
    result = refresh._revision(full, scope(), time.monotonic()+1)
    assert revision['instruction'] in result['text']
    assert len(result['text']) <= 4000
    assert len([line for line in result['text'].splitlines()
                if line.startswith('{') and '"source": "native_kanban_coverage"' not in line]) <= 8
    assert result['input_provenance']['source_refs'] == revision['source_refs']
    assert result['input_provenance']['unannotated_input_refs'] == revision['unannotated_input_refs']
    assert result['input_provenance']['watermark'] == 3
    if not terminal:
        assert len(full['text']) + 640 > 4000
        assert purpose in result['text']
        # The already-qualified small-budget locator remains available.
        compact = request_work_context({'items':[task]}, session_id='observer', max_chars=1800)
        assert 'a'*64 in compact['native_task_ids'] and len(compact['text']) <= 1800


@pytest.mark.parametrize('payload', [
    {'messages': [{'role': 'system', 'content': 'Stable identity'},
                  {'role': 'user', 'content': 'Continue my task.'},
                  {'role': 'tool', 'tool_call_id': 'read-1', 'content': 'Observed file bytes'}]},
    {'instructions': 'Stable identity', 'input': [
        {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Continue.'}]},
        {'type': 'function_call_output', 'call_id': 'read-1', 'output': 'Observed file bytes'}]},
    {'system': [{'type': 'text', 'text': 'Stable identity', 'cache_control': {'type': 'ephemeral'}}],
     'messages': [{'role': 'user', 'content': 'Continue.'}]},
])
def test_fresh_context_replaces_only_our_block_and_preserves_input(module, payload):
    before = copy.deepcopy(payload)
    calls = []
    def get(path, **kwargs):
        calls.append((path, kwargs))
        return response('Task running.' if len(calls) == 1 else 'Task completed.')
    refresh = module.RequestWork(SimpleNamespace(get=get))
    first = refresh(payload, scope())
    second = refresh(first, scope())
    wire = json.dumps(second)
    assert wire.count('[pacomind-work-request-v1]') == 1
    assert 'Task completed.' in wire and 'Task running.' not in wire
    assert module.replace_context(second) == before
    assert payload == before
    assert all(path == '/v1/host/executions' for path, _ in calls)
    assert calls[1][1]['params'] == {'contact_id': 'owner', 'session_id': 'session-a',
                                    'limit': 8, 'projection': 'request', 'input_context': True}
    assert 0 < calls[1][1]['timeout'] <= .25


@pytest.mark.parametrize('lane,platform,status', [
    ('guest', 'sms', 'resolved'), ('unresolved', 'sms', 'missing'),
    ('owner', 'background_review', 'resolved'),
    ('system', 'cron', 'attested_system'), ('system', 'cli', 'resolved'),
])
def test_non_owner_context_cannot_reuse_prior_operational_block(module, lane, platform, status):
    def get(*args, **kwargs):
        pytest.fail('Ineligible scope fetched owner work')
    literal = '[pacomind-work-request-v1]\nThis is literal user content.\n[/pacomind-work-request-v1]'
    original = {'messages': [{'role': 'user', 'content': literal}]}
    earlier = module.replace_context(original, 'Owner task metadata')
    participant = SimpleNamespace(valid_participant=True, authority_lane=lane,
        resolution_status=status, platform=platform)
    assert module.RequestWork(SimpleNamespace(get=get))(earlier, participant) == original


def test_explicit_local_owner_attestation_is_supported(module):
    participant = scope()
    participant.authority_lane = 'system'
    participant.platform = 'cli'
    participant.resolution_status = 'attested_system'
    result = module.RequestWork(SimpleNamespace(get=lambda *a, **k: response()))(
        {'messages': [{'role': 'user', 'content': 'Current work?'}]}, participant)
    assert 'A neutral task is running.' in json.dumps(result)


@pytest.mark.parametrize('available', [False, True])
@pytest.mark.parametrize('offline', [False, True])
@pytest.mark.parametrize('payload', [
    {'messages': [{'role': 'developer', 'content': 'Cached identity'},
                  {'role': 'user', 'content': 'Continue.'}]},
    {'instructions': 'Cached identity', 'input': [{'role': 'user', 'content': 'Continue.'}]},
    {'system': [{'type': 'text', 'text': 'Cached identity', 'cache_control': {'type': 'ephemeral'}}],
     'messages': [{'role': 'user', 'content': 'Continue.'}]},
])
def test_handoff_clarification_uses_existing_supported_request_block(module, monkeypatch, available, offline, payload):
    controller = importlib.import_module(module.__package__ + '.task_controller')
    monkeypatch.setattr(controller, 'FinishTurn', object if available else None)
    calls = []
    def get(path, **kwargs):
        calls.append(kwargs)
        if offline:
            raise httpx.ConnectError('offline')
        return response()
    original = copy.deepcopy(payload)
    refresh = module.RequestWork(SimpleNamespace(get=get), SimpleNamespace())
    result, provenance, current = refresh.prepare(payload, scope())
    assert current is not offline and provenance is None
    text = json.dumps(result)
    assert ("pacomind_task(operation='handoff')" in text) is available
    assert text.count('[pacomind-work-request-v1]') == 1
    assert module.replace_context(result) == original == payload
    assert calls[0]['params']['reserve_chars'] <= 1200


def test_handoff_guidance_shares_existing_work_budget(module, monkeypatch):
    controller = importlib.import_module(module.__package__ + '.task_controller')
    monkeypatch.setattr(controller, 'FinishTurn', object)
    def get(path, **kwargs):
        value = response('Large full projection. ' * 165).json()
        value['reserved'] = response('Compact current work.').json()
        return httpx.Response(200, request=httpx.Request('GET', 'http://localhost'), json=value)
    result, provenance, current = module.RequestWork(SimpleNamespace(get=get), SimpleNamespace()).prepare(
        {'messages':[{'role':'user','content':'Continue.'}]}, scope())
    assert current is True and provenance is None
    block = result['messages'][-1]['content']
    assert 'Compact current work.' in block and 'Large full projection.' not in block
    assert "pacomind_task(operation='handoff')" in block and len(block) <= 4200


@pytest.mark.parametrize('shape', ['chat', 'responses', 'anthropic'])
def test_superseded_work_removal_requires_observed_suffix_and_preserves_sources(module, shape):
    memory = importlib.import_module(module.__package__ + '.request_memory')
    fallback = '## Work observed at turn start [priority 73]\nOnly the old work snapshot.'
    packet = ('[pacomind-recall-v1 {"contact_id":"owner","watermark":0}]\n'
              'Persistent source evidence.\n\n' + fallback +
              '\n\n## Relevant Memories [priority 90]\nOriginal sourced fact.\n[/pacomind-recall-v1]')
    # A literal matching heading in actual user input must survive unchanged.
    direct = 'Discuss this literal heading: ' + fallback
    native_suffix = '\n\n<memory-context>\n' + packet + '\n</memory-context>'
    if shape == 'chat':
        content, enriched = direct, direct + native_suffix
    else:
        kind = 'input_text' if shape == 'responses' else 'text'
        content = [{'type': kind, 'text': direct}]
        enriched = content + [{'type': kind, 'text': native_suffix}]
    current = {'role': 'user', 'content': content, 'api_content': enriched}
    key = 'input' if shape == 'responses' else 'messages'
    request = {key: [{'role': 'user', 'content': enriched},
                     {'role': 'tool', 'content': packet, 'tool_call_id': 'source-read'}]}
    before = copy.deepcopy(request)
    result = memory._without_turn_start_work(request, current)
    user = result[key][0]['content']
    text = user if isinstance(user, str) else ''.join(part['text'] for part in user)
    assert text.count(fallback) == 1  # The literal user copy is preserved.
    assert 'Original sourced fact.' in text and '[pacomind-recall-v1' in text
    assert result[key][1] == before[key][1]
    assert request == before
    assert memory._without_turn_start_work(request, None) == before
    assert memory._without_turn_start_work(request, {'role':'user','content':enriched}) == before


def test_empty_native_anthropic_system_uses_top_level_slot(module):
    original = {'model': 'fixture/model', 'max_tokens': 100,
                'messages': [{'role': 'user', 'content': 'Continue.'}]}
    before = copy.deepcopy(original)
    refresh = module.RequestWork(SimpleNamespace(get=lambda *a, **k: response()))
    result = refresh(original, scope(), api_mode='anthropic_messages')
    assert result['messages'] == before['messages']
    assert result['system'].startswith('[pacomind-work-request-v1]\n')
    assert all(row['role'] != 'system' for row in result['messages'])
    assert original == before
    again = refresh(result, scope(), api_mode='anthropic_messages')
    assert json.dumps(again).count('[pacomind-work-request-v1]') == 1


def test_native_chat_developer_role_is_preserved_and_replaceable(module):
    original = {'messages': [{'role': 'developer', 'content': 'Stable identity'},
                             {'role': 'user', 'content': 'Continue.'}]}
    before = copy.deepcopy(original)
    refresh = module.RequestWork(SimpleNamespace(get=lambda *a, **k: response()))
    first = refresh(original, scope(), api_mode='chat_completions')
    second = refresh(first, scope(), api_mode='chat_completions')
    assert second['messages'][-1]['role'] == 'developer'
    assert all(row['role'] != 'system' for row in second['messages'])
    assert json.dumps(second).count('[pacomind-work-request-v1]') == 1
    assert module.replace_context(second) == before
    assert original == before


@pytest.mark.parametrize('failure', ['offline', 'late', 'oversized', 'wrong_schema'])
def test_no_previous_request_is_advertised_as_fresh_after_failure(module, monkeypatch, failure):
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    def get(*args, **kwargs):
        if failure == 'offline':
            raise httpx.ConnectError('offline')
        if failure == 'late':
            clock[0] = 1.0
        if failure == 'oversized':
            return response('x' * 4001)
        if failure == 'wrong_schema':
            return httpx.Response(200, request=httpx.Request('GET', 'http://localhost'), json={})
        return response()
    original = {'messages': [{'role': 'user', 'content': 'Continue.'}]}
    previous = module.replace_context(original, 'Old task was running.')
    result = module.RequestWork(SimpleNamespace(get=get))(previous, scope())
    text = json.dumps(result)
    assert 'Old task' not in text and 'unavailable' in text
    assert 'does not establish' in text
    assert module.replace_context(result) == original


def test_late_work_response_with_input_lineage_withholds_quote_and_provenance(module, monkeypatch):
    # Replace only this module's clock, not the process-wide time module.
    clock = [10.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    def get(path, **kwargs):
        calls.append(path)
        assert kwargs['timeout'] == .25
        assert kwargs['_deadline_monotonic'] == 10.25
        clock[0] = 10.251
        value = response('Use the lamp maintenance record I supplied.').json()
        value['input_provenance'] = {
            'contact_id': 'owner', 'watermark': 0,
            'source_refs': [{'source_id': 'original-input', 'source_version': 'a' * 64}],
            'unannotated_input_refs': [{'source_id': 'original-input', 'input_message_hash': 'b' * 64}],
        }
        return httpx.Response(200, request=httpx.Request('GET', 'http://localhost' + path), json=value)
    original = {'messages': [{'role': 'user', 'content': 'What are you doing?'}]}
    result, provenance, current = module.RequestWork(SimpleNamespace(get=get)).prepare(original, scope())
    assert current is False
    assert calls == ['/v1/host/executions']
    assert provenance is None
    assert 'Use the lamp maintenance record I supplied.' not in json.dumps(result)
    assert 'Current shared work is unavailable' in json.dumps(result)
    assert module.replace_context(result) == original


def test_projection_retains_operational_evidence_without_task_or_draft_prose():
    report_hash = 'a' * 64
    view = {'items': [], 'local_work': {'available': True, 'items': [], 'recent': [
        {'initiative_id': 'completed-1', 'status': 'completed', 'question': 'Private task prose',
         'result': {'draft': 'Private draft prose', 'report_path': '/private/retained/report',
                    'report_sha256': report_hash}},
        {'initiative_id': 'older', 'status': 'failed'}]},
        'native_cron': {'available': False, 'items': []},
        'reported_worker': {'available': True, 'items': [
            {'label': 'Download', 'state': 'running', 'freshness': 'recent', 'pid': 123,
             'liveness': 'unverified', 'age_seconds': 2.5, 'path': '/private/status'},
            {'label': 'Offline reporter', 'available': False, 'liveness': 'unverified'}]}}
    result = request_work_context(view)
    text = result['text']
    assert 'completed-1' in text and report_hash in text
    assert 'Download' in text and 'unverified' in text
    offline = next(json.loads(line) for line in text.splitlines()
                   if line.startswith('{') and 'Offline reporter' in line)
    assert offline['available'] is False
    assert 'Unavailable sources: native_cron' in text
    assert result['truncated'] is True and 'Additional operational records omitted' in text
    assert 'Private' not in text and '/private' not in text and '"pid"' not in text
    assert len(text) <= 4000


def test_projection_bounds_many_active_records_and_discloses_omissions():
    rows = [{'execution_id': str(i), 'phase': 'tool', 'tool_name': 'x' * 128} for i in range(30)]
    result = request_work_context({'items': rows, 'truncated': False})
    assert result['truncated'] is True and len(result['text']) <= 4000
    assert result['text'].count('"source": "execution"') == 8
