"""The people family's instrument (families/mind-people-1.md section 7): the plugin arm's people
store is seeded from the same ``contacts.json`` every arm reads, an inbound session carries its
sender so the adapter resolves the contact, the outbound path is declared in the plan, and the
``full`` and ``full-people`` arms differ in the one flag the mind reads."""

from __future__ import annotations

import json
from types import SimpleNamespace

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
