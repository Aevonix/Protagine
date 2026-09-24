"""M8's two gate contrasts are real on the rendered dev splits (build plan M8 "Eval gate"; evals 6.1, 6.7).

``full-consolidation`` on mind-memory-1 and ``full-self_narrative`` on mind-self-1 are only
instruments if the nightly faculties act inside an episode and change what its probe sees.
Every scenario of both pinned dev splits (seed 7, three per template) is walked here the way the
paired body walks it: owner turns land in the ledger and their claim and capture jobs run, a
tick is a forced tick (``POST /v1/mind/tick``), a clock advance moves the harness clock (pinned to
start at 12:00 UTC, protocol paired-clock-start-1), a restart builds a new mind over the same
stores, and the probe turn reads what the agent would be shown: the context the host assembles
for the owner's probe session and the self-narrative the plugin renders into that session's
prompt. Only the model is a fake, answering by task. The real mind, consolidation, claim
projection, capture and context assembly run under each arm's own ``mind`` section
(``native_memory_worker.mind_section``).

What it shows: in ``full`` the night's tick runs the consolidation in every scenario and in
``full-consolidation`` in none (its night runs only the lesson stage); the probe's view differs where a night has something to
consolidate (an own action to narrate, a long session to summarise, a contradiction to ask
about) and nowhere else, so which types can separate the arms is known before any pilot.
"""

from __future__ import annotations

import importlib.util
import json
import re
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from onekey import RequestAuthority
from protagine.api.routers import host
from protagine.api.routers import mind as mind_router
from protagine.api.schemas.host import ContextAssembleRequest, HostIdentity, HostMessage, HostTurnContext
from protagine.beliefs.source_projection import SourceClaimProjection
from protagine.commitments.extract import CommitmentExtractor
from protagine.commitments.store import CommitmentStore
from protagine.feedback import TypeFeedbackStore
from protagine.initiatives.store import InitiativeStore
from protagine.mind import Mind
from protagine.mind.consolidate import TASK_DIGEST, TASK_EPISODE, TASK_NARRATIVE
from protagine.qualification import paired_body
from protagine.qualification.native_memory_worker import mind_clock, mind_section
from protagine.self_model.expectations import ExpectationEngine, ExpectationStore
from protagine.turns.idempotency import TurnIdempotencyLedger

# These drive the paired worker in-process, and the worker runs inside Hermes. The sidecar's own test run has
# no Hermes and skips them; CI runs them in a second step with stock Hermes installed.
needs_hermes = pytest.mark.skipif(importlib.util.find_spec("hermes_time") is None,
                                  reason="needs stock Hermes in the test interpreter")


GENERATORS = Path(__file__).resolve().parents[2] / "benchmarks" / "paired" / "generators"
OWNER = "p-00"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The two conflicting statements of contradiction-ask, the only claims these fakes extract.
PICKUP = [re.compile(r"^(?P<who>p-\d\d) comes for the (?P<item>.+?) at (?P<time>\d\d:\d\d)\."),
          re.compile(r"^The (?P<item>.+?) pickup with (?P<who>p-\d\d) is at (?P<time>\d\d:\d\d)\.")]
# self-report-after-action's promise, the only commitment these fakes capture.
PROMISE = re.compile(r"(?:I told (?P<a>p-\d\d) I would send the (?P<item_a>.+?) within the next|"
                     r"I promised (?P<b>p-\d\d) the (?P<item_b>.+?) in the next) (?P<minutes>\d+) minutes")
SUMMARY = "Session summary:"
ARMS = {"full": {"full": True}, "full-consolidation": {"full": True, "minus_consolidation": True},
        "full-self_narrative": {"full": True, "minus_self_narrative": True}}


def dev_split(name):
    """The pinned dev split: seed 7, three scenarios per template (benchmarks/paired/generators/README.md)."""
    spec = importlib.util.spec_from_file_location("m8_contrast_generate", GENERATORS / "generate.py")
    generate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generate)
    return generate.render(generate.load_templates(GENERATORS / name), 7, 3)


class EpisodeModel:
    """The one model endpoint of an arm, answering each task the served sidecar and mind ask of it."""

    supports_function_routing = True

    def __init__(self):
        self.tasks = []

    def function_deadline_seconds(self, **_):
        return 20

    def tier_config(self, tier):
        return SimpleNamespace(base_url="http://127.0.0.1:8080/v1")      # a local endpoint: extraction runs

    async def complete(self, messages, *, context=None, **_):
        task = (context or {}).get("task")
        self.tasks.append(task)
        prompt = messages[-1]["content"]
        if task == "source_claim_extraction":
            answer = self._claims(json.loads(prompt)["message"])
        elif task == "source_claim_review":
            answer = {str(row["index"]): {"keep": True, "reason": "The fixture statement is source-grounded."}
                      for row in json.loads(prompt)["proposals"]}
        elif task == "commitment_extract":
            answer = self._promise(prompt)
        elif task == TASK_NARRATIVE:
            ids = list(dict.fromkeys(UUID.findall(prompt)))
            answer = {"lines": [{"text": "I sent the owner the reminder they asked for", "cites": [ident]}
                                for ident in ids[:2]]}
        elif task == TASK_EPISODE:
            said = [line[len("They said: "):] for line in prompt.splitlines() if line.startswith("They said: ")]
            answer = {"summary": f"{SUMMARY} " + " ".join(said)[:600]}
        elif task == TASK_DIGEST:
            answer = {"digest": "", "sources": []}
        else:
            answer = {}                          # deliberation falls back to its template
        return SimpleNamespace(content=json.dumps(answer), usage={"total_tokens": 50}, model_id="fixture",
                               raw=None, binding="fixture", config_revision="r1", model_revision="w1")

    @staticmethod
    def _claims(message):
        for pattern in PICKUP:
            found = pattern.search(message)
            if found:
                return [{"subject": found["who"], "predicate": f"{found['item']} pickup time", "value": found["time"],
                         "evidence": message.split(" Nothing")[0], "operation": "assert", "prior_claim_id": None,
                         "memory_kind": "personal_context", "recall_reason": "When the owner asks about the pickup.",
                         "valid_from_text": None, "valid_to_text": None, "event_at_text": None}]
        return []

    @staticmethod
    def _promise(prompt):
        found = PROMISE.search(prompt)
        if not found:
            return []
        said = re.search(r"^Turn time: (\S+)", prompt, re.M)      # the promise runs from when it was made
        start = datetime.fromisoformat(said[1].replace("Z", "+00:00")) if said else mind_clock()
        due = start + timedelta(minutes=int(found["minutes"]))
        return [{"description": f"Send {found['a'] or found['b']} the {found['item_a'] or found['item_b']}",
                 "due_at": due.isoformat(), "priority": 70, "source_type": "cognition", "metadata": None,
                 "obligor": None}]


def _request(person):
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"",
                       "server": ("test", 80), "client": ("127.0.0.1", 1), "scheme": "http"})
    request.state.protagine_authority = RequestAuthority(
        principal_id="hermes-text", credential_id="current", scopes=frozenset(("context:read",)),
        viewer_person_id=person, person_ids=frozenset((person,)), audiences=frozenset(("viewer",)),
        authenticated=True)
    return request


class Episode:
    """One scenario under one arm, walked the way the paired body walks it."""

    def __init__(self, root: Path, scenario, arm):
        self.state = root
        self.state.mkdir(parents=True)
        self.section = mind_section(ARMS[arm])
        self.model = EpisodeModel()
        self.store = InitiativeStore(state_dir=self.state)
        self.commitments = CommitmentStore(self.state / "protagine-commitments.db")
        self.ledger = TurnIdempotencyLedger(self.state / "turn-idempotency.db")
        self.projection = SourceClaimProjection(self.ledger)
        self.scenario = scenario
        self.nights = []
        self.mind = self.build()

    def build(self):
        mind = Mind(config=self.section, store=self.store, state_dir=self.state, owner_id=OWNER,
                    commitments=self.commitments,
                    feedback=TypeFeedbackStore(db_path=str(self.state / "protagine-feedback.db")),
                    expectations=ExpectationEngine(ExpectationStore(str(self.state / "protagine-expectations.db"))),
                    contacts=None, ledger=self.ledger, clock=mind_clock, backups=False, router=self.model,
                    capture=CommitmentExtractor(self.ledger, lambda: self.commitments))
        mind_router.set_mind(mind)
        return mind

    async def turn(self, index, entry):
        if "inbound" in entry:
            inbound = entry["inbound"]
            contact, session, text = inbound["contact"], entry["session_id"], inbound["text"]
            reply = "Yes, it is still on."
        else:
            contact, session, text, reply = OWNER, entry["session_id"], entry["user"], "Noted."
        self.ledger.record_source(f"{self.scenario['id']}:{index}", contact_id=contact, session_id=session,
                                  messages=[{"role": "user", "content": text}, {"role": "assistant", "content": reply}],
                                  occurred_at=mind_clock().isoformat())
        while await self.projection.process_one(self.model):             # the source worker, between turns
            pass

    async def run(self):
        episodes = self.scenario["episodes"]
        restarts = set((self.scenario.get("workflow") or {}).get("restart_before") or [])
        for index, entry in enumerate(episodes[:-1]):
            if index in restarts:
                self.mind = self.build()                                   # a new process, the same stores
            if "advance_clock" in entry:
                paired_body.advance_clock(entry["advance_clock"])
            elif "tick" in entry:
                for _ in range(int(entry["tick"])):
                    summary = await self.mind.tick(force=True)
                    if summary.get("consolidation"):
                        self.nights.append((index, summary["consolidation"]))
            else:
                await self.turn(index, entry)
        if len(episodes) - 1 in restarts:
            self.mind = self.build()
        probe = episodes[-1]
        body = ContextAssembleRequest(
            identity=HostIdentity(host_id="hermes"),
            context=HostTurnContext(contact_id=OWNER, session_id=probe["session_id"], channel_id="cli:owner"),
            incoming_message=HostMessage(role="user", content=probe["user"]))
        response = await host.context_assemble(body, request=_request(OWNER))
        return {"nights": list(self.nights), "sections": {section.id: section.body for section in response.sections},
                "narrative": self.mind.narrative()["text"],       # what the plugin renders in the owner's session
                "notes": [row for row in self.store.intentions(kind=["note"], limit=50) if row.type == "consolidation"],
                "questions": [row for row in self.store.intentions(kind=["message"], limit=50)
                              if row.type == "contradiction"],
                "actions": [(row.kind, row.type, row.id, row.decision, row.status)
                            for row in self.store.intentions(limit=200) if row.kind != "note"]}

    def close(self):
        mind_router.set_mind(None)
        self.store.close()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    monkeypatch.setenv("PROTAGINE_RECALL_RERANK", "off")
    monkeypatch.setenv("PROTAGINE_EMBED_PROVIDER", "skip")
    monkeypatch.setattr(host, "_contacts_store", None)
    views = {}

    async def walk(family, arms):
        for scenario in dev_split(family):
            for arm in arms:
                root = tmp_path / arm / scenario["id"]
                monkeypatch.setenv("PROTAGINE_STATE_DIR", str(root))
                # Each episode starts on the harness clock: the next 12:00 UTC (paired-clock-start-1).
                paired_body.install_clock(paired_body.start_offset("12:00"))
                episode = Episode(root, scenario, arm)
                monkeypatch.setattr(host, "_commitment_store", episode.commitments)
                try:
                    views[(scenario["id"], arm)] = {"scenario": scenario["scenario"], "episodes": scenario["episodes"],
                                                    **await episode.run()}
                finally:
                    episode.close()
                    paired_body.uninstall_clock()
        return views

    yield walk
    paired_body.uninstall_clock()
    mind_router.set_mind(None)


STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?")


def probe_view(view):
    """What the probe turn is shown: the assembled context and the narrative in its prompt. Clock readings
    are left out (each arm's episode starts on the harness clock at a slightly different real second)."""
    sections = {key: STAMP.sub("<t>", body) for key, body in view["sections"].items() if key != "temporal-context"}
    return sections, view["narrative"]


def differing(views, left, right):
    ids = sorted({ident for ident, _ in views})
    return {views[(ident, left)]["scenario"] for ident in ids
            if probe_view(views[(ident, left)]) != probe_view(views[(ident, right)])}


@needs_hermes
async def test_consolidation_runs_inside_every_memory_episode_and_changes_what_its_probe_sees(harness):
    views = await harness("memory.py", ("full", "full-consolidation"))
    scenarios = sorted({ident for ident, _ in views})
    assert len(scenarios) == 24
    for ident in scenarios:
        full, ablated = views[(ident, "full")], views[(ident, "full-consolidation")]
        # The night's tick, right before the probe, ran it inline and to the end, once; never in the ablation,
        # whose night runs only the lesson stage (faculties.lessons stays on in full-consolidation, M9).
        assert full["nights"] == [(len(full["episodes"]) - 2, "done")], (ident, full["nights"])
        assert len(full["notes"]) == 1 and full["notes"][0].status == "done", ident
        assert "episodes" in full["notes"][0].context["done"], ident
        assert [note.context["done"] for note in ablated["notes"]] == [["lessons"]], ident
    # What the night changes for the probe: the summary of a long owner session, recalled in the probe
    # session, and the question the mind put to the owner about two conflicting statements, which its recall
    # then carries. The other types give a night nothing to consolidate (sessions of one or two turns).
    assert differing(views, "full", "full-consolidation") == {"preference-after-distractors", "contradiction-ask"}
    for ident in scenarios:
        full, ablated = views[(ident, "full")], views[(ident, "full-consolidation")]
        memory, other = full["sections"].get("protagine-memory", ""), ablated["sections"].get("protagine-memory", "")
        if full["scenario"] == "preference-after-distractors":
            assert SUMMARY in memory and "episode_summary" in memory and SUMMARY not in other, ident
        if full["scenario"] == "contradiction-ask":
            question, = full["questions"]
            assert question.entity_id == OWNER and question.status == "approved", ident
            assert f"mind:{question.id}:decided_act" in memory and "decided_act" not in other, ident
            assert ablated["questions"] == [], ident
        else:
            assert full["questions"] == [], ident


@needs_hermes
async def test_the_nightly_narrative_changes_what_the_self_probe_sees(harness):
    views = await harness("identity.py", ("full", "full-consolidation", "full-self_narrative"))
    scenarios = sorted({ident for ident, _ in views})
    assert len(scenarios) == 15
    for ident in scenarios:
        full = views[(ident, "full")]
        assert full["nights"] == [(len(full["episodes"]) - 2, "done")] and len(full["notes"]) == 1, ident
        assert len(views[(ident, "full-self_narrative")]["notes"]) == 1, ident   # the night runs, the delta does not
        assert [note.context["done"] for note in views[(ident, "full-consolidation")]["notes"]] == [["lessons"]], ident
        assert views[(ident, "full-self_narrative")]["narrative"] == "", ident
    # The narrative moves with the agent's own actions: self-report-after-action is the type whose episode
    # has one (the overdue-promise reminder formed in its ticks). No other type gives the night anything to
    # narrate yet (stances arrive with M7, strengths need two tasks of one type).
    assert differing(views, "full", "full-self_narrative") == {"self-report-after-action"}
    assert differing(views, "full", "full-consolidation") == {"self-report-after-action"}
    for ident in scenarios:
        full = views[(ident, "full")]
        if full["scenario"] != "self-report-after-action":
            continue
        (kind, type_, reminder, decision, status), = full["actions"]
        assert (kind, type_, decision, status) == ("message", "commitment_reminder", "act", "approved"), ident
        # The fake model claims "sent"; the render says where the row stands: queued for the body.
        assert full["narrative"] == f"recent: I sent the owner the reminder they asked for (queued) [{reminder}]", ident
        ablated = views[(ident, "full-consolidation")]
        assert ablated["narrative"] == "" and [row[1] for row in ablated["actions"]] == ["commitment_reminder"], ident
