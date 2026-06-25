import asyncio
import logging
from typing import Type

import aiohttp
import uvicorn

from gradys_embedded.encapsulator.embedded import EmbeddedEncapsulator
from gradys_embedded.protocol.interface import IProtocol
from gradys_embedded.protocol.messages.telemetry import Telemetry
from gradys_embedded.protocol.position import cartesian_to_geo, geo_to_cartesian
from gradys_embedded.runner.configuration import RunnerConfiguration
from gradys_embedded.runner.message_api import create_control_app, create_message_app


class EmbeddedRunner:
    def __init__(self, configuration: RunnerConfiguration, protocol: Type[IProtocol]):
        self._configuration = configuration
        self._protocol_class = protocol
        self._logger = logging.getLogger(__name__)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: aiohttp.ClientSession | None = None
        self._encapsulator: EmbeddedEncapsulator | None = None
        self._setup_done = False
        self._started = False
        # Zenoh data-plane session (only when communication_protocol == "zenoh").
        self._zenoh_session = None
        self._zenoh_subscribers: list = []

    def start_api(self) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._loop.create_task(self._serve_communication())

        try:
            self._loop.run_forever()
        except KeyboardInterrupt:
            self._logger.info("Shutting down...")
        finally:
            if self._encapsulator is not None:
                self._encapsulator.finish()
            if self._session is not None:
                self._loop.run_until_complete(self._session.close())
            if self._zenoh_session is not None:
                self._zenoh_session.close()
            self._loop.close()

    async def _ensure_origin_and_heading(self) -> None:
        need_origin = self._configuration.origin_gps_coordinates is None
        need_heading = self._configuration.x_axis_degrees is None
        if not (need_origin or need_heading):
            return

        missing = ", ".join(name for name, want in [("origin", need_origin), ("x_axis_degrees", need_heading)] if want)
        self._logger.info(f"Fetching UAV telemetry to resolve: {missing}")
        async with self._session.get(f"http://localhost:{self._configuration.uav_api_port}/telemetry/gps") as resp:
            data = await resp.json()
            info = data["info"]

        if need_origin:
            pos = info["position"]
            self._configuration.origin_gps_coordinates = (pos["lat"], pos["lon"], pos["relative_alt"])
            self._logger.info(f"Origin GPS coordinates set to: {self._configuration.origin_gps_coordinates}")
        if need_heading:
            self._configuration.x_axis_degrees = float(info["heading"])
            self._logger.info(f"x_axis_degrees set to: {self._configuration.x_axis_degrees}")

    async def _serve_communication(self) -> None:
        self._session = aiohttp.ClientSession()

        await self._ensure_origin_and_heading()

        own_addr = self._configuration.node_ip_dict[self._configuration.node_id]
        _, port_str = own_addr.rsplit(":", 1)
        port = int(port_str)

        # Control plane (/protocol/*) and data plane (/message) run as two independent
        # servers. The control plane is plain HTTP on control_api_port for every transport;
        # the data plane owns the node_ip_dict port via the selected communication_protocol.
        control_app = create_control_app(self)
        message_app = create_message_app(self)

        protocol = self._configuration.communication_protocol
        if protocol == "http":
            data_plane = self._serve_http(message_app, port)
        elif protocol == "https":
            data_plane = self._serve_https(message_app, port)
        elif protocol == "http3":
            data_plane = self._serve_http3(message_app, port)
        elif protocol == "zenoh":
            data_plane = self._serve_zenoh(port)
        else:
            raise ValueError(f"Invalid communication_protocol {protocol!r}")

        await asyncio.gather(self._serve_control(control_app), data_plane)

    def _resolve_tls_material(self) -> tuple[str, str]:
        """Return (certfile, keyfile) from configuration, or generate an ephemeral self-signed pair."""
        certfile = self._configuration.certfile
        keyfile = self._configuration.keyfile
        if certfile is None or keyfile is None:
            from gradys_embedded.runner.certs import generate_self_signed_cert

            certfile, keyfile = generate_self_signed_cert()
            self._logger.info("No TLS cert provided; using an ephemeral self-signed certificate for the message-API server")
        return certfile, keyfile

    async def _run_uvicorn(self, config: uvicorn.Config) -> None:
        server = uvicorn.Server(config)
        # Several servers share this loop (control plane + data plane); let the runner's
        # own KeyboardInterrupt handling own shutdown instead of each server racing to
        # install process-wide signal handlers.
        server.install_signal_handlers = lambda: None
        await server.serve()

    async def _serve_control(self, app) -> None:
        config = uvicorn.Config(app, host="0.0.0.0", port=self._configuration.control_api_port, loop="asyncio")
        await self._run_uvicorn(config)

    async def _serve_http(self, app, port: int) -> None:
        config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
        await self._run_uvicorn(config)

    async def _serve_https(self, app, port: int) -> None:
        certfile, keyfile = self._resolve_tls_material()
        config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio", ssl_certfile=certfile, ssl_keyfile=keyfile)
        await self._run_uvicorn(config)

    def _build_zenoh_config(self, port: int):
        """Build a Zenoh peer-mode Config. Option A (auto_scout) uses default multicast
        scouting; Option B builds explicit connect.endpoints from node_ip_dict."""
        import json

        import zenoh

        cfg = zenoh.Config()
        cfg.insert_json5("mode", json.dumps("peer"))
        if self._configuration.auto_scout:
            # Option A: rely on Zenoh's default UDP multicast scouting for discovery.
            return cfg

        # Option B: no multicast; connect explicitly to peers listed in node_ip_dict.
        cfg.insert_json5("scouting/multicast/enabled", "false")
        cfg.insert_json5("scouting/gossip/enabled", "true")
        own_addr = self._configuration.node_ip_dict[self._configuration.node_id]
        own_ip = own_addr.rsplit(":", 1)[0]
        cfg.insert_json5("listen/endpoints", json.dumps([f"tcp/{own_ip}:{port}"]))
        connect = [
            f"tcp/{addr}"
            for nid, addr in self._configuration.node_ip_dict.items()
            if nid != self._configuration.node_id
        ]
        cfg.insert_json5("connect/endpoints", json.dumps(connect))
        return cfg

    async def _serve_zenoh(self, port: int) -> None:
        import json

        import zenoh

        self._zenoh_session = zenoh.open(self._build_zenoh_config(port))

        def on_sample(sample) -> None:
            # Runs on a Zenoh background thread — marshal onto the runner's event loop.
            if self._encapsulator is None:
                return  # protocol not started yet; drop, mirroring /message 409-before-start
            try:
                data = json.loads(bytes(sample.payload))
                message = data["message"]
            except Exception as e:
                self._logger.error(f"Failed to decode zenoh message: {e}")
                return
            self._loop.call_soon_threadsafe(self._encapsulator.handle_packet, message)

        node_id = self._configuration.node_id
        self._zenoh_subscribers = [
            self._zenoh_session.declare_subscriber(f"gradys/msg/{node_id}", on_sample),
            self._zenoh_session.declare_subscriber("gradys/msg/broadcast", on_sample),
        ]
        self._logger.info(
            f"Zenoh peer session open (auto_scout={self._configuration.auto_scout}); "
            f"subscribed to gradys/msg/{node_id} and gradys/msg/broadcast"
        )
        # Keep the data plane alive for the lifetime of the runner.
        await asyncio.Future()

    async def _serve_http3(self, app, port: int) -> None:
        from hypercorn.config import Config
        from hypercorn.asyncio import serve

        certfile, keyfile = self._resolve_tls_material()

        config = Config()
        config.bind = [f"0.0.0.0:{port}"]
        config.quic_bind = [f"0.0.0.0:{port}"]
        config.certfile = certfile
        config.keyfile = keyfile

        # The runner's event loop owns the lifecycle; never let Hypercorn self-shutdown.
        await serve(app, config, shutdown_trigger=lambda: asyncio.Future())

    async def _goto_initial_position(self) -> bool:
        arm_result = await self._session.get(f"http://localhost:{self._configuration.uav_api_port}/command/arm")
        if arm_result.status != 200:
            self._logger.fatal(f"Failed to arm UAV: {await arm_result.text()}")
            return False

        takeoff_result = await self._session.get(f"http://localhost:{self._configuration.uav_api_port}/command/takeoff", params={"alt": self._configuration.initial_position[2]})
        if takeoff_result.status != 200:
            self._logger.fatal(f"Failed to take off UAV: {await takeoff_result.text()}")
            return False
        initial_gps_coordinates = cartesian_to_geo(self._configuration.origin_gps_coordinates, self._configuration.initial_position, self._configuration.x_axis_degrees)

        movement_result = await self._session.post(f"http://localhost:{self._configuration.uav_api_port}/movement/go_to_gps_wait", json={"lat": initial_gps_coordinates[0], "long": initial_gps_coordinates[1], "alt": initial_gps_coordinates[2]})
        if movement_result.status != 200:
            self._logger.fatal(f"Failed to move UAV to initial position: {await movement_result.text()}")
            return False

        return True

    async def _bootstrap_protocol(self) -> None:
        self._encapsulator = EmbeddedEncapsulator(self._configuration, self._loop, self._session, zenoh_session=self._zenoh_session)
        self._encapsulator.encapsulate(self._protocol_class)
        self._encapsulator.initialize()

        self._loop.create_task(self._periodic_telemetry())

    async def _periodic_telemetry(self) -> None:
        base_url = f"http://localhost:{self._configuration.uav_api_port}"
        interval = self._configuration.telemetry_interval
        origin = self._configuration.origin_gps_coordinates
        x_axis = self._configuration.x_axis_degrees

        while True:
            try:
                async with self._session.get(f"{base_url}/telemetry/gps") as resp:
                    data = await resp.json()

                info = data["info"]
                pos = info["position"]
                geo_coords = (pos["lat"], pos["lon"], pos["relative_alt"])

                cartesian = geo_to_cartesian(origin, geo_coords, x_axis)
                telemetry = Telemetry(current_position=cartesian)
                self._encapsulator.handle_telemetry(telemetry)

            except Exception as e:
                self._logger.error(f"Telemetry fetch failed: {e}")

            await asyncio.sleep(interval)
