"""Модели запросов и ответов HTTP бесшовного режима (/gapless/*)."""

from pydantic import BaseModel


class StartBody(BaseModel):
    """Запрос новой сессии; None — значение из настроек."""

    lead: float | None = None
    margin: float | None = None


class SegmentBody(BaseModel):
    """В момент Станции t_station трек был на позиции pos."""

    track_id: str
    t_station: float
    pos: float
    url: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    cover: str = ""


class EndBody(BaseModel):
    """Тишина с момента Станции t_station."""

    t_station: float


class PrepareBody(BaseModel):
    """Предзапуск декодера следующего трека."""

    track_id: str
    url: str
    cover: str = ""


class StartReply(BaseModel):
    """Имя новой сессии, путь потока и якоря времени."""

    session: str
    stream_path: str
    t0: float
    w_start: float


class SegmentReply(BaseModel):
    """Добавлен ли сегмент (повтор того же состояния — нет)."""

    added: bool
    seg_id: int | None = None


class EndReply(BaseModel):
    """Номер закрытого сегмента или None (в этот момент тишина)."""

    ended: int | None = None


class OkReply(BaseModel):
    """Команда выполнена."""

    ok: bool = True


class SegmentState(BaseModel):
    """Сегмент, который пишется сейчас."""

    seg_id: int
    track_id: str
    pos: float


class GaplessState(BaseModel):
    """Состояние сессии для /gapless/state."""

    closed: bool = False
    session: str = ""
    frame: int = 0
    write_ahead_s: float = 0.0
    client_connected: bool = False
    client_start_frame: int | None = None
    t0: float | None = None
    segment: SegmentState | None = None
    underrun_s: float = 0.0
