"""Process lifecycle and observations for frozen, synthetic paired workflows.

This supervisor runs inside the already disposable benchmark container. It
preserves its two state directories between ordinary worker processes; it never
supplies a transcript, memory answer, hidden tool, or extra inference call.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from .paired_trace import MARKER as TRACE_MARKER, PROTOCOL as TRACE_PROTOCOL

PROTOCOL = 'paired-workflow-runtime-1'
RESULT_MARKER = 'PROTAGINE_PAIRED_RESULT:'
REQUEST_ID_STRIDE = 1 << 32
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_TRACE_BYTES = 8 * 1024 * 1024


def validate_workflow(workflow, episodes):
    """Validate only declared lifecycle events, not scenario answers or grades."""
    if (not isinstance(workflow, dict) or set(workflow) - {
            'restart_before', 'snapshot_after', 'read_failures'}
            or not isinstance(episodes, list) or not 1 <= len(episodes) <= 24):
        raise ValueError('Invalid paired workflow contract')
    result = {}
    for name, minimum in (('restart_before', 1), ('snapshot_after', 0)):
        values = workflow.get(name, [])
        if (not isinstance(values, list) or len(values) > 8
                or any(type(value) is not int or not minimum <= value < len(episodes)
                       for value in values) or values != sorted(set(values))):
            raise ValueError('Invalid workflow ' + name)
        result[name] = list(values)
    # A restart intentionally removes in-process transcript objects. Reusing a
    # session ID would test unspecified resume semantics instead of recollection.
    boundaries = [0, *result['restart_before'], len(episodes)]
    seen = set()
    for start, end in zip(boundaries, boundaries[1:]):
        turns = episodes[start:end]
        if any(not isinstance(turn, dict) or not isinstance(turn.get('session_id'), str)
               or not turn['session_id'] for turn in turns):
            raise ValueError('Workflow restart requires fresh session IDs')
        sessions = {turn['session_id'] for turn in turns}
        if seen.intersection(sessions):
            raise ValueError('Workflow restart requires fresh session IDs')
        seen.update(sessions)
    faults = workflow.get('read_failures', [])
    if not isinstance(faults, list) or len(faults) > 8:
        raise ValueError('Invalid workflow read failures')
    identities = set()
    for fault in faults:
        if not isinstance(fault, dict) or set(fault) != {'turn_index', 'path', 'error'}:
            raise ValueError('Invalid workflow read failure')
        index, path, error = fault['turn_index'], fault['path'], fault['error']
        if (type(index) is not int or not 0 <= index < len(episodes)
                or not isinstance(path, str) or not path or len(path) > 128
                or path in {'.', '..'} or '/' in path or '\\' in path or '\x00' in path
                or not isinstance(error, str) or not 1 <= len(error) <= 512
                or (index, path) in identities):
            raise ValueError('Invalid workflow read failure')
        identities.add((index, path))
    result['read_failures'] = deepcopy(faults)
    return result


class TurnObservations:
    """One-shot faults and snapshots, indexed by original workflow turn."""
    def __init__(self, workflow, *, consumed_before=()):
        self.workflow = workflow
        self.turn_index = None
        self.consumed = []
        self.consumed_before = list(consumed_before)
        self.read_recoveries = []
        self.snapshots = {}

    def read_failure(self, relative_path):
        for fault in self.workflow['read_failures']:
            if (fault['turn_index'] == self.turn_index and fault['path'] == relative_path
                    and fault not in self.consumed):
                self.consumed.append(dict(fault))
                return fault['error']
        return None

    def after_turn(self, workspace, snapshot):
        if self.turn_index in self.workflow['snapshot_after']:
            self.snapshots[str(self.turn_index)] = snapshot(workspace)

    def after_read(self, relative_path, result):
        """Observe real native content delivery after a fault, without copying it."""
        if not any(fault['path'] == relative_path and fault['turn_index'] <= self.turn_index
                   for fault in [*self.consumed_before, *self.consumed]):
            return
        try:
            value = json.loads(result) if isinstance(result, str) else result
        except (ValueError, TypeError):
            return
        # Hermes ReadResult includes content, with error only on failure. A
        # dedup stub or claimed success without content is not a successful read.
        # Text inside content is opaque: the word "error" is not a failure signal.
        if (not isinstance(value, dict) or not isinstance(value.get('content'), str)
                or value.get('error') or value.get('success') is False or value.get('is_error') is True):
            return
        receipt = {'turn_index': self.turn_index, 'path': relative_path}
        if receipt not in self.read_recoveries:
            self.read_recoveries.append(receipt)


def state_fingerprint(home, workspace):
    """Hash closed on-disk state without copying state contents into evidence."""
    digest = hashlib.sha256()
    count = size = 0
    roots = {}
    for name, root in (('home', home), ('workspace', workspace)):
        if not root.exists():
            roots[name] = None
            continue
        if not root.is_dir() or root.is_symlink():
            raise ValueError('Invalid workflow state directory')
        roots[name] = {'device': root.stat().st_dev, 'inode': root.stat().st_ino}
        for path in sorted(root.rglob('*')):
            metadata = path.lstat()
            relative = name + '/' + path.relative_to(root).as_posix()
            if stat.S_ISREG(metadata.st_mode):
                count += 1
                size += metadata.st_size
                if count > 8192 or size > 512 * 1024 * 1024:
                    raise ValueError('Workflow state fingerprint exceeds container bound')
                contents = hashlib.sha256()
                with path.open('rb') as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        contents.update(block)
                record = [relative, metadata.st_size, contents.hexdigest()]
            elif stat.S_ISLNK(metadata.st_mode):
                # Never traverse a memory-store or adapter symlink.
                record = [relative, 'symlink', os.readlink(path)]
            else:
                record = [relative, stat.S_IFMT(metadata.st_mode)]
            digest.update(json.dumps(record, separators=(',', ':')).encode() + b'\n')
    return {'roots': roots, 'sha256': digest.hexdigest(), 'files': count, 'bytes': size}


class TraceForwarder:
    """Keep a single bounded trace and disjoint request IDs across processes."""
    def __init__(self, sink=None):
        self.sink = sink or (lambda line: print(TRACE_MARKER + line, file=sys.stderr, flush=True))
        self.events = self.bytes = self.dropped = self.errors = self.truncated = 0

    def accept(self, line, phase):
        if not line.startswith(TRACE_MARKER):
            return False
        try:
            event = json.loads(line[len(TRACE_MARKER):])
            if not isinstance(event, dict) or event.get('protocol') != TRACE_PROTOCOL:
                raise ValueError('Invalid child diagnostic')
            data = event.get('data')
            if isinstance(data, dict) and 'request_id' in data:
                data['request_id'] = request_id(data['request_id'], phase)
            event.update(sequence=self.events + 1, workflow_phase=phase)
            encoded = json.dumps(event, ensure_ascii=True)
            length = len(encoded.encode()) + 1
            if self.bytes + length > MAX_TRACE_BYTES:
                self.dropped += 1
            else:
                self.sink(encoded)
                self.events += 1
                self.bytes += length
        except Exception:
            self.errors += 1
        return True

    def summary(self):
        return {'protocol': TRACE_PROTOCOL, 'events': self.events, 'bytes': self.bytes,
                'dropped': self.dropped, 'errors': self.errors, 'truncated': self.truncated,
                'scope': 'all workflow phases; private synthetic native evidence'}


def request_id(local, phase):
    if type(local) is not int or not 1 <= local < REQUEST_ID_STRIDE:
        raise ValueError('Invalid phase request identity')
    return phase * REQUEST_ID_STRIDE + local


def run_phase(request, stop, trace, *, command=None):
    """Run a real child, forward diagnostics, retain exactly one bounded result."""
    command = command or [sys.executable, '-I', '-B', '-m',
                          'protagine.qualification.paired_worker', '--workflow-phase']
    phase = request['_workflow_phase']['index']
    with tempfile.TemporaryFile(mode='w+b') as output, tempfile.TemporaryFile(mode='w+b') as error:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=error)
        terminated = None
        try:
            data = json.dumps(request, allow_nan=False).encode()
            while True:
                if stop.is_set():
                    if terminated is None:
                        child.terminate()
                        terminated = time.monotonic()
                    elif time.monotonic() - terminated > 10:
                        child.kill()
                try:
                    child.communicate(data, timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    data = None
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        results = []
        for stream in (output, error):
            stream.seek(0)
            for raw in stream:
                if len(raw) > MAX_RESULT_BYTES:
                    raise ValueError('Workflow child log line exceeded bound')
                line = raw.decode('utf-8', errors='replace').rstrip('\n')
                if line.startswith(RESULT_MARKER):
                    results.append(json.loads(line[len(RESULT_MARKER):]))
                elif not trace.accept(line, phase):
                    print(line, file=sys.stderr, flush=True)
        if len(results) != 1 or not isinstance(results[0], dict):
            raise ValueError('Workflow child did not emit exactly one result')
        return {'pid': child.pid, 'exit_code': child.returncode, 'result': results[0]}


def supervise(request, *, home=Path('/state/home'), workspace=Path('/state/workspace'),
              runner=run_phase, stop=None, trace=None):
    """Aggregate ordinary workers; no later phase runs after a failed turn."""
    inputs = request['inputs']
    episodes = inputs['episodes']
    workflow = validate_workflow(inputs['workflow'], episodes)
    if home.exists() or workspace.exists():
        raise ValueError('Workflow requires initially fresh container state')
    stop, trace = stop or threading.Event(), trace or TraceForwarder()
    effects = {'declared_turns': len(episodes), 'turns_completed': 0, 'turns': [],
        'model_requests': [], 'artifacts': {}, 'workflow': {'protocol': PROTOCOL,
            'phases': [], 'restart_before': workflow['restart_before'],
            'restarts_completed': 0, 'snapshots': {},
            'read_failures_declared': workflow['read_failures'], 'read_failures_consumed': [],
            'read_recoveries': [],
            'all_declared_turns_attempted': False, 'all_phases_closed': False,
            'state_preserved': True, 'restart_kind': 'graceful_worker_process'}}
    lifecycle = effects['workflow']
    result = {'stage': 'preparing', 'agent_close_returned': False, 'worker_stopped': False,
              'tool_evidence': effects}
    boundaries = [0, *workflow['restart_before'], len(episodes)]
    previous = None
    try:
        for phase, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
            if stop.is_set():
                raise InterruptedError('Workflow interrupted')
            before = state_fingerprint(home, workspace)
            preserved = phase == 0 or before == previous
            lifecycle['state_preserved'] &= preserved
            if not preserved:
                raise RuntimeError('Workflow state changed between closed phases')
            child_request = deepcopy(request)
            child_request['inputs']['episodes'] = episodes[start:end]
            child_request['_workflow_phase'] = {'index': phase, 'start_turn': start,
                'workflow': workflow, 'prior_read_failures': deepcopy(lifecycle['read_failures_consumed'])}
            result['stage'] = 'running'
            observed = runner(child_request, stop, trace)
            child, code = observed['result'], observed['exit_code']
            evidence = child.get('tool_evidence', {})
            after = state_fingerprint(home, workspace)
            identity = child.get('workflow_phase', {})
            phase_row = {'index': phase, 'start_turn': start, 'end_turn_exclusive': end,
                'pid': observed['pid'], 'worker_pid': identity.get('pid'), 'exit_code': code,
                'stage': child.get('stage'), 'agent_close_returned': child.get('agent_close_returned'),
                'error_type': child.get('error_type'), 'error_origin_stage': child.get('error_origin_stage'),
                'worker_stopped': child.get('worker_stopped'), 'state_before': before,
                'state_after': after, 'state_preserved': preserved,
                'turns_attempted': len(evidence.get('turns', [])),
                'turns_completed': evidence.get('turns_completed', 0)}
            lifecycle['phases'].append(phase_row)
            if (type(observed['pid']) is not int or observed['pid'] <= 0
                    or identity.get('pid') != observed['pid'] or identity.get('index') != phase
                    or any(row['pid'] == observed['pid'] for row in lifecycle['phases'][:-1])):
                raise RuntimeError('Workflow child identity is unverified')
            if not all(after['roots'].values()):
                raise RuntimeError('Workflow child did not preserve both state directories')
            for field in ('turns', 'model_requests'):
                items = deepcopy(evidence.get(field, []))
                if field == 'model_requests':
                    for item in items:
                        if 'trace_request_id' in item:
                            item['trace_request_id'] = request_id(item['trace_request_id'], phase)
                effects[field].extend(items)
            effects['turns_completed'] += evidence.get('turns_completed', 0)
            effects['artifacts'] = evidence.get('artifacts', {})
            for field in ('native_memory_enabled', 'session_search_enabled', 'treatment_profile', 'limitations'):
                if field in evidence:
                    effects[field] = evidence[field]
            loaded = evidence.get('treatment_loaded', False)
            effects['treatment_loaded'] = loaded if phase == 0 else effects['treatment_loaded'] and loaded
            treatment = evidence.get('treatment')
            if isinstance(treatment, dict):
                combined = effects.setdefault('treatment', {'memory_provider_loaded': True,
                    'context_route_successes': 0, 'recorded_routes': 0, 'request_count': 0})
                combined['memory_provider_loaded'] &= treatment.get('memory_provider_loaded') is True
                for key in ('context_route_successes', 'recorded_routes', 'request_count'):
                    combined[key] += treatment.get(key, 0)
            observations = evidence.get('workflow_observations', {})
            lifecycle['snapshots'].update(observations.get('snapshots', {}))
            lifecycle['read_failures_consumed'].extend(observations.get('read_failures_consumed', []))
            lifecycle['read_recoveries'].extend(observations.get('read_recoveries', []))
            child_trace = child.get('private_diagnostics', {})
            trace.dropped += child_trace.get('dropped', 0)
            trace.errors += child_trace.get('errors', 0)
            trace.truncated += child_trace.get('truncated', 0)
            result['output'] = child.get('output')
            if (code != 0 or child.get('stage') != 'returned'
                    or child.get('agent_close_returned') is not True
                    or child.get('worker_stopped') is not True):
                result.setdefault('private_phase_errors', []).append({'phase': phase,
                    'error_type': child.get('error_type'), 'error_origin_stage': child.get('error_origin_stage'),
                    'private_error_traceback': child.get('private_error_traceback')})
                raise RuntimeError('Workflow child did not close successfully')
            if phase:
                lifecycle['restarts_completed'] += 1
            previous = after
            if evidence.get('turns_completed') != end-start:
                break
        lifecycle['all_declared_turns_attempted'] = len(effects['turns']) == len(episodes)
        lifecycle['all_phases_closed'] = all(row['agent_close_returned'] is True
            and row['worker_stopped'] is True and row['exit_code'] == 0 for row in lifecycle['phases'])
        result.update(stage='returned', agent_close_returned=lifecycle['all_phases_closed'])
    except BaseException as exc:
        result.update(error_origin_stage=result['stage'], stage='error', error_type=type(exc).__name__,
                      private_error_traceback=''.join(traceback.format_exception(exc))[-8192:])
    finally:
        from .paired_transport import usage_summary
        effects['resource_usage'] = usage_summary(effects['model_requests'])
        result['private_diagnostics'] = trace.summary()
        result['worker_stopped'] = True
    return result


def main(request):
    stop = threading.Event()
    previous = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        result = supervise(request, stop=stop)
        print(RESULT_MARKER + json.dumps(result, allow_nan=False), flush=True)
        return 0 if result['stage'] == 'returned' else 1
    finally:
        signal.signal(signal.SIGTERM, previous)
