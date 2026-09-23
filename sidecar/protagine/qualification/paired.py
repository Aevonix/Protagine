"""Frozen, rotating N-arm sets of fresh containerized Hermes episodes."""
import asyncio
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sys

from . import paired_arms
from .pack_batch import implementation_identity
from .paired_worker import (ARM_PROFILE_PROTOCOL, EAGER_TOOLS_CONFIG, ENVIRONMENT_NOTE_PROTOCOL,
                            ENVIRONMENT_NOTES, MESSAGE_TIMESTAMP_FORMAT, MESSAGE_TIMESTAMPS_MODES,
                            MESSAGE_TIMESTAMPS_PROTOCOL, MIND_SWITCHES, MIND_TICK_PROTOCOL, PROFILE_SWITCHES,
                            TOOL_LOADING_MODES, TOOL_LOADING_PROTOCOL)
from .records import digest, publish, read, write_once
from .runner import evaluate

VERSION = 'paired-runner-1'
# An arm profile names whether the Protagine plugin is installed, which
# PROTAGINE_* flags are overlaid after the fixture's own forced flags, and which
# binary comparator switches are on (a switch is listed only when it is on).
# Model, budget, endpoint and oracle settings are never part of a profile.
PROFILES = {'base_hermes': {'plugin': False, 'overlay': {}},
            'protagine': {'plugin': True, 'overlay': {}},
            'base-heartbeat': {'plugin': False, 'overlay': {}, 'heartbeat': True},
            'base-curator': {'plugin': False, 'overlay': {}, 'curator': True},
            # The treatment arm of mind-initiative-1: the plugin with the mind on
            # (autonomy standard, only the initiative faculty), ticked by the body tick.
            'protagine-initiative': {'plugin': True, 'overlay': {}, 'initiative': True},
            # The drives family (evals section 6.6): every faculty at its release-candidate
            # value, the flat-priority ablation (weights 1, no satiation, no goal adoption), the
            # broadcast ablation, and one diagnostic per drive (its weight set to 0).
            'full': {'plugin': True, 'overlay': {}, 'full': True},
            'full-drives': {'plugin': True, 'overlay': {}, 'full': True, 'minus_drives': True},
            'full-broadcast': {'plugin': True, 'overlay': {}, 'full': True, 'minus_broadcast': True},
            'full-duty': {'plugin': True, 'overlay': {}, 'full': True, 'minus_duty': True},
            'full-curiosity': {'plugin': True, 'overlay': {}, 'full': True, 'minus_curiosity': True},
            'full-mastery': {'plugin': True, 'overlay': {}, 'full': True, 'minus_mastery': True},
            'full-upkeep': {'plugin': True, 'overlay': {}, 'full': True, 'minus_upkeep': True},
            'full-social': {'plugin': True, 'overlay': {}, 'full': True, 'minus_social': True},
            # One ablation per later faculty, each ``full`` with that faculty's flag off (the
            # faculty claim of its family's gate); the flag is served whether or not the
            # faculty's code has landed, so the arm is a no-op contrast until its milestone.
            'full-people': {'plugin': True, 'overlay': {}, 'full': True, 'minus_people': True},
            'full-affect': {'plugin': True, 'overlay': {}, 'full': True, 'minus_affect': True},
            'full-opinions': {'plugin': True, 'overlay': {}, 'full': True, 'minus_opinions': True},
            'full-semantic_recall': {'plugin': True, 'overlay': {}, 'full': True, 'minus_semantic_recall': True},
            'full-consolidation': {'plugin': True, 'overlay': {}, 'full': True, 'minus_consolidation': True},
            'full-self_narrative': {'plugin': True, 'overlay': {}, 'full': True, 'minus_self_narrative': True}}
ARMS = ('base_hermes', 'protagine')
BUILT_IN_PAIR = {name: PROFILES[name] for name in ARMS}
HEARTBEAT = {'prompt_sha256': paired_arms.HEARTBEAT_PROMPT_SHA256,
             'extra_toolsets': list(paired_arms.HEARTBEAT_EXTRA_TOOLSETS),
             'deliver': paired_arms.HEARTBEAT_DELIVER}
# What a dataset's declared tool loading means inside the image, recorded in the
# plan when a dataset declares one; the same Hermes config keys in every arm.
TOOL_LOADING = {'eager': {'protocol': TOOL_LOADING_PROTOCOL, 'mode': 'eager',
                          'config': {'tools': deepcopy(EAGER_TOOLS_CONFIG)}}}
# The body clock every model-facing turn carries (owner turns, inbound messages
# and cron prompts alike), in the stock gateway message timestamp format.
MESSAGE_TIMESTAMPS = {'gateway': {'protocol': MESSAGE_TIMESTAMPS_PROTOCOL, 'mode': 'gateway',
                                  'format': MESSAGE_TIMESTAMP_FORMAT}}
# The description of the body every turn's system message and every cron run carries.
ENVIRONMENT_NOTE = {mode: {'protocol': ENVIRONMENT_NOTE_PROTOCOL, 'mode': mode, 'text': text,
                           'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
                    for mode, text in ENVIRONMENT_NOTES.items()}
RULE = {'test': 'sign_exact', 'alpha': 0.05, 'min_wins': 6, 'ci': 'cluster_bootstrap_95',
        'unit': 'scenario', 'non_inferior_pp': -10}
PROFILE_NAME = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,39}'
OVERLAY_DENIED = re.compile(r'URL|MODEL|KEY|TOKEN|CONTACT|PASSPHRASE|WEBHOOK|_DIR$|_DB$|_PATH$')
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


def validate_profiles(custom):
    """Merge declared profiles over the built-in ones; reject model, budget or path overrides."""
    if custom is None:
        return deepcopy(PROFILES)
    if not isinstance(custom, dict) or len(custom) > 16:
        raise ValueError('Profiles must be an object of at most 16 named arm profiles')
    result = deepcopy(PROFILES)
    for name, profile in custom.items():
        if not isinstance(name, str) or not re.fullmatch(PROFILE_NAME, name) or name in PROFILES:
            raise ValueError('Profile names are short, plain, and cannot redefine the built-in profiles')
        if (not isinstance(profile, dict) or set(profile) - {'plugin', 'overlay', *PROFILE_SWITCHES}
                or type(profile.get('plugin')) is not bool
                or any(type(profile.get(switch, False)) is not bool for switch in PROFILE_SWITCHES)):
            raise ValueError('A profile declares plugin (true/false), an optional overlay and '
                             'optional true/false switches: ' + ', '.join(PROFILE_SWITCHES))
        overlay = profile.get('overlay', {})
        if not isinstance(overlay, dict) or len(overlay) > 32:
            raise ValueError('A profile overlay is an object of at most 32 flags')
        for key, value in overlay.items():
            if (not isinstance(key, str) or not re.fullmatch(r'PROTAGINE_[A-Z0-9_]+', key)
                    or OVERLAY_DENIED.search(key)):
                raise ValueError('Overlays set PROTAGINE_* flags only; model, endpoint, credential, '
                                 'contact and path settings are not arm differences')
            if not isinstance(value, str) or not 1 <= len(value) <= 240 or not value.isprintable():
                raise ValueError('Overlay values are short, nonempty printable strings')
        result[name] = {'plugin': profile['plugin'], 'overlay': dict(overlay),
                        **{switch: True for switch in PROFILE_SWITCHES if profile.get(switch)}}
    return result


def arm_labels(arms, profiles):
    """Label each selected arm; a repeated profile (A/A) gets a numbered label."""
    if (not isinstance(arms, list) or not 2 <= len(arms) <= 8
            or any(not isinstance(arm, str) or arm not in profiles for arm in arms)):
        raise ValueError('Select 2..8 arms by declared profile name')
    labels = {}
    for arm in arms:
        count = sum(profile['name'] == arm for profile in labels.values()) + 1
        label = arm if count == 1 else f'{arm}.{count}'
        labels[label] = {'name': arm, **deepcopy(profiles[arm])}
    return labels


def _settings(temperature, seeds):
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                                    or not 0 <= temperature <= 2):
        raise ValueError('Temperature is unset (provider default) or a number from 0 to 2')
    if (not isinstance(seeds, list) or len(seeds) > 128 or len(set(seeds)) != len(seeds)
            or any(type(seed) is not int or not 0 <= seed < 2 ** 32 for seed in seeds)):
        raise ValueError('Seeds are distinct 32-bit integers')
    return (None if temperature is None else float(temperature)), list(seeds)


def _task(case):
    value = case.record()
    value = {key: item for key, item in value.items()
             if key not in {'sha256', 'inputs_sha256', 'oracle_sha256'}}
    value['inputs'] = {key: item for key, item in value['inputs'].items() if key not in {'arm', 'profile'}}
    return value


def declared_mode(by_arm, key, modes, what):
    """The one value every episode of every arm declares for ``key`` (a known mode), or None."""
    declared = {case.inputs.get(key) for cases in by_arm.values() for case in cases}
    if len(declared) != 1 or (declared - {None} and next(iter(declared)) not in modes):
        raise ValueError(f'Every episode of every arm declares the same {what}, or none')
    return next(iter(declared))


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
            label='candidate', evidence_mode='actual_inference', arms=None, reference_arm=None,
            profiles=None, temperature=None, seeds=None, dataset_dir=None):
    """Resolve local recipes and pinned image identity without calling a model."""
    from . import paired_body, paired_cases, paired_container
    from .native_memory_batch import preflight_output
    if evidence_mode not in {'actual_inference', 'controlled'}:
        raise ValueError('Invalid evidence mode')
    if dataset_dir is not None:
        if dataset_version is not None:
            raise ValueError('Select an installed dataset version or a generated dataset directory, not both')
        if not isinstance(dataset_dir, (str, Path)) or not Path(dataset_dir).is_dir():
            raise ValueError('A generated dataset directory must exist')
        dataset_dir = str(Path(dataset_dir).resolve())
    if type(repetitions) is not int or not 1 <= repetitions <= 3:
        raise ValueError('Paired repetitions must be an integer from 1 to 3')
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        raise ValueError('Provide a nonempty label of at most 120 characters')
    if case_ids is not None and (not isinstance(case_ids, list) or not case_ids
            or len(set(case_ids)) != len(case_ids)):
        raise ValueError('Select distinct, nonempty case IDs')
    declared_profiles = validate_profiles(profiles)
    labels = arm_labels(list(ARMS) if arms is None else arms, declared_profiles)
    reference = next(iter(labels)) if reference_arm is None else reference_arm
    if reference not in labels:
        raise ValueError('The reference arm must be one of the selected arms')
    temperature, seeds = _settings(temperature, [] if seeds is None else seeds)
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
    legacy_pair = list(labels) == list(ARMS) and all(
        {k: v for k, v in labels[arm].items() if k != 'name'} == BUILT_IN_PAIR[arm] for arm in labels)
    payload = recipe.get('container_payload', {})
    if not legacy_pair and payload.get('arm_profiles') != ARM_PROFILE_PROTOCOL:
        raise ValueError('Arm profiles beyond the built-in pair require an image whose worker applies profiles')
    if any(labels[arm].get('heartbeat') for arm in labels) and payload.get('heartbeat_prompt_sha256') != HEARTBEAT['prompt_sha256']:
        raise ValueError('The heartbeat arm requires an image whose worker carries the same heartbeat prompt')
    if (any(labels[arm].get(switch) for arm in labels for switch in MIND_SWITCHES)
            and payload.get('mind_tick') != MIND_TICK_PROTOCOL):
        raise ValueError('A mind arm requires an image whose worker serves the mind and ticks it')
    dataset_options = ({'dataset_dir': dataset_dir} if dataset_dir is not None
                       else {'dataset_version': dataset_version} if dataset_version is not None else {})
    by_arm = {arm: paired_cases.cases(arm=arm, case_ids=case_ids, profile=labels[arm], **dataset_options)
              for arm in labels}
    episodes = [case for cases in by_arm.values() for case in cases]
    if any(case.inputs.get('workflow') is not None for case in episodes):
        from .paired_workflow_runtime import PROTOCOL as workflow_protocol
        if payload.get('workflow_protocol') != workflow_protocol:
            raise ValueError('Process restarts require an image with process-restart workflow support')
    if dataset_dir is not None and payload.get('body_protocol') != paired_body.PROTOCOL:
        raise ValueError('Generated families require an image whose worker runs the body tick')
    if any(case.inputs.get('history') for case in episodes):
        from .paired_history import PROTOCOL as history_protocol
        if payload.get('history_protocol') != history_protocol:
            raise ValueError('Seeded history requires an image whose worker imports it before the first turn')
    first = reference
    tool_loading = declared_mode(by_arm, 'tool_loading', TOOL_LOADING_MODES, 'tool loading')
    if tool_loading is not None and payload.get('tool_loading') != TOOL_LOADING_PROTOCOL:
        raise ValueError('Eager tool loading requires an image whose worker applies it to every arm')
    message_timestamps = declared_mode(by_arm, 'message_timestamps', MESSAGE_TIMESTAMPS_MODES,
                                       'message timestamps')
    if message_timestamps is not None and payload.get('message_timestamps') != MESSAGE_TIMESTAMPS_PROTOCOL:
        raise ValueError('Message timestamps require an image whose worker stamps every arm')
    environment_note = declared_mode(by_arm, 'environment_note', tuple(ENVIRONMENT_NOTES), 'environment note')
    if environment_note is not None and payload.get('environment_note') != ENVIRONMENT_NOTE_PROTOCOL:
        raise ValueError('An environment note requires an image whose worker carries it to every arm')
    identifiers = [case.id for case in by_arm[first]]
    if not 1 <= len(identifiers) <= 128 or len(set(identifiers)) != len(identifiers):
        raise ValueError('Paired plan requires 1..128 distinct episodes')
    for arm in labels:
        if identifiers != [case.id for case in by_arm[arm]]:
            raise ValueError('Every arm must declare the same episode IDs and order')
        for left, right in zip(by_arm[first], by_arm[arm]):
            if _task(left) != _task(right):
                raise ValueError('Paired tasks, oracles and budgets may differ only in inputs.arm')
        for case in by_arm[arm]:
            if (case.inputs.get('arm') != arm or case.inputs.get('profile') != labels[arm]
                    or case.boundary != 'native_hermes'
                    or case.consumer != 'native_paired' or case.evaluator != 'paired_artifacts'
                    or case.consumer not in paired_container.CONSUMERS
                    or case.evaluator not in paired_cases.EVALUATORS):
                raise ValueError('Invalid paired native case contract')
    tasks = [_task(case) for case in by_arm[first]]
    declarations = [case.inputs.get('dataset') for case in by_arm[first]]
    declared = declarations[0]
    if any(item is not None for item in declarations) and (not isinstance(declared, dict)
            or any(item != declared for item in declarations)
            or any(case.version != declared.get('version') for case in by_arm[first])):
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
                  'container_recipe_sha256': digest(recipe), 'evidence_mode': evidence_mode,
                  'arms': list(labels), 'reference_arm': reference, 'profiles': labels,
                  'temperature': temperature, 'seeds': seeds, 'rule': RULE, 'heartbeat': HEARTBEAT}
    if tool_loading is not None:
        comparison['tool_loading'] = deepcopy(TOOL_LOADING[tool_loading])
    if message_timestamps is not None:
        comparison['message_timestamps'] = deepcopy(MESSAGE_TIMESTAMPS[message_timestamps])
    if environment_note is not None:
        comparison['environment_note'] = deepcopy(ENVIRONMENT_NOTE[environment_note])
    comparison_key = digest(comparison)
    recipe = {**recipe, 'paired_version': VERSION, 'paired_dataset': dataset,
        'paired_policy': policy, 'comparison_key': comparison_key,
        'implementation': implementation, 'paired_temperature': temperature}
    order_labels = list(labels)
    pairs, prepared = [], []
    for pair_index in range(len(identifiers) * repetitions):
        repetition, index = divmod(pair_index, len(identifiers))
        case_id = identifiers[index]
        shift = (index + repetition) % len(order_labels)
        order = order_labels[shift:] + order_labels[:shift]
        pair = {'episode_id': case_id, 'order': order, 'task_sha256': digest(tasks[index]),
                'oracle_sha256': digest(by_arm[first][index].oracle), 'arms': {}}
        if repetitions > 1:
            pair.update(episode_id=f'{case_id}.repeat-{repetition + 1:02d}',
                        workflow_id=case_id, repetition=repetition + 1)
        for arm in labels:
            case = by_arm[arm][index]
            pair['arms'][arm] = {'path': f'runs/{pair_index + 1:03d}-{arm}',
                'case': case.record(), 'recipe_sha256': digest(recipe)}
        pairs.append(pair)
        prepared.extend((pair['arms'][arm], by_arm[arm][index]) for arm in order)
    manifest = {'schema': 1, 'kind': 'paired_qualification', 'orchestrator': VERSION,
        'label': label, 'evidence_mode': evidence_mode, 'recipe': recipe, 'dataset': dataset,
        'comparison': comparison, 'comparison_key': comparison_key,
        'selected_episodes': len(pairs), 'declared_attempts': len(pairs) * len(labels),
        'declared_seconds': sum(case.timeout_seconds for _, case in prepared),
        'order_policy': 'rotate-arm-order-by-(episode-index + repetition) mod arms',
        'state_policy': 'fresh-container-and-state-per-arm-episode; persistence-within-episode',
        'budget_verification': 'declared; accounting/enforcement require execution evidence',
        'options': {'native_binding': native_binding, 'case_ids': identifiers, 'dataset_version': dataset_version,
                    'dataset_dir': dataset_dir,
                    'arms': list(arms) if arms is not None else list(ARMS), 'reference_arm': reference,
                    'profiles': profiles, 'temperature': temperature, 'seeds': seeds},
        'pairs': pairs, 'implementation': implementation,
        'execution_implementation_sha256': _execution_identity(paired_container.CONSUMERS, paired_cases.EVALUATORS),
        'coverage': 'Development pilot episodes; no held-out, deployment or model-tier qualification.'}
    if repetitions > 1:
        manifest['options']['repetitions'] = repetitions
    if dataset_version == paired_cases.WORKFLOW_VERSION:
        manifest['coverage'] = ('Frozen public workflow evaluation, not a private holdout. '
            'Repeated trials share workflow designs; no independent-sample or production qualification claim.')
    elif dataset_dir is not None:
        manifest['coverage'] = (f"Generated family instances ({dataset['split']} split, seeded templates); "
            'a held-out split counts only when its templates live outside the repository.')
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
    parser = commands.add_parser('paired', help='Compare fresh Hermes arms on identical episodes')
    sub = parser.add_subparsers(dest='paired_command', required=True)
    for command in ('plan', 'run', 'report', 'export'):
        item = sub.add_parser(command)
        item.add_argument('--output', type=Path, required=True, help='Private immutable paired-run directory')
        if command in {'plan', 'run'}:
            item.add_argument('--native-config', type=Path, required=True, help='Private common Hermes provider configuration')
            item.add_argument('--comparison-policy', type=Path, required=True, help='Versioned budget and environment declarations')
            item.add_argument('--container-image', required=True, help='Same digest-pinned image for every arm')
            item.add_argument('--docker-host', help='Declared Docker daemon; never mounted inside the benchmark')
        if command == 'plan':
            item.add_argument('--native-binding', required=True)
            item.add_argument('--dataset-version', help='Explicit installed fixture version; frozen into the plan')
            item.add_argument('--dataset-dir', type=Path,
                              help='Generated family directory (manifest.json + scenarios.json); its content hash is frozen into the plan')
            item.add_argument('--repetitions', type=int, choices=(1, 2, 3), default=1,
                              help='Complete paired repetitions, frozen before execution; use 3 for workflow release evaluation')
            item.add_argument('--case-ids', help='Comma-separated installed episode IDs; selects every arm together')
            item.add_argument('--arms', default=','.join(ARMS),
                              help='Comma-separated arm profile names; repeat a name for an A/A run')
            item.add_argument('--reference-arm', help='Comparator arm label; defaults to the first arm')
            item.add_argument('--profiles', type=Path, help='JSON object of additional arm profiles {name: {plugin, overlay}}')
            item.add_argument('--temperature', type=float, help='Pinned sampling temperature for every model call; unset keeps the provider default')
            item.add_argument('--seeds', help='Comma-separated scenario seeds frozen into the plan')
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
            dataset_version=args.dataset_version, dataset_dir=args.dataset_dir,
            repetitions=args.repetitions,
            case_ids=[value.strip() for value in args.case_ids.split(',')] if args.case_ids else None,
            arms=[value.strip() for value in args.arms.split(',')],
            reference_arm=args.reference_arm,
            profiles=read(args.profiles) if args.profiles else None,
            temperature=args.temperature,
            seeds=[int(value) for value in args.seeds.split(',')] if args.seeds else None,
            label=args.label, evidence_mode=args.evidence_mode, **resources)
        print(json.dumps(result, indent=2))
        return 0
    result = asyncio.run(run(args.output, resume=args.resume, **resources))
    print(markdown(result))
    # A completed comparison may legitimately find either model inadequate.
    # Keep task quality in the report; reserve a nonzero status for a cohort
    # without attributable results, so campaigns do not stop on model failures.
    return 0 if result['paired_score'] is not None else 1
