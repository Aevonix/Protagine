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


@pytest.mark.parametrize('arm,switch,prompt', [
    ('base-heartbeat', 'heartbeat', paired_arms.HEARTBEAT_PROMPT),
    ('base-heartbeat-checkin', 'heartbeat_checkin', paired_arms.HEARTBEAT_CHECKIN_PROMPT)])
def test_heartbeat_arm_installs_the_job_once_and_its_tick_step_makes_it_due(stubbed_hermes, monkeypatch, capsys,
                                                                            arm, switch, prompt):
    installs, prompts, due = [], [], []

    def install(toolsets, wording=paired_arms.HEARTBEAT_PROMPT):
        installs.append(list(toolsets))
        prompts.append(wording)
        return 'job-1'
    monkeypatch.setattr(paired_arms, 'install_heartbeat', install)
    monkeypatch.setattr(paired_arms, 'make_due', lambda job_id: due.append(job_id) or {'job_id': job_id})
    profile = {'name': arm, 'plugin': False, 'overlay': {}, switch: True}
    code, result = run_worker(monkeypatch, capsys, comparator_request(arm, profile))
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    assert installs == [['file', 'memory', 'session_search', 'todo']] and prompts == [prompt]
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
    monkeypatch.setattr(paired_arms, 'install_heartbeat',
                        lambda toolsets, prompt=None: pytest.fail('no heartbeat in this arm'))
    profile = {'name': 'base-curator', 'plugin': False, 'overlay': {}, 'curator': True}
    code, result = run_worker(monkeypatch, capsys, comparator_request('base-curator', profile))
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    config = json.loads((stubbed_hermes.root / 'home' / 'config.yaml').read_text())
    assert config['curator'] == {'enabled': True, 'consolidate': True}
    ticks = [event for event in stubbed_hermes.events if event[0] == 'tick']
    assert ticks[0][2]() == {'curator': {'summary': 'no changes'}} and reviews == [1]


def test_a_generated_family_loads_every_tool_eagerly_in_every_arm(stubbed_hermes, monkeypatch, capsys):
    monkeypatch.setattr(paired_arms, 'install_heartbeat', lambda toolsets, prompt=None: 'job-1')
    monkeypatch.setattr(paired_arms, 'make_due', lambda job_id: {'job_id': job_id})
    profile = {'name': 'base-heartbeat', 'plugin': False, 'overlay': {}, 'heartbeat': True}
    request = comparator_request('base-heartbeat', profile)
    request['inputs']['tool_loading'] = 'eager'
    request['config']['tools'] = {'other': {'kept': True}}
    code, result = run_worker(monkeypatch, capsys, request)
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    config = json.loads((stubbed_hermes.root / 'home' / 'config.yaml').read_text())
    # The stock Hermes key, alongside whatever the shared config already said about tools.
    assert config['tools'] == {'other': {'kept': True}, 'tool_search': {'enabled': 'off'}}
    assert result['tool_evidence']['tool_loading'] == 'eager'
    assert worker.EAGER_TOOLS_CONFIG == {'tool_search': {'enabled': 'off'}}
    with pytest.raises(ValueError, match='tool loading'):
        worker.install_tool_loading({}, 'lazy')


def test_frozen_datasets_keep_stock_tool_loading_and_bare_turns(stubbed_hermes, monkeypatch, capsys):
    profile = {'name': 'base-plain', 'plugin': False, 'overlay': {}}
    request = comparator_request('base-plain', profile)
    assert 'tool_loading' not in request['inputs'] and 'message_timestamps' not in request['inputs']
    code, result = run_worker(monkeypatch, capsys, request)
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    config = json.loads((stubbed_hermes.root / 'home' / 'config.yaml').read_text())
    assert 'tools' not in config and result['tool_evidence']['tool_loading'] is None
    assert result['tool_evidence']['message_timestamps'] is None
    assert result['tool_evidence']['environment_note'] is None
    assert [call[2] for call in stubbed_hermes.calls] == [USER['user']]
    assert stubbed_hermes.systems == [worker.SYSTEM]
    assert stubbed_hermes.cron_prompts == ['[Heartbeat]\nCheck.'] * 3
    assert stubbed_hermes.cron_systems == [None] * 3
    assert worker.install_tool_loading({'tools': {'x': 1}}, None) is None


def test_a_generated_family_stamps_every_turn_with_the_body_clock(stubbed_hermes, monkeypatch, capsys):
    from datetime import datetime, timezone
    import sys
    from protagine.qualification import paired_body
    from test_qualification_body_events import INBOUND, REACTION
    clock = [datetime(2027, 3, 4, 9, 19, 34, tzinfo=timezone.utc)]
    monkeypatch.setattr(sys.modules['hermes_time'], 'now', lambda: clock[0])
    monkeypatch.setattr(paired_arms, 'install_heartbeat', lambda toolsets, prompt=None: 'job-1')
    monkeypatch.setattr(paired_arms, 'make_due', lambda job_id: {'job_id': job_id})
    profile = {'name': 'base-heartbeat', 'plugin': False, 'overlay': {}, 'heartbeat': True}
    request = comparator_request('base-heartbeat', profile)
    request['inputs']['message_timestamps'] = 'gateway'
    request['inputs']['environment_note'] = 'messaging'
    request['inputs']['episodes'] = [USER, INBOUND, CLOCK, REACTION, TICK]
    original_advance = paired_body.advance_clock

    def advance(seconds):
        clock[0] = clock[0].replace(hour=10)
        return original_advance(seconds)
    monkeypatch.setattr(paired_body, 'advance_clock', advance)
    code, result = run_worker(monkeypatch, capsys, request)
    assert code == 0 and result['stage'] == 'returned', result.get('private_error_traceback')
    assert result['tool_evidence']['message_timestamps'] == 'gateway'
    # The stock gateway format on the owner turn, the inbound message and the reaction after the advance.
    assert [call[2] for call in stubbed_hermes.calls] == [
        '[Thu 2027-03-04 09:19:34 UTC] Remind me about the invoice tomorrow.',
        '[Thu 2027-03-04 09:19:34 UTC] [Message from contact p-03 on chat]\nAny news?',
        '[Thu 2027-03-04 10:19:34 UTC] Please do not remind me again.']
    # Every turn's system message and every cron run carry the same description of the body.
    note = worker.ENVIRONMENT_NOTES['messaging']
    assert result['tool_evidence']['environment_note'] == 'messaging'
    assert stubbed_hermes.systems == [worker.SYSTEM + '\n' + note] * 3
    assert 'contacts.json' in note and 'scheduler' in note and 'p-07' in note
    # A cron run (the heartbeat) sees the same clock on its prompt; runtime and limits stay pinned.
    assert stubbed_hermes.cron_prompts == ['[Thu 2027-03-04 10:19:34 UTC] [Heartbeat]\nCheck.'] * 2
    assert stubbed_hermes.cron_systems == [note] * 2
    assert stubbed_hermes.cron_agents[-1]['agent']['max_tokens'] == 64
    assert stubbed_hermes.cron_agents[-1]['max_iterations'] == 4
    with pytest.raises(ValueError, match='environment note'):
        worker.environment_note('coaching')
    assert worker.environment_note(None) is None
    with pytest.raises(ValueError, match='message timestamps'):
        worker.stamp_message('x', 'iso')
    assert worker.stamp_message('bare', None) == 'bare'
    assert worker.MESSAGE_TIMESTAMP_FORMAT == '[%a %Y-%m-%d %H:%M:%S %Z]'


def test_cron_prompts_are_stamped_through_the_pinned_agent_seam(monkeypatch):
    import sys
    from types import SimpleNamespace
    prompts = []

    class Agent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_conversation(self, prompt, **kwargs):
            prompts.append((prompt, kwargs))
            return {'completed': True}
    scheduler = SimpleNamespace(_construct_cron_agent=lambda AIAgent, job, config, setup, **kw: AIAgent(job=job))
    monkeypatch.setitem(sys.modules, 'cron', SimpleNamespace(scheduler=scheduler))
    monkeypatch.setitem(sys.modules, 'cron.scheduler', scheduler)
    setup = SimpleNamespace(runtime={}, max_iterations=None)
    with paired_arms.pinned_cron_agents(lambda runtime: runtime, max_iterations=8, max_tokens=64,
                                        stamp=lambda text: '[Thu 2027-03-04 09:19:34 UTC] ' + text,
                                        system_message='the body'):
        agent = scheduler._construct_cron_agent(Agent, {'name': 'paired-heartbeat'}, {}, setup)
        assert agent.run_conversation('[Heartbeat]\nCheck.', task_id='t') == {'completed': True}
    assert prompts == [('[Thu 2027-03-04 09:19:34 UTC] [Heartbeat]\nCheck.', {'task_id': 't', 'system_message': 'the body'})]
    assert agent.kwargs == {'job': {'name': 'paired-heartbeat'}, 'max_tokens': 64} and setup.max_iterations == 8
    with paired_arms.pinned_cron_agents(lambda runtime: runtime, max_iterations=8, max_tokens=64):
        scheduler._construct_cron_agent(Agent, {}, {}, setup).run_conversation('[Heartbeat]\nCheck.')
    assert prompts[-1] == ('[Heartbeat]\nCheck.', {})


def test_base_arms_have_no_tick_step_and_switches_must_be_booleans(stubbed_hermes, monkeypatch, capsys):
    profile = {'name': 'base-plain', 'plugin': False, 'overlay': {}}
    code, _ = run_worker(monkeypatch, capsys, comparator_request('base-plain', profile))
    assert code == 0 and [event[2] for event in stubbed_hermes.events if event[0] == 'tick'] == [None] * 3
    with pytest.raises(ValueError, match='Unknown experiment arm'):
        worker.arm_profile({'arm': 'x', 'profile': {'plugin': False, 'overlay': {}, 'heartbeat': 'yes'}})
    assert worker.arm_profile({'arm': 'x', 'profile': {**profile, 'curator': True}})['curator'] is True
