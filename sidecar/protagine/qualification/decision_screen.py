"""Small, frozen decision-role screen. No live routing or memory writes.

Adapters receive only ``request_for(case)``. They return the record described in
``evaluate``; neither labels nor rationales belong in a model request. This is a
classifier screen, not evidence that a native agent's task outcomes improved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import mean


VERSION = 'decision-role-screen-1'
DIRECTORY = Path(__file__).parent / 'fixtures' / VERSION
LIMITATIONS = [
    'Authored public synthetic screening data, not an independent hidden test set.',
    'Related perturbations are reported separately, not counted as independent tasks.',
    'No native Hermes turns, memory writes, retrieval execution or downstream task outcomes.',
    'Confidence metrics describe this small sample, not a deployment guarantee.',
    'Latency boundaries and hardware must be supplied for every backend.',
    'Observed answer accuracy is separate from input coverage; missing provider telemetry is not a wrong answer.',
]


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def load(directory=DIRECTORY):
    directory = Path(directory)
    raw = (directory / 'scenarios.json').read_bytes()
    manifest = json.loads((directory / 'manifest.json').read_bytes())
    expected = manifest['files']['scenarios.json']
    if hashlib.sha256(raw).hexdigest() != expected['sha256'] or len(raw) != expected['bytes']:
        raise ValueError('Decision fixture checksum mismatch')
    if manifest['dataset_id'] != VERSION:
        raise ValueError('Unknown decision fixture version')
    cases = json.loads(raw)
    if not cases or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Decision cases must have distinct identities')
    return manifest, cases


def request_for(case):
    """An explicit allowlist prevents oracle/rationale fields reaching a backend."""
    questions = {'decision': case['question']}
    if 'evidence_question' in case:
        questions['evidence'] = case['evidence_question']
    return {'state': encode(case['state']).decode(), 'questions': questions}


def request_hash(case):
    return hashlib.sha256(encode(request_for(case))).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _answer(answer, question, expected):
    labels = list(question['criteria'])
    if not isinstance(answer, dict):
        raise ValueError('Missing answer')
    probabilities = answer.get('probabilities')
    if not isinstance(probabilities, dict) or set(probabilities) != set(labels):
        raise ValueError('Probability labels differ from requested labels')
    if any(not _number(p) or not 0 <= p <= 1 for p in probabilities.values()):
        raise ValueError('Invalid probability')
    total = sum(probabilities.values())
    # Laya's public API rounds every entry to four decimals. Keep the raw answer;
    # normalize only rounding-sized discrepancies for proper probability scores.
    if abs(total - 1) > 0.000051 * len(labels) or total <= 0:
        raise ValueError('Probability sum exceeds rounding tolerance')
    choice = answer.get('choice')
    if choice not in probabilities or probabilities[choice] != max(probabilities.values()):
        raise ValueError('Selected label is not a maximum-probability option')
    probs = {k: v / total for k, v in probabilities.items()}
    return {
        'correct': choice == expected, 'choice': choice, 'expected': expected,
        'confidence': max(probs.values()),
        'brier': sum((p - int(label == expected)) ** 2 for label, p in probs.items()),
        'log_loss': -math.log(max(probs[expected], 1e-15)),
    }


def evaluate(case, record):
    """Grade a captured first attempt; missing/error attempts stay in denominators.

    Required record: case_id, request_sha256, status, input_truncated, answers,
    latency_ms. status is ok/input_rejected/error. A token-budget rejection may
    provide rejection_reason=input_would_truncate for explicit overflow probes.
    Timing/usage and backend identities are captured separately, never inferred.
    """
    row = {k: case[k] for k in ('id', 'group_id', 'family', 'kind')}
    row.update(status='missing', correct=False, evidence_correct=False, joint_correct=False)
    if record is None:
        return row
    if record.get('case_id') != case['id'] or record.get('request_sha256') != request_hash(case):
        raise ValueError('Captured record does not match frozen input')
    row['status'] = record.get('status', 'error')
    if _number(record.get('latency_ms')) and record['latency_ms'] >= 0:
        row['latency_ms'] = record['latency_ms']
    row['input_truncated'] = record.get('input_truncated')
    row['input_coverage_verified'] = record.get('input_truncated') is False
    if case['kind'] == 'overflow':
        row['safe_overflow'] = (record.get('status') == 'input_rejected'
            and record.get('rejection_reason') == 'input_would_truncate'
            and record.get('input_truncated') is False)
    if row['status'] != 'ok':
        return row
    if record.get('input_truncated') is True:
        row['status'] = 'input_truncated'
        return row
    answers = record.get('answers', {})
    try:
        if not isinstance(answers, dict):
            raise ValueError('Answers must be an object')
        decision = _answer(answers.get('decision'), case['question'], case['expected_label'])
    except (ValueError, TypeError, KeyError):
        row['status'] = 'invalid_answer'
    else:
        row.update(decision)
    row['decision_status'] = row['status']
    row['evidence_status'] = 'not_requested'
    if 'evidence_question' in case:
        try:
            evidence = _answer(answers.get('evidence'), case['evidence_question'],
                               case['expected_evidence_label'])
        except (ValueError, TypeError, KeyError, AttributeError):
            row['evidence_status'] = 'invalid_answer'
        else:
            row['evidence_status'] = 'ok'
            row['evidence_correct'] = evidence['correct']
    else:
        row['evidence_correct'] = None
    row['joint_correct'] = row['correct'] and row['evidence_correct'] is not False
    if case['kind'] == 'overflow':
        row['safe_overflow'] = row['joint_correct'] and row['input_coverage_verified']
    return row


def _percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * p
    low, high = math.floor(pos), math.ceil(pos)
    return values[low] + (values[high] - values[low]) * (pos - low)


def summarize(rows):
    valid = [r for r in rows if r['status'] == 'ok']
    latencies = [r['latency_ms'] for r in rows if 'latency_ms' in r]
    correct = sum(r['correct'] for r in rows)
    evidence_rows = [r for r in rows if r.get('evidence_correct') is not None]
    # These fixed cutoffs show error/coverage tradeoffs. None chooses a threshold
    # from this evaluation set or changes the agent's policy.
    selective = []
    for threshold in (0.5, 0.7, 0.9):
        accepted = [r for r in valid if r['confidence'] >= threshold]
        selective.append({'threshold': threshold, 'accepted': len(accepted),
            'coverage': len(accepted) / len(rows) if rows else None,
            'error_rate': mean(not r['correct'] for r in accepted) if accepted else None})
    return {'count': len(rows), 'valid': len(valid), 'correct': correct,
        'accuracy': correct / len(rows) if rows else None,
        'input_coverage_verified': sum(r.get('input_coverage_verified', False) for r in rows),
        'complete_input_comparison': bool(rows) and all(r.get('input_coverage_verified') for r in rows),
        'evidence_correct': sum(bool(r['evidence_correct']) for r in evidence_rows),
        'evidence_valid': sum(r.get('evidence_status') == 'ok' for r in rows),
        'joint_correct': sum(r['joint_correct'] for r in rows),
        'brier_valid_only': mean(r['brier'] for r in valid) if valid else None,
        'log_loss_valid_only': mean(r['log_loss'] for r in valid) if valid else None,
        'latency_count': len(latencies), 'latency_p50_ms': _percentile(latencies, .5),
        'latency_p95_ms': _percentile(latencies, .95),
        'statuses': {s: sum(r['status'] == s for r in rows) for s in sorted({r['status'] for r in rows})},
        'selective': selective}


def report(cases, records, *, backend, dataset_sha256):
    if not backend.get('id') or not backend.get('revision') or not backend.get('latency_boundary'):
        raise ValueError('Declare backend identity, revision and latency boundary')
    by_id = {}
    known = {c['id'] for c in cases}
    for record in records:
        identity = record['case_id']
        if identity in by_id or identity not in known:
            raise ValueError('Duplicate or unknown attempts; no best-of selection')
        by_id[identity] = record
    rows = [evaluate(c, by_id.get(c['id'])) for c in cases]
    base = [r for r in rows if r['kind'] == 'base']
    perturbations = [r for r in rows if r['kind'] == 'perturbation']
    overflow = [r for r in rows if r['kind'] == 'overflow']
    parents = {r['id']: r for r in rows}
    pairs = []
    for case in cases:
        if case['kind'] != 'perturbation':
            continue
        parent, child = parents[case['perturbation']['parent_id']], parents[case['id']]
        pairs.append({'parent': parent['id'], 'child': child['id'],
            'both_valid': parent['status'] == child['status'] == 'ok',
            'both_correct': parent['correct'] and child['correct'],
            'choice_agreement': parent.get('choice') == child.get('choice')
                if parent['status'] == child['status'] == 'ok' else None})
    return {'schema_version': 1, 'dataset_id': VERSION, 'dataset_sha256': dataset_sha256,
        'backend': backend, 'complete': len(by_id) == len(cases),
        'base': summarize(base),
        'families': {f: summarize([r for r in base if r['family'] == f])
                     for f in sorted({r['family'] for r in base})},
        'perturbations': {**summarize(perturbations), 'pairs': pairs},
        'overflow': {'count': len(overflow), 'safe': sum(r.get('safe_overflow', False) for r in overflow)},
        'rows': rows, 'limitations': LIMITATIONS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DIRECTORY)
    parser.add_argument('--records', type=Path, required=True, help='Captured first attempts, JSONL')
    parser.add_argument('--backend', type=Path, required=True, help='Backend identity and timing boundary JSON')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest, cases = load(args.dataset)
    records = [json.loads(line) for line in args.records.read_text().splitlines() if line.strip()]
    result = report(cases, records, backend=json.loads(args.backend.read_text()),
                    dataset_sha256=manifest['files']['scenarios.json']['sha256'])
    with args.output.open('x') as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write('\n')


if __name__ == '__main__':
    main()
