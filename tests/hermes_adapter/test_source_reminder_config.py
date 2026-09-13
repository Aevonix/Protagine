"""Cold cron launchers resolve the selected profile, with no network/registration."""
import importlib.util
import os
from pathlib import Path
import shutil
import sysconfig

import pytest

from conftest import ROOT, run_python


PROBE = r'''
import importlib.util,json,os,socket,sys,types
from pathlib import Path
sys.path.insert(0,sys.argv[1])
sys.path.append(sys.argv[4])
root=Path(sys.argv[2]); case=sys.argv[3]; layout=sys.argv[5]
assert importlib.util.find_spec('pacomind_memory') is None
def blocked(*args,**kwargs): raise AssertionError('Configuration qualification must not call the network')
socket.socket.connect=blocked; socket.create_connection=blocked
home=Path(os.environ['HERMES_HOME']); home.mkdir(mode=0o700)
memory={'url':'http://127.0.0.1:7771','contact_id':'inline-owner','api_key':'${SELECTED_KEY}'}
native={'url':'http://127.0.0.1:7772','contact_id':'native-owner','api_key':'${SELECTED_KEY}'}
plugin={}
expected=('http://127.0.0.1:7772','native-owner','profile-key')
if case=='plugin':
 plugin={'url':'http://127.0.0.1:7773','owner_contact_id':'plugin-owner','api_key':'${PLUGIN_KEY}'}
 expected=('http://127.0.0.1:7773','plugin-owner','profile-plugin-key')
elif case=='environment':
 memory={'contact_id':'default'}; native={}
 expected=('http://127.0.0.1:7774','environment-owner','profile-api-key')
elif case=='inherited':
 memory['api_key']='${PACOMIND_API_KEY}'; native={}
 expected=('http://127.0.0.1:7771','inline-owner','fixture-factory-key')
plugin['turn_outbox_path']=str(home/'state'/'owned-outbox.db')
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':[],'pacomind':plugin},
    'memory':{'provider':'pacomind-memory','config':memory}}))
(home/'pacomind-memory.json').write_text(json.dumps(native))
(home/'.env').write_text('SELECTED_KEY=profile-key\nPLUGIN_KEY=profile-plugin-key\n'
    'PACOMIND_API_KEY=profile-api-key\nPACOMIND_URL=http://127.0.0.1:7774\n'
    'PACOMIND_OWNER_CONTACT_ID=environment-owner\n')
assert not any(k in os.environ for k in ('SELECTED_KEY','PLUGIN_KEY','PACOMIND_API_KEY','PACOMIND_OWNER_CONTACT_ID'))
if case=='inherited':
 (home/'.env').write_text('')
 from tools.environments.local import build_subprocess_env
 child=build_subprocess_env(base={**os.environ,'PACOMIND_API_KEY':'fixture-factory-key'})
 assert child['PACOMIND_API_KEY']=='fixture-factory-key'
 os.environ.clear(); os.environ.update(child)
implementation=root/('pacomind_hermes' if layout=='private' else 'plugins/hermes-plugin')/'reminders.py'
spec=importlib.util.spec_from_file_location('reminder_config_probe',implementation)
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
client_spec=importlib.util.spec_from_file_location('reminder_config_client',implementation.with_name('client.py'))
client_module=importlib.util.module_from_spec(client_spec); sys.modules[client_spec.name]=client_module
client_spec.loader.exec_module(client_module)
client_module.TurnOutbox(plugin['turn_outbox_path']).prepare()
observed=[]
def reader(client,owner,*,home,outbox):
 assert (client.url,owner,client._api_key)==expected
 assert home==Path(os.environ['HERMES_HOME']).resolve()
 assert outbox.path==Path(plugin['turn_outbox_path']) and outbox.path.is_file()
 def render(binding): observed.append(binding); return ''
 return types.SimpleNamespace(render=render)
module.NativeReminders=reader
module.main('selected-binding')
assert observed==['selected-binding']
assert importlib.util.find_spec('pacomind_memory') is None
print(json.dumps({'case':case,'layout':layout,'adapter_package_absent':True,
    'selected_profile':True,'credentials_from_existing_profile':True,'network_calls':0}))
'''


@pytest.mark.parametrize('case,layout', [('native','source'), ('plugin','source'),
    ('environment','source'), ('inherited','source'), ('native','private')])
def test_cold_reminder_uses_native_profile_config_and_credentials(tmp_path, case, layout):
    native = os.environ.get('PACOMIND_TEST_HERMES_PATH')
    if not native:
        spec = importlib.util.find_spec('hermes_cli')
        if spec is None:
            pytest.skip('Install the qualified native Hermes runtime')
        native = str(Path(spec.origin).resolve().parents[1])
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR') if key in os.environ}
    env.update(HOME=str(tmp_path/'user'), HERMES_HOME=str(tmp_path/'profile'),
        HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1',
        LITELLM_LOCAL_MODEL_COST_MAP='True')
    # A cold interpreter sees native dependencies, but neither an installed
    # adapter nor editable-package import hooks. The private-directory layout
    # is intentionally outside sys.path; main must find only its own sibling.
    dependencies = tmp_path/'dependencies'
    dependencies.mkdir()
    for entry in Path(sysconfig.get_path('purelib')).iterdir():
        if 'pacomind' not in entry.name and not entry.name.endswith('.pth'):
            (dependencies/entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    source = ROOT
    if layout == 'private':
        source = tmp_path/'private-adapter'
        (source/'pacomind_hermes').mkdir(parents=True)
        (source/'pacomind_memory').mkdir()
        for name in ('reminders.py', 'client.py'):
            shutil.copyfile(ROOT/'plugins/hermes-plugin'/name, source/'pacomind_hermes'/name)
        shutil.copyfile(ROOT/'plugins/pacomind-memory/provider.py', source/'pacomind_memory/provider.py')
    run_python('-I', '-S', '-B', '-c', PROBE, native, source, case, dependencies, layout,
               cwd=tmp_path, env=env)
