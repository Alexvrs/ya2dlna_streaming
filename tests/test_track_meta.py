from unittest.mock import AsyncMock, MagicMock

from main_stream_service.main_stream_manager import MainStreamManager
from main_stream_service.yandex_music_api import YandexMusicAPI
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller
from yandex_station.models import Track
from yandex_station.station_controls import YandexStationControls


def make(
    meta=("Live in Moscow", "avatars.yandex.net/get-music-content/1/a/%%")
):
    api = AsyncMock(spec=YandexMusicAPI)
    api.get_track_meta.return_value = meta
    manager = MainStreamManager(
        station_ws_client=MagicMock(),
        station_controls=AsyncMock(spec=YandexStationControls),
        ruark_controls=AsyncMock(spec=RuarkR5Controller),
        yandex_music_api=api,
    )
    return manager, api


def track(cover=None, type_="Track", id_="35722550"):
    return Track(
        id=id_,
        title="Metal Heart",
        type=type_,
        artist="Dirkschneider",
        duration=520,
        progress=0,
        playing=True,
        cover=cover,
    )


async def test_album_and_cover_from_music_api_cached():
    manager, api = make()
    meta = await manager._track_meta(track())
    assert meta == {
        "album": "Live in Moscow",
        "cover": "https://avatars.yandex.net/get-music-content/1/a/400x400",
    }
    await manager._track_meta(track())
    api.get_track_meta.assert_awaited_once()


async def test_station_cover_preferred():
    manager, _ = make()
    meta = await manager._track_meta(track(cover="avatars.yandex.net/st/%%"))
    assert meta["cover"] == "https://avatars.yandex.net/st/400x400"
    assert meta["album"] == "Live in Moscow"


async def test_radio_uses_station_cover_without_api():
    manager, api = make()
    meta = await manager._track_meta(
        track(cover="avatars.yandex.net/radio/%%", type_="FmRadio")
    )
    assert meta == {
        "album": "",
        "cover": "https://avatars.yandex.net/radio/400x400",
    }
    api.get_track_meta.assert_not_called()


async def test_api_failure_does_not_break_stream():
    manager, api = make()
    api.get_track_meta.side_effect = RuntimeError("сеть")
    assert await manager._track_meta(track()) == {"album": "", "cover": ""}
