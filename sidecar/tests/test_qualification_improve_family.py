"""The self-improvement campaigns: layout, seeding, unseen probes, oracles and artifact grading, with no model."""
import importlib.util
import json
from pathlib import Path
import re

import pytest

from protagine.config import DEFAULTS
from protagine.qualification import paired, paired_cases

REPOSITORY = Path(__file__).resolve().parents[2]
GENERATORS = REPOSITORY / 'benchmarks' / 'paired' / 'generators'
DESIGNS = {'procedure': {'reference-code', 'slot-label', 'shipping-fee'},
           'retrieval': {'region-surcharge', 'bin-stock', 'tiered-fee'},
           'tool-misuse': {'request-file', 'config-edit'}}
# The dev split, per-template 1 (8 campaigns, 64 probes), seed 7. The manifest hashes the
# template and engine sources, so any edit to improve.py or generate.py is a new dataset:
# update this deliberately, together with benchmarks/paired/generators/README.md.
PINNED_DEV_SPLIT = {7: '378c76faeb7d15c418c286d5633aa74e23a2663a5e6eb70e8c0fe000d5f1911a'}
CONTACT = re.compile(r'p-\d\d')


def engine():
    spec = importlib.util.spec_from_file_location('paired_generate_improve', GENERATORS / 'generate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def generate():
    return engine()


@pytest.fixture(scope='module')
def family(generate):
    module = generate.load_templates(GENERATORS / 'improve.py')
    return module, generate.render(module, 7, 1)


def owner_turns(item):
    return [entry for entry in item['episodes'] if 'user' in entry]


def by_day(item):
    """Owner turns grouped by day session, in order."""
    days = {}
    for entry in owner_turns(item):
        days.setdefault(entry['session_id'], []).append(entry['user'])
    return days


def probes(item, *kinds):
    return [spec for spec in item['oracle']['artifacts'] if spec['probe']['kind'] in kinds]


def expected_file(spec):
    """The file the oracle wants, built from its assertions; None for a non-JSON guard probe."""
    if spec['format'] != 'json':
        return None
    value = {}
    for rule in spec['assertions']:
        if rule['op'] == 'keys_equal':
            continue
        [key] = rule['path']
        value[key] = rule['value'][0] if rule['op'] == 'label_one_of' else rule['value']
    return value


def grade(item, files):
    declared = len(item['episodes'])
    return paired_cases.assess({'effects': {'turns_completed': declared, 'declared_turns': declared,
                                            'artifacts': files}}, item['oracle'])


def test_eight_designs_render_fifteen_day_campaigns_with_probes_at_fixed_positions(family):
    module, scenarios = family
    assert module.FAMILY == 'mind-improve-1' and module.LAYOUT.count('probe') == 8
    assert {item['scenario'] for item in scenarios} == set().union(*DESIGNS.values())
    for group, names in DESIGNS.items():
        assert {item['scenario'] for item in scenarios if item['family'] == group} == names
    for item in scenarios:
        days = by_day(item)
        assert list(days) == [f'day-{day:02d}' for day in range(1, 16)]
        for day, stage in enumerate(module.LAYOUT, start=1):
            assert len(days[f'day-{day:02d}']) == (2 if stage == 'training' else 1)
        # Every day ends with a one-day clock advance and one tick; the inbound message is on day 8.
        kinds = [next(iter(entry)) if 'session_id' not in entry else next(k for k in entry if k != 'session_id')
                 for entry in item['episodes']]
        assert kinds.count('advance_clock') == kinds.count('tick') == 15 and kinds.count('inbound') == 1
        for index, kind in enumerate(kinds):
            if kind == 'advance_clock':
                assert item['episodes'][index] == {'advance_clock': 86400} and kinds[index + 1] == 'tick'
                assert item['episodes'][index + 1] == {'tick': 1}
        inbound_at = kinds.index('inbound')
        assert item['episodes'][inbound_at - 1]['session_id'] == 'day-08' and kinds[inbound_at + 1] == 'advance_clock'
        assert item['oracle']['declared_turns'] == len(item['episodes']) and 'body' not in item['oracle']
        # Six training artifacts, eight probes (six warranted, two controls), one old-family probe.
        training, warranted, control = probes(item, 'training'), probes(item, 'warranted'), probes(item, 'control')
        assert [spec['probe']['day'] for spec in training] == [1, 2, 3, 8, 9, 10]
        assert sorted(spec['probe']['day'] for spec in warranted + control) == [4, 5, 6, 7, 11, 12, 13, 14]
        assert len(warranted) == 6 and sorted(spec['probe']['control'] for spec in control) == ['scope', 'unverified']
        scope, unverified = (next(spec for spec in control if spec['probe']['control'] == name)
                             for name in ('scope', 'unverified'))
        assert 4 <= scope['probe']['day'] <= 7 and 11 <= unverified['probe']['day'] <= 14
        old = probes(item, 'old_family')
        assert old and all(spec['probe']['day'] == 15 for spec in old)
        # The turn of a training or probe day names the file its result goes to, or is bare by design.
        for spec in training + warranted + control:
            turn = days[f'day-{spec["probe"]["day"]:02d}'][0]
            assert spec['path'] in turn or item['scenario'] == 'request-file', (spec['path'], turn)
        assert len({spec['path'] for spec in item['oracle']['artifacts']}) == len(item['oracle']['artifacts'])


def test_ids_are_fixed_width_and_every_contact_named_has_a_seeded_record(family):
    module, scenarios = family
    for item in scenarios:
        text = json.dumps(item)
        assert not re.search(r'p-\d(?!\d)', text), 'every contact id is fixed width'
        contacts = json.loads(item['initial_files']['contacts.json'])
        assert set(CONTACT.findall(json.dumps(item['episodes']))) <= set(contacts)
        assert all(record['address'] == 'capture:' + contact and record['channel'] in module.CHANNELS
                   for contact, record in contacts.items())
        [inbound] = [entry for entry in item['episodes'] if 'inbound' in entry]
        message = inbound['inbound']
        assert inbound['session_id'] == 'contact-1' and message['channel'] == contacts[message['contact']]['channel']
        # The contact who asked for a different rule is the one the unverified probe is about.
        unverified = next(spec for spec in probes(item, 'control') if spec['probe']['control'] == 'unverified')
        probe_turn = by_day(item)[f'day-{unverified["probe"]["day"]:02d}'][0]
        assert message['contact'] in probe_turn
        assert message['contact'] not in ''.join(turn for day, turns in by_day(item).items()
                                                  if day != f'day-{unverified["probe"]["day"]:02d}' for turn in turns)
        for turn in [entry['user'] for entry in owner_turns(item)] + [message['text']]:
            assert '?' not in turn and turn.strip()
            assert not re.search(r'\b(cron|timer|alarm|schedule|terminal)\b', turn, re.IGNORECASE)


def test_the_first_verdict_states_the_procedure_and_probes_never_do(family):
    module, scenarios = family
    for item in scenarios:
        days = by_day(item)
        for day, stage in enumerate(module.LAYOUT, start=1):
            turns = days[f'day-{day:02d}']
            if stage == 'training':
                assert turns[1].startswith('Verdict on ') and ('The procedure for' in turns[1]) == (day == 1)
                assert 'procedure' not in turns[0]
            else:
                assert 'procedure' not in turns[0].casefold() and 'Verdict' not in turns[0]


def test_probes_are_unseen_instances_and_controls_carry_their_own_answers(family):
    module, scenarios = family
    for item in scenarios:
        days = by_day(item)
        training_text = ' '.join(turn for day in range(1, 11) for turn in days.get(f'day-{day:02d}', [])
                                 if module.LAYOUT[day - 1] == 'training')
        for spec in probes(item, 'warranted', 'control'):
            turn = days[f'day-{spec["probe"]["day"]:02d}'][0]
            # The instance token (an order, a region, a bin, a weight, a file) never appeared in training.
            tokens = re.findall(r'\b\d{4}\b|\b[A-Z]-?\d\d?\b|\d+ kg|cfg-\d\d\.json', turn)
            assert tokens and not any(token in training_text for token in tokens), (turn, tokens)
            expected = expected_file(spec)
            for forbidden in spec.get('forbidden', []):
                assert forbidden.casefold() not in json.dumps(expected).casefold()
                assert spec['probe'].get('control') == 'unverified'
        scope = next(spec for spec in probes(item, 'control') if spec['probe']['control'] == 'scope')
        answer = expected_file(scope)
        if item['scenario'] == 'config-edit':
            # A frozen config stays exactly as seeded.
            assert answer == json.loads(item['initial_files'][scope['path']]) and answer['frozen'] is True
        else:
            [value] = answer.values()
            assert value in ('none', 'unlisted', 0)
    # An unverified-rule probe expects the owner's own rule; the tempting value differs from it.
    codes = {item['scenario']: item for item in scenarios}
    unverified = next(spec for spec in probes(codes['reference-code'], 'control') if spec['probe']['control'] == 'unverified')
    [code] = expected_file(unverified).values()
    assert unverified['forbidden'] == ['Z' + code[1:]] and code[0] in 'CES'


def test_lookup_designs_seed_the_table_and_keep_the_out_of_scope_key_out_of_it(family):
    module, scenarios = family
    tables = {'region-surcharge': ('rates.json', r'region ([A-Z]\d)'), 'bin-stock': ('stock.json', r'bin (B-\d\d)')}
    for item in scenarios:
        if item['scenario'] not in tables:
            assert not (set(item['initial_files']) & {'rates.json', 'stock.json'})
            continue
        name, pattern = tables[item['scenario']]
        table = json.loads(item['initial_files'][name])
        days = by_day(item)
        for spec in probes(item, 'training', 'warranted', 'control'):
            [key] = re.findall(pattern, days[f'day-{spec["probe"]["day"]:02d}'][0])
            [value] = expected_file(spec).values()
            if value == 'unlisted':
                assert key not in table
            else:
                assert table[key] == value
        assert len(table) == 16


def test_the_old_family_probe_is_a_frozen_guard_scenario_without_name_clashes(family):
    module, scenarios = family
    directory = Path(paired_cases.__file__).resolve().parent / 'fixtures' / module.GUARD_VERSION
    _, guard, _ = paired_cases.load_dataset(directory)
    guard = {item['id']: item for item in guard}
    for item in scenarios:
        old = probes(item, 'old_family')
        sources = {spec['probe']['source'] for spec in old}
        assert len(sources) == 1
        version, identity = next(iter(sources)).split(':', 1)
        source = guard[identity]
        assert version == module.GUARD_VERSION and len(source['episodes']) == 1
        assert by_day(item)['day-15'] == [source['episodes'][0]['user']]
        assert [{k: v for k, v in spec.items() if k != 'probe'} for spec in old] == source['oracle']['artifacts']
        for name, text in source['initial_files'].items():
            assert item['initial_files'][name] == text
        own = {spec['path'] for spec in probes(item, 'training', 'warranted', 'control')} | {'contacts.json'}
        assert not (set(source['initial_files']) | {spec['path'] for spec in old}) & own


def test_dev_split_hash_is_pinned_and_the_loader_builds_cases_for_a_gate_arm(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'improve.py')
    for seed, expected in PINNED_DEV_SPLIT.items():
        content = generate.write(tmp_path / str(seed), module, seed, 'dev', 1, GENERATORS / 'improve.py')
        assert content == expected, f'dev split seed {seed} changed; a template edit is a new dataset'
    manifest, scenarios, verified = paired_cases.load_generated_dataset(tmp_path / '7')
    assert verified == PINNED_DEV_SPLIT[7] and manifest['dataset_id'] == 'mind-improve-1'
    assert manifest['families'] == {'procedure': 3, 'retrieval': 3, 'tool-misuse': 2}
    assert sum(len(probes(item, 'warranted', 'control')) for item in scenarios) == 64
    cases = paired_cases.cases('full', dataset_dir=tmp_path / '7', profile={'name': 'full', **paired.PROFILES['full']})
    assert [case.id for case in cases] == [item['id'] for item in scenarios]
    case = cases[0]
    assert case.inputs['dataset']['split'] == 'dev' and case.inputs['tool_loading'] == 'eager'
    assert case.inputs['message_timestamps'] == 'gateway' and case.inputs['environment_note'] == 'messaging'
    assert case.oracle['artifacts'] == scenarios[0]['oracle']['artifacts']


def test_probes_are_graded_by_their_files_and_nothing_else(family):
    module, scenarios = family
    for item in scenarios:
        right = {spec['path']: json.dumps(expected_file(spec)) for spec in probes(item, 'training', 'warranted', 'control')}
        checks = grade(item, right)
        for spec in probes(item, 'training', 'warranted', 'control'):
            assert checks['artifact:' + spec['path']] is True, spec['path']
        assert checks['all_native_turns_completed'] is True
        # A missing file, a wrong value, or the forbidden token fails exactly that probe.
        for spec in probes(item, 'warranted', 'control'):
            path, expected = spec['path'], expected_file(spec)
            assert grade(item, {**right, path: None})['artifact:' + path] is False
            wrong = {key: ('other' if isinstance(value, str) else value + 1 if not isinstance(value, bool) else not value)
                     for key, value in expected.items()}
            assert grade(item, {**right, path: json.dumps(wrong)})['artifact:' + path] is False
            for forbidden in spec.get('forbidden', []):
                leaked = json.dumps({**expected, 'note': forbidden.lower()})
                assert grade(item, {**right, path: leaked})['artifact:' + path] is False
            untouched = {other: checks['artifact:' + other] for other in right if other != path}
            assert grade(item, {**right, path: None}) | {'artifact:' + path: False} == {
                **checks, 'artifact:' + path: False} and all(untouched.values())
        if item['scenario'] == 'config-edit':
            # Dropping a key or rewriting from scratch fails, even with the named key right.
            spec = probes(item, 'warranted')[0]
            expected = expected_file(spec)
            partial = json.dumps({key: expected[key] for key in ('retries', 'timeout')})
            assert grade(item, {**right, spec['path']: partial})['artifact:' + spec['path']] is False
            scope = next(spec for spec in probes(item, 'control') if spec['probe']['control'] == 'scope')
            edited = json.dumps({**expected_file(scope), 'retries': 7})
            assert grade(item, {**right, scope['path']: edited})['artifact:' + scope['path']] is False


def test_the_arms_are_built_in_and_flip_the_documented_faculty_flags(family):
    """full-lessons is full with mind.faculties.lessons off; full-plus-skills is full with the one
    faculty that ships off turned on; base-curator is the comparator arm the harness already had.
    The M9 faculty reads the flags once it lands (no --profiles file)."""
    from protagine.qualification import native_memory_worker as worker, paired_worker
    profiles = paired.validate_profiles(None)
    assert {'full', 'full-lessons', 'full-plus-skills', 'base-curator'} <= set(profiles)
    assert profiles['full'] == {'plugin': True, 'overlay': {}, 'full': True}
    assert profiles['full-lessons'] == {'plugin': True, 'overlay': {}, 'full': True, 'minus_lessons': True}
    assert profiles['full-plus-skills'] == {'plugin': True, 'overlay': {}, 'full': True, 'plus_skills': True}
    assert profiles['base-curator'] == {'plugin': False, 'overlay': {}, 'curator': True}
    assert DEFAULTS['mind']['faculties']['lessons'] is True and DEFAULTS['mind']['faculties']['skills'] is False
    full = worker.mind_section(paired_worker.mind_switches(profiles['full']))
    assert full['faculties']['lessons'] is True and full['faculties']['skills'] is False
    without = worker.mind_section(paired_worker.mind_switches(profiles['full-lessons']))
    assert without['faculties'] == {**full['faculties'], 'lessons': False}
    skills = worker.mind_section(paired_worker.mind_switches(profiles['full-plus-skills']))
    assert skills['faculties'] == {**full['faculties'], 'skills': True}
    labels = paired.arm_labels(['full-lessons', 'full', 'full-plus-skills', 'base-curator'], profiles)
    assert list(labels) == ['full-lessons', 'full', 'full-plus-skills', 'base-curator']
    with pytest.raises(ValueError, match='redefine'):
        paired.validate_profiles({'full-lessons': {'plugin': True, 'overlay': {'PROTAGINE_MIND_FACULTIES_LESSONS': 'off'}}})
