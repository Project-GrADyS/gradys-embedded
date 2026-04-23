import asyncio
import logging
import aiohttp
from typing import Type, Callable, Optional


from gradys_embedded.protocol.interface import IProtocol, IProvider
from gradys_embedded.encapsulator.interface import IEncapsulator
from gradys_embedded.protocol.messages.communication import CommunicationCommand, CommunicationCommandType
from gradys_embedded.protocol.messages.mobility import MobilityCommand, MobilityCommandType
from gradys_embedded.protocol.messages.telemetry import Telemetry
from gradys_embedded.protocol.position import cartesian_to_geo
from gradys_embedded.runner.configuration import RunnerConfiguration


class EmbeddedProvider(IProvider):
    """
    Implements the IProvider interface for the embedded runner. Translates protocol
    calls into HTTP requests to the UAV API and inter-node message API.
    """

    def __init__(self, runner_configuration: RunnerConfiguration, loop: asyncio.AbstractEventLoop, timer_callback: Callable[[str], None], session: aiohttp.ClientSession):
        self.node_id = runner_configuration.node_id
        self.node_ip_dict = runner_configuration.node_ip_dict
        self.origin_gps_coordinates = runner_configuration.origin_gps_coordinates
        self.x_axis_degrees = runner_configuration.x_axis_degrees
        self._timer_callback: Callable[[str], None] = timer_callback
        self._session = session

        self.tracked_variables = {}
        self._logger = logging.getLogger(__name__)

        self._loop = loop
        self._uav_base_url = f"http://localhost:{runner_configuration.uav_api_port}"
        self._timers: dict[str, asyncio.TimerHandle] = {}

    def set_timer_callback(self, callback: Callable[[str], None]) -> None:
        self._timer_callback = callback

    async def _get(self, url: str, params: Optional[dict] = None) -> None:
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    self._logger.error(f"GET {url} returned {resp.status}: {body}")
        except Exception as e:
            self._logger.error(f"GET {url} failed: {e}")

    async def _post(self, url: str, json: dict) -> None:
        try:
            async with self._session.post(url, json=json) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    self._logger.error(f"POST {url} returned {resp.status}: {body}")
        except Exception as e:
            self._logger.error(f"POST {url} failed: {e}")

    def _fire_and_forget(self, coro) -> None:
        task = self._loop.create_task(coro)
        task.add_done_callback(self._log_task_exception)

    def _log_task_exception(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._logger.error(f"Fire-and-forget task failed: {task.exception()}")

    def send_communication_command(self, command: CommunicationCommand) -> None:
        if command.command_type == CommunicationCommandType.SEND:
            if command.destination is None:
                self._logger.warning("SEND command requires a destination")
                return
            dest_addr = self.node_ip_dict.get(command.destination)
            if dest_addr is None:
                self._logger.warning(f"Unknown destination node {command.destination}")
                return
            self._fire_and_forget(self._post(
                f"http://{dest_addr}/message",
                {"message": command.message, "source": self.node_id}
            ))

        elif command.command_type == CommunicationCommandType.BROADCAST:
            for nid, addr in self.node_ip_dict.items():
                if nid != self.node_id:
                    self._fire_and_forget(self._post(
                        f"http://{addr}/message",
                        {"message": command.message, "source": self.node_id}
                    ))

    def send_mobility_command(self, command: MobilityCommand) -> None:
        if command.command_type == MobilityCommandType.GOTO_COORDS:
            lat, lon, alt = cartesian_to_geo(self.origin_gps_coordinates, (command.param_1, command.param_2, command.param_3), self.x_axis_degrees)
            self._fire_and_forget(self._post(
                f"{self._uav_base_url}/movement/go_to_gps",
                {"lat": lat, "long": lon, "alt": alt, "look_at_target": False}
            ))

        elif command.command_type == MobilityCommandType.GOTO_GEO_COORDS:
            self._fire_and_forget(self._post(
                f"{self._uav_base_url}/movement/go_to_gps/",
                {"lat": command.param_1, "long": command.param_2, "alt": command.param_3, "look_at_target": False}
            ))

        elif command.command_type == MobilityCommandType.SET_SPEED:
            self._fire_and_forget(self._get(
                f"{self._uav_base_url}/command/set_air_speed",
                {"new_v": int(command.param_1)}
            ))

        else:
            self._logger.warning(f"Unknown mobility command type: {command.command_type}")

    def schedule_timer(self, timer: str, timestamp: float) -> None:
        handle = self._loop.call_at(timestamp, self._on_timer, timer)
        self._timers[timer] = handle

    def _on_timer(self, timer: str) -> None:
        self._timers.pop(timer, None)
        if self._timer_callback is not None:
            self._timer_callback(timer)
        else:
            self._logger.warning("Timer fired but no timer callback is set")

    def cancel_timer(self, timer: str) -> None:
        handle = self._timers.pop(timer, None)
        if handle is not None:
            handle.cancel()

    def current_time(self) -> float:
        return self._loop.time()

    def get_id(self) -> int:
        return self.node_id

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


class EmbeddedEncapsulator(IEncapsulator):
    """
    Encapsulates the protocol to work with the embedded runner.
    """

    def __init__(self, runner_configuration: RunnerConfiguration, loop: asyncio.AbstractEventLoop, session: aiohttp.ClientSession):
        self.provider = EmbeddedProvider(runner_configuration, loop, self.handle_timer, session)

    def encapsulate(self, protocol: Type[IProtocol]) -> None:
        self.protocol = protocol.instantiate(self.provider)
        self.provider.set_timer_callback(self.handle_timer)

    def initialize(self) -> None:
        self.protocol.initialize()

    def handle_timer(self, timer: str) -> None:
        self.protocol.handle_timer(timer)

    def handle_packet(self, message: str) -> None:
        self.protocol.handle_packet(message)

    def handle_telemetry(self, telemetry: Telemetry) -> None:
        self.protocol.handle_telemetry(telemetry)

    def finish(self) -> None:
        self.protocol.finish()
