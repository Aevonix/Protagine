"""Image sources for embedding come straight from API request bodies.

They may be raw bytes, a base64/data-URL string, or a public http(s) URL.
A local file path must not be read, and a URL fetch must not reach private
addresses (directly or via redirect) or download without a size cap.
"""

from __future__ import annotations

import asyncio
import base64
import socket

import httpx
import pytest

from protagine.vector import image_preprocess as ip

PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAC"
    "hwGA60e6kgAAAABJRU5ErkJggg=="
)
PNG = base64.b64decode(PNG_B64)


# ── Local paths ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_existing_file_path_is_not_read(tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    with pytest.raises(ValueError):
        await ip.load_image(str(secret))


@pytest.mark.asyncio
async def test_missing_file_path_is_not_a_file_error():
    # Nothing about the filesystem leaks back, not even "not found".
    with pytest.raises(ValueError):
        await ip.load_image("/etc/hostname")


# ── Inline sources ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bytes_base64_and_data_url_still_load():
    assert await ip.load_image(PNG) == (PNG, "image/png")
    assert await ip.load_image(PNG_B64) == (PNG, "image/png")
    assert await ip.load_image("data:image/png;base64," + PNG_B64) == (PNG, "image/png")
    assert await ip.load_image(PNG, "image/x-custom") == (PNG, "image/x-custom")


@pytest.mark.asyncio
async def test_non_base64_string_is_rejected():
    with pytest.raises(ValueError):
        await ip.load_image("not an image, not a url")


def test_request_schema_has_no_file_path_field():
    from protagine.api.schemas.host import ImageEmbedRequest

    assert "image_path" not in ImageEmbedRequest.model_fields


# ── URLs ─────────────────────────────────────────────────────────────────


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _never(request):  # pragma: no cover - only runs when the guard fails
    raise AssertionError(f"fetch reached the network: {request.url}")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1/x.png",
        "http://10.0.0.1/x.png",
        "http://192.168.1.1/x.png",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x.png",
        "http://[::ffff:127.0.0.1]/x.png",
        "http://localhost/x.png",
        "http://user:pw@8.8.8.8/x.png",
        "ftp://8.8.8.8/x.png",
    ],
)
@pytest.mark.asyncio
async def test_private_or_malformed_urls_are_refused_before_any_request(url):
    async with _client(_never) as client:
        with pytest.raises(ValueError):
            await ip._fetch_image(url, client)


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_refused():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret.png"})

    async with _client(handler) as client:
        with pytest.raises(ValueError, match="non-public"):
            await ip._fetch_image("http://8.8.8.8/img.png", client)
    assert seen == ["http://8.8.8.8/img.png"]


@pytest.mark.asyncio
async def test_redirect_chain_is_capped():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://8.8.8.8/again.png"})

    async with _client(handler) as client:
        with pytest.raises(ValueError, match="redirects"):
            await ip._fetch_image("http://8.8.8.8/img.png", client)


@pytest.mark.asyncio
async def test_declared_oversize_body_is_refused(monkeypatch):
    monkeypatch.setattr(ip, "MAX_IMAGE_SIZE_BYTES", 1024)

    def handler(request):
        return httpx.Response(200, headers={"content-length": "4096", "content-type": "image/png"})

    async with _client(handler) as client:
        with pytest.raises(ValueError, match="exceeds limit"):
            await ip._fetch_image("http://8.8.8.8/img.png", client)


@pytest.mark.asyncio
async def test_streamed_body_over_cap_is_refused(monkeypatch):
    monkeypatch.setattr(ip, "MAX_IMAGE_SIZE_BYTES", 1024)

    def handler(request):
        # No content-length: the cap must hold while streaming.
        return httpx.Response(
            200, stream=httpx.ByteStream(b"x" * 4096), headers={"content-type": "image/png"},
        )

    async with _client(handler) as client:
        with pytest.raises(ValueError, match="exceeds limit"):
            await ip._fetch_image("http://8.8.8.8/img.png", client)


@pytest.mark.asyncio
async def test_error_status_is_a_value_error():
    async with _client(lambda request: httpx.Response(404)) as client:
        with pytest.raises(ValueError, match="404"):
            await ip._fetch_image("http://8.8.8.8/img.png", client)


@pytest.mark.asyncio
async def test_public_url_fetch_returns_bytes_and_mime(monkeypatch):
    def handler(request):
        if request.url.path == "/moved.png":
            return httpx.Response(301, headers={"location": "/img.png"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png; charset=binary"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    assert await ip.load_image("http://8.8.8.8/moved.png") == (PNG, "image/png")


@pytest.mark.asyncio
async def test_fetch_connects_to_the_checked_address_not_the_name(monkeypatch):
    async def resolve(host, port, **kwargs):
        assert host == "img.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["host"],
                     request.extensions.get("sni_hostname")))
        if request.url.path == "/moved.png":
            return httpx.Response(302, headers={"location": "https://img.example/img.png"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    async with _client(handler) as client:
        assert await ip._fetch_image("https://img.example:8443/moved.png", client) == (PNG, "image/png")
    # The client never resolves the name itself, so a second DNS answer that
    # points at a private address cannot reach the connection unchecked.
    assert seen == [
        ("93.184.216.34", "img.example:8443", "img.example"),
        ("93.184.216.34", "img.example", "img.example"),
    ]
