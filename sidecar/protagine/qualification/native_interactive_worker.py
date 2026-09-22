"""Controlled scheduling around real native handoff and gateway state transitions."""
import asyncio
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from protagine.qualification.native_memory_worker import prepare as memory_prepare
from protagine.qualification.native_unified_host import host_state, native_gateway
from protagine.qualification.native_worker import main

ROLE = ContextVar('qualification_interactive_role', default='foreground')
TASK = ContextVar('qualification_interactive_task', default=None)


def bounded_text(text):
    text = text if isinstance(text, str) else ''
    return {'text': text[:16384], 'truncated': len(text) > 16384,
            'sha256': hashlib.sha256(text.encode()).hexdigest(), 'bytes': len(text.encode())}


@contextmanager
def prepare(request, state, arguments, config):
    import httpx
    from run_agent import AIAgent
    from protagine.api.routers import host
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore
    from protagine.turns import get_turn_idempotency_ledger
    from gateway.session_context import set_session_vars, clear_session_vars

    inputs = request['inputs']
    scenario = inputs['scenario']
    release, queued_release = threading.Event(), threading.Event()
    entered, wire, conversations = {}, [], []
    errors = []
    gates = {'fail_worker': scenario == 'failed_resume', 'publication_failure': scenario == 'failed_retention'}
    gateway, adapter = None, None

    with ExitStack() as resources:
        resources.callback(release.set)
        resources.callback(queued_release.set)
        contacts = SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'memory-state'/'contacts.db')))

        async def enroll():
            await contacts.connect()
            owner = await contacts.create(display_name='Synthetic shared-work owner', trust_tier='inner_circle')
            for platform, address in [('sms', '+15550007621'), ('whatsapp', '15550007621@s.whatsapp.net')]:
                await contacts.add_handle(owner.contact_id, platform, address, verified=True)
            return owner.contact_id

        inputs['contact_id'] = asyncio.run(enroll())
        resources.callback(lambda: asyncio.run(contacts.close()))

        @contextmanager
        def setup_host(app, owned_state, source_inputs, configuration):
            with host_state(app, owned_state, source_inputs, configuration), patch.object(host, '_contacts_store', contacts):
                keyring = state/'fixture-keyring.json'
                keys = json.loads(keyring.read_text())
                keys['principals'][0].update(turn_ingress_platforms=['sms', 'whatsapp'],
                    scopes=['api:access', 'context:read', 'memory:read', 'turns:write', 'turns:resolve-sender'])
                keyring.write_text(json.dumps(keys))
                configuration['agent']['api_max_retries'] = 0
                yield

        observe_memory = resources.enter_context(memory_prepare(request, state, arguments, config, setup_host=setup_host))
        ledger = get_turn_idempotency_ledger(state/'memory-state')
        gateway_resources = ExitStack()
        resources.callback(gateway_resources.close)
        resources.callback(release.set)
        resources.callback(queued_release.set)

        def start_gateway():
            nonlocal gateway, adapter
            gateway = gateway_resources.enter_context(native_gateway(state))
            adapter = next(value for platform, value in gateway.adapters.items() if platform.value == 'protagine_task')

        start_gateway()
        original_conversation, original_send = AIAgent.run_conversation, httpx.Client.send

        def conversation(agent, *args, **kwargs):
            role = 'worker' if str(agent.platform) == 'protagine_task' else ROLE.get()
            token = ROLE.set(role)
            from protagine_hermes.native_task_platform import ACTIVE
            active = ACTIVE.get() if role == 'worker' else None
            task_token = TASK.set(active.get('id') if active else None)
            identity_tokens = None
            platform = str(agent.platform)
            if platform in {'sms', 'whatsapp'}:
                address = '+15550007621' if platform == 'sms' else '15550007621@s.whatsapp.net'
                identity_tokens = set_session_vars(platform=platform, user_id=address,
                    chat_id='fixture-'+platform, session_id=agent.session_id)
            try:
                response = original_conversation(agent, *args, **kwargs)
                import protagine_hermes
                scope = protagine_hermes._TRANSPORT_SCOPES.for_session(agent.session_id)
                tool_results = [bounded_text(message.get('content')) for message in response.get('messages', [])
                                if message.get('role') == 'tool']
                conversations.append({'role': role, 'session_id': agent.session_id,
                    'platform': platform, 'completed': response.get('completed') is True,
                    'failed': response.get('failed') is True, 'final': bounded_text(response.get('final_response')),
                    'tool_results': tool_results,
                    'scope': {'owner_matches': bool(scope and scope.contact_id == inputs['contact_id']),
                              'valid': bool(scope and scope.valid_participant),
                              'lane': scope.authority_lane if scope else None}})
                return response
            finally:
                if identity_tokens is not None:
                    clear_session_vars(identity_tokens)
                TASK.reset(task_token)
                ROLE.reset(token)

        foreground_count = {'value': 0}

        def send(client, outgoing, *args, **kwargs):
            role = ROLE.get()
            body = None
            if outgoing.method == 'POST' and str(outgoing.url).startswith(arguments['base_url'].rstrip('/')+'/'):
                body = json.loads(outgoing.content)
            if isinstance(body, dict) and 'messages' in body:
                text = json.dumps(body['messages'], ensure_ascii=False)
                source_text = adapter.handoffs.get(TASK.get())['request'] if TASK.get() else text
                task = next((item['name'] for item in inputs['tasks'] if item['marker'] in source_text), None)
                if role == 'worker':
                    entered.setdefault(TASK.get() or 'unknown', threading.Event()).set()
                    if gates['fail_worker']:
                        errors.append({'boundary': 'owned_provider_transport', 'kind': 'declared_read_timeout'})
                        raise httpx.ReadTimeout('Declared fixture provider failure', request=outgoing)
                    if not release.wait(180):
                        raise TimeoutError('Owned background rendezvous expired')
                elif role == 'foreground' and scenario == 'completion_during_foreground':
                    foreground_count['value'] += 1
                    if foreground_count['value'] == 1:
                        release.set()
                        wait_for(lambda: all(row.get('response') for row in adapter.handoffs.recent()), 90)
                wire.append({'role': role, 'task': task, 'text': text,
                             'sha256': hashlib.sha256(text.encode()).hexdigest()})
            return original_send(client, outgoing, *args, **kwargs)

        resources.enter_context(patch.object(AIAgent, 'run_conversation', conversation))
        resources.enter_context(patch.object(httpx.Client, 'send', send))
        if scenario in {'queued_cancel', 'queued_steer'}:
            dispatch = adapter.dispatch_native_event

            async def delayed_dispatch(payload):
                if payload.get('action', 'submit') == 'submit':
                    await asyncio.to_thread(queued_release.wait, 180)
                return await dispatch(payload)

            resources.enter_context(patch.object(adapter, 'dispatch_native_event', delayed_dispatch))
        if scenario == 'failed_retention':
            retain = adapter.handoffs.retain_reply

            def failed_retention(*args, **kwargs):
                if gates['publication_failure']:
                    errors.append({'boundary': 'owned_reply_retention', 'kind': 'declared_write_failure'})
                    raise OSError('Declared fixture reply retention failure')
                return retain(*args, **kwargs)

            resources.enter_context(patch.object(adapter.handoffs, 'retain_reply', failed_retention))

        def run_turn(text, role, identity, platform='cli'):
            extras = {'enabled_toolsets': ['protagine'], 'max_iterations': 8,
                      'session_id': identity, 'platform': platform}
            if platform in {'sms', 'whatsapp'}:
                extras.update(user_id='+15550007621' if platform == 'sms' else '15550007621@s.whatsapp.net',
                              chat_id='fixture-'+platform, chat_type='dm')
            agent = AIAgent(**{**arguments, **extras})
            token = ROLE.set(role)
            try:
                return agent.run_conversation(text, system_message=inputs['system'])
            finally:
                agent.close()
                ROLE.reset(token)

        for index, task in enumerate(inputs['tasks']):
            run_turn(task['bootstrap'], 'bootstrap', 'fixture-submit-'+str(index),
                     'sms' if scenario == 'channel_steer' else 'cli')
        before = adapter.handoffs.recent(contact_id=inputs['contact_id'])
        known_names = {row['id']: next((item['name'] for item in inputs['tasks']
            if item['marker'] in row['request']), 'unknown') for row in before}
        if before and scenario not in {'queued_cancel', 'queued_steer'}:
            wait_for(lambda: len(entered) >= len(inputs['tasks']), 15)

        def finished():
            rows = adapter.handoffs.recent(contact_id=inputs['contact_id'])
            if scenario == 'source_withdrawal' and not rows:
                return not adapter._session_tasks
            return not before or bool(rows) and all(row.get('response') or row.get('terminal') or row.get('stop') for row in rows)

        if scenario in {'completed_result', 'completed_resume', 'gateway_restart', 'failed_retention'}:
            release.set()
            wait_for(finished, 120)
            wait_for(lambda: not adapter._session_tasks, 15)
            if scenario == 'gateway_restart':
                gateway_resources.close()
                start_gateway()
        if scenario == 'failed_resume':
            wait_for(finished, 60)
            wait_for(lambda: not adapter._session_tasks, 15)
            gates['fail_worker'] = False
        if scenario == 'source_withdrawal':
            sources = [ref['source_id'] for row in before for ref in row['source'].get('input_refs', [])]
            ledger.erase_sources(contact_id=inputs['contact_id'], turn_ids=sources)
        for index, text in enumerate(inputs.get('prior_turns', [])):
            run_turn(text, 'prior', 'fixture-prior-'+str(index))
        initial = snapshot(adapter, inputs, known_names)
        arguments.update(enabled_toolsets=['protagine'], max_iterations=8, session_id='fixture-final')
        if scenario == 'channel_steer':
            arguments.update(platform='whatsapp', user_id='15550007621@s.whatsapp.net',
                             chat_id='fixture-whatsapp', chat_type='dm')

        def evidence(agent, response):
            foreground_before_release = not release.is_set() and len(entered) >= len(inputs['tasks'])
            release.set()
            queued_release.set()
            wait_for(finished, 120)
            wait_for(lambda: not adapter._session_tasks, 15)
            final = snapshot(adapter, inputs, known_names)
            memory = observe_memory(agent, response)
            markers = [row['id'] for row in initial] + [item['code'] for item in inputs['tasks']]
            markers += inputs.get('observed_markers', [])
            compact = [{'role': row['role'], 'task': row['task'], 'sha256': row['sha256'],
                        'bytes': len(row['text'].encode()),
                        'markers': [marker for marker in markers if marker in row['text']]} for row in wire]
            for observation in memory.get('request_observations', []):
                text = observation.pop('text', '')
                observation['captured_text_sha256'] = hashlib.sha256(text.encode()).hexdigest()
                observation['captured_text_bytes'] = len(text.encode())
            return {**memory, 'interactive': {'scenario': scenario, 'before': initial, 'after': final,
                'gateway_connected': True, 'foreground_before_release': foreground_before_release,
                'conversations': conversations, 'requests': compact, 'injected_failures': errors,
                'worker_entries': sorted(entered),
                'gateway_restart_performed': scenario == 'gateway_restart',
                'scheduling': 'Owned rendezvous and declared faults; no capacity or physical transport measurement'}}

        try:
            yield evidence
        finally:
            release.set()
            queued_release.set()
    (state/'unified-cleanup.json').write_text(json.dumps({'gateway_stopped': True}))


def wait_for(predicate, seconds):
    until = time.monotonic() + seconds
    while not predicate() and time.monotonic() < until:
        time.sleep(.05)
    return bool(predicate())


def snapshot(adapter, inputs, known_names):
    result = []
    rows = {row['id']: row for row in adapter.handoffs.recent(contact_id=inputs['contact_id'])}
    for identity in known_names:
        rows.setdefault(identity, adapter.handoffs.get(identity))
    for row in rows.values():
        task = known_names.get(row['id']) or next((item['name'] for item in inputs['tasks'] if item['marker'] in row['request']), 'unknown')
        result.append({'name': task, 'id': row['id'], 'native_session_id': row.get('native_session_id'),
            'native_turn_id': row.get('native_turn_id'), 'native_task_id': row.get('native_task_id'),
            'request_sha256': hashlib.sha256(row['request'].encode()).hexdigest(),
            'request_erased': row['request'] == '',
            'source_sha256': hashlib.sha256(json.dumps(row['source'], sort_keys=True).encode()).hexdigest(),
            'owner_matches': row['source']['contact_id'] == inputs['contact_id'],
            'origin_platform': row['source'].get('origin', {}).get('platform'),
            'source_session_id': row['source'].get('source_session_id'),
            'terminal': row.get('terminal'), 'stop': adapter.handoffs.stop_view(row),
            'response': bounded_text(row['response']['text']) if row.get('response') else None,
            'updates': adapter.handoffs.updates(row['id'])})
    return sorted(result, key=lambda row: row['name'])


if __name__ == '__main__':
    raise SystemExit(main(prepare))
