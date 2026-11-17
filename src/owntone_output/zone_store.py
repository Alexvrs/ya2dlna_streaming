import json
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

DEFAULT_STORE_PATH = Path("cache") / "owntone_zone.txt"


class ZoneStore:
    """Хранит выбранную зону воспроизведения."""

    # API-сервис и DLNA-сервер — отдельные процессы со своими
    # контроллерами OwnTone; общий файл в cache/ даёт им одну зону

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


DEFAULT_START_VOLUME_PATH = Path("cache") / "owntone_start_volume.json"


class StartVolumeStore:
    """Последняя громкость зон: с ней зона включается в следующий раз."""

    # Пишется при смене громкости зоны (в том числе при выключенном
    # стриме) и при остановке стрима; хранится, пока не перезапишут

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_START_VOLUME_PATH

    def _load(self) -> dict[str, int]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning(f"⚠️ Не удалось прочитать стартовую громкость: {e}")
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): int(v) for k, v in data.items() if isinstance(v, int)}

    def _save(self, data: dict[str, int]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            logger.warning(f"⚠️ Не удалось сохранить стартовую громкость: {e}")

    def get(self, zone: str) -> int | None:
        """Запомненная громкость зоны или None."""
        return self._load().get(zone)

    def put(self, zone: str, volume: int) -> None:
        """Запоминает громкость для следующего включения зоны."""
        data = self._load()
        data[zone] = volume
        self._save(data)

    def pop(self, zone: str) -> int | None:
        """Забирает громкость зоны (одноразово)."""
        data = self._load()
        volume = data.pop(zone, None)
        if volume is not None:
            self._save(data)
        return volume
