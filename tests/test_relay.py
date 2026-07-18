from __future__ import annotations

import asyncio
import io
from typing import Any
from uuid import UUID

import aiohttp
import pytest
from PIL import Image

from cast_immich.config import RelaySettings
from cast_immich.immich import Preview
from cast_immich.relay import ImageRelay

ASSET_ID = UUID("12345678-1234-4234-8234-123456789abc")


def image_bytes(size: tuple[int, int] = (10, 10), image_format: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format=image_format)
    return output.getvalue()


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
    source = Source(Preview(image_bytes(), "image/png"))
    relay = ImageRelay(settings(), source)
    public_url, content_type = await relay.mint(ASSET_ID)
    token = public_url.rsplit("/", 1)[1]
    server = await serve_app(relay.app)
    url = server.make_url(f"/image/{token}")

    async with aiohttp.ClientSession() as session:
        first = await session.get(url)
        second = await session.get(url)
        head = await session.head(url)
        first_body = await first.read()
        assert first_body == await second.read()
        assert first.headers["Content-Type"] == "image/jpeg"
        assert head.headers["Content-Length"] == str(len(first_body))
        assert head.headers["Access-Control-Allow-Origin"] == "*"
    assert content_type == "image/jpeg"
    assert source.calls == 1


@pytest.mark.asyncio
async def test_preview_is_resized_to_cast_display_limit() -> None:
    relay = ImageRelay(settings(), Source(Preview(image_bytes((1913, 1440)), "image/png")))

    await relay.mint(ASSET_ID)

    capability = next(iter(relay._tokens.values()))
    with Image.open(io.BytesIO(capability.preview.body)) as image:
        assert image.size == (957, 720)
        assert image.format == "JPEG"


@pytest.mark.asyncio
async def test_preloaded_preview_is_reused_by_next_load() -> None:
    class MultiSource:
        def __init__(self) -> None:
            self.calls: list[UUID] = []

        async def fetch_preview(self, asset_id: UUID, max_bytes: int | None = None) -> Preview:
            self.calls.append(asset_id)
            color = "red" if asset_id == ASSET_ID else "blue"
            output = io.BytesIO()
            Image.new("RGB", (16, 9), color).save(output, format="PNG")
            return Preview(output.getvalue(), "image/png")

    next_id = UUID(int=2)
    source = MultiSource()
    relay = ImageRelay(settings(), source)

    await relay.mint(ASSET_ID)
    await relay.preload(next_id)
    _url, content_type = await relay.mint(next_id)

    capability = next(reversed(relay._tokens.values()))
    assert source.calls == [ASSET_ID, next_id]
    assert content_type == "image/jpeg"
    with Image.open(io.BytesIO(capability.preview.body)) as image:
        assert image.format == "JPEG"


@pytest.mark.asyncio
async def test_unknown_expired_and_unsupported_tokens_are_safe(serve_app: Any) -> None:
    now = [10.0]
    relay = ImageRelay(
        settings(),
        Source(Preview(image_bytes(image_format="JPEG"), "image/jpeg")),
        clock=lambda: now[0],
    )
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
