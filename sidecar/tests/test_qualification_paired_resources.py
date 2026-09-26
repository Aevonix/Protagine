"""Resource views join diagnostics conservatively, without rerunning scored work."""
from copy import deepcopy
import json

import pytest

from protagine.qualification import paired_report


def request(identity, *, workload=None, usage=None, complete=True, elapsed=1000):
    result = {'trace_request_id': identity, 'timing_protocol': 'paired-transport-2',
        'response_mode': 'buffered_json', 'response_complete': complete,
        'status': 200, 'elapsed_ms': elapsed, 'usage': usage}
    if workload is not None:
        result['workload'] = workload
    return result


def trace(tmp_path, events):
    member = {'path': 'runs/one', 'case': {'id': 'paired.test'}}
    path = tmp_path / member['path'] / 'attempts' / member['case']['id'] / 'private-trace.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(json.dumps({'protocol': 'paired-private-trace-1',
        'kind': 'model_request', 'thread': thread, 'data': {'request_id': identity,
        'payload': {'messages': ['PRIVATE_BODY_MUST_NOT_BE_COPIED']}}}) for identity, thread in events))
    return member, path


def test_workloads_keep_generic_threads_unknown_and_preserve_usage_and_missingness(tmp_path):
    member, path = trace(tmp_path, [(1, 'Thread-3 (<lambda>)'), (2, 'paired-source-worker'),
                                  (3, 'paired-source-worker'), (4, 'MainThread')])
    requests = [request(1, usage={'prompt_tokens': 100, 'completion_tokens': 20}),
        request(2, usage={'prompt_tokens': 40, 'completion_tokens': 10}, elapsed=500),
        request(3, complete=False), request(4, workload='foreground',
            usage={'prompt_tokens': 80, 'completion_tokens': 30}, elapsed=2000)]
    original, raw_trace = deepcopy(requests), path.read_bytes()
    result = paired_report._workloads([paired_report._workload_observations(
        tmp_path, member, {'effects': {'model_requests': requests}})])
    attribution = result['attribution']
    assert attribution['observed_requests'] == 4
    assert attribution['attributed_requests'] == 3 and attribution['unknown_requests'] == 1
    assert attribution['coverage'] == 'partial'
    groups = result['groups']
    assert groups['background']['usage']['observed_requests'] == 2
    assert groups['background']['usage']['requests_missing_usage'] == 1
    assert groups['background']['usage']['incomplete_or_failed_requests'] == 1
    assert groups['background']['usage']['input_tokens']['observed_total'] == 40
    assert groups['background']['usage']['usage_coverage'] == 'partial'
    assert groups['background']['timing']['metrics']['request_elapsed_ms']['median'] == 500
    assert groups['background']['timing']['metrics']['request_output_tokens_per_second']['median'] == 20
    assert groups['foreground']['timing']['metrics']['request_output_tokens_per_second']['median'] == 15
    assert groups['unknown']['usage']['observed_requests'] == 1
    assert all('episode_elapsed_ms' not in group['timing']['metrics'] for group in groups.values())
    assert all(group['timing']['decode_tokens_per_second'] is None for group in groups.values())
    assert 'PRIVATE_BODY' not in json.dumps(result)
    assert requests == original and path.read_bytes() == raw_trace


@pytest.mark.parametrize('events,requests', [
    ([(1, 'Thread-3 (<lambda>)')], [request(1)]),
    ([(1, 'MainThread')], [request(1)]),
    ([(1, 'paired-source-worker')], [request(2)]),
    ([(1, 'paired-source-worker'), (1, 'paired-source-worker')], [request(1)]),
    ([(1, 'paired-source-worker')], [request(1), request(1)]),
    ([(1, 'paired-source-worker')], [request(1, workload='foreground')]),
    ([(True, 'paired-source-worker')], [request(True)]),
])
def test_ambiguous_or_conflicting_evidence_never_implies_foreground(tmp_path, events, requests):
    member, _ = trace(tmp_path, events)
    observations, _ = paired_report._workload_observations(tmp_path, member,
        {'effects': {'model_requests': requests}})
    assert all(group == 'unknown' for _, group, _ in observations)


@pytest.mark.parametrize('problem', ['absent', 'malformed', 'symlink', 'oversized'])
def test_unusable_trace_preserves_explicit_labels_and_does_not_break_reporting(tmp_path, problem):
    member, path = trace(tmp_path, [(1, 'paired-source-worker')])
    if problem == 'absent':
        path.unlink()
    elif problem == 'malformed':
        path.write_text('{broken')
    elif problem == 'symlink':
        target = tmp_path / 'other-private-trace'
        path.rename(target)
        path.symlink_to(target)
    else:
        with path.open('wb') as output:
            output.truncate(8 * 1024 * 1024 + 1)
    result = paired_report._workloads([paired_report._workload_observations(tmp_path, member,
        {'effects': {'model_requests': [request(1), request(2, workload='foreground')]}})])
    assert result['attribution']['attributed_requests'] == 1
    assert result['attribution']['unknown_requests'] == 1
    assert result['attribution']['trace_episodes'] == {('absent' if problem == 'absent' else 'invalid'): 1}


def test_usage_counts_partial_fields_zero_and_incomplete_response_without_inventing_totals():
    requests = [request(1, usage={'prompt_tokens': 0, 'completion_tokens': 0}),
        request(2, usage={'prompt_tokens': True, 'completion_tokens': 20}),
        request(3, usage={'prompt_tokens': 5, 'completion_tokens': -1}),
        request(4, usage={'prompt_tokens': 7, 'completion_tokens': 3}, complete=False), request(5)]
    usage = paired_report._request_usage(requests)
    assert usage['requests_with_usage'] == 2 and usage['requests_missing_usage'] == 3
    assert usage['completed_requests_missing_usage'] == 3
    assert usage['input_tokens'] == {'observed_total': 12,
        'requests_with_observation': 3, 'requests_missing_observation': 2}
    assert usage['output_tokens'] == {'observed_total': 23,
        'requests_with_observation': 3, 'requests_missing_observation': 2}
    assert paired_report._request_usage([])['input_tokens']['observed_total'] is None
    assert paired_report._request_usage([request(1)])['usage_coverage'] == 'unobserved'
    assert paired_report._request_usage([request(1, usage={'completion_tokens': 0})])['usage_coverage'] == 'partial'


def test_workload_summary_preserves_scores_rows_and_existing_aggregate_keys(tmp_path, monkeypatch):
    member, path = trace(tmp_path, [(1, 'paired-source-worker')])
    row = {'outcome': 'pass', 'primary_outcome': 'pass', 'elapsed_ms': 4000,
        'effects': {'model_requests': [request(1, usage={'prompt_tokens': 20, 'completion_tokens': 10})]}}
    original, raw_trace = deepcopy(row), path.read_bytes()
    manifest = {'recipe': {}, 'pairs': [{'episode_id': 'paired.test', 'order': list(paired_report.ARMS),
        'task_sha256': 'b' * 64, 'oracle_sha256': 'c' * 64,
        'arms': dict.fromkeys(paired_report.ARMS, member)}], 'sha256': 'd' * 64,
        'comparison_key': 'e' * 64, 'label': 'test', 'evidence_mode': 'controlled',
        'dataset': {'version': 'test', 'split': 'development'},
        'comparison': {'policy': {'environment': {'endpoint_usage': 'unknown'}}}}
    monkeypatch.setattr(paired_report, 'load_manifest', lambda _: manifest)
    monkeypatch.setattr(paired_report, '_row', lambda *args: row)
    report = paired_report.summarize(tmp_path)
    assert report['paired_score']['ties'] == report['paired_score']['both_completed'] == 1
    assert report['report_protocol'] == 'paired-attribution-3' and report['schema'] == 1
    for arm in paired_report.ARMS:
        assert report['arms'][arm]['accounting'] == paired_report._accounting([original])
        assert report['arms'][arm]['timing'] == paired_report._timing([original])
        assert report['pairs'][0]['results'][arm] == original
    rendered = paired_report.markdown(report)
    assert 'Usage missing' in rendered and 'workload attribution: 1/1' in rendered
    assert 'PRIVATE_BODY' not in rendered
    assert row == original and path.read_bytes() == raw_trace


def test_incomplete_agent_turns_are_counted_per_arm_without_touching_the_score(tmp_path, monkeypatch):
    member, _ = trace(tmp_path, [(1, 'paired-source-worker')])
    turns = [{'session_id': 'owner-1', 'kind': 'user', 'completed': False, 'final_response': 'Summary.'},
             {'event': 'advance_clock', 'completed': True},
             {'session_id': 'contact-1', 'kind': 'inbound', 'completed': True, 'final_response': 'Noted.'},
             {'event': 'tick', 'completed': True}]
    row = {'outcome': 'pass', 'primary_outcome': 'pass', 'elapsed_ms': 4000,
        'effects': {'model_requests': [request(1)], 'turns': turns, 'declared_turns': 4, 'turns_completed': 3}}
    original = deepcopy(row)
    manifest = {'recipe': {}, 'pairs': [{'episode_id': 'paired.test', 'order': list(paired_report.ARMS),
        'task_sha256': 'b' * 64, 'oracle_sha256': 'c' * 64,
        'arms': dict.fromkeys(paired_report.ARMS, member)}], 'sha256': 'd' * 64,
        'comparison_key': 'e' * 64, 'label': 'test', 'evidence_mode': 'controlled',
        'dataset': {'version': 'test', 'split': 'development'},
        'comparison': {'policy': {'environment': {'endpoint_usage': 'unknown'}}}}
    monkeypatch.setattr(paired_report, 'load_manifest', lambda _: manifest)
    monkeypatch.setattr(paired_report, '_row', lambda *args: row)
    report = paired_report.summarize(tmp_path)
    for arm in paired_report.ARMS:
        # Events are not agent turns; one of the two agent turns ended short, in the one episode.
        assert report['arms'][arm]['native_turns'] == {
            'agent_turns': 2, 'incomplete_agent_turns': 1, 'episodes_with_incomplete_turn': 1}
    assert report['paired_score']['ties'] == 1
    assert '| Incomplete agent turns |' in paired_report.markdown(report)
    assert '1/2 in 1 episodes' in paired_report.markdown(report)
    assert row == original
    assert paired_report._native_turns([{'effects': {}}]) == {
        'agent_turns': 0, 'incomplete_agent_turns': 0, 'episodes_with_incomplete_turn': 0}


def test_dead_values_in_the_injected_context_are_a_secondary_count_per_arm(tmp_path, monkeypatch):
    """A forbidden (superseded) value on a line of the model's injected context, with no expected value and no
    superseded note, counts; the text itself never reaches the report."""
    case = {'id': 'knowledge-update.01', 'oracle': {'artifacts': [{'path': 'answer.json', 'forbidden': ['corner office'],
            'assertions': [{'path': ['place'], 'op': 'label_one_of', 'value': ['front lobby']}]}]}}
    member = {'path': 'runs/one', 'case': case}
    path = tmp_path / 'runs/one/attempts/knowledge-update.01/private-trace.jsonl'
    path.parent.mkdir(parents=True)

    def event(thread, *blocks):
        content = 'Where is it now?' + ''.join(f'\n\n<memory-context>\n{block}\n</memory-context>' for block in blocks)
        return json.dumps({'protocol': 'paired-private-trace-1', 'kind': 'model_request', 'thread': thread,
                           'data': {'request_id': 1, 'payload': {'messages': [
                               {'role': 'system', 'content': 'The review is in the corner office.'},
                               {'role': 'user', 'content': content}]}}})
    earlier = 'The review is in the corner office.'
    probe = '\n'.join(['## Relevant Memories', 'The review is in the corner office.',
                       'The review is in the corner office. [superseded: now "front lobby" since 2026-09-18]',
                       'It moved from the corner office to the front lobby.', 'Unrelated line.'])
    path.write_text('\n'.join([event('paired-source-worker', earlier, earlier), event('Thread-5 (<lambda>)', earlier),
                               event('Thread-6 (<lambda>)', earlier, probe), event('Thread-1 (run)')]))
    missing = {'path': 'runs/two', 'case': dict(case)}
    row = {'outcome': 'pass', 'primary_outcome': 'pass', 'elapsed_ms': 4000, 'effects': {'model_requests': []}}
    manifest = {'recipe': {}, 'pairs': [{'episode_id': episode, 'order': list(paired_report.ARMS),
        'task_sha256': 'b' * 64, 'oracle_sha256': 'c' * 64, 'arms': dict.fromkeys(paired_report.ARMS, arm_member)}
        for episode, arm_member in (('knowledge-update.01', member), ('knowledge-update.02', missing))],
        'sha256': 'd' * 64, 'comparison_key': 'e' * 64, 'label': 'test', 'evidence_mode': 'controlled',
        'dataset': {'version': 'test', 'split': 'development'},
        'comparison': {'policy': {'environment': {'endpoint_usage': 'unknown'}}}}
    monkeypatch.setattr(paired_report, 'load_manifest', lambda _: manifest)
    monkeypatch.setattr(paired_report, '_row', lambda *args: row)
    report = paired_report.summarize(tmp_path)
    for arm in paired_report.ARMS:
        dead = report['arms'][arm]['dead_values_in_context']
        # The last request carrying context replays the earlier block and adds the probe's: two dead lines.
        assert (dead['episodes_observed'], dead['episodes_with_dead_values'], dead['dead_value_lines']) == (1, 1, 2)
        assert dead['unreadable_traces'] == 0
    rendered = paired_report.markdown(report)
    assert 'Dead values in the injected context' in rendered and '1/1 episodes, 2 lines' in rendered
    assert 'corner office' not in json.dumps(report['arms']) and 'corner office' not in rendered
    assert paired_report._dead_values(tmp_path, [{'path': 'runs/one', 'case': {'id': 'x', 'oracle': {}}}])[
        'episodes_observed'] == 0


def dead_value_member(tmp_path, specs, block):
    case = {'id': 'knowledge-update.07', 'oracle': {'artifacts': specs}}
    path = tmp_path / 'runs/one/attempts/knowledge-update.07/private-trace.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'protocol': 'paired-private-trace-1', 'kind': 'model_request', 'thread': 'Thread-6',
                                'data': {'request_id': 1, 'payload': {'messages': [{'role': 'user', 'content':
                                    f'Where?\n\n<memory-context>\n{block}\n</memory-context>'}]}}}))
    return {'path': 'runs/one', 'case': case}


def spec(forbidden, expected):
    return {'path': 'answer.json', 'forbidden': [forbidden],
            'assertions': [{'path': ['value'], 'op': 'label_one_of', 'value': [expected]}]}


def test_dead_values_are_matched_in_decoded_text(tmp_path):
    """A forbidden value serialized with JSON escapes is still shown to the model."""
    block = '- {"source": "turn:s-meet"} ' + json.dumps('We meet at Café Central.')
    member = dead_value_member(tmp_path, [spec('Café Central', 'Main Library')], block)
    assert paired_report._dead_values(tmp_path, [member])['dead_value_lines'] == 1


def test_a_note_answers_only_for_the_value_it_corrects(tmp_path):
    """Three superseded facts on one line with two notes: the third is still shown as current."""
    specs = [spec('locker 42', 'locker 43'), spec('blue door', 'green door'), spec('tuesday', 'thursday')]
    line = 'Locker 42, the blue door, every Tuesday.'
    partial = line + ' [superseded: now "locker 43"] [superseded: now "green door"]'
    marked = partial + ' [superseded: now "Thursday"]'
    member = dead_value_member(tmp_path, specs, '\n'.join([partial, marked]))
    assert paired_report._dead_values(tmp_path, [member])['dead_value_lines'] == 1
    # A note with a value the spec does not expect answers for nothing; a spec without expected values
    # counts its forbidden value wherever it is shown.
    member = dead_value_member(tmp_path, [spec('tuesday', 'thursday'),
                                          {'path': 'answer.json', 'forbidden': ['r14-erased']}],
                               'Every Tuesday. [superseded: now "Monday"]\nCode r14-erased [superseded: now "x"]')
    assert paired_report._dead_values(tmp_path, [member])['dead_value_lines'] == 2


def test_a_note_naming_its_record_is_read_as_a_note(tmp_path):
    """The context names each note's record ([superseded id=<record>: ...]); the counter reads it as a note."""
    line = ('Standup [superseded id=claim:0a1b: now "Wednesday, was Tuesday" since 2026-09-19]\n'
            'Review [superseded id=c-1: rescheduled to "Friday, was Tuesday"]')
    member = dead_value_member(tmp_path, [spec('tuesday', 'thursday')], line)
    assert paired_report._dead_values(tmp_path, [member])['dead_value_lines'] == 0


def fields_spec(forbidden, *expected):
    """One artifact forbidding several values, with one expected-value assertion per field."""
    return {'path': 'answer.json', 'forbidden': list(forbidden),
            'assertions': [{'path': [f'field{n}'], 'op': 'label_one_of', 'value': list(values)}
                           for n, values in enumerate(expected)]}


def test_each_shown_superseded_value_needs_its_own_answer_within_one_artifact(tmp_path):
    """Round 3, finding 8: one field's expected value never answers for another field's stale value."""
    artifact = fields_spec(['locker 42', 'blue door', 'tuesday'], ['locker 43'], ['green door'], ['thursday'])
    line = 'Locker 42, the blue door, every Tuesday.'
    partial = line + ' [superseded: now "locker 43"] [superseded: now "green door"]'
    marked = partial + ' [superseded: now "Thursday"]'
    member = dead_value_member(tmp_path, [artifact], '\n'.join([partial, marked]))
    assert paired_report._dead_values(tmp_path, [member])['dead_value_lines'] == 1
    # Two versions of one field, each with its own note, are answered; one note for two versions is not.
    artifact = fields_spec(['corner office', 'east room'], ['front lobby'])
    both = 'Corner office, later the east room.'
    member = dead_value_member(tmp_path, [artifact], '\n'.join([
        both + ' [superseded id=c-1: now "front lobby"] [superseded id=c-2: now "front lobby"]',
        both + ' [superseded id=c-1: now "front lobby"]',
        'It moved from the corner office to the front lobby.']))
    assert paired_report._dead_values(tmp_path, [member])['dead_value_lines'] == 1
