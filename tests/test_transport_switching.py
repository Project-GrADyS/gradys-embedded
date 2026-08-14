"""The transport is a mission parameter, and only the chosen one is served.

The first group binds real ports against a real EmbeddedRunner — that is the
point of the change, and a stub would not prove the listener was ever released.
The second group covers the rejections that keep a bad transport from being
accepted and then failing inside a serve task nobody is awaiting.
"""

import asyncio
import socket

import pytest

from gradys_embedded.runner.configuration import RunnerConfiguration
from gradys_embedded.runner.mission import MissionError
from gradys_embedded.runner.runner import EmbeddedRunner


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def is_listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
def bound_runner(tmp_path, loop):
    """A real runner whose data plane can actually bind."""
    configuration = RunnerConfiguration(
        node_id=1,
        uav_api_port=8000,
        control_api_port=free_port(),
        data_port=free_port(),
        node_ip_dict={1: "127.0.0.1:5000"},
        runs_dir=str(tmp_path / "runs"),
        protocols_dir=str(tmp_path / "protocols"),
        min_free_disk_mb=0,
    )
    runner = EmbeddedRunner(configuration)
    runner._loop = loop
    yield runner
    loop.run_until_complete(runner.stop_backend())


def _settle(loop, seconds=0.35):
    loop.run_until_complete(asyncio.sleep(seconds))


# ------------------------------------------------------------- real binding

def test_nothing_is_served_until_a_mission_is_loaded(bound_runner):
    """A drone with no mission listens on nothing but its control port."""
    assert bound_runner._backend is None
    assert not is_listening(bound_runner._configuration.data_port)


def test_start_backend_binds_the_data_port(bound_runner, loop):
    loop.run_until_complete(bound_runner.start_backend(bound_runner._configuration))
    _settle(loop)

    assert is_listening(bound_runner._configuration.data_port)
    assert bound_runner._backend is not None


def test_stop_backend_releases_the_port(bound_runner, loop):
    port = bound_runner._configuration.data_port
    loop.run_until_complete(bound_runner.start_backend(bound_runner._configuration))
    _settle(loop)
    assert is_listening(port)

    loop.run_until_complete(bound_runner.stop_backend())
    _settle(loop)

    assert not is_listening(port), "the listener outlived stop_backend"
    assert bound_runner._backend is None


def test_switching_transport_rebinds_the_same_port(bound_runner, loop):
    """The whole point: a different transport, same port, same process."""
    from dataclasses import replace

    port = bound_runner._configuration.data_port
    http_config = bound_runner._configuration

    loop.run_until_complete(bound_runner.start_backend(http_config))
    _settle(loop)
    first = bound_runner._backend
    assert first._protocol == "http"

    https_config = replace(http_config, communication_protocol="https")
    loop.run_until_complete(bound_runner.start_backend(https_config))
    _settle(loop, 0.6)

    assert bound_runner._backend is not first, "backend was not rebuilt"
    assert bound_runner._backend._protocol == "https"
    assert is_listening(port), "the new transport did not take the port"


def test_same_transport_is_reused_without_rebinding(bound_runner, loop):
    """Consecutive missions on one transport must not churn the listener."""
    loop.run_until_complete(bound_runner.start_backend(bound_runner._configuration))
    _settle(loop)
    first = bound_runner._backend

    loop.run_until_complete(bound_runner.start_backend(bound_runner._configuration))

    assert bound_runner._backend is first, "an unchanged transport rebound the port"


def test_stop_backend_is_safe_when_nothing_is_running(bound_runner, loop):
    loop.run_until_complete(bound_runner.stop_backend())
    loop.run_until_complete(bound_runner.stop_backend())


def test_backend_is_built_from_the_mission_configuration(bound_runner, loop):
    """Regression: the backend used to capture the PROVISIONED config, so a
    mission-supplied peer map never reached the send path."""
    from dataclasses import replace

    mission_peers = {1: "127.0.0.1:5000", 9: "10.9.9.9:5000"}
    mission_config = replace(bound_runner._configuration, node_ip_dict=mission_peers)

    loop.run_until_complete(bound_runner.start_backend(mission_config))
    _settle(loop)

    assert bound_runner._backend._configuration.node_ip_dict == mission_peers
    assert bound_runner._configuration.node_ip_dict != mission_peers


# ---------------------------------------------------------- load rejections

def test_unknown_transport_is_rejected(runner, loop, demo_protocol):
    with pytest.raises(MissionError) as excinfo:
        loop.run_until_complete(runner.mission.load(
            protocol=demo_protocol, communication_protocol="carrier-pigeon"))

    assert excinfo.value.status_code == 400
    assert "carrier-pigeon" in str(excinfo.value)


def test_transport_needing_a_missing_extra_is_rejected(runner, loop, demo_protocol, monkeypatch):
    """Caught at load, naming the extra — not as an ImportError inside a serve
    task that nobody is awaiting."""
    import gradys_embedded.runner.mission as mission_module

    monkeypatch.setattr(mission_module, "missing_extra",
                        lambda protocol: "gradys-embedded[zenoh]" if protocol == "zenoh_tcp" else None)

    with pytest.raises(MissionError) as excinfo:
        loop.run_until_complete(runner.mission.load(
            protocol=demo_protocol, communication_protocol="zenoh_tcp"))

    assert excinfo.value.status_code == 400
    assert "gradys-embedded[zenoh]" in str(excinfo.value)


def test_zenoh_quic_without_a_fleet_cert_is_rejected(runner, loop, demo_protocol, monkeypatch):
    """QUIC verifies peers against a shared root CA. With a per-node ephemeral
    cert the fleet silently fails to mesh, so this must be a hard rejection
    rather than the warning it used to be."""
    import gradys_embedded.runner.mission as mission_module
    monkeypatch.setattr(mission_module, "missing_extra", lambda protocol: None)

    assert runner._configuration.certfile is None

    with pytest.raises(MissionError) as excinfo:
        loop.run_until_complete(runner.mission.load(
            protocol=demo_protocol, communication_protocol="zenoh_quic"))

    assert excinfo.value.status_code == 400
    assert "fleet-wide" in str(excinfo.value)


def test_zenoh_quic_is_allowed_with_a_provisioned_cert(runner, loop, demo_protocol, monkeypatch, tmp_path):
    import gradys_embedded.runner.mission as mission_module
    monkeypatch.setattr(mission_module, "missing_extra", lambda protocol: None)

    runner._configuration.certfile = str(tmp_path / "fleet-cert.pem")
    runner._configuration.keyfile = str(tmp_path / "fleet-key.pem")

    status = loop.run_until_complete(runner.mission.load(
        protocol=demo_protocol, communication_protocol="zenoh_quic",
        initial_position=[0.0, 0.0, 10.0]))

    assert status["frame"]["communication_protocol"] == "zenoh_quic"


def test_a_rejected_transport_leaves_no_orphan_run(runner, loop, demo_protocol):
    """Validation runs before the run directory is created."""
    with pytest.raises(MissionError):
        loop.run_until_complete(runner.mission.load(
            protocol=demo_protocol, communication_protocol="nope"))

    assert runner.mission.list_runs()["runs"] == []


# ------------------------------------------------------------- the contract

def test_transport_defaults_to_the_provisioned_value(runner, loop, demo_protocol):
    """Keeps gradys-sitl-tester, the examples and /protocol/setup working."""
    status = loop.run_until_complete(runner.mission.load(protocol=demo_protocol))

    assert status["frame"]["communication_protocol"] == "http"
    assert runner.served_protocol == "http"


def test_status_echoes_the_transport_for_fleet_verification(runner, loop, demo_protocol):
    status = loop.run_until_complete(runner.mission.load(
        protocol=demo_protocol, communication_protocol="https",
        initial_position=[0.0, 0.0, 10.0]))

    assert status["frame"]["communication_protocol"] == "https"
    assert runner.served_protocol == "https"


def test_loading_a_different_transport_restarts_the_data_plane(runner, loop, demo_protocol):
    loop.run_until_complete(runner.mission.load(protocol=demo_protocol))
    loop.run_until_complete(runner.mission.stop())
    assert runner.backend_starts == 1

    loop.run_until_complete(runner.mission.load(
        protocol=demo_protocol, communication_protocol="https"))

    assert runner.backend_starts == 2
    assert runner.backend_stops == 1
    assert runner.served_protocol == "https"


def test_reloading_the_same_transport_does_not_restart_it(runner, loop, demo_protocol):
    loop.run_until_complete(runner.mission.load(protocol=demo_protocol))
    loop.run_until_complete(runner.mission.stop())
    loop.run_until_complete(runner.mission.load(protocol=demo_protocol))

    assert runner.backend_starts == 1
    assert runner.backend_reuses == 1
    assert runner.backend_stops == 0
