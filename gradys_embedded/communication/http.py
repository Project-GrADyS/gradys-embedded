"""HTTP-family transports: ``http``, ``https``, ``http3``.

All three share the inter-node ``/message`` FastAPI app (receive side) and the peer
``POST /message`` send path, so they live in one file:
  - ``http``  — HTTP/1.1 over TCP via uvicorn, plain.
  - ``https`` — HTTP/1.1 over TLS via uvicorn.
  - ``http3`` — HTTP/3 over QUIC via Hypercorn (server) + niquests (client); requires the
    ``gradys-embedded[http3]`` extra.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import TYPE_CHECKING

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel

from gradys_embedded.communication import certs
from gradys_embedded.communication.base import CommunicationBackend

if TYPE_CHECKING:
    from gradys_embedded.runner.runner import EmbeddedRunner


class MessagePayload(BaseModel):
    message: str
    source: int


def build_message_app(runner: "EmbeddedRunner") -> FastAPI:
    """Data-plane app: the inter-node ``/message`` endpoint only.

    Served on the node's message port (``node_ip_dict[node_id]``). Kept separate from the
    control plane (``control_panel.create_control_app``) so the message port belongs entirely to
    the data plane.
    """
    router = APIRouter()

    @router.post("/message")
    async def receive_message(payload: MessagePayload):
        if runner._encapsulator is None:
            raise HTTPException(status_code=409, detail="Protocol not started")
        runner._encapsulator.handle_packet(payload.message)
        return {"status": "ok"}

    app = FastAPI()
    app.include_router(router)
    return app


class HttpBackend(CommunicationBackend):
    """Serves ``/message`` over HTTP/HTTPS/HTTP3 and sends to peers over the matching scheme."""

    def __init__(self, runner: "EmbeddedRunner", configuration=None) -> None:
        super().__init__(runner, configuration)
        self._protocol = self._configuration.communication_protocol
        self._app = build_message_app(runner)

        # Held so close() can release the listener: the transport is chosen per
        # mission, so this server has to give the port back when the next
        # mission picks a different one.
        self._server: uvicorn.Server | None = None
        self._stop = asyncio.Event()

        # Server-side TLS material (https/http3 only).
        self._certfile: str | None = None
        self._keyfile: str | None = None
        if self._protocol in ("https", "http3"):
            self._certfile, self._keyfile = certs.resolve_tls_material(self._configuration, self._logger)

        # Client-side peer verification. The local uav_api connection (in the provider) always
        # stays on plain HTTP; this only governs inter-node sends over TLS.
        self._peer_certfile = self._configuration.certfile
        self._peer_ssl = ssl.create_default_context(cafile=self._peer_certfile) if self._peer_certfile else False
        self._h3_session = None

    async def _run_uvicorn(self, config: uvicorn.Config) -> None:
        server = uvicorn.Server(config)
        # Several servers share this loop (control plane + data plane); let the runner's own
        # KeyboardInterrupt handling own shutdown instead of each server racing to install
        # process-wide signal handlers.
        server.install_signal_handlers = lambda: None
        self._server = server
        try:
            await server.serve()
        finally:
            self._server = None

    async def serve(self) -> None:
        if self._protocol == "http":
            config = uvicorn.Config(self._app, host="0.0.0.0", port=self._port, loop="asyncio")
            await self._run_uvicorn(config)
        elif self._protocol == "https":
            config = uvicorn.Config(
                self._app, host="0.0.0.0", port=self._port, loop="asyncio",
                ssl_certfile=self._certfile, ssl_keyfile=self._keyfile,
            )
            await self._run_uvicorn(config)
        elif self._protocol == "http3":
            from hypercorn.config import Config
            from hypercorn.asyncio import serve

            config = Config()
            config.bind = [f"0.0.0.0:{self._port}"]
            config.quic_bind = [f"0.0.0.0:{self._port}"]
            config.certfile = self._certfile
            config.keyfile = self._keyfile
            # Hypercorn must never install its own signal handlers -- the runner
            # owns the loop -- but it does need a way out, so close() can hand the
            # port to the next mission's transport.
            await serve(self._app, config, shutdown_trigger=self._stop.wait)

    def send(self, dest_node_id: int, payload: dict) -> None:
        dest_addr = self._configuration.node_ip_dict.get(dest_node_id)
        if dest_addr is None:
            self._logger.warning(f"Unknown destination node {dest_node_id}")
            return
        self._fire_and_forget(self._send_to_peer(dest_addr, payload))

    def broadcast(self, payload: dict) -> None:
        for nid, addr in self._configuration.node_ip_dict.items():
            if nid != self._configuration.node_id:
                self._fire_and_forget(self._send_to_peer(addr, payload))

    async def _send_to_peer(self, addr: str, payload: dict) -> None:
        if self._protocol == "http":
            await self._post(f"http://{addr}/message", payload)
            return

        if self._protocol == "https":
            await self._post(f"https://{addr}/message", payload, ssl=self._peer_ssl)
            return

        # http3: lazily create a niquests HTTP/3 client session.
        if self._h3_session is None:
            import niquests
            self._h3_session = niquests.AsyncSession()
            self._h3_session.verify = self._peer_certfile or False

        url = f"https://{addr}/message"
        try:
            resp = await self._h3_session.post(url, json=payload)
            if resp.status_code != 200:
                self._logger.error(f"POST {url} returned {resp.status_code}: {resp.text}")
        except Exception as e:
            self._logger.error(f"POST {url} failed: {e}")

    async def _post(self, url: str, json: dict, ssl=None) -> None:
        try:
            async with self._runner._session.post(url, json=json, ssl=ssl) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    self._logger.error(f"POST {url} returned {resp.status}: {body}")
        except Exception as e:
            self._logger.error(f"POST {url} failed: {e}")

    async def close(self) -> None:
        # Releases the listening port. Without this the next mission's transport
        # cannot bind, and the http3 path would never return at all.
        self._stop.set()
        if self._server is not None:
            self._server.should_exit = True
        if self._h3_session is not None:
            await self._h3_session.close()
            self._h3_session = None
