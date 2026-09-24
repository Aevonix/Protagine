"""AdaptiveParamStore: registered bounds, clamping, reset and journaling.

The store only ever moves a parameter within the bounds its consumer
registered; the parameters here belong to the test.
"""

import pytest

from protagine.self_model.journal import ActionJournal
from protagine.self_model.params import AdaptiveParamStore

PARAM_MERGE = "fixture.merge_threshold"
PARAM_FLOOR = "fixture.relevance_floor"


def register(store: AdaptiveParamStore) -> None:
    store.register(PARAM_MERGE, default=0.92, lo=0.85, hi=0.98,
                   description="fixture merge threshold (floor 0.85)")
    store.register(PARAM_FLOOR, default=0.0, lo=0.0, hi=0.5,
                   description="fixture relevance floor (cap 0.5)")


@pytest.fixture()
def store() -> AdaptiveParamStore:
    s = AdaptiveParamStore()
    register(s)
    return s


class TestStore:
    def test_registered_default(self, store):
        assert store.get(PARAM_MERGE) == pytest.approx(0.92)
        assert store.get(PARAM_FLOOR) == pytest.approx(0.0)

    def test_set_and_get(self, store):
        applied = store.set(PARAM_FLOOR, 0.35, reason="test")
        assert applied == pytest.approx(0.35)
        assert store.get(PARAM_FLOOR) == pytest.approx(0.35)

    def test_set_clamps_to_bounds(self, store):
        # A self-adjustment can never move a parameter past its registered
        # bounds, whatever the writer requests.
        applied = store.set(PARAM_MERGE, 0.5, reason="bad")
        assert applied == pytest.approx(0.85)
        applied = store.set(PARAM_FLOOR, 0.9, reason="bad")
        assert applied == pytest.approx(0.5)

    def test_unregistered_param_refused(self, store):
        assert store.set("made.up.knob", 1.0) is None
        assert store.get("made.up.knob", default=7.0) == pytest.approx(7.0)

    def test_reset_restores_default(self, store):
        store.set(PARAM_FLOOR, 0.4)
        store.reset(PARAM_FLOOR)
        assert store.get(PARAM_FLOOR) == pytest.approx(0.0)

    def test_reregistration_keeps_value_but_reclamps(self, store):
        store.set(PARAM_FLOOR, 0.4)
        store.register(PARAM_FLOOR, default=0.0, lo=0.0,
                       hi=0.2, description="narrowed")
        assert store.get(PARAM_FLOOR) == pytest.approx(0.2)

    def test_set_is_journaled(self):
        journal = ActionJournal()
        s = AdaptiveParamStore(journal=journal)
        register(s)
        s.set(PARAM_FLOOR, 0.3, reason="semantic_mismatch gap")
        entries = journal.recent(limit=5)
        assert any(e["domain"] == "meta_learning" and
                   PARAM_FLOOR in (e.get("description") or "")
                   for e in entries)

    def test_snapshot_reports_effective(self, store):
        store.set(PARAM_FLOOR, 0.25)
        snap = {p["name"]: p for p in store.snapshot()}
        assert snap[PARAM_FLOOR]["effective"] == pytest.approx(0.25)
        assert snap[PARAM_MERGE]["effective"] == pytest.approx(0.92)
