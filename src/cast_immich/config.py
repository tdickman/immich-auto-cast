from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Never
from urllib.parse import urlparse
from uuid import UUID, uuid4


class ConfigError(ValueError):
    """Configuration is missing or unsafe."""


class ConfigConflictError(ConfigError):
    """The configuration changed after the caller read it."""


class ConfigPersistenceError(ConfigError):
    """A validated configuration could not be persisted."""


class SecretSource(StrEnum):
    FILE = "file"
    ENVIRONMENT = "environment"


LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass(frozen=True, slots=True)
class ImmichSettings:
    url: str
    api_key: str = field(repr=False)
    request_timeout: float
    retry_attempts: int


@dataclass(frozen=True, slots=True)
class ChromecastSettings:
    uuid: UUID
    discovery_timeout: float
    load_timeout: float


@dataclass(frozen=True, slots=True)
class RelaySettings:
    bind_host: str
    port: int
    advertised_host: str
    token_lifetime: float
    max_response_bytes: int
    max_concurrent: int

    @property
    def advertised_base_url(self) -> str:
        host = f"[{self.advertised_host}]" if ":" in self.advertised_host else self.advertised_host
        return f"http://{host}:{self.port}"


@dataclass(frozen=True, slots=True)
class RotationSettings:
    interval: float
    idle_debounce: float
    cooldown: float
    recent_history: int
    candidate_batch: int
    autocast_delay: float = 30.0
    video_max_duration: float = 30.0
    video_muted: bool = True
    show_web_qr: bool = False
    web_qr_size: int = 1
    web_qr_position: str = "bottom-left"
    web_qr_inset_x: int = 36
    web_qr_inset_y: int = 36


@dataclass(frozen=True, slots=True)
class OutputSettings:
    id: str
    name: str
    chromecast: ChromecastSettings
    rotation: RotationSettings


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    installation_id_file: Path
    installation_id: UUID
    log_level: str


@dataclass(frozen=True, slots=True)
class Settings:
    immich: ImmichSettings
    outputs: tuple[OutputSettings, ...]
    relay: RelaySettings
    service: ServiceSettings

    @property
    def chromecast(self) -> ChromecastSettings:
        return self.outputs[0].chromecast

    @property
    def rotation(self) -> RotationSettings:
        return self.outputs[0].rotation


@dataclass(frozen=True, slots=True)
class SettingsDocument:
    settings: Settings = field(repr=False)
    revision: int
    form_values: dict[str, Any]
    api_key_configured: bool
    api_key_source: SecretSource


@dataclass(frozen=True, slots=True)
class SettingsCandidate:
    """Validated configuration ready for an optimistic atomic write."""

    document: SettingsDocument
    path: Path
    content: str = field(repr=False)
    previous_content: bytes | None = field(repr=False)


def default_form_values() -> dict[str, Any]:
    """Return a complete, secret-free form for first-run setup."""
    return {
        "immich": {
            "url": "",
            "api_key": "",
            "request_timeout": 15,
            "retry_attempts": 3,
        },
        "outputs": [
            {
                "id": "default",
                "name": "Chromecast",
                "uuid": "",
                "discovery_timeout": 10,
                "load_timeout": 15,
                "interval": 60,
                "idle_debounce": 3,
                "cooldown": 15,
                "recent_history": 25,
                "candidate_batch": 50,
                "autocast_delay": 30,
                "video_max_duration": 30,
                "video_muted": True,
                "show_web_qr": False,
                "web_qr_size": 1,
                "web_qr_position": "bottom-left",
                "web_qr_inset_x": 36,
                "web_qr_inset_y": 36,
            }
        ],
        "relay": {
            "bind_host": "0.0.0.0",
            "port": 8787,
            "advertised_host": "",
            "token_lifetime": 120,
            "max_response_bytes": 25_000_000,
            "max_concurrent": 4,
        },
        "service": {"installation_id_file": "installation-id", "log_level": "INFO"},
    }


def _fail(message: str) -> Never:
    raise ConfigError(message)


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        _fail(f"missing [{name}] configuration")
    return value


def _required(table: dict[str, Any], key: str, section: str) -> Any:
    value = table.get(key)
    if value is None or value == "":
        _fail(f"missing {section}.{key}")
    return value


def _positive(value: Any, name: str, *, integer: bool = False) -> float | int:
    expected = int if integer else int | float
    if isinstance(value, bool) or not isinstance(value, expected) or value <= 0:
        _fail(f"{name} must be positive")
    return value if integer else float(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{name} must be a boolean")
    return value


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("service.revision must be a non-negative integer")
    return value


def _load_installation_id(path: Path) -> UUID:
    temporary: Path | None = None
    try:
        if path.exists():
            return UUID(path.read_text(encoding="ascii").strip())
        identity = uuid4()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.write(f"{identity}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
        return identity
    except (OSError, ValueError) as error:
        raise ConfigError(f"cannot load service.installation_id_file: {error}") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _read_configuration(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read configuration: {error}") from None


def _parse_candidate(
    data: dict[str, Any],
    path: Path,
    environment: Mapping[str, str],
    installation_id: UUID,
) -> tuple[Settings, int, str | None, SecretSource]:
    immich = _table(data, "immich")
    relay = _table(data, "relay")
    service = _table(data, "service")

    raw_outputs = data.get("outputs")
    has_legacy = "chromecast" in data or "rotation" in data
    if raw_outputs is not None and has_legacy:
        _fail("cannot mix [[outputs]] with [chromecast] or [rotation]")
    if raw_outputs is None:
        cast = _table(data, "chromecast")
        rotation = _table(data, "rotation")
        output_tables: list[dict[str, Any]] = [
            {"id": "default", "name": "Chromecast", **cast, **rotation}
        ]
    elif not isinstance(raw_outputs, list) or not raw_outputs:
        _fail("outputs must be a non-empty list")
    elif not all(isinstance(item, dict) for item in raw_outputs):
        _fail("each output must be a table")
    else:
        output_tables = raw_outputs

    raw_url = str(_required(immich, "url", "immich")).rstrip("/")
    parsed_url = urlparse(raw_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        _fail("immich.url must be an absolute HTTP(S) URL")

    file_api_key_value = immich.get("api_key")
    file_api_key = (
        str(file_api_key_value)
        if file_api_key_value is not None and file_api_key_value != ""
        else None
    )
    environment_api_key = environment.get("CAST_IMMICH_API_KEY") or None
    if environment_api_key is not None:
        api_key = environment_api_key
        api_key_source = SecretSource.ENVIRONMENT
    elif file_api_key is not None:
        api_key = file_api_key
        api_key_source = SecretSource.FILE
    else:
        _fail("missing immich.api_key")

    advertised_host = str(_required(relay, "advertised_host", "relay"))
    try:
        address = ipaddress.ip_address(advertised_host)
    except ValueError:
        labels = advertised_host.rstrip(".").split(".")
        valid_hostname = bool(labels) and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels
        )
        if advertised_host.lower() == "localhost" or not valid_hostname:
            _fail("relay.advertised_host must be a valid LAN host")
    else:
        if address.is_loopback or address.is_unspecified:
            _fail("relay.advertised_host must be reachable from the Chromecast")
    port = _positive(relay.get("port", 8787), "relay.port", integer=True)
    if port > 65535:
        _fail("relay.port must be between 1 and 65535")

    identity_path = Path(str(service.get("installation_id_file", "installation-id")))
    if not identity_path.is_absolute():
        identity_path = path.parent / identity_path

    token_lifetime = float(_positive(relay.get("token_lifetime", 120), "relay.token_lifetime"))
    if token_lifetime > 600:
        _fail("relay.token_lifetime must not exceed 600 seconds")

    outputs: list[OutputSettings] = []
    output_ids: set[str] = set()
    cast_uuids: set[UUID] = set()
    for index, output in enumerate(output_tables):
        section = f"outputs[{index}]"
        output_id = str(_required(output, "id", section))
        if re.fullmatch(r"[A-Za-z0-9_-]+", output_id) is None:
            _fail(f"{section}.id must be URL-safe")
        if output_id in output_ids:
            _fail("output IDs must be unique")
        name = str(_required(output, "name", section)).strip()
        if not name:
            _fail(f"{section}.name must not be blank")
        try:
            cast_uuid = UUID(str(_required(output, "uuid", section)))
        except ValueError:
            _fail(f"{section}.uuid must be a valid UUID")
        if cast_uuid in cast_uuids:
            _fail("output Chromecast UUIDs must be unique")
        discovery_timeout = float(
            _positive(output.get("discovery_timeout", 10), f"{section}.discovery_timeout")
        )
        if discovery_timeout > 30:
            _fail(f"{section}.discovery_timeout must not exceed 30 seconds")
        web_qr_size = int(
            _positive(output.get("web_qr_size", 1), f"{section}.web_qr_size", integer=True)
        )
        if web_qr_size > 6:
            _fail(f"{section}.web_qr_size must be between 1 and 6")
        web_qr_position = str(output.get("web_qr_position", "bottom-left"))
        if web_qr_position not in {"top-left", "top-right", "bottom-left", "bottom-right"}:
            _fail(f"{section}.web_qr_position must be a valid corner")
        web_qr_insets: list[int] = []
        for key, maximum in (("web_qr_inset_x", 640), ("web_qr_inset_y", 360)):
            value = output.get(key, 36)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                _fail(f"{section}.{key} must be between 0 and {maximum}")
            web_qr_insets.append(value)
        outputs.append(
            OutputSettings(
                output_id,
                name,
                ChromecastSettings(
                    cast_uuid,
                    discovery_timeout,
                    float(_positive(output.get("load_timeout", 15), f"{section}.load_timeout")),
                ),
                RotationSettings(
                    float(_positive(output.get("interval", 60), f"{section}.interval")),
                    float(_positive(output.get("idle_debounce", 3), f"{section}.idle_debounce")),
                    float(_positive(output.get("cooldown", 15), f"{section}.cooldown")),
                    int(
                        _positive(
                            output.get("recent_history", 25),
                            f"{section}.recent_history",
                            integer=True,
                        )
                    ),
                    int(
                        _positive(
                            output.get("candidate_batch", 50),
                            f"{section}.candidate_batch",
                            integer=True,
                        )
                    ),
                    float(_positive(output.get("autocast_delay", 30), f"{section}.autocast_delay")),
                    float(
                        _positive(
                            output.get("video_max_duration", 30),
                            f"{section}.video_max_duration",
                        )
                    ),
                    _boolean(output.get("video_muted", True), f"{section}.video_muted"),
                    _boolean(output.get("show_web_qr", False), f"{section}.show_web_qr"),
                    web_qr_size,
                    web_qr_position,
                    web_qr_insets[0],
                    web_qr_insets[1],
                ),
            )
        )
        output_ids.add(output_id)
        cast_uuids.add(cast_uuid)
    log_level = str(service.get("log_level", "INFO")).upper()
    if log_level not in LOG_LEVELS:
        _fail("service.log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

    settings = Settings(
        immich=ImmichSettings(
            raw_url,
            api_key,
            float(_positive(immich.get("request_timeout", 15), "immich.request_timeout")),
            int(_positive(immich.get("retry_attempts", 3), "immich.retry_attempts", integer=True)),
        ),
        outputs=tuple(outputs),
        relay=RelaySettings(
            str(relay.get("bind_host", "0.0.0.0")),
            int(port),
            advertised_host,
            token_lifetime,
            int(
                _positive(
                    relay.get("max_response_bytes", 25_000_000),
                    "relay.max_response_bytes",
                    integer=True,
                )
            ),
            int(_positive(relay.get("max_concurrent", 4), "relay.max_concurrent", integer=True)),
        ),
        service=ServiceSettings(
            identity_path,
            installation_id,
            log_level,
        ),
    )
    return settings, _revision(service.get("revision", 0)), file_api_key, api_key_source


def _installation_path(data: dict[str, Any], path: Path) -> Path:
    service = _table(data, "service")
    identity_path = Path(str(service.get("installation_id_file", "installation-id")))
    return identity_path if identity_path.is_absolute() else path.parent / identity_path


def _form_values(settings: Settings, path: Path) -> dict[str, Any]:
    identity_path: str
    try:
        identity_path = str(settings.service.installation_id_file.relative_to(path.parent))
    except ValueError:
        identity_path = str(settings.service.installation_id_file)
    return {
        "immich": {
            "url": settings.immich.url,
            "api_key": "",
            "request_timeout": settings.immich.request_timeout,
            "retry_attempts": settings.immich.retry_attempts,
        },
        "outputs": [
            {
                "id": output.id,
                "name": output.name,
                "uuid": str(output.chromecast.uuid),
                "discovery_timeout": output.chromecast.discovery_timeout,
                "load_timeout": output.chromecast.load_timeout,
                "interval": output.rotation.interval,
                "idle_debounce": output.rotation.idle_debounce,
                "cooldown": output.rotation.cooldown,
                "recent_history": output.rotation.recent_history,
                "candidate_batch": output.rotation.candidate_batch,
                "autocast_delay": output.rotation.autocast_delay,
                "video_max_duration": output.rotation.video_max_duration,
                "video_muted": output.rotation.video_muted,
                "show_web_qr": output.rotation.show_web_qr,
                "web_qr_size": output.rotation.web_qr_size,
                "web_qr_position": output.rotation.web_qr_position,
                "web_qr_inset_x": output.rotation.web_qr_inset_x,
                "web_qr_inset_y": output.rotation.web_qr_inset_y,
            }
            for output in settings.outputs
        ],
        "relay": {
            "bind_host": settings.relay.bind_host,
            "port": settings.relay.port,
            "advertised_host": settings.relay.advertised_host,
            "token_lifetime": settings.relay.token_lifetime,
            "max_response_bytes": settings.relay.max_response_bytes,
            "max_concurrent": settings.relay.max_concurrent,
        },
        "service": {
            "installation_id_file": identity_path,
            "log_level": settings.service.log_level,
        },
    }


def load_editable_settings(path: Path, environ: dict[str, str] | None = None) -> SettingsDocument:
    environment = os.environ if environ is None else environ
    data = _read_configuration(path)
    settings, revision, file_api_key, source = _parse_candidate(
        data, path, environment, _load_installation_id(_installation_path(data, path))
    )
    return SettingsDocument(
        settings=settings,
        revision=revision,
        form_values=_form_values(settings, path),
        api_key_configured=file_api_key is not None or source is SecretSource.ENVIRONMENT,
        api_key_source=source,
    )


def load_settings(path: Path, environ: dict[str, str] | None = None) -> Settings:
    """Load settings with the historical environment and installation-ID behavior."""
    return load_editable_settings(path, environ).settings


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _serialize_configuration(
    settings: Settings, path: Path, revision: int, file_api_key: str | None
) -> str:
    values = _form_values(settings, path)
    lines: list[str] = []
    order = {
        "immich": ("url", "api_key", "request_timeout", "retry_attempts"),
        "relay": (
            "bind_host",
            "port",
            "advertised_host",
            "token_lifetime",
            "max_response_bytes",
            "max_concurrent",
        ),
        "service": ("installation_id_file", "log_level", "revision"),
    }
    values["immich"]["api_key"] = file_api_key
    values["service"]["revision"] = revision
    for section, keys in order.items():
        lines.append(f"[{section}]")
        for key in keys:
            value = values[section].get(key)
            if value is None:
                continue
            rendered = _toml_string(value) if isinstance(value, str) else str(value).lower()
            lines.append(f"{key} = {rendered}")
        lines.append("")
    output_keys = (
        "id",
        "name",
        "uuid",
        "discovery_timeout",
        "load_timeout",
        "interval",
        "idle_debounce",
        "autocast_delay",
        "cooldown",
        "recent_history",
        "candidate_batch",
        "video_max_duration",
        "video_muted",
        "show_web_qr",
        "web_qr_size",
        "web_qr_position",
        "web_qr_inset_x",
        "web_qr_inset_y",
    )
    for output in values["outputs"]:
        lines.append("[[outputs]]")
        for key in output_keys:
            value = output[key]
            rendered = _toml_string(value) if isinstance(value, str) else str(value).lower()
            lines.append(f"{key} = {rendered}")
        lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except OSError:
        raise ConfigPersistenceError("cannot persist configuration") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_settings(
    path: Path,
    form_values: dict[str, Any],
    *,
    expected_revision: int,
    environ: dict[str, str] | None = None,
) -> SettingsDocument:
    candidate = prepare_settings(
        path, form_values, expected_revision=expected_revision, environ=environ
    )
    persist_settings(candidate)
    return candidate.document


def prepare_settings(
    path: Path,
    form_values: dict[str, Any],
    *,
    expected_revision: int,
    environ: dict[str, str] | None = None,
) -> SettingsCandidate:
    """Validate a complete candidate without replacing the configuration file."""
    environment = os.environ if environ is None else environ
    try:
        previous_content = path.read_bytes()
    except FileNotFoundError:
        previous_content = None
    except OSError:
        raise ConfigError("cannot read configuration") from None

    current_file_key: str | None = None
    current_revision = 0
    current_data: dict[str, Any] | None = None
    if previous_content is not None:
        try:
            current_data = tomllib.loads(previous_content.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError):
            current_data = None
        if current_data is not None:
            raw_immich = current_data.get("immich")
            if isinstance(raw_immich, dict):
                raw_key = raw_immich.get("api_key")
                if raw_key is not None and raw_key != "":
                    current_file_key = str(raw_key)
            try:
                installation_path = _installation_path(current_data, path)
                installation_id = _load_installation_id(installation_path)
                _, current_revision, parsed_key, _ = _parse_candidate(
                    current_data, path, environment, installation_id
                )
                current_file_key = parsed_key
            except ConfigError:
                current_data = None
                current_file_key = None
    if current_data is not None and expected_revision != current_revision:
        raise ConfigConflictError("configuration revision is stale")
    if current_data is None and expected_revision != 0:
        raise ConfigConflictError("configuration revision is stale")

    candidate = copy.deepcopy(form_values)
    immich = _table(candidate, "immich")
    replacement_value = immich.get("api_key", "")
    replacement = str(replacement_value) if replacement_value is not None else ""
    environment_key = environment.get("CAST_IMMICH_API_KEY") or None
    current_url = None
    if current_data is not None:
        current_immich = current_data.get("immich")
        if isinstance(current_immich, dict) and current_immich.get("url"):
            current_url = str(current_immich["url"])
    candidate_url = str(immich.get("url", ""))
    if current_url is not None and _origin(current_url) != _origin(candidate_url):
        if environment_key is not None:
            raise ConfigError("immich.url cannot change while the API key is environment-managed")
        if not replacement:
            raise ConfigError("changing immich.url requires a replacement API key")
    if environment_key is not None and replacement:
        raise ConfigError("immich.api_key is environment-managed")
    file_api_key = replacement or current_file_key
    if file_api_key is None and environment_key is None:
        _fail("missing immich.api_key")
    if file_api_key is None:
        immich.pop("api_key", None)
    else:
        immich["api_key"] = file_api_key
    service = _table(candidate, "service")
    if current_data is not None:
        current_service = _table(current_data, "service")
        service["installation_id_file"] = current_service.get(
            "installation_id_file", "installation-id"
        )
    else:
        identity_value = Path(str(service.get("installation_id_file", "installation-id")))
        if identity_value.is_absolute() or ".." in identity_value.parts:
            raise ConfigError("service.installation_id_file must stay within the config directory")
        base = path.parent.resolve()
        resolved_identity = (base / identity_value).resolve()
        try:
            resolved_identity.relative_to(base)
        except ValueError:
            raise ConfigError(
                "service.installation_id_file must stay within the config directory"
            ) from None
    next_revision = expected_revision + 1
    service["revision"] = next_revision

    installation_path = _installation_path(candidate, path)
    if installation_path.exists():
        installation_id = _load_installation_id(installation_path)
    else:
        # Validate every candidate field before creating installation state.
        _parse_candidate(candidate, path, environment, UUID(int=0))
        installation_id = _load_installation_id(installation_path)
    settings, parsed_revision, _, source = _parse_candidate(
        candidate, path, environment, installation_id
    )
    content = _serialize_configuration(settings, path, parsed_revision, file_api_key)
    document = SettingsDocument(
        settings=settings,
        revision=next_revision,
        form_values=_form_values(settings, path),
        api_key_configured=file_api_key is not None or source is SecretSource.ENVIRONMENT,
        api_key_source=source,
    )
    return SettingsCandidate(document, path, content, previous_content)


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        raise ConfigError("immich.url contains an invalid port") from None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def persist_settings(candidate: SettingsCandidate) -> None:
    """Persist a prepared candidate if its source file has not changed."""
    try:
        current = candidate.path.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError:
        raise ConfigPersistenceError("cannot persist configuration") from None
    if current != candidate.previous_content:
        raise ConfigConflictError("configuration revision is stale")
    _atomic_write(candidate.path, candidate.content)


def restore_settings(candidate: SettingsCandidate) -> None:
    """Restore the exact pre-candidate file after a failed runtime commit."""
    try:
        current = candidate.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    except (OSError, UnicodeError):
        raise ConfigPersistenceError("cannot restore configuration") from None
    if current != candidate.content:
        raise ConfigConflictError("configuration changed during rollback")
    if candidate.previous_content is None:
        try:
            candidate.path.unlink()
            _fsync_directory(candidate.path.parent)
        except FileNotFoundError:
            pass
        except OSError:
            raise ConfigPersistenceError("cannot restore configuration") from None
        return
    try:
        content = candidate.previous_content.decode("utf-8")
    except UnicodeError:
        raise ConfigPersistenceError("cannot restore configuration") from None
    _atomic_write(candidate.path, content)
