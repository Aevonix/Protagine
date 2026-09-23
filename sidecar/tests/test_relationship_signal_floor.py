"""Relationship provenance + signal floor, honest why_it_helps, and the
outbound third-party delivery gate (relationship-perspective fix)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from protagine.intelligence.relationships import signal_floor as sf


# ---------------------------------------------------------------------------
# Item 1 + 2: provenance (direct interlocutors only) + signal floor
# ---------------------------------------------------------------------------

def test_identical_legacy_scores_cannot_exclude_direct_interlocutors(monkeypatch):
    """Composite closeness no longer measures relationship usefulness."""
    monkeypatch.delenv("PROTAGINE_RELATIONSHIP_MIN_EXCHANGES", raising=False)
    monkeypatch.delenv("PROTAGINE_RELATIONSHIP_MAX_IDENTICAL", raising=False)
    batch = [
        {"entity_id": f"p{i}", "name": f"Person {i}", "interaction_count": 9,
         "relationship_score": 0.226}
        for i in range(6)
    ]
    survivors = sf.filter_relationship_candidates(batch)
    assert len(survivors) == 6
    assert all('relationship_score' not in row for row in survivors)
    assert all('relationship_score' in row for row in batch)


def test_passively_observed_third_party_dropped(monkeypatch):
    """No direct-exchange evidence => passively observed => out of scope."""
    monkeypatch.delenv("PROTAGINE_RELATIONSHIP_MIN_EXCHANGES", raising=False)
    cands = [
        {"entity_id": "obs", "name": "Observed", "relationship_score": 0.5},  # no count
        {"entity_id": "few", "name": "Barely", "interaction_count": 1,
         "relationship_score": 0.6},  # below floor of 3
    ]
    assert sf.filter_relationship_candidates(cands) == []


def test_genuine_direct_interlocutor_survives(monkeypatch):
    monkeypatch.delenv("PROTAGINE_RELATIONSHIP_MIN_EXCHANGES", raising=False)
    monkeypatch.delenv("PROTAGINE_RELATIONSHIP_MAX_IDENTICAL", raising=False)
    cands = [
        {"entity_id": "real", "name": "Real Friend", "interaction_count": 12,
         "relationship_score": 0.71},
        {"entity_id": "real2", "name": "Other", "interaction_count": 5,
         "relationship_score": 0.33},
    ]
    survivors = sf.filter_relationship_candidates(cands)
    assert {c["entity_id"] for c in survivors} == {"real", "real2"}


def test_score_history_is_not_relationship_evidence():
    cands = [
        {"entity_id": "a", "interaction_count": 8, "relationship_score": 0.4,
         "score_events": 1},   # legacy history must not govern selection
        {"entity_id": "b", "interaction_count": 8, "relationship_score": 0.5,
         "score_events": 4},
    ]
    survivors = sf.filter_relationship_candidates(cands)
    assert {c["entity_id"] for c in survivors} == {"a", "b"}
    assert all(not {"score_events", "relationship_score"} & row.keys() for row in survivors)


def test_enrich_pulls_interaction_count_from_contact_store():
    class FakeStore:
        async def get(self, cid):
            if cid == "known":
                return SimpleNamespace(interaction_count=7, score=0.4)
            return None
        async def find_by_person_node_id(self, cid):
            return None
    cands = [{"entity_id": "known"}, {"entity_id": "unknown"}]
    asyncio.run(sf.enrich_interaction_counts(cands, FakeStore()))
    assert cands[0]["interaction_count"] == 7
    assert "interaction_count" not in cands[1]  # unresolved -> stays observed


# ---------------------------------------------------------------------------
# Item 4: honest why_it_helps (grounded or does not ship)
# ---------------------------------------------------------------------------

def test_ungrounded_thought_does_not_ship():
    from protagine.proposals.engine import build_from_thinker
    for rationale in ("", "I think this work is worth doing now.",
                      "moves a piece of your work forward"):
        init = SimpleNamespace(description="Do a thing", rationale=rationale,
                               type="task", priority=0.6)
        assert build_from_thinker(init) is None


def test_grounded_thought_ships_with_evidence_based_why():
    from protagine.proposals.engine import build_from_thinker
    init = SimpleNamespace(
        description="Draft migration plan",
        rationale="The auth service still uses the deprecated v1 token format, "
                  "which breaks next month. Migrating now avoids an outage.",
        type="task", priority=0.7)
    prop = build_from_thinker(init)
    assert prop is not None
    assert "moves a piece of your work forward" not in prop.why_it_helps
    assert prop.why_it_helps  # grounded, from the rationale
    assert "deprecated v1 token format" in prop.finding


def test_research_without_goal_or_finding_does_not_ship():
    from protagine.proposals.engine import build_from_research
    assert build_from_research("", "some finding", []) is None
    assert build_from_research("a goal", "", []) is None


def test_research_with_evidence_ships_grounded():
    from protagine.proposals.engine import build_from_research
    prop = build_from_research("best vector DB for us", "Qdrant fits.",
                               [{"title": "bench", "url": "http://x"}])
    assert prop is not None
    assert "best vector DB for us" in prop.why_it_helps


# ---------------------------------------------------------------------------
# Item 3: outbound third-party delivery gate
# ---------------------------------------------------------------------------


