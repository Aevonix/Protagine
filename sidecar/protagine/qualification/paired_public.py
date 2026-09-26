"""Allowlisted public paired results, without agent output or private recipes."""
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import re

from . import paired_cases, paired_report
from .public_export import identifier, number, public_deployment, stamp, text
from .records import write_once

USAGE = ('total_model_calls', 'input_tokens', 'output_tokens', 'background_model_calls')
OUTCOMES = {'pass', 'fail', 'unsupported', 'setup_error', 'error', 'timeout', 'interrupted', 'not_run'}
SCORES = ('episodes', 'base_hermes_completed', 'protagine_completed',
    'base_hermes_completion_percent', 'protagine_completion_percent',
    'delta_percentage_points', 'wins', 'ties', 'losses', 'both_completed', 'neither_completed')
TIMINGS = ('first_generated_ms', 'first_content_ms', 'request_elapsed_ms',
           'request_output_tokens_per_second', 'episode_elapsed_ms')
REQUEST_COUNTS = ('observed', 'instrumented', 'completed', 'streaming', 'with_usage',
                  'with_first_generated', 'with_first_content')


def _hash(value, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError('Invalid public evidence hash')
    return value


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError('Invalid public evidence count')
    return value


def _accounting(value):
    result = {}
    for key in USAGE:
        source = value[key]
        coverage = source['coverage']
        if coverage not in {'complete', 'partial', 'unobserved'}:
            raise ValueError('Unknown accounting coverage')
        result[key] = {'total': number(source['total']), 'observed_total': number(source['observed_total']),
            'observed_episodes': _count(source['observed_episodes']),
            'declared_episodes': _count(source['declared_episodes']), 'coverage': coverage}
    source = value['runner_elapsed_ms']
    result['runner_elapsed_ms'] = {'total': number(source['total']),
        'observed_total': number(source['observed_total']),
        'observed_episodes': _count(source['observed_episodes']),
        'basis': 'Arm wall time includes tools, settling and container cleanup; not model-only latency.'}
    return result


def score_names(reference, treatment):
    """Public score keys are named by the two arms of the primary contrast."""
    return ('episodes', reference + '_completed', treatment + '_completed',
            reference + '_completion_percent', treatment + '_completion_percent',
            'delta_percentage_points', 'wins', 'ties', 'losses', 'both_completed', 'neither_completed')


def _signed(value):
    """Deltas and interval bounds may be negative; anything else is unknown."""
    return value if type(value) in {int, float} and math.isfinite(value) else None


def _statistics(value):
    """Scalars of each contrast only; the basis text is a fixed repository string."""
    contrasts = []
    for item in value['contrasts']:
        entry = {'treatment': identifier(item['treatment']), 'comparator': identifier(item['comparator']),
                 'same_profile': bool(item['same_profile']), 'unit': item.get('unit', 'scenario'),
                 'declared_units': _count(item['declared_units']),
                 'unavailable_units': _count(item['unavailable_units']),
                 'verdict': item['verdict']}
        if entry['unit'] not in {'scenario', 'probe'}:
            raise ValueError('Unknown public unit')
        if entry['unit'] == 'probe':
            # Campaign probes are clustered by campaign (paired_report.PROBE_STATISTICS_BASIS).
            entry.update(cluster='campaign', unavailable_campaigns=_count(item.get('unavailable_campaigns')))
        if entry['verdict'] not in {'demonstrated', 'not_demonstrated', 'unavailable'}:
            raise ValueError('Unknown public verdict')
        if entry['verdict'] != 'unavailable':
            entry.update({key: _count(item.get(key)) for key in ('units', 'clusters', 'wins', 'losses', 'ties')},
                delta_pp=_signed(item['delta_pp']), mde_pp=number(item['mde_pp']),
                p_two_sided=number(item['sign_test']['p_two_sided']),
                p_one_sided=number(item['sign_test']['p_one_sided']),
                ci_pp={'lower': _signed(item['ci_pp']['lower']), 'upper': _signed(item['ci_pp']['upper']),
                       'level': number(item['ci_pp']['level']), 'samples': _count(item['ci_pp']['samples'])},
                non_inferior_point_estimate=bool(item['non_inferior_point_estimate']))
        contrasts.append(entry)
    probes = value['rule'].get('unit') == 'probe'
    return {'protocol': paired_report.STATISTICS_PROTOCOL, 'rule': dict(value['rule']),
            'bootstrap_seed': _count(value['bootstrap_seed']),
            'basis': paired_report.PROBE_STATISTICS_BASIS if probes else paired_report.STATISTICS_BASIS,
            'contrasts': contrasts}


def _timing(value):
    value = value or {}
    requests = value.get('requests', {})
    definitions = paired_report.TIMING_DEFINITIONS
    metrics = {}
    for key in TIMINGS:
        source = value.get('metrics', {}).get(key, {})
        metrics[key] = {name: number(source.get(name)) for name in ('median', 'min', 'max')}
        metrics[key].update(samples=_count(source.get('samples', 0)),
            eligible_samples=_count(source.get('eligible_samples', 0)),
            unit='tokens/s' if key == 'request_output_tokens_per_second' else 'ms', definition=definitions[key])
    return {'protocol': 'paired-transport-2',
        'requests': {key: _count(requests.get(key, 0)) for key in REQUEST_COUNTS},
        'metrics': metrics, 'decode_tokens_per_second': None,
        'coverage': paired_report.TIMING_COVERAGE}


def export_record(directory, metadata, *, published_at=None):
    """Export only repository fixture runs with separately authored public metadata."""
    deployment = public_deployment(metadata)
    manifest = paired_report.load_manifest(directory)
    report = paired_report.summarize(directory)
    version = manifest['dataset']['version']
    if version not in paired_cases.DATASET_VERSIONS:
        raise ValueError('Only repository-owned synthetic datasets may be published')
    arm_order, reference, profiles, _ = paired_report.arms_of(manifest)
    declared_profiles = (manifest.get('comparison') or {}).get('profiles')
    for arm in arm_order:
        expected = paired_cases.cases(arm, manifest['dataset']['episode_ids'], dataset_version=version,
            profile=None if declared_profiles is None else declared_profiles[arm])
        actual = [pair['arms'][arm]['case'] for pair in manifest['pairs']]
        repetitions = manifest['dataset'].get('repetitions', 1)
        if type(repetitions) is not int or not 1 <= repetitions <= 3:
            raise ValueError('Invalid public repetition count')
        if actual != [case.record() for case in expected] * repetitions:
            raise ValueError('Private or changed fixture inputs cannot be publicly exported')
    known = expected[0].inputs['dataset']
    if known['sha256'] != manifest['dataset']['source_sha256']:
        raise ValueError('Dataset source hash does not match repository fixtures')
    fixture_manifest, _, _ = paired_cases.load_dataset(
        Path(paired_cases.__file__).parent / 'fixtures' / version)
    quality = ('grading_under_review' if version == paired_cases.VERSION else
               'frozen_public_evaluation' if version == paired_cases.WORKFLOW_VERSION else 'development')
    score = report['paired_score'] if (quality in {'development', 'frozen_public_evaluation'}
        and report['evidence_mode'] == 'actual_inference') else None
    treatment = next(arm for arm in arm_order if arm != reference)
    score_keys = score_names(reference, treatment)
    arms = {}
    for arm in arm_order:
        source = report['arms'][arm]
        if set(source['outcomes']) - OUTCOMES:
            raise ValueError('Unknown public episode outcome')
        arms[arm] = {'attributed_completed': _count(source['attributed_completed']),
            'observed_completed': _count(source['observed_completed']),
            'outcomes': {key: _count(value) for key, value in source['outcomes'].items()},
            'completion_percent': number(source['completion_percent']) if score else None,
            'accounting': _accounting(source['accounting']), 'timing': _timing(source.get('timing'))}
    episodes = []
    corrected_episodes = 0
    for pair in report['pairs']:
        results = {}
        for arm in arm_order:
            row = pair['results'][arm]
            if row['outcome'] not in OUTCOMES or row['primary_outcome'] not in {'pass', 'fail', 'unverified'}:
                raise ValueError('Unknown public episode attribution')
            results[arm] = {'outcome': row['outcome'], 'primary_outcome': row['primary_outcome'],
                'elapsed_ms': number(row.get('elapsed_ms')), 'completion': pair['completion'][arm]}
            exposure = pair.get('workflow_exposure_projection', {}).get(arm)
            if exposure is not None:
                if (version != paired_cases.WORKFLOW_VERSION or pair['completion'][arm] is not None
                        or exposure.get('rule') != 'workflow_fault_exposure_unavailable'):
                    raise ValueError('Invalid workflow exposure projection')
                results[arm]['workflow_exposure_projection'] = {
                    'rule': 'workflow_fault_exposure_unavailable',
                    'reason': 'Functional task passed; declared read failure was not encountered.',
                    'source_row_sha256': _hash(exposure['source_row_sha256'])}
            if report['report_protocol'] == paired_report.REPORT_PROTOCOL:
                projection = pair.get('completion_projection', {}).get(arm)
                results[arm]['completion_projection'] = None
                if projection is not None:
                    if (projection.get('rule') != paired_report.NO_OUTPUT_RULE
                            or row['outcome'] != 'fail' or row['primary_outcome'] != 'unverified'
                            or pair['completion'][arm] is not False):
                        raise ValueError('Invalid public completion projection')
                    results[arm]['completion_projection'] = {
                        'rule': paired_report.NO_OUTPUT_RULE,
                        'source_row_sha256': _hash(projection.get('source_row_sha256'))}
                    corrected_episodes += 1
        episodes.append({'episode_id': identifier(pair['episode_id']),
            'family': pair['episode_id'].split('.')[1], 'results': results, 'comparison': pair['comparison']})
    recipe = manifest['recipe']
    image_id = recipe['container']['image_id']
    if not isinstance(image_id, str) or not image_id.startswith('sha256:'):
        raise ValueError('Missing immutable image identity')
    _hash(image_id[7:])
    native = recipe.get('native_runtime', {})
    environment = report['policy']['environment']
    if environment['endpoint_usage'] not in {'idle_declared', 'shared', 'unknown'}:
        raise ValueError('Unknown endpoint usage declaration')
    if report['policy']['budget_mode'] not in {'matched_work', 'deployment_policy'}:
        raise ValueError('Unknown budget declaration')
    limitations = list(fixture_manifest['limitations']) + [
        'Endpoint usage is operator-declared; competing traffic may slow the live agent and distort these measurements.',
        'Public development results do not establish model tiers or a causal improvement estimate.',
        'Request throughput includes the whole HTTP request, not isolated decode; completion tokens may include reasoning.',
        'Auxiliary model work is partially observed. Missing observations are not zero.']
    if quality == 'grading_under_review':
        limitations.append('Original pilot grading is under review; aggregate quality scores are withheld.')
    record = {'schema_version': 1, 'kind': 'paired_agent_benchmark',
        'run_id': identifier('paired-' + manifest['sha256'][:24]),
        'published_at': stamp(published_at or datetime.now(timezone.utc).isoformat()),
        'deployment': deployment,
        'dataset': {'version': identifier(version), 'split': manifest['dataset']['split'],
            'sha256': _hash(report['dataset']['sha256']), 'source_sha256': _hash(known['sha256'])},
        'runtime': {'image_id': image_id,
            'hermes_version': text(native.get('distribution_version'), nullable=True),
            'hermes_payload_sha256': _hash(native.get('native_payload_sha256'), nullable=True),
            'protagine_payload_sha256': _hash(recipe.get('container_payload', {}).get('adapters', {})
                .get('packages', {}).get('protagine', {}).get('sha256'), nullable=True)},
        'evidence_mode': report['evidence_mode'], 'report_protocol': report['report_protocol'],
        'manifest_sha256': _hash(report['manifest_sha256']), 'comparison_key': _hash(report['comparison_key']),
        'endpoint_usage': environment['endpoint_usage'], 'budget_mode': report['policy']['budget_mode'],
        'budget_verification': 'unverified', 'quality_status': quality,
        'declared_episodes': _count(report['declared_episodes']),
        'comparable_pairs': _count(report['comparable_pairs']), 'unavailable_pairs': _count(report['unavailable_pairs']),
        'arm_order': [identifier(arm) for arm in arm_order], 'reference_arm': identifier(reference),
        'profiles': {arm: {'name': identifier(profiles[arm]['name']), 'plugin': bool(profiles[arm]['plugin']),
                           'overlay': {text(key): text(value) for key, value in profiles[arm]['overlay'].items()}}
                     for arm in arm_order},
        'temperature': number(report.get('temperature')),
        'arms': arms, 'paired_score': {key: score[key] for key in score_keys} if score else None,
        'statistics': _statistics(report['statistics']) if score else None,
        'episodes': episodes, 'limitations': limitations}
    if report['report_protocol'] == paired_report.REPORT_PROTOCOL:
        if report.get('completion_projection', {}).get('corrected_episodes') != corrected_episodes:
            raise ValueError('Inconsistent public completion projection count')
        record['completion_projection'] = {
            'source_protocol': paired_report.SOURCE_REPORT_PROTOCOL,
            'rule': paired_report.NO_OUTPUT_RULE, 'corrected_episodes': corrected_episodes,
            'raw_results_preserved': True, 'artifact_verifier_reexecuted': False,
            'basis': paired_report.PROJECTION_BASIS}
        limitations.append(paired_report.PROJECTION_BASIS)
    if 'workflow_repetitions' in report:
        # Dataset ownership was verified above. Only counts and repository scenario IDs leave.
        groups = report['workflow_repetitions']
        record['workflow_repetitions'] = {'unique_workflows': _count(groups['unique_workflows']),
            'basis': groups['basis'], 'workflows': [{
                'workflow_id': identifier(row['workflow_id']),
                'family': identifier(row['family']),
                'memory_condition': row['memory_condition'],
                **{key: _count(row[key]) for key in ('declared_repetitions', 'comparable_repetitions', 'wins', 'ties', 'losses')},
                'successes': {arm: _count(row['successes'][arm]) for arm in arm_order},
                'unavailable': {arm: _count(row['unavailable'][arm]) for arm in arm_order},
                'accounting': {arm: _accounting(row['accounting'][arm]) for arm in arm_order},
                'dimensions': {dimension: {arm: {
                    'passed_repetitions': _count(counts[arm]['passed_repetitions']),
                    'observed_repetitions': _count(counts[arm]['observed_repetitions'])}
                    for arm in arm_order} for dimension, counts in row['dimensions'].items()},
            } for row in groups['workflows']]}
    return record


def publish_record(directory, metadata, output):
    """Write a new static snapshot; never overwrite a prior public record."""
    record = export_record(directory, metadata)
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (output / 'runs').mkdir()
    target = output / 'runs' / (record['run_id'] + '.json')
    write_once(target, record)
    index = {'schema_version': 1, 'records': [
        {'path': 'benchmarks/paired/runs/' + target.name,
         'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}]}
    write_once(output / 'index.json', index)
    return {'run_id': record['run_id'], 'index': str(output / 'index.json')}
