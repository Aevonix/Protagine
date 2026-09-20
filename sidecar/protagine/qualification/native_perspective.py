"""Actual durable opinions/appraisals followed by scoped native inspection."""
import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from .native import native_cli
from .native_memory import MemoryRouter
from .records import digest


def snapshot(ledger, owner, subject, clock=time.time):
    from protagine.self_model.judgments import SelfJudgments
    from protagine.self_model.appraisals import AppraisalStore
    judgments = SelfJudgments(ledger, owner_id=owner, clock=clock)
    appraisals = AppraisalStore(ledger, owner_id=owner, clock=clock)
    with closing(ledger._connect()) as db:
        appraisal_jobs = [dict(row) for row in db.execute('SELECT * FROM appraisal_runs')]
    return {'judgments': judgments.revisions(), 'judgment_history': judgments.revisions(history=True),
        'judgment_jobs': judgments.processing(), 'appraisals': appraisals.view(subject, viewer_contact_id=owner, history=True),
        'appraisal_current': appraisals.view(subject, viewer_contact_id=owner), 'appraisal_jobs': appraisal_jobs}


async def consume(inputs, context):
    from protagine.beliefs.source_projection import SourceClaimProjection
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore
    from protagine.self_model.judgments import SelfJudgments
    from protagine.self_model.appraisals import AppraisalStore
    from protagine.turns import TurnIdempotencyLedger
    from .native_memory_batch import preflight_output
    context.state_dir.parent.parent.chmod(0o700)
    preflight_output(context.state_dir)
    state = context.state_dir/'memory-state'; state.mkdir(mode=0o700)
    contacts = SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'contacts.db')))
    people = {}
    await contacts.connect()
    try:
        for index, alias in enumerate(('owner', 'subject', 'bystander')):
            person = await contacts.create(display_name='Synthetic '+alias,
                trust_tier='inner_circle' if alias == 'owner' else 'regular')
            phone = '+1555000940'+str(index)
            await contacts.add_handle(person.contact_id, gateway='sms', address=phone, verified=True)
            people[alias] = {'id': person.contact_id, 'phone': phone, 'trust_tier': person.trust_tier}
    finally:
        await contacts.close()
    ledger = TurnIdempotencyLedger(state/'turn-idempotency.db')
    owner, subject = people['owner']['id'], people[inputs['subject']]['id']
    now = [time.time() - inputs.get('clock_origin_age_seconds', 30)]
    clock = lambda: now[0]
    snapshots = []
    with patch.dict(os.environ, {'PROTAGINE_SELF_JUDGMENTS_ENABLED': '1', 'PROTAGINE_OWNER_CONTACT_ID': owner}):
        judgment = SelfJudgments(ledger, owner_id=owner, clock=clock)
        appraisal = AppraisalStore(ledger, owner_id=owner, clock=clock)
        projection = SourceClaimProjection(ledger)
        origin = now[0]
        for episode in inputs['episodes']:
            now[0] = origin + episode['after_seconds']
            ledger.record_source(episode['id'], contact_id=subject, session_id='history-'+episode['id'],
                occurred_at=datetime.fromtimestamp(now[0], timezone.utc).isoformat(),
                messages=[{'role': 'user', 'content': episode['text']}])
            if inputs['mechanism'] == 'judgment':
                await projection.process_one(context.router)
                await judgment.process_one(context.router)
            else:
                await appraisal.process_one(context.router)
            snapshots.append(snapshot(ledger, owner, subject, clock))
    native_inputs = {**deepcopy(inputs), 'role': 'chat', 'people': people,
        'turns': [{'session_id': 'history-'+episode['id']} for episode in inputs['episodes']]}
    async with asyncio.timeout(inputs['native_seconds']):
        native = await native_cli(native_inputs, context, worker=Path(__file__).with_name('native_perspective_worker.py'))
    # Read a newly constructed store after native shutdown. No state is inferred
    # from the assistant's prose or a tool's asserted success flag.
    reopened = TurnIdempotencyLedger(state/'turn-idempotency.db')
    after = snapshot(reopened, owner, subject)
    store = SQLiteContactStore(ContactsConfig(sqlite_path=str(state/'contacts.db')))
    await store.connect()
    try:
        tiers = {alias: (await store.get(person['id'])).trust_tier for alias, person in people.items()}
    finally:
        await store.close()
    native['effects'].update(state_snapshots=snapshots, state_after_native=after,
        producer_observations=[{key: row.get(key) for key in ('task', 'selected_binding', 'returned_model',
            'configured_model', 'outcome', 'prior_attempts')} for row in context.observations
            if row.get('boundary') == 'router_complete' and row.get('binding_purpose') == 'target'],
        contact_tiers_unchanged=all(tiers[alias] == person['trust_tier'] for alias, person in people.items()),
        clock_basis='injected_logical_clock_for_reducer_interval; not_elapsed_wall_time',
        source_ids=[row['id'] for row in inputs['episodes']],
        source_capture='canonical_import; native_writer_not_exercised')
    return native


def _calls(effects, operation):
    matches = []
    for row in effects.get('tool_results', []):
        function = row.get('call') or {}
        try:
            args = json.loads(function.get('arguments', '{}'))
            value = json.loads(row.get('result', '{}'))
        except (ValueError, TypeError):
            continue
        name = function.get('name')
        if name == 'tool_call' and len(args.get('calls', [])) == 1:
            name, args = args['calls'][0].get('name'), args['calls'][0].get('arguments', {})
        if name == 'protagine_judgments' and args.get('operation') == operation and not value.get('error'):
            matches.append(value)
    return matches


def assess(observed, oracle):
    effects = observed.get('effects', {})
    states = effects.get('state_snapshots', [])
    first, last = (states[0], states[-1]) if states else ({}, {})
    after = effects.get('state_after_native', {})
    wire = '\n'.join(row.get('text', '') for row in effects.get('request_observations', []))
    checks = {'durable_state_observed': len(states) == oracle['episodes'],
        'contact_permissions_unchanged': effects.get('contact_tiers_unchanged') is True,
        'fresh_native_session': effects.get('fresh_session') is True,
        'automatic_context_route_succeeded': any(row.get('path') == '/v1/host/context/assemble'
            and row.get('status') == 200 for row in effects.get('context_routes', [])),
        'native_read_completed': bool(effects.get('request_observations')) and all(
            row.get('truncated') is False for row in effects['request_observations']),
        'native_cleanup_confirmed': all(effects.get(key) is True for key in ('worker_stopped', 'process_exited', 'agent_close_returned'))}
    kind = oracle['check']
    if kind in {'stable', 'revise', 'bounded', 'withdraw', 'portable'}:
        initial, final = first.get('judgments', []), last.get('judgments', [])
        checks['initial_evidence_backed_opinion'] = bool(initial) and all(row.get('support') for row in initial)
        inspected = [item for response in _calls(effects, 'inspect') for item in response.get('judgments', [])]
        checks['native_explicit_inspection'] = bool(final) and any(
            item.get('id') == final[0]['id'] and item.get('stance') == final[0].get('stance') for item in inspected)
        if kind in {'stable', 'bounded'}:
            checks['existing_opinion_preserved'] = bool(initial) and final == initial
        if kind == 'stable':
            checks['flattery_supplied_no_new_premise'] = any(row.get('turn_id') == oracle['last_source']
                and row.get('status') == 'complete' and row.get('disposition') == 'unsupported_source'
                for row in last.get('judgment_jobs', []))
        if kind == 'revise':
            checks['new_revision_links_predecessor'] = bool(initial and final) and final[0].get('supersedes') == initial[0]['id']
            checks['contrary_evidence_preserved'] = bool(final) and bool(final[0].get('contrary'))
            checks['new_evidence_supports_revision'] = bool(final) and any(ref.get('turn_id') == oracle['last_source'] for ref in final[0]['support'])
        if kind == 'bounded':
            checks['default_rate_limit_observed'] = any(row.get('disposition') == 'topic_rate_limited' for row in last.get('judgment_jobs', []))
        if kind == 'withdraw':
            checks['native_owner_withdrawal_observed'] = any(row.get('accepted') is True for row in _calls(effects, 'withdraw'))
            checks['withdrawal_survives_store_reopen'] = not after.get('judgments') and any(row.get('status') == 'withdrawn'
                and row.get('owner_correction', {}).get('control_turn_id') for row in after.get('judgment_history', []))
        if kind == 'portable':
            producers = effects.get('producer_observations', [])
            produced = {row.get('returned_model') for row in producers}
            served = {model for request in effects.get('request_observations', []) for model in request.get('returned_models', [])}
            normalize = lambda s: s.removeprefix('openai/') if isinstance(s, str) else s
            checks['distinct_processor_identity_observed'] = bool(produced and served) and None not in produced and all(
                row.get('outcome') == 'returned' and row.get('prior_attempts') == [] for row in producers
            ) and all(not row.get('response_identity_truncated') for row in effects.get('request_observations', [])) and {
                normalize(m) for m in produced}.isdisjoint({normalize(m) for m in served})
    else:
        initial = first.get('appraisal_current', {}).get('records', [])
        checks['source_backed_appraisal_formed'] = bool(initial) and all(row.get('sources') for row in initial)
        if kind == 'repair':
            checks['incident_settled_with_receipt'] = any(row.get('status') == 'settled' and row.get('supersedes')
                for row in last.get('appraisals', {}).get('records', []))
            checks['settled_hint_removed'] = last.get('appraisal_current', {}).get('behavior_hints') == []
            checks['settled_appraisal_not_injected'] = all(row.get('text', '') not in wire
                for row in initial if row.get('text'))
        if kind == 'separation':
            checks['other_contact_appraisal_absent'] = all(row.get('text', '') not in wire for row in initial if row.get('text'))
            checks['other_contact_sources_absent'] = all('turn:'+source not in wire for source in effects.get('source_ids', []))
        if kind == 'appraisal-withdraw':
            initial_wire = (effects.get('request_observations') or [{}])[0].get('text', '')
            checks['active_appraisal_automatically_injected'] = bool(initial) and all(
                row.get('text') and row['text'] in initial_wire for row in initial)
            checks['native_appraisal_withdrawal_observed'] = any(row.get('accepted') is True for row in _calls(effects, 'withdraw'))
            checks['appraisal_withdrawal_persisted'] = not after.get('appraisal_current', {}).get('records') and bool(after.get('appraisals', {}).get('corrections'))
    return checks


def recipe_metadata(cases):
    return {'consumer': 'durable_perspective_then_native_inspection',
        'perspective_cases_sha256': digest([case.record() for case in cases]),
        'perspective_implementation': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('native_perspective.py', 'native_perspective_worker.py', 'native_perspective_cases.py')},
        'automatic_opinion_projection': 'unsupported_canonical_only_host',
        'freeform_tone': 'human_rubric_ungraded'}


CONSUMERS = {'native_perspective': consume}
EVALUATORS = {'native_perspective_outcomes': assess}
