"""Real native failure capture, recurrence and staged review without a skill."""
import json
import os

import pytest
from conftest import run_python
from test_native_current_work import environment


PROBE = r'''
import hashlib,json,os,socket,sys,threading,time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':['pacomind'],'pacomind':{
    'owner_contact_id':'fixture-owner','attested_system_platforms':['cli'],
    'native_reviews':{'enabled':True},'turn_outbox_path':str(home/'outbox.db')}},
    'skills':{'creation_nudge_interval':1,'write_approval':False}}))
def no_network(*a,**kw):raise AssertionError('Native experience fixture is offline')
socket.socket.connect=no_network;socket.create_connection=no_network
import pacomind_hermes
class Reply:
    status_code=200
    def __init__(self,value):self.value=value
    def json(self):return self.value
    def raise_for_status(self):pass
def get(self,path,**kw):
    if path=='/v1/host/contacts/resolve':return Reply({'contact_id':'fixture-owner'})
    if path=='/v1/host/memory/sources/erasures':
        return Reply({'contact_id':'fixture-owner','head':0,'through':0,'events':[],'complete':True})
    raise RuntimeError('No fixture service')
pacomind_hermes.PacoMindClient.get=get
pacomind_hermes.PacoMindClient.post=lambda *a,**kw:Reply({})
from hermes_cli.plugins import get_plugin_manager,PluginContext,PluginManifest
plugins=get_plugin_manager();plugins.discover_and_load()
from tools import skill_ledger as ledger,write_approval as approval,skill_provenance
from pacomind_hermes.review_experience import next_batch,UNATTRIBUTED_ACTION
from pacomind_hermes.review_evidence import capture
from run_agent import AIAgent
import run_agent
from hermes_state import SessionDB
from agent import background_review
assert Path(background_review.__file__).resolve().is_relative_to(Path(sys.argv[2]).resolve())
OPENAI_TARGET='run_agent.OpenAI' if 'OpenAI' in vars(run_agent) else 'agent.process_bootstrap.OpenAI'
TOOLS_TARGET='run_agent' if 'get_tool_definitions' in vars(run_agent) else 'model_tools'
def reply(content,call=None):
    return NS(choices=[NS(message=NS(content=content,tool_calls=[call] if call else None),
        finish_reason='tool_calls' if call else 'stop')],model='fixture/model',usage=None)
def tool(call_id,name,args):
    return NS(id=call_id,type='function',function=NS(name=name,arguments=json.dumps(args)))
defs=[{'type':'function','function':{'name':name,'description':name,
    'parameters':{'type':'object','properties':{}}}}
    for name in ('read_file','skills_list','skill_view','skill_manage')]
missing=home/'absent-neutral.txt'
parent_client=MagicMock();review_client=MagicMock()
parent_client.chat.completions.create.side_effect=[
    reply('',tool('failed-one','read_file',{'path':str(missing)})),reply('FIRST_FINISHED'),
    reply('',tool('failed-two','read_file',{'path':str(missing)})),reply('SECOND_FINISHED')]
name='neutral-path-recovery'
content='---\nname: '+name+'\ndescription: Recover a supplied path after a read failure.\n---\nVerify the current supplied path before retrying.\n'
review_client.chat.completions.create.side_effect=[
    reply('',tool('proposed','skill_manage',{'action':'create','name':name,'content':content})),
    reply('PROPOSED_ONLY')]
entered=threading.Event();release=threading.Event();batch=None
real_builder=background_review.build_cache_parity_fork
def delayed_builder(*args,**kwargs):
    entered.set();assert release.wait(10),'Fixture review handoff deadline'
    return real_builder(*args,**kwargs)
def bind_batch(**kwargs):
    if kwargs.get('tool_name')=='skill_manage' and skill_provenance.is_background_review():
        assert batch is not None
        return {'args':{**kwargs['args'],'_pacomind_review_batch_sha256':batch['failure_sha256'],
            '_pacomind_review_observation_ids':batch['observation_ids']}}
handle=PluginContext(PluginManifest(name='fixture-batch-consumer'),plugins).register_middleware('tool_request',bind_batch)
parent=None
try:
    with patch(OPENAI_TARGET,side_effect=[parent_client,review_client]), \
            patch(TOOLS_TARGET+'.get_tool_definitions',return_value=defs), \
            patch(TOOLS_TARGET+'.check_toolset_requirements',return_value={}), \
            patch.object(background_review,'build_cache_parity_fork',side_effect=delayed_builder):
        parent=AIAgent(api_key='fixture-key',base_url='http://127.0.0.1:1/v1',provider='openai',
            model='fixture/model',max_iterations=4,quiet_mode=True,skip_context_files=True,
            skip_memory=True,platform='sms',enabled_toolsets=['file','skills'],session_db=SessionDB(home/'state.db'))
        parent._user_id='fixture-owner-address'
        parent._cached_system_prompt='Read the supplied file and finish.'
        parent._use_prompt_caching=False;parent.compression_enabled=False;parent.save_trajectories=False
        parent._skill_nudge_interval=0
        assert parent.run_conversation('Read the supplied file.',task_id='first-task')['final_response']=='FIRST_FINISHED'
        first=[r for r in ledger.list_entries() if r['action']==UNATTRIBUTED_ACTION]
        assert len(first)==1 and first[0]['skill'] is None,first
        assert next_batch(ledger.list_entries()) is None
        parent._skill_nudge_interval=1
        assert parent.run_conversation('Read the supplied file again.',task_id='second-task')['final_response']=='SECOND_FINISHED'
        assert entered.wait(10),'Actual native post-turn fork did not begin'
        rows=[r for r in ledger.list_entries() if r['action']==UNATTRIBUTED_ACTION]
        assert len(rows)==2 and all(r['skill'] is None for r in rows),rows
        batch=next_batch(ledger.list_entries())
        assert batch and batch['attribution']=='unattributed' and batch['skill'] is None,batch
        assert len({r['turn_id'] for r in batch['observations']})==2
        assert {r['tool_call_id'] for r in batch['observations']}=={'failed-one','failed-two'}
        for row in rows:
            value=row['evidence'];assert 'skill_sha256' not in value and 'skill_call_id' not in value
            assert value['contact_id']=='fixture-owner' and value['platform']=='sms'
            assert str(missing) not in json.dumps(value)
        current=pacomind_hermes._TRANSPORT_SCOPES.for_session(parent.session_id)
        request=parent_client.chat.completions.create.call_args_list[-1].kwargs
        capture(current,request,durable=True)
        assert len([r for r in ledger.list_entries() if r['action']==UNATTRIBUTED_ACTION])==2
        # The same actual failure body cannot become qualification or guest experience.
        for fields in ({'platform':'cli','authority_lane':'system'}, {'authority_lane':'guest'}):
            scope=replace(current,**fields)
            capture(scope,request,durable=True)
        assert len([r for r in ledger.list_entries() if r['action']==UNATTRIBUTED_ACTION])==2
        assert ledger.append_entry('ordinary_skill_review',None,actor='curator',evidence=batch)
        assert next_batch(ledger.list_entries()) is None
        release.set()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline and getattr(parent,'_background_review_run',None) is not None:
            time.sleep(.02)
        assert getattr(parent,'_background_review_run',None) is None
        pending=approval.list_pending(approval.SKILLS)
        assert len(pending)==1 and pending[0]['origin']=='background_review',pending
        assert pending[0]['payload']['name']==name and pending[0]['payload']['content']==content
        assert pending[0]['payload']['_pacomind_review_batch_sha256']==batch['failure_sha256']
        assert pending[0]['payload']['_pacomind_review_observation_ids']==batch['observation_ids']
        assert not list((home/'skills').rglob('SKILL.md'))
        assert not any(r['action']=='create' for r in ledger.list_entries())
        assert review_client.chat.completions.create.call_count==2
finally:
    release.set();handle.dispose()
    if parent:parent.close()
print(json.dumps({'real_native_failures':2,'unattributed':True,'one_native_staged_proposal':True,
    'duplicate_and_qualification_excluded':True,'active_skill_changes':0,'real_model_calls':0}))
'''


def test_native_unattributed_failures_reach_one_staged_review(artifacts,tmp_path):
    native=os.environ.get('PACOMIND_TEST_HERMES_PATH')
    if not native:
        pytest.skip('Requires the selected native Hermes source')
    _,_,_,installed=artifacts
    result=run_python('-I','-B','-c',PROBE,installed,native,cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['one_native_staged_proposal']


TASK_PROBE = r'''
import copy,json,os,socket,sys,types
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
sys.path[:0]=[sys.argv[1],sys.argv[2]]
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':['pacomind'],'pacomind':{
    'owner_contact_id':'fixture-owner','native_reviews':{'enabled':True},
    'native_tasks':{'enabled':True,'factory':'fixture_sources:build'},
    'turn_outbox_path':str(home/'outbox.db')}},'skills':{'creation_nudge_interval':0},
    'auxiliary':{'title_generation':{'enabled':False}}}))
def no_network(*a,**kw):raise AssertionError('Task purpose fixture must stay offline')
socket.socket.connect=no_network;socket.create_connection=no_network
import pacomind_hermes
class Reply:
    status_code=200
    def __init__(self,value):self.value=value
    def json(self):return self.value
    def raise_for_status(self):pass
def get(self,path,**kw):
    if path=='/v1/host/contacts/resolve':return Reply({'contact_id':'fixture-owner'})
    if path=='/v1/host/memory/sources/erasures':return Reply({'contact_id':'fixture-owner',
        'head':0,'through':0,'events':[],'complete':True})
    raise RuntimeError('No fixture service')
pacomind_hermes.PacoMindClient.get=get
pacomind_hermes.PacoMindClient.post=lambda *a,**kw:Reply({})
from pacomind_hermes.task_controller import NativeTasks
class Sources:
    def resolve_source(self,value,dependencies=None):return copy.deepcopy(value)
    def resolve_owner(self,value,require_task_grant=False):
        assert value['contact_id']=='fixture-owner';return 'fixture-owner'
    def execution_identity(self,value):
        return {'sender_id':'owner-address','contact_id':'fixture-owner','authority_lane':'owner',
            'resolution_status':'resolved','authority_gateway':'sms'}
created=[]
def build(client,outbox,owner_contact_id,**kw):
    value=NativeTasks(client,outbox,owner_contact_id,sources=Sources())
    created.append(value);return value
module=types.ModuleType('fixture_sources');module.build=build;sys.modules[module.__name__]=module
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
controller=created[0]
from gateway.config import PlatformConfig
controller.adapter=controller.create_adapter(PlatformConfig(enabled=True))
from pacomind_hermes.native_task_platform import ACTIVE
from pacomind_hermes.review_experience import next_batch,UNATTRIBUTED_ACTION
from pacomind_hermes.review_evidence import current
from tools import skill_ledger as ledger
from run_agent import AIAgent
import run_agent
from hermes_state import SessionDB
OPENAI_TARGET='run_agent.OpenAI' if 'OpenAI' in vars(run_agent) else 'agent.process_bootstrap.OpenAI'
TOOLS_TARGET='run_agent' if 'get_tool_definitions' in vars(run_agent) else 'model_tools'
defs=[{'type':'function','function':{'name':'read_file','description':'Read the file',
    'parameters':{'type':'object','properties':{}}}}]
source={'version':1,'principal':'fixture','source_session_id':'actual-owner-input',
    'input_refs':[{'source_id':'owner-question','input_message_hash':'a'*64}],
    'source_refs':[{'source_id':'owner-question','source_version':'b'*64}],
    'watermark':0,'contact_id':'fixture-owner'}
def reply(content,call=None):
    return NS(choices=[NS(message=NS(content=content,tool_calls=[call] if call else None),
        finish_reason='tool_calls' if call else 'stop')],model='fixture/model',usage=None)
observed=[]
for index,purpose in enumerate(('qualification','qualification','operational','operational')):
    row=controller.handoffs.admit(request_id='purpose-'+str(index),request='Read supplied file',
        source_input=source,experience=purpose)
    active={'adapter':controller.adapter,'handoffs':controller.handoffs,'id':row['id'],'supplied':None}
    token=ACTIVE.set(active)
    sdk=MagicMock()
    call=NS(id='read-'+str(index),type='function',function=NS(name='read_file',
        arguments=json.dumps({'path':str(home/'absent-neutral.txt')})))
    sdk.chat.completions.create.side_effect=[reply('',call),reply('FINISHED')]
    parent=None
    try:
        with patch(OPENAI_TARGET,return_value=sdk),patch(TOOLS_TARGET+'.get_tool_definitions',return_value=defs), \
                patch(TOOLS_TARGET+'.check_toolset_requirements',return_value={}):
            parent=AIAgent(api_key='fixture-key',base_url='http://127.0.0.1:1/v1',provider='openai',
                model='fixture/model',max_iterations=3,quiet_mode=True,skip_context_files=True,
                skip_memory=True,skip_background_review=True,platform='pacomind_task',
                enabled_toolsets=['file'],session_db=SessionDB(home/'state.db'))
            parent._user_id='fixture-owner'
            parent._cached_system_prompt='Read the supplied file and finish.'
            parent._use_prompt_caching=False;parent.compression_enabled=False;parent.save_trajectories=False
            result=parent.run_conversation('Read the supplied file.',task_id='task-'+str(index))
            assert result['final_response']=='FINISHED',result
            scope=pacomind_hermes._TRANSPORT_SCOPES.for_session(parent.session_id)
            assert scope.platform=='pacomind_task' and scope.authority_lane=='owner'
            assert scope.resolution_status=='resolved' and scope.contact_id=='fixture-owner'
            experience=controller.execution_experience(**active['native'],platform='pacomind_task')
            assert experience['purpose']==purpose and experience['task_id']==row['id']
            assert current()['failures'][0]['tool_call_id']=='read-'+str(index)
            retained=[r for r in ledger.list_entries() if r['action']==UNATTRIBUTED_ACTION]
            assert len(retained)==max(0,index-1),(purpose,retained)
            observed.append({'purpose':purpose,'scope_lane':scope.authority_lane,'retained':len(retained)})
    finally:
        if parent:parent.close()
        ACTIVE.reset(token)
batch=next_batch(ledger.list_entries())
assert len(batch['observations'])==2
assert {r['tool_call_id'] for r in batch['observations']}=={'read-2','read-3'}
print(json.dumps({'native_owner_tasks':observed,'qualification_excluded':True,'operational_batch':True,'model_calls':0}))
'''


def test_native_owner_task_purpose_excludes_qualification_experience(artifacts,tmp_path):
    native=os.environ.get('PACOMIND_TEST_HERMES_PATH')
    if not native:
        pytest.skip('Requires the selected native Hermes source')
    _,_,_,installed=artifacts
    result=run_python('-I','-B','-c',TASK_PROBE,installed,native,cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['qualification_excluded']
