"""Cron output ownership in the existing native source-erasure ledger."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def retain(outbox, *, home, owner, job, binding, sources):
    """Reserve exact native job outputs before any remembered text is released."""
    from cron.owned_output import payload_fingerprint
    identity = 'cron:' + hashlib.sha256(encoded([str(home), owner, job['id']]).encode()).hexdigest()
    fingerprint = payload_fingerprint(job)
    with closing(outbox._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT metadata_json FROM native_source_ownership WHERE ownership_id=?',
                         (identity,)).fetchone()
        metadata = json.loads(old[0]) if old else {
            'kind': 'cron', 'native_home': str(home), 'job_id': job['id'],
            'binding_id': binding['binding_id'], 'payload_fingerprint': fingerprint, 'sources': []}
        if metadata['payload_fingerprint'] != fingerprint:
            raise ValueError('native_cron_source_binding_changed')
        metadata['sources'] = list({(ref['source_id'], ref['source_version']): dict(ref)
                                   for ref in [*metadata['sources'], *sources]}.values())
        if not metadata['sources']:
            raise ValueError('native_cron_source_evidence_required')
        db.execute('INSERT OR REPLACE INTO native_source_ownership VALUES (?,?,?,?,?)',
                   (identity, owner, 'cron:'+job['id'], job['id'], encoded(metadata)))
    outbox._fsync_storage()


async def erase(row, gateway=None):
    """Use native writers for files, queues, transcript rows and live caches."""
    from cron.jobs import use_cron_store
    from cron.owned_output import erase as erase_outputs, snapshot
    from hermes_state import SessionDB
    metadata = row['metadata']
    with use_cron_store(metadata['native_home']):
        result = erase_outputs(metadata['job_id'], expected_payload_fingerprint=metadata['payload_fingerprint'])
        if result.get('status') != 'erased':
            return {'status': 'pending', 'native_outputs': result}
        current = snapshot(metadata['job_id'])
        count = 0
        if current['sessions']:
            native = SessionDB(Path(current['native_db']))
            try:
                with closing(sqlite3.connect(Path(current['native_db']).as_uri()+'?mode=ro', uri=True)) as db:
                    routes = [json.loads(row[0]) for row in db.execute('SELECT entry_json FROM gateway_routing')]
                for group in current['sessions']:
                    session = group['session_id']
                    family = set(native.get_transcript_dependents(session))
                    keys = sorted({entry['session_key'] for entry in routes if entry.get('session_id') in family})
                    if gateway is not None:
                        keys = keys or sorted({entry['session_key'] for entry in routes})
                    if gateway is not None and keys:
                        receipt = await gateway.redact_native_message_payloads(keys[0], session, group['messages'],
                            expected_message_watermark=group['message_watermark'])
                    elif not keys and (native.get_session(session) or {}).get('source') in {'cli', 'local', 'cron', 'subagent'}:
                        receipt = native.redact_message_payloads(session, group['messages'],
                            expected_message_watermark=group['message_watermark'])
                    else:
                        return {'status': 'pending', 'reason': 'native_cron_gateway_reconciliation_required'}
                    if receipt.get('status') != 'redacted':
                        return {'status': 'pending', 'native_messages': receipt}
                    count += len(receipt['redacted_ids'])
            finally:
                native.close()
        remaining = snapshot(metadata['job_id'])
        if (remaining['sessions'] or remaining['non_message_artifacts']
                or remaining['active_executions'] or remaining['delivering']):
            return {'status': 'pending', 'reason': 'native_cron_outputs_remain'}
        return {'status': 'redacted', 'redacted_rows': count, 'native_outputs': result}
