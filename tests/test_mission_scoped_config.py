"""The provisioned / mission-supplied split.

gradys-fleet provisions what is bound to a machine and its network; the mission
layer supplies what describes an experiment — the coordinate frame, the initial
position, the peer map, the transport and the poll rate. The split is now
structural: `RunnerConfiguration` cannot even carry a mission parameter, and
POST /mission/load is the only gateway. These tests pin that boundary, and the
failure modes it introduces.
"""

import asyncio

import pytest

from gradys_embedded.runner.configuration import RunnerConfiguration
from gradys_embedded.runner.mission import MissionError

from tests.conftest import MISSION_KWARGS

FRAME = [-15.840081, -47.926642, 0.016]
OTHER_FRAME = [-22.906847, -43.172896, 5.0]
PEERS = {3: "127.0.0.1:5000"}


def _load(loop, runner, **kwargs):
    return loop.run_until_complete(runner.mission.load(**kwargs))


# --------------------------------------------------- provisioned bind port

def test_data_port_is_provisioned():
    config = RunnerConfiguration(
        node_id=1, uav_api_port=8000, control_api_port=6000, data_port=5000
    )
    assert config.data_port == 5000


def test_missing_bind_port_fails_clearly():
    """data_port is required: the peer map is mission-supplied, so nothing else
    can provide the bind port."""
    with pytest.raises(TypeError) as excinfo:
        RunnerConfiguration(node_id=9, uav_api_port=8000, control_api_port=6000)
    assert "data_port" in str(excinfo.value)


# ------------------------------------------------ the split is structural

def test_provisioned_config_cannot_carry_mission_parameters(configuration):
    """The boundary is enforced by shape, not by convention: a mission
    parameter has nowhere to live on the provisioned config, so it cannot leak
    between missions or be half-set by provisioning."""
    for field in ["node_ip_dict", "initial_position", "origin_gps_coordinates",
                  "x_axis_degrees", "communication_protocol", "auto_scout",
                  "telemetry_interval"]:
        assert not hasattr(configuration, field), field

    with pytest.raises(TypeError):
        RunnerConfiguration(
            node_id=1, uav_api_port=8000, control_api_port=6000, data_port=5000,
            node_ip_dict={1: "10.0.2.11:5000"},
        )


def test_mission_parameters_do_not_outlive_the_mission(runner, loop, demo_protocol):
    """After a mission ends, its context is gone -- the next mission starts from
    nothing rather than silently inheriting the previous frame or peer map."""
    _load(loop, runner, protocol=demo_protocol, **MISSION_KWARGS)
    loop.run_until_complete(runner.mission.stop())

    assert runner.mission.context is None
    assert runner.mission.status()["frame"] is None


# ------------------------------------------------ frame is mission-supplied

def test_mission_supplies_the_frame(runner, loop, demo_protocol):
    status = _load(loop, runner, protocol=demo_protocol, node_ip_dict=PEERS,
                   origin_gps_coordinates=FRAME, x_axis_degrees=90.0,
                   initial_position=[5.0, 6.0, 7.0])

    frame = status["frame"]
    assert frame["origin_gps_coordinates"] == FRAME
    assert frame["x_axis_degrees"] == 90.0
    assert frame["initial_position"] == [5.0, 6.0, 7.0]


def test_status_echoes_the_frame_for_fleet_verification(runner, loop, demo_protocol):
    """This replaces the structural guarantee the fleet inventory used to give:
    the mission layer compares these across drones before starting anything."""
    _load(loop, runner, protocol=demo_protocol, node_ip_dict=PEERS,
          origin_gps_coordinates=FRAME, x_axis_degrees=0.0,
          initial_position=[0.0, 0.0, 10.0])

    frame = runner.mission.status()["frame"]
    assert set(frame) == {
        "origin_gps_coordinates", "x_axis_degrees", "initial_position",
        "node_ip_dict", "communication_protocol", "auto_scout",
    }
    assert frame["communication_protocol"] == "http"


def test_a_divergent_frame_is_visible_in_status(runner, loop, demo_protocol):
    """Two drones given different origins must be distinguishable before flight."""
    first = _load(loop, runner, protocol=demo_protocol, node_ip_dict=PEERS,
                  origin_gps_coordinates=FRAME,
                  initial_position=[0.0, 0.0, 10.0])["frame"]
    loop.run_until_complete(runner.mission.stop())

    second = _load(loop, runner, protocol=demo_protocol, node_ip_dict=PEERS,
                   origin_gps_coordinates=OTHER_FRAME,
                   initial_position=[0.0, 0.0, 10.0])["frame"]

    assert first["origin_gps_coordinates"] != second["origin_gps_coordinates"]


def test_setup_without_an_initial_position_is_rejected(runner, loop, demo_protocol):
    """It cannot be provisioned, so a mission that omits it has nowhere to fly."""
    _load(loop, runner, protocol=demo_protocol, node_ip_dict=PEERS)

    with pytest.raises(MissionError) as excinfo:
        loop.run_until_complete(runner.mission.setup())

    assert excinfo.value.status_code == 400
    assert "initial_position" in str(excinfo.value)
    assert runner.commands == []


def test_mission_can_set_the_telemetry_interval(runner, loop, demo_protocol):
    _load(loop, runner, protocol=demo_protocol, telemetry_interval=0.1,
          **MISSION_KWARGS)
    assert runner.mission.context.telemetry_interval == 0.1


def test_a_nonpositive_telemetry_interval_is_rejected(runner, loop, demo_protocol):
    """0 would spin the poll loop; a negative value is nonsense. Both 400."""
    with pytest.raises(MissionError) as excinfo:
        _load(loop, runner, protocol=demo_protocol, telemetry_interval=0,
              **MISSION_KWARGS)
    assert excinfo.value.status_code == 400
    assert "telemetry_interval" in str(excinfo.value)


# ------------------------------------------------------- peer map at runtime

def test_load_requires_a_peer_map(runner, loop, demo_protocol):
    """Without one, sends have nowhere to go -- and the old behavior was a
    crash on the first send, mid-flight, instead of a 400 at load."""
    with pytest.raises(MissionError) as excinfo:
        _load(loop, runner, protocol=demo_protocol,
              initial_position=[0.0, 0.0, 10.0])

    assert excinfo.value.status_code == 400
    assert "node_ip_dict" in str(excinfo.value)
    assert runner.mission.status()["state"] == "idle"
    assert runner.mission.list_runs()["runs"] == []


def test_mission_supplies_the_peer_map(runner, loop, demo_protocol):
    peers = {1: "10.0.2.11:5000", 2: "10.0.2.12:5000", 4: "10.0.2.14:5000"}
    status = _load(loop, runner, protocol=demo_protocol, node_ip_dict=peers,
                   initial_position=[0.0, 0.0, 10.0])

    # A node the drone has never been provisioned for is reachable this mission
    # -- which is what lets a fleet gain a drone without re-provisioning the rest.
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


def test_auto_scout_zenoh_needs_no_peer_map(runner, loop, demo_protocol, monkeypatch):
    """Zenoh multicast scouting discovers peers, so the one transport mode with
    its own discovery is exempt from the peer-map requirement."""
    monkeypatch.setattr("gradys_embedded.runner.mission.missing_extra", lambda p: None)

    status = _load(loop, runner, protocol=demo_protocol,
                   communication_protocol="zenoh_tcp", auto_scout=True,
                   initial_position=[0.0, 0.0, 10.0])

    assert status["state"] == "loaded"
    assert status["frame"]["node_ip_dict"] is None


def test_zenoh_without_auto_scout_needs_its_own_entry(runner, loop, demo_protocol,
                                                      monkeypatch):
    """Zenoh builds its listen endpoint from the node's own map entry."""
    monkeypatch.setattr("gradys_embedded.runner.mission.missing_extra", lambda p: None)

    with pytest.raises(MissionError) as excinfo:
        _load(loop, runner, protocol=demo_protocol,
              communication_protocol="zenoh_tcp",
              node_ip_dict={1: "10.0.2.11:5000", 2: "10.0.2.12:5000"},
              initial_position=[0.0, 0.0, 10.0])

    assert excinfo.value.status_code == 400
    assert "own entry" in str(excinfo.value)


def test_zenoh_accepts_a_changed_peer_map(runner, loop, demo_protocol, monkeypatch):
    """Zenoh used to reject a per-mission map, because a long-lived session fixes
    its connect endpoints when it opens. The session is now opened per mission, so
    the map a mission supplies is the one it is built from."""
    monkeypatch.setattr("gradys_embedded.runner.mission.missing_extra", lambda p: None)

    status = _load(loop, runner, protocol=demo_protocol,
                   communication_protocol="zenoh_tcp",
                   node_ip_dict={1: "10.0.2.11:5000", 2: "10.0.2.12:5000",
                                 3: "10.0.2.13:5000"},
                   initial_position=[0.0, 0.0, 10.0])

    assert status["state"] == "loaded"
    assert status["frame"]["node_ip_dict"] == {1: "10.0.2.11:5000",
                                               2: "10.0.2.12:5000",
                                               3: "10.0.2.13:5000"}


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


def test_telemetry_uses_the_mission_frame(tmp_path, loop):
    """Regression.

    The telemetry loop used to read the *process* configuration for the origin.
    With a mission-supplied frame, mobility commands would be converted in the
    mission frame while telemetry was converted in another — the protocol
    would believe it was somewhere other than where it was being sent, with no
    error raised and nothing visible until the flight data was analysed.
    """
    from gradys_embedded.protocol.position import geo_to_cartesian
    from gradys_embedded.runner.mission import MissionManager
    from gradys_embedded.runner.runner import EmbeddedRunner
    from tests.conftest import DEMO_PROTOCOL_SRC

    other_frame = (-15.840081, -47.926642, 0.0)
    mission_frame = (-22.906847, -43.172896, 0.0)
    reading = {"lat": -15.840000, "lon": -47.926000, "relative_alt": 12.0}

    configuration = RunnerConfiguration(
        node_id=3, uav_api_port=8000, control_api_port=6000, data_port=5000,
        runs_dir=str(tmp_path / "runs"), protocols_dir=str(tmp_path / "protocols"),
        min_free_disk_mb=0,
    )

    runner = EmbeddedRunner(configuration)
    runner._loop = loop
    runner._session = _FakeSession({"info": {"position": reading, "heading": 0.0}})
    runner.mission = MissionManager(runner)
    runner.mission.save_protocol("frame_demo.py", DEMO_PROTOCOL_SRC.encode())

    # The test is about the telemetry loop's frame, not about binding a port.
    async def _no_backend(configuration):
        return None

    runner.start_backend = _no_backend

    loop.run_until_complete(runner.mission.load(
        protocol="frame_demo",
        node_ip_dict={3: "127.0.0.1:5000"},
        origin_gps_coordinates=list(mission_frame),
        x_axis_degrees=0.0,
        initial_position=[0.0, 0.0, 10.0],
        telemetry_interval=0.01,
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
    wrong = geo_to_cartesian(other_frame, geo, 0.0)

    assert got == pytest.approx(expected), "telemetry did not use the mission frame"
    assert got != pytest.approx(wrong), "test is not discriminating between frames"
