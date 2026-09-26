"""A mind task's worker caps every model request it makes (the ``protagine-act`` profile).

Live: on the first autonomous task the worker's second model call ran away, about 20K tokens until
the run was killed at its time limit. Nothing capped the worker's requests: stock Hermes sends an
output limit or a sampling field to a custom endpoint only when that endpoint's provider entry
carries it in ``extra_body``, and the profile copied the main entries as they were.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import yaml

from protagine import init
from protagine.config import load_config
from test_init import HERMES_PYTHON, STOCK_CONFIG, _args, homes  # noqa: F401  (fixture)

CAP = {"max_tokens": 8192, "top_p": 0.95}
NAMED = {**STOCK_CONFIG, "model": {"provider": "house", "default": "house-model"},
         "providers": {"house": {"base_url": "http://127.0.0.1:9/v1", "api_key": "house-secret",
                                 "default_model": "house-model",
                                 "extra_body": {"chat_template_kwargs": {"enable_thinking": False}, "top_p": 0.8}}},
         "custom_providers": [{"name": "spare", "base_url": "http://127.0.0.1:10/v1", "model": "spare-model"}]}

# What stock Hermes puts on the wire for a dispatched worker: the runtime it resolves in the profile's
# home and the agent the CLI builds from it (``hermes -p protagine-act chat -q``), then its request.
PROBE = """
import json
from hermes_cli.config import load_config
from hermes_cli.runtime_provider import resolve_runtime_provider
from run_agent import AIAgent
from agent.chat_completion_helpers import build_api_kwargs
runtime = resolve_runtime_provider()
agent = AIAgent(model=runtime.get("model") or (load_config().get("model") or {}).get("default"),
                api_key=runtime.get("api_key"), base_url=runtime.get("base_url"), provider=runtime.get("provider"),
                requested_provider=runtime.get("requested_provider"), api_mode=runtime.get("api_mode"),
                quiet_mode=True, enabled_toolsets=[], skip_context_files=True, skip_memory=True)
request = build_api_kwargs(agent, [{"role": "user", "content": "hello"}])
print(json.dumps({"base_url": runtime.get("base_url"), "extra_body": request.get("extra_body")}))
"""


def _profile(tmp_path, main, **request):
    home = tmp_path / "protagine"
    home.mkdir(parents=True, exist_ok=True)
    if request:
        (home / "protagine.yaml").write_text(yaml.safe_dump({"mind": {"worker_request": request}}))
    cfg = load_config(home, environ={})
    return init.worker_profile_config(main, cfg, sidecar_url="http://127.0.0.1:7901", key_file=home / "api.key")


def test_every_endpoint_the_worker_may_use_carries_the_cap(tmp_path):
    main = copy.deepcopy(NAMED)
    profile = _profile(tmp_path, main)
    assert profile["providers"]["house"]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}, "top_p": 0.95, "max_tokens": 8192}
    assert profile["custom_providers"] == [{"name": "spare", "base_url": "http://127.0.0.1:10/v1",
                                            "model": "spare-model", "extra_body": CAP}]
    assert profile["model"] == NAMED["model"]
    assert main == NAMED                                                # the main profile is never touched


def test_a_direct_endpoint_gets_an_entry_that_carries_the_cap(tmp_path):
    profile = _profile(tmp_path, copy.deepcopy(STOCK_CONFIG))
    assert profile["model"] == STOCK_CONFIG["model"]
    assert profile["custom_providers"] == [{"name": init.WORKER_PROFILE, "base_url": "http://127.0.0.1:9/v1",
                                            "extra_body": CAP}]


def test_protagine_yaml_sets_the_fields_and_null_leaves_one_to_the_provider(tmp_path):
    profile = _profile(tmp_path, copy.deepcopy(NAMED), max_tokens=4096, top_p=None, temperature=0.6)
    assert profile["providers"]["house"]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}, "top_p": 0.8, "max_tokens": 4096, "temperature": 0.6}


def _wire(profile_home: Path) -> dict:
    result = subprocess.run([HERMES_PYTHON, "-c", PROBE], capture_output=True, text=True, timeout=180,
                            env={**os.environ, "HERMES_HOME": str(profile_home)})
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_stock_hermes_sends_the_cap_with_every_worker_request(tmp_path):
    for name, main in (("named", NAMED), ("direct", STOCK_CONFIG)):
        root = tmp_path / name / "profiles"
        init.write_worker_profile(root, _profile(tmp_path / name, copy.deepcopy(main)))
        sent = _wire(root / init.WORKER_PROFILE)
        assert sent["base_url"] == "http://127.0.0.1:9/v1", name
        assert {key: sent["extra_body"].get(key) for key in CAP} == CAP, (name, sent)


def test_upgrade_writes_the_cap_into_an_existing_install(homes, capsys):  # noqa: F811
    home, hermes_home = homes
    assert init.run_init(_args(home, hermes_home)) == 0
    path = hermes_home / "profiles" / init.WORKER_PROFILE / "config.yaml"
    before = yaml.safe_load(path.read_text())
    before.pop("custom_providers", None)                                # the profile an earlier release wrote
    path.write_text(yaml.safe_dump(before, sort_keys=False))
    capsys.readouterr()
    upgrade = SimpleNamespace(home=str(home), hermes_home=None, hermes_python=HERMES_PYTHON, adapter_source=None)
    assert init.run_upgrade(upgrade) == 0
    assert f"profiles/{init.WORKER_PROFILE} written" in capsys.readouterr().out
    after = yaml.safe_load(path.read_text())
    assert after["custom_providers"][0]["extra_body"] == CAP
    assert {key: _wire(path.parent)["extra_body"].get(key) for key in CAP} == CAP
