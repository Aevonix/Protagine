"""Attributed host reports of machine assessments already retained in the source ledger.

Hashes verified received bytes, not file origin, review correctness or owner
approval. New assessments are no longer admitted (their only consumer, the old
judgment pass, was replaced by the opinion store); recall and the owner read
route keep interpreting the retained ones.
"""
from contextlib import closing
import json

from protagine.turns.idempotency import canonical_turn_digest, source_message_hash

VERSION = 'task-artifact-assessment-v1'
ATTRIBUTION = 'host_reported_machine_assessment_unverified'


def quotation_metadata(message):
    """Identify an admitted review bundle even when recall selects an interior chunk."""
    if message.get('role') != 'assistant' or message.get('_task_artifact_assessment') != VERSION:
        return {}
    return {'assessment_context': {
        'kind': VERSION, 'attribution': ATTRIBUTION, 'owner_approval': 'unobserved',
        'interpretation': 'This source bundles a reviewed artifact, a machine assessment and supplied review context. '
            'An excerpt may quote the reviewed artifact rather than the assessment conclusion. '
            'Open the complete source before treating it as a review verdict or recommendation.'}}


def _current(conn, refs, *, contact_id, session_id, annotation_checks=(), input_refs=()):
    from protagine.turns.source_annotations import current_candidates_in_connection
    expected = {(ref['source_id'], ref['source_version']) for ref in refs}
    membership, candidates = {}, []
    for check in annotation_checks:
        # An applicable correction still withholds a current assessment. Exact
        # membership only distinguishes an unrelated annotated sibling; it is
        # host-reported provenance, never inferred from the quoted artifact.
        if check['annotation_ids'] or set(check['message_hashes']) != {
                ref['source_id'] for ref in check['source_refs']}:
            return False
        for ref in check['source_refs']:
            hashes = check['message_hashes'][ref['source_id']]
            if not hashes or (ref['source_id'], ref['source_version']) not in expected:
                return False
            membership.setdefault(ref['source_id'], set()).update(hashes)
        candidates.append({'_annotation_source_refs': check['source_refs'],
            '_annotation_message_hashes': check['message_hashes'], '_annotation_ids': []})
    for ref in refs:
        row = conn.execute('''SELECT session_id,messages_json FROM turn_sources s WHERE turn_id=?
            AND contact_id=? AND (scope='person' OR session_id=?)
            AND NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=s.turn_id)
            AND NOT EXISTS (SELECT 1 FROM source_projection_erasures e WHERE e.turn_id=s.turn_id)''',
            (ref['source_id'], contact_id, session_id)).fetchone()
        if row is None or canonical_turn_digest(json.loads(row['messages_json'])) != ref['source_version']:
            return False
        if ref['source_id'] in membership:
            retained = {source_message_hash(row['session_id'], message)
                for message in json.loads(row['messages_json'])}
            if not membership[ref['source_id']] <= retained:
                return False
        elif conn.execute('SELECT 1 FROM source_annotations WHERE target_source_id=?',
                          (ref['source_id'],)).fetchone():
            return False
    # A host cannot select an unrelated sibling instead of the execution's
    # immutable admitted input, even if that sibling is a valid message.
    if any(ref['source_id'] in membership and ref['input_message_hash'] not in membership[ref['source_id']]
           for ref in input_refs):
        return False
    return len(current_candidates_in_connection(conn, candidates,
        contact_id=contact_id, session_id=session_id)) == len(candidates)


def supported(conn, *, message, source_id, contact_id, session_id):
    """The assessment and every supplied revision must still be current."""
    if (message.get('_task_artifact_assessment') != VERSION or not message.get('_supplied_inputs')
            or not message.get('_supplied_sources')):
        return False
    if conn.execute('SELECT 1 FROM source_annotations WHERE target_source_id=?', (source_id,)).fetchone():
        return False
    return _current(conn, message['_supplied_sources'], contact_id=contact_id, session_id=session_id,
        annotation_checks=message.get('_assessment_annotation_checks', ()),
        input_refs=message['_supplied_inputs'])


def read_assessments(registry, *, contact_id, source_refs=(), offset=0, limit=16):
    """Complete current review bundles, not inferred failure labels or verdicts.

    The second line is the JSON facts frame written at admission, not reviewer
    prose. Reading its task identity avoids another projection or migration.
    Exact-ref reads also provide the existing consumer's freshness boundary.
    """
    expected = {ref['source_id']: ref['source_version'] for ref in source_refs}
    with closing(registry.ledger._connect()) as conn:
        clauses, args = ["contact_id=?", "scope='person'",
            "json_extract(messages_json,'$[0]._task_artifact_assessment')=?",
            "NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=turn_sources.turn_id)",
            "NOT EXISTS (SELECT 1 FROM source_projection_erasures e WHERE e.turn_id=turn_sources.turn_id)"], [contact_id, VERSION]
        if expected:
            clauses.append('turn_id IN (SELECT value FROM json_each(?))')
            args.append(json.dumps(list(expected)))
        rows = conn.execute('SELECT turn_id,session_id,messages_json FROM turn_sources WHERE '
            + ' AND '.join(clauses) + ' ORDER BY ingested_at DESC,turn_id LIMIT ? OFFSET ?',
            [*args, len(expected) if expected else limit, 0 if expected else offset]).fetchall()
        records = []
        for row in rows:
            messages = json.loads(row['messages_json'])
            if len(messages) != 1:
                continue
            message = messages[0]
            version = canonical_turn_digest(messages)
            if ((expected and expected.get(row['turn_id']) != version)
                    or not supported(conn, message=message, source_id=row['turn_id'],
                                     contact_id=contact_id, session_id=row['session_id'])):
                continue
            # This is an internal source format, never a parser for findings.
            facts = json.loads(message['content'].split('\n', 2)[1])
            if facts.get('version') != VERSION:
                continue
            records.append({'source_id': row['turn_id'], 'source_version': version,
                **{key: facts[key] for key in ('task_id', 'execution_id', 'assessed_at',
                                               'reviewer_identity', 'reviewer_model')},
                'attribution': ATTRIBUTION, 'owner_approval': 'unobserved',
                'content': message['content'], 'complete': True})
    return {'assessments': records,
        'sources_current': not expected or expected == {
            row['source_id']: row['source_version'] for row in records},
        'next_offset': offset + len(rows) if not expected and len(rows) == limit else None}
