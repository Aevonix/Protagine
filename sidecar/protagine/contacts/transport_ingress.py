"""Metadata receipts for a transport-owned durable inbox, in the communications DB.

No message bodies or attachment bytes belong here. A claimed handoff is deliberately
not retried: only an actual canonical turn receipt can settle its processing.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS transport_ingress (
            receipt_id TEXT PRIMARY KEY, producer TEXT NOT NULL,
            account_id TEXT NOT NULL, epoch TEXT NOT NULL, sequence INTEGER NOT NULL,
            event_id TEXT NOT NULL, contact_id TEXT, occurred_at REAL NOT NULL,
            journal_ref TEXT NOT NULL, payload_digest TEXT NOT NULL,
            media_available INTEGER NOT NULL, metadata_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'admitted', batch_id TEXT,
            native_turn_json TEXT, source_versions_json TEXT,
            outcome TEXT, created_at REAL NOT NULL, completed_at REAL,
            UNIQUE(producer,account_id,epoch,sequence));
        CREATE INDEX IF NOT EXISTS transport_ingress_native
            ON transport_ingress(native_turn_json,state);
        CREATE TABLE IF NOT EXISTS transport_ingress_coverage (
            producer TEXT NOT NULL, account_id TEXT NOT NULL, epoch TEXT NOT NULL,
            connected_since REAL, observed_at REAL NOT NULL, watermark INTEGER NOT NULL,
            connected INTEGER NOT NULL, unavailable INTEGER NOT NULL,
            sequence_floor INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(producer,account_id));
    ''')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(transport_ingress_coverage)')}
    if 'sequence_floor' not in columns:
        conn.execute('ALTER TABLE transport_ingress_coverage ADD COLUMN sequence_floor INTEGER NOT NULL DEFAULT 0')
    conn.commit()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _text(value, name, limit=512):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError('invalid_ingress_' + name)
    return value


class TransportIngress:
    """Use the caller's existing CommsLog connection and transaction lifetime."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, receipt_id):
        row = self.conn.execute('SELECT * FROM transport_ingress WHERE receipt_id=?',
                                (receipt_id,)).fetchone()
        return dict(row) if row else None

    def for_event(self, *, producer, account_id, epoch, sequence):
        row = self.conn.execute('SELECT * FROM transport_ingress WHERE producer=? AND account_id=? '
                                'AND epoch=? AND sequence=?', (producer, account_id, epoch, sequence)).fetchone()
        return dict(row) if row else None

    def admit(self, *, producer, account_id, epoch, sequence, event_id, contact_id,
              occurred_at, journal_ref, payload_digest, media_available, metadata=None,
              now=None):
        now = time.time() if now is None else float(now)
        for name, value in [('producer', producer), ('account_id', account_id),
                            ('epoch', epoch), ('event_id', event_id), ('journal_ref', journal_ref)]:
            _text(value, name)
        if (type(sequence) is not int or sequence < 1 or type(media_available) is not bool
                or not 0 < float(occurred_at) <= now + 1
                or len(payload_digest) != 64 or any(c not in '0123456789abcdef' for c in payload_digest)):
            raise ValueError('invalid_ingress_envelope')
        if contact_id is not None:
            _text(contact_id, 'contact_id', 128)
        metadata = dict(metadata or {})
        if set(metadata) - {'channel', 'sender_ref', 'reply_to_ref', 'from_owner', 'is_group'}:
            raise ValueError('ingress_metadata_only')
        if len(_json(metadata)) > 2048:
            raise ValueError('ingress_metadata_too_large')
        receipt_id = 'ingress:' + hashlib.sha256(_json([producer, account_id, epoch, sequence]).encode()).hexdigest()
        fields = dict(producer=producer, account_id=account_id, epoch=epoch, sequence=sequence,
                      event_id=event_id, contact_id=contact_id, occurred_at=float(occurred_at),
                      journal_ref=journal_ref, payload_digest=payload_digest,
                      media_available=int(media_available), metadata_json=_json(metadata))
        with self.conn:
            # The event itself is the identity, not the digest of its tuple: a
            # receipt adopted from a retired producer keeps the id the transport
            # already journaled, and a re-admit must find it.
            old = self.for_event(producer=producer, account_id=account_id, epoch=epoch, sequence=sequence)
            if old:
                receipt_id = old['receipt_id']
                if old['state'] == 'erased':
                    if any(old[key] != fields[key] for key in ('event_id', 'journal_ref', 'payload_digest')):
                        raise ValueError('ingress_event_conflict')
                    return self.view(old)
                # A retained provider event can acquire its missing attachment later.
                # No claimed turn or identity/timestamp may be rewritten by repair.
                repair = (old['state'] == 'admitted' and not old['media_available']
                          and fields['media_available'] == 1
                          and all(old[key] == val for key, val in fields.items()
                                  if key not in {'payload_digest', 'media_available'}))
                if repair:
                    self.conn.execute('UPDATE transport_ingress SET payload_digest=?,media_available=1 '
                                      'WHERE receipt_id=?', (payload_digest, receipt_id))
                    return self.view(self.get(receipt_id))
                if any(old[key] != val for key, val in fields.items()):
                    raise ValueError('ingress_event_conflict')
                return self.view(old)
            columns = ','.join(fields)
            self.conn.execute(f'INSERT INTO transport_ingress (receipt_id,{columns},created_at) '
                              f'VALUES ({",".join("?" for _ in range(len(fields)+2))})',
                              (receipt_id, *fields.values(), now))
        return self.view(self.get(receipt_id))

    @staticmethod
    def view(row):
        return {key: row[key] for key in ('receipt_id', 'state', 'batch_id', 'journal_ref',
                                         'media_available', 'source_versions_json', 'outcome')}

    def handoff(self, *, producer, receipt_ids, batch_id, native_turn=None):
        _text(batch_id, 'batch_id', 128)
        ids = sorted(set(receipt_ids))
        if not ids or len(ids) > 100:
            raise ValueError('invalid_ingress_batch')
        native = None
        if native_turn is not None:
            if set(native_turn) != {'session_id', 'task_id', 'turn_id'}:
                raise ValueError('invalid_ingress_native_turn')
            native = _json({key: _text(value, key, 256) for key, value in native_turn.items()})
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            rows = [self.get(receipt) for receipt in ids]
            if any(not row or row['producer'] != producer for row in rows):
                raise ValueError('ingress_receipt_scope_mismatch')
            if any(row['state'] not in {'admitted', 'handed_off'} or not row['media_available'] for row in rows):
                raise ValueError('ingress_not_ready')
            if any(row['batch_id'] not in (None, batch_id) for row in rows):
                raise ValueError('ingress_handoff_conflict')
            if native and any(row['native_turn_json'] not in (None, native) for row in rows):
                raise ValueError('ingress_native_turn_conflict')
            may_dispatch = all(row['state'] == 'admitted' for row in rows)
            if native and may_dispatch:
                raise ValueError('ingress_handoff_required_before_native_binding')
            for row in rows:
                self.conn.execute('UPDATE transport_ingress SET state=?,batch_id=?, '
                                  'native_turn_json=COALESCE(native_turn_json,?) WHERE receipt_id=?',
                                  ('handed_off', batch_id, native, row['receipt_id']))
        return {'receipt_ids': ids, 'batch_id': batch_id, 'may_dispatch': may_dispatch,
                'native_bound': native is not None, 'processing_complete': False}

    def complete(self, *, native_turn, source_versions, outcome, now=None):
        """Call only after canonical source admission; not exposed to a model tool."""
        if not source_versions or outcome not in {'captured', 'interrupted', 'failed'}:
            raise ValueError('canonical_ingress_completion_required')
        for key, value in source_versions.items():
            _text(key, 'source_id'); _text(value, 'source_version')
        stamp = time.time() if now is None else now
        rows = self.conn.execute('SELECT * FROM transport_ingress WHERE native_turn_json=?',
                                 (_json(native_turn),)).fetchall()
        with self.conn:
            for row in rows:
                if row['state'] == 'erased':
                    continue
                if row['state'] == 'completed':
                    if row['source_versions_json'] != _json(source_versions) or row['outcome'] != outcome:
                        raise ValueError('ingress_completion_conflict')
                    continue
                self.conn.execute('UPDATE transport_ingress SET state=?,source_versions_json=?, '
                                  'outcome=?,completed_at=? WHERE receipt_id=?',
                                  ('completed', _json(source_versions), outcome, stamp, row['receipt_id']))
        return [self.view(self.get(row['receipt_id'])) for row in rows]

    def for_canonical_turn(self, *, turn_id, contact_id):
        """Unique native tuple bound by the transport producer, never a text match."""
        rows = self.conn.execute("SELECT * FROM transport_ingress WHERE contact_id=? "
                                 "AND native_turn_json IS NOT NULL AND state!='erased'", (contact_id,)).fetchall()
        rows = [row for row in rows if json.loads(row['native_turn_json']).get('turn_id') == turn_id]
        tuples = {row['native_turn_json'] for row in rows}
        return ([dict(row) for row in rows] if len(tuples) == 1 else [])

    def receipts(self, *, producer, receipt_ids):
        if len(receipt_ids) > 100:
            raise ValueError('invalid_ingress_batch')
        rows = [self.get(receipt) for receipt in receipt_ids]
        if any(not row or row['producer'] != producer for row in rows):
            raise ValueError('ingress_receipt_scope_mismatch')
        return [self.view(row) for row in rows]

    def erase_sources(self, source_ids):
        """Return locators root must erase at the sole payload owner, idempotently."""
        wanted = set(source_ids)
        rows = self.conn.execute('SELECT * FROM transport_ingress WHERE source_versions_json IS NOT NULL '
                                 'OR native_turn_json IS NOT NULL').fetchall()
        selected = [row for row in rows if wanted.intersection(json.loads(row['source_versions_json'] or '{}'))
                    or json.loads(row['native_turn_json'] or '{}').get('turn_id') in wanted]
        with self.conn:
            for row in selected:
                self.conn.execute("UPDATE transport_ingress SET state='erased',metadata_json='{}',"
                                  "contact_id=NULL WHERE receipt_id=?", (row['receipt_id'],))
        return [{'receipt_id': row['receipt_id'], 'journal_ref': row['journal_ref']} for row in selected]

    def observe_coverage(self, *, producer, account_id, epoch, connected_since, observed_at,
                         watermark, connected, unavailable, sequence_floor=0, now=None):
        stamp = time.time() if now is None else float(now)
        if (type(watermark) is not int or watermark < 0 or type(connected) is not bool
                or type(sequence_floor) is not int or not 0 <= sequence_floor <= watermark
                or type(unavailable) is not int or unavailable < 0
                or not 0 < float(observed_at) <= stamp + 1
                or (connected and (connected_since is None or not 0 < connected_since <= observed_at))):
            raise ValueError('invalid_ingress_coverage')
        old = self.conn.execute('SELECT * FROM transport_ingress_coverage WHERE producer=? AND account_id=?',
                                (producer, account_id)).fetchone()
        if old and (observed_at < old['observed_at'] or (epoch == old['epoch'] and watermark < old['watermark'])):
            raise ValueError('ingress_coverage_regression')
        with self.conn:
            self.conn.execute('INSERT OR REPLACE INTO transport_ingress_coverage VALUES (?,?,?,?,?,?,?,?,?)',
                              (producer, account_id, epoch, connected_since, observed_at,
                               watermark, int(connected), unavailable, sequence_floor))

    def coverage(self, *, producer, account_id, contact_id, since, now=None, max_age=5,
                 until=None, observation=None):
        """Read current coverage or recheck a retained internal observation.

        A fixed horizon excludes later activity from that earlier interval.
        Rechecking a stored observation still queries current intake/erasure
        records; it never treats a retained coverage hash as timeless proof.
        """
        now = time.time() if now is None else now
        row = observation if observation is not None else self.conn.execute(
            'SELECT * FROM transport_ingress_coverage WHERE producer=? AND account_id=?',
            (producer, account_id)).fetchone()
        if observation is not None and (row['producer'] != producer or row['account_id'] != account_id):
            raise ValueError('coverage_observation_scope_mismatch')
        if until is not None and not since <= until <= now:
            raise ValueError('invalid_coverage_interval')
        reasons = []
        if not row or not row['connected'] or row['connected_since'] is None or row['connected_since'] > since:
            reasons.append('connection_interval_unknown')
        if not row or now - row['observed_at'] > max_age:
            reasons.append('intake_observation_stale')
        if row:
            admitted = self.conn.execute('SELECT COUNT(*) FROM transport_ingress WHERE producer=? '
                'AND account_id=? AND epoch=? AND sequence>? AND sequence<=?',
                (producer, account_id, row['epoch'], row['sequence_floor'], row['watermark'])).fetchone()[0]
            if admitted != row['watermark'] - row['sequence_floor'] or row['unavailable']:
                reasons.append('intake_gap')
        activity = self.conn.execute('SELECT receipt_id,state,metadata_json FROM transport_ingress '
            'WHERE producer=? AND account_id=? AND occurred_at>=? AND '
            '(contact_id=? OR contact_id IS NULL)'+(' AND occurred_at<=?' if until is not None else ''),
            (producer, account_id, since, contact_id, *([until] if until is not None else []))).fetchall()
        activity = [item for item in activity if not json.loads(item['metadata_json']).get('from_owner')]
        if activity:
            reasons.append('recipient_activity_requires_review')
        return {'observed': not reasons, 'reasons': reasons, 'observed_through': row['observed_at'] if row else None,
                'watermark': row['watermark'] if row else None, 'activity_receipts': [r['receipt_id'] for r in activity],
                'effect_authorized': False}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def retired_producer_rows(conn: sqlite3.Connection, producer: str) -> dict:
    """What ``adopt_retired_producers`` would change: receipts and coverage rows
    stamped by any producer other than *producer*, minus the receipts whose event
    already exists under *producer* (those are kept as they are)."""
    result = {'receipts': 0, 'coverage': 0, 'producers': []}
    if not _has_table(conn, 'transport_ingress'):
        return result
    adoptable = conn.execute(
        'SELECT producer FROM transport_ingress AS r WHERE r.producer!=? AND NOT EXISTS ('
        'SELECT 1 FROM transport_ingress AS t WHERE t.producer=? AND t.account_id=r.account_id '
        'AND t.epoch=r.epoch AND t.sequence=r.sequence)', (producer, producer)).fetchall()
    result['receipts'] = len(adoptable)
    producers = {row[0] for row in adoptable}
    if _has_table(conn, 'transport_ingress_coverage'):
        result['coverage'] = conn.execute('SELECT COUNT(*) FROM transport_ingress_coverage WHERE producer!=?',
                                          (producer,)).fetchone()[0]
        producers.update(row[0] for row in conn.execute(
            'SELECT DISTINCT producer FROM transport_ingress_coverage WHERE producer!=?', (producer,)))
    result['producers'] = sorted(producers)
    return result


def adopt_retired_producers(conn: sqlite3.Connection, producer: str) -> dict:
    """Re-scope every receipt and coverage row of a retired producer to *producer*.

    The instance authenticates one credential, so every row stamped by another
    producer was written by a principal that can no longer present itself; the
    transport that journaled those receipts now presents the instance key and
    must still read, hand off and settle them. Receipt ids are kept (the
    transport holds them). Rows of every state move together, because a coverage
    observation counts the receipts of its epoch window whatever their state.
    A receipt whose event already exists under *producer* is kept as it is.
    Coverage is one row per (producer, account): the newest observation wins.
    Idempotent: a second call finds nothing to adopt.
    """
    result = {'receipts': 0, 'coverage': 0, 'kept': 0, 'producers': []}
    if not _has_table(conn, 'transport_ingress'):
        return result
    producers = set()
    with conn:
        rows = conn.execute('SELECT receipt_id, producer, account_id, epoch, sequence FROM transport_ingress '
                            'WHERE producer!=? ORDER BY created_at, receipt_id', (producer,)).fetchall()
        for row in rows:
            producers.add(row['producer'])
            taken = conn.execute('SELECT 1 FROM transport_ingress WHERE producer=? AND account_id=? AND epoch=? '
                                 'AND sequence=?', (producer, row['account_id'], row['epoch'], row['sequence'])).fetchone()
            if taken:
                result['kept'] += 1
                continue
            conn.execute('UPDATE transport_ingress SET producer=? WHERE receipt_id=?', (producer, row['receipt_id']))
            result['receipts'] += 1
        if _has_table(conn, 'transport_ingress_coverage'):
            accounts = [row[0] for row in conn.execute(
                'SELECT DISTINCT account_id FROM transport_ingress_coverage WHERE producer!=?', (producer,))]
            for account_id in accounts:
                candidates = conn.execute(
                    'SELECT * FROM transport_ingress_coverage WHERE account_id=? '
                    'ORDER BY observed_at DESC, watermark DESC, (producer=?) DESC', (account_id, producer)).fetchall()
                producers.update(row['producer'] for row in candidates if row['producer'] != producer)
                winner = candidates[0]
                conn.execute('DELETE FROM transport_ingress_coverage WHERE account_id=? AND producer!=?',
                             (account_id, winner['producer']))
                if winner['producer'] != producer:
                    conn.execute('UPDATE transport_ingress_coverage SET producer=? WHERE account_id=? AND producer=?',
                                 (producer, account_id, winner['producer']))
                result['coverage'] += 1
    result['producers'] = sorted(producers)
    return result
