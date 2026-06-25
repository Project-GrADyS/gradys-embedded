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


def create_message_app(runner: "EmbeddedRunner") -> FastAPI:
    """Data-plane app: the inter-node `/message` endpoint only.

    Served on the node's message port (`node_ip_dict[node_id]`) by the selected
    `communication_protocol` transport. Kept separate from the control plane so the
    message port belongs entirely to the data plane (notably, so Zenoh's transport can
    own that port without colliding with a uvicorn control server)."""
    app = FastAPI()
    app.include_router(_build_message_router(runner))
    return app


def create_control_app(runner: "EmbeddedRunner") -> FastAPI:
    """Control-plane app: the `/protocol/setup` + `/protocol/start` endpoints only.

    Served over plain HTTP on `control_api_port` for every transport, independently of
    `communication_protocol`. This is the surface an operator drives to bring a drone up."""
    app = FastAPI()
    app.include_router(_build_protocol_router(runner))
    return app
