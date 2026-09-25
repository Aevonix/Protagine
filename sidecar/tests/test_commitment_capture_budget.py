"""Capture has room to finish.

The extractor's system prompt grew from 8,200 to 13,667 characters with the same 1,500-token output
budget, and the reasoning model spends its budget thinking before it writes the JSON: in the second
re-pilot 16 of 60 extractions ended at the length cap (median completion 806 tokens, reasoning up to
6,033 characters), and two owner turns lost their capture after three empty attempts. The prompt now
keeps every rule in fewer characters, and the output budget fits a long turn's answer after the
thinking it takes.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from protagine.commitments import extract
from protagine.commitments.extract import OUTPUT_BUDGET_TOKENS, SYSTEM, CommitmentExtractor, record_items
from protagine.commitments.store import CommitmentStore

OWNER = "owner-5"
# The measured pilots: the longest reasoning an extraction spent (characters) and a conservative
# characters-per-token for that prose and for the JSON answer.
MAX_REASONING_CHARS = 6033
REASONING_CHARS_PER_ITEM = 400
PROSE_CHARS_PER_TOKEN = 3.5
JSON_CHARS_PER_TOKEN = 3.0


def test_the_prompt_is_measured_and_stays_short():
    """Every rule, in fewer characters than the prompt that truncated (13,667)."""
    assert len(SYSTEM) <= 13000


def _long_turn(count: int):
    """A long owner turn holding ``count`` dated obligations, and the answer a correct model gives."""
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    things = ["the venue deposit", "the catering order", "the guest list", "the stage plan", "the parking permits",
              "the badge proofs", "the insurance form", "the speaker bios", "the floor plan", "the rental return"]
    people = [f"p-{index + 20}" for index in range(count)]
    sentences, items = [], []
    for index in range(count):
        due = start + timedelta(hours=index + 1)
        sentences.append(f"I promised {people[index]} {things[index]} by {due.strftime('%H:%M')} UTC on "
                         f"{due.strftime('%A')}, and there is a fair amount of back story to it: " + "we went back "
                         "and forth on the details for most of the week, the numbers changed twice, and the last "
                         "version is the one in the shared folder. " * 4)
        items.append({"action": "create", "target": None, "description": f"Send {people[index]} {things[index]}",
                      "due_at": due.isoformat(), "priority": 80, "source_type": "cognition", "metadata": None,
                      "listed_due": None, "counterpart": people[index], "obligor": "owner"})
    said = " ".join(sentences)[:6000]
    return said, items


class _ReasoningModel:
    """A reasoning model that thinks before it answers: the thinking the pilots measured, a little more per
    item, then the JSON. When both do not fit the output budget it stops at the cap (finish_reason length)."""

    supports_function_routing = True

    def __init__(self, items):
        self.items, self.budgets = items, []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        budget = int((context or {}).get("max_output_tokens") or 0)
        self.budgets.append(budget)
        answer = json.dumps({"items": self.items})
        thinking = math.ceil((MAX_REASONING_CHARS + REASONING_CHARS_PER_ITEM * len(self.items)) / PROSE_CHARS_PER_TOKEN)
        needed = thinking + math.ceil(len(answer) / JSON_CHARS_PER_TOKEN)
        if needed > budget:
            cut = answer[: max(0, int((budget - thinking) * JSON_CHARS_PER_TOKEN))]
            choice = SimpleNamespace(finish_reason="length", message=SimpleNamespace(content=cut))
        else:
            choice = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=answer))
        return SimpleNamespace(raw=SimpleNamespace(choices=[choice]), content=choice.message.content)


@pytest.mark.asyncio
async def test_a_long_owner_turns_extraction_is_not_cut_off(tmp_path, monkeypatch):
    from protagine.turns.idempotency import TurnIdempotencyLedger
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    store = CommitmentStore(db_path=tmp_path / "c.db")
    ledger = TurnIdempotencyLedger(tmp_path / "ledger.db")
    extractor = CommitmentExtractor(ledger, lambda: store)
    said, items = _long_turn(8)
    assert len(said) >= 5000
    ledger.record_source("t-long", contact_id=OWNER, session_id="s-1", channel_id=None, messages=[
        {"role": "user", "content": said}, {"role": "assistant", "content": "Noted, all eight."}])
    model = _ReasoningModel(items)
    assert await extractor.process_one(model) is True
    assert model.budgets == [OUTPUT_BUDGET_TOKENS]                # one call, never cut off and retried
    rows = store.list(status=["pending", "overdue"], person_id=OWNER)["commitments"]
    assert sorted(row["description"] for row in rows) == sorted(item["description"] for item in items)


def test_the_budget_fits_the_longest_measured_thinking_and_a_long_answer():
    said, items = _long_turn(8)
    answer = json.dumps({"items": items})
    thinking = (MAX_REASONING_CHARS + REASONING_CHARS_PER_ITEM * len(items)) / PROSE_CHARS_PER_TOKEN
    assert thinking + len(answer) / JSON_CHARS_PER_TOKEN <= OUTPUT_BUDGET_TOKENS


def test_a_field_the_examples_leave_out_takes_the_default_they_state(tmp_path):
    """The examples show only fields that differ from their defaults; an answer from a binding without a
    strict schema that copies that shape is read with the same defaults, never as another item type."""
    store = CommitmentStore(db_path=tmp_path / "c.db")
    due = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    result = record_items([{"action": "create", "description": "Call the dentist", "due_at": due,
                            "metadata": {"kind": "reminder"}, "obligor": "owner"},
                           {"action": "create", "description": "Email them the Q3 revenue", "due_at": due,
                            "metadata": {"kind": "deliverable", "content": "Q3 revenue was 4.2 million."},
                            "obligor": "assistant"}],
                          person_id=OWNER, commitment_store=store, existing=[], rejections=[], owner_id=OWNER,
                          owner_text="Remind me to call the dentist. Email me the Q3 revenue number.")
    reminder, deliverable = (store.get(ident) for ident in result["created"])
    assert reminder["source_type"] == "cognition" and reminder["priority"] == 70
    assert deliverable["source_type"] == "introspection"
    assert "Every element has every field" in " ".join(extract.SYSTEM.split())
