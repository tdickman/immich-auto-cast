from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from .cast import CastAdapter, DiscoveredChromecast, discover_chromecasts
from .config import (
    ConfigConflictError,
    ConfigError,
    SecretSource,
    Settings,
    SettingsCandidate,
    SettingsDocument,
    default_form_values,
    load_editable_settings,
    persist_settings,
    prepare_settings,
    restore_settings,
)
from .coordinator import Command, CommandResult, Coordinator, CoordinatorEvent, CoordinatorSnapshot
from .history import HistoryState, HistoryStore
from .immich import Album, AssetUnavailable, ImmichClient, Preview
from .relay import ImageRelay


class RuntimeMode(StrEnum):
    SETUP = "setup"
    ACTIVE = "active"
    RECONFIGURING = "reconfiguring"
    DEGRADED = "degraded"
    CLOSED = "closed"


class ApplyStatus(StrEnum):
    APPLIED = "applied"
    CONFLICT = "conflict"
    INVALID = "invalid"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    mode: RuntimeMode
    revision: int
    generation: int
    coordinator: CoordinatorSnapshot | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    revision: int
    form_values: dict[str, dict[str, Any]]
    api_key_configured: bool
    api_key_source: SecretSource | None


@dataclass(frozen=True, slots=True)
class ApplySettingsResult:
    status: ApplyStatus
    snapshot: RuntimeSnapshot
    error: str | None = None


class ComponentGraph(Protocol):
    @property
    def coordinator_snapshot(self) -> CoordinatorSnapshot | None: ...

    async def stage(self) -> None: ...

    async def validate(self) -> None: ...

    async def start(self) -> None: ...

    async def command(self, command: Command, request_id: str) -> CommandResult: ...

    async def seek(self, target_kind: str, target_id: str, request_id: str) -> CommandResult: ...

    async def albums(self) -> tuple[Album, ...]: ...

    async def select_source(self, album_id: UUID | None) -> bool: ...

    async def reconnect(self) -> None: ...

    def transfer_capabilities_to(self, target: ComponentGraph) -> None: ...

    async def quiesce(self) -> None: ...

    async def thumbnail(self, event_id: str) -> Preview: ...

    async def upcoming_thumbnail(self, asset_id: UUID) -> Preview: ...

    async def current_thumbnail(self, asset_id: UUID) -> Preview: ...

    async def close(self) -> None: ...


GraphFactory = Callable[[Settings, HistoryStore], ComponentGraph]
Discovery = Callable[[float], Awaitable[tuple[DiscoveredChromecast, ...]]]


class ServiceGraph:
    """Dependency-ordered active service graph used by the production supervisor."""

    def __init__(self, settings: Settings, history: HistoryStore) -> None:
        queue: asyncio.Queue[CoordinatorEvent] = asyncio.Queue()
        self._immich = ImmichClient(settings.immich)
        self._history = history
        self._thumbnail_max_bytes = settings.relay.max_response_bytes
        self._relay = ImageRelay(settings.relay, self._immich)
        self._cast = CastAdapter(settings.chromecast, queue)  # type: ignore[arg-type]
        self._coordinator = Coordinator(
            queue,
            self._immich,
            self._relay,
            self._cast,
            settings.rotation,
            settings.service.installation_id,
            settings.chromecast.load_timeout,
            history=history,
        )
        self._coordinator_task: asyncio.Task[None] | None = None
        self._staged = False
        self._closed = False
        self._quiesced = False
        self._quiesce_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._command_timeout = settings.chromecast.load_timeout * 2

    @property
    def coordinator_snapshot(self) -> CoordinatorSnapshot:
        snapshot = self._coordinator.snapshot
        task = self._coordinator_task
        if task is not None and task.done() and not task.cancelled():
            with contextlib.suppress(Exception):
                if task.exception() is not None:
                    return replace(snapshot, error="coordinator stopped unexpectedly")
        return snapshot

    async def stage(self) -> None:
        if self._staged:
            return
        self._closed = False
        try:
            await self._immich.start()
            await self._relay.start()
        except BaseException:
            await self.close()
            raise
        self._staged = True

    async def validate(self) -> None:
        await self._immich.start()
        await self._immich.validate_access()

    async def start(self) -> None:
        if not self._staged:
            raise RuntimeError("component graph has not been staged")
        if self._coordinator_task is not None:
            return
        self._coordinator_task = asyncio.create_task(self._coordinator.run(), name="coordinator")
        await self._cast.start()

    async def command(self, command: Command, request_id: str) -> CommandResult:
        coordinator_task = self._coordinator_task
        if coordinator_task is None or coordinator_task.done():
            return CommandResult.FAILED
        command_task = asyncio.create_task(self._coordinator.command(command, request_id))
        done, _ = await asyncio.wait(
            {coordinator_task, command_task},
            timeout=self._command_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if command_task in done:
            return await command_task
        command_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await command_task
        return CommandResult.FAILED

    async def seek(self, target_kind: str, target_id: str, request_id: str) -> CommandResult:
        coordinator_task = self._coordinator_task
        if coordinator_task is None or coordinator_task.done():
            return CommandResult.FAILED
        seek_task = asyncio.create_task(self._coordinator.seek(target_kind, target_id, request_id))
        done, _ = await asyncio.wait(
            {coordinator_task, seek_task},
            timeout=self._command_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if seek_task in done:
            return await seek_task
        seek_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await seek_task
        return CommandResult.FAILED

    async def albums(self) -> tuple[Album, ...]:
        return await self._immich.list_albums()

    async def select_source(self, album_id: UUID | None) -> bool:
        if album_id is not None and album_id not in {album.id for album in await self.albums()}:
            return False
        return await self._coordinator.select_source(album_id)

    async def reconnect(self) -> None:
        await self._cast.reconnect()

    def transfer_capabilities_to(self, target: ComponentGraph) -> None:
        if isinstance(target, ServiceGraph):
            self._relay.transfer_capabilities_to(target._relay)

    async def quiesce(self) -> None:
        if self._quiesce_task is None:
            self._quiesce_task = asyncio.create_task(
                self._quiesce_resources(), name="service-graph-quiesce"
            )
        await asyncio.shield(self._quiesce_task)

    async def _quiesce_resources(self) -> None:
        if self._quiesced:
            return
        with contextlib.suppress(Exception):
            await self._coordinator.close()
        task, self._coordinator_task = self._coordinator_task, None
        if task is not None:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await self._cast.close()
        self._quiesced = True

    async def thumbnail(self, event_id: str) -> Preview:
        state = await asyncio.to_thread(self._history.load)
        record = next((item for item in state.records if item.event_id == event_id), None)
        if record is None:
            raise AssetUnavailable("history event is no longer available")
        try:
            asset_id = UUID(record.asset_id)
        except ValueError:
            raise AssetUnavailable("history event is invalid") from None
        return await self._immich.fetch_preview(asset_id, self._thumbnail_max_bytes)

    async def upcoming_thumbnail(self, asset_id: UUID) -> Preview:
        if asset_id not in self._coordinator.snapshot.upcoming_assets:
            raise AssetUnavailable("asset is no longer upcoming")
        return await self._immich.fetch_preview(asset_id, self._thumbnail_max_bytes)

    async def current_thumbnail(self, asset_id: UUID) -> Preview:
        if asset_id != self._coordinator.snapshot.current_asset:
            raise AssetUnavailable("asset is no longer current")
        return await self._immich.fetch_preview(asset_id, self._thumbnail_max_bytes)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_resources(), name="service-graph-close"
            )
        await asyncio.shield(self._close_task)

    async def _close_resources(self) -> None:
        if self._closed:
            return
        await self.quiesce()
        try:
            await self._relay.close()
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                await self._immich.close()
            self._staged = False
            self._closed = True


class RuntimeSupervisor:
    """Own setup mode and transactional replacement of one active service graph."""

    def __init__(
        self,
        config_path: Path,
        *,
        history_path: Path | None = None,
        environ: dict[str, str] | None = None,
        graph_factory: GraphFactory = ServiceGraph,
        discovery: Discovery = discover_chromecasts,
    ) -> None:
        self._config_path = config_path
        self._history = HistoryStore(history_path or config_path.with_name("state.json"))
        self._environ = environ
        self._graph_factory = graph_factory
        self._discovery = discovery
        self._lock = asyncio.Lock()
        self._graph: ComponentGraph | None = None
        self._document: SettingsDocument | None = None
        self._mode = RuntimeMode.SETUP
        self._generation = 0
        self._error: str | None = None
        self._started = False
        self._closed = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._draining_tasks: set[asyncio.Task[None]] = set()
        self._thumbnail_semaphore = asyncio.Semaphore(4)
        self._close_task: asyncio.Task[None] | None = None

    @property
    def history_store(self) -> HistoryStore:
        return self._history

    @property
    def snapshot(self) -> RuntimeSnapshot:
        graph = self._graph
        return RuntimeSnapshot(
            self._mode,
            self._document.revision if self._document is not None else 0,
            self._generation,
            graph.coordinator_snapshot if graph is not None else None,
            self._error,
        )

    @property
    def config_snapshot(self) -> ConfigSnapshot:
        document = self._document
        if document is None:
            return ConfigSnapshot(0, default_form_values(), False, None)
        return ConfigSnapshot(
            document.revision,
            copy.deepcopy(document.form_values),
            document.api_key_configured,
            document.api_key_source,
        )

    async def history_snapshot(self) -> HistoryState:
        return await asyncio.to_thread(self._history.load)

    async def start(self) -> RuntimeSnapshot:
        async with self._lock:
            if self._closed or self._started:
                return self.snapshot
            self._started = True
            try:
                document = await asyncio.to_thread(
                    load_editable_settings, self._config_path, self._environ
                )
            except ConfigError as error:
                self._error = str(error)
                return self.snapshot
            self._document = document
            graph: ComponentGraph | None = None
            try:
                graph = self._graph_factory(document.settings, self._history)
                await graph.stage()
                await graph.start()
            except Exception:
                if graph is not None:
                    await self._close_graph(graph)
                self._error = "runtime activation failed"
                return self.snapshot
            self._graph = graph
            self._generation = 1
            self._mode = RuntimeMode.ACTIVE
            self._error = None
            return self.snapshot

    async def apply_settings(
        self,
        form_values: dict[str, dict[str, Any]],
        *,
        expected_revision: int,
    ) -> ApplySettingsResult:
        async with self._lock:
            if self._closed:
                return ApplySettingsResult(ApplyStatus.CLOSED, self.snapshot, "runtime is closed")
            try:
                candidate = await asyncio.to_thread(
                    prepare_settings,
                    self._config_path,
                    form_values,
                    expected_revision=expected_revision,
                    environ=self._environ,
                )
            except ConfigConflictError as error:
                return ApplySettingsResult(ApplyStatus.CONFLICT, self.snapshot, str(error))
            except ConfigError as error:
                return ApplySettingsResult(ApplyStatus.INVALID, self.snapshot, str(error))

            previous_graph = self._graph
            previous_document = self._document
            previous_closed = False
            persisted = False
            candidate_graph: ComponentGraph | None = None
            self._mode = RuntimeMode.RECONFIGURING
            self._error = None
            try:
                candidate_graph = self._graph_factory(candidate.document.settings, self._history)
                await candidate_graph.validate()
                same_endpoint = previous_graph is not None and self._same_relay_endpoint(
                    previous_document, candidate.document
                )
                if same_endpoint and previous_graph is not None:
                    previous_graph.transfer_capabilities_to(candidate_graph)
                    previous_closed = True
                    await previous_graph.close()
                    self._graph = None
                await candidate_graph.stage()
                persist_task = asyncio.create_task(
                    asyncio.to_thread(persist_settings, candidate),
                    name="persist-configuration",
                )
                try:
                    await asyncio.shield(persist_task)
                    persisted = True
                except asyncio.CancelledError:
                    await persist_task
                    persisted = True
                    raise
                if previous_graph is not None and not previous_closed:
                    previous_closed = True
                    await previous_graph.quiesce()
                    self._graph = None
                await candidate_graph.start()
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._rollback(
                        candidate_graph,
                        candidate,
                        persisted,
                        previous_document,
                        previous_graph,
                        previous_closed,
                    )
                )
                raise
            except ConfigConflictError as error:
                await self._rollback(
                    candidate_graph,
                    candidate,
                    persisted,
                    previous_document,
                    previous_graph,
                    previous_closed,
                )
                return ApplySettingsResult(ApplyStatus.CONFLICT, self.snapshot, str(error))
            except Exception:
                await self._rollback(
                    candidate_graph,
                    candidate,
                    persisted,
                    previous_document,
                    previous_graph,
                    previous_closed,
                )
                return ApplySettingsResult(
                    ApplyStatus.FAILED, self.snapshot, "candidate activation failed"
                )

            self._graph = candidate_graph
            self._document = candidate.document
            self._generation += 1
            self._mode = RuntimeMode.ACTIVE
            self._error = None
            logging.getLogger().setLevel(candidate.document.settings.service.log_level)
            if previous_graph is not None and not same_endpoint:
                self._start_relay_drain(
                    previous_graph,
                    previous_document.settings.relay.token_lifetime
                    if previous_document is not None
                    else 0,
                )
            return ApplySettingsResult(ApplyStatus.APPLIED, self.snapshot)

    async def command(self, command: Command, request_id: str) -> CommandResult:
        async with self._lock:
            if self._graph is None or self._closed:
                return CommandResult.FAILED
            return await self._graph.command(command, request_id)

    async def pause(self, request_id: str) -> CommandResult:
        return await self.command(Command.PAUSE, request_id)

    async def enable(self, request_id: str) -> CommandResult:
        return await self.command(Command.ENABLE, request_id)

    async def next(self, request_id: str) -> CommandResult:
        return await self.command(Command.NEXT, request_id)

    async def stop(self, request_id: str) -> CommandResult:
        return await self.command(Command.STOP, request_id)

    async def seek(self, target_kind: str, target_id: str, request_id: str) -> CommandResult:
        async with self._lock:
            if self._graph is None or self._closed:
                return CommandResult.FAILED
            return await self._graph.seek(target_kind, target_id, request_id)

    async def albums(self) -> tuple[Album, ...]:
        async with self._lock:
            if self._graph is None or self._closed:
                return ()
            return await self._graph.albums()

    async def select_source(self, album_id: UUID | None) -> bool:
        async with self._lock:
            if self._graph is None or self._closed:
                return False
            return await self._graph.select_source(album_id)

    async def reconnect(self) -> bool:
        async with self._lock:
            if self._graph is None or self._closed:
                return False
            task = self._reconnect_task
            if task is None or task.done():
                task = asyncio.create_task(self._graph.reconnect(), name="runtime-reconnect")
                self._reconnect_task = task
        try:
            await asyncio.shield(task)
            return True
        finally:
            if task.done() and self._reconnect_task is task:
                self._reconnect_task = None

    async def thumbnail(self, event_id: str) -> Preview:
        async with self._lock:
            if self._graph is None or self._closed:
                raise AssetUnavailable("thumbnail service is unavailable")
            graph = self._graph
        try:
            async with self._thumbnail_semaphore:
                return await graph.thumbnail(event_id)
        except RuntimeError:
            raise AssetUnavailable("thumbnail service changed during request") from None

    async def upcoming_thumbnail(self, asset_id: UUID) -> Preview:
        async with self._lock:
            if self._graph is None or self._closed:
                raise AssetUnavailable("thumbnail service is unavailable")
            graph = self._graph
        try:
            async with self._thumbnail_semaphore:
                return await graph.upcoming_thumbnail(asset_id)
        except RuntimeError:
            raise AssetUnavailable("thumbnail service changed during request") from None

    async def current_thumbnail(self, asset_id: UUID) -> Preview:
        async with self._lock:
            if self._graph is None or self._closed:
                raise AssetUnavailable("thumbnail service is unavailable")
            graph = self._graph
        try:
            async with self._thumbnail_semaphore:
                return await graph.current_thumbnail(asset_id)
        except RuntimeError:
            raise AssetUnavailable("thumbnail service changed during request") from None

    async def discover(
        self, discovery_timeout: float | None = None
    ) -> tuple[DiscoveredChromecast, ...]:
        if self._closed:
            return ()
        document = self._document
        bounded_timeout = (
            document.settings.chromecast.discovery_timeout
            if discovery_timeout is None and document is not None
            else discovery_timeout or 10.0
        )
        return await self._discovery(bounded_timeout)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_resources(), name="runtime-supervisor-close"
            )
        await asyncio.shield(self._close_task)

    async def _close_resources(self) -> None:
        async with self._lock:
            if self._closed:
                return
            graph, self._graph = self._graph, None
            if graph is not None:
                await graph.close()
            draining = list(self._draining_tasks)
            for task in draining:
                task.cancel()
            if draining:
                await asyncio.gather(*draining, return_exceptions=True)
            self._closed = True
            self._mode = RuntimeMode.CLOSED

    async def _rollback(
        self,
        candidate_graph: ComponentGraph | None,
        candidate: SettingsCandidate,
        persisted: bool,
        previous_document: SettingsDocument | None,
        previous_graph: ComponentGraph | None,
        previous_closed: bool,
    ) -> None:
        restore_failed = False
        if persisted:
            try:
                await asyncio.to_thread(restore_settings, candidate)
            except ConfigError:
                restore_failed = True
        self._document = candidate.document if restore_failed else previous_document
        self._graph = previous_graph if not previous_closed else None
        if previous_closed and previous_document is not None:
            replacement: ComponentGraph | None = None
            try:
                replacement = self._graph_factory(previous_document.settings, self._history)
                if previous_graph is not None:
                    previous_graph.transfer_capabilities_to(replacement)
                if candidate_graph is not None:
                    candidate_graph.transfer_capabilities_to(replacement)
                    await self._close_graph(candidate_graph)
                    candidate_graph = None
                if previous_graph is not None:
                    await self._close_graph(previous_graph)
                await replacement.stage()
                await replacement.start()
                self._graph = replacement
            except Exception:
                if replacement is not None:
                    await self._close_graph(replacement)
        if candidate_graph is not None:
            await self._close_graph(candidate_graph)
        self._mode = (
            RuntimeMode.DEGRADED
            if restore_failed
            else RuntimeMode.ACTIVE
            if self._graph is not None
            else RuntimeMode.SETUP
        )
        self._error = (
            "configuration rollback failed" if restore_failed else "candidate activation failed"
        )

    @staticmethod
    async def _close_graph(graph: ComponentGraph) -> None:
        with contextlib.suppress(Exception):
            await graph.close()

    @staticmethod
    def _same_relay_endpoint(
        previous: SettingsDocument | None, candidate: SettingsDocument
    ) -> bool:
        if previous is None:
            return False
        old = previous.settings.relay
        new = candidate.settings.relay
        return (old.bind_host, old.port) == (new.bind_host, new.port)

    def _start_relay_drain(self, graph: ComponentGraph, delay: float) -> None:
        async def close_later() -> None:
            try:
                await asyncio.sleep(delay)
            finally:
                await self._close_graph(graph)

        task = asyncio.create_task(close_later(), name="relay-capability-drain")
        self._draining_tasks.add(task)
        task.add_done_callback(self._draining_tasks.discard)
