from aiohttp import web
from aiohttp.test_utils import TestServer

from owntone_output.linked_volume import LinkedVolumeMirror, UpnpVolume

DESCRIPTION = """<root><device><serviceList>
<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
<controlURL>/upnp/control/rendertransport1</controlURL></service>
<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
<controlURL>/upnp/control/rendercontrol1</controlURL></service>
</serviceList></device></root>"""


class FakeSpeaker:
    """UPnP RenderingControl колонки в памяти."""

    def __init__(self, volume: int, mute: bool = False) -> None:
        self.volume = volume
        self.mute = mute
        self.sets = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/description.xml", self.description)
        app.router.add_post("/upnp/control/rendercontrol1", self.control)
        return app

    async def description(self, request: web.Request) -> web.Response:
        return web.Response(text=DESCRIPTION, content_type="text/xml")

    async def control(self, request: web.Request) -> web.Response:
        action = request.headers["SOAPACTION"].strip('"').split("#")[1]
        body = await request.text()
        if action == "GetVolume":
            out = f"<CurrentVolume>{self.volume}</CurrentVolume>"
        elif action == "GetMute":
            out = f"<CurrentMute>{int(self.mute)}</CurrentMute>"
        elif action == "SetVolume":
            self.volume = int(body.split("<DesiredVolume>")[1].split("<")[0])
            self.sets += 1
            out = ""
        else:
            self.mute = body.split("<DesiredMute>")[1].split("<")[0] == "1"
            out = ""
        return web.Response(
            text=f"<s:Envelope><s:Body>{out}</s:Body></s:Envelope>",
            content_type="text/xml",
        )


async def serve(speaker: FakeSpeaker) -> tuple[TestServer, str]:
    server = TestServer(speaker.app())
    await server.start_server()
    return server, f"{server.host}:{server.port}"


async def test_upnp_volume_roundtrip():
    speaker = FakeSpeaker(27)
    server, host = await serve(speaker)
    try:
        upnp = UpnpVolume(host)
        assert await upnp.get() == (27, False)
        await upnp.set(5, True)
        assert (speaker.volume, speaker.mute) == (5, True)
    finally:
        await server.close()


async def test_mirror_copies_master_to_partner_once():
    master, partner = FakeSpeaker(6, mute=False), FakeSpeaker(27)
    s1, h1 = await serve(master)
    s2, h2 = await serve(partner)
    try:
        mirror = LinkedVolumeMirror({h1: [h2]})
        await mirror.sync_once()
        assert (partner.volume, partner.mute) == (6, False)
        # Без изменений ведущей связанная не трогается
        await mirror.sync_once()
        assert partner.sets == 1
        # Ведущую убавили до нуля с mute — повторяется
        master.volume, master.mute = 0, True
        await mirror.sync_once()
        assert (partner.volume, partner.mute) == (0, True)
    finally:
        await s1.close()
        await s2.close()


async def test_unreachable_partner_retried_next_pass():
    master = FakeSpeaker(10)
    s1, h1 = await serve(master)
    try:
        mirror = LinkedVolumeMirror({h1: ["127.0.0.1:9"]})
        await mirror.sync_once()
        assert h1 not in mirror._last
    finally:
        await s1.close()


async def test_mirror_without_links_does_nothing():
    mirror = LinkedVolumeMirror({})
    mirror.start()
    assert mirror._task is None
    await mirror.stop()
