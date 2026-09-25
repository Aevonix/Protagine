"""Small versioned records. Inputs and independent oracles stay separate."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

SCHEMA = 1
# A case's deadline and output bound. A campaign (``inputs.campaign``, paired_cases) is fifteen
# days of turns and ticks in one container, so it gets its own, larger bounds; a record reader
# accepts the campaign's output together with the attempt metadata around it.
MAX_CASE_SECONDS = 600
# A generated family may declare a longer episode deadline (``inputs['family_deadline']``), up to this.
MAX_FAMILY_SECONDS = 1800
MAX_CASE_OUTPUT_BYTES = 1024 * 1024
MAX_CAMPAIGN_SECONDS = 14400
MAX_CAMPAIGN_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
BOUNDARIES = {'role_completion', 'cognition_consumer', 'native_hermes', 'retrieval', 'speech', 'media_consumer'}


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + '\n').encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def read(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES:
        raise ValueError('Invalid qualification record')
    return json.loads(path.read_bytes())


def write_once(path, value):
    """Publish complete bytes without replacing an earlier outcome."""
    publish(path, encode(value))


def publish(path, raw):
    """Atomically publish a new private record or derived text report."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.record-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


@dataclass(frozen=True)
class CaseSpec:
    id: str
    version: str
    role: str
    boundary: str
    consumer: str
    evaluator: str
    inputs: dict
    oracle: dict
    required_capabilities: tuple[str, ...] = ()
    timeout_seconds: float = 60
    max_output_bytes: int = 262144
    provenance: str = 'public'
    target_tasks: tuple[str, ...] = ()

    def record(self):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}', self.id):
            raise ValueError('Invalid case ID')
        if self.boundary not in BOUNDARIES or self.provenance not in {'public', 'private'}:
            raise ValueError('Invalid case boundary/provenance')
        if not self.version or not self.role or not self.consumer or not self.evaluator:
            raise ValueError('Case identity is incomplete')
        if not isinstance(self.inputs, dict) or not isinstance(self.oracle, dict):
            raise ValueError('Case input and oracle must be objects')
        campaign = isinstance(self.inputs.get('campaign'), dict)
        declared = self.inputs.get('family_deadline') is not None
        seconds = MAX_CAMPAIGN_SECONDS if campaign else MAX_FAMILY_SECONDS if declared else MAX_CASE_SECONDS
        output = MAX_CAMPAIGN_OUTPUT_BYTES if campaign else MAX_CASE_OUTPUT_BYTES
        if isinstance(self.timeout_seconds, bool) or not .01 <= self.timeout_seconds <= seconds:
            raise ValueError(f'Case deadline must be .01..{seconds} seconds')
        if not 1 <= self.max_output_bytes <= output:
            raise ValueError(f'Case output bound must be 1..{output} bytes')
        if (not isinstance(self.target_tasks, tuple) or len(self.target_tasks) > 16
                or any(not isinstance(task, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,99}', task)
                       for task in self.target_tasks)
                or len(set(self.target_tasks)) != len(self.target_tasks)):
            raise ValueError('Target tasks must be distinct bounded task names')
        value = asdict(self)
        value['required_capabilities'] = list(self.required_capabilities)
        value['target_tasks'] = list(self.target_tasks)
        return {**value, 'sha256': digest(value), 'inputs_sha256': digest(self.inputs),
                'oracle_sha256': digest(self.oracle)}
