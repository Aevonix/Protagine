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
    assert worker.mind_section(False) == {'enabled': False} == worker.mind_section(None)
    section = worker.mind_section(True)
    assert section == worker.mind_section({'initiative': True})
    assert section['enabled'] is True and section['autonomy'] == 'standard'
    assert section['faculties'] == {name: name == 'initiative' for name in worker.MIND_FACULTIES}
    assert {'drives', 'deliberation', 'goals', 'broadcast'} <= set(section['faculties'])
    assert section['quiet_hours'] == '' and section['digest_hour'] == 24
    from protagine.mind.authority import Policy
    policy = Policy.from_config(section)
    assert policy.level == 'standard' and policy.enabled is True and policy.quiet_hours == ''


def test_the_drives_family_arms_are_full_and_its_binary_ablations(fixture):
    from protagine.config import DEFAULTS
    arms = ['base-heartbeat', 'full', 'full-drives', 'full-broadcast']
    manifest = paired.plan(fixture.output, native_binding='candidate', evidence_mode='controlled', arms=arms,
                           reference_arm='full', **fixture.resources)
    profiles = manifest['comparison']['profiles']
    assert profiles['full'] == {'name': 'full', 'plugin': True, 'overlay': {}, 'full': True}
    assert profiles['full-drives'] == {'name': 'full-drives', 'plugin': True, 'overlay': {}, 'full': True,
                                       'minus_drives': True}
    # The per-drive diagnostics (evals section 6.6) are their own 8-arm plan against full.
    diagnostics = ['full', 'full-duty', 'full-curiosity', 'full-mastery', 'full-upkeep', 'full-social']
    diagnostic_plan = paired.plan(fixture.output / 'diagnostics', native_binding='candidate',
                                  evidence_mode='controlled', arms=diagnostics, reference_arm='full',
                                  **fixture.resources)
    profiles.update(diagnostic_plan['comparison']['profiles'])
    for arm in [*arms[1:], *diagnostics[1:]]:
        pairs = (manifest if arm in arms else diagnostic_plan)['pairs']
        assert paired_worker.arm_profile(pairs[0]['arms'][arm]['case']['inputs'])['full'] is True
        assert paired_worker.mind_switches(profiles[arm])
    assert paired_worker.mind_switches({'plugin': True, 'overlay': {}}) is None
    assert paired_worker.ARM_PROFILE_PROTOCOL == 'paired-arm-profiles-3'
    assert set(paired_worker.MIND_SWITCHES) <= set(paired_worker.PROFILE_SWITCHES)

    full = worker.mind_section(paired_worker.mind_switches(profiles['full']))
    assert full['faculties'] == {name: bool(DEFAULTS['mind']['faculties'].get(name, False))
                                 for name in worker.MIND_FACULTIES}
    assert full['faculties']['drives'] and full['faculties']['broadcast'] and not full['faculties']['skills']
    assert full['drives'] == DEFAULTS['mind']['drives'] and full['budgets'] == DEFAULTS['mind']['budgets']
    flat = worker.mind_section(paired_worker.mind_switches(profiles['full-drives']))
    assert flat['faculties']['drives'] is False and flat['faculties']['broadcast'] is True
    no_broadcast = worker.mind_section(paired_worker.mind_switches(profiles['full-broadcast']))
    assert no_broadcast['faculties']['broadcast'] is False and no_broadcast['faculties']['drives'] is True
    for name in ('duty', 'curiosity', 'mastery', 'upkeep', 'social'):
        section = worker.mind_section(paired_worker.mind_switches(profiles[f'full-{name}']))
        assert section['drives'][name] == 0.0
        assert all(section['drives'][other] == DEFAULTS['mind']['drives'][other]
                   for other in section['drives'] if other != name)
    from protagine.mind.drives import weights
    assert weights(flat['drives'], faculty_on=flat['faculties']['drives']) == {
        'duty': 1.0, 'social': 1.0, 'curiosity': 1.0, 'mastery': 1.0, 'upkeep': 1.0}
    assert weights(full['drives'], faculty_on=full['faculties']['drives']) == DEFAULTS['mind']['drives']


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


def _hermes_plugin(name):
    """One module of the Hermes adapter, loaded from its directory without executing the plugin's
    ``__init__`` (which registers hooks with a running Hermes); ``body``, ``capture`` and ``client``
    import only the standard library, httpx and each other."""
    import importlib
    import importlib.util
    import sys
    from pathlib import Path
    package = 'hermes_plugin_under_test'
    if package not in sys.modules:
        location = Path(__file__).resolve().parents[2] / 'plugins' / 'hermes-plugin'
        spec = importlib.util.spec_from_loader(package, loader=None, is_package=True)
        module = importlib.util.module_from_spec(spec)
        module.__path__ = [str(location)]
        sys.modules[package] = module
    return importlib.import_module(f'{package}.{name}')


def test_the_benchmark_identity_gives_the_body_an_owner_handle_to_send_to(tmp_path):
    """An owner message from the mind is sent through ``Body.message_target``, which needs an owner
    handle from ``identity.yaml``; with none the body finds no target and the message waits unclaimed.
    The disposable identity names the capture platform's home channel, the target the grader expects."""
    import json
    from protagine.qualification import paired_body
    client, body = _hermes_plugin('client'), _hermes_plugin('body')
    instance = tmp_path / 'protagine-instance'
    instance.mkdir()
    identity = worker.benchmark_identity('p-01')
    assert identity['owner']['contact_id'] == 'p-01' and identity['owner']['handles'] == {'capture': ['owner']}
    (instance / 'identity.yaml').write_text(json.dumps(identity))
    settings = client.Settings(sidecar_url='http://127.0.0.1:1', key_file=instance / 'api.key', api_key='k',
                               home=instance, hermes_home=tmp_path, outbox_path=tmp_path / 'outbox.sqlite3')
    assert settings.owner_handle() == ('capture', 'owner') and settings.owner_contact_id() == 'p-01'
    target = body.Body(client.ProtagineClient(settings), None, None, settings).message_target
    # The shapes the outbox lists an owner reminder in: flagged, named as the owner contact, unnamed.
    assert target({'id': 'm-1', 'kind': 'notice', 'recipient': 'p-01', 'recipient_is_owner': True}) == 'capture:owner'
    assert target({'id': 'm-2', 'kind': 'notice', 'recipient': 'p-01'}) == 'capture:owner'
    assert target({'id': 'm-3', 'kind': 'notice'}) == 'capture:owner'
    assert target({'id': 'm-1', 'recipient': 'p-01', 'recipient_is_owner': True}) == paired_body.PLUGIN + ':' + paired_body.OWNER
    # A contact with no handle anywhere still gets nothing: the owner handle is not a fallback for others.
    assert target({'id': 'm-4', 'kind': 'message', 'recipient': 'p-02', 'recipient_is_owner': False}) == ''
