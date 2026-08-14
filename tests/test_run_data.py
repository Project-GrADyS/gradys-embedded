"""Run archive: listing, downloads, deletion, and the statistics plugin's output."""

import asyncio

import pytest

from gradys_embedded.encapsulator.embedded import EmbeddedEncapsulator
from gradys_embedded.protocol.interface import IProtocol
from gradys_embedded.protocol.plugin import dispatcher
from gradys_embedded.protocol.plugin.statistics import (
    _statistics_protocol_wrappers,
    create_statistics,
    finish_statistics,
)
from gradys_embedded.runner.mission import MissionError


def _rows(path):
    return len(path.read_text().strip().splitlines()) - 1  # minus the header


# --------------------------------------------------------------- run archive

def test_unknown_run_is_a_404(runner):
    with pytest.raises(MissionError) as excinfo:
        runner.mission.run_dir("run_does_not_exist")
    assert excinfo.value.status_code == 404


@pytest.mark.parametrize("name", ["../escape", "..", ".", "/etc/passwd", "../../etc/passwd"])
def test_run_ids_cannot_escape_the_runs_directory(runner, name):
    """Run ids arrive from the network."""
    with pytest.raises(MissionError) as excinfo:
        runner.mission.run_dir(name)
    assert excinfo.value.status_code in (400, 404)


def test_run_files_cannot_escape_the_run_directory(runner, loop, demo_protocol):
    status = loop.run_until_complete(runner.mission.load(protocol=demo_protocol))
    with pytest.raises(MissionError) as excinfo:
        runner.mission.run_file(status["run_id"], "../../../etc/passwd")
    assert excinfo.value.status_code in (400, 404)


def test_listing_reports_disk_usage(runner, loop, demo_protocol):
    loop.run_until_complete(runner.mission.load(protocol=demo_protocol))
    listing = runner.mission.list_runs()

    assert len(listing["runs"]) == 1
    assert listing["disk"]["free_mb"] > 0
    assert listing["disk"]["low"] is False


def test_delete_removes_a_run(runner, loop, demo_protocol):
    run_id = loop.run_until_complete(runner.mission.load(protocol=demo_protocol))["run_id"]
    loop.run_until_complete(runner.mission.stop())

    runner.mission.delete_run(run_id)

    assert runner.mission.list_runs()["runs"] == []
    with pytest.raises(MissionError):
        runner.mission.run_dir(run_id)


def test_refuses_to_delete_the_run_in_progress(runner, loop, demo_protocol):
    run_id = loop.run_until_complete(runner.mission.load(protocol=demo_protocol))["run_id"]
    loop.run_until_complete(runner.mission.setup())

    with pytest.raises(MissionError):
        runner.mission.delete_run(run_id)


# ---------------------------------------------------------------- protocols

def test_uploaded_protocol_is_listed_and_deletable(runner):
    runner.mission.save_protocol("thing.py", b"# empty\n")
    assert "thing" in [p["name"] for p in runner.mission.list_protocols()]

    runner.mission.delete_protocol("thing")
    assert "thing" not in [p["name"] for p in runner.mission.list_protocols()]


def test_only_python_files_are_accepted(runner):
    with pytest.raises(MissionError) as excinfo:
        runner.mission.save_protocol("payload.sh", b"rm -rf /")
    assert excinfo.value.status_code == 400


def test_upload_filename_cannot_escape_the_protocols_directory(runner):
    path = runner.mission.save_protocol("../../escape.py", b"# x\n")
    assert path.parent == runner.mission.protocols_dir


def test_reupload_replaces_the_cached_module(runner):
    """importlib caches by name, so a re-upload must not keep running old code."""
    runner.mission.save_protocol("swap.py", '''
from gradys_embedded.protocol.interface import IProtocol
class A(IProtocol):
    marker = "first"
    def initialize(self): pass
    def handle_timer(self, t): pass
    def handle_packet(self, m): pass
    def handle_telemetry(self, t): pass
    def finish(self): pass
'''.encode())
    assert runner.mission.resolve_protocol("swap:A").marker == "first"

    runner.mission.save_protocol("swap.py", '''
from gradys_embedded.protocol.interface import IProtocol
class A(IProtocol):
    marker = "second"
    def initialize(self): pass
    def handle_timer(self, t): pass
    def handle_packet(self, m): pass
    def handle_telemetry(self, t): pass
    def finish(self): pass
'''.encode())
    assert runner.mission.resolve_protocol("swap:A").marker == "second"


def test_ambiguous_module_requires_an_explicit_class(runner):
    runner.mission.save_protocol("two.py", '''
from gradys_embedded.protocol.interface import IProtocol
class _Base(IProtocol):
    def initialize(self): pass
    def handle_timer(self, t): pass
    def handle_packet(self, m): pass
    def handle_telemetry(self, t): pass
    def finish(self): pass
class A(_Base): pass
class B(_Base): pass
'''.encode())

    with pytest.raises(MissionError) as excinfo:
        runner.mission.resolve_protocol("two")
    assert "name one explicitly" in str(excinfo.value)

    assert runner.mission.resolve_protocol("two:A").__name__ == "A"


def test_non_protocol_is_rejected(runner):
    runner.mission.save_protocol("plain.py", b"class NotAProtocol: pass\n")
    with pytest.raises(MissionError):
        runner.mission.resolve_protocol("plain:NotAProtocol")


# --------------------------------------------------------------- statistics

class StatsProtocol(IProtocol):
    output_dir = None

    def initialize(self):
        self.provider.tracked_variables["messages_sent"] = 0
        create_statistics(self, file_name_part="t", collection_interval=0.05,
                          output_dir=str(self.output_dir), flush_every=3)
        self.provider.schedule_timer("work", self.provider.current_time() + 0.05)

    def handle_timer(self, timer):
        if timer == "work":
            self.provider.tracked_variables["messages_sent"] += 1
            self.provider.schedule_timer("work", self.provider.current_time() + 0.05)

    def handle_packet(self, message):
        pass

    def handle_telemetry(self, telemetry):
        pass

    def finish(self):
        finish_statistics(self)


def test_statistics_timer_keeps_rescheduling(configuration, loop, tmp_path):
    """Regression: the interval was read off the protocol instead of the wrapper,
    so the first statistics timer raised AttributeError and never rescheduled --
    every flight produced a simulation_real_time CSV with a single row."""
    StatsProtocol.output_dir = tmp_path

    encapsulator = EmbeddedEncapsulator(configuration, loop, None, backend=None)
    encapsulator.encapsulate(StatsProtocol)
    encapsulator.initialize()
    loop.run_until_complete(asyncio.sleep(0.7))
    encapsulator.finish()

    srt = tmp_path / "simulation_real_time_t_StatsProtocol_3.csv"
    assert _rows(srt) >= 5, f"statistics timer stopped rescheduling: {_rows(srt)} rows"


def test_statistics_write_into_the_run_directory(configuration, loop, tmp_path):
    StatsProtocol.output_dir = tmp_path

    encapsulator = EmbeddedEncapsulator(configuration, loop, None, backend=None)
    encapsulator.encapsulate(StatsProtocol)
    encapsulator.initialize()
    loop.run_until_complete(asyncio.sleep(0.3))
    encapsulator.finish()

    tracked = tmp_path / "tracked_variables_t_StatsProtocol_3.csv"
    assert tracked.is_file()
    assert "messages_sent" in tracked.read_text().splitlines()[0]


def test_statistics_flush_before_finish(configuration, loop, tmp_path):
    """A killed process must not lose the whole run."""
    StatsProtocol.output_dir = tmp_path

    encapsulator = EmbeddedEncapsulator(configuration, loop, None, backend=None)
    encapsulator.encapsulate(StatsProtocol)
    encapsulator.initialize()
    loop.run_until_complete(asyncio.sleep(0.5))

    # finish() has NOT been called yet
    assert (tmp_path / "simulation_real_time_t_StatsProtocol_3.csv").is_file()
    encapsulator.finish()


def test_statistics_registries_are_emptied(configuration, loop, tmp_path):
    """Both registries are module-global and keyed by protocol instance, so a
    long-running service would leak one wrapper per mission."""
    StatsProtocol.output_dir = tmp_path

    encapsulator = EmbeddedEncapsulator(configuration, loop, None, backend=None)
    encapsulator.encapsulate(StatsProtocol)
    encapsulator.initialize()
    loop.run_until_complete(asyncio.sleep(0.15))
    encapsulator.finish()

    assert _statistics_protocol_wrappers == {}
    assert dispatcher._protocol_wrappers == {}


def test_finishing_statistics_twice_is_safe(configuration, loop, tmp_path):
    StatsProtocol.output_dir = tmp_path

    encapsulator = EmbeddedEncapsulator(configuration, loop, None, backend=None)
    encapsulator.encapsulate(StatsProtocol)
    encapsulator.initialize()
    loop.run_until_complete(asyncio.sleep(0.15))

    encapsulator.finish()
    encapsulator.finish()
    finish_statistics(encapsulator.protocol)
