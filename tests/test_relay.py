from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import aiohttp
import pytest

from cast_immich.config import RelaySettings
from cast_immich.immich import Preview
from cast_immich.relay import ImageRelay

ASSET_ID = UUID("12345678-1234-4234-8234-123456789abc")


class Source:
    def __init__(self, preview: Preview) -> None:
        self.preview = preview
        self.calls = 0

    async def fetch_preview(self, asset_id: UUID, max_bytes: int | None = None) -> Preview:
        assert asset_id == ASSET_ID
        self.calls += 1
        return self.preview


def settings() -> RelaySettings:
    return RelaySettings("127.0.0.1", 8787, "192.168.1.5", 60, 1024, 2)


@pytest.mark.asyncio
async def test_valid_capability_supports_repeated_get_and_head(serve_app: Any) -> None:
    source = Source(Preview(b"photo", "image/webp"))
    relay = ImageRelay(settings(), source)
    public_url, content_type = await relay.mint(ASSET_ID)
    token = public_url.rsplit("/", 1)[1]
    server = await serve_app(relay.app)
    url = server.make_url(f"/image/{token}")

    async with aiohttp.ClientSession() as session:
        first = await session.get(url)
        second = await session.get(url)
        head = await session.head(url)
        assert await first.read() == await second.read() == b"photo"
        assert first.headers["Content-Type"] == "image/webp"
        assert head.headers["Content-Length"] == "5"
        assert head.headers["Access-Control-Allow-Origin"] == "*"
    assert content_type == "image/webp"
    assert source.calls == 1


@pytest.mark.asyncio
async def test_unknown_expired_and_unsupported_tokens_are_safe(serve_app: Any) -> None:
    now = [10.0]
    relay = ImageRelay(settings(), Source(Preview(b"photo", "image/jpeg")), clock=lambda: now[0])
    public_url, _ = await relay.mint(ASSET_ID)
    token = public_url.rsplit("/", 1)[1]
    server = await serve_app(relay.app)
    async with aiohttp.ClientSession() as session:
        assert (await session.get(server.make_url("/image/not-a-token"))).status == 404
        assert (await session.post(server.make_url(f"/image/{token}"))).status == 405
        now[0] = 100.0
        assert (await session.get(server.make_url(f"/image/{token}"))).status == 404


@pytest.mark.asyncio
async def test_non_image_is_rejected_before_url_is_minted() -> None:
    relay = ImageRelay(settings(), Source(Preview(b"html", "text/html")))
    with pytest.raises(Exception, match="supported image"):
        await relay.mint(ASSET_ID)


class BlockingSource:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def fetch_preview(self, asset_id: UUID, max_bytes: int | None = None) -> Preview:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_close_cancels_active_preview_preparation() -> None:
    source = BlockingSource()
    relay = ImageRelay(settings(), source)
    mint = asyncio.create_task(relay.mint(ASSET_ID))
    await source.started.wait()
    await relay.close()
    assert mint.cancelled()
    with pytest.raises(Exception, match="closed"):
        await relay.mint(ASSET_ID)
