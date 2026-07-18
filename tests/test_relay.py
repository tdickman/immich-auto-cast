from __future__ import annotations

import asyncio
import io
from typing import Any
from uuid import UUID

import aiohttp
import pytest
from aiohttp import web
from PIL import Image, ImageChops, ImageStat

from cast_immich.config import ImmichSettings, RelaySettings
from cast_immich.immich import Asset, ImmichClient, MediaType, Preview
from cast_immich.relay import ImageRelay, QrPlacement, _draw_metadata, _draw_web_qr, _make_qr

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


def test_web_qr_badge_adapts_to_background_brightness() -> None:
    qr = _make_qr("http://192.168.1.5:8080/")
    dark = Image.new("RGB", (1280, 720), (40, 40, 40))
    light = Image.new("RGB", (1280, 720), (220, 220, 220))

    _draw_web_qr(dark, qr)
    _draw_web_qr(light, qr)

    margin = min(dark.size) // 20
    padding = max(2, qr.width // 12)
    top = dark.height - qr.height - padding * 2 - margin
    module_x, module_y = next(
        (x, y)
        for y in range(qr.height)
        for x in range(qr.width)
        if qr.getpixel((x, y)) == (0, 0, 0)
    )
    dark_module = dark.getpixel((margin + padding + module_x, top + padding + module_y))
    light_module = light.getpixel((margin + padding + module_x, top + padding + module_y))

    assert min(dark_module) > 180
    assert max(light_module) < 70
    assert max(dark.getpixel((margin + 1, top + qr.height // 2))) < 40
    assert min(light.getpixel((margin + 1, top + qr.height // 2))) > 220


def test_web_qr_uses_corner_and_exact_insets() -> None:
    qr = _make_qr("http://192.168.1.5:8080/", 2)
    image = Image.new("RGB", (1280, 720), (220, 220, 220))
    original = image.copy()

    _draw_web_qr(image, qr, QrPlacement(2, "top-right", 24, 18, 75))

    bounds = ImageChops.difference(original, image).getbbox()
    assert bounds is not None
    assert bounds[0] > image.width - qr.width - 40
    assert bounds[1] == 18
    assert bounds[2] == image.width - 24


def test_web_qr_opacity_dims_the_badge() -> None:
    qr = _make_qr("http://192.168.1.5:8080/")
    original = Image.new("RGB", (1280, 720), (40, 40, 40))
    dimmed = original.copy()
    opaque = original.copy()

    _draw_web_qr(dimmed, qr, QrPlacement(1, "bottom-left", 36, 36, 50))
    _draw_web_qr(opaque, qr, QrPlacement(1, "bottom-left", 36, 36, 100))

    dimmed_difference = ImageStat.Stat(ImageChops.difference(original, dimmed)).sum
    opaque_difference = ImageStat.Stat(ImageChops.difference(original, opaque)).sum
    assert sum(dimmed_difference) < sum(opaque_difference)


def test_web_qr_accepts_authenticated_dashboard_url() -> None:
    qr = _make_qr("http://192.168.1.5:8080/?password=random-secret")

    assert qr.width > 0


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
async def test_video_capability_streams_single_ranges_from_immich(serve_app: Any) -> None:
    body = b"0123456789"
    observed: list[tuple[str, str | None]] = []

    async def video(request: Any) -> Any:
        range_header = request.headers.get("Range")
        observed.append((request.method, range_header))
        if range_header == "bytes=2-5":
            return web.Response(
                status=206,
                body=body[2:6],
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": "bytes 2-5/10",
                    "Accept-Ranges": "bytes",
                },
            )
        return web.Response(
            body=body,
            headers={"Content-Type": "video/mp4", "Accept-Ranges": "bytes"},
        )

    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/api/assets/{asset_id}/video/playback", video)
    upstream = await serve_app(upstream_app)
    immich_settings = ImmichSettings(str(upstream.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(immich_settings) as client:
        relay = ImageRelay(settings(), client)
        public_url, content_type = await relay.mint(
            Asset(ASSET_ID, media_type=MediaType.VIDEO, duration=10)
        )
        token = public_url.rsplit("/", 1)[1]
        server = await serve_app(relay.app)
        async with aiohttp.ClientSession() as session:
            response = await session.get(
                server.make_url(f"/video/{token}"), headers={"Range": "bytes=2-5"}
            )
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 2-5/10"
            assert await response.read() == b"2345"
            invalid = await session.get(
                server.make_url(f"/video/{token}"), headers={"Range": "bytes=0-1,4-5"}
            )
            assert invalid.status == 416

    assert content_type == "video/mp4"
    assert observed == [("GET", "bytes=2-5")]


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
async def test_web_interface_qr_is_drawn_only_when_enabled() -> None:
    body = image_bytes((640, 360))
    relay = ImageRelay(
        settings(), Source(Preview(body, "image/png")), dashboard_url="http://192.168.1.5:8080/"
    )

    await relay.mint(ASSET_ID)
    await relay.mint(ASSET_ID, show_web_qr=True)
    await relay.mint(ASSET_ID, show_web_qr=True, web_qr_size=3)

    plain_preview, qr_preview, large_qr_preview = [
        capability.preview for capability in relay._tokens.values()
    ]
    with (
        Image.open(io.BytesIO(plain_preview.body)) as plain_image,
        Image.open(io.BytesIO(qr_preview.body)) as qr_image,
        Image.open(io.BytesIO(large_qr_preview.body)) as large_qr_image,
    ):
        difference = ImageChops.difference(plain_image, qr_image)
        large_difference = ImageChops.difference(plain_image, large_qr_image)
        assert difference.crop((0, 500, 200, 720)).getbbox() is not None
        assert difference.crop((200, 0, 1280, 720)).getbbox() is None
        assert large_difference.getbbox()[2] > difference.getbbox()[2]

    assert relay._source.calls == 3


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
async def test_confirmed_capability_survives_expiry_until_retired(serve_app: Any) -> None:
    now = [10.0]
    relay = ImageRelay(
        settings(),
        Source(Preview(image_bytes(image_format="JPEG"), "image/jpeg")),
        clock=lambda: now[0],
    )
    public_url, _ = await relay.mint(ASSET_ID)
    token = public_url.rsplit("/", 1)[1]
    relay.confirm(public_url)
    server = await serve_app(relay.app)

    now[0] = 100.0
    async with aiohttp.ClientSession() as session:
        assert (await session.get(server.make_url(f"/image/{token}"))).status == 200
        relay.retire(public_url)
        assert (await session.get(server.make_url(f"/image/{token}"))).status == 404


@pytest.mark.asyncio
async def test_pinned_capability_is_not_evicted_by_speculative_mints() -> None:
    class MultiSource:
        async def fetch_preview(self, asset_id: UUID, max_bytes: int | None = None) -> Preview:
            return Preview(image_bytes(), "image/png")

    relay = ImageRelay(settings(), MultiSource(), max_tokens=1)
    pinned_url, _ = await relay.mint(ASSET_ID)
    relay.confirm(pinned_url)
    await relay.mint(UUID(int=2))

    assert pinned_url.rsplit("/", 1)[1] in relay._tokens


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
