"""Review proposals obey the installed native create contract before staging."""
import importlib.util
import json
import os

import pytest
from conftest import run_python
from test_native_current_work import environment


PROBE = r'''
import json,os,socket,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);shape=sys.argv[2]
if sys.argv[3]:sys.path.insert(0,sys.argv[3])
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text('skills:\n  ledger: true\n')
def no_network(*args,**kwargs):raise AssertionError('Native create fixture must stay offline')
socket.socket.connect=no_network;socket.create_connection=no_network
from tools import skill_manager_tool as manager,skill_provenance as provenance
from tools import skill_ledger as ledger,write_approval as approval
from pacomind_hermes.review import stage_skill_change
if sys.argv[3]:
    assert Path(manager.__file__).resolve().is_relative_to(Path(sys.argv[3]).resolve())
name='catalog-maintenance'
def content(description='Use when maintaining a catalog.'):
    return '---\nname: '+name+'\ndescription: '+description+'\n---\nCheck entries against the current catalog.\n'
valid={'action':'create','name':name,'category':'reference','content':content('D'*59+'.')}
def arguments(operation):
    if shape=='flat':return operation
    if shape=='default_name':
        return {'name':operation['name'],'operations':[{k:v for k,v in operation.items() if k!='name'}]}
    if shape=='mixed_batch':
        return {'operations':[{**valid,'name':'catalog-index'},operation]}
    return {'operations':[operation],'_pacomind_review_create_only':True}
def ledger_bytes():
    return ledger.ledger_path().read_bytes() if ledger.ledger_path().exists() else None
token=provenance.set_current_write_origin('background_review')
try:
    before=approval.list_pending(approval.SKILLS);ledger_before=ledger_bytes()
    cases=[{**valid,'content':content('D'*60+'.')},
        {**valid,'content':content('Use when maintaining a catalog. '+'More detail belongs in the body. '*4)},
        {**valid,'name':'../catalog'},
        {**valid,'category':'../reference'},
        {**valid,'content':'Catalog guidance without frontmatter.'},
        {**valid,'content':'---\nname: '+name+'\ndescription: Maintain a catalog.\n---\n'},
        {**valid,'content':content()+'x'*(manager.MAX_SKILL_CONTENT_CHARS+1)}]
    for operation in cases:
        # Native creation independently supplies the expected rejection. It
        # rejects these inputs before making directories or recording a write.
        native=manager._create_skill(operation['name'],operation['content'],operation['category'])
        assert native['success'] is False,native
        result=json.loads(stage_skill_change(arguments(operation)))
        assert result['success'] is False and not result.get('staged'),result
        assert result['error']==native['error'],(native,result)
        assert approval.list_pending(approval.SKILLS)==before
        assert ledger_bytes()==ledger_before
        assert manager._find_skill(name) is None and manager._find_skill('catalog-index') is None
    for missing in (None,False,42,{},[]):
        result=json.loads(stage_skill_change(arguments({**valid,'content':missing})))
        assert result['success'] is False and not result.get('staged'),result
        assert approval.list_pending(approval.SKILLS)==before
    # Exactly 60 description characters remain valid. The same review can
    # recover from rejected input without installing either proposed skill.
    submitted=arguments(valid)
    result=json.loads(stage_skill_change(submitted))
    assert result['success'] and result['staged'],result
    pending=approval.get_pending(approval.SKILLS,result['pending_id'])
    assert pending['origin']=='background_review'
    assert all(pending['payload'][key]==value for key,value in submitted.items())
    assert len(approval.list_pending(approval.SKILLS))==len(before)+1
    assert ledger_bytes()==ledger_before
    assert manager._find_skill(name) is None and manager._find_skill('catalog-index') is None
finally:provenance.reset_current_write_origin(token)
print(json.dumps({'passed':True,'shape':shape,'network_calls':0}))
'''


@pytest.mark.parametrize('shape', ['flat', 'batch', 'default_name', 'mixed_batch'])
def test_native_create_contract_before_review_staging(artifacts, tmp_path, shape):
    native = os.environ.get('PACOMIND_TEST_HERMES_PATH', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native create staging')
    result = run_python('-I', '-B', '-c', PROBE, artifacts[3], shape, native,
        cwd=tmp_path, env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['passed']
