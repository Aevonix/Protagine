"""The comparator's noise is reported beside its score.

The heartbeat arm's score moved 13-14/28 to 22/28 between runs on byte-identical prompts, mostly on
output delivered where nothing was due: a silence marker Hermes delivers because it sat mid-line, and
"nothing to do" status reports. The report now shows, beside every arm's score, its tick sends, how
many carried a silence marker, and how many control episodes (nothing warranted) had any tick send.
Descriptive only: nothing is graded differently.
"""

from __future__ import annotations

from protagine.qualification import paired_report


def _row(*ticks, replies=()):
    """An episode whose body ran ``ticks`` (each a list of texts delivered during that tick)."""
    outbox, rows = [], []
    for text in replies:
        outbox.append({'via': 'reply', 'text': text, 'target': 'capture:owner'})
    for index, texts in enumerate(ticks, start=1):
        before = len(outbox)
        outbox.extend({'via': 'platform', 'text': text, 'target': 'capture:owner'} for text in texts)
        rows.append({'tick': index, 'outbox_before': before, 'outbox_after': len(outbox)})
    return {'outcome': 'pass', 'effects': {'body': {'ticks': rows, 'outbox': outbox}}}


def _case(action):
    return {'oracle': {'body': {'action': action}}}


def test_tick_output_counts_leaked_markers_and_controls_that_sent():
    rows = [_row(['Checked in: nothing to do. [SILENT]'], [], replies=['Noted.']),        # control, leaked marker
            _row(['Reminder: send the brief.']),                                            # warranted, a send
            _row([], ['All quiet, nothing due.']),                                          # control, status report
            _row([], []),                                                                   # control, silent
            {'outcome': 'pass', 'effects': {'turns': []}}]                                  # no ticks at all
    cases = [_case('none'), _case({'token': 'brief'}), _case('none'), _case('none'), _case('none')]
    output = paired_report._tick_output(rows, cases)
    assert output['episodes_with_ticks'] == 4 and output['tick_sends'] == 3
    assert output['silence_marker_sends'] == 1 and output['silence_marker_rate'] == 1 / 3
    assert output['control_episodes'] == 3 and output['control_episodes_with_tick_sends'] == 2
    assert 'never graded' in output['basis']


def test_a_word_is_not_a_marker_unless_it_is_the_marker():
    rows = [_row(['The room went silent after the call.', 'NO_REPLY needed here', 'SILENT'])]
    assert paired_report._tick_output(rows, [_case('none')])['silence_marker_sends'] == 2


def test_the_report_shows_it_beside_every_arms_score(tmp_path, monkeypatch):
    from test_qualification_paired_resources import request, trace
    member, _ = trace(tmp_path, [(1, 'paired-source-worker')])
    member = {**member, 'case': {**member.get('case', {}), 'oracle': {'body': {'action': 'none'}}}}
    row = {**_row(['Checked in: nothing to do. [SILENT]']), 'primary_outcome': 'pass', 'elapsed_ms': 4000}
    row['effects'].update(model_requests=[request(1)], turns=[], declared_turns=1, turns_completed=1)
    manifest = {'recipe': {}, 'pairs': [{'episode_id': 'paired.test', 'order': list(paired_report.ARMS),
        'task_sha256': 'b' * 64, 'oracle_sha256': 'c' * 64, 'arms': dict.fromkeys(paired_report.ARMS, member)}],
        'sha256': 'd' * 64, 'comparison_key': 'e' * 64, 'label': 'test', 'evidence_mode': 'controlled',
        'dataset': {'version': 'test', 'split': 'development'},
        'comparison': {'policy': {'environment': {'endpoint_usage': 'unknown'}}}}
    monkeypatch.setattr(paired_report, 'load_manifest', lambda _: manifest)
    monkeypatch.setattr(paired_report, '_row', lambda *args: row)
    report = paired_report.summarize(tmp_path)
    for arm in paired_report.ARMS:
        assert report['arms'][arm]['tick_output']['silence_marker_sends'] == 1
    rendered = paired_report.markdown(report)
    assert '| Tick output |' in rendered and '1/1 carried a silence marker; 1/1 controls sent' in rendered
