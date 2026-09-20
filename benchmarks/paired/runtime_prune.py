"""Build-time only: agent image contains execution code, never case answers."""
from pathlib import Path
import shutil

root = Path('/opt/protagine')
qualification = root / 'sidecar/protagine/qualification'
keep = {'__init__.py', 'paired_worker.py', 'paired_transport.py',
        'native_memory_worker.py', 'native_identity.py', 'native_memory_identity.py'}
for path in qualification.iterdir():
    if path.name not in keep:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
for path in (root / 'tests', root / 'sidecar/tests', root / 'benchmarks',
             Path('/opt/hermes/tests')):
    if path.exists():
        shutil.rmtree(path)
