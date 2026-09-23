"""Capture platform: a benchmark-only Hermes gateway platform that records deliveries.

Every outbound message, whether a cron delivery, a ``send_message`` tool call or
a gateway send, is appended to one JSON array (``CAPTURE_OUTBOX``, default
``<HERMES_HOME>/outbox.json``) as ``{"target", "text", "at", "via"}``. Nothing is
transmitted and nothing is received. Graders read the array on the host.
"""
import fcntl
import json
import os
from pathlib import Path

NAME = 'capture'
OUTBOX_ENV = 'CAPTURE_OUTBOX'
HOME_CHANNEL_ENV = 'CAPTURE_HOME_CHANNEL'
VIA_PLATFORM = 'platform'  # sent through Hermes (cron delivery, send_message, gateway send)
VIA_REPLY = 'reply'  # the harness recorded the agent's reply to an inbound contact message


def outbox_path():
    configured = os.environ.get(OUTBOX_ENV)
    if configured:
        return Path(configured)
    from hermes_constants import get_hermes_home
    return get_hermes_home() / 'outbox.json'


def read_outbox(path=None):
    path = Path(path or outbox_path())
    if not path.exists():
        return []
    rows = json.loads(path.read_text())
    if not isinstance(rows, list):
        raise ValueError('Capture outbox must be a JSON array')
    return rows


def record(target, text, *, via=VIA_PLATFORM, path=None):
    """Append one delivery; ``at`` follows the Hermes clock so clock advances show.

    Cron runs due jobs concurrently, so the read-append-replace is serialized by a
    file lock every writer shares, whichever module instance or thread it is.
    """
    import hermes_time
    path = Path(path or outbox_path())
    with open(path.with_name(path.name + '.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = read_outbox(path)
        rows.append({'target': str(target), 'text': str(text), 'at': hermes_time.now().isoformat(), 'via': via})
        staging = path.with_name(path.name + '.tmp')
        staging.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
        os.replace(staging, path)
    return rows[-1]


async def standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None,
                          force_document=False):
    """Out-of-process sender used by cron delivery and ``send_message`` without a gateway."""
    record(f'{NAME}:{chat_id}', message)
    return {'success': True, 'message_id': str(len(read_outbox()))}


def parse_target(reference):
    """Any nonempty reference is a chat id; a wrong recipient must be recorded, not rejected."""
    reference = str(reference or '').strip()
    return (reference, None) if reference else None


def adapter_factory(config):
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class CaptureAdapter(BasePlatformAdapter):
        def __init__(self, platform_config):
            super().__init__(platform_config, Platform(NAME))

        async def connect(self, *, is_reconnect=False):
            self._mark_connected()
            return True

        async def disconnect(self):
            self._mark_disconnected()

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            record(f'{NAME}:{chat_id}', content)
            return SendResult(success=True, message_id=str(len(read_outbox())))

        async def get_chat_info(self, chat_id):
            return {'name': str(chat_id), 'type': 'dm'}

    return CaptureAdapter(config)


def register(ctx):
    ctx.register_platform(
        name=NAME, label='Capture', adapter_factory=adapter_factory, check_fn=lambda: True,
        is_connected=lambda config: True, install_hint='No extra packages needed',
        cron_deliver_env_var=HOME_CHANNEL_ENV, standalone_sender_fn=standalone_send,
        parse_target_ref_fn=parse_target, emoji='#')
