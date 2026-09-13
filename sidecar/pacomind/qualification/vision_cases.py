"""Image-grounded role checks, not native recall or general vision qualification."""
import base64
from pathlib import Path

from .records import CaseSpec


def _messages(fixture, question):
    image = (Path(__file__).parent / 'fixtures' / fixture).read_bytes()
    return [
        {'role': 'system', 'content': (
            'Answer from the supplied image only. Return one JSON object without '
            'markdown or extra prose. Use null for information that cannot be '
            'determined from the visible image.')},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': question},
            {'type': 'image_url', 'image_url': {
                'url': 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')}},
        ]},
    ]


# Expected fields are specified independently, not calculated from image pixels
# or supplied in the request. Case records retain the exact image-bearing input.
CASES = [
    CaseSpec(id='vision.spatial-arrangement', version='1', role='vision',
        boundary='role_completion', consumer='role_completion', evaluator='json_fields',
        required_capabilities=('supports_vision',), max_output_bytes=16384,
        inputs={'role': 'vision', 'max_output_tokens': 768, 'messages': _messages(
            'vision-arrangement.png',
            'Identify the red, blue and yellow objects. Return red_shape, red_position, '
            'blue_shape, blue_position, yellow_shape, yellow_position and arrow_direction. '
            'Use circle, triangle or square for shape; upper_left, upper_right, lower_left '
            'or lower_right for position; left, right, up or down for the black arrow direction.')},
        oracle={'fields': [
            {'name': 'red_shape', 'path': ['output', 'red_shape'], 'equals': 'circle'},
            {'name': 'red_position', 'path': ['output', 'red_position'], 'equals': 'upper_right'},
            {'name': 'blue_shape', 'path': ['output', 'blue_shape'], 'equals': 'triangle'},
            {'name': 'blue_position', 'path': ['output', 'blue_position'], 'equals': 'lower_left'},
            {'name': 'yellow_shape', 'path': ['output', 'yellow_shape'], 'equals': 'square'},
            {'name': 'yellow_position', 'path': ['output', 'yellow_position'], 'equals': 'lower_right'},
            {'name': 'arrow_direction', 'path': ['output', 'arrow_direction'], 'equals': 'left'},
        ]}),
    CaseSpec(id='vision.legible-labels', version='1', role='vision',
        boundary='role_completion', consumer='role_completion', evaluator='json_fields',
        required_capabilities=('supports_vision',), max_output_bytes=16384,
        inputs={'role': 'vision', 'max_output_tokens': 768, 'messages': _messages(
            'vision-labels.png',
            'Read the three four-character labels from top to bottom. '
            'Return keys top, middle and bottom with the exact visible strings.')},
        oracle={'fields': [
            {'name': 'top_label', 'path': ['output', 'top'], 'equals': 'R7K2'},
            {'name': 'middle_label', 'path': ['output', 'middle'], 'equals': 'M4Q9'},
            {'name': 'bottom_label', 'path': ['output', 'bottom'], 'equals': 'B6T3'},
        ]}),
    CaseSpec(id='vision.covered-and-unknown', version='1', role='vision',
        boundary='role_completion', consumer='role_completion', evaluator='json_fields',
        required_capabilities=('supports_vision',), max_output_bytes=16384,
        inputs={'role': 'vision', 'max_output_tokens': 768, 'messages': _messages(
            'vision-covered-label.png',
            'Read the exposed top label and the lower label under the opaque cover. '
            'Also identify the owner and capture time if visible. Return keys '
            'visible_label, covered_label, owner and capture_time.')},
        oracle={'fields': [
            {'name': 'visible_label', 'path': ['output', 'visible_label'], 'equals': 'P8N5'},
            {'name': 'covered_label_unknown', 'path': ['output', 'covered_label'], 'equals': None},
            {'name': 'owner_unknown', 'path': ['output', 'owner'], 'equals': None},
            {'name': 'capture_time_unknown', 'path': ['output', 'capture_time'], 'equals': None},
        ]}),
]
