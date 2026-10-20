from contextlib import asynccontextmanager
from logging import getLogger
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from api.endpoints.routers import main_router
from core.audio_output import AudioOutput
from core.config.settings import settings
from core.dependencies.main_di_container import MainDIContainer
from core.logging.setup import setup_logging
from main_stream_service.main_stream_manager import MainStreamManager
from owntone_output.linked_volume import LinkedVolumeMirror

setup_logging("api")

logger = getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Создание DI-контейнера и менеджера стриминга при старте."""
    container = MainDIContainer().get_container()
    app.state.main_stream_manager = container.get(MainStreamManager)
    app.state.audio_output = container.get(
        AudioOutput  # type: ignore[type-abstract]
    )
    mirror = LinkedVolumeMirror(settings.linked_speakers)
    mirror.start()
    logger.info("✅ Зависимости API сервиса инициализированы")
    yield
    await mirror.stop()


app = FastAPI(lifespan=lifespan)

app.include_router(main_router)


def main():
    """Запускает API сервер управления стримингом."""
    logger.info("▶️ Запуск API сервера...")
    uvicorn.run(
        app,
        host=settings.local_server_host,
        port=settings.local_server_port_api,
        # Node-RED опрашивает /status каждые 10 с по одному соединению:
        # при штатных 5 с запрос попадал на закрытие («socket hang up»)
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
