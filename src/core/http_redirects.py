import asyncio
from logging import getLogger

import aiohttp
from yarl import URL

logger = getLogger(__name__)

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


async def try_resolve_redirects(
    url: str, max_hops: int = 5, timeout: float = 5.0
) -> str | None:
    """Проходит HTTP-перенаправления и возвращает конечный адрес.

    Returns:
        str | None: Конечный адрес или None при ошибке сети.
    """
    # Яндекс отдаёт трек через 308, который FFmpeg 4.x (Debian 11)
    # не понимает и читает мусор; конечный адрес открывает любой FFmpeg
    current = url
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            for _ in range(max_hops):
                async with session.get(current, allow_redirects=False) as resp:
                    location = resp.headers.get("Location")
                    if resp.status not in REDIRECT_STATUSES or not location:
                        return current
                    current = str(resp.url.join(URL(location)))
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        logger.warning(f"⚠️ Не удалось пройти перенаправления: {e!r}")
        return None
    return current


async def resolve_redirects(
    url: str, max_hops: int = 5, timeout: float = 5.0
) -> str:
    """То же, что try_resolve_redirects, но при ошибке — исходный URL."""
    final = await try_resolve_redirects(url, max_hops, timeout)
    return final if final is not None else url
