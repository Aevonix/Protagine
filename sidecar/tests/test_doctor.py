"""``protagine doctor``: the sidecar checks read the served health and fail loudly."""
from __future__ import annotations

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
