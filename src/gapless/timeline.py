"""Расписание бесшовного потока: сегменты треков на шкале времени Станции.

Поток — задержанная копия шкалы Станции. Кадр потока ``f`` (стерео, 16 бит)
соответствует моменту Станции ``t0 + f / SAMPLE_RATE``. Сегмент говорит:
«с момента Станции ``start_frame`` играет трек ``track_id`` с позиции
``pos_frame``». Сегмент заканчивается следующим сегментом, явным концом
(``end_frame`` — скип с диджеем, пауза) или концом файла трека (EOF
декодера). Всё, что не покрыто сегментами, — тишина.

Модуль без asyncio и без часов: всё в целых кадрах, поэтому стыки
считаются точно и проверяются простыми тестами.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_FRAME = 4  # s16le, стерео


@dataclass
class Segment:
    """Трек на шкале потока.

    Attributes:
        seg_id: Порядковый номер сегмента в сессии.
        track_id: Идентификатор трека Станции.
        start_frame: Кадр потока, с которого трек звучит.
        pos_frame: Позиция трека (в кадрах) в момент ``start_frame``.
        end_frame: Явный конец (не включая), None — до EOF/следующего.
        eof_frame: Кадр потока, на котором у трека кончились данные.
        meta: Описание для ICY: title, artist, album, cover.
    """

    seg_id: int
    track_id: str
    start_frame: int
    pos_frame: int
    end_frame: int | None = None
    eof_frame: int | None = None
    meta: dict[str, str] = field(default_factory=dict)

    def track_frame(self, frame: int) -> int:
        """Позиция трека (кадр) для кадра потока ``frame``."""
        return self.pos_frame + (frame - self.start_frame)

    def stop_frame(self) -> int | None:
        """Ближайший известный конец: явный или EOF."""
        ends = [f for f in (self.end_frame, self.eof_frame) if f is not None]
        return min(ends) if ends else None


class Timeline:
    """Список сегментов и правило «что звучит в кадре ``f``».

    Звучит последний по времени начала сегмент, который уже начался
    и ещё не закончился. Новый сегмент, начавшийся раньше конца
    предыдущего (скип, перемотка, кроссфейд Станции), перекрывает его.
    """

    def __init__(self, t0: float) -> None:
        self.t0 = t0
        self._segments: list[Segment] = []
        self._next_id = 1

    @property
    def segments(self) -> list[Segment]:
        """Сегменты по возрастанию начала (копия)."""
        return list(self._segments)

    def frame_at(self, t_station: float) -> int:
        """Кадр потока для момента Станции."""
        return round((t_station - self.t0) * SAMPLE_RATE)

    def time_at(self, frame: int) -> float:
        """Момент Станции для кадра потока."""
        return self.t0 + frame / SAMPLE_RATE

    def add(
        self,
        track_id: str,
        t_station: float,
        pos_seconds: float,
        meta: dict[str, str] | None = None,
        tolerance_frames: int = SAMPLE_RATE // 2,
    ) -> Segment | None:
        """Добавляет сегмент «в момент Станции t трек был на позиции p».

        Повтор того же трека, согласованный с уже заведённым сегментом
        (позиция в пределах ``tolerance_frames``), ничего не меняет —
        Станция шлёт состояние каждую секунду.

        Returns:
            Новый сегмент или None, если это повтор существующего.
        """
        start = self.frame_at(t_station)
        pos = max(0, round(pos_seconds * SAMPLE_RATE))
        if pos_seconds < 0:
            # Трек начнётся позже момента t: сдвигаем начало вперёд
            start += round(-pos_seconds * SAMPLE_RATE)
        current = self.active_at(start)
        if current is not None and current.track_id == track_id:
            if abs(current.track_frame(start) - pos) <= tolerance_frames:
                return None
        seg = Segment(
            self._next_id, track_id, start, pos, meta=dict(meta or {})
        )
        self._next_id += 1
        self._segments.append(seg)
        self._segments.sort(key=lambda s: (s.start_frame, s.seg_id))
        return seg

    def end_at(self, t_station: float) -> Segment | None:
        """Явно заканчивает сегмент, звучащий в момент Станции t.

        Returns:
            Закрытый сегмент или None, если в этот момент тишина.
        """
        frame = self.frame_at(t_station)
        seg = self.active_at(frame)
        # Сегменты, начатые позже этого момента (перемотка в последние
        # секунды), тоже не должны звучать
        for later in self._segments:
            if later.start_frame > frame:
                later.end_frame = later.start_frame
        if seg is None:
            return None
        if seg.end_frame is None or seg.end_frame > frame:
            seg.end_frame = frame
        return seg

    def mark_eof(self, seg: Segment, frame: int) -> None:
        """Отмечает, что данные трека кончились на кадре ``frame``."""
        if seg.eof_frame is None or frame < seg.eof_frame:
            seg.eof_frame = frame

    def active_at(self, frame: int) -> Segment | None:
        """Сегмент, звучащий в кадре ``frame``, или None (тишина)."""
        for seg in reversed(self._segments):
            if seg.start_frame > frame:
                continue
            stop = seg.stop_frame()
            if stop is not None and frame >= stop:
                # Закончился — раньше начатые тоже не звучат: их
                # перекрыл этот сегмент
                return None
            return seg
        return None

    def next_boundary(self, frame: int) -> int | None:
        """Ближайший кадр > ``frame``, где может смениться звучащее."""
        cands: list[int] = []
        for seg in self._segments:
            if seg.start_frame > frame:
                cands.append(seg.start_frame)
            stop = seg.stop_frame()
            if stop is not None and stop > frame:
                cands.append(stop)
        return min(cands) if cands else None

    def prune(self, before_frame: int) -> None:
        """Забывает сегменты, закончившиеся до ``before_frame``.

        Оставляет хотя бы один сегмент — для повтора и ресинка.
        """
        keep: list[Segment] = []
        for i, seg in enumerate(self._segments):
            later = i + 1
            later_started = any(
                s.start_frame <= before_frame for s in self._segments[later:]
            )
            stop = seg.stop_frame()
            finished = stop is not None and stop <= before_frame
            if (finished or later_started) and i < len(self._segments) - 1:
                continue
            keep.append(seg)
        self._segments = keep
