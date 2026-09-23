"""The comparator arms inside the worker loop, with stubbed native APIs and no model."""
import json

import pytest

from protagine.qualification import paired_arms
from protagine.qualification import paired_worker as worker
from test_qualification_body_events import CLOCK, TICK, USER, run_worker, stubbed_hermes  # noqa: F401


def comparator_request(arm, profile):
    return {'binding': 'candidate', 'config': {'model': {'default': 'test'}},
            'inputs': {'arm': arm, 'profile': profile, 'initial_files': {}, 'max_iterations': 4,
                       'max_output_tokens': 64, 'settle_seconds': 0, 'worker_wait_seconds': 7,
                       'episodes': [USER, TICK, CLOCK, {'tick': 1}]}}


def test_heartbeat_arm_installs_the_job_once_and_its_tick_step_makes_it_due(stubbed_hermes, monkeypatch, capsys):
    installs, due = [], []
    monkeypatch.setattr(paired_arms, 'install_heartbeat', lambda toolsets: installs.append(list(toolsets)) or 'job-1')
    monkeypatch.setattr(paired_arms, 'make_due', lambda job_id: due.append(job_id) or {'job_id': job_id})
    profile = {'name': 'base-heartbeat', 'plugin': False, 'overlay': {}, 'heartbeat': True}
    code, result = run_worker(monkeypatch, capsys, comparator_request('base-heartbeat', profile))
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    assert installs == [['file', 'memory', 'session_search', 'todo']]
    assert paired_arms.heartbeat_toolsets(installs[0]) == ['file', 'memory', 'session_search', 'todo', 'kanban', 'cronjob']
    ticks = [event for event in stubbed_hermes.events if event[0] == 'tick']
    assert len(ticks) == 3 and all(callable(event[2]) for event in ticks)
    # The arm's step of every tick makes the same job due; nothing else runs in it.
    assert ticks[0][2]() == {'heartbeat': {'job_id': 'job-1'}} and due == ['job-1']
    assert result['tool_evidence']['arm_profile'] == profile
    config = json.loads((stubbed_hermes.root / 'home' / 'config.yaml').read_text())
    assert 'curator' not in config


def test_curator_arm_turns_the_curator_on_and_reviews_at_every_tick(stubbed_hermes, monkeypatch, capsys):
    reviews = []
    monkeypatch.setattr(paired_arms, 'curator_review', lambda: reviews.append(1) or {'summary': 'no changes'})
    monkeypatch.setattr(paired_arms, 'install_heartbeat', lambda toolsets: pytest.fail('no heartbeat in this arm'))
    profile = {'name': 'base-curator', 'plugin': False, 'overlay': {}, 'curator': True}
    code, result = run_worker(monkeypatch, capsys, comparator_request('base-curator', profile))
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    config = json.loads((stubbed_hermes.root / 'home' / 'config.yaml').read_text())
    assert config['curator'] == {'enabled': True, 'consolidate': True}
    ticks = [event for event in stubbed_hermes.events if event[0] == 'tick']
    assert ticks[0][2]() == {'curator': {'summary': 'no changes'}} and reviews == [1]


def test_base_arms_have_no_tick_step_and_switches_must_be_booleans(stubbed_hermes, monkeypatch, capsys):
    profile = {'name': 'base-plain', 'plugin': False, 'overlay': {}}
    code, _ = run_worker(monkeypatch, capsys, comparator_request('base-plain', profile))
    assert code == 0 and [event[2] for event in stubbed_hermes.events if event[0] == 'tick'] == [None] * 3
    with pytest.raises(ValueError, match='Unknown experiment arm'):
        worker.arm_profile({'arm': 'x', 'profile': {'plugin': False, 'overlay': {}, 'heartbeat': 'yes'}})
    assert worker.arm_profile({'arm': 'x', 'profile': {**profile, 'curator': True}})['curator'] is True
