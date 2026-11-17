from typing import Protocol, runtime_checkable


class AudioOutput(Protocol):
    """Устройство вывода звука, на которое уходит локальный стрим."""

    # Реализации: Ruark R5 (DLNA + fsapi) и OwnTone (AirPlay 2, мультирум);
    # методы — те, что вызывают MainStreamManager и StreamHandler

    device_name: str

    async def connect(self, attempts: int = 3, delay: float = 2.0) -> bool:
        """Находит устройство; True, если оно готово принимать команды."""
        ...

    async def reconnect(self, attempts: int = 1, delay: float = 0.0) -> bool:
        """Повторно находит устройство после сбоя."""
        ...

    async def set_av_transport_uri(
        self,
        url: str,
        title: str = "",
        artist: str = "",
        mime_type: str = "audio/mpeg",
    ) -> None:
        """Задаёт адрес потока, который устройство будет играть."""
        ...

    async def play(self) -> None:
        """Запускает воспроизведение заданного потока."""
        ...

    async def stop(self) -> None:
        """Останавливает воспроизведение."""
        ...

    async def is_playing(self, timeout: float = 5.0) -> bool:
        """Проверяет, играет ли устройство."""
        ...

    async def get_volume(self) -> int:
        """Текущая громкость, 0–100."""
        ...

    async def set_volume(self, volume: int) -> None:
        """Устанавливает громкость, 0–100."""
        ...

    async def get_session_id(self) -> str:
        """Открывает сессию управления (если устройству она нужна)."""
        ...

    async def is_powered_on(self) -> bool:
        """Включено ли устройство."""
        ...

    async def turn_power_on(self) -> bool:
        """Включает устройство."""
        ...

    async def turn_power_off(self) -> bool:
        """Выключает устройство."""
        ...

    async def set_mode(self, mode_id: str) -> bool:
        """Переключает режим источника (Ruark: режим покоя при остановке)."""
        ...


@runtime_checkable
class MetadataOutput(Protocol):
    """Вывод, показывающий альбом и обложку трека (OwnTone)."""

    async def set_metadata(
        self,
        title: str = ...,
        artist: str = ...,
        album: str = ...,
        artwork_url: str = ...,
    ) -> None:
        """Запоминает метаданные трека для следующего play()."""
        ...


@runtime_checkable
class StartVolumeOutput(Protocol):
    """Вывод, который сам помнит громкость зон между сеансами (OwnTone)."""

    async def set_start_volume(self, volume: int) -> int:
        """Ставит стартовую громкость, возвращает применённую."""
        ...

    def remember_session_volume(self, volume: int) -> None:
        """Запоминает громкость сеанса при остановке стрима."""
        ...
