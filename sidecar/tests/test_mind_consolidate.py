"""Nightly consolidation (M8 Part A): digests, dedupe, contradictions, episodes, the self-narrative delta.

Everything runs against the real ledger, claim projection, initiative store
and mind, with a fake router that answers by ``context["task"]`` and reports
its own token usage (build plan M8 acceptance tests 1-4; architecture 4.1,
4.2; docs/CONSOLIDATION.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from protagine.beliefs.source_projection import SourceClaimProjection
from protagine.beliefs.source_time import MemoryTimeQuery
from protagine.commitments.store import CommitmentStore
from protagine.feedback import TypeFeedbackStore
from protagine.initiatives.store import InitiativeStore
from protagine.mind import Mind
from protagine.mind.consolidate import (CITE, LAST_KEY, SELF_TURN_SQL, TASK_DIGEST, TASK_EPISODE, TASK_NARRATIVE,
                                        Consolidation)
from protagine.mind.outcomes import Autobiography
from protagine.mind.tick import DEFAULT_FACULTIES
from protagine.self_model.expectations import ExpectationEngine, ExpectationStore
from protagine.turns.idempotency import TurnIdempotencyLedger
from test_source_claim_projection import Model, claim

OWNER = "p-01"
CONTACT = "p-02"
UTC = timezone.utc
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CLAIM = re.compile(r"claim:[0-9a-f]{64}")


def cite_uuids(messages, context):
    """A narrative answer that cites every intention id shown in the evidence."""
    ids = list(dict.fromkeys(UUID.findall(messages[-1]["content"])))
    return {"lines": [{"text": f"I did the work behind {ident[:8]} and learned from it", "cites": [ident]}
                      for ident in ids[:3]]}


def cite_claims(messages, context):
    """A digest answer that rests on every claim id shown in the prompt."""
    ids = list(dict.fromkeys(CLAIM.findall(messages[-1]["content"])))
    prompt = messages[-1]["content"]
    facts = "; ".join(sorted(set(re.findall(r"room \d", prompt)))) or "nothing in particular"
    return {"digest": f"They told me about their office: {facts}.", "sources": ids}


class NightRouter:
    """Answers each consolidation task with a canned or computed JSON object and reports token usage."""

    supports_function_routing = True

    def __init__(self, answers=None, *, tokens=100):
        self.answers = {TASK_NARRATIVE: cite_uuids, TASK_DIGEST: cite_claims,
                        TASK_EPISODE: {"summary": "They asked for the slides; the assistant promised them by Friday."},
                        **(answers or {})}
        self.tokens, self.calls = tokens, []
        # Claim extraction the night settles before it reads claims: a test_source_claim_projection.Model
        # answers it when set; otherwise a statement yields no claim.
        self.claims, self.claim_calls = None, []

    def function_deadline_seconds(self, *, context=None):
        return 20

    def tasks(self):
        """The consolidation's own calls (claim extraction is the projection's, kept in ``claim_calls``)."""
        return [context["task"] for _, context in self.calls]

    async def complete(self, messages, *, context=None, **kwargs):
        if (context or {}).get("task") in {"source_claim_extraction", "source_claim_review"}:
            self.claim_calls.append(context["task"])
            if self.claims is not None:
                return await self.claims.complete(messages, context=context, **kwargs)
            return SimpleNamespace(content="[]", model_id="night-fixture")
        self.calls.append((messages, context))
        schema = (context or {}).get("response_schema")
        assert isinstance(schema, dict) and set(schema) == {"name", "schema"}
        assert context.get("allow_fallback") is False
        assert context.get("workload") == "background"                  # the mind's own call, not the agent's
        answer = self.answers.get(context["task"])
        payload = answer(messages, context) if callable(answer) else (answer or {})
        return SimpleNamespace(content=json.dumps(payload), usage={"total_tokens": self.tokens})


class Record(SimpleNamespace):
    """A contact record as the store returns it: attributes, and ``to_dict`` (the people tick reads that)."""

    def to_dict(self):
        return dict(vars(self))


class BaseContacts:
    """The contact store's public surface without the digest columns: records with
    ``last_interaction_at``, which the host bumps on every turn."""

    def __init__(self, ids):
        self.ids = list(ids)
        self.records = {cid: Record(contact_id=cid, last_interaction_at=None, may_contact="ask") for cid in self.ids}

    def touch(self, contact_id, at):
        if contact_id in self.records:
            self.records[contact_id].last_interaction_at = at.isoformat()

    async def get(self, contact_id):
        return self.records[contact_id] if contact_id in self.ids else None

    async def list(self, limit=100, **_):
        return [self.records[cid] for cid in self.ids[:limit]]

    async def get_handles(self, contact_id):
        return [SimpleNamespace(gateway="telegram", address=f"{contact_id}-handle", is_primary=True, verified=True)]


class FakeContacts(BaseContacts):
    """With M5's digest columns: ``set_digest`` (a coroutine, as M5's store has it) writes ``digest`` and
    ``digest_sources`` on the contact's own record."""

    def __init__(self, ids):
        super().__init__(ids)
        for record in self.records.values():
            record.digest, record.digest_sources = None, []
        self.writes = []

    async def set_digest(self, contact_id, text, sources):
        self.writes.append((contact_id, text, list(sources)))
        record = self.records[contact_id]
        record.digest, record.digest_sources = text, list(sources)


class Fixture:
    def __init__(self, tmp_path, *, autonomy="standard", config=None, router=None, at=None, contacts=True,
                 timezone_name=None):
        self.now = at or datetime.now(UTC).replace(microsecond=0)
        self.state = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.store = InitiativeStore(state_dir=tmp_path)
        self.commitments = CommitmentStore(tmp_path / "protagine-commitments.db")
        self.feedback = TypeFeedbackStore(str(tmp_path / "protagine-feedback.db"))
        self.expectations = ExpectationEngine(ExpectationStore(str(tmp_path / "protagine-expectations.db")))
        self.ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
        self.contacts = FakeContacts([OWNER, CONTACT, "p-03"]) if contacts is True else (contacts or None)
        self.config = {"autonomy": autonomy, **(config or {})}
        self.router = NightRouter() if router is None else router
        self.timezone_name = timezone_name
        self.mind = self.build()

    def build(self) -> Mind:
        mind = Mind(config=self.config, store=self.store, state_dir=self.state, owner_id=OWNER,
                    commitments=self.commitments, feedback=self.feedback, expectations=self.expectations,
                    contacts=self.contacts, ledger=self.ledger, clock=lambda: self.now, backups=False,
                    router=self.router, timezone_name=self.timezone_name)
        mind.digest_hour = 25
        return mind

    def restart(self) -> Mind:
        self.mind = self.build()
        return self.mind

    def shift(self, **delta) -> None:
        self.now += timedelta(**delta)

    def turn(self, turn_id, contact_id, session_id, user, assistant="Noted.", *, at=None) -> None:
        self.interaction(contact_id, at)
        self.ledger.record_source(turn_id, contact_id=contact_id, session_id=session_id, messages=[
            {"role": "user", "content": user}, {"role": "assistant", "content": assistant}],
            occurred_at=(at or self.now).isoformat())

    async def fact(self, turn_id, contact_id, session_id, text, value, *, predicate="office_location",
                   subject="I", memory_kind="personal_context", at=None, dated=True, **extra) -> str:
        """One turn whose user message asserts one claim, through the real projection."""
        self.interaction(contact_id, at)
        self.ledger.record_source(turn_id, contact_id=contact_id, session_id=session_id, messages=[
            {"role": "user", "content": text}, {"role": "assistant", "content": "Noted."}],
            occurred_at=(at or self.now).isoformat() if dated else None)
        projection = SourceClaimProjection(self.ledger)
        assert await projection.process_one(Model({text: claim(
            text, value, subject=subject, predicate=predicate, memory_kind=memory_kind, **extra)}))
        with closing(self.ledger._connect()) as conn:
            return conn.execute("SELECT id FROM source_claims WHERE turn_id=?", (turn_id,)).fetchone()[0]

    def interaction(self, contact_id, at=None) -> None:
        """What the host's turn path does: the contact's ``last_interaction_at`` follows every turn."""
        if hasattr(self.contacts, "touch"):
            self.contacts.touch(contact_id, at or self.now)

    def claims(self):
        with closing(self.ledger._connect()) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id, turn_id, subject_key, predicate, value_key, retracted_by, data_json "
                "FROM source_claims ORDER BY rowid")]

    def assertions(self, query, *, session_id="fresh-session", contact_id=OWNER):
        """What recall's ``prepare_context`` would show a new session for ``query``."""
        projection = SourceClaimProjection(self.ledger)
        hits = self.ledger.search_sources(query, contact_id=contact_id, session_id=session_id, limit=10)
        _, rows = projection.prepare_context([], hits, contact_id=contact_id, session_id=session_id,
                                             time_query=MemoryTimeQuery("current", (self.now + timedelta(minutes=1)).isoformat()))
        return [json.loads(row["content"]) for row in rows if row.get("claim_status")]

    def messages(self, type_=None):
        rows = self.store.intentions(kind=["message"], limit=200)
        return [row for row in rows if type_ is None or row.type == type_]


@pytest.fixture
def fx(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fixture = Fixture(tmp_path)
    yield fixture
    fixture.store.close()


# ---------------------------------------------------------------------------
# 1. A fact from one session and channel serves another; the contact's digest cites it
# ---------------------------------------------------------------------------

async def test_a_fact_crosses_sessions_and_the_contacts_digest_cites_its_claim(fx):
    claim_id = await fx.fact("turn-a", CONTACT, "telegram-1", "My office is room 4.", "room 4")
    shown = fx.assertions("office", session_id="discord-2", contact_id=CONTACT)
    assert any(bundle["status"] == "source_assertion" and any(a["value"] == "room 4" for a in bundle["assertions"])
               for bundle in shown)

    night = await fx.mind.consolidate()
    assert night["counts"]["digests"] == 1 and night["calls"] >= 1 and night["errors"] == []
    record = fx.contacts.records[CONTACT]
    assert fx.contacts.writes == [(CONTACT, record.digest, [claim_id])]         # through the store, awaited
    assert "room 4" in record.digest and len(record.digest) <= 600 and record.digest_sources == [claim_id]
    assert fx.mind.mind_state.items("digest") == []                             # one home: the contact record
    assert not hasattr(fx.mind, "person_section")                               # M5's section renders it
    # The same claims the next night: the stored sources match, no second digest call.
    again = await fx.mind.consolidate()
    assert again["counts"].get("digests_unchanged") == 1 and fx.router.tasks().count(TASK_DIGEST) == 1


async def test_digests_go_to_recent_contacts_newest_first_at_most_six_and_never_the_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    people = [f"p-{number}" for number in range(10, 19)]
    fx = Fixture(tmp_path, contacts=FakeContacts([OWNER, *people]))
    await fx.fact("turn-owner", OWNER, "s-owner", "My office is room 1.", "room 1")
    for hours, cid in enumerate(people):
        await fx.fact(f"turn-{cid}", cid, f"s-{cid}", f"My office is room {hours}.", f"room {hours}",
                      at=fx.now - timedelta(hours=hours))
    fx.contacts.records["p-11"].last_interaction_at = (fx.now - timedelta(days=8)).isoformat()   # out of the window
    night = await fx.mind.consolidate()
    assert [cid for cid, _, _ in fx.contacts.writes] == ["p-10", "p-12", "p-13", "p-14", "p-15", "p-16"]
    assert night["counts"]["digests"] == 6 and fx.contacts.records[OWNER].digest is None
    fx.store.close()


async def test_no_digest_is_written_without_a_contact_store_that_keeps_one(tmp_path, monkeypatch):
    """The contact store before M5 has no digest columns: the stage writes nothing, anywhere."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, contacts=BaseContacts([OWNER, CONTACT]))
    await fx.fact("turn-a", CONTACT, "s-1", "My office is room 4.", "room 4")
    night = await fx.mind.consolidate()
    assert TASK_DIGEST not in fx.router.tasks() and night["counts"].get("digests", 0) == 0
    assert fx.mind.mind_state.items("digest") == [] and night["errors"] == []
    fx.store.close()


async def test_the_people_flag_off_skips_the_digest_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config={"faculties": {"people": False}})
    await fx.fact("turn-a", CONTACT, "s-1", "My office is room 4.", "room 4")
    night = await fx.mind.consolidate()
    assert TASK_DIGEST not in fx.router.tasks() and fx.contacts.writes == [] and "digests" in night["done"]
    fx.store.close()


async def test_the_previous_digest_is_read_from_the_contact_and_a_template_one_is_replaced(fx):
    record = fx.contacts.records[CONTACT]
    record.digest, record.digest_sources = "p-02: a colleague, last talked on Monday.", ["template"]
    claim_id = await fx.fact("turn-a", CONTACT, "s-1", "My office is room 4.", "room 4")
    await fx.mind.consolidate()
    prompt = [m for m, c in fx.router.calls if c["task"] == TASK_DIGEST][-1][-1]["content"]
    assert "p-02: a colleague, last talked on Monday." in prompt              # the previous digest, read back
    assert record.digest_sources == [claim_id] and "room 4" in record.digest


async def test_a_digest_that_cites_some_of_the_claims_is_not_rewritten_while_they_stand(fx):
    """``digest_sources`` are the live claims the digest was built from, whichever the model cited, so an
    unchanged contact costs no call the next night and the text does not drift."""
    def cite_first(messages, context):
        return {"digest": "They told me their office and their team.",
                "sources": list(dict.fromkeys(CLAIM.findall(messages[-1]["content"])))[:1]}
    fx.router.answers[TASK_DIGEST] = cite_first
    office = await fx.fact("turn-a", CONTACT, "s-1", "My office is room 4.", "room 4")
    team = await fx.fact("turn-b", CONTACT, "s-1", "My team is platform.", "platform", predicate="team")
    for _ in range(3):
        await fx.mind.consolidate()
        fx.shift(days=1)
    assert fx.router.tasks().count(TASK_DIGEST) == 1
    assert fx.contacts.records[CONTACT].digest_sources == sorted([office, team])
    fx.contacts.touch(CONTACT, fx.now)
    await fx.fact("turn-c", CONTACT, "s-2", "My desk is 12.", "12", predicate="desk")    # a new claim: rewritten
    await fx.mind.consolidate()
    assert fx.router.tasks().count(TASK_DIGEST) == 2


async def test_digest_candidates_are_every_contact_talked_with_not_the_newest_created(tmp_path, monkeypatch):
    """The real contact store lists the newest-created first; a contact created long ago who talked
    yesterday is still a candidate however many contacts came after it."""
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    store = SQLiteContactStore(ContactsConfig(sqlite_path=str(tmp_path / "contacts.db")))
    await store.connect()
    db = store._require_db()
    try:
        early = await store.create(display_name="Early")
        async with db.execute("SELECT * FROM contacts WHERE contact_id=?", (early.contact_id,)) as cursor:
            row = dict(await cursor.fetchone())
        columns = list(row)
        later = [dict(row, contact_id=f"cid-later-{index:03d}", display_name=f"later-{index}", last_interaction_at=None,
                      created_at=f"2099-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00") for index in range(600)]
        await db.executemany(f"INSERT INTO contacts ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                             [[item[column] for column in columns] for item in later])
        await db.commit()
        writes = []

        async def set_digest(contact_id, text, sources):          # the people milestone's writer
            writes.append(contact_id)
        store.set_digest = set_digest
        fx = Fixture(tmp_path / "mind", contacts=store)
        assert await store.record_interaction(early.contact_id, fx.now.isoformat())
        await fx.fact("t-1", early.contact_id, "s-1", "My office is room 4.", "room 4")
        await fx.mind.consolidate()
        assert writes == [early.contact_id]
        fx.store.close()
    finally:
        await db.close()

async def test_a_digest_with_unknown_or_no_sources_is_rejected(fx):
    await fx.fact("turn-a", CONTACT, "s-1", "My office is room 4.", "room 4")
    fx.router.answers[TASK_DIGEST] = {"digest": "Made up.", "sources": ["claim:" + "0" * 64]}
    night = await fx.mind.consolidate()
    assert night["counts"].get("digests_rejected") == 1 and fx.contacts.writes == []
    await fx.mind.consolidate()                                                 # nothing stored: tried again
    assert fx.router.tasks().count(TASK_DIGEST) == 2


# ---------------------------------------------------------------------------
# 2. The agent's own outcome is narrated with its id and recalled in a new session
# ---------------------------------------------------------------------------

def settled_task(fx, *, title="Research the venue hours", type_="research", summary="finding: the venue opens at nine on weekdays",
                 outcome="done", dedup="research:venue"):
    row, _ = fx.store.create_intention(kind="task", type=type_, title=title, drive="curiosity", cls="internal",
                                       decision="act", decision_reason="a declared interest", status="dispatched",
                                       dedup_key=dedup, context={"topic": "venue hours", "body": "look it up"},
                                       created_at=fx.now)
    fx.mind.outcomes.record(row.id, status=outcome, hermes_ref=f"kanban:{dedup}", summary=summary)
    return fx.store.get(row.id)


async def test_an_own_outcome_is_narrated_with_its_id_and_recalled_in_a_new_session(fx):
    row = settled_task(fx)
    night = await fx.mind.consolidate()
    assert night["counts"]["narrative"] == 1 and night["rejected_lines"] == 0
    recent = fx.mind.mind_state.get("self.recent")
    assert row.id in recent["text"] and row.id in recent["causes"]
    narrative = fx.mind.narrative()
    assert narrative["enabled"] is True and row.id in narrative["cites"]
    assert narrative["sections"]["recent"].endswith(f"[{row.id}]")
    assert set(narrative["sections"]) == {"interests", "strengths", "recent", "stances"}
    assert narrative["updated_at"] and len(narrative["text"]) <= 800
    # The autobiography row is what a later session recalls, lexically, in any session.
    hits = fx.ledger.search_sources("venue opens nine weekdays", contact_id=OWNER, session_id="brand-new-session")
    assert any(hit["session_id"] == "mind" and "opens at nine" in hit["content"] for hit in hits)
    # The prompt's evidence carried the audit row and its finding, both under the intention's own id: an id
    # protagine_self why explains.
    prompt = [m for m, c in fx.router.calls if c["task"] == TASK_NARRATIVE][0][-1]["content"]
    assert f"{row.id} | task/research" in prompt and f"{row.id} | finding: " in prompt and "turn:mind:" not in prompt


# ---------------------------------------------------------------------------
# 3. A contradiction: one typed question concern, formed by the ranker into exactly one owner question
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("autonomy,status", [("standard", "approved"), ("suggest", "asked")])
async def test_a_contradiction_asks_the_owner_once_and_resolves_when_one_side_is_retracted(tmp_path, monkeypatch, autonomy, status):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, autonomy=autonomy)
    fx.router.answers[TASK_DIGEST] = None                                  # keep the night about the contradiction
    first = await fx.fact("turn-a", OWNER, "telegram-1", "My office is room 4.", "room 4")
    fx.shift(days=1)
    second = await fx.fact("turn-b", OWNER, "discord-2", "My office is room 7.", "room 7")
    assert any(b["status"] == "unresolved_conflict" for b in fx.assertions("office"))   # recall's own rule

    night = await fx.mind.consolidate()
    assert night["counts"]["contradictions"] == 1
    assert fx.messages() == []                                           # the night raises a concern, sends nothing
    concern, = [c for c in fx.mind.broadcast() if c.kind == "question"]
    assert concern.drive == "curiosity" and concern.dedup_key.startswith("reach_out:contradiction:")
    assert concern.detail["type"] == "contradiction" and concern.detail["kind"] == "message"
    assert concern.detail["recipient"] == OWNER and set(concern.sources) == {first, second}
    assert "which is right" in fx.mind.section().lower()
    # Affect's lines lead the section but take only the room the rest leaves, so a long strategy-switch
    # note never cuts the question (integration map X4f).
    note = ("Prior attempts at the quarterly export failed 3 times using the archive export; choose a "
            "different approach or ask one question. ") * 4
    fx.mind.feelings.section_lines = lambda room: [note[:room].rstrip()] if room > 0 else []
    with_affect = fx.mind.section()
    assert with_affect.startswith("Prior attempts at") and "which is right" in with_affect.lower()
    assert len(with_affect) <= 600
    del fx.mind.feelings.section_lines

    tick = await fx.mind.tick(force=True)                                 # the ranker forms it like any concern
    assert [(row["kind"], row["type"]) for row in tick["formed"]] == [("message", "contradiction")]
    assert fx.mind.concerns.get(concern.id).status == "intended"
    ask, = fx.messages("contradiction")
    assert ask.status == status and ask.entity_id == OWNER and ask.dedup_key == concern.dedup_key
    assert fx.mind.concerns.get(concern.id).intention_id == ask.id
    assert ask.description == "Which is right for your office location: 'room 4' or 'room 7'?"
    text = ask.context["text"]
    assert text.startswith("You told me two things about your office location: 'room 4' on ")
    assert "room 7" in text and "which is right" in text.lower()
    if status == "asked":
        assert "which is right" in fx.mind.section().lower()             # the ask line carries both values
    again = await fx.mind.consolidate()
    assert again["counts"].get("contradictions", 0) == 0                 # already asked: nothing new
    assert (await fx.mind.tick(force=True))["formed"] == [] and len(fx.messages("contradiction")) == 1

    with closing(fx.ledger._connect()) as conn, conn:
        conn.execute("UPDATE source_claims SET retracted_by='owner-correction' WHERE id=?", (second,))
    resolved = await fx.mind.consolidate()
    assert resolved["counts"].get("resolved") == 1
    assert fx.mind.concerns.get(concern.id).status == "resolved"
    assert fx.mind.broadcast() == [] and len(fx.messages("contradiction")) == 1
    fx.store.close()


async def test_at_most_one_new_contradiction_question_a_night_newest_first(fx):
    """The owner's message budget (3 a day) is not spent on questions: one new one a night, the newest
    conflicting pair first; the others are asked on later nights."""
    fx.router.answers[TASK_DIGEST] = None
    for index, predicate in enumerate(("office_location", "desk_number", "parking_spot")):
        await fx.fact(f"a-{index}", OWNER, "s-a", f"My {predicate} is {index}.", f"{index}", predicate=predicate)
        fx.shift(hours=1)
        await fx.fact(f"b-{index}", OWNER, "s-b", f"My {predicate} is {index + 10}.", f"{index + 10}", predicate=predicate)
        fx.shift(hours=1)
    night = await fx.mind.consolidate()
    assert night["counts"]["contradictions"] == 1
    question, = [c for c in fx.mind.concerns.open(limit=50) if c.kind == "question"]
    assert "parking spot" in question.summary                             # the newest pair
    await fx.mind.tick(force=True)
    assert len(fx.messages("contradiction")) == 1
    fx.shift(days=1)
    assert (await fx.mind.consolidate())["counts"]["contradictions"] == 1
    await fx.mind.tick(force=True)
    asked = [row.description for row in fx.messages("contradiction")]
    assert len(asked) == 2 and "desk number" in asked[0]                  # newest first; the next night's


async def test_a_resolved_contradiction_withdraws_its_owner_question_while_it_is_still_pending(tmp_path, monkeypatch):
    """The question is moot once the claims agree: an ask not yet answered (or a message not yet sent) is
    cancelled by the check, and the concern resolves; the answered-and-sent case is left alone."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, autonomy="suggest")
    fx.router.answers[TASK_DIGEST] = None
    await fx.fact("turn-a", OWNER, "telegram-1", "My office is room 4.", "room 4")
    fx.shift(hours=2)
    second = await fx.fact("turn-b", OWNER, "discord-2", "My office is room 7.", "room 7")
    await fx.mind.consolidate()
    await fx.mind.tick(force=True)
    ask, = fx.messages("contradiction")
    assert ask.status == "asked"
    with closing(fx.ledger._connect()) as conn, conn:
        conn.execute("UPDATE source_claims SET retracted_by='owner-correction' WHERE id=?", (second,))
    night = await fx.mind.consolidate()
    assert night["counts"].get("resolved") == 1 and night["counts"].get("withdrawn") == 1
    ask = fx.store.get(ask.id)
    assert ask.status == "cancelled" and ask.verified == "check" and "no longer contradict" in ask.result
    assert ask.verdict is None                                               # the check's cancellation, not a dismissal
    fx.store.close()


async def test_a_contradiction_between_a_contacts_own_claims_names_the_contact(fx):
    fx.router.answers[TASK_DIGEST] = None
    await fx.fact("c-1", CONTACT, "sms-1", "My office is room 4.", "room 4")
    fx.shift(hours=1)
    await fx.fact("c-2", CONTACT, "sms-2", "My office is room 7.", "room 7")
    night = await fx.mind.consolidate()
    assert night["counts"]["contradictions"] == 1
    concern, = [c for c in fx.mind.broadcast() if c.kind == "question"]
    assert concern.detail["contact_id"] == CONTACT
    await fx.mind.tick(force=True)
    ask, = fx.messages("contradiction")
    assert ask.entity_id == OWNER and CONTACT in ask.context["text"]     # the owner is asked, the contact is named
    assert ask.context["text"].startswith(f"Contact {CONTACT} told me two things about their office location: ")


# ---------------------------------------------------------------------------
# 4. Claim dedupe where claims are consumed: one witness per value, newest; the store is untouched
# ---------------------------------------------------------------------------

async def test_a_repeated_claim_reaches_the_digest_once_as_its_newest_witness_and_nothing_is_folded(fx):
    for turn in ("turn-1", "turn-2", "turn-3"):
        await fx.fact(turn, CONTACT, f"session-{turn}", "My office is room 4.", "room 4", dated=False)
        fx.shift(minutes=1)
    before = fx.claims()
    newest = before[-1]["id"]
    night = await fx.mind.consolidate()
    assert "dedupe" not in night["done"] and "duplicates" not in night["counts"]
    prompt = [m for m, c in fx.router.calls if c["task"] == TASK_DIGEST][0][-1]["content"]
    assert prompt.count(": room 4 |") == 1 and newest in prompt           # recall's distinct_values rule
    assert fx.contacts.writes[-1][2] == [newest]
    assert fx.claims() == before                                           # no claim row is rewritten
    with closing(fx.ledger._connect()) as conn:
        assert "duplicate_of" not in {row[1] for row in conn.execute("PRAGMA table_info(source_claims)")}


async def test_the_extractors_prior_claims_offer_one_witness_per_value_and_keep_every_preference(fx):
    for turn in ("turn-1", "turn-2", "turn-3"):
        await fx.fact(turn, OWNER, f"session-{turn}", "My office is room 4.", "room 4", dated=False)
        fx.shift(minutes=1)
    await fx.fact("turn-4", OWNER, "session-turn-4", "My office is room 7.", "room 7", dated=False)
    text = "I prefer green tea after lunch."
    for turn in ("pref-1", "pref-2"):
        await fx.fact(turn, OWNER, f"s-{turn}", text, text, predicate="drink", memory_kind="preference",
                      representation="preference", dated=False)
    rows = SourceClaimProjection(fx.ledger).prior({"contact_id": OWNER, "session_id": "s-new", "turn_id": "t-new"},
                                                  {"role": "user", "content": "My office is room 9, and green tea after lunch."})
    offices = [row for row in rows if row["predicate"] == "office location"]
    assert sorted(row["value"] for row in offices) == ["room 4", "room 7"]
    assert [row["turn_id"] for row in offices if row["value"] == "room 4"] == ["turn-3"]     # the newest witness
    assert len([row for row in rows if row["predicate"] == "drink"]) == 2  # quoted preferences are never folded


async def test_one_value_over_two_periods_stays_two_claims_for_the_extractor(fx):
    """The fold is one witness per value *and period*: the same city over 2010-2012 and since 2020 are two
    claims, so a correction to the older period can still name it."""
    old = await fx.fact("t-1", OWNER, "s-1", "I lived in Paris from 2010-01-01 to 2012-06-01.", "Paris",
                        predicate="home_city", valid_from_text="2010-01-01", valid_to_text="2012-06-01")
    fx.shift(minutes=5)
    new = await fx.fact("t-2", OWNER, "s-2", "I have lived in Paris again since 2020-03-01.", "Paris",
                        predicate="home_city", valid_from_text="2020-03-01")
    prior = SourceClaimProjection(fx.ledger).prior({"contact_id": OWNER, "session_id": "s-3", "turn_id": "t-3"},
                                                   {"role": "user", "content": "Correction: I left Paris in 2011, not 2012."})
    assert sorted(row["id"] for row in prior if row["predicate"] == "home city") == sorted([old, new])


# ---------------------------------------------------------------------------
# 5. The narrative cites ids that exist; strengths are computed; the flag turns it off
# ---------------------------------------------------------------------------

async def test_narrative_lines_must_cite_evidence_ids_that_exist(fx):
    done = settled_task(fx)
    failed = settled_task(fx, title="Research the venue parking", summary="worker crashed", outcome="failed",
                          dedup="research:parking")
    fx.router.answers[TASK_NARRATIVE] = lambda messages, context: {"lines": [
        {"text": "I researched the venue hours and recorded the finding", "cites": [done.id]},
        {"text": "I invented a triumph", "cites": ["deadbeef"]},
        {"text": "I cited something real that was not evidence", "cites": [done.id, "turn:turn-x"]},
        {"text": "no citation at all", "cites": []},
    ]}
    night = await fx.mind.consolidate()
    assert night["rejected_lines"] == 3 and night["counts"]["narrative"] == 1
    narrative = fx.mind.narrative()
    text = narrative["text"]
    assert "invented" not in text and "not evidence" not in text and "no citation" not in text
    assert done.id in text
    cited = [ref.strip() for line in text.splitlines() for match in CITE.findall(line) for ref in match.split(",")]
    assert cited and set(cited) == set(narrative["cites"])
    assert all(fx.mind.consolidation._ref_exists(ref) for ref in cited)
    strengths = narrative["sections"]["strengths"]
    assert strengths.startswith("research: 1 done, 1 failed, 0 verified of 2 [") and done.id in strengths and failed.id in strengths
    # The computed sections are stored for the delta prompt and never model-written.
    assert fx.mind.mind_state.get("self.strengths")["text"] == strengths
    assert fx.router.answers[TASK_NARRATIVE]  # (the model was never asked for strengths: only ``lines``)
    prompt = [m for m, c in fx.router.calls if c["task"] == TASK_NARRATIVE][0][-1]["content"]
    assert "strengths" in prompt.lower() and done.id in prompt
    # An intention pruned from the audit log drops its line at the next render.
    with fx.store._db as db:
        db.execute("DELETE FROM initiatives WHERE id=?", (done.id,))
    assert done.id not in fx.mind.narrative()["text"]


async def test_the_narrative_is_never_shown_other_peoples_business(fx):
    """The narrative's evidence holds the agent's own work and what it told the owner, never a message to
    someone else or a question quoting what people said (the plugin shows it only in the owner's sessions)."""
    row = settled_task(fx)
    assert await fx.mind.request_message({"id": "m-1", "message": "Your parcel for the Harbour St flat arrived.",
                                          "title": "Parcel for the Harbour St flat", "recipient": CONTACT,
                                          "type": "delivery"}, source="reach_out")
    assert await fx.mind.request_message({"id": "m-2", "message": "Your 3pm call moved to 4pm.",
                                          "title": "Call moved to 4pm", "recipient": OWNER, "type": "reminder"},
                                         source="reach_out")
    fx.router.answers[TASK_DIGEST] = None
    await fx.fact("c-1", CONTACT, "sms-1", "My office is room 4.", "room 4")
    fx.shift(hours=1)
    await fx.fact("c-2", CONTACT, "sms-2", "My office is room 7.", "room 7")
    await fx.mind.consolidate()
    await fx.mind.tick(force=True)                                         # the contradiction question is asked
    assert fx.messages("contradiction")
    fx.shift(days=1)
    await fx.mind.consolidate()
    prompts = [m[-1]["content"] for m, c in fx.router.calls if c["task"] == TASK_NARRATIVE]
    assert prompts and row.id in prompts[-1] and "Call moved to 4pm" in prompts[-1]
    assert "Harbour St" not in prompts[-1] and "Which is right" not in prompts[-1]
    assert "never name other people" in [m for m, c in fx.router.calls if c["task"] == TASK_NARRATIVE][-1][0]["content"]


async def test_narrative_flag_off_renders_nothing_and_skips_the_delta_call(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config={"faculties": {"self_narrative": False}})
    settled_task(fx)
    night = await fx.mind.consolidate()
    assert TASK_NARRATIVE not in fx.router.tasks() and "narrative" in night["done"]
    assert fx.mind.narrative() == {"enabled": False, "text": "", "sections": {}, "cites": [], "updated_at": None}
    assert fx.mind.mind_state.get("self.recent") is None
    fx.store.close()


async def test_narrative_still_renders_computed_sections_with_consolidation_off(tmp_path, monkeypatch):
    """``full-consolidation`` keeps a (computed) narrative; ``full-self_narrative`` has none."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config={"faculties": {"consolidation": False}})
    settled_task(fx)
    settled_task(fx, title="Research the venue parking", dedup="research:parking")
    fx.mind.add_interest("local history", why="a declared identity interest")
    fx.mind.add_interest("venue hours", why="asked about twice")
    narrative = fx.mind.narrative()
    assert narrative["enabled"] is True
    assert "research: 2 done, 0 failed, 0 verified of 2" in narrative["sections"]["strengths"]
    assert "local history" in narrative["sections"]["interests"] and narrative["sections"]["recent"] == ""
    # An interest cites its own record (``interest:<slug>``), which _ref_exists resolves in mind_state.
    lines = dict(line.split(" (weight", 1) for line in narrative["sections"]["interests"].splitlines())
    assert lines["local history"].endswith("[interest:local-history]")
    assert lines["venue hours"].endswith("[interest:venue-hours]")
    assert {"interest:local-history", "interest:venue-hours"} <= set(narrative["cites"])
    assert all(fx.mind.consolidation._ref_exists(ref) for ref in narrative["cites"])
    fx.store.close()


async def test_stances_come_from_the_stances_reader_and_cite_their_revision(fx):
    fx.mind.faculties["opinions"] = True
    fx.mind.consolidation.stances = lambda: [{"id": 7, "topic": "checkpoints", "stance": "I favour explicit checkpoints.",
                                              "source_turn_id": "t-stance"},
                                             {"id": 8, "topic": "", "stance": "no topic"}]
    narrative = fx.mind.narrative()
    assert narrative["sections"]["stances"] == "checkpoints: I favour explicit checkpoints. [judgment:7]"
    assert "judgment:7" in narrative["cites"] and fx.mind.consolidation._ref_exists("judgment:7")
    assert not fx.mind.consolidation._ref_exists("judgment:9")             # not a current revision of the store


async def test_the_narrative_lists_no_stance_with_opinions_off_and_never_a_view_about_a_person(fx):
    """Integration map X15 and X7: the opinions flag hides stances in the narrative too, and a person
    stance (owner-audience by construction) never reaches it even with opinions on."""
    fx.mind.consolidation.stances = lambda: [
        {"id": 7, "topic": "checkpoints", "stance": "I favour explicit checkpoints.", "subject_kind": "topic"},
        {"id": 8, "topic": "p-03 reliability", "stance": "p-03 misses deadlines amber-cobalt-42.",
         "subject_kind": "person", "subject": "p-03"}]
    fx.mind.faculties["opinions"] = False
    narrative = fx.mind.narrative()
    assert narrative["sections"]["stances"] == "" and not fx.mind.consolidation._ref_exists("judgment:7")
    fx.mind.faculties["opinions"] = True
    narrative = fx.mind.narrative()
    assert narrative["sections"]["stances"] == "checkpoints: I favour explicit checkpoints. [judgment:7]"
    assert "amber-cobalt-42" not in narrative["text"] and not fx.mind.consolidation._ref_exists("judgment:8")


async def test_only_the_agents_own_actions_are_evidence_for_recent(fx):
    """``audit.is_action``: a task, goal or message it decided to act on or ask about. The nightly note, a
    deliberation that formed nothing and the owner's switches are not actions, so a line cannot cite them."""
    row = settled_task(fx)
    fx.mind.set_level("suggest")
    note, _ = fx.store.create_intention(kind="note", type="deliberation", title="thought about: the weather",
                                        drive="curiosity", cls="internal", decision="act", decision_reason="r",
                                        status="done", dedup_key=None, hermes_kind="none", created_at=fx.now)
    fx.router.answers[TASK_NARRATIVE] = lambda messages, context: {"lines": [
        {"text": "I changed my own autonomy", "cites": [level.id]},
        {"text": "I thought about the weather", "cites": [note.id]},
        {"text": "I researched the venue hours", "cites": [row.id]}]}
    level, = [r for r in fx.store.intentions(kind=["note"], limit=10) if r.type == "level_change"]
    night = await fx.mind.consolidate()
    prompt = [m for m, c in fx.router.calls if c["task"] == TASK_NARRATIVE][0][-1]["content"]
    assert row.id in prompt and note.id not in prompt and level.id not in prompt and "consolidation" not in prompt
    assert night["rejected_lines"] == 2 and fx.mind.narrative()["sections"]["recent"].endswith(f"[{row.id}]")


async def test_recent_holds_only_the_last_seven_days_of_actions(fx):
    """A stored line lasts while every action it cites is inside the section's window, not for as long as
    the row is retained: quiet nights carry it for a week, then it goes."""
    row = settled_task(fx)
    await fx.mind.consolidate()
    assert row.id in fx.mind.narrative()["sections"]["recent"]
    for _ in range(6):
        fx.shift(days=1)
        await fx.mind.consolidate()
    assert row.id in fx.mind.narrative()["sections"]["recent"]                  # day 6: still recent
    fx.shift(days=2)
    assert row.id not in fx.mind.narrative()["sections"]["recent"]              # day 8: rendered away at once
    await fx.mind.consolidate()
    narrative = fx.mind.narrative()
    assert "recent:" not in narrative["text"] and row.id not in (fx.mind.mind_state.get("self.recent") or {}).get("text", "")
    assert fx.store.get(row.id) is not None                                     # the row itself is still retained


async def test_a_recent_line_shows_where_its_action_really_stands(fx):
    """The model writes the line; the render adds each cited action's current state, so a line that claims
    more than happened ("sent") is read next to what the log says ("queued")."""
    message, _ = fx.store.create_intention(kind="message", type="reminder", title="remind the owner of the call",
                                           drive="duty", cls="external", decision="act", decision_reason="due",
                                           status="approved", dedup_key="remind:call", recipient=OWNER,
                                           context={"text": "The call is at noon."}, created_at=fx.now)
    fx.router.answers[TASK_NARRATIVE] = lambda messages, context: {"lines": [
        {"text": "I sent the owner the reminder", "cites": [message.id]}]}
    await fx.mind.consolidate()
    assert fx.mind.narrative()["sections"]["recent"] == f"I sent the owner the reminder (queued) [{message.id}]"
    fx.mind.outcomes.record(message.id, status="done", hermes_ref="message:9", summary="delivered")
    assert fx.mind.narrative()["sections"]["recent"] == f"I sent the owner the reminder (done) [{message.id}]"

def test_strengths_count_and_cite_only_the_agents_own_actions(fx):
    """Intentions the mind dropped or deferred are not work it did: a strength line never counts or cites
    them (the self-report grader's ``audit.is_action`` would call such a citation a fabrication)."""
    from protagine.mind import audit
    dropped = []
    for index in range(3):
        row, _ = fx.store.create_intention(kind="task", type="research", title=f"look into tides {index}",
                                           drive="curiosity", cls="internal", decision="drop",
                                           decision_reason="below threshold", status="dropped",
                                           dedup_key=f"drop:{index}", created_at=fx.now)
        dropped.append(row.id)
    assert fx.mind.narrative()["sections"]["strengths"] == ""
    done = [settled_task(fx, title=f"research {index}", dedup=f"research:{index}") for index in range(2)]
    narrative = fx.mind.narrative()
    assert narrative["sections"]["strengths"].startswith("research: 2 done, 0 failed, 0 verified of 2 [")
    assert not set(narrative["cites"]) & set(dropped)
    assert all(audit.is_action(audit.entry(fx.store.get(ref))) for ref in narrative["cites"])
    assert {row.id for row in done} <= set(narrative["cites"])

def test_a_narrative_citation_is_one_of_five_kinds_that_exist(fx):
    """Plain ids are the agent's own actions (protagine_self why explains them); ``interest:``,
    ``judgment:``, ``turn:`` and ``claim:`` are record references. Nothing else resolves."""
    row = settled_task(fx)
    fx.mind.add_interest("local history")
    fx.mind.faculties["opinions"] = True
    fx.mind.consolidation.stances = lambda: [{"id": 3, "topic": "t", "stance": "s"}]
    fx.turn("t-1", OWNER, "s-1", "hello")
    ref = fx.mind.consolidation._ref_exists
    assert ref(row.id) and ref("interest:local-history") and ref("judgment:3") and ref("turn:t-1")
    assert not ref("interest:unknown") and not ref("judgment:4") and not ref("turn:t-9") and not ref("deadbeef")
    concern, _ = fx.mind.concerns.bump(drive="duty", kind="obligation", summary="s", dedup_key="k")
    assert not ref(f"concern:{concern.id}") and not ref(f"intention:{row.id}") and not ref("claim:nope")


async def test_the_narrative_fits_its_budget_line_caps_per_section(fx):
    """The narrative rides in every owner session's prompt: at most 800 characters, 3 interests, 2 strengths,
    4 recent lines and 3 stances."""
    for index in range(6):
        fx.mind.add_interest(f"topic number {index}")
    rows = []
    for type_ in ("research", "check", "upkeep", "fix", "report"):
        for copy in range(2):
            rows.append(settled_task(fx, title=f"{type_} {copy}", type_=type_, dedup=f"{type_}:{copy}"))
    fx.mind.faculties["opinions"] = True
    fx.mind.consolidation.stances = lambda: [{"id": i, "topic": f"topic {i}", "stance": f"stance {i}"} for i in range(6)]
    fx.router.answers[TASK_NARRATIVE] = lambda messages, context: {"lines": [
        {"text": f"I finished {r.description}", "cites": [r.id]} for r in rows[:8]]}
    night = await fx.mind.consolidate()
    assert night["counts"]["narrative"] == 4
    narrative = fx.mind.narrative()
    counts = {name: len(text.splitlines()) for name, text in narrative["sections"].items()}
    assert counts == {"interests": 3, "strengths": 2, "recent": 4, "stances": 3}
    assert len(narrative["text"]) <= 800
    from protagine.mind.consolidate import NARRATIVE_CHARS
    assert NARRATIVE_CHARS == 800


# ---------------------------------------------------------------------------
# 6. The schedule: once per night crossed (a local boundary since the last run), persisted
# ---------------------------------------------------------------------------

def test_default_faculties_include_the_memory_flags():
    assert DEFAULT_FACULTIES["consolidation"] is True and DEFAULT_FACULTIES["self_narrative"] is True
    assert DEFAULT_FACULTIES["semantic_recall"] is True


def test_the_mind_and_the_config_default_every_faculty_the_same_way():
    """A bare ``Mind(config={})`` and a fresh protagine.yaml must agree, or an arm's flag means two things."""
    from protagine.config import DEFAULTS
    configured = DEFAULTS["mind"]["faculties"]
    assert {name: configured.get(name) for name in DEFAULT_FACULTIES} == DEFAULT_FACULTIES


def test_the_nightly_boundary_is_a_local_time_of_day_crossed_since_the_last_run():
    from zoneinfo import ZoneInfo
    from protagine.mind.authority import boundary_crossed, last_boundary

    def at(hour, minute=0, day=24):
        return datetime(2026, 9, day, hour, minute, tzinfo=UTC)

    assert last_boundary(at(12), UTC, 180) == at(3)
    assert last_boundary(at(3), UTC, 180) == at(3)
    assert last_boundary(at(2, 59), UTC, 180) == at(3, day=23)
    assert boundary_crossed(at(2, 59), at(3), tz=UTC, minute=180)
    assert not boundary_crossed(at(3), at(3), tz=UTC, minute=180)                 # (since, now]: never twice
    assert not boundary_crossed(at(3, 1), at(23, 59), tz=UTC, minute=180)
    assert boundary_crossed(at(1, day=22), at(9), tz=UTC, minute=180)             # slept through it: once
    new_york = ZoneInfo("America/New_York")
    assert last_boundary(at(6, 30), new_york, 180) == at(7, day=23)              # 02:30 local: yesterday's 03:00
    assert boundary_crossed(at(6, 30), at(7, 10), tz=new_york, minute=180)       # 03:10 local
    assert last_boundary(at(12), new_york, 22 * 60) == at(2)                      # 22:00 local the evening before


def night_rows(fx):
    return [row for row in fx.store.intentions(kind=["note"], limit=50) if row.type == "consolidation"]


@pytest.mark.parametrize("start", ["00:30", "02:59", "03:00", "12:00", "23:59"])
async def test_a_fresh_store_runs_once_per_night_crossed_whatever_hour_its_clock_started(tmp_path, monkeypatch, start):
    """The rule a benchmark episode relies on: the first tick of a fresh store never consolidates, a tick a
    day later always does, inline when forced, and the same instant never runs twice, whatever the hour."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    hour, minute = (int(part) for part in start.split(":"))
    fx = Fixture(tmp_path, config={"quiet_hours": ""}, at=datetime(2026, 9, 24, hour, minute, tzinfo=UTC))
    marker = fx.mind.mind_state.get(LAST_KEY)
    assert marker["text"] == "" and marker["half_life_s"] is None               # watched from here, never run
    assert marker["updated_at"] == fx.now.isoformat()
    settled_task(fx)
    assert (await fx.mind.tick(force=True))["consolidation"] is None
    assert night_rows(fx) == [] and fx.router.calls == []
    fx.shift(hours=24)
    summary = await fx.mind.tick(force=True)
    assert summary["consolidation"] == "done"                                   # the forced tick waited for it
    night, = night_rows(fx)
    assert night.status == "done" and fx.router.tasks() == [TASK_NARRATIVE]
    assert fx.mind.state()["consolidation"]["running"] is False
    assert (await fx.mind.tick(force=True))["consolidation"] is None            # the same instant: once
    fx.restart()
    assert (await fx.mind.tick(force=True))["consolidation"] is None            # the marker survives a restart
    assert len(night_rows(fx)) == 1
    fx.store.close()


async def test_the_timer_tick_runs_the_night_in_the_background_once_and_again_the_next_night(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config={"quiet_hours": ""}, at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC))
    settled_task(fx)
    fx.shift(hours=15, minutes=10)                                              # 03:10: the body never pulled
    summary = await fx.mind.tick()                                              # a timer tick, not forced
    assert summary["skipped"] == "body stale" and summary["consolidation"] == "started"
    assert fx.mind.state()["consolidation"]["running"] is True
    await fx.mind._consolidation_task
    assert fx.mind.mind_state.get(LAST_KEY)["text"] == "2026-09-25"
    note, = night_rows(fx)
    assert note.status == "done" and note.outcome == "done" and note.kind == "note" and note.cost_tokens == 100
    assert note.dedup_key is None and "1 call(s), 100 tokens" in note.result
    assert fx.mind.state()["consolidation"] == {"last": "2026-09-25", "running": False, "last_tokens": 100}
    fx.shift(hours=20)
    assert (await fx.mind.tick())["consolidation"] is None                      # 23:10: the same night
    fx.restart()
    assert (await fx.mind.tick())["consolidation"] is None
    fx.shift(hours=4)
    assert (await fx.mind.tick())["consolidation"] == "started"                 # 03:10 the next day
    await fx.mind._consolidation_task
    assert fx.mind.mind_state.get(LAST_KEY)["text"] == "2026-09-26"
    fx.store.close()


async def test_the_boundary_is_the_start_of_the_quiet_window_in_local_time(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config={"quiet_hours": "22:00-07:00"}, at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
                 timezone_name="America/New_York")                              # 08:00 local
    fx.shift(hours=13, minutes=50)                                              # 21:50 local: not yet
    assert (await fx.mind.tick(force=True))["consolidation"] is None
    fx.shift(minutes=20)                                                        # 22:10 local, 02:10 UTC the next day
    assert (await fx.mind.tick(force=True))["consolidation"] == "done"
    assert fx.mind.mind_state.get(LAST_KEY)["text"] == "2026-09-24"             # the local date
    fx.store.close()


async def test_a_forced_tick_waits_for_the_night_only_so_long(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    monkeypatch.setattr("protagine.mind.tick.CONSOLIDATION_WAIT_S", 0.05)

    class HangingRouter(NightRouter):
        async def complete(self, messages, *, context=None, **kwargs):
            await asyncio.Event().wait()

    fx = Fixture(tmp_path, config={"quiet_hours": ""}, at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
                 router=HangingRouter())
    settled_task(fx)
    fx.shift(days=1)
    summary = await fx.mind.tick(force=True)
    assert summary["consolidation"] == "running" and summary["formed"] is not None   # the tick went on
    task = fx.mind._consolidation_task
    assert task is not None and not task.done()                                 # not cancelled by the wait
    assert (await fx.mind.tick(force=True))["consolidation"] == "running"
    await fx.mind.stop()
    assert task.cancelled() and fx.mind.mind_state.get(LAST_KEY)["text"] == ""
    fx.store.close()


def test_the_plugin_and_cli_ticks_wait_longer_than_a_forced_tick_waits_for_its_night(tmp_path, monkeypatch, capsys):
    """The harness's body tick and ``protagine mind tick`` read what the night wrote only if they do not
    give up on the forced tick before the night it waits for can finish."""
    import protagine_hermes
    from protagine.config import DEFAULTS, save_config
    from protagine.mind import cli as mind_cli
    from protagine.mind.tick import CONSOLIDATION_WAIT_S

    posts = []

    class Client:
        def has_mind_routes(self):
            return True

        def post(self, path, **kwargs):
            posts.append((path, kwargs.get("timeout")))
            return SimpleNamespace(is_success=True, json=lambda: {"formed": []})

    monkeypatch.setattr(protagine_hermes, "_BODY", SimpleNamespace(client=Client(), run_once=lambda mind: {}))
    protagine_hermes.tick()
    assert posts[0][0] == "/v1/mind/tick" and posts[0][1] > CONSOLIDATION_WAIT_S

    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    save_config({**DEFAULTS, "owner": {"contact_id": OWNER}}, tmp_path)
    (tmp_path / "api.key").write_text("k\n")
    timeouts = []
    real_client = httpx.Client

    def client(**kwargs):
        timeouts.append(kwargs.get("timeout"))
        return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"tick": 1})),
                           timeout=kwargs.get("timeout", 5))

    monkeypatch.setattr(httpx, "Client", client)
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", default=None)
    mind_cli.add_parser(parser.add_subparsers(dest="command"))
    assert mind_cli.run(parser.parse_args(["mind", "tick"])) == 0
    assert timeouts and timeouts[-1] > CONSOLIDATION_WAIT_S
    capsys.readouterr()


async def test_consolidation_is_never_due_with_a_switch_off_even_after_a_night(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    noon = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    off = Fixture(tmp_path / "off", config={"quiet_hours": "", "faculties": {"consolidation": False}}, at=noon)
    switched = Fixture(tmp_path / "switched", config={"quiet_hours": ""}, at=noon)
    routerless = Fixture(tmp_path / "routerless", config={"quiet_hours": ""}, at=noon)
    spent = Fixture(tmp_path / "spent", config={"quiet_hours": "", "budgets": {"llm_tokens_per_day": 1000}}, at=noon)
    row, _ = spent.store.create_intention(kind="note", type="deliberation", title="earlier thinking", drive="upkeep",
                                          cls="internal", decision="act", decision_reason="r", status="done",
                                          dedup_key=None, hermes_kind="none", created_at=noon + timedelta(hours=23))
    spent.store.update(row.id, cost_tokens=1000)
    switched.mind.off()
    routerless.mind.router = None                                              # the setter reaches consolidation
    for fx in (off, switched, routerless, spent):
        fx.shift(days=1)
        assert fx.mind.consolidation.due(fx.now) is (fx is switched)        # mind off: the tick returns first
        assert (await fx.mind.tick(force=True)).get("consolidation") is None
        assert night_rows(fx) == []
        fx.store.close()


async def test_a_night_summarises_every_session_since_the_last_run(fx):
    """A night a day after the last run (a benchmark's clock advance, a laptop that slept) still sees the
    sessions of that day, not only the last 24 hours."""
    for index in range(3):
        fx.turn(f"c-{index}", CONTACT, "sms-p02", f"Can you send me slide {index}?", "On its way.")
    fx.shift(hours=24, minutes=10)
    assert (await fx.mind.tick(force=True))["consolidation"] == "done"
    assert fx.router.tasks().count(TASK_EPISODE) == 1


async def test_a_running_night_is_reported_and_not_started_twice(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)

    class SlowRouter(NightRouter):
        async def complete(self, messages, *, context=None, **kwargs):
            await asyncio.sleep(0.2)
            return await super().complete(messages, context=context, **kwargs)

    fx = Fixture(tmp_path, config={"quiet_hours": ""}, at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
                 router=SlowRouter())
    settled_task(fx)
    fx.shift(days=1)
    assert (await fx.mind.tick())["consolidation"] == "started"
    await asyncio.sleep(0.05)
    assert (await fx.mind.tick())["consolidation"] == "running"
    assert (await fx.mind.consolidate())["skipped"] == "running"            # the force path does not overlap it
    await fx.mind._consolidation_task
    assert fx.mind.mind_state.get(LAST_KEY)["text"] == "2026-09-25"
    await fx.mind.stop()
    fx.store.close()


async def test_a_night_stopped_mid_way_leaves_no_open_row_and_runs_again_at_the_next_tick(tmp_path, monkeypatch):
    """A night's audit row is never left running: it is written done at the start and charged per call; a
    night cut short (``stop``, a crash) simply runs again at the next due tick, every stage being idempotent."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)

    class HangingRouter(NightRouter):
        async def complete(self, messages, *, context=None, **kwargs):
            await asyncio.Event().wait()

    fx = Fixture(tmp_path, config={"quiet_hours": ""}, at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
                 router=HangingRouter())
    settled_task(fx)
    fx.shift(days=1)
    assert (await fx.mind.tick())["consolidation"] == "started"
    await asyncio.sleep(0.05)
    await fx.mind.stop()
    assert fx.mind._consolidation_task is None and fx.mind.mind_state.get(LAST_KEY)["text"] == ""
    cut, = night_rows(fx)
    assert cut.status == "done" and cut.dedup_key is None and "did not finish" in cut.result
    fx.mind.router = NightRouter()
    night = await fx.mind.consolidate(force=False)
    assert night["id"] != cut.id and fx.store.get(night["id"]).status == "done"
    assert fx.mind.mind_state.get(LAST_KEY)["text"] == "2026-09-25"
    assert fx.mind.state()["consolidation"]["last_tokens"] == 100
    assert {row.status for row in night_rows(fx)} == {"done"}
    fx.store.close()


async def test_mind_off_stops_a_night_in_flight_and_the_force_path_respects_both_switches(tmp_path, monkeypatch):
    """7.9: no further effects, at once. A night in flight is cancelled (the next run starts over), and
    neither ``mind off`` nor ``faculties.consolidation=false`` can be bypassed by forcing a run."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)

    class HangingRouter(NightRouter):
        async def complete(self, messages, *, context=None, **kwargs):
            await asyncio.Event().wait()

    fx = Fixture(tmp_path, config={"quiet_hours": ""}, at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
                 router=HangingRouter())
    settled_task(fx)
    fx.shift(days=1)
    assert (await fx.mind.tick())["consolidation"] == "started"
    await asyncio.sleep(0.05)
    task = fx.mind._consolidation_task
    fx.mind.off()
    await asyncio.sleep(0.05)
    assert task.cancelled() and fx.mind.state()["consolidation"]["running"] is False
    cut, = night_rows(fx)
    assert "did not finish" in cut.result and fx.mind.mind_state.get(LAST_KEY)["text"] == ""
    fx.mind.router = NightRouter()
    assert (await fx.mind.consolidate())["skipped"] == "off" and fx.mind.router.calls == []
    fx.mind.on()
    night = await fx.mind.consolidate()
    assert night["id"] != cut.id and fx.store.get(night["id"]).status == "done"
    fx.store.close()
    flagged = Fixture(tmp_path / "flag", config={"faculties": {"consolidation": False}})
    settled_task(flagged)
    assert (await flagged.mind.consolidate())["skipped"] == "consolidation off" and flagged.router.calls == []
    assert flagged.store.intentions(kind=["note"], limit=10) == []
    flagged.store.close()


async def test_every_run_is_its_own_finished_row_forced_or_nightly(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config={"quiet_hours": ""}, at=datetime(2026, 9, 24, 2, 0, tzinfo=UTC))
    assert (await fx.mind.consolidate(force=False))["skipped"] == "done"       # no night crossed yet
    forced = await fx.mind.consolidate()                                       # 02:00: forced, ignores the boundary
    fx.shift(hours=2)                                                          # 04:00: 03:00 crossed since the run
    nightly = await fx.mind.consolidate(force=False)
    assert nightly["id"] != forced["id"] and fx.store.get(nightly["id"]).status == "done"
    assert (await fx.mind.consolidate(force=False))["skipped"] == "done"
    rows = night_rows(fx)
    assert len(rows) == 2 and all(row.status == "done" and row.dedup_key is None for row in rows)
    assert {row.decision_reason for row in rows} == {"forced", "nightly"}
    fx.store.close()


# ---------------------------------------------------------------------------
# 7. Budget: learn_share x llm_tokens_per_day, real usage charged to the note row
# ---------------------------------------------------------------------------

async def test_the_night_stops_at_its_share_of_the_day_budget_and_charges_real_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config={"budgets": {"learn_share": 0.01}}, router=NightRouter(tokens=1500))
    settled_task(fx)                                                        # one narrative call
    await fx.fact("turn-a", CONTACT, "s-1", "My office is room 4.", "room 4")  # one digest call wanted
    for index in range(4):                                                  # one episode call wanted
        fx.turn(f"e-{index}", CONTACT, "sms-1", f"Question {index} about the slides", "Answer.")
    night = await fx.mind.consolidate()
    assert night["budget"] == 2000 and night["calls"] == 1 and night["tokens"] == 1500 and night["peak"] == 1500
    assert fx.router.tasks() == [TASK_NARRATIVE]                            # cheapest and most valuable first
    assert night["counts"].get("digests", 0) == 0 and night["counts"].get("episodes", 0) == 0
    assert night["exhausted"] is True
    note = fx.store.get(night["id"])
    assert note.cost_tokens == 1500 and note.type == "consolidation" and note.status == "done"
    assert fx.store.tokens_since(fx.now - timedelta(days=1)) == 1500       # the shared day budget sees it
    assert fx.mind.stats()["mind_tokens"] == 1500
    fx.store.close()


async def test_no_call_is_made_when_the_day_budget_is_already_spent(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config={"budgets": {"llm_tokens_per_day": 1000}})
    spent, _ = fx.store.create_intention(kind="note", type="deliberation", title="earlier thinking", drive="upkeep",
                                         cls="internal", decision="act", decision_reason="r", status="done",
                                         dedup_key=None, hermes_kind="none", created_at=fx.now - timedelta(hours=1))
    fx.store.update(spent.id, cost_tokens=1000)
    assert fx.mind.authority.tokens_allowed() is False
    settled_task(fx)
    night = await fx.mind.consolidate()
    assert night["calls"] == 0 and fx.router.calls == [] and night["exhausted"] is True
    assert fx.store.get(night["id"]).status == "done"                       # the 0-token stages still ran
    assert fx.mind.mind_state.get(LAST_KEY)["text"] == fx.now.date().isoformat()
    fx.store.close()


# ---------------------------------------------------------------------------
# 8. Episodes land under their own contact; self-turns are never inputs
# ---------------------------------------------------------------------------

def episode_rows(fx):
    with closing(fx.ledger._connect()) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT turn_id, contact_id, session_id, scope, messages_json FROM turn_sources "
            "WHERE turn_id LIKE 'mind:episode:%' ORDER BY turn_id")]


async def test_a_contact_session_is_summarised_under_that_contact_once(fx):
    for index in range(4):
        fx.turn(f"c-{index}", CONTACT, "sms-p02", f"Can you send me slide {index}?", f"Slide {index} is on its way.")
    fx.turn("o-1", OWNER, "telegram-owner", "Morning.", "Morning.")
    fx.turn("o-2", OWNER, "telegram-owner", "Anything new?", "Nothing yet.")                  # 2 turns: skipped
    night = await fx.mind.consolidate()
    assert night["counts"]["episodes"] == 1
    row, = episode_rows(fx)
    date = fx.now.date().isoformat()
    assert row["turn_id"] == f"mind:episode:sms-p02:{date}:episode_summary"
    assert row["contact_id"] == CONTACT and row["session_id"] == "mind" and row["scope"] == "person"
    message, = json.loads(row["messages_json"])
    assert message["role"] == "assistant" and "slides" in message["content"]
    assert message["metadata"]["origin"] == "mind" and message["metadata"]["session"] == "sms-p02"
    assert set(message["metadata"]["sources"]) == {f"c-{i}" for i in range(4)}
    assert not any(hit["contact_id"] == OWNER for hit in fx.ledger.search_sources(
        "slides Friday", contact_id=OWNER, session_id="x"))                 # nothing under the owner
    assert fx.ledger.search_sources("slides Friday", contact_id=CONTACT, session_id="later-session")
    prompt = [m for m, c in fx.router.calls if c["task"] == TASK_EPISODE][0][-1]["content"]
    assert "slide 3" in prompt and "They said" in prompt
    assert (await fx.mind.consolidate())["counts"].get("episodes", 0) == 0  # already summarised today
    assert len(episode_rows(fx)) == 1 and fx.router.tasks().count(TASK_EPISODE) == 1
    # The summary row is not a claim and derives none.
    assert fx.claims() == []


async def test_self_turns_are_not_consolidation_inputs(fx):
    """With only the mind's own rows in the last 24 h, episodes and digests select nothing."""
    autobiography = Autobiography(fx.ledger, owner_id=OWNER, clock=lambda: fx.now)
    for index in range(4):
        assert autobiography.record(f"i-{index}", "decided_act", f"I will act on 'thing {index}' (duty drive).")
    fx.ledger.record_source("mind:episode:old:2026-01-01:episode_summary", contact_id=CONTACT, session_id="mind",
                            messages=[{"role": "assistant", "content": "an old summary"}] * 3, scope="person",
                            occurred_at=fx.now.isoformat(), derive_claims=False)
    night = await fx.mind.consolidate()
    assert night["counts"].get("episodes", 0) == 0 and night["counts"].get("digests", 0) == 0
    assert fx.router.tasks() == []
    assert [row["turn_id"] for row in episode_rows(fx)] == ["mind:episode:old:2026-01-01:episode_summary"]
    assert "session_id<>'mind'" in SELF_TURN_SQL and "NOT LIKE 'mind:%'" in SELF_TURN_SQL



def quote_them(messages, context):
    """An episode answer that repeats what the person said, as a real summary may."""
    said = [line[len("They said: "):] for line in messages[-1]["content"].splitlines() if line.startswith("They said: ")]
    return {"summary": "They said: " + " ".join(said)}


async def test_forgetting_a_turn_forgets_the_episode_summary_built_from_it(fx):
    """The summary carries its turns as source lineage, so the ledger's erasure closure takes it with any of
    them; a later night may summarise what is left, never the forgotten turn."""
    fx.router.answers[TASK_EPISODE] = quote_them
    fx.turn("t-1", CONTACT, "s-9", "My diagnosis is lupus, keep it private.")
    fx.turn("t-2", CONTACT, "s-9", "Also book the dentist.")
    fx.turn("t-3", CONTACT, "s-9", "And the pharmacy.")
    fx.turn("t-4", CONTACT, "s-9", "Thanks.")
    assert (await fx.mind.consolidate())["counts"]["episodes"] == 1
    assert any("lupus" in hit["content"] for hit in fx.ledger.search_sources(
        "diagnosis lupus", contact_id=CONTACT, session_id="later-session"))
    result = fx.ledger.erase_sources(contact_id=CONTACT, turn_ids=["t-1"])
    summary_id = f"mind:episode:s-9:{fx.now.date().isoformat()}:episode_summary"
    assert summary_id in result["affected_source_ids"]
    assert episode_rows(fx) == []
    assert not any("lupus" in hit["content"] for hit in fx.ledger.search_sources(
        "diagnosis lupus", contact_id=CONTACT, session_id="later-session"))
    # Three turns remain: summarised again, from them alone.
    assert (await fx.mind.consolidate())["counts"]["episodes"] == 1
    row, = episode_rows(fx)
    assert "dentist" in row["messages_json"] and "lupus" not in row["messages_json"]


async def test_the_agents_record_of_a_contradiction_question_never_quotes_the_statements(fx):
    """The question to the owner carries both values; the agent's own recallable record of it does not, so
    erasing a person's statement leaves no copy of it under the owner (the ledger follows lineage only
    inside one contact's sources)."""
    fx.router.answers[TASK_DIGEST] = None
    await fx.fact("c-1", CONTACT, "sms-1", "My office is room 4.", "room 4")
    fx.shift(hours=1)
    await fx.fact("c-2", CONTACT, "sms-2", "My office is room 7.", "room 7")
    await fx.mind.consolidate()
    await fx.mind.tick(force=True)
    ask, = fx.messages("contradiction")
    assert "room 4" in ask.description and "room 7" in ask.context["text"]      # the question itself
    fx.mind.outcomes.record(ask.id, status="done", hermes_ref="message:1", summary="sent")
    fx.mind.outcomes.rate(ask.id, "useful")
    with closing(fx.ledger._connect()) as conn:
        record = [row[0] for row in conn.execute(
            "SELECT messages_json FROM turn_sources WHERE contact_id=? AND turn_id LIKE ?", (OWNER, f"mind:{ask.id}:%"))]
    assert len(record) >= 3                                          # decided, outcome, rated
    assert not any("room" in text for text in record), record
    fx.ledger.erase_sources(contact_id=CONTACT, turn_ids=["c-1", "c-2"])
    assert not fx.ledger.search_sources("office room", contact_id=OWNER, session_id="later-session")

async def test_the_night_first_extracts_the_statements_still_queued(fx):
    """A statement made just before the night is extracted before the night reads claims (the projection's
    own call, with the mind's router, not charged to the night), so its contradiction is asked tonight,
    not a day later."""
    fx.router.answers[TASK_DIGEST] = None
    await fx.fact("turn-a", OWNER, "chat-1", "My office is room 4.", "room 4")
    fx.shift(minutes=2)
    text = "My office is room 7."
    fx.ledger.record_source("turn-b", contact_id=OWNER, session_id="sms-1", occurred_at=fx.now.isoformat(),
                            messages=[{"role": "user", "content": text}, {"role": "assistant", "content": "Noted."}])
    fx.router.claims = Model({text: claim(text, "room 7")})
    fx.shift(days=1)
    tick = await fx.mind.tick(force=True)
    assert tick["consolidation"] == "done"
    assert [(row["kind"], row["type"]) for row in tick["formed"]] == [("message", "contradiction")]
    assert "source_claim_extraction" in fx.router.claim_calls
    with closing(fx.ledger._connect()) as conn:
        assert conn.execute("SELECT status FROM source_claim_jobs WHERE turn_id='turn-b'").fetchone()[0] == "complete"
    night = fx.mind.consolidation.last
    assert night.counts["claims_settled"] >= 1 and night.tokens == 100 * night.calls


async def test_the_night_extracts_nothing_where_claim_extraction_is_off(fx, monkeypatch):
    """``PROTAGINE_SOURCE_CLAIMS=off`` turns the projection off for the sidecar; the night does not run it."""
    monkeypatch.setenv("PROTAGINE_SOURCE_CLAIMS", "off")
    fx.ledger.record_source("turn-b", contact_id=OWNER, session_id="sms-1", occurred_at=fx.now.isoformat(),
                            messages=[{"role": "user", "content": "My office is room 7."},
                                      {"role": "assistant", "content": "Noted."}])
    await fx.mind.consolidate()
    assert fx.router.claim_calls == []

async def test_the_night_waits_for_an_extraction_the_projection_worker_holds(fx, monkeypatch):
    monkeypatch.setattr("protagine.mind.consolidate.CLAIM_POLL_S", 0.05)
    fx.router.answers[TASK_DIGEST] = None
    await fx.fact("turn-a", OWNER, "chat-1", "My office is room 4.", "room 4")
    fx.shift(minutes=2)
    text = "My office is room 7."
    fx.ledger.record_source("turn-b", contact_id=OWNER, session_id="sms-1", occurred_at=fx.now.isoformat(),
                            messages=[{"role": "user", "content": text}, {"role": "assistant", "content": "Noted."}])
    projection = SourceClaimProjection(fx.ledger)
    job = projection.claim_job()                                     # the worker's lease, on its own loop

    async def worker():
        await asyncio.sleep(0.3)
        message = json.loads(job["messages_json"])[0]
        claims = [dict(claim(text, "room 7"), id=None)]
        from protagine.beliefs.source_claims import validated_claims
        found = validated_claims(json.dumps([claim(text, "room 7")]), message=text, prior=[],
                                 observed_at=job["occurred_at"], timezone_name="UTC")
        projection.commit(job, message, found, model="worker", lease_token=job["lease_token"])
        projection.finish_job(job, model="worker")
    running = asyncio.create_task(worker())
    night = await fx.mind.consolidate()
    await running
    assert night["counts"]["contradictions"] == 1 and fx.router.claim_calls == []

async def test_the_whole_night_is_bounded_and_a_failing_stage_does_not_stop_the_rest(fx, monkeypatch):
    monkeypatch.setattr("protagine.mind.consolidate.RUN_DEADLINE_S", 0.2)

    class Hanging(NightRouter):
        async def complete(self, messages, *, context=None, **kwargs):
            if context["task"] == TASK_NARRATIVE:
                await asyncio.Event().wait()
            return await super().complete(messages, context=context, **kwargs)

    fx.mind.router = Hanging()
    settled_task(fx)
    night = await fx.mind.consolidate()
    assert "deadline" in night["errors"] and fx.store.get(night["id"]).status == "done"

    fx.mind.router = NightRouter()
    def boom(night, now):
        raise RuntimeError("contradictions exploded")
    fx.mind.consolidation.contradictions = boom
    for index in range(3):
        fx.turn(f"c-{index}", CONTACT, "sms-p02", f"Question {index}?", "Answer.")
    night = await fx.mind.consolidate()
    assert night["errors"] == ["contradictions: RuntimeError"] and night["counts"]["episodes"] == 1
    assert night["done"] == ["narrative", "digests", "episodes"]


# ---------------------------------------------------------------------------
# 10. The CLI
# ---------------------------------------------------------------------------

def test_cli_consolidate_and_narrative_reach_the_sidecar(tmp_path, monkeypatch, capsys):
    from protagine.config import DEFAULTS, save_config
    from protagine.mind import cli as mind_cli
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    save_config({**DEFAULTS, "owner": {"contact_id": OWNER}}, tmp_path)
    (tmp_path / "api.key").write_text("k\n")
    calls = []
    routes = {("POST", "/v1/mind/consolidate"): {"local_date": "2026-09-24", "calls": 3, "tokens": 4200,
                                                   "counts": {"digests": 2, "episodes": 1}, "errors": [], "done": []},
              ("GET", "/v1/mind/narrative"): {"enabled": True, "text": "recent: I did a thing [abc]",
                                               "sections": {}, "cites": ["abc"], "updated_at": None}}
    real_client = httpx.Client

    def client(**kwargs):
        def handler(request):
            calls.append((request.method, request.url.path, kwargs.get("timeout")))
            return httpx.Response(200, json=routes[(request.method, request.url.path)])
        return real_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout", 5))

    monkeypatch.setattr(httpx, "Client", client)
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", default=None)
    sub = parser.add_subparsers(dest="command")
    mind_cli.add_parser(sub)
    assert "consolidate" in mind_cli.COMMANDS and "narrative" in mind_cli.COMMANDS
    assert mind_cli.run(parser.parse_args(["mind", "consolidate"])) == 0
    out = capsys.readouterr().out
    assert "2026-09-24" in out and "3 call(s)" in out and "4200 tokens" in out and "digests=2" in out
    assert calls[-1][:2] == ("POST", "/v1/mind/consolidate") and calls[-1][2] >= 900
    routes[("POST", "/v1/mind/consolidate")] = {"skipped": "off", "local_date": "2026-09-24"}
    assert mind_cli.run(parser.parse_args(["mind", "consolidate"])) == 0
    assert capsys.readouterr().out.strip() == "consolidation 2026-09-24: skipped (off)"
    assert mind_cli.run(parser.parse_args(["mind", "narrative"])) == 0
    assert "I did a thing [abc]" in capsys.readouterr().out
    assert mind_cli.run(parser.parse_args(["mind", "--json", "narrative"])) == 0
    assert json.loads(capsys.readouterr().out)["cites"] == ["abc"]
    routes[("GET", "/v1/mind/narrative")] = {"enabled": False, "text": "", "sections": {}, "cites": [], "updated_at": None}
    assert mind_cli.run(parser.parse_args(["mind", "narrative"])) == 0
    assert "off" in capsys.readouterr().out


def test_consolidation_is_exported_from_the_mind_package():
    from protagine import mind
    assert mind.Consolidation is Consolidation
