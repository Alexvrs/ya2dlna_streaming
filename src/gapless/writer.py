"""Писатель бесшовного потока: расписание → непрерывный PCM в темпе."""

from __future__ import annotations

import asyncio
import struct
import time
from logging import getLogger
from typing import AsyncIterator, Callable

from .models import GaplessState, SegmentState
from .pcm_source import FfmpegPcmSource, PcmSource
from .timeline import BYTES_PER_FRAME, CHANNELS, SAMPLE_RATE, Segment, Timeline

logger = getLogger(__name__)

SourceFactory = Callable[[str, str, float], PcmSource]

CHUNK_FRAMES = SAMPLE_RATE // 20  # 50 мс
ICY_METAINT = 16000


def wav_header() -> bytes:
    """Заголовок WAV «бесконечной» длины (0xFFFFFFFF), s16le 44,1/2."""
    byte_rate = SAMPLE_RATE * BYTES_PER_FRAME
    return (
        b"RIFF"
        + struct.pack("<I", 0xFFFFFFFF)
        + b"WAVEfmt "
        + struct.pack(
            "<IHHIIHH",
            16,
            1,
            CHANNELS,
            SAMPLE_RATE,
            byte_rate,
            BYTES_PER_FRAME,
            16,
        )
        + b"data"
        + struct.pack("<I", 0xFFFFFFFF)
    )


def default_factory(
    track_id: str, url: str, start_seconds: float
) -> PcmSource:
    """Создаёт и запускает FFmpeg-декодер."""
    src = FfmpegPcmSource(track_id, url, start_seconds)
    src.start()
    return src


class SourcePool:
    """Декодеры сегментов и предзапущенные декодеры следующих треков."""

    def __init__(self, factory: SourceFactory) -> None:
        self._factory = factory
        self._by_segment: dict[int, PcmSource] = {}
        self._prepared: dict[str, tuple[str, PcmSource]] = {}
        self._urls: dict[str, str] = {}

    def set_url(self, track_id: str, url: str) -> None:
        """Запоминает ссылку трека (для сегментов без своей ссылки)."""
        if url:
            self._urls[track_id] = url

    def prepare(self, track_id: str, url: str) -> None:
        """Предзапускает декодер трека с начала (идемпотентно)."""
        self.set_url(track_id, url)
        old = self._prepared.get(track_id)
        if old is not None and old[0] == url:
            return
        if old is not None:
            old[1].close()
        self._prepared[track_id] = (url, self._factory(track_id, url, 0.0))
        # Держим не больше двух предзапущенных: следующий и запасной
        while len(self._prepared) > 2:
            first = next(iter(self._prepared))
            self._prepared.pop(first)[1].close()

    def source_for(self, seg: Segment, frame: int) -> PcmSource | None:
        """Декодер сегмента: уже открытый, предзапущенный или новый."""
        src = self._by_segment.get(seg.seg_id)
        if src is not None:
            return src
        need = seg.track_frame(frame) * BYTES_PER_FRAME
        prepared = self._prepared.get(seg.track_id)
        if (
            prepared is not None
            and prepared[1].start_offset <= need
            and not prepared[1].eof
        ):
            self._prepared.pop(seg.track_id)
            src = prepared[1]
        else:
            url = seg.meta.get("url") or self._urls.get(seg.track_id)
            if not url:
                return None
            src = self._factory(
                seg.track_id, url, need / BYTES_PER_FRAME / SAMPLE_RATE
            )
        self._by_segment[seg.seg_id] = src
        return src

    def retain(self, seg_ids: set[int]) -> None:
        """Закрывает декодеры сегментов, которых нет в ``seg_ids``."""
        for sid in list(self._by_segment):
            if sid not in seg_ids:
                self._by_segment.pop(sid).close()

    def close(self) -> None:
        """Закрывает все декодеры."""
        self.retain(set())
        for _, src in self._prepared.values():
            src.close()
        self._prepared.clear()


class Renderer:
    """Собирает PCM кадров [f, f+n) по расписанию (без ожиданий)."""

    def __init__(self, timeline: Timeline, pool: SourcePool) -> None:
        self.timeline = timeline
        self.pool = pool
        self.underrun_frames = 0  # тишина там, где по расписанию трек
        self.last_segment: Segment | None = None

    def render(self, frame: int, nframes: int) -> bytes:
        """PCM для кадров [frame, frame + nframes)."""
        tl = self.timeline
        out = bytearray()
        f, end = frame, frame + nframes
        while f < end:
            seg = tl.active_at(f)
            nb = tl.next_boundary(f)
            stop = end if nb is None else min(end, nb)
            n = stop - f
            if seg is None:
                out += bytes(n * BYTES_PER_FRAME)
                f = stop
                continue
            self.last_segment = seg
            src = self.pool.source_for(seg, f)
            if src is None:
                out += bytes(n * BYTES_PER_FRAME)
                self.underrun_frames += n
                f = stop
                continue
            data, ended = src.take(
                seg.track_frame(f) * BYTES_PER_FRAME, n * BYTES_PER_FRAME
            )
            got = len(data) // BYTES_PER_FRAME
            out += data[: got * BYTES_PER_FRAME]
            if got < n and ended:
                tl.mark_eof(seg, f + got)
                f += got
                continue
            if got < n:
                out += bytes((n - got) * BYTES_PER_FRAME)
                self.underrun_frames += n - got
            f = stop
        # Декодер нужен только сегменту, звучащему на конце отрезка;
        # перекрытые (перемотка, скип, кроссфейд) закрываются
        current = tl.active_at(end)
        keep = {s.seg_id for s in tl.segments if s.start_frame > end}
        if current is not None:
            keep.add(current.seg_id)
        self.pool.retain(keep)
        return bytes(out)


class GaplessSession:
    """Одна сессия непрерывного потока (от старта до остановки)."""

    def __init__(
        self,
        lead: float = 1.0,
        margin: float = 2.5,
        factory: SourceFactory = default_factory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.clock = clock
        self.lead = lead
        self.margin = margin
        self.w_start = clock()
        # Кадр f — момент Станции t0 + f/44100. Сдвиг на lead + margin:
        # к записи кадра событие Станции о нём уже пришло (margin покрывает
        # опоздание события и запуск декодера), а OwnTone держит lead
        # секунд впереди слышимого. Задержка D «Станция → колонки» —
        # lead + margin + буфер OwnTone (~3 с), см. план, раздел 0
        self.timeline = Timeline(self.w_start - lead - margin)
        self.pool = SourcePool(factory)
        self.renderer = Renderer(self.timeline, self.pool)
        self.frame = 0  # следующий кадр к записи
        self.client_epoch = 0
        self.client_start_frame: int | None = None  # первый кадр клиента
        self.client_connected = False
        self.closed = False

    # --- команды менеджера -------------------------------------------------

    def add_segment(
        self,
        track_id: str,
        t_station: float,
        pos_seconds: float,
        url: str = "",
        meta: dict[str, str] | None = None,
    ) -> Segment | None:
        """В момент Станции t трек на позиции pos (см. ``Timeline.add``)."""
        self.pool.set_url(track_id, url)
        full = dict(meta or {})
        if url:
            full["url"] = url
        seg = self.timeline.add(track_id, t_station, pos_seconds, full)
        if seg is not None:
            # Декодер — сразу, а не когда писатель дойдёт: запуск FFmpeg
            # и TLS к CDN занимают 1,1–1,5 с (опыт E6). Без клиента —
            # не запускаем: никто не читает, процессы копились бы
            if self.client_connected:
                self.pool.source_for(seg, max(seg.start_frame, self.frame))
            self.pool.retain({s.seg_id for s in self.timeline.segments[-3:]})
            late = (self.frame - seg.start_frame) / SAMPLE_RATE
            logger.info(
                f"🧩 Сегмент #{seg.seg_id} {track_id} с {pos_seconds:.2f}с, "
                f"начало через {-late:+.2f}с от записи"
                + (" (опоздал — с подмоткой)" if late > 0 else "")
            )
        return seg

    def end_segment(self, t_station: float) -> Segment | None:
        """Тишина с момента Станции t (скип с диджеем, пауза)."""
        seg = self.timeline.end_at(t_station)
        if seg is not None:
            logger.info(f"⏹ Сегмент #{seg.seg_id} {seg.track_id} закрыт")
        return seg

    def prepare(self, track_id: str, url: str) -> None:
        """Предзапуск декодера следующего трека."""
        self.pool.prepare(track_id, url)

    def close(self) -> None:
        """Завершает сессию: декодеры закрыты, клиенты отпущены."""
        self.closed = True
        self.client_epoch += 1
        self.pool.close()

    # --- состояние -----------------------------------------------------------

    def station_time_of(self, frame: int) -> float:
        """Момент Станции для кадра потока."""
        return self.timeline.time_at(frame)

    def state(self) -> GaplessState:
        """Состояние для /gapless/state."""
        now_frame = self.timeline.frame_at(self.clock())
        seg = self.timeline.active_at(self.frame)
        return GaplessState(
            closed=self.closed,
            frame=self.frame,
            write_ahead_s=round((self.frame - now_frame) / SAMPLE_RATE, 3),
            client_connected=self.client_connected,
            client_start_frame=self.client_start_frame,
            t0=self.timeline.t0,
            segment=(
                None
                if seg is None
                else SegmentState(
                    seg_id=seg.seg_id,
                    track_id=seg.track_id,
                    pos=round(seg.track_frame(self.frame) / SAMPLE_RATE, 3),
                )
            ),
            underrun_s=round(self.renderer.underrun_frames / SAMPLE_RATE, 3),
        )

    def meta_at(self, frame: int) -> dict[str, str]:
        """Описание трека, звучащего в кадре (для ICY)."""
        seg = self.timeline.active_at(frame)
        return {} if seg is None else seg.meta

    # --- запись ----------------------------------------------------------------

    def _allowed_frame(self) -> int:
        """Сколько кадров можно записать к текущему моменту."""
        return round((self.clock() - self.w_start + self.lead) * SAMPLE_RATE)

    async def pcm(
        self, chunk_frames: int = CHUNK_FRAMES
    ) -> AsyncIterator[bytes]:
        """Бесконечный PCM в темпе; новый клиент вытесняет старого."""
        self.client_epoch += 1
        epoch = self.client_epoch
        # Не догоняем прошлое рывком: иначе OwnTone наберёт лишний буфер
        # и задержка D вырастет
        self.frame = max(
            self.frame, self._allowed_frame() - round(self.lead * SAMPLE_RATE)
        )
        self.client_start_frame = self.frame
        self.client_connected = True
        try:
            while not self.closed and epoch == self.client_epoch:
                # Не быстрее реального времени + lead: иначе OwnTone
                # забирает ~37 с вперёд (опыт E3)
                wait = (self.frame - self._allowed_frame()) / SAMPLE_RATE
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue
                data = self.renderer.render(self.frame, chunk_frames)
                self.frame += chunk_frames
                yield data
                if self.frame % (SAMPLE_RATE * 30) < chunk_frames:
                    self.timeline.prune(self.frame - SAMPLE_RATE * 60)
        finally:
            if epoch == self.client_epoch:
                self.client_connected = False


def icy_title(meta: dict[str, str]) -> str:
    """Строка StreamTitle для ICY: «Исполнитель - Название»."""
    title = meta.get("title", "")
    artist = meta.get("artist", "")
    return f"{artist} - {title}" if artist else title


def icy_block(meta: dict[str, str], cover_url: str = "") -> bytes:
    """Блок метаданных ICY (байт длины/16 + текст, дополненный нулями)."""
    text = "StreamTitle='" + icy_title(meta).replace("'", "’") + "';"
    if cover_url:
        text += f"StreamUrl='{cover_url}';"
    raw = text.encode("utf-8")[: 255 * 16]
    raw += bytes(-len(raw) % 16)
    return bytes([len(raw) // 16]) + raw


class IcyInterleaver:
    """Вставляет метаданные ICY через каждые ``metaint`` байт аудио."""

    def __init__(self, metaint: int = ICY_METAINT) -> None:
        self.metaint = metaint
        self._since = 0
        self._sent: bytes | None = None

    def feed(self, data: bytes, block: bytes) -> bytes:
        """Аудио ``data`` с вставками; ``block`` — текущее описание."""
        out = bytearray()
        while data:
            n = min(len(data), self.metaint - self._since)
            out += data[:n]
            data = data[n:]
            self._since += n
            if self._since == self.metaint:
                self._since = 0
                # Текст только при смене описания, иначе нулевой байт
                # длины, как у интернет-радио
                if block != self._sent:
                    out += block
                    self._sent = block
                else:
                    out += b"\x00"
        return bytes(out)
