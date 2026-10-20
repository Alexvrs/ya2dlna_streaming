import asyncio
from logging import getLogger
from typing import Any, Callable, Protocol

logger = getLogger(__name__)

POLL_INTERVAL = 1.0


class SpeakerError(Exception):
    """Колонка не ответила на запрос громкости."""


class SpeakerVolume(Protocol):
    """Громкость и mute одной колонки."""

    host: str

    async def get(self) -> tuple[int, bool]:
        """Возвращает (громкость 0–100, mute)."""
        ...

    async def set(self, volume: int, mute: bool) -> None:
        """Устанавливает громкость и mute."""
        ...


class LinkplayVolume:
    """Громкость колонки WiiM/Linkplay через её HTTPS API.

    Ведомая колонка группы WiiM не принимает громкость ни по AirPlay,
    ни по UPnP (обрывает соединение), а HTTPS API слушается. API требует
    клиентский сертификат — его даёт библиотека python-linkplay.
    """

    def __init__(self, host: str) -> None:
        """Принимает IP-адрес колонки."""
        self.host = host
        self._bridge: Any = None
        self._session: Any = None

    async def _get_bridge(self) -> Any:
        """Подключается к колонке (один раз, затем переиспользует)."""
        if self._bridge is not None:
            return self._bridge
        from linkplay.discovery import linkplay_factory_httpapi_bridge
        from linkplay.utils import async_create_unverified_client_session

        if self._session is None:
            self._session = await async_create_unverified_client_session()
        self._bridge = await linkplay_factory_httpapi_bridge(
            self.host, self._session
        )
        return self._bridge

    async def _run(self, action: Callable[[Any], Any]) -> Any:
        """Выполняет действие, при сбое сбрасывает подключение."""
        try:
            return await action(await self._get_bridge())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._bridge = None
            raise SpeakerError(f"{self.host}: {e!r}") from e

    async def get(self) -> tuple[int, bool]:
        """Возвращает (громкость 0–100, mute)."""

        async def read(bridge: Any) -> tuple[int, bool]:
            await bridge.player.update_status()
            return int(bridge.player.volume), bool(bridge.player.muted)

        result: tuple[int, bool] = await self._run(read)
        return result

    async def set(self, volume: int, mute: bool) -> None:
        """Устанавливает громкость и mute."""
        level = max(0, min(100, int(volume)))

        async def write(bridge: Any) -> None:
            await bridge.player.set_volume(level)
            await (bridge.player.mute() if mute else bridge.player.unmute())

        await self._run(write)

    async def close(self) -> None:
        """Закрывает HTTP-сессию."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            self._bridge = None


class LinkedVolumeMirror:
    """Повторяет громкость ведущей колонки на связанных с ней.

    Стереопара WiiM (Linkplay) принимает громкость по AirPlay и с телефона
    только ведущей колонкой; вторая остаётся на своей. Зеркало раз
    в секунду читает громкость и mute ведущей и при изменении ставит то же
    связанным — откуда бы громкость ни меняли (OwnTone, Алиса, телефон).
    """

    def __init__(
        self,
        links: dict[str, list[str]],
        interval: float = POLL_INTERVAL,
        speaker_factory: Callable[[str], SpeakerVolume] = LinkplayVolume,
    ) -> None:
        """links: адрес ведущей колонки → адреса связанных."""
        self._pairs = [
            (speaker_factory(master), [speaker_factory(h) for h in partners])
            for master, partners in links.items()
            if partners
        ]
        self._interval = interval
        self._last: dict[str, tuple[int, bool]] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Запускает фоновое зеркалирование (если пары настроены)."""
        if self._pairs and not self._task:
            logger.info(
                "🔗 Зеркало громкости: "
                + ", ".join(
                    f"{m.host} → {[p.host for p in ps]}"
                    for m, ps in self._pairs
                )
            )
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Останавливает зеркалирование и закрывает подключения."""
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        for master, partners in self._pairs:
            for speaker in [master, *partners]:
                close = getattr(speaker, "close", None)
                if close:
                    await close()

    async def _run(self) -> None:
        while True:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Цикл не должен умирать молча: логируем и продолжаем
                logger.exception(f"❌ Зеркало громкости: {e}")
            await asyncio.sleep(self._interval)

    async def sync_once(self) -> None:
        """Один проход: при изменении ведущей — повторить на связанных."""
        for master, partners in self._pairs:
            try:
                state = await master.get()
            except SpeakerError as e:
                logger.debug(f"Зеркало: ведущая недоступна: {e}")
                continue
            if self._last.get(master.host) == state:
                continue
            ok = True
            for partner in partners:
                try:
                    await partner.set(*state)
                except SpeakerError as e:
                    ok = False
                    logger.warning(f"⚠️ Зеркало громкости: {e}")
            if ok:
                self._last[master.host] = state
                logger.info(
                    f"🔗 Громкость {master.host} = {state[0]}"
                    f"{' (mute)' if state[1] else ''} → связанные"
                )
