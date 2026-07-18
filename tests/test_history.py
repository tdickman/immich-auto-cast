from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cast_immich.history import HistoryError, HistoryStore


def test_rotation_enabled_persists_atomically(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = HistoryStore(path)

    assert store.load().rotation_enabled is True
    state = store.set_rotation_enabled(False)

    assert state.rotation_enabled is False
    assert HistoryStore(path).load().rotation_enabled is False
    assert path.stat().st_mode & 0o777 == 0o600


def test_duplicate_load_is_inserted_once(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "state.json")
    confirmed = datetime(2026, 7, 17, 12, tzinfo=UTC)

    first = store.record_display("load-1", "asset-1", confirmed_at=confirmed)
    duplicate = store.record_display("load-1", "asset-2", confirmed_at=confirmed)

    assert duplicate == first
    assert store.load().records == (first,)


def test_only_newest_ten_confirmations_survive_reload(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = HistoryStore(path)
    start = datetime(2026, 7, 17, tzinfo=UTC)

    for index in range(11):
        store.record_display(
            f"load-{index}", f"asset-{index}", confirmed_at=start + timedelta(minutes=index)
        )

    records = HistoryStore(path).load().records
    assert len(records) == 10
    assert [record.load_id for record in records] == [f"load-{index}" for index in range(10, 0, -1)]
    assert all(record.confirmed_at.tzinfo is UTC for record in records)
    assert len({record.event_id for record in records}) == 10


def test_malformed_history_is_reported_and_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    malformed = b'{"version": 1, "rotation_enabled": true, "records": "bad"}\n'
    path.write_bytes(malformed)
    store = HistoryStore(path)

    with pytest.raises(HistoryError, match="cannot read history state"):
        store.load()
    with pytest.raises(HistoryError):
        store.set_rotation_enabled(False)

    assert path.read_bytes() == malformed


def test_replace_failure_preserves_previous_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    store = HistoryStore(path)
    store.record_display("load-1", "asset-1")
    before = path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("cast_immich.history.os.replace", fail_replace)
    with pytest.raises(HistoryError, match="cannot persist history state"):
        store.set_rotation_enabled(False)

    assert path.read_bytes() == before


def test_history_json_is_versioned_and_contains_no_extra_records(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = HistoryStore(path)
    store.record_display("load-1", "asset-1")

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    assert persisted["rotation_enabled"] is True
    assert len(persisted["records"]) == 1
