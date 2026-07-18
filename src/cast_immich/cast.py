from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import pychromecast
from pychromecast.controllers.media import MediaStatusListener
from pychromecast.controllers.receiver import CastStatusListener
from pychromecast.discovery import discover_chromecasts as _discover_chromecasts
from pychromecast.socket_client import ConnectionStatusListener

from .config import ChromecastSettings

logger = logging.getLogger(__name__)

DEFAULT_MEDIA_RECEIVER = "CC1AD845"
BACKDROP_RECEIVER = "E8C28D3C"


class EventKind(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECEIVER = "receiver"
    MEDIA = "media"
    LOAD_FAILED = "load_failed"


class DiscoveryError(RuntimeError):
    """Chromecast discovery failed rather than completing with no devices."""


@dataclass(frozen=True, slots=True)
class ReceiverStatus:
    app_id: str | None
    session_id: str | None


@dataclass(frozen=True, slots=True)
class MediaStatus:
    player_state: str
    content_id: str | None
    media_session_id: int | None
    custom_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DiscoveredChromecast:
    friendly_name: str
    uuid: UUID


@dataclass(frozen=True, slots=True)
class CastEvent:
    kind: EventKind
    generation: int
    observed_at: float
    receiver: ReceiverStatus | None = None
    media: MediaStatus | None = None
    detail: str | None = None


class _Listeners(ConnectionStatusListener, CastStatusListener, MediaStatusListener):
    def __init__(self, adapter: CastAdapter, generation: int) -> None:
        self._adapter = adapter
        self._generation = generation

    def new_connection_status(self, status: Any) -> None:
        state = str(status.status).upper()
        kind = EventKind.CONNECTED if state == "CONNECTED" else None
        if state in {"DISCONNECTED", "FAILED", "FAILED_RESOLVE", "LOST"}:
            kind = EventKind.DISCONNECTED
        if kind is not None:
            self._emit(CastEvent(kind, self._generation, time.monotonic()))

    def new_cast_status(self, status: Any) -> None:
        snapshot = ReceiverStatus(status.app_id, status.session_id)
        self._emit(
            CastEvent(
                EventKind.RECEIVER,
                self._generation,
                time.monotonic(),
                receiver=snapshot,
            )
        )

    def new_media_status(self, status: Any) -> None:
        custom_data = status.media_custom_data
        snapshot = MediaStatus(
            str(status.player_state or "UNKNOWN").upper(),
            status.content_id,
            status.media_session_id,
            dict(custom_data) if isinstance(custom_data, dict) else {},
        )
        self._emit(CastEvent(EventKind.MEDIA, self._generation, time.monotonic(), media=snapshot))

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        self._emit(
            CastEvent(
                EventKind.LOAD_FAILED,
                self._generation,
                time.monotonic(),
                detail=f"cast_error_{error_code}",
            )
        )

    def _emit(self, event: CastEvent) -> None:
        if self._adapter._listeners is self:
            self._adapter._emit(event)


async def discover_chromecasts(discovery_timeout: float) -> tuple[DiscoveredChromecast, ...]:
    """Run an isolated, bounded scan without connecting to discovered receivers."""
    worker = asyncio.create_task(
        asyncio.to_thread(_discover_chromecasts, timeout=discovery_timeout)
    )
    browser: Any = None
    try:
        devices, browser = await asyncio.shield(worker)
        discovered = {
            device.uuid: DiscoveredChromecast(
                friendly_name=device.friendly_name or "Unknown Chromecast",
                uuid=device.uuid,
            )
            for device in devices
        }
        return tuple(
            sorted(
                discovered.values(),
                key=lambda device: (device.friendly_name.casefold(), device.uuid),
            )
        )
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            _devices, browser = await worker
        raise
    except Exception as error:
        raise DiscoveryError("Chromecast discovery failed") from error
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(browser.stop_discovery)


class CastAdapter:
    """Owns disposable PyChromecast connections and emits immutable snapshots."""

    def __init__(self, settings: ChromecastSettings, queue: asyncio.Queue[CastEvent]) -> None:
        self._settings = settings
        self._queue = queue
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._cast: Any = None
        self._browser: Any = None
        self._listeners: _Listeners | None = None
        self._generation = 0
        self._stopping = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_event = asyncio.Event()
        self._reconnect_serial = 0
        self._dispose_lock = asyncio.Lock()

    @property
    def generation(self) -> int:
        return self._generation

    async def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._task = asyncio.create_task(self._discovery_loop(), name="chromecast-discovery")

    async def close(self) -> None:
        self._stopping = True
        self._reconnect_event.set()
        reconnect_task, self._reconnect_task = self._reconnect_task, None
        if reconnect_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await reconnect_task
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._dispose_async()

    async def reconnect(self) -> None:
        """Dispose the current connection and coalesce requests for a fresh discovery."""
        if self._stopping or self._task is None:
            return
        task = self._reconnect_task
        if task is None or task.done():
            task = asyncio.create_task(self._force_reconnect(), name="chromecast-reconnect")
            self._reconnect_task = task
        await asyncio.shield(task)

    async def _force_reconnect(self) -> None:
        self._reconnect_serial += 1
        self._reconnect_event.set()
        if self._cast is not None:
            self._emit(CastEvent(EventKind.DISCONNECTED, self._generation, time.monotonic()))
        await self._dispose_async()

    async def load_image(
        self,
        generation: int,
        url: str,
        content_type: str,
        custom_data: dict[str, str],
    ) -> bool:
        if generation != self._generation or self._cast is None or self._stopping:
            return False
        cast = self._cast
        loop = asyncio.get_running_loop()
        response: asyncio.Future[tuple[bool, dict[str, Any] | None]] = loop.create_future()

        def loaded(sent: bool, data: dict[str, Any] | None) -> None:
            def resolve() -> None:
                if not response.done():
                    response.set_result((sent, data))

            loop.call_soon_threadsafe(resolve)

        def load() -> None:
            cast.media_controller.play_media(
                url,
                content_type,
                stream_type="LIVE",
                title="Immich photo",
                media_info={"customData": custom_data},
                callback_function=loaded,
            )

        await asyncio.to_thread(load)
        try:
            sent, data = await asyncio.wait_for(response, self._settings.load_timeout)
        except TimeoutError:
            logger.warning("cast_load_response", extra={"reason": "timeout"})
            return False
        reason = data.get("type", "unknown") if isinstance(data, dict) else "no_response"
        logger.info("cast_load_response", extra={"reason": reason})
        return sent and generation == self._generation and not self._stopping

    async def refresh_status(self, generation: int) -> None:
        if generation != self._generation or self._cast is None or self._stopping:
            return
        cast = self._cast
        await asyncio.to_thread(cast.socket_client.receiver_controller.update_status)
        if cast.media_controller.is_active:
            await asyncio.to_thread(cast.media_controller.update_status)
        else:
            self._emit(
                CastEvent(
                    EventKind.MEDIA,
                    generation,
                    time.monotonic(),
                    media=MediaStatus("UNKNOWN", None, None, {}),
                )
            )

    async def stop_media(self, generation: int) -> bool:
        """Stop media only on the coordinator's current connection generation."""
        if generation != self._generation or self._cast is None or self._stopping:
            return False
        cast = self._cast
        await asyncio.to_thread(cast.media_controller.stop, timeout=self._settings.load_timeout)
        return generation == self._generation and cast is self._cast and not self._stopping

    async def stop_cast(self, generation: int) -> bool:
        """Stop owned media and terminate its receiver app on the current generation."""
        if generation != self._generation or self._cast is None or self._stopping:
            return False
        cast = self._cast
        await asyncio.to_thread(cast.media_controller.stop, timeout=self._settings.load_timeout)
        if generation != self._generation or cast is not self._cast or self._stopping:
            return False
        await asyncio.to_thread(cast.quit_app)
        return generation == self._generation and cast is self._cast and not self._stopping

    async def _discovery_loop(self) -> None:
        delay = 1.0
        while not self._stopping:
            connected_at: float | None = None
            discovery_serial = self._reconnect_serial
            try:
                casts, browser = await self._discover()
                if discovery_serial != self._reconnect_serial:
                    self._browser = browser
                    await self._dispose_async(casts)
                    self._reconnect_event.clear()
                    continue
                self._browser = browser
                if not casts:
                    raise ConnectionError("configured Chromecast was not discovered")
                self._generation += 1
                cast = casts[0]
                listeners = _Listeners(self, self._generation)
                self._cast, self._listeners = cast, listeners
                cast.register_connection_listener(listeners)
                cast.register_status_listener(listeners)
                cast.media_controller.register_status_listener(listeners)
                await asyncio.to_thread(cast.wait, self._settings.discovery_timeout)
                self._emit(CastEvent(EventKind.CONNECTED, self._generation, time.monotonic()))
                await self.refresh_status(self._generation)
                connected_at = time.monotonic()
                while not self._stopping and cast.socket_client.is_connected:
                    try:
                        await asyncio.wait_for(self._reconnect_event.wait(), timeout=1.0)
                        break
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                if self._cast is not None:
                    self._emit(
                        CastEvent(EventKind.DISCONNECTED, self._generation, time.monotonic())
                    )
                await self._dispose_async()
            reconnecting = self._reconnect_event.is_set()
            self._reconnect_event.clear()
            if reconnecting or (connected_at is not None and time.monotonic() - connected_at >= 30):
                delay = 1.0
            else:
                delay = min(delay * 2, 30.0)
            if not self._stopping:
                try:
                    await asyncio.wait_for(self._reconnect_event.wait(), timeout=delay)
                    self._reconnect_event.clear()
                    delay = 1.0
                except TimeoutError:
                    pass

    async def _discover(self) -> tuple[list[Any], Any]:
        worker = asyncio.create_task(
            asyncio.to_thread(
                pychromecast.get_listed_chromecasts,
                uuids=[self._settings.uuid],
                discovery_timeout=self._settings.discovery_timeout,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            casts, browser = await worker
            for cast in casts:
                with contextlib.suppress(Exception):
                    cast.disconnect(timeout=5)
            with contextlib.suppress(Exception):
                browser.stop_discovery()
            raise

    def _dispose(self) -> None:
        cast, browser = self._cast, self._browser
        self._cast = self._browser = self._listeners = None
        if cast is not None:
            with contextlib.suppress(Exception):
                cast.disconnect(timeout=5)
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.stop_discovery()

    async def _dispose_async(self, extra_casts: list[Any] | None = None) -> None:
        async with self._dispose_lock:
            await asyncio.to_thread(self._dispose)
            if extra_casts is not None:
                for cast in extra_casts:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(cast.disconnect, timeout=5)

    def _emit(self, event: CastEvent) -> None:
        if self._loop is not None and not self._stopping:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
