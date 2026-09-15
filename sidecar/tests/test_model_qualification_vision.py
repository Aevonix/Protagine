"""Actual image request plumbing and independent field oracles; no model calls."""
import base64
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from protagine.qualification.cases import json_fields, select_cases
from protagine.qualification.cli import run
from protagine.qualification.records import digest, read
from protagine.qualification.vision_cases import CASES
from test_function_routing import config, endpoint


# Authored independently of the CaseSpec oracle and checked against viewed PNGs.
ANSWERS = {
    'vision.spatial-arrangement': {
        'red_shape': 'circle', 'red_position': 'upper_right',
        'blue_shape': 'triangle', 'blue_position': 'lower_left',
        'yellow_shape': 'square', 'yellow_position': 'lower_right',
        'arrow_direction': 'left'},
    'vision.legible-labels': {'top': 'R7K2', 'middle': 'M4Q9', 'bottom': 'B6T3'},
    'vision.covered-and-unknown': {
        'visible_label': 'P8N5', 'covered_label': None, 'owner': None, 'capture_time': None},
}


def _image(messages):
    parts = messages[-1]['content']
    assert len(parts) == 2 and parts[0]['type'] == 'text'
    assert parts[1]['type'] == 'image_url'
    url = parts[1]['image_url']['url']
    assert url.startswith('data:image/png;base64,')
    return base64.b64decode(url.split(',', 1)[1], validate=True)


def _args(tmp_path, cfg, binding='deliberate'):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(cfg))
    return SimpleNamespace(models_command='evaluate', binding=binding, config=path,
        roles='vision', suite='standard', output=tmp_path/'run', resume=False,
        evidence_mode='controlled')


def test_vision_registry_uses_pixels_and_declares_capability_without_changing_defaults():
    assert select_cases(['vision']) == CASES
    assert [case.id for case in select_cases(['chat', 'extraction'])] == [
        'chat.grounded-note', 'extraction.conditions',
        'memory.formation-quality', 'memory.corrected-recollection']
    fixture_names = ['vision-arrangement.png', 'vision-labels.png', 'vision-covered-label.png']
    fixture_root = Path(__file__).parents[1] / 'protagine/qualification/fixtures'
    for case, filename in zip(CASES, fixture_names):
        assert case.role == case.inputs['role'] == 'vision'
        assert case.boundary == case.consumer == 'role_completion'
        assert case.evaluator == 'json_fields'
        assert case.required_capabilities == ('supports_vision',)
        assert case.timeout_seconds == 60 and case.max_output_bytes == 16384
        assert case.inputs['max_output_tokens'] == 768 and case.target_tasks == ()
        raw = _image(case.inputs['messages'])
        assert raw == (fixture_root / filename).read_bytes()
        with Image.open(BytesIO(raw)) as image:
            assert image.format == 'PNG' and image.mode == 'RGB'
            assert image.width == 720 and image.height in (420, 480)
            assert image.info == {}  # no alternate text or hidden answer metadata
        text = case.inputs['messages'][0]['content'] + case.inputs['messages'][1]['content'][0]['text']
        assert not any(label in text for label in ('R7K2', 'M4Q9', 'B6T3', 'P8N5'))
        assert 'oracle' not in case.inputs
        assert case.record()['inputs_sha256'] != case.record()['oracle_sha256']


@pytest.mark.parametrize('case', CASES, ids=lambda case: case.id)
def test_independent_visual_answers_pass(case):
    checks = json_fields({'output': json.dumps(ANSWERS[case.id])}, case.oracle)
    assert checks and all(checks.values())


@pytest.mark.parametrize('case_id,field,wrong,check', [
    ('vision.spatial-arrangement', 'red_position', 'upper_left', 'red_position'),
    ('vision.spatial-arrangement', 'blue_shape', 'circle', 'blue_shape'),
    ('vision.spatial-arrangement', 'arrow_direction', 'right', 'arrow_direction'),
    ('vision.legible-labels', 'top', 'R7K3', 'top_label'),
    ('vision.legible-labels', 'middle', 'M409', 'middle_label'),
    ('vision.legible-labels', 'bottom', None, 'bottom_label'),
    ('vision.covered-and-unknown', 'visible_label', None, 'visible_label'),
    ('vision.covered-and-unknown', 'covered_label', 'P8N5', 'covered_label_unknown'),
    ('vision.covered-and-unknown', 'owner', 'A researcher', 'owner_unknown'),
    ('vision.covered-and-unknown', 'capture_time', 'today', 'capture_time_unknown'),
    ('vision.covered-and-unknown', 'capture_time', 'null', 'capture_time_unknown'),
])
def test_wrong_visual_readings_and_invented_unknowns_fail(case_id, field, wrong, check):
    case = next(case for case in CASES if case.id == case_id)
    answer = deepcopy(ANSWERS[case_id])
    answer[field] = wrong
    checks = json_fields({'output': json.dumps(answer)}, case.oracle)
    assert checks[check] is False and not all(checks.values())


def test_real_cli_router_transmits_images_and_records_candidate_output_identity_and_usage(tmp_path, capsys):
    cap = 6144

    def answer(payload):
        case = next(case for case in CASES if case.inputs['messages'] == payload['messages'])
        assert _image(payload['messages']) == _image(case.inputs['messages'])
        frozen = next(row for row in read(tmp_path/'run/run.json')['cases'] if row['id'] == case.id)
        assert frozen['inputs']['messages'] == payload['messages']
        assert frozen['inputs']['max_output_tokens'] == cap
        assert frozen['version'] == '1-configured-output-v1'
        assert payload.get('max_tokens', payload.get('max_completion_tokens')) == cap
        assert 'oracle' not in payload and 'fields' not in payload
        return json.dumps(ANSWERS[case.id])

    with endpoint(content=answer) as (url, calls):
        cfg = config(url, url)
        cfg['modelPool']['deliberate']['maxTokens'] = cap
        args = _args(tmp_path, cfg)
        original = args.config.read_bytes()
        assert run(args) == 0
        assert len(calls) == 3 and args.config.read_bytes() == original
        manifest = read(args.output/'run.json')
        assert manifest['evidence_mode'] == 'controlled'
        assert manifest['recipe']['declared']['supports_vision'] is True
        for case in CASES:
            result = read(args.output/'attempts'/case.id/'result.json')
            assert result['outcome'] == result['primary_outcome'] == 'pass'
            assert result['output'] == json.dumps(ANSWERS[case.id])
            assert result['cleanup'] == 'state_directory_removed'
            assert len(result['observations']) == 1
            observed = result['observations'][0]
            assert observed['input_sha256'] == digest(case.inputs['messages'])
            assert observed['role'] == 'vision' and observed['binding_purpose'] == 'target'
            assert observed['selected_binding'] == 'deliberate'
            assert observed['configured_model'] == 'openai/strong-neutral'
            assert observed['returned_model'] == 'strong-neutral'
            assert observed['weight_revision'] == 'fixture-revision-b'
            assert observed['requested_max_output_tokens'] == observed['client_max_tokens'] == cap
            assert observed['usage'] == {'prompt_tokens': 10, 'completion_tokens': 2,
                'total_tokens': 12, 'reasoning_tokens': None}
            assert observed['prior_attempts'] == []
            assert observed['completion_evidence']['text'] == result['output']
            assert observed['completion_evidence']['truncated'] is False
        report = capsys.readouterr().out
        assert 'role_completion' in report and 'vision' in report


def test_text_only_candidate_is_unsupported_without_using_other_visual_binding(tmp_path):
    with endpoint(content='must not be requested') as (url, calls):
        cfg = config(url, url)
        # deliberate is image-capable, but it must not rescue this candidate.
        args = _args(tmp_path, cfg, binding='interactive')
        assert run(args) == 1
        assert calls == []
        for case in CASES:
            result = read(args.output/'attempts'/case.id/'result.json')
            assert result['outcome'] == 'unsupported'
            assert result['primary_outcome'] == 'unverified'
            assert result['missing_capabilities'] == ['supports_vision']
            assert result['observations'] == [] and result['checks'] == {}


def test_vision_declaration_and_http_success_do_not_make_unseeing_answers_pass(tmp_path):
    def blank_answer(payload):
        case = next(case for case in CASES if case.inputs['messages'] == payload['messages'])
        return json.dumps(dict.fromkeys(ANSWERS[case.id]))

    with endpoint(content=blank_answer) as (url, calls):
        args = _args(tmp_path, config(url, url))
        assert run(args) == 1 and len(calls) == 3
        for case in CASES:
            result = read(args.output/'attempts'/case.id/'result.json')
            assert result['outcome'] == result['primary_outcome'] == 'fail'
            assert not all(result['checks'].values())
        unknowns = read(args.output/'attempts/vision.covered-and-unknown/result.json')['checks']
        assert unknowns['owner_unknown'] is True and unknowns['visible_label'] is False
