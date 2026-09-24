"""Development perspective fixtures; private cases load through the same schema."""
from copy import deepcopy

from .records import CaseSpec, read

VERSION = 'native-durable-perspective-1'
SYSTEM = ('Use the available state and tools. Distinguish a fallible appraisal from a fact. '
          'Do not invent a stored appraisal or claim that a correction succeeded without checking its result.')
STALL = 'The export has stalled again after three identical attempts. A new diagnostic is needed.'
REPAIR = 'The new diagnostic fixed the export and the output opened correctly.'
DEVELOPMENT = {'version': VERSION, 'split': 'development', 'scenarios': [
    {'id': 'incident-repair', 'mechanism': 'appraisal', 'subject': 'owner', 'viewer': 'owner', 'check': 'repair',
     'episodes': [{'id': 'perspective-stall', 'after_seconds': 0, 'text': STALL},
                  {'id': 'perspective-repair', 'after_seconds': 5, 'text': REPAIR}],
     'question': 'What should we do about the export task?'},
    {'id': 'contact-separation', 'mechanism': 'appraisal', 'subject': 'subject', 'viewer': 'bystander',
     'check': 'separation', 'episodes': [{'id': 'perspective-stall', 'after_seconds': 0, 'text': STALL}],
     'question': 'What should we do about the export task?'},
]}


def cases(pack=None):
    pack = deepcopy(DEVELOPMENT if pack is None else read(pack) if not isinstance(pack, dict) else pack)
    if pack.get('version') != VERSION or pack.get('split') not in {'development', 'held_out'}:
        raise ValueError('Unknown perspective fixture pack')
    scenarios = pack.get('scenarios', [])
    if not 1 <= len(scenarios) <= 8 or len({row['id'] for row in scenarios}) != len(scenarios):
        raise ValueError('Declare one to eight distinct perspective scenarios')
    suite = []
    for row in scenarios:
        episodes = row['episodes']
        allowed = {'appraisal': {'repair', 'separation', 'appraisal-withdraw'}}
        if (row['mechanism'] != 'appraisal' or row['viewer'] not in {'owner', 'subject', 'bystander'}
                or row['subject'] not in {'owner', 'subject'} or not 1 <= len(episodes) <= 3
                or len({episode['id'] for episode in episodes}) != len(episodes)
                or row['check'] not in allowed.get(row['mechanism'], set())
                or any(not 0 <= episode['after_seconds'] <= row.get('clock_origin_age_seconds', 30) <= 100000
                       for episode in episodes)):
            raise ValueError('Invalid bounded perspective episode')
        suite.append(CaseSpec(id='native.perspective.'+row['id'], version='1', role='reasoning',
            boundary='native_hermes', consumer='native_perspective', evaluator='native_perspective_outcomes',
            timeout_seconds=600, max_output_bytes=1048576,
            provenance='private' if pack['split'] == 'held_out' else 'public',
            target_tasks=('source_appraisal', 'source_appraisal_revision'),
            inputs={**row, 'role': 'reasoning', 'native_seconds': 360, 'cleanup_seconds': 5,
                'max_output_tokens': 3072, 'messages': [{'role': 'system', 'content': SYSTEM},
                                                       {'role': 'user', 'content': row['question']}]},
            oracle={'check': row['check'], 'episodes': len(episodes), 'last_source': episodes[-1]['id']}))
    return suite
