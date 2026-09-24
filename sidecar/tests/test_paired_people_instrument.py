"""The people family's instrument (families/mind-people-1.md section 7): the plugin arm's people
store is seeded from the same ``contacts.json`` every arm reads, an inbound session carries its
sender so the adapter resolves the contact, the outbound path is declared in the plan, and the
``full`` and ``full-people`` arms differ in the one flag the mind reads."""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest

from protagine.initiatives.store import InitiativeStore
from protagine.mind import Mind
from protagine.qualification import native_memory_worker as worker
from protagine.qualification import paired, paired_worker
from test_qualification_paired_runner import fixture  # noqa: F401  (pytest fixture)

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


def test_the_plan_declares_the_outbound_path_in_every_arm(fixture):
    assert paired_worker.OUTBOUND_PROTOCOL == 'capture-send-message-1'
    manifest = paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                           arms=['base-heartbeat', 'full', 'full-people'], reference_arm='base-heartbeat',
                           **fixture.resources)
    assert manifest['comparison']['outbound'] == 'capture-send-message-1'


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
    for name, value in (('_contacts_store', store), ('_comms_log', comms), ('_graph', None),
                        ('_presence_store', None), ('_telemetry', None)):
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
