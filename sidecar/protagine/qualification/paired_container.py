"""Disposable, identically provisioned agent containers for paired experiments.

The host grader never sends its oracle. No host directory or Docker socket is
mounted in an agent. Docker may be local or reached through an operator-owned
socket; the model endpoint is explicitly selected, never taken from a live home.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from types import SimpleNamespace
import uuid

from .records import read, write_once
from .paired_trace import MARKER as DIAGNOSTIC_MARKER

RESULT_MARKER = 'PROTAGINE_PAIRED_RESULT:'
IMAGE_PATTERN = r'(?:[a-zA-Z0-9._:/-]+@)?sha256:[a-f0-9]{64}'
LIMITS = {'cpus': 2, 'memory_bytes': 4294967296, 'pids': 256,
          'state_bytes': 536870912}


def docker_command(spec):
    image, host = spec.get('image', ''), spec.get('docker_host')
    if not re.fullmatch(IMAGE_PATTERN, image):
        raise ValueError('Use an installed digest-pinned benchmark image')
    if host is not None and (not isinstance(host, str) or not re.fullmatch(
            r'(?:unix:///[^\s]+|tcp://127\.0\.0\.1:[0-9]{1,5})', host)):
        raise ValueError('Use a local or explicitly forwarded Docker socket')
    return ['docker'] + (['--host', host] if host else [])


def docker_environment(home):
    # No ambient Docker context, registry credential store, agent home or API key.
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL') if k in os.environ}
    return {**env, 'HOME': str(home), 'DOCKER_CONFIG': str(home / 'docker-config')}


def container_args(spec, name, *, network='bridge'):
    return [*docker_command(spec), 'run', '--pull=never', '--name', name, '--interactive',
        '--read-only', '--user', '65534:65534', '--runtime', 'runc',
        '--cap-drop=ALL', '--security-opt', 'no-new-privileges',
        '--cpus', str(LIMITS['cpus']), '--memory', str(LIMITS['memory_bytes']),
        '--pids-limit', str(LIMITS['pids']), '--network', network,
        '--tmpfs', f"/state:rw,exec,size={LIMITS['state_bytes']},mode=1777",
        '--tmpfs', '/tmp:rw,noexec,size=64m,mode=1777',
        '--workdir', '/state', '--env', 'HOME=/state/home', '--env', 'HERMES_HOME=/state/home',
        '--env', 'NVIDIA_VISIBLE_DEVICES=void', '--env', 'PYTHONDONTWRITEBYTECODE=1',
        '--env', 'PYTHONUNBUFFERED=1', '--entrypoint', 'python', spec['image']]


def configuration(path, binding, *, image, docker_host=None):
    from . import native
    # Selecting a provider does not read a deployed Hermes home. Runtime evidence
    # below comes from the image, replacing the local-interpreter probe entirely.
    selected, recipe = native.configuration(path, binding, inspect_runtime=False)
    spec = {'image': image, 'docker_host': docker_host}
    name = 'protagine-paired-inspect-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='protagine-paired-inspect-') as raw_home:
        env = docker_environment(Path(raw_home))
        command = container_args(spec, name, network='none')
        try:
            inspected = subprocess.run([*docker_command(spec), 'image', 'inspect', image],
                capture_output=True, text=True, timeout=15, check=True, env=env)
            metadata = json.loads(inspected.stdout)[0]
            if metadata.get('Config', {}).get('Volumes'):
                raise ValueError('Benchmark images must not declare automatic volumes')
            result = subprocess.run([*command, '-I', '-B', '-m',
                'protagine.qualification.paired_worker', '--inspect'],
                capture_output=True, text=True, timeout=45, check=True, env=env)
            payload = json.loads(result.stdout)
        finally:
            cleanup = subprocess.run([*docker_command(spec), 'rm', '--force', name],
                capture_output=True, timeout=15, env=env)
            if cleanup.returncode and b'No such container' not in cleanup.stderr:
                raise RuntimeError('Inspection container cleanup is unconfirmed')
    runtime = payload['native_runtime']
    if not runtime.get('native_payload_sha256') or not payload.get('worker_sha256'):
        raise ValueError('Container has no verifiable native benchmark runtime')
    spec.update(image_id=metadata['Id'], architecture=metadata['Architecture'],
                os=metadata['Os'], limits=dict(LIMITS), host_mounts=[])
    recipe.update(native_runtime={**runtime, 'status': 'ready'},
        consumer='disposable_paired_native_container', container=spec,
        container_payload=payload, native_worker_sha256=payload['worker_sha256'],
        basis='Same immutable image and isolated fresh state per arm and episode; '
              'inference endpoint may be shared; no live-agent files or services mounted')
    return selected, recipe


def context(config, recipe):
    return SimpleNamespace(binding=recipe['binding'], native_config=config,
                           container_spec=recipe['container'])


def _result_from_log(path):
    # Worker limits its artifact snapshot; logs are private diagnostics only.
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 4 * 1024 * 1024))
        lines = stream.read().splitlines()
    rows = [line[len(RESULT_MARKER):] for line in lines if line.startswith(RESULT_MARKER.encode())]
    if len(rows) != 1:
        raise ValueError('Container did not emit one benchmark result')
    return json.loads(rows[0])


def _extract_diagnostics(log_path):
    """Keep private traces separately; they never enter scored artifact snapshots."""
    target = log_path.with_name('private-trace.jsonl')
    marker = DIAGNOSTIC_MARKER.encode()
    handle = None
    try:
        with log_path.open('rb') as source:
            for line in source:
                if not line.startswith(marker):
                    continue
                if handle is None:
                    handle = target.open('xb')
                    target.chmod(0o600)
                handle.write(line[len(marker):])
    finally:
        if handle is not None:
            handle.close()
    return target if handle is not None else None


async def consume(inputs, context):
    spec = context.router.container_spec
    state = context.state_dir
    state.chmod(0o700)
    env = docker_environment(state)
    name = 'protagine-paired-' + uuid.uuid4().hex
    config = context.router.native_config
    from .native import _environment
    # Resolve only credentials explicitly named by the supplied candidate config.
    provider_env = _environment(state, config)
    forwarded = {k: v for k, v in provider_env.items() if k not in
                 {'HOME', 'HERMES_HOME', 'PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT',
                  'HERMES_SKIP_DOTENV', 'PYTHONNOUSERSITE', 'PYTHONUNBUFFERED'}}
    payload = {'binding': context.router.binding, 'config': config,
               'provider_env': forwarded, 'inputs': inputs}
    write_once(state / 'container-request.json', payload)
    command = [*container_args(spec, name), '-I', '-B', '-m',
               'protagine.qualification.paired_worker']
    command[len(docker_command(spec))] = 'create'
    # Immutable private diagnostics survive removal of disposable agent state.
    log_path = state.parent / 'container.log'
    started = time.monotonic()
    result, proc, spawning, creating = {}, None, None, None
    creation_settled = False
    observation = {'boundary': 'paired_native_container', 'role': inputs['role'],
        'selected_binding': None, 'returned_model': None, 'prior_attempts': None,
        'arm': inputs['arm'], 'image_id': spec['image_id'], 'outcome': 'starting',
        'state_isolation': 'fresh_tmpfs_per_arm_and_episode', 'host_mounts': [],
        'resource_limits': dict(LIMITS), 'container_removed': False}
    context.state_cleanup_safe = False
    try:
        # Creation finishes before attaching input. Cancellation can therefore
        # await this bounded operation before removing the one owned container,
        # rather than racing a still-starting `docker run` command.
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(*command, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
        creator = await asyncio.shield(spawning)
        creating = asyncio.create_task(creator.communicate())
        created, error = await asyncio.shield(creating)
        creation_settled = True
        if creator.returncode or not re.fullmatch(rb'[a-f0-9]{64}\s*', created):
            raise RuntimeError('Cannot create disposable benchmark container')
        with log_path.open('xb') as log:
            log_path.chmod(0o600)
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                *docker_command(spec), 'start', '--attach', '--interactive', name, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=log, stderr=log))
            proc = await asyncio.shield(spawning)
            await proc.communicate(json.dumps(payload, allow_nan=False).encode())
        _extract_diagnostics(log_path)
        result = _result_from_log(log_path)
        write_once(state.parent / 'container-result.json', result)
        observation.update(outcome=result.get('stage', 'error'), exit_code=proc.returncode,
            error_type=result.get('error_type'), error_origin_stage=result.get('error_origin_stage'))
    except asyncio.CancelledError:
        observation['outcome'] = 'cancelled'
        raise
    finally:
        if not creation_settled and spawning is not None:
            creator = await asyncio.shield(spawning)
            if creating is None:
                creating = asyncio.create_task(creator.communicate())
            try:
                await asyncio.wait_for(asyncio.shield(creating), 15)
                creation_settled = True
            except TimeoutError:
                creator.kill()
                await creator.wait()
        elif proc is None and spawning is not None:
            proc = await asyncio.shield(spawning)
        # Delete only the container named by this attempt, also after interrupted
        # docker run. No global Docker prune, daemon reset or live-agent action.
        cleanup = await asyncio.create_subprocess_exec(*docker_command(spec), 'rm', '--force', name,
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(cleanup.communicate(), 15)
            removed = creation_settled and (cleanup.returncode == 0 or b'No such container' in err)
        except TimeoutError:
            cleanup.kill()
            await cleanup.wait()
            removed = False
        if proc and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if log_path.exists() and not log_path.with_name('private-trace.jsonl').exists():
            _extract_diagnostics(log_path)
        observation.update(container_removed=removed, elapsed_ms=round((time.monotonic()-started)*1000, 3))
        context.state_cleanup_safe = removed
        requests = result.get('tool_evidence', {}).get('model_requests', [])
        returned = {m for row in requests for m in row.get('returned_models', [])}
        successful = [row for row in requests if isinstance(row.get('status'), int)
                      and 200 <= row['status'] < 300]
        # Auxiliary work can still be in flight when an episode ends. Its
        # dispatched model is known, but its output/usage is not. Do not invent
        # a response, or require unfinished background work to supply one to
        # attribute the native conversation's already completed requests.
        if (requests and successful and len(returned) == 1 and all(
                row.get('model') == config['model']['default'] for row in requests)
                and all(row.get('returned_models') for row in successful)):
            observation.update(selected_binding=context.router.binding, returned_model=next(iter(returned)),
                prior_attempts=[], attribution_basis='serialized_requests_and_returned_models',
                dispatch_observed=True, unreturned_requests=sum(not row.get('returned_models') for row in requests),
                attribution_limit='Unreturned auxiliary calls establish requested model only; '
                                  'their completion and usage remain unknown')
        context.observe(observation)
    if proc.returncode != 0 or result.get('stage') != 'returned':
        raise RuntimeError('Paired container did not return a completed native episode')
    return {'output': result.get('output'), 'effects': {
        **result.get('tool_evidence', {}), 'container_removed': observation['container_removed'],
        'image_id': spec['image_id'], 'state_isolation': observation['state_isolation']}}


CONSUMERS = {'native_paired': consume}
