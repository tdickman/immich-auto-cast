from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
    source_collection: str | None = None
    source_start_date: str | None = None
    source_end_date: str | None = None
    source_city: str | None = None
    source_state: str | None = None
    source_country: str | None = None
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
        self,
        kind: str,
        source_id: str | None = None,
        query: str | None = None,
        collection: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        city: str | None = None,
        state: str | None = None,
        country: str | None = None,
    ) -> HistoryState:
        return self._store._set_source(
            self.output_id,
            kind,
            source_id,
            query,
            collection,
            start_date,
            end_date,
            city,
            state,
            country,
        )

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
    SOURCE_KINDS = frozenset({"timeline", "album", "person", "search", "event", "filter", "video"})
    EVENT_COLLECTIONS = frozenset(
        {"on_this_day", "recent_favorites", "last_month", "seasonal", "recent_person_recap"}
    )

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
        self,
        kind: str,
        source_id: str | None = None,
        query: str | None = None,
        collection: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        city: str | None = None,
        state: str | None = None,
        country: str | None = None,
    ) -> HistoryState:
        return self.for_output("default").set_source(
            kind, source_id, query, collection, start_date, end_date, city, state, country
        )

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
                source_collection=current.source_collection,
                source_start_date=current.source_start_date,
                source_end_date=current.source_end_date,
                source_city=current.source_city,
                source_state=current.source_state,
                source_country=current.source_country,
                recent_asset_ids=current.recent_asset_ids,
            )
            states[output_id] = state
            self._write_unlocked(states)
            return state

    def _set_source(
        self,
        output_id: str,
        kind: str,
        source_id: str | None,
        query: str | None,
        collection: str | None,
        start_date: str | None,
        end_date: str | None,
        city: str | None,
        region: str | None,
        country: str | None,
    ) -> HistoryState:
        source = self._validate_source(
            kind, source_id, query, collection, start_date, end_date, city, region, country
        )
        with self._lock:
            states = self._load_all_unlocked()
            current = states.get(output_id, HistoryState())
            updated = HistoryState(
                rotation_enabled=current.rotation_enabled,
                records=current.records,
                autocast_enabled=current.autocast_enabled,
                source_kind=source[0],
                source_id=source[1],
                source_query=source[2],
                source_collection=source[3],
                source_start_date=source[4],
                source_end_date=source[5],
                source_city=source[6],
                source_state=source[7],
                source_country=source[8],
                recent_asset_ids=current.recent_asset_ids,
            )
            states[output_id] = updated
            self._write_unlocked(states)
            return updated

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
                source_collection=current.source_collection,
                source_start_date=current.source_start_date,
                source_end_date=current.source_end_date,
                source_city=current.source_city,
                source_state=current.source_state,
                source_country=current.source_country,
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
        source = self._validate_source(
            raw.get("source_kind", "timeline"),
            raw.get("source_id"),
            raw.get("source_query"),
            raw.get("source_collection"),
            raw.get("source_start_date"),
            raw.get("source_end_date"),
            raw.get("source_city"),
            raw.get("source_state"),
            raw.get("source_country"),
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
            source_kind=source[0],
            source_id=source[1],
            source_query=source[2],
            source_collection=source[3],
            source_start_date=source[4],
            source_end_date=source[5],
            source_city=source[6],
            source_state=source[7],
            source_country=source[8],
            recent_asset_ids=tuple(recent_data),
        )

    @classmethod
    def _validate_source(
        cls,
        kind: Any,
        source_id: Any,
        query: Any,
        collection: Any = None,
        start_date: Any = None,
        end_date: Any = None,
        city: Any = None,
        state: Any = None,
        country: Any = None,
        *,
        error_type: type[Exception] = HistoryError,
    ) -> tuple[
        str,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ]:
        if not isinstance(kind, str) or kind not in cls.SOURCE_KINDS:
            raise error_type("invalid photo source kind")
        if kind in {"album", "person"}:
            if not isinstance(source_id, str) or any(
                value is not None
                for value in (query, collection, start_date, end_date, city, state, country)
            ):
                raise error_type("invalid photo source fields")
            try:
                normalized_id = str(UUID(source_id))
            except ValueError:
                raise error_type("invalid photo source ID") from None
            return kind, normalized_id, None, None, None, None, None, None, None
        if kind == "search":
            if (
                source_id is not None
                or not isinstance(query, str)
                or any(
                    value is not None
                    for value in (collection, start_date, end_date, city, state, country)
                )
            ):
                raise error_type("invalid photo source fields")
            normalized_query = query.strip()
            if not normalized_query or len(normalized_query) > 200:
                raise error_type("invalid photo source query")
            return kind, None, normalized_query, None, None, None, None, None, None
        if kind == "event":
            if collection not in cls.EVENT_COLLECTIONS or any(
                value is not None for value in (query, start_date, end_date, city, state, country)
            ):
                raise error_type("invalid event collection")
            if collection == "recent_person_recap":
                if not isinstance(source_id, str):
                    raise error_type("invalid photo source ID")
                try:
                    event_source_id = str(UUID(source_id))
                except ValueError:
                    raise error_type("invalid photo source ID") from None
                return kind, event_source_id, None, collection, None, None, None, None, None
            elif source_id is not None:
                raise error_type("invalid photo source fields")
            return kind, None, None, collection, None, None, None, None, None
        if kind == "filter":
            if any(value is not None for value in (source_id, query, collection)):
                raise error_type("invalid photo source fields")
            normalized_dates: list[str | None] = []
            for value in (start_date, end_date):
                if value is None:
                    normalized_dates.append(None)
                    continue
                if not isinstance(value, str):
                    raise error_type("invalid photo source date")
                try:
                    normalized_dates.append(date.fromisoformat(value).isoformat())
                except ValueError:
                    raise error_type("invalid photo source date") from None
            if (
                normalized_dates[0]
                and normalized_dates[1]
                and normalized_dates[0] > normalized_dates[1]
            ):
                raise error_type("photo source start date must not follow end date")
            locations: list[str | None] = []
            for value in (city, state, country):
                if value is None:
                    locations.append(None)
                    continue
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
                    raise error_type("invalid photo source location")
                locations.append(value.strip())
            if not any((*normalized_dates, *locations)):
                raise error_type("photo source filter must not be empty")
            return (
                kind,
                None,
                None,
                None,
                normalized_dates[0],
                normalized_dates[1],
                locations[0],
                locations[1],
                locations[2],
            )
        if any(
            value is not None
            for value in (source_id, query, collection, start_date, end_date, city, state, country)
        ):
            raise error_type("invalid photo source fields")
        return kind, None, None, None, None, None, None, None, None

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
                    "source_collection": state.source_collection,
                    "source_start_date": state.source_start_date,
                    "source_end_date": state.source_end_date,
                    "source_city": state.source_city,
                    "source_state": state.source_state,
                    "source_country": state.source_country,
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
