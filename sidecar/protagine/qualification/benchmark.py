"""Frozen batches over the existing qualification runner; no deployment machinery."""
import asyncio
from collections import Counter
import fcntl
import hashlib
import json
from pathlib import Path

from .benchmark_cases import LIMITATIONS, VERSION, screen_cases
from .cases import CONSUMERS, EVALUATORS
from .records import digest, publish, read, write_once
from .report import markdown, summarize
from .runner import evaluate, inspect_binding, materialize_role_cases, router_for

SCHEMA = 1


def _identity():
    names = ('benchmark.py', 'benchmark_cases.py', 'cases.py', 'memory_cases.py',
             'structured_cases.py', 'vision_cases.py', 'records.py', 'runner.py', 'report.py',
             'native.py', 'native_worker.py', 'native_identity.py', 'native_reasoning.py', 'native_coding.py')
    identity = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}
    # Qualification wrappers call the actual router, formation and recall code.
    # Freeze that payload too, including data/template resources. Test/report
    # tooling under qualification is covered separately by the explicit list.
    root = Path(__file__).parent.parent
    payload = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if (path.is_file() and relative.parts[0] != 'qualification'
                and '__pycache__' not in relative.parts and path.suffix not in {'.pyc', '.pyo'}):
            payload[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    identity['protagine_payload_sha256'] = digest(payload)
    return identity


def _stable(recipe):
    return {key: value for key, value in recipe.items() if key != 'observed_at'}


def _shards(cases):
    """Keep boundaries/configuration separate and honor summed declared deadlines."""
    groups = []
    for suite in ('standard', 'native'):
        current, seconds = [], 0
        for case in cases:
            if (case.boundary == 'native_hermes') != (suite == 'native'):
                continue
            if current and (len(current) >= 128 or seconds + case.timeout_seconds > 3600):
                groups.append((suite, current))
                current, seconds = [], 0
            current.append(case)
            seconds += case.timeout_seconds
        if current:
            groups.append((suite, current))
    return groups


def prepare(*, config_path, binding, native_config_path=None, native_binding=None,
            hermes_python=None, roles=None, boundaries=None, native_deadline_seconds=120,
            cleanup_seconds=5, evidence_mode='actual_inference', label='candidate'):
    """Resolve and freeze local declarations. Does not query inference endpoints."""
    if evidence_mode not in {'controlled', 'actual_inference'}:
        raise ValueError('Invalid evidence mode')
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        raise ValueError('Provide a nonempty candidate label of at most 120 characters')
    cases = screen_cases(native_deadline_seconds=native_deadline_seconds,
                         cleanup_seconds=cleanup_seconds)
    all_roles, all_boundaries = {c.role for c in cases}, {c.boundary for c in cases}
    selected_roles = sorted(all_roles if roles is None else set(roles))
    selected_boundaries = sorted(all_boundaries if boundaries is None else set(boundaries))
    if (not selected_roles or set(selected_roles) - all_roles or not selected_boundaries
            or set(selected_boundaries) - all_boundaries):
        raise ValueError('Select installed screening roles and boundaries')
    cases = [c for c in cases if c.role in selected_roles and c.boundary in selected_boundaries]
    if not cases:
        raise ValueError('Selection contains no cases')
    host_config = read(config_path)
    host_recipe = inspect_binding(host_config, binding)
    direct, policy = materialize_role_cases(host_config, binding,
        [c for c in cases if c.boundary != 'native_hermes'])
    host_recipe = _stable({**host_recipe, 'qualification_output_policy': policy})
    implementation = _identity()
    host_recipe['protagine_payload_sha256'] = implementation['protagine_payload_sha256']
    native_selected = [c for c in cases if c.boundary == 'native_hermes']
    native_config, native_recipe = None, None
    if native_selected:
        if not native_config_path or not native_binding:
            raise ValueError('Native cases require --native-config and --native-binding')
        from .native import configuration
        native_config, native_recipe = configuration(native_config_path, native_binding,
                                                      hermes_python=hermes_python)
        # Unavailable Hermes remains visible and will become setup_error when run.
        native_recipe = _stable(native_recipe)
    cases = direct + native_selected
    frozen, prepared = [], []
    counters = Counter()
    for suite, members in _shards(cases):
        counters[suite] += 1
        shard_id = f'{suite}-{counters[suite]:03d}'
        recipe = native_recipe if suite == 'native' else host_recipe
        shard = {'id': shard_id, 'suite': suite, 'path': 'runs/' + shard_id,
            'case_ids': [c.id for c in members], 'declared_cases': len(members),
            'declared_seconds': sum(c.timeout_seconds for c in members),
            'recipe': recipe, 'case_hashes': {c.id: c.record()['sha256'] for c in members}}
        frozen.append(shard)
        prepared.append((shard, members))
    manifest = {'schema': SCHEMA, 'kind': 'qualification_batch', 'suite_version': VERSION,
        'stage': 'development_screen', 'label': label, 'evidence_mode': evidence_mode,
        'options': {'binding': binding, 'native_binding': native_binding, 'roles': selected_roles,
            'boundaries': selected_boundaries, 'native_deadline_seconds': native_deadline_seconds,
            'cleanup_seconds': cleanup_seconds},
        'available_cases': 24, 'selected_cases': len(cases), 'shards': frozen,
        'cases': [{'case_id': c.id, 'role': c.role, 'boundary': c.boundary, 'version': c.version,
            'case_sha256': c.record()['sha256'], 'inputs_sha256': c.record()['inputs_sha256'],
            'oracle_sha256': c.record()['oracle_sha256'], 'coverage': LIMITATIONS[c.boundary]}
            for c in cases],
        'implementation': implementation, 'coverage': 'Only declared screening cases; not full role or agent qualification.',
        'unimplemented': ['native_automatic_recollection', 'cross_channel_shared_state',
            'learning_transfer', 'code_execution', 'load_and_streaming_performance',
            'speech_delivery', 'embedding_and_reranking', 'live_authority_enforcement']}
    manifest['sha256'] = digest(manifest)
    return manifest, prepared, host_config, native_config


def plan(directory, **kwargs):
    manifest, _, _, _ = prepare(**kwargs)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_once(directory / 'benchmark.json', manifest)
    return manifest


def inspect(directory):
    """Derive progress from immutable child runs, including absent/untouched shards."""
    directory = Path(directory)
    manifest = read(directory / 'benchmark.json')
    raw = {key: value for key, value in manifest.items() if key != 'sha256'}
    if (manifest.get('schema') != SCHEMA or manifest.get('kind') != 'qualification_batch'
            or manifest.get('sha256') != digest(raw)):
        raise ValueError('Invalid or modified benchmark manifest')
    runs, outcomes = [], Counter()
    for shard in manifest['shards']:
        # Paths in a loaded manifest are not permission to read arbitrary files.
        expected = 'runs/' + shard['id']
        if (shard['path'] != expected or Path(shard['id']).name != shard['id']
                or shard['id'] in {'', '.', '..'}):
            raise ValueError('Invalid shard path')
        target = directory / shard['path']
        if (target / 'run.json').exists():
            report = summarize(target)
            if ({row['case_id']: row['case_sha256'] for row in report['cases']} != shard['case_hashes']
                    or _stable(report['recipe']) != shard['recipe']
                    or report['evidence_mode'] != manifest['evidence_mode']):
                raise ValueError('Child run does not match frozen batch')
            counts = Counter(row['outcome'] for row in report['cases'])
            runs.append({'id': shard['id'], 'path': shard['path'], 'run_id': report['run_id'],
                'declared': len(report['cases']), 'outcomes': dict(counts),
                'primary_passes': sum(row.get('primary_outcome') == 'pass' for row in report['cases'])})
        else:
            counts = Counter({'not_run': shard['declared_cases']})
            runs.append({'id': shard['id'], 'path': shard['path'], 'run_id': None,
                'declared': shard['declared_cases'], 'outcomes': dict(counts), 'primary_passes': 0})
        outcomes.update(counts)
    return {'schema': SCHEMA, 'kind': 'qualification_batch_report', 'suite_version': manifest['suite_version'],
        'batch_sha256': manifest['sha256'], 'label': manifest['label'],
        'evidence_mode': manifest['evidence_mode'], 'declared': manifest['selected_cases'],
        'outcomes': dict(outcomes), 'runs': runs, 'coverage': manifest['coverage'],
        'unimplemented': manifest['unimplemented']}


async def run(directory, *, config_path, native_config_path=None, hermes_python=None, resume=False):
    """Execute only the frozen plan through existing consumers; no adaptive tuning."""
    directory = Path(directory)
    manifest = read(directory / 'benchmark.json')
    inspect(directory)
    expected, prepared, host_config, native_config = prepare(config_path=config_path,
        native_config_path=native_config_path, hermes_python=hermes_python,
        evidence_mode=manifest['evidence_mode'], label=manifest['label'], **manifest['options'])
    if expected != manifest:
        raise ValueError('Benchmark execution requires identical plan, configurations and implementation')
    from .native import native_cli, native_context
    consumers = {**CONSUMERS, 'native_cli': native_cli}

    def factory(shard, case):
        return (native_context(native_config, shard['recipe']) if shard['suite'] == 'native'
            else router_for(host_config, manifest['options']['binding'], [case]))

    return await execute_prepared(directory, manifest, prepared, consumers, EVALUATORS, factory,
                                  resume=resume)


async def execute_prepared(directory, manifest, prepared, consumers, evaluators, factory, *, resume=False):
    """Shared immutable batch execution after the caller validates its frozen plan."""
    directory = Path(directory)
    with (directory / '.benchmark.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not resume and any((directory / shard['path'] / 'run.json').exists() for shard, _ in prepared):
            raise ValueError('A child run already exists; use --resume to preserve completed attempts')
        # Native plugin trust checks reject writable ancestors. Do not let the
        # caller's umask create a permissive intermediate directory.
        (directory / 'runs').mkdir(mode=0o700, exist_ok=True)
        for shard, cases in prepared:
            target = directory / shard['path']
            existing = (target / 'run.json').exists()
            recipe = shard['recipe']
            await evaluate(target, recipe, cases, consumers, evaluators, lambda case: factory(shard, case),
                resume=existing, evidence_mode=manifest['evidence_mode'], suite_version=manifest['suite_version'])
            report = summarize(target)
            index = len(list(target.glob('report-*.json'))) + 1
            write_once(target / f'report-{index:03d}.json', report)
            publish(target / f'report-{index:03d}.md', markdown(report).encode())
            if any(row.get('cleanup') == 'state_directory_retained' for row in report['cases']):
                break
        result = inspect(directory)
        index = len(list(directory.glob('report-*.json'))) + 1
        write_once(directory / f'report-{index:03d}.json', result)
        return result


def add_parser(commands):
    parser = commands.add_parser('benchmark', help='Freeze/run a bounded development screening batch')
    subs = parser.add_subparsers(dest='benchmark_command', required=True)
    for command in ('plan', 'run', 'inspect'):
        item = subs.add_parser(command)
        item.add_argument('--output', type=Path, required=True, help='Private batch directory')
        if command != 'inspect':
            item.add_argument('--config', type=Path, required=True, help='Existing host model JSON')
            item.add_argument('--native-config', type=Path, help='Existing Hermes provider YAML')
            item.add_argument('--hermes-python', type=Path)
        if command == 'plan':
            item.add_argument('--binding', required=True)
            item.add_argument('--native-binding')
            item.add_argument('--roles', help='Comma-separated subset; default all screening roles')
            item.add_argument('--boundaries', help='Comma-separated subset; default all screening boundaries')
            item.add_argument('--native-deadline-seconds', type=float, default=120)
            item.add_argument('--cleanup-seconds', type=float, default=5)
            item.add_argument('--label', default='candidate')
            item.add_argument('--evidence-mode', choices=['controlled', 'actual_inference'], default='actual_inference')
        if command == 'run':
            item.add_argument('--resume', action='store_true')


def cli(args):
    if args.benchmark_command == 'inspect':
        result = inspect(args.output)
    elif args.benchmark_command == 'plan':
        result = plan(args.output, config_path=args.config, binding=args.binding,
            native_config_path=args.native_config, native_binding=args.native_binding,
            hermes_python=args.hermes_python,
            roles=[v.strip() for v in args.roles.split(',')] if args.roles else None,
            boundaries=[v.strip() for v in args.boundaries.split(',')] if args.boundaries else None,
            native_deadline_seconds=args.native_deadline_seconds, cleanup_seconds=args.cleanup_seconds,
            label=args.label, evidence_mode=args.evidence_mode)
    else:
        result = asyncio.run(run(args.output, config_path=args.config,
            native_config_path=args.native_config, hermes_python=args.hermes_python, resume=args.resume))
    print(json.dumps(result, indent=2))
    return 0 if args.benchmark_command != 'run' or set(result['outcomes']) <= {'pass'} else 1
