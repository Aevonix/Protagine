"""Self-model: the competence store, the action journal, the brief and the live load."""

from __future__ import annotations

import time

from protagine.self_model import ActionJournal, CompetenceStore, SelfModel, self_brief


def _fill(store, domain, wins=0, losses=0, timeouts=0, shadow=False):
    for _ in range(wins):
        store.record(domain, "success", shadow=shadow)
    for _ in range(losses):
        store.record(domain, "failure", shadow=shadow)
    for _ in range(timeouts):
        store.record(domain, "timeout", shadow=shadow)


# ---------------------------------------------------------------------------
# CompetenceStore
# ---------------------------------------------------------------------------

def test_record_math_and_rates():
    s = CompetenceStore()
    _fill(s, "research", wins=4, losses=1)
    d = s.get("research")
    assert d["n"] == 5
    assert d["success_rate"] == 0.8
    assert d["timeout_rate"] == 0.0


def test_ewma_latency():
    s = CompetenceStore()
    s.record("x", "success", latency_secs=10.0)
    assert s.get("x")["ewma_latency_secs"] == 10.0
    s.record("x", "success", latency_secs=20.0)
    # 0.3 * 20 + 0.7 * 10 = 13
    assert abs(s.get("x")["ewma_latency_secs"] - 13.0) < 0.01


def test_events_windowing_and_shadow_flag():
    s = CompetenceStore()
    s.record("d", "success", shadow=True)
    s.record("d", "failure")
    assert len(s.events("d")) == 2
    assert len(s.events("d", include_shadow=False)) == 1
    assert s.events("d", since=time.time() + 10) == []


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

def test_brief_reports_runtime_counts_without_claiming_ability():
    s = CompetenceStore()
    _fill(s, "research", wins=8, losses=1)
    _fill(s, "scheduling", wins=1, losses=3)
    _fill(s, "coding", wins=2, timeouts=2)
    text = self_brief(s.snapshot(), {"total": 2, "active_initiatives": 2})
    assert "research: 8 labeled success, 1 failure, 0 timeout" in text
    assert "scheduling: 1 labeled success, 3 failure, 0 timeout" in text
    assert "coding: 2 labeled success, 0 failure, 2 timeout" in text
    assert "do not verify output quality" in text and "current model's ability" in text
    assert "You reliably complete" not in text and "You often fail at" not in text
    assert "2 initiative(s) in flight" in text


def test_brief_empty_without_evidence():
    assert self_brief([], {"total": 0}) == ""


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def test_journal_roundtrip_and_today():
    j = ActionJournal()
    jid = j.record("directed:read", "audited a repo",
                   reasoning="read-only auto", confidence=0.7,
                   decision="acted", ref="stask-1")
    assert jid > 0
    j.set_outcome(jid, "clean")
    entries = j.today()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "clean"
    assert j.recent(domain="directed:read")[0]["ref"] == "stask-1"
    assert j.recent(domain="other") == []


# ---------------------------------------------------------------------------
# Load + status
# ---------------------------------------------------------------------------

class _FakeInitStore:
    def count(self, status=None):
        return 2


class _FakeReg:
    initiative_store = _FakeInitStore()
    project_engine = None
    task_queue = None


def test_load_counts_live_reads():
    sm = SelfModel(CompetenceStore(), registry=_FakeReg())
    load = sm.load()
    assert load["active_initiatives"] == 2
    assert load["total"] == 2
