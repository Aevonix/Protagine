"""Actual Hermes proposal, mutation, evidence and recovery contracts."""
import importlib.util
import json
import os

import pytest
from conftest import run_python
from test_native_current_work import environment


PROBE = r'''
import json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1]); scenario=sys.argv[2]
if sys.argv[3]: sys.path.insert(0,sys.argv[3])
home=Path(os.environ['HERMES_HOME']); home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text('skills:\n  ledger: true\n')
def no_network(*a,**kw): raise AssertionError('Contract fixture must stay offline')
socket.socket.connect=no_network; socket.create_connection=no_network
from tools import skill_manager_tool as manager,skill_provenance as provenance,skill_ledger as ledger,write_approval as approval,skills_tool
if sys.argv[3]: assert Path(manager.__file__).resolve().is_relative_to(Path(sys.argv[3]).resolve())
from protagine_hermes.review import stage_skill_change
from protagine_hermes.review_evaluation import evaluate_pending,audit_evaluation
from protagine_hermes.review_evidence import capture,current
capture(NS(valid_participant=True,authority_lane='owner',platform='cli',session_id='native-parent',turn_id='native-turn'),
    {'messages':[{'role':'assistant','tool_calls':[{'id':'failed-read','function':{'name':'read_file'}}]},
        {'role':'tool','tool_call_id':'failed-read','content':json.dumps({'error':'Selected fixture path is absent'})}]})
if scenario=='batch':capture(None,{})
expected_source=current()
name='neutral-path-recovery'
old='---\nname: '+name+'\ndescription: Recover a neutral supplied path.\n---\nUse the earlier path after a read failure.\n'
new=old.replace('Use the earlier path after a read failure.','Use the current supplied path after a read failure.')
token=provenance.set_current_write_origin('background_review' if scenario!='user_owned' else 'foreground')
try:
    assert json.loads(manager.skill_manage('create',name,content=old))['success']
    if scenario=='user_owned':
        pending=approval.stage_write(approval.SKILLS,{'action':'patch','name':name,'content':new},summary='fixture',origin='background_review')
        pid=pending['id']
    else:
        assert json.loads(skills_tool.skill_view(name,preprocess=False)).get('success',True)
        operation={'action':'patch','name':name,'content':new}
        if scenario in {'targeted','patch_conflict','interrupted_targeted'}:
            operation={'action':'patch','name':name,'old_string':'absent-text' if scenario=='patch_conflict' else 'earlier','new_string':'current supplied'}
        arguments={'operations':[operation]} if scenario in {'batch','targeted','patch_conflict','interrupted_targeted'} else operation
        arguments['_protagine_review_evidence']={'invented_by_model':True}
        staged=json.loads(stage_skill_change(arguments))
        assert staged['staged']; pid=staged['pending_id']
        assert approval.get_pending(approval.SKILLS,pid)['payload']['_protagine_review_evidence']==expected_source
finally: provenance.reset_current_write_origin(token)
target=manager._find_skill(name)['path']/'SKILL.md'
phases=[]
def oracle(text,*,phase):
    phases.append(phase)
    if phase=='post_activation' and scenario=='initial_unavailable':
        raise TimeoutError('controlled unavailable initial qualification')
    # Controlled task outcomes qualify lifecycle decisions, not model quality.
    passed=text==new
    if phase=='post_activation' and scenario in {'regression','owner_changed','interrupted','interrupted_targeted','pending_retained','cleanup_interrupted'}:
        passed=False
    if phase=='post_activation' and scenario=='owner_changed': target.write_text('OWNER_EDIT')
    return {'cases':[{'id':'changed-path','passed':passed},{'id':'unchanged-path','passed':True}],
            'metadata':{'fixture':'controlled task outcomes'}}
if scenario=='stale': target.write_text('OWNER_EDIT')
if scenario=='user_owned':
    try: evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
    except ValueError: pass
    else: raise AssertionError('User-owned skill was evaluated')
    assert not phases and target.read_text()==old
elif scenario=='ledger_failed':
    with patch.object(ledger,'append_entry',return_value=None):
        try: evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
        except RuntimeError: pass
        else: raise AssertionError('Mutation occurred without durable evaluation')
    assert target.read_text()==old and approval.get_pending(approval.SKILLS,pid)
elif scenario in {'pending_retained','cleanup_interrupted'}:
    with patch.object(approval,'discard_pending',side_effect=KeyboardInterrupt('after terminal record') if scenario=='cleanup_interrupted' else None,return_value=False):
        try: result=evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
        except KeyboardInterrupt: pass
    assert target.read_text()==old and approval.get_pending(approval.SKILLS,pid)
    phases.clear()
    result=evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
    assert result['status']=='rolled_back' and result['already_final'] and not phases,result
    assert target.read_text()==old and not approval.get_pending(approval.SKILLS,pid)
elif scenario in {'interrupted','interrupted_targeted'}:
    real=manager.apply_skill_pending
    def interrupted(payload):
        result=real(payload); assert json.loads(result)['success']
        raise KeyboardInterrupt('after native apply, before result')
    with patch.object(manager,'apply_skill_pending',side_effect=interrupted):
        try: evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
        except KeyboardInterrupt: pass
    assert target.read_text()==new and approval.get_pending(approval.SKILLS,pid)
    phases.clear()
    result=evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
    assert result['status']=='rolled_back' and phases==['post_activation'],result
    assert target.read_text()==old
else:
    result=evaluate_pending(pid,name,oracle,oracle_id='fixture-v1')
    expected={'activate':'activated','batch':'activated','targeted':'activated','periodic_unavailable':'activated','initial_unavailable':'rolled_back','patch_conflict':'patch_conflict','regression':'rolled_back','owner_changed':'changed_elsewhere','stale':'stale_proposal'}[scenario]
    assert result['status']==expected,result
    assert target.read_text()==(new if scenario in {'activate','batch','targeted','periodic_unavailable'} else 'OWNER_EDIT' if scenario in {'owner_changed','stale'} else old)
    if scenario in {'stale','patch_conflict'}: assert not phases
    elif scenario=='owner_changed': assert approval.get_pending(approval.SKILLS,pid)
    else:
        assert not approval.get_pending(approval.SKILLS,pid)
        entry=ledger.get_entry(result['evaluation_id'])
        assert entry['evidence']['source_evidence']==expected_source
        assert entry['evidence']['baseline']['cases'][0]['passed'] is False
        assert entry['evidence']['candidate']['cases'][0]['passed'] is True
        if scenario in {'regression','initial_unavailable'}:
            assert any(e['action']=='rollback' and e['evidence']['rollback_target']==entry['id'] for e in ledger.list_entries())
            if scenario=='initial_unavailable':
                assert result['measurement']=={'status':'unavailable','error_type':'TimeoutError'}
        else:
            if scenario=='periodic_unavailable':
                def unavailable_repeat(text,*,phase): raise TimeoutError('controlled transient outage')
                result=audit_evaluation(entry['id'],unavailable_repeat,oracle_id='fixture-v1')
                assert result['status']=='unavailable' and target.read_text()==new,result
                assert ledger.get_entry(result['result_entry_id'])['evidence']['measurement']['error_type']=='TimeoutError'
                assert not any(e['action']=='rollback' for e in ledger.list_entries())
                result=audit_evaluation(entry['id'],oracle,oracle_id='fixture-v1')
                assert result['status']=='activated' and target.read_text()==new,result
            # A later failed repeat has the same recoverable native target.
            def failed_repeat(text,*,phase):
                return {'cases':[{'id':'changed-path','passed':False},{'id':'unchanged-path','passed':True}]}
            result=audit_evaluation(entry['id'],failed_repeat,oracle_id='fixture-v1')
            assert result['status']=='rolled_back' and target.read_text()==old,result
print(json.dumps({'passed':True,'scenario':scenario}))
'''


@pytest.mark.parametrize('scenario',['activate','batch','targeted','periodic_unavailable','initial_unavailable','patch_conflict','regression','owner_changed','stale','user_owned','ledger_failed','interrupted','interrupted_targeted','pending_retained','cleanup_interrupted'])
def test_native_measured_proposal_and_recovery(artifacts,tmp_path,scenario):
    native=os.environ.get('PROTAGINE_TEST_HERMES_PATH','')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native skill evaluation')
    _,_,_,installed=artifacts
    result=run_python('-I','-c',PROBE,installed,scenario,native,cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['passed']


SHAPE_PROBE = r'''
import json,os,socket,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]); scenario=sys.argv[2]
if sys.argv[3]: sys.path.insert(0,sys.argv[3])
home=Path(os.environ['HERMES_HOME']); home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text('skills:\n  ledger: true\n')
def no_network(*a,**kw): raise AssertionError('Contract fixture must stay offline')
socket.socket.connect=no_network; socket.create_connection=no_network
from tools import skill_manager_tool as manager,skill_provenance as provenance,skill_ledger as ledger,write_approval as approval,skills_tool
from protagine_hermes.review import stage_skill_change
name='neutral-batch-shape'
old='---\nname: '+name+'\ndescription: Use for neutral batch shape checks.\n---\nOriginal guidance.\n'
new=old.replace('Original guidance.','Updated guidance.')
token=provenance.set_current_write_origin('background_review')
try:
    assert json.loads(manager.skill_manage('create',name,content=old))['success']
    operation={'action':'patch','name':name,'content':new}
    retained=approval.stage_write(approval.SKILLS,{'operations':'[{malformed'},summary='Existing malformed proposal',origin='background_review')
    cases={'malformed_string':'[{malformed','encoded_array':json.dumps([operation]),'empty':[],
           'nonobject':['patch'],'missing_action':[{'name':name}],
           'missing_name':[{'action':'patch','content':new}],
           'too_many':[{'action':'create','name':f'neutral-batch-new-{i}',
                        'content':f'---\nname: neutral-batch-new-{i}\ndescription: Neutral check.\n---\nGuidance.\n'}
                       for i in range(21)],
           'mixed_delete':[{'action':'delete','name':name},
                           {'action':'create','name':'neutral-batch-new','content':new}]}
    before=approval.list_pending(approval.SKILLS)
    ledger_before=ledger.ledger_path().read_bytes()
    if scenario in cases:
        # Actual installed Hermes rejects these shapes before native mutation.
        native=json.loads(manager.skill_manage('', '', operations=cases[scenario]))
        assert native['success'] is False and approval.list_pending(approval.SKILLS)==before
        result=json.loads(stage_skill_change({'operations':cases[scenario]}))
        assert result['success'] is False and not result.get('staged'),result
        assert approval.list_pending(approval.SKILLS)==before
    elif scenario=='create_only':
        assert json.loads(skills_tool.skill_view(name,preprocess=False)).get('success',True)
        result=json.loads(stage_skill_change({'operations':[operation],'_protagine_review_create_only':True}))
        assert result['success'] is False and not result.get('staged'),result
        assert 'existing skills have not been implicated' in result['error']
        assert approval.list_pending(approval.SKILLS)==before
    else:
        assert json.loads(skills_tool.skill_view(name,preprocess=False)).get('success',True)
        if scenario=='legacy_edit': operation={**operation,'action':'edit'}
        arguments=operation if scenario in {'legacy','legacy_edit'} else {'operations':[operation]}
        if scenario=='batch_default_name':
            arguments={'name':name,'operations':[{k:v for k,v in operation.items() if k!='name'}]}
        result=json.loads(stage_skill_change(arguments))
        assert result['success'] and result['staged'],result
        assert len(approval.list_pending(approval.SKILLS))==len(before)+1
        stored=approval.get_pending(approval.SKILLS,result['pending_id'])['payload']
        assert all(stored[k]==v for k,v in arguments.items())
    assert ledger.ledger_path().read_bytes()==ledger_before
    assert (manager._find_skill(name)['path']/'SKILL.md').read_text()==old
    assert approval.get_pending(approval.SKILLS,retained['id'])==retained
finally: provenance.reset_current_write_origin(token)
print(json.dumps({'passed':True,'scenario':scenario}))
'''


@pytest.mark.parametrize('scenario', ['malformed_string','encoded_array','empty','nonobject',
                                    'missing_action','missing_name','too_many','mixed_delete',
                                    'legacy','legacy_edit','batch','batch_default_name','create_only'])
def test_native_review_batch_shape_before_staging(artifacts,tmp_path,scenario):
    native=os.environ.get('PROTAGINE_TEST_HERMES_PATH','')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native skill evaluation')
    _,_,_,installed=artifacts
    result=run_python('-I','-c',SHAPE_PROBE,installed,scenario,native,cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['passed']


READ_PROBE = r'''
import hashlib,json,os,socket,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
if sys.argv[2]: sys.path.insert(0,sys.argv[2])
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text('skills:\n  ledger: true\n')
def no_network(*a,**kw):raise AssertionError('Native review read qualification is offline')
socket.socket.connect=no_network;socket.create_connection=no_network
from tools import skill_manager_tool as manager,skill_provenance as provenance,skills_tool,write_approval as approval
from tools.skill_manager_guards import _reset_background_review_read_marks
from protagine_hermes.review import stage_skill_change
from protagine_hermes.review_evaluation import evaluate_pending
if sys.argv[2]:assert Path(manager.__file__).resolve().is_relative_to(Path(sys.argv[2]).resolve())
name='neutral-current-contract'
old='---\nname: '+name+'\ndescription: Read the current contract.\n---\nUse current receipts.\n'
token=provenance.set_current_write_origin('background_review')
try:
    assert json.loads(manager.skill_manage('create',name,content=old))['success']
    target=manager._find_skill(name)['path']/'SKILL.md'
    operation={'action':'patch','name':name,'old_string':'Use current receipts.','new_string':'Distinguish acceptance and delivery.'}
    _reset_background_review_read_marks()
    before=approval.list_pending(approval.SKILLS)
    denied=json.loads(stage_skill_change({'operations':[operation]}))
    assert denied.get('_read_before_write_required') and not denied.get('staged'),denied
    assert approval.list_pending(approval.SKILLS)==before and target.read_text()==old
    viewed=json.loads(skills_tool.skill_view(name,preprocess=False))
    assert viewed.get('success',True) and 'Use current receipts.' in json.dumps(viewed)
    staged=json.loads(stage_skill_change({'operations':[operation]}))
    assert staged['staged'] and target.read_text()==old,staged
    pending=approval.get_pending(approval.SKILLS,staged['pending_id'])
    assert pending['payload']['_protagine_review_base_sha256']==hashlib.sha256(old.encode()).hexdigest()
    # A separate owner edit after staging invalidates the existing proposal.
    # Native path-read marks do not claim same-review byte freshness.
    target.write_text(old+'Owner correction.\n')
    def no_measure(*a,**kw):raise AssertionError('Stale candidate must not be measured or applied')
    outcome=evaluate_pending(staged['pending_id'],name,no_measure,oracle_id='must-not-run')
    assert outcome['status']=='stale_proposal' and target.read_text()==old+'Owner correction.\n',outcome
    assert approval.get_pending(approval.SKILLS,staged['pending_id'])==pending
    # A new detached review cannot inherit the prior review's read mark.
    _reset_background_review_read_marks()
    again=json.loads(stage_skill_change({'operations':[operation]}))
    assert again.get('_read_before_write_required') and not again.get('staged'),again
finally:provenance.reset_current_write_origin(token)
print(json.dumps({'passed':True,'native_manager':manager.__file__,'adapter':str(Path(sys.modules['protagine_hermes.review'].__file__).resolve()),
                  'read_guard':'native path mark','changed_after_staging':'stale_proposal','model_calls':0,'live_effect':False}))
'''


def test_native_review_reads_current_skill_before_staging(artifacts,tmp_path):
    native=os.environ.get('PROTAGINE_TEST_HERMES_PATH','')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native skill evaluation')
    _,_,_,installed=artifacts
    result=run_python('-I','-c',READ_PROBE,installed,native,cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['passed']


CREATE_PROBE = r'''
import json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1]); scenario=sys.argv[2]
if sys.argv[3]:sys.path.insert(0,sys.argv[3])
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text('skills:\n  ledger: true\n')
def no_network(*a,**kw):raise AssertionError('Native creation qualification stays offline')
socket.socket.connect=no_network;socket.create_connection=no_network
from tools import skill_manager_tool as manager,skill_provenance as provenance,skill_ledger as ledger,write_approval as approval
from protagine_hermes.review import stage_skill_change,editable_operation
from protagine_hermes.review_evaluation import evaluate_pending,audit_evaluation
name='neutral-json-boundary'
new='---\nname: '+name+'\ndescription: Use when generating strict JSON utilities.\n---\nCheck the declared input and output contract.\n'
operation={'action':'create','name':name,'content':new}
assert editable_operation(operation) is None
target=manager._resolve_skill_dir(name)/'SKILL.md'
assert not target.parent.exists() and manager._find_skill(name) is None
token=provenance.set_current_write_origin('background_review')
try:
    arguments={'operations':[operation]} if scenario=='batch' else dict(operation)
    if scenario=='create_only':arguments['_protagine_review_create_only']=True
    arguments['_protagine_review_base_absent']=False
    staged=json.loads(stage_skill_change(arguments));assert staged['staged'],staged
finally:provenance.reset_current_write_origin(token)
pid=staged['pending_id'];pending=approval.get_pending(approval.SKILLS,pid)
assert pending['payload']['_protagine_review_base_absent'] is True
assert not target.parent.exists() and manager._find_skill(name) is None
phases=[]
def owner_create():
    token=provenance.set_current_write_origin('foreground')
    try:assert json.loads(manager.apply_skill_pending(operation))['success']
    finally:provenance.reset_current_write_origin(token)
def oracle(text,*,phase):
    phases.append(phase)
    if phase=='baseline':
        assert text is None and not target.exists(), 'Absence is not a seeded weak skill'
    else:assert text==new
    if phase=='candidate' and scenario=='concurrent_owner':owner_create()
    if phase=='post_activation':
        assert target.read_text()==new
        if scenario=='owner_changed':target.write_text('LATER_OWNER_EDIT')
        if scenario=='owner_extra_file':(target.parent/'owner-note.md').write_text('KEEP_OWNER_NOTE')
    # Controlled outcomes test lifecycle; actual skill usefulness needs model/task evidence.
    rows=[{'id':'selected-task','passed':text is not None or scenario=='not_improved'},
          {'id':'retained-behavior','passed':True}]
    if phase=='post_activation':
        rows.append({'id':'independent-transfer','passed':scenario not in {'transfer','owner_changed','owner_extra_file','interrupted'}})
    return {'cases':rows,'scope':'controlled creation/evaluation lifecycle'}
if scenario=='preexisting_owner':owner_create()
if scenario=='interrupted':
    real=manager.apply_skill_pending
    def stop_after_native_creation(payload):
        response=real(payload);assert json.loads(response)['success']
        raise KeyboardInterrupt('after native create, before evaluator result')
    with patch.object(manager,'apply_skill_pending',side_effect=stop_after_native_creation):
        try:evaluate_pending(pid,name,oracle,oracle_id='creation-v1')
        except KeyboardInterrupt:pass
        else:raise AssertionError('Expected interrupted native creation')
    assert target.read_text()==new
    phases.clear()
    result=evaluate_pending(pid,name,oracle,oracle_id='creation-v1')
    assert phases==['post_activation'] and result['status']=='rolled_back',result
    assert not target.exists()
elif scenario=='owner_after_intent':
    real=manager.apply_skill_pending
    def owner_won(payload):
        token=provenance.set_current_write_origin('foreground')
        try:assert json.loads(real(operation))['success']
        finally:provenance.reset_current_write_origin(token)
        return json.dumps({'success':False,'error':'An owner independently created the name'})
    with patch.object(manager,'apply_skill_pending',side_effect=owner_won):
        result=evaluate_pending(pid,name,oracle,oracle_id='creation-v1')
    assert result['status']=='apply_failed' and target.read_text()==new,result
    phases.clear()
    result=evaluate_pending(pid,name,oracle,oracle_id='creation-v1')
    assert result['status']=='apply_unobserved' and not phases and target.read_text()==new,result
    assert not any(row['action']=='rollback' for row in ledger.list_entries())
else:
    result=evaluate_pending(pid,name,oracle,oracle_id='creation-v1')
    expected={'activate':'activated','batch':'activated','create_only':'activated','transfer':'rolled_back',
              'owner_changed':'changed_elsewhere','owner_extra_file':'rolled_back',
              'preexisting_owner':'stale_proposal','concurrent_owner':'changed_elsewhere',
              'not_improved':'not_improved'}[scenario]
    assert result['status']==expected,result
    if scenario=='preexisting_owner':assert not phases and target.read_text()==new
    elif scenario=='concurrent_owner':assert phases==['baseline','candidate'] and target.read_text()==new
    elif scenario=='owner_changed':assert target.read_text()=='LATER_OWNER_EDIT'
    elif scenario in {'transfer','owner_extra_file','not_improved'}:
        assert not target.exists() and manager._find_skill(name) is None
        if scenario=='owner_extra_file':assert (target.parent/'owner-note.md').read_text()=='KEEP_OWNER_NOTE'
        if scenario=='not_improved':assert not target.parent.exists()
    else:
        assert target.read_text()==new
        entry=ledger.get_entry(result['evaluation_id'])
        assert entry['before']==[] and entry['evidence']['before_sha256'] is None
        assert entry['evidence']['change_kind']=='create' and len(entry['after'])==1
        assert entry['after'][0]['path']==str(target)
        def later_failed_transfer(text,*,phase):
            return {'cases':[{'id':'selected-task','passed':True},{'id':'retained-behavior','passed':True},
                             {'id':'independent-transfer','passed':False}]}
        result=audit_evaluation(entry['id'],later_failed_transfer,oracle_id='creation-v1')
        assert result['status']=='rolled_back' and not target.exists(),result
print(json.dumps({'passed':True,'scenario':scenario,'model_calls':0,'live_effect':False}))
'''


@pytest.mark.parametrize('scenario', ['activate','batch','create_only','transfer','owner_changed','owner_extra_file',
                                    'preexisting_owner','concurrent_owner','owner_after_intent',
                                    'not_improved','interrupted'])
def test_native_new_skill_creation_and_owned_rollback(artifacts,tmp_path,scenario):
    native=os.environ.get('PROTAGINE_TEST_HERMES_PATH','')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native skill evaluation')
    _,_,_,installed=artifacts
    result=run_python('-I','-c',CREATE_PROBE,installed,scenario,native,cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['passed']
