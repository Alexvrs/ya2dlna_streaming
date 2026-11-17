"""Исключения команд API сервиса."""


class UnknownPlayerCommandError(ValueError):
    """Команды плееру с таким именем нет."""


class StreamNotRunningError(RuntimeError):
    """Стрим не идёт — Станцией управляет она сама."""


class NotATrackError(LookupError):
    """Сейчас играет не трек (радио) — оценивать нечего."""
