from __future__ import annotations

import os
from uuid import UUID

import pytest

from cast_immich.cast import discover_chromecasts


@pytest.mark.hardware
@pytest.mark.skipif(
    not os.environ.get("CAST_IMMICH_TEST_CHROMECAST_UUID"),
    reason="set CAST_IMMICH_TEST_CHROMECAST_UUID to run hardware discovery",
)
async def test_discovers_configured_chromecast() -> None:
    target = UUID(os.environ["CAST_IMMICH_TEST_CHROMECAST_UUID"])
    discovered = await discover_chromecasts(10)
    assert target in {device.uuid for device in discovered}
