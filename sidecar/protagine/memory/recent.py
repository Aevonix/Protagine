"""Bounded chronological conversation reads over current canonical evidence."""
from contextlib import closing
import hashlib
import json
import time

from protagine.turns.idempotency import canonical_turn_digest, source_message_hash
from protagine.turns.source_annotations import expand, current_candidates
from protagine.turns.source_channels import AUTOMATION_PLATFORMS, epoch


MAX_CONTENT = 12000
LOCATOR_LIMIT = 128


def _locators(ledger, *, contact_id, session_id, platform, comms_log, as_of):
    """Locate sources, never return a communications summary or an unlinked copy."""
    candidates, reasons = {}, set()
    scope_sql = "s.contact_id=:contact_id AND (s.scope='person' OR s.session_id=:session_id)"
    valid_sql = '''NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i
        WHERE i.source_id=s.turn_id)'''
    parameters = dict(contact_id=contact_id, session_id=session_id, platform=platform,
                      as_of=as_of, locator_limit=LOCATOR_LIMIT + 1)
    # Body JSON stays in SQLite while locating and ordering candidates. In
    # particular, LIMIT 128 does not bound memory when each source can be 8 MiB.
    columns = '''s.turn_id,s.session_id,s.occurred_at,s.ingested_at,
        (SELECT CASE WHEN json_type(m.value,'$.provenance.message_id')='integer'
                     THEN json_extract(m.value,'$.provenance.message_id') ELSE 0 END
         FROM json_each(s.messages_json) m
         WHERE json_extract(m.value,'$.role') IN ('user','assistant') LIMIT 1) AS ordinal'''
    channel_where = f'''c.contact_id=:contact_id AND c.platform=:platform
        AND {scope_sql} AND {valid_sql}'''
    history_where = f'''{scope_sql}
        AND json_extract(messages_json,'$[0].provenance.kind')='hermes_history'
        AND json_extract(messages_json,'$[0].provenance.actor_basis')='reviewed_direct_session'
        AND json_extract(messages_json,'$[0].provenance.platform')=:platform AND {valid_sql}
        AND NOT EXISTS (SELECT 1 FROM source_channels c WHERE c.turn_id=s.turn_id)'''
    current_time = "(julianday(s.occurred_at) IS NULL OR julianday(s.occurred_at)<=julianday(:as_of,'unixepoch'))"
    with closing(ledger._connect()) as conn:
        if conn.execute(f'''SELECT 1 FROM source_channels c JOIN turn_sources s USING(turn_id)
            WHERE {channel_where} AND c.occurred_epoch>:as_of LIMIT 1''', parameters).fetchone():
            reasons.add('future_occurrence_time')
        rows = conn.execute(f'''SELECT {columns},c.conversation_id,c.basis FROM source_channels c
            JOIN turn_sources s USING(turn_id)
            WHERE {channel_where} AND (c.occurred_epoch IS NULL OR c.occurred_epoch<=:as_of)
            ORDER BY c.occurred_epoch DESC,c.ordinal DESC,c.turn_id DESC LIMIT :locator_limit''', parameters).fetchall()
        if len(rows) > LOCATOR_LIMIT:
            reasons.add('locator_limit')
        for row in rows[:LOCATOR_LIMIT]:
            candidates[row['turn_id']] = dict(row)
        # Read predecessor reviewed imports in place. The expression index is
        # rebuilt from canonical metadata, not from a Hermes database.
        if conn.execute(f'''SELECT 1 FROM turn_sources s WHERE {history_where}
            AND julianday(s.occurred_at)>julianday(:as_of,'unixepoch') LIMIT 1''', parameters).fetchone():
            reasons.add('future_occurrence_time')
        rows = conn.execute(f'''SELECT {columns},
            json_extract(messages_json,'$[0].provenance.chat_id') AS history_chat
            FROM turn_sources s WHERE {history_where} AND {current_time}
            ORDER BY julianday(s.occurred_at) DESC,
                json_extract(s.messages_json,'$[0].provenance.message_id') DESC,s.turn_id DESC
            LIMIT :locator_limit''', parameters).fetchall()
        if len(rows) > LOCATOR_LIMIT:
            reasons.add('locator_limit')
        for row in rows[:LOCATOR_LIMIT]:
            candidates[row['turn_id']] = {**dict(row),
                'conversation_id': platform + ':' + str(row['history_chat'] or ''),
                'basis': 'reviewed_history', 'verify_history': True}
        # Absence of channel metadata cannot be interpreted as absence of a
        # conversation. This is coverage metadata, never an unscoped read.
        unknown = conn.execute(f'''SELECT 1 FROM turn_sources s WHERE {scope_sql}
            AND {valid_sql} AND NOT EXISTS (SELECT 1 FROM source_channels c WHERE c.turn_id=s.turn_id)
            AND coalesce(json_extract(messages_json,'$[0].provenance.kind'),'')!='hermes_history'
            AND EXISTS (SELECT 1 FROM json_each(s.messages_json) m
                WHERE json_extract(m.value,'$.role') IN ('user','assistant')) LIMIT 1''',
            parameters).fetchone()
        if unknown:
            reasons.add('legacy_channel_metadata_incomplete')
        if comms_log is not None:
            # Select metadata only. Its summary and write time are not source
            # evidence, and an offline communications store grants no scope.
            with closing(comms_log.read_connection()) as comms:
                link_where = '''contact_id=:contact_id AND source_lineage_json IS NOT NULL
                    AND (channel=:platform OR substr(channel,1,:prefix_length)=:prefix)'''
                link_parameters = {**parameters, 'prefix_length': len(platform) + 1, 'prefix': platform + ':'}
                link_time = "julianday(json_extract(source_lineage_json,'$.occurred_at'))"
                if comms.execute(f'''SELECT 1 FROM communications WHERE {link_where}
                    AND {link_time}>julianday(:as_of,'unixepoch') LIMIT 1''', link_parameters).fetchone():
                    reasons.add('future_occurrence_time')
                linked = comms.execute(f'''SELECT channel,source_lineage_json FROM communications
                    WHERE {link_where} AND ({link_time} IS NULL OR {link_time}<=julianday(:as_of,'unixepoch'))
                    GROUP BY channel,source_lineage_json
                    ORDER BY {link_time} DESC LIMIT :locator_limit''', link_parameters).fetchall()
            if len(linked) > LOCATOR_LIMIT:
                reasons.add('locator_limit')
            for link in linked[:LOCATOR_LIMIT]:
                lineage = json.loads(link['source_lineage_json'])
                identifier = lineage.get('turn_id')
                if identifier in candidates:
                    continue
                row = conn.execute(f'''SELECT {columns} FROM turn_sources s WHERE s.turn_id=:turn_id
                    AND {scope_sql} AND {valid_sql}
                    AND NOT EXISTS (SELECT 1 FROM source_channels c WHERE c.turn_id=s.turn_id)''',
                    {**parameters, 'turn_id': identifier}).fetchone()
                if row is None or row['session_id'] != lineage.get('session_id'):
                    continue
                candidates[identifier] = {**dict(row), 'conversation_id': link['channel'],
                                         'basis': 'source_linked_communication', 'verify_lineage': lineage}
    return list(candidates.values()), reasons


def _hydrate(ledger, row, *, contact_id, session_id, platform):
    """Read one current scoped body; stale or invalid locators grant no evidence."""
    with closing(ledger._connect()) as conn:
        source = conn.execute('''SELECT messages_json FROM turn_sources s WHERE s.turn_id=?
            AND s.contact_id=? AND (s.scope='person' OR s.session_id=?)
            AND NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=s.turn_id)''',
            (row['turn_id'], contact_id, session_id)).fetchone()
    if source is None:
        return None
    messages = json.loads(source['messages_json'])
    if row.get('verify_history'):
        origins = [message.get('provenance', {}) for message in messages]
        if not origins or not all(origin.get('kind') == 'hermes_history'
                and origin.get('actor_basis') == 'reviewed_direct_session'
                and origin.get('platform') == platform
                and origin.get('chat_id') == row['history_chat'] for origin in origins):
            return None
    if 'verify_lineage' in row and [source_message_hash(row['session_id'], message) for message in messages
            ] != row['verify_lineage'].get('message_hashes'):
        return None
    return messages


def read_recent(ledger, *, contact_id, session_id, platform, limit=8, comms_log=None):
    """Select by occurrence, then apply the same correction checks as source_read."""
    if not contact_id or not session_id or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('invalid_recent_conversation_scope')
    watermark = ledger.erasure_watermark(contact_id)
    scope = {'contact_id': contact_id, 'session_id': session_id}
    as_of = time.time()
    rows, reasons = ([], {'not_a_conversation_platform'}) if platform in AUTOMATION_PLATFORMS else _locators(
        ledger, **scope, platform=platform, comms_log=comms_log, as_of=as_of)
    eligible = []
    for row in rows:
        at = epoch(row['occurred_at'])
        if at is None:
            reasons.add('unknown_occurrence_time')
            continue  # An old import must not become recent at its ingestion time.
        if at > as_of:
            reasons.add('future_occurrence_time')
            continue
        ordinal = row['ordinal']
        ordinal = ordinal if type(ordinal) is int else 0
        row['order'] = (at, row['session_id'], ordinal, row['turn_id'])
        eligible.append(row)
    eligible.sort(key=lambda row: row['order'], reverse=True)
    entries, checked, used = [], [], 0
    from protagine.turns.audio import source_text
    for row in eligible:
        if used >= MAX_CONTENT:
            reasons.add('content_limit')
            break
        if len(entries) == limit:
            reasons.add('result_limit')
            break
        messages = _hydrate(ledger, row, **scope, platform=platform)
        if messages is None:
            continue
        direct = [message for message in messages if message.get('role') in {'user', 'assistant'}]
        if not direct:
            continue
        identifier = row['turn_id']
        current = ledger.source_references([identifier], **scope)
        expected = {'source_id': identifier,
                    'source_version': canonical_turn_digest(messages)}
        if current != [expected]:
            reasons.add('source_changed_during_read')
            continue
        # Compact conversation text keeps role attribution without copying
        # internal transport metadata or historical database provenance.
        text = '\n\n'.join(message['role'].upper() + ': ' + source_text(message.get('content'))
                           for message in direct)
        hashes = [source_message_hash(row['session_id'], message) for message in direct]
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
        entries.append({**expected, 'roles': list(dict.fromkeys(message['role'] for message in direct)),
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
