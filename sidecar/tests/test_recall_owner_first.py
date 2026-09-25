"""Recall shows what the owner said; the agent's reply appears only next to the message it answers.

The second re-pilot's memory probes lost to the agent's own restatement: the owner said "on Tuesday
at 09:15 in the studio two", the agent replied "Noted: ..., Tue Sep 29 09:15, studio two.", and the
probe's recall held only that reply. Two things hid the owner's words:

* the probe's arrival stamp ("[Sat 2026-09-26 12:02:06 UTC] From what I told you earlier ...") was
  read as the date the question asks about, so the owner's admitted claim (valid on Tuesday) was
  filtered out of a question about the stamp's day, and its span was cut from the owner's quotation;
* the reply, whose input could no longer be paired with it, was shown alone.

The stamp is when the message came, never a date the question asks about; and a reply whose input
was found but cannot be shown beside it is not shown by itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from protagine.beliefs.source_projection import SourceClaimProjection
from protagine.beliefs.source_time import interpret_time_query
from protagine.memory.search import collect_sources, select_memory
from protagine.memory.selection import RecallSelector
from protagine.turns import TurnIdempotencyLedger

SAID = ("[Fri 2026-09-25 12:00:04 UTC] For the record: the quarterly review with p-72 is on Tuesday at 09:15 in "
        "the studio two.")
REPLY = "Noted: quarterly review with p-72, Tue Sep 29 09:15, studio two."
PROBE = ("[Sat 2026-09-26 12:02:06 UTC] From what I told you earlier about the quarterly review with p-72: write "
         "down the day, the time and the place as I named it.")
NOW = datetime(2026, 9, 26, 12, 2, 7, tzinfo=timezone.utc)


class _Claims:
    """The extraction admits one claim over the owner's words (its subject without "with p-72")."""

    def tier_config(self, tier):
        return SimpleNamespace(base_url="http://127.0.0.1:8080/v1")

    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        if kwargs.get("context", {}).get("task") == "source_claim_review":
            return SimpleNamespace(content=json.dumps({str(row["index"]): {"keep": True, "reason": "grounded"}
                                                       for row in payload["proposals"]}), model_id="m")
        return SimpleNamespace(model_id="m", content=json.dumps([{
            "subject": "quarterly review", "predicate": "time_and_place",
            "value": "Tuesday at 09:15 in the studio two",
            "evidence": "the quarterly review with p-72 is on Tuesday at 09:15 in the studio two",
            "operation": "assert", "prior_claim_id": None, "memory_kind": "personal_context",
            "recall_reason": "Use the stated time and place when the review comes up.",
            "valid_from_text": None, "valid_to_text": None, "event_at_text": None}]))


async def _packet(tmp_path, *, claims=True):
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    ledger.record_source("owner-1:t-1", contact_id="owner", session_id="s-1", occurred_at="2026-09-25T12:00:04+00:00",
                         messages=[{"role": "user", "content": SAID}, {"role": "assistant", "content": REPLY}],
                         derive_claims=claims)
    if claims:
        assert await SourceClaimProjection(ledger).process_one(_Claims())
    collected = await collect_sources(ledger, query=PROBE, contact_id="owner", session_id="s-2")
    return await select_memory(collected, query=PROBE, selector=RecallSelector(), timezone_name="UTC", now=NOW)


def test_an_arrival_stamp_is_not_the_date_a_question_asks_about():
    assert interpret_time_query(PROBE, now=NOW, timezone_name="UTC").mode == "current"
    assert interpret_time_query("[2026-09-26T12:02:06+0200] What did I say?", now=NOW).mode == "current"
    dated = interpret_time_query("[Sat 2026-09-26 12:02:06 UTC] Where was the kit on 2026-09-20?", now=NOW)
    assert dated.mode == "valid_range" and dated.start.startswith("2026-09-20")


@pytest.mark.asyncio
async def test_recall_carries_the_owners_words_and_never_the_reply_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_RECALL_RERANK", "off")
    packet = await _packet(tmp_path)
    content = packet.content
    assert "on Tuesday at 09:15 in the studio two" in content
    assert REPLY not in content                     # its input is there; the reply is not shown by itself
    assert all(row.get("role") != "assistant" or row.get("kind") != "source_quote" for row in packet.selected)


@pytest.mark.asyncio
async def test_a_reply_whose_input_is_a_plain_quotation_still_comes_with_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_RECALL_RERANK", "off")
    packet = await _packet(tmp_path, claims=False)
    pair, = [row for row in packet.selected if row["kind"] == "conversation_pair"]
    assert packet.content.index("on Tuesday at 09:15") < packet.content.index("Tue Sep 29 09:15")
