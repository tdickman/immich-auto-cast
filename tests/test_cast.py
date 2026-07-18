from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from cast_immich.cast import (
    CastAdapter,
    DiscoveredChromecast,
    DiscoveryError,
    EventKind,
    _Listeners,
    discover_chromecasts,
)
from cast_immich.config import ChromecastSettings


class MediaController:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.is_active = True

    def play_media(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        callback = kwargs.get("callback_function")
        if callback is not None:
            callback(True, {"type": "MEDIA_STATUS"})

    def stop(self, **kwargs: Any) -> None:
        self.calls.append((("stop",), kwargs))

    def update_status(self) -> None:
        self.calls.append((("update_status",), {}))


@pytest.mark.asyncio
async def test_load_uses_live_media_and_custom_ownership_data() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 1), queue)
    adapter._loop = asyncio.get_running_loop()
    adapter._generation = 4
    controller = MediaController()
    adapter._cast = SimpleNamespace(media_controller=controller)

    sent = await adapter.load_image(4, "http://lan/image/token", "image/webp", {"loadId": "x"})

    assert sent is True
    args, kwargs = controller.calls[0]
    assert args == ("http://lan/image/token", "image/webp")
    assert kwargs["stream_type"] == "LIVE"
    assert kwargs["media_info"] == {"customData": {"loadId": "x"}}
    assert kwargs["callback_function"] is not None
    assert await adapter.load_image(3, "http://bad", "image/jpeg", {}) is False


@pytest.mark.asyncio
async def test_video_load_is_buffered_and_restores_receiver_mute() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 1), queue)
    adapter._loop = asyncio.get_running_loop()
    adapter._generation = 4
    controller = MediaController()
    mute_calls: list[bool] = []
    adapter._cast = SimpleNamespace(
        media_controller=controller,
        status=SimpleNamespace(volume_muted=False),
        set_volume_muted=mute_calls.append,
    )

    sent = await adapter.load_media(
        4,
        "http://lan/video/token",
        "video/mp4",
        {"loadId": "x"},
        is_video=True,
        duration=12.5,
        muted=True,
    )
    await adapter.release_audio(4)

    assert sent is True
    _args, kwargs = controller.calls[0]
    assert kwargs["stream_type"] == "BUFFERED"
    assert kwargs["media_info"] == {"customData": {"loadId": "x"}, "duration": 12.5}
    assert mute_calls == [True, False]


@pytest.mark.asyncio
async def test_refresh_requests_receiver_and_media_status() -> None:
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 1), asyncio.Queue())
    adapter._generation = 4
    receiver_calls: list[str] = []
    controller = MediaController()
    adapter._cast = SimpleNamespace(
        socket_client=SimpleNamespace(
            receiver_controller=SimpleNamespace(
                update_status=lambda: receiver_calls.append("update_status")
            )
        ),
        media_controller=controller,
    )

    await adapter.refresh_status(4)

    assert receiver_calls == ["update_status"]
    assert controller.calls == [(("update_status",), {})]


@pytest.mark.asyncio
async def test_refresh_does_not_launch_inactive_media_receiver() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 1), queue)
    adapter._loop = asyncio.get_running_loop()
    adapter._generation = 4
    controller = MediaController()
    controller.is_active = False
    adapter._cast = SimpleNamespace(
        socket_client=SimpleNamespace(
            receiver_controller=SimpleNamespace(update_status=lambda: None)
        ),
        media_controller=controller,
    )

    await adapter.refresh_status(4)

    assert controller.calls == []
    event = await asyncio.wait_for(queue.get(), 1)
    assert event.kind is EventKind.MEDIA
    assert event.media.player_state == "UNKNOWN"
    assert event.media.content_id is None
    assert event.media.media_session_id is None


@pytest.mark.asyncio
async def test_listener_normalizes_status_and_tags_generation() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 1), queue)
    adapter._loop = asyncio.get_running_loop()
    listener = _Listeners(adapter, 7)
    adapter._listeners = listener
    listener.new_media_status(
        SimpleNamespace(
            player_state="PLAYING",
            content_id="http://lan/image/token",
            media_session_id=9,
            media_custom_data={"schema": "cast-immich/v1"},
        )
    )
    event = await asyncio.wait_for(queue.get(), 1)
    assert event.kind is EventKind.MEDIA
    assert event.generation == 7
    assert event.media.player_state == "PLAYING"


@pytest.mark.asyncio
async def test_load_failure_is_an_event_not_an_automatic_retry() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 1), queue)
    adapter._loop = asyncio.get_running_loop()
    listener = _Listeners(adapter, 2)
    adapter._listeners = listener
    listener.load_media_failed(5, 104)
    event = await asyncio.wait_for(queue.get(), 1)
    assert event.kind is EventKind.LOAD_FAILED
    assert event.detail == "cast_error_104"


@pytest.mark.asyncio
async def test_cancelled_discovery_cleans_up_late_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    stopped = threading.Event()
    browser = SimpleNamespace(stop_discovery=stopped.set)

    def discover(**_kwargs: Any) -> tuple[list[Any], Any]:
        release.wait(timeout=1)
        return [], browser

    monkeypatch.setattr("cast_immich.cast.pychromecast.get_listed_chromecasts", discover)
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 1), asyncio.Queue())
    task = asyncio.create_task(adapter._discover())
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_broad_discovery_deduplicates_uuids_and_closes_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = threading.Event()
    duplicate_uuid = UUID(int=1)
    devices = [
        SimpleNamespace(uuid=duplicate_uuid, friendly_name="Living Room", cast_type="cast"),
        SimpleNamespace(uuid=duplicate_uuid, friendly_name="Duplicate", cast_type="cast"),
        SimpleNamespace(uuid=UUID(int=2), friendly_name="Living Room", cast_type="cast"),
        SimpleNamespace(uuid=UUID(int=3), friendly_name="Audio", cast_type="audio"),
        SimpleNamespace(uuid=UUID(int=4), friendly_name="Group", cast_type="group"),
    ]
    browser = SimpleNamespace(stop_discovery=stopped.set)
    monkeypatch.setattr(
        "cast_immich.cast._discover_chromecasts",
        lambda **_kwargs: (devices, browser),
    )

    result = await discover_chromecasts(1)

    assert result == (
        DiscoveredChromecast("Duplicate", UUID(int=1)),
        DiscoveredChromecast("Living Room", UUID(int=2)),
    )
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_cancelled_broad_discovery_closes_late_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    stopped = threading.Event()
    browser = SimpleNamespace(stop_discovery=stopped.set)

    def discover(**_kwargs: Any) -> tuple[list[Any], Any]:
        release.wait(timeout=1)
        return [], browser

    monkeypatch.setattr("cast_immich.cast._discover_chromecasts", discover)
    task = asyncio.create_task(discover_chromecasts(1))
    await asyncio.sleep(0)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_broad_discovery_exception_returns_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def discover(**_kwargs: Any) -> tuple[list[Any], Any]:
        raise RuntimeError("discovery failed")

    monkeypatch.setattr("cast_immich.cast._discover_chromecasts", discover)

    with pytest.raises(DiscoveryError):
        await discover_chromecasts(1)


@pytest.mark.asyncio
async def test_broad_discovery_does_not_touch_active_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_cast = SimpleNamespace()
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 1), asyncio.Queue())
    adapter._generation = 5
    adapter._cast = active_cast
    browser = SimpleNamespace(stop_discovery=lambda: None)
    monkeypatch.setattr(
        "cast_immich.cast._discover_chromecasts",
        lambda **_kwargs: ([], browser),
    )

    assert await discover_chromecasts(1) == ()
    assert adapter._cast is active_cast
    assert adapter.generation == 5


@pytest.mark.asyncio
async def test_reconnect_coalesces_disposal_and_suppresses_late_events() -> None:
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 1), asyncio.Queue())
    adapter._loop = asyncio.get_running_loop()
    adapter._task = asyncio.create_task(asyncio.Event().wait())
    adapter._generation = 3
    listener = _Listeners(adapter, 3)
    adapter._listeners = listener
    disconnect_started = threading.Event()
    release = threading.Event()
    disconnect_calls = 0

    def disconnect(**_kwargs: Any) -> None:
        nonlocal disconnect_calls
        disconnect_calls += 1
        disconnect_started.set()
        release.wait(timeout=1)

    adapter._cast = SimpleNamespace(disconnect=disconnect)
    first = asyncio.create_task(adapter.reconnect())
    await asyncio.to_thread(disconnect_started.wait, 1)
    second = asyncio.create_task(adapter.reconnect())
    release.set()
    await asyncio.gather(first, second)
    listener.new_connection_status(SimpleNamespace(status="CONNECTED"))

    assert disconnect_calls == 1
    assert adapter._cast is None
    event = adapter._queue.get_nowait()
    assert event.kind is EventKind.DISCONNECTED
    assert event.generation == 3
    assert adapter._queue.empty()
    adapter._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await adapter._task


@pytest.mark.asyncio
async def test_stop_media_is_generation_scoped() -> None:
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 2), asyncio.Queue())
    adapter._generation = 4
    controller = MediaController()
    adapter._cast = SimpleNamespace(media_controller=controller)

    assert await adapter.stop_media(3) is False
    assert controller.calls == []
    assert await adapter.stop_media(4) is True
    assert controller.calls == [(("stop",), {"timeout": 2})]


@pytest.mark.asyncio
async def test_stop_cast_stops_media_and_quits_receiver_app() -> None:
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 2), asyncio.Queue())
    adapter._generation = 4
    controller = MediaController()
    quit_calls: list[bool] = []
    adapter._cast = SimpleNamespace(
        media_controller=controller,
        quit_app=lambda: quit_calls.append(True),
    )

    assert await adapter.stop_cast(3) is False
    assert await adapter.stop_cast(4) is True
    assert controller.calls == [(("stop",), {"timeout": 2})]
    assert quit_calls == [True]


@pytest.mark.asyncio
async def test_stop_cast_quits_app_when_media_stop_is_unsupported() -> None:
    adapter = CastAdapter(ChromecastSettings(UUID(int=1), 1, 2), asyncio.Queue())
    adapter._generation = 4
    quit_calls: list[bool] = []

    def unsupported_stop(**_kwargs: Any) -> None:
        raise RuntimeError("no media namespace")

    adapter._cast = SimpleNamespace(
        media_controller=SimpleNamespace(stop=unsupported_stop),
        quit_app=lambda: quit_calls.append(True),
    )

    assert await adapter.stop_cast(4) is True
    assert quit_calls == [True]
