"""Этап 4: исполнитель бесшовного режима (сеть и устройства подменены)."""

import asyncio

import pytest

from main_stream_service import gapless_runner as gr
from main_stream_service.yandex_music_api import TrackInfo, TrackSource
from yandex_station.models import Track


class Station:
    def __init__(self):
        self.calls = []

    async def unmute(self):
        self.calls.append("unmute")

    async def fade_out_alice_volume(self, wait=0.0, delay=0.0, **_):
        self.calls.append("fade")


class Output:
    def __init__(self):
        self.calls = []
        self.progress = None

    async def set_av_transport_uri(self, url, title="", artist="", **_):
        self.calls.append(("uri", url))

    async def play(self):
        self.calls.append("play")

    async def stop(self):
        self.calls.append("stop")

    async def player_progress(self):
        return self.progress


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def track(tid="A", prog=50.0, dur=200.0, playing=True, t=1000.0, **kw):
    return Track(
        id=tid,
        title="Песня",
        type=kw.pop("type", "Track"),
        artist="Артист",
        duration=dur,
        progress=prog,
        playing=playing,
        captured_at=t,
        **kw,
    )


@pytest.fixture
def env(monkeypatch):
    clock = Clock()
    posts = []
    state = {"lost": False}

    async def resolve(tid):
        return (
            None
            if tid == "nolink"
            else TrackSource(f"http://cdn/{tid}", "mp3", True)
        )

    async def info(tid):
        return TrackInfo(
            title=f"t{tid}", artist="a", album="al", cover="avatars/%%"
        )

    runner = gr.GaplessRunner(Station(), Output(), resolve, info, clock=clock)

    async def post(path, body):
        if state["lost"] and path != "/gapless/start":
            raise gr.GaplessLost("409")
        posts.append((path, body))
        if path == "/gapless/start":
            return {"stream_path": "/live/s1.wav", "t0": 990.0}
        return {}

    async def get(path):
        return {"client_start_frame": 0, "t0": 990.0}

    monkeypatch.setattr(runner, "_post", post)
    monkeypatch.setattr(runner, "_get", get)
    return runner, posts, clock, state


def segs(posts):
    return [b for p, b in posts if p == "/gapless/segment"]


async def test_start_opens_session_and_plays_stream(env):
    runner, posts, clock, _ = env
    assert await runner.handle(track(), "IDLE") is False
    assert posts[0][0] == "/gapless/start"
    out = runner._output.calls
    assert out[0][0] == "uri" and out[0][1].endswith("/live/s1.wav")
    assert out[1] == "play"
    seg = segs(posts)[0]
    # Синхронный старт: позиция сдвинута на D
    assert seg["pos"] == pytest.approx(50.0 + runner.delay)
    assert seg["url"] == "http://cdn/A" and seg["title"] == "Песня"


async def test_handover_fades_station_after_delay(env, monkeypatch):
    runner, posts, clock, _ = env
    slept = []

    async def fake_sleep(dt):
        slept.append(dt)
        clock.t += dt

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    runner._output.progress = ("play", 0)
    d0 = runner.delay  # замер D после сегмента его меняет
    await runner.handle(track(), "IDLE")
    await runner._fade_task
    assert runner._station.calls == ["unmute", "fade"]
    lag = runner.decider.event_lag
    assert slept[0] == pytest.approx(d0 - lag + gr.FADE_AFTER_AUDIBLE)
    assert runner.decider.station_loud is False


async def test_missing_link_retries_on_next_snapshot(env):
    runner, posts, clock, _ = env
    await runner.handle(track("nolink"), "IDLE")
    assert segs(posts) == []
    assert runner.decider.seg_open is False


async def test_lost_session_restarts_on_next_snapshot(env):
    runner, posts, clock, state = env
    await runner.handle(track(), "IDLE")
    await runner._cancel_fade()
    state["lost"] = True
    await runner.handle(track("B", prog=0.5, t=1001.0), "IDLE")
    assert runner.decider.session is False
    state["lost"] = False
    await runner.handle(track("B", prog=1.5, t=1002.0), "IDLE")
    assert [p for p, _ in posts].count("/gapless/start") == 2


async def test_radio_returns_fallback_and_stops_session(env):
    runner, posts, clock, _ = env
    await runner.handle(track(), "IDLE")
    await runner._cancel_fade()
    fallback = await runner.handle(track("R", type="FmRadio"), "IDLE")
    assert fallback is True
    assert ("/gapless/stop", {}) in posts
    assert "stop" in runner._output.calls


async def test_delay_measured_from_owntone_progress(env):
    runner, posts, clock, _ = env
    await runner.handle(track(), "IDLE")
    await runner._cancel_fade()
    # Слышен кадр потока 0 + 10 с; t0 = 990 → слышно Станция 1000,
    # сейчас 1006,3 → D = 6,3
    runner._output.progress = ("play", 10_000)
    clock.t = 1006.3
    runner._last_measure = 0.0
    await runner._check_output()
    assert runner.delay == pytest.approx(6.3)
    assert runner.delay_measured


async def test_prepare_posts_decoder_warmup(env):
    runner, posts, clock, _ = env
    await runner.handle(track(prog=170.0, next_id="B"), "IDLE")
    await runner._cancel_fade()
    await asyncio.gather(*runner._bg)
    prep = [b for p, b in posts if p == "/gapless/prepare"]
    assert (
        prep
        and prep[0]["track_id"] == "B"
        and prep[0]["url"] == "http://cdn/B"
    )


async def test_fade_skipped_when_speakers_silent(env, monkeypatch):
    runner, posts, clock, _ = env

    async def fake_sleep(dt):
        clock.t += dt

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    runner._output.progress = ("stop", 0)
    await runner.handle(track(), "IDLE")
    await runner._fade_task
    assert "fade" not in runner._station.calls
    assert runner.decider.station_loud is True


async def test_error_in_segment_rolls_back_and_keeps_loop(env, monkeypatch):
    runner, posts, clock, _ = env

    async def boom(path, body):
        if path == "/gapless/segment":
            raise RuntimeError("сеть")
        return {"stream_path": "/live/s1.wav", "t0": 990.0}

    monkeypatch.setattr(runner, "_post", boom)
    assert await runner.handle(track(), "IDLE") is False
    assert runner.decider.seg_open is False


async def test_end_segment_in_sync_mode_shifted_by_delay(env):
    runner, posts, clock, _ = env
    await runner.handle(track(), "IDLE")
    await runner._cancel_fade()
    await runner.handle(track(prog=60.0, t=1010.0), "LISTENING")
    await runner.handle(track(prog=61.0, t=1011.0), "NONE")
    end = [b for p, b in posts if p == "/gapless/end"][0]
    assert end["t_station"] == pytest.approx(1011.0 - 1.3 - runner.delay)
