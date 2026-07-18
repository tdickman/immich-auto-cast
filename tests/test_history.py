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


def test_autocast_enabled_defaults_on_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = HistoryStore(path)

    assert store.load().autocast_enabled is True
    store.set_autocast_enabled(False)

    state = HistoryStore(path).load()
    assert state.autocast_enabled is False
    assert state.rotation_enabled is True


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
    assert persisted["version"] == 3
    assert persisted["outputs"]["default"]["rotation_enabled"] is True
    assert persisted["outputs"]["default"]["source_kind"] == "timeline"
    assert persisted["outputs"]["default"]["recent_asset_ids"] == ["asset-1"]
    assert len(persisted["outputs"]["default"]["records"]) == 1


def test_outputs_are_isolated_and_removed_output_state_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = HistoryStore(path)
    kitchen = store.for_output("kitchen")
    office = store.for_output("office")

    kitchen.set_rotation_enabled(False)
    kitchen.record_display("kitchen-load", "kitchen-asset")
    office.record_display("office-load", "office-asset")

    assert kitchen.load().rotation_enabled is False
    assert [item.asset_id for item in kitchen.load().records] == ["kitchen-asset"]
    assert [item.asset_id for item in office.load().records] == ["office-asset"]
    office.set_autocast_enabled(False)
    assert "kitchen" in json.loads(path.read_text())["outputs"]


def test_v1_maps_to_default_and_is_not_rewritten_until_mutation(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    original = b'{"version":1,"rotation_enabled":false,"records":[]}\n'
    path.write_bytes(original)
    store = HistoryStore(path)

    assert store.for_output("default").load().rotation_enabled is False
    assert store.for_output("other").load().rotation_enabled is True
    assert path.read_bytes() == original

    store.for_output("other").set_autocast_enabled(False)
    document = json.loads(path.read_text())
    assert document["version"] == 3
    assert document["outputs"]["default"]["rotation_enabled"] is False


def test_v2_loads_with_v3_defaults_and_upgrades_on_mutation(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    original = b'{"version":2,"outputs":{"office":{"rotation_enabled":true,"records":[]}}}\n'
    path.write_bytes(original)
    office = HistoryStore(path).for_output("office")

    state = office.load()
    assert state.source_kind == "timeline"
    assert state.source_id is None
    assert state.source_query is None
    assert state.recent_asset_ids == ()
    assert path.read_bytes() == original

    office.set_rotation_enabled(False)
    assert json.loads(path.read_text())["version"] == 3


def test_source_selection_persists_as_validated_primitives(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    history = HistoryStore(path).for_output("office")
    source_id = "12345678-1234-4234-8234-123456789ABC"

    album = history.set_source("album", source_id)
    assert album.source_kind == "album"
    assert album.source_id == source_id.lower()
    assert album.source_query is None

    search = history.set_source("search", query="  summer holiday  ")
    assert search.source_kind == "search"
    assert search.source_id is None
    assert search.source_query == "summer holiday"
    assert HistoryStore(path).for_output("office").load() == search
    persisted = json.loads(path.read_text())["outputs"]["office"]
    assert persisted["source_kind"] == "search"
    assert persisted["source_id"] is None
    assert persisted["source_query"] == "summer holiday"


def test_event_and_filter_sources_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    history = HistoryStore(path).for_output("office")
    person_id = "12345678-1234-4234-8234-123456789abc"

    recap = history.set_source("event", person_id, collection="recent_person_recap")
    assert recap.source_collection == "recent_person_recap"
    assert recap.source_id == person_id

    filtered = history.set_source(
        "filter",
        start_date="2020-01-02",
        end_date="2020-02-03",
        city="  Bath  ",
        state="Somerset",
        country="United Kingdom",
    )
    assert filtered.source_start_date == "2020-01-02"
    assert filtered.source_end_date == "2020-02-03"
    assert filtered.source_city == "Bath"
    assert HistoryStore(path).for_output("office").load() == filtered


@pytest.mark.parametrize(
    ("kind", "source_id", "query"),
    [
        ("unknown", None, None),
        ("timeline", "unexpected", None),
        ("album", "not-a-uuid", None),
        ("person", None, None),
        ("search", None, "   "),
        ("search", None, "x" * 201),
    ],
)
def test_invalid_source_is_rejected_without_overwrite(
    tmp_path: Path, kind: str, source_id: str | None, query: str | None
) -> None:
    path = tmp_path / "state.json"
    history = HistoryStore(path)
    history.set_rotation_enabled(False)
    before = path.read_bytes()

    with pytest.raises(HistoryError):
        history.set_source(kind, source_id, query)

    assert path.read_bytes() == before


def test_record_display_atomically_updates_bounded_unique_recent_assets(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = HistoryStore(path)
    store.MAX_RECENT_ASSETS = 3

    store.record_display("load-1", "asset-1")
    store.record_display("load-2", "asset-2")
    store.record_display("load-3", "asset-1")
    store.record_display("load-4", "asset-3")
    store.record_display("load-5", "asset-4")

    assert store.load().recent_asset_ids == ("asset-4", "asset-3", "asset-1")
    persisted = json.loads(path.read_text())["outputs"]["default"]
    assert persisted["recent_asset_ids"] == ["asset-4", "asset-3", "asset-1"]


def test_rejects_malformed_v3_recent_and_source_values(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "outputs": {
                    "default": {
                        "rotation_enabled": True,
                        "records": [],
                        "source_kind": "album",
                        "source_id": 123,
                        "source_query": None,
                        "recent_asset_ids": ["duplicate", "duplicate"],
                    }
                },
            }
        )
    )

    with pytest.raises(HistoryError, match="cannot read history state"):
        HistoryStore(path).load()


@pytest.mark.parametrize("version", [True, 2.0, "3"])
def test_rejects_non_integer_schema_versions(tmp_path: Path, version: object) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": version, "outputs": {}}))

    with pytest.raises(HistoryError, match="cannot read history state"):
        HistoryStore(path).load()


def test_history_write_fsyncs_file_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []
    real_fsync = __import__("os").fsync

    def track_fsync(descriptor: int) -> None:
        synced.append(Path(f"/proc/self/fd/{descriptor}").resolve())
        real_fsync(descriptor)

    monkeypatch.setattr("cast_immich.history.os.fsync", track_fsync)
    HistoryStore(tmp_path / "state.json").set_rotation_enabled(False)

    assert tmp_path in synced
    assert any(path.parent == tmp_path and path.name.startswith(".state.json.") for path in synced)
