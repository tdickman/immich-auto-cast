from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cast_immich.cast import DiscoveredChromecast
from cast_immich.config import SecretSource, default_form_values
from cast_immich.coordinator import Command, CommandResult, CoordinatorSnapshot, State
from cast_immich.history import DisplayRecord, HistoryState
from cast_immich.immich import Album, AssetUnavailable, Preview
from cast_immich.runtime import (
    ApplySettingsResult,
    ApplyStatus,
    ConfigSnapshot,
    OutputSnapshot,
    RuntimeMode,
    RuntimeSnapshot,
)
from cast_immich.web import CSRF_HEADER, MUTATION_HEADER, SECURITY_HEADERS, create_management_app

CSRF = "process-csrf-token"
SECRET = "super-secret-api-key"
EVENT_ID = "opaque-event"
UPCOMING_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class FakeSupervisor:
    def __init__(self) -> None:
        self.snapshot = RuntimeSnapshot(
            RuntimeMode.ACTIVE,
            4,
            2,
            (
                OutputSnapshot(
                    "living-room",
                    "Living Room",
                    UUID("12345678-1234-4234-8234-123456789abc"),
                    CoordinatorSnapshot(State.PROTECTED, True, 9, upcoming_assets=(UPCOMING_ID,)),
                ),
            ),
        )
        values = default_form_values()
        values["immich"]["url"] = "https://photos.example"
        values["outputs"][0].update(
            id="living-room",
            name="Living Room",
            uuid="12345678-1234-4234-8234-123456789abc",
        )
        self.config_snapshot = ConfigSnapshot(4, values, True, SecretSource.FILE)
        self.apply_calls: list[tuple[dict[str, dict[str, Any]], int]] = []
        self.command_calls: list[tuple[Command, str]] = []
        self.seek_calls: list[tuple[str, str, str]] = []
        self.source_calls: list[UUID | None] = []
        self.discovery_calls = 0
        self.reconnect_calls = 0
        self.apply_status = ApplyStatus.APPLIED
        self.command_result = CommandResult.REFUSED_NOT_OWNED
        self.record = DisplayRecord(
            EVENT_ID,
            "private-load-id",
            "12345678-1234-4234-8234-123456789abc",
            datetime(2026, 7, 17, 12, tzinfo=UTC),
        )

    async def apply_settings(
        self, values: dict[str, dict[str, Any]], *, expected_revision: int
    ) -> ApplySettingsResult:
        self.apply_calls.append((values, expected_revision))
        error = "relay.port must be positive" if self.apply_status is ApplyStatus.INVALID else None
        return ApplySettingsResult(self.apply_status, self.snapshot, error)

    async def discover(self) -> tuple[DiscoveredChromecast, ...]:
        self.discovery_calls += 1
        return (DiscoveredChromecast("Kitchen", UUID("12345678-1234-4234-8234-123456789abc")),)

    async def command(self, output_id: str, command: Command, request_id: str) -> CommandResult:
        assert output_id == "living-room"
        self.command_calls.append((command, request_id))
        return self.command_result

    async def reconnect(self, output_id: str) -> bool:
        assert output_id == "living-room"
        self.reconnect_calls += 1
        return True

    async def albums(self) -> tuple[Album, ...]:
        return (Album(UUID(int=7), "Summer", 24),)

    async def select_source(self, output_id: str, album_id: UUID | None) -> bool:
        assert output_id == "living-room"
        self.source_calls.append(album_id)
        return True

    async def seek(
        self, output_id: str, target_kind: str, target_id: str, request_id: str
    ) -> CommandResult:
        assert output_id == "living-room"
        self.seek_calls.append((target_kind, target_id, request_id))
        return CommandResult.APPLIED

    async def history_snapshot(self, output_id: str) -> HistoryState:
        assert output_id == "living-room"
        return HistoryState(records=(self.record,))

    async def thumbnail(self, output_id: str, event_id: str) -> Preview:
        assert output_id == "living-room"
        if event_id != EVENT_ID:
            raise AssetUnavailable("not a current history event")
        return Preview(b"jpeg-data", "image/jpeg")

    async def upcoming_thumbnail(self, output_id: str, asset_id: UUID) -> Preview:
        assert output_id == "living-room"
        if asset_id != UPCOMING_ID:
            raise AssetUnavailable("not a current upcoming asset")
        return Preview(b"upcoming-data", "image/webp")

    async def current_thumbnail(self, output_id: str, asset_id: UUID) -> Preview:
        assert output_id == "living-room"
        if asset_id != UUID(self.record.asset_id):
            raise AssetUnavailable("not current")
        return Preview(b"current-data", "image/jpeg")


@pytest.fixture
async def management() -> tuple[TestClient[Any, Any], FakeSupervisor, str]:
    supervisor = FakeSupervisor()
    client = TestClient(TestServer(create_management_app(supervisor, csrf_token=CSRF)))
    await client.start_server()
    origin = str(client.make_url("/")).rstrip("/")
    try:
        yield client, supervisor, origin
    finally:
        await client.close()


def mutation_headers(origin: str, **extra: str) -> dict[str, str]:
    return {
        "Origin": origin,
        "Content-Type": "application/json",
        MUTATION_HEADER: "1",
        CSRF_HEADER: CSRF,
        **extra,
    }


async def test_status_and_config_are_safe_and_setup_schema_is_complete(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
) -> None:
    client, supervisor, _origin = management

    status = await client.get("/api/status")
    config = await client.get("/api/config")
    status_payload = await status.json()
    config_payload = await config.json()
    serialized = json.dumps([status_payload, config_payload])

    assert status_payload["csrf_token"] == CSRF
    assert status_payload["outputs"][0]["id"] == "living-room"
    assert status_payload["outputs"][0]["name"] == "Living Room"
    assert status_payload["outputs"][0]["receiver"] == {
        "uuid": "12345678-1234-4234-8234-123456789abc"
    }
    assert status_payload["outputs"][0]["available_actions"]["stop"] is True
    assert status_payload["outputs"][0]["next_photo_remaining_seconds"] is None
    assert status_payload["coordinator"] == {
        "state": "protected",
        "rotation_enabled": True,
        "generation": 9,
        "error": None,
        "health": "degraded",
        "health_reason": "starting",
        "health_message": "Connecting to Chromecast",
        "retry_remaining_seconds": None,
        "last_display_event_id": None,
        "selected_album_id": None,
    }
    assert config_payload["values"]["immich"]["api_key"] == ""
    assert config_payload["api_key_configured"] is True
    assert config_payload["api_key_source"] == "file"
    assert set(config_payload["values"]) == set(default_form_values())
    assert SECRET not in serialized
    assert "customData" not in serialized
    assert "private-load-id" not in serialized
    assert "content_id" not in serialized
    assert "token=" not in serialized
    assert "Access-Control-Allow-Origin" not in status.headers
    for name, value in SECURITY_HEADERS.items():
        assert status.headers[name] == value
    assert supervisor.apply_calls == []


async def test_dashboard_assets_expose_complete_operator_interface(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
) -> None:
    client, _supervisor, _origin = management
    page = await client.get("/")
    script = await client.get("/app.js")
    styles = await client.get("/styles.css")
    favicon = await client.get("/favicon.svg")
    html = await page.text()
    javascript = await script.text()
    css = await styles.text()
    icon = await favicon.text()

    assert page.status == script.status == styles.status == favicon.status == 200
    assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml">' in html
    assert favicon.content_type == "image/svg+xml"
    assert 'viewBox="0 0 64 64"' in icon
    assert "#19201d" in icon
    assert "#e55c38" in icon
    assert 'name="immich.api_key" type="password"' in html
    assert 'aria-describedby="api-key-permissions key-status"' in html
    for permission in ("asset.read", "asset.view", "album.read", "person.read"):
        assert f"<code>{permission}</code>" in html
    assert 'id="output-list"' in html
    assert 'role="tablist"' in html
    assert 'id="output-total"' not in html
    assert 'id="selected-workspace"' in html
    assert "Your photos" not in html
    assert 'id="cast-state"' not in html
    assert 'id="cast-detail"' not in html
    assert 'class="readout-label"' not in html
    assert 'id="autocast-status" role="timer"' in html
    assert 'id="next-photo-countdown" role="timer"' in html
    assert "Selected output" not in html
    assert "Now showing /" not in javascript
    assert "Previous /" not in javascript
    assert "Up next /" not in javascript
    assert 'class="now-panel"' in html
    assert "Previously shown" in html
    assert "On screen" not in html
    assert "Coming up" not in html
    assert "Recently on screen" not in html
    assert html.count("use its corner control to play from there") == 2
    assert 'id="current-count"' not in html
    assert "function immichPhotoUrl(assetId)" in javascript
    assert 'link.target = "_blank"' in javascript
    assert 'button.textContent = isPending ? "…" : "▶"' in javascript
    assert "function preloadThumbnail(outputId, record)" in javascript
    assert "`${outputId}\\u0000${record.asset_id}`" in javascript
    assert "`${outputId}\\u0000${record.thumbnail_url}`" not in javascript
    assert 'fetch(record.thumbnail_url, { cache: "no-store" })' in javascript
    assert "await image.decode()" in javascript
    assert "records.forEach(record => { preloadThumbnail" in javascript
    accessible_play_label = (
        'button.setAttribute("aria-label", isPending ? "Playing from this photo" '
        ': "Play from here")'
    )
    assert accessible_play_label in javascript
    assert 'actionAvailable(output, "next")' in javascript
    assert 'setAttribute("aria-busy", String(seekPending))' in javascript
    assert ".current-record" in css
    assert ".upcoming-panel" in css
    assert ".photo-visual { position: relative; }" in css
    assert ".shared-settings { padding-top: 1.5rem; }" in css
    assert css.count("object-fit: contain") == 2
    assert "object-fit: cover" not in css
    assert 'id="stop-button"' in html
    assert 'id="reconnect-button"' not in html
    assert "Stop cast" in html
    assert "Disable autocast" in html
    assert "function createRequestId()" in javascript
    assert "crypto.randomUUID()" not in javascript
    assert "X-CSRF-Token" in javascript
    assert "output?.available_actions?.[command] === true" in javascript
    assert 'actionAvailable(output, "stop")' in javascript
    assert 'output.autocast_enabled ? "Disable autocast" : "Enable autocast"' in javascript
    assert "output.autocast_remaining_seconds" in javascript
    assert "output.next_photo_remaining_seconds" in javascript
    assert "Autocast in ${remaining}s" in javascript
    assert "if (state.disconnectedSince !== null) renderServiceSignal()" in javascript
    assert "Dashboard connection lost" in javascript
    assert "refreshCatalogs" in javascript
    assert "const canUseRotation = output.autocast_enabled" in javascript
    assert "rotation.disabled = !canUseRotation" in javascript
    assert 'performControl(state.selectedOutputId, "stop")' in javascript
    assert "addDiscoveredOutputs(state.devices)" in javascript
    assert "Save changes to activate" in javascript
    assert 'tab.setAttribute("role", "tab")' in javascript
    assert 'className = "output-quick"' not in javascript
    assert "@media (max-width: 520px)" in css
    assert "overflow-x: auto" in css
    assert page.headers["Cache-Control"] == "no-store"


async def test_csrf_is_not_returned_to_cross_origin_status(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
) -> None:
    client, _supervisor, _origin = management
    response = await client.get("/api/status", headers={"Origin": "https://evil.example"})
    assert "csrf_token" not in await response.json()


async def test_untrusted_host_is_rejected_for_read_endpoints(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
) -> None:
    client, _supervisor, _origin = management
    response = await client.get(
        "/api/outputs/living-room/history", headers={"Host": "attacker.example"}
    )
    assert response.status == 421
    assert (await response.json())["outcome"] == "invalid_host"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Content-Type": "application/json"}, 403),
        ({"Origin": "https://evil.example", "Content-Type": "application/json"}, 403),
        (
            {
                "Origin": "ORIGIN",
                "Content-Type": "text/plain",
                MUTATION_HEADER: "1",
                CSRF_HEADER: CSRF,
            },
            415,
        ),
        (
            {"Origin": "ORIGIN", "Content-Type": "application/json", CSRF_HEADER: CSRF},
            403,
        ),
        (
            {
                "Origin": "ORIGIN",
                "Content-Type": "application/json",
                MUTATION_HEADER: "1",
            },
            403,
        ),
        (
            {
                "Origin": "ORIGIN",
                "Host": "unexpected.example",
                "Content-Type": "application/json",
                MUTATION_HEADER: "1",
                CSRF_HEADER: CSRF,
            },
            421,
        ),
    ],
)
async def test_invalid_mutation_envelope_is_rejected_before_runtime(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
    headers: dict[str, str],
    expected: int,
) -> None:
    client, supervisor, origin = management
    headers = {key: origin if value == "ORIGIN" else value for key, value in headers.items()}
    response = await client.put(
        "/api/config", headers=headers, data=json.dumps({"revision": 4, "config": {}})
    )
    assert response.status == expected
    assert supervisor.apply_calls == []


async def test_invalid_json_oversized_body_and_method_are_bounded(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
) -> None:
    client, supervisor, origin = management
    malformed = await client.put("/api/config", headers=mutation_headers(origin), data="{")
    oversized = await client.put(
        "/api/config", headers=mutation_headers(origin), data=json.dumps({"x": "a" * 70_000})
    )
    method = await client.delete("/api/config", headers=mutation_headers(origin), data="{}")

    assert malformed.status == 400
    assert oversized.status == 413
    assert method.status == 405
    assert oversized.headers["Cache-Control"] == "no-store"
    assert supervisor.apply_calls == []


async def test_save_maps_conflict_and_redacts_invalid_submitted_secret(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
) -> None:
    client, supervisor, origin = management
    values = default_form_values()
    values["immich"]["api_key"] = SECRET
    values["immich"]["customData"] = "relay-token-value"
    supervisor.apply_status = ApplyStatus.CONFLICT
    conflict = await client.put(
        "/api/config",
        headers=mutation_headers(origin),
        json={"revision": 3, "config": values},
    )
    supervisor.apply_status = ApplyStatus.INVALID
    invalid = await client.put(
        "/api/config",
        headers=mutation_headers(origin),
        json={"revision": 4, "config": values},
    )
    invalid_payload = await invalid.json()

    assert conflict.status == 409
    assert invalid.status == 422
    assert invalid_payload["draft"]["immich"]["api_key"] == ""
    assert "customData" not in json.dumps(invalid_payload)
    assert SECRET not in json.dumps(invalid_payload)
    assert supervisor.snapshot.revision == 4


async def test_discovery_controls_and_reconnect_use_narrow_runtime_operations(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
) -> None:
    client, supervisor, origin = management
    headers = mutation_headers(origin)
    discovery = await client.post("/api/discovery", headers=headers, json={})
    control = await client.post(
        "/api/outputs/living-room/controls/stop",
        headers=headers,
        json={"request_id": "stop-1"},
    )
    reconnect = await client.post("/api/outputs/living-room/reconnect", headers=headers, json={})

    assert await discovery.json() == {
        "outcome": "completed",
        "devices": [
            {
                "friendly_name": "Kitchen",
                "uuid": "12345678-1234-4234-8234-123456789abc",
            }
        ],
    }
    assert control.status == 200
    control_payload = await control.json()
    assert control_payload["outcome"] == "refused_not_owned"
    assert control_payload["command"] == "stop"
    assert control_payload["request_id"] == "stop-1"
    assert reconnect.status == 202
    assert supervisor.command_calls == [(Command.STOP, "stop-1")]
    assert supervisor.discovery_calls == supervisor.reconnect_calls == 1


async def test_album_source_and_photo_seek_use_guarded_operations(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
) -> None:
    client, supervisor, origin = management
    albums = await client.get("/api/albums")
    album_id = UUID(int=7)
    source = await client.post(
        "/api/outputs/living-room/source",
        headers=mutation_headers(origin),
        json={"album_id": str(album_id)},
    )
    seek = await client.post(
        "/api/outputs/living-room/seek",
        headers=mutation_headers(origin),
        json={
            "request_id": "seek-1",
            "target_kind": "upcoming",
            "target_id": str(UPCOMING_ID),
        },
    )

    assert await albums.json() == [{"id": str(album_id), "name": "Summer", "asset_count": 24}]
    assert source.status == seek.status == 200
    assert supervisor.source_calls == [album_id]
    assert supervisor.seek_calls == [("upcoming", str(UPCOMING_ID), "seek-1")]


async def test_history_is_opaque_and_thumbnail_rejects_arbitrary_ids(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
) -> None:
    client, _supervisor, _origin = management
    history = await client.get("/api/outputs/living-room/history")
    payload = await history.json()
    serialized = json.dumps(payload)
    current = await client.get(f"/api/outputs/living-room/history/{EVENT_ID}/thumbnail")
    arbitrary = await client.get(
        "/api/outputs/living-room/history/12345678-1234-4234-8234-123456789abc/thumbnail"
    )

    assert payload == {
        "rotation_enabled": True,
        "current": None,
        "records": [
            {
                "event_id": EVENT_ID,
                "asset_id": "12345678-1234-4234-8234-123456789abc",
                "confirmed_at": "2026-07-17T12:00:00Z",
                "thumbnail_url": f"/api/outputs/living-room/history/{EVENT_ID}/thumbnail",
            }
        ],
        "upcoming": [
            {
                "asset_id": str(UPCOMING_ID),
                "thumbnail_url": f"/api/outputs/living-room/upcoming/{UPCOMING_ID}/thumbnail",
            }
        ],
    }
    assert "private-load-id" not in serialized
    assert "private-load-id" not in serialized
    assert current.status == 200
    assert current.headers["Content-Type"] == "image/jpeg"
    assert await current.read() == b"jpeg-data"
    assert arbitrary.status == 404
    assert "Authorization" not in current.headers
    assert "Access-Control-Allow-Origin" not in current.headers

    upcoming = await client.get(f"/api/outputs/living-room/upcoming/{UPCOMING_ID}/thumbnail")
    arbitrary_upcoming = await client.get(
        f"/api/outputs/living-room/upcoming/{UUID(int=9)}/thumbnail"
    )
    assert upcoming.status == 200
    assert upcoming.headers["Content-Type"] == "image/webp"
    assert await upcoming.read() == b"upcoming-data"
    assert arbitrary_upcoming.status == 404


async def test_current_photo_is_separate_from_previous_history(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
) -> None:
    client, supervisor, _origin = management
    current_id = UUID(supervisor.record.asset_id)
    supervisor.snapshot = RuntimeSnapshot(
        RuntimeMode.ACTIVE,
        4,
        2,
        (
            OutputSnapshot(
                "living-room",
                "Living Room",
                UUID("12345678-1234-4234-8234-123456789abc"),
                CoordinatorSnapshot(
                    State.OWNED,
                    True,
                    9,
                    last_display=supervisor.record,
                    current_asset=current_id,
                    current_load_id=supervisor.record.load_id,
                ),
            ),
        ),
    )

    response = await client.get("/api/outputs/living-room/history")
    payload = await response.json()
    thumbnail = await client.get(f"/api/outputs/living-room/current/{current_id}/thumbnail")

    assert payload["records"] == []
    assert payload["current"] == {
        "asset_id": str(current_id),
        "confirmed_at": "2026-07-17T12:00:00Z",
        "thumbnail_url": f"/api/outputs/living-room/current/{current_id}/thumbnail",
    }
    assert thumbnail.status == 200
    assert await thumbnail.read() == b"current-data"


async def test_unknown_output_routes_return_404_before_runtime(
    management: tuple[TestClient[Any, Any], FakeSupervisor, str],
) -> None:
    client, supervisor, origin = management
    response = await client.post(
        "/api/outputs/missing/controls/pause",
        headers=mutation_headers(origin),
        json={"request_id": "missing-1"},
    )
    history = await client.get("/api/outputs/missing/history")

    assert response.status == history.status == 404
    assert supervisor.command_calls == []
