"""P8 server, authenticated context, read-model, and lifecycle wiring."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from onekey import RequestAuthority
from protagine.api.routers import host
from protagine.api.schemas.host import (
    ContextAssembleRequest,
    HostIdentity,
    HostMessage,
    HostTurnContext,
    MultimodalSearchRequest,
)
from protagine.server import _attach_p8_runtime
from protagine.tom.facts import SharedFactsStore
from protagine.tom.integration import P8Runtime
from protagine.turns import TurnIdempotencyLedger


def current_fact_source(facts, tmp_path, person, text):
    """Actual canonical support for automatic-context fixture positives."""
    import uuid
    from pathlib import Path
    from protagine import get_state_dir
    ledger = TurnIdempotencyLedger(Path(get_state_dir()) / 'turn-idempotency.db')
    facts._source_ledger = ledger
    turn = 'fact-support-' + uuid.uuid4().hex
    ledger.record_source(turn, contact_id=person, session_id='prior',
        messages=[{'role': 'user', 'content': text}], derive_claims=False)
    return facts.source_input(turn, person)[0]


P8_FILES = (
    "protagine-p8-visibility.db",
    "protagine-p8-arcs.db",
    "protagine-p8-recipient-audit.db",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _authority(
    person: str = "alice",
    *,
    principal: str = "hermes-text",
    person_ids: tuple[str, ...] = (),
    scopes: tuple[str, ...] = ("context:read", "tom:read"),
) -> RequestAuthority:
    return RequestAuthority(
        principal_id=principal,
        credential_id="current",
        scopes=frozenset(scopes),
        viewer_person_id=person,
        person_ids=frozenset((person, *person_ids)),
        audiences=frozenset(("viewer",)),
        authenticated=True,
    )


def _request(authority: RequestAuthority) -> Request:
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
        "scheme": "http",
    })
    request.state.protagine_authority = authority
    return request


def _context(person: str) -> ContextAssembleRequest:
    return ContextAssembleRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(
            contact_id=person,
            session_id="session:1",
            channel_id="body-claimed-channel",
        ),
        incoming_message=HostMessage(role="user", content="hello"),
    )


@pytest.fixture(autouse=True)
def _restore_host_globals(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_RECALL_RERANK", "off")
    names = (
        "_p8_runtime", "_facts_store", "_tom2_store",
        "_relationship_profiler", "_embedder", "_goals_store",
        "_initiative_store", "_briefings_engine",
        "_contacts_store",
        "_connection_discoverer", "_metalearner", "_commitment_store",
        "_preference_learner", "_affect_store", "_engagement_store",
        "_comms_log",
    )
    originals = {name: getattr(host, name, None) for name in names}
    yield
    # Tests mix direct spy wiring with monkeypatch replacements. Undo those
    # replacements first, so their captured spies cannot overwrite this restore.
    monkeypatch.undo()
    for name, value in originals.items():
        setattr(host, name, value)


class _LegacyGlobalContextSpies:
    """Content-bearing legacy sources without a P8 visibility envelope."""

    def __init__(self):
        self.calls = {
            name: 0 for name in (
                "goals", "initiatives", "briefings",
                "directive_ack", "directive_pending", "directive_brief",
                "insights", "contacts_list", "cognition",
            )
        }

        owner_goal = SimpleNamespace(
            priority=SimpleNamespace(name="HIGH"),
            title="owner-global-goal",
            description="owner-global-goal-description",
            progress_pct=0.5,
            status=SimpleNamespace(value="active"),
        )

        outer = self

        class Goals:
            def list_goals(self, *args, **kwargs):
                outer.calls["goals"] += 1
                return [owner_goal]

        class Initiatives:
            def list(self, *args, **kwargs):
                outer.calls["initiatives"] += 1
                return [SimpleNamespace(
                    type="research",
                    description="owner-global-initiative",
                    priority=0.8,
                )]

        class Briefings:
            def get_recent(self, *args, **kwargs):
                outer.calls["briefings"] += 1
                return [{
                    "title": "owner-global-briefing",
                    "body": "owner-global-briefing-body",
                }]

        class Directives:
            def consume_ack(self):
                outer.calls["directive_ack"] += 1
                return "owner-global-directive-ack"

            def pending_confirmation(self):
                outer.calls["directive_pending"] += 1
                return "owner-global-directive-pending"

            def context_brief(self):
                outer.calls["directive_brief"] += 1
                return "owner-global-directive-brief"

        class Contacts:
            async def get(self, _contact_id):
                return None

            async def get_style(self, _contact_id):
                return {}

            async def list(self):
                outer.calls["contacts_list"] += 1
                return [{
                    "contact_id": "owner-private-contact",
                    "display_name": "owner-global-contact-list",
                    "trust_tier": "trusted",
                }]

            async def compute_cadence_overdue(self, **_kwargs):
                return []

        class Insights:
            async def discover_connections(self, *args, **kwargs):
                outer.calls["insights"] += 1
                return [SimpleNamespace(
                    novelty=0.9,
                    description="owner-global-insight",
                )]

        class Cognition:
            async def evaluate(self):
                outer.calls["cognition"] += 1
                return SimpleNamespace(overall=0.9)

        self.goals = Goals()
        self.initiatives = Initiatives()
        self.briefings = Briefings()
        self.directives = Directives()
        self.contacts = Contacts()
        self.insights = Insights()
        self.cognition = Cognition()

    def wire(self, monkeypatch):
        host._goals_store = self.goals
        host._initiative_store = self.initiatives
        host._briefings_engine = self.briefings
        host._contacts_store = self.contacts
        host._connection_discoverer = self.insights
        host._metalearner = self.cognition


class _PersonalContextSpies:
    """Exact-person stores that must never see an unsealed P8 selector."""

    def __init__(self):
        self.calls = {
            name: [] for name in (
                "commitment_list", "commitment_overdue",
                "commitment_pending", "contact_get", "contact_style",
                "contact_cadence", "affect", "engagement", "comms",
                "relationship_profile",
            )
        }
        outer = self

        class Commitments:
            def list(self, **kwargs):
                outer.calls["commitment_list"].append(kwargs)
                return {"commitments": [{
                    "id": "commitment-private",
                    "person_id": kwargs.get("person_id"),
                    "description": "PERSONAL OWNER COMMITMENT",
                    "status": "pending",
                }]}

            def get_overdue(self):
                outer.calls["commitment_overdue"].append(True)
                return [{
                    "id": "overdue-private",
                    "person_id": "owner",
                    "description": "PERSONAL OWNER OVERDUE",
                    "status": "overdue",
                }]

            def get_pending_for_person(self, person_id):
                outer.calls["commitment_pending"].append(person_id)
                return [{
                    "description": "PERSONAL OWNER PENDING",
                    "priority": "high",
                    "due_at": None,
                }]

        class ContactRecord:
            display_name = "Private Person"
            given_name = "Private"
            timezone = "UTC"
            last_interaction_at = None
            relationship_score = 0.9
            trust_tier = "trusted"

            def get(self, key, default=None):
                return {
                    "trust_tier": self.trust_tier,
                    "style_notes": "PERSONAL OWNER STYLE",
                }.get(key, default)

        class Contacts:
            async def get(self, person_id):
                outer.calls["contact_get"].append(person_id)
                return ContactRecord()

            async def get_style(self, person_id):
                outer.calls["contact_style"].append(person_id)
                return {"tone": "PERSONAL OWNER STYLE"}

            async def compute_cadence_overdue(self, **kwargs):
                outer.calls["contact_cadence"].append(kwargs)
                return []

        class Affect:
            def get_state(self, person_id):
                outer.calls["affect"].append(person_id)
                return {
                    "event_count": 1,
                    "current_valence": 0.4,
                    "current_arousal": 0.5,
                    "valence": 0.4,
                    "arousal": 0.5,
                    "trend": "stable",
                }

        class Engagement:
            def get_profile(self, person_id):
                outer.calls["engagement"].append(person_id)
                return None

        class Comms:
            def last_per_channel(self, person_id):
                outer.calls["comms"].append(("per", person_id))
                return {"rcs": {"ts": "2026-07-12"}}

            def last_outbound(self, person_id):
                outer.calls["comms"].append(("outbound", person_id))
                return None

        class Brief:
            def render(self):
                return "PERSONAL OWNER RELATIONSHIP PROFILE"

        class Profiler:
            def cached(self, person_id, **kwargs):
                outer.calls["relationship_profile"].append(
                    (person_id, kwargs))
                return Brief()

        self.commitments = Commitments()
        self.contacts = Contacts()
        self.affect = Affect()
        self.engagement = Engagement()
        self.comms = Comms()
        self.profiler = Profiler()

    def wire(self):
        host._commitment_store = self.commitments
        host._contacts_store = self.contacts
        host._affect_store = self.affect
        host._engagement_store = self.engagement
        host._comms_log = self.comms
        host.set_relationship_profiler(self.profiler)


@pytest.mark.asyncio
async def test_p8_non_owner_never_queries_untyped_global_context(
    tmp_path, monkeypatch,
):
    """Reproduce the legacy-global P8 leak before adding its boundary."""

    monkeypatch.setenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    spies = _LegacyGlobalContextSpies()
    spies.wire(monkeypatch)

    assembled_body = _context("alice")
    assembled_body.include_initiatives = True
    assembled_body.projection_policy = "scoped_viewer_required"
    assembled = await host.context_assemble(
        assembled_body, request=_request(_authority("alice")))

    rendered = repr(assembled)
    assert "owner-global" not in rendered
    assert all(count == 0 for count in spies.calls.values())
    projection = assembled.projection_attestation
    assert projection is not None
    assert projection.viewer_person_id == "alice"
    assert projection.viewer_attested is True
    assert projection.viewer_is_owner is False
    assert projection.p8_mode == "shadow"
    assert projection.scoped_projection_ready is True
    assert projection.legacy_global_allowed is False


@pytest.mark.asyncio
async def test_p8_exact_owner_retains_untyped_global_context(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    spies = _LegacyGlobalContextSpies()
    spies.wire(monkeypatch)
    owner_request = _request(_authority("owner"))

    assembled_body = _context("owner")
    assembled_body.include_initiatives = True
    assembled = await host.context_assemble(
        assembled_body, request=owner_request)

    rendered = repr(assembled)
    for marker in ("owner-global-goal", "owner-global-initiative"):
        assert marker in rendered
    assert all(spies.calls[name] > 0 for name in ("goals", "initiatives"))
    projection = assembled.projection_attestation
    assert projection is not None
    assert projection.viewer_person_id == "owner"
    assert projection.viewer_is_owner is True
    assert projection.p8_mode == "shadow"
    assert projection.scoped_projection_ready is True
    assert projection.legacy_global_allowed is True


@pytest.mark.asyncio
async def test_p8_off_scoped_guest_uses_canonical_projection_without_global_producers(
    monkeypatch,
):
    monkeypatch.delenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", raising=False)
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    host.set_p8_runtime(None)
    spies = _LegacyGlobalContextSpies()
    spies.wire(monkeypatch)
    alice_request = _request(_authority("alice"))

    assembled_body = _context("alice")
    assembled_body.include_initiatives = True
    assembled_body.projection_policy = "scoped_viewer_required"
    response = await host.context_assemble(assembled_body, request=alice_request)
    assert response.projection_attestation.projection_backend == "canonical_sources"
    assert response.projection_attestation.scoped_projection_ready
    assert response.projection_attestation.p8_mode == "off"
    assert not response.projection_attestation.legacy_global_allowed
    assert "owner-global" not in repr(response)
    assert all(count == 0 for count in spies.calls.values())


@pytest.mark.asyncio
async def test_p8_off_exact_owner_keeps_legacy_global_context(monkeypatch):
    monkeypatch.delenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", raising=False)
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    host.set_p8_runtime(None)
    spies = _LegacyGlobalContextSpies()
    spies.wire(monkeypatch)

    assembled_body = _context("owner")
    assembled_body.include_initiatives = True
    assembled = await host.context_assemble(
        assembled_body, request=_request(_authority("owner")))

    assert "owner-global" in repr(assembled)
    assert spies.calls["goals"] > 0


@pytest.mark.asyncio
async def test_p8_temporal_keeps_global_owner_heads_up_owner_only(
    tmp_path, monkeypatch,
):
    calls = {"commitments": 0, "cadence": 0}

    class Commitments:
        def get_overdue(self):
            calls["commitments"] += 1
            return [{
                "description": "owner-global-overdue",
                "due_at": _now(),
            }]

    class Contacts:
        async def get(self, _contact_id):
            return None

        async def compute_cadence_overdue(self, **_kwargs):
            calls["cadence"] += 1
            return [{
                "name": "owner-global-cadence",
                "days_since": 10,
                "cadence_days": 3,
            }]

    monkeypatch.setenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    host._commitment_store = Commitments()
    host._contacts_store = Contacts()

    guest = await host.context_temporal(
        contact_id="alice", request=_request(_authority("alice")))
    assert "owner-global" not in guest["body"]
    assert calls == {"commitments": 0, "cadence": 0}

    owner = await host.context_temporal(
        contact_id="owner", request=_request(_authority("owner")))
    assert "owner-global-overdue" in owner["body"]
    assert "owner-global-cadence" in owner["body"]
    assert calls == {"commitments": 1, "cadence": 1}

    host.set_p8_runtime(None)
    legacy = await host.context_temporal(
        contact_id="alice", request=_request(_authority("alice")))
    assert "owner-global-overdue" in legacy["body"]
    assert "owner-global-cadence" in legacy["body"]
    assert calls == {"commitments": 2, "cadence": 2}


@pytest.mark.asyncio
async def test_p8_scoped_non_owner_queries_only_exact_person_commitments(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    spies = _PersonalContextSpies()
    spies.wire()
    request = _request(_authority("alice"))

    assembled = await host.context_assemble(
        _context("alice"), request=request)

    assert spies.calls["commitment_list"]
    assert all(call == {
        "person_id": "alice",
        "status": ["pending", "overdue"],
        "limit": 5,
    } for call in spies.calls["commitment_list"])
    assert spies.calls["commitment_pending"] == []
    assert spies.calls["commitment_overdue"] == []
    assert "PERSONAL OWNER COMMITMENT" in repr(assembled)


@pytest.mark.asyncio
async def test_p8_guest_comms_uses_neutral_owner_label(tmp_path, monkeypatch):
    from protagine.contacts.comms import CommsLog

    monkeypatch.setenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("PROTAGINE_OWNER_NAME", "PRIVATE OWNER NAME")
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    spies = _PersonalContextSpies()
    spies.wire()
    comms_log = CommsLog(str(tmp_path / "communications.db"))
    comms_log.log("alice", channel="voice:thread", direction="out", summary="A routine reply.")
    monkeypatch.setattr(host, "_comms_log", comms_log)

    response = await host.context_assemble(
        _context("alice"), request=_request(_authority("alice")))
    comms = next(
        section for section in response.sections
        if section.id == "protagine-comms-landscape"
    )
    assert "PRIVATE OWNER NAME" not in comms.body
    assert "owner approval" in comms.body.lower()
    assert "Last recorded outgoing message:" in comms.body
    assert "does not establish proactive outreach or delivery" in comms.body
    assert "I last reached out" not in comms.body


def _dynamic_provider(name, handler):
    return lambda: {
        name: ({
            "type": "function",
            "function": {
                "name": name,
                "description": "must never shadow the shipped capability",
                "parameters": {"type": "object", "properties": {}},
            },
        }, handler),
    }


def _collision_authority(name):
    if name == "calculate":
        return _authority("alice", scopes=("api:access",))
    if name == "read_file":
        return _authority("owner", scopes=("api:access",))
    return _authority(
        "owner", scopes=("api:access", "tools:mutate"))


def test_default_off_and_live_request_create_no_p8_state(tmp_path, monkeypatch):
    facts = SimpleNamespace()
    for configured in (None, "off", "live", "unknown"):
        if configured is None:
            monkeypatch.delenv(
                "PROTAGINE_RECIPIENT_SIMULATOR_MODE", raising=False)
        else:
            monkeypatch.setenv(
                "PROTAGINE_RECIPIENT_SIMULATOR_MODE", configured)
        runtime = _attach_p8_runtime(
            state_dir=tmp_path, facts_store=facts)
        assert runtime is None
        assert host._p8_runtime is None
        assert all(not (tmp_path / name).exists() for name in P8_FILES)


def test_shadow_attaches_one_runtime_and_restart_closes_cleanly(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("PROTAGINE_P8_FACT_MIN_CONFIDENCE", "0")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))

    first = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    assert isinstance(first, P8Runtime)
    assert first is host._p8_runtime
    assert all((tmp_path / name).exists() for name in P8_FILES)
    assert first.status()["mode"] == "shadow"
    assert first.status()["fact_min_confidence"] == 0.5
    first.close()
    first.close()

    restarted = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    assert isinstance(restarted, P8Runtime)
    assert restarted is host._p8_runtime
    restarted.close()


def test_new_fact_uses_typed_envelope_and_legacy_row_stays_excluded(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    assert runtime is not None
    alice = host._p8_viewer_for_request(_request(_authority("alice")), "alice")
    bob = host._p8_viewer_for_request(_request(_authority("bob")), "bob")

    legacy = facts.create_fact(
        contact_id="alice", fact="legacy unscoped secret",
        source="shared_context", confidence=0.99)
    scoped = facts.create_fact(
        contact_id="alice", fact="Alice likes concise updates",
        source="shared_context", confidence=0.9,
        metadata={
            "viewer_scope": "public",
            "shareability": "public",
            "subject_person_id": "bob",
        },
    )
    candidate = runtime.append_shared_fact(
        scoped, producer=alice, origin="body")
    weak = facts.create_fact(
        contact_id="alice", fact="zero-confidence claim",
        source="shared_context", confidence=0.0)
    weak_candidate = runtime.append_shared_fact(
        weak, producer=alice, origin="body")
    assert candidate.visibility.subject_person_id == "alice"
    assert candidate.visibility.viewer_scope == "person:alice"
    assert candidate.visibility.shareability == "subject_private"

    alice_batch = runtime.project_shared_facts(alice, now=_now())
    assert [row.content for row in alice_batch.facts] == [
        "Alice likes concise updates"]
    assert legacy["fact"] not in repr(alice_batch.public())
    assert "zero-confidence claim" not in repr(alice_batch.public())
    deck = runtime.deck_projection(alice, now=_now())
    assert weak_candidate.visibility.fact_ref not in repr(deck)
    lowered = runtime.project_shared_facts(
        alice, now=_now(), min_confidence=0.0)
    assert "zero-confidence claim" not in repr(lowered.public())
    assert runtime.project_shared_facts(bob, now=_now()).facts == ()


@pytest.mark.asyncio
async def test_context_renders_only_authenticated_enveloped_facts(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    assert runtime is not None
    alice_request = _request(_authority("alice"))
    alice = host._p8_viewer_for_request(alice_request, "alice")

    included = facts.create_fact(
        contact_id="alice", fact="allowed alice context",
        confidence=0.9, source_lineage=current_fact_source(
            facts, tmp_path / 'state', 'alice', 'allowed alice context'))
    runtime.append_shared_fact(included, producer=alice, origin="server")
    facts.create_fact(
        contact_id="alice", fact="legacy row must not render",
        confidence=1.0)
    bob_row = facts.create_fact(
        contact_id="bob", fact="Bob private context", confidence=1.0)
    bob = host._p8_viewer_for_request(_request(_authority("bob")), "bob")
    runtime.append_shared_fact(bob_row, producer=bob, origin="server")

    query = _context("alice")
    query.incoming_message.content = "allowed alice context"
    response = await host.context_assemble(query, request=alice_request)
    section = next(
        part for part in response.sections if part.id == "protagine-memory")
    assert "allowed alice context" in section.body
    assert "legacy row must not render" not in section.body
    assert "Bob private context" not in section.body


@pytest.mark.asyncio
async def test_canonical_context_never_falls_through_to_raw_legacy_facts(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    request = _request(_authority("alice"))
    viewer = host._p8_viewer_for_request(request, "alice")
    scoped = facts.create_fact(
        contact_id="alice", fact="authorized enriched fact", confidence=0.9,
        source_lineage=current_fact_source(facts, tmp_path, 'alice', 'authorized enriched fact'))
    runtime.append_shared_fact(scoped, producer=viewer, origin="server")
    facts.create_fact(
        contact_id="alice", fact="raw legacy enriched leak", confidence=1.0)

    body = _context('alice')
    body.incoming_message = HostMessage(role='user', content='authorized enriched fact')
    response = await host.context_assemble(body, request=request)
    rendered = "\n".join(section.body for section in response.sections)
    assert "authorized enriched fact" in rendered
    assert "raw legacy enriched leak" not in rendered


@pytest.mark.asyncio
async def test_default_off_relationship_detail_keeps_legacy_selector_behavior():
    class Brief:
        def to_dict(self):
            return {"contact_id": "alice", "rapport_topics": ["legacy"]}

        def render(self):
            return "legacy relationship brief"

    class StrictProfiler:
        def __init__(self):
            self.calls = []

        def cached(self, contact_id):
            self.calls.append(contact_id)
            return Brief()

    profiler = StrictProfiler()
    host.set_p8_runtime(None)
    host.set_relationship_profiler(profiler)

    # Baseline endpoint semantics allowed the body/path-selected contact under
    # the general API scope; P8-off must not introduce a new person gate.
    result = await host.get_relationship_brief(
        "alice", request=_request(_authority("bob")))
    assert result["rendered"] == "legacy relationship brief"
    assert profiler.calls == ["alice"]

    class RefreshProfiler:
        def __init__(self):
            self.profile_calls = []

        def cached(self, _contact_id):
            raise AssertionError("refresh=True must skip cache")

        async def profile(self, contact_id):
            self.profile_calls.append(contact_id)
            return Brief()

    refresh_profiler = RefreshProfiler()
    host.set_relationship_profiler(refresh_profiler)
    positional = await host.get_relationship_brief("alice", True)
    assert positional["rendered"] == "legacy relationship brief"
    assert refresh_profiler.profile_calls == ["alice"]


@pytest.mark.asyncio
async def test_p8_multimodal_memory_search_releases_no_legacy_rows(
    tmp_path, monkeypatch,
):
    class Embedder:
        is_multimodal = False

        async def embed(self, _text):
            return [0.1, 0.2]

    class Store:
        def __init__(self):
            self.limits = []

        async def search_cross_modal(
            self, _collection, _vector, *, limit,
            filter_modality, min_score,
        ):
            self.limits.append(limit)
            return [
                {"id": "mirror", "text": "private mirror", "metadata": {
                    "source_uri": "tom:shared_fact"}},
                {"id": "legacy", "text": "legacy private mirror",
                 "metadata": {}},
                {"id": "ordinary", "text": "ordinary memory",
                 "metadata": {"source_uri": "session:one"}},
            ]

    monkeypatch.setenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", "shadow")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    store = Store()
    host._embedder = Embedder()
    import protagine.vector as vector_module
    monkeypatch.setattr(vector_module, "get_store", lambda: store)

    response = await host.memory_search_multimodal(
        MultimodalSearchRequest(
            identity=HostIdentity(host_id="test"),
            query="memory", collection="memories", limit=2,
        ))
    # Legacy memory rows carry no authoritative scope: under P8 none is released.
    assert response.results == []
    assert store.limits == [40]

    # Turning P8 off preserves the historical exact search call.
    host.set_p8_runtime(None)
    response = await host.memory_search_multimodal(
        MultimodalSearchRequest(
            identity=HostIdentity(host_id="test"),
            query="memory", collection="memories", limit=2,
        ))
    assert response.results[0]["id"] == "mirror"
    assert store.limits[-1] == 2


def _principal(
    name: str,
    secret: str,
    person: str,
    scopes: list[str],
    *,
    audiences: list[str] | None = None,
) -> dict:
    return {
        "principal": name,
        "status": "active",
        "scopes": scopes,
        "viewer_person_id": person,
        "audiences": audiences or ["viewer"],
        "credentials": [{
            "id": "current", "secret": secret, "status": "active",
        }],
    }


def test_restart_replays_envelopes_and_all_runtime_stores_close(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    viewer = host._p8_viewer_for_request(_request(_authority("alice")), "alice")
    row = facts.create_fact(
        contact_id="alice", fact="survives restart", confidence=0.8)
    runtime.append_shared_fact(row, producer=viewer, origin="server")
    runtime.close()

    restarted = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    batch = restarted.project_shared_facts(viewer, now=_now())
    assert [fact.content for fact in batch.facts] == ["survives restart"]
    restarted.close()
