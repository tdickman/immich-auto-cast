from __future__ import annotations

import asyncio
import io
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from aiohttp import web
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import RelaySettings
from .immich import AssetUnavailable, Preview

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_TYPES = {
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
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


@dataclass(frozen=True, slots=True)
class Capability:
    asset_id: UUID
    expires_at: float
    preview: Preview


class ImageRelay:
    def __init__(
        self,
        settings: RelaySettings,
        source: PreviewSource,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_tokens: int = 32,
    ) -> None:
        self._settings = settings
        self._source = source
        self._clock = clock
        self._max_tokens = max_tokens
        self._tokens: OrderedDict[str, Capability] = OrderedDict()
        self._previews: OrderedDict[UUID, Preview] = OrderedDict()
        self._preview_tasks: dict[UUID, asyncio.Task[Preview]] = {}
        self._semaphore = asyncio.Semaphore(settings.max_concurrent)
        self._app = web.Application(client_max_size=1024)
        self._app.router.add_route("*", "/image/{token}", self._handle)
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
        while len(target._tokens) > target._max_tokens:
            target._tokens.popitem(last=False)

    async def preload(self, asset_id: UUID) -> None:
        """Fetch and normalize an image before the receiver needs it."""
        await self._get_preview(asset_id)

    async def mint(self, asset_id: UUID) -> tuple[str, str]:
        if self._closed:
            raise AssetUnavailable("image relay is closed")
        self._purge()
        preview = await self._get_preview(asset_id)
        if self._closed:
            raise AssetUnavailable("image relay is closed")
        token = secrets.token_urlsafe(24)
        self._tokens[token] = Capability(
            asset_id, self._clock() + self._settings.token_lifetime, preview
        )
        while len(self._tokens) > self._max_tokens:
            self._tokens.popitem(last=False)
        return f"{self._settings.advertised_base_url}/image/{token}", preview.content_type

    async def _get_preview(self, asset_id: UUID) -> Preview:
        if self._closed:
            raise AssetUnavailable("image relay is closed")
        preview = self._previews.get(asset_id)
        if preview is not None:
            self._previews.move_to_end(asset_id)
            return preview
        task = self._preview_tasks.get(asset_id)
        if task is None:
            task = asyncio.create_task(self._fetch_preview(asset_id), name="image-preview-fetch")
            self._preview_tasks[asset_id] = task
            self._active_mints.add(task)
        try:
            preview = await asyncio.shield(task)
        finally:
            if task.done():
                self._preview_tasks.pop(asset_id, None)
                self._active_mints.discard(task)
        self._previews[asset_id] = preview
        self._previews.move_to_end(asset_id)
        while len(self._previews) > PREVIEW_CACHE_SIZE:
            self._previews.popitem(last=False)
        return preview

    async def _fetch_preview(self, asset_id: UUID) -> Preview:
        async with self._semaphore:
            preview = await self._source.fetch_preview(asset_id, self._settings.max_response_bytes)
            if preview.content_type not in ALLOWED_IMAGE_TYPES:
                raise AssetUnavailable("asset preview is not a supported image type")
            return await asyncio.to_thread(_normalize_preview, preview)

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

    async def _handle(self, request: web.Request) -> web.Response:
        logger.info("image_requested")
        if request.method not in {"GET", "HEAD"}:
            return web.Response(status=405, headers={"Allow": "GET, HEAD", **SAFE_HEADERS})
        capability = self._tokens.get(request.match_info["token"])
        if capability is None or capability.expires_at <= self._clock():
            return web.Response(status=404, headers=SAFE_HEADERS)
        preview = capability.preview
        if preview.content_type not in ALLOWED_IMAGE_TYPES:
            return web.Response(status=502, headers=SAFE_HEADERS)
        headers = {"Content-Type": preview.content_type, "Content-Length": str(len(preview.body))}
        headers.update(SAFE_HEADERS)
        return web.Response(body=b"" if request.method == "HEAD" else preview.body, headers=headers)

    def _purge(self) -> None:
        now = self._clock()
        expired = [token for token, item in self._tokens.items() if item.expires_at <= now]
        for token in expired:
            self._tokens.pop(token, None)


def _normalize_preview(preview: Preview) -> Preview:
    try:
        with Image.open(io.BytesIO(preview.body)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise AssetUnavailable("asset preview dimensions exceed the safety limit")
            image = ImageOps.exif_transpose(source)
            image.thumbnail(MAX_CAST_SIZE, Image.Resampling.LANCZOS)
            if image.mode != "RGB":
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90, optimize=True)
    except (OSError, UnidentifiedImageError):
        raise AssetUnavailable("asset preview is not a valid image") from None
    return Preview(output.getvalue(), "image/jpeg")
