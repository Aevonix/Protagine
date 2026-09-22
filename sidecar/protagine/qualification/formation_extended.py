"""Additional real source-formation checks, with fixed supporting review."""
import base64
from contextlib import closing
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import wave

from .memory_cases import _recollect
from .records import CaseSpec, digest, read

VERSION = 'source-formation-inventory-1'


def turn(identity, text, *, at='2026-09-10T12:00:00+00:00', **extra):
    return {'id': identity, 'session_id': 'earlier-'+identity, 'occurred_at': at, 'text': text, **extra}


DEVELOPMENT = {'version': VERSION, 'split': 'development', 'scenarios': [
    {'id': 'selective-property-correction', 'query': 'survey review room badge',
     'turns': [turn('review-details', 'The survey review is in room Cedar. My survey badge code is opal-642.'),
               turn('review-correction', 'Correction: the survey review is in room Maple, not Cedar. The survey badge code is unchanged.',
                    at='2026-09-11T12:00:00+00:00')],
     'expected': [{'source': 'review-correction', 'value_terms': ['Maple'], 'kind': 'personal_context'},
                  {'source': 'review-details', 'value_terms': ['opal-642'], 'kind': 'personal_context'}],
     'retired': {'source': 'review-details', 'value_terms': ['Cedar'], 'by_source': 'review-correction'},
     'recall_terms': ['Maple', 'opal-642']},
    {'id': 'conflict-remains-independent', 'query': 'orchard key location',
     'turns': [turn('key-report-one', 'The orchard key is in the copper drawer.'),
               turn('key-report-two', 'The orchard key is in the slate cabinet.', at='2026-09-11T12:00:00+00:00')],
     'expected': [{'source': 'key-report-one', 'value_terms': ['copper drawer'], 'kind': 'personal_context'},
                  {'source': 'key-report-two', 'value_terms': ['slate cabinet'], 'kind': 'personal_context'}],
     'independent': True, 'recall_terms': ['copper drawer', 'slate cabinet']},
    {'id': 'duplicate-delivery-idempotence', 'query': 'observatory badge code',
     'turns': [turn('observatory-badge', 'My observatory badge code is moss-738.')],
     'expected': [{'source': 'observatory-badge', 'value_terms': ['moss-738'], 'kind': 'personal_context'}],
     'replay': True, 'recall_terms': ['moss-738']},
    {'id': 'explicit-negative-fact', 'query': 'terrace projector mounting',
     'turns': [turn('projector-negative', 'The terrace projector has no ceiling mount. A freestanding support is required for future screenings.')],
     'expected': [{'source': 'projector-negative', 'value_terms': ['no ceiling mount'],
                   'evidence_terms': ['no ceiling mount'], 'kind': 'personal_context'}],
     'max_claims': 2, 'recall_terms': ['no ceiling mount']},
]}


def cases(pack=None):
    pack = deepcopy(DEVELOPMENT if pack is None else read(pack) if not isinstance(pack, dict) else pack)
    if pack.get('version') != VERSION or pack.get('split') not in {'development', 'held_out'}:
        raise ValueError('Unknown source formation pack')
    scenarios = pack.get('scenarios', [])
    if not 1 <= len(scenarios) <= 8 or len({row['id'] for row in scenarios}) != len(scenarios):
        raise ValueError('Provide one to eight distinct formation scenarios')
    result = []
    for row in scenarios:
        turns = row['turns']
        if (not 1 <= len(turns) <= 4 or len({t['id'] for t in turns}) != len(turns)
                or any(not isinstance(t['text'], str) or not 1 <= len(t['text']) <= 2000 for t in turns)):
            raise ValueError('Invalid bounded formation sources')
        result.append(CaseSpec(id='formation.'+row['id'], version='1', role='extraction',
            boundary='cognition_consumer', consumer='source_formation_extended',
            evaluator='source_formation_extended_outcomes', timeout_seconds=360,
            max_output_bytes=524288, target_tasks=('source_claim_extraction',),
            provenance='private' if pack['split'] == 'held_out' else 'public',
            inputs={'contact_id': 'person', 'turns': turns, 'query': row['query'],
                'recall_session': 'new-reader-session', 'now': '2026-09-20T12:00:00+00:00',
                'max_chars': 14000, 'replay': row.get('replay', False)},
            oracle={key: deepcopy(value) for key, value in row.items() if key not in {'turns', 'query'}} |
                   {'source_ids': [t['id'] for t in turns]}))
    return result


def source_message(source):
    if not source.get('audio_transcript'):
        return {'role': 'user', 'content': source['text']}
    # A deterministic synthetic WAV plus supplied fixture transcript exercises
    # storage/provenance only. No recognizer or acoustic accuracy is claimed.
    output = io.BytesIO()
    with wave.open(output, 'wb') as clip:
        clip.setnchannels(1); clip.setsampwidth(2); clip.setframerate(16000)
        clip.writeframes(b'\x00\x00'*1600)
    raw = output.getvalue()
    return {'role': 'user', 'content': [
        {'type': 'input_audio', 'input_audio': {'format': 'wav', 'data': base64.b64encode(raw).decode()}},
        {'type': 'audio_transcript', 'audio_sha256': hashlib.sha256(raw).hexdigest(),
         'segments': [{'start_ms': 0, 'end_ms': 100, 'text': source['text']}],
         'recognizer': {'model_id': 'supplied-synthetic-transcript', 'model_revision': 'fixture-v1'},
         'received_at': source['occurred_at'], 'captured_at': source.get('captured_at')}]}


def state(ledger, contact):
    from protagine.beliefs.source_projection import SourceClaimProjection
    with closing(ledger._connect()) as db:
        claims = [dict(json.loads(row['data_json']), **{k: row[k] for k in (
            'id', 'turn_id', 'message_hash', 'retracted_by', 'superseded_by')})
            for row in db.execute('SELECT * FROM source_claims ORDER BY turn_id,id')]
        sources = [{'id': row['turn_id'], 'session_id': row['session_id'],
            'messages': json.loads(row['messages_json'])} for row in db.execute('SELECT * FROM turn_sources ORDER BY turn_id')]
    return {'claims': claims, 'sources': sources, 'jobs': SourceClaimProjection(ledger).status(contact)}


async def consume(inputs, context):
    from protagine.beliefs.source_projection import SourceClaimProjection
    from protagine.turns import TurnIdempotencyLedger
    ledger = TurnIdempotencyLedger(context.state_dir/'turn-idempotency.db')
    projection = SourceClaimProjection(ledger)
    recorded = []
    for source in inputs['turns']:
        if not ledger.record_source(source['id'], contact_id=inputs['contact_id'], session_id=source['session_id'],
                occurred_at=source['occurred_at'], messages=[source_message(source)]):
            raise ValueError('Fixture source was not newly retained')
        recorded.append(source['id'])
        await projection.process_one(context.router)
        current = next(job for job in projection.status(inputs['contact_id']) if job['turn_id'] == source['id'])
        if current['status'] != 'complete':
            break  # A failed formation attempt remains visible; no retry or later repair.
    before = state(ledger, inputs['contact_id'])
    refs = ledger.source_references(recorded, contact_id=inputs['contact_id'], session_id=inputs['recall_session'])
    replay = None
    if inputs['replay'] and len(recorded) == len(inputs['turns']) and all(job['status'] == 'complete' for job in before['jobs']):
        observations_before = len(context.observations)
        inserts = [ledger.record_source(source['id'], contact_id=inputs['contact_id'], session_id=source['session_id'],
            occurred_at=source['occurred_at'], messages=[source_message(source)]) for source in inputs['turns']]
        processed = await projection.process_one(context.router)
        replay = {'new_inserts': inserts, 'processed_again': processed,
            'extra_model_calls': len(context.observations)-observations_before,
            'durable_state_unchanged': state(ledger, inputs['contact_id']) == before}
    reopened = SourceClaimProjection(TurnIdempotencyLedger(ledger.db_path))
    output = state(reopened.ledger, inputs['contact_id'])
    output['recall'] = _recollect(reopened, inputs['query'], contact=inputs['contact_id'],
        session=inputs['recall_session'], now=inputs['now'], max_chars=inputs['max_chars'])
    after_refs = reopened.ledger.source_references(recorded, contact_id=inputs['contact_id'], session_id=inputs['recall_session'])
    return {'output': output, 'effects': {'source_references': after_refs,
        'source_versions_survive_reopen': refs == after_refs, 'replay': replay,
        'boundary': 'actual_canonical_formation_admission_reopen_lexical_recollection',
        'native_request_and_answer': 'not_exercised', 'semantic_embedding_and_reranking': 'not_exercised',
        'speech_recognition': 'not_exercised; transcript is supplied synthetic fixture data',
        'source_capture': 'canonical_import', 'recall_session': inputs['recall_session']}}


def contains(text, terms):
    return isinstance(text, str) and all(term.casefold() in text.casefold() for term in terms)


def matches(row, wanted):
    return row['turn_id'] == wanted['source'] and contains(row.get('value'), wanted['value_terms']) and (
        'kind' not in wanted or row.get('memory_quality', {}).get('memory_kind') == wanted['kind'])


def assess(observed, oracle):
    from protagine.turns.audio import source_text
    output, effects = observed['output'], observed['effects']
    claims, jobs = output['claims'], output['jobs']
    sources = {row['id']: row for row in output['sources']}
    active = [row for row in claims if not row['retracted_by'] and not row['superseded_by']]
    checks = {'all_sources_retained': set(sources) == set(oracle['source_ids']),
        'first_formation_attempts_complete': {row['turn_id'] for row in jobs} == set(oracle['source_ids']) and all(
            row['status'] == 'complete' and row['attempts'] == 1 for row in jobs),
        'source_versions_survive_reopen': effects['source_versions_survive_reopen'],
        'future_session_recollection': all(row['session_id'] != effects['recall_session'] for row in sources.values()),
        'promoted_quotations_match_source': bool(claims) and all(any(row['evidence'] in source_text(m['content'])
            for m in sources[row['turn_id']]['messages']) for row in claims),
        'useful_source_recollected': contains(output['recall']['body'], oracle['recall_terms']),
        'recollection_has_source_lineage': bool(output['recall']['citations']) and all(
            ref in effects['source_references'] for ref in output['recall']['citations'])}
    for index, wanted in enumerate(oracle['expected']):
        selected = [row for row in active if matches(row, wanted)]
        checks['retained_fact.'+str(index)] = bool(selected)
        for name, field in (('evidence_terms', 'evidence'), ('value_terms', 'value')):
            if wanted.get(name):
                checks[name+'.'+str(index)] = bool(selected) and all(contains(row[field], wanted[name]) for row in selected)
        for field in ('valid_from', 'valid_to', 'event_at'):
            if field in wanted:
                checks[field+'.'+str(index)] = bool(selected) and all(row.get(field) == wanted[field] for row in selected)
        if wanted.get('audio_basis'):
            checks['fallible_audio_lineage.'+str(index)] = bool(selected) and all(
                row.get('epistemic_state') == 'derived_unverified'
                and row.get('source_modality') == 'audio_transcript'
                and row.get('evidence_basis', {}).get('segment', {}).get('recognizer', {}).get('model_id') == 'supplied-synthetic-transcript'
                and row.get('evidence_basis', {}).get('source_version_at_formation')
                and row.get('evidence_basis', {}).get('source_message_hash') for row in selected)
            checks['audio_uncertainty_survives_recall.'+str(index)] = all(term in output['recall']['body']
                for term in ('derived_unverified', 'audio_transcript'))
    if oracle.get('retired'):
        old = [row for row in claims if matches(row, oracle['retired'])]
        replacements = [row for row in active if row['turn_id'] == oracle['retired']['by_source']]
        checks['exact_prior_property_corrected'] = bool(old and replacements) and all(any(
            row['retracted_by'] == newer['id'] and newer.get('prior_claim_id') == row['id']
            and newer['operation'] == 'correct' for newer in replacements) for row in old)
    if oracle.get('independent'):
        checks['conflict_not_silently_superseded'] = len(active) == len(claims) and all(
            row['operation'] == 'assert' and row.get('prior_claim_id') is None for row in claims)
    if oracle.get('replay'):
        replay = effects['replay'] or {}
        checks['replay_adds_no_memory_or_inference'] = bool(replay) and replay.get('new_inserts') == [False]*len(oracle['source_ids']) and (
            replay.get('processed_again') is False and replay.get('extra_model_calls') == 0 and replay.get('durable_state_unchanged') is True)
    if 'max_claims' in oracle:
        checks['no_extra_promotions'] = len(claims) <= oracle['max_claims']
    if oracle.get('forbidden_values'):
        checks['instructions_not_promoted'] = all(not any(term.casefold() in row['value'].casefold()
            for term in oracle['forbidden_values']) for row in claims)
    return checks


def recipe_metadata(suite):
    return {'formation_inventory_version': VERSION, 'formation_cases_sha256': digest([case.record() for case in suite]),
        'formation_implementation': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'boundary': 'canonical_formation_and_lexical_recollection; native_and_recognition_not_exercised'}


CONSUMERS = {'source_formation_extended': consume}
EVALUATORS = {'source_formation_extended_outcomes': assess}
