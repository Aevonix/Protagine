"""M9 deletions: the dormant learning machinery lessons and Hermes skills replace is gone.

Nothing imports the removed packages, no route serves them, no client calls those routes, no
capability advertises them, no environment flag names them, and the state files and tables they
left behind move into the upgrade backup.
"""

from importlib.util import find_spec
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
PACKAGE = ROOT / "protagine"

RETIRED_MODULES = (
    "protagine.toolsmith",
    "protagine.skills",          # the whole package: registry, executor, sandbox runner, synthesis, packager
    "protagine.self_model.experiments",
    "protagine.self_model.params",
    "protagine.intelligence.cognition",     # MetaLearner, CPI, StrategyAdjuster, gap detector
)
RETIRED_ROUTE_PREFIXES = ("/v1/host/self/tools", "/v1/host/skills/", "/v1/host/self/experiments",
                          "/v1/host/self/params")
RETIRED_ENV = ("PROTAGINE_TOOLSMITH", "PROTAGINE_EXPERIMENTS_", "PROTAGINE_EXPERIMENT_PREGRANTS_JSON")
RETIRED_CAPABILITIES: tuple = ("skills", "skill_sandbox", "security_scanner", "cognition")


def absent(module: str) -> bool:
    try:
        return find_spec(module) is None
    except ModuleNotFoundError:
        return True


@pytest.mark.parametrize("module", RETIRED_MODULES)
def test_the_m9_retired_modules_do_not_exist(module):
    assert absent(module), f"{module} still exists"


def test_no_retired_route_is_served():
    from protagine.server import create_app
    paths = set(create_app().openapi().get("paths", {}))
    assert "/v1/host/context/assemble" in paths
    assert sorted(path for path in paths if path.startswith(RETIRED_ROUTE_PREFIXES)) == []


def test_no_client_calls_a_retired_route():
    """Plugins, scripts, benchmarks, the MCP server and the e2e suites call only served routes."""
    route = re.compile("|".join(re.escape(prefix) + r"\b" for prefix in RETIRED_ROUTE_PREFIXES))
    roots = [REPO / "plugins", REPO / "scripts", REPO / "benchmarks", REPO / "tests", ROOT / "scripts",
             ROOT / "tests" / "e2e", ROOT / "tests" / "integration", PACKAGE / "mcp"]
    hits = []
    for base in roots:
        for path in sorted(base.rglob("*.py")) if base.is_dir() else ():
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if route.search(line):
                    hits.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert hits == []


def test_no_source_line_imports_a_retired_module():
    pattern = re.compile("|".join(re.escape(module) + r"\b" for module in RETIRED_MODULES))
    hits = []
    for base in (PACKAGE, ROOT / "tests", REPO / "plugins", REPO / "scripts", REPO / "tests"):
        for path in sorted(base.rglob("*.py")):
            if path.name == Path(__file__).name:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert hits == []


def test_no_environment_flag_names_a_retired_subsystem():
    text = (REPO / ".env.example").read_text() + (PACKAGE / "server.py").read_text()
    assert [name for name in RETIRED_ENV if name in text] == []


def test_health_capabilities_never_advertise_the_m9_subsystems():
    from protagine.api.routers.host import supported_capabilities
    assert not set(RETIRED_CAPABILITIES) & set(supported_capabilities())
