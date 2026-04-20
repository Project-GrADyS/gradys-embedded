from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel

if TYPE_CHECKING:
    from gradys_embedded.runner.runner import EmbeddedRunner


class MessagePayload(BaseModel):
    message: str
    source: int


def _build_message_router(runner: "EmbeddedRunner") -> APIRouter:
    router = APIRouter()

    @router.post("/message")
    async def receive_message(payload: MessagePayload):
        if runner._encapsulator is None:
            raise HTTPException(status_code=409, detail="Protocol not started")
        runner._encapsulator.handle_packet(payload.message)
        return {"status": "ok"}

    return router


def _build_protocol_router(runner: "EmbeddedRunner") -> APIRouter:
    router = APIRouter(prefix="/protocol")

    @router.post("/setup")
    async def setup():
        if runner._setup_done:
            raise HTTPException(status_code=409, detail="Already set up")
        ok = await runner._goto_initial_position()
        if not ok:
            raise HTTPException(status_code=500, detail="Setup failed; check logs")
        runner._setup_done = True
        return {"status": "ok"}

    @router.post("/start")
    async def start():
        if not runner._setup_done:
            raise HTTPException(status_code=409, detail="Setup not completed")
        if runner._started:
            raise HTTPException(status_code=409, detail="Already started")
        await runner._bootstrap_protocol()
        runner._started = True
        return {"status": "ok"}

    return router


def create_app(runner: "EmbeddedRunner") -> FastAPI:
    app = FastAPI()
    app.include_router(_build_message_router(runner))
    app.include_router(_build_protocol_router(runner))
    return app
