"""Install a qualified Hermes patchset beside an existing runtime.

The source and environment are disposable. No profile, service, running process,
or active interpreter selection changes until the caller attaches the result.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .hermes_capabilities import probe_runtime, require_capabilities
from .hermes_patches import DEFAULT_PATCHSET, describe_patchset, inspect_runtime, stage_runtime


def _run(command, *, cwd=None, timeout=600):
    result = subprocess.run([str(item) for item in command], cwd=cwd,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Package-manager output may include authenticated registry URLs.
        raise ValueError(f'Hermes preparation failed: {Path(str(command[0])).name} '
                         f'{command[1]} exited {result.returncode}; active runtime retained')
    return result.stdout


def default_destination(patchset_id=DEFAULT_PATCHSET):
    describe_patchset(patchset_id)  # Validate the identifier before using it as a path.
    return Path.home()/'.local/share/protagine/hermes'/patchset_id


def source_for_interpreter(python):
    output = _run([python, '-I', '-B', '-c',
        'import importlib.util,json; s=importlib.util.find_spec("hermes_cli"); '
        'print(json.dumps(s.origin if s else None))'], timeout=15)
    origin = json.loads(output.splitlines()[-1])
    if not origin:
        raise ValueError('Hermes is not installed in the selected interpreter')
    return Path(origin).resolve().parent.parent



def _source_inventory(root):
    """Hash the complete staged source, excluding only installer/cache output."""
    files = {}
    for folder, directories, names in os.walk(root):
        base = Path(folder)
        directories[:] = sorted(name for name in directories
            if name not in {'__pycache__', '.pytest_cache'}
            and not (base == root and name in {'.venv', 'hermes_agent.egg-info'}))
        if any((base/name).is_symlink() for name in directories):
            raise ValueError('Hermes runtime source contains a directory symlink')
        for name in sorted(names):
            if base == root and name == '.protagine-runtime.json':
                continue
            path = base/name
            if path.is_symlink() or not path.is_file():
                raise ValueError('Hermes runtime source contains a non-regular file')
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            files[path.relative_to(root).as_posix()] = digest
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'sha256': digest, 'file_count': len(files)}


def prepare_runtime(*, source=None, destination=None, python=None, patchset_id=DEFAULT_PATCHSET):
    """Stage exact source, install native dependencies, then run offline probes.

    A failed candidate remains unselected. Reuse rechecks the complete source
    digest, patch hashes, dependencies and behavior before returning its interpreter.
    """
    manifest = describe_patchset(patchset_id)
    root = Path(destination or default_destination(patchset_id)).expanduser().absolute()
    if root.is_symlink():
        raise ValueError('Choose a dedicated runtime directory, not a symlink')
    receipt_path = root/'.protagine-runtime.json'
    reused = root.exists()
    if reused:
        if source:
            revision = _run(['git', '-C', source, 'rev-parse', 'HEAD']).strip()
            if revision != manifest['official_revision']:
                raise ValueError('Unsupported Hermes update; qualify a new patchset before switching runtimes')
            if _run(['git', '-C', source, 'status', '--porcelain', '--untracked-files=no']).strip():
                raise ValueError('Hermes source has tracked modifications; source was not changed')
        inspection = inspect_runtime(root, patchset_id=patchset_id)
        if inspection['status'] != 'patched' or not receipt_path.is_file():
            raise ValueError('Runtime destination is occupied or incomplete; choose a new directory')
        prior = json.loads(receipt_path.read_text())
        if (prior.get('schema') != 'protagine.hermes-runtime.v1'
                or prior.get('patchset') != patchset_id
                or prior.get('official_revision') != manifest['official_revision']):
            raise ValueError('Existing runtime receipt belongs to a different source or patchset')
        source_inventory = _source_inventory(root)
        if source_inventory != prior.get('source_inventory'):
            raise ValueError('Existing Hermes source tree changed; prepare a new runtime directory')
    else:
        root.parent.mkdir(parents=True, exist_ok=True)
        if source:
            stage_runtime(source, root, patchset_id=patchset_id)
        else:
            with tempfile.TemporaryDirectory(prefix='protagine-hermes-source-') as temporary:
                checkout = Path(temporary)/'official'
                _run(['git', 'init', '-q', checkout])
                _run(['git', '-C', checkout, 'remote', 'add', 'origin', manifest['official_repository']])
                _run(['git', '-C', checkout, 'fetch', '--depth=1', 'origin', manifest['official_revision']])
                _run(['git', '-C', checkout, 'checkout', '--detach', 'FETCH_HEAD'])
                stage_runtime(checkout, root, patchset_id=patchset_id)
    if not reused:
        source_inventory = _source_inventory(root)
    native = root/'.venv'/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if reused and prior.get('python') != str(native):
        raise ValueError('Existing runtime receipt selects a different interpreter')
    if not reused:
        print(f'Installing Hermes {manifest["package_version"]} with {patchset_id}: {root}', flush=True)
        _run([python or sys.executable, '-I', '-m', 'venv', root/'.venv'])
        _run([native, '-I', '-m', 'pip', 'install', '--disable-pip-version-check', '-e', root], timeout=1200)
    _run([native, '-I', '-m', 'pip', 'check'])
    if source_for_interpreter(native) != root.resolve():
        raise ValueError('Candidate interpreter imports another Hermes source; active runtime retained')
    report = probe_runtime(native)
    require_capabilities(report, manifest['features'])
    if _source_inventory(root) != source_inventory:
        raise ValueError('Hermes source changed during qualification; active runtime retained')
    packages = json.loads(_run([native, '-I', '-m', 'pip', 'list', '--format=json']))
    receipt = {'schema': 'protagine.hermes-runtime.v1', 'patchset': patchset_id,
               'official_revision': manifest['official_revision'], 'python': str(native),
               'source_inventory': source_inventory,
               'capabilities': report, 'packages': packages, 'activation_state': 'not_activated'}
    # A new successful receipt replaces only our own previous receipt.
    fd, temporary = tempfile.mkstemp(prefix='.runtime-receipt-', dir=root)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(receipt, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, receipt_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return native


def add_parser(sub):
    parser = sub.add_parser('hermes', help='Prepare and inspect supported Hermes runtimes')
    commands = parser.add_subparsers(dest='hermes_command', required=True)
    prepare = commands.add_parser('prepare', help='Install official Hermes plus the shipped patches in a separate directory')
    prepare.add_argument('--source', type=Path, help='Clean official Hermes Git checkout; otherwise download the qualified official revision')
    prepare.add_argument('--destination', type=Path, help='New candidate directory; defaults to ~/.local/share/protagine/hermes/PATCHSET')
    prepare.add_argument('--python', help='Interpreter used to create the candidate environment')
    prepare.add_argument('--patchset', default=DEFAULT_PATCHSET)
    check = commands.add_parser('check', help='Check patched files and offline native behavior without changing selection')
    check.add_argument('runtime', type=Path)
    check.add_argument('--patchset', default=DEFAULT_PATCHSET)
    run = commands.add_parser('run', help='Run Hermes using the selected private instance runtime and profile')
    import argparse
    run.add_argument('hermes_args', nargs=argparse.REMAINDER)


def run(args):
    if args.hermes_command == 'prepare':
        native = prepare_runtime(source=args.source, destination=args.destination,
                                 python=args.python, patchset_id=args.patchset)
        print(f'Qualified interpreter: {native}')
        print(f'Attach with: protagine init --hermes-python {str(native)!r}')
        print('Existing services and profiles remain selected until attachment and gateway cutover.')
        return 0
    if args.hermes_command == 'check':
        inspection = inspect_runtime(args.runtime, patchset_id=args.patchset)
        print(json.dumps(inspection, indent=2))
        if inspection['status'] != 'patched':
            return 1
        native = args.runtime/'.venv'/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        report = probe_runtime(native)
        print(json.dumps(report, indent=2))
        require_capabilities(report, describe_patchset(args.patchset)['features'])
        return 0
    from .util.instance import load_environment
    load_environment()
    if not os.environ.get('PROTAGINE_STATE_DIR'):
        raise ValueError('Select a private instance with protagine --instance PATH hermes run')
    state = Path(os.environ['PROTAGINE_STATE_DIR']).expanduser()
    manifest = json.loads((state/'instance.json').read_text())
    native = Path(manifest['hermes_python'])
    features = manifest.get('hermes_capabilities', {}).get('required_features', ['core'])
    require_capabilities(probe_runtime(native), features)
    environment = {**os.environ, 'HERMES_HOME': manifest['hermes_home'],
                   'VIRTUAL_ENV': str(native.parent.parent),
                   'PATH': str(native.parent)+os.pathsep+os.environ.get('PATH', '')}
    parameters = args.hermes_args
    if parameters[:1] == ['--']:
        parameters = parameters[1:]
    return subprocess.call([str(native.parent/'hermes'), *parameters], env=environment)
