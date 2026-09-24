"""Paired episode outcomes, without treating partial cohorts as improvement."""
from collections import Counter
import json
import math
from pathlib import Path
import statistics

from .records import digest, read
from .report import summarize as summarize_run
from .paired_statistics import ALPHA, MIN_WINS, contrast
from .paired_transport import TIMING_PROTOCOL
from .paired_trace import PROTOCOL as TRACE_PROTOCOL
from .paired_worker import LEGACY_PROFILES

ARMS = ('base_hermes', 'protagine')
STATISTICS_PROTOCOL = 'paired-statistics-1'
DEFAULT_RULE = {'test': 'sign_exact', 'alpha': ALPHA, 'min_wins': MIN_WINS, 'ci': 'cluster_bootstrap_95',
                'unit': 'scenario', 'non_inferior_pp': -10}
STATISTICS_BASIS = (
    'Units are scenarios: repetitions of one scenario are averaged into one pass value per arm '
    'before testing. A win means the treatment exceeds the comparator on that scenario. '
    'Demonstrated needs all three: exact two-sided sign test over non-tied units under alpha, '
    'at least the minimum number of winning scenarios, and a cluster-bootstrap interval whose '
    'lower bound is above zero. The MDE is the smallest treatment-minus-comparator difference '
    'this many units finds with 80% power at the observed disagreement rate, modelled as one '
    'binary outcome per unit; with repetitions it is an upper bound on the pass-rate difference. '
    'A contrast with any '
    'unattributed unit is unavailable; no partial cohort is tested. Public development scenarios '
    'give a development estimate, not a held-out claim.')
PROBE_STATISTICS_BASIS = (
    'Units are campaign probes: the warranted and control artifact checks of each campaign (an artifact '
    'spec declaring probe.kind), repetitions averaged into one pass value per probe and arm. A win means '
    'the treatment passes a probe the comparator does not. Demonstrated needs all three: exact two-sided '
    'sign test over non-tied probes under alpha, at least the minimum number of winning probes, and a '
    'bootstrap interval that resamples whole campaigns (their probes share one training) whose lower bound '
    'is above zero. The MDE is computed over probes and ignores the clustering. A campaign is unavailable '
    'when either arm\'s attempt is unattributed or ended before its last declared entry; a contrast with '
    'any unavailable campaign is unavailable, and no partial cohort is tested.')
CAMPAIGN_BASIS = (
    'Descriptive campaign rows. The old-family row is a point-estimate non-inferiority over campaigns '
    'available in both arms (a campaign passes when every old-family artifact passes). Cost per success '
    'is observed model calls and tokens per passed warranted or control probe. Forbidden hits count probe '
    'files whose raw text holds a declared forbidden token. Lesson diagnostics read the mind\'s own '
    'lesson record at episode end; an arm without it is unavailable. Classes, probe kinds, blocks and '
    'training rows count probes of the arm\'s own available campaigns.')
UNIT_PROBES = ('warranted', 'control')
GPU_HOURS_BASIS = (
    'Sum of observed arm wall time on the declared endpoint, including tools, settling and '
    'container cleanup. It equals GPU-hours only at concurrency 1 on one endpoint; GPU '
    'utilization is not measured, and unobserved episodes are not zero.')
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
WORKLOAD_PROTOCOL = 'paired-request-workloads-1'
WORKLOADS = ('foreground', 'background', 'unknown')
WORKLOAD_BASIS = (
    'Request workload is taken from explicit recorded workload metadata, or joined by unique '
    'trace_request_id to a private model_request event on paired-source-worker (background). '
    'Generic helper threads do not distinguish Hermes foreground from background review; '
    'missing, ambiguous or conflicting evidence stays unknown. Coverage describes observed '
    'HTTP requests only, not all model work. Tokens are resource observations, not monetary cost.')


def load_manifest(directory):
    manifest = read(Path(directory) / 'paired.json')
    if (manifest.get('schema') != 1 or manifest.get('kind') != 'paired_qualification'
            or manifest.get('orchestrator') != 'paired-runner-1'
            or manifest.get('sha256') != digest({key: value for key, value in manifest.items() if key != 'sha256'})
            or manifest.get('comparison_key') != digest(manifest.get('comparison'))):
        raise ValueError('Invalid frozen paired manifest')
    return manifest


def arms_of(manifest):
    """Arm labels, comparator, profiles and rule; plans before profiles carry the built-in pair."""
    comparison = manifest.get('comparison') or {}
    arms = list(comparison.get('arms') or ARMS)
    reference = comparison.get('reference_arm') or arms[0]
    profiles = comparison.get('profiles') or {arm: LEGACY_PROFILES[arm] for arm in arms}
    if (len(arms) < 2 or len(set(arms)) != len(arms) or reference not in arms
            or set(profiles) != set(arms)):
        raise ValueError('Invalid frozen arm declaration')
    return arms, reference, profiles, comparison.get('rule') or DEFAULT_RULE


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


def _workload_observations(directory, member, row):
    """Read only thread/ID evidence; do not copy private trace payloads into reports."""
    requests = [request for request in (row.get('effects') or {}).get('model_requests', [])
                if isinstance(request, dict)]
    if not requests:
        return [], 'no_requests'
    root = Path(directory).resolve()
    path = root / member['path'] / 'attempts' / member['case']['id'] / 'private-trace.jsonl'
    threads, trace_status = {}, 'absent'
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Unsafe diagnostic path')
        if path.exists():
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError('Oversized diagnostic trace')
            for line in path.read_text().splitlines():
                event = json.loads(line)
                if not isinstance(event, dict) or event.get('protocol') != TRACE_PROTOCOL or event.get('kind') != 'model_request':
                    continue
                data = event.get('data')
                if not isinstance(data, dict) or type(data.get('request_id')) is not int:
                    continue
                identity = data['request_id']
                # Duplicate IDs cannot identify a request, even when threads agree.
                threads[identity] = None if identity in threads else event.get('thread')
            trace_status = 'read'
    except (OSError, ValueError, RuntimeError):
        threads, trace_status = {}, 'invalid'
    counts = Counter(request.get('trace_request_id') for request in requests
                     if type(request.get('trace_request_id')) is int)
    observations = []
    for request in requests:
        explicit = request.get('workload')
        explicit = explicit if isinstance(explicit, str) and explicit in WORKLOADS[:2] else None
        identity = request.get('trace_request_id')
        source_worker = (type(identity) is int and counts[identity] == 1
                         and threads.get(identity) == 'paired-source-worker')
        if explicit == 'foreground' and source_worker:
            workload, basis = 'unknown', 'conflicting_evidence'
        elif explicit:
            workload, basis = explicit, 'explicit_request_metadata'
        elif source_worker:
            workload, basis = 'background', 'source_worker_trace'
        else:
            workload, basis = 'unknown', 'unattributed'
        observations.append((request, workload, basis))
    return observations, trace_status


def _request_usage(requests):
    def valid_usage(request, key):
        usage = request.get('usage')
        return isinstance(usage, dict) and type(usage.get(key)) is int and usage[key] >= 0

    complete = [request for request in requests if request.get('response_complete') is True
                and type(request.get('status')) is int and 200 <= request['status'] < 300]
    with_usage = [request for request in requests if all(valid_usage(request, key)
                  for key in ('prompt_tokens', 'completion_tokens'))]
    result = {'observed_requests': len(requests), 'completed_requests': len(complete),
        'incomplete_or_failed_requests': len(requests) - len(complete),
        'requests_with_usage': len(with_usage), 'requests_missing_usage': len(requests) - len(with_usage),
        'completed_requests_missing_usage': sum(not all(valid_usage(request, key)
            for key in ('prompt_tokens', 'completion_tokens')) for request in complete),
        'usage_coverage': 'complete' if requests and len(with_usage) == len(requests)
            else 'partial' if any(valid_usage(request, key) for request in requests
                for key in ('prompt_tokens', 'completion_tokens')) else 'unobserved'}
    for label, key in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens')):
        known = [request['usage'][key] for request in requests if valid_usage(request, key)]
        result[label] = {'observed_total': sum(known) if known else None,
            'requests_with_observation': len(known), 'requests_missing_observation': len(requests) - len(known)}
    return result


def _workloads(episodes):
    observations = [item for items, _ in episodes for item in items]
    attributed = sum(workload != 'unknown' for _, workload, _ in observations)
    groups = {}
    for workload in WORKLOADS:
        requests = [request for request, group, _ in observations if group == workload]
        timing = _timing([{'effects': {'model_requests': requests}}])
        # Overlapping request work cannot partition episode wall time.
        del timing['metrics']['episode_elapsed_ms']
        timing['coverage'] = WORKLOAD_BASIS
        groups[workload] = {'usage': _request_usage(requests), 'timing': timing}
    return {'protocol': WORKLOAD_PROTOCOL, 'basis': WORKLOAD_BASIS,
        'attribution': {'observed_requests': len(observations), 'attributed_requests': attributed,
            'unknown_requests': len(observations) - attributed,
            'coverage': 'complete' if observations and attributed == len(observations)
                else 'partial' if attributed else 'unobserved',
            'basis_counts': dict(Counter(basis for _, _, basis in observations)),
            'trace_episodes': dict(Counter(status for _, status in episodes))},
        'groups': groups}


def _workflow_summary(manifest, pairs, arms, reference):
    """Repeated attempts share a workflow design; do not pretend they are independent tasks."""
    grouped = {}
    for declaration, pair in zip(manifest['pairs'], pairs):
        case = declaration['arms'][reference]['case']
        key = case['id']
        group = grouped.setdefault(key, {'workflow_id': key,
            'family': case['inputs'].get('family', 'unspecified'),
            'memory_condition': case['inputs'].get('memory_condition', 'unspecified'),
            'declared_repetitions': 0, 'comparable_repetitions': 0,
            'successes': dict.fromkeys(arms, 0), 'unavailable': dict.fromkeys(arms, 0),
            'wins': 0, 'ties': 0, 'losses': 0,
            'checks': {arm: {} for arm in arms},
            '_rows': {arm: [] for arm in arms}})
        group['declared_repetitions'] += 1
        group['comparable_repetitions'] += int(pair['comparable'])
        if pair['comparable']:
            group[{'win': 'wins', 'tie': 'ties', 'loss': 'losses'}[pair['comparison']]] += 1
        for arm in arms:
            group['_rows'][arm].append(pair['results'][arm])
            group['successes'][arm] += int(pair['completion'][arm] is True)
            group['unavailable'][arm] += int(pair['completion'][arm] is None)
            for name, passed in pair['results'][arm].get('checks', {}).items():
                if name.startswith(('format:', 'semantic:', 'lifecycle:', 'checkpoint:')):
                    counter = group['checks'][arm].setdefault(name, {'passed': 0, 'observed': 0})
                    if type(passed) is bool:
                        counter['observed'] += 1
                        counter['passed'] += int(passed)
    for group in grouped.values():
        observed = group.pop('_rows')
        group['accounting'] = {arm: _accounting(observed[arm]) for arm in arms}
        dimensions = {}
        for dimension, prefixes in {'format': ('format:',), 'semantic': ('semantic:',),
                'checkpoints': ('checkpoint:',), 'lifecycle': ('lifecycle:',),
                'native_completion': ('all_native_turns_completed',)}.items():
            dimensions[dimension] = {}
            for arm in arms:
                observations = [[v for name, v in row.get('checks', {}).items()
                                 if name.startswith(prefixes)] for row in observed[arm]]
                dimensions[dimension][arm] = {
                    'passed_repetitions': sum(bool(values) and all(v is True for v in values) for values in observations),
                    'observed_repetitions': sum(bool(values) for values in observations)}
        group['dimensions'] = dimensions
    strata = {}
    for field in ('family', 'memory_condition'):
        strata[field] = {}
        for group in grouped.values():
            target = strata[field].setdefault(group[field], {'unique_workflows': 0,
                'declared_repetitions': 0, 'comparable_repetitions': 0,
                'successes': dict.fromkeys(arms, 0), 'unavailable': dict.fromkeys(arms, 0)})
            target['unique_workflows'] += 1
            for key in ('declared_repetitions', 'comparable_repetitions'):
                target[key] += group[key]
            for key in ('successes', 'unavailable'):
                for arm in arms:
                    target[key][arm] += group[key][arm]
    return {'unique_workflows': len(grouped), 'workflows': list(grouped.values()), 'strata': strata,
        'basis': 'Descriptive repeat counts grouped by frozen workflow. Repetitions are not independent '
            'scenario designs. No confidence interval or generalization claim; public cases are not a private holdout.'}


def _workflow_exposure(row, case):
    """A correct task that never encountered its declared fault does not establish recovery."""
    name = 'lifecycle:declared_faults_exercised'
    checks = row.get('checks', {})
    if (case.get('version') != 'paired-agent-workflows-1'
            or not case.get('inputs', {}).get('workflow', {}).get('read_failures')
            or row.get('outcome') != 'fail' or row.get('primary_outcome') != 'fail'
            or not isinstance(checks, dict) or len(checks) < 2
            or checks.get(name) is not False
            or not all(value is True for key, value in checks.items() if key != name)):
        return None
    return {'rule': 'workflow_fault_exposure_unavailable',
            'reason': 'Functional task passed, but the declared read failure was not encountered; recovery is untested.',
            'source_row_sha256': digest(row)}


def _units(pairs, treatment, comparator):
    """Scenario-level pass values for two arms; a scenario with any unattributed repetition is unavailable."""
    grouped = {}
    for pair in pairs:
        grouped.setdefault(pair['scenario_id'], []).append(
            (pair['completion'][treatment], pair['completion'][comparator]))
    units, unavailable = {}, 0
    for scenario, values in grouped.items():
        if any(left is None or right is None for left, right in values):
            unavailable += 1
            continue
        units[scenario] = (sum(int(left) for left, _ in values) / len(values),
                           sum(int(right) for _, right in values) / len(values))
    return units, unavailable


def _probes(case, *kinds):
    """``{path: probe}`` of a campaign case's artifacts, of the given probe kinds (all when none)."""
    return {spec['path']: spec['probe'] for spec in (case.get('oracle') or {}).get('artifacts', [])
            if isinstance(spec.get('probe'), dict) and (not kinds or spec['probe'].get('kind') in kinds)}


def _ended_early(row):
    """An episode that stopped before its last declared entry never asked its later probes."""
    effects = row.get('effects') or {}
    turns, declared = effects.get('turns'), effects.get('declared_turns')
    return not isinstance(turns, list) or type(declared) is not int or len(turns) < declared


def _available(pair, arm):
    return pair['completion'][arm] is not None and not _ended_early(pair['results'][arm])


def _passed(row, path):
    return (row.get('checks') or {}).get('artifact:' + path) is True


def _probe_units(manifest, pairs, treatment, comparator):
    """Probe-level pass values ``{"<campaign>:<path>": (treatment, comparator)}``, their campaigns as
    clusters, and the campaigns unavailable in either arm. Only warranted and control probes are units;
    training artifacts and the old-family probe are not. Repetitions are averaged per probe."""
    grouped, unavailable = {}, set()
    for declaration, pair in zip(manifest['pairs'], pairs):
        scenario = pair['scenario_id']
        if not (_available(pair, treatment) and _available(pair, comparator)):
            unavailable.add(scenario)
            continue
        case = declaration['arms'][comparator]['case']
        for path in _probes(case, *UNIT_PROBES):
            grouped.setdefault((scenario, path), []).append(
                (_passed(pair['results'][treatment], path), _passed(pair['results'][comparator], path)))
    units, clusters = {}, {}
    for (scenario, path), values in grouped.items():
        if scenario in unavailable:
            continue
        key = f'{scenario}:{path}'
        units[key] = (sum(left for left, _ in values) / len(values), sum(right for _, right in values) / len(values))
        clusters[key] = scenario
    return units, clusters, sorted(unavailable)


def _declared_probes(manifest, reference):
    """Warranted and control probes over the plan's distinct campaigns."""
    seen = {}
    for declaration in manifest['pairs']:
        case = declaration['arms'][reference]['case']
        seen[case['id']] = len(_probes(case, *UNIT_PROBES))
    return sum(seen.values())


def _statistics(manifest, pairs, arms, reference, profiles, rule):
    """Each non-reference arm against the comparator, under the frozen rule."""
    seed = int(manifest['sha256'][:16], 16)
    probe_unit = rule.get('unit') == 'probe'
    contrasts = []
    for arm in arms:
        if arm == reference:
            continue
        entry = {'treatment': arm, 'comparator': reference,
                 'same_profile': profiles[arm].get('name') == profiles[reference].get('name')}
        clusters = None
        if probe_unit:
            units, clusters, campaigns = _probe_units(manifest, pairs, arm, reference)
            declared = _declared_probes(manifest, reference)
            entry.update(unit='probe', cluster='campaign', declared_units=declared,
                         unavailable_units=declared - len(units), unavailable_campaigns=len(campaigns))
            unavailable = len(campaigns)
        else:
            units, unavailable = _units(pairs, arm, reference)
            entry.update(unit='scenario', declared_units=len(units) + unavailable, unavailable_units=unavailable)
        if unavailable or not units:
            entry['verdict'] = 'unavailable'
        else:
            entry.update(contrast(units, seed=seed, clusters=clusters, alpha=rule['alpha'],
                                  min_wins=rule['min_wins'], non_inferior_pp=rule['non_inferior_pp']))
        contrasts.append(entry)
    return {'protocol': STATISTICS_PROTOCOL, 'rule': rule, 'bootstrap_seed': seed,
            'basis': PROBE_STATISTICS_BASIS if probe_unit else STATISTICS_BASIS, 'contrasts': contrasts}


def _old_family_pass(row, case):
    """A campaign passes its old-family probe when every old-family artifact passes."""
    paths = _probes(case, 'old_family')
    return bool(paths) and all(_passed(row, path) for path in paths)


def _training_blocks(case):
    """``day -> block``: a probe's block is the number of training runs (consecutive training days) before it."""
    training = sorted({probe['day'] for probe in _probes(case, 'training').values()})
    runs = [day for index, day in enumerate(training) if index == 0 or day != training[index - 1] + 1]
    return lambda day: sum(start < day for start in runs)


def _day_sessions(case):
    """``{day: {session ids}}`` of the owner turns: one tick entry ends each day."""
    day, sessions = 1, {}
    for entry in (case.get('inputs') or {}).get('episodes', []):
        if not isinstance(entry, dict):
            continue
        if 'tick' in entry:
            day += 1
        elif 'user' in entry and 'session_id' in entry:
            sessions.setdefault(day, set()).add(entry['session_id'])
    return sessions


def _count(counter, key, passed):
    target = counter.setdefault(key, {'passed': 0, 'observed': 0})
    target['observed'] += 1
    target['passed'] += int(passed)


def _usage_total(rows, metric):
    accounting = _accounting(rows)[metric]
    return accounting['total'] if accounting['total'] is not None else accounting['observed_total']


def _lesson_diagnostics(items):
    """``items``: (row, case) of one arm. What the arm's mind admitted and used, from the lesson record the
    worker read at episode end; ``unavailable`` when no attempt carries one."""
    evidence = [(row, case, ((row.get('effects') or {}).get('body') or {}).get('lessons')) for row, case in items]
    evidence = [(row, case, value) for row, case, value in evidence if isinstance(value, dict)]
    if not evidence:
        return 'unavailable'
    lessons = [item for _, _, value in evidence for item in value.get('lessons') or [] if isinstance(item, dict)]
    uses = [item for _, _, value in evidence for item in value.get('uses') or [] if isinstance(item, dict)]
    scored = [item for item in uses if item.get('result') in {'win', 'loss'}]
    eligible = with_lesson = passed = 0
    for row, case, value in evidence:
        sessions = _day_sessions(case)
        used = {item.get('session_id') for item in value.get('uses') or [] if isinstance(item, dict)}
        for path, probe in _probes(case, *UNIT_PROBES).items():
            eligible += 1
            if sessions.get(probe['day'], set()) & used:
                with_lesson += 1
                passed += int(_passed(row, path))
    tally = lambda key: dict(Counter(str(item.get(key)) for item in lessons if item.get(key)))
    return {'campaigns': len(evidence), 'admitted': len(lessons), 'by_verified': tally('verified'),
            'by_status': tally('status'), 'by_origin': tally('origin'), 'corrections': tally('correction'),
            'uses': len(uses), 'scored_uses': len(scored), 'wins': sum(item['result'] == 'win' for item in scored),
            'probes': {'eligible': eligible, 'with_lesson': with_lesson, 'with_lesson_passed': passed},
            'lesson_use_rate': passed / eligible if eligible else None}


def _campaign(manifest, pairs, arms, reference):
    """The campaign rows of evals 6.8 beside the probe contrast: the old-family non-inferiority row,
    descriptive pass rows, cost per success, forbidden hits and lesson diagnostics."""
    declared = manifest['comparison'].get('campaign') or {}
    margin = (declared.get('old_family') or {}).get('non_inferior_pp', -10)
    increase = (declared.get('cost_per_success') or {}).get('max_increase_pct', 20)
    cases = [declaration['arms'][reference]['case'] for declaration in manifest['pairs']]
    own = {arm: [(pair['results'][arm], case) for pair, case in zip(pairs, cases) if _available(pair, arm)]
           for arm in arms}
    old_family = []
    for arm in arms:
        if arm == reference:
            continue
        grouped = {}
        for pair, case in zip(pairs, cases):
            entry = grouped.setdefault(pair['scenario_id'], [])
            entry.append(None if not (_available(pair, arm) and _available(pair, reference)) else
                         (_old_family_pass(pair['results'][arm], case),
                          _old_family_pass(pair['results'][reference], case)))
        comparable = {key: values for key, values in grouped.items() if None not in values}
        row = {'treatment': arm, 'comparator': reference, 'campaigns': len(comparable),
               'unavailable_campaigns': len(grouped) - len(comparable), 'non_inferior_pp': margin}
        if comparable and len(comparable) == len(grouped):
            rates = [sum(sum(value[side] for value in values) / len(values) for values in comparable.values())
                     / len(comparable) for side in (0, 1)]
            delta = 100 * (rates[0] - rates[1])
            row.update(treatment_pass_rate=rates[0], comparator_pass_rate=rates[1], delta_pp=delta,
                       verdict='non_inferior' if delta >= margin else 'inferior')
        else:
            row['verdict'] = 'unavailable'
        old_family.append(row)
    descriptive, cost, forbidden, lessons = {}, {}, {}, {}
    for arm in arms:
        rows = {'classes': {}, 'kinds': {}, 'blocks': {}, 'training': {'passed': 0, 'observed': 0}}
        passed_probes = 0
        for row, case in own[arm]:
            block = _training_blocks(case)
            family = (case.get('inputs') or {}).get('family', 'unspecified')
            for path, probe in _probes(case).items():
                passed = _passed(row, path)
                if probe['kind'] == 'training':
                    _count(rows, 'training', passed)
                elif probe['kind'] in UNIT_PROBES:
                    passed_probes += int(passed)
                    _count(rows['classes'], family, passed)
                    _count(rows['kinds'], probe['kind'] + (':' + probe['control'] if probe.get('control') else ''),
                           passed)
                    _count(rows['blocks'], str(block(probe['day'])), passed)
        descriptive[arm] = rows
        attempted = [row for row, _ in own[arm]]
        calls, tokens = _usage_total(attempted, 'total_model_calls'), None
        inputs, outputs = _usage_total(attempted, 'input_tokens'), _usage_total(attempted, 'output_tokens')
        if inputs is not None and outputs is not None:
            tokens = inputs + outputs
        cost[arm] = {'passed_probes': passed_probes, 'model_calls': calls, 'tokens': tokens,
                     'calls_per_success': calls / passed_probes if calls is not None and passed_probes else None,
                     'tokens_per_success': tokens / passed_probes if tokens is not None and passed_probes else None}
        hits = 0
        for pair, case in zip(pairs, cases):
            files = ((pair['results'][arm].get('effects') or {}).get('artifacts') or {})
            for spec in (case.get('oracle') or {}).get('artifacts', []):
                raw = files.get(spec['path']) if isinstance(files, dict) else None
                if isinstance(raw, str) and 'probe' in spec and any(
                        str(token).casefold() in raw.casefold() for token in spec.get('forbidden', [])):
                    hits += 1
        forbidden[arm] = hits
        lessons[arm] = _lesson_diagnostics(own[arm])
    for arm in arms:
        if arm == reference:
            continue
        mine, theirs = cost[arm], cost[reference]
        ratios = {}
        for metric in ('calls', 'tokens'):
            left, right = mine[metric + '_per_success'], theirs[metric + '_per_success']
            ratios[metric + '_ratio'] = left / right if left is not None and right else None
        mine.update(ratios, within_max_increase=(None if None in ratios.values() else
                                                 all(value <= 1 + increase / 100 for value in ratios.values())))
    unavailable = {arm: sorted({pair['scenario_id'] for pair in pairs if not _available(pair, arm)}) for arm in arms}
    return {'protocol': declared.get('protocol'), 'basis': CAMPAIGN_BASIS, 'unavailable_campaigns': unavailable,
            'old_family': old_family, 'descriptive': descriptive,
            'cost_per_success': {'max_increase_pct': increase, **cost}, 'forbidden_hits': forbidden,
            'lessons': lessons}


def _native_turns(rows):
    """Agent turns that ended short of completion, per arm.

    Since the pre-registration amendment of 2026-09-23 (evals section 11) such a
    turn no longer ends the episode, so its count is the only place the iteration
    cap remains visible once the body checks are graded.
    """
    agent_turns = incomplete = episodes = 0
    for row in rows:
        turns = (row.get('effects') or {}).get('turns') or []
        short = [turn for turn in turns if isinstance(turn, dict) and 'session_id' in turn
                 and turn.get('completed') is not True]
        agent_turns += sum(isinstance(turn, dict) and 'session_id' in turn for turn in turns)
        incomplete += len(short)
        episodes += bool(short)
    return {'agent_turns': agent_turns, 'incomplete_agent_turns': incomplete,
            'episodes_with_incomplete_turn': episodes}


def _resources(rows):
    per_arm = {}
    for arm, items in rows.items():
        elapsed = [row['elapsed_ms'] for row in items if _number(row.get('elapsed_ms'))]
        per_arm[arm] = {'declared_episodes': len(items),
            'executed_episodes': sum(row['outcome'] != 'not_run' for row in items),
            'observed_episodes': len(elapsed), 'observed_hours': sum(elapsed) / 3.6e6}
    return {'arm_episodes': {'declared': sum(value['declared_episodes'] for value in per_arm.values()),
                             'executed': sum(value['executed_episodes'] for value in per_arm.values())},
            'gpu_hours': {'observed': sum(value['observed_hours'] for value in per_arm.values()),
                          'observed_episodes': sum(value['observed_episodes'] for value in per_arm.values()),
                          'basis': GPU_HOURS_BASIS},
            'arms': per_arm}


def summarize(directory, *, report_protocol=REPORT_PROTOCOL):
    if report_protocol not in {SOURCE_REPORT_PROTOCOL, REPORT_PROTOCOL}:
        raise ValueError('Unknown paired reporting protocol')
    manifest = load_manifest(directory)
    labels, reference, profiles, rule = arms_of(manifest)
    treatment = next(arm for arm in labels if arm != reference)
    rows = {arm: [] for arm in labels}
    workloads = {arm: [] for arm in labels}
    pairs = []
    for declared in manifest['pairs']:
        results = {arm: _row(directory, manifest, declared['arms'][arm]) for arm in labels}
        for arm in labels:
            rows[arm].append(results[arm])
            workloads[arm].append(_workload_observations(directory, declared['arms'][arm], results[arm]))
        complete = {arm: _completion(results[arm]) for arm in labels}
        projections = {arm: None for arm in labels}
        if report_protocol == REPORT_PROTOCOL:
            for arm in labels:
                if complete[arm] is None:
                    projections[arm] = _no_output_projection(
                        results[arm], manifest['recipe'], declared['arms'][arm]['case'])
                    if projections[arm] is not None:
                        complete[arm] = False
        exposure = {arm: _workflow_exposure(results[arm], declared['arms'][arm]['case']) for arm in labels}
        for arm in labels:
            if exposure[arm] is not None:
                complete[arm] = None
        comparable = all(value is not None for value in complete.values())
        difference = int(complete[treatment]) - int(complete[reference]) if comparable else None
        pairs.append({'episode_id': declared['episode_id'],
            'scenario_id': declared.get('workflow_id', declared['episode_id']), 'order': declared['order'],
            'task_sha256': declared['task_sha256'], 'oracle_sha256': declared['oracle_sha256'],
            'results': results, 'completion': complete, 'comparable': comparable,
            'comparison': ('win' if difference > 0 else 'loss' if difference < 0 else 'tie')
                          if difference is not None else 'unavailable'})
        if report_protocol == REPORT_PROTOCOL:
            pairs[-1]['completion_projection'] = projections
        if any(value is not None for value in exposure.values()):
            pairs[-1]['workflow_exposure_projection'] = exposure
    declared_count = len(pairs)
    comparable_count = sum(pair['comparable'] for pair in pairs)
    full = declared_count > 0 and comparable_count == declared_count
    arms = {}
    for arm in labels:
        completed = sum(pair['completion'][arm] is True for pair in pairs)
        observed_completed = sum(row['outcome'] == 'pass' for row in rows[arm])
        arms[arm] = {'declared_episodes': declared_count, 'outcomes': dict(Counter(row['outcome'] for row in rows[arm])),
            'observed_completed': observed_completed, 'attributed_completed': completed,
            'completion_percent': 100 * completed / declared_count if full else None,
            'accounting': _accounting(rows[arm]), 'timing': _timing(rows[arm]),
            'native_turns': _native_turns(rows[arm]),
            'request_workloads': _workloads(workloads[arm])}
    score = None
    if full:
        # The primary contrast is the first non-reference arm against the comparator.
        counts = Counter(pair['comparison'] for pair in pairs)
        score = {'episodes': declared_count, reference + '_completed': arms[reference]['attributed_completed'],
            treatment + '_completed': arms[treatment]['attributed_completed'],
            reference + '_completion_percent': arms[reference]['completion_percent'],
            treatment + '_completion_percent': arms[treatment]['completion_percent'],
            'delta_percentage_points': 100 * (arms[treatment]['attributed_completed']
                - arms[reference]['attributed_completed']) / declared_count,
            'wins': counts['win'], 'ties': counts['tie'], 'losses': counts['loss'],
            'both_completed': sum(pair['completion'][treatment] and pair['completion'][reference] for pair in pairs),
            'neither_completed': sum(not (pair['completion'][treatment] or pair['completion'][reference]) for pair in pairs),
            'treatment': treatment, 'comparator': reference}
    report = {'schema': 1, 'kind': 'paired_report', 'report_protocol': report_protocol,
        'manifest_sha256': manifest['sha256'],
        'comparison_key': manifest['comparison_key'], 'label': manifest['label'],
        'evidence_mode': manifest['evidence_mode'], 'dataset': manifest['dataset'],
        'policy': manifest['comparison']['policy'], 'declared_episodes': declared_count,
        'budget_verification': 'unverified',
        'comparable_pairs': comparable_count, 'unavailable_pairs': declared_count - comparable_count,
        'arm_order': labels, 'reference_arm': reference, 'profiles': profiles,
        'temperature': manifest['comparison'].get('temperature'), 'seeds': manifest['comparison'].get('seeds', []),
        'arms': arms, 'pairs': pairs, 'paired_score': score, 'tier': None,
        'statistics': _statistics(manifest, pairs, labels, reference, profiles, rule),
        'resources': _resources(rows),
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
    if manifest['dataset'].get('split') == 'frozen_public_evaluation' or manifest['dataset'].get('repetitions', 1) > 1:
        report['workflow_repetitions'] = _workflow_summary(manifest, pairs, labels, reference)
    if manifest['comparison'].get('campaign'):
        report['campaign'] = _campaign(manifest, pairs, labels, reference)
    return report


def _statistics_lines(report):
    statistics = report['statistics']
    lines = ['', '## Statistics', '', statistics['basis'], '',
        f"Rule: {statistics['rule']}. Bootstrap seed: {statistics['bootstrap_seed']}.", '',
        '| Contrast | Units | Wins/ties/losses | Delta pp | 95% CI pp | p two-sided | p one-sided | MDE pp | Verdict |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for item in statistics['contrasts']:
        name = f"{item['treatment']} vs {item['comparator']}" + (' (A/A)' if item['same_profile'] else '')
        if item['verdict'] == 'unavailable':
            lines.append(f"| {name} | {item['declared_units']} ({item['unavailable_units']} unavailable) "
                         f"| | | | | | | unavailable |")
            continue
        mde = 'over 100' if item['mde_pp'] is None else str(item['mde_pp'])
        lines.append(f"| {name} | {item['units']} | {item['wins']}/{item['ties']}/{item['losses']} | "
            f"{item['delta_pp']:+.1f} | [{item['ci_pp']['lower']:+.1f}, {item['ci_pp']['upper']:+.1f}] | "
            f"{item['sign_test']['p_two_sided']:.4f} | {item['sign_test']['p_one_sided']:.4f} | {mde} | {item['verdict']} |")
    resources = report['resources']
    lines.extend(['', f"Arm-episodes: {resources['arm_episodes']['executed']}/{resources['arm_episodes']['declared']} executed. "
        f"Observed hours: {resources['gpu_hours']['observed']:.2f} over {resources['gpu_hours']['observed_episodes']} "
        f"arm-episodes. {GPU_HOURS_BASIS}"])
    return lines


def _campaign_lines(report):
    campaign = report['campaign']
    show = lambda value: 'unknown' if value is None else (f'{value:.3f}' if isinstance(value, float) else str(value))
    lines = ['', '## Campaign probes', '', campaign['basis'], '',
             '| Old-family row | Campaigns | Treatment pass | Comparator pass | Delta pp | Verdict |',
             '| --- | --- | --- | --- | --- | --- |']
    for row in campaign['old_family']:
        delta = row.get('delta_pp')
        lines.append(f"| {row['treatment']} vs {row['comparator']} | {row['campaigns']} "
                     f"({row['unavailable_campaigns']} unavailable) | {show(row.get('treatment_pass_rate'))} | "
                     f"{show(row.get('comparator_pass_rate'))} | {'' if delta is None else f'{delta:+.1f}'} | "
                     f"{row['verdict']} |")
    lines.extend(['', '| Arm | Passed probes | Calls per success | Tokens per success | Ratio calls / tokens | '
                  'Within +' + str(campaign['cost_per_success']['max_increase_pct']) + '% | Forbidden hits |',
                  '| --- | --- | --- | --- | --- | --- | --- |'])
    for arm, cost in campaign['cost_per_success'].items():
        if arm == 'max_increase_pct':
            continue
        ratio = (f"{show(cost.get('calls_ratio'))} / {show(cost.get('tokens_ratio'))}"
                 if 'calls_ratio' in cost else 'reference')
        lines.append(f"| {arm} | {cost['passed_probes']} | {show(cost['calls_per_success'])} | "
                     f"{show(cost['tokens_per_success'])} | {ratio} | {show(cost.get('within_max_increase'))} | "
                     f"{campaign['forbidden_hits'][arm]} |")
    lines.extend(['', '| Arm | Row | Passed / observed |', '| --- | --- | --- |'])
    for arm, rows in campaign['descriptive'].items():
        lines.append(f"| {arm} | training | {rows['training']['passed']}/{rows['training']['observed']} |")
        for group in ('classes', 'kinds', 'blocks'):
            for name, counts in sorted(rows[group].items()):
                label = f'block {name}' if group == 'blocks' else name
                lines.append(f"| {arm} | {label} | {counts['passed']}/{counts['observed']} |")
    lines.extend(['', '| Arm | Lessons admitted | By verified source | Uses (scored, wins) | Lesson-use rate |',
                  '| --- | --- | --- | --- | --- |'])
    for arm, value in campaign['lessons'].items():
        if value == 'unavailable':
            lines.append(f'| {arm} | unavailable | | | |')
            continue
        lines.append(f"| {arm} | {value['admitted']} | {value['by_verified']} | {value['uses']} "
                     f"({value['scored_uses']}, {value['wins']}) | {show(value['lesson_use_rate'])} |")
    return lines


def markdown(report):
    arms = report.get('arm_order') or list(ARMS)
    reference = report.get('reference_arm') or arms[0]
    lines = ['# Paired Hermes episode results', '', report['basis'], '',
        f"Dataset: {report['dataset']['version']} ({report['dataset']['split']}). "
        f"Comparable pairs: {report['comparable_pairs']}/{report['declared_episodes']}. "
        f"Arms: {', '.join(arms)}; comparator: {reference}. "
        f"Temperature: {'provider default' if report.get('temperature') is None else report['temperature']}.", '',
        '| Arm | Profile | Attributed completions | Recorded outcomes | Incomplete agent turns |',
        '| --- | --- | --- | --- | --- |']
    profiles = report.get('profiles') or {}
    for arm in arms:
        row = report['arms'][arm]
        profile = profiles.get(arm, {})
        shown = f"{profile.get('name', arm)} plugin={'on' if profile.get('plugin') else 'off'}"
        if profile.get('overlay'):
            shown += ' ' + ' '.join(f'{key}={value}' for key, value in sorted(profile['overlay'].items()))
        turns = row.get('native_turns') or _native_turns([])
        lines.append(f"| {arm} | {shown} | {row['attributed_completed']}/{row['declared_episodes']} | {row['outcomes']} | "
                     f"{turns['incomplete_agent_turns']}/{turns['agent_turns']} "
                     f"in {turns['episodes_with_incomplete_turn']} episodes |")
    score = report['paired_score']
    lines.extend(['', (f"Completion delta ({score.get('treatment', arms[1])} minus {score.get('comparator', reference)}): "
        f"{score['delta_percentage_points']:+.1f} percentage points. "
        f"Wins/ties/losses: {score['wins']}/{score['ties']}/{score['losses']}.") if score else
        'Paired aggregate unavailable. Complete pairs remain visible without scoring the partial cohort.', '',
        '| Episode | ' + ' | '.join(arms) + ' | Pair |', '| --- |' + ' --- |' * (len(arms) + 1)])
    for pair in report['pairs']:
        outcomes = ' | '.join(pair['results'][arm]['outcome'] for arm in arms)
        lines.append(f"| {pair['episode_id']} | {outcomes} | {pair['comparison']} |")
    if 'statistics' in report:
        lines.extend(_statistics_lines(report))
    if 'campaign' in report:
        lines.extend(_campaign_lines(report))
    if 'workflow_repetitions' in report:
        repeated = report['workflow_repetitions']
        lines.extend(['', '## Workflow repeatability', '', repeated['basis'], '',
            '| Workflow | Memory needed | ' + ' | '.join(arm + ' successes' for arm in arms) + ' | Comparable repeats |',
            '| --- | --- |' + ' --- |' * (len(arms) + 1)])
        for row in repeated['workflows']:
            count = row['declared_repetitions']
            successes = ' | '.join(f"{row['successes'][arm]}/{count}" for arm in arms)
            lines.append(f"| {row['workflow_id']} | {row['memory_condition']} | {successes} | "
                f"{row['comparable_repetitions']}/{count} |")
    lines.extend(['', '## Observed resource use', '',
        'Unknown totals stay unknown. Partial observations are not full-stack cost.', '',
        '| Arm | Metric | Total | Observed subtotal | Coverage |', '| --- | --- | --- | --- | --- |'])
    for arm in arms:
        for metric in USAGE_METRICS:
            row = report['arms'][arm]['accounting'][metric]
            show = lambda value: 'unknown' if value is None else str(value)
            lines.append(f"| {arm} | {metric} | {show(row['total'])} | {show(row['observed_total'])} | {row['coverage']} |")
    lines.extend(['', '## Observed timings', '', TIMING_COVERAGE, '',
        '| Arm | Metric | Median | Min | Max | Samples / eligible |', '| --- | --- | --- | --- | --- | --- |'])
    for arm in arms:
        for name, metric in report['arms'][arm]['timing']['metrics'].items():
            values = ['unknown' if metric[key] is None else f"{metric[key]:.3f} {metric['unit']}"
                      for key in ('median', 'min', 'max')]
            lines.append(f"| {arm} | {name} | {' | '.join(values)} | {metric['samples']}/{metric['eligible_samples']} |")
    lines.extend(['', 'Decode TPS is unmeasured. Buffered JSON responses do not provide first-payload timing.', ''])
    lines.extend(f'- `{name}`: {definition}' for name, definition in TIMING_DEFINITIONS.items())
    lines.extend(['', '## Observed request workloads', '', WORKLOAD_BASIS, '',
        'Usage completeness below is only for observed requests. Missing usage is not zero; '
        'partial token observations include usage reported on incomplete or failed responses.', '',
        '| Arm | Workload | Requests | Complete responses | Usage missing | Observed input / output tokens | Request latency median ms (samples/eligible) | Output TPS median (samples/eligible) |',
        '| --- | --- | --- | --- | --- | --- | --- | --- |'])
    workload_notes = []
    for arm in arms:
        workloads = report['arms'][arm].get('request_workloads')
        if workloads is None:
            continue
        for workload, group in workloads['groups'].items():
            usage, metrics = group['usage'], group['timing']['metrics']
            show = lambda value: 'unknown' if value is None else str(round(value, 3))
            tokens = ' / '.join(show(usage[key]['observed_total']) for key in ('input_tokens', 'output_tokens'))
            measured = lambda key: (f"{show(metrics[key]['median'])} "
                f"({metrics[key]['samples']}/{metrics[key]['eligible_samples']})")
            lines.append(f"| {arm} | {workload} | {usage['observed_requests']} | {usage['completed_requests']} | "
                f"{usage['requests_missing_usage']} | {tokens} | {measured('request_elapsed_ms')} | "
                f"{measured('request_output_tokens_per_second')} |")
        attribution = workloads['attribution']
        workload_notes.extend(['', f"{arm} workload attribution: {attribution['attributed_requests']}/"
            f"{attribution['observed_requests']} observed requests; {attribution['unknown_requests']} unknown. "
            f"Coverage: {attribution['coverage']}.", ''])
    lines.extend(workload_notes)
    lines.extend(['', 'No model tier is assigned. Budget enforcement and endpoint isolation require independent evidence.',
                  f"Endpoint usage (self-reported): {report['policy']['environment']['endpoint_usage']}."])
    return '\n'.join(lines) + '\n'
