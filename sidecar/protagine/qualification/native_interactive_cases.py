"""Distinct native task transitions, separate from physical transport/performance."""
from copy import deepcopy
from .records import CaseSpec, read

VERSION = 'native-interactive-transitions-v1'
SYSTEM = ('Use actual shared task tools and observed receipts. Do not create replacements, '
          'invent results or treat acceptance as completion or outward delivery. '
          'Return only the JSON fields requested. Use null when the evidence is unavailable.')

ALPHA = {'name': 'alpha', 'marker': 'BENCH_TASK_ALPHA', 'code': 'cedar-462', 'units': 3}
BETA = {'name': 'beta', 'marker': 'BENCH_TASK_BETA', 'code': 'quartz-813', 'units': 7}

DEVELOPMENT = {'version': VERSION, 'split': 'development', 'cases': [
    {'id': 'U04.completed-result-fresh-session', 'scenario': 'completed_result', 'tasks': [ALPHA],
     'question': 'Inspect the retained alpha task from the other conversation. Return its label and units.',
     'answer': {'label': 'cedar-462', 'units': 3}, 'results': {'alpha': {'label': 'cedar-462', 'units': 3}}},
    {'id': 'U05.independent-native-roots', 'scenario': 'two_roots', 'tasks': [ALPHA, BETA],
     'question': 'Inspect the two retained background tasks. Return task_count and distinct_tasks.',
     'answer': {'task_count': 2, 'distinct_tasks': True},
     'results': {'alpha': {'label': 'cedar-462', 'units': 3}, 'beta': {'label': 'quartz-813', 'units': 7}}},
    {'id': 'U06.cancel-only-selected-sibling', 'scenario': 'cancel_sibling', 'tasks': [ALPHA, BETA],
     'question': 'Stop only alpha. Keep beta running. Return stopped_alpha and stopped_beta.',
     'answer': {'stopped_alpha': True, 'stopped_beta': False},
     'results': {'beta': {'label': 'quartz-813', 'units': 7}}, 'stopped': ['alpha']},
    {'id': 'U07.stale-generation-resume', 'scenario': 'stale_resume', 'tasks': [ALPHA],
     'question': 'Test the existing alpha task resume fence once: request resume with expected_turn_id="obsolete-fixture-turn". Do not replace it. Report resumed (boolean) from the actual receipt.',
     'answer': {'resumed': False}, 'results': {'alpha': {'label': 'cedar-462', 'units': 3}}},
    {'id': 'U08.resume-retained-failed-task', 'scenario': 'failed_resume', 'tasks': [ALPHA],
     'question': 'Inspect alpha. If it failed, resume that same task using the native_turn_id just observed. Do not submit a replacement. Return resume_requested.',
     'answer': {'resume_requested': True}, 'results': {'alpha': {'label': 'cedar-462', 'units': 3}}},
    {'id': 'X02.useful-foreground-with-two-workers', 'scenario': 'short_question', 'tasks': [ALPHA, BETA],
     'question': 'While the background tasks continue, what is 19 plus 23? Return sum. Do not wait, inspect or change those tasks.',
     'answer': {'sum': 42},
     'results': {'alpha': {'label': 'cedar-462', 'units': 3}, 'beta': {'label': 'quartz-813', 'units': 7}}},
    {'id': 'X03.ordered-corrections', 'scenario': 'ordered_steer', 'tasks': [ALPHA],
     'prior_turns': ['Steer the existing alpha task: change its label to indigo-254 and preserve units=3. Do not submit another task. Return correction_requested.'],
     'question': 'Steer alpha again: replace the earlier label with violet-975, preserving units=3. Return correction_requested.',
     'answer': {'correction_requested': True}, 'results': {'alpha': {'label': 'violet-975', 'units': 3}},
     'observed_markers': ['indigo-254', 'violet-975'], 'updates': {'alpha': 2}},
    {'id': 'X04.steer-only-selected-sibling', 'scenario': 'targeted_steer', 'tasks': [ALPHA, BETA],
     'question': 'Steer alpha to use label moss-628, keeping units=3. Leave beta unchanged. Return alpha_corrected and beta_changed.',
     'answer': {'alpha_corrected': True, 'beta_changed': False},
     'results': {'alpha': {'label': 'moss-628', 'units': 3}, 'beta': {'label': 'quartz-813', 'units': 7}},
     'observed_markers': ['moss-628'], 'updates': {'alpha': 1, 'beta': 0}},
    {'id': 'X05.terminal-handoff-keeps-worker', 'scenario': 'terminal_handoff', 'tasks': [ALPHA],
     'question': 'The previous conversation handed off alpha. Inspect the retained task. Return task_exists and start_replacement.',
     'answer': {'task_exists': True, 'start_replacement': False},
     'results': {'alpha': {'label': 'cedar-462', 'units': 3}}},
    {'id': 'X06.completion-during-conversation', 'scenario': 'completion_during_foreground', 'tasks': [ALPHA],
     'question': 'Check alpha with its actual status tool now. Return label and units only from a completed retained result, otherwise null.',
     'answer': {'label': 'cedar-462', 'units': 3}, 'results': {'alpha': {'label': 'cedar-462', 'units': 3}}},
    {'id': 'X07.verified-cross-channel-steering', 'scenario': 'channel_steer', 'tasks': [ALPHA],
     'question': 'From this conversation, steer the alpha task started over SMS: use label amber-536 and preserve units=3. Return correction_requested.',
     'answer': {'correction_requested': True}, 'results': {'alpha': {'label': 'amber-536', 'units': 3}},
     'observed_markers': ['amber-536'], 'updates': {'alpha': 1}},
]}


def cases(pack=None):
    pack = deepcopy(DEVELOPMENT if pack is None else read(pack) if not isinstance(pack, dict) else pack)
    if pack.get('version') != VERSION or pack.get('split') not in {'development', 'held_out'}:
        raise ValueError('Unknown native interactive pack')
    rows = pack['cases']
    if not 1 <= len(rows) <= 20 or len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Declare distinct bounded task scenarios')
    results = []
    for row in rows:
        tasks = deepcopy(row['tasks'])
        if not 1 <= len(tasks) <= 2 or len({item['name'] for item in tasks}) != len(tasks):
            raise ValueError('Declare one or two distinct native tasks')
        for item in tasks:
            work = (f"{item['marker']}: Prepare a synthetic inventory result. Use label {item['code']} "
                    f"and units={item['units']}. Return only JSON with label and units. "
                    'Do not access external systems or create another task.')
            mode = row['scenario']
            action = ('Use protagine_task handoff to run this complete request in the background: '
                      if mode == 'terminal_handoff' else 'Use protagine_task submit to run this complete request in the background: ')
            if mode == 'existing_handoff':
                ending = ' Then use handoff with only the task_id just accepted, in this same turn. Do not submit again.'
            elif mode == 'duplicate_submit':
                ending = ' Repeat the identical submit call once in this same turn to test idempotence, then return.'
            else:
                ending = ' Return immediately after acceptance. Do not poll or finish the work here.'
            item.update(instruction=work, bootstrap=action+work+ending)
        results.append(CaseSpec(id=row['id'], version=VERSION, role='chat', boundary='native_hermes',
            consumer='native_interactive', evaluator='native_interactive_outcomes', timeout_seconds=600,
            max_output_bytes=1048576, provenance='private' if pack['split']=='held_out' else 'public',
            inputs={'role': 'chat', 'scenario': row['scenario'], 'tasks': tasks, 'system': SYSTEM,
                'contact_id': 'replaced-by-verified-fixture-owner', 'turns': [], 'cleanup_seconds': 30,
                'max_output_tokens': 1536, 'prior_turns': row.get('prior_turns', []),
                'observed_markers': row.get('observed_markers', []),
                'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': row['question']}]},
            oracle={'scenario': row['scenario'], 'answer': row['answer'], 'results': row.get('results', {}),
                'task_names': [item['name'] for item in tasks], 'stopped': row.get('stopped', []),
                'updates': row.get('updates', {}), 'observed_markers': row.get('observed_markers', [])}))
    return results
