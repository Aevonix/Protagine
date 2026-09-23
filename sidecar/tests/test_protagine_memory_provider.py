"""Unit harness for the Protagine memory provider.

The provider previously had no test coverage at all; this loads it straight
from the plugin directory (it is a standalone module, no Hermes install
needed) and exercises the prefetch-cache and per-turn-contact logic with a
stubbed httpx transport.

Regression locks:
  * prefetch performs exactly one bounded assemble for the current turn's
    query; queue_prefetch starts no background work, and an open circuit
    breaker skips the sidecar entirely (assemble and temporal);
  * a real-channel resolution miss yields no context and never falls back to
    the provider-wide owner/default contact.
  * guest context requires a server-attested exact viewer plus a supported scoped
    projection before any context producer is queried;
  * temporal and reply-thread fallbacks never query owner-global data for a
    guest, and lifecycle write hooks exact-bind or stay dark.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys
import threading
import time
import types

import pytest

_PROVIDER_PATH = (pathlib.Path(__file__).resolve().parents[2]
                  / "plugins" / "protagine-memory" / "provider.py")


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "protagine_memory_provider_under_test", _PROVIDER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def provider_mod():
    return _load_provider_module()


@pytest.fixture(autouse=True)
def _privacy_posture(monkeypatch):
    monkeypatch.setenv("PROTAGINE_PREFETCH_TURN_CONTACT", "1")
    monkeypatch.setenv(
        "PROTAGINE_MEMORY_DEFAULT_CONTEXT_AUTHORITY", "owner_system",
    )


# --- stubbed httpx -----------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttpx:
    """Drop-in for the provider module's `httpx` attribute. Records every
    request and answers from a route table {(method, path_suffix): payload}."""

    class HTTPError(Exception):
        pass

    class HTTPStatusError(Exception):
        pass

    class ConnectError(Exception):
        pass

    def __init__(self, routes=None, *, delay=0.0, error=None):
        """`delay` sleeps that long on every request (a slow sidecar); `error`
        is an exception class raised after the delay (a hung sidecar whose
        requests time out)."""
        self.routes = routes or {}
        self.requests = []
        self.delay = delay
        self.error = error
        fake = self

        class _Client:
            def __init__(self, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def _handle(self, method, url, **kwargs):
                request = {
                    "method": method, "url": url,
                    "params": kwargs.get("params"),
                    "json": kwargs.get("json"),
                }
                fake.requests.append(request)
                if fake.delay:
                    time.sleep(fake.delay)
                if fake.error is not None:
                    raise fake.error("sidecar request timed out")
                for (m, suffix), payload in fake.routes.items():
                    if m == method and url.endswith(suffix):
                        if callable(payload):
                            payload = payload(request)
                        return _FakeResponse(payload=payload)
                return _FakeResponse(payload={})

            def get(self, url, **kwargs):
                return self._handle("GET", url, **kwargs)

            def post(self, url, **kwargs):
                return self._handle("POST", url, **kwargs)

            def put(self, url, **kwargs):
                return self._handle("PUT", url, **kwargs)

        self.Client = _Client


def _make_provider(provider_mod, fake_httpx, monkeypatch):
    monkeypatch.setattr(provider_mod, "httpx", fake_httpx)
    p = provider_mod.ProtagineMemoryProvider(config={
        "url": "http://sidecar.test", "api_key": "k", "contact_id": "cid-base"})
    return p


def test_offline_start_does_not_detach_provider_and_same_instance_recovers(
        provider_mod, monkeypatch):
    attempts = 0
    def assemble(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _FakeHttpx.HTTPError("sidecar offline")
        return {"sections": [{"id": "memory", "body": "remembered after recovery"}]}
    fake = _FakeHttpx(routes={("POST", "/v1/host/context/assemble"): assemble})
    provider = _make_provider(provider_mod, fake, monkeypatch)
    assert provider.is_available() is True
    assert fake.requests == []
    assert provider.get_diagnostics()["connection_status"] == "unverified"
    assert provider._prefetch_sync("recall", contact_id="cid-base") == ""
    assert provider.get_diagnostics()["connection_status"] == "degraded"
    assert "remembered after recovery" in provider._prefetch_sync("recall", contact_id="cid-base")
    assert provider.get_diagnostics()["connection_status"] == "connected"


def test_invalid_saved_profile_does_not_fall_back_to_another_instance(
        provider_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(provider_mod, "_active_hermes_home", lambda: tmp_path)
    (tmp_path / "protagine-memory.json").write_text('{"url": "private-broken-value"')
    with pytest.raises(ValueError, match="invalid Protagine memory configuration") as error:
        provider_mod.ProtagineMemoryProvider()
    assert "private-broken-value" not in str(error.value)


_ASSEMBLE = ("POST", "/v1/host/context/assemble")
_TEMPORAL = ("GET", "/v1/host/context/temporal")
_READINESS = ("GET", "/v1/host/context/projection-readiness")
_RESOLVE = ("GET", "/v1/host/contacts/resolve")
_TURN_V2 = ("PUT", "/v2/host/turns/")
_QUEUE_CLAIM = ("POST", "/v1/host/queue/jobs/claim")
_QUEUE_START = ("POST", "/v1/host/queue/jobs/job-tool/start")


def _assemble_calls(fake):
    return [r for r in fake.requests if r["url"].endswith(_ASSEMBLE[1])]


def _projection(contact_id, *, mode="shadow", owner=False):
    return {
        "schema": "ContextProjectionAttestationV1",
        "version": 1,
        "viewer_person_id": contact_id,
        "viewer_attested": True,
        "viewer_is_owner": owner,
        "p8_mode": mode,
        "scoped_projection_ready": mode in {"shadow", "live"},
        "legacy_global_allowed": bool(owner),
    }


def _install_session_context(monkeypatch):
    local = threading.local()
    module = types.ModuleType("gateway.session_context")

    def get_session_env(name, default=""):
        return getattr(local, "values", {}).get(name, default)

    def set_turn(*, platform="", sender="", chat=""):
        local.values = {
            "HERMES_SESSION_PLATFORM": platform,
            "HERMES_SESSION_USER_ID": sender,
            "HERMES_SESSION_CHAT_ID": chat,
        }

    module.get_session_env = get_session_env
    package = types.ModuleType("gateway")
    package.session_context = module
    monkeypatch.setitem(sys.modules, "gateway", package)
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)
    return set_turn


def test_approval_tool_is_never_model_visible(provider_mod, monkeypatch):
    monkeypatch.delenv("PROTAGINE_GENERAL_PLUGIN_ACTIVE", raising=False)
    monkeypatch.setenv("PROTAGINE_MEMORY_WORKER_TOOLS", "1")
    provider = _make_provider(provider_mod, _FakeHttpx(), monkeypatch)
    assert "protagine_approve_initiative" not in {
        schema["name"] for schema in provider.get_tool_schemas()
    }


def test_memory_provider_is_read_only_when_general_plugin_is_active(
        provider_mod, monkeypatch, tmp_path):
    # The general plugin's outbox owns capture whenever the Hermes config enables it.
    monkeypatch.setattr(provider_mod, "_active_hermes_home", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(json.dumps({
        "plugins": {"enabled": ["protagine"]}, "memory": {"provider": "protagine-memory"}}))
    fake = _FakeHttpx()
    p = _make_provider(provider_mod, fake, monkeypatch)
    p.sync_turn("hello", "hi", session_id="s1", turn_id="turn-1")
    assert fake.requests == []
    assert p.get_diagnostics()["turn_writer"] == "read-only"


@pytest.mark.parametrize("general_active", [False, True])
@pytest.mark.parametrize("action", ["add", "replace"])
def test_native_file_edits_never_become_new_owner_evidence(
        provider_mod, monkeypatch, general_active, action):
    monkeypatch.setenv("PROTAGINE_GENERAL_PLUGIN_ACTIVE", "1" if general_active else "0")
    monkeypatch.setenv("PROTAGINE_MEMORY_TURN_WRITER", "enabled")
    fake = _FakeHttpx()
    provider = _make_provider(provider_mod, fake, monkeypatch)
    provider._session_id = "exact-session"
    monkeypatch.setattr(provider, "_prefetch_contact", lambda: "cid-base")

    result = provider.on_memory_write(
        action, "MEMORY.md", "An assistant's edited interpretation.",
        metadata={"old_text": "An earlier interpretation.", "kind": "fact"},
    )

    assert result is None  # This hook makes no persistence receipt.
    for name, args in (("protagine_write_memory", {"content": "edited interpretation"}),
                       ("protagine_search_memory", {"query": "interpretation"})):
        assert not hasattr(provider, "_tool_" + name)
        assert "error" in json.loads(provider.handle_tool_call(name, args))
    assert fake.requests == []


def test_standalone_memory_provider_syncs_the_turn_with_its_stable_id(
        provider_mod, monkeypatch, tmp_path):
    # Without the general plugin the provider is the turn writer: one sync per turn.
    monkeypatch.setattr(provider_mod, "_active_hermes_home", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(json.dumps({"memory": {"provider": "protagine-memory"}}))
    set_turn = _install_session_context(monkeypatch)
    set_turn(platform="cli", sender="", chat="")
    fake = _FakeHttpx(routes={("POST", "/v1/host/turns/sync"): {"accepted": True}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    assert p.get_diagnostics()["turn_writer"] == "enabled"

    p.sync_turn("hello", "hi", session_id="s1", turn_id="turn/1")
    p._sync_thread.join(timeout=5)

    posts = [r for r in fake.requests if r["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["url"].endswith("/v1/host/turns/sync")
    assert posts[0]["json"]["context"]["turn_id"] == "turn/1"
    assert posts[0]["json"]["context"]["contact_id"] == "cid-base"
    assert posts[0]["json"]["user_message"] == {"role": "user", "content": "hello"}
    assert posts[0]["json"]["assistant_message"] == {"role": "assistant", "content": "hi"}


# --- U14: one bounded assemble per turn --------------------------------------
# Hermes hands queue_prefetch() the message of the turn that just finished and
# prefetch() the message of the next turn. Recall is keyed on the current
# message, so nothing assembled in the background could ever be consumed.

_LOCAL_CLOCK = "Runtime reference clock"


def test_queue_prefetch_starts_no_work_and_prefetch_assembles_once(
        provider_mod, monkeypatch):
    fake = _FakeHttpx(routes={
        _ASSEMBLE: {"sections": [{"title": "M", "body": "fresh", "priority": 90}]},
        _TEMPORAL: {"title": "Current Time", "body": "now"},
    })
    p = _make_provider(provider_mod, fake, monkeypatch)
    p.queue_prefetch("previous turn", session_id="s1")
    assert _assemble_calls(fake) == []
    out = p.prefetch("next turn", session_id="s1")
    assert "fresh" in out
    calls = _assemble_calls(fake)
    assert len(calls) == 1
    assert calls[0]["json"]["incoming_message"]["content"] == "next turn"


def test_prefetch_never_waits_on_queued_background_work(provider_mod, monkeypatch):
    delay = 0.4

    def slow_assemble(request):
        time.sleep(delay)
        return {"sections": [{"title": "M", "body": "fresh", "priority": 90}]}

    fake = _FakeHttpx(routes={
        _ASSEMBLE: slow_assemble,
        _TEMPORAL: {"title": "Current Time", "body": "now"},
    })
    p = _make_provider(provider_mod, fake, monkeypatch)
    p.queue_prefetch("previous turn", session_id="s1")
    started = time.monotonic()
    out = p.prefetch("next turn", session_id="s1")
    elapsed = time.monotonic() - started
    assert "fresh" in out
    # Exactly one assemble; joining stale background work (or a second
    # assemble) would cost at least another `delay`.
    assert len(_assemble_calls(fake)) == 1
    assert elapsed < delay * 1.5


def test_prefetch_with_open_circuit_never_touches_the_sidecar(
        provider_mod, monkeypatch):
    fake = _FakeHttpx(routes={
        _ASSEMBLE: {"sections": [{"title": "M", "body": "fresh", "priority": 90}]},
        _TEMPORAL: {"title": "Current Time", "body": "now"},
    }, delay=0.5)
    p = _make_provider(provider_mod, fake, monkeypatch)
    for _ in range(3):
        p._record_connection_failure()
    assert p.get_diagnostics()["circuit_open"] is True
    started = time.monotonic()
    out = p.prefetch("next turn", session_id="s1")
    elapsed = time.monotonic() - started
    assert fake.requests == []
    assert elapsed < 0.1
    assert _LOCAL_CLOCK in out          # local clock only, no assembled recall
    assert "fresh" not in out


def test_hung_sidecar_prefetch_is_bounded_and_opens_the_breaker(
        provider_mod, monkeypatch):
    delay = 0.15
    fake = _FakeHttpx(delay=delay, error=_FakeHttpx.HTTPError)
    p = _make_provider(provider_mod, fake, monkeypatch)
    for turn in range(3):
        p.queue_prefetch(f"turn {turn}", session_id="s1")
        started = time.monotonic()
        out = p.prefetch(f"turn {turn + 1}", session_id="s1")
        elapsed = time.monotonic() - started
        assert _LOCAL_CLOCK in out
        # Bound: one assemble timeout plus at most one temporal timeout.
        assert elapsed < delay * 3
    assert len(_assemble_calls(fake)) == 3
    assert p.get_diagnostics()["circuit_open"] is True
    started = time.monotonic()
    out = p.prefetch("turn 4", session_id="s1")
    assert time.monotonic() - started < 0.05
    assert _LOCAL_CLOCK in out
    assert len(_assemble_calls(fake)) == 3
    assert p.get_diagnostics()["connection_failures"] == 3


def test_temporal_fetch_honours_open_circuit(provider_mod, monkeypatch):
    fake = _FakeHttpx(routes={
        _TEMPORAL: {"title": "Current Time", "body": "sidecar clock"},
    }, delay=0.5)
    p = _make_provider(provider_mod, fake, monkeypatch)
    for _ in range(3):
        p._record_connection_failure()
    started = time.monotonic()
    block = p._fresh_temporal_block_sync(contact_id="cid-base")
    assert time.monotonic() - started < 0.1
    assert fake.requests == []
    assert _LOCAL_CLOCK in block


# --- U15: per-turn contact in prefetch ---------------------------------------

def test_prefetch_internal_owner_lane_requires_explicit_attestation(
        provider_mod, monkeypatch):
    monkeypatch.delenv("PROTAGINE_PREFETCH_TURN_CONTACT", raising=False)
    fake = _FakeHttpx(routes={_ASSEMBLE: {"sections": []}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    p._prefetch_sync("hello", session_id="s1")
    calls = _assemble_calls(fake)
    assert len(calls) == 1
    assert calls[0]["json"]["context"]["contact_id"] == "cid-base"


def test_prefetch_binds_a_guest_turn_to_its_resolved_contact(provider_mod, monkeypatch):
    # The turn's sender, resolved server-side, is the prefetch person; a guest
    # request carries the viewer audience and the scoped projection policy.
    set_turn = _install_session_context(monkeypatch)
    set_turn(platform="rcs", sender="alice", chat="thread-a")
    fake = _FakeHttpx(routes={_RESOLVE: {"contact_id": "cid-turn"}, _ASSEMBLE: {"sections": []}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    p.prefetch("hello", session_id="s1")
    call = _assemble_calls(fake)[0]["json"]
    assert call["context"]["contact_id"] == "cid-turn"
    assert call["audience"] == "viewer"
    assert call["projection_policy"] == "scoped_viewer_required"
    assert call["include_initiatives"] is False


def test_prefetch_internal_owner_lane_may_fallback_when_attested(
        provider_mod, monkeypatch):
    monkeypatch.setenv("PROTAGINE_PREFETCH_TURN_CONTACT", "1")
    fake = _FakeHttpx(routes={_ASSEMBLE: {"sections": []}})
    p = _make_provider(provider_mod, fake, monkeypatch)

    def _boom():
        raise RuntimeError("resolver down")

    monkeypatch.setattr(p, "_turn_contact", _boom)
    p._prefetch_sync("hello", session_id="s1")
    assert _assemble_calls(fake)[0]["json"]["context"]["contact_id"] == "cid-base"


def test_temporal_block_guest_uses_local_clock_only(provider_mod, monkeypatch):
    set_turn = _install_session_context(monkeypatch)
    fake = _FakeHttpx(routes={_RESOLVE: {"contact_id": "cid-turn"},
                              _TEMPORAL: {"title": "Current Time", "body": "now"}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    set_turn(platform="rcs", sender="alice", chat="thread-a")
    block = p._fresh_temporal_block_sync()
    assert "Runtime reference clock" in block
    temporal = [r for r in fake.requests if r["url"].endswith(_TEMPORAL[1])]
    assert temporal == []
    # A real channel without a sender binding is never the owner either.
    set_turn(platform="rcs", sender="", chat="thread-b")
    p._fresh_temporal_block_sync()
    temporal = [r for r in fake.requests if r["url"].endswith(_TEMPORAL[1])]
    assert temporal == []


def test_temporal_block_attested_internal_lane_uses_provider_contact(
        provider_mod, monkeypatch):
    monkeypatch.delenv("PROTAGINE_PREFETCH_TURN_CONTACT", raising=False)
    fake = _FakeHttpx(routes={_TEMPORAL: {"title": "Current Time", "body": "now"}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    p._fresh_temporal_block_sync()
    temporal = [r for r in fake.requests if r["url"].endswith(_TEMPORAL[1])]
    assert temporal[0]["params"]["contact_id"] == "cid-base"


@pytest.mark.parametrize("clock_body", ["Contact clock.", ""])
def test_reused_provider_refreshes_turn_gap_without_refetching_contact_clock(
        provider_mod, monkeypatch, clock_body):
    fake = _FakeHttpx(routes={_TEMPORAL: {"title": "Current Time", "body": clock_body}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    now = [100.0]
    monkeypatch.setattr(provider_mod, "_ttime", types.SimpleNamespace(
        time=lambda: now[0], monotonic=lambda: now[0]))
    p.initialize("conversation-one")
    p.on_turn_start(1, "First request.")
    now[0] += 3600
    p.on_turn_start(2, "Follow up on the request.")
    first = p._with_fresh_temporal_sync(
        "## Relevant Memories [priority 80]\nRemember the archive location.", contact_id="cid-base")
    assert "Gap before current turn: 1h 00m." in first
    now[0] += 7
    p.on_turn_start(3, "A quick clarification.")
    second = p._with_fresh_temporal_sync(first, contact_id="cid-base")
    assert "Gap before current turn: 7s." in second
    assert "1h 00m" not in second
    assert second.count("Gap before current turn:") == 1
    assert (clock_body or "Runtime reference clock") in second and "Remember the archive location." in second
    assert len([r for r in fake.requests if r["url"].endswith(_TEMPORAL[1])]) == 1


@pytest.mark.parametrize("new_session,kwargs,preserves_gap", [
    ("compressed", {"parent_session_id": "conversation-one", "reason": "compression"}, True),
    ("conversation-one", {"parent_session_id": "conversation-one", "reason": "compression"}, True),
    ("resumed", {"parent_session_id": "conversation-one", "reason": "resume"}, False),
    ("branch", {"parent_session_id": "conversation-one", "reason": "branch"}, False),
    ("new", {"reset": True, "reason": "new_session"}, False),
    ("conversation-one", {"rewound": True}, False),
    ("unknown", {}, False),
    ("mismatched", {"parent_session_id": "another", "reason": "compression"}, False),
])
def test_conversation_gap_follows_native_session_boundary(
        provider_mod, monkeypatch, new_session, kwargs, preserves_gap):
    fake = _FakeHttpx(routes={_TEMPORAL: {"title": "Current Time", "body": "Contact clock."}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    now = [100.0]
    monkeypatch.setattr(provider_mod, "_ttime", types.SimpleNamespace(
        time=lambda: now[0], monotonic=lambda: now[0]))
    p.initialize("conversation-one")
    p.on_turn_start(1, "First request.")
    now[0] += 3600
    p.on_turn_start(2, "Continue the request.")
    assert "1h 00m" in p._fresh_temporal_block_sync(contact_id="cid-base")
    p.on_session_switch(new_session, **kwargs)
    now[0] += 7
    p.on_turn_start(3, "Review the selected conversation.")
    block = p._fresh_temporal_block_sync(contact_id="cid-base")
    assert ("Gap before current turn: 7s." in block) is preserves_gap
    if not preserves_gap:
        assert "Gap before current turn:" not in block
    assert "1h 00m" not in block
    assert len([r for r in fake.requests if r["url"].endswith(_TEMPORAL[1])]) == 1


def test_resolve_handle_ttl_cache(provider_mod, monkeypatch):
    """_resolve_handle results are TTL-cached so per-turn resolution does not
    hammer /contacts/resolve on every prefetch."""
    fake = _FakeHttpx(routes={_RESOLVE: {"contact_id": "cid-r"}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    assert p._resolve_handle("sms", "+15550001") == "cid-r"
    assert p._resolve_handle("sms", "+15550001") == "cid-r"
    resolves = [r for r in fake.requests if r["url"].endswith(_RESOLVE[1])]
    assert len(resolves) == 1                       # second call served by TTL cache
    # Expired entry refetches.
    key = "sms:+15550001"
    ts, cid = p._handle_cache[key]
    p._handle_cache[key] = (ts - 3600.0, cid)
    assert p._resolve_handle("sms", "+15550001") == "cid-r"
    resolves = [r for r in fake.requests if r["url"].endswith(_RESOLVE[1])]
    assert len(resolves) == 2


def test_unresolved_real_channel_has_no_context_temporal_sync_or_writes(
        provider_mod, monkeypatch):
    set_turn = _install_session_context(monkeypatch)
    set_turn(platform="whatsapp", sender="unknown", chat="thread-1")
    fake = _FakeHttpx(routes={_RESOLVE: {}})
    provider = _make_provider(provider_mod, fake, monkeypatch)
    monkeypatch.delenv("PROTAGINE_GENERAL_PLUGIN_ACTIVE", raising=False)

    assert provider.prefetch("hello", session_id="s1") == ""
    provider.sync_turn("hello", "hi", session_id="s1")
    provider.on_memory_write("add", "memory", "secret")
    provider.on_pre_compress([
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
    ])
    urls = [request["url"] for request in fake.requests]
    assert not any(url.endswith(_ASSEMBLE[1]) for url in urls)
    assert not any(url.endswith(_TEMPORAL[1]) for url in urls)
    assert not any("/turns/" in url for url in urls)
    assert not any("/memory/write" in url for url in urls)
    assert not any("/signals/ingest" in url for url in urls)
    assert sum(url.endswith(_RESOLVE[1]) for url in urls) == 1

    # A new turn clears only the short negative result and retries safely.
    provider.on_turn_start(2, "retry")
    assert provider.prefetch("retry", session_id="s1") == ""
    assert sum(
        request["url"].endswith(_RESOLVE[1])
        for request in fake.requests
    ) == 2


def test_two_concurrent_senders_never_share_context(
        provider_mod, monkeypatch):
    set_turn = _install_session_context(monkeypatch)

    def resolve(request):
        return {"contact_id": f"cid-{request['params']['address']}"}

    def readiness(request):
        return _projection(request["params"]["contact_id"])

    def assemble(request):
        contact = request["json"]["context"]["contact_id"]
        return {
            "sections": [{"title": "Bound", "body": f"ctx-{contact}"}],
            "projection_attestation": _projection(contact),
        }

    fake = _FakeHttpx(routes={
        _RESOLVE: resolve,
        _READINESS: readiness,
        _ASSEMBLE: assemble,
    })
    provider = _make_provider(provider_mod, fake, monkeypatch)
    results = {}

    def run(sender):
        set_turn(platform="whatsapp", sender=sender, chat=f"chat-{sender}")
        results[sender] = provider.prefetch("same query", session_id="same")

    threads = [threading.Thread(target=run, args=(sender,)) for sender in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert "ctx-cid-a" in results["a"]
    assert "ctx-cid-b" not in results["a"]
    assert "ctx-cid-b" in results["b"]
    assert "ctx-cid-a" not in results["b"]
    assert {
        call["json"]["context"]["contact_id"]
        for call in _assemble_calls(fake)
    } == {"cid-a", "cid-b"}


def test_queued_prefetch_for_sender_a_never_reaches_sender_b(
        provider_mod, monkeypatch):
    set_turn = _install_session_context(monkeypatch)

    def resolve(request):
        return {"contact_id": f"cid-{request['params']['address']}"}

    def readiness(request):
        return _projection(request["params"]["contact_id"])

    def assemble(request):
        contact = request["json"]["context"]["contact_id"]
        return {
            "sections": [{"title": "Bound", "body": f"ctx-{contact}"}],
            "projection_attestation": _projection(contact),
        }

    fake = _FakeHttpx(routes={
        _RESOLVE: resolve,
        _READINESS: readiness,
        _ASSEMBLE: assemble,
    })
    provider = _make_provider(provider_mod, fake, monkeypatch)
    set_turn(platform="sms", sender="a", chat="chat-a")
    provider.queue_prefetch("same", session_id="same")
    assert _assemble_calls(fake) == []
    set_turn(platform="sms", sender="b", chat="chat-b")
    result = provider.prefetch("same", session_id="same")

    assert "ctx-cid-b" in result
    assert "ctx-cid-a" not in result
    assert [
        call["json"]["context"]["contact_id"]
        for call in _assemble_calls(fake)
    ] == ["cid-b"]


def test_resolve_contact_does_not_mutate_provider_owner_contact(
        provider_mod, monkeypatch):
    fake = _FakeHttpx(routes={_RESOLVE: {"contact_id": "cid-guest"}})
    provider = _make_provider(provider_mod, fake, monkeypatch)
    provider.resolve_contact("sms", "+15550004")
    assert provider._contact_id == "cid-base"


def test_guest_read_tools_never_call_legacy_unprojected_endpoints(
        provider_mod, monkeypatch):
    monkeypatch.setenv("PROTAGINE_GENERAL_PLUGIN_ACTIVE", "1")
    set_turn = _install_session_context(monkeypatch)
    set_turn(platform="rcs", sender="alice", chat="thread-a")
    fake = _FakeHttpx(routes={
        _RESOLVE: {"contact_id": "cid-alice"},
        _READINESS: _projection("cid-alice"),
        ("GET", "/v1/host/commitments"): {"commitments": []},
    })
    provider = _make_provider(provider_mod, fake, monkeypatch)

    denied = json.loads(provider.handle_tool_call(
        "protagine_check_commitments", {"contact_id": "cid-bob"},
    ))
    assert denied["error"] == "contact override exceeds turn authority"
    assert not any(
        request["url"].endswith("/v1/host/commitments")
        for request in fake.requests
    )
    withheld = json.loads(provider.handle_tool_call(
        "protagine_check_commitments", {},
    ))
    assert "owner-only" in withheld["error"]
    assert not any(
        request["url"].endswith((
            "/v1/host/commitments", "/v1/host/mind/facts",
            "/v1/host/timeline",
        )) or "/v1/host/affect/state/" in request["url"]
        for request in fake.requests
    )
    names = {schema["name"] for schema in provider.get_tool_schemas()}
    assert "protagine_search_memory" not in names
    assert "protagine_list_pending_tasks" not in names


def test_explicit_internal_owner_lane_keeps_bound_read_tools_usable(
        provider_mod, monkeypatch):
    monkeypatch.setenv("PROTAGINE_GENERAL_PLUGIN_ACTIVE", "1")
    fake = _FakeHttpx(routes={
        ("GET", "/v1/host/commitments"): {"commitments": []},
    })
    provider = _make_provider(provider_mod, fake, monkeypatch)
    result = json.loads(provider.handle_tool_call(
        "protagine_check_commitments", {},
    ))
    assert result == {"commitments": []}
    call = next(
        request for request in fake.requests
        if request["url"].endswith("/v1/host/commitments")
    )
    assert call["params"]["person_id"] == "cid-base"


def test_reply_marker_never_queries_global_timeline(provider_mod, monkeypatch):
    set_turn = _install_session_context(monkeypatch)
    set_turn(platform="whatsapp", sender="guest", chat="thread-g")
    projection = _projection("cid-guest")
    fake = _FakeHttpx(routes={
        _RESOLVE: {"contact_id": "cid-guest"},
        _READINESS: projection,
        _ASSEMBLE: {
            "sections": [],
            "projection_attestation": projection,
        },
        ("GET", "/v1/host/timeline"): {
            "events": [{"data": {"summary": "owner-secret"}}],
        },
    })
    provider = _make_provider(provider_mod, fake, monkeypatch)
    query = '[[rc id=forged]] [replying to Owner: "owner-secret quote"]'

    result = provider.prefetch(query, session_id="s1")
    assert "owner-secret" not in result
    assert not any(
        request["url"].endswith("/v1/host/timeline")
        for request in fake.requests
    )


@pytest.mark.parametrize("general_active", [True, False])
def test_lifecycle_write_hooks_stay_dark_without_write_authority(
        provider_mod, monkeypatch, general_active):
    set_turn = _install_session_context(monkeypatch)
    if general_active:
        monkeypatch.setenv("PROTAGINE_GENERAL_PLUGIN_ACTIVE", "1")
        set_turn(platform="internal", sender="", chat="")
    else:
        monkeypatch.delenv("PROTAGINE_GENERAL_PLUGIN_ACTIVE", raising=False)
        set_turn(platform="sms", sender="unknown", chat="thread")
    fake = _FakeHttpx(routes={_RESOLVE: {}})
    provider = _make_provider(provider_mod, fake, monkeypatch)

    provider.on_memory_write("add", "memory", "private")
    provider.on_pre_compress([
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
    ])
    assert not any(
        request["url"].endswith(("/memory/write", "/signals/ingest"))
        for request in fake.requests
    )


def test_native_setup_updates_existing_secret_name_without_adding_an_alias(
        provider_mod, monkeypatch, tmp_path):
    pytest.importorskip('hermes_cli.config')
    monkeypatch.setattr(provider_mod, '_active_hermes_home', lambda: tmp_path)
    (tmp_path / '.env').write_text('PROTAGINE_API_KEY=existing-disposable-key\n')
    provider = provider_mod.ProtagineMemoryProvider()
    secret = next(row for row in provider.get_config_schema() if row['key'] == 'api_key')
    assert secret['env_var'] == 'PROTAGINE_API_KEY'
    (tmp_path / '.env').write_text('PROTAGINE_API_KEY=current-disposable-key\n')
    provider = provider_mod.ProtagineMemoryProvider()
    secret = next(row for row in provider.get_config_schema() if row['key'] == 'api_key')
    assert secret['env_var'] == 'PROTAGINE_API_KEY'


def test_prefetch_with_open_circuit_skips_real_channel_contact_resolution(
        provider_mod, monkeypatch):
    set_turn = _install_session_context(monkeypatch)
    set_turn(platform="telegram", sender="tg-1", chat="chat-1")
    fake = _FakeHttpx(routes={
        _RESOLVE: {"contact_id": "cid-tg"},
        _ASSEMBLE: {"sections": [{"title": "M", "body": "fresh", "priority": 90}]},
    }, delay=0.5)
    p = _make_provider(provider_mod, fake, monkeypatch)
    for _ in range(3):
        p._record_connection_failure()
    started = time.monotonic()
    out = p.prefetch("next turn", session_id="s1")
    assert fake.requests == []          # no /contacts/resolve, no assemble
    assert time.monotonic() - started < 0.1
    assert "fresh" not in out


def test_contact_resolution_transport_failures_open_the_breaker(
        provider_mod, monkeypatch):
    fake = _FakeHttpx(error=_FakeHttpx.HTTPError)
    p = _make_provider(provider_mod, fake, monkeypatch)
    for turn in range(3):
        p._turn_number = turn           # a later turn retries a cached miss
        assert p._resolve_handle("telegram", "tg-1") is None
    assert p.get_diagnostics()["connection_failures"] == 3
    assert p.get_diagnostics()["circuit_open"] is True
    p._turn_number = 3
    assert p._resolve_handle("telegram", "tg-1") is None
    assert len(fake.requests) == 3      # the open breaker skipped the call
