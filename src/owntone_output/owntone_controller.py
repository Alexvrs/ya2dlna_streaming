import asyncio
import time
from logging import getLogger
from typing import Any

import aiohttp

from owntone_output.zone_store import StartVolumeStore, ZoneStore
from yandex_station.constants import RUARK_IDLE_VOLUME

logger = getLogger(__name__)

# Типы выходов OwnTone, которые не относятся к колонкам в сети
LOCAL_OUTPUT_TYPES = frozenset({"ALSA", "Pulseaudio", "fifo"})

# Пауза перед повторной проверкой громкости после старта: AirPlay-колонка
# при подключении может сообщить OwnTone свою громкость (вплоть до 100)
VOLUME_RECHECK_DELAY = 1.5
# Стартовую громкость закрепляем после начала игры дважды: колонка
# сообщает свою громкость в первые секунды AirPlay-сеанса
START_VOLUME_PIN_DELAYS = (1.5, 4.0)
# Сколько ждать начала игры, чтобы закрепить стартовую громкость
START_VOLUME_WAIT = 30.0


class OwnToneError(Exception):
    """Ошибка обращения к OwnTone."""


class OwnToneZoneError(OwnToneError):
    """Неизвестная зона или ни одной её колонки нет в сети."""


class OwnToneController:
    """Вывод звука через OwnTone: AirPlay 2, несколько колонок синхронно.

    Повторяет интерфейс RuarkR5Controller (см. core.audio_output), чтобы
    остальной код не зависел от устройства. Вместо одного устройства —
    зона: именованный набор выходов OwnTone. Питание моделируется выбором
    выходов: «включено» — выходы зоны выбраны в OwnTone.
    """

    def __init__(
        self,
        base_url: str,
        zones: dict[str, list[str]],
        default_zone: str | None = None,
        max_volume: int = 40,
        zone_store: ZoneStore | None = None,
        timeout: float = 10.0,
        start_volume_store: StartVolumeStore | None = None,
    ) -> None:
        """Сохраняет настройки; связь с OwnTone проверяется в connect()."""
        self.device_name = "OwnTone"
        self._base_url = base_url.rstrip("/")
        self._zones = {name: list(outputs) for name, outputs in zones.items()}
        self._default_zone = default_zone
        self._max_volume = max(0, min(100, max_volume))
        self._zone_store = zone_store or ZoneStore()
        self._start_volumes = start_volume_store or StartVolumeStore()
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._stream_url: str | None = None
        # Метаданные текущего трека для дисплеев (AirPlay, WiiM, Apple TV)
        self._metadata: dict[str, str] = {}
        # Имя выхода OwnTone → его id (обновляется при каждом запросе)
        self._outputs: dict[str, dict[str, Any]] = {}
        self._recheck_task: asyncio.Task[None] | None = None
        # Фоновый замер «через сколько OwnTone заиграл»
        self._timing_task: asyncio.Task[None] | None = None
        self._pin_task: asyncio.Task[None] | None = None

    # ---- HTTP ---------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json: Any = None,
    ) -> Any:
        """Выполняет запрос к JSON API OwnTone.

        Raises:
            OwnToneError: OwnTone недоступен или ответил ошибкой.
        """
        url = f"{self._base_url}/api{path}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.request(
                    method, url, params=params, json=json
                ) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        raise OwnToneError(
                            f"{method} {path}: HTTP {resp.status} {text[:200]}"
                        )
                    if resp.status == 204:
                        return None
                    body = await resp.read()
                    if not body:
                        return None
                    return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise OwnToneError(f"{method} {path}: {e!r}") from e

    async def _refresh_outputs(self) -> dict[str, dict[str, Any]]:
        """Перечитывает список выходов OwnTone."""
        data = await self._request("GET", "/outputs")
        outputs = data.get("outputs", []) if isinstance(data, dict) else []
        self._outputs = {o["name"]: o for o in outputs if "name" in o}
        return self._outputs

    # ---- Зоны ---------------------------------------------------------

    @property
    def zones(self) -> dict[str, list[str]]:
        """Настроенные зоны: имя → имена выходов OwnTone."""
        return {name: list(outputs) for name, outputs in self._zones.items()}

    def current_zone(self) -> str | None:
        """Выбранная зона: сохранённая, иначе зона по умолчанию."""
        stored = self._zone_store.load()
        if stored and (stored in self._zones or not self._zones):
            return stored
        if self._default_zone and self._default_zone in self._zones:
            return self._default_zone
        return next(iter(self._zones), None)

    def _zone_output_names(self, zone: str | None) -> list[str]:
        """Имена выходов зоны; без зон — все колонки в сети."""
        if zone and zone in self._zones:
            return self._zones[zone]
        return [
            name
            for name, o in self._outputs.items()
            if o.get("type") not in LOCAL_OUTPUT_TYPES
        ]

    def _zone_output_ids(self, zone: str | None) -> list[str]:
        """Id выходов зоны, которые сейчас есть в OwnTone."""
        names = self._zone_output_names(zone)
        missing = [n for n in names if n not in self._outputs]
        if missing:
            logger.warning(f"⚠️ Зона «{zone}»: нет в сети {missing}")
        return [
            str(self._outputs[n]["id"]) for n in names if n in self._outputs
        ]

    async def _apply_zone(self) -> list[str]:
        """Выбирает в OwnTone выходы текущей зоны.

        Raises:
            OwnToneZoneError: Ни одной колонки зоны нет в сети.
        """
        await self._refresh_outputs()
        zone = self.current_zone()
        ids = self._zone_output_ids(zone)
        if not ids:
            raise OwnToneZoneError(f"Колонки зоны «{zone}» не найдены")
        await self._request("PUT", "/outputs/set", json={"outputs": ids})
        return ids

    async def select_zone(self, zone: str) -> None:
        """Переключает зону; если музыка играет — сразу, на лету.

        Raises:
            OwnToneZoneError: Зона не настроена.
        """
        if zone not in self._zones:
            raise OwnToneZoneError(f"Неизвестная зона «{zone}»")
        self._zone_store.save(zone)
        logger.info(f"🔈 Выбрана зона «{zone}»")
        if await self.is_playing():
            ids = await self._apply_zone()
            await self._cap_output_volumes(ids)

    # ---- Громкость ----------------------------------------------------

    async def get_zone_volume(self, zone: str) -> int | None:
        """Громкость зоны: наибольшая из её колонок, None — колонок нет.

        Raises:
            OwnToneZoneError: Зона не настроена.
        """
        if zone not in self._zones:
            raise OwnToneZoneError(f"Неизвестная зона «{zone}»")
        outputs = await self._refresh_outputs()
        levels = [
            int(outputs[name].get("volume", 0))
            for name in self._zones[zone]
            if name in outputs
        ]
        return max(levels) if levels else None

    async def set_zone_volume(self, zone: str, volume: int) -> int:
        """Ставит громкость всем колонкам зоны (не выше потолка).

        Работает и когда зона не играет: уровень сохранится в OwnTone
        до следующего включения.

        Returns:
            int: Установленная громкость.
        Raises:
            OwnToneZoneError: Зона не настроена или её колонок нет в сети.
        """
        if zone not in self._zones:
            raise OwnToneZoneError(f"Неизвестная зона «{zone}»")
        target = self._clamp(volume)
        outputs = await self._refresh_outputs()
        ids = [
            str(outputs[n]["id"]) for n in self._zones[zone] if n in outputs
        ]
        if not ids:
            raise OwnToneZoneError(f"Колонки зоны «{zone}» не найдены")
        for output_id in ids:
            await self._request(
                "PUT", f"/outputs/{output_id}", json={"volume": target}
            )
        logger.info(f"🔊 Громкость зоны «{zone}»: {target}")
        self._remember_zone_volume(zone, target)
        return target

    def _remember_zone_volume(self, zone: str, volume: int) -> None:
        """Сохраняет громкость зоны для следующего включения.

        Тишину не запоминаем: стрим, включившийся в тишине, выглядит
        сломанным (и приглушение на речь Алисы — не выбор громкости).
        """
        if volume > RUARK_IDLE_VOLUME:
            self._start_volumes.put(zone, volume)

    def _clamp(self, volume: int) -> int:
        """Ограничивает громкость диапазоном 0…max_volume."""
        return max(0, min(self._max_volume, int(volume)))

    async def _cap_output_volumes(self, ids: list[str]) -> None:
        """Опускает до потолка громкость выбранных колонок."""
        outputs = await self._refresh_outputs()
        for o in outputs.values():
            if (
                str(o.get("id")) in ids
                and o.get("volume", 0) > self._max_volume
            ):
                logger.warning(
                    f"🔉 «{o['name']}»: громкость {o['volume']} выше "
                    f"потолка, ставим {self._max_volume}"
                )
                await self._request(
                    "PUT",
                    f"/outputs/{o['id']}",
                    json={"volume": self._max_volume},
                )

    async def _recheck_volumes_later(self, ids: list[str]) -> None:
        """Повторно опускает до потолка громкость после подключения колонок."""
        try:
            await asyncio.sleep(VOLUME_RECHECK_DELAY)
            await self._cap_output_volumes(ids)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"⚠️ Повторная проверка громкости не удалась: {e}")

    async def _set_outputs_volume(self, ids: list[str], volume: int) -> None:
        """Ставит громкость указанным выходам OwnTone."""
        for output_id in ids:
            await self._request(
                "PUT", f"/outputs/{output_id}", json={"volume": volume}
            )

    async def set_start_volume(self, volume: int) -> int:
        """Стартовая громкость сеанса: всем колонкам текущей зоны.

        Закрепляется ещё раз, когда OwnTone начнёт играть: колонка
        при подключении по AirPlay может навязать свою громкость.

        Raises:
            OwnToneZoneError: Колонок зоны нет в сети.
        """
        zone = self.current_zone()
        override = self._start_volumes.get(zone) if zone else None
        target = self._clamp(override if override is not None else volume)
        ids = await self._apply_zone()
        await self._set_outputs_volume(ids, target)
        # play() выполняет другой процесс (DLNA-сервер), поэтому начало игры
        # ждём здесь и закрепляем громкость после подключения колонок
        if self._pin_task and not self._pin_task.done():
            self._pin_task.cancel()
        self._pin_task = asyncio.create_task(
            self._pin_start_volume(ids, target)
        )
        source = "последняя" if override is not None else "по умолчанию"
        logger.info(f"🔊 Стартовая громкость: {target} ({source})")
        return target

    async def _pin_start_volume(self, ids: list[str], volume: int) -> None:
        """Ждёт начала игры и закрепляет стартовую громкость."""
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + START_VOLUME_WAIT
            while loop.time() < deadline:
                player = await self._request("GET", "/player")
                if player and player.get("state") == "play":
                    break
                await asyncio.sleep(0.5)
            else:
                return
            started = loop.time()
            for delay in START_VOLUME_PIN_DELAYS:
                await asyncio.sleep(max(0.0, started + delay - loop.time()))
                await self._set_outputs_volume(ids, volume)
            logger.info(f"🔊 Стартовая громкость {volume} закреплена")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"⚠️ Не удалось закрепить стартовую громкость: {e}")

    def remember_start_volume(self, zone: str, volume: int) -> int:
        """Запоминает громкость для следующего включения зоны.

        Raises:
            OwnToneZoneError: Зона не настроена.
        """
        if zone not in self._zones:
            raise OwnToneZoneError(f"Неизвестная зона «{zone}»")
        target = self._clamp(volume)
        self._start_volumes.put(zone, target)
        logger.info(f"🔊 Зона «{zone}» включится с громкостью {target}")
        return target

    def remember_session_volume(self, volume: int) -> None:
        """Запоминает громкость текущей зоны при остановке стрима."""
        zone = self.current_zone()
        if not zone:
            return
        target = self._clamp(volume)
        if target <= RUARK_IDLE_VOLUME:
            return
        self._start_volumes.put(zone, target)
        logger.info(f"💾 Громкость зоны «{zone}» запомнена: {target}")

    def next_start_volume(self, zone: str, default: int) -> int:
        """Громкость, с которой зона включится в следующий раз."""
        override = self._start_volumes.get(zone)
        return self._clamp(override if override is not None else default)

    async def get_volume(self) -> int:
        """Общая громкость OwnTone, 0–100."""
        player = await self._request("GET", "/player")
        return int(player.get("volume", 0)) if player else 0

    async def set_volume(self, volume: int) -> None:
        """Общая громкость OwnTone (пропорционально для всех колонок)."""
        target = self._clamp(volume)
        await self._request(
            "PUT", "/player/volume", params={"volume": str(target)}
        )

    # ---- Воспроизведение ----------------------------------------------

    async def connect(self, attempts: int = 3, delay: float = 2.0) -> bool:
        """Проверяет, что OwnTone отвечает и колонки зоны в сети."""
        for attempt in range(1, attempts + 1):
            try:
                await self._refresh_outputs()
                if self._zone_output_ids(self.current_zone()):
                    logger.info(
                        f"✅ OwnTone на связи, зона «{self.current_zone()}»"
                    )
                    return True
                logger.warning("⚠️ OwnTone на связи, но колонок зоны нет")
            except OwnToneError as e:
                logger.warning(
                    f"⚠️ OwnTone недоступен (попытка {attempt}): {e}"
                )
            if attempt < attempts:
                await asyncio.sleep(delay)
        return False

    async def reconnect(self, attempts: int = 1, delay: float = 0.0) -> bool:
        """Повторно проверяет связь с OwnTone."""
        return await self.connect(attempts=attempts, delay=delay)

    async def set_av_transport_uri(
        self,
        url: str,
        title: str = "",
        artist: str = "",
        mime_type: str = "audio/mpeg",
    ) -> None:
        """Запоминает адрес локального стрима для следующего play()."""
        self._stream_url = url
        self._metadata.update(title=title, artist=artist)
        logger.debug(f"OwnTone: поток {url} ({artist} — {title})")

    async def set_metadata(
        self,
        title: str = "",
        artist: str = "",
        album: str = "",
        artwork_url: str = "",
    ) -> None:
        """Запоминает метаданные трека для следующего play()."""
        self._metadata = {
            "title": title,
            "artist": artist,
            "album": album,
            "artwork_url": artwork_url,
        }

    async def _apply_metadata(self, added: Any) -> None:
        """Записывает метаданные в элемент очереди OwnTone.

        OwnTone передаёт их по AirPlay: название, исполнитель, альбом
        и обложка видны в WiiM и на экране Apple TV.
        """
        params = {k: v for k, v in self._metadata.items() if v}
        if not params:
            return
        items = added.get("items") if isinstance(added, dict) else None
        item_id = (
            items[0].get("id")
            if items and isinstance(items[0], dict)
            else None
        )
        target = str(item_id) if item_id is not None else "now_playing"
        try:
            await self._request("PUT", f"/queue/items/{target}", params=params)
        except OwnToneError as e:
            logger.warning(f"⚠️ Метаданные трека не записаны: {e}")

    async def play(self) -> None:
        """Выбирает колонки зоны и ставит в OwnTone локальный стрим.

        Raises:
            OwnToneError: Адрес потока не задан или OwnTone недоступен.
        """
        if not self._stream_url:
            raise OwnToneError("Адрес потока не задан")
        started = time.monotonic()
        ids = await self._apply_zone()
        zone_done = time.monotonic()
        await self._cap_output_volumes(ids)
        cap_done = time.monotonic()
        await self._request("PUT", "/queue/clear")
        added = await self._request(
            "POST",
            "/queue/items/add",
            params={"uris": self._stream_url, "playback": "start"},
        )
        add_done = time.monotonic()
        await self._apply_metadata(added)
        logger.info(
            f"⏱ OwnTone play: зона {zone_done - started:.2f}с, "
            f"потолок громкости {cap_done - zone_done:.2f}с, очередь "
            f"{add_done - cap_done:.2f}с, метаданные "
            f"{time.monotonic() - add_done:.2f}с"
        )
        if self._timing_task and not self._timing_task.done():
            self._timing_task.cancel()
        self._timing_task = asyncio.create_task(
            self._log_time_to_play(add_done)
        )
        if self._recheck_task and not self._recheck_task.done():
            self._recheck_task.cancel()
        self._recheck_task = asyncio.create_task(
            self._recheck_volumes_later(ids)
        )

    async def _log_time_to_play(
        self, since: float, timeout: float = 10.0, interval: float = 0.2
    ) -> None:
        """Замер: через сколько после постановки в очередь OwnTone играет."""
        deadline = since + timeout
        try:
            while time.monotonic() < deadline:
                player = await self._request("GET", "/player")
                if isinstance(player, dict) and player.get("state") == "play":
                    logger.info(
                        f"⏱ OwnTone играет через "
                        f"{time.monotonic() - since:.2f}с после очереди "
                        f"(позиция {player.get('item_progress_ms')} мс)"
                    )
                    return
                await asyncio.sleep(interval)
            logger.info(f"⏱ OwnTone не заиграл за {timeout:.0f}с")
        except OwnToneError as e:
            logger.debug(f"Замер старта OwnTone прерван: {e}")

    async def stop(self) -> None:
        """Останавливает воспроизведение OwnTone."""
        await self._request("PUT", "/player/stop")

    async def is_playing(self, timeout: float = 5.0) -> bool:
        """Проверяет, играет ли OwnTone хотя бы на одну колонку."""
        try:
            player = await asyncio.wait_for(
                self._request("GET", "/player"), timeout
            )
            if not player or player.get("state") != "play":
                return False
            outputs = await self._refresh_outputs()
            return any(o.get("selected") for o in outputs.values())
        except (OwnToneError, asyncio.TimeoutError) as e:
            logger.debug(f"OwnTone: состояние не получено: {e}")
            return False

    # ---- «Питание» ----------------------------------------------------

    async def get_session_id(self) -> str:
        """Возвращает заглушку: OwnTone сессии не требует."""
        return "owntone"

    async def get_power_status(self) -> str:
        """Возвращает "1", если выбрана хоть одна колонка зоны, иначе "0"."""
        outputs = await self._refresh_outputs()
        names = set(self._zone_output_names(self.current_zone()))
        selected = any(
            o.get("selected") for n, o in outputs.items() if n in names
        )
        return "1" if selected else "0"

    async def turn_power_on(self) -> bool:
        """Выбирает колонки текущей зоны."""
        ids = await self._apply_zone()
        await self._cap_output_volumes(ids)
        return True

    async def turn_power_off(self) -> bool:
        """Останавливает звук и отпускает все колонки."""
        await self.stop()
        await self._request("PUT", "/outputs/set", json={"outputs": []})
        return True
