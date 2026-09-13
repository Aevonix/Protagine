"""Carry real gateway attachments to the matching admitted native turn.

The pre-dispatch hook snapshots metadata only. Files are read only after the
native participant, chat and provider message match; message text is never a
file locator. This bounded carrier is transient, not another media store.
"""
from collections import OrderedDict
import base64
import copy
import io
import os
from pathlib import Path
import threading
import time


MAX_BYTES = 4 * 1024 * 1024


def _transport():
    from gateway.session_context import get_session_env
    return tuple(str(get_session_env('HERMES_SESSION_' + name, '') or '')
                 for name in ('PLATFORM', 'USER_ID', 'CHAT_ID'))


def _home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()).resolve()


def _fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _image_metadata(path):
    try:
        return _fingerprint(Path(path).stat())
    except OSError:
        return None


def _read_image(path, remaining, observed):
    """Only actual adapter-supplied local image caches; never fetch a URL."""
    selected = Path(path).resolve()
    home = _home()
    if not any(selected.is_relative_to(home / root) for root in ('cache', 'images', 'image_cache')):
        raise ValueError('outside_media_cache')
    with selected.open('rb') as stream:
        if observed is None or _fingerprint(os.fstat(stream.fileno())) != observed:
            raise ValueError('original_changed')
        data = stream.read(min(MAX_BYTES, remaining) + 1)
        if _fingerprint(os.fstat(stream.fileno())) != observed:
            raise ValueError('original_changed')
    if not data or len(data) > min(MAX_BYTES, remaining):
        raise ValueError('image_size_limit')
    # Adapters can use a generic .jpg cache suffix for PNG bytes. Preserve the
    # original codec, rather than making canonical decoding reject that image.
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = Image.MIME.get(image.format)
    except Image.DecompressionBombError as exc:
        raise ValueError('unsupported_image') from exc
    if mime not in {'image/png', 'image/jpeg', 'image/webp'}:
        raise ValueError('unsupported_image')
    return 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii'), len(data)


class TransportMedia:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = OrderedDict()
        self._bound = OrderedDict()

    def observe(self, *, event=None, **_):
        source = getattr(event, 'source', None)
        paths, types = getattr(event, 'media_urls', []), getattr(event, 'media_types', [])
        platform = str(getattr(getattr(source, 'platform', None), 'value', '') or '')
        key = (platform, str(getattr(source, 'user_id', '') or ''),
               str(getattr(source, 'chat_id', '') or ''), str(getattr(event, 'message_id', '') or ''))
        caption = getattr(event, 'text', '')
        if (not all(key) or any(len(v) > 512 for v in key) or not isinstance(caption, str)
                or len(caption) > 32768 or not isinstance(paths, list) or len(paths) > 8):
            return
        images = [(i, path, _image_metadata(path)) for i, path in enumerate(paths) if isinstance(path, str)
                  and len(path) <= 4096 and i < len(types) and str(types[i]).startswith('image/')]
        if not images:
            return
        with self._lock:
            now = time.monotonic()
            self._pending[key] = (now, caption, tuple(images))
            self._pending.move_to_end(key)
            while self._pending and (len(self._pending) > 256 or next(iter(self._pending.values()))[0] < now - 3600):
                self._pending.popitem(last=False)

    def bind(self, scope, kwargs):
        if (scope is None or not scope.valid_participant or kwargs.get('parent_session_id')
                or scope.platform in {'cron', 'subagent', 'background_review', 'pacomind_task'}):
            return
        history = kwargs.get('conversation_history') or []
        current = next((row for row in reversed(history) if isinstance(row, dict) and row.get('role') == 'user'), {})
        message_id = current.get('platform_message_id')
        if not isinstance(message_id, str) or not message_id:
            return
        try:
            platform, sender, chat = _transport()
            if platform != scope.platform or sender != scope.sender_id or not chat:
                return
            key = (platform, sender, chat, message_id)
            native = (scope.session_id, scope.task_id, scope.turn_id)
            if not all(native):
                return
            with self._lock:
                if native in self._bound:
                    return
                entry = self._pending.pop(key, None)
            if entry is None or entry[0] < time.monotonic() - 3600:
                return
            images, remaining = [], MAX_BYTES
            for ordinal, path, observed in entry[2]:
                try:
                    data_url, size = _read_image(path, remaining, observed)
                    remaining -= size
                    images.append({'ordinal': ordinal, 'data_url': data_url})
                except (ImportError, OSError, ValueError):
                    images.append({'ordinal': ordinal, 'unavailable': 'original_unavailable'})
            envelope = {'platform': platform, 'provider_message_id': message_id,
                        'caption': entry[1], 'images': images}
            with self._lock:
                self._bound[native] = (time.monotonic(), envelope)
                while len(self._bound) > 16:
                    self._bound.popitem(last=False)
        except (ImportError, OSError, ValueError):
            return

    def for_turn(self, scope):
        with self._lock:
            entry = self._bound.get((scope.session_id, scope.task_id, scope.turn_id))
            if entry and entry[0] >= time.monotonic() - 3600:
                return copy.deepcopy(entry[1])
        return None
