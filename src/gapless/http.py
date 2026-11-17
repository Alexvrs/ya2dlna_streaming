"""HTTP бесшовного потока в DLNA-сервере: поток, обложки, команды."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from logging import getLogger
from typing import AsyncIterator

import aiohttp
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from core.config.settings import settings
from core.covers import cover_url

from .models import (
    EndBody,
    EndReply,
    GaplessState,
    OkReply,
    PrepareBody,
    SegmentBody,
    SegmentReply,
    StartBody,
    StartReply,
)
from .timeline import BYTES_PER_FRAME, SAMPLE_RATE
from .writer import GaplessSession, IcyInterleaver, icy_block, wav_header

logger = getLogger(__name__)

router = APIRouter()

PROBE_SILENCE_SECONDS = 1.0
COVER_CACHE_SIZE = 16


class GaplessHub:
    """Текущая сессия и кэш обложек одного DLNA-сервера."""

    def __init__(self) -> None:
        self.session: GaplessSession | None = None
        self.session_name = ""
        self._covers: OrderedDict[str, bytes] = OrderedDict()
        self._cover_urls: dict[str, str] = {}
        self._counter = 0

    def start(self, lead: float, margin: float) -> GaplessSession:
        """Новая сессия вместо старой."""
        self.stop()
        self._counter += 1
        self.session = GaplessSession(lead=lead, margin=margin)
        self.session_name = f"s{self._counter}"
        logger.info(
            f"▶️ Бесшовная сессия {self.session_name}: lead {lead}с, "
            f"margin {margin}с, t0={self.session.timeline.t0:.3f}"
        )
        return self.session

    def stop(self) -> None:
        """Закрывает текущую сессию."""
        if self.session is not None:
            self.session.close()
            logger.info(f"⏹ Бесшовная сессия {self.session_name} закрыта")
        self.session = None

    def remember_cover(self, track_id: str, url: str) -> None:
        """Запоминает ссылку на обложку трека для /cover/<id>.jpg."""
        if url:
            self._cover_urls[track_id] = cover_url(url)

    def cover_link(self, track_id: str) -> str:
        """Ссылка для ICY StreamUrl (пустая, если обложки нет)."""
        if track_id not in self._cover_urls:
            return ""
        # OwnTone берёт обложку из ICY только по ссылке с расширением .jpg
        return (
            f"http://{settings.local_server_host}:"
            f"{settings.local_server_port_dlna}/cover/{track_id}.jpg"
        )

    async def cover(self, track_id: str) -> bytes | None:
        """Картинка обложки (кэш на несколько треков)."""
        if track_id in self._covers:
            self._covers.move_to_end(track_id)
            return self._covers[track_id]
        url = self._cover_urls.get(track_id)
        if not url:
            return None
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(url) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"⚠️ Обложка {track_id}: HTTP {resp.status}"
                    )
                    return None
                data = await resp.read()
        self._covers[track_id] = data
        while len(self._covers) > COVER_CACHE_SIZE:
            self._covers.popitem(last=False)
        return data


def _hub(request: Request) -> GaplessHub:
    hub = getattr(request.app.state, "gapless", None)
    if hub is None:
        hub = GaplessHub()
        request.app.state.gapless = hub
    return hub


def _session(request: Request) -> GaplessSession:
    session = _hub(request).session
    if session is None or session.closed:
        raise HTTPException(status_code=409, detail="нет бесшовной сессии")
    return session


def _require_gapless() -> None:
    if not settings.gapless or settings.output_backend != "owntone":
        raise HTTPException(
            status_code=409, detail="бесшовный режим выключен (APP_GAPLESS)"
        )


@router.post("/gapless/start")
async def gapless_start(request: Request, body: StartBody) -> StartReply:
    """Открывает новую сессию вместо прежней."""
    _require_gapless()
    hub = _hub(request)
    session = hub.start(
        settings.gapless_lead if body.lead is None else body.lead,
        settings.gapless_margin if body.margin is None else body.margin,
    )
    # Менеджер на том же хосте: часы time.monotonic у сервера общие с ним
    return StartReply(
        session=hub.session_name,
        stream_path=f"/live/{hub.session_name}.wav",
        t0=session.timeline.t0,
        w_start=session.w_start,
    )


@router.post("/gapless/segment")
async def gapless_segment(request: Request, body: SegmentBody) -> SegmentReply:
    """Добавляет сегмент: в момент Станции t_station трек на позиции pos."""
    session = _session(request)
    hub = _hub(request)
    hub.remember_cover(body.track_id, body.cover)
    meta = {
        "title": body.title,
        "artist": body.artist,
        "album": body.album,
        "cover": hub.cover_link(body.track_id),
    }
    seg = session.add_segment(
        body.track_id, body.t_station, body.pos, body.url, meta
    )
    return SegmentReply(
        added=seg is not None, seg_id=None if seg is None else seg.seg_id
    )


@router.post("/gapless/end")
async def gapless_end(request: Request, body: EndBody) -> EndReply:
    """Закрывает звучащий сегмент: тишина с момента Станции t_station."""
    seg = _session(request).end_segment(body.t_station)
    return EndReply(ended=None if seg is None else seg.seg_id)


@router.post("/gapless/prepare")
async def gapless_prepare(request: Request, body: PrepareBody) -> OkReply:
    """Предзапускает декодер следующего трека."""
    session = _session(request)
    _hub(request).remember_cover(body.track_id, body.cover)
    session.prepare(body.track_id, body.url)
    return OkReply()


@router.post("/gapless/stop")
async def gapless_stop(request: Request) -> OkReply:
    """Закрывает сессию."""
    _hub(request).stop()
    return OkReply()


@router.get("/gapless/state")
async def gapless_state(request: Request) -> GaplessState:
    """Состояние сессии (или closed=True, если её нет)."""
    hub = _hub(request)
    if hub.session is None:
        return GaplessState(closed=True)
    return hub.session.state().model_copy(update={"session": hub.session_name})


@router.get("/cover/{track_id}.jpg")
async def cover(request: Request, track_id: str) -> Response:
    """Обложка трека для OwnTone (ICY StreamUrl)."""
    try:
        data = await _hub(request).cover(track_id)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"⚠️ Обложка {track_id}: {e!r}")
        data = None
    if data is None:
        raise HTTPException(status_code=404)
    return Response(content=data, media_type="image/jpeg")


# Имя сессии в пути, а не в query: query OwnTone при переподключении теряет
@router.get("/live/{name}.wav")
async def live(request: Request, name: str) -> Response:
    """Непрерывный WAV сессии ``name``, с ICY по ``Icy-MetaData: 1``."""
    hub = _hub(request)
    session = hub.session
    if session is None or session.closed or name != hub.session_name:
        raise HTTPException(status_code=404)
    ua = request.headers.get("user-agent", "")
    icy = request.headers.get("icy-metadata") == "1"
    headers = {"Cache-Control": "no-cache"}
    if ua.startswith("Lavf"):
        # Проба OwnTone: формат по заголовку, писатель не трогаем
        logger.info(f"🔎 Проба потока {name} ({ua})")
        silence = bytes(
            round(PROBE_SILENCE_SECONDS * SAMPLE_RATE) * BYTES_PER_FRAME
        )
        return Response(
            content=wav_header() + silence,
            media_type="audio/wav",
            headers=headers,
        )
    logger.info(f"🔌 Клиент потока {name}: {ua} (ICY: {icy})")
    if icy:
        headers["icy-metaint"] = str(IcyInterleaver().metaint)
        # Заголовки — только latin-1
        headers["icy-name"] = "Stream Yandex Station"
    return StreamingResponse(
        _body(session, icy), media_type="audio/wav", headers=headers
    )


async def _body(session: GaplessSession, icy: bool) -> AsyncIterator[bytes]:
    """Тело ответа: заголовок WAV, PCM в темпе, ICY по запросу."""
    interleaver = IcyInterleaver() if icy else None
    last: dict[str, str] = {}
    first = True
    async for pcm in session.pcm():
        data = (wav_header() + pcm) if first else pcm
        first = False
        if interleaver is None:
            yield data
            continue
        # В тишине (комментарий диджея) остаётся последнее описание,
        # пока не зазвучит следующий трек
        meta = session.meta_at(session.frame - 1) or last
        last = meta
        yield interleaver.feed(data, icy_block(meta, meta.get("cover", "")))
