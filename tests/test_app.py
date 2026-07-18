from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from cast_immich.app import JsonFormatter, _load_or_create_web_password, run_from_path


def test_structured_logs_contain_reason_without_credentials() -> None:
    record = logging.LogRecord("cast_immich", logging.INFO, "", 0, "state_changed", (), None)
    record.reason = "external_media"
    payload = json.loads(JsonFormatter().format(record))
    assert payload == {
        "level": "INFO",
        "logger": "cast_immich",
        "message": "state_changed",
        "reason": "external_media",
    }


def test_web_password_is_generated_once_or_uses_existing_value(tmp_path: Path) -> None:
    path = tmp_path / "web-password"

    generated = _load_or_create_web_password(path)
    reloaded = _load_or_create_web_password(path)
    path.write_text("chosen-password\n", encoding="utf-8")
    chosen = _load_or_create_web_password(path)

    assert generated == reloaded
    assert len(generated) >= 24
    assert chosen == "chosen-password"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_run_from_path_starts_and_closes_management_before_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class FakeSupervisor:
        def __init__(self, _path: Path) -> None:
            self.config_snapshot = None

        async def start(self) -> object:
            events.append("supervisor-start")
            return type("Snapshot", (), {"mode": type("Mode", (), {"value": "setup"})()})()

        async def close(self) -> None:
            events.append("supervisor-close")

        async def wait_for_failure(self) -> None:
            await __import__("asyncio").Event().wait()

    class FakeManagement:
        def __init__(self, _supervisor: object, password: str, host: str, port: int) -> None:
            assert (host, port) == ("127.0.0.2", 9080)
            assert password

        async def start(self) -> None:
            events.append("web-start")

        async def close(self) -> None:
            events.append("web-close")

    stop = __import__("asyncio").Event()
    stop.set()
    monkeypatch.setattr("cast_immich.app.RuntimeSupervisor", FakeSupervisor)
    monkeypatch.setattr("cast_immich.app.ManagementServer", FakeManagement)

    await run_from_path(tmp_path / "missing.toml", stop, web_host="127.0.0.2", web_port=9080)

    assert events == ["web-start", "supervisor-start", "web-close", "supervisor-close"]


@pytest.mark.asyncio
async def test_run_from_path_propagates_runtime_failure_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class FakeSupervisor:
        def __init__(self, _path: Path) -> None:
            self.config_snapshot = None

        async def start(self) -> object:
            return type("Snapshot", (), {"mode": type("Mode", (), {"value": "setup"})()})()

        async def wait_for_failure(self) -> None:
            raise RuntimeError("coordinator crashed")

        async def close(self) -> None:
            events.append("supervisor-close")

    class FakeManagement:
        def __init__(self, _supervisor: object, password: str, _host: str, _port: int) -> None:
            assert password
            pass

        async def start(self) -> None:
            events.append("web-start")

        async def close(self) -> None:
            events.append("web-close")

    monkeypatch.setattr("cast_immich.app.RuntimeSupervisor", FakeSupervisor)
    monkeypatch.setattr("cast_immich.app.ManagementServer", FakeManagement)

    with pytest.raises(RuntimeError, match="coordinator crashed"):
        await run_from_path(tmp_path / "config.toml")
    assert events == ["web-start", "web-close", "supervisor-close"]
