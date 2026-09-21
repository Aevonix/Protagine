"""Inspect the selected Hermes runtime in a disposable, offline profile.

These bounded checks admit an attachment, not a release or a live deployment.
The complete installed-adapter qualification remains a separate requirement.
This file also runs directly under another interpreter; keep imports stdlib-only.
"""
from __future__ import annotations

import argparse
import ast
import contextvars
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

SCHEMA = "protagine.hermes-capabilities.v1"
FEATURES = {
    "core": ("plugin_callbacks", "memory_checkpoint", "request_authority",
             "native_input_identity", "owned_payload_erasure", "settled_native_turn", "durable_tasks"),
    "concurrent_work": ("overlapping_callbacks", "settled_gateway_turn"),
    "detached_review": ("detached_turn_observer",),
    "source_reminders": ("owned_cron_output",),
    "terminal_handoff": ("post_tool_batch",),
}


def missing_capabilities(report, features=("core",)):
    if not isinstance(report, dict) or report.get("schema") != SCHEMA or not isinstance(report.get("capabilities"), dict):
        raise ValueError("Invalid Hermes capability receipt")
    required = set()
    for feature in features:
        if feature not in FEATURES:
            raise ValueError(f"Unknown Hermes feature: {feature}")
        required.update(FEATURES[feature])
    return sorted(name for name in required if not isinstance(report["capabilities"].get(name), dict)
                  or report["capabilities"][name].get("available") is not True)


def require_capabilities(report, features=("core",)):
    missing = missing_capabilities(report, features)
    if missing:
        raise ValueError("Selected Hermes lacks required capabilities: " + ", ".join(missing)
                         + ". Select the qualified runtime; see docs/HERMES-CAPABILITIES.md. "
                         "The existing runtime and profile were not replaced.")


def probe_runtime(python, *, timeout=45):
    """Never import native plugins or inspect personal profiles in the installer process."""
    with tempfile.TemporaryDirectory(prefix="protagine-hermes-capabilities-") as temporary:
        home = Path(temporary)
        environment = {key: os.environ[key] for key in
                       ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        environment.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(home / "profile"),
                           HERMES_BUNDLED_PLUGINS=str(home / "empty-plugins"),
                           HERMES_ENABLE_PROJECT_PLUGINS="0", PYTHONDONTWRITEBYTECODE="1",
                           XDG_CACHE_HOME=str(home / "cache"), XDG_CONFIG_HOME=str(home / "config"))
        try:
            completed = subprocess.run([str(python), "-I", "-B", str(Path(__file__).resolve()), "--probe"],
                                       cwd=home, env=environment, capture_output=True, text=True, timeout=timeout)
            report = json.loads(completed.stdout.splitlines()[-1]) if completed.returncode == 0 else None
            if not isinstance(report, dict) or report.get("schema") != SCHEMA:
                raise ValueError()
            missing_capabilities(report)  # Validate the receipt before accepting it.
            return report
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
            raise ValueError("Could not inspect the selected Hermes interpreter. "
                             "Select its Python with native dependencies installed; "
                             "no existing profile was changed.") from None


def _probe():
    def no_network(*args, **kwargs):
        raise RuntimeError("Capability checks must stay offline")
    socket.socket.connect = no_network
    socket.create_connection = no_network
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True)
    Path(os.environ["HERMES_BUNDLED_PLUGINS"]).mkdir()
    result = {"schema": SCHEMA, "scope": "offline interface and bounded native behavior checks",
              "activation_state": "not_activated", "capabilities": {}}

    def check(name, action, evidence):
        try:
            action()
            result["capabilities"][name] = {"available": True, "evidence": evidence}
        except Exception as error:
            # Native import errors can contain personal configuration. Retain the type only.
            result["capabilities"][name] = {"available": False, "evidence": evidence,
                                             "error_type": type(error).__name__}

    try:
        distribution = importlib.metadata.distribution("hermes-agent")
        metadata = distribution.read_text("METADATA") or ""
        native = importlib.import_module("hermes_cli.plugins")
        root = Path(native.__file__).resolve().parent.parent
        revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=5)
        result["runtime"] = {"version": distribution.version,
                             "revision": revision.stdout.strip() if revision.returncode == 0 else None,
                             "metadata_sha256": hashlib.sha256(metadata.encode()).hexdigest()}
        patch_receipt = root / ".protagine-patch-receipt.json"
        if patch_receipt.is_file():
            patch = json.loads(patch_receipt.read_text())
            if patch.get("schema") == "protagine.hermes-patch-stage.v1":
                result["runtime"]["patchset"] = {key: patch.get(key) for key in
                    ("patchset_id", "official_revision", "source_revision", "manifest_sha256", "qualification_only")}
    except Exception as error:
        result["runtime"] = {"version": None, "revision": None, "error_type": type(error).__name__}

    def callbacks(*, overlap=False):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest
        manager = PluginManager(scope_key=str(home))
        context = PluginContext(PluginManifest(name="capability_probe", source="user"), manager)
        marker = contextvars.ContextVar("capability_probe_marker", default="missing")
        entered, release = threading.Event(), threading.Event()
        values, errors = {}, []
        def callback(**kwargs):
            value = marker.get()
            if overlap and value == "first":
                entered.set()
                if not release.wait(2):
                    raise AssertionError("probe release timed out")
            return value
        registration = context.register_hook("pre_llm_call", callback)
        assert registration is not None
        def invoke(value):
            token = marker.set(value)
            try:
                values[value] = manager.invoke_hook("pre_llm_call", session_id=value)
            except BaseException as error:
                errors.append(type(error).__name__)
            finally:
                marker.reset(token)
        if overlap:
            first = threading.Thread(target=invoke, args=("first",), daemon=True)
            second = threading.Thread(target=invoke, args=("second",), daemon=True)
            first.start()
            try:
                assert entered.wait(2)
                second.start()
                second.join(.1)
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
            assert not first.is_alive() and not second.is_alive()
            assert values == {"first": ["first"], "second": ["second"]}, values
        else:
            invoke("first"); invoke("second")
            assert values == {"first": ["first"], "second": ["second"]}, values
        assert not errors

    def memory():
        from agent.memory_provider import MemoryProvider, PRE_COMPRESS_CHECKPOINT_API_VERSION
        from agent.memory_manager import MemoryManager
        from plugins.memory import load_memory_provider
        assert PRE_COMPRESS_CHECKPOINT_API_VERSION >= 2
        assert callable(load_memory_provider)
        assert all(callable(getattr(MemoryProvider, name, None)) for name in
                   ("initialize", "prefetch", "sync_turn", "on_pre_compress"))
        assert callable(MemoryManager.on_pre_compress)

    def middleware():
        from hermes_cli.middleware import VALID_MIDDLEWARE, apply_llm_request_middleware
        assert {"llm_request", "tool_request", "tool_execution"} <= VALID_MIDDLEWARE
        request = {"messages": [{"role": "user", "content": "neutral probe"}]}
        response = apply_llm_request_middleware(request, session_id="probe")
        assert response.payload == request

    def native_input():
        from agent.turn_api_request import _native_user_message, build_api_request
        from hermes_state import SessionDB
        tree = ast.parse(inspect.getsource(build_api_request))
        assert any(isinstance(node, ast.Call) and
                   {"native_user_message", "original_user_message"} <= {arg.arg for arg in node.keywords}
                   for node in ast.walk(tree))
        db = SessionDB(home / "input.db")
        try:
            db.create_session("probe", "cli")
            row_id = db.append_message("probe", "user", "neutral probe")
            row = {"role": "user", "content": "neutral probe", "_row_id": row_id}
            agent = SimpleNamespace(_session_db=db, session_id="probe")
            assert _native_user_message(agent, [row], 0, "neutral probe", "neutral probe") == row
            assert _native_user_message(agent, [row], 0, "changed", "changed") is None
            assert _native_user_message(agent, [row], 1, "neutral probe", "neutral probe") is None
        finally:
            db.close()

    def erasure():
        from hermes_state import SessionDB
        assert callable(SessionDB.message_redaction_snapshot)
        assert "expected_message_watermark" in inspect.signature(SessionDB.redact_message_payloads).parameters
        db = SessionDB(home / "erasure.db")
        try:
            db.create_session("probe", "cli")
            erased = db.append_message("probe", "user", "neutral owned payload")
            retained = db.append_message("probe", "user", "unrelated retained payload")
            rows = db.get_message_redaction_snapshot("probe", [erased])
            wrong = [{**rows[0], "sha256": "0" * 64}]
            try:
                db.redact_message_payloads("probe", wrong, expected_message_watermark=retained)
            except ValueError:
                pass
            else:
                raise AssertionError("changed preimage accepted")
            receipt = db.redact_message_payloads("probe", rows, expected_message_watermark=retained)
            assert receipt["redacted_ids"] == [erased]
            messages = {row["id"]: row for row in db.get_messages("probe")}
            assert messages[erased]["content"] != "neutral owned payload"
            assert messages[retained]["content"] == "unrelated retained payload"
            assert db.redact_message_payloads("probe", rows)["redacted_ids"] == []
        finally:
            db.close()

    def hook(name):
        from hermes_cli.plugins import VALID_HOOKS
        assert name in VALID_HOOKS

    def tasks():
        from hermes_cli import kanban_db as kanban
        path = home / "tasks.db"
        db = kanban.connect(path)
        try:
            task_id = kanban.create_task(db, title="neutral probe", workspace_kind="scratch",
                                         idempotency_key="probe")
            assert kanban.create_task(db, title="neutral probe", workspace_kind="scratch",
                                      idempotency_key="probe") == task_id
            assert kanban.claim_task(db, task_id, claimer="probe:worker") is not None
            assert kanban.claim_task(db, task_id, claimer="probe:duplicate") is None
        finally:
            db.close()
        db = kanban.connect(path)
        try:
            assert kanban.get_task(db, task_id).status == "running"
            # Expire only our synthetic claim. There is no worker process to signal.
            db.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (task_id,))
            db.commit()
            assert kanban.release_stale_claims(db, signal_fn=lambda *args: None) == 1
            assert kanban.get_task(db, task_id).status == "ready"
        finally:
            db.close()

    def cron():
        from cron import owned_output
        assert all(callable(getattr(owned_output, name, None)) for name in ("payload_fingerprint", "snapshot", "erase"))

    def handoff():
        hook("post_tool_batch")
        from hermes_cli.tool_completion import FinishTurn
        assert callable(FinishTurn)

    check("plugin_callbacks", callbacks, "native registration and serial caller-context round trip")
    check("memory_checkpoint", memory, "native provider discovery and checkpoint-v2 interfaces")
    check("request_authority", middleware, "native request/execution middleware and unchanged-request round trip")
    check("native_input_identity", native_input, "real SQLite row identity, mismatched input/index rejection, request wiring")
    check("owned_payload_erasure", erasure, "real SQLite preimage rejection, selective erasure and idempotent replay")
    check("settled_native_turn", lambda: hook("on_native_turn_settled"), "declared post-persistence observer; full suite checks delivery")
    check("durable_tasks", tasks, "real task persistence, duplicate admission/claim rejection, restart and stale-claim recovery")
    check("overlapping_callbacks", lambda: callbacks(overlap=True), "two overlapping native callbacks retain distinct caller context")
    check("settled_gateway_turn", lambda: hook("on_gateway_turn_settled"), "declared gateway-settled observer; full suite checks ownership")
    check("detached_turn_observer", lambda: hook("on_detached_turn_end"), "declared detached-end observer; full suite checks terminal outcomes")
    check("owned_cron_output", cron, "native exact-job snapshot/erase interface; full suite checks retained copies")
    check("post_tool_batch", handoff, "typed native terminal-handoff interface; full suite checks accepted tool receipts")
    result["features"] = {name: {"available": not missing_capabilities(result, (name,)),
                                "missing": missing_capabilities(result, (name,))} for name in FEATURES}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require", action="append", choices=tuple(FEATURES), default=[])
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    report = _probe() if args.probe else probe_runtime(args.python)
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        with args.output.open("x") as destination:
            destination.write(encoded)
    print(json.dumps(report))
    if args.require:
        try:
            require_capabilities(report, args.require)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
