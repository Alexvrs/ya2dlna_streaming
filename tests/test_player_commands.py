from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.endpoints.api_service import player_command
from yandex_station.protobuf_parser import Protobuf
from yandex_station.station_controls import YandexStationControls
from yandex_station.station_ws_control import YandexStationClient


def make_manager(running: bool, playing: bool | None):
    """Менеджер с замоканными контролами Станции."""
    station = MagicMock()
    for name in ("next", "prev", "play", "stop"):
        setattr(station, name, AsyncMock())
    track = None if playing is None else SimpleNamespace(playing=playing)
    station.get_current_track = AsyncMock(return_value=track)
    manager = MagicMock()
    manager.is_running = running
    manager.station_controls = station
    return manager, station


@pytest.mark.parametrize("action", ["next", "prev"])
async def test_track_switch_sends_station_command(action):
    manager, station = make_manager(running=True, playing=True)
    assert await player_command(manager, action) == action
    getattr(station, action).assert_awaited_once()
    station.play.assert_not_awaited()
    station.stop.assert_not_awaited()


async def test_play_pause_pauses_playing_station():
    manager, station = make_manager(running=True, playing=True)
    assert await player_command(manager, "play_pause") == "paused"
    station.stop.assert_awaited_once()
    station.play.assert_not_awaited()


@pytest.mark.parametrize("playing", [False, None])
async def test_play_pause_resumes_paused_or_unknown(playing):
    manager, station = make_manager(running=True, playing=playing)
    assert await player_command(manager, "play_pause") == "playing"
    station.play.assert_awaited_once()
    station.stop.assert_not_awaited()


async def test_commands_refused_when_stream_stopped():
    manager, station = make_manager(running=False, playing=True)
    with pytest.raises(RuntimeError):
        await player_command(manager, "next")
    station.next.assert_not_awaited()


async def test_unknown_action_rejected():
    manager, station = make_manager(running=True, playing=True)
    with pytest.raises(ValueError):
        await player_command(manager, "rewind")


@pytest.mark.parametrize(
    "method, command", [("next", "next"), ("prev", "prev")]
)
async def test_station_controls_send_glagol_command(method, command):
    ws_client = AsyncMock(spec=YandexStationClient)
    controls = YandexStationControls(
        ws_client=ws_client, protobuf=MagicMock(spec=Protobuf)
    )
    await getattr(controls, method)()
    ws_client.send_command.assert_awaited_once_with({"command": command})
