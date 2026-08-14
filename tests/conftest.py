"""Shared fixtures.

These tests never fly a drone and never contact uav_api. `StubRunner` stands in
for `EmbeddedRunner`, recording the vehicle commands a real runner would issue,
so the mission lifecycle can be exercised deterministically and in-process.
"""

import asyncio

import pytest

from gradys_embedded.encapsulator.embedded import EmbeddedEncapsulator
from gradys_embedded.runner.configuration import RunnerConfiguration
from gradys_embedded.runner.mission import MissionManager

DEMO_PROTOCOL_SRC = '''
from gradys_embedded.protocol.interface import IProtocol

class DemoProtocol(IProtocol):
    def initialize(self):
        self.provider.tracked_variables["ticks"] = 0
        self.provider.schedule_timer("t", self.provider.current_time() + 0.02)
    def handle_timer(self, timer):
        self.provider.tracked_variables["ticks"] += 1
        self.provider.schedule_timer("t", self.provider.current_time() + 0.02)
    def handle_packet(self, message): pass
    def handle_telemetry(self, telemetry): pass
    def finish(self): pass
'''


class StubRunner:
    """Records what a real runner would command instead of doing it."""

    def __init__(self, configuration, loop):
        self._configuration = configuration
        self._loop = loop
        self._session = None
        self._backend = None
        self._encapsulator = None
        self._default_protocol_class = None
        self.commands = []
        self.setup_succeeds = True
        self.mission = None

        # Data-plane bookkeeping. Mirrors the real runner's reuse rule so mission
        # tests can assert on transport switching without binding a real port;
        # the actual bind/unbind is covered against EmbeddedRunner itself.
        self.served_protocol = None
        self._backend_signature = None
        self.backend_starts = 0
        self.backend_stops = 0
        self.backend_reuses = 0

    async def resolve_frame(self, configuration):
        # The real runner queries uav_api to fill a frame the mission omitted.
        # Tests supply frames explicitly, so this is a pass-through. Deliberately
        # not recorded in `commands`, which asserts on vehicle actions only.
        return configuration

    async def start_backend(self, configuration):
        signature = (configuration.communication_protocol,
                     configuration.certfile, configuration.keyfile)
        if self._backend_signature == signature:
            self.backend_reuses += 1
            return
        if self._backend_signature is not None:
            self.backend_stops += 1
        self._backend_signature = signature
        self.served_protocol = configuration.communication_protocol
        self.backend_starts += 1

    async def stop_backend(self):
        if self._backend_signature is not None:
            self.backend_stops += 1
        self._backend_signature = None
        self.served_protocol = None

    async def goto_initial_position(self, configuration=None):
        self.commands.append("goto_initial_position")
        return self.setup_succeeds

    async def bootstrap_protocol(self, protocol_class, configuration=None):
        encapsulator = EmbeddedEncapsulator(
            configuration or self._configuration, self._loop, None, backend=None
        )
        encapsulator.encapsulate(protocol_class)
        self._encapsulator = encapsulator
        encapsulator.initialize()

    def teardown_protocol(self):
        encapsulator, self._encapsulator = self._encapsulator, None
        if encapsulator is not None:
            encapsulator.finish()

    def request_return_to_launch(self):
        self.commands.append("rtl")


@pytest.fixture
def configuration(tmp_path):
    return RunnerConfiguration(
        node_id=3,
        node_ip_dict={3: "127.0.0.1:5000"},
        initial_position=(0.0, 0.0, 10.0),
        uav_api_port=8000,
        control_api_port=6000,
        origin_gps_coordinates=(-15.840081, -47.926642, 0.0),
        x_axis_degrees=0.0,
        runs_dir=str(tmp_path / "runs"),
        protocols_dir=str(tmp_path / "protocols"),
        min_free_disk_mb=0,
    )


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


@pytest.fixture
def runner(configuration, loop):
    runner = StubRunner(configuration, loop)
    runner.mission = MissionManager(runner)
    return runner


@pytest.fixture
def demo_protocol(runner):
    """An uploaded protocol module, importable by name."""
    runner.mission.save_protocol("demo.py", DEMO_PROTOCOL_SRC.encode())
    return "demo:DemoProtocol"


@pytest.fixture
def provisioned_runner(tmp_path, loop):
    """A drone provisioned the way gradys-fleet renders it after the narrowing:
    machine-bound settings only, with no frame, initial position or peer map.

    The default `configuration` fixture keeps the older shape, so both the new
    and the transitional configurations stay covered.
    """
    configuration = RunnerConfiguration(
        node_id=3,
        uav_api_port=8000,
        control_api_port=6000,
        data_port=5000,
        runs_dir=str(tmp_path / "provisioned-runs"),
        protocols_dir=str(tmp_path / "provisioned-protocols"),
        min_free_disk_mb=0,
    )
    runner = StubRunner(configuration, loop)
    runner.mission = MissionManager(runner)
    # A distinct module name so it cannot be shadowed by the other fixture's
    # copy, which importlib would have already cached under "demo".
    runner.mission.save_protocol("bare_demo.py", DEMO_PROTOCOL_SRC.encode())
    return runner
