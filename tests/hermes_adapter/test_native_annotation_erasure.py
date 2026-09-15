"""An annotation's actual native creating call survives acknowledgement loss."""
import pytest

import test_source_annotate as annotation


@pytest.mark.parametrize('erase_target,changed_call', [(False,False),(True,False),(False,True)])
def test_annotation_erasure_owns_creating_call_and_supplied_copy(artifacts, tmp_path, monkeypatch, erase_target, changed_call):
    probe = annotation.PROBE
    # Give the parent source a real native origin, independent of the turn
    # whose assistant creates an annotation. Keep same-turn user facts too.
    probe = probe.replace("native_db=SessionDB(home/'state.db')", """native_db=SessionDB(home/'state.db')
native_db.create_session('work','cli')
native_db.append_message('work','assistant',report)
""")
    probe = probe.replace("row={'role':'user','content':''}", "row={'role':'user','content':'Independent parent fact: the spare lamp is rechargeable.'}")
    probe = probe.replace("user_message='',conversation_history=[row]", "user_message=row['content'],conversation_history=[row]")
    probe = probe.replace("compose_user_api_content('',recalled", "compose_user_api_content(row['content'],recalled")
    probe = probe.replace("native_db.append_message(session,'user','',api_content=row['api_content'])",
        "native_db.append_message(session,'user',row['content'],api_content=row['api_content'])")
    probe = probe.replace('first=dispatch(args)', 'first=dispatch(args)\n    first_creating_row=dispatch.last_native_row')
    check = r'''
import asyncio
from protagine_hermes.client import TurnOutbox
from protagine_hermes.request_memory import RequestMemory
from protagine_hermes.native_owned_copies import NativeOwnedCopies
outbox=TurnOutbox(home/'state'/'protagine-turn-outbox.sqlite3')
memory=RequestMemory(protagine_hermes.ProtagineClient(url='http://fixture',api_key='fixture-key'),outbox)
owned=NativeOwnedCopies(memory,NS())
before=native_db.get_messages('later')
first_call=next(row for row in before if row['id']==first_creating_row)
native_db.append_message('later','assistant','The annotation says: '+correction)
native_db.append_message('later','user','Independent later request about the lamp.')
before=native_db.get_messages('later')
independent={row['id']:row for row in before if row['role']=='user'}
erased_ids={row['id'] for row in before if first_call['id']<=row['id']<before[-1]['id']}
# A separate native consumer carries the accepted annotation revision.
native_db.create_session('annotation-reader','cli')
reader=NS(contact_id='person',session_id='annotation-reader',task_id='reader',
    turn_id='reader',valid_participant=True,platform='cli')
reader_row={'role':'user','content':'Use the archive note.'}
reader_row['_row_id']=native_db.append_message('annotation-reader','user',reader_row['content'],api_content=correction)
memory.observe_native_anchor(reader,[reader_row],user_message=reader_row['content'])
assert owned.retain(reader,[{k:retry[k] for k in ('source_id','source_version')}])
native_db.append_message('annotation-reader','assistant','The note says: '+correction)
if CHANGED_CALL:
    native_db._execute_write(lambda db: db.execute('UPDATE messages SET tool_calls=? WHERE id=?',
        (json.dumps([{'id':'replacement','type':'function','function':{
            'name':'terminal','arguments':json.dumps({'command':'independent replacement'})}}]),first_creating_row)))
ledger.erase_sources(contact_id='person',turn_ids=['report' if ERASE_TARGET else retry['source_id']])
settled=asyncio.run(owned.reconcile(contact='person'))
assert settled['status']==('pending' if CHANGED_CALL else 'settled') and settled['pending']==int(CHANGED_CALL),settled
after={row['id']:row for row in native_db.get_messages('later')}
if CHANGED_CALL:
    assert 'independent replacement' in json.dumps(after[first_creating_row]),after[first_creating_row]
    pending=[row for row in owned._rows('person',actionable=True) if row['metadata']['kind']=='erasure']
    assert len(pending)==1 and pending[0]['metadata']['pending_reason']=='native_source_anchor_changed',pending
else:
    for key in erased_ids:
        row=after[key]
        assert row['display_metadata'].get('redacted_from_sha256') and correction not in json.dumps(row),row
        assert all(json.loads(call['function']['arguments'])=={} for call in row.get('tool_calls') or []),row
assert all(after[key]['content']==row['content'] for key,row in independent.items()),after
assert all(correction not in json.dumps(row) for row in native_db.get_messages('annotation-reader'))
assert native_db.get_messages('annotation-reader')[0]['content']==reader_row['content']
assert bool(ledger.source_references(['report'],contact_id='person',session_id='later')) is (not ERASE_TARGET)
assert native_db.get_messages('work')[0]['content']==('[Content removed.]' if ERASE_TARGET else report)
assert len([row for row in owned._rows('person',actionable=True) if row['metadata']['kind']=='erasure'])==int(CHANGED_CALL)
'''.replace('ERASE_TARGET', repr(erase_target)).replace('CHANGED_CALL', repr(changed_call))
    marker="print(json.dumps({'native_dispatch':True"
    assert marker in probe
    probe=probe.replace(marker,check+'\n'+marker)
    monkeypatch.setattr(annotation,'PROBE',probe)
    annotation.test_native_annotation_uses_supplied_revision_and_retries_unknown_ack(artifacts,tmp_path)


@pytest.mark.parametrize('malformed_hash', ['missing', 'integer'])
def test_annotation_malformed_receipt_does_not_resubmit_and_shared_call_is_rejected(artifacts, tmp_path, monkeypatch, malformed_hash):
    probe=annotation.PROBE.replace('posts=[]; lose_ack=True', 'posts=[]; lose_ack=True; malformed_receipt=False')
    probe=probe.replace("posts.append((json.loads(request.content),response.status_code,response.json()))", """posts.append((json.loads(request.content),response.status_code,response.json()))
        if malformed_receipt and response.status_code==200:
            body=response.json(); body.pop('source_message_hash',None)
            return httpx.Response(response.status_code,json=body)
""")
    if malformed_hash=='integer':
        probe=probe.replace("body.pop('source_message_hash',None)", "body['source_message_hash']=int('1'*64)")
    probe=probe.replace('agent._execute_tool_calls_sequential(explicit_call,results,effective_task_id=task)', """if call=='shared-call':
            stored=native_db.get_messages(session)[-1]['tool_calls']
            stored.append({'id':'independent-call','type':'function','function':{
                'name':'terminal','arguments':json.dumps({'command':'independent fact'})}})
            native_db._execute_write(lambda db: db.execute('UPDATE messages SET tool_calls=? WHERE id=?',
                (json.dumps(stored),dispatch.last_native_row)))
        agent._execute_tool_calls_sequential(explicit_call,results,effective_task_id=task)""")
    check=r'''
before=len(posts)
shared=dispatch({**args,'correction':correction+' Separate note.'},call='shared-call')
assert shared.get('accepted') is False and len(posts)==before,shared
malformed_receipt=True
unknown=dispatch({**args,'correction':correction+' Separate note.'},call='malformed-receipt')
assert unknown.get('confirmation')=='unknown' and 'accepted' not in unknown,unknown
assert len(posts)==before+1,posts
with sqlite3.connect(ledger.db_path) as db:
    assert db.execute('SELECT count(*) FROM source_annotations').fetchone()[0]==2
'''
    marker="print(json.dumps({'native_dispatch':True"
    assert marker in probe
    monkeypatch.setattr(annotation,'PROBE',probe.replace(marker,check+'\n'+marker))
    annotation.test_native_annotation_uses_supplied_revision_and_retries_unknown_ack(artifacts,tmp_path)
