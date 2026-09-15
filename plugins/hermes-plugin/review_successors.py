"""Bounded failed-proposal successors in the existing native review ledger.

Original experience stays consumed. A successor is another assessment of an
actual failed proposal, never another ordinary observation or an improvement.
"""
import hashlib
import json
import os


VERSION = 'ordinary-review-failure-v1'
MAX_SUCCESSORS = 2


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def append(action, skill, evidence):
    from tools import skill_ledger as ledger
    identifier = ledger.append_entry(action, skill, actor='curator', evidence=evidence)
    if not identifier or ledger.get_entry(identifier) is None:
        raise RuntimeError('Native review disposition was not retained')
    with ledger.ledger_path().open('rb') as stream:
        os.fsync(stream.fileno())
    return identifier


def claim_for(payload, entries):
    """Only code-bound proposals from an actual current review can continue."""
    from .task_review_experience import receipt
    batch = payload.get('_protagine_task_assessment_batch') or payload.get('_protagine_native_failure_batch')
    if not isinstance(batch, dict) or (payload.get('_protagine_task_assessment_batch')
                                     and payload.get('_protagine_native_failure_batch')):
        return None
    try:
        expected = receipt(batch)
    except (KeyError, TypeError):
        return None
    claims = [row for row in entries if row.get('action') == 'ordinary_skill_review'
        and row.get('evidence', {}).get('status') == 'claimed'
        and row['evidence'].get('owner_contact_id')
        and row['evidence'].get('native_execution_id') == payload.get('_protagine_review_native_execution')
        and row['evidence'].get('failure_sha256') == payload.get('_protagine_review_batch_sha256')
        and all(row['evidence'].get(key) == value for key, value in expected.items())]
    return claims[0] if len(claims) == 1 else None


def root_id(claim):
    return claim['evidence'].get('successor', {}).get('root_claim_id', claim['id'])


def stopped(entries, root):
    return any(row.get('action') == 'ordinary_skill_review'
        and row.get('evidence', {}).get('status') == 'successor_stopped'
        and row['evidence'].get('root_claim_id') == root for row in entries)


def stop(claim, reason):
    from tools import skill_ledger as ledger
    root = root_id(claim)
    if not stopped(ledger.list_entries(), root):
        append('ordinary_skill_review', claim.get('skill'), {
            'status': 'successor_stopped', 'root_claim_id': root, 'claim_id': claim['id'],
            'reason': reason, 'quality_credit': False})


def record_failure(payload, *, kind, diagnostic, phase, pending_id=None, evaluation_id=None, measurement=None):
    """Retain exact proposed bytes and typed public feedback, before any removal.

    Hidden oracle answers remain in their original evaluation entry. Only the
    fact of measured non-improvement can become successor feedback.
    """
    from tools import skill_ledger as ledger
    from .review import editable_operation
    entries = ledger.list_entries()
    claim = claim_for(payload, entries)
    operation = editable_operation(payload, allow_create=True)
    if claim is None or operation is None or operation['action'] != 'create':
        return None
    failure = {'version': VERSION, 'status': 'proposal_failed', 'kind': kind,
        'diagnostic': diagnostic, 'phase': phase, 'claim_id': claim['id'],
        'root_claim_id': root_id(claim), 'owner_contact_id': claim['evidence']['owner_contact_id'],
        'pending_id': pending_id, 'payload_sha256': digest(payload),
        'candidate_sha256': hashlib.sha256(operation['content'].encode()).hexdigest(),
        'evaluation_id': evaluation_id, 'candidate_measured': kind == 'not_improved',
        'quality_credit': False, **({'measurement_progress': measurement} if measurement is not None else {})}
    failure['failure_signature'] = digest([kind, phase, diagnostic])
    for row in entries:
        value = row.get('evidence', {})
        if (value.get('version') == VERSION and value.get('claim_id') == claim['id']
                and value.get('payload_sha256') == failure['payload_sha256']
                and value.get('failure_signature') == failure['failure_signature']):
            return row['id']
    raw = json.dumps(payload, sort_keys=True).encode()
    failure['proposal_blob'] = ledger._store_blob(raw)
    if ledger.read_blob(failure['proposal_blob']) != raw:
        raise RuntimeError('Failed native proposal bytes were not retained')
    identifier = append('evaluation', operation['name'], failure)
    if kind == 'validation' and any(row.get('evidence', {}).get('root_claim_id') == root_id(claim)
            and row['evidence'].get('version') == VERSION
            and row['evidence'].get('claim_id') != claim['id']
            and row['evidence'].get('failure_signature') == failure['failure_signature'] for row in entries):
        stop(claim, 'repeated_deterministic_failure')
    return identifier


def candidate_guard(payload):
    from tools import skill_ledger as ledger
    from .review import editable_operation
    entries = ledger.list_entries()
    claim = claim_for(payload, entries)
    if claim is None or not claim['evidence'].get('successor'):
        return None
    if not pending_current(claim, entries):
        return 'The failed proposal was removed or changed; no successor proposal is eligible.'
    if stopped(entries, root_id(claim)):
        return 'This failed-proposal review chain has stopped; no further proposal is eligible.'
    operation = editable_operation(payload, allow_create=True)
    if operation and isinstance(operation.get('content'), str):
        candidate = hashlib.sha256(operation['content'].encode()).hexdigest()
        if any(row.get('evidence', {}).get('version') == VERSION
                and row['evidence'].get('root_claim_id') == root_id(claim)
                and row['evidence'].get('candidate_sha256') == candidate for row in entries):
            stop(claim, 'identical_candidate')
            return 'The proposed bytes already failed in this review chain; no new proposal was staged.'
    return None


def pending_current(claim, entries):
    """Repeat the same rejection check at selection, staging and evaluation."""
    from tools import write_approval
    for row in entries:
        value = row.get('evidence', {})
        if (value.get('version') != VERSION or value.get('root_claim_id') != root_id(claim)
                or not value.get('pending_id')):
            continue
        pending = write_approval.get_pending(write_approval.SKILLS, value['pending_id'])
        if pending is None or digest(pending['payload']) != value['payload_sha256']:
            stop(claim, 'pending_removed_or_changed')
            return False
    return True


def evaluation_current(payload):
    from tools import skill_ledger as ledger
    entries = ledger.list_entries()
    claim = claim_for(payload, entries)
    return claim is None or not claim['evidence'].get('successor') or pending_current(claim, entries)


def settle_ancestors(payload, evaluation_id):
    """Retire only exact failed proposals after actual successor activation."""
    from tools import skill_ledger as ledger, write_approval
    entries = ledger.list_entries()
    claim = claim_for(payload, entries)
    if claim is None or not claim['evidence'].get('successor'):
        return
    for row in entries:
        value = row.get('evidence', {})
        if (value.get('version') != VERSION or value.get('root_claim_id') != root_id(claim)
                or not value.get('pending_id')):
            continue
        pending = write_approval.get_pending(write_approval.SKILLS, value['pending_id'])
        if pending is not None and digest(pending['payload']) == value['payload_sha256']:
            if write_approval.discard_pending(write_approval.SKILLS, value['pending_id']):
                append('ordinary_skill_review', row.get('skill'), {
                    'status': 'failed_proposal_superseded', 'root_claim_id': root_id(claim),
                    'failure_entry_id': row['id'], 'pending_id': value['pending_id'],
                    'evaluation_id': evaluation_id, 'quality_credit': False})


def handled_pending(pending, entries):
    return any(row.get('evidence', {}).get('version') == VERSION
        and row['evidence'].get('pending_id') == pending['id']
        and row['evidence'].get('payload_sha256') == digest(pending['payload']) for row in entries)


def next_successor(entries, *, native, owner, evaluator, connection):
    """Select one linked attempt; ordinary cohort selectors remain unchanged."""
    from tools import skill_ledger as ledger
    from . import task_review_experience as experience
    claims = {row['id']: row for row in entries if row.get('action') == 'ordinary_skill_review'
              and row.get('evidence', {}).get('status') == 'claimed'}
    if any(row['evidence'].get('native_execution_id') == native['id'] for row in claims.values()):
        return None
    latest = {}
    for row in entries:  # Native ledger supplies newest entries first.
        value = row.get('evidence', {})
        if value.get('version') == VERSION:
            latest.setdefault(value['claim_id'], row)
    for parent in reversed(list(claims.values())):
        root = root_id(parent)
        if (stopped(entries, root) or parent['evidence'].get('owner_contact_id') != owner
                or parent['evidence'].get('native_job_id') != native['job_id']):
            continue
        children = [row for row in claims.values() if row['evidence'].get('successor', {}).get('root_claim_id') == root]
        if children and children[0]['id'] != parent['id']:
            continue  # Only the newest linked assessment can advance this root.
        failure = latest.get(parent['id'])
        if failure is None:
            continue
        value = failure['evidence']
        terminal = next((row['evidence'] for row in entries if row.get('action') == 'ordinary_skill_review'
            and row.get('evidence', {}).get('claim_id') == parent['id']
            and row['evidence'].get('status') not in {'claimed', 'successor_stopped'}), None)
        if not terminal:
            continue  # Interrupted claims are not silently re-executed.
        if terminal['status'] == 'proposed' and terminal.get('pending_id') != value['pending_id']:
            continue  # A failed draft followed by a valid proposal needs its evaluation.
        if terminal['status'] not in {'no_proposal', 'proposed'}:
            stop(parent, 'assessment_unavailable')
            continue
        if value['kind'] not in {'validation', 'not_improved'}:
            stop(parent, 'evaluation_unavailable')
            continue
        if len(children) >= MAX_SUCCESSORS:
            stop(parent, 'successor_limit')
            continue
        if not pending_current(parent, entries):
            continue
        raw = ledger.read_blob(value['proposal_blob'])
        if raw is None or hashlib.sha256(raw).hexdigest() != value['payload_sha256']:
            stop(parent, 'failed_proposal_unavailable')
            continue
        payload = json.loads(raw)
        batch = payload.get('_protagine_task_assessment_batch') or payload.get('_protagine_native_failure_batch')
        if batch.get('evaluator') != (evaluator['binding'] if evaluator is not None else None):
            stop(parent, 'evaluator_changed')
            continue
        try:
            current = experience.recheck(batch, connection, owner)
        except ValueError:
            stop(parent, 'source_unavailable')
            continue
        except Exception:
            continue  # An unavailable reader supplies no basis for rewriting.
        if batch['source'] == experience.SOURCE:
            batch = {**batch, 'observations': current, 'skill': None,
                'attribution': experience.ATTRIBUTION, 'recurrence': 'previously_assessed', 'quality_credit': False}
        link = {'root_claim_id': root, 'parent_claim_id': parent['id'],
                'failure_entry_id': failure['id'], 'attempt': len(children) + 1}
        feedback = {'attribution': 'system_recorded_failed_proposal', 'successor': link,
            'proposal': {key: payload[key] for key in ('action', 'name', 'content', 'category', 'operations') if key in payload},
            'failure': {key: value[key] for key in ('kind', 'phase', 'diagnostic', 'candidate_measured')},
            'instruction': 'Assess whether this actual failure supports a changed proposal. '
                'No change is required; do not repeat the failed bytes. Independent evaluation is still required.'}
        return batch, link, feedback
    return None


def finish(claim, result):
    if claim['evidence'].get('successor') and result['status'] != 'proposed':
        from tools import skill_ledger as ledger
        if not any(row.get('evidence', {}).get('version') == VERSION
                and row['evidence'].get('claim_id') == claim['id'] for row in ledger.list_entries()):
            stop(claim, 'no_supported_change' if result['status'] == 'no_proposal' else 'assessment_unavailable')
