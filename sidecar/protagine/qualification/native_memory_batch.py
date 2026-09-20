"""Explicit plan/run command for the real native memory development pack."""
import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from .benchmark import _shards, inspect as inspect_batch
from .native import configuration, native_context, _environment
from .native_memory import MemoryRouter, CONSUMERS, EVALUATORS, implementation_identity
from .native_memory_cases import CASES, VERSION
from .records import digest, publish, read, write_once
from .report import markdown, summarize
from .runner import evaluate


def preflight_output(output):
    """Check existing ancestors before spending inference on source formation."""
    for path in (Path(output).absolute(), *Path(output).absolute().parents):
        try:
            value = path.lstat()
        except FileNotFoundError:
            continue
        sticky_root = value.st_uid == 0 and bool(value.st_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(value.st_mode) or value.st_uid not in {0, os.geteuid()}
                or (stat.S_IMODE(value.st_mode) & 0o022 and not sticky_root)):
            raise ValueError('Native memory output requires owner/root directories without '
                             'group/other write access; incompatible ancestor: ' + str(path))


def prepare(args):
    from protagine.router import LLMRouter
    preflight_output(args.output)
    native_config, recipe = configuration(args.native_config, args.native_binding,
                                          hermes_python=args.hermes_python)
    supporting = read(args.support_config)
    router = LLMRouter(tiers={})
    router.configure(supporting)
    # configure returns the snapshot object in some supported versions; use the
    # ordinary public status projection for a stable credential-free record.
    snapshot = router.routing_status()
    selected = set(args.case_ids.split(',')) if args.case_ids else {case.id for case in CASES}
    if not selected or selected - {case.id for case in CASES}:
        raise ValueError('Select installed native memory case IDs')
    if not 0 < args.native_seconds < args.case_seconds <= 600:
        raise ValueError('Native allowance must fit inside the declared case deadline (maximum 600 seconds)')
    cases = [replace(deepcopy(case), timeout_seconds=args.case_seconds,
        inputs={**deepcopy(case.inputs), 'native_seconds': args.native_seconds})
        for case in CASES if case.id in selected]
    identity = {**implementation_identity(), 'native_memory_batch.py':
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    from .native_memory_identity import inspect_runtime
    inspector = Path(__file__).with_name('native_memory_identity.py')
    with tempfile.TemporaryDirectory(prefix='protagine-memory-inspect-') as home:
        probe = subprocess.run([str(args.hermes_python), '-I', '-B', str(inspector)],
            cwd=home, env=_environment(Path(home), {'providers': {}}),
            text=True, capture_output=True, timeout=10, check=True)
    memory_runtime = {'native': json.loads(probe.stdout), 'controller': inspect_runtime(('protagine',))}
    recipe = {**recipe, 'supporting_router_snapshot': snapshot,
        'supporting_config_sha256': digest(supporting), 'native_memory_implementation': identity,
        'memory_runtime': memory_runtime,
        'basis': 'Native reader with fixed supporting extraction/review; isolated real lexical memory pipeline.'}
    shards, groups = [], []
    for index, (_, members) in enumerate(_shards(cases), 1):
        shard = {'id': f'native-memory-{index:03d}', 'suite': 'native',
            'path': f'runs/native-memory-{index:03d}', 'recipe': recipe,
            'case_ids': [case.id for case in members], 'declared_cases': len(members),
            'declared_seconds': sum(case.timeout_seconds for case in members),
            'case_hashes': {case.id: case.record()['sha256'] for case in members}}
        shards.append(shard)
        groups.append((shard, members))
    manifest = {'schema': 1, 'kind': 'qualification_batch', 'suite_version': VERSION,
        'stage': 'development', 'label': args.label, 'evidence_mode': args.evidence_mode,
        'available_cases': len(CASES), 'selected_cases': len(cases), 'shards': shards,
        'cases': [{'case_id': case.id, 'role': case.role, 'boundary': case.boundary,
            'version': case.version, 'case_sha256': case.record()['sha256'], 'inputs_sha256': case.record()['inputs_sha256'],
            'oracle_sha256': case.record()['oracle_sha256'],
            'coverage': 'Native reader, isolated real lexical memory; fixed supporting extractor/reviewer.'} for case in cases],
        'coverage': 'Real formation/review, ledger, native prefetch and final answers; lexical retrieval only.',
        'unimplemented': ['embedding_and_reranking', 'live_channel_transport',
                          'forgetting_unconfigured_external_stores', 'same_session_history_erasure',
                          'native_chat_memory_creation', 'natural_language_forget_dispatch'],
        'implementation': identity}
    manifest['sha256'] = digest(manifest)
    return manifest, groups, supporting, native_config


async def execute(args, manifest, groups, supporting, native_config):
    from protagine.router import LLMRouter
    (args.output / 'runs').mkdir(mode=0o700, exist_ok=True)
    with (args.output / '.benchmark.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for shard, cases in groups:
            target = args.output / shard['path']
            existing = (target / 'run.json').exists()
            if existing and not args.resume:
                raise ValueError('Use --resume to retain completed attempts')

            def factory(_):
                router = LLMRouter(tiers={})
                router.configure(deepcopy(supporting))
                return MemoryRouter(router, native_context(native_config, shard['recipe']))

            await evaluate(target, shard['recipe'], cases, CONSUMERS, EVALUATORS, factory,
                resume=existing, evidence_mode=args.evidence_mode, suite_version=VERSION)
            report = summarize(target)
            index = len(list(target.glob('report-*.json'))) + 1
            write_once(target / f'report-{index:03d}.json', report)
            publish(target / f'report-{index:03d}.md', markdown(report).encode())
            if any(row.get('cleanup') == 'state_directory_retained' for row in report['cases']):
                break
        report = inspect_batch(args.output)
        index = len(list(args.output.glob('report-*.json'))) + 1
        write_once(args.output / f'report-{index:03d}.json', report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'run'])
    parser.add_argument('--support-config', required=True, type=Path)
    parser.add_argument('--native-config', required=True, type=Path)
    parser.add_argument('--native-binding', required=True)
    parser.add_argument('--hermes-python', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--case-ids')
    parser.add_argument('--native-seconds', type=float, default=120)
    parser.add_argument('--case-seconds', type=float, default=600)
    parser.add_argument('--label', default='native-memory-candidate')
    parser.add_argument('--evidence-mode', choices=['controlled', 'actual_inference'], default='actual_inference')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    manifest, groups, supporting, native_config = prepare(args)
    if args.command == 'plan':
        args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        write_once(args.output / 'benchmark.json', manifest)
        result = manifest
    else:
        frozen = read(args.output / 'benchmark.json')
        if frozen != manifest:
            raise ValueError('Run requires the exact frozen native memory plan')
        inspect_batch(args.output)
        result = asyncio.run(execute(args, manifest, groups, supporting, native_config))
    print(json.dumps(result, indent=2))
    return 0 if args.command == 'plan' or set(result['outcomes']) <= {'pass'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
