"""Shared fixtures.

These tests never fly a drone and never contact uav_api. `StubRunner` stands in
for `EmbeddedRunner`, recording the vehicle commands a real runner would issue,
so the mission lifecycle can be exercised deterministically and in-process.
"""

import asyncio

import pytest

from gradys_embedded.communication import ZENOH_PROTOCOLS
from gradys_embedded.encapsulator.embedded import EmbeddedEncapsulator
from gradys_embedded.runner.configuration import (
    MissionConfiguration,
    MissionContext,
    RunnerConfiguration,
)
from gradys_embedded.runner.mission import MissionManager

# The mission-scoped parameters most tests load with. Mirrors what a mission
# layer would put in POST /mission/load; node 3 matches the `configuration`
# fixture's node_id.
MISSION_KWARGS = dict(
    node_ip_dict={3: "127.0.0.1:5000"},
    initial_position=(0.0, 0.0, 10.0),
    origin_gps_coordinates=(-15.840081, -47.926642, 0.0),
    x_axis_degrees=0.0,
)

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
        # Mirrors EmbeddedRunner._backend_signature, zenoh branch included: a
        # zenoh mission that changes only its peer map or auto_scout rebinds in
        # production, so the stub must not count it as a reuse.
        signature = (configuration.communication_protocol,
                     configuration.certfile, configuration.keyfile)
        if configuration.communication_protocol in ZENOH_PROTOCOLS:
            peers = tuple(sorted((configuration.node_ip_dict or {}).items()))
            signature += (configuration.auto_scout, peers)
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

    async def bootstrap_protocol(self, protocol_class, configuration):
        encapsulator = EmbeddedEncapsulator(configuration, self._loop, None, backend=None)
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
        uav_api_port=8000,
        control_api_port=6000,
        data_port=5000,
        runs_dir=str(tmp_path / "runs"),
        protocols_dir=str(tmp_path / "protocols"),
        min_free_disk_mb=0,
    )


@pytest.fixture
def mission_context(configuration):
    """The effective configuration of a loaded mission, for tests that build
    encapsulators or backends directly rather than going through load()."""
    return MissionContext(configuration, MissionConfiguration(**MISSION_KWARGS))


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
