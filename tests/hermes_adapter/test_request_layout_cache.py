"""Final native layout is the exact body certified by the memory boundary."""
import importlib.util
import json
import os

import pytest

from conftest import ROOT, run_python
from test_request_work import PROBE as WORK_PROBE


def test_compacted_native_work_request_uses_checked_body(artifacts, tmp_path):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for the native request boundary')
    anchor = 'from hermes_cli.plugins import get_plugin_manager'
    observer = '''
from protagine_hermes.native_memory import NativeMemoryRequests, _content_digest
checked_requests=[]
original_checked=NativeMemoryRequests.checked
def observe_checked(self,request,scope):
    original_checked(self,request,scope)
    key=(scope.session_id,scope.task_id,scope.turn_id) if scope else None
    owned=self._turns.get(key)
    checked_requests.append((_content_digest(request),bool(owned and _content_digest(request) in owned[2])))
NativeMemoryRequests.checked=observe_checked
'''
    assert WORK_PROBE.count(anchor) == 1
    probe = WORK_PROBE.replace(anchor, observer+'\n'+anchor)
    provider_anchor = '    requests.append(copy.deepcopy(kwargs)); index=len(requests)'
    assertions = '''
    assert checked_requests, 'No ordinary native request was certified'
    assert checked_requests[-1][0] == _content_digest(kwargs), 'Certified body differs from final provider body'
    assert checked_requests[-1][1], 'Final body was not entered into the active turn cache'
    assert kwargs['messages'][0]['role']=='system'
    assert all(row.get('role') not in {'system','developer'} for row in kwargs['messages'][1:])
'''
    assert probe.count(provider_anchor) == 1
    probe = probe.replace(provider_anchor, assertions+'\n'+provider_anchor)
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG',
        'PROTAGINE_TEST_HERMES_PATH') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), PROTAGINE_STATE_DIR=str(tmp_path/'protagine'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PROTAGINE_GENERAL_PLUGIN_ACTIVE='1',
        PROTAGINE_MEMORY_WORKER_TOOLS='0', PROTAGINE_MEMORY_TURN_WRITER='disabled',
        PROTAGINE_MEMORY_DEFAULT_CONTEXT_AUTHORITY='owner_system', PROTAGINE_GUARD_CHAT_MODE='off',
        PROTAGINE_OWNER_CONTACT_ID='owner', PROTAGINE_SKIP_DOTENV='1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    result = run_python('-I', '-c', probe, artifacts[3], ROOT/'sidecar',
        os.environ.get('PROTAGINE_TEST_DEPENDENCY_PATH', ''), 'sms', 'task_source', '',
        cwd=tmp_path, env=env)
    outcome = json.loads(result.stdout.splitlines()[-1])
    assert outcome['source_excerpt_and_reader_owned'] is True
    assert outcome['model_requests'] == 2
