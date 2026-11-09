import time
from dataclasses import dataclass, field

from core.enums import TrackType


@dataclass
class Track:
    """Класс для представления музыкального трека."""

    id: str
    title: str
    type: TrackType
    artist: str
    duration: float
    progress: float
    playing: bool
    # Соседние треки очереди станции (entityInfo) — для предзагрузки
    next_id: str | None = None
    prev_id: str | None = None
    # Ссылка на обложку из состояния станции (extra.coverURI), с %%
    cover: str | None = None
    # Момент чтения состояния станции (time.monotonic): к нему привязан
    # progress, поэтому ожидаемая позиция считается от него, а не от
    # конца итерации цикла (долгая итерация выглядела бы как перемотка)
    captured_at: float = field(
        default_factory=lambda: time.monotonic(), compare=False, repr=False
    )

    @property
    def is_radio(self) -> bool:
        """Станция играет радио, а не трек."""
        return self.type == TrackType.FM_RADIO
