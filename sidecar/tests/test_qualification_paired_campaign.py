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


# --- The campaign report -----------------------------------------------------------------------

ARMS = ('full-lessons', 'full')


@pytest.fixture(scope='module')
def records(rendered, tmp_path_factory):
    """Each arm's case records for the eight dev campaigns, as a frozen plan holds them."""
    directory = write_dataset(tmp_path_factory.mktemp('report') / 'campaigns', rendered)
    return {arm: [case.record() for case in CASES(arm, dataset_dir=directory,
                                                   profile={'name': arm, **paired.PROFILES[arm]})]
            for arm in ARMS}


def specs(record, *kinds):
    return [spec for spec in record['oracle']['artifacts'] if spec['probe']['kind'] in kinds]


def attempt(record, passed=(), *, outcome='fail', turns=None, files=None, calls=10, tokens=(100, 10), lessons=None):
    """A summarized attempt row: which artifact checks passed, how far the episode got, what it cost."""
    from protagine.qualification import paired_report
    declared = len(record['inputs']['episodes'])
    checks = {'all_native_turns_completed': turns is None,
              **{'artifact:' + spec['path']: spec['path'] in passed for spec in record['oracle']['artifacts']}}
    effects = {'declared_turns': declared, 'turns': [{'completed': True}] * (declared if turns is None else turns),
               'artifacts': dict(files or {}),
               'resource_usage': {'coverage': 'complete', 'total_model_calls': calls, 'input_tokens': tokens[0],
                                  'output_tokens': tokens[1], 'background_model_calls': 0}}
    if lessons is not None:
        effects['body'] = {'lessons': lessons}
    row = {'outcome': outcome, 'primary_outcome': outcome if outcome in {'pass', 'fail'} else 'unverified',
           'checks': checks if outcome in {'pass', 'fail'} else {}, 'effects': effects, 'elapsed_ms': 1.0}
    assert paired_report._completion(row) is (None if outcome not in {'pass', 'fail'} else outcome == 'pass')
    return row


def cohort(records, rows):
    """A frozen manifest and summarize()-shaped pairs; ``rows(index, arm, record)`` gives each attempt."""
    from protagine.qualification import paired_report
    manifest = {'pairs': [], 'sha256': '0' * 64,
                'comparison': {'campaign': {'protocol': paired_cases.CAMPAIGN_PROTOCOL, **paired.CAMPAIGN},
                               'rule': {**paired.RULE, 'unit': 'probe', 'cluster': 'campaign'}}}
    pairs = []
    for index in range(len(records[ARMS[0]])):
        results = {arm: rows(index, arm, records[arm][index]) for arm in ARMS}
        manifest['pairs'].append({'arms': {arm: {'case': records[arm][index]} for arm in ARMS}})
        pairs.append({'scenario_id': records[ARMS[0]][index]['id'], 'results': results,
                      'completion': {arm: paired_report._completion(results[arm]) for arm in ARMS}})
    return manifest, pairs


def paths(record, *kinds):
    return {spec['path'] for spec in specs(record, *kinds)}


def test_probe_units_exclude_training_old_family_and_unavailable_campaigns(records):
    from protagine.qualification import paired_report

    def rows(index, arm, record):
        # full passes every warranted probe; the comparator passes only the training artifacts and the
        # old-family probe; the last campaign's full attempt errored (unattributable).
        if index == 7 and arm == 'full':
            return attempt(record, outcome='error')
        kinds = ('warranted',) if arm == 'full' else ('training', 'old_family')
        return attempt(record, paths(record, *kinds))
    manifest, pairs = cohort(records, rows)
    units, clusters, unavailable = paired_report._probe_units(manifest, pairs, 'full', 'full-lessons')
    first = records['full'][0]
    assert unavailable == [records['full'][7]['id']]
    assert len(units) == 7 * 8 and set(clusters.values()) == {record['id'] for record in records['full'][:7]}
    assert {key.split(':', 1)[1] for key in units if key.startswith(first['id'] + ':')} == paths(
        first, 'warranted', 'control')
    for key, (treatment, comparator) in units.items():
        scenario, path = key.split(':', 1)
        record = next(item for item in records['full'] if item['id'] == scenario)
        assert (treatment, comparator) == ((1.0 if path in paths(record, 'warranted') else 0.0), 0.0)
        assert clusters[key] == scenario
    # The plan's rule makes the probe the unit: one unavailable campaign leaves the contrast unavailable,
    # a complete cohort is tested over probes with the campaigns as clusters.
    statistics = paired_report._statistics(manifest, pairs, list(ARMS), 'full-lessons',
                                           {arm: {'name': arm} for arm in ARMS}, manifest['comparison']['rule'])
    [entry] = statistics['contrasts']
    assert entry['unit'] == 'probe' and entry['verdict'] == 'unavailable'
    assert entry['declared_units'] == 64 and entry['unavailable_units'] == 8 and entry['unavailable_campaigns'] == 1
    manifest, pairs = cohort(records, lambda index, arm, record: attempt(
        record, paths(record, 'warranted') if arm == 'full' else ()))
    [entry] = paired_report._statistics(manifest, pairs, list(ARMS), 'full-lessons',
                                        {arm: {'name': arm} for arm in ARMS},
                                        manifest['comparison']['rule'])['contrasts']
    assert entry['unit'] == 'probe' and entry['units'] == 64 and entry['clusters'] == 8
    assert entry['wins'] == 48 and entry['ties'] == 16 and entry['verdict'] == 'demonstrated'


def test_a_campaign_whose_episode_ended_early_is_unavailable_in_both_arms(records):
    from protagine.qualification import paired_report
    # A failed turn on day 9 ends the comparator's third campaign: its later probes were never asked.
    manifest, pairs = cohort(records, lambda index, arm, record: attempt(
        record, paths(record, 'warranted'), turns=30 if (index, arm) == (2, 'full-lessons') else None))
    units, clusters, unavailable = paired_report._probe_units(manifest, pairs, 'full', 'full-lessons')
    assert unavailable == [records['full'][2]['id']]
    assert not any(key.startswith(records['full'][2]['id'] + ':') for key in units)
    report = paired_report._campaign(manifest, pairs, list(ARMS), 'full-lessons')
    assert report['unavailable_campaigns'] == {'full-lessons': [records['full'][2]['id']], 'full': []}


def test_old_family_row_is_a_point_estimate_non_inferiority(records):
    from protagine.qualification import paired_report

    def rows(losing):
        def row(index, arm, record):
            kinds = ('warranted',) if arm == 'full' and index < losing else ('warranted', 'old_family')
            return attempt(record, paths(record, *kinds))
        return row
    # One campaign of eight lost: -12.5 pp, inferior at -10 pp; none lost: non-inferior.
    manifest, pairs = cohort(records, rows(1))
    [row] = paired_report._campaign(manifest, pairs, list(ARMS), 'full-lessons')['old_family']
    assert row['treatment'] == 'full' and row['comparator'] == 'full-lessons'
    assert row['campaigns'] == 8 and row['treatment_pass_rate'] == 7 / 8 and row['comparator_pass_rate'] == 1
    assert row['delta_pp'] == -12.5 and row['non_inferior_pp'] == -10 and row['verdict'] == 'inferior'
    manifest, pairs = cohort(records, rows(0))
    [row] = paired_report._campaign(manifest, pairs, list(ARMS), 'full-lessons')['old_family']
    assert row['delta_pp'] == 0 and row['verdict'] == 'non_inferior'
    # A campaign passes the old-family probe only when every one of its old-family artifacts passes.
    multi = copy.deepcopy(records['full'][0])
    [old] = specs(multi, 'old_family')
    multi['oracle']['artifacts'].append({**copy.deepcopy(old), 'path': 'second-' + old['path']})
    some = {old['path']}
    assert not paired_report._old_family_pass(attempt(multi, some), multi)
    assert paired_report._old_family_pass(attempt(multi, paths(multi, 'old_family')), multi)


def test_forbidden_hits_are_counted_from_the_probe_files(records):
    from protagine.qualification import paired_report

    def rows(index, arm, record):
        files = {}
        for spec in specs(record, 'control'):
            if spec.get('forbidden') and arm == 'full':
                files[spec['path']] = json.dumps({'value': spec['forbidden'][0].lower()})
        return attempt(record, files=files)
    manifest, pairs = cohort(records, rows)
    expected = sum(bool(spec.get('forbidden')) for record in records['full'] for spec in specs(record, 'control'))
    assert expected >= 2
    report = paired_report._campaign(manifest, pairs, list(ARMS), 'full-lessons')
    assert report['forbidden_hits'] == {'full-lessons': 0, 'full': expected}


def test_cost_per_success_compares_calls_and_tokens_per_passed_probe(records):
    from protagine.qualification import paired_report

    def rows(cost):
        def row(index, arm, record):
            # Both arms pass the same six warranted probes of every campaign; full spends ``cost`` times as much.
            scale = cost if arm == 'full' else 1
            return attempt(record, paths(record, 'warranted'), calls=10 * scale, tokens=(100 * scale, 10 * scale))
        return row
    manifest, pairs = cohort(records, rows(1.1))
    cost = paired_report._campaign(manifest, pairs, list(ARMS), 'full-lessons')['cost_per_success']
    assert cost['full-lessons']['passed_probes'] == cost['full']['passed_probes'] == 48
    assert cost['full-lessons']['calls_per_success'] == pytest.approx(80 / 48)
    assert cost['full']['calls_ratio'] == pytest.approx(1.1) and cost['full']['tokens_ratio'] == pytest.approx(1.1)
    assert cost['full']['within_max_increase'] is True and cost['max_increase_pct'] == 20
    manifest, pairs = cohort(records, rows(1.3))
    cost = paired_report._campaign(manifest, pairs, list(ARMS), 'full-lessons')['cost_per_success']
    assert cost['full']['within_max_increase'] is False


def test_lesson_diagnostics_are_unavailable_without_lesson_evidence(records):
    from protagine.qualification import paired_report
    manifest, pairs = cohort(records, lambda index, arm, record: attempt(record, paths(record, 'warranted')))
    report = paired_report._campaign(manifest, pairs, list(ARMS), 'full-lessons')
    assert report['lessons'] == {'full-lessons': 'unavailable', 'full': 'unavailable'}
    # The descriptive rows: per class, per probe kind and control, per block, training.
    rows = report['descriptive']['full']
    assert rows['kinds']['warranted'] == {'passed': 48, 'observed': 48}
    assert rows['kinds']['control:scope'] == rows['kinds']['control:unverified'] == {'passed': 0, 'observed': 8}
    assert rows['blocks'] == {'1': {'passed': 24, 'observed': 32}, '2': {'passed': 24, 'observed': 32}}
    assert rows['classes']['procedure']['observed'] == 24 and rows['training'] == {'passed': 0, 'observed': 48}
    assert 'Campaign probes' in paired_report._campaign_lines({'campaign': report})[1]


def test_lesson_diagnostics_count_admissions_by_source_and_lesson_use_on_probe_days(records):
    from protagine.qualification import paired_report

    def rows(index, arm, record):
        if arm == 'full-lessons':
            return attempt(record, (), lessons={'lessons': [], 'uses': []})
        probe = specs(record, 'warranted')[0]
        lessons = [{'id': 'L-1', 'verified': 'owner', 'status': 'active', 'origin': 'night', 'correction': 'retrieval'},
                   {'id': 'L-2', 'verified': 'check', 'status': 'candidate', 'origin': 'reflector', 'correction': None}]
        uses = [{'lesson_id': 'L-1', 'session_id': f"day-{probe['probe']['day']:02d}", 'result': None},
                {'lesson_id': 'L-1', 'session_id': 'day-02', 'result': 'win'}]
        return attempt(record, {probe['path']}, lessons={'lessons': lessons, 'uses': uses})
    manifest, pairs = cohort(records, rows)
    lessons = paired_report._campaign(manifest, pairs, list(ARMS), 'full-lessons')['lessons']
    assert lessons['full-lessons']['admitted'] == 0 and lessons['full-lessons']['campaigns'] == 8
    full = lessons['full']
    assert full['admitted'] == 16 and full['by_verified'] == {'owner': 8, 'check': 8}
    assert full['by_status'] == {'active': 8, 'candidate': 8} and full['corrections'] == {'retrieval': 8}
    assert full['uses'] == 16 and full['scored_uses'] == 8 and full['wins'] == 8
    assert full['probes'] == {'eligible': 64, 'with_lesson': 8, 'with_lesson_passed': 8}
    assert full['lesson_use_rate'] == 8 / 64


def test_improve_campaigns_give_every_arm_the_same_read_only_skill_tools(fixture, monkeypatch, rendered, tmp_path):
    """Hermes shows the skills index only to an agent with a skill tool, so without one no arm could see a
    skill (evals section 11, the amendment of 2026-09-24): every arm of mind-improve-1 gets skills_list and
    skill_view, and none gets skill_manage."""
    from protagine.qualification import paired_container, paired_worker
    assert paired_cases.GENERATED_SKILL_TOOLS == {'mind-improve-1': 'read'}
    assert paired_worker.SKILL_TOOLS == {'read': ('skills_list', 'skill_view')}
    assert all('skill_manage' not in tools for tools in paired_worker.SKILL_TOOLS.values())
    arms = ('full-lessons', 'full', 'full-plus-skills', 'base-curator')
    manifest = campaign_plan(fixture, monkeypatch, rendered, tmp_path, arms=arms)
    assert manifest['comparison']['skill_tools'] == {'protocol': paired_worker.SKILLS_PROTOCOL, 'mode': 'read',
                                                     'tools': ['skills_list', 'skill_view']}
    assert all(pair['arms'][arm]['case']['inputs']['skill_tools'] == 'read'
               for pair in manifest['pairs'] for arm in arms)
    # Another generated family declares none.
    other = copy.deepcopy(rendered)
    for item in other:
        for spec in item['oracle']['artifacts']:
            del spec['probe']
    write_dataset(tmp_path / 'data-other', other, dataset_id='mind-other-1')
    monkeypatch.setattr(paired_cases, 'cases', CASES)
    plain = paired.plan(fixture.output / 'other', native_binding='candidate', evidence_mode='controlled',
                        dataset_dir=tmp_path / 'data-other', arms=['full-lessons', 'full'],
                        reference_arm='full-lessons', **fixture.resources)
    assert 'skill_tools' not in plain['comparison']
    assert all('skill_tools' not in pair['arms']['full']['case']['inputs'] for pair in plain['pairs'])
    # An image whose worker does not give the tools cannot run the family.
    original = paired_container.configuration

    def without_skills(*args, **kwargs):
        supplied, recipe = original(*args, **kwargs)
        recipe['container_payload'] = {k: v for k, v in recipe['container_payload'].items() if k != 'skills_dir'}
        return supplied, recipe
    monkeypatch.setattr(paired_container, 'configuration', without_skills)
    with pytest.raises(ValueError, match='skill'):
        campaign_plan(fixture, monkeypatch, rendered, tmp_path, name='old-image')


def test_a_plus_skills_arm_needs_an_image_whose_worker_mounts_the_skills_dir(fixture, monkeypatch):
    from protagine.qualification import paired_container
    original = paired_container.configuration

    def without_skills(*args, **kwargs):
        supplied, recipe = original(*args, **kwargs)
        recipe['container_payload'] = {k: v for k, v in recipe['container_payload'].items() if k != 'skills_dir'}
        return supplied, recipe
    monkeypatch.setattr(paired_container, 'configuration', without_skills)
    fixture.output.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='skills'):
        paired.plan(fixture.output / 'skills', native_binding='candidate', evidence_mode='controlled',
                    arms=['full', 'full-plus-skills'], **fixture.resources)
    assert paired.plan(fixture.output / 'lessons', native_binding='candidate', evidence_mode='controlled',
                       arms=['full-lessons', 'full'], **fixture.resources)['declared_attempts'] == 4


@pytest.mark.asyncio
async def test_every_end_of_day_tick_of_a_campaign_starts_a_night(tmp_path, monkeypatch):
    """Nightly work needs nothing new in a campaign: the mind arms run with quiet hours off, so the 03:00
    boundary falls inside every one-day clock advance, and the forced end-of-day tick waits for the night."""
    from types import SimpleNamespace
    from protagine.initiatives.store import InitiativeStore
    from protagine.mind import Mind
    from protagine.qualification import paired_body
    from protagine.qualification.native_memory_worker import mind_clock, mind_section
    from protagine.turns.idempotency import TurnIdempotencyLedger
    # Imported before the shifted clock is installed: a default argument bound to time.time at import would
    # otherwise keep the shifted function after the clock is uninstalled.
    import protagine.self_model.appraisals  # noqa: F401

    class Router:
        supports_function_routing = True

        def function_deadline_seconds(self, **_):
            return 20

        async def complete(self, messages, *, context=None, **_):
            return SimpleNamespace(content='{}', usage={'total_tokens': 10})

    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', 'p-01')
    for arm in ({'full': True}, {'full': True, 'minus_lessons': True}):
        root = tmp_path / str(len(arm))
        root.mkdir()
        store = InitiativeStore(state_dir=root)
        paired_body.install_clock(paired_body.start_offset('12:00'))
        try:
            mind = Mind(config=mind_section(arm), store=store, state_dir=root, owner_id='p-01',
                        ledger=TurnIdempotencyLedger(root / 'turn-idempotency.db'), router=Router(),
                        clock=mind_clock, backups=False)
            assert mind_clock().hour == 12
            nights = []
            for _ in range(3):
                paired_body.advance_clock(86400)
                nights.append((await mind.tick(force=True))['consolidation'])
            assert nights == ['done'] * 3
            rows = [row for row in store.intentions(kind=['note'], limit=20) if row.type == 'consolidation']
            assert len(rows) == 3
        finally:
            paired_body.uninstall_clock()
            store.close()
