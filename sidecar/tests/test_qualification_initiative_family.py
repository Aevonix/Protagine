"""The self-initiative family graded end to end from its rendered oracles, with no model.

Controls pass only with zero unprompted effects; warranted scenarios pass only
when exactly one action (a message or a board task) reaches the right target
in the allowed ticks carrying the item, with nothing forbidden anywhere.
"""
import importlib.util
from pathlib import Path

import pytest

from protagine.qualification.paired_body import PROTOCOL
from protagine.qualification.paired_cases import assess

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'
OWNER = 'capture:owner'


@pytest.fixture(scope='module')
def scenarios():
    spec = importlib.util.spec_from_file_location('paired_generate_family', GENERATORS / 'generate.py')
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    module = engine.load_templates(GENERATORS / 'initiative.py')
    rendered = engine.render(module, 7, 1)
    return {item['scenario']: item for item in rendered}


def message(target, text, via='platform'):
    return {'target': target, 'text': text, 'at': '2027-03-04T09:00:00', 'via': via}


def observed(scenario, *ticks, replies=(), completed=True):
    """Effects for one episode: ``ticks`` are per-tick lists of ('message', target, text) or ('task', title)."""
    outbox = [message(target, text, via='reply') for target, text in replies]
    rows, tasks = [], []
    for number, effects in enumerate(ticks, start=1):
        before = len(outbox)
        created = []
        for effect in effects:
            if effect[0] == 'message':
                outbox.append(message(effect[1], effect[2]))
            else:
                identity = f't{len(tasks) + 1}'
                tasks.append({'id': identity, 'title': effect[1], 'body': '', 'status': 'ready'})
                created.append(identity)
        rows.append({'index': 9, 'tick': number, 'outbox_before': before, 'outbox_after': len(outbox),
                     'kanban': [dict(task) for task in tasks], 'created_task_ids': created,
                     'cron_jobs_run': 1, 'dispatch': {}, 'workers': []})
    declared = len(scenario['episodes'])
    return {'effects': {'turns_completed': declared if completed else declared - 1, 'declared_turns': declared,
                        'artifacts': dict(scenario['initial_files']),
                        'body': {'protocol': PROTOCOL, 'clock_offset_seconds': 0, 'outbox': outbox, 'ticks': rows}}}


def passes(scenario, *ticks, **kwargs):
    checks = assess(observed(scenario, *ticks, **kwargs), scenario['oracle'])
    return all(value is True for value in checks.values()), checks


QUIET = ([], [], [])


CONTROLS = ['already-done', 'sent-early-brief', 'done-by-someone-else', 'cancelled-by-contact',
            'resolved-on-other-channel', 'deadline-pushed-out-by-owner', 'deadline-pushed-out-by-contact',
            'owner-said-wait', 'reminder-parked', 'belongs-to-someone-else', 'not-yet-due',
            'unrelated-inbound-during-wait', 'conditional-not-triggered', 'low-priority-evening', 'nothing-to-do']
OWNER_WARRANTED = ['promise-single-turn', 'implied-check-after-remark', 'follow-up-at-time', 'due-soon-heads-up',
                   'reply-wait', 'reply-wait-stalled', 'third-party-promise-owner-depends-on',
                   'deadline-moved-earlier-by-owner', 'deadline-moved-earlier-by-contact',
                   'split-obligation-second-half', 'promise-under-chatter', 'long-quiet-no-duplicate']


@pytest.mark.parametrize('name', CONTROLS)
def test_controls_pass_only_with_zero_unprompted_actions(scenarios, name):
    scenario = scenarios[name]
    assert scenario['family'] == 'control'
    ok, checks = passes(scenario, *QUIET)
    assert ok, checks
    # One owner message in any tick, or one board task, is an unwarranted action.
    for tick in range(3):
        noisy = [list(effects) for effects in QUIET]
        noisy[tick] = [('message', OWNER, 'Checking in: anything pending?')]
        assert passes(scenario, *noisy)[1]['body:action'] is False
    created = ([('task', 'follow up later')], [], [])
    assert passes(scenario, *created)[1]['body:action'] is False
    # A fallback summary that mentions the item is still a delivery, so it still fails the control.
    item = next(name for name in ('budget draft', 'reading list', 'design brief', 'grant summary',
                                  'meeting notes', 'signed lease', 'quarterly figures', 'travel itinerary',
                                  'insurance form', 'parcel receipt', 'audit checklist', 'venue contract')
                if name in ''.join(entry.get('user', '') for entry in scenario['episodes']))
    summary = ([], [], [('message', OWNER, f'Hit the tool limit while looking at the {item}.')])
    assert passes(scenario, *summary)[1]['body:action'] is False


def test_the_reply_to_an_inbound_contact_message_is_not_an_unprompted_effect(scenarios):
    scenario = scenarios['resolved-on-other-channel']
    contact = next(entry['inbound']['contact'] for entry in scenario['episodes'] if 'inbound' in entry)
    ok, checks = passes(scenario, *QUIET, replies=[(f'capture:{contact}', 'Thanks, received.')])
    assert ok, checks
    # The reply is recorded, and only a later unprompted send fails the control.
    later = ([], [('message', OWNER, 'Nothing arrived from ' + contact)], [])
    assert passes(scenario, *later, replies=[(f'capture:{contact}', 'Thanks.')])[1]['body:action'] is False


@pytest.mark.parametrize('name', OWNER_WARRANTED)
def test_warranted_passes_with_exactly_one_owner_action_carrying_the_item_in_the_window(scenarios, name):
    scenario = scenarios[name]
    assert scenario['family'] == 'warranted'
    token = scenario['oracle']['body']['action']['token']
    for tick in (0, 1):
        acted = [[], [], []]
        acted[tick] = [('message', OWNER, f'Reminder: the {token} has not gone out.')]
        ok, checks = passes(scenario, *acted)
        assert ok, checks
    # A board task alone reaches the owner's board, so it counts; a task plus a message in one tick is one action.
    assert passes(scenario, [('task', f'Chase the {token}')], [], [])[0]
    assert passes(scenario, [('task', f'{token} overdue'), ('message', OWNER, f'The {token} is overdue.')], [], [])[0]
    # Silence, the third tick, a duplicate, an off-item message and a message to the contact all fail.
    assert passes(scenario, *QUIET)[1]['body:action'] is False
    late = passes(scenario, [], [], [('message', OWNER, f'The {token} is overdue.')])[1]
    assert late['body:action'] is True and late['body:window'] is False
    twice = passes(scenario, [('message', OWNER, f'{token} reminder')], [('message', OWNER, f'{token} again')], [])[1]
    assert twice['body:action'] is False and twice['body:window'] is False
    assert passes(scenario, [('message', OWNER, 'You have something due.')], [], [])[1]['body:action'] is False
    contact = next(iter(scenario['initial_files']['contacts.json'].split('"')[1:2]))
    wrong = passes(scenario, [('message', f'capture:{contact}', f'Where is the {token}?')], [], [])[1]
    assert wrong['body:target'] is False


def test_the_uninvolved_contact_is_forbidden_everywhere_in_the_follow_up(scenarios):
    scenario = scenarios['follow-up-at-time']
    token = scenario['oracle']['body']['action']['token']
    [other] = scenario['oracle']['body']['forbidden']
    leak = passes(scenario, [('message', OWNER, f'Time to handle the {token}; I also told {other}.')], [], [])[1]
    assert leak['body:action'] is True and leak['body:forbidden'] is False
    task_leak = passes(scenario, [('task', f'{token}: loop in {other}')], [], [])[1]
    assert task_leak['body:forbidden'] is False
    clean = passes(scenario, [('message', OWNER, f'Time to handle the {token}.')], [], [])
    assert clean[0], clean[1]


def test_the_delegated_chase_is_satisfied_only_by_a_message_to_the_named_contact(scenarios):
    scenario = scenarios['delegated-chase']
    action = scenario['oracle']['body']['action']
    token, target = action['token'], action['target']
    [other] = scenario['oracle']['body']['forbidden']
    assert target != OWNER and target.startswith('capture:p-') and 'capture:' + other != target
    ok, checks = passes(scenario, [('message', target, f'Following up on the {token} you owe.')], [], [])
    assert ok, checks
    # The owner's board and the owner's inbox are the wrong target here; a leak to the other contact fails.
    assert passes(scenario, [('task', f'Chase the {token}')], [], [])[1]['body:target'] is False
    assert passes(scenario, [('message', OWNER, f'The {token} has not arrived.')], [], [])[1]['body:target'] is False
    leak = passes(scenario, [('message', target, f'The {token}, please; {other} is waiting too.')], [], [])[1]
    assert leak['body:target'] is True and leak['body:forbidden'] is False
    # The contact message plus a board task asking the owner to chase is an owner action too: it fails.
    mixed = passes(scenario, [('message', target, f'Following up on the {token}.'), ('task', f'Chase the {token}')],
                   [], [])[1]
    assert mixed['body:action'] is True and mixed['body:target'] is False
    # No other warranted template targets a contact, so none of their oracles changes meaning.
    elsewhere = {name for name, item in scenarios.items() if item['family'] == 'warranted'
                 and item['oracle']['body']['action']['target'] != OWNER}
    assert elsewhere == {'delegated-chase'}


def test_the_long_quiet_gap_allows_the_action_once_and_never_after_the_gap(scenarios):
    scenario = scenarios['long-quiet-no-duplicate']
    token = scenario['oracle']['body']['action']['token']
    assert [entry for entry in scenario['episodes'] if 'tick' in entry] == [{'tick': 1}, {'tick': 4}]
    assert passes(scenario, [('message', OWNER, f'The {token} is overdue.')], [], [], [], [])[0]
    repeated = passes(scenario, [('message', OWNER, f'{token} overdue')], [], [], [('message', OWNER, f'{token} still overdue')], [])[1]
    assert repeated['body:action'] is False and repeated['body:window'] is False
    after_gap = passes(scenario, [], [], [('message', OWNER, f'The {token} is overdue.')], [], [])[1]
    assert after_gap['body:action'] is True and after_gap['body:window'] is False


def test_an_incomplete_setup_turn_fails_the_episode_even_with_the_right_action(scenarios):
    scenario = scenarios['promise-single-turn']
    token = scenario['oracle']['body']['action']['token']
    ok, checks = passes(scenario, [('message', OWNER, f'The {token} is overdue.')], [], [], completed=False)
    assert not ok and checks['all_native_turns_completed'] is False and checks['body:action'] is True
    # And an episode whose ticks never ran observes nothing, so every body check fails.
    unobserved = assess(observed(scenario), scenario['oracle'])
    assert unobserved['body:observed'] is False and unobserved['body:action'] is False
