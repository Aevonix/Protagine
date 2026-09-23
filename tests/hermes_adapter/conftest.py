"""Fixtures: a fake sidecar, a temporary Hermes home and subprocess probes.

Every probe runs stock Hermes (installed in this environment) in a fresh
interpreter with ``HERMES_HOME`` pointing at a temporary profile whose config
enables the adapter the way ``protagine init`` writes it. The sidecar is a
small HTTP server in the test process that records every request.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
API_KEY = "test-key-0123456789abcdef"
OWNER = "p-01"
CANARY = "OWNER-ONLY-CANARY-7f3a"
CONTACTS = {
    ("telegram", "1001"): {"contact_id": OWNER, "display_name": "Owner", "interaction_allowed": True,
                           "trust_tier": "GENESIS"},
    ("telegram", "2002"): {"contact_id": "p-02", "display_name": "Never", "interaction_allowed": False,
                           "trust_tier": "unknown"},
    ("telegram", "2003"): {"contact_id": "p-03", "display_name": "Friend", "interaction_allowed": True,
                           "trust_tier": "REGULAR"},
}


class FakeSidecar:
    """Just enough of ``/v1/host`` (and optionally ``/v1/mind``) for the adapter."""

    def __init__(self):
        self.requests: list[dict] = []
        self.unauthorized: list[str] = []
        self.mind_routes = False
        self.mind_enabled = True
        self.guard_verdict: dict = {"action": "allow"}
        self.contacts = {key: dict(value) for key, value in CONTACTS.items()}
        self.lock = threading.Lock()
        sidecar = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _route(self):
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                record = {"method": self.command, "path": parts.path, "query": query, "json": body,
                          "authorization": self.headers.get("Authorization", "")}
                with sidecar.lock:
                    sidecar.requests.append(record)
                if record["authorization"] != f"Bearer {API_KEY}":
                    with sidecar.lock:
                        sidecar.unauthorized.append(parts.path)
                    return self._reply(401, {"detail": "unauthorized"})
                status, reply = sidecar.dispatch(self.command, parts.path, query, body)
                self._reply(status, reply)

            do_GET = do_POST = do_PUT = do_PATCH = _route

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def calls(self, path: str, method: str | None = None) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if r["path"] == path and (method is None or r["method"] == method)]

    def dispatch(self, method, path, query, body):
        if path.startswith("/v1/mind/"):
            return self._mind(method, path, body)
        if path == "/v1/host/health":
            return 200, {"status": "ok", "capabilities": ["memory"]}
        if path == "/v1/host/contacts/resolve":
            contact = self.contacts.get((query.get("gateway", ""), query.get("address", "")))
            if contact is None and query.get("create") == "true" and query.get("address"):
                contact = {"contact_id": "p-" + query["address"][-2:], "display_name": query["address"],
                           "interaction_allowed": False, "trust_tier": "unknown"}
                self.contacts[(query.get("gateway", ""), query["address"])] = contact
            return (200, contact) if contact else (404, {"detail": "No contact for that handle"})
        if path == "/v1/host/contacts":
            return 200, {"contacts": list(self.contacts.values()), "total": len(self.contacts)}
        if path.startswith("/v1/host/contacts/"):
            wanted = path.rsplit("/", 1)[1]
            found = next((c for c in self.contacts.values() if c["contact_id"] == wanted), None)
            return (200, found) if found else (404, {"detail": "unknown contact"})
        if path == "/v1/host/turns/sync":
            if not isinstance(body, dict) or not body.get("context", {}).get("contact_id"):
                return 422, {"detail": "contact_id required"}
            return 200, {"accepted": True, "continuity_updated": True, "source_recorded": True}
        if path == "/v1/host/context/assemble":
            audience = body.get("audience") if isinstance(body, dict) else None
            shared = [{"id": "shared", "title": "Shared", "body": "shared facts", "priority": 50}]
            if audience == "viewer" or body.get("projection_policy"):
                return 200, {"sections": shared}
            return 200, {"sections": [{"id": "private", "title": "Owner notes", "body": CANARY, "priority": 90},
                                      *shared]}
        if path == "/v1/host/context/temporal":
            return 200, {"title": "Current Time", "body": "Sunday, 10:00 AM UTC"}
        if path == "/v1/host/memory/search":
            return 200, {"content": f"excerpt for {body.get('person_id')}", "count": 1,
                         "source_refs": [{"source_id": "src-1", "source_version": "a" * 64}],
                         "watermark": 0, "retrieval": {"semantic": "ready", "contact_facts": "ready"},
                         "annotation_checks": []}
        if path == "/v1/host/memory/sources/forget":
            return 200, {"source_erased": True, "watermark": 1}
        if path == "/v1/host/memory/sources/deadline":
            from datetime import datetime, timedelta, timezone
            deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            return 200, {"status": "current", "deadline_at": deadline, "subject": "the report",
                         "predicate": "is due", "value": "in two hours", "timezone_name": "UTC",
                         "timezone_basis": "caller_override", "claim_id": body.get("claim_id"),
                         "root_claim_id": body.get("claim_id"), "source_refs": []}
        return 404, {"detail": "not found"}

    def _mind(self, method, path, body):
        if not self.mind_routes:
            return 404, {"detail": "not found"}
        if path == "/v1/mind/status":
            return 200, {"enabled": self.mind_enabled, "autonomy": "standard", "queued": 0, "asks": 0}
        if path == "/v1/mind/guard":
            return 200, dict(self.guard_verdict)
        if path == "/v1/mind/off":
            self.mind_enabled = False
            return 200, {"ok": True}
        if path == "/v1/mind/log":
            return 200, {"text": "log entries"}
        if path == "/v1/mind/asks":
            return 200, {"text": "no open asks"}
        if path.startswith("/v1/mind/asks/"):
            _, code, answer = path.rsplit("/", 2)
            return 200, {"ok": True, "code": code, "answer": answer}
        if path.startswith("/v1/mind/people/"):
            return 200, {"ok": True, "may_contact": (body or {}).get("may_contact")}
        return 404, {"detail": "not found"}


@pytest.fixture
def sidecar():
    server = FakeSidecar().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def home(tmp_path, sidecar):
    """A Hermes profile configured the way ``protagine init`` writes it."""
    hermes, instance = tmp_path / "hermes", tmp_path / "protagine"
    hermes.mkdir()
    instance.mkdir()
    key_file = instance / "api.key"
    key_file.write_text(API_KEY + "\n")
    key_file.chmod(0o600)
    (instance / "protagine.yaml").write_text(yaml.safe_dump({
        "sidecar": {"host": "127.0.0.1", "port": sidecar.server.server_address[1]},
        "mind": {"enabled": True, "autonomy": "standard",
                 "deny": {"commands": ["rm -rf *"], "tools": ["terminal"], "text": ["secret-project-x"]}},
    }))
    (instance / "identity.yaml").write_text(yaml.safe_dump({
        "owner": {"name": "Owner", "contact_id": OWNER, "handles": {"telegram": ["1001"]}},
        "agent": {"name": "Agent", "values": ["care"]},
    }))
    config = {
        "plugins": {"enabled": ["protagine"], "hook_callback_timeout": 0,
                    "protagine": {"sidecar_url": sidecar.url, "key_file": str(key_file)}},
        "memory": {"provider": "protagine-memory", "config": {"contact_id": OWNER}},
        "kanban": {"dispatch_in_gateway": True},
    }
    (hermes / "config.yaml").write_text(yaml.safe_dump(config))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def write_config(**changes):
        merged = json.loads(json.dumps(config))
        for key, value in changes.items():
            merged[key] = value
        (hermes / "config.yaml").write_text(yaml.safe_dump(merged))

    def write_mind(**mind):
        current = yaml.safe_load((instance / "protagine.yaml").read_text())
        current["mind"].update(mind)
        (instance / "protagine.yaml").write_text(yaml.safe_dump(current))

    return SimpleNamespace(root=tmp_path, hermes=hermes, instance=instance, key_file=key_file,
                           workspace=workspace, config=config, write_config=write_config,
                           write_mind=write_mind)


PRELUDE = r'''
import json, os, sys, threading, time
def emit(**values):
    print("@@RESULT@@" + json.dumps(values, default=str), flush=True)
from hermes_cli.plugins import get_plugin_manager, _reset_plugin_managers_for_tests, VALID_HOOKS
from hermes_cli.lifecycle import invoke_hook
_reset_plugin_managers_for_tests()
manager = get_plugin_manager()
manager.discover_and_load(force=True)
loaded = manager._plugins["protagine"]
assert loaded.enabled, loaded.error
import protagine_hermes
'''

WORKER_ENV_KEYS = ("HERMES_KANBAN_TASK", "HERMES_PROFILE", "HERMES_KANBAN_WORKSPACE", "TERMINAL_CWD")


def worker_env(home, task_id="task-01", profile="protagine-act") -> dict[str, str]:
    """The variables the kanban dispatcher sets for a spawned worker."""
    return {"HERMES_KANBAN_TASK": task_id, "HERMES_PROFILE": profile,
            "HERMES_KANBAN_WORKSPACE": str(home.workspace), "TERMINAL_CWD": str(home.workspace)}


def probe(code: str, home, *, env: dict | None = None, timeout: float = 240, prelude: str = PRELUDE) -> dict:
    """Run ``code`` after the prelude in a fresh interpreter; return the emitted result."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("HERMES_", "PROTAGINE_", "TERMINAL_"))}
    environment.update({"HERMES_HOME": str(home.hermes), "HOME": str(home.root), "PYTHONUNBUFFERED": "1",
                        "PYTHON_DOTENV_DISABLED": "1"})
    environment.update(env or {})
    result = subprocess.run([sys.executable, "-c", prelude + code], env=environment, text=True,
                            capture_output=True, timeout=timeout, cwd=home.root, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stdout[-4000:] + "\n---\n" + result.stderr[-4000:]
    lines = [line for line in result.stdout.splitlines() if line.startswith("@@RESULT@@")]
    assert lines, result.stdout[-4000:] + "\n---\n" + result.stderr[-4000:]
    return json.loads(lines[-1][len("@@RESULT@@"):])


def run_python(*args, cwd, env=None):
    result = subprocess.run([sys.executable, *map(str, args)], cwd=cwd, env=env, text=True,
                            capture_output=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory):
    """The built wheel and sdist (or the prepared ones CI hands over)."""
    output = tmp_path_factory.mktemp("adapter-artifacts")
    prepared = os.environ.get("PROTAGINE_DISTRIBUTIONS_DIR")
    distributions = Path(prepared).resolve() if prepared else output
    if not prepared:
        run_python("-m", "build", "--no-isolation", "--outdir", output, cwd=ROOT)
    wheel, = distributions.glob("protagine_hermes-*.whl")
    source, = distributions.glob("protagine_hermes-*.tar.gz")
    return output, wheel, source
