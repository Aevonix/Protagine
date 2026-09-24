"""``protagine people``: the owner's CLI over ``/v1/mind/people`` (architecture 7.4, 7.10)."""

from __future__ import annotations

import argparse
import json

import httpx
import pytest

from protagine import cli as protagine_cli
from protagine.config import DEFAULTS, save_config
from protagine.contacts import cli as people_cli


def _parse(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", default=None)
    sub = parser.add_subparsers(dest="command")
    people_cli.add_parser(sub)
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
    """Route the CLI's requests to a canned sidecar."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def client(self, **kwargs):
        calls, routes = self.calls, self.routes

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.raw_path.decode().split("?")[0], dict(request.url.params),
                          json.loads(request.content) if request.content else None))
            key = (request.method, request.url.path)
            if key not in routes:
                return httpx.Response(404, json={"detail": {"code": "unknown_contact", "message": "no such person"}})
            status, body = routes[key] if isinstance(routes[key], tuple) else (200, routes[key])
            return httpx.Response(status, json=body)
        return _RealClient(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout", 5))


def test_every_subcommand_parses_and_the_main_cli_registers_people():
    extra = {"inspect": ["Sam"], "permit": ["Sam", "auto"], "cadence": ["Sam", "90"], "merge": ["a", "b"],
             "link": ["Sam", "sms", "+15550000001"]}
    for command in people_cli.COMMANDS:
        assert _parse(["people", command, *extra.get(command, [])]).people_command == command
    with pytest.raises(SystemExit):
        _parse(["people", "permit", "Sam", "always"])
    parser_source = open(protagine_cli.__file__, encoding="utf-8").read()
    assert "add_people_parser(sub)" in parser_source and 'args.command == "people"' in parser_source


def test_reads_go_to_the_people_routes(home, monkeypatch, capsys):
    transport = _Transport({
        ("GET", "/v1/mind/people"): {"contacts": [{"contact_id": "cid-a", "display_name": "Sam"}], "text": "cid-a  Sam"},
        ("GET", "/v1/mind/people/sms:+1 555 000 0001"): {"contact": {"contact_id": "cid-a"}, "text": "cid-a  Sam\ndigest"},
        ("GET", "/v1/mind/people/proposals"): {"proposals": [], "text": "(no open proposals)"},
    })
    monkeypatch.setattr(httpx, "Client", transport.client)
    assert people_cli.run(_parse(["people", "who", "sam"])) == 0
    assert "cid-a  Sam" in capsys.readouterr().out
    assert people_cli.run(_parse(["people", "inspect", "sms:+1 555 000 0001"])) == 0
    assert "digest" in capsys.readouterr().out
    assert people_cli.run(_parse(["people", "proposals"])) == 0
    assert "no open proposals" in capsys.readouterr().out
    assert people_cli.run(_parse(["people", "--json", "who"])) == 0
    assert json.loads(capsys.readouterr().out) == [{"contact_id": "cid-a", "display_name": "Sam"}]
    who, inspect, _, _ = transport.calls
    assert who[2] == {"q": "sam", "limit": "20"}
    assert inspect[1] == "/v1/mind/people/sms%3A%2B1%20555%20000%200001"  # one path segment, whatever the reference


def test_mutations_say_by_cli(home, monkeypatch, capsys):
    transport = _Transport({
        ("POST", "/v1/mind/people/Sam/permission"): {"ok": True, "may_contact": "auto", "text": "Sam: may_contact=auto"},
        ("POST", "/v1/mind/people/Sam/cadence"): {"ok": True, "cadence_minutes": None, "text": "Sam: no cadence"},
        ("POST", "/v1/mind/people/merge"): {"ok": True, "sources_pending": 2, "text": "merged cid-b into cid-a"},
        ("POST", "/v1/mind/people/link"): {"ok": True, "text": "proposed sms:+15550000001 for Sam; the owner confirms"},
    })
    monkeypatch.setattr(httpx, "Client", transport.client)
    assert people_cli.run(_parse(["people", "permit", "Sam", "auto", "--reason", "old friend"])) == 0
    assert "may_contact=auto" in capsys.readouterr().out
    assert people_cli.run(_parse(["people", "cadence", "Sam", "off"])) == 0
    assert people_cli.run(_parse(["people", "merge", "cid-a", "cid-b"])) == 0
    assert "2 source(s) left" in capsys.readouterr().out
    assert people_cli.run(_parse(["people", "link", "Sam", "sms", "+15550000001"])) == 0
    bodies = [call[3] for call in transport.calls]
    assert bodies == [{"may_contact": "auto", "by": "cli", "reason": "old friend"},
                      {"minutes": None, "by": "cli"},
                      {"keep": "cid-a", "drop": "cid-b", "by": "cli"},
                      {"contact_id": "Sam", "gateway": "sms", "address": "+15550000001", "by": "cli"}]


def test_errors_are_one_line_and_bad_cadence_never_calls_the_sidecar(home, monkeypatch, capsys):
    transport = _Transport({("POST", "/v1/mind/people/Sam/permission"): (
        403, {"detail": {"code": "not_owner", "message": "only the owner can change who may be contacted"}})})
    monkeypatch.setattr(httpx, "Client", transport.client)
    assert people_cli.run(_parse(["people", "inspect", "Nobody"])) == 1
    assert "no such person" in capsys.readouterr().err
    assert people_cli.run(_parse(["people", "permit", "Sam", "never"])) == 1
    assert "only the owner" in capsys.readouterr().err
    calls = len(transport.calls)
    assert people_cli.run(_parse(["people", "cadence", "Sam", "soon"])) == 1
    assert "whole number of minutes" in capsys.readouterr().err
    assert len(transport.calls) == calls
