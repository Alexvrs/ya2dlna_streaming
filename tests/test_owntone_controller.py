import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import owntone_output.owntone_controller as oc
from owntone_output.owntone_controller import (
    OwnToneController,
    OwnToneError,
    OwnToneZoneError,
)
from owntone_output.zone_store import StartVolumeStore, ZoneStore

ZONES = {
    "Обитель": ["Колонка"],
    "Спальня": ["Спальня"],
    "Весь дом": ["Колонка", "Спальня"],
}
STREAM_URL = "http://192.168.1.10:8080/live_stream.mp3?radio=false"


class FakeOwnTone:
    """Минимальный JSON API OwnTone в памяти."""

    def __init__(self) -> None:
        self.outputs: list[dict[str, Any]] = [
            {"id": "1", "name": "Колонка", "type": "AirPlay 2"},
            {"id": "2", "name": "Спальня", "type": "AirPlay 2"},
            {"id": "0", "name": "Computer", "type": "ALSA"},
        ]
        for o in self.outputs:
            o.update(selected=False, volume=100)
        self.state = "stop"
        self.volume = 50
        self.queue: list[str] = []
        # Громкость, которую колонка «сообщит» после старта (как MUZO)
        self.volume_after_start: int | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/outputs", self.get_outputs)
        app.router.add_put("/api/outputs/set", self.set_outputs)
        app.router.add_put("/api/outputs/{id}", self.put_output)
        app.router.add_get("/api/player", self.get_player)
        app.router.add_put("/api/player/volume", self.put_volume)
        app.router.add_put("/api/player/stop", self.put_stop)
        app.router.add_put("/api/queue/clear", self.put_clear)
        app.router.add_post("/api/queue/items/add", self.post_add)
        return app

    def by_id(self, output_id: str) -> dict[str, Any]:
        return next(o for o in self.outputs if o["id"] == output_id)

    async def get_outputs(self, request: web.Request) -> web.Response:
        return web.json_response({"outputs": self.outputs})

    async def set_outputs(self, request: web.Request) -> web.Response:
        ids = (await request.json())["outputs"]
        for o in self.outputs:
            o["selected"] = o["id"] in ids
        return web.Response(status=204)

    async def put_output(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.by_id(request.match_info["id"]).update(body)
        return web.Response(status=204)

    async def get_player(self, request: web.Request) -> web.Response:
        return web.json_response({"state": self.state, "volume": self.volume})

    async def put_volume(self, request: web.Request) -> web.Response:
        self.volume = int(request.query["volume"])
        return web.Response(status=204)

    async def put_stop(self, request: web.Request) -> web.Response:
        self.state = "stop"
        return web.Response(status=204)

    async def put_clear(self, request: web.Request) -> web.Response:
        self.queue.clear()
        return web.Response(status=204)

    async def post_add(self, request: web.Request) -> web.Response:
        self.queue.append(request.query["uris"])
        if request.query.get("playback") == "start":
            self.state = "play"
            if self.volume_after_start is not None:
                for o in self.outputs:
                    if o["selected"]:
                        o["volume"] = self.volume_after_start
        return web.json_response({"count": 1})


@pytest.fixture(autouse=True)
async def instant_volume_recheck(monkeypatch):
    """Повторная проверка громкости без паузы, завершается внутри теста."""
    monkeypatch.setattr(oc, "VOLUME_RECHECK_DELAY", 0.0)
    monkeypatch.setattr(oc, "START_VOLUME_PIN_DELAYS", (0.0, 0.01))
    monkeypatch.setattr(oc, "START_VOLUME_WAIT", 0.0)
    yield
    await asyncio.sleep(0.05)


@pytest.fixture
async def fake():
    fake = FakeOwnTone()
    server = TestServer(fake.app())
    await server.start_server()
    fake.url = str(server.make_url(""))  # type: ignore[attr-defined]
    yield fake
    await server.close()


def make_controller(
    fake: FakeOwnTone, tmp_path: Path, **kwargs: Any
) -> OwnToneController:
    return OwnToneController(
        base_url=fake.url,  # type: ignore[attr-defined]
        zones=ZONES,
        default_zone="Обитель",
        max_volume=30,
        zone_store=ZoneStore(tmp_path / "zone.txt"),
        start_volume_store=StartVolumeStore(tmp_path / "start.json"),
        **kwargs,
    )


async def test_play_selects_zone_and_caps_volume(fake, tmp_path):
    ctl = make_controller(fake, tmp_path)
    await ctl.set_av_transport_uri(STREAM_URL, title="t", artist="a")
    await ctl.play()

    assert fake.queue == [STREAM_URL]
    assert fake.state == "play"
    assert fake.by_id("1")["selected"] is True
    assert fake.by_id("2")["selected"] is False
    # Громкость колонки 100 опущена до потолка до старта звука
    assert fake.by_id("1")["volume"] == 30


async def test_volume_rechecked_after_start(fake, tmp_path):
    fake.volume_after_start = 100
    ctl = make_controller(fake, tmp_path)
    await ctl.set_av_transport_uri(STREAM_URL)
    await ctl.play()
    assert ctl._recheck_task is not None
    await ctl._recheck_task

    assert fake.by_id("1")["volume"] == 30


async def test_play_without_url_fails(fake, tmp_path):
    ctl = make_controller(fake, tmp_path)
    with pytest.raises(OwnToneError):
        await ctl.play()


async def test_zone_without_outputs_fails(fake, tmp_path):
    fake.outputs = [o for o in fake.outputs if o["name"] != "Колонка"]
    ctl = make_controller(fake, tmp_path)
    await ctl.set_av_transport_uri(STREAM_URL)

    assert await ctl.connect(attempts=1) is False
    with pytest.raises(OwnToneZoneError):
        await ctl.play()


async def test_set_volume_is_clamped(fake, tmp_path):
    ctl = make_controller(fake, tmp_path)
    await ctl.set_volume(90)
    assert fake.volume == 30
    await ctl.set_volume(-5)
    assert fake.volume == 0


async def test_select_zone_switches_live(fake, tmp_path):
    ctl = make_controller(fake, tmp_path)
    await ctl.set_av_transport_uri(STREAM_URL)
    await ctl.play()

    await ctl.select_zone("Весь дом")

    assert ctl.current_zone() == "Весь дом"
    assert fake.by_id("1")["selected"] is True
    assert fake.by_id("2")["selected"] is True
    assert fake.by_id("2")["volume"] == 30


async def test_select_zone_when_stopped_only_saves(fake, tmp_path):
    ctl = make_controller(fake, tmp_path)
    await ctl.select_zone("Спальня")

    assert ctl.current_zone() == "Спальня"
    assert not any(o["selected"] for o in fake.outputs)
    # Другой процесс (DLNA-сервер) видит ту же зону через файл
    assert make_controller(fake, tmp_path).current_zone() == "Спальня"


async def test_unknown_zone_rejected(fake, tmp_path):
    ctl = make_controller(fake, tmp_path)
    with pytest.raises(OwnToneZoneError):
        await ctl.select_zone("Кухня")


async def test_power_follows_zone_selection(fake, tmp_path):
    ctl = make_controller(fake, tmp_path)
    assert await ctl.get_power_status() == "0"

    await ctl.turn_power_on()
    assert await ctl.get_power_status() == "1"

    await ctl.set_av_transport_uri(STREAM_URL)
    await ctl.play()
    assert await ctl.is_playing() is True

    await ctl.turn_power_off()
    assert fake.state == "stop"
    assert not any(o["selected"] for o in fake.outputs)
    assert await ctl.is_playing() is False


async def test_unreachable_owntone(tmp_path):
    ctl = OwnToneController(
        base_url="http://127.0.0.1:9",
        zones=ZONES,
        zone_store=ZoneStore(tmp_path / "zone.txt"),
        timeout=1.0,
    )
    assert await ctl.connect(attempts=1) is False
    assert await ctl.is_playing() is False
    with pytest.raises(OwnToneError):
        await asyncio.wait_for(ctl.get_volume(), 5)


async def test_zone_volume_sets_all_zone_outputs_capped(fake, tmp_path):
    ctl = make_controller(fake, tmp_path)
    assert await ctl.set_zone_volume("Весь дом", 25) == 25
    assert fake.by_id("1")["volume"] == 25
    assert fake.by_id("2")["volume"] == 25
    # Выше потолка (30) не поднимается
    assert await ctl.set_zone_volume("Обитель", 80) == 30
    assert await ctl.get_zone_volume("Обитель") == 30
    assert fake.by_id("2")["volume"] == 25


async def test_zone_volume_unknown_zone(fake, tmp_path):
    ctl = make_controller(fake, tmp_path)
    with pytest.raises(OwnToneZoneError):
        await ctl.set_zone_volume("Кухня", 10)
    with pytest.raises(OwnToneZoneError):
        await ctl.get_zone_volume("Кухня")


async def test_start_volume_applied_and_pinned_after_connect(fake, tmp_path):
    fake.volume_after_start = 8  # колонка навязывает свою громкость
    oc.START_VOLUME_WAIT = 5.0
    api_ctl = make_controller(fake, tmp_path)  # процесс API
    dlna_ctl = make_controller(fake, tmp_path)  # процесс DLNA-сервера
    await api_ctl.set_start_volume(24)
    assert fake.by_id("1")["volume"] == 24
    await dlna_ctl.set_av_transport_uri(STREAM_URL)
    await dlna_ctl.play()
    assert fake.by_id("1")["volume"] == 8
    assert api_ctl._pin_task is not None
    await api_ctl._pin_task
    assert fake.by_id("1")["volume"] == 24
    # Следующий трек сеанса громкость не трогает (только потолок)
    fake.by_id("1")["volume"] = 25
    fake.volume_after_start = None
    await dlna_ctl.play()
    await dlna_ctl._recheck_task
    assert fake.by_id("1")["volume"] == 25


async def test_volume_set_while_stopped_becomes_one_time_start(fake, tmp_path):
    ctl = make_controller(fake, tmp_path)
    assert ctl.next_start_volume("Обитель", 10) == 10
    assert ctl.remember_start_volume("Обитель", 25) == 25
    assert ctl.next_start_volume("Обитель", 10) == 25
    # Другая зона не затронута
    assert ctl.next_start_volume("Спальня", 10) == 10

    assert await ctl.set_start_volume(10) == 25
    assert fake.by_id("1")["volume"] == 25
    # Одноразово: следующее включение — снова по умолчанию
    assert ctl.next_start_volume("Обитель", 10) == 10
    assert await ctl.set_start_volume(10) == 10


async def test_remember_start_volume_unknown_zone(fake, tmp_path):
    ctl = make_controller(fake, tmp_path)
    with pytest.raises(OwnToneZoneError):
        ctl.remember_start_volume("Кухня", 20)
