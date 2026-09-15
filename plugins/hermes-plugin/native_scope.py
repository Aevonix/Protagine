"""Read the general plugin's resolved identity for an actual native turn."""


def attested_cli_contact(session_id):
    return _attested_contact(session_id, 'cli')


def attested_api_contact(session_id):
    return _attested_contact(session_id, 'api_server')


def _attested_contact(session_id, platform):
    """No session-recency lookup, configured-owner fallback or child promotion."""
    if not session_id:
        return None
    try:
        from agent.relay_runtime import active_turn
        from protagine_hermes import _TRANSPORT_SCOPES
    except ImportError:
        return None
    turn = active_turn(session_id)
    if (turn is None or turn.handle is None or not turn.task_id or not turn.turn_id
            or turn.lease.platform != platform or turn.lease.parent_session_id):
        return None
    scope = _TRANSPORT_SCOPES.for_execution(
        session_id=session_id, task_id=turn.task_id, turn_id=turn.turn_id)
    if (scope is None or not scope.valid_participant or scope.platform != platform
            or scope.authority_lane != 'system' or scope.resolution_status != 'attested_system'
            or scope.sender_id):
        return None
    return scope.contact_id
