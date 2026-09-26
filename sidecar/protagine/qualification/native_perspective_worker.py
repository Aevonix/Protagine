"""Read existing durable perspective through the actual scoped native adapter."""
import asyncio
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
from unittest.mock import patch


@contextmanager
def prepare(request, state, arguments, config):
    from protagine.qualification.native_memory_worker import prepare as memory_prepare
    from protagine.api.routers import host, social_state
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore
    from protagine.self_model.perspective import SelfPerspective
    from protagine.self_model.store import CompetenceStore, SelfModel
    from protagine.intelligence.components.preference_learner import PreferenceLearner
    from protagine.turns import get_turn_idempotency_ledger
    from gateway.session_context import set_session_vars, clear_session_vars
    inputs = request['inputs']
    owner = inputs['people']['owner']
    viewer = inputs['people'][inputs['viewer']]
    inputs['contact_id'] = owner['id']

    @contextmanager
    def setup_host(app, state, inputs, config):
        with ExitStack() as resources:
            contacts = SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'memory-state'/'contacts.db')))
            asyncio.run(contacts.connect())
            resources.callback(lambda: asyncio.run(contacts.close()))
            ledger = get_turn_idempotency_ledger(state/'memory-state')
            perspective = SelfPerspective(ledger, owner_id=owner['id'])
            preferences = PreferenceLearner(perspective=perspective)
            competence = CompetenceStore()
            resources.callback(competence.close)
            self_model = SelfModel(competence)
            self_model.perspective = perspective
            for name, value in (('_contacts_store', contacts), ('_self_model', self_model),
                                ('_preference_learner', preferences)):
                resources.enter_context(patch.object(host, name, value))
            app.include_router(social_state.router)
            keyring = state/'fixture-keyring.json'
            record = json.loads(keyring.read_text())
            record['principals'][0].update(viewer_person_id=viewer['id'], person_ids=[viewer['id']],
                scopes=['context:read', 'memory:read', 'turns:write', 'turns:resolve-sender']
                    + (['memory:write'] if inputs['viewer'] == 'owner' else []),
                turn_ingress_platforms=['sms'])
            keyring.write_text(json.dumps(record))
            yield

    with ExitStack() as resources:
        observe_memory = resources.enter_context(memory_prepare(request, state, arguments, config, setup_host=setup_host))
        arguments.update(enabled_toolsets=['protagine'] if inputs.get('control') else [],
                         max_iterations=8 if inputs.get('control') else 1)
        if inputs['viewer'] != 'owner':
            arguments.update(platform='sms', user_id=viewer['phone'], chat_id='perspective-fixture', chat_type='dm')
            tokens = set_session_vars(platform='sms', user_id=viewer['phone'], chat_id='perspective-fixture')
            resources.callback(clear_session_vars, tokens)

        def observe(agent, response):
            calls, results = {}, []
            for row in response.get('messages', []):
                for call in row.get('tool_calls') or []:
                    calls[call['id']] = call.get('function', {})
                if row.get('role') == 'tool':
                    results.append({'call': calls.get(row.get('tool_call_id')), 'result': row.get('content')})
            return {**observe_memory(agent, response), 'consumer': 'native_durable_perspective',
                'tool_results': results, 'automatic_opinion_projection': 'unsupported_canonical_only_host',
                'freeform_tone': 'human_rubric_ungraded'}
        yield observe


if __name__ == '__main__':
    from protagine.qualification.native_worker import main
    raise SystemExit(main(prepare))
