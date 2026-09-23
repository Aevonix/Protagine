"""The initiative arm: the mind served next to the host routes and ticked by the body tick, with no model."""
import asyncio
from datetime import datetime, timedelta, timezone
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import host
from protagine.api.routers import mind as mind_router
from protagine.qualification import native_memory_worker as worker
from protagine.qualification import paired, paired_worker
from test_qualification_paired_runner import fixture  # noqa: F401  (pytest fixture)

KEY = 'arm-key'
AUTH = {'Authorization': 'Bearer ' + KEY}


def test_the_initiative_profile_is_the_plugin_with_the_mind_on(fixture):
    manifest = paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                           arms=['base_hermes', 'base-heartbeat', 'protagine-initiative'],
                           reference_arm='base-heartbeat', **fixture.resources)
    profiles = manifest['comparison']['profiles']
    assert profiles['protagine-initiative'] == {'name': 'protagine-initiative', 'plugin': True, 'overlay': {},
                                                'initiative': True}
    case = manifest['pairs'][0]['arms']['protagine-initiative']['case']
    assert case['inputs']['profile']['initiative'] is True and case['inputs']['arm'] == 'protagine-initiative'
    assert paired_worker.arm_profile(case['inputs'])['initiative'] is True
    assert 'initiative' in paired_worker.PROFILE_SWITCHES
    assert paired_worker.MIND_TICK_PROTOCOL == 'paired-mind-tick-1'


def test_the_initiative_arm_needs_an_image_whose_worker_serves_the_mind(fixture, monkeypatch):
    from protagine.qualification import paired_container
    original = paired_container.configuration

    def without_mind(*args, **kwargs):
        supplied, recipe = original(*args, **kwargs)
        recipe['container_payload'] = {k: v for k, v in recipe['container_payload'].items() if k != 'mind_tick'}
        return supplied, recipe
    monkeypatch.setattr(paired_container, 'configuration', without_mind)
    with pytest.raises(ValueError, match='serves the mind'):
        paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                    arms=['base-heartbeat', 'protagine-initiative'], **fixture.resources)
    # The plain plugin arm does not need it.
    assert paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled',
                       arms=['base-heartbeat', 'protagine'], **fixture.resources)['declared_attempts'] == 4


def test_mind_section_is_off_in_the_plugin_arm_and_initiative_only_in_the_treatment():
    assert worker.mind_section(False) == {'enabled': False}
    section = worker.mind_section(True)
    assert section['enabled'] is True and section['autonomy'] == 'standard'
    assert section['faculties'] == {'initiative': True, 'people': False, 'affect': False, 'opinions': False,
                                    'broadcast': False, 'semantic_recall': False, 'consolidation': False,
                                    'self_narrative': False, 'lessons': False, 'skills': False}
    assert section['quiet_hours'] == '' and section['digest_hour'] == 24
    from protagine.mind.authority import Policy
    policy = Policy.from_config(section)
    assert policy.level == 'standard' and policy.enabled is True and policy.quiet_hours == ''


def test_the_worker_profile_exists_for_the_dispatcher(tmp_path):
    directory = worker.install_worker_profile(tmp_path)
    assert directory == tmp_path / 'profiles' / 'protagine-act' and (directory / 'config.yaml').is_file()
    assert (directory / 'sessions').is_dir()
    worker.install_worker_profile(tmp_path)  # idempotent


def test_served_mind_follows_the_shifted_body_clock_and_forms_from_the_host_commitment_store(tmp_path, monkeypatch):
    from protagine.commitments.store import CommitmentStore
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', 'p-01')
    state = tmp_path / 'state'
    (state / 'memory-state').mkdir(parents=True)
    commitments = CommitmentStore(state / 'memory-state' / 'protagine-commitments.db')
    monkeypatch.setattr(host, '_commitment_store', commitments)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=KEY)
    app.include_router(mind_router.router)
    real_time = time.time
    offset = [0.0]
    monkeypatch.setattr(time, 'time', lambda: real_time() + offset[0])
    with worker.serve_mind(app, state, 'p-01', worker.mind_section(True)) as mind:
        assert mind_router.get_mind() is mind and mind.enabled and mind.level == 'standard'
        assert abs((mind.clock() - datetime.now(timezone.utc)).total_seconds()) < 5
        commitments.create(person_id='p-01', description='send the owner the report',
                           due_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat())

        async def run():
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://arm') as client:
                early = (await client.post('/v1/mind/tick', headers=AUTH)).json()
                offset[0] = 3600.0  # the episode's advance_clock
                late = (await client.post('/v1/mind/tick', headers=AUTH)).json()
                queue = (await client.get('/v1/mind/dispatch', headers=AUTH)).json()
                state_ = (await client.get('/v1/mind/state', headers=AUTH)).json()
                return early, late, queue, state_
        early, late, queue, state_ = asyncio.run(run())
        assert early['formed'] == [] and [item['type'] for item in late['formed']] == ['commitment_overdue']
        assert len(queue) == 1 and queue[0]['assignee'] == 'protagine-act'
        assert state_['enabled'] is True and state_['autonomy'] == 'standard'
        assert mind.clock() - datetime.now(timezone.utc) > timedelta(minutes=59)
    assert mind_router.get_mind() is None
    assert (state / 'memory-state' / 'initiatives.db').is_file()
