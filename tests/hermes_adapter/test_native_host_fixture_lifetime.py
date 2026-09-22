"""Optional host consumers share the owned API lifecycle without model calls."""
import json
from pathlib import Path
import subprocess
import sys

from protagine.qualification.native import _environment


def test_optional_host_setup_is_owned_even_when_fixture_body_fails(tmp_path):
    state = tmp_path / 'profile'
    state.mkdir(mode=0o700)
    config = {'model': {'provider': 'fixture', 'default': 'fixture'},
        'providers': {'fixture': {'base_url': 'http://127.0.0.1:9/v1', 'api_key': 'fixture'}}}
    (state / 'config.yaml').write_text(json.dumps(config))
    code = '''
from contextlib import contextmanager
from pathlib import Path
import json,sys
from protagine.qualification.native_memory_worker import prepare
state=Path(sys.argv[1]);config=json.loads((state/'config.yaml').read_text());events=[]
@contextmanager
def setup(app,state,inputs,config):
    events.append('host_entered')
    config['fixture_setup_marker']='ready'
    config['plugins']={'protagine':{'fixture_host_marker':True,'url':'http://must-not-use'}}
    try: yield
    finally: events.append('host_closed')
try:
    with prepare({'binding':'fixture','inputs':{'contact_id':'person','turns':[]}},state,
                 {'base_url':'http://127.0.0.1:9/v1','model':'fixture'},config,setup_host=setup):
        assert config['fixture_setup_marker']=='ready'
        assert config['plugins']['protagine']['fixture_host_marker'] is True
        assert config['plugins']['protagine']['url'].startswith('http://127.0.0.1:')
        assert events==['host_entered']
        raise RuntimeError('controlled body failure')
except RuntimeError as error:
    assert str(error)=='controlled body failure',str(error)
assert events==['host_entered','host_closed'],events
print('OWNED_HOST_LIFETIME_OK')
'''
    completed = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(state)],
        cwd=state, env=_environment(state, config), text=True, capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'OWNED_HOST_LIFETIME_OK' in completed.stdout
