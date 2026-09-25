"""The owner's replies direct outreach (architecture 4.10, M11): linked to what they answer, learned where
the mind already learns (the verdict and its feedback, interests, mutes, the pause, the hour's timing)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from protagine.mind import outreach
from test_mind_outreach_loop import H, OWNER, Fx, make, report  # noqa: F401  (make is a pytest fixture)


async def shared(fx, topic="tidal energy", code="QX-41", turn="t-declare", research=True):
    """The owner declares ``topic``, the mind researches it and shares the finding; the outreach row."""
    await say(fx, f"I care a lot about {topic}; anything new on it is worth hearing about.", turn)
    fx.shift(timedelta(minutes=1))
    if research:
        await fx.research(topic, report(code, topic))
    else:
        fx.found(topic, report(code, topic))   # curiosity's satiation holds a second research for hours
    await fx.tick()
    row = next(item for item in fx.outreach_rows("outreach_finding") if item.context["topic"] == topic)
    assert row.status == "sent"
    return row


async def say(fx, text, turn, session="owner-1"):
    """An owner turn: in the ledger (quotes read it back), then through the turn path's hook."""
    fx.ledger.record_source(turn, contact_id=OWNER, session_id=session, messages=[{"role": "user", "content": text}],
                            scope="person", occurred_at=fx.now.isoformat(), derive_claims=False)
    return await fx.mind.owner_turn(text, turn_id=turn, occurred_at=fx.now, session_id=session)


def reaction(fx, row):
    return (fx.store.get(row.id).result_metadata or {}).get("reaction") or {}


# -- the link -----------------------------------------------------------------------------------------

async def test_a_reply_naming_the_topic_links_to_that_outreach_newest_first(make):
    fx = make()
    first = await shared(fx, "tidal energy", "QX-41")
    fx.shift(3 * H)
    second = await shared(fx, "fern species", "RB-17", turn="t-2", research=False)
    fx.shift(10 * 60 * timedelta(seconds=1))
    summary = await say(fx, "That tidal energy item you sent was not useful to me.", "t-reply", "owner-2")
    assert summary["linked"] == first.id and reaction(fx, first)["class"] == "negative"
    assert reaction(fx, second) == {}


async def test_the_first_turn_after_an_outreach_links_by_position_when_it_reads_as_a_reply(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=10))
    assert (await say(fx, "Not now.", "t-reply", "owner-2"))["linked"] == row.id
    fx2 = make()
    row2 = await shared(fx2)
    fx2.shift(timedelta(minutes=10))
    long = "I need to find out when the last train leaves the central station tonight, can you check the timetable"
    assert (await say(fx2, long, "t-other"))["linked"] is None, "a long turn about something else is not a reply"
    assert reaction(fx2, row2) == {}


async def test_only_the_owners_first_turn_after_a_send_links_by_position(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=5))
    assert (await say(fx, "Thanks, see you later.", "t-0", "owner-2"))["linked"] is None, "no class, no position link"
    fx2 = make()
    row = await shared(fx2)
    fx2.shift(timedelta(minutes=5))
    await say(fx2, "Great find, thanks.", "t-1", "owner-2")        # welcome: linked by position
    fx2.shift(timedelta(minutes=5))
    assert (await say(fx2, "Not now.", "t-2", "owner-2"))["linked"] is None
    assert reaction(fx2, row)["class"] == "welcome" and fx2.store.get(row.id).verdict == "useful"


async def test_a_linked_reply_with_no_reaction_is_engagement(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=5))
    await say(fx, "Ah, the tidal energy item you sent reminds me of a trip I took.", "t-1", "owner-2")
    assert reaction(fx, row)["class"] == "engaged" and fx.store.get(row.id).verdict == "actioned"


async def test_a_reply_after_the_window_links_nothing(make):
    fx = make()
    await shared(fx)
    fx.shift(25 * H)
    assert (await say(fx, "That tidal energy item was not useful.", "t-late", "owner-2"))["linked"] is None


async def test_a_retried_turn_applies_its_effects_once(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=5))
    await say(fx, "Yes, dig deeper into the tidal energy item you sent.", "t-dig", "owner-2")
    level = fx.mind.mind_state.get("interest:tidal-energy")["level"]
    again = await fx.mind.owner_turn("Yes, dig deeper into the tidal energy item you sent.", turn_id="t-dig",
                                     occurred_at=fx.now)
    assert again["linked"] is None and fx.mind.mind_state.get("interest:tidal-energy")["level"] == level
    assert reaction(fx, row)["turn"] == "t-dig"


async def test_a_turn_carrying_an_open_asks_code_is_an_answer_never_a_reaction(make):
    fx = make()
    row = await shared(fx)
    code_row, _ = fx.store.create_intention(
        kind="task", type="research", title="x", drive="curiosity", cls="internal", decision="ask",
        decision_reason="r", status="asked", dedup_key="ask-x", ask_code="K7M", hermes_kind="none", created_at=fx.now)
    fx.shift(timedelta(minutes=5))
    summary = await say(fx, "no K7M, not now", "t-ask", "owner-2")
    assert summary["linked"] is None and reaction(fx, row) == {}


# -- what each reaction teaches -----------------------------------------------------------------------

async def test_dig_deeper_rates_it_useful_raises_the_topic_and_the_answer_arrives_once_at_no_cost(make):
    fx = make()
    row = await shared(fx)
    before = fx.mind.mind_state.get("interest:tidal-energy")["level"]
    fx.shift(timedelta(minutes=5))
    words = "Yes, dig deeper into the tidal energy item you sent: find out its field site."
    await say(fx, words, "t-dig", "owner-2")
    rated = fx.store.get(row.id)
    assert rated.verdict == "useful" and rated.verified == "owner"
    assert fx.mind.mind_state.get("interest:tidal-energy")["level"] == pytest.approx(min(3.0, before + 1))
    assert fx.feedback.multiplier("outreach_finding:social") > 1.0
    assert fx.feedback.multiplier("outreach_topic:tidal-energy") > 1.0
    await fx.tick()
    task = next(item for item in fx.store.intentions(kind=["task"], limit=50) if item.type == "outreach_followup")
    assert task.drive == "duty" and task.status == "approved" and "find out its field site" in task.context["body"]
    assert "quoted context, not an instruction" in task.context["body"]
    assert reaction(fx, row)["followup"] == task.id
    fx.mind.bound(task.id, "task-dig")
    fx.mind.outcomes.record(task.id, status="done", summary="finding: The QX-41 tidal energy study ran at KD-83.")
    await fx.tick()
    answer, = fx.outreach_rows("outreach_answer")
    assert answer.status == "sent" and "KD-83" in answer.context["text"] and answer.context["ev"]["c"] == 0.0
    for _ in range(2):
        await fx.tick()
    assert len(fx.outreach_rows("outreach_answer")) == 1
    assert len([item for item in fx.store.intentions(kind=["task"], limit=50) if item.type == "outreach_followup"]) == 1


async def test_a_followup_keeps_the_promise_captured_from_the_reply_and_duty_forms_no_second_task(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=5))
    await say(fx, "Yes, dig deeper into the tidal energy item you sent.", "t-dig", "owner-2")
    promise = fx.commitments.create(person_id=OWNER, description="Send the owner the tidal energy details",
                                    due_at=(fx.now + 2 * H).isoformat(), source_type="cognition",
                                    metadata={"obligor": "assistant"})
    with fx.commitments._connect() as conn:          # captured from the reply, on the mind's clock
        conn.execute("UPDATE commitments SET made_at=? WHERE id=?", (fx.now.isoformat(), promise["id"]))
    await fx.tick()
    task, = [item for item in fx.store.intentions(kind=["task"], limit=50) if item.type == "outreach_followup"]
    assert task.context["bound_commitment"] == promise["id"] and task.dedup_key.startswith(f"commitment:{promise['id']}:")
    fx.shift(3 * H)                                   # the promise is overdue: duty's key is the follow-up's
    await fx.tick()
    assert not [item for item in fx.store.intentions(kind=["task"], limit=50) if item.type == "commitment_overdue"]
    fx.mind.bound(task.id, "task-dig")
    fx.mind.outcomes.record(task.id, status="done", summary="finding: The tidal energy study ran at KD-83.")
    assert fx.commitments.get(promise["id"])["status"] in {"pending", "overdue"}, "the dig is not the delivery"
    await fx.tick()
    assert fx.outreach_rows("outreach_answer")[0].status == "sent"
    assert fx.commitments.get(promise["id"])["status"] == "fulfilled"


async def test_not_interested_mutes_the_topic_and_a_later_similar_finding_is_held(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=5))
    await say(fx, "Not interested in tidal energy after all, drop it.", "t-no", "owner-2")
    assert fx.store.get(row.id).verdict == "not_useful"
    assert fx.mind.mind_state.get("outreach.mute:tidal-energy")["level"] == 1.0
    assert fx.mind.mind_state.get("interest:tidal-energy")["level"] == 0.0
    assert fx.feedback.multiplier("outreach_topic:tidal-energy") < 1.0
    fx.shift(3 * 24 * H)
    fx.found("tidal energy prices", report("MV-52", "tidal energy prices"))
    for _ in range(2):
        await fx.tick()
    assert len(fx.outreach_rows()) == 1


async def test_a_disinterest_about_a_topic_mutes_it_without_any_outreach(make):
    fx = make()
    await say(fx, "My flatmate keeps going on about kelp farming; I could not care less about kelp farming.", "t-1")
    assert fx.mind.mind_state.get("outreach.mute:kelp-farming")["level"] == 1.0
    fx.found("kelp farming", report("YB-63", "kelp farming"))
    for _ in range(2):
        await fx.tick()
    assert fx.outreach_rows() == []


async def test_not_now_pauses_for_hours_marks_the_hour_and_the_digest_lists_the_item(make):
    fx = make()
    row = await shared(fx)
    fx.shift(timedelta(minutes=5))
    await say(fx, "That tidal energy item you sent: not now, I am in the middle of something.", "t-later", "owner-2")
    assert fx.store.get(row.id).verdict is None and reaction(fx, row)["class"] == "not_now"
    until = outreach.pause_until(fx.mind.mind_state.get(outreach.PAUSE_KEY))
    assert until == fx.now + outreach.NOT_NOW_HOLD
    hour = fx.now.astimezone(fx.mind.tz).hour
    assert fx.mind.mind_state.get(f"outreach.timing:{hour:02d}")["level"] == 1.0
    assert fx.mind.mind_state.get(f"outreach.timing:{(hour + 1) % 24:02d}")["level"] == 0.5
    fx.found("fern species", report("RB-17", "fern species"))
    fx.mind.add_interest("fern species", by="turn:t-x")
    await fx.tick()
    assert len(fx.outreach_rows()) == 1, "held while the owner is busy"
    fx.mind.digest_hour = 0
    fx.shift(timedelta(minutes=1))
    await fx.tick()
    digest = [p for p in fx.sent if p["type"] == "digest"][-1]
    assert "tidal energy" in digest["text"] and "not now" in digest["text"]


async def test_stop_pauses_at_once_cancels_what_is_queued_and_reminders_still_go(make):
    fx = make()
    await say(fx, "I care a lot about tidal energy.", "t-1")
    await fx.research("tidal energy", report("QX-41", "tidal energy"))
    await fx.mind.tick(force=True)                    # formed, not yet pulled by the body
    queued, = fx.outreach_rows()
    assert queued.status == "approved"
    summary = await say(fx, "Please stop checking in with me unprompted.", "t-stop", "owner-2")
    assert summary["classes"] == ["stop"] and fx.store.get(queued.id).status == "cancelled"
    assert fx.mind.state()["outreach"]["paused_until"] == outreach.INDEFINITE
    fx.owner_commitment("Send the signed lease back", due=fx.now - H)
    fx.shift(40 * H)
    await fx.tick()
    assert [p["type"] for p in fx.sent] == ["commitment_reminder"]
    await say(fx, "You can check in again.", "t-resume", "owner-3")
    assert fx.mind.state()["outreach"]["paused_until"] is None


async def test_strain_is_care_for_the_named_thing_and_relief_ends_it(make):
    fx = make()
    await say(fx, "I am really stressed about the grant report; I am behind on it.", "t-stress")
    care = fx.mind.mind_state.get("care:grant-report")
    assert care["level"] == 1.0 and care["text"] == "grant report" and "turn:t-stress" in care["causes"]
    await fx.mind.tick(force=True)
    offer, = fx.outreach_rows("outreach_care")
    assert 'You said "I am really stressed about the grant report"' in offer.context["text"]
    assert offer.status == "approved"
    await say(fx, "I finished the grant report, all good now.", "t-done", "owner-2")
    assert fx.mind.mind_state.get("care:grant-report")["level"] == 0.0
    assert fx.store.get(offer.id).status == "cancelled"


async def test_a_worry_about_a_person_is_not_care(make):
    fx = make()
    await say(fx, "I am worried about p-03, they have gone quiet.", "t-p")
    assert fx.mind.mind_state.items("care:") == []


async def test_silence_past_a_day_is_ignored_lowers_the_topic_and_raises_the_next_cost(make):
    fx = make()
    row = await shared(fx)
    before = fx.mind.mind_state.get("interest:tidal-energy")["level"]
    fx.shift(25 * H)
    await fx.tick()
    scored = fx.store.get(row.id)
    assert scored.verdict == "ignored" and reaction(fx, row)["class"] == "silence"
    assert fx.mind.mind_state.get("interest:tidal-energy")["level"] == pytest.approx(0.8 * before, rel=0.05)
    state = await fx.mind._gather(fx.now)
    assert outreach.ignored_streak(state.outreach) == 1


async def test_the_owners_opt_out_seen_by_the_appraisal_pauses_when_no_phrase_matched(make):
    fx = make()
    await fx.mind.owner_signal({"turn_id": "t-q", "opt_out": True, "occurred_at": fx.now.timestamp()})
    assert fx.mind.state()["outreach"]["paused_until"] == outreach.INDEFINITE


async def test_an_owner_dismissal_seen_by_the_appraisal_after_a_send_is_a_negative_on_it(make):
    fx = make()
    row = await shared(fx)
    events = []

    class Appraisals:
        def view(self, *_, **__):
            return {"records": []}

        def affect_events(self, *, since, limit=1000):
            return [event for event in events if event["occurred_at"] >= since]

        def pending_jobs(self, **_):
            return {"pending": 0, "running": 0}
    fx.mind.appraisals = Appraisals()
    fx.shift(timedelta(minutes=10))
    events.append({"ref": "o-1", "kind": "dismissed", "topic": "the tidal energy news", "turn_id": "t-meh",
                   "occurred_at": fx.now.timestamp(), "created_at": fx.now.timestamp()})
    await fx.tick()
    assert fx.store.get(row.id).verdict == "not_useful" and reaction(fx, row)["by"] == "appraisal"


async def test_with_the_faculty_off_the_owners_words_change_nothing(make):
    fx = make(config={"faculties": {"outreach": False}})
    assert await say(fx, "Please stop checking in with me unprompted.", "t-stop") is None
    assert fx.mind.mind_state.get(outreach.PAUSE_KEY) is None
    await fx.mind.owner_signal({"turn_id": "t-q", "opt_out": True, "occurred_at": fx.now.timestamp()})
    assert fx.mind.mind_state.get(outreach.PAUSE_KEY) is None


# -- the switch and the turn path ---------------------------------------------------------------------

async def test_the_api_switch_pauses_and_resumes_and_answers_off_when_the_faculty_is(make, monkeypatch):
    from protagine.api.routers import mind as mind_router
    fx = make()
    app = FastAPI()
    app.include_router(mind_router.router)
    mind_router.set_mind(fx.mind)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as client:
            off = (await client.post("/v1/mind/outreach", json={"state": "off"})).json()
            assert off["enabled"] is True and off["paused_until"] == outreach.INDEFINITE
            on = (await client.post("/v1/mind/outreach", json={"state": "on"})).json()
            assert on["paused_until"] is None
            assert (await client.post("/v1/mind/outreach", json={"state": "maybe"})).status_code == 422
        fx.mind.faculties["outreach"] = False
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as client:
            assert (await client.post("/v1/mind/outreach", json={"state": "off"})).json() == {"enabled": False}
    finally:
        mind_router.set_mind(None)


async def test_the_turn_path_hands_the_owners_turns_to_the_mind_and_nothing_else(monkeypatch, tmp_path):
    from protagine.api.routers import host
    from protagine.api.routers import mind as mind_router
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    for name in ("_contacts_store", "_telemetry", "_reranker", "_context_recall_selector", "_comms_log"):
        monkeypatch.setattr(host, name, None)
    calls = []

    class Mind:
        async def owner_turn(self, text, **kwargs):
            calls.append((text, kwargs))
            if "boom" in text:
                raise RuntimeError("boom")
            return {}
    from protagine.api.middleware import ApiKeyMiddleware
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    key = "hook-key-" + "x" * 32
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=key)
    app.include_router(host.router)
    mind_router.set_mind(Mind())
    auth = {"Authorization": f"Bearer {key}"}

    def turn(contact, text, ident):
        return {"identity": {"host_id": "hermes"},
                "context": {"session_id": "s-1", "contact_id": contact, "turn_id": ident,
                            "metadata": {"occurred_at": "2026-01-06T12:00:00Z"}},
                "user_message": {"role": "user", "content": text},
                "assistant_message": {"role": "assistant", "content": "ok"}}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as client:
            for contact, text, ident in ((OWNER, "Not now.", "t-1"), ("p-02", "Not now.", "t-2"),
                                         ("system", "Not now.", "t-3"), (OWNER, "boom", "t-4")):
                response = await client.post("/v1/host/turns/sync", json=turn(contact, text, ident), headers=auth)
                assert response.status_code == 200, (contact, response.text)
    finally:
        mind_router.set_mind(None)
    assert [text for text, _ in calls] == ["Not now.", "boom"]
    assert calls[0][1]["turn_id"] == "t-1" and calls[0][1]["session_id"] == "s-1"
    assert calls[0][1]["occurred_at"].isoformat().startswith("2026-01-06T12:00")


async def test_the_appraisal_hands_the_owners_opt_out_to_the_owner_net_and_never_to_contact_affect(tmp_path):
    from protagine.self_model.appraisals import AppraisalStore
    from protagine.turns.idempotency import TurnIdempotencyLedger
    contact_calls, owner_calls = [], []

    async def on_owner(signal):
        owner_calls.append(signal)
    store = AppraisalStore(TurnIdempotencyLedger(tmp_path / "ledger.db"), owner_id=OWNER,
                           on_contact=contact_calls.append, on_owner=on_owner)
    source = {"contact_id": OWNER, "turn_id": "t-1", "version": 1, "occurred_at": 5.0, "ingested_at": 6.0}
    await store._signal_contact(source, {"their_valence": -0.5, "opt_out": True})
    await store._signal_contact(source, {"their_valence": -0.5, "opt_out": False})
    assert owner_calls == [{"turn_id": "t-1", "opt_out": True, "occurred_at": 5.0}] and contact_calls == []
    await store._signal_contact({**source, "contact_id": "p-02"}, {"their_valence": None, "opt_out": True})
    assert len(contact_calls) == 1 and len(owner_calls) == 1


async def test_the_production_writer_finds_the_running_mind_or_does_nothing():
    from protagine.beliefs.source_projection import owner_signal_writer
    seen = []

    class Mind:
        async def owner_signal(self, signal):
            seen.append(signal)
    result = owner_signal_writer(lambda: Mind())({"turn_id": "t", "opt_out": True})
    await result
    assert seen == [{"turn_id": "t", "opt_out": True}]
    assert owner_signal_writer(lambda: None)({"opt_out": True}) is None


async def test_a_reply_in_the_same_second_as_the_send_still_answers_it(make):
    """A turn's time comes to the whole second, the send's to the microsecond: the same second links."""
    fx = make()
    await say(fx, "I care a lot about tidal energy.", "t-declare")
    fx.shift(timedelta(minutes=1, milliseconds=400))
    fx.found("tidal energy", report("QX-41", "tidal energy"))
    await fx.tick()
    row, = fx.outreach_rows()
    assert fx.store.get(row.id).completed_at.microsecond == 400000
    fx.now = fx.now.replace(microsecond=0)          # the reply, stamped to the whole second
    assert (await say(fx, "That tidal energy item you sent: not now.", "t-same", "owner-2"))["linked"] == row.id
