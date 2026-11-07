from fastapi import APIRouter

from dlna_stream_server.endpoints.stream import router
from gapless.http import router as gapless_router

main_router = APIRouter()

main_router.include_router(router)
main_router.include_router(gapless_router)
