from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import aiohttp

from .config import ImmichSettings


class ImmichError(RuntimeError):
    """Base error for Immich operations."""


class PermanentImmichError(ImmichError):
    """Authentication, permission, or API compatibility is invalid."""


class TransientImmichError(ImmichError):
    """Immich is temporarily unavailable."""


class AssetUnavailable(ImmichError):
    """No usable asset or preview is currently available."""


@dataclass(frozen=True, slots=True)
class Asset:
    id: UUID
    location: str | None = None


@dataclass(frozen=True, slots=True)
class Album:
    id: UUID
    name: str
    asset_count: int


@dataclass(frozen=True, slots=True)
class Preview:
    body: bytes
    content_type: str


class ImmichClient:
    def __init__(
        self,
        settings: ImmichSettings,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._settings = settings
        self._session = session
        self._owns_session = session is None
        self._locations: dict[UUID, str | None] = {}

    async def __aenter__(self) -> ImmichClient:
        await self.start()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self._settings.request_timeout)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"x-api-key": self._settings.api_key},
                raise_for_status=False,
            )

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None

    async def select_asset(self, recent: set[UUID], batch_size: int) -> Asset:
        return (await self.select_assets(recent, batch_size, 1))[0]

    async def select_assets(
        self, recent: set[UUID], batch_size: int, count: int
    ) -> tuple[Asset, ...]:
        return await self.select_assets_from(recent, batch_size, count, None)

    async def select_assets_from(
        self,
        recent: set[UUID],
        batch_size: int,
        count: int,
        album_id: UUID | None,
    ) -> tuple[Asset, ...]:
        payload = {
            "type": "IMAGE",
            "withDeleted": False,
            "isOffline": False,
            "withExif": True,
            "size": min(max(batch_size, count, 1), 1000),
        }
        if album_id is None:
            payload["visibility"] = "timeline"
        else:
            payload["albumIds"] = [str(album_id)]
        data = await self._json_request("POST", "/api/search/random", json=payload)
        if not isinstance(data, list):
            raise PermanentImmichError("Immich random search returned an incompatible response")

        candidates: dict[UUID, Asset] = {}
        for item in data:
            asset = self._parse_eligible_asset(item, require_timeline=album_id is None)
            if asset is not None and asset.id not in recent:
                candidates[asset.id] = asset
                self._locations[asset.id] = asset.location
        if not candidates:
            raise AssetUnavailable("Immich returned no eligible new images")
        selected = list(candidates.values())
        random.SystemRandom().shuffle(selected)
        return tuple(selected[:count])

    async def list_albums(self) -> tuple[Album, ...]:
        data = await self._json_request("GET", "/api/albums")
        if not isinstance(data, list):
            raise PermanentImmichError("Immich albums returned an incompatible response")
        albums: list[Album] = []
        for value in data:
            if not isinstance(value, dict):
                continue
            try:
                album_id = UUID(str(value["id"]))
                name = str(value["albumName"]).strip()
                count = int(value.get("assetCount", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if name and count >= 0:
                albums.append(Album(album_id, name, count))
        return tuple(sorted(albums, key=lambda album: album.name.casefold()))

    async def fetch_location(self, asset_id: UUID) -> str | None:
        if asset_id in self._locations:
            return self._locations[asset_id]
        data = await self._json_request("GET", f"/api/assets/{asset_id}")
        location = self._parse_location(data)
        self._locations[asset_id] = location
        return location

    async def validate_access(self) -> None:
        data = await self._json_request(
            "POST",
            "/api/search/random",
            json={
                "type": "IMAGE",
                "visibility": "timeline",
                "withDeleted": False,
                "isOffline": False,
                "withExif": True,
                "size": 1,
            },
        )
        if not isinstance(data, list):
            raise PermanentImmichError("Immich random search returned an incompatible response")

    async def fetch_preview(self, asset_id: UUID, max_bytes: int | None = None) -> Preview:
        self._require_session()
        path = f"/api/assets/{asset_id}/thumbnail"
        response = await self._request("GET", path, params={"size": "preview"})
        async with response:
            if response.status == 404:
                raise AssetUnavailable("asset preview is no longer available")
            self._raise_for_status(response.status)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            limit = max_bytes or 25_000_000
            body = bytearray()
            try:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > limit:
                        raise AssetUnavailable("asset preview exceeds relay size limit")
            except (aiohttp.ClientError, TimeoutError) as error:
                raise TransientImmichError("Immich preview download was interrupted") from error
            if not body:
                raise AssetUnavailable("asset preview is empty")
            return Preview(bytes(body), content_type)

    async def _json_request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        async with response:
            self._raise_for_status(response.status)
            try:
                return await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                raise PermanentImmichError("Immich returned malformed JSON") from None

    async def _request(self, method: str, path: str, **kwargs: Any) -> aiohttp.ClientResponse:
        session = self._require_session()
        url = f"{self._settings.url}{path}"
        last_error: BaseException | None = None
        for attempt in range(self._settings.retry_attempts):
            try:
                response = await session.request(method, url, allow_redirects=False, **kwargs)
                if response.status not in {429, 500, 502, 503, 504}:
                    return response
                last_error = TransientImmichError(
                    f"Immich temporarily unavailable ({response.status})"
                )
                response.release()
            except (aiohttp.ClientError, TimeoutError) as error:
                last_error = error
            if attempt + 1 < self._settings.retry_attempts:
                await asyncio.sleep(min(0.25 * (2**attempt) + random.random() * 0.1, 2.0))
        raise TransientImmichError("Immich request failed after bounded retries") from last_error

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("ImmichClient.start() has not been called")
        return self._session

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if status in {401, 403}:
            raise PermanentImmichError("Immich API key is invalid or lacks required permissions")
        if 400 <= status < 500 and status not in {408, 429}:
            raise PermanentImmichError(f"Immich rejected the request ({status})")
        if status < 200 or status >= 300:
            raise TransientImmichError(f"Immich request failed ({status})")

    @staticmethod
    def _parse_eligible_asset(value: object, *, require_timeline: bool = True) -> Asset | None:
        if not isinstance(value, dict):
            return None
        if (
            value.get("type") != "IMAGE"
            or (require_timeline and value.get("visibility") != "timeline")
            or value.get("isArchived") is not False
            or value.get("isTrashed") is not False
            or value.get("isOffline") is not False
        ):
            return None
        try:
            return Asset(UUID(str(value["id"])), ImmichClient._parse_location(value))
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _parse_location(value: object) -> str | None:
        if not isinstance(value, dict) or not isinstance(value.get("exifInfo"), dict):
            return None
        exif = value["exifInfo"]
        parts = [
            item.strip()
            for item in (exif.get("city"), exif.get("state"))
            if isinstance(item, str) and item.strip()
        ]
        return ", ".join(dict.fromkeys(parts)) or None
