"""Mission state machine: ordering, protocol swapping, RTL on stop, run data."""

import asyncio
import json

import pytest

from gradys_embedded.runner.mission import MissionError, MissionState

from tests.conftest import MISSION_KWARGS

RUN_INFO = "RUN_INFO.json"


def test_boots_idle(runner):
    """A drone that reboots in the field must not spontaneously arm."""
    status = runner.mission.status()
    assert status["state"] == "idle"
    assert status["run_id"] is None
    assert status["protocol"] is None


def test_control_surface_has_no_legacy_endpoints(runner):
    """The mission API is the only gateway; /protocol/* is gone, not aliased."""
    from gradys_embedded.runner.control_panel import create_control_app

    paths = create_control_app(runner).openapi()["paths"]

    assert "/protocol/setup" not in paths
    assert "/protocol/start" not in paths
    for path in ["/mission/load", "/mission/setup", "/mission/start",
                 "/mission/stop", "/mission/status",
                 "/protocols", "/protocols/upload",
                 "/runs", "/runs/{run_id}", "/runs/{run_id}/archive",
                 "/runs/{run_id}/files/{filename}"]:
        assert path in paths, path


def test_setup_and_start_require_a_loaded_mission(runner, loop):
    with pytest.raises(MissionError) as excinfo:
        loop.run_until_complete(runner.mission.setup())
    assert excinfo.value.status_code == 409

    with pytest.raises(MissionError):
        loop.run_until_complete(runner.mission.start())


def test_start_requires_setup(runner, loop, demo_protocol):
    loop.run_until_complete(runner.mission.load(protocol=demo_protocol, **MISSION_KWARGS))
    with pytest.raises(MissionError):
        loop.run_until_complete(runner.mission.start())


def test_load_creates_a_run_directory(runner, loop, demo_protocol):
    status = loop.run_until_complete(runner.mission.load(protocol=demo_protocol, **MISSION_KWARGS, label="alpha"))

    assert status["state"] == "loaded"
    assert status["run_id"].endswith("_alpha")

    run_dir = runner.mission.run_dir(status["run_id"])
    assert (run_dir / RUN_INFO).is_file()
    assert json.loads((run_dir / RUN_INFO).read_text())["protocol"] == demo_protocol


def test_load_rejected_while_a_mission_is_active(runner, loop, demo_protocol):
    loop.run_until_complete(runner.mission.load(protocol=demo_protocol, **MISSION_KWARGS))
    with pytest.raises(MissionError) as excinfo:
        loop.run_until_complete(runner.mission.load(protocol=demo_protocol, **MISSION_KWARGS))
    assert excinfo.value.status_code == 409


def test_protocol_resolves_from_a_bare_module_name(runner, loop, demo_protocol):
    """An uploaded file usually holds exactly one protocol; naming it is optional."""
    status = loop.run_until_complete(runner.mission.load(protocol="demo", **MISSION_KWARGS))
    assert status["state"] == "loaded"


def test_setup_failure_is_retryable(runner, loop, demo_protocol):
    loop.run_until_complete(runner.mission.load(protocol=demo_protocol, **MISSION_KWARGS))
    runner.setup_succeeds = False

    with pytest.raises(MissionError) as excinfo:
        loop.run_until_complete(runner.mission.setup())
    assert excinfo.value.status_code == 500
    assert runner.mission.state is MissionState.LOADED

    runner.setup_succeeds = True
    assert loop.run_until_complete(runner.mission.setup())["state"] == "ready"


def _fly(runner, loop, protocol, label=None):
    loop.run_until_complete(runner.mission.load(protocol=protocol, **MISSION_KWARGS, label=label))
    loop.run_until_complete(runner.mission.setup())
    loop.run_until_complete(runner.mission.start())


def test_full_lifecycle_and_rtl_on_stop(runner, loop, demo_protocol):
    _fly(runner, loop, demo_protocol)
    assert runner.mission.state is MissionState.RUNNING
    loop.run_until_complete(asyncio.sleep(0.1))
    assert runner.mission.status()["tracked_variables"]["ticks"] >= 2

    loop.run_until_complete(runner.mission.stop())

    assert runner.mission.state is MissionState.RETURNING
    assert runner.commands == ["goto_initial_position", "rtl"]

    runner.mission.note_landed()
    assert runner.mission.state is MissionState.IDLE


def test_stop_halts_the_protocol_before_returning(runner, loop, demo_protocol):
    """A protocol left ticking would fight the RTL, or command a disarmed vehicle."""
    _fly(runner, loop, demo_protocol)
    loop.run_until_complete(asyncio.sleep(0.08))

    loop.run_until_complete(runner.mission.stop())
    ticks_at_stop = runner.mission.status()["tracked_variables"]

    loop.run_until_complete(asyncio.sleep(0.1))
    assert runner.mission.status()["tracked_variables"] == ticks_at_stop


def test_stop_without_flying_does_not_command_rtl(runner, loop, demo_protocol):
    loop.run_until_complete(runner.mission.load(protocol=demo_protocol, **MISSION_KWARGS))
    loop.run_until_complete(runner.mission.stop())

    assert runner.mission.state is MissionState.IDLE
    assert "rtl" not in runner.commands


def test_missions_swap_without_restart_and_stay_isolated(runner, loop, demo_protocol):
    """The point of the refactor: a second mission in the same process."""
    _fly(runner, loop, demo_protocol, label="alpha")
    loop.run_until_complete(asyncio.sleep(0.1))
    first_run = runner.mission.run_id
    first_ticks = runner.mission.status()["tracked_variables"]["ticks"]
    loop.run_until_complete(runner.mission.stop())
    runner.mission.note_landed()

    _fly(runner, loop, demo_protocol, label="bravo")
    loop.run_until_complete(asyncio.sleep(0.1))
    second_run = runner.mission.run_id

    assert second_run != first_run
    # Fresh state, not a continuation of the first mission's counter.
    assert runner.mission.status()["tracked_variables"]["ticks"] <= first_ticks + 1

    loop.run_until_complete(runner.mission.stop())
    runner.mission.note_landed()

    run_ids = [r["run_id"] for r in runner.mission.list_runs()["runs"]]
    assert sorted(run_ids) == sorted([first_run, second_run])


def test_run_info_records_the_outcome(runner, loop, demo_protocol):
    _fly(runner, loop, demo_protocol)
    run_id = runner.mission.run_id
    loop.run_until_complete(runner.mission.stop())

    info_path = runner.mission.run_dir(run_id) / RUN_INFO
    assert json.loads(info_path.read_text())["outcome"] == "returning"

    runner.mission.note_landed()
    info = json.loads(info_path.read_text())
    assert info["outcome"] == "completed"
    assert info["node_id"] == 3


def test_interrupted_run_is_not_reported_as_successful(runner, loop, demo_protocol):
    """A process killed mid-flight must not leave a run that looks complete."""
    _fly(runner, loop, demo_protocol)
    run_id = runner.mission.run_id

    runner.mission.shutdown()

    info = json.loads((runner.mission.run_dir(run_id) / RUN_INFO).read_text())
    assert info["outcome"] == "interrupted"


def test_resource_usage_is_captured_per_run(runner, loop, demo_protocol):
    # performance_monitor is an optional dependency: a mission runs without it,
    # just with no resource_usage.csv. Provisioning installs it into the drone's
    # venv (pip install -e ../performance_monitor).
    pytest.importorskip("resource_monitor")

    _fly(runner, loop, demo_protocol)
    run_id = runner.mission.run_id
    loop.run_until_complete(asyncio.sleep(1.2))
    loop.run_until_complete(runner.mission.stop())

    csv = runner.mission.run_dir(run_id) / "resource_usage.csv"
    assert csv.is_file()
    assert len(csv.read_text().strip().splitlines()) >= 2  # header + a sample


def test_disk_threshold_blocks_a_new_run(runner, loop, demo_protocol):
    """Nothing is auto-pruned, so a full card must fail loudly before takeoff."""
    runner._configuration.min_free_disk_mb = 10 ** 9

    with pytest.raises(MissionError) as excinfo:
        loop.run_until_complete(runner.mission.load(protocol=demo_protocol, **MISSION_KWARGS))
    assert excinfo.value.status_code == 507
