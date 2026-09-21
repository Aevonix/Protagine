"""Owned, offline Hermes Docker environments for synthetic coding tasks."""
import base64
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import PurePosixPath
import re
import shlex
import subprocess
import uuid
from unittest.mock import patch

MAX_FILES = 64
MAX_BYTES = 262144
EXTRA_ARGS = ['--read-only', '--user', '65534:65534', '--entrypoint', '',
              '--runtime', 'runc', '--cpus', '1', '--memory', '1024m', '--pids-limit', '128',
              '--tmpfs', '/workspace:rw,exec,size=256m,mode=1777']


def validate_files(files):
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError('Invalid coding repository')
    for name, content in files.items():
        path = PurePosixPath(name)
        if (not isinstance(name, str) or path.is_absolute() or '..' in path.parts
                or name != str(path) or not path.parts or path.parts[0] in {'.git', '__pycache__'}
                or not isinstance(content, str)):
            raise ValueError('Invalid coding fixture file')
    if len(json.dumps(files).encode()) > MAX_BYTES:
        raise ValueError('Coding repository exceeds bound')
    return files


def validate_sandbox(sandbox):
    if (not isinstance(sandbox, dict) or set(sandbox) != {'image', 'docker_host'}
            or not re.fullmatch(r'(?:[a-zA-Z0-9._:/-]+@)?sha256:[a-f0-9]{64}', sandbox.get('image', ''))):
        raise ValueError('Provide a digest-pinned, already installed Docker image')
    host = sandbox['docker_host']
    if host is not None and (not isinstance(host, str)
            or not re.fullmatch(r'(?:unix:///[^\s]+|tcp://127\.0\.0\.1:[0-9]{1,5})', host)):
        raise ValueError('Use a local Docker socket or an explicitly forwarded loopback daemon')
    return sandbox


def configure_environment(sandbox):
    validate_sandbox(sandbox)
    if sandbox['docker_host']:
        os.environ['DOCKER_HOST'] = sandbox['docker_host']
    else:
        os.environ.pop('DOCKER_HOST', None)
    # Never inherit a real Docker context or registry credentials.
    os.environ.pop('DOCKER_CONTEXT', None)
    os.environ['DOCKER_CONFIG'] = os.path.join(os.environ['HOME'], 'docker-config')


def terminal_configuration(sandbox):
    validate_sandbox(sandbox)
    return {'backend': 'docker', 'cwd': '/workspace', 'docker_image': sandbox['image'],
        'container_cpu': 1, 'container_memory': 1024, 'container_disk': 0,
        'container_persistent': False, 'docker_network': False, 'docker_volumes': [],
        'docker_forward_env': [], 'docker_env': {'HOME': '/workspace', 'NVIDIA_VISIBLE_DEVICES': 'void'},
        'docker_extra_args': list(EXTRA_ARGS),
        'docker_mount_cwd_to_workspace': False, 'docker_persist_across_processes': False,
        'docker_orphan_reaper': False, 'docker_shm_size': '64m', 'timeout': 20}


def inspect_environment(env):
    identifier = getattr(env, '_container_id', None)
    if not identifier or not re.fullmatch(r'[a-f0-9]{12,64}', identifier):
        raise RuntimeError('No owned Docker container identity')
    result = subprocess.run([env._docker_exe, 'inspect', identifier], capture_output=True,
                            text=True, timeout=10, check=True)
    item = json.loads(result.stdout)[0]
    host = item['HostConfig']
    if (host.get('NetworkMode') != 'none' or host.get('Privileged')
            or host.get('ReadonlyRootfs') is not True
            or item['Config'].get('User') != '65534:65534'
            or host.get('DeviceRequests') or host.get('Devices')
            or host.get('Memory', 0) != 1024*1024*1024 or host.get('NanoCpus', 0) != 1000000000
            or host.get('PidsLimit') != 128
            or not any(str(option).startswith('no-new-privileges') for option in host.get('SecurityOpt', []))
            or any(mount.get('Type') != 'tmpfs' for mount in item.get('Mounts', []))):
        summary = {key: host.get(key) for key in ('NetworkMode', 'Privileged', 'ReadonlyRootfs',
                   'Memory', 'NanoCpus', 'PidsLimit', 'DeviceRequests', 'Devices', 'SecurityOpt')}
        summary.update(user=item['Config'].get('User'), mount_types=[m.get('Type') for m in item.get('Mounts', [])])
        raise RuntimeError('Coding sandbox inspection failed: '+json.dumps(summary))
    return identifier


def close_environment(env, identifier):
    # Backend cleanup can be asynchronous. Confirm this one owned container is gone.
    env.cleanup(force_remove=True)
    thread = getattr(env, '_cleanup_thread', None)
    if thread:
        thread.join(timeout=35)
    result = subprocess.run([env._docker_exe, 'container', 'ls', '--all', '--quiet',
                             '--no-trunc', '--filter', f'id={identifier}'],
                            capture_output=True, text=True, timeout=10, check=True)
    if result.stdout.strip():
        raise RuntimeError('Owned coding container cleanup is unconfirmed')


@contextmanager
def without_host_mounts():
    # Hermes mounts skill/cache directories even for an empty isolated home.
    # This fixture suppresses only host-asset discovery, preserving the backend,
    # executor, file tools and terminal commands themselves.
    with ExitStack() as stack:
        for name in ('get_credential_file_mounts', 'get_skills_directory_mount', 'get_cache_directory_mounts'):
            stack.enter_context(patch('tools.credential_files.'+name, return_value=[]))
        yield


@contextmanager
def verification_environment(sandbox):
    configure_environment(sandbox)
    from tools.environments.docker import DockerEnvironment
    with without_host_mounts():
        env = DockerEnvironment(image=sandbox['image'], cwd='/workspace', timeout=20,
            cpu=1, memory=1024, disk=0, persistent_filesystem=False,
            task_id='coding-verify-'+uuid.uuid4().hex, volumes=[], forward_env=[],
            env={'HOME': '/workspace', 'NVIDIA_VISIBLE_DEVICES': 'void'},
            network=False, auto_mount_cwd=False, extra_args=list(EXTRA_ARGS),
            persist_across_processes=False, shm_size='64m')
        identifier = getattr(env, '_container_id', None)
        try:
            inspect_environment(env)
            yield env
        finally:
            if identifier:
                close_environment(env, identifier)


def python_command(script, payload):
    encoded = base64.b64encode(json.dumps(payload, allow_nan=False).encode()).decode()
    return 'python3 -I -B -c ' + shlex.quote(script) + ' ' + shlex.quote(encoded)


SEED = '''import base64,json,pathlib,sys
files=json.loads(base64.b64decode(sys.argv[1]))
for name,content in files.items():
 p=pathlib.Path('/workspace')/name
 p.parent.mkdir(parents=True,exist_ok=True)
 p.write_text(content)
print('repository-ready')
'''

SNAPSHOT = '''import json,pathlib
root=pathlib.Path('/workspace');files={}
for p in sorted(root.rglob('*')):
 if '__pycache__' in p.parts or '.git' in p.parts or '.pytest_cache' in p.parts: continue
 if p.is_symlink(): raise ValueError('symlink in repository')
 if p.is_file():
  if p.stat().st_size>262144 or len(files)>=64: raise ValueError('repository bound')
  files[str(p.relative_to(root))]=p.read_text()
raw=json.dumps(files)
if len(raw.encode())>262144: raise ValueError('repository bound')
print(raw)
'''

CALL = '''import base64,importlib,json,sys
query=json.loads(base64.b64decode(sys.argv[1]))
sys.path.insert(0,'/workspace')
try:
 fn=getattr(importlib.import_module(query['module']),query['function'])
 result={'value':fn(*query.get('args',[]),**query.get('kwargs',{})),'exception':None}
except BaseException as exc:
 result={'exception':type(exc).__name__}
print(query['marker']+json.dumps(result,allow_nan=False))
'''


def seed(env, files):
    validate_files(files)
    result = env.execute(python_command(SEED, files), timeout=20)
    if result.get('returncode') != 0 or 'repository-ready' not in result.get('output', ''):
        raise RuntimeError('Synthetic repository seeding failed')


def snapshot(env):
    result = env.execute('python3 -I -B -c '+shlex.quote(SNAPSHOT), timeout=20)
    if result.get('returncode') != 0:
        raise RuntimeError('Synthetic repository capture failed')
    return validate_files(json.loads(result['output']))


def execute_checks(env, checks):
    results = {}
    for check in checks:
        marker = 'coding-result-'+uuid.uuid4().hex+':'
        query = {k: check[k] for k in ('module', 'function', 'args', 'kwargs') if k in check}
        query['marker'] = marker
        response = env.execute(python_command(CALL, query), timeout=5, bounded_capture=True)
        lines = [line[len(marker):] for line in response.get('output', '').splitlines() if line.startswith(marker)]
        result = json.loads(lines[0]) if len(lines) == 1 and response.get('returncode') == 0 else {}
        if 'exception' in check:
            passed = result.get('exception') == check['exception']
        else:
            from .cases import _exact_value
            passed = result.get('exception') is None and 'value' in result and _exact_value(result['value'], check['expected'])
        results[check['id']] = passed
    return results
