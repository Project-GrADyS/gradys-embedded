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
from gradys_embedded.runner.message_api import create_message_app


class EmbeddedRunner:
    def __init__(self, configuration: RunnerConfiguration, protocol: Type[IProtocol]):
        self._configuration = configuration
        self._protocol_class = protocol
        self._logger = logging.getLogger(__name__)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ready_to_run = False
        self._encapsulator: EmbeddedEncapsulator | None = None

    def setup(self) -> None:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()

        self._ready_to_run = self._loop.run_until_complete(self._goto_initial_position())

        return self._ready_to_run
    
    def run(self) -> None:
        if not self._ready_to_run:
            self._logger.fatal("Runner is not ready to run. Call 'setup' first.")
            return
        
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        self._loop.create_task(self._bootstrap())

        try:
            self._loop.run_forever()
        except KeyboardInterrupt:
            self._logger.info("Shutting down...")
        finally:
            if self._encapsulator is not None:
                self._encapsulator.finish()
            if self._session is not None:
                self._loop.run_until_complete(self._session.close())
            self._loop.close()

    async def _goto_initial_position(self) -> None:            
        if self._session is None:
            self._session = aiohttp.ClientSession()

        arm_result = await self._session.get(f"http://localhost:{self._configuration.uav_api_port}/command/arm")
        if arm_result.status != 200:
            self._logger.fatal(f"Failed to arm UAV: {await arm_result.text()}")
            return False
        
        takeoff_result = await self._session.get(f"http://localhost:{self._configuration.uav_api_port}/command/takeoff", params={"alt": self._configuration.initial_position[2]})
        if takeoff_result.status != 200:
            self._logger.fatal(f"Failed to take off UAV: {await takeoff_result.text()}")
            return False
        initial_gps_coordinates = cartesian_to_geo(self._configuration.origin_gps_coordinates, self._configuration.initial_position)
        
        movement_result = await self._session.post(f"http://localhost:{self._configuration.uav_api_port}/movement/go_to_gps_wait", json={"lat": initial_gps_coordinates[0], "long": initial_gps_coordinates[1], "alt": initial_gps_coordinates[2]})
        if movement_result.status != 200:
            self._logger.fatal(f"Failed to move UAV to initial position: {await movement_result.text()}")
            return False
        
        return True

    async def _bootstrap(self) -> None:
        self._encapsulator = EmbeddedEncapsulator(self._configuration, self._loop, self._session)
        self._encapsulator.encapsulate(self._protocol_class)
        self._encapsulator.initialize()

        self._loop.create_task(self._start_message_server())
        self._loop.create_task(self._periodic_telemetry())

    async def _start_message_server(self) -> None:
        own_addr = self._configuration.node_ip_dict[self._configuration.node_id]
        host, port_str = own_addr.rsplit(":", 1)
        port = int(port_str)

        app = create_message_app(self._encapsulator)
        config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
        server = uvicorn.Server(config)
        await server.serve()

    async def _periodic_telemetry(self) -> None:
        base_url = f"http://localhost:{self._configuration.uav_api_port}"
        interval = self._configuration.telemetry_interval
        origin = self._configuration.origin_gps_coordinates

        while True:
            try:
                async with self._session.get(f"{base_url}/telemetry/gps") as resp:
                    data = await resp.json()

                info = data["info"]
                pos = info["position"]
                geo_coords = (pos["lat"], pos["lon"], pos["relative_alt"])

                cartesian = geo_to_cartesian(origin, geo_coords)
                telemetry = Telemetry(current_position=cartesian)
                self._encapsulator.handle_telemetry(telemetry)

            except Exception as e:
                self._logger.error(f"Telemetry fetch failed: {e}")

            await asyncio.sleep(interval)
