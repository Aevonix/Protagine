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
    assert results[1].name == "sidecar-auth" and results[1].status == PASS


def test_semantic_recall_is_skipped_without_an_embedding_endpoint(tmp_path, monkeypatch):
    _instance(tmp_path, monkeypatch, {"embed_url": ""})
    calls = _serve(monkeypatch, {})
    result = check_semantic_recall("http://127.0.0.1:7777", "key", 5)
    assert result.status == SKIP and "router.embed_url" in result.detail
    assert calls == []


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
    assert names == {"sidecar": PASS, "sidecar-auth": PASS, "semantic-recall": FAIL}

    def down(url, api_key="", timeout=10.0):
        raise OSError("connection refused")
    monkeypatch.setattr(doctor, "_http_get", down)
    names = {r.name: r.status for r in doctor.run_doctor("http://127.0.0.1:7777", "key", 5)}
    assert names == {"sidecar": FAIL}
