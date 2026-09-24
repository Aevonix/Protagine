"""``protagine.yaml``: defaults, the file, the environment overrides and the key file."""

from __future__ import annotations

import stat

import pytest
import yaml

from protagine import config
from protagine.config import (
    ConfigError,
    apply_environment,
    env_bool,
    env_choice,
    load_config,
    read_api_key,
    save_config,
    save_identity,
    write_api_key,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    return tmp_path


def test_defaults_without_a_file(home):
    cfg = load_config(home, environ={})
    assert cfg.exists is False
    assert cfg.get("sidecar.host") == "127.0.0.1"
    assert cfg.get("sidecar.port") == 7777
    assert cfg.get("mind.enabled") is True
    assert cfg.get("mind.autonomy") == "suggest"
    assert cfg.get("mind.deny.commands") == []
    assert cfg.get("mind.faculties.skills") is False
    assert cfg.sidecar_url == "http://127.0.0.1:7777"
    assert cfg.hermes_home.name == ".hermes"


def test_required_file_missing_is_an_error(home):
    with pytest.raises(ConfigError):
        load_config(home, required=True)


def test_file_merges_over_defaults_and_keeps_unknown_keys(home):
    (home / "protagine.yaml").write_text(yaml.safe_dump({
        "sidecar": {"port": 8100},
        "mind": {"autonomy": "suggest", "deny": {"commands": ["rm -rf *"]}, "faculties": {"people": False}},
        "custom": {"kept": True},
    }))
    cfg = load_config(home, environ={})
    assert cfg.exists is True
    assert cfg.get("sidecar.port") == 8100
    assert cfg.get("sidecar.host") == "127.0.0.1"
    assert cfg.get("mind.autonomy") == "suggest"
    assert cfg.get("mind.deny.commands") == ["rm -rf *"]
    assert cfg.get("mind.deny.tools") == []
    assert cfg.get("mind.faculties.people") is False
    assert cfg.get("mind.faculties.affect") is True
    assert cfg.get("custom.kept") is True


@pytest.mark.parametrize("document, message", [
    ({"mind": {"autonomy": "shadow"}}, "mind.autonomy"),
    ({"sidecar": {"port": "many"}}, "sidecar.port"),
    ({"sidecar": {"port": 70000}}, "sidecar.port"),
    ({"mind": {"deny": {"commands": "rm"}}}, "mind.deny.commands"),
    ({"mind": {"faculties": {"people": "sometimes"}}}, "mind.faculties.people"),
    ({"mind": {"budgets": {"tasks_per_hour": "four"}}}, "mind.budgets.tasks_per_hour"),
    ({"mind": "on"}, "mind must be a mapping"),
])
def test_invalid_values_are_refused(home, document, message):
    (home / "protagine.yaml").write_text(yaml.safe_dump(document))
    with pytest.raises(ConfigError) as info:
        load_config(home, environ={})
    assert message in str(info.value)


def test_not_a_mapping_is_refused(home):
    (home / "protagine.yaml").write_text("- just\n- a list\n")
    with pytest.raises(ConfigError):
        load_config(home)


def test_environment_overrides_are_the_small_documented_set(home):
    cfg = load_config(home, environ={
        "PROTAGINE_SIDECAR_HOST": "0.0.0.0",
        "PROTAGINE_SIDECAR_PORT": "9001",
        "HERMES_HOME": str(home / "hermes"),
        "PROTAGINE_MIND_ENABLED": "false",
        "PROTAGINE_AUTONOMY": "trusted",
        "PROTAGINE_ROUTER_MODEL": "ignored",
    })
    assert cfg.get("sidecar.host") == "0.0.0.0"
    assert cfg.get("sidecar.port") == 9001
    assert cfg.hermes_home == home / "hermes"
    assert cfg.get("mind.enabled") is False
    assert cfg.get("mind.autonomy") == "trusted"
    assert cfg.get("router.model") == ""
    assert set(config.ENV_OVERRIDES) == {
        "PROTAGINE_SIDECAR_HOST", "PROTAGINE_SIDECAR_PORT", "HERMES_HOME",
        "PROTAGINE_MIND_ENABLED", "PROTAGINE_AUTONOMY",
    }


def test_save_validates_and_writes_a_private_file(home):
    path = save_config({**config.DEFAULTS, "mind": {**config.DEFAULTS["mind"], "autonomy": "off"}}, home)
    assert path == home / "protagine.yaml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_config(home, environ={}).get("mind.autonomy") == "off"
    with pytest.raises(ConfigError):
        save_config({**config.DEFAULTS, "mind": {**config.DEFAULTS["mind"], "autonomy": "yolo"}}, home)


def test_api_key_file_is_one_private_line(home):
    assert read_api_key(home, environ={}) is None
    path = write_api_key("  s3cret-token \n", home)
    assert path.read_text() == "s3cret-token\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_api_key(home, environ={}) == "s3cret-token"
    assert read_api_key(home, environ={"PROTAGINE_API_KEY": "from-env"}) == "from-env"
    with pytest.raises(ConfigError):
        write_api_key("two words", home)
    with pytest.raises(ConfigError):
        write_api_key("", home)


def test_apply_environment_exports_what_the_sidecar_reads(home):
    save_config({**config.DEFAULTS, "owner": {"contact_id": "cid-owner"},
                 "router": {**config.DEFAULTS["router"], "embed_url": "http://127.0.0.1:9/v1", "embed_model": "e5"}},
                home)
    save_identity({"owner": {"name": "Ada", "handles": []},
                   "agent": {"name": "Sol", "values": ["care"], "timezone": "UTC", "quiet_hours": "22:00-07:00"}},
                  home)
    write_api_key("k", home)
    environ: dict[str, str] = {"PROTAGINE_SIDECAR_PORT": "5000"}
    applied = apply_environment(load_config(home, environ={}), environ=environ)
    assert environ["PROTAGINE_STATE_DIR"] == str(home)
    assert environ["PROTAGINE_HOME"] == str(home)
    assert environ["PROTAGINE_API_KEY"] == "k"
    assert environ["PROTAGINE_OWNER_CONTACT_ID"] == "cid-owner"
    assert environ["PROTAGINE_OWNER_NAME"] == "Ada"
    assert environ["PROTAGINE_PERSONA_NAME"] == "Sol"
    assert environ["PROTAGINE_AGENT_VALUES"] == '["care"]'
    assert environ["PROTAGINE_AGENT_QUIET_HOURS"] == "22:00-07:00"
    assert environ["PROTAGINE_EMBED_PROVIDER"] == "openai_api"
    assert environ["PROTAGINE_EMBED_BASE_URL"] == "http://127.0.0.1:9/v1"
    assert environ["PROTAGINE_EMBED_MODEL"] == "e5"
    # An existing environment value wins over the file.
    assert environ["PROTAGINE_SIDECAR_PORT"] == "5000"
    assert "PROTAGINE_SIDECAR_PORT" not in applied


def test_apply_environment_without_embeddings_skips_the_embedder(home):
    save_config(config.DEFAULTS, home)
    environ: dict[str, str] = {}
    apply_environment(load_config(home, environ={}), environ=environ)
    assert environ["PROTAGINE_EMBED_PROVIDER"] == "skip"
    assert "PROTAGINE_API_KEY" not in environ
    # No reranker configured: nothing rerank-related is exported and recall keeps its default.
    assert not any(name.startswith("PROTAGINE_RERANKER_") for name in environ)
    assert "PROTAGINE_RECALL_RERANK" not in environ


def test_apply_environment_exports_a_configured_reranker(home):
    save_config({**config.DEFAULTS,
                 "router": {**config.DEFAULTS["router"],
                            "rerank_url": "http://127.0.0.1:8/v1", "rerank_model": "r1"}},
                home)
    environ: dict[str, str] = {}
    applied = apply_environment(load_config(home, environ={}), environ=environ)
    # The same code path the sidecar takes for the embedding endpoint: a remote
    # provider, the model it serves, and recall switched to use it.
    assert environ["PROTAGINE_RERANKER_PROVIDER"] == "openai_api"
    assert environ["PROTAGINE_RERANKER_BASE_URL"] == "http://127.0.0.1:8/v1"
    assert environ["PROTAGINE_RERANKER_MODEL"] == "r1"
    assert environ["PROTAGINE_RECALL_RERANK"] == "on"
    assert applied["PROTAGINE_RECALL_RERANK"] == "on"
    # A service unit or shell may still pin the recall mode (shadow measures before flipping).
    pinned: dict[str, str] = {"PROTAGINE_RECALL_RERANK": "shadow"}
    applied = apply_environment(load_config(home, environ={}), environ=pinned)
    assert pinned["PROTAGINE_RECALL_RERANK"] == "shadow"
    assert "PROTAGINE_RECALL_RERANK" not in applied
    assert pinned["PROTAGINE_RERANKER_BASE_URL"] == "http://127.0.0.1:8/v1"


def test_reranker_endpoint_needs_the_model_it_serves(home):
    # The sidecar cannot choose a reranker by itself, so an endpoint alone is a
    # configuration error rather than a silent no-op.
    with pytest.raises(ConfigError, match="router.rerank_model"):
        save_config({**config.DEFAULTS,
                     "router": {**config.DEFAULTS["router"], "rerank_url": "http://127.0.0.1:8/v1"}},
                    home)
    (home / config.CONFIG_FILE).write_text("router: {rerank_url: http://127.0.0.1:8/v1}\n")
    with pytest.raises(ConfigError, match="router.rerank_model"):
        load_config(home, environ={})
    # A model without an endpoint is kept in the file but exports nothing: the
    # remote path is the one this configuration describes, like embed_model.
    save_config({**config.DEFAULTS, "router": {**config.DEFAULTS["router"], "rerank_model": "r1"}}, home)
    environ: dict[str, str] = {}
    apply_environment(load_config(home, environ={}), environ=environ)
    assert "PROTAGINE_RERANKER_MODEL" not in environ
    assert "PROTAGINE_RECALL_RERANK" not in environ


def test_env_switches_are_plain(monkeypatch):
    monkeypatch.delenv("PROTAGINE_TEST_MODE", raising=False)
    assert env_choice("PROTAGINE_TEST_MODE", ("off", "live"), "off") == "off"
    monkeypatch.setenv("PROTAGINE_TEST_MODE", "LIVE")
    assert env_choice("PROTAGINE_TEST_MODE", ("off", "live"), "off") == "live"
    monkeypatch.setenv("PROTAGINE_TEST_MODE", "shadow")
    assert env_choice("PROTAGINE_TEST_MODE", ("off", "live"), "off") == "off"
    monkeypatch.setenv("PROTAGINE_TEST_FLAG", "yes")
    assert env_bool("PROTAGINE_TEST_FLAG") is True
    monkeypatch.setenv("PROTAGINE_TEST_FLAG", "0")
    assert env_bool("PROTAGINE_TEST_FLAG", True) is False
    monkeypatch.delenv("PROTAGINE_TEST_FLAG")
    assert env_bool("PROTAGINE_TEST_FLAG", True) is True
