from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from cast_immich.cast import DiscoveredChromecast
from cast_immich.config import ConfigPersistenceError, Settings
from cast_immich.coordinator import Command, CommandResult, CoordinatorSnapshot, State
from cast_immich.history import DisplayRecord, HistoryState, HistoryStore
from cast_immich.immich import AssetUnavailable, Preview
from cast_immich.runtime import (
    ApplyStatus,
    OutputSnapshot,
    RuntimeMode,
    RuntimeSupervisor,
    ServiceGraph,
    _OutputRuntime,
)


def form(*, port: int = 8787, interval: float = 30) -> dict[str, Any]:
    return {
        "immich": {
            "url": "https://photos.example",
            "api_key": "secret-value",
            "request_timeout": 5,
            "retry_attempts": 2,
        },
        "outputs": [
            {
                "id": "default",
                "name": "Chromecast",
                "uuid": "12345678-1234-4234-8234-123456789abc",
                "discovery_timeout": 3,
                "load_timeout": 4,
                "interval": interval,
                "idle_debounce": 1,
                "cooldown": 2,
                "recent_history": 10,
                "candidate_batch": 20,
                "autocast_delay": 30,
            }
        ],
        "relay": {
            "bind_host": "127.0.0.1",
            "port": port,
            "advertised_host": "192.168.1.8",
            "token_lifetime": 60,
            "max_response_bytes": 1000,
            "max_concurrent": 2,
        },
        "service": {"installation_id_file": "identity", "log_level": "INFO"},
    }


def config_text(*, port: int = 8787) -> str:
    values = form(port=port)
    return f"""\
[immich]
url = "https://photos.example"
api_key = "secret-value"
request_timeout = 5
retry_attempts = 2
[chromecast]
uuid = "12345678-1234-4234-8234-123456789abc"
discovery_timeout = 3
load_timeout = 4
[relay]
bind_host = "127.0.0.1"
port = {port}
advertised_host = "192.168.1.8"
token_lifetime = 60
max_response_bytes = 1000
max_concurrent = 2
[rotation]
interval = {values["outputs"][0]["interval"]}
idle_debounce = 1
cooldown = 2
recent_history = 10
candidate_batch = 20
[service]
installation_id_file = "identity"
log_level = "INFO"
revision = 0
"""


class FakeGraph:
    def __init__(
        self,
        call: int,
        settings: Settings,
        *,
        fail_stage: bool,
        fail_start: bool,
        fail_validate: bool,
    ) -> None:
        self.call = call
        self.fail_stage = fail_stage
        self.fail_start = fail_start
        self.fail_validate = fail_validate
        self.stages = 0
        self.starts = 0
        self.closes = 0
        self.reconnects = 0
        self.quiesces = 0
        self.output_specs = settings.outputs
        self.commands: list[tuple[str, Command, str]] = []
        self.thumbnails: list[str] = []

    @property
    def coordinator_snapshot(self) -> CoordinatorSnapshot:
        return CoordinatorSnapshot(State.UNAVAILABLE, True, self.call)

    @property
    def output_snapshots(self) -> tuple[OutputSnapshot, ...]:
        return tuple(
            OutputSnapshot(
                output.id,
                output.name,
                output.chromecast.uuid,
                self.coordinator_snapshot,
            )
            for output in self.output_specs
        )

    async def stage(self) -> None:
        self.stages += 1
        if self.fail_stage:
            raise OSError("stage failed")

    async def validate(self) -> None:
        if self.fail_validate:
            raise OSError("validation failed")

    async def start(self) -> None:
        self.starts += 1
        if self.fail_start:
            raise OSError("start failed")

    async def command(self, output_id: str, command: Command, request_id: str) -> CommandResult:
        assert output_id in {output.id for output in self.output_specs}
        self.commands.append((output_id, command, request_id))
        return CommandResult.APPLIED

    async def reconnect(self, output_id: str) -> None:
        assert output_id in {output.id for output in self.output_specs}
        self.reconnects += 1

    def transfer_capabilities_to(self, target: object) -> None:
        return None

    async def quiesce(self) -> None:
        self.quiesces += 1

    async def thumbnail(self, output_id: str, event_id: str) -> Preview:
        assert output_id == "default"
        self.thumbnails.append(event_id)
        if event_id != "current-event":
            raise AssetUnavailable("not current")
        return Preview(b"image", "image/jpeg")

    async def upcoming_thumbnail(self, output_id: str, asset_id: UUID) -> Preview:
        assert output_id == "default"
        value = str(asset_id)
        self.thumbnails.append(value)
        if asset_id != UUID(int=8):
            raise AssetUnavailable("not upcoming")
        return Preview(b"upcoming", "image/jpeg")

    async def close(self) -> None:
        self.closes += 1


class Factory:
    def __init__(
        self,
        *,
        fail_stage_calls: set[int] | None = None,
        fail_start_calls: set[int] | None = None,
        fail_validate_calls: set[int] | None = None,
    ) -> None:
        self.fail_stage_calls = fail_stage_calls or set()
        self.fail_start_calls = fail_start_calls or set()
        self.fail_validate_calls = fail_validate_calls or set()
        self.graphs: list[FakeGraph] = []

    def __call__(self, _settings: Settings, _history: HistoryStore) -> FakeGraph:
        call = len(self.graphs) + 1
        graph = FakeGraph(
            call,
            _settings,
            fail_stage=call in self.fail_stage_calls,
            fail_start=call in self.fail_start_calls,
            fail_validate=call in self.fail_validate_calls,
        )
        self.graphs.append(graph)
        return graph


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "not = [valid", "[immich]\nurl='x'\n"])
async def test_missing_or_invalid_configuration_starts_in_setup_without_graph(
    tmp_path: Path, content: str | None
) -> None:
    path = tmp_path / "config.toml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    factory = Factory()
    supervisor = RuntimeSupervisor(path, graph_factory=factory, environ={})

    snapshot = await supervisor.start()

    assert snapshot.mode is RuntimeMode.SETUP
    assert snapshot.revision == 0
    assert factory.graphs == []
    assert supervisor.config_snapshot.form_values["immich"]["api_key"] == ""
    assert supervisor.config_snapshot.form_values["relay"]["port"] == 8787
    await supervisor.close()


@pytest.mark.asyncio
async def test_setup_candidate_activates_and_exposes_only_masked_configuration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    factory = Factory()
    supervisor = RuntimeSupervisor(path, graph_factory=factory, environ={})
    await supervisor.start()

    result = await supervisor.apply_settings(form(), expected_revision=0)

    assert result.status is ApplyStatus.APPLIED
    assert result.snapshot.mode is RuntimeMode.ACTIVE
    assert result.snapshot.revision == 1
    assert result.snapshot.generation == 1
    assert supervisor.config_snapshot.form_values["immich"]["api_key"] == ""
    assert "secret-value" not in repr(supervisor.config_snapshot)
    assert factory.graphs[0].stages == factory.graphs[0].starts == 1
    await supervisor.close()


@pytest.mark.asyncio
async def test_invalid_setup_candidate_creates_no_config_identity_or_graph(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    factory = Factory()
    supervisor = RuntimeSupervisor(path, graph_factory=factory, environ={})
    await supervisor.start()
    invalid = form()
    invalid["relay"]["port"] = 0

    result = await supervisor.apply_settings(invalid, expected_revision=0)

    assert result.status is ApplyStatus.INVALID
    assert result.snapshot.mode is RuntimeMode.SETUP
    assert not path.exists()
    assert not (tmp_path / "identity").exists()
    assert factory.graphs == []
    await supervisor.close()


@pytest.mark.asyncio
async def test_concurrent_saves_allow_one_revision_winner(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    supervisor = RuntimeSupervisor(path, graph_factory=Factory(), environ={})
    await supervisor.start()
    first_form = supervisor.config_snapshot.form_values
    second_form = supervisor.config_snapshot.form_values
    first_form["relay"]["port"] = 8788
    second_form["relay"]["port"] = 8789

    first, second = await asyncio.gather(
        supervisor.apply_settings(first_form, expected_revision=0),
        supervisor.apply_settings(second_form, expected_revision=0),
    )

    assert {first.status, second.status} == {ApplyStatus.APPLIED, ApplyStatus.CONFLICT}
    assert supervisor.snapshot.revision == 1
    await supervisor.close()


@pytest.mark.asyncio
async def test_failed_candidate_restores_file_and_reconstructs_previous_runtime(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    before = path.read_bytes()
    factory = Factory(fail_start_calls={2})
    supervisor = RuntimeSupervisor(path, graph_factory=factory, environ={})
    await supervisor.start()
    candidate_form = supervisor.config_snapshot.form_values
    candidate_form["relay"]["port"] = 8790

    result = await supervisor.apply_settings(candidate_form, expected_revision=0)

    assert result.status is ApplyStatus.FAILED
    assert result.snapshot.mode is RuntimeMode.ACTIVE
    assert result.snapshot.revision == 0
    assert result.snapshot.generation == 1
    assert path.read_bytes() == before
    assert len(factory.graphs) == 3
    assert factory.graphs[0].closes == 1
    assert factory.graphs[1].closes == 1
    assert factory.graphs[2].starts == 1
    await supervisor.close()


@pytest.mark.asyncio
async def test_stage_and_persistence_failures_keep_previous_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    factory = Factory(fail_stage_calls={2})
    supervisor = RuntimeSupervisor(path, graph_factory=factory, environ={})
    await supervisor.start()
    changed = supervisor.config_snapshot.form_values
    changed["relay"]["port"] = 8791

    staged = await supervisor.apply_settings(changed, expected_revision=0)
    assert staged.status is ApplyStatus.FAILED
    assert factory.graphs[0].closes == 0

    factory.fail_stage_calls.clear()

    def fail_persist(_candidate: object) -> None:
        raise ConfigPersistenceError("cannot persist configuration")

    monkeypatch.setattr("cast_immich.runtime.persist_settings", fail_persist)
    persisted = await supervisor.apply_settings(changed, expected_revision=0)

    assert persisted.status is ApplyStatus.FAILED
    assert supervisor.snapshot.mode is RuntimeMode.ACTIVE
    assert supervisor.snapshot.revision == 0
    assert factory.graphs[0].closes == 0
    await supervisor.close()


@pytest.mark.asyncio
async def test_candidate_validation_failure_keeps_previous_graph_active(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    factory = Factory(fail_validate_calls={2})
    supervisor = RuntimeSupervisor(path, graph_factory=factory, environ={})
    await supervisor.start()
    changed = supervisor.config_snapshot.form_values
    changed["outputs"][0]["interval"] = 90

    result = await supervisor.apply_settings(changed, expected_revision=0)

    assert result.status is ApplyStatus.FAILED
    assert result.snapshot.mode is RuntimeMode.ACTIVE
    assert result.snapshot.revision == 0
    assert factory.graphs[0].closes == 0
    assert factory.graphs[1].closes == 1
    await supervisor.close()


@pytest.mark.asyncio
async def test_slow_thumbnail_does_not_block_controls(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    factory = Factory()
    supervisor = RuntimeSupervisor(path, graph_factory=factory, environ={})
    await supervisor.start()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_thumbnail(_output_id: str, _event_id: str) -> Preview:
        started.set()
        await release.wait()
        return Preview(b"image", "image/jpeg")

    factory.graphs[0].thumbnail = blocked_thumbnail  # type: ignore[method-assign]
    thumbnail = asyncio.create_task(supervisor.thumbnail("default", "current-event"))
    await started.wait()

    assert (
        await asyncio.wait_for(supervisor.pause("default", "pause-during-thumbnail"), 0.1)
        is CommandResult.APPLIED
    )
    release.set()
    assert await thumbnail == Preview(b"image", "image/jpeg")
    await supervisor.close()


@pytest.mark.asyncio
async def test_controls_reconnect_and_discovery_delegate_to_active_boundaries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    factory = Factory()
    form_uuid = UUID(int=7)
    receiver = DiscoveredChromecast("Kitchen", form_uuid)
    discovery_calls: list[float] = []

    async def discover(discovery_timeout: float) -> tuple[DiscoveredChromecast, ...]:
        discovery_calls.append(discovery_timeout)
        return (receiver,)

    supervisor = RuntimeSupervisor(path, graph_factory=factory, discovery=discover, environ={})
    await supervisor.start()

    assert await supervisor.pause("default", "pause-1") is CommandResult.APPLIED
    assert await supervisor.reconnect("default") is True
    assert await supervisor.discover() == (receiver,)
    assert factory.graphs[0].commands == [("default", Command.PAUSE, "pause-1")]
    assert factory.graphs[0].reconnects == 1
    assert discovery_calls == [3.0]
    assert receiver.uuid == form_uuid
    assert await supervisor.thumbnail("default", "current-event") == Preview(b"image", "image/jpeg")
    assert await supervisor.upcoming_thumbnail("default", UUID(int=8)) == Preview(
        b"upcoming", "image/jpeg"
    )
    with pytest.raises(AssetUnavailable):
        await supervisor.thumbnail("default", "arbitrary-asset-id")
    assert factory.graphs[0].thumbnails == [
        "current-event",
        str(UUID(int=8)),
        "arbitrary-asset-id",
    ]
    await supervisor.close()


@pytest.mark.asyncio
async def test_history_calls_run_off_event_loop_and_shutdown_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    factory = Factory()
    supervisor = RuntimeSupervisor(path, graph_factory=factory, environ={})
    await supervisor.start()
    event_loop_thread = threading.get_ident()
    called_on: list[int] = []
    original = supervisor.history_store._load_output

    def tracked_load(output_id: str) -> Any:
        called_on.append(threading.get_ident())
        return original(output_id)

    monkeypatch.setattr(supervisor.history_store, "_load_output", tracked_load)
    await supervisor.history_snapshot("default")
    await supervisor.close()
    await supervisor.close()

    assert called_on and called_on[0] != event_loop_thread
    assert supervisor.snapshot.mode is RuntimeMode.CLOSED
    assert factory.graphs[0].closes == 1


@pytest.mark.asyncio
async def test_service_graph_thumbnail_requires_current_history_membership() -> None:
    record = DisplayRecord(
        "current-event",
        "load-id",
        "12345678-1234-4234-8234-123456789abc",
        datetime.now(UTC),
    )

    class History:
        def load(self) -> Any:
            return HistoryState(records=(record,))

    class Immich:
        def __init__(self) -> None:
            self.calls: list[tuple[UUID, int | None]] = []

        async def fetch_preview(self, asset_id: UUID, max_bytes: int | None = None) -> Preview:
            self.calls.append((asset_id, max_bytes))
            return Preview(b"preview", "image/jpeg")

    graph: Any = object.__new__(ServiceGraph)
    graph._immich = Immich()
    graph._thumbnail_max_bytes = 2048
    coordinator = type("Coordinator", (), {"snapshot": CoordinatorSnapshot(State.OWNED, True, 1)})()
    graph._outputs = {
        "default": _OutputRuntime(
            "default", "Chromecast", UUID(int=1), Any, coordinator, History(), 1
        )
    }

    with pytest.raises(AssetUnavailable):
        await graph.thumbnail("default", "arbitrary-asset-id")
    preview = await graph.thumbnail("default", "current-event")

    assert preview == Preview(b"preview", "image/jpeg")
    assert graph._immich.calls == [(UUID("12345678-1234-4234-8234-123456789abc"), 2048)]


@pytest.mark.asyncio
async def test_service_graph_upcoming_thumbnail_requires_queue_membership() -> None:
    asset_id = UUID(int=8)

    class Immich:
        async def fetch_preview(self, selected: UUID, max_bytes: int | None = None) -> Preview:
            assert (selected, max_bytes) == (asset_id, 2048)
            return Preview(b"preview", "image/jpeg")

    graph: Any = object.__new__(ServiceGraph)
    graph._immich = Immich()
    graph._thumbnail_max_bytes = 2048
    coordinator = type(
        "Coordinator",
        (),
        {"snapshot": CoordinatorSnapshot(State.OWNED, True, 1, upcoming_assets=(asset_id,))},
    )()
    graph._outputs = {
        "default": _OutputRuntime("default", "Chromecast", UUID(int=1), Any, coordinator, Any, 1)
    }

    with pytest.raises(AssetUnavailable):
        await graph.upcoming_thumbnail("default", UUID(int=9))
    assert await graph.upcoming_thumbnail("default", asset_id) == Preview(b"preview", "image/jpeg")


@pytest.mark.asyncio
async def test_supervisor_routes_commands_and_history_by_output(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    values = form()
    values["outputs"].append(
        {
            **values["outputs"][0],
            "id": "office",
            "name": "Office",
            "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        }
    )
    factory = Factory()
    supervisor = RuntimeSupervisor(path, graph_factory=factory, environ={})
    await supervisor.start()

    result = await supervisor.apply_settings(values, expected_revision=0)

    assert [output.id for output in result.snapshot.outputs] == ["default", "office"]
    assert await supervisor.pause("office", "office-pause") is CommandResult.APPLIED
    supervisor.history_store.for_output("office").set_rotation_enabled(False)
    assert (await supervisor.history_snapshot("default")).rotation_enabled is True
    assert (await supervisor.history_snapshot("office")).rotation_enabled is False
    assert factory.graphs[0].commands == [("office", Command.PAUSE, "office-pause")]
    await supervisor.close()
