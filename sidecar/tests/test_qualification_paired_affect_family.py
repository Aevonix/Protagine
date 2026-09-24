"""The mind-affect-1 dev family: template contract, seeded state, oracles from draws, graders, arms."""
import importlib.util
import json
from pathlib import Path
import re

import pytest

from protagine.qualification import paired, paired_cases

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'
TEMPLATE = GENERATORS / 'affect.py'
TREATMENT = {'overload-postpone-curiosity', 'worry-commitment-first', 'aggregate-one-cause'}
CONTROLS = {'overload-light-load', 'worry-nothing-due-soon'}
CONSUMERS = {'overload', 'priority', 'aggregate'}
# Words that would send the agent to a tool during a setup turn.
TOOL_WORDS = re.compile(r'\b(set up|set a|create|schedule|cron|timer|alarm|read|file|look up|search|check|fetch)\b',
                        re.IGNORECASE)
# The dev split, per-template 3, seed 7: the manifest hashes the template and engine sources, so
# any edit to affect.py or generate.py is a new dataset. Update deliberately, together with
# benchmarks/paired/generators/README.md and docs/proto-agi/families/mind-affect-1.md.
PINNED_DEV_SPLIT = {7: '7810d2c0e19b3430bfbcf8032421905a2752af48136de9212f0f74d4a22f0c50',
                    11: '57834816404761bbbb298c88dddd26b3a7d7dd40037eef03ea17fa8f9b1f8b46'}


@pytest.fixture(scope='module')
def generate():
    spec = importlib.util.spec_from_file_location('paired_generate', GENERATORS / 'generate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def affect(generate, seed=11, per_template=3):
    module = generate.load_templates(TEMPLATE)
    return module, generate.render(module, seed, per_template)


def turns(item, session):
    return [entry['user'] for entry in item['episodes'] if entry.get('session_id') == session]


def events(item):
    return [next(iter(entry)) for entry in item['episodes'] if 'session_id' not in entry]


def test_family_declares_treatment_and_control_templates_by_consumer(generate):
    module, scenarios = affect(generate)
    assert module.FAMILY == 'mind-affect-1' and generate.FAMILIES['affect'] == TEMPLATE
    assert set(module.TEMPLATES) == TREATMENT | CONTROLS == set(module.CONSUMERS)
    assert set(module.CONSUMERS.values()) == CONSUMERS
    assert {name for name, (group, _) in module.TEMPLATES.items() if group == 'treatment'} == TREATMENT
    assert len(scenarios) == 5 * 3 and len({item['id'] for item in scenarios}) == len(scenarios)
    for item in scenarios:
        text = json.dumps(item)
        assert not re.search(r'p-\d(?!\d)', text), 'every contact id is fixed width'
        assert all(1 <= int(c[2:]) <= 99 for c in re.findall(r'p-\d\d', text))
        assert item['oracle']['declared_turns'] == len(item['episodes'])
        assert (item['scenario'] in TREATMENT) == (item['family'] == 'treatment')


def test_setup_turns_are_statements_and_only_the_decision_turn_asks_for_work(generate):
    module, scenarios = affect(generate, seed=5, per_template=4)
    for item in scenarios:
        setup = turns(item, module.SETUP_SESSION)
        assert setup, item['id']
        for text in setup:
            assert any(sentence in text for sentence in module.NOTHING_NOW), text
            assert not TOOL_WORDS.search(text), text
            assert '?' not in text, 'a setup turn never asks the agent anything'
        decision = turns(item, module.DECISION_SESSION)
        kinds = [next(k for k in entry if k != 'session_id') if 'session_id' in entry else next(iter(entry))
                 for entry in item['episodes']]
        assert 'advance_clock' in kinds and 'tick' in kinds and kinds.index('advance_clock') < kinds.index('tick')
        [text] = decision
        assert item['episodes'][-1]['session_id'] == module.DECISION_SESSION, 'the decision turn comes last'
        [spec] = item['oracle']['artifacts']
        assert spec['path'] in text and spec['format'] == 'json' and spec['forbidden'] == []
        assert ('body' in item['oracle']) == (item['scenario'] == 'aggregate-one-cause')
        # Every session named is one of the two; a fresh session holds the decision.
        assert {e['session_id'] for e in item['episodes'] if 'session_id' in e} <= {
            module.SETUP_SESSION, module.DECISION_SESSION}


def test_background_state_is_seeded_into_the_workspace_rather_than_fetched(generate):
    module, scenarios = affect(generate, seed=3)
    for item in scenarios:
        files = item['initial_files']
        contacts = json.loads(files['contacts.json'])
        mentioned = set(re.findall(r'p-\d\d', json.dumps(item['episodes'])))
        assert mentioned == set(contacts), 'every seeded contact is named and every named contact is seeded'
        for contact, record in contacts.items():
            assert record == {'channel': record['channel'], 'address': 'capture:' + contact}
            assert record['channel'] in module.CHANNELS
        assert set(files) == {'contacts.json'}, 'no seeded source stands in for the agent\'s own history'
        assert not any('inbound' in entry for entry in item['episodes'])


def test_oracles_come_from_the_same_draws_as_the_turns(generate):
    module, scenarios = affect(generate, seed=9)
    for item in scenarios:
        name, oracle = item['scenario'], item['oracle']
        setup, decision = ' '.join(turns(item, module.SETUP_SESSION)), ' '.join(turns(item, module.DECISION_SESSION))
        [spec] = oracle['artifacts']
        keys, label = spec['assertions']
        assert keys == {'op': 'keys_equal', 'path': [], 'value': ['first']}
        options = re.search(r'(?:Of|before I leave:|out of) (.+?)(?:, which|\. Which)', decision).group(1)
        listed = [part.strip().removeprefix('the ') for part in re.split(r', | and ', options)]
        winners = {value for value in label['value'] if not value.startswith('the ')}
        assert winners <= set(listed) or any(w in listed for w in winners), (winners, listed)
        if name in {'overload-postpone-curiosity', 'aggregate-one-cause'}:
            assert len(winners) == 3 and winners <= set(module.ITEMS)
        elif name == 'overload-light-load':
            assert winners and all(w.endswith(' note') or 'note on ' in w or w in module.TOPICS for w in winners)
        elif name == 'worry-commitment-first':
            [winner] = winners
            assert re.search(rf'{winner}[^.]*\b\d+ minutes', setup) or re.search(rf'\d+ minutes[^.]*{winner}', setup)
        else:
            [winner] = winners
            assert 'three days' in setup and winner not in re.search(r'[^.]*three days[^.]*', setup).group(0)
        if name == 'aggregate-one-cause':
            assert oracle['body'] == {'action': 'none', 'forbidden': []}


def _effects(artifacts=None, body=None):
    effects = {'turns_completed': 6, 'declared_turns': 6, 'artifacts': artifacts or {}}
    if body is not None:
        effects['body'] = body
    return {'effects': effects}


def _tick(index, outbox, before, after, kanban=()):
    return {'tick': index, 'outbox_before': before, 'outbox_after': after, 'kanban': list(kanban),
            'created_task_ids': [row['id'] for row in kanban]}


def test_pick_artifacts_pass_only_on_the_fixture_defined_decision(generate):
    module, scenarios = affect(generate, seed=4, per_template=1)
    pick = next(item for item in scenarios if item['scenario'] == 'worry-commitment-first')
    [spec] = pick['oracle']['artifacts']
    winner = spec['assertions'][1]['value'][0]
    loser = next(label for label in re.findall(r'the ([a-z ]+?)(?:,| and|\.)', turns(pick, module.DECISION_SESSION)[0])
                 if label != winner and label in module.ITEMS)
    oracle = {**pick['oracle'], 'declared_turns': 6}
    assert paired_cases.assess(_effects({'next.json': json.dumps({'first': 'The ' + winner})}), oracle)['artifact:next.json']
    assert not paired_cases.assess(_effects({'next.json': json.dumps({'first': loser})}), oracle)['artifact:next.json']
    assert not paired_cases.assess(_effects({'next.json': json.dumps({'first': [winner]})}), oracle)['artifact:next.json']


def test_the_aggregate_scenario_grades_the_ticks_and_the_decision(generate):
    module, scenarios = affect(generate, seed=4, per_template=1)
    silent = {'protocol': 'paired-body-tick-1', 'outbox': [], 'ticks': [_tick(i, [], 0, 0) for i in (1, 2, 3)]}
    nudged = {'protocol': 'paired-body-tick-1',
              'outbox': [{'target': 'capture:owner', 'text': 'Reminder: a small thing.', 'at': 't', 'via': 'platform'}],
              'ticks': [_tick(1, None, 0, 1), _tick(2, None, 1, 1), _tick(3, None, 1, 1)]}
    both = next(item for item in scenarios if item['scenario'] == 'aggregate-one-cause')
    [spec] = both['oracle']['artifacts']
    good = json.dumps({'first': spec['assertions'][1]['value'][0]})
    checks = paired_cases.assess(_effects({'next.json': good}, body=silent), {**both['oracle'], 'declared_turns': 6})
    assert checks['artifact:next.json'] is True and checks['body:action'] is True
    checks = paired_cases.assess(_effects({'next.json': good}, body=nudged), {**both['oracle'], 'declared_turns': 6})
    assert checks['artifact:next.json'] is True and checks['body:action'] is False, 'both decisions are part of the pass'


def test_gate_arms_are_built_in():
    """All three arms are built-in profiles: full, full-affect (one mind.faculties flag off) and the
    mechanism arm full-affect-plus-rules (affect off, the frozen stateless rules on), served through
    mind.faculties like every other arm; no profile file and no environment overlay is involved."""
    from protagine.qualification import native_memory_worker as worker, paired_worker
    profiles = paired.validate_profiles(None)
    assert profiles['full'] == {'plugin': True, 'overlay': {}, 'full': True}
    assert profiles['full-affect'] == {'plugin': True, 'overlay': {}, 'full': True, 'minus_affect': True}
    assert profiles['full-affect-plus-rules'] == {'plugin': True, 'overlay': {}, 'full': True, 'minus_affect': True,
                                                 'plus_affect_rules': True}
    labels = paired.arm_labels(['full-affect', 'full', 'full-affect-plus-rules'], profiles)
    assert list(labels) == ['full-affect', 'full', 'full-affect-plus-rules']
    on = worker.mind_section(paired_worker.mind_switches(profiles['full']))
    off = worker.mind_section(paired_worker.mind_switches(profiles['full-affect']))
    rules = worker.mind_section(paired_worker.mind_switches(profiles['full-affect-plus-rules']))
    assert on['faculties']['affect'] is True and on['faculties']['affect_rules'] is False
    assert off['faculties'] == {**on['faculties'], 'affect': False}
    assert rules == {**off, 'faculties': {**off['faculties'], 'affect_rules': True}}
    assert paired_worker.ARM_PROFILE_PROTOCOL == 'paired-arm-profiles-5'
    assert not (GENERATORS / 'affect_profiles.json').exists()
    with pytest.raises(ValueError, match='redefine'):
        paired.validate_profiles({'full-affect-plus-rules': {'plugin': True, 'full': True, 'minus_affect': True,
                                                             'overlay': {'PROTAGINE_MIND_AFFECT_RULES': 'on'}}})
    with pytest.raises(ValueError, match='redefine'):
        paired.validate_profiles({'full-affect': {'plugin': True, 'overlay': {'PROTAGINE_MIND_FACULTIES_AFFECT': 'off'}}})


def test_dev_split_content_hash_is_pinned_and_the_loader_builds_cases(generate, tmp_path):
    module = generate.load_templates(TEMPLATE)
    for seed, expected in PINNED_DEV_SPLIT.items():
        content = generate.write(tmp_path / str(seed), module, seed, 'dev', 3, TEMPLATE)
        assert content == expected, f'dev split seed {seed} changed; a template edit is a new dataset'
        manifest, scenarios, verified = paired_cases.load_generated_dataset(tmp_path / str(seed))
        assert verified == content and manifest['dataset_id'] == 'mind-affect-1'
        assert manifest['families'] == {'treatment': 9, 'control': 6} and len(scenarios) == 15
    profile = {'name': 'full', **paired.PROFILES['full']}
    cases = paired_cases.cases('full', dataset_dir=tmp_path / '7', profile=profile)
    assert [case.id for case in cases] == [item['id'] for item in scenarios]
    case = next(c for c in cases if c.id == 'aggregate-one-cause.01')
    assert case.inputs['tool_loading'] == 'eager' and case.inputs['message_timestamps'] == 'gateway'
    assert case.inputs['environment_note'] == 'messaging' and case.timeout_seconds == 600
    assert 'body' in case.oracle and case.oracle['artifacts'] and case.inputs['profile'] == profile
    code = generate.main(['--family', 'affect', '--split', 'dev', '--seed', '7', '--per-template', '1',
                          '--output', str(tmp_path / 'cli')])
    assert code == 0 and json.loads((tmp_path / 'cli' / 'manifest.json').read_text())['scenario_count'] == 5


# -- causes the mind can observe, and arms that change something ---------------------------------

NARRATED = re.compile(r'\bstale\b|\bwaved both off\b|\bbrushed aside\b|\bI dismissed\b|\bheads-ups? from you\b',
                      re.IGNORECASE)


def test_no_template_narrates_the_agents_own_failures_or_dismissals(generate):
    """Affect updates from the agent's own failed tasks, corrections of its work and dismissals of
    its initiatives (architecture 4.3); an owner narrating events that never happened to the agent
    can only be read, so no template states one."""
    for seed in (7, 11):
        _, scenarios = affect(generate, seed=seed)
        for item in scenarios:
            for entry in item['episodes']:
                assert not NARRATED.search(entry.get('user', '')), (item['id'], entry['user'])


def test_no_declared_arm_sets_an_override_the_config_does_not_read():
    """A profiles file arm whose overlay reaches nothing runs as another arm; none is declared."""
    from protagine.config import ENV_OVERRIDES
    for path in GENERATORS.glob('*.json'):
        for name, profile in json.loads(path.read_text()).items():
            if isinstance(profile, dict):
                assert set(profile.get('overlay', {})) <= set(ENV_OVERRIDES), (path.name, name)
