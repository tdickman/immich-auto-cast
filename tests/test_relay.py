from __future__ import annotations

import asyncio
import io
from typing import Any
from uuid import UUID

import aiohttp
import pytest
from PIL import Image, ImageChops

from cast_immich.config import RelaySettings
from cast_immich.immich import Preview
from cast_immich.relay import ImageRelay, _draw_metadata

ASSET_ID = UUID("12345678-1234-4234-8234-123456789abc")


def image_bytes(size: tuple[int, int] = (10, 10), image_format: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format=image_format)
    return output.getvalue()


def test_metadata_stays_inside_title_safe_right_margin() -> None:
    image = Image.new("RGB", (1280, 720), "black")
    original = image.copy()

    _draw_metadata(image, "A fairly long location name", "July 17, 2026")

    bounds = ImageChops.difference(original, image).getbbox()
    assert bounds is not None
    assert bounds[2] <= image.width - min(image.size) // 20 + 1


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
        assert image.size == (1280, 720)
        assert image.format == "JPEG"
        assert max(image.getpixel((10, 360))) <= 5
        assert image.getpixel((640, 360))[0] > 200


@pytest.mark.asyncio
async def test_location_is_drawn_in_the_bottom_right_corner() -> None:
    class LocatedSource(Source):
        async def fetch_location(self, asset_id: UUID) -> str | None:
            assert asset_id == ASSET_ID
            return "Portland, Oregon"

    body = image_bytes((640, 360))
    plain = ImageRelay(settings(), Source(Preview(body, "image/png")))
    located = ImageRelay(settings(), LocatedSource(Preview(body, "image/png")))

    await plain.mint(ASSET_ID)
    await located.mint(ASSET_ID)

    plain_preview = next(iter(plain._tokens.values())).preview
    located_preview = next(iter(located._tokens.values())).preview
    with (
        Image.open(io.BytesIO(plain_preview.body)) as plain_image,
        Image.open(io.BytesIO(located_preview.body)) as located_image,
    ):
        difference = ImageChops.difference(plain_image, located_image)
        assert difference.getbbox() is not None
        assert difference.crop((900, 500, 1280, 720)).getbbox() is not None


@pytest.mark.asyncio
async def test_date_is_drawn_beneath_location() -> None:
    class LocatedSource(Source):
        async def fetch_location(self, asset_id: UUID) -> str | None:
            assert asset_id == ASSET_ID
            return "Portland, Oregon"

    class MetadataSource(Source):
        async def fetch_metadata(self, asset_id: UUID) -> tuple[str | None, str | None]:
            assert asset_id == ASSET_ID
            return "Portland, Oregon", "July 4, 2026"

    body = image_bytes((640, 360))
    location_only = ImageRelay(settings(), LocatedSource(Preview(body, "image/png")))
    metadata = ImageRelay(settings(), MetadataSource(Preview(body, "image/png")))

    await location_only.mint(ASSET_ID)
    await metadata.mint(ASSET_ID)

    location_preview = next(iter(location_only._tokens.values())).preview
    metadata_preview = next(iter(metadata._tokens.values())).preview
    with (
        Image.open(io.BytesIO(location_preview.body)) as location_image,
        Image.open(io.BytesIO(metadata_preview.body)) as metadata_image,
    ):
        difference = ImageChops.difference(location_image, metadata_image)
        assert difference.crop((900, 500, 1280, 720)).getbbox() is not None


@pytest.mark.asyncio
async def test_portrait_metadata_is_drawn_in_screen_letterbox() -> None:
    class MetadataSource(Source):
        async def fetch_metadata(self, asset_id: UUID) -> tuple[str | None, str | None]:
            return "Portland, Oregon", "July 4, 2026"

    body = image_bytes((360, 640))
    plain = ImageRelay(settings(), Source(Preview(body, "image/png")))
    metadata = ImageRelay(settings(), MetadataSource(Preview(body, "image/png")))

    await plain.mint(ASSET_ID)
    await metadata.mint(ASSET_ID)

    plain_preview = next(iter(plain._tokens.values())).preview
    metadata_preview = next(iter(metadata._tokens.values())).preview
    with (
        Image.open(io.BytesIO(plain_preview.body)) as plain_image,
        Image.open(io.BytesIO(metadata_preview.body)) as metadata_image,
    ):
        difference = ImageChops.difference(plain_image, metadata_image)
        assert plain_image.size == metadata_image.size == (1280, 720)
        assert difference.crop((900, 500, 1280, 720)).getbbox() is not None


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
