"""``protagine upgrade``: idempotent, and it repairs what drifted."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
import yaml

from protagine import init
from protagine.init import WORKER_PROFILE
from test_init import STOCK_CONFIG, _args, _snapshot

HERMES_PYTHON = os.environ.get("PROTAGINE_TEST_HERMES_PYTHON") or sys.executable
if not os.environ.get("PROTAGINE_TEST_HERMES_PYTHON"):
    pytest.importorskip("hermes_cli", reason="stock Hermes is not installed in this interpreter")


@pytest.fixture
def installed(tmp_path, monkeypatch):
    home = tmp_path / "protagine"
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(STOCK_CONFIG, sort_keys=False))
    monkeypatch.setenv("PROTAGINE_HOME", str(home))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    assert init.run_init(_args(home, hermes_home)) == 0
    return home, hermes_home


def _upgrade_args(home, **overrides):
    values = {"home": str(home), "hermes_home": None, "hermes_python": HERMES_PYTHON, "adapter_source": None}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_upgrade_twice_changes_nothing(installed, capsys):
    home, hermes_home = installed
    before_home, before_hermes = _snapshot(home), _snapshot(hermes_home)
    assert init.run_upgrade(_upgrade_args(home)) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert _snapshot(home) == before_home
    assert _snapshot(hermes_home) == before_hermes
    assert not (home / "backups").exists() or not any(p.is_dir() and p.name[:1].isdigit() for p in (home / "backups").iterdir())
    assert init.run_upgrade(_upgrade_args(home)) == 0
    assert _snapshot(home) == before_home


def test_upgrade_repairs_drift_after_a_backup(installed, capsys):
    home, hermes_home = installed
    config_path = hermes_home / "config.yaml"
    drifted = yaml.safe_load(config_path.read_text())
    del drifted["memory"]
    drifted["plugins"]["enabled"] = []
    config_path.write_text(yaml.safe_dump(drifted, sort_keys=False))
    (hermes_home / "profiles" / WORKER_PROFILE / "config.yaml").unlink()

    assert init.run_upgrade(_upgrade_args(home)) == 0
    output = capsys.readouterr().out
    assert "backup taken" in output
    assert "memory.provider: protagine-memory" in output
    assert "plugins.enabled += protagine" in output
    assert f"profiles/{WORKER_PROFILE} written" in output
    repaired = yaml.safe_load(config_path.read_text())
    assert repaired["memory"]["provider"] == "protagine-memory"
    assert repaired["plugins"]["enabled"] == ["protagine"]
    assert (hermes_home / "profiles" / WORKER_PROFILE / "config.yaml").is_file()
    backups = [p for p in (home / "backups").iterdir() if p.is_dir()]
    assert len(backups) == 1
    assert (backups[0] / "protagine.yaml").is_file() and (backups[0] / "api.key").is_file()

    before = _snapshot(hermes_home)
    assert init.run_upgrade(_upgrade_args(home)) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert _snapshot(hermes_home) == before


def test_upgrade_records_a_moved_hermes_binding(installed, tmp_path, capsys):
    home, hermes_home = installed
    moved = tmp_path / "hermes-moved"
    moved.mkdir()
    (moved / "config.yaml").write_text(yaml.safe_dump(STOCK_CONFIG, sort_keys=False))
    assert init.run_upgrade(_upgrade_args(home, hermes_home=str(moved))) == 0
    output = capsys.readouterr().out
    assert "Hermes binding recorded" in output
    from protagine.config import load_config
    assert load_config(home, environ={}).get("hermes.home") == str(moved)
    assert yaml.safe_load((moved / "config.yaml").read_text())["plugins"]["enabled"] == ["protagine"]


def test_upgrade_reconciles_a_changed_worker_toolset(installed, capsys):
    """Editing ``mind.worker_toolsets`` or ``mind.deny.commands`` is not "nothing to do"."""
    from protagine.config import load_config, save_config
    home, hermes_home = installed
    cfg = load_config(home, environ={})
    cfg.data["mind"]["worker_toolsets"] = ["web", "file"]
    cfg.data["mind"]["deny"]["commands"] = ["shutdown*"]
    save_config(cfg.data, home)
    assert init.run_upgrade(_upgrade_args(home)) == 0
    output = capsys.readouterr().out
    assert f"profiles/{WORKER_PROFILE} written" in output and "nothing to do" not in output
    profile = yaml.safe_load((hermes_home / "profiles" / WORKER_PROFILE / "config.yaml").read_text())
    assert profile["toolsets"] == ["web", "file"]
    assert profile["approvals"] == {"deny": ["shutdown*"]}
    assert init.run_upgrade(_upgrade_args(home)) == 0
    assert "nothing to do" in capsys.readouterr().out
