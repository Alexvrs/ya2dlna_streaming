import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.endpoints import api_service
from api.endpoints.api_service import zone_off, zone_on
from owntone_output.owntone_controller import OwnToneZoneError

ZONES = {"Обитель": ["Колонка"], "Спальня": ["Спальня"]}


def make(running: bool, current: str):
    manager = MagicMock()
    manager.is_running = running
    manager.start = AsyncMock()
    manager.stop = AsyncMock()
    owntone = MagicMock()
    owntone.zones = ZONES
    state = {"zone": current}
    owntone.current_zone = lambda: state["zone"]

    async def select(zone):
        if zone not in ZONES:
            raise OwnToneZoneError(zone)
        state["zone"] = zone

    owntone.select_zone = AsyncMock(side_effect=select)
    return manager, owntone


async def test_on_starts_stream_when_stopped():
    manager, owntone = make(running=False, current="Обитель")
    assert await zone_on(manager, owntone, "Спальня") == "started"
    await asyncio.sleep(0)
    assert api_service._start_task is not None
    await api_service._start_task
    manager.start.assert_awaited_once()
    assert owntone.current_zone() == "Спальня"


async def test_on_switches_zone_while_playing():
    manager, owntone = make(running=True, current="Обитель")
    assert await zone_on(manager, owntone, "Спальня") == "switched"
    manager.start.assert_not_called()


async def test_on_same_zone_is_noop():
    manager, owntone = make(running=True, current="Обитель")
    assert await zone_on(manager, owntone, "Обитель") == "unchanged"
    manager.start.assert_not_called()


async def test_off_stops_only_current_zone():
    manager, owntone = make(running=True, current="Обитель")
    assert await zone_off(manager, owntone, "Спальня") == "unchanged"
    manager.stop.assert_not_called()
    assert await zone_off(manager, owntone, "Обитель") == "stopped"
    manager.stop.assert_awaited_once()


async def test_off_when_stopped_is_noop():
    manager, owntone = make(running=False, current="Обитель")
    assert await zone_off(manager, owntone, "Обитель") == "unchanged"


async def test_unknown_zone_rejected():
    manager, owntone = make(running=False, current="Обитель")
    with pytest.raises(OwnToneZoneError):
        await zone_on(manager, owntone, "Кухня")
    with pytest.raises(OwnToneZoneError):
        await zone_off(manager, owntone, "Кухня")
