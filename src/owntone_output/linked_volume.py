import asyncio
import re
from logging import getLogger

import aiohttp

logger = getLogger(__name__)

RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
DEFAULT_UPNP_PORT = 49152
POLL_INTERVAL = 1.0

SOAP_BODY = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    '<s:Body><u:{action} xmlns:u="' + RENDERING_CONTROL + '">'
    "<InstanceID>0</InstanceID><Channel>Master</Channel>{args}"
    "</u:{action}></s:Body></s:Envelope>"
)


class UpnpError(Exception):
    """Ошибка обращения к UPnP RenderingControl колонки."""


class UpnpVolume:
    """Громкость и mute колонки через UPnP RenderingControl (DLNA)."""

    def __init__(self, host: str, timeout: float = 4.0) -> None:
        """Принимает адрес вида "192.168.1.21" или "192.168.1.21:49152"."""
        self.host = host if ":" in host else f"{host}:{DEFAULT_UPNP_PORT}"
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._control_url: str | None = None

    async def _control(self, session: aiohttp.ClientSession) -> str:
        """Находит адрес управления RenderingControl в описании колонки."""
        if self._control_url:
            return self._control_url
        async with session.get(f"http://{self.host}/description.xml") as r:
            text = await r.text()
        for block in re.findall(r"<service>(.*?)</service>", text, re.S):
            if "RenderingControl" in block:
                found = re.search(r"<controlURL>(.*?)</controlURL>", block)
                if found:
                    path = found.group(1).strip()
                    self._control_url = (
                        f"http://{self.host}/{path.lstrip('/')}"
                    )
                    return self._control_url
        raise UpnpError(f"{self.host}: нет RenderingControl")

    async def _call(self, action: str, args: str = "") -> dict[str, str]:
        """Вызывает действие RenderingControl и разбирает ответ."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                url = await self._control(s)
                async with s.post(
                    url,
                    data=SOAP_BODY.format(action=action, args=args).encode(),
                    headers={
                        "Content-Type": 'text/xml; charset="utf-8"',
                        "SOAPACTION": f'"{RENDERING_CONTROL}#{action}"',
                    },
                ) as r:
                    text = await r.text()
                    if r.status >= 400:
                        raise UpnpError(f"{self.host} {action}: {r.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self._control_url = None
            raise UpnpError(f"{self.host} {action}: {e!r}") from e
        return dict(re.findall(r"<(\w+)>([^<]*)</\1>", text))

    async def get(self) -> tuple[int, bool]:
        """Возвращает (громкость 0–100, mute)."""
        volume = await self._call("GetVolume")
        mute = await self._call("GetMute")
        return (
            int(volume.get("CurrentVolume", 0)),
            mute.get("CurrentMute", "0") in ("1", "true"),
        )

    async def set(self, volume: int, mute: bool) -> None:
        """Устанавливает громкость и mute."""
        level = max(0, min(100, int(volume)))
        await self._call(
            "SetVolume", f"<DesiredVolume>{level}</DesiredVolume>"
        )
        await self._call(
            "SetMute", f"<DesiredMute>{1 if mute else 0}</DesiredMute>"
        )


class LinkedVolumeMirror:
    """Повторяет громкость ведущей колонки на связанных с ней.

    Стереопара MUZO (Linkplay) принимает громкость по AirPlay только
    ведущей колонкой; вторая остаётся на своей. Зеркало раз в секунду
    читает громкость и mute ведущей по UPnP и при изменении ставит то же
    связанным — откуда бы громкость ни меняли (OwnTone, телефон, кнопки).
    """

    def __init__(
        self,
        links: dict[str, list[str]],
        interval: float = POLL_INTERVAL,
    ) -> None:
        """links: адрес ведущей колонки → адреса связанных."""
        self._pairs = [
            (UpnpVolume(master), [UpnpVolume(h) for h in partners])
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
        """Останавливает зеркалирование."""
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            await self.sync_once()
            await asyncio.sleep(self._interval)

    async def sync_once(self) -> None:
        """Один проход: при изменении ведущей — повторить на связанных."""
        for master, partners in self._pairs:
            try:
                state = await master.get()
            except UpnpError as e:
                logger.debug(f"Зеркало: ведущая недоступна: {e}")
                continue
            if self._last.get(master.host) == state:
                continue
            ok = True
            for partner in partners:
                try:
                    await partner.set(*state)
                except UpnpError as e:
                    ok = False
                    logger.warning(f"⚠️ Зеркало громкости: {e}")
            if ok:
                self._last[master.host] = state
                logger.info(
                    f"🔗 Громкость {master.host} = {state[0]}"
                    f"{' (mute)' if state[1] else ''} → связанные"
                )
