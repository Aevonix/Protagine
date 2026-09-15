"""Conversation locators beside canonical sources, without changing their hashes."""
from datetime import datetime
import re


AUTOMATION_PLATFORMS = frozenset({'cron', 'subagent', 'background_review', 'protagine_task', 'system'})


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS source_channels (
        turn_id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, platform TEXT NOT NULL,
        conversation_id TEXT NOT NULL, occurred_epoch REAL, ordinal INTEGER NOT NULL,
        basis TEXT NOT NULL)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS source_channel_recent
        ON source_channels(contact_id,platform,occurred_epoch DESC,ordinal DESC,turn_id)''')
    # Predecessor imports already contain reviewed provenance. This index makes
    # the compatibility reader participant/platform bounded, without rewriting
    # any imported source or its version.
    conn.execute('''CREATE INDEX IF NOT EXISTS source_history_channel_recent
        ON turn_sources(contact_id,json_extract(messages_json,'$[0].provenance.platform'),
            julianday(occurred_at) DESC,turn_id)
        WHERE json_extract(messages_json,'$[0].provenance.kind')='hermes_history'
          AND json_extract(messages_json,'$[0].provenance.actor_basis')='reviewed_direct_session' ''')


def epoch(value):
    if not value:
        return None
    try:
        at = datetime.fromisoformat(value)
        return at.timestamp() if at.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def record(conn, *, turn_id, contact_id, messages, occurred_at, channel_id=None):
    """Bind once, in the source writer's transaction; absent metadata stays unknown."""
    basis = 'host_channel'
    ordinal = 0
    if channel_id:
        if not isinstance(channel_id, str) or len(channel_id) > 512:
            return
        platform, separator, conversation = channel_id.partition(':')
        if not separator or not conversation:
            return  # Legacy opaque channel names do not establish a platform.
        conversation_id = channel_id
    else:
        origins = [message.get('provenance', {}) for message in messages]
        if not origins or not all(isinstance(origin, dict)
                and origin.get('kind') == 'hermes_history'
                and origin.get('actor_basis') == 'reviewed_direct_session' for origin in origins):
            return
        pairs = {(origin.get('platform'), origin.get('chat_id')) for origin in origins}
        if len(pairs) != 1:
            return
        platform, conversation = pairs.pop()
        if not isinstance(conversation, str) or not conversation:
            return
        conversation_id = str(platform) + ':' + conversation
        basis = 'reviewed_history'
        ordinal = origins[0].get('message_id', 0)
        ordinal = ordinal if type(ordinal) is int else 0
    if not isinstance(platform, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', platform):
        return
    if platform in AUTOMATION_PLATFORMS:
        return
    existing = conn.execute('SELECT contact_id,platform,conversation_id FROM source_channels WHERE turn_id=?',
                            (turn_id,)).fetchone()
    if existing:
        if tuple(existing) != (contact_id, platform, conversation_id):
            raise ValueError('source_channel_conflict')
        return
    conn.execute('INSERT INTO source_channels VALUES (?,?,?,?,?,?,?)',
                 (turn_id, contact_id, platform, conversation_id, epoch(occurred_at), ordinal, basis))
