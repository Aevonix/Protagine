"""Inspect/reconcile the retired built-in executor's existing queue, offline.

This command never dispatches work. Stop the old executor and back up state
before applying. Native task owners, results and effect receipts are preserved.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .native_work import contract

MIGRATION = 'retire-builtin-executor-v1'
# Statuses that would run again: the generator reactivates a failed row when
# the same work is proposed again, so a failed row is not a settled record.
REPLAYABLE = {'pending', 'assigned', 'acknowledged', 'failed'}


def _pending_path(row):
    """Identify existing consumers without authorizing or scheduling anything."""
    from protagine.commitments.local_work import CREATOR, SOURCE
    from protagine.delivery.classification import is_reachout
    if row['created_by'] == CREATOR and row['source_type'] == SOURCE:
        return 'existing_native_local_work'
    try:
        contract(row)
        return 'existing_native_review'
    except (KeyError, TypeError, ValueError):
        pass
    if is_reachout(row['type']):
        return 'existing_delivery_policy'
    return 'proposal_only_native_execution_capability_missing'


def reconcile(state_dir: Path, *, executor_ids=('protagine-executor',),
              apply=False, executor_stopped=False):
    """Return one disposition per row; optionally record retirement atomically.

    Historical IDs must be supplied explicitly. A terminal runtime status alone
    does not establish quality, but existing effect/result evidence is retained.
    """
    if apply and not executor_stopped:
        raise ValueError('stop_old_executor_before_applying')
    executor_ids = tuple(sorted(set(executor_ids)))
    if not executor_ids or any(not name.strip() for name in executor_ids):
        raise ValueError('explicit_executor_identity_required')
    path = Path(state_dir) / 'initiatives.db'
    # Do not create an empty database or run recovery on a missing/corrupt one.
    mode = 'rw' if apply else 'ro'
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=' + mode, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        try:
            placeholders = ','.join('?' for _ in executor_ids)
            old_ids = {r[0] for r in db.execute(
                f'SELECT DISTINCT initiative_id FROM assignment_history WHERE agent_id IN ({placeholders})',
                executor_ids)}
            recorded = {r[0] for r in db.execute(
                'SELECT initiative_id FROM assignment_history WHERE action=?', (MIGRATION,))}
            report = []
            for row in db.execute('SELECT * FROM initiatives ORDER BY id').fetchall():
                owner, status = row['assigned_agent_id'], row['status']
                was_retired = owner in executor_ids or row['id'] in old_ids
                detail = {'id': row['id'], 'status': status, 'assigned_agent_id': owner}
                if owner and owner not in executor_ids:
                    detail['disposition'] = 'preserve_other_owner'
                elif row['id'] in recorded and status not in REPLAYABLE:
                    detail['disposition'] = 'already_reconciled'
                elif was_retired:
                    detail.update(disposition=('reconcile_effect_before_retry' if status in REPLAYABLE
                                               else 'preserve_terminal_record'),
                                  runtime_status_proves_quality=False, quality_credit=False)
                    if apply:
                        if status in REPLAYABLE:
                            now = datetime.now(timezone.utc).isoformat()
                            # Cancellation blocks replay of this ID; the failure
                            # reason and time stay on the row as evidence.
                            db.execute('''UPDATE initiatives SET status='cancelled', cancelled_at=?,
                                cancelled_by=?, cancelled_reason='executor_retired_effect_unverified',
                                recovery_reason='Reconcile prior effects before explicit native retry'
                                WHERE id=?''', (now, MIGRATION, row['id']))
                        db.execute('''INSERT INTO assignment_history
                            (initiative_id,agent_id,agent_name,action,details) VALUES (?,?,?,?,?)''',
                            (row['id'], MIGRATION, 'Executor retirement', MIGRATION,
                             json.dumps(detail, sort_keys=True)))
                elif status == 'pending':
                    detail['disposition'] = _pending_path(row)
                else:
                    detail['disposition'] = 'preserve_unowned_record'
                report.append(detail)
            if apply:
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise
    return {'schema': MIGRATION, 'applied': apply, 'executor_ids': list(executor_ids),
            'total': len(report), 'counts': dict(Counter(r['disposition'] for r in report)),
            'rows': report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--executor-id', action='append', help='Repeat for historical executor IDs')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--executor-stopped', action='store_true')
    args = parser.parse_args()
    print(json.dumps(reconcile(args.state_dir, executor_ids=args.executor_id or ('protagine-executor',),
        apply=args.apply, executor_stopped=args.executor_stopped), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
