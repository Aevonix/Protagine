"""Required interfaces run against the actual selected native interpreter, without skips."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_selected_native_runtime_has_required_core_capabilities(tmp_path):
    root = Path(__file__).resolve().parents[2]
    receipt = tmp_path/'capabilities.json'
    result = subprocess.run([
        sys.executable, str(root/'sidecar/protagine/hermes_capabilities.py'),
        '--python', os.environ.get('PROTAGINE_HERMES_TEST_PYTHON', sys.executable),
        '--output', str(receipt), '--require', 'core',
    ], capture_output=True, text=True, timeout=60)
    assert receipt.is_file(), result.stderr
    report = json.loads(receipt.read_text())
    assert report['activation_state'] == 'not_activated'
    assert report['runtime']['version'], report
    assert result.returncode == 0, result.stderr + '\n' + json.dumps(report['features'])
    assert report['features']['core']['available'] is True
