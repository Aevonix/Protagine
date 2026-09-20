"""Finite coding qualification using actual native edits and executable checks."""
import argparse
import asyncio
from copy import deepcopy
import json
import hashlib
from pathlib import Path

from .coding_sandbox import validate_files, validate_sandbox
from .native import _environment, configuration, native_cli, native_context
from .records import CaseSpec, digest, read, write_once
from .report import summarize
from .runner import evaluate

VERSION = 'agent-coding-effects-v1'


def load_pack(path=None):
    pack = read(path or Path(__file__).with_name('fixtures')/'coding-development.json')
    if pack.get('version') != VERSION or pack.get('split') not in {'development', 'held_out'}:
        raise ValueError('Unsupported coding fixture pack')
    rows = pack.get('tasks')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 6:
        raise ValueError('A coding shard must contain one to six tasks')
    seen = set()
    for task in rows:
        if task['id'] in seen:
            raise ValueError('Repeated coding task')
        seen.add(task['id'])
        validate_files(task['files'])
        if (not isinstance(task['checks'], list) or not 2 <= len(task['checks']) <= 12
                or not set(task['editable']) <= set(task['files'])):
            raise ValueError('Invalid coding oracle or editable files')
        check_ids = set()
        for check in task['checks']:
            if (check['id'] in check_ids or ('expected' in check) == ('exception' in check)
                    or set(check) - {'id', 'module', 'function', 'args', 'kwargs', 'expected', 'exception'}):
                raise ValueError('Invalid executable check')
            check_ids.add(check['id'])
    return pack


def cases(pack, sandbox, *, deadline_seconds=480, max_output_tokens=8192, max_iterations=18):
    validate_sandbox(sandbox)
    if not 1 <= max_iterations <= 40 or not 256 <= max_output_tokens <= 32768:
        raise ValueError('Coding work exceeds declared bounds')
    result = []
    for task in pack['tasks']:
        result.append(CaseSpec(id=task['id'], version=VERSION, role='coding', boundary='native_hermes',
            consumer='coding_native', evaluator='coding_effects', timeout_seconds=deadline_seconds,
            max_output_bytes=1048576, provenance='private' if pack['split'] == 'held_out' else 'public',
            inputs={'role': 'coding', 'cleanup_seconds': 30, 'max_output_tokens': max_output_tokens,
                'max_iterations': max_iterations, 'sandbox': deepcopy(sandbox),
                'repository': deepcopy(task['files']), 'editable': list(task['editable']),
                'messages': [{'role': 'system', 'content': (
                    'Work in the synthetic repository at /workspace. Read AGENTS.md and repair the implementation. '
                    'Use the actual file and terminal tools. The sandbox has Python and no network. '
                    'Do not change protected files or test expectations. Run the available tests. '
                    'Finish with a brief, accurate account of the change and tests you actually ran.')},
                    {'role': 'user', 'content': task['instruction']}]},
            oracle={'checks': deepcopy(task['checks']), 'editable': list(task['editable']),
                    'original_files': deepcopy(task['files'])}))
    return result


async def _verify(context, sandbox, files, checks):
    state = context.state_dir/'verification'
    state.mkdir(mode=0o700)
    context.state_cleanup_safe = False
    payload = json.dumps({'sandbox': sandbox, 'files': files, 'checks': checks}).encode()
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(context.router.hermes_python, '-I', '-B',
        str(Path(__file__).with_name('coding_verify_worker.py')), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=state, env=_environment(state, {'providers': {}})))
    proc = None
    io = None
    try:
        proc = await asyncio.shield(spawning)
        io = asyncio.create_task(proc.communicate(payload))
        stdout, _stderr = await asyncio.wait_for(asyncio.shield(io), 100)
        result = json.loads(stdout)
        context.state_cleanup_safe = result.get('cleanup_verified') is True
        if proc.returncode != 0 or result.get('error'):
            raise RuntimeError('Offline coding verification did not finish cleanly')
        if result.get('fresh_verification_container') is not True or not context.state_cleanup_safe:
            raise RuntimeError('Offline verification cleanup is unconfirmed')
        return result
    except (asyncio.CancelledError, TimeoutError):
        # A cancellation racing process creation must still acquire and stop
        # that owned process. Never let a verifier continue in a detached thread.
        if proc is None:
            proc = await asyncio.shield(spawning)
        if io is None:
            io = asyncio.create_task(proc.communicate(payload))
        if proc.returncode is None:
            proc.terminate()
        try:
            stdout, _stderr = await asyncio.wait_for(asyncio.shield(io), 50)
            context.state_cleanup_safe = json.loads(stdout).get('cleanup_verified') is True
        except (ValueError, TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
        raise
    finally:
        if proc is None and spawning.done() and not spawning.cancelled() and spawning.exception() is not None:
            context.state_cleanup_safe = True  # Creation failed before any child existed.
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        if io is not None and not io.done():
            io.cancel()


async def coding_native(inputs, context):
    try:
        result = await native_cli(inputs, context, worker=Path(__file__).with_name('coding_worker.py'))
    finally:
        try:
            receipt = read(context.state_dir/'coding-cleanup.json')
        except (ValueError, OSError):
            receipt = {}
        context.state_cleanup_safe = context.state_cleanup_safe and receipt.get('cleanup_verified') is True
    if not context.state_cleanup_safe:
        raise RuntimeError('Native coding container cleanup is unconfirmed')
    files = result['effects'].pop('coding_files', None)
    validate_files(files)
    # Candidate sources stay with the private immutable attempt, outside disposable state.
    write_once(context.state_dir.parent/'coding-artifact.json', {'files': files, 'sha256': digest(files)})
    original = inputs['repository']
    changed = sorted(name for name in set(original) | set(files) if original.get(name) != files.get(name))
    verified = await _verify(context, inputs['sandbox'], files, context.router.coding_oracle['checks'])
    result['effects'].update(changed_files=changed, snapshot_sha256=digest(files), **verified)
    return result


def coding_effects(observed, oracle):
    effects = observed.get('effects', {})
    changed = effects.get('changed_files', [])
    checks = effects.get('checks', {})
    expected = {check['id'] for check in oracle['checks']}
    return {'native_edit_observed': bool(changed) and bool(set(effects.get('native_tool_calls', [])) & {'terminal', 'write_file', 'patch'}),
        'edit_scope_respected': bool(changed) and set(changed) <= set(oracle['editable']),
        'offline_container_verified': effects.get('offline_container_verified') is True,
        'fresh_verification_container': effects.get('fresh_verification_container') is True,
        'verification_cleanup_confirmed': effects.get('cleanup_verified') is True,
        'all_hidden_checks_passed': set(checks) == expected and all(value is True for value in checks.values())}


CONSUMERS = {'coding_native': coding_native}
EVALUATORS = {'coding_effects': coding_effects}


def recipe_metadata(pack, sandbox):
    return {'consumer': 'native_coding_effects', 'coding_pack_sha256': digest(pack),
        'sandbox': deepcopy(sandbox),
        'coding_implementation': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('coding.py', 'coding_worker.py', 'coding_verify_worker.py', 'coding_sandbox.py')},
        'basis': 'Real native tool edits in offline synthetic repositories; executable checks in separate containers'}


async def run(directory, *, config_path, binding, hermes_python, sandbox, pack_path=None,
              deadline_seconds=480, max_output_tokens=8192, max_iterations=18, resume=False):
    pack = load_pack(pack_path)
    selected, recipe = configuration(config_path, binding, hermes_python=hermes_python)
    suite = cases(pack, sandbox, deadline_seconds=deadline_seconds,
                  max_output_tokens=max_output_tokens, max_iterations=max_iterations)
    recipe.update(recipe_metadata(pack, sandbox))
    def router(case):
        native = native_context(selected, recipe)
        native.coding_oracle = case.oracle
        return native
    await evaluate(directory, recipe, suite, CONSUMERS, EVALUATORS,
                   router, resume=resume, evidence_mode='actual_inference', suite_version=VERSION)
    report = summarize(directory)
    count = len(list(Path(directory).glob('report-*.json'))) + 1
    write_once(Path(directory)/f'report-{count:03d}.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true', help='Inspect fixture IDs without model calls')
    parser.add_argument('--pack', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--binding')
    parser.add_argument('--hermes-python', type=Path)
    parser.add_argument('--image', help='Already installed, digest-pinned Docker image')
    parser.add_argument('--docker-host', help='Owned local socket or loopback forward; never public metadata')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if args.list:
        pack = load_pack(args.pack)
        print(json.dumps({'version': VERSION, 'split': pack['split'], 'cases': [task['id'] for task in pack['tasks']]}))
        return 0
    if not all((args.config, args.binding, args.hermes_python, args.image, args.output)):
        parser.error('Execution requires config, binding, hermes-python, image and output')
    report = asyncio.run(run(args.output, config_path=args.config, binding=args.binding,
        hermes_python=str(args.hermes_python), sandbox={'image': args.image, 'docker_host': args.docker_host},
        pack_path=args.pack, resume=args.resume))
    print(json.dumps({'run_id': report['run_id'], 'cases': [(row['case_id'], row['outcome']) for row in report['cases']]}))
    return 0 if all(row['outcome'] == 'pass' for row in report['cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
