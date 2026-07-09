"""Control-panel HTTP app (`/protocol/*`).

The inter-node data-plane `/message` endpoint lives with the HTTP transport in
`gradys_embedded/communication/http.py`; this module owns only the control panel, which is always
served over plain HTTP on `control_api_port` regardless of `communication_protocol`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI, HTTPException

if TYPE_CHECKING:
    from gradys_embedded.runner.runner import EmbeddedRunner


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


def create_control_app(runner: "EmbeddedRunner") -> FastAPI:
    """Control-panel app: the `/protocol/setup` + `/protocol/start` endpoints only.

    Served over plain HTTP on `control_api_port` for every transport, independently of
    `communication_protocol`. This is the surface an operator drives to bring a drone up."""
    app = FastAPI()
    app.include_router(_build_protocol_router(runner))
    return app
