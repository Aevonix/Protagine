"""A recurring check-in the owner sets for a contact, and the matter it is about (audit B3).

"p-02 asked for a check-in every 10 minutes while the budget draft is in progress": capture
records the owner's cadence as an undated row ``{kind: cadence, recipient, topic,
cadence_minutes}``, only from the owner's own turn and never with a grant. The tick resolves the
recipient, sets the contact's cadence once, and the check-ins the social drive forms carry that
topic, so the message names the matter. Permission stays the contact's ``may_contact``: an
``ask`` contact still gets an owner ask and nothing is sent to them.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from protagine.commitments import extract
from protagine.commitments.extract import CommitmentExtractor, record_items
from protagine.commitments.store import CommitmentStore
from protagine.mind.drives import DriveInputs, duty
from test_mind_social import CADENCE, CONTACT, OTHER, OWNER, PAST, T0, C, contact, make  # noqa: F401

ITEM = "budget draft"
CADENCE_ITEM = {"action": "create", "target": None, "priority": 60, "due_at": None, "source_type": "cognition",
                "description": f"Check in with {CONTACT} every {CADENCE} minutes about the {ITEM}",
                "listed_due": None, "counterpart": CONTACT, "obligor": "assistant",
                "metadata": {"kind": "cadence", "recipient": CONTACT, "topic": f"the {ITEM}",
                             "cadence_minutes": CADENCE}}
PEOPLE = Path(__file__).resolve().parents[2] / "benchmarks" / "paired" / "generators" / "people.py"


def cadence_turns():
    """The family's three owner phrasings of a cadence (``people._cadence_turn``)."""
    spec = importlib.util.spec_from_file_location("people_templates_cadence", PEOPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    turns = []
    for index in range(3):
        draw = SimpleNamespace(pick=lambda options, index=index: options[index % len(options)])
        turns.append(module._cadence_turn(draw, CONTACT, ITEM, CADENCE)["user"])
    return turns


class CadenceRouter:
    """Plays the extraction model (the cadence item for a cadence turn) and a composer that
    writes around the topic it was given."""

    supports_function_routing = True

    def __init__(self, item=CADENCE_ITEM):
        self.item, self.calls = item, []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        task = (context or {}).get("task")
        self.calls.append(task)
        if task == "commitment_extract":
            turn = messages[1]["content"].split("This turn, verbatim:", 1)[-1]
            return SimpleNamespace(content=json.dumps([self.item] if f"every {CADENCE} minutes" in turn else []))
        if task == "mind_compose":
            topic = next(line.partition(":")[2].strip() for line in messages[1]["content"].splitlines()
                         if line.startswith("Topic:"))
            return SimpleNamespace(content=f"Hello, how is {topic} coming along?", usage={"total_tokens": 12})
        raise AssertionError(f"unexpected task {task}")


def cadence_log(fx):
    return [entry for entry in fx.contacts.cadences if entry[0] == CONTACT]


def owner_turn(fx, turn_id, text):
    fx.ledger.record_source(turn_id, contact_id=OWNER, session_id="owner-1", occurred_at=fx.now.isoformat(),
                            messages=[{"role": "user", "content": text}, {"role": "assistant", "content": "Noted."}])


# ---------------------------------------------------------------------------
# capture: the contract and the stored row
# ---------------------------------------------------------------------------

def test_the_extractor_contract_has_a_cadence_case_that_never_grants():
    assert "RECURRING CHECK-IN THE OWNER SETS FOR A CONTACT" in extract.SYSTEM
    assert '"kind":"cadence"' in extract.SYSTEM and '"cadence_minutes"' in extract.SYSTEM
    assert "never grants" in extract.SYSTEM and extract.CADENCE_KIND == "cadence"


def test_the_owners_cadence_is_an_undated_row_with_a_clean_topic_and_no_grant(tmp_path):
    store = CommitmentStore(tmp_path / "protagine-commitments.db")
    noisy = {**CADENCE_ITEM, "metadata": {**CADENCE_ITEM["metadata"], "grant": "owner", "cadence_minutes": "10",
                                          "topic": "the budget draft 2027 figures and more words here"}}
    result = record_items([noisy], person_id=OWNER, commitment_store=store, existing=[], rejections=[],
                          turn_id="turn-1", owner_id=OWNER)
    row = store.get(result["created"][0])
    assert row["due_at"] is None and row["status"] == "pending"
    assert row["metadata"]["kind"] == "cadence" and row["metadata"]["recipient"] == CONTACT
    assert row["metadata"]["cadence_minutes"] == CADENCE and "grant" not in row["metadata"]
    assert row["metadata"]["topic"] == "the budget draft figures and more"   # no digits, six words
    assert row["metadata"]["obligor"] == "assistant"
    # The rhythm never falls due: once the tick resolved the contact, duty leaves the row alone.
    resolved = {**row, "metadata": {**row["metadata"], "recipient_id": CONTACT, "recipient_exact": True}}
    assert duty(DriveInputs(now=T0 + 100 * C, owner_id=OWNER, commitments=[resolved]))[1] == []


def test_a_cadence_from_anyone_but_the_owner_or_without_minutes_is_not_recorded(tmp_path):
    store = CommitmentStore(tmp_path / "protagine-commitments.db")
    result = record_items([CADENCE_ITEM], person_id=OTHER, commitment_store=store, existing=[], rejections=[],
                          turn_id="turn-2", owner_id=OWNER)
    assert result["created"] == [] and store.get_pending_for_person(OTHER) == []
    for minutes in (None, 0, "often", True):
        broken = {**CADENCE_ITEM, "metadata": {**CADENCE_ITEM["metadata"], "cadence_minutes": minutes}}
        assert record_items([broken], person_id=OWNER, commitment_store=store, existing=[], rejections=[],
                            owner_id=OWNER)["created"] == []
    assert store.get_pending_for_person(OWNER) == []


# ---------------------------------------------------------------------------
# the tick: the cadence set once, the matter on every check-in
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrasing", range(3))
async def test_each_family_phrasing_sets_the_cadence_and_the_check_in_names_the_matter(make, phrasing):
    router = CadenceRouter()
    fx = make([contact(CONTACT, may_contact="auto"), contact(OTHER, may_contact="auto")], router=router)
    fx.mind.capture = CommitmentExtractor(fx.ledger, lambda: fx.commitments)
    owner_turn(fx, "turn-1", cadence_turns()[phrasing])
    first = await fx.tick()
    assert first["capture_drained"]["recorded"] == 1 and first["formed"] == []
    row, = fx.commitments.get_pending_for_person(OWNER)
    assert row["metadata"]["kind"] == "cadence" and row["metadata"]["recipient_id"] == CONTACT
    assert fx.contacts.records[CONTACT]["cadence_minutes"] == CADENCE
    (who, minutes, by), = cadence_log(fx)
    assert minutes == CADENCE and by == f"owner-turn:commitment:{row['id']}"

    fx.shift(C + PAST)
    formed = [item for tick in [await fx.tick(), await fx.tick(), await fx.tick()] for item in tick["formed"]]
    assert [(item["type"], item["decision"]) for item in formed] == [("check_in", "act")]
    payload, = await fx.send_all()
    assert payload["recipient"] == CONTACT and ITEM in payload["text"] and OTHER not in payload["text"]
    # Tick 2 of the backoff: the next check-in still names the matter; the cadence was set once.
    fx.shift(2 * C + PAST)
    formed = (await fx.tick())["formed"]
    assert [item["type"] for item in formed] == ["check_in"]
    again, = await fx.send_all()
    assert ITEM in again["text"] and len(cadence_log(fx)) == 1
    assert fx.messages_to(OTHER) == []


async def test_the_owners_cadence_never_grants_permission_an_ask_contact_gets_an_owner_ask(make):
    fx = make([contact(CONTACT, may_contact="ask")], router=CadenceRouter())
    fx.mind.capture = CommitmentExtractor(fx.ledger, lambda: fx.commitments)
    owner_turn(fx, "turn-1", cadence_turns()[0])
    await fx.tick()
    assert fx.contacts.records[CONTACT]["cadence_minutes"] == CADENCE
    fx.shift(C + PAST)
    formed = [item for tick in [await fx.tick(), await fx.tick(), await fx.tick()] for item in tick["formed"]]
    assert [(item["type"], item["decision"]) for item in formed] == [("check_in", "ask")]
    asked, = fx.messages_to(CONTACT)
    assert asked.status == "asked" and ITEM in asked.context["text"]
    assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []


async def test_a_cadence_the_owner_later_changes_by_hand_is_not_set_back(make):
    fx = make([contact(CONTACT, may_contact="auto")], router=CadenceRouter())
    fx.mind.capture = CommitmentExtractor(fx.ledger, lambda: fx.commitments)
    owner_turn(fx, "turn-1", cadence_turns()[1])
    await fx.tick()
    await fx.contacts.set_cadence(CONTACT, 60, by="cli")
    await fx.tick()
    assert fx.contacts.records[CONTACT]["cadence_minutes"] == 60 and len(cadence_log(fx)) == 2


async def test_a_contacts_own_cadence_turn_and_people_off_set_nothing(make):
    fx = make([contact(CONTACT, may_contact="auto")], router=CadenceRouter())
    fx.mind.capture = CommitmentExtractor(fx.ledger, lambda: fx.commitments)
    fx.ledger.record_source("turn-1", contact_id=CONTACT, session_id="contact-1", occurred_at=fx.now.isoformat(),
                            messages=[{"role": "user", "content": f"Check in with me every {CADENCE} minutes."},
                                      {"role": "assistant", "content": "Sure."}])
    await fx.tick()
    assert fx.contacts.records[CONTACT]["cadence_minutes"] is None and cadence_log(fx) == []
    off = make([contact(CONTACT, may_contact="auto")], router=CadenceRouter(), config={"faculties": {"people": False}})
    off.mind.capture = CommitmentExtractor(off.ledger, lambda: off.commitments)
    owner_turn(off, "turn-1", cadence_turns()[2])
    await off.tick()
    assert off.contacts.records[CONTACT]["cadence_minutes"] is None and cadence_log(off) == []


async def test_a_cadence_for_someone_matched_only_by_name_waits_for_the_owners_word(make):
    """A cadence times check-ins to that person; set on a name guess it could time them to the
    wrong one, so only a recipient the owner identified exactly gets it at once (audit M4). A name
    match is the owner's question, with the person and handle it matched (review F7: it was
    dropped silently and the row stayed open for good); a yes sets it and its matter."""
    named = {**CADENCE_ITEM, "metadata": {**CADENCE_ITEM["metadata"], "recipient": "Sam"}, "counterpart": "Sam"}
    fx = make([contact(CONTACT, may_contact="auto", name="Sam")], router=CadenceRouter(named))
    fx.mind.capture = CommitmentExtractor(fx.ledger, lambda: fx.commitments)
    owner_turn(fx, "turn-1", cadence_turns()[0].replace(CONTACT, "Sam"))
    formed = (await fx.tick())["formed"]
    row, = fx.commitments.get_pending_for_person(OWNER)
    assert row["metadata"]["recipient_id"] == CONTACT and row["metadata"]["recipient_exact"] is False
    assert fx.contacts.records[CONTACT]["cadence_minutes"] is None and cadence_log(fx) == []
    question, = formed
    asked = fx.store.get(question["id"])
    assert asked.type == "cadence_confirm" and asked.status == "asked" and asked.ask_code
    assert "'Sam'" in asked.description and f"capture:{CONTACT}" in asked.description
    assert (await fx.tick())["formed"] == []                                # asked once
    await fx.mind.answer(asked.ask_code, yes=True, contact_id=OWNER)
    assert fx.store.get(asked.id).status == "done"
    assert fx.contacts.records[CONTACT]["cadence_minutes"] == CADENCE and len(cadence_log(fx)) == 1
    fx.shift(C + PAST)
    formed = [item for tick in [await fx.tick(), await fx.tick()] for item in tick["formed"]]
    assert [item["type"] for item in formed] == ["check_in"]
    payload, = [p for p in await fx.send_all() if p["recipient"] == CONTACT]
    assert ITEM in payload["text"]


async def test_a_no_to_a_name_matched_cadence_withdraws_it(make):
    named = {**CADENCE_ITEM, "metadata": {**CADENCE_ITEM["metadata"], "recipient": "Sam"}, "counterpart": "Sam"}
    fx = make([contact(CONTACT, may_contact="auto", name="Sam")], router=CadenceRouter(named))
    fx.mind.capture = CommitmentExtractor(fx.ledger, lambda: fx.commitments)
    owner_turn(fx, "turn-1", cadence_turns()[0].replace(CONTACT, "Sam"))
    question, = (await fx.tick())["formed"]
    row, = fx.commitments.get_pending_for_person(OWNER)
    await fx.mind.answer(fx.store.get(question["id"]).ask_code, yes=False, contact_id=OWNER)
    assert fx.commitments.get(row["id"])["status"] != "pending"
    assert fx.contacts.records[CONTACT]["cadence_minutes"] is None and cadence_log(fx) == []
    fx.shift(C + PAST)
    assert (await fx.tick())["formed"] == [] and fx.messages_to(CONTACT) == []


async def test_a_cadence_naming_someone_unknown_asks_the_owner_who_they_are(make):
    """Review F7: duty skipped undated rows before its unknown-recipient check, so a cadence for
    someone the store cannot resolve was never applied and the owner never heard of it."""
    fx = make([contact(CONTACT, may_contact="auto", name="Sam")])
    fx.commitments.create(person_id=OWNER, description="Check in with Kim weekly about the kitchen quote",
                          due_at=None, source_type="cognition",
                          metadata={"kind": "cadence", "recipient": "Kim", "topic": "the kitchen quote",
                                    "cadence_minutes": 10, "counterpart": "Kim", "obligor": "assistant"})
    formed, = (await fx.tick())["formed"]
    assert formed["type"] == "recipient_unknown" and formed["decision"] == "act"
    ask = fx.store.get(formed["id"])
    assert ask.entity_id == OWNER and "Kim" in ask.context["text"]
    assert (await fx.tick())["formed"] == [] and fx.contacts.cadences == []
