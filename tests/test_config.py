from __future__ import annotations

import os
from pathlib import Path

import pytest

from cast_immich.config import (
    ConfigConflictError,
    ConfigError,
    ConfigPersistenceError,
    SecretSource,
    default_form_values,
    load_editable_settings,
    load_settings,
    persist_settings,
    prepare_settings,
    restore_settings,
    save_settings,
)


def config_text(*, host: str = "192.168.1.5", port: int | float = 8787) -> str:
    return f'''\
[immich]
url = "https://photos.example/"
api_key = "file-secret"
[chromecast]
uuid = "12345678-1234-4234-8234-123456789abc"
[relay]
advertised_host = "{host}"
port = {port}
[rotation]
interval = 30
[service]
installation_id_file = "identity"
'''


def test_loads_normalized_settings_and_environment_secret(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")

    settings = load_settings(path, {"CAST_IMMICH_API_KEY": "environment-secret"})

    assert settings.immich.url == "https://photos.example"
    assert settings.immich.api_key == "environment-secret"
    assert settings.relay.advertised_base_url == "http://192.168.1.5:8787"
    assert settings.service.installation_id_file == tmp_path / "identity"
    assert settings.outputs[0].rotation.video_max_duration == 30
    assert settings.outputs[0].rotation.video_muted is True
    assert settings.outputs[0].rotation.show_web_qr is False
    assert settings.outputs[0].rotation.web_qr_size == 2
    assert settings.outputs[0].rotation.web_qr_position == "bottom-left"
    assert settings.outputs[0].rotation.web_qr_inset_x == 36
    assert settings.outputs[0].rotation.web_qr_inset_y == 36
    assert settings.outputs[0].rotation.web_qr_opacity == 75


def test_rejects_non_boolean_video_muting(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text().replace("interval = 30", 'interval = 30\nvideo_muted = "yes"'))
    with pytest.raises(ConfigError, match="video_muted must be a boolean"):
        load_settings(path)


@pytest.mark.parametrize("size", [0, 0.5, 7])
def test_rejects_invalid_web_qr_size(tmp_path: Path, size: int | float) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text().replace("interval = 30", f"interval = 30\nweb_qr_size = {size}"))
    with pytest.raises(ConfigError, match="web_qr_size"):
        load_settings(path)


def test_accepts_fractional_web_qr_size(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text().replace("interval = 30", "interval = 30\nweb_qr_size = 1.5"))

    assert load_settings(path).outputs[0].rotation.web_qr_size == 1.5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("web_qr_position", '"center"'),
        ("web_qr_inset_x", -1),
        ("web_qr_inset_x", 641),
        ("web_qr_inset_y", 361),
        ("web_qr_inset_y", 1.5),
    ],
)
def test_rejects_invalid_web_qr_placement(
    tmp_path: Path, field: str, value: str | int | float
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text().replace("interval = 30", f"interval = 30\n{field} = {value}"))
    with pytest.raises(ConfigError, match=field):
        load_settings(path)


@pytest.mark.parametrize("opacity", [49, 101, 75.5])
def test_rejects_invalid_web_qr_opacity(tmp_path: Path, opacity: int | float) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        config_text().replace("interval = 30", f"interval = 30\nweb_qr_opacity = {opacity}")
    )
    with pytest.raises(ConfigError, match="web_qr_opacity"):
        load_settings(path)


def test_installation_identity_persists(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    first = load_settings(path)
    second = load_settings(path)
    assert first.service.installation_id == second.service.installation_id


def test_installation_identity_creation_fsyncs_file_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    synced: list[Path] = []
    real_fsync = os.fsync

    def track_fsync(descriptor: int) -> None:
        synced.append(Path(f"/proc/self/fd/{descriptor}").resolve())
        real_fsync(descriptor)

    monkeypatch.setattr("cast_immich.config.os.fsync", track_fsync)
    load_settings(path)

    assert tmp_path in synced
    assert any(item.parent == tmp_path and item.name.startswith(".identity.") for item in synced)
    assert (tmp_path / "identity").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "0.0.0.0", "localhost", "bad host"])
def test_rejects_unreachable_advertised_hosts(tmp_path: Path, host: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(host=host), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"relay\.advertised_host"):
        load_settings(path)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_rejects_invalid_ports(tmp_path: Path, port: int) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(port=port), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"relay\.port"):
        load_settings(path)


def test_rejects_fractional_integer_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(port=0.5), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"relay\.port"):
        load_settings(path)


def test_errors_never_include_api_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    secret = "super-secret-value"
    path.write_text(
        config_text().replace("file-secret", secret).replace("interval = 30", "interval = 0")
    )
    with pytest.raises(ConfigError) as raised:
        load_settings(path)
    assert secret not in str(raised.value)


def test_editable_settings_round_trip_every_value_and_mask_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    document = load_editable_settings(path, {})

    assert document.revision == 0
    assert document.api_key_configured is True
    assert document.api_key_source is SecretSource.FILE
    assert document.form_values["immich"]["api_key"] == ""
    assert "file-secret" not in repr(document)

    form = document.form_values
    form["immich"]["url"] = "https://new.example///"
    form["immich"]["api_key"] = "new-origin-secret"
    form["immich"]["request_timeout"] = 7.5
    form["immich"]["retry_attempts"] = 4
    form["outputs"][0]["discovery_timeout"] = 8.5
    form["outputs"][0]["load_timeout"] = 9.5
    form["relay"].update(
        bind_host="127.0.0.1",
        port=9876,
        advertised_host="192.168.1.9",
        token_lifetime=90.0,
        max_response_bytes=123456,
        max_concurrent=2,
    )
    form["outputs"][0].update(
        interval=45.0,
        idle_debounce=2.0,
        cooldown=12.0,
        recent_history=17,
        candidate_batch=31,
        show_web_qr=True,
        web_qr_size=3.5,
        web_qr_position="top-right",
        web_qr_inset_x=72,
        web_qr_inset_y=54,
        web_qr_opacity=60,
    )
    form["service"]["log_level"] = "debug"

    saved = save_settings(path, form, expected_revision=0, environ={})
    reloaded = load_editable_settings(path, {})

    assert saved.revision == reloaded.revision == 1
    assert reloaded.settings == saved.settings
    assert reloaded.settings.immich.url == "https://new.example"
    assert reloaded.settings.immich.api_key == "new-origin-secret"
    assert reloaded.settings.service.log_level == "DEBUG"
    assert reloaded.settings.outputs[0].rotation.show_web_qr is True
    assert reloaded.form_values["outputs"][0]["show_web_qr"] is True
    assert reloaded.settings.outputs[0].rotation.web_qr_size == 3.5
    assert reloaded.settings.outputs[0].rotation.web_qr_position == "top-right"
    assert reloaded.settings.outputs[0].rotation.web_qr_inset_x == 72
    assert reloaded.settings.outputs[0].rotation.web_qr_inset_y == 54
    assert reloaded.settings.outputs[0].rotation.web_qr_opacity == 60
    assert "show_web_qr = true" in path.read_text(encoding="utf-8")
    assert "web_qr_size = 3.5" in path.read_text(encoding="utf-8")
    assert 'web_qr_position = "top-right"' in path.read_text(encoding="utf-8")
    assert "web_qr_opacity = 60" in path.read_text(encoding="utf-8")
    assert "[[outputs]]" in path.read_text(encoding="utf-8")
    assert "[chromecast]" not in path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o600


def test_blank_api_key_preserves_file_secret(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    before = load_editable_settings(path, {})

    save_settings(path, before.form_values, expected_revision=before.revision, environ={})

    assert load_settings(path, {}).immich.api_key == "file-secret"
    assert "file-secret" in path.read_text(encoding="utf-8")


def test_changing_immich_origin_requires_replacement_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    document = load_editable_settings(path, {})
    document.form_values["immich"]["url"] = "https://attacker.example"

    with pytest.raises(ConfigError, match="requires a replacement API key"):
        save_settings(path, document.form_values, expected_revision=0, environ={})

    assert load_settings(path, {}).immich.url == "https://photos.example"


@pytest.mark.parametrize("host", ["bad/host", "host:123", "https://host", "user@host"])
def test_rejects_non_authority_relay_hosts(tmp_path: Path, host: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(host=host), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"relay\.advertised_host"):
        load_settings(path)


def test_rejects_invalid_log_level(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        config_text().replace('installation_id_file = "identity"', 'log_level = "LOUD"')
    )
    with pytest.raises(ConfigError, match=r"service\.log_level"):
        load_settings(path)


def test_invalid_existing_config_never_reuses_its_stored_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[immich]\nurl = "https://old.example"\napi_key = "must-not-move"\n',
        encoding="utf-8",
    )
    candidate = default_form_values()
    candidate["immich"]["url"] = "https://new.example"
    candidate["outputs"][0]["uuid"] = "12345678-1234-4234-8234-123456789abc"
    candidate["relay"]["advertised_host"] = "192.168.1.5"

    with pytest.raises(ConfigError, match=r"immich\.api_key"):
        save_settings(path, candidate, expected_revision=0, environ={})

    assert "must-not-move" in path.read_text(encoding="utf-8")


def test_setup_identity_path_cannot_escape_through_symlink(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    candidate = default_form_values()
    candidate["immich"].update(url="https://photos.example", api_key="new-secret")
    candidate["outputs"][0]["uuid"] = "12345678-1234-4234-8234-123456789abc"
    candidate["relay"]["advertised_host"] = "192.168.1.5"
    candidate["service"]["installation_id_file"] = "escape/identity"

    with pytest.raises(ConfigError, match="must stay within"):
        save_settings(path, candidate, expected_revision=0, environ={})

    assert not (outside / "identity").exists()


def test_nonblank_api_key_replaces_file_secret_but_remains_masked(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    document = load_editable_settings(path, {})
    document.form_values["immich"]["api_key"] = "replacement-secret"

    saved = save_settings(path, document.form_values, expected_revision=0, environ={})

    assert saved.form_values["immich"]["api_key"] == ""
    assert "replacement-secret" not in repr(saved)
    assert load_settings(path, {}).immich.api_key == "replacement-secret"


def test_environment_key_is_authoritative_and_cannot_be_replaced(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    environment = {"CAST_IMMICH_API_KEY": "environment-secret"}
    document = load_editable_settings(path, environment)
    before = path.read_bytes()

    assert document.api_key_source is SecretSource.ENVIRONMENT
    assert document.api_key_configured is True
    document.form_values["immich"]["api_key"] = "proposed-secret"
    with pytest.raises(ConfigError, match="environment-managed") as raised:
        save_settings(path, document.form_values, expected_revision=0, environ=environment)

    assert path.read_bytes() == before
    assert "proposed-secret" not in str(raised.value)
    assert "environment-secret" not in str(raised.value)


def test_invalid_candidate_and_stale_revision_do_not_modify_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    document = load_editable_settings(path, {})
    before = path.read_bytes()
    document.form_values["relay"]["port"] = 0

    with pytest.raises(ConfigError):
        save_settings(path, document.form_values, expected_revision=0, environ={})
    assert path.read_bytes() == before

    valid = load_editable_settings(path, {}).form_values
    save_settings(path, valid, expected_revision=0, environ={})
    after = path.read_bytes()
    with pytest.raises(ConfigConflictError):
        save_settings(path, valid, expected_revision=0, environ={})
    assert path.read_bytes() == after


def test_replace_failure_preserves_previous_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    document = load_editable_settings(path, {})
    before = path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("cast_immich.config.os.replace", fail_replace)
    with pytest.raises(ConfigPersistenceError, match="cannot persist configuration"):
        save_settings(path, document.form_values, expected_revision=0, environ={})

    assert path.read_bytes() == before


def test_configuration_write_fsyncs_file_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    document = load_editable_settings(path, {})
    synced: list[Path] = []
    real_fsync = os.fsync

    def track_fsync(descriptor: int) -> None:
        synced.append(Path(f"/proc/self/fd/{descriptor}").resolve())
        real_fsync(descriptor)

    monkeypatch.setattr("cast_immich.config.os.fsync", track_fsync)
    save_settings(path, document.form_values, expected_revision=0, environ={})

    assert tmp_path in synced
    assert any(item.parent == tmp_path and item.name.startswith(".config.toml.") for item in synced)


def test_rollback_never_overwrites_a_newer_external_write(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(), encoding="utf-8")
    document = load_editable_settings(path, {})
    candidate = prepare_settings(path, document.form_values, expected_revision=0, environ={})
    persist_settings(candidate)
    path.write_text(path.read_text(encoding="utf-8") + "# external revision\n", encoding="utf-8")

    with pytest.raises(ConfigConflictError, match="changed during rollback"):
        restore_settings(candidate)

    assert path.read_text(encoding="utf-8").endswith("# external revision\n")


def test_legacy_config_loads_as_default_output_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = config_text()
    path.write_text(original, encoding="utf-8")

    document = load_editable_settings(path, {})

    assert document.settings.outputs[0].id == "default"
    assert document.settings.outputs[0].name == "Chromecast"
    assert document.form_values["outputs"][0]["uuid"] == "12345678-1234-4234-8234-123456789abc"
    assert path.read_text(encoding="utf-8") == original


def test_rejects_mixed_duplicate_and_invalid_outputs(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    base = (
        config_text()
        .replace(
            '[chromecast]\nuuid = "12345678-1234-4234-8234-123456789abc"\n',
            '[[outputs]]\nid = "living-room"\nname = "Living Room"\n'
            'uuid = "12345678-1234-4234-8234-123456789abc"\n',
        )
        .replace("[rotation]\ninterval = 30\n", "")
    )
    path.write_text(base + "\n[chromecast]\nuuid = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'\n")
    with pytest.raises(ConfigError, match="cannot mix"):
        load_settings(path, {})

    path.write_text(base + base[base.index("[[outputs]]") : base.index("[relay]")])
    with pytest.raises(ConfigError, match="unique"):
        load_settings(path, {})

    path.write_text(base.replace('id = "living-room"', 'id = "not/a/path"'))
    with pytest.raises(ConfigError, match="URL-safe"):
        load_settings(path, {})
