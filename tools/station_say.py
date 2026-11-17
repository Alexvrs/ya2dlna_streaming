"""Отправляет Станции текстовую команду, как будто её сказали голосом."""

# Запуск из корня проекта:
#   PYTHONPATH=src .venv/bin/python tools/station_say.py "включи кроссфейд"
# Glagol допускает несколько подключений — работающий стрим не трогается

import asyncio
import sys

from core.dependencies.main_di_container import MainDIContainer
from yandex_station.station_ws_control import YandexStationClient


async def main(text: str) -> int:
    """Отправляет фразу и печатает ответ; код выхода 1 — нет связи."""
    container = MainDIContainer().get_container()
    # Отдельный клиент, не синглтон сервиса: своё подключение к Станции
    client = YandexStationClient(
        device_finder=container.get(YandexStationClient).device_finder
    )
    await client.run_once()
    for _ in range(50):
        if client.authenticated:
            break
        await asyncio.sleep(0.2)
    if not client.authenticated:
        print("Станция не ответила (нет авторизации glagol)")
        await client.close()
        return 1
    response = await client.send_command({"command": "sendText", "text": text})
    print("Ответ Станции:", response.get("status"), response.get("error", ""))
    await asyncio.sleep(1)
    await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(" ".join(sys.argv[1:]) or "который час")))
