"""Tests for protagine.vector.openai_provider — API embedding provider."""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from protagine.vector.config import EmbeddingConfig
from protagine.vector.openai_provider import OpenAIAPIEmbeddingProvider


class TestOpenAIAPIEmbeddingProvider:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("request_dimensions", [None, 2])
    async def test_request_width_is_explicit_and_response_width_is_validated(self, monkeypatch, request_dimensions):
        import httpx

        requests = []
        vector = [1., 0.]
        def response(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"model": "served-neutral", "data": [
                {"index": 0, "embedding": vector}]})
        client = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(
            transport=httpx.MockTransport(response), **kw))
        provider = OpenAIAPIEmbeddingProvider(EmbeddingConfig(
            provider="openai_api", model_id="neutral", dimensions=2,
            request_dimensions=request_dimensions))
        provider.configure("http://fixture/v1", "")
        assert await provider.embed("A copper key.") == [1., 0.]
        expected = {"model": "neutral", "input": ["A copper key."]}
        if request_dimensions is not None:
            expected["dimensions"] = request_dimensions
        assert requests == [expected]
        vector.append(0.)  # An endpoint that ignores the requested width is not truncated.
        with pytest.raises(ValueError, match="dimension"):
            await provider.embed("A different key.")
        assert len(requests) == 2

    def test_configure(self):
        config = EmbeddingConfig(provider="openai_api", model_id="text-embedding-3-small", dimensions=1536)
        provider = OpenAIAPIEmbeddingProvider(config)
        provider.configure("https://api.openai.com", "sk-test")
        assert provider._base_url == "https://api.openai.com"
        assert provider._api_key == "sk-test"

    def test_dimensions(self):
        config = EmbeddingConfig(provider="openai_api", model_id="text-embedding-3-small", dimensions=1536)
        provider = OpenAIAPIEmbeddingProvider(config)
        assert provider.dimensions == 1536

    @pytest.mark.asyncio
    async def test_warmup_without_config(self):
        config = EmbeddingConfig(provider="openai_api", model_id="test", dimensions=384)
        provider = OpenAIAPIEmbeddingProvider(config)
        await provider.warmup()  # Should not raise, just warn

    @pytest.mark.asyncio
    async def test_embed_batch_empty(self):
        config = EmbeddingConfig(provider="openai_api", model_id="test", dimensions=384)
        provider = OpenAIAPIEmbeddingProvider(config)
        result = await provider.embed_batch([])
        assert result == []


class TestMakeProvider:
    def test_openai_api_provider(self):
        from protagine.vector.embedder import make_provider
        config = EmbeddingConfig(provider="openai_api", model_id="text-embedding-3-small", dimensions=1536)
        provider = make_provider(config)
        assert isinstance(provider, OpenAIAPIEmbeddingProvider)

    def test_unknown_provider_raises(self):
        from protagine.vector.embedder import make_provider
        config = EmbeddingConfig(provider="nonexistent", model_id="test", dimensions=384)
        with pytest.raises(ValueError, match="Unknown embedding provider"):
            make_provider(config)


class TestRequestDimensionsConfig:
    @pytest.mark.parametrize("value", [0, -1, True, False, 2.0, "2"])
    def test_request_requires_positive_integer(self, value):
        with pytest.raises(ValueError, match="positive integer"):
            EmbeddingConfig(provider="openai_api", model_id="neutral", dimensions=2,
                            request_dimensions=value)

    def test_request_width_must_match_validation_width(self):
        with pytest.raises(ValueError, match="equal.*output dimensions"):
            EmbeddingConfig(provider="openai_api", model_id="neutral", dimensions=3,
                            request_dimensions=2)

    @pytest.mark.parametrize("provider", ["cpu", "cuda", "mlx", "native_mlx", "skip"])
    def test_local_providers_cannot_silently_ignore_request_width(self, provider):
        with pytest.raises(ValueError, match="openai_api text"):
            EmbeddingConfig(provider=provider, model_id="neutral", dimensions=2,
                            request_dimensions=2)

    def test_environment_option_is_independent_of_validation_width(self, monkeypatch):
        monkeypatch.setenv("PROTAGINE_EMBED_PROVIDER", "openai_api")
        monkeypatch.setenv("PROTAGINE_EMBED_MODEL", "neutral")
        monkeypatch.setenv("PROTAGINE_EMBED_DIMS", "2")
        monkeypatch.delenv("PROTAGINE_EMBED_REQUEST_DIMS", raising=False)
        assert EmbeddingConfig.from_env().request_dimensions is None
        monkeypatch.setenv("PROTAGINE_EMBED_REQUEST_DIMS", "2")
        config = EmbeddingConfig.from_env()
        assert config.request_dimensions == config.dimensions == 2
        monkeypatch.setenv("PROTAGINE_EMBED_REQUEST_DIMS", "3")
        with pytest.raises(ValueError, match="equal.*output dimensions"):
            EmbeddingConfig.from_env()

    def test_multimodal_api_cannot_silently_ignore_text_request_width(self):
        from protagine.vector.multimodal_provider import make_multimodal_provider
        config = EmbeddingConfig(provider="openai_api", model_id="neutral", dimensions=2,
                                base_url="http://fixture/v1", request_dimensions=2)
        with pytest.raises(ValueError, match="only for text API"):
            make_multimodal_provider(config)


@pytest.mark.asyncio
async def test_width_is_learned_from_the_endpoint_when_none_is_declared(monkeypatch):
    """dimensions=0 means the endpoint's first vector defines the width; later vectors are held to it."""
    import httpx

    vector = [0.5, 0.25, 0.125]
    def response(request):
        return httpx.Response(200, json={"model": "served-neutral", "data": [
            {"index": 0, "embedding": list(vector)}]})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(
        transport=httpx.MockTransport(response), **kw))
    provider = OpenAIAPIEmbeddingProvider(EmbeddingConfig(provider="openai_api", model_id="neutral", dimensions=0))
    provider.configure("http://fixture/v1", "")
    assert provider.dimensions == 0
    await provider.warmup()
    assert provider.dimensions == 3
    assert await provider.embed("A copper key.") == [0.5, 0.25, 0.125]

    from protagine.vector.embedder import EmbeddingPipeline
    pipeline = EmbeddingPipeline(provider)
    await pipeline.warmup()
    assert pipeline.index_identity.dimensions == 3

    vector.append(0.0)  # The endpoint changing its width is a mismatch, named after the setting.
    with pytest.raises(ValueError, match="router.embed_dims"):
        await provider.embed("A different key.")


@pytest.mark.asyncio
async def test_declared_width_mismatch_names_the_setting(monkeypatch):
    import httpx

    def response(request):
        return httpx.Response(200, json={"model": "served-neutral", "data": [
            {"index": 0, "embedding": [0.1] * 8}]})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(
        transport=httpx.MockTransport(response), **kw))
    provider = OpenAIAPIEmbeddingProvider(EmbeddingConfig(provider="openai_api", model_id="neutral", dimensions=4))
    provider.configure("http://fixture/v1", "")
    with pytest.raises(ValueError, match=r"dimension 8 differs from the configured 4 \(router.embed_dims"):
        await provider.warmup()
