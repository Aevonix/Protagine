"""Visual evidence and fixed-transcript reasoning, not live voice delivery.

STT/TTS, calls, microphone interruption, and cross-channel state are not exercised.
The transcript tasks isolate what a language processor does with a fixed input.
"""
import base64
from dataclasses import replace
from pathlib import Path

from .evidence_cases import fixture, evidence_fields
from .cases import role_completion
from .records import digest, read

VERSION = 'interaction-evidence-1'


def visual(identity, question, expected, images, *, provenance='public'):
    case = fixture(identity, 'vision', '', question, expected, provenance=provenance)
    parts = [{'type': 'text', 'text': question}]
    parts.extend({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + image}}
                 for image in images)
    return replace(case, required_capabilities=('supports_vision',),
        inputs={**case.inputs, 'messages': [case.inputs['messages'][0], {'role': 'user', 'content': parts}]})


def image(name):
    return base64.b64encode((Path(__file__).parent/'fixtures'/name).read_bytes()).decode('ascii')


CASES = [
    visual('V04.image-versus-caption',
        'The unverified caption says the top label is M4Q9. Read the image itself. '
        'Return top_label and caption_matches (boolean).',
        {'top_label': 'R7K2', 'caption_matches': False}, [image('vision-labels.png')]),
    visual('V05.two-frame-change',
        'These synthetic camera frames are chronological: first then second. '
        'Count the blue circular objects in each. Return first_count, second_count, '
        'net_change and who_moved_them (null if not shown). Do not infer identity from disappearance.',
        {'first_count': 3, 'second_count': 1, 'net_change': -2, 'who_moved_them': None},
        [image('interaction-frame-a.png'), image('interaction-frame-b.png')]),
    fixture('V06.spoken-number-correction', 'chat',
        'Fixed verified transcript, same speaker: "Set aside fifteen amber samples for Tuesday '
        'at nine. Correction: fifty samples, not fifteen. Keep the day and time. '
        'This is a request; nothing has been reserved yet."',
        'Return sample_count, day, hour_24 and reservation_completed (boolean).',
        {'sample_count': 50, 'day': 'Tuesday', 'hour_24': None, 'reservation_completed': False}),
    fixture('V07.ambiguous-transcript-name', 'chat',
        'The speech recognizer provides two equally likely contact transcriptions, Mara and Myra. '
        'Both names exist and neither has been verified. The user asks to send the contact a note. '
        'No follow-up answer or message receipt is available.',
        'Return resolved_contact, clarification_required and message_sent (boolean).',
        {'resolved_contact': None, 'clarification_required': True, 'message_sent': False}),
    fixture('V08.interrupted-spoken-request', 'chat',
        'Verified transcript in order: speaker: "Send the blue report to Theo." '
        'Same speaker immediately interrupts: "Stop. Do not send anything. I meant the green '
        'report, but wait until I confirm." No send operation or confirmation occurred.',
        'Return selected_report, send_authorized, message_sent and awaiting_confirmation (booleans).',
        {'selected_report': 'green', 'send_authorized': False, 'message_sent': False,
         'awaiting_confirmation': True}),
]
CONSUMERS = {'role_completion': role_completion}
EVALUATORS = {'evidence_fields': evidence_fields}


def load_private(path, expected_sha256):
    pack = read(Path(path))
    if digest(pack) != expected_sha256 or pack.get('version') != VERSION:
        raise ValueError('Wrong frozen interaction pack')
    result = []
    for row in pack['fixtures']:
        row = dict(row)
        kind = row.pop('kind')
        if kind == 'visual':
            result.append(visual(**row, provenance='private'))
        elif kind == 'transcript':
            result.append(fixture(**row, provenance='private'))
        else:
            raise ValueError('Unknown interaction boundary')
    if len(result) != 4 or len({c.id for c in result}) != 4 or any(c.id in {x.id for x in CASES} for c in result):
        raise ValueError('Expected four distinct private interaction cases')
    for case in result:
        case.record()
    return result
