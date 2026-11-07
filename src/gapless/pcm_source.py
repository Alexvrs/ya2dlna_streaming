"""Источник PCM трека: FFmpeg-декодер в s16le 44,1 кГц стерео с буфером.

Писатель потока не ждёт декодер: он спрашивает «дай байты трека с
позиции X» и берёт то, что уже есть (``take``). Чего нет — писатель
заменяет тишиной, а при следующем запросе декодер отдаёт данные уже
с новой позиции, сбросив пропущенное. Так опоздание декодера даёт
короткую тишину, но не сдвигает трек относительно Станции.

Фоновая задача читает stdout FFmpeg в ограниченный буфер; когда буфер
полон, чтение встаёт, и FFmpeg блокируется на записи в pipe — память
не растёт. Сброс вперёд (подмотка) идёт без ограничения буфера:
декодирование на Pi в разы быстрее реального времени.
"""

from __future__ import annotations

import asyncio
import time
from logging import getLogger
from typing import Protocol, Sequence

from .timeline import BYTES_PER_FRAME, SAMPLE_RATE

logger = getLogger(__name__)

# Сколько держать в буфере впереди запрошенного (байт): 2 с
DEFAULT_BUFFER_BYTES = 2 * SAMPLE_RATE * BYTES_PER_FRAME
READ_CHUNK = 64 * 1024


def decoder_args(url: str, start_seconds: float) -> list[str]:
    """Аргументы FFmpeg: любой вход → s16le 44,1 кГц стерео в stdout.

    ``-ss`` перед ``-i`` — быстрый поиск по входу; ссылка уже развёрнута
    (FFmpeg 4.3 не понимает 308 Яндекса).
    """
    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rw_timeout",
        "30000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "2",
        "-analyzeduration",
        "1000000",
        "-probesize",
        "1000000",
    ]
    if start_seconds > 0:
        args += ["-ss", f"{start_seconds:.3f}"]
    args += [
        "-i",
        url,
        "-vn",
        "-map_metadata",
        "-1",
        "-ac",
        "2",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "s16le",
        "pipe:1",
    ]
    return args


class PcmSource(Protocol):
    """Что писатель знает об источнике трека."""

    track_id: str
    start_offset: int  # смещение в байтах трека, с которого начат декодер

    def take(self, offset: int, size: int) -> tuple[bytes, bool]:
        """Байты трека с ``offset`` (не больше ``size``), уже имеющиеся.

        Returns:
            (данные, конец) — конец=True, если после выданного данных
            больше не будет (EOF или ошибка декодера).
        """
        ...

    def close(self) -> None:
        """Останавливает декодер (без ожидания)."""
        ...


class FfmpegPcmSource:
    """Декодер трека на FFmpeg с фоновым чтением в буфер."""

    def __init__(
        self,
        track_id: str,
        url: str,
        start_seconds: float = 0.0,
        buffer_bytes: int = DEFAULT_BUFFER_BYTES,
        args: Sequence[str] | None = None,
    ) -> None:
        self.track_id = track_id
        self.url = url
        frames = max(0, round(start_seconds * SAMPLE_RATE))
        self.start_offset = frames * BYTES_PER_FRAME
        self._start_seconds = frames / SAMPLE_RATE
        self._buffer_limit = buffer_bytes
        self._args = list(args) if args else decoder_args(url, start_seconds)
        self._buf = bytearray()
        self._buf_offset = self.start_offset  # смещение трека для _buf[0]
        self._want = self.start_offset  # куда хочет читать писатель
        self._eof = False
        self._closed = False
        self._space = asyncio.Event()
        self._space.set()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self.created_at = time.monotonic()
        self.first_byte_at: float | None = None
        self.returncode: int | None = None
        self._stderr_tail = bytearray()

    def start(self) -> None:
        """Запускает процесс и фоновое чтение."""
        self._task = asyncio.create_task(self._run())

    @property
    def eof(self) -> bool:
        """Данных больше не будет (EOF, ошибка или закрыт)."""
        return self._eof

    async def _run(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout = self._proc.stdout
            assert stdout is not None
            # stderr читается всё время: переполненный pipe блокирует FFmpeg
            drain = asyncio.create_task(self._drain_stderr())
            while not self._closed:
                await self._wait_for_space()
                chunk = await stdout.read(READ_CHUNK)
                if not chunk:
                    break
                if self.first_byte_at is None:
                    self.first_byte_at = time.monotonic()
                    logger.info(
                        f"⏱ Декодер {self.track_id} (с {self._start_seconds:.1f}с): "
                        f"первые PCM через "
                        f"{self.first_byte_at - self.created_at:.2f}с"
                    )
                self._append(chunk)
            self.returncode = await self._proc.wait()
            await asyncio.wait({drain}, timeout=1.0)
            if self.returncode not in (0, None) and not self._closed:
                err = bytes(self._stderr_tail)
                logger.warning(
                    f"⚠️ Декодер {self.track_id}: код {self.returncode} "
                    f"{err.decode(errors='replace').strip()[-300:]}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"⚠️ Декодер {self.track_id}: {e}")
        finally:
            self._eof = True
            self._kill()

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            line = await proc.stderr.read(4096)
            if not line:
                return
            self._stderr_tail += line
            del self._stderr_tail[:-2000]

    async def _wait_for_space(self) -> None:
        while not self._closed:
            ahead = self._buf_offset + len(self._buf) - self._want
            if ahead < self._buffer_limit:
                return
            self._space.clear()
            await self._space.wait()

    def _append(self, chunk: bytes) -> None:
        self._buf += chunk
        self._drop_before(self._want)

    def _drop_before(self, offset: int) -> None:
        drop = min(len(self._buf), offset - self._buf_offset)
        if drop > 0:
            del self._buf[:drop]
            self._buf_offset += drop

    def take(self, offset: int, size: int) -> tuple[bytes, bool]:
        """См. ``PcmSource.take``."""
        if offset > self._want:
            self._want = offset
        self._drop_before(offset)
        start = offset - self._buf_offset
        data = b""
        if start >= 0:
            avail = min(size, len(self._buf) - start)
            avail -= avail % BYTES_PER_FRAME  # только целые кадры
            stop = start + max(0, avail)
            data = bytes(self._buf[start:stop])
            if data:
                self._want = offset + len(data)
                self._drop_before(self._want)
        self._space.set()
        # Хвост короче кадра на EOF — тоже конец
        left = self._buf_offset + len(self._buf) - (offset + len(data))
        ended = self._eof and left < BYTES_PER_FRAME
        return data, ended

    def close(self) -> None:
        """См. ``PcmSource.close``."""
        self._closed = True
        self._space.set()
        self._kill()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
