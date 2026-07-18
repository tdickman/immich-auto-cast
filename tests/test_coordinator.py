from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from cast_immich.cast import CastEvent, EventKind, MediaStatus, ReceiverStatus
from cast_immich.config import RotationSettings
from cast_immich.coordinator import Command, CommandResult, Coordinator, State
from cast_immich.history import DisplayRecord, HistoryState
from cast_immich.immich import (
    Asset,
    AssetUnavailable,
    MediaType,
    PermanentImmichError,
    PhotoSource,
    SourceKind,
)

INSTALLATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ASSET_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


class Selector:
    def __init__(self) -> None:
        self.calls = 0
        self.recent: set[UUID] = set()

    async def select_assets(
        self, recent: set[UUID], batch_size: int, count: int
    ) -> tuple[Asset, ...]:
        self.calls += 1
        self.recent = recent
        return (Asset(ASSET_ID),)


class Relay:
    def __init__(self) -> None:
        self.confirmed: list[str] = []
        self.retired: list[str] = []

    async def preload(self, asset_id: UUID) -> None:
        pass

    async def mint(self, asset_id: UUID) -> tuple[str, str]:
        return "http://192.168.1.5:8787/image/opaque", "image/webp"

    def confirm(self, url: str) -> None:
        self.confirmed.append(url)

    def retire(self, url: str) -> None:
        self.retired.append(url)


class Cast:
    def __init__(self) -> None:
        self.loads: list[tuple[int, str, str, dict[str, str]]] = []
        self.refreshes: list[int] = []
        self.stops: list[int] = []
        self.cast_stops: list[int] = []

    async def load_image(
        self, generation: int, url: str, content_type: str, custom_data: dict[str, str]
    ) -> bool:
        self.loads.append((generation, url, content_type, custom_data))
        return True

    async def refresh_status(self, generation: int) -> None:
        self.refreshes.append(generation)

    async def stop_media(self, generation: int) -> bool:
        self.stops.append(generation)
        return True

    async def stop_cast(self, generation: int) -> bool:
        self.stops.append(generation)
        self.cast_stops.append(generation)
        return True


class History:
    def __init__(
        self, *, enabled: bool = True, autocast: bool = True, fail_records: bool = False
    ) -> None:
        self.enabled = enabled
        self.autocast = autocast
        self.fail_records = fail_records
        self.records: list[DisplayRecord] = []
        self.source = PhotoSource()
        self.recent: tuple[str, ...] = ()

    def load(self) -> HistoryState:
        return HistoryState(
            rotation_enabled=self.enabled,
            records=tuple(self.records),
            autocast_enabled=self.autocast,
            source_kind=self.source.kind.value,
            source_id=str(self.source.id) if self.source.id is not None else None,
            source_query=self.source.query,
            recent_asset_ids=self.recent,
        )

    def set_rotation_enabled(self, enabled: bool) -> HistoryState:
        self.enabled = enabled
        return self.load()

    def set_autocast_enabled(self, enabled: bool) -> HistoryState:
        self.autocast = enabled
        return self.load()

    def set_source(
        self, kind: str, source_id: str | None = None, query: str | None = None
    ) -> HistoryState:
        self.source = PhotoSource(
            SourceKind(kind), UUID(source_id) if source_id is not None else None, query
        )
        return self.load()

    def record_display(self, load_id: str, asset_id: str) -> DisplayRecord:
        if self.fail_records:
            raise OSError("disk unavailable")
        record = DisplayRecord("event", load_id, asset_id, datetime.now(UTC))
        self.records.append(record)
        return record


class CorruptHistory(History):
    def load(self) -> HistoryState:
        raise OSError("corrupt state")


def make_coordinator(
    history: History | None = None,
    *,
    output_id: str | None = None,
) -> tuple[Coordinator, asyncio.Queue[Any], Selector, Cast]:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    selector = Selector()
    cast = Cast()
    settings = RotationSettings(0.02, 0.01, 0.02, 5, 10)
    coordinator = Coordinator(
        queue,
        selector,
        Relay(),
        cast,
        settings,
        INSTALLATION_ID,
        0.02,
        history=history,
        output_id=output_id,
    )
    return coordinator, queue, selector, cast


@pytest.mark.asyncio
async def test_qr_placement_is_forwarded_to_media_relay() -> None:
    observed: dict[str, object] = {}

    class PlacementRelay(Relay):
        async def mint_media(self, asset: Asset, **options: object) -> tuple[str, str]:
            observed.update(options)
            return await self.mint(asset.id)

    settings = RotationSettings(
        60,
        3,
        15,
        25,
        50,
        show_web_qr=True,
        web_qr_size=3,
        web_qr_position="top-right",
        web_qr_inset_x=72,
        web_qr_inset_y=54,
        web_qr_opacity=60,
        web_qr_lossless=True,
        web_qr_quiet_zone=0,
    )
    coordinator = Coordinator(
        asyncio.Queue(), Selector(), PlacementRelay(), Cast(), settings, INSTALLATION_ID, 15
    )

    await coordinator._mint(Asset(ASSET_ID))

    assert observed == {
        "show_web_qr": True,
        "web_qr_size": 3,
        "web_qr_position": "top-right",
        "web_qr_inset_x": 72,
        "web_qr_inset_y": 54,
        "web_qr_opacity": 60,
        "web_qr_lossless": True,
        "web_qr_quiet_zone": 0,
    }


def event(
    kind: EventKind,
    *,
    generation: int = 1,
    receiver: ReceiverStatus | None = None,
    media: MediaStatus | None = None,
) -> CastEvent:
    return CastEvent(kind, generation, time.monotonic(), receiver=receiver, media=media)


async def observe_idle(coordinator: Coordinator) -> None:
    await coordinator.handle(event(EventKind.CONNECTED))
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus(app_id=None, session_id=None))
    )
    await coordinator.handle(
        event(
            EventKind.MEDIA,
            media=MediaStatus("IDLE", None, None, {}),
        )
    )


async def drain_one(queue: asyncio.Queue[Any], coordinator: Coordinator) -> None:
    await coordinator.handle(await asyncio.wait_for(queue.get(), 1))


async def send_idle_snapshot(coordinator: Coordinator) -> None:
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus(app_id=None, session_id=None))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("IDLE", None, None, {})))


async def drive_idle_to_load(coordinator: Coordinator, queue: asyncio.Queue[Any]) -> None:
    await observe_idle(coordinator)
    await drain_one(queue, coordinator)  # debounce requests fresh status
    await send_idle_snapshot(coordinator)  # starts background preview preparation
    await drain_one(queue, coordinator)  # prepared preview requests final fresh status
    await send_idle_snapshot(coordinator)  # final check sends LOAD


async def confirm_load(coordinator: Coordinator, cast: Cast) -> tuple[str, dict[str, str]]:
    _generation, url, _mime, metadata = cast.loads[-1]
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))
    return url, metadata


async def wait_until(predicate: Any) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise TimeoutError("condition was not met")


@pytest.mark.asyncio
async def test_stable_idle_sends_exactly_one_load_and_confirms_ownership() -> None:
    coordinator, queue, selector, cast = make_coordinator()
    await drive_idle_to_load(coordinator, queue)

    assert len(cast.loads) == 1
    assert selector.calls == 1
    assert coordinator.state is State.LOAD_PENDING
    generation, url, mime, metadata = cast.loads[0]
    assert generation == 1
    assert mime == "image/webp"
    assert metadata["installationId"] == str(INSTALLATION_ID)

    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(
        event(
            EventKind.MEDIA,
            media=MediaStatus("PLAYING", url, 3, metadata),
        )
    )
    assert coordinator.snapshot.state is State.OWNED
    assert coordinator.snapshot.rotation_deadline is not None
    assert 0 < coordinator.snapshot.rotation_deadline - time.monotonic() <= 0.02
    assert len(cast.loads) == 1
    await coordinator.close()


@pytest.mark.asyncio
async def test_video_uses_media_load_and_duration_based_rotation() -> None:
    video = Asset(ASSET_ID, media_type=MediaType.VIDEO, duration=0.05)

    class VideoSelector(Selector):
        async def select_assets_for(
            self, recent: set[UUID], batch_size: int, count: int, source: PhotoSource
        ) -> tuple[Asset, ...]:
            assert source.kind is SourceKind.VIDEO
            assert source.max_video_duration == 0.1
            return (video,)

    class VideoRelay(Relay):
        async def mint_media(self, asset: Asset) -> tuple[str, str]:
            assert asset is video
            return "http://192.168.1.5:8787/video/opaque", "video/mp4"

    class VideoCast(Cast):
        def __init__(self) -> None:
            super().__init__()
            self.media_load: tuple[bool, float | None, bool] | None = None

        async def load_media(
            self,
            generation: int,
            url: str,
            content_type: str,
            custom_data: dict[str, str],
            *,
            is_video: bool,
            duration: float | None,
            muted: bool,
        ) -> bool:
            self.loads.append((generation, url, content_type, custom_data))
            self.media_load = (is_video, duration, muted)
            return True

    queue: asyncio.Queue[Any] = asyncio.Queue()
    cast = VideoCast()
    coordinator = Coordinator(
        queue,
        VideoSelector(),
        VideoRelay(),
        cast,
        RotationSettings(0.01, 0.01, 0.02, 5, 10, video_max_duration=0.1),
        INSTALLATION_ID,
        0.02,
    )
    coordinator._source = PhotoSource(SourceKind.VIDEO, max_video_duration=0.1)

    await drive_idle_to_load(coordinator, queue)
    _url, metadata = await confirm_load(coordinator, cast)

    assert cast.media_load == (True, 0.05, True)
    assert metadata["mediaType"] == "video"
    assert metadata["duration"] == "0.05"
    assert coordinator.snapshot.rotation_deadline is not None
    assert coordinator.snapshot.rotation_deadline - time.monotonic() > 0.9
    await coordinator.close()


@pytest.mark.asyncio
async def test_output_ownership_does_not_adopt_another_outputs_session() -> None:
    coordinator, queue, _selector, cast = make_coordinator(output_id="living-room")
    await drive_idle_to_load(coordinator, queue)
    _generation, url, _mime, metadata = cast.loads[0]

    assert metadata["outputId"] == "living-room"
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(
        event(
            EventKind.MEDIA,
            media=MediaStatus("PLAYING", url, 3, dict(metadata, outputId="office")),
        )
    )

    assert coordinator.snapshot.state is State.PROTECTED
    await coordinator.close()


@pytest.mark.asyncio
async def test_disabled_autocast_does_not_load_and_can_be_enabled() -> None:
    history = History(enabled=False, autocast=False)
    coordinator, queue, selector, cast = make_coordinator(history)

    await observe_idle(coordinator)
    await asyncio.sleep(0.01)

    assert queue.empty()
    assert selector.calls == 0
    assert cast.loads == []
    assert coordinator.snapshot.autocast_enabled is False

    enabling = asyncio.create_task(coordinator.command(Command.AUTOCAST_ENABLE, "enable-autocast"))
    await drain_one(queue, coordinator)

    assert await enabling is CommandResult.APPLIED
    assert history.autocast is True
    assert history.enabled is True
    assert coordinator.snapshot.autocast_enabled is True
    assert coordinator.snapshot.rotation_enabled is True

    await send_idle_snapshot(coordinator)
    await drain_one(queue, coordinator)

    assert selector.calls == 1
    await coordinator.close()


@pytest.mark.asyncio
async def test_disabling_autocast_stops_owned_photo_after_fresh_check() -> None:
    history = History()
    coordinator, queue, _selector, cast = make_coordinator(history)
    await drive_idle_to_load(coordinator, queue)
    url, metadata = await confirm_load(coordinator, cast)

    disabling = asyncio.create_task(
        coordinator.command(Command.AUTOCAST_DISABLE, "disable-autocast")
    )
    await drain_one(queue, coordinator)
    assert not disabling.done()

    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))

    assert await disabling is CommandResult.APPLIED
    assert cast.stops == [1]
    assert cast.cast_stops == [1]
    assert history.autocast is False
    assert coordinator.snapshot.autocast_enabled is False
    await coordinator.close()


@pytest.mark.asyncio
async def test_preparation_publishes_the_next_ten_assets_in_cast_order() -> None:
    assets = tuple(Asset(UUID(int=value)) for value in range(1, 12))

    class QueueSelector:
        async def select_assets(
            self, recent: set[UUID], batch_size: int, count: int
        ) -> tuple[Asset, ...]:
            assert count == 11
            return assets

    class QueueRelay:
        async def preload(self, asset_id: UUID) -> None:
            pass

        async def mint(self, asset_id: UUID) -> tuple[str, str]:
            return f"http://192.168.1.5:8787/image/{asset_id}", "image/jpeg"

    queue: asyncio.Queue[Any] = asyncio.Queue()
    cast = Cast()
    coordinator = Coordinator(
        queue,
        QueueSelector(),
        QueueRelay(),
        cast,
        RotationSettings(0.02, 0.01, 0.02, 5, 10),
        INSTALLATION_ID,
        0.02,
    )

    await drive_idle_to_load(coordinator, queue)

    assert cast.loads[0][3]["assetId"] == str(assets[0].id)
    assert coordinator.snapshot.upcoming_assets == tuple(asset.id for asset in assets[1:])
    await coordinator.close()


@pytest.mark.asyncio
async def test_preparation_discards_an_unavailable_candidate_and_continues() -> None:
    assets = tuple(Asset(UUID(int=value)) for value in range(1, 12))

    class QueueSelector:
        def __init__(self) -> None:
            self.discarded: list[UUID] = []

        async def select_assets(
            self, recent: set[UUID], batch_size: int, count: int
        ) -> tuple[Asset, ...]:
            return assets

        def discard_asset(self, source: PhotoSource, asset_id: UUID) -> None:
            self.discarded.append(asset_id)

    class QueueRelay:
        async def preload(self, asset_id: UUID) -> None:
            pass

        async def mint(self, asset_id: UUID) -> tuple[str, str]:
            if asset_id == assets[0].id:
                raise AssetUnavailable("preview disappeared")
            return f"http://192.168.1.5:8787/image/{asset_id}", "image/jpeg"

    queue: asyncio.Queue[Any] = asyncio.Queue()
    selector = QueueSelector()
    cast = Cast()
    coordinator = Coordinator(
        queue,
        selector,
        QueueRelay(),
        cast,
        RotationSettings(0.02, 0.01, 0.02, 5, 10),
        INSTALLATION_ID,
        0.02,
    )

    await drive_idle_to_load(coordinator, queue)

    assert selector.discarded == [assets[0].id]
    assert cast.loads[0][3]["assetId"] == str(assets[1].id)
    assert assets[0].id not in coordinator.snapshot.upcoming_assets
    await coordinator.close()


@pytest.mark.asyncio
async def test_album_source_is_applied_to_the_next_selection() -> None:
    album_id = UUID(int=99)

    class SourceSelector(Selector):
        def __init__(self) -> None:
            super().__init__()
            self.sources: list[UUID | None] = []

        async def select_assets_from(
            self,
            recent: set[UUID],
            batch_size: int,
            count: int,
            selected_album: UUID | None,
        ) -> tuple[Asset, ...]:
            self.sources.append(selected_album)
            return await self.select_assets(recent, batch_size, count)

    queue: asyncio.Queue[Any] = asyncio.Queue()
    selector, cast = SourceSelector(), Cast()
    coordinator = Coordinator(
        queue,
        selector,
        Relay(),
        cast,
        RotationSettings(0.02, 0.01, 0.02, 5, 10),
        INSTALLATION_ID,
        0.02,
    )
    source_change = asyncio.create_task(coordinator.select_source(album_id))
    await drain_one(queue, coordinator)
    assert await source_change is True

    await drive_idle_to_load(coordinator, queue)

    assert selector.sources == [album_id]
    assert coordinator.snapshot.selected_album == album_id
    await coordinator.close()


@pytest.mark.asyncio
async def test_source_change_immediately_replaces_owned_photo() -> None:
    album_id = UUID(int=99)
    coordinator, queue, selector, cast = make_coordinator()
    await drive_idle_to_load(coordinator, queue)
    old_url, old_metadata = await confirm_load(coordinator, cast)

    source_change = asyncio.create_task(coordinator.select_source(album_id))
    await drain_one(queue, coordinator)
    assert await source_change is True
    await drain_one(queue, coordinator)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(
        event(EventKind.MEDIA, media=MediaStatus("PLAYING", old_url, 3, old_metadata))
    )

    assert selector.calls == 2
    assert len(cast.loads) == 2
    assert coordinator.state is State.LOAD_PENDING
    assert coordinator.snapshot.selected_album == album_id
    await coordinator.close()


@pytest.mark.asyncio
async def test_seek_to_upcoming_photo_rebases_the_cast_order() -> None:
    assets = tuple(Asset(UUID(int=value)) for value in range(1, 12))

    class QueueSelector:
        def __init__(self) -> None:
            self.calls = 0

        async def select_assets(
            self, recent: set[UUID], batch_size: int, count: int
        ) -> tuple[Asset, ...]:
            self.calls += 1
            start = 1 if self.calls == 1 else 100
            return tuple(Asset(UUID(int=start + value)) for value in range(count))

    class QueueRelay:
        async def preload(self, asset_id: UUID) -> None:
            pass

        async def mint(self, asset_id: UUID) -> tuple[str, str]:
            return f"http://192.168.1.5:8787/image/{asset_id}", "image/jpeg"

    queue: asyncio.Queue[Any] = asyncio.Queue()
    cast = Cast()
    coordinator = Coordinator(
        queue,
        QueueSelector(),
        QueueRelay(),
        cast,
        RotationSettings(0.02, 0.01, 0.02, 5, 10),
        INSTALLATION_ID,
        0.02,
    )
    await drive_idle_to_load(coordinator, queue)
    url, metadata = await confirm_load(coordinator, cast)
    runner = asyncio.create_task(coordinator.run())
    refreshes = len(cast.refreshes)
    seek = asyncio.create_task(coordinator.seek("upcoming", str(assets[4].id), "seek-next"))
    await wait_until(lambda: len(cast.refreshes) > refreshes)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))
    await wait_until(lambda: len(cast.refreshes) > refreshes + 1)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))

    assert await seek is CommandResult.APPLIED
    assert cast.loads[-1][3]["assetId"] == str(assets[4].id)
    assert coordinator.snapshot.upcoming_assets[:2] == (assets[5].id, assets[6].id)
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_seek_to_previous_photo_replays_forward_from_that_occurrence() -> None:
    older_id, newer_id = UUID(int=20), UUID(int=21)
    history = History(autocast=False)
    history.records = [
        DisplayRecord("newer", "newer-load", str(newer_id), datetime.now(UTC)),
        DisplayRecord("older", "older-load", str(older_id), datetime(2025, 1, 1, tzinfo=UTC)),
    ]

    class AnyRelay:
        async def preload(self, asset_id: UUID) -> None:
            pass

        async def mint(self, asset_id: UUID) -> tuple[str, str]:
            return f"http://192.168.1.5:8787/image/{asset_id}", "image/jpeg"

    coordinator, _queue, _selector, cast = make_coordinator(history)
    coordinator._relay = AnyRelay()
    queued = tuple(Asset(UUID(int=value)) for value in range(30, 40))
    coordinator._upcoming = deque(queued, maxlen=10)
    url = "http://192.168.1.5:8787/image/current"
    metadata = {
        "schema": "cast-immich/v1",
        "installationId": str(INSTALLATION_ID),
        "loadId": "current-load",
        "contentUrl": url,
        "assetId": str(ASSET_ID),
    }
    await coordinator.handle(event(EventKind.CONNECTED))
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))
    runner = asyncio.create_task(coordinator.run())
    refreshes = len(cast.refreshes)
    seek = asyncio.create_task(coordinator.seek("history", "older", "seek-old"))
    await wait_until(lambda: len(cast.refreshes) > refreshes)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))
    await wait_until(lambda: len(cast.refreshes) > refreshes + 1)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))

    assert await seek is CommandResult.APPLIED
    assert cast.loads[-1][3]["assetId"] == str(older_id)
    assert coordinator.snapshot.upcoming_assets[0] == newer_id
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_backdrop_without_media_is_idle_and_sends_one_load() -> None:
    coordinator, queue, selector, cast = make_coordinator()
    await coordinator.handle(event(EventKind.CONNECTED))
    for step in range(3):
        await coordinator.handle(
            event(EventKind.RECEIVER, receiver=ReceiverStatus("E8C28D3C", "backdrop"))
        )
        await coordinator.handle(
            event(EventKind.MEDIA, media=MediaStatus("UNKNOWN", None, None, {}))
        )
        if step < 2:
            await drain_one(queue, coordinator)

    assert selector.calls == 1
    assert len(cast.loads) == 1
    assert coordinator.state is State.LOAD_PENDING
    await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("player_state", ["PLAYING", "BUFFERING", "PAUSED"])
async def test_external_active_media_is_protected(player_state: str) -> None:
    coordinator, _queue, selector, cast = make_coordinator()
    await coordinator.handle(event(EventKind.CONNECTED))
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "external"))
    )
    await coordinator.handle(
        event(
            EventKind.MEDIA,
            media=MediaStatus(player_state, "https://example/media", 4, {}),
        )
    )
    await asyncio.sleep(0.03)
    assert coordinator.state is State.PROTECTED
    assert selector.calls == 0
    assert cast.loads == []
    await coordinator.close()


@pytest.mark.asyncio
async def test_external_activity_during_idle_debounce_cancels_load() -> None:
    coordinator, _queue, selector, cast = make_coordinator()
    await observe_idle(coordinator)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "external"))
    )
    await coordinator.handle(
        event(EventKind.MEDIA, media=MediaStatus("PLAYING", "https://external", 9, {}))
    )
    await asyncio.sleep(0.03)
    assert coordinator.state is State.PROTECTED
    assert selector.calls == 0
    assert cast.loads == []
    await coordinator.close()


@pytest.mark.asyncio
async def test_disconnect_and_stale_events_cannot_load() -> None:
    coordinator, _queue, _selector, cast = make_coordinator()
    await observe_idle(coordinator)
    await coordinator.handle(event(EventKind.DISCONNECTED))
    await coordinator.handle(
        event(
            EventKind.MEDIA,
            generation=0,
            media=MediaStatus("IDLE", None, None, {}),
        )
    )
    await asyncio.sleep(0.03)
    assert coordinator.state is State.UNAVAILABLE
    assert cast.loads == []
    await coordinator.close()


@pytest.mark.asyncio
async def test_restart_recognizes_only_complete_persistent_markers() -> None:
    coordinator, _queue, _selector, cast = make_coordinator()
    url = "http://192.168.1.5:8787/image/existing"
    metadata = {
        "schema": "cast-immich/v1",
        "installationId": str(INSTALLATION_ID),
        "loadId": "existing-load",
        "contentUrl": url,
        "assetId": str(ASSET_ID),
    }
    await coordinator.handle(event(EventKind.CONNECTED))
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 1, metadata)))
    assert coordinator.snapshot.state is State.OWNED
    assert cast.loads == []
    await coordinator.close()


@pytest.mark.asyncio
async def test_restart_renews_owned_asset_with_a_fresh_relay_url() -> None:
    coordinator, queue, _selector, cast = make_coordinator()
    old_url = "http://192.168.1.5:8787/image/existing"
    metadata = {
        "schema": "cast-immich/v1",
        "installationId": str(INSTALLATION_ID),
        "loadId": "existing-load",
        "contentUrl": old_url,
        "assetId": str(ASSET_ID),
    }
    await coordinator.handle(event(EventKind.CONNECTED))
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(
        event(EventKind.MEDIA, media=MediaStatus("PLAYING", old_url, 1, metadata))
    )

    await drain_one(queue, coordinator)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(
        event(EventKind.MEDIA, media=MediaStatus("PLAYING", old_url, 1, metadata))
    )

    assert len(cast.loads) == 1
    assert cast.loads[0][3]["assetId"] == str(ASSET_ID)
    assert cast.loads[0][1] != old_url
    await coordinator.close()


@pytest.mark.asyncio
async def test_persisted_source_and_recent_assets_are_restored() -> None:
    album_id = UUID(int=42)
    history = History()
    history.source = PhotoSource(SourceKind.ALBUM, album_id)
    history.recent = (str(UUID(int=7)), str(UUID(int=8)))
    coordinator, queue, selector, _cast = make_coordinator(history)

    await observe_idle(coordinator)
    await drain_one(queue, coordinator)
    await send_idle_snapshot(coordinator)
    await drain_one(queue, coordinator)

    assert coordinator.snapshot.source_kind is SourceKind.ALBUM
    assert coordinator.snapshot.selected_album == album_id
    assert selector.recent == {UUID(int=7), UUID(int=8)}
    await coordinator.close()


@pytest.mark.asyncio
async def test_persisted_video_source_falls_back_to_image_timeline() -> None:
    history = History()
    history.source = PhotoSource(SourceKind.VIDEO)
    coordinator, queue, _selector, _cast = make_coordinator(history)

    await observe_idle(coordinator)
    await drain_one(queue, coordinator)

    assert coordinator.snapshot.source_kind is SourceKind.TIMELINE
    assert await coordinator.select_source(PhotoSource(SourceKind.VIDEO)) is False
    await coordinator.close()


@pytest.mark.asyncio
async def test_attention_immich_failure_retries_instead_of_becoming_protected() -> None:
    class RecoveringRelay(Relay):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def mint(self, asset_id: UUID) -> tuple[str, str]:
            self.calls += 1
            if self.calls == 1:
                raise PermanentImmichError("temporary authorization failure")
            return await super().mint(asset_id)

    queue: asyncio.Queue[Any] = asyncio.Queue()
    selector = Selector()
    cast = Cast()
    relay = RecoveringRelay()
    coordinator = Coordinator(
        queue,
        selector,
        relay,
        cast,
        RotationSettings(0.02, 0.001, 0.001, 5, 10, 0.001),
        INSTALLATION_ID,
        0.02,
    )

    await observe_idle(coordinator)
    await drain_one(queue, coordinator)
    await send_idle_snapshot(coordinator)
    await drain_one(queue, coordinator)
    assert coordinator.state is State.COOLDOWN
    assert coordinator.snapshot.health.value == "attention"

    await drain_one(queue, coordinator)
    await send_idle_snapshot(coordinator)
    await drain_one(queue, coordinator)
    await send_idle_snapshot(coordinator)

    assert cast.loads
    await coordinator.close()


@pytest.mark.asyncio
async def test_owned_still_image_reported_as_paused_remains_owned() -> None:
    coordinator, _queue, _selector, cast = make_coordinator()
    url = "http://192.168.1.5:8787/image/existing"
    metadata = {
        "schema": "cast-immich/v1",
        "installationId": str(INSTALLATION_ID),
        "loadId": "existing-load",
        "contentUrl": url,
        "assetId": str(ASSET_ID),
    }
    await coordinator.handle(event(EventKind.CONNECTED))
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PAUSED", url, 1, metadata)))

    assert coordinator.state is State.OWNED
    assert cast.loads == []
    await coordinator.close()


@pytest.mark.asyncio
async def test_owned_paused_still_can_be_stopped_after_fresh_ownership_check() -> None:
    coordinator, queue, _selector, cast = make_coordinator()
    await drive_idle_to_load(coordinator, queue)
    _generation, url, _mime, metadata = cast.loads[-1]
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PAUSED", url, 3, metadata)))
    runner = asyncio.create_task(coordinator.run())

    refresh_count = len(cast.refreshes)
    command = asyncio.create_task(coordinator.stop("paused-stop"))
    await wait_until(lambda: len(cast.refreshes) > refresh_count)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PAUSED", url, 3, metadata)))

    assert await command is CommandResult.APPLIED
    assert cast.stops == [1]
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_unknown_application_cannot_claim_matching_ownership() -> None:
    coordinator, _queue, _selector, cast = make_coordinator()
    url = "http://192.168.1.5:8787/image/existing"
    metadata = {
        "schema": "cast-immich/v1",
        "installationId": str(INSTALLATION_ID),
        "loadId": "existing-load",
        "contentUrl": url,
        "assetId": str(ASSET_ID),
    }
    await coordinator.handle(event(EventKind.CONNECTED))
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("UNKNOWN_APP", "external"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 1, metadata)))
    assert coordinator.state is State.PROTECTED
    assert cast.loads == []
    await coordinator.close()


class BlockingRelay:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def preload(self, asset_id: UUID) -> None:
        pass

    async def mint(self, asset_id: UUID) -> tuple[str, str]:
        self.started.set()
        await self.release.wait()
        return "http://192.168.1.5:8787/image/opaque", "image/jpeg"


@pytest.mark.asyncio
async def test_takeover_during_preview_preparation_sends_no_load() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    selector, relay, cast = Selector(), BlockingRelay(), Cast()
    settings = RotationSettings(0.02, 0.01, 0.02, 5, 10)
    coordinator = Coordinator(queue, selector, relay, cast, settings, INSTALLATION_ID, 0.02)
    runner = asyncio.create_task(coordinator.run())
    await observe_idle(coordinator)
    await asyncio.sleep(0.015)
    await send_idle_snapshot(coordinator)
    await asyncio.wait_for(relay.started.wait(), 1)

    await queue.put(event(EventKind.RECEIVER, receiver=ReceiverStatus("UNKNOWN_APP", "external")))
    await queue.put(event(EventKind.MEDIA, media=MediaStatus("PLAYING", "https://external", 2, {})))
    await asyncio.sleep(0)
    relay.release.set()
    await asyncio.sleep(0.02)

    assert coordinator.state is State.PROTECTED
    assert cast.loads == []
    await coordinator.close()
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner


@pytest.mark.asyncio
async def test_old_owned_status_cannot_confirm_new_pending_load() -> None:
    coordinator, queue, _selector, cast = make_coordinator()
    await drive_idle_to_load(coordinator, queue)
    _generation, new_url, _mime, new_metadata = cast.loads[0]
    old_url = "http://192.168.1.5:8787/image/old"
    old_metadata = dict(new_metadata, loadId="old", contentUrl=old_url)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(
        event(EventKind.MEDIA, media=MediaStatus("PLAYING", old_url, 1, old_metadata))
    )
    assert coordinator.state is State.LOAD_PENDING
    await coordinator.handle(
        event(EventKind.MEDIA, media=MediaStatus("PLAYING", new_url, 2, new_metadata))
    )
    assert coordinator.snapshot.state is State.OWNED
    await coordinator.close()


@pytest.mark.asyncio
async def test_idle_status_does_not_cancel_failure_cooldown() -> None:
    coordinator, queue, _selector, _cast = make_coordinator()
    await drive_idle_to_load(coordinator, queue)
    await coordinator.handle(event(EventKind.LOAD_FAILED))
    assert coordinator.state is State.COOLDOWN
    await send_idle_snapshot(coordinator)
    assert coordinator.state is State.COOLDOWN
    await coordinator.close()


@pytest.mark.asyncio
async def test_unreadable_persisted_state_fails_closed_for_rotation() -> None:
    coordinator, _queue, selector, cast = make_coordinator(CorruptHistory())
    await observe_idle(coordinator)
    await asyncio.sleep(0.03)
    assert coordinator.snapshot.rotation_enabled is False
    assert coordinator.snapshot.error == "history persistence failed"
    assert selector.calls == 0
    assert cast.loads == []
    await coordinator.close()


@pytest.mark.asyncio
async def test_pause_persists_and_cancels_unsent_automatic_work() -> None:
    history = History()
    coordinator, _queue, selector, cast = make_coordinator(history)
    runner = asyncio.create_task(coordinator.run())
    await observe_idle(coordinator)

    assert await coordinator.pause("pause-1") is CommandResult.APPLIED
    await asyncio.sleep(0.03)

    assert history.enabled is False
    assert coordinator.snapshot.rotation_enabled is False
    assert selector.calls == 0
    assert cast.loads == []
    assert cast.stops == []
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_pause_after_load_leaves_media_untouched_and_records_confirmation() -> None:
    history = History()
    coordinator, queue, _selector, cast = make_coordinator(history)
    await drive_idle_to_load(coordinator, queue)
    runner = asyncio.create_task(coordinator.run())

    assert await coordinator.pause("pause-after-load") is CommandResult.APPLIED
    await confirm_load(coordinator, cast)
    await asyncio.sleep(0.03)

    assert len(cast.loads) == 1
    assert cast.stops == []
    assert len(history.records) == 1
    assert coordinator.snapshot.last_display == history.records[0]
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_enable_persists_and_requires_fresh_status_before_loading() -> None:
    history = History(enabled=False)
    coordinator, _queue, selector, cast = make_coordinator(history)
    runner = asyncio.create_task(coordinator.run())
    await observe_idle(coordinator)

    command = asyncio.create_task(coordinator.enable("enable-1"))
    await wait_until(lambda: cast.refreshes == [1])
    assert selector.calls == 0
    await send_idle_snapshot(coordinator)

    assert await command is CommandResult.APPLIED
    assert history.enabled is True
    assert coordinator.snapshot.rotation_enabled is True
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup",
    ["unavailable", "synchronizing", "idle", "pending", "protected", "cooldown", "paused"],
)
async def test_protected_control_states_issue_no_media_command(setup: str) -> None:
    coordinator, queue, _selector, cast = make_coordinator()
    if setup == "synchronizing":
        await coordinator.handle(event(EventKind.CONNECTED))
    elif setup == "idle":
        await observe_idle(coordinator)
    elif setup == "pending":
        await drive_idle_to_load(coordinator, queue)
    elif setup == "protected":
        await coordinator.handle(event(EventKind.CONNECTED))
        await coordinator.handle(
            event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "external"))
        )
        await coordinator.handle(
            event(EventKind.MEDIA, media=MediaStatus("PLAYING", "https://external", 4, {}))
        )
    elif setup == "cooldown":
        await drive_idle_to_load(coordinator, queue)
        await coordinator.handle(event(EventKind.LOAD_FAILED))
        cast.loads.clear()
    elif setup == "paused":
        await coordinator.handle(event(EventKind.CONNECTED))
        await coordinator.handle(
            event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
        )
        await coordinator.handle(
            event(EventKind.MEDIA, media=MediaStatus("PAUSED", "https://owned", 4, {}))
        )

    cast.loads.clear()
    runner = asyncio.create_task(coordinator.run())
    result = await coordinator.next(f"next-{setup}")

    assert result is CommandResult.REFUSED_NOT_OWNED
    assert cast.loads == []
    assert cast.stops == []
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_fresh_ownership_race_refuses_without_media_command() -> None:
    coordinator, queue, _selector, cast = make_coordinator()
    await drive_idle_to_load(coordinator, queue)
    await confirm_load(coordinator, cast)
    cast.loads.clear()
    runner = asyncio.create_task(coordinator.run())

    refresh_count = len(cast.refreshes)
    command = asyncio.create_task(coordinator.next("race-next"))
    await wait_until(lambda: len(cast.refreshes) > refresh_count)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "external"))
    )
    await coordinator.handle(
        event(EventKind.MEDIA, media=MediaStatus("PLAYING", "https://external", 9, {}))
    )

    assert await command is CommandResult.REFUSED_NOT_OWNED
    assert cast.loads == []
    assert cast.stops == []
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_stop_terminates_external_cast_after_fresh_status() -> None:
    history = History()
    coordinator, _queue, _selector, cast = make_coordinator(history)
    await coordinator.handle(event(EventKind.CONNECTED))
    receiver = ReceiverStatus("CC1AD845", "external")
    media = MediaStatus("PLAYING", "https://external", 9, {})
    await coordinator.handle(event(EventKind.RECEIVER, receiver=receiver))
    await coordinator.handle(event(EventKind.MEDIA, media=media))
    runner = asyncio.create_task(coordinator.run())

    command = asyncio.create_task(coordinator.stop("stop-external"))
    await wait_until(lambda: cast.refreshes == [1])
    await coordinator.handle(event(EventKind.RECEIVER, receiver=receiver))
    await coordinator.handle(event(EventKind.MEDIA, media=media))

    assert await command is CommandResult.APPLIED
    assert cast.cast_stops == [1]
    assert history.autocast is False
    assert coordinator.snapshot.autocast_enabled is False
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_stop_refuses_idle_receiver() -> None:
    coordinator, _queue, _selector, cast = make_coordinator()
    await observe_idle(coordinator)
    runner = asyncio.create_task(coordinator.run())

    assert await coordinator.stop("stop-idle") is CommandResult.REFUSED_NOT_OWNED
    assert cast.cast_stops == []
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_stop_rechecks_fresh_exact_ownership_and_deduplicates_request() -> None:
    coordinator, queue, _selector, cast = make_coordinator()
    await drive_idle_to_load(coordinator, queue)
    url, metadata = await confirm_load(coordinator, cast)
    runner = asyncio.create_task(coordinator.run())

    refresh_count = len(cast.refreshes)
    first = asyncio.create_task(coordinator.stop("same-request"))
    duplicate = asyncio.create_task(coordinator.stop("same-request"))
    await wait_until(lambda: len(cast.refreshes) > refresh_count)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))

    assert await first is CommandResult.APPLIED
    assert await duplicate is CommandResult.APPLIED
    assert cast.stops == [1]
    assert len(cast.loads) == 1
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_next_rechecks_ownership_immediately_before_one_load() -> None:
    coordinator, queue, selector, cast = make_coordinator()
    await drive_idle_to_load(coordinator, queue)
    url, metadata = await confirm_load(coordinator, cast)
    runner = asyncio.create_task(coordinator.run())
    refresh_count = len(cast.refreshes)

    command = asyncio.create_task(coordinator.next("next-1"))
    await wait_until(lambda: len(cast.refreshes) > refresh_count)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))
    await wait_until(lambda: selector.calls == 2)
    await wait_until(lambda: len(cast.refreshes) > refresh_count + 1)
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))

    assert await command is CommandResult.APPLIED
    assert len(cast.loads) == 2
    assert cast.stops == []
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_cancelled_next_cannot_load_after_its_caller_times_out() -> None:
    coordinator, queue, _selector, cast = make_coordinator()
    await drive_idle_to_load(coordinator, queue)
    url, metadata = await confirm_load(coordinator, cast)
    runner = asyncio.create_task(coordinator.run())
    refresh_count = len(cast.refreshes)
    command = asyncio.create_task(coordinator.next("cancelled-next"))
    await wait_until(lambda: len(cast.refreshes) > refresh_count)
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command

    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))
    await asyncio.sleep(0.02)

    assert len(cast.loads) == 1
    assert coordinator.state is State.OWNED
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
async def test_matching_pending_confirmation_is_persisted_once() -> None:
    history = History()
    coordinator, queue, _selector, cast = make_coordinator(history)
    await drive_idle_to_load(coordinator, queue)
    url, metadata = await confirm_load(coordinator, cast)
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 3, metadata)))

    assert len(history.records) == 1
    assert history.records[0].load_id == metadata["loadId"]
    await coordinator.close()


@pytest.mark.asyncio
async def test_reclaimed_restart_ownership_does_not_persist_display() -> None:
    history = History()
    coordinator, _queue, _selector, _cast = make_coordinator(history)
    url = "http://192.168.1.5:8787/image/existing"
    metadata = {
        "schema": "cast-immich/v1",
        "installationId": str(INSTALLATION_ID),
        "loadId": "existing-load",
        "contentUrl": url,
        "assetId": str(ASSET_ID),
    }
    await coordinator.handle(event(EventKind.CONNECTED))
    await coordinator.handle(
        event(EventKind.RECEIVER, receiver=ReceiverStatus("CC1AD845", "session"))
    )
    await coordinator.handle(event(EventKind.MEDIA, media=MediaStatus("PLAYING", url, 1, metadata)))

    assert coordinator.state is State.OWNED
    assert history.records == []
    await coordinator.close()


@pytest.mark.asyncio
async def test_history_failure_keeps_confirmed_ownership_and_surfaces_sanitized_error() -> None:
    history = History(fail_records=True)
    coordinator, queue, _selector, cast = make_coordinator(history)
    await drive_idle_to_load(coordinator, queue)
    await confirm_load(coordinator, cast)

    assert coordinator.state is State.OWNED
    assert coordinator.snapshot.error == "history persistence failed"
    assert len(cast.loads) == 1
    await coordinator.close()
