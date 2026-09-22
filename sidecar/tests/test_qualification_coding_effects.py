"""Controlled evaluator checks. These are not model performance results."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from protagine.qualification.coding import cases, coding_effects, load_pack
from protagine.qualification.coding_sandbox import snapshot, validate_files, validate_sandbox


SANDBOX = {'image': 'sha256:'+'a'*64, 'docker_host': None}
FIXES = {
    'C01': {'solution.py': '''def solve(items, offset, limit):
 if offset < 0 or limit <= 0: raise ValueError()
 return {'items':items[offset:offset+limit], 'next_offset':offset+limit if offset+limit<len(items) else None}
'''},
    'C02': {'solution.py': '''from datetime import datetime
def solve(events):
 def key(item):
  parsed=datetime.fromisoformat(item['at'].replace('Z','+00:00'))
  if parsed.tzinfo is None: raise ValueError()
  return parsed
 return [item['id'] for item in sorted(events,key=key)]
'''},
    'C03': {'solution.py': '''def solve(attempts, base, cap):
 if min(attempts,base,cap)<0: raise ValueError()
 return [min(cap,base*2**i) for i in range(attempts)]
'''},
    'C04': {'solution.py': '''from decimal import Decimal, ROUND_HALF_UP
def solve(amounts):
 return sum(int(Decimal(a).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)*100) for a in amounts)
'''},
    'C05': {'solution.py': '''import csv,io
def solve(rows):
 target=io.StringIO(newline='')
 writer=csv.writer(target,lineterminator='\\n')
 writer.writerow(['name','note'])
 writer.writerows([row.get('name',''),row.get('note','')] for row in rows)
 return target.getvalue()
'''},
    'C06': {'settings/parse.py': '''def parse_bool(value):
 if isinstance(value,bool): return value
 text=value.strip().lower()
 if text in ('true','yes','on','1'): return True
 if text in ('false','no','off','0'): return False
 raise ValueError()
''', 'settings/config.py': '''from .parse import parse_bool
def solve(cli,env,config):
 if cli is not None: return cli
 if 'ENABLED' in env: return parse_bool(env['ENABLED'])
 return config.get('enabled',False)
'''},
}


def trusted_fixture_checks(files, checks, tmp_path):
    """Only hand-written fixture/control code runs locally, never model output."""
    for name, content in files.items():
        path = tmp_path/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    script = '''import importlib,json,sys
sys.path.insert(0,sys.argv[1]);out={}
for check in json.loads(sys.argv[2]):
 try:
  value=getattr(importlib.import_module(check['module']),check['function'])(*check.get('args',[]),**check.get('kwargs',{}))
  out[check['id']]='expected' in check and type(value)==type(check['expected']) and value==check['expected']
 except BaseException as exc: out[check['id']]=type(exc).__name__==check.get('exception')
print(json.dumps(out))
'''
    response = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(tmp_path), json.dumps(checks)],
                              text=True, capture_output=True, timeout=10, check=True)
    return json.loads(response.stdout)


@pytest.mark.parametrize('task', load_pack()['tasks'], ids=lambda task: task['id'])
def test_development_oracles_reject_originals_and_accept_reference_repairs(task, tmp_path):
    assert not all(trusted_fixture_checks(task['files'], task['checks'], tmp_path/'broken').values())
    repaired = {**task['files'], **FIXES[task['id']]}
    assert all(trusted_fixture_checks(repaired, task['checks'], tmp_path/'fixed').values())


def test_pack_boundary_and_oracles_are_not_sent_to_model():
    pack = load_pack()
    suite = cases(pack, SANDBOX)
    assert len(suite) == 6 and {case.boundary for case in suite} == {'native_hermes'}
    assert all(case.role == 'coding' and case.provenance == 'public' for case in suite)
    assert all('checks' not in case.inputs and 'expected' not in case.inputs for case in suite)
    assert all('AGENTS.md' in case.inputs['repository'] for case in suite)
    suite[0].inputs['repository'].clear()
    assert pack['tasks'][0]['files']
    assert not any('C07' in path.read_text() for path in Path(__file__).parents[1].glob(
        'protagine/qualification/fixtures/coding-*.json'))


def test_private_pack_preserves_private_provenance(tmp_path):
    pack = load_pack()
    pack['split'] = 'held_out'
    path = tmp_path/'pack.json'
    path.write_text(json.dumps(pack))
    assert all(case.provenance == 'private' for case in cases(load_pack(path), SANDBOX))


def test_model_claims_alone_never_pass_and_effects_require_protected_files():
    case = cases(load_pack(), SANDBOX)[0]
    assert not all(coding_effects({'output': 'All tests passed.'}, case.oracle).values())
    effects = {'changed_files': ['solution.py'], 'native_tool_calls': ['write_file', 'terminal'],
        'offline_container_verified': True, 'fresh_verification_container': True, 'cleanup_verified': True,
        'checks': {check['id']: True for check in case.oracle['checks']}}
    assert all(coding_effects({'effects': effects}, case.oracle).values())
    for mutation in ({'changed_files': ['solution.py', 'test_smoke.py']}, {'native_tool_calls': []},
                     {'cleanup_verified': False}, {'checks': {}}, {'changed_files': []}):
        assert not all(coding_effects({'effects': {**effects, **mutation}}, case.oracle).values())


def test_snapshot_ignores_real_pytest_cache_but_preserves_other_edits(tmp_path):
    """Run only the hand-written repair/test fixture, never candidate model code."""
    task = load_pack()['tasks'][0]
    repaired = {**task['files'], **FIXES[task['id']]}
    for name, content in repaired.items():
        (tmp_path / name).write_text(content)
    env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTEST_ADDOPTS': ''}
    result = subprocess.run([sys.executable, '-I', '-B', '-m', 'pytest', '-q',
        '-o', 'cache_dir=.pytest_cache', 'test_smoke.py'], cwd=tmp_path,
        env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / '.pytest_cache/v/cache/nodeids').is_file()

    class LocalSnapshotEnvironment:
        def execute(self, command, timeout):
            # Execute the production capture command with only its workspace remapped.
            args = shlex.split(command)
            assert args[:4] == ['python3', '-I', '-B', '-c']
            script = args[4].replace("pathlib.Path('/workspace')", f'pathlib.Path({str(tmp_path)!r})')
            result = subprocess.run([sys.executable, *args[1:4], script],
                text=True, capture_output=True, timeout=timeout)
            return {'returncode': result.returncode, 'output': result.stdout}

    local = LocalSnapshotEnvironment()
    assert snapshot(local) == repaired
    edits = {'extra.py': 'extra = True\n', '.pytest_cache.py': 'source = True\n',
             '.pytest_cache_source/extra.py': 'source = True\n',
             'test_smoke.py': '# unauthorized protected-file edit\n'}
    for name, content in edits.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    assert snapshot(local) == {**repaired, **edits}


@pytest.mark.parametrize('files', [{}, {'../escape.py': ''}, {'/tmp/escape': ''},
    {'.git/config': ''}, {'a/../solution.py': ''}, {'solution.py': 'a'*300000}])
def test_repository_bounds(files):
    with pytest.raises(ValueError):
        validate_files(files)


@pytest.mark.parametrize('sandbox', [{'image': 'python:latest', 'docker_host': None},
    {'image': SANDBOX['image'], 'docker_host': 'tcp://example.invalid:2375'},
    {**SANDBOX, 'credentials': 'forbidden'}])
def test_sandbox_is_pinned_and_endpoint_is_local(sandbox):
    with pytest.raises(ValueError):
        validate_sandbox(sandbox)


def test_hidden_check_cannot_be_both_value_and_exception(tmp_path):
    pack = deepcopy(load_pack())
    pack['tasks'][0]['checks'][0]['exception'] = 'ValueError'
    path = tmp_path/'pack.json'
    path.write_text(json.dumps(pack))
    with pytest.raises(ValueError):
        load_pack(path)
