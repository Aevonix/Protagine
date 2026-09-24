"""The wheel carries exactly the thin adapter and the memory provider."""

import configparser
from email.parser import BytesParser
import tarfile
import zipfile

import yaml
from conftest import ROOT

ADAPTER_MODULES = {"__init__", "client", "capture", "body", "guard", "commands", "tools", "reminders"}
PROVIDER_MODULES = {"__init__", "provider", "cli"}


def test_wheel_contains_the_adapter_modules_and_nothing_else(artifacts):
    _, wheel, source = artifacts
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        adapter = {n.split("/", 1)[1] for n in names if n.startswith("protagine_hermes/")}
        provider = {n.split("/", 1)[1] for n in names if n.startswith("protagine_memory/")}
        assert adapter == {f"{m}.py" for m in ADAPTER_MODULES} | {"plugin.yaml"}, adapter
        assert provider == {f"{m}.py" for m in PROVIDER_MODULES} | {"plugin.yaml", "SKILL.md"}, provider
        for module in ADAPTER_MODULES:
            assert archive.read(f"protagine_hermes/{module}.py") == (ROOT / f"plugins/hermes-plugin/{module}.py").read_bytes()
        assert not any(n.startswith("protagine/") or "/ops/" in n or "bundled_skills" in n
                       for n in names)
        metadata = BytesParser().parsebytes(archive.read(next(n for n in names if n.endswith(".dist-info/METADATA"))))
        for package in ("protagine_hermes", "protagine_memory"):
            manifest = yaml.safe_load(archive.read(f"{package}/plugin.yaml"))
            assert manifest["version"] == metadata["Version"], package
        entries = configparser.ConfigParser()
        entries.read_string(archive.read(next(n for n in names if n.endswith("/entry_points.txt"))).decode())
        assert dict(entries["hermes_agent.plugins"]) == {"protagine": "protagine_hermes"}
        assert dict(entries["hermes_agent.memory_providers"]) == {"protagine-memory": "protagine_memory"}
    with tarfile.open(source) as archive:
        names = archive.getnames()
        assert any(n.endswith("/plugins/hermes-plugin/guard.py") for n in names)
        assert not any("/ops/" in n for n in names)


def test_adapter_line_count_is_reported():
    """Tracked, not gated (build plan section 5). The ceiling moved from 2,500 to 2,800 with the memory
    milestone: the constitution render the adapter cannot import from the sidecar, the narrative fetch,
    the completed ``protagine_self`` and the guard's protected-file rule (2,449 before, 2,606 after). It
    moved to 2,850 with the review fixes: the owner's ``/mind`` gate at ``pre_gateway_dispatch`` and the
    guest's refused contact listing (2,786 before, 2,812 after). It moved to 2,900 with the
    self-improvement milestone: forwarding loads of Protagine's skills (``on_skill_lifecycle``) and
    clearing Hermes' skills prompt cache when the sidecar's skills change (2,812 before, about 2,865 after). It
    moved to 2,950 with the upgrade fixes: final answers where the plugin's tools cannot help (an unknown
    commitment id, a guest's refused session search) and a late forget reported as unconfirmed, not failed
    (2,901 after)."""
    total = sum(len((ROOT / f"plugins/hermes-plugin/{m}.py").read_text().splitlines()) for m in ADAPTER_MODULES)
    print(f"plugin lines: {total}")
    assert total < 2950
