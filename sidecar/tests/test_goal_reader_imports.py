"""Real API/server imports keep legacy records without loading their planner."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


SIDECAR = Path(__file__).resolve().parents[1]
PLANNERS = ('engine', 'decomposer', 'inference', 'priority', 'queue_bridge', 'replan')


def isolated(tmp_path, code):
    environment = {**os.environ, 'PROTAGINE_STATE_DIR': str(tmp_path / 'state'),
                   'PROTAGINE_SKIP_DOTENV': '1', 'LITELLM_LOCAL_MODEL_COST_MAP': 'True',
                   'HOME': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, '-I', '-B', '-c',
        'import sys\nsys.path.insert(0, sys.argv[1])\n' + code,
        str(SIDECAR)], cwd=tmp_path, env=environment, text=True,
        capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize('entry', ('protagine.api.routers.host', 'protagine.server'))
def test_actual_api_and_server_import_do_not_load_legacy_planner(tmp_path, entry):
    isolated(tmp_path, f'''
import importlib
entry = importlib.import_module({entry!r})
assert entry is not None
from protagine.goals import Goal, GoalStore, GoalNotFoundError
assert Goal.__module__ == 'protagine.goals.models'
assert GoalStore.__module__ == 'protagine.goals.store'
assert GoalNotFoundError.__module__ == 'protagine.goals.store'
for name in {PLANNERS!r}:
    assert 'protagine.goals.' + name not in sys.modules, name
''')
    assert not (tmp_path / 'state' / 'protagine-goals.db').exists()


def test_only_record_exports_remain(tmp_path):
    isolated(tmp_path, """
import importlib.util
import protagine.goals as goals
assert set(goals.__all__) <= set(dir(goals))
for name in ('GoalEngine', 'GoalQueueBridge', 'GoalDecomposer', 'ReplanEngine'):
    assert not hasattr(goals, name), name
for name in ('engine', 'decomposer', 'inference', 'priority', 'queue_bridge', 'replan', 'config'):
    assert importlib.util.find_spec('protagine.goals.' + name) is None, name
""")
