"""Seeded scenario generators for paired families.

A family module lists templates; each template turns seeded draws (contacts,
items, horizons, paraphrases) into one scenario with an oracle computed from
the same draws. The output is the frozen fixture shape, ``manifest.json`` and
``scenarios.json``, byte-hashed the way the loader hashes it. Dev templates
live in this directory. Held-out templates are a Python file outside the
repository, named by ``--heldout-templates`` or ``PROTAGINE_HELDOUT_TEMPLATES``,
and are never committed.

    python benchmarks/paired/generators/generate.py --family initiative \
        --split dev --seed 7 --per-template 3 --output /private/families/initiative-dev-7

A template may also render ``workflow`` (``restart_before``, ``snapshot_after``)
and ``checkpoints`` (artifact checks on a snapshot), the frozen workflow
contract, for a family whose probe follows a restart.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import sys

PROTOCOL = 'paired-generator-1'
HELDOUT_ENV = 'PROTAGINE_HELDOUT_TEMPLATES'
HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[2]
# Every family module in this directory; ``--family`` is the module's stem.
FAMILIES = {path.stem: path for path in sorted(HERE.glob('*.py')) if path.stem != Path(__file__).stem}
MAX_PER_TEMPLATE = 16
RENDERED_KEYS = {'initial_files', 'episodes', 'body', 'artifacts', 'workflow', 'checkpoints'}
_LEAF = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')


class Draw:
    """Deterministic draws for one scenario instance; contact and source ids are fixed-width and distinct."""

    def __init__(self, seed):
        self.random = random.Random(seed)
        self.contacts = []
        self.sources = []

    def pick(self, options):
        return options[self.random.randrange(len(options))]

    def picks(self, options, count):
        return self.random.sample(list(options), count)

    def integer(self, low, high):
        return self.random.randint(low, high)

    def contact(self):
        """``p-01``..``p-99``: a forbidden check on one id can never match another."""
        return self._identity('p', self.contacts)

    def source(self):
        """``s-01``..``s-99``: a cited source id, distinct from every other id in the instance."""
        return self._identity('s', self.sources)

    def _identity(self, prefix, drawn):
        """A fresh fixed-width id whose number no earlier contact or source of the instance used."""
        while True:
            identity = '%s-%02d' % (prefix, self.random.randint(1, 99))
            if identity not in self.contacts and identity not in self.sources:
                drawn.append(identity)
                return identity


def instance_seed(seed, template, index):
    text = f'{seed}:{template}:{index}'.encode()
    return int.from_bytes(hashlib.sha256(text).digest()[:8], 'big')


def load_templates(path):
    """A family module: FAMILY, TEMPLATES = {name: (group, render)}, optional ROLE."""
    path = Path(path)
    spec = importlib.util.spec_from_file_location('paired_family_' + hashlib.sha256(str(path).encode()).hexdigest()[:12], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    family, templates = getattr(module, 'FAMILY', None), getattr(module, 'TEMPLATES', None)
    if (not isinstance(family, str) or not _LEAF.fullmatch(family)
            or not isinstance(templates, dict) or not templates or len(templates) > 64
            or any(not isinstance(name, str) or not _LEAF.fullmatch(name) or not isinstance(entry, tuple)
                   or len(entry) != 2 or not isinstance(entry[0], str) or not _LEAF.fullmatch(entry[0])
                   or not callable(entry[1]) for name, entry in templates.items())):
        raise ValueError('A family module declares FAMILY and TEMPLATES = {name: (group, render)}')
    return module


def render(module, seed, per_template):
    """Every template rendered ``per_template`` times from seeds derived from (seed, name, index)."""
    if type(seed) is not int or not 0 <= seed < 2 ** 32:
        raise ValueError('The seed is a 32-bit integer')
    if type(per_template) is not int or not 1 <= per_template <= MAX_PER_TEMPLATE:
        raise ValueError(f'Instances per template are 1..{MAX_PER_TEMPLATE}')
    role = getattr(module, 'ROLE', 'reasoning')
    scenarios = []
    for name, (group, template) in sorted(module.TEMPLATES.items()):
        for index in range(1, per_template + 1):
            instance = instance_seed(seed, name, index)
            rendered = template(Draw(instance))
            if (not isinstance(rendered, dict) or not {'initial_files', 'episodes'} <= set(rendered)
                    or set(rendered) - RENDERED_KEYS
                    or not ('body' in rendered or rendered.get('artifacts'))
                    or ('checkpoints' in rendered and 'workflow' not in rendered)):
                raise ValueError('A template renders initial_files, episodes and a body or artifacts oracle')
            oracle = {'declared_turns': len(rendered['episodes']), 'artifacts': list(rendered.get('artifacts', []))}
            if 'body' in rendered:
                oracle['body'] = rendered['body']
            if 'checkpoints' in rendered:
                oracle['checkpoints'] = list(rendered['checkpoints'])
            scenario = {'id': f'{name}.{index:02d}', 'family': group, 'scenario': name, 'seed': instance,
                        'role': role, 'initial_files': rendered['initial_files'],
                        'episodes': rendered['episodes'], 'limitations': [], 'oracle': oracle}
            if 'workflow' in rendered:
                # A restart (fresh worker process over the same state) before the probe, and
                # workspace snapshots the oracle's checkpoints grade as of that turn.
                scenario['workflow'] = rendered['workflow']
            scenarios.append(scenario)
    return scenarios


def encode(value):
    return (json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + '\n').encode()


def manifest_for(module, scenarios, *, split, seed, per_template, template_path, scenario_bytes):
    counts = {}
    for item in scenarios:
        counts[item['family']] = counts.get(item['family'], 0) + 1
    return {'dataset_id': module.FAMILY, 'version': module.FAMILY, 'scenario_count': len(scenarios),
            'families': counts,
            'files': {'scenarios.json': {'bytes': len(scenario_bytes),
                                         'sha256': hashlib.sha256(scenario_bytes).hexdigest()}},
            'generator': {'protocol': PROTOCOL, 'family': module.FAMILY, 'split': split, 'seed': seed,
                          'per_template': per_template,
                          'templates': {name: per_template for name in sorted(module.TEMPLATES)},
                          'template_source_sha256': hashlib.sha256(Path(template_path).read_bytes()).hexdigest(),
                          'engine_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
            'source': {'kind': 'generated_synthetic',
                       'description': 'Seeded template instances with synthetic fixed-width contacts; '
                                      'no production conversations, names, hosts or credentials.'},
            'limitations': ['Generated from parameterized templates; instances share template structure.',
                            'A dev split is public development data, never a held-out result.']}


def write(directory, module, seed, split, per_template, template_path):
    """Write manifest.json and scenarios.json once; return the loader's content hash."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    targets = [directory / 'manifest.json', directory / 'scenarios.json']
    if any(target.exists() for target in targets):
        raise FileExistsError('Refusing to overwrite an existing generated dataset')
    scenarios = render(module, seed, per_template)
    scenario_bytes = encode(scenarios)
    manifest_bytes = encode(manifest_for(module, scenarios, split=split, seed=seed, per_template=per_template,
                                         template_path=template_path, scenario_bytes=scenario_bytes))
    targets[0].write_bytes(manifest_bytes)
    targets[1].write_bytes(scenario_bytes)
    return hashlib.sha256(b'manifest\0' + manifest_bytes + b'\0scenarios\0' + scenario_bytes).hexdigest()


def heldout_path(argument):
    path = argument or os.environ.get(HELDOUT_ENV)
    if not path:
        raise ValueError(f'A held-out split needs --heldout-templates or {HELDOUT_ENV}')
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('Held-out templates must be an existing file')
    if path.is_relative_to(REPOSITORY):
        raise ValueError('Held-out templates must live outside the repository')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--family', choices=sorted(FAMILIES), required=True)
    parser.add_argument('--split', choices=('dev', 'heldout'), required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--per-template', type=int, default=3)
    parser.add_argument('--heldout-templates', help=f'Python file outside the repository (or {HELDOUT_ENV})')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    dev = load_templates(FAMILIES[args.family])
    template_path = FAMILIES[args.family]
    module = dev
    if args.split == 'heldout':
        template_path = heldout_path(args.heldout_templates)
        module = load_templates(template_path)
        if module.FAMILY != dev.FAMILY:
            raise ValueError('Held-out templates must declare the same FAMILY as the dev family')
    content = write(args.output, module, args.seed, args.split, args.per_template, template_path)
    # The installed loader is the authority on the shape; refuse output it would refuse.
    from protagine.qualification.paired_cases import load_generated_dataset
    manifest, scenarios, verified = load_generated_dataset(args.output)
    if verified != content:
        raise RuntimeError('Loader content hash differs from the written hash')
    print(json.dumps({'dataset_id': manifest['dataset_id'], 'split': args.split, 'seed': args.seed,
                      'scenario_count': len(scenarios), 'families': manifest['families'],
                      'content_sha256': content, 'output': str(Path(args.output).resolve())}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
