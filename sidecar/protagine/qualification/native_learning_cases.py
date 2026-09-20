"""Four development transfer scenarios, with matched system-contribution arms."""
from .records import CaseSpec

VERSION = 'native-procedure-transfer-development-1'
ARMS = ('protagine', 'base_hermes', 'recall_disabled')
SYSTEM = ('Answer from available evidence only. If the named local procedure is not recorded, '
          'use null rather than guessing. Return only the JSON fields requested. '
          'Do not claim to perform an external action.')


def scenario(identity, lesson, baseline, baseline_answer, transfer, answer, terms, baseline_goal):
    return [CaseSpec(id=f'native.learning.{identity}.{arm}', version='1', role='chat',
        boundary='native_hermes', consumer='native_learning', evaluator='native_learning_outcomes',
        timeout_seconds=600, max_output_bytes=1048576,
        inputs={'role': 'chat', 'scenario': identity, 'arm': arm, 'contact_id': 'person',
            'turns': [], 'forget_source_ids': [], 'native_seconds': 480,
            'cleanup_seconds': 5, 'max_output_tokens': 2048,
            'baseline_question': baseline,
            'feedback': lesson,
            'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': transfer}]},
        oracle={'arm': arm, 'baseline_answer': baseline_answer, 'answer': answer,
            'baseline_goal_answer': baseline_goal,
            'unknown_answer': {key: None for key in answer}, 'lesson_terms': terms})
        for arm in ARMS]


CASES = [
    *scenario('intake-procedure',
        'For workshop intake tickets, use the amber tray if the seal is cracked. '
        'Use the cobalt tray if the seal is intact. Ask for clarification if the seal status is unknown.',
        'Workshop intake ticket 17 has an intact seal. Which tray does our local procedure require? Return tray.',
        {'tray': None},
        'Workshop intake ticket 93 has a cracked seal. Which tray does our local procedure require? Return tray.',
        {'tray': 'amber'}, ['workshop intake', 'amber', 'cobalt'], {'tray': 'cobalt'}),
    *scenario('exception-precedence',
        'For archive review, use the silver rack for an item with a signed label. '
        'Use the cedar rack for an item without a signed label. '
        'Exception: use the quarantine rack for every item with a torn wrapper, regardless of its label.',
        'Archive item 28 has an unsigned label and an intact wrapper. Which rack does our procedure require? Return rack.',
        {'rack': None},
        'Archive item 61 has a signed label and a torn wrapper. Which rack does our procedure require? Return rack.',
        {'rack': 'quarantine'}, ['archive review', 'silver', 'quarantine'], {'rack': 'cedar'}),
    *scenario('computed-transfer',
        'For the lab parcel checklist, reserve two spare labels per parcel plus one spare label for the complete batch. '
        'Count parcels, not the number of items inside each parcel.',
        'Our lab parcel checklist covers a batch of 3 parcels containing 11 items. How many spare labels are required? Return labels.',
        {'labels': None},
        'Our lab parcel checklist covers a batch of 7 parcels containing 23 items. How many spare labels are required? Return labels.',
        {'labels': 15}, ['lab parcel', 'two spare labels per parcel', 'one spare label for the complete batch'], {'labels': 7}),
    *scenario('negative-transfer',
        'For greenhouse sensor alerts only, route an intermittent fault to the fern bench. '
        'Route a continuous fault to the quartz bench. This greenhouse procedure does not govern freezer sensors.',
        'A greenhouse sensor has a continuous fault. Which bench does our local procedure require? Return bench.',
        {'bench': None},
        'A freezer sensor has an intermittent fault. Which bench does our local procedure require? Return bench.',
        {'bench': None}, ['greenhouse sensor', 'fern', 'quartz'], {'bench': 'quartz'}),
]
