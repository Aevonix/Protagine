"""``/v1/host/health`` says in words why it is not ``ok``.

A configured embedder that failed to initialise used to leave the sidecar
serving keyword recall with a health note that read as optional. Now the
failure is a problem sentence, the status is degraded, and ``embed/health``
carries the same reason.
"""
import pytest

from protagine.api.routers import host


@pytest.fixture
def quiet_host(monkeypatch):
    """Remove every other degradation source so only the embedder decides."""
    from protagine import vector
    monkeypatch.setattr(host, "_telemetry", None)
    monkeypatch.setattr(host, "_commitment_store", None)
    monkeypatch.setattr(host, "_embedder", None)
    monkeypatch.setattr(vector, "_store", None)
    monkeypatch.setattr(host, "_embed_failure", None)
    return host


@pytest.mark.asyncio
async def test_configured_embedder_that_failed_is_a_named_problem(quiet_host):
    host.set_embed_failure("the embedder (provider=openai_api, model=some-model) did not initialise: "
                           "ValueError: Embedding response dimension 4096 differs from the configured 384")
    result = await host.health()
    assert result.status == "degraded"
    assert result.problems == ["semantic recall is off: the embedder (provider=openai_api, model=some-model) "
                               "did not initialise: ValueError: Embedding response dimension 4096 differs "
                               "from the configured 384"]
    assert result.notes["embed"].startswith("semantic recall is off: ")
    assert "embed" not in result.capabilities

    embed = await host.embed_health()
    assert embed.status == "error"
    assert "did not initialise" in (embed.error or "")


@pytest.mark.asyncio
async def test_no_embedder_and_no_failure_is_not_a_problem(quiet_host):
    result = await host.health()
    assert result.status == "ok"
    assert result.problems == []
    assert "embed" not in (result.notes or {})
    assert (await host.embed_health()).error == "embedder not initialized"


@pytest.mark.asyncio
async def test_clearing_the_failure_clears_the_problem(quiet_host):
    host.set_embed_failure("the vector store did not open: ImportError: No module named lancedb")
    assert (await host.health()).status == "degraded"
    host.set_embed_failure(None)
    assert (await host.health()).status == "ok"
