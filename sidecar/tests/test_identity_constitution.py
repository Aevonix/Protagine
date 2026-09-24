"""The constitution: ``identity.yaml`` ``agent.{name, values, boundaries}`` rendered once, capped, owner-authored
(architecture 4.2, evals 7.2 test 14). Nothing the agent learns can write it."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from protagine import config
from protagine.config import CONSTITUTION_CHARS, constitution_length, render_constitution, save_identity
from protagine.init import InitError, _collect_identity
from protagine.self_model import appraisals
from protagine.turns import TurnIdempotencyLedger

SIDECAR = Path(__file__).resolve().parents[1] / "protagine"
IDENTITY = {"owner": {"name": "Owner", "handles": []},
            "agent": {"name": "Agent", "values": ["care", "candour"],
                      "boundaries": ["never send money", "never contact family members"]}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PROTAGINE_AGENT_VALUES", raising=False)
    return tmp_path


# -- render --------------------------------------------------------------------------------

def test_render_names_values_and_boundaries():
    assert render_constitution(IDENTITY) == ("You are Agent. Your values: care; candour. "
                                             "Your boundaries: never send money; never contact family members.")


def test_render_omits_empty_lists_and_survives_a_bare_identity():
    assert render_constitution({"agent": {"name": "Agent", "values": [], "boundaries": None}}) == "You are Agent."
    assert render_constitution({"agent": {"name": "Agent", "values": ["care"]}}) == "You are Agent. Your values: care."
    assert render_constitution({}) == ""
    assert render_constitution({"agent": "Agent"}) == ""
    # Items are one-line strings of 1 to 160 characters, at most 12 of each, duplicates dropped.
    rendered = render_constitution({"agent": {"name": "Agent", "values": ["a\nb", " care ", "care", "", 7, "x" * 161]
                                              + [f"v{i}" for i in range(20)]}})
    assert rendered.startswith("You are Agent. Your values: a b; care; v0;")
    assert "x" * 161 not in rendered and rendered.count("care") == 1
    assert rendered.count(";") == 11  # 12 items


def test_render_is_capped_at_1500_characters():
    long_identity = {"agent": {"name": "Agent", "values": [f"v{i}".ljust(160, "v") for i in range(12)],
                               "boundaries": [f"b{i}".ljust(160, "b") for i in range(12)]}}
    assert constitution_length(long_identity) > CONSTITUTION_CHARS == 1500
    assert len(render_constitution(long_identity)) == CONSTITUTION_CHARS
    assert constitution_length(IDENTITY) == len(render_constitution(IDENTITY))


# -- init ----------------------------------------------------------------------------------

def _args(**overrides):
    values = {"owner_name": "Owner", "owner_handle": ["telegram=1001"], "agent_name": "Agent",
              "agent_values": "care, candour", "agent_boundaries": "never send money, never contact family members",
              "timezone": "UTC", "quiet_hours": ""}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_init_collects_boundaries_and_keeps_existing_ones_as_defaults():
    identity = _collect_identity(_args(), {}, True)
    assert identity["agent"]["boundaries"] == ["never send money", "never contact family members"]
    assert identity["agent"]["values"] == ["care", "candour"]
    again = _collect_identity(_args(agent_boundaries=None, agent_values=None), identity, True)
    assert again["agent"]["boundaries"] == identity["agent"]["boundaries"]
    assert again["agent"]["values"] == identity["agent"]["values"]
    # Existing keys init does not ask about (interests, the owner's contact id) survive a re-run.
    kept = _collect_identity(_args(), {"owner": {"name": "Owner", "contact_id": "p-01"},
                                       "agent": {"name": "Agent", "interests": ["tides"]}}, True)
    assert kept["owner"]["contact_id"] == "p-01" and kept["agent"]["interests"] == ["tides"]


def test_init_refuses_an_over_long_constitution_and_names_the_list_to_shorten():
    long = lambda letter: ", ".join(f"{letter}{i}".ljust(150, letter) for i in range(12))  # noqa: E731
    with pytest.raises(InitError) as info:
        _collect_identity(_args(agent_boundaries=long("b")), {}, True)
    message = str(info.value)
    assert "1500" in message and "boundaries" in message
    with pytest.raises(InitError) as info:
        _collect_identity(_args(agent_values=long("v"), agent_boundaries=""), {}, True)
    assert "values" in str(info.value)
    # Under the limit it is accepted.
    short = ", ".join(f"v{i}".ljust(100, "v") for i in range(12))
    assert _collect_identity(_args(agent_values=short, agent_boundaries=""), {}, True)["agent"]["values"]


# -- values reach the appraisal prompt --------------------------------------------------------

def test_chosen_values_come_from_identity_yaml_only(home, monkeypatch):
    """identity.yaml replaced PROTAGINE_AGENT_VALUES (init moves an old unit's values into the file); the
    variable stays reserved, and nothing reads it."""
    assert appraisals.chosen_values() == []
    monkeypatch.setenv("PROTAGINE_AGENT_VALUES", json.dumps(["Be candid"]))
    assert appraisals.chosen_values() == []
    save_identity({"owner": {"name": "Owner"}, "agent": {"name": "Agent", "values": ["candour", " care ", "candour", ""]}},
                  home)
    assert appraisals.chosen_values() == ["candour", "care"]
    assert "PROTAGINE_AGENT_VALUES" in config.RESERVED_ENVIRONMENT


class _Processor:
    supports_function_routing = True

    def __init__(self):
        self.requests = []

    def function_deadline_seconds(self, **kwargs):
        return 20

    async def complete(self, *, messages, context):
        payload = json.loads(messages[-1]["content"])
        self.requests.append((payload, context))
        return SimpleNamespace(content=json.dumps({"observations": [], "incident_decisions": []}), raw=None,
                               model_id="fixture", binding="fixture", config_revision="r1", model_revision="w1")


def test_the_constitution_is_an_input_of_the_appraisal_prompt_never_an_output(home, tmp_path):
    identity = {"owner": {"name": "Owner"}, "agent": {"name": "Agent", "values": ["candour"],
                                                      "boundaries": ["never send money"]}}
    save_identity(identity, home)
    store = appraisals.AppraisalStore(TurnIdempotencyLedger(tmp_path / "sources.db"), owner_id="p-01",
                                      clock=lambda: 1800000000.0)
    messages = [{"role": "user", "content": "The export has failed again after the same retry."}]
    store.ledger.record_source("t-1", contact_id="p-02", session_id="s-1", scope="person", messages=messages,
                               occurred_at="2027-01-01T00:00:00+00:00")
    with store.ledger._connect() as conn, conn:
        appraisals.enqueue(conn, "t-1", "p-02", messages, scope="person")
    processor = _Processor()
    assert asyncio.run(store.process_one(processor)) is True
    (payload, context), = processor.requests
    assert payload["agent_constitution"] == render_constitution(identity) == (
        "You are Agent. Your values: candour. Your boundaries: never send money.")
    assert "chosen_values" not in payload and "agent_values" not in payload
    assert "agent_constitution" not in json.dumps(context["response_schema"])
    assert "agent_constitution" in appraisals.SYSTEM


# -- the constitution has no writer among the learning paths ---------------------------------

def test_no_learning_path_writes_the_constitution():
    """The only writers of identity.yaml are init and the owner's CLI; the mind's ``persist`` hook
    touches ``mind.enabled`` and ``mind.autonomy`` in protagine.yaml and nothing else."""
    identity_writers = {"save_identity", "identity_path", "IDENTITY_FILE"}
    config_writers = {"save_config", "update_config"}
    offenders = []
    for directory in ("mind", "self_model", "beliefs", "memory", "commitments"):
        for path in sorted((SIDECAR / directory).rglob("*.py")):
            relative = path.relative_to(SIDECAR).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {node.body[0].value for node in ast.walk(tree)
                          if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                          and node.body and isinstance(node.body[0], ast.Expr)
                          and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)}
            for node in ast.walk(tree):
                name = (node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute)
                        else node.name if isinstance(node, ast.alias) else None)
                if name in identity_writers or (name in config_writers and relative != "mind/cli.py"):
                    offenders.append(f"{relative}:{node.lineno}: {name}")
                if (isinstance(node, ast.Constant) and isinstance(node.value, str) and "identity.yaml" in node.value
                        and node not in docstrings):
                    offenders.append(f"{relative}:{node.lineno}: {node.value!r}")
    assert offenders == []
    # The owner's CLI writes protagine.yaml on the owner's command (level, on); never identity.yaml.
    cli = (SIDECAR / "mind" / "cli.py").read_text(encoding="utf-8")
    assert re.findall(r"update_config\(\{\"mind\": \{\"(\w+)\"", cli) == ["autonomy", "enabled"]
    tick = (SIDECAR / "mind" / "tick.py").read_text(encoding="utf-8")
    persists = re.findall(r"self\.persist\((.*)\)", tick)
    assert persists == ['{"mind": {"enabled": True}}', '{"mind": {"autonomy": level}}']


def test_render_helpers_are_exported():
    assert config.CONSTITUTION_CHARS == 1500
    assert callable(config.constitution_list)


def test_the_adapter_renders_the_same_constitution_as_the_sidecar():
    """The plugin cannot import the sidecar, so it carries the same render; the two must not drift."""
    import importlib
    import importlib.util
    import sys
    package = "hermes_plugin_constitution_under_test"
    if package not in sys.modules:
        location = Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin"
        spec = importlib.util.spec_from_loader(package, loader=None, is_package=True)
        module = importlib.util.module_from_spec(spec)
        module.__path__ = [str(location)]
        sys.modules[package] = module
    client = importlib.import_module(f"{package}.client")
    assert client.CONSTITUTION_CHARS == CONSTITUTION_CHARS
    long_identity = {"agent": {"name": "Agent", "values": [f"v{i}".ljust(160, "v") for i in range(14)],
                               "boundaries": [f"b{i}".ljust(160, "b") for i in range(3)] + ["", "x\ny", 3]}}
    for identity in (IDENTITY, long_identity, {}, {"agent": {"name": " Agent  Two "}}):
        assert client.render_constitution(identity) == render_constitution(identity)
    settings = client.Settings(sidecar_url="http://127.0.0.1:1", key_file=Path("/nonexistent/api.key"), api_key="",
                               home=Path("/nonexistent"), hermes_home=Path("/nonexistent"),
                               outbox_path=Path("/nonexistent/outbox.sqlite3"))
    assert settings.constitution() == ""



def _adapter_client():
    """The adapter's client module, loaded from its source: the adapter never imports the sidecar, so it
    carries its own copy of the render, and this is where the two are held to one output."""
    import importlib.util
    import sys
    name = "protagine_adapter_client_under_test"
    if name not in sys.modules:
        path = SIDECAR.parents[1] / "plugins" / "hermes-plugin" / "client.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module          # dataclasses resolve their module through sys.modules
        spec.loader.exec_module(module)
    return sys.modules[name]


@pytest.mark.parametrize("identity", [
    IDENTITY,
    {"agent": {"name": " Agent\n the\tSecond ", "values": ["care", "care", " candour ", 7, "", "x" * 161],
               "boundaries": [f"never {i}" for i in range(20)]}},
    {"agent": {"name": "Agent", "values": ["v" * 160] * 1 + [f"{i}".ljust(150, "w") for i in range(12)],
               "boundaries": [f"{i}".ljust(160, "b") for i in range(12)]}},
    {"agent": {"values": "care"}},
    {"agent": None},
    {},
    None,
], ids=["plain", "messy", "over-long", "values-not-a-list", "no-agent", "empty", "none"])
def test_the_adapter_renders_the_constitution_exactly_as_the_sidecar_does(identity):
    adapter = _adapter_client()
    assert adapter.CONSTITUTION_CHARS == CONSTITUTION_CHARS
    assert adapter.render_constitution(identity) == render_constitution(identity)
    assert adapter.render_constitution(identity, limit=None) == render_constitution(identity, limit=None)
