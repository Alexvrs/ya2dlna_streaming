import sys
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def resolve_env_path() -> Path:
    """Определяет путь к файлу .env."""
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / ".env"
    return Path(__file__).resolve().parent / ".env"


class Settings(BaseSettings):
    """Конфигурация приложения."""

    model_config = SettingsConfigDict(
        env_file=resolve_env_path(),
        env_prefix="APP_",
        extra="ignore",
        # Текст ошибки валидации не должен показывать токен и PIN из ввода
        hide_input_in_errors=True,
    )

    # Yandex Music settings
    ya_music_token: SecretStr

    # API server settings
    local_server_host: str
    local_server_port_dlna: int
    local_server_port_api: int

    # Устройство вывода: ruark (DLNA, Ruark R5) или owntone (AirPlay 2)
    output_backend: Literal["ruark", "owntone"] = "ruark"

    # Ruark R5 settings
    # Нужен только выводу на Ruark; для OwnTone — заглушка
    ruark_pin: SecretStr = SecretStr("1234")
    # Известный IP Ruark: быстрый старт без SSDP-скана всей сети
    ruark_host: str | None = None

    # OwnTone settings (output_backend=owntone)
    owntone_url: str = "http://localhost:3689"
    # Зоны: имя → имена выходов OwnTone, JSON в .env, например
    # {"Обитель": ["Колонка"], "Спальня": ["Спальня"]}
    owntone_zones: dict[str, list[str]] = {}
    owntone_default_zone: str | None = None
    # Потолок громкости: выше не поднимется ни из кода, ни с колонки
    owntone_max_volume: int = 40
    # Колонки, повторяющие громкость ведущей (стереопара WiiM/Linkplay
    # принимает громкость только ведущей): IP ведущей → IP связанных,
    # например {"192.168.1.20": ["192.168.1.21"]}
    linked_speakers: dict[str, list[str]] = {}
    # Стартовая громкость стрима, 0–100. OwnTone: только для первого
    # включения зоны, дальше зона включается с последней громкостью.
    # Не задана — громкость прошлого сеанса (поведение автора)
    start_volume: int | None = None

    # Mode settings
    debug: bool = False
    # Сырой журнал состояний Станции (опыт E5: комментарии диджея,
    # порядок событий на стыке треков). По умолчанию выключен
    station_raw_log: bool = False

    # Бесшовный поток (только output_backend=owntone): один непрерывный
    # WAV в OwnTone, треки по расписанию Станции с задержкой D.
    # По умолчанию выключен — классический путь
    gapless: bool = False
    # Писатель опережает реальное время не больше чем на lead секунд
    gapless_lead: float = 1.0
    # Запас на опоздание событий Станции и запуск декодера, секунды.
    # D ≈ буфер OwnTone (~3 с) + lead + margin. Запас должен покрыть
    # опоздание события (~1,3 с) и запуск декодера (1,1–1,5 с, E6)
    gapless_margin: float = 2.5

    # Stream settings
    stream_quality: str = "192"
    stream_is_local_file: bool = False
    # Предпочитать lossless (FLAC), когда он доступен для трека
    prefer_lossless: bool = True
    # «Алиса, громче» и «тише» во время стрима меняют громкость Ruark
    voice_volume_to_ruark: bool = True
    # Пауза дольше стольких минут выключает стрим, 0 — никогда
    idle_shutdown_minutes: float = 60.0


settings = Settings()
