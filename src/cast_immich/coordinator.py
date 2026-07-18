from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from .cast import (
    BACKDROP_RECEIVER,
    DEFAULT_MEDIA_RECEIVER,
    CastEvent,
    EventKind,
    MediaStatus,
    ReceiverStatus,
)
from .config import RotationSettings
from .history import DisplayRecord, HistoryState
from .immich import (
    Asset,
    AssetUnavailable,
    EventCollection,
    ImmichError,
    ImmichFailureKind,
    PhotoSource,
    SourceKind,
)

logger = logging.getLogger(__name__)


class State(StrEnum):
    UNAVAILABLE = "unavailable"
    SYNCHRONIZING = "synchronizing"
    IDLE_CANDIDATE = "idle_candidate"
    LOAD_PENDING = "load_pending"
    OWNED = "owned"
    PROTECTED = "protected"
    COOLDOWN = "cooldown"


class HealthLevel(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    ATTENTION = "attention"
    FATAL = "fatal"


class AssetSelector(Protocol):
    async def select_assets(
        self, recent: set[UUID], batch_size: int, count: int
    ) -> tuple[Asset, ...]: ...

    async def select_assets_from(
        self,
        recent: set[UUID],
        batch_size: int,
        count: int,
        album_id: UUID | None,
    ) -> tuple[Asset, ...]: ...

    async def select_assets_for(
        self,
        recent: set[UUID],
        batch_size: int,
        count: int,
        source: PhotoSource,
    ) -> tuple[Asset, ...]: ...


class Relay(Protocol):
    async def preload(self, asset_id: UUID) -> None: ...

    async def mint(self, asset_id: UUID) -> tuple[str, str]: ...

    def confirm(self, url: str) -> None: ...

    def retire(self, url: str) -> None: ...


class CastCommands(Protocol):
    async def load_image(
        self,
        generation: int,
        url: str,
        content_type: str,
        custom_data: dict[str, str],
    ) -> bool: ...

    async def refresh_status(self, generation: int) -> None: ...

    async def stop_media(self, generation: int) -> bool: ...

    async def stop_cast(self, generation: int) -> bool: ...


class HistoryPersistence(Protocol):
    def load(self) -> HistoryState: ...

    def set_rotation_enabled(self, enabled: bool) -> HistoryState: ...

    def set_autocast_enabled(self, enabled: bool) -> HistoryState: ...

    def set_source(
        self,
        kind: str,
        source_id: str | None = None,
        query: str | None = None,
        collection: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        city: str | None = None,
        state: str | None = None,
        country: str | None = None,
    ) -> HistoryState: ...

    def record_display(self, load_id: str, asset_id: str) -> DisplayRecord: ...


class Command(StrEnum):
    PAUSE = "pause"
    ENABLE = "enable"
    NEXT = "next"
    STOP = "stop"
    SEEK = "seek"
    AUTOCAST_ENABLE = "autocast_enable"
    AUTOCAST_DISABLE = "autocast_disable"


class CommandResult(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    REFUSED_NOT_OWNED = "refused_not_owned"
    REFUSED_BUSY = "refused_busy"
    REFUSED_INVALID_TARGET = "refused_invalid_target"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CoordinatorSnapshot:
    state: State
    rotation_enabled: bool
    generation: int
    error: str | None = None
    last_display: DisplayRecord | None = None
    upcoming_assets: tuple[UUID, ...] = ()
    current_asset: UUID | None = None
    current_load_id: str | None = None
    selected_album: UUID | None = None
    autocast_enabled: bool = True
    source_kind: SourceKind = SourceKind.TIMELINE
    selected_person: UUID | None = None
    search_query: str | None = None
    autocast_deadline: float | None = None
    health: HealthLevel = HealthLevel.DEGRADED
    health_reason: str = "starting"
    health_message: str = "Connecting to Chromecast"
    retry_deadline: float | None = None
    rotation_deadline: float | None = None
    source: PhotoSource = field(default_factory=PhotoSource)


@dataclass(frozen=True, slots=True)
class CommandEvent:
    command: Command
    request_id: str
    completion: asyncio.Future[CommandResult]
    target_kind: str | None = None
    target_id: str | None = None


@dataclass(frozen=True, slots=True)
class SourceEvent:
    source: PhotoSource
    completion: asyncio.Future[bool]


@dataclass(frozen=True, slots=True)
class _TimerEvent:
    purpose: str
    generation: int
    nonce: int


@dataclass(frozen=True, slots=True)
class _PreparedEvent:
    generation: int
    nonce: int
    mode: str
    asset: Asset | None
    url: str | None
    content_type: str | None
    error: BaseException | None
    upcoming: tuple[Asset, ...] = ()


CoordinatorEvent = CastEvent | CommandEvent | SourceEvent | _TimerEvent | _PreparedEvent


class Coordinator:
    OWNERSHIP_VERSION = "cast-immich/v1"

    def __init__(
        self,
        queue: asyncio.Queue[CoordinatorEvent],
        selector: AssetSelector,
        relay: Relay,
        cast: CastCommands,
        settings: RotationSettings,
        installation_id: UUID,
        load_timeout: float,
        *,
        history: HistoryPersistence | None = None,
        output_id: str | None = None,
    ) -> None:
        self.queue = queue
        self.state = State.UNAVAILABLE
        self._selector = selector
        self._relay = relay
        self._cast = cast
        self._settings = settings
        self._installation_id = str(installation_id)
        self._output_id = output_id
        self._load_timeout = load_timeout
        self._history = history
        self._history_loaded = history is None
        self._rotation_enabled = history is None
        self._autocast_enabled = True
        self._error: str | None = None
        self._last_display: DisplayRecord | None = None
        self._generation = 0
        self._receiver: tuple[ReceiverStatus, float] | None = None
        self._media: tuple[MediaStatus, float] | None = None
        self._recent: deque[UUID] = deque(maxlen=settings.recent_history)
        self._upcoming: deque[Asset] = deque(maxlen=10)
        self._source = PhotoSource()
        self._startup_pending = True
        self._reclaim_pending = False
        self._reclaim_asset: UUID | None = None
        self._confirmed_url: str | None = None
        self._nonce = 0
        self._timer: asyncio.Task[None] | None = None
        self._timer_purpose: str | None = None
        self._autocast_deadline: float | None = None
        self._preparation: asyncio.Task[None] | None = None
        self._preload: asyncio.Task[None] | None = None
        self._prepared: _PreparedEvent | None = None
        self._refresh: tuple[str, float] | None = None
        self._expected: tuple[str, str, UUID] | None = None
        self._failure_count = 0
        self._immich_failure: ImmichError | None = None
        self._retry_deadline: float | None = None
        self._rotation_deadline: float | None = None
        self._stopping = False
        self._active_command: tuple[CommandEvent, tuple[str, str, UUID] | None] | None = None
        self._command_results: OrderedDict[
            str, tuple[tuple[Command, str | None, str | None], CommandResult]
        ] = OrderedDict()
        self._command_waiters: dict[str, list[asyncio.Future[CommandResult]]] = {}
        self._snapshot = CoordinatorSnapshot(self.state, True, 0)

    @property
    def snapshot(self) -> CoordinatorSnapshot:
        return self._snapshot

    async def command(self, command: Command, request_id: str) -> CommandResult:
        if not request_id:
            raise ValueError("request_id must not be blank")
        if self._stopping:
            return CommandResult.FAILED
        completion: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
        await self.queue.put(CommandEvent(command, request_id, completion))
        return await completion

    async def seek(self, target_kind: str, target_id: str, request_id: str) -> CommandResult:
        if target_kind not in {"history", "upcoming"} or not target_id:
            return CommandResult.REFUSED_INVALID_TARGET
        if not request_id:
            raise ValueError("request_id must not be blank")
        if self._stopping:
            return CommandResult.FAILED
        completion: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
        await self.queue.put(
            CommandEvent(Command.SEEK, request_id, completion, target_kind, target_id)
        )
        return await completion

    async def select_source(self, source: PhotoSource | UUID | None) -> bool:
        if self._stopping:
            return False
        completion: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        normalized = (
            source
            if isinstance(source, PhotoSource)
            else PhotoSource(SourceKind.ALBUM, source)
            if source is not None
            else PhotoSource()
        )
        await self.queue.put(SourceEvent(normalized, completion))
        return await completion

    async def pause(self, request_id: str) -> CommandResult:
        return await self.command(Command.PAUSE, request_id)

    async def enable(self, request_id: str) -> CommandResult:
        return await self.command(Command.ENABLE, request_id)

    async def next(self, request_id: str) -> CommandResult:
        return await self.command(Command.NEXT, request_id)

    async def stop(self, request_id: str) -> CommandResult:
        return await self.command(Command.STOP, request_id)

    async def run(self) -> None:
        while not self._stopping:
            event = await self.queue.get()
            await self.handle(event)

    async def close(self) -> None:
        self._stopping = True
        self._invalidate_work()
        self._publish_snapshot()

    async def handle(self, event: CoordinatorEvent) -> None:
        if self._stopping:
            return
        await self._ensure_history_loaded()
        previous = self.state
        if isinstance(event, CastEvent):
            await self._handle_cast(event)
        elif isinstance(event, CommandEvent):
            await self._handle_command(event)
        elif isinstance(event, SourceEvent):
            await self._handle_source(event)
        elif event.generation != self._generation or event.nonce != self._nonce:
            pass
        elif isinstance(event, _TimerEvent):
            await self._handle_timer(event)
        else:
            await self._handle_prepared(event)
        if self.state is not previous:
            logger.info(
                "state_transition",
                extra={
                    "reason": event.kind if isinstance(event, CastEvent) else type(event).__name__,
                    "from_state": previous,
                    "to_state": self.state,
                    "generation": self._generation,
                },
            )
        self._publish_snapshot()

    async def _ensure_history_loaded(self) -> None:
        if self._history_loaded or self._history is None:
            return
        self._history_loaded = True
        try:
            state = await asyncio.to_thread(self._history.load)
            self._rotation_enabled = state.rotation_enabled
            self._autocast_enabled = state.autocast_enabled
            source_id = UUID(state.source_id) if state.source_id is not None else None
            self._source = PhotoSource(
                SourceKind(state.source_kind),
                source_id,
                state.source_query,
                EventCollection(state.source_collection) if state.source_collection else None,
                date.fromisoformat(state.source_start_date) if state.source_start_date else None,
                date.fromisoformat(state.source_end_date) if state.source_end_date else None,
                state.source_city,
                state.source_state,
                state.source_country,
            )
            recent = state.recent_asset_ids or tuple(record.asset_id for record in state.records)
            self._recent.extend(UUID(asset_id) for asset_id in reversed(recent))
        except Exception:
            self._error = "history persistence failed"

    async def _handle_command(self, event: CommandEvent) -> None:
        if event.completion.cancelled():
            return
        duplicate = self._command_results.get(event.request_id)
        if duplicate is not None:
            result = (
                duplicate[1]
                if duplicate[0] == self._command_signature(event)
                else CommandResult.FAILED
            )
            event.completion.set_result(result)
            return
        if (
            self._active_command is not None
            and self._active_command[0].request_id == event.request_id
        ):
            if self._command_signature(self._active_command[0]) == self._command_signature(event):
                self._command_waiters.setdefault(event.request_id, []).append(event.completion)
            else:
                event.completion.set_result(CommandResult.FAILED)
            return
        if self._active_command is not None:
            self._store_result(event, CommandResult.REFUSED_BUSY)
            return
        if event.command in {Command.PAUSE, Command.ENABLE}:
            await self._set_rotation(event, event.command is Command.ENABLE)
            return
        if event.command in {Command.AUTOCAST_ENABLE, Command.AUTOCAST_DISABLE}:
            await self._set_autocast(event, event.command is Command.AUTOCAST_ENABLE)
            return
        ownership = self._controllable_ownership()
        if event.command is Command.STOP:
            if not self._has_active_cast():
                self._store_result(event, CommandResult.REFUSED_NOT_OWNED)
                return
            if self._autocast_enabled and self._history is not None:
                try:
                    await asyncio.to_thread(self._history.set_autocast_enabled, False)
                except Exception:
                    self._error = "history persistence failed"
                    self._store_result(event, CommandResult.FAILED)
                    return
            self._autocast_enabled = False
            self._error = None
            self._active_command = (event, ownership)
            await self._request_refresh(event.command.value)
            return
        if ownership is None:
            self._store_result(event, CommandResult.REFUSED_NOT_OWNED)
            return
        if event.command is Command.SEEK:
            sequence = await self._seek_sequence(event)
            if sequence is None:
                self._store_result(event, CommandResult.REFUSED_INVALID_TARGET)
                return
            self._upcoming = deque(sequence[:10], maxlen=10)
        self._active_command = (event, ownership)
        await self._request_refresh(event.command.value)

    async def _handle_source(self, event: SourceEvent) -> None:
        if event.completion.cancelled():
            return
        if self._active_command is not None or self.state is State.LOAD_PENDING:
            event.completion.set_result(False)
            return
        if self._source == event.source:
            event.completion.set_result(True)
            return
        if self._history is not None:
            try:
                await asyncio.to_thread(
                    self._history.set_source,
                    event.source.kind.value,
                    str(event.source.id) if event.source.id is not None else None,
                    event.source.query,
                    event.source.collection.value if event.source.collection is not None else None,
                    event.source.start_date.isoformat()
                    if event.source.start_date is not None
                    else None,
                    event.source.end_date.isoformat()
                    if event.source.end_date is not None
                    else None,
                    event.source.city,
                    event.source.state,
                    event.source.country,
                )
            except Exception:
                self._error = "history persistence failed"
                event.completion.set_result(False)
                return
        owned = self._ownership_current()
        self._invalidate_work(preserve_expected=owned is not None)
        self._upcoming.clear()
        self._source = event.source
        if owned is not None:
            self._expected = owned
            self.state = State.OWNED
            self._begin_preparation("source")
        elif (
            self.state is State.IDLE_CANDIDATE and self._rotation_enabled and self._autocast_enabled
        ):
            self._begin_preparation("idle")
        event.completion.set_result(True)

    async def _seek_sequence(self, event: CommandEvent) -> tuple[Asset, ...] | None:
        if event.target_kind == "upcoming":
            try:
                target = UUID(str(event.target_id))
            except ValueError:
                return None
            upcoming = tuple(self._upcoming)
            index = next((i for i, asset in enumerate(upcoming) if asset.id == target), None)
            return upcoming[index:] if index is not None else None
        if event.target_kind != "history" or self._history is None:
            return None
        try:
            state = await asyncio.to_thread(self._history.load)
        except Exception:
            return None
        index = next(
            (i for i, record in enumerate(state.records) if record.event_id == event.target_id),
            None,
        )
        if index is None:
            return None
        try:
            replay = tuple(Asset(UUID(record.asset_id)) for record in state.records[index::-1])
        except ValueError:
            return None
        return replay + tuple(self._upcoming)

    async def _set_rotation(self, event: CommandEvent, enabled: bool) -> None:
        if self._rotation_enabled is enabled:
            self._store_result(event, CommandResult.ALREADY_APPLIED)
            return
        if self._history is not None:
            try:
                await asyncio.to_thread(self._history.set_rotation_enabled, enabled)
            except Exception:
                self._error = "history persistence failed"
                self._store_result(event, CommandResult.FAILED)
                return
        self._rotation_enabled = enabled
        self._error = None
        self._invalidate_work(preserve_expected=self.state is State.LOAD_PENDING)
        if enabled and self.state is not State.UNAVAILABLE:
            self._receiver = self._media = None
            await self._request_refresh("enable")
        self._store_result(event, CommandResult.APPLIED)

    async def _set_autocast(self, event: CommandEvent, enabled: bool) -> None:
        autocast_changed = self._autocast_enabled is not enabled
        rotation_changed = enabled and not self._rotation_enabled
        if not autocast_changed and not rotation_changed:
            self._store_result(event, CommandResult.ALREADY_APPLIED)
            return
        if self._history is not None:
            try:
                if rotation_changed:
                    await asyncio.to_thread(self._history.set_rotation_enabled, True)
                if autocast_changed:
                    await asyncio.to_thread(self._history.set_autocast_enabled, enabled)
            except Exception:
                if rotation_changed:
                    try:
                        await asyncio.to_thread(self._history.set_rotation_enabled, False)
                    except Exception:
                        pass
                self._error = "history persistence failed"
                self._store_result(event, CommandResult.FAILED)
                return
        self._autocast_enabled = enabled
        if rotation_changed:
            self._rotation_enabled = True
        self._error = None
        owned = self._ownership_current()
        self._invalidate_work(preserve_expected=owned is not None)
        if owned is not None:
            self._expected = owned
            self.state = State.OWNED
            if enabled:
                if self._reclaim_pending:
                    self._reclaim_asset = owned[2]
                    self._begin_preparation("reclaim")
                elif self._rotation_enabled:
                    self._schedule("rotate", self._settings.interval)
                self._store_result(event, CommandResult.APPLIED)
            else:
                self._active_command = (event, owned)
                await self._request_refresh("autocast_disable")
            return
        if enabled and self.state is not State.UNAVAILABLE:
            self._receiver = self._media = None
            await self._request_refresh("autocast_enable")
        else:
            self.state = State.IDLE_CANDIDATE if self._is_idle() else State.PROTECTED
        self._store_result(event, CommandResult.APPLIED)

    def _store_result(self, event: CommandEvent, result: CommandResult) -> None:
        if event.request_id not in self._command_results:
            self._command_results[event.request_id] = (self._command_signature(event), result)
            while len(self._command_results) > 256:
                self._command_results.popitem(last=False)
        if not event.completion.done():
            event.completion.set_result(result)
        for completion in self._command_waiters.pop(event.request_id, []):
            if not completion.done():
                completion.set_result(result)

    @staticmethod
    def _command_signature(event: CommandEvent) -> tuple[Command, str | None, str | None]:
        return event.command, event.target_kind, event.target_id

    def _complete_active(self, result: CommandResult) -> None:
        active, self._active_command = self._active_command, None
        if active is not None:
            self._store_result(active[0], result)

    async def _handle_cast(self, event: CastEvent) -> None:
        if event.kind is EventKind.CONNECTED:
            if event.generation < self._generation:
                return
            if event.generation == self._generation and self.state is not State.UNAVAILABLE:
                return
            self._invalidate_work()
            self._receiver = self._media = None
            self._generation = event.generation
            self._reclaim_pending = True
            self._reclaim_asset = None
            self.state = State.SYNCHRONIZING
            return
        if event.generation != self._generation:
            return
        if event.kind is EventKind.DISCONNECTED:
            self._invalidate_work()
            self._receiver = self._media = None
            self.state = State.UNAVAILABLE
            return
        if event.kind is EventKind.LOAD_FAILED:
            if self.state is State.LOAD_PENDING:
                self._enter_cooldown()
            return
        if event.kind is EventKind.RECEIVER and event.receiver is not None:
            if self._receiver is not None and event.observed_at < self._receiver[1]:
                return
            self._receiver = (event.receiver, event.observed_at)
            if event.receiver.app_id not in {
                None,
                BACKDROP_RECEIVER,
                DEFAULT_MEDIA_RECEIVER,
            }:
                self._retire_confirmed()
                self._invalidate_work()
                self._reclaim_pending = False
                self._reclaim_asset = None
                self.state = State.PROTECTED
                return
        elif event.kind is EventKind.MEDIA and event.media is not None:
            if self._media is not None and event.observed_at < self._media[1]:
                return
            self._media = (event.media, event.observed_at)
        await self._classify()

    async def _classify(self) -> None:
        if self.state is State.COOLDOWN:
            return
        if self._receiver is None or self._media is None:
            self.state = State.SYNCHRONIZING
            return
        if self._refresh is not None:
            purpose, cutoff = self._refresh
            if self._receiver[1] < cutoff or self._media[1] < cutoff:
                self.state = State.SYNCHRONIZING
                return
            self._refresh = None
            self._cancel_timer()
            await self._finish_refresh(purpose)
            return

        receiver, media = self._receiver[0], self._media[0]
        startup_idle = self._startup_pending and self._is_idle()
        self._startup_pending = False
        owned = self._ownership_current()
        if self.state is State.LOAD_PENDING:
            if owned is not None:
                if owned != self._expected:
                    return
                self._cancel_timer()
                self._recent.append(owned[2])
                self._failure_count = 0
                previous_url = self._confirmed_url
                confirm = getattr(self._relay, "confirm", None)
                if confirm is not None:
                    confirm(owned[1])
                self._confirmed_url = owned[1]
                if previous_url is not None and previous_url != owned[1]:
                    self._retire_url(previous_url)
                self._reclaim_pending = False
                self._reclaim_asset = None
                self._expected = owned
                self.state = State.OWNED
                await self._record_confirmation(owned)
                if self._rotation_enabled:
                    self._schedule("rotate", self._settings.interval)
                return
            if (
                receiver.app_id == DEFAULT_MEDIA_RECEIVER
                and media.content_id is None
                and media.player_state in {"IDLE", "UNKNOWN"}
            ):
                return
            self._invalidate_work()
            self.state = State.PROTECTED
            return
        if owned is not None:
            self._expected = owned
            self.state = State.OWNED
            if self._reclaim_pending and self._rotation_enabled and self._autocast_enabled:
                self._reclaim_asset = owned[2]
                self._begin_preparation("reclaim")
                return
            if not self._reclaim_pending:
                self._reclaim_asset = None
            if self._rotation_enabled and self._timer is None:
                self._schedule("rotate", self._settings.interval)
            return
        if self._is_idle():
            self._reclaim_pending = False
            self._reclaim_asset = None
            if not self._rotation_enabled or not self._autocast_enabled:
                self._invalidate_work()
                self.state = State.IDLE_CANDIDATE
                return
            if self.state is not State.IDLE_CANDIDATE:
                self._invalidate_work()
                self.state = State.IDLE_CANDIDATE
                delay = 0 if startup_idle else self._settings.autocast_delay
                self._schedule("idle", delay)
            return
        self._invalidate_work()
        self._retire_confirmed()
        self._reclaim_pending = False
        self._reclaim_asset = None
        self.state = State.PROTECTED

    async def _handle_timer(self, event: _TimerEvent) -> None:
        self._timer = None
        self._timer_purpose = None
        if event.purpose == "idle":
            self._autocast_deadline = None
        elif event.purpose == "rotate":
            self._rotation_deadline = None
        if event.purpose == "cooldown":
            self._receiver = self._media = None
            await self._request_refresh("cooldown")
        elif event.purpose in {"load_timeout", "refresh_timeout"}:
            self._enter_cooldown()
        elif event.purpose == "idle" and self.state is State.IDLE_CANDIDATE:
            await self._request_refresh("idle")
        elif event.purpose == "rotate" and self.state is State.OWNED:
            await self._request_refresh("rotate")

    async def _request_refresh(self, purpose: str) -> None:
        self._refresh = (purpose, time.monotonic())
        self.state = State.SYNCHRONIZING
        self._schedule("refresh_timeout", self._load_timeout)
        try:
            await self._cast.refresh_status(self._generation)
        except Exception:
            self._complete_active(CommandResult.FAILED)
            self._enter_cooldown()

    async def _finish_refresh(self, purpose: str) -> None:
        if self._abandon_cancelled_command():
            return
        if purpose == "cooldown":
            owned = self._ownership_current()
            if self._rotation_enabled and self._autocast_enabled and owned is not None:
                self.state = State.OWNED
                if self._reclaim_pending:
                    self._reclaim_asset = owned[2]
                self._begin_preparation("reclaim" if self._reclaim_pending else "owned")
            elif self._rotation_enabled and self._autocast_enabled and self._is_idle():
                self.state = State.IDLE_CANDIDATE
                self._begin_preparation("idle")
            else:
                await self._classify()
        elif purpose == "enable":
            await self._classify()
        elif purpose == "autocast_enable" and self._is_idle():
            self.state = State.IDLE_CANDIDATE
            self._begin_preparation("idle")
        elif purpose == "autocast_enable":
            await self._classify()
        elif purpose == "idle" and self._is_idle():
            self.state = State.IDLE_CANDIDATE
            self._begin_preparation("idle")
        elif purpose == "rotate" and self._ownership_current() is not None:
            self.state = State.OWNED
            self._begin_preparation("owned")
        elif purpose == "next" and self._active_ownership_matches():
            self.state = State.OWNED
            self._begin_preparation("next")
        elif purpose == "seek" and self._active_ownership_matches():
            self.state = State.OWNED
            self._begin_preparation("next")
        elif purpose == "stop":
            if not self._has_active_cast():
                self._complete_active(CommandResult.ALREADY_APPLIED)
                await self._classify()
            else:
                try:
                    stopped = await self._cast.stop_cast(self._generation)
                except Exception:
                    stopped = False
                self._complete_active(CommandResult.APPLIED if stopped else CommandResult.FAILED)
                if stopped:
                    self._retire_confirmed()
                    self._invalidate_work()
                    self._receiver = self._media = None
                    self.state = State.SYNCHRONIZING
                else:
                    self._enter_cooldown()
        elif purpose == "autocast_disable" and self._active_ownership_matches():
            try:
                stopped = await self._cast.stop_cast(self._generation)
            except Exception:
                stopped = False
            self._complete_active(CommandResult.APPLIED if stopped else CommandResult.FAILED)
            if stopped:
                self._retire_confirmed()
                self._invalidate_work()
                self._receiver = self._media = None
                self.state = State.SYNCHRONIZING
            else:
                self._enter_cooldown()
        elif purpose == "next_load" and self._active_ownership_matches():
            await self._send_prepared_load()
        elif purpose == "load" and self._prepared is not None:
            valid = self._is_idle() if self._prepared.mode == "idle" else self._ownership_current()
            if valid:
                await self._send_prepared_load()
            else:
                self._retire_confirmed()
                self._invalidate_work()
                self.state = State.PROTECTED
        else:
            self._complete_active(CommandResult.REFUSED_NOT_OWNED)
            self._retire_confirmed()
            self._invalidate_work()
            self.state = State.PROTECTED

    def _begin_preparation(self, mode: str) -> None:
        if self._preparation is not None:
            return
        self._nonce += 1
        nonce, generation = self._nonce, self._generation
        self._preparation = asyncio.create_task(
            self._prepare(generation, nonce, mode), name="immich-preview-preparation"
        )

    async def _prepare(self, generation: int, nonce: int, mode: str) -> None:
        try:
            if mode == "reclaim" and self._reclaim_asset is not None:
                try:
                    reclaimed = Asset(self._reclaim_asset)
                    url, content_type = await self._relay.mint(reclaimed.id)
                except AssetUnavailable:
                    pass
                else:
                    await self.queue.put(
                        _PreparedEvent(
                            generation,
                            nonce,
                            mode,
                            reclaimed,
                            url,
                            content_type,
                            None,
                            tuple(self._upcoming),
                        )
                    )
                    return
            candidates = list(self._upcoming)
            needed = 11 - len(candidates)
            try:
                recent = set(self._recent) | {asset.id for asset in candidates}
                source_selector = getattr(self._selector, "select_assets_for", None)
                if source_selector is not None:
                    selected = await source_selector(
                        recent,
                        self._settings.candidate_batch,
                        needed,
                        self._source,
                    )
                else:
                    album_selector = getattr(self._selector, "select_assets_from", None)
                    if album_selector is not None and self._source.kind in {
                        SourceKind.TIMELINE,
                        SourceKind.ALBUM,
                    }:
                        selected = await album_selector(
                            recent,
                            self._settings.candidate_batch,
                            needed,
                            self._source.id,
                        )
                    else:
                        selected = await self._selector.select_assets(
                            recent, self._settings.candidate_batch, needed
                        )
                candidates.extend(selected)
            except Exception:
                if not candidates:
                    raise
            asset = candidates[0]
            url, content_type = await self._relay.mint(asset.id)
            event = _PreparedEvent(
                generation,
                nonce,
                mode,
                asset,
                url,
                content_type,
                None,
                tuple(candidates[1:11]),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            event = _PreparedEvent(generation, nonce, mode, None, None, None, error)
        await self.queue.put(event)

    async def _handle_prepared(self, event: _PreparedEvent) -> None:
        self._preparation = None
        if self._abandon_cancelled_command():
            return
        if event.error is not None:
            if isinstance(event.error, ImmichError):
                self._immich_failure = event.error
            self._enter_cooldown()
            return
        if event.asset is None or event.url is None or event.content_type is None:
            self._enter_cooldown()
            return
        valid = self._is_idle() if event.mode == "idle" else self._ownership_current()
        if not valid:
            self._complete_active(CommandResult.REFUSED_NOT_OWNED)
            return
        self._upcoming = deque(event.upcoming, maxlen=10)
        self._failure_count = 0
        self._immich_failure = None
        self._retry_deadline = None
        self._error = None
        if self._upcoming:
            if self._preload is not None:
                self._preload.cancel()
            self._preload = asyncio.create_task(
                self._preload_next(self._upcoming[0].id), name="immich-next-image-preload"
            )
        self._prepared = event
        await self._request_refresh("next_load" if event.mode == "next" else "load")

    async def _preload_next(self, asset_id: UUID) -> None:
        try:
            await self._relay.preload(asset_id)
        except (asyncio.CancelledError, Exception) as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            logger.warning("next_image_preload_failed")

    async def _send_prepared_load(self) -> None:
        if self._abandon_cancelled_command():
            return
        prepared = self._prepared
        if prepared is None or prepared.asset is None or prepared.url is None:
            return
        load_id = str(uuid4())
        metadata = {
            "schema": self.OWNERSHIP_VERSION,
            "installationId": self._installation_id,
            "loadId": load_id,
            "contentUrl": prepared.url,
            "assetId": str(prepared.asset.id),
        }
        if self._output_id is not None:
            metadata["outputId"] = self._output_id
        self._expected = (load_id, prepared.url, prepared.asset.id)
        self._prepared = None
        self.state = State.LOAD_PENDING
        try:
            sent = await self._cast.load_image(
                self._generation, prepared.url, prepared.content_type or "", metadata
            )
        except Exception:
            sent = False
        if not sent or self.state is not State.LOAD_PENDING:
            self._complete_active(CommandResult.FAILED)
            self._enter_cooldown()
            return
        self._complete_active(CommandResult.APPLIED)
        self._schedule("load_timeout", self._load_timeout)

    async def _record_confirmation(self, owned: tuple[str, str, UUID]) -> None:
        if self._history is None:
            return
        try:
            self._last_display = await asyncio.to_thread(
                self._history.record_display, owned[0], str(owned[2])
            )
            self._error = None
        except Exception:
            self._error = "history persistence failed"

    def _ownership(self, media: MediaStatus) -> tuple[str, str, UUID] | None:
        data = media.custom_data
        if (
            data.get("schema") != self.OWNERSHIP_VERSION
            or data.get("installationId") != self._installation_id
            or not isinstance(data.get("loadId"), str)
            or not data.get("loadId")
            or data.get("contentUrl") != media.content_id
        ):
            return None
        metadata_output = data.get("outputId")
        if self._output_id is not None and metadata_output != self._output_id:
            # The original single-output metadata had no output marker.
            if self._output_id != "default" or metadata_output is not None:
                return None
        try:
            asset_id = UUID(str(data["assetId"]))
        except (KeyError, ValueError):
            return None
        return str(data["loadId"]), str(data["contentUrl"]), asset_id

    def _ownership_current(self) -> tuple[str, str, UUID] | None:
        if self._receiver is None or self._media is None:
            return None
        receiver, media = self._receiver[0], self._media[0]
        if receiver.app_id != DEFAULT_MEDIA_RECEIVER or not receiver.session_id:
            return None
        return self._ownership(media)

    def _controllable_ownership(self) -> tuple[str, str, UUID] | None:
        if self.state is not State.OWNED or self._media is None:
            return None
        return self._ownership_current()

    def _active_ownership_matches(self) -> bool:
        if self._active_command is None or self._media is None:
            return False
        return self._ownership_current() == self._active_command[1]

    def _abandon_cancelled_command(self) -> bool:
        active = self._active_command
        if active is None or not active[0].completion.cancelled():
            return False
        self._active_command = None
        self._invalidate_work()
        owned = self._ownership_current()
        if owned is not None:
            self._expected = owned
            self.state = State.OWNED
            if self._rotation_enabled:
                self._schedule("rotate", self._settings.interval)
        else:
            self.state = State.PROTECTED
        return True

    def _is_idle(self) -> bool:
        if self._receiver is None or self._media is None:
            return False
        receiver, media = self._receiver[0], self._media[0]
        idle_receiver = (receiver.app_id is None and receiver.session_id is None) or (
            receiver.app_id == BACKDROP_RECEIVER and receiver.session_id is not None
        )
        return (
            idle_receiver
            and media.player_state in {"IDLE", "UNKNOWN"}
            and media.media_session_id is None
            and media.content_id is None
        )

    def _has_active_cast(self) -> bool:
        if self._receiver is None or self._media is None or self._is_idle():
            return False
        receiver = self._receiver[0]
        return receiver.app_id is not None and receiver.session_id is not None

    def _retire_confirmed(self) -> None:
        if self._confirmed_url is not None:
            self._retire_url(self._confirmed_url)
            self._confirmed_url = None

    def _retire_url(self, url: str) -> None:
        retire = getattr(self._relay, "retire", None)
        if retire is not None:
            retire(url)

    def _enter_cooldown(self) -> None:
        self._failure_count += 1
        delay = min(self._settings.cooldown * (2 ** (self._failure_count - 1)), 300.0)
        self._invalidate_work()
        self.state = State.COOLDOWN
        self._retry_deadline = time.monotonic() + delay
        self._schedule("cooldown", delay)

    def _schedule(self, purpose: str, delay: float) -> None:
        self._cancel_timer()
        nonce, generation = self._nonce, self._generation
        self._timer_purpose = purpose
        if purpose == "idle":
            self._autocast_deadline = time.monotonic() + delay
        elif purpose == "rotate":
            self._rotation_deadline = time.monotonic() + delay

        async def send_later() -> None:
            await asyncio.sleep(delay)
            await self.queue.put(_TimerEvent(purpose, generation, nonce))

        self._timer = asyncio.create_task(send_later(), name=f"coordinator-{purpose}")

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._timer_purpose == "idle":
            self._autocast_deadline = None
        elif self._timer_purpose == "rotate":
            self._rotation_deadline = None
        self._timer_purpose = None

    def _invalidate_work(self, *, preserve_expected: bool = False) -> None:
        self._nonce += 1
        self._cancel_timer()
        self._refresh = None
        self._prepared = None
        if not preserve_expected:
            self._expected = None
        if self._preparation is not None:
            self._preparation.cancel()
            self._preparation = None
        if self._preload is not None:
            self._preload.cancel()
            self._preload = None
        self._complete_active(CommandResult.REFUSED_NOT_OWNED)

    def _publish_snapshot(self) -> None:
        current = self._ownership_current()
        health, reason, message = self._health()
        self._snapshot = CoordinatorSnapshot(
            state=self.state,
            rotation_enabled=self._rotation_enabled,
            generation=self._generation,
            error=self._error,
            last_display=self._last_display,
            upcoming_assets=tuple(asset.id for asset in self._upcoming),
            current_asset=current[2] if current is not None else None,
            current_load_id=current[0] if current is not None else None,
            selected_album=self._source.id if self._source.kind is SourceKind.ALBUM else None,
            autocast_enabled=self._autocast_enabled,
            source_kind=self._source.kind,
            selected_person=self._source.id if self._source.kind is SourceKind.PERSON else None,
            search_query=self._source.query if self._source.kind is SourceKind.SEARCH else None,
            autocast_deadline=self._autocast_deadline,
            health=health,
            health_reason=reason,
            health_message=message,
            retry_deadline=self._retry_deadline,
            rotation_deadline=self._rotation_deadline,
            source=self._source,
        )

    def _health(self) -> tuple[HealthLevel, str, str]:
        if self._error is not None:
            return HealthLevel.ATTENTION, "state_persistence", self._error
        if self._immich_failure is not None:
            kind = self._immich_failure.kind
            if kind is ImmichFailureKind.AUTHORIZATION:
                return (
                    HealthLevel.ATTENTION,
                    kind.value,
                    "Immich credentials or permissions need attention; retrying automatically",
                )
            if kind in {
                ImmichFailureKind.REQUEST_REJECTED,
                ImmichFailureKind.INCOMPATIBLE_RESPONSE,
            }:
                return (
                    HealthLevel.ATTENTION,
                    kind.value,
                    "Immich API response needs attention; retrying automatically",
                )
            if kind is ImmichFailureKind.ASSET_UNAVAILABLE:
                return (
                    HealthLevel.DEGRADED,
                    kind.value,
                    "No eligible Immich photo is available; retrying automatically",
                )
            return (
                HealthLevel.DEGRADED,
                kind.value,
                "Immich is unavailable; retrying automatically",
            )
        if self.state is State.UNAVAILABLE:
            return HealthLevel.DEGRADED, "cast_disconnected", "Chromecast disconnected; retrying"
        if self.state is State.SYNCHRONIZING:
            return HealthLevel.DEGRADED, "cast_synchronizing", "Synchronizing with Chromecast"
        if not self._rotation_enabled:
            return HealthLevel.HEALTHY, "rotation_paused", "Rotation is paused"
        if not self._autocast_enabled:
            return HealthLevel.HEALTHY, "autocast_disabled", "Autocast is disabled"
        if self.state is State.PROTECTED:
            return HealthLevel.HEALTHY, "external_playback", "External playback is protected"
        if self.state is State.IDLE_CANDIDATE:
            return HealthLevel.HEALTHY, "waiting_for_idle", "Waiting to start autocast"
        if self.state is State.COOLDOWN:
            return HealthLevel.DEGRADED, "cast_retry", "Receiver operation failed; retrying"
        return HealthLevel.HEALTHY, "healthy", "Service is operating normally"
