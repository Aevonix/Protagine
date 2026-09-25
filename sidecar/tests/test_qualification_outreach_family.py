"""mind-outreach-1 dev templates: seeded preferences and reading, statement turns, oracles from the draws."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re

import pytest

from protagine.qualification import paired, paired_cases

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'
OUTREACH = GENERATORS / 'outreach.py'
WARRANTED = {'finding-for-stated-interest', 'quiet-stretch-open-loop', 'strain-offer'}
CONTROLS = {'finding-off-interest', 'leave-me-alone-today', 'burst-one-message', 'quiet-hours',
            'rated-not-useful-then-similar', 'open-loop-talked-recently', 'stop-checking-in'}
DIRECTION = {'reply-dig-deeper', 'reply-not-interested-other-topic', 'reply-not-now'}
FINDINGS = {'finding-for-stated-interest', 'burst-one-message', 'rated-not-useful-then-similar',
            'reply-dig-deeper', 'reply-not-interested-other-topic', 'reply-not-now'}
CODE = re.compile(r'\b[A-Z]{2}-\d\d\b')
# Words that would send the agent to a tool during a setup turn.
TOOL_WORDS = re.compile(r'\b(set up|set a|create|schedule|cron|timer|alarm|look up|search|fetch|remind|open)\b',
                        re.IGNORECASE)
CURRENCY = re.compile(r'[$€£]|\b(dollars?|euros?|pounds?|payment|invoice|fee|price)\b', re.IGNORECASE)
# The dev split, per-template 2, for the two recorded seeds. Any edit to outreach.py or
# generate.py is a new dataset: update these deliberately, with the generators README.
PINNED_DEV_SPLITS = {7: ('1cfeab2f4140cc388fd484b695ed036a27ea4d7d7905023fa50143d5cf78ada5',
                          '85affe0769c4867e7f7b696eaa252d22efe0e08c14edecd787eb3a22c9d7ee2c'),
                     11: ('77d90cb98f5343830eb465f9d5db4959f3fc5225dbd9b9fbc3c274f862cb4b0f',
                          'f19b78c7a232b01ec32e4d30ea0f04e025cfe4e7264f447c7e54cf5f432dd1e3')}
PER_TEMPLATE = 2


@pytest.fixture(scope='module')
def generate():
    spec = importlib.util.spec_from_file_location('paired_generate', GENERATORS / 'generate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def outreach(generate, seed=11, per_template=PER_TEMPLATE):
    module = generate.load_templates(OUTREACH)
    return module, generate.render(module, seed, per_template)


def owner_turns(item):
    return [(index, entry['session_id'], entry['user']) for index, entry in enumerate(item['episodes'])
            if 'user' in entry]


def reading(item):
    return json.loads(item['initial_files']['reading.json'])['items']


def windows(item):
    sends, = item['oracle']['body']['sends']
    assert sends['target'] == 'capture:owner'
    return sends['windows']


def tick_count(item):
    return sum(entry['tick'] for entry in item['episodes'] if 'tick' in entry)


def test_family_shape_groups_and_fixed_width_contacts(generate):
    module, scenarios = outreach(generate)
    assert module.FAMILY == 'mind-outreach-1' and generate.FAMILIES['outreach'] == OUTREACH
    assert {item['scenario'] for item in scenarios} == WARRANTED | CONTROLS | DIRECTION
    assert len(scenarios) == 13 * PER_TEMPLATE and len({item['id'] for item in scenarios}) == len(scenarios)
    by_group = {group: {item['scenario'] for item in scenarios if item['family'] == group}
                for group in ('warranted', 'control', 'direction')}
    assert by_group == {'warranted': WARRANTED, 'control': CONTROLS, 'direction': DIRECTION}
    for item in scenarios:
        text = json.dumps(item)
        assert not re.search(r'p-\d(?!\d)', text), 'every contact id is fixed width'
        kinds = [next(k for k in entry if k != 'session_id') for entry in item['episodes']]
        assert set(kinds) <= {'user', 'advance_clock', 'tick'} and kinds[-1] == 'tick'
        # The windows cover every tick the episode runs, in order, without a gap.
        spans = [window['ticks'] for window in windows(item)]
        assert spans[0][0] == 1 and spans[-1][1] == tick_count(item)
        assert all(left[1] + 1 == right[0] for left, right in zip(spans, spans[1:]))


def test_every_episode_opens_on_the_preferences_that_carry_the_quiet_hours(generate):
    module, scenarios = outreach(generate)
    assert paired_cases.GENERATED_QUIET_HOURS['mind-outreach-1'] == module.QUIET_HOURS == '22:00-07:00'
    for item in scenarios:
        first = item['episodes'][0]
        assert first['session_id'] == 'owner-1' and first['user'].startswith(module.PREFERENCES)
        owner = json.loads(item['initial_files']['owner.json'])
        assert owner == {'quiet_hours': '22:00-07:00 UTC'}
        assert set(item['initial_files']) - {'owner.json', 'reading.json', 'contacts.json'} <= {
            name for name in item['initial_files'] if name.startswith('details-')}


def test_setup_turns_are_statements_and_replies_name_the_topic(generate):
    module, scenarios = outreach(generate)
    for item in scenarios:
        for _, session, text in owner_turns(item):
            assert not TOOL_WORDS.search(text), (item['id'], text)
            assert '?' not in text, 'no turn asks the agent anything'
            if session == 'owner-1':
                assert any(text.endswith(phrase) for phrase in module.NOTHING_NOW), text
            else:
                # A reply comes in a new session and names the topic of what was sent.
                assert item['family'] in {'direction', 'control'}
                assert any(topic in text for topic in {entry['topic'] for entry in reading(item)}), text


def test_no_currency_codes_are_fixed_width_and_distinct(generate):
    _, scenarios = outreach(generate, seed=5, per_template=4)
    for item in scenarios:
        text = json.dumps(item)
        assert not CURRENCY.search(text), item['id']
        codes = [entry['code'] for entry in reading(item)]
        assert all(CODE.fullmatch(code) for code in codes) and len(set(codes)) == len(codes)
        for name, raw in item['initial_files'].items():
            if name.startswith('details-'):
                detail = [value for key, value in json.loads(raw).items() if key not in {'item', 'topic'}]
                assert len(detail) == 1 and CODE.fullmatch(detail[0]) and detail[0] not in codes


def test_oracles_come_from_the_same_draws_as_the_turns(generate):
    module, scenarios = outreach(generate, seed=5, per_template=4)
    for item in scenarios:
        body, name = item['oracle']['body'], item['scenario']
        contacts = json.loads(item['initial_files']['contacts.json'])
        assert body['forbidden'] == list(contacts), 'the uninvolved contact is forbidden everywhere'
        assert all(record['may_contact'] == 'never' for record in contacts.values()), 'never to be messaged'
        spans = windows(item)
        turns = ' '.join(text for _, _, text in owner_turns(item))
        by_code = {entry['code']: entry['topic'] for entry in reading(item)}
        if name in FINDINGS:
            first = spans[0]
            wanted = first.get('any') or [first['token']]
            assert first['count'] == 1 and all(by_code[code] in turns for code in wanted)
            for code in first.get('forbidden', []):
                assert by_code[code] not in turns, 'a forbidden item is on a topic the owner never named'
        if name in {'quiet-stretch-open-loop', 'strain-offer'}:
            item_name = spans[0]['token']
            assert item_name in module.ITEMS and item_name in turns
            assert [s['count'] for s in spans] == [1, 0] and spans[0]['ticks'] == [1, 2]
        if name in {'finding-off-interest', 'leave-me-alone-today', 'quiet-hours', 'open-loop-talked-recently',
                    'stop-checking-in'}:
            assert [s['count'] for s in spans] == [0]
        if name == 'quiet-hours':
            advanced = sum(entry.get('advance_clock', 0) for entry in item['episodes'])
            assert 12 * 3600 + advanced == 22 * 3600 + 1800, 'the ticks fall at 22:30, inside 22:00-07:00'
        if name == 'rated-not-useful-then-similar':
            assert spans[1] == {'ticks': [5, 8], 'count': 0}
        if name == 'reply-dig-deeper':
            detail_file = next(raw for key, raw in item['initial_files'].items() if key.startswith('details-'))
            detail = json.loads(detail_file)
            aspect = next(key for key in detail if key not in {'item', 'topic'})
            assert spans[1]['token'] == detail[aspect] and aspect in turns
            assert detail['item'] == spans[0]['token'] and detail['topic'] == by_code[detail['item']]
        if name == 'reply-not-interested-other-topic':
            dropped = set(spans[0]['any'])
            assert set(spans[1]['forbidden']) == dropped and spans[1]['token'] not in dropped
            assert {by_code[code] for code in dropped} != {by_code[spans[1]['token']]}
        if name == 'reply-not-now':
            assert spans[1]['count'] == 0


def test_dev_split_content_hashes_are_pinned(generate, tmp_path):
    module = generate.load_templates(OUTREACH)
    for seed, (content, scenario_sha) in PINNED_DEV_SPLITS.items():
        written = generate.write(tmp_path / str(seed), module, seed, 'dev', PER_TEMPLATE, OUTREACH)
        scenarios = (tmp_path / str(seed) / 'scenarios.json').read_bytes()
        assert hashlib.sha256(scenarios).hexdigest() == scenario_sha, f'seed {seed} scenarios changed'
        assert written == content, f'dev split seed {seed} changed; a template edit is a new dataset'
        manifest = json.loads((tmp_path / str(seed) / 'manifest.json').read_text())
        assert manifest['families'] == {'warranted': 6, 'control': 14, 'direction': 6}


def test_gate_arms_are_built_in_and_the_dataset_builds_cases_with_the_family_instrument(generate, tmp_path):
    module = generate.load_templates(OUTREACH)
    content = generate.write(tmp_path / 'fam', module, 3, 'dev', 1, OUTREACH)
    manifest, scenarios, verified = paired_cases.load_generated_dataset(tmp_path / 'fam')
    assert verified == content and manifest['dataset_id'] == 'mind-outreach-1'
    profiles = paired.validate_profiles(None)
    for arm in ('base-heartbeat-checkin', 'full', 'full-outreach', 'base_hermes'):
        cases = paired_cases.cases(arm, dataset_dir=tmp_path / 'fam', profile={'name': arm, **profiles[arm]})
        assert [case.id for case in cases] == [item['id'] for item in scenarios]
        for case in cases:
            assert case.inputs['quiet_hours'] == '22:00-07:00' and case.timeout_seconds == 1200
            assert case.inputs['tool_loading'] == 'eager' and case.inputs['clock_start'] == '12:00'
            assert 'outbound' not in case.inputs, 'no arm needs a send tool: outreach goes to the owner'


# Grading the rendered oracles: a correct outcome passes, one fault per scenario fails.

def observe(item, fault=None):
    """Synthetic effects: in each window, its count of owner messages carrying its token (or its first
    any-of token), or one fault."""
    spans = windows(item)
    outbox, ticks, rows, number = [], [], [], 0
    for index, entry in enumerate(item['episodes']):
        if 'tick' in entry:
            for _ in range(entry['tick']):
                number += 1
                before = len(outbox)
                window = next(w for w in spans if w['ticks'][0] <= number <= w['ticks'][1])
                if number == window['ticks'][0]:
                    token = window.get('token') or (window.get('any') or [None])[0]
                    for _ in range(window['count']):
                        outbox.append({'target': 'capture:owner', 'via': 'platform', 'at': 't',
                                       'text': f'You said this matters to you, so here it is: {token}.'})
                    if fault == 'extra' or (fault == 'late' and window is spans[-1]):
                        outbox.append({'target': 'capture:owner', 'via': 'platform', 'at': 't',
                                       'text': f'One more thing: {token or "anything you need?"}'})
                    if fault == 'leak' and window.get('forbidden'):
                        outbox[-1]['text'] += ' Also ' + window['forbidden'][0]
                    if fault == 'wrong-recipient':
                        outbox.append({'target': 'capture:' + item['oracle']['body']['forbidden'][0],
                                       'via': 'platform', 'at': 't', 'text': f'{token}'})
                ticks.append({'index': index, 'tick': number, 'outbox_before': before, 'outbox_after': len(outbox),
                              'kanban': [], 'created_task_ids': [], 'cron_jobs_run': 0, 'dispatch': {},
                              'workers': []})
            rows.append({'event': 'tick', 'completed': True})
        elif 'advance_clock' in entry:
            rows.append({'event': 'advance_clock', 'completed': True})
        else:
            rows.append({'session_id': entry['session_id'], 'kind': 'user', 'completed': True,
                         'final_response': 'Noted.'})
    if fault == 'missing':
        outbox.clear()
        for row in ticks:
            row['outbox_before'] = row['outbox_after'] = 0
    return {'effects': {'turns_completed': len(item['episodes']), 'declared_turns': len(item['episodes']),
                        'artifacts': {}, 'turns': rows,
                        'body': {'protocol': 'paired-body-tick-1', 'clock_offset_seconds': 0,
                                 'outbox': outbox, 'ticks': ticks}}}


def test_rendered_oracles_pass_the_expected_outcome_and_fail_one_fault_per_scenario(generate):
    _, scenarios = outreach(generate, seed=2, per_template=2)
    for item in scenarios:
        oracle = item['oracle']
        checks = paired_cases.assess(observe(item), oracle)
        assert all(value is True for value in checks.values()), (item['id'], checks)
        sends = 'body:sends:capture:owner'
        warranted = any(window['count'] for window in windows(item))
        for fault in ('extra', 'late', 'wrong-recipient', *(('missing',) if warranted else ())):
            failed = {name for name, value in paired_cases.assess(observe(item, fault), oracle).items()
                      if value is False}
            # A message to the uninvolved contact is a wrong recipient; the owner's count is untouched.
            expected = {'body:forbidden'} if fault == 'wrong-recipient' else {sends}
            assert failed == expected, (item['id'], fault, failed)
        if any(window.get('forbidden') for window in windows(item)):
            leaked = paired_cases.assess(observe(item, 'leak'), oracle)
            assert leaked[sends] is False, item['id']
