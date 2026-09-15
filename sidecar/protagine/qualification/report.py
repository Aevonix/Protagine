"""Descriptive results by consumer boundary; all declared cases count."""
from collections import Counter, defaultdict
from pathlib import Path
import statistics

from .records import digest, read

OUTCOMES = {'pass', 'fail', 'unsupported', 'setup_error', 'error', 'timeout', 'interrupted', 'not_run'}


def _configured_task(case, recipe):
    """Compare only the two transformations made by configured-output-v1."""
    policy = recipe.get('qualification_output_policy', {})
    budget = policy.get('cases', {}).get(case['id'], {})
    if (policy.get('version') != 'configured-output-v1'
            or policy.get('binding') != recipe.get('binding')
            or case['boundary'] != 'role_completion' or case['consumer'] != 'role_completion'
            or not case['version'].endswith('-configured-output-v1')
            or not budget or budget.get('max_output_tokens') != case['inputs'].get('max_output_tokens')
            or budget.get('case_timeout_seconds') != case['timeout_seconds']
            or budget.get('case_overhead_seconds') != 5
            or not isinstance(budget.get('role_deadline_seconds'), (int, float))
            or case['timeout_seconds'] < budget['role_deadline_seconds'] + 5):
        return None, None
    # All questions, oracle bytes, versions, capabilities and other bounds count.
    # Only the declared client allowance and outer time envelope may differ.
    task = {key: value for key, value in case.items()
            if key not in {'sha256', 'inputs_sha256', 'oracle_sha256', 'timeout_seconds'}}
    task['inputs'] = {key: value for key, value in case['inputs'].items() if key != 'max_output_tokens'}
    return digest(task), budget


def summarize(directory):
    directory = Path(directory)
    run = read(directory / 'run.json')
    rows, groups = [], defaultdict(list)
    for case in run['cases']:
        path = directory / 'attempts' / case['id'] / 'result.json'
        result = read(path) if path.exists() else {'outcome': 'not_run', 'primary_outcome': 'unverified', 'elapsed_ms': None}
        if path.exists() and (result.get('run_id') != run['id'] or result.get('case_sha256') != case['sha256']):
            raise ValueError('Result identity does not match declared case')
        if result['outcome'] not in OUTCOMES:
            raise ValueError('Unknown outcome')
        task_identity, budget = _configured_task(case, run['recipe'])
        row = {'case_id': case['id'], 'role': case['role'], 'boundary': case['boundary'],
               'case_sha256': case['sha256'], 'inputs_sha256': case['inputs_sha256'],
               'oracle_sha256': case['oracle_sha256'],
               'configured_task_sha256': task_identity, 'configured_output_budget': budget,
               'evaluator_identity': run.get('evaluator_identities', {}).get(case['evaluator']), **result}
        rows.append(row)
        groups[(case['role'], case['boundary'])].append(row)
    summaries = []
    for (role, boundary), items in sorted(groups.items()):
        durations = {}
        for outcome in sorted({r['outcome'] for r in items}):
            times = [r['elapsed_ms'] for r in items if r['outcome'] == outcome and r.get('elapsed_ms') is not None]
            durations[outcome] = {'samples': len(times), 'median_ms': statistics.median(times) if times else None}
        summaries.append({'role': role, 'boundary': boundary, 'declared': len(items),
            'outcomes': dict(Counter(r['outcome'] for r in items)),
            'primary_passes': sum(r.get('primary_outcome') == 'pass' for r in items),
            'duration_by_outcome': durations,
            'coverage': 'only the listed cases and consumer boundary; not full role qualification'})
    return {'schema': 1, 'kind': 'run', 'run_id': run['id'], 'recipe': run['recipe'],
            'suite_sha256': run['suite_sha256'], 'evidence_mode': run['evidence_mode'],
            'implementation_sha256': run.get('implementation_sha256'),
            'declared': len(rows), 'groups': summaries, 'cases': rows}


def compare(left, right):
    before, after = summarize(left), summarize(right)
    old = {r['case_id']: r for r in before['cases']}
    new = {r['case_id']: r for r in after['cases']}
    rows = []
    for identity in sorted(old.keys() | new.keys()):
        a, b = old.get(identity), new.get(identity)
        same_grading = bool(a and b and a.get('evaluator_identity') is not None
                            and a['evaluator_identity'] == b.get('evaluator_identity')
                            and before['evidence_mode'] == after['evidence_mode'])
        same_case = bool(a and b and a['case_sha256'] == b['case_sha256'])
        configured_comparison = bool(a and b and a.get('configured_task_sha256') is not None
            and a['configured_task_sha256'] == b.get('configured_task_sha256')
            and before['implementation_sha256'] is not None
            and before['implementation_sha256'] == after['implementation_sha256']
            and before['recipe'].get('runtime_version') is not None
            and before['recipe']['runtime_version'] == after['recipe'].get('runtime_version'))
        comparable = same_grading and (same_case or configured_comparison)
        budgets_differ = bool(a and b and a.get('configured_output_budget')
                             != b.get('configured_output_budget'))
        basis = ('same_task_different_budget_recipes' if configured_comparison and budgets_differ
                 else 'identical_case' if same_case else 'incomparable') if comparable else 'incomparable'
        rows.append({'case_id': identity, 'comparable': comparable,
            'comparison_basis': basis,
            'before_budget': a.get('configured_output_budget') if a else None,
            'after_budget': b.get('configured_output_budget') if b else None,
            'grading_changed': bool(a and b and a.get('evaluator_identity') != b.get('evaluator_identity')),
            'before': a['outcome'] if a else 'absent', 'after': b['outcome'] if b else 'absent',
            'primary_before': a.get('primary_outcome') if a else None,
            'primary_after': b.get('primary_outcome') if b else None,
            'elapsed_ms_delta': (b['elapsed_ms'] - a['elapsed_ms']) if comparable
                and a['outcome'] == b['outcome'] == 'pass'
                and a.get('elapsed_ms') is not None and b.get('elapsed_ms') is not None else None})
    return {'schema': 1, 'kind': 'comparison', 'before': before, 'after': after, 'cases': rows,
            'basis': 'recipe comparison; no causal weight, serving identity, tail-latency or confidence claim',
            'system_implementation_changed': before['implementation_sha256'] != after['implementation_sha256'],
            'same_suite': before['suite_sha256'] == after['suite_sha256']}


def _check_text(checks):
    if not checks:
        return 'not recorded'
    groups = []
    for label, value in (('pass', True), ('fail', False), ('unknown', None)):
        names = sorted(name for name, observed in checks.items() if observed is value)
        if names:
            groups.append(f"{label}: {', '.join(names)}")
    return '; '.join(groups)


def markdown(report):
    if report['kind'] == 'comparison':
        lines = ['# Recipe comparison', '', report['basis'], '',
                 '| Case | Comparison basis | Before | After | Primary before/after | Elapsed change ms |',
                 '| --- | --- | --- | --- | --- | --- |']
        for row in report['cases']:
            lines.append(f"| {row['case_id']} | {row['comparison_basis']} | {row['before']} | {row['after']} | "
                         f"{row['primary_before']}/{row['primary_after']} | {row['elapsed_ms_delta']} |")
        for row in report['cases']:
            if row['comparison_basis'] == 'same_task_different_budget_recipes':
                lines.extend(['', f"{row['case_id']} declared budgets: before {row['before_budget']}; "
                              f"after {row['after_budget']}. This is not a same-budget comparison."])
    else:
        lines = ['# Model qualification observations', '',
            f"Mode: {report['evidence_mode']}. Declared cases: {report['declared']}.",
            'Coverage is limited to the listed cases and consumer boundaries. Unknown observations remain unknown.', '',
            '| Role | Boundary | All cases | Outcomes | Primary passes | Durations by outcome (median ms, samples) |',
            '| --- | --- | --- | --- | --- | --- |']
        for group in report['groups']:
            lines.append(f"| {group['role']} | {group['boundary']} | {group['declared']} | {group['outcomes']} | "
                f"{group['primary_passes']} | {group['duration_by_outcome']} |")
    datasets = [('Before case checks', report['before']), ('After case checks', report['after'])] \
        if report['kind'] == 'comparison' else [('Case checks', report)]
    for title, dataset in datasets:
        lines.extend(['', f'## {title}', '',
            'Checks describe the listed case only. Unknown and unrecorded checks do not pass. '
            'Primary attribution remains separate from the observed checks.', '',
            '| Case | Role | Boundary | Outcome | Primary | Checks |',
            '| --- | --- | --- | --- | --- | --- |'])
        for row in dataset['cases']:
            cells = (row['case_id'], row['role'], row['boundary'], row['outcome'],
                     row.get('primary_outcome', 'unverified'), _check_text(row.get('checks')))
            lines.append('| ' + ' | '.join(str(cell).replace('|', '\\|').replace('\n', ' ')
                                         for cell in cells) + ' |')
    return '\n'.join(lines) + '\n'
