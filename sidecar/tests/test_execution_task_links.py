"""A conversation can locate its native work without searching the filesystem."""
import json

from protagine.turns.executions import request_work_context
from protagine.turns.source_read import read
from test_execution_input_purpose import admitted
from test_execution_registry import observation, store


def task(store, name='coding', *, person='owner',
         text='Build a run summary utility. Preserve unconfirmed outcomes.'):
    refs = admitted(store, name=name + '-input', person=person,
                    text=text)
    value = observation(name, contact_id=person, input_refs=refs,
                        task_experience={'task_id': 'a' * 64, 'purpose': 'qualification',
                                         'origin_platform': 'cli'})
    store.observe(value, principal_id='native-host', contact_id=person)
    return value


def test_live_task_has_usable_handle_and_reopenable_original_input(store):
    value = task(store)
    view = store.view(contact_id='owner', owner=True, session_id='other-conversation')
    row = view['items'][0]
    assert row['task_id'] == 'a' * 64
    reference = row['request_input']
    opened = read(store.ledger, contact_id='owner', session_id='other-conversation',
                  source_id=reference['source_id'], source_version=reference['source_version'])
    assert 'run summary utility' in opened['content']
    context = request_work_context(view, session_id='other-conversation')['text']
    assert '"task_id": "' + 'a' * 64 + '"' in context
    assert '"input_source"' in context
    assert 'protagine_memory_read_source' in context
    assert 'terminal observation is not proof of useful completion' in context
    # Work observations also reach workers without owner task controls.
    # The owning adapter supplies available actions, not this shared data view.
    assert 'Use task_id with protagine_task' not in context
    # The task handle comes from its admitted metadata, not a session or turn ID.
    assert value['execution_id'] != row['task_id']


def test_finished_task_remains_inspectable_after_foreground_turns(store):
    value = task(store)
    store.observe({**value, 'sequence': 2, 'state': 'completed', 'phase': 'ended'},
                  principal_id='native-host', contact_id='owner')
    unrelated = observation('foreground', contact_id='owner')
    store.observe(unrelated, principal_id='native-host', contact_id='owner')
    store.observe({**unrelated, 'sequence': 2, 'state': 'completed', 'phase': 'ended'},
                  principal_id='native-host', contact_id='owner')
    view = store.view(contact_id='owner', owner=True, session_id='later-conversation')
    assert not view['items']
    assert len(view['recent']) == 1
    assert view['recent'][0]['task_id'] == 'a' * 64
    assert view['recent'][0]['state'] == 'completed'
    assert view['recent'][0]['liveness'] == 'terminal_observation'
    rendered = request_work_context(view)['text']
    assert '"task_id": "' + 'a' * 64 + '"' in rendered
    assert 'completion' in rendered.lower()
    assert 'result' not in view['recent'][0]  # No invented artifact result.
    assert not store.view(contact_id='stranger', session_id='elsewhere')['recent']


def test_erased_terminal_task_is_not_offered_as_an_actionable_current_task(store):
    value = task(store)
    store.observe({**value, 'sequence': 2, 'state': 'completed', 'phase': 'ended'},
                  principal_id='native-host', contact_id='owner')
    assert store.view(contact_id='owner', owner=True)['recent']
    store.ledger.erase_sources(contact_id='owner', turn_ids=['coding-input'])
    view = store.view(contact_id='owner', owner=True, session_id='later-conversation')
    assert view['recent'] == []
    assert 'a' * 64 not in request_work_context(view)['text']


def test_task_handles_do_not_cross_contact_source_scope(store):
    task(store, person='guest')
    view = store.view(contact_id='owner', owner=True, session_id='observer')
    assert 'task_id' not in view['items'][0]
    assert 'a' * 64 not in request_work_context(view)['text']


def test_active_attempt_does_not_advertise_its_prior_terminal_as_current_result(store):
    value = task(store)
    store.observe({**value, 'sequence': 2, 'state': 'interrupted', 'phase': 'ended'},
                  principal_id='native-host', contact_id='owner')
    task(store, name='resumed')
    view = store.view(contact_id='owner', owner=True)
    assert view['items'][0]['task_id'] == 'a' * 64
    assert view['recent'] == []


def test_present_building_question_uses_current_work_without_dropping_history():
    from protagine.memory.selection import current_work_query
    assert current_work_query('What are you building for me right now, and what changed with my latest correction? Keep it short.')
    assert current_work_query('What are you developing currently?')
    assert not current_work_query('What are you building right now compared with last week?')
    assert not current_work_query('What are you building right now? Show me the instructions from earlier.')


def test_small_work_budget_keeps_source_locator_actionable_without_quotation(store):
    task(store, text=(
        'Build a run summary utility. Include each run\'s input, processor, '
        'elapsed time, observed output, verification result and unresolved '
        'conditions. Keep reported outcomes distinct from independently '
        'checked results. Preserve the source references so another session '
        'can inspect the original request before continuing the work.'))
    view = store.view(contact_id='owner', owner=True, session_id='observer')
    # Fit the single execution and source handle while withholding its longer
    # optional quotation. A locator still needs the ordinary freshness check.
    result = request_work_context(view, max_chars=1800)
    rows = [json.loads(line) for line in result['text'].splitlines() if line.startswith('{')]
    row = next(row for row in rows if row.get('source') == 'execution')
    assert row['input_source'] == view['items'][0]['request_input']['_provenance']['source_refs'][0]
    assert 'request_input' not in row
    assert row['input_source'] in result['input_provenance']['source_refs']
    assert result['input_provenance']['unannotated_input_refs']
    assert len(result['text']) <= 1800
