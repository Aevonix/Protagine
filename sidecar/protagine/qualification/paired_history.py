"""Seeded conversation history, imported without model calls before an episode's first turn.

A generated scenario may carry ``history``: earlier owner sessions the agent is
supposed to remember. Inside the container the worker writes them into Hermes'
own ``state.db`` through the stock session import in every arm, so the base
arm's ``session_search`` and memory see them, and in plugin arms it additionally
retains them in the Protagine ledger through the reviewed history importer
(``protagine.turns.hermes_history``), bound to the fixture owner. No turn is
replayed and nothing is fetched; the histories are fixture bytes.
"""
import json
from pathlib import Path
import re
import sqlite3

PROTOCOL = 'paired-history-1'
# The platform imported sessions are recorded on: a direct message on the capture
# platform, which the history importer accepts (it refuses automation sources).
SOURCE = 'capture'
CHAT_TYPE = 'dm'
ROLES = ('user', 'assistant')
MAX_SESSIONS = 128
MAX_MESSAGES = 1024
MAX_BYTES = 8 * 1024 * 1024
NAMESPACE = 'paired-history'
_LEAF = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')


def validate_history(value):
    """``[{id, at, messages: [{role, content}]}]``; ids are leaf names, ``at`` is epoch seconds."""
    if not isinstance(value, list) or not value or len(value) > MAX_SESSIONS:
        raise ValueError(f'History is 1..{MAX_SESSIONS} sessions')
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ValueError('History exceeds the size bound')
    identities = set()
    for session in value:
        if (not isinstance(session, dict) or set(session) != {'id', 'at', 'messages'}
                or not isinstance(session['id'], str) or not _LEAF.fullmatch(session['id'])
                or session['id'] in identities or type(session['at']) is not int or session['at'] < 0):
            raise ValueError('Invalid history session')
        identities.add(session['id'])
        messages = session['messages']
        if (not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES
                or any(not isinstance(m, dict) or set(m) != {'role', 'content'} or m['role'] not in ROLES
                       or not isinstance(m['content'], str) or not m['content'].strip() for m in messages)):
            raise ValueError('Invalid history messages')
    return value


def hermes_sessions(sessions, *, user_id, chat_id):
    """The stock import payload: closed sessions whose messages keep their stated order and times."""
    payload = []
    for session in sessions:
        at = session['at']
        payload.append({'id': session['id'], 'source': SOURCE, 'user_id': user_id, 'title': session['id'],
                        'started_at': at, 'ended_at': at + len(session['messages']),
                        'end_reason': 'imported',
                        'messages': [{'role': m['role'], 'content': m['content'], 'timestamp': at + index}
                                     for index, m in enumerate(session['messages'])]})
    return payload


def mapping(sessions, *, contact_id, user_id, chat_id):
    """The reviewed binding the ledger importer requires, one per seeded session."""
    return {'version': 1, 'namespace': NAMESPACE, 'reviewed': True, 'bindings': [
        {'session_id': session['id'], 'platform': SOURCE, 'actor_id': user_id, 'chat_id': chat_id,
         'chat_type': CHAT_TYPE, 'contact_id': contact_id,
         'review_evidence': {'kind': 'fixture', 'reference': PROTOCOL}} for session in sessions]}


def seed_hermes(db_path, sessions, *, session_db, user_id, chat_id):
    """Import the sessions into ``state.db`` and stamp the transport columns the importer binds on."""
    db = session_db(Path(db_path))
    try:
        outcome = db.import_sessions(hermes_sessions(sessions, user_id=user_id, chat_id=chat_id))
    finally:
        db.close()
    if not outcome.get('ok') or outcome.get('imported') != len(sessions):
        raise RuntimeError('History import into state.db was incomplete: ' + json.dumps(outcome)[:512])
    with sqlite3.connect(db_path) as conn:
        for session in sessions:
            conn.execute('UPDATE sessions SET chat_id=?, chat_type=? WHERE id=?',
                         (chat_id, CHAT_TYPE, session['id']))
    return {'imported': outcome['imported']}


def snapshot(db_path, target):
    """A consistent copy with no WAL, the only input the ledger importer accepts."""
    with sqlite3.connect(db_path) as source, sqlite3.connect(target) as copy:
        source.backup(copy)
    return Path(target)


def seed_ledger(db_path, sessions, *, state_dir, contact_id, user_id, chat_id):
    """Retain the seeded sessions in the Protagine ledger as owner-scoped sources."""
    from protagine.turns.hermes_history import import_history
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    copy = snapshot(db_path, Path(state_dir) / 'history-snapshot.db')
    document = mapping(sessions, contact_id=contact_id, user_id=user_id, chat_id=chat_id)
    counts, rounds = {}, 0
    while True:
        outcome = import_history(copy, document, state_dir=state_dir, apply=True, limit=10000)
        counts, rounds = outcome['counts'], rounds + 1
        if outcome['complete'] or rounds > 64:
            break
    if not outcome['complete']:
        raise RuntimeError('History import into the ledger did not complete')
    return {'counts': counts, 'models_called': outcome['models_called']}


def seed(home, sessions, *, session_db, contact_id, ledger):
    """Seed every arm's ``state.db``; plugin arms (``ledger``) also retain the sources in the ledger."""
    sessions = validate_history(sessions)
    home = Path(home)
    user_id, chat_id = contact_id, contact_id
    record = {'protocol': PROTOCOL, 'sessions': len(sessions),
              'messages': sum(len(session['messages']) for session in sessions),
              'hermes': seed_hermes(home / 'state.db', sessions, session_db=session_db,
                                    user_id=user_id, chat_id=chat_id)}
    if ledger:
        record['ledger'] = seed_ledger(home / 'state.db', sessions, state_dir=home / 'memory-state',
                                       contact_id=contact_id, user_id=user_id, chat_id=chat_id)
    return record
