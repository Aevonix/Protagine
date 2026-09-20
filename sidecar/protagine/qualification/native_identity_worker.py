"""Actual native identity, contact controls and audience-context observations."""
import asyncio
from contextlib import ExitStack, closing, contextmanager
import json
from string import Template
from unittest.mock import patch
import uuid


@contextmanager
def prepare(request, state, arguments, config):
    from protagine.qualification.native_memory_worker import prepare as memory_prepare
    from protagine.api.routers import host, social_state
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore
    from gateway.session_context import set_session_vars, clear_session_vars
    import httpx
    inputs = request['inputs']
    (state/'memory-state').mkdir(mode=0o700, exist_ok=True)
    contacts = SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'memory-state'/'contacts.db')))
    with ExitStack() as resources:
        asyncio.run(contacts.connect())
        resources.callback(lambda: asyncio.run(contacts.close()))

        async def enroll():
            people = {}
            for index, alias in enumerate(('owner', 'contact', 'other', 'stranger')):
                tier = 'inner_circle' if alias == 'owner' else 'unknown' if alias == 'stranger' else 'regular'
                display = 'Synthetic contact' if inputs.get('same_names') and alias == 'other' else 'Synthetic '+alias
                person = await contacts.create(display_name=display, trust_tier=tier)
                phone, whatsapp = '+1555000950'+str(index), '1555000950'+str(index)+'@s.whatsapp.net'
                verified = not (inputs.get('unverified_sender') and alias == inputs['sender'])
                origin = 'manual' if verified else 'auto:scoped-name'
                await contacts.add_handle(person.contact_id, gateway='sms', address=phone, verified=verified, source=origin)
                await contacts.add_handle(person.contact_id, gateway='whatsapp', address=whatsapp, verified=verified, source=origin)
                people[alias] = {'id': person.contact_id, 'sms': phone, 'whatsapp': whatsapp, 'tier': tier}
            return people

        people = asyncio.run(enroll())
        viewer = people[inputs['sender']]
        inputs['contact_id'], inputs['turns'] = people['owner']['id'], []
        substitutes = {alias+'_id': person['id'] for alias, person in people.items()}
        substitutes['handle'] = people['contact']['whatsapp']
        inputs['messages'][-1]['content'] = Template(inputs['messages'][-1]['content']).substitute(substitutes)
        ledger = None

        @contextmanager
        def setup_host(app, _state, _inputs, _config):
            nonlocal ledger
            from protagine.turns import get_turn_idempotency_ledger
            ledger = get_turn_idempotency_ledger(state/'memory-state')
            for source in inputs['sources']:
                ledger.record_source(source['id'], contact_id=people[source['person']]['id'],
                    session_id='identity-history-'+source['id'],
                    messages=[{'role': 'user', 'content': source['text']}], derive_claims=False)
                inputs['turns'].append({'session_id': 'identity-history-'+source['id']})
            app.include_router(social_state.router)
            keyring = state/'fixture-keyring.json'
            data = json.loads(keyring.read_text())
            data['principals'][0].update(viewer_person_id=viewer['id'], person_ids=[viewer['id']],
                scopes=['context:read', 'memory:read', 'turns:write', 'turns:resolve-sender'],
                turn_ingress_platforms=['sms', 'whatsapp'])
            keyring.write_text(json.dumps(data))
            with patch.object(host, '_contacts_store', contacts):
                yield

        observe_memory = resources.enter_context(memory_prepare(request, state, arguments, config, setup_host=setup_host))
        session_id = 'identity-'+uuid.uuid4().hex
        arguments.update(platform=inputs['platform'], user_id=viewer[inputs['platform']],
            chat_id='identity-shared-room' if inputs['chat_type'] == 'group' else 'identity-dm',
            chat_type=inputs['chat_type'], session_id=session_id,
            enabled_toolsets=['protagine'], max_iterations=inputs['max_iterations'])
        tokens = set_session_vars(platform=arguments['platform'], user_id=arguments['user_id'],
            chat_id=arguments['chat_id'], chat_type=arguments['chat_type'], session_id=session_id)
        resources.callback(clear_session_vars, tokens)

        # A deterministic negative host-boundary probe has no model input or
        # effect. Keep its evidence distinct from model tool decisions.
        probe = {}
        if inputs['check'] == 'stale':
            plugin = config['plugins']['protagine']
            response = httpx.post(plugin['url']+'/v1/host/social/contacts/correct-identity',
                headers={'Authorization': 'Bearer '+plugin['api_key']}, timeout=5, json={
                    'contact_id': people['owner']['id'], 'operation_id': 'stale-preimage-probe',
                    'gateway': 'whatsapp', 'address': people['contact']['whatsapp'],
                    'expected_contact_id': people['other']['id'], 'subject_id': people['stranger']['id'],
                    'evidence_refs': ['controlled-negative-probe']})
            probe = {'boundary': 'actual_scoped_host_preimage_check', 'status': response.status_code}

        def observe(agent, response):
            import protagine_hermes
            scope = protagine_hermes._TRANSPORT_SCOPES.for_session(agent.session_id)
            memory = observe_memory(agent, response)
            calls, results = {}, []
            for row in response.get('messages', []):
                for call in row.get('tool_calls') or []:
                    calls[call['id']] = call.get('function', {})
                if row.get('role') == 'tool':
                    results.append({'call': calls.get(row.get('tool_call_id')), 'result': row.get('content')})
            resolved = asyncio.run(contacts.resolve_handle('whatsapp', people['contact']['whatsapp']))
            verified = asyncio.run(contacts.resolve_verified_handles('whatsapp', [people['contact']['whatsapp']]))
            tiers = {alias: asyncio.run(contacts.get(person['id'])).trust_tier for alias, person in people.items()}
            with closing(ledger._connect()) as db:
                source_owners = {row['turn_id']: row['contact_id'] for row in db.execute('SELECT turn_id,contact_id FROM turn_sources')}
            return {**memory, 'consumer': 'native_identity_and_audience', 'tool_results': results,
                'native_audience': {'platform': arguments['platform'], 'chat_type': arguments['chat_type'],
                    'chat_id': arguments['chat_id'], 'sender_alias': inputs['sender']},
                'scope': {'lane': scope.authority_lane if scope else None,
                    'valid_participant': bool(scope and scope.valid_participant),
                    'contact_matches_sender': bool(scope and scope.contact_id == viewer['id']),
                    'resolution': scope.resolution_status if scope else None},
                'current_handle_person': next((alias for alias, person in people.items()
                    if resolved and resolved.contact_id == person['id']), None),
                'verified_handle_person': next((alias for alias, person in people.items()
                    if verified and verified.contact_id == person['id']), None),
                'source_owner_aliases': {source['id']: next((alias for alias, person in people.items()
                    if source_owners.get(source['id']) == person['id']), None) for source in inputs['sources']},
                'source_ownership_before': {source['id']: source['person'] for source in inputs['sources']},
                'trust_tiers_unchanged': all(tiers[alias] == person['tier'] for alias, person in people.items()),
                'host_preimage_probe': probe, 'network_channel_delivery': 'not_exercised',
                'source_capture': 'canonical_import', 'formation': 'not_exercised'}
        yield observe


if __name__ == '__main__':
    from protagine.qualification.native_worker import main
    raise SystemExit(main(prepare))
