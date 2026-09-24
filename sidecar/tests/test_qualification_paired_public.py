"""Public exports retain evidence and exclude private recipes and agent contents."""
import argparse
from copy import deepcopy
import hashlib
import json

import pytest

from protagine.qualification import paired, paired_cases, paired_container, paired_public, paired_report
from protagine.qualification.cli import add_parser, run as cli_run
from protagine.qualification.records import digest, read


@pytest.fixture(params=[paired_cases.REVIEWED_VERSION, paired_cases.BASELINE_VERSION])
def planned(tmp_path, monkeypatch, request):
    secret = 'PRIVATE_SENTINEL_NOT_FOR_PUBLICATION'
    config = tmp_path / 'candidate.json'
    config.write_text(json.dumps({'providers': {'candidate': {'api_key': secret}}}))
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'version': 'paired-policy-1', 'budget_mode': 'deployment_policy',
        'budget_policy': {'description': secret},
        'environment': {'endpoint_usage': 'shared', 'hardware_recipe': secret}}))
    monkeypatch.setattr(paired_container, 'configuration', lambda *a, **kw: (read(config),
        {'container': {'image_id': 'sha256:' + 'a' * 64, 'docker_host': secret},
         'container_payload': {'adapters': {'packages': {'protagine': {'sha256': 'b' * 64}}}},
         'native_runtime': {'distribution_version': '0.21.3', 'native_payload_sha256': 'c' * 64}}))
    directory = tmp_path / 'paired'
    manifest = paired.plan(directory, native_config=config, native_binding='candidate',
        comparison_policy=policy, container_image='sha256:' + 'a' * 64,
        case_ids=[paired_cases.CASE_IDS[0]], dataset_version=request.param)
    metadata = {'publication_scope': 'public_synthetic',
        'deployment': {'id': 'candidate', 'model': 'Test candidate', 'profile': 'Paired development'}}
    return directory, metadata, manifest, secret


def test_public_snapshot_keeps_missing_data_unknown_and_contains_no_private_body(planned, tmp_path):
    directory, metadata, _, secret = planned
    output = tmp_path / 'public'
    result = paired_public.publish_record(directory, metadata, output)
    index = read(output / 'index.json')
    raw = (output / 'runs' / (result['run_id'] + '.json')).read_bytes()
    record = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == index['records'][0]['sha256']
    assert record['declared_episodes'] == 1 and record['paired_score'] is None
    assert record['endpoint_usage'] == 'shared'
    assert record['runtime']['protagine_payload_sha256'] == 'b' * 64
    assert record['quality_status'] == 'development'
    assert record['arms']['base_hermes']['accounting']['input_tokens']['total'] is None
    assert record['arms']['base_hermes']['timing']['metrics']['first_generated_ms']['median'] is None
    assert secret.encode() not in raw
    assert all(key.encode() not in raw for key in ('initial_files', 'oracle', 'api_key', 'docker_host'))
    with pytest.raises(FileExistsError):
        paired_public.publish_record(directory, metadata, output)


def test_publication_rejects_modified_synthetic_inputs(planned):
    directory, metadata, manifest, _ = planned
    manifest['pairs'][0]['arms']['base_hermes']['case']['inputs']['episodes'][0]['user'] = 'Private owner request'
    manifest['sha256'] = digest({k: v for k, v in manifest.items() if k != 'sha256'})
    (directory / 'paired.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Private or changed fixture'):
        paired_public.export_record(directory, metadata)


def test_completed_public_projection_keeps_negative_delta_and_timing_counts(planned, monkeypatch):
    directory, metadata, _, secret = planned
    report = paired_report.summarize(directory)
    report.update(comparable_pairs=1, unavailable_pairs=0)
    report['paired_score'] = dict(zip(paired_public.SCORES, (1, 1, 0, 100, 0, -100, 0, 0, 1, 0, 0)))
    pair = report['pairs'][0]
    pair.update(comparison='loss', completion={'base_hermes': True, 'protagine': False})
    for arm, outcome in (('base_hermes', 'pass'), ('protagine', 'fail')):
        report['arms'][arm].update(outcomes={outcome: 1}, attributed_completed=int(outcome == 'pass'),
            observed_completed=int(outcome == 'pass'), completion_percent=100 if outcome == 'pass' else 0)
        pair['results'][arm].update(outcome=outcome, primary_outcome=outcome, output=secret)
    report['arms']['protagine']['timing'] = {'requests': {'observed': 3, 'instrumented': 3, 'completed': 2},
        'metrics': {'first_generated_ms': {'median': 0, 'min': 0, 'max': 2, 'samples': 2, 'eligible_samples': 3,
                                          'definition': secret}}}
    monkeypatch.setattr(paired_report, 'summarize', lambda _: deepcopy(report))
    record = paired_public.export_record(directory, metadata)
    assert record['paired_score']['delta_percentage_points'] == -100
    timing = record['arms']['protagine']['timing']
    assert timing['metrics']['first_generated_ms']['median'] == 0
    assert timing['metrics']['first_generated_ms']['eligible_samples'] == 3
    assert secret not in json.dumps(record)
    report['evidence_mode'] = 'controlled'
    assert paired_public.export_record(directory, metadata)['paired_score'] is None


def test_cli_export_needs_no_endpoint_and_writes_new_public_snapshot(planned, tmp_path, capsys):
    directory, metadata, _, _ = planned
    meta = tmp_path / 'public-metadata.json'
    meta.write_text(json.dumps(metadata))
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    assert cli_run(parser.parse_args(['models', 'paired', 'export', '--output', str(directory),
        '--metadata', str(meta), '--public-output', str(tmp_path / 'snapshot')])) == 0
    captured = capsys.readouterr()
    assert captured.err == ''
    assert read(tmp_path / 'snapshot' / 'index.json')['records']


def test_public_v3_projection_is_allowlisted_and_keeps_raw_attribution(planned, monkeypatch):
    directory, metadata, _, secret = planned
    report = paired_report.summarize(directory)
    pair = report['pairs'][0]
    pair['results']['protagine'].update(outcome='fail', primary_outcome='unverified', output=secret)
    pair['completion']['protagine'] = False
    pair['completion_projection']['protagine'] = {'rule': paired_report.NO_OUTPUT_RULE,
        'source_row_sha256': 'f' * 64, 'private_details': secret}
    report['completion_projection'].update(corrected_episodes=1, basis=secret)
    monkeypatch.setattr(paired_report, 'summarize', lambda _: deepcopy(report))
    record = paired_public.export_record(directory, metadata)
    projected = record['episodes'][0]['results']['protagine']
    assert record['report_protocol'] == 'paired-attribution-3'
    assert record['completion_projection']['corrected_episodes'] == 1
    assert projected['primary_outcome'] == 'unverified' and projected['completion'] is False
    assert projected['completion_projection'] == {'rule': paired_report.NO_OUTPUT_RULE,
                                                 'source_row_sha256': 'f' * 64}
    assert secret not in json.dumps(record)
    assert record['paired_score'] is None
    report['completion_projection']['corrected_episodes'] = 0
    with pytest.raises(ValueError, match='projection count'):
        paired_public.export_record(directory, metadata)


def test_public_export_keeps_the_probe_unit():
    """A campaign contrast is exported with its probe unit and campaign clusters, not as scenarios."""
    from protagine.qualification import paired_statistics
    units = {f'c{index // 8}:p{index % 8}': (float(index % 2), 0.0) for index in range(64)}
    clusters = {key: key.split(':')[0] for key in units}
    result = paired_statistics.contrast(units, seed=3, clusters=clusters)
    contrast = {'treatment': 'full', 'comparator': 'full-lessons', 'same_profile': False, 'unit': 'probe',
                'cluster': 'campaign', 'declared_units': 64, 'unavailable_units': 0, 'unavailable_campaigns': 0,
                **result}
    rule = {**paired.RULE, 'unit': 'probe', 'cluster': 'campaign'}
    exported = paired_public._statistics({'rule': rule, 'bootstrap_seed': 3, 'contrasts': [contrast]})
    [entry] = exported['contrasts']
    assert entry['unit'] == 'probe' and entry['cluster'] == 'campaign' and entry['clusters'] == 8
    assert entry['units'] == 64 and exported['basis'] == paired_report.PROBE_STATISTICS_BASIS
    # A scenario contrast keeps the scenario unit and the scenario basis.
    scenario = {**contrast, 'unit': 'scenario', **paired_statistics.contrast(units, seed=3)}
    del scenario['cluster']
    exported = paired_public._statistics({'rule': paired.RULE, 'bootstrap_seed': 3, 'contrasts': [scenario]})
    assert exported['contrasts'][0]['unit'] == 'scenario' and 'cluster' not in exported['contrasts'][0]
    assert exported['basis'] == paired_report.STATISTICS_BASIS
