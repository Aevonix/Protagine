"""``protagine init`` against a temporary Hermes home with stock Hermes installed.

The interpreter running these tests has ``hermes-agent`` and the adapter
installed, so it stands in for the Python of the ``hermes`` executable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from protagine import init
from protagine.config import load_config, read_api_key
from protagine.init import (
    PROTECTED_PATTERNS,
    WORKER_PROFILE,
    hermes_version_supported,
    parse_version,
    reconcile_hermes_config,
    strip_hermes_config,
)

# A dedicated stock-Hermes interpreter (CI) or this one when it has Hermes installed.
HERMES_PYTHON = os.environ.get("PROTAGINE_TEST_HERMES_PYTHON") or sys.executable
if not os.environ.get("PROTAGINE_TEST_HERMES_PYTHON"):
    pytest.importorskip("hermes_cli", reason="stock Hermes is not installed in this interpreter")
# The adapter init installs comes from this checkout, never from an index: a
# published release of the same version number would be a different plugin.
ADAPTER_SOURCE = os.environ.get("PROTAGINE_TEST_ADAPTER_SOURCE") or str(Path(__file__).resolve().parents[2])

STOCK_CONFIG = {
    "model": {"provider": "custom", "default": "test-model", "base_url": "http://127.0.0.1:9/v1"},
    "toolsets": ["hermes-cli"],
    "approvals": {"deny": ["shutdown*"]},
}


def _args(home, hermes_home, **overrides):
    values = {
        "home": str(home), "non_interactive": True, "uninstall": False, "owner_name": "Ada",
        "owner_handle": ["telegram=1001"], "agent_name": "Sol", "agent_values": "care, candour",
        "agent_boundaries": "never send money", "timezone": "UTC", "quiet_hours": "22:00-07:00", "autonomy": None,
        "hermes_home": str(hermes_home), "hermes_python": HERMES_PYTHON, "host": None, "port": 7901,
        "model_url": None, "model": None, "model_key": None, "embed_url": None, "embed_model": None,
        "adapter_source": ADAPTER_SOURCE, "no_service": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "protagine"
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(STOCK_CONFIG, sort_keys=False))
    (hermes_home / ".env").write_text("OPENAI_API_KEY=model-secret\n")
    monkeypatch.setenv("PROTAGINE_HOME", str(home))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    monkeypatch.delenv("PROTAGINE_OWNER_CONTACT_ID", raising=False)
    return home, hermes_home


def _snapshot(root):
    """Every file's bytes, minus backups and SQLite's transient WAL side files."""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and "backups" not in p.parts and not p.name.endswith((".db-shm", ".db-wal"))}


def test_version_range():
    assert parse_version("0.21.3") == (0, 21, 3)
    assert hermes_version_supported("0.21.3")
    assert hermes_version_supported("0.21.4")
    assert not hermes_version_supported("0.21.2")
    assert not hermes_version_supported("0.22.0")


def test_reconcile_writes_every_key_once(tmp_path):
    key_file = tmp_path / "api.key"
    updated, changes = reconcile_hermes_config(STOCK_CONFIG, sidecar_url="http://127.0.0.1:7901",
                                               key_file=key_file, skills_dir=tmp_path / "skills")
    assert updated["plugins"]["enabled"] == ["protagine"]
    assert updated["plugins"]["hook_callback_timeout"] == 0
    assert updated["plugins"]["protagine"] == {"sidecar_url": "http://127.0.0.1:7901", "key_file": str(key_file)}
    assert updated["memory"]["provider"] == "protagine-memory"
    assert updated["kanban"]["dispatch_in_gateway"] is True
    assert updated["skills"]["external_dirs"] == [str(tmp_path / "skills")]
    assert updated["security"]["protected_instruction_extra_patterns"] == list(PROTECTED_PATTERNS)
    assert updated["approvals"] == {"deny": ["shutdown*"]}  # untouched
    assert "admin" not in json.dumps(updated)  # no admin lists, ever
    again, no_changes = reconcile_hermes_config(updated, sidecar_url="http://127.0.0.1:7901",
                                                key_file=key_file, skills_dir=tmp_path / "skills")
    assert no_changes == [] and again == updated
    stripped, _ = strip_hermes_config(updated, skills_dir=tmp_path / "skills")
    assert stripped == STOCK_CONFIG


def test_init_performs_the_seven_steps_and_is_idempotent(homes, capsys):
    home, hermes_home = homes
    assert init.run_init(_args(home, hermes_home)) == 0
    output = capsys.readouterr().out
    assert "hermes gateway restart" in output
    assert "pip check" in output

    # Step 2: the instance files.
    cfg = load_config(home, environ={})
    assert cfg.get("sidecar.port") == 7901
    assert cfg.get("mind.autonomy") == "suggest"
    assert cfg.get("hermes.python") == HERMES_PYTHON
    assert cfg.get("owner.contact_id")
    assert read_api_key(home, environ={})
    assert oct((home / "api.key").stat().st_mode & 0o777) == "0o600"
    identity = yaml.safe_load((home / "identity.yaml").read_text())
    assert identity["owner"] == {"name": "Ada", "handles": [{"platform": "telegram", "id": "1001"}]}
    assert identity["agent"]["name"] == "Sol"
    assert identity["agent"]["values"] == ["care", "candour"]
    assert identity["agent"]["boundaries"] == ["never send money"]
    assert "You are Sol. Your values: care; candour. Your boundaries: never send money." in output

    # Step 3: the router points at the endpoint Hermes uses; no embeddings recorded.
    llm = json.loads((home / ".protagine-llm-config.json").read_text())
    assert llm["baseUrl"] == "http://127.0.0.1:9/v1"
    assert llm["apiKey"] == "model-secret"
    assert llm["models"]["medium"] == "test-model"
    assert cfg.get("mind.faculties.semantic_recall") is True           # the switch; the endpoint decides

    # Steps 5 and 6: the Hermes keys and the worker profile.
    hermes = yaml.safe_load((hermes_home / "config.yaml").read_text())
    assert hermes["plugins"]["enabled"] == ["protagine"]
    assert hermes["plugins"]["protagine"] == {"sidecar_url": "http://127.0.0.1:7901", "key_file": str(home / "api.key")}
    assert hermes["memory"]["provider"] == "protagine-memory"
    assert hermes["model"] == STOCK_CONFIG["model"]
    profile = yaml.safe_load((hermes_home / "profiles" / WORKER_PROFILE / "config.yaml").read_text())
    assert profile["model"] == STOCK_CONFIG["model"]
    assert profile["toolsets"] == ["web", "file", "session_search", "memory", "todo"]
    assert profile["approvals"] == {"deny": []}
    assert profile["plugins"]["enabled"] == ["protagine"]
    assert profile["memory"]["provider"] == "protagine-memory"

    # Stock Hermes loads the result and sees the profile.
    check = subprocess.run(
        [HERMES_PYTHON, "-c",
         "from hermes_cli.config import load_config; from hermes_cli.profiles import profile_exists; "
         "c = load_config(); assert 'protagine' in c['plugins']['enabled'], c['plugins']; "
         "assert c['memory']['provider'] == 'protagine-memory'; assert profile_exists('protagine-act'); print('ok')"],
        capture_output=True, text=True, env={**os.environ, "HERMES_HOME": str(hermes_home)}, timeout=120,
    )
    assert check.returncode == 0, check.stdout + check.stderr

    # Running init again changes nothing.
    before_home, before_hermes = _snapshot(home), _snapshot(hermes_home)
    assert init.run_init(_args(home, hermes_home)) == 0
    assert _snapshot(home) == before_home
    assert _snapshot(hermes_home) == before_hermes
    assert not any((home / "backups").glob("hermes-config-*")) or len(list((home / "backups").glob("hermes-config-*"))) == 1


def test_init_keeps_existing_answers_and_lets_flags_change_them(homes):
    home, hermes_home = homes
    assert init.run_init(_args(home, hermes_home)) == 0
    key = read_api_key(home, environ={})
    contact = load_config(home, environ={}).get("owner.contact_id")
    assert init.run_init(_args(home, hermes_home, owner_name=None, agent_name=None, agent_values=None,
                               autonomy="suggest", embed_url="http://127.0.0.1:9/v1")) == 0
    cfg = load_config(home, environ={})
    assert cfg.get("mind.autonomy") == "suggest"
    assert cfg.get("mind.faculties.semantic_recall") is True
    assert cfg.get("owner.contact_id") == contact
    assert read_api_key(home, environ={}) == key
    identity = yaml.safe_load((home / "identity.yaml").read_text())
    assert identity["owner"]["name"] == "Ada" and identity["agent"]["name"] == "Sol"
    assert identity["agent"]["boundaries"] == ["never send money"]
    # A flag replaces the list; the constitution the plugin renders follows the file.
    assert init.run_init(_args(home, hermes_home, agent_boundaries="never contact family members")) == 0
    identity = yaml.safe_load((home / "identity.yaml").read_text())
    assert identity["agent"]["boundaries"] == ["never contact family members"]
    from protagine.config import render_constitution
    assert render_constitution(identity).endswith("Your boundaries: never contact family members.")


def test_init_refuses_bad_input_before_writing(homes):
    home, hermes_home = homes
    assert init.run_init(_args(home, hermes_home, owner_handle=["bogus"])) == 1
    assert not (home / "protagine.yaml").exists()
    # An over-long constitution (rendered name + values + boundaries above 1,500 characters) is refused too.
    over_long = ", ".join(f"b{i}".ljust(150, "b") for i in range(12))
    assert init.run_init(_args(home, hermes_home, agent_boundaries=over_long)) == 1
    assert not (home / "identity.yaml").exists()
    assert init.run_init(_args(home, hermes_home, quiet_hours="late")) == 1
    assert init.run_init(_args(home, hermes_home, hermes_python=str(home / "missing-python"))) == 1


def test_uninstall_leaves_stock_hermes(homes, capsys):
    home, hermes_home = homes
    assert init.run_init(_args(home, hermes_home)) == 0
    session = hermes_home / "profiles" / WORKER_PROFILE / "sessions" / "worker-1.json"
    session.write_text("{}")
    assert init.run_init(_args(home, hermes_home, uninstall=True, hermes_python=None)) == 0
    hermes = yaml.safe_load((hermes_home / "config.yaml").read_text())
    assert hermes == STOCK_CONFIG
    assert not (hermes_home / "profiles" / WORKER_PROFILE).exists()
    assert (hermes_home / "profiles" / ".deleted" / WORKER_PROFILE).exists()
    assert (home / "protagine.yaml").exists()  # the instance data is kept
    # The worker's history moved into the backups instead of being deleted.
    kept = list((home / "backups").glob(f"*/profiles/{WORKER_PROFILE}/sessions/worker-1.json"))
    assert len(kept) == 1 and (kept[0].parents[1] / "config.yaml").is_file()
    check = subprocess.run(
        [HERMES_PYTHON, "-c",
         "from hermes_cli.config import load_config; from hermes_cli.profiles import profile_exists; "
         "c = load_config(); assert 'protagine' not in (c.get('plugins') or {}).get('enabled', []); "
         "assert not profile_exists('protagine-act'); print('ok')"],
        capture_output=True, text=True, env={**os.environ, "HERMES_HOME": str(hermes_home)}, timeout=120,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert "stock again" in capsys.readouterr().out


def test_uninstall_keeps_the_adapter_while_another_profile_uses_it(homes, capsys):
    home, hermes_home = homes
    assert init.run_init(_args(home, hermes_home)) == 0
    other = hermes_home / "profiles" / "other"
    other.mkdir(parents=True)
    (other / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["protagine"]}}))
    assert init.run_init(_args(home, hermes_home, uninstall=True, hermes_python=None)) == 0
    output = capsys.readouterr().out
    assert "kept in" in output and str(other) in output
    assert init.installed_adapter_version(Path(HERMES_PYTHON)) is not None
    assert yaml.safe_load((hermes_home / "config.yaml").read_text()) == STOCK_CONFIG


class _FakeService:
    def __init__(self, fail_start=None):
        self.calls = []
        self.fail_start = fail_start

    def _manager_ready(self):
        pass

    def install(self):
        self.calls.append("install")
        return {"manager": "systemd-user", "label": "protagine-test.service"}

    def start(self):
        self.calls.append("start")
        if self.fail_start is not None:
            raise self.fail_start
        return {"ready": True}


def test_init_starts_the_service_it_installs(homes, monkeypatch, capsys):
    """Enabling a user service starts nothing before the next login; init must start it."""
    from protagine.services.instance import ServiceError
    home, hermes_home = homes
    monkeypatch.delenv("PROTAGINE_INIT_NO_SERVICE", raising=False)
    service = _FakeService()
    monkeypatch.setattr(init, "_service", lambda cfg: service)
    assert init.run_init(_args(home, hermes_home, no_service=False)) == 0
    assert service.calls == ["install", "start"]
    assert "sidecar service installed (systemd-user: protagine-test.service) and running" in capsys.readouterr().out

    failing = _FakeService(fail_start=ServiceError("Instance port is already occupied"))
    monkeypatch.setattr(init, "_service", lambda cfg: failing)
    assert init.run_init(_args(home, hermes_home, no_service=False)) == 0
    output = capsys.readouterr().out
    assert failing.calls == ["install", "start"]
    assert "not running: Instance port is already occupied" in output and "protagine service start" in output


def test_init_records_the_embedding_width_when_given(homes):
    home, hermes_home = homes
    assert init.run_init(_args(home, hermes_home, embed_url="http://127.0.0.1:8092",
                               embed_model="an-embedding-model", embed_dims=4096)) == 0
    cfg = load_config(home, environ={})
    assert cfg.get("router.embed_dims") == 4096
    assert cfg.get("mind.faculties.semantic_recall") is True
    # Left out, the width stays as recorded; the endpoint defines it when none was ever given.
    assert init.run_init(_args(home, hermes_home, embed_url="http://127.0.0.1:8092")) == 0
    assert load_config(home, environ={}).get("router.embed_dims") == 4096


def test_init_repeats_a_degraded_health_verdict_in_words(homes, monkeypatch, capsys):
    """The service started, but health is not ok: init says why, from the served problems."""
    home, hermes_home = homes
    monkeypatch.delenv("PROTAGINE_INIT_NO_SERVICE", raising=False)

    class Degraded(_FakeService):
        def start(self):
            self.calls.append("start")
            return {"ready": True, "health": "degraded",
                    "problems": ["semantic recall is off: the embedder (provider=openai_api, model=m) did not "
                                 "initialise: ValueError: dimension 4096 differs from the configured 384"]}

    monkeypatch.setattr(init, "_service", lambda cfg: Degraded())
    assert init.run_init(_args(home, hermes_home, no_service=False)) == 0
    output = capsys.readouterr().out
    assert ("and running; health degraded: semantic recall is off: the embedder (provider=openai_api, model=m) "
            "did not initialise: ValueError: dimension 4096 differs from the configured 384") in output


def test_init_refuses_an_environment_without_the_vector_store(homes, monkeypatch, capsys):
    """A sidecar without its vector store would serve keyword recall only; init says so and stops."""
    home, hermes_home = homes
    monkeypatch.setattr(init, "vector_store_available", lambda: False)
    assert init.run_init(_args(home, hermes_home)) == 1
    assert "lancedb" in capsys.readouterr().out and not (home / "protagine.yaml").exists()


def test_upgrade_refuses_an_environment_without_the_vector_store(homes, monkeypatch, capsys):
    home, hermes_home = homes
    assert init.run_init(_args(home, hermes_home)) == 0
    capsys.readouterr()
    before = _snapshot(home)
    monkeypatch.setattr(init, "vector_store_available", lambda: False)
    upgrade = SimpleNamespace(home=str(home), hermes_home=str(hermes_home), hermes_python=HERMES_PYTHON,
                              adapter_source=ADAPTER_SOURCE)
    assert init.run_upgrade(upgrade) == 1
    assert "lancedb" in capsys.readouterr().out
    assert _snapshot(home) == before, "nothing was backed up, migrated or rewritten"
    assert not any(path.is_dir() for path in (home / "backups").glob("*")), "no upgrade backup was taken"


def test_upgrade_retires_the_m8_state_files_and_keeps_the_instance_id(homes, capsys):
    """The belief engine, chain and world-model files move into the upgrade backup right after it is
    taken; the chain's instance id carries over as ``instance-id``; a second upgrade has nothing to do."""
    import sqlite3
    home, hermes_home = homes
    assert init.run_init(_args(home, hermes_home)) == 0
    upgrade = SimpleNamespace(home=str(home), hermes_home=None, hermes_python=HERMES_PYTHON, adapter_source=None)
    assert init.run_upgrade(upgrade) == 0
    assert "nothing to do" in capsys.readouterr().out
    with sqlite3.connect(home / "protagine_world_model.db") as connection:
        connection.execute("CREATE TABLE entities (id TEXT)")
    (home / "protagine-id").write_text("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n")
    (home / "instance-id").unlink(missing_ok=True)
    (home / "protagine-keys").mkdir()
    (home / "protagine-keys" / "private.pem").write_text("key")
    assert init.run_upgrade(upgrade) == 0
    output = capsys.readouterr().out
    assert "retired protagine_world_model.db" in output and "backup taken" in output
    retired, = (home / "backups").glob("*/retired")
    assert {path.name for path in retired.iterdir()} == {"protagine_world_model.db", "protagine-id", "protagine-keys"}
    assert (home / "instance-id").read_text().strip() == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert not any((home / name).exists() for name in ("protagine_world_model.db", "protagine-id", "protagine-keys"))
    assert init.run_upgrade(upgrade) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_an_embedding_endpoint_added_after_init_turns_semantic_recall_on(homes):
    """``mind.faculties.semantic_recall`` is the owner's (or an arm's) off switch, not a copy of whether an
    endpoint existed at init: an instance set up without one and given ``router.embed_url`` later recalls
    semantically, as the install guide says; turning the flag off still keeps the embedder off."""
    from protagine.config import apply_environment, save_config
    home, hermes_home = homes
    assert init.run_init(_args(home, hermes_home)) == 0
    cfg = load_config(home, environ={})
    assert cfg.get("mind.faculties.semantic_recall") is True
    environ: dict[str, str] = {}
    apply_environment(cfg, environ=environ)
    assert environ["PROTAGINE_EMBED_PROVIDER"] == "skip"            # no endpoint yet: lexical recall
    data = yaml.safe_load((home / "protagine.yaml").read_text())
    data["router"]["embed_url"] = "http://127.0.0.1:9/v1"
    save_config(data, home)
    environ = {}
    apply_environment(load_config(home, environ={}), environ=environ)
    assert environ["PROTAGINE_EMBED_PROVIDER"] == "openai_api"
    data["mind"]["faculties"]["semantic_recall"] = False
    save_config(data, home)
    environ = {}
    apply_environment(load_config(home, environ={}), environ=environ)
    assert environ["PROTAGINE_EMBED_PROVIDER"] == "skip"
