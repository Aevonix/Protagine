"""Skills behind the ``skills`` flag (architecture 4.8 item 4, build plan M9).

An active lesson with at least three verified wins and a win rate of at least 0.7 becomes a
``SKILL.md`` in Protagine's own ``skills.external_dirs`` entry (``<state>/skills``). Protagine owns
their lifecycle: a manifest lists what it wrote, a retired or superseded lesson takes its skill with it,
the flag off removes every owned skill and nothing else, and every change bumps a generation the
plugin reads to clear Hermes' skills prompt cache. Loads of its skills are counted.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from protagine.api.routers import mind as mind_router
from protagine.mind import skills as skills_module
from test_mind_loop import OWNER, Fixture

ROOMY = {"breaker": {"failures": 50}, "budgets": {"tasks_per_hour": 50, "concurrent_tasks": 50}}
FIELDS = {"signature": "topic:order-codes", "kind": "strategy", "title": "Order codes by channel",
          "when_to_use": "an order code is asked for", "content": "Channel letter, then the order's last two digits."}


def make(tmp_path, monkeypatch, *, skills=True, lessons=True):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    return Fixture(tmp_path, config={**ROOMY, "faculties": {"skills": skills, "lessons": lessons}})


@pytest.fixture
def fx(tmp_path, monkeypatch):
    fixture = make(tmp_path, monkeypatch)
    yield fixture
    mind_router.set_mind(None)
    fixture.store.close()


def lesson(fx, **fields):
    return fx.mind.lessons.admit({**FIELDS, **fields}, verified="owner", origin="night", status="active",
                                 evidence=[], lineage=[], now=fx.now)


def uses(fx, lesson_id, wins, losses, start=0):
    for n in range(wins + losses):
        row, _ = fx.store.create_intention(kind="task", type="research", title=f"use {start + n}", drive="duty",
                                           cls="internal", decision="act", decision_reason="t", status="done",
                                           dedup_key=f"use-{lesson_id}-{start + n}", created_at=fx.now)
        won = n < wins
        fx.store.update(row.id, lesson_ids=[lesson_id], outcome="done" if won else "failed",
                        verified="owner" if won else "hermes_failure", verdict="useful" if won else None,
                        failed_reason=None if won else "wrong")


def sync(fx):
    return fx.mind.skills.sync(fx.mind.lessons.all(), fx.mind.lessons.tally(fx.now), fx.now)


def skill_dirs(fx):
    root = fx.state / "skills"
    return sorted(path.parent.name for path in root.glob("*/SKILL.md")) if root.exists() else []


def test_a_lesson_with_three_verified_wins_and_seventy_percent_becomes_a_skill(fx):
    item = lesson(fx)
    uses(fx, item.id, wins=3, losses=1)                     # 3 wins, 0.75
    result = sync(fx)
    assert result["written"] == ["protagine-order-codes-by-channel"] and result["removed"] == []
    text = (fx.state / "skills" / "protagine-order-codes-by-channel" / "SKILL.md").read_text()
    assert text.startswith("---\nname: protagine-order-codes-by-channel\ndescription: ")
    import yaml
    _, head, body = text.split("---\n", 2)
    frontmatter = yaml.safe_load(head)
    assert frontmatter["name"] == "protagine-order-codes-by-channel"
    assert frontmatter["description"] == "When an order code is asked for"
    assert "Channel letter, then the order's last two digits." in body and item.id in body
    assert "3 wins in 4 verified uses" in body and "strategy" in body
    assert fx.mind.skills.owned() == {"protagine-order-codes-by-channel": item.id}
    manifest = json.loads((fx.state / "skills" / skills_module.MANIFEST).read_text())
    assert manifest == {"skills": {"protagine-order-codes-by-channel": item.id}}
    state = fx.mind.state()["skills"]
    assert state == {"enabled": True, "generation": 1, "owned": ["protagine-order-codes-by-channel"]}


def test_below_the_rule_no_skill_is_written(fx):
    few = lesson(fx)
    uses(fx, few.id, wins=2, losses=0)                      # two wins are not three
    low = lesson(fx, signature="topic:slot-labels", title="Slot labels", content="Morning is before noon.")
    uses(fx, low.id, wins=3, losses=2, start=10)            # 0.6 is under 0.7
    trial = fx.mind.lessons.admit({**FIELDS, "signature": "topic:bins", "title": "Bins", "content": "Check the bin."},
                                  verified="none", origin="reflector", status="candidate", evidence=[], lineage=[],
                                  now=fx.now)
    uses(fx, trial.id, wins=3, losses=0, start=20)          # a candidate is never promoted
    assert fx.mind.skills.promotable([trial], fx.mind.lessons.tally(fx.now)) == []
    assert sync(fx) == {"written": [], "removed": [], "generation": 0}
    assert skill_dirs(fx) == [] and not (fx.state / "skills").exists()


def test_a_retired_or_superseded_lesson_takes_its_skill_with_it(fx):
    first = lesson(fx)
    second = lesson(fx, signature="topic:slot-labels", title="Slot labels", content="Morning is before noon.")
    for item, start in ((first, 0), (second, 10)):
        uses(fx, item.id, wins=3, losses=0, start=start)
    sync(fx)
    assert skill_dirs(fx) == ["protagine-order-codes-by-channel", "protagine-slot-labels"]
    fx.mind.lessons.retire(first.id, reason="the rule changed")
    replacement = lesson(fx, signature="topic:slot-labels", title="Slot labels", content="Morning ends at eleven.")
    assert fx.mind.lessons.get(second.id).status == "superseded" and replacement.supersedes == second.id
    result = sync(fx)
    assert sorted(result["removed"]) == ["protagine-order-codes-by-channel", "protagine-slot-labels"]
    assert skill_dirs(fx) == [] and fx.mind.skills.owned() == {}


def test_skills_off_removes_every_owned_skill_and_nothing_else(tmp_path, monkeypatch):
    fx = make(tmp_path, monkeypatch)
    item = lesson(fx)
    uses(fx, item.id, wins=3, losses=0)
    sync(fx)
    hand = fx.state / "skills" / "my-own-skill"
    hand.mkdir()
    (hand / "SKILL.md").write_text("---\nname: my-own-skill\ndescription: mine\n---\nkeep me\n")
    other = fx.state / "skills" / "protagine-not-in-the-manifest"
    other.mkdir()
    (other / "SKILL.md").write_text("---\nname: protagine-not-in-the-manifest\ndescription: x\n---\n")
    fx.store.close()
    # Restarted with the flag off: the start-up sync removes what Protagine wrote, and only that.
    off = make(tmp_path, monkeypatch, skills=False)
    assert skill_dirs(off) == ["my-own-skill", "protagine-not-in-the-manifest"]
    assert off.mind.skills.owned() == {} and off.mind.state()["skills"]["enabled"] is False
    off.store.close()
    # Lessons off takes the skills with it too.
    on = make(tmp_path / "lessons-off", monkeypatch)
    item = lesson(on)
    uses(on, item.id, wins=3, losses=0)
    sync(on)
    on.store.close()
    lessons_off = make(tmp_path / "lessons-off", monkeypatch, lessons=False)
    assert skill_dirs(lessons_off) == []
    lessons_off.store.close()


def test_every_change_bumps_the_generation(fx):
    assert fx.mind.skills.generation() == 0
    item = lesson(fx)
    uses(fx, item.id, wins=3, losses=0)
    assert sync(fx)["generation"] == 1
    assert sync(fx)["generation"] == 1                       # nothing changed, nothing bumped
    uses(fx, item.id, wins=1, losses=0, start=50)
    assert sync(fx)["generation"] == 1                       # a better tally rewrites nothing
    fx.mind.lessons.retire(item.id, reason="stale")
    assert sync(fx)["generation"] == 2 and fx.mind.state()["skills"]["generation"] == 2


def test_skill_loads_are_counted(fx):
    item = lesson(fx)
    uses(fx, item.id, wins=3, losses=0)
    sync(fx)
    app = FastAPI()
    app.include_router(mind_router.router)
    mind_router.set_mind(fx.mind)

    async def post(body):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mind") as client:
            return await client.post("/v1/mind/skills/used", json=body)
    first = asyncio.run(post({"skill": "protagine-order-codes-by-channel", "session_id": "day-04"}))
    assert first.status_code == 200 and first.json()["loads"] == 1
    asyncio.run(post({"skill": "protagine-order-codes-by-channel", "task_id": "t-9"}))
    ignored = asyncio.run(post({"skill": "someone-elses-skill", "session_id": "day-04"}))
    assert ignored.json()["counted"] is False
    assert fx.mind.skills.loads() == {"protagine-order-codes-by-channel": 2}
    assert fx.mind.stats()["skills"]["loads"] == {"protagine-order-codes-by-channel": 2}

    async def listed():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mind") as client:
            return (await client.get("/v1/mind/lessons")).json()
    value = asyncio.run(listed())
    assert value["skills"] == {"enabled": True, "generation": 1, "owned": ["protagine-order-codes-by-channel"],
                               "loads": {"protagine-order-codes-by-channel": 2}}
    assert "skill protagine-order-codes-by-channel: 2 loads" in value["text"]


async def test_the_night_ends_by_syncing_the_skills(tmp_path, monkeypatch):
    from test_mind_lessons_night import LESSONS_ONLY, make as night_fixture
    fx = night_fixture(tmp_path, monkeypatch, config={**LESSONS_ONLY, "faculties": {
        **LESSONS_ONLY["faculties"], "skills": True}})
    item = lesson(fx)
    uses(fx, item.id, wins=3, losses=0)
    fx.shift(days=1)
    result = await fx.mind.consolidate()
    assert result["counts"]["skills_written"] == 1 and skill_dirs(fx) == ["protagine-order-codes-by-channel"]
    assert fx.mind.state()["skills"]["generation"] == 1
    fx.store.close()
