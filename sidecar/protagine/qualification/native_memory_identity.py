"""Inspect installed memory consumer bytes without importing its plugin state."""
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sys


def inspect_runtime(names=('protagine', 'protagine_hermes', 'protagine_memory')):
    packages = {}
    for name in names:
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.submodule_search_locations:
            raise ModuleNotFoundError(name)
        files = {}
        for index, location in enumerate(spec.submodule_search_locations):
            root = Path(location)
            for path in sorted(root.rglob('*')):
                relative = path.relative_to(root)
                if path.is_file() and '__pycache__' not in relative.parts and path.suffix not in {'.pyc', '.pyo'}:
                    files[f'{index}/{relative.as_posix()}'] = hashlib.sha256(path.read_bytes()).hexdigest()
        packages[name] = {'files': len(files), 'sha256': hashlib.sha256(
            json.dumps(files, sort_keys=True).encode()).hexdigest()}
    versions = {row.metadata['Name']: row.version for row in importlib.metadata.distributions()}
    return {'python_version': sys.version.split()[0], 'packages': packages,
            'distribution_versions': dict(sorted(versions.items()))}


if __name__ == '__main__':
    print(json.dumps(inspect_runtime()))
