"""Versioned paired native-agent scenarios, judged by files rather than reply wording.

The original 18-case pilot and reviewed 60-case set are public development data,
not a comprehensive agent benchmark. Both arms receive identical tasks and oracles.
Memory facts arrive in ordinary turns. Session changes are sequential; disclosure
tasks test requested artifact scope, not authenticated access control. The Python
repair case verifies syntax/API shape only, not executable behavior.
"""
import ast
import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

from .records import CaseSpec

VERSION = 'paired-agent-pilot-1'
REVIEWED_VERSION = 'paired-agent-reviewed-1'
BASELINE_VERSION = 'paired-agent-reviewed-2'
WORKFLOW_VERSION = 'paired-agent-workflows-1'
DATASET_VERSIONS = (VERSION, REVIEWED_VERSION, BASELINE_VERSION, WORKFLOW_VERSION)
WORKFLOW_FAMILIES = ('workflow-recall', 'workflow-correction', 'workflow-recovery', 'workflow-scope')
FAMILIES = (
    'grounded-evidence', 'planning-toolrecovery', 'persistent-memory',
    'crosssession-authority', 'coding-ops', 'extraction-review',
)
LIMITATIONS = (
    'Public development scenarios; neither dataset establishes comprehensive agent qualification.',
    'Sequential sessions only; no concurrency, scheduling fairness or race coverage.',
    'Disclosure checks inspect requested artifacts, not authenticated authorization or every output channel.',
    'Forgetting checks later output behavior, not deletion from every underlying store.',
    'Python repair checks syntax and function signature; candidate code is never executed by this verifier.',
    'Native memory and session search availability are diagnostics, not automatic success conditions.',
)


_FIXTURE_DIRECTORY = Path(__file__).parent / 'fixtures' / VERSION


def _leaf_name(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value) is not None


def load_dataset(directory=_FIXTURE_DIRECTORY):
    """Verify versioned fixture bytes before constructing any executable case."""
    directory = Path(directory)
    manifest_raw = (directory / 'manifest.json').read_bytes()
    scenario_raw = (directory / 'scenarios.json').read_bytes()
    manifest = json.loads(manifest_raw)
    expected = manifest['files']['scenarios.json']
    if (len(scenario_raw) != expected['bytes']
            or hashlib.sha256(scenario_raw).hexdigest() != expected['sha256']):
        raise ValueError('Paired dataset checksum mismatch')
    if (manifest['dataset_id'] not in DATASET_VERSIONS
            or manifest['version'] != manifest['dataset_id']):
        raise ValueError('Unsupported paired dataset version')
    scenarios = json.loads(scenario_raw)
    if not isinstance(scenarios, list) or len(scenarios) != manifest['scenario_count']:
        raise ValueError('Paired dataset scenario count mismatch')
    identities = set()
    workflow_dataset = manifest['dataset_id'] == WORKFLOW_VERSION
    counts = dict.fromkeys(WORKFLOW_FAMILIES if workflow_dataset else FAMILIES, 0)
    for item in scenarios:
        identity, family = item['id'], item['family']
        if (family not in counts or identity in identities
                or identity != 'paired.' + family + '.' + item['scenario']):
            raise ValueError('Invalid paired scenario identity')
        identities.add(identity)
        counts[family] += 1
        files, turns, oracle = item['initial_files'], item['episodes'], item['oracle']
        if not isinstance(files, dict) or any(not _leaf_name(k) or not isinstance(v, str) for k, v in files.items()):
            raise ValueError('Initial files require leaf names and text')
        if (not isinstance(turns, list) or not 1 <= len(turns) <= (24 if workflow_dataset else 4)
                or len(turns) != oracle['declared_turns']
                or any(set(turn) != {'session_id', 'user'} or not _leaf_name(turn['session_id'])
                       or not isinstance(turn['user'], str) or not turn['user'].strip() for turn in turns)):
            raise ValueError('Invalid paired native episodes')
        artifacts = oracle['artifacts']
        if not artifacts or len({a['path'] for a in artifacts}) != len(artifacts):
            raise ValueError('Paired scenarios require distinct artifact outcomes')
        if any(not _leaf_name(a['path']) for a in artifacts):
            raise ValueError('Artifact declarations require leaf names')
        if workflow_dataset:
            from .paired_workflow_runtime import validate_workflow
            validate_workflow(item['workflow'], turns)
            if (len(turns) < 6 or not item['workflow']['restart_before']
                    or item.get('memory_condition') not in {'relevant', 'irrelevant'}):
                raise ValueError('Workflows require multi-turn history, restart and memory condition')
            checkpoints = oracle.get('checkpoints', [])
            if (not checkpoints or len({c['turn_index'] for c in checkpoints}) != len(checkpoints)
                    or {c['turn_index'] for c in checkpoints} != set(item['workflow']['snapshot_after'])
                    or any(not c['artifacts'] or any(not _leaf_name(a['path']) for a in c['artifacts'])
                           for c in checkpoints)):
                raise ValueError('Workflow checkpoints must match declared snapshots')
    if counts != manifest['families']:
        raise ValueError('Paired dataset family count mismatch')
    if workflow_dataset and (counts != dict.fromkeys(WORKFLOW_FAMILIES, 3)
            or any(sum(s['memory_condition'] == 'irrelevant' for s in scenarios if s['family'] == family) != 1
                   for family in WORKFLOW_FAMILIES)):
        raise ValueError('Frozen workflows require three per family with one irrelevant-memory control')
    content_hash = hashlib.sha256(b'manifest\0' + manifest_raw + b'\0scenarios\0' + scenario_raw).hexdigest()
    return manifest, tuple(scenarios), content_hash


DATASET_MANIFEST, _SCENARIOS, DATASET_SHA256 = load_dataset()

CASE_IDS = tuple(item['id'] for item in _SCENARIOS)

# Seeded template families (benchmarks/paired/generators) are written outside the
# frozen fixtures. Their scenarios may hold body events and body oracles.
GENERATOR_PROTOCOL = 'paired-generator-1'
# An anchor split is public external data rendered into the fixture shape (the
# LongMemEval_S anchor); it is reported descriptively, never as a held-out gate.
GENERATED_SPLITS = ('dev', 'heldout', 'anchor')
# Generated families run with every enabled Hermes tool loaded eagerly in every
# arm (paired_worker.EAGER_TOOLS_CONFIG); the frozen datasets keep stock loading.
GENERATED_TOOL_LOADING = 'eager'
# Every model-facing turn of a generated family carries the body clock in the stock
# gateway message timestamp format (paired_worker.MESSAGE_TIMESTAMP_FORMAT); the
# frozen datasets keep bare turns.
GENERATED_MESSAGE_TIMESTAMPS = 'gateway'
# Every turn and cron run of a generated family carries the same description of
# the body (paired_worker.ENVIRONMENT_NOTES); the frozen datasets carry none.
GENERATED_ENVIRONMENT_NOTE = 'messaging'
# The families whose scenarios grade messages to contacts give every arm the same outbound
# path (paired_worker.OUTBOUND_SCHEMA, families/mind-people-1.md 7.1); in every other family
# no arm has a send tool, as their plans say.
GENERATED_OUTBOUND = {'mind-people-1': 'send_message'}
GENERATED_SCENARIO_KEYS = frozenset({'id', 'family', 'scenario', 'seed', 'role', 'initial_files',
                                     'episodes', 'limitations', 'oracle'})
# A generated scenario may also declare a process-restart contract (``workflow``, the
# frozen workflows' shape) with checkpoint artifacts graded on its snapshots, and seeded
# conversation history (``history``, paired_history) that the worker imports before the
# first turn.
GENERATED_OPTIONAL_KEYS = frozenset({'workflow', 'history'})
GENERATED_ORACLE_KEYS = frozenset({'declared_turns', 'artifacts', 'body', 'checkpoints', 'self_report'})


def _validate_checkpoints(checkpoints, workflow):
    if (not isinstance(checkpoints, list) or not checkpoints
            or len({c.get('turn_index') for c in checkpoints if isinstance(c, dict)}) != len(checkpoints)
            or any(not isinstance(c, dict) or set(c) != {'turn_index', 'artifacts'}
                   or c['turn_index'] not in workflow['snapshot_after']
                   or not isinstance(c['artifacts'], list) or not c['artifacts']
                   or any(not isinstance(a, dict) or not _leaf_name(a.get('path')) for a in c['artifacts'])
                   or len({a['path'] for a in c['artifacts']}) != len(c['artifacts'])
                   for c in checkpoints)):
        raise ValueError('Generated checkpoints must grade declared snapshots')


def load_generated_dataset(directory):
    """Verify a generated family's bytes and shape before constructing any executable case."""
    directory = Path(directory)
    manifest_raw = (directory / 'manifest.json').read_bytes()
    scenario_raw = (directory / 'scenarios.json').read_bytes()
    manifest = json.loads(manifest_raw)
    expected = manifest['files']['scenarios.json']
    if (len(scenario_raw) != expected['bytes']
            or hashlib.sha256(scenario_raw).hexdigest() != expected['sha256']):
        raise ValueError('Generated dataset checksum mismatch')
    generator = manifest.get('generator')
    if (not isinstance(generator, dict) or generator.get('protocol') != GENERATOR_PROTOCOL
            or generator.get('split') not in GENERATED_SPLITS or type(generator.get('seed')) is not int
            or not _leaf_name(manifest.get('dataset_id')) or manifest.get('version') != manifest['dataset_id']
            or manifest['dataset_id'] in DATASET_VERSIONS):
        raise ValueError('Unsupported generated dataset manifest')
    scenarios = json.loads(scenario_raw)
    if (not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 128
            or len(scenarios) != manifest['scenario_count']):
        raise ValueError('Generated dataset scenario count mismatch')
    from .paired_body_grading import validate_body_oracle, validate_self_report_oracle
    from .paired_history import validate_history
    from .paired_workflow_runtime import validate_episodes, validate_workflow
    identities, counts = set(), {}
    for item in scenarios:
        if (not isinstance(item, dict) or set(item) - GENERATED_OPTIONAL_KEYS != GENERATED_SCENARIO_KEYS
                or not _leaf_name(item['id']) or item['id'] in identities
                or not _leaf_name(item['family']) or not _leaf_name(item['scenario'])
                or type(item['seed']) is not int or not isinstance(item['role'], str) or not item['role']
                or not isinstance(item['limitations'], list)):
            raise ValueError('Invalid generated scenario')
        identities.add(item['id'])
        counts[item['family']] = counts.get(item['family'], 0) + 1
        files, oracle = item['initial_files'], item['oracle']
        if not isinstance(files, dict) or any(not _leaf_name(k) or not isinstance(v, str) for k, v in files.items()):
            raise ValueError('Initial files require leaf names and text')
        kinds = validate_episodes(item['episodes'])
        workflow = validate_workflow(item['workflow'], item['episodes']) if 'workflow' in item else None
        if 'history' in item:
            validate_history(item['history'])
            sessions = {entry['session_id'] for entry in item['episodes'] if 'session_id' in entry}
            if any(session['id'] in sessions for session in item['history']):
                raise ValueError('History session ids must differ from the episode session ids')
        artifacts = oracle.get('artifacts') if isinstance(oracle, dict) else None
        if (not isinstance(oracle, dict) or set(oracle) - GENERATED_ORACLE_KEYS
                or oracle.get('declared_turns') != len(kinds) or not isinstance(artifacts, list)
                or any(not isinstance(a, dict) or not _leaf_name(a.get('path')) for a in artifacts)
                or len({a['path'] for a in artifacts}) != len(artifacts)
                or not (artifacts or 'body' in oracle or 'self_report' in oracle)
                or ('checkpoints' in oracle and workflow is None)):
            raise ValueError('Generated scenarios need artifact, body or self-report outcomes')
        if 'body' in oracle:
            validate_body_oracle(oracle['body'])
        if 'self_report' in oracle:
            validate_self_report_oracle(oracle['self_report'])
        if 'checkpoints' in oracle:
            _validate_checkpoints(oracle['checkpoints'], workflow)
    if counts != manifest['families']:
        raise ValueError('Generated dataset family count mismatch')
    content_hash = hashlib.sha256(b'manifest\0' + manifest_raw + b'\0scenarios\0' + scenario_raw).hexdigest()
    return manifest, tuple(scenarios), content_hash


def cases(arm, case_ids=None, *, dataset_version=VERSION, profile=None, dataset_dir=None):
    """Return fresh independent declarations; only inputs.arm and inputs.profile differ by arm."""
    if profile is None and arm not in {'base_hermes', 'protagine'}:
        raise ValueError('Use base_hermes or protagine, or declare an arm profile')
    if profile is not None and (not isinstance(profile, dict) or type(profile.get('plugin')) is not bool
                                or not isinstance(profile.get('overlay'), dict)
                                or not isinstance(arm, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,41}', arm)):
        raise ValueError('An arm label needs a profile with plugin and overlay')
    split = None
    if dataset_dir is not None:
        manifest, scenarios, content_hash = load_generated_dataset(dataset_dir)
        dataset_version, split = manifest['dataset_id'], manifest['generator']['split']
    elif dataset_version not in DATASET_VERSIONS:
        raise ValueError('Select an installed paired dataset version')
    elif dataset_version == VERSION:
        scenarios, content_hash = _SCENARIOS, DATASET_SHA256
    else:
        _, scenarios, content_hash = load_dataset(_FIXTURE_DIRECTORY.parent / dataset_version)
    identities = tuple(item['id'] for item in scenarios)
    selected = list(identities) if case_ids is None else list(case_ids)
    if not selected or len(selected) != len(set(selected)) or set(selected) - set(identities):
        raise ValueError('Select distinct installed paired case IDs')
    result = []
    for scenario in scenarios:
        if scenario['id'] not in selected:
            continue
        inputs = {key: copy.deepcopy(scenario[key]) for key in (
            'family', 'scenario', 'role', 'initial_files', 'episodes', 'limitations')}
        inputs.update(arm=arm, max_output_tokens=4096, max_iterations=8,
            cleanup_seconds=5, settle_seconds=5, contact_id='fixture-owner',
            dataset={'id': dataset_version, 'version': dataset_version, 'sha256': content_hash})
        if profile is not None:
            inputs['profile'] = copy.deepcopy(profile)
        oracle = copy.deepcopy(scenario['oracle'])
        if dataset_version == WORKFLOW_VERSION:
            inputs.update(workflow=copy.deepcopy(scenario['workflow']),
                          memory_condition=scenario['memory_condition'])
            oracle['workflow_contract'] = copy.deepcopy(scenario['workflow'])
            inputs['dataset']['split'] = 'frozen_public_evaluation'
        if split is not None:
            inputs['dataset']['split'] = split
            inputs['tool_loading'] = GENERATED_TOOL_LOADING
            inputs['message_timestamps'] = GENERATED_MESSAGE_TIMESTAMPS
            inputs['environment_note'] = GENERATED_ENVIRONMENT_NOTE
            if dataset_version in GENERATED_OUTBOUND:
                inputs['outbound'] = GENERATED_OUTBOUND[dataset_version]
            if 'workflow' in scenario:
                # The normalized contract: the supervisor restarts the worker process before
                # the probe and the workflow grader checks the lifecycle and the checkpoints.
                from .paired_workflow_runtime import validate_workflow
                contract = validate_workflow(scenario['workflow'], scenario['episodes'])
                inputs['workflow'] = copy.deepcopy(contract)
                oracle['workflow_contract'] = copy.deepcopy(contract)
            if 'history' in scenario:
                inputs['history'] = copy.deepcopy(scenario['history'])
        # Tick episodes wait for cron runs and in-process workers; give them the workflow deadline.
        generous = dataset_version == WORKFLOW_VERSION or split is not None
        result.append(CaseSpec(id=scenario['id'], version=dataset_version, role=scenario['role'],
            boundary='native_hermes', consumer='native_paired', evaluator='paired_artifacts',
            inputs=inputs, oracle=oracle,
            timeout_seconds=600 if generous else 120 * len(scenario['episodes']) + 30,
            max_output_bytes=1048576 if generous else 262144))
    return result


def _same(a, b):
    # JSON numbers with equal values are equivalent; booleans are not counts.
    if _number(a) and _number(b):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _schedule(actual, expected):
    if not isinstance(actual, list) or len(actual) != len(expected['tasks']):
        return False
    by_id = {}
    for item in actual:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            return False
        identity, start, finish = item['id'], item.get('start'), item.get('finish')
        if identity not in expected['tasks'] or identity in by_id or not _number(start) or not _number(finish):
            return False
        if start < 0 or finish > expected['max_finish'] or not math.isclose(
                finish - start, expected['tasks'][identity]['duration'], abs_tol=1e-9):
            return False
        by_id[identity] = item
    for identity, spec in expected['tasks'].items():
        if any(by_id[parent]['finish'] > by_id[identity]['start'] for parent in spec['after']):
            return False
        for other, other_spec in expected['tasks'].items():
            if identity != other and spec['resource'] == other_spec['resource']:
                left, right = by_id[identity], by_id[other]
                if max(left['start'], right['start']) < min(left['finish'], right['finish']):
                    return False
    return True


def _assertion(actual, rule):
    try:
        for key in rule['path']:
            actual = actual[key]
    except (KeyError, IndexError, TypeError):
        return False
    expected, op = rule['value'], rule['op']
    if op == 'equals':
        return _same(actual, expected)
    if op == 'number':
        return _number(actual) and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)
    if op == 'set_equals':
        return (isinstance(actual, list) and len(actual) == len(expected)
            and all(sum(_same(value, item) for value in actual) == 1 for item in expected))
    if op == 'label_one_of':
        return isinstance(actual, str) and actual.strip().casefold() in {x.casefold() for x in expected}
    if op == 'labels_set_equals':
        if not isinstance(actual, list) or any(not isinstance(x, str) for x in actual):
            return False
        aliases = rule.get('aliases', {})
        normalized = [aliases.get(x.strip().casefold(), x.strip().casefold()) for x in actual]
        return len(normalized) == len(expected) and set(normalized) == set(expected)
    if op == 'keys_equal':
        return isinstance(actual, dict) and set(actual) == set(expected)
    if op == 'schedule':
        return _schedule(actual, expected)
    raise ValueError('Unknown paired artifact assertion')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError('Nonfinite JSON number')


def _decoded_strings(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _artifact_checks(raw, spec):
    try:
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 65536:
            return False
        if any(value.casefold() in raw.casefold() for value in spec.get('forbidden', [])):
            return False
        if spec['format'] == 'text':
            return raw == spec['exact']
        if spec['format'] == 'python':
            tree = ast.parse(raw)
            functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == spec['function']]
            if len(functions) != 1:
                return False
            args = functions[0].args
            return ([a.arg for a in args.args] == spec['arguments'] and not args.posonlyargs
                and not args.kwonlyargs and args.vararg is None and args.kwarg is None
                and not args.defaults)
        if spec['format'] != 'json':
            raise ValueError('Unknown paired artifact format')
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
        if spec.get('forbidden_in_decoded_json') and any(
                marker.casefold() in item.casefold()
                for item in _decoded_strings(value) for marker in spec.get('forbidden', [])):
            return False
        return all(_assertion(value, item) for item in spec['assertions'])
    except (ValueError, TypeError, SyntaxError, RecursionError, OverflowError):
        return False


def assess(observed, oracle):
    """No prose judge, tool-name reward, or advantage for merely loading treatment."""
    effects = observed.get('effects', {})
    if not isinstance(effects, dict):
        effects = {}
    completed, declared = effects.get('turns_completed'), effects.get('declared_turns')
    checks = {'all_native_turns_completed': type(completed) is int and type(declared) is int
        and completed == declared == oracle['declared_turns']}
    artifacts = effects.get('artifacts', {})
    if not isinstance(artifacts, dict):
        artifacts = {}
    for spec in oracle['artifacts']:
        path = spec['path']
        if PurePosixPath(path).name != path or path in {'', '.', '..'}:
            raise ValueError('Artifact declarations must use leaf names')
        checks['artifact:' + path] = _artifact_checks(artifacts.get(path), spec)
    if 'workflow_contract' in oracle:
        from .paired_workflow_grading import assess_workflow
        checks.update(assess_workflow(effects, oracle))
    if 'body' in oracle:
        from .paired_body_grading import assess_body
        checks.update(assess_body(effects, oracle['body']))
    if 'self_report' in oracle:
        from .paired_body_grading import assess_self_report
        checks.update(assess_self_report(effects, oracle['self_report']))
    return checks


EVALUATORS = {'paired_artifacts': assess}
