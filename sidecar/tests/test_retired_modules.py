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
    "protagine.beliefs.engine",
)
RETIRED_ROUTE_PREFIXES = ("/v1/host/world", "/v1/host/world-model", "/v1/host/beliefs",
                          "/v1/host/identity", "/v1/host/chain")
RETIRED_ROUTES = ("/v1/host/learning/weights", "/v1/host/learning/engagement")
# The contact store still labels rows it once received from the world model; a
# quoted "world_model" is that import_source value, never a module path.
ALLOWED_LITERAL = re.compile(r"""["']world_model["']""")


@pytest.mark.parametrize("module", RETIRED_MODULES)
def test_retired_modules_do_not_exist(module):
    assert find_spec(module) is None, f"{module} still exists"


def test_no_retired_route_is_served():
    from protagine.server import create_app

    paths = {route.path for route in create_app().routes if hasattr(route, "path")}
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
    pattern = re.compile(r"world_model|intelligence\.graph|protagine\.chain|continuous_learner|NEO4J")
    hits = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "identity_bootstrap" in path.parts or path.name == "retired.py":
            continue  # M5 deletes the bootstrap package; retired.py names the retired files
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line) and not ALLOWED_LITERAL.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
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


def test_retire_state_moves_every_retired_file_and_keeps_the_instance_id(tmp_path):
    from protagine.retired import RETIRED_STATE, retire_state, retired_state_present

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
