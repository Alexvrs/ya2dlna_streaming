"""Решатель бесшовного режима: снимки Станции → действия."""

from __future__ import annotations

from dataclasses import dataclass, field

LISTEN_STATES = {"LISTENING", "BUSY"}

# Ключ снимка: трек, играет ли, позиция, длительность, состояние Алисы
SnapshotKey = tuple[str, bool, float, float, str | None]


@dataclass(frozen=True)
class Snapshot:
    """Состояние Станции в момент ``t``."""

    t: float
    track_id: str
    playing: bool
    progress: float
    duration: float
    alice: str | None
    type: str = "Track"
    next_id: str | None = None


@dataclass(frozen=True)
class StartSession:
    """Открыть сессию и поставить поток в OwnTone."""


@dataclass(frozen=True)
class AddSegment:
    """В момент Станции ``t`` трек ``track_id`` на позиции ``pos``."""

    track_id: str
    t: float
    pos: float
    sync: bool = False  # колонки вровень со Станцией: позиция + D
    fade_station: bool = False  # погасить Станцию, когда колонки зазвучат


@dataclass(frozen=True)
class EndSegment:
    """Тишина на колонках с момента Станции ``t``."""

    t: float
    # Трек вровень со Станцией: момент сдвигается на D назад, чтобы
    # колонки смолкли вместе со Станцией
    sync: bool = False


@dataclass(frozen=True)
class Prepare:
    """Предзапустить декодер следующего трека."""

    track_id: str


@dataclass(frozen=True)
class UnmuteStation:
    """Вернуть Станции громкость."""

    reason: str


@dataclass(frozen=True)
class StopSession:
    """Закрыть сессию, остановить колонки, вернуть звук Станции."""

    reason: str


@dataclass(frozen=True)
class Fallback:
    """Не бесшовный случай (радио) — классический путь."""


Action = (
    StartSession
    | AddSegment
    | EndSegment
    | Prepare
    | UnmuteStation
    | StopSession
    | Fallback
)


@dataclass
class GaplessDecider:
    """Состояние решателя между снимками."""

    event_lag: float = 1.3  # опоздание событий Станции (E5)
    grace: float = 4.0  # окно недостоверного progress нового трека
    grace_tolerance: float = 2.5
    jump_threshold: float = 3.0  # перемотка/повтор
    end_window: float = 3.0  # «у конца трека»
    pause_confirm: float = 2.0
    idle_stop: float = 60.0  # тишина после конца очереди → стоп
    pre_end_unmute: float = 1.5  # заранее вернуть Станцию (синхронный)
    prepare_before_end: float = 45.0  # предзапуск следующего за N с
    resume_after_skip: float = 2.0  # тот же трек после скипа — не ранее
    delay: float = 6.3  # задержка колонок D (обновляет исполнитель)

    session: bool = False
    track_id: str | None = None
    # Режим текущего трека. Синхронный: колонки вровень со Станцией,
    # первые D секунд играет сама Станция, затем гаснет (выбор
    # пользователя 29.10.2026) — на старте, после диджея и на стыке после
    # синхронного трека. Зеркальный: колонки повторяют Станцию с
    # задержкой D, Станция приглушена — после кроссфейда синхронного
    # трека и на стыках без диджея после зеркального
    sync: bool = False
    seg_open: bool = False
    anchor_t: float = 0.0
    anchor_pos: float = 0.0
    new_since: float = 0.0
    last_progress: float = 0.0
    last_duration: float = 0.0
    station_loud: bool = False
    paused_since: float | None = None
    ended_since: float | None = None
    prepared: set[str] = field(default_factory=set)
    last_alice: str | None = None
    last_key: SnapshotKey | None = None
    skipped_id: str | None = None
    skipped_pos: float = 0.0

    def reset(self) -> None:
        """Сессия закрыта: забыть трек и стык."""
        self.session = False
        self.track_id = None
        self.sync = False
        self.seg_open = False
        self.station_loud = False
        self.paused_since = None
        self.ended_since = None
        self.prepared.clear()
        self.last_key = None
        self.skipped_id = None

    def station_faded(self) -> None:
        """Менеджер погасил Станцию (колонки зазвучали)."""
        self.station_loud = False

    def feed(self, s: Snapshot) -> list[Action]:
        """Действия по новому снимку Станции."""
        # Цикл менеджера просыпается и без новых сообщений: тот же снимок
        # с новым временем дал бы ложную «перемотку» — пропускаем
        key: SnapshotKey = (
            s.track_id,
            s.playing,
            s.progress,
            s.duration,
            s.alice,
        )
        if key == self.last_key and s.type != "FmRadio":
            # Только проверки по времени (подтверждение паузы, конец
            # очереди): на паузе Станция шлёт одинаковые снимки
            if self.session and not s.playing:
                near = (
                    s.duration > 0
                    and s.duration - s.progress < self.end_window
                )
                return self._not_playing(s, near)
            return []
        self.last_key = key
        prev_alice, self.last_alice = self.last_alice, s.alice
        if s.type == "FmRadio":
            acts: list[Action] = []
            if self.session:
                acts.append(StopSession("радио"))
                self.reset()
            return acts + [Fallback()]

        acts = []
        near_end = s.duration > 0 and s.duration - s.progress < self.end_window
        same = s.track_id == self.track_id

        # Сигналы Станции (опыт E5): комментарий на стыке — alice → NONE
        # и playing=false у конца трека; скип с диджеем — LISTENING → NONE
        # при playing и прежнем id; пауза — playing=false не у конца
        # дольше pause_confirm
        if self.session and same and s.alice == "NONE":
            if prev_alice == "LISTENING" and s.playing:
                # Скип/«назад» с диджеем: LISTENING→NONE без BUSY (E5);
                # обычный вопрос Алисе идёт через BUSY
                if self.seg_open:
                    acts.append(EndSegment(s.t - self.event_lag, self.sync))
                    self.seg_open = False
                self.skipped_id = s.track_id
                self.skipped_pos = s.progress
                acts += self._unmute("диджей после команды")
            elif not s.playing and near_end:
                acts += self._unmute("комментарий диджея")

        if not self.session:
            if s.playing and s.track_id:
                self.session = True
                self.station_loud = True  # до старта звучит Станция
                acts.append(StartSession())
                acts += self._new_track(s, sync=True)
            return acts + self._prefetch(s)

        if s.playing:
            self.paused_since = None
            self.ended_since = None
            if not same:
                acts += self._on_new_id(s)
            elif not self.seg_open:
                resumed = s.alice not in LISTEN_STATES and s.alice != "NONE"
                if resumed and s.track_id == self.skipped_id:
                    # После скипа: тот же id ещё виден до нового трека
                    resumed = (
                        s.progress > self.skipped_pos + self.resume_after_skip
                    )
                if resumed:
                    # Тот же трек продолжился (после команды/паузы)
                    self.skipped_id = None
                    acts += self._segment(s)
            else:
                acts += self._check_jump(s)
            if (
                self.sync
                and not self.station_loud
                and s.duration > 0
                and s.duration - s.progress < self.pre_end_unmute
            ):
                acts += self._unmute("синхронный трек кончается")
        else:
            acts += self._not_playing(s, near_end)

        if s.track_id == self.track_id:
            self.last_progress = s.progress
            self.last_duration = s.duration
        return acts + self._prefetch(s)

    # --- правила ---------------------------------------------------------

    def _unmute(self, reason: str) -> list[Action]:
        if self.station_loud:
            return []
        self.station_loud = True
        return [UnmuteStation(reason)]

    def _on_new_id(self, s: Snapshot) -> list[Action]:
        if self.station_loud:
            return self._new_track(s, sync=True)
        old_left = self.last_duration - self.last_progress
        crossfade = self.seg_open and old_left > self.end_window
        if self.sync and crossfade:
            # Кроссфейд: колонки (вровень) доигрывают старый трек до его
            # конца, новый — вровень со Станцией с этого же момента
            return self._new_track(s, sync=True, start_after=old_left)
        if self.sync:
            # Обычный стык после синхронного трека, а Станция не
            # успела стать громкой — подхват с запозданием
            return self._unmute("стык без подготовки") + self._new_track(
                s, sync=True
            )
        return self._new_track(s, sync=False)

    def _new_track(
        self, s: Snapshot, sync: bool, start_after: float = 0.0
    ) -> list[Action]:
        self.track_id = s.track_id
        self.new_since = s.t
        self.sync = sync
        self.skipped_id = None
        self.last_progress = s.progress
        self.last_duration = s.duration
        return self._segment(s, start_after)

    def _segment(self, s: Snapshot, start_after: float = 0.0) -> list[Action]:
        """Сегмент по снимку; момент — с поправкой на опоздание события."""
        self.anchor_t = s.t
        self.anchor_pos = s.progress
        self.seg_open = True
        fade = self.sync and self.station_loud
        t = s.t - self.event_lag
        pos = s.progress
        if start_after > 0:
            # Кроссфейд вровень: новый трек — когда у колонок кончится
            # старый, через start_after после стыка; на шкале потока
            # это на D раньше
            shift = start_after - self.delay
            t += shift
            pos += shift
        return [AddSegment(s.track_id, t, pos, self.sync, fade)]

    def _check_jump(self, s: Snapshot) -> list[Action]:
        expected = self.anchor_pos + (s.t - self.anchor_t)
        tolerance = (
            self.grace_tolerance
            if s.t - self.new_since < self.grace
            else self.jump_threshold
        )
        if abs(s.progress - expected) <= tolerance:
            return []
        return self._segment(s)

    def _not_playing(self, s: Snapshot, near_end: bool) -> list[Action]:
        if near_end:
            # Естественный конец: трек доигрывает до EOF; если очередь
            # кончилась — через idle_stop закрываем
            self.paused_since = None
            if self.ended_since is None:
                self.ended_since = s.t
            elif s.t - self.ended_since >= self.idle_stop:
                self.reset()
                return [StopSession("очередь закончилась")]
            return []
        if s.alice in LISTEN_STATES:
            return []  # Станция слушает команду — не пауза
        if s.track_id != self.track_id:
            return []  # новый трек ещё не пошёл (duration=0, E5)
        if self.paused_since is None:
            self.paused_since = s.t
            return []
        if s.t - self.paused_since < self.pause_confirm:
            return []
        self.reset()
        return [StopSession("пауза")]

    def _prefetch(self, s: Snapshot) -> list[Action]:
        """Предзапуск следующего — ближе к концу (CDN рвёт простой)."""
        nid = s.next_id
        if not self.session or not nid or nid == s.track_id:
            return []
        if nid in self.prepared or s.duration <= 0:
            return []
        if s.duration - s.progress > self.prepare_before_end:
            return []
        self.prepared.add(nid)
        return [Prepare(nid)]
