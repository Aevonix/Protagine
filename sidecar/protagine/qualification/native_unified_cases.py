"""Development scenarios for real shared work and native task control."""
from copy import deepcopy
from dataclasses import replace
from .records import CaseSpec

VERSION = 'native-unified-work-v2'


def case(identifier, scenario, question, answer):
    instruction = ('Prepare a synthetic workshop inventory summary. Its label must be amber-462 '
        'and its unit count must be 3. Return only JSON with label and units. '
        'Do not access external systems or create other tasks.')
    return CaseSpec(id='unified.'+identifier, version=VERSION, role='chat', boundary='native_hermes',
        consumer='native_unified', evaluator='native_unified_outcomes', timeout_seconds=540,
        max_output_bytes=1048576, inputs={'role': 'chat', 'scenario': scenario,
            'contact_id': 'fixture-owner', 'turns': [], 'cleanup_seconds': 30,
            'max_output_tokens': 1536, 'worker_instruction': instruction,
            'commitment': 'Prepare the synthetic workshop inventory summary.',
            'bootstrap': ('Use protagine_commitment_work to claim the existing workshop inventory commitment, '
                'then use protagine_task submit to run this task in the background: '+instruction+
                ' Return promptly after actual acceptance. Do not complete the inventory here.'),
            'messages': [{'role': 'system', 'content': ('Use shared work and actual tool receipts. '
                'Do not start duplicate work. A pending operation is not a completed operation. '
                'Return only the requested JSON fields; use null for unknowns.')},
                {'role': 'user', 'content': question}]},
        oracle={'scenario': scenario, 'answer': answer, 'original_label': 'amber-462',
                'revised_label': 'violet-953', 'worker_units': 3})


CASES = [
    case('shared-commitment', 'shared',
         'What is happening with the workshop inventory task started in another conversation? '
         'Return background_task_exists, commitment_pending and start_duplicate (booleans or null).',
         {'background_task_exists': True, 'commitment_pending': True, 'start_duplicate': False}),
    case('foreground-during-worker', 'foreground',
         'Give me the current workshop inventory task status without waiting for it to finish. '
         'Return background_task_exists, result_completed and start_duplicate (booleans or null).',
         {'background_task_exists': True, 'result_completed': False, 'start_duplicate': False}),
    case('cross-session-steer', 'steer',
         'Change the running workshop inventory task: use label violet-953 instead of amber-462. '
         'Keep its unit count at 3. Apply the correction to the existing task; do not submit another. '
         'Return correction_requested (boolean), and worker_already_applied (boolean or null) '
         'only when you have an observed result.',
         {'correction_requested': True, 'worker_already_applied': None}),
    case('stop-blocks-stale-delivery', 'stop',
         'Stop the workshop inventory task from the other conversation. Do not start a replacement. '
         'Return stop_requested and replacement_started (booleans or null).',
         {'stop_requested': True, 'replacement_started': False}),
]


def cases(*, arm='protagine'):
    if arm == 'protagine':
        return deepcopy(CASES)
    if arm != 'base_hermes':
        raise ValueError('Unknown unified work comparison arm')
    results = []
    for original in deepcopy(CASES[:2]):
        answer = {key: False if key == 'start_duplicate' else None for key in original.oracle['answer']}
        results.append(replace(original, id=original.id+'.base-hermes',
            inputs={**original.inputs, 'arm': arm}, oracle={**original.oracle, 'arm': arm, 'answer': answer}))
    return results
