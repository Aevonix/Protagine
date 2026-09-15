"""Actual gateway task tools, concurrent roots and cross-channel control.

SDK responses are controlled. This qualifies native integration and provenance,
not model obedience, physical messaging, or production performance.
"""
import importlib.util
import os
from pathlib import Path

import pytest

from conftest import ROOT, run_python


@pytest.mark.parametrize('steer_delivery,submission,tool_form', [
    pytest.param('tool_batch', 'submit', 'deferred', id='tool_batch-submit'),
    pytest.param('next_turn', 'submit', 'deferred', id='next_turn-submit'),
    pytest.param('tool_batch', 'handoff', 'deferred', id='tool_batch-handoff'),
    *[('tool_batch', 'existing_handoff', form)
      for form in ('direct', 'deferred', 'mixed_direct', 'mixed_deferred')]])
def test_native_task_channels(artifacts, tmp_path, steer_delivery, submission, tool_form):
    native = os.environ.get('PROTAGINE_TEST_HERMES_PATH', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native gateway integration')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), PROTAGINE_STATE_DIR=str(tmp_path/'state'),
        PROTAGINE_TEST_STEER_DELIVERY=steer_delivery,
        PROTAGINE_TEST_TASK_SUBMISSION=submission,
        PROTAGINE_TEST_TASK_TOOL_FORM=tool_form,
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PROTAGINE_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1',
        PROTAGINE_GENERAL_PLUGIN_ACTIVE='1', PROTAGINE_MEMORY_WORKER_TOOLS='0',
        PROTAGINE_MEMORY_TURN_WRITER='disabled', PROTAGINE_GUARD_CHAT_MODE='off',
        PROTAGINE_INTROSPECTION_ENABLED='false', PROTAGINE_RECALL_RERANK='off',
        OPENAI_API_KEY='fixture-model-key', OPENAI_BASE_URL='http://model.fixture/v1',
        LITELLM_LOCAL_MODEL_COST_MAP='True', GATEWAY_ALLOWED_USERS='owner-api',
        WHATSAPP_ALLOWED_USERS='15550003@s.whatsapp.net')
    result = run_python('-I', Path(__file__).with_name('native_task_channels_probe.py'),
        artifacts[3], ROOT/'sidecar', native,
        os.environ.get('PROTAGINE_TEST_DEPENDENCY_PATH', ''), cwd=tmp_path, env=env)
    assert '"cross_channel_native_tasks": true' in result.stdout
