"""Durable source ownership for Hermes transcript payloads.

Only native anchors, source revisions, payload hashes and pending erasure state
live in the existing outbox. No conversation copy, canonical answer or worker
is created. Native writers own mutation, lease checks and cache invalidation.
"""
import asyncio
from contextlib import closing
import hashlib
import json
import logging
import re
from pathlib import Path
import sqlite3
import threading

from .client import source_message_hash

logger = logging.getLogger(__name__)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _affected(refs, rules):
    return any(ref['source_id'] == rule.get('source_turn_id', rule['turn_id'])
        and (rule.get('whole_source', True) or ref['source_version'] == rule.get('source_version'))
        for ref in refs for rule in rules)


class NativeOwnedCopies:
    def __init__(self, memory, scopes):
        self.memory, self.outbox, self.scopes = memory, memory.outbox, scopes
        self.gateway = None
        self.loop = None
        self._running = threading.Lock()

    @staticmethod
    def _path():
        from hermes_state import _default_db_path
        return Path(_default_db_path()).resolve()

    def _native_read(self, path=None):
        db = sqlite3.connect(Path(path or self._path()).as_uri()+'?mode=ro', uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        return db

    def retain(self, scope, sources):
        """Called only for authentic native recall/read observations, never prose."""
        if (scope is None or not scope.valid_participant or not sources
                or scope.platform == 'background_review'):
            return not sources or (scope is not None and scope.platform == 'background_review')
        try:
            anchor = self.memory.native_anchor(scope)
            if anchor is None:
                raise ValueError('native_source_anchor_unavailable')
            from hermes_state import SessionDB
            with closing(self._native_read()) as db:
                if type(anchor.get('_row_id')) is int:
                    row = db.execute("SELECT id,role,content FROM messages WHERE session_id=? AND id=?",
                                     (scope.session_id, anchor['_row_id'])).fetchone()
                else:
                    # Native turn-start persistence precedes request dispatch.
                    # The verified clean current input must match its newest
                    # user row; identical words in older turns are not selected.
                    row = db.execute("SELECT id,role,content FROM messages WHERE session_id=? "
                                     "AND role='user' ORDER BY id DESC LIMIT 1", (scope.session_id,)).fetchone()
                expected = source_message_hash(scope.session_id, {'role':'user','content':anchor.get('content')})
                if (row is None or row['role'] != 'user' or source_message_hash(scope.session_id,
                        {'role':row['role'],'content':SessionDB._decode_content(row['content'])}) != expected):
                    raise ValueError('native_source_anchor_unavailable')
            identity = 'turn:' + hashlib.sha256(_json(
                [scope.contact_id, scope.session_id, scope.task_id, scope.turn_id]).encode()).hexdigest()
            with closing(self.outbox._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                previous = db.execute('SELECT metadata_json FROM native_source_ownership WHERE ownership_id=?',
                                      (identity,)).fetchone()
                metadata = json.loads(previous[0]) if previous else {
                    'kind':'supplied', 'native_db':str(self._path()),
                    'anchor_id':row['id'], 'anchor_hash':expected, 'sources':[]}
                if metadata['anchor_id'] != row['id'] or metadata['anchor_hash'] != expected:
                    raise ValueError('native_source_anchor_changed')
                merged = list({(ref['source_id'], ref['source_version']):dict(ref)
                    for ref in [*metadata['sources'], *sources]}.values())
                if previous and merged == metadata['sources']:
                    return True
                metadata['sources'] = merged
                db.execute('INSERT OR REPLACE INTO native_source_ownership VALUES (?,?,?,?,?)',
                           (identity, scope.contact_id, scope.session_id, scope.turn_id, _json(metadata)))
            self.outbox._fsync_storage()
            return True
        except Exception as error:
            logger.warning('Native source ownership unavailable (%s)', type(error).__name__)
            return False

    def retain_origin(self, scope, source_id, *, messages=None, row_only_ids=()):
        """Bind canonical capture to actual native row IDs, never all equal text.

        Root and helper profiles can share one outbox. This content-free mapping
        is retained before enqueue; it is not a new canonical source or answer.
        """
        try:
            from hermes_state import SessionDB
            messages = messages if messages is not None else [self.memory.native_anchor(scope)]
            anchors = {}
            with closing(self._native_read()) as db:
                if not db.execute('SELECT 1 FROM sessions WHERE id=?', (scope.session_id,)).fetchone():
                    return False
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    if type(message.get('_row_id')) is int:
                        original = db.execute('SELECT id,role,content FROM messages WHERE session_id=? AND id=?',
                            (scope.session_id, message['_row_id'])).fetchone()
                    elif message.get('role') == 'user':
                        # Some native callers omit _row_id from the hook copy.
                        # The observed current input can bind only the newest
                        # user row, never every historical hash match.
                        original = db.execute("SELECT id,role,content FROM messages WHERE session_id=? "
                            "AND role='user' ORDER BY id DESC LIMIT 1", (scope.session_id,)).fetchone()
                    else:
                        raise ValueError('native_source_origin_unavailable')
                    digest = source_message_hash(scope.session_id, message)
                    if (original is None or source_message_hash(scope.session_id, {
                            'role':original['role'], 'content':SessionDB._decode_content(original['content'])}) != digest):
                        raise ValueError('native_source_origin_changed')
                    anchors[str(original['id'])] = {'mode':'payload', 'source_hash':digest}
            if not anchors:
                return False
            if row_only_ids:
                # A sole original tool-call row owns only that exact row. Its
                # arguments require a full native preimage, not an empty
                # assistant-content hash. Older native service remains usable;
                # this optional input-retention capability can be unavailable.
                with SessionDB(self._path()) as native:
                    for snapshot in native.get_message_redaction_snapshot(scope.session_id, list(row_only_ids)):
                        observed = next(message for message in messages if message.get('_row_id') == snapshot['id'])
                        if snapshot['sha256'] != observed.get('_native_payload_sha256'):
                            raise ValueError('native_source_origin_changed')
                        binding = anchors[str(snapshot['id'])]
                        binding.update(row_only=True, row_sha256=snapshot['sha256'])
            metadata = {'kind':'origin', 'native_db':str(self._path()), 'anchors':anchors}
            with closing(self.outbox._connect()) as db, db:
                db.execute('INSERT OR IGNORE INTO native_source_ownership VALUES (?,?,?,?,?)',
                    ('origin:' + source_id, scope.contact_id, scope.session_id, source_id,
                     _json(metadata)))
            self.outbox._fsync_storage()
            return bool(anchors)
        except Exception as error:
            logger.warning('Native source location unavailable (%s)', type(error).__name__)
            return False

    def observe_gateway(self, **kwargs):
        gateway = kwargs.get('gateway')
        if (gateway is not None and kwargs.get('session_store') is getattr(gateway, 'session_store', None)
                and callable(getattr(gateway, 'redact_native_message_payloads', None))):
            # Discovery grants no authority: only contact-scoped erasure events
            # and previously authenticated source ownership nominate payloads.
            self.gateway = gateway

    def _feed(self, contact):
        watermark = self.outbox.erasure_watermark(contact)
        response = self.memory.client.get('/v1/host/memory/sources/erasures',
            params={'contact_id':contact,'after':watermark}, timeout=2)
        response.raise_for_status()
        page = response.json()
        self.outbox.apply_erasure_page(contact, page)
        if page.get('complete') is not True:
            # The retained cursor advances, so the next existing settled/idle
            # callback continues the feed. This finite pass is not yet clean.
            raise ValueError('native_erasure_feed_incomplete')

    def _rows(self, contact=None, *, actionable=False):
        with closing(self.outbox._connect()) as db:
            # Two primary-key ranges avoid loading permanent origin records on
            # every turn. Origins are looked up by exact source ID when needed.
            source = ('(SELECT * FROM native_source_ownership WHERE ownership_id < \'origin:\' '
                      'UNION ALL SELECT * FROM native_source_ownership WHERE ownership_id >= \'origin;\')'
                      if actionable else 'native_source_ownership')
            return [dict(row, metadata=json.loads(row['metadata_json'])) for row in db.execute(
                'SELECT * FROM '+source+' '+('WHERE contact_id=? ' if contact else '')+
                'ORDER BY ownership_id', (contact,) if contact else ())]

    def _origin(self, row, rules):
        if row['metadata']['kind'] != 'erasure':
            return None
        rule = next((value for value in rules if value['sequence'] == row['metadata']['sequence']), None)
        if rule is None:
            raise ValueError('native_erasure_rule_unavailable')
        with closing(self.outbox._connect()) as db:
            origin = db.execute('SELECT * FROM native_source_ownership WHERE ownership_id=? AND contact_id=?',
                ('origin:' + rule.get('source_turn_id', rule['turn_id']), row['contact_id'])).fetchone()
        if origin is not None:
            if origin['session_id'] != row['session_id']:
                raise ValueError('native_source_origin_changed')
            return dict(origin, metadata=json.loads(origin['metadata_json']))

    def _save(self, row, metadata):
        with closing(self.outbox._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT metadata_json FROM native_source_ownership WHERE ownership_id=?',
                                  (row['ownership_id'],)).fetchone()
            current = json.loads(previous[0]) if previous else {}
            merged = {**current, **metadata}
            if 'sources' in merged:
                merged['sources'] = list({(ref['source_id'], ref['source_version']):ref
                    for ref in [*current.get('sources', []), *metadata.get('sources', [])]}.values())
            db.execute('UPDATE native_source_ownership SET metadata_json=? WHERE ownership_id=?',
                       (_json(merged), row['ownership_id']))
        row['metadata'] = merged
        self.outbox._fsync_storage()

    def _remove(self, row):
        with closing(self.outbox._connect()) as db, db:
            db.execute('DELETE FROM native_source_ownership WHERE ownership_id=?', (row['ownership_id'],))
        self.outbox._fsync_storage()

    def _location(self, row, owner_rows, origin=None):
        meta = row['metadata']
        if meta.get('native_db'):
            return Path(meta['native_db'])
        if origin is not None:
            self._save(row, {**meta, 'native_db':origin['metadata']['native_db']})
            return Path(origin['metadata']['native_db'])
        # Pre-migration canonical erasures have no location mapping. Inspect
        # only this profile and native paths already attested by this owner’s
        # actual adapter. Ambiguous session identities remain pending.
        candidates = {str(self._path()), *(item['metadata']['native_db'] for item in owner_rows
                                         if item['metadata'].get('native_db'))}
        found = []
        for candidate in candidates:
            with closing(self._native_read(candidate)) as db:
                if db.execute('SELECT 1 FROM sessions WHERE id=?', (row['session_id'],)).fetchone():
                    found.append(candidate)
        if len(found) > 1:
            raise ValueError('native_source_location_ambiguous')
        if not found:
            raise ValueError('native_source_location_unobserved')
        self._save(row, {**meta, 'native_db':found[0]})
        row['metadata']['native_db'] = found[0]
        return Path(found[0])

    @staticmethod
    def _span(db, session, anchor):
        end = db.execute("SELECT MIN(id) FROM messages WHERE session_id=? AND role='user' AND id>?",
                         (session, anchor)).fetchone()[0]
        rows = db.execute('SELECT * FROM messages WHERE session_id=? AND id>=? '
                          'AND (? IS NULL OR id<?) ORDER BY id LIMIT 513', (session, anchor, end, end)).fetchall()
        if len(rows) > 512:
            raise ValueError('native_source_span_exceeds_batch')
        return rows

    def _selection(self, row, rules, native, origin=None):
        meta, session = row['metadata'], row['session_id']
        if meta['kind'] == 'origin':
            return None
        if meta['kind'] == 'supplied':
            if not _affected(meta['sources'], rules):
                return None
            anchors = {meta['anchor_id']: {'mode':'api_content', 'source_hash':meta['anchor_hash']}}
        else:
            # Persist authentic anchor IDs before erasing the text that proved
            # ownership. On retry, expand the turn again: its writer may have
            # added a final answer after an earlier attempt returned pending.
            rule = next((value for value in rules if value['sequence'] == meta['sequence']), None)
            if rule is None:
                raise ValueError('native_erasure_rule_unavailable')
            anchors = {int(key): value for key, value in meta.get('anchors', {}).items()}
            if not anchors and origin is not None:
                anchors = {int(key): value for key, value in origin['metadata'].get('anchors', {}).items()}
                if anchors and not rule.get('whole_source', True):
                    anchors = {key:value for key,value in anchors.items()
                               if value['source_hash'] in rule['message_hashes']}
                    if not anchors:
                        raise ValueError('native_source_partial_origin_unobserved')
        modes, replay, payloads = {}, {}, {}
        previous = {item['id']:item for item in meta.get('selection', [])}
        with closing(self._native_read(native.db_path)) as db:
            from hermes_state import SessionDB
            # Anchor validation, span expansion and native payload digests must
            # describe one SQLite snapshot. The writer separately checks this
            # watermark under its own mutation transaction before erasing.
            db.execute('BEGIN')
            message_watermark = db.execute('SELECT coalesce(MAX(id),0) FROM messages WHERE session_id=?',
                                            (session,)).fetchone()[0]
            if not anchors:
                matches = {}
                for original in db.execute("SELECT id,role,content FROM messages WHERE session_id=? "
                                           "AND role IN ('user','assistant','tool')", (session,)):
                    message = {'role': original['role'],
                               'content': SessionDB._decode_content(original['content'])}
                    digest = source_message_hash(session, message)
                    # Metadata-only observations are not native transcript
                    # messages merely because both have an empty body.
                    if message['content'] and digest in rule['message_hashes']:
                        if digest in matches:
                            raise ValueError('native_source_origin_ambiguous')
                        matches[digest] = original['id']
                        anchors[original['id']] = {'mode':'payload', 'source_hash':digest}
                if not anchors:
                    raise ValueError('native_source_origin_unobserved')
            for anchor, binding in anchors.items():
                actual = db.execute('SELECT * FROM messages WHERE session_id=? AND id=?',
                                    (session, anchor)).fetchone()
                if actual is None:
                    raise ValueError('native_source_anchor_missing')
                digest = source_message_hash(session, {'role':actual['role'],
                    'content':SessionDB._decode_content(actual['content'])})
                row_changed = (binding.get('row_sha256') is not None and
                    native.message_redaction_snapshot(actual)['sha256'] != binding['row_sha256'])
                if digest != binding['source_hash'] or row_changed:
                    prior = previous.get(anchor)
                    markers = json.loads(actual['display_metadata'] or '{}')
                    marker = markers.get('redacted_from_sha256')
                    if prior is None and isinstance(marker, str) and re.fullmatch('[0-9a-f]{64}', marker):
                        # Another authentically overlapping source may already
                        # have erased this exact bound row. The native writer
                        # accepts this digest only when EVERY payload field
                        # still has its fully redacted shape. A retained marker
                        # beside new unrelated content cannot pass that check.
                        prior = {'id':anchor, 'mode':'payload', 'sha256':marker}
                    if (not prior or prior['mode'] != 'payload'
                            or marker != prior['sha256']):
                        raise ValueError('native_source_anchor_changed')
                    # Native validates the full already-redacted row against
                    # this original preimage before accepting the replay.
                    replay[anchor] = prior
                span = [actual] if binding.get('row_only') else self._span(db, session, anchor)
                for item in span:
                    payloads[item['id']] = item
                    selected_mode = binding['mode'] if item['id'] == anchor else 'payload'
                    # A canonical origin is stronger ownership than an API-only
                    # recall copy attached to an otherwise unrelated user row.
                    if modes.get(item['id']) != 'payload':
                        modes[item['id']] = selected_mode
            selected = [native.message_redaction_snapshot(payloads[key], mode=mode)
                for mode in ('payload', 'api_content')
                for key in sorted(key for key, value in modes.items() if value == mode)]
        selected = [replay.get(item['id'], item) for item in selected]
        if selected:
            self._save(row, {**meta, 'anchors':anchors, 'selection':selected,
                             'selection_message_watermark':message_watermark})
        return selected

    def _contacts(self):
        with closing(self.outbox._connect()) as db:
            return [row[0] for row in db.execute('SELECT DISTINCT contact_id FROM native_source_ownership')]

    async def reconcile(self, *, gateway=None, contact=None):
        if not self._running.acquire(blocking=False):
            return {'status':'pending','reason':'reconciliation_running'}
        result = {'status':'settled','redacted_rows':0,'pending':0}
        try:
            contacts = [contact] if contact else self._contacts()
            for owner in contacts:
                try:
                    await asyncio.to_thread(self._feed, owner)
                except Exception:
                    # Already retained erasures remain authoritative during an
                    # outage; only discovery of newer events is unavailable.
                    result['pending'] += 1
                _, rules = self.outbox.erasure_state(owner)
                from hermes_state import SessionDB
                owner_rows = self._rows(owner, actionable=True)
                removed = set()
                for row in owner_rows:
                    if row['ownership_id'] in removed:
                        continue
                    if row['metadata']['kind'] == 'origin':
                        continue
                    if (row['metadata']['kind'] == 'supplied'
                            and not _affected(row['metadata']['sources'], rules)):
                        continue
                    native = None
                    try:
                        origin = self._origin(row, rules)
                        location = self._location(row, owner_rows, origin)
                        native = SessionDB(location)
                        with closing(self._native_read(location)) as db:
                            routes = [json.loads(value[0]) for value in db.execute('SELECT entry_json FROM gateway_routing')]
                        family = set(native.get_transcript_dependents(row['session_id']))
                        family_keys = sorted({entry['session_key'] for entry in routes
                                              if entry.get('session_id') in family})
                        standalone = (not family_keys and
                            (native.get_session(row['session_id']) or {}).get('source')
                            in {'cli', 'local', 'cron', 'subagent'})
                        selected = self._selection(row, rules, native, origin)
                        if not selected:
                            receipt = {'status':'redacted', 'redacted_ids':[]}
                        elif gateway is None:
                            if not standalone:
                                raise ValueError('native_gateway_reconciliation_required')
                            receipt = native.redact_message_payloads(row['session_id'], selected,
                                expected_message_watermark=row['metadata']['selection_message_watermark'])
                        else:
                            # A retired conversation can use another retained
                            # routing key in the same profile; the native door
                            # validates the exact DB and evicts all owned aliases.
                            keys = family_keys or sorted({entry['session_key'] for entry in routes})
                            if keys:
                                receipt = await gateway.redact_native_message_payloads(keys[0], row['session_id'], selected,
                                    expected_message_watermark=row['metadata']['selection_message_watermark'])
                            elif standalone:
                                receipt = native.redact_message_payloads(row['session_id'], selected,
                                    expected_message_watermark=row['metadata']['selection_message_watermark'])
                            else:
                                raise ValueError('native_erasure_routing_unavailable')
                        if receipt.get('status') == 'redacted':
                            self._remove(row)
                            result['redacted_rows'] += len(receipt['redacted_ids'])
                            full_ids = {item['id'] for item in selected if item['mode'] == 'payload'}
                            for linked in owner_rows:
                                if (linked['metadata']['kind'] == 'supplied'
                                        and linked['metadata'].get('native_db') == str(location)
                                        and linked['session_id'] == row['session_id']
                                        and linked['metadata']['anchor_id'] in full_ids):
                                    self._remove(linked)
                                    removed.add(linked['ownership_id'])
                            if row['metadata']['kind'] == 'erasure':
                                rule = next(value for value in rules if value['sequence'] == row['metadata']['sequence'])
                                if rule.get('whole_source', True):
                                    if origin is not None:
                                        self._remove(origin)
                        else:
                            result['pending'] += 1
                    except Exception as error:
                        result['pending'] += 1
                        reason = str(error) if (type(error) is ValueError
                            and re.fullmatch(r'native_[a-z_]{1,80}', str(error))) else type(error).__name__
                        if row['metadata'].get('pending_reason') != reason:
                            try:
                                self._save(row, {**row['metadata'], 'pending_reason':reason})
                            except Exception:
                                pass  # The original durable ownership still requires reconciliation.
                            logger.warning('Native owned-copy erasure pending (%s)', reason)
                    finally:
                        if native is not None:
                            native.close()
            if result['pending']:
                result['status'] = 'pending'
            return result
        finally:
            self._running.release()

    def native_settled(self, **kwargs):
        scope = self.scopes.for_execution(**{name:str(kwargs.get(name) or '')
            for name in ('session_id','task_id','turn_id')})
        if scope is None or not scope.valid_participant:
            return
        self.memory.release_native_anchor(scope)
        # Gateway still owns its outer local token at this native boundary.
        if self.gateway is not None:
            return
        return asyncio.run(self.reconcile(contact=scope.contact_id))

    def gateway_settled(self, **kwargs):
        gateway = kwargs.get('gateway')
        if gateway is None or not callable(getattr(gateway, 'redact_native_message_payloads', None)):
            return
        self.gateway, self.loop = gateway, asyncio.get_running_loop()
        task = self.loop.create_task(self.reconcile(gateway=gateway))
        task.add_done_callback(self._completed)

    @staticmethod
    def _completed(task):
        if not task.cancelled() and task.exception() is not None:
            logger.warning('Native owned-copy reconciliation remains pending (%s)',
                           type(task.exception()).__name__)

    def idle(self, **kwargs):
        if kwargs.get('dry_run') or kwargs.get('board') not in (None,'default'):
            return
        if self.gateway is not None and self.loop is not None and self.loop.is_running():
            task = asyncio.run_coroutine_threadsafe(self.reconcile(gateway=self.gateway), self.loop)
            task.add_done_callback(self._completed)
