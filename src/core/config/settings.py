import sys
from pathlib import Path
from typing import Literal

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
    )

    # Yandex Music settings
    ya_music_token: str

    # API server settings
    local_server_host: str
    local_server_port_dlna: int
    local_server_port_api: int

    # Устройство вывода: ruark (DLNA, Ruark R5) или owntone (AirPlay 2)
    output_backend: Literal["ruark", "owntone"] = "ruark"

    # Ruark R5 settings
    ruark_pin: str = "1234"
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
    # Колонки, повторяющие громкость ведущей (стереопара MUZO принимает
    # громкость по AirPlay только ведущей): UPnP-адрес ведущей → связанных,
    # например {"192.168.1.20": ["192.168.1.21"]}
    linked_speakers: dict[str, list[str]] = {}

    # Mode settings
    debug: bool = False

    # Stream settings
    stream_quality: str = "192"
    stream_is_local_file: bool = False
    # Предпочитать lossless (FLAC), когда он доступен для трека
    prefer_lossless: bool = True


settings = Settings()
