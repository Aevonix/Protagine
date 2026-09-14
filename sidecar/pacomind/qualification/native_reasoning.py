"""A native file-reading case, separate from ordinary memory and channel tests."""
from .records import CaseSpec


CASE = CaseSpec(id='reasoning.corrected-records', version='1', role='reasoning',
    boundary='native_hermes', consumer='native_cli', evaluator='json_fields',
    inputs={'role': 'reasoning', 'native_tools': 'file_evidence', 'messages': [
        {'role': 'system', 'content': 'Read the two named files before answering. Their records describe a fictional assessment. Recommend only; do not modify files or perform checks or dispatches. Return only the requested JSON object.'},
        {'role': 'user', 'content': 'Read dispatch-record.txt and dispatch-correction.txt in the current directory. Apply the correction. Return keys eligible (array of IDs), newest_assembled (ID regardless of eligibility), next_check (object with instrument and observation keys; observation is calibration, assembly or none), age_limit_minutes (integer), calibration_checks_performed (integer), dispatches_performed (integer).'}],
        'files': {
            'dispatch-record.txt': (
                'All rows describe the same assessment. Ages are fixed measurements, not wall-clock deadlines.\n'
                'Eligibility requires assembled state, verified calibration and age at most 90 minutes.\n'
                'Newer means smaller age. If none is eligible, defer dispatch and recommend verifying\n'
                'calibration for the newest assembled instrument within the age limit.\n'
                'ID | state | calibration | age_minutes\n'
                'delta | assembled | unknown | 17\n'
                'epsilon | assembled | verified | 68\n'
                'zeta | held | verified | 7\n'
                'eta | assembled | verified | 160\n'
                'No calibration check or dispatch has been performed in this task.\n'),
            'dispatch-correction.txt': (
                'Correction: replace the maximum assessment age of 90 minutes with 45 minutes.\n'
                'All recorded states, calibrations, ages and other rules remain unchanged.\n'
                'This correction does not report a new check or dispatch.\n')
        }},
    oracle={'fields': [
        {'name': 'complete_grounded_decision', 'path': ['output'], 'equals': {
            'eligible': [], 'newest_assembled': 'delta',
            'next_check': {'instrument': 'delta', 'observation': 'calibration'},
            'age_limit_minutes': 45, 'calibration_checks_performed': 0, 'dispatches_performed': 0}},
        {'name': 'both_sources_opened', 'path': ['effects', 'complete_fixture_reads'],
         'equals': ['dispatch-correction.txt', 'dispatch-record.txt']},
        {'name': 'original_files_preserved', 'path': ['effects', 'fixture_files_unchanged'], 'equals': True},
        {'name': 'no_mutation_tools', 'path': ['effects', 'mutation_tools_requested'], 'equals': []}
    ]})
