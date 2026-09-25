"""The people family's instrument (families/mind-people-1.md section 7): the plugin arm's people
store is seeded from the same ``contacts.json`` every arm reads, an inbound session carries its
sender so the adapter resolves the contact, the outbound path is declared in the plan, and the
``full`` and ``full-people`` arms differ in the one flag the mind reads."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from protagine.initiatives.store import InitiativeStore
from protagine.mind import Mind
from protagine.qualification import native_memory_worker as worker
from protagine.qualification import paired, paired_cases, paired_container, paired_worker
from protagine.qualification.paired_cases import cases as real_cases
from test_qualification_body_events import INBOUND, TICK, USER, run_worker, stubbed_hermes  # noqa: F401
from test_qualification_paired_runner import fixture  # noqa: F401  (pytest fixture)

# These drive the paired worker in-process, and the worker runs inside Hermes. The sidecar's own test run has
# no Hermes and skips them; CI runs them in a second step with stock Hermes installed.
needs_hermes = pytest.mark.skipif(importlib.util.find_spec("hermes_time") is None,
                                  reason="needs stock Hermes in the test interpreter")


GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'

CONTACTS = {'p-02': {'channel': 'chat', 'address': 'capture:p-02', 'may_contact': 'auto', 'cadence_minutes': 12},
            'p-03': {'channel': 'sms', 'address': 'capture:p-03', 'may_contact': 'never', 'name': 'Sam'},
            'p-04': {'channel': 'email', 'address': 'capture:p-04'}}


def test_contacts_json_is_seeded_into_the_people_store_as_capture_handles():
    records = paired_worker.people_records({'contacts.json': json.dumps(CONTACTS)})
    assert list(records) == ['p-02', 'p-03', 'p-04']
    posted = []

    def post(path, body):
        posted.append((path, body))
        return {'contact_id': 'cid-' + body['display_name']}
    seeded = paired_worker.seed_people(records, post)
    assert [path for path, _ in posted] == ['/v1/host/contacts'] * 3
    assert posted[0][1] == {'display_name': 'p-02', 'trust_tier': 'regular', 'may_contact': 'auto',
                            'cadence_minutes': 12, 'notes': 'seeded from contacts.json',
                            'handles': [{'gateway': 'capture', 'address': 'p-02', 'is_primary': True, 'verified': True}]}
    assert posted[1][1]['display_name'] == 'Sam' and posted[1][1]['may_contact'] == 'never'
    assert posted[1][1]['cadence_minutes'] is None and posted[1][1]['handles'][0]['address'] == 'p-03'
    assert posted[2][1]['may_contact'] == 'ask'                       # the default permission
    assert seeded == {'p-02': 'cid-p-02', 'p-03': 'cid-Sam', 'p-04': 'cid-p-04'}
    # No file, an unreadable file, a record without a capture address: nothing is seeded, nothing raised.
    assert paired_worker.people_records({}) == {} and paired_worker.people_records({'contacts.json': 'nope'}) == {}
    assert paired_worker.seed_people({'p-05': {'channel': 'chat', 'address': 'p-05'}}, post) == {}
    assert len(posted) == 3


def test_a_failed_seed_is_reported_not_raised():
    def post(path, body):
        raise OSError('sidecar unreachable')
    assert paired_worker.seed_people(paired_worker.people_records({'contacts.json': json.dumps(CONTACTS)}), post) == {}


def test_an_inbound_session_carries_its_sender_and_an_owner_session_does_not():
    agent = SimpleNamespace()
    inbound = {'session_id': 'contact-1', 'inbound': {'contact': 'p-07', 'channel': 'chat', 'text': 'hello'}}
    assert paired_worker.bind_sender(agent, inbound) == 'p-07' and agent._user_id == 'p-07'
    owner = SimpleNamespace(_user_id='')
    assert paired_worker.bind_sender(owner, {'session_id': 'owner-1', 'user': 'hi'}) is None
    assert owner._user_id == ''


def people_family(tmp_path, generator='people.py'):
    """A rendered dev split of a generated family (seed 7, one per template)."""
    spec = importlib.util.spec_from_file_location('paired_generate_outbound', GENERATORS / 'generate.py')
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    module = engine.load_templates(GENERATORS / generator)
    engine.write(tmp_path / generator, module, 7, 'dev', 1, GENERATORS / generator)
    return tmp_path / generator


def image_without(original, key):
    def older_image(*args, **kwargs):
        supplied, recipe = original(*args, **kwargs)
        recipe['container_payload'] = {k: v for k, v in recipe['container_payload'].items() if k != key}
        return supplied, recipe
    return older_image


def test_a_people_plan_gives_every_arm_one_send_message_path_and_refuses_an_image_without_it(
        fixture, monkeypatch, tmp_path):
    """Audit B2: the contact-targeted scenarios measure judgment only when every arm can message a
    contact, so the people family declares the outbound path and a stale image cannot run it."""
    monkeypatch.setattr(paired_cases, 'cases', real_cases)
    directory = people_family(tmp_path)
    arms = ['base-heartbeat', 'full', 'full-people']
    options = dict(native_binding='candidate', evidence_mode='controlled', arms=arms,
                   reference_arm='base-heartbeat', dataset_dir=directory, **fixture.resources)
    manifest = paired.plan(fixture.output, **options)
    schema = json.dumps(paired_worker.OUTBOUND_SCHEMA, sort_keys=True).encode()
    assert manifest['comparison']['outbound'] == {
        'protocol': 'paired-outbound-1', 'mode': 'send_message', 'toolset': 'paired_outbound',
        'tool': 'send_message', 'schema_sha256': hashlib.sha256(schema).hexdigest()}
    assert manifest['comparison']['people_instrument'] == 'paired-people-instrument-1'
    for pair in manifest['pairs']:
        for arm in arms:
            assert pair['arms'][arm]['case']['inputs']['outbound'] == 'send_message'
    original = paired_container.configuration
    for key, message in (('outbound', 'outbound path'), ('people_instrument', 'people store')):
        monkeypatch.setattr(paired_container, 'configuration', image_without(original, key))
        with pytest.raises(ValueError, match=message):
            paired.plan(tmp_path / ('again-' + key), **options)
        assert not (tmp_path / ('again-' + key)).exists()
    # Comparator arms alone read contacts.json from the workspace: no people store to seed.
    monkeypatch.setattr(paired_container, 'configuration', image_without(original, 'people_instrument'))
    base_only = paired.plan(tmp_path / 'base-only', **{**options, 'arms': ['base-heartbeat', 'base_hermes']})
    assert 'people_instrument' not in base_only['comparison'] and base_only['comparison']['outbound']['mode'] == 'send_message'
    # A family that does not declare it has no send tool in any arm.
    monkeypatch.setattr(paired_container, 'configuration', original)
    initiative = paired.plan(tmp_path / 'initiative', **{**options, 'dataset_dir': people_family(tmp_path, 'initiative.py')})
    assert 'outbound' not in initiative['comparison']
    assert all('outbound' not in arm['case']['inputs'] for pair in initiative['pairs'] for arm in pair['arms'].values())


def test_a_declared_outbound_path_is_one_send_message_tool_for_every_turn_worker_and_heartbeat(
        stubbed_hermes, monkeypatch, capsys):
    from protagine.qualification import paired_arms
    registered, toolsets, agents, installs = [], {}, [], []
    monkeypatch.setattr(sys.modules['tools.registry'].registry, 'register',
                        lambda **entry: registered.append(entry), raising=False)
    monkeypatch.setitem(sys.modules, 'toolsets', SimpleNamespace(
        create_custom_toolset=lambda name, description, tools=None, includes=None: toolsets.update({name: tools})))
    native = sys.modules['run_agent'].AIAgent

    class Recorded(native):
        def __init__(self, **kwargs):
            agents.append((kwargs['session_id'], list(kwargs['enabled_toolsets'])))
            super().__init__(**kwargs)
    monkeypatch.setattr(sys.modules['run_agent'], 'AIAgent', Recorded)
    monkeypatch.setattr(paired_arms, 'install_heartbeat', lambda names, prompt=None: installs.append(list(names)) or 'job-1')
    monkeypatch.setattr(paired_arms, 'make_due', lambda job_id: {'job_id': job_id})
    profile = {'name': 'base-heartbeat', 'plugin': False, 'overlay': {}, 'heartbeat': True}
    request = {'binding': 'candidate', 'config': {'model': {'default': 'test'}},
               'inputs': {'arm': 'base-heartbeat', 'profile': profile, 'initial_files': {}, 'max_iterations': 4,
                          'max_output_tokens': 64, 'settle_seconds': 0, 'outbound': 'send_message',
                          'episodes': [USER, INBOUND, TICK]}}
    code, result = run_worker(monkeypatch, capsys, request)
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    expected = [*paired_worker.COMMON_TOOLS, 'paired_outbound']
    assert [entry['name'] for entry in registered] == ['send_message']
    assert registered[0]['toolset'] == 'paired_outbound' and registered[0]['schema'] == paired_worker.OUTBOUND_SCHEMA
    assert registered[0]['handler'] is paired_worker.outbound_send and toolsets == {'paired_outbound': ['send_message']}
    assert agents == [('owner-1', expected), ('contact-1', expected)] and installs == [expected]
    assert result['tool_evidence']['outbound'] == 'send_message'
    # An unknown path stops the worker before any turn runs.
    request['inputs']['outbound'] = 'email'
    agents.clear()
    for name in ('home', 'workspace'):
        shutil.rmtree(stubbed_hermes.root / name)
    with pytest.raises(ValueError, match='outbound path'):
        run_worker(monkeypatch, capsys, request)
    assert agents == []


OUTBOUND_DRIVER = r'''
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
home = root / 'home'
home.mkdir()
os.environ.update(HOME=str(root), HERMES_HOME=str(home), HERMES_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1',
    HERMES_DISABLE_TELEMETRY='1', HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_ENABLE_PROJECT_PLUGINS='0',
    HERMES_BUNDLED_PLUGINS=str(home / 'empty-bundled'))
(home / 'empty-bundled').mkdir()
from protagine.qualification import paired_body, paired_worker
config = {'model': {'default': 'test-model', 'provider': 'custom'}, 'plugins': {'enabled': []}}
outbox = root / 'outbox.json'
paired_body.install_capture_platform(home, config, outbox)
paired_worker.install_tool_loading(config, 'eager')  # as every generated family runs
(home / 'config.yaml').write_text(json.dumps(config))
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load(force=True)
import model_tools
def names(toolsets):
    return sorted(tool['function']['name'] for tool in model_tools.get_tool_definitions(enabled_toolsets=toolsets, quiet_mode=True))
before = names([*paired_worker.COMMON_TOOLS])
added = paired_worker.install_outbound('send_message')
again = paired_worker.install_outbound('send_message')
from tools.registry import registry
sent = registry.dispatch('send_message', {'target': 'capture:p-05', 'message': 'Is the lease signed?'})
wrong = registry.dispatch('send_message', {'target': 'sms:p-05', 'message': 'Is the lease signed?'})
report = {'wrong': json.loads(wrong), 'before': before, 'added': added, 'again': again,
          'with': names([*paired_worker.COMMON_TOOLS, *added]),
          'without': names([*paired_worker.COMMON_TOOLS]), 'schema': registry.get_schema('send_message'),
          'sent': json.loads(sent) if isinstance(sent, str) else sent, 'outbox': paired_body.read_outbox(outbox)}
print('RESULT:' + json.dumps(report, default=str))
'''


@pytest.mark.skipif(importlib.util.find_spec('hermes_constants') is None,
                    reason='Hermes source is not installed in this environment')
def test_the_outbound_tool_is_the_stock_send_path_to_the_capture_platform(tmp_path):
    """Against the real Hermes source: the tool is offered only with its toolset, and a call lands
    in the capture outbox as a platform send to the contact as written."""
    completed = subprocess.run([sys.executable, '-c', OUTBOUND_DRIVER, str(tmp_path)], capture_output=True,
                               text=True, timeout=300)
    assert completed.returncode == 0, completed.stderr[-4000:]
    report = json.loads(next(line[len('RESULT:'):] for line in completed.stdout.splitlines()
                             if line.startswith('RESULT:')))
    assert 'send_message' not in report['before'] and 'send_message' not in report['without']
    assert report['added'] == report['again'] == ['paired_outbound'] and 'send_message' in report['with']
    assert report['schema']['parameters']['required'] == ['target', 'message']
    assert report['sent'].get('success') is True, report['sent']
    assert [(row['target'], row['text'], row['via']) for row in report['outbox']] == [
        ('capture:p-05', 'Is the lease signed?', 'platform')]
    # A target the stock path cannot resolve fails as before, and the error names the valid form.
    assert report['wrong']['error'] and report['wrong']['target_form'] == paired_worker.TARGET_FORM


def test_a_failed_send_names_the_valid_target_form(monkeypatch):
    """Every arm guessed at targets (cli:, chat:, sms:p-NN, a bare p-NN) because the stock error never says what
    a valid one is; the declared outbound tool adds it to a failed call and changes nothing else."""
    def stock(args):
        return json.dumps({'error': 'Unknown platform: sms'} if args['target'].startswith('sms:') else
                          {'success': True, 'message_id': '1'})
    monkeypatch.setitem(sys.modules, 'tools.send_message_tool', SimpleNamespace(send_message_tool=stock))
    failed = json.loads(paired_worker.outbound_send({'target': 'sms:p-05', 'message': 'hi'}))
    assert failed == {'error': 'Unknown platform: sms', 'target_form': paired_worker.TARGET_FORM}
    assert '"address" in contacts.json' in paired_worker.TARGET_FORM
    assert json.loads(paired_worker.outbound_send({'target': 'capture:p-05', 'message': 'hi'})) == {
        'success': True, 'message_id': '1'}


def test_full_and_full_people_differ_only_in_the_people_flag_and_the_mind_reads_it(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', 'p-01')
    on = worker.mind_section(paired_worker.mind_switches(paired.PROFILES['full']))
    off = worker.mind_section(paired_worker.mind_switches(paired.PROFILES['full-people']))
    assert on['faculties']['people'] is True and off['faculties'] == {**on['faculties'], 'people': False}
    assert on['drives'] == off['drives'] and on['drives']['social'] > 0
    minds = {}
    for name, section in (('on', on), ('off', off)):
        directory = tmp_path / name
        directory.mkdir()
        store = InitiativeStore(state_dir=directory)
        try:
            minds[name] = Mind(config=section, store=store, state_dir=directory, owner_id='p-01', backups=False)
        finally:
            store.close()
    assert minds['on'].faculties['people'] is True and minds['on'].drive_weights['social'] == on['drives']['social']
    assert minds['on'].composer.enabled is True
    assert minds['off'].faculties['people'] is False and minds['off'].drive_weights['social'] == 0.0
    assert minds['off'].composer.enabled is False


async def test_the_plugin_arm_has_a_people_store_on_the_body_clock(tmp_path, monkeypatch):
    """A real install has a contact store; the benchmark's plugin arm gets one in its state
    directory for the lifespan, and it stamps times with the shifted body clock the mind ticks
    on, so a conversation after a clock advance is not read as one from before it."""
    import time
    from datetime import datetime, timezone
    from protagine.api.routers import host
    before = host._contacts_store
    shifted = datetime(2031, 3, 4, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(time, 'time', lambda: shifted)
    async with paired_worker.people_store(tmp_path) as store:
        assert host._contacts_store is store
        created = await store.create(display_name='p-02', trust_tier='regular')
        await store.record_interaction(created.contact_id)
        seen = await store.get(created.contact_id)
        assert seen.last_interaction_at.startswith('2031-03-04T12:00') and seen.first_seen_at.startswith('2031-03-04')
    assert host._contacts_store is before and (tmp_path / 'memory-state' / 'protagine-contacts.db').is_file()


# -- One clock for every stamp the social drive compares (audit B1) ------------------------------

CADENCE_MIN = 10


@contextlib.contextmanager
def body_clock():
    """The paired body's shifted wall clock, as every arm runs under it."""
    from protagine.qualification import paired_body
    paired_body.install_clock(0)
    try:
        yield paired_body
    finally:
        paired_body.uninstall_clock()


@pytest.fixture
async def sync_host(tmp_path, monkeypatch):
    """``POST /v1/host/turns/sync`` over a real contact store, comms log and ledger."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from protagine.api.routers import host
    from protagine.contacts.comms import CommsLog
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore
    from protagine.turns import TurnIdempotencyLedger
    monkeypatch.setenv('PROTAGINE_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', 'p-01')
    store = SQLiteContactStore(ContactsConfig(sqlite_path=str(tmp_path / 'protagine-contacts.db')))
    await store.connect()
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    comms = CommsLog(str(tmp_path / 'comms.db'), source_ledger=ledger)
    for name, value in (('_contacts_store', store), ('_comms_log', comms), ('_telemetry', None)):
        monkeypatch.setattr(host, name, value, raising=False)
    app = FastAPI()
    app.include_router(host.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        async def sync(contact_id, turn_id, text, occurred_at=None):
            body = {'identity': {'host_id': 'hermes'},
                    'context': {'session_id': 'contact-1', 'contact_id': contact_id, 'turn_id': turn_id,
                                'channel_id': 'capture:' + contact_id,
                                'metadata': {'occurred_at': occurred_at} if occurred_at else None},
                    'user_message': {'role': 'user', 'content': text},
                    'assistant_message': {'role': 'assistant', 'content': 'Thanks.'}}
            response = await client.post('/v1/host/turns/sync', json=body)
            assert response.status_code == 200, response.text
            return response.json()
        yield SimpleNamespace(store=store, sync=sync, dir=tmp_path)
    comms._conn.close()
    await store.close()


def _seconds_apart(stamp, moment):
    from protagine.util.temporal import parse_iso
    return abs((parse_iso(stamp) - moment).total_seconds())


@needs_hermes
def test_the_body_clock_moves_every_now_line_the_model_reads():
    """One clock (the pilots' I2): Hermes' message stamps, the sidecar's "Now" and the plugin's "Current Time"
    all read the body clock once it is shifted, so no prompt carries two different days."""
    from datetime import datetime, timezone

    import hermes_time
    from protagine.util import temporal
    from protagine_memory.provider import ProtagineMemoryProvider

    provider = ProtagineMemoryProvider({"url": "http://127.0.0.1:1", "contact_id": "p-01"})
    with body_clock() as body:
        body.advance_clock(19 * 3600)
        hermes = hermes_time.now().astimezone(timezone.utc)
        sidecar, line = temporal.now_utc(), provider._current_time_line()
    assert (hermes - datetime.now(timezone.utc)).total_seconds() > 18 * 3600
    assert abs((sidecar - hermes).total_seconds()) < 5
    assert hermes.strftime("%A, %B %d, %Y") in line


@needs_hermes
async def test_an_inbound_turn_after_a_clock_advance_is_recorded_on_the_body_clock(sync_host):
    with body_clock() as body:
        contact = await sync_host.store.create(display_name='p-02', trust_tier='regular', may_contact='auto',
                                               cadence_minutes=CADENCE_MIN)
        body.advance_clock(600)
        # The capture stamps the turn when it happened; the host records that time.
        await sync_host.sync(contact.contact_id, 'turn-1', 'The budget draft is on track.',
                             occurred_at=worker.mind_clock().isoformat())
        seen = await sync_host.store.get(contact.contact_id)
        assert _seconds_apart(seen.last_interaction_at, worker.mind_clock()) <= 5
        # Without a stamp, the store's own clock is the body clock too.
        body.advance_clock(3600)
        await sync_host.sync(contact.contact_id, 'turn-2', 'Still on track.')
        seen = await sync_host.store.get(contact.contact_id)
        assert _seconds_apart(seen.last_interaction_at, worker.mind_clock()) <= 5
        # A stamp from the future (a skewed host) is clamped to now.
        future = worker.mind_clock().timestamp() + 86400
        from datetime import datetime, timezone
        await sync_host.sync(contact.contact_id, 'turn-3', 'Hello again.',
                             occurred_at=datetime.fromtimestamp(future, timezone.utc).isoformat())
        seen = await sync_host.store.get(contact.contact_id)
        assert _seconds_apart(seen.last_interaction_at, worker.mind_clock()) <= 5


@needs_hermes
async def test_a_conversation_after_a_clock_advance_satisfies_the_cadence_end_to_end(sync_host):
    """The family's cadence-satisfied-by-conversation control on the real store and the body clock:
    the contact writes 0.7 cadences in, the ticks run at 1.3 cadences, and no check-in forms."""
    from protagine.commitments.store import CommitmentStore
    from protagine.feedback import TypeFeedbackStore
    from protagine.turns import TurnIdempotencyLedger
    directory = sync_host.dir / 'mind'
    directory.mkdir()
    cadence = CADENCE_MIN * 60
    with body_clock() as body:
        contact = await sync_host.store.create(display_name='p-02', trust_tier='regular', may_contact='auto',
                                               cadence_minutes=CADENCE_MIN)
        await sync_host.store.add_handle(contact.contact_id, 'capture', 'p-02', is_primary=True, verified=True)
        store = InitiativeStore(state_dir=directory)
        try:
            mind = Mind(config={'autonomy': 'standard'}, store=store, state_dir=directory, owner_id='p-01',
                        commitments=CommitmentStore(directory / 'protagine-commitments.db'),
                        feedback=TypeFeedbackStore(str(directory / 'protagine-feedback.db')),
                        contacts=sync_host.store, ledger=TurnIdempotencyLedger(directory / 'ledger.db'),
                        clock=worker.mind_clock, backups=False)
            mind.digest_hour = 25
            body.advance_clock(0.7 * cadence)
            await sync_host.sync(contact.contact_id, 'turn-1', 'The budget draft is on track from my side.',
                                 occurred_at=worker.mind_clock().isoformat())
            body.advance_clock(0.6 * cadence)
            for _ in range(3):
                assert (await mind.tick(force=True))['formed'] == []
            body.advance_clock(0.5 * cadence)       # 1.1 cadences after the conversation: due again
            assert [item['type'] for item in (await mind.tick(force=True))['formed']] == ['check_in']
        finally:
            store.close()


# -- The benchmark's Mind is the production Mind (audit M1) ---------------------------------------

async def test_the_mind_arm_serves_the_people_routes_and_its_mind_has_the_people_reads(tmp_path, monkeypatch):
    """An owner's ``protagine_people merge`` in the benchmark reaches ``/v1/mind/people`` as it does
    in a real install, and the served Mind composes and digests from the same host callables."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from protagine.api.routers import host
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore
    monkeypatch.setenv('PROTAGINE_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', 'p-01')
    store = SQLiteContactStore(ContactsConfig(sqlite_path=str(tmp_path / 'protagine-contacts.db')))
    await store.connect()
    monkeypatch.setattr(host, '_contacts_store', store)
    try:
        await store.create(display_name='p-02', trust_tier='regular')
        for mind, expected in ((True, 200), (False, 404)):
            app = FastAPI()
            worker.mount_routes(app, mind=mind)
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://arm') as client:
                response = await client.get('/v1/mind/people', params={'q': 'p-02'})
            assert response.status_code == expected, (mind, response.text)
        state = tmp_path / 'state'
        full = worker.mind_section(paired_worker.mind_switches(paired.PROFILES['full']))
        with worker.serve_mind(FastAPI(), state, 'p-01', full) as served:
            assert served.packet_for is host.assemble_packet and served.claims_for is host.claims_for
            assert served.contacts is store and served.composer.enabled is True
    finally:
        await store.close()


async def test_the_mind_arm_has_the_comms_ledger_and_the_reply_waits_a_real_install_has(tmp_path, monkeypatch):
    """Review M1: the benchmark host never set a comms ledger and the served Mind had no follow-ups,
    so the arm's digest had no exchange counts, the mind's sends were nowhere for the social drive
    to read, and due reply waits never reached duty. The arm's host stores include the comms
    ledger (``protagine-comms.db`` next to the others) and the Mind gets the server's follow-ups."""
    from fastapi import FastAPI
    from protagine.api.routers import host
    from protagine.contacts.comms import CommsLog
    from protagine.initiatives.temporal_followup import TemporalFollowups
    monkeypatch.setenv('PROTAGINE_STATE_DIR', str(tmp_path / 'memory-state'))
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', 'p-01')
    previous = host._comms_log
    full = worker.mind_section(paired_worker.mind_switches(paired.PROFILES['full']))
    with paired_worker.provider_read_services(tmp_path):
        assert isinstance(host._comms_log, CommsLog)
        assert Path(host._comms_log._db_path) == tmp_path / 'memory-state' / 'protagine-comms.db'
        with worker.serve_mind(FastAPI(), tmp_path, 'p-01', full) as served:
            assert served.comms is host._comms_log
            assert isinstance(served.followups, TemporalFollowups)
            assert served.followups.store is host._commitment_store
    assert host._comms_log is previous
