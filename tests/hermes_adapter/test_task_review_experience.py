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
        'environment':{'PROTAGINE_SELECTED_ORACLE_PLAN':'/controlled/frozen-plan.json'}, 'allow_apply':False}
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
from protagine_hermes import task_review_experience as experience
from protagine_hermes.review import stage_skill_change
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
 'oracle_id':'neutral-frozen-recipe','environment':{'PROTAGINE_FIXTURE_INPUT':'selected'},'allow_apply':scenario!='proposal_only'}
path.write_text(json.dumps(value));evaluator=experience.declaration(path)
batch=experience.selected_batch([],evaluator,reader,'owner')
phases=[];regression=False;unavailable_audit=False
oracle=ModuleType('neutral_oracle')
def check(text,*,phase):
 assert os.environ['PROTAGINE_FIXTURE_INPUT']=='selected'
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
   '_protagine_review_create_only':True,'_protagine_task_assessment_batch':experience.receipt(batch)}))
 finally:skill_provenance.reset_current_write_origin(token)
 assert result['staged'],result
 return result['pending_id'],home/'skills'/name/'SKILL.md',text
if scenario.startswith('successor_'):
 import asyncio,hashlib
 from protagine_hermes.ordinary_skill_review import review_once
 from protagine_hermes import review_successors
 calls=[];results=[];directory=home/'review-results';directory.mkdir()
 name='neutral-task-procedure'
 original='---\nname: '+name+'\ndescription: '+('x'*80 if scenario in {
  'successor_validation','successor_repeat_failure','successor_no_change','successor_erasure'}
  else 'Use complete source evidence.')+'\n---\nRead the supplied source.\n'
 corrected='---\nname: '+name+'\ndescription: Use complete source evidence.\n---\nRead the complete source and its limits.\n'
 async def runtime(config):return {},{'run_deadline_seconds':30}
 async def author(evidence,**options):
  # Scripted native author output qualifies lifecycle, not model quality. The
  # real native staging/validation/pending/ledger paths run without a model.
  calls.append((evidence,options))
  index=len(calls)-1
  if index:
   feedback=options['diagnostic_context']
   assert evidence['task_ids']==batch['task_ids'] and evidence['source_refs']==batch['source_refs']
   assert feedback['successor']['attempt']==index
   assert feedback['proposal']['content']==(original if index==1 else corrected+'\nrevision1')
   assert 'cases' not in json.dumps(feedback) and 'private_expected_answer' not in json.dumps(feedback)
  if index and scenario=='successor_no_change':return {'status':'no_proposal'}
  if index and scenario=='successor_rejected_during_assessment':
   assert write_approval.discard_pending(write_approval.SKILLS,first['pending_id'])
  content=original if not index or scenario in {'successor_identical','successor_repeat_failure'} else corrected
  if scenario=='successor_repeat_failure' and index:content+='\nA changed draft with the same invalid description.'
  if scenario=='successor_limit' and index:content+='\nrevision'+str(index)
  arguments={'action':'create','name':name,'content':content,'_protagine_review_create_only':True,
   '_protagine_task_assessment_batch':experience.receipt(evidence),
   '_protagine_review_batch_sha256':evidence['failure_sha256'],
   '_protagine_review_native_execution':options['native']['id']}
  token=skill_provenance.set_current_write_origin('background_review')
  try:result=json.loads(stage_skill_change(arguments))
  finally:skill_provenance.reset_current_write_origin(token)
  results.append(result)
  return {'status':'proposed' if result.get('staged') else 'no_proposal','pending_id':result.get('pending_id')}
 def fire(identifier):
  return asyncio.run(review_once(home,Path(sys.argv[2]),{'id':identifier,'job_id':'ordinary-review'},
   {},directory,reviewer=author,evaluator=evaluator,connection=reader,owner='owner',resolve_runtime=runtime))
 first=fire('original-fire')
 root=skill_ledger.get_entry(first['claim_id'])
 assert root['evidence']['task_ids']==['task-1','task-2']
 assert experience.selected_batch(skill_ledger.list_entries(),evaluator,reader,'owner') is None
 assert len(calls)==1
 if first['status']=='proposed':
  if scenario=='successor_oracle_unavailable':
   def unavailable(text,*,phase):
    phases.append((phase,text))
    if phase=='candidate':raise RuntimeError('Controlled unavailable measurement')
    return {'cases':[{'id':'current-source','passed':False}],'private_expected_answer':'hidden'}
   oracle.check=unavailable
  else:regression=True
  outcome=fire('first-evaluation')
  assert outcome['status']==('unavailable' if scenario=='successor_oracle_unavailable' else 'not_improved'),outcome
  assert len(calls)==1
 failures=[r for r in skill_ledger.list_entries() if r['evidence'].get('version')==review_successors.VERSION]
 assert len(failures)==1,failures
 retained=json.dumps(failures[0],sort_keys=True)
 failure=failures[0]['evidence']
 assert failure['root_claim_id']==root['id'] and failure['claim_id']==root['id']
 assert hashlib.sha256(skill_ledger.read_blob(failure['proposal_blob'])).hexdigest()==failure['payload_sha256']
 if scenario=='successor_owner_reject':
  assert write_approval.discard_pending(write_approval.SKILLS,first['pending_id'])
 if scenario=='successor_erasure':reader.current=False
 if scenario=='successor_oracle_unavailable':
  assert failure['kind']=='oracle_unavailable' and not failure['candidate_measured']
  assert failure['measurement_progress']['baseline']['private_expected_answer']=='hidden'
 before=len(phases)
 second=fire('successor-fire')
 if scenario in {'successor_owner_reject','successor_erasure','successor_oracle_unavailable'}:
  assert len(calls)==1 and len(phases)==before
 else:
  assert len(calls)==2
  child=skill_ledger.get_entry(second['claim_id'])
  link=child['evidence']['successor']
  assert link=={'root_claim_id':root['id'],'parent_claim_id':root['id'],
   'failure_entry_id':failures[0]['id'],'attempt':1}
  assert child['evidence']['observation_ids']==root['evidence']['observation_ids']
  # The same actual scheduled execution cannot claim another successor.
  fire('successor-fire')
  assert len(calls)==2
  if scenario in {'successor_validation','successor_measured'}:
   assert second['status']=='proposed' and not (home/'skills'/name/'SKILL.md').exists()
   assert write_approval.get_pending(write_approval.SKILLS,second['pending_id'])
   assert len(phases)==before  # Staging the changed proposal is not evaluation.
   regression=False
   measured=fire('changed-evaluation')
   assert measured['status']=='activated' and (home/'skills'/name/'SKILL.md').read_text()==corrected
   assert [phase for phase,_ in phases[before:]]==['baseline','candidate','post_activation']
   assert not write_approval.list_pending(write_approval.SKILLS)
  elif scenario=='successor_rejected_before_evaluation':
   assert second['status']=='proposed'
   assert write_approval.discard_pending(write_approval.SKILLS,first['pending_id'])
   from protagine_hermes.review_evaluation import evaluate_pending
   rejected=evaluate_pending(second['pending_id'],name,oracle.check,oracle_id='neutral-frozen-recipe')
   assert rejected['status']=='proposal_rejected',rejected
   fire('rejected-evaluation')
   assert len(phases)==before and not (home/'skills'/name/'SKILL.md').exists()
  elif scenario=='successor_limit':
   assert fire('second-evaluation')['status']=='not_improved'
   third=fire('last-successor')
   assert len(calls)==3 and skill_ledger.get_entry(third['claim_id'])['evidence']['successor']['attempt']==2
   assert fire('third-evaluation')['status']=='not_improved'
   fire('exhausted')
   assert len(calls)==3
  else:
   assert second['status']=='no_proposal'
  for i in range(2):fire('later-'+str(i))
  assert len(calls)==(3 if scenario=='successor_limit' else 2)
 assert json.dumps(skill_ledger.get_entry(failures[0]['id']),sort_keys=True)==retained
 assert len(reader.records)==2 and experience.selected_batch(skill_ledger.list_entries(),evaluator,reader,'owner') is None
 assert all(not r['evidence'].get('quality_credit') for r in skill_ledger.list_entries()
  if r['action']=='ordinary_skill_review')
 stopped=[r['evidence']['reason'] for r in skill_ledger.list_entries() if r['evidence'].get('status')=='successor_stopped']
 expected={'successor_identical':'identical_candidate','successor_repeat_failure':'repeated_deterministic_failure',
  'successor_no_change':'no_supported_change','successor_limit':'successor_limit',
  'successor_owner_reject':'pending_removed_or_changed','successor_erasure':'source_unavailable',
  'successor_rejected_during_assessment':'pending_removed_or_changed',
  'successor_rejected_before_evaluation':'pending_removed_or_changed',
  'successor_oracle_unavailable':'evaluation_unavailable'}.get(scenario)
 if expected:assert expected in stopped,(scenario,stopped)
 print(json.dumps({'passed':True,'scenario':scenario,'production_learning_proof':False}));sys.exit(0)
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
  assert os.environ.get('PROTAGINE_FIXTURE_INPUT') is None
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
                                     'declaration_changed','owner_edit','audit_rotation','audit_unavailable_pending',
                                     'successor_validation','successor_measured','successor_identical',
                                     'successor_repeat_failure','successor_no_change','successor_limit',
                                     'successor_owner_reject','successor_erasure','successor_oracle_unavailable',
                                     'successor_rejected_during_assessment','successor_rejected_before_evaluation'])
def test_actual_native_task_candidate_evaluation_and_later_audit(artifacts,tmp_path,scenario):
    import os
    from conftest import run_python
    from test_native_current_work import environment
    native=os.environ.get('PROTAGINE_TEST_HERMES_PATH')
    if not native:
        pytest.skip('Select qualified native source for task-review evaluation')
    installed=artifacts[3]
    result=run_python('-I','-c',NATIVE,installed,native,scenario,cwd=tmp_path,env=environment(tmp_path))
    assert json.loads(result.stdout.splitlines()[-1])['passed']
