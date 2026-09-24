"""``protagine doctor``: the sidecar checks read the served health and fail loudly."""
from __future__ import annotations

import plistlib

import pytest
import yaml

from protagine import doctor
from protagine.doctor import FAIL, PASS, SKIP, WARN, check_semantic_recall, check_sidecar


def _serve(monkeypatch, routes):
    """Answer ``_http_get`` from a mapping of path suffix to (status, body)."""
    calls = []

    def fake_get(url, api_key="", timeout=10.0):
        calls.append(url)
        for suffix, answer in routes.items():
            if url.endswith(suffix):
                return answer
        return 404, {"detail": "not found"}

    monkeypatch.setattr(doctor, "_http_get", fake_get)
    return calls


def _instance(tmp_path, monkeypatch, router):
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    (tmp_path / "protagine.yaml").write_text(yaml.safe_dump({"router": router}))


def test_sidecar_check_repeats_the_problems_in_words(monkeypatch):
    _serve(monkeypatch, {
        "/v1/host/health": (200, {"status": "degraded",
                                  "problems": ["semantic recall is off: the embedder did not initialise: boom"]}),
        "/v1/mind/state": (200, {}),
    })
    results = check_sidecar("http://127.0.0.1:7777", "key", 5)
    assert results[0].status == WARN
    assert "status=degraded: semantic recall is off: the embedder did not initialise: boom" in results[0].detail
    assert [r.name for r in results] == ["sidecar", "open-files", "sidecar-auth"] and results[2].status == PASS


def test_semantic_recall_is_skipped_without_an_embedding_endpoint(tmp_path, monkeypatch):
    _instance(tmp_path, monkeypatch, {"embed_url": ""})
    calls = _serve(monkeypatch, {})
    result = check_semantic_recall("http://127.0.0.1:7777", "key", 5)
    assert result.status == SKIP and "router.embed_url" in result.detail
    assert calls == []


def test_semantic_recall_warns_when_an_endpoint_is_set_but_the_switch_is_off(tmp_path, monkeypatch):
    """An install from before the switch was live may still carry ``semantic_recall: false``: recall is
    lexical although an endpoint is recorded, and doctor says why."""
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    (tmp_path / "protagine.yaml").write_text(yaml.safe_dump({
        "router": {"embed_url": "http://127.0.0.1:8092", "embed_model": "m"},
        "mind": {"faculties": {"semantic_recall": False}}}))
    calls = _serve(monkeypatch, {})
    result = check_semantic_recall("http://127.0.0.1:7777", "key", 5)
    assert result.status == WARN and "mind.faculties.semantic_recall is false" in result.detail
    assert "semantic_recall: true" in result.remedy and calls == []

def test_semantic_recall_fails_when_the_configured_embedder_is_not_serving(tmp_path, monkeypatch):
    _instance(tmp_path, monkeypatch, {"embed_url": "http://127.0.0.1:8092", "embed_model": "m"})
    _serve(monkeypatch, {"/v1/host/embed/health": (200, {
        "status": "error", "error": "the embedder (provider=openai_api, model=m) did not initialise: "
                                    "ValueError: Embedding response dimension 4096 differs from the configured 384"})})
    result = check_semantic_recall("http://127.0.0.1:7777", "key", 5)
    assert result.status == FAIL
    assert "dimension 4096 differs from the configured 384" in result.detail
    assert "router.embed_dims" in result.remedy and "protagine service restart" in result.remedy


def test_semantic_recall_passes_when_the_embedder_answers(tmp_path, monkeypatch):
    _instance(tmp_path, monkeypatch, {"embed_url": "http://127.0.0.1:8092", "embed_model": "m", "embed_dims": 4096})
    _serve(monkeypatch, {"/v1/host/embed/health": (200, {"status": "ok", "model": "m", "dims": 4096,
                                                        "latency_ms": 194.0})})
    result = check_semantic_recall("http://127.0.0.1:7777", "key", 5)
    assert result.status == PASS and "dims=4096" in result.detail


def test_run_doctor_asks_about_recall_only_when_the_sidecar_answers(tmp_path, monkeypatch):
    _instance(tmp_path, monkeypatch, {"embed_url": "http://127.0.0.1:8092", "embed_model": "m"})
    monkeypatch.setattr(doctor, "run_local_checks", lambda: [])
    _serve(monkeypatch, {"/v1/host/health": (200, {"status": "ok", "problems": []}),
                         "/v1/mind/state": (200, {}),
                         "/v1/host/embed/health": (200, {"status": "error", "error": "embedder not initialized"})})
    names = {r.name: r.status for r in doctor.run_doctor("http://127.0.0.1:7777", "key", 5)}
    assert names == {"sidecar": PASS, "open-files": SKIP, "sidecar-auth": PASS, "semantic-recall": FAIL}

    def down(url, api_key="", timeout=10.0):
        raise OSError("connection refused")
    monkeypatch.setattr(doctor, "_http_get", down)
    names = {r.name: r.status for r in doctor.run_doctor("http://127.0.0.1:7777", "key", 5)}
    assert names == {"sidecar": FAIL}


def test_sidecar_check_warns_when_the_running_sidecar_has_few_open_files(monkeypatch):
    _serve(monkeypatch, {"/v1/host/health": (200, {"status": "ok", "notes": {"fd_limit": "256"}}),
                         "/v1/mind/state": (200, {})})
    results = {r.name: r for r in check_sidecar("http://127.0.0.1:7777", "key", 5)}
    assert results["open-files"].status == WARN
    assert "256 open files" in results["open-files"].detail and "service restart" in results["open-files"].remedy
    _serve(monkeypatch, {"/v1/host/health": (200, {"status": "ok", "notes": {"fd_limit": "16384"}}),
                         "/v1/mind/state": (200, {})})
    results = {r.name: r for r in check_sidecar("http://127.0.0.1:7777", "key", 5)}
    assert results["open-files"].status == PASS
    _serve(monkeypatch, {"/v1/host/health": (200, {"status": "ok"}), "/v1/mind/state": (200, {})})
    results = {r.name: r for r in check_sidecar("http://127.0.0.1:7777", "key", 5)}
    assert results["open-files"].status == SKIP


def test_vector_store_check_fails_when_the_library_is_missing(monkeypatch):
    import importlib.util
    from protagine.doctor import check_vector_store
    assert check_vector_store().status == PASS
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None if name == "lancedb" else real(name, *a, **k))
    result = check_vector_store()
    assert result.status == FAIL and "pipx install --force protagine" in result.remedy
    assert doctor.run_local_checks()[0].name == "vector-store"


def _service_instance(tmp_path, monkeypatch, platform):
    """The doctor's view of this instance's user service, managed by the fake user manager."""
    import socket
    from protagine import init
    from protagine.services.instance import InstanceService
    from test_instance_service import Manager
    (tmp_path / "instance").mkdir()
    _instance(tmp_path / "instance", monkeypatch, {})
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError()))
    manager = Manager()
    service = InstanceService(tmp_path / "instance", tmp_path / "hermes", python=str(tmp_path / "venv/bin/python"),
                              home=tmp_path / "user", platform=platform, runner=manager)
    monkeypatch.setattr(init, "_service", lambda cfg: service)
    monkeypatch.setattr(service, "health", lambda: {"status": "ok", "problems": []})
    return service



@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_service_check_says_whether_the_instance_service_keeps_the_sidecar_up(tmp_path, monkeypatch, platform):
    """Cutover operability-3: a sidecar started by hand ('protagine start --detach') answers every other
    check, but nothing restarts it after a crash or starts it at login. The service check says whether
    this instance's user service is installed, running and written by this version."""
    service = _service_instance(tmp_path, monkeypatch, platform)
    result = doctor.check_service()
    assert result.name == "service" and result.status == WARN
    assert "no user service" in result.detail and "protagine service install" in result.remedy

    service.install()
    result = doctor.check_service()
    assert result.status == FAIL and "not running" in result.detail and "protagine service start" in result.remedy

    service.start()
    result = doctor.check_service()
    assert result.status == PASS and service.label in result.detail and "321" in result.detail

    # A unit an earlier release wrote: one log shared with the rotating logger, a restart every 5 s.
    if platform == "darwin":
        plist = plistlib.loads(service.definition.read_bytes())
        plist.update(ThrottleInterval=5, StandardOutPath=str(service.log), StandardErrorPath=str(service.log))
        service.definition.write_bytes(plistlib.dumps(plist, sort_keys=True))
    else:
        unit = service.definition.read_text().replace("RestartSec=30", "RestartSec=5")
        service.definition.write_text(unit.replace(str(service.manager_log), str(service.log)))
    result = doctor.check_service()
    assert result.status == WARN and "earlier release" in result.detail
    assert "protagine service stop" in result.remedy and "protagine service install" in result.remedy
    assert "service" in [r.name for r in doctor.run_local_checks()]


def test_service_check_skips_where_no_user_manager_exists(tmp_path, monkeypatch):
    from protagine import init
    from protagine.services.instance import ServiceError
    _instance(tmp_path, monkeypatch, {})

    def unsupported(cfg):
        raise ServiceError("Instance autostart supports Linux systemd user services and macOS launchd")

    monkeypatch.setattr(init, "_service", unsupported)
    result = doctor.check_service()
    assert result.status == SKIP and "autostart supports" in result.detail
