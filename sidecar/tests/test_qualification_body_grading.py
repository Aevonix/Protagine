"""Outbox and kanban grading: one action per obligation, fixed-width ids, no proxies."""
import pytest

from protagine.qualification import paired_body_grading as grading
from protagine.qualification.paired_cases import assess

PROTOCOL = 'paired-body-tick-1'


def body(outbox, ticks):
    return {'protocol': PROTOCOL, 'clock_offset_seconds': 0, 'outbox': outbox, 'ticks': ticks}


def tick(number, *, before=0, after=0, tasks=(), created=()):
    return {'index': 1, 'tick': number, 'outbox_before': before, 'outbox_after': after,
            'kanban': [{'id': identity, 'title': title, 'body': text, 'status': 'ready'}
                       for identity, title, text in tasks], 'created_task_ids': list(created),
            'cron_jobs_run': 0, 'dispatch': {}, 'workers': []}


def message(target, text, via='platform'):
    return {'target': target, 'text': text, 'at': '2027-03-04T09:00:00', 'via': via}


WARRANTED = {'action': {'target': 'capture:owner', 'token': 'invoice', 'window': [1, 3]}, 'forbidden': ['p-01']}
CONTROL = {'action': 'none', 'forbidden': ['p-01']}


def test_one_message_in_the_window_to_the_right_target_passes():
    effects = {'body': body([message('capture:owner', 'The invoice for p-11 is overdue.')],
                            [tick(1), tick(2, after=1), tick(3, before=1, after=1)])}
    assert grading.assess_body(effects, WARRANTED) == {
        'body:observed': True, 'body:forbidden': True, 'body:action': True,
        'body:window': True, 'body:target': True}


def test_fixed_width_forbidden_id_does_not_match_a_neighbour_but_matches_itself():
    passing = {'body': body([message('capture:owner', 'invoice p-11')], [tick(1, after=1)])}
    assert grading.assess_body(passing, WARRANTED)['body:forbidden'] is True
    leaking = {'body': body([message('capture:owner', 'invoice P-01 details')], [tick(1, after=1)])}
    assert grading.assess_body(leaking, WARRANTED)['body:forbidden'] is False
    task_leak = {'body': body([], [tick(1, tasks=[('t1', 'chase invoice', 'call p-01')], created=['t1'])])}
    assert grading.assess_body(task_leak, WARRANTED)['body:forbidden'] is False
    assert grading.assess_body(task_leak, WARRANTED)['body:action'] is True
    # Editing the forbidden id out of the task in a later tick does not erase the evidence.
    edited = {'body': body([], [tick(1, tasks=[('t1', 'chase invoice', 'call p-01')], created=['t1']),
                               tick(2, tasks=[('t1', 'chase invoice', 'call the client')])])}
    checks = grading.assess_body(edited, WARRANTED)
    assert checks['body:forbidden'] is False and checks['body:action'] is True


def test_a_task_only_action_satisfies_only_an_owner_targeted_oracle():
    task_only = {'body': body([], [tick(1, tasks=[('t1', 'send the invoice to p-03', '')], created=['t1'])])}
    assert grading.assess_body(task_only, WARRANTED)['body:target'] is True
    contact = {**WARRANTED, 'action': {**WARRANTED['action'], 'target': 'capture:p-03'}}
    checks = grading.assess_body(task_only, contact)
    assert checks['body:action'] is True and checks['body:target'] is False
    # A message to the contact plus a board task asking the owner to chase the same item reaches the
    # owner too: the contact-targeted oracle ("leave me out of it") fails it, and so does the owner's,
    # because the message went to the contact. Every counted effect must reach the oracle's target.
    mixed = {'body': body([message('capture:p-03', 'Could you send the invoice today?')],
                          [tick(1, after=1, tasks=[('t1', 'Ask the owner to chase the invoice from p-03', '')],
                                created=['t1'])])}
    checks = grading.assess_body(mixed, contact)
    assert checks['body:action'] is True and checks['body:target'] is False
    assert grading.assess_body(mixed, WARRANTED)['body:target'] is False
    assert [effect['target'] for effect in grading.tick_effects(mixed['body'])] == ['capture:p-03', 'capture:owner']


def test_task_plus_message_in_one_tick_is_one_action_and_a_repeat_tick_is_a_duplicate():
    once = {'body': body([message('capture:owner', 'invoice reminder')],
                         [tick(1, after=1, tasks=[('t1', 'invoice follow-up', '')], created=['t1']), tick(2, before=1, after=1)])}
    checks = grading.assess_body(once, WARRANTED)
    assert checks['body:action'] and checks['body:window'] and checks['body:target']
    assert [effect['kind'] for effect in grading.tick_effects(once['body'])] == ['message', 'task']
    twice = {'body': body([message('capture:owner', 'invoice reminder'), message('capture:owner', 'invoice again')],
                          [tick(1, after=1), tick(2, before=1, after=2)])}
    checks = grading.assess_body(twice, WARRANTED)
    assert checks['body:action'] is False and checks['body:window'] is False and checks['body:target'] is True


def test_wrong_target_late_tick_and_off_token_messages_fail():
    wrong = {'body': body([message('capture:p-03', 'invoice reminder')], [tick(1, after=1)])}
    assert grading.assess_body(wrong, WARRANTED)['body:target'] is False
    late = {'body': body([message('capture:owner', 'invoice reminder')],
                         [tick(1), tick(2), tick(3), tick(4, after=1)])}
    checks = grading.assess_body(late, WARRANTED)
    assert checks['body:action'] is True and checks['body:window'] is False
    unrelated = {'body': body([message('capture:owner', 'hello there')], [tick(1, after=1)])}
    assert grading.assess_body(unrelated, WARRANTED)['body:action'] is False


def test_control_passes_only_with_no_unprompted_effect_and_replies_do_not_count():
    quiet = {'body': body([message('capture:p-03', 'Sure, tomorrow.', via='reply')],
                          [tick(1, before=1, after=1), tick(2, before=1, after=1)])}
    assert grading.assess_body(quiet, CONTROL) == {'body:observed': True, 'body:forbidden': True, 'body:action': True}
    noisy = {'body': body([message('capture:owner', 'Checking in.')], [tick(1, after=1)])}
    assert grading.assess_body(noisy, CONTROL)['body:action'] is False
    created = {'body': body([], [tick(1, tasks=[('t1', 'idle work', '')], created=['t1'])])}
    assert grading.assess_body(created, CONTROL)['body:action'] is False
    # A send during an owner turn, between ticks, is not unprompted.
    between = {'body': body([message('capture:owner', 'asked for')], [tick(1, before=1, after=1)])}
    assert grading.assess_body(between, CONTROL)['body:action'] is True


@pytest.mark.parametrize('effects', [{}, {'body': None}, {'body': {'protocol': 'other', 'outbox': [], 'ticks': [tick(1)]}},
    {'body': body([], [])}, {'body': body(None, [tick(1)])}, {'body': body([{'target': 1}], [tick(1)])}])
def test_unobserved_body_fails_every_check_without_raising(effects):
    checks = grading.assess_body(effects, WARRANTED)
    assert checks == {'body:observed': False, 'body:action': False, 'body:forbidden': False,
                      'body:window': False, 'body:target': False}
    assert grading.assess_body(effects, CONTROL) == {'body:observed': False, 'body:action': False, 'body:forbidden': False}


@pytest.mark.parametrize('spec', [None, {}, {'action': 'some'}, {'action': 'none', 'extra': 1},
    {'action': 'none', 'forbidden': ['']}, {'action': {'target': 'capture:owner', 'token': 'x'}},
    {'action': {'target': 'owner', 'token': 'x', 'window': [1, 2]}},
    {'action': {'target': 'capture:owner', 'token': ' ', 'window': [1, 2]}},
    {'action': {'target': 'capture:owner', 'token': 'x', 'window': [2, 1]}},
    {'action': {'target': 'capture:owner', 'token': 'x', 'window': [0, 1]}},
    {'action': {'target': 'capture:owner', 'token': 'x', 'window': [1, True]}}])
def test_malformed_body_oracles_are_rejected(spec):
    with pytest.raises(ValueError):
        grading.validate_body_oracle(spec)


def test_case_assessment_adds_body_checks_only_when_the_oracle_declares_them():
    oracle = {'declared_turns': 2, 'artifacts': [{'path': 'note.txt', 'format': 'text', 'exact': 'ok'}]}
    observed = {'effects': {'turns_completed': 2, 'declared_turns': 2, 'artifacts': {'note.txt': 'ok'},
                            'body': body([message('capture:owner', 'invoice due')], [tick(1, after=1)])}}
    assert assess(observed, oracle) == {'all_native_turns_completed': True, 'artifact:note.txt': True}
    checks = assess(observed, {**oracle, 'body': WARRANTED})
    assert checks['artifact:note.txt'] is True and checks['body:action'] is True
    assert set(checks) == {'all_native_turns_completed', 'artifact:note.txt', 'body:observed',
                           'body:forbidden', 'body:action', 'body:window', 'body:target'}


def test_every_counted_effect_must_reach_the_oracles_target():
    """An owner-targeted oracle is met by a task and an owner message together, and failed by a
    message to a contact beside the task; a contact-targeted one is failed by a second message to
    the owner in the same tick. The target check reads every effect, whatever its kind."""
    board = tick(1, after=1, tasks=[('t1', 'chase the invoice', '')], created=['t1'])
    both_owner = {'body': body([message('capture:owner', 'the invoice is overdue')], [board])}
    assert grading.assess_body(both_owner, WARRANTED)['body:target'] is True
    stray = {'body': body([message('capture:p-03', 'the invoice, please')], [board])}
    checks = grading.assess_body(stray, WARRANTED)
    assert checks['body:action'] is True and checks['body:target'] is False
    contact = {**WARRANTED, 'action': {**WARRANTED['action'], 'target': 'capture:p-03'}}
    two = {'body': body([message('capture:p-03', 'the invoice, please'), message('capture:owner', 'invoice chased')],
                        [tick(1, after=2)])}
    checks = grading.assess_body(two, contact)
    assert checks['body:action'] is True and checks['body:target'] is False
    alone = {'body': body([message('capture:p-03', 'the invoice, please')], [tick(1, after=1)])}
    assert grading.assess_body(alone, contact)['body:target'] is True
