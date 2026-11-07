import asyncio
from logging import getLogger

from fastapi import APIRouter, HTTPException, Request
from yandex_music.exceptions import YandexMusicError

from core.config.settings import settings
from main_stream_service.main_stream_manager import MainStreamManager
from owntone_output.owntone_controller import (
    OwnToneController,
    OwnToneError,
    OwnToneZoneError,
)

logger = getLogger(__name__)

router = APIRouter()

# Ссылка на задачу запуска, чтобы её не собрал сборщик мусора
_start_task: asyncio.Task[None] | None = None


def _get_manager(request: Request) -> MainStreamManager:
    """Возвращает MainStreamManager, созданный в lifespan приложения."""
    return request.app.state.main_stream_manager


@router.post("/stream_on")
async def stream_on(request: Request):
    """API-команда для запуска стрима."""
    global _start_task
    _start_task = asyncio.create_task(_get_manager(request).start())
    return {"status": "stream_on"}


@router.post("/shutdown")
async def shutdown(request: Request):
    """API-команда для остановки стрима."""
    await _get_manager(request).stop()
    return {"status": "main_service stopped"}


@router.get("/status")
async def status(request: Request):
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
    return {
        "running": manager.is_running,
        "zone": zone,
        "volumes": volumes,
        # Следующий трек очереди станции или null (радио, конец очереди)
        "next": await manager.next_track_info(),
    }


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
async def zones(request: Request):
    """Список зон и текущая зона."""
    owntone = _get_owntone(request)
    return {"zones": owntone.zones, "current": owntone.current_zone()}


@router.post("/zone/{zone}")
async def select_zone(request: Request, zone: str):
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

    Повтор для уже играющей зоны ничего не меняет — эхо от синхронизации
    переключателей (Алиса, Apple Home) безвредно.

    Returns:
        str: Что сделано: started | switched | unchanged.
    """
    previous = owntone.current_zone()
    await owntone.select_zone(zone)
    if not manager.is_running:
        global _start_task
        _start_task = asyncio.create_task(manager.start())
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
async def zone_on_endpoint(request: Request, zone: str):
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
async def zone_off_endpoint(request: Request, zone: str):
    """Выключает стрим, если он играет в этой зоне."""
    owntone = _get_owntone(request)
    try:
        result = await zone_off(_get_manager(request), owntone, zone)
    except OwnToneZoneError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"status": result, "zone": owntone.current_zone()}


@router.post("/zone/{zone}/volume/{volume}")
async def zone_volume(request: Request, zone: str, volume: int):
    """Громкость колонок зоны, 0–100 (не выше APP_OWNTONE_MAX_VOLUME)."""
    owntone = _get_owntone(request)
    manager = _get_manager(request)
    playing_here = manager.is_running and owntone.current_zone() == zone
    try:
        if not playing_here and settings.start_volume is not None:
            # Стрим в зоне не играет: громкость станет стартовой
            level = owntone.remember_start_volume(zone, volume)
            return {
                "status": "start_volume_set",
                "zone": zone,
                "volume": level,
            }
        level = await owntone.set_zone_volume(zone, volume)
    except OwnToneZoneError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except OwnToneError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"status": "volume_set", "zone": zone, "volume": level}


PLAYER_ACTIONS = (
    "next",
    "prev",
    "play_pause",
    "like",
    "dislike",
    "dj_on",
    "dj_off",
)

# Режим диджея «Моей волны»: отдельной команды glagol нет, Алиса
# переключает его по фразе (проверено на Станции 07.11.2026; «включи
# диджея» и «включи кроссфейд» не срабатывают). Состояние не читается.
DJ_PHRASES = {
    "dj_on": "включи режим диджей",
    "dj_off": "выключи режим диджей",
}


async def player_command(manager: MainStreamManager, action: str) -> str:
    """Команда плееру Станции с пульта (ресивер «Стрим» в Apple Home).

    Стрим сам следует за Станцией: смена трека, пауза и продолжение
    подхватываются циклом менеджера, поэтому достаточно отдать команду
    Станции. Пауза/продолжение — по тому, играет ли Станция сейчас.
    Лайк и дизлайк — текущему треку в аккаунте Яндекс Музыки; после
    дизлайка — следующий трек, как в самой Музыке.

    Raises:
        ValueError: неизвестная команда.
        RuntimeError: стрим не идёт — Станцией управляет она сама.
        LookupError: лайк/дизлайк, а сейчас играет не трек (радио).
    """
    if action not in PLAYER_ACTIONS:
        raise ValueError(f"Неизвестная команда «{action}»")
    if not manager.is_running:
        raise RuntimeError("Стрим не идёт")
    station = manager.station_controls
    if action == "next":
        await station.next()
        return "next"
    if action == "prev":
        await station.prev()
        return "prev"
    if action in DJ_PHRASES:
        await station.send_text(DJ_PHRASES[action])
        return action
    track = await station.get_current_track()
    if action in ("like", "dislike"):
        if track is None or track.type != "Track" or not track.id:
            raise LookupError("Сейчас играет не трек")
        music = manager.yandex_music_api
        if action == "like":
            await music.like_track(track.id)
            return "liked"
        await music.dislike_track(track.id)
        await station.next()
        return "disliked"
    if track is not None and track.playing:
        await station.stop()
        return "paused"
    await station.play()
    return "playing"


@router.post("/player/{action}")
async def player_endpoint(request: Request, action: str):
    """Команды плееру Станции с пульта.

    Трек и пауза: next, prev, play_pause; оценка: like, dislike;
    режим диджея «Моей волны»: dj_on, dj_off.
    """
    try:
        result = await player_command(_get_manager(request), action)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (RuntimeError, LookupError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except YandexMusicError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"status": result}
