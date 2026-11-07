"""Этап 4: решатель бесшовного режима на сценариях из опыта E5."""

import pytest

from main_stream_service.gapless_decider import (
    AddSegment,
    EndSegment,
    Fallback,
    GaplessDecider,
    Prepare,
    Snapshot,
    StartSession,
    StopSession,
    UnmuteStation,
)


def snap(t, tid, prog, dur=200.0, playing=True, alice="IDLE", **kw):
    return Snapshot(t, tid, playing, prog, dur, alice, **kw)


def run(d, *snaps):
    acts = []
    for s in snaps:
        acts += d.feed(s)
    return acts


def kinds(acts, cls):
    return [a for a in acts if isinstance(a, cls)]


def started(d, t=100.0, tid="A", prog=50.0, dur=200.0):
    """Сессия идёт, трек A синхронный, Станция уже погашена."""
    acts = d.feed(snap(t, tid, prog, dur))
    assert isinstance(acts[0], StartSession)
    d.station_faded()
    return acts


def test_start_is_sync_handover_with_fade():
    d = GaplessDecider()
    acts = started(d)
    seg = kinds(acts, AddSegment)[0]
    assert (seg.track_id, seg.pos) == ("A", 50.0)
    assert seg.t == 100.0 - d.event_lag  # поправка на опоздание события
    assert seg.sync and seg.fade_station


def test_steady_playback_gives_no_actions():
    d = GaplessDecider()
    started(d)
    assert (
        run(d, *[snap(100.0 + i, "A", 50.0 + i) for i in range(1, 30)]) == []
    )


def test_natural_joint_with_comment():
    """IDLE→NONE + playing=false у конца, тишина, новый id через ~6 с."""
    d = GaplessDecider()
    started(d, prog=190.0)
    acts = run(
        d,
        snap(108.0, "A", 198.0, alice="IDLE"),
        snap(109.0, "A", 199.6, playing=False, alice="NONE"),
        snap(110.0, "A", 199.6, playing=False, alice="NONE"),
    )
    # Синхронный трек: Станция стала громкой ещё до конца
    assert len(kinds(acts, UnmuteStation)) == 1
    assert kinds(acts, EndSegment) == []  # трек доигрывает до конца
    acts = run(
        d,
        snap(115.0, "A", 199.6, playing=False, alice="IDLE"),
        snap(116.0, "B", 1.0, dur=180.0),
        snap(117.0, "B", 0.0, dur=180.0),  # недостоверный progress
        snap(118.0, "B", 3.0, dur=180.0),
    )
    segs = kinds(acts, AddSegment)
    assert len(segs) == 1
    assert segs[0].track_id == "B" and segs[0].sync and segs[0].fade_station
    assert kinds(acts, StopSession) == []


def test_crossfade_after_sync_starts_new_track_at_old_end():
    """Новый id, когда старому ещё 7 с до конца (кроссфейд).

    Колонки вровень доигрывают старый трек, новый начинается вровень
    со Станцией с того момента, когда у колонок кончится старый.
    """
    d = GaplessDecider(delay=6.3)
    started(d, prog=200.0, dur=222.0)
    run(d, snap(115.0, "A", 215.0, dur=222.0))
    acts = d.feed(snap(116.0, "B", 1.7, dur=259.0))
    seg = kinds(acts, AddSegment)[0]
    assert seg.track_id == "B" and seg.sync and not seg.fade_station
    shift = 7.0 - 6.3
    assert seg.t == pytest.approx(116.0 - d.event_lag + shift)
    assert seg.pos == pytest.approx(1.7 + shift)


def test_plain_joint_after_sync_track_is_bridged_by_station():
    d = GaplessDecider()
    started(d, prog=190.0, dur=200.0)
    acts = run(d, snap(109.0, "A", 199.0), snap(110.0, "B", 0.7))
    assert len(kinds(acts, UnmuteStation)) == 1  # заранее, до конца
    seg = kinds(acts, AddSegment)[0]
    assert seg.sync and seg.fade_station


def test_skip_with_dj_ends_segment_at_event_minus_lag():
    d = GaplessDecider(event_lag=1.3)
    started(d)
    acts = run(
        d,
        snap(110.0, "A", 60.0, alice="LISTENING"),
        snap(111.0, "A", 61.0, alice="NONE"),
    )
    ends = kinds(acts, EndSegment)
    assert len(ends) == 1 and ends[0].t == 111.0 - 1.3
    assert len(kinds(acts, UnmuteStation)) == 1
    # Диджей говорит ~8 с, трек на Станции «играет» с прежним id
    acts = run(
        d, *[snap(112.0 + i, "A", 62.0, alice="NONE") for i in range(6)]
    )
    assert kinds(acts, AddSegment) == []
    acts = d.feed(snap(119.0, "B", 0.5, alice="IDLE"))
    seg = kinds(acts, AddSegment)[0]
    assert seg.track_id == "B" and seg.sync and seg.fade_station


def test_seek_command_resegments_same_track_without_fade():
    d = GaplessDecider()
    started(d, prog=47.0)
    acts = run(
        d,
        snap(105.0, "A", 52.0, alice="LISTENING"),
        snap(107.0, "A", 54.0, alice="BUSY"),
        snap(108.0, "A", 113.0, alice="IDLE"),  # +60 с
    )
    segs = kinds(acts, AddSegment)
    assert len(segs) == 1 and segs[0].pos == 113.0
    assert segs[0].sync and not segs[0].fade_station
    assert kinds(acts, EndSegment) == []


def test_unreliable_progress_at_track_start_is_ignored():
    d = GaplessDecider()
    started(d, prog=0.0)
    acts = run(d, snap(101.0, "A", 0.0), snap(102.0, "A", 3.0))
    assert kinds(acts, AddSegment) == []


def test_pause_stops_after_confirmation_and_resume_restarts():
    d = GaplessDecider(pause_confirm=2.0)
    started(d)
    acts = run(
        d,
        snap(110.0, "A", 60.0, playing=False),
        snap(111.0, "A", 60.0, playing=False),
    )
    assert kinds(acts, StopSession) == []
    acts = d.feed(snap(112.5, "A", 60.0, playing=False))
    assert kinds(acts, StopSession)[0].reason == "пауза"
    acts = d.feed(snap(120.0, "A", 60.0))
    assert isinstance(acts[0], StartSession)


def test_listening_without_playing_is_not_pause():
    d = GaplessDecider()
    started(d)
    acts = run(
        d,
        *[
            snap(110.0 + i, "A", 60.0, playing=False, alice="LISTENING")
            for i in range(5)
        ],
    )
    assert kinds(acts, StopSession) == []


def test_queue_end_stops_after_idle_timeout():
    d = GaplessDecider(idle_stop=60.0)
    started(d, prog=199.0)
    acts = run(
        d,
        snap(101.0, "A", 199.8, playing=False),
        snap(150.0, "A", 199.8, playing=False),
    )
    assert kinds(acts, StopSession) == []
    acts = d.feed(snap(162.0, "A", 199.8, playing=False))
    assert kinds(acts, StopSession)[0].reason == "очередь закончилась"


def test_radio_falls_back_and_closes_session():
    d = GaplessDecider()
    started(d)
    acts = d.feed(snap(110.0, "R", 0.0, type="FmRadio"))
    assert isinstance(acts[0], StopSession) and isinstance(acts[-1], Fallback)
    assert d.session is False


def test_prefetch_once_per_next_id():
    d = GaplessDecider()
    acts = d.feed(snap(100.0, "A", 10.0, next_id="B"))
    assert kinds(acts, Prepare) == []  # рано: CDN рвёт простой
    acts = d.feed(snap(240.0, "A", 160.0, next_id="B"))
    acts += d.feed(snap(241.0, "A", 161.0, next_id="B"))
    assert [a.track_id for a in kinds(acts, Prepare)] == ["B"]


def test_track_continues_after_command_with_none():
    """LISTENING→NONE (диджей), но потом тот же трек снова играет."""
    d = GaplessDecider()
    started(d)
    run(
        d,
        snap(110.0, "A", 60.0, alice="LISTENING"),
        snap(111.0, "A", 61.0, alice="NONE"),
    )
    acts = d.feed(snap(115.0, "A", 65.0, alice="IDLE"))
    seg = kinds(acts, AddSegment)[0]
    assert seg.track_id == "A" and seg.pos == 65.0 and seg.fade_station


def test_question_to_alice_busy_to_none_is_not_skip():
    """Обычный вопрос: LISTENING→BUSY→NONE — колонки не обрываются."""
    d = GaplessDecider()
    started(d)
    acts = run(
        d,
        snap(110.0, "A", 60.0, alice="LISTENING"),
        snap(111.0, "A", 61.0, alice="BUSY"),
        snap(112.0, "A", 62.0, alice="NONE"),
        snap(114.0, "A", 64.0, alice="IDLE"),
    )
    assert kinds(acts, EndSegment) == [] and kinds(acts, AddSegment) == []


def test_repeated_snapshot_with_later_time_is_not_a_seek():
    """Цикл повторно подаёт то же сообщение — не перемотка."""
    d = GaplessDecider()
    started(d, prog=1.0)
    acts = run(
        d,
        snap(101.0, "A", 0.0),
        snap(101.6, "A", 0.0),  # то же сообщение, итерация позже
        snap(102.1, "A", 0.0),
    )
    assert kinds(acts, AddSegment) == []


def test_after_skip_same_id_is_not_reopened():
    """После скипа с диджеем NONE→IDLE с тем же id до нового трека."""
    d = GaplessDecider()
    started(d)
    run(
        d,
        snap(110.0, "A", 60.0, alice="LISTENING"),
        snap(111.0, "A", 61.0, alice="NONE"),
    )
    acts = d.feed(snap(118.0, "A", 61.0, alice="IDLE"))
    assert kinds(acts, AddSegment) == []


def test_new_id_not_playing_yet_is_not_pause():
    d = GaplessDecider(pause_confirm=2.0)
    started(d, prog=199.0)
    acts = run(
        d,
        snap(101.0, "A", 199.8, playing=False, alice="NONE"),
        *[snap(106.0 + i, "B", 0.0, dur=0.0, playing=False) for i in range(4)],
    )
    assert kinds(acts, StopSession) == []


def test_skip_in_sync_mode_carries_sync_flag():
    d = GaplessDecider()
    started(d)
    acts = run(
        d,
        snap(110.0, "A", 60.0, alice="LISTENING"),
        snap(111.0, "A", 61.0, alice="NONE"),
    )
    assert kinds(acts, EndSegment)[0].sync is True
