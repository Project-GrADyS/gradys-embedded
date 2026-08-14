"""The original /protocol/setup + /protocol/start pair must keep working.

gradys-sitl-tester and the experiment runbooks drive these two endpoints after
constructing an EmbeddedRunner with a protocol class. That flow has to survive
the move to a mission service, without reintroducing autostart at boot.

Handlers are invoked directly rather than over HTTP so the suite needs no HTTP
client; test_mission_lifecycle covers the same lifecycle through MissionManager.
"""

import asyncio

import pytest
from fastapi import HTTPException

from gradys_embedded.protocol.interface import IProtocol
from gradys_embedded.runner.control_panel import (
    _build_legacy_protocol_router,
    create_control_app,
)
from gradys_embedded.runner.mission import MissionState


class LegacyProtocol(IProtocol):
    def initialize(self):
        self.provider.tracked_variables["ok"] = True

    def handle_timer(self, timer):
        pass

    def handle_packet(self, message):
        pass

    def handle_telemetry(self, telemetry):
        pass

    def finish(self):
        pass


def _endpoint(runner, path, method="POST"):
    """Resolve a legacy handler from the router that defines it.

    Taken from the router rather than from app.routes: newer FastAPI wraps an
    included router in an opaque container, so walking the app's route list is
    version-dependent. `test_legacy_endpoints_are_still_served` checks the
    app-level contract separately via the OpenAPI schema.
    """
    for route in _build_legacy_protocol_router(runner).routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"no {method} {path} in the legacy router")


def test_legacy_endpoints_are_still_served(runner):
    """The published contract, as a client sees it."""
    paths = create_control_app(runner).openapi()["paths"]

    assert "post" in paths["/protocol/setup"]
    assert "post" in paths["/protocol/start"]
    # ...alongside the new surface, not instead of it.
    for path in ["/mission/load", "/mission/setup", "/mission/start",
                 "/mission/stop", "/mission/status",
                 "/protocols", "/protocols/upload",
                 "/runs", "/runs/{run_id}", "/runs/{run_id}/archive",
                 "/runs/{run_id}/files/{filename}"]:
        assert path in paths, path


def test_legacy_flow_loads_the_constructor_protocol(runner, loop):
    """Construct with a protocol, POST setup, POST start — the pre-refactor flow."""
    runner._default_protocol_class = LegacyProtocol
    create_control_app(runner)

    assert runner.mission.state is MissionState.IDLE

    result = loop.run_until_complete(_endpoint(runner, "/protocol/setup")())
    assert result == {"status": "ok"}
    assert runner.mission.state is MissionState.READY
    assert runner.commands == ["goto_initial_position"]

    result = loop.run_until_complete(_endpoint(runner, "/protocol/start")())
    assert result == {"status": "ok"}
    assert runner.mission.state is MissionState.RUNNING
    assert runner._encapsulator.provider.tracked_variables == {"ok": True}


def test_legacy_setup_opens_a_run_directory(runner, loop):
    """Even the legacy path gets per-run data capture."""
    runner._default_protocol_class = LegacyProtocol
    create_control_app(runner)

    loop.run_until_complete(_endpoint(runner, "/protocol/setup")())

    assert runner.mission.run_id is not None
    assert (runner.mission.run_dir(runner.mission.run_id) / "RUN_INFO.json").is_file()


def test_legacy_setup_without_a_protocol_explains_itself(runner, loop):
    runner._default_protocol_class = None
    create_control_app(runner)

    with pytest.raises(HTTPException) as excinfo:
        loop.run_until_complete(_endpoint(runner, "/protocol/setup")())

    assert excinfo.value.status_code == 409
    assert "/mission/load" in excinfo.value.detail


def test_legacy_setup_is_not_autostart(runner):
    """Constructing with a protocol must not fly anything until asked."""
    runner._default_protocol_class = LegacyProtocol
    create_control_app(runner)

    assert runner.mission.state is MissionState.IDLE
    assert runner.commands == []


def test_legacy_double_setup_is_rejected(runner, loop):
    runner._default_protocol_class = LegacyProtocol
    create_control_app(runner)

    loop.run_until_complete(_endpoint(runner, "/protocol/setup")())
    with pytest.raises(HTTPException) as excinfo:
        loop.run_until_complete(_endpoint(runner, "/protocol/setup")())
    assert excinfo.value.status_code == 409


def test_legacy_start_before_setup_is_rejected(runner, loop):
    runner._default_protocol_class = LegacyProtocol
    create_control_app(runner)

    with pytest.raises(HTTPException) as excinfo:
        loop.run_until_complete(_endpoint(runner, "/protocol/start")())
    assert excinfo.value.status_code == 409
