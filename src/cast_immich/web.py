from __future__ import annotations

import copy
import ipaddress
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from aiohttp import web

from .cast import DiscoveryError
from .config import default_form_values
from .coordinator import Command, CommandResult, CoordinatorSnapshot
from .history import DisplayRecord, HistoryError
from .immich import AssetUnavailable, ImmichError, PhotoSource, SourceKind
from .relay import ALLOWED_IMAGE_TYPES
from .runtime import ApplyStatus, ConfigSnapshot, OutputSnapshot, RuntimeSnapshot, RuntimeSupervisor

CLIENT_MAX_SIZE = 64 * 1024
MUTATION_HEADER = "X-Cast-Immich-Request"
CSRF_HEADER = "X-CSRF-Token"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'; img-src 'self' data: blob:"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def create_management_app(
    supervisor: RuntimeSupervisor,
    *,
    csrf_token: str | None = None,
    allowed_hosts: set[str] | None = None,
) -> web.Application:
    token = csrf_token or secrets.token_urlsafe(32)

    @web.middleware
    async def security_headers(request: web.Request, handler: Any) -> web.StreamResponse:
        if _request_origin(request, allowed_hosts) is None:
            response = web.json_response(
                {"outcome": "invalid_host", "error": "management host is not allowed"},
                status=421,
            )
        else:
            try:
                response = await handler(request)
            except web.HTTPException as error:
                headers = {
                    name: value
                    for name, value in error.headers.items()
                    if name.lower() not in {"content-length", "content-type"}
                }
                response = web.json_response(
                    {"outcome": "http_error", "error": error.reason},
                    status=error.status,
                    headers=headers,
                )
        if not isinstance(response, web.StreamResponse):
            raise TypeError("management handler returned an invalid response")
        response.headers.update(SECURITY_HEADERS)
        return response

    app = web.Application(client_max_size=CLIENT_MAX_SIZE, middlewares=[security_headers])

    async def index(_request: web.Request) -> web.Response:
        return _static_response("index.html", "text/html")

    async def script(_request: web.Request) -> web.Response:
        return _static_response("app.js", "text/javascript")

    async def stylesheet(_request: web.Request) -> web.Response:
        return _static_response("styles.css", "text/css")

    async def favicon(_request: web.Request) -> web.Response:
        return _static_response("favicon.svg", "image/svg+xml")

    async def status(request: web.Request) -> web.Response:
        payload = _runtime_json(supervisor.snapshot)
        if _origin_is_absent_or_same(request, allowed_hosts):
            payload["csrf_token"] = token
        return web.json_response(payload)

    async def config(request: web.Request) -> web.Response:
        payload = _config_json(supervisor.config_snapshot)
        if _origin_is_absent_or_same(request, allowed_hosts):
            payload["csrf_token"] = token
        return web.json_response(payload)

    async def save(request: web.Request) -> web.Response:
        failure = _mutation_failure(request, token, allowed_hosts)
        if failure is not None:
            return failure
        body = await _json_object(request)
        if isinstance(body, web.Response):
            return body
        revision = body.get("revision")
        values = body.get("config")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not isinstance(values, dict)
        ):
            return _error(400, "invalid_request", "revision and config are required")
        result = await supervisor.apply_settings(values, expected_revision=revision)
        payload: dict[str, Any] = {
            "outcome": result.status.value,
            "status": _runtime_json(result.snapshot),
            "config": _config_json(supervisor.config_snapshot),
        }
        if result.error is not None:
            payload["error"] = result.error
        if result.status is ApplyStatus.INVALID:
            payload["draft"] = _safe_draft(values)
        status_code = {
            ApplyStatus.APPLIED: 200,
            ApplyStatus.CONFLICT: 409,
            ApplyStatus.INVALID: 422,
            ApplyStatus.FAILED: 503,
            ApplyStatus.CLOSED: 503,
        }[result.status]
        return web.json_response(payload, status=status_code)

    async def discovery(request: web.Request) -> web.Response:
        failure = _mutation_failure(request, token, allowed_hosts)
        if failure is not None:
            return failure
        body = await _json_object(request)
        if isinstance(body, web.Response):
            return body
        try:
            devices = await supervisor.discover()
        except DiscoveryError:
            return _error(503, "discovery_failed", "Chromecast discovery failed")
        return web.json_response(
            {
                "outcome": "completed",
                "devices": [
                    {"friendly_name": item.friendly_name, "uuid": str(item.uuid)}
                    for item in devices
                ],
            }
        )

    async def control(request: web.Request) -> web.Response:
        output_id = _request_output_id(request, supervisor.snapshot)
        if output_id is None:
            raise web.HTTPNotFound
        failure = _mutation_failure(request, token, allowed_hosts)
        if failure is not None:
            return failure
        body = await _json_object(request)
        if isinstance(body, web.Response):
            return body
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            return _error(400, "invalid_request", "request_id is required")
        try:
            command = Command(request.match_info["command"])
        except ValueError:
            raise web.HTTPNotFound from None
        outcome = await supervisor.command(output_id, command, request_id)
        code = 503 if outcome is CommandResult.FAILED else 200
        return web.json_response(
            {
                "outcome": outcome.value,
                "command": command.value,
                "request_id": request_id,
                "status": _runtime_json(supervisor.snapshot),
            },
            status=code,
        )

    async def reconnect(request: web.Request) -> web.Response:
        output_id = _request_output_id(request, supervisor.snapshot)
        if output_id is None:
            raise web.HTTPNotFound
        failure = _mutation_failure(request, token, allowed_hosts)
        if failure is not None:
            return failure
        body = await _json_object(request)
        if isinstance(body, web.Response):
            return body
        if body:
            return _error(400, "invalid_request", "request body must be empty")
        available = await supervisor.reconnect(output_id)
        return web.json_response(
            {"outcome": "accepted" if available else "unavailable"},
            status=202 if available else 503,
        )

    async def history(request: web.Request) -> web.Response:
        snapshot = supervisor.snapshot
        output_id = _request_output_id(request, snapshot)
        if output_id is None:
            raise web.HTTPNotFound
        try:
            state = await supervisor.history_snapshot(output_id)
        except HistoryError:
            return _error(503, "history_unavailable", "history is unavailable")
        output = next(item for item in snapshot.outputs if item.id == output_id)
        coordinator = output.coordinator
        upcoming = coordinator.upcoming_assets if coordinator is not None else ()
        current_asset = coordinator.current_asset if coordinator is not None else None
        current_record = (
            next(
                (
                    record
                    for record in state.records
                    if record.load_id == coordinator.current_load_id
                ),
                None,
            )
            if coordinator is not None and coordinator.current_load_id is not None
            else None
        )
        return web.json_response(
            {
                "rotation_enabled": state.rotation_enabled,
                "current": (
                    _current_json(output_id, current_asset, current_record)
                    if current_asset is not None
                    else None
                ),
                "records": [
                    _record_json(output_id, record)
                    for record in state.records
                    if record is not current_record
                ],
                "upcoming": [_upcoming_json(output_id, asset_id) for asset_id in upcoming],
            }
        )

    async def albums(_request: web.Request) -> web.Response:
        try:
            items = await supervisor.albums()
        except ImmichError:
            return _error(502, "albums_upstream_error", "albums are unavailable")
        return web.json_response(
            [
                {"id": str(album.id), "name": album.name, "asset_count": album.asset_count}
                for album in items
            ]
        )

    async def people(_request: web.Request) -> web.Response:
        try:
            items = await supervisor.people()
        except ImmichError:
            return _error(502, "people_upstream_error", "people are unavailable")
        return web.json_response([{"id": str(person.id), "name": person.name} for person in items])

    async def source(request: web.Request) -> web.Response:
        output_id = _request_output_id(request, supervisor.snapshot)
        if output_id is None:
            raise web.HTTPNotFound
        failure = _mutation_failure(request, token, allowed_hosts)
        if failure is not None:
            return failure
        body = await _json_object(request)
        if isinstance(body, web.Response):
            return body
        legacy_album_request = "kind" not in body and "album_id" in body
        kind_value = body.get("kind")
        if kind_value is None and "album_id" in body:
            kind_value = "album" if body.get("album_id") is not None else "timeline"
        try:
            kind = SourceKind(kind_value or "timeline")
        except (TypeError, ValueError):
            return _error(400, "invalid_request", "kind must be timeline, album, person, or search")
        source_id: UUID | None = None
        query: str | None = None
        if kind in {SourceKind.ALBUM, SourceKind.PERSON}:
            value = body.get("id", body.get("album_id"))
            try:
                source_id = UUID(value)
            except (TypeError, ValueError):
                return _error(400, "invalid_request", "source id must be a UUID")
        elif kind is SourceKind.SEARCH:
            value = body.get("query")
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
                return _error(400, "invalid_request", "query must contain 1 to 200 characters")
            query = value.strip()
        selected = PhotoSource(kind, source_id, query)
        try:
            applied = await supervisor.select_source(
                output_id,
                source_id if legacy_album_request and kind is SourceKind.ALBUM else selected,
            )
        except ImmichError:
            return _error(502, "source_upstream_error", "photo source is unavailable")
        if not applied:
            return _error(
                409, "source_not_applied", "photo source is unavailable or playback is busy"
            )
        return web.json_response({"outcome": "applied", "source": _source_json(selected)})

    async def seek(request: web.Request) -> web.Response:
        output_id = _request_output_id(request, supervisor.snapshot)
        if output_id is None:
            raise web.HTTPNotFound
        failure = _mutation_failure(request, token, allowed_hosts)
        if failure is not None:
            return failure
        body = await _json_object(request)
        if isinstance(body, web.Response):
            return body
        request_id = body.get("request_id")
        target_kind = body.get("target_kind")
        target_id = body.get("target_id")
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or not isinstance(target_kind, str)
            or not target_kind.strip()
            or not isinstance(target_id, str)
            or not target_id.strip()
        ):
            return _error(400, "invalid_request", "request_id and target are required")
        outcome = await supervisor.seek(output_id, target_kind, target_id, request_id)
        code = 503 if outcome is CommandResult.FAILED else 200
        return web.json_response({"outcome": outcome.value, "request_id": request_id}, status=code)

    async def thumbnail(request: web.Request) -> web.Response:
        output_id = _request_output_id(request, supervisor.snapshot)
        if output_id is None:
            raise web.HTTPNotFound
        try:
            preview = await supervisor.thumbnail(output_id, request.match_info["event_id"])
        except AssetUnavailable:
            return _error(404, "thumbnail_unavailable", "thumbnail is unavailable")
        except HistoryError:
            return _error(503, "history_unavailable", "thumbnail is unavailable")
        except ImmichError:
            return _error(502, "thumbnail_upstream_error", "thumbnail is unavailable")
        if preview.content_type not in ALLOWED_IMAGE_TYPES:
            return _error(502, "thumbnail_invalid", "thumbnail is unavailable")
        return web.Response(body=preview.body, content_type=preview.content_type)

    async def upcoming_thumbnail(request: web.Request) -> web.Response:
        output_id = _request_output_id(request, supervisor.snapshot)
        if output_id is None:
            raise web.HTTPNotFound
        try:
            asset_id = UUID(request.match_info["asset_id"])
            preview = await supervisor.upcoming_thumbnail(output_id, asset_id)
        except (ValueError, AssetUnavailable):
            return _error(404, "thumbnail_unavailable", "thumbnail is unavailable")
        except ImmichError:
            return _error(502, "thumbnail_upstream_error", "thumbnail is unavailable")
        if preview.content_type not in ALLOWED_IMAGE_TYPES:
            return _error(502, "thumbnail_invalid", "thumbnail is unavailable")
        return web.Response(body=preview.body, content_type=preview.content_type)

    async def current_thumbnail(request: web.Request) -> web.Response:
        output_id = _request_output_id(request, supervisor.snapshot)
        if output_id is None:
            raise web.HTTPNotFound
        try:
            asset_id = UUID(request.match_info["asset_id"])
            preview = await supervisor.current_thumbnail(output_id, asset_id)
        except (ValueError, AssetUnavailable):
            return _error(404, "thumbnail_unavailable", "thumbnail is unavailable")
        except ImmichError:
            return _error(502, "thumbnail_upstream_error", "thumbnail is unavailable")
        if preview.content_type not in ALLOWED_IMAGE_TYPES:
            return _error(502, "thumbnail_invalid", "thumbnail is unavailable")
        return web.Response(body=preview.body, content_type=preview.content_type)

    app.router.add_get("/", index)
    app.router.add_get("/app.js", script)
    app.router.add_get("/styles.css", stylesheet)
    app.router.add_get("/favicon.svg", favicon)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/config", config)
    app.router.add_put("/api/config", save)
    app.router.add_post("/api/discovery", discovery)
    app.router.add_post("/api/outputs/{output_id}/controls/{command}", control)
    app.router.add_post("/api/outputs/{output_id}/reconnect", reconnect)
    app.router.add_post("/api/outputs/{output_id}/source", source)
    app.router.add_post("/api/outputs/{output_id}/seek", seek)
    app.router.add_get("/api/outputs/{output_id}/history", history)
    app.router.add_get("/api/outputs/{output_id}/history/{event_id}/thumbnail", thumbnail)
    app.router.add_get("/api/outputs/{output_id}/upcoming/{asset_id}/thumbnail", upcoming_thumbnail)
    app.router.add_get("/api/outputs/{output_id}/current/{asset_id}/thumbnail", current_thumbnail)
    # Singleton aliases keep existing dashboards operational during migration.
    app.router.add_post("/api/controls/{command}", control)
    app.router.add_post("/api/reconnect", reconnect)
    app.router.add_get("/api/albums", albums)
    app.router.add_get("/api/people", people)
    app.router.add_post("/api/source", source)
    app.router.add_post("/api/seek", seek)
    app.router.add_get("/api/history", history)
    app.router.add_get("/api/history/{event_id}/thumbnail", thumbnail)
    app.router.add_get("/api/upcoming/{asset_id}/thumbnail", upcoming_thumbnail)
    app.router.add_get("/api/current/{asset_id}/thumbnail", current_thumbnail)
    return app


@dataclass(slots=True)
class ManagementServer:
    supervisor: RuntimeSupervisor
    host: str = "127.0.0.1"
    port: int = 8080
    _runner: web.AppRunner | None = None

    @property
    def app(self) -> web.Application:
        runner = self._runner
        if runner is None:
            raise RuntimeError("management server has not been started")
        return runner.app

    async def start(self) -> None:
        if self._runner is not None:
            return
        allowed = None if self.host in {"0.0.0.0", "::"} else {self.host}
        if self.host in {"127.0.0.1", "::1", "localhost"}:
            allowed = {"127.0.0.1", "::1", "localhost"}
        runner = web.AppRunner(
            create_management_app(self.supervisor, allowed_hosts=allowed), access_log=None
        )
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner

    async def close(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()


def _mutation_failure(
    request: web.Request, csrf_token: str, allowed_hosts: set[str] | None
) -> web.Response | None:
    if not _origin_is_same(request, allowed_hosts):
        return _error(403, "invalid_origin", "same-origin request required")
    if request.content_type != "application/json":
        return _error(415, "invalid_content_type", "application/json required")
    if request.headers.get(MUTATION_HEADER) != "1":
        return _error(403, "invalid_request_header", "request header required")
    if not secrets.compare_digest(request.headers.get(CSRF_HEADER, ""), csrf_token):
        return _error(403, "invalid_csrf", "valid CSRF token required")
    return None


def _request_origin(request: web.Request, allowed_hosts: set[str] | None) -> str | None:
    try:
        host = request.host
    except ValueError:
        return None
    if not host or any(character in host for character in "/\\@"):
        return None
    hostname = urlsplit(f"//{host}").hostname
    if hostname is None or not _host_allowed(hostname, allowed_hosts):
        return None
    return f"{request.scheme}://{host}"


def _origin_is_same(request: web.Request, allowed_hosts: set[str] | None) -> bool:
    origin = request.headers.get("Origin")
    expected = _request_origin(request, allowed_hosts)
    if origin is None or expected is None or origin != expected:
        return False
    parsed = urlsplit(origin)
    return bool(
        parsed.scheme
        and parsed.netloc
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def _origin_is_absent_or_same(request: web.Request, allowed_hosts: set[str] | None) -> bool:
    return (
        "Origin" not in request.headers and _request_origin(request, allowed_hosts) is not None
    ) or _origin_is_same(request, allowed_hosts)


def _host_allowed(hostname: str, allowed_hosts: set[str] | None) -> bool:
    normalized = hostname.rstrip(".").lower()
    if allowed_hosts is not None:
        return normalized in {host.rstrip(".").lower() for host in allowed_hosts}
    if normalized == "localhost":
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


async def _json_object(request: web.Request) -> dict[str, Any] | web.Response:
    try:
        value = await request.json()
    except (ValueError, UnicodeError):
        return _error(400, "invalid_json", "request body must be a JSON object")
    if not isinstance(value, dict):
        return _error(400, "invalid_json", "request body must be a JSON object")
    return value


def _runtime_json(snapshot: RuntimeSnapshot) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": snapshot.mode.value,
        "revision": snapshot.revision,
        "generation": snapshot.generation,
        "error": snapshot.error,
        "outputs": [
            _output_json(output, snapshot.mode.value == "active") for output in snapshot.outputs
        ],
    }
    if len(snapshot.outputs) == 1:
        payload.update(_legacy_output_json(snapshot.outputs[0], snapshot.mode.value == "active"))
    return payload


def _output_json(output: OutputSnapshot, active: bool) -> dict[str, Any]:
    return {
        "id": output.id,
        "name": output.name,
        "receiver": {"uuid": str(output.receiver_uuid)},
        **_legacy_output_json(output, active),
    }


def _legacy_output_json(output: OutputSnapshot, active: bool) -> dict[str, Any]:
    coordinator = output.coordinator
    owned = active and coordinator.state.value == "owned"
    stoppable = active and coordinator.state.value in {"owned", "protected"}
    rotation_enabled = coordinator.rotation_enabled
    return {
        "coordinator": _coordinator_json(coordinator),
        "autocast_enabled": coordinator.autocast_enabled,
        "autocast_remaining_seconds": (
            max(0.0, coordinator.autocast_deadline - time.monotonic())
            if coordinator.autocast_deadline is not None
            else None
        ),
        "source": _source_json(
            PhotoSource(
                coordinator.source_kind,
                coordinator.selected_album or coordinator.selected_person,
                coordinator.search_query,
            )
        ),
        "available_actions": {
            "pause": active and rotation_enabled,
            "enable": active and not rotation_enabled,
            "next": owned,
            "stop": stoppable,
            "reconnect": active,
            "autocast_enable": (active and not coordinator.autocast_enabled),
            "autocast_disable": active and coordinator.autocast_enabled,
        },
    }


def _coordinator_json(snapshot: CoordinatorSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "state": snapshot.state.value,
        "rotation_enabled": snapshot.rotation_enabled,
        "generation": snapshot.generation,
        "error": snapshot.error,
        "health": snapshot.health.value,
        "health_reason": snapshot.health_reason,
        "health_message": snapshot.health_message,
        "retry_remaining_seconds": (
            max(0.0, snapshot.retry_deadline - time.monotonic())
            if snapshot.retry_deadline is not None
            else None
        ),
        "last_display_event_id": (
            snapshot.last_display.event_id if snapshot.last_display is not None else None
        ),
        "selected_album_id": (
            str(snapshot.selected_album) if snapshot.selected_album is not None else None
        ),
    }


def _source_json(source: PhotoSource) -> dict[str, str | None]:
    return {
        "kind": source.kind.value,
        "id": str(source.id) if source.id is not None else None,
        "query": source.query,
    }


def _config_json(snapshot: ConfigSnapshot) -> dict[str, Any]:
    return {
        "revision": snapshot.revision,
        "values": copy.deepcopy(snapshot.form_values),
        "api_key_configured": snapshot.api_key_configured,
        "api_key_source": snapshot.api_key_source.value if snapshot.api_key_source else None,
    }


def _safe_draft(values: dict[str, Any]) -> dict[str, Any]:
    draft: dict[str, Any] = {}
    schema = default_form_values()
    for section, fields in schema.items():
        submitted = values.get(section)
        if section == "outputs" and isinstance(submitted, list):
            allowed = set(fields[0])
            draft[section] = [
                {key: copy.deepcopy(value[key]) for key in allowed if key in value}
                for value in submitted
                if isinstance(value, dict)
            ]
            continue
        if not isinstance(submitted, dict):
            continue
        draft[section] = {key: copy.deepcopy(submitted[key]) for key in fields if key in submitted}
    draft.setdefault("immich", {})["api_key"] = ""
    return draft


def _record_json(output_id: str, record: DisplayRecord) -> dict[str, str]:
    return {
        "event_id": record.event_id,
        "asset_id": record.asset_id,
        "confirmed_at": _isoformat(record.confirmed_at),
        "thumbnail_url": f"/api/outputs/{output_id}/history/{record.event_id}/thumbnail",
    }


def _upcoming_json(output_id: str, asset_id: UUID) -> dict[str, str]:
    value = str(asset_id)
    return {
        "asset_id": value,
        "thumbnail_url": f"/api/outputs/{output_id}/upcoming/{value}/thumbnail",
    }


def _current_json(
    output_id: str, asset_id: UUID, record: DisplayRecord | None
) -> dict[str, str | None]:
    value = str(asset_id)
    return {
        "asset_id": value,
        "confirmed_at": _isoformat(record.confirmed_at) if record is not None else None,
        "thumbnail_url": f"/api/outputs/{output_id}/current/{value}/thumbnail",
    }


def _request_output_id(request: web.Request, snapshot: RuntimeSnapshot) -> str | None:
    requested = request.match_info.get("output_id")
    if requested is not None:
        return requested if any(output.id == requested for output in snapshot.outputs) else None
    return snapshot.outputs[0].id if len(snapshot.outputs) == 1 else None


def _isoformat(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _error(status: int, outcome: str, message: str) -> web.Response:
    return web.json_response({"outcome": outcome, "error": message}, status=status)


def _static_response(name: str, content_type: str) -> web.Response:
    body = files("cast_immich").joinpath("static", name).read_bytes()
    return web.Response(body=body, content_type=content_type)
