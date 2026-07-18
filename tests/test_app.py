from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from cast_immich.app import JsonFormatter, run_from_path


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

    class FakeManagement:
        def __init__(self, _supervisor: object, host: str, port: int) -> None:
            assert (host, port) == ("127.0.0.2", 9080)

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
