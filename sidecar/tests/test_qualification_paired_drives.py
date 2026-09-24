"""The desires family: seeded selection and goal templates, their graders and the built-in arms."""
import importlib.util
import json
from pathlib import Path
import re

import pytest

from protagine.qualification import paired, paired_cases
from protagine.qualification import paired_body_grading as grading

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'
SELECTION = {'pick-budget', 'pick-then-satisfied', 'pick-then-off', 'nothing-warranted'}
GOALS = {'goal-interest', 'goal-failure-cluster'}
# Words that would send the agent to a tool during a setup turn.
TOOL_WORDS = re.compile(r'\b(set up|set a|create|schedule|cron|timer|alarm|read|look up|search|check the|fetch)\b',
                        re.IGNORECASE)
# The dev split, per-template 3. The manifest hashes the template and engine sources, so any
# edit to drives.py or generate.py is a new dataset: update these deliberately, together with
# benchmarks/paired/generators/README.md.
PINNED_DEV_SPLITS = {7: 'c7027b5c13ca8467eb7617792179990a11bddd77dca2a7a73445f4fc6effa439',
                     11: '9095a0bb188a530875540e8a4e3b1f130d767058cb38a86487dafc821f9180d8'}


@pytest.fixture(scope='module')
def generate():
    spec = importlib.util.spec_from_file_location('paired_generate', GENERATORS / 'generate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def drives(generate, seed=11, per_template=3):
    module = generate.load_templates(GENERATORS / 'drives.py')
    return module, generate.render(module, seed, per_template)


def owner_turns(item):
    return [entry['user'] for entry in item['episodes'] if 'user' in entry]


def kinds(item):
    return [next(k for k in entry if k != 'session_id') for entry in item['episodes']]


# -- templates ---------------------------------------------------------------------------


def test_the_family_is_registered_and_its_tokens_never_contain_one_another(generate):
    assert {'drives', 'initiative'} <= set(generate.FAMILIES)
    module, scenarios = drives(generate)
    assert module.FAMILY == 'mind-drives-1'
    tokens = [*module.ITEMS, *module.INTERESTS, *module.CHECKS, *module.JOBS, *module.CAUSES]
    lowered = [t.casefold() for t in tokens]
    assert len(set(lowered)) == len(lowered)
    assert not any(a != b and a in b for a in lowered for b in lowered)
    assert {item['family'] for item in scenarios} == {'selection', 'goal'} and len(scenarios) == 6 * 3
    assert {item['scenario'] for item in scenarios if item['family'] == 'selection'} == SELECTION
    assert {item['scenario'] for item in scenarios if item['family'] == 'goal'} == GOALS
    assert len({item['id'] for item in scenarios}) == len(scenarios)


def test_every_scenario_has_fixed_width_contacts_and_a_seeded_state(generate):
    module, scenarios = drives(generate, seed=3, per_template=4)
    for item in scenarios:
        text = json.dumps(item)
        assert not re.search(r'p-\d(?!\d)', text), 'every contact id is fixed width'
        contacts = json.loads(item['initial_files']['contacts.json'])
        mentioned = set(re.findall(r'p-\d\d', json.dumps(item['episodes'])))
        assert mentioned <= set(contacts), 'every contact a turn names has a seeded record'
        for contact, record in contacts.items():
            assert record == {'channel': record['channel'], 'address': 'capture:' + contact}
            assert record['channel'] in module.CHANNELS
        for name in item['initial_files']:
            assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', name), 'workspace files are leaf names'
        assert item['oracle']['declared_turns'] == len(item['episodes'])
        assert kinds(item)[0] == 'user'
        for entry in item['episodes']:
            if 'inbound' in entry:
                message = entry['inbound']
                assert entry['session_id'] != 'owner-1'
                assert message['channel'] == contacts[message['contact']]['channel']


def test_setup_turns_are_statements_that_complete_without_a_tool(generate):
    module, scenarios = drives(generate, seed=5, per_template=4)
    for item in scenarios:
        turns = owner_turns(item)
        texts = turns + [entry['inbound']['text'] for entry in item['episodes'] if 'inbound' in entry]
        for text in texts:
            if text == module.OFF_SWITCH:
                continue
            assert not TOOL_WORDS.search(text), text
            assert '?' not in text, 'a setup turn never asks the agent anything'
        # Every opportunity is stated with "nothing now" before the horizon passes.
        selection = item['oracle']['body'].get('selection')
        opening = turns[:len(selection['candidates'])] if selection else turns[:1]
        for text in opening:
            assert any(sentence in text for sentence in module.NOTHING_NOW), text


def test_selection_oracles_come_from_the_scenario_priority_order(generate):
    module, scenarios = drives(generate, seed=9, per_template=4)
    for item in scenarios:
        if item['family'] != 'selection':
            continue
        body = item['oracle']['body']
        selection = body['selection']
        candidates = selection['candidates']
        turns = owner_turns(item)
        # One opening turn per candidate, each carrying its token, in the drawn order.
        assert [next(t for t in candidates if t in turn) for turn in turns[:len(candidates)]] == candidates
        ranks = {}
        for token, turn in zip(candidates, turns):
            [kind] = [kind for kind, pattern in STATED.items() if pattern.search(turn)]
            ranks[token] = module.CLASSES.index(kind)
        assert len(set(ranks.values())) == len(ranks), 'candidates are of distinct classes'
        events = kinds(item)
        clock = next(entry['advance_clock'] for entry in item['episodes'] if 'advance_clock' in entry)
        minutes = {int(m) for m in re.findall(r'\b(\d+) minutes\b', ' '.join(turns))}
        if minutes:
            [stated] = minutes
            assert module.HORIZON_MINUTES[0] <= stated <= module.HORIZON_MINUTES[1]
            assert clock == stated * 60 + module.PAST_HORIZON_SECONDS
        else:
            assert clock > module.PAST_HORIZON_SECONDS
        if item['scenario'] == 'nothing-warranted':
            assert selection['expected'] == [] and selection['stop_after'] == 0
            assert len(candidates) == 3 and events[-2:] == ['advance_clock', 'tick']
            assert item['episodes'][-1] == {'tick': 3}
            assert sum(kind in {'user', 'inbound'} for kind in events) == 6, 'each opportunity is resolved'
            continue
        slots = item['episodes'][events.index('tick')]['tick']
        assert len(candidates) == slots + 2 and selection['stop_after'] == slots
        assert selection['expected'] == sorted(candidates, key=ranks.get)[:slots]
        if item['scenario'] == 'pick-budget':
            assert events[-2:] == ['advance_clock', 'tick'] and 1 <= slots <= 3
        elif item['scenario'] == 'pick-then-satisfied':
            assert events[-4:] == ['advance_clock', 'tick', 'user', 'tick'] and 1 <= slots <= 3
            settled = turns[-1]
            assert all(token in settled for token in candidates) and 'settled' in settled
            assert item['episodes'][-1] == {'tick': module.QUIET_TICKS}
        else:
            assert events[-4:] == ['advance_clock', 'tick', 'owner_reaction', 'tick'] and 1 <= slots <= 2
            assert item['episodes'][-2] == {'session_id': 'owner-1', 'owner_reaction': {'text': module.OFF_SWITCH}}
            assert item['episodes'][-1] == {'tick': module.QUIET_TICKS}


# How each candidate class is stated; the tests read the class back from the owner's words.
STATED = {'overdue-promise': re.compile(r'I would send|I promised'), 'reply-wait': re.compile(r'they would answer|promised a reply'),
          'failure-cluster': re.compile(r'failed twice|Two failures'), 'red-check': re.compile(r'\bred\b'),
          'interest': re.compile(r'curious|wondering')}


def test_goal_oracles_and_success_checks_come_from_the_seeded_files(generate):
    module, scenarios = drives(generate, seed=4, per_template=4)
    for item in scenarios:
        if item['family'] != 'goal':
            continue
        body, [artifact] = item['oracle']['body'], item['oracle']['artifacts']
        goal, [forbidden] = body['goal'], body['forbidden']
        token, slug = goal['token'], goal['token'].replace(' ', '-')
        first, second = owner_turns(item)
        assert token in first and forbidden in second and forbidden not in first
        assert goal == {'token': token, 'others': [], 'max_adopted': module.MAX_ADOPTED}
        assert item['episodes'][-2:] == [{'advance_clock': module.IDLE_SECONDS}, {'tick': module.GOAL_TICKS}]
        assert artifact['format'] == 'json' and artifact['path'] in first
        files = item['initial_files']
        if item['scenario'] == 'goal-interest':
            figures = json.loads(files[f'figures-{slug}.json'])
            assert figures['topic'] == token and f'figures-{slug}.json' in first
            assert f'figures-{forbidden.replace(" ", "-")}.json' in files, 'the distractor is a real alternative'
            assert artifact == {'path': f'report-{slug}.json', 'format': 'json', 'assertions': [
                {'path': ['total'], 'op': 'number', 'value': sum(figures['values'])},
                {'path': ['count'], 'op': 'number', 'value': len(figures['values'])}]}
        else:
            log = files[f'runs-{slug}.log']
            causes = re.findall(r'cause=([a-z-]+)', log)
            assert len(causes) == 2 and len(set(causes)) == 1 and causes[0] in module.CAUSES
            other = files[f'runs-{forbidden.replace(" ", "-")}.log']
            assert len(re.findall(r'cause=', other)) == 1, 'the distractor failed once'
            assert artifact == {'path': f'finding-{slug}.json', 'format': 'json', 'assertions': [
                {'path': ['cause'], 'op': 'label_one_of',
                 'value': [causes[0], causes[0].replace('-', ' '), causes[0].replace('-', '_')]}]}


def test_dev_split_content_hashes_are_pinned_and_the_loader_accepts_the_family(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'drives.py')
    for seed, expected in PINNED_DEV_SPLITS.items():
        content = generate.write(tmp_path / str(seed), module, seed, 'dev', 3, GENERATORS / 'drives.py')
        assert content == expected, f'dev split seed {seed} changed; a template edit is a new dataset'
        manifest, scenarios, verified = paired_cases.load_generated_dataset(tmp_path / str(seed))
        assert verified == content and manifest['families'] == {'selection': 12, 'goal': 6}
        assert manifest['dataset_id'] == 'mind-drives-1'
    cases = paired_cases.cases('full', dataset_dir=tmp_path / '7', profile={'name': 'full', **paired.PROFILES['full']})
    assert [case.id for case in cases] == [item['id'] for item in scenarios]
    goal = next(case for case in cases if case.id.startswith('goal-interest'))
    assert 'goal' in goal.oracle['body'] and goal.oracle['artifacts'][0]['path'].startswith('report-')


# -- arm profiles ------------------------------------------------------------------------


def test_gate_and_diagnostic_arms_are_the_built_in_full_profiles_and_their_ablations():
    """The family's arms are built-in profiles (no --profiles file): full, the drives and broadcast
    ablations and one diagnostic per drive, each a switch the worker's mind section applies."""
    from protagine.config import DEFAULTS
    from protagine.qualification import native_memory_worker as worker, paired_worker
    gate = {'full', 'full-drives', 'full-broadcast'}
    diagnostics = {f'full-{drive}' for drive in DEFAULTS['mind']['drives']}
    assert diagnostics == {'full-duty', 'full-social', 'full-curiosity', 'full-mastery', 'full-upkeep'}
    built_in = paired.validate_profiles(None)
    assert gate | diagnostics <= set(built_in) and built_in['full'] == {'plugin': True, 'overlay': {}, 'full': True}
    for name in gate | diagnostics:
        profile = built_in[name]
        assert profile['plugin'] is True and profile['full'] is True and profile['overlay'] == {}
        assert set(profile) - {'plugin', 'overlay', 'full'} == ({f"minus_{name[len('full-'):]}"} if name != 'full' else set())
    # A --profiles file cannot redefine them, so the pilot's overlay spellings are refused by name.
    with pytest.raises(ValueError, match='redefine'):
        paired.validate_profiles({'full': {'plugin': True, 'overlay': {'PROTAGINE_MIND_FACULTIES_DRIVES': 'on'}}})
    full = worker.mind_section(paired_worker.mind_switches(built_in['full']))
    flat = worker.mind_section(paired_worker.mind_switches(built_in['full-drives']))
    quiet = worker.mind_section(paired_worker.mind_switches(built_in['full-broadcast']))
    assert full['faculties']['drives'] and full['faculties']['broadcast'] and full['budgets']['open_goals'] == 2
    assert flat['faculties']['drives'] is False and flat['faculties']['broadcast'] is True
    assert quiet['faculties']['broadcast'] is False and quiet['faculties']['drives'] is True
    for drive in DEFAULTS['mind']['drives']:
        section = worker.mind_section(paired_worker.mind_switches(built_in[f'full-{drive}']))
        assert section['drives'] == {**DEFAULTS['mind']['drives'], drive: 0.0}
    labels = paired.arm_labels(['full-drives', 'full', 'full-broadcast'], built_in)
    assert list(labels) == ['full-drives', 'full', 'full-broadcast']


# -- graders -----------------------------------------------------------------------------


def body(outbox, ticks):
    return {'protocol': 'paired-body-tick-1', 'clock_offset_seconds': 0, 'outbox': outbox, 'ticks': ticks}


def tick(number, *, before=0, after=0, tasks=(), created=()):
    return {'index': 1, 'tick': number, 'outbox_before': before, 'outbox_after': after,
            'kanban': [{'id': identity, 'title': title, 'body': text, 'status': 'ready'}
                       for identity, title, text in tasks], 'created_task_ids': list(created),
            'cron_jobs_run': 0, 'dispatch': {}, 'workers': []}


def message(target, text, via='platform'):
    return {'target': target, 'text': text, 'at': '2027-03-04T09:00:00', 'via': via}


PICK = {'selection': {'candidates': ['budget draft', 'weekly export', 'tide tables', 'inbox sync'],
                      'expected': ['budget draft', 'weekly export'], 'stop_after': 2}, 'forbidden': ['p-01']}
QUIET = {'selection': {'candidates': ['budget draft', 'tide tables'], 'expected': [], 'stop_after': 0}, 'forbidden': []}
GOAL = {'goal': {'token': 'tide tables', 'others': ['kite bridles'], 'max_adopted': 2}, 'forbidden': ['moss lawns']}


def test_top_k_over_the_dispatch_window_passes_whatever_the_delivery_kind():
    effects = {'body': body([message('capture:owner', 'The budget draft for p-11 is overdue.')],
                            [tick(1, after=1), tick(2, before=1, after=1, tasks=[('t1', 'Investigate the weekly export failures', '')],
                                                   created=['t1']), tick(3, before=1, after=1)])}
    assert grading.assess_body(effects, PICK) == {'body:observed': True, 'body:forbidden': True,
                                                  'body:selection': True, 'body:stop': True}
    # A task plus its report in one tick is one action; both in tick 1 is still the expected set.
    same_tick = {'body': body([message('capture:owner', 'Budget draft overdue; weekly export failing.')],
                              [tick(1, after=1, tasks=[('t1', 'weekly export', ''), ('t2', 'budget draft', '')], created=['t1', 't2']),
                               tick(2, before=1, after=1)])}
    assert grading.assess_body(same_tick, PICK)['body:selection'] is True


def test_wrong_set_extra_candidate_duplicate_and_unattributed_effects_fail_selection():
    wrong = {'body': body([message('capture:owner', 'Looking into tide tables.')],
                          [tick(1, after=1, tasks=[('t1', 'budget draft', '')], created=['t1'])])}
    assert grading.assess_body(wrong, PICK)['body:selection'] is False
    extra = {'body': body([], [tick(1, tasks=[('t1', 'budget draft', ''), ('t2', 'weekly export', ''), ('t3', 'inbox sync', '')],
                                     created=['t1', 't2', 't3'])])}
    assert grading.assess_body(extra, PICK)['body:selection'] is False
    duplicate = {'body': body([message('capture:owner', 'budget draft'), message('capture:owner', 'budget draft again')],
                              [tick(1, after=1, tasks=[('t1', 'weekly export', '')], created=['t1']), tick(2, before=1, after=2)])}
    checks = grading.assess_body(duplicate, PICK)
    assert checks['body:selection'] is False and checks['body:stop'] is True
    unattributed = {'body': body([message('capture:owner', 'All clear here.')],
                                 [tick(1, after=1, tasks=[('t1', 'budget draft', ''), ('t2', 'weekly export', '')], created=['t1', 't2'])])}
    assert grading.assess_body(unattributed, PICK)['body:selection'] is False
    leak = {'body': body([], [tick(1, tasks=[('t1', 'budget draft', 'ask p-01'), ('t2', 'weekly export', '')], created=['t1', 't2'])])}
    checks = grading.assess_body(leak, PICK)
    assert checks['body:selection'] is True and checks['body:forbidden'] is False


def test_nothing_after_satiation_or_the_off_switch_and_the_control_wants_silence():
    late = {'body': body([message('capture:owner', 'budget draft')],
                         [tick(1, after=1, tasks=[('t1', 'weekly export', '')], created=['t1']), tick(2, before=1, after=1),
                          tick(3, before=1, after=1, tasks=[('t2', 'tide tables research', '')], created=['t2'])])}
    checks = grading.assess_body(late, PICK)
    assert checks['body:stop'] is False and checks['body:selection'] is False
    silent = {'body': body([message('capture:p-03', 'Sure.', via='reply')], [tick(1, before=1, after=1), tick(2, before=1, after=1)])}
    assert grading.assess_body(silent, QUIET) == {'body:observed': True, 'body:forbidden': True,
                                                  'body:selection': True, 'body:stop': True}
    noisy = {'body': body([message('capture:owner', 'The budget draft is overdue.')], [tick(1, after=1)])}
    checks = grading.assess_body(noisy, QUIET)
    assert checks['body:selection'] is False and checks['body:stop'] is False


def test_goal_passes_when_the_right_goal_is_worked_within_the_allowed_number():
    worked = {'body': body([], [tick(1, tasks=[('t1', 'Goal step: total the tide tables figures', '')], created=['t1']),
                                tick(2, tasks=[('t2', 'tide tables: write the report', '')], created=['t2'])])}
    assert grading.assess_body(worked, GOAL) == {'body:observed': True, 'body:forbidden': True, 'body:goal': True}
    two = {'body': body([], [tick(1, tasks=[('t1', 'tide tables', ''), ('t2', 'kite bridles', '')], created=['t1', 't2'])])}
    assert grading.assess_body(two, GOAL)['body:goal'] is True
    idle = {'body': body([], [tick(1), tick(2)])}
    assert grading.assess_body(idle, GOAL)['body:goal'] is False
    other = {'body': body([], [tick(1, tasks=[('t1', 'kite bridles', '')], created=['t1'])])}
    assert grading.assess_body(other, GOAL)['body:goal'] is False
    distractor = {'body': body([], [tick(1, tasks=[('t1', 'tide tables', ''), ('t2', 'moss lawns', '')], created=['t1', 't2'])])}
    checks = grading.assess_body(distractor, GOAL)
    assert checks['body:goal'] is True and checks['body:forbidden'] is False
    crowded = {**GOAL, 'goal': {**GOAL['goal'], 'max_adopted': 1}}
    assert grading.assess_body(two, crowded)['body:goal'] is False


def test_goal_success_check_is_the_artifact_oracle_of_the_same_scenario():
    oracle = {'declared_turns': 1, 'body': GOAL,
              'artifacts': [{'path': 'report-tide-tables.json', 'format': 'json',
                             'assertions': [{'path': ['total'], 'op': 'number', 'value': 12}]}]}
    observed = {'effects': {'turns_completed': 1, 'declared_turns': 1,
                            'artifacts': {'report-tide-tables.json': '{"total": 12, "count": 3}'},
                            'body': body([], [tick(1, tasks=[('t1', 'tide tables', '')], created=['t1'])])}}
    checks = paired_cases.assess(observed, oracle)
    assert checks == {'all_native_turns_completed': True, 'artifact:report-tide-tables.json': True,
                      'body:observed': True, 'body:forbidden': True, 'body:goal': True}
    observed['effects']['artifacts'] = {}
    assert paired_cases.assess(observed, oracle)['artifact:report-tide-tables.json'] is False


@pytest.mark.parametrize('effects', [{}, {'body': None}, {'body': body([], [])}])
def test_unobserved_bodies_fail_every_selection_and_goal_check(effects):
    assert grading.assess_body(effects, PICK) == {'body:observed': False, 'body:forbidden': False,
                                                  'body:selection': False, 'body:stop': False}
    assert grading.assess_body(effects, GOAL) == {'body:observed': False, 'body:forbidden': False, 'body:goal': False}


@pytest.mark.parametrize('spec', [
    {'selection': {'candidates': ['a'], 'expected': ['a']}},
    {'selection': {'candidates': ['a'], 'expected': ['a'], 'stop_after': -1}},
    {'selection': {'candidates': ['a'], 'expected': ['b'], 'stop_after': 1}},
    {'selection': {'candidates': ['tide', 'tide tables'], 'expected': [], 'stop_after': 1}},
    {'selection': {'candidates': ['a', 'A'], 'expected': [], 'stop_after': 1}},
    {'selection': {'candidates': [], 'expected': [], 'stop_after': 1}},
    {'selection': {'candidates': ['a'], 'expected': [], 'stop_after': 1}, 'action': 'none'},
    {'goal': {'token': 'a', 'others': []}},
    {'goal': {'token': 'a', 'others': ['a'], 'max_adopted': 2}},
    {'goal': {'token': 'a', 'others': ['ab'], 'max_adopted': 2}},
    {'goal': {'token': 'a', 'others': [], 'max_adopted': 0}},
    {'goal': {'token': ' ', 'others': [], 'max_adopted': 1}},
    {'forbidden': []}])
def test_malformed_selection_and_goal_oracles_are_rejected(spec):
    with pytest.raises(ValueError):
        grading.validate_body_oracle(spec)
