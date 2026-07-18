from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


class HistoryError(RuntimeError):
    """Persistent history state is malformed or unavailable."""


@dataclass(frozen=True, slots=True)
class DisplayRecord:
    event_id: str
    load_id: str
    asset_id: str
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class HistoryState:
    rotation_enabled: bool = True
    records: tuple[DisplayRecord, ...] = ()
    version: int = 1


class HistoryStore:
    MAX_RECORDS = 10
    VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def load(self) -> HistoryState:
        with self._lock:
            return self._load_unlocked()

    def set_rotation_enabled(self, enabled: bool) -> HistoryState:
        if not isinstance(enabled, bool):
            raise HistoryError("rotation_enabled must be a boolean")
        with self._lock:
            current = self._load_unlocked()
            state = HistoryState(rotation_enabled=enabled, records=current.records)
            self._write_unlocked(state)
            return state

    def record_display(
        self,
        load_id: str,
        asset_id: str,
        *,
        confirmed_at: datetime | None = None,
        event_id: str | None = None,
    ) -> DisplayRecord:
        if not load_id or not asset_id:
            raise HistoryError("display record IDs must not be blank")
        confirmation = datetime.now(UTC) if confirmed_at is None else confirmed_at
        if confirmation.tzinfo is None or confirmation.utcoffset() is None:
            raise HistoryError("confirmed_at must include a timezone")
        confirmation = confirmation.astimezone(UTC)
        with self._lock:
            current = self._load_unlocked()
            for record in current.records:
                if record.load_id == load_id:
                    return record
            record = DisplayRecord(
                event_id=event_id or str(uuid4()),
                load_id=load_id,
                asset_id=asset_id,
                confirmed_at=confirmation,
            )
            records = sorted(
                (*current.records, record), key=lambda item: item.confirmed_at, reverse=True
            )
            state = HistoryState(
                rotation_enabled=current.rotation_enabled,
                records=tuple(records[: self.MAX_RECORDS]),
            )
            self._write_unlocked(state)
            return record

    def _load_unlocked(self) -> HistoryState:
        if not self._path.exists():
            return HistoryState()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != self.VERSION:
                raise ValueError("unsupported version")
            enabled = raw.get("rotation_enabled")
            records_data = raw.get("records")
            if not isinstance(enabled, bool) or not isinstance(records_data, list):
                raise ValueError("invalid state fields")
            records = tuple(self._parse_record(item) for item in records_data)
            if len(records) > self.MAX_RECORDS:
                raise ValueError("too many display records")
            if len({record.load_id for record in records}) != len(records):
                raise ValueError("duplicate load IDs")
            if tuple(sorted(records, key=lambda item: item.confirmed_at, reverse=True)) != records:
                raise ValueError("display records are not newest first")
            return HistoryState(rotation_enabled=enabled, records=records)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise HistoryError("cannot read history state") from None

    def _parse_record(self, value: Any) -> DisplayRecord:
        if not isinstance(value, dict):
            raise ValueError("invalid display record")
        event_id = value["event_id"]
        load_id = value["load_id"]
        asset_id = value["asset_id"]
        confirmed_value = value["confirmed_at"]
        record_values = (event_id, load_id, asset_id, confirmed_value)
        if not all(isinstance(item, str) and item for item in record_values):
            raise ValueError("invalid display record")
        confirmed_at = datetime.fromisoformat(confirmed_value)
        if confirmed_at.tzinfo is None or confirmed_at.utcoffset() is None:
            raise ValueError("confirmation time has no timezone")
        return DisplayRecord(event_id, load_id, asset_id, confirmed_at.astimezone(UTC))

    def _write_unlocked(self, state: HistoryState) -> None:
        document = {
            "version": self.VERSION,
            "rotation_enabled": state.rotation_enabled,
            "records": [
                {
                    "event_id": record.event_id,
                    "load_id": record.load_id,
                    "asset_id": record.asset_id,
                    "confirmed_at": record.confirmed_at.astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
                for record in state.records
            ],
        }
        temporary: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent
            )
            temporary = Path(name)
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(document, output, separators=(",", ":"), sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
            temporary = None
        except OSError:
            raise HistoryError("cannot persist history state") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
