"""Incomplete native turns retain evidence only through an explicit opt-in."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from protagine.qualification.native import native_cli
from protagine.qualification.runner import RunContext


@pytest.mark.parametrize('stage,allowed,returned', [
    ('incomplete', False, False), ('incomplete', True, True),
    ('error', True, False), ('returned', False, True),
])
def test_incomplete_native_result_is_explicit_and_not_an_execution_error(tmp_path, stage, allowed, returned):
    worker = tmp_path/'controlled-worker.py'
    worker.write_text('''import json,sys
from pathlib import Path
state=Path(sys.argv[1]).parent
stage=json.loads((state/'input.json').read_text())['inputs']['test_stage']
result={'stage':stage,'worker_stopped':True,'agent_close_returned':True,
        'agent_construction_started':True,'output':'partial answer',
        'tool_evidence':{'observed_guard_rejection':True}}
(state/'native-result.json').write_text(json.dumps(result))
raise SystemExit(0 if stage=='returned' else 1)
''')
    state = tmp_path/'state'
    state.mkdir()
    observed = []
    router = SimpleNamespace(binding='controlled', hermes_python=sys.executable, native_config={'providers': {}})
    context = RunContext(router, state, observed)
    call = native_cli({'role': 'chat', 'cleanup_seconds': 2, 'test_stage': stage}, context,
                      worker=worker, allow_incomplete_results=allowed)
    if returned:
        result = asyncio.run(call)
        assert result['effects']['observed_guard_rejection'] is True
        assert result['effects']['native_turn_complete'] is (stage == 'returned')
    else:
        with pytest.raises(RuntimeError, match='completed result'):
            asyncio.run(call)
    assert observed[0]['native_stage'] == stage
    assert context.state_cleanup_safe is True
