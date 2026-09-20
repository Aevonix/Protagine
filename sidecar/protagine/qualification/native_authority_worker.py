"""Verified fixture senders, real scoped host auth and native tool guards."""
import asyncio
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch


@contextmanager
def prepare(request, state, arguments, config):
    from protagine.qualification.coding_worker import prepare as coding_prepare
    from protagine.qualification.native_memory_worker import prepare as memory_prepare
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore
    from protagine.api.routers import host
    from gateway.session_context import set_session_vars, clear_session_vars
    import httpx

    inputs = request['inputs']
    with ExitStack() as resources:
        # Container startup removes forwarded credentials. Restore the scoped
        # Protagine environment only after that sandbox is established.
        coding_observe = resources.enter_context(coding_prepare(request, state, arguments, config))
        (state / 'memory-state').mkdir(mode=0o700, exist_ok=True)
        contacts = SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'memory-state'/'contacts.db')))

        async def enroll():
            await contacts.connect()
            people = {}
            for index, (name, tier) in enumerate((('owner', 'inner_circle'), ('contact', 'regular'),
                                                ('other', 'regular'), ('stranger', 'unknown'))):
                person = await contacts.create(display_name='Synthetic ' + name, trust_tier=tier)
                phone = '+1555000820' + str(index)
                await contacts.add_handle(person.contact_id, gateway='sms', address=phone, verified=True)
                people[name] = {'id': person.contact_id, 'phone': phone}
            return people

        people = asyncio.run(enroll())
        resources.callback(lambda: asyncio.run(contacts.close()))
        active = people[inputs['sender']]
        inputs['contact_id'] = people['owner']['id']
        inputs['turns'] = []
        grant_probe = {}

        @contextmanager
        def setup_host(app, _state, _inputs, _config):
            from protagine.turns import get_turn_idempotency_ledger
            ledger = get_turn_idempotency_ledger(state/'memory-state')
            for source in inputs['sources']:
                person = people[source['person']]['id']
                ledger.record_source(source['id'], contact_id=person, session_id='prior-'+source['id'],
                    messages=[{'role': 'user', 'content': source['text']}], derive_claims=False)
                inputs['turns'].append({'id': source['id'], 'session_id': 'prior-'+source['id']})
            keyring = state/'fixture-keyring.json'
            data = json.loads(keyring.read_text())
            principal = data['principals'][0]
            principal.update(principal='benchmark-'+inputs['sender'], viewer_person_id=active['id'],
                person_ids=[active['id']], turn_ingress_platforms=['sms'],
                scopes=['context:read', 'memory:read', 'turns:write', 'turns:resolve-sender'])
            keyring.write_text(json.dumps(data))
            with patch.object(host, '_contacts_store', contacts):
                yield

        memory_observe = resources.enter_context(memory_prepare(request, state, arguments, config, setup_host=setup_host))
        # A real transport identity, never an authority declaration in a prompt.
        arguments.update(platform='sms', user_id=active['phone'], chat_id='authority-fixture-chat',
            chat_type='dm', enabled_toolsets=['file', 'protagine'], max_iterations=inputs['max_iterations'])
        tokens = set_session_vars(platform='sms', user_id=active['phone'], chat_id='authority-fixture-chat',
                                  session_id=arguments['session_id'])
        resources.callback(clear_session_vars, tokens)
        if inputs['sender'] != 'owner':
            # Separate deterministic host-boundary probe. This is not attributed
            # to model judgment and its response is never put into model context.
            plugin = config['plugins']['protagine']
            reply = httpx.get(plugin['url']+'/v1/host/contacts/resolve',
                params={'gateway': 'sms', 'address': people['owner']['phone'], 'create': 'false'},
                headers={'Authorization': 'Bearer '+plugin['api_key']}, timeout=5)
            grant_probe = {'boundary': 'authenticated_host_sender_resolution',
                'attempted_other_person': True, 'status': reply.status_code,
                'owner_identifier_disclosed': people['owner']['id'] in reply.text}

        def observe(agent, response):
            import protagine_hermes
            memory, coding = memory_observe(agent, response), coding_observe(agent, response)
            scope = protagine_hermes._TRANSPORT_SCOPES.for_session(agent.session_id)
            calls, results = {}, []
            for message in response.get('messages', []):
                for call in message.get('tool_calls') or []:
                    calls[call.get('id')] = call.get('function', {}).get('name')
                if message.get('role') == 'tool':
                    results.append({'name': calls.get(message.get('tool_call_id')),
                                    'result': message.get('content')})
            return {**memory, **coding, 'consumer': 'native_verified_sender_authority',
                'scope': {'lane': scope.authority_lane if scope else None,
                    'valid_participant': bool(scope and scope.valid_participant),
                    'contact_matches_sender': bool(scope and scope.contact_id == active['id']),
                    'platform': scope.platform if scope else None,
                    'resolution': scope.resolution_status if scope else None},
                'host_grant_probe': grant_probe, 'tool_results': results,
                'synthetic_sources_seeded': [source['id'] for source in inputs['sources']],
                'formation': 'not_exercised', 'channel_network_delivery': 'not_exercised'}

        yield observe


if __name__ == '__main__':
    from protagine.qualification.coding_sandbox import terminal_configuration
    from protagine.qualification.native_worker import main
    state = Path(sys.argv[1]).parent
    request = json.loads(Path(sys.argv[1]).read_text())
    config = json.loads((state/'config.yaml').read_text())
    config['terminal'] = terminal_configuration(request['inputs']['sandbox'])
    (state/'config.yaml').write_text(json.dumps(config))
    raise SystemExit(main(prepare))
