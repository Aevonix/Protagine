"""The skill sandbox runner must not inherit the sidecar's environment.

The sidecar process holds API keys and provider credentials in its env; a
skill subprocess gets only what it needs to start plus the variables its
manifest declares.
"""

from __future__ import annotations

import asyncio
import json
import textwrap

import pytest

from tests.test_skill_sandbox import _build_skill, _make_executor


class _FakeProc:
    returncode = 0

    async def communicate(self, input=None):
        return json.dumps({"status": "success", "output": 1, "peak_memory_kb": 0}).encode(), b""


@pytest.fixture
def spawn_env(monkeypatch):
    """Capture the ``env=`` the executor hands to the runner process."""
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs.get("env") or {})
        captured["__args__"] = args
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return captured


@pytest.mark.asyncio
async def test_runner_does_not_inherit_api_keys(tmp_path, monkeypatch, spawn_env):
    monkeypatch.setenv("PROTAGINE_API_KEY", "sidecar-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    manifest = _build_skill(tmp_path, "def run(**kw): return 1")
    executor, _ = _make_executor(manifest)

    result = await executor.invoke(manifest.skill_id, {})

    assert result.status == "success"
    assert "__args__" in spawn_env, "runner was not spawned through create_subprocess_exec"
    assert "PROTAGINE_API_KEY" not in spawn_env
    assert "OPENAI_API_KEY" not in spawn_env
    assert spawn_env["PATH"] == "/usr/bin:/bin"


@pytest.mark.asyncio
async def test_declared_env_vars_pass_through(tmp_path, monkeypatch, spawn_env):
    monkeypatch.setenv("SKILL_TOKEN", "declared")
    monkeypatch.setenv("OTHER_TOKEN", "undeclared")
    manifest = _build_skill(tmp_path, "def run(**kw): return 1")
    manifest.permissions.allowed_env_vars = ["SKILL_TOKEN", "NOT_SET_ANYWHERE"]
    executor, _ = _make_executor(manifest)

    await executor.invoke(manifest.skill_id, {})

    assert spawn_env["SKILL_TOKEN"] == "declared"
    assert "OTHER_TOKEN" not in spawn_env
    assert "NOT_SET_ANYWHERE" not in spawn_env


@pytest.mark.asyncio
async def test_real_runner_starts_and_sees_a_minimal_env(tmp_path, monkeypatch):
    """End to end: the runner still imports and runs with the reduced env,
    and the skill itself cannot read the sidecar's key."""
    monkeypatch.setenv("PROTAGINE_API_KEY", "sidecar-secret")
    source = textwrap.dedent("""
        import os
        def run(**kw):
            return {"key": os.environ.get("PROTAGINE_API_KEY"), "n": len(os.environ)}
    """)
    manifest = _build_skill(tmp_path, source)
    manifest.permissions.allowed_imports = ["os"]
    executor, _ = _make_executor(manifest)

    result = await executor.invoke(manifest.skill_id, {})

    assert result.status == "success", result.error
    assert result.output["key"] is None
    from protagine.skills.executor import _CHILD_ENV_KEYS
    assert result.output["n"] <= len(_CHILD_ENV_KEYS)
