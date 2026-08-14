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
from gradys_embedded.runner.configuration import MissionContext


class EmbeddedProvider(IProvider):
    """
    Implements the IProvider interface for the embedded runner. Translates protocol
    calls into HTTP requests to the UAV API and inter-node message API.
    """

    def __init__(self, context: MissionContext, loop: asyncio.AbstractEventLoop, timer_callback: Callable[[str], None], session: aiohttp.ClientSession, backend=None):
        self.node_id = context.node_id
        self.node_ip_dict = context.node_ip_dict
        self.origin_gps_coordinates = context.origin_gps_coordinates
        self.x_axis_degrees = context.x_axis_degrees
        self._timer_callback: Callable[[str], None] = timer_callback
        self._session = session

        self.tracked_variables = {}
        self._logger = logging.getLogger(__name__)

        self._loop = loop
        self._uav_base_url = f"http://localhost:{context.uav_api_port}"
        self._timers: dict[str, asyncio.TimerHandle] = {}

        # Set False by shutdown(). Everything this provider can emit -- mobility
        # commands, peer messages, timer callbacks -- is gated on it, so a
        # protocol that is still winding down cannot act on the vehicle after its
        # mission was stopped. Without this, an in-flight GOTO from a finished
        # mission can land on the drone after the next mission has taken over,
        # or after a stop has put the vehicle into RTL.
        self._active = True

        # Fire-and-forget tasks are otherwise unreferenced, so asyncio may
        # garbage-collect them mid-flight; holding them also lets shutdown()
        # cancel anything still in the air.
        self._pending_tasks: set[asyncio.Task] = set()

        # Inter-node message transport. The backend (one per communication_protocol) owns the
        # send/broadcast side; the local uav_api connection above always stays on plain HTTP and
        # is independent of it.
        self._backend = backend

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

    async def _post(self, url: str, json: dict, ssl=None) -> None:
        try:
            async with self._session.post(url, json=json, ssl=ssl) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    self._logger.error(f"POST {url} returned {resp.status}: {body}")
        except Exception as e:
            self._logger.error(f"POST {url} failed: {e}")

    def _fire_and_forget(self, coro) -> None:
        if not self._active:
            coro.close()
            self._logger.debug("Dropped a command from a stopped protocol")
            return
        task = self._loop.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        task.add_done_callback(self._log_task_exception)

    def _log_task_exception(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._logger.error(f"Fire-and-forget task failed: {task.exception()}")

    def send_communication_command(self, command: CommunicationCommand) -> None:
        if not self._active:
            self._logger.debug("Dropped a communication command from a stopped protocol")
            return

        if command.command_type == CommunicationCommandType.SEND:
            if command.destination is None:
                self._logger.warning("SEND command requires a destination")
                return
            payload = {"message": command.message, "source": self.node_id}
            self._backend.send(command.destination, payload)

        elif command.command_type == CommunicationCommandType.BROADCAST:
            payload = {"message": command.message, "source": self.node_id}
            self._backend.broadcast(payload)

    def send_mobility_command(self, command: MobilityCommand) -> None:
        if not self._active:
            # The vehicle may already be in RTL. Commanding GUIDED movement here
            # would either be ignored (disarmed) or fight the return.
            self._logger.debug("Dropped a mobility command from a stopped protocol")
            return

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
        if not self._active:
            self._logger.debug(f"Dropped timer {timer!r} from a stopped protocol")
            return
        handle = self._loop.call_at(timestamp, self._on_timer, timer)
        self._timers[timer] = handle

    def _on_timer(self, timer: str) -> None:
        self._timers.pop(timer, None)
        if not self._active:
            return
        if self._timer_callback is not None:
            self._timer_callback(timer)
        else:
            self._logger.warning("Timer fired but no timer callback is set")

    def cancel_timer(self, timer: str) -> None:
        handle = self._timers.pop(timer, None)
        if handle is not None:
            handle.cancel()

    def cancel_all_timers(self) -> None:
        """Cancel every scheduled timer.

        Required when a protocol ends: the statistics plugin reschedules its own
        `"statistics"` timer on every fire, so without this it keeps firing into
        a finished protocol forever.
        """
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()

    def shutdown(self) -> None:
        """Stop this provider emitting anything, and drop its scheduled work.

        Deliberately does NOT touch the aiohttp session — see close().
        """
        self._active = False
        self.cancel_all_timers()
        for task in list(self._pending_tasks):
            task.cancel()
        self._pending_tasks.clear()

    def current_time(self) -> float:
        return self._loop.time()

    def get_id(self) -> int:
        return self.node_id

    async def close(self) -> None:
        """Close the shared aiohttp session. Process-scoped — NOT per mission.

        The session is shared with the telemetry loop, the uav_api client and the
        peer transport, so calling this between missions would break all three.
        Use shutdown() to end a protocol; this belongs to process teardown only.
        """
        if self._session is not None and not self._session.closed:
            await self._session.close()


class EmbeddedEncapsulator(IEncapsulator):
    """
    Encapsulates the protocol to work with the embedded runner.
    """

    def __init__(self, context: MissionContext, loop: asyncio.AbstractEventLoop, session: aiohttp.ClientSession, backend=None):
        self.provider = EmbeddedProvider(context, loop, self.handle_timer, session, backend=backend)
        self._finished = False

    def encapsulate(self, protocol: Type[IProtocol]) -> None:
        self.protocol = protocol.instantiate(self.provider)
        self.provider.set_timer_callback(self.handle_timer)

    def initialize(self) -> None:
        self.protocol.initialize()

    # Inbound hooks are gated on _finished. The runner unbinds the encapsulator
    # on stop, but a packet already dispatched, or a telemetry tick already in
    # flight, can still arrive here afterwards.

    def handle_timer(self, timer: str) -> None:
        if self._finished:
            return
        self.protocol.handle_timer(timer)

    def handle_packet(self, message: str) -> None:
        if self._finished:
            return
        self.protocol.handle_packet(message)

    def handle_telemetry(self, telemetry: Telemetry) -> None:
        if self._finished:
            return
        self.protocol.handle_telemetry(telemetry)

    def finish(self) -> None:
        """End the protocol and drop everything it had scheduled or in flight.

        Order matters. `protocol.finish()` runs first because that is where a
        protocol flushes its data (the statistics plugin writes its CSVs from
        here), and it may legitimately use the provider while doing so. Only then
        is the provider shut down, which cancels its timers and stops it emitting
        anything further.

        Safe to call more than once, so a stop racing with process shutdown
        cannot double-finish a protocol.
        """
        if self._finished:
            return
        self._finished = True

        try:
            self.protocol.finish()
        finally:
            # Runs even if the protocol raised, otherwise a protocol that throws
            # in finish() would leave its timers live forever.
            self.provider.shutdown()
