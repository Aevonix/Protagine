"""H1.1: the generic supervised-live rung (self_model/supervised.py).

Locks: PROTAGINE_SUPERVISED_LIVE_DOMAINS defaults empty (rung off everywhere);
reversible() fails CLOSED on unknown domains and unlisted operations, and
answers False everywhere while no domain has registered a contract;
effective_mode degrades to the env mode on trust errors and never upgrades
past what stage + flag earn.
"""

import pytest

from protagine.self_model import supervised
from protagine.self_model.supervised import (
    REVERSIBLE_CONTRACT, effective_mode, reversible, supervised_domains,
    supervised_enabled,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PROTAGINE_SUPERVISED_LIVE_DOMAINS", raising=False)


# --- flag parsing ------------------------------------------------------------

def test_default_no_domains_supervised():
    """Regression lock: with nothing set, the rung is off for every domain."""
    assert supervised_domains() == frozenset()
    assert not supervised_enabled("research")
    assert not supervised_enabled("goals")
    assert not supervised_enabled("")


def test_generic_flag_parses_csv(monkeypatch):
    monkeypatch.setenv("PROTAGINE_SUPERVISED_LIVE_DOMAINS", " Research, goals ,")
    assert supervised_domains() == {"research", "goals"}
    assert supervised_enabled("research")
    assert supervised_enabled("GOALS")
    assert not supervised_enabled("delivery")


# --- reversibility contract: fail-closed ---------------------------------------

def test_reversible_contract_is_empty_and_fails_closed():
    assert REVERSIBLE_CONTRACT == {}
    assert not reversible("research", "append")
    assert not reversible("", "append")
    assert not reversible("research", "")


def test_registered_operations_are_the_only_reversible_ones(monkeypatch):
    monkeypatch.setitem(supervised.REVERSIBLE_CONTRACT, "fixture", frozenset({"append"}))
    assert reversible("fixture", "append")
    assert reversible("Fixture", " APPEND ")
    # unlisted op in a known domain: non-reversible
    assert not reversible("fixture", "delete")
    # unknown domain: non-reversible, whatever the op claims
    assert not reversible("other", "append")
    # the contract only ever names operations, never wildcards
    assert all("*" not in op for ops in REVERSIBLE_CONTRACT.values() for op in ops)


# --- effective_mode ------------------------------------------------------------

class _Trust:
    def __init__(self, stage):
        self._stage = stage

    def stage(self, domain, default="shadow"):
        return self._stage


class _BrokenTrust:
    def stage(self, domain, default="shadow"):
        raise RuntimeError("trust db unavailable")


def test_env_override_wins():
    assert effective_mode("research", "off", _Trust("act_first")) == "off"
    assert effective_mode("research", "live", _Trust("shadow")) == "live"


def test_no_trust_degrades_to_env_mode():
    assert effective_mode("research", "shadow", None) == "shadow"


def test_trust_error_degrades_to_env_mode(monkeypatch):
    monkeypatch.setenv("PROTAGINE_SUPERVISED_LIVE_DOMAINS", "research")
    assert effective_mode("research", "shadow", _BrokenTrust()) == "shadow"


def test_stage_ladder(monkeypatch):
    # rung off: ask_first stays shadow (the historical catch-22 posture)
    assert effective_mode("research", "shadow", _Trust("ask_first")) == "shadow"
    # rung on: ask_first becomes supervised; act_first is live; shadow stays shadow
    monkeypatch.setenv("PROTAGINE_SUPERVISED_LIVE_DOMAINS", "research")
    assert effective_mode("research", "shadow", _Trust("shadow")) == "shadow"
    assert effective_mode("research", "shadow", _Trust("ask_first")) == "supervised"
    assert effective_mode("research", "shadow", _Trust("act_first")) == "live"
    # the flag only unlocks the listed domain
    assert effective_mode("goals", "shadow", _Trust("ask_first")) == "shadow"


# --- rung visibility (H1.4) -----------------------------------------------------

def test_trust_snapshot_shows_rung(monkeypatch):
    """TrustEngine.snapshot() (surfaced via GET /v1/host/self) carries the
    rung: supervised_enabled + effective_rung per domain."""
    from protagine.self_model import (
        ActionJournal, CompetenceStore, TrustEngine,
    )
    trust = TrustEngine(CompetenceStore(), journal=ActionJournal())
    trust.set_stage("research", "ask_first", notify=False)
    trust.set_stage("goals", "act_first", notify=False)

    snap = {r["domain"]: r for r in trust.snapshot()}
    assert snap["research"]["supervised_enabled"] is False
    assert snap["research"]["effective_rung"] == "ask_first"

    monkeypatch.setenv("PROTAGINE_SUPERVISED_LIVE_DOMAINS", "research")
    snap = {r["domain"]: r for r in trust.snapshot()}
    assert snap["research"]["supervised_enabled"] is True
    assert snap["research"]["effective_rung"] == "supervised"
    # the rung only ever refines ask_first; other stages pass through
    assert snap["goals"]["effective_rung"] == "act_first"
    assert snap["goals"]["supervised_enabled"] is False
