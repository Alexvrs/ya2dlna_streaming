import time
from dataclasses import dataclass, field


@dataclass
class Track:
    """Класс для представления музыкального трека."""

    id: str
    title: str
    type: str
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
        default_factory=time.monotonic, compare=False, repr=False
    )
