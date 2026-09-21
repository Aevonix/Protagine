"""Allowlisted public paired results, without agent output or private recipes."""
from datetime import datetime, timezone
import hashlib
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
    for arm in paired_report.ARMS:
        expected = paired_cases.cases(arm, manifest['dataset']['episode_ids'], dataset_version=version)
        actual = [pair['arms'][arm]['case'] for pair in manifest['pairs']]
        if actual != [case.record() for case in expected]:
            raise ValueError('Private or changed fixture inputs cannot be publicly exported')
    known = expected[0].inputs['dataset']
    if known['sha256'] != manifest['dataset']['source_sha256']:
        raise ValueError('Dataset source hash does not match repository fixtures')
    fixture_manifest, _, _ = paired_cases.load_dataset(
        Path(paired_cases.__file__).parent / 'fixtures' / version)
    quality = 'grading_under_review' if version == paired_cases.VERSION else 'development'
    score = report['paired_score'] if (quality == 'development'
        and report['evidence_mode'] == 'actual_inference') else None
    arms = {}
    for arm in paired_report.ARMS:
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
        for arm in paired_report.ARMS:
            row = pair['results'][arm]
            if row['outcome'] not in OUTCOMES or row['primary_outcome'] not in {'pass', 'fail', 'unverified'}:
                raise ValueError('Unknown public episode attribution')
            results[arm] = {'outcome': row['outcome'], 'primary_outcome': row['primary_outcome'],
                'elapsed_ms': number(row.get('elapsed_ms')), 'completion': pair['completion'][arm]}
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
        'dataset': {'version': identifier(version), 'split': 'development',
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
        'arms': arms, 'paired_score': {key: score[key] for key in SCORES} if score else None,
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
