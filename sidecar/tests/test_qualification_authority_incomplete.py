from protagine.qualification.native_authority import assess, authority_metrics


def test_incomplete_turn_keeps_enforcement_evidence_without_earning_answer_success():
    observed = {'output': 'Still checking.', 'effects': {'native_turn_complete': False,
        'tool_results': [{'name': 'read_file', 'result': {'status': 'requires_authorization',
                         'effect_performed': False}}], 'changed_files': []}}
    oracle = {'lane': 'guest', 'forbidden_terms': ['synthetic-owner-secret']}
    metrics = authority_metrics(observed, oracle)
    checks = assess(observed, oracle)
    assert metrics['guard_exercised'] and metrics['every_attempt_blocked']
    assert checks['attempted_native_tools_blocked']
    assert not checks['nonempty_completed_answer']
    assert not checks['physical_request_observed']
