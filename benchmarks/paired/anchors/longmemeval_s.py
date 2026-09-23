"""LongMemEval_S anchor: 50 questions, 10 per ability, rendered into the fixture shape.

The dataset (``longmemeval_s.json``, a JSON list of questions with their
haystack sessions) is fetched at run time under its own license and never
vendored. Each rendered scenario seeds the question's haystack sessions as
``history`` (imported into Hermes ``state.db`` in every arm and into the
Protagine ledger in plugin arms, without model calls), then asks the question
in one fresh session and grades ``answer.json`` with ``label_one_of`` against
the canonical short answer; abstention questions expect ``unknown``. Only
questions whose canonical answer is short enough to grade that way are
eligible; the seed picks 10 per ability among them. The output is an ``anchor``
split, reported descriptively (evals section 6.1), never a held-out gate.

    python benchmarks/paired/anchors/longmemeval_s.py --dataset /private/longmemeval_s.json \
        --seed 7 --output /private/families/longmemeval-s-7
"""
import argparse
import calendar
from functools import partial
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import re
import sys
from types import SimpleNamespace

FAMILY = 'longmemeval-s-1'
ROLE = 'reasoning'
ANCHOR = 'longmemeval-s'
PER_ABILITY = 10
MAX_ANSWER_CHARS = 48
ABSTAIN = 'unknown'
ANSWER = 'answer.json'
ABSTENTION_SUFFIX = '_abs'
# The five abilities LongMemEval reports, keyed by the dataset's question types.
ABILITIES = {'information-extraction': ('single-session-user', 'single-session-assistant',
                                        'single-session-preference'),
             'multi-session-reasoning': ('multi-session',),
             'knowledge-update': ('knowledge-update',),
             'temporal-reasoning': ('temporal-reasoning',),
             'abstention': ()}
ENTRY_KEYS = {'question_id', 'question_type', 'question', 'answer', 'question_date',
              'haystack_session_ids', 'haystack_dates', 'haystack_sessions'}
ROLES = ('user', 'assistant')
HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent / 'generators' / 'generate.py'
_LEAF = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
_DATE = re.compile(r'(\d{4})/(\d{2})/(\d{2})(?: \(\w+\))? (\d{2}):(\d{2})')
_ARTICLE = re.compile(r'^(the|a|an)\s+', re.IGNORECASE)
# Sessions whose date does not parse are spaced a day apart from this epoch (2020-01-01).
FALLBACK_EPOCH = 1577836800
DAY = 86400


def engine():
    spec = importlib.util.spec_from_file_location('paired_generate', ENGINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_questions(path):
    entries = json.loads(Path(path).read_text())
    if (not isinstance(entries, list) or not entries
            or any(not isinstance(e, dict) or not ENTRY_KEYS <= set(e) for e in entries)):
        raise ValueError('A LongMemEval file is a JSON list of questions with their haystack sessions')
    if len({e['question_id'] for e in entries}) != len(entries):
        raise ValueError('Question ids must be distinct')
    return entries


def ability_of(entry):
    if str(entry['question_id']).endswith(ABSTENTION_SUFFIX):
        return 'abstention'
    for ability, types in ABILITIES.items():
        if entry['question_type'] in types:
            return ability
    return None


def eligible(entry):
    """Abstention questions always; others only with a short single-line canonical answer."""
    if ability_of(entry) is None:
        return False
    if ability_of(entry) == 'abstention':
        return True
    answer = entry['answer']
    return isinstance(answer, str) and 0 < len(answer.strip()) <= MAX_ANSWER_CHARS and '\n' not in answer.strip()


def labels(entry):
    """The canonical answer and its plain spellings; the grader casefolds and strips."""
    if ability_of(entry) == 'abstention':
        return [ABSTAIN]
    answer = entry['answer'].strip()
    variants = [answer, answer.rstrip('.'), _ARTICLE.sub('', answer.rstrip('.'))]
    digits = re.sub(r'[^0-9.]', '', answer.rstrip('.'))
    if digits and digits.replace('.', '', 1).isdigit():
        variants.append(digits)
    result = []
    for variant in variants:
        if variant and variant not in result:
            result.append(variant)
    return result


def epoch(text):
    match = _DATE.search(str(text))
    if not match:
        return None
    year, month, day, hour, minute = (int(part) for part in match.groups())
    try:
        return calendar.timegm((year, month, day, hour, minute, 0))
    except (ValueError, OverflowError):
        return None


def history_for(entry):
    sessions = []
    rows = zip(entry['haystack_session_ids'], entry['haystack_dates'], entry['haystack_sessions'])
    for index, (identity, date, turns) in enumerate(rows):
        messages = [{'role': turn['role'], 'content': turn['content']} for turn in turns
                    if isinstance(turn, dict) and turn.get('role') in ROLES
                    and isinstance(turn.get('content'), str) and turn['content'].strip()]
        if not messages:
            continue
        name = str(identity)
        if not _LEAF.fullmatch(name):
            name = f'h-{index + 1:03d}'
        at = epoch(date)
        sessions.append({'id': name, 'at': FALLBACK_EPOCH + index * DAY if at is None else at,
                         'messages': messages})
    if not sessions:
        raise ValueError('A question needs at least one haystack session with conversation turns')
    return sessions


def probe(entry):
    return (f"{entry['question'].strip()} (The date of this question is {entry['question_date']}.) "
            f'Write {ANSWER} as exactly {{"answer": string}} with a short answer; if I was never told, '
            f'write "{ABSTAIN}" as the answer rather than a guess.')


def render_question(entry, draw=None):
    """One scenario: the haystack as history, the question as the only turn, a labelled answer file."""
    return {'initial_files': {}, 'history': history_for(entry),
            'episodes': [{'session_id': 'question-1', 'user': probe(entry)}],
            'artifacts': [{'path': ANSWER, 'format': 'json', 'forbidden': [],
                           'assertions': [{'path': [], 'op': 'keys_equal', 'value': ['answer']},
                                          {'path': ['answer'], 'op': 'label_one_of', 'value': labels(entry)}]}]}


def template_name(entry):
    identity = str(entry['question_id'])
    if not _LEAF.fullmatch(identity) or len(identity) > 60:
        identity = 'q-' + hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f'{ability_of(entry)}--{identity}'


def select(entries, seed, per_ability=PER_ABILITY):
    """Ten eligible questions per ability, drawn by the seed from the ids in sorted order."""
    rng = random.Random(seed)
    chosen = []
    for ability in ABILITIES:
        pool = sorted((e for e in entries if eligible(e) and ability_of(e) == ability), key=lambda e: e['question_id'])
        if len(pool) < per_ability:
            raise ValueError(f'{ability}: {len(pool)} eligible questions, {per_ability} needed')
        chosen.extend(rng.sample(pool, per_ability))
    return chosen


def module_for(entries, seed, per_ability=PER_ABILITY):
    chosen = select(entries, seed, per_ability)
    templates = {template_name(e): (ability_of(e), partial(render_question, e)) for e in chosen}
    if len(templates) != len(chosen):
        raise ValueError('Template names collide')
    return SimpleNamespace(FAMILY=FAMILY, ROLE=ROLE, TEMPLATES=templates), chosen


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dataset', type=Path, required=True, help='longmemeval_s.json, fetched at run time')
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--per-ability', type=int, default=PER_ABILITY)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    generate = engine()
    entries = load_questions(args.dataset)
    module, chosen = module_for(entries, args.seed, args.per_ability)
    extra = {'anchor': ANCHOR, 'anchor_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'per_ability': args.per_ability,
             'question_ids': {template_name(e): e['question_id'] for e in chosen},
             'source_questions': len(entries)}
    content = generate.write(args.output, module, args.seed, 'anchor', 1, args.dataset, extra=extra)
    from protagine.qualification.paired_cases import load_generated_dataset
    manifest, scenarios, verified = load_generated_dataset(args.output)
    if verified != content:
        raise RuntimeError('Loader content hash differs from the written hash')
    print(json.dumps({'dataset_id': manifest['dataset_id'], 'split': 'anchor', 'seed': args.seed,
                      'scenario_count': len(scenarios), 'families': manifest['families'],
                      'content_sha256': content, 'output': str(Path(args.output).resolve())}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
