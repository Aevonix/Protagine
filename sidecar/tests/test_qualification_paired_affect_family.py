"""The mind-affect-1 dev family: template contract, seeded state, oracles from draws, graders, arms."""
import importlib.util
import json
from pathlib import Path
import re

import pytest

from protagine.qualification import paired, paired_cases

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'
TEMPLATE = GENERATORS / 'affect.py'
TREATMENT = {'switch-recent-failures', 'overload-postpone-curiosity', 'worry-commitment-first',
             'satiation-hold-soft-nudge', 'aggregate-mixed-topics', 'aggregate-one-cause'}
CONTROLS = {'switch-old-failures', 'switch-recovered', 'switch-no-history', 'overload-light-load',
            'worry-nothing-due-soon', 'satiation-soft-nudge-fresh', 'satiation-duty-still-fires'}
TICK_GRADED = {'satiation-hold-soft-nudge', 'satiation-soft-nudge-fresh', 'satiation-duty-still-fires'}
CONSUMERS = {'strategy_switch', 'overload', 'priority', 'satiation', 'aggregate'}
# Words that would send the agent to a tool during a setup turn.
TOOL_WORDS = re.compile(r'\b(set up|set a|create|schedule|cron|timer|alarm|read|file|look up|search|check|fetch)\b',
                        re.IGNORECASE)
# The dev split, per-template 3, seed 7: the manifest hashes the template and engine sources, so
# any edit to affect.py or generate.py is a new dataset. Update deliberately, together with
# benchmarks/paired/generators/README.md and docs/proto-agi/families/mind-affect-1.md.
PINNED_DEV_SPLIT = {7: '518b0dedaa8042de85118c609aeb5d7ff586421d0f2dc59008b08895e338dbdc',
                    11: '0f5a9c90fff6ca3965194272913b1f56c805dd3eee451adcf1b440acb26baf5e'}


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
    assert len(scenarios) == 13 * 3 and len({item['id'] for item in scenarios}) == len(scenarios)
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
        if item['scenario'] in TICK_GRADED:
            assert not decision and item['episodes'][-1] == {'tick': 3} and 'body' in item['oracle']
            assert item['oracle']['artifacts'] == []
        else:
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
        exports = {name for name in files if name != 'contacts.json'}
        if item['scenario'].startswith('switch') or item['scenario'] == 'aggregate-mixed-topics':
            expected = 4 if item['scenario'] == 'aggregate-mixed-topics' else 2
            assert len(exports) == expected
            figures = {}
            for name in exports:
                stem, source = name[:-len('.json')].rsplit('-', 1)
                record = json.loads(files[name])
                assert source in module.SOURCES and record == {'item': stem.replace('-', ' '), 'figure': record['figure']}
                assert module.FIGURES[0] <= record['figure'] <= module.FIGURES[1]
                figures.setdefault(stem, set()).add(record['figure'])
                assert f'the {source} export' in ' '.join(turns(item, module.SETUP_SESSION)), name
            assert all(len(values) == 2 for values in figures.values()), 'the two exports never agree'
        else:
            assert not exports
        assert not any('inbound' in entry for entry in item['episodes'])


def test_oracles_come_from_the_same_draws_as_the_turns(generate):
    module, scenarios = affect(generate, seed=9)
    for item in scenarios:
        name, files, oracle = item['scenario'], item['initial_files'], item['oracle']
        setup, decision = ' '.join(turns(item, module.SETUP_SESSION)), ' '.join(turns(item, module.DECISION_SESSION))
        if name.startswith('switch'):
            [spec] = oracle['artifacts']
            keys, number, label = spec['assertions']
            assert keys == {'op': 'keys_equal', 'path': [], 'value': ['figure', 'source']}
            source = label['value'][0]
            usual = (re.search(r'usual export is the (\w+)', decision)
                     or re.search(r'comes from the (\w+) export', decision)
                     or re.search(r'the (\w+) export is the usual one', decision))
            assert usual, decision
            expected_source = 'fallback' if name == 'switch-recent-failures' else 'usual'
            assert (source != usual.group(1)) == (expected_source == 'fallback'), (name, source, usual.group(1))
            stem = spec['path'][:-len('.json')]
            assert number['value'] == json.loads(files[f'{stem}-{source}.json'])['figure']
            assert stem.replace('-', ' ') in setup and stem.replace('-', ' ') in decision
        elif name == 'aggregate-mixed-topics':
            [spec] = oracle['artifacts']
            keys, *rules = spec['assertions']
            slug1, slug2 = keys['value']
            for slug, prefix_rules in ((slug1, rules[:3]), (slug2, rules[3:])):
                _, number, label = prefix_rules
                source = label['value'][0]
                assert number['value'] == json.loads(files[f'{slug}-{source}.json'])['figure']
            assert re.search(rf'usual exports are the {re.escape(rules[2]["value"][0])}', decision) is None, \
                'the first figure switches away from its usual export'
            assert re.search(rf'the {re.escape(rules[5]["value"][0])} for the {slug2.replace("-", " ")}', decision) \
                or re.search(rf'usually the {re.escape(rules[5]["value"][0])} export', decision), \
                'the second figure keeps its usual export'
        elif name in TICK_GRADED:
            body = oracle['body']
            if name == 'satiation-hold-soft-nudge':
                assert body == {'action': 'none', 'forbidden': []}
            else:
                assert body['action']['target'] == 'capture:owner' and body['action']['window'] == [1, 2]
                assert body['action']['token'] in module.ITEMS and body['action']['token'] in setup
            clock = next(entry['advance_clock'] for entry in item['episodes'] if 'advance_clock' in entry)
            minutes = int(re.search(r'\b(\d+) minutes\b', setup).group(1))
            assert clock == minutes * 60 + module.PAST_HORIZON_SECONDS
            assert ('waved' in setup or 'brushed' in setup or 'dismissed' in setup) == (name != 'satiation-soft-nudge-fresh')
        else:
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


def test_figure_and_pick_artifacts_pass_only_on_the_fixture_defined_decision(generate):
    module, scenarios = affect(generate, seed=4, per_template=1)
    switch = next(item for item in scenarios if item['scenario'] == 'switch-recent-failures')
    oracle = {**switch['oracle'], 'declared_turns': 6}
    [spec] = oracle['artifacts']
    figure = spec['assertions'][1]['value']
    source = spec['assertions'][2]['value'][0]
    stale = next(json.loads(v)['figure'] for k, v in switch['initial_files'].items()
                 if k.endswith('.json') and k != 'contacts.json' and json.loads(v)['figure'] != figure)
    good = json.dumps({'figure': figure, 'source': source})
    checks = paired_cases.assess(_effects({spec['path']: good}), oracle)
    assert checks == {'all_native_turns_completed': True, 'artifact:' + spec['path']: True}
    for wrong in (json.dumps({'figure': stale, 'source': source}),          # the usual export's stale number
                  json.dumps({'figure': str(figure), 'source': source}),    # a string is not a number
                  json.dumps({'figure': figure}),                           # the decision is missing
                  json.dumps({'figure': figure, 'source': source, 'note': 'x'}),
                  json.dumps({'figure': figure, 'source': 'the other one'}),
                  '{"figure": %d, "source": "%s", "figure": %d}' % (figure, source, stale),
                  None):
        assert paired_cases.assess(_effects({spec['path']: wrong}), oracle)['artifact:' + spec['path']] is False, wrong
    assert paired_cases.assess(_effects({spec['path']: json.dumps({'figure': figure, 'source': source.upper() + ' '})}),
                               oracle)['artifact:' + spec['path']] is True, 'labels are case-insensitive and stripped'
    pick = next(item for item in scenarios if item['scenario'] == 'worry-commitment-first')
    [spec] = pick['oracle']['artifacts']
    winner = spec['assertions'][1]['value'][0]
    loser = next(label for label in re.findall(r'the ([a-z ]+?)(?:,| and|\.)', turns(pick, module.DECISION_SESSION)[0])
                 if label != winner and label in module.ITEMS)
    oracle = {**pick['oracle'], 'declared_turns': 6}
    assert paired_cases.assess(_effects({'next.json': json.dumps({'first': 'The ' + winner})}), oracle)['artifact:next.json']
    assert not paired_cases.assess(_effects({'next.json': json.dumps({'first': loser})}), oracle)['artifact:next.json']
    assert not paired_cases.assess(_effects({'next.json': json.dumps({'first': [winner]})}), oracle)['artifact:next.json']


def test_tick_graded_scenarios_use_the_body_oracle(generate):
    module, scenarios = affect(generate, seed=4, per_template=1)
    hold = next(item for item in scenarios if item['scenario'] == 'satiation-hold-soft-nudge')
    fresh = next(item for item in scenarios if item['scenario'] == 'satiation-soft-nudge-fresh')
    token = fresh['oracle']['body']['action']['token']
    silent = {'protocol': 'paired-body-tick-1', 'outbox': [], 'ticks': [_tick(i, [], 0, 0) for i in (1, 2, 3)]}
    nudged = {'protocol': 'paired-body-tick-1',
              'outbox': [{'target': 'capture:owner', 'text': f'Reminder: the {token}.', 'at': 't', 'via': 'platform'}],
              'ticks': [_tick(1, None, 0, 1), _tick(2, None, 1, 1), _tick(3, None, 1, 1)]}
    twice = {**nudged, 'outbox': nudged['outbox'] * 2, 'ticks': [_tick(1, None, 0, 1), _tick(2, None, 1, 2), _tick(3, None, 2, 2)]}
    for oracle, body, passes in ((hold['oracle'], silent, True), (hold['oracle'], nudged, False),
                                 (fresh['oracle'], nudged, True), (fresh['oracle'], silent, False),
                                 (fresh['oracle'], twice, False)):
        checks = paired_cases.assess(_effects(body=body), {**oracle, 'declared_turns': 6})
        assert checks['body:observed'] is True
        assert all(value is True for key, value in checks.items() if key.startswith('body:')) is passes, (checks, passes)
    both = next(item for item in scenarios if item['scenario'] == 'aggregate-one-cause')
    [spec] = both['oracle']['artifacts']
    good = json.dumps({'first': spec['assertions'][1]['value'][0]})
    checks = paired_cases.assess(_effects({'next.json': good}, body=silent), {**both['oracle'], 'declared_turns': 6})
    assert checks['artifact:next.json'] is True and checks['body:action'] is True
    checks = paired_cases.assess(_effects({'next.json': good}, body=nudged), {**both['oracle'], 'declared_turns': 6})
    assert checks['artifact:next.json'] is True and checks['body:action'] is False, 'both decisions are part of the pass'


def test_gate_arms_are_built_in_and_only_the_rules_arm_is_declared_in_a_file():
    """full and full-affect are built-in profiles (one mind.faculties flag apart); the mechanism
    arm keeps a profile file because mind.affect_rules does not exist yet, so its overlay reaches
    nothing and it runs as full-affect until M6 lands."""
    from protagine.qualification import native_memory_worker as worker, paired_worker
    declared = {name: value for name, value in
                json.loads((GENERATORS / 'affect_profiles.json').read_text()).items() if not name.startswith('_')}
    assert list(declared) == ['full-affect-plus-rules']
    profiles = paired.validate_profiles(declared)
    assert profiles['full'] == {'plugin': True, 'overlay': {}, 'full': True}
    assert profiles['full-affect'] == {'plugin': True, 'overlay': {}, 'full': True, 'minus_affect': True}
    assert profiles['full-affect-plus-rules'] == {'plugin': True, 'full': True, 'minus_affect': True,
                                                 'overlay': {'PROTAGINE_MIND_AFFECT_RULES': 'on'}}
    labels = paired.arm_labels(['full-affect', 'full', 'full-affect-plus-rules'], profiles)
    assert list(labels) == ['full-affect', 'full', 'full-affect-plus-rules']
    on = worker.mind_section(paired_worker.mind_switches(profiles['full']))
    off = worker.mind_section(paired_worker.mind_switches(profiles['full-affect']))
    rules = worker.mind_section(paired_worker.mind_switches(profiles['full-affect-plus-rules']))
    assert on['faculties']['affect'] is True and off['faculties'] == {**on['faculties'], 'affect': False}
    assert rules == off, 'the rules overlay is outside the mind section until the flag exists'
    with pytest.raises(ValueError, match='redefine'):
        paired.validate_profiles({'full-affect': {'plugin': True, 'overlay': {'PROTAGINE_MIND_FACULTIES_AFFECT': 'off'}}})


def test_dev_split_content_hash_is_pinned_and_the_loader_builds_cases(generate, tmp_path):
    module = generate.load_templates(TEMPLATE)
    for seed, expected in PINNED_DEV_SPLIT.items():
        content = generate.write(tmp_path / str(seed), module, seed, 'dev', 3, TEMPLATE)
        assert content == expected, f'dev split seed {seed} changed; a template edit is a new dataset'
        manifest, scenarios, verified = paired_cases.load_generated_dataset(tmp_path / str(seed))
        assert verified == content and manifest['dataset_id'] == 'mind-affect-1'
        assert manifest['families'] == {'treatment': 18, 'control': 21} and len(scenarios) == 39
    profile = {'name': 'full', **paired.PROFILES['full']}
    cases = paired_cases.cases('full', dataset_dir=tmp_path / '7', profile=profile)
    assert [case.id for case in cases] == [item['id'] for item in scenarios]
    case = next(c for c in cases if c.id == 'aggregate-one-cause.01')
    assert case.inputs['tool_loading'] == 'eager' and case.inputs['message_timestamps'] == 'gateway'
    assert case.inputs['environment_note'] == 'messaging' and case.timeout_seconds == 600
    assert 'body' in case.oracle and case.oracle['artifacts'] and case.inputs['profile'] == profile
    code = generate.main(['--family', 'affect', '--split', 'dev', '--seed', '7', '--per-template', '1',
                          '--output', str(tmp_path / 'cli')])
    assert code == 0 and json.loads((tmp_path / 'cli' / 'manifest.json').read_text())['scenario_count'] == 13
