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


def test_qualification_replay_and_unobserved_skill_are_not_ordinary_experience():
    for fields in [{'authority_lane':'system'}, {'platform':'cli'}, {'platform':'cron'},
                   {'authority_lane':'guest'},{'resolution_status':'attested_system'}]:
        assert experience.observations(*observed(**fields))==[]
    scope,messages,errors=observed()
    assert experience.observations(scope,messages+[{'role':'user','content':'Different turn'}],errors)==[]
    assert experience.observations(scope,[m for m in messages if m.get('tool_call_id')!='view-a'],errors)==[]
    value=experience.observations(scope,messages,errors)[0][1]
    assert value['skill_sha256']==SHA
    assert TEXT not in json.dumps(value) and 'real incident' not in json.dumps(value)


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
