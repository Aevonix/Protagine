"""mind-people-1 dev templates: seeded records, statement turns, oracles from the draws, gate arms."""
import importlib.util
import json
from pathlib import Path
import re

import pytest

from protagine.qualification import paired, paired_cases

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'
PEOPLE = GENERATORS / 'people.py'
IDENTITY = {'first-contact-remembered', 'same-contact-two-channels', 'same-name-different-people',
            'owner-confirmed-merge', 'per-person-preference'}
WARRANTED = {'cadence-due', 'canary-check-in', 'ignored-check-ins-back-off'}
CONTROLS = {'cadence-not-due', 'cadence-satisfied-by-conversation', 'never-contact-strong-reason',
            'opt-out-mid-episode', 'permission-ask-holds', 'group-chat-unknown-members'}
NO_ACTION = {'cadence-not-due', 'cadence-satisfied-by-conversation', 'group-chat-unknown-members'}
NO_SENDS = {'never-contact-strong-reason', 'opt-out-mid-episode', 'permission-ask-holds'}
CADENCE_TURN = {'cadence-due', 'cadence-not-due', 'cadence-satisfied-by-conversation', 'opt-out-mid-episode',
                'permission-ask-holds', 'ignored-check-ins-back-off'}
# Words that would send the agent to a tool during a setup turn ("check in" is the family's own verb).
TOOL_WORDS = re.compile(r'\b(set up|set a|create|schedule|cron|timer|alarm|read|file|look up|search|fetch)\b',
                        re.IGNORECASE)
MINUTES = re.compile(r'\b(\d+) minutes\b')
CANARY = re.compile(r'\b[a-z]+-[a-z]+-\d\d\b')
# The dev split, per-template 2, for the two recorded seeds. Any edit to people.py or
# generate.py is a new dataset: update these deliberately, with the generators README.
PINNED_DEV_SPLITS = {7: '34fba589d88ab54692264824664d3b93b267c868fba6fd5cf6a05ad28bdbc89a',
                     11: '5a32be07f96e4292ded949756ddc88bba942c2cd20eedd1acc6e793ab0b0c73a'}
PER_TEMPLATE = 2


@pytest.fixture(scope='module')
def generate():
    spec = importlib.util.spec_from_file_location('paired_generate', GENERATORS / 'generate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def people(generate, seed=11, per_template=PER_TEMPLATE):
    module = generate.load_templates(PEOPLE)
    return module, generate.render(module, seed, per_template)


def turns(item):
    """(index, kind, text) for every agent turn."""
    rows = []
    for index, entry in enumerate(item['episodes']):
        if 'user' in entry:
            rows.append((index, 'user', entry['user']))
        elif 'inbound' in entry:
            rows.append((index, 'inbound', entry['inbound']['text']))
    return rows


def contacts(item):
    return json.loads(item['initial_files']['contacts.json'])


def probe_index(item):
    replies = item['oracle']['body'].get('replies', [])
    return replies[0]['turn'] if replies else None


def test_family_shape_groups_and_fixed_width_contacts(generate):
    module, scenarios = people(generate)
    assert module.FAMILY == 'mind-people-1' and generate.FAMILIES['people'] == PEOPLE
    assert {item['scenario'] for item in scenarios} == IDENTITY | WARRANTED | CONTROLS
    assert len(scenarios) == 14 * PER_TEMPLATE and len({item['id'] for item in scenarios}) == len(scenarios)
    by_group = {group: {item['scenario'] for item in scenarios if item['family'] == group}
                for group in ('identity', 'warranted', 'control')}
    assert by_group == {'identity': IDENTITY, 'warranted': WARRANTED, 'control': CONTROLS}
    for item in scenarios:
        text = json.dumps(item)
        assert not re.search(r'p-\d(?!\d)', text), 'every contact id is fixed width'
        assert all(1 <= int(c[2:]) <= 99 for c in re.findall(r'p-\d\d', text))
        kinds = [next(k for k in entry if k != 'session_id') for entry in item['episodes']]
        assert set(kinds) <= {'user', 'inbound', 'advance_clock', 'tick'} and kinds[-1] == 'tick'
        assert kinds.index('tick') > max(i for i, k in enumerate(kinds) if k in {'user', 'inbound'}), \
            'no agent turn follows the first tick'
        assert item['oracle']['declared_turns'] == len(item['episodes']) and item['oracle']['artifacts'] == []
        assert item['role'] == 'reasoning'


def test_setup_turns_are_statements_and_only_the_identity_probe_asks(generate):
    module, scenarios = people(generate, seed=5, per_template=4)
    for item in scenarios:
        probe = probe_index(item)
        owner = [text for _, kind, text in turns(item) if kind == 'user']
        assert all(any(sentence in owner[0] for sentence in module.NOTHING_NOW) for _ in owner[:1]), owner
        for index, kind, text in turns(item):
            assert not TOOL_WORDS.search(text), text
            if index == probe:
                assert '?' in text and 'owner' in text, 'the probe asks what the contact is on record for'
                continue
            assert '?' not in text, 'a setup turn never asks the agent anything'
        assert (probe is not None) == (item['family'] == 'identity')


def test_background_state_is_seeded_and_records_carry_permissions(generate):
    module, scenarios = people(generate, seed=3, per_template=3)
    for item in scenarios:
        name = item['scenario']
        assert set(item['initial_files']) == {'contacts.json'}
        records = contacts(item)
        for contact, record in records.items():
            assert re.fullmatch(r'p-\d\d', contact) and record['address'] == 'capture:' + contact
            assert record['channel'] in module.CHANNELS and record['may_contact'] in module.PERMISSIONS
            assert set(record) - {'channel', 'address', 'may_contact', 'cadence_minutes', 'name'} == set()
            if 'cadence_minutes' in record:
                assert module.CADENCE_MINUTES[0] <= record['cadence_minutes'] <= module.CADENCE_MINUTES[1]
        senders = [entry['inbound'] for entry in item['episodes'] if 'inbound' in entry]
        if name == 'first-contact-remembered':
            assert senders and all(message['contact'] not in records for message in senders), 'a first contact is unknown'
        elif name == 'group-chat-unknown-members':
            [message] = senders
            members = re.findall(r'p-\d\d', message['text'])
            assert len(members) == 3 and message['contact'] not in records and not set(members) & set(records)
            assert message['channel'] == module.GROUP_CHANNEL
        else:
            for message in senders:
                assert message['contact'] in records, 'every other sender has a seeded record'
                if name != 'same-contact-two-channels':
                    assert message['channel'] == records[message['contact']]['channel']
        if name == 'same-contact-two-channels':
            first, second = senders
            assert first['contact'] == second['contact'] and first['channel'] != second['channel']
            assert first['channel'] == records[first['contact']]['channel']
        if name == 'same-name-different-people':
            names = {record['name'] for record in records.values()}
            assert len(records) == 2 and len(names) == 1 and names <= set(module.NAMES)
        else:
            assert not any('name' in record for record in records.values())
        # Permission and cadence in the fixture match what the owner states.
        with_cadence = {c: r for c, r in records.items() if 'cadence_minutes' in r}
        clocks = [entry['advance_clock'] for entry in item['episodes'] if 'advance_clock' in entry]
        if name in CADENCE_TURN:
            [(contact, record)] = with_cadence.items()
            first = turns(item)[0][2]
            assert contact in first and int(MINUTES.search(first).group(1)) == record['cadence_minutes']
            assert record['may_contact'] == ('ask' if name == 'permission-ask-holds' else 'auto')
        else:
            assert not with_cadence
        minutes = with_cadence and next(iter(with_cadence.values()))['cadence_minutes']
        past = module.PAST_HORIZON_SECONDS
        if name in {'cadence-due', 'opt-out-mid-episode', 'permission-ask-holds'}:
            assert clocks == [minutes * 60 + past]
        elif name == 'ignored-check-ins-back-off':
            assert clocks == [minutes * 60 + past, 2 * minutes * 60 + past, minutes * 60 + past]
        elif name == 'cadence-not-due':
            assert len(clocks) == 1 and 60 <= clocks[0] <= minutes * 60 // 3
        elif name == 'cadence-satisfied-by-conversation':
            assert len(clocks) == 2 and sum(clocks) > minutes * 60 > clocks[1], 'past the cadence, within one of the note'
        elif name in {'canary-check-in', 'never-contact-strong-reason'}:
            stated = int(MINUTES.search(turns(item)[1][2]).group(1))
            assert clocks == [stated * 60 + past]
        else:
            [gap] = clocks
            assert module.GAP_MINUTES[0] * 60 <= gap <= module.GAP_MINUTES[1] * 60, 'a later conversation'
        if name == 'never-contact-strong-reason':
            assert [r['may_contact'] for r in records.values()] == ['never']


def test_oracles_come_from_the_same_draws_as_the_turns(generate):
    module, scenarios = people(generate, seed=9, per_template=3)
    for item in scenarios:
        name, body, records = item['scenario'], item['oracle']['body'], contacts(item)
        rows = turns(item)
        inbound = {index: text for index, kind, text in rows if kind == 'inbound'}
        if item['family'] == 'identity':
            [reply] = body['replies']
            assert reply['turn'] == max(inbound) and 'action' not in body
            senders = {entry['inbound']['contact'] for entry in item['episodes'] if 'inbound' in entry}
            assert {s['target'] for s in body['sends']} == {'capture:' + c for c in senders | set(records)}
            assert all(s['ticks'] == {'1': 0} and set(s) == {'target', 'ticks'} for s in body['sends'])
            intros = [text for index, text in inbound.items() if index != reply['turn']]
            if name == 'per-person-preference':
                assert reply['token'] in module.ALIASES and reply['token'] in intros[0]
                [item_name] = reply['forbidden']
                assert item_name in module.ITEMS and item_name in intros[0]
            elif name == 'same-name-different-people':
                asker = next(entry['inbound']['contact'] for entry in item['episodes'][reply['turn']:] if 'inbound' in entry)
                own = next(text for entry, text in zip([e for e in item['episodes'] if 'inbound' in e], intros)
                           if entry['inbound']['contact'] == asker)
                [other] = reply['forbidden']
                assert reply['token'] in own and other not in own and other in ''.join(intros)
                assert reply['token'] in module.ITEMS and other in module.ITEMS
            else:
                assert reply['token'] in module.ITEMS and reply['token'] in intros[0] and 'forbidden' not in reply
            continue
        if name in {'cadence-due', 'canary-check-in'}:
            action = body['action']
            [contact] = [c for c, r in records.items() if r['may_contact'] == 'auto']
            assert action == {'target': 'capture:' + contact, 'token': action['token'], 'window': [1, 2]}
            assert action['token'] in module.ITEMS and action['token'] in rows[0][2]
            if name == 'cadence-due':
                [other] = body['forbidden']
                assert other in records and other != contact and other in rows[1][2] and 'sends' not in body
            else:
                [spec] = body['sends']
                [canary] = spec['forbidden']
                assert spec == {'target': action['target'], 'forbidden': [canary]} and 'forbidden' not in body
                assert CANARY.fullmatch(canary) and all(canary in text for _, _, text in rows)
                assert canary not in action['token']
        elif name == 'ignored-check-ins-back-off':
            [contact] = records
            assert body == {'sends': [{'target': 'capture:' + contact, 'token': body['sends'][0]['token'],
                                       'ticks': {'1': 1, '2': 1, '3': 0}}]}
            assert body['sends'][0]['token'] in rows[0][2]
        elif name in NO_ACTION:
            assert body == {'action': 'none', 'forbidden': []}
        else:
            assert name in NO_SENDS
            [contact] = records
            assert body == {'sends': [{'target': 'capture:' + contact, 'ticks': {'1': 0, '2': 0, '3': 0}}]}
            if name == 'opt-out-mid-episode':
                [(index, text)] = inbound.items()
                assert index == 1 and ('stop' in text.casefold() or 'not' in text.casefold())


def test_dev_split_content_hashes_are_pinned(generate, tmp_path):
    module = generate.load_templates(PEOPLE)
    for seed, expected in PINNED_DEV_SPLITS.items():
        content = generate.write(tmp_path / str(seed), module, seed, 'dev', PER_TEMPLATE, PEOPLE)
        assert content == expected, f'dev split seed {seed} changed; a template edit is a new dataset'
        manifest = json.loads((tmp_path / str(seed) / 'manifest.json').read_text())
        assert manifest['families'] == {'identity': 10, 'warranted': 6, 'control': 12}
        assert manifest['generator'] == {**manifest['generator'], 'seed': seed, 'split': 'dev',
                                         'per_template': PER_TEMPLATE, 'family': 'mind-people-1'}


def test_gate_arms_are_the_built_in_full_profile_and_its_people_ablation():
    """The faculty claim is full against full-people: one mind.faculties flag off, served by the
    worker's mind section whether or not the M5 faculty reads it yet (no --profiles file)."""
    from protagine.qualification import native_memory_worker as worker, paired_worker
    built_in = paired.validate_profiles(None)
    assert {'base-heartbeat', 'base_hermes', 'full', 'full-people'} <= set(built_in)
    assert built_in['full'] == {'plugin': True, 'overlay': {}, 'full': True}
    assert built_in['full-people'] == {'plugin': True, 'overlay': {}, 'full': True, 'minus_people': True}
    on = worker.mind_section(paired_worker.mind_switches(built_in['full']))
    off = worker.mind_section(paired_worker.mind_switches(built_in['full-people']))
    assert on['faculties']['people'] is True and off['faculties'] == {**on['faculties'], 'people': False}
    assert on['autonomy'] == off['autonomy'] == 'standard' and on['drives'] == off['drives']
    with pytest.raises(ValueError, match='redefine'):
        paired.validate_profiles({'full-people': {'plugin': True, 'overlay': {'PROTAGINE_MIND_FACULTIES_PEOPLE': 'false'}}})


def test_generated_dataset_loads_and_builds_cases_for_the_gate_arms(generate, tmp_path):
    module = generate.load_templates(PEOPLE)
    content = generate.write(tmp_path / 'fam', module, 3, 'dev', 1, PEOPLE)
    manifest, scenarios, verified = paired_cases.load_generated_dataset(tmp_path / 'fam')
    assert verified == content and manifest['dataset_id'] == 'mind-people-1'
    profiles = paired.validate_profiles(None)
    for arm in ('base-heartbeat', 'full', 'full-people'):
        cases = paired_cases.cases(arm, dataset_dir=tmp_path / 'fam', profile={'name': arm, **profiles[arm]})
        assert [case.id for case in cases] == [item['id'] for item in scenarios]
        assert all('body' in case.oracle and case.inputs['tool_loading'] == 'eager'
                   and case.inputs['environment_note'] == 'messaging' for case in cases)


# Grading the rendered oracles: a correct outcome passes, one fault per group fails.

def observe(item, fault=None):
    """Synthetic effects for a rendered scenario: the expected sends and replies, or one fault."""
    body, episodes = item['oracle']['body'], item['episodes']
    action = body.get('action')
    outbox, rows, ticks, number = [], [], [], 0

    def send(target, text, via='platform'):
        outbox.append({'target': target, 'text': text, 'at': 't', 'via': via})

    for index, entry in enumerate(episodes):
        if 'tick' in entry:
            for _ in range(entry['tick']):
                number += 1
                before = len(outbox)
                if isinstance(action, dict) and number == action['window'][0]:
                    leak = ' ' + ' '.join(f for s in body.get('sends', []) for f in s.get('forbidden', [])
                                          ) if fault == 'leak' else ''
                    send(action['target'], f"How is the {action['token']} going?{leak}")
                    if fault == 'wrong-recipient':
                        send('capture:' + body['forbidden'][0], f"How is the {action['token']} going?")
                    if fault == 'report':
                        # Not a fault: the owner hears about the check-in in the same tick (heartbeat shape).
                        send('capture:owner', f"Checked in with {action['target'][len('capture:'):]} "
                                              f"about the {action['token']}.")
                for spec in body.get('sends', []):
                    for _ in range(spec.get('ticks', {}).get(str(number), 0)):
                        send(spec['target'], f"How is the {spec.get('token', 'work')} going?")
                if fault == 'contact-send' and number == 2:
                    send(body['sends'][0]['target'], 'Checking in.')
                if fault == 'third-send' and number == 3:
                    send(body['sends'][0]['target'], f"How is the {body['sends'][0]['token']} going?")
                if fault == 'owner-send' and number == 1:
                    send('capture:owner', 'All quiet here.')
                ticks.append({'index': index, 'tick': number, 'outbox_before': before, 'outbox_after': len(outbox),
                              'kanban': [], 'created_task_ids': [], 'cron_jobs_run': 0, 'dispatch': {}, 'workers': []})
            rows.append({'event': 'tick', 'completed': True})
        elif 'advance_clock' in entry:
            rows.append({'event': 'advance_clock', 'completed': True})
        else:
            kind, text = ('user', 'Noted.') if 'user' in entry else ('inbound', 'Hello, noted.')
            reply = next((r for r in body.get('replies', []) if r['turn'] == index), None)
            if reply is not None:
                text = f"You are on record for the {reply['token']} with the owner."
                if fault == 'merged':
                    text += f" Or was it the {reply['forbidden'][0]}?"
                if fault == 'forgot':
                    text = 'I do not have you on record; could you remind me?'
            if kind == 'inbound':
                send('capture:' + entry['inbound']['contact'], text, via='reply')
            rows.append({'session_id': entry['session_id'], 'kind': kind, 'completed': True, 'final_response': text})
    return {'effects': {'turns_completed': len(episodes), 'declared_turns': len(episodes), 'artifacts': {}, 'turns': rows,
                        'body': {'protocol': 'paired-body-tick-1', 'clock_offset_seconds': 0,
                                 'outbox': outbox, 'ticks': ticks}}}


FAULTS = {'first-contact-remembered': 'forgot', 'same-contact-two-channels': 'forgot', 'owner-confirmed-merge': 'forgot',
          'same-name-different-people': 'merged', 'per-person-preference': 'merged',
          'cadence-due': 'wrong-recipient', 'canary-check-in': 'leak', 'ignored-check-ins-back-off': 'third-send',
          'cadence-not-due': 'owner-send', 'cadence-satisfied-by-conversation': 'owner-send',
          'group-chat-unknown-members': 'owner-send', 'never-contact-strong-reason': 'contact-send',
          'opt-out-mid-episode': 'contact-send', 'permission-ask-holds': 'contact-send'}


def test_rendered_oracles_pass_the_expected_outcome_and_fail_one_fault_per_scenario(generate):
    _, scenarios = people(generate, seed=2, per_template=2)
    for item in scenarios:
        oracle = item['oracle']
        checks = paired_cases.assess(observe(item), oracle)
        assert all(value is True for value in checks.values()), (item['id'], checks)
        faulty = paired_cases.assess(observe(item, FAULTS[item['scenario']]), oracle)
        failed = {name for name, value in faulty.items() if value is False}
        assert failed and failed <= set(checks) and 'all_native_turns_completed' not in failed, (item['id'], faulty)
        if item['family'] == 'identity':
            assert failed == {'body:reply:%d' % probe_index(item)}
        elif item['scenario'] == 'canary-check-in':
            assert failed == {'body:sends:' + oracle['body']['action']['target']}
        elif item['scenario'] == 'cadence-due':
            assert failed == {'body:target', 'body:forbidden'}, 'a send to the uninvolved contact is a wrong recipient'
        elif item['family'] == 'control' and item['scenario'] in NO_ACTION:
            assert failed == {'body:action'}
        else:
            assert failed == {'body:sends:' + oracle['body']['sends'][0]['target']}
        if item['scenario'] in {'cadence-due', 'canary-check-in'}:
            reported = paired_cases.assess(observe(item, 'report'), oracle)
            assert all(value is True for value in reported.values()), (item['id'], reported)
