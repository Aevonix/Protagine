"""Quoted contributions to a partially revised assertion value.

The existing semantic review decides whether a delta is actually asserted.
This module only proves where its characters came from. Contributions point
straight to original quotations, so successive updates need no recursive reader.
"""
from difflib import SequenceMatcher
import json
import re


def revision_parts(value, evidence, previous):
    """Trace unchanged tokens to their old quotation and changed tokens here."""
    old = previous['value']
    if old == value:
        return None
    inherited = previous.get('value_parts') or [{'text': old}]
    if ''.join(p['text'] for p in inherited) != old:
        return None
    tokens = lambda text: re.findall(r'\w+|[^\w\s]|\s+', text)
    before, after = tokens(old), tokens(value)
    offsets = [0]
    for token in before:
        offsets.append(offsets[-1] + len(token))
    output = []

    def append(text, identifier=None):
        if not text:
            return
        if output and output[-1].get('source_claim_id') == identifier:
            output[-1]['text'] += text
        else:
            output.append({'text': text, **({'source_claim_id': identifier} if identifier else {})})

    for tag, start, stop, left, right in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag == 'equal':
            position = 0
            for part in inherited:
                end = position + len(part['text'])
                a, b = max(position, offsets[start]), min(end, offsets[stop])
                if a < b:
                    append(part['text'][a-position:b-position],
                           part.get('source_claim_id') or previous['id'])
                position = end
        else:
            added = ''.join(after[left:right])
            if added.strip() and added.casefold() not in evidence.casefold():
                return None
            append(added)
    if (len(output) > 32 or not output or not any(p.get('source_claim_id') for p in output)
            or ''.join(p['text'] for p in output) != value):
        return None
    return output


def dependency_sql(data_sql, contact_sql):
    """Eligibility before value deduplication; arguments are internal SQL."""
    from .source_projection import _subject_basis_source_sql
    return (f"NOT EXISTS (SELECT 1 FROM json_each({data_sql},'$.value_parts') part "
            "WHERE json_extract(part.value,'$.source_claim_id') IS NOT NULL AND NOT EXISTS (SELECT 1 "
            + _subject_basis_source_sql("json_extract(part.value,'$.source_claim_id')", contact_sql) + '))')


def value_basis(conn, claim, *, contact_id):
    """Reopen every quoted contribution under its current canonical ownership."""
    from .source_projection import _subject_basis_source_sql
    from .source_claims import admission_metadata
    from protagine.turns.idempotency import source_message_hash, canonical_turn_digest
    from protagine.turns.audio import claim_message
    parts = claim.get('value_parts')
    if (not isinstance(parts, list) or not 1 <= len(parts) <= 32
            or any(not isinstance(p, dict) or not isinstance(p.get('text'), str) for p in parts)
            or ''.join(p['text'] for p in parts) != claim['value']):
        return None
    bases = {}
    for part in parts:
        identifier = part.get('source_claim_id')
        if not identifier:
            if part['text'].strip() and part['text'].casefold() not in claim['evidence'].casefold():
                return None
            continue
        if identifier not in bases:
            row = conn.execute('SELECT b.*,bs.session_id,bs.messages_json,bs.occurred_at,bs.ingested_at,bj.timezone AS source_timezone '
                + _subject_basis_source_sql('?', '?'), (identifier, contact_id)).fetchone()
            if row is None:
                return None
            data = json.loads(row['data_json'])
            messages = json.loads(row['messages_json'])
            message = next((claim_message(m) for m in messages
                if source_message_hash(row['session_id'], m) == row['message_hash']), None)
            if (message is None or message.get('role') != 'user'
                    or admission_metadata(data) is None or data['evidence'] not in message.get('content', '')
                    or row['subject_key'] != claim['subject_key'] or row['predicate'] != claim['predicate']):
                return None
            bases[identifier] = {'claim_id': identifier, 'turn_id': row['turn_id'],
                'message_hash': row['message_hash'], 'source_version': canonical_turn_digest(messages),
                'evidence': data['evidence'], 'contributions': [],
                'reported_at': row['occurred_at'], 'recorded_at': row['ingested_at'],
                'timezone': row['source_timezone'],
                'value_use': 'Carried words retain their original source clock. The correction does not reset relative dates.',
                **{k: data[k] for k in ('evidence_basis', 'source_modality', 'epistemic_state') if k in data}}
        basis = bases[identifier]
        if part['text'].strip() and part['text'].casefold() not in basis['evidence'].casefold():
            return None
        basis['contributions'].append(part['text'])
    return list(bases.values()) if bases else None


def erase_dependents(conn, identifier):
    conn.execute("""WITH RECURSIVE removed(id) AS (
        SELECT ? UNION SELECT c.id FROM source_claims c JOIN removed r
        ON json_extract(c.data_json,'$.subject_basis_claim_id')=r.id OR EXISTS
            (SELECT 1 FROM json_each(c.data_json,'$.value_parts') p
             WHERE json_extract(p.value,'$.source_claim_id')=r.id))
        DELETE FROM source_claims WHERE id IN (SELECT id FROM removed)""", (identifier,))
