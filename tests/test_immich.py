from __future__ import annotations

import asyncio
from datetime import date
from itertools import pairwise
from typing import Any
from uuid import UUID

import pytest
from aiohttp import web

from cast_immich.config import ImmichSettings
from cast_immich.immich import (
    AssetUnavailable,
    EventCollection,
    ImmichClient,
    ImmichFailureKind,
    PermanentImmichError,
    PhotoSource,
    SourceKind,
)

ASSET_ID = UUID("12345678-1234-4234-8234-123456789abc")


def eligible(asset_id: UUID = ASSET_ID, **overrides: Any) -> dict[str, Any]:
    value = {
        "id": str(asset_id),
        "type": "IMAGE",
        "visibility": "timeline",
        "isArchived": False,
        "isTrashed": False,
        "isOffline": False,
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_search_contract_filters_and_preview_authentication(serve_app: Any) -> None:
    observed: dict[str, Any] = {}

    async def search(request: web.Request) -> web.Response:
        observed["search_key"] = request.headers.get("x-api-key")
        observed["body"] = await request.json()
        return web.json_response([eligible()])

    async def preview(request: web.Request) -> web.Response:
        observed["preview_key"] = request.headers.get("x-api-key")
        assert request.query["size"] == "preview"
        return web.Response(body=b"jpeg", content_type="image/jpeg")

    app = web.Application()
    app.router.add_post("/api/search/random", search)
    app.router.add_get("/api/assets/{asset_id}/thumbnail", preview)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(settings) as client:
        asset = await client.select_asset(set(), 50)
        result = await client.fetch_preview(asset.id)

    assert asset.id == ASSET_ID
    assert result.body == b"jpeg"
    assert observed["search_key"] == observed["preview_key"] == "secret"
    assert observed["body"] == {
        "type": "IMAGE",
        "visibility": "timeline",
        "withDeleted": False,
        "isOffline": False,
        "withExif": True,
        "size": 50,
    }


@pytest.mark.asyncio
async def test_preview_reads_all_network_chunks(serve_app: Any) -> None:
    async def preview(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        await response.prepare(request)
        await response.write(b"first-")
        await asyncio.sleep(0.01)
        await response.write(b"second")
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/api/assets/{asset_id}/thumbnail", preview)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(settings) as client:
        result = await client.fetch_preview(ASSET_ID)

    assert result.body == b"first-second"


@pytest.mark.asyncio
async def test_ineligible_and_recent_assets_are_skipped(serve_app: Any) -> None:
    async def search(_request: web.Request) -> web.Response:
        return web.json_response(
            [
                eligible(type="VIDEO"),
                eligible(visibility="locked"),
                eligible(isArchived=True),
                eligible(isTrashed=True),
                eligible(isOffline=True),
                eligible(),
            ]
        )

    app = web.Application()
    app.router.add_post("/api/search/random", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(settings) as client:
        with pytest.raises(AssetUnavailable):
            await client.select_asset({ASSET_ID}, 10)


@pytest.mark.asyncio
async def test_select_assets_returns_unique_non_recent_candidates(serve_app: Any) -> None:
    recent_id = UUID(int=1)
    available = [UUID(int=value) for value in range(2, 14)]

    async def search(request: web.Request) -> web.Response:
        assert (await request.json())["size"] == 11
        return web.json_response(
            [
                eligible(recent_id),
                *(eligible(asset_id) for asset_id in available),
                eligible(available[0]),
            ]
        )

    app = web.Application()
    app.router.add_post("/api/search/random", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(settings) as client:
        selected = await client.select_assets({recent_id}, 5, 11)

    assert len(selected) == 11
    assert len({asset.id for asset in selected}) == 11
    assert recent_id not in {asset.id for asset in selected}


@pytest.mark.asyncio
async def test_albums_filter_random_search_and_preserve_location(serve_app: Any) -> None:
    album_id = UUID(int=99)
    observed: dict[str, Any] = {}

    async def albums(_request: web.Request) -> web.Response:
        return web.json_response(
            [
                {"id": str(album_id), "albumName": "Trips", "assetCount": 12},
                {"id": "invalid", "albumName": "Ignored", "assetCount": 1},
            ]
        )

    async def search(request: web.Request) -> web.Response:
        observed["body"] = await request.json()
        return web.json_response(
            [
                eligible(
                    visibility="archive",
                    exifInfo={"city": "Portland", "state": "Oregon"},
                    localDateTime="2026-07-04T14:30:00",
                )
            ]
        )

    app = web.Application()
    app.router.add_get("/api/albums", albums)
    app.router.add_post("/api/search/random", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(settings) as client:
        listed = await client.list_albums()
        selected = await client.select_assets_from(set(), 20, 1, album_id)
        cached_location = await client.fetch_location(ASSET_ID)

    assert [(album.name, album.asset_count) for album in listed] == [("Trips", 12)]
    assert selected[0].location == "Portland, Oregon"
    assert selected[0].date == "July 4, 2026"
    assert cached_location == "Portland, Oregon"
    assert observed["body"] == {
        "type": "IMAGE",
        "withDeleted": False,
        "isOffline": False,
        "withExif": True,
        "size": 20,
        "albumIds": [str(album_id)],
    }


@pytest.mark.asyncio
async def test_people_are_paginated_and_person_filters_random_search(serve_app: Any) -> None:
    first_id, second_id = UUID(int=21), UUID(int=22)
    observed: dict[str, Any] = {}

    async def people(request: web.Request) -> web.Response:
        page = int(request.query["page"])
        assert request.query["withHidden"] == "false"
        if page == 1:
            return web.json_response(
                {
                    "people": [{"id": str(first_id), "name": "Ada", "isHidden": False}],
                    "hasNextPage": True,
                }
            )
        return web.json_response(
            {
                "people": [
                    {"id": str(second_id), "name": "", "isHidden": False},
                    {"id": str(UUID(int=23)), "name": "Hidden", "isHidden": True},
                ],
                "hasNextPage": False,
            }
        )

    async def search(request: web.Request) -> web.Response:
        observed["body"] = await request.json()
        return web.json_response([eligible(visibility="archive")])

    app = web.Application()
    app.router.add_get("/api/people", people)
    app.router.add_post("/api/search/random", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(settings) as client:
        listed = await client.list_people()
        selected = await client.select_assets_for(
            set(), 20, 1, PhotoSource(SourceKind.PERSON, first_id)
        )

    assert [(person.id, person.name) for person in listed] == [
        (first_id, "Ada"),
        (second_id, "Unnamed person"),
    ]
    assert selected[0].id == ASSET_ID
    assert observed["body"]["personIds"] == [str(first_id)]


@pytest.mark.asyncio
async def test_ai_search_uses_smart_search_contract(serve_app: Any) -> None:
    observed: dict[str, Any] = {}

    async def search(request: web.Request) -> web.Response:
        observed["body"] = await request.json()
        return web.json_response({"assets": {"items": [eligible(visibility="archive")]}})

    app = web.Application()
    app.router.add_post("/api/search/smart", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(settings) as client:
        selected = await client.select_assets_for(
            set(), 40, 1, PhotoSource(SourceKind.SEARCH, query="snowy mountains")
        )

    assert selected[0].id == ASSET_ID
    assert observed["body"]["query"] == "snowy mountains"
    assert observed["body"]["page"] == 1
    assert observed["body"]["size"] == 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            PhotoSource(SourceKind.EVENT, collection=EventCollection.RECENT_FAVORITES),
            {"isFavorite": True, "takenAfter": "2026-04-19T00:00:00Z"},
        ),
        (
            PhotoSource(SourceKind.EVENT, collection=EventCollection.LAST_MONTH),
            {
                "takenAfter": "2026-06-01T00:00:00Z",
                "takenBefore": "2026-07-01T00:00:00Z",
            },
        ),
        (
            PhotoSource(
                SourceKind.EVENT,
                UUID(int=42),
                collection=EventCollection.RECENT_PERSON_RECAP,
            ),
            {
                "personIds": [str(UUID(int=42))],
                "takenAfter": "2025-07-18T00:00:00Z",
            },
        ),
        (
            PhotoSource(
                SourceKind.FILTER,
                start_date=date(2020, 1, 2),
                end_date=date(2020, 2, 3),
                city="Bath",
                state="Somerset",
                country="United Kingdom",
            ),
            {
                "takenAfter": "2020-01-02T00:00:00Z",
                "takenBefore": "2020-02-04T00:00:00Z",
                "city": "Bath",
                "state": "Somerset",
                "country": "United Kingdom",
            },
        ),
    ],
)
async def test_event_and_custom_filters_use_random_search_contract(
    serve_app: Any, source: PhotoSource, expected: dict[str, Any]
) -> None:
    observed: dict[str, Any] = {}

    async def search(request: web.Request) -> web.Response:
        observed.update(await request.json())
        return web.json_response([eligible(visibility="archive")])

    app = web.Application()
    app.router.add_post("/api/search/random", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(settings, today=lambda: date(2026, 7, 18)) as client:
        selected = await client.select_assets_for(set(), 20, 1, source)

    assert selected[0].id == ASSET_ID
    assert {key: observed[key] for key in expected} == expected


@pytest.mark.asyncio
async def test_on_this_day_keeps_only_matching_dates_from_prior_years(serve_app: Any) -> None:
    other_id = UUID(int=2)
    matching_id = UUID(int=3)
    observed: dict[str, Any] = {}
    requests = 0

    async def search(request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        observed.update(await request.json())
        return web.json_response(
            [
                eligible(localDateTime="2019-07-18T09:00:00"),
                eligible(other_id, localDateTime="2019-07-19T09:00:00"),
                eligible(matching_id, localDateTime="2020-07-18T09:00:00"),
            ]
        )

    app = web.Application()
    app.router.add_post("/api/search/random", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    source = PhotoSource(SourceKind.EVENT, collection=EventCollection.ON_THIS_DAY)
    async with ImmichClient(settings, today=lambda: date(2026, 7, 18)) as client:
        selected = await client.select_assets_for(set(), 20, 11, source)
        repeated = await client.select_assets_for({ASSET_ID, matching_id}, 20, 11, source)

    assert len(selected) == len(repeated) == 11
    assert {asset.id for asset in (*selected, *repeated)} == {ASSET_ID, matching_id}
    assert all(left.id != right.id for left, right in pairwise(selected))
    assert all(left.id != right.id for left, right in pairwise(repeated))
    assert requests == 1
    assert observed["takenBefore"] == "2026-01-01T00:00:00Z"
    assert observed["size"] == 1000


@pytest.mark.asyncio
async def test_random_search_batch_is_consumed_before_refill(serve_app: Any) -> None:
    requests = 0
    assets = [UUID(int=value) for value in range(1, 31)]

    async def search(_request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        return web.json_response([eligible(asset_id) for asset_id in assets])

    app = web.Application()
    app.router.add_post("/api/search/random", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    selected_ids: set[UUID] = set()
    async with ImmichClient(settings) as client:
        initial = await client.select_assets(set(), 50, 11)
        selected_ids.update(asset.id for asset in initial)
        for _ in range(8):
            selected = await client.select_assets(selected_ids, 50, 1)
            selected_ids.add(selected[0].id)

    assert len(selected_ids) == 19
    assert requests == 1


@pytest.mark.asyncio
async def test_authentication_failure_is_permanent(serve_app: Any) -> None:
    async def search(_request: web.Request) -> web.Response:
        return web.Response(status=403)

    app = web.Application()
    app.router.add_post("/api/search/random", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(settings) as client:
        with pytest.raises(PermanentImmichError, match="permissions") as raised:
            await client.select_asset(set(), 10)
    assert raised.value.kind is ImmichFailureKind.AUTHORIZATION


@pytest.mark.asyncio
async def test_incompatible_search_endpoint_is_permanent(serve_app: Any) -> None:
    async def search(_request: web.Request) -> web.Response:
        return web.Response(status=404)

    app = web.Application()
    app.router.add_post("/api/search/random", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(settings) as client:
        with pytest.raises(PermanentImmichError, match="rejected"):
            await client.select_asset(set(), 10)


@pytest.mark.asyncio
async def test_arbitrary_5xx_recovers_within_bounded_retries(serve_app: Any) -> None:
    attempts = 0

    async def search(_request: web.Request) -> web.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return web.Response(status=507)
        return web.json_response(
            [
                {
                    "id": str(ASSET_ID),
                    "type": "IMAGE",
                    "visibility": "timeline",
                    "isArchived": False,
                    "isTrashed": False,
                    "isOffline": False,
                }
            ]
        )

    app = web.Application()
    app.router.add_post("/api/search/random", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 2)
    async with ImmichClient(settings) as client:
        selected = await client.select_asset(set(), 10)

    assert selected.id == ASSET_ID
    assert attempts == 2
