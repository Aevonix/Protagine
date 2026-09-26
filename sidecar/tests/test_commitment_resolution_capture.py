"""Resolution from conversation: the capture contract's actions, output budget, retry and drain.

The gate showed that a later message which re-times, completes or cancels an
open item never reached the row (the extractor could only create), that a
truncated extraction waited a minute to retry, and that the mind decided over
a store whose capture was still in flight. These tests pin the three shut
with a fake router and a real store and ledger.
"""

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from protagine.commitments import extract
from protagine.commitments.extract import CommitmentExtractor, build_prompt, parse_items, record_items
from protagine.commitments.store import CommitmentStore
from protagine.mind.drives import DriveInputs, duty
from protagine.mind.outcomes import invalidation_reason
from protagine.turns.idempotency import TurnIdempotencyLedger

PERSON = "p-01"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(**delta) -> str:
    return (_now() + timedelta(**delta)).isoformat()


def _item(description, *, action="create", target=None, due_at=None, priority=70):
    return {"action": action, "target": target, "description": description, "due_at": due_at,
            "priority": priority, "source_type": "cognition", "metadata": None}


def _reply(*items, finish_reason="stop"):
    """A router response carrying a raw provider choice, as the real router does."""
    content = json.dumps(list(items))
    choice = SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))
    return SimpleNamespace(raw=SimpleNamespace(choices=[choice]), content=content)


class _Router:
    """Answers ``commitment_extract`` from a queue of canned replies; the last one repeats."""

    supports_function_routing = True

    def __init__(self, *replies, delay=0.0):
        self.replies, self.calls, self.delay = list(replies), [], delay

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.calls.append((messages, context))
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def prompt(self, index=-1) -> str:
        return self.calls[index][0][1]["content"]


def _setup(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    ledger = TurnIdempotencyLedger(tmp_path / "ledger.db")
    return cstore, ledger, CommitmentExtractor(ledger, lambda: cstore)


def _turn(ledger, turn_id, user, assistant="Noted.", *, person=PERSON, session="s-1", occurred_at=None):
    ledger.record_source(turn_id, contact_id=person, session_id=session, occurred_at=occurred_at, messages=[
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


# --- F2: output budget and retry ---------------------------------------------------------------


async def test_truncated_reply_retries_at_once_with_a_larger_output_budget(tmp_path):
    """A cut-off completion is re-queued immediately (not 60 s later), and the request asks for
    enough output for a reasoning model to think and still answer."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap within the hour, remind me if I forget.")
    router = _Router(_reply(_item("Send Sam the recap", due_at=_iso(hours=1)), finish_reason="length"),
                     _reply(_item("Send Sam the recap", due_at=_iso(hours=1))))
    assert await extractor.process_one(router) is True
    job = _job(ledger, "t-1")
    assert job["status"] == "pending" and job["attempts"] == 1 and job["error"] == "incomplete_final_answer"
    assert job["next_attempt"] <= time.time() + 1
    assert router.calls[0][1]["max_output_tokens"] >= 1200
    assert await extractor.process_one(router) is True
    assert _job(ledger, "t-1")["status"] == "complete" and _job(ledger, "t-1")["disposition"] == "recorded"
    assert [c["description"] for c in _open(cstore)] == ["Send Sam the recap"]


async def test_router_wrapped_incomplete_reply_is_also_immediate(tmp_path):
    """The real router wraps an unusable answer in its own failover error; the reason is read
    through the chain, so the job is not backed off like a transport failure."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "Remind me to call the vet at four.")
    wrapped = RuntimeError("No eligible local model completed function extraction; attempts=incomplete_final_answer")
    wrapped.__cause__ = ValueError("incomplete_final_answer")
    assert await extractor.process_one(_Router(wrapped)) is True
    job = _job(ledger, "t-1")
    assert job["status"] == "pending" and job["error"] == "incomplete_final_answer"
    assert job["next_attempt"] <= time.time() + 1


async def test_unparsable_non_empty_reply_is_an_error_not_nothing(tmp_path):
    """Prose where JSON was asked for is a failed attempt, retried at once and capped at three; it
    never finishes the job as "nothing" and silently loses the turn."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I owe Sam the recap by five.")
    router = _Router(SimpleNamespace(content="I could not find anything worth recording in this turn."))
    for attempt in (1, 2):
        assert await extractor.process_one(router) is True
        job = _job(ledger, "t-1")
        assert job["status"] == "pending" and job["attempts"] == attempt and job["error"] == "unparsable_output"
        assert job["next_attempt"] <= time.time() + 1
    assert await extractor.process_one(router) is True
    job = _job(ledger, "t-1")
    assert (job["status"], job["disposition"], job["attempts"]) == ("complete", "failed", 3)
    assert await extractor.process_one(router) is False
    assert _open(cstore) == []


async def test_transport_failures_keep_the_backoff(tmp_path):
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I owe Sam the recap by five.")
    assert await extractor.process_one(_Router(ConnectionError("refused"))) is True
    job = _job(ledger, "t-1")
    assert job["status"] == "pending" and job["error"] == "ConnectionError"
    assert job["next_attempt"] >= time.time() + 59


def test_parse_items_contract():
    assert parse_items("") == [] and parse_items("   ") == []
    assert parse_items("[]") == []
    assert parse_items('{"items": [{"description": "x"}]}') == [{"description": "x"}]
    assert parse_items("[1, 2]") == []                       # non-object elements are dropped
    for text in ("Nothing to record.", '{"items": 3}', "[{unterminated"):
        with pytest.raises(ValueError, match="unparsable_output"):
            parse_items(text)


# --- F3a: the prompt lists open items by number ------------------------------------------------


def test_build_prompt_numbers_open_items_with_their_due_times():
    existing = [{"description": "Send Sam the build recap", "due_at": "2026-06-26T17:00:00+00:00"},
                {"description": "Water the plants", "due_at": None}]
    existing += [{"description": f"Item {n}", "due_at": None} for n in range(3, 20)]
    prompt = build_prompt(user_message="make that noon", assistant_message="Noted.",
                          conversation_text="[earlier turn]\n  They said: I'll send Sam the recap by five.",
                          existing=existing, rejections=[], turn_time="2026-06-26T09:00:00+00:00")
    assert "[1] Send Sam the build recap (due 2026-06-26T17:00:00+00:00)" in prompt
    assert "[2] Water the plants (no due)" in prompt
    assert "[12] Item 12" in prompt and "[13]" not in prompt
    assert "Recent conversation" in prompt and "I'll send Sam the recap by five." in prompt
    assert prompt.index("Recent conversation") < prompt.index("This turn, verbatim")
    assert "action with its number as target" in prompt


def test_schema_carries_the_action_contract():
    item = extract.ITEM_SCHEMA
    assert item["properties"]["action"]["enum"] == ["create", "reschedule", "complete", "cancel"]
    assert item["properties"]["target"]["type"] == ["integer", "null"]
    assert {"action", "target"} <= set(item["required"])
    assert extract.RESPONSE_SCHEMA["schema"]["properties"]["items"]["items"] is item


# --- F3b: applying actions through the store ---------------------------------------------------


def _duty_candidates(cstore, now, person=PERSON):
    rows = cstore.list(status=["pending", "overdue"], person_id=person)["commitments"]
    return duty(DriveInputs(now=now, owner_id=person, commitments=rows))[1]


def test_reschedule_earlier_updates_and_normalizes_the_deadline(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(days=2))
    existing = _open(cstore)
    earlier = (_now() + timedelta(hours=1)).replace(tzinfo=None).isoformat()      # naive, as a model writes it
    result = record_items([_item("Send Sam the build recap", action="reschedule", target=1, due_at=earlier)],
                          person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[], turn_id="t-2")
    assert result["updated"] == [row["id"]] and result["created"] == [] and result["ignored_actions"] == 0
    after = cstore.get(row["id"])
    assert after["due_at"] == (_now() + timedelta(hours=1)).isoformat() and after["due_at"].endswith("+00:00")
    assert after["status"] == "pending"
    assert after["metadata"]["reschedule"] == {"from": row["due_at"], "by": "conversation", "note": "turn:t-2"}
    assert len(_open(cstore)) == 1


def test_reschedule_later_reopens_an_overdue_row_so_nothing_is_due_at_the_old_time(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=-1),
                        allow_overdue=True)
    assert row["status"] == "overdue"
    moved = _iso(days=1)                       # read once: a second boundary may fall between two reads
    later = _item("Send Sam the build recap", action="reschedule", target=1, due_at=moved)
    result = record_items([later], person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[])
    assert result["updated"] == [row["id"]]
    after = cstore.get(row["id"])
    assert after["status"] == "pending" and after["due_at"] == moved
    # What the mind's overdue flip and the duty drive would see at the old deadline: nothing.
    now = _now()
    assert [r for r in cstore.list(status=["pending"])["commitments"]
            if datetime.fromisoformat(r["due_at"]) <= now] == []
    assert _duty_candidates(cstore, now) == []
    assert cstore.get_overdue() == []


def test_complete_from_conversation_fulfils_the_row(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2))
    result = record_items([_item("Send Sam the build recap", action="complete", target=1)], person_id=PERSON,
                          commitment_store=cstore, existing=_open(cstore), rejections=[], turn_id="t-2")
    assert result["resolved"] == [row["id"]]
    after = cstore.get(row["id"])
    assert after["status"] == "fulfilled" and after["fulfilled_at"]
    assert after["metadata"]["resolution"]["by"] == "conversation"
    assert after["metadata"]["resolution"]["outcome"] == "done"
    assert after["metadata"]["resolution"]["note"] == "turn:t-2"
    assert _open(cstore) == []


def test_cancel_from_conversation_is_obsolete_and_invalidates_the_waiting_intention(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2))
    assert invalidation_reason(f"commitment:{row['id']}:resolved", commitments=cstore) is None
    result = record_items([_item("Send Sam the build recap", action="cancel", target=1)],
                          person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[])
    assert result["resolved"] == [row["id"]]
    after = cstore.get(row["id"])
    assert after["status"] == "cancelled" and after["metadata"]["resolution"]["outcome"] == "obsolete"
    assert invalidation_reason(f"commitment:{row['id']}:resolved", commitments=cstore)
    # The withdrawn item is shown to the model as closed, so the earlier turns still in the context do
    # not get it re-recorded; but it is not a lesson about a bad extraction, so a clear fresh commitment
    # to the same thing lands, where an item rejected as invalid stays blocked in code.
    rejections = cstore.recent_rejections()
    assert [(r["description"], r["outcome"]) for r in rejections] == [("Send Sam the build recap", "obsolete")]
    prompt = build_prompt(user_message="u", assistant_message="a", conversation_text="", existing=[],
                          rejections=rejections)
    assert "- Send Sam the build recap [obsolete]" in prompt and "clearly commits to it afresh" in prompt
    again = record_items([_item("Send Sam the build recap", due_at=_iso(days=1))], person_id=PERSON,
                         commitment_store=cstore, existing=_open(cstore), rejections=rejections)
    assert len(again["created"]) == 1 and again["skipped_duplicates"] == 0
    invalid = cstore.create(person_id=PERSON, description="Water the plants")
    cstore.resolve(invalid["id"], outcome="invalid", note="never promised")
    blocked = record_items([_item("Water the plants")], person_id=PERSON, commitment_store=cstore,
                           existing=_open(cstore), rejections=cstore.recent_rejections())
    assert blocked["created"] == [] and blocked["skipped_duplicates"] == 1


def test_actions_with_a_bad_target_or_wording_are_ignored(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2), priority=80)
    cstore.create(person_id=PERSON, description="Water the plants")
    existing = _open(cstore)
    assert [c["description"] for c in existing] == ["Send Sam the build recap", "Water the plants"]
    result = record_items([
        _item("Send Sam the build recap", action="complete", target=5),         # out of range
        _item("Water the plants", action="complete", target=1),                 # wording names the other item
        _item("Send Sam the build recap", action="complete", target=True),      # not a number
        _item("Send Sam the build recap", action="snooze", target=1),           # not an action
        _item("Send Sam the build recap", action="reschedule", target=1, due_at="not a time"),
    ], person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    assert result["ignored_actions"] == 5 and result["updated"] == [] and result["resolved"] == []
    assert cstore.get(row["id"])["status"] == "pending" and len(_open(cstore)) == 2
    # A number written as a string still points at the listed item; the wording must still be its own.
    moved = _iso(days=3)                       # read once: a second boundary may fall between two reads
    result = record_items([_item("Send Sam the build recap.", action="reschedule", target="1", due_at=moved)],
                          person_id=PERSON, commitment_store=cstore, existing=existing, rejections=[])
    assert result["updated"] == [row["id"]] and cstore.get(row["id"])["due_at"] == moved


def test_hold_clears_the_deadline_and_yields_no_duty_candidate(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Chase Sam for the signed form", due_at=_iso(hours=-1),
                        allow_overdue=True)
    hold = _item("Chase Sam for the signed form", action="reschedule", target=1, due_at=None)
    result = record_items([hold], person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[],
                          turn_id="t-2")
    assert result["updated"] == [row["id"]]
    after = cstore.get(row["id"])
    assert after["due_at"] is None and after["status"] == "pending"
    assert after["metadata"]["reschedule"]["from"] == row["due_at"]
    assert _duty_candidates(cstore, _now() + timedelta(days=3)) == []
    assert [r["id"] for r in _open(cstore)] == [row["id"]]        # held, not closed


def test_reinstated_hold_reschedules_the_same_row(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Chase Sam for the signed form", due_at=_iso(hours=1))
    record_items([_item("Chase Sam for the signed form", action="reschedule", target=1, due_at=None)],
                 person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[])
    assert cstore.get(row["id"])["due_at"] is None
    moved = _iso(days=1)                       # read once: a second boundary may fall between two reads
    result = record_items([_item("Chase Sam for the signed form", action="reschedule", target=1, due_at=moved)],
                          person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[])
    assert result["updated"] == [row["id"]] and result["created"] == []
    assert cstore.get(row["id"])["due_at"] == moved
    assert [r["id"] for r in _open(cstore)] == [row["id"]]
    assert len(_duty_candidates(cstore, _now() + timedelta(days=2))) == 1


def test_a_closed_target_no_longer_blocks_a_fresh_item_in_the_same_batch(tmp_path):
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(hours=2))
    result = record_items([_item("Send Sam the build recap", action="cancel", target=1),
                           _item("Send Sam the build recap", due_at=_iso(days=2))],
                          person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[])
    assert result["resolved"] == [row["id"]] and len(result["created"]) == 1
    assert cstore.get(row["id"])["status"] == "cancelled"
    assert [r["due_at"] for r in _open(cstore)] == [_iso(days=2)]


async def test_extraction_applies_an_action_end_to_end(tmp_path):
    """process_one: the open row is listed by number, the model answers a reschedule, the row moves
    and no second row appears; the job's disposition says something landed."""
    cstore, ledger, extractor = _setup(tmp_path)
    row = cstore.create(person_id=PERSON, description="Send Sam the build recap", due_at=_iso(days=2))
    _turn(ledger, "t-2", "Sam needs the recap by tomorrow noon now, not the day after.")
    moved = _iso(days=1)                       # read once: a second boundary may fall between two reads
    router = _Router(_reply(_item("Send Sam the build recap", action="reschedule", target=1, due_at=moved)))
    assert await extractor.process_one(router) is True
    assert f"[1] Send Sam the build recap (due {row['due_at']})" in router.prompt()
    assert _job(ledger, "t-2")["disposition"] == "recorded"
    assert [r["id"] for r in _open(cstore)] == [row["id"]]
    assert cstore.get(row["id"])["due_at"] == moved


# --- F3d: the previous turns travel with the audited turn -------------------------------------


async def test_previous_turns_are_rendered_as_recent_conversation(tmp_path):
    """The person's last turns within the hour, from any session, are shown oldest first; another
    person's turn and a turn older than the window are not."""
    cstore, ledger, extractor = _setup(tmp_path)
    at = _now()
    _turn(ledger, "t-old", "Two hours ago I promised the slides.", occurred_at=(at - timedelta(hours=2)).isoformat())
    _turn(ledger, "t-1", "I'll send Sam the recap by five.", occurred_at=(at - timedelta(minutes=10)).isoformat())
    _turn(ledger, "t-other", "Unrelated person, unrelated matter.", person="p-02",
          occurred_at=(at - timedelta(minutes=5)).isoformat())
    _turn(ledger, "t-1b", "Also the invoice.", occurred_at=(at - timedelta(minutes=4)).isoformat(), session="s-9")
    _turn(ledger, "t-2", "Make that noon.", occurred_at=at.isoformat(), session="s-2")
    router = _Router(_reply())
    while await extractor.process_one(router):
        pass
    by_turn = {}
    for messages, _context in router.calls:
        prompt = messages[1]["content"]
        current = prompt.split("This turn, verbatim:\n  They said: ", 1)[1].split("\n", 1)[0]
        by_turn[current] = prompt
    latest = by_turn["Make that noon."]
    assert "Recent conversation" in latest
    assert "I'll send Sam the recap by five." in latest and "Also the invoice." in latest
    assert latest.index("I'll send Sam the recap by five.") < latest.index("Also the invoice.")
    assert "Two hours ago" not in latest and "Unrelated person" not in latest
    assert latest.index("Recent conversation") < latest.index("This turn, verbatim")
    assert "Recent conversation" not in by_turn["Two hours ago I promised the slides."]
    assert "Recent conversation" not in by_turn["Unrelated person, unrelated matter."]


# --- F8: a word asked for before the deadline ---------------------------------------------------

async def test_a_heads_up_asked_for_travels_as_metadata_on_the_one_item(tmp_path):
    """The prompt carries the rule, the schema lets the metadata through, and the stored row is what
    the duty drive reads (``drives.heads_up_at``): one item with its deadline and its warning time."""
    from protagine.mind.drives import heads_up_at
    assert "heads_up_at" in extract.SYSTEM and "lead_minutes" in extract.SYSTEM
    cstore, ledger, extractor = _setup(tmp_path)
    due, warn = _iso(minutes=40), _iso(minutes=30)
    _turn(ledger, "t-h", "The invoice has to reach Kim in forty minutes, give me a heads-up ten minutes before.")
    router = _Router(_reply({**_item("Send Kim the invoice", due_at=due), "metadata": {"heads_up_at": warn}}))
    assert await extractor.process_one(router) is True
    row, = _open(cstore)
    assert row["due_at"] == due and row["metadata"] == {"heads_up_at": warn, "source_turn": "t-h"}
    assert heads_up_at(row) == datetime.fromisoformat(warn)
    # The other spelling the drive reads: minutes before the deadline.
    other = record_items([{**_item("Call the bank", due_at=due), "metadata": {"lead_minutes": 15}}],
                         person_id=PERSON, commitment_store=cstore, existing=_open(cstore), rejections=[])
    assert heads_up_at(cstore.get(other["created"][0])) == datetime.fromisoformat(due) - timedelta(minutes=15)


# --- pending_counts and drain: what the mind waits for ----------------------------------------


async def test_pending_counts_and_drain_land_every_claimable_job(tmp_path):
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _turn(ledger, "t-2", "And remind me to call the vet at four.", session="s-2")
    assert extractor.pending_counts() == {"pending": 2, "running": 0}
    router = _Router(_reply(_item("Send Sam the recap", due_at=_iso(hours=1))),
                     _reply(_item("Call the vet", due_at=_iso(hours=2))), delay=0.05)
    summary = await extractor.drain(router, budget_seconds=5)
    assert summary["recorded"] == 2 and summary["processed"] == 2 and summary["items"] == 2
    assert summary["pending_left"] == 0 and 0 < summary["waited_seconds"] < 2
    assert extractor.pending_counts() == {"pending": 0, "running": 0, "complete": 2}
    assert sorted(c["description"] for c in _open(cstore)) == ["Call the vet", "Send Sam the recap"]
    # Nothing owed: the drain returns at once.
    idle = await extractor.drain(router, budget_seconds=5)
    assert idle["processed"] == 0 and idle["waited_seconds"] < 0.05


async def test_drain_returns_at_its_budget_and_hands_the_job_back(tmp_path):
    """A router that never answers cannot hold the tick: the drain stops at its budget and the
    cancelled attempt goes back to pending, uncharged, for the worker or the next drain."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")

    class Never(_Router):
        async def complete(self, messages, *, context=None, **_):
            self.calls.append((messages, context))
            await asyncio.sleep(60)

    started = time.monotonic()
    summary = await extractor.drain(Never(None), budget_seconds=0.3)
    assert 0.3 <= time.monotonic() - started < 1.5
    assert summary["recorded"] == 0 and summary["processed"] == 0 and summary["pending_left"] == 1
    job = _job(ledger, "t-1")
    assert (job["status"], job["attempts"], job["lease_until"]) == ("pending", 0, 0)
    assert _open(cstore) == []
    # The job is still there for a later pass.
    assert await extractor.process_one(_Router(_reply(_item("Send Sam the recap", due_at=_iso(hours=1))))) is True
    assert len(_open(cstore)) == 1


async def test_forced_drain_claims_a_backed_off_job(tmp_path):
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _set_job(ledger, "t-1", next_attempt=time.time() + 60, attempts=1, error="ConnectionError")
    router = _Router(_reply(_item("Send Sam the recap", due_at=_iso(hours=1))))
    assert await extractor.process_one(router) is False                    # the worker respects the backoff
    assert (await extractor.drain(router, budget_seconds=1, ignore_backoff=False))["processed"] == 0
    summary = await extractor.drain(router, budget_seconds=1)
    assert summary["processed"] == 1 and summary["recorded"] == 1 and summary["pending_left"] == 0
    assert len(_open(cstore)) == 1


async def test_drain_waits_for_a_job_leased_elsewhere(tmp_path):
    """A row the projection worker holds is not taken over; the drain polls until the lease clears,
    or gives up at its budget."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    _set_job(ledger, "t-1", status="running", lease_token="worker", lease_until=time.time() + 100, attempts=1)
    router = _Router(_reply(_item("Send Sam the recap", due_at=_iso(hours=1))))

    async def worker_finishes():
        await asyncio.sleep(0.25)
        cstore.create(person_id=PERSON, description="Send Sam the recap", due_at=_iso(hours=1))
        _set_job(ledger, "t-1", status="complete", disposition="recorded", lease_until=0)

    finisher = asyncio.create_task(worker_finishes())
    summary = await extractor.drain(router, budget_seconds=3)
    await finisher
    assert summary["processed"] == 0 and summary["pending_left"] == 0
    assert 0.2 <= summary["waited_seconds"] < 1.5 and router.calls == []
    # Leased and never finished: back at the budget, the row untouched.
    _turn(ledger, "t-2", "Remind me to call the vet.", session="s-2")
    _set_job(ledger, "t-2", status="running", lease_token="worker", lease_until=time.time() + 100, attempts=1)
    summary = await extractor.drain(router, budget_seconds=0.3)
    assert summary["pending_left"] == 1 and summary["waited_seconds"] >= 0.3 and router.calls == []
    assert _job(ledger, "t-2")["status"] == "running"


async def test_drain_does_not_retake_a_job_it_already_failed_in_this_drain(tmp_path):
    """Ignoring the backoff must not let one dead endpoint burn all three attempts in one tick."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    router = _Router(ConnectionError("refused"))
    summary = await extractor.drain(router, budget_seconds=2)
    assert summary["processed"] == 1 and len(router.calls) == 1 and summary["waited_seconds"] < 1
    job = _job(ledger, "t-1")
    assert job["status"] == "pending" and job["attempts"] == 1 and summary["pending_left"] == 1


async def test_back_to_back_drains_do_not_spend_a_backed_off_jobs_attempts(tmp_path):
    """Forced ticks come in bursts, and the router refuses a failed endpoint for its cooldown. A drain
    may try a backed-off job before its retry time, but that try is uncharged like any early retry:
    only the scheduled attempts spend MAX_ATTEMPTS, so a blip across three ticks never fails a capture."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    down = _Router(ConnectionError("refused"))
    for _ in range(3):
        await extractor.drain(down, budget_seconds=2)
    job = _job(ledger, "t-1")
    assert (job["status"], job["attempts"]) == ("pending", 1) and job["next_attempt"] >= time.time() + 50
    summary = await extractor.drain(_Router(_reply(_item("Send Sam the recap", due_at=_iso(hours=1)))),
                                    budget_seconds=2)
    assert summary["recorded"] == 1 and len(_open(cstore)) == 1


async def test_a_drain_retries_an_unusable_answer_within_its_budget(tmp_path):
    """An unusable answer is retried at once (charged, capped at three) inside the same drain, so the
    tick decides over the landed row rather than over a store one retry short."""
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    router = _Router(SimpleNamespace(content="Nothing worth recording, I think."),
                     _reply(_item("Send Sam the recap", due_at=_iso(hours=1))))
    summary = await extractor.drain(router, budget_seconds=2)
    assert summary["recorded"] == 1 and len(router.calls) == 2 and _job(ledger, "t-1")["attempts"] == 2


async def test_the_prompt_carries_the_speaker_and_the_turns_local_time(tmp_path, monkeypatch):
    """Who spoke decides obligor and counterpart, and the zone of the turn decides what "3pm" is: the
    job keeps the zone the turn was recorded with, and the prompt shows both."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", PERSON)
    cstore = CommitmentStore(db_path=tmp_path / "c.db")
    ledger = TurnIdempotencyLedger(tmp_path / "ledger.db")
    extractor = CommitmentExtractor(ledger, lambda: cstore, aliases=lambda contact: ["Sam Iqbal"])
    for turn_id, person in (("t-1", "p-07"), ("t-2", PERSON)):
        ledger.record_source(turn_id, contact_id=person, session_id=f"s-{person}", messages=[
            {"role": "user", "content": "I'll have the signed form to you by 3pm."},
            {"role": "assistant", "content": "Thanks."}],
            occurred_at="2026-09-24T09:28:41+00:00", timezone_name="America/New_York")
    router = _Router(_reply())
    assert await extractor.process_one(router) and await extractor.process_one(router)
    contact, owner = router.prompt(0), router.prompt(1)
    assert "Speaker: contact p-07 (Sam Iqbal), not the owner" in contact
    assert "Speaker: the owner" in owner and "p-07" not in owner
    for prompt in (contact, owner):
        assert "Turn time: 2026-09-24T09:28:41+00:00 (local: Thu 2026-09-24 05:28 EDT, America/New_York)" in prompt


def test_the_examples_resolve_every_clock_time_in_one_zone():
    """The few-shot examples state one zone and keep to it: 9am, 4pm, half three, noon and five all
    map with the same offset, so the model never learns two conventions for "3pm"."""
    system = extract.SYSTEM
    assert "UTC-4" in system
    for due in ("T13:00:00", "T20:00:00", "T19:30:00", "T16:00:00", "T21:00:00"):
        assert due in system, due
    assert "T12:00:00" not in system and "T17:00:00" not in system


async def test_drain_without_a_usable_router_only_polls_leased_rows(tmp_path):
    cstore, ledger, extractor = _setup(tmp_path)
    _turn(ledger, "t-1", "I'll send Sam the recap by five.")
    summary = await extractor.drain(None, budget_seconds=1)
    assert summary["processed"] == 0 and summary["pending_left"] == 1 and summary["waited_seconds"] < 0.05
    assert _job(ledger, "t-1")["status"] == "pending"


async def test_worker_loop_and_drain_share_the_ledger_without_double_processing(tmp_path):
    """The projection worker on its own thread and loop, and a drain on this loop, against one
    ledger: every job is processed exactly once and every item lands. Six people, so both sides can
    hold a job at once (one person's jobs land in order, and the faster worker would starve the drain)."""
    cstore, ledger, extractor = _setup(tmp_path)
    for n in range(6):
        _turn(ledger, f"t-{n}", f"Remind me about errand {n} in an hour.", session=f"s-{n}", person=f"p-{n}")
    router = _Router(*[_reply(_item(f"Errand {n}", due_at=_iso(hours=1))) for n in range(6)], delay=0.1)
    stop = threading.Event()

    def worker_thread():
        async def loop():
            while not stop.is_set():
                if not await extractor.process_one(router):
                    await asyncio.sleep(0.02)
        asyncio.run(loop())

    thread = threading.Thread(target=worker_thread, daemon=True)
    thread.start()
    try:
        summary = await extractor.drain(router, budget_seconds=10)
    finally:
        stop.set()
        thread.join(5)
    assert summary["pending_left"] == 0
    assert len(router.calls) == 6
    assert extractor.pending_counts() == {"pending": 0, "running": 0, "complete": 6}
    with closing(ledger._connect()) as conn:
        assert [row[0] for row in conn.execute("SELECT attempts FROM commitment_runs")] == [1] * 6
    assert cstore.list(status=["pending"])["total"] == 6
    assert 0 < summary["processed"] <= 6
