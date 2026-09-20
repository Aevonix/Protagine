"""Allowlisted serving measurements, separate from native agent qualification.

Exports timing and oracle scalars, never prompts, answers or raw stream text.
The serving client's unverified peak-concurrency estimator is not published.
"""
import hashlib
import math
from pathlib import Path
import statistics

from .public_export import identifier, number, public_deployment, stamp, text
from .records import digest, read

VERSION = 'serving-performance-v1'
CACHES = {'warm_engine_unique_prefixes', 'warm_prefix_cache', 'cold_engine', 'unknown'}


def _hash(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Missing measurement identity')
    return value


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError('Invalid measurement count')
    return value


def _unique(rows):
    mapped = {_hash(row['prompt_sha256']): row for row in rows}
    if len(mapped) != len(rows):
        raise ValueError('Repeated prompts need a distinct repetition run')
    return mapped


def _quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered)-1)*q
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo]+(ordered[hi]-ordered[lo])*(index-lo)


def _peak(timing):
    """Count actual overlapping intervals from one client's monotonic clock."""
    intervals = []
    for row in timing:
        start, end = number(row.get('request_started_monotonic_s')), number(row.get('request_finished_monotonic_s'))
        if start is None or end is None:
            return None
        if end < start:
            raise ValueError('Reversed request interval')
        if end == start:
            continue
        intervals.extend([(start, 1), (end, -1)])
    current = peak = 0
    for _, change in sorted(intervals):
        current += change
        peak = max(peak, current)
    return peak if timing else None


def export_serving_record(directory, metadata, dataset_manifest, dataset_key):
    """Build an immutable public record from a completed or interrupted cell.

    ``metadata.serving`` declares the public target, cache and token boundaries.
    ``metadata.expected_returned_model`` is a private exact model attribution
    label; it is checked against observed responses and never exported.
    Missing attempts and missing grades remain in the denominator.
    """
    root = Path(directory)
    launch, client, correctness = (read(root/name) for name in ('launch.json', 'sglang.json', 'correctness-001.json'))
    manifest = read(dataset_manifest)
    dataset = manifest['datasets'][dataset_key]
    declared = _unique(dataset['cases'])
    if not 1 <= len(declared) <= 10000:
        raise ValueError('Declare a bounded nonempty request set')
    timing = _unique(_timings(root/'timing.jsonl'))
    grades = _unique(correctness['cases'])
    if not set(timing).issubset(declared) or not set(grades).issubset(declared):
        raise ValueError('Unregistered performance request')
    if _hash(launch['dataset_sha256']) != _hash(dataset['sha256']):
        raise ValueError('Launched dataset differs from declared fixture')
    if _count(correctness['passed']) != sum(row.get('pass') is True for row in grades.values()):
        raise ValueError('Correctness aggregate differs from case receipts')
    if _count(correctness['independent_tasks']) != len(grades):
        raise ValueError('Correctness task count differs from case receipts')
    if any(type(row.get('success')) is not bool or type(row.get('deadline_exceeded')) is not bool for row in timing.values()):
        raise ValueError('Invalid transport status')
    deployment = public_deployment(metadata)
    config = metadata['serving']
    target, concurrency = _count(config['target_input_tokens']), _count(config['client_concurrency_limit'])
    if not target or not concurrency or _count(client['max_concurrency']) != concurrency or config['cache_state'] not in CACHES:
        raise ValueError('Invalid declared serving cell')
    accounting = config['token_accounting']
    if accounting not in {'completion_includes_reasoning', 'final_only', 'unknown'}:
        raise ValueError('Unknown completion-token boundary')
    interval_basis = config['stream_interval_basis']
    if interval_basis not in {'sse_chunks', 'not_measured'}:
        raise ValueError('Unknown stream interval boundary')
    expected = metadata['expected_returned_model']
    if not isinstance(expected, str) or not expected:
        raise ValueError('Explicit private model identity required')
    cases = []
    for index, prompt in enumerate(declared, 1):
        row, grade = timing.get(prompt), grades.get(prompt)
        outcome, primary, duration = 'not_run', 'unverified', None
        if row:
            duration = number(row.get('elapsed_ms'))
            if row.get('deadline_exceeded') is True:
                outcome = 'timeout'
            elif row.get('success') is not True:
                outcome = 'error'
            elif grade:
                outcome = 'pass' if grade.get('pass') is True and grade.get('success') is True and grade.get('untruncated_final') is True else 'fail'
            if row.get('candidate_model') == expected and row.get('returned_models') == [expected]:
                primary = 'pass' if outcome == 'pass' else 'fail' if outcome != 'not_run' else 'unverified'
        cases.append({'case_id': f'request-{index:02d}', 'title': f'Synthetic request {index:02d}',
            'outcome': outcome, 'primary_outcome': primary,
            'failure_stage': None if outcome == 'pass' else 'answer_checks' if outcome == 'fail' else outcome,
            'duration_ms': duration, 'summary': None})
    observed = list(timing.values())
    usage = [row.get('server_usage') for row in observed]
    complete_usage = bool(usage) and all(isinstance(u, dict)
        and type(u.get('prompt_tokens')) is int and u['prompt_tokens'] >= 0
        and type(u.get('completion_tokens')) is int and u['completion_tokens'] >= 0 for u in usage)
    input_tokens = [u['prompt_tokens'] for u in usage] if complete_usage else []
    completion_tokens = sum(u['completion_tokens'] for u in usage) if complete_usage else None
    duration = number(client.get('duration'))
    if duration == 0:
        raise ValueError('Empty measurement interval')
    success = sum(row.get('success') is True and row.get('deadline_exceeded') is not True for row in observed)
    metrics = []

    def metric(identity, label, value, unit, samples, definition):
        metrics.append({'id': identity, 'label': label, 'value': value, 'unit': unit,
                        'samples': samples, 'definition': definition})

    metric('completion_throughput_tps', 'End-to-end completion throughput',
        completion_tokens/duration if completion_tokens is not None and duration else None, 'tok/s', len(observed),
        'Server-reported completion tokens divided by the entire measured batch duration, including prefill, waiting and failed requests. Token accounting is stated in the serving cell.')
    for field, identity, label, explanation in [
        ('first_generated_delta_ms', 'first_generated_median_ms', 'First generated output, median',
         'First nonempty reasoning or content delta; this may precede any visible answer.'),
        ('first_content_delta_ms', 'first_final_content_median_ms', 'First final content, median',
         'First nonempty final-content delta; arrival does not establish correctness or usefulness.'),
        ('elapsed_ms', 'completion_median_ms', 'Request completion, median',
         'Full request duration, including reasoning; includes failures with recorded duration.'),
    ]:
        values = [number(row.get(field)) for row in observed
                  if field != 'first_content_delta_ms' or row.get('content_contains_think_tag') is False]
        values = [value for value in values if value is not None]
        metric(identity, label, statistics.median(values) if values else None, 'ms', len(values),
            explanation+' Includes every response with that timing; missing timings are not zero.')
    values = [row['elapsed_ms'] for row in observed if number(row.get('elapsed_ms')) is not None]
    metric('completion_p95_ms', 'Request completion, p95', _quantile(values, .95), 'ms', len(values),
        'Linear-interpolated p95 over all recorded request durations, including failures. Small samples give an unstable tail estimate.')
    chunks = [number(item) for sequence in client.get('itls', []) for item in sequence] if interval_basis == 'sse_chunks' else []
    chunks = [value*1000 for value in chunks if value is not None]
    if chunks:
        metric('stream_chunk_interval_median_ms', 'Stream chunk interval, median', statistics.median(chunks), 'ms', len(chunks),
            'Observed SSE intervals reported by the serving client. Speculative decoding can emit several tokens per chunk; this is not per-token latency.')
    metric('batch_duration_seconds', 'Measured batch duration', duration, 's', 1 if duration is not None else 0,
        'Client measurement interval, excluding process setup. This is the denominator for aggregate completion throughput.')
    record = {'schema_version': 1, 'run_id': identifier(metadata['public_id']),
        'started_at': stamp(launch['created_at']), 'completed_at': stamp(launch['finished_at']) if launch.get('finished_at') else None,
        'status': 'completed' if len(timing) == len(declared) and len(grades) == len(declared) and launch.get('finished_at') else 'aborted',
        'evidence_mode': 'actual_inference', 'role': 'serving', 'boundary': 'role_completion', 'phase': 'performance',
        'suite_version': VERSION, 'suite_sha256': digest({'dataset_version': manifest['version'], 'dataset': dataset['sha256'], 'prompts': list(declared)}),
        'evaluator_sha256': digest({'scorer': correctness['scorer'], 'exporter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}),
        'comparison_key': None, 'deployment': deployment,
        'performance': {'version': 'serving-cell-v1', 'target_input_tokens': target,
            'actual_input_tokens_min': min(input_tokens) if input_tokens else None,
            'actual_input_tokens_max': max(input_tokens) if input_tokens else None,
            'client_concurrency_limit': concurrency, 'observed_peak_inflight': _peak(observed),
            'cache_state': config['cache_state'], 'transport_successes': success,
            'token_accounting': accounting, 'stream_interval_basis': interval_basis},
        'counts': {'declared': len(cases), 'primary_passes': sum(c['primary_outcome'] == 'pass' for c in cases),
            'outcomes': {outcome: sum(c['outcome'] == outcome for c in cases) for outcome in sorted({c['outcome'] for c in cases})}},
        'metrics': metrics, 'cases': cases,
        'conditions': [
            {'label': 'Timing observer SHA-256', 'value': _hash(launch['observer_sha256'])},
            {'label': 'Dataset SHA-256', 'value': _hash(dataset['sha256'])},
            {'label': 'Correctness scorer', 'value': text(correctness['scorer'])},
            {'label': 'Peak-overlap basis', 'value': 'Same-client monotonic request intervals' if _peak(observed) is not None else 'Not recorded; serving-client peak estimates are excluded'},
        ] + [{'label': text(row['label']), 'value': text(row['value'])} for row in metadata.get('conditions', [])],
        'limitations': [
            'Prompt-only development workloads; these do not exercise native agent loops or establish held-out quality.',
            'Input-size labels are targets. Actual server token usage differs across tokenizers and does not establish a maximum usable context window.',
            'Client concurrency limits and configured server slots are not observed overlap or maximum sustainable capacity.',
            'Observed overlap, when available, counts client request spans; it is not a count of active GPU sequences.',
            'No throughput ranking or comparison delta is authorized by this record; matching conditions have not been established.',
            'Transport completion and independently graded answer correctness are separate. Missing attempts and grades stay in the denominator.',
            'First final-content arrival is a channel timing, not proof that the content is useful or correct.',
            'Final-content timings exclude responses flagged for thinking tags in that channel, or lacking that observation; separate reasoning duration is not inferred.',
        ] + [text(value) for value in metadata.get('limitations', [])], 'evidence': []}
    record['run_id'] = identifier(record['run_id']+'-'+digest(record)[:12])
    return record


def _timings(path):
    import json
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
