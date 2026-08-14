"""Mission lifecycle for the long-running embedded service.

The runner owns process-scoped things: the event loop, the shared aiohttp
session, the data-plane backend and the control server. A :class:`MissionManager`
owns everything that belongs to a single mission -- the protocol, its
encapsulator, the run directory, the resource monitor -- so a protocol can be
swapped without restarting any of the former.

State machine (one mission at a time; a drone flies one protocol):

    IDLE ──load──▶ LOADED ──setup──▶ READY ──start──▶ RUNNING ──stop──▶ RETURNING ──▶ IDLE

`setup` and `start` are deliberately separate: fleet coordination needs every
drone at its initial position before any protocol begins.

RETURNING → IDLE is decided locally. uav_api exposes no flight-mode or
armed-state endpoint, so the service watches the telemetry it already polls and
calls the vehicle down when relative altitude drops below
``telemetry_landed_alt``.
"""

from __future__ import annotations

import importlib
import json
import logging
import shutil
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Type

from gradys_embedded.communication import PROTOCOLS, missing_extra
from gradys_embedded.protocol.interface import IProtocol

if TYPE_CHECKING:
    from gradys_embedded.runner.configuration import RunnerConfiguration
    from gradys_embedded.runner.runner import EmbeddedRunner


RUN_INFO_FILENAME = "RUN_INFO.json"
RUN_LOG_FILENAME = "runner.log"
RESOURCE_CSV_FILENAME = "resource_usage.csv"


class MissionState(str, Enum):
    IDLE = "idle"
    LOADED = "loaded"
    READY = "ready"
    RUNNING = "running"
    RETURNING = "returning"


class MissionError(Exception):
    """A mission operation was rejected. Carries the HTTP status to report."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _safe_component(name: str) -> str:
    """Reduce a network-supplied name to a single path component."""
    cleaned = Path(str(name)).name
    if not cleaned or cleaned in {".", ".."}:
        raise MissionError(f"Invalid name: {name!r}", status_code=400)
    return cleaned


class MissionManager:
    """Owns the current mission and the run archive."""

    def __init__(self, runner: "EmbeddedRunner"):
        self._runner = runner
        self._configuration = runner._configuration
        self._logger = logging.getLogger(__name__)

        self.state = MissionState.IDLE
        self.run_id: Optional[str] = None
        self.protocol_spec: Optional[str] = None

        self._protocol_class: Optional[Type[IProtocol]] = None
        self._run_dir: Optional[Path] = None
        self._mission_config = self._configuration
        self._monitor = None
        self._log_handler: Optional[logging.Handler] = None
        self._started_at: Optional[float] = None

        self.runs_dir = Path(self._configuration.runs_dir)
        self.protocols_dir = Path(self._configuration.protocols_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.protocols_dir.mkdir(parents=True, exist_ok=True)

        # Uploaded protocols are imported by module name, so their directory has
        # to be importable.
        if str(self.protocols_dir) not in sys.path:
            sys.path.insert(0, str(self.protocols_dir))

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        tracked: dict[str, Any] = {}
        encapsulator = self._runner._encapsulator
        if encapsulator is not None:
            # A plain dict on the provider; copied so a caller cannot mutate
            # live protocol state through the API response.
            tracked = dict(encapsulator.provider.tracked_variables)

        elapsed = None
        if self._started_at is not None:
            elapsed = round(time.time() - self._started_at, 3)

        config = self._mission_config
        return {
            "state": self.state.value,
            "node_id": self._configuration.node_id,
            "run_id": self.run_id,
            "protocol": self.protocol_spec,
            "elapsed_seconds": elapsed,
            "tracked_variables": tracked,
            # Echoed so the mission layer can confirm every drone agrees BEFORE
            # starting. The fleet used to guarantee this structurally, by
            # generating identical configs from one inventory; now it is the
            # caller's job, and this is what makes divergence checkable rather
            # than something you discover in the flight data afterwards.
            "frame": {
                "origin_gps_coordinates": list(config.origin_gps_coordinates)
                if config.origin_gps_coordinates else None,
                "x_axis_degrees": config.x_axis_degrees,
                "initial_position": list(config.initial_position)
                if config.initial_position else None,
                "node_ip_dict": dict(config.node_ip_dict) if config.node_ip_dict else None,
                "communication_protocol": config.communication_protocol,
                "auto_scout": config.auto_scout,
            },
        }

    # ------------------------------------------------------------- protocols

    def list_protocols(self) -> list[dict]:
        return [
            {"name": p.stem, "module": p.stem, "size_bytes": p.stat().st_size,
             "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
            for p in sorted(self.protocols_dir.glob("*.py"))
            if p.is_file()
        ]

    def save_protocol(self, filename: str, content: bytes) -> Path:
        name = _safe_component(filename)
        if not name.endswith(".py"):
            raise MissionError("Only .py files are accepted", status_code=400)

        target = self.protocols_dir / name
        target.write_bytes(content)

        # importlib caches by module name, so re-uploading an edited protocol
        # under the same name would keep running the old code. Drop the cached
        # module; the next load re-imports from disk.
        sys.modules.pop(target.stem, None)
        importlib.invalidate_caches()

        self._logger.info(f"Stored protocol {name} ({len(content)} bytes)")
        return target

    def delete_protocol(self, filename: str) -> None:
        name = _safe_component(filename)
        if not name.endswith(".py"):
            name += ".py"
        target = self.protocols_dir / name
        if not target.is_file():
            raise MissionError(f"Protocol {name!r} not found", status_code=404)
        target.unlink()
        sys.modules.pop(target.stem, None)

    def resolve_protocol(self, spec: str) -> Type[IProtocol]:
        """Import a protocol from ``module:ClassName``.

        A bare ``module`` is accepted when the module holds exactly one
        IProtocol subclass, which is the common case for an uploaded file.
        """
        module_name, _, class_name = spec.partition(":")
        module_name = module_name.strip()
        class_name = class_name.strip()

        if not module_name:
            raise MissionError(f"Invalid protocol specification: {spec!r}", status_code=400)

        try:
            if module_name in sys.modules:
                module = importlib.reload(sys.modules[module_name])
            else:
                module = importlib.import_module(module_name)
        except Exception as exc:
            raise MissionError(
                f"Could not import protocol module {module_name!r}: {exc}", status_code=400
            ) from exc

        if class_name:
            protocol = getattr(module, class_name, None)
            if protocol is None:
                raise MissionError(
                    f"Module {module_name!r} has no attribute {class_name!r}", status_code=400
                )
        else:
            candidates = [
                obj for obj in vars(module).values()
                if isinstance(obj, type) and issubclass(obj, IProtocol)
                and obj is not IProtocol and obj.__module__ == module.__name__
            ]
            if len(candidates) != 1:
                names = sorted(c.__name__ for c in candidates)
                raise MissionError(
                    f"Module {module_name!r} defines {len(candidates)} protocols ({names}); "
                    f"name one explicitly as \"{module_name}:ClassName\"",
                    status_code=400,
                )
            protocol = candidates[0]

        if not (isinstance(protocol, type) and issubclass(protocol, IProtocol)):
            raise MissionError(f"{spec} is not an IProtocol subclass", status_code=400)

        return protocol

    # --------------------------------------------------------------- lifecycle

    def _validate_transport(self, protocol: str) -> None:
        """Reject a transport this drone cannot actually serve, at load time.

        Every one of these would otherwise surface as a failure inside the serve
        task — after the mission looked like it loaded successfully.
        """
        if protocol not in PROTOCOLS:
            raise MissionError(
                f"Unknown communication_protocol {protocol!r}; expected one of "
                f"{', '.join(sorted(PROTOCOLS))}.",
                status_code=400,
            )

        extra = missing_extra(protocol)
        if extra is not None:
            raise MissionError(
                f"{protocol!r} needs an optional dependency that is not installed on this "
                f"drone. Provision it with: pip install \"{extra}\".",
                status_code=400,
            )

        if protocol == "zenoh_quic" and self._configuration.certfile is None:
            # QUIC verifies peers against a shared root CA, and the fallback is a
            # per-node ephemeral cert -- so peers would simply refuse to trust
            # each other and the fleet would silently fail to mesh. A mission
            # cannot conjure a fleet-wide cert, so this is a hard rejection.
            raise MissionError(
                "zenoh_quic requires a fleet-wide certfile/keyfile, identical on every "
                "drone, and none is provisioned on this one. Provision TLS material, or "
                "choose another transport.",
                status_code=400,
            )

    def _build_mission_config(self, initial_position=None, origin_gps_coordinates=None,
                              x_axis_degrees=None, node_ip_dict=None,
                              communication_protocol=None, auto_scout=None) -> "RunnerConfiguration":
        """Overlay this run's parameters onto the provisioned configuration.

        Anything not supplied keeps whatever was provisioned, so a drone that is
        still configured the old way behaves exactly as before.
        """
        overrides: dict[str, Any] = {}

        if initial_position is not None:
            overrides["initial_position"] = tuple(initial_position)
        if origin_gps_coordinates is not None:
            overrides["origin_gps_coordinates"] = tuple(origin_gps_coordinates)
        if x_axis_degrees is not None:
            overrides["x_axis_degrees"] = float(x_axis_degrees)

        if node_ip_dict is not None:
            peers = {int(k): str(v) for k, v in node_ip_dict.items()}
            for node, address in peers.items():
                # Same trap the config loader guards: the transport builds
                # f"http://{addr}/message" itself, so a scheme here yields
                # "http://http://..." and every send fails silently.
                if "://" in address:
                    raise MissionError(
                        f"node_ip_dict[{node}] = {address!r} must not include a scheme; "
                        f"use the bare form \"host:port\".",
                        status_code=400,
                    )

            # No zenoh restriction here any more. The session is opened per
            # mission, so its connect endpoints are built from whatever map this
            # mission supplied -- the reason the old guard existed is gone.
            overrides["node_ip_dict"] = peers

        if communication_protocol is not None:
            self._validate_transport(communication_protocol)
            overrides["communication_protocol"] = communication_protocol
        if auto_scout is not None:
            overrides["auto_scout"] = bool(auto_scout)

        if not overrides:
            return self._configuration
        return replace(self._configuration, **overrides)

    @property
    def mission_config(self) -> "RunnerConfiguration":
        """The configuration in force: the mission's overlay, or what was provisioned."""
        return self._mission_config

    async def load(self, protocol: str, initial_position=None, label: Optional[str] = None,
                   origin_gps_coordinates=None, x_axis_degrees=None, node_ip_dict=None,
                   communication_protocol=None, auto_scout=None) -> dict:
        """Open a run and select the protocol to fly.

        The coordinate frame (`origin_gps_coordinates`, `x_axis_degrees`), the peer
        map and the transport are mission-scoped: they describe an experiment
        rather than a machine, so the mission layer supplies them per run.
        Anything omitted falls back to whatever was provisioned.

        Loading also binds the data plane for the chosen transport — only the
        chosen one is ever served — reusing the running listener when the
        transport is unchanged.
        """
        if self.state is not MissionState.IDLE:
            raise MissionError(
                f"Cannot load a mission while {self.state.value}; stop the current mission first"
            )

        # Validated before anything is created, so a rejected transport does not
        # leave an orphan run directory behind.
        if communication_protocol is not None:
            self._validate_transport(communication_protocol)

        self._check_disk_space()

        protocol_class = self.resolve_protocol(protocol)

        run_id = "run_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        if label:
            run_id += "_" + _safe_component(label)
        run_dir = self.runs_dir / run_id
        # A second mission inside the same clock second would otherwise silently
        # write into the previous run's directory.
        suffix = 1
        while run_dir.exists():
            suffix += 1
            run_dir = self.runs_dir / f"{run_id}_{suffix}"
        run_id = run_dir.name
        run_dir.mkdir(parents=True)

        self._protocol_class = protocol_class
        self.protocol_spec = protocol
        self.run_id = run_id
        self._run_dir = run_dir
        self._mission_config = self._build_mission_config(
            initial_position=initial_position,
            origin_gps_coordinates=origin_gps_coordinates,
            x_axis_degrees=x_axis_degrees,
            node_ip_dict=node_ip_dict,
            communication_protocol=communication_protocol,
            auto_scout=auto_scout,
        )

        self._attach_run_log(run_dir)

        # Resolved here rather than at setup so `/mission/status` reports a
        # concrete frame straight away. That is what lets the mission layer spot
        # a drone that fell back to its own GPS -- and therefore disagrees with
        # the rest of the fleet -- before anything takes off.
        self._mission_config = await self._runner.resolve_frame(self._mission_config)

        # Bind the data plane for this mission's transport. Done at load rather
        # than start so every drone is listening before any of them begins
        # sending -- the fleet loads together, then starts together.
        await self._runner.start_backend(self._mission_config)

        self._write_run_info(outcome="loaded")
        self.state = MissionState.LOADED
        self._logger.info(f"Loaded mission {run_id} with protocol {protocol}")
        return self.status()

    async def setup(self) -> dict:
        if self.state is MissionState.READY:
            raise MissionError("Already set up")
        if self.state is not MissionState.LOADED:
            raise MissionError(f"Cannot set up while {self.state.value}; load a mission first")

        if self._mission_config.initial_position is None:
            # It is no longer provisioned, so a mission that omits it has nowhere
            # to fly. Caught here rather than as a TypeError mid-takeoff.
            raise MissionError(
                "No initial_position for this mission. Supply it in POST /mission/load, "
                "or provision a default.",
                status_code=400,
            )

        ok = await self._runner.goto_initial_position(self._mission_config)
        if not ok:
            raise MissionError("Setup failed; check logs", status_code=500)

        self.state = MissionState.READY
        self._write_run_info(outcome="ready")
        return self.status()

    async def start(self) -> dict:
        if self.state is MissionState.RUNNING:
            raise MissionError("Already started")
        if self.state is not MissionState.READY:
            raise MissionError(f"Cannot start while {self.state.value}; run setup first")

        self._start_resource_monitor()
        self._started_at = time.time()
        await self._runner.bootstrap_protocol(self._protocol_class, self._mission_config)

        self.state = MissionState.RUNNING
        self._write_run_info(outcome="running")
        self._logger.info(f"Mission {self.run_id} started")
        return self.status()

    async def stop(self) -> dict:
        if self.state not in (MissionState.RUNNING, MissionState.READY, MissionState.LOADED):
            raise MissionError(f"Nothing to stop; state is {self.state.value}")

        was_flying = self.state in (MissionState.RUNNING, MissionState.READY)

        # Order matters. The protocol is finished first so it flushes its data
        # and, critically, stops emitting mobility commands -- otherwise a
        # still-live protocol would fight the return, or command a vehicle that
        # RTL has already disarmed.
        self._runner.teardown_protocol()

        self._stop_resource_monitor()
        self._started_at = None

        if was_flying:
            # /command/rtl blocks until the vehicle is home AND disarmed (up to
            # ~250s), so it must not be awaited here: stop has to return while
            # the drone is still descending, and a fleet-wide stop has to be
            # able to fan out quickly.
            self._runner.request_return_to_launch()
            self.state = MissionState.RETURNING
            self._write_run_info(outcome="returning")
        else:
            self.state = MissionState.IDLE
            self._write_run_info(outcome="stopped")

        self._logger.info(f"Mission {self.run_id} stopped (state={self.state.value})")
        status = self.status()
        if not was_flying:
            self._finalize_run()
        return status

    def note_landed(self) -> None:
        """Called by the telemetry loop once the vehicle is back on the ground."""
        if self.state is not MissionState.RETURNING:
            return
        self.state = MissionState.IDLE
        self._write_run_info(outcome="completed")
        self._logger.info(f"Mission {self.run_id} complete; vehicle has landed")
        self._finalize_run()

    def shutdown(self) -> None:
        """Process teardown: end any live mission without commanding the vehicle."""
        self._runner.teardown_protocol()
        self._stop_resource_monitor()
        if self.state is not MissionState.IDLE:
            self._write_run_info(outcome="interrupted")
        self._detach_run_log()

    # ------------------------------------------------------------------- runs

    def list_runs(self) -> dict:
        runs = []
        for path in sorted(self.runs_dir.iterdir(), reverse=True):
            if path.is_dir():
                runs.append(self.describe_run(path.name, include_files=False))

        usage = shutil.disk_usage(self.runs_dir)
        return {
            "runs": runs,
            "disk": {
                "free_mb": round(usage.free / (1024 * 1024), 1),
                "total_mb": round(usage.total / (1024 * 1024), 1),
                "min_free_mb": self._configuration.min_free_disk_mb,
                "low": self._free_mb() < self._configuration.min_free_disk_mb,
            },
        }

    def run_dir(self, run_id: str) -> Path:
        path = self.runs_dir / _safe_component(run_id)
        if not path.is_dir():
            raise MissionError(f"Run {run_id!r} not found", status_code=404)
        return path

    def describe_run(self, run_id: str, include_files: bool = True) -> dict:
        path = self.run_dir(run_id)
        info = {}
        info_path = path / RUN_INFO_FILENAME
        if info_path.is_file():
            try:
                info = json.loads(info_path.read_text())
            except json.JSONDecodeError:
                info = {"error": "RUN_INFO.json is unreadable"}

        result = {"run_id": path.name, "info": info}
        if include_files:
            result["files"] = [
                {"name": f.name, "size_bytes": f.stat().st_size}
                for f in sorted(path.iterdir()) if f.is_file()
            ]
        return result

    def run_file(self, run_id: str, filename: str) -> Path:
        path = self.run_dir(run_id) / _safe_component(filename)
        if not path.is_file():
            raise MissionError(f"File {filename!r} not found in run {run_id!r}", status_code=404)
        return path

    def delete_run(self, run_id: str) -> None:
        path = self.run_dir(run_id)
        if path.name == self.run_id and self.state is not MissionState.IDLE:
            raise MissionError("Refusing to delete the run that is currently in progress")
        shutil.rmtree(path)

    # --------------------------------------------------------------- internals

    def _free_mb(self) -> float:
        return shutil.disk_usage(self.runs_dir).free / (1024 * 1024)

    def _check_disk_space(self) -> None:
        threshold = self._configuration.min_free_disk_mb
        if not threshold:
            return
        free = self._free_mb()
        if free < threshold:
            # Refusing here is deliberate: nothing is ever auto-pruned, because
            # silently deleting an uncollected flight is worse than a clear
            # failure before takeoff.
            raise MissionError(
                f"Only {free:.0f} MB free in {self.runs_dir}, below the {threshold} MB minimum. "
                f"Download and delete old runs before starting another.",
                status_code=507,
            )

    def _start_resource_monitor(self) -> None:
        try:
            from resource_monitor import ResourceMonitor
        except ImportError:
            self._logger.warning(
                "resource_monitor is not installed; this run will have no resource_usage.csv"
            )
            return

        self._monitor = ResourceMonitor(csv_path=str(self._run_dir / RESOURCE_CSV_FILENAME))
        try:
            self._monitor.start()
        except Exception as exc:
            # Losing CPU/RAM sampling must never abort a flight.
            self._logger.error(f"Could not start the resource monitor: {exc}")
            self._monitor = None

    def _stop_resource_monitor(self) -> None:
        if self._monitor is None:
            return
        try:
            self._monitor.stop()
        except Exception as exc:
            self._logger.error(f"Could not stop the resource monitor: {exc}")
        finally:
            self._monitor = None

    def _attach_run_log(self, run_dir: Path) -> None:
        self._detach_run_log()
        handler = logging.FileHandler(run_dir / RUN_LOG_FILENAME)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

    def _detach_run_log(self) -> None:
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None

    def _write_run_info(self, outcome: str) -> None:
        if self._run_dir is None:
            return
        config = self._mission_config
        payload = {
            "run_id": self.run_id,
            "node_id": config.node_id,
            "protocol": self.protocol_spec,
            "outcome": outcome,
            # All nullable now that the mission layer supplies them: a run can be
            # loaded before a frame or an initial position has been given.
            "initial_position": list(config.initial_position) if config.initial_position else None,
            "origin_gps_coordinates": list(config.origin_gps_coordinates)
            if config.origin_gps_coordinates else None,
            "x_axis_degrees": config.x_axis_degrees,
            "node_ip_dict": dict(config.node_ip_dict) if config.node_ip_dict else None,
            "communication_protocol": config.communication_protocol,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "notes": (
                "resource_usage.csv samples host-wide CPU and RAM (uav_api included), "
                "not just this process."
            ),
        }
        existing = self._run_dir / RUN_INFO_FILENAME
        if existing.is_file():
            try:
                previous = json.loads(existing.read_text())
                payload = {**previous, **payload}
            except json.JSONDecodeError:
                pass
        payload.setdefault("created_at", payload["updated_at"])
        existing.write_text(json.dumps(payload, indent=2) + "\n")

    def _finalize_run(self) -> None:
        self._detach_run_log()
        self._protocol_class = None
        self._run_dir = None
        self._mission_config = self._configuration
        # run_id and protocol_spec are kept so status() still reports what was
        # last flown after the vehicle has landed.
