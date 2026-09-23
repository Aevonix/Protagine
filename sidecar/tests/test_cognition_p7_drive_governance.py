"""P7: owner-visible drives and immutable charter governance.

These are contract tests for a deliberately non-executing ranking layer.  A
drive score may order already-eligible goals; it can never create eligibility
or replace P3's charter, boundary, situation, duplicate, and authority gates.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import stat
import threading

import pytest

from onekey import RequestAuthority
from protagine.cognition.drive_governance import (
    CharterRevisionV1,
    DriveGovernance,
    DriveGovernanceError,
    DriveGovernanceStore,
    DriveRanker,
    DriveSignalV1,
    DriveV1,
    GoalRankInputV1,
    RankingBudgetV1,
    ScopeV1,
    drive_governance_mode,
)
from protagine.cognition.goal_spine import (
    CognitionSpineStore,
    PolicyDecisionV1,
)


NOW = datetime(2026, 7, 12, 14, 0, tzinfo=timezone.utc)
OWNER_SCOPE = ScopeV1(
    subject_person_id="person-owner",
    viewer_scope="owner",
    shareability="owner_private",
)


class BoundaryManager:
    def __init__(self, *, allowed=True, reason="no_active_boundaries"):
        self.allowed = allowed
        self.reason = reason
        self.checked = []

    def check(self, action):
        self.checked.append(action)
        return type("Verdict", (), {
            "allowed": self.allowed,
            "reason": self.reason,
        })()


def drive(
    key="owner_outcomes",
    *,
    state="enabled",
    expires_at=None,
    maximum=0.8,
):
    return DriveV1.create(
        key=key,
        version="v1",
        title=key.replace("_", " ").title(),
        definition_summary=f"Prefer evidence-backed progress for {key}",
        max_abs_contribution=maximum,
        max_signals_per_goal=3,
        state=state,
        scope=OWNER_SCOPE,
        evidence_refs=("directive:owner-charter",),
        created_at=NOW,
        expires_at=expires_at,
    )


def charter(
    drives,
    *,
    charter_key="default",
    parent=None,
    label="owner-charter-v1",
    expires_at=None,
    budget=None,
    scope=OWNER_SCOPE,
):
    weights = {item.drive_id: round(0.8 / len(drives), 4) for item in drives}
    return CharterRevisionV1.create(
        charter_key=charter_key,
        revision_label=label,
        parent_revision_id=parent,
        title="Owner charter",
        purpose_summary="Rank bounded goals toward the owner's stated outcomes",
        principles=(
            "Respect explicit boundaries and consent",
            "Prefer verified, reversible progress",
        ),
        drive_weights=weights,
        ranking_budget=budget or RankingBudgetV1(
            max_goals=20,
            max_signals_per_drive=3,
            max_total_signals=60,
            max_evidence_refs_per_goal=12,
        ),
        scope=scope,
        evidence_refs=("directive:owner-charter",),
        proposed_by="model:charter-drafter",
        proposed_at=NOW,
        expires_at=expires_at or NOW + timedelta(days=90),
    )


def register_and_propose(governance, drives, revision):
    for index, item in enumerate(drives):
        result = governance.register_drive(
            item, operation_id=f"drive-operation-{index:02d}"
        )
        assert result["status"] in {"drive_registered", "drive_replayed"}
    result = governance.propose_charter(
        revision, operation_id=f"proposal-{revision.revision_id}"
    )
    assert result["lifecycle_status"] == "proposed"


def policy_rows(proposal_id, *, deny=None):
    rows = {}
    for stage in ("charter", "boundary", "situation", "duplicate", "authority"):
        ref = f"policy-decision:{proposal_id}:{stage}"
        rows[ref] = {
            "decision_ref": ref,
            "proposal_id": proposal_id,
            "stage": stage,
            "allowed": stage != deny,
            "reason": "accepted" if stage != deny else f"{stage}_denied",
            "evidence_refs": [f"gate-evidence:{stage}"],
        }
    return rows


def goal(name, *, proposal=None, scope=OWNER_SCOPE, decision_refs=()):
    proposal = proposal or f"goal-proposal:{name}"
    return GoalRankInputV1(
        goal_id=f"project:{name}",
        proposal_id=proposal,
        goal_fingerprint=f"fingerprint-{name}",
        title=f"Goal {name}",
        objective_summary=f"Produce a verified outcome for {name}",
        rationale_summary=f"Evidence indicates {name} should be evaluated",
        evidence_refs=(f"event:{name}",),
        policy_decision_refs=tuple(decision_refs),
        scope=scope,
    )


def signal(item, candidate, value, *, suffix="1", state="active", scope=None):
    return DriveSignalV1.derive(
        drive=item,
        goal_fingerprint=candidate.goal_fingerprint,
        normalized_value=value,
        confidence=0.9,
        state=state,
        rationale_summary=f"Verified signal {suffix} for {candidate.goal_id}",
        evidence_refs=(f"receipt:{candidate.goal_id}:{suffix}",),
        observed_at=NOW,
        expires_at=NOW + timedelta(hours=6),
        scope=scope or item.scope,
    )


def test_mode_is_default_off_and_invalid_values_fail_off(monkeypatch):
    monkeypatch.delenv("PROTAGINE_DRIVE_GOVERNANCE_MODE", raising=False)
    assert drive_governance_mode() == "off"
    monkeypatch.setenv("PROTAGINE_DRIVE_GOVERNANCE_MODE", "surprise")
    assert drive_governance_mode() == "off"
    monkeypatch.setenv("PROTAGINE_DRIVE_GOVERNANCE_MODE", "shadow")
    assert drive_governance_mode() == "shadow"
    monkeypatch.setenv("PROTAGINE_DRIVE_GOVERNANCE_MODE", "live")
    assert drive_governance_mode() == "live"
    monkeypatch.setenv("PROTAGINE_DRIVE_GOVERNANCE_MODE", "bootstrap")
    assert drive_governance_mode() == "bootstrap"


def test_off_mode_creates_no_governance_state(tmp_path):
    path = tmp_path / "nested" / "drive.db"
    governance = DriveGovernance.lazy(path, mode="off")
    result = governance.register_drive(
        drive(), operation_id="drive-operation-off-01"
    )
    assert result == {"enabled": False, "status": "off"}
    assert not path.exists()


def test_store_is_private_additive_and_passes_sqlite_integrity_check(tmp_path):
    path = tmp_path / "drive.db"
    store = DriveGovernanceStore(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert store._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    tables = {
        row[0] for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "drive_definitions", "drive_signals", "charter_revisions",
        "charter_lifecycle_events", "charter_authority_uses",
    } <= tables


def test_scope_narrowing_never_adds_a_viewer_lane():
    public = ScopeV1("person-a", "public", "public")
    shared = ScopeV1("person-a", "shared", "shared")
    subject = ScopeV1("person-a", "person:person-a", "subject_private")
    owner = ScopeV1("person-a", "owner", "owner_private")
    assert public.permits_child(shared)
    assert public.permits_child(subject)
    assert shared.permits_child(owner)
    assert not shared.permits_child(subject)
    assert subject.permits_child(owner)
    assert not owner.permits_child(subject)


def test_global_pause_wins_even_without_an_active_charter(tmp_path):
    store = DriveGovernanceStore(tmp_path / "empty-drive.db")
    candidate = goal("pause-before-charter")
    batch = DriveRanker(
        store,
        policy_decision_resolver=lambda _ref: None,
        directive_manager=BoundaryManager(
            allowed=False, reason="global_pause_active",
        ),
    ).rank([candidate], mode="live", now=NOW)
    assert batch.status == "global_pause_active"
    assert batch.charter_revision_id is None
    assert batch.effective_order == ()


