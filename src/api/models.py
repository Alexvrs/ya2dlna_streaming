"""Модели ответов API сервиса."""

from pydantic import BaseModel

from main_stream_service.main_stream_manager import NextTrack


class StatusReply(BaseModel):
    """Состояние стрима для /status."""

    running: bool
    zone: str | None
    volumes: dict[str, int | None]
    # Следующий трек очереди станции или null (радио, конец очереди)
    next: NextTrack | None


class ZonesReply(BaseModel):
    """Настроенные зоны и текущая зона."""

    zones: dict[str, list[str]]
    current: str | None


class ZoneVolumeReply(BaseModel):
    """Итог команды громкости зоны."""

    status: str
    zone: str
    volume: int
