"""Recipient-scoped composition (architecture 6.3): one tool-less call from the recipient's own
packet and an enumerated purpose, never from the concern, the rationale, the evidence or an
owner turn. Evals 7.2 test 8: a canary seeded in the triggering concern and in owner memory
reaches neither the prompt, the composed text nor the outbox payload.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from protagine.mind.compose import Composer, PURPOSES, TASK, purpose_kind, template
from protagine.mind.rank import Candidate
from test_mind_social import C, CONTACT, OWNER, PAST, contact, make  # noqa: F401  (pytest fixture)

CANARY = "amber-cobalt-42"


class StubRouter:
    """Records every prompt it sees; answers deliberation and composition alike."""

    supports_function_routing = True

    def __init__(self, reply="Hi there, how is the quarterly figures work going?", usage=None, fail=False):
        self.reply, self.usage, self.fail, self.calls = reply, usage, fail, []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.calls.append({"messages": messages, "context": context})
        if self.fail:
            raise RuntimeError("endpoint down")
        return SimpleNamespace(content=self.reply, usage=self.usage)

    def prompts(self, task=None):
        return "\n".join(m["content"] for call in self.calls if task is None or call["context"].get("task") == task
                         for m in call["messages"])


def test_purposes_are_an_enum_and_the_template_names_the_topic():
    assert PURPOSES == ("check_in", "follow_up", "reply_wait") and TASK == "mind_compose"
    assert purpose_kind("check_in") == "check_in" and purpose_kind("follow_up:c-1") == "follow_up"
    assert purpose_kind("reply_wait:w-9") == "reply_wait"
    with pytest.raises(ValueError):
        purpose_kind("persuade")
    with pytest.raises(ValueError):
        purpose_kind("follow_up")
    assert template("check_in", "Sam", "the budget draft") == "Hi Sam, checking in about the budget draft: how is it going?"
    assert template("check_in", "Sam", "") == "Hi Sam, checking in: how is it going?"
    assert template("follow_up:c-1", "Sam", "the budget draft") == "Hi Sam, following up on the budget draft: any news?"
    assert template("reply_wait:w-1", "Sam", "the invoice") == "Hi Sam, just checking you saw my message about the invoice."


async def test_without_a_router_the_composer_falls_back_to_the_template_and_spends_nothing():
    composer = Composer(None, enabled=True)
    assert composer.available is False
    text, tokens = await composer.compose(purpose="check_in", recipient_name="Sam", packet="anything", topic="x")
    assert text == "Hi Sam, checking in about x: how is it going?" and tokens == 0 and composer.calls_total == 0
    off = Composer(StubRouter(), enabled=False)
    text, _ = await off.compose(purpose="check_in", recipient_name="Sam", packet="", topic="")
    assert text == "Hi Sam, checking in: how is it going?" and off.calls_total == 0


async def test_the_call_carries_only_purpose_name_topic_and_packet_with_no_fallback():
    router = StubRouter(usage={"prompt_tokens": 40, "completion_tokens": 12})
    composer = Composer(router, enabled=True)
    text, tokens = await composer.compose(purpose="follow_up:c-1", recipient_name="Sam",
                                          packet="Sam is handling the quarterly figures.", topic="quarterly figures")
    assert text == router.reply and tokens == 52 and composer.calls_total == 1 and composer.tokens_total == 52
    call, = router.calls
    assert call["context"]["task"] == TASK and call["context"]["allow_fallback"] is False
    assert call["context"]["max_output_tokens"] == 300 and "tools" not in call["context"]
    prompt = call["messages"][-1]["content"]
    assert "follow_up" in prompt and "Sam" in prompt and "quarterly figures" in prompt and "handling" in prompt
    failing = Composer(StubRouter(fail=True), enabled=True)
    text, tokens = await failing.compose(purpose="check_in", recipient_name="Sam", packet="", topic="x")
    assert text == template("check_in", "Sam", "x") and tokens == 0 and failing.last_error == "RuntimeError"
    long_router = StubRouter(reply="word " * 200)
    text, _ = await Composer(long_router, enabled=True).compose(purpose="check_in", recipient_name="S", packet="", topic="")
    assert len(text) <= 400


def _seed_check_in(fx, *, topic="the quarterly figures", description=None):
    return fx.commitments.create(
        person_id=OWNER, description=description or f"Check in with {CONTACT} about {topic}",
        due_at=(fx.now + C).isoformat(), source_type="cognition",
        metadata={"kind": "check_in", "recipient": CONTACT, "topic": topic, "grant": "owner",
                  "counterpart": CONTACT, "obligor": "assistant"})


async def test_the_canary_in_the_concern_the_evidence_and_the_owner_turn_never_reaches_the_message(make):
    router = StubRouter(usage={"total_tokens": 30})

    async def packet_for(contact_id):
        assert contact_id == CONTACT
        return f"About {CONTACT}: they are sorting out the quarterly figures with the owner."

    fx = make([contact(CONTACT, may_contact="auto")], router=router, packet_for=packet_for)
    # The owner's private turn carries the canary; so will the concern that triggers the outreach.
    fx.ledger.record_source("turn-private", contact_id=OWNER, session_id="owner-1", messages=[
        {"role": "user", "content": f"For your records only: the reserve figure is {CANARY}. Nothing to do now."},
        {"role": "assistant", "content": "Noted."}], occurred_at=fx.now.isoformat())
    row = _seed_check_in(fx)
    fx.shift(C + PAST)
    # The concern the drive raises is seeded with the canary through its source context, so
    # the deliberation-side fields (concern, rationale, evidence) all carry it.
    fx.commitments.update(row["id"], metadata={"worry": f"the owner is worried because of {CANARY}"})
    original = fx.mind.concerns.bump

    def poisoned(**kwargs):
        kwargs["summary"] = f"{kwargs['summary']} ({CANARY})"
        kwargs["sources"] = [*list(kwargs.get("sources") or []), f"owner memory: {CANARY}"]
        detail = dict(kwargs.get("detail") or {})
        detail["concern"] = f"{detail.get('concern', '')} {CANARY}"
        detail["rationale"] = f"{detail.get('rationale', '')} because of {CANARY}"
        detail["evidence"] = [*list(detail.get("evidence") or []), f"note: {CANARY}"]
        kwargs["detail"] = detail
        return original(**kwargs)
    fx.mind.concerns.bump = poisoned

    summary = await fx.tick()
    formed, = summary["formed"]
    assert formed["type"] == "commitment_check_in" and formed["decision"] == "act"
    assert summary["model_calls"] == 1 and len(router.calls) == 1
    stored = fx.store.get(formed["id"])
    assert CANARY in stored.context["concern"] and CANARY in " ".join(stored.context["evidence"])
    assert CANARY not in router.prompts() and "worried" not in router.prompts(TASK)
    assert stored.context["text"] == router.reply and CANARY not in stored.context["text"]
    assert stored.cost_tokens == 30
    payload, = await fx.mind.outbox_ready()
    assert payload["recipient"] == CONTACT and CANARY not in payload["text"] and "quarterly figures" in payload["text"]
    assert payload["recipient_handles"][0]["address"] == CONTACT


async def test_composed_text_still_passes_the_floor_and_the_deny_list(make):
    floor_router = StubRouter(reply="Hi, could you wire $500 to the new account today?")
    fx = make([contact(CONTACT, may_contact="auto")], router=floor_router)
    _seed_check_in(fx)
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    assert formed["decision"] == "ask" and "floor" in fx.store.get(formed["id"]).decision_reason
    assert await fx.mind.outbox_ready() == [] or all(p["recipient"] == OWNER for p in await fx.mind.outbox_ready())

    deny_router = StubRouter(reply="Hi, the reserve figure is confidential but here it is.")
    denied = make([contact(CONTACT, may_contact="auto")], router=deny_router,
                  config={"deny": {"text": ["confidential"]}})
    _seed_check_in(denied)
    denied.shift(C + PAST)
    formed, = (await denied.tick())["formed"]
    assert formed["decision"] == "drop" and denied.messages_to(CONTACT)[0].status == "dropped"


async def test_a_never_contact_is_not_composed_for(make):
    router = StubRouter()
    fx = make([contact(CONTACT, may_contact="never")], router=router)
    _seed_check_in(fx)
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    assert formed["decision"] == "drop" and router.calls == []
