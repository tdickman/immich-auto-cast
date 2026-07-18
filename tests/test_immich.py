from __future__ import annotations

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
        "size": 50,
    }


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
