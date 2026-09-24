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
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
API_KEY = "test-key-0123456789abcdef"
OWNER = "p-01"
CANARY = "OWNER-ONLY-CANARY-7f3a"
CONTACTS = {
    ("telegram", "1001"): {"contact_id": OWNER, "display_name": "Owner", "may_contact": "auto",
                           "trust_tier": "GENESIS"},
    ("telegram", "2002"): {"contact_id": "p-02", "display_name": "Never", "may_contact": "never",
                           "trust_tier": "unknown"},
    ("telegram", "2003"): {"contact_id": "p-03", "display_name": "Friend", "may_contact": "ask",
                           "trust_tier": "REGULAR", "cadence_minutes": None,
                           "digest": "Friend: known since spring; " + CANARY},
}
PUBLIC_PERSON = ("contact_id", "display_name", "trust_tier")


class FakeMind:
    """The ``/v1/mind`` contract the adapter's body, tools and guard rely on.

    Intentions live in ``intentions`` keyed by id with a lifecycle ``status``
    (``asked``, ``approved``, ``dispatched``, ``done``...). ``GET dispatch``
    offers the approved tasks; ``bound`` moves them to ``dispatched`` unless
    ``lose_bound`` simulates a lost ack. Outbox messages live in ``outbox``
    with ``state`` ``ready | sending | sent | failed | uncertain``;
    ``relist_sending`` makes the fake re-offer a message already in
    ``sending``, the sloppy sidecar the body must tolerate.
    """

    def __init__(self):
        self.enabled = True
        self.autonomy = "standard"
        self.narrative: dict = {"enabled": False, "text": "", "sections": {}, "cites": [], "updated_at": None}
        self.intentions: dict[str, dict] = {}
        self.outbox: dict[str, dict] = {}
        self.guard_verdict: dict = {"allow": True, "reason": ""}
        self.lose_bound = False
        self.relist_sending = False
        self.bound: list[dict] = []
        self.outcomes: list[dict] = []
        self.observations: list[dict] = []
        self.decisions: list[dict] = []
        self.rates: list[dict] = []
        self.sending: list[dict] = []
        self.sent: list[dict] = []
        self.pulls = 0
        self.last_pull_at = None

    def handle(self, method, path, body, query=None):
        import time as _time
        query = query or {}
        parts = path.split("/")[3:]  # after /v1/mind
        head = parts[0] if parts else ""
        if head == "state" and method == "GET":
            asks = [{"id": i["id"], "code": i.get("ask_code"), "title": i.get("title"), "expires_at": i.get("expires_at")}
                    for i in self.intentions.values() if i.get("status") == "asked"]
            return 200, {"enabled": self.enabled, "autonomy": self.autonomy, "level": self.autonomy,
                         "queued": len([i for i in self.intentions.values() if i.get("status") == "approved"]),
                         "asks": asks, "last_tick_at": None, "body": {"last_pull_at": self.last_pull_at}}
        if head == "dispatch" and method == "GET":
            self.pulls += 1
            self.last_pull_at = _time.time()
            if not self.enabled:
                return 200, {"intentions": []}
            offered = [dict(i) for i in self.intentions.values()
                       if i.get("status") == "approved" and i.get("kind", "task") == "task"]
            for item in offered:
                item.setdefault("dedup_key", "mind:" + item["id"])
                item.setdefault("assignee", "protagine-act")
            return 200, {"intentions": offered}
        if head == "dispatch" and len(parts) == 3 and parts[2] == "bound" and method == "POST":
            intention = self.intentions.get(parts[1])
            if intention is None:
                return 404, {"detail": "unknown intention"}
            self.bound.append({"id": parts[1], **(body or {})})
            if not self.lose_bound:
                intention["status"] = "dispatched"
                intention["hermes_ref"] = (body or {}).get("hermes_ref")
            return 200, {"ok": True, "id": parts[1], "status": intention["status"]}
        if head == "outbox" and len(parts) == 1 and method == "GET":
            if not self.enabled:
                return 200, {"messages": []}
            states = {"ready", "sending"} if self.relist_sending else {"ready"}
            return 200, {"messages": [dict(m) for m in self.outbox.values() if m.get("state", "ready") in states]}
        if head == "outbox" and len(parts) == 3 and method == "POST":
            message = self.outbox.get(parts[1])
            if message is None:
                return 404, {"detail": "unknown message"}
            if parts[2] == "sending":
                self.sending.append({"id": parts[1], **(body or {})})
                if message.get("state", "ready") != "ready":
                    return 409, {"detail": f"message is {message['state']}"}
                message["state"] = "sending"
                return 200, {"ok": True, "state": "sending"}
            if parts[2] == "sent":
                self.sent.append({"id": parts[1], **(body or {})})
                message["state"] = (body or {}).get("result") or "uncertain"
                return 200, {"ok": True, "state": message["state"]}
        if head == "outcome" and method == "POST":
            intention = self.intentions.get(str((body or {}).get("id")))
            if intention is None:
                return 404, {"detail": "unknown intention"}
            self.outcomes.append(dict(body))
            intention["outcome"] = body.get("outcome")
            if body.get("final"):
                intention["status"] = body.get("outcome")
            return 200, {"ok": True}
        if head == "observations" and method == "POST":
            self.observations.append(dict(body or {}))
            return 200, {"ok": True}
        if head == "guard" and method == "POST":
            return 200, dict(self.guard_verdict)
        if head == "decide" and method == "POST":
            self.decisions.append(dict(body or {}))
            code = str((body or {}).get("code") or "")
            intention = next((i for i in self.intentions.values() if i.get("ask_code") == code and i.get("status") == "asked"), None)
            if intention is None:
                return 404, {"detail": "no open ask with that code"}
            intention["status"] = "approved" if body.get("answer") == "yes" else "denied"
            return 200, {"ok": True, "id": intention["id"], "status": intention["status"]}
        if head == "rate" and method == "POST":
            self.rates.append(dict(body or {}))
            return 200, {"ok": True, **(body or {})}
        if head == "log" and method == "GET":
            statuses = {s for s in query.get("status", "").split(",") if s}
            kinds = {k for k in query.get("kind", "").split(",") if k}
            rows = [i for i in self.intentions.values()
                    if (not statuses or i.get("status") in statuses) and (not kinds or i.get("kind", "task") in kinds)
                    and (not query.get("recipient") or i.get("recipient") == query["recipient"])]
            return 200, {"entries": [{"id": i["id"], "kind": i.get("kind", "task"), "status": i.get("status"),
                                      "title": i.get("title"), "decision": i.get("decision"),
                                      "recipient": i.get("recipient")} for i in rows]}
        if head == "narrative" and method == "GET":
            return (200, dict(self.narrative)) if self.narrative is not None else (500, {"detail": "narrative failed"})
        if head == "why" and len(parts) == 2 and method == "GET":
            intention = self.intentions.get(parts[1])
            return (200, dict(intention)) if intention else (404, {"detail": {
                "code": "unknown_intention", "message": f"no intention {parts[1]} exists in the audit log"}})
        if head == "off" and method == "POST":
            self.enabled = False
            for message in self.outbox.values():
                if message.get("state", "ready") == "ready":
                    message["state"] = "cancelled"
            return 200, {"ok": True, "enabled": False}
        if head == "on" and method == "POST":
            self.enabled = True
            return 200, {"ok": True, "enabled": True}
        if head == "tick" and method == "POST":
            return 200, {"ok": True}
        return 404, {"detail": "not found"}


class FakeSidecar:
    """Just enough of ``/v1/host`` (and optionally ``/v1/mind``) for the adapter."""

    def __init__(self):
        self.requests: list[dict] = []
        self.unauthorized: list[str] = []
        self.mind_routes = False
        self.mind = FakeMind()
        self.contacts = {key: dict(value) for key, value in CONTACTS.items()}
        self.proposals: list[dict] = []
        self.people_owner = OWNER   # whom the sidecar knows as the owner (its own check)
        self.told: dict[str, list[str]] = {}   # what each contact said in synced turns, recalled by contact
        self.delays: dict[str, float] = {}     # path -> seconds the answer is held (a slow route)
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
                if sidecar.delays.get(parts.path):
                    time.sleep(sidecar.delays[parts.path])
                self._reply(status, reply)

            do_GET = do_POST = do_PUT = do_PATCH = _route

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # a probe that exits mid-request (the crash tests) resets its socket

        self.server = Server(("127.0.0.1", 0), Handler)
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

    @property
    def mind_enabled(self) -> bool:
        return self.mind.enabled

    @mind_enabled.setter
    def mind_enabled(self, value: bool) -> None:
        self.mind.enabled = bool(value)

    @property
    def guard_verdict(self) -> dict:
        return self.mind.guard_verdict

    @guard_verdict.setter
    def guard_verdict(self, value: dict) -> None:
        self.mind.guard_verdict = dict(value)

    def dispatch(self, method, path, query, body):
        if path == "/v1/mind/people" or path.startswith("/v1/mind/people/"):
            return self._people(method, path, query, body)
        if path.startswith("/v1/mind/"):
            return self._mind(method, path, body, query)
        if path == "/v1/host/health":
            return 200, {"status": "ok", "capabilities": ["memory"]}
        if path == "/v1/host/contacts/resolve":
            contact = self.contacts.get((query.get("gateway", ""), query.get("address", "")))
            if contact is None and query.get("create") == "true" and query.get("address"):
                contact = {"contact_id": "p-" + query["address"][-2:], "display_name": query["address"],
                           "may_contact": "ask", "trust_tier": "unknown"}
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
            self.told.setdefault(body["context"]["contact_id"], []).append(
                str((body.get("user_message") or {}).get("content") or ""))
            return 200, {"accepted": True, "continuity_updated": True, "source_recorded": True}
        if path == "/v1/host/context/assemble":
            audience = body.get("audience") if isinstance(body, dict) else None
            shared = [{"id": "shared", "title": "Shared", "body": "shared facts", "priority": 50}]
            told = self.told.get(str((body.get("context") or {}).get("contact_id") or ""), [])
            if told:   # recall by contact, whatever session or channel the words arrived on
                shared.append({"id": "protagine-memory", "title": "Recalled", "body": "\n".join(told), "priority": 90})
            if audience == "viewer":
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

    def _mind(self, method, path, body, query=None):
        if not self.mind_routes:
            return 404, {"detail": "not found"}
        with self.lock:
            return self.mind.handle(method, path, body, query)

    def _people(self, method, path, query, body):
        """``/v1/mind/people`` as the sidecar serves it: a named non-owner viewer sees only who someone
        is, and a mutation needs ``contact_id`` == the owner (or ``by: cli``)."""
        if not self.mind_routes:
            return 404, {"detail": "not found"}
        body = body or {}
        rest = [unquote(part) for part in path[len("/v1/mind/people"):].split("/") if part]
        with self.lock:
            people = {c["contact_id"]: c for c in self.contacts.values()}
            viewer = query.get("contact_id")

            def view(contact):
                return dict(contact) if viewer is None or viewer == self.people_owner else \
                    {key: contact[key] for key in PUBLIC_PERSON}

            def find(reference):
                return people.get(reference) or next(
                    (c for c in people.values() if c["display_name"].lower() == reference.lower()), None)

            if method == "GET" and not rest:
                wanted = query.get("q", "").lower()
                return 200, {"contacts": [view(c) for c in people.values()
                                          if not wanted or wanted in c["display_name"].lower() or wanted == c["contact_id"]]}
            if method == "GET" and rest == ["proposals"]:
                return 200, {"proposals": list(self.proposals)}
            if method == "POST" and rest == ["link"]:
                candidate = {"candidate_id": f"identity-candidate:{len(self.proposals) + 1}", "status": "pending",
                             **{key: body.get(key) for key in ("contact_id", "gateway", "address", "by")}}
                self.proposals.append(candidate)
                return 200, {"ok": True, **candidate, "text": "proposed; the owner confirms"}
            if method == "GET" and len(rest) == 1:
                contact = find(rest[0])
                if contact is None:
                    return 404, {"detail": {"code": "unknown_contact", "message": f"no single contact matches {rest[0]!r}"}}
                return 200, {"contact": view(contact)}
            if method != "POST":
                return 404, {"detail": "not found"}
            if body.get("by") != "cli" and body.get("contact_id") != self.people_owner:
                return 403, {"detail": {"code": "not_owner", "message": "only the owner can change this"}}
            if rest == ["merge"]:
                keep, drop = find(body.get("keep") or ""), find(body.get("drop") or "")
                if keep is None or drop is None:
                    return 404, {"detail": {"code": "unknown_contact", "message": "no single contact matches"}}
                self.contacts = {k: v for k, v in self.contacts.items() if v["contact_id"] != drop["contact_id"]}
                return 200, {"ok": True, "dropped": drop["contact_id"], "contact": keep, "text": "merged"}
            contact = find(rest[0]) if len(rest) == 2 else None
            if contact is None:
                return 404, {"detail": {"code": "unknown_contact", "message": "no single contact matches"}}
            if rest[1] == "permission":
                contact["may_contact"] = body.get("may_contact")
                return 200, {"ok": True, "contact_id": contact["contact_id"], "may_contact": contact["may_contact"]}
            if rest[1] == "cadence":
                contact["cadence_minutes"] = body.get("minutes")
                return 200, {"ok": True, "contact_id": contact["contact_id"], "cadence_minutes": body.get("minutes")}
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
    return build_home(tmp_path, sidecar.server.server_address[1], sidecar.url)


def build_home(tmp_path, port, url):
    hermes, instance = tmp_path / "hermes", tmp_path / "protagine"
    hermes.mkdir()
    instance.mkdir()
    key_file = instance / "api.key"
    key_file.write_text(API_KEY + "\n")
    key_file.chmod(0o600)
    (instance / "protagine.yaml").write_text(yaml.safe_dump({
        "sidecar": {"host": "127.0.0.1", "port": port},
        "mind": {"enabled": True, "autonomy": "standard",
                 "deny": {"commands": ["rm -rf *"], "tools": ["terminal"], "text": ["secret-project-x"]}},
    }))
    (instance / "identity.yaml").write_text(yaml.safe_dump({
        "owner": {"name": "Owner", "contact_id": OWNER, "handles": {"telegram": ["1001"]}},
        "agent": {"name": "Agent", "values": ["care"], "boundaries": ["never send money"]},
    }))
    config = {
        "plugins": {"enabled": ["protagine"], "hook_callback_timeout": 0,
                    "protagine": {"sidecar_url": url, "key_file": str(key_file)}},
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

# The body tests drive ticks by hand: the registered body thread stays parked so
# its own ticks cannot race the assertions.
MIND_PRELUDE = PRELUDE.replace("manager.discover_and_load(force=True)", '''
import protagine_hermes.body as _body_module
_body_module.Body.start = lambda self: None
manager.discover_and_load(force=True)''') + r'''
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
body = protagine_hermes._BODY
def tick():
    body.on_dispatch_tick(board="default", outcome="idle")  # the gateway dispatcher's heartbeat
    return body.run_once()
def tasks():
    conn = connect()
    try:
        return {t.idempotency_key or t.id: {"id": t.id, "status": t.status, "assignee": t.assignee, "title": t.title,
                "body": t.body, "goal_mode": t.goal_mode, "max_runtime_seconds": t.max_runtime_seconds,
                "max_retries": t.max_retries, "created_by": t.created_by, "workspace_kind": t.workspace_kind}
                for t in kb.list_tasks(conn, include_archived=True)}
    finally:
        conn.close()
SENDS = []
def fake_send(args, **kw):
    SENDS.append(dict(args))
    return json.dumps({"success": True, "platform": "telegram", "message_id": "m-%d" % len(SENDS)})
import tools.send_message_tool as _smt
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
