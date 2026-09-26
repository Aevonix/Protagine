"""``protagine.yaml``: defaults, the file, the environment overrides and the key file."""

from __future__ import annotations

import json
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


def test_the_affect_mechanism_switch_ships_off_is_a_binary_faculty_and_has_no_environment_variable(home):
    """``mind.faculties.affect_rules`` (build plan M6): the stateless-rules arm of the affect family."""
    assert config.DEFAULTS["mind"]["faculties"]["affect_rules"] is False
    assert load_config(home, environ={"PROTAGINE_MIND_AFFECT_RULES": "on"}).get("mind.faculties.affect_rules") is False
    assert not any("AFFECT" in name for name in config.ENV_OVERRIDES)
    (home / "protagine.yaml").write_text(yaml.safe_dump({"mind": {"faculties": {"affect_rules": "on"}}}))
    cfg = load_config(home, environ={})
    assert cfg.get("mind.faculties.affect_rules") is True and cfg.get("mind.faculties.affect") is True
    (home / "protagine.yaml").write_text(yaml.safe_dump({"mind": {"faculties": {"affect_rules": "maybe"}}}))
    with pytest.raises(ConfigError, match="mind.faculties.affect_rules"):
        load_config(home, environ={})


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
    # Values reach the appraisal prompt from identity.yaml itself (chosen_values), not through the environment.
    assert "PROTAGINE_AGENT_VALUES" not in environ
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


def test_apply_environment_honours_the_semantic_recall_flag(home):
    """``mind.faculties.semantic_recall`` is a real binary switch: off, the embedder stays off even with an
    endpoint recorded (the ``full-semantic_recall`` arm); the endpoint itself is still exported."""
    save_config({**config.DEFAULTS,
                 "router": {**config.DEFAULTS["router"], "embed_url": "http://127.0.0.1:9/v1", "embed_model": "e5"},
                 "mind": {**config.DEFAULTS["mind"],
                          "faculties": {**config.DEFAULTS["mind"]["faculties"], "semantic_recall": False}}},
                home)
    environ: dict[str, str] = {}
    apply_environment(load_config(home, environ={}), environ=environ)
    assert environ["PROTAGINE_EMBED_PROVIDER"] == "skip"
    assert environ["PROTAGINE_EMBED_BASE_URL"] == "http://127.0.0.1:9/v1"
    # A pinned process environment still wins over the derived value.
    pinned: dict[str, str] = {"PROTAGINE_EMBED_PROVIDER": "openai_api"}
    apply_environment(load_config(home, environ={}), environ=pinned)
    assert pinned["PROTAGINE_EMBED_PROVIDER"] == "openai_api"


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


def test_embed_dims_is_exported_only_when_declared(home):
    """Without a declared width the endpoint's first embedding defines it; a declared one is validated."""
    (home / "protagine.yaml").write_text(yaml.safe_dump({
        "router": {"embed_url": "http://127.0.0.1:8092", "embed_model": "an-embedding-model"},
    }))
    environ = {}
    apply_environment(load_config(home, environ={}), environ=environ)
    assert "PROTAGINE_EMBED_DIMS" not in environ
    assert environ["PROTAGINE_EMBED_PROVIDER"] == "openai_api"

    (home / "protagine.yaml").write_text(yaml.safe_dump({
        "router": {"embed_url": "http://127.0.0.1:8092", "embed_model": "an-embedding-model", "embed_dims": 4096},
    }))
    environ = {}
    apply_environment(load_config(home, environ={}), environ=environ)
    assert environ["PROTAGINE_EMBED_DIMS"] == "4096"

    pinned = {"PROTAGINE_EMBED_DIMS": "1024"}
    apply_environment(load_config(home, environ={}), environ=pinned)
    assert pinned["PROTAGINE_EMBED_DIMS"] == "1024"


@pytest.mark.parametrize("value, message", [
    ("many", "router.embed_dims"),
    (-1, "router.embed_dims"),
    (True, "router.embed_dims"),
])
def test_embed_dims_must_be_a_whole_number(home, value, message):
    (home / "protagine.yaml").write_text(yaml.safe_dump({"router": {"embed_dims": value}}))
    with pytest.raises(ConfigError) as info:
        load_config(home, environ={})
    assert message in str(info.value)
    (home / "protagine.yaml").write_text(yaml.safe_dump({"router": {"embed_dims": "2048"}}))
    assert load_config(home, environ={}).get("router.embed_dims") == 2048


def test_environment_mapping_lands_over_derived_values_and_under_the_process_environment(home, caplog):
    """Any PROTAGINE_* tuning the sidecar reads travels in the file; the process still wins."""
    import logging
    (home / "protagine.yaml").write_text(yaml.safe_dump({
        "router": {"rerank_url": "http://127.0.0.1:8093", "rerank_model": "a-reranker"},
        "environment": {
            "PROTAGINE_RERANKER_PROMPT_STYLE": "qwen3",
            "PROTAGINE_RECALL_RERANK_MIN_SCORE": 0.7362908869981766,
            "PROTAGINE_RECALL_OVERSAMPLE": 5,
            "PROTAGINE_RECALL_STRENGTH_RANKING": "on",
            "PROTAGINE_RECALL_RERANK": "shadow",
            "PROTAGINE_EMBED_API_KEY": "s3cret-value",
        },
    }))
    cfg = load_config(home, environ={})
    assert cfg.get("environment.PROTAGINE_RECALL_OVERSAMPLE") == "5"
    environ = {"PROTAGINE_RECALL_OVERSAMPLE": "2"}
    with caplog.at_level(logging.INFO, logger="protagine.config"):
        applied = apply_environment(cfg, environ=environ)
    assert environ["PROTAGINE_RERANKER_PROMPT_STYLE"] == "qwen3"
    assert environ["PROTAGINE_RECALL_RERANK_MIN_SCORE"] == "0.7362908869981766"
    assert environ["PROTAGINE_RECALL_STRENGTH_RANKING"] == "on"
    assert environ["PROTAGINE_RECALL_RERANK"] == "shadow"          # the mapping over the derived "on"
    assert environ["PROTAGINE_RERANKER_BASE_URL"] == "http://127.0.0.1:8093"
    assert environ["PROTAGINE_RECALL_OVERSAMPLE"] == "2"           # the process environment over the mapping
    assert environ["PROTAGINE_EMBED_API_KEY"] == "s3cret-value"
    assert "PROTAGINE_RECALL_OVERSAMPLE" not in applied
    # The log names what was exported and keeps a credential's value and name out of it.
    assert "PROTAGINE_RERANKER_PROMPT_STYLE" in caplog.text
    assert "s3cret-value" not in caplog.text and "PROTAGINE_EMBED_API_KEY" not in caplog.text
    assert "1 credential entry (names withheld)" in caplog.text


@pytest.mark.parametrize("entry, message", [
    ({"OPENAI_API_KEY": "x"}, "PROTAGINE_ followed by"),
    ({"protagine_recall_oversample": "5"}, "PROTAGINE_ followed by"),
    ({"PROTAGINE_": "5"}, "PROTAGINE_ followed by"),
    ({"HERMES_HOME": "/elsewhere"}, "hermes.home"),
    ({"PROTAGINE_SIDECAR_PORT": "8000"}, "sidecar.port"),
    ({"PROTAGINE_EMBED_DIMS": "4096"}, "router.embed_dims"),
    ({"PROTAGINE_API_KEY": "k"}, "api.key"),
    ({"PROTAGINE_OWNER_NAME": "Ada"}, "identity.yaml"),
    ({"PROTAGINE_RECALL_STRENGTH_RANKING": True}, 'quote it ("on")'),
    ({"PROTAGINE_RECALL_STRENGTH_RANKING": False}, 'quote it ("off")'),
    ({"PROTAGINE_RECALL_OVERSAMPLE": None}, "string or a number"),
    ({"PROTAGINE_RECALL_OVERSAMPLE": [5]}, "string or a number"),
    ({"PROTAGINE_RECALL_OVERSAMPLE": ""}, "empty"),
    ({"PROTAGINE_RECALL_OVERSAMPLE": "5\n"}, "control characters"),
])
def test_environment_entries_are_validated(home, entry, message):
    (home / "protagine.yaml").write_text(yaml.safe_dump({"environment": entry}))
    with pytest.raises(ConfigError) as info:
        load_config(home, environ={})
    assert message in str(info.value)
    (home / "protagine.yaml").write_text(yaml.safe_dump({"environment": "PROTAGINE_X=1"}))
    with pytest.raises(ConfigError, match="mapping"):
        load_config(home, environ={})


def test_reserved_names_cover_every_export_the_keys_derive(home):
    """The refusal list and apply_environment cannot drift apart; PROTAGINE_RECALL_RERANK is the one
    derived export the mapping may restate (to measure a reranker as ``shadow``)."""
    save_identity({"owner": {"name": "Ada"}, "agent": {"name": "Sol", "values": ["care"], "timezone": "UTC",
                                                       "quiet_hours": "22:00-07:00"}}, home)
    write_api_key("private-secret", home)
    (home / "protagine.yaml").write_text(yaml.safe_dump({
        "router": {"embed_url": "http://127.0.0.1:8092", "embed_model": "m", "embed_dims": 8,
                   "rerank_url": "http://127.0.0.1:8093", "rerank_model": "r"},
        "owner": {"contact_id": "cid-1"},
    }))
    environ = {}
    apply_environment(load_config(home, environ={}), environ=environ)
    derived = set(environ) - {"HERMES_HOME", "PROTAGINE_RECALL_RERANK"}
    assert derived <= set(config.RESERVED_ENVIRONMENT), derived - set(config.RESERVED_ENVIRONMENT)
    assert "PROTAGINE_HOME" in derived and "PROTAGINE_EMBED_DIMS" in derived


# -- the fast decision layer (protagine.decisions) ------------------------------------------------------------

def test_the_decision_section_is_exported_only_with_an_endpoint(home):
    environ: dict[str, str] = {}
    apply_environment(load_config(home, environ={}), environ=environ)
    assert not any(name.startswith("PROTAGINE_DECISIONS") for name in environ)
    save_config({**config.DEFAULTS, "decisions": {
        "url": "http://127.0.0.1:9/", "timeout_ms": 300,
        "points": {"opt_out": {"enabled": "on", "temperature": 2, "abstain": [0.1, 0.95]}}}}, home)
    loaded = load_config(home, environ={})
    assert loaded.get("decisions.points.opt_out") == {"enabled": True, "temperature": 2.0, "abstain": [0.1, 0.95]}
    environ = {}
    apply_environment(loaded, environ=environ)
    assert environ["PROTAGINE_DECISIONS_URL"] == "http://127.0.0.1:9/"
    assert environ["PROTAGINE_DECISIONS_TIMEOUT_MS"] == "300"
    assert json.loads(environ["PROTAGINE_DECISIONS_POINTS"]) == {
        "opt_out": {"enabled": True, "temperature": 2.0, "abstain": [0.1, 0.95]}}
    from protagine.decisions import from_environment
    decider = from_environment(environ)
    assert decider.enabled("opt_out") and decider.timeout_s == pytest.approx(0.3)


@pytest.mark.parametrize("section,message", [
    ({"url": "ftp://host"}, "decisions.url"),
    ({"timeout_ms": 0}, "decisions.timeout_ms"),
    ({"timeout_ms": 60000}, "decisions.timeout_ms"),
    ({"points": {"anything": {"enabled": True}}}, "decisions.points.anything"),
    ({"points": {"opt_out": {"temperature": 0}}}, "decisions.points.opt_out.temperature"),
    ({"points": {"opt_out": {"abstain": [0.9, 0.1]}}}, "decisions.points.opt_out.abstain"),
    ({"points": {"opt_out": {"abstain": [0.1]}}}, "decisions.points.opt_out.abstain"),
    ({"points": {"opt_out": {"threshold": 0.5}}}, "decisions.points.opt_out"),
    ({"points": {"opt_out": {"enabled": "maybe"}}}, "decisions.points.opt_out.enabled"),
])
def test_a_malformed_decision_section_is_refused_by_name(home, section, message):
    (home / config.CONFIG_FILE).write_text(yaml.safe_dump({"decisions": section}))
    with pytest.raises(ConfigError, match=message.replace(".", r"\.")):
        load_config(home, environ={})


def test_the_decision_environment_has_one_place(home):
    for name in ("PROTAGINE_DECISIONS_URL", "PROTAGINE_DECISIONS_TIMEOUT_MS", "PROTAGINE_DECISIONS_POINTS"):
        (home / config.CONFIG_FILE).write_text(yaml.safe_dump({"environment": {name: "x"}}))
        with pytest.raises(ConfigError, match="decisions"):
            load_config(home, environ={})
