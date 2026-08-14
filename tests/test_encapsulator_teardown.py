"""Encapsulator teardown — what makes swapping a protocol in-place safe.

These are the guarantees the mission layer depends on. Without them a finished
protocol keeps its timers alive and can still command the vehicle, which on a
drone means fighting an RTL or flying under the next mission's protocol.
"""

import asyncio

import pytest

from gradys_embedded.encapsulator.embedded import EmbeddedEncapsulator
from gradys_embedded.protocol.interface import IProtocol
from gradys_embedded.protocol.messages.communication import (
    CommunicationCommand,
    CommunicationCommandType,
)
from gradys_embedded.protocol.messages.mobility import MobilityCommand, MobilityCommandType

FIRED = []


class SelfRescheduling(IProtocol):
    """Reschedules its own timer forever, like the statistics plugin does."""

    label = "first"

    def initialize(self):
        self.provider.tracked_variables["hits"] = 0
        self.provider.schedule_timer("tick", self.provider.current_time() + 0.02)

    def handle_timer(self, timer):
        FIRED.append(self.label)
        self.provider.tracked_variables["hits"] += 1
        self.provider.schedule_timer("tick", self.provider.current_time() + 0.02)

    def handle_packet(self, message):
        pass

    def handle_telemetry(self, telemetry):
        pass

    def finish(self):
        pass


class Second(SelfRescheduling):
    label = "second"


class RecordingBackend:
    def __init__(self):
        self.sent = []

    def send(self, dest_node_id, payload):
        self.sent.append(("send", dest_node_id))

    def broadcast(self, payload):
        self.sent.append(("broadcast", None))


@pytest.fixture(autouse=True)
def _clear():
    FIRED.clear()
    yield
    FIRED.clear()


def _run(mission_context, loop, protocol, backend=None):
    encapsulator = EmbeddedEncapsulator(mission_context, loop, None, backend=backend)
    encapsulator.encapsulate(protocol)
    encapsulator.initialize()
    return encapsulator


def test_finish_cancels_every_timer(mission_context, loop):
    encapsulator = _run(mission_context, loop, SelfRescheduling)
    loop.run_until_complete(asyncio.sleep(0.08))
    assert encapsulator.provider.tracked_variables["hits"] >= 2

    encapsulator.finish()

    assert encapsulator.provider._timers == {}
    assert encapsulator.provider._active is False


def test_no_timer_leaks_into_the_next_mission(mission_context, loop):
    first = _run(mission_context, loop, SelfRescheduling)
    loop.run_until_complete(asyncio.sleep(0.08))
    first.finish()

    FIRED.clear()
    second = _run(mission_context, loop, Second)
    loop.run_until_complete(asyncio.sleep(0.08))

    assert "second" in FIRED
    assert "first" not in FIRED, "a finished protocol's timer fired into the next mission"
    second.finish()


def test_stopped_protocol_cannot_command_the_vehicle(mission_context, loop):
    """After a stop the vehicle is returning, and may already be disarmed."""
    encapsulator = _run(mission_context, loop, SelfRescheduling)
    encapsulator.finish()

    encapsulator.provider.send_mobility_command(
        MobilityCommand(command_type=MobilityCommandType.GOTO_COORDS,
                        param_1=1.0, param_2=2.0, param_3=3.0)
    )
    assert encapsulator.provider._pending_tasks == set()


def test_stopped_protocol_cannot_send_messages(mission_context, loop):
    backend = RecordingBackend()
    encapsulator = _run(mission_context, loop, SelfRescheduling, backend=backend)

    encapsulator.provider.send_communication_command(
        CommunicationCommand(command_type=CommunicationCommandType.BROADCAST, message="before")
    )
    assert len(backend.sent) == 1

    encapsulator.finish()
    encapsulator.provider.send_communication_command(
        CommunicationCommand(command_type=CommunicationCommandType.BROADCAST, message="after")
    )
    assert len(backend.sent) == 1, "a stopped protocol still reached the network"


def test_late_delivery_to_a_finished_mission_is_dropped(mission_context, loop):
    """A packet already dispatched, or a telemetry tick in flight, can land late."""
    encapsulator = _run(mission_context, loop, SelfRescheduling)
    encapsulator.finish()
    FIRED.clear()

    encapsulator.handle_packet("late")
    encapsulator.handle_timer("tick")

    assert FIRED == []


def test_finish_is_idempotent(mission_context, loop):
    encapsulator = _run(mission_context, loop, SelfRescheduling)
    encapsulator.finish()
    encapsulator.finish()


def test_finish_still_tears_down_when_the_protocol_raises(mission_context, loop):
    """A protocol that throws must not leave its timers running forever."""

    class Exploding(SelfRescheduling):
        def finish(self):
            raise RuntimeError("boom")

    encapsulator = _run(mission_context, loop, Exploding)
    loop.run_until_complete(asyncio.sleep(0.05))

    with pytest.raises(RuntimeError):
        encapsulator.finish()

    assert encapsulator.provider._timers == {}
    assert encapsulator.provider._active is False


def test_provider_shutdown_leaves_the_shared_session_alone(mission_context, loop):
    """close() is process-scoped: the session is shared with telemetry and uav_api."""

    class FakeSession:
        closed = False

        async def close(self):
            self.closed = True

    session = FakeSession()
    encapsulator = EmbeddedEncapsulator(mission_context, loop, session, backend=None)
    encapsulator.encapsulate(SelfRescheduling)
    encapsulator.initialize()

    encapsulator.finish()

    assert session.closed is False
