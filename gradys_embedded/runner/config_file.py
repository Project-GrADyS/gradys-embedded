"""Load a :class:`RunnerConfiguration` from a TOML file.

Without this, every drone needs a hand-written runner script carrying a
``RunnerConfiguration`` literal, which makes fleet provisioning mean generating
Python source. A data file can be templated safely and diffed between drones.

The loader is deliberately strict: unknown or malformed keys raise rather than
warn. A fleet's configuration is generated, so a key that does not apply is a
bug in the generator, and the failure modes it would otherwise cause (a drone
in a different coordinate frame, a peer directory that silently drops messages)
are invisible until after a flight.
"""

import tomllib
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

from gradys_embedded.runner.configuration import RunnerConfiguration

# Declared as tuples on the dataclass; TOML can only express arrays.
_TUPLE_FIELDS = {"initial_position", "origin_gps_coordinates"}

# Not a RunnerConfiguration field. It names a default protocol for operators to
# load; it is NOT an autostart -- the service boots idle and only runs a protocol
# when told to over HTTP.
_PROTOCOL_KEY = "protocol"


class ConfigurationError(ValueError):
    """Raised when a configuration file cannot be turned into a valid runner configuration."""


def _coerce_node_ip_dict(raw: Any) -> dict[int, str]:
    """TOML table keys are always strings; the runner keys peers by int node_id."""
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"node_ip_dict must be a table of node_id = \"ip:port\", got {type(raw).__name__}"
        )

    coerced: dict[int, str] = {}
    for key, value in raw.items():
        try:
            node_id = int(key)
        except (TypeError, ValueError):
            raise ConfigurationError(
                f"node_ip_dict key {key!r} is not an integer node id"
            ) from None

        if not isinstance(value, str):
            raise ConfigurationError(
                f"node_ip_dict[{node_id}] must be a string, got {type(value).__name__}"
            )

        # The send path builds f"http://{addr}/message", so a scheme here yields
        # "http://http://..." and every send fails silently. Catch it at load
        # time rather than in flight.
        if "://" in value:
            raise ConfigurationError(
                f"node_ip_dict[{node_id}] = {value!r} must not include a scheme. "
                f"Use the bare form \"host:port\" -- the transport adds the scheme itself."
            )

        coerced[node_id] = value

    return coerced


def parse_configuration(data: dict[str, Any]) -> tuple[RunnerConfiguration, str | None]:
    """Build a runner configuration from an already-parsed TOML mapping.

    Returns the configuration and the protocol import path, if one was given.
    """
    data = dict(data)
    protocol_path = data.pop(_PROTOCOL_KEY, None)
    if protocol_path is not None and not isinstance(protocol_path, str):
        raise ConfigurationError(
            f"{_PROTOCOL_KEY} must be a string like \"my_module:MyProtocol\""
        )

    known = {f.name: f for f in fields(RunnerConfiguration)}

    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigurationError(
            f"Unknown configuration key(s): {', '.join(unknown)}. "
            f"Valid keys are: {', '.join(sorted(known))}, {_PROTOCOL_KEY}"
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

    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        if name == "node_ip_dict":
            kwargs[name] = _coerce_node_ip_dict(value)
        elif name in _TUPLE_FIELDS and isinstance(value, list):
            kwargs[name] = tuple(value)
        else:
            kwargs[name] = value

    try:
        configuration = RunnerConfiguration(**kwargs)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(str(exc)) from exc

    return configuration, protocol_path


def load_configuration(path: str | Path) -> tuple[RunnerConfiguration, str | None]:
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
