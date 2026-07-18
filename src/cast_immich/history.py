from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


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
    version: int = 3
    autocast_enabled: bool = True
    source_kind: str = "timeline"
    source_id: str | None = None
    source_query: str | None = None
    recent_asset_ids: tuple[str, ...] = ()


class OutputHistory:
    def __init__(self, store: HistoryStore, output_id: str) -> None:
        self._store = store
        self.output_id = output_id

    def load(self) -> HistoryState:
        return self._store._load_output(self.output_id)

    def set_rotation_enabled(self, enabled: bool) -> HistoryState:
        return self._store._set_enabled(self.output_id, "rotation_enabled", enabled)

    def set_autocast_enabled(self, enabled: bool) -> HistoryState:
        return self._store._set_enabled(self.output_id, "autocast_enabled", enabled)

    def set_source(
        self, kind: str, source_id: str | None = None, query: str | None = None
    ) -> HistoryState:
        return self._store._set_source(self.output_id, kind, source_id, query)

    def record_display(
        self,
        load_id: str,
        asset_id: str,
        *,
        confirmed_at: datetime | None = None,
        event_id: str | None = None,
    ) -> DisplayRecord:
        return self._store._record_display(
            self.output_id,
            load_id,
            asset_id,
            confirmed_at=confirmed_at,
            event_id=event_id,
        )


class HistoryStore:
    MAX_RECORDS = 10
    MAX_RECENT_ASSETS = 1000
    VERSION = 3
    SOURCE_KINDS = frozenset({"timeline", "album", "person", "search"})

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def for_output(self, output_id: str) -> OutputHistory:
        if not output_id:
            raise HistoryError("output_id must not be blank")
        return OutputHistory(self, output_id)

    # Compatibility wrappers for installations and callers that predate multi-output.
    def load(self) -> HistoryState:
        return self.for_output("default").load()

    def set_rotation_enabled(self, enabled: bool) -> HistoryState:
        return self.for_output("default").set_rotation_enabled(enabled)

    def set_autocast_enabled(self, enabled: bool) -> HistoryState:
        return self.for_output("default").set_autocast_enabled(enabled)

    def set_source(
        self, kind: str, source_id: str | None = None, query: str | None = None
    ) -> HistoryState:
        return self.for_output("default").set_source(kind, source_id, query)

    def record_display(
        self,
        load_id: str,
        asset_id: str,
        *,
        confirmed_at: datetime | None = None,
        event_id: str | None = None,
    ) -> DisplayRecord:
        return self.for_output("default").record_display(
            load_id, asset_id, confirmed_at=confirmed_at, event_id=event_id
        )

    def _load_output(self, output_id: str) -> HistoryState:
        with self._lock:
            return self._load_all_unlocked().get(output_id, HistoryState())

    def _set_enabled(self, output_id: str, field: str, enabled: bool) -> HistoryState:
        if not isinstance(enabled, bool):
            raise HistoryError(f"{field} must be a boolean")
        with self._lock:
            states = self._load_all_unlocked()
            current = states.get(output_id, HistoryState())
            state = HistoryState(
                rotation_enabled=enabled
                if field == "rotation_enabled"
                else current.rotation_enabled,
                records=current.records,
                autocast_enabled=enabled
                if field == "autocast_enabled"
                else current.autocast_enabled,
                source_kind=current.source_kind,
                source_id=current.source_id,
                source_query=current.source_query,
                recent_asset_ids=current.recent_asset_ids,
            )
            states[output_id] = state
            self._write_unlocked(states)
            return state

    def _set_source(
        self, output_id: str, kind: str, source_id: str | None, query: str | None
    ) -> HistoryState:
        source_kind, normalized_id, normalized_query = self._validate_source(kind, source_id, query)
        with self._lock:
            states = self._load_all_unlocked()
            current = states.get(output_id, HistoryState())
            state = HistoryState(
                rotation_enabled=current.rotation_enabled,
                records=current.records,
                autocast_enabled=current.autocast_enabled,
                source_kind=source_kind,
                source_id=normalized_id,
                source_query=normalized_query,
                recent_asset_ids=current.recent_asset_ids,
            )
            states[output_id] = state
            self._write_unlocked(states)
            return state

    def _record_display(
        self,
        output_id: str,
        load_id: str,
        asset_id: str,
        *,
        confirmed_at: datetime | None,
        event_id: str | None,
    ) -> DisplayRecord:
        if not load_id or not asset_id:
            raise HistoryError("display record IDs must not be blank")
        confirmation = datetime.now(UTC) if confirmed_at is None else confirmed_at
        if confirmation.tzinfo is None or confirmation.utcoffset() is None:
            raise HistoryError("confirmed_at must include a timezone")
        confirmation = confirmation.astimezone(UTC)
        with self._lock:
            states = self._load_all_unlocked()
            current = states.get(output_id, HistoryState())
            for record in current.records:
                if record.load_id == load_id:
                    return record
            record = DisplayRecord(event_id or str(uuid4()), load_id, asset_id, confirmation)
            records = sorted(
                (*current.records, record), key=lambda item: item.confirmed_at, reverse=True
            )
            recent_asset_ids = (
                asset_id,
                *(item for item in current.recent_asset_ids if item != asset_id),
            )
            states[output_id] = HistoryState(
                rotation_enabled=current.rotation_enabled,
                records=tuple(records[: self.MAX_RECORDS]),
                autocast_enabled=current.autocast_enabled,
                source_kind=current.source_kind,
                source_id=current.source_id,
                source_query=current.source_query,
                recent_asset_ids=recent_asset_ids[: self.MAX_RECENT_ASSETS],
            )
            self._write_unlocked(states)
            return record

    def _load_all_unlocked(self) -> dict[str, HistoryState]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("invalid document")
            version = raw.get("version")
            if isinstance(version, bool) or not isinstance(version, int):
                raise ValueError("invalid version")
            if version == 1:
                return {"default": self._parse_state(raw)}
            if version not in {2, self.VERSION} or not isinstance(raw.get("outputs"), dict):
                raise ValueError("unsupported version")
            return {
                output_id: self._parse_state(value)
                for output_id, value in raw["outputs"].items()
                if isinstance(output_id, str) and output_id
            }
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise HistoryError("cannot read history state") from None

    def _parse_state(self, raw: Any) -> HistoryState:
        if not isinstance(raw, dict):
            raise ValueError("invalid state")
        enabled = raw.get("rotation_enabled")
        autocast_enabled = raw.get("autocast_enabled", True)
        records_data = raw.get("records")
        if (
            not isinstance(enabled, bool)
            or not isinstance(autocast_enabled, bool)
            or not isinstance(records_data, list)
        ):
            raise ValueError("invalid state fields")
        records = tuple(self._parse_record(item) for item in records_data)
        if len(records) > self.MAX_RECORDS or len({item.load_id for item in records}) != len(
            records
        ):
            raise ValueError("invalid display records")
        if tuple(sorted(records, key=lambda item: item.confirmed_at, reverse=True)) != records:
            raise ValueError("display records are not newest first")
        source_kind, source_id, source_query = self._validate_source(
            raw.get("source_kind", "timeline"),
            raw.get("source_id"),
            raw.get("source_query"),
            error_type=ValueError,
        )
        recent_data = raw.get("recent_asset_ids", [])
        if (
            not isinstance(recent_data, list)
            or len(recent_data) > self.MAX_RECENT_ASSETS
            or not all(isinstance(item, str) and item.strip() for item in recent_data)
            or len(set(recent_data)) != len(recent_data)
        ):
            raise ValueError("invalid recent asset IDs")
        return HistoryState(
            rotation_enabled=enabled,
            records=records,
            autocast_enabled=autocast_enabled,
            source_kind=source_kind,
            source_id=source_id,
            source_query=source_query,
            recent_asset_ids=tuple(recent_data),
        )

    @classmethod
    def _validate_source(
        cls,
        kind: Any,
        source_id: Any,
        query: Any,
        *,
        error_type: type[Exception] = HistoryError,
    ) -> tuple[str, str | None, str | None]:
        if not isinstance(kind, str) or kind not in cls.SOURCE_KINDS:
            raise error_type("invalid photo source kind")
        if kind in {"album", "person"}:
            if not isinstance(source_id, str) or query is not None:
                raise error_type("invalid photo source fields")
            try:
                normalized_id = str(UUID(source_id))
            except ValueError:
                raise error_type("invalid photo source ID") from None
            return kind, normalized_id, None
        if kind == "search":
            if source_id is not None or not isinstance(query, str):
                raise error_type("invalid photo source fields")
            normalized_query = query.strip()
            if not normalized_query or len(normalized_query) > 200:
                raise error_type("invalid photo source query")
            return kind, None, normalized_query
        if source_id is not None or query is not None:
            raise error_type("invalid photo source fields")
        return kind, None, None

    @staticmethod
    def _parse_record(value: Any) -> DisplayRecord:
        if not isinstance(value, dict):
            raise ValueError("invalid display record")
        values = (value["event_id"], value["load_id"], value["asset_id"], value["confirmed_at"])
        if not all(isinstance(item, str) and item for item in values):
            raise ValueError("invalid display record")
        confirmed_at = datetime.fromisoformat(values[3])
        if confirmed_at.tzinfo is None or confirmed_at.utcoffset() is None:
            raise ValueError("confirmation time has no timezone")
        return DisplayRecord(values[0], values[1], values[2], confirmed_at.astimezone(UTC))

    def _write_unlocked(self, states: dict[str, HistoryState]) -> None:
        document = {
            "version": self.VERSION,
            "outputs": {
                output_id: {
                    "rotation_enabled": state.rotation_enabled,
                    "autocast_enabled": state.autocast_enabled,
                    "source_kind": state.source_kind,
                    "source_id": state.source_id,
                    "source_query": state.source_query,
                    "recent_asset_ids": list(state.recent_asset_ids),
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
                for output_id, state in states.items()
            },
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
            self._fsync_directory(self._path.parent)
        except OSError:
            raise HistoryError("cannot persist history state") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
