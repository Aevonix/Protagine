"""M8 deletions: the graph memory, the world model, the chain and the continuous learner are gone.

Nothing imports them, no route serves them, no dependency pulls them in, and the
state files they left behind move into the upgrade backup.
"""

from importlib.util import find_spec
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "protagine"

RETIRED_MODULES = (
    "protagine.intelligence.graph",
    "protagine.world_model",
    "protagine.chain",
    "protagine.intelligence.learning.continuous_learner",
    # The graph-bound belief maintenance; the source-claim pipeline stays.
    "protagine.beliefs.engine",
    "protagine.beliefs.store",
    "protagine.beliefs.models",
    "protagine.beliefs.contradictions",
    "protagine.beliefs.resolve",
    "protagine.beliefs.decay",
    # Graph-only code nothing could construct or feed once the graph went.
    "protagine.intelligence.mind_model.graph_baseline",
    "protagine.sessions.context_loader",
)
RETIRED_ROUTE_PREFIXES = ("/v1/host/world", "/v1/host/world-model", "/v1/host/beliefs",
                          "/v1/host/identity", "/v1/host/chain")
RETIRED_ROUTES = ("/v1/host/learning/weights", "/v1/host/learning/engagement")
# The contact store still labels rows it once received from the world model; a
# quoted "world_model" is that import_source value, never a module path. The
# upgrade's retired-state list names the world model's old file, quoted.
ALLOWED_LITERAL = re.compile(r"""["'](?:world_model|protagine_world_model\.db)["']""")


def absent(module: str) -> bool:
    """A module is absent when nothing resolves it, including when its parent package is gone (find_spec
    then raises instead of returning None)."""
    try:
        return find_spec(module) is None
    except ModuleNotFoundError:
        return True


@pytest.mark.parametrize("module", RETIRED_MODULES)
def test_retired_modules_do_not_exist(module):
    assert absent(module), f"{module} still exists"


def test_research_gathers_from_no_graph():
    """The research pipeline's graph stage had no client to query once the graph went: it is gone."""
    import inspect
    from protagine.research import gatherer, pipeline, synthesizer
    assert not hasattr(gatherer, "GraphGatherer") and "GRAPH" not in gatherer.SourceType.__members__
    assert not {"enable_graph", "max_graph_depth"} & set(gatherer.GatherConfig.__dataclass_fields__)
    assert "graph" not in inspect.signature(pipeline.ResearchPipeline).parameters
    assert "graph" not in inspect.signature(gatherer.SourceGatherer).parameters
    assert "graph_entities_referenced" not in synthesizer.SynthesisReport.__dataclass_fields__


def test_the_consolidate_help_names_the_stages_that_run():
    from protagine.mind.consolidate import NIGHT_TASKS
    source = (PACKAGE / "mind" / "cli.py").read_text()
    assert "dedupe" not in source and set(NIGHT_TASKS) == {"narrative", "lessons", "contradictions", "digests",
                                                           "episodes"}
    assert "lessons" in source.split('"consolidate", help=', 1)[1].split(")", 1)[0]

def test_no_retired_route_is_served():
    from protagine.server import create_app

    # The OpenAPI table lists every included router's paths (app.routes nests them).
    paths = set(create_app().openapi().get("paths", {}))
    assert "/v1/host/context/assemble" in paths and "/v1/host/learning/correction" in paths
    served = sorted(path for path in paths
                    if path.startswith(RETIRED_ROUTE_PREFIXES) or path in RETIRED_ROUTES)
    assert served == []


def test_health_capabilities_never_advertise_retired_subsystems():
    from protagine.api.routers.host import supported_capabilities

    assert not {"consolidate", "world_model", "world_model_api", "identity", "learning"} & set(
        supported_capabilities())


def test_neo4j_is_not_a_dependency():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "neo4j" not in pyproject.lower()
    assert "[graph" not in pyproject and "graph = [" not in pyproject


def test_source_tree_has_no_retired_references():
    pattern = re.compile(r"world_model|intelligence\.graph|protagine\.chain|continuous_learner|NEO4J"
                         r"|beliefs\.(engine|store|models|contradictions|resolve|decay)\b")
    hits = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "identity_bootstrap" in path.parts:
            continue  # M5 deletes the bootstrap package
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line) and not ALLOWED_LITERAL.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert hits == []


def test_no_client_calls_a_retired_route():
    """Plugins, scripts, benchmarks, the MCP server and the live-server e2e suites call only served routes."""
    repo = ROOT.parent
    route = re.compile(r"/v1/host/(world|world-model|beliefs|identity|chain)\b|/v1/host/learning/(weights|engagement)")
    roots = [repo / "plugins", repo / "scripts", repo / "benchmarks", repo / "tests",
             ROOT / "scripts", ROOT / "tests" / "e2e", PACKAGE / "mcp"]
    hits = []
    for base in roots:
        for path in sorted(base.rglob("*.py")) if base.is_dir() else ():
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if route.search(line):
                    hits.append(f"{path.relative_to(repo)}:{number}: {line.strip()}")
    assert hits == []


def test_source_tree_imports_no_neo4j_at_import_time():
    script = (
        "import importlib.abc, sys\n"
        "class NoNeo4j(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'neo4j' or name.startswith('neo4j.'):\n"
        "            raise AssertionError('neo4j imported: ' + name)\n"
        "sys.meta_path.insert(0, NoNeo4j())\n"
        "import protagine.server, protagine.backup, protagine.cli\n"
        "print('OK')\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            cwd=str(ROOT), timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


@pytest.mark.parametrize("argv", [
    ["key", "info"],
    ["node", "info"],
    ["backup", "--no-graph"],
    ["restore", "--force-identity", "--input", "archive.tar.gz"],
], ids=["key", "node", "backup-no-graph", "restore-force-identity"])
def test_cli_has_no_chain_or_graph_commands(argv, monkeypatch, capsys):
    from protagine import cli

    monkeypatch.setattr(sys, "argv", ["protagine", *argv])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    assert "error" in capsys.readouterr().err


def test_plain_backup_is_the_full_instance_archive(tmp_path, monkeypatch, capsys):
    """The chain's identity-only JSON export is gone: every backup is the full archive."""
    import json
    import tarfile

    from protagine import cli

    state = tmp_path / "state"
    state.mkdir()
    (state / "instance-id").write_text("instance-7\n")
    (state / "identity.yaml").write_text("agent: {name: Agent}\n")
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(state))
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.setattr(sys, "argv", ["protagine", "backup", "--no-vectors", "--output", str(tmp_path / "out")])

    cli.main()

    assert "Full backup saved" in capsys.readouterr().out
    archive, = (tmp_path / "out").glob("protagine-backup-*.tar.gz")
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        meta = json.load(tar.extractfile(next(n for n in names if n.endswith("meta.json"))))
    assert meta["instance_id"] == "instance-7"
    assert any(name.endswith("identity/identity.yaml") for name in names)
    assert not any("protagine-keys" in name or "graph" in name for name in names)


def test_instance_id_is_created_once_and_adopts_the_chain_id(tmp_path):
    from protagine.instance import INSTANCE_ID_FILE, instance_id, read_instance_id

    fresh = tmp_path / "fresh"
    assert read_instance_id(fresh) == ""
    first = instance_id(fresh)
    assert first and instance_id(fresh) == first == read_instance_id(fresh)
    assert (fresh / INSTANCE_ID_FILE).stat().st_mode & 0o077 == 0

    upgraded = tmp_path / "upgraded"
    upgraded.mkdir()
    (upgraded / "protagine-id").write_text("11111111-2222-3333-4444-555555555555\n")
    # Before the upgrade the chain's file still names the instance; reading creates nothing.
    assert read_instance_id(upgraded) == "11111111-2222-3333-4444-555555555555"
    assert not (upgraded / INSTANCE_ID_FILE).exists()
    assert instance_id(upgraded) == "11111111-2222-3333-4444-555555555555"
    assert (upgraded / INSTANCE_ID_FILE).read_text().strip() == "11111111-2222-3333-4444-555555555555"


M8_RETIRED_STATE = ("protagine-beliefs.db", "chain.db", "protagine-id", "node-id", "node-cert.json", "genesis.json",
                    "protagine-manifest.json", "protagine_world_model.db", "protagine-keys", "node-keys")


def test_retire_state_moves_every_retired_file_and_keeps_the_instance_id(tmp_path):
    from protagine.init import RETIRED_STATE as ALL_RETIRED, retire_state, retired_state_present

    RETIRED_STATE = M8_RETIRED_STATE
    assert set(RETIRED_STATE) <= set(ALL_RETIRED)          # the upgrade's one list of retired state
    home = tmp_path / "home"
    home.mkdir()
    for name in ("protagine-beliefs.db", "chain.db", "protagine_world_model.db"):
        sqlite3.connect(home / name).execute("CREATE TABLE t (x)").connection.close()
    (home / "chain.db-wal").write_bytes(b"")
    (home / "protagine-id").write_text("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n")
    (home / "node-id").write_text("node-1\n")
    (home / "node-cert.json").write_text("{}")
    (home / "genesis.json").write_text("{}")
    (home / "protagine-manifest.json").write_text("{}")
    for directory in ("protagine-keys", "node-keys"):
        (home / directory).mkdir()
        (home / directory / "private.pem").write_text("key")
    (home / "protagine.yaml").write_text("mind: {}\n")
    (home / "turn-idempotency.db").write_bytes(b"")
    backup = tmp_path / "backups" / "stamp"

    assert sorted(retired_state_present(home)) == sorted(RETIRED_STATE)
    notes = retire_state(home, backup)

    assert len(notes) == len(RETIRED_STATE) and all(note.startswith("retired ") for note in notes)
    assert retired_state_present(home) == []
    assert sorted(path.name for path in (backup / "retired").iterdir()) == sorted(
        RETIRED_STATE + ("chain.db-wal",))
    assert (backup / "retired" / "protagine-keys" / "private.pem").read_text() == "key"
    assert (home / "instance-id").read_text().strip() == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert (home / "protagine.yaml").exists() and (home / "turn-idempotency.db").exists()
    # A clean state directory is left alone: no backup directory, no notes.
    assert retire_state(home, tmp_path / "second") == []
    assert not (tmp_path / "second").exists()


def test_the_graph_only_leftovers_are_gone():
    """What only the graph, the world model's extraction or the chain's signing ever used: the `extraction`
    extra, the Neo4j relationship aggregator of the briefings and the node-certificate signer."""
    import inspect

    from protagine.agents.store import AgentStore
    from protagine.briefings import aggregators

    optional = (ROOT / "pyproject.toml").read_text().split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
    assert not re.search(r"^extraction\s*=", optional, re.M)
    assert not hasattr(aggregators, "RelationshipAggregator")
    assert "MATCH (" not in inspect.getsource(aggregators)
    assert not hasattr(AgentStore, "sign_node_certificate")
    assert "protagine_key_manager" not in inspect.signature(AgentStore).parameters


RETIRED_WORDS = re.compile(r"neo4j|graph|world.?model|belief engine|beliefs\.engine|chain|genesis"
                           r"|continuous.?learner|protagine-id|node.?cert", re.I)


def test_the_sidecar_starts_on_a_fresh_state_dir_without_a_word_about_a_retired_subsystem(tmp_path, monkeypatch, caplog):
    """The deleted wiring lived in the lifespan, which the route and import checks never run: the whole
    startup and shutdown on an empty state directory, with every warning it logs read for a retired name."""
    import logging

    from fastapi.testclient import TestClient

    from protagine.config import write_api_key
    from protagine.server import create_app

    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    write_api_key("smoke-key", tmp_path)
    caplog.set_level(logging.WARNING)
    with TestClient(create_app(), base_url="http://127.0.0.1:7777", client=("127.0.0.1", 50000)) as client:
        assert client.get("/v1/host/health", headers={"Authorization": "Bearer smoke-key"}).status_code == 200
        assert client.get("/v1/mind/state", headers={"Authorization": "Bearer smoke-key"}).status_code == 200
    noisy = [f"{record.name}: {record.getMessage()}" for record in caplog.records
             if record.levelno >= logging.WARNING and RETIRED_WORDS.search(record.getMessage())]
    assert noisy == []
    assert not any((tmp_path / name).exists() for name in ("chain.db", "protagine-beliefs.db", "protagine_world_model.db",
                                                            "protagine-id", "genesis.json", "protagine-keys"))
