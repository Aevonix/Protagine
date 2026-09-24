"""Upgrading a 1.9.0 instance fixture keeps its stores and converts its settings."""

from __future__ import annotations

import json
import sqlite3
import os
import sys
from types import SimpleNamespace

import pytest
import yaml

from protagine import init
from protagine.config import load_config, load_identity, read_api_key
from protagine.init import is_legacy_instance, migrate_legacy_instance, prepared_runtime
from test_init import ADAPTER_SOURCE

HERMES_PYTHON = os.environ.get("PROTAGINE_TEST_HERMES_PYTHON") or sys.executable
if not os.environ.get("PROTAGINE_TEST_HERMES_PYTHON"):
    pytest.importorskip("hermes_cli", reason="stock Hermes is not installed in this interpreter")

LEGACY_SECRET = "legacy-client-secret-0123456789"


def _write_190_instance(home, hermes_home, *, hermes_python):
    home.mkdir(parents=True)
    owner_id = _seed_contacts(home / "contacts.db")
    (home / "instance.json").write_text(json.dumps({
        "version": 1, "hermes_home": str(hermes_home), "hermes_python": hermes_python,
        "owner_id": owner_id, "agent_name": "Orion", "endpoint": "http://127.0.0.1:9/v1",
        "model": "legacy-model", "agent_preferences": {"values": ["care"], "timezone": "UTC", "quiet_hours": ""},
        "profile": "local",
    }, indent=2))
    (home / ".env").write_text("\n".join([
        "PROTAGINE_INSTALL_PROFILE=local", f"PROTAGINE_STATE_DIR={home}",
        "PROTAGINE_SIDECAR_HOST=127.0.0.1", "PROTAGINE_SIDECAR_PORT=7811",
        f"PROTAGINE_OWNER_CONTACT_ID={owner_id}", "PROTAGINE_OWNER_NAME=Ada", "PROTAGINE_PERSONA_NAME=Orion",
        f"PROTAGINE_CONTACTS_DB={home / 'contacts.db'}", f"PROTAGINE_API_KEYRING_PATH={home / 'api-keyring.json'}",
        "PROTAGINE_API_KEY=", f"PROTAGINE_CLIENT_API_KEY={LEGACY_SECRET}", "PROTAGINE_AUTONOMY_PRESET=passive",
        'PROTAGINE_AGENT_VALUES=["care"]', "PROTAGINE_AGENT_TIMEZONE=UTC", "PROTAGINE_AGENT_QUIET_HOURS=",
        "PROTAGINE_EMBED_PROVIDER=skip", "",
    ]))
    (home / "api-keyring.json").write_text(json.dumps({"version": 1, "principals": [{
        "principal": "hermes-local", "status": "active", "viewer_person_id": owner_id,
        "credentials": [{"id": "initial", "secret": LEGACY_SECRET, "status": "active"}],
    }]}))
    (home / ".protagine-llm-config.json").write_text(json.dumps({
        "provider": "local", "baseUrl": "http://127.0.0.1:9/v1", "apiKey": "local-no-key",
        "models": {"small": "legacy-model", "medium": "legacy-model", "large": "legacy-model"},
    }))
    with sqlite3.connect(home / "protagine-facts.db") as db:
        db.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        db.executemany("INSERT INTO facts (body) VALUES (?)", [(f"fact {n}",) for n in range(25)])
    (hermes_home).mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "custom", "default": "legacy-model", "base_url": "http://127.0.0.1:9/v1"},
        "plugins": {"enabled": ["protagine"], "protagine": {"instance_dir": str(home), "api_key": "${PROTAGINE_NATIVE_API_KEY}"}},
        "memory": {"provider": "protagine-memory", "config": {"api_key": "${PROTAGINE_NATIVE_API_KEY}"}},
    }, sort_keys=False))
    return owner_id


def _seed_contacts(path):
    """A real 1.9.0 contact store: the owner plus one guest."""
    import asyncio
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore

    async def seed():
        store = SQLiteContactStore(ContactsConfig(sqlite_path=str(path)))
        await store.connect()
        try:
            owner = await store.create(display_name="Ada", trust_tier="inner_circle", interaction_allowed=True,
                                       import_source="wizard")
            await store.create(display_name="Guest", import_source="wizard")
            return owner.contact_id
        finally:
            await store.close()

    return asyncio.run(seed())


def _rows(path, table):
    with sqlite3.connect(path) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    home = tmp_path / "protagine"
    hermes_home = tmp_path / "hermes"
    owner_id = _write_190_instance(home, hermes_home, hermes_python=HERMES_PYTHON)
    monkeypatch.setenv("PROTAGINE_HOME", str(home))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    return home, hermes_home, owner_id


def test_upgrade_converts_a_190_instance_and_keeps_its_rows(legacy, capsys):
    home, hermes_home, owner_id = legacy
    assert is_legacy_instance(home)
    # The first upgrade installs the adapter from this checkout (an earlier test may
    # have uninstalled it from the shared interpreter); the no-op check below runs
    # without a source, as a user's second `protagine upgrade` would.
    args = SimpleNamespace(home=str(home), hermes_home=None, hermes_python=HERMES_PYTHON,
                           adapter_source=ADAPTER_SOURCE)
    assert init.run_upgrade(args) == 0
    args.adapter_source = None
    output = capsys.readouterr().out
    assert "backup taken" in output
    assert "api.key written from the 1.9.0 keyring" in output
    assert "autonomy starts at 'suggest'" in output

    assert not is_legacy_instance(home)
    assert read_api_key(home, environ={}) == LEGACY_SECRET
    cfg = load_config(home, environ={})
    assert cfg.get("mind.autonomy") == "suggest"
    assert cfg.get("sidecar.port") == 7811
    assert cfg.get("owner.contact_id") == owner_id
    assert cfg.get("router.base_url") == "http://127.0.0.1:9/v1"
    assert cfg.get("router.model") == "legacy-model"
    assert cfg.get("hermes.home") == str(hermes_home)
    assert cfg.get("hermes.python") == HERMES_PYTHON
    assert cfg.get("mind.faculties.semantic_recall") is True           # the switch; no endpoint was carried
    identity = load_identity(home)
    assert identity["owner"]["name"] == "Ada"
    assert identity["agent"] == {"name": "Orion", "values": ["care"], "timezone": "UTC", "quiet_hours": ""}

    # Ledger row counts survive; the old credential files are no longer read.
    assert _rows(home / "contacts.db", "contacts") == 2
    assert _rows(home / "protagine-facts.db", "facts") == 25
    assert not (home / ".env").exists() and (home / ".env.1.9.0").exists()
    assert not (home / "api-keyring.json").exists() and (home / "api-keyring.json.1.9.0").exists()
    backup = next(p for p in (home / "backups").iterdir() if p.is_dir())
    assert _rows(backup / "protagine-facts.db", "facts") == 25
    assert (backup / "api-keyring.json").is_file()

    # The Hermes config now carries the M1 keys; the old ones are left alone.
    hermes = yaml.safe_load((hermes_home / "config.yaml").read_text())
    assert hermes["plugins"]["protagine"]["sidecar_url"] == "http://127.0.0.1:7811"
    assert hermes["plugins"]["protagine"]["key_file"] == str(home / "api.key")
    assert hermes["plugins"]["hook_callback_timeout"] == 0
    assert (hermes_home / "profiles" / "protagine-act" / "config.yaml").is_file()

    # A second upgrade is a no-op.
    assert init.run_upgrade(args) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_prepared_runtime_is_detected_and_the_stock_command_is_printed(tmp_path, monkeypatch, capsys):
    home = tmp_path / "protagine"
    hermes_home = tmp_path / "hermes"
    runtime = tmp_path / "prepared" / "hermes-0.21.3-protagine-1"
    (runtime / ".venv" / "bin").mkdir(parents=True)
    (runtime / ".protagine-runtime.json").write_text("{}")
    python = runtime / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\n")
    assert prepared_runtime(python)
    assert not prepared_runtime(sys.executable)
    _write_190_instance(home, hermes_home, hermes_python=str(python))
    monkeypatch.setenv("PROTAGINE_HOME", str(home))
    notes = migrate_legacy_instance(home)
    joined = "\n".join(notes)
    assert "prepared (patched) Hermes runtime" in joined
    assert "protagine upgrade --hermes-python" in joined
    assert load_config(home, environ={}).get("hermes.python") == ""


def _forwarder(home, module):
    return ("import importlib as _importlib\nimport sys as _sys\n"
            f"_site = {str(home / 'adapter')!r}\n"
            "if _site not in _sys.path:\n    _sys.path.insert(0, _site)\n"
            f"_implementation = _importlib.import_module({module!r})\n")


def test_migration_takes_only_a_live_credential_and_archives_the_forwarders(tmp_path, monkeypatch):
    home = tmp_path / "protagine"
    hermes_home = tmp_path / "hermes"
    _write_190_instance(home, hermes_home, hermes_python=HERMES_PYTHON)
    monkeypatch.setenv("PROTAGINE_HOME", str(home))
    # A revoked principal with an individually active credential comes first; a retiring one is past
    # its accept_until; the configured client credential belongs to the live principal.
    (home / "api-keyring.json").write_text(json.dumps({"version": 1, "principals": [
        {"principal": "old-host", "status": "revoked",
         "credentials": [{"id": "initial", "secret": "revoked-secret-000000000000", "status": "active"}]},
        {"principal": "retired", "status": "retiring", "accept_until": "2020-01-01T00:00:00Z",
         "credentials": [{"id": "initial", "secret": "expired-secret-000000000000", "status": "active"}]},
        {"principal": "hermes-local", "status": "active", "credentials": [
            {"id": "rotated-out", "secret": "rotated-secret-000000000000", "status": "revoked"},
            {"id": "initial", "secret": LEGACY_SECRET, "status": "active"}]},
    ]}))
    for name, module in (("protagine", "protagine_hermes"), ("protagine-memory", "protagine_memory")):
        (hermes_home / "plugins" / name).mkdir(parents=True)
        (hermes_home / "plugins" / name / "__init__.py").write_text(_forwarder(home, module))
        (hermes_home / "plugins" / name / "plugin.yaml").write_text("name: " + name + "\n")
    (hermes_home / "plugins" / "other").mkdir()
    (hermes_home / "plugins" / "other" / "__init__.py").write_text("def register(ctx): pass\n")

    notes = migrate_legacy_instance(home)
    assert read_api_key(home, environ={}) == LEGACY_SECRET
    assert not (hermes_home / "plugins" / "protagine").exists()
    assert not (hermes_home / "plugins" / "protagine-memory").exists()
    assert (hermes_home / "plugins" / "other" / "__init__.py").is_file()
    archived = list((home / "backups").glob("*/hermes-plugins/protagine-memory/__init__.py"))
    assert len(archived) == 1 and "_site" in archived[0].read_text()
    assert sum("1.9.0 forwarder" in note for note in notes) == 2


def test_migration_generates_a_key_when_no_190_credential_is_live(tmp_path, monkeypatch):
    home = tmp_path / "protagine"
    hermes_home = tmp_path / "hermes"
    _write_190_instance(home, hermes_home, hermes_python=HERMES_PYTHON)
    monkeypatch.setenv("PROTAGINE_HOME", str(home))
    (home / "api-keyring.json").write_text(json.dumps({"version": 1, "principals": [
        {"principal": "hermes-local", "status": "disabled",
         "credentials": [{"id": "initial", "secret": LEGACY_SECRET, "status": "active"}]},
    ]}))
    notes = migrate_legacy_instance(home)
    key = read_api_key(home, environ={})
    assert key and key != LEGACY_SECRET
    assert any("api.key generated" in note for note in notes)
