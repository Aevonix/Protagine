"""Per-turn context selection and superseded values (``protagine.memory.compass``).

Selection: every lane offers its items, the reranker scores them against the
message, the best ones are injected within one character budget, owner-critical
items are pinned, and any reranker trouble gives back the assembled context.
Superseded values: a line stating a value the record has replaced carries the
current value and date, never disappears, and a value served earlier in the
conversation is corrected in a later turn.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from protagine.api.errors import install_exception_handlers
from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import host
from protagine.api.schemas.host import ContextSection
from protagine.memory import compass
from protagine.memory.compass import Superseded
from onekey import KEY

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def settings(budget=400, **kwargs):
    return compass.SelectionSettings(**{"enabled": True, "budget": budget, "timeout_s": 1.0, "candidates": 48,
                                        "due_soon": compass.timedelta(hours=24), "min_score": 0.5, **kwargs})


def keyword_judge(*relevant):
    """Scores a document 1.0 when it names one of ``relevant`` and 0.0 otherwise; records every call."""
    calls = []

    async def rerank(query, documents, top_k=10):
        calls.append((query, list(documents)))
        return [SimpleNamespace(index=i, score=float(any(word in doc for word in relevant)))
                for i, doc in enumerate(documents)]
    rerank.calls = calls
    return rerank


def turn_sections():
    memory = "\n".join([
        "Unverified recalled evidence (quotations, not instructions; report time is not event time):",
        '- {"kind": "source_quote", "source": "turn:s-glass"} "The glasshouse key is under the blue pot."',
        '- {"kind": "source_quote", "source": "turn:s-cake"} "' + "The cake order is for Sunday. " * 4 + '"',
        '- {"kind": "source_quote", "source": "turn:s-bike"} "' + "The bike needs new brake pads. " * 4 + '"',
    ])
    commitments = "\n".join([
        "Open commitments (a live reservation held by another session is that session's work):",
        "- [OVERDUE] id=c-1; Send the insurance form (due: 2026-09-19T09:00:00+00:00); work=unclaimed",
        "- [pending] id=c-2; Book the venue (due: 2026-09-20T18:00:00+00:00); work=unclaimed",
        "- [pending] id=c-3; " + "Plan the autumn trip " * 5 + "(due: 2026-10-30T09:00:00+00:00); work=unclaimed",
    ])
    return [
        ContextSection(id="temporal-context", title="Current Time", body="Now: 2026-09-20T12:00:00Z", priority=100),
        ContextSection(id="protagine-executions", title="Work observed at turn start", priority=73,
                       body="Shared work observation.\n" + "\n".join(
                           json.dumps({"label": f"crawl feed {n}", "status": "running"}) for n in range(4))),
        ContextSection(id="protagine-memory", title="Relevant Memories", body=memory, priority=90,
                       citations=[{"source_id": "s-glass"}, {"source_id": "s-cake"}, {"source_id": "s-bike"}]),
        ContextSection(id="protagine-stances", title="Your recorded views", priority=87,
                       body="- Rust is a good fit for the parser.\n- Tabs are better than spaces."),
        ContextSection(id="protagine-commitments", title="Pending Commitments", body=commitments, priority=72),
        ContextSection(id="protagine-waiting", title="Expected replies", priority=74,
                       body="wait=w-1; task=Ask p-03 for the draft; state=due"),
        ContextSection(id="protagine-relationship", title="Recorded relationship context", priority=86,
                       body="Recorded interactions: 40 · recorded contact tier: owner"),
    ]


# -- selection ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_best_items_fit_the_budget_and_owner_critical_ones_stay():
    sections = turn_sections()
    judge = keyword_judge("glasshouse")
    selected, report = await compass.select_context(sections, "Where is the glasshouse key?", judge,
                                                    settings=settings(budget=400), now=NOW)
    by_id = {s.id: s for s in selected}
    assert report["status"] == "selected" and report["chars_after"] <= 400 < report["chars_before"]
    # The judged answer is injected with its section header; the passages it outranked are not.
    assert "glasshouse key" in by_id["protagine-memory"].body
    assert by_id["protagine-memory"].body.startswith("Unverified recalled evidence")
    assert "brake pads" not in by_id["protagine-memory"].body and "cake order" not in by_id["protagine-memory"].body
    assert by_id["protagine-memory"].citations == [{"source_id": "s-glass"}]
    # Pinned: the current time and the open asks, whole; the overdue and the due-today commitments.
    assert by_id["temporal-context"] is sections[0] and by_id["protagine-waiting"] is sections[5]
    commitments = by_id["protagine-commitments"].body
    assert "id=c-1" in commitments and "id=c-2" in commitments and "id=c-3" not in commitments
    assert commitments.startswith("Open commitments")
    # Order is the assembled order; nothing is reworded.
    assert [s.id for s in selected] == [s.id for s in sections if s.id in by_id]
    assert all(line in "\n".join(s.body for s in sections) for s in selected for line in s.body.split("\n"))
    # One judge call over every lane's unpinned items, each read with its lane's title and without ids.
    ((query, documents),) = judge.calls
    assert query == "Where is the glasshouse key?"
    assert any(doc.startswith("Work observed at turn start: ") for doc in documents)
    assert any(doc.startswith("Your recorded views: ") for doc in documents)
    assert not any("id=c-1" in doc for doc in documents)
    assert not any("turn:s-glass" in doc for doc in documents)


@pytest.mark.asyncio
async def test_without_a_score_floor_the_budget_fills_in_score_order_then_lane_priority():
    sections = turn_sections()
    selected, report = await compass.select_context(sections, "Where is the glasshouse key?", keyword_judge("glasshouse"),
                                                    settings=settings(budget=400, min_score=0.0), now=NOW)
    by_id = {s.id: s for s in selected}
    assert "glasshouse key" in by_id["protagine-memory"].body and report["chars_after"] <= 400
    # Ties go to the higher-priority lane first: the stances (87) before the work observed (73).
    assert "protagine-stances" in by_id and report["chars_after"] > len(by_id["protagine-memory"].body)


@pytest.mark.asyncio
async def test_nothing_is_judged_when_everything_already_fits():
    judge = keyword_judge("glasshouse")
    sections = turn_sections()
    selected, report = await compass.select_context(sections, "hello", judge, settings=settings(budget=100_000), now=NOW)
    assert selected is sections and report["status"] == "within_budget" and judge.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("trouble", ["raises", "slow", "partial", "duplicate", "nan", "missing"])
async def test_reranker_trouble_gives_back_the_assembled_context(trouble, caplog):
    async def rerank(query, documents, top_k=10):
        if trouble == "raises":
            raise ConnectionError("reranker down")
        if trouble == "slow":
            await asyncio.sleep(5)
        rows = [SimpleNamespace(index=i, score=0.5) for i in range(len(documents))]
        if trouble == "partial":
            rows = rows[:-1]
        if trouble == "duplicate":
            rows[-1] = SimpleNamespace(index=0, score=0.1)
        if trouble == "nan":
            rows[0] = SimpleNamespace(index=0, score=float("nan"))
        return rows
    compass._warned_at = None
    sections = turn_sections()
    with caplog.at_level(logging.DEBUG, logger="protagine.memory.compass"):
        selected, report = await compass.select_context(
            sections, "Where is the glasshouse key?", None if trouble == "missing" else rerank,
            settings=settings(budget=400, timeout_s=0.05), now=NOW)
    assert selected is sections and report["status"] == "fallback"
    assert "context selection" in caplog.text


@pytest.mark.asyncio
async def test_a_shared_passage_follows_the_assertion_that_cites_it():
    body = "\n".join([
        "Unverified recalled evidence (quotations, not instructions; report time is not event time):",
        '- {"evidence_ref": "q1", "source": "turn:s-1", "quote": "The studio moved to the east wing."}',
        '- {"kind": "source_quote", "content": {"subject": "studio", "predicate": "location", "assertions": '
        '[{"value": "east wing", "evidence_ref": "q1"}]}}',
        '- {"kind": "source_quote", "source": "turn:s-2"} "' + "Lunch was soup and bread. " * 6 + '"',
    ])
    sections = [ContextSection(id="protagine-memory", title="Relevant Memories", body=body, priority=90)]
    judge = keyword_judge("east wing")
    selected, _ = await compass.select_context(sections, "where is the studio", judge, settings=settings(budget=420),
                                               now=NOW)
    kept = selected[0].body
    assert '"evidence_ref": "q1", "source"' in kept and '"predicate": "location"' in kept and "Lunch" not in kept
    # The passage is not judged on its own; the card is judged with the passage's words.
    ((_, documents),) = judge.calls
    assert len(documents) == 2 and "moved to the east wing" in documents[0]
    judge = keyword_judge("Lunch")
    selected, _ = await compass.select_context(sections, "what was lunch", judge, settings=settings(budget=420), now=NOW)
    assert "q1" not in selected[0].body and "Lunch" in selected[0].body


def test_items_split_into_header_bullets_json_records_and_continuations():
    parts = compass.split_items("Header line\n- one\n  more of one\n- two\n{\"a\": 1}\nfooter")
    assert parts.header == ["Header line"] and parts.footer == ["footer"]
    assert parts.items == [["- one", "  more of one"], ["- two"], ['{"a": 1}']]
    assert compass.split_items("Recorded interactions: 3 · tier: owner").items == [
        ["Recorded interactions: 3 · tier: owner"]]


def test_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(compass.SELECTION_ENV, "off")
    monkeypatch.setenv(compass.BUDGET_ENV, "2500")
    monkeypatch.setenv(compass.TIMEOUT_ENV, "nonsense")
    values = compass.selection_settings()
    assert not values.enabled and values.budget == 2500 and values.timeout_s == compass.DEFAULT_TIMEOUT_MS / 1000
    monkeypatch.delenv(compass.SELECTION_ENV)
    assert compass.selection_settings().enabled is compass.DEFAULT_ENABLED


# -- superseded values --------------------------------------------------------------------------

def cid(name):
    """A claim id shaped as the ledger makes them."""
    return "claim:" + hashlib.sha256(name.encode()).hexdigest()


def card_line(source, *assertions, subject="event"):
    """An assertion card as the memory lane renders it: ``assertions`` are (claim id, value[, extra fields])."""
    members = [{"claim_id": claim, "source": "turn:" + source, "value": value, **(extra[0] if extra else {})}
               for claim, value, *extra in assertions]
    return "- " + json.dumps({"kind": "source_quote", "source_uri": "turn:" + source, "content": {
        "subject": subject, "predicate": "place", "status": "source_assertion", "assertions": members}},
        ensure_ascii=False)


def claim_quote(claim, text, source="s-1", **extra):
    """A quotation line whose record is one claim."""
    return "- " + json.dumps({"kind": "source_quote", "claim_id": claim, "source": "turn:" + source, **extra}) + " " \
        + json.dumps(text)


VENUE = Superseded(old="the corner office", current="the front lobby", since="2026-09-18", subject="design review",
                   kind="changed", record=cid("venue"))
DAY = Superseded(old="Monday", current="Thursday", since="2026-09-19", subject="dentist appointment",
                 record=cid("dentist"))


def noted(record):
    return f'[superseded id={record.record}: now "{record.current}" since {record.since}]'


def test_a_line_stating_a_superseded_value_is_annotated_never_removed():
    sections = [ContextSection(id="protagine-memory", title="Relevant Memories", body="\n".join([
        card_line("s-old", (VENUE.record, "the corner office"), subject="design review"),
        claim_quote(cid("venue-2"), "The review moved to the front lobby; the corner office is no longer right."),
        "- Book a Monday slot for the gym.",
        claim_quote(DAY.record, "The dentist appointment is on Monday."),
    ])), ContextSection(id="protagine-stances", title="Your recorded views", body="- The corner office is too loud."),
        ContextSection(id="protagine-appraisals", title="Relevant working perspective",
                       body=f'- {{"claim_id": "{VENUE.record}"}} The corner office is where the review is held.')]
    annotated = compass.annotate_superseded(sections, [VENUE, DAY])
    lines = annotated[0].body.split("\n")
    assert lines[0].endswith(noted(VENUE))
    assert lines[1] == sections[0].body.split("\n")[1]          # another claim's line
    assert lines[2] == "- Book a Monday slot for the gym."       # a line of no record
    assert lines[3].endswith(noted(DAY))
    assert annotated[1] is sections[1]                           # the old value, but not the record's line
    assert annotated[2].body.endswith(noted(VENUE))              # every lane
    assert len(annotated[0].body.split("\n")) == 4
    assert compass.dead_value_lines("\n".join(s.body for s in sections), [VENUE, DAY]) == 3
    assert compass.dead_value_lines("\n".join(s.body for s in annotated), [VENUE, DAY]) == 0
    assert compass.annotate_superseded(sections, []) is sections


def test_rescheduled_commitments_are_superseded_deadlines():
    rows = [{"id": "c-1", "description": "Send the form", "due_at": "2026-09-22T15:00:00+00:00",
             "updated_at": "2026-09-19T08:00:00+00:00",
             "metadata": {"reschedule": {"from": "2026-09-21T09:00:00+00:00", "by": "conversation"}}},
            {"id": "c-2", "description": "Call back", "due_at": "2026-09-23T10:00:00+00:00", "metadata": {}}]
    (record,) = compass.commitment_reschedules(rows)
    assert record.old == "2026-09-21T09:00:00+00:00" and record.current == "2026-09-22T15:00:00+00:00"
    line = "- [pending] id=c-1; Send the form (due: 2026-09-21T09:00:00+00:00); work=unclaimed"
    assert compass.asserts_superseded(line, record)
    assert record.note() == '[superseded id=c-1: rescheduled to "2026-09-22T15:00:00+00:00" since 2026-09-19]'


@pytest.mark.asyncio
async def test_changed_and_corrected_claims_are_read_from_the_ledger_with_their_latest_value(tmp_path):
    from protagine.beliefs.source_projection import SourceClaimProjection
    from protagine.turns import TurnIdempotencyLedger
    from test_source_claim_projection import Model, claim
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")

    async def say(identifier, text, value, operation, when):
        ledger.record_source(identifier, contact_id="contact-a", session_id="session-" + identifier,
                             messages=[{"role": "user", "content": text}], occurred_at=when, derive_claims=True)
        model = Model({text: claim(text, value, subject="design review", predicate="venue",
                                   operation=operation, match_prior=operation != "assert")})
        assert await SourceClaimProjection(ledger).process_one(model)

    await say("first", "The design review is in the corner office.", "the corner office", "assert",
              "2026-09-10T09:00:00+00:00")
    await say("second", "Correction: the design review is in the east room.", "the east room", "correct",
              "2026-09-11T09:00:00+00:00")
    await say("third", "Correction: the design review is in the front lobby.", "the front lobby", "correct",
              "2026-09-12T09:00:00+00:00")
    records = compass.claim_supersessions(ledger, contact_id="contact-a", session_id="later")
    assert {(r.old, r.current, r.kind) for r in records} == {
        ("the corner office", "the front lobby", "corrected"), ("the east room", "the front lobby", "corrected")}
    assert all(r.subject == "design review" and r.since for r in records)
    assert len({r.record for r in records}) == 2 and all(r.record.startswith("claim:") for r in records)
    assert compass.claim_supersessions(ledger, contact_id="contact-b", session_id="later") == []
    ledger.erase_sources(contact_id="contact-a", turn_ids=["third"])
    with closing(ledger._connect()) as conn:
        assert conn.execute("SELECT count(*) FROM source_claims").fetchone()[0] >= 2
    # An erased successor asserts nothing: no current value is invented for the older ones.
    assert all(r.current != "the front lobby" for r in
               compass.claim_supersessions(ledger, contact_id="contact-a", session_id="later"))


# -- the route ----------------------------------------------------------------------------------

@pytest.fixture
def route(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "cid-owner")
    monkeypatch.setenv(compass.BUDGET_ENV, "400")
    monkeypatch.delenv(compass.SELECTION_ENV, raising=False)
    monkeypatch.setattr(host, "_telemetry", None)
    state = SimpleNamespace(records=[], sections=turn_sections())

    async def assembled(body, *, viewer_person_id, request=None, superseded=None):
        if superseded is not None:
            superseded.extend(state.records)
        return compass.annotate_superseded(list(state.sections), state.records)
    monkeypatch.setattr(host, "_assemble_sections", assembled)
    app = FastAPI()
    install_exception_handlers(app)
    app.add_middleware(ApiKeyMiddleware, api_key=KEY)
    app.include_router(host.router)
    return app, state


async def post(app, query, session="s-1"):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/host/context/assemble", headers={"Authorization": "Bearer " + KEY}, json={
            "identity": {"host_id": "fixture"}, "context": {"contact_id": "cid-owner", "session_id": session},
            "incoming_message": {"role": "user", "content": query}})
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()["sections"]}


@pytest.mark.asyncio
async def test_the_route_injects_the_selection_and_falls_back_without_a_reranker(route, monkeypatch):
    app, _ = route
    monkeypatch.setattr(host, "_reranker", SimpleNamespace(rerank=keyword_judge("glasshouse")))
    selected = await post(app, "Where is the glasshouse key?")
    assert "glasshouse" in selected["protagine-memory"]["body"] and "brake pads" not in selected["protagine-memory"]["body"]
    assert "protagine-executions" not in selected and "temporal-context" in selected
    monkeypatch.setattr(host, "_reranker", None)
    whole = await post(app, "Where is the glasshouse key?", session="s-2")
    assert set(whole) == {s.id for s in turn_sections()}
    monkeypatch.setattr(host, "_reranker", SimpleNamespace(rerank=keyword_judge("glasshouse")))
    monkeypatch.setenv(compass.SELECTION_ENV, "off")
    assert set(await post(app, "Where is the glasshouse key?", session="s-3")) == {s.id for s in turn_sections()}


@pytest.mark.asyncio
async def test_a_value_served_earlier_and_superseded_since_is_corrected_in_that_conversation(route, monkeypatch):
    app, state = route
    monkeypatch.setattr(host, "_reranker", None)
    state.sections = [ContextSection(id="protagine-memory", title="Relevant Memories",
                                     body=claim_quote(VENUE.record, "The design review is in the corner office."))]
    first = await post(app, "where is the review")
    assert "protagine-corrections" not in first
    # The owner moves it; the old quote is no longer recalled, but the host replays the earlier turn as it was.
    state.records = [VENUE]
    state.sections = [ContextSection(id="protagine-stances", title="Your recorded views", body="- Lobbies are loud.")]
    second = await post(app, "anything else?")
    correction = second["protagine-corrections"]["body"]
    assert f'- id={VENUE.record}; "the corner office" (design review): {noted(VENUE)}' in correction
    assert compass.dead_value_lines(correction, [VENUE]) == 0
    # Delivered: the next turn of the conversation does not repeat it.
    assert "protagine-corrections" not in await post(app, "and now?")
    # Another conversation was never served the old value.
    assert "protagine-corrections" not in await post(app, "anything else?", session="s-other")


# -- round 2: identity, chains, decoding, caps ---------------------------------------------------

def commitment(cid, description, due, moved_from=None, updated="2026-09-20T08:00:00+00:00"):
    metadata = {"reschedule": {"from": moved_from, "by": "conversation"}} if moved_from else {}
    return {"id": cid, "description": description, "due_at": due, "updated_at": updated, "metadata": metadata}


def commitment_line(cid, description, due):
    return f"- [pending] id={cid}; {description} (due: {due}); work=unclaimed"


def quote_line(turn, text, **encoding):
    return "- " + json.dumps({"kind": "source_quote", "source": "turn:" + turn}, **encoding) + " " + json.dumps(
        text, **encoding)


def claim_record(old, current, claim, subject="", since="2026-09-19"):
    """A changed claim as ``claim_supersessions`` makes it: identified by its own claim id."""
    return Superseded(old=old, current=current, since=since, subject=subject, kind="changed", record=claim)


def body_of(sections):
    return "\n".join(section.body for section in sections)


def test_a_reschedule_marks_only_the_commitment_it_moved():
    """Finding 1: identity, never shared subject words or a shared old value."""
    tax_old, tax_new = "2026-09-21T09:00:00+00:00", "2026-09-29T09:00:00+00:00"
    records = compass.commitment_reschedules([commitment("c-tax", "Send the tax form", tax_new, tax_old),
                                              commitment("c-bank", "Send the bank form", tax_old)])
    listed = [ContextSection(id="protagine-commitments", title="Pending Commitments", body="\n".join([
        "Open commitments (a live reservation held by another session is that session's work):",
        commitment_line("c-tax", "Send the tax form", tax_new),
        commitment_line("c-bank", "Send the bank form", tax_old),
        commitment_line("c-tax-2", "Send the tax form copy", tax_old)]))]
    assert body_of(compass.annotate_superseded(listed, records)) == listed[0].body
    stale = [ContextSection(id="protagine-commitments", title="Pending Commitments",
                            body=commitment_line("c-tax", "Send the tax form", tax_old))]
    assert body_of(compass.annotate_superseded(stale, records)).endswith(f'rescheduled to "{tax_new}" since 2026-09-20]')


def test_a_changed_claim_marks_only_lines_from_its_own_record():
    """Finding 1, claims: a multiword old value on an unrelated line or another source is not the record's."""
    record = claim_record("the corner office", "the front lobby", "claim-7")
    sections = [ContextSection(id="protagine-memory", title="Relevant Memories", body="\n".join([
        claim_quote("claim-7", "The design review is in the corner office.", source="s-old"),
        quote_line("s-other", "The corner office has a broken heater."),
        '- {"kind": "source_quote", "content": {"assertions": [{"claim_id": "claim-7", "value": "the corner office"}]}}',
        '- {"kind": "source_quote", "content": {"assertions": [{"claim_id": "claim-70", "value": "the corner office"}]}}',
    ])), ContextSection(id="protagine-stances", title="Your recorded views", body="- The corner office is too loud.")]
    lines = body_of(compass.annotate_superseded(sections, [record])).split("\n")
    assert [compass.MARKER in line for line in lines] == [True, False, True, False, False]


def test_a_second_reschedule_corrects_a_value_first_served_as_a_correction():
    """Finding 2: a value a note introduced is superseded in turn."""
    a, b, c = "2026-09-21T09:00:00+00:00", "2026-09-25T09:00:00+00:00", "2026-09-29T09:00:00+00:00"
    key = ("viewer", "s-1")
    first = compass.commitment_reschedules([commitment("c-1", "Send the form", b, a)])
    served = compass.annotate_superseded([ContextSection(id="protagine-commitments", title="Pending Commitments",
                                                         body=commitment_line("c-1", "Send the form", a))], first)
    compass.SERVED.remember(key, body_of(served))
    second = compass.commitment_reschedules([commitment("c-1", "Send the form", c, b)])
    assert f'rescheduled to "{c}"' in compass.SERVED.corrections(key, second)
    # The same through a served correction line: its note carried B, and B is now old.
    compass.SERVED.clear()
    compass.SERVED.remember(key, commitment_line("c-1", "Send the form", a))
    note = compass.SERVED.corrections(key, first)
    assert f'rescheduled to "{b}"' in note
    compass.SERVED.remember(key, note)
    assert f'rescheduled to "{c}"' in compass.SERVED.corrections(key, second)


@pytest.mark.asyncio
async def test_the_candidate_cap_keeps_the_items_the_message_asks_about():
    """Finding 3: candidates are ranked against the message before the reranker's cap."""
    memory = "Unverified recalled evidence:\n" + "\n".join(
        f"- Note {n}: the garden shed paint is drying in batch {n}." for n in range(48))
    sections = [ContextSection(id="protagine-memory", title="Relevant Memories", body=memory, priority=90),
                ContextSection(id="protagine-commitments", title="Pending Commitments", priority=72, body="\n".join([
                    "Open commitments:", commitment_line("c-9", "Renew the insurance policy",
                                                         "2026-11-02T09:00:00+00:00")]))]
    judge = keyword_judge("insurance")
    selected, report = await compass.select_context(sections, "When is the insurance policy renewal due?", judge,
                                                    settings=settings(budget=400), now=NOW)
    assert "insurance" in body_of(selected) and report["judged"] == 48
    ((_, documents),) = judge.calls
    assert any("insurance" in doc for doc in documents)


@pytest.mark.asyncio
async def test_a_long_chain_resolves_every_version_to_the_value_held_now(tmp_path):
    """Finding 4: no hop bound stops the walk on a value that is itself superseded."""
    from protagine.beliefs.source_projection import SourceClaimProjection
    from protagine.turns import TurnIdempotencyLedger
    from test_source_claim_projection import Model, claim
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    for n in range(21):
        text = f"The review room is room {100 + n}."
        ledger.record_source(f"v{n}", contact_id="contact-a", session_id=f"session-{n}",
                             messages=[{"role": "user", "content": text}],
                             occurred_at=f"2026-09-{n + 1:02d}T09:00:00+00:00", derive_claims=True)
        model = Model({text: claim(text, f"room {100 + n}", subject="review room", predicate="room")})
        assert await SourceClaimProjection(ledger).process_one(model)
    with closing(ledger._connect()) as conn:  # each version corrected by the next: v0 -> v1 -> ... -> v20
        turns = dict(conn.execute("SELECT turn_id, id FROM source_claims").fetchall())
        ids = [turns[f"v{n}"] for n in range(21) if f"v{n}" in turns]
        assert len(ids) == 21
        conn.executemany("UPDATE source_claims SET retracted_by=? WHERE id=?", list(zip(ids[1:], ids)))
        conn.commit()
    records = compass.claim_supersessions(ledger, contact_id="contact-a", session_id="later")
    assert len(records) == 20 and {record.current for record in records} == {"room 120"}
    # A cycle has no value held now: nothing is asserted for it.
    with closing(ledger._connect()) as conn:
        conn.execute("UPDATE source_claims SET retracted_by=? WHERE id=?", (ids[0], ids[20]))
        conn.commit()
    assert compass.claim_supersessions(ledger, contact_id="contact-a", session_id="later") == []


def test_corrections_beyond_one_turns_share_reach_later_turns_and_are_not_repeated():
    """Finding 5: a correction once served is delivered; the next turn carries the ones still owed."""
    key = ("viewer", "s-1")
    rows = [commitment(f"c-{n}", f"Task {n}", f"2026-10-{n + 1:02d}T09:00:00+00:00", f"2026-09-{n + 1:02d}T09:00:00+00:00")
            for n in range(9)]
    compass.SERVED.remember(key, "\n".join(commitment_line(f"c-{n}", f"Task {n}", f"2026-09-{n + 1:02d}T09:00:00+00:00")
                                           for n in range(9)))
    records, emitted = compass.commitment_reschedules(rows), []
    for _ in range(3):
        note = compass.SERVED.corrections(key, records)
        emitted.extend(line for line in note.split("\n") if line.startswith("- "))
        compass.SERVED.remember(key, note)
    assert sorted(line.split(" ")[1] for line in emitted) == sorted(f"id=c-{n};" for n in range(9))


def test_a_short_value_on_its_own_record_is_corrected():
    """Finding 6: a two-character value is marked on a line of the source that stated it."""
    record = claim_record("42", "43", cid("locker"), subject="locker code")
    line = claim_quote(record.record, "My locker code is 42.", source="s-locker")
    sections = [ContextSection(id="protagine-memory", title="Relevant Memories", body=line)]
    assert body_of(compass.annotate_superseded(sections, [record])).endswith(noted(record))
    compass.SERVED.remember(("viewer", "s-1"), line)
    assert '"43"' in compass.SERVED.corrections(("viewer", "s-1"), [record])
    # Not a digit inside the line's own timestamps, and not on another claim's line of the same source.
    timed = claim_quote(record.record, "My locker moved.", source="s-locker", reported_at="2026-09-10T09:42:00+00:00")
    other = claim_quote(cid("towels"), "Bring 42 towels.", source="s-locker")
    assert compass.dead_value_lines("\n".join([timed, other]), [record]) == 0


def test_escaped_values_are_decoded_before_matching():
    """Finding 7: a value serialized with JSON escapes is still the value."""
    record = claim_record("Café Central", "Main Library", cid("meet"))
    line = claim_quote(record.record, "We meet at Café Central on Fridays.")  # json.dumps defaults: Café
    assert "Caf\\u00e9" in line
    annotated = body_of(compass.annotate_superseded(
        [ContextSection(id="protagine-memory", title="Relevant Memories", body=line)], [record]))
    assert annotated.endswith(noted(record))
    work = json.dumps({"label": "Café Central booking", "claim_id": record.record})
    assert compass.dead_value_lines(work, [record]) == 1
    # Escaped literals outside a parsed JSON record: a text remainder, or a line in the correction shape.
    remainder = f'- {{"claim_id": "{record.record}"}} said: ' + json.dumps("Meet at Café Central.")
    correction = f"- id={record.record}; " + json.dumps("Café Central") + " (meeting place): noted"
    assert compass.dead_value_lines("\n".join([remainder, correction]), [record]) == 2


def test_every_superseded_value_on_a_line_is_marked_and_a_partly_marked_line_still_counts():
    """Finding 8: no per-line cap, and a note answers only for the value it corrects."""
    records = [claim_record("42", "43", cid("locker")), claim_record("blue", "green", cid("door")),
               claim_record("Tuesday", "Thursday", cid("day"))]
    line = card_line("s-1", (cid("locker"), "Locker 42"), (cid("door"), "the blue door"), (cid("day"), "every Tuesday"))
    annotated = body_of(compass.annotate_superseded(
        [ContextSection(id="protagine-memory", title="Relevant Memories", body=line)], records))
    assert all(noted(record) in annotated for record in records)
    assert compass.dead_value_lines(annotated, records) == 0
    partial = line + " " + noted(records[0]) + " " + noted(records[1])
    assert compass.dead_value_lines(partial, records) == 1


# -- round 3: record identity, record history, one tokenizer --------------------------------------

def test_corrections_from_one_source_are_delivered_per_record():
    """Round 3, finding 5: nine claims from one source message each owe their own correction."""
    key = ("viewer", "s-1")
    rooms = ["Alder", "Birch", "Cedar", "Dogwood", "Elm", "Fir", "Ginkgo", "Hazel", "Ironwood"]
    records = [claim_record(f"the {room} room", "the main hall", cid(room), subject=f"event {n}")
               for n, room in enumerate(rooms)]
    compass.SERVED.remember(key, "\n".join(card_line("s-1", (cid(room), f"the {room} room")) for room in rooms))
    counts = []
    for _ in range(3):
        note = compass.SERVED.corrections(key, records)
        counts.append(sum(line.startswith("- ") for line in note.split("\n")))
        compass.SERVED.remember(key, note)
    assert counts == [8, 1, 0]
    # The same with the nine claims in one card on one line.
    compass.SERVED.clear()
    compass.SERVED.remember(key, card_line("s-1", *((cid(room), f"the {room} room") for room in rooms)))
    counts = []
    for _ in range(3):
        note = compass.SERVED.corrections(key, records)
        counts.append(sum(line.startswith("- ") for line in note.split("\n")))
        compass.SERVED.remember(key, note)
    assert counts == [8, 1, 0]


def test_an_annotation_needs_the_line_to_be_the_superseded_record():
    """Round 3, finding 1: a shared source message or shared words never make a line the record's."""
    design, heater, successor = cid("design"), cid("heater"), cid("design-2")
    record = claim_record("the corner office", "the front lobby", design, subject="design review")
    lines = [
        card_line("s-1", (heater, "the corner office"), subject="heater"),       # same source and words, other claim
        quote_line("s-1", "The design review is in the corner office."),         # the source itself: no record id
        "- " + json.dumps({"evidence_ref": "q1", "source": "turn:s-1", "quote": "The review is in the corner office."}),
        card_line("s-2", (successor, "the front lobby", {"prior_claim_id": design})),  # cites the old id, is not it
        "- [pending] Visit the corner office (due: 2026-10-01T09:00:00+00:00)",   # no record id at all
        card_line("s-1", (design, "the corner office"), subject="design review"),  # the record's own card
        card_line("s-1", (heater, "the corner office"), (design, "the corner office")),  # both claims on one line
    ]
    sections = [ContextSection(id="protagine-memory", title="Relevant Memories", body="\n".join(lines))]
    annotated = body_of(compass.annotate_superseded(sections, [record])).split("\n")
    assert [compass.MARKER in line for line in annotated] == [False] * 5 + [True, True]
    assert annotated[5].endswith(f'[superseded id={design}: now "the front lobby" since 2026-09-19]')
    assert annotated[6].count(compass.MARKER) == 1
    assert compass.dead_value_lines("\n".join(lines), [record]) == 2
    assert compass.dead_value_lines("\n".join(annotated), [record]) == 0
    # A note answers only for the record it names: the heater's claim moved to the same place is still owed.
    moved = claim_record("the corner office", "the front lobby", heater, subject="heater")
    assert compass.dead_value_lines(annotated[6], [record, moved]) == 1
    both = body_of(compass.annotate_superseded([ContextSection(id="m", title="m", body=lines[6])], [record, moved]))
    assert both.count(compass.MARKER) == 2 and compass.dead_value_lines(both, [record, moved]) == 0
    # The served history: the unrelated card and the bare quote owe nothing; the record's own card does.
    compass.SERVED.remember(("viewer", "s-1"), "\n".join(lines[:5]))
    assert compass.SERVED.corrections(("viewer", "s-1"), [record]) == ""
    compass.SERVED.remember(("viewer", "s-1"), lines[5])
    assert f"id={design};" in compass.SERVED.corrections(("viewer", "s-1"), [record])


def test_a_value_restored_after_a_change_is_corrected_by_the_records_history():
    """Round 3, finding 2: A -> B -> A. What a line last told the model about its record (its note) decides."""
    a, b = "2026-09-21T09:00:00+00:00", "2026-09-25T09:00:00+00:00"
    key = ("viewer", "s-1")
    moved = compass.commitment_reschedules([commitment("c-1", "Send the form", b, a)])
    served = body_of(compass.annotate_superseded([ContextSection(id="protagine-commitments", title="Pending",
                                                                 body=commitment_line("c-1", "Send the form", a))],
                                                 moved))
    compass.SERVED.remember(key, served)
    back = compass.commitment_reschedules([commitment("c-1", "Send the form", a, b)])
    note = compass.SERVED.corrections(key, back)
    assert f'- id=c-1; "{b}" (Send the form): [superseded id=c-1: rescheduled to "{a}"' in note
    assert compass.dead_value_lines(served, back) == 1 and compass.dead_value_lines(note, back) == 0
    compass.SERVED.remember(key, note)
    assert compass.SERVED.corrections(key, back) == ""
    # The same when B was first served as a correction line.
    compass.SERVED.clear()
    compass.SERVED.remember(key, commitment_line("c-1", "Send the form", a))
    compass.SERVED.remember(key, compass.SERVED.corrections(key, moved))
    assert f'rescheduled to "{a}"' in compass.SERVED.corrections(key, back)
    # A claim: the record of the first version holds its own value again, and its served note said otherwise.
    first = cid("v1")
    stale = card_line("s-1", (first, "room 100")) + " " + claim_record("room 100", "room 101", first).note()
    restored = claim_record("room 100", "room 100", first)
    assert compass.asserts_superseded(stale, restored)
    assert not compass.asserts_superseded(card_line("s-1", (first, "room 100")), restored)
    compass.SERVED.clear()
    compass.SERVED.remember(key, stale)
    assert f'- id={first}; "room 101"' in compass.SERVED.corrections(key, [restored])


@pytest.mark.asyncio
async def test_a_chain_back_to_an_earlier_value_keeps_a_record_for_every_version(tmp_path):
    """Round 3, finding 2, the ledger: A -> B -> A gives both earlier versions the value held now."""
    from protagine.beliefs.source_projection import SourceClaimProjection
    from protagine.turns import TurnIdempotencyLedger
    from test_source_claim_projection import Model, claim
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    for n, room in enumerate(["room 100", "room 101", "room 100"]):
        text = f"Version {n}: the review room is {room}."
        ledger.record_source(f"v{n}", contact_id="contact-a", session_id=f"session-{n}",
                             messages=[{"role": "user", "content": text}],
                             occurred_at=f"2026-09-0{n + 1}T09:00:00+00:00", derive_claims=True)
        assert await SourceClaimProjection(ledger).process_one(
            Model({text: claim(text, room, subject="review room", predicate="room")}))
    with closing(ledger._connect()) as conn:
        turns = dict(conn.execute("SELECT turn_id, id FROM source_claims").fetchall())
        ids = [turns[f"v{n}"] for n in range(3)]
        conn.executemany("UPDATE source_claims SET retracted_by=? WHERE id=?", list(zip(ids[1:], ids)))
        conn.commit()
    records = {r.record: r for r in compass.claim_supersessions(ledger, contact_id="contact-a", session_id="later")}
    assert set(records) == set(ids[:2]) and {r.current for r in records.values()} == {"room 100"}


@pytest.mark.asyncio
@pytest.mark.parametrize("query, fact", [
    ("Who is on-call?", "Mira is on-call this weekend"),                     # the reviewer's case
    ("Who is on call?", "Mira is on-call this weekend"),                      # the words of a hyphenated term
    ("Where is the café?", "Meet at the café on Main Street"),         # composed and decomposed accents
    ("Who’s covering Mira’s shift?", "Cover Mira's shift on Saturday"),  # curly and straight apostrophes
])
async def test_the_candidate_cap_keeps_an_exact_term_match(query, fact):
    """Round 3, finding 3: queries and documents share one tokenizer, so an exact match ranks above ties."""
    memory = "Unverified recalled evidence:\n" + "\n".join(
        f"- Note {n}: the garden shed paint is drying in batch {n}." for n in range(48))
    sections = [ContextSection(id="protagine-memory", title="Relevant Memories", body=memory, priority=90),
                ContextSection(id="protagine-commitments", title="Pending Commitments", priority=72, body="\n".join([
                    "Open commitments:", commitment_line("c-9", fact, "2026-11-02T09:00:00+00:00")]))]
    judge = keyword_judge("Mira", "caf")
    selected, report = await compass.select_context(sections, query, judge, settings=settings(budget=400), now=NOW)
    ((_, documents),) = judge.calls
    assert report["judged"] == 48 and any(fact[:12] in doc for doc in documents)
    assert fact[:12] in body_of(selected)


def test_values_are_matched_with_the_same_tokenizer():
    """Round 3, finding 3: a value and a line differing only in apostrophes, accents' encoding or case match."""
    desk = claim_record("Mira’s desk", "the east wing", cid("desk"))
    cafe = claim_record("Café Central", "Main Library", cid("cafe"))
    call = claim_record("on-call", "off duty", cid("rota"))
    assert compass.asserts_superseded(claim_quote(desk.record, "Leave it at MIRA'S DESK."), desk)
    assert compass.asserts_superseded(claim_quote(cafe.record, "We meet at Café Central."), cafe)
    assert compass.asserts_superseded(claim_quote(call.record, "Mira is on-call."), call)
    assert not compass.asserts_superseded(claim_quote(call.record, "Mira is on call-backs."), call)
    assert not compass.asserts_superseded(claim_quote(cafe.record, "Café Centrale is closed."), cafe)


def test_a_quotation_of_only_a_timestamp_is_still_the_records_value():
    """Round 3, new P2: a scalar quotation is matched whatever it looks like; only metadata fields are skipped."""
    old = "2026-09-21T09:00:00+00:00"
    record = claim_record(old, "September 29", cid("deadline"), subject="deadline")
    line = claim_quote(record.record, old)
    annotated = body_of(compass.annotate_superseded(
        [ContextSection(id="protagine-memory", title="Relevant Memories", body=line)], [record]))
    assert annotated.endswith(noted(record))
    assert compass.dead_value_lines(line, [record]) == 1 and compass.dead_value_lines(annotated, [record]) == 0
    compass.SERVED.remember(("viewer", "s-1"), line)
    assert f'- id={record.record}; "{old}" (deadline)' in compass.SERVED.corrections(("viewer", "s-1"), [record])
    # The same instant in the line's own metadata is not its value.
    timed = claim_quote(record.record, "The deadline is set.", reported_at=old, observed_at=old)
    assert compass.dead_value_lines(timed, [record]) == 0


def test_the_superseded_value_annotation_ships_off_and_turns_on_only_when_asked(monkeypatch):
    from protagine.memory.compass import ANNOTATION_ENV, records_annotation_enabled
    monkeypatch.delenv(ANNOTATION_ENV, raising=False)
    assert records_annotation_enabled() is False
    for value in ("1", "true", "on", "YES"):
        monkeypatch.setenv(ANNOTATION_ENV, value)
        assert records_annotation_enabled() is True
    for value in ("", "0", "off", "false", "maybe"):
        monkeypatch.setenv(ANNOTATION_ENV, value)
        assert records_annotation_enabled() is False
