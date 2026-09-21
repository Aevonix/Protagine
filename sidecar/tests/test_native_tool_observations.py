"""Ordinary native tool evidence crosses the existing outbox/API/recall boundary."""
import copy
import hashlib
import importlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from protagine.api.middleware import ApiKeyMiddleware
from protagine.turns import TurnIdempotencyLedger
from test_hermes_turn_outbox import _load_plugin, _Context
from test_scoped_api_authority import _principal, _write_keyring
from test_turn_source_evidence import source_app

RESULT = '{"operation":"copper synchronization","exit_code":3,"files_written":0}'
INSTRUCTION = 'Inspect the copper synchronization fixture and retain useful findings.'


def observation_hints(request):
    """Read the framed hint inside its possibly compacted instruction carrier."""
    opening = '[protagine-observation-candidates-v1]'
    closing = '[/protagine-observation-candidates-v1]'
    pattern = re.compile(r'^' + re.escape(opening) + r'\n.*?^' + re.escape(closing)
                         + r'(?=\n|$)', re.MULTILINE | re.DOTALL)
    hints = []
    for row in request['messages']:
        if row.get('role') not in {'system', 'developer'} or not isinstance(row.get('content'), str):
            continue
        content = row['content']
        matches = pattern.findall(content)
        assert len(matches) == content.count(opening) == content.count(closing)
        hints.extend(matches)
    return hints


@pytest.fixture
def native(source_app, monkeypatch, tmp_path, request):
    hermes_state = pytest.importorskip('hermes_state', reason='Native qualification requires Hermes on PYTHONPATH')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'native'))
    for name, value in {'PROTAGINE_GENERAL_PLUGIN_ACTIVE':'1', 'PROTAGINE_MEMORY_WORKER_TOOLS':'0',
                        'PROTAGINE_MEMORY_TURN_WRITER':'disabled', 'PROTAGINE_OWNER_CONTACT_ID':'cid-owner',
                        'PROTAGINE_RECALL_RERANK':'off'}.items():
        monkeypatch.setenv(name, value)
    dbpath = tmp_path/'native'/'state.db'
    dbpath.parent.mkdir()
    monkeypatch.setattr(hermes_state, '_default_db_path', lambda: dbpath)
    db = hermes_state.SessionDB(dbpath)
    fixture_options = getattr(request, 'param', 'cli')
    db.create_session('native-session', fixture_options if isinstance(fixture_options, str) else 'cli')
    db.append_message('native-session', 'user', INSTRUCTION)
    keyring = tmp_path/'keys.json'
    _write_keyring(keyring, [_principal(principal='host', secret='writer', viewer='cid-owner'),
        _principal(principal='other', secret='other', viewer='other'),
        _principal(principal='reader', secret='reader', viewer='cid-owner', scopes=['context:read'])])
    source_app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    # The serving process initializes its store before requests. Keep fixture
    # schema/ASGI startup outside the unchanged native freshness deadline.
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    with TestClient(source_app, headers={'Authorization':'Bearer writer'}) as http:
        plugin = _load_plugin('protagine_original_tool_observation_test')
        client_module = importlib.import_module(plugin.__name__ + '.client')
        # These native/ASGI tests exercise source semantics, not scheduler or
        # filesystem latency. Only a drain and its nested delivery share this
        # controlled clock; request freshness and separate deadline tests keep
        # their real clocks. Explicit timeout tests advance it at the I/O boundary.
        delivery_clock = SimpleNamespace(now=None, wall=None)
        monkeypatch.setattr(client_module, 'time', SimpleNamespace(
            monotonic=lambda: time.monotonic() if delivery_clock.now is None else delivery_clock.now,
            time=lambda: time.time() if delivery_clock.wall is None else delivery_clock.wall,
            sleep=time.sleep))
        original_drain = plugin.TurnOutbox.drain
        def drain(outbox, *args, **kwargs):
            delivery_clock.now = time.monotonic()
            try:
                return original_drain(outbox, *args, **kwargs)
            finally:
                delivery_clock.now = None
        monkeypatch.setattr(plugin.TurnOutbox, 'drain', drain)
        http_events = []
        class Client(plugin.ProtagineClient):
            outage = False
            erasure_unavailable = False
            def _call(self, method, path, kwargs):
                began = time.monotonic()
                event = {'method': method, 'path': path}
                if kwargs.get('_deadline_monotonic') is not None:
                    event['remaining_budget_ms'] = round((kwargs['_deadline_monotonic']
                        - client_module.time.monotonic()) * 1000, 3)
                try:
                    if self.outage and '/source-observation/' in path:
                        response = httpx.Response(503, request=httpx.Request(method, 'http://fixture'+path))
                    elif self.erasure_unavailable and path == '/v1/host/memory/sources/erasures':
                        response = httpx.Response(403, request=httpx.Request(method, 'http://fixture'+path))
                    else:
                        response = http.request(method, path,
                            **{k:v for k,v in kwargs.items() if k in {'json','params'}})
                    event['status'] = response.status_code
                    return response
                except Exception as exc:
                    event['error'] = type(exc).__name__
                    raise
                finally:
                    event['elapsed_ms'] = round((time.monotonic() - began) * 1000, 3)
                    http_events.append(event)
            def get(self, path, **kwargs): return self._call('GET', path, kwargs)
            def post(self, path, **kwargs): return self._call('POST', path, kwargs)
            def put(self, path, **kwargs): return self._call('PUT', path, kwargs)
        clients = []
        def make_client(**kwargs):
            value = Client(**kwargs)
            clients.append(value)
            return value
        monkeypatch.setattr(plugin, 'ProtagineClient', make_client)
        context = _Context(tmp_path/'outbox.db')
        context.config['plugins']['protagine'] = context.config['plugins'].pop('protagine')
        if isinstance(fixture_options, dict) and fixture_options.get('tasks'):
            context.config['plugins']['protagine']['native_tasks'] = {'enabled': True}
            context.platforms = {}
            context.register_platform = lambda **kwargs: context.platforms.update({kwargs['name']: kwargs})
        plugin.register(context)
        from hermes_cli import middleware as native_middleware, plugins as native_plugins
        manager = SimpleNamespace(_middleware={key:[value] for key,value in context.middleware.items()})
        monkeypatch.setattr(native_plugins, 'get_plugin_manager', lambda: manager)
        monkeypatch.setattr(native_plugins, 'has_middleware', lambda kind: kind in manager._middleware)
        monkeypatch.setattr(native_plugins, 'invoke_middleware',
            lambda kind, **kwargs: [callback(**kwargs) for callback in manager._middleware.get(kind, [])])
        call_context = {'session_id':'native-session', 'task_id':'native-task', 'turn_id':'native-turn'}
        context.hooks['pre_llm_call'](**call_context, platform='cli', sender_id='owner', user_message=INSTRUCTION,
            conversation_history=[{'role':'user','content':INSTRUCTION}])
        messages = [{'role':'user','content':INSTRUCTION}]
        def request(request_id='api-2', *, anthropic=False, responses=False, deferred=False, tools=True,
                    before_middleware=None, execution_scope=None, history=None):
            payload = {'messages': copy.deepcopy(messages if history is None else history), 'tools':[{'type':'function','function':
                context.tools['protagine_memory_retain_observation']['schema']}]}
            if deferred:
                from tools.tool_search import assemble_tool_defs, ToolSearchConfig
                payload['tools'] = assemble_tool_defs(payload['tools'], config=ToolSearchConfig.from_raw(
                    {'enabled': 'on', 'defer': ['protagine_memory_retain_observation']})).tool_defs
            if not tools:
                payload['tools'] = []
            if anthropic:
                from agent.anthropic_message_convert import convert_messages_to_anthropic, convert_tools_to_anthropic
                system, payload['messages'] = convert_messages_to_anthropic(payload['messages'])
                if system is not None:
                    payload['system'] = system
                payload['tools'] = convert_tools_to_anthropic(payload['tools'])
            if responses:
                from agent.codex_responses_adapter import _chat_messages_to_responses_input, _responses_tools
                payload['input'] = _chat_messages_to_responses_input(payload.pop('messages'))
                payload['tools'] = _responses_tools(payload['tools'])
                payload['instructions'] = 'Stable identity.'
            if before_middleware is not None:
                before_middleware(payload)
            return native_middleware.apply_llm_request_middleware(payload,
                **(call_context if execution_scope is None else execution_scope), api_request_id=request_id,
                api_mode='anthropic_messages' if anthropic else 'chat_completions')
        request('api-1')
        def complete(call_id='call-1', result=RESULT, name='fixture_observe', arguments=None):
            arguments = {} if arguments is None else copy.deepcopy(arguments)
            call = {'id':call_id,'type':'function','function':{'name':name,'arguments':json.dumps(arguments)}}
            messages.append({'role':'assistant','content':None,'tool_calls':[call]})
            db.append_message('native-session','assistant',tool_calls=[call])
            value = native_middleware.run_tool_execution_middleware(**call_context, api_request_id='api-1',
                tool_name=name, tool_call_id=call_id, args=arguments, next_call=lambda args: subprocess.run(
                    [sys.executable, '-c', 'import sys; sys.stdout.write(sys.argv[1])', result],
                    check=True, capture_output=True, text=True, timeout=5).stdout)
            assert value == result
            message_id = db.append_message('native-session','tool',result,tool_name=name,tool_call_id=call_id,
                                           timestamp=1789149600.0)
            messages.append({'role':'tool','tool_call_id':call_id,'content':value})
            return message_id
        def retain(call_id='call-1', request_id='api-2', reason='Use the recorded synchronization outcome later.',
                   include_input=None):
            args = {'call_id':call_id, 'reason':reason}
            if include_input is not None:
                args['include_input'] = include_input
            return json.loads(native_middleware.run_tool_execution_middleware(**call_context, api_request_id=request_id,
                tool_name='protagine_memory_retain_observation', tool_call_id='retention-call', args=args,
                next_call=lambda selected: context.tools['protagine_memory_retain_observation']['handler'](selected)))
        def recall(contact='cid-owner'):
            response = http.post('/v1/host/context/assemble', json={'identity':{'host_id':'test'},
                'context':{'contact_id':contact,'session_id':'other-channel-session'},
                'incoming_message':{'role':'user','content':'copper synchronization outcome'}})
            assert response.status_code == 200, response.text
            return next((s for s in response.json()['sections'] if s['id']=='protagine-memory'), {})
        outbox = plugin.TurnOutbox(tmp_path/'outbox.db')
        def diagnostics(receipt):
            # Assertion messages read this lazily; no headers, source payloads or retries.
            try:
                rows = [{key: row[key] for key in ('turn_id', 'state', 'attempts', 'last_error')}
                        for row in outbox.snapshot()]
            except Exception as exc:
                rows = {'snapshot_error': type(exc).__name__}
            return json.dumps({'receipt': receipt, 'http': http_events[-16:], 'outbox': rows}, indent=2)
        yield SimpleNamespace(plugin=plugin, context=context, db=db, http=http, clients=clients,
            complete=complete, request=request, retain=retain, ledger=ledger, recall=recall,
            messages=messages, scope=call_context, outbox=outbox, diagnostics=diagnostics,
            delivery_clock=delivery_clock)
        db.close()


def test_actual_native_original_roundtrips_into_automatic_recall(native):
    n = native
    assert 'protagine_memory_retain_observation' in n.context.tools
    message_id = n.complete()
    assert not n.retain()['accepted']  # Completion alone is not current-request exposure.
    n.request()
    result = n.retain()
    assert result['accepted'] and result['source_recorded'], n.diagnostics(result)
    packet = n.recall()
    assert 'copper synchronization' in packet['body'] and '"role": "tool"' in packet['body']
    with sqlite3.connect(n.ledger.db_path) as db:
        message = json.loads(db.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',
                                      (result['source_id'],)).fetchone()[0])[0]
        assert message['content'] == RESULT and message['role'] == 'tool'
        assert message['provenance']['native']['message_id'] == message_id
        assert message['provenance']['native']['result_sha256'] == hashlib.sha256(RESULT.encode()).hexdigest()
        assert message['provenance']['native']['api_request_id'] == 'api-1'
        assert message['provenance']['selection']['author'] == 'model'
        assert db.execute('SELECT count(*) FROM source_claim_jobs').fetchone()[0] == 0
    assert 'Use the recorded synchronization outcome later.' not in packet['body']
    assert n.retain(reason='A different retry reason')['source_id'] == result['source_id']
    assert len([row for row in n.outbox.snapshot() if row['turn_id']==result['source_id']]) == 1


@pytest.mark.parametrize('name', ['tool_search', 'tool_describe'])
def test_tool_catalog_stays_in_history_without_becoming_a_memory(native, name):
    n = native
    catalog = json.dumps({'tools': {'fixture_observe': {
        'description': 'Inspect copper synchronization.',
        'parameters': {'type': 'object', 'properties': {}}}}})
    n.complete('catalog', catalog, name, {'name': 'fixture_observe'})
    request = n.request().payload
    hints = observation_hints(request)
    assert not any('"call_id": "catalog"' in text for text in hints)
    assert any(row.get('tool_call_id') == 'catalog' and row.get('content') == catalog
               for row in request['messages'])
    nomination = n.retain('catalog', reason='Reuse this tool description next time.')
    assert not nomination['accepted'] and not nomination['source_recorded'], nomination
    with sqlite3.connect(n.ledger.db_path) as db:
        assert db.execute("SELECT count(*) FROM turn_sources WHERE turn_id LIKE 'native-observation:%'").fetchone()[0] == 0

    # Discovery remains usable; a subsequent substantive result still crosses
    # the actual native/outbox/API boundary and is recalled in another session.
    n.complete('finding', RESULT, 'fixture_observe', {'target': 'copper'})
    n.request('api-3')
    saved = n.retain('finding', request_id='api-3')
    assert saved['accepted'] and saved['source_recorded'], n.diagnostics(saved)
    assert 'copper synchronization' in n.recall()['body']


@pytest.mark.parametrize('name', ['protagine_memory_search', 'protagine_memory_read_source',
    'protagine_get_facts', 'protagine_timeline', 'protagine_get_affect', 'protagine_check_commitments'])
@pytest.mark.parametrize('content', ['', 'Previously retained copper synchronization evidence.'])
def test_self_memory_reads_stay_in_history_without_recursive_retention(native, name, content):
    n = native
    result = json.dumps({'content': content, 'count': int(bool(content)), 'source_refs': []})
    n.complete('memory-read', result, name, {'query': 'copper synchronization'})
    request = n.request().payload
    assert not observation_hints(request)
    assert 'protagine_memory_retain_observation' not in str(request['tools'])
    assert any(row.get('tool_call_id') == 'memory-read' and row.get('content') == result
               for row in request['messages'])
    receipt = n.retain('memory-read')
    assert not receipt['accepted'] and not receipt['source_recorded'], receipt
    assert not n.outbox.snapshot()


@pytest.mark.parametrize('name,result', [
    ('file_search', '{"query":"copper synchronization","matches":[],"count":0}'),
    ('terminal', 'No matching records in the copper synchronization fixture.\n'),
])
def test_external_negative_findings_remain_eligible_and_retain_exact_result(native, name, result):
    n = native
    n.complete('negative-finding', result, name, {'target': 'copper synchronization'})
    hint, = observation_hints(n.request().payload)
    assert '"call_id": "negative-finding"' in hint
    receipt = n.retain('negative-finding', reason='Remember which fixture was checked and found empty.')
    assert receipt['accepted'] and receipt['source_recorded'], n.diagnostics(receipt)
    stored = n.outbox.lookup(receipt['source_id'])['payload']['observation']
    assert stored['content'] == result and stored['native']['tool_name'] == name


@pytest.mark.parametrize('deferred', [False, True])
def test_delivered_candidates_disappear_without_losing_other_findings_or_exact_retries(native, deferred):
    n = native
    n.complete('first', RESULT)
    n.complete('second', 'Independent useful finding.')
    n.request(deferred=deferred)
    saved = n.retain('first')
    assert saved['source_recorded'], n.diagnostics(saved)
    hint, = observation_hints(n.request('api-3', deferred=deferred).payload)
    assert '"call_id": "first"' not in hint and '"call_id": "second"' in hint
    retry = n.retain('first', request_id='api-3', reason='Changed retry reason.', include_input=True)
    assert retry['source_id'] == saved['source_id'] and retry['source_recorded']
    assert not retry['input_included']  # The first nomination remains immutable.
    second = n.retain('second', request_id='api-3')
    assert second['source_recorded'], n.diagnostics(second)
    request = n.request('api-4', deferred=deferred).payload
    assert not observation_hints(request)
    assert 'protagine_memory_retain_observation' not in str(request['tools'])
    assert len([row for row in n.outbox.snapshot() if row['turn_id'].startswith('native-observation:')]) == 2


@pytest.mark.parametrize('surface', ['search', 'describe'])
def test_discovery_stops_offering_retention_after_delivery_in_same_request(native, monkeypatch, surface):
    from hermes_cli import middleware
    from tools import tool_search
    n = native
    name = 'protagine_memory_retain_observation'
    schema = {'type': 'function', 'function': n.context.tools[name]['schema']}
    config = tool_search.ToolSearchConfig.from_raw({'enabled': 'on', 'defer': [name]})
    monkeypatch.setattr(tool_search, 'load_config_readonly', lambda: config)
    args = {'queries': [name]} if surface == 'search' else {'names': [name]}
    dispatch = tool_search.dispatch_tool_search if surface == 'search' else tool_search.dispatch_tool_describe
    def discover():
        return json.loads(middleware.run_tool_execution_middleware(**n.scope, api_request_id='api-2',
            tool_name='tool_' + surface, tool_call_id='discovery', args=args,
            next_call=lambda selected: dispatch(selected, current_tool_defs=[schema], config=config)))
    n.complete()
    n.request(deferred=True)
    assert name in discover()['tools']
    receipt = n.retain()
    assert receipt['source_recorded'], n.diagnostics(receipt)
    assert name not in discover()['tools']


def test_recipe_original_inputs_and_final_result_open_through_native_reader(native):
    from hermes_cli import middleware as native_middleware
    n = native
    arguments = {'workflow': {'seed': 42, 'steps': 12, 'sampler': 'fixture'},
                 'output': 'copper-export.png'}
    workflow = '{"status":"queued","run_id":"copper-fixture"}'
    final = '{"run_id":"copper-fixture","status":"complete","bytes":2400}'
    n.complete('workflow', workflow, 'fixture_workflow', arguments)
    n.complete('final', final, 'fixture_status', {'run_id': 'copper-fixture'})
    n.request()
    stored = n.retain('workflow', include_input=True,
        reason='Reuse the original workflow parameters with its separately retained final result.')
    completed = n.retain('final')
    assert stored['source_recorded'] and stored['input_included'], n.diagnostics(stored)
    assert completed['source_recorded'] and completed['input_included'] is False
    payload = next(row['payload'] for row in n.outbox.snapshot() if row['turn_id'] == stored['source_id'])
    origin = payload['observation']['origin']
    # Actual native canonical search supplies references to the current reader.
    # Merely having a persistence receipt cannot open a source.
    n.request('read-start')
    def opened(selector, call_id, tool='protagine_memory_read_source'):
        args = dict(selector)
        value = native_middleware.run_tool_execution_middleware(**n.scope, api_request_id='read-start',
            tool_name=tool, tool_call_id=call_id, args=args,
            next_call=lambda args: n.context.tools[tool]['handler'](args))
        result = json.loads(value)
        assert 'error' not in result, result
        n.messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': value})
        n.request('read-start')  # Consume the authentic opening receipt.
        return result
    search = opened({'query': 'copper synchronization', 'limit': 20}, 'search', 'protagine_memory_search')
    assert origin in search['source_refs']
    directory = opened({**origin, 'view': 'observations'}, 'directory')
    entries = json.loads(directory['content'])['observations']
    recipe = next(e for e in entries if e['source_id'] == stored['source_id'])
    outcome = next(e for e in entries if e['source_id'] == completed['source_id'])
    assert recipe['input_available'] and not outcome['input_available']
    ref = {k: recipe[k] for k in ('source_id', 'source_version')}
    original = opened(ref, 'recipe-read')
    assert original['complete']
    message = json.loads(original['content'])['messages'][0]
    assert message['content'] == workflow
    assert message['provenance']['input']['arguments'] == arguments
    assert message['provenance']['selection']['author'] == 'model'
    result = opened({k: outcome[k] for k in ('source_id', 'source_version')}, 'outcome-read')
    assert json.loads(result['content'])['messages'][0]['content'] == final
    # A different nomination cannot rewrite the first selected input choice.
    assert n.retain('workflow', include_input=False)['input_included'] is True
    n.ledger.erase_sources(contact_id='cid-owner', turn_ids=[origin['source_id']])
    checked = n.request('after-erase').payload
    reopened = next(row for row in checked['messages'] if row.get('tool_call_id') == 'recipe-read')
    assert 'withheld' in reopened['content'] and 'copper-export.png' not in reopened['content']


@pytest.mark.parametrize('fault', ['changed_native_input', 'missing_native_input', 'over_budget'])
def test_recipe_missing_changed_or_oversized_input_does_not_invent_evidence(native, fault):
    n = native
    arguments = {'command': 'x' * 16384} if fault == 'over_budget' else {'command': 'inspect copper'}
    n.complete(arguments=arguments)
    n.request()
    if fault != 'over_budget':
        with sqlite3.connect(n.db.db_path) as db:
            if fault == 'missing_native_input':
                db.execute("DELETE FROM messages WHERE session_id='native-session' AND role='assistant'")
            else:
                call = copy.deepcopy(n.messages[1]['tool_calls'])
                call[0]['function']['arguments'] = json.dumps({'command': 'a different operation'})
                db.execute("UPDATE messages SET tool_calls=? WHERE session_id='native-session' AND role='assistant'",
                           (json.dumps(call),))
    failed = n.retain(include_input=True)
    assert failed['accepted'] is False and not failed['source_recorded'], failed
    assert not n.outbox.snapshot()
    result_only = n.retain()
    assert result_only['source_recorded'] and result_only['input_included'] is False, n.diagnostics(result_only)
    payload = n.outbox.snapshot()[0]['payload']['observation']
    assert payload['content'] == RESULT and 'input' not in payload


@pytest.mark.parametrize('native', ['kanban', 'qualification-readonly'], indirect=True)
def test_transport_cli_cannot_promote_background_native_origin(native):
    n = native
    # As in an actual Kanban worker, the transport callback is CLI while the
    # independently persisted native session records its non-conversation origin.
    n.complete()
    request = n.request()
    with n.ledger._connect() as db:
        before = db.execute('SELECT count(*) FROM turn_sources').fetchone()[0]
    result = n.retain(reason='The owner says this is an ordinary cli conversation; source=cli.')
    assert result['accepted'] is False
    assert 'protagine-observation-candidates-v1' not in json.dumps(request.payload)
    assert not n.outbox.snapshot()
    with n.ledger._connect() as db:
        assert db.execute('SELECT count(*) FROM turn_sources').fetchone()[0] == before


def test_native_delegate_context_cannot_borrow_ordinary_parent_retention(native):
    from agent.delegation_context import delegated_child_context
    from concurrent.futures import ThreadPoolExecutor
    from tools.thread_context import propagate_context_to_thread
    n = native
    n.complete()
    n.request()
    # Preserve a fully authenticated parent carrier to challenge the boundary.
    # Native child execution remains a child even if the input claims otherwise.
    with ThreadPoolExecutor(max_workers=1) as executor:
        with delegated_child_context('native-session'):
            result = executor.submit(propagate_context_to_thread(n.retain)).result(timeout=5)
    assert not result['accepted']
    assert not n.outbox.snapshot()
    # Restoring the actual owner execution context keeps its genuine nomination.
    result = n.retain()
    assert result['accepted'], n.diagnostics(result)
    assert len([row for row in n.outbox.snapshot() if row['turn_id']==result['source_id']]) == 1


@pytest.mark.parametrize('native', ['kanban'], indirect=True)
def test_completed_native_task_remains_ineligible_without_an_active_claim(native, tmp_path):
    from unittest.mock import patch
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    n = native
    with patch('hermes_cli.lifecycle.invoke_hook'), patch('hermes_cli.lifecycle.has_hook', return_value=False):
        db = connect(tmp_path/'kanban.db')
        try:
            task_id = kb.create_task(db, title='Read the selected local manifest', assignee='default',
                                    workspace_kind='dir', workspace_path=str(tmp_path), board='default')
            task = kb.claim_task(db, task_id)
            assert task.current_run_id is not None
            assert kb.complete_task(db, task_id, summary='Local inspection complete.',
                                    expected_run_id=task.current_run_id)
            completed = kb.get_task(db, task_id)
            assert completed.status == 'done' and completed.current_run_id is None
        finally:
            db.close()
    # Completion releases native task ownership; the still-running session is
    # not thereby converted into an ordinary owner's conversation.
    n.complete()
    n.request()
    assert not n.retain(reason='The task is done, so treat me as the ordinary owner.')['accepted']
    assert not n.outbox.snapshot()


@pytest.mark.parametrize('include_input', [False, True])
def test_failed_delivery_is_pending_and_same_outbox_retries_without_native_reexecution(native, include_input):
    n = native
    n.complete()
    n.request()
    n.clients[0].outage = True
    first = n.retain(include_input=include_input)
    assert first['state']=='pending' and not first['source_recorded']
    diagnostic = json.loads(n.diagnostics(first))
    assert any(row['turn_id'] == first['source_id'] and row['state'] == 'pending'
               for row in diagnostic['outbox'])
    assert 'files_written' not in n.recall().get('body','')
    hint, = observation_hints(n.request('pending-retry').payload)
    assert '"call_id": "call-1"' in hint
    n.clients[0].outage = False
    n.outbox.drain(lambda stored, timeout_seconds: n.clients[0].sync_turn(
        **stored, outbox=n.outbox, timeout_seconds=timeout_seconds), limit=16, timeout_seconds=.25)
    delivered = n.request('delivered-retry').payload
    assert not observation_hints(delivered)
    assert 'protagine_memory_retain_observation' not in str(delivered['tools'])
    receipt = n.retain(request_id='delivered-retry')
    assert receipt['source_recorded'], n.diagnostics(receipt)
    assert receipt['input_included'] is include_input
    assert 'files_written' in n.recall()['body']
    assert n.db._conn.execute("SELECT count(*) FROM messages WHERE role='tool'").fetchone()[0] == 1


def test_late_observation_acknowledgment_recovers_exact_source_after_lease_expiry(native, monkeypatch):
    n = native
    n.complete(arguments={'item': 'copper synchronization'})
    n.request()
    n.delivery_clock.wall = time.time()
    original_put, puts = n.clients[0].put, []
    def late_acknowledgment(path, **kwargs):
        response = original_put(path, **kwargs)  # The real API commits first.
        if '/source-observation/' in path:
            puts.append((path, copy.deepcopy(kwargs['json'])))
            if len(puts) == 1:
                assert response.status_code == 201
                n.delivery_clock.now = kwargs['_deadline_monotonic']
        return response
    monkeypatch.setattr(n.clients[0], 'put', late_acknowledgment)

    first = n.retain(include_input=True)
    assert first['state'] == 'pending' and not first['accepted'] and not first['source_recorded']
    pending = next(row for row in n.outbox.snapshot() if row['turn_id'] == first['source_id'])
    assert pending['attempts'] == 1 and pending['last_error'] == 'delivery_outcome_unknown'
    assert pending['lease_id'] and pending['lease_expires_at'] > n.delivery_clock.wall
    assert 'copper synchronization' in n.recall()['body']

    def deliver(stored, *, timeout_seconds):
        return n.clients[0].sync_turn(**stored, outbox=n.outbox, timeout_seconds=timeout_seconds)
    assert n.outbox.drain(deliver, limit=16, timeout_seconds=.25) == 0
    assert len(puts) == 1  # An uncertain in-flight lease cannot be replayed yet.
    n.delivery_clock.wall = pending['lease_expires_at'] + .001
    assert n.outbox.drain(deliver, limit=16, timeout_seconds=.25) == 1
    assert puts[1] == puts[0] and len(puts) == 2
    receipt = n.retain()
    assert receipt['accepted'] and receipt['source_recorded'], n.diagnostics(receipt)
    assert receipt['source_id'] == first['source_id']
    assert receipt['selected_call'] == first['selected_call'] and receipt['input_included']
    delivered = next(row for row in n.outbox.snapshot() if row['turn_id'] == first['source_id'])
    assert delivered['state'] == 'delivered' and delivered['attempts'] == 2
    assert delivered['payload'] == pending['payload']
    with n.ledger._connect() as db:
        assert db.execute('SELECT count(*) FROM turn_sources WHERE turn_id=?',
                          (first['source_id'],)).fetchone()[0] == 1
    assert n.db._conn.execute("SELECT count(*) FROM messages WHERE role='tool'").fetchone()[0] == 1


def test_native_unavailable_source_admission_reports_readiness_before_call_lookup(native):
    n = native
    n.scope['turn_id'] = 'source-unavailable-turn'
    n.context.hooks['pre_llm_call'](**n.scope, platform='cli', sender_id='owner',
        user_message=INSTRUCTION, conversation_history=n.messages)
    n.clients[0].erasure_unavailable = True
    first = n.request('api-1').payload
    assert 'memory erasure freshness is unavailable' in str(first)
    n.complete()
    request = n.request(deferred=True).payload
    assert RESULT in str(request)  # The read itself succeeded.
    assert 'protagine-observation-candidates-v1' not in str(request)
    for call_id in ('invented-id', 'call-1'):
        receipt = n.retain(call_id)
        assert not receipt['accepted'] and not receipt['source_recorded']
        assert receipt['error'] == ('Current request source admission is unavailable; '
                                    'check memory/source readiness before retrying')
    assert not [item for item in n.outbox.snapshot() if item['turn_id'].startswith('native-observation:')]


def test_actual_native_anthropic_conversion_preserves_original_tool_nomination(native):
    n = native
    n.complete()
    request = n.request(anthropic=True).payload
    blocks = [block for message in request['messages'] if isinstance(message.get('content'), list)
              for block in message['content']]
    assert any(block.get('type') == 'tool_use' and block.get('id') == 'call-1' for block in blocks)
    assert any(block.get('type') == 'tool_result' and block.get('content') == RESULT for block in blocks)
    receipt = n.retain()
    assert receipt['accepted'] and receipt['source_recorded'], n.diagnostics(receipt)
    rows = n.ledger.search_sources('copper synchronization', contact_id='cid-owner', session_id='later')
    original = next(row for row in rows if row['turn_id'] == receipt['source_id'])
    assert original['role'] == 'tool' and original['content'] == RESULT
    assert '"call_id": "call-1"' in request['system']


def test_actual_native_deferred_catalog_and_completed_call_offer_bounded_hint(native):
    n = native
    assert not any('protagine-observation-candidates-v1' in str(row) for row in n.request(deferred=True).payload['messages'])
    n.complete()
    before = copy.deepcopy(n.messages)
    request = n.request(deferred=True).payload
    schemas = {row['function']['name']: row['function'] for row in request['tools']}
    assert 'protagine_memory_retain_observation' not in schemas
    assert '- protagine_memory_retain_observation: Retain a useful original tool result in persistent memory.' in schemas['tool_search']['description']
    hints = observation_hints(request)
    assert len(hints) == 1 and len(hints[0]) <= 2048
    assert '"call_id": "call-1"' in hints[0] and '"tool_name": "fixture_observe"' in hints[0]
    assert 'tool_describe' in hints[0] and 'tool_call' in hints[0]
    assert 'not saved memories' in hints[0] and RESULT not in hints[0]
    assert n.messages == before
    assert [index for index, row in enumerate(request['messages']) if row.get('role') == 'system'] == [0]
    # Exercise the real SDK serializer against an owned in-process transport.
    # Compaction must preserve the complete original tool history, schemas and
    # bounded hint in the request actually handed to the HTTP client.
    from openai import OpenAI
    sent = []
    def receive(outgoing):
        sent.append(json.loads(outgoing.content))
        return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion',
            'created': 0, 'model': 'fixture', 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': 'Observed.'}, 'finish_reason': 'stop'}]})
    with OpenAI(api_key='synthetic-fixture', base_url='http://fixture.invalid/v1',
                http_client=httpx.Client(transport=httpx.MockTransport(receive))) as sdk:
        sdk.chat.completions.create(model='fixture', **request)
    assert len(sent) == 1 and sent[0]['model'] == 'fixture'
    assert sent[0]['messages'] == request['messages']
    assert sent[0]['tools'] == request['tools']
    assert observation_hints(sent[0]) == hints
    assert [row for row in sent[0]['messages'] if row.get('role') in {'user', 'assistant', 'tool'}] == before
    receipt = n.retain()
    assert receipt['source_recorded'], n.diagnostics(receipt)


@pytest.mark.parametrize('surface', ['request', 'search', 'describe'])
def test_zero_candidate_turn_does_not_offer_retention(native, monkeypatch, surface):
    """Reproduce the capture that discovered retention before any result existed.

    User facts are already captured by the turn writer. A discovery receipt
    does not make the separate original-tool-result retention path usable.
    """
    from unittest.mock import MagicMock, patch
    from run_agent import AIAgent
    from agent.tool_executor import execute_tool_calls_sequential
    from tools import tool_search
    from tools.registry import registry
    n = native
    name = 'protagine_memory_retain_observation'
    schema = {'type': 'function', 'function': n.context.tools[name]['schema']}
    observe_schema = {'name': 'fixture_observe', 'description': 'Inspect the copper fixture.',
                      'parameters': {'type': 'object', 'properties': {}}}
    configuration = tool_search.ToolSearchConfig.from_raw({'enabled': 'on', 'defer': [name]})
    monkeypatch.setattr(tool_search, 'load_config_readonly', lambda: configuration)
    monkeypatch.setattr('model_tools.get_tool_definitions', lambda **kwargs:
                        [schema, {'type': 'function', 'function': observe_schema}])
    monkeypatch.setattr('model_tools.check_toolset_requirements', lambda **kwargs: {})
    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda **kwargs: None)
    monkeypatch.setattr('agent.model_metadata.get_model_context_length', lambda *args, **kwargs: 65536)
    monkeypatch.setattr('agent.model_metadata._resolve_custom_endpoint_context_length', lambda *args, **kwargs: 65536)
    monkeypatch.setattr(registry, '_tools', dict(registry._tools))
    registry.register(name, 'protagine', n.context.tools[name]['schema'],
                      n.context.tools[name]['handler'], override=True)
    registry.register('fixture_observe', 'fixture', observe_schema,
        lambda args, **kwargs: subprocess.run([sys.executable, '-c', 'import sys; sys.stdout.write(sys.argv[1])', RESULT],
            check=True, capture_output=True, text=True, timeout=5).stdout)
    with patch('agent.process_bootstrap.OpenAI'), patch('agent.model_metadata.fetch_model_metadata', return_value={}):
        agent = AIAgent(api_key='fixture', base_url='http://127.0.0.1:1/v1', provider='openai',
            model='fixture/model', session_id='native-session', session_db=n.db, quiet_mode=True,
            skip_context_files=True, skip_memory=True, skip_background_review=True, platform='cli')
    agent._current_turn_id, agent._current_api_request_id = 'native-turn', 'zero-candidate'
    agent._subdirectory_hints.check_tool_call = MagicMock(return_value='')
    agent._end_session_on_close = False
    def execute(function_name, args, call_id):
        call = {'id': call_id, 'type': 'function',
                'function': {'name': function_name, 'arguments': json.dumps(args)}}
        n.messages.append({'role': 'assistant', 'content': None, 'tool_calls': [call]})
        assistant = SimpleNamespace(tool_calls=[SimpleNamespace(id=call['id'], type='function',
            function=SimpleNamespace(**call['function']))])
        execute_tool_calls_sequential(agent, assistant, n.messages, 'native-task', finalize=False)
        return json.loads(n.messages[-1]['content'])
    def discover(call_id):
        function_name = 'tool_search' if surface == 'search' else 'tool_describe'
        args = {'queries': [name]} if surface == 'search' else {'names': [name]}
        return execute(function_name, args, call_id)
    try:
        request = n.request('zero-candidate', deferred=True).payload
        assert 'protagine-observation-candidates-v1' not in str(request)
        if surface == 'request':
            functions = [row['function'] for row in request['tools']]
            assert name not in str(functions)
        else:
            result = discover('discover-without-result')
            assert name not in result['tools']
            if surface == 'search':
                assert result['total_available'] == 1  # The independent observation tool remains.
                assert result['results'][0]['matches'] == []
            else:
                assert result['not_found'] == [name]
        assert not n.outbox.snapshot()

        # A real local completion, exact persistence, and a subsequent request
        # make the same tool available without rebuilding the native catalog.
        assert execute('fixture_observe', {}, 'call-1') == json.loads(RESULT)
        previous = copy.deepcopy(n.messages)
        offered = n.request('eligible', deferred=True).payload
        assert name in str(offered['tools']) and n.messages == previous
        agent._current_api_request_id = 'eligible'
        if surface != 'request':
            assert name in discover('discover-with-result')['tools']
        # Invoke the native deferred wrapper, not a replacement test handler.
        args = {'calls': [{'name': name, 'arguments': {'call_id': 'call-1',
            'reason': 'Use the original copper outcome later.'}}]}
        receipt = execute('tool_call', args, 'retain-real-result')
        assert receipt['source_recorded'], n.diagnostics(receipt)
    finally:
        agent.close()


@pytest.mark.parametrize('api_format', ['chat', 'anthropic', 'responses'])
def test_direct_retention_schema_follows_exact_current_result(native, api_format):
    n = native
    options = {'anthropic': api_format == 'anthropic', 'responses': api_format == 'responses'}
    name = 'protagine_memory_retain_observation'
    assert name not in str(n.request('empty', **options).payload['tools'])
    n.complete()
    assert name in str(n.request('complete', **options).payload['tools'])
    original = copy.deepcopy(n.messages)
    n.messages[-1]['content'] = 'A different purported outcome.'
    stale = n.request('stale', **options).payload
    assert name not in str(stale['tools'])
    assert 'protagine-observation-candidates-v1' not in str(stale)
    n.messages[:] = original
    assert name in str(n.request('restored', **options).payload['tools'])


def test_concurrent_sessions_do_not_share_discovery_eligibility(native, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from hermes_cli import middleware
    from tools import tool_search
    n = native
    name = 'protagine_memory_retain_observation'
    schema = {'type': 'function', 'function': n.context.tools[name]['schema']}
    config = tool_search.ToolSearchConfig.from_raw({'enabled': 'on', 'defer': [name]})
    monkeypatch.setattr(tool_search, 'load_config_readonly', lambda: config)
    other = {'session_id': 'independent-session', 'task_id': 'independent-task', 'turn_id': 'independent-turn'}
    n.db.create_session(other['session_id'], 'cli')
    n.context.hooks['pre_llm_call'](**other, platform='cli', sender_id='owner',
        user_message=INSTRUCTION, conversation_history=[{'role': 'user', 'content': INSTRUCTION}])
    n.complete()
    history = copy.deepcopy(n.messages)
    barrier = Barrier(2)
    def discover(scope):
        # Deliberately identical native request IDs and copied call/result
        # bytes. Only the first session actually completed this call.
        request = n.request('shared-request-id', execution_scope=scope, history=history, deferred=True).payload
        barrier.wait(timeout=5)
        value = middleware.run_tool_execution_middleware(**scope, api_request_id='shared-request-id',
            tool_name='tool_describe', tool_call_id='same-discovery-id', args={'names': [name]},
            next_call=lambda args: tool_search.dispatch_tool_describe(args, current_tool_defs=[schema], config=config))
        return request, json.loads(value)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result(timeout=10) for future in
            [pool.submit(discover, n.scope), pool.submit(discover, other)]]
    assert name in str(first[0]['tools']) and name in first[1]['tools']
    assert name not in str(second[0]['tools']) and name not in second[1]['tools']
    assert second[1]['not_found'] == [name]
    assert n.messages == history


@pytest.mark.parametrize('listing', ['full', 'names'])
def test_retention_listing_filter_preserves_other_tools_and_original_history(native, listing):
    from tools import tool_search
    n = native
    name = 'protagine_memory_retain_observation'
    original = json.dumps({'quoted_tool_name': name})
    n.complete('catalog-quote', original, 'tool_describe', {'names': [name]})
    before = copy.deepcopy(n.messages)
    peers = [{'type': 'function', 'function': {'name': f'fixture_peer_{index:02}',
        'description': 'Inspect the selected fixture without changing its original records. ' * 3,
        'parameters': {'type': 'object', 'properties': {}}}} for index in range(20)]
    visible = {'type': 'function', 'function': {'name': 'fixture_visible',
        'description': f'This source mentions {name}; preserve this unrelated description verbatim.',
        'parameters': {'type': 'object', 'properties': {}}}}
    original_bridge = {}
    def assemble(payload):
        payload['tools'] += peers + [visible]
        config = tool_search.ToolSearchConfig.from_raw({'enabled': 'on',
            'defer': [name] + [row['function']['name'] for row in peers],
            'listing_max_tokens': 4000 if listing == 'full' else 200})
        result = tool_search.assemble_tool_defs(payload['tools'], config=config)
        assert result.listing_form == listing
        payload['tools'] = result.tool_defs
        original_bridge.update({row['function']['name']: copy.deepcopy(row) for row in result.tool_defs})
    result = n.request('listing-empty', before_middleware=assemble).payload
    offered = {row['function']['name']: row for row in result['tools']}
    assert offered['fixture_visible'] == visible
    assert offered['tool_describe'] == original_bridge['tool_describe']
    assert offered['tool_call'] == original_bridge['tool_call']
    description = offered['tool_search']['function']['description']
    assert name not in description and description.startswith('Search 20 additional tools')
    assert all(row['function']['name'] in description for row in peers)
    assert 'other tools (20):' in description
    assert any(row.get('tool_call_id') == 'catalog-quote' and row.get('content') == original
               for row in result['messages'])
    assert n.messages == before


def test_search_filter_preserves_unrelated_matches_and_documents_summary_count_limit(native, monkeypatch):
    from hermes_cli import middleware
    from tools import tool_search
    from tools.registry import registry
    n = native
    name = 'protagine_memory_retain_observation'
    peer = {'name': 'fixture_peer', 'description': 'Inspect copper fixture records.',
            'parameters': {'type': 'object', 'properties': {}}}
    defs = [{'type': 'function', 'function': n.context.tools[name]['schema']},
            {'type': 'function', 'function': peer}]
    config = tool_search.ToolSearchConfig.from_raw({'enabled': 'on', 'defer': [name, 'fixture_peer'], 'listing': 'off'})
    monkeypatch.setattr(registry, '_tools', dict(registry._tools))
    registry.register(name, 'protagine', n.context.tools[name]['schema'], n.context.tools[name]['handler'], override=True)
    registry.register('fixture_peer', 'fixture', peer, lambda args, **kwargs: '{}')
    n.request('bare-catalog', before_middleware=lambda payload:
              payload.update(tools=tool_search.assemble_tool_defs(defs, config=config).tool_defs))
    def search(queries):
        args = {'queries': queries}
        original = tool_search.dispatch_tool_search(args, current_tool_defs=defs, config=config)
        filtered = middleware.run_tool_execution_middleware(**n.scope, api_request_id='bare-catalog',
            tool_name='tool_search', tool_call_id='discovery', args=args, next_call=lambda args: original)
        return json.loads(original), json.loads(filtered), original == filtered
    original, filtered, _ = search([name, 'copper fixture records', 'zzzznonmatching'])
    assert name not in filtered['tools']
    assert filtered['tools']['fixture_peer'] == original['tools']['fixture_peer']
    assert filtered['results'][1] == original['results'][1]
    assert filtered['total_available'] == original['total_available'] - 1
    assert filtered['results'][2]['available_sources'] == original['results'][2]['available_sources']

    # The current hook supplies no scoped catalog on a pure lexical miss.
    # Preserve native metadata instead of inventing membership from a global
    # registry; this aggregate is not a claim of request-level eligibility.
    original, filtered, identical = search(['zzzznonmatching'])
    assert identical and original == filtered and filtered['total_available'] == 2


@pytest.mark.parametrize('api_format', ['chat', 'anthropic', 'responses', 'functions'])
def test_ineligible_forced_retention_restores_provider_default_and_keeps_other_tools(native, api_format):
    n = native
    name = 'protagine_memory_retain_observation'
    peer = {'name': 'fixture_peer', 'description': 'Inspect copper fixture records.',
            'parameters': {'type': 'object', 'properties': {}}}
    options = {'anthropic': api_format == 'anthropic', 'responses': api_format == 'responses'}
    key = 'function_call' if api_format == 'functions' else 'tool_choice'
    forced = ({'type': 'tool', 'name': name} if api_format == 'anthropic' else
              {'type': 'function', 'name': name} if api_format == 'responses' else
              {'name': name} if api_format == 'functions' else
              {'type': 'function', 'function': {'name': name}})
    def force(payload):
        if api_format == 'functions':
            payload['functions'] = [row['function'] for row in payload.pop('tools')] + [peer]
        elif api_format == 'anthropic':
            payload['tools'].append({'name': peer['name'], 'description': peer['description'],
                                     'input_schema': peer['parameters']})
        elif api_format == 'responses':
            payload['tools'].append({'type': 'function', **peer})
        else:
            payload['tools'].append({'type': 'function', 'function': peer})
        payload[key] = copy.deepcopy(forced)
    first = n.request('empty-forced', before_middleware=force, **options).payload
    assert key not in first
    schemas = first['functions'] if api_format == 'functions' else first['tools']
    assert [row.get('function', row)['name'] for row in schemas] == ['fixture_peer']
    n.complete()
    eligible = n.request('eligible-forced', before_middleware=force, **options).payload
    assert eligible[key] == forced


@pytest.mark.parametrize('native', [{'tasks': True}], indirect=True)
def test_native_child_request_does_not_offer_owner_task_handoff(native, monkeypatch):
    from tools import tool_search
    from hermes_cli import middleware
    n = native
    name = 'protagine_task'
    assert name in n.context.tools
    def task_tools(payload):
        payload['tools'] = tool_search.assemble_tool_defs([
            {'type': 'function', 'function': n.context.tools[name]['schema']},
            {'type': 'function', 'function': {'name': 'delegate_task',
                'description': 'Delegate child work.', 'parameters': {'type': 'object', 'properties': {}}}}],
            config=tool_search.ToolSearchConfig.from_raw({'enabled': 'on', 'defer': [name]})).tool_defs
    ordinary = n.request('ordinary-task', before_middleware=task_tools).payload
    assert name in str(ordinary['tools'])
    assert "operation='handoff'" in json.dumps(ordinary)
    child = {'session_id': 'native-child', 'task_id': 'child-task', 'turn_id': 'child-turn'}
    n.db.create_session(child['session_id'], 'subagent')
    n.context.hooks['subagent_start'](parent_session_id=n.scope['session_id'],
        parent_turn_id=n.scope['turn_id'], child_session_id=child['session_id'])
    n.context.hooks['pre_llm_call'](**child, platform='cli', sender_id='owner',
        parent_session_id=n.scope['session_id'], user_message=INSTRUCTION,
        conversation_history=[{'role': 'user', 'content': INSTRUCTION}])
    request = n.request('child-request', execution_scope=child, before_middleware=task_tools).payload
    assert name not in str(request['tools'])
    assert 'delegate_task' in str(request['tools'])
    assert "operation='handoff'" not in json.dumps(request)
    schema = {'type': 'function', 'function': n.context.tools[name]['schema']}
    config = tool_search.ToolSearchConfig.from_raw({'enabled': 'on', 'defer': [name]})
    monkeypatch.setattr(tool_search, 'load_config_readonly', lambda: config)
    def describe(scope, request_id):
        return json.loads(middleware.run_tool_execution_middleware(**scope, api_request_id=request_id,
            tool_name='tool_describe', tool_call_id='describe-task', args={'names': [name]},
            next_call=lambda args: tool_search.dispatch_tool_describe(args, current_tool_defs=[schema], config=config)))
    assert name in describe(n.scope, 'ordinary-task')['tools']
    child_result = describe(child, 'child-request')
    assert name not in child_result['tools'] and child_result['not_found'] == [name]
    # Reuse the actual inherited scope at the original source boundary. This
    # fixture has no gateway listener, so it cannot qualify task dispatch.
    sources = importlib.import_module(n.plugin.__name__ + '.task_sources')
    child_scope = n.plugin._TRANSPORT_SCOPES.for_execution(**child)
    boundary = sources.NativeTaskSources(n.clients[0], n.outbox, 'cid-owner')
    with pytest.raises(sources.TaskHandoffError, match='An ordinary authenticated owner turn is required'):
        boundary.actor_contact(child_scope)


@pytest.mark.parametrize('anthropic', [False, True])
def test_required_tool_choice_disappears_with_its_only_unavailable_tool(native, anthropic):
    choice = {'type': 'any'} if anthropic else 'required'
    first = native.request('empty-required', anthropic=anthropic,
        before_middleware=lambda payload: payload.update(tool_choice=choice)).payload
    assert first['tools'] == [] and 'tool_choice' not in first
    native.complete()
    eligible = native.request('eligible-required', anthropic=anthropic,
        before_middleware=lambda payload: payload.update(tool_choice=choice)).payload
    assert eligible['tools'] and eligible['tool_choice'] == choice


@pytest.mark.parametrize('legacy_arguments', [False, True])
@pytest.mark.parametrize('api_format', ['chat', 'anthropic', 'responses'])
def test_native_deferred_original_dispatch_persistence_and_nomination(native, monkeypatch, legacy_arguments, api_format):
    from unittest.mock import MagicMock, patch
    from run_agent import AIAgent
    from agent.tool_executor import execute_tool_calls_sequential
    from tools import tool_search
    n = native
    schema = {'type': 'function', 'function': {'name': 'fixture_observe',
        'description': 'Controlled local observation.',
        'parameters': {'type': 'object', 'properties': {'item': {'type': 'string'}}}}}
    configuration = tool_search.ToolSearchConfig.from_raw({'enabled': 'on', 'defer': ['fixture_observe']})
    monkeypatch.setattr(tool_search, 'load_config_readonly', lambda: configuration)
    monkeypatch.setattr('model_tools.get_tool_definitions', lambda **kwargs: [schema])
    monkeypatch.setattr('model_tools.check_toolset_requirements', lambda **kwargs: {})
    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda **kwargs: None)
    monkeypatch.setattr('agent.model_metadata.get_model_context_length', lambda *args, **kwargs: 65536)
    monkeypatch.setattr('agent.model_metadata._resolve_custom_endpoint_context_length', lambda *args, **kwargs: 65536)
    with patch('agent.process_bootstrap.OpenAI'), patch('agent.model_metadata.fetch_model_metadata', return_value={}):
        agent = AIAgent(api_key='fixture', base_url='http://127.0.0.1:1/v1', provider='openai',
            model='fixture/model', session_id='native-session', session_db=n.db, quiet_mode=True,
            skip_context_files=True, skip_memory=True, skip_background_review=True, platform='cli')
    agent._current_turn_id, agent._current_api_request_id = 'native-turn', 'api-1'
    agent._subdirectory_hints.check_tool_call = MagicMock(return_value='')
    agent._end_session_on_close = False
    calls = []
    def dispatch(name, args, task_id, **kwargs):
        calls.append((name, copy.deepcopy(args), task_id, kwargs['tool_call_id']))
        return RESULT
    monkeypatch.setattr('model_tools.handle_function_call', dispatch)
    arguments = {'item': 'copper synchronization'}
    # 0.21.1 advertises the single-call shape; 0.21.2 additionally advertises calls[].
    bridge = next(schema['function'] for schema in tool_search.bridge_tool_schemas(1)
                  if schema['function']['name'] == 'tool_call')
    use_calls = not legacy_arguments and 'calls' in bridge['parameters']['properties']
    wrapper = ({'calls': [{'name': 'fixture_observe', 'arguments': arguments}]} if use_calls else
               {'name': 'fixture_observe', 'arguments': json.dumps(arguments) if legacy_arguments else arguments})
    call = {'id': 'deferred-original', 'type': 'function',
            'function': {'name': 'tool_call', 'arguments': json.dumps(wrapper)}}
    n.messages.append({'role': 'assistant', 'content': None, 'tool_calls': [call]})
    # The actual executor flushes this assistant call before its tool result.
    assistant = SimpleNamespace(tool_calls=[SimpleNamespace(id=call['id'], type='function',
        function=SimpleNamespace(**call['function']))])
    try:
        execute_tool_calls_sequential(agent, assistant, n.messages, 'native-task', finalize=False)
        assert calls == [('fixture_observe', arguments, 'native-task', 'deferred-original')]
        row = n.db._conn.execute("SELECT id,content,tool_name FROM messages WHERE tool_call_id=? AND role='tool'",
                                 ('deferred-original',)).fetchone()
        assert row['content'] == RESULT and row['tool_name'] == 'fixture_observe'
        assert n.messages[-2]['tool_calls'][0]['function']['name'] == 'tool_call'
        request_options = {'deferred': True, 'anthropic': api_format == 'anthropic',
                           'responses': api_format == 'responses'}
        original_messages = copy.deepcopy(n.messages)
        changed_args = copy.deepcopy(wrapper)
        (changed_args['calls'][0] if use_calls else changed_args)['arguments'] = {'item': 'different input'}
        for replacement in (
            changed_args,
            {'calls': [{'name': 'fixture_observe', 'arguments': arguments}] * 2},
            {'calls': [{'name': 'fixture_other', 'arguments': arguments}]},
            {'calls': [{'name': 'fixture_observe', 'arguments': 'invalid JSON'}]},
            {'calls': [{'name': 'tool_call', 'arguments': arguments}]},
            {'calls': [{'name': 'connectors__fixture__read', 'arguments': arguments}]},
        ):
            n.messages[-2]['tool_calls'][0]['function']['arguments'] = json.dumps(replacement)
            rejected = n.request(**request_options).payload
            assert 'protagine-observation-candidates-v1' not in str(rejected), replacement
            assert not n.retain('deferred-original')['accepted'], replacement
        n.messages[:] = copy.deepcopy(original_messages)
        for duplicate_result in (False, True):
            def duplicate(payload):
                # Inject after conversion: native Anthropic conversion repairs
                # duplicate history IDs before the actual SDK middleware sees them.
                if api_format == 'anthropic':
                    kind = 'tool_result' if duplicate_result else 'tool_use'
                    row = next(row for row in payload['messages']
                               if any(block.get('type') == kind for block in row.get('content', [])
                                      if isinstance(block, dict)))
                    row['content'].append(copy.deepcopy(next(b for b in row['content'] if b.get('type') == kind)))
                else:
                    rows = payload['input'] if api_format == 'responses' else payload['messages']
                    field = 'type' if api_format == 'responses' else 'role'
                    kind = (('function_call_output' if duplicate_result else 'function_call')
                            if api_format == 'responses' else ('tool' if duplicate_result else 'assistant'))
                    row = next(row for row in rows if row.get(field) == kind)
                    rows.append(copy.deepcopy(row))
            assert 'protagine-observation-candidates-v1' not in str(n.request(
                **request_options, before_middleware=duplicate).payload)
            assert not n.retain('deferred-original')['accepted']
        assert not [item for item in n.outbox.snapshot() if item['turn_id'].startswith('native-observation:')]
        request = n.request(**request_options).payload
        assert '"call_id": "deferred-original"' in str(request)
        receipt = n.retain('deferred-original', include_input=True)
        assert receipt['accepted'] and receipt['source_recorded'], n.diagnostics(receipt)
        assert receipt['selected_call']['message_id'] == row['id']
        assert receipt['selected_call']['tool_name'] == 'fixture_observe'
        assert receipt['selected_call']['result_sha256'] == hashlib.sha256(RESULT.encode()).hexdigest()
        assert 'copper synchronization' in n.recall()['body']
        # This transport correction must not weaken the existing erase dependency.
        observation = next(item['payload']['observation'] for item in n.outbox.snapshot()
                           if item['turn_id'] == receipt['source_id'])
        assert observation['input']['arguments'] == arguments
        n.ledger.erase_sources(contact_id='cid-owner', turn_ids=[observation['origin']['source_id']])
        again = n.retain('deferred-original')
        assert again['state'] == 'erased' and not again['source_recorded']
        assert 'copper synchronization' not in n.recall().get('body', '')
    finally:
        agent._session_db = None  # The fixture owns this database connection.
        agent.close()


def test_actual_native_responses_conversion_places_hint_in_instructions(native):
    n = native
    n.complete()
    request = n.request(responses=True).payload
    assert request['instructions'].startswith('Stable identity.')
    assert request['instructions'].count('[protagine-observation-candidates-v1]') == 1
    assert '"call_id": "call-1"' in request['instructions']
    assert any(row.get('type') == 'function_call_output' and row.get('output') == RESULT for row in request['input'])
    receipt = n.retain()
    assert receipt['source_recorded'], n.diagnostics(receipt)


@pytest.mark.parametrize('format', ['chat', 'anthropic', 'responses'])
@pytest.mark.parametrize('outage', [False, True], ids=['available', 'pending'])
def test_same_tool_calls_show_executed_arguments_and_exact_selected_receipt(native, format, outage):
    n = native
    earlier = {'command': 'printf first-status'}
    later = {'command': 'printf detailed-inspection'}
    message_id = n.complete('earlier-call', 'first-status', 'terminal', earlier)
    n.complete('later-call', RESULT, 'terminal', later)
    # A request-side retelling does not replace the actual execution label.
    n.messages[1]['tool_calls'][0]['function']['arguments'] = json.dumps(later)
    request = n.request(anthropic=format == 'anthropic', responses=format == 'responses').payload
    if format == 'chat':
        hint, = observation_hints(request)
    else:
        hint = request['system' if format == 'anthropic' else 'instructions']
    candidates = json.loads(hint.split('Eligible completed calls in this request: ', 1)[1].split('\n[/', 1)[0])
    assert [row['call_id'] for row in candidates] == ['later-call', 'earlier-call']
    assert [json.loads(row['arguments_preview']) for row in candidates] == [later, earlier]
    assert all(row['tool_name'] == 'terminal' and row['arguments_truncated'] is False for row in candidates)

    # A wrong nomination remains the exact selected original, visibly identified.
    n.clients[0].outage = outage
    receipt = n.retain('earlier-call', reason='Retain the detailed inspection outcome.', include_input=True)
    # Bounded delivery may leave the exact durable nomination pending. Its
    # identity must remain truthful in both states, without claiming persistence.
    assert receipt['state'] in {'pending', 'delivered'}, n.diagnostics(receipt)
    assert receipt['accepted'] is (receipt['state'] == 'delivered')
    assert receipt['source_recorded'] is (receipt['state'] == 'delivered')
    if outage:
        assert receipt['state'] == 'pending'
    selected = receipt['selected_call']
    assert selected == {'tool_call_id': 'earlier-call', 'tool_name': 'terminal',
        'message_id': message_id, 'result_sha256': hashlib.sha256(b'first-status').hexdigest(),
        'arguments_preview': json.dumps(earlier, sort_keys=True, separators=(',', ':')),
        'arguments_truncated': False}
    row = next(row for row in n.outbox.snapshot() if row['turn_id'] == receipt['source_id'])
    assert row['state'] == receipt['state']
    observation = row['payload']['observation']
    assert observation['content'] == 'first-status'
    assert observation['native']['tool_call_id'] == 'earlier-call'
    assert observation['input']['arguments'] == earlier
    assert 'arguments_preview' not in observation['native']  # Persisted source contract is unchanged.
    assert n.retain('earlier-call', reason='Another retelling')['selected_call'] == selected


def test_argument_previews_are_explicitly_truncated_inside_total_hint_budget(native):
    n = native
    arguments = {'command': 'inspect ' + 'z' * 200}
    for number in range(10):
        n.complete(f'call-{number}', f'original-{number}', 'terminal', arguments)
    request = n.request(deferred=True).payload
    hint, = observation_hints(request)
    assert len(hint) <= 2048
    candidates = json.loads(hint.split('Eligible completed calls in this request: ', 1)[1].split('\n[/', 1)[0])
    assert 1 <= len(candidates) <= 8
    assert [row['call_id'] for row in candidates] == [f'call-{number}' for number in range(9, 9-len(candidates), -1)]
    assert all(row['arguments_truncated'] and len(row['arguments_preview']) == 128 for row in candidates)
    assert all(f'original-{number}' not in hint for number in range(10))
    receipt = n.retain('call-9')
    assert receipt['source_recorded'] and receipt['selected_call']['arguments_truncated'], n.diagnostics(receipt)
    assert receipt['selected_call']['arguments_preview'] == candidates[0]['arguments_preview']


def test_hint_omits_invented_stale_calls_and_disappears_without_available_tool(native):
    n = native
    n.complete()
    n.messages.extend([{'role': 'assistant', 'tool_calls': [{'id': 'invented', 'function': {
        'name': 'fixture_observe', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'invented', 'content': RESULT}])
    request = n.request().payload
    hint, = observation_hints(request)
    assert 'invented' not in hint and '"call_id": "call-1"' in hint
    # Re-process the actual outgoing layout, including the compacted carrier.
    n.messages[:] = copy.deepcopy(request['messages'])
    no_tools = n.request(tools=False).payload
    assert not any('protagine-observation-candidates-v1' in str(row) for row in no_tools['messages'])
    for row in n.messages:
        if row.get('role') == 'tool' and row.get('tool_call_id') == 'call-1':
            row['content'] = 'Different bytes in the current request.'
    stale = n.request().payload
    assert not any('protagine-observation-candidates-v1' in str(row) for row in stale['messages'])
    assert not n.retain()['accepted']


def test_hint_operation_requires_direct_schema_or_exact_native_catalog_entry(native):
    module = importlib.import_module(native.plugin.__name__ + '.tool_observations')
    tools = [{'name': name} for name in ('tool_search', 'tool_describe', 'tool_call')]
    tools[0]['description'] = 'Some prose mentions protagine_memory_retain_observation.'
    assert module._available_retention({'tools': tools}) is None
    tools[0]['description'] = module._CATALOG_HEADER + '\nother tools (2):\nprotagine_memory_retain_observation, other_tool'
    assert module._available_retention({'tools': tools}) == ('protagine_memory_retain_observation', True)
    assert module._available_retention({'tools': tools, 'tool_choice': 'none'}) is None
    assert module._available_retention({'tools': tools[:-1]}) is None


def test_origin_erasure_removes_observation_and_queued_retry(native):
    n = native
    n.complete()
    n.request()
    result = n.retain()
    assert result['accepted'], n.diagnostics(result)
    assert not observation_hints(n.request('saved-before-erasure').payload)
    row = next(row for row in n.outbox.snapshot() if row['turn_id']==result['source_id'])
    origin = row['payload']['observation']['origin']['source_id']
    erased = n.ledger.erase_sources(contact_id='cid-owner', turn_ids=[origin])
    assert result['source_id'] in erased['affected_source_ids']
    assert 'files_written' not in n.recall().get('body','')
    again = n.retain(request_id='saved-before-erasure')
    assert not again['source_recorded'] and again['state']=='erased', again
    if hasattr(n.db, 'redact_message_payloads'):
        # The instruction span and the observation overlap on the same native
        # result. Both canonical erasures must settle through native replay.
        result = n.context.hooks['on_native_turn_settled'](**n.scope, platform='cli')
        assert result['status'] == 'settled', result
        assert not n.db.search_messages('synchronization', include_inactive=True)


@pytest.mark.parametrize('include_input', [False, True])
def test_native_observation_erasure_uses_exact_result_and_optional_input_rows(native, include_input):
    n = native
    if not hasattr(n.db, 'redact_message_payloads'):
        pytest.skip('selected native runtime lacks owned-copy erasure')
    result_row = n.complete(arguments={'recipe':'copper-input-original'})
    n.request()
    receipt = n.retain(include_input=include_input)
    assert receipt['accepted'], n.diagnostics(receipt)
    before = {row['id']:row for row in n.db.get_messages('native-session')}
    input_row = next(row['id'] for row in before.values() if row['role']=='assistant')
    answer = n.db.append_message('native-session','assistant','The copper result needs investigation.')
    kept_input = n.db.append_message('native-session','user','Keep this separate later task.')
    duplicate_result = n.complete(call_id='later-call', arguments={'recipe':'independent-later-input'})
    kept_before = {row['id']:row for row in n.db.get_messages('native-session') if row['id'] >= kept_input}
    n.ledger.erase_sources(contact_id='cid-owner', turn_ids=[receipt['source_id']])
    result = n.context.hooks['on_native_turn_settled'](**n.scope, platform='cli')
    assert result['status'] == 'settled', result
    after = {row['id']:row for row in n.db.get_messages('native-session')}
    assert after[result_row]['content'] == '[Content removed.]'
    assert after[answer]['content'] == '[Content removed.]'
    assert {key:after[key] for key in kept_before} == kept_before
    assert after[duplicate_result]['content'] == RESULT
    if include_input:
        assert after[input_row]['tool_calls'] == [{'id':'call-1','type':'function',
            'function':{'name':'fixture_observe','arguments':'{}'}}]
        assert not n.db.search_messages('copper-input-original', include_inactive=True)
    else:
        assert after[input_row] == before[input_row]


def test_input_retention_withholds_shared_assistant_call_row(native):
    n = native
    n.complete(arguments={'recipe':'first-input'})
    n.request()
    row = next(row for row in n.db.get_messages('native-session') if row['role']=='assistant')
    calls = [*row['tool_calls'], {'id':'independent-sibling','type':'function',
             'function':{'name':'other_tool','arguments':'{"keep":"sibling-input"}'}}]
    n.db._execute_write(lambda db: db.execute('UPDATE messages SET tool_calls=? WHERE id=?',
                                              (json.dumps(calls), row['id'])))
    result = n.retain(include_input=True)
    assert not result['accepted'] and 'shares a native row' in result['error']
    assert not [item for item in n.outbox.snapshot() if item['turn_id'].startswith('native-observation:')]
    assert n.db.get_messages('native-session')[1]['tool_calls'] == calls


def test_input_changed_after_observation_cannot_gain_new_ownership(native, monkeypatch):
    n = native
    if not hasattr(n.db, 'message_redaction_snapshot'):
        pytest.skip('selected native runtime lacks snapshot formatter')
    n.complete(arguments={'recipe':'observed-original-input'})
    n.request()
    module = importlib.import_module(n.plugin.__name__ + '.tool_observations')
    read = module.native_input
    def intervene(*args, **kwargs):
        original, row = read(*args, **kwargs)
        changed = [{'id':'call-1','type':'function','function':{
            'name':'fixture_observe','arguments':'{"keep":"new-unrelated-input"}'}}]
        n.db._execute_write(lambda db: db.execute('UPDATE messages SET tool_calls=? WHERE id=?',
                                                  (json.dumps(changed), row['_row_id'])))
        return original, row
    monkeypatch.setattr(module, 'native_input', intervene)
    result = n.retain(include_input=True)
    assert not result['accepted'] and 'ownership is unavailable' in result['error']
    assert not [row for row in n.outbox.snapshot() if row['turn_id'].startswith('native-observation:')]
    assert 'new-unrelated-input' in json.dumps(n.db.get_messages('native-session'))


def test_erased_origin_cannot_be_restored_by_pending_delivery(native):
    n = native
    n.complete()
    n.request()
    n.clients[0].outage = True
    pending = n.retain()
    assert pending['state'] == 'pending'
    row = next(row for row in n.outbox.snapshot() if row['turn_id']==pending['source_id'])
    origin = row['payload']['observation']['origin']['source_id']
    n.ledger.erase_sources(contact_id='cid-owner', turn_ids=[origin])
    n.clients[0].outage = False
    n.outbox.drain(lambda stored, timeout_seconds: n.clients[0].sync_turn(
        **stored, outbox=n.outbox, timeout_seconds=timeout_seconds), limit=16, timeout_seconds=.25)
    result = n.retain()
    assert result['state'] == 'erased' and not result['source_recorded']
    assert 'files_written' not in n.recall().get('body','')


def test_call_request_viewer_and_raw_bytes_are_bound(native):
    n = native
    n.complete()
    n.request()
    assert not n.retain(call_id='invented')['accepted']
    assert not n.retain(request_id='different-concurrent-request')['accepted']
    # A second request that no longer contains the result cannot reuse a prior receipt.
    original = n.messages[-1]['content']
    n.messages[-1]['content'] = '{"operation":"copper synchronization","exit_code":0}'
    n.request('api-3')
    assert not n.retain(request_id='api-3')['accepted']
    n.messages[-1]['content'] = original
    result = n.retain()
    assert result['accepted'], n.diagnostics(result)
    row = next(row for row in n.outbox.snapshot() if row['turn_id']==result['source_id'])
    body = {'identity':{'host_id':'hermes'}, 'context':{'session_id':'native-session',
        'contact_id':'cid-owner','turn_id':result['source_id']},'observation':row['payload']['observation']}
    path = '/v2/host/turns/source-observation/'+result['source_id']
    assert n.http.put(path,json=body,headers={'Authorization':'Bearer other'}).status_code == 403
    assert n.http.put(path,json=body,headers={'Authorization':'Bearer reader'}).status_code == 403
    changed = copy.deepcopy(body)
    changed['observation']['content'] += ' altered'
    assert n.http.put(path,json=changed).status_code == 422


def test_another_turn_cannot_nominate_a_call_from_this_turn(native):
    n = native
    n.complete()
    n.request()
    other_context = {**n.scope, 'turn_id':'another-turn', 'task_id':'another-task'}
    n.context.hooks['pre_llm_call'](**other_context, platform='cli',sender_id='owner',
        user_message=INSTRUCTION,conversation_history=n.messages)
    result = json.loads(n.context.middleware['tool_execution'](**other_context,
        api_request_id='api-2',tool_name='protagine_memory_retain_observation',tool_call_id='intruding',
        args={'call_id':'call-1','reason':'reuse'}, next_call=lambda args:
            n.context.tools['protagine_memory_retain_observation']['handler'](args)))
    assert not result['accepted']


@pytest.mark.parametrize('name,result', [('session_search',RESULT),
    ('protagine_memory_retain_observation',RESULT), ('terminal','x'*16385)])
def test_rereads_retention_outputs_and_oversized_results_are_not_candidates(native, name, result):
    n = native
    # session_search requires its own native result reconciliation. Exercise the
    # exact completion observer directly for these excluded producer classes.
    module = importlib.import_module(n.plugin.__name__+'.tool_observations')
    memory = SimpleNamespace(supplied_snapshot=lambda scope: [])
    observer = module.ToolObservations(n.clients[0], n.outbox, memory)
    scope = SimpleNamespace(contact_id='cid-owner',session_id='native-session',task_id='native-task',
        turn_id='native-turn',valid_participant=True,authority_lane='system',platform='cli',user_message=INSTRUCTION)
    observer.completed(scope,{'tool_call_id':'excluded','tool_name':name,'api_request_id':'api-1'},result)
    assert not observer._turns
    request = observer.checked({'messages': [], 'tools': [{'name': 'protagine_memory_retain_observation'}]},
        scope, 'api-2')
    assert 'protagine-observation-candidates-v1' not in str(request)
