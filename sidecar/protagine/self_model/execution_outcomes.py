"""Source-bound task lifecycle evidence for the existing judgment worker.

Only prospectively classified operational task admissions participate. A
terminal callback establishes lifecycle, never artifact quality or permission.
The existing execution metadata, source lineage and processing ledger own all
state; there is no new queue, worker, retrospective import or model call here.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging

from . import judgments
from .execution_forecasts import VERSION as FORECAST_VERSION, _read, _config, _processor

VERSION = 'task-execution-outcome-v1'
logger = logging.getLogger(__name__)


def _enabled(owner):
    from protagine.identity import get_owner_contact_id
    return bool(owner and owner == get_owner_contact_id() and judgments.enabled())


def evidence_text(facts):
    """Render only for judgment deliberation; telemetry is not recall text."""
    return ('Runtime-recorded operational task experience. The attributed request is what was '
        'asked, not evidence that it was fulfilled. Completion means the native turn ended; '
        'output correctness, usefulness, external effects and owner approval are unobserved. '
        'Lifecycle and elapsed time do not establish competence, trust or blame. '
        'Requested model labels and response-reported labels remain distinct.\n'
        + json.dumps(facts, sort_keys=True, separators=(',', ':')))


def observe(registry, execution_id, owner):
    if not _enabled(owner):
        return None
    row, data = _read(registry, execution_id)
    if row is None or row['contact_id'] != owner or row['state'] == 'observed':
        return None
    selection = data.get('task_experience') or {}
    if not selection:
        return None
    if (selection.get('purpose') != 'operational' or not data.get('start_observed')
            or row['parent_execution_id'] or not data.get('input_refs')):
        return 'ineligible'
    ledger = registry.ledger
    source_id = 'task-execution:' + hashlib.sha256(execution_id.encode()).hexdigest()
    # Empty-content messages need distinct source-session identities so an
    # exact-message forget cannot match another execution's empty content.
    source_session = 'task-execution:' + execution_id
    with closing(ledger._connect()) as conn:
        if conn.execute('SELECT 1 FROM turn_sources WHERE turn_id=?', (source_id,)).fetchone():
            return 'retained'
        if conn.execute('SELECT 1 FROM source_erasures WHERE turn_id=?', (source_id,)).fetchone():
            return 'source_erased'
    from protagine.turns.source_read import input_excerpt
    from protagine.turns.idempotency import SourceErased
    try:
        request = input_excerpt(ledger, contact_id=owner, session_id=source_session,
                                refs=data['input_refs'], max_chars=640)
    except ValueError:
        return 'source_unavailable'
    if request.get('status') != 'admitted_input_excerpt':
        return 'source_unavailable'
    facts = {'version': VERSION, 'execution_id': execution_id, 'task': selection,
        'platform': row['platform'], 'session_id': row['session_id'], 'turn_id': row['turn_id'],
        'state': row['state'], 'first_observed_at': row['first_observed_at'],
        'ended_at': row['last_observed_at'],
        'duration_seconds': max(0, row['last_observed_at'] - row['first_observed_at']),
        'measurement': 'first_turn_observation_to_terminal',
        'input_coverage': 'admitted inputs at turn start; later steering is not observed here',
        'request_input': {key: value for key, value in request.items() if key != '_provenance'},
        'processor': _processor(data, _config(data.get('first_api', {}).get('event', {})), FORECAST_VERSION),
        'output_quality': 'unobserved', 'owner_approval': 'unobserved'}
    message = {'role': 'assistant', '_native_runtime_observation': 'native-runtime-observation-v1',
        '_task_execution_outcome': VERSION, '_supplied_inputs': data['input_refs'],
        # Like duration receipts, this makes no lexical/vector memory chunks.
        # Source handles still open the exact metadata through the source API.
        'content': '', '_task_execution_facts': facts}
    try:
        ledger.record_source(source_id, contact_id=owner, session_id=source_session,
            occurred_at=datetime.fromtimestamp(row['last_observed_at'], timezone.utc).isoformat(),
            messages=[message], derive_claims=False, runtime_judgment=True)
    except SourceErased:
        return 'source_erased'
    return 'retained'


def safe_observe(registry, execution_id, owner):
    try:
        result = observe(registry, execution_id, owner)
        if result is not None:
            with closing(registry.ledger._connect()) as conn, conn:
                conn.execute("UPDATE execution_runtime_observations SET metadata_json="
                    "json_set(metadata_json,'$.judgment_outcome_settled',?) WHERE execution_id=?",
                    (result, execution_id))
        return result
    except Exception as error:
        logger.warning('Task outcome observation unavailable: %s', type(error).__name__)
        return None


def reconcile(registry, owner):
    """Replay at most twenty marked terminals on existing callbacks/owner reads.

    A failed source/queue transaction remains retryable. Losing the settled
    optimization after commit reuses the first source and never votes twice.
    Unmarked historical executions and internal Kanban reviews are excluded.
    """
    if not _enabled(owner):
        return
    try:
        with closing(registry.ledger._connect()) as conn:
            ids = [row[0] for row in conn.execute(
                "SELECT e.execution_id FROM execution_observations e "
                "JOIN execution_runtime_observations r USING(execution_id) "
                "WHERE e.contact_id=? AND e.state!='observed' AND e.last_observed_at>=? "
                "AND json_extract(r.metadata_json,'$.task_experience') IS NOT NULL "
                "AND json_extract(r.metadata_json,'$.judgment_outcome_settled') IS NULL "
                "ORDER BY e.last_observed_at,e.execution_id LIMIT 20", (owner, registry.clock()-7*86400))]
        for execution_id in ids:
            safe_observe(registry, execution_id, owner)
    except Exception as error:
        logger.warning('Task outcome reconciliation unavailable: %s', type(error).__name__)
