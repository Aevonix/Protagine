"""Canonical task reviews on the existing ordinary-review and skill-evaluation path.

Reviews remain unverified reports. Two task identities invite an assessment of
recurrence, not an assertion that a failure or useful fix has been established.
The caller supplies an evaluator declaration; model output cannot choose code.
"""
import hashlib
import importlib
import json
import os
from pathlib import Path

SOURCE = 'ordinary_task_assessment_batch'
ATTRIBUTION = 'host_reported_machine_assessment_unverified'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def declaration(path):
    """Read operator-selected scope, callable and frozen inputs, without inference."""
    path = Path(path).resolve(strict=True)
    value = json.loads(path.read_bytes())
    if (set(value) != {'id', 'scope', 'oracle', 'oracle_id', 'environment', 'allow_apply'}
            or not all(isinstance(value[k], str) and value[k].strip()
                       for k in ('id', 'scope', 'oracle', 'oracle_id'))
            or type(value['allow_apply']) is not bool or not isinstance(value['environment'], dict)
            or any(not isinstance(k, str) or not k or '=' in k or '\x00' in k
                   or k in {'HOME', 'home', 'CODEX_HOME'} or not isinstance(v, str)
                   for k, v in value['environment'].items())):
        raise ValueError('Invalid operator task-review evaluator declaration')
    module, function = value['oracle'].split(':')
    if not module or not function:
        raise ValueError('Select an existing evaluator module:function')
    # The existing oracle owns its frozen recipe and runtime checks. This local
    # configuration binds scope and invocation; it adds no second input manifest.
    return {'path': str(path), 'value': value, 'binding': {
        'id': value['id'], 'scope': value['scope'], 'declaration_sha256': digest(value)}}


def read(connection, owner, *, refs=(), offset=0):
    response = connection.post('/v1/host/executions/assessments/read', json={
        'contact_id': owner, 'source_refs': list(refs), 'offset': offset, 'limit': 16}, timeout=5)
    response.raise_for_status()
    return response.json()


def selected_batch(entries, evaluator, connection, owner):
    claims = [r['evidence'] for r in entries if r.get('action') == 'ordinary_skill_review'
              and r.get('evidence', {}).get('source') == SOURCE]
    consumed_tasks = {i for row in claims for i in row['task_ids']}
    consumed_executions = {i for row in claims for i in row['execution_ids']}
    records, tasks, executions, seen_refs, offset = [], set(), set(), set(), 0
    # Bounded observation of recent sources; no new cursor, queue or copied store.
    for _ in range(4):
        page = read(connection, owner, offset=offset)
        for row in page['assessments']:
            reference = (row['source_id'], row['source_version'])
            if (row['task_id'] in consumed_tasks or row['execution_id'] in consumed_executions
                    or reference in seen_refs or row['complete'] is not True
                    or row['attribution'] != ATTRIBUTION):
                continue
            if row['task_id'] not in tasks and len(tasks) == 2:
                continue
            if row['execution_id'] in executions and row['task_id'] not in tasks:
                continue
            records.append(row)
            seen_refs.add(reference)
            tasks.add(row['task_id']); executions.add(row['execution_id'])
        if len(tasks) == 2 or page['next_offset'] is None:
            break
        offset = page['next_offset']
    if len(tasks) < 2 or len(executions) < 2:
        return None
    # Never clip off a review conclusion or silently split a quoted bundle.
    if len(records) > 16 or sum(len(r['content']) for r in records) > 64000:
        raise ValueError('Complete task-review batch exceeds its input bound')
    refs = [{'source_id': r['source_id'], 'source_version': r['source_version']} for r in records]
    ids = sorted(digest(ref) for ref in refs)
    return {'source': SOURCE, 'attribution': ATTRIBUTION, 'skill': None,
        'observations': records, 'source_refs': refs, 'task_ids': sorted(tasks),
        'execution_ids': sorted(executions), 'observation_ids': ids,
        'failure_sha256': digest(ids), 'evaluator': evaluator['binding'] if evaluator is not None else None,
        'recurrence': 'not_yet_assessed', 'quality_credit': False}


def receipt(batch):
    return {key: batch[key] for key in ('source', 'source_refs', 'task_ids',
        'execution_ids', 'observation_ids', 'failure_sha256', 'evaluator')}


def recheck(batch, connection, owner):
    page = read(connection, owner, refs=batch['source_refs'])
    expected = {(r['source_id'], r['source_version']) for r in batch['source_refs']}
    actual = {(r['source_id'], r['source_version']) for r in page['assessments']}
    if (not page['sources_current'] or actual != expected
            or {r['task_id'] for r in page['assessments']} != set(batch['task_ids'])
            or {r['execution_id'] for r in page['assessments']} != set(batch['execution_ids'])):
        raise ValueError('Task-review evidence is no longer current')
    return page['assessments']


def evaluate_once(evaluator, connection, owner):
    """One declared candidate, through the existing evaluator and rollback ledger."""
    from tools import write_approval, skill_ledger
    from pacomind_hermes.review import editable_operation
    from pacomind_hermes.review_evaluation import audit_evaluation, evaluate_pending
    current = declaration(evaluator['path'])
    if current['binding'] != evaluator['binding']:
        raise ValueError('Task-review evaluator declaration changed')
    entries = skill_ledger.list_entries()
    oracle_id = evaluator['value']['oracle_id']
    def bound_oracle(batch):
        def oracle(text, *, phase):
            if declaration(evaluator['path'])['binding'] != evaluator['binding']:
                raise ValueError('Task-review evaluator changed during evaluation')
            recheck(batch, connection, owner)
            module, function = evaluator['value']['oracle'].split(':')
            fn = getattr(importlib.import_module(module), function)
            before = {k: os.environ.get(k) for k in evaluator['value']['environment']}
            try:
                os.environ.update(evaluator['value']['environment'])
                measured = fn(text, phase=phase)
            finally:
                for key, value in before.items():
                    if value is None: os.environ.pop(key, None)
                    else: os.environ[key] = value
            recheck(batch, connection, owner)
            return {**measured, 'task_assessment_evidence': receipt(batch),
                    'assessment_attribution': ATTRIBUTION, 'owner_approval': 'unobserved'}
        return oracle
    audited = None
    terminal = {r.get('evidence', {}).get('evaluation_id') for r in entries
        if r.get('action') == 'evaluation' and r.get('evidence', {}).get('status') in
        {'rolled_back', 'rollback_failed', 'changed_elsewhere'}}
    terminal.update(r['evidence']['rollback_target'] for r in entries
        if r.get('action') == 'rollback' and r.get('evidence', {}).get('rollback_target'))
    seen, eligible, last_audit = set(), [], {}
    for position, row in enumerate(entries):
        if row.get('action') == 'evaluation':
            last_audit.setdefault(row.get('evidence', {}).get('evaluation_id'), position)
    for row in entries:
        value = row.get('evidence', {})
        if row.get('action') != 'evaluation' or value.get('status') != 'candidate_passed':
            continue
        skill = row.get('skill')
        if skill in seen: continue
        seen.add(skill)
        batch = value.get('baseline', {}).get('task_assessment_evidence')
        if (row['id'] in terminal or not batch or value.get('oracle_id') != oracle_id
                or batch.get('evaluator') != evaluator['binding'] or not evaluator['value']['allow_apply']):
            continue
        eligible.append((last_audit.get(row['id'], len(entries)), row, batch))
    if eligible:
        # Existing ledger order rotates audits; the newest skill cannot starve
        # older adopted skills. No additional cursor or scheduler is needed.
        _, row, batch = max(eligible, key=lambda item:item[0])
        audited = audit_evaluation(row['id'], bound_oracle(batch), oracle_id=oracle_id)
        if 'result_entry_id' not in audited:
            recorded = skill_ledger.append_entry('evaluation', row['skill'], actor='curator', evidence=audited)
            if not recorded or skill_ledger.get_entry(recorded) is None:
                raise RuntimeError('Native task-review audit result was not retained')
            audited['result_entry_id'] = recorded
        audited = {**audited, 'candidate_measured': False, 'quality_credit': False}
        if audited['status'] not in {'activated', 'unavailable'}: return audited
    for pending in write_approval.list_pending(write_approval.SKILLS):
        payload = pending.get('payload', {})
        batch = payload.get('_pacomind_task_assessment_batch')
        if (pending.get('origin') != 'background_review' or not batch
                or batch.get('evaluator') != evaluator['binding']):
            continue
        operation = editable_operation(payload, allow_create=True)
        if not operation or operation['action'] != 'create':
            continue
        if not evaluator['value']['allow_apply']:
            return {'status': 'proposal_only', 'pending_id': pending['id'], 'quality_credit': False}
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if any(r.get('action') == 'evaluation' and r.get('evidence', {}).get('pending_id') == pending['id']
               and r['evidence'].get('payload_sha256') == payload_hash
               and r['evidence'].get('oracle_id') == oracle_id
               and r['evidence'].get('status') == 'not_improved' for r in skill_ledger.list_entries()):
            continue
        recheck(batch, connection, owner)
        result = evaluate_pending(pending['id'], operation['name'], bound_oracle(batch), oracle_id=oracle_id)
        return {**result, 'candidate_measured': True, 'quality_credit': False}
    return audited
