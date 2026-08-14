"""Control-panel HTTP app.

Three routers, all served over plain HTTP on `control_api_port` regardless of
`communication_protocol`:

  /protocols  manage the protocol modules available on this drone
  /mission    load, set up, start and stop the current mission
  /runs       list and download the data each run produced

`/protocol/setup` and `/protocol/start` are kept as aliases of the mission
lifecycle so existing operators, runbooks and gradys-sitl-tester keep working.

The inter-node data-plane `/message` endpoint lives with the HTTP transport in
`gradys_embedded/communication/http.py`; this module owns only the control panel.
"""

from __future__ import annotations

import io
import tarfile
from typing import TYPE_CHECKING, Dict, List, Optional

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from gradys_embedded.runner.mission import MissionError, MissionState

if TYPE_CHECKING:
    from gradys_embedded.runner.runner import EmbeddedRunner


class LoadRequest(BaseModel):
    """Everything that describes a run, as opposed to a machine.

    The frame and peer map are supplied here rather than provisioned because a
    fleet's mission layer (gradys-gs) holds them: the operator enters the frame
    once and it is fanned out to every drone. They must be identical across the
    fleet — `/mission/status` echoes them back so the caller can verify that
    before starting anything.
    """

    protocol: str
    initial_position: Optional[List[float]] = None
    label: Optional[str] = None

    origin_gps_coordinates: Optional[List[float]] = None
    x_axis_degrees: Optional[float] = None
    node_ip_dict: Optional[Dict[int, str]] = None

    # The transport. Loading binds the data plane for it, and only for it.
    # Defaults to the provisioned value (http unless changed), echoed in
    # /mission/status so the caller can confirm the fleet agrees.
    communication_protocol: Optional[str] = None
    auto_scout: Optional[bool] = None


def _mission(runner: "EmbeddedRunner"):
    if runner.mission is None:
        raise HTTPException(status_code=503, detail="Service is still starting up")
    return runner.mission


def _handle(exc: MissionError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _build_protocols_router(runner: "EmbeddedRunner") -> APIRouter:
    router = APIRouter(prefix="/protocols", tags=["protocols"])

    @router.get("", summary="List protocol modules available on this drone")
    async def list_protocols():
        return {"protocols": _mission(runner).list_protocols()}

    @router.post("/upload", summary="Upload a protocol module (.py)")
    async def upload_protocol(file: UploadFile = File(...)):
        content = await file.read()
        await file.close()
        try:
            path = _mission(runner).save_protocol(file.filename, content)
        except MissionError as exc:
            raise _handle(exc)
        return {"protocol": path.stem, "path": str(path), "size_bytes": len(content)}

    @router.delete("/{name}", summary="Remove an uploaded protocol module")
    async def delete_protocol(name: str):
        try:
            _mission(runner).delete_protocol(name)
        except MissionError as exc:
            raise _handle(exc)
        return {"protocol": name, "deleted": True}

    return router


def _build_mission_router(runner: "EmbeddedRunner") -> APIRouter:
    router = APIRouter(prefix="/mission", tags=["mission"])

    @router.get("/status", summary="Current mission state and live tracked variables")
    async def status():
        return _mission(runner).status()

    @router.post("/load", summary="Load a protocol and open a run directory")
    async def load(request: LoadRequest):
        try:
            return await _mission(runner).load(
                protocol=request.protocol,
                initial_position=request.initial_position,
                label=request.label,
                origin_gps_coordinates=request.origin_gps_coordinates,
                x_axis_degrees=request.x_axis_degrees,
                node_ip_dict=request.node_ip_dict,
                communication_protocol=request.communication_protocol,
                auto_scout=request.auto_scout,
            )
        except MissionError as exc:
            raise _handle(exc)

    @router.post("/setup", summary="Arm, take off and fly to the initial position")
    async def setup():
        try:
            return await _mission(runner).setup()
        except MissionError as exc:
            raise _handle(exc)

    @router.post("/start", summary="Start the loaded protocol")
    async def start():
        try:
            return await _mission(runner).start()
        except MissionError as exc:
            raise _handle(exc)

    @router.post("/stop", summary="Stop the protocol, flush run data and return to launch")
    async def stop():
        try:
            return await _mission(runner).stop()
        except MissionError as exc:
            raise _handle(exc)

    return router


def _build_runs_router(runner: "EmbeddedRunner") -> APIRouter:
    router = APIRouter(prefix="/runs", tags=["runs"])

    @router.get("", summary="List runs held on this drone, with disk usage")
    async def list_runs():
        return _mission(runner).list_runs()

    @router.get("/{run_id}", summary="Manifest for one run")
    async def describe_run(run_id: str):
        try:
            return _mission(runner).describe_run(run_id)
        except MissionError as exc:
            raise _handle(exc)

    @router.get("/{run_id}/archive", summary="Download the whole run as a .tar.gz")
    async def archive_run(run_id: str):
        mission = _mission(runner)
        try:
            path = mission.run_dir(run_id)
        except MissionError as exc:
            raise _handle(exc)

        # Built in memory: a run is a handful of small CSVs, and this avoids
        # writing a temporary file onto an SD card that may be nearly full.
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            archive.add(path, arcname=path.name)
        buffer.seek(0)

        return StreamingResponse(
            buffer,
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{path.name}.tar.gz"'},
        )

    @router.get("/{run_id}/files/{filename}", summary="Download one file from a run")
    async def run_file(run_id: str, filename: str):
        try:
            path = _mission(runner).run_file(run_id, filename)
        except MissionError as exc:
            raise _handle(exc)
        return FileResponse(path, filename=path.name)

    @router.delete("/{run_id}", summary="Delete a run to reclaim space")
    async def delete_run(run_id: str):
        try:
            _mission(runner).delete_run(run_id)
        except MissionError as exc:
            raise _handle(exc)
        return {"run_id": run_id, "deleted": True}

    return router


def _build_legacy_protocol_router(runner: "EmbeddedRunner") -> APIRouter:
    """The original `/protocol/setup` + `/protocol/start` pair.

    Preserved so existing operators, experiment runbooks and gradys-sitl-tester
    keep working unchanged. When the runner was constructed with a protocol class
    and no mission has been loaded over HTTP, setup loads that protocol first --
    which reproduces the old "construct with a protocol, POST setup, POST start"
    flow exactly, without reintroducing autostart at boot.
    """
    router = APIRouter(prefix="/protocol", tags=["protocol (legacy)"])

    @router.post("/setup", summary="Deprecated: use /mission/load then /mission/setup")
    async def setup():
        mission = _mission(runner)
        try:
            if mission.state is MissionState.IDLE:
                if runner._default_protocol_class is None:
                    raise MissionError(
                        "No protocol loaded. Use POST /mission/load, or construct "
                        "EmbeddedRunner with a protocol class.",
                        status_code=409,
                    )
                cls = runner._default_protocol_class
                await mission.load(
                    protocol=f"{cls.__module__}:{cls.__name__}",
                    protocol_class=cls,
                )
            await mission.setup()
        except MissionError as exc:
            raise _handle(exc)
        return {"status": "ok"}

    @router.post("/start", summary="Deprecated: use /mission/start")
    async def start():
        try:
            await _mission(runner).start()
        except MissionError as exc:
            raise _handle(exc)
        return {"status": "ok"}

    return router


def create_control_app(runner: "EmbeddedRunner") -> FastAPI:
    """Control-panel app.

    Served over plain HTTP on `control_api_port` for every transport,
    independently of `communication_protocol`. This is the surface an operator --
    or gradys-gs -- drives to run missions on a drone."""
    app = FastAPI(
        title="GrADyS Embedded",
        description="Mission control for a single drone's protocol runtime.",
    )
    app.include_router(_build_protocols_router(runner))
    app.include_router(_build_mission_router(runner))
    app.include_router(_build_runs_router(runner))
    app.include_router(_build_legacy_protocol_router(runner))
    return app
