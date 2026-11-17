from owntone_output.linked_volume import LinkedVolumeMirror, SpeakerError


class FakeSpeaker:
    """Колонка в памяти: громкость, mute и счётчик записей."""

    registry: dict[str, "FakeSpeaker"] = {}

    def __init__(self, host: str) -> None:
        self.host = host
        self.volume = 0
        self.mute = False
        self.sets = 0
        self.broken = False
        FakeSpeaker.registry[host] = self

    async def get(self) -> tuple[int, bool]:
        if self.broken:
            raise SpeakerError(self.host)
        return self.volume, self.mute

    async def set(self, volume: int, mute: bool) -> None:
        if self.broken:
            raise SpeakerError(self.host)
        self.volume, self.mute = volume, mute
        self.sets += 1

    async def close(self) -> None:
        pass


def make_mirror() -> tuple[LinkedVolumeMirror, FakeSpeaker, FakeSpeaker]:
    FakeSpeaker.registry.clear()
    mirror = LinkedVolumeMirror(
        {"master": ["partner"]}, speaker_factory=FakeSpeaker
    )
    return (
        mirror,
        FakeSpeaker.registry["master"],
        FakeSpeaker.registry["partner"],
    )


async def test_mirror_copies_master_to_partner_once():
    mirror, master, partner = make_mirror()
    master.volume, partner.volume = 6, 18

    await mirror.sync_once()
    assert (partner.volume, partner.mute) == (6, False)
    # Без изменений ведущей связанная не трогается
    await mirror.sync_once()
    assert partner.sets == 1
    # Ведущую убавили до нуля с mute — повторяется
    master.volume, master.mute = 0, True
    await mirror.sync_once()
    assert (partner.volume, partner.mute) == (0, True)


async def test_unreachable_partner_retried_next_pass():
    mirror, master, partner = make_mirror()
    master.volume = 10
    partner.broken = True

    await mirror.sync_once()
    assert partner.sets == 0
    partner.broken = False
    await mirror.sync_once()
    assert partner.volume == 10


async def test_unreachable_master_skipped():
    mirror, master, partner = make_mirror()
    master.broken = True
    await mirror.sync_once()
    assert partner.sets == 0


async def test_mirror_without_links_does_nothing():
    mirror = LinkedVolumeMirror({}, speaker_factory=FakeSpeaker)
    mirror.start()
    assert mirror._task is None
    await mirror.stop()
