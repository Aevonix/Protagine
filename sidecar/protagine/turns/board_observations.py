"""The Hermes board as the body observed it, for the owner's work view.

The plugin posts board observations to ``POST /v1/mind/observations``
(stale owner tasks, blocked tasks, goal-mode tasks and the mind's own
tasks). This reader projects the last observation into the shape the work
view renders; the sidecar never opens Hermes' kanban database itself.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

_TERMINAL = ('done', 'cancelled', 'archived')
_COVERAGE = ('board observations posted by the body at its last tick; task records and goal '
             'budgets, not process liveness or verified external effects')


def _observations():
    try:
        from protagine.api.routers.mind import get_mind
    except Exception:
        return None
    return get_mind()


def _item(entry: Dict[str, Any], board: str, now: float) -> Dict[str, Any]:
    idle = entry.get('idle_s')
    age = entry.get('age_s')
    status = str(entry.get('status') or 'unknown')
    return {'native_board': board, 'native_task_id': str(entry.get('id') or ''),
            'label': str(entry.get('title') or '')[:128], 'status': status,
            'assignee': str(entry.get('assignee') or '')[:128],
            'goal_mode': bool(entry.get('goal') or entry.get('goal_mode')), 'goal_max_turns': entry.get('goal_max_turns'),
            'native_run_id': None, 'native_run_status': None,
            'created_at': (now - float(age)) if isinstance(age, (int, float)) else None,
            'started_at': None, 'completed_at': None, 'terminal_record_at': None,
            'heartbeat_age_seconds': round(float(idle), 1) if isinstance(idle, (int, float)) else None,
            'block_kind': entry.get('block_kind'), 'intention_id': entry.get('intention_id'),
            'liveness': 'native_terminal_record' if status in _TERMINAL else 'unknown'}


def kanban_view(*, limit: int = 8, now: float | None = None, read_budget: float = .2) -> Dict[str, Any]:
    """Owner-only projection of the body's last board observation; nothing is read from Hermes."""
    view: Dict[str, Any] = {'source': 'mind_board_observations', 'available': False, 'items': [], 'recent': [],
                            'complete': False, 'boards': [], 'coverage': _COVERAGE}
    mind = _observations()
    if mind is None:
        return {**view, 'reason': 'mind_not_running'}
    observed_at = getattr(mind, 'observed_at', None)
    if observed_at is None:
        return {**view, 'reason': 'no_board_observation_yet'}
    limit = max(1, min(int(limit), 100))
    now = time.time() if now is None else now
    board = 'default'
    groups = getattr(mind, 'observations', {}) or {}
    seen: set = set()
    active: List[Dict[str, Any]] = []
    for kind in ('goal', 'blocked_task', 'stale_task', 'mind_task'):
        for entry in groups.get(kind, []):
            if not isinstance(entry, dict) or not entry.get('id') or entry['id'] in seen:
                continue
            seen.add(entry['id'])
            active.append(_item(entry, board, now))
    counts = {str(k): int(v) for k, v in (getattr(mind, 'board_counts', {}) or {}).items()}
    total = sum(count for state, count in counts.items() if state not in _TERMINAL) or len(active)
    view.update(selection='body_observation', observed_at=observed_at.isoformat(),
                boards=[{'board': board, 'available': True, 'total': total}], available=True, partial=False)
    return {**view, 'items': active[:limit], 'recent': [], 'total': total, 'recent_total': 0,
            'truncated': total > len(active[:limit]), 'recent_truncated': False, 'limit': limit,
            'state_counts': counts, 'recent_window_seconds': 7 * 86400}


__all__ = ['kanban_view']
