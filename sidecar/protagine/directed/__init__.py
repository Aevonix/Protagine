"""Directed action: owner directive -> gated, delegated, audited execution.

Option A architecture: Protagine never mutates external systems itself. It
scopes the directive deterministically, gates it (boundaries first, then
approval), dispatches the contract to an env-configured delegate, audits
what actually happened against the granted scope, and reports back through
the guarded reach-out path.
"""

from protagine.directed.models import (
    ScopedTask, ScopedTaskStore, ScopeLimits, READ_OPS, MUTATE_OPS,
)
from protagine.directed.intake import scope_from_directive, resolve_targets
from protagine.directed.audit import audit_completion, audit_via_report
from protagine.directed.service import DirectedActionService, directed_mode

__all__ = [
    "ScopedTask", "ScopedTaskStore", "ScopeLimits", "READ_OPS", "MUTATE_OPS",
    "scope_from_directive", "resolve_targets",
    "audit_completion", "audit_via_report",
    "DirectedActionService", "directed_mode",
]
