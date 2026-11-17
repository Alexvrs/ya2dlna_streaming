"""Расписание бесшовного потока: сегменты треков на шкале времени Станции."""

from __future__ import annotations

from dataclasses import dataclass, field

# Без asyncio и часов: всё в целых кадрах, стыки считаются точно
SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_FRAME = 4  # s16le, стерео


@dataclass
class Segment:
    """Трек ``track_id`` с позиции ``pos_frame`` от кадра ``start_frame``."""

    seg_id: int  # порядковый номер в сессии
    track_id: str
    start_frame: int  # кадр потока, с которого трек звучит
    pos_frame: int  # позиция трека в кадрах в момент start_frame
    # Явный конец, не включая (скип с диджеем, пауза); None — до EOF
    # или следующего сегмента
    end_frame: int | None = None
    eof_frame: int | None = None  # кадр, где у трека кончились данные
    meta: dict[str, str] = field(default_factory=dict)  # title, artist, …

    def track_frame(self, frame: int) -> int:
        """Позиция трека (кадр) для кадра потока ``frame``."""
        return self.pos_frame + (frame - self.start_frame)

    def stop_frame(self) -> int | None:
        """Ближайший известный конец: явный или EOF."""
        ends = [f for f in (self.end_frame, self.eof_frame) if f is not None]
        return min(ends) if ends else None


class Timeline:
    """Сегменты потока и правило «что звучит в кадре ``f``»."""

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

        Returns:
            Segment | None: None — повтор уже заведённого сегмента.
        """
        start = self.frame_at(t_station)
        pos = max(0, round(pos_seconds * SAMPLE_RATE))
        if pos_seconds < 0:
            # Трек начнётся позже момента t: сдвигаем начало вперёд
            start += round(-pos_seconds * SAMPLE_RATE)
        current = self.active_at(start)
        # Станция шлёт состояние каждую секунду: тот же трек с позицией
        # в пределах допуска ничего не меняет
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
        """Заканчивает сегмент, звучащий в момент Станции t; None — тишина."""
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
        # Звучит последний начавшийся и не закончившийся: новый сегмент
        # (скип, перемотка, кроссфейд Станции) перекрывает предыдущий
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
        """Забывает сегменты, закончившиеся до ``before_frame``."""
        # Последний сегмент остаётся всегда — для повтора и ресинка
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
