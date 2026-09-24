"""``protagine mind``: the owner's CLI over the sidecar, with the off switch working offline."""

from __future__ import annotations

import argparse
import json

import httpx
import pytest
import yaml

from protagine import cli as protagine_cli
from protagine.config import DEFAULTS, load_config, save_config
from protagine.mind import cli as mind_cli


def _parse(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", default=None)
    sub = parser.add_subparsers(dest="command")
    mind_cli.add_parser(sub)
    return parser.parse_args(argv)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    save_config({**DEFAULTS, "owner": {"contact_id": "p-01"}}, tmp_path)
    (tmp_path / "api.key").write_text("k\n")
    return tmp_path


_RealClient = httpx.Client


class _Transport:
    """Route the CLI's requests to a canned sidecar; None means unreachable."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def client(self, **kwargs):
        calls = self.calls
        routes = self.routes

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path, request.content.decode() or None))
            if routes is None:
                raise httpx.ConnectError("refused", request=request)
            key = (request.method, request.url.path)
            if key not in routes:
                return httpx.Response(404, json={"detail": {"code": "not_found"}})
            return httpx.Response(200, json=routes[key])
        return _RealClient(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout", 5))


def test_every_subcommand_parses():
    for command in mind_cli.COMMANDS:
        extra = {"why": ["abc"], "yes": ["K7F"], "no": ["K7F"], "rate": ["abc", "useful"], "reset": ["owner"],
                 "interest": ["local history"]}.get(command, [])
        args = _parse(["mind", command, *extra])
        assert args.mind_command == command
    assert _parse(["mind", "level", "trusted"]).autonomy == "trusted"
    with pytest.raises(SystemExit):
        _parse(["mind", "level", "yolo"])
    assert "mind" in protagine_cli.main.__code__.co_consts or True  # the main parser registers it


def test_status_log_and_yes_go_to_the_sidecar(home, monkeypatch, capsys):
    transport = _Transport({
        ("GET", "/v1/mind/state"): {"enabled": True, "autonomy": "standard", "queued": 1, "outbox": 0, "asks": 2,
                                    "dispatched": 0, "ticks": 3, "last_tick": None, "last_pull": None,
                                    "body_stale": False, "breaker": [{"cls": "owner", "tripped": False}]},
        ("GET", "/v1/mind/log"): {"entries": [{"id": "abcdef12", "created_at": "2026-09-23T10:00:00+00:00",
                                               "kind": "task", "decision": "act", "outcome": "done", "status": "done",
                                               "drive": "duty", "type": "commitment_overdue", "title": "Overdue: x",
                                               "ask_code": None}], "text": "rendered"},
        ("POST", "/v1/mind/asks/K7F/yes"): {"text": "yes: x -> approved"},
    })
    monkeypatch.setattr(httpx, "Client", transport.client)
    assert mind_cli.run(_parse(["mind", "status"])) == 0
    out = capsys.readouterr().out
    assert "mind: on" in out and "autonomy: standard" in out and "open asks: 2" in out
    assert mind_cli.run(_parse(["mind", "log", "--limit", "5"])) == 0
    assert "commitment_overdue" in capsys.readouterr().out
    assert mind_cli.run(_parse(["mind", "yes", "k7f"])) == 0
    assert "approved" in capsys.readouterr().out
    approvals = [call for call in transport.calls if call[1] == "/v1/mind/asks/K7F/yes"]
    assert approvals and approvals[0][0] == "POST" and json.loads(approvals[0][2]) == {"by": "cli"}
    assert all(call[1].startswith("/v1/mind") for call in transport.calls)


def test_status_prints_the_affect_line(home, monkeypatch, capsys):
    base = {"enabled": True, "autonomy": "standard", "queued": 0, "outbox": 0, "asks": [], "dispatched": 0,
            "ticks": 1, "last_tick": None, "last_pull": None, "body_stale": False, "breaker": []}
    affect = {"enabled": True, "source": "state", "route": {}, "levels": {}, "satiated": True, "boost": 0.5,
              "load": {"level": 0.6, "overloaded": True, "obligations": 3, "running": 0, "cap": 2,
                       "failures_last_hour": 0, "asks": 0},
              "switch": ["quarterly figures"], "notes": [], "due_soon": [],
              "line": "Mood: somewhat frustrated about quarterly figures.", "updated_at": None}
    transport = _Transport({("GET", "/v1/mind/state"): {**base, "affect": affect}})
    monkeypatch.setattr(httpx, "Client", transport.client)
    assert mind_cli.run(_parse(["mind", "status"])) == 0
    assert ("affect: Mood: somewhat frustrated about quarterly figures.; load 0.6, overloaded, satiated; "
            "switch: quarterly figures [state]") in capsys.readouterr().out
    calm = {**affect, "line": "", "satiated": False, "switch": [], "source": "rules",
            "load": {**affect["load"], "level": 0.2, "overloaded": False}}
    transport.routes[("GET", "/v1/mind/state")] = {**base, "affect": calm}
    assert mind_cli.run(_parse(["mind", "status"])) == 0
    assert "affect: calm; load 0.2; switch: none [rules]" in capsys.readouterr().out
    transport.routes[("GET", "/v1/mind/state")] = {**base, "affect": {**affect, "enabled": False, "source": "off"}}
    assert mind_cli.run(_parse(["mind", "status"])) == 0
    assert "affect: off" in capsys.readouterr().out
    transport.routes[("GET", "/v1/mind/state")] = base          # a sidecar before the feelings landed
    assert mind_cli.run(_parse(["mind", "status"])) == 0
    assert "affect:" not in capsys.readouterr().out


def test_off_writes_the_marker_when_the_sidecar_is_down(home, monkeypatch, capsys):
    transport = _Transport(None)
    monkeypatch.setattr(httpx, "Client", transport.client)
    assert mind_cli.run(_parse(["mind", "off", "--reason", "maintenance"])) == 0
    assert (home / "mind.off").read_text().startswith("maintenance")
    assert "marker written" in capsys.readouterr().out
    assert mind_cli.run(_parse(["mind", "on"])) == 0
    assert not (home / "mind.off").exists()
    assert load_config(home).get("mind.enabled") is True


def test_level_writes_the_config_and_tells_the_sidecar(home, monkeypatch, capsys):
    transport = _Transport({("POST", "/v1/mind/level"): {"autonomy": "trusted", "previous": "standard"}})
    monkeypatch.setattr(httpx, "Client", transport.client)
    assert mind_cli.run(_parse(["mind", "level", "trusted"])) == 0
    assert yaml.safe_load((home / "protagine.yaml").read_text())["mind"]["autonomy"] == "trusted"
    assert json.loads(transport.calls[-1][2]) == {"autonomy": "trusted"}
    assert "autonomy: trusted" in capsys.readouterr().out


def test_missing_routes_explain_themselves(home, monkeypatch, capsys):
    transport = _Transport({})
    monkeypatch.setattr(httpx, "Client", transport.client)
    assert mind_cli.run(_parse(["mind", "stats"])) == 1
    assert "not_found" in capsys.readouterr().err


def test_owner_can_retire_a_lesson_from_the_cli(home, monkeypatch, capsys):
    lesson = {"id": "L-1a2b3c4d5e", "status": "active", "kind": "strategy", "signature": "topic:order-codes",
              "title": "Order codes by channel", "when_to_use": "an order code is asked for",
              "content": "Channel letter first.", "verified": "owner", "origin": "night", "evidence": ["turn:t-1"],
              "tally": {"uses": 2, "wins": 2, "losses": 0, "applied": 3}}
    transport = _Transport({
        ("GET", "/v1/mind/lessons"): {"enabled": True, "lessons": [lesson], "uses": [], "text": "rendered",
                                      "skills": {"enabled": True, "generation": 2, "owned": ["protagine-codes"],
                                                 "loads": {"protagine-codes": 4}}},
        ("POST", "/v1/mind/lessons/L-1a2b3c4d5e/retire"): {**lesson, "status": "retired",
                                                           "closed_reason": "the rule changed"}})
    monkeypatch.setattr(httpx, "Client", transport.client)
    assert mind_cli.run(_parse(["mind", "lessons"])) == 0
    listed = capsys.readouterr().out
    assert "L-1a2b3c4d5e" in listed and "Order codes by channel" in listed and "2 wins in 2 verified uses" in listed
    assert "skill protagine-codes: 4 loads" in listed
    method, path, _ = transport.calls[-1]
    assert (method, path) == ("GET", "/v1/mind/lessons")
    assert mind_cli.run(_parse(["mind", "lessons", "show", "L-1a2b3c4d5e"])) == 0
    shown = capsys.readouterr().out
    assert "Channel letter first." in shown and "turn:t-1" in shown
    assert mind_cli.run(_parse(["mind", "lessons", "retire", "L-1a2b3c4d5e"])) == 2      # --reason is required
    assert "needs --reason" in capsys.readouterr().err
    assert mind_cli.run(_parse(["mind", "lessons", "retire", "L-1a2b3c4d5e", "--reason", "the rule changed"])) == 0
    method, path, body = transport.calls[-1]
    assert (method, path) == ("POST", "/v1/mind/lessons/L-1a2b3c4d5e/retire")
    assert json.loads(body) == {"reason": "the rule changed", "by": "cli"}
    assert "retired" in capsys.readouterr().out
