from aiohttp import web
from aiohttp.test_utils import TestServer

from dlna_stream_server.handlers.stream_handler import resolve_redirects


async def start(app: web.Application) -> TestServer:
    server = TestServer(app)
    await server.start_server()
    return server


async def test_follows_308_chain_to_final_url():
    async def first(request: web.Request) -> web.Response:
        raise web.HTTPPermanentRedirect("/second?sign=abc")

    async def second(request: web.Request) -> web.Response:
        raise web.HTTPTemporaryRedirect("/track.mp3?sign=abc")

    async def track(request: web.Request) -> web.Response:
        return web.Response(body=b"\xff\xfb")

    app = web.Application()
    app.router.add_get("/get-mp3", first)
    app.router.add_get("/second", second)
    app.router.add_get("/track.mp3", track)
    server = await start(app)
    try:
        result = await resolve_redirects(str(server.make_url("/get-mp3")))
        assert result == str(server.make_url("/track.mp3?sign=abc"))
    finally:
        await server.close()


async def test_url_without_redirect_is_unchanged():
    async def track(request: web.Request) -> web.Response:
        return web.Response(body=b"\xff\xfb")

    app = web.Application()
    app.router.add_get("/track.mp3", track)
    server = await start(app)
    try:
        url = str(server.make_url("/track.mp3"))
        assert await resolve_redirects(url) == url
    finally:
        await server.close()


async def test_unreachable_host_returns_original_url():
    url = "http://127.0.0.1:9/get-mp3"
    assert await resolve_redirects(url, timeout=1.0) == url
