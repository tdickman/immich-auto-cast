from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from aiohttp import web

from cast_immich.config import ImmichSettings
from cast_immich.immich import (
    AssetUnavailable,
    ImmichClient,
    PermanentImmichError,
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
async def test_authentication_failure_is_permanent(serve_app: Any) -> None:
    async def search(_request: web.Request) -> web.Response:
        return web.Response(status=403)

    app = web.Application()
    app.router.add_post("/api/search/random", search)
    server = await serve_app(app)
    settings = ImmichSettings(str(server.make_url("/")).rstrip("/"), "secret", 2, 1)
    async with ImmichClient(settings) as client:
        with pytest.raises(PermanentImmichError, match="permissions"):
            await client.select_asset(set(), 10)


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
