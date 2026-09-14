"""Short ordinary turns accumulate without inventing task or skill failures."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import hashlib
import json
import sys

SOURCE=Path(__file__).parents[2]/'plugins/hermes-plugin/review_experience.py'
spec=importlib.util.spec_from_file_location('review_experience',SOURCE)
experience=importlib.util.module_from_spec(spec);spec.loader.exec_module(experience)
TEXT='Neutral task playbook.'
SHA=hashlib.sha256(TEXT.encode()).hexdigest()


def observed(session='a',turn='one',**fields):
    scope=NS(authority_lane='owner',resolution_status='resolved',contact_id='neutral-owner',
             platform='whatsapp',session_id=session,turn_id=turn)
    for key,value in fields.items():setattr(scope,key,value)
    raw=json.dumps({'error':'Tool fixture failed; this is not a real incident.'})
    calls=[{'role':'user','content':'Neutral task'},
        {'role':'assistant','tool_calls':[{'id':'view-'+session,'function':{
            'name':'skill_view','arguments':json.dumps({'name':'artifact-sanitization'})}}]},
        {'role':'tool','tool_call_id':'view-'+session,'content':json.dumps({
            'success':True,'name':'artifact-sanitization','content':TEXT})},
        {'role':'assistant','tool_calls':[{'id':'failure-'+session,'function':{
            'name':'read_file','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'failure-'+session,'content':raw}]
    errors=[{'tool_call_id':'failure-'+session,'tool_name':'read_file','error_class':'tool_returned_error',
             'request_visible_result_sha256':hashlib.sha256(raw.encode()).hexdigest()}]
    return scope,calls,errors


def entries(*sessions):
    return [{'action':experience.ACTION,'skill':name,'evidence':value}
            for session in reversed(sessions) for name,value in experience.observations(*observed(session))]


def test_shared_recurrence_uses_distinct_real_turns_and_current_skill():
    assert experience.next_batch(entries('a'),'artifact-sanitization',SHA) is None
    batch=experience.next_batch(entries('a','b'),'artifact-sanitization',SHA)
    assert {x['session_id'] for x in batch['observations']}=={'a','b'}
    assert len(batch['observation_ids'])==2
    assert experience.next_batch(entries('a','a'),'artifact-sanitization',SHA) is None
    assert experience.next_batch(entries('a','b'),'artifact-sanitization','0'*64) is None
    assert experience.next_batch(entries('a','b'),'another-skill',SHA) is None
    mixed=entries('a','b');mixed[0]['evidence']['request_visible_result_sha256']='0'*64
    assert experience.next_batch(mixed,'artifact-sanitization',SHA) is None
    receipt={'action':'ordinary_skill_review','evidence':batch}
    assert experience.next_batch([receipt,*entries('a','b')],'artifact-sanitization',SHA) is None


def test_qualification_and_replayed_old_turn_are_not_ordinary_experience():
    for fields in [{'authority_lane':'system'}, {'platform':'cli'}, {'platform':'cron'},
                   {'authority_lane':'guest'},{'resolution_status':'attested_system'}]:
        assert experience.observations(*observed(**fields))==[]
    scope,messages,errors=observed()
    assert experience.observations(scope,messages+[{'role':'user','content':'Different turn'}],errors)==[]
    value=experience.observations(scope,messages,errors)[0][1]
    assert value['skill_sha256']==SHA
    assert TEXT not in json.dumps(value) and 'real incident' not in json.dumps(value)


def test_unviewed_failure_retains_real_occurrence_without_skill_attribution():
    rows=[]
    for session in ('a','b'):
        scope,messages,errors=observed(session)
        messages=[m for m in messages if m.get('tool_call_id')!='view-'+session]
        values=experience.observations(scope,messages,errors)
        assert len(values)==1
        name,value=values[0]
        assert name is None and value['attribution']=='unattributed'
        assert 'skill_call_id' not in value and 'skill_sha256' not in value
        rows.insert(0,{'action':'ordinary_tool_failure','skill':None,'evidence':value})
    assert experience.next_batch(rows[:1]) is None
    batch=experience.next_batch(rows)
    assert batch['skill'] is None and batch['skill_sha256'] is None
    assert batch['attribution']=='unattributed'
    assert len(batch['observations'])==2 and len(batch['observation_ids'])==2
    assert {r['session_id'] for r in batch['observations']}=={'a','b'}
    assert experience.next_batch([rows[0],rows[0]]) is None
    assert experience.next_batch([{'action':'ordinary_skill_review','evidence':batch},*rows]) is None
    assert experience.next_batch(rows,'artifact-sanitization',SHA) is None
    assert experience.next_batch(entries('a','b')) is None


def test_unattributed_recurring_call_id_is_distinct_per_native_turn():
    rows=[]
    for turn in ('one','two'):
        scope,messages,errors=observed('same-session',turn)
        messages=[m for m in messages if m.get('tool_call_id')!='view-same-session']
        name,value=experience.observations(scope,messages,errors)[0]
        rows.insert(0,{'action':'ordinary_tool_failure','skill':name,'evidence':value})
    assert len({row['evidence']['observation_id'] for row in rows})==2
    assert len(experience.next_batch(rows)['observations'])==2
    assert experience.next_batch([rows[0],rows[0]]) is None
    changed=json.loads(json.dumps(rows))
    changed[0]['evidence']['request_visible_result_sha256']='0'*64
    assert experience.next_batch(changed) is None


def test_retained_native_ledger_records_survive_fresh_collector(monkeypatch,tmp_path):
    # The native ledger interface is represented by its on-disk JSONL contract;
    # the existing native package check separately exercises its actual writer.
    path=tmp_path/'ledger.jsonl'
    def read():
        return [json.loads(line) for line in reversed(path.read_text().splitlines())] if path.exists() else []
    def append(action,skill,**kwargs):
        with path.open('a') as stream:stream.write(json.dumps({'action':action,'skill':skill,**kwargs})+'\n')
        return 'written'
    ledger=NS(list_entries=read,append_entry=append)
    monkeypatch.setitem(sys.modules,'tools',NS(skill_ledger=ledger))
    experience.retain(*observed('a'))
    experience.retain(*observed('a'))
    assert len(read())==1
    fresh=importlib.util.module_from_spec(spec);spec.loader.exec_module(fresh)
    fresh.retain(*observed('b'))
    assert len(read())==2 and fresh.next_batch(read(),'artifact-sanitization',SHA)


def test_tool_recurrence_survives_different_skill_contexts():
    rows = entries('first')
    rows[0]['skill'] = 'unrelated-playbook'
    scope, messages, errors = observed('second')
    messages = [m for m in messages if m.get('tool_call_id') != 'view-second']
    name, evidence = experience.observations(scope, messages, errors)[0]
    rows.insert(0, {'action': experience.UNATTRIBUTED_ACTION, 'skill': name, 'evidence': evidence})
    assert experience.next_batch(rows) is None
    assert experience.next_batch(rows, 'artifact-sanitization', SHA) is None
    before = json.dumps(rows, sort_keys=True)
    batch = experience.next_tool_batch(rows)
    assert batch['skill'] is None and batch['attribution'] == 'unassigned'
    by_session = {row['session_id']: row for row in batch['observations']}
    assert by_session['first']['skill_views'] == [{'skill': 'unrelated-playbook',
        'skill_call_id': 'view-first', 'skill_sha256': SHA}]
    assert by_session['second']['skill_views'] == []
    assert {row['observation_id'] for row in batch['observations']} == set(batch['observation_ids'])
    assert json.dumps(rows, sort_keys=True) == before


def test_multiple_skill_views_are_one_tool_execution_and_one_consumable_batch():
    rows = entries('first', 'second')
    duplicate = json.loads(json.dumps(rows[-1]))
    duplicate['skill'] = 'another-viewed-playbook'
    duplicate['evidence'].update(skill_call_id='another-view', observation_id='another-reference')
    rows.append(duplicate)
    batch = experience.next_tool_batch(rows)
    assert len(batch['observations']) == 2 and len(batch['observation_ids']) == 3
    assert len(next(row for row in batch['observations']
                    if row['session_id'] == 'first')['skill_views']) == 2
    assert experience.next_tool_batch([rows[-1], rows[-2]]) is None
    assert experience.next_tool_batch([{'action': 'ordinary_skill_review', 'evidence': batch}, *rows]) is None
    # A skill-specific review has already consumed this execution. Its other
    # skill view must not make the same call look new to the generic selector.
    claim = {'action': 'ordinary_skill_review', 'evidence': {
        'observation_ids': [rows[-2]['evidence']['observation_id']]}}
    assert experience.next_tool_batch([claim, *rows]) is None


def test_tool_recurrence_keeps_distinct_turns_people_and_failure_classes():
    assert experience.next_tool_batch(entries('first', 'first')) is None
    for key, value in [('contact_id', 'another-owner'), ('tool_name', 'another-tool'),
                       ('request_visible_result_sha256', '0' * 64)]:
        rows = entries('first', 'second')
        rows[0]['evidence'][key] = value
        assert experience.next_tool_batch(rows) is None
    rows = entries('first', 'second')
    for row in rows:
        row['evidence']['error_class'] = 'kernel_timeout_state_lost'
    rows[0]['evidence']['request_visible_result_sha256'] = '0' * 64
    assert len(experience.next_tool_batch(rows)['observations']) == 2
