"""A detached local sidecar releases its launcher's terminal and stays usable."""
import errno
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import time

import httpx
import pytest


@pytest.mark.skipif(os.name != 'posix', reason='PTY lifecycle requires POSIX')
def test_detached_sidecar_releases_terminal_before_product_stop(tmp_path):
    import pty

    state = tmp_path/'state'
    state.mkdir()
    source = Path(__file__).resolve().parents[1]
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
    env.update(
        PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE='1',
        PACOMIND_STATE_DIR=str(state), PACOMIND_INSTALL_PROFILE='local',
        PACOMIND_SKIP_DOTENV='1', PACOMIND_API_KEY='isolated-lifecycle-test',
        PACOMIND_CLIENT_API_KEY='isolated-lifecycle-test',
        PACOMIND_GRAPH_ENABLED='false', PACOMIND_EMBED_PROVIDER='skip',
        PACOMIND_SOURCE_CLAIMS='off', PACOMIND_AUTONOMY_PRESET='passive',
        PACOMIND_EMBEDDED_WORKER_ENABLED='false',
        LITELLM_LOCAL_MODEL_COST_MAP='True', DO_NOT_TRACK='1',
    )
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    master, slave = pty.openpty()
    launcher = subprocess.Popen(
        [sys.executable, '-c', 'from pacomind import cli; '
         f'cli._cmd_start_daemon("127.0.0.1", {port}, False)'],
        cwd=tmp_path, env=env, stdin=slave, stdout=slave, stderr=slave,
    )
    os.close(slave)
    output = bytearray()
    terminal_closed = False
    try:
        assert launcher.wait(timeout=30) == 0
        # Check the real running application before closing its launcher PTY.
        with httpx.Client(trust_env=False) as client:
            response = client.get(f'http://127.0.0.1:{port}/v1/host/health',
                headers={'X-API-Key': 'isolated-lifecycle-test'}, timeout=3)
        assert response.status_code == 200
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not select.select([master], [], [], deadline-time.monotonic())[0]:
                break
            try:
                data = os.read(master, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                data = b''
            if not data:
                terminal_closed = True
                break
            output.extend(data)
        assert terminal_closed, 'Detached sidecar retained launcher terminal: ' + output.decode()
    finally:
        if launcher.poll() is None:
            launcher.terminate()
            launcher.wait(timeout=5)
        # Exercise the same recorded-process shutdown used by the CLI.
        stopped = subprocess.run(
            [sys.executable, '-c', 'from pacomind import cli; cli._cmd_stop()'],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
        )
        os.close(master)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert not (state/'sidecar.pid').exists(), stopped.stdout + stopped.stderr
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', port), timeout=.2)
