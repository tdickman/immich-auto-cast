from __future__ import annotations

import asyncio
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

import aiohttp

from .config import ImmichSettings


class ImmichFailureKind(StrEnum):
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    AUTHORIZATION = "authorization"
    REQUEST_REJECTED = "request_rejected"
    INCOMPATIBLE_RESPONSE = "incompatible_response"
    ASSET_UNAVAILABLE = "asset_unavailable"


class ImmichError(RuntimeError):
    """Base error for Immich operations."""

    def __init__(self, message: str, kind: ImmichFailureKind = ImmichFailureKind.UNAVAILABLE):
        super().__init__(message)
        self.kind = kind


class PermanentImmichError(ImmichError):
    """Authentication, permission, or API compatibility is invalid."""

    def __init__(
        self,
        message: str,
        kind: ImmichFailureKind = ImmichFailureKind.INCOMPATIBLE_RESPONSE,
    ) -> None:
        super().__init__(message, kind)


class TransientImmichError(ImmichError):
    """Immich is temporarily unavailable."""


class AssetUnavailable(ImmichError):
    """No usable asset or preview is currently available."""

    def __init__(self, message: str) -> None:
        super().__init__(message, ImmichFailureKind.ASSET_UNAVAILABLE)


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class Asset:
    id: UUID
    location: str | None = None
    date: str | None = None
    media_type: MediaType = MediaType.IMAGE
    duration: float | None = None


class SourceKind(StrEnum):
    TIMELINE = "timeline"
    ALBUM = "album"
    PERSON = "person"
    SEARCH = "search"
    EVENT = "event"
    FILTER = "filter"
    VIDEO = "video"


class EventCollection(StrEnum):
    ON_THIS_DAY = "on_this_day"
    RECENT_FAVORITES = "recent_favorites"
    LAST_MONTH = "last_month"
    SEASONAL = "seasonal"
    RECENT_PERSON_RECAP = "recent_person_recap"


@dataclass(frozen=True, slots=True)
class PhotoSource:
    kind: SourceKind = SourceKind.TIMELINE
    id: UUID | None = None
    query: str | None = None
    collection: EventCollection | None = None
    start_date: date | None = None
    end_date: date | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    max_video_duration: float | None = None


@dataclass(frozen=True, slots=True)
class Album:
    id: UUID
    name: str
    asset_count: int


@dataclass(frozen=True, slots=True)
class Person:
    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class Preview:
    body: bytes
    content_type: str


class ImmichClient:
    def __init__(
        self,
        settings: ImmichSettings,
        session: aiohttp.ClientSession | None = None,
        *,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._settings = settings
        self._session = session
        self._owns_session = session is None
        self._metadata: dict[UUID, tuple[str | None, str | None]] = {}
        self._today = today
        self._random_pools: dict[tuple[PhotoSource, date | None], deque[Asset]] = {}
        self._random_pool_locks: dict[tuple[PhotoSource, date | None], asyncio.Lock] = {}
        self._recyclable_assets: dict[tuple[PhotoSource, date | None], dict[UUID, Asset]] = {}

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
        source = PhotoSource(SourceKind.ALBUM, album_id) if album_id else PhotoSource()
        return await self.select_assets_for(recent, batch_size, count, source)

    async def select_assets_for(
        self,
        recent: set[UUID],
        batch_size: int,
        count: int,
        source: PhotoSource,
    ) -> tuple[Asset, ...]:
        video_only = source.kind is SourceKind.VIDEO
        payload: dict[str, Any] = {
            "type": "VIDEO" if video_only else "IMAGE",
            "withDeleted": False,
            "isOffline": False,
            "withExif": True,
            "size": min(max(batch_size, count, 1), 1000),
        }
        if source.kind in {SourceKind.TIMELINE, SourceKind.VIDEO}:
            payload["visibility"] = "timeline"
        elif source.kind is SourceKind.ALBUM and source.id is not None:
            payload["albumIds"] = [str(source.id)]
        elif source.kind is SourceKind.PERSON and source.id is not None:
            payload["personIds"] = [str(source.id)]
        elif source.kind is SourceKind.SEARCH and source.query:
            payload["query"] = source.query
            payload["page"] = 1
        elif source.kind is SourceKind.EVENT and source.collection is not None:
            self._apply_event_filter(payload, source)
        elif source.kind is SourceKind.FILTER:
            self._apply_custom_filter(payload, source)
        else:
            raise ValueError("invalid media source")
        path = "/api/search/smart" if source.kind is SourceKind.SEARCH else "/api/search/random"
        if path == "/api/search/random":
            return await self._select_from_random_pool(recent, batch_size, count, source, payload)
        candidates = await self._request_candidates(path, payload, source)
        selected = [asset for asset in candidates if asset.id not in recent]
        if not selected:
            raise AssetUnavailable("Immich returned no eligible new media")
        random.SystemRandom().shuffle(selected)
        return tuple(selected[:count])

    async def _select_from_random_pool(
        self,
        recent: set[UUID],
        batch_size: int,
        count: int,
        source: PhotoSource,
        payload: dict[str, Any],
    ) -> tuple[Asset, ...]:
        key = (source, self._today() if source.kind is SourceKind.EVENT else None)
        lock = self._random_pool_locks.setdefault(key, asyncio.Lock())
        async with lock:
            pool = self._random_pools.setdefault(key, deque())
            recyclable = (
                source.kind is SourceKind.EVENT and source.collection is EventCollection.ON_THIS_DAY
            )
            known = self._recyclable_assets.setdefault(key, {}) if recyclable else None
            low_water = max(1, min(max(batch_size, count, 1), 1000) // 5)
            fetched = False
            if len(pool) <= low_water and (known is None or not known):
                await self._refill_random_pool(pool, known, source, payload)
                fetched = True

            selected = self._consume_pool(pool, recent, count)
            if len(selected) < count and known is None:
                if not fetched:
                    await self._refill_random_pool(pool, None, source, payload)
                remaining = count - len(selected)
                excluded = recent | {asset.id for asset in selected}
                selected.extend(self._consume_pool(pool, excluded, remaining))
            if known is not None and len(selected) < count:
                cycle = list(known.values())
                random.SystemRandom().shuffle(cycle)
                if len(cycle) > 1 and selected and cycle[0].id == selected[-1].id:
                    cycle.append(cycle.pop(0))
                while cycle and len(selected) < count:
                    selected.extend(cycle[: count - len(selected)])
            if not selected:
                raise AssetUnavailable("Immich returned no eligible new media")
            return tuple(selected[:count])

    async def _refill_random_pool(
        self,
        pool: deque[Asset],
        known: dict[UUID, Asset] | None,
        source: PhotoSource,
        payload: dict[str, Any],
    ) -> None:
        candidates = await self._request_candidates("/api/search/random", payload, source)
        existing = {asset.id for asset in pool}
        for asset in candidates:
            if asset.id not in existing:
                pool.append(asset)
                existing.add(asset.id)
            if known is not None:
                known[asset.id] = asset

    @staticmethod
    def _consume_pool(pool: deque[Asset], recent: set[UUID], count: int) -> list[Asset]:
        selected: list[Asset] = []
        for _ in range(len(pool)):
            asset = pool.popleft()
            if asset.id in recent:
                pool.append(asset)
            else:
                selected.append(asset)
                if len(selected) == count:
                    break
        return selected

    async def _request_candidates(
        self, path: str, payload: dict[str, Any], source: PhotoSource
    ) -> list[Asset]:
        data = await self._json_request("POST", path, json=payload)
        values = data.get("assets", {}).get("items") if isinstance(data, dict) else data
        if not isinstance(values, list):
            raise PermanentImmichError("Immich random search returned an incompatible response")

        candidates: dict[UUID, Asset] = {}
        for item in values:
            if not self._matches_event(item, source):
                continue
            asset = self._parse_eligible_asset(
                item,
                require_timeline=source.kind in {SourceKind.TIMELINE, SourceKind.VIDEO},
                media_type=MediaType.VIDEO if source.kind is SourceKind.VIDEO else MediaType.IMAGE,
                max_video_duration=source.max_video_duration,
            )
            if asset is not None:
                candidates[asset.id] = asset
                self._metadata[asset.id] = (asset.location, asset.date)
        selected = list(candidates.values())
        random.SystemRandom().shuffle(selected)
        return selected

    def discard_asset(self, source: PhotoSource, asset_id: UUID) -> None:
        for key, pool in self._random_pools.items():
            if key[0] != source:
                continue
            retained = [asset for asset in pool if asset.id != asset_id]
            pool.clear()
            pool.extend(retained)
        for key, known in self._recyclable_assets.items():
            if key[0] == source:
                known.pop(asset_id, None)

    def _apply_event_filter(self, payload: dict[str, Any], source: PhotoSource) -> None:
        today = self._today()
        collection = source.collection
        if collection is EventCollection.RECENT_FAVORITES:
            payload["isFavorite"] = True
            payload["takenAfter"] = self._date_time(today - timedelta(days=90))
        elif collection is EventCollection.LAST_MONTH:
            this_month = today.replace(day=1)
            last_month = (this_month - timedelta(days=1)).replace(day=1)
            payload["takenAfter"] = self._date_time(last_month)
            payload["takenBefore"] = self._date_time(this_month)
        elif collection is EventCollection.RECENT_PERSON_RECAP and source.id is not None:
            payload["personIds"] = [str(source.id)]
            payload["takenAfter"] = self._date_time(today - timedelta(days=365))
        elif collection in {EventCollection.ON_THIS_DAY, EventCollection.SEASONAL}:
            payload["takenBefore"] = self._date_time(today.replace(month=1, day=1))
            payload["size"] = 1000
        else:
            raise ValueError("invalid event collection")

    @classmethod
    def _apply_custom_filter(cls, payload: dict[str, Any], source: PhotoSource) -> None:
        if source.start_date is not None:
            payload["takenAfter"] = cls._date_time(source.start_date)
        if source.end_date is not None:
            payload["takenBefore"] = cls._date_time(source.end_date + timedelta(days=1))
        for field in ("city", "state", "country"):
            value = getattr(source, field)
            if value:
                payload[field] = value
        if not any(
            value is not None
            for value in (
                source.start_date,
                source.end_date,
                source.city,
                source.state,
                source.country,
            )
        ):
            raise ValueError("invalid custom photo filter")

    def _matches_event(self, value: object, source: PhotoSource) -> bool:
        if source.kind is not SourceKind.EVENT or source.collection not in {
            EventCollection.ON_THIS_DAY,
            EventCollection.SEASONAL,
        }:
            return True
        captured = self._asset_datetime(value)
        if captured is None:
            return False
        today = self._today()
        if source.collection is EventCollection.ON_THIS_DAY:
            return (captured.month, captured.day) == (today.month, today.day)
        return captured.month in self._season_months(today.month)

    @staticmethod
    def _date_time(value: date) -> str:
        return datetime.combine(value, datetime.min.time(), UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _season_months(month: int) -> frozenset[int]:
        if month in {12, 1, 2}:
            return frozenset({12, 1, 2})
        if month in {3, 4, 5}:
            return frozenset({3, 4, 5})
        if month in {6, 7, 8}:
            return frozenset({6, 7, 8})
        return frozenset({9, 10, 11})

    @staticmethod
    def _asset_datetime(value: object) -> datetime | None:
        if not isinstance(value, dict):
            return None
        exif = value.get("exifInfo")
        candidates = (
            value.get("localDateTime"),
            exif.get("dateTimeOriginal") if isinstance(exif, dict) else None,
            value.get("fileCreatedAt"),
        )
        for candidate in candidates:
            if isinstance(candidate, str):
                try:
                    return datetime.fromisoformat(candidate.strip().replace("Z", "+00:00"))
                except ValueError:
                    pass
        return None

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

    async def list_people(self) -> tuple[Person, ...]:
        people: dict[UUID, Person] = {}
        page = 1
        while True:
            data = await self._json_request(
                "GET", "/api/people", params={"page": page, "size": 100, "withHidden": "false"}
            )
            if not isinstance(data, dict) or not isinstance(data.get("people"), list):
                raise PermanentImmichError("Immich people returned an incompatible response")
            for value in data["people"]:
                if not isinstance(value, dict) or value.get("isHidden") is True:
                    continue
                try:
                    person_id = UUID(str(value["id"]))
                except (KeyError, ValueError):
                    continue
                name = str(value.get("name", "")).strip() or "Unnamed person"
                people[person_id] = Person(person_id, name)
            if data.get("hasNextPage") is not True:
                break
            page += 1
        return tuple(sorted(people.values(), key=lambda person: person.name.casefold()))

    async def fetch_location(self, asset_id: UUID) -> str | None:
        return (await self.fetch_metadata(asset_id))[0]

    async def fetch_metadata(self, asset_id: UUID) -> tuple[str | None, str | None]:
        if asset_id in self._metadata:
            return self._metadata[asset_id]
        data = await self._json_request("GET", f"/api/assets/{asset_id}")
        location = self._parse_location(data)
        date = self._parse_date(data)
        self._metadata[asset_id] = (location, date)
        return location, date

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

    async def open_video(
        self, asset_id: UUID, method: str, range_header: str | None
    ) -> aiohttp.ClientResponse:
        headers = {"Range": range_header} if range_header is not None else None
        response = await self._request(
            method,
            f"/api/assets/{asset_id}/video/playback",
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=None,
                sock_connect=self._settings.request_timeout,
                sock_read=self._settings.request_timeout,
            ),
        )
        if response.status == 404:
            response.release()
            raise AssetUnavailable("asset video is no longer available")
        if response.status not in {200, 206, 416}:
            try:
                self._raise_for_status(response.status)
            except Exception:
                response.release()
                raise
        return response

    async def _json_request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        async with response:
            self._raise_for_status(response.status)
            try:
                return await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                raise PermanentImmichError(
                    "Immich returned malformed JSON", ImmichFailureKind.INCOMPATIBLE_RESPONSE
                ) from None

    async def _request(self, method: str, path: str, **kwargs: Any) -> aiohttp.ClientResponse:
        session = self._require_session()
        url = f"{self._settings.url}{path}"
        last_error: BaseException | None = None
        for attempt in range(self._settings.retry_attempts):
            retry_after: str | None = None
            try:
                response = await session.request(method, url, allow_redirects=False, **kwargs)
                if response.status != 408 and response.status != 429 and response.status < 500:
                    return response
                last_error = TransientImmichError(
                    f"Immich temporarily unavailable ({response.status})",
                    ImmichFailureKind.RATE_LIMITED
                    if response.status == 429
                    else ImmichFailureKind.UNAVAILABLE,
                )
                retry_after = response.headers.get("Retry-After")
                response.release()
            except (aiohttp.ClientError, TimeoutError) as error:
                last_error = error
            if attempt + 1 < self._settings.retry_attempts:
                delay = min(0.25 * (2**attempt) + random.random() * 0.1, 2.0)
                if retry_after is not None:
                    try:
                        delay = min(max(float(retry_after), 0.0), 5.0)
                    except ValueError:
                        pass
                await asyncio.sleep(delay)
        kind = (
            last_error.kind
            if isinstance(last_error, ImmichError)
            else ImmichFailureKind.UNAVAILABLE
        )
        raise TransientImmichError(
            "Immich request failed after bounded retries", kind
        ) from last_error

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("ImmichClient.start() has not been called")
        return self._session

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if status in {401, 403}:
            raise PermanentImmichError(
                "Immich API key is invalid or lacks required permissions",
                ImmichFailureKind.AUTHORIZATION,
            )
        if 400 <= status < 500 and status not in {408, 429}:
            raise PermanentImmichError(
                f"Immich rejected the request ({status})", ImmichFailureKind.REQUEST_REJECTED
            )
        if status < 200 or status >= 300:
            raise TransientImmichError(f"Immich request failed ({status})")

    @staticmethod
    def _parse_eligible_asset(
        value: object,
        *,
        require_timeline: bool = True,
        media_type: MediaType = MediaType.IMAGE,
        max_video_duration: float | None = None,
    ) -> Asset | None:
        if not isinstance(value, dict):
            return None
        if (
            value.get("type") != media_type.value.upper()
            or (require_timeline and value.get("visibility") != "timeline")
            or value.get("isArchived") is not False
            or value.get("isTrashed") is not False
            or value.get("isOffline") is not False
        ):
            return None
        duration: float | None = None
        if media_type is MediaType.VIDEO:
            duration = ImmichClient._parse_duration(value.get("duration"))
            if duration is None or max_video_duration is None or duration > max_video_duration:
                return None
        try:
            return Asset(
                UUID(str(value["id"])),
                ImmichClient._parse_location(value),
                ImmichClient._parse_date(value),
                media_type,
                duration,
            )
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _parse_duration(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return float(value) if value > 0 else None
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        try:
            if ":" not in text:
                parsed = float(text)
            else:
                parts = [float(part) for part in text.split(":")]
                if len(parts) not in {2, 3}:
                    return None
                parsed = sum(part * (60**index) for index, part in enumerate(reversed(parts)))
        except ValueError:
            return None
        return parsed if parsed > 0 else None

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

    @staticmethod
    def _parse_date(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        exif = value.get("exifInfo")
        candidates = [
            value.get("localDateTime"),
            exif.get("dateTimeOriginal") if isinstance(exif, dict) else None,
            value.get("fileCreatedAt"),
        ]
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            try:
                parsed = datetime.fromisoformat(candidate.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            return f"{parsed:%B} {parsed.day}, {parsed:%Y}"
        return None
