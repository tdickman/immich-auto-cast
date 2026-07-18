from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import tempfile
from pathlib import Path
from typing import Any

from .config import Settings
from .history import HistoryStore
from .runtime import RuntimeSupervisor, ServiceGraph
from .web import ManagementServer


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        reason = getattr(record, "reason", None)
        if reason is not None:
            value["reason"] = str(reason)
        for field in ("from_state", "to_state", "generation"):
            field_value = getattr(record, field, None)
            if field_value is not None:
                value[field] = str(field_value) if field != "generation" else field_value
        return json.dumps(value, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


async def run_service(settings: Settings, stop: asyncio.Event | None = None) -> None:
    logger = logging.getLogger("cast_immich")
    stop_event = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    if stop is None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signum, stop_event.set)
                installed_signals.append(signum)

    history = HistoryStore(settings.service.installation_id_file.with_name("state.json"))
    graph = ServiceGraph(settings, history)
    stop_task: asyncio.Task[bool] | None = None
    coordinator_task: asyncio.Task[None] | None = None
    try:
        await graph.stage()
        await graph.start()
        logger.info("service_started", extra={"reason": "startup_complete"})
        stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-signal")
        coordinator_task = asyncio.create_task(
            graph.wait_for_coordinator_exit(), name="coordinator-monitor"
        )
        done, _ = await asyncio.wait(
            {stop_task, coordinator_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if coordinator_task in done:
            await coordinator_task
    finally:
        for task in (stop_task, coordinator_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await graph.close()
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
        logger.info("service_stopped", extra={"reason": "shutdown_complete"})


async def run_from_path(
    path: Path,
    stop: asyncio.Event | None = None,
    *,
    web_host: str = "127.0.0.1",
    web_port: int = 8080,
) -> None:
    """Run the stable process lifecycle, including first-run setup mode."""
    configure_logging("INFO")
    stop_event = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    if stop is None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signum, stop_event.set)
                installed_signals.append(signum)

    password = _load_or_create_web_password(path.with_name("web-password"))
    supervisor = RuntimeSupervisor(path)
    configure_dashboard = getattr(supervisor, "set_dashboard_access", None)
    if configure_dashboard is not None:
        configure_dashboard(web_port, password)
    management = ManagementServer(supervisor, password, web_host, web_port)
    stop_task: asyncio.Task[bool] | None = None
    failure_task: asyncio.Task[None] | None = None
    try:
        await management.start()
        snapshot = await supervisor.start()
        if snapshot.mode.value == "active":
            configure_logging(supervisor.config_snapshot.form_values["service"]["log_level"])
        stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-signal")
        failure_task = asyncio.create_task(supervisor.wait_for_failure(), name="runtime-failure")
        done, _ = await asyncio.wait({stop_task, failure_task}, return_when=asyncio.FIRST_COMPLETED)
        if failure_task in done:
            await failure_task
    finally:
        for task in (stop_task, failure_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await management.close()
        await supervisor.close()
        for signum in installed_signals:
            loop.remove_signal_handler(signum)


def _load_or_create_web_password(path: Path) -> str:
    """Load an operator-provided password or atomically create a random one."""
    temporary: Path | None = None
    try:
        if path.exists():
            password = path.read_text(encoding="utf-8").strip()
            if password:
                path.chmod(0o600)
                return password
        path.parent.mkdir(parents=True, exist_ok=True)
        password = secrets.token_urlsafe(24)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(f"{password}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return password
    except OSError as error:
        raise RuntimeError(f"cannot load web password: {error}") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
