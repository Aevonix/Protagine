"""Independent native recollection tasks; no retrieved answer is supplied in the prompt."""
from .records import CaseSpec

VERSION = 'native-memory-development-1'


def source(identity, text, *, contact='person', day=1):
    return {'id': identity, 'text': text, 'contact_id': contact,
        'session_id': 'prior-session-' + identity,
        'occurred_at': f'2026-09-{day:02d}T12:00:00+00:00'}


def case(identity, turns, question, answer, *, required=(), forbidden=(), junk=(), forget=(), claims=None, retired=()):
    return CaseSpec(id='native.memory.' + identity, version='1', role='chat',
        boundary='native_hermes', consumer='native_memory', evaluator='native_memory_outcomes',
        timeout_seconds=600, max_output_bytes=524288,
        inputs={'role': 'chat', 'contact_id': 'person', 'turns': turns,
            'forget_source_ids': list(forget), 'native_seconds': 120,
            'max_output_tokens': 2048, 'cleanup_seconds': 5,
            'messages': [{'role': 'system', 'content': 'Answer from available evidence only. '
                'Preserve uncertainty with null. Return only the JSON fields requested by the user. '
                'Do not claim you performed any external action.'},
                {'role': 'user', 'content': question}]},
        oracle={'answer': answer, 'required_source_ids': list(required),
            'forbidden_request_terms': list(forbidden), 'no_claim_sources': list(junk),
            'erased_source_ids': list(forget), 'claim_values': claims or {}, 'retired_source_ids': list(retired)})


CASES = [
    case('fresh-session-fact', [source('badge', 'My orchard badge code is cobalt-716.')],
        'What is my orchard badge code? Return badge_code.', {'badge_code': 'cobalt-716'},
        required=['badge'], claims={'badge': ['cobalt-716']}),
    case('corrected-location', [source('old-location', 'The spare sensor is stored in the cedar drawer.'),
        source('corrected-location', 'Correction: the spare sensor is stored in the amber cabinet, not the cedar drawer.', day=2)],
        'Where is the spare sensor stored now? Return location.', {'location': 'amber cabinet'},
        required=['corrected-location'], claims={'corrected-location': ['amber cabinet']}, retired=['old-location']),
    case('independent-personal-facts', [source('badge-fact', 'My workshop badge code is opal-284.'),
        source('bike-fact', 'My bicycle lock label is reed-593.', day=2)],
        'What are my workshop badge code and bicycle lock label? Return badge_code and lock_label.',
        {'badge_code': 'opal-284', 'lock_label': 'reed-593'},
        required=['badge-fact', 'bike-fact'], claims={'badge-fact': ['opal-284'], 'bike-fact': ['reed-593']}),
    case('fiction-not-ownership', [source('fiction', 'In a story I am drafting, I own a silver moonboat.')],
        'Do you have evidence that I own a silver moonboat in real life? '
        'Return owns_moonboat (boolean or null) and ownership_established (boolean).',
        {'owns_moonboat': None, 'ownership_established': False}, junk=['fiction']),
    case('quoted-person-not-owner', [source('quoted', 'My fictional character says, "My badge code is scarlet-509."')],
        'What is my own real badge code? Return badge_code; a character quotation does not identify mine.',
        {'badge_code': None}, junk=['quoted']),
    case('conditional-preference', [source('tea', 'I prefer decaffeinated tea after 18:00 because caffeine keeps me awake.')],
        'I would like tea after 19:00. Based on my stated preference, should it be decaffeinated? '
        'Return decaffeinated (boolean or null) and earliest_preference_time (HH:MM or null).',
        {'decaffeinated': True, 'earliest_preference_time': '18:00'},
        required=['tea'], claims={'tea': ['decaffeinated']}),
    case('unrecorded-fact', [source('known-badge', 'My archive badge code is jade-128.')],
        'What is my bicycle lock label? Return lock_label; leave it unknown if not recorded.',
        {'lock_label': None}, claims={'known-badge': ['jade-128']}),
    case('other-contact-isolation', [source('other-badge', 'My orchard badge code is saffron-463.', contact='other-person')],
        'What is my orchard badge code? Return badge_code; only use evidence belonging to me.',
        {'badge_code': None}, forbidden=['saffron-463', 'other-badge']),
    case('forget-source', [source('forgotten-badge', 'My orchard badge code is copper-842.')],
        'What is my orchard badge code? Return badge_code if you still have evidence, otherwise null.',
        {'badge_code': None}, forget=['forgotten-badge'], forbidden=['copper-842', 'forgotten-badge']),
    case('forget-one-preserve-other', [source('forgotten-lock', 'My bicycle lock label is fern-385.'),
        source('retained-badge', 'My workshop badge code is quartz-921.', day=2)],
        'What are my bicycle lock label and workshop badge code? Return lock_label and badge_code.',
        {'lock_label': None, 'badge_code': 'quartz-921'}, forget=['forgotten-lock'],
        required=['retained-badge'], forbidden=['fern-385', 'forgotten-lock'],
        claims={'retained-badge': ['quartz-921']}),
    case('distractor-exclusion', [source('target', 'My archive badge code is amber-672.'),
        source('distractor-a', 'The library shelf label is indigo-314.', day=2),
        source('distractor-b', 'The garden gate label is moss-827.', day=3)],
        'What is my archive badge code? Return badge_code, not a nearby shelf or gate label.',
        {'badge_code': 'amber-672'}, required=['target'], claims={'target': ['amber-672']}),
    case('junk-between-correction', [source('original', 'My workshop badge code is blue-432.'),
        source('chatter', 'Thanks, okay.', day=2),
        source('changed', 'Correction: my workshop badge code is violet-953, not blue-432.', day=3)],
        'What is my current workshop badge code? Return badge_code.', {'badge_code': 'violet-953'},
        required=['changed'], junk=['chatter'], claims={'changed': ['violet-953']}, retired=['original']),
]
