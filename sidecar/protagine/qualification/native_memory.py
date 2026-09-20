"""Real source formation followed by fresh native automatic recollection.

This consumer does not assemble or inject memory. The installed Protagine
memory provider and request middleware own that path inside the native worker.
"""
import asyncio
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .cases import _exact_value
from .native import native_cli


class MemoryRouter:
    """Existing supporting-role router plus the explicitly selected native reader."""
    def __init__(self, router, native):
        self._router = router
        self.binding = native.binding
        self.native_config = native.native_config
        self.hermes_python = native.hermes_python

    def __getattr__(self, name):
        return getattr(self._router, name)


async def consume(inputs, context):
    from protagine.beliefs.source_projection import SourceClaimProjection
    from protagine.turns import TurnIdempotencyLedger
    # The existing runner creates intermediate directories under the process
    # umask. The native outbox requires its owned artifact ancestry private too.
    attempts = context.state_dir.parent.parent
    if attempts.name != 'attempts':
        raise ValueError('Native memory requires an owned qualification attempt directory')
    attempts.chmod(0o700)
    state = context.state_dir / 'memory-state'
    state.mkdir(mode=0o700)
    ledger = TurnIdempotencyLedger(state / 'turn-idempotency.db')
    projection = SourceClaimProjection(ledger)
    turns = inputs['turns']
    if not 1 <= len(turns) <= 5 or len({t['id'] for t in turns}) != len(turns):
        raise ValueError('Declare one to five distinct memory sources')
    for turn in turns:
        if not ledger.record_source(turn['id'], contact_id=turn['contact_id'],
            session_id=turn['session_id'], occurred_at=turn['occurred_at'],
            messages=[{'role': 'user', 'content': turn['text']}]):
            raise ValueError('Fixture source was not retained')
        await projection.process_one(context.router)
    contacts = sorted({turn['contact_id'] for turn in turns})
    jobs = [row for contact in contacts for row in projection.status(contact)]
    with closing(ledger._connect()) as db:
        claims_before = [dict(json.loads(row['data_json']), source_id=row['turn_id'],
            claim_id=row['id'], superseded_by=row['superseded_by'], retracted_by=row['retracted_by'])
            for row in db.execute('SELECT * FROM source_claims ORDER BY turn_id,id')]
    erased = None
    if inputs.get('forget_source_ids'):
        erased = ledger.erase_sources(contact_id=inputs['contact_id'], turn_ids=inputs['forget_source_ids'])
    with closing(ledger._connect()) as db:
        claims_after = [dict(json.loads(row['data_json']), source_id=row['turn_id'],
            claim_id=row['id'], superseded_by=row['superseded_by'], retracted_by=row['retracted_by'])
            for row in db.execute('SELECT * FROM source_claims ORDER BY turn_id,id')]
    formation = {'boundary': 'canonical_source_formation_before_native_recollection',
        'jobs': jobs, 'claims_before': claims_before, 'claims_after': claims_after,
        'erasure': erased, 'source_ids': [turn['id'] for turn in turns]}
    context.observe(formation)
    # The model receives only the question. Sources stay in the private ledger;
    # ordinary prefetch obtains and injects them through authenticated host routes.
    async with asyncio.timeout(inputs['native_seconds']):
        result = await native_cli(deepcopy(inputs), context,
            worker=Path(__file__).with_name('native_memory_worker.py'))
    result['effects']['formation'] = formation
    requests = result['effects'].get('request_observations', [])
    returned = {model for row in requests for model in row.get('returned_models', [])}
    if (requests and len(returned) == 1 and all(
            row.get('boundary') == 'httpx_serialized_provider_request'
            and row.get('selected_binding') == context.router.binding
            and row.get('returned_models') and not row.get('response_identity_truncated')
            and 200 <= row.get('status', 0) < 300 for row in requests)):
        # Refine this owned native attempt only after physical provider evidence.
        # A configured label or correct answer alone never earns attribution.
        native = [row for row in context.observations if row.get('boundary') == 'native_cli_loop']
        if len(native) == 1 and native[0].get('outcome') == 'returned':
            native[0].update(selected_binding=context.router.binding, returned_model=next(iter(returned)),
                prior_attempts=[], attribution_basis='serialized_request_and_returned_model',
                observed_weight_revision=None)
    return result


def assess(observed, oracle):
    output = observed.get('output')
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except (ValueError, TypeError):
            output = None
    effects = observed.get('effects', {})
    formation = effects.get('formation', {})
    jobs, before = formation.get('jobs', []), formation.get('claims_before', [])
    requests = [row for row in effects.get('request_observations', [])
                if row.get('boundary') == 'httpx_serialized_provider_request']
    request_text = '\n'.join(row.get('text', '') for row in requests)
    checks = {'correct_final_answer': _exact_value(output, oracle['answer']),
        'all_sources_formed_or_rejected': bool(jobs)
            and {row.get('turn_id') for row in jobs} == set(formation.get('source_ids', []))
            and all(row.get('status') == 'complete' for row in jobs),
        'native_memory_provider_loaded': effects.get('memory_provider_loaded') is True,
        'fresh_native_session': effects.get('fresh_session') is True,
        'automatic_context_route_succeeded': any(row.get('path') == '/v1/host/context/assemble'
            and row.get('status') == 200 for row in effects.get('context_routes', [])),
        'request_evidence_complete': bool(requests) and all(row.get('truncated') is False for row in requests),
        'native_cleanup_confirmed': all(effects.get(key) is True for key in
            ('process_exited', 'worker_stopped', 'agent_close_returned'))}
    for source in oracle['required_source_ids']:
        checks['source_visible.' + source] = ('turn:' + source) in request_text if requests else None
    for term in oracle['forbidden_request_terms']:
        checks['private_or_erased_absent.' + term] = term not in request_text if requests else None
    for source in oracle['no_claim_sources']:
        job = next((row for row in jobs if row.get('turn_id') == source), None)
        checks['junk_not_promoted.' + source] = (not any(row.get('source_id') == source for row in before)
            if job and job.get('status') == 'complete' else None)
    for source, expected in oracle['claim_values'].items():
        matching = [row for row in before if row.get('source_id') == source
                    and not row.get('superseded_by') and not row.get('retracted_by')]
        # Cases use distinctive literal codes or explicit preference terms. The
        # final answer and source visibility are graded independently of this check.
        checks['useful_claim_formed.' + source] = any(all(term.casefold() in
            str(row.get('value', '')).casefold() for term in expected) for row in matching)
    for source in oracle['erased_source_ids']:
        checks['durable_erasure.' + source] = effects.get('erased_sources', {}).get(source) is True
        checks['erased_claims_absent.' + source] = (not any(row.get('source_id') == source
            for row in formation['claims_after']) if 'claims_after' in formation else None)
    for source in oracle.get('retired_source_ids', []):
        matching = [row for row in before if row.get('source_id') == source]
        checks['corrected_claim_retired.' + source] = bool(matching) and all(
            row.get('superseded_by') or row.get('retracted_by') for row in matching)
    return checks


def implementation_identity():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('native_memory.py', 'native_memory_cases.py', 'native_memory_worker.py',
                         'native_memory_identity.py')}


CONSUMERS = {'native_memory': consume}
EVALUATORS = {'native_memory_outcomes': assess}
