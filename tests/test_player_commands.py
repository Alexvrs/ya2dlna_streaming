from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.endpoints.api_service import player_command
from main_stream_service.yandex_music_api import YandexMusicAPI
from yandex_station.protobuf_parser import Protobuf
from yandex_station.station_controls import YandexStationControls
from yandex_station.station_ws_control import YandexStationClient


def make_manager(
    running: bool,
    playing: bool | None,
    track_type: str = "Track",
    track_id: str = "42",
):
    """Менеджер с замоканными контролами Станции и API Музыки."""
    station = MagicMock()
    for name in ("next", "prev", "play", "stop", "send_text"):
        setattr(station, name, AsyncMock())
    track = (
        None
        if playing is None
        else SimpleNamespace(playing=playing, type=track_type, id=track_id)
    )
    station.get_current_track = AsyncMock(return_value=track)
    manager = MagicMock()
    manager.is_running = running
    manager.station_controls = station
    manager.yandex_music_api.like_track = AsyncMock(return_value=True)
    manager.yandex_music_api.dislike_track = AsyncMock(return_value=True)
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


async def test_like_marks_current_track():
    manager, station = make_manager(running=True, playing=True)
    assert await player_command(manager, "like") == "liked"
    manager.yandex_music_api.like_track.assert_awaited_once_with("42")
    station.next.assert_not_awaited()


async def test_dislike_marks_and_skips():
    manager, station = make_manager(running=True, playing=True)
    assert await player_command(manager, "dislike") == "disliked"
    manager.yandex_music_api.dislike_track.assert_awaited_once_with("42")
    station.next.assert_awaited_once()


@pytest.mark.parametrize("action", ["like", "dislike"])
async def test_rating_refused_for_radio(action):
    manager, station = make_manager(
        running=True, playing=True, track_type="FmRadio"
    )
    with pytest.raises(LookupError):
        await player_command(manager, action)
    manager.yandex_music_api.like_track.assert_not_awaited()
    manager.yandex_music_api.dislike_track.assert_not_awaited()
    station.next.assert_not_awaited()


async def test_rating_refused_without_track():
    manager, station = make_manager(running=True, playing=None)
    with pytest.raises(LookupError):
        await player_command(manager, "like")


async def test_music_api_like_inits_account_once():
    client = MagicMock()
    client.me = None

    async def init():
        client.me = object()

    client.init = AsyncMock(side_effect=init)
    client.users_likes_tracks_add = AsyncMock(return_value=True)
    api = YandexMusicAPI(client=client)
    assert await api.like_track("42") is True
    assert await api.like_track("43") is True
    client.init.assert_awaited_once()
    client.users_likes_tracks_add.assert_awaited_with("43")


@pytest.mark.parametrize(
    "action, phrase",
    [("dj_on", "включи режим диджей"), ("dj_off", "выключи режим диджей")],
)
async def test_dj_mode_sends_phrase(action, phrase):
    manager, station = make_manager(running=True, playing=True)
    assert await player_command(manager, action) == action
    station.send_text.assert_awaited_once_with(phrase)
    station.get_current_track.assert_not_awaited()


async def test_dj_mode_refused_when_stream_stopped():
    manager, station = make_manager(running=False, playing=True)
    with pytest.raises(RuntimeError):
        await player_command(manager, "dj_on")
    station.send_text.assert_not_awaited()
