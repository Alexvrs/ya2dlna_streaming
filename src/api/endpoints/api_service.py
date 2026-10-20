import asyncio
from logging import getLogger

from fastapi import APIRouter, HTTPException, Request

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
