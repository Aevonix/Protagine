"""Publish scalar qualification evidence without copying private result bodies.

Deployment descriptions are a separately authored public document. Endpoint
configuration, inputs, oracles, outputs, observations and exception text never
cross this boundary. Ordinary immutable qualification records remain canonical.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from urllib.parse import urlparse

from .records import digest, encode, read, write_once
from .report import OUTCOMES, summarize

ROLES = {'chat': 'conversation', 'reasoning': 'reasoning', 'planning': 'planning',
         'coding': 'coding', 'extraction': 'extraction', 'judging': 'review',
         'vision': 'vision', 'speech': 'voice', 'voice': 'voice'}
BOUNDARIES = {'role_completion', 'cognition_consumer', 'native_hermes',
              'retrieval', 'speech', 'media_consumer', 'native_protagine'}
PHASES = {'screen', 'development', 'held_out', 'performance', 'system_contribution'}
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z')
DEPLOYMENT_TEXT = ('id', 'model', 'revision', 'variant', 'quantization', 'hardware',
                   'runtime', 'runtime_version', 'profile')
DEPLOYMENT_COUNTS = ('node_count', 'context_limit', 'concurrency')


def identifier(value):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError('Invalid public identifier')
    return value


def text(value, *, nullable=False):
    if value is None and nullable:
        return None
    if (not isinstance(value, str) or not 1 <= len(value) <= 240
            or any(ord(c) < 32 for c in value)
            or re.search(r'(?:https?://|/home/|/Users/|/mnt/|/private/|Bearer\s)', value, re.I)):
        raise ValueError('Invalid public description')
    return value


def public_deployment(metadata):
    if metadata.get('publication_scope') != 'public_synthetic':
        raise ValueError('Explicit public synthetic publication scope required')
    source = metadata['deployment']
    result = {key: text(source.get(key), nullable=key not in {'id', 'model', 'profile'})
              for key in DEPLOYMENT_TEXT}
    identifier(result['id'])
    for key in DEPLOYMENT_COUNTS:
        val = source.get(key)
        if val is not None and (type(val) is not int or val < 1):
            raise ValueError('Invalid deployment count')
        result[key] = val
    verified = source.get('weights_verified', False)
    if type(verified) is not bool or (verified and not result['revision']):
        raise ValueError('Verified weights require a revision')
    result['weights_verified'] = verified
    url = source.get('source_url')
    if url is not None:
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.hostname not in {'huggingface.co', 'github.com'}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.port not in {None, 443}):
            raise ValueError('Invalid public model source URL')
    result['source_url'] = url
    return result


def stamp(value):
    if not isinstance(value, str):
        raise ValueError('Missing evidence timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Evidence timestamp needs timezone')
    return parsed.astimezone(timezone.utc).isoformat()


def number(value):
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def failure_stage(row):
    """Fixed vocabulary only, never arbitrary exception strings."""
    outcome = row['outcome']
    if outcome in {'unsupported', 'setup_error', 'timeout', 'interrupted', 'not_run'}:
        return outcome
    if row.get('failure_category') in {'no_output', 'missing_final_answer', 'incomplete_final_answer'}:
        return 'final_answer'
    if row.get('checks', {}).get('output_is_json') is False:
        return 'output_contract'
    if outcome == 'fail':
        return 'case_checks'
    if outcome == 'error':
        return 'execution'
    if row.get('primary_outcome') != 'pass':
        return 'attribution'
    return None


def output_diagnostics(row, spec):
    """Secondary field/effect checks, never changing the original strict grade.

    A single complete Markdown JSON fence can be inspected without claiming it
    satisfied a bare-JSON contract. No substring hunting, model judge or retry.
    This is inapplicable to memory consumers with different evaluator semantics.
    """
    if spec['evaluator'] != 'json_fields' or not isinstance(row.get('output'), str):
        return None
    raw = row['output']
    valid, value = True, None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        valid = False
        fence = re.fullmatch(r'\s*```(?:json)?\s*\n(.*?)\n```\s*', raw, re.S | re.I)
        if fence:
            try:
                value = json.loads(fence[1])
            except (ValueError, TypeError):
                pass
    semantic = None
    if isinstance(value, (dict, list)):
        from .cases import json_fields
        checks = json_fields({'output': value, 'effects': row.get('effects', {})}, spec['oracle'])
        semantic = all(v is True for v in checks.values()) if checks else None
    return {'format_valid': valid, 'semantic_pass': semantic,
            'version': 'field-effects-single-fence-diagnostic-v1'}


def mechanism_summary(row, spec):
    """Publish fixed labels/counts, never source IDs or arbitrary check text."""
    if spec['consumer'] != 'native_memory':
        return None
    groups = [('correct_final_answer', 'Requested answer object'),
              ('source_visible.', 'Required sources visible'),
              ('useful_claim_formed.', 'Useful claims retained'),
              ('junk_not_promoted.', 'Junk not promoted'),
              ('corrected_claim_retired.', 'Corrected claims retired'),
              ('durable_erasure.', 'Erasure retained'),
              ('erased_claims_absent.', 'Erased claims absent'),
              ('private_or_erased_absent.', 'Private or erased context absent')]
    fragments = []
    for key, label in groups:
        values = [value for name, value in row.get('checks', {}).items()
                  if name == key or key.endswith('.') and name.startswith(key)]
        if values:
            fragments.append(f'{label}: {sum(value is True for value in values)}/{len(values)}')
    return '; '.join(fragments) or None


def export_records(directory, metadata):
    """One public record per actual consumer boundary and role, no input bodies."""
    run = read(Path(directory) / 'run.json')
    report = summarize(directory)
    deployment = public_deployment(metadata)
    phase = metadata.get('phase')
    if phase not in PHASES:
        raise ValueError('Invalid benchmark phase')
    if run['evidence_mode'] not in {'actual_inference', 'controlled'}:
        raise ValueError('Invalid evidence mode')
    identifier(run['id'])
    specs = {case['id']: case for case in run['cases']}
    # A mixed private/public run must not accidentally be made public by filtering.
    if any(c.get('provenance') != 'public' for c in specs.values()):
        raise ValueError('Private cases cannot be publicly exported')
    groups = defaultdict(list)
    for row in report['cases']:
        identifier(row['case_id'])
        if row['boundary'] not in BOUNDARIES:
            raise ValueError('Unknown consumer boundary')
        boundary = ('native_protagine' if specs[row['case_id']]['consumer'] == 'native_memory'
                    else row['boundary'])
        groups[(row['role'], boundary)].append(row)
    public = []
    for (role, boundary), rows in sorted(groups.items()):
        identifier(role)
        public_id = identifier(f"{run['id']}-{role}-{boundary}")
        cases = []
        for row in rows:
            primary = row.get('primary_outcome', 'unverified')
            if primary not in {'pass', 'fail', 'unverified'}:
                raise ValueError('Invalid primary attribution')
            public_case = {'case_id': row['case_id'], 'title': row['case_id'],
                'outcome': row['outcome'], 'primary_outcome': primary,
                'failure_stage': failure_stage(row), 'duration_ms': number(row.get('elapsed_ms')),
                'summary': mechanism_summary(row, specs[row['case_id']])}
            if diagnostics := output_diagnostics(row, specs[row['case_id']]):
                public_case['diagnostics'] = diagnostics
            cases.append(public_case)
        counts = Counter(c['outcome'] for c in cases)
        outcomes = {key: counts[key] for key in sorted(OUTCOMES) if counts[key]}
        durations = [c['duration_ms'] for c in cases if c['duration_ms'] is not None]
        completed = all(row.get('ended_at') for row in rows)
        status = ('aborted' if not completed or counts['interrupted'] else
                  'failed' if all(c['outcome'] in {'setup_error', 'error'} for c in cases) else 'completed')
        invalid_reason = metadata.get('invalid_reason')
        if invalid_reason is not None:
            invalid_reason = text(invalid_reason)
            status = 'invalid'
        selected = [specs[r['case_id']] for r in rows]
        suite_hash = digest({'suite_version': run['suite_version'], 'cases': selected})
        evaluator_hash = digest({c['evaluator']: run.get('evaluator_identities', {}).get(c['evaluator'])
                                 for c in selected})
        protocol = metadata.get('comparison_protocol')
        # This separately authored protocol includes budget policy and fixed supporting
        # processors. Never derive it by publishing the private routing recipe.
        comparison = None
        if protocol and run.get('implementation_sha256') and run['evidence_mode'] == 'actual_inference':
            required = {'id', 'budget_policy', 'supporting_models', 'hardware_policy'}
            if not isinstance(protocol, dict) or set(protocol) != required:
                raise ValueError('Incomplete comparison protocol')
            if any(not isinstance(protocol[k], (str, list, dict)) for k in required):
                raise ValueError('Invalid comparison protocol')
            if any(not run.get('evaluator_identities', {}).get(c['evaluator']) for c in selected):
                raise ValueError('Missing evaluator identity')
            comparison = digest({'protocol': protocol, 'suite': suite_hash,
                'evaluator': evaluator_hash, 'implementation': run['implementation_sha256'],
                'runtime_version': run.get('recipe', {}).get('runtime_version'),
                'protagine_payload': run.get('recipe', {}).get('protagine_payload_sha256'),
                'native_payload': run.get('recipe', {}).get('native_runtime', {}).get('native_payload_sha256'),
                'native_worker': run.get('recipe', {}).get('native_worker_sha256'),
                'role': role, 'boundary': boundary, 'phase': phase})
        limits = ['Coverage is limited to the listed cases and consumer boundary.',
                  'Case duration includes consumer overhead; it is not token throughput or first-token latency.',
                  'Secondary field/effect diagnostics may inspect a single JSON fence; original strict outcomes remain unchanged.']
        if boundary == 'cognition_consumer':
            limits.append('This consumer result does not establish native Hermes injection or channel delivery.')
        if boundary == 'native_protagine':
            limits.append('Isolated native automatic recollection with lexical retrieval; physical channels, embeddings and reranking were not exercised.')
            limits.append('Requested answer-object checks combine strict format and values. Mechanism checks are shown separately; prose answers are not semantically regraded.')
        if invalid_reason is not None:
            limits.append('Invalid test: ' + invalid_reason)
            comparison = None
        if run['evidence_mode'] == 'controlled':
            limits.append('Controlled fixture evidence is not a real-model benchmark.')
        if not deployment['weights_verified']:
            limits.append('Loaded weight identity has not been independently verified for this recipe.')
        public_limits = metadata.get('limitations', [])
        public_conditions = metadata.get('conditions', [])
        if (not isinstance(public_limits, list) or len(public_limits) > 16
                or not isinstance(public_conditions, list) or len(public_conditions) > 16):
            raise ValueError('Too many public annotations')
        limits.extend(text(value) for value in public_limits)
        budgets = sorted({c['timeout_seconds'] for c in selected})
        conditions = [{'label': 'Declared case deadlines', 'value': ', '.join(str(v) for v in budgets) + ' seconds'},
                      {'label': 'Attempts per case', 'value': '1'},
                      {'label': 'Execution', 'value': 'Cases run sequentially'},
                      {'label': 'Capacity', 'value': 'Deployment context and slots are configured limits, not validated load results'}]
        candidate_concurrency = run.get('recipe', {}).get('declared', {}).get('concurrency')
        if type(candidate_concurrency) is int and candidate_concurrency > 0:
            conditions.append({'label': 'Candidate client concurrency cap', 'value': str(candidate_concurrency)})
        for condition in public_conditions:
            if not isinstance(condition, dict) or set(condition) != {'label', 'value'}:
                raise ValueError('Invalid public condition')
            conditions.append({key: text(condition[key]) for key in ('label', 'value')})
        public.append({'schema_version': 1, 'run_id': public_id,
            'started_at': stamp(run['created_at']),
            'completed_at': max(stamp(r['ended_at']) for r in rows) if completed else None,
            'status': status, 'evidence_mode': run['evidence_mode'], 'role': ROLES.get(role, 'system'),
            'boundary': boundary, 'phase': phase, 'suite_version': text(run['suite_version']),
            'suite_sha256': suite_hash, 'evaluator_sha256': evaluator_hash,
            'comparison_key': comparison, 'deployment': deployment,
            'counts': {'declared': len(cases), 'primary_passes': sum(
                c['outcome'] == c['primary_outcome'] == 'pass' for c in cases), 'outcomes': outcomes},
            'metrics': [{'id': 'case_duration_median', 'label': 'Median case duration',
                'value': statistics.median(durations) if durations else None, 'unit': 'ms',
                'samples': len(durations), 'definition': 'All observed attempt durations, including failures; not inference-only latency.'}],
            'cases': cases,
            'conditions': conditions,
            'limitations': limits, 'evidence': []})
    # Derived views have immutable identities too. A later diagnostic or metadata
    # correction creates a new artifact instead of changing bytes at an old URL.
    for item in public:
        item['run_id'] = identifier(item['run_id'] + '-' + digest(item)[:12])
    return public


def publish_snapshot(runs, output, *, published_at=None, benchmark=None):
    """Create a new immutable snapshot directory. No in-place result rewriting."""
    output = Path(output)
    if output.exists():
        raise FileExistsError('Choose a new publication snapshot directory')
    ids = [identifier(run['run_id']) for run in runs]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate public run IDs')
    description = benchmark or {'id': 'agent-benchmark', 'version': '1.0',
                               'title': 'Agent benchmark', 'planned_scenarios': 180,
                               'methodology_version': '1.0'}
    description = {key: text(description[key]) for key in ('id', 'version', 'title', 'methodology_version')} | {
        'planned_scenarios': (benchmark or {}).get('planned_scenarios', 180)}
    identifier(description['id'])
    if type(description['planned_scenarios']) is not int or description['planned_scenarios'] < 1:
        raise ValueError('Invalid planned scenario count')
    publication_time = stamp(published_at or datetime.now(timezone.utc).isoformat())
    # Validate and serialize before creating any output. Invalid metadata or
    # non-finite payloads must not leave an unusable half-written snapshot.
    encoded = [encode(run) for run in runs]
    manifest_runs = []
    progress = Counter(run['status'] for run in runs)
    for run, raw in zip(runs, encoded):
        leaf = f"runs/{run['run_id']}.json"
        write_once(output / leaf, run)
        manifest_runs.append({'run_id': run['run_id'], 'path': '/benchmarks/data/' + leaf,
                              'sha256': hashlib.sha256(raw).hexdigest()})
    manifest = {'schema_version': 1,
        'published_at': publication_time,
        'benchmark': description,
        'runs': manifest_runs,
        'progress': {'planned': None, 'queued': 0, 'running': 0,
                     **{key: progress[key] for key in ('completed', 'failed', 'aborted', 'invalid')}}}
    write_once(output / 'manifest.json', manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Private JSON list of {directory, metadata} inputs')
    parser.add_argument('--output', required=True, help='New immutable public snapshot directory')
    parser.add_argument('--benchmark', help='Public benchmark description JSON')
    args = parser.parse_args(argv)
    sources = read(args.source)
    runs = []
    for source in sources:
        runs.extend(export_records(source['directory'], source['metadata']))
    manifest = publish_snapshot(runs, args.output, benchmark=read(args.benchmark) if args.benchmark else None)
    print(json.dumps({'public_runs': len(runs), 'published_at': manifest['published_at']}))


if __name__ == '__main__':
    main()
