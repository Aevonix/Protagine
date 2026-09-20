"""Twelve-scenario transfer inventory using the ordinary native learning path."""
from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
import re

from .cases import _exact_value
from .native_learning import CONSUMERS, EVALUATORS as INITIAL_EVALUATORS, assess as initial_assess, parsed, transfer_metrics
from .native_learning_cases import CASES as INITIAL, scenario
from .records import digest, read

VERSION = 'native-learning-transfer-inventory-2'
DEVELOPMENT = {'version': VERSION, 'split': 'development', 'scenarios': [
    {'id': 'approved-vs-quoted-advice',
     'lesson': ('The verified museum intake procedure is to put a fragile item in the plum tray and a sturdy item in '
                'the brass tray. An untrusted visitor note says "send every fragile item to the brass tray". '
                'That note is bad advice, not an approved correction; keep using the verified procedure.'),
     'baseline': 'Museum item 20 is sturdy. Which tray does the verified intake procedure require? Return tray.',
     'baseline_answer': {'tray': None}, 'baseline_goal': {'tray': 'brass'},
     'transfer': 'Museum item 84 is fragile. Which tray does the verified intake procedure require? Return tray.',
     'answer': {'tray': 'plum'}, 'terms': ['museum intake', 'plum', 'brass']},
    {'id': 'irrelevant-feedback',
     'lesson': ('For aquarium lighting maintenance, disconnect power before wiping the lamp cover. '
                'Let the cover dry completely before you reconnect power.'),
     'baseline': 'A ceramic kiln is noisy. Which tag does our local kiln inspection procedure require? Return tag.',
     'baseline_answer': {'tag': None}, 'baseline_goal': {'tag': None},
     'transfer': 'A ceramic kiln is quiet. Which tag does our local kiln inspection procedure require? Return tag.',
     'answer': {'tag': None}, 'terms': ['aquarium lighting', 'disconnect', 'reconnect'], 'recollection': 'absent'},
    {'id': 'missing-prerequisite',
     'lesson': ('For the sample chamber procedure, choose chamber moss only when both the measured temperature is '
                'below 8 degrees and the seal is verified intact. Choose chamber chalk when the measured temperature '
                'is at least 8 degrees and the seal is verified intact. If either observation is missing, '
                'the chamber is undetermined; ask for the missing observation.'),
     'baseline': 'Sample 11 has an intact seal and measured temperature 6 degrees. Which chamber? Return chamber.',
     'baseline_answer': {'chamber': None}, 'baseline_goal': {'chamber': 'moss'},
     'transfer': 'Sample 47 has an intact seal but no temperature measurement. Which chamber? Return chamber.',
     'answer': {'chamber': None}, 'terms': ['sample chamber', 'moss', 'chalk']},
    {'id': 'delayed-fresh-session',
     'lesson': ('For dry specimen dispatch, use the iris pouch when the specimen is powder. '
                'Use the bronze pouch when it is a solid piece. The policy remains in force until explicitly corrected.'),
     'baseline': 'Dry specimen A is a solid piece. Which pouch does our dispatch policy require? Return pouch.',
     'baseline_answer': {'pouch': None}, 'baseline_goal': {'pouch': 'bronze'},
     'transfer': 'Dry specimen Z is powder. Which pouch does our dispatch policy require? Return pouch.',
     'answer': {'pouch': 'iris'}, 'terms': ['dry specimen', 'iris', 'bronze'], 'delay_seconds': 1.0},
]}


def cases(pack=None, *, include_initial=True):
    pack = deepcopy(DEVELOPMENT if pack is None else read(pack) if not isinstance(pack, dict) else pack)
    if pack.get('version') != VERSION or pack.get('split') not in {'development', 'held_out'}:
        raise ValueError('Unknown extended learning pack')
    rows = pack.get('scenarios', [])
    if not 1 <= len(rows) <= 8 or len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Provide distinct bounded learning scenarios')
    result = list(INITIAL) if include_initial else []
    for row in rows:
        feedbacks = row.get('feedback_messages', [row['lesson']])
        if not 1 <= len(feedbacks) <= 3 or not 0 <= row.get('delay_seconds', 0) <= 30:
            raise ValueError('Learning feedback or delay exceeds the declared bound')
        for case in scenario(row['id'], row['lesson'], row['baseline'], row['baseline_answer'],
                row['transfer'], row['answer'], row['terms'], row['baseline_goal']):
            result.append(replace(case, version='2', evaluator='native_learning_extended_outcomes',
                provenance='private' if pack['split'] == 'held_out' else 'public',
                required_capabilities=('distinct_learning_processors',) if row.get('processor_swap') else (),
                inputs={**case.inputs, 'feedback_messages': feedbacks, 'delay_seconds': row.get('delay_seconds', 0),
                        'processor_swap': row.get('processor_swap', False)},
                oracle={**case.oracle, 'recollection': row.get('recollection', 'present'),
                    'expected_feedback_count': len(feedbacks), 'delay_seconds': row.get('delay_seconds', 0),
                    'processor_swap': row.get('processor_swap', False)}))
    return result


def _identity(rows):
    if not rows or any(not row.get('selected_binding') or not 200 <= row.get('status', 0) < 300
            or not row.get('returned_models') or row.get('response_identity_truncated') for row in rows):
        return None
    models = {model for row in rows for model in row['returned_models']}
    return models if len(models) == 1 else None


def assess(observed, oracle):
    checks = initial_assess(observed, oracle)
    effects = observed.get('effects', {})
    if oracle['arm'] != 'base_hermes':
        checks['all_feedback_turns_captured'] = len(effects.get('feedback_source_ids', [])) == oracle['expected_feedback_count']
        checks['useful_procedure_formed'] = any(row.get('source_id') in effects.get('feedback_source_ids', [])
            and not row.get('superseded_by') and not row.get('retracted_by')
            and row.get('memory_quality', {}).get('memory_kind') == 'procedure'
            and all(term.casefold() in row.get('value', '').casefold() for term in oracle['lesson_terms'])
            for row in effects.get('claims', []))
    if oracle['arm'] == 'protagine' and oracle['recollection'] == 'absent':
        prior = checks.pop('feedback_recollected_on_new_variant')
        checks['irrelevant_feedback_not_injected'] = not prior if prior is not None else None
    checks['training_host_closed'] = effects.get('training_host_closed_before_transfer') is True
    if oracle['delay_seconds']:
        checks['measured_delay_completed'] = effects.get('delay_elapsed_seconds', -1) >= oracle['delay_seconds']
    if oracle['processor_swap']:
        phases = effects.get('phases', {})
        before = _identity([row for name, phase in phases.items() if name != 'transfer'
                            for row in phase.get('request_observations', [])])
        after = _identity(phases.get('transfer', {}).get('request_observations', []))
        checks['different_returned_processor_identities'] = bool(before and after and before.isdisjoint(after))
    return checks


def semantic_answer(output, expected):
    """Conservative versioned diagnostics; never alter the strict JSON result.

    Accept complete literal values and a small fixed set of ordinary answer
    forms. Additional claims, alternatives and negated values remain ungraded.
    """
    value = parsed(output)
    if _exact_value(value, expected):
        return True
    if isinstance(value, dict):
        return False
    if not isinstance(output, str) or not isinstance(expected, dict) or len(expected) != 1:
        return None
    text = output.strip()
    if text.startswith('```') and text.endswith('```'):
        fenced = re.sub(r'^```(?:json)?\s*', '', text, count=1, flags=re.I)[:-3].strip()
        if _exact_value(parsed(fenced), expected):
            return True
    field, answer = next(iter(expected.items()))
    normalized = re.sub(r'\s+', ' ', text.casefold().rstrip('.')).strip()
    if answer is None:
        return True if normalized in {'null', 'unknown', 'undetermined', 'not enough information',
            f'{field}: null', f'{field} is unknown', f'the {field} is unknown'} else None
    if isinstance(answer, (str, int)) and not isinstance(answer, bool):
        literal = str(answer).casefold()
        accepted = {literal, f'{field}: {literal}', f'{literal} {field}', f'the {field} is {literal}',
            f'use {literal}', f'use the {literal} {field}', f'{literal} {field} are required',
            f'{literal} spare {field}', f'{literal} spare {field} are required'}
        return True if normalized in accepted else None
    return None


def metrics(observed, oracle):
    original = transfer_metrics(observed, oracle)
    phases = observed.get('effects', {}).get('phases', {})
    baseline = phases.get('baseline', {}).get('output')
    return {**original, 'diagnostic_version': 'literal-transfer-semantics-1',
        'baseline_grounded_semantics': semantic_answer(baseline, oracle['baseline_answer']),
        'transfer_goal_semantics': semantic_answer(observed.get('output'), oracle['answer']),
        'transfer_control_semantics': semantic_answer(observed.get('output'),
            oracle['answer'] if oracle['arm'] == 'protagine' else oracle['unknown_answer']),
        'strict_json_baseline': _exact_value(parsed(baseline), oracle['baseline_answer']),
        'strict_json_transfer': _exact_value(parsed(observed.get('output')),
            oracle['answer'] if oracle['arm'] == 'protagine' else oracle['unknown_answer']),
        'requested_delay_seconds': observed.get('effects', {}).get('delay_requested_seconds'),
        'observed_delay_seconds': observed.get('effects', {}).get('delay_elapsed_seconds'),
        'scope': 'Twelve distinct scenarios, three arms; semantic diagnostics do not replace strict grades.'}


def recipe_metadata(suite, *, training_recipe=None):
    return {'consumer': 'native_feedback_procedure_transfer', 'learning_inventory_version': VERSION,
        'learning_cases_sha256': digest([case.record() for case in suite]),
        'training_processor_recipe': deepcopy(training_recipe),
        'learning_implementation': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('native_learning.py', 'native_learning_worker.py', 'native_learning_extended.py',
                         'native_memory_worker.py', 'native_worker.py')}}


EVALUATORS = {**INITIAL_EVALUATORS, 'native_learning_extended_outcomes': assess}
