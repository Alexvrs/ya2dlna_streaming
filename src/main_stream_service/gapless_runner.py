"""Исполнитель бесшовного режима: решатель → сервер потока и Станция."""

from __future__ import annotations

import asyncio
import time
from logging import getLogger
from typing import Awaitable, Callable, Protocol, runtime_checkable

import aiohttp
from pydantic import BaseModel, JsonValue, ValidationError
from yandex_music.exceptions import YandexMusicError

from core.config.settings import settings
from gapless.models import (
    EndBody,
    GaplessState,
    PrepareBody,
    SegmentBody,
    StartReply,
)
from main_stream_service.gapless_decider import (
    Action,
    AddSegment,
    EndSegment,
    Fallback,
    GaplessDecider,
    Prepare,
    Snapshot,
    StartSession,
    StopSession,
    UnmuteStation,
)
from main_stream_service.yandex_music_api import TrackInfo, TrackSource
from owntone_output.owntone_controller import OwnToneError
from yandex_station.models import Track

logger = getLogger(__name__)

SAMPLE_RATE = 44100
# Буфер OwnTone до старта (опыт E3c) — для оценки D до первого замера
OWNTONE_START_BUFFER = 2.8
DELAY_MEASURE_INTERVAL = 10.0
# Станцию гасить чуть позже момента, когда колонки зазвучали
FADE_AFTER_AUDIBLE = 0.3
# Нет ссылки трека — повтор запроса не чаще, секунд
LINK_RETRY = 5.0
# Название потока в очереди OwnTone. Без него OwnTone показывает имя
# файла (s1.wav); исполнитель и трек приходят по ICY в поля «исполнитель»
# и «альбом»
GAPLESS_STREAM_TITLE = "Stream Yandex Station"


class GaplessLost(Exception):
    """Сервер потока не знает сессии (перезапущен) — начать заново."""


# Сбои сервера потока: сеть, таймаут, ответ не по контракту
SERVER_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, ValidationError)
# Сбои одного действия: сервер потока или OwnTone
ACTION_ERRORS = (*SERVER_ERRORS, OwnToneError)


class GaplessStation(Protocol):
    """Громкость Станции, которой управляет бесшовный режим."""

    async def unmute(self) -> bool:
        """Возвращает Станции рабочую громкость."""
        ...

    async def fade_out_alice_volume(
        self,
        min_volume: float = ...,
        step: float = ...,
        delay: float = ...,
        wait: float | None = ...,
    ) -> float | None:
        """Плавно гасит Станцию."""
        ...


@runtime_checkable
class GaplessOutput(Protocol):
    """Вывод, умеющий играть бесшовный поток и сообщать позицию."""

    async def set_av_transport_uri(
        self,
        url: str,
        title: str = ...,
        artist: str = ...,
        mime_type: str = ...,
    ) -> None:
        """Задаёт адрес потока."""
        ...

    async def play(self) -> None:
        """Запускает поток."""
        ...

    async def stop(self) -> None:
        """Останавливает поток."""
        ...

    async def player_progress(self) -> tuple[str, int] | None:
        """Состояние плеера и позиция элемента в мс или None."""
        ...


class GaplessRunner:
    """Связывает решатель с сервером потока, OwnTone и Станцией."""

    def __init__(
        self,
        station: GaplessStation,
        output: GaplessOutput,
        resolve_source: Callable[[str], Awaitable[TrackSource | None]],
        track_info: Callable[[str], Awaitable[TrackInfo]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.decider = GaplessDecider()
        self._station = station
        self._output = output
        self._resolve_source = resolve_source
        self._track_info = track_info
        self._clock = clock
        self._base = (
            f"http://{settings.local_server_host}:"
            f"{settings.local_server_port_dlna}"
        )
        self.delay = (
            OWNTONE_START_BUFFER
            + settings.gapless_lead
            + settings.gapless_margin
        )
        self.delay_measured = False
        self._last_measure = 0.0
        self._session: StartReply | None = None
        self._fade_task: asyncio.Task[None] | None = None
        self._fading = False
        self._silent_checks = 0
        self._link_failed: dict[str, float] = {}
        self._bg: set[asyncio.Task[None]] = set()

    @property
    def fade_pending(self) -> bool:
        """Гашение Станции запланировано или идёт."""
        return self._fade_task is not None and not self._fade_task.done()

    @property
    def active(self) -> bool:
        """Идёт ли бесшовная сессия."""
        return self.decider.session

    # --- цикл ---------------------------------------------------------------

    async def handle(self, track: Track, alice: str | None) -> bool:
        """Обрабатывает снимок Станции.

        Returns:
            bool: True — случай не бесшовный (радио), нужен классический путь.
        """
        snap = Snapshot(
            t=track.captured_at,
            track_id=track.id,
            playing=track.playing,
            progress=track.progress,
            duration=track.duration,
            alice=alice,
            type=track.type,
            next_id=track.next_id,
        )
        self.decider.delay = self.delay
        for act in self.decider.feed(snap):
            if isinstance(act, Fallback):
                return True
            # Сбой действия не роняет цикл: состояние решателя
            # откатывается, следующий снимок повторит попытку
            try:
                await self._do(act, track)
            except GaplessLost as e:
                logger.warning(f"⚠️ Бесшовная сессия потеряна ({e}) — заново")
                await self._lose_session()
                break
            except ACTION_ERRORS as e:
                logger.warning(
                    f"⚠️ Бесшовный режим: {type(act).__name__}: {e!r}"
                )
                await self._rollback(act)
                break
            except Exception as e:
                # Последний слой: неожиданная ошибка не роняет стрим
                logger.exception(
                    f"❌ Бесшовный режим: {type(act).__name__}: {e!r}"
                )
                await self._rollback(act)
                break
        await self._check_output()
        return False

    async def stop(self) -> None:
        """Останавливает сессию (стоп стрима)."""
        await self._cancel_fade()
        for task in list(self._bg):
            task.cancel()
        if self.decider.session or self._session:
            await self._stop_server_session()
        self.decider.reset()
        self._session = None

    async def _lose_session(self) -> None:
        """Сессия пропала или колонки молчат: Станция громкая, заново."""
        await self._cancel_fade()
        self.decider.reset()
        self._session = None
        await self._station.unmute()

    async def _rollback(self, act: Action) -> None:
        if isinstance(act, AddSegment):
            self.decider.seg_open = False  # следующий снимок повторит
        elif isinstance(act, (StartSession, StopSession)):
            if isinstance(act, StartSession):
                await self._stop_server_session()
            await self._lose_session()

    # --- действия -----------------------------------------------------------

    async def _do(self, act: Action, track: Track) -> None:
        match act:
            case StartSession():
                await self._start_session()
            case AddSegment():
                await self._add_segment(act, track)
            case EndSegment():
                t = act.t - (self.delay if act.sync else 0.0)
                await self._post("/gapless/end", EndBody(t_station=t))
            case Prepare():
                self._spawn(self._prepare(act.track_id))
            case UnmuteStation():
                logger.info(f"🔊 Станция громкая: {act.reason}")
                await self._cancel_fade()
                await self._station.unmute()
            case StopSession():
                logger.info(f"⏹ Бесшовная сессия: стоп ({act.reason})")
                await self._cancel_fade()
                self._session = None
                try:
                    await self._post("/gapless/stop")
                    await self._output.stop()
                except GaplessLost:
                    # Сервер сессию уже забыл: колонкам нечего доигрывать
                    logger.debug("Сессии на сервере потока уже нет")
                finally:
                    await self._station.unmute()

    async def _start_session(self) -> None:
        # Станция могла остаться приглушённой (радио, потерянная сессия):
        # первые D секунд синхронного трека играет она
        await self._station.unmute()
        self._session = StartReply.model_validate(
            await self._post("/gapless/start")
        )
        url = self._base + self._session.stream_path
        self.delay_measured = False
        self._silent_checks = 0
        await self._output.set_av_transport_uri(
            url, title=GAPLESS_STREAM_TITLE
        )
        await self._output.play()
        logger.info(f"▶️ Бесшовный поток в OwnTone: {url}")

    async def _add_segment(self, act: AddSegment, track: Track) -> None:
        failed_at = self._link_failed.get(act.track_id)
        if failed_at is not None and self._clock() - failed_at < LINK_RETRY:
            self.decider.seg_open = False
            return
        source, info = await asyncio.gather(
            self._resolve_source(act.track_id), self._track_info(act.track_id)
        )
        if source is None:
            logger.warning(f"⚠️ Нет ссылки трека {act.track_id} — повторим")
            self._link_failed[act.track_id] = self._clock()
            self.decider.seg_open = False
            return
        self._link_failed.pop(act.track_id, None)
        pos = act.pos + (self.delay if act.sync else 0.0)
        same = track.id == act.track_id
        body = SegmentBody(
            track_id=act.track_id,
            t_station=act.t,
            pos=pos,
            url=source.url,
            title=(track.title if same else "") or info.title,
            artist=(track.artist if same else "") or info.artist,
            album=info.album,
            cover=(track.cover if same else None) or info.cover or "",
        )
        await self._post("/gapless/segment", body)
        mode = "вровень" if act.sync else "зеркало"
        logger.info(
            f"🧩 Сегмент {act.track_id} ({mode}): позиция {pos:.1f}с, "
            f"D={self.delay:.2f}с"
        )
        if act.fade_station:
            await self._schedule_fade(act.t + self.delay + FADE_AFTER_AUDIBLE)

    async def _prepare(self, track_id: str) -> None:
        try:
            source = await self._resolve_source(track_id)
            info = await self._track_info(track_id)
            if source is None or not self.decider.session:
                return
            await self._post(
                "/gapless/prepare",
                PrepareBody(
                    track_id=track_id, url=source.url, cover=info.cover or ""
                ),
            )
            logger.info(f"📦 Декодер следующего трека {track_id} готовится")
        except (*SERVER_ERRORS, GaplessLost, YandexMusicError) as e:
            logger.debug(f"Предзапуск {track_id} не удался: {e!r}")
        except Exception as e:
            # Последний слой фоновой задачи: предзапуск — только ускорение
            logger.exception(f"❌ Предзапуск {track_id}: {e!r}")

    # --- громкость Станции -----------------------------------------------

    async def _schedule_fade(self, at: float) -> None:
        await self._cancel_fade()
        self._fade_task = asyncio.create_task(self._fade_at(at))

    async def _fade_at(self, at: float) -> None:
        wait = at - self._clock()
        if wait > 0:
            await asyncio.sleep(wait)
        if not await self._output_playing():
            # Колонки молчат — Станцию не гасим, иначе тишина везде
            logger.warning("⚠️ Колонки не играют — Станция остаётся громкой")
            return
        logger.info("🔉 Колонки зазвучали — Станция гаснет")
        self._fading = True
        try:
            await self._station.fade_out_alice_volume(wait=0, delay=0.05)
            self.decider.station_faded()
        finally:
            self._fading = False

    async def _cancel_fade(self) -> None:
        """Отменяет ожидание гашения; начатое гашение доводит до конца."""
        task, self._fade_task = self._fade_task, None
        if task is None or task.done():
            return
        if self._fading:
            # Оборванное гашение оставило бы Станцию тихой с неверно
            # запомненной громкостью: unmute её бы не вернул
            await asyncio.shield(task)
            return
        task.cancel()

    # --- страховка и задержка D ------------------------------------------------

    async def _output_playing(self) -> bool:
        player = await self._output.player_progress()
        return player is not None and player[0] == "play"

    async def _check_output(self) -> None:
        """Раз в 10 с: колонки играют? замер D; страховка от тишины."""
        now = self._clock()
        if not self.decider.session or self._session is None:
            return
        if now - self._last_measure < DELAY_MEASURE_INTERVAL:
            return
        self._last_measure = now
        player = await self._output.player_progress()
        heard_at = self._clock()
        try:
            state = GaplessState.model_validate(
                await self._get("/gapless/state")
            )
        except SERVER_ERRORS as e:
            logger.debug(f"Состояние сервера потока не получено: {e!r}")
            return
        if player is None or player[0] != "play":
            self._silent_checks += 1
            if self._silent_checks >= 2 and now - self._started_at() > 15:
                logger.warning(
                    "⚠️ Колонки не играют бесшовный поток — "
                    "Станция громкая, сессия заново"
                )
                await self._stop_server_session()
                await self._lose_session()
            return
        self._silent_checks = 0
        if state.client_start_frame is None or state.t0 is None:
            return
        # D меряется по item_progress_ms OwnTone: позиция совпадает
        # со слышимым звуком (опыт E4)
        heard = (
            state.t0 + state.client_start_frame / SAMPLE_RATE + player[1] / 1000
        )
        delay = heard_at - heard
        if 2.0 < delay < 15.0:
            if abs(delay - self.delay) > 0.2 or not self.delay_measured:
                logger.info(f"⏱ Задержка колонок D = {delay:.2f}с")
            self.delay = delay
            self.delay_measured = True

    def _started_at(self) -> float:
        if self._session is None:
            return self._clock()
        return self._session.w_start

    # --- HTTP -------------------------------------------------------------

    async def _stop_server_session(self) -> None:
        """Закрывает сессию на сервере потока; его сбой не мешает стопу."""
        try:
            await self._post("/gapless/stop")
        except (*SERVER_ERRORS, GaplessLost) as e:
            logger.debug(f"Сервер потока не закрыл сессию: {e!r}")

    async def _post(
        self, path: str, body: BaseModel | None = None
    ) -> JsonValue:
        timeout = aiohttp.ClientTimeout(total=10)
        payload = {} if body is None else body.model_dump()
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(self._base + path, json=payload) as resp:
                if resp.status == 409:
                    raise GaplessLost(await resp.text())
                resp.raise_for_status()
                data: JsonValue = await resp.json()
                return data

    async def _get(self, path: str) -> JsonValue:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(self._base + path) as resp:
                resp.raise_for_status()
                data: JsonValue = await resp.json()
                return data

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
