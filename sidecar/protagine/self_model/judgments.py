"""The one opinion store: the agent's stances, what each rests on and how it changed.

Stances are fallible agent views, never facts, owner preferences or grants. Each
rests on explicit premises: an admitted source claim of any contact, a settled mind
task outcome, a mind finding, the agent's own statement, or a quote carried over from
a migrated appraisal. The store, not the model, enforces the new-premise rule: a
revision needs a current premise of a revising kind that the head does not already
cite, by reference or by content. Every formation, revision and withdrawal is an
owner-audience autobiography entry in the ledger. The pass that proposes stances is
``protagine.mind.opinions``; the projection worker reaches it through ``process_one``.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import time
import unicodedata

from protagine.turns.idempotency import source_message_hash

logger = logging.getLogger(__name__)

VERSION = 'opinion-v3'
SUBJECT_KINDS = ('topic', 'person', 'approach')
AUDIENCES = ('all', 'owner')
PREMISE_KINDS = ('claim', 'outcome', 'finding', 'statement', 'quote')
REVISING_KINDS = frozenset({'claim', 'outcome', 'finding'})
FORMING_KINDS = frozenset({'claim', 'outcome', 'finding', 'statement'})
VERIFIED_OUTCOMES = frozenset({'owner', 'check', 'hermes_failure'})
REVISIONS_PER_DAY, LIMIT_WINDOW_S = 1, 86400
JOB_ATTEMPTS, PREMISE_CHARS = 3, 600
CERTAINTIES = ('tentative', 'moderate', 'strong')
STANCE_CLASSES = ('avoid', 'prefer')
STALE_JOB_S = 48 * 3600
ENTRY_PREFIX = 'mind:opinion:'
ENTRY_EVENTS = ('formed', 'revised', 'withdrawn')
_RUNTIME_MARKERS = ('_native_runtime_observation', '_task_artifact_assessment', '_task_execution_outcome')
_RECORD_ID = re.compile(r'\b[a-z]{1,6}-\d{1,8}\b', re.I)
_HEX_RUN = re.compile(r'\b[0-9a-f]{8,}\b', re.I)
# The ledger's recall stop list plus the entry template's own words, so a query is
# not matched to every stance merely because it asks for a "view".
_FTS_STOP = {'the', 'and', 'that', 'this', 'what', 'when', 'where', 'how', 'you', 'your', 'was', 'were',
             'are', 'for', 'with', 'remember', 'about', 'view', 'opinion', 'formed', 'changed', 'withdrew',
             'because', 'would', 'change', 'now', 'new', 'evidence', 'owner', 'request'}


def content_key(text):
    """Same content under a fresh record id or hash is the same premise."""
    value = unicodedata.normalize('NFKC', str(text or '')).casefold()
    value = _HEX_RUN.sub(' ', _RECORD_ID.sub(' ', value))
    return hashlib.sha256(' '.join(value.split()).encode()).hexdigest()


def normalize_topic(topic):
    if not isinstance(topic, str) or '\n' in topic or '\r' in topic:
        raise ValueError('invalid_topic')
    value = ' '.join(topic.casefold().split())
    if not 1 <= len(value) <= 80:
        raise ValueError('invalid_topic')
    return value


def head_key(subject_kind, subject, topic):
    # Topic stances keep the predecessor's key, so legacy heads stay valid.
    if subject_kind == 'topic' and not subject:
        return hashlib.sha256(topic.encode()).hexdigest()
    return hashlib.sha256(json.dumps([subject_kind, subject, topic]).encode()).hexdigest()


@dataclass(frozen=True)
class Premise:
    kind: str
    ref: str
    text: str
    key: str
    turn_id: str = ''
    message_hash: str = ''
    contact_id: str = ''
    verified: str = ''
    corrects: tuple = ()
    role: str = 'support'
    at: str = ''

    def as_dict(self):
        return asdict(self) | {'corrects': list(self.corrects)}

    @classmethod
    def from_dict(cls, value):
        if isinstance(value, cls):
            return value
        known = {name: value[name] for name in cls.__dataclass_fields__ if name in value}
        known['corrects'] = tuple(known.get('corrects') or ())
        return cls(**known)


@dataclass
class Proposal:
    subject_kind: str
    subject: str
    topic: str
    stance: str
    reason: str
    certainty: str
    revise_if: str
    premises: list
    source_ref: str
    session_id: str = ''
    new_evidence: tuple = ()
    stance_class: str | None = None
    processor: dict = field(default_factory=dict)
    owner_reconsider: bool = False


@dataclass(frozen=True)
class Result:
    disposition: str
    stance_id: int | None = None
    retry_at: float | None = None


class _Invalid(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _text(message):
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, list):
        content = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict)
                            and p.get('type') in {'text', 'input_text', 'output_text'} and isinstance(p.get('text'), str))
    return content if isinstance(content, str) else ''


def _claim_ref(identifier):
    return identifier if identifier.startswith('claim:') else 'claim:' + identifier


def _audience(subject_kind, premises):
    return 'all' if subject_kind == 'topic' and premises and all(
        p.kind in {'finding', 'outcome'} for p in premises) else 'owner'


def _about(subject_kind, subject):
    return f' about {subject}' if subject_kind == 'person' else f' (approach to {subject})' if subject_kind == 'approach' else ''


def _unique(values):
    return list({_json(value): value for value in values}.values())


def _admitted(conn, turn_id, message_hash, contact_id, *, corrects=False):
    """Admitted premises of one message, bound to its own contact: [(Premise, basis refs)].

    Only decisions, procedures and substantive events; the claim job must be complete;
    superseded, retracted, annotated, attribution-invalidated and projection-erased
    claims never qualify, and subject and value bases must still resolve. Admission
    preserves an attributed report or unverified model judgment, never factual authority.
    """
    rows = conn.execute('''SELECT c.id,c.data_json,s.occurred_at,s.ingested_at FROM source_claims c
        JOIN source_claim_jobs j ON j.turn_id=c.turn_id
        JOIN turn_sources s ON s.turn_id=c.turn_id
        WHERE c.turn_id=? AND c.message_hash=? AND s.contact_id=? AND s.scope='person'
        AND j.status='complete' AND c.superseded_by IS NULL AND c.retracted_by IS NULL
        AND json_extract(c.data_json,'$.memory_quality.memory_kind') IN ('decision','procedure','substantive_event')
        AND NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=s.turn_id)
        AND NOT EXISTS (SELECT 1 FROM source_projection_erasures e WHERE e.turn_id=s.turn_id)
        AND NOT EXISTS (SELECT 1 FROM source_annotations a,json_each(a.target_message_hashes_json) h
                        WHERE a.target_source_id=s.turn_id AND h.value=c.message_hash)
        ORDER BY c.id''', (turn_id, message_hash, contact_id)).fetchall()
    from protagine.beliefs.source_claims import admission_metadata
    result = []
    for row in rows:
        claim = json.loads(row['data_json'])
        if admission_metadata(claim) is None:
            continue
        bases = []
        if claim.get('subject_basis_claim_id'):
            from protagine.beliefs.source_projection import subject_basis
            basis = subject_basis(conn, claim, contact_id=contact_id)
            if basis is None:
                continue
            bases.append({'turn_id': basis['turn_id'], 'message_hash': basis['message_hash']})
        if claim.get('value_parts'):
            from protagine.beliefs.value_revision import value_basis
            basis = value_basis(conn, claim, contact_id=contact_id)
            if basis is None:
                continue
            bases.extend({'turn_id': b['turn_id'], 'message_hash': b['message_hash']} for b in basis)
        corrected = tuple(_claim_ref(r[0]) for r in conn.execute(
            'SELECT id FROM source_claims WHERE superseded_by=? OR retracted_by=? ORDER BY id',
            (row['id'], row['id']))) if corrects else ()
        # The quoted span is the premise: a bare value ("17") means nothing to a reader.
        text = str(claim.get('evidence') or claim.get('value') or '')
        result.append((Premise('claim', _claim_ref(row['id']), text[:PREMISE_CHARS], content_key(text),
                               turn_id, message_hash, contact_id, corrects=corrected,
                               at=row['occurred_at'] or row['ingested_at'] or ''), bases))
    return result


class _Sources:
    """Source, admission and retention lookups of one read, cached per connection."""

    def __init__(self, conn, owner_id):
        self.conn, self.owner_id, self._sources, self._admitted = conn, owner_id, {}, {}

    def source(self, turn_id):
        if turn_id not in self._sources:
            row = self.conn.execute('''SELECT * FROM turn_sources s WHERE turn_id=?
                AND NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=s.turn_id)
                AND NOT EXISTS (SELECT 1 FROM source_projection_erasures e WHERE e.turn_id=s.turn_id)''',
                (turn_id,)).fetchone()
            value = None
            if row is not None:
                value = dict(row, messages=json.loads(row['messages_json']))
                value['by_hash'] = {source_message_hash(row['session_id'], m): m for m in value['messages']}
            self._sources[turn_id] = value
        return self._sources[turn_id]

    def admitted(self, turn_id, message_hash, contact_id, *, corrects=False):
        key = (turn_id, message_hash, contact_id, corrects)
        if key not in self._admitted:
            self._admitted[key] = {p.ref: (p, bases) for p, bases in _admitted(
                self.conn, turn_id, message_hash, contact_id, corrects=corrects)}
        return self._admitted[key]

    def current(self, premise):
        p = Premise.from_dict(premise)
        if p.kind == 'outcome':
            return True
        if p.kind == 'claim':
            return p.ref in self.admitted(p.turn_id, p.message_hash, p.contact_id)
        source = self.source(p.turn_id)
        if source is None or (p.contact_id and source['contact_id'] != p.contact_id):
            return False
        message = source['by_hash'].get(p.message_hash)
        return message is not None and (p.kind != 'quote' or p.text in _text(message))

    def retained(self, refs):
        for ref in refs:
            if ref.get('erasure_only') is True:
                # A native owner control precedes the turn finalizer: its identity
                # is an erasure fence, not a retained quote.
                if self.conn.execute('''SELECT 1 FROM source_erasures WHERE contact_id=? AND (turn_id=? OR turn_id IN
                        (SELECT source_turn_id FROM source_projection_erasures WHERE turn_id=?))''',
                        (self.owner_id, ref['turn_id'], ref['turn_id'])).fetchone():
                    return False
                continue
            row = self.conn.execute('''SELECT session_id,messages_json FROM turn_sources s WHERE turn_id=? AND NOT EXISTS
                (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=s.turn_id)''', (ref['turn_id'],)).fetchone()
            if row is None or ref.get('message_hash') not in {
                    source_message_hash(row['session_id'], m) for m in json.loads(row['messages_json'])}:
                return False
        return True


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS self_judgment_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, topic TEXT NOT NULL,
        payload_json TEXT NOT NULL, dependency_json TEXT NOT NULL, supersedes INTEGER,
        processor_json TEXT NOT NULL, created_at REAL NOT NULL, status TEXT NOT NULL,
        source_turn_id TEXT NOT NULL, version TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS self_judgment_heads (
        owner_id TEXT NOT NULL, topic TEXT NOT NULL, revision_id INTEGER NOT NULL,
        PRIMARY KEY(owner_id,topic))''')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(self_judgment_revisions)')}
    for column, kind in (('correction_id', 'TEXT'),
                         ('subject_kind', "TEXT NOT NULL DEFAULT 'topic'"),
                         ('subject', "TEXT NOT NULL DEFAULT ''"),
                         ('audience', "TEXT NOT NULL DEFAULT 'owner'"),
                         ('premises_json', "TEXT NOT NULL DEFAULT '[]'"),
                         ('revise_if', "TEXT NOT NULL DEFAULT ''")):
        if column not in columns:
            conn.execute('ALTER TABLE self_judgment_revisions ADD COLUMN ' + column + ' ' + kind)
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS self_judgment_correction_id ON self_judgment_revisions(owner_id,correction_id) WHERE correction_id IS NOT NULL')
    conn.execute('CREATE INDEX IF NOT EXISTS self_judgment_subject ON self_judgment_revisions(owner_id,subject_kind,subject)')
    conn.execute('''CREATE TABLE IF NOT EXISTS opinion_jobs (
        ref TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('turn','finding','reconsider')),
        contact_id TEXT NOT NULL, enqueued_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, done_at REAL, disposition TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS opinion_jobs_pending ON opinion_jobs(done_at,next_attempt)')
    _migrate_appraisal_judgments(conn)


def _migrate_appraisal_judgments(conn):
    """Move current and withdrawn appraisal judgment heads in as person opinions, then delete the kind.

    Idempotent because it deletes what it moved. History stays in the upgrade backup.
    Migrated rows get no autobiography entry (no ledger writes inside initialization).
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='appraisal_records'").fetchone():
        return
    records = conn.execute('''SELECT r.* FROM appraisal_records r JOIN appraisal_heads h ON h.record_id=r.id
        WHERE r.kind='judgment' ORDER BY r.created_at,r.id''').fetchall()
    for record in records:
        data = json.loads(record['payload_json'] or '{}')
        if not data.get('topic') or record['status'] not in {'current', 'withdrawn', 'reconsidering'}:
            continue
        try:
            topic = normalize_topic(' '.join(f"{data.get('dimension', '')} {data['topic']}".split())[:80])
        except ValueError:
            continue
        dependencies = json.loads(record['dependencies_json'] or '[]')
        cited = {d['message_hash']: d for d in dependencies}
        premises, payload, status = [], {'migrated_from': record['id']}, 'withdrawn'
        if record['status'] == 'current':
            status = 'current'
            for role in ('support', 'contrary'):
                for citation in data.get(role, []):
                    dependency = cited.get(citation.get('handle'))
                    if dependency is None or not citation.get('quote'):
                        continue
                    premises.append(Premise('quote', f"turn:{dependency['source_id']}#{citation['handle']}",
                        citation['quote'][:PREMISE_CHARS], content_key(citation['quote']), dependency['source_id'],
                        citation['handle'], dependency.get('source_contact_id', ''), role=role))
            if not any(p.role == 'support' for p in premises):
                continue
            payload.update(stance=data.get('text', ''), reason=data.get('reason', ''),
                           certainty={'low': 'tentative'}.get(data.get('intensity'), 'moderate'), session_id='')
        else:
            operation = conn.execute('SELECT operation_json FROM appraisal_corrections WHERE record_id=? ORDER BY created_at DESC LIMIT 1',
                                     (record['id'],)).fetchone()
            payload['owner_correction'] = (json.loads(operation[0]) if operation else {}) or {'action': 'withdraw'}
        key = head_key('person', record['subject_id'], topic)
        if conn.execute('SELECT 1 FROM self_judgment_heads WHERE owner_id=? AND topic=?', (record['owner_id'], key)).fetchone():
            continue  # A stance the store already holds is not replaced by an older interpretation.
        cur = conn.execute('''INSERT INTO self_judgment_revisions (owner_id,topic,payload_json,dependency_json,supersedes,
            processor_json,created_at,status,source_turn_id,version,subject_kind,subject,audience,premises_json,revise_if)
            VALUES (?,?,?,?,NULL,?,?,?,?,?,'person',?,'owner',?,'')''', (record['owner_id'], topic, _json(payload),
            _json(_unique({'turn_id': d['source_id'], 'message_hash': d['message_hash']} for d in dependencies)),
            record['processor_json'], record['created_at'], status, record['source_id'], VERSION,
            record['subject_id'], _json([p.as_dict() for p in premises])))
        conn.execute('INSERT INTO self_judgment_heads VALUES (?,?,?)', (record['owner_id'], key, cur.lastrowid))
    judged = "SELECT id FROM appraisal_records WHERE kind='judgment'"
    conn.execute(f'DELETE FROM appraisal_corrections WHERE record_id IN ({judged})')
    conn.execute(f'DELETE FROM appraisal_heads WHERE record_id IN ({judged})')
    conn.execute("DELETE FROM appraisal_records WHERE kind='judgment'")


def enqueue(conn, turn_id, contact_id, messages, *, scope, derive_claims):
    """Ledger hook: a person turn of any contact that learns claims, or a mind finding.

    History imports, checkpoints and the opinion entries themselves are never queued,
    so an opinion never feeds itself.
    """
    kind = None
    if derive_claims and scope == 'person' and any(m.get('role') == 'user' and _text(m).strip() for m in messages):
        kind = 'turn'
    elif (turn_id.startswith('mind:') and turn_id.endswith(':finding') and not turn_id.startswith(ENTRY_PREFIX)
          and len(messages) == 1 and isinstance(messages[0].get('metadata'), dict)
          and messages[0]['metadata'].get('origin') == 'mind' and messages[0]['metadata'].get('event') == 'finding'):
        kind = 'finding'
    if kind:
        conn.execute('INSERT OR IGNORE INTO opinion_jobs(ref,kind,contact_id,enqueued_at) VALUES (?,?,?,?)',
                     (turn_id, kind, contact_id, time.time()))


def erase_removed(conn, turn_id, session_id, retained):
    """Tombstone every revision resting on removed evidence and delete its entries.

    The head keeps pointing at the tombstone, so forgetting never revives an older view.
    """
    hashes = {source_message_hash(session_id, message) for message in retained}
    for row in conn.execute("SELECT id,status,dependency_json,premises_json FROM self_judgment_revisions WHERE status IN ('current','withdrawn','reconsidering')").fetchall():
        refs = json.loads(row['dependency_json'] or '[]') + json.loads(row['premises_json'] or '[]')
        if any(ref.get('turn_id') == turn_id and (ref.get('erasure_only') is True or ref.get('message_hash') not in hashes)
               for ref in refs):
            conn.execute("""UPDATE self_judgment_revisions SET status=?,topic='',payload_json='{}',premises_json='[]',
                dependency_json='[]',revise_if='' WHERE id=?""", ('erased' if row['status'] == 'current' else 'withdrawn', row['id']))
            entries = [f"{ENTRY_PREFIX}{row['id']}:{event}" for event in ENTRY_EVENTS]
            for table in ('turn_source_search', 'turn_sources', 'source_vector_jobs'):
                conn.execute('DELETE FROM ' + table + ' WHERE turn_id IN (?,?,?)', entries)
            conn.execute('DELETE FROM opinion_jobs WHERE ref=?', (f"reconsider:{row['id']}",))
    if not retained:
        conn.execute('DELETE FROM opinion_jobs WHERE ref=?', (turn_id,))


def _words(text):
    return {w for w in re.findall(r'\w+', str(text).casefold()) if len(w) > 3} - {
        'what', 'your', 'this', 'that', 'have', 'with', 'from', 'about', 'would',
        'should', 'think', 'judgment', 'opinion', 'please', 'could', 'there', 'which'}


def _fts_expression(query):
    words = list(dict.fromkeys(word.lower() for word in re.findall(r'\w+', str(query)[:4096])
                               if len(word) > 2 and word.lower() not in _FTS_STOP))[:12]
    return ' OR '.join('"' + word + '"' for word in words)


class SelfJudgments:
    def __init__(self, ledger, *, owner_id, clock=time.time):
        self.ledger, self.owner_id, self.clock = ledger, str(owner_id or ''), clock
        with closing(ledger._connect()) as conn, conn:
            initialize(conn)

    # -- premises -------------------------------------------------------------------
    def source(self, turn_id):
        with closing(self.ledger._connect()) as conn:
            found = _Sources(conn, self.owner_id).source(turn_id)
        if found is None:
            return None
        return {key: found[key] for key in ('turn_id', 'contact_id', 'session_id', 'scope', 'messages')} | {
            'occurred_at': found['occurred_at'] or found['ingested_at']}

    def admitted_premises(self, turn_id):
        with closing(self.ledger._connect()) as conn:
            sources = _Sources(conn, self.owner_id)
            found = sources.source(turn_id)
            if found is None or found['scope'] != 'person':
                return []
            return [premise for message_hash, message in found['by_hash'].items()
                    if message.get('role') == 'user' and _text(message).strip()
                    for premise, _ in sources.admitted(turn_id, message_hash, found['contact_id'], corrects=True).values()]

    def statements(self, turn_id):
        found = self._found(turn_id)
        if found is None:
            return []
        return [Premise('statement', f'turn:{turn_id}#{message_hash}', _text(message)[:PREMISE_CHARS],
                        content_key(_text(message)[:2000]), turn_id, message_hash, found['contact_id'],
                        at=found['occurred_at'] or found['ingested_at'] or '')
                for message_hash, message in found['by_hash'].items()
                if message.get('role') == 'assistant' and _text(message).strip()
                and not any(marker in message for marker in _RUNTIME_MARKERS)]

    def finding_premise(self, turn_id):
        if not turn_id.startswith('mind:') or not turn_id.endswith(':finding') or turn_id.startswith(ENTRY_PREFIX):
            return None
        found = self._found(turn_id)
        if found is None or len(found['by_hash']) != 1:
            return None
        (message_hash, message), = found['by_hash'].items()
        metadata = message.get('metadata') if isinstance(message.get('metadata'), dict) else {}
        if metadata.get('origin') != 'mind' or not _text(message).strip():
            return None
        text = _text(message)
        return Premise('finding', f'finding:{turn_id}', text[:PREMISE_CHARS], content_key(text[:2000]), turn_id,
                       message_hash, found['contact_id'], at=found['occurred_at'] or found['ingested_at'] or '')

    def outcome_premise(self, row):
        def get(name):
            return row.get(name) if isinstance(row, dict) else getattr(row, name, None)
        at = get('failed_at') or get('completed_at') or ''
        identifier = f"intention:{get('id')}"
        return Premise('outcome', identifier, f"{get('outcome') or ''}: {get('failed_reason') or get('result') or ''}"[:PREMISE_CHARS],
                       content_key(identifier), verified=str(get('verified') or ''),
                       at=at.isoformat() if isinstance(at, datetime) else str(at))

    def premise_current(self, premise):
        with closing(self.ledger._connect()) as conn:
            return _Sources(conn, self.owner_id).current(premise)

    def _found(self, turn_id):
        with closing(self.ledger._connect()) as conn:
            return _Sources(conn, self.owner_id).source(turn_id)

    # -- reads ----------------------------------------------------------------------
    def _premises_of(self, conn, row):
        """Stored premises; a legacy row derives them from its support/contrary citations."""
        stored = json.loads(row['premises_json'] or '[]')
        if stored:
            return [Premise.from_dict(p) for p in stored]
        payload = json.loads(row['payload_json'] or '{}')
        result = []
        for role in ('support', 'contrary'):
            for ref in payload.get(role) or []:
                if not isinstance(ref, dict) or not ref.get('turn_id') or not ref.get('message_hash'):
                    continue
                for identifier in ref.get('premise_claim_ids') or []:
                    claim = conn.execute('SELECT data_json FROM source_claims WHERE id=?', (identifier,)).fetchone()
                    data = json.loads(claim[0]) if claim else {}
                    text = str(data.get('evidence') or data.get('value') or '')
                    result.append(Premise('claim', _claim_ref(identifier), text[:PREMISE_CHARS], content_key(text),
                                          ref['turn_id'], ref['message_hash'], self.owner_id, role=role))
                if not ref.get('premise_claim_ids'):
                    source = _Sources(conn, self.owner_id).source(ref['turn_id'])
                    text = _text(source['by_hash'].get(ref['message_hash'], {}))[:PREMISE_CHARS] if source else ''
                    result.append(Premise('quote', f"turn:{ref['turn_id']}#{ref['message_hash']}", text,
                                          content_key(text), ref['turn_id'], ref['message_hash'], self.owner_id, role=role))
        return result

    def _original(self, conn, row):
        """The view an owner control row withdrew (walks past withdrawn/reconsidering rows)."""
        original, seen = row, set()
        while (original is not None and original['status'] in {'withdrawn', 'reconsidering'}
               and original['supersedes'] and original['id'] not in seen):
            seen.add(original['id'])
            original = conn.execute('SELECT * FROM self_judgment_revisions WHERE id=? AND owner_id=?',
                                    (original['supersedes'], self.owner_id)).fetchone()
        return original if original is not None and original['status'] == 'current' and original['topic'] else None

    def _row(self, conn, sources, row, premises=None, *, superseded=False):
        payload = json.loads(row['payload_json'] or '{}')
        result = {'id': row['id'], 'topic': row['topic'], 'subject_kind': row['subject_kind'], 'subject': row['subject'],
                  'audience': row['audience'], 'stance': '', 'reason': '', 'certainty': '', 'revise_if': '',
                  'stance_class': None, 'premises': [], 'supersedes': row['supersedes'], 'created_at': row['created_at'],
                  'source_turn_id': row['source_turn_id'], 'session_id': '', 'status': row['status'],
                  'owner_correction': None, 'processor': json.loads(row['processor_json'] or '{}')}
        if not row['topic']:
            result['status'] = 'withdrawn' if row['status'] == 'withdrawn' else 'erased'
            return result
        view = row
        if row['status'] in {'withdrawn', 'reconsidering'}:
            view = self._original(conn, row)
            result.update(owner_correction=payload.get('owner_correction'), original_id=view['id'] if view else None)
        if view is not None:
            data = json.loads(view['payload_json'] or '{}')
            if premises is None or view is not row:
                premises = self._premises_of(conn, view)
            result.update(stance=data.get('stance', ''), reason=data.get('reason', ''), certainty=data.get('certainty', ''),
                          revise_if=view['revise_if'], stance_class=data.get('stance_class'), session_id=data.get('session_id', ''),
                          premises=[p.as_dict() for p in premises])
        if row['status'] == 'current' and superseded:
            result['status'] = 'superseded'  # revised since: a later revision is the topic's head
        elif row['status'] == 'current' and not (premises and sources.retained(json.loads(row['dependency_json'] or '[]'))
                                                 and all(sources.current(p) for p in premises)):
            result['status'] = 'unsupported_premise'
        return result

    def _head_ids(self, conn):
        return {r[0] for r in conn.execute('SELECT revision_id FROM self_judgment_heads WHERE owner_id=?', (self.owner_id,))}

    def _heads(self, conn, *, subject_kind=None, limit=500):
        query = '''SELECT r.* FROM self_judgment_heads h JOIN self_judgment_revisions r ON r.id=h.revision_id
            WHERE h.owner_id=? AND r.status='current' AND r.topic!='' '''
        args = [self.owner_id]
        if subject_kind:
            query += 'AND r.subject_kind=? '
            args.append(subject_kind)
        return conn.execute(query + 'ORDER BY r.id DESC LIMIT ?', [*args, limit]).fetchall()

    def _head_row(self, conn, key):
        return conn.execute('''SELECT r.* FROM self_judgment_heads h JOIN self_judgment_revisions r ON r.id=h.revision_id
            WHERE h.owner_id=? AND h.topic=?''', (self.owner_id, key)).fetchone()

    def get(self, stance_id):
        with closing(self.ledger._connect()) as conn:
            row = conn.execute('SELECT * FROM self_judgment_revisions WHERE id=? AND owner_id=?', (stance_id, self.owner_id)).fetchone()
            if row is None:
                return None
            head = conn.execute('SELECT 1 FROM self_judgment_heads WHERE owner_id=? AND revision_id=?',
                                (self.owner_id, row['id'])).fetchone()
            return self._row(conn, _Sources(conn, self.owner_id), row, superseded=bool(row['topic']) and head is None)

    def head(self, *, subject_kind, subject='', topic):
        try:
            key = head_key(subject_kind, subject, normalize_topic(topic))
        except ValueError:
            return None
        with closing(self.ledger._connect()) as conn:
            row = self._head_row(conn, key)
            return None if row is None else self._row(conn, _Sources(conn, self.owner_id), row)

    def revisions(self, *, history=False, audience=None, subject_kind=None, limit=100):
        """Current supported heads, or with history every revision (tombstones without text)."""
        with closing(self.ledger._connect()) as conn:
            sources = _Sources(conn, self.owner_id)
            if history:
                query, args = 'SELECT * FROM self_judgment_revisions WHERE owner_id=? ', [self.owner_id]
                if subject_kind:
                    query += 'AND subject_kind=? '
                    args.append(subject_kind)
                rows = conn.execute(query + 'ORDER BY id DESC LIMIT 500', args).fetchall()
            else:
                rows = self._heads(conn, subject_kind=subject_kind)
            result, heads = [], self._head_ids(conn) if history else set()
            for row in rows:
                if audience == 'all' and row['audience'] != 'all':
                    continue
                item = self._row(conn, sources, row, superseded=history and bool(row['topic']) and row['id'] not in heads)
                if history or item['status'] == 'current':
                    result.append(item)
                if len(result) >= limit:
                    break
            return result

    def relevant(self, query, *, audience=None, session_id='', limit=3, semantic_turn_ids=()):
        """Current heads for a query: this session's first, then FTS/semantic rank, then word overlap."""
        heads = {head_key(r['subject_kind'], r['subject'], r['topic']): r
                 for r in self.revisions(audience=audience, limit=500)}
        if not heads or limit <= 0:
            return []
        def revision(turn_id):
            parts = str(turn_id).split(':')
            return int(parts[2]) if len(parts) == 4 and str(turn_id).startswith(ENTRY_PREFIX) and parts[2].isdigit() else None
        with closing(self.ledger._connect()) as conn:
            entries = [r[0] for r in conn.execute('SELECT turn_id FROM turn_sources WHERE turn_id>=? AND turn_id<?',
                                                  (ENTRY_PREFIX, ENTRY_PREFIX[:-1] + ';'))]
            expression = _fts_expression(query)
            lexical = [r[0] for r in conn.execute('''SELECT f.turn_id FROM turn_source_search f
                WHERE turn_source_search MATCH ? AND f.turn_id LIKE 'mind:opinion:%'
                ORDER BY bm25(turn_source_search) LIMIT 50''', (expression,))] if expression else []
            named = {revision(t) for t in [*entries, *lexical, *(semantic_turn_ids or ())]} - {None}
            keys = {r['id']: head_key(r['subject_kind'], r['subject'], r['topic']) for r in conn.execute(
                "SELECT id,subject_kind,subject,topic FROM self_judgment_revisions WHERE owner_id=? AND topic!='' "
                'AND id IN (SELECT value FROM json_each(?))', (self.owner_id, json.dumps(sorted(named))))}

        def key_of(turn_id):
            return keys.get(revision(turn_id))
        entered = {key_of(turn_id) for turn_id in entries}
        scores = {}
        for ranked in (lexical, list(semantic_turn_ids or ())):
            seen = []
            for turn_id in ranked:
                key = key_of(turn_id)
                if key in heads and key not in seen:
                    seen.append(key)
            for rank, key in enumerate(seen):
                scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
        order = [k for k in heads if session_id and heads[k]['session_id'] == session_id]
        order += sorted((k for k in scores if k not in order), key=lambda k: (-scores[k], -heads[k]['id']))
        words = _words(query)
        overlap = {k: len(words & _words(h['topic'] + ' ' + h['stance'])) for k, h in heads.items()
                   if k not in entered and k not in order}
        order += sorted((k for k, n in overlap.items() if n), key=lambda k: (-overlap[k], -heads[k]['id']))
        return [heads[k] for k in order[:limit]]

    def citing(self, refs):
        """Current heads citing any of these refs, including heads whose cited premise is no longer current."""
        wanted = {str(ref) for ref in refs if ref}
        if not wanted:
            return []
        with closing(self.ledger._connect()) as conn:
            sources, result = _Sources(conn, self.owner_id), []
            for row in self._heads(conn):
                premises = self._premises_of(conn, row)
                if wanted & {p.ref for p in premises}:
                    result.append(self._row(conn, sources, row, premises))
            return result

    def unweighed_since(self, since, *, contact_id):
        """Newer turns of this contact the opinion pass has not weighed yet."""
        with closing(self.ledger._connect()) as conn:
            sources, result = _Sources(conn, self.owner_id), []
            for row in conn.execute('''SELECT j.ref,j.enqueued_at,(SELECT c.status FROM source_claim_jobs c WHERE c.turn_id=j.ref) AS claims
                    FROM opinion_jobs j WHERE j.kind='turn' AND j.done_at IS NULL AND j.contact_id=? AND j.enqueued_at>?
                    ORDER BY j.enqueued_at,j.ref''', (contact_id, since)).fetchall():
                if row['claims'] not in (None, 'complete'):
                    result.append({'ref': row['ref'], 'enqueued_at': row['enqueued_at'], 'claims': 'pending'})
                    continue
                found = sources.source(row['ref'])
                if found and any(message.get('role') == 'user' and sources.admitted(row['ref'], message_hash, found['contact_id'])
                                 for message_hash, message in found['by_hash'].items()):
                    result.append({'ref': row['ref'], 'enqueued_at': row['enqueued_at'], 'claims': 'admitted'})
            return result

    def processing(self, limit=10):
        with closing(self.ledger._connect()) as conn:
            return [{'ref': r['ref'], 'kind': r['kind'], 'contact_id': r['contact_id'], 'enqueued_at': r['enqueued_at'],
                     'attempts': r['attempts'], 'next_attempt': r['next_attempt'], 'done_at': r['done_at'],
                     'status': 'pending' if r['done_at'] is None else 'complete',
                     'disposition': 'waiting_source_claims' if r['done_at'] is None and r['kind'] == 'turn'
                     and r['claims'] not in (None, 'complete') else r['disposition']}
                    for r in conn.execute('''SELECT j.*,(SELECT c.status FROM source_claim_jobs c WHERE c.turn_id=j.ref) AS claims
                        FROM opinion_jobs j ORDER BY j.enqueued_at DESC,j.ref DESC LIMIT ?''', (limit,))]

    # -- writes ---------------------------------------------------------------------
    def _validate(self, proposal):
        if proposal.subject_kind not in SUBJECT_KINDS:
            raise _Invalid('subject_kind')
        subject = str(proposal.subject or '')
        if (proposal.subject_kind == 'topic') == bool(subject) or len(subject) > 256:
            raise _Invalid('subject')
        try:
            topic = normalize_topic(proposal.topic)
        except ValueError:
            raise _Invalid('topic') from None
        for name, low, high in (('stance', 1, 500), ('reason', 1, 700), ('revise_if', 0, 200)):
            value = getattr(proposal, name)
            if not isinstance(value, str) or not low <= len(value.strip()) <= high:
                raise _Invalid(name)
        if proposal.certainty not in CERTAINTIES:
            raise _Invalid('certainty')
        if (proposal.stance_class in STANCE_CLASSES) != (proposal.subject_kind == 'approach'):
            raise _Invalid('stance_class')
        try:
            premises = [Premise.from_dict(p) for p in proposal.premises or []]
        except (TypeError, AttributeError):
            raise _Invalid('premise') from None
        if (not premises or len({p.ref for p in premises}) != len(premises) or any(
                p.kind not in FORMING_KINDS | {'quote'} or p.role not in {'support', 'contrary'}
                or not p.ref or len(p.text) > PREMISE_CHARS for p in premises)):
            raise _Invalid('premise')
        if not any(p.role == 'support' for p in premises):
            raise _Invalid('support')
        if not re.fullmatch(r'(?:turn|finding|intention|reconsider):\S.{0,255}', str(proposal.source_ref or '')):
            raise _Invalid('source_ref')
        return replace(proposal, subject=subject, topic=topic, stance=proposal.stance.strip(),
                       reason=proposal.reason.strip(), revise_if=proposal.revise_if.strip(), premises=premises,
                       new_evidence=tuple(proposal.new_evidence or ()))

    def _canonical(self, sources, premise):
        """The stored form of a current premise (claims re-read from their admission), else None."""
        if premise.kind == 'claim':
            found = sources.admitted(premise.turn_id, premise.message_hash, premise.contact_id, corrects=True).get(premise.ref)
            return replace(found[0], role=premise.role) if found else None
        return premise if sources.current(premise) else None

    def _dependencies(self, sources, premises):
        refs = []
        for p in premises:
            if not p.turn_id:
                continue
            refs.append({'turn_id': p.turn_id, 'message_hash': p.message_hash})
            if p.kind == 'claim':
                found = sources.admitted(p.turn_id, p.message_hash, p.contact_id).get(p.ref)
                refs.extend(found[1] if found else [])
        return refs

    def _insert(self, conn, proposal, premises, dependencies, *, supersedes):
        payload = {'stance': proposal.stance, 'reason': proposal.reason, 'certainty': proposal.certainty,
                   'session_id': str(proposal.session_id or '')}
        if proposal.stance_class:
            payload['stance_class'] = proposal.stance_class
        kind, _, rest = proposal.source_ref.partition(':')
        audience = _audience(proposal.subject_kind, premises)
        cur = conn.execute('''INSERT INTO self_judgment_revisions (owner_id,topic,payload_json,dependency_json,supersedes,
            processor_json,created_at,status,source_turn_id,version,subject_kind,subject,audience,premises_json,revise_if)
            VALUES (?,?,?,?,?,?,?,'current',?,?,?,?,?,?,?)''', (self.owner_id, proposal.topic, _json(payload),
            _json(_unique(dependencies)), supersedes, _json(proposal.processor or {}), self.clock(),
            rest if kind in {'turn', 'finding'} else proposal.source_ref, VERSION, proposal.subject_kind, proposal.subject,
            audience, _json([p.as_dict() for p in premises]), proposal.revise_if))
        conn.execute('''INSERT INTO self_judgment_heads VALUES (?,?,?) ON CONFLICT(owner_id,topic)
            DO UPDATE SET revision_id=excluded.revision_id''',
                     (self.owner_id, head_key(proposal.subject_kind, proposal.subject, proposal.topic), cur.lastrowid))
        return cur.lastrowid, audience

    def form(self, proposal):
        try:
            clean = self._validate(proposal)
        except _Invalid as exc:
            return Result('invalid:' + exc.code)
        route = None
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            head = self._head_row(conn, head_key(clean.subject_kind, clean.subject, clean.topic))
            if head is not None and head['topic'] and head['status'] in {'withdrawn', 'reconsidering'}:
                return Result('withdrawn_head', head['id'])
            if head is not None and head['topic'] and head['status'] == 'current':
                route = head['id']  # Re-forming is a revision: the rule cannot be dodged.
            else:
                sources = _Sources(conn, self.owner_id)
                premises = [self._canonical(sources, p) for p in clean.premises]
                if None in premises:
                    return Result('invalid:premise_not_current')
                if clean.session_id and not any(p.kind in REVISING_KINDS for p in premises):
                    for other in self._heads(conn, subject_kind=clean.subject_kind):
                        if (other['subject'] == clean.subject and other['created_at'] > self.clock() - LIMIT_WINDOW_S
                                and json.loads(other['payload_json']).get('session_id') == clean.session_id):
                            return Result('duplicate_topic', other['id'])
                identifier, audience = self._insert(conn, clean, premises, self._dependencies(sources, premises), supersedes=None)
        if route is not None:
            return self.revise(route, replace(clean, new_evidence=tuple(
                p.ref for p in clean.premises if p.kind in REVISING_KINDS)))
        self._entry(identifier, 'formed', f"I formed a view on {clean.topic}{_about(clean.subject_kind, clean.subject)}: "
                    f"{clean.stance} Because: {clean.reason}" + (f" Would change if: {clean.revise_if}" if clean.revise_if else '')
                    + f" [opinion {identifier}]", audience=audience, subject_kind=clean.subject_kind, subject=clean.subject)
        return Result('formed', identifier)

    def revise(self, stance_id, proposal):
        try:
            clean = self._validate(proposal)
        except _Invalid as exc:
            return Result('invalid:' + exc.code, stance_id)
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM self_judgment_revisions WHERE id=? AND owner_id=?', (stance_id, self.owner_id)).fetchone()
            if row is None:
                return Result('invalid:stance', stance_id)
            if not row['topic']:
                return Result('source_erased', stance_id)
            key = head_key(row['subject_kind'], row['subject'], row['topic'])
            if key != head_key(clean.subject_kind, clean.subject, clean.topic):
                return Result('invalid:topic', stance_id)
            head = self._head_row(conn, key)
            sources = _Sources(conn, self.owner_id)
            if head is None:
                return Result('head_changed', stance_id)
            if head['id'] != stance_id:
                # A replay after its own commit finds its evidence already cited.
                cited = {p.ref for p in self._premises_of(conn, head)}
                named = {p.ref for p in clean.premises if p.ref in clean.new_evidence}
                if (named and named <= cited) or (row['correction_id'] is not None and head['supersedes'] == stance_id
                                                  and head['status'] == 'current'):
                    return Result('no_new_premise', head['id'])
                return Result('withdrawn_head' if row['correction_id'] is not None else 'head_changed', head['id'])
            reconsider = row['status'] == 'reconsidering' and clean.owner_reconsider
            if row['status'] == 'withdrawn' or (row['status'] == 'reconsidering' and not reconsider):
                return Result('withdrawn_head', stance_id)
            premises = [p for p in (self._canonical(sources, p) for p in clean.premises) if p is not None]
            if reconsider:
                original = self._original(conn, row)
                earlier = self._premises_of(conn, original) if original is not None else []
                allowed = {p.ref for p in earlier}
                if any(p.ref not in allowed and p.kind not in REVISING_KINDS for p in clean.premises):
                    return Result('invalid:premise', stance_id)
                if len(premises) != len(clean.premises):
                    return Result('invalid:premise_not_current', stance_id)
                merged, new = premises, [p for p in premises if p.ref not in allowed]
                old = json.loads(original['payload_json']).get('stance', '') if original is not None else ''
            else:
                earlier = self._premises_of(conn, row)
                refs, keys = {p.ref for p in earlier}, {p.key for p in earlier}
                new = [p for p in premises if p.ref in clean.new_evidence and p.kind in REVISING_KINDS
                       and p.ref not in refs and p.key not in keys]
                if not new:
                    return Result('no_new_premise', stance_id)
                if not any(set(p.corrects) & refs or (p.kind == 'outcome' and p.verified in VERIFIED_OUTCOMES) for p in new):
                    recent = [r[0] for r in conn.execute('''SELECT created_at FROM self_judgment_revisions
                        WHERE owner_id=? AND subject_kind=? AND subject=? AND topic=? AND supersedes IS NOT NULL
                        AND correction_id IS NULL AND created_at>?''', (self.owner_id, row['subject_kind'], row['subject'],
                                                                        row['topic'], self.clock() - LIMIT_WINDOW_S))]
                    if len(recent) >= REVISIONS_PER_DAY:
                        return Result('rate_limited', stance_id, retry_at=min(recent) + LIMIT_WINDOW_S)
                # The new evidence first, then the data the head rested on (roles kept, so it can
                # never come back as new). The agent's earlier words stay a dependency below, not a
                # premise of the view that replaces them.
                merged = [p for p in premises if p.ref not in refs] + [
                    p for p in earlier if p.kind != 'statement' and sources.current(p)]
                old = json.loads(row['payload_json']).get('stance', '')
            if not any(p.role == 'support' for p in merged):
                return Result('invalid:support', stance_id)
            dependencies = self._dependencies(sources, merged) + json.loads(row['dependency_json'] or '[]')
            identifier, audience = self._insert(conn, clean, merged, dependencies, supersedes=stance_id)
        evidence = '; '.join(p.text[:120] for p in new[:2])
        self._entry(identifier, 'revised', f"I changed my view on {clean.topic}{_about(clean.subject_kind, clean.subject)}: "
                    f"now {clean.stance} (was: {old}). Because: {clean.reason}" + (f" New evidence: {evidence}." if evidence else '')
                    + f" [opinion {identifier}]", audience=audience, subject_kind=clean.subject_kind, subject=clean.subject)
        return Result('revised', identifier)

    def end_reconsideration(self, control_id):
        """The pass found no reasoned replacement: the withdrawn view stays withdrawn."""
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM self_judgment_revisions WHERE id=? AND owner_id=?', (control_id, self.owner_id)).fetchone()
            if row is None or row['correction_id'] is None:
                return Result('invalid:stance', control_id)
            if not row['topic']:
                return Result('source_erased', control_id)
            head = self._head_row(conn, head_key(row['subject_kind'], row['subject'], row['topic']))
            if head['id'] != control_id:
                return Result('no_new_premise' if head['supersedes'] == control_id else 'withdrawn_head', head['id'])
            changed = row['status'] == 'reconsidering'
            if changed:
                conn.execute("UPDATE self_judgment_revisions SET status='withdrawn' WHERE id=?", (control_id,))
            reason = (json.loads(row['payload_json']).get('owner_correction') or {}).get('reason', '')
        if changed:
            self._entry(control_id, 'withdrawn', f"At the owner's request I withdrew my view on {row['topic']}"
                        f"{_about(row['subject_kind'], row['subject'])} [opinion {control_id}]: {reason}",
                        audience=row['audience'], subject_kind=row['subject_kind'], subject=row['subject'])
        return Result('reconsider_withdrawn', control_id)

    def correct(self, revision_id, *, action, correction_id, reason, source_id=None, control_turn_id=None):
        """Explicit owner control, separate from a model-authored stance."""
        if (type(revision_id) is not int or action not in {'withdraw', 'reconsider'} or
                not isinstance(correction_id, str) or not 1 <= len(correction_id) <= 192 or
                not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1500):
            raise ValueError('invalid_judgment_correction')
        operation = {'action': action, 'reason': reason, 'actor': 'owner', 'source_id': source_id,
                     'target_revision_id': revision_id, 'control_turn_id': control_turn_id}
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            prior = conn.execute('SELECT * FROM self_judgment_revisions WHERE owner_id=? AND correction_id=?',
                                 (self.owner_id, correction_id)).fetchone()
            if prior:
                retained = json.loads(prior['payload_json']).get('owner_correction')
                if retained is not None and retained != operation:
                    raise ValueError('judgment_correction_id_conflict')
                return {'revision_id': prior['id'], 'status': prior['status'], 'correction_id': correction_id}
            row = conn.execute('''SELECT r.* FROM self_judgment_revisions r JOIN self_judgment_heads h
                ON h.revision_id=r.id AND h.owner_id=r.owner_id WHERE r.owner_id=? AND r.id=?''', (self.owner_id, revision_id)).fetchone()
            if row is None or not row['topic']:
                raise ValueError('judgment_head_changed')
            refs = json.loads(row['dependency_json'] or '[]')
            if control_turn_id is not None:
                if not isinstance(control_turn_id, str) or not 1 <= len(control_turn_id) <= 256:
                    raise ValueError('invalid_control_turn_id')
                control_ref = {'turn_id': control_turn_id, 'erasure_only': True}
                if not _Sources(conn, self.owner_id).retained([control_ref]):
                    raise ValueError('control_turn_erased')
                refs.append(control_ref)
            if source_id is not None:
                evidence = conn.execute("SELECT * FROM turn_sources WHERE turn_id=? AND contact_id=? AND scope='person'",
                                        (source_id, self.owner_id)).fetchone()
                current = [] if evidence is None else [
                    {'turn_id': source_id, 'message_hash': source_message_hash(evidence['session_id'], m)}
                    for m in json.loads(evidence['messages_json']) if m.get('role') == 'user' and _text(m).strip()]
                if not current:
                    raise ValueError('retained_owner_source_required')
                refs.extend(current)
            status = 'withdrawn' if action == 'withdraw' else 'reconsidering'
            cur = conn.execute('''INSERT INTO self_judgment_revisions (owner_id,topic,payload_json,dependency_json,supersedes,
                processor_json,created_at,status,source_turn_id,version,correction_id,subject_kind,subject,audience,premises_json,revise_if)
                VALUES (?,?,?,?,?,'{}',?,?,?,?,?,?,?,?,'[]','')''', (self.owner_id, row['topic'], _json({'owner_correction': operation}),
                _json(_unique(refs)), revision_id, self.clock(), status, source_id or '', VERSION, correction_id,
                row['subject_kind'], row['subject'], row['audience']))
            conn.execute('UPDATE self_judgment_heads SET revision_id=? WHERE owner_id=? AND topic=?',
                         (cur.lastrowid, self.owner_id, head_key(row['subject_kind'], row['subject'], row['topic'])))
            if action == 'reconsider':
                conn.execute('INSERT OR IGNORE INTO opinion_jobs(ref,kind,contact_id,enqueued_at) VALUES (?,?,?,?)',
                             (f'reconsider:{cur.lastrowid}', 'reconsider', self.owner_id, self.clock()))
        if action == 'withdraw':
            self._entry(cur.lastrowid, 'withdrawn', f"At the owner's request I withdrew my view on {row['topic']}"
                        f"{_about(row['subject_kind'], row['subject'])} [opinion {cur.lastrowid}]: {reason}",
                        audience=row['audience'], subject_kind=row['subject_kind'], subject=row['subject'])
        return {'revision_id': cur.lastrowid, 'status': status, 'correction_id': correction_id}

    def _entry(self, revision_id, event, text, *, audience, subject_kind, subject):
        """One owner-audience autobiography row per change, shaped like ``Autobiography.record``; never raises."""
        if not self.owner_id or not text.strip():
            return False
        message = {'role': 'assistant', 'content': text.strip(), 'metadata': {
            'origin': 'mind', 'event': 'opinion_' + event, 'opinion_id': revision_id, 'audience': audience,
            'subject_kind': subject_kind, 'subject': subject}}
        try:
            return bool(self.ledger.record_source(
                f'{ENTRY_PREFIX}{revision_id}:{event}', contact_id=self.owner_id, session_id='mind', messages=[message],
                scope='person', occurred_at=datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                derive_claims=False))
        except Exception as error:
            logger.warning('opinion entry not written (%s)', type(error).__name__)
            return False

    # -- the queue ------------------------------------------------------------------
    def next_job(self):
        """The oldest eligible job; a turn waits for its claim job unless it is older than 48 h."""
        now = self.clock()
        with closing(self.ledger._connect()) as conn:
            row = conn.execute('''SELECT ref,kind,contact_id,enqueued_at,attempts FROM opinion_jobs j
                WHERE done_at IS NULL AND next_attempt<=? AND (kind!='turn' OR j.enqueued_at<? OR NOT EXISTS
                    (SELECT 1 FROM source_claim_jobs c WHERE c.turn_id=j.ref AND c.status!='complete'))
                ORDER BY enqueued_at,ref LIMIT 1''', (now, now - STALE_JOB_S)).fetchone()
            return dict(row) if row else None

    def finish(self, ref, disposition, *, retry_at=None):
        now = self.clock()
        with closing(self.ledger._connect()) as conn, conn:
            if retry_at is not None:
                conn.execute('UPDATE opinion_jobs SET next_attempt=?,disposition=? WHERE ref=? AND done_at IS NULL',
                             (retry_at, disposition, ref))
            else:
                conn.execute('UPDATE opinion_jobs SET done_at=?,disposition=? WHERE ref=?', (now, disposition, ref))
            conn.execute('DELETE FROM opinion_jobs WHERE done_at IS NOT NULL AND done_at<?', (now - 30 * 86400,))

    def fail(self, ref, code):
        now = self.clock()
        with closing(self.ledger._connect()) as conn, conn:
            row = conn.execute('SELECT attempts FROM opinion_jobs WHERE ref=? AND done_at IS NULL', (ref,)).fetchone()
            if row is None:
                return
            attempts = row['attempts'] + 1
            conn.execute('UPDATE opinion_jobs SET attempts=?,disposition=?,next_attempt=?,done_at=? WHERE ref=?',
                         (attempts, f'failed:{code}', now + 60 * attempts, now if attempts >= JOB_ATTEMPTS else None, ref))

    async def process_one(self, router):
        """The projection worker's 'judgment' reflection: one opinion job, when the pass is installed."""
        try:
            from protagine.mind.opinions import run_one
        except ModuleNotFoundError as exc:
            if exc.name != 'protagine.mind.opinions':
                raise
            return False
        return await run_one(self, router)
