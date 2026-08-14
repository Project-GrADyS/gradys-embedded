"""Load a :class:`RunnerConfiguration` from a TOML file.

Without this, every drone needs a hand-written runner script carrying a
``RunnerConfiguration`` literal, which makes fleet provisioning mean generating
Python source. A data file can be templated safely and diffed between drones.

The loader is deliberately strict: unknown, malformed or mission-level keys
raise rather than warn. A fleet's configuration is generated, so a key that
does not apply is a bug in the generator, and the failure modes it would
otherwise cause (a port that never binds, a mission parameter an operator
believes is set but is silently ignored) are invisible until after a flight.
Only provisioned, machine-bound keys belong here; everything describing an
experiment goes through POST /mission/load.
"""

import tomllib
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

from gradys_embedded.runner.configuration import MissionConfiguration, RunnerConfiguration

# Mission-level parameters describe an experiment, not a machine. They are NOT
# provisionable: POST /mission/load is their only gateway, so the loader rejects
# them by name rather than leaving a config that silently half-applies.
_MISSION_LEVEL_KEYS = frozenset(f.name for f in fields(MissionConfiguration))

# Keys the loader once accepted, with a pointed error instead of the generic
# unknown-key one: the message is the migration path for configs written
# against the old surface.
_REMOVED_KEYS = {
    "protocol": (
        "the default-protocol concept was removed; every POST /mission/load "
        "names its protocol"
    ),
}


class ConfigurationError(ValueError):
    """Raised when a configuration file cannot be turned into a valid runner configuration."""


def parse_configuration(data: dict[str, Any]) -> RunnerConfiguration:
    """Build a runner configuration from an already-parsed TOML mapping."""
    data = dict(data)

    known = {f.name: f for f in fields(RunnerConfiguration)}

    mission_level = sorted(set(data) & _MISSION_LEVEL_KEYS)
    if mission_level:
        raise ConfigurationError(
            f"Mission-level key(s) in the provisioned configuration: "
            f"{', '.join(mission_level)}. These describe an experiment, not a "
            f"machine -- supply them in POST /mission/load; they can no longer "
            f"be provisioned."
        )

    removed = sorted(set(data) & set(_REMOVED_KEYS))
    if removed:
        reasons = "; ".join(f"{key}: {_REMOVED_KEYS[key]}" for key in removed)
        raise ConfigurationError(f"Removed configuration key(s): {reasons}")

    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigurationError(
            f"Unknown configuration key(s): {', '.join(unknown)}. "
            f"Valid keys are: {', '.join(sorted(known))}"
        )

    missing = sorted(
        name
        for name, f in known.items()
        if name not in data and f.default is MISSING and f.default_factory is MISSING
    )
    if missing:
        raise ConfigurationError(
            f"Missing required configuration key(s): {', '.join(missing)}"
        )

    try:
        configuration = RunnerConfiguration(**data)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(str(exc)) from exc

    return configuration


def load_configuration(path: str | Path) -> RunnerConfiguration:
    """Read a TOML file and build a runner configuration from it."""
    path = Path(path).expanduser()

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigurationError(f"Configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"{path} is not valid TOML: {exc}") from exc

    try:
        return parse_configuration(data)
    except ConfigurationError as exc:
        raise ConfigurationError(f"{path}: {exc}") from None
