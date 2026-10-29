from unittest.mock import AsyncMock, MagicMock

from yandex_station.protobuf_parser import Protobuf
from yandex_station.station_controls import YandexStationControls
from yandex_station.station_ws_control import YandexStationClient

BASE_PLAYER_STATE = {
    "id": "42",
    "title": "title",
    "type": "Track",
    "subtitle": "artist",
    "duration": 200,
    "progress": 10,
}


def make_controls(player_state) -> YandexStationControls:
    """Контролы станции с замоканным состоянием плеера."""
    ws_client = AsyncMock(spec=YandexStationClient)
    ws_client.get_latest_message.return_value = {
        "state": {"playerState": player_state, "playing": True}
    }
    return YandexStationControls(
        ws_client=ws_client,
        protobuf=MagicMock(spec=Protobuf),
    )


async def test_current_track_carries_neighbor_ids():
    controls = make_controls(
        {
            **BASE_PLAYER_STATE,
            "entityInfo": {
                "next": {"id": "43", "type": "Track"},
                "prev": {"id": "41", "type": "Track"},
            },
        }
    )

    track = await controls.get_current_track()

    assert track is not None
    assert track.next_id == "43"
    assert track.prev_id == "41"


async def test_non_track_neighbors_are_ignored():
    controls = make_controls(
        {
            **BASE_PLAYER_STATE,
            "entityInfo": {
                "next": {"id": "gen", "type": "Generative"},
                "prev": "мусор",
            },
        }
    )

    track = await controls.get_current_track()

    assert track is not None
    assert track.next_id is None
    assert track.prev_id is None


async def test_missing_entity_info_is_safe():
    controls = make_controls(dict(BASE_PLAYER_STATE))

    track = await controls.get_current_track()

    assert track is not None
    assert track.next_id is None
    assert track.prev_id is None


async def test_say_sends_repeat_command_to_station():
    """say() превращает фразу в команду локального TTS."""
    controls = make_controls(dict(BASE_PLAYER_STATE))

    await controls.say("тестовая фраза")

    controls._ws_client.send_command.assert_awaited_once_with(
        {"command": "sendText", "text": "Повтори за мной тестовая фраза"}
    )


async def test_alice_fade_is_short_and_immediate(monkeypatch):
    """После речи Алисы станция гаснет сразу и за ~0,5 с."""
    import asyncio as _asyncio

    from yandex_station.station_controls import YandexStationControls

    sleeps = []

    async def fake_sleep(delay, *args, **kwargs):
        sleeps.append(delay)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    controls = YandexStationControls.__new__(YandexStationControls)
    controls._was_muted = False
    controls._volume = 0

    async def get_volume():
        return 0.5

    async def set_volume(v):
        pass

    controls.get_volume = get_volume
    controls.set_volume = set_volume

    await controls.fade_out_alice_volume()

    assert sum(sleeps) <= 0.6
    assert controls._was_muted


def test_raw_state_line_keeps_player_and_alice_state():
    """Сырой журнал: время прихода, Алиса, плеер; без appState."""
    import json

    from yandex_station.station_ws_control import raw_state_line

    data = {
        "state": {
            "aliceState": "SPEAKING",
            "playing": False,
            "volume": 0.4,
            "playerState": {"id": "43", "entityInfo": {"next": None}},
        },
        "extra": {"appState": "огромный protobuf"},
    }
    line = raw_state_line(data, 12.3456)
    assert line is not None and line.startswith("RAW ")
    record = json.loads(line[4:])
    assert record == {
        "t": 12.346,
        "alice": "SPEAKING",
        "playing": False,
        "volume": 0.4,
        "player": {"id": "43", "entityInfo": {"next": None}},
    }
    assert "appState" not in line
    assert raw_state_line({"requestId": "1"}, 0) is None


def test_raw_station_log_off_by_default():
    from core.config.settings import Settings

    assert Settings().station_raw_log is False
