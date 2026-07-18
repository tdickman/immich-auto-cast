from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from cast_immich.cast import CastEvent, EventKind, MediaStatus, ReceiverStatus
from cast_immich.config import RotationSettings
from cast_immich.coordinator import CommandResult, Coordinator, State
from cast_immich.history import DisplayRecord, HistoryState
from cast_immich.immich import Asset

INSTALLATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ASSET_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


class Selector:
    def __init__(self) -> None:
        self.calls = 0

    async def select_asset(self, recent: set[UUID], batch_size: int) -> Asset:
        self.calls += 1
        return Asset(ASSET_ID)


class Relay:
    async def mint(self, asset_id: UUID) -> tuple[str, str]:
        assert asset_id == ASSET_ID
        return "http://192.168.1.5:8787/image/opaque", "image/webp"


class Cast:
    def __init__(self) -> None:
        self.loads: list[tuple[int, str, str, dict[str, str]]] = []
        self.refreshes: list[int] = []
        self.stops: list[int] = []

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


class History:
    def __init__(self, *, enabled: bool = True, fail_records: bool = False) -> None:
        self.enabled = enabled
        self.fail_records = fail_records
        self.records: list[DisplayRecord] = []

    def load(self) -> HistoryState:
        return HistoryState(rotation_enabled=self.enabled, records=tuple(self.records))

    def set_rotation_enabled(self, enabled: bool) -> HistoryState:
        self.enabled = enabled
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
) -> tuple[Coordinator, asyncio.Queue[Any], Selector, Cast]:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    selector = Selector()
    cast = Cast()
    settings = RotationSettings(0.02, 0.01, 0.02, 5, 10)
    coordinator = Coordinator(
        queue, selector, Relay(), cast, settings, INSTALLATION_ID, 0.02, history=history
    )
    return coordinator, queue, selector, cast


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
    assert len(cast.loads) == 1
    await coordinator.close()


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
@pytest.mark.parametrize("action", ["next", "stop"])
async def test_protected_control_states_issue_no_media_command(setup: str, action: str) -> None:
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
    result = await getattr(coordinator, action)(f"{action}-{setup}")

    assert result is CommandResult.REFUSED_NOT_OWNED
    assert cast.loads == []
    assert cast.stops == []
    await coordinator.close()
    runner.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["next", "stop"])
async def test_fresh_ownership_race_refuses_without_media_command(action: str) -> None:
    coordinator, queue, _selector, cast = make_coordinator()
    await drive_idle_to_load(coordinator, queue)
    await confirm_load(coordinator, cast)
    cast.loads.clear()
    runner = asyncio.create_task(coordinator.run())

    refresh_count = len(cast.refreshes)
    command = asyncio.create_task(getattr(coordinator, action)(f"race-{action}"))
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
