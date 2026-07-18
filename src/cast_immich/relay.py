from __future__ import annotations

import asyncio
import io
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol, cast
from uuid import UUID

import aiohttp
import qrcode  # type: ignore[import-untyped]
from aiohttp import web
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat, UnidentifiedImageError

from .config import RelaySettings
from .immich import Asset, AssetUnavailable, MediaType, Preview

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_TYPES = {
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
ALLOWED_VIDEO_TYPES = {"video/mp4"}
SAFE_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "private, max-age=60",
    "X-Content-Type-Options": "nosniff",
}
MAX_CAST_SIZE = (1280, 720)
MAX_IMAGE_PIXELS = 40_000_000
PREVIEW_CACHE_SIZE = 3


class PreviewSource(Protocol):
    async def fetch_preview(self, asset_id: UUID, max_bytes: int | None = None) -> Preview: ...

    async def fetch_location(self, asset_id: UUID) -> str | None: ...

    async def fetch_metadata(self, asset_id: UUID) -> tuple[str | None, str | None]: ...

    async def open_video(
        self, asset_id: UUID, method: str, range_header: str | None
    ) -> aiohttp.ClientResponse: ...


@dataclass(frozen=True, slots=True)
class Capability:
    asset_id: UUID
    expires_at: float
    preview: Preview | None
    media_type: MediaType = MediaType.IMAGE
    pinned: bool = False


@dataclass(frozen=True, slots=True)
class QrPlacement:
    size: float
    position: str
    inset_x: int
    inset_y: int
    opacity: int
    lossless: bool
    quiet_zone: int


DEFAULT_QR_PLACEMENT = QrPlacement(2, "bottom-left", 36, 36, 75, False, 4)


class ImageRelay:
    def __init__(
        self,
        settings: RelaySettings,
        source: PreviewSource,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_tokens: int = 32,
        dashboard_url: str | None = None,
    ) -> None:
        self._settings = settings
        self._source = source
        self._clock = clock
        self._max_tokens = max_tokens
        self._tokens: OrderedDict[str, Capability] = OrderedDict()
        self._previews: OrderedDict[tuple[UUID, QrPlacement | None], Preview] = OrderedDict()
        self._preview_tasks: dict[tuple[UUID, QrPlacement | None], asyncio.Task[Preview]] = {}
        self._dashboard_url = dashboard_url
        self._web_qrs: dict[tuple[float, int], Image.Image] = {}
        self._semaphore = asyncio.Semaphore(settings.max_concurrent)
        self._stream_semaphore = asyncio.Semaphore(settings.max_concurrent)
        self._app = web.Application(client_max_size=1024)
        self._app.router.add_route("*", "/image/{token}", self._handle)
        self._app.router.add_route("*", "/video/{token}", self._handle)
        self._runner: web.AppRunner | None = None
        self._closed = False
        self._active_mints: set[asyncio.Task[object]] = set()

    @property
    def app(self) -> web.Application:
        return self._app

    def transfer_capabilities_to(self, target: ImageRelay) -> None:
        """Preserve receiver retries when replacing a relay on the same listener."""
        self._purge()
        target._purge()
        target._tokens.update(self._tokens)
        target._trim_tokens()

    def confirm(self, url: str) -> None:
        token = self._token_from_url(url)
        capability = self._tokens.get(token)
        if capability is not None:
            self._tokens[token] = replace(capability, pinned=True)
            self._tokens.move_to_end(token)

    def retire(self, url: str) -> None:
        token = self._token_from_url(url)
        capability = self._tokens.get(token)
        if capability is not None:
            self._tokens[token] = replace(capability, pinned=False)
        self._purge()

    async def preload(
        self,
        asset: Asset | UUID,
        *,
        show_web_qr: bool = False,
        web_qr_size: float = 2,
        web_qr_position: str = "bottom-left",
        web_qr_inset_x: int = 36,
        web_qr_inset_y: int = 36,
        web_qr_opacity: int = 75,
        web_qr_lossless: bool = False,
        web_qr_quiet_zone: int = 4,
    ) -> None:
        """Fetch and normalize an image before the receiver needs it."""
        if isinstance(asset, Asset) and asset.media_type is MediaType.VIDEO:
            return
        await self._get_preview(
            asset.id if isinstance(asset, Asset) else asset,
            show_web_qr=show_web_qr,
            web_qr_size=web_qr_size,
            web_qr_position=web_qr_position,
            web_qr_inset_x=web_qr_inset_x,
            web_qr_inset_y=web_qr_inset_y,
            web_qr_opacity=web_qr_opacity,
            web_qr_lossless=web_qr_lossless,
            web_qr_quiet_zone=web_qr_quiet_zone,
        )

    async def preload_media(
        self,
        asset: Asset,
        *,
        show_web_qr: bool = False,
        web_qr_size: float = 2,
        web_qr_position: str = "bottom-left",
        web_qr_inset_x: int = 36,
        web_qr_inset_y: int = 36,
        web_qr_opacity: int = 75,
        web_qr_lossless: bool = False,
        web_qr_quiet_zone: int = 4,
    ) -> None:
        await self.preload(
            asset,
            show_web_qr=show_web_qr,
            web_qr_size=web_qr_size,
            web_qr_position=web_qr_position,
            web_qr_inset_x=web_qr_inset_x,
            web_qr_inset_y=web_qr_inset_y,
            web_qr_opacity=web_qr_opacity,
            web_qr_lossless=web_qr_lossless,
            web_qr_quiet_zone=web_qr_quiet_zone,
        )

    async def mint(
        self,
        asset: Asset | UUID,
        *,
        show_web_qr: bool = False,
        web_qr_size: float = 2,
        web_qr_position: str = "bottom-left",
        web_qr_inset_x: int = 36,
        web_qr_inset_y: int = 36,
        web_qr_opacity: int = 75,
        web_qr_lossless: bool = False,
        web_qr_quiet_zone: int = 4,
    ) -> tuple[str, str]:
        if self._closed:
            raise AssetUnavailable("media relay is closed")
        self._purge()
        media = asset if isinstance(asset, Asset) else Asset(asset)
        preview = (
            await self._get_preview(
                media.id,
                show_web_qr=show_web_qr,
                web_qr_size=web_qr_size,
                web_qr_position=web_qr_position,
                web_qr_inset_x=web_qr_inset_x,
                web_qr_inset_y=web_qr_inset_y,
                web_qr_opacity=web_qr_opacity,
                web_qr_lossless=web_qr_lossless,
                web_qr_quiet_zone=web_qr_quiet_zone,
            )
            if media.media_type is MediaType.IMAGE
            else None
        )
        if self._closed:
            raise AssetUnavailable("media relay is closed")
        token = secrets.token_urlsafe(24)
        self._tokens[token] = Capability(
            media.id,
            self._clock() + self._settings.token_lifetime,
            preview,
            media.media_type,
        )
        self._trim_tokens()
        path = "video" if media.media_type is MediaType.VIDEO else "image"
        content_type = "video/mp4" if preview is None else preview.content_type
        return f"{self._settings.advertised_base_url}/{path}/{token}", content_type

    async def mint_media(
        self,
        asset: Asset,
        *,
        show_web_qr: bool = False,
        web_qr_size: float = 2,
        web_qr_position: str = "bottom-left",
        web_qr_inset_x: int = 36,
        web_qr_inset_y: int = 36,
        web_qr_opacity: int = 75,
        web_qr_lossless: bool = False,
        web_qr_quiet_zone: int = 4,
    ) -> tuple[str, str]:
        return await self.mint(
            asset,
            show_web_qr=show_web_qr,
            web_qr_size=web_qr_size,
            web_qr_position=web_qr_position,
            web_qr_inset_x=web_qr_inset_x,
            web_qr_inset_y=web_qr_inset_y,
            web_qr_opacity=web_qr_opacity,
            web_qr_lossless=web_qr_lossless,
            web_qr_quiet_zone=web_qr_quiet_zone,
        )

    async def _get_preview(
        self,
        asset_id: UUID,
        *,
        show_web_qr: bool = False,
        web_qr_size: float = 2,
        web_qr_position: str = "bottom-left",
        web_qr_inset_x: int = 36,
        web_qr_inset_y: int = 36,
        web_qr_opacity: int = 75,
        web_qr_lossless: bool = False,
        web_qr_quiet_zone: int = 4,
    ) -> Preview:
        if self._closed:
            raise AssetUnavailable("image relay is closed")
        placement = (
            QrPlacement(
                web_qr_size,
                web_qr_position,
                web_qr_inset_x,
                web_qr_inset_y,
                web_qr_opacity,
                web_qr_lossless,
                web_qr_quiet_zone,
            )
            if show_web_qr
            else None
        )
        key = (asset_id, placement)
        preview = self._previews.get(key)
        if preview is not None:
            self._previews.move_to_end(key)
            return preview
        task = self._preview_tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                self._fetch_preview(asset_id, placement), name="image-preview-fetch"
            )
            self._preview_tasks[key] = task
            self._active_mints.add(task)
        try:
            preview = await asyncio.shield(task)
        finally:
            if task.done():
                self._preview_tasks.pop(key, None)
                self._active_mints.discard(task)
        self._previews[key] = preview
        self._previews.move_to_end(key)
        while len(self._previews) > PREVIEW_CACHE_SIZE:
            self._previews.popitem(last=False)
        return preview

    async def _fetch_preview(self, asset_id: UUID, placement: QrPlacement | None) -> Preview:
        async with self._semaphore:
            preview = await self._source.fetch_preview(asset_id, self._settings.max_response_bytes)
            if preview.content_type not in ALLOWED_IMAGE_TYPES:
                raise AssetUnavailable("asset preview is not a supported image type")
            location: str | None = None
            date: str | None = None
            fetch_metadata = getattr(self._source, "fetch_metadata", None)
            if fetch_metadata is not None:
                try:
                    location, date = await fetch_metadata(asset_id)
                except Exception:
                    logger.warning("asset_metadata_fetch_failed")
            else:
                fetch_location = getattr(self._source, "fetch_location", None)
                if fetch_location is not None:
                    try:
                        location = await fetch_location(asset_id)
                    except Exception:
                        logger.warning("asset_location_fetch_failed")
            qr = (
                self._web_qr(placement.size, placement.quiet_zone)
                if placement is not None
                else None
            )
            return await asyncio.to_thread(
                _normalize_preview, preview, location, date, qr, placement
            )

    def _web_qr(self, size: float, quiet_zone: int) -> Image.Image | None:
        if self._dashboard_url is None:
            return None
        key = (size, quiet_zone)
        qr = self._web_qrs.get(key)
        if qr is None:
            qr = _make_qr(self._dashboard_url, size, quiet_zone)
            self._web_qrs[key] = qr
        return qr

    async def start(self) -> None:
        if self._runner is not None:
            return
        self._closed = False
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._settings.bind_host, self._settings.port)
        try:
            await site.start()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self._closed = True
        active = list(self._active_mints)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
        self._tokens.clear()
        self._previews.clear()
        self._preview_tasks.clear()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        logger.info("media_requested")
        if request.method not in {"GET", "HEAD"}:
            return web.Response(status=405, headers={"Allow": "GET, HEAD", **SAFE_HEADERS})
        capability = self._tokens.get(request.match_info["token"])
        if capability is None or (not capability.pinned and capability.expires_at <= self._clock()):
            return web.Response(status=404, headers=SAFE_HEADERS)
        if capability.media_type is MediaType.VIDEO:
            async with self._stream_semaphore:
                return await self._handle_video(request, capability)
        preview = capability.preview
        if preview is None or preview.content_type not in ALLOWED_IMAGE_TYPES:
            return web.Response(status=502, headers=SAFE_HEADERS)
        headers = {"Content-Type": preview.content_type, "Content-Length": str(len(preview.body))}
        headers.update(SAFE_HEADERS)
        return web.Response(body=b"" if request.method == "HEAD" else preview.body, headers=headers)

    async def _handle_video(
        self, request: web.Request, capability: Capability
    ) -> web.StreamResponse:
        range_header = request.headers.get("Range")
        if (
            range_header is not None
            and re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header) is None
        ):
            return web.Response(status=416, headers=SAFE_HEADERS)
        try:
            upstream = await self._source.open_video(
                capability.asset_id, request.method, range_header
            )
        except AssetUnavailable:
            return web.Response(status=404, headers=SAFE_HEADERS)
        except Exception:
            logger.warning("video_stream_open_failed")
            return web.Response(status=502, headers=SAFE_HEADERS)
        async with upstream:
            content_type = upstream.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if upstream.status != 416 and content_type not in ALLOWED_VIDEO_TYPES:
                return web.Response(status=502, headers=SAFE_HEADERS)
            headers = dict(SAFE_HEADERS)
            for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                value = upstream.headers.get(name)
                if value is not None:
                    headers[name] = value
            headers.setdefault("Accept-Ranges", "bytes")
            if upstream.status == 416 or request.method == "HEAD":
                return web.Response(status=upstream.status, headers=headers)
            response = web.StreamResponse(status=upstream.status, headers=headers)
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
            except (aiohttp.ClientError, ConnectionError, TimeoutError):
                logger.info("video_stream_interrupted")
            return response

    def _purge(self) -> None:
        now = self._clock()
        expired = [
            token
            for token, item in self._tokens.items()
            if not item.pinned and item.expires_at <= now
        ]
        for token in expired:
            self._tokens.pop(token, None)

    def _trim_tokens(self) -> None:
        while len(self._tokens) > self._max_tokens:
            removable = next(
                (token for token, capability in self._tokens.items() if not capability.pinned),
                None,
            )
            if removable is None:
                break
            self._tokens.pop(removable)

    def _token_from_url(self, url: str) -> str:
        for path in ("image", "video"):
            prefix = f"{self._settings.advertised_base_url}/{path}/"
            if url.startswith(prefix):
                return url.removeprefix(prefix)
        return ""


def _normalize_preview(
    preview: Preview,
    location: str | None = None,
    date: str | None = None,
    web_qr: Image.Image | None = None,
    qr_placement: QrPlacement | None = None,
) -> Preview:
    try:
        with Image.open(io.BytesIO(preview.body)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise AssetUnavailable("asset preview dimensions exceed the safety limit")
            image = ImageOps.exif_transpose(source)
            if image.mode != "RGB":
                image = image.convert("RGB")
            scale = min(MAX_CAST_SIZE[0] / image.width, MAX_CAST_SIZE[1] / image.height)
            size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            if image.size != size:
                image = image.resize(size, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", MAX_CAST_SIZE, "black")
            canvas.paste(
                image,
                ((canvas.width - image.width) // 2, (canvas.height - image.height) // 2),
            )
            image = canvas
            if location or date:
                _draw_metadata(image, location, date)
            if web_qr is not None and qr_placement is not None:
                _draw_web_qr(image, web_qr, qr_placement)
            output = io.BytesIO()
            if qr_placement is not None and qr_placement.lossless:
                image.save(output, format="PNG", optimize=True)
                content_type = "image/png"
            else:
                image.save(output, format="JPEG", quality=90, optimize=True)
                content_type = "image/jpeg"
    except (OSError, UnidentifiedImageError):
        raise AssetUnavailable("asset preview is not a valid image") from None
    return Preview(output.getvalue(), content_type)


def _draw_metadata(image: Image.Image, location: str | None, date: str | None) -> None:
    labels = [value.strip()[:120] for value in (location, date) if value and value.strip()]
    if not labels:
        return
    draw = ImageDraw.Draw(image, "RGBA")
    font = ImageFont.load_default(size=max(12, min(image.size) // 28))
    padding = max(6, min(image.size) // 80)
    margin = max(12, min(image.size) // 20)
    available_width = max(1, image.width - (margin + padding) * 2)
    for index, label in enumerate(labels):
        while len(label) > 4 and draw.textlength(label, font=font) > available_width:
            label = f"{label[:-4].rstrip()}..."
        labels[index] = label
    label = "\n".join(labels)
    spacing = max(2, padding // 2)
    box = draw.multiline_textbbox((0, 0), label, font=font, spacing=spacing, align="right")
    width, height = box[2] - box[0], box[3] - box[1]
    right, bottom = image.width - margin, image.height - margin
    background = (
        right - width - padding * 2,
        bottom - height - padding * 2,
        right,
        bottom,
    )
    draw.rounded_rectangle(background, radius=padding, fill=(0, 0, 0, 155))
    draw.multiline_text(
        (right - padding - width, bottom - padding - height - box[1]),
        label,
        font=font,
        fill=(255, 255, 255, 235),
        spacing=spacing,
        align="right",
    )


def _draw_location(image: Image.Image, location: str) -> None:
    _draw_metadata(image, location, None)


def _draw_web_qr(
    image: Image.Image,
    qr: Image.Image,
    placement: QrPlacement = DEFAULT_QR_PLACEMENT,
) -> None:
    padding = max(2, qr.width // 12)
    width, height = qr.width + padding * 2, qr.height + padding * 2
    left = (
        placement.inset_x
        if placement.position.endswith("left")
        else image.width - width - placement.inset_x
    )
    top = (
        placement.inset_y
        if placement.position.startswith("top")
        else image.height - height - placement.inset_y
    )
    left = min(max(0, left), image.width - width)
    top = min(max(0, top), image.height - height)
    sample = image.crop((left, top, left + width, top + height)).convert("L")
    luminance = ImageStat.Stat(sample).mean[0]
    dark_background = luminance < 145
    opacity = placement.opacity / 100
    background_alpha = round((155 if dark_background else 175) * opacity)
    background = (
        (0, 0, 0, background_alpha) if dark_background else (255, 255, 255, background_alpha)
    )

    badge = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(badge)
    draw.rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=max(4, padding * 2), fill=background
    )
    qr_surface = Image.new("RGBA", qr.size, (255, 255, 255, 255))
    badge.paste(qr_surface, (padding, padding))
    module_mask = ImageOps.invert(qr.convert("L"))
    modules = Image.new("RGBA", qr.size, (0, 0, 0, 255))
    badge.paste(modules, (padding, padding), module_mask)
    image.paste(badge, (left, top), badge)


def _make_qr(url: str, size: float = 1, quiet_zone: int = 4) -> Image.Image:
    render_scale = 8
    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=render_scale,
        border=quiet_zone,
    )
    code.add_data(url)
    code.make(fit=True)
    image = cast(
        Image.Image, code.make_image(fill_color="black", back_color="white").convert("RGB")
    )
    target = max(1, round(image.width * size / render_scale))
    return image.resize((target, target), Image.Resampling.NEAREST)
