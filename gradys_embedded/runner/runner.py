import asyncio
import logging
from dataclasses import replace
from typing import Optional, Type

import aiohttp
import uvicorn

from gradys_embedded.communication import (
    ZENOH_PROTOCOLS,
    CommunicationBackend,
    create_backend,
)
from gradys_embedded.encapsulator.embedded import EmbeddedEncapsulator
from gradys_embedded.protocol.interface import IProtocol
from gradys_embedded.protocol.messages.telemetry import Telemetry
from gradys_embedded.protocol.position import cartesian_to_geo, geo_to_cartesian
from gradys_embedded.runner.configuration import RunnerConfiguration
from gradys_embedded.runner.control_panel import create_control_app
from gradys_embedded.runner.mission import MissionManager, MissionState


class EmbeddedRunner:
    """Long-running service hosting one mission at a time.

    The runner owns everything process-scoped -- the event loop, the shared
    aiohttp session, the data-plane backend, the control server and the telemetry
    poll. Anything belonging to a single mission lives on :class:`MissionManager`,
    so a protocol can be swapped without restarting any of the above.

    `protocol` is accepted for backwards compatibility but is NOT started
    automatically: the service boots idle and runs a protocol only when told to
    over HTTP. A rebooting drone in the field must never spontaneously arm.
    """

    def __init__(self, configuration: RunnerConfiguration, protocol: Optional[Type[IProtocol]] = None):
        self._configuration = configuration
        self._default_protocol_class = protocol
        self._logger = logging.getLogger(__name__)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: aiohttp.ClientSession | None = None
        self._encapsulator: EmbeddedEncapsulator | None = None
        # Data-plane transport. Built at /mission/load, not at boot: the protocol
        # is a mission parameter, and only the chosen one is ever served.
        self._backend: CommunicationBackend | None = None
        self._backend_task: asyncio.Task | None = None
        self._backend_signature_current: tuple | None = None
        self._telemetry_task: asyncio.Task | None = None
        self._rtl_task: asyncio.Task | None = None
        self.mission: MissionManager | None = None

    # ------------------------------------------------------------------- boot

    def start_api(self) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        boot = self._loop.create_task(self._serve_communication())
        # Without this, an exception during boot is only ever seen as a
        # "Task exception was never retrieved" warning at garbage-collection.
        boot.add_done_callback(self._on_boot_finished)

        try:
            self._loop.run_forever()
        except KeyboardInterrupt:
            self._logger.info("Shutting down...")
        finally:
            self._shutdown()

    def _on_boot_finished(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._logger.critical(f"Service failed to start: {exc}", exc_info=exc)
            self._loop.stop()

    def _shutdown(self) -> None:
        if self.mission is not None:
            # Ends any live protocol and flushes its data. Deliberately does not
            # command the vehicle: a process shutdown is not a mission stop.
            self.mission.shutdown()

        if self._telemetry_task is not None:
            self._telemetry_task.cancel()

        # Release the data-plane listener before the blanket cancel below, so the
        # backend gets an orderly close() rather than only a cancellation.
        if self._backend is not None:
            self._loop.run_until_complete(self.stop_backend())

        pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )

        if self._session is not None:
            self._loop.run_until_complete(self._session.close())
        self._loop.close()

    async def resolve_frame(self, configuration: RunnerConfiguration) -> RunnerConfiguration:
        """Fill in any coordinate frame the mission did not supply.

        Runs at mission load, not at boot: the frame describes an experiment, so
        it arrives with the mission. Returns a configuration with the gaps filled
        from this drone's own telemetry.

        Falling back is a last resort. Each drone resolves its *own* position and
        heading, so a fleet that relies on this ends up with every drone in a
        different cartesian frame — "go to (50, 0, 20)" then means a different
        place on each one, and nothing raises. Hence the warning.
        """
        need_origin = configuration.origin_gps_coordinates is None
        need_heading = configuration.x_axis_degrees is None
        if not (need_origin or need_heading):
            return configuration

        missing = ", ".join(
            name for name, want in [("origin_gps_coordinates", need_origin),
                                    ("x_axis_degrees", need_heading)] if want
        )
        self._logger.warning(
            f"Mission did not supply {missing}; falling back to this drone's own "
            f"telemetry. Every drone in a fleet must be given the SAME frame, or "
            f"their cartesian coordinates will not agree."
        )

        async with self._session.get(
            f"http://localhost:{self._configuration.uav_api_port}/telemetry/gps"
        ) as resp:
            data = await resp.json()
            info = data["info"]

        overrides = {}
        if need_origin:
            pos = info["position"]
            overrides["origin_gps_coordinates"] = (pos["lat"], pos["lon"], pos["relative_alt"])
        if need_heading:
            overrides["x_axis_degrees"] = float(info["heading"])

        resolved = replace(configuration, **overrides)
        self._logger.info(
            f"Frame resolved: origin={resolved.origin_gps_coordinates} "
            f"x_axis_degrees={resolved.x_axis_degrees}"
        )
        return resolved

    async def _serve_communication(self) -> None:
        self._session = aiohttp.ClientSession()

        # The coordinate frame is no longer resolved here. It arrives with the
        # mission and is resolved in `resolve_frame` at load time, so boot no
        # longer depends on uav_api being reachable.
        self.mission = MissionManager(self)

        # Only the control plane is served at boot. The data plane's transport is a
        # mission parameter, so its backend is built and bound at /mission/load --
        # a drone with no mission listens on nothing but the control port.
        control_app = create_control_app(self)

        # Process-lifetime, not per-mission: it re-reads the encapsulator every
        # tick, so it follows a protocol swap on its own, and it is also what
        # detects the vehicle landing after a stop.
        self._telemetry_task = self._loop.create_task(self._periodic_telemetry())

        self._logger.info(
            f"Node {self._configuration.node_id} idle and awaiting a mission on "
            f"port {self._configuration.control_api_port}"
        )

        await self._serve_control(control_app)

    async def _serve_control(self, app) -> None:
        config = uvicorn.Config(app, host="0.0.0.0", port=self._configuration.control_api_port, loop="asyncio")
        server = uvicorn.Server(config)
        # Several servers share this loop; let the runner's own KeyboardInterrupt handling own
        # shutdown instead of each server racing to install process-wide signal handlers.
        server.install_signal_handlers = lambda: None
        await server.serve()

    # ------------------------------------------------------ data-plane backend

    # How long to let a transport unwind before cancelling it. Long enough for a
    # server to close its listening socket, short enough that a wedged transport
    # cannot hold up a mission.
    BACKEND_STOP_TIMEOUT = 5.0

    def _backend_signature(self, configuration: RunnerConfiguration) -> tuple:
        """What must match for a running backend to be reusable.

        The transport itself plus its TLS material — a mission that changes
        either needs a rebuilt listener. The peer map is deliberately excluded
        for the HTTP transports, which resolve a destination per send; zenoh
        wires its endpoints at session open, so it includes the map.
        """
        protocol = configuration.communication_protocol
        signature = (protocol, configuration.certfile, configuration.keyfile)
        if protocol in ZENOH_PROTOCOLS:
            peers = tuple(sorted((configuration.node_ip_dict or {}).items()))
            return signature + (configuration.auto_scout, peers)
        return signature

    async def start_backend(self, configuration: RunnerConfiguration) -> None:
        """Bind the data plane for this mission's transport.

        A no-op when the running backend already matches, so consecutive missions
        on the same transport do not rebind the port — which also avoids racing
        the previous listener's TIME_WAIT.
        """
        signature = self._backend_signature(configuration)
        if self._backend is not None and self._backend_signature_current == signature:
            return

        await self.stop_backend()

        backend = create_backend(self, configuration)
        self._backend = backend
        self._backend_signature_current = signature
        self._backend_task = self._loop.create_task(backend.serve())
        self._backend_task.add_done_callback(self._on_backend_finished)
        self._logger.info(
            f"Data plane serving {configuration.communication_protocol} "
            f"on port {configuration.resolve_data_port()}"
        )

    def _on_backend_finished(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # Otherwise a backend that fails to bind dies as an unretrieved task
            # exception and the drone silently has no data plane.
            self._logger.error(f"Data plane stopped unexpectedly: {exc}", exc_info=exc)

    async def stop_backend(self) -> None:
        """Release the data-plane port. Safe when nothing is running."""
        backend, self._backend = self._backend, None
        task, self._backend_task = self._backend_task, None
        self._backend_signature_current = None

        if backend is not None:
            try:
                await backend.close()
            except Exception as e:
                self._logger.error(f"Error closing the data plane: {e}")

        if task is not None:
            # close() asks serve() to stop; this waits for it to actually unwind.
            # Cancelling immediately would leave the listening socket open — the
            # server closes it while shutting down — and the next mission's
            # transport would then fail to bind the same port.
            try:
                await asyncio.wait_for(task, timeout=self.BACKEND_STOP_TIMEOUT)
            except asyncio.TimeoutError:
                # wait_for already cancelled it. A transport that will not stop
                # must not be able to block the next mission.
                self._logger.warning("Data plane did not stop in time; cancelled")
            except asyncio.CancelledError:
                pass
            except BaseException as e:
                # SystemExit included: uvicorn raises it on a failed bind, and it
                # must not escape teardown.
                self._logger.error(f"Data plane stopped with an error: {e!r}")

    # -------------------------------------------------------- mission actions

    async def goto_initial_position(self, configuration: RunnerConfiguration | None = None) -> bool:
        configuration = configuration or self._configuration
        base_url = f"http://localhost:{self._configuration.uav_api_port}"

        arm_result = await self._session.get(f"{base_url}/command/arm")
        if arm_result.status != 200:
            self._logger.fatal(f"Failed to arm UAV: {await arm_result.text()}")
            return False

        takeoff_result = await self._session.get(f"{base_url}/command/takeoff", params={"alt": configuration.initial_position[2]})
        if takeoff_result.status != 200:
            self._logger.fatal(f"Failed to take off UAV: {await takeoff_result.text()}")
            return False

        initial_gps_coordinates = cartesian_to_geo(configuration.origin_gps_coordinates, configuration.initial_position, configuration.x_axis_degrees)

        movement_result = await self._session.post(f"{base_url}/movement/go_to_gps_wait", json={"lat": initial_gps_coordinates[0], "long": initial_gps_coordinates[1], "alt": initial_gps_coordinates[2]})
        if movement_result.status != 200:
            self._logger.fatal(f"Failed to move UAV to initial position: {await movement_result.text()}")
            return False

        return True

    async def bootstrap_protocol(self, protocol_class: Type[IProtocol],
                                 configuration: RunnerConfiguration | None = None) -> None:
        """Install a protocol as the current mission's.

        The data-plane backend is untouched: both transports read
        `runner._encapsulator` per delivery, so rebinding it here re-routes
        inbound messages atomically without rebinding any port. The backend
        itself was bound earlier, at mission load, for this mission's transport.
        """
        configuration = configuration or self._configuration
        if self._backend is None:
            raise RuntimeError(
                "No data plane is serving; a mission must be loaded before a protocol starts."
            )
        encapsulator = EmbeddedEncapsulator(configuration, self._loop, self._session, backend=self._backend)
        encapsulator.encapsulate(protocol_class)
        self._encapsulator = encapsulator
        encapsulator.initialize()

    def teardown_protocol(self) -> None:
        """End the current protocol and unbind it. Safe when none is running."""
        encapsulator, self._encapsulator = self._encapsulator, None
        if encapsulator is None:
            return
        try:
            encapsulator.finish()
        except Exception as exc:
            # A protocol that throws in finish() must not leave the service
            # stuck in a state it cannot be commanded out of.
            self._logger.error(f"Protocol finish() failed: {exc}", exc_info=exc)

    def request_return_to_launch(self) -> None:
        """Command RTL without waiting for it.

        `/command/rtl` blocks until the vehicle is home *and* disarmed, up to
        ~250 seconds. Awaiting it would hold the stop request open for the whole
        descent and serialize a fleet-wide stop.
        """
        async def _rtl() -> None:
            url = f"http://localhost:{self._configuration.uav_api_port}/command/rtl"
            try:
                async with self._session.get(url) as resp:
                    if resp.status != 200:
                        self._logger.error(f"RTL returned {resp.status}: {await resp.text()}")
                    else:
                        self._logger.info("RTL complete; vehicle is home and disarmed")
            except Exception as exc:
                self._logger.error(f"RTL request failed: {exc}")

        self._logger.info("Commanding return to launch")
        task = self._loop.create_task(_rtl())
        # Held so asyncio cannot garbage-collect the request mid-flight.
        self._rtl_task = task

    # ---------------------------------------------------------------- telemetry

    async def _periodic_telemetry(self) -> None:
        base_url = f"http://localhost:{self._configuration.uav_api_port}"

        while True:
            # Read the ACTIVE MISSION's frame, not the process config. Mobility
            # commands are converted with the mission's frame (the provider is
            # built from the mission config), so converting telemetry with the
            # boot frame would put the protocol somewhere other than where it is
            # being sent -- with no error, visible only as strange flight data.
            configuration = self._active_configuration()
            interval = configuration.telemetry_interval
            origin = configuration.origin_gps_coordinates
            x_axis = configuration.x_axis_degrees

            try:
                async with self._session.get(f"{base_url}/telemetry/gps") as resp:
                    data = await resp.json()

                info = data["info"]
                pos = info["position"]
                geo_coords = (pos["lat"], pos["lon"], pos["relative_alt"])

                encapsulator = self._encapsulator
                # Before a mission has resolved a frame there is nothing to
                # convert against; landing detection below still runs.
                if encapsulator is not None and origin is not None and x_axis is not None:
                    cartesian = geo_to_cartesian(origin, geo_coords, x_axis)
                    encapsulator.handle_telemetry(Telemetry(current_position=cartesian))

                self._check_for_landing(pos.get("relative_alt"))

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._logger.error(f"Telemetry fetch failed: {e}")

            await asyncio.sleep(interval)

    def _active_configuration(self) -> RunnerConfiguration:
        """The configuration in force right now.

        The mission's overlay when one is loaded, otherwise what was provisioned.
        """
        if self.mission is not None and self.mission.mission_config is not None:
            return self.mission.mission_config
        return self._configuration

    def _check_for_landing(self, relative_alt) -> None:
        """Close out a RETURNING mission once the vehicle is back on the ground.

        uav_api exposes no flight-mode or armed-state endpoint, so altitude from
        the telemetry we already poll is the available signal.
        """
        if self.mission is None or self.mission.state is not MissionState.RETURNING:
            return
        if relative_alt is None:
            return
        if relative_alt <= self._configuration.telemetry_landed_alt:
            self.mission.note_landed()
