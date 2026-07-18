from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer


@pytest.fixture
async def serve_app() -> AsyncIterator[object]:
    servers: list[TestServer] = []

    async def start(app: web.Application) -> TestServer:
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return server

    yield start
    for server in servers:
        await server.close()
