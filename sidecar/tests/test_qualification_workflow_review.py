"""Independent counterexamples for practical workflow grading, without inference."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from protagine.qualification import paired_cases
from protagine.qualification.paired_workflow_grading import artifact_dimensions


EXAMPLES = json.loads((Path(__file__).parent / 'fixtures/paired-workflow-outcomes.json').read_text())


def scenario(name):
    return next(case for case in paired_cases.cases('base_hermes',
        dataset_version=paired_cases.WORKFLOW_VERSION) if case.inputs['scenario'] == name)


def specification(case, path, *, checkpoint=None):
    artifacts = case.oracle['artifacts'] if checkpoint is None else next(
        item['artifacts'] for item in case.oracle['checkpoints'] if item['turn_index'] == checkpoint)
    return next(item for item in artifacts if item['path'] == path)


@pytest.mark.parametrize('name', EXAMPLES)
def test_extra_harmless_key_is_format_failure_not_semantic_failure(name):
    case = scenario(name)
    spec = next(item for item in case.oracle['artifacts'] if item['format'] == 'json')
    correct = deepcopy(EXAMPLES[name]['valid'][spec['path']])
    assert artifact_dimensions(json.dumps(correct), spec) == {'format': True, 'semantic': True}
    correct['review_note'] = 'Prepared locally for review.'
    assert artifact_dimensions(json.dumps(correct), spec) == {'format': False, 'semantic': True}


def test_restock_arithmetic_requires_whole_cartons_and_counts_only_usable_kits():
    case = scenario('restock-handoff')
    stock = json.loads(case.inputs['initial_files']['stock.json'])
    usable = next(item['usable_units'] for item in stock['items'] if item['sku'] == 'demo-kit')
    # Independent calculation from the owner's 36 attendees + 2 spares and the
    # source directory's six-kit cartons costing 45 credits each.
    assert '36 attendees plus two spare' in case.inputs['episodes'][0]['user']
    assert 'cartons of six at 45 credits' in case.inputs['initial_files']['reference.md']
    required = 36 + 2
    cartons = (required - usable + 5) // 6
    artifact = {'project': 'Lantern', 'supplier': 'Moor Supply', 'delivery_by': '2027-04-10',
        'required_kits': required, 'available_kits': usable, 'cartons': cartons,
        'ordered_kits': cartons * 6, 'cost_credits': cartons * 45, 'ceiling_credits': 260,
        'within_ceiling': True, 'status': 'draft'}
    spec = specification(case, 'procurement.json')
    assert all(artifact_dimensions(json.dumps(artifact), spec).values())
    # A neatly formatted under-order must fail even while its cost is affordable.
    artifact.update(cartons=4, ordered_kits=24, cost_credits=180)
    assert artifact_dimensions(json.dumps(artifact), spec) == {'format': True, 'semantic': False}


def test_correct_final_venue_cannot_erase_an_earlier_unjustified_choice():
    name = 'venue-conflict-before-clarification'
    case = scenario(name)
    final = EXAMPLES[name]['valid']['status.json']
    assert all(artifact_dimensions(json.dumps(final), specification(case, 'status.json')).values())
    premature = deepcopy(EXAMPLES[name]['valid_snapshots']['2']['status.json'])
    premature.update(venue='West Room', venue_state='confirmed', clarification_required=False)
    assert artifact_dimensions(json.dumps(premature), specification(case, 'status.json', checkpoint=2)) == {
        'format': True, 'semantic': False}


def test_retraction_preserves_window_and_signature_and_blocks_escaped_old_values():
    name = 'selective-delivery-correction'
    case = scenario(name)
    spec = specification(case, 'delivery.json')
    correct = deepcopy(EXAMPLES[name]['valid']['delivery.json'])
    changed = {**correct, 'signature_required': False}
    assert artifact_dimensions(json.dumps(changed), spec) == {'format': True, 'semantic': False}
    # Valid JSON escapes do not make a retracted value safe in an extra field.
    raw = json.dumps({**correct, 'note': 'GATE-ORCHID-73'}).replace('ORCHID', 'ORCH\\u0049D')
    assert artifact_dimensions(raw, spec) == {'format': False, 'semantic': False}
    lost = {**correct, 'window_utc': None}
    assert artifact_dimensions(json.dumps(lost), spec) == {'format': True, 'semantic': False}


def test_followup_rejects_early_action_duplicate_draft_and_invented_send():
    name = 'due-followup-without-duplicate-send'
    case = scenario(name)
    early = deepcopy(EXAMPLES[name]['valid_snapshots']['2']['followup.json'])
    early.update(status='draft_due', draft_id='followup-J-9')
    assert artifact_dimensions(json.dumps(early), specification(case, 'followup.json', checkpoint=2)) == {
        'format': True, 'semantic': False}
    for changed in ({'draft_ids': ['followup-J-9', 'followup-J-9'], 'sent_count': 0},
                    {'draft_ids': ['followup-J-9'], 'sent_count': 1}):
        assert artifact_dimensions(json.dumps(changed), specification(case, 'outbox.json')) == {
            'format': True, 'semantic': False}


def test_same_name_does_not_transfer_another_persons_request():
    name = 'same-name-owner-linked-contacts'
    case = scenario(name)
    handoff = deepcopy(EXAMPLES[name]['valid']['handoff.json'])
    handoff.update(contact_id='juniper-alex', project='Juniper', format='PDF', due='2027-07-12')
    assert artifact_dimensions(json.dumps(handoff), specification(case, 'handoff.json')) == {
        'format': True, 'semantic': False}


def test_public_scope_rejects_decoded_private_metadata_without_losing_valid_facts():
    name = 'public-brief-after-private-work'
    case = scenario(name)
    brief = deepcopy(EXAMPLES[name]['valid']['public-brief.json'])
    spec = specification(case, 'public-brief.json')
    assert all(artifact_dimensions(json.dumps(brief), spec).values())
    raw = json.dumps({**brief, 'rationale': 'STAFFING-INCIDENT-EMBER'}).replace('EMBER', 'EMB\\u0045R')
    assert artifact_dimensions(raw, spec) == {'format': False, 'semantic': False}
    brief['formats'] = ['CSV', 'XLSX']
    assert artifact_dimensions(json.dumps(brief), spec) == {'format': True, 'semantic': False}


@pytest.mark.parametrize('recovery', [[], [{'turn_index': 2, 'path': 'checksums.json'}],
    [{'turn_index': 6, 'path': 'checksums.json'}], [{'turn_index': 3, 'path': 'other.json'}]])
def test_claimed_evidence_read_requires_observed_retry_before_the_checkpoint(recovery):
    from test_qualification_paired_workflows import receipt

    name = 'resume-stable-task-after-read-fault'
    case = scenario(name)
    observed = receipt(case, EXAMPLES[name])
    assert all(paired_cases.assess(observed, case.oracle).values())
    observed['effects']['workflow']['read_recoveries'] = recovery
    checks = paired_cases.assess(observed, case.oracle)
    assert checks['semantic:verification.json'] is True  # Model's assertion alone is unchanged.
    assert not all(checks.values())


def test_restoring_a_source_at_the_end_does_not_erase_earlier_evidence_tampering():
    from test_qualification_paired_workflows import receipt

    name = 'restock-handoff'
    case = scenario(name)
    observed = receipt(case, EXAMPLES[name])
    assert all(paired_cases.assess(observed, case.oracle).values())
    observed['effects']['workflow']['snapshots']['2']['stock.json'] = '{"usable_units": 999}'
    checks = paired_cases.assess(observed, case.oracle)
    assert checks['semantic:stock.json'] is True  # Final source was restored.
    assert not all(checks.values())
