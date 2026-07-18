from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from pathlib import Path
from typing import Any

from .cast import CastAdapter
from .config import Settings
from .coordinator import Coordinator, CoordinatorEvent
from .immich import ImmichClient
from .relay import ImageRelay
from .runtime import RuntimeSupervisor
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

    queue: asyncio.Queue[CoordinatorEvent] = asyncio.Queue()
    immich = ImmichClient(settings.immich)
    relay = ImageRelay(settings.relay, immich)
    cast = CastAdapter(settings.chromecast, queue)  # type: ignore[arg-type]
    coordinator = Coordinator(
        queue,
        immich,
        relay,
        cast,
        settings.rotation,
        settings.service.installation_id,
        settings.chromecast.load_timeout,
    )
    coordinator_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[bool] | None = None
    try:
        await immich.start()
        await relay.start()
        coordinator_task = asyncio.create_task(coordinator.run(), name="coordinator")
        await cast.start()
        logger.info("service_started", extra={"reason": "startup_complete"})
        stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-signal")
        done, _ = await asyncio.wait(
            {coordinator_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if coordinator_task in done:
            await coordinator_task
    finally:
        await coordinator.close()
        if coordinator_task is not None and not coordinator_task.done():
            coordinator_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await coordinator_task
        await cast.close()
        await relay.close()
        await immich.close()
        for task in (coordinator_task, stop_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
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

    supervisor = RuntimeSupervisor(path)
    management = ManagementServer(supervisor, web_host, web_port)
    try:
        await management.start()
        snapshot = await supervisor.start()
        if snapshot.mode.value == "active":
            configure_logging(supervisor.config_snapshot.form_values["service"]["log_level"])
        await stop_event.wait()
    finally:
        await management.close()
        await supervisor.close()
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
