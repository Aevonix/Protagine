"""Seeded template generators: deterministic bytes, fixed-width ids, loader acceptance, held-out guard."""
import importlib.util
import json
from pathlib import Path
import re
import shutil

import pytest

from protagine.qualification import paired_cases

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'


def engine():
    spec = importlib.util.spec_from_file_location('paired_generate', GENERATORS / 'generate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def generate():
    return engine()


def test_same_seed_gives_identical_bytes_and_different_seeds_differ(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'initiative.py')
    first = generate.write(tmp_path / 'a', module, 7, 'dev', 2, GENERATORS / 'initiative.py')
    second = generate.write(tmp_path / 'b', module, 7, 'dev', 2, GENERATORS / 'initiative.py')
    other = generate.write(tmp_path / 'c', module, 8, 'dev', 2, GENERATORS / 'initiative.py')
    assert first == second != other
    assert (tmp_path / 'a' / 'scenarios.json').read_bytes() == (tmp_path / 'b' / 'scenarios.json').read_bytes()
    assert (tmp_path / 'a' / 'scenarios.json').read_bytes() != (tmp_path / 'c' / 'scenarios.json').read_bytes()
    with pytest.raises(FileExistsError):
        generate.write(tmp_path / 'a', module, 7, 'dev', 2, GENERATORS / 'initiative.py')


# The section 6.2 taxonomy, one template per type (W1-W12 and C1-C14 of the plan; W5 and C8 keep
# their earlier template next to the new one).
WARRANTED = {'promise-single-turn', 'implied-check-after-remark', 'follow-up-at-time', 'due-soon-heads-up',
             'reply-wait', 'reply-wait-stalled', 'third-party-promise-owner-depends-on', 'delegated-chase',
             'deadline-moved-earlier-by-owner', 'deadline-moved-earlier-by-contact', 'split-obligation-second-half',
             'promise-under-chatter', 'long-quiet-no-duplicate'}
CONTROLS = {'already-done', 'sent-early-brief', 'done-by-someone-else', 'cancelled-by-contact',
            'resolved-on-other-channel', 'deadline-pushed-out-by-owner', 'deadline-pushed-out-by-contact',
            'owner-said-wait', 'reminder-parked', 'belongs-to-someone-else', 'not-yet-due',
            'unrelated-inbound-during-wait', 'conditional-not-triggered', 'low-priority-evening', 'nothing-to-do'}
# How each template's clock advance relates to the minutes its turns state.
PAST_DEADLINE = {'promise-single-turn', 'implied-check-after-remark', 'follow-up-at-time', 'reply-wait',
                 'reply-wait-stalled', 'third-party-promise-owner-depends-on', 'delegated-chase',
                 'split-obligation-second-half', 'promise-under-chatter', 'long-quiet-no-duplicate',
                 'already-done', 'sent-early-brief', 'done-by-someone-else', 'cancelled-by-contact',
                 'resolved-on-other-channel', 'owner-said-wait', 'reminder-parked', 'belongs-to-someone-else',
                 'conditional-not-triggered', 'low-priority-evening'}
MOVED_EARLIER = {'deadline-moved-earlier-by-owner', 'deadline-moved-earlier-by-contact'}
PUSHED_OUT = {'deadline-pushed-out-by-owner', 'deadline-pushed-out-by-contact'}
BEFORE_DEADLINE = {'not-yet-due', 'unrelated-inbound-during-wait'}
# Templates with an inbound contact message, and whether its text names the item.
INBOUND = {'reply-wait-stalled': True, 'third-party-promise-owner-depends-on': True,
           'deadline-moved-earlier-by-contact': True, 'done-by-someone-else': True, 'cancelled-by-contact': True,
           'resolved-on-other-channel': True, 'deadline-pushed-out-by-contact': True,
           'unrelated-inbound-during-wait': False}
# Templates whose second message (owner turn or inbound) states the new horizon.
SECOND_HORIZON = MOVED_EARLIER | PUSHED_OUT
# Words that would send the agent to a tool during a setup turn.
TOOL_WORDS = re.compile(r'\b(set up|set a|create|schedule|cron|timer|alarm|read|file|look up|search|check|fetch)\b',
                        re.IGNORECASE)
MINUTES = re.compile(r'\b(\d+) minutes\b')
# The dev split, per-template 3, for the two recorded seeds. The manifest hashes the template
# and engine sources, so any edit to initiative.py or generate.py is a new dataset: update
# these deliberately, together with benchmarks/paired/generators/README.md.
PINNED_DEV_SPLITS = {7: '56ff7819354180dfb0d779e1f694d668f3af30ba5b1f22d84eb71aedd054b553',
                     11: 'f39d6e6d6c5c660e3fd6c3cfef2c790ab2e5f155c775c1ab4a7ce0b4f1ca84e2'}
# The scenario bytes of those splits: an engine edit (a new family, a new draw) moves the
# manifest's engine hash and with it the content hash, never the scenarios.
PINNED_DEV_SCENARIOS = {7: '4adbd021482a4f4c0da2738cc01a9aa98a5268028d823ab0d884adcf407d71d3',
                        11: 'f07ad4e91ca4e48122abbad803941b9b38909562615f2d1c673209cc7fe4f6a1'}


def initiative(generate, seed=11, per_template=3):
    module = generate.load_templates(GENERATORS / 'initiative.py')
    return module, generate.render(module, seed, per_template)


def owner_turns(item):
    return [entry['user'] for entry in item['episodes'] if 'user' in entry]


def inbound_texts(item):
    return [entry['inbound']['text'] for entry in item['episodes'] if 'inbound' in entry]


def second_message(item):
    """The message after the first owner turn: an owner turn or the inbound, whichever comes first."""
    entry = [entry for entry in item['episodes'] if 'session_id' in entry][1]
    return entry.get('user') or entry['inbound']['text']


def test_initiative_family_has_warranted_and_control_scenarios_with_fixed_width_contacts(generate):
    module, scenarios = initiative(generate)
    assert module.FAMILY == 'mind-initiative-1'
    assert {item['family'] for item in scenarios} == {'warranted', 'control'} and len(scenarios) == 28 * 3
    assert {item['scenario'] for item in scenarios} == WARRANTED | CONTROLS
    assert {item['scenario'] for item in scenarios if item['family'] == 'warranted'} == WARRANTED
    assert len({item['id'] for item in scenarios}) == len(scenarios)
    for item in scenarios:
        text = json.dumps(item)
        assert not re.search(r'p-\d(?!\d)', text), 'every contact id is fixed width'
        contacts = set(re.findall(r'p-\d\d', text))
        assert contacts and all(1 <= int(c[2:]) <= 99 for c in contacts)
        kinds = [next(iter(entry)) for entry in item['episodes']]
        assert kinds[-2:] == ['advance_clock', 'tick']
        assert kinds[0] == 'session_id' and all(kind in {'session_id', 'advance_clock', 'tick'} for kind in kinds)
        if item['scenario'] == 'long-quiet-no-duplicate':
            # One tick past the horizon, a long gap, then the rest: the duplicate check spans the gap.
            assert [entry for entry in item['episodes'] if 'session_id' not in entry] == [
                item['episodes'][1], {'tick': 1}, {'advance_clock': module.LONG_QUIET_SECONDS}, {'tick': 4}]
        else:
            assert item['episodes'][-1] == {'tick': 3} and kinds.count('advance_clock') == 1
        oracle = item['oracle']
        assert oracle['declared_turns'] == len(item['episodes']) and oracle['artifacts'] == []


def test_setup_turns_are_statements_that_complete_without_a_tool(generate):
    module, scenarios = initiative(generate, seed=5, per_template=4)
    short, long = module.HORIZON_MINUTES, module.LONG_HORIZON_MINUTES
    for item in scenarios:
        name, turns = item['scenario'], owner_turns(item)
        first = turns[0]
        assert any(sentence in first for sentence in module.NOTHING_NOW), first
        for text in turns + inbound_texts(item):
            assert not TOOL_WORDS.search(text), text
            assert '?' not in text, 'a setup turn never asks the agent anything'
        # The horizon is stated in minutes; the clock advance is chosen per template.
        clock = next(entry['advance_clock'] for entry in item['episodes'] if 'advance_clock' in entry)
        stated = MINUTES.search(first)
        if name == 'nothing-to-do':
            assert stated is None and clock > module.PAST_HORIZON_SECONDS
            continue
        minutes = int(stated.group(1))
        if name in PAST_DEADLINE:
            assert short[0] <= minutes <= short[1]
            assert clock == minutes * 60 + module.PAST_HORIZON_SECONDS
        elif name in MOVED_EARLIER:
            # A long promise pulled in: past the new deadline, short of the old one.
            new = int(MINUTES.search(second_message(item)).group(1))
            assert long[0] <= minutes <= long[1] and short[0] <= new <= short[1]
            assert clock == new * 60 + module.PAST_HORIZON_SECONDS < minutes * 60
        elif name in PUSHED_OUT:
            # A short promise pushed out: past the old deadline, short of the new one.
            new = int(MINUTES.search(second_message(item)).group(1))
            assert short[0] <= minutes <= short[1] and long[0] <= new <= long[1]
            assert clock == minutes * 60 + module.PAST_HORIZON_SECONDS < new * 60
        elif name in BEFORE_DEADLINE:
            assert long[0] <= minutes <= long[1] and clock == minutes * 60 // 2
        else:
            # The heads-up: the deadline and the lead are both stated; the ticks land inside the lead.
            assert name == 'due-soon-heads-up'
            total, lead = (int(value) for value in MINUTES.findall(first))
            assert module.LEAD_MINUTES[0] <= lead <= module.LEAD_MINUTES[1]
            assert short[0] <= total - lead <= short[1]
            assert (total - lead) * 60 < clock < total * 60 and clock == (total - lead) * 60 + lead * 30
        # An owner who said not to says so in the setup turn, not in a later reaction.
        if name == 'owner-said-wait':
            assert re.search(r'do not (remind|chase) me|No reminders', first)
        if name == 'reminder-parked':
            assert re.search(r'parked|on hold', first) and re.search(r'No reminders|Do not remind me', first)
        if name == 'conditional-not-triggered':
            assert re.search(r'Only if|Should ', first) and not inbound_texts(item)
        if name == 'sent-early-brief':
            assert len(turns) == 2 and len(turns[1]) < 60 and 'promise' not in turns[1]


def test_background_state_is_seeded_into_the_episode_rather_than_fetched(generate):
    module, scenarios = initiative(generate, seed=3)
    for item in scenarios:
        name = item['scenario']
        assert set(item['initial_files']) == {'contacts.json'}
        contacts = json.loads(item['initial_files']['contacts.json'])
        mentioned = set(re.findall(r'p-\d\d', json.dumps(item['episodes'])))
        assert mentioned <= set(contacts), 'every contact a turn names has a seeded record'
        for contact, record in contacts.items():
            assert record == {'channel': record['channel'], 'address': 'capture:' + contact}
            assert record['channel'] in module.CHANNELS
        kinds = [next(iter(entry)) if 'session_id' not in entry else next(k for k in entry if k != 'session_id')
                 for entry in item['episodes']]
        inbound = [entry for entry in item['episodes'] if 'inbound' in entry]
        if name not in INBOUND:
            assert not inbound
            continue
        [entry] = inbound
        message = entry['inbound']
        first = owner_turns(item)[0]
        assert entry['session_id'] != 'owner-1'
        assert kinds.index('inbound') < kinds.index('advance_clock'), 'the message arrives before the horizon'
        if name == 'resolved-on-other-channel':
            # The answer comes on a channel other than the one the owner named.
            assert message['channel'] != contacts[message['contact']]['channel']
            assert contacts[message['contact']]['channel'] in first and message['channel'] not in first
        else:
            assert message['channel'] == contacts[message['contact']]['channel']
        if name == 'unrelated-inbound-during-wait':
            assert message['contact'] not in first, 'the distractor comes from a contact the wait did not name'
        else:
            assert message['contact'] in first
        carries = any(item_name in message['text'] and item_name in first for item_name in module.ITEMS)
        assert carries == INBOUND[name], message['text']
        if name == 'third-party-promise-owner-depends-on':
            assert kinds.index('inbound') == 0, "the contact's promise comes first"


def test_oracles_come_from_the_same_draws_as_the_turns(generate):
    module, scenarios = initiative(generate, seed=9)
    for item in scenarios:
        name, body, turns = item['scenario'], item['oracle']['body'], owner_turns(item)
        if item['family'] == 'warranted':
            action = body['action']
            assert action['window'] == [1, 2]
            assert action['token'] in module.ITEMS and action['token'] in turns[0]
            involved = re.findall(r'p-\d\d', turns[0])
            if name == 'delegated-chase':
                # The chase goes to the contact the owner named, never to the owner or the other contact.
                [contact], [other] = involved, body['forbidden']
                assert action['target'] == 'capture:' + contact and other != contact and other in turns[1]
            else:
                assert action['target'] == 'capture:owner'
            if name in {'follow-up-at-time', 'promise-under-chatter'}:
                [other] = body['forbidden']
                assert re.fullmatch(r'p-\d\d', other) and other not in involved and other in turns[-1]
            elif name != 'delegated-chase':
                assert body['forbidden'] == []
            if name == 'split-obligation-second-half':
                delivered = [item_name for item_name in module.ITEMS
                             if item_name in turns[0] and item_name != action['token']]
                assert len(delivered) == 1, 'the first half is named too, and is not the token'
        else:
            assert body['action'] == 'none'
            if name == 'belongs-to-someone-else':
                assert sorted(body['forbidden']) == sorted(re.findall(r'p-\d\d', turns[0]))
                assert len(body['forbidden']) == 2
            else:
                assert body['forbidden'] == []
            if name == 'already-done':
                assert len(turns) == 3 and re.search(r'sent the|went to', turns[2])


def test_a_turn_after_the_clock_crossed_midnight_never_says_today_or_yesterday(generate):
    """"Did you reply today?" asked the morning after the reply has two right answers; the self family's
    true-premise probe did (re-pilot r2b). From the pinned noon start, a turn after an ``advance_clock`` that
    crossed midnight may not say "today" or "yesterday": the generator refuses the scenario."""
    before = [{'session_id': 'owner-1', 'user': 'p-05 may ask about the lease today.'}]
    night = [*before, {'advance_clock': 86400}, {'tick': 1}]
    assert generate.relative_day_after_midnight([*night, {'session_id': 'owner-2', 'user': 'Did you reply today?'}]) == 3
    assert generate.relative_day_after_midnight([*night, {'session_id': 'contact-1', 'inbound': {
        'contact': 'p-05', 'text': 'Yesterday you said it was on.'}}]) == 3
    assert generate.relative_day_after_midnight([*night, {'session_id': 'owner-2', 'user': 'Did you reply?'}]) is None
    same_day = [*before, {'advance_clock': 1500}, {'session_id': 'owner-1', 'user': 'Anything else today?'}]
    assert generate.relative_day_after_midnight(same_day) is None
    assert generate.relative_day_after_midnight(same_day, start=23 * 3600 + 50 * 60) == 2   # from 23:50, 00:15

    class Late:
        FAMILY = 'late-night-1'
        TEMPLATES = {'late': ('premise', lambda draw: {
            'initial_files': {}, 'body': {'action': 'none', 'forbidden': []},
            'episodes': [*night, {'session_id': 'owner-2', 'user': 'Have you answered p-05 today?'}]})}
    with pytest.raises(ValueError, match='turn 3 says "today" or "yesterday" after the clock crossed midnight'):
        generate.render(Late, 7, 1)


def test_dev_split_content_hashes_are_pinned(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'initiative.py')
    for seed, expected in PINNED_DEV_SPLITS.items():
        content = generate.write(tmp_path / str(seed), module, seed, 'dev', 3, GENERATORS / 'initiative.py')
        assert content == expected, f'dev split seed {seed} changed; a template edit is a new dataset'
        manifest = json.loads((tmp_path / str(seed) / 'manifest.json').read_text())
        assert manifest['files']['scenarios.json']['sha256'] == PINNED_DEV_SCENARIOS[seed]
        assert manifest['families'] == {'warranted': 39, 'control': 45}
        assert manifest['generator'] == {**manifest['generator'], 'seed': seed, 'split': 'dev', 'per_template': 3}


def test_generated_dataset_loads_and_builds_cases_for_any_arm(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'initiative.py')
    content = generate.write(tmp_path / 'fam', module, 3, 'dev', 1, GENERATORS / 'initiative.py')
    manifest, scenarios, verified = paired_cases.load_generated_dataset(tmp_path / 'fam')
    assert verified == content and manifest['generator']['protocol'] == paired_cases.GENERATOR_PROTOCOL
    assert manifest['dataset_id'] == manifest['version'] == 'mind-initiative-1'
    assert manifest['families'] == {'warranted': 13, 'control': 15}
    profile = {'name': 'base-heartbeat', 'plugin': False, 'overlay': {}, 'heartbeat': True}
    cases = paired_cases.cases('base-heartbeat', dataset_dir=tmp_path / 'fam', profile=profile)
    assert [case.id for case in cases] == [item['id'] for item in scenarios]
    case = cases[0]
    assert case.version == 'mind-initiative-1' and case.timeout_seconds == 600
    assert case.inputs['dataset'] == {'id': 'mind-initiative-1', 'version': 'mind-initiative-1',
                                      'sha256': content, 'split': 'dev'}
    assert case.inputs['profile'] == profile and case.inputs['arm'] == 'base-heartbeat'
    assert 'body' in case.oracle and 'seed' not in case.inputs
    legacy = paired_cases.cases('base_hermes', dataset_dir=tmp_path / 'fam')
    assert [c.id for c in legacy] == [c.id for c in cases]
    with pytest.raises(ValueError, match='installed'):
        paired_cases.cases('base_hermes', dataset_version='mind-initiative-1')


def test_generated_loader_rejects_tampering_and_frozen_names(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'initiative.py')
    generate.write(tmp_path / 'fam', module, 3, 'dev', 1, GENERATORS / 'initiative.py')
    scenarios = tmp_path / 'fam' / 'scenarios.json'
    original = scenarios.read_bytes()
    scenarios.write_bytes(original.replace(b'"window": [\n', b'"window": [ \n', 1))
    with pytest.raises(ValueError, match='checksum'):
        paired_cases.load_generated_dataset(tmp_path / 'fam')
    scenarios.write_bytes(original)
    manifest_path = tmp_path / 'fam' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    for change in ({'dataset_id': 'paired-agent-reviewed-2', 'version': 'paired-agent-reviewed-2'},
                   {'generator': {**manifest['generator'], 'split': 'private'}},
                   {'families': {'warranted': 4, 'control': 3}}):
        manifest_path.write_text(json.dumps({**manifest, **change}))
        with pytest.raises(ValueError):
            paired_cases.load_generated_dataset(tmp_path / 'fam')


def test_heldout_templates_must_live_outside_the_repository(generate, tmp_path, monkeypatch):
    inside = GENERATORS / 'initiative.py'
    with pytest.raises(ValueError, match='outside the repository'):
        generate.heldout_path(str(inside))
    monkeypatch.delenv(generate.HELDOUT_ENV, raising=False)
    with pytest.raises(ValueError, match='held-out split needs'):
        generate.heldout_path(None)
    outside = tmp_path / 'heldout_initiative.py'
    shutil.copy(inside, outside)
    assert generate.heldout_path(str(outside)) == outside.resolve()
    monkeypatch.setenv(generate.HELDOUT_ENV, str(outside))
    assert generate.heldout_path(None) == outside.resolve()
    code = generate.main(['--family', 'initiative', '--split', 'heldout', '--seed', '5',
                          '--per-template', '1', '--output', str(tmp_path / 'held')])
    assert code == 0
    manifest = json.loads((tmp_path / 'held' / 'manifest.json').read_text())
    assert manifest['generator']['split'] == 'heldout'
    assert not (tmp_path / 'held' / 'heldout_initiative.py').exists()
    assert 'heldout_initiative' not in (tmp_path / 'held' / 'manifest.json').read_text()


def test_draws_give_fixed_width_ids_distinct_across_contacts_and_sources(generate):
    assert {'initiative', 'drives', 'people', 'affect', 'opinions', 'memory', 'identity'} <= set(generate.FAMILIES)
    draw = generate.Draw(3)
    identities = [draw.contact() for _ in range(40)] + [draw.source() for _ in range(40)]
    assert len(set(identities)) == 80
    assert all(re.fullmatch(r'p-\d\d', item) for item in identities[:40])
    assert all(re.fullmatch(r's-\d\d', item) for item in identities[40:])
    assert generate.Draw(3).contact() == identities[0], 'the same seed draws the same ids'


def test_family_module_contract_is_checked(generate, tmp_path):
    bad = tmp_path / 'bad.py'
    bad.write_text("FAMILY = 'x'\nTEMPLATES = {'t': ('group', 'not callable')}\n")
    with pytest.raises(ValueError, match='FAMILY and TEMPLATES'):
        generate.load_templates(bad)
    wrong = tmp_path / 'wrong.py'
    wrong.write_text("FAMILY = 'x-1'\nTEMPLATES = {'t': ('g', lambda draw: {'episodes': []})}\n")
    with pytest.raises(ValueError, match='renders initial_files'):
        generate.render(generate.load_templates(wrong), 1, 1)
    # Checkpoints grade snapshots, so they need the workflow that declares them.
    orphan = tmp_path / 'orphan.py'
    orphan.write_text("FAMILY = 'x-1'\nTEMPLATES = {'t': ('g', lambda draw: {'initial_files': {}, 'episodes': [],"
                      " 'artifacts': [{'path': 'a.json'}], 'checkpoints': []})}\n")
    with pytest.raises(ValueError, match='renders initial_files'):
        generate.render(generate.load_templates(orphan), 1, 1)
    module = generate.load_templates(GENERATORS / 'initiative.py')
    with pytest.raises(ValueError, match='32-bit'):
        generate.render(module, -1, 1)
    with pytest.raises(ValueError, match='per template'):
        generate.render(module, 1, 0)
