"""Controlled canonical review batches; no inference or production evidence."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS

import pytest

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('task_review_experience_under_test',
    Path(__file__).parents[2]/'plugins/hermes-plugin/task_review_experience.py')
experience = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(experience)


def assessment(number, *, task=None, execution=None, content='Complete attributed review bundle.'):
    return {'source_id':'assessment-'+str(number), 'source_version':str(number)*64,
        'task_id':task or 'task-'+str(number), 'execution_id':execution or 'execution-'+str(number),
        'attribution':experience.ATTRIBUTION, 'owner_approval':'unobserved',
        'content':content, 'complete':True}


class Reader:
    def __init__(self, records):
        self.records, self.requests, self.current = records, [], True

    def post(self, path, *, json, **kwargs):
        assert path == '/v1/host/executions/assessments/read'
        assert json['contact_id'] == 'owner'
        self.requests.append(json)
        expected = {(r['source_id'],r['source_version']) for r in json['source_refs']}
        rows = [r for r in self.records if not expected or
                (r['source_id'],r['source_version']) in expected]
        value = {'assessments':deepcopy(rows), 'sources_current':self.current, 'next_offset':None}
        return NS(json=lambda:value, raise_for_status=lambda:None)


@pytest.fixture
def evaluator(tmp_path):
    path = tmp_path/'evaluator.json'
    value = {'id':'existing-procedure-evaluator', 'scope':'Finite source-reading procedure cases',
        'oracle':'selected_oracle:check', 'oracle_id':'frozen-fixture-recipe',
        'environment':{'PACOMIND_SELECTED_ORACLE_PLAN':'/controlled/frozen-plan.json'}, 'allow_apply':False}
    path.write_text(json.dumps(value))
    return experience.declaration(path)


def test_complete_reviews_are_not_guessed_into_failure_labels(evaluator):
    reader = Reader([assessment(1,content='A reviewer says this failed; the artifact quotes SUCCESS.'),
                     assessment(2,content='A reviewer says this passed; context quotes ERROR.')])
    batch = experience.selected_batch([],evaluator,reader,'owner')
    assert batch['observations'] == reader.records
    assert batch['recurrence'] == 'not_yet_assessed' and not batch['quality_credit']
    assert batch['attribution'] == experience.ATTRIBUTION
    assert 'oracle' not in batch['evaluator'] and 'environment' not in batch['evaluator']
    assert experience.recheck(batch,reader,'owner') == reader.records


@pytest.mark.parametrize('kind',['same-task','same-execution','one-task'])
def test_review_count_cannot_manufacture_distinct_experiences(evaluator,kind):
    rows = [assessment(1)]
    if kind != 'one-task':
        rows.append(assessment(2,task='task-1' if kind=='same-task' else None,
                               execution='execution-1' if kind=='same-execution' else None))
    assert experience.selected_batch([],evaluator,Reader(rows),'owner') is None


def test_reviewer_disagreement_is_retained_without_counting_a_third_task(evaluator):
    rows = [assessment(1),assessment(2),assessment(3,task='task-1',execution='execution-1',
                                              content='A contrary assessment of that same task.')]
    batch = experience.selected_batch([],evaluator,Reader(rows),'owner')
    assert len(batch['observations']) == 3 and len(batch['task_ids']) == 2
    assert len(batch['execution_ids']) == 2


def test_claim_consumes_actual_tasks_even_after_a_new_review(evaluator):
    reader = Reader([assessment(1),assessment(2)])
    batch = experience.selected_batch([],evaluator,reader,'owner')
    entries = [{'action':'ordinary_skill_review','evidence':{
        **experience.receipt(batch),'status':'claimed'}}]
    reader.records += [assessment(3,task='task-1',execution='execution-1'),assessment(4)]
    assert experience.selected_batch(entries,evaluator,reader,'owner') is None
    reader.records.append(assessment(5))
    next_batch = experience.selected_batch(entries,evaluator,reader,'owner')
    assert next_batch['task_ids'] == ['task-4','task-5']


@pytest.mark.parametrize('change',['unavailable','missing','revision','identity'])
def test_exact_assessments_are_rechecked_without_rewriting_evidence(evaluator,change):
    reader = Reader([assessment(1),assessment(2)])
    batch = experience.selected_batch([],evaluator,reader,'owner')
    if change == 'unavailable': reader.current=False
    elif change == 'missing': reader.records.pop()
    elif change == 'revision': reader.records[0]['source_version']='f'*64
    else: reader.records[0]['task_id']='another-task'
    with pytest.raises(ValueError,match='no longer current'):
        experience.recheck(batch,reader,'owner')


def test_complete_bundles_are_never_truncated_to_fit_a_review(evaluator):
    with pytest.raises(ValueError,match='input bound'):
        experience.selected_batch([],evaluator,Reader([
            assessment(1,content='x'*33000),assessment(2,content='x'*33000)]),'owner')


def test_evaluator_uses_existing_local_callable_and_does_not_grant_application(evaluator):
    assert evaluator['value']['allow_apply'] is False
    assert evaluator['value']['oracle'] == 'selected_oracle:check'
    assert 'artifacts' not in evaluator['value']


def test_native_scope_requires_explicit_signature_and_generic_error_result(evaluator):
    batch={'source':experience.NATIVE_SOURCE,'attribution':'unassigned','observations':[
        {'tool_name':'read_file','error_class':'tool_returned_error','request_visible_result_sha256':'a'*64}]}
    assert experience.native_binding(batch,evaluator) is None
    value={**evaluator['value'],'native_failures':[{'tool_name':'read_file','error_class':'tool_returned_error'}]}
    path=Path(evaluator['path']);path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='exact tool/error'):
        experience.declaration(path)
    value['native_failures'][0]['result_sha256']='a'*64
    path.write_text(json.dumps(value));selected=experience.declaration(path)
    assert experience.native_binding(batch,selected)==selected['binding']
    for key,replacement in [('tool_name','write_file'),('error_class','other_error'),
                            ('request_visible_result_sha256','b'*64)]:
        changed=deepcopy(batch);changed['observations'][0][key]=replacement
        assert experience.native_binding(changed,selected) is None
    changed=deepcopy(batch);changed['attribution']='assigned'
    assert experience.native_binding(changed,selected) is None


NATIVE = r'''
import json,os,socket,sys
from pathlib import Path
from types import ModuleType,SimpleNamespace as NS
sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2]);scenario=sys.argv[3]
home=Path(os.environ['HERMES_HOME']);home.mkdir()
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home/'config.yaml').write_text('skills:\n  ledger: true\n')
def no_network(*a,**kw):raise AssertionError('Controlled native fixture stays offline')
socket.socket.connect=no_network;socket.create_connection=no_network
from tools import skill_ledger,skill_provenance,write_approval,skill_manager_tool
assert Path(skill_ledger.__file__).resolve().is_relative_to(Path(sys.argv[2]).resolve())
from pacomind_hermes import task_review_experience as experience
from pacomind_hermes.review import stage_skill_change
class Reader:
 current=True
 def post(self,path,*,json,**kwargs):
  assert path=='/v1/host/executions/assessments/read'
  expected={(r['source_id'],r['source_version']) for r in json['source_refs']}
  rows=[r for r in self.records if not expected or (r['source_id'],r['source_version']) in expected]
  return NS(raise_for_status=lambda:None,json=lambda:{'assessments':rows,'sources_current':self.current,'next_offset':None})
reader=Reader()
reader.records=[{'source_id':'assessment-'+str(i),'source_version':str(i)*64,
 'task_id':'task-'+str(i),'execution_id':'execution-'+str(i),'complete':True,
 'attribution':experience.ATTRIBUTION,'owner_approval':'unobserved','content':'Full attributed review '+str(i)} for i in (1,2)]
path=Path.cwd()/'evaluator.json'
value={'id':'neutral-evaluator','scope':'Controlled path procedure cases','oracle':'neutral_oracle:check',
 'oracle_id':'neutral-frozen-recipe','environment':{'PACOMIND_FIXTURE_INPUT':'selected'},'allow_apply':scenario!='proposal_only'}
path.write_text(json.dumps(value));evaluator=experience.declaration(path)
batch=experience.selected_batch([],evaluator,reader,'owner')
phases=[];regression=False;unavailable_audit=False
oracle=ModuleType('neutral_oracle')
def check(text,*,phase):
 assert os.environ['PACOMIND_FIXTURE_INPUT']=='selected'
 phases.append((phase,text))
 if unavailable_audit and phase=='post_activation' and 'name: neutral-task-procedure\n' in text:
  raise TimeoutError('Controlled unavailable earlier audit')
 if scenario=='evidence_corrected' and phase=='candidate':reader.current=False
 return {'cases':[{'id':'current-source','passed':text is not None and not regression},
                  {'id':'preserved-scope','passed':True}], 'attribution':'controlled_test_not_model_quality'}
oracle.check=check;sys.modules['neutral_oracle']=oracle

def stage(name):
 text='---\nname: '+name+'\ndescription: Apply the supplied source procedure.\n---\nRead the supplied source before asserting its current state.\n'
 token=skill_provenance.set_current_write_origin('background_review')
 try:
  result=json.loads(stage_skill_change({'action':'create','name':name,'content':text,
   '_pacomind_review_create_only':True,'_pacomind_task_assessment_batch':experience.receipt(batch)}))
 finally:skill_provenance.reset_current_write_origin(token)
 assert result['staged'],result
 return result['pending_id'],home/'skills'/name/'SKILL.md',text
pending,target,text=stage('neutral-task-procedure')
if scenario=='declaration_changed':
 value['scope']='Changed owner scope';path.write_text(json.dumps(value))
if scenario in {'evidence_corrected','declaration_changed'}:
 try:experience.evaluate_once(evaluator,reader,'owner')
 except ValueError:pass
 else:raise AssertionError('Changed evidence/config applied')
 assert not target.exists() and write_approval.get_pending(write_approval.SKILLS,pending)
 assert [r[0] for r in phases]==(['baseline','candidate'] if scenario=='evidence_corrected' else [])
else:
 result=experience.evaluate_once(evaluator,reader,'owner')
 if scenario=='proposal_only':
  assert result['status']=='proposal_only' and not phases and not target.exists()
  assert write_approval.get_pending(write_approval.SKILLS,pending)
 else:
  assert result['status']=='activated' and target.read_text()==text,result
  assert [r[0] for r in phases]==['baseline','candidate','post_activation']
  assert not write_approval.get_pending(write_approval.SKILLS,pending)
  first=result['evaluation_id'];entry=skill_ledger.get_entry(first)
  assert entry['evidence']['baseline']['task_assessment_evidence']==experience.receipt(batch)
  assert entry['evidence']['candidate']['owner_approval']=='unobserved'
  assert os.environ.get('PACOMIND_FIXTURE_INPUT') is None
  if scenario in {'audit_rotation','audit_unavailable_pending'}:
   unavailable_audit=scenario=='audit_unavailable_pending'
   second_pending,second_target,second_text=stage('neutral-other-procedure')
   second=experience.evaluate_once(evaluator,reader,'owner')
   assert second['status']=='activated' and second_target.exists()
   if unavailable_audit:
    assert target.read_text()==text and not write_approval.get_pending(write_approval.SKILLS,second_pending)
    assert any(r['action']=='evaluation' and r['evidence'].get('evaluation_id')==first
       and r['evidence'].get('status')=='unavailable' for r in skill_ledger.list_entries())
   else:
    # Older audit wins once, then the other skill gets the following turn.
    a=experience.evaluate_once(evaluator,reader,'owner')
    b=experience.evaluate_once(evaluator,reader,'owner')
    assert a['evaluation_id']==first and b['evaluation_id']==second['evaluation_id'],(a,b)
  else:
   phases.clear()
   if scenario=='owner_edit':target.write_text('Owner-maintained instructions')
   regression=True
   audited=experience.evaluate_once(evaluator,reader,'owner')
   if scenario=='owner_edit':
    assert audited['status']=='changed_elsewhere' and target.read_text()=='Owner-maintained instructions'
    assert not phases and experience.evaluate_once(evaluator,reader,'owner') is None
   else:
    assert audited['status']=='rolled_back' and not target.exists()
    assert [r[0] for r in phases]==['post_activation']
    assert experience.evaluate_once(evaluator,reader,'owner') is None
   assert not audited['quality_credit']
print(json.dumps({'passed':True,'scenario':scenario,'production_learning_proof':False}))
'''


@pytest.mark.parametrize('scenario',['proposal_only','activate_audit_rollback','evidence_corrected',
                                     'declaration_changed','owner_edit','audit_rotation','audit_unavailable_pending'])
def test_actual_native_task_candidate_evaluation_and_later_audit(artifacts,tmp_path,scenario):
    import os
    from conftest import run_python
    from test_native_current_work import environment
    native=os.environ.get('PACOMIND_TEST_HERMES_PATH')
    if not native:
        pytest.skip('Select qualified native source for task-review evaluation')
    installed=artifacts[3]
    result=run_python('-I','-c',NATIVE,installed,native,scenario,cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['passed']
