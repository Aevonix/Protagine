"""Bounded private diagnostics for synthetic paired runs, outside scored artifacts."""
import json
import sys
import threading
import time

MARKER = 'PROTAGINE_PAIRED_DIAGNOSTIC:'
PROTOCOL = 'paired-private-trace-1'


class DiagnosticTrace:
    def __init__(self, *, secrets=(), sink=None, max_bytes=8 * 1024 * 1024,
                 event_bytes=512 * 1024):
        self._secrets = {s for s in secrets if isinstance(s, str) and len(s) >= 4}
        self._sink = sink or self._stderr
        self.max_bytes, self.event_bytes = max_bytes, event_bytes
        self.lock = threading.RLock()
        self.events = self.bytes = self.dropped = self.truncated = self.errors = 0

    @staticmethod
    def _stderr(line):
        sys.stderr.write(MARKER + line + '\n')
        sys.stderr.flush()

    def add_secret(self, value):
        if isinstance(value, str) and len(value) >= 4:
            with self.lock:
                self._secrets.add(value)

    def _redact(self, value):
        if isinstance(value, dict):
            return {k: ('[REDACTED]' if str(k).lower().replace('-', '_') in {
                'authorization', 'api_key', 'apikey', 'access_token', 'password', 'secret',
                'cookie', 'set_cookie', 'provider_env', 'headers'} else self._redact(v))
                for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(v) for v in value]
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, '[REDACTED]')
        return value

    def record(self, kind, data):
        # Diagnostic errors must not change the agent's response or score.
        with self.lock:
            try:
                payload = json.dumps(self._redact(data), ensure_ascii=True, default=str)
                if len(payload.encode()) > self.event_bytes:
                    self.truncated += 1
                    payload = json.dumps({'truncated': True, 'original_bytes': len(payload.encode()),
                        'prefix': payload[:self.event_bytes // 2]}, ensure_ascii=True)
                event = {'protocol': PROTOCOL, 'sequence': self.events + 1,
                    'monotonic': time.monotonic(), 'thread': threading.current_thread().name,
                    'kind': kind, 'data': json.loads(payload)}
                line = json.dumps(event, ensure_ascii=True)
                size = len(line.encode()) + 1
                if self.bytes + size > self.max_bytes:
                    self.dropped += 1
                    return
                self._sink(line)
                self.bytes += size
                self.events += 1
            except Exception:
                self.errors += 1

    def summary(self):
        with self.lock:
            return {'protocol': PROTOCOL, 'events': self.events, 'bytes': self.bytes,
                    'dropped': self.dropped, 'truncated': self.truncated, 'errors': self.errors,
                    'scope': 'private synthetic requests, responses, context routes and native turns'}
