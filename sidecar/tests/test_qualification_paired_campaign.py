"""Campaign mode of the paired harness: ordered days in one state, probes as the unit, the campaign
as the cluster (build plan M9, families/mind-improve-1.md section 3)."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from protagine.qualification import paired, paired_cases
from protagine.qualification.records import (CaseSpec, MAX_CAMPAIGN_OUTPUT_BYTES, MAX_CAMPAIGN_SECONDS,
                                             MAX_CASE_OUTPUT_BYTES, MAX_CASE_SECONDS)

from test_qualification_paired_runner import fixture  # noqa: F401  (pytest fixture)

REPOSITORY = Path(__file__).resolve().parents[2]
# The runner fixture replaces paired_cases.cases with its controlled cases; campaign plans need the real one.
CASES = paired_cases.cases
GENERATORS = REPOSITORY / 'benchmarks' / 'paired' / 'generators'


def engine():
    spec = importlib.util.spec_from_file_location('paired_generate_campaign', GENERATORS / 'generate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def rendered():
    generate = engine()
    module = generate.load_templates(GENERATORS / 'improve.py')
    return generate.render(module, 7, 1)


def write_dataset(directory, scenarios, dataset_id='mind-improve-1'):
    """A generated dataset directory with the loader's manifest shape."""
    directory = Path(directory)
    directory.mkdir(parents=True)
    raw = (json.dumps(scenarios, indent=1, sort_keys=True) + '\n').encode()
    families = {}
    for item in scenarios:
        families[item['family']] = families.get(item['family'], 0) + 1
    manifest = {'dataset_id': dataset_id, 'version': dataset_id, 'scenario_count': len(scenarios),
                'families': families,
                'files': {'scenarios.json': {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}},
                'generator': {'protocol': paired_cases.GENERATOR_PROTOCOL, 'split': 'dev', 'seed': 7}}
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    (directory / 'scenarios.json').write_bytes(raw)
    return directory


def full_profile():
    return {'name': 'full', **paired.PROFILES['full']}


def test_campaign_cases_get_a_deadline_from_their_days_and_a_larger_output_bound(rendered, tmp_path):
    directory = write_dataset(tmp_path / 'campaigns', rendered)
    cases = paired_cases.cases('full', dataset_dir=directory, profile=full_profile())
    for case, item in zip(cases, rendered):
        days = sum('tick' in entry for entry in item['episodes'])
        assert days == 15
        assert case.inputs['campaign'] == {'protocol': paired_cases.CAMPAIGN_PROTOCOL, 'days': days}
        assert case.timeout_seconds == min(MAX_CAMPAIGN_SECONDS, 600 + paired_cases.CAMPAIGN_DAY_SECONDS * days)
        assert case.timeout_seconds == 11400 and case.max_output_bytes == MAX_CAMPAIGN_OUTPUT_BYTES
        record = case.record()
        assert record['timeout_seconds'] == 11400 and record['inputs']['campaign']['days'] == 15
    # A campaign may not declare more than the campaign bounds.
    case = cases[0]
    for change in ({'timeout_seconds': MAX_CAMPAIGN_SECONDS + 1}, {'max_output_bytes': MAX_CAMPAIGN_OUTPUT_BYTES + 1}):
        with pytest.raises(ValueError):
            CaseSpec(**{**case.__dict__, **change}).record()


def test_case_records_keep_the_old_bounds_outside_campaigns(rendered, tmp_path):
    assert (MAX_CASE_SECONDS, MAX_CASE_OUTPUT_BYTES) == (600, 1024 * 1024)
    base = dict(id='case-1', version='v', role='reasoning', boundary='native_hermes', consumer='c',
                evaluator='e', inputs={}, oracle={})
    assert CaseSpec(**base, timeout_seconds=600, max_output_bytes=1024 * 1024).record()
    for change in ({'timeout_seconds': 601}, {'max_output_bytes': 1024 * 1024 + 1},
                   {'timeout_seconds': 11400, 'inputs': {'campaign': 'not an object'}}):
        with pytest.raises(ValueError):
            CaseSpec(**{**base, **change}).record()
    # A generated dataset without probes keeps the generated-episode deadline.
    plain = copy.deepcopy(rendered[:1])
    for spec in plain[0]['oracle']['artifacts']:
        del spec['probe']
    case, = paired_cases.cases('full', dataset_dir=write_dataset(tmp_path / 'plain', plain), profile=full_profile())
    assert 'campaign' not in case.inputs
    assert case.timeout_seconds == 600 and case.max_output_bytes == 1024 * 1024


def test_probe_metadata_is_validated_when_a_dataset_loads(rendered, tmp_path):
    assert paired_cases.PROBE_KINDS == ('training', 'warranted', 'control', 'old_family')
    manifest, scenarios, _ = paired_cases.load_generated_dataset(write_dataset(tmp_path / 'good', rendered))
    assert len(scenarios) == 8

    def broken(name, change):
        scenarios = copy.deepcopy(rendered)
        change(scenarios)
        with pytest.raises(ValueError, match='probe'):
            paired_cases.load_generated_dataset(write_dataset(tmp_path / name, scenarios))

    def spec(scenarios):
        return scenarios[0]['oracle']['artifacts'][0]

    broken('kind', lambda s: spec(s)['probe'].update(kind='guess'))
    broken('day-missing', lambda s: spec(s)['probe'].pop('day'))
    broken('day-zero', lambda s: spec(s)['probe'].update(day=0))
    broken('day-bool', lambda s: spec(s)['probe'].update(day=True))
    broken('extra-key', lambda s: spec(s)['probe'].update(weight=2))
    broken('control-type', lambda s: spec(s)['probe'].update(control=3))
    broken('not-object', lambda s: spec(s).update(probe='training'))
    # A scenario is a campaign only when every artifact carries a probe.
    broken('partial', lambda s: spec(s).pop('probe'))
    # A dataset is all campaigns or none.

    def mixed(scenarios):
        for artifact in scenarios[1]['oracle']['artifacts']:
            del artifact['probe']
    broken('mixed', mixed)


@pytest.mark.asyncio
async def test_one_campaign_is_a_run_of_its_own_and_its_record_fits_the_reader(tmp_path):
    """The runner's one-hour cap stays for every other run; a campaign's result record, its output
    at the campaign bound plus the attempt metadata, is still a readable record."""
    from protagine.qualification import records
    from protagine.qualification.runner import evaluate

    async def consumer(inputs, context):
        return {'output': 'done', 'effects': {}}

    campaign = CaseSpec(id='campaign-1', version='v', role='reasoning', boundary='native_hermes', consumer='c',
                        evaluator='e', inputs={'campaign': {'protocol': paired_cases.CAMPAIGN_PROTOCOL, 'days': 15}},
                        oracle={}, timeout_seconds=11400, max_output_bytes=MAX_CAMPAIGN_OUTPUT_BYTES)
    recipe = {'binding': 'candidate', 'declared': {}}
    run = lambda path, cases: evaluate(path, recipe, cases, {'c': consumer}, {'e': lambda observed, oracle: {'ok': True}},
                                      lambda case: None)
    await run(tmp_path / 'one', [campaign])
    assert records.read(tmp_path / 'one' / 'attempts' / 'campaign-1' / 'result.json')['outcome'] == 'pass'
    with pytest.raises(ValueError, match='one hour'):
        await run(tmp_path / 'two', [campaign, CaseSpec(**{**campaign.__dict__, 'id': 'campaign-2'})])
    assert records.MAX_RECORD_BYTES >= 2 * MAX_CAMPAIGN_OUTPUT_BYTES


def test_a_campaign_sized_result_line_is_read_from_the_container_log(tmp_path):
    from protagine.qualification import paired_container
    from protagine.qualification.paired_worker import RESULT_MARKER
    result = {'stage': 'returned', 'output': 'x' * (6 * 1024 * 1024)}
    log = tmp_path / 'container.log'
    log.write_bytes(b'noise\n' * 1000 + RESULT_MARKER.encode() + json.dumps(result).encode() + b'\n')
    assert paired_container._result_from_log(log) == result


def campaign_plan(fixture, monkeypatch, rendered, tmp_path, *, arms=('full-lessons', 'full'), name='plan'):
    monkeypatch.setattr(paired_cases, 'cases', CASES)
    directory = tmp_path / ('data-' + name)
    if not directory.exists():
        write_dataset(directory, rendered)
    fixture.output.mkdir(mode=0o700, exist_ok=True)
    return paired.plan(fixture.output / name, native_binding='candidate', evidence_mode='controlled',
                       dataset_dir=directory, arms=list(arms), reference_arm=arms[0], **fixture.resources)


def test_a_campaign_plan_freezes_the_probe_unit_the_campaign_cluster_and_the_old_family_rule(
        fixture, monkeypatch, rendered, tmp_path):
    manifest = campaign_plan(fixture, monkeypatch, rendered, tmp_path)
    comparison = manifest['comparison']
    assert comparison['campaign'] == {'protocol': paired_cases.CAMPAIGN_PROTOCOL, 'unit': 'probe',
                                      'cluster': 'campaign', 'old_family': {'non_inferior_pp': -10},
                                      'cost_per_success': {'max_increase_pct': 20}}
    assert comparison['rule'] == {**paired.RULE, 'unit': 'probe', 'cluster': 'campaign'}
    assert manifest['declared_seconds'] == 8 * 2 * 11400
    assert all(pair['arms'][arm]['case']['inputs']['campaign']['days'] == 15
               for pair in manifest['pairs'] for arm in ('full-lessons', 'full'))
    # A plan over plain scenarios keeps the scenario unit and no campaign block.
    plain = copy.deepcopy(rendered)
    for item in plain:
        for spec in item['oracle']['artifacts']:
            del spec['probe']
    write_dataset(tmp_path / 'data-plain', plain)
    other = campaign_plan(fixture, monkeypatch, plain, tmp_path, name='plain')
    assert 'campaign' not in other['comparison'] and other['comparison']['rule'] == paired.RULE
