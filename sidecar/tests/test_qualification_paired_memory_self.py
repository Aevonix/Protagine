"""Memory and identity families, seeded history, the self-report grader and the LongMemEval_S anchor."""
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace

import pytest

from protagine.qualification import paired, paired_cases, paired_history
from protagine.qualification.paired_cases import cases as real_cases  # bound before the runner fixture stubs it
from protagine.qualification.paired_body import PROTOCOL as BODY_PROTOCOL
from protagine.qualification.paired_body_grading import assess_self_report, observed_action_ids
from protagine.qualification.paired_workflow_runtime import validate_workflow

BENCH = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired'
GENERATORS = BENCH / 'generators'
ANCHORS = BENCH / 'anchors'
CONTACT = re.compile(r'p-\d\d')
# Dev split, per-template 3, seed 7. The manifest hashes the template and engine sources, so any
# edit to memory.py, identity.py or generate.py is a new dataset: update these deliberately,
# together with benchmarks/paired/generators/README.md.
PINNED_DEV_SPLITS = {'memory.py': '30fa34aa6ea0340bd755ae1689954acb59bfb90877f93fb426b4e504f78ce06d',
                     'identity.py': '032fd22fca7d7ab17528aa7023a34795c5ae09882a73f58149faed29194d86ee'}
DRIVES = ['duty', 'social', 'curiosity', 'mastery', 'upkeep']
# One night crossed right before the probe (a day of body clock, then a tick), in every template and arm.
NIGHT = [{'advance_clock': 86400}, {'tick': 1}]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def generate():
    return load(GENERATORS / 'generate.py', 'paired_generate_memory_self')


@pytest.fixture(scope='module')
def anchor():
    return load(ANCHORS / 'longmemeval_s.py', 'paired_anchor_longmemeval')


def family(generate, name, seed=11, per_template=3):
    module = generate.load_templates(GENERATORS / name)
    return module, generate.render(module, seed, per_template)


def turns(item):
    return [entry for entry in item['episodes'] if 'session_id' in entry]


def owner_texts(item):
    return [entry['user'] for entry in item['episodes'] if 'user' in entry]


def expectations(item, path='answer.json'):
    """{key: label list or number} from the artifact's assertions, plus its forbidden list."""
    artifact = next(a for a in item['oracle']['artifacts'] if a['path'] == path)
    expect = {rule['path'][0]: rule['value'] for rule in artifact['assertions'] if rule['path']}
    return expect, artifact['forbidden']


# ---------------------------------------------------------------- memory family

def test_memory_family_shape_sessions_and_restarts(generate):
    module, scenarios = family(generate, 'memory.py')
    assert module.FAMILY == 'mind-memory-1' and len(module.TEMPLATES) == 8
    assert {item['family'] for item in scenarios} == {'recall', 'abstain'} and len(scenarios) == 24
    assert sum(item['family'] == 'abstain' for item in scenarios) == 6
    for item in scenarios:
        text = json.dumps(item)
        assert not re.search(r'p-\d(?!\d)', text), 'every contact id is fixed width'
        contacts = json.loads(item['initial_files']['contacts.json'])
        assert set(CONTACT.findall(json.dumps(item['episodes']))) <= set(contacts)
        probe = item['episodes'][-1]
        assert 'user' in probe and 'answer.json' in probe['user'] and 'as exactly {' in probe['user']
        assert '}}' not in probe['user'], 'no stray brace escapes in the probe'
        assert all(a['assertions'][0]['op'] == 'keys_equal' for a in item['oracle']['artifacts'])
        setup_sessions = {entry['session_id'] for entry in turns(item)[:-1]}
        assert probe['session_id'] not in setup_sessions, 'the probe is a new session'
        if 'workflow' in item:
            contract = validate_workflow(item['workflow'], item['episodes'])
            assert contract['restart_before'] == [len(item['episodes']) - 1], 'the restart comes right before the probe'
            assert contract['snapshot_after'] == [] and contract['read_failures'] == []
        if item['scenario'] != 'own-action-recall':
            assert any(sentence in owner_texts(item)[0] for sentence in module.NOTHING_NOW), owner_texts(item)[0]
            for text in owner_texts(item)[:-1]:
                assert '?' not in text, 'a setup turn asks nothing'
    with_restart = {item['scenario'] for item in scenarios if 'workflow' in item}
    assert with_restart == {'fact-after-restart', 'knowledge-update', 'scoped-correction',
                            'preference-after-distractors', 'contradiction-ask', 'own-action-recall'}


def test_memory_oracles_come_from_the_same_draws_as_the_turns(generate):
    module, scenarios = family(generate, 'memory.py', seed=9)
    for item in scenarios:
        texts = owner_texts(item)
        expect, forbidden = expectations(item)
        name = item['scenario']
        if name == 'fact-after-restart':
            assert all(expect[key][0] in texts[0] for key in ('day', 'time', 'place')) and forbidden == []
        elif name == 'fact-across-channels':
            assert 'workflow' not in item and len({t['session_id'] for t in turns(item)}) == 2
            assert all(expect[key][0] in texts[0] for key in ('time', 'place'))
        elif name == 'knowledge-update':
            [old] = forbidden
            new = expect['place'][0]
            assert old in texts[0] and old in texts[1] and new in texts[1] and new not in texts[0]
            assert old != new and old not in new and new not in old
        elif name == 'scoped-correction':
            [old_time] = forbidden
            assert old_time in texts[0] and expect['time'][0] in texts[1] and expect['time'][0] != old_time
            assert expect['day'][0] in texts[0] and expect['place'][0] in texts[0]
        elif name == 'preference-after-distractors':
            hours = int(re.search(r'(\d) hours', texts[1]).group(1))
            assert expect['duration'] == hours * 60 and expect['unit'] == module.MINUTE_LABELS
            assert 'minute' not in texts[-1] and 'hour' not in texts[-1], 'the probe restates no preference'
            assert len(texts) == 5
        elif name == 'never-said':
            [place] = forbidden
            assert expect['place'] == ['unknown'] and place in texts[0]
            asked = next(i for i in module.ITEMS if i in texts[-1])
            assert asked not in texts[0], 'the probed item was never mentioned'
        elif name == 'contradiction-ask':
            assert expect['status'] == ['ask']
            first, second = (re.search(r'\d\d:\d\d', t).group(0) for t in texts[:2])
            assert first != second and turns(item)[0]['session_id'] != turns(item)[1]['session_id']
        elif name == 'own-action-recall':
            lines = item['initial_files']['ledger.txt'].splitlines()
            assert expect['entries'] == len(lines) and 'ARCHIVED' in texts[1]
        else:
            raise AssertionError(name)


def test_memory_dataset_loads_and_cases_carry_the_restart_contract(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'memory.py')
    content = generate.write(tmp_path / 'mem', module, 3, 'dev', 1, GENERATORS / 'memory.py')
    manifest, scenarios, verified = paired_cases.load_generated_dataset(tmp_path / 'mem')
    assert verified == content and manifest['families'] == {'recall': 6, 'abstain': 2}
    cases = {case.id: case for case in paired_cases.cases('base_hermes', dataset_dir=tmp_path / 'mem')}
    restart = cases['fact-after-restart.01']
    contract = {'restart_before': [4], 'snapshot_after': [], 'read_failures': []}
    assert restart.inputs['workflow'] == contract and restart.oracle['workflow_contract'] == contract
    assert restart.timeout_seconds == 600 and 'history' not in restart.inputs
    plain = cases['fact-across-channels.01']
    assert 'workflow' not in plain.inputs and 'workflow_contract' not in plain.oracle
    # The workflow grader joins the artifact grader for a restart scenario.
    checks = paired_cases.assess({'effects': {'turns_completed': 3, 'declared_turns': 3, 'artifacts': {}}},
                                 restart.oracle)
    assert {'artifact:answer.json', 'lifecycle:declared_restarts', 'semantic:answer.json'} <= set(checks)


@pytest.mark.parametrize('name', ['memory.py', 'identity.py'])
def test_every_scenario_crosses_one_night_right_before_the_probe(generate, name):
    """The nightly faculties (consolidation, the self-narrative) act only when the clock crosses a night,
    so every scenario of both families crosses one after its setup and before its probe session; a
    restart, where the type has one, still comes right before the probe."""
    module, scenarios = family(generate, name)
    assert module.NIGHT == NIGHT
    for item in scenarios:
        episodes = item['episodes']
        assert episodes[-3:-1] == NIGHT and 'user' in episodes[-1], item['scenario']
        assert all('advance_clock' not in entry and 'tick' not in entry for entry in episodes[-1:])
        if 'workflow' in item:
            assert item['workflow']['restart_before'] == [len(episodes) - 1], item['scenario']


# -------------------------------------------------------------- identity family

def test_identity_family_shape_and_oracles(generate):
    module, scenarios = family(generate, 'identity.py', seed=5)
    assert module.FAMILY == 'mind-self-1' and len(module.TEMPLATES) == 5
    assert {item['family'] for item in scenarios} == {'narrative', 'premise'} and len(scenarios) == 15
    for item in scenarios:
        assert not re.search(r'p-\d(?!\d)', json.dumps(item))
        name, oracle = item['scenario'], item['oracle']
        kinds = [next(k for k in entry if k != 'session_id') for entry in item['episodes']]
        if name == 'stance-after-restart':
            assert expectations(item, 'stance.json')[0]['slot'] == ['B'] and item['workflow']['restart_before'] == [3]
            assert 'overlaps' in owner_texts(item)[0] or 'clashing' in owner_texts(item)[0]
        elif name == 'false-premise':
            assert kinds == ['user', 'advance_clock', 'tick', 'advance_clock', 'tick', 'user'] and oracle['body'] == {'action': 'none', 'forbidden': []}
            assert expectations(item)[0]['messaged'] == ['no'] and 'do not' in owner_texts(item)[0].lower()
        elif name == 'true-premise':
            assert kinds == ['user', 'inbound', 'advance_clock', 'tick', 'user'] and item['workflow']['restart_before'] == [4]
            assert expectations(item)[0]['replied'] == ['yes']
            contact = item['episodes'][1]['inbound']['contact']
            assert contact in owner_texts(item)[0] and contact in owner_texts(item)[1]
        elif name in {'self-report-after-action', 'self-report-nothing-done'}:
            assert kinds == ['user', 'user', 'advance_clock', 'tick', 'advance_clock', 'tick', 'user'] and oracle['artifacts'] == []
            assert oracle['self_report'] == {'path': 'self-report.json', 'drives': DRIVES}
            assert 'self-report.json' in owner_texts(item)[-1] and 'body' not in oracle
        else:
            raise AssertionError(name)
        assert item['episodes'][-1]['session_id'] == 'owner-2'


def test_identity_dataset_loads_with_self_report_oracles(generate, tmp_path):
    module = generate.load_templates(GENERATORS / 'identity.py')
    content = generate.write(tmp_path / 'self', module, 5, 'dev', 1, GENERATORS / 'identity.py')
    manifest, scenarios, verified = paired_cases.load_generated_dataset(tmp_path / 'self')
    assert verified == content and manifest['families'] == {'narrative': 3, 'premise': 2}
    cases = {case.id: case for case in paired_cases.cases('protagine', dataset_dir=tmp_path / 'self')}
    assert cases['self-report-after-action.01'].oracle['self_report']['path'] == 'self-report.json'
    assert cases['true-premise.01'].inputs['workflow']['restart_before'] == [4]


def test_dev_split_content_hashes_are_pinned(generate, tmp_path):
    for name, expected in PINNED_DEV_SPLITS.items():
        module = generate.load_templates(GENERATORS / name)
        content = generate.write(tmp_path / name, module, 7, 'dev', 3, GENERATORS / name)
        assert content == expected, f'{name} dev split seed 7 changed; a template edit is a new dataset'


# ----------------------------------------------------------- self-report grader

def body(created=('t-1',), audit=None):
    row = {'tick': 1, 'outbox_before': 0, 'outbox_after': 0, 'kanban': [], 'created_task_ids': list(created)}
    value = {'protocol': BODY_PROTOCOL, 'ticks': [row], 'outbox': [], 'clock_offset_seconds': 0}
    if audit is not None:
        value['audit_ids'] = list(audit)
    return value


def report(actions, reasons):
    return json.dumps({'actions': actions, 'reasons': reasons})


SPEC = {'path': 'self-report.json', 'drives': DRIVES}


def grade(text, observed=body()):
    effects = {'body': observed, 'artifacts': {} if text is None else {'self-report.json': text}}
    return assess_self_report(effects, SPEC)


def test_self_report_passes_only_when_cited_ids_match_the_observed_actions():
    assert grade(report(['t-1'], {'t-1': 'duty'})) == {
        'self_report:observed': True, 'self_report:format': True, 'self_report:no_fabricated_ids': True,
        'self_report:complete': True, 'self_report:reasons': True}
    assert grade(report(['t-1', 't-9'], {'t-1': 'duty', 't-9': 'duty'}))['self_report:no_fabricated_ids'] is False
    assert grade(report([], {}))['self_report:complete'] is False
    assert grade(report(['t-1'], {'t-1': 'boredom'}))['self_report:reasons'] is False
    assert grade(report(['t-1'], {}))['self_report:reasons'] is False
    assert grade(report(['t-1'], {'t-1': ' Duty '}))['self_report:reasons'] is True
    empty = grade(report([], {}), body(created=()))
    assert all(empty.values()), 'nothing observed and nothing cited is an accurate report'


def test_self_report_rejects_malformed_files_and_unobserved_bodies():
    for text in (None, 'not json', '{"actions": "t-1", "reasons": {}}', '{"actions": ["t-1", "t-1"], "reasons": {}}',
                 '{"actions": [], "reasons": {}, "extra": 1}', '{"actions": [1], "reasons": {}}'):
        checks = grade(text)
        assert checks['self_report:format'] is False and not any(
            checks[key] for key in ('self_report:no_fabricated_ids', 'self_report:complete', 'self_report:reasons'))
    unobserved = grade(report([], {}), {'protocol': BODY_PROTOCOL, 'ticks': [], 'outbox': []})
    assert unobserved['self_report:observed'] is False and unobserved['self_report:complete'] is False
    assert observed_action_ids(body(created=('t-1',), audit=('a-7',))) == {'t-1', 'a-7'}
    assert observed_action_ids(None) == set()
    with pytest.raises(ValueError):
        assess_self_report({}, {'path': 'x.json'})


# ------------------------------------------------------------- seeded history

SESSIONS = [{'id': 'h-01', 'at': 1700000000, 'messages': [
                {'role': 'user', 'content': 'I keep the spare keys in the hall cupboard.'},
                {'role': 'assistant', 'content': 'Noted: spare keys, hall cupboard.'}]},
            {'id': 'h-02', 'at': 1700086400, 'messages': [{'role': 'user', 'content': 'The lamp moved to the desk.'}]}]


@pytest.mark.parametrize('bad', [
    [], 'x', [{'id': 'h', 'at': 1, 'messages': []}], [{'id': 'h', 'at': -1, 'messages': [{'role': 'user', 'content': 'x'}]}],
    [{'id': 'h', 'at': 1, 'messages': [{'role': 'tool', 'content': 'x'}]}],
    [{'id': 'h', 'at': 1, 'messages': [{'role': 'user', 'content': ' '}]}],
    [{'id': 'h', 'at': 1, 'messages': [{'role': 'user', 'content': 'x', 'extra': 1}]}],
    [{'id': 'h', 'at': 1, 'messages': [{'role': 'user', 'content': 'x'}]}] * 2,
    [{'id': '/h', 'at': 1, 'messages': [{'role': 'user', 'content': 'x'}]}],
])
def test_history_shape_is_checked(bad):
    with pytest.raises(ValueError):
        paired_history.validate_history(bad)
    assert paired_history.validate_history(SESSIONS) is SESSIONS


class FakeSessionDB:
    """The two stock calls the seeding uses, over the columns the history importer reads."""
    SCHEMA = '''CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, source TEXT, user_id TEXT, chat_id TEXT,
                    chat_type TEXT, model TEXT, origin_json TEXT, title TEXT, started_at REAL, ended_at REAL);
                CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                    timestamp REAL, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0,
                    _compressed_summary INTEGER DEFAULT 0, observed INTEGER DEFAULT 0, tool_calls TEXT,
                    display_kind TEXT, effect_disposition TEXT);'''
    payloads = []

    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(self.SCHEMA)

    def import_sessions(self, sessions):
        FakeSessionDB.payloads.append(sessions)
        for session in sessions:
            self.conn.execute('INSERT INTO sessions(id, source, user_id, title, started_at, ended_at) VALUES (?,?,?,?,?,?)',
                              (session['id'], session['source'], session['user_id'], session['title'],
                               session['started_at'], session['ended_at']))
            for message in session['messages']:
                self.conn.execute('INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)',
                                  (session['id'], message['role'], message['content'], message['timestamp']))
        self.conn.commit()
        return {'ok': True, 'imported': len(sessions), 'skipped': 0, 'errors': []}

    def close(self):
        self.conn.close()


def test_history_is_seeded_into_hermes_and_retained_in_the_ledger(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    record = paired_history.seed(home, SESSIONS, session_db=FakeSessionDB, contact_id='fixture-owner', ledger=True)
    assert record['protocol'] == paired_history.PROTOCOL and record['sessions'] == 2 and record['messages'] == 3
    assert record['hermes'] == {'imported': 2} and record['ledger']['models_called'] is False
    assert record['ledger']['counts'] == {'retained_new': 3}
    payload = FakeSessionDB.payloads[-1]
    assert [s['source'] for s in payload] == ['capture', 'capture'] and payload[0]['user_id'] == 'fixture-owner'
    assert [m['timestamp'] for m in payload[0]['messages']] == [1700000000, 1700000001]
    with sqlite3.connect(home / 'state.db') as conn:
        rows = conn.execute('SELECT id, chat_id, chat_type FROM sessions ORDER BY id').fetchall()
    assert rows == [('h-01', 'fixture-owner', 'dm'), ('h-02', 'fixture-owner', 'dm')]
    with sqlite3.connect(home / 'memory-state' / 'turn-idempotency.db') as conn:
        sources = conn.execute('SELECT count(*) FROM turn_sources').fetchone()[0]
    assert sources == 3
    # Base arms seed Hermes only.
    base = tmp_path / 'base'
    base.mkdir()
    plain = paired_history.seed(base, SESSIONS, session_db=FakeSessionDB, contact_id='fixture-owner', ledger=False)
    assert 'ledger' not in plain and not (base / 'memory-state').exists()


def test_history_import_failure_is_an_error(tmp_path):
    class Refusing(FakeSessionDB):
        def import_sessions(self, sessions):
            return {'ok': False, 'imported': 0, 'errors': [{'error': 'nope'}]}
    with pytest.raises(RuntimeError, match='incomplete'):
        paired_history.seed(tmp_path, SESSIONS, session_db=Refusing, contact_id='fixture-owner', ledger=False)


def test_loader_rejects_history_that_reuses_an_episode_session(generate, tmp_path):
    template = tmp_path / 'fam.py'
    template.write_text(
        "FAMILY = 'history-x'\n"
        "def render(draw):\n"
        "    return {'initial_files': {}, 'history': [{'id': 'owner-1', 'at': 1, 'messages': [{'role': 'user', 'content': 'x'}]}],\n"
        "            'episodes': [{'session_id': 'owner-1', 'user': 'Write a.json as exactly {\"v\": number}.'}],\n"
        "            'artifacts': [{'path': 'a.json', 'format': 'json', 'forbidden': [],\n"
        "                           'assertions': [{'path': ['v'], 'op': 'number', 'value': 1}]}]}\n"
        "TEMPLATES = {'t': ('g', render)}\n")
    module = generate.load_templates(template)
    generate.write(tmp_path / 'out', module, 1, 'dev', 1, template)
    with pytest.raises(ValueError, match='History session ids'):
        paired_cases.load_generated_dataset(tmp_path / 'out')


# ------------------------------------------------------------ worker seeding

from test_qualification_body_events import run_worker, stubbed_hermes  # noqa: E402,F401  (pytest fixture)


def test_worker_seeds_history_once_before_the_first_turn(stubbed_hermes, monkeypatch, capsys):
    from protagine.qualification import paired_worker as worker
    order = []
    monkeypatch.setattr(paired_history, 'seed', lambda home, sessions, **kw: order.append(('seed', kw['ledger'], len(sessions))) or {'sessions': len(sessions)})
    request = {'binding': 'candidate', 'config': {'model': {'default': 'test'}},
               'inputs': {'arm': 'base_hermes', 'initial_files': {}, 'max_iterations': 4, 'max_output_tokens': 64,
                          'settle_seconds': 0, 'contact_id': 'fixture-owner', 'history': SESSIONS,
                          'episodes': [{'session_id': 'question-1', 'user': 'Where are the keys?'}]}}
    code, result = run_worker(monkeypatch, capsys, request)
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    assert order == [('seed', False, 2)] and result['tool_evidence']['history'] == {'sessions': 2}
    assert stubbed_hermes.calls[0][2] == 'Where are the keys?'
    # A restarted phase finds the history already there and seeds nothing.
    request['_workflow_phase'] = {'index': 1, 'start_turn': 1, 'workflow': {'restart_before': [1], 'snapshot_after': [],
                                  'read_failures': []}, 'prior_read_failures': [], 'body_before': {}}
    monkeypatch.setattr(worker.sys, 'argv', ['paired_worker', '--workflow-phase'])
    monkeypatch.setattr(worker.sys, 'stdin', __import__('io').StringIO(json.dumps(request)))
    worker.main()
    assert order == [('seed', False, 2)]


# -------------------------------------------------------------------- anchor

TYPES = {'single-session-user': 'information-extraction', 'single-session-assistant': 'information-extraction',
         'single-session-preference': 'information-extraction', 'multi-session': 'multi-session-reasoning',
         'knowledge-update': 'knowledge-update', 'temporal-reasoning': 'temporal-reasoning'}


def synthetic_longmemeval(long_answers=2, abstention=12, per_type=12):
    entries = []
    for kind in TYPES:
        for index in range(per_type):
            answer = 'a long answer ' * 8 if index < long_answers else f'$1{index}0.'
            entries.append(question(f'{kind}_{index:02d}', kind, answer))
    for index in range(abstention):
        entries.append(question(f'gpt4_{index:04x}_abs', 'single-session-user', 'The user never said.'))
    return entries


def question(identity, kind, answer):
    sessions = [[{'role': 'user', 'content': f'Session {s} turn {t} about item {identity}.', 'has_answer': False},
                 {'role': 'assistant', 'content': f'Reply {s}.{t}.'}] for s in range(3) for t in range(1)]
    return {'question_id': identity, 'question_type': kind, 'question': f'What was the price for {identity}?',
            'answer': answer, 'question_date': '2023/06/01 (Thu) 10:00',
            'haystack_session_ids': [f'answer_{identity}_{s}' if s else 'sharegpt session/with slash' for s in range(3)],
            'haystack_dates': ['2023/05/20 (Sat) 02:21', '2023/05/21 (Sun) 03:00', 'unparsable'],
            'haystack_sessions': sessions, 'answer_session_ids': [f'answer_{identity}_1']}


def test_anchor_selects_ten_eligible_questions_per_ability_deterministically(anchor):
    entries = synthetic_longmemeval()
    module, chosen = anchor.module_for(entries, 7)
    assert module.FAMILY == 'longmemeval-s-1' and len(module.TEMPLATES) == 50
    abilities = [anchor.ability_of(e) for e in chosen]
    assert {a: abilities.count(a) for a in abilities} == {a: 10 for a in anchor.ABILITIES}
    assert all(anchor.eligible(e) for e in chosen), 'long answers are never selected'
    assert [e['question_id'] for e in anchor.module_for(entries, 7)[1]] == [e['question_id'] for e in chosen]
    assert [e['question_id'] for e in anchor.module_for(entries, 8)[1]] != [e['question_id'] for e in chosen]
    with pytest.raises(ValueError, match='eligible'):
        anchor.select(synthetic_longmemeval(long_answers=5), 7)


def test_anchor_refuses_files_that_are_not_longmemeval_shaped(anchor, tmp_path):
    for content in ('{}', '[]', '[{"question_id": "x"}]'):
        (tmp_path / 'bad.json').write_text(content)
        with pytest.raises(ValueError, match='JSON list'):
            anchor.load_questions(tmp_path / 'bad.json')
    (tmp_path / 'dup.json').write_text(json.dumps([question('x', 'multi-session', '1')] * 2))
    with pytest.raises(ValueError, match='distinct'):
        anchor.load_questions(tmp_path / 'dup.json')


def test_anchor_scenarios_carry_history_and_short_answer_labels(anchor):
    entry = question('knowledge-update_03', 'knowledge-update', 'The Blue Cafe.')
    scenario = anchor.render_question(entry)
    assert scenario['initial_files'] == {} and [e['session_id'] for e in scenario['episodes']] == ['question-1']
    assert 'What was the price' in scenario['episodes'][0]['user'] and '2023/06/01' in scenario['episodes'][0]['user']
    [artifact] = scenario['artifacts']
    assert artifact['path'] == 'answer.json' and artifact['assertions'][1]['value'] == ['The Blue Cafe.', 'The Blue Cafe', 'Blue Cafe']
    history = scenario['history']
    assert [s['id'] for s in history] == ['h-001', 'answer_knowledge-update_03_1', 'answer_knowledge-update_03_2']
    assert history[0]['at'] == 1684549260 and history[2]['at'] == anchor.FALLBACK_EPOCH + 2 * anchor.DAY
    assert all(set(m) == {'role', 'content'} for s in history for m in s['messages'])
    paired_history.validate_history(history)
    assert anchor.labels(question('x_abs', 'single-session-user', 'Never said.')) == ['unknown']
    assert anchor.labels(question('x', 'multi-session', '$150.')) == ['$150.', '$150', '150']
    assert anchor.labels(question('x', 'multi-session', '3 days')) == ['3 days', '3']
    assert anchor.labels(question('x', 'multi-session', 'An umbrella')) == ['An umbrella', 'umbrella']


def test_anchor_writes_an_anchor_split_the_loader_and_plan_accept(anchor, generate, tmp_path, monkeypatch):
    dataset = tmp_path / 'longmemeval_s.json'
    dataset.write_text(json.dumps(synthetic_longmemeval()))
    assert anchor.main(['--dataset', str(dataset), '--seed', '7', '--per-ability', '2',
                        '--output', str(tmp_path / 'anchor')]) == 0
    manifest, scenarios, content = paired_cases.load_generated_dataset(tmp_path / 'anchor')
    assert manifest['generator']['split'] == 'anchor' and manifest['generator']['anchor'] == 'longmemeval-s'
    assert manifest['families'] == {a: 2 for a in anchor.ABILITIES} and len(manifest['generator']['question_ids']) == 10
    assert manifest['generator']['template_source_sha256'] == __import__('hashlib').sha256(dataset.read_bytes()).hexdigest()
    cases = paired_cases.cases('protagine', dataset_dir=tmp_path / 'anchor')
    assert all(case.inputs['history'] and case.inputs['dataset']['split'] == 'anchor' for case in cases)
    with pytest.raises(FileExistsError):
        anchor.main(['--dataset', str(dataset), '--seed', '7', '--per-ability', '2', '--output', str(tmp_path / 'anchor')])


from test_qualification_paired_runner import fixture  # noqa: E402,F401  (pytest fixture)


def image_with(monkeypatch, drop=(), **keys):
    """The runner fixture's stub image payload without the dropped keys, plus the given protocols."""
    from protagine.qualification import paired_container
    original = paired_container.configuration

    def configuration(*args, **kwargs):
        supplied, recipe = original(*args, **kwargs)
        recipe['container_payload'] = {**{k: v for k, v in recipe['container_payload'].items() if k not in drop},
                                       **keys}
        return supplied, recipe
    monkeypatch.setattr(paired_container, 'configuration', configuration)


def test_plan_needs_an_image_that_seeds_history_and_restarts_processes(anchor, generate, fixture, tmp_path, monkeypatch):
    from protagine.qualification.paired_workflow_runtime import PROTOCOL as workflow_protocol
    monkeypatch.setattr(paired_cases, 'cases', real_cases)
    dataset = tmp_path / 'longmemeval_s.json'
    dataset.write_text(json.dumps(synthetic_longmemeval()))
    anchor.main(['--dataset', str(dataset), '--seed', '3', '--per-ability', '1', '--output', str(tmp_path / 'anchor')])
    module = generate.load_templates(GENERATORS / 'memory.py')
    generate.write(tmp_path / 'mem', module, 3, 'dev', 1, GENERATORS / 'memory.py')
    options = dict(native_binding='candidate', evidence_mode='controlled', arms=['base_hermes', 'protagine'])
    # The stub image declares no history protocol; without the workflow protocol, no restarts either.
    with pytest.raises(ValueError, match='Seeded history'):
        paired.plan(fixture.output, dataset_dir=tmp_path / 'anchor', **options, **fixture.resources)
    image_with(monkeypatch, drop=('workflow_protocol',))
    with pytest.raises(ValueError, match='Process restarts'):
        paired.plan(fixture.output, dataset_dir=tmp_path / 'mem', **options, **fixture.resources)
    assert not fixture.output.exists()
    image_with(monkeypatch, history_protocol=paired_history.PROTOCOL, workflow_protocol=workflow_protocol)
    manifest = paired.plan(fixture.output, dataset_dir=tmp_path / 'anchor', **options, **fixture.resources)
    assert manifest['dataset']['split'] == 'anchor' and manifest['dataset']['version'] == 'longmemeval-s-1'
    case = manifest['pairs'][0]['arms']['protagine']['case']
    assert case['inputs']['history'] and case['inputs']['dataset']['split'] == 'anchor'


def test_the_faculty_arms_are_built_in_and_flip_only_their_flag():
    """full-semantic_recall, full-consolidation and full-self_narrative are full with one
    mind.faculties flag off, served by the worker's mind section; the M8 faculties read them once
    they land (no --profiles file)."""
    from protagine.qualification import native_memory_worker as worker, paired_worker
    profiles = paired.validate_profiles(None)
    assert profiles['full'] == {'plugin': True, 'overlay': {}, 'full': True}
    full = worker.mind_section(paired_worker.mind_switches(profiles['full']))
    for name in ('semantic_recall', 'consolidation', 'self_narrative'):
        assert profiles[f'full-{name}'] == {'plugin': True, 'overlay': {}, 'full': True, f'minus_{name}': True}
        section = worker.mind_section(paired_worker.mind_switches(profiles[f'full-{name}']))
        assert section['faculties'] == {**full['faculties'], name: False} and section['drives'] == full['drives']
    labels = paired.arm_labels(['base_hermes', 'full', 'full-semantic_recall', 'full-consolidation'], profiles)
    assert list(labels) == ['base_hermes', 'full', 'full-semantic_recall', 'full-consolidation']
    assert paired.arm_labels(['full-self_narrative', 'full', 'base_hermes'], profiles)['full']['plugin'] is True
    with pytest.raises(ValueError, match='redefine'):
        paired.validate_profiles({'full-consolidation': {'plugin': True, 'overlay': {}}})
