"""Bounded chronological conversation reads over current canonical evidence."""
from contextlib import closing
import hashlib
import json

from protagine.turns.idempotency import canonical_turn_digest, source_message_hash
from protagine.turns.source_annotations import expand, current_candidates
from protagine.turns.source_channels import AUTOMATION_PLATFORMS, epoch


MAX_CONTENT = 12000
LOCATOR_LIMIT = 128


def _locators(ledger, *, contact_id, session_id, platform, comms_log):
    """Locate sources, never return a communications summary or an unlinked copy."""
    candidates, reasons = {}, set()
    scope_sql = "s.contact_id=? AND (s.scope='person' OR s.session_id=?)"
    valid_sql = '''NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i
        WHERE i.source_id=s.turn_id)'''
    with closing(ledger._connect()) as conn:
        rows = conn.execute(f'''SELECT s.*,c.conversation_id,c.basis FROM source_channels c
            JOIN turn_sources s USING(turn_id)
            WHERE c.contact_id=? AND c.platform=? AND {scope_sql} AND {valid_sql}
            ORDER BY c.occurred_epoch DESC,c.ordinal DESC,c.turn_id DESC LIMIT ?''',
            (contact_id, platform, contact_id, session_id, LOCATOR_LIMIT + 1)).fetchall()
        if len(rows) > LOCATOR_LIMIT:
            reasons.add('locator_limit')
        for row in rows[:LOCATOR_LIMIT]:
            candidates[row['turn_id']] = dict(row)
        # Read predecessor reviewed imports in place. The expression index is
        # rebuilt from canonical metadata, not from a Hermes database.
        rows = conn.execute(f'''SELECT s.* FROM turn_sources s WHERE {scope_sql}
            AND json_extract(messages_json,'$[0].provenance.kind')='hermes_history'
            AND json_extract(messages_json,'$[0].provenance.actor_basis')='reviewed_direct_session'
            AND json_extract(messages_json,'$[0].provenance.platform')=? AND {valid_sql}
            AND NOT EXISTS (SELECT 1 FROM source_channels c WHERE c.turn_id=s.turn_id)
            ORDER BY julianday(s.occurred_at) DESC,
                json_extract(s.messages_json,'$[0].provenance.message_id') DESC,s.turn_id DESC LIMIT ?''',
            (contact_id, session_id, platform, LOCATOR_LIMIT + 1)).fetchall()
        if len(rows) > LOCATOR_LIMIT:
            reasons.add('locator_limit')
        for row in rows[:LOCATOR_LIMIT]:
            value = dict(row)
            origins = [message.get('provenance', {}) for message in json.loads(row['messages_json'])]
            first = origins[0]
            if not all(origin.get('kind') == 'hermes_history'
                    and origin.get('actor_basis') == 'reviewed_direct_session'
                    and origin.get('platform') == platform
                    and origin.get('chat_id') == first.get('chat_id') for origin in origins):
                continue
            candidates[row['turn_id']] = {**value,
                'conversation_id': platform + ':' + str(first.get('chat_id') or ''),
                'basis': 'reviewed_history'}
        # Absence of channel metadata cannot be interpreted as absence of a
        # conversation. This is coverage metadata, never an unscoped read.
        unknown = conn.execute(f'''SELECT 1 FROM turn_sources s WHERE {scope_sql}
            AND {valid_sql} AND NOT EXISTS (SELECT 1 FROM source_channels c WHERE c.turn_id=s.turn_id)
            AND coalesce(json_extract(messages_json,'$[0].provenance.kind'),'')!='hermes_history'
            AND EXISTS (SELECT 1 FROM json_each(s.messages_json) m
                WHERE json_extract(m.value,'$.role')='user') LIMIT 1''',
            (contact_id, session_id)).fetchone()
        if unknown:
            reasons.add('legacy_channel_metadata_incomplete')
        if comms_log is not None:
            # Select metadata only. Its summary and write time are not source
            # evidence, and an offline communications store grants no scope.
            with closing(comms_log.read_connection()) as comms:
                linked = comms.execute('''SELECT channel,source_lineage_json FROM communications
                    WHERE contact_id=? AND source_lineage_json IS NOT NULL
                      AND (channel=? OR substr(channel,1,?)=?)
                    GROUP BY channel,source_lineage_json
                    ORDER BY julianday(json_extract(source_lineage_json,'$.occurred_at')) DESC
                    LIMIT ?''', (contact_id, platform, len(platform) + 1,
                                  platform + ':', LOCATOR_LIMIT + 1)).fetchall()
            if len(linked) > LOCATOR_LIMIT:
                reasons.add('locator_limit')
            for link in linked[:LOCATOR_LIMIT]:
                lineage = json.loads(link['source_lineage_json'])
                identifier = lineage.get('turn_id')
                if identifier in candidates:
                    continue
                row = conn.execute(f'''SELECT s.* FROM turn_sources s WHERE s.turn_id=?
                    AND {scope_sql} AND {valid_sql}
                    AND NOT EXISTS (SELECT 1 FROM source_channels c WHERE c.turn_id=s.turn_id)''',
                    (identifier, contact_id, session_id)).fetchone()
                if row is None or row['session_id'] != lineage.get('session_id'):
                    continue
                messages = json.loads(row['messages_json'])
                if [source_message_hash(row['session_id'], message) for message in messages] != lineage.get('message_hashes'):
                    continue
                candidates[identifier] = {**dict(row), 'conversation_id': link['channel'],
                                         'basis': 'source_linked_communication'}
    return list(candidates.values()), reasons


def read_recent(ledger, *, contact_id, session_id, platform, limit=8, comms_log=None):
    """Select by occurrence, then apply the same correction checks as source_read."""
    if not contact_id or not session_id or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('invalid_recent_conversation_scope')
    watermark = ledger.erasure_watermark(contact_id)
    scope = {'contact_id': contact_id, 'session_id': session_id}
    rows, reasons = ([], {'not_a_conversation_platform'}) if platform in AUTOMATION_PLATFORMS else _locators(
        ledger, **scope, platform=platform, comms_log=comms_log)
    eligible = []
    for row in rows:
        at = epoch(row['occurred_at'])
        if at is None:
            reasons.add('unknown_occurrence_time')
            continue  # An old import must not become recent at its ingestion time.
        messages = json.loads(row['messages_json'])
        direct = [message for message in messages if message.get('role') in {'user', 'assistant'}]
        if not direct:
            continue
        origin = direct[0].get('provenance', {})
        ordinal = origin.get('message_id', 0)
        ordinal = ordinal if type(ordinal) is int else 0
        row.update(messages=direct, order=(at, row['session_id'], ordinal, row['turn_id']))
        eligible.append(row)
    eligible.sort(key=lambda row: row['order'], reverse=True)
    if len(eligible) > limit:
        reasons.add('result_limit')
    entries, checked, used = [], [], 0
    from protagine.turns.audio import source_text
    for row in eligible[:limit]:
        identifier = row['turn_id']
        current = ledger.source_references([identifier], **scope)
        expected = {'source_id': identifier,
                    'source_version': canonical_turn_digest(json.loads(row['messages_json']))}
        if current != [expected]:
            reasons.add('source_changed_during_read')
            continue
        # Compact conversation text keeps role attribution without copying
        # internal transport metadata or historical database provenance.
        text = '\n\n'.join(message['role'].upper() + ': ' + source_text(message.get('content'))
                           for message in row['messages'])
        hashes = [source_message_hash(row['session_id'], message) for message in row['messages']]
        candidate = {'id': 'recent:' + identifier, 'kind': 'source_quote',
            'source_turn_ids': [identifier], '_source_message_hashes': {identifier: hashes}, 'content': text}
        expanded = current_candidates(ledger, expand(ledger, [candidate], **scope), **scope)
        if len(expanded) != 1:
            reasons.add('source_correction_unavailable')
            continue
        evidence = expanded[0]
        if expected not in evidence.get('_annotation_source_refs', []):
            reasons.add('source_changed_during_read')
            continue
        content = evidence['content']
        revision = hashlib.sha256(content.encode()).hexdigest()
        remaining = MAX_CONTENT - used
        complete = len(content) <= remaining
        if not complete:
            reasons.add('content_limit')
            if evidence.get('_annotation_ids') or remaining <= 0:
                break  # Never detach or truncate an attributed correction.
            content = content[:remaining]
        used += len(content)
        checked.append(evidence)
        entries.append({**expected, 'roles': list(dict.fromkeys(message['role'] for message in row['messages'])),
            'content': content, 'occurred_at': row['occurred_at'], 'recorded_at': row['ingested_at'],
            'timestamp_basis': 'recorded_occurrence', 'session_id': row['session_id'],
            'conversation_id': row['conversation_id'], 'complete': complete, 'read_revision': revision})
        if not complete:
            break
    # Annotation writes do not necessarily move the erasure watermark. Recheck
    # exact membership AND the watermark after all selections, before publication.
    if (len(current_candidates(ledger, checked, **scope)) != len(checked)
            or ledger.erasure_watermark(contact_id) != watermark):
        raise ValueError('recent_conversation_changed_during_read')
    refs = {ref['source_id']: ref for evidence in checked for ref in evidence['_annotation_source_refs']}
    checks = [{'source_refs': evidence['_annotation_source_refs'],
               'message_hashes': evidence['_annotation_message_hashes'],
               'annotation_ids': sorted(evidence['_annotation_ids'])} for evidence in checked]
    return {'platform': platform, 'entries': list(reversed(entries)), 'source_refs': list(refs.values()),
            'watermark': watermark, 'annotation_checks': checks,
            'coverage': {'status': 'partial' if reasons else 'complete', 'reasons': sorted(reasons),
                         'ordering': 'occurrence_ascending', 'selected_source_count': len(entries)}}
