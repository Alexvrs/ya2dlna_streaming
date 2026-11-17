"""Модели ответов JSON API OwnTone."""

from pydantic import BaseModel, ConfigDict


class OwnToneOutput(BaseModel):
    """Выход OwnTone: колонка AirPlay, ALSA и т. п."""

    # OwnTone отдаёт id строкой, но в очереди — числом: приводим к str
    model_config = ConfigDict(coerce_numbers_to_str=True)

    id: str
    name: str
    type: str = ""
    selected: bool = False
    volume: int = 0


class OutputsReply(BaseModel):
    """Ответ GET /api/outputs."""

    outputs: list[OwnToneOutput] = []


class OwnTonePlayer(BaseModel):
    """Ответ GET /api/player."""

    state: str = ""
    volume: int = 0
    item_progress_ms: int | None = None


class QueueItem(BaseModel):
    """Элемент очереди OwnTone."""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    id: str


class QueueAddReply(BaseModel):
    """Ответ POST /api/queue/items/add."""

    items: list[QueueItem] = []
