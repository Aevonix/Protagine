"""Paired episode outcomes, without treating partial cohorts as improvement."""
from collections import Counter
import math
from pathlib import Path
import statistics

from .records import digest, read
from .report import summarize as summarize_run
from .paired_transport import TIMING_PROTOCOL

ARMS = ('base_hermes', 'protagine')
REPORT_PROTOCOL = 'paired-attribution-3'
SOURCE_REPORT_PROTOCOL = 'paired-attribution-2'
NO_OUTPUT_RULE = 'returned_native_no_output_noncompletion'
PROJECTION_BASIS = (
    'Report-only attribution correction, applied symmetrically to both arms. '
    'A returned native container with empty output counts as noncompletion only with '
    'independent serialized candidate requests and returned-model evidence, no fallback, '
    'confirmed cleanup, and consistent recorded native incomplete turns. '
    'Saved outcomes, primary attribution and checks are unchanged; artifact grading is not rerun.')
USAGE_METRICS = ('total_model_calls', 'input_tokens', 'output_tokens', 'background_model_calls')
OBSERVED_USAGE = {'total_model_calls': 'observed_model_calls', 'input_tokens': 'observed_input_tokens',
                  'output_tokens': 'observed_output_tokens', 'background_model_calls': 'observed_background_model_calls'}
TIMING_COVERAGE = ('Observed HTTP model requests across foreground and background work; '
                   'coverage is incomplete and endpoint traffic may be shared. '
                   'Request timing is not user-turn latency or serving capacity.')
TIMING_DEFINITIONS = {
    'first_generated_ms': 'Client-observed first nonempty reasoning, text or tool payload in completed SSE responses. '
        'Role and empty frames are excluded; this is not server-internal token timing.',
    'first_content_ms': 'Client-observed first nonempty nonreasoning text in completed SSE responses. '
        'Tool-only responses have no value; this is not whole-task final-answer latency.',
    'request_elapsed_ms': 'Client-observed request wall time through response completion/close for completed HTTP 2xx responses, '
        'including queueing, prefill, generation and client response consumption.',
    'request_output_tokens_per_second': 'Provider-reported completion tokens divided by client-observed request seconds, '
        'for completed HTTP 2xx responses with token usage. Counts may include reasoning and tool tokens; '
        'this is end-to-end output TPS, not decode TPS or aggregate serving throughput.',
    'episode_elapsed_ms': 'Runner wall time per arm episode, including model calls, tools, fixed settling, '
        'container startup and cleanup. Observed failed attempts are included.',
}


def load_manifest(directory):
    manifest = read(Path(directory) / 'paired.json')
    if (manifest.get('schema') != 1 or manifest.get('kind') != 'paired_qualification'
            or manifest.get('orchestrator') != 'paired-runner-1'
            or manifest.get('sha256') != digest({key: value for key, value in manifest.items() if key != 'sha256'})
            or manifest.get('comparison_key') != digest(manifest.get('comparison'))):
        raise ValueError('Invalid frozen paired manifest')
    return manifest


def _row(directory, manifest, member):
    relative = Path(member['path'])
    if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'runs':
        raise ValueError('Invalid paired attempt path')
    path = Path(directory) / relative
    if not (path / 'run.json').exists():
        return {'case_id': member['case']['id'], 'outcome': 'not_run',
                'primary_outcome': 'unverified', 'elapsed_ms': None, 'effects': {}}
    run = read(path / 'run.json')
    if (run.get('cases') != [member['case']] or run.get('recipe') != manifest['recipe']
            or run.get('recipe_sha256') != member['recipe_sha256']
            or run.get('implementation_sha256') != manifest['execution_implementation_sha256']
            or run.get('evidence_mode') != manifest['evidence_mode']
            or run.get('suite_version') != manifest['dataset']['version']):
        raise ValueError('Paired result does not match its frozen arm recipe and task')
    return summarize_run(path)['cases'][0]


def _completion(row):
    if row.get('cleanup') in {'state_directory_retained', 'unconfirmed', 'failed'}:
        return None
    if row['outcome'] == 'timeout':
        routing = row.get('qualification_routing') or {}
        binding, role = routing.get('binding'), row.get('role') or routing.get('role')
        observations = [item for item in row.get('observations', []) if item.get('role') == role]
        dispatched = bool(binding and observations) and all(
            item.get('selected_binding') == binding and item.get('prior_attempts') == []
            and (item.get('dispatch_observed') is True
                 or item.get('attribution_basis') == 'serialized_requests_and_returned_models')
            for item in observations)
        return False if dispatched else None
    # An exception in setup, a consumer, or the verifier is not evidence that
    # the model failed the task. Returned episodes use the ordinary fail outcome.
    if row['outcome'] in {'pass', 'fail'} and row.get('primary_outcome') in {'pass', 'fail'}:
        return row['outcome'] == row['primary_outcome'] == 'pass'
    return None


def _no_output_projection(row, recipe, case):
    """Recover skipped attribution, never rerun a consumer or artifact verifier.

    runner's no_output branch precedes both attribution and artifact assessment.
    An independently recorded incomplete native turn necessarily fails the frozen
    all_native_turns_completed check, regardless of any workspace artifact.
    """
    if (row.get('outcome') != 'fail' or row.get('failure_category') != 'no_output'
            or row.get('primary_outcome') != 'unverified' or row.get('checks') != {}
            or 'output' not in row or row['output'] not in (None, '', {})
            or row.get('evidence_mode') != 'actual_inference'
            or row.get('cleanup') != 'state_directory_removed'
            or recipe.get('consumer') != 'disposable_paired_native_container'
            or case.get('consumer') != 'native_paired' or case.get('boundary') != 'native_hermes'
            or case.get('evaluator') != 'paired_artifacts'):
        return None
    binding, model = recipe.get('binding'), recipe.get('configured_model')
    role = case.get('role')
    routing = row.get('qualification_routing') or {}
    observations = row.get('observations')
    if (not binding or not model or not role or row.get('role') != role or not isinstance(routing, dict)
            or routing.get('binding') != binding or routing.get('role') != role
            or not isinstance(observations, list) or len(observations) != 1):
        return None
    observation = observations[0]
    image_id = (recipe.get('container') or {}).get('image_id')
    if (not isinstance(observation, dict) or not image_id
            or observation.get('boundary') != 'paired_native_container'
            or observation.get('role') != role or observation.get('selected_binding') != binding
            or observation.get('outcome') != 'returned'
            or type(observation.get('exit_code')) is not int or observation['exit_code'] != 0
            or observation.get('error_type') is not None or observation.get('error_origin_stage') is not None
            or observation.get('prior_attempts') != [] or observation.get('dispatch_observed') is not True
            or observation.get('attribution_basis') != 'serialized_requests_and_returned_models'
            or observation.get('container_removed') is not True or observation.get('image_id') != image_id):
        return None
    effects = row.get('effects') or {}
    if (not isinstance(effects, dict)
            or effects.get('container_removed') is not True or effects.get('image_id') != image_id
            or effects.get('state_isolation') != 'fresh_tmpfs_per_arm_and_episode'
            or observation.get('state_isolation') != effects.get('state_isolation')):
        return None
    requests = effects.get('model_requests')
    returned_model = observation.get('returned_model')
    if (not isinstance(requests, list) or not requests or not returned_model
            or any(not isinstance(request, dict) or request.get('model') != model
                   or not isinstance(request.get('returned_models', []), list)
                   or any(value != returned_model for value in request.get('returned_models', []))
                   for request in requests)):
        return None
    successful = [request for request in requests if type(request.get('status')) is int
                  and 200 <= request['status'] < 300]
    if (not successful or any(not request.get('returned_models') for request in successful)
            or not any(request.get('response_complete') is True for request in successful)):
        return None
    declared, completed = effects.get('declared_turns'), effects.get('turns_completed')
    turns = effects.get('turns')
    expected = (case.get('oracle') or {}).get('declared_turns')
    if (type(declared) is not int or type(completed) is not int or type(expected) is not int
            or declared != expected or declared != len((case.get('inputs') or {}).get('episodes', []))
            or not 0 <= completed < declared or not isinstance(turns, list)
            or len(turns) != completed + 1 or not all(isinstance(turn, dict) for turn in turns)
            or any(turn.get('completed') is not True for turn in turns[:-1])
            or turns[-1].get('completed') is not False
            or turns[-1].get('final_response') != row['output']):
        return None
    return {'rule': NO_OUTPUT_RULE, 'source_row_sha256': digest(row)}


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def _accounting(rows):
    result = {}
    for metric in USAGE_METRICS:
        observations = [(row.get('effects') or {}).get('resource_usage') or {} for row in rows]
        totals = [entry[metric] for entry in observations if _number(entry.get(metric))]
        known = [entry[metric] if _number(entry.get(metric)) else entry.get(OBSERVED_USAGE[metric])
                 for entry in observations]
        known = [value for value in known if _number(value)]
        complete = bool(rows) and len(totals) == len(rows) and all(
            entry.get('coverage') == 'complete' for entry in observations)
        result[metric] = {'total': sum(totals) if complete else None,
            'observed_total': sum(known) if known else None, 'observed_episodes': len(known),
            'declared_episodes': len(rows), 'coverage': 'complete' if complete else 'partial' if known else 'unobserved'}
    elapsed = [row['elapsed_ms'] for row in rows if _number(row.get('elapsed_ms'))]
    result['runner_elapsed_ms'] = {'total': sum(elapsed) if len(elapsed) == len(rows) else None,
        'observed_total': sum(elapsed) if elapsed else None, 'observed_episodes': len(elapsed),
        'basis': 'Observed arm wall time including consumer/cleanup; not model-only latency or queue isolation.'}
    return result


def _timing(rows):
    requests = [request for row in rows for request in (row.get('effects') or {}).get('model_requests', [])
                if isinstance(request, dict)]
    instrumented = [request for request in requests if request.get('timing_protocol') == TIMING_PROTOCOL]
    completed = [request for request in instrumented if request.get('response_complete') is True
                 and type(request.get('status')) is int and 200 <= request['status'] < 300]
    streaming = [request for request in completed if request.get('response_mode') == 'sse']
    with_usage = [request for request in completed if isinstance(request.get('usage'), dict)
                  and type(request['usage'].get('completion_tokens')) is int
                  and request['usage']['completion_tokens'] >= 0]
    metrics = {}

    def add(name, values, eligible, unit='ms'):
        values = [value for value in values if _number(value)]
        metrics[name] = {'samples': len(values), 'eligible_samples': eligible,
            'median': statistics.median(values) if values else None,
            'min': min(values) if values else None, 'max': max(values) if values else None,
            'unit': unit, 'definition': TIMING_DEFINITIONS[name]}

    for name in ('first_generated_ms', 'first_content_ms'):
        add(name, [request.get(name) for request in streaming], len(streaming))
    add('request_elapsed_ms', [request.get('elapsed_ms') for request in completed], len(completed))
    add('request_output_tokens_per_second', [request['usage']['completion_tokens'] * 1000 / request['elapsed_ms']
        for request in with_usage if _number(request.get('elapsed_ms')) and request['elapsed_ms'] > 0],
        len(completed), 'tokens/s')
    add('episode_elapsed_ms', [row.get('elapsed_ms') for row in rows], len(rows))
    return {'protocol': TIMING_PROTOCOL, 'coverage': TIMING_COVERAGE,
        'requests': {'observed': len(requests), 'instrumented': len(instrumented),
            'completed': len(completed), 'streaming': sum(request.get('response_mode') == 'sse' for request in instrumented),
            'with_usage': len(with_usage), 'with_first_generated': metrics['first_generated_ms']['samples'],
            'with_first_content': metrics['first_content_ms']['samples']},
        'metrics': metrics, 'decode_tokens_per_second': None}


def summarize(directory, *, report_protocol=REPORT_PROTOCOL):
    if report_protocol not in {SOURCE_REPORT_PROTOCOL, REPORT_PROTOCOL}:
        raise ValueError('Unknown paired reporting protocol')
    manifest = load_manifest(directory)
    rows = {arm: [] for arm in ARMS}
    pairs = []
    for declared in manifest['pairs']:
        results = {arm: _row(directory, manifest, declared['arms'][arm]) for arm in ARMS}
        for arm in ARMS:
            rows[arm].append(results[arm])
        complete = {arm: _completion(results[arm]) for arm in ARMS}
        projections = {arm: None for arm in ARMS}
        if report_protocol == REPORT_PROTOCOL:
            for arm in ARMS:
                if complete[arm] is None:
                    projections[arm] = _no_output_projection(
                        results[arm], manifest['recipe'], declared['arms'][arm]['case'])
                    if projections[arm] is not None:
                        complete[arm] = False
        comparable = all(value is not None for value in complete.values())
        difference = int(complete['protagine']) - int(complete['base_hermes']) if comparable else None
        pairs.append({'episode_id': declared['episode_id'], 'order': declared['order'],
            'task_sha256': declared['task_sha256'], 'oracle_sha256': declared['oracle_sha256'],
            'results': results, 'completion': complete, 'comparable': comparable,
            'comparison': ('win' if difference > 0 else 'loss' if difference < 0 else 'tie')
                          if difference is not None else 'unavailable'})
        if report_protocol == REPORT_PROTOCOL:
            pairs[-1]['completion_projection'] = projections
    declared_count = len(pairs)
    comparable_count = sum(pair['comparable'] for pair in pairs)
    full = declared_count > 0 and comparable_count == declared_count
    arms = {}
    for arm in ARMS:
        completed = sum(pair['completion'][arm] is True for pair in pairs)
        observed_completed = sum(row['outcome'] == 'pass' for row in rows[arm])
        arms[arm] = {'declared_episodes': declared_count, 'outcomes': dict(Counter(row['outcome'] for row in rows[arm])),
            'observed_completed': observed_completed, 'attributed_completed': completed,
            'completion_percent': 100 * completed / declared_count if full else None,
            'accounting': _accounting(rows[arm]), 'timing': _timing(rows[arm])}
    score = None
    if full:
        counts = Counter(pair['comparison'] for pair in pairs)
        score = {'episodes': declared_count, 'base_hermes_completed': arms['base_hermes']['attributed_completed'],
            'protagine_completed': arms['protagine']['attributed_completed'],
            'base_hermes_completion_percent': arms['base_hermes']['completion_percent'],
            'protagine_completion_percent': arms['protagine']['completion_percent'],
            'delta_percentage_points': 100 * (arms['protagine']['attributed_completed']
                - arms['base_hermes']['attributed_completed']) / declared_count,
            'wins': counts['win'], 'ties': counts['tie'], 'losses': counts['loss'],
            'both_completed': sum(all(pair['completion'].values()) for pair in pairs),
            'neither_completed': sum(not any(pair['completion'].values()) for pair in pairs)}
    report = {'schema': 1, 'kind': 'paired_report', 'report_protocol': report_protocol,
        'manifest_sha256': manifest['sha256'],
        'comparison_key': manifest['comparison_key'], 'label': manifest['label'],
        'evidence_mode': manifest['evidence_mode'], 'dataset': manifest['dataset'],
        'policy': manifest['comparison']['policy'], 'declared_episodes': declared_count,
        'budget_verification': 'unverified',
        'comparable_pairs': comparable_count, 'unavailable_pairs': declared_count - comparable_count,
        'arms': arms, 'pairs': pairs, 'paired_score': score, 'tier': None,
        'qualification_status': 'insufficient_evidence',
        'basis': 'Descriptive development-episode results, not a model ranking or causal performance estimate. '
                 'No aggregate delta until every declared episode has an attributable pair. '
                 'Returned attributable task failures count as noncompletion. Timeouts count only with '
                 'observed dispatch to the candidate; infrastructure/consumer errors, unsupported, setup, '
                 'interrupted and unattributed attempts remain unavailable. '
                 'Declared budgets do not prove equal total work.'}
    if report_protocol == REPORT_PROTOCOL:
        report['completion_projection'] = {
            'source_protocol': SOURCE_REPORT_PROTOCOL, 'rule': NO_OUTPUT_RULE,
            'corrected_episodes': sum(value is not None for pair in pairs
                                      for value in pair['completion_projection'].values()),
            'raw_results_preserved': True, 'artifact_verifier_reexecuted': False,
            'basis': PROJECTION_BASIS}
        report['basis'] += ' ' + PROJECTION_BASIS
    return report


def markdown(report):
    lines = ['# Paired Hermes episode results', '', report['basis'], '',
        f"Dataset: {report['dataset']['version']} ({report['dataset']['split']}). "
        f"Comparable pairs: {report['comparable_pairs']}/{report['declared_episodes']}.", '',
        '| Arm | Attributed completions | Recorded outcomes |', '| --- | --- | --- |']
    for arm in ARMS:
        row = report['arms'][arm]
        lines.append(f"| {arm} | {row['attributed_completed']}/{row['declared_episodes']} | {row['outcomes']} |")
    score = report['paired_score']
    lines.extend(['', (f"Completion delta: {score['delta_percentage_points']:+.1f} percentage points. "
        f"Wins/ties/losses: {score['wins']}/{score['ties']}/{score['losses']}.") if score else
        'Paired aggregate unavailable. Complete pairs remain visible without scoring the partial cohort.', '',
        '| Episode | Base Hermes | Hermes + Protagine | Pair |', '| --- | --- | --- | --- |'])
    for pair in report['pairs']:
        lines.append(f"| {pair['episode_id']} | {pair['results']['base_hermes']['outcome']} | "
                     f"{pair['results']['protagine']['outcome']} | {pair['comparison']} |")
    lines.extend(['', '## Observed resource use', '',
        'Unknown totals stay unknown. Partial observations are not full-stack cost.', '',
        '| Arm | Metric | Total | Observed subtotal | Coverage |', '| --- | --- | --- | --- | --- |'])
    for arm in ARMS:
        for metric in USAGE_METRICS:
            row = report['arms'][arm]['accounting'][metric]
            show = lambda value: 'unknown' if value is None else str(value)
            lines.append(f"| {arm} | {metric} | {show(row['total'])} | {show(row['observed_total'])} | {row['coverage']} |")
    lines.extend(['', '## Observed timings', '', TIMING_COVERAGE, '',
        '| Arm | Metric | Median | Min | Max | Samples / eligible |', '| --- | --- | --- | --- | --- | --- |'])
    for arm in ARMS:
        for name, metric in report['arms'][arm]['timing']['metrics'].items():
            values = ['unknown' if metric[key] is None else f"{metric[key]:.3f} {metric['unit']}"
                      for key in ('median', 'min', 'max')]
            lines.append(f"| {arm} | {name} | {' | '.join(values)} | {metric['samples']}/{metric['eligible_samples']} |")
    lines.extend(['', 'Decode TPS is unmeasured. Buffered JSON responses do not provide first-payload timing.', ''])
    lines.extend(f'- `{name}`: {definition}' for name, definition in TIMING_DEFINITIONS.items())
    lines.extend(['', 'No model tier is assigned. Budget enforcement and endpoint isolation require independent evidence.',
                  f"Endpoint usage (self-reported): {report['policy']['environment']['endpoint_usage']}."])
    return '\n'.join(lines) + '\n'
