"""Portable frozen plans over existing qualification cases and consumers."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from . import benchmark
from .pack_registry import PACKS, modules
from .records import digest, read, write_once
from .runner import inspect_binding, materialize_role_cases, router_for

VERSION = 'portable-qualification-packs-1'
RESOURCE_NAMES = ('config', 'native_config', 'support_config', 'retrieval_config', 'sandbox_config')


def implementation_identity():
    # Include factories, observers, oracles, worker helpers and image fixtures.
    # Installed wheels and source trees use the same relative identities.
    root = Path(__file__).parent
    files = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob('*')) if path.is_file()
        and '__pycache__' not in path.parts and path.suffix not in {'.pyc', '.pyo'}}
    return {**benchmark._identity(), 'qualification_payload_sha256': digest(files)}


def _native_packages(recipe):
    """Inspect selected installed adapters without opening a deployed agent home."""
    from .native import _environment
    inspector = Path(__file__).with_name('native_memory_identity.py')
    try:
        with tempfile.TemporaryDirectory(prefix='protagine-pack-inspect-') as home:
            result = subprocess.run([recipe['native_runtime']['python'], '-I', '-B', str(inspector)],
                cwd=home, env=_environment(Path(home), {'providers': {}}),
                capture_output=True, text=True, timeout=15, check=True)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ValueError('Selected Hermes interpreter requires the Protagine sidecar and both native adapters') from exc


def _sandbox_resource(sandbox):
    """Read installed image identity only; never pull, create or start a container."""
    from .coding_sandbox import validate_sandbox
    validate_sandbox(sandbox)
    try:
        with tempfile.TemporaryDirectory(prefix='protagine-sandbox-inspect-') as home:
            env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
            env.update(HOME=home, DOCKER_CONFIG=str(Path(home) / 'docker-config'))
            if sandbox['docker_host']:
                env['DOCKER_HOST'] = sandbox['docker_host']
            result = subprocess.run(['docker', 'image', 'inspect', sandbox['image'], '--format', '{{.Id}}'],
                cwd=home, env=env, capture_output=True, text=True, timeout=10, check=True)
        identity = result.stdout.strip()
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', identity):
            raise ValueError('Docker did not return a pinned image identity')
        return {'installed_image_id': identity}
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('Sandbox requires a reachable declared Docker daemon and an already installed image') from exc


def _support_snapshot(router, tasks, *, candidate_role=None):
    from protagine.router.functions import select_role
    snapshot = router.routing_status()
    for task in tasks:
        role = select_role(router._snapshot, {'task': task})
        if role == candidate_role:
            raise ValueError('Supporting task ' + task + ' shares the pinned candidate role; configure a separate support role')
        if not snapshot['roles'].get(role):
            raise ValueError('Missing configured supporting role for ' + task)
    return snapshot


def _fixture(path, expected_sha256):
    if path is None:
        if expected_sha256 is not None:
            raise ValueError('A held-out plan requires its explicit --fixture-pack on every run')
        return None, None
    if not isinstance(expected_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_sha256):
        raise ValueError('Explicit fixture loading requires --fixture-sha256 from records.digest(JSON)')
    document = read(path)
    if digest(document) != expected_sha256:
        raise ValueError('Fixture does not match its declared canonical JSON hash')
    if document.get('split', 'held_out') != 'held_out':
        raise ValueError('--fixture-pack is reserved for an explicitly selected held_out pack')
    return document, {'sha256': expected_sha256, 'split': 'held_out'}


def prepare(*, pack, output, config=None, binding=None, native_config=None, native_binding=None,
            hermes_python=None, support_config=None, retrieval_config=None, sandbox_config=None,
            fallback_binding=None, fixture_pack=None, fixture_sha256=None, case_ids=None,
            distinct_reader=False, label='candidate', evidence_mode='actual_inference'):
    """Resolve local declarations and resources; never query a model endpoint."""
    spec, cases_module, consumer_module = modules(pack)
    if evidence_mode not in {'actual_inference', 'controlled'}:
        raise ValueError('Invalid evidence mode')
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        raise ValueError('Provide a nonempty label of at most 120 characters')
    host = spec.mode in {'host', 'perspective', 'router'}
    native = spec.mode in {'native', 'perspective', 'semantic'}
    required = {'config': host, 'native_config': native, 'support_config': spec.mode == 'semantic',
                'retrieval_config': spec.mode == 'semantic', 'sandbox_config': spec.sandbox}
    paths = dict(config=config, native_config=native_config, support_config=support_config,
                 retrieval_config=retrieval_config, sandbox_config=sandbox_config)
    for name, needed in required.items():
        if needed != (paths[name] is not None):
            raise ValueError(f'{pack} ' + ('requires ' if needed else 'does not use ') + '--' + name.replace('_', '-'))
    if host != bool(binding) or native != bool(native_binding):
        raise ValueError('Provide --binding for host packs and --native-binding for native packs only')
    if (hermes_python and not native) or (distinct_reader and spec.mode != 'perspective'):
        raise ValueError('Native interpreter/reader options do not apply to this pack')
    if (spec.mode == 'router') != bool(fallback_binding):
        raise ValueError('--fallback-binding is required only for router-recovery')
    document, fixture = _fixture(fixture_pack, fixture_sha256)
    resources = {name: read(path) for name, path in paths.items() if path is not None and name != 'native_config'}
    if pack == 'unified':
        if document is not None:
            raise ValueError('The unified pack has no installed held-out factory')
        cases = cases_module.cases() + cases_module.cases(arm='base_hermes')
    elif pack in {'evidence', 'interaction'}:
        cases = (deepcopy(cases_module.CASES) if document is None
                 else cases_module.load_private(fixture_pack, fixture_sha256))
    elif spec.sandbox:
        cases = cases_module.cases(resources['sandbox_config'], document)
    elif spec.mode == 'semantic':
        cases = cases_module.cases(resources['retrieval_config'], document)
    else:
        cases = cases_module.cases(document)
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError('Pack must contain distinct case IDs')
    available = len(cases)
    if case_ids is not None:
        if not isinstance(case_ids, list) or not case_ids or len(set(case_ids)) != len(case_ids):
            raise ValueError('Select distinct, nonempty case IDs')
        if set(case_ids) - {case.id for case in cases}:
            raise ValueError('Case IDs must belong to the explicitly selected pack/split')
        cases = [case for case in cases if case.id in case_ids]
    split = 'held_out' if fixture else 'development'
    if any(case.provenance != ('private' if fixture else 'public') for case in cases):
        raise ValueError('Pack case provenance disagrees with its declared split')
    for case in cases:
        case.record()
        if case.consumer not in consumer_module.CONSUMERS or case.evaluator not in consumer_module.EVALUATORS:
            raise ValueError('Pack has no installed consumer/evaluator for ' + case.id)
    resource_identity = {name + '_sha256': digest(value) for name, value in resources.items()}
    from .native_memory_identity import inspect_runtime
    resource_identity['controller_runtime'] = inspect_runtime(('protagine',))
    recipe = {}
    if host:
        recipe = benchmark._stable(inspect_binding(resources['config'], binding))
        cases, policy = materialize_role_cases(resources['config'], binding, cases)
        recipe['qualification_output_policy'] = policy
        # Validate the same routing copy used by execution, preserving all
        # independently configured supporting roles and task mappings.
        for case in cases:
            router = router_for(resources['config'], binding, [case])
            tasks = (('source_claim_review',) if pack == 'formation' else
                     ('source_claim_extraction', 'source_claim_review') if pack == 'perspective' else ())
            _support_snapshot(router, tasks, candidate_role=case.role)
    selected, native_recipe = None, None
    if native:
        from .native import configuration
        from .native_memory_batch import preflight_output
        preflight_output(Path(output) / 'runs')
        selected, native_recipe = configuration(native_config, native_binding, hermes_python=hermes_python)
        if native_recipe['native_runtime']['status'] != 'ready':
            raise ValueError('Selected Hermes interpreter is unavailable; provide --hermes-python')
        # Do not wait until after source-formation calls to discover a missing
        # provider credential variable. Values never enter the manifest.
        variables = set(re.findall(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', json.dumps(selected)))
        for provider in selected['providers'].values():
            variables.update(provider[key] for key in ('key_env', 'api_key_env') if provider.get(key))
        if any(not os.environ.get(name) for name in variables):
            raise ValueError('Selected native provider references an unset credential environment variable')
        native_recipe = benchmark._stable(native_recipe)
        resource_identity['native_config_sha256'] = native_recipe['config_sha256']
        if pack not in {'planning', 'recovery'}:
            resource_identity['native_protagine_runtime'] = _native_packages(native_recipe)
        if spec.mode == 'perspective':
            recipe['fixed_reader_recipe'] = native_recipe
        else:
            recipe = deepcopy(native_recipe)
    if spec.mode == 'semantic':
        from protagine.router import LLMRouter
        supporting = LLMRouter(tiers={})
        supporting.configure(resources['support_config'])
        recipe['supporting_router_snapshot'] = _support_snapshot(supporting,
            ('source_claim_extraction', 'source_claim_review'))
        versions = resource_identity['native_protagine_runtime']['distribution_versions']
        installed = {name.lower().replace('_', '-') for name in versions}
        if not {'lancedb', 'pyarrow'} <= installed:
            raise ValueError('Semantic native interpreter requires lancedb and pyarrow')
    if spec.mode == 'router':
        consumer_module.RecoveryRouter(resources['config'], binding, fallback_binding)
        recipe['supporting_fallback_recipe'] = benchmark._stable(inspect_binding(resources['config'], fallback_binding))
    if spec.sandbox:
        resource_identity['sandbox_resource'] = _sandbox_resource(resources['sandbox_config'])
    if distinct_reader:
        recipe['declared'] = {**recipe.get('declared', {}), 'distinct_identity_processors': True}
    metadata = getattr(consumer_module, 'recipe_metadata', None)
    if metadata:
        recipe.update(metadata(cases, resources['sandbox_config']) if pack == 'authority' else metadata(cases))
    implementation = implementation_identity()
    recipe.update(pack=pack, split=split, pack_version=cases_module.VERSION,
                  resources=resource_identity, implementation=implementation, basis=spec.coverage)
    frozen, prepared = [], []
    for index, (suite, members) in enumerate(benchmark._shards(cases), 1):
        identity = f'{pack}-{index:03d}'
        shard = {'id': identity, 'suite': suite, 'path': 'runs/' + identity, 'recipe': recipe,
            'case_ids': [case.id for case in members], 'declared_cases': len(members),
            'declared_seconds': sum(case.timeout_seconds for case in members),
            'case_hashes': {case.id: case.record()['sha256'] for case in members}}
        frozen.append(shard)
        prepared.append((shard, members))
    manifest = {'schema': 1, 'kind': 'qualification_batch', 'orchestrator': VERSION,
        'suite_version': cases_module.VERSION, 'stage': split, 'label': label,
        'evidence_mode': evidence_mode, 'available_cases': available, 'selected_cases': len(cases),
        'declared_seconds': sum(case.timeout_seconds for case in cases), 'shards': frozen,
        'options': {'pack': pack, 'binding': binding, 'native_binding': native_binding,
            'fallback_binding': fallback_binding, 'fixture_sha256': fixture_sha256,
            'case_ids': [case.id for case in cases], 'distinct_reader': distinct_reader},
        'fixture': fixture, 'implementation': implementation, 'coverage': spec.coverage,
        'unimplemented': list(spec.unimplemented),
        'cases': [{'case_id': case.id, 'role': case.role, 'boundary': case.boundary, 'version': case.version,
            'split': split, 'provenance': case.provenance, 'case_sha256': case.record()['sha256'],
            'comparison_arm': case.inputs.get('arm', 'protagine') if pack == 'unified' else None,
            'inputs_sha256': case.record()['inputs_sha256'], 'oracle_sha256': case.record()['oracle_sha256'],
            'timeout_seconds': case.timeout_seconds, 'required_capabilities': list(case.required_capabilities),
            'coverage': spec.coverage} for case in cases]}
    manifest['sha256'] = digest(manifest)

    def factory(shard, case):
        if spec.mode == 'router':
            return consumer_module.RecoveryRouter(resources['config'], binding, fallback_binding)
        if spec.mode == 'host':
            return router_for(resources['config'], binding, [case])
        from .native import native_context
        reader = native_context(selected, native_recipe)
        if spec.mode == 'native':
            return reader
        from .native_memory import MemoryRouter
        if spec.mode == 'perspective':
            return MemoryRouter(router_for(resources['config'], binding, [case]), reader)
        from protagine.router import LLMRouter
        supporting = LLMRouter(tiers={})
        supporting.configure(deepcopy(resources['support_config']))
        return MemoryRouter(supporting, reader)

    return manifest, prepared, consumer_module.CONSUMERS, consumer_module.EVALUATORS, factory


def plan(output, **kwargs):
    manifest, *_ = prepare(output=output, **kwargs)
    Path(output).mkdir(mode=0o700, parents=True, exist_ok=False)
    write_once(Path(output) / 'benchmark.json', manifest)
    return manifest


async def run(output, *, resume=False, **resources):
    manifest = read(Path(output) / 'benchmark.json')
    benchmark.inspect(output)
    if manifest.get('orchestrator') != VERSION:
        raise ValueError('Use the command that created this frozen plan')
    expected, prepared, consumers, evaluators, factory = prepare(output=output,
        **manifest['options'], label=manifest['label'], evidence_mode=manifest['evidence_mode'], **resources)
    if expected != manifest:
        raise ValueError('Run requires identical frozen fixtures, resources and implementation; create a new plan')
    return await benchmark.execute_prepared(output, manifest, prepared, consumers, evaluators, factory, resume=resume)


def add_parser(commands):
    parser = commands.add_parser('packs', help='Plan/run installed behavioral packs with explicit resources')
    sub = parser.add_subparsers(dest='pack_command', required=True)
    for command in ('plan', 'run', 'inspect'):
        item = sub.add_parser(command)
        item.add_argument('--output', type=Path, required=True, help='Private immutable batch directory')
        if command != 'inspect':
            for resource in RESOURCE_NAMES:
                item.add_argument('--' + resource.replace('_', '-'), type=Path)
            item.add_argument('--hermes-python', type=Path)
            item.add_argument('--fixture-pack', type=Path, help='Explicit private held-out JSON; never discovered automatically')
        if command == 'plan':
            item.add_argument('--pack', required=True, choices=sorted(PACKS))
            item.add_argument('--binding', help='Candidate host binding for host/perspective/router packs')
            item.add_argument('--native-binding', help='Native candidate, or fixed reader for perspective')
            item.add_argument('--fallback-binding', help='Independent supporting fallback for router-recovery')
            item.add_argument('--fixture-sha256', help='Canonical JSON digest from qualification.records.digest')
            item.add_argument('--case-ids', help='Comma-separated subset from the chosen pack/split')
            item.add_argument('--distinct-reader', action='store_true',
                help='Declare distinct perspective processors; actual returned identities remain graded')
            item.add_argument('--label', default='candidate')
            item.add_argument('--evidence-mode', choices=['actual_inference', 'controlled'], default='actual_inference')
        elif command == 'run':
            item.add_argument('--resume', action='store_true', help='Continue untouched cases; never replay a started attempt')


def cli(args):
    if args.pack_command == 'inspect':
        result = benchmark.inspect(args.output)
    else:
        resources = {name: getattr(args, name) for name in RESOURCE_NAMES}
        resources.update(hermes_python=args.hermes_python, fixture_pack=args.fixture_pack)
        if args.pack_command == 'plan':
            result = plan(args.output, pack=args.pack, binding=args.binding, native_binding=args.native_binding,
                fallback_binding=args.fallback_binding, fixture_sha256=args.fixture_sha256,
                case_ids=[value.strip() for value in args.case_ids.split(',')] if args.case_ids else None,
                distinct_reader=args.distinct_reader, label=args.label, evidence_mode=args.evidence_mode, **resources)
        else:
            result = asyncio.run(run(args.output, resume=args.resume, **resources))
    print(json.dumps(result, indent=2))
    return 0 if args.pack_command != 'run' or set(result['outcomes']) <= {'pass'} else 1
