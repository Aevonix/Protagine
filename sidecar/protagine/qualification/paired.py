"""Frozen, alternating pairs of fresh containerized Hermes episodes."""
import asyncio
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sys

from .pack_batch import implementation_identity
from .records import digest, publish, read, write_once
from .runner import evaluate

VERSION = 'paired-runner-1'
ARMS = ('base_hermes', 'protagine')
ENDPOINT_WARNING = (
    'Benchmark calls may slow a live agent using the same endpoint, and competing traffic '
    'distorts timings. Prefer an idle endpoint when practical; sharing is allowed and '
    'this command does not stop the live agent. Endpoint usage is self-reported.')


def _policy(path):
    value = read(path)
    if (not isinstance(value, dict) or value.get('version') != 'paired-policy-1'
            or value.get('budget_mode') not in {'matched_work', 'deployment_policy'}
            or not isinstance(value.get('budget_policy'), dict) or not value['budget_policy']
            or not isinstance(value.get('environment'), dict)
            or value['environment'].get('endpoint_usage') not in {'idle_declared', 'shared', 'unknown'}):
        raise ValueError('Comparison policy requires version paired-policy-1, budget_mode, '
                         'nonempty budget_policy, and environment.endpoint_usage')
    digest(value)  # Reject nonfinite/unserializable declarations before any execution.
    return value


def _task(case):
    value = case.record()
    value = {key: item for key, item in value.items()
             if key not in {'sha256', 'inputs_sha256', 'oracle_sha256'}}
    value['inputs'] = {key: item for key, item in value['inputs'].items() if key != 'arm'}
    return value


def _execution_identity(consumers, evaluators):
    """Match the ordinary runner's source identity without replacing its grading."""
    source = Path(__file__).with_name('runner.py')
    identity = {source.name: hashlib.sha256(source.read_bytes()).hexdigest()}
    for key, function in {**{'consumer:' + key: value for key, value in consumers.items()},
                          **{'evaluator:' + key: value for key, value in evaluators.items()}}.items():
        path = inspect.getsourcefile(function)
        identity[key] = {'module': function.__module__, 'name': function.__qualname__,
            'source_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None}
    return digest(identity)


def prepare(*, output, native_config, native_binding, comparison_policy, container_image,
            docker_host=None, case_ids=None, dataset_version=None, repetitions=1,
            label='candidate', evidence_mode='actual_inference'):
    """Resolve local recipes and pinned image identity without calling a model."""
    from . import paired_cases, paired_container
    from .native_memory_batch import preflight_output
    if evidence_mode not in {'actual_inference', 'controlled'}:
        raise ValueError('Invalid evidence mode')
    if type(repetitions) is not int or not 1 <= repetitions <= 3:
        raise ValueError('Paired repetitions must be an integer from 1 to 3')
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        raise ValueError('Provide a nonempty label of at most 120 characters')
    if case_ids is not None and (not isinstance(case_ids, list) or not case_ids
            or len(set(case_ids)) != len(case_ids)):
        raise ValueError('Select distinct, nonempty case IDs')
    policy = _policy(comparison_policy)
    preflight_output(Path(output) / 'runs')
    selected, recipe = paired_container.configuration(native_config, native_binding,
        image=container_image, docker_host=docker_host)
    recipe = {key: value for key, value in recipe.items() if key != 'observed_at'}
    variables = set(re.findall(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', json.dumps(selected)))
    for provider in selected['providers'].values():
        variables.update(provider[key] for key in ('key_env', 'api_key_env') if provider.get(key))
    if any(not os.environ.get(name) for name in variables):
        raise ValueError('Selected native provider references an unset credential environment variable')
    dataset_options = {'dataset_version': dataset_version} if dataset_version is not None else {}
    by_arm = {arm: paired_cases.cases(arm=arm, case_ids=case_ids, **dataset_options) for arm in ARMS}
    if dataset_version == paired_cases.WORKFLOW_VERSION:
        from .paired_workflow_runtime import PROTOCOL as workflow_protocol
        if recipe.get('container_payload', {}).get('workflow_protocol') != workflow_protocol:
            raise ValueError('Frozen workflows require an image with process-restart workflow support')
    identifiers = [case.id for case in by_arm[ARMS[0]]]
    if not 1 <= len(identifiers) <= 128 or len(set(identifiers)) != len(identifiers):
        raise ValueError('Paired plan requires 1..128 distinct episodes')
    if identifiers != [case.id for case in by_arm[ARMS[1]]]:
        raise ValueError('Both arms must declare the same episode IDs and order')
    for left, right in zip(*(by_arm[arm] for arm in ARMS)):
        if _task(left) != _task(right):
            raise ValueError('Paired tasks, oracles and budgets may differ only in inputs.arm')
        for arm, case in zip(ARMS, (left, right)):
            if (case.inputs.get('arm') != arm or case.boundary != 'native_hermes'
                    or case.consumer != 'native_paired' or case.evaluator != 'paired_artifacts'
                    or case.consumer not in paired_container.CONSUMERS
                    or case.evaluator not in paired_cases.EVALUATORS):
                raise ValueError('Invalid paired native case contract')
    tasks = [_task(case) for case in by_arm[ARMS[0]]]
    declarations = [case.inputs.get('dataset') for case in by_arm[ARMS[0]]]
    declared = declarations[0]
    if any(item is not None for item in declarations) and (not isinstance(declared, dict)
            or any(item != declared for item in declarations)
            or any(case.version != declared.get('version') for case in by_arm[ARMS[0]])):
        raise ValueError('Episodes must identify one consistent dataset version and source hash')
    dataset = {'version': declared['version'] if declared else paired_cases.VERSION, 'sha256': digest(tasks),
               'source_sha256': declared['sha256'] if declared else getattr(paired_cases, 'DATASET_SHA256', None),
               'split': declared.get('split', 'development') if declared else 'development',
               'episode_ids': identifiers}
    if repetitions > 1:
        dataset['repetitions'] = repetitions
    implementation = implementation_identity()
    # The candidate recipe is also frozen below. This key declares the shared
    # task/budget/environment cohort; it does not attest identical serving loads.
    comparison = {'dataset': dataset, 'policy': policy, 'implementation': implementation,
                  'container_recipe_sha256': digest(recipe), 'evidence_mode': evidence_mode}
    comparison_key = digest(comparison)
    recipe = {**recipe, 'paired_version': VERSION, 'paired_dataset': dataset,
        'paired_policy': policy, 'comparison_key': comparison_key,
        'implementation': implementation}
    pairs, prepared = [], []
    for pair_index in range(len(identifiers) * repetitions):
        repetition, index = divmod(pair_index, len(identifiers))
        case_id = identifiers[index]
        order = list(ARMS if (index + repetition) % 2 == 0 else reversed(ARMS))
        pair = {'episode_id': case_id, 'order': order, 'task_sha256': digest(tasks[index]),
                'oracle_sha256': digest(by_arm[ARMS[0]][index].oracle), 'arms': {}}
        if repetitions > 1:
            pair.update(episode_id=f'{case_id}.repeat-{repetition + 1:02d}',
                        workflow_id=case_id, repetition=repetition + 1)
        for arm in ARMS:
            case = by_arm[arm][index]
            pair['arms'][arm] = {'path': f'runs/{pair_index + 1:03d}-{arm}',
                'case': case.record(), 'recipe_sha256': digest(recipe)}
        pairs.append(pair)
        prepared.extend((pair['arms'][arm], by_arm[arm][index]) for arm in order)
    manifest = {'schema': 1, 'kind': 'paired_qualification', 'orchestrator': VERSION,
        'label': label, 'evidence_mode': evidence_mode, 'recipe': recipe, 'dataset': dataset,
        'comparison': comparison, 'comparison_key': comparison_key,
        'selected_episodes': len(pairs), 'declared_attempts': len(pairs) * 2,
        'declared_seconds': sum(case.timeout_seconds for _, case in prepared),
        'order_policy': 'alternate-first-arm-by-declared-episode-index',
        'state_policy': 'fresh-container-and-state-per-arm-episode; persistence-within-episode',
        'budget_verification': 'declared; accounting/enforcement require execution evidence',
        'options': {'native_binding': native_binding, 'case_ids': identifiers, 'dataset_version': dataset_version},
        'pairs': pairs, 'implementation': implementation,
        'execution_implementation_sha256': _execution_identity(paired_container.CONSUMERS, paired_cases.EVALUATORS),
        'coverage': 'Development pilot episodes; no held-out, deployment or model-tier qualification.'}
    if repetitions > 1:
        manifest['options']['repetitions'] = repetitions
        manifest['order_policy'] = 'alternate-first-arm-by-workflow-and-repetition-index'
    if dataset_version == paired_cases.WORKFLOW_VERSION:
        manifest['coverage'] = ('Frozen public workflow evaluation, not a private holdout. '
            'Repeated trials share workflow designs; no independent-sample or production qualification claim.')
    manifest['sha256'] = digest(manifest)
    return (manifest, prepared, paired_container.CONSUMERS, paired_cases.EVALUATORS,
            lambda _: paired_container.context(deepcopy(selected), recipe))


def plan(output, **kwargs):
    manifest, *_ = prepare(output=output, **kwargs)
    Path(output).mkdir(mode=0o700, parents=True, exist_ok=False)
    (Path(output) / 'runs').mkdir(mode=0o700)
    write_once(Path(output) / 'paired.json', manifest)
    return manifest


async def run(output, *, resume=False, **resources):
    from .paired_report import load_manifest, summarize, markdown
    output = Path(output)
    manifest = load_manifest(output)
    expected, prepared, consumers, evaluators, factory = prepare(output=output,
        **manifest['options'], label=manifest['label'], evidence_mode=manifest['evidence_mode'], **resources)
    if expected != manifest:
        raise ValueError('Run requires identical frozen dataset, policy, container and implementation; create a new plan')
    with (output / '.paired.lock').open('a') as lock:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not resume and any((output / member['path']).exists() for member, _ in prepared):
            raise ValueError('Use --resume to continue untouched episodes; started attempts are never rerun')
        for member, case in prepared:
            destination = output / member['path']
            await evaluate(destination, manifest['recipe'], [case], consumers, evaluators, factory,
                resume=destination.exists(), evidence_mode=manifest['evidence_mode'],
                suite_version=manifest['dataset']['version'])
            result = read(destination / 'attempts' / case.id / 'result.json')
            if result.get('cleanup') in {'state_directory_retained', 'unconfirmed', 'failed'}:
                break
        result = summarize(output)
        index = len(list(output.glob('report-*.json'))) + 1
        write_once(output / f'report-{index:03d}.json', result)
        publish(output / f'report-{index:03d}.md', markdown(result).encode())
        return result


def add_parser(commands):
    parser = commands.add_parser('paired', help='Compare fresh Hermes and Hermes + Protagine on identical episodes')
    sub = parser.add_subparsers(dest='paired_command', required=True)
    for command in ('plan', 'run', 'report', 'export'):
        item = sub.add_parser(command)
        item.add_argument('--output', type=Path, required=True, help='Private immutable paired-run directory')
        if command in {'plan', 'run'}:
            item.add_argument('--native-config', type=Path, required=True, help='Private common Hermes provider configuration')
            item.add_argument('--comparison-policy', type=Path, required=True, help='Versioned budget and environment declarations')
            item.add_argument('--container-image', required=True, help='Same digest-pinned image for both arms')
            item.add_argument('--docker-host', help='Declared Docker daemon; never mounted inside the benchmark')
        if command == 'plan':
            item.add_argument('--native-binding', required=True)
            item.add_argument('--dataset-version', help='Explicit installed fixture version; frozen into the plan')
            item.add_argument('--repetitions', type=int, choices=(1, 2, 3), default=1,
                              help='Complete paired repetitions, frozen before execution; use 3 for workflow release evaluation')
            item.add_argument('--case-ids', help='Comma-separated installed episode IDs; selects both arms together')
            item.add_argument('--label', default='candidate')
            item.add_argument('--evidence-mode', choices=['actual_inference', 'controlled'], default='actual_inference')
        elif command == 'run':
            item.add_argument('--resume', action='store_true', help='Continue untouched episodes; never replay a started attempt')
        elif command == 'report':
            item.add_argument('--json', action='store_true')
        else:
            item.add_argument('--metadata', type=Path, required=True, help='Separately authored public deployment metadata')
            item.add_argument('--public-output', type=Path, required=True, help='New public snapshot directory')


def cli(args):
    from .paired_report import summarize, markdown
    if args.paired_command == 'export':
        from .paired_public import publish_record
        result = publish_record(args.output, read(args.metadata), args.public_output)
        print(json.dumps(result, indent=2))
        return 0
    if args.paired_command == 'report':
        result = summarize(args.output)
        print(json.dumps(result, indent=2) if args.json else markdown(result))
        return 0
    print(ENDPOINT_WARNING, file=sys.stderr)
    resources = {key: getattr(args, key) for key in
        ('native_config', 'comparison_policy', 'container_image', 'docker_host')}
    if args.paired_command == 'plan':
        result = plan(args.output, native_binding=args.native_binding,
            dataset_version=args.dataset_version,
            repetitions=args.repetitions,
            case_ids=[value.strip() for value in args.case_ids.split(',')] if args.case_ids else None,
            label=args.label, evidence_mode=args.evidence_mode, **resources)
        print(json.dumps(result, indent=2))
        return 0
    result = asyncio.run(run(args.output, resume=args.resume, **resources))
    print(markdown(result))
    # A completed comparison may legitimately find either model inadequate.
    # Keep task quality in the report; reserve a nonzero status for a cohort
    # without attributable results, so campaigns do not stop on model failures.
    return 0 if result['paired_score'] is not None else 1
