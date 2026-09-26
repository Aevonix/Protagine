"""Build-time only: agent image contains execution code, never case answers."""
from pathlib import Path
import shutil

root = Path('/opt/protagine')
qualification = root / 'sidecar/protagine/qualification'
keep = {'__init__.py', 'paired_worker.py', 'paired_transport.py', 'paired_trace.py',
        'paired_workflow_runtime.py', 'paired_body.py', 'paired_arms.py', 'paired_history.py',
        'native_memory_worker.py', 'native_identity.py', 'native_memory_identity.py'}
for path in qualification.iterdir():
    if path.name not in keep:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
# The capture platform plugin is runtime code (copied into each profile);
# everything else under benchmarks/ is build or grading material.
for directory, kept in ((root / 'benchmarks', 'paired'), (root / 'benchmarks/paired', 'capture_platform')):
    for path in directory.iterdir() if directory.is_dir() else ():
        if path.name != kept:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
for path in (root / 'tests', root / 'sidecar/tests', Path('/opt/hermes/tests')):
    if path.exists():
        shutil.rmtree(path)
