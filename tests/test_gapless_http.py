"""Этап 3 бесшовного потока: эндпоинты DLNA-сервера."""

import struct
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.config.settings import settings
from gapless import http as gh
from gapless.timeline import BYTES_PER_FRAME, SAMPLE_RATE


def req(hub=None, headers=None):
    state = SimpleNamespace(gapless=hub or gh.GaplessHub())
    return SimpleNamespace(
        app=SimpleNamespace(state=state),
        headers={k.lower(): v for k, v in (headers or {}).items()},
    )


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "gapless", True)
    monkeypatch.setattr(settings, "output_backend", "owntone")


async def test_start_refused_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "gapless", False)
    with pytest.raises(HTTPException) as e:
        await gh.gapless_start(req(), gh.StartBody())
    assert e.value.status_code == 409


async def test_start_refused_for_ruark(monkeypatch):
    monkeypatch.setattr(settings, "gapless", True)
    monkeypatch.setattr(settings, "output_backend", "ruark")
    with pytest.raises(HTTPException):
        await gh.gapless_start(req(), gh.StartBody())


async def test_start_segment_state_stop(enabled):
    r = req()
    res = await gh.gapless_start(r, gh.StartBody(lead=1.0, margin=2.0))
    assert res["stream_path"] == f"/live/{res['session']}.wav"
    hub = r.app.state.gapless
    t = hub.session.timeline.t0 + 1.0
    seg = await gh.gapless_segment(
        r,
        gh.SegmentBody(
            track_id="42",
            t_station=t,
            pos=0.0,
            url="http://x/a.mp3",
            title="Песня",
            artist="Артист",
            cover="avatars.yandex.net/get-music-content/1/2/%%",
        ),
    )
    assert seg["added"] is True
    # Повтор того же состояния — без нового сегмента
    again = await gh.gapless_segment(
        r, gh.SegmentBody(track_id="42", t_station=t + 1.0, pos=1.0)
    )
    assert again["added"] is False
    meta = hub.session.timeline.segments[0].meta
    assert meta["cover"].endswith("/cover/42.jpg")
    assert hub._cover_urls["42"].startswith("https://avatars.yandex.net/")
    assert hub._cover_urls["42"].endswith("/400x400")
    state = await gh.gapless_state(r)
    assert state["closed"] is False and state["session"] == res["session"]
    await gh.gapless_stop(r)
    assert (await gh.gapless_state(r)) == {"closed": True}


async def test_segment_without_session_is_409(enabled):
    with pytest.raises(HTTPException) as e:
        await gh.gapless_segment(
            req(), gh.SegmentBody(track_id="1", t_station=0.0, pos=0.0)
        )
    assert e.value.status_code == 409


async def test_new_start_closes_old_session(enabled):
    r = req()
    await gh.gapless_start(r, gh.StartBody())
    old = r.app.state.gapless.session
    await gh.gapless_start(r, gh.StartBody())
    assert old.closed and r.app.state.gapless.session is not old


async def test_probe_gets_header_and_silence_only(enabled):
    r = req(headers={"User-Agent": "Lavf/58.45.100"})
    res = await gh.gapless_start(r, gh.StartBody())
    resp = await gh.live(r, res["session"])
    body = resp.body
    assert body[:4] == b"RIFF"
    assert len(body) == 44 + SAMPLE_RATE * BYTES_PER_FRAME
    assert set(body[44:]) == {0}
    assert r.app.state.gapless.session.client_connected is False


async def test_live_unknown_session_404(enabled):
    r = req(headers={"User-Agent": "AirPlay/999.0.0"})
    await gh.gapless_start(r, gh.StartBody())
    with pytest.raises(HTTPException) as e:
        await gh.live(r, "s999")
    assert e.value.status_code == 404


async def test_live_player_gets_icy_headers(enabled):
    r = req(headers={"User-Agent": "AirPlay/999.0.0", "Icy-MetaData": "1"})
    res = await gh.gapless_start(r, gh.StartBody())
    resp = await gh.live(r, res["session"])
    assert resp.headers["icy-metaint"] == str(gh.IcyInterleaver().metaint)
    assert resp.media_type == "audio/wav"


class FakeSession:
    """Сессия-заглушка: два куска PCM, затем конец."""

    def __init__(self, metas):
        self.metas = metas
        self.frame = 0

    async def pcm(self):
        for _ in self.metas:
            self.frame += 4000
            yield bytes(4000 * BYTES_PER_FRAME)

    def meta_at(self, frame):
        return self.metas[frame // 4000]


async def test_body_keeps_last_title_during_silence():
    song = {"title": "Песня", "artist": "Артист", "cover": ""}
    s = FakeSession([song, {}])
    out = b"".join([c async for c in gh._body(s, icy=True)])
    assert out[:4] == b"RIFF"
    assert out.count("StreamTitle='Артист - Песня';".encode()) == 1
    assert b"StreamTitle='';" not in out


async def test_body_without_icy_is_plain_wav():
    s = FakeSession([{}])
    out = b"".join([c async for c in gh._body(s, icy=False)])
    assert out[:4] == b"RIFF" and len(out) == 44 + 4000 * BYTES_PER_FRAME
    assert struct.unpack_from("<I", out, 40)[0] == 0xFFFFFFFF


async def test_cover_404_when_unknown():
    with pytest.raises(HTTPException) as e:
        await gh.cover(req(), "nope")
    assert e.value.status_code == 404


async def test_cover_served_from_cache():
    hub = gh.GaplessHub()
    hub._covers["7"] = b"\xff\xd8jpeg"
    resp = await gh.cover(req(hub), "7")
    assert resp.body == b"\xff\xd8jpeg" and resp.media_type == "image/jpeg"
