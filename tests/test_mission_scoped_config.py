"""The provisioned / mission-supplied split.

gradys-fleet provisions what is bound to a machine and its network; the mission
layer supplies what describes an experiment — the coordinate frame, the initial
position and the peer map. These tests pin that boundary, and the failure modes
it introduces.
"""

import asyncio

import pytest

from gradys_embedded.runner.configuration import RunnerConfiguration
from gradys_embedded.runner.mission import MissionError

FRAME = [-15.840081, -47.926642, 0.016]
OTHER_FRAME = [-22.906847, -43.172896, 5.0]


def _load(loop, runner, **kwargs):
    return loop.run_until_complete(runner.mission.load(**kwargs))


# --------------------------------------------------- provisioned bind port

def test_data_port_is_provisioned():
    config = RunnerConfiguration(
        node_id=1, uav_api_port=8000, control_api_port=6000, data_port=5000
    )
    assert config.resolve_data_port() == 5000


def test_data_port_falls_back_to_the_peer_map():
    """Configs written before the split expressed the bind port this way."""
    config = RunnerConfiguration(
        node_id=2, uav_api_port=8000, control_api_port=6000,
        node_ip_dict={1: "10.0.2.11:5000", 2: "10.0.2.12:5001"},
    )
    assert config.resolve_data_port() == 5001


def test_data_port_prefers_the_provisioned_value():
    config = RunnerConfiguration(
        node_id=2, uav_api_port=8000, control_api_port=6000, data_port=6100,
        node_ip_dict={2: "10.0.2.12:5001"},
    )
    assert config.resolve_data_port() == 6100


def test_missing_bind_port_fails_clearly():
    config = RunnerConfiguration(node_id=9, uav_api_port=8000, control_api_port=6000)
    with pytest.raises(ValueError) as excinfo:
        config.resolve_data_port()
    assert "data_port" in str(excinfo.value)


# ------------------------------------------------ frame is mission-supplied

def test_config_is_valid_without_a_frame(configuration):
    """The fleet layer no longer renders these, so a config must load without them."""
    bare = RunnerConfiguration(
        node_id=1, uav_api_port=8000, control_api_port=6000, data_port=5000,
        runs_dir=configuration.runs_dir, protocols_dir=configuration.protocols_dir,
    )
    assert bare.origin_gps_coordinates is None
    assert bare.x_axis_degrees is None
    assert bare.initial_position is None
    assert bare.node_ip_dict is None


def test_mission_supplies_the_frame(runner, loop, demo_protocol):
    status = _load(loop, runner, protocol=demo_protocol,
                   origin_gps_coordinates=FRAME, x_axis_degrees=90.0,
                   initial_position=[5.0, 6.0, 7.0])

    frame = status["frame"]
    assert frame["origin_gps_coordinates"] == FRAME
    assert frame["x_axis_degrees"] == 90.0
    assert frame["initial_position"] == [5.0, 6.0, 7.0]


def test_status_echoes_the_frame_for_fleet_verification(runner, loop, demo_protocol):
    """This replaces the structural guarantee the fleet inventory used to give:
    the mission layer compares these across drones before starting anything."""
    _load(loop, runner, protocol=demo_protocol, origin_gps_coordinates=FRAME,
          x_axis_degrees=0.0, initial_position=[0.0, 0.0, 10.0])

    frame = runner.mission.status()["frame"]
    assert set(frame) == {
        "origin_gps_coordinates", "x_axis_degrees", "initial_position",
        "node_ip_dict", "communication_protocol", "auto_scout",
    }
    assert frame["communication_protocol"] == "http"


def test_a_divergent_frame_is_visible_in_status(runner, loop, demo_protocol):
    """Two drones given different origins must be distinguishable before flight."""
    first = _load(loop, runner, protocol=demo_protocol, origin_gps_coordinates=FRAME,
                  initial_position=[0.0, 0.0, 10.0])["frame"]
    loop.run_until_complete(runner.mission.stop())

    second = _load(loop, runner, protocol=demo_protocol,
                   origin_gps_coordinates=OTHER_FRAME,
                   initial_position=[0.0, 0.0, 10.0])["frame"]

    assert first["origin_gps_coordinates"] != second["origin_gps_coordinates"]


def test_mission_frame_does_not_leak_into_the_provisioned_config(provisioned_runner, loop):
    """A mission overlay must not mutate what was provisioned, or the next
    mission would silently inherit the previous one's frame."""
    _load(loop, provisioned_runner, protocol="bare_demo",
          origin_gps_coordinates=FRAME, initial_position=[1.0, 2.0, 3.0])

    assert provisioned_runner._configuration.origin_gps_coordinates is None
    assert provisioned_runner._configuration.initial_position is None


def test_omitted_frame_falls_back_to_what_was_provisioned(runner, loop, demo_protocol):
    """Transitional: a drone still provisioned the old way keeps working."""
    runner._configuration.origin_gps_coordinates = tuple(FRAME)
    runner._configuration.x_axis_degrees = 0.0
    runner._configuration.initial_position = (1.0, 1.0, 1.0)

    frame = _load(loop, runner, protocol=demo_protocol)["frame"]

    assert frame["origin_gps_coordinates"] == FRAME
    assert frame["initial_position"] == [1.0, 1.0, 1.0]


def test_setup_without_an_initial_position_is_rejected(provisioned_runner, loop):
    """It is no longer provisioned, so a mission that omits it has nowhere to fly."""
    _load(loop, provisioned_runner, protocol="bare_demo")

    with pytest.raises(MissionError) as excinfo:
        loop.run_until_complete(provisioned_runner.mission.setup())

    assert excinfo.value.status_code == 400
    assert "initial_position" in str(excinfo.value)
    assert provisioned_runner.commands == []


# ------------------------------------------------------- peer map at runtime

def test_mission_supplies_the_peer_map(runner, loop, demo_protocol):
    peers = {1: "10.0.2.11:5000", 2: "10.0.2.12:5000", 4: "10.0.2.14:5000"}
    status = _load(loop, runner, protocol=demo_protocol, node_ip_dict=peers,
                   initial_position=[0.0, 0.0, 10.0])

    # A node absent from the provisioned config is reachable for this mission --
    # which is what lets a fleet gain a drone without re-provisioning the rest.
    assert status["frame"]["node_ip_dict"] == {1: "10.0.2.11:5000",
                                               2: "10.0.2.12:5000",
                                               4: "10.0.2.14:5000"}


def test_peer_map_rejects_a_scheme(runner, loop, demo_protocol):
    """The transport prefixes http:// itself; a scheme here fails silently."""
    with pytest.raises(MissionError) as excinfo:
        _load(loop, runner, protocol=demo_protocol,
              node_ip_dict={1: "http://10.0.2.11:5000"})

    assert excinfo.value.status_code == 400
    assert "scheme" in str(excinfo.value)


def test_zenoh_accepts_a_changed_peer_map(runner, loop, demo_protocol):
    """Zenoh used to reject this, because a long-lived session fixes its connect
    endpoints when it opens. The session is now opened per mission, so the map a
    mission supplies is the one it is built from — the restriction is gone."""
    runner._configuration.communication_protocol = "zenoh_tcp"
    runner._configuration.node_ip_dict = {1: "10.0.2.11:5000"}

    status = _load(loop, runner, protocol=demo_protocol,
                   node_ip_dict={1: "10.0.2.11:5000", 2: "10.0.2.12:5000"},
                   initial_position=[0.0, 0.0, 10.0])

    assert status["state"] == "loaded"
    assert status["frame"]["node_ip_dict"] == {1: "10.0.2.11:5000", 2: "10.0.2.12:5000"}


# ------------------------------------------- telemetry follows the mission frame

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Serves one fixed /telemetry/gps reading."""

    def __init__(self, payload):
        self._payload = payload

    def get(self, url, **kwargs):
        return _FakeResponse(self._payload)


class _RecordingEncapsulator:
    def __init__(self):
        self.telemetry = []

    def handle_telemetry(self, telemetry):
        self.telemetry.append(telemetry)


def test_telemetry_uses_the_mission_frame_not_the_boot_frame(tmp_path, loop):
    """Regression.

    The telemetry loop used to read the *process* configuration for the origin.
    With a mission-supplied frame, mobility commands would be converted in the
    mission frame while telemetry was converted in the boot frame — the protocol
    would believe it was somewhere other than where it was being sent, with no
    error raised and nothing visible until the flight data was analysed.
    """
    from gradys_embedded.protocol.position import geo_to_cartesian
    from gradys_embedded.runner.mission import MissionManager
    from gradys_embedded.runner.runner import EmbeddedRunner
    from tests.conftest import DEMO_PROTOCOL_SRC

    boot_frame = (-15.840081, -47.926642, 0.0)
    mission_frame = (-22.906847, -43.172896, 0.0)
    reading = {"lat": -15.840000, "lon": -47.926000, "relative_alt": 12.0}

    configuration = RunnerConfiguration(
        node_id=3, uav_api_port=8000, control_api_port=6000, data_port=5000,
        origin_gps_coordinates=boot_frame, x_axis_degrees=0.0,
        telemetry_interval=0.01,
        runs_dir=str(tmp_path / "runs"), protocols_dir=str(tmp_path / "protocols"),
        min_free_disk_mb=0,
    )

    runner = EmbeddedRunner(configuration)
    runner._loop = loop
    runner._session = _FakeSession({"info": {"position": reading, "heading": 0.0}})
    runner.mission = MissionManager(runner)
    runner.mission.save_protocol("frame_demo.py", DEMO_PROTOCOL_SRC.encode())

    loop.run_until_complete(runner.mission.load(
        protocol="frame_demo",
        origin_gps_coordinates=list(mission_frame),
        x_axis_degrees=0.0,
        initial_position=[0.0, 0.0, 10.0],
    ))

    recorder = _RecordingEncapsulator()
    runner._encapsulator = recorder

    task = loop.create_task(runner._periodic_telemetry())
    loop.run_until_complete(asyncio.sleep(0.05))
    task.cancel()

    assert recorder.telemetry, "no telemetry was delivered"
    got = recorder.telemetry[0].current_position

    geo = (reading["lat"], reading["lon"], reading["relative_alt"])
    expected = geo_to_cartesian(mission_frame, geo, 0.0)
    wrong = geo_to_cartesian(boot_frame, geo, 0.0)

    assert got == pytest.approx(expected), "telemetry did not use the mission frame"
    assert got != pytest.approx(wrong), "test is not discriminating between frames"
