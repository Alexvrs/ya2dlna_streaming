"""Этап 2 бесшовного потока: расписание, сборка PCM, темп, ICY."""

import asyncio
import struct
import sys

import pytest

from gapless.pcm_source import FfmpegPcmSource
from gapless.timeline import BYTES_PER_FRAME, SAMPLE_RATE, Timeline
from gapless.writer import (
    GaplessSession,
    IcyInterleaver,
    Renderer,
    SourcePool,
    icy_block,
    wav_header,
)

SR = SAMPLE_RATE


class FakeSource:
    """Трек длиной ``total`` кадров; кадр k = (код трека, k mod 30000).

    ``ready`` — сколько кадров трека (от начала) декодер уже выдал;
    None — всё сразу.
    """

    def __init__(self, track_id, start_frames=0, total=10 * SR, ready=None):
        self.track_id = track_id
        self.code = ord(track_id[0])
        self.start_offset = start_frames * BYTES_PER_FRAME
        self.total = total
        self.ready = ready
        self.closed = False
        self.taken = []

    def take(self, offset, size):
        self.taken.append(offset // BYTES_PER_FRAME)
        k0 = offset // BYTES_PER_FRAME
        limit = (
            self.total if self.ready is None else min(self.total, self.ready)
        )
        if k0 < self.start_offset // BYTES_PER_FRAME:
            return b"", False
        n = max(0, min(size // BYTES_PER_FRAME, limit - k0))
        data = b"".join(
            struct.pack("<hh", self.code, (k0 + i) % 30000) for i in range(n)
        )
        ended = self.ready is None and k0 + n >= self.total
        return data, ended

    def close(self):
        self.closed = True


class Factory:
    def __init__(self, total=10 * SR, ready=None):
        self.made = []
        self.total = total
        self.ready = ready

    def __call__(self, track_id, url, start_seconds):
        src = FakeSource(
            track_id,
            round(start_seconds * SR),
            total=self.total,
            ready=self.ready,
        )
        src.url = url
        self.made.append(src)
        return src


def frames(data):
    """Список (код трека, кадр) или (0, 0) для тишины."""
    return [
        struct.unpack_from("<hh", data, i)
        for i in range(0, len(data), BYTES_PER_FRAME)
    ]


def make(total=10 * SR, ready=None):
    tl = Timeline(t0=100.0)
    fac = Factory(total, ready)
    pool = SourcePool(fac)
    return tl, pool, Renderer(tl, pool), fac


def test_two_segments_back_to_back_no_silence():
    tl, pool, r, _ = make(total=SR)  # треки по 1 с
    tl.add("A", 100.0, 0.0, {"url": "a"})
    tl.add("B", 101.0, 0.0, {"url": "b"})
    out = frames(r.render(0, 2 * SR))
    assert out[SR - 1] == (ord("A"), (SR - 1) % 30000)
    assert out[SR] == (ord("B"), 0)
    assert (0, 0) not in out


def test_gap_between_segments_is_exact_silence():
    tl, pool, r, _ = make(total=SR)
    tl.add("A", 100.0, 0.0, {"url": "a"})
    tl.add("B", 101.5, 0.0, {"url": "b"})  # комментарий 0,5 с
    out = frames(r.render(0, 3 * SR))
    silence = [i for i, x in enumerate(out) if x == (0, 0)]
    gap = [i for i in silence if i < 2 * SR]
    assert gap[0] == SR and gap[-1] == SR + SR // 2 - 1 and len(gap) == SR // 2
    assert out[SR + SR // 2] == (ord("B"), 0)
    assert r.underrun_frames == 0


def test_late_segment_starts_from_shifted_position():
    tl, pool, r, _ = make()
    first = frames(r.render(0, SR))  # писатель уже прошёл 1 с тишины
    assert set(first) == {(0, 0)}
    # Событие опоздало: трек начался на Станции в t0+0,5 с
    tl.add("A", 100.5, 0.0, {"url": "a"})
    out = frames(r.render(SR, SR // 2))
    assert out[0] == (ord("A"), (SR // 2) % 30000)  # подмотка на опоздание


def test_repeated_state_does_not_duplicate_segment():
    tl, *_ = make()
    assert tl.add("A", 100.0, 0.0) is not None
    # Через 3,1 с Станция шлёт тот же трек с progress 3,0 (джиттер 0,1 с)
    assert tl.add("A", 103.1, 3.0) is None
    assert len(tl.segments) == 1


def test_seek_cuts_old_segment_and_closes_its_decoder():
    tl, pool, r, fac = make(total=100 * SR)
    tl.add("A", 100.0, 0.0, {"url": "a"})
    r.render(0, SR)
    tl.add("A", 101.0, 60.0, {"url": "a"})  # перемотка на 60 с
    out = frames(r.render(SR, SR // 10))
    assert out[0] == (ord("A"), (60 * SR) % 30000)
    assert fac.made[0].closed and not fac.made[1].closed
    assert fac.made[1].start_offset == 60 * SR * BYTES_PER_FRAME


def test_underrun_pads_silence_and_keeps_alignment():
    tl, pool, r, fac = make(ready=SR // 4)  # декодер дал только 0,25 с
    tl.add("A", 100.0, 0.0, {"url": "a"})
    out = frames(r.render(0, SR // 2))
    assert out[SR // 4 - 1] == (ord("A"), (SR // 4 - 1) % 30000)
    assert out[SR // 4] == (0, 0)
    assert r.underrun_frames == SR // 4
    fac.made[0].ready = None  # декодер догнал
    out = frames(r.render(SR // 2, 10))
    assert out[0] == (ord("A"), (SR // 2) % 30000)  # шкала не съехала


def test_end_segment_gives_silence_from_that_moment():
    tl, pool, r, _ = make()
    tl.add("A", 100.0, 0.0, {"url": "a"})
    tl.end_at(100.5)  # «следующий трек» с диджеем
    out = frames(r.render(0, SR))
    assert out[SR // 2 - 1][0] == ord("A")
    half = SR // 2
    assert set(out[half:]) == {(0, 0)}


def test_crossfade_new_segment_cuts_old_before_its_eof():
    tl, pool, r, _ = make(total=5 * SR)
    tl.add("A", 100.0, 0.0, {"url": "a"})
    tl.add("B", 102.0, 0.0, {"url": "b"})  # A ещё не доиграл
    out = frames(r.render(0, 3 * SR))
    assert out[2 * SR - 1][0] == ord("A") and out[2 * SR] == (ord("B"), 0)


def test_prepared_decoder_is_reused_with_skip():
    tl, pool, r, fac = make()
    pool.prepare("B", "b")
    tl.add("B", 100.0 - 0.3, 0.0, {"url": "b"})  # уже 0,3 с играет
    out = frames(r.render(0, 10))
    assert len(fac.made) == 1  # новый декодер не запускался
    assert out[0] == (ord("B"), round(0.3 * SR))


def test_segment_without_url_is_underrun_not_crash():
    tl, pool, r, _ = make()
    tl.add("A", 100.0, 0.0)
    out = frames(r.render(0, 100))
    assert set(out) == {(0, 0)} and r.underrun_frames == 100


def test_eof_marks_segment_and_following_is_silence():
    tl, pool, r, _ = make(total=SR // 2)
    tl.add("A", 100.0, 0.0, {"url": "a"})
    out = frames(r.render(0, SR))
    assert out[SR // 2 - 1][0] == ord("A")
    half = SR // 2
    assert set(out[half:]) == {(0, 0)}
    assert r.underrun_frames == 0
    assert tl.segments[0].eof_frame == SR // 2


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def test_session_paces_writes_and_new_client_replaces_old(monkeypatch):
    clock = Clock()

    async def fake_sleep(dt):
        clock.t += dt

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    s = GaplessSession(lead=1.0, margin=2.0, factory=Factory(), clock=clock)
    assert s.timeline.t0 == pytest.approx(1000.0 - 3.0)
    gen = s.pcm(chunk_frames=SR // 10)
    total = 0
    async for chunk in gen:
        total += len(chunk) // BYTES_PER_FRAME
        if clock.t >= 1005.0:
            break
    # Записано не больше «прошло + lead»
    assert total / SR <= (clock.t - 1000.0) + 1.0 + 0.1
    assert total / SR >= (clock.t - 1000.0) + 1.0 - 0.2
    await gen.aclose()
    # Второй клиент приходит через 10 с: без рывка из прошлого
    clock.t += 10.0
    gen2 = s.pcm(chunk_frames=SR // 10)
    await gen2.__anext__()
    assert s.client_start_frame >= round((clock.t - 1000.0) * SR) - SR // 10
    await gen2.aclose()


def test_wav_header_infinite_lengths():
    h = wav_header()
    assert h[:4] == b"RIFF" and h[8:16] == b"WAVEfmt "
    assert struct.unpack_from("<I", h, 4)[0] == 0xFFFFFFFF
    assert struct.unpack_from("<I", h, 40)[0] == 0xFFFFFFFF
    assert struct.unpack_from("<HHI", h, 20) == (1, 2, SR)


def test_icy_interleaver_inserts_block_only_on_change():
    icy = IcyInterleaver(metaint=10)
    b1 = icy_block({"title": "Песня", "artist": "Артист"}, "http://x/c.jpg")
    out = icy.feed(b"a" * 25, b1)
    assert out[:10] == b"a" * 10
    assert out[10] == len(b1) // 16 * 16 // 16
    end = 11 + len(b1) - 1
    assert out[11:end].startswith("StreamTitle='Артист - Песня';".encode())
    cut = 10 + len(b1)
    rest = out[cut:]
    assert rest[:10] == b"a" * 10 and rest[10] == 0  # повтор — нулевой блок
    assert b"StreamUrl='http://x/c.jpg';" in out


async def test_ffmpeg_source_buffers_and_skips_with_real_process():
    payload = bytes(range(256)) * 4096  # 1 МБ
    script = "import sys; sys.stdout.buffer.write(bytes(range(256)) * 4096)"
    src = FfmpegPcmSource(
        "X",
        "u",
        0.0,
        buffer_bytes=64 * 1024,
        args=[sys.executable, "-c", script],
    )
    src.start()
    for _ in range(200):
        data, _ = src.take(0, 1024)
        if data:
            break
        await asyncio.sleep(0.01)
    assert data == payload[:1024]
    # Подмотка далеко вперёд — без ограничения буфера
    off = 900 * 1024
    for _ in range(300):
        data, ended = src.take(off, 4096)
        if data:
            break
        await asyncio.sleep(0.01)
    stop = off + 4096
    assert data == payload[off:stop]
    for _ in range(300):
        data, ended = src.take(len(payload), 4096)
        if ended:
            break
        await asyncio.sleep(0.01)
    assert ended and data == b""
    src.close()


def test_session_starts_decoder_as_soon_as_segment_arrives():
    fac = Factory()
    s = GaplessSession(lead=1.0, margin=2.5, factory=fac, clock=Clock())
    s.client_connected = True
    s.add_segment("A", s.timeline.t0 + 1.0, 0.0, url="a")
    assert len(fac.made) == 1  # декодер запущен до записи
    # Будущий сегмент не закрывается писателем раньше времени
    s.renderer.render(0, SR // 2)
    assert not fac.made[0].closed
    s.renderer.render(SR // 2, SR)
    assert not fac.made[0].closed and fac.made[0].taken


def test_no_eager_decoders_without_client_and_limit():
    fac = Factory()
    s = GaplessSession(factory=fac, clock=Clock())
    s.add_segment("A", s.timeline.t0 + 1.0, 0.0, url="a")
    assert fac.made == []  # клиента нет — процессы не запускаем
    s.client_connected = True
    for i in range(6):
        s.add_segment(f"T{i}", s.timeline.t0 + 10.0 * (i + 1), 0.0, url="u")
    alive = [x for x in fac.made if not x.closed]
    assert len(alive) <= 3


def test_end_at_also_closes_later_started_segments():
    tl, pool, r, _ = make(total=100 * SR)
    tl.add("A", 100.0, 0.0, {"url": "a"})
    tl.add("A", 101.0, 60.0, {"url": "a"})  # перемотка
    tl.end_at(100.5)  # скип раньше начала перемотки
    out = frames(r.render(0, 2 * SR))
    half = SR // 2
    assert set(out[half:]) == {(0, 0)}
