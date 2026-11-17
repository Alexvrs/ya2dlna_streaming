import asyncio
from enum import StrEnum
from logging import getLogger

from fastapi import APIRouter, HTTPException, Request
from yandex_music.exceptions import YandexMusicError

from api.exceptions import (
    NotATrackError,
    StreamNotRunningError,
    UnknownPlayerCommandError,
)
from api.models import StatusReply, ZonesReply, ZoneVolumeReply
from core.config.settings import settings
from core.enums import TrackType
from main_stream_service.main_stream_manager import MainStreamManager
from owntone_output.owntone_controller import (
    OwnToneController,
    OwnToneError,
    OwnToneZoneError,
)

logger = getLogger(__name__)

router = APIRouter()

# Ссылки на задачи запуска, чтобы их не собрал сборщик мусора:
# задача держится до своего завершения, повторный запрос её не вытесняет
_start_tasks: set[asyncio.Task[None]] = set()


def _get_manager(request: Request) -> MainStreamManager:
    """Возвращает MainStreamManager, созданный в lifespan приложения."""
    return request.app.state.main_stream_manager


def _spawn_start(manager: MainStreamManager) -> None:
    """Запускает стрим фоновой задачей и держит ссылку до её конца."""
    task = asyncio.create_task(manager.start())
    _start_tasks.add(task)
    task.add_done_callback(_start_tasks.discard)


@router.post("/stream_on")
async def stream_on(request: Request) -> dict[str, str]:
    """API-команда для запуска стрима."""
    _spawn_start(_get_manager(request))
    return {"status": "stream_on"}


@router.post("/shutdown")
async def shutdown(request: Request) -> dict[str, str]:
    """API-команда для остановки стрима."""
    await _get_manager(request).stop()
    return {"status": "main_service stopped"}


@router.get("/status")
async def status(request: Request) -> StatusReply:
    """Состояние стрима: идёт ли, зона (только OwnTone), следующий трек."""
    output = request.app.state.audio_output
    zone = None
    volumes: dict[str, int | None] = {}
    if isinstance(output, OwnToneController):
        zone = output.current_zone()
        running = _get_manager(request).is_running
        for name in output.zones:
            # Выключенный стрим: громкость, с которой зона включится
            if not running and settings.start_volume is not None:
                volumes[name] = output.next_start_volume(
                    name, settings.start_volume
                )
                continue
            try:
                volumes[name] = await output.get_zone_volume(name)
            except OwnToneError:
                volumes[name] = None
    manager = _get_manager(request)
    return StatusReply(
        running=manager.is_running,
        zone=zone,
        volumes=volumes,
        next=await manager.next_track_info(),
    )


def _get_owntone(request: Request) -> OwnToneController:
    """Возвращает контроллер OwnTone или 404, если вывод не через OwnTone."""
    output = request.app.state.audio_output
    if not isinstance(output, OwnToneController):
        raise HTTPException(
            status_code=404,
            detail="Зоны доступны только при APP_OUTPUT_BACKEND=owntone",
        )
    return output


@router.get("/zones")
async def zones(request: Request) -> ZonesReply:
    """Список зон и текущая зона."""
    owntone = _get_owntone(request)
    return ZonesReply(zones=owntone.zones, current=owntone.current_zone())


@router.post("/zone/{zone}")
async def select_zone(request: Request, zone: str) -> dict[str, str | None]:
    """Переключает зону воспроизведения; если музыка играет — на лету."""
    owntone = _get_owntone(request)
    try:
        await owntone.select_zone(zone)
    except OwnToneZoneError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except OwnToneError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"status": "zone_selected", "current": owntone.current_zone()}


async def zone_on(
    manager: MainStreamManager, owntone: OwnToneController, zone: str
) -> str:
    """Включает стрим в зоне: выбирает её и запускает стрим, если он стоит.

    Returns:
        str: Что сделано: started | switched | unchanged.
    """
    # Повтор для играющей зоны ничего не меняет: эхо синхронизации
    # переключателей (Алиса, Apple Home) безвредно
    previous = owntone.current_zone()
    await owntone.select_zone(zone)
    if not manager.is_running:
        _spawn_start(manager)
        return "started"
    return "unchanged" if previous == zone else "switched"


async def zone_off(
    manager: MainStreamManager, owntone: OwnToneController, zone: str
) -> str:
    """Выключает стрим, только если он играет именно в этой зоне.

    Returns:
        str: Что сделано: stopped | unchanged.
    """
    if zone not in owntone.zones:
        raise OwnToneZoneError(f"Неизвестная зона «{zone}»")
    if manager.is_running and owntone.current_zone() == zone:
        await manager.stop()
        return "stopped"
    return "unchanged"


@router.post("/zone/{zone}/on")
async def zone_on_endpoint(
    request: Request, zone: str
) -> dict[str, str | None]:
    """Включает стрим в зоне (для переключателей Алисы и Apple Home)."""
    owntone = _get_owntone(request)
    try:
        result = await zone_on(_get_manager(request), owntone, zone)
    except OwnToneZoneError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except OwnToneError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"status": result, "zone": owntone.current_zone()}


@router.post("/zone/{zone}/off")
async def zone_off_endpoint(
    request: Request, zone: str
) -> dict[str, str | None]:
    """Выключает стрим, если он играет в этой зоне."""
    owntone = _get_owntone(request)
    try:
        result = await zone_off(_get_manager(request), owntone, zone)
    except OwnToneZoneError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"status": result, "zone": owntone.current_zone()}


@router.post("/zone/{zone}/volume/{volume}")
async def zone_volume(
    request: Request, zone: str, volume: int
) -> ZoneVolumeReply:
    """Громкость колонок зоны, 0–100 (не выше APP_OWNTONE_MAX_VOLUME)."""
    owntone = _get_owntone(request)
    manager = _get_manager(request)
    playing_here = manager.is_running and owntone.current_zone() == zone
    try:
        if not playing_here and settings.start_volume is not None:
            # Стрим в зоне не играет: громкость станет стартовой
            level = owntone.remember_start_volume(zone, volume)
            return ZoneVolumeReply(
                status="start_volume_set", zone=zone, volume=level
            )
        level = await owntone.set_zone_volume(zone, volume)
    except OwnToneZoneError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except OwnToneError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return ZoneVolumeReply(status="volume_set", zone=zone, volume=level)


class PlayerAction(StrEnum):
    """Команды плееру Станции с пульта."""

    NEXT = "next"
    PREV = "prev"
    PLAY_PAUSE = "play_pause"
    LIKE = "like"
    DISLIKE = "dislike"
    DJ_ON = "dj_on"
    DJ_OFF = "dj_off"


# Режим диджея «Моей волны»: отдельной команды glagol нет, Алиса
# переключает его по фразе (проверено на Станции 07.11.2026; «включи
# диджея» и «включи кроссфейд» не срабатывают). Состояние не читается.
DJ_PHRASES = {
    PlayerAction.DJ_ON: "включи режим диджей",
    PlayerAction.DJ_OFF: "выключи режим диджей",
}


async def player_command(manager: MainStreamManager, action: str) -> str:
    """Отдаёт команду плееру Станции с пульта (ресивер «Стрим»).

    Returns:
        str: Что сделано: команда, liked, disliked, paused или playing.
    Raises:
        UnknownPlayerCommandError: Неизвестная команда.
        StreamNotRunningError: Стрим не идёт — Станцией управляет она сама.
        NotATrackError: Лайк или дизлайк, а играет не трек.
    """
    try:
        command = PlayerAction(action)
    except ValueError as e:
        raise UnknownPlayerCommandError(
            f"Неизвестная команда «{action}»"
        ) from e
    if not manager.is_running:
        raise StreamNotRunningError("Стрим не идёт")
    # Стрим сам следует за Станцией: смену трека и паузу подхватывает
    # цикл менеджера, поэтому достаточно команды Станции
    station = manager.station_controls
    match command:
        case PlayerAction.NEXT:
            await station.next()
            return command
        case PlayerAction.PREV:
            await station.prev()
            return command
        case PlayerAction.DJ_ON | PlayerAction.DJ_OFF:
            await station.send_text(DJ_PHRASES[command])
            return command
    track = await station.get_current_track()
    if command in (PlayerAction.LIKE, PlayerAction.DISLIKE):
        if track is None or track.type != TrackType.TRACK or not track.id:
            raise NotATrackError("Сейчас играет не трек")
        music = manager.yandex_music_api
        if command == PlayerAction.LIKE:
            await music.like_track(track.id)
            return "liked"
        # После дизлайка — следующий трек, как в самой Музыке
        await music.dislike_track(track.id)
        await station.next()
        return "disliked"
    # Пауза или продолжение — по тому, играет ли Станция сейчас
    if track is not None and track.playing:
        await station.stop()
        return "paused"
    await station.play()
    return "playing"


@router.post("/player/{action}")
async def player_endpoint(request: Request, action: str) -> dict[str, str]:
    """Команды плееру Станции: next, prev, play_pause, like, dislike, dj_*."""
    try:
        result = await player_command(_get_manager(request), action)
    except UnknownPlayerCommandError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (StreamNotRunningError, NotATrackError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except YandexMusicError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"status": result}
