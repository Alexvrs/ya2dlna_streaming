from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

DEFAULT_STORE_PATH = Path("cache") / "owntone_zone.txt"


class ZoneStore:
    """Хранит выбранную зону воспроизведения.

    API-сервис и DLNA-сервер — отдельные процессы с отдельными
    контроллерами OwnTone; общий файл в cache/ даёт им одну зону.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_STORE_PATH

    def load(self) -> str | None:
        """Возвращает сохранённую зону или None, если её нет."""
        try:
            zone = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning(f"⚠️ Не удалось прочитать зону: {e}")
            return None
        return zone or None

    def save(self, zone: str) -> None:
        """Сохраняет выбранную зону."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(zone, encoding="utf-8")
        except OSError as e:
            logger.warning(f"⚠️ Не удалось сохранить зону: {e}")
