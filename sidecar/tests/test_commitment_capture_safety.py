"""Capture in real use: destructive actions hit the right row, races lose to the owner, contacts reach their items.

The review of the initiative branch reproduced five ways capture goes wrong once
contacts message the owner, turns race and the owner corrects things:

1. the similarity guard let ``cancel`` close a row whose wording merely overlapped;
2. an extraction validated against a pre-inference snapshot overwrote a newer change;
3. a contact's authenticated update never saw the owner's obligation to them;
4. the drain took a later job for a person while an earlier one was still in flight;
7. a moved deadline kept an obsolete absolute heads-up time.

Each test here is that reproduction, against the real extractor, store and ledger.
"""

import asyncio
import json
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from protagine.commitments.extract import (HOLD_RETRY_SECONDS, MAX_ATTEMPTS, CommitmentExtractor, build_prompt,
                                           record_items)
from protagine.commitments.store import CommitmentConflict, CommitmentStore
from protagine.mind.drives import DriveInputs, duty, heads_up_at

PERSON = "p-01"
OWNER = "owner-1"
SAM = "c-sam"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(**delta) -> str:
    return (_now() + timedelta(**delta)).isoformat()


def _item(description, *, action="create", target=None, due_at=None, priority=70, listed_due=None,
          counterpart=None, obligor=None, metadata=None):
    return {"action": action, "target": target, "description": description, "due_at": due_at,
            "priority": priority, "source_type": "cognition", "metadata": metadata,
            "listed_due": listed_due, "counterpart": counterpart, "obligor": obligor}


def _reply(*items):
    content = json.dumps(list(items))
    choice = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))
    return SimpleNamespace(raw=SimpleNamespace(choices=[choice]), content=content)


class _Router:
    supports_function_routing = True

    def __init__(self, *replies, delay=0.0, before=None):
        self.replies, self.calls, self.delay, self.before = list(replies), [], delay, before

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.calls.append((messages, context))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.before is not None:
            self.before(len(self.calls))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    def prompt(self, index=-1) -> str:
        return self.calls[index][0][1]["content"]


def _setup(tmp_path, **kwargs):
    from protagine.turns.idempotency import TurnIdempotencyLedger
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    ledger = TurnIdempotencyLedger(tmp_path / "ledger.db")
    return cstore, ledger, CommitmentExtractor(ledger, lambda: cstore, **kwargs)


def _turn(ledger, turn_id, user, assistant="Noted.", *, person=PERSON, session="s-1", channel=None):
    ledger.record_source(turn_id, contact_id=person, session_id=session, channel_id=channel, messages=[
        {"role": "user", "content": user}, {"role": "assistant", "content": assistant}])


def _job(ledger, turn_id) -> dict:
    with closing(ledger._connect()) as conn:
        return dict(conn.execute("SELECT * FROM commitment_runs WHERE turn_id=?", (turn_id,)).fetchone())


def _set_job(ledger, turn_id, **fields) -> None:
    with closing(ledger._connect()) as conn, conn:
        conn.execute(f"UPDATE commitment_runs SET {', '.join(f'{k}=?' for k in fields)} WHERE turn_id=?",
                     (*fields.values(), turn_id))


def _open(cstore, person=PERSON):
    return cstore.get_pending_for_person(person)


def _duty(cstore, now, person=PERSON):
    rows = cstore.list(status=["pending", "overdue"], person_id=person)["commitments"]
    return duty(DriveInputs(now=now, owner_id=person, commitments=rows))[1]


# --- 1. a destructive action needs the row's identity, not a resemblance -----------------------


def test_cancel_with_overlapping_wording_does_not_close_the_other_persons_item(tmp_path):
    """The review's reproduction: target 1 is Sam's recap, the wording names Kim's; the old guard
    accepted the token overlap and cancelled Sam's row. Now it is ignored and counted."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    sam = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2), priority=80)
    kim = cstore.create(person_id=PERSON, description="Send Kim the build recap", due_at=_iso(hours=3))
    existing = _open(cstore)
    assert [r["id"] for r in existing] == [sam["id"], kim["id"]]
    result = record_items([_item("Send Kim the build recap", action="cancel", target=1)],
                          person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    assert result["resolved"] == [] and result["ignored_actions"] == 1
    assert cstore.get(sam["id"])["status"] == "pending" and cstore.get(kim["id"])["status"] == "pending"


def test_an_empty_description_never_selects_a_row_for_a_destructive_action(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2))
    existing = _open(cstore)
    result = record_items([_item("", action="cancel", target=1), _item("", action="complete", target="1"),
                           _item("", action="reschedule", target=1, due_at=None),          # a hold clears a deadline
                           _item("", action="reschedule", target=1, due_at=_iso(days=3))],
                          person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    assert result["ignored_actions"] == 4 and result["updated"] == [] and result["resolved"] == []
    after = cstore.get(row["id"])
    assert after["status"] == "pending" and after["due_at"] == row["due_at"]


def test_duplicate_wording_is_told_apart_by_the_listed_deadline_or_left_alone(tmp_path):
    """Two open rows with the same words: the pointer alone is a guess. The listed due time the
    model echoes must single out the pointed row; without it, or disagreeing with it, nothing moves."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    first = cstore.create(person_id=PERSON, description="Send Sam the recap", due_at=_iso(hours=2), priority=80)
    second = cstore.create(person_id=PERSON, description="Send Sam the recap", due_at=_iso(days=2))
    existing = _open(cstore)
    assert [r["id"] for r in existing] == [first["id"], second["id"]]
    ambiguous = record_items([_item("Send Sam the recap", action="complete", target=1)],
                             person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    disagreeing = record_items([_item("Send Sam the recap", action="complete", target=1, listed_due=second["due_at"])],
                               person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    assert ambiguous["ignored_actions"] == 1 and disagreeing["ignored_actions"] == 1
    assert cstore.get(first["id"])["status"] == "pending" and cstore.get(second["id"])["status"] == "pending"
    # The echoed deadline may be spelled the way a model spells it.
    spelled = datetime.fromisoformat(second["due_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    agreed = record_items([_item("Send Sam the recap", action="complete", target=2, listed_due=spelled)],
                          person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    assert agreed["resolved"] == [second["id"]]
    assert cstore.get(second["id"])["status"] == "fulfilled" and cstore.get(first["id"])["status"] == "pending"


def test_the_listed_wording_of_a_long_description_is_its_identity(tmp_path):
    """The prompt shows the first 100 characters; the model echoes what it saw."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    long = "Send Sam the build recap with the numbers from the second run " + "and the notes " * 6
    row = cstore.create(person_id=PERSON, description=long, due_at=_iso(hours=2))
    existing = _open(cstore)
    prompt = build_prompt(user_message="u", assistant_message="a", conversation_text="", existing=existing,
                          rejections=[])
    assert f"[1] {long[:100]} (" in prompt
    result = record_items([_item(long[:100], action="complete", target=1)],
                          person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    assert result["resolved"] == [row["id"]]


# --- 2. the write is a compare-and-set against what the extractor listed ------------------------


def test_store_update_and_resolve_honour_an_expectation_inside_the_transaction(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2))
    listed = {"description": row["description"], "due_at": row["due_at"]}
    moved = cstore.update(row["id"], due_at=_iso(days=1))
    with pytest.raises(CommitmentConflict):
        cstore.update(row["id"], clear_due_at=True, expect=listed)
    with pytest.raises(CommitmentConflict):
        cstore.resolve(row["id"], outcome="obsolete", expect=listed)
    after = cstore.get(row["id"])
    assert after["due_at"] == moved["due_at"] and after["status"] == "pending"
    fresh = {"description": after["description"], "due_at": after["due_at"]}
    assert cstore.update(row["id"], clear_due_at=True, expect=fresh)["due_at"] is None
    # A row that closed since it was listed is a conflict too, never a silent no-op.
    cstore.resolve(row["id"], outcome="done")
    with pytest.raises(CommitmentConflict):
        cstore.resolve(row["id"], outcome="obsolete", expect={"description": after["description"], "due_at": None})


def test_an_older_hold_does_not_erase_a_deadline_moved_since_the_snapshot(tmp_path):
    """The review's reproduction: snapshot, move the deadline through the store, apply the older
    extraction's hold. The newer deadline used to become NULL."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2))
    snapshot = _open(cstore)
    newer = cstore.update(row["id"], due_at=_iso(days=1))["due_at"]
    result = record_items([_item("Send Sam the build recap", action="reschedule", target=1, due_at=None)],
                          person_id=PERSON, commitment_store=cstore, existing=snapshot, rejections=[])
    assert result["updated"] == [] and result["conflicts"] == 1 and result["ignored_actions"] == 0
    assert cstore.get(row["id"])["due_at"] == newer


def test_a_description_edited_since_the_snapshot_cannot_be_cancelled_by_its_old_wording(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2))
    snapshot = _open(cstore)
    cstore.update(row["id"], description="Send Sam the build recap and the invoice")
    result = record_items([_item("Send Sam the build recap", action="cancel", target=1)],
                          person_id=PERSON, commitment_store=cstore, existing=snapshot, rejections=[])
    assert result["resolved"] == [] and result["conflicts"] == 1
    assert cstore.get(row["id"])["status"] == "pending"


async def test_a_conflicting_extraction_is_rerun_once_against_the_fresh_state(tmp_path):
    """process_one: the owner moves the deadline while the model is thinking; the stale hold is
    refused, the job goes back to pending at once, and the rerun's prompt lists the new deadline.
    A second conflict does not loop: the job finishes."""
    cstore, ledger, extractor = _setup(tmp_path)
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2))
    _turn(ledger, "t-2", "Stop reminding me about the recap for now.")
    moved = []

    def owner_moves_the_deadline(call):
        moved.append(cstore.update(row["id"], due_at=_iso(days=call))["due_at"])

    hold = _item("Send Sam the build recap", action="reschedule", target=1, due_at=None)
    router = _Router(_reply(hold), before=owner_moves_the_deadline)
    assert await extractor.process_one(router) is True
    job = _job(ledger, "t-2")
    assert job["status"] == "pending" and job["error"] == "stale_snapshot" and job["next_attempt"] <= time.time() + 1
    assert cstore.get(row["id"])["due_at"] == moved[0]
    assert await extractor.process_one(router) is True
    assert f"(due {moved[0]})" in router.prompt()
    job = _job(ledger, "t-2")
    assert job["status"] == "complete" and job["disposition"] == "nothing"
    assert cstore.get(row["id"])["due_at"] == moved[1] and len(router.calls) == 2


# --- 3. the counterpart of an obligation reaches it from their own turn -------------------------


def test_a_created_item_records_its_counterpart(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    result = record_items([_item("Send Sam the build recap", due_at=_iso(hours=2), counterpart=" Sam ")],
                          person_id=OWNER, commitment_store=cstore, existing=[], rejections=[])
    row = cstore.get(result["created"][0])
    assert row["person_id"] == OWNER and row["metadata"] == {"counterpart": "Sam"}
    # Rows that predate the field carry none and match nobody.
    plain = cstore.create(person_id=OWNER, description="Water the plants")
    assert cstore.get(plain["id"])["metadata"] is None
    assert [r["id"] for r in cstore.get_open_for_counterpart(["sam"])] == [row["id"]]
    assert cstore.get_open_for_counterpart(["kim"]) == [] and cstore.get_open_for_counterpart([]) == []


async def test_a_contacts_own_turn_lists_and_moves_the_owners_obligation_to_them(tmp_path):
    """The turn is attributed to Sam's contact id, the way the attribution chokepoint writes a
    turn that arrived with sender metadata; the owner's promise names Sam. Sam's "I need it
    earlier" must see that row by number and move it, though the row is the owner's."""
    cstore, ledger, extractor = _setup(tmp_path, aliases=lambda cid: ["Sam", "+15550100"] if cid == SAM else [])
    _turn(ledger, "t-1", "I'll send Sam the build recap by five.", person=OWNER, session="owner-1")
    promise = _item("Send Sam the build recap", due_at=_iso(hours=5), counterpart="Sam")
    assert await extractor.process_one(_Router(_reply(promise))) is True
    row, = _open(cstore, OWNER)
    assert row["metadata"] == {"counterpart": "Sam"}
    _turn(ledger, "t-2", "Could I have the recap by noon instead of five?", assistant="I'll let them know.",
          person=SAM, session="sms:c-sam", channel="sms:+15550100")
    earlier = _iso(hours=1)
    router = _Router(_reply(_item("Send Sam the build recap", action="reschedule", target=1, due_at=earlier,
                                  listed_due=row["due_at"])))
    assert await extractor.process_one(router) is True
    assert f"[1] Send Sam the build recap (due {row['due_at']})" in router.prompt()
    assert _job(ledger, "t-2")["disposition"] == "recorded"
    after = cstore.get(row["id"])
    assert after["person_id"] == OWNER and after["due_at"] == earlier
    assert _open(cstore, SAM) == []                                    # nothing was created under Sam
    # Sam's confirmation closes it the same way.
    _turn(ledger, "t-3", "Got the recap, thanks, all good.", person=SAM, session="sms:c-sam", channel="sms:+15550100")
    router = _Router(_reply(_item("Send Sam the build recap", action="complete", target=1)))
    assert await extractor.process_one(router) is True
    assert cstore.get(row["id"])["status"] == "fulfilled" and _job(ledger, "t-3")["disposition"] == "recorded"


async def test_a_counterpart_named_by_contact_id_matches_without_an_alias_lookup(tmp_path):
    cstore, ledger, extractor = _setup(tmp_path)
    cstore.create(person_id=OWNER, description="Send p-07 the signed form", due_at=_iso(hours=3),
                  metadata={"counterpart": "p-07"})
    _turn(ledger, "t-2", "No need for the form any more, we cancelled the booking.", person="p-07", session="c-1")
    router = _Router(_reply(_item("Send p-07 the signed form", action="cancel", target=1)))
    assert await extractor.process_one(router) is True
    assert "[1] Send p-07 the signed form" in router.prompt()
    assert _open(cstore, OWNER) == [] and cstore.list(person_id=OWNER)["commitments"][0]["status"] == "cancelled"


async def test_a_contacts_promise_to_the_owner_is_listed_on_the_owners_turn(tmp_path, monkeypatch):
    """The other direction: Sam promises the owner something from Sam's own turn; the owner's later
    "Sam sent it" sees Sam's row. The owner is known by the configured owner id."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll get you the signed form by Friday.", assistant="Thanks, I'll pass that on.",
          person=SAM, session="sms:c-sam")
    promise = _item("Sam sends the signed form", due_at=_iso(days=2), counterpart="owner")
    assert await extractor.process_one(_Router(_reply(promise))) is True
    row, = _open(cstore, SAM)
    _turn(ledger, "t-2", "Sam's form came through this morning.", person=OWNER, session="owner-1")
    router = _Router(_reply(_item("Sam sends the signed form", action="complete", target=1)))
    assert await extractor.process_one(router) is True
    assert "[1] Sam sends the signed form" in router.prompt()
    assert cstore.get(row["id"])["status"] == "fulfilled"


async def test_contact_aliases_names_a_contact_the_way_a_conversation_does():
    """The lookup the server wires: the display name and the messaging addresses from the (async)
    contacts store; a store that is not up yet, or an unknown contact, names nobody."""
    from protagine.commitments.extract import contact_aliases

    class Store:
        async def get(self, contact_id):
            return SimpleNamespace(display_name="Sam Iqbal") if contact_id == SAM else None

        async def get_handles(self, contact_id):
            return [SimpleNamespace(address="+15550100"), SimpleNamespace(address="")] if contact_id == SAM else []

    lookup = contact_aliases(lambda: Store())
    assert await lookup(SAM) == ["Sam Iqbal", "+15550100"]
    assert await lookup("c-kim") == []
    assert await contact_aliases(lambda: None)(SAM) == []


def test_the_contract_carries_the_counterpart_and_the_listed_deadline():
    from protagine.commitments import extract
    assert "counterpart" in extract.SYSTEM and "listed_due" in extract.SYSTEM
    for name in ("counterpart", "listed_due"):
        assert extract.ITEM_SCHEMA["properties"][name]["type"] == ["string", "null"]
        assert name in extract.ITEM_SCHEMA["required"]


# --- 4. one person's capture lands in order ----------------------------------------------------


async def test_drain_waits_for_an_earlier_job_of_the_same_person_instead_of_cancelling_nothing(tmp_path):
    """The review's race: the worker holds the creation, the drain takes the cancellation. The
    cancellation used to finish "nothing" and the creation then landed an unwanted row."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _turn(ledger, "t-2", "Sam says forget the recap, the meeting is off.", session="s-2")
    _set_job(ledger, "t-1", status="running", lease_token="worker", lease_until=time.time() + 100, attempts=1)
    router = _Router(_reply(_item("Send Sam the recap", action="cancel", target=1)))

    async def worker_finishes_the_creation():
        await asyncio.sleep(0.25)
        cstore.create(person_id=PERSON, description="Send Sam the recap", due_at=_iso(hours=1))
        _set_job(ledger, "t-1", status="complete", disposition="recorded", lease_until=0)

    finisher = asyncio.create_task(worker_finishes_the_creation())
    summary = await extractor.drain(router, budget_seconds=3)
    await finisher
    assert summary["processed"] == 1 and summary["pending_left"] == 0 and summary["waited_seconds"] >= 0.2
    assert len(router.calls) == 1 and "[1] Send Sam the recap" in router.prompt()
    assert _open(cstore) == [] and _job(ledger, "t-2")["disposition"] == "recorded"


async def test_claims_are_serialized_per_person_and_free_across_people(tmp_path):
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _turn(ledger, "t-2", "Make that noon.", session="s-2")
    _turn(ledger, "t-3", "Remind me to call the vet.", person="p-02")
    first = extractor._claim(0)
    second = extractor._claim(0)
    assert (first["turn_id"], second["turn_id"]) == ("t-1", "t-3")
    assert extractor._claim(0) is None                                  # t-2 waits for t-1
    extractor._finish(first, "nothing")
    assert extractor._claim(0)["turn_id"] == "t-2"


async def test_an_earlier_job_in_backoff_still_holds_the_persons_later_jobs(tmp_path):
    """A transport failure backs t-1 off for a minute; t-2 must not overtake it. The forced drain
    takes t-1 first (ignoring the backoff), then t-2."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _turn(ledger, "t-2", "Sam says forget the recap.", session="s-2")
    _set_job(ledger, "t-1", next_attempt=time.time() + 60, hold_until=time.time() + HOLD_RETRY_SECONDS, attempts=1,
             error="ConnectionError")
    router = _Router(_reply(_item("Send Sam the recap", due_at=_iso(hours=1))),
                     _reply(_item("Send Sam the recap", action="cancel", target=1)))
    assert await extractor.process_one(router) is False                 # neither t-1 (backoff) nor t-2
    summary = await extractor.drain(router, budget_seconds=3)
    assert summary["processed"] == 2
    audited = [c[0][1]["content"].split("This turn, verbatim:\n  They said: ", 1)[1].split("\n", 1)[0]
               for c in router.calls]
    assert audited == ["I'll send Sam the recap by five.", "Sam says forget the recap."]
    assert _open(cstore) == [] and _job(ledger, "t-2")["disposition"] == "recorded"


async def test_a_job_whose_source_is_gone_neither_blocks_nor_waits(tmp_path):
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _turn(ledger, "t-2", "Make that noon.", session="s-2")
    with closing(ledger._connect()) as conn, conn:
        conn.execute("DELETE FROM turn_sources WHERE turn_id='t-1'")
    router = _Router(_reply())
    assert await extractor.process_one(router) is True
    assert _job(ledger, "t-1")["disposition"] == "unsupported_source"
    assert await extractor.process_one(router) is True and _job(ledger, "t-2")["status"] == "complete"


# --- 7. a moved deadline keeps the lead of its heads-up, not the clock time ---------------------


def test_reschedule_shifts_an_absolute_heads_up_by_the_same_delta(tmp_path):
    """The review's reproduction: a warning time already past, the deadline pushed out two days.
    The old absolute time made duty fire a heads-up at once for the distant deadline."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    due, warn = _iso(minutes=5), _iso(minutes=-5)
    row = cstore.create(person_id=PERSON, description="Send Kim the invoice", due_at=due,
                        metadata={"heads_up_at": warn})
    pushed = _iso(days=2, minutes=5)
    result = record_items([_item("Send Kim the invoice", action="reschedule", target=1, due_at=pushed)],
                          person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[])
    assert result["updated"] == [row["id"]]
    after = cstore.get(row["id"])
    assert after["metadata"]["heads_up_at"] == _iso(days=2, minutes=-5)
    assert heads_up_at(after) == datetime.fromisoformat(pushed) - timedelta(minutes=10)
    assert [c.type for c in _duty(cstore, _now())] == []
    assert [c.type for c in _duty(cstore, _now() + timedelta(days=2, minutes=-3))] == ["commitment_due_soon"]


def test_a_hold_drops_the_absolute_heads_up_and_a_relative_lead_survives(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    absolute = cstore.create(person_id=PERSON, description="Send Kim the invoice", due_at=_iso(hours=1),
                             metadata={"heads_up_at": _iso(minutes=50)})
    relative = cstore.create(person_id=PERSON, description="Call the bank", due_at=_iso(hours=2),
                             metadata={"lead_minutes": 15})
    existing = _open(cstore)
    record_items([_item("Send Kim the invoice", action="reschedule", target=1, due_at=None),
                  _item("Call the bank", action="reschedule", target=2, due_at=_iso(days=1))],
                 person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    held = cstore.get(absolute["id"])
    assert held["due_at"] is None and held["metadata"].get("heads_up_at") is None
    moved = cstore.get(relative["id"])
    assert moved["metadata"]["lead_minutes"] == 15
    assert heads_up_at(moved) == datetime.fromisoformat(_iso(days=1)) - timedelta(minutes=15)


def test_a_reschedule_that_states_a_new_warning_time_replaces_the_old_one(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Kim the invoice", due_at=_iso(hours=1),
                        metadata={"heads_up_at": _iso(minutes=50)})
    new_due, new_warn = _iso(hours=3), _iso(hours=2)
    record_items([_item("Send Kim the invoice", action="reschedule", target=1, due_at=new_due,
                        metadata={"heads_up_at": new_warn})],
                 person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[])
    after = cstore.get(row["id"])
    assert after["due_at"] == new_due and after["metadata"]["heads_up_at"] == new_warn
    assert heads_up_at(after) == datetime.fromisoformat(new_warn)
    # A lead stated in minutes replaces an absolute time, not the other way round.
    record_items([_item("Send Kim the invoice", action="reschedule", target=1, due_at=_iso(hours=4),
                        metadata={"lead_minutes": 20})],
                 person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[])
    after = cstore.get(row["id"])
    assert after["metadata"].get("heads_up_at") is None and after["metadata"]["lead_minutes"] == 20
    assert heads_up_at(after) == datetime.fromisoformat(_iso(hours=4)) - timedelta(minutes=20)


# --- the fixes under pressure: the variations the reproductions above do not cover ---------------


def test_the_other_rows_exact_wording_does_not_redirect_an_action_to_it(tmp_path):
    """Target 2 (Kim) with row 1's exact wording (Sam), and the reverse: the pointer and the words
    disagree, so nothing moves in either direction. The owner's own correction survives the
    strictness: case and punctuation are not an identity difference."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    sam = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2), priority=80)
    kim = cstore.create(person_id=PERSON, description="Send Kim the build recap", due_at=_iso(hours=3))
    existing = _open(cstore)
    result = record_items([_item("Send Sam the build recap", action="cancel", target=2),
                           _item("Send Kim the build recap", action="complete", target=1)],
                          person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    assert result["ignored_actions"] == 2 and result["resolved"] == [] and result["updated"] == []
    assert cstore.get(sam["id"])["status"] == "pending" and cstore.get(kim["id"])["status"] == "pending"
    result = record_items([_item("send sam, the build recap!", action="complete", target=1)],
                          person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    assert result["resolved"] == [sam["id"]] and cstore.get(kim["id"])["status"] == "pending"


def test_twins_without_a_deadline_and_a_pointer_past_the_listed_window_are_left_alone(tmp_path):
    """Two undated rows with one wording cannot be told apart, so neither moves; and a number the
    prompt never showed (the 13th open item) is not a pointer, whatever wording comes with it."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    first = cstore.create(person_id=PERSON, description="Call the bank")
    second = cstore.create(person_id=PERSON, description="Call the bank")
    existing = _open(cstore)
    result = record_items([_item("Call the bank", action="complete", target=1),
                           _item("Call the bank", action="cancel", target=2, listed_due=None)],
                          person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    assert result["ignored_actions"] == 2
    assert cstore.get(first["id"])["status"] == "pending" and cstore.get(second["id"])["status"] == "pending"
    for index in range(13):
        cstore.create(person_id=PERSON, description=f"Task number {index}", due_at=_iso(hours=index + 1), priority=90)
    existing = _open(cstore)
    thirteenth = existing[12]
    prompt = build_prompt(user_message="u", assistant_message="a", conversation_text="", existing=existing,
                          rejections=[])
    assert "[12] " in prompt and "[13] " not in prompt
    result = record_items([_item(thirteenth["description"], action="cancel", target=13)],
                          person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    assert result["ignored_actions"] == 1 and cstore.get(thirteenth["id"])["status"] == "pending"


async def test_an_extraction_that_loses_to_a_resolution_reruns_once_and_finishes(tmp_path):
    """The owner marks the row done while the model is thinking; the older reschedule is refused,
    the rerun lists nothing and the job finishes: the row stays fulfilled, two calls in all."""
    cstore, ledger, extractor = _setup(tmp_path)
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2))
    _turn(ledger, "t-2", "Push the recap to tomorrow.")

    def owner_marks_it_done(call):
        if call == 1:
            cstore.resolve(row["id"], outcome="done")

    router = _Router(_reply(_item("Send Sam the build recap", action="reschedule", target=1, due_at=_iso(days=1))),
                     before=owner_marks_it_done)
    assert await extractor.process_one(router) is True
    assert _job(ledger, "t-2")["error"] == "stale_snapshot"
    assert await extractor.process_one(router) is True
    assert "[1]" not in router.prompt() and len(router.calls) == 2
    job = _job(ledger, "t-2")
    assert job["status"] == "complete" and job["disposition"] == "nothing"
    after = cstore.get(row["id"])
    assert after["status"] == "fulfilled" and after["due_at"] == row["due_at"]


async def test_repeated_conflicts_and_a_transport_failure_cannot_requeue_forever(tmp_path):
    """The owner keeps moving the row and the router drops one call in between: the rerun is still
    once per job, so the job finishes within a bounded number of calls and the owner's last move
    stands."""
    cstore, ledger, extractor = _setup(tmp_path)
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2))
    _turn(ledger, "t-2", "Stop reminding me about the recap for now.")
    moves = []

    def storm(call):
        if call == 2:
            raise ConnectionError("router down")
        moves.append(cstore.update(row["id"], due_at=_iso(days=call))["due_at"])

    router = _Router(_reply(_item("Send Sam the build recap", action="reschedule", target=1, due_at=None)), before=storm)
    drains = 0
    while _job(ledger, "t-2")["status"] != "complete" and drains < 8:
        await extractor.drain(router, budget_seconds=3)
        drains += 1
    job = _job(ledger, "t-2")
    assert job["status"] == "complete" and len(router.calls) <= 4, (job, len(router.calls))
    assert cstore.get(row["id"])["due_at"] == moves[-1]


async def test_a_rerun_after_a_partial_landing_does_not_duplicate_what_landed(tmp_path):
    """One turn creates an item and cancels another; only the cancel conflicts. The create stays,
    the rerun dedupes it against its own first landing and lands the cancel against the fresh row."""
    cstore, ledger, extractor = _setup(tmp_path)
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2), priority=90)
    _turn(ledger, "t-2", "Forget the recap, and remind me to call the vet at four.")

    def owner_moves_it_once(call):
        if call == 1:
            cstore.update(row["id"], due_at=_iso(days=1))

    router = _Router(_reply(_item("Remind them to call the vet", due_at=_iso(hours=3), priority=60),
                            _item("Send Sam the build recap", action="cancel", target=1)), before=owner_moves_it_once)
    assert await extractor.process_one(router) is True
    assert _job(ledger, "t-2")["error"] == "stale_snapshot"
    assert await extractor.process_one(router) is True
    job = _job(ledger, "t-2")
    assert job["status"] == "complete" and job["disposition"] == "recorded"
    rows = cstore.list(person_id=PERSON)["commitments"]
    assert sorted((r["description"], r["status"]) for r in rows) == [
        ("Remind them to call the vet", "pending"), ("Send Sam the build recap", "cancelled")]


async def test_another_contacts_turn_neither_sees_nor_moves_the_owners_obligation_to_sam(tmp_path):
    """Kim's turn lists nothing of the owner's promise to Sam, so Kim's "drop it" cancels nothing;
    Sam's own turn lists the row that names Sam and not the one recorded before the field existed,
    and even there an action with the wrong wording is ignored."""
    cstore, ledger, extractor = _setup(tmp_path, aliases=lambda cid: {SAM: ["Sam"], "c-kim": ["Kim"]}.get(cid, []))
    named = cstore.create(person_id=OWNER, description="Send Sam the build recap", due_at=_iso(hours=5),
                          metadata={"counterpart": "Sam"})
    legacy = cstore.create(person_id=OWNER, description="Send Sam the slides", due_at=_iso(hours=6))
    _turn(ledger, "t-2", "You can drop the recap, Sam does not need it.", person="c-kim", session="sms:c-kim")
    router = _Router(_reply(_item("Send Sam the build recap", action="cancel", target=1)))
    assert await extractor.process_one(router) is True
    assert "Already-recorded OPEN items" not in router.prompt()
    assert _job(ledger, "t-2")["disposition"] == "nothing"
    _turn(ledger, "t-3", "No need for the slides after all.", person=SAM, session="sms:c-sam")
    router = _Router(_reply(_item("Send Sam the slides", action="cancel", target=1)))
    assert await extractor.process_one(router) is True
    assert "[1] Send Sam the build recap" in router.prompt() and "[2]" not in router.prompt()
    assert _job(ledger, "t-3")["disposition"] == "nothing"
    assert cstore.get(named["id"])["status"] == "pending" and cstore.get(legacy["id"])["status"] == "pending"


async def test_three_queued_jobs_with_a_backoff_in_the_middle_land_in_order(tmp_path):
    """t-1 landed, t-2 is backing off after a transport failure, t-3 is ready: the worker takes
    nothing (t-3 waits), the forced drain takes t-2 then t-3, and the cancel lands on the moved row."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _turn(ledger, "t-2", "Make that noon.", session="s-2")
    _turn(ledger, "t-3", "Sam says forget the recap.", session="s-3")
    row = cstore.create(person_id=PERSON, description="Send Sam the recap", due_at=_iso(hours=5))
    _set_job(ledger, "t-1", status="complete", disposition="recorded", attempts=1)
    _set_job(ledger, "t-2", next_attempt=time.time() + 60, hold_until=time.time() + HOLD_RETRY_SECONDS, attempts=1,
             error="ConnectionError")
    noon = _iso(hours=1)
    router = _Router(_reply(_item("Send Sam the recap", action="reschedule", target=1, due_at=noon)),
                     _reply(_item("Send Sam the recap", action="cancel", target=1)))
    assert await extractor.process_one(router) is False
    summary = await extractor.drain(router, budget_seconds=3)
    assert summary["processed"] == 2 and summary["pending_left"] == 0
    audited = [c[0][1]["content"].split("This turn, verbatim:\n  They said: ", 1)[1].split("\n", 1)[0]
               for c in router.calls]
    assert audited == ["Make that noon.", "Sam says forget the recap."]
    assert f"(due {noon})" in router.prompt()                            # t-3 saw t-2's move
    after = cstore.get(row["id"])
    assert after["status"] == "cancelled" and after["due_at"] == noon


async def test_a_head_job_that_exhausts_its_attempts_releases_the_persons_queue(tmp_path):
    """The earlier job fails for the last time: it is finished as failed, not left pending, so the
    later job of the same person runs in the same drain."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _turn(ledger, "t-2", "Remind me to call the vet at four.", session="s-2")
    _set_job(ledger, "t-1", attempts=MAX_ATTEMPTS - 1, next_attempt=time.time() + 60,
             hold_until=time.time() + HOLD_RETRY_SECONDS, error="ConnectionError")

    def down_once(call):
        if call == 1:
            raise ConnectionError("router down")

    router = _Router(_reply(_item("Remind them to call the vet", due_at=_iso(hours=4))), before=down_once)
    summary = await extractor.drain(router, budget_seconds=3)
    assert summary["processed"] == 2
    assert _job(ledger, "t-1")["disposition"] == "failed" and _job(ledger, "t-2")["disposition"] == "recorded"
    assert [r["description"] for r in _open(cstore)] == ["Remind them to call the vet"]


async def test_an_expired_lease_on_the_head_job_is_retaken_before_the_later_job(tmp_path):
    """A worker died holding t-1: its lease has run out, so the drain takes t-1 first and t-2 after,
    and the cancel finds the row the creation made."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _turn(ledger, "t-2", "Sam says forget the recap.", session="s-2")
    _set_job(ledger, "t-1", status="running", lease_token="dead-worker", lease_until=time.time() - 1, attempts=1)
    router = _Router(_reply(_item("Send Sam the recap", due_at=_iso(hours=5))),
                     _reply(_item("Send Sam the recap", action="cancel", target=1)))
    summary = await extractor.drain(router, budget_seconds=3)
    assert summary["processed"] == 2
    audited = [c[0][1]["content"].split("This turn, verbatim:\n  They said: ", 1)[1].split("\n", 1)[0]
               for c in router.calls]
    assert audited == ["I'll send Sam the recap by five.", "Sam says forget the recap."]
    assert _open(cstore) == [] and cstore.list(person_id=PERSON)["commitments"][0]["status"] == "cancelled"


async def test_one_persons_backoff_does_not_hold_another_persons_job(tmp_path):
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _turn(ledger, "t-2", "Make that noon.", session="s-2")
    _turn(ledger, "t-3", "Remind me to call the vet at four.", person="p-02")
    _set_job(ledger, "t-1", next_attempt=time.time() + 60, hold_until=time.time() + HOLD_RETRY_SECONDS, attempts=1,
             error="ConnectionError")
    router = _Router(_reply(_item("Remind them to call the vet", due_at=_iso(hours=4))))
    assert await extractor.process_one(router) is True
    assert _job(ledger, "t-3")["disposition"] == "recorded" and _job(ledger, "t-2")["status"] == "pending"
    assert await extractor.process_one(router) is False                 # p-01 still waits for t-1


async def test_a_backing_off_head_job_holds_the_persons_later_jobs_for_seconds_not_its_full_backoff(tmp_path):
    """The stall: t-1 hit a transport failure and backs off a minute (two on the next failure) while
    t-2 is ready. The worker honours the backoff and t-2 may not overtake t-1, so without the mind's
    drain (mind off, or a model slower than the tick's budget) every later capture of this person
    waited out t-1's whole backoff. A head job that is holding later jobs is retried
    HOLD_RETRY_SECONDS after its failure instead, uncharged, so the queue moves in seconds."""
    now = [time.time()]
    cstore, ledger, extractor = _setup(tmp_path, clock=lambda: now[0])
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _turn(ledger, "t-2", "Remind me to call the vet at four.", session="s-2")

    def down_once(call):
        if call == 1:
            raise ConnectionError("router down")

    router = _Router(_reply(_item("Send Sam the recap", due_at=_iso(hours=5))),
                     _reply(_item("Remind them to call the vet", due_at=_iso(hours=4))), before=down_once)
    start = now[0]
    assert await extractor.process_one(router) is True                  # t-1: a transport failure
    job = _job(ledger, "t-1")
    assert job["status"] == "pending" and job["attempts"] == 1 and job["next_attempt"] >= start + 60
    now[0] = start + 5
    assert await extractor.process_one(router) is False                 # t-2 still waits behind t-1
    now[0] = start + HOLD_RETRY_SECONDS
    assert await extractor.process_one(router) is True                  # t-1 again, early and in order
    assert _job(ledger, "t-1")["disposition"] == "recorded" and _job(ledger, "t-1")["attempts"] == 1
    assert await extractor.process_one(router) is True and _job(ledger, "t-2")["disposition"] == "recorded"
    audited = [c[0][1]["content"].split("This turn, verbatim:\n  They said: ", 1)[1].split("\n", 1)[0]
               for c in router.calls]
    assert audited == ["I'll send Sam the recap by five."] * 2 + ["Remind me to call the vet at four."]
    assert sorted(r["description"] for r in _open(cstore)) == ["Remind them to call the vet", "Send Sam the recap"]


async def test_early_retries_are_uncharged_and_a_job_with_nothing_behind_it_keeps_its_backoff(tmp_path):
    """A dead endpoint: the held head is retried every hold, none of those attempts counts against its
    budget, t-2 never overtakes it, and the scheduled attempts still exhaust it and release the queue.
    With nothing queued behind it, a backing-off job is left alone until its retry time."""
    now = [time.time()]
    start = now[0]
    cstore, ledger, extractor = _setup(tmp_path, clock=lambda: now[0])
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")

    def down(call):
        raise ConnectionError("router down")

    router = _Router(_reply(), before=down)
    assert await extractor.process_one(router) is True                  # attempt 1 fails: backoff 60 s
    now[0] = start + HOLD_RETRY_SECONDS + 1
    assert await extractor.process_one(router) is False                 # nothing waits: the backoff stands
    _turn(ledger, "t-2", "Remind me to call the vet at four.", session="s-2")
    for cycle in range(3):
        assert await extractor.process_one(router) is True              # an early, uncharged retry
        assert (_job(ledger, "t-1")["status"], _job(ledger, "t-1")["attempts"]) == ("pending", 1)
        assert await extractor.process_one(router) is False             # held again; t-2 does not overtake
        now[0] = start + HOLD_RETRY_SECONDS + 1 + (cycle + 1) * HOLD_RETRY_SECONDS
    now[0] = start + 61
    assert await extractor.process_one(router) is True                  # the scheduled attempt 2
    assert (_job(ledger, "t-1")["status"], _job(ledger, "t-1")["attempts"]) == ("pending", 2)
    now[0] = start + 61 + 121
    assert await extractor.process_one(router) is True                  # attempt 3, the last
    assert _job(ledger, "t-1")["disposition"] == "failed"
    assert await extractor.process_one(router) is True                  # the person's queue is released
    assert _job(ledger, "t-2")["attempts"] == 1


def test_a_null_heads_up_in_an_updates_metadata_is_not_a_removal(tmp_path):
    """A model that fills the metadata object with nulls on a reschedule states nothing: the
    recorded heads-up keeps its lead and moves with the deadline, as it does for null or {}."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    due = _iso(hours=1)
    row = cstore.create(person_id=PERSON, description="Send Kim the invoice", due_at=due,
                        metadata={"heads_up_at": _iso(minutes=50)})
    for stated in ({"heads_up_at": None}, {"heads_up_at": None, "lead_minutes": None}, {}):
        moved = _iso(days=1, hours=1)
        record_items([_item("Send Kim the invoice", action="reschedule", target=1, due_at=moved, metadata=stated)],
                     person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[])
        after = cstore.get(row["id"])
        assert after["due_at"] == moved
        assert heads_up_at(after) == datetime.fromisoformat(moved) - timedelta(minutes=10), stated
        cstore.update(row["id"], due_at=due, metadata={"heads_up_at": _iso(minutes=50)})


def test_a_deadline_pulled_in_past_its_warning_time_keeps_the_heads_up_and_fires_it_now(tmp_path):
    """The lead is kept even when the shifted warning time is already behind us: the heads-up
    fires at once for the imminent deadline rather than being lost."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Kim the invoice", due_at=_iso(hours=2),
                        metadata={"heads_up_at": _iso(hours=1, minutes=50)})
    soon = _iso(minutes=5)
    record_items([_item("Send Kim the invoice", action="reschedule", target=1, due_at=soon)],
                 person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[])
    after = cstore.get(row["id"])
    assert after["metadata"]["heads_up_at"] == _iso(minutes=-5)
    assert [c.type for c in _duty(cstore, _now())] == ["commitment_due_soon"]


# --- 5. who owes the work travels with the item, for the mind to read -----------------------------


def test_a_created_item_records_who_owes_it_and_updates_leave_it_alone(tmp_path):
    """The mind forms a task for the assistant's own promise and a reminder for the owner's; the
    extractor records ``metadata.obligor`` next to the counterpart, and a reschedule or a
    heads-up patch never drops it. Nothing stated means nothing recorded (the mind reads that as
    the owner's own)."""
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    result = record_items([_item("Send the owner the report", due_at=_iso(hours=1), counterpart="owner",
                                 obligor=" assistant "),
                           _item("Water the plants", due_at=_iso(hours=2))],
                          person_id=OWNER, commitment_store=cstore, existing=[], rejections=[])
    promised, plain = (cstore.get(ident) for ident in result["created"])
    assert promised["metadata"] == {"counterpart": "owner", "obligor": "assistant"}
    assert plain["metadata"] is None
    record_items([_item("Send the owner the report", action="reschedule", target=1, due_at=_iso(hours=3),
                        metadata={"heads_up_at": _iso(hours=2, minutes=45)})],
                 person_id=OWNER, commitment_store=cstore, existing=_open(cstore, OWNER), rejections=[])
    after = cstore.get(promised["id"])
    assert after["metadata"]["obligor"] == "assistant" and after["metadata"]["counterpart"] == "owner"
    assert after["metadata"]["heads_up_at"] == _iso(hours=2, minutes=45)


def test_the_contract_names_the_obligor():
    from protagine.commitments import extract
    assert "obligor" in extract.SYSTEM and "assistant" in extract.SYSTEM
    assert extract.ITEM_SCHEMA["properties"]["obligor"]["type"] == ["string", "null"]
    assert "obligor" in extract.ITEM_SCHEMA["required"]


def test_the_contract_sets_priority_below_50_only_for_what_the_person_calls_optional():
    """The owed/optional split affect relies on (a nice-to-have nudge may be held after
    dismissals, a hard promise never is) rides on the stored priority (build plan M6)."""
    from protagine.commitments import extract
    system = " ".join(extract.SYSTEM.split())
    assert ("priority: 70 for an ordinary promise or reminder, 80 or more when someone depends on a hard "
            "deadline, and below 50 only when the person calls the item optional, a nice-to-have or low "
            "priority.") in system
    assert system.index("priority: 70 for an ordinary promise") < system.index('Use "introspection"')
