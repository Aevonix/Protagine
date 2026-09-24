"""``/v1/mind/opinions`` and ``protagine mind opinions``: audience-filtered reads, owner-only controls."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.routers import mind as mind_router
from protagine.api.routers import opinions as opinions_router
from protagine.mind import cli as mind_cli
from protagine.mind.opinions import Opinions
from protagine.self_model.judgments import Proposal, SelfJudgments
from protagine.turns import TurnIdempotencyLedger

OWNER, GUEST = "p-01", "p-02"


def formed(store, ledger, n, *, topic, text, outcome=None):
    if outcome is not None:
        premises, ref = [store.outcome_premise(outcome)], f"intention:{outcome['id']}"
    else:
        ledger.record_source(f"view-{n}", contact_id=OWNER, session_id=f"s-{n}", derive_claims=False, messages=[
            {"role": "user", "content": f"What do you think about {topic}?"}, {"role": "assistant", "content": text}])
        premises, ref = store.statements(f"view-{n}"), f"turn:view-{n}"
    result = store.form(Proposal(subject_kind="topic", subject="", topic=topic, stance=text, reason="As observed.",
                                 certainty="moderate", revise_if="New figures.", premises=premises, source_ref=ref))
    assert result.disposition == "formed"
    return result.stance_id


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    store = SelfJudgments(ledger, owner_id=OWNER)
    private = formed(store, ledger, 1, topic="garden plan", text="Raised beds suit this garden.")
    shared = formed(store, ledger, 2, topic="garden soil", text="The soil is clay.", outcome={
        "id": "i-07", "outcome": "done", "result": "clay soil", "verified": "check", "completed_at": "2026-09-01"})
    opinions = Opinions(store, None, enabled=True)
    mind_router.set_mind(SimpleNamespace(opinions=opinions))
    app = FastAPI()
    app.include_router(opinions_router.router)
    yield SimpleNamespace(app=app, store=store, opinions=opinions, private=private, shared=shared)
    mind_router.set_mind(None)


async def client_for(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_lists_are_audience_filtered(served):
    async with await client_for(served.app) as client:
        owner = (await client.get("/v1/mind/opinions", params={"contact_id": OWNER})).json()
        cli = (await client.get("/v1/mind/opinions", params={"by": "cli"})).json()
        guest = (await client.get("/v1/mind/opinions", params={"contact_id": GUEST})).json()
        anonymous = (await client.get("/v1/mind/opinions")).json()
        query = (await client.get("/v1/mind/opinions", params={"contact_id": GUEST, "q": "garden plan soil"})).json()
        history = (await client.get("/v1/mind/opinions", params={"by": "cli", "history": "true"})).json()
    assert owner["enabled"] is True and {row["id"] for row in owner["opinions"]} == {served.private, served.shared}
    assert {row["id"] for row in cli["opinions"]} == {served.private, served.shared}
    for view in (guest, anonymous, query):
        assert [row["id"] for row in view["opinions"]] == [served.shared]
    assert {row["id"] for row in history["opinions"]} >= {served.private, served.shared}


async def test_show_is_a_404_for_a_guest_asking_for_an_owner_view(served):
    async with await client_for(served.app) as client:
        hidden = await client.get(f"/v1/mind/opinions/{served.private}", params={"contact_id": GUEST})
        unknown = await client.get("/v1/mind/opinions/9999", params={"by": "cli"})
        shown = await client.get(f"/v1/mind/opinions/{served.private}", params={"contact_id": OWNER})
        public = await client.get(f"/v1/mind/opinions/{served.shared}", params={"contact_id": GUEST})
    assert hidden.status_code == 404 and hidden.json()["detail"]["code"] == "unknown_opinion"
    assert unknown.status_code == 404
    assert shown.status_code == 200 and shown.json()["opinion"]["stance"] == "Raised beds suit this garden."
    assert [row["id"] for row in shown.json()["history"]] == [served.private]
    assert public.status_code == 200 and public.json()["opinion"]["audience"] == "all"


async def test_withdraw_and_reconsider_are_owner_only(served):
    async with await client_for(served.app) as client:
        refused = await client.post(f"/v1/mind/opinions/{served.private}/withdraw",
                                    json={"reason": "Drop it.", "contact_id": GUEST})
        nobody = await client.post(f"/v1/mind/opinions/{served.private}/withdraw", json={"reason": "Drop it."})
        withdrawn = await client.post(f"/v1/mind/opinions/{served.private}/withdraw",
                                      json={"reason": "Drop it.", "contact_id": OWNER, "correction_id": "c-1"})
        replay = await client.post(f"/v1/mind/opinions/{served.private}/withdraw",
                                   json={"reason": "Drop it.", "contact_id": OWNER, "correction_id": "c-1"})
        stale = await client.post(f"/v1/mind/opinions/{served.private}/reconsider",
                                  json={"reason": "Look again.", "by": "cli"})
        reconsidered = await client.post(f"/v1/mind/opinions/{served.shared}/reconsider",
                                         json={"reason": "Look again.", "by": "cli"})
        empty = await client.post(f"/v1/mind/opinions/{served.shared}/withdraw", json={"reason": "", "by": "cli"})
        unknown = await client.post(f"/v1/mind/opinions/{served.shared}/flip", json={"reason": "x", "by": "cli"})
        listed = (await client.get("/v1/mind/opinions", params={"contact_id": OWNER})).json()
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "not_owner"
    assert nobody.status_code == 403
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    assert replay.json() == withdrawn.json()
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "judgment_head_changed"
    assert reconsidered.status_code == 200 and reconsidered.json()["status"] == "reconsidering"
    assert empty.status_code == 422 and unknown.status_code == 404
    assert listed["opinions"] == []
    assert any(row["ref"] == f"reconsider:{reconsidered.json()['revision_id']}" for row in served.store.processing())


async def test_off_and_unwired(served):
    served.opinions.enabled = False
    async with await client_for(served.app) as client:
        assert (await client.get("/v1/mind/opinions", params={"by": "cli"})).json() == {"enabled": False,
                                                                                         "opinions": []}
        mind_router.set_mind(None)
        response = await client.get("/v1/mind/opinions")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "mind_not_wired"


# -- the CLI ---------------------------------------------------------------------------------------

def _parse(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", default=None)
    mind_cli.add_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(argv)


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    from protagine.config import DEFAULTS, save_config
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    save_config({**DEFAULTS, "owner": {"contact_id": OWNER}}, tmp_path)
    (tmp_path / "api.key").write_text("k\n")
    return tmp_path


def sidecar(monkeypatch, routes):
    calls, real = [], httpx.Client

    def client(**kwargs):
        def handler(request):
            calls.append((request.method, request.url.path, dict(request.url.params),
                          json.loads(request.content) if request.content else None))
            key = (request.method, request.url.path)
            return httpx.Response(200, json=routes[key]) if key in routes else httpx.Response(404, json={})
        return real(transport=httpx.MockTransport(handler), timeout=5)
    monkeypatch.setattr(httpx, "Client", client)
    return calls


def test_cli_lists_shows_and_controls_as_the_owner(cli_home, monkeypatch, capsys):
    row = {"id": 4, "topic": "garden plan", "subject_kind": "topic", "subject": "", "stance": "Raised beds.",
           "reason": "Drainage.", "revise_if": "A soil test.", "audience": "owner", "status": "current",
           "premises": [{"kind": "statement", "text": "Raised beds, I think.", "ref": "turn:v#h"}]}
    calls = sidecar(monkeypatch, {
        ("GET", "/v1/mind/opinions"): {"enabled": True, "opinions": [row]},
        ("GET", "/v1/mind/opinions/4"): {"opinion": row, "history": [row]},
        ("POST", "/v1/mind/opinions/4/withdraw"): {"revision_id": 5, "status": "withdrawn"}})
    assert mind_cli.run(_parse(["mind", "opinions"])) == 0
    assert "[4] garden plan: Raised beds." in capsys.readouterr().out
    assert mind_cli.run(_parse(["mind", "opinions", "list", "--query", "garden", "--history"])) == 0
    assert mind_cli.run(_parse(["mind", "opinions", "show", "4"])) == 0
    shown = capsys.readouterr().out
    assert "would change if: A soil test." in shown and "rests on: Raised beds, I think. (turn:v#h)" in shown
    assert mind_cli.run(_parse(["mind", "opinions", "withdraw", "4", "--reason", "Not any more."])) == 0
    assert "opinion 4: withdrawn (revision 5)" in capsys.readouterr().out
    assert calls[0][2] == {"q": "", "history": "false", "by": "cli", "limit": "50"}
    assert calls[1][2]["q"] == "garden" and calls[1][2]["history"] == "true"
    assert calls[2][2] == {"by": "cli"}
    assert calls[3][3] == {"reason": "Not any more.", "by": "cli"}
    assert mind_cli.run(_parse(["mind", "opinions", "reconsider", "4"])) == 2      # a reason is required
    assert mind_cli.run(_parse(["mind", "opinions", "show"])) == 2                  # an id is required
    assert "opinions" in mind_cli.COMMANDS
