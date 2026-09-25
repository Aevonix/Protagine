"""Owner outreach through the tick: a Mind over the real stores, no model, a fake clock (M11).

A finding on a declared interest goes out once; a second waits for the interruption to fade; the daily
budget stops the fourth without taking a reminder's slot; quiet hours form nothing and a finding that
ages out lands in the digest once; an open loop is offered after a quiet stretch and not minutes after
the owner talked; care is offered for a named thing and survives overload while a finding does not; with
``faculties.outreach`` off none of it exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from protagine.commitments.store import CommitmentStore
from protagine.feedback import TypeFeedbackStore
from protagine.initiatives.store import InitiativeStore
from protagine.mind import Mind
from protagine.mind import outreach
from protagine.mind.affect import AffectView
from protagine.turns.idempotency import TurnIdempotencyLedger

OWNER = "p-01"
T0 = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
H = timedelta(hours=1)
OUTREACH = {"outreach_finding", "outreach_loop", "outreach_care", "outreach_answer"}


class Fx:
    def __init__(self, tmp_path, config=None):
        self.now = T0
        self.store = InitiativeStore(state_dir=tmp_path)
        self.commitments = CommitmentStore(tmp_path / "protagine-commitments.db")
        self.feedback = TypeFeedbackStore(str(tmp_path / "protagine-feedback.db"))
        self.ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
        config = dict(config or {})
        config["faculties"] = {"consolidation": False, **(config.get("faculties") or {})}
        self.mind = Mind(config={"autonomy": "standard", "quiet_hours": "", **config}, store=self.store,
                         state_dir=tmp_path, owner_id=OWNER, commitments=self.commitments, feedback=self.feedback,
                         ledger=self.ledger, clock=lambda: self.now, backups=False)
        self.mind.digest_hour = 25
        self.sent = []
        self.tasks = 0

    def shift(self, delta):
        self.now += delta

    def owner_spoke(self, at=None):
        self.mind.mind_state.set(outreach.OWNER_TURN_KEY, text=(at or self.now).isoformat(), now=self.now)

    async def tick(self):
        summary = await self.mind.tick(force=True)
        for payload in await self.mind.outbox_ready():
            self.mind.outbox.sending(payload["id"], target=f"capture:{payload['recipient']}")
            self.mind.outbox.sent(payload["id"])
            self.sent.append(payload)
        return summary

    async def research(self, topic, report):
        """The curiosity drive's research on ``topic`` runs and reports ``report``."""
        await self.tick()
        row = next(item for item in self.store.intentions(kind=["task"], limit=100)
                   if item.type == "research" and topic in item.description and item.status == "approved")
        self.tasks += 1
        self.mind.bound(row.id, f"task-{self.tasks}")
        self.mind.outcomes.record(row.id, status="done", summary=report)
        return self.store.get(row.id)

    def found(self, topic, report_text):
        """A research task on ``topic`` the mind ran earlier finishes now with ``report_text`` (curiosity's
        satiation after a first research holds a second for hours, which is not what these tests are about)."""
        row, _ = self.store.create_intention(
            kind="task", type="research", title=f"Research: {topic}", drive="curiosity", cls="internal",
            decision="act", decision_reason="standard: internal -> act", status="approved",
            dedup_key=f"research:{topic}:{self.now.isoformat()}", context={"topic": topic, "evidence": []},
            hermes_kind="none", created_at=self.now)
        self.tasks += 1
        self.mind.bound(row.id, f"task-{self.tasks}")
        self.mind.outcomes.record(row.id, status="done", summary=report_text)
        return self.store.get(row.id)

    def outreach_rows(self, type=None):
        return [row for row in self.store.intentions(kind=["message"], limit=500)
                if row.type in OUTREACH and (type is None or row.type == type)]

    def owner_commitment(self, description, *, due=None, created=None, **metadata):
        row = self.commitments.create(person_id=OWNER, description=description, due_at=due.isoformat() if due else None,
                                      source_type="cognition", metadata={"obligor": "owner", **metadata},
                                      allow_overdue=True)
        if created is not None:
            with self.commitments._connect() as conn:
                conn.execute("UPDATE commitments SET made_at=? WHERE id=?", (created.isoformat(), row["id"]))
        return row

    def close(self):
        self.store.close()


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    made = []

    def build(**kwargs):
        directory = tmp_path / f"fx-{len(made)}"
        directory.mkdir()
        fx = Fx(directory, **kwargs)
        made.append(fx)
        return fx
    yield build
    for fx in made:
        fx.close()


def report(code, topic):
    return f"finding: {code}: A practical study of {topic} was published. It compares two approaches."


async def test_a_finding_on_a_declared_interest_goes_out_once_and_says_why(make):
    fx = make()
    fx.owner_spoke()
    fx.mind.add_interest("tidal energy", by="turn:t-1")
    done = await fx.research("tidal energy", report("QX-41", "tidal energy"))
    assert done.result_metadata["outreach"]["state"] == "pending"
    await fx.tick()
    row, = fx.outreach_rows()
    assert row.type == "outreach_finding" and row.entity_id == OWNER and row.status == "sent"
    assert row.context["topic_slug"] == "tidal-energy" and row.context["source_ref"] == f"intention:{done.id}"
    assert "QX-41" in row.context["text"] and "tidal energy" in row.context["text"]
    assert row.context["why"] in row.context["text"] and row.expires_at <= fx.now + 12 * H + timedelta(minutes=1)
    assert fx.store.get(done.id).result_metadata["outreach"] == {**fx.store.get(done.id).result_metadata["outreach"],
                                                                 "state": "sent", "row": row.id}
    for _ in range(3):
        await fx.tick()
    assert len(fx.outreach_rows()) == 1 and len([p for p in fx.sent if p["type"] == "outreach_finding"]) == 1


async def test_a_second_finding_waits_for_the_first_to_fade_and_the_budget_spares_reminders(make):
    fx = make(config={"budgets": {"outreach_per_day": 2, "owner_messages_per_day": 1}})
    fx.owner_spoke()
    fx.mind.add_interest("tidal energy", by="turn:t-1")
    await fx.research("tidal energy", report("QX-41", "tidal energy"))
    await fx.tick()
    assert len(fx.outreach_rows()) == 1
    fx.shift(timedelta(minutes=5))
    fx.mind.add_interest("fern species", by="turn:t-2")
    fx.found("fern species", report("RB-17", "fern species"))
    for _ in range(2):
        await fx.tick()
    assert len(fx.outreach_rows()) == 1, "minutes after the first, a second interruption is held"
    fx.shift(2 * H)
    await fx.tick()
    assert [row.type for row in fx.outreach_rows()] == ["outreach_finding"] * 2
    # The third would be the third unprompted message today: the outreach budget (2) holds it.
    fx.shift(2 * H)
    fx.mind.add_interest("clock repair", by="turn:t-3")
    fx.found("clock repair", report("KD-83", "clock repair"))
    fx.shift(3 * H)
    for _ in range(2):
        await fx.tick()
    assert len(fx.outreach_rows()) == 2
    assert "2 outreach messages per day" in fx.mind.authority.budget_check(kind="message", recipient=OWNER,
                                                                          type="outreach_finding")
    # A reminder the owner asked for still has its own slot today.
    fx.owner_commitment("Send the signed lease back", due=fx.now - H)
    await fx.tick()
    assert [p["type"] for p in fx.sent][-1] == "commitment_reminder"


async def test_quiet_hours_form_nothing_and_an_aged_out_finding_lands_in_the_digest_once(make):
    fx = make(config={"quiet_hours": "22:00-07:00"})
    fx.shift(10 * H)                                  # 22:00
    fx.owner_spoke()
    fx.mind.add_interest("tidal energy", by="turn:t-1")
    done = await fx.research("tidal energy", report("QX-41", "tidal energy"))
    fx.shift(30 * 60 * 1 * timedelta(seconds=1))
    for _ in range(3):
        await fx.tick()
    assert fx.outreach_rows() == [] and fx.store.get(done.id).result_metadata["outreach"]["state"] == "pending"
    # Two days on the finding has aged out: the next digest lists it once.
    fx.mind.digest_hour = 8
    fx.shift(timedelta(hours=58))                     # 08:30 two days later
    await fx.tick()
    digests = [p for p in fx.sent if p["type"] == "digest"]
    assert len(digests) == 1 and "Found for you" in digests[0]["text"] and "QX-41" in digests[0]["text"]
    assert fx.store.get(done.id).result_metadata["outreach"]["state"] == "listed"
    fx.shift(timedelta(days=1))
    await fx.tick()
    later = [p for p in fx.sent if p["type"] == "digest"]
    assert len(later) == 1 or "QX-41" not in later[-1]["text"]


async def test_an_open_loop_is_offered_after_a_quiet_stretch_and_not_minutes_after_a_turn(make):
    fx = make()
    fx.owner_spoke(fx.now - 30 * H)
    loop = fx.owner_commitment("Finish the lease renewal", due=fx.now + timedelta(days=14), created=fx.now - 31 * H)
    fx.owner_commitment("Tidy the garden plan", created=fx.now - 31 * H, reschedule={"from": None, "by": "conversation"})
    await fx.tick()
    row, = fx.outreach_rows()
    assert row.type == "outreach_loop" and "lease renewal" in row.context["text"]
    assert row.invalidates_if == f"commitment:{loop['id']}:resolved"
    for _ in range(2):
        await fx.tick()
    assert len(fx.outreach_rows()) == 1


async def test_nothing_is_offered_minutes_after_the_owner_talked(make):
    fx = make()
    fx.owner_commitment("Finish the lease renewal", due=fx.now + timedelta(days=14), created=fx.now - 2 * H)
    fx.owner_spoke(fx.now - timedelta(minutes=20))
    for _ in range(3):
        await fx.tick()
    assert fx.outreach_rows() == []


async def test_care_for_a_named_thing_is_offered_even_under_overload_and_a_finding_is_not(make, monkeypatch):
    fx = make()
    fx.owner_spoke()
    item = fx.owner_commitment("Finish the grant report", due=fx.now + timedelta(days=3), created=fx.now - 2 * H)
    fx.mind.add_interest("tidal energy", by="turn:t-1")
    done = await fx.research("tidal energy", report("QX-41", "tidal energy"))
    # Overloaded, with a finding pending (fresher, so ranked first) and the owner's strain on a named thing.
    monkeypatch.setattr(fx.mind.feelings, "view", lambda: AffectView(route={}, owner_id=OWNER, overloaded=True))
    fx.mind.mind_state.set("care:grant-report", level=1.0, text="grant report",
                           causes=["turn:t-5", f"commitment:{item['id']}"], half_life_s=72 * 3600, now=fx.now)
    fx.shift(timedelta(minutes=30))
    await fx.tick()
    rows = fx.outreach_rows()
    assert [row.type for row in rows] == ["outreach_care"]
    assert "grant report" in rows[0].context["text"] and rows[0].invalidates_if == f"commitment:{item['id']}:resolved"
    assert fx.store.get(done.id).result_metadata["outreach"]["state"] == "pending"
    # The loop branch never offers the same item the care offer names.
    fx.shift(40 * H)
    await fx.tick()
    assert "outreach_loop" not in [row.type for row in fx.outreach_rows()]


async def test_with_the_faculty_off_none_of_it_exists(make, monkeypatch):
    fx = make(config={"faculties": {"outreach": False}})
    fx.owner_spoke(fx.now - 40 * H)
    fx.owner_commitment("Finish the lease renewal", due=fx.now + timedelta(days=14), created=fx.now - 41 * H)
    fx.mind.mind_state.set("care:grant-report", level=1.0, text="grant report", causes=["turn:t-5"],
                           half_life_s=72 * 3600, now=fx.now)
    fx.mind.add_interest("tidal energy", by="turn:t-1")
    done = await fx.research("tidal energy", report("QX-41", "tidal energy"))
    assert "outreach" not in done.result_metadata
    for _ in range(3):
        await fx.tick()
    assert fx.outreach_rows() == []
    inputs = await fx.mind._gather(fx.now)
    assert inputs.outreach is None
    fx.mind.digest_hour = 0
    await fx.tick()
    digest = [p for p in fx.sent if p["type"] == "digest"]
    assert all("Found for you" not in p["text"] and "Offers" not in p["text"] for p in digest)
    assert "outreach" not in fx.mind.state() or fx.mind.state()["outreach"]["enabled"] is False


async def test_interests_read_as_before_with_the_faculty_off(make):
    """``_interests`` with outreach off is what it was before M11: the mind's interests and the owner's
    appraisal interests, each appraisal adding one to its topic, nothing persisted."""
    fx = make(config={"faculties": {"outreach": False}})

    class Appraisals:
        def view(self, subject, **_):
            return {"records": [{"id": "a-1", "kind": "appraisal", "dimension": "interest", "topic": "kelp farming",
                                 "hint": "offer_relevant_topic"}]}
    fx.mind.appraisals = Appraisals()
    fx.mind.add_interest("tidal energy", by="turn:t-1")
    found = {item["topic"]: item for item in fx.mind._interests()}
    assert found["kelp farming"]["weight"] == 1.0 and found["kelp farming"]["sources"] == ["appraisal:a-1"]
    assert fx.mind.mind_state.get("interest:kelp-farming") is None


async def test_a_welcome_appraisal_interest_is_kept_for_a_month_with_the_faculty_on(make):
    fx = make()

    class Appraisals:
        def view(self, subject, **_):
            return {"records": [{"id": "a-1", "kind": "appraisal", "dimension": "interest", "topic": "kelp farming",
                                 "hint": "offer_relevant_topic"},
                                {"id": "a-2", "kind": "appraisal", "dimension": "interest", "topic": "moss",
                                 "hint": "none"}]}
    fx.mind.appraisals = Appraisals()
    first = {item["topic"]: item for item in fx.mind._interests()}
    again = {item["topic"]: item for item in fx.mind._interests()}
    kept = fx.mind.mind_state.get("interest:kelp-farming")
    assert kept["level"] == 1.0 and kept["causes"] == ["appraisal:a-1"], "persisted once per record"
    assert first["kelp farming"]["origin"] == again["kelp farming"]["origin"] == "welcome"
    assert first["moss"]["origin"] == "mentioned" and fx.mind.mind_state.get("interest:moss") is None


async def test_the_state_shows_the_outreach_block(make):
    fx = make()
    state = fx.mind.state()["outreach"]
    assert state["enabled"] is True and state["paused_until"] is None and state["per_day"] == 3
    assert state["sent_24h"] == 0 and state["muted"] == [] and state["care"] == []


# -- substance, spacing and grounded reasons (review of M11) -------------------------------------------------

async def test_a_muted_interest_weighs_nothing_and_is_not_researched_again(make):
    fx = make()
    fx.owner_spoke()
    fx.mind.add_interest("tidal energy", by="turn:t-1")
    await fx.research("tidal energy", report("QX-41", "tidal energy"))
    await fx.mind.owner_turn("Not interested in tidal energy after all, drop it.", turn_id="t-no", occurred_at=fx.now)
    entry = next(item for item in fx.mind._interests() if item["topic"] == "tidal energy")
    assert entry["weight"] == 0.0
    before = {row.id for row in fx.store.intentions(kind=["task"], limit=200) if row.type == "research"}
    fx.shift(timedelta(days=8))
    fx.owner_spoke()
    await fx.tick()
    assert not [row for row in fx.store.intentions(kind=["task"], limit=200)
                if row.type == "research" and row.id not in before and "tidal energy" in row.description]


async def test_silence_on_an_outreach_never_makes_the_agents_own_interest_the_owners(make):
    fx = make()
    fx.owner_spoke()
    fx.mind.add_interest("tidal energy", why="a declared identity interest", by="owner")
    fx.owner_commitment("write the tidal energy cost memo", due=fx.now + timedelta(days=10), created=fx.now - 2 * H)
    fx.found("tidal energy", report("QX-41", "tidal energy"))
    await fx.tick()
    first, = fx.outreach_rows("outreach_finding")
    fx.shift(25 * H)
    fx.owner_spoke()
    await fx.tick()
    assert fx.store.get(first.id).verdict == "ignored"
    causes = fx.mind.mind_state.get("interest:tidal-energy")["causes"]
    assert outreach.interest_origin(causes) == "own" and not any(c.startswith("silence:") for c in causes)
    fx.shift(8 * 24 * H)
    fx.owner_spoke()
    fx.found("tidal energy", report("RB-17", "tidal energy"))
    await fx.tick()
    assert not any(row.context["why"].startswith("You told me") for row in fx.outreach_rows("outreach_finding"))


async def test_three_findings_are_spread_out_not_sent_inside_an_hour(make):
    fx = make(config={"budgets": {"outreach_per_day": 3}})
    fx.owner_spoke()
    for i, topic in enumerate(["tidal energy", "fern species", "clock repair"]):
        fx.mind.add_interest(topic, by=f"turn:t-{i}")
    fx.found("tidal energy", report("QX-41", "tidal energy"))
    await fx.tick()
    start = fx.now
    fx.found("fern species", report("RB-17", "fern species"))
    fx.found("clock repair", report("KD-83", "clock repair"))
    times = []
    for _ in range(6 * 12):
        fx.shift(timedelta(minutes=5))
        before = len(fx.outreach_rows())
        await fx.tick()
        if len(fx.outreach_rows()) > before:
            times.append(fx.now - start)
    assert len(times) == 2 and times[0] >= outreach.MIN_GAP and times[1] - times[0] >= outreach.MIN_GAP, times


async def test_the_owners_last_turn_is_read_from_the_ledger_when_the_mark_is_missing(make):
    """After an upgrade (or the flag switched back on) the mark is absent or stale: old open items do not read
    as a quiet stretch when the owner spoke minutes ago."""
    fx = make()
    for item in ["Renew the passport", "Sort the garage shelves", "Book the boiler service"]:
        fx.owner_commitment(item, created=fx.now - timedelta(days=10))
    fx.ledger.record_source("t-pre", contact_id=OWNER, session_id="s", messages=[{"role": "user", "content": "hi"}],
                            scope="person", occurred_at=(fx.now - timedelta(minutes=5)).isoformat(), derive_claims=False)
    for _ in range(0, 90, 5):
        await fx.tick()
        fx.shift(timedelta(minutes=5))
    assert fx.outreach_rows("outreach_loop") == []


async def test_one_check_in_per_quiet_stretch(make):
    fx = make()
    fx.owner_spoke(fx.now - 40 * H)
    for item in ["Renew the passport", "Sort the garage shelves", "Book the boiler service"]:
        fx.owner_commitment(item, created=fx.now - timedelta(days=10))
    for _ in range(12):
        await fx.tick()
        fx.shift(H)
    assert len(fx.outreach_rows("outreach_loop")) == 1


async def test_a_repeated_or_empty_report_is_never_sent_as_a_finding(make):
    from test_mind_outreach_reactions import say, shared
    fx = make()
    row = await shared(fx)                              # tidal energy, QX-41
    fx.shift(timedelta(minutes=5))
    await say(fx, "Great find, thanks.", "t-w", "owner-2")
    repeat = empty = None
    for week, summary in enumerate([report("QX-41", "tidal energy"),
                                    "Nothing new on tidal energy this week; no new studies or updates were found."]):
        fx.shift(timedelta(days=8))
        fx.owner_spoke()
        done = await fx.research("tidal energy", summary)
        await fx.tick()
        if week == 0:
            repeat = done
        else:
            empty = done
    assert [r.id for r in fx.outreach_rows("outreach_finding")] == [row.id], "the same item and a null report stay home"
    assert fx.store.get(repeat.id).result_metadata["outreach"]["state"] == "repeat"
    assert fx.store.get(empty.id).result_metadata["outreach"]["state"] == "empty"
    fx.mind.digest_hour = fx.now.astimezone(fx.mind.tz).hour
    fx.shift(timedelta(days=3))
    await fx.tick()
    assert not any("QX-41" in p["text"] or "Nothing new" in p["text"] for p in fx.sent if p["type"] == "digest")
    # A new item on the same topic a week later still goes.
    fx.shift(timedelta(days=5))
    fx.owner_spoke()
    await fx.research("tidal energy", report("RB-17", "tidal energy").replace("A practical study", "New observations"))
    await fx.tick()
    assert len(fx.outreach_rows("outreach_finding")) == 2


async def test_a_memory_match_needs_the_topic_in_one_sentence_of_the_owners(make):
    from test_mind_outreach_reactions import say, shared
    fx = make()
    await shared(fx)
    fx.shift(timedelta(minutes=5))
    await say(fx, "Great find, thanks.", "t-w", "owner-2")
    fx.shift(timedelta(days=2))
    await say(fx, "My python script for the invoices crashed again.", "t-a", "owner-3")
    fx.shift(3 * H)
    await say(fx, "The parcel arrived but the packaging was torn.", "t-b", "owner-3")
    fx.shift(20 * H)
    fx.found("python packaging", report("ZX-11", "python packaging"))
    fx.shift(timedelta(minutes=1))
    await fx.tick()
    assert not [r for r in fx.outreach_rows("outreach_finding") if r.context["topic"] == "python packaging"]
    state = await fx.mind._gather(fx.now)
    finding = outreach.Finding(id="x", type="research", topic="python packaging", slug="python-packaging",
                               summary=report("ZX-11", "python packaging"), completed_at=fx.now)
    assert outreach.relevance(finding, state.outreach)[0] == 0.0


async def test_a_finding_whose_message_the_owners_pause_cancelled_goes_to_the_digest(make):
    from test_mind_outreach_reactions import say
    fx = make()
    await say(fx, "I care a lot about tidal energy.", "t-1")
    done = await fx.research("tidal energy", report("QX-41", "tidal energy"))
    await fx.mind.tick(force=True)
    queued, = fx.outreach_rows()
    await say(fx, "Leave me alone for the rest of today, please.", "t-q", "owner-2")
    assert fx.store.get(queued.id).status == "cancelled"
    assert fx.store.get(done.id).result_metadata["outreach"]["state"] == "digest"
    fx.mind.digest_hour = 8
    fx.shift(timedelta(hours=20))           # 08:00 next day
    await fx.tick()
    fx.shift(timedelta(hours=1))
    await fx.tick()
    digests = [p["text"] for p in fx.sent if p["type"] == "digest"]
    assert len(digests) == 1 and digests[0].count("QX-41") == 1
    assert not [p for p in fx.sent if p["type"] == "outreach_finding"]


async def test_a_finding_whose_message_expired_unsent_is_listed_once(make):
    from test_mind_outreach_reactions import say
    fx = make()
    await say(fx, "I care a lot about tidal energy.", "t-1")
    done = await fx.research("tidal energy", report("QX-41", "tidal energy"))
    await fx.mind.tick(force=True)          # formed, never pulled by the body
    queued, = fx.outreach_rows()
    fx.mind.digest_hour = (fx.now + timedelta(hours=14)).astimezone(fx.mind.tz).hour
    fx.shift(timedelta(hours=13))
    await fx.mind.tick(force=True)
    assert fx.store.get(queued.id).status == "expired"
    assert fx.store.get(done.id).result_metadata["outreach"]["state"] == "digest"
    fx.shift(timedelta(hours=1, minutes=5))
    await fx.tick()
    digest, = [p["text"] for p in fx.sent if p["type"] == "digest"]
    assert digest.count("QX-41") == 1
