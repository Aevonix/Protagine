"""Selfhood benchmark (Mind M0a): store, derivations, honest skips, API."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import protagine.api.routers.host as host_mod
from protagine.contacts.comms import CommsLog
from protagine.intelligence.learning.feedback_store import FeedbackStore, UserCorrection
from protagine.self_model.benchmark import (
    BenchmarkStore, MetricDefinition, SelfhoodBenchmark, cognition_p4_mode, legacy_cpi_payload, previous_week,
    week_window,
)

WEEK = "2026-W26"
START, END = week_window(WEEK)
T0 = START.timestamp()


# --- fakes -----------------------------------------------------------------

class FakeCommitments:
    def list(self, status=None, limit=50, **kw):
        inside = (START + timedelta(days=1)).isoformat()
        outside = (START - timedelta(days=2)).isoformat()
        return {"commitments": [
            {"fulfilled_at": inside}, {"fulfilled_at": inside},
            {"fulfilled_at": outside},
        ]}

    def get_overdue(self):
        return [{"id": "c1"}]


class FakeCompetence:
    def snapshot(self):
        return [{"domain": "worker:research"}, {"domain": "delivery"}]

    def events(self, domain, since=None, include_shadow=True):
        if domain == "delivery":
            return [
                {"ts": T0 + 3600, "outcome": "success"},
                {"ts": T0 + 7200, "outcome": "success"},
                {"ts": T0 + 9000, "outcome": "success"},
                {"ts": T0 + 10800, "outcome": "failure"},
            ]
        return [
            {"ts": T0 + 3600, "outcome": "success"},
            {"ts": T0 + 7200, "outcome": "success"},
        ]


class FakeJournal:
    def recent(self, limit=50, domain=None, since=None):
        return ([{"ts": T0 + 100, "decision": "acted"}] * 3
                + [{"ts": T0 + 200, "decision": "asked"}]
                + [{"ts": T0 + 300, "decision": "noted"}] * 2)


class FakeComms:
    def inbound_since(self, contact_id, since_iso):
        assert contact_id == "cid-owner"
        # responds after the first two deliveries only
        from datetime import datetime, timezone
        return [
            datetime.fromtimestamp(T0 + 4000, tz=timezone.utc).isoformat(),
            datetime.fromtimestamp(T0 + 7500, tz=timezone.utc).isoformat(),
        ]


class FakeFacts:
    def list_facts(self, min_confidence=0.0, limit=100, **kw):
        return {"facts": [
            {"id": "f1", "fact": "the reranker service runs on port 8093"},
            {"id": "f2", "fact": "quarterly report deadline moved to friday"},
        ]}


class FakeGraph:
    async def recall(self, query, *, person_id=None, limit=5):
        if "reranker" in query:
            return [{"content": "the reranker service runs on port 8093 "
                                "behind the tunnel"}]
        return [{"content": "something entirely unrelated"}]


class FakeQueue:
    async def completed_durations(self, since_iso, until_iso, limit=1000):
        assert since_iso[:19] <= until_iso[:19]
        return [1.0, 2.0, 3.0, 100.0]


def make_bench(tmp_path, **overrides):
    store = BenchmarkStore(db_path=str(tmp_path / "bench.db"))
    deps = dict(
        commitments=FakeCommitments(), competence=FakeCompetence(),
        journal=FakeJournal(), comms=FakeComms(), recall=FakeGraph().recall,
        facts=FakeFacts(), queue=FakeQueue(),
        owner_contact_id="cid-owner", probes=2,
    )
    deps.update(overrides)
    return SelfhoodBenchmark(store, **deps)


# --- store -----------------------------------------------------------------

def test_store_sample_validation(tmp_path):
    s = BenchmarkStore(db_path=str(tmp_path / "b.db"))
    assert s.add_sample("latency.voice_ttfb_ms", 42.0)
    assert not s.add_sample("BadMetric", 1.0)
    assert not s.add_sample("noprefix", 1.0)
    assert not s.add_sample("latency.x", "nan-ish")  # type: ignore[arg-type]


def test_store_rollup_roundtrip(tmp_path):
    s = BenchmarkStore(db_path=str(tmp_path / "b.db"))
    s.write_rollup("2026-W25", "actions.success", 0.8, numerator=8,
                   denominator=10, detail={"domains": {}})
    s.write_rollup("2026-W26", "actions.success", 0.9, numerator=9,
                   denominator=10)
    rolls = s.rollups(weeks=8)
    assert list(rolls.keys()) == ["2026-W26", "2026-W25"]
    assert rolls["2026-W26"]["actions.success"]["value"] == 0.9


def test_week_helpers():
    start, end = week_window("2026-W26")
    assert start.isoweekday() == 1
    assert (end - start).days == 7
    assert previous_week(start) != "2026-W26"


# --- derivations -----------------------------------------------------------

async def test_compute_week_full(tmp_path):
    bench = make_bench(tmp_path)
    bench.store.add_sample("latency.voice_ttfb_ms", 30, ts=T0 + 50)
    bench.store.add_sample("latency.voice_ttfb_ms", 90, ts=T0 + 60)
    bench.store.add_sample("surface.wake_accuracy", 0.5, ts=T0 + 70)

    out = (await bench.compute_week(WEEK))["metrics"]

    assert out["commitments.fulfillment"]["value"] == pytest.approx(2 / 3)
    assert out["delivery.success"]["value"] == pytest.approx(0.75)
    # actions.success excludes the delivery domain
    assert out["actions.success"]["value"] == 1.0
    assert out["actions.success"]["detail"]["domains"] == {
        "worker:research": {"success": 2, "n": 2}}
    assert out["journal.acted_share"]["value"] == pytest.approx(0.75)
    # 2 of 3 delivery successes answered within 24h
    assert out["initiative.acceptance"]["value"] == pytest.approx(2 / 3)
    # one of two fact probes covered
    assert out["recall.fact_coverage"]["value"] == pytest.approx(0.5)
    assert out["latency.jobs_p50_secs"]["detail"]["n"] == 4
    assert out["latency.voice_ttfb_ms"]["detail"]["p95"] == 90
    assert out["surface.wake_accuracy"]["value"] == pytest.approx(0.5)

    # rollups persisted; probes recorded as samples
    rolls = bench.store.rollups()
    assert WEEK in rolls and "recall.fact_coverage" in rolls[WEEK]
    import time as _time
    probes = bench.store.samples_in(0, _time.time() + 10,
                                    metric="recall.probe")
    assert len(probes) == 2


async def test_compute_week_honest_skips(tmp_path):
    """Missing sources omit metrics; nothing is zero-filled."""
    store = BenchmarkStore(db_path=str(tmp_path / "b2.db"))
    bench = SelfhoodBenchmark(store, commitments=None, competence=None,
                              journal=None, comms=None, recall=None,
                              facts=None, queue=None,
                              owner_contact_id="", probes=2)
    # Force lazy resolution to find nothing rather than the real host globals
    bench._host_attr = staticmethod(lambda name: None)  # type: ignore
    out = (await bench.compute_week(WEEK))["metrics"]
    assert out == {}


async def test_acceptance_skipped_without_owner(tmp_path):
    bench = make_bench(tmp_path, owner_contact_id="")
    bench._host_attr = staticmethod(lambda name: None)  # type: ignore
    out = (await bench.compute_week(WEEK))["metrics"]
    assert "initiative.acceptance" not in out
    assert "delivery.success" in out


def test_snapshot_trends(tmp_path):
    s = BenchmarkStore(db_path=str(tmp_path / "b3.db"))
    bench = SelfhoodBenchmark(s)
    s.write_rollup("2026-W25", "actions.success", 0.6)
    s.write_rollup("2026-W26", "actions.success", 0.9)
    snap = bench.snapshot()
    assert snap["latest"] == "2026-W26"
    assert snap["trends"]["actions.success"] == pytest.approx(0.3)


# --- API -------------------------------------------------------------------

@asynccontextmanager
async def _client(bench):
    orig = host_mod._benchmark
    host_mod._benchmark = bench
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            yield c
    finally:
        host_mod._benchmark = orig


async def test_api_samples_and_snapshot(tmp_path):
    bench = make_bench(tmp_path)
    async with _client(bench) as c:
        r = await c.post("/v1/host/self/benchmark/samples", json={
            "samples": [
                {"metric": "latency.voice_ttfb_ms", "value": 33.0},
                {"metric": "NOT VALID", "value": 1.0},
            ],
            "source": "voice-gateway"})
        body = r.json()
        assert r.status_code == 200
        assert body["accepted"] == 1 and body["rejected"] == 1

        r = await c.get("/v1/host/self/benchmark")
        assert r.status_code == 200
        assert r.json()["available"] is True


async def test_api_unavailable():
    async with _client(None) as c:
        r = await c.get("/v1/host/self/benchmark")
        assert r.json() == {"available": False}
        r = await c.post("/v1/host/self/benchmark/samples",
                         json={"samples": []})
        assert r.json()["available"] is False


# --- on-demand recall probe (U0) --------------------------------------------

class ManyFakeFacts:
    """Enough facts that a seeded sample is a real subset."""

    def list_facts(self, min_confidence=0.0, limit=100, **kw):
        return {"facts": [
            {"id": f"f{i}", "fact": f"unique subject number{i} lives in "
                                    f"building{i} downtown"}
            for i in range(10)
        ]}


class HalfHitGraph:
    """Covers even-numbered facts only, and records every query."""

    def __init__(self):
        self.queries = []

    async def recall(self, query, *, person_id=None, limit=5):
        self.queries.append(query)
        import re
        m = re.search(r"number(\d+)", query)
        if m and int(m.group(1)) % 2 == 0:
            return [{"content": query + " extra context"}]
        return [{"content": "something entirely unrelated"}]


async def test_recall_probe_seeded_deterministic(tmp_path):
    g1, g2 = HalfHitGraph(), HalfHitGraph()
    b1 = make_bench(tmp_path, recall=g1.recall, facts=ManyFakeFacts())
    b2 = make_bench(tmp_path, recall=g2.recall, facts=ManyFakeFacts())
    r1 = await b1.run_recall_probe(probes=4, seed=42)
    r2 = await b2.run_recall_probe(probes=4, seed=42)
    assert r1 is not None and r2 is not None
    # Same seed -> identical fact picks and identical score
    assert g1.queries == g2.queries
    assert len(g1.queries) == 4
    assert r1["value"] == r2["value"]
    assert r1["denominator"] == 4
    assert r1["detail"]["seed"] == 42
    assert r1["detail"]["source"] == "manual-probe"


async def test_recall_probe_samples_excluded_from_rollups(tmp_path):
    bench = make_bench(tmp_path, recall=HalfHitGraph().recall, facts=ManyFakeFacts())
    await bench.run_recall_probe(probes=5, seed=7)
    import time as _time
    samples = bench.store.samples_in(0, _time.time() + 10,
                                     metric="recall.probe")
    assert len(samples) == 5
    assert all(s["source"] == "manual-probe" for s in samples)
    # The generic sample rollup never reads recall.probe samples, so a
    # manual probe run cannot leak into the weekly scorecard.
    submitted = bench._m_submitted(0, _time.time() + 10)
    assert "recall.probe" not in submitted


async def test_recall_probe_clamps_and_skips(tmp_path):
    bench = make_bench(tmp_path, recall=HalfHitGraph().recall, facts=ManyFakeFacts())
    r = await bench.run_recall_probe(probes=500, seed=1)
    assert r is not None and r["denominator"] == 10  # capped at 100, 10 facts
    # honest skip when a source is missing
    assert await make_bench(tmp_path, recall=None,
                            facts=ManyFakeFacts()).run_recall_probe() is None


async def test_weekly_recall_metric_unchanged_by_refactor(tmp_path):
    """Regression lock: the weekly recall.fact_coverage derivation still
    produces the pre-refactor result and source tag."""
    bench = make_bench(tmp_path)
    out = (await bench.compute_week(WEEK))["metrics"]
    assert out["recall.fact_coverage"]["value"] == pytest.approx(0.5)
    assert out["recall.fact_coverage"]["detail"] == {"probes": 2}
    import time as _time
    probes = bench.store.samples_in(0, _time.time() + 10,
                                    metric="recall.probe")
    assert {p["source"] for p in probes} == {"benchmark"}


async def test_api_recall_probe(tmp_path):
    bench = make_bench(tmp_path, recall=HalfHitGraph().recall, facts=ManyFakeFacts())
    async with _client(bench) as c:
        r = await c.post("/v1/host/self/benchmark/recall-probe",
                         json={"probes": 4, "seed": 42})
        body = r.json()
        assert r.status_code == 200
        assert body["available"] is True and body["ran"] is True
        assert body["denominator"] == 4
        assert body["detail"]["seed"] == 42
    async with _client(None) as c:
        r = await c.post("/v1/host/self/benchmark/recall-probe", json={})
        assert r.json() == {"available": False}


async def test_canonical_probe_recall_reads_the_scoped_source_ledger(tmp_path, monkeypatch):
    """The probe's production read path is the source ledger, scoped to the subject."""
    import protagine.identity as identity
    from protagine.self_model.benchmark import canonical_probe_recall
    from protagine.turns import TurnIdempotencyLedger

    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    ledger.record_source("owner-turn", contact_id="cid-owner", session_id="s1", derive_claims=False,
                         messages=[{"role": "user", "content": "the reranker service runs on port 8093"}])
    ledger.record_source("other-turn", contact_id="cid-other", session_id="s1", derive_claims=False,
                         messages=[{"role": "user", "content": "the reranker service moved to port 9000"}])
    recall = canonical_probe_recall(tmp_path)

    monkeypatch.setattr(identity, "get_owner_contact_id", lambda: "")
    assert await recall("reranker port") == []   # no owner, no subject: read nothing
    monkeypatch.setattr(identity, "get_owner_contact_id", lambda: "cid-owner")
    rows = await recall("reranker port")
    assert rows and all("8093" in row["content"] for row in rows)
    other = await recall("reranker port", person_id="cid-other")
    assert other and "9000" in other[0]["content"] and not any("8093" in row["content"] for row in other)


# --- P4 evidence, kept from the deleted experiment suite (M9) --------------------------------

METRIC = MetricDefinition(
    metric="quality.answer_accuracy",
    version="v1",
    direction="higher",
    unit="ratio",
    evidence_query="receipt.type=answer_grade AND receipt.verified=true",
    minimum_samples=6,
    description="Receipt-verified answer accuracy for an assigned exposure.",
)


def test_p4_flag_is_default_off(monkeypatch):
    monkeypatch.delenv("PROTAGINE_COGNITION_P4_MODE", raising=False)
    assert cognition_p4_mode() == "off"


def test_metric_definitions_are_immutable_and_samples_attested(tmp_path):
    store = BenchmarkStore(str(tmp_path / "benchmark.db"))
    first = store.register_definition(METRIC)
    assert first["definition_hash"]
    assert store.register_definition(METRIC) == first
    with pytest.raises(ValueError, match="immutable"):
        store.register_definition(MetricDefinition(
            **{**METRIC.__dict__, "evidence_query": "anything=true"}
        ))

    assert store.add_evidence_sample(
        METRIC.metric,
        0.75,
        definition_version=METRIC.version,
        sample_principal="verifier:answer-grader",
        source_ref="turn:abc",
        receipt_ref="receipt:abc",
        sample_id="sample-abc",
    )
    # Idempotent retry, not a duplicate sample.
    assert store.add_evidence_sample(
        METRIC.metric,
        0.75,
        definition_version=METRIC.version,
        sample_principal="verifier:answer-grader",
        source_ref="turn:abc",
        receipt_ref="receipt:abc",
        sample_id="sample-abc",
    )
    rows = store.evidence_samples_in(0, datetime.now(timezone.utc).timestamp() + 60)
    assert len(rows) == 1
    assert rows[0]["sample_principal"] == "verifier:answer-grader"
    assert rows[0]["receipt_ref"] == "receipt:abc"
    with pytest.raises(ValueError, match="registered metric definition"):
        store.add_evidence_sample(
            "made.up_metric", 1.0, definition_version="v1",
            sample_principal="verifier:test", source_ref="source:x",
        )


def test_benchmark_sqlite_migrations_are_additive(tmp_path):
    benchmark_path = tmp_path / "legacy-benchmark.db"
    with sqlite3.connect(benchmark_path) as conn:
        conn.executescript("""
            CREATE TABLE benchmark_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,value REAL NOT NULL,source TEXT NOT NULL,
                ts REAL NOT NULL,meta TEXT);
            CREATE TABLE benchmark_rollups (
                week TEXT NOT NULL,metric TEXT NOT NULL,value REAL,
                numerator REAL,denominator REAL,detail TEXT,
                computed_at REAL NOT NULL,PRIMARY KEY(week,metric));
            INSERT INTO benchmark_samples(metric,value,source,ts)
                VALUES('latency.jobs_p50_secs',1.5,'legacy',1.0);
        """)
    benchmark = BenchmarkStore(str(benchmark_path))
    legacy = benchmark.samples_in(0, 2)
    assert len(legacy) == 1 and legacy[0]["source"] == "legacy"
    assert legacy[0]["definition_version"] is None
    assert benchmark.definition("actions.success", "v2") is not None


def test_the_legacy_cpi_payload_points_to_the_benchmark(tmp_path):
    benchmark = SelfhoodBenchmark(BenchmarkStore(str(tmp_path / "b.db")))
    payload = legacy_cpi_payload(benchmark)
    assert payload["deprecated"] is True
    assert payload["canonical_endpoint"] == "/v1/host/self/benchmark"
    assert "memory" not in payload and "reasoning" not in payload


class _CommitmentCohort:
    def __init__(self, rows):
        self.rows = rows

    def list(self, **kwargs):
        return {"commitments": list(self.rows)}


class _DeliveryEvidence:
    def snapshot(self):
        return [{"domain": "delivery"}]

    def reconciliation_revision(self, **kwargs):
        return 0

    def active_evidence_gaps(self, *args, **kwargs):
        return []

    def events(self, domain, **kwargs):
        return list(self.rows) if domain == "delivery" else []

    def __init__(self, rows):
        self.rows = rows


def test_commitment_metric_uses_one_due_date_cohort(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_COGNITION_P4_MODE", "live")
    start, end = week_window("2026-W26")
    inside = start + timedelta(days=2)
    rows = [
        {"id": "on-time", "status": "fulfilled",
         "due_at": inside.isoformat(),
         "fulfilled_at": (inside - timedelta(hours=1)).isoformat()},
        {"id": "late", "status": "fulfilled",
         "due_at": inside.isoformat(),
         "fulfilled_at": (inside + timedelta(hours=1)).isoformat()},
        {"id": "open", "status": "pending", "due_at": inside.isoformat(),
         "fulfilled_at": None},
        {"id": "outside", "status": "fulfilled",
         "due_at": (end + timedelta(days=1)).isoformat(),
         "fulfilled_at": end.isoformat()},
        {"id": "cancelled", "status": "cancelled",
         "due_at": inside.isoformat(), "fulfilled_at": None},
    ]
    benchmark = SelfhoodBenchmark(
        BenchmarkStore(str(tmp_path / "benchmark.db")),
        commitments=_CommitmentCohort(rows),
    )
    result = benchmark._m_commitments(
        start, end, start.timestamp(), end.timestamp())
    assert result["numerator"] == 1
    assert result["denominator"] == 3
    assert result["detail"]["late"] == 1
    assert result["detail"]["cohort"] == "due_at_in_iso_week"


def test_initiative_acceptance_requires_exact_message_reaction(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_COGNITION_P4_MODE", "live")
    start, end = week_window("2026-W26")
    deliveries = _DeliveryEvidence([
        {"id": 1, "ts": start.timestamp() + 100, "outcome": "success",
         "evidence_status": "verified", "source_ref": "delivery:d1",
         "evidence": {"delivery_id": "delivery:d1"}},
        {"id": 2, "ts": start.timestamp() + 200, "outcome": "success",
         "evidence_status": "verified", "source_ref": "delivery:d2",
         "evidence": {"delivery_id": "delivery:d2"}},
    ])
    comms = CommsLog(str(tmp_path / "comms.db"))
    comms.log(
        "owner", direction="in", summary="yes", reaction="accepted",
        reply_to_ref="delivery:d1",
        ts=(start + timedelta(hours=1)).isoformat())
    # This would have counted in v1's any-inbound-within-24h heuristic.
    comms.log(
        "owner", direction="in", summary="unrelated", reaction="accepted",
        reply_to_ref="delivery:someone-else",
        ts=(start + timedelta(hours=2)).isoformat())
    benchmark = SelfhoodBenchmark(
        BenchmarkStore(str(tmp_path / "benchmark.db")),
        competence=deliveries, comms=comms, owner_contact_id="owner")
    result = benchmark._m_acceptance(
        start, end, start.timestamp(), end.timestamp())
    assert result["numerator"] == 1
    assert result["denominator"] == 2
    assert result["detail"]["binding"] == "reply_to_ref"


def test_correction_rate_binds_to_receipt_backed_outbound_cohort(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_COGNITION_P4_MODE", "live")
    start, end = week_window("2026-W26")
    comms = CommsLog(str(tmp_path / "comms.db"))
    for index in (1, 2):
        comms.log(
            "owner", direction="out", summary=f"answer {index}",
            external_ref=f"response:r{index}", receipt_ref=f"receipt:r{index}",
            ts=(start + timedelta(hours=index)).isoformat())
    # An outbound row without a receipt is intentionally not a denominator.
    comms.log(
        "owner", direction="out", summary="unverified",
        external_ref="response:unverified",
        ts=(start + timedelta(hours=3)).isoformat())
    feedback = FeedbackStore(str(tmp_path / "feedback.db"))
    feedback.record_correction(UserCorrection(
        correction_id="correction-1",
        timestamp=start + timedelta(hours=4),
        original_response="answer 1", correction_text="fix it",
        correction_type="factual", context_hash="response:r1",
        person_id="owner"))
    benchmark = SelfhoodBenchmark(
        BenchmarkStore(str(tmp_path / "benchmark.db")),
        comms=comms, corrections=feedback, owner_contact_id="owner")
    result = benchmark._m_corrections(
        start, end, start.timestamp(), end.timestamp())
    assert result["value"] == pytest.approx(0.5)
    assert result["numerator"] == 1
    assert result["denominator"] == 2


@pytest.mark.asyncio
async def test_recall_probe_is_subject_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_COGNITION_P4_MODE", "live")

    class Facts:
        def list_facts(self, **kwargs):
            assert kwargs["contact_id"] == "owner"
            return {"facts": [
                {"id": "owner-fact", "contact_id": "owner",
                 "shareability": "owner_private",
                 "fact": "the owner project uses a cobalt release marker"},
                {"id": "guest-fact", "contact_id": "guest",
                 "shareability": "shared",
                 "fact": "guest unrelated private phrase"},
            ]}

    person_ids = []

    async def recall(query, *, person_id=None, limit=5):
        person_ids.append(person_id)
        return [{"content": query}]

    benchmark = SelfhoodBenchmark(
        BenchmarkStore(str(tmp_path / "benchmark.db")),
        recall=recall, facts=Facts(), owner_contact_id="owner", probes=10)
    result = await benchmark._m_recall(0, float("inf"))
    assert result["value"] == 1.0
    assert result["denominator"] == 1
    assert person_ids == ["owner"]
