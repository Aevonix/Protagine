"""Host-only workflow checks. Neither this verifier nor its answers enter the agent image."""
import copy
import json

from .paired_cases import _artifact_checks, _assertion, _nonfinite, _object
from .paired_workflow_runtime import PROTOCOL


def artifact_dimensions(raw, spec):
    """Distinguish parse/key-shape compliance from semantic assertions and disclosure."""
    if spec['format'] != 'json':
        # Exact preserved text has no separate JSON shape contract.
        return {'format': isinstance(raw, str), 'semantic': _artifact_checks(raw, spec)}
    try:
        if not isinstance(raw, str) or len(raw.encode()) > 65536:
            raise ValueError('Missing or oversized artifact')
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
        shape = all(_assertion(value, rule) for rule in spec['assertions'] if rule['op'] == 'keys_equal')
    except (ValueError, TypeError, RecursionError, OverflowError):
        return {'format': False, 'semantic': False}
    semantic = copy.deepcopy(spec)
    semantic['assertions'] = [rule for rule in spec['assertions'] if rule['op'] != 'keys_equal']
    return {'format': shape, 'semantic': _artifact_checks(raw, semantic)}


def _lifecycle(lifecycle, contract, turns):
    if not isinstance(lifecycle, dict):
        lifecycle = {}
    phases = lifecycle.get('phases', [])
    bounds = [0, *contract['restart_before'], turns]
    phase_shape = (isinstance(phases, list) and len(phases) == len(bounds) - 1
                   and all(isinstance(row, dict) for row in phases))
    correct_phases = phase_shape and all(
        row.get('index') == index and row.get('start_turn') == start
        and row.get('end_turn_exclusive') == end and row.get('turns_attempted') == end - start
        and row.get('turns_completed') == end - start
        and row.get('stage') == 'returned' and row.get('exit_code') == 0
        and row.get('agent_close_returned') is True and row.get('worker_stopped') is True
        for index, (row, start, end) in enumerate(zip(phases, bounds, bounds[1:])))
    pids = [row.get('pid') for row in phases] if phase_shape else []
    fresh_processes = bool(pids) and all(type(pid) is int and pid > 0 for pid in pids) \
        and len(set(pids)) == len(pids) and all(row.get('worker_pid') == row['pid'] for row in phases)
    preserved = phase_shape and all(
        row.get('state_preserved') is True and isinstance(row.get('state_after'), dict)
        and all(row['state_after'].get('roots', {}).get(key) for key in ('home', 'workspace'))
        and (index == 0 or row.get('state_before') == phases[index - 1].get('state_after'))
        for index, row in enumerate(phases))
    consumed = lifecycle.get('read_failures_consumed', [])
    recoveries = lifecycle.get('read_recoveries', [])
    recovered = isinstance(consumed, list) and isinstance(recoveries, list) and all(
        isinstance(fault, dict) and any(
            isinstance(read, dict) and read.get('path') == fault.get('path')
            and type(read.get('turn_index')) is int and type(fault.get('turn_index')) is int
            and fault['turn_index'] <= read['turn_index'] <= next(
                (index for index in contract['snapshot_after'] if index >= fault['turn_index']), turns - 1)
            for read in recoveries)
        for fault in consumed)
    return {
        'lifecycle:protocol': lifecycle.get('protocol') == PROTOCOL
            and lifecycle.get('restart_kind') == 'graceful_worker_process',
        'lifecycle:declared_restarts': lifecycle.get('restart_before') == contract['restart_before']
            and lifecycle.get('restarts_completed') == len(contract['restart_before']),
        'lifecycle:fresh_processes': bool(fresh_processes),
        'lifecycle:all_phases_completed': bool(correct_phases)
            and lifecycle.get('all_declared_turns_attempted') is True
            and lifecycle.get('all_phases_closed') is True,
        'lifecycle:state_preserved': bool(preserved) and lifecycle.get('state_preserved') is True,
        'lifecycle:declared_faults_exercised': lifecycle.get('read_failures_declared') == contract['read_failures']
            and lifecycle.get('read_failures_consumed') == contract['read_failures'],
        'lifecycle:observed_read_recovery': bool(recovered),
    }


def assess_workflow(effects, oracle):
    contract = oracle['workflow_contract']
    lifecycle = effects.get('workflow', {})
    checks = _lifecycle(lifecycle, contract, oracle['declared_turns'])
    artifacts = effects.get('artifacts', {})
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    for spec in oracle['artifacts']:
        for dimension, passed in artifact_dimensions(artifacts.get(spec['path']), spec).items():
            checks[dimension + ':' + spec['path']] = passed
    snapshots = lifecycle.get('snapshots', {}) if isinstance(lifecycle, dict) else {}
    snapshots = snapshots if isinstance(snapshots, dict) else {}
    checks['lifecycle:declared_snapshots'] = set(snapshots) == {str(i) for i in contract['snapshot_after']}
    for checkpoint in oracle.get('checkpoints', []):
        index = str(checkpoint['turn_index'])
        files = snapshots.get(index, {})
        files = files if isinstance(files, dict) else {}
        for spec in checkpoint['artifacts']:
            for dimension, passed in artifact_dimensions(files.get(spec['path']), spec).items():
                checks[f'checkpoint:{index}:{dimension}:{spec["path"]}'] = passed
    return checks
